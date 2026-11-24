"""Send for invoice: one finished Time & Material stop, emailed to the work AI agent so it can invoice while the
technician is still on site (the agent prices labor and handles the invoice and payment; JobHub only documents).

Only when the technician taps Send (never automatic). The stop's data is the day report's own block for that stop
(`report.build_day`), so the numbers match the day report. Labor is documented as minutes on site, billed through the
moment of sending when the GPS says you are still there ("Here now"), else through the departure. Each send is logged in `invoice_requests`; a later send of the same stop says "(update n)", and
the day report carries `invoice_request` on that stop so the agent can flag (not re-invoice) later differences.
"""
import json
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from . import mailer, report, service
from .service import EditError

log = logging.getLogger("jobhub.invoice")
CATEGORY = "Time & Material"          # the only kind invoiced from a stop for now (user decision 2026-11-23)
SCHEMA = "1"


def sent_for_day(conn, date, tz):
    """{stop_uid: newest successful send} for the day (day payload and report)."""
    out = {}
    for r in conn.execute("SELECT * FROM invoice_requests WHERE date=? AND ok=1 ORDER BY id", (date,)):
        out[r["stop_uid"]] = {"version": r["version"], "sent_at": datetime.fromtimestamp(r["ts"], ZoneInfo(tz)).isoformat(timespec="minutes"),
                              "sent_time": datetime.fromtimestamp(r["ts"], ZoneInfo(tz)).strftime("%-I:%M %p"),
                              "billed_through": r["billed_through"], "time_on_site_minutes": r["minutes"]}
    return out


def build(conn, cfg, stop_id, now=None):
    """(request dict, warnings). Raises EditError when the stop cannot be invoiced from here."""
    now = int(now or time.time())
    tz = ZoneInfo(cfg.app_tz)
    row = conn.execute("SELECT * FROM stops WHERE id=?", (stop_id,)).fetchone()
    if not row:
        raise EditError("That stop no longer exists.")
    payload = service.day_payload(conn, cfg, row["date"])
    ps = next((s for s in payload["stops"] if s["id"] == stop_id), None)
    if ps is None:
        raise EditError("That stop no longer exists.")
    if (ps.get("work_category") or "") != CATEGORY:
        raise EditError(f"Send for invoice is for {CATEGORY} jobs only. This stop's category is "
                        f"{ps.get('work_category') or 'not set'}.")
    if ps["status"] != "Confirmed" or ps.get("needs"):
        raise EditError("Finish the stop first (Finish stop), then send it for invoice.")
    day = report.build_day(payload, cfg)
    s = next((x for x in day["stops"] if x["stop_uid"] == ps["uid"]), None)
    if s is None:
        raise EditError("This stop is not in the report (is it Confirmed?).")
    clock = lambda ts: datetime.fromtimestamp(ts, tz).strftime("%H:%M")
    today = datetime.fromtimestamp(now, tz).date().isoformat()
    # "Here now" (the newest GPS point is fresh and inside this stop): bill through the moment of sending. Otherwise the
    # departure (typed, Leaving now, or the last point here), so a drive away is never billed as labor.
    still_here = row["date"] == today and bool(ps.get("here_now")) and not ps.get("depart_pinned") and now > ps["depart_ts"]
    through = now if still_here else ps["depart_ts"]
    minutes = s["time_on_site_minutes"] + (max(0, round((through - ps["depart_ts"]) / 60)) if still_here else 0)
    photos = [p for p in day.get("photos") or [] if p["id"] in set(s.get("photos") or [])]
    billable = lambda xs: [x for x in xs if x.get("billable")]
    not_billed = lambda xs: [x for x in xs if not x.get("billable")]
    req = {
        "type": "jobhub_invoice_request", "schema_version": SCHEMA, "invoice_requested": True,
        "timezone": cfg.app_tz, "technician": {"name": cfg.tech_name or "", "email": cfg.tech_email or ""},
        "date": row["date"], "report_key": row["date"], "stop_uid": s["stop_uid"], "stop_number": s["stop_number"],
        "customer": {"business_name": s["business_name"], "address": s["address"],
                     "hubspot_company_id": s["hubspot_company_id"] or ""},
        "hubspot_ticket": s["hubspot_ticket"], "ticket_action": s["ticket_action"], "qbo_estimate": s["qbo_estimate"],
        "category": s["category"], "billing": s["billing"], "visit_result": s["visit_result"],
        "labor": {"arrival": s["arrival"], "billed_through": clock(through), "still_on_site": bool(still_here),
                  "time_on_site_minutes": minutes, "hours": round(minutes / 60, 2),
                  "lunch_non_billable": s["lunch_non_billable"], "personal_breaks_unpaid": s["personal_breaks_unpaid"]},
        "drive_from_previous_stop": s["drive_from_previous_stop"],
        "parts": billable(s["parts_used"]), "parts_not_billed": not_billed(s["parts_used"]),
        "expenses": billable(s["expenses"]), "expenses_not_billed": not_billed(s["expenses"]),
        "work_performed": s["work_performed"], "follow_ups": s["follow_ups"],
        "photos": [{"attachment": p["attachment"], "kind": p["kind"], "label": p["label"]} for p in photos],
    }
    warn = []
    if not s["hubspot_company_id"]:
        warn.append("No HubSpot company ID for this place: the agent matches the customer by name and address.")
    for p in req["parts"]:
        if p["unit_cost"] is None:
            warn.append(f"{p['name']} is billable but has no price.")
        if not p["part_number"]:
            warn.append(f"{p['name']} has no part number.")
    if s["visit_result"] and s["visit_result"] != service.VISIT_RESULTS["complete"]:
        warn.append(f"Visit result is “{s['visit_result']}”: the job is not complete.")
    if not cfg.tech_name or not cfg.tech_email:
        warn.append("Your name or work email is not set (Settings → General → Technician).")
    return req, warn, photos


