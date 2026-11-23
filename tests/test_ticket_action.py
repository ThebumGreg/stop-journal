"""C1: the HubSpot ticket is optional; a pasted link keeps the ID; the visit choice tells the agent what to do."""
from app.service import hubspot_ticket_id
from tests.test_commute import D, make


def job(c):
    return next(s for s in c.get(f"/api/day/{D}").json["stops"] if s["business_name"] == "Acme")


def rstop(c):
    return c.get(f"/api/export.json?from={D}").json["days"][0]["stops"][0]


def test_ticket_links_become_ids():
    assert hubspot_ticket_id("https://app.hubspot.com/contacts/123456/ticket/987654321") == "987654321"
    assert hubspot_ticket_id("https://app.hubspot.com/contacts/123456/record/0-5/24681357913/view/1") == "24681357913"
    assert hubspot_ticket_id("https://app.hubspot.com/help-desk/123456/view/1/ticket/55512345678?x=1") == "55512345678"
    assert hubspot_ticket_id(" #1042 ") == "1042" and hubspot_ticket_id("") == ""


def test_ticket_action_in_report(tmp_path):
    c = make(tmp_path)
    j = job(c)
    s = rstop(c)
    assert s["hubspot_ticket"] == "" and s["ticket_action"] == "find"
    assert not any("ticket" in w.lower() for w in c.get(f"/api/export/check?from={D}").json["warnings"])
    for choice, action in (("continue", "continue"), ("new", "create"), ("unsure", "find")):
        assert c.patch(f"/api/stops/{j['id']}", json={"ticket_action": choice}).status_code == 200
        assert rstop(c)["ticket_action"] == action
    assert c.patch(f"/api/stops/{j['id']}", json={"ticket_action": "maybe"}).status_code == 400
    c.patch(f"/api/stops/{j['id']}", json={"ticket": "https://app.hubspot.com/contacts/1/ticket/24681357913"})
    assert job(c)["ticket"] == "24681357913"
    s = rstop(c)
    assert s["hubspot_ticket"] == "24681357913" and s["ticket_action"] == "use"
    assert c.get(f"/api/export.json?from={D}").json["records_to_update"][0]["ticket_action"] == "use"


def test_return_visit_defaults_to_continue(tmp_path):
    c = make(tmp_path)
    j = job(c)
    c.patch(f"/api/stops/{j['id']}", json={"visit_result": "return"})
    # a second visit to Acme the same day, later
    c.post(f"/api/day/{D}/stops", json={"arrive": "13:00", "depart": "13:30", "business_name": "Acme"})
    second = [s for s in c.get(f"/api/day/{D}").json["stops"] if s["business_name"] == "Acme"][-1]
    assert second["ticket_action_default"] == "continue"
    from app.report import _ticket_action
    assert _ticket_action(second) == "continue"                     # what the report says for that stop
    assert _ticket_action(dict(second, ticket_action="new")) == "create"   # your choice wins


def test_calendar_ticket_line_with_a_link():
    from app.calendar_feed import _ticket, labelled
    assert _ticket(labelled("Ticket: https://app.hubspot.com/contacts/9/ticket/12345678")["ticket"]) == "12345678"
