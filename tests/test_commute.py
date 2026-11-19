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
    c.post(f"/api/day/{D}/mark", json={"which": "end", "time": "10:30"})
    d = day(c)
    assert d["drives"][1]["counts"] and d["totals"]["drive_s"] > first + 15 * 60


def test_starting_after_the_commute_does_not_count_it(tmp_path):
    c = make(tmp_path)
    c.post(f"/api/day/{D}/mark", json={"which": "start", "time": "08:25"})   # tapped at the job site
    assert not day(c)["drives"][0]["counts"]
    c.post(f"/api/day/{D}/mark", json={"which": "end", "time": "10:45"})     # tapped at home
    assert not day(c)["drives"][1]["counts"]


def test_report_includes_counted_commutes(tmp_path):
    c = make(tmp_path)
    c.post(f"/api/day/{D}/mark", json={"which": "start", "time": "07:55"})
    c.post(f"/api/day/{D}/mark", json={"which": "end", "time": "10:30"})
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
