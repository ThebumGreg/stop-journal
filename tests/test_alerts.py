from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app import alerts, create_app, db, notify, sample
from tests.test_detect import CFG

TZ = ZoneInfo("America/Los_Angeles")


def ts(y, mo, d, h, mi=0):
    return int(datetime(y, mo, d, h, mi, tzinfo=TZ).timestamp())


WED_9 = ts(2026, 11, 18, 9)          # a Wednesday
SAT_9 = ts(2026, 11, 21, 9)


class Rec:
    def __init__(self, last=None, boom=False):
        self.last, self.boom = last, boom

    def last_tst(self):
        if self.boom:
            raise ConnectionError("down")
        return self.last


class Sender:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def __call__(self, cfg, title, message, priority=0, url=None):
        self.calls.append((title, message, url))
        if self.fail:
            raise notify.NotifyError("Pushover: nope")


def base(tmp_path, **kw):
    kw.setdefault("pushover_token", "tok")
    kw.setdefault("pushover_user", "usr")
    return replace(CFG, data_dir=str(tmp_path), **kw)


def run(cfg, rec, now, send):
    alerts.run_once(cfg, rec, now=now, send=send)


# ---- Pushover client --------------------------------------------------
class Resp:
    def __init__(self, status, body):
        self.status_code, self.body = status, body

    def json(self):
        return self.body


class Sess:
    def __init__(self, resp):
        self.resp, self.sent = resp, []

    def post(self, url, data=None, timeout=None):
        self.sent.append((url, data))
        return self.resp


def test_pushover_payload_and_errors():
    cfg = replace(CFG, pushover_token="TOK", pushover_user="USR")
    s = Sess(Resp(200, {"status": 1}))
    notify.send(cfg, "T", "M", 0, "http://x", session=s)
    url, data = s.sent[0]
    assert url.endswith("/messages.json") and data["title"] == "T" and data["url"] == "http://x"
    with pytest.raises(notify.NotifyError, match="application token is invalid") as e:
        notify.send(cfg, "T", "M", session=Sess(Resp(400, {"status": 0, "errors": ["application token is invalid"]})))
    assert "TOK" not in str(e.value) and "USR" not in str(e.value)
    with pytest.raises(notify.NotifyError, match="isn't set up"):
        notify.send(CFG, "T", "M")


# ---- no-data alert ----------------------------------------------------
def test_alert_fires_once_after_threshold(tmp_path):
    cfg, snd = base(tmp_path), Sender()
    rec = Rec(last=WED_9)
    run(cfg, rec, WED_9 + 30 * 60, snd)
    assert snd.calls == []                              # 30 min < 60 min threshold
    run(cfg, rec, WED_9 + 65 * 60, snd)
    assert len(snd.calls) == 1 and "65 min" in snd.calls[0][1] and "9:00 AM" in snd.calls[0][1]
    run(cfg, rec, WED_9 + 90 * 60, snd)
    assert len(snd.calls) == 1                          # not repeated


def test_alert_message_has_no_location_data(tmp_path):
    cfg, snd = base(tmp_path, app_url="http://192.168.1.5:5000"), Sender()
    run(cfg, Rec(last=WED_9), WED_9 + 70 * 60, snd)
    title, msg, url = snd.calls[0]
    assert url == "http://192.168.1.5:5000"
    assert "lat" not in msg.lower() and "lon" not in msg.lower()


def test_recovery_rearms(tmp_path):
    cfg, snd = base(tmp_path), Sender()
    run(cfg, Rec(last=WED_9), WED_9 + 70 * 60, snd)
    run(cfg, Rec(last=WED_9 + 75 * 60), WED_9 + 76 * 60, snd)   # data resumed
    run(cfg, Rec(last=WED_9 + 75 * 60), WED_9 + 140 * 60, snd)  # silent again
    assert len(snd.calls) == 2


def test_repeat_reminder(tmp_path):
    cfg, snd = base(tmp_path, alert_repeat_min=30), Sender()
    rec = Rec(last=WED_9)
    run(cfg, rec, WED_9 + 65 * 60, snd)
    run(cfg, rec, WED_9 + 80 * 60, snd)
    assert len(snd.calls) == 1
    run(cfg, rec, WED_9 + 100 * 60, snd)
    assert len(snd.calls) == 2


def test_only_on_work_days_and_hours(tmp_path):
    cfg, snd = base(tmp_path), Sender()
    run(cfg, Rec(last=SAT_9), SAT_9 + 3 * 3600, snd)                 # Saturday
    run(cfg, Rec(last=ts(2026, 11, 18, 6)), ts(2026, 11, 18, 6, 50), snd)   # before 07:00
    run(cfg, Rec(last=ts(2026, 11, 18, 14)), ts(2026, 11, 18, 18), snd)     # after 17:00
    assert snd.calls == []


