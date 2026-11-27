"""Vendors tab: one record per vendor name, across branches and parts; details; rename merges."""
from tests.test_export import D, make


def test_vendor_list_detail_and_rename_merge(tmp_path):
    c = make(tmp_path)
    ss = c.get(f"/api/day/{D}").json["stops"]
    a, b = ss[0], ss[1]
    c.post("/api/places", json={"name": "Home Depot", "address": "1 Main", "lat": a["lat"], "lon": a["lon"], "kind": "vendor"})
    c.post("/api/places", json={"name": "Home Depot", "address": "9 5th Ave", "lat": b["lat"], "lon": b["lon"], "kind": "vendor"})
    c.post("/api/parts", json={"name": "Tape", "vendor": "home depot", "on_hand": "1", "reorder_at": "2"})
    c.post("/api/parts", json={"name": "Screws", "vendor": "HomeDepot", "on_hand": "50"})
    sid = a["id"]
    c.patch(f"/api/stops/{sid}", json={"stop_type": "vendor", "business_name": "Home Depot"})
    c.post(f"/api/stops/{sid}/parts", json={"name": "Store purchase", "type": "expense", "qty": 1, "unit_cost": "86.40"})
    vs = {v["name"].lower(): v for v in c.get("/api/vendors").json["vendors"]}
    assert vs["home depot"]["locations"] == 2 and vs["home depot"]["parts"] == 1 and vs["home depot"]["low"] == 1
    assert "homedepot" in vs
    d = c.post("/api/vendors/detail", json={"name": "Home Depot"}).json
    assert len(d["locations"]) == 2 and d["low"][0]["name"] == "Tape" and d["totals"]["expenses"] == 86.4
    assert d["visits"] and d["visits"][0]["stop_id"] == sid
    r = c.put("/api/vendors", json={"name": "Home Depot", "account": "PRO-123", "phone": "555-0100"})
    assert r.status_code == 200 and c.post("/api/vendors/detail", json={"name": "home depot"}).json["info"]["account"] == "PRO-123"
    # rename the misspelt one into the real vendor: its parts move over
    c.put("/api/vendors", json={"name": "HomeDepot", "rename": "Home Depot", "notes": "same store"})
    names = [v["name"].lower() for v in c.get("/api/vendors").json["vendors"]]
    assert names.count("home depot") == 1 and "homedepot" not in names
    d = c.post("/api/vendors/detail", json={"name": "Home Depot"}).json
    assert {p["name"] for p in d["parts"]} == {"Tape", "Screws"} and d["info"]["account"] == "PRO-123"
    assert d["info"]["notes"] == "same store"
    assert c.post("/api/vendors/detail", json={"name": "Nobody"}).status_code == 400
    assert "Home Depot" in c.get("/api/parts/choices").json["vendors"]
