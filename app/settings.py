"""User-editable settings stored in SQLite, layered over the env-var defaults.

Env vars stay the baseline (Docker config). A value saved on the Settings page
overrides it; clearing the field reverts to the env value. Infrastructure and
credentials for the Recorder, the timezone and APP_PASSWORD stay env-only. The Recorder
address, user and device can be picked on the Settings page.
"""
import secrets
from dataclasses import replace

SECRETS = ("recorder_auth", "google_places_key", "pushover_token", "pushover_user", "calendar_url", "receipt_key", "anthropic_key", "smtp_password", "report_signing_key")
DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def parse_owners(text):
    """'Me\nOffice = office@x.com' -> [("Me", ""), ("Office", "office@x.com")]. Me may have no email (it uses the
    technician email); everyone else needs one. Labels are unique (case-insensitive); 20 at most."""
    import re
    out, seen = [], set()
    for line in (text or "").replace("\r", "").split("\n"):
        line = line.strip()
        if not line:
            continue
        label, _, email = line.partition("=")
        label, email = label.strip()[:40], email.strip()
        if not label:
            raise ValueError(f"“{line}”: give a name before the =")
        if label.lower() in seen:
            raise ValueError(f"“{label}” is listed twice")
        if email and not re.fullmatch(r"[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+", email):
            raise ValueError(f"“{label}”: “{email}” is not an email address")
        if not email and label.lower() != "me":
            raise ValueError(f"“{label}” needs an email, like {label} = name@example.com")
        seen.add(label.lower())
        out.append((label, email))
    if not out:
        raise ValueError("add at least one owner (Me)")
    return out[:20]


def owner_emails(cfg):
    """{label lower: (label, email)} from the Follow-up owners setting; Me falls back to the technician email."""
    try:
        owners = parse_owners(cfg.followup_owners)
    except ValueError:
        owners = [("Me", "")]
    if not any(k.lower() == "me" for k, _ in owners):
        owners.insert(0, ("Me", ""))
    return {k.lower(): (k, v or (cfg.tech_email if k.lower() == "me" else "")) for k, v in owners}


def parse_days(text):
    """'Mon-Fri', 'Mon,Wed,Fri' or 'Fri-Mon' -> set of weekday numbers (Mon=0)."""
    def one(t):
        if t[:3] not in DAY_NAMES:
            raise ValueError(f"Unknown day “{t}”. Use names like Mon-Fri or Mon,Wed.")
        return DAY_NAMES.index(t[:3])

    out = set()
    for tok in text.replace(" ", "").lower().split(","):
        if not tok:
            continue
        if "-" in tok:
            a, b = (one(x) for x in tok.split("-", 1))
            while True:
                out.add(a)
                if a == b:
                    break
                a = (a + 1) % 7
        else:
            out.add(one(tok))
    if not out:
        raise ValueError("Pick at least one day, for example Mon-Fri.")
    return out

