"""Native notifications (Web Push): keys, sign-up, sending, channels, routes."""
import base64
import json
from dataclasses import replace

import pytest

from app import auth, create_app, db, notify, webpush
from app.config import Config

CFG = Config.from_env()
APPLE = "https://web.push.apple.com/QGuQyavXutnMH-abc123"


def b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def client_keys():
    """A phone's key pair and auth secret, so the test can decrypt what JobHub sends."""
    import os
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    k = ec.generate_private_key(ec.SECP256R1())
    pub = k.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    auth_secret = os.urandom(16)
    return k, {"endpoint": APPLE, "keys": {"p256dh": b64(pub), "auth": b64(auth_secret)}}, auth_secret


def cfg_for(tmp_path, **kw):
    return replace(CFG, data_dir=str(tmp_path), **kw)


def test_keys_made_once_and_kept(tmp_path):
    conn = db.connect(str(tmp_path))
    pem, pub = webpush.keys(conn)
    assert webpush.keys(conn) == (pem, pub)
    raw = base64.urlsafe_b64decode(pub + "==")
    assert len(raw) == 65 and raw[0] == 4 and "PRIVATE KEY" in pem


def test_only_real_push_services_accepted(tmp_path):
    conn = db.connect(str(tmp_path))
    _, sub, _ = client_keys()
    for bad in ("http://web.push.apple.com/x", "https://evil.example/x", "https://192.168.1.5/x",
                "https://push.apple.com.evil.example/x"):
        with pytest.raises(webpush.PushError):
            webpush.subscribe(conn, 1, {**sub, "endpoint": bad})
    webpush.subscribe(conn, 1, sub, "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit")
    webpush.subscribe(conn, 1, sub)                                    # same phone again: still one row
    d = webpush.devices(conn)
    assert len(d) == 1 and d[0]["label"] == "iPhone · Home Screen app"
    assert webpush.has_devices(str(tmp_path)) and not webpush.has_devices("")


def test_real_encryption_decrypts_on_the_phone(tmp_path, monkeypatch):
    import http_ece
    import requests
    phone_key, sub, secret = client_keys()
    conn = db.connect(str(tmp_path))
    webpush.subscribe(conn, 1, sub)
    posted = []

    class R:
        status_code, text, headers = 201, "", {}

    def fake_post(url, data=None, headers=None, timeout=None, **kw):
        posted.append((url, data, headers))
        return R()
    monkeypatch.setattr(requests, "post", fake_post)
    n = webpush.send_all(cfg_for(tmp_path, app_url="https://jobhub.example.com"), "JobHub",
                         "No location data for 30 min", 1, "http://192.168.1.5:8000/day?x=1")
    assert n == 1
    url, body, headers = posted[0]
    assert url == APPLE and headers["Urgency"] == "high" and int(headers["TTL"]) == webpush.TTL_S
    assert headers["Authorization"].startswith("vapid t=") and "k=" in headers["Authorization"]
    plain = json.loads(http_ece.decrypt(body, private_key=phone_key, auth_secret=secret, version="aes128gcm"))
    assert plain == {"title": "JobHub", "body": "No location data for 30 min", "url": "/day?x=1", "tag": "JobHub"}
    # the VAPID token names the push service and JobHub's https address, nothing else
    token = headers["Authorization"].split("t=")[1].split(",")[0]
    claims = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "=="))
    assert claims["aud"] == "https://web.push.apple.com" and claims["sub"] == "https://jobhub.example.com"


def test_gone_device_removed_and_errors_kept(tmp_path):
    conn = db.connect(str(tmp_path))
    _, a, _ = client_keys()
    _, b, _ = client_keys()
    webpush.subscribe(conn, 1, a)
    webpush.subscribe(conn, 1, {**b, "endpoint": "https://fcm.googleapis.com/fcm/send/xyz"})

    class Gone(Exception):
        response = type("R", (), {"status_code": 410})()

    def sender(info, data, claims, urgency):
        if "apple" in info["endpoint"]:
            raise Gone()
    assert webpush.send_all(cfg_for(tmp_path), "JobHub", "x", sender=sender) == 1
    assert [d["endpoint"] for d in webpush.devices(conn)] == ["https://fcm.googleapis.com/fcm/send/xyz"]

    def broken(info, data, claims, urgency):
        raise TimeoutError()
    with pytest.raises(webpush.PushError, match="TimeoutError"):
        webpush.send_all(cfg_for(tmp_path), "JobHub", "x", sender=broken)
    assert "TimeoutError" in webpush.devices(conn)[0]["last_error"]


def test_channels_pick_iphone_pushover_or_both(tmp_path, monkeypatch):
    po = dict(pushover_token="t", pushover_user="u")
    assert notify.channels(cfg_for(tmp_path, **po)) == ["pushover"]
    conn = db.connect(str(tmp_path))
    webpush.subscribe(conn, 1, client_keys()[1])
    assert notify.channels(cfg_for(tmp_path, **po)) == ["iphone", "pushover"]
    assert notify.channels(cfg_for(tmp_path, notify_channel="iphone", **po)) == ["iphone"]
    assert notify.channels(cfg_for(tmp_path, notify_channel="pushover")) == []
    sent = []
    monkeypatch.setattr(webpush, "send_all", lambda cfg, *a: sent.append(("iphone", a)))

    def po_fail(*a, **k):
        raise notify.NotifyError("Pushover: down")
    monkeypatch.setattr(notify, "send_pushover", po_fail)
    notify.send(cfg_for(tmp_path, **po), "JobHub", "msg")             # one channel worked: sent
    assert sent and sent[0][1][:2] == ("JobHub", "msg")
    monkeypatch.setattr(webpush, "send_all", lambda cfg, *a: (_ for _ in ()).throw(webpush.PushError("gone")))
    with pytest.raises(notify.NotifyError, match="iPhone: gone; Pushover: down"):
        notify.send(cfg_for(tmp_path, **po), "JobHub", "msg")


def test_routes(tmp_path, monkeypatch):
    app = create_app(cfg_for(tmp_path), client=object())
    conn = db.connect(str(tmp_path))
    auth.create(conn, "greg", "correct horse battery")
    c, anon = app.test_client(), app.test_client()
    sw = anon.get("/sw.js")                                           # the phone fetches it without the cookie
    assert sw.status_code == 200 and "javascript" in sw.headers["Content-Type"] and b"showNotification" in sw.data
    assert anon.get("/api/push").status_code == 401
    assert anon.post("/api/push/subscribe", json={}).status_code == 401
    c.post("/login", data={"username": "greg", "password": "correct horse battery"})
    assert c.get("/api/push").json["devices"] == []
    assert "Turn on notifications" in c.get("/account").data.decode()
    r = c.post("/api/push/subscribe", json={"subscription": {"endpoint": "https://evil.example/x", "keys": {}}})
    assert r.status_code == 400
    _, sub, _ = client_keys()
    assert c.post("/api/push/subscribe", json={"subscription": sub},
                  headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; rv:133.0) Gecko Firefox/133.0"}).json["ok"]
    d = c.get("/api/push").json["devices"]
    assert d[0]["label"] == "Windows · Firefox" and "endpoint" not in d[0]
    calls = []
    monkeypatch.setattr(webpush, "send_all", lambda cfg, *a: calls.append(a) or 1)
    assert c.post("/api/push/test").json["ok"] and calls
    assert c.post("/api/push/remove", json={"endpoint": sub["endpoint"]}).json["ok"]
    assert c.get("/api/push").json["devices"] == []
