"""Start/End day from a stop, End at home, auto-saved places."""
from dataclasses import replace

from app import create_app, sample
from tests.test_detect import CFG

D = "2026-11-18"
HOME, JOB = sample.SITE_A, sample.SITE_B


def make(tmp_path, home_stop=True):
    d = sample.Day(D)
    t = d.drive(HOME, JOB, d.at(9) - 1200, 20)       # arrives at the job 09:00
    t = d.dwell(*JOB, t, 120)                         # 09:00-11:00
    if home_stop:
        t = d.drive(JOB, HOME, t, 20)
        d.dwell(*HOME, t, 60)
    c = create_app(replace(CFG, data_dir=str(tmp_path)),
                   client=sample.ListRecorder(d.points())).test_client()
    return c


def day(c):
    return c.get(f"/api/day/{D}").json


def test_stop_becomes_start_and_end(tmp_path):
    c = make(tmp_path, home_stop=False)
    st = day(c)["stops"]
    job = [s for s in st if s["kind"] == "stop"][-1]
    assert c.post(f"/api/stops/{job['id']}/mark", json={"which": "start"}).status_code == 200
    assert c.post(f"/api/stops/{job['id']}/mark", json={"which": "end"}).status_code == 200
    m = day(c)["marks"]
    assert m["start_ts"] == job["arrive_ts"] and m["end_ts"] == job["depart_ts"]


def test_end_at_home_adds_basic_stop(tmp_path):
    c = make(tmp_path, home_stop=False)
    day(c)   # the day is opened (detected) before the user taps anything
    r = c.post("/api/places", json={"name": "Home", "kind": "home", "lat": HOME[0], "lon": HOME[1]})
    assert r.status_code in (200, 201), r.data
    # day is in the past: needs a time
    assert c.post(f"/api/day/{D}/end-at-home", json={}).status_code == 400
    r = c.post(f"/api/day/{D}/end-at-home", json={"time": "11:30"})
    assert r.status_code == 200, r.data
    d = r.json
    last = d["stops"][-1]
    assert last["business_name"] == "Home" and last["status"] == "Personal"
    assert d["marks"]["end"] == "11:30"
    assert len(d["drives"]) == len(d["stops"]) - 1 and d["drives"][-1]["miles_approx"] > 0


def test_end_at_home_needs_a_home_place(tmp_path):
    c = make(tmp_path, home_stop=False)
    assert c.post(f"/api/day/{D}/end-at-home", json={"time": "11:30"}).status_code == 400


def test_end_at_home_uses_detected_home_stop(tmp_path):
    c = make(tmp_path)
    st = day(c)["stops"]
    c.patch(f"/api/stops/{st[-1]['id']}", json={"status": "Personal"})
    c.post("/api/places", json={"name": "Home", "kind": "home", "lat": HOME[0], "lon": HOME[1]})
    n = len(day(c)["stops"])
    r = c.post(f"/api/day/{D}/end-at-home", json={"time": "11:30"})
    assert r.status_code == 200 and len(r.json["stops"]) == n


def test_named_personal_stop_saved_as_place(tmp_path):
    c = make(tmp_path)
    st = day(c)["stops"]
    last = st[-1]
    c.patch(f"/api/stops/{last['id']}", json={"business_name": "Home", "status": "Personal"})
    places = c.get("/api/places").json
    rows = places if isinstance(places, list) else places.get("places", places.get("results", []))
    assert any(p["name"] == "Home" and p["kind"] == "home" for p in rows)