# group, key, label, kind, (min, max) or choices, help
SPEC = [
    ("Recorder", [
        dict(key="recorder_url", label="Recorder address", kind="url",
             help="Your OwnTracks Recorder, e.g. http://192.168.1.50:8083"),
        dict(key="recorder_auth", label="Recorder login (only if it needs one)", kind="secret",
             help="Basic auth as user:password. Write-only: it is never shown again after saving."),
        dict(key="recorder_user", label="Recorder user", kind="text",
             help="Use “Find on Recorder” below to pick it."),
        dict(key="recorder_device", label="Recorder device", kind="text",
             help="Use “Find on Recorder” below to pick it."),
        dict(key="auto_sync_min", label="Auto-sync every (min)", kind="int", lo=0, hi=60,
             help="While today's page is open on screen, new points are fetched this often and new stops appear by "
                  "themselves. 0 turns it off (use Sync new data instead)."),
    ]),
    ("Stop detection", [
        dict(key="stop_min_minutes", label="Minimum stop length (min)", kind="int", lo=1, hi=240,
             help="Shorter visits show as dismissible short stops."),
        dict(key="stop_short_min_s", label="Shortest stop to show (seconds)", kind="int", lo=30, hi=1800,
             help="Pauses shorter than this (traffic lights, stop signs) are ignored; "
                  "between this and the minimum stop length they show as dismissible short stops."),
        dict(key="stop_radius_m", label="Stop radius (m)", kind="int", lo=10, hi=1000,
             help="Points within this distance of the stop centre count as the same place."),
        dict(key="stop_max_gap_min", label="Max gap inside a stop (min)", kind="int", lo=1, hi=120,
             help="Phone asleep / no signal tolerated inside one stop."),
        dict(key="stop_merge_gap_min", label="Merge gap (min)", kind="int", lo=0, hi=240,
             help="Adjacent stops at the same spot closer in time than this are merged."),
        dict(key="max_acc_m", label="Drop points worse than (m accuracy)", kind="int", lo=10, hi=1000,
             help="GPS fixes with a larger error are ignored."),
    ]),
    ("Road distance (OSRM)", [
        dict(key="routing_url", label="Road distance service (OSRM)", kind="url",
             help="Used only to document the miles from where the day ended to Home. Coordinates are rounded to about "
                  "100 m before they are sent. Blank turns it off (a straight-line estimate is used)."),
    ]),
    ("Calendar (Outlook)", [
        dict(key="calendar_url", label="Published calendar link (ICS)", kind="secret",
             help="Outlook on the web: Settings → Calendar → Shared calendars → Publish a calendar → pick the calendar, "
                  "“Can view all details” → Publish → copy the ICS link. Anyone with the link can read the calendar, so "
                  "it is write-only here. Blank = off."),
        dict(key="calendar_keyword", label="Word that marks an on-site appointment", kind="text",
             help="Only events with this word in the title, location, notes or category show up (on-site and on site "
                  "match too). Default onsite."),
    ]),
    ("Receipts", [
        dict(key="receipt_reader", label="Read receipts with", kind="choice", choices=["off", "google_vision", "claude"],
             help="google_vision: Google Cloud Vision reads the total (and gallons and price on a fuel receipt); free for "
                  "the first 1000 a month. claude: Claude (the key in Settings → AI) also reads the store, date, tax and "
                  "each line item, and handles faded receipts better; about 2-3 cents each. Either way the photo has no "
                  "GPS and you always confirm before anything is filled in."),
        dict(key="receipt_key", label="Google key for receipts (optional)", kind="secret",
             help="Blank = use the Google Places key. Its Google Cloud project needs “Cloud Vision API” enabled."),
        dict(key="receipt_monthly_cap", label="Most receipts to read per month", kind="int", lo=0, hi=5000,
             help="A spending safeguard. Google's free tier is 1000 a month."),
    ]),
    ("AI (Claude)", [
        dict(key="anthropic_key", label="Anthropic API key", kind="secret",
             help="From console.anthropic.com → API keys. Set a monthly spend limit there too. Only the Work performed "
                  "text is sent, when you tap Polish notes; nothing else."),
        dict(key="ai_notes", label="Polish notes button", kind="choice", choices=["on", "off"],
             help="Shows ✨ Polish notes under Work performed (only with a key). About 1-2 cents per use."),
        dict(key="ai_monthly_cap", label="Most AI requests per month", kind="int", lo=0, hi=5000,
             help="A spending safeguard on top of the Anthropic account's own limit."),
    ]),
    ("Email (Gmail)", [
        dict(key="mail_to", label="Send reports to", kind="email",
             help="The address your work AI agent reads (your work email). Blank turns Email report off."),
        dict(key="smtp_user", label="Gmail address that sends", kind="email",
             help="The Gmail account the reports are sent from."),
        dict(key="smtp_password", label="Gmail App password", kind="secret",
             help="Not your normal password: Google Account → Security → 2-Step Verification (must be on) → App "
                  "passwords → create one named JobHub, and paste the 16 letters. Write-only."),
        dict(key="mail_from", label="From address (optional)", kind="email",
             help="Blank = the Gmail address above."),
        dict(key="smtp_host", label="Mail server", kind="text", help="smtp.gmail.com for Gmail."),
        dict(key="smtp_port", label="Mail server port", kind="int", lo=1, hi=65535,
             help="587 (STARTTLS) for Gmail; 465 uses SSL."),
        dict(key="report_signing_key", label="Report signing key (optional)", kind="secret",
             help="A long random phrase that only JobHub and your work AI agent know (give the agent the same phrase). "
                  "Each emailed report then has a “Signature:” line: HMAC-SHA256 of the attached JSON file with this key, "
                  "so the agent can reject a report that JobHub did not make. Write-only."),
        dict(key="message_format", label="Message format", kind="choice", choices=["1", "2"],
             help="The JSON layout of every email to the agent (daily summary, Send for invoice, late notice). "
                  "2 is the new shared format the agent asked for (docs/sample-report/README.md). Stay on 1 until the "
                  "agent has checked a real day sent as 2 (Reports → Email report → “Send as format 2 (test)”)."),
        dict(key="late_notice_to", label="Send late notices to (optional)", kind="email",
             help="Where “Tell the customer” (running late) sends the JobHub Late Notice: your work AI agent's "
                  "address. Blank = the “Send reports to” address."),
        dict(key="calendar_invites", label="Put my stops on my work calendar", kind="choice", choices=["off", "on"],
             help="Each stop today is emailed as a calendar invite to the address above, so it shows on your Outlook "
                  "calendar (and to coworkers who can see it): type, customer name, address, ticket, times, finished. "
                  "Never notes or prices. Changes update the same event; a deleted stop is cancelled."),
        dict(key="calendar_invite_min", label="Update the calendar every (min)", kind="int", lo=5, hi=60,
             help="How often stop changes are sent. A stop you are still at is updated about every 15 minutes."),
    ]),
    ("Quick phrases", [
        dict(key="note_phrases", label="Phrases under Work performed", kind="lines",
             help="One per line (20 at most). Tapping one adds it to the notes."),
    ]),
    ("Grammar check (LanguageTool)", [
        dict(key="languagetool_url", label="Grammar check server (LanguageTool)", kind="url",
             help="Your own LanguageTool container, for example http://192.168.1.10:8010. Only the note text is "
                  "sent to it. Blank turns the Check grammar button off."),
    ]),
    ("Business suggestions", [
        dict(key="places_provider", label="Provider", kind="choice", choices=["osm", "osm_google", "google"],
             help="osm is free. osm_google tries your known sites and OSM first and only "
                  "asks Google when they come up short. google needs a key and costs per request."),
        dict(key="places_radius_m", label="Search radius (m)", kind="int", lo=25, hi=1000,
             help="How far around a stop to look for businesses."),
        dict(key="overpass_url", label="Overpass URL (OSM)", kind="url",
             help="Public servers get overloaded. Several addresses can be separated by commas; they are tried in order."),
        dict(key="google_fallback_min", label="Ask Google when OSM finds fewer than", kind="int", lo=0, hi=20,
             help="osm_google mode: number of business-like OSM results below which Google is "
                  "asked (only if no known site is nearby). 0 = only when you press the button."),
        dict(key="google_places_key", label="Google Places API key", kind="secret",
             help="Write-only: it is never shown again after saving."),
        dict(key="google_daily_cap", label="Google daily request cap", kind="int", lo=0, hi=10000,
             help="Hard stop on outgoing Google requests per UTC day. 0 blocks Google."),
    ]),
]
SPEC += [
    ("Notifications (Pushover)", [
        dict(key="pushover_token", label="Pushover application token", kind="secret",
             help="From pushover.net → Your Applications → create an application named JobHub."),
        dict(key="pushover_user", label="Pushover user key", kind="secret",
             help="Shown at the top of your pushover.net dashboard."),
        dict(key="app_url", label="App address for notification links", kind="url",
             help="Tapping a notification opens this, e.g. http://192.168.1.50:5000"),
    ]),
    ("Alerts", [
        dict(key="alert_no_data", label="No-data alert", kind="choice", choices=["on", "off"],
             help="Notify me when no location data arrives during work hours."),
        dict(key="alert_no_data_min", label="Alert after this many minutes without data", kind="int",
             lo=5, hi=1440, help="OwnTracks may send nothing while the phone sits still, so keep this generous."),
        dict(key="alert_drafts", label="End-of-day review reminder", kind="choice", choices=["on", "off"],
             help="Notify me (30 min after the work day ends, or after you tap End day) if stops are still in Draft."),
        dict(key="alert_low_stock", label="Low truck stock alert", kind="choice", choices=["on", "off"],
             help="Notify me when a tracked part drops to its reorder level."),
        dict(key="alert_left_stop", label="Unfinished stop alert", kind="choice", choices=["on", "off"],
             help="Notify me a few minutes after I drive away from a stop that isn't finished (work type, notes, "
                  "visit result...). JobHub then checks the Recorder in the background during the work day."),
        dict(key="alert_late", label="Running late alert", kind="choice", choices=["on", "off"],
             help="Notify me when the drive time says I will be late for an on-site appointment on my calendar "
                  "(Settings → Calendar). The alert never names the customer; tapping it opens JobHub, where "
                  "“Tell the customer” emails your work AI agent a late notice. Nothing is sent without that tap."),
        dict(key="alert_late_min", label="Running late: alert when late by (min)", kind="int", lo=1, hi=120,
             help="How many minutes after the appointment start your arrival has to be."),
        dict(key="work_days", label="Work days", kind="days", help="For example Mon-Fri or Mon,Tue,Thu."),
        dict(key="work_start", label="Work day starts", kind="time", help="24-hour time, local."),
        dict(key="work_end", label="Work day ends", kind="time", help="24-hour time, local."),
        dict(key="alert_repeat_min", label="Repeat reminder every (min)", kind="int", lo=0, hi=1440,
             help="0 = one alert per outage."),
    ]),
]
SPEC += [
    ("Technician", [
        dict(key="tech_name", label="Technician name", kind="text",
             help="Your name as HubSpot knows you. It goes in every report (technician) and the report email."),
        dict(key="tech_email", label="Technician email", kind="email",
             help="Your work email, so the agent can set you as the owner of notes and tasks."),
        dict(key="followup_owners", label="Follow-up owners", kind="owners",
             help="One per line as Name = email, for example “Office = office@work.com”. “Me” is you (the technician "
                  "email above). The stop sheet offers these names, and the report gives the agent each owner's email "
                  "so HubSpot tasks go to the right person."),
    ]),
    ("Pay rules", [
        dict(key="lunch_paid", label="Lunch is paid", kind="choice", choices=["on", "off"],
             help="on: lunch counts in total paid time (never billed to the customer). off: lunch is unpaid and left out "
                  "of paid time. Mileage is the same either way."),
    ]),
    ("Parts import", [
        dict(key="qbo_service_with_sku", label="QuickBooks: import Service items that have a SKU", kind="choice",
             choices=["on", "off"],
             help="When QuickBooks inventory tracking is off, every item is a Service. on: a Service item with a SKU is "
                  "imported as a part. Items without a SKU, and anything called Labor, are always skipped."),
    ]),
    ("Log", [
        dict(key="log_level", label="Log detail", kind="choice", choices=["info", "debug"],
             help="info: requests (path only), errors and events such as syncs, emails and alerts. debug: more detail, "
                  "for tracking down a problem. Coordinates are never written to the log."),
    ]),
    ("Backups", [
        dict(key="backup_keep", label="Daily backups to keep", kind="int", lo=0, hi=365,
             help="One snapshot of the database is saved each day. 0 turns automatic backups off."),
    ]),
]
# Settings page tabs: one per integration, then the app's own settings. All tabs belong to one form.
TABS = [
    ("OwnTracks", ["Recorder"]),
    ("Places", ["Business suggestions"]),
    ("Pushover", ["Notifications (Pushover)", "Alerts"]),
    ("Road distance", ["Road distance (OSRM)"]),
    ("Calendar", ["Calendar (Outlook)"]),
    ("Receipts", ["Receipts"]),
    ("AI", ["AI (Claude)"]),
    ("Email", ["Email (Gmail)"]),
    ("Notes", ["Quick phrases", "Grammar check (LanguageTool)"]),
    ("General", ["Technician", "Pay rules", "Parts import", "Stop detection", "Backups", "Log"]),
]
FIELDS = {f["key"]: f for _, fs in SPEC for f in fs}


