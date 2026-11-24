"""Vendor stops record purchases ("Bought here"): stock goes UP, nothing is billed; and parts can be deleted for good."""
import importlib.util
import os

import pytest

D = "2026-11-18"


@pytest.fixture
def sample(tmp_path):
    path = os.path.join(os.path.dirname(__file__), "..", "scripts", "make_sample_report.py")
    spec = importlib.util.spec_from_file_location("mk", path)
    mk = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mk)
    c, cfg = mk.build(str(tmp_path))
    return c


def vendor_stop(c):
    return next(s for s in c.get(f"/api/day/{D}").json["stops"] if s["stop_type"] == "vendor")


def part(c, pid):
    return next(p for p in c.get("/api/parts?all=1").json["parts"] if p["id"] == pid)


def mine(s, pid):
    return next(b for b in s["bought"] if b["part_id"] == pid)


def new_part(c, **f):
    return c.post("/api/parts", json={"name": "Test Jack", "unit": "ea", "unit_cost": 9, "on_hand": 5, **f}).json["id"]


def test_bought_adds_stock_and_shows_in_report(sample):
    c = sample
    pid = new_part(c)
    s = vendor_stop(c)
    r = c.post(f"/api/stops/{s['id']}/bought", json={"part_id": pid, "qty": 10, "paid": "25.00"})
    assert r.status_code == 200, r.json
    assert part(c, pid)["on_hand"] == 15
    s = vendor_stop(c)
    b = mine(s, pid)
    assert (b["name"], b["qty"], b["paid"], b["each"]) == ("Test Jack", 10, 25.0, 2.5)
    pu = c.get(f"/api/purchases?part={pid}").json["purchases"][0]
    assert pu["vendor"] == s["business_name"] and pu["total"] == 25.0
    assert part(c, pid)["last_cost"] == 2.5

    rep = c.get(f"/api/export.json?from={D}").json
    st = next(x for x in rep["days"][0]["stops"] if x["business_name"] == s["business_name"])
    assert {"name": "Test Jack", "part_number": "", "quantity": 10, "unit": "ea", "paid": 25.0} in st["bought_here"]
    assert all(p["name"] != "Test Jack" for p in st["parts_used"])            # never billed at the vendor
    assert rep["days"][0]["summary"]["bought_total"] >= 25.0
    rec = next(x for x in rep["records_to_update"] if x["business_name"] == s["business_name"])
    assert "10 x Test Jack $25.00" in rec["bought_here"]
    assert "1000 x Cat 6 cable (C6-1000-BLU) $189.00" in rec["bought_here"]
    nocost = c.get(f"/api/export.json?from={D}&omit=costs").json
    st = next(x for x in nocost["days"][0]["stops"] if x["business_name"] == s["business_name"])
    assert "paid" not in st["bought_here"][0] and "bought_total" not in nocost["days"][0]["summary"]
    assert c.get(f"/api/export.pdf?from={D}").status_code == 200


def test_change_and_remove_a_purchase(sample):
    c = sample
    pid = new_part(c)
    s = vendor_stop(c)
    c.post(f"/api/stops/{s['id']}/bought", json={"part_id": pid, "qty": 4})
    b = mine(vendor_stop(c), pid)
    assert b["paid"] is None
    assert c.patch(f"/api/purchases/{b['id']}", json={"qty": 6, "paid": 12}).status_code == 200
    assert part(c, pid)["on_hand"] == 11
    # pages send DELETE as POST + X-Method-Override (the router blocks real DELETEs)
    r = c.post(f"/api/purchases/{b['id']}", headers={"X-Method-Override": "DELETE"})
    assert r.status_code == 200
    assert part(c, pid)["on_hand"] == 5 and all(x["part_id"] != pid for x in vendor_stop(c)["bought"])


def test_packs_and_new_parts(sample):
    c = sample
    s = vendor_stop(c)
    pid = c.post("/api/parts", json={"name": "Raceway", "unit": "ft", "pack_size": 6, "pack_unit": "stick",
                                     "unit_cost": 3}).json["id"]
    c.post(f"/api/stops/{s['id']}/bought", json={"part_id": pid, "packs": 3, "paid": 36})
    p = part(c, pid)
    assert p["on_hand"] == 18 and p["tracked"] and p["last_cost"] == 2.0
    r = c.post(f"/api/stops/{s['id']}/bought", json={"name": "Brand New Widget", "unit": "box", "qty": 2, "paid": 10})
    assert r.status_code == 200
    w = next(p for p in c.get("/api/parts").json["parts"] if p["name"] == "Brand New Widget")
    assert w["on_hand"] == 2 and w["unit"] == "box" and w["vendor"] == s["business_name"]
    assert c.post(f"/api/stops/{s['id']}/bought", json={"part_id": pid, "qty": 0}).status_code == 400


def test_old_part_line_moves_to_bought(sample):
    c = sample
    pid = new_part(c)
    s = vendor_stop(c)
    c.post(f"/api/stops/{s['id']}/parts", json={"part_id": pid, "qty": 2})
    assert part(c, pid)["on_hand"] == 3                                         # the old way took stock off
    sp = next(p for p in vendor_stop(c)["parts"] if p["part_id"] == pid)
    assert c.post(f"/api/stop-parts/{sp['id']}/to-bought").status_code == 200
    s = vendor_stop(c)
    assert part(c, pid)["on_hand"] == 7
    assert not any(p["part_id"] == pid for p in s["parts"]) and mine(s, pid)["qty"] == 2


def test_purchases_follow_undo_after_delete(sample):
    c = sample
    pid = new_part(c)
    s = vendor_stop(c)
    c.post(f"/api/stops/{s['id']}/bought", json={"part_id": pid, "qty": 1})
    c.delete(f"/api/stops/{s['id']}")
    assert part(c, pid)["on_hand"] == 6                                         # still bought: stock stays
    c.post(f"/api/day/{D}/undo")
    assert mine(vendor_stop(c), pid)["qty"] == 1


def test_delete_part_for_good(sample):
    c = sample
    pid = new_part(c, name="Typo Part")
    c.post(f"/api/parts/{pid}/adjust", json={"delta": 2, "paid": 4})
    r = c.delete(f"/api/parts/{pid}")
    assert r.status_code == 200 and r.json["deleted"] == "Typo Part" and r.json["purchases"] == 1
    assert all(p["id"] != pid for p in c.get("/api/parts?all=1").json["parts"])
    assert c.get(f"/api/purchases?part={pid}").json["purchases"] == []

    used = new_part(c, name="Used Part")
    job = next(s for s in c.get(f"/api/day/{D}").json["stops"] if s["stop_type"] == "job")
    c.post(f"/api/stops/{job['id']}/parts", json={"part_id": used, "qty": 1})
    r = c.delete(f"/api/parts/{used}")
    assert r.status_code == 400 and "Remove from catalog" in r.json["error"]
