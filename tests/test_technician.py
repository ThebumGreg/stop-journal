from app import db
from tests.test_commute import D, make
from tests.test_email import SENT, fake_smtp, setup_mail  # noqa: F401


def set_tech(tmp_path, **kv):
    conn = db.connect(str(tmp_path))
    for k, v in kv.items():
        conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)", (k, v))
    conn.commit(); conn.close()


def test_technician_in_report_email_and_warning(tmp_path):
    c = make(tmp_path)
    r = c.get(f"/api/export.json?from={D}").json
    assert r["technician"] == {"name": "", "email": ""}
    w = c.get(f"/api/export/check?from={D}").json["warnings"]
    assert any(x.startswith("Technician name and email are not set") for x in w)
    set_tech(tmp_path, tech_name="Pat Tech", tech_email="pat@work.example")
    r = c.get(f"/api/export.json?from={D}").json
    assert r["technician"] == {"name": "Pat Tech", "email": "pat@work.example"}
    assert not any("Technician" in x for x in c.get(f"/api/export/check?from={D}").json["warnings"])
    assert c.get(f"/api/export.pdf?from={D}").status_code == 200
    setup_mail(tmp_path)
    c.post(f"/api/export/email?from={D}", json={"json": True})
    body = SENT[0][1].get_body(("plain",)).get_content().splitlines()
    assert body[1] == f"Dates: {D}" and body[2] == "Technician: Pat Tech <pat@work.example>"


def test_only_email_missing(tmp_path):
    c = make(tmp_path)
    set_tech(tmp_path, tech_name="Pat Tech")
    w = c.get(f"/api/export/check?from={D}").json["warnings"]
    assert any(x.startswith("Technician email is not set") for x in w)