def overrides(conn):
    return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}


def _convert(field, raw):
    raw = raw.strip()
    if field["kind"] == "int":
        v = int(raw)
        if not field["lo"] <= v <= field["hi"]:
            raise ValueError(f"must be between {field['lo']} and {field['hi']}")
        return v
    if field["kind"] == "choice":
        if raw not in field["choices"]:
            raise ValueError("choose one of: " + ", ".join(field["choices"]))
        return raw
    if field["kind"] == "url":
        if not raw.startswith(("http://", "https://")):
            raise ValueError("must start with http:// or https://")
        return raw.rstrip("/")
    if field["kind"] == "time":
        try:
            h, m = raw.split(":")
            if not (0 <= int(h) <= 23 and 0 <= int(m) <= 59):
                raise ValueError
        except ValueError:
            raise ValueError("must be a time like 07:30")
        return f"{int(h):02d}:{int(m):02d}"
    if field["kind"] == "lines":
        lines = [l.strip()[:120] for l in raw.replace("\r", "").split("\n") if l.strip()][:20]
        if not lines:
            raise ValueError("add at least one line, or leave it empty for the defaults")
        return "\n".join(lines)
    if field["kind"] == "email":
        import re
        if not re.fullmatch(r"[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+", raw):
            raise ValueError("must be one email address, like name@example.com")
        return raw
    if field["kind"] == "owners":
        owners = parse_owners(raw)
        return "\n".join(f"{k} = {v}" if v else k for k, v in owners)
    if field["kind"] == "days":
        parse_days(raw)
        return raw
    return raw


