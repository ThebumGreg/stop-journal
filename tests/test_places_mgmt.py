from dataclasses import replace

from app import create_app, db, sample
from tests.test_detect import CFG
from tests.test_places import FakeSession  # noqa: F401  (shared fixtures live there)

D = "2026-11-18"
HOME = sample.SITE_A
OFFICE = sample.SITE_B


def revisit_day():
    """A at 9, drive to B, then back to A in the afternoon."""
    d = sample.Day(D)
    d.dwell(*sample.SITE_A, d.at(9), 45)
    d.drive(sample.SITE_A, sample.SITE_B, d.at(9, 45), 20)
    d.dwell(*sample.SITE_B, d.at(10, 5), 40)
    d.drive(sample.SITE_B, sample.SITE_A, d.at(10, 45), 20)
    d.dwell(*sample.SITE_A, d.at(11, 5), 40)
    return d.points()


def make(tmp_path, points=None, **kw):
    cfg = replace(CFG, data_dir=str(tmp_path), **kw)
    return create_app(cfg, client=sample.ListRecorder(revisit_day() if points is None else points),
                      provider={"osm": _Osm(), "google": _Osm()}).test_client(), cfg


class _Osm:
    def search_text(self, q, lat, lon):
        from app.places.base import Candidate
        return [Candidate("Acme Supply", address="1 Dock Rd", lat=lat + .001, lon=lon, category="shop=hardware")]

    search_nearby = search_text

    def reverse(self, lat, lon):
        return "9 Sample Ave, Testville, CA, 90001"


def place(c, **f):
    body = {"name": "X", "lat": HOME[0], "lon": HOME[1], "kind": "home"}
    body.update(f)
    r = c.post("/api/places", json=body)
    assert r.status_code == 200, r.json
    return r.json


def stops(c):
    return c.get(f"/api/day/{D}").json["stops"]


def test_create_validate_defaults_and_dedupe(tmp_path):
    c, _ = make(tmp_path)
    h = place(c, name="Home", kind="home")
    assert h["default_status"] == "Personal" and h["radius_m"] == 100
    assert place(c, name="Main office", kind="office", lat=OFFICE[0], lon=OFFICE[1])["default_status"] == "Draft"
    assert place(c, name="home", kind="home")["id"] == h["id"]           # same place, same name
    assert c.post("/api/places", json={"name": "", "lat": 1, "lon": 1}).status_code == 400
    assert c.post("/api/places", json={"name": "x", "lat": 99, "lon": 1}).status_code == 400
    assert c.post("/api/places", json={"name": "x", "lat": 1, "lon": 1, "kind": "bogus"}).status_code == 400
    assert c.post("/api/places", json={"name": "x", "lat": 1, "lon": 1, "radius_m": 5}).status_code == 400


def test_places_prefill_when_day_is_first_opened(tmp_path):
    c, _ = make(tmp_path)
    place(c, name="Home", kind="home")
    place(c, name="Main office", kind="office", lat=OFFICE[0], lon=OFFICE[1])
    st = stops(c)                                  # first open of the day
    assert [(s["business_name"], s["status"]) for s in st] == [
        ("Home", "Personal"), ("Main office", "Draft"), ("Home", "Personal")]
    assert st[0]["category"] == "place=home" and st[0]["known_site_id"]


def test_places_added_later_apply_on_redetect(tmp_path):
    c, _ = make(tmp_path)
    assert all(s["business_name"] == "" for s in stops(c))   # day already opened
    place(c, name="Home", kind="home")
    assert all(s["business_name"] == "" for s in stops(c))   # existing stops are not rewritten
    c.post(f"/api/day/{D}/redetect", json={})
    assert [s["business_name"] for s in stops(c)] == ["Home", "", "Home"]


def test_work_at_home_still_possible(tmp_path):
    c, _ = make(tmp_path)
    place(c, name="Home", kind="home")
    c.post(f"/api/day/{D}/redetect", json={})
    sid = stops(c)[0]["id"]
    r = c.patch(f"/api/stops/{sid}", json={"status": "Confirmed", "notes": "bench repair"})
    assert r.status_code == 200 and r.json["stops"][0]["status"] == "Confirmed"
    assert r.json["totals"]["confirmed"] == 1


def test_clients_and_edited_stops_are_not_auto_filled(tmp_path):
    c, _ = make(tmp_path)
    place(c, name="Client A", kind="client")
    first = stops(c)[0]
    assert first["business_name"] == "" and first["status"] == "Draft"
    c.patch(f"/api/stops/{first['id']}", json={"notes": "hand edited"})
    place(c, name="Home", kind="home")
    c.post(f"/api/day/{D}/sync")
    assert stops(c)[0]["business_name"] == ""            # edited stop untouched by sync


