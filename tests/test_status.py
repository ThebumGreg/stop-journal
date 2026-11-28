"""/healthz and /api/status: for a dashboard and an uptime monitor (app/status.py)."""
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
import requests

from app import auth, create_app, db, logs, sample, status
from app.config import Config

CFG = Config.from_env()
TOKEN = "t0ken-" + "a" * 40
GOOD = {"Authorization": f"Bearer {TOKEN}"}
STAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
TZ = "America/Los_Angeles"


class Recorder(sample.ListRecorder):
    def __init__(self, points=(), error=None, delay=0):
        super().__init__(list(points))
        self.error, self.delay, self.calls = error, delay, 0

    def last_tst(self):
        self.calls += 1
        time.sleep(self.delay)
        if self.error:
            raise self.error
        return super().last_tst()


def make(tmp_path, client=None, **kw):
    kw = {"status_token": TOKEN, "recorder_url": "http://192.168.1.9:8083", "recorder_user": "someuser",
          "recorder_device": "somephone", "app_tz": TZ, "app_password": "", "mail_to": "", **kw}
    return create_app(replace(CFG, data_dir=str(tmp_path), **kw), client=client or Recorder())


def day(offset=0):
    return datetime.fromtimestamp(time.time() + offset * 86400, ZoneInfo(TZ)).date().isoformat()


def add_stop(tmp_path, date, status_="Draft", kind="stop", arrive=1795000000, name="Secret Dental Office"):
    conn = db.connect(str(tmp_path))
    conn.execute("INSERT INTO stops(date,lat,lon,arrive_utc,depart_utc,business_name,address,notes,status,kind) "
                 "VALUES (?,34.123456,-118.654321,?,?,?,?,?,?,?)",
                 (date, arrive, arrive + 600, name, "100 Hidden Ave", "call bob@client.example", status_, kind))
    conn.commit()
    conn.close()


def fingerprint(tmp_path):
    return hashlib.sha256((tmp_path / "stop_journal.db").read_bytes()).hexdigest()


def check_shape(body):
    assert set(body) == {"app", "version", "status", "checked_at", "uptime_seconds", "checks", "metrics"}
    assert body["app"] == "jobhub" and isinstance(body["version"], str) and body["version"]
    assert body["status"] in ("ok", "degraded", "down")
    assert STAMP.match(body["checked_at"]) and isinstance(body["uptime_seconds"], int)
    for c in body["checks"].values():
        assert set(c) <= {"status", "latency_ms", "detail"} and c["status"] in ("ok", "degraded", "down")
        assert isinstance(c["latency_ms"], int) and len(c.get("detail", "")) <= 60
    assert 3 <= len(body["metrics"]) <= 6 and set(body["metrics"]) == set(status.METRICS)
    for key, v in body["metrics"].items():
        assert re.match(r"^[a-z]+(_[a-z]+)*$", key)
        assert v is None or isinstance(v, (int, float)) or STAMP.match(v), (key, v)


def test_healthz_open_minimal_and_quick(tmp_path):
    app = make(tmp_path, app_password="pw")
    t = time.monotonic()
    r = app.test_client().get("/healthz")
    assert time.monotonic() - t < 0.2
    assert r.status_code == 200 and r.json == {"status": "ok"}
    assert "Set-Cookie" not in r.headers and "Access-Control-Allow-Origin" not in r.headers
    assert r.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("damage", ["gone", "garbage"])
def test_database_unreachable_is_down(tmp_path, damage):
    app = make(tmp_path)
    path = tmp_path / "stop_journal.db"
    path.unlink() if damage == "gone" else path.write_bytes(b"not a database" * 200)
    c = app.test_client()
    r = c.get("/healthz")
    assert r.status_code == 503 and r.json == {"status": "down"}
    r = c.get("/api/status", headers=GOOD)
    assert r.status_code == 503 and r.json["status"] == "down"
    assert r.json["checks"]["database"]["detail"] == "unreadable"
    assert all(r.json["metrics"][k] is None for k in ("stops_today", "unfinished_stops", "last_stop_at"))
    check_shape(r.json)
    assert damage != "gone" or not path.exists()            # read-only: nothing re-creates it


def test_token_off_wrong_and_right(tmp_path):
    off = make(tmp_path / "off", status_token="").test_client()
    assert off.get("/api/status").status_code == 404 and off.get("/api/status", headers=GOOD).status_code == 404
    assert off.get("/api/status", headers={"Authorization": "Bearer "}).status_code == 404
    c = make(tmp_path / "on").test_client()
    for bad in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN}, {"Authorization": f"Basic {TOKEN}"},
                {"Authorization": f"Bearer {TOKEN}x"}, {"Authorization": "Bearer café"}):
        r = c.get("/api/status", headers=bad)
        assert r.status_code == 401 and r.json == {"error": "unauthorized"}, bad
    r = c.get("/api/status", headers=GOOD)
    assert r.status_code == 200 and r.content_type == "application/json"
    assert r.headers["Cache-Control"] == "no-store" and "Access-Control-Allow-Origin" not in r.headers
    assert len(r.data) < 4096
    check_shape(r.json)