def effective(base, conn):
    """Config with saved settings applied on top of the env defaults."""
    changes = {}
    ov = overrides(conn)
    for key, field in FIELDS.items():
        if field["kind"] == "secret":
            if ov.get(key):
                changes[key] = ov[key]
        elif key in ov:
            try:
                changes[key] = _convert(field, ov[key])
            except ValueError:
                pass  # ignore a corrupt stored value; fall back to the env default
    return replace(base, **changes)


def save(conn, form):
    """Validate and store a submitted form. Returns {key: error message}."""
    errors, writes, deletes = {}, {}, []
    for key, field in FIELDS.items():
        if field["kind"] == "secret":
            continue
        raw = (form.get(key) or "").strip()
        if not raw:
            deletes.append(key)  # blank = revert to the env default
            continue
        try:
            writes[key] = str(_convert(field, raw))
        except ValueError as e:
            errors[key] = ("must be a whole number"
                           if field["kind"] == "int" and "between" not in str(e) else str(e))
    if errors:
        return errors
    for key in SECRETS:
        new = (form.get(key) or "").strip()
        if form.get("clear_" + key):
            deletes.append(key)
        elif new:
            if key == "calendar_url":
                new = new.replace("webcal://", "https://", 1) if new.lower().startswith("webcal://") else new
                if not new.startswith("https://"):
                    return {key: "must be the https:// (or webcal://) ICS link"}
            if len(new) > (1000 if key == "calendar_url" else 200):
                return {key: "is too long"}
            writes[key] = new
    for k in deletes:
        conn.execute("DELETE FROM settings WHERE key=?", (k,))
    for k, v in writes.items():
        conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)", (k, v))
    conn.commit()
    return {}


def secret_key(conn):
    """Flask session signing key, generated once and kept in the database."""
    row = conn.execute("SELECT value FROM settings WHERE key='_session_secret'").fetchone()
    if row:
        return row["value"]
    val = secrets.token_hex(32)
    conn.execute("INSERT INTO settings(key,value) VALUES ('_session_secret',?)", (val,))
    conn.commit()
    return val


def key_hint(key):
    return ("set (ends …" + key[-4:] + ")") if key else "not set"
