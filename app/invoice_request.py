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
                              "billed_through": r["billed_through"], "time_on_site_minutes": r["minutes"],
                              "message_id": r["message_id"] or "", "sent_ts": r["ts"],
                              "billed_through_ts": r["billed_through_ts"] or _clock_ts(date, r["billed_through"], tz),
                              "invoice_to_email": r["invoice_to_email"] or ""}
    return out


def _clock_ts(date, hhmm, tz):
    """'10:30' on a date -> seconds (requests sent before billed_through_ts was kept)."""
    try:
        return int(datetime.fromisoformat(f"{date}T{hhmm}").replace(tzinfo=ZoneInfo(tz)).timestamp())
    except (TypeError, ValueError):
        return None


def _prepare(conn, cfg, stop_id, now):
    """Checks and the numbers shared by both formats: (row, payload stop, report stop with _src, still_here, through,
    minutes, photos of the stop as report photo dicts)."""
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
    day = report.build_day(payload, cfg, keep_src=True)
    s = next((x for x in day["stops"] if x["stop_uid"] == ps["uid"]), None)
    if s is None:
        raise EditError("This stop is not in the report (is it Confirmed?).")
    today = datetime.fromtimestamp(now, tz).date().isoformat()
    # "Here now" (the newest GPS point is fresh and inside this stop): bill through the moment of sending. Otherwise the
    # departure (typed, Leaving now, or the last point here), so a drive away is never billed as labor.
    still_here = row["date"] == today and bool(ps.get("here_now")) and not ps.get("depart_pinned") and now > ps["depart_ts"]
    through = now if still_here else ps["depart_ts"]
    minutes = s["time_on_site_minutes"] + (max(0, round((through - ps["depart_ts"]) / 60)) if still_here else 0)
    photos = [p for p in day.get("photos") or [] if p["id"] in set(s.get("photos") or [])]
    return row, ps, s, still_here, through, minutes, photos, payload


def build(conn, cfg, stop_id, now=None, info=None):
    """(request dict, warnings, photos) in format 1. Raises EditError when the stop cannot be invoiced from here.
    info (a dict) gets `through_ts`: the exact moment labor is billed through."""
    now = int(now or time.time())
    tz = ZoneInfo(cfg.app_tz)
    row, ps, s, still_here, through, minutes, photos, _ = _prepare(conn, cfg, stop_id, now)
    if info is not None:
        info["through_ts"] = through
    photos = report._public(photos)
    clock = lambda ts: datetime.fromtimestamp(ts, tz).strftime("%H:%M")
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
        warn.append("Your name or work email is not set (Settings → Me).")
    return req, warn, photos


def build_v2(conn, cfg, stop_id, now=None, invoice_to_email="", revision=1, supersedes=None, message_id=None):
    """(message, photos as report photo dicts, [(attachment, photo id)]) in format 2. While still on site the Stop's
    departure and minutes run to the moment of sending (the same as `billed_through`)."""
    from . import message_v2 as v2
    now = int(now or time.time())
    row, ps, s, still_here, through, minutes, photos, payload = _prepare(conn, cfg, stop_id, now)
    sites = v2.sites_by_id(conn)
    from_uid = v2._from_uids(payload)
    st = v2.stop(s, s["_src"], row["date"], cfg.app_tz, sites, from_uid.get(s["stop_uid"]),
                 getattr(cfg, "lunch_paid", "on") != "off", ())
    if still_here:
        st["departure"], st["time_on_site_minutes"] = v2.iso(through, cfg.app_tz), minutes
    pics = [v2.photo(p, s["stop_uid"], cfg.app_tz) for p in photos]
    # lines on the previous request for this stop that are gone now (removed or changed to another stop)
    prev = v2.last_sent_uids(conn, "invoice_requests", "stop_uid=?", (s["stop_uid"],))
    now_lines = {ln["line_uid"] for ln in st["lines"]}
    msg = {**v2.envelope(cfg, "invoice_request", s["stop_uid"], revision, supersedes, now, (), message_id),
           "stop": st, "billed_through": v2.iso(through, cfg.app_tz), "still_on_site": bool(still_here),
           "invoice_to_email": (invoice_to_email or "").strip(), "photos": pics,
           "removed_line_uids": [x for x in prev.get("lines", []) if x not in now_lines]}
    return msg, report._public(photos), [(pic["attachment"], p["id"]) for pic, p in zip(pics, photos)]


def billing_email_for(conn, stop_id):
    """The billing email saved on the stop's place (filled in on the Send for invoice preview), or ''."""
    r = conn.execute("SELECT k.billing_email FROM stops s JOIN known_sites k ON k.id=s.known_site_id WHERE s.id=?",
                     (stop_id,)).fetchone()
    return (r["billing_email"] or "") if r else ""


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


def v2_lines(revision, message_id, replaces):
    """The same three lines in every format 2 email body."""
    return ["Message format: 2", f"Revision: {revision}" + (f" ({replaces})" if revision > 1 else ""),
            f"Message ID: {message_id}"]


