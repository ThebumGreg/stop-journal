"""Email report: send the day's report to an address (the user's work email, read by their work AI agent).

Only when you tap **Email report** on the Reports page, after checking the day; never automatically. Sent through
your own mail account over SMTP (Gmail: smtp.gmail.com:587 STARTTLS with an App password). Python's smtplib, no
extra package.

What leaves the server: the report itself (customer names, addresses, notes, parts, and GPS coordinates unless the
"GPS coordinates" section is unticked) and, if ticked, the receipts and photos. Each send is logged in `email_log`.

The message is made easy for an agent to pick up:
- Subject `JobHub Daily Summary 2026-11-23` (a range: `... 2026-11-19 to 2026-11-23`); a second send of the same
  dates adds ` (update 2)`, and the body says it replaces the earlier one.
- The body starts with fixed `Key: value` lines (Dates, Version, Confirmed stops, Attached), then a short stop list.
- Attachments: the JSON (the data: format 1, or format 2 per Settings → Work AI agent → Message format), the PDF, and
  optionally the pictures (named as in the report).
"""
import json
import logging
import smtplib
import ssl
import time
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from .report import attachment_name

log = logging.getLogger("jobhub.email")
MAX_ATTACH_BYTES = 17 * 1024 * 1024      # Gmail allows 25 MB per message; base64 adds about a third
TIMEOUT_S = 30


class MailError(Exception):
    pass


def enabled(cfg):
    return bool(cfg.mail_to and cfg.smtp_user and cfg.smtp_password)


def _password(cfg):
    pw = cfg.smtp_password
    return pw.replace(" ", "") if "gmail" in (cfg.smtp_host or "").lower() else pw   # "abcd efgh ijkl mnop"


def send(cfg, msg):
    """Send one EmailMessage. Raises MailError with a plain message."""
    if not (cfg.smtp_user and cfg.smtp_password):
        raise MailError("Email is not set up: add the Gmail address and App password in Settings → Email.")
    ctx = ssl.create_default_context()
    try:
        if cfg.smtp_port == 465:
            server = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=TIMEOUT_S, context=ctx)
        else:
            server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=TIMEOUT_S)
        with server:
            if cfg.smtp_port != 465:
                server.starttls(context=ctx)
            server.login(cfg.smtp_user, _password(cfg))
            server.send_message(msg)
    except smtplib.SMTPAuthenticationError:
        raise MailError("The mail server refused the login. For Gmail use an App password (Google Account → Security → "
                        "2-Step Verification → App passwords), not your normal password.")
    except smtplib.SMTPRecipientsRefused:
        raise MailError("The mail server refused the address " + cfg.mail_to + ".")
    except smtplib.SMTPException as e:
        raise MailError(f"The mail server said no ({type(e).__name__}: {str(e)[:150]}).")
    except (OSError, TimeoutError) as e:
        raise MailError(f"Could not reach {cfg.smtp_host}:{cfg.smtp_port} ({type(e).__name__}).")


def _message(cfg, subject, body):
    msg = EmailMessage()
    msg["From"] = cfg.mail_from or cfg.smtp_user
    msg["To"] = cfg.mail_to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="jobhub.local")
    msg.set_content(body)
    return msg


def test(cfg):
    msg = _message(cfg, "JobHub test email", "This is a test from JobHub (Settings → Email). Reports will look like:\n"
                   "Subject: JobHub Daily Summary 2026-11-23\nAttached: the JSON report and the PDF.\n")
    send(cfg, msg)


def subject_for(rep, version):
    a, b = rep["date_from"], rep["date_to"]
    s = f"JobHub Daily Summary {a}" + ("" if a == b else f" to {b}")
    return s + (f" (update {version})" if version > 1 else "")


def tech_text(t):
    """{name, email} -> 'Jane Smith <jane@work.com>' (or whichever is set), '(not set)' when neither."""
    t = t or {}
    name, email = (t.get("name") or "").strip(), (t.get("email") or "").strip()
    return f"{name} <{email}>" if name and email else name or email or "(not set)"


def signature(key, data):
    """HMAC-SHA256 (hex) of the exact bytes of the attached JSON file. The agent recomputes it over the attachment as
    received with the same key; no JSON re-formatting is involved."""
    import hashlib
    import hmac
    return hmac.new(key.encode(), data, hashlib.sha256).hexdigest()


def body_for(rep, version, attached, skipped_photos=0, sig="", message_id=""):
    a, b = rep["date_from"], rep["date_to"]
    stops = [(d, s) for d in rep["days"] for s in d["stops"]]
    replaces = "replaces the earlier email for these dates"
    lines = [
        "JobHub Daily Summary",
        "Dates: " + (a if a == b else f"{a} to {b}"),
        "Technician: " + tech_text(rep.get("technician")),
        *(["Message format: 2", f"Revision: {version}" + (f" ({replaces})" if version > 1 else ""),
           f"Message ID: {message_id}"] if message_id else
          [f"Version: {version}" + (f" ({replaces})" if version > 1 else ""),
           "Report schema version: " + str(rep.get("schema_version", "1"))]),
        f"Confirmed stops: {len(stops)}",
        "Attached: " + ("; ".join(attached) if attached else "nothing (the summary below only)"),
    ]
    if sig:
        lines.append(f"Signature: hmac-sha256={sig} (of the attached JSON file)")
    if rep.get("omitted"):
        lines.append("Left out on purpose: " + ", ".join(o["label"] for o in rep["omitted"]))
    if skipped_photos:
        lines.append(f"Photos not attached (too large for one email): {skipped_photos}")
    lines += ["", "Stops:"]
    for d, s in stops:
        lines.append(f"- {d['date']} {s['arrival']}-{s['departure']} {s['business_name'] or '(no name)'}"
                     + (f", {s['address']}" if s.get("address") else ""))
    if not stops:
        lines.append("- none confirmed")
    has_json = any(x.endswith(".json (the data)") for x in attached)
    has_pdf = any(x.endswith(".pdf") for x in attached)
    lines += ["", "Sent by JobHub after the technician checked the day."
              + (" The JSON attachment holds every field." if has_json else "")
              + (" The PDF is the same report for people." if has_pdf else "")]
    return "\n".join(lines) + "\n"


