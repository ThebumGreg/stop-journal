"""Calendar invites: today's stops on your Outlook calendar, so coworkers who can see it know where you are.

The published calendar link is read-only, so JobHub cannot write to Outlook directly. Instead it emails a calendar
invite (iCalendar METHOD:REQUEST) per stop from the Gmail account in Settings → Email to your work address; Outlook
puts it on the calendar. When the stop changes (name, times, finished, visit result) the same event is updated
(same UID, higher SEQUENCE); when the stop is deleted, merged away or re-detected it is cancelled (METHOD:CANCEL).

- Runs from the background thread every CALENDAR_INVITE_MIN minutes (default 10) for TODAY's stops only. A stop
  that has not changed sends nothing. A stop you are still at ends at the next quarter hour, so it updates about
  every 15 minutes at most while you are there.
- Basic customer information only: type, name, address, ticket, times and whether it is finished. Never the work
  notes, parts or prices. Personal stops are a plain "Personal" block (no place); lunch is "Lunch".
- Our events are titled "JobHub · …" and their UIDs end in @jobhub; the calendar reader skips them, so they are
  never mistaken for on-site appointments.
- At most MAX_PER_DAY invites a day (a safeguard against a loop).
"""
import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import mailer, service

log = logging.getLogger("jobhub.calendar")
UID_DOMAIN = "jobhub"
MAX_PER_DAY = 150
QUARTER = 15 * 60
TITLE_PREFIX = "JobHub · "
_last_run = {}


def enabled(cfg):
    return cfg.calendar_invites == "on" and mailer.enabled(cfg)


def is_ours(uid, title=""):
    return str(uid).split("@")[-1].startswith(UID_DOMAIN) or str(title).startswith(TITLE_PREFIX)


