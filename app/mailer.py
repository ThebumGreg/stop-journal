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
- Attachments: the JSON (the data, schema version 1), the PDF, and optionally the pictures (named as in the report).
"""
import json
import smtplib
import ssl
import time
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

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


def body_for(rep, version, attached, skipped_photos=0):
    a, b = rep["date_from"], rep["date_to"]
    stops = [(d, s) for d in rep["days"] for s in d["stops"]]
    lines = [
        "JobHub Daily Summary",
        "Dates: " + (a if a == b else f"{a} to {b}"),
        "Technician: " + tech_text(rep.get("technician")),
        f"Version: {version}" + (" (replaces the earlier email for these dates)" if version > 1 else ""),
        f"Confirmed stops: {len(stops)}",
        "Report schema version: " + str(rep.get("schema_version", "1")),
        "Attached: " + ("; ".join(attached) if attached else "nothing (the summary below only)"),
    ]
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
    lines += ["", "Sent by JobHub after the technician checked the day. The JSON attachment holds every field; "
                  "the PDF is the same report for people."]
    return "\n".join(lines) + "\n"


def send_report(conn, cfg, rep, name, pdf_bytes, opts, load_photo):
    """Build and send the report email. opts: {pdf, json, photos}. Returns the log entry dict. Raises MailError."""
    if not cfg.mail_to:
        raise MailError("Add the address to send to in Settings → Email.")
    if not (opts.get("pdf") or opts.get("json")):
        raise MailError("Tick the PDF or the JSON (or both) to attach.")
    a, b = rep["date_from"], rep["date_to"]
    version = 1 + conn.execute("SELECT COUNT(*) FROM email_log WHERE ok=1 AND date_from=? AND date_to=?",
                               (a, b)).fetchone()[0]
    files, attached, total, skipped = [], [], 0, 0
    if opts.get("json"):
        data = json.dumps(rep, indent=2, ensure_ascii=False).encode()
        files.append((f"{name}.json", "application", "json", data)); attached.append(f"{name}.json (the data)")
        total += len(data)
    if opts.get("pdf"):
        files.append((f"{name}.pdf", "application", "pdf", pdf_bytes)); attached.append(f"{name}.pdf")
        total += len(pdf_bytes)
    n_photos = 0
    if opts.get("photos"):
        for day in rep["days"]:
            for p in day.get("photos") or []:
                img = load_photo(p["id"])
                if not img:
                    continue
                if total + len(img) > MAX_ATTACH_BYTES:
                    skipped += 1
                    continue
                files.append((p["file"].replace("/", "_"), "image", "jpeg", img))
                total += len(img); n_photos += 1
        if n_photos:
            attached.append(f"{n_photos} receipt/photo picture{'s' if n_photos != 1 else ''}")
    subject = subject_for(rep, version)
    msg = _message(cfg, subject, body_for(rep, version, attached, skipped))
    for fname, main, sub, data in files:
        msg.add_attachment(data, maintype=main, subtype=sub, filename=fname)
    entry = {"ts": int(time.time()), "date_from": a, "date_to": b, "mail_to": cfg.mail_to, "subject": subject,
             "version": version, "attached": "; ".join(attached)}
    try:
        send(cfg, msg)
    except MailError as e:
        conn.execute("INSERT INTO email_log(ts,date_from,date_to,mail_to,subject,version,ok,error,attached) "
                     "VALUES (?,?,?,?,?,?,0,?,?)", (entry["ts"], a, b, cfg.mail_to, subject, version, str(e), entry["attached"]))
        conn.commit()
        raise
    conn.execute("INSERT INTO email_log(ts,date_from,date_to,mail_to,subject,version,ok,error,attached) "
                 "VALUES (?,?,?,?,?,?,1,NULL,?)", (entry["ts"], a, b, cfg.mail_to, subject, version, entry["attached"]))
    conn.commit()
    return {**entry, "skipped_photos": skipped}


def history(conn, date_from, date_to, limit=10):
    return [dict(r) for r in conn.execute(
        "SELECT ts, mail_to, subject, version, ok, error, attached FROM email_log WHERE date_from=? AND date_to=? "
        "ORDER BY id DESC LIMIT ?", (date_from, date_to, limit))]