def history(conn, stop_uid, tz, limit=5):
    return [{"time": datetime.fromtimestamp(r["ts"], ZoneInfo(tz)).strftime("%-I:%M %p"), "version": r["version"],
             "ok": bool(r["ok"]), "error": r["error"] or "", "mail_to": r["mail_to"], "minutes": r["minutes"]}
            for r in conn.execute("SELECT * FROM invoice_requests WHERE stop_uid=? ORDER BY id DESC LIMIT ?",
                                  (stop_uid, limit))]


def file_stem(req):
    return f"jobhub-invoice-request-{req['date']}-stop-{req['stop_number']}-{report._slug(req['customer']['business_name'])}"


def subject(req, version):
    return (f"JobHub Invoice Request {req['date']} – {req['customer']['business_name'] or 'Stop ' + str(req['stop_number'])}"
            + (f" (update {version})" if version > 1 else ""))


def body(req, version, attached, sig=""):
    lab = req["labor"]
    money = lambda v: "" if v is None else f" ${v:.2f}"
    lines = ["JobHub Invoice Request: please invoice this Time & Material visit. Details are in the attached JSON.",
             "Labor is documented as time on site; apply the company's labor rates and rules.", "",
             f"Technician: {mailer.tech_text(req['technician'])}",
             f"Customer: {req['customer']['business_name']}",
             f"Address: {req['customer']['address']}",
             f"HubSpot company ID: {req['customer']['hubspot_company_id'] or '(not set)'}",
             f"HubSpot ticket: {req['hubspot_ticket'] or '(not given: ' + req['ticket_action'] + ')'}",
             f"QuickBooks estimate: {req['qbo_estimate'] or '(none)'}",
             f"Date: {req['date']}",
             f"Labor: {lab['arrival']} to {lab['billed_through']}, {lab['time_on_site_minutes']} min"
             + (" (still on site when sent)" if lab["still_on_site"] else ""),
             f"Visit result: {req['visit_result'] or '(not set)'}",
             f"Version: {version}" + (" (replaces the earlier request for this stop)" if version > 1 else ""),
             f"Stop ID: {req['stop_uid']}"]
    if req["parts"]:
        lines.append("Parts:")
        lines += [f"  {p['quantity']:g} {p['unit']} x {p['name']}" + (f" ({p['part_number']})" if p["part_number"] else "")
                  + money(p["unit_cost"]) + (" each" if p["unit_cost"] is not None else "") for p in req["parts"]]
    if req["expenses"]:
        lines.append("Billable expenses:")
        lines += [f"  {e['name']}" + (f" ({e['notes']})" if e.get("notes") else "") + money(e["line_cost"])
                  for e in req["expenses"]]
    lines += ["", "Work performed:", req["work_performed"] or "(none)", "", "Attached: " + "; ".join(attached)]
    if sig:
        lines.append(f"Signature: hmac-sha256={sig} (of the attached JSON file)")
    return "\n".join(lines) + "\n"


