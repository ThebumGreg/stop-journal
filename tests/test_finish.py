"""Finish stop: the end of a stop. Checks what the type needs, saves the visit result, confirms it,
and leaves the departure to the GPS so you can finish before you drive away."""
from tests.test_export import D, make
from tests.test_growing import more, setup, stops as grow_stops


def stops(c):
    return c.get(f"/api/day/{D}").json["stops"]


def test_needs_list_follows_the_type(tmp_path):
    c = make(tmp_path)
    s = stops(c)[0]
    assert s["needs"] == ["stop type"]
    s = c.patch(f"/api/stops/{s['id']}", json={"stop_type": "job"}).json["stops"][0]
    assert s["needs"] == ["business name", "category", "work performed", "visit result"]
    s = c.patch(f"/api/stops/{s['id']}", json={"stop_type": "vendor"}).json["stops"][0]
    assert s["needs"] == ["vendor name"]
    s = c.patch(f"/api/stops/{s['id']}", json={"stop_type": "gas"}).json["stops"][0]
    assert s["needs"] == ["total paid"]
    s = c.patch(f"/api/stops/{s['id']}", json={"stop_type": "lunch"}).json["stops"][0]
    assert s["needs"] == []


def test_finish_refuses_until_complete_then_confirms(tmp_path):
    c = make(tmp_path)
    sid = stops(c)[0]["id"]
    c.patch(f"/api/stops/{sid}", json={"stop_type": "job", "business_name": "Acme"})
    r = c.post(f"/api/stops/{sid}/finish", json={})
    assert r.status_code == 400 and "category, work performed, visit result" in r.json["error"]
    c.patch(f"/api/stops/{sid}", json={"work_category": "Time & Material", "notes": "Fixed the valve"})
    r = c.post(f"/api/stops/{sid}/finish", json={"visit_result": "complete"})
    assert r.status_code == 200, r.json
    s = r.json["stops"][0]
    assert s["status"] == "Confirmed" and s["visit_result"] == "complete" and s["needs"] == [] and s["follow_ups"] == []


def test_return_visit_adds_one_follow_up(tmp_path):
    c = make(tmp_path)
    sid = stops(c)[0]["id"]
    c.patch(f"/api/stops/{sid}", json={"stop_type": "job", "business_name": "Acme", "notes": "x",
                                       "work_category": "Time & Material"})
    c.post(f"/api/stops/{sid}/finish", json={"visit_result": "return", "due": "2026-11-25"})
    c.patch(f"/api/stops/{sid}", json={"status": "Draft"})
    c.post(f"/api/stops/{sid}/finish", json={"visit_result": "return"})     # finishing again: no duplicate
    f = stops(c)[0]["follow_ups"]
    assert [(x["text"], x["owner"], x["due"]) for x in f] == [("Return visit", "Me", "2026-11-25")]


def test_finished_stop_keeps_growing_until_you_drive_away(tmp_path):
    c, rec, d = setup(tmp_path)
    sid = grow_stops(c)[0]["id"]
    c.patch(f"/api/stops/{sid}", json={"stop_type": "job", "business_name": "Acme", "notes": "Fixed it",
                                       "work_category": "Time & Material"})
    assert c.post(f"/api/stops/{sid}/finish", json={"visit_result": "complete"}).status_code == 200
    before = grow_stops(c)[0]["depart_ts"]
    more(rec, d, 31, 90)                      # still on site for another hour, then the next sync
    after = c.post(f"/api/day/{D}/sync").json["stops"][0]
    assert after["depart_ts"] > before + 50 * 60
    assert (after["status"], after["notes"], after["visit_result"]) == ("Confirmed", "Fixed it", "complete")


def test_finish_gas_needs_a_total_and_keeps_its_status(tmp_path):
    c = make(tmp_path)
    sid = stops(c)[0]["id"]
    c.patch(f"/api/stops/{sid}", json={"stop_type": "gas"})
    assert c.post(f"/api/stops/{sid}/finish", json={}).status_code == 400
    c.put(f"/api/stops/{sid}/fuel", json={"total": 40})
    s = c.post(f"/api/stops/{sid}/finish", json={}).json["stops"][0]
    assert s["status"] == "Traffic" and s["needs"] == []
