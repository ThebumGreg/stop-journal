"""Background alert checks. Each check turns the current situation into
Conditions (key, active?, message); `apply` sends a notification when a condition
becomes active, optionally repeats it, and re-arms when it clears.

Dedupe state lives in SQLite (alert_state), claimed inside a write transaction, so
several workers or a restart can never double-send.

Add a new alert by writing a function that returns Conditions and appending it to
`conditions()`.
"""
import logging
import threading
import time
from collections import namedtuple
from datetime import datetime
from zoneinfo import ZoneInfo

from . import db, notify, settings

log = logging.getLogger("jobhub.alerts")
Condition = namedtuple("Condition", "key active title message priority")
RETRY_S = 600  # after a failed delivery, wait this long before trying again


def _t(text):
    h, m = text.split(":")
    return int(h) * 60 + int(m)


def work_window(cfg, now):
    """(in_window, window_start_epoch) for 'now' in the app timezone."""
    local = datetime.fromtimestamp(now, ZoneInfo(cfg.app_tz))
    try:
        days = settings.parse_days(cfg.work_days)
        start, end = _t(cfg.work_start), _t(cfg.work_end)
    except ValueError:
        days, start, end = set(range(5)), 7 * 60, 17 * 60
    minute = local.hour * 60 + local.minute
    start_epoch = local.replace(hour=start // 60, minute=start % 60, second=0, microsecond=0).timestamp()
    return (local.weekday() in days and start <= minute <= end), int(start_epoch)


def _clock(cfg, ts):
    return datetime.fromtimestamp(ts, ZoneInfo(cfg.app_tz)).strftime("%-I:%M %p")


def no_data_conditions(cfg, in_window, window_start, last_tst, recorder_error, now):
    """Alert when no point has arrived for N minutes during work hours. The clock starts
    at the beginning of the work window, so an overnight gap doesn't alarm at 07:00."""
    off = [Condition("no_data", False, "", "", 0), Condition("recorder_down", False, "", "", 0)]
    if cfg.alert_no_data != "on" or not notify.configured(cfg) or not in_window:
        return off
    if recorder_error:
        return [Condition("recorder_down", True, "JobHub – can't reach Recorder",
                          "JobHub can't reach the OwnTracks Recorder, so no location data is "
                          "arriving. Check the Recorder server.", 0), off[0]]
    age = (now - max(last_tst or 0, window_start)) / 60
    msg = f"No location data from OwnTracks for {int(age)} min"
    if last_tst and last_tst >= window_start:
        msg += f" (last point {_clock(cfg, last_tst)})"
    msg += ". Open JobHub or the OwnTracks app on your phone and check it is running."
    return [Condition("no_data", age >= cfg.alert_no_data_min, "JobHub – no location data", msg, 0), off[1]]


def today_marks(conn, cfg, now):
    date = datetime.fromtimestamp(now, ZoneInfo(cfg.app_tz)).date().isoformat()
    r = conn.execute("SELECT start_utc, end_utc FROM day_marks WHERE date=?", (date,)).fetchone()
    return {"date": date, "start": r["start_utc"] if r else None, "end": r["end_utc"] if r else None}


def draft_conditions(conn, cfg, now, marks, in_work_day):
    """Remind me to review stops once the day is over (End day tapped, or 30 min past work end)."""
    off = [Condition("drafts", False, "", "", 0)]
    if cfg.alert_drafts != "on":
        return off
    local = datetime.fromtimestamp(now, ZoneInfo(cfg.app_tz))
    try:
        end_min = _t(cfg.work_end)
    except ValueError:
        end_min = 17 * 60
    if marks["end"]:
        due = now >= marks["end"] + 300
    else:
        due = in_work_day and local.hour * 60 + local.minute >= end_min + 30
    n = conn.execute("SELECT COUNT(*) FROM stops WHERE date=? AND status='Draft' AND kind='stop'",
                     (marks["date"],)).fetchone()[0]
    if not (due and n):
        return off
    return [Condition("drafts", True, "JobHub – review today's stops",
                      f"{n} stop{'s' if n != 1 else ''} from today still need review. "
                      "Open JobHub to confirm them before exporting.", 0)]


def low_stock_conditions(conn, cfg):
    off = [Condition("low_stock", False, "", "", 0)]
    if cfg.alert_low_stock != "on":
        return off
    n = conn.execute("SELECT COUNT(*) FROM parts WHERE tracked=1 AND active=1 "
                     "AND reorder_at IS NOT NULL AND COALESCE(on_hand,0) <= reorder_at").fetchone()[0]
    if not n:
        return off
    return [Condition("low_stock", True, "JobHub – truck stock low",
                      f"{n} truck part{'s are' if n != 1 else ' is'} at or below the reorder level. "
                      "Open the Truck page to review.", 0)]


def apply(conn, cfg, cond, now, send):
    """Send/re-arm for one condition. Returns True if a notification was delivered."""
    conn.execute("BEGIN IMMEDIATE")
    st = conn.execute("SELECT * FROM alert_state WHERE key=?", (cond.key,)).fetchone()
    do_send = False
    if cond.active:
        if st is None or not st["active"]:
            do_send = st is None or now - (st["last_attempt"] or 0) >= RETRY_S
        elif cfg.alert_repeat_min > 0 and now - (st["last_sent"] or 0) >= cfg.alert_repeat_min * 60:
            do_send = True
    elif st is not None and st["active"]:
        conn.execute("UPDATE alert_state SET active=0 WHERE key=?", (cond.key,))
    if not do_send:
        conn.commit()
        return False
    conn.execute("INSERT INTO alert_state(key,active,last_attempt) VALUES (?,0,?) "
                 "ON CONFLICT(key) DO UPDATE SET last_attempt=excluded.last_attempt", (cond.key, now))
    conn.commit()  # claimed: other workers now see a fresh attempt
    try:
        send(cfg, cond.title, cond.message, cond.priority, cfg.app_url or None)
        ok, err = True, None
    except notify.NotifyError as e:
        ok, err = False, str(e)
    conn.execute("UPDATE alert_state SET active=?, last_sent=CASE WHEN ? THEN ? ELSE last_sent END "
                 "WHERE key=?", (1 if ok else 0, 1 if ok else 0, now, cond.key))
    conn.commit()
    notify.record(conn, cond.title, cond.message, ok, err)
    return ok


def run_once(base, client, now=None, send=notify.send):
    now = int(now if now is not None else time.time())
    conn = db.connect(base.data_dir)
    try:
        cfg = settings.effective(base, conn)
        in_window, start = work_window(cfg, now)
        in_work_day = in_window or _is_work_day(cfg, now)
        marks = today_marks(conn, cfg, now)
        if marks["end"]:
            in_window = False                      # day ended: no more no-data alerts
        elif marks["start"]:
            in_window, start = True, marks["start"]  # day started: watch from then on
        last = err = None
        if (cfg.alert_no_data == "on" and notify.configured(cfg) and in_window
                and cfg.recorder_url and cfg.recorder_user and cfg.recorder_device):
            try:
                last = client.last_tst()
            except Exception as e:  # unreachable Recorder is itself worth an alert
                err = type(e).__name__
        conds = list(no_data_conditions(cfg, in_window, start, last, err, now))
        if notify.configured(cfg):
            conds += draft_conditions(conn, cfg, now, marks, in_work_day)
            conds += low_stock_conditions(conn, cfg)
        for cond in conds:
            apply(conn, cfg, cond, now, send)
    finally:
        conn.close()


def _is_work_day(cfg, now):
    try:
        days = settings.parse_days(cfg.work_days)
    except ValueError:
        days = set(range(5))
    return datetime.fromtimestamp(now, ZoneInfo(cfg.app_tz)).weekday() in days


def start(base, client, interval=60):
    def loop():
        while True:
            try:
                run_once(base, client)
            except Exception as e:  # never let the thread die; log the type only
                log.warning("alert check failed: %s", type(e).__name__)
            time.sleep(interval)

    threading.Thread(target=loop, daemon=True, name="jobhub-alerts").start()