def test_token_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("STATUS_TOKEN", "  abc  ")
    assert Config.from_env().status_token == "abc"
    monkeypatch.delenv("STATUS_TOKEN")
    assert Config.from_env().status_token == ""


def test_get_only(tmp_path):
    c = make(tmp_path, app_password="pw").test_client()
    for path in status.PATHS:
        for method in ("post", "put", "patch", "delete", "head", "options"):
            r = getattr(c, method)(path, headers={**GOOD, "Origin": "https://elsewhere.example"})
            assert r.status_code == 405 and r.headers["Allow"] == "GET", (path, method)
        assert c.post(path, headers={"X-Method-Override": "DELETE"}).status_code == 405


def test_sign_in_is_neither_needed_nor_used(tmp_path):
    app = make(tmp_path)
    conn = db.connect(str(tmp_path))
    auth.create(conn, "greg", "correct horse battery", "")
    conn.close()
    c = app.test_client()
    c.post("/login", data={"username": "greg", "password": "correct horse battery"})
    assert c.get("/api/places").status_code == 200
    assert c.get("/api/status").status_code == 401                      # the login cookie is no token
    for r in (c.get("/api/status", headers=GOOD), c.get("/healthz")):
        assert r.status_code == 200 and "Set-Cookie" not in r.headers
    anon = app.test_client()
    assert anon.get("/api/status", headers=GOOD).status_code == 200
    assert anon.get("/api/places", headers=GOOD).status_code == 401     # the token opens nothing else
    assert anon.get("/api/status/", headers=GOOD).status_code == 401


def test_metrics(tmp_path):
    add_stop(tmp_path, day(), "Draft", arrive=1795000000)
    add_stop(tmp_path, day(), "Confirmed", arrive=1795003600)
    add_stop(tmp_path, day(), "Draft", kind="short", arrive=1795009999)      # short pauses don't count
    add_stop(tmp_path, day(-1), "Confirmed")
    add_stop(tmp_path, day(-1), "Confirmed")
    add_stop(tmp_path, day(-2), "Confirmed")
    add_stop(tmp_path, day(-3), "Draft")
    add_stop(tmp_path, day(-40), "Draft")                                    # older than the window
    add_stop(tmp_path, day(-40), "Confirmed")
    conn = db.connect(str(tmp_path))
    conn.execute("INSERT INTO email_log(ts,date_from,date_to,ok) VALUES (1,?,?,1)", (day(-2), day(-2)))
    conn.execute("INSERT INTO email_log(ts,date_from,date_to,ok) VALUES (1,?,?,0)", (day(-1), day(-1)))   # failed send
    conn.commit()
    conn.close()
    os.makedirs(tmp_path / "backups")
    (tmp_path / "backups" / "jobhub-2026-11-20.db").write_bytes(b"x")
    os.utime(tmp_path / "backups" / "jobhub-2026-11-20.db", (1795100000, 1795100000))
    rec = Recorder([{"tst": 1795004000, "lat": 34.5, "lon": -118.5}])
    app = make(tmp_path, rec, mail_to="agent@work.example", smtp_user="me@mail.example", smtp_password="x")
    before = fingerprint(tmp_path)
    m = app.test_client().get("/api/status", headers=GOOD).json["metrics"]
    assert m == {"stops_today": 2, "unfinished_stops": 2, "reports_pending": 1,
                 "last_point_at": status._iso(1795004000), "last_stop_at": status._iso(1795003600),
                 "last_backup_at": status._iso(1795100000)}
    assert fingerprint(tmp_path) == before                                   # nothing written


def test_unknown_values_are_null(tmp_path):
    m = make(tmp_path).test_client().get("/api/status", headers=GOOD).json["metrics"]
    assert m == {"stops_today": 0, "unfinished_stops": 0, "reports_pending": None, "last_point_at": None,
                 "last_stop_at": None, "last_backup_at": None}


def response_with(error):
    r = requests.Response()
    r.status_code = error
    return requests.HTTPError("401 for url http://192.168.1.9:8083/api/0/last?user=someuser", response=r)


@pytest.mark.parametrize("error,detail", [
    (requests.ConnectionError("no route to 192.168.1.9"), "unreachable"), (requests.Timeout(), "timeout"),
    (response_with(401), "sign-in refused"), (response_with(500), "error answer"),
    (ValueError("/data/secret"), "unexpected answer")])
