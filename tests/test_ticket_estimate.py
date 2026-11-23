"""HubSpot ticket # and QuickBooks estimate # are two fields; the report carries both, with warnings."""
from tests.test_commute import D, make


def job(c):
    return next(s for s in c.get(f"/api/day/{D}").json["stops"] if s["business_name"] == "Acme")


def warns(c):
    return c.get(f"/api/export/check?from={D}").json["warnings"]


def test_both_fields_in_report_and_warnings(tmp_path):
    c = make(tmp_path)
    j = job(c)
    c.patch(f"/api/stops/{j['id']}", json={"work_category": "Quoted project"})
    w = warns(c)
    assert not any("ticket" in x.lower() for x in w) and any("no QuickBooks estimate #" in x for x in w)   # blank ticket is normal
    c.patch(f"/api/stops/{j['id']}", json={"ticket": "#1042", "qbo_estimate": " #2210 "})
    assert job(c)["qbo_estimate"] == "2210"
    r = c.get(f"/api/export.json?from={D}").json
    s = r["days"][0]["stops"][0]
    assert s["hubspot_ticket"] == "1042" and s["qbo_estimate"] == "2210" and s["job_reference"] == "1042"
    assert r["records_to_update"][0]["hubspot_ticket"] == "1042" and r["records_to_update"][0]["qbo_estimate"] == "2210"
    assert not any("ticket" in x.lower() or "estimate" in x.lower() for x in warns(c))
    c.patch(f"/api/stops/{j['id']}", json={"ticket": "PRJ-1"})
    assert any("does not look like a HubSpot ticket" in x for x in warns(c))
    assert c.get(f"/api/export.pdf?from={D}").status_code == 200


def test_time_and_material_needs_no_estimate(tmp_path):
    c = make(tmp_path)
    j = job(c)
    c.patch(f"/api/stops/{j['id']}", json={"work_category": "Time & Material", "ticket": "1042"})
    assert not any("estimate" in x for x in warns(c))


def test_calendar_estimate_line():
    from app.calendar_feed import labelled
    assert labelled("Ticket: 1042\nEstimate: #2210\n")["estimate"] == "#2210"
    assert labelled("Quote: 77")["estimate"] == "77"
