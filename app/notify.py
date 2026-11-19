"""Outbound notifications (Pushover). Message text must never contain coordinates
or addresses: it leaves the server for a third party."""
import time

import requests

URL = "https://api.pushover.net/1/messages.json"


class NotifyError(Exception):
    """Delivery failure; the message is safe to show (never contains credentials)."""


def configured(cfg):
    return bool(cfg.pushover_token and cfg.pushover_user)


def send(cfg, title, message, priority=0, url=None, session=None):
    if not configured(cfg):
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