def test_failing_recorder_is_degraded(tmp_path, error, detail):
    r = make(tmp_path, Recorder(error=error)).test_client().get("/api/status", headers=GOOD)
    assert r.status_code == 200 and r.json["status"] == "degraded"
    assert r.json["checks"]["recorder"]["detail"] == detail and r.json["checks"]["database"]["status"] == "ok"
    assert "192.168" not in r.get_data(as_text=True)
    check_shape(r.json)


def test_slow_recorder_is_cut_off(tmp_path, monkeypatch):
    monkeypatch.setattr(status, "CHECK_TIMEOUT_S", 0.2)
    c = make(tmp_path, Recorder(delay=1.5)).test_client()
    t = time.monotonic()
    r = c.get("/api/status", headers=GOOD)
    assert time.monotonic() - t < 1.0
    assert r.json["checks"]["recorder"] == {"status": "degraded", "detail": "timeout",
                                            "latency_ms": r.json["checks"]["recorder"]["latency_ms"]}


def test_database_down_outranks_degraded_recorder(tmp_path):
    app = make(tmp_path, Recorder(error=requests.Timeout()))
    (tmp_path / "stop_journal.db").unlink()
    assert app.test_client().get("/api/status", headers=GOOD).json["status"] == "down"


def test_no_recorder_check_when_not_set_up(tmp_path):
    rec = Recorder()
    r = make(tmp_path, rec, recorder_url="").test_client().get("/api/status", headers=GOOD)
    assert r.json["status"] == "ok" and set(r.json["checks"]) == {"database"} and rec.calls == 0


def test_recorder_settings_saved_in_the_app_are_used(tmp_path):
    rec = Recorder()
    app = make(tmp_path, rec, recorder_url="")
    conn = db.connect(str(tmp_path))
    conn.execute("INSERT INTO settings(key,value) VALUES ('recorder_url','http://rec')")
    conn.commit()
    conn.close()
    assert "recorder" in app.test_client().get("/api/status", headers=GOOD).json["checks"] and rec.calls == 1


def test_results_are_cached(tmp_path, monkeypatch):
    rec = Recorder()
    c = make(tmp_path, rec).test_client()
    for _ in range(5):
        c.get("/api/status", headers=GOOD)
    assert rec.calls == 1
    c.get("/healthz")
    (tmp_path / "stop_journal.db").rename(tmp_path / "moved.db")
    assert c.get("/healthz").status_code == 200                       # still the cached answer
    monkeypatch.setattr(status, "CACHE_S", 0)
    assert c.get("/healthz").status_code == 503
    c.get("/api/status", headers=GOOD)
    assert rec.calls == 2


def test_nothing_sensitive_in_either_answer(tmp_path):
    add_stop(tmp_path, day())
    rec = Recorder([{"tst": 1795004000, "lat": 34.123456, "lon": -118.654321}])
    c = make(tmp_path, rec, mail_to="agent@work.example", smtp_user="me@mail.example", smtp_password="hunter2",
             recorder_auth="u:recorderpass").test_client()
    text = c.get("/api/status", headers=GOOD).get_data(as_text=True) + c.get("/healthz").get_data(as_text=True)
    for pattern in (r"[\w.+-]+@[\w-]+\.\w+", r"\b\d{1,3}(\.\d{1,3}){3}\b", r"-?\d{1,3}\.\d{4,}", r"https?://",
                    r"(/[\w-]+){2,}", r"SELECT ", r"Traceback"):
        assert not re.search(pattern, text), pattern
    for word in ("Secret", "Hidden Ave", "someuser", "somephone", "hunter2", "recorderpass", TOKEN, str(tmp_path)):
        assert word not in text


def test_polls_stay_out_of_the_log_unless_debugging(tmp_path):
    c = make(tmp_path).test_client()
    logs.set_level("info")
    mark = len(logs.recent(1000))
    c.get("/healthz"), c.get("/api/status", headers=GOOD), c.get("/api/status")
    assert not [l for l in logs.recent(1000)[mark:] if "healthz" in l or "/api/status" in l]
    access = logging.getLogger("gunicorn.access")
    line = lambda path: logging.LogRecord("gunicorn.access", logging.INFO, "", 0, "%(U)s", ({"U": path},), None)
    assert not access.filter(line("/healthz")) and not access.filter(line("/api/status"))
    assert access.filter(line("/api/places"))
    logs.set_level("debug")
    try:
        assert access.filter(line("/healthz"))
        c.get("/api/status", headers=GOOD)
        assert any("/api/status" in l for l in logs.recent(20))
        assert TOKEN not in "\n".join(logs.recent(1000))
    finally:
        logs.set_level("info")