def test_overnight_gap_does_not_alarm_at_start_of_day(tmp_path):
    cfg, snd = base(tmp_path), Sender()
    last_night = ts(2026, 11, 17, 18)
    run(cfg, Rec(last=last_night), ts(2026, 11, 18, 7, 5), snd)      # 5 min into the work day
    assert snd.calls == []
    run(cfg, Rec(last=last_night), ts(2026, 11, 18, 8, 5), snd)      # an hour in, still nothing
    assert len(snd.calls) == 1


def test_custom_days_and_hours(tmp_path):
    cfg, snd = base(tmp_path, work_days="Sat", work_start="08:00", work_end="12:00"), Sender()
    run(cfg, Rec(last=SAT_9), SAT_9 + 2 * 3600, snd)
    assert len(snd.calls) == 1


def test_recorder_unreachable_has_own_alert(tmp_path):
    cfg, snd = base(tmp_path), Sender()
    run(cfg, Rec(boom=True), WED_9 + 5 * 60, snd)
    assert len(snd.calls) == 1 and "reach" in snd.calls[0][0].lower()


def test_disabled_or_unconfigured_does_nothing(tmp_path):
    for kw in ({"alert_no_data": "off"}, {"pushover_token": "", "pushover_user": ""}):
        snd = Sender()
        run(base(tmp_path, **kw), Rec(last=WED_9), WED_9 + 5 * 3600, snd)
        assert snd.calls == []


def test_failed_delivery_retries_later_not_every_minute(tmp_path):
    cfg, snd = base(tmp_path), Sender(fail=True)
    rec = Rec(last=WED_9)
    run(cfg, rec, WED_9 + 65 * 60, snd)
    run(cfg, rec, WED_9 + 66 * 60, snd)
    assert len(snd.calls) == 1                          # backed off
    run(cfg, rec, WED_9 + 80 * 60, snd)                 # > 10 min later
    assert len(snd.calls) == 2
    conn = db.connect(cfg.data_dir)
    assert [r["ok"] for r in conn.execute("SELECT ok FROM notifications")] == [0, 0]
    snd.fail = False
    run(cfg, rec, WED_9 + 100 * 60, snd)
    assert len(snd.calls) == 3
    run(cfg, rec, WED_9 + 101 * 60, snd)
    assert len(snd.calls) == 3                          # delivered once, now quiet


def test_settings_page_overrides_apply_to_alerts(tmp_path):
    cfg = base(tmp_path, pushover_token="", pushover_user="")
    c = create_app(cfg, client=sample.ListRecorder([])).test_client()
    c.post("/settings", data={"pushover_token": "t", "pushover_user": "u", "alert_no_data_min": "10"})
    snd = Sender()
    run(cfg, Rec(last=WED_9), WED_9 + 11 * 60, snd)
    assert len(snd.calls) == 1


# ---- settings validation + pages -------------------------------------
def test_new_settings_validation(tmp_path):
    c = create_app(base(tmp_path), client=sample.ListRecorder([])).test_client()
    r = c.post("/settings", data={"work_days": "Funday", "work_start": "25:00", "alert_no_data_min": "1"})
    assert r.status_code == 400
    for msg in (b"Unknown day", b"time like 07:30", b"between 5 and 1440"):
        assert msg in r.data
    assert c.post("/settings", data={"work_days": "Mon,Wed-Fri", "work_start": "7:30"}).status_code == 302
    assert b'value="07:30"' in c.get("/settings").data


def test_pushover_secrets_write_only_and_test_route(tmp_path, monkeypatch):
    c = create_app(base(tmp_path, pushover_token="", pushover_user=""),
                   client=sample.ListRecorder([])).test_client()
    assert c.post("/settings/test-pushover").json["ok"] is False
    c.post("/settings", data={"pushover_token": "SECRETTOK1234", "pushover_user": "SECRETUSR5678"})
    page = c.get("/settings").data.decode()
    assert "SECRETTOK1234" not in page and "SECRETUSR5678" not in page and "…1234" in page
    sent = []
    monkeypatch.setattr(notify, "send", lambda cfg, *a, **k: sent.append(a))
    assert c.post("/settings/test-pushover").json["ok"] is True and sent
    page = c.get("/settings").data.decode()
    assert "JobHub test" in page and "delivered" in page                 # shows in the recent list


def test_branding_ios_meta_and_manifest(tmp_path):
    c = create_app(base(tmp_path, app_password="pw"), client=sample.ListRecorder([])).test_client()
    html = c.post("/login", data={"password": "pw"}) and c.get("/").data.decode()
    assert "<title>JobHub</title>" in html and "viewport-fit=cover" in html
    assert 'rel="apple-touch-icon"' in html and 'id="split"' in html
    m = c.get("/manifest.webmanifest")
    assert m.status_code == 200 and m.json["short_name"] == "JobHub"
    anon = create_app(base(tmp_path, app_password="pw"), client=sample.ListRecorder([])).test_client()
    assert anon.get("/manifest.webmanifest").status_code == 200       # reachable before login
    assert anon.get("/static/icon-180.png").status_code == 200
    assert anon.get("/").status_code == 302
