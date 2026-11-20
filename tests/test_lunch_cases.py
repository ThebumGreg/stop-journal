"""Lunch: (1) a visit, lunch, back to the same site is ONE stop with non-billable lunch time;
(2) lunch between two different jobs: the drives before and after it count as one drive."""
from dataclasses import replace

from app import create_app, sample
from tests.test_detect import CFG

D = "2026-11-18"
HOME, JOB = sample.SITE_A, sample.SITE_B
FOOD = (JOB[0] + 0.02, JOB[1])
JOB2 = (JOB[0] + 0.05, JOB[1])


def build(tmp_path, second):
    d = sample.Day(D)
    t = d.dwell(*JOB, d.at(8), 120)                    # 08:00-10:00 at Acme
    t = d.drive(JOB, FOOD, t, 10)
    t = d.dwell(*FOOD, t, 40)                          # lunch 10:10-10:50
    t = d.drive(FOOD, second, t, 10)
    d.dwell(*second, t, 60)                            # 11:00-12:00
    c = create_app(replace(CFG, data_dir=str(tmp_path)), client=sample.ListRecorder(d.points())).test_client()
    c.post("/api/places", json={"name": "Home", "kind": "home", "lat": HOME[0], "lon": HOME[1]})
    st = c.get(f"/api/day/{D}").json["stops"]
    assert len(st) == 3
    c.patch(f"/api/stops/{st[0]['id']}", json={"business_name": "Acme", "status": "Confirmed", "notes": "Morning work"})
    c.patch(f"/api/stops/{st[1]['id']}", json={"status": "Lunch"})
    c.patch(f"/api/stops/{st[2]['id']}", json={"business_name": "Acme" if second == JOB else "Beta",
                                               "status": "Confirmed", "notes": "Afternoon work"})
    return c


def test_back_to_same_site_is_one_stop_with_lunch_note(tmp_path):
    c = build(tmp_path, JOB)
    d = c.get(f"/api/day/{D}").json
    assert not any(x["counts"] for x in d["drives"])                       # the lunch trip is not work
    assert [(l["from"], l["to"]) for l in d["mileage"]["legs"]] == [("Home", "Acme"), ("Acme", "Home")]
    j = c.get(f"/api/export.json?from={D}").json["days"][0]
    assert len(j["stops"]) == 1
    s = j["stops"][0]
    assert s["arrival"] == "08:00" and s["departure"] == "12:00"
    assert s["lunch_non_billable"][0]["minutes"] == 60                      # 10:00 left, 11:00 back
    assert s["time_on_site_minutes"] == 180                                 # 4 h elapsed minus 1 h lunch trip
    assert "Morning work" in s["work_performed"] and "Afternoon work" in s["work_performed"]
    assert j["summary"]["on_site_minutes"] == 180 and j["summary"]["drive_minutes"] == 0
    assert any(e["reason"] == "Lunch" for e in j["excluded"])
    assert not any(e["departure"] == "12:00" for e in j["excluded"])
    from tests.test_export import pdf_text
    import re
    flat = re.sub(r"\s+", " ", pdf_text(c.get(f"/api/export.pdf?from={D}")))
    assert "Lunch (non-billable) 10:00 to 11:00, 60 min" in flat and "(lunch and breaks not included)" in flat


def test_lunch_between_jobs_is_one_drive(tmp_path):
    c = build(tmp_path, JOB2)
    d = c.get(f"/api/day/{D}").json
    assert d["drives"][0]["counts"] and d["drives"][1]["counts"]
    assert d["totals"]["drive_s"] == sum(x["arrive_ts"] - x["depart_ts"] for x in d["drives"])
    j = c.get(f"/api/export.json?from={D}").json["days"][0]
    assert len(j["stops"]) == 2
    drive = j["stops"][1]["drive_from_previous_stop"]
    assert drive["from"] == "Acme" and drive["minutes"] == sum(x["minutes"] for x in d["drives"])
    assert [(l["from"], l["to"]) for l in j["mileage"]["legs"]][1] == ("Acme", "Beta")   # direct, not via lunch
    from app.geo import haversine_m
    assert abs(drive["miles_approx"] - haversine_m(*JOB, *JOB2) * 1.25 / 1609.344) < 0.2
    assert j["summary"]["drive_minutes"] == drive["minutes"]