def send(conn, cfg, stop_id, opts, render_pdf, load_photo, now=None):
    """Email the invoice request. opts {pdf, photos}. Returns the history entry. Raises EditError / mailer.MailError."""
    if not cfg.mail_to:
        raise EditError("Email is not set up: add the address to send to in Settings → Email.")
    req, _, photos = build(conn, cfg, stop_id, now)
    version = 1 + conn.execute("SELECT COUNT(*) FROM invoice_requests WHERE stop_uid=? AND ok=1",
                               (req["stop_uid"],)).fetchone()[0]
    req["request_version"] = version
    req["sent_at"] = datetime.fromtimestamp(int(now or time.time()), ZoneInfo(cfg.app_tz)).isoformat(timespec="minutes")
    stem = file_stem(req)
    data = json.dumps(req, indent=2, ensure_ascii=False).encode()
    sig = mailer.signature(cfg.report_signing_key, data) if cfg.report_signing_key else ""
    files, attached, total = [(f"{stem}.json", "application", "json", data)], [f"{stem}.json (the data)"], len(data)
    if opts.get("pdf"):
        pdf_bytes = render_pdf(req, photos if opts.get("photos") else [])     # pictures only when ticked
        files.append((f"{stem}.pdf", "application", "pdf", pdf_bytes)); attached.append(f"{stem}.pdf")
        total += len(pdf_bytes)
    if opts.get("photos"):
        n = 0
        for p in photos:
            img = load_photo(p["id"])
            if img and total + len(img) <= mailer.MAX_ATTACH_BYTES:
                files.append((p["attachment"], "image", "jpeg", img)); total += len(img); n += 1
        if n:
            attached.append(f"{n} receipt/photo picture{'s' if n != 1 else ''}")
    subj = subject(req, version)
    msg = mailer._message(cfg, subj, body(req, version, attached, sig))
    for fname, main, sub, d in files:
        msg.add_attachment(d, maintype=main, subtype=sub, filename=fname)
    ok, err = True, None
    try:
        mailer.send(cfg, msg)
    except mailer.MailError as e:
        ok, err = False, str(e)
    conn.execute("INSERT INTO invoice_requests(ts,date,stop_id,stop_uid,version,billed_through,minutes,mail_to,ok,error) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?)", (int(time.time()), req["date"], stop_id, req["stop_uid"],
                                                 version if ok else None, req["labor"]["billed_through"],
                                                 req["labor"]["time_on_site_minutes"], cfg.mail_to, 1 if ok else 0, err))
    conn.commit()
    if not ok:
        log.warning("invoice request not sent: %s", err)
        raise mailer.MailError(err)
    log.info("invoice request emailed: %s, %d min%s", req["date"], req["labor"]["time_on_site_minutes"],
             ", signed" if sig else "")
    return {"subject": subj, "version": version, "mail_to": cfg.mail_to}
