"""Running late: the alert (generic text, once per appointment) and the Late Notice email (only on a tap)."""
import json
from dataclasses import replace

from app import alerts, calendar_feed, db, late, mailer, summary
from app.places.base import Candidate
from tests.test_detect import CFG
from tests.test_export import D, make


def setup(tmp_path, monkeypatch, start_in_min):
    c = make(tmp_path)
    last = c.get(f"/api/day/{D}").json["stops"][-1]
    now = last["depart_ts"] + 3 * 3600       # well after the last stop: the appointment is not matched to it
    ev = {"uid": "ev1@1", "start_ts": now + start_in_min * 60, "end_ts": now + start_in_min * 60 + 3600,
          "title": "Onsite Beta", "name": "Beta Plumbing", "location": "9 Oak Ave, Testville", "ticket": "77",
          "contact": "Jane 555-0100", "estimate": "2210"}
    monkeypatch.setattr(calendar_feed, "events_for_day", lambda cfg, date: [ev])
    import app.service as svc
    monkeypatch.setattr(svc.time, "time", lambda: now)
    cfg = replace(CFG, data_dir=str(tmp_path), calendar_url="https://x/cal.ics", routing_url="", alert_late_min=10,
                  mail_to="me@work.example", smtp_user="t@gmail.example", smtp_password="x", tech_name="Greg T",
                  tech_email="greg@work.example", late_notice_to="agent@work.example")
    live = {"lat": last["lat"], "lon": last["lon"], "moving": True, "last_update_s": 30}
    far = Candidate(name="9 Oak Ave", lat=last["lat"] + 0.15, lon=last["lon"])       # ~17 km: ~25+ min estimate
    return db.connect(str(tmp_path)), cfg, now, live, (lambda q, lat, lon: [far])


def test_late_shows_on_home_and_check(tmp_path, monkeypatch):
    conn, cfg, now, live, search = setup(tmp_path, monkeypatch, start_in_min=5)
    n = summary.build(conn, cfg, D, now, live, search)["next"]
    assert n["late"] and n["eta"]["late_min"] >= 10 and n["uid"] == "ev1@1" and n["late_notice"] is None
    ev, eta = late.check(conn, cfg, D, now, live, search)
    assert ev["uid"] == "ev1@1" and eta["late_min"] == n["eta"]["late_min"]
    # plenty of time: not late
    conn2, cfg2, now2, live2, search2 = setup(tmp_path / "b", monkeypatch, start_in_min=120)
    assert late.check(conn2, cfg2, D, now2, live2, search2) is None
    assert summary.build(conn2, cfg2, D, now2, live2, search2)["next"]["late"] is False
    # a stale position is not trusted
    assert late.check(conn, cfg, D, now, dict(live, last_update_s=3600), search) is None


class FakeClient:
    def __init__(self, live):
        self.pt = {"lat": live["lat"], "lon": live["lon"], "acc": 5}

    def fetch(self, a, b):
        return [dict(self.pt, tst=int(b.timestamp()) - 20)]


def test_alert_is_generic_and_sent_once(tmp_path, monkeypatch):
    conn, cfg, now, live, search = setup(tmp_path, monkeypatch, start_in_min=5)
    cfg = replace(cfg, recorder_url="http://r", recorder_user="u", recorder_device="d", app_url="http://jobhub.lan")
    late._last_check.clear()
    marks = {"date": D, "start": None, "end": None}
    conds = alerts.late_conditions(conn, cfg, FakeClient(live), now, marks, search)
    assert len(conds) == 1
    c = conds[0]
    assert c.key == "late:ev1@1" and c.once and c.url == "http://jobhub.lan/?late=1"
    for secret in ("Beta", "Oak", "Testville", "77", "Jane"):               # no names, places or tickets
        assert secret not in c.title + c.message
    sent = []
    assert alerts.apply(conn, cfg, c, now, lambda *a: sent.append(a))
    assert not alerts.apply(conn, cfg, c, now + 600, lambda *a: sent.append(a))   # once per appointment
    assert len(sent) == 1
    # checked every few minutes, not every minute
    assert alerts.late_conditions(conn, cfg, FakeClient(live), now + 60, marks, search) == []
    late._last_check.clear()
    assert alerts.late_conditions(conn, replace(cfg, alert_late="off"), FakeClient(live), now, marks, search) == []


