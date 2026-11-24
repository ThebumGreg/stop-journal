"""Running late for an on-site appointment.

1. Alert: during the work day the alert thread works out the drive time from the phone's position to the next on-site
   calendar appointment (the same ETA as the Home page). When the arrival would be ALERT_LATE_MIN or more minutes after
   the appointment start, one Pushover alert is sent per appointment. Its text is generic (the appointment time and how
   late, never the customer, address or place: it passes through a third party) and it opens the Home page.
2. Tell the customer: the Home page shows "You'll be about 15 min late" with **Tell the customer**. Only that tap sends
   the `JobHub Late Notice` email (to LATE_NOTICE_TO, else MAIL_TO): labelled lines in the body and a JSON attachment the
   work AI agent reads to contact the customer. Never sent automatically.
"""
import json
import logging
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import mailer

log = logging.getLogger("jobhub.late")
CHECK_EVERY_S = 180           # the alert thread re-checks the ETA this often (routing results are cached too)
WITHIN_S = 3 * 3600           # only appointments starting in the next few hours
FRESH_S = 15 * 60             # the phone's position must be this recent
_last_check = {}


def last_sent(conn, date, uid, tz="UTC"):
    if not uid:
        return None
    r = conn.execute("SELECT ts, minutes_late, mail_to FROM late_notices WHERE date=? AND event_uid=? AND ok=1 "
                     "ORDER BY id DESC LIMIT 1", (date, uid)).fetchone()
    return {"ts": r["ts"], "time": datetime.fromtimestamp(r["ts"], ZoneInfo(tz)).strftime("%-I:%M %p"),
            "minutes_late": r["minutes_late"], "mail_to": r["mail_to"]} if r else None


def _next_event(day, now):
    return next((e for e in day.get("calendar", []) if e["state"] == "upcoming"
                 and e["start_ts"] - now <= WITHIN_S and e["end_ts"] > now), None)


def check(conn, cfg, date, now, live, search):
    """(event, eta) when you will be late for the next appointment, else None."""
    from . import service, summary
    if not live or live.get("lat") is None or live.get("last_update_s", 0) > FRESH_S:
        return None
    ev = _next_event(service.day_payload(conn, cfg, date), now)
    if not ev:
        return None
    eta, _ = summary.eta(conn, cfg, ev, now, live, search)
    if not eta or eta.get("arrived") or eta["late_min"] < cfg.alert_late_min:
        return None
    return ev, eta


def conditions(conn, cfg, client, now, date, search, Condition):
    """Alert conditions (alerts.Condition) for running late. Checked every CHECK_EVERY_S."""
    from . import live as live_mod
    if cfg.alert_late != "on" or not cfg.calendar_url:
        return []
    if not (cfg.recorder_url and cfg.recorder_user and cfg.recorder_device):
        return []
    if now - _last_check.get(date, 0) < CHECK_EVERY_S:
        return []
    _last_check[date] = now
    try:
        pts = client.fetch(datetime.fromtimestamp(now - 3600, timezone.utc), datetime.fromtimestamp(now, timezone.utc))
    except Exception as e:
        log.warning("late check: could not reach the Recorder (%s)", type(e).__name__)
        return []
    st = live_mod.status(pts or [], now, cfg.stop_radius_m, cfg.max_acc_m)
    try:
        hit = check(conn, cfg, date, now, st, search)
    except Exception as e:                        # map search / routing trouble: try again next time
        log.warning("late check failed: %s", type(e).__name__)
        return []
    if not hit:
        return []
    ev, eta = hit
    url = (cfg.app_url.rstrip("/") + "/?late=1") if cfg.app_url else None
    log.info("running late: about %d min for an appointment at %s", eta["late_min"], ev["start"])
    return [Condition("late:" + ev["uid"], True, "JobHub – running late",
                      f"You may be about {eta['late_min']} min late for your {ev['start']} appointment "
                      f"(arriving about {eta['arrive']}). Open JobHub to tell the customer.", 0, url, True)]


def notice(cfg, ev, eta, now, note=""):
    """The late notice the work AI agent reads (schema "1")."""
    tz = ZoneInfo(cfg.app_tz)
    iso = lambda ts: datetime.fromtimestamp(ts, tz).isoformat(timespec="minutes")
    return {"type": "jobhub_late_notice", "schema_version": "1", "sent_at": iso(now), "timezone": cfg.app_tz,
            "technician": {"name": cfg.tech_name or "", "email": cfg.tech_email or ""},
            "hubspot_ticket": ev.get("ticket") or "", "qbo_estimate": ev.get("estimate") or "",
            "customer": ev.get("name") or "", "address": ev.get("location") or "", "contact": ev.get("contact") or "",
            "appointment_start": iso(ev["start_ts"]), "appointment_end": iso(ev["end_ts"]),
            "new_eta": iso(eta["arrive_ts"]), "minutes_late": eta["late_min"],
            "eta_method": "road route" if eta.get("method") == "routed" else "estimate",
            "note": (note or "").strip()[:300],
            "calendar_event_uid": ev.get("uid") or ""}


