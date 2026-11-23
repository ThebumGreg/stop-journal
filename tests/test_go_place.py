"""Home page → Go to a place: the next stop at that place is filled in with it."""
from dataclasses import replace

from app import create_app, sample
from app import service as svc
from tests.test_detect import CFG

D = "2026-11-18"
HOME, JOB = sample.SITE_A, sample.SITE_B


def app_with_day(tmp_path):
    d = sample.Day(D)
    t = d.dwell(*HOME, d.at(7), 60)                 # 07:00-08:00 at home
    t = d.drive(HOME, JOB, t, 20)                    # 08:00-08:20
    t = d.dwell(*JOB, t, 120)                        # 08:20-10:20 at the job
    t = d.drive(JOB, HOME, t, 20)
    d.dwell(*HOME, t, 60)
    c = create_app(replace(CFG, data_dir=str(tmp_path)), client=sample.ListRecorder(d.points())).test_client()
    return c, d


def place(c, name, kind, ll):
    r = c.post("/api/places", json={"name": name, "kind": kind, "lat": ll[0], "lon": ll[1], "address": "1 Main St"})
    assert r.status_code in (200, 201), r.json
    return next(p["id"] for p in c.get("/api/places").json["places"] if p["name"] == name)


def test_heading_to_a_place_fills_in_the_stop_there(tmp_path, monkeypatch):
    c, d = app_with_day(tmp_path)
    acme = place(c, "Acme Dental", "client", JOB)
    monkeypatch.setattr(svc.time, "time", lambda: d.at(7) + 55 * 60)     # tapped at 07:55, still at home
    r = c.post("/api/go", json={"site_id": acme})
    assert r.status_code == 200 and r.json["trip"]["name"] == "Acme Dental"
    assert c.post("/api/go", json={"site_id": "x"}).status_code == 400
    monkeypatch.setattr(svc.time, "time", lambda: d.at(9))
    stops = c.get(f"/api/day/{D}").json["stops"]
    job = next(s for s in stops if s["business_name"] == "Acme Dental")
    assert job["stop_type"] == "job" and job["known_site_id"] == acme and job["status"] == "Draft"
    assert not stops[0]["business_name"] or stops[0]["business_name"] != "Acme Dental"    # home before the tap
    assert svc.active_trip(__import__("app.db", fromlist=["db"]).connect(str(tmp_path))) is None   # used once


def test_trip_never_replaces_a_typed_name_and_cancel_works(tmp_path, monkeypatch):
    c, d = app_with_day(tmp_path)
    monkeypatch.setattr(svc.time, "time", lambda: d.at(9))
    stops = c.get(f"/api/day/{D}").json["stops"]
    job = stops[1]
    c.patch(f"/api/stops/{job['id']}", json={"business_name": "Typed Co"})
    acme = place(c, "Acme Dental", "client", JOB)
    monkeypatch.setattr(svc.time, "time", lambda: d.at(7) + 55 * 60)
    c.post("/api/go", json={"site_id": acme})
    monkeypatch.setattr(svc.time, "time", lambda: d.at(9))
    c.post(f"/api/day/{D}/sync")
    assert c.get(f"/api/day/{D}").json["stops"][1]["business_name"] == "Typed Co"
    c.delete("/api/go")
    from app import db
    assert svc.active_trip(db.connect(str(tmp_path))) is None


def test_vendor_trip_sets_vendor_type(tmp_path, monkeypatch):
    c, d = app_with_day(tmp_path)
    hd = place(c, "Supply House", "vendor", JOB)
    monkeypatch.setattr(svc.time, "time", lambda: d.at(7) + 58 * 60)
    c.post("/api/go", json={"site_id": hd})
    monkeypatch.setattr(svc.time, "time", lambda: d.at(9))
    job = c.get(f"/api/day/{D}").json["stops"][1]
    assert job["business_name"] == "Supply House" and job["stop_type"] == "vendor"


def test_go_places_lists_saved_places(tmp_path):
    c, d = app_with_day(tmp_path)
    place(c, "Acme Dental", "client", JOB)
    r = c.get("/api/go/places").json
    p = next(x for x in r["places"] if x["name"] == "Acme Dental")
    assert p["kind"] == "client" and p["lat"] is not None and r["trip"] is None
