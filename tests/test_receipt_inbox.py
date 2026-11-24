"""Truck → Receipts: past gas and vendor stops created from receipt photos, each item handled its own way."""
import io

import pytest
from PIL import Image

from app import ai, db
from tests.test_commute import D, make

OLD = "2026-10-06"          # a day with no GPS data at all


def jpeg():
    b = io.BytesIO()
    Image.new("RGB", (200, 300), "white").save(b, "JPEG")
    return b.getvalue()


@pytest.fixture
def app_(tmp_path, monkeypatch):
    c = make(tmp_path)
    conn = db.connect(str(tmp_path))
    conn.execute("INSERT INTO settings(key,value) VALUES ('anthropic_key','sk-test')")
    conn.commit(); conn.close()
    reads = []
    monkeypatch.setattr(ai, "read_receipt", lambda cfg, data, client=None: reads.pop(0))
    return c, reads, tmp_path


def upload(c):
    r = c.post("/api/receipts/inbox", data={"file": (io.BytesIO(jpeg()), "r.jpg")}, content_type="multipart/form-data")
    assert r.status_code == 200, r.json
    return r.json["item"]


GAS = {"store": "Chevron", "date": OLD, "time": "07:42", "total": 68.10, "tax": None, "gallons": 14.2,
       "price_per_gallon": 4.796, "is_fuel": True, "items": []}
STORE = {"store": "Home Depot", "date": OLD, "time": "10:15", "total": 61.13, "tax": 4.13, "gallons": None,
         "price_per_gallon": None, "is_fuel": False, "items": [
             {"description": "CAT6 KEYSTONE WHT", "sku": "KJ-1", "qty": 4, "unit_price": 3.5, "amount": 14.0},
             {"description": "Blank plate", "sku": None, "qty": 2, "unit_price": 1.5, "amount": 3.0},
             {"description": "Shop towels", "sku": None, "qty": 1, "unit_price": 6.0, "amount": 6.0},
             {"description": "Snacks", "sku": None, "qty": 1, "unit_price": 34.0, "amount": 34.0}]}


def test_gas_receipt_makes_a_fill_up_on_a_day_without_gps(app_):
    c, reads, _ = app_
    reads.append(dict(GAS))
    it = upload(c)
    p = it["proposal"]
    assert p["type"] == "gas" and p["date"] == OLD and p["time"] == "07:42" and p["arrive"] == "07:34" and p["depart"] == "07:44"
    assert p["stop_id"] is None
    r = c.post(f"/api/receipts/inbox/{it['id']}/apply", json={**{k: p[k] for k in ("type", "store", "date", "time", "arrive", "depart")},
                                                                "total": 68.10, "gallons": 14.2, "odometer": 48211})
    assert r.status_code == 200, r.json
    day = c.get(f"/api/day/{OLD}").json
    s = day["stops"][0]
    assert s["stop_type"] == "gas" and s["business_name"] == "Chevron" and s["fuel"]["total"] == 68.1 and s["from_receipt"]
    assert len(s["photos"]) == 1
    rep = c.get(f"/api/export.json?from={OLD}").json["days"][0]
    assert rep["fuel"][0]["added_from_receipt"] and rep["fuel"][0]["gallons"] == 14.2
    assert c.post(f"/api/receipts/inbox/{it['id']}/apply", json={"type": "gas"}).status_code == 400   # only once


