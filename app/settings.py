"""User-editable settings stored in SQLite, layered over the env-var defaults.

Env vars stay the baseline (Docker config). A value saved on the Settings page
overrides it; clearing the field reverts to the env value. Infrastructure and
credentials for the Recorder, the timezone and APP_PASSWORD stay env-only.
"""
import secrets
from dataclasses import replace

SECRET = "google_places_key"

# group, key, label, kind, (min, max) or choices, help
SPEC = [
    ("Stop detection", [
        dict(key="stop_min_minutes", label="Minimum stop length (min)", kind="int", lo=1, hi=240,
             help="Shorter visits show as dismissible short stops."),
        dict(key="stop_radius_m", label="Stop radius (m)", kind="int", lo=10, hi=1000,
             help="Points within this distance of the stop centre count as the same place."),
        dict(key="stop_max_gap_min", label="Max gap inside a stop (min)", kind="int", lo=1, hi=120,
             help="Phone asleep / no signal tolerated inside one stop."),
        dict(key="stop_merge_gap_min", label="Merge gap (min)", kind="int", lo=0, hi=240,
             help="Adjacent stops at the same spot closer in time than this are merged."),
        dict(key="max_acc_m", label="Drop points worse than (m accuracy)", kind="int", lo=10, hi=1000,
             help="GPS fixes with a larger error are ignored."),
    ]),
    ("Business suggestions", [
        dict(key="places_provider", label="Provider", kind="choice", choices=["osm", "osm_google", "google"],
             help="osm is free. osm_google tries your known sites and OSM first and only "
                  "asks Google when they come up short. google needs a key and costs per request."),
        dict(key="places_radius_m", label="Search radius (m)", kind="int", lo=25, hi=1000,
             help="How far around a stop to look for businesses."),
        dict(key="overpass_url", label="Overpass URL (OSM)", kind="url",
             help="Public servers get overloaded; point this at a mirror if lookups fail."),
        dict(key="google_fallback_min", label="Ask Google when OSM finds fewer than", kind="int", lo=0, hi=20,
             help="osm_google mode: number of business-like OSM results below which Google is "
                  "asked (only if no known site is nearby). 0 = only when you press the button."),
        dict(key="google_daily_cap", label="Google daily request cap", kind="int", lo=0, hi=10000,
             help="Hard stop on outgoing Google requests per UTC day. 0 blocks Google."),
    ]),
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
        return raw
    return raw


def effective(base, conn):
    """Config with saved settings applied on top of the env defaults."""
    changes = {}
    ov = overrides(conn)
    for key, field in FIELDS.items():
        if key in ov:
            try:
                changes[key] = _convert(field, ov[key])
            except ValueError:
                pass  # ignore a corrupt stored value; fall back to the env default
    if ov.get(SECRET):
        changes[SECRET] = ov[SECRET]
    return replace(base, **changes)


def save(conn, form):
    """Validate and store a submitted form. Returns {key: error message}."""
    errors, writes, deletes = {}, {}, []
    for key, field in FIELDS.items():
        raw = (form.get(key) or "").strip()
        if not raw:
            deletes.append(key)  # blank = revert to the env default
            continue
        try:
            writes[key] = str(_convert(field, raw))
        except ValueError as e:
            errors[key] = str(e) if field["kind"] != "int" or "between" in str(e) \
                else "must be a whole number"
    if errors:
        return errors
    new_key = (form.get(SECRET) or "").strip()
    if form.get("clear_" + SECRET):
        deletes.append(SECRET)
    elif new_key:
        writes[SECRET] = new_key
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
