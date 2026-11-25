"""Outbound notifications: native ones on the iPhone / browser (Web Push, app/webpush.py) and Pushover.
Settings → Notifications → "Send alerts to" picks iPhone, Pushover or both (each only when set up).
Message text must never contain coordinates, addresses, business names or ticket numbers: it passes through a third
party (Apple's / the browser's push service, or Pushover)."""
import time

import requests

URL = "https://api.pushover.net/1/messages.json"


class NotifyError(Exception):
    """Delivery failure; the message is safe to show (never contains credentials)."""


def pushover_configured(cfg):
    return bool(cfg.pushover_token and cfg.pushover_user)


def channels(cfg):
    """The ways an alert goes out now: "iphone" (Web Push devices) and/or "pushover"."""
    from . import webpush
    want = (getattr(cfg, "notify_channel", "") or "both").lower()
    out = []
    if want in ("both", "iphone") and webpush.has_devices(cfg.data_dir):
        out.append("iphone")
    if want in ("both", "pushover") and pushover_configured(cfg):
        out.append("pushover")
    return out


def configured(cfg):
    return bool(channels(cfg))


def send(cfg, title, message, priority=0, url=None, session=None):
    """Send on every channel in use; it counts as sent when one of them delivered."""
    from . import webpush
    chans = channels(cfg)
    if not chans:
        raise NotifyError("Notifications aren't set up: turn them on in Settings → Account (iPhone), "
                          "or add the Pushover keys.")
    errors, ok = [], False
    for ch in chans:
        try:
            if ch == "iphone":
                webpush.send_all(cfg, title, message, priority, url)
            else:
                send_pushover(cfg, title, message, priority, url, session)
            ok = True
        except (NotifyError, webpush.PushError) as e:
            errors.append(("iPhone: " if ch == "iphone" else "") + str(e))
    if not ok:
        raise NotifyError("; ".join(errors))


def send_pushover(cfg, title, message, priority=0, url=None, session=None):
    if not pushover_configured(cfg):
        raise NotifyError("Pushover isn't set up. Add the application token and user key in Settings.")
    data = {"token": cfg.pushover_token, "user": cfg.pushover_user,
            "title": title[:250], "message": message[:1000], "priority": priority}
    if url:
        data["url"], data["url_title"] = url, "Open JobHub"
    try:
        r = (session or requests).post(URL, data=data, timeout=10)
    except requests.RequestException as e:
        raise NotifyError(f"Could not reach Pushover ({type(e).__name__}).")
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code != 200 or body.get("status") != 1:
        errs = ", ".join(str(x) for x in body.get("errors", [])) or f"HTTP {r.status_code}"
        raise NotifyError(f"Pushover: {errs}")


def record(conn, title, message, ok, error=None):
    conn.execute("INSERT INTO notifications(ts,title,message,ok,error) VALUES (?,?,?,?,?)",
                 (int(time.time()), title, message, 1 if ok else 0, error))
    conn.execute("DELETE FROM notifications WHERE id NOT IN "
                 "(SELECT id FROM notifications ORDER BY id DESC LIMIT 100)")
    conn.commit()


def recent(conn, n=5):
    return [dict(r) for r in conn.execute(
        "SELECT ts, title, ok, error FROM notifications ORDER BY id DESC LIMIT ?", (n,))]
