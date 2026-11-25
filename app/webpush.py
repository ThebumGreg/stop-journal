"""Native notifications on the iPhone Home Screen app (and desktop browsers): standard Web Push.

The phone subscribes from Settings → Account ("Turn on notifications"); its push address (an Apple, Google, Mozilla or
Microsoft push server) and keys are stored in `push_devices`. JobHub signs each message with its own key pair (VAPID,
made once and kept in the settings table) and the push server delivers it. The text passes through that push service,
so the same rule as Pushover applies: never coordinates, addresses, business names or ticket numbers.

iOS needs 16.4+, the app added to the Home Screen from the https address, and a tap to allow notifications.
"""
import base64
import json
import sqlite3
import os
import time
from urllib.parse import urlsplit

TTL_S = 4 * 3600                 # an alert older than this is not worth delivering
# Only real push services: a subscription can't make JobHub post to any other address.
PUSH_HOSTS = ("push.apple.com", "fcm.googleapis.com", "push.services.mozilla.com", "notify.windows.com",
              "android.googleapis.com")

SCHEMA = """
CREATE TABLE IF NOT EXISTS push_devices (
    id INTEGER PRIMARY KEY,
    user_id INTEGER,
    endpoint TEXT NOT NULL UNIQUE,
    p256dh TEXT NOT NULL,
    auth TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    created_ts INTEGER NOT NULL,
    last_ok_ts INTEGER,
    last_error TEXT
);
"""


class PushError(Exception):
    pass


def _b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def keys(conn):
    """(private PEM, public key for the browser) — made the first time and kept."""
    rows = dict(conn.execute("SELECT key, value FROM settings WHERE key IN ('_vapid_private','_vapid_public')")
                .fetchall())
    if rows.get("_vapid_private") and rows.get("_vapid_public"):
        return rows["_vapid_private"], rows["_vapid_public"]
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    k = ec.generate_private_key(ec.SECP256R1())
    pem = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                          serialization.NoEncryption()).decode()
    pub = _b64(k.public_key().public_bytes(serialization.Encoding.X962,
                                           serialization.PublicFormat.UncompressedPoint))
    conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES ('_vapid_private',?)", (pem,))
    conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES ('_vapid_public',?)", (pub,))
    conn.commit()
    return pem, pub


def allowed_endpoint(url):
    try:
        u = urlsplit(url)
    except ValueError:
        return False
    host = (u.hostname or "").lower()
    return u.scheme == "https" and any(host == h or host.endswith("." + h) for h in PUSH_HOSTS)


def label_for(user_agent):
    ua = user_agent or ""
    dev = "iPhone" if "iPhone" in ua else "iPad" if "iPad" in ua else "Android" if "Android" in ua else (
        "Windows" if "Windows" in ua else "Mac" if "Macintosh" in ua else "Linux" if "Linux" in ua else "device")
    app = "Firefox" if "Firefox" in ua else "Edge" if "Edg/" in ua else "Chrome" if "Chrome" in ua else (
        "Home Screen app" if dev in ("iPhone", "iPad") else "Safari" if "Safari" in ua else "browser")
    return f"{dev} · {app}"


def subscribe(conn, user_id, sub, user_agent="", now=None):
    endpoint = str((sub or {}).get("endpoint") or "")
    k = (sub or {}).get("keys") or {}
    p256dh, auth = str(k.get("p256dh") or ""), str(k.get("auth") or "")
    if not allowed_endpoint(endpoint) or len(endpoint) > 1000 or not (20 < len(p256dh) < 200) or not (8 < len(auth) < 64):
        raise PushError("This browser gave a push address JobHub does not accept.")
    now = int(now or time.time())
    label = label_for(user_agent) if user_agent else None
    conn.execute("INSERT INTO push_devices(user_id,endpoint,p256dh,auth,label,created_ts) "
                 "VALUES (?,?,?,?,COALESCE(?,'device'),?) "
                 "ON CONFLICT(endpoint) DO UPDATE SET user_id=excluded.user_id, p256dh=excluded.p256dh, "
                 "auth=excluded.auth, label=COALESCE(?, label), last_error=NULL",
                 (user_id, endpoint, p256dh, auth, label, now, label))
    conn.commit()
    return conn.execute("SELECT id FROM push_devices WHERE endpoint=?", (endpoint,)).fetchone()[0]