def body(n, sig="", message_id="", revision=1):
    lines = ["JobHub Late Notice: the technician is running late for this appointment.",
             "Details are in the attached JSON.", "",
             f"Technician: {mailer.tech_text(n['technician'])}",
             f"Customer: {n['customer'] or '(not given)'}",
             f"Address: {n['address'] or '(not given)'}",
             f"HubSpot ticket: {n['hubspot_ticket'] or '(not given)'}",
             f"Appointment: {n['appointment_start']} to {n['appointment_end']}",
             f"New arrival time: {n['new_eta']} (about {n['minutes_late']} min late, {n['eta_method']})"]
    if n["note"]:
        lines.append(f"Note from the technician: {n['note']}")
    if message_id:
        from .invoice_request import v2_lines
        lines += v2_lines(revision, message_id, "replaces the earlier notice for this appointment")
    if sig:
        lines.append(f"Signature: hmac-sha256={sig} (of the attached JSON file)")
    return "\n".join(lines) + "\n"


def send(conn, cfg, date, uid, now, live, search, note=""):
    """Tell the customer: email the late notice. Returns the log entry. Raises mailer.MailError / ValueError."""
    from . import service, summary
    to = cfg.late_notice_to or cfg.mail_to
    if not to:
        raise ValueError("Add where late notices go in Settings → Email (Send late notices to, or Send reports to).")
    ev = next((e for e in service.day_payload(conn, cfg, date).get("calendar", []) if e.get("uid") == uid), None)
    if not ev:
        raise ValueError("That appointment is no longer on your calendar for today.")
    eta, why = summary.eta(conn, cfg, ev, now, live, search)
    if not eta:
        raise ValueError(why or "JobHub does not know where you are right now, so it cannot work out the new arrival time.")
    if eta.get("arrived"):
        raise ValueError("You are already there.")
    if eta["late_min"] <= 0:
        raise ValueError(f"You are on time now (arriving about {eta['arrive']}): nothing was sent.")
    from . import message_v2 as v2
    n = notice(cfg, ev, eta, now, note)
    revision, supersedes = v2.previous(conn, "late_notices", "event_uid=?", (uid,))
    mid = v2.new_id()
    stamp = datetime.fromtimestamp(ev["start_ts"], ZoneInfo(cfg.app_tz)).strftime("%Y-%m-%d-%H%M")
    if cfg.message_format == "2":
        stop_uid = next((s["uid"] for s in service.day_payload(conn, cfg, date)["stops"] if s["id"] == ev.get("stop_id")), "")
        data = json.dumps(v2.late_notice(conn, cfg, ev, eta, now, note, stop_uid, revision, supersedes, mid),
                          indent=2, ensure_ascii=False).encode()
        subject = v2.subject("late_notice", date, n["customer"], revision)
        fname = v2.file_stem("late_notice", date, stamp[11:], revision) + ".json"
    else:
        data = json.dumps(n, indent=2, ensure_ascii=False).encode()
        subject = f"JobHub Late Notice {n['hubspot_ticket'] or n['customer'] or ev['start']}"
        fname = f"jobhub-late-notice-{stamp}.json"
    sig = mailer.signature(cfg.report_signing_key, data) if cfg.report_signing_key else ""
    msg = mailer._message(cfg, subject, body(n, sig, mid if cfg.message_format == "2" else "", revision))
    msg.replace_header("To", to)
    msg.add_attachment(data, maintype="application", subtype="json", filename=fname)
    ok, err = True, None
    try:
        mailer.send(cfg, msg)
    except mailer.MailError as e:
        ok, err = False, str(e)
    conn.execute("INSERT INTO late_notices(ts,date,event_uid,minutes_late,eta_ts,mail_to,ok,error,note,message_id) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?)", (int(time.time()), date, uid, eta["late_min"], eta["arrive_ts"], to,
                                                  1 if ok else 0, err, n["note"] or None, mid if ok else None))
    conn.commit()
    if not ok:
        log.warning("late notice not sent: %s", err)
        raise mailer.MailError(err)
    log.info("late notice emailed (about %d min late)%s", eta["late_min"], ", signed" if sig else "")
    return {"ts": int(time.time()), "minutes_late": eta["late_min"], "mail_to": to, "new_eta": eta["arrive"]}
