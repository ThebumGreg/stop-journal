"""User-editable settings stored in SQLite, layered over the env-var defaults.

Env vars stay the baseline (Docker config). A value saved on the Settings page
overrides it; clearing the field reverts to the env value. Infrastructure and
credentials for the Recorder, the timezone and APP_PASSWORD stay env-only. The Recorder
address, user and device can be picked on the Settings page.
"""
import secrets
from dataclasses import replace

SECRETS = ("recorder_auth", "google_places_key", "pushover_token", "pushover_user", "calendar_url", "receipt_key")
DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


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
        dict(key="receipt_reader", label="Read receipts with", kind="choice", choices=["off", "google_vision"],
             help="google_vision: each receipt photo is sent to Google Cloud Vision to read the total (and gallons and "
                  "price on a fuel receipt). You always confirm before anything is filled in. The photo has no GPS. "
                  "Free for the first 1000 a month."),
        dict(key="receipt_key", label="Google key for receipts (optional)", kind="secret",
             help="Blank = use the Google Places key. Its Google Cloud project needs “Cloud Vision API” enabled."),
        dict(key="receipt_monthly_cap", label="Most receipts to read per month", kind="int", lo=0, hi=5000,
             help="A spending safeguard. Google's free tier is 1000 a month."),
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
        dict(key="work_days", label="Work days", kind="days", help="For example Mon-Fri or Mon,Tue,Thu."),
        dict(key="work_start", label="Work day starts", kind="time", help="24-hour time, local."),
        dict(key="work_end", label="Work day ends", kind="time", help="24-hour time, local."),
        dict(key="alert_repeat_min", label="Repeat reminder every (min)", kind="int", lo=0, hi=1440,
             help="0 = one alert per outage."),
    ]),
]
SPEC += [
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
    ("Grammar", ["Grammar check (LanguageTool)"]),
    ("General", ["Stop detection", "Backups"]),
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