def test_late_notice_email(tmp_path, monkeypatch):
    conn, cfg, now, live, search = setup(tmp_path, monkeypatch, start_in_min=5)
    cfg = replace(cfg, report_signing_key="shared phrase")
    msgs = []
    monkeypatch.setattr(mailer, "send", lambda cfg, msg: msgs.append(msg))
    r = late.send(conn, cfg, D, "ev1@1", now, live, search, note="Traffic on I-5")
    m = msgs[0]
    assert m["To"] == "agent@work.example" and m["Subject"] == "JobHub Late Notice 77"
    att = next(m.iter_attachments())
    data = att.get_content()
    data = data if isinstance(data, bytes) else data.encode()
    n = json.loads(data)
    assert n["type"] == "jobhub_late_notice" and n["hubspot_ticket"] == "77" and n["customer"] == "Beta Plumbing"
    assert n["address"] == "9 Oak Ave, Testville" and n["contact"] == "Jane 555-0100" and n["qbo_estimate"] == "2210"
    assert n["minutes_late"] == r["minutes_late"] >= 10 and n["note"] == "Traffic on I-5"
    assert n["technician"] == {"name": "Greg T", "email": "greg@work.example"}
    body = m.get_body(preferencelist=("plain",)).get_content()
    assert "Signature: hmac-sha256=" + mailer.signature("shared phrase", data) in body
    assert late.last_sent(conn, D, "ev1@1")["minutes_late"] == r["minutes_late"]
    assert summary.build(conn, cfg, D, now, live, search)["next"]["late_notice"]["mail_to"] == "agent@work.example"


def test_late_notice_refused_when_on_time_or_unknown(tmp_path, monkeypatch):
    conn, cfg, now, live, search = setup(tmp_path, monkeypatch, start_in_min=120)
    monkeypatch.setattr(mailer, "send", lambda cfg, msg: (_ for _ in ()).throw(AssertionError("sent")))
    for uid, msg in (("ev1@1", "on time"), ("nope", "no longer on your calendar")):
        try:
            late.send(conn, cfg, D, uid, now, live, search)
            raise AssertionError("should refuse")
        except ValueError as e:
            assert msg in str(e)


def test_late_notice_route_needs_position(tmp_path):
    c = make(tmp_path)
    r = c.post("/api/late-notice", json={"uid": "x"})
    assert r.status_code == 400 and r.json["error"]


def test_previous_job_does_not_take_the_next_appointment(tmp_path, monkeypatch):
    """Left the last job 10 min ago, appointment in 5 min: it is still upcoming (and late), not matched to that job."""
    c = make(tmp_path)
    last = c.get(f"/api/day/{D}").json["stops"][-1]
    now = last["depart_ts"] + 600
    ev = {"uid": "e@1", "start_ts": now + 300, "end_ts": now + 3900, "title": "Onsite", "name": "Beta Plumbing",
          "location": "9 Oak Ave", "ticket": ""}
    monkeypatch.setattr(calendar_feed, "events_for_day", lambda cfg, date: [ev])
    import app.service as svc
    monkeypatch.setattr(svc.time, "time", lambda: now)
    cfg = replace(CFG, data_dir=str(tmp_path), calendar_url="https://x/cal.ics", routing_url="")
    conn = db.connect(str(tmp_path))
    cal = svc.day_payload(conn, cfg, D)["calendar"]
    assert cal[0]["state"] == "upcoming" and cal[0]["stop_id"] is None
    far = Candidate(name="9 Oak Ave", lat=last["lat"] + 0.15, lon=last["lon"])
    live = {"lat": last["lat"], "lon": last["lon"], "moving": True, "last_update_s": 30}
    assert late.check(conn, cfg, D, now, live, lambda q, a, b: [far]) is not None
