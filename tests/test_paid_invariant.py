"""Paid time in the summary always equals the paid rows of the timesheet, whatever sits between the jobs."""
import importlib.util
import os
import tempfile
from dataclasses import replace

import pytest

from app import create_app, sample
from tests.test_detect import CFG

D = "2026-11-18"
HOME = sample.SITE_A
P = [sample.offset(*HOME, 6000 + 1500 * k, 4000 + 1800 * k) for k in range(8)]


def run_day(tmp_path, plan):
    """plan: list of (place index or 'home', minutes, type, name); a 10-minute drive between each."""
    d = sample.Day(D)
    t = d.at(7)
    where = HOME
    spots = []
    for i, (p, mins, typ, name) in enumerate(plan):
        ll = HOME if p == "home" else P[p]
        if i:
            t = d.drive(where, ll, t, 10)
        t = d.dwell(*ll, t, mins)
        where = ll
        spots.append((typ, name))
    d.dwell(*where, t, 5)
    c = create_app(replace(CFG, data_dir=str(tmp_path)), client=sample.ListRecorder(d.points())).test_client()
    c.post("/api/places", json={"name": "Home", "kind": "home", "lat": HOME[0], "lon": HOME[1]})
    stops = [s for s in c.get(f"/api/day/{D}").json["stops"] if s["kind"] == "stop"]
    assert len(stops) == len(plan), [(s["arrive"], s["depart"]) for s in stops]
    for s, (typ, name) in zip(stops, spots):
        body = {"stop_type": typ, "business_name": name}
        if typ == "job":
            body.update(work_category="Time & Material", notes="Work.")
        assert c.patch(f"/api/stops/{s['id']}", json=body).status_code == 200
        if typ in ("job", "vendor"):
            c.post(f"/api/stops/{s['id']}/finish", json={"visit_result": "complete"} if typ == "job" else {})
    c.post(f"/api/day/{D}/mark", json={"which": "start", "time": "07:05"})
    c.post(f"/api/day/{D}/end-at-home")
    return c.get(f"/api/export.json?from={D}").json["days"][0]


def check(day):
    sm, ts = day["summary"], day["timesheet"]
    assert sm["paid_minutes"] == sum(r["minutes"] for r in ts if r["paid"]), ts
    assert sm["drive_minutes"] == sum(r["minutes"] for r in ts if r["type"] == "drive"), ts
    assert sm["traffic_minutes"] == sm["fuel_stop_minutes"] + sm["short_break_minutes"] + sum(
        r["minutes"] for r in ts if r["description"].startswith("Traffic"))


HOMEP = ("home", 30, "personal", "Home")
DAYS = {
    "fuel between jobs": [HOMEP, (0, 60, "job", "A"), (1, 15, "gas", "Fuel"), (2, 60, "job", "B"), HOMEP],
    "two fuel stops in a row": [HOMEP, (0, 60, "job", "A"), (1, 15, "gas", "F1"), (2, 15, "gas", "F2"), (3, 60, "job", "B"), HOMEP],
    "break then fuel": [HOMEP, (0, 60, "job", "A"), (1, 15, "break", "Coffee"), (2, 15, "gas", "Fuel"), (3, 60, "job", "B"), HOMEP],
    "lunch after fuel": [HOMEP, (0, 60, "job", "A"), (1, 15, "gas", "Fuel"), (2, 30, "lunch", "Deli"), (3, 60, "job", "B"), HOMEP],
    "fuel on the way home": [HOMEP, (0, 60, "job", "A"), (1, 15, "gas", "Fuel"), HOMEP],
    "fuel before the first job": [HOMEP, (1, 15, "gas", "Fuel"), (0, 60, "job", "A"), HOMEP],
}


@pytest.mark.parametrize("name", list(DAYS))
def test_paid_minutes_match_the_timesheet(tmp_path, name):
    check(run_day(tmp_path, DAYS[name]))


def test_fuel_between_jobs_drive_covers_both_legs(tmp_path):
    day = run_day(tmp_path, DAYS["fuel between jobs"])
    b = next(s for s in day["stops"] if s["business_name"] == "B")
    rows = [r for r in day["timesheet"] if r["type"] == "drive" and "Fuel stop" not in r["description"]]
    a_to_fuel = next(r for r in rows if r["description"].endswith("to Fuel"))
    fuel_to_b = next(r for r in rows if r["description"].endswith("to B"))
    assert b["drive_from_previous_stop"]["minutes"] == a_to_fuel["minutes"] + fuel_to_b["minutes"]


def test_sample_day(tmp_path):
    path = os.path.join(os.path.dirname(__file__), "..", "scripts", "make_sample_report.py")
    spec = importlib.util.spec_from_file_location("mk", path)
    mk = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mk)
    c, _ = mk.build(str(tmp_path))
    day = c.get(f"/api/export.json?from={D}").json["days"][0]
    check(day)
    assert day["stops"][1]["drive_from_previous_stop"]["minutes"] == 22
    assert day["summary"]["drive_minutes"] == 101 and day["summary"]["paid_minutes"] == 447
