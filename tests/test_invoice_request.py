"""Send for invoice: one finished Time & Material stop to the work agent (only on a tap)."""
import importlib.util
import json
import os

import pytest

from app import db, invoice_request, mailer

D = "2026-11-18"


@pytest.fixture
def sample(tmp_path, monkeypatch):
    path = os.path.join(os.path.dirname(__file__), "..", "scripts", "make_sample_report.py")
    spec = importlib.util.spec_from_file_location("mk", path)
    mk = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mk)
    c, cfg = mk.build(str(tmp_path))
    conn = db.connect(str(tmp_path))
    for k, v in {"mail_to": "agent@work.example", "smtp_user": "t@gmail.example", "smtp_password": "x",
                 "report_signing_key": "shared phrase", "tech_name": "Sample Tech", "tech_email": "tech@work.example"}.items():
        conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)", (k, v))
    conn.commit(); conn.close()
    sent = []
    monkeypatch.setattr(mailer, "send", lambda cfg, msg: sent.append(msg))
    return c, sent


def stops(c):
    return c.get(f"/api/day/{D}").json["stops"]


def tm_stop(c):
    return next(s for s in stops(c) if s.get("work_category") == "Time & Material")


def test_preview_has_labor_parts_and_no_send(sample):
    c, sent = sample
    s = tm_stop(c)
    r = c.get(f"/api/stops/{s['id']}/invoice-request")
    assert r.status_code == 200, r.json
    req = r.json["request"]
    assert req["type"] == "jobhub_invoice_request" and req["invoice_requested"] is True
    assert req["customer"]["business_name"] == s["business_name"] and req["stop_uid"] == s["uid"]
    assert req["hubspot_ticket"] == "24681357913" and req["category"] == "Time & Material"
    assert req["labor"]["time_on_site_minutes"] > 0 and req["labor"]["still_on_site"] is False      # a past day
    assert req["labor"]["billed_through"] == s["depart"]
    assert req["parts"] and all(p["billable"] for p in req["parts"])
    assert all(not p["billable"] for p in req["parts_not_billed"])
    assert sent == []                                                       # a preview never sends


def test_send_email_json_pdf_signature_and_versions(sample):
    c, sent = sample
    s = tm_stop(c)
    r = c.post(f"/api/stops/{s['id']}/invoice-request", json={"pdf": True, "photos": True})
    assert r.status_code == 200, r.json
    m = sent[0]
    assert m["To"] == "agent@work.example"
    assert m["Subject"] == f"JobHub Invoice Request {D} – {s['business_name']}"
    files = {p.get_filename(): p for p in m.iter_attachments()}
    js = next(n for n in files if n.endswith(".json"))
    assert js.startswith(f"jobhub-invoice-request-{D}-stop-") and any(n.endswith(".pdf") for n in files)
    data = files[js].get_content()
    data = data if isinstance(data, bytes) else data.encode()
    req = json.loads(data)
    assert req["request_version"] == 1 and req["technician"]["email"] == "tech@work.example"
    assert all(p["attachment"] in files for p in req["photos"]) and req["photos"]
    body = m.get_body(preferencelist=("plain",)).get_content()
    assert "Signature: hmac-sha256=" + mailer.signature("shared phrase", data) in body
    assert "Labor: " in body and "Stop ID: " + s["uid"] in body
    # the stop and the day report now say it was sent
    s2 = next(x for x in r.json["stops"] if x["id"] == s["id"])
    assert s2["invoice_request"]["version"] == 1
    rep = c.get(f"/api/export.json?from={D}").json
    st = next(x for x in rep["days"][0]["stops"] if x["stop_uid"] == s["uid"])
    assert st["invoice_request"]["version"] == 1 and st["invoice_request"]["time_on_site_minutes"] == req["labor"]["time_on_site_minutes"]
    assert all(x["invoice_request"] is None for x in rep["days"][0]["stops"] if x["stop_uid"] != s["uid"])
    # again: update 2
    c.post(f"/api/stops/{s['id']}/invoice-request", json={})
    assert sent[1]["Subject"].endswith("(update 2)")
    assert [p.get_filename().endswith(".json") for p in sent[1].iter_attachments()] == [True]   # JSON only


def test_only_finished_time_and_material(sample):
    c, sent = sample
    quoted = next(s for s in stops(c) if s.get("work_category") == "Quoted project")
    r = c.post(f"/api/stops/{quoted['id']}/invoice-request", json={})
    assert r.status_code == 400 and "Time & Material" in r.json["error"]
    s = tm_stop(c)
    c.patch(f"/api/stops/{s['id']}", json={"status": "Draft"})
    r = c.get(f"/api/stops/{s['id']}/invoice-request")
    assert r.status_code == 400 and "Finish the stop" in r.json["error"]
    assert sent == []


def test_still_on_site_bills_through_now(tmp_path, monkeypatch):
    """Today's latest stop whose departure follows the GPS: labor runs to the moment of sending."""
    from dataclasses import replace
    from tests.test_detect import CFG
    from tests.test_export import make
    c = make(tmp_path)
    s = c.get(f"/api/day/{D}").json["stops"][-1]
    c.patch(f"/api/stops/{s['id']}", json={"stop_type": "job", "business_name": "Beta", "work_category": "Time & Material",
                                          "notes": "Fixed it."})
    c.post(f"/api/stops/{s['id']}/finish", json={"visit_result": "complete"})
    conn = db.connect(str(tmp_path))
    cfg = replace(CFG, data_dir=str(tmp_path))
    import app.service as svc
    now = s["depart_ts"] + 5 * 60                       # same day, the last point 5 min ago, still inside the stop
    monkeypatch.setattr(svc.time, "time", lambda: now)
    req, warn, _ = invoice_request.build(conn, cfg, s["id"], now)
    assert req["labor"]["still_on_site"] is True
    full = req["labor"]["time_on_site_minutes"]
    later = s["depart_ts"] + 40 * 60                    # no point for 40 min: not "here now", so through the departure
    monkeypatch.setattr(svc.time, "time", lambda: later)
    req2, _, _ = invoice_request.build(conn, cfg, s["id"], later)
    assert req2["labor"]["still_on_site"] is False and full == req2["labor"]["time_on_site_minutes"] + 5
    assert any("company ID" in w for w in warn)
