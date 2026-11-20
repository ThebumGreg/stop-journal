"""The commute counts only on days you tap Start day / End day around it."""
from dataclasses import replace

from app import create_app, sample
from tests.test_detect import CFG

D = "2026-11-18"
HOME, JOB = sample.SITE_A, sample.SITE_B


def make(tmp_path):
    d = sample.Day(D)
    t = d.dwell(*HOME, d.at(7), 60)                 # 07:00-08:00 at home
    t = d.drive(HOME, JOB, t, 20)                    # commute 08:00-08:20
    t = d.dwell(*JOB, t, 120)                        # job 08:20-10:20
    t = d.drive(JOB, HOME, t, 20)                    # drive home 10:20-10:40
    d.dwell(*HOME, t, 60)                            # home again
    c = create_app(replace(CFG, data_dir=str(tmp_path)),
                   client=sample.ListRecorder(d.points())).test_client()
    day = c.get(f"/api/day/{D}").json
    home1, job, home2 = day["stops"]
    c.patch(f"/api/stops/{home1['id']}", json={"business_name": "Home", "status": "Personal"})
    c.patch(f"/api/stops/{home2['id']}", json={"business_name": "Home", "status": "Personal"})
    c.patch(f"/api/stops/{job['id']}", json={"business_name": "Acme", "status": "Confirmed"})
    return c


def day(c):
    return c.get(f"/api/day/{D}").json


def test_commute_not_counted_without_marks(tmp_path):
    c = make(tmp_path)
    d = day(c)
    assert d["totals"]["drive_s"] == 0 and all(x["counts"] is False for x in d["drives"])


def test_start_and_end_day_count_the_commutes(tmp_path):
    c = make(tmp_path)
    c.post(f"/api/day/{D}/mark", json={"which": "start", "time": "07:55"})
    d = day(c)
    assert d["drives"][0]["counts"] and not d["drives"][1]["counts"]
    first = d["totals"]["drive_s"]
    assert 15 * 60 <= first <= 21 * 60
    c.post(f"/api/day/{D}/mark", json={"which": "end", "time": "10:30"})     # ended on the way home
    assert not day(c)["drives"][1]["counts"]
    c.post(f"/api/day/{D}/mark", json={"which": "end", "time": "10:45"})     # ended at home: drive home counts
    d = day(c)
    assert d["drives"][1]["counts"] and d["totals"]["drive_s"] > first + 15 * 60


def test_starting_after_the_commute_does_not_count_it(tmp_path):
    c = make(tmp_path)
    c.post(f"/api/day/{D}/mark", json={"which": "start", "time": "08:25"})   # tapped at the job site
    assert not day(c)["drives"][0]["counts"]
    c.post(f"/api/day/{D}/mark", json={"which": "end", "time": "10:15"})     # ended at the job site
    assert not day(c)["drives"][1]["counts"]


def test_report_includes_counted_commutes(tmp_path):
    c = make(tmp_path)
    c.post(f"/api/day/{D}/mark", json={"which": "start", "time": "07:55"})
    c.post(f"/api/day/{D}/mark", json={"which": "end", "time": "10:45"})
    j = c.get(f"/api/export.json?from={D}").json["days"][0]
    assert j["stops"][0]["drive_from_previous_stop"]["minutes"] >= 15
    assert j["drive_after_last_stop"]["minutes"] >= 15
    assert j["summary"]["drive_minutes"] >= 30
    assert c.get(f"/api/export.pdf?from={D}").status_code == 200


def test_midday_personal_stop_never_counts(tmp_path):
    d = sample.Day(D)
    t = d.dwell(*HOME, d.at(7), 30)
    t = d.drive(HOME, JOB, t, 20)
    t = d.dwell(*JOB, t, 60)
    t = d.drive(JOB, HOME, t, 20)
    t = d.dwell(*HOME, t, 40)                         # lunch at home
    t = d.drive(HOME, JOB, t, 20)
    d.dwell(*JOB, t, 60)
    c = create_app(replace(CFG, data_dir=str(tmp_path)),
                   client=sample.ListRecorder(d.points())).test_client()
    st = c.get(f"/api/day/{D}").json["stops"]
    for s in st:
        c.patch(f"/api/stops/{s['id']}", json={"business_name": "X", "status": "Personal" if s["seq"] in (1, 3) else "Confirmed"})
    c.post(f"/api/day/{D}/mark", json={"which": "start", "time": "06:50"})
    c.post(f"/api/day/{D}/mark", json={"which": "end", "time": "23:00"})
    drives = c.get(f"/api/day/{D}").json["drives"]
    assert [x["counts"] for x in drives] == [True, False, False]


