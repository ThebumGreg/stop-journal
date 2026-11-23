"""Follow-up owners: names with emails in Settings; the report gives the agent each owner's email."""
import pytest

from app import db, settings
from tests.test_commute import D, make


def set_kv(tmp_path, **kv):
    conn = db.connect(str(tmp_path))
    for k, v in kv.items():
        conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)", (k, v))
    conn.commit(); conn.close()


def test_parse_owners():
    assert settings.parse_owners("Me\nOffice = office@x.com\n") == [("Me", ""), ("Office", "office@x.com")]
    for bad in ("Office", "Office = nope", "Me\nme", " = a@b.co"):
        with pytest.raises(ValueError):
            settings.parse_owners(bad)


def test_owner_email_in_report_and_warnings(tmp_path):
    c = make(tmp_path)
    set_kv(tmp_path, tech_email="pat@work.example", followup_owners="Me\nOffice = office@work.example")
    day = c.get(f"/api/day/{D}").json
    assert day["followup_owners"] == ["Me", "Office"]
    job = next(s for s in day["stops"] if s["business_name"] == "Acme")
    for owner in ("Me", "office", "Bob"):
        c.post(f"/api/stops/{job['id']}/follow-ups", json={"text": "Task " + owner, "owner": owner, "due": "2026-11-20"})
    r = c.get(f"/api/export.json?from={D}").json
    emails = {f["owner"]: f["owner_email"] for f in r["follow_ups"]}
    assert emails == {"Me": "pat@work.example", "office": "office@work.example", "Bob": ""}
    assert {f["owner"]: f["owner_email"] for f in r["days"][0]["stops"][0]["follow_ups"]} == emails
    w = c.get(f"/api/export/check?from={D}").json["warnings"]
    assert any("owner “Bob” is not in Settings" in x for x in w)
    assert not any("Task Me" in x or "Task office" in x for x in w)


def test_me_without_technician_email_warns(tmp_path):
    c = make(tmp_path)
    job = next(s for s in c.get(f"/api/day/{D}").json["stops"] if s["business_name"] == "Acme")
    c.post(f"/api/stops/{job['id']}/follow-ups", json={"text": "Call", "owner": "Me", "due": "2026-11-20"})
    w = c.get(f"/api/export/check?from={D}").json["warnings"]
    assert any("“Me” has no email" in x for x in w)