def test_save_stop_as_place_prefills_matching_stops(tmp_path):
    c, _ = make(tmp_path)
    first = stops(c)[0]
    r = c.post(f"/api/stops/{first['id']}/save-place", json={"kind": "vendor", "name": "Dock Supply"})
    assert r.status_code == 200
    st = r.json["stops"]
    assert st[0]["business_name"] == "Dock Supply" and st[0]["category"] == "place=vendor"
    assert st[2]["business_name"] == "Dock Supply" and st[2]["status"] == "Draft"   # revisit pre-filled
    assert st[1]["business_name"] == ""
    assert c.post(f"/api/stops/{first['id']}/save-place", json={"kind": "client"}).status_code == 400
    m = c.post(f"/api/day/{D}/stops", json={"arrive": "13:00", "depart": "13:30"}).json["stops"][-1]
    assert c.post(f"/api/stops/{m['id']}/save-place", json={"kind": "home"}).status_code == 400  # no GPS


def test_edit_and_delete_place(tmp_path):
    c, _ = make(tmp_path)
    p = place(c, name="Home", kind="home")
    r = c.patch(f"/api/places/{p['id']}", json={"name": "House", "kind": "office", "radius_m": 250})
    assert r.json["name"] == "House" and r.json["radius_m"] == 250 and r.json["default_status"] == "Draft"
    assert c.patch(f"/api/places/{p['id']}", json={"radius_m": 5000}).status_code == 400
    c.post(f"/api/day/{D}/redetect", json={})
    assert stops(c)[0]["known_site_id"] == p["id"]
    assert c.delete(f"/api/places/{p['id']}").status_code == 200
    st = stops(c)[0]
    assert st["known_site_id"] is None and st["business_name"] == "House"   # history keeps the name
    assert c.get("/api/places").json["places"] == []
    assert c.delete(f"/api/places/{p['id']}").status_code == 400


def test_list_orders_special_places_first_and_filters(tmp_path):
    c, _ = make(tmp_path)
    place(c, name="Zed Client", kind="client", lat=1, lon=1)
    place(c, name="Home", kind="home")
    names = [p["name"] for p in c.get("/api/places").json["places"]]
    assert names == ["Home", "Zed Client"]
    assert [p["name"] for p in c.get("/api/places?kind=client").json["places"]] == ["Zed Client"]
    assert [p["name"] for p in c.get("/api/places?q=zed").json["places"]] == ["Zed Client"]


def test_from_last_location_and_search(tmp_path):
    c, _ = make(tmp_path)
    r = c.post("/api/places/from-last", json={"kind": "office", "name": "Shop"})
    assert r.status_code == 200 and r.json["kind"] == "office"
    last = revisit_day()[-1]
    assert abs(r.json["lat"] - last["lat"]) < 1e-9
    s = c.get("/api/places/search?q=acme").json
    assert s["candidates"][0]["name"] == "Acme Supply" and all(x["source"] != "known" for x in s["candidates"])
    assert c.get("/api/places/search?q=ab").status_code == 400
    empty, _ = make(tmp_path / "e", points=[])
    assert empty.post("/api/places/from-last", json={}).status_code == 400   # nothing to use yet


def test_suggestions_label_special_places(tmp_path):
    c, _ = make(tmp_path)
    place(c, name="Home", kind="home")
    r = c.get(f"/api/suggest?lat={HOME[0]}&lon={HOME[1]}").json
    assert r["candidates"][0]["name"] == "Home" and r["candidates"][0]["kind"] == "home"


def test_pages_and_work_performed_label(tmp_path):
    c, _ = make(tmp_path)
    assert c.get("/places").status_code == 200 and b"Places" in c.get("/places").data
    js = c.get("/static/day.js").data.decode()
    assert "labeled('Work performed', notes)" in js  # the stop field; drives keep 'Notes'
    locked = create_app(replace(CFG, data_dir=str(tmp_path / "l"), app_password="pw"),
                        client=sample.ListRecorder([])).test_client()
    assert locked.get("/places").status_code == 302 and locked.get("/api/places").status_code == 401


def test_from_last_fills_in_the_address(tmp_path):
    c, _ = make(tmp_path)
    r = c.post("/api/places/from-last", json={"kind": "home", "name": "Home"})
    assert r.json["address"] == "9 Sample Ave, Testville, CA, 90001"
    kept = c.post("/api/places/from-last", json={"kind": "office", "name": "Shop", "address": "Typed St"})
    assert kept.json["address"] == "Typed St"


def test_save_stop_as_place_fills_blank_address(tmp_path):
    c, _ = make(tmp_path)
    day = c.get(f"/api/day/{D}").json
    sid = day["stops"][0]["id"]
    c.post(f"/api/stops/{sid}/save-place", json={"kind": "home", "name": "Home"})
    assert c.get("/api/places?kind=home").json["places"][0]["address"] == "9 Sample Ave, Testville, CA, 90001"


def test_live_status_shape_and_place_name(tmp_path):
    import time
    from app import live
    now = int(time.time())
    pts = [{"tst": now - 1800 + i * 60, "lat": 34.0, "lon": -118.0, "acc": 5} for i in range(30)]
    pts = [{"tst": now - 3600, "lat": 34.05, "lon": -118.0, "acc": 5}] + pts   # arrived from elsewhere
    st = live.status(pts, now, 100)
    assert 1700 <= st["here_s"] <= 1860 and not st["moving"] and st["last_update_s"] < 90
    assert live.status([], now, 100) is None
    one = live.status([{"tst": now, "lat": 1, "lon": 1, "acc": 5}], now, 100)
    assert one["moving"]
    bad = live.status([{"tst": now, "lat": 1, "lon": 1, "acc": 500}], now, 100)
    assert bad is None