def test_end_at_job_documents_miles_home_but_does_not_count_them(tmp_path):
    c = make(tmp_path)
    c.post("/api/places", json={"name": "Home", "kind": "home", "lat": HOME[0], "lon": HOME[1]})
    c.post(f"/api/day/{D}/mark", json={"which": "end", "time": "10:15"})
    d = day(c)
    e = d["end_day"]
    assert e["kind"] == "job" and not e["counted"]
    assert e["miles_to_home"] and e["miles_to_home"] > 2
    assert not d["drives"][1]["counts"] and d["totals"]["drive_s"] == 0
    j = c.get(f"/api/export.json?from={D}").json["days"][0]
    assert j["end_day"]["miles_to_home"] == e["miles_to_home"] and j["drive_after_last_stop"] is None
    assert c.get(f"/api/export.pdf?from={D}").status_code == 200


def test_end_at_home_counts_drive_and_miles(tmp_path):
    c = make(tmp_path)
    c.post("/api/places", json={"name": "Home", "kind": "home", "lat": HOME[0], "lon": HOME[1]})
    c.post(f"/api/day/{D}/mark", json={"which": "end", "time": "10:45"})
    d = day(c)
    assert d["end_day"]["kind"] == "home" and d["end_day"]["counted"] and d["end_day"]["miles_to_home"] > 2
    assert d["drives"][1]["counts"]


def test_end_at_personal_stop_counts_the_drive_to_it_not_the_one_after(tmp_path):
    d0 = sample.Day(D)
    t = d0.dwell(*JOB, d0.at(8), 120)                 # job 08:00-10:00
    t = d0.drive(JOB, (HOME[0] + 0.02, HOME[1]), t, 15)
    t = d0.dwell(HOME[0] + 0.02, HOME[1], t, 30)      # errand 10:15-10:45
    t = d0.drive((HOME[0] + 0.02, HOME[1]), HOME, t, 10)
    d0.dwell(*HOME, t, 60)
    c = create_app(replace(CFG, data_dir=str(tmp_path)),
                   client=sample.ListRecorder(d0.points())).test_client()
    c.post("/api/places", json={"name": "Home", "kind": "home", "lat": HOME[0], "lon": HOME[1]})
    st = c.get(f"/api/day/{D}").json["stops"]
    c.patch(f"/api/stops/{st[0]['id']}", json={"business_name": "Acme", "status": "Confirmed"})
    c.patch(f"/api/stops/{st[1]['id']}", json={"status": "Personal"})
    c.patch(f"/api/stops/{st[2]['id']}", json={"business_name": "Home", "status": "Personal"})
    c.post(f"/api/stops/{st[1]['id']}/mark", json={"which": "end"})     # End day here, at the errand
    d = c.get(f"/api/day/{D}").json
    assert d["end_day"]["kind"] == "personal" and d["end_day"]["miles_to_home"]
    assert d["drives"][0]["counts"] and not d["drives"][1]["counts"]
    j = c.get(f"/api/export.json?from={D}").json["days"][0]
    assert any(x["reason"] == "End Day" for x in j["excluded"])


def test_mileage_is_always_home_to_home(tmp_path):
    c = make(tmp_path)
    c.post("/api/places", json={"name": "Home", "kind": "home", "lat": HOME[0], "lon": HOME[1]})
    d = day(c)                                    # no Start or End tapped at all
    m = d["mileage"]
    assert [(x["from"], x["to"]) for x in m["legs"]] == [("Home", "Acme"), ("Acme", "Home")]
    assert d["totals"]["miles_approx"] == m["total_miles"] > 4
    j = c.get(f"/api/export.json?from={D}").json["days"][0]
    assert j["summary"]["miles_approx"] == m["total_miles"] and j["mileage"]["basis"] == "home-to-home"
    assert d["totals"]["drive_s"] == 0            # time rules are unchanged


def test_mileage_skips_personal_detours(tmp_path):
    d0 = sample.Day(D)
    t = d0.dwell(*JOB, d0.at(8), 120)                 # job 08:00-10:00
    far = (HOME[0] + 0.05, HOME[1])
    t = d0.drive(JOB, far, t, 15)
    t = d0.dwell(*far, t, 30)                         # errand
    t = d0.drive(far, HOME, t, 10)
    d0.dwell(*HOME, t, 60)
    c = create_app(replace(CFG, data_dir=str(tmp_path)),
                   client=sample.ListRecorder(d0.points())).test_client()
    c.post("/api/places", json={"name": "Home", "kind": "home", "lat": HOME[0], "lon": HOME[1]})
    st = c.get(f"/api/day/{D}").json["stops"]
    c.patch(f"/api/stops/{st[0]['id']}", json={"business_name": "Acme", "status": "Confirmed"})
    c.patch(f"/api/stops/{st[1]['id']}", json={"status": "Personal"})
    c.patch(f"/api/stops/{st[2]['id']}", json={"business_name": "Home", "status": "Personal"})
    m = c.get(f"/api/day/{D}").json["mileage"]
    last = m["legs"][-1]
    assert (last["from"], last["to"], last["method"]) == ("Acme", "Home", "estimate")   # direct, not via the errand
    from app.geo import haversine_m
    direct = haversine_m(*JOB, *HOME) * 1.25 / 1609.344
    assert abs(last["miles"] - direct) < 0.2
