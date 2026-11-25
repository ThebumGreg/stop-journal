"""Email report: sent only on request, through SMTP, JSON + PDF attached, versioned subjects, coordinates optional."""
import json

import pytest

from app import db, mailer
from tests.test_commute import D, make

SENT = []


class FakeSMTP:
    fail_login = False

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        self.tls = True

    def login(self, user, pw):
        import smtplib
        if FakeSMTP.fail_login:
            raise smtplib.SMTPAuthenticationError(535, b"bad")
        self.login_as = (user, pw)

    def send_message(self, msg):
        SENT.append((self, msg))


@pytest.fixture(autouse=True)
def fake_smtp(monkeypatch):
    SENT.clear()
    FakeSMTP.fail_login = False
    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)


def setup_mail(tmp_path, **extra):
    conn = db.connect(str(tmp_path))
    vals = {"mail_to": "me@work.example", "smtp_user": "me@gmail.example", "smtp_password": "abcd efgh ijkl mnop", **extra}
    for k, v in vals.items():
        conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)", (k, v))
    conn.commit(); conn.close()


def atts(msg):
    return {p.get_filename(): p.get_content() for p in msg.iter_attachments()}


def test_email_report_sends_json_and_pdf_and_logs(tmp_path):
    c = make(tmp_path)
    assert c.get(f"/api/export/emails?from={D}").json["ready"] is False
    r = c.post(f"/api/export/email?from={D}", json={"pdf": True, "json": True})
    assert r.status_code == 400 and "Settings → Work AI agent" in r.json["error"]
    setup_mail(tmp_path)
    info = c.get(f"/api/export/emails?from={D}").json
    assert info["ready"] and info["to"] == "me@work.example" and info["history"] == []
    r = c.post(f"/api/export/email?from={D}", json={"pdf": True, "json": True})
    assert r.status_code == 200, r.json
    server, msg = SENT[0]
    assert server.host == "smtp.gmail.com" and server.port == 587 and server.tls
    assert server.login_as == ("me@gmail.example", "abcdefghijklmnop")      # Gmail App password spaces dropped
    assert msg["To"] == "me@work.example" and msg["Subject"] == f"JobHub Daily Summary {D}"
    a = atts(msg)
    name = f"jobhub-daily-summary-{D}"
    assert set(a) == {name + ".json", name + ".pdf"}
    rep = json.loads(a[name + ".json"])
    assert rep["days"][0]["stops"][0]["business_name"] == "Acme" and "latitude" in rep["days"][0]["stops"][0]
    assert a[name + ".pdf"].startswith(b"%PDF")
    body = msg.get_body(("plain",)).get_content()
    assert f"Dates: {D}" in body and "Version: 1" in body and "Confirmed stops: 1" in body and "Acme" in body
    # a second send is an update; history lists both
    r = c.post(f"/api/export/email?from={D}&omit=coordinates", json={"json": True})
    msg = SENT[1][1]
    assert msg["Subject"] == f"JobHub Daily Summary {D} (update 2)" and "replaces the earlier email" in msg.get_body(("plain",)).get_content()
    rep = json.loads(atts(msg)[f"jobhub-daily-summary-{D}.json"])
    assert "latitude" not in rep["days"][0]["stops"][0] and rep["omitted"][0]["key"] == "coordinates"
    assert len(atts(msg)) == 1
    h = c.get(f"/api/export/emails?from={D}").json["history"]
    assert [x["version"] for x in h] == [2, 1] and all(x["ok"] for x in h)


def test_email_needs_an_attachment_and_reports_login_errors(tmp_path):
    c = make(tmp_path)
    setup_mail(tmp_path)
    r = c.post(f"/api/export/email?from={D}", json={})
    assert r.status_code == 400 and "Tick the PDF or the JSON" in r.json["error"]
    FakeSMTP.fail_login = True
    r = c.post(f"/api/export/email?from={D}", json={"json": True})
    assert r.status_code == 400 and "App password" in r.json["error"]
    h = c.get(f"/api/export/emails?from={D}").json["history"]
    assert h[0]["ok"] == 0 and "App password" in h[0]["error"]
    FakeSMTP.fail_login = False                       # a failed send does not use up a version number
    c.post(f"/api/export/email?from={D}", json={"json": True})
    assert SENT[-1][1]["Subject"] == f"JobHub Daily Summary {D}"


def test_email_blocked_while_drafts_unless_anyway(tmp_path):
    c = make(tmp_path)
    setup_mail(tmp_path)
    job = next(s for s in c.get(f"/api/day/{D}").json["stops"] if s["business_name"] == "Acme")
    c.patch(f"/api/stops/{job['id']}", json={"status": "Draft"})
    r = c.post(f"/api/export/email?from={D}", json={"json": True})
    assert "not confirmed" in r.json["error"] and not SENT
    r = c.post(f"/api/export/email?from={D}", json={"json": True, "anyway": True})
    assert r.status_code == 200 and len(SENT) == 1


def test_coordinates_can_be_left_out_of_the_pdf(tmp_path):
    c = make(tmp_path)
    assert c.get(f"/api/export.pdf?from={D}&omit=coordinates").status_code == 200
    j = c.get(f"/api/export.json?from={D}&omit=coordinates").json
    assert "latitude" not in j["days"][0]["stops"][0]


def test_settings_test_email(tmp_path):
    c = make(tmp_path)
    r = c.post("/settings/test-email", data={"mail_to": "me@work.example", "smtp_user": "me@gmail.example",
                                             "smtp_password": "pw"}).json
    assert r["ok"] and SENT[0][1]["Subject"] == "JobHub test email"
    r = c.post("/settings/test-email", data={}).json
    assert not r["ok"]


def test_signature_line_matches_the_attached_json(tmp_path):
    import hashlib
    import hmac
    c = make(tmp_path)
    setup_mail(tmp_path, report_signing_key="correct horse battery staple")
    c.post(f"/api/export/email?from={D}", json={"json": True})
    msg = SENT[0][1]
    body = msg.get_body(("plain",)).get_content()
    line = next(l for l in body.splitlines() if l.startswith("Signature: hmac-sha256="))
    data = next(p for p in msg.iter_attachments() if p.get_filename().endswith(".json")).get_payload(decode=True)
    want = hmac.new(b"correct horse battery staple", data, hashlib.sha256).hexdigest()
    assert line.split("=", 1)[1].split()[0] == want
    # no key: no signature line
    SENT.clear()
    from app import db
    conn = db.connect(str(tmp_path)); conn.execute("DELETE FROM settings WHERE key='report_signing_key'"); conn.commit(); conn.close()
    c.post(f"/api/export/email?from={D}", json={"json": True})
    assert "Signature:" not in SENT[0][1].get_body(("plain",)).get_content()
