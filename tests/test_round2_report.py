"""Round 2: billable on-site time, photo attachment names, fuller records_to_update (on the sample day)."""
import importlib.util
import json
import os

import pytest

from app import db, mailer

D = "2026-11-18"


@pytest.fixture
def sample(tmp_path):
    path = os.path.join(os.path.dirname(__file__), "..", "scripts", "make_sample_report.py")
    spec = importlib.util.spec_from_file_location("mk", path)
    mk = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mk)
    c, cfg = mk.build(str(tmp_path))
    return c


def test_billable_on_site_minutes(sample):
    day = sample.get(f"/api/export.json?from={D}").json["days"][0]
    sm = day["summary"]
    assert sm["billable_on_site_minutes"] == sum(r["on_site_minutes"] for r in sm["hours_by_category"]
                                                 if r["billing"] != "non_billable")
    assert sm["billable_on_site_minutes"] < sm["on_site_minutes"]           # the supply-house stop is not billable
    assert sample.get(f"/api/export.pdf?from={D}").status_code == 200


def test_photo_attachments_match_the_email(sample, tmp_path, monkeypatch):
    sent = []

    class Fake:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self, **k): pass
        def login(self, *a): pass
        def send_message(self, m): sent.append(m)

    monkeypatch.setattr(mailer.smtplib, "SMTP", Fake)
    conn = db.connect(str(tmp_path))
    for k, v in {"mail_to": "a@work.example", "smtp_user": "t@gmail.example", "smtp_password": "x"}.items():
        conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)", (k, v))
    conn.commit(); conn.close()
    assert sample.post(f"/api/export/email?from={D}", json={"json": True, "photos": True}).status_code == 200
    names = {p.get_filename() for p in sent[0].iter_attachments()}
    rep = json.loads(next(p for p in sent[0].iter_attachments() if p.get_filename().endswith(".json")).get_content())
    photos = rep["days"][0]["photos"]
    assert photos and all(p["attachment"] in names for p in photos)
    rec = rep["records_to_update"][0]
    assert rec["photo_attachments"] and set(rec["photo_attachments"]) <= names


def test_records_to_update_has_everything(sample):
    r = sample.get(f"/api/export.json?from={D}").json
    rec = {x["business_name"]: x for x in r["records_to_update"]}
    d = rec["Sample Dental Office"]
    assert d["billing"] == "hourly" and d["visit_result"] == "Complete" and d["time_on_site_minutes"] == 150
    assert d["technician_email"] == "tech@work.example"
    assert d["consumables_used"] == ["1 x Zip ties 8 in"] and d["expenses"] == ["Parking $12.00"]
    assert rec["Sample Supply House"]["billing"] == "non_billable"