def remove(conn, device_id=None, endpoint=None):
    if endpoint:
        conn.execute("DELETE FROM push_devices WHERE endpoint=?", (endpoint,))
    else:
        conn.execute("DELETE FROM push_devices WHERE id=?", (device_id,))
    conn.commit()


def devices(conn):
    return [dict(r) for r in conn.execute(
        "SELECT id, user_id, label, created_ts, last_ok_ts, last_error, endpoint FROM push_devices ORDER BY id")]


def has_devices(data_dir):
    """Cheap check (no schema work): is any phone or browser signed up?"""
    path = os.path.join(data_dir or "", "stop_journal.db")
    if not data_dir or not os.path.exists(path):
        return False
    try:
        c = sqlite3.connect(path, timeout=5)
        try:
            return c.execute("SELECT 1 FROM push_devices LIMIT 1").fetchone() is not None
        finally:
            c.close()
    except sqlite3.Error:
        return False


def _subject(cfg):
    u = (cfg.app_url or "").strip()
    return u if u.startswith("https://") else "mailto:jobhub@example.com"


def payload(title, message, url=None):
    path = "/"
    if url:
        s = urlsplit(url)
        path = (s.path or "/") + (("?" + s.query) if s.query else "") + (("#" + s.fragment) if s.fragment else "")
    return json.dumps({"title": title[:120], "body": message[:400], "url": path, "tag": title[:60]})


def send_all(cfg, title, message, priority=0, url=None, sender=None, now=None):
    """Send to every signed-up device. Gone subscriptions (404/410) are removed. Raises PushError when none got it."""
    from . import db
    conn = db.connect(cfg.data_dir)
    try:
        devs = conn.execute("SELECT * FROM push_devices ORDER BY id").fetchall()
        if not devs:
            raise PushError("No phone or browser has notifications turned on (Settings → Account).")
        pem, _ = keys(conn)
        if sender is None:
            from pywebpush import webpush
            from py_vapid import Vapid02
            vapid = Vapid02.from_pem(pem.encode())

            def sender(info, data, claims, urgency):
                return webpush(info, data, vapid_private_key=vapid, vapid_claims=claims, ttl=TTL_S, timeout=10,
                               headers={"Urgency": urgency})
        data = payload(title, message, url)
        now = int(now or time.time())
        ok, errors = 0, []
        for d in devs:
            info = {"endpoint": d["endpoint"], "keys": {"p256dh": d["p256dh"], "auth": d["auth"]}}
            try:
                sender(info, data, {"sub": _subject(cfg)}, "high" if priority > 0 else "normal")
                conn.execute("UPDATE push_devices SET last_ok_ts=?, last_error=NULL WHERE id=?", (now, d["id"]))
                ok += 1
            except Exception as e:
                status = getattr(getattr(e, "response", None), "status_code", None)
                if status in (404, 410):              # the phone dropped it (app removed, notifications off)
                    conn.execute("DELETE FROM push_devices WHERE id=?", (d["id"],))
                    errors.append(f"{d['label']}: no longer signed up (removed)")
                else:
                    msg = f"{d['label']}: " + (f"push service said {status}" if status else type(e).__name__)
                    conn.execute("UPDATE push_devices SET last_error=? WHERE id=?", (msg[:200], d["id"]))
                    errors.append(msg)
        conn.commit()
        if not ok:
            raise PushError("; ".join(errors) or "not delivered")
        return ok
    finally:
        conn.close()