def send_report(conn, cfg, rep, name, pdf_bytes, opts, load_photo, build_v2=None):
    """Build and send the report email. opts: {pdf, json, photos}. Returns the log entry dict. Raises MailError.
    rep is the format-1 report (the PDF and the body come from it). build_v2(revision, supersedes, message_id) ->
    (message, [(attachment, photo id)]) makes the JSON format 2 instead (app/message_v2.py)."""
    from . import message_v2
    if not cfg.mail_to:
        raise MailError("Add the address to send to in Settings → Work AI agent.")
    if not (opts.get("pdf") or opts.get("json")):
        raise MailError("Tick the PDF or the JSON (or both) to attach.")
    a, b = rep["date_from"], rep["date_to"]
    version, supersedes = message_v2.daily_revision(conn, a, b)
    mid = message_v2.new_id()
    pics = [(p.get("attachment") or attachment_name(p["file"]), p["id"]) for d in rep["days"] for p in d.get("photos") or []]
    data_obj = rep
    if build_v2:
        data_obj, pics = build_v2(version, supersedes, mid)
        name = message_v2.daily_stem(a, b, version)
    files, attached, skipped = [], [], 0
    sig = ""
    total = len(json.dumps(data_obj, ensure_ascii=False).encode()) if opts.get("json") else 0
    if opts.get("pdf"):
        total += len(pdf_bytes)
    images, sent_names = [], set()
    if opts.get("photos"):
        for fname, pid in pics:
            img = load_photo(pid)
            if not img:
                continue
            if total + len(img) > MAX_ATTACH_BYTES:
                skipped += 1
                continue
            images.append((fname, "image", "jpeg", img)); sent_names.add(fname)
            total += len(img)
    if build_v2:                      # format 2 says, per photo, whether its picture is attached to this email
        for d in data_obj["days"]:
            for p in d["photos"]:
                p["attached"] = p["attachment"] in sent_names
    if opts.get("json"):
        data = json.dumps(data_obj, indent=2, ensure_ascii=False).encode()
        if cfg.report_signing_key:
            sig = signature(cfg.report_signing_key, data)
        files.append((f"{name}.json", "application", "json", data)); attached.append(f"{name}.json (the data)")
    if opts.get("pdf"):
        files.append((f"{name}.pdf", "application", "pdf", pdf_bytes)); attached.append(f"{name}.pdf")
    files += images
    if images:
        attached.append(f"{len(images)} receipt/photo picture{'s' if len(images) != 1 else ''}")
    subject = subject_for(rep, version)
    msg = _message(cfg, subject, body_for(rep, version, attached, skipped, sig, mid if build_v2 else ""))
    for fname, main, sub, data in files:
        msg.add_attachment(data, maintype=main, subtype=sub, filename=fname)
    entry = {"ts": int(time.time()), "date_from": a, "date_to": b, "mail_to": cfg.mail_to, "subject": subject,
             "version": version, "attached": "; ".join(attached), "format": "2" if build_v2 else "1"}
    try:
        send(cfg, msg)
    except MailError as e:
        log.warning("report email failed: %s: %s", subject, e)
        conn.execute("INSERT INTO email_log(ts,date_from,date_to,mail_to,subject,version,ok,error,attached) "
                     "VALUES (?,?,?,?,?,?,0,?,?)", (entry["ts"], a, b, cfg.mail_to, subject, version, str(e), entry["attached"]))
        conn.commit()
        raise
    uids = json.dumps(message_v2.sent_uids(data_obj["days"])) if build_v2 else None    # for the next revision's removed lists
    conn.execute("INSERT INTO email_log(ts,date_from,date_to,mail_to,subject,version,ok,error,attached,message_id,schema,"
                 "sent_uids) VALUES (?,?,?,?,?,?,1,NULL,?,?,?,?)", (entry["ts"], a, b, cfg.mail_to, subject, version,
                                                                   entry["attached"], mid, entry["format"], uids))
    conn.commit()
    log.info("report emailed: %s (%s), format %s%s", subject, entry["attached"], entry["format"], ", signed" if sig else "")
    return {**entry, "skipped_photos": skipped}


def history(conn, date_from, date_to, limit=10):
    return [dict(r) for r in conn.execute(
        "SELECT ts, mail_to, subject, version, ok, error, attached, schema AS format FROM email_log WHERE date_from=? AND date_to=? "
        "ORDER BY id DESC LIMIT ?", (date_from, date_to, limit))]