def event_for(s, now):
    """A stop (day payload) -> what its calendar event should say, or None for no event."""
    if s["kind"] != "stop":
        return None
    t = s["stop_type"] or ""
    name = s["business_name"] or ""
    start, end = s["arrive_ts"], s["depart_ts"]
    here = bool(s.get("here_now"))
    if here:
        end = max(end, now)
        end = (end // QUARTER + 1) * QUARTER                     # round up: fewer updates while you are there
    end = max(end, start + 5 * 60)
    label = service.STOP_TYPES.get(t, "Stop")
    if t == "personal":
        return {"title": TITLE_PREFIX + "Personal", "start": start, "end": end, "location": "", "desc": ""}
    if t == "lunch":
        return {"title": TITLE_PREFIX + "Lunch", "start": start, "end": end, "location": "", "desc": ""}
    finished = s["status"] == "Confirmed" and not s["needs"] or (t == "gas" and not s["needs"])
    title = TITLE_PREFIX + (name or label) + (f" ({label})" if name and t not in ("job", "") else "")
    if finished:
        title = TITLE_PREFIX + "✓ " + title[len(TITLE_PREFIX):]
    vr = service.VISIT_RESULTS.get(s.get("visit_result") or "")
    if vr and s.get("visit_result") != "complete":
        title += " · " + vr
    lines = [f"Type: {label}"]
    if s.get("ticket"):
        lines.append(f"Ticket: {s['ticket']}")
    lines.append("Status: " + ("On site now" if here else "Finished" if finished else "Visited"))
    if vr:
        lines.append(f"Visit result: {vr}")
    lines.append("Added by JobHub from the technician's location; updates automatically.")
    loc = s["address"] if t not in ("traffic",) else ""
    return {"title": title, "start": start, "end": end, "location": loc or "", "desc": "\n".join(lines)}


def _ics(cfg, uid, seq, ev, method):
    import icalendar
    cal = icalendar.Calendar()
    cal.add("prodid", "-//JobHub//Stops//EN")
    cal.add("version", "2.0")
    cal.add("method", method)
    e = icalendar.Event()
    e.add("uid", uid)
    e.add("sequence", seq)
    e.add("dtstamp", datetime.now(timezone.utc))
    e.add("dtstart", datetime.fromtimestamp(ev["start"], timezone.utc))
    e.add("dtend", datetime.fromtimestamp(ev["end"], timezone.utc))
    e.add("summary", ev["title"])
    if ev.get("location"):
        e.add("location", ev["location"])
    if ev.get("desc"):
        e.add("description", ev["desc"])
    org = icalendar.vCalAddress("mailto:" + (cfg.mail_from or cfg.smtp_user))
    org.params["cn"] = "JobHub"
    e.add("organizer", org)
    att = icalendar.vCalAddress("mailto:" + cfg.mail_to)
    att.params.update({"role": "REQ-PARTICIPANT", "partstat": "ACCEPTED", "rsvp": "FALSE"})
    e.add("attendee", att)
    e.add("transp", "OPAQUE")
    e.add("status", "CANCELLED" if method == "CANCEL" else "CONFIRMED")
    cal.add_component(e)
    return cal.to_ical()


def _send(cfg, uid, seq, ev, method):
    msg = mailer._message(cfg, ("Canceled: " if method == "CANCEL" else "") + ev["title"],
                          ev.get("desc") or ev["title"])
    data = _ics(cfg, uid, seq, ev, method)
    msg.add_alternative(data, maintype="text", subtype="calendar", params={"method": method})
    msg.add_attachment(data, maintype="application", subtype="ics", filename="invite.ics")
    mailer.send(cfg, msg)


def sync(conn, cfg, date, now=None, send=_send):
    """Bring the calendar in line with the day's stops. Returns (sent, cancelled). MailError is logged, not raised."""
    now = int(now or time.time())
    day = service.day_payload(conn, cfg, date)
    want = {}
    for s in day["stops"]:
        ev = event_for(s, now)
        if ev:
            want[s["id"]] = ev
    rows = {r["stop_id"]: r for r in conn.execute("SELECT * FROM cal_sync WHERE date=?", (date,))}
    sent_today = conn.execute("SELECT COUNT(*) FROM cal_sync_log WHERE day=?", (date,)).fetchone()[0]
    sent = cancelled = 0

    def budget():
        return sent_today + sent + cancelled < MAX_PER_DAY

    for sid, ev in want.items():
        h = hashlib.sha1(json.dumps(ev, sort_keys=True).encode()).hexdigest()
        r = rows.get(sid)
        if r and r["hash"] == h and not r["cancelled"]:
            continue
        if not budget():
            break
        uid = r["uid"] if r else f"stop-{sid}-{now}@{UID_DOMAIN}"
        seq = (r["seq"] + 1) if r else 0
        try:
            send(cfg, uid, seq, ev, "REQUEST")
        except mailer.MailError as e:
            log.warning("calendar invite failed: %s", str(e)[:120])
            return sent, cancelled
        conn.execute("INSERT INTO cal_sync(stop_id, date, uid, seq, hash, cancelled, event_json) VALUES (?,?,?,?,?,0,?) "
                     "ON CONFLICT(stop_id) DO UPDATE SET uid=excluded.uid, seq=excluded.seq, hash=excluded.hash, "
                     "cancelled=0, event_json=excluded.event_json", (sid, date, uid, seq, h, json.dumps(ev)))
        conn.execute("INSERT INTO cal_sync_log(day, ts, stop_id, method) VALUES (?,?,?,'REQUEST')", (date, now, sid))
        conn.commit()
        sent += 1
    for sid, r in rows.items():
        if sid in want or r["cancelled"] or not budget():
            continue
        ev = json.loads(r["event_json"] or "{}") or {"title": TITLE_PREFIX + "Stop", "start": now, "end": now + 300}
        try:
            send(cfg, r["uid"], r["seq"] + 1, ev, "CANCEL")
        except mailer.MailError as e:
            log.warning("calendar cancel failed: %s", str(e)[:120])
            return sent, cancelled
        conn.execute("UPDATE cal_sync SET cancelled=1, seq=seq+1 WHERE stop_id=?", (sid,))
        conn.execute("INSERT INTO cal_sync_log(day, ts, stop_id, method) VALUES (?,?,?,'CANCEL')", (date, now, sid))
        conn.commit()
        cancelled += 1
    if sent or cancelled:
        log.info("calendar %s: %d invite%s sent, %d cancelled", date, sent, "" if sent == 1 else "s", cancelled)
    return sent, cancelled


def tick(conn, cfg, client, now, marks, in_window):
    """From the background thread: every CALENDAR_INVITE_MIN minutes, pull new points (during the work day) and sync."""
    if not enabled(cfg):
        return None
    date = datetime.fromtimestamp(now, ZoneInfo(cfg.app_tz)).date().isoformat()
    every = max(cfg.calendar_invite_min, 5) * 60
    if now - _last_run.get(date, 0) < every:
        return None
    _last_run[date] = now
    if (in_window or marks.get("start")) and not marks.get("end") and \
            cfg.recorder_url and cfg.recorder_user and cfg.recorder_device:
        from .alerts import _last_sync
        if now - _last_sync.get(date, 0) >= 120:
            _last_sync[date] = now
            try:
                service.catch_up(conn, client, cfg, date)
            except Exception as e:
                log.warning("background sync failed: %s", type(e).__name__)
    return sync(conn, cfg, date, now)