def test_live_endpoint(tmp_path):
    import time
    now = int(time.time())
    pts = [{"tst": now - 600 + i * 60, "lat": HOME[0], "lon": HOME[1], "acc": 5, "batt": 77}
           for i in range(10)]
    c, _ = make(tmp_path, points=pts)
    place(c, name="Home", kind="home")
    d = c.get("/api/live").json
    assert d["place"] == "Home" and d["kind"] == "home" and d["batt"] == 77 and d["here_s"] >= 500
    c2, _ = make(tmp_path / "x", points=[{"tst": now, "lat": 5.0, "lon": 5.0, "acc": 5}] * 1 +
                 [{"tst": now - 400, "lat": 5.0, "lon": 5.0, "acc": 5}])
    d2 = c2.get("/api/live").json
    assert d2["place"] == "" and d2["address"].startswith("9 Sample Ave")
    e, _ = make(tmp_path / "e", points=[])
    assert e.get("/api/live").status_code == 404


def test_place_history_and_gas_stations(tmp_path):
    from tests.test_export import D, make
    c = make(tmp_path)
    ss = c.get(f"/api/day/{D}").json["stops"]
    a, b = ss[0], ss[1]
    c.patch(f"/api/stops/{a['id']}", json={"status": "Confirmed", "business_name": "Acme", "notes": "Fixed the valve\nmore",
                                          "work_category": "Time & Material", "visit_result": "complete"})
    site = next(x for x in c.get(f"/api/day/{D}").json["stops"] if x["id"] == a["id"])["known_site_id"]
    h = c.get(f"/api/places/{site}/history").json
    v = h["visits"][0]
    assert h["totals"]["visits"] == 1 and h["totals"]["minutes"] == a["minutes"]
    assert v["summary"] == "Fixed the valve…" and v["visit_result"] == "Complete" and v["type_label"] == "Job"
    assert v["url"] == f"/day/{D}?stop={a['id']}"
    c.patch(f"/api/stops/{b['id']}", json={"stop_type": "gas", "business_name": "Corner Fuel"})
    c.put(f"/api/stops/{b['id']}/fuel", json={"gallons": 10, "total": 40})
    st = c.get("/api/gas-stations").json["stations"]
    assert st[0]["name"] == "Corner Fuel" and st[0]["fills"] == 1 and st[0]["total"] == 40
    g = c.get("/api/gas-stations/history?key=corner fuel").json
    assert g["totals"]["avg_price_per_gallon"] == 4.0 and g["visits"][0]["fuel"]["total"] == 40
    page = c.get("/places").data.decode()
    assert 'aria-current="page"' in page and ">Places<" in page.split('aria-current="page"')[1][:20]
    day = c.get(f"/day/{D}").data.decode()
    assert 'id="toolslink"' in day and '<a href="/day" aria-current="page">Day</a>' in day


def test_new_vendor_branch_stays_a_vendor_and_old_ones_are_repaired(tmp_path):
    from app import db
    from tests.test_export import D, make
    c = make(tmp_path)
    ss = c.get(f"/api/day/{D}").json["stops"]
    a, b, d = ss[0], ss[1], ss[2]
    c.post("/api/places", json={"name": "Home Depot", "lat": a["lat"], "lon": a["lon"], "kind": "vendor"})
    c.patch(f"/api/stops/{b['id']}", json={"business_name": "Home Depot", "status": "Confirmed"})   # typed the old way
    kinds = sorted(p["kind"] for p in c.get("/api/places").json["places"])
    assert kinds == ["vendor", "vendor"]                        # a new branch of a vendor is a vendor
    # a customer with the same name at two addresses (a relocation) stays a client
    c.patch(f"/api/stops/{d['id']}", json={"business_name": "Acme Dental", "status": "Confirmed"})
    assert next(p for p in c.get("/api/places").json["places"] if p["name"] == "Acme Dental")["kind"] == "client"
    # data saved by the old version: a vendor branch stored as a client gets repaired once
    conn = db.connect(str(tmp_path))
    conn.execute("UPDATE known_sites SET kind=NULL WHERE id=(SELECT MAX(id) FROM known_sites WHERE name='Home Depot')")
    conn.execute("DELETE FROM settings WHERE key='_fix_vendor_branches'")
    conn.commit(); conn.close()
    conn = db.connect(str(tmp_path))
    assert [r["kind"] for r in conn.execute("SELECT kind FROM known_sites WHERE name='Home Depot'")] == ["vendor", "vendor"]
    assert conn.execute("SELECT kind FROM known_sites WHERE name='Acme Dental'").fetchone()["kind"] is None
    conn.execute("UPDATE known_sites SET kind=NULL WHERE name='Home Depot'")   # runs once only
    conn.commit(); conn.close()
    conn = db.connect(str(tmp_path))
    assert conn.execute("SELECT COUNT(*) FROM known_sites WHERE kind='vendor'").fetchone()[0] == 0
    conn.close()