def body(req, version, attached, sig="", message_id=""):
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
             f"Customer billing email: {req.get('invoice_to_email') or '(not given)'}",
             *(v2_lines(version, message_id, "replaces the earlier request for this stop") if message_id else
               [f"Version: {version}" + (" (replaces the earlier request for this stop)" if version > 1 else "")]),
             f"Stop ID: {req['stop_uid']}"]
    if req["parts"]:
        lines.append("Parts (JobHub prices; the invoice uses QuickBooks prices):")
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
    """Email the invoice request. opts {pdf, photos, invoice_to_email}. Returns the history entry. Raises EditError /
    mailer.MailError. The JSON is in the Message format setting (1 or 2); the PDF and the body are the same in both."""
    from . import message_v2 as v2
    from .places import known
    if not cfg.mail_to:
        raise EditError("Email is not set up: add the address to send to in Settings → Work AI agent.")
    now = int(now or time.time())
    bill_to = known.billing_email(opts.get("invoice_to_email")) or ""
    info = {}
    req, _, photos = build(conn, cfg, stop_id, now, info)
    version, supersedes = v2.previous(conn, "invoice_requests", "stop_uid=?", (req["stop_uid"],))
    mid = v2.new_id()
    req["request_version"] = version
    req["sent_at"] = datetime.fromtimestamp(now, ZoneInfo(cfg.app_tz)).isoformat(timespec="minutes")
    req["invoice_to_email"] = bill_to
    fmt2 = cfg.message_format == "2"
    if fmt2:
        msg2, photos, pic_files = build_v2(conn, cfg, stop_id, now, bill_to, version, supersedes, mid)
        stem = v2.file_stem("invoice_request", req["date"],
                            f"stop-{req['stop_number']}-{report._slug(req['customer']['business_name'])}", version)
        data = json.dumps(msg2, indent=2, ensure_ascii=False).encode()
        names = dict((pid, name) for name, pid in pic_files)
        subj = v2.subject("invoice_request", req["date"], req["customer"]["business_name"] or f"Stop {req['stop_number']}",
                          version)
    else:
        stem = file_stem(req)
        data = json.dumps(req, indent=2, ensure_ascii=False).encode()
        names = {p["id"]: p["attachment"] for p in photos}
        subj = subject(req, version)
    files, attached, total = [], [f"{stem}.json (the data)"], len(data)
    if opts.get("pdf"):
        pdf_bytes = render_pdf(req, photos if opts.get("photos") else [])     # pictures only when ticked
        files.append((f"{stem}.pdf", "application", "pdf", pdf_bytes)); attached.append(f"{stem}.pdf")
        total += len(pdf_bytes)
    sent_names = set()
    # the receipts always go when an expense is billed (the invoice needs them); other pictures only when ticked
    receipts_needed = bool(req["expenses"])
    chosen = [p for p in photos if opts.get("photos") or (receipts_needed and p["kind"] == "receipt")]
    if chosen:
        for p in chosen:
            img = load_photo(p["id"])
            fname = names.get(p["id"]) or p["attachment"]
            if img and total + len(img) <= mailer.MAX_ATTACH_BYTES:
                files.append((fname, "image", "jpeg", img)); total += len(img); sent_names.add(fname)
        if sent_names:
            attached.append(f"{len(sent_names)} receipt/photo picture{'s' if len(sent_names) != 1 else ''}"
                            + ("" if opts.get("photos") else " (receipts for the billed expenses)"))
    if fmt2:                          # format 2 says, per photo, whether its picture is attached to this email
        for p in msg2["photos"]:
            p["attached"] = p["attachment"] in sent_names
        data = json.dumps(msg2, indent=2, ensure_ascii=False).encode()
    sig = mailer.signature(cfg.report_signing_key, data) if cfg.report_signing_key else ""
    files.insert(0, (f"{stem}.json", "application", "json", data))
    msg = mailer._message(cfg, subj, body(req, version, attached, sig, mid if fmt2 else ""))
    for fname, main, sub, d in files:
        msg.add_attachment(d, maintype=main, subtype=sub, filename=fname)
    ok, err = True, None
    try:
        mailer.send(cfg, msg)
    except mailer.MailError as e:
        ok, err = False, str(e)
    through_ts = info["through_ts"]
    uids = json.dumps({"lines": [ln["line_uid"] for ln in msg2["stop"]["lines"]]}) if fmt2 else None
    conn.execute("INSERT INTO invoice_requests(ts,date,stop_id,stop_uid,version,billed_through,minutes,mail_to,ok,error,"
                 "message_id,billed_through_ts,invoice_to_email,sent_uids) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (now, req["date"], stop_id, req["stop_uid"], version if ok else None, req["labor"]["billed_through"],
                  req["labor"]["time_on_site_minutes"], cfg.mail_to, 1 if ok else 0, err, mid if ok else None, through_ts,
                  bill_to or None, uids))
    if ok and bill_to:          # remembered on the place: filled in next time (the user agreed 2026-11-24)
        conn.execute("UPDATE known_sites SET billing_email=? WHERE id=(SELECT known_site_id FROM stops WHERE id=?)",
                     (bill_to, stop_id))
    conn.commit()
    if not ok:
        log.warning("invoice request not sent: %s", err)
        raise mailer.MailError(err)
    log.info("invoice request emailed: %s, %d min, format %s%s", req["date"], req["labor"]["time_on_site_minutes"],
             "2" if fmt2 else "1", ", signed" if sig else "")
    return {"subject": subj, "version": version, "mail_to": cfg.mail_to}