def test_store_receipt_items_each_their_own_way(app_):
    c, reads, tmp = app_
    c.post("/api/parts", json={"name": "Keystone jack white", "sku": "KJ-1", "unit_cost": 9, "billable": True})
    jack = next(p for p in c.get("/api/parts").json["parts"] if p["sku"] == "KJ-1")
    job = next(s for s in c.get(f"/api/day/{D}").json["stops"] if s["business_name"] == "Acme")
    reads.append(dict(STORE))
    it = upload(c)
    items = it["items"]
    assert items[0]["part"]["id"] == jack["id"] and items[0]["action"] == "stock" and items[3]["action"] == "expense"
    jobs = c.get(f"/api/receipts/jobs?date={OLD}").json["jobs"]
    assert any(j["id"] == job["id"] for j in jobs)
    p = it["proposal"]
    body = {**{k: p[k] for k in ("type", "store", "date", "time", "arrive", "depart")}, "total": 61.13, "tax": 4.13,
            "tax_expense": True, "items": [
                {"action": "stock", "part_id": jack["id"], "name": "CAT6 KEYSTONE WHT", "qty": 4, "amount": 14.0},
                {"action": "job", "job_stop_id": job["id"], "name": "Blank plate", "qty": 2, "amount": 3.0},
                {"action": "expense", "name": "Shop towels", "qty": 1, "amount": 6.0},
                {"action": "skip", "name": "Snacks", "qty": 1, "amount": 34.0}]}
    r = c.post(f"/api/receipts/inbox/{it['id']}/apply", json=body)
    assert r.status_code == 200, r.json
    jack = next(p for p in c.get("/api/parts").json["parts"] if p["sku"] == "KJ-1")
    assert jack["on_hand"] == 4 and jack["last_cost"] == 3.5 and jack["tracked"]
    pur = c.get("/api/purchases?from=2026-10-01&to=2026-10-31").json
    assert len(pur["purchases"] if "purchases" in pur else pur["list"]) == 2          # dated on the receipt day
    vend = c.get(f"/api/day/{OLD}").json["stops"][0]
    assert vend["stop_type"] == "vendor" and vend["status"] == "Confirmed" and vend["from_receipt"]
    exp = [x for x in vend["parts"] if x["expense"]]
    assert sorted((x["notes"], x["unit_cost"]) for x in exp) == [("Sales tax", 4.13), ("Shop towels", 6.0)]
    jp = [x for x in next(s for s in c.get(f"/api/day/{D}").json["stops"] if s["id"] == job["id"])["parts"]
          if x["name"] == "Blank plate"]
    assert jp and jp[0]["qty"] == 2 and jp[0]["billable"] and jp[0]["unit_cost"] == 1.5
    rep = c.get(f"/api/export.json?from={OLD}").json
    assert rep["days"][0]["stops"][0]["added_from_receipt"]
    assert "Store purchase (Shop towels) $6.00" in rep["records_to_update"][0]["expenses"]
    assert c.get(f"/api/export.pdf?from={OLD}").status_code == 200


def test_validation_and_skip(app_):
    c, reads, _ = app_
    reads.append(dict(STORE, date=None, time=None))
    it = upload(c)
    assert it["proposal"]["date"] == "" and it["proposal"]["arrive"] == ""
    bad = c.post(f"/api/receipts/inbox/{it['id']}/apply", json={"type": "vendor", "store": "HD", "date": ""})
    assert bad.status_code == 400 and "date" in bad.json["error"]
    bad = c.post(f"/api/receipts/inbox/{it['id']}/apply", json={"type": "vendor", "store": "HD", "date": OLD,
              "arrive": "10:00", "depart": "10:20", "items": [{"action": "job", "name": "x", "qty": 1}]})
    assert bad.status_code == 400 and "Pick the job" in bad.json["error"]
    assert c.get(f"/api/day/{OLD}").json["stops"] == []                 # nothing written by a refused Add
    assert c.post(f"/api/receipts/inbox/{it['id']}/skip").status_code == 200
    assert c.get("/api/receipts/inbox").json["items"][0]["status"] == "skipped"
    assert c.get(f"/api/receipts/inbox/{it['id']}.jpg").status_code == 200
    assert c.delete(f"/api/receipts/inbox/{it['id']}").status_code == 200


def test_needs_the_anthropic_key(tmp_path):
    c = make(tmp_path)
    r = c.post("/api/receipts/inbox", data={"file": (io.BytesIO(jpeg()), "r.jpg")}, content_type="multipart/form-data")
    assert r.status_code == 400 and "Anthropic" in r.json["error"]


def test_receipt_counting_sticks_adds_feet(app_):
    c, reads, _ = app_
    c.post("/api/parts", json={"name": "Raceway 3/4in - White", "sku": "TSR1W-6A", "unit": "ft", "unit_cost": 4.25,
                               "pack_size": 6, "pack_unit": "stick", "vendor_item": "HT-TSR1W-6A", "vendor_unit_cost": 2.6333})
    reads.append({"store": "Discount Low Voltage", "date": OLD, "time": "14:05", "total": 31.6, "tax": None, "gallons": None,
                  "price_per_gallon": None, "is_fuel": False,
                  "items": [{"description": "HT RACEWAY .75 6FT WHT", "sku": "HT-TSR1W-6A", "qty": 2, "unit_price": 15.8, "amount": 31.6}]})
    it = upload(c)
    x = it["items"][0]
    assert x["part"]["name"] == "Raceway 3/4in - White" and x["packs"] is True and x["action"] == "stock"
    p = it["proposal"]
    r = c.post(f"/api/receipts/inbox/{it['id']}/apply", json={**{k: p[k] for k in ("type", "store", "date", "time", "arrive", "depart")},
               "total": 31.6, "items": [{"action": "stock", "part_id": x["part"]["id"], "name": x["description"], "qty": 2,
                                         "amount": 31.6, "packs": True}]})
    assert r.status_code == 200, r.json
    part = next(q for q in c.get("/api/parts").json["parts"] if q["sku"] == "TSR1W-6A")
    assert part["on_hand"] == 12 and round(part["last_cost"], 4) == 2.6333        # 2 sticks = 12 ft at $2.63/ft