def test_lunch_next_to_home_still_not_counted(tmp_path):
    d0 = sample.Day(D)
    t = d0.dwell(*HOME, d0.at(7), 30)
    t = d0.drive(HOME, FOOD, t, 10)
    t = d0.dwell(*FOOD, t, 40)
    t = d0.drive(FOOD, JOB, t, 10)
    d0.dwell(*JOB, t, 60)
    c = create_app(replace(CFG, data_dir=str(tmp_path)), client=sample.ListRecorder(d0.points())).test_client()
    st = c.get(f"/api/day/{D}").json["stops"]
    c.patch(f"/api/stops/{st[0]['id']}", json={"business_name": "Home", "status": "Personal"})
    c.patch(f"/api/stops/{st[1]['id']}", json={"status": "Lunch"})
    c.patch(f"/api/stops/{st[2]['id']}", json={"business_name": "Acme", "status": "Confirmed"})
    assert not any(x["counts"] for x in c.get(f"/api/day/{D}").json["drives"])


def test_lunch_is_paid_in_the_timesheet(tmp_path):
    c = build(tmp_path, JOB2)
    j = c.get(f"/api/export.json?from={D}").json["days"][0]
    sheet = j["timesheet"]
    lunch = [r for r in sheet if r["type"] == "lunch"]
    assert len(lunch) == 1 and lunch[0]["paid"] and lunch[0]["minutes"] == 40
    sm = j["summary"]
    assert sm["lunch_minutes"] == 40
    assert sm["paid_minutes"] == sm["on_site_minutes"] + sm["drive_minutes"] + 40
    assert sum(r["minutes"] for r in sheet if r["paid"]) == sm["paid_minutes"]
    from tests.test_export import pdf_text
    import re
    flat = re.sub(r"\s+", " ", pdf_text(c.get(f"/api/export.pdf?from={D}")))
    assert "Lunch (paid, not billed)" in flat and "Lunch (paid, not billed) 40 min" in flat and "(40 min)" not in flat


def test_same_site_lunch_gap_is_paid_but_not_billable(tmp_path):
    c = build(tmp_path, JOB)
    j = c.get(f"/api/export.json?from={D}").json["days"][0]
    assert j["summary"]["on_site_minutes"] == 180 and j["summary"]["lunch_minutes"] == 60
    assert j["summary"]["paid_minutes"] == 240                       # 3 h on site + the 1 h lunch gap


def test_personal_break_is_a_clock_out(tmp_path):
    c = build(tmp_path, JOB)
    st = c.get(f"/api/day/{D}").json["stops"]
    c.patch(f"/api/stops/{st[1]['id']}", json={"status": "Personal"})          # same break, now personal
    d = c.get(f"/api/day/{D}").json
    assert not any(x["counts"] for x in d["drives"])
    j = c.get(f"/api/export.json?from={D}").json["days"][0]
    assert len(j["stops"]) == 1 and j["stops"][0]["personal_breaks_unpaid"][0]["minutes"] == 60
    assert j["stops"][0]["lunch_non_billable"] == []
    brk = [r for r in j["timesheet"] if r["type"] == "break"]
    assert len(brk) == 1 and not brk[0]["paid"] and brk[0]["minutes"] == 60
    assert j["summary"]["paid_minutes"] == 180 and j["summary"]["break_minutes_unpaid"] == 60
    from tests.test_export import pdf_text
    import re
    flat = re.sub(r"\s+", " ", pdf_text(c.get(f"/api/export.pdf?from={D}")))
    assert "Clocked out (personal break) (1 h 00 min)" in flat and "Personal break (unpaid) 10:00 to 11:00, 60 min" in flat


def test_home_before_and_after_the_day_is_not_a_break(tmp_path):
    c, st = None, None
    from tests.test_stop_marks import three_stops
    c, st = three_stops(tmp_path)
    c.patch(f"/api/stops/{st[0]['id']}", json={"status": "Personal"})
    c.patch(f"/api/stops/{st[1]['id']}", json={"business_name": "Acme", "status": "Confirmed"})
    c.patch(f"/api/stops/{st[2]['id']}", json={"status": "Personal"})
    j = c.get(f"/api/export.json?from={D}").json["days"][0]
    assert not [r for r in j["timesheet"] if r["type"] in ("break", "lunch")]
