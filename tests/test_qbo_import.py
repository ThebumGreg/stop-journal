"""Parts CSV import accepts a QuickBooks Online Products and Services export."""
import io

from tests.test_commute import D, make

QBO = """Product/Service Name,Sales Description,SKU,Type,Sales Price / Rate,Income Account,Purchase Description,Purchase Cost,Expense Account,Quantity On Hand,Reorder Point,Preferred Vendor
Cable:Cat 6 cable,Cat 6 plenum,C6-1000-BLU,Inventory,0.45,Sales,,0.21,COGS,5000,500,Sample Supply House
Keystone jack,,KJ-C6-W,Non-inventory,"$6.50",Sales,,2.10,COGS,,,
Labor,Hourly labor,,Service,95,Services,,,,,,
"""


def upload(c, text):
    return c.post("/api/parts/import", data={"file": (io.BytesIO(text.encode()), "q.csv")},
                  content_type="multipart/form-data").json


def test_qbo_products_and_services(tmp_path):
    c = make(tmp_path)
    c.post("/api/parts", json={"name": "Keystone jack", "type": "consumable", "billable": True})
    r = upload(c, QBO)
    assert r["format"] == "QuickBooks Products and Services" and r["created"] == 1 and r["updated"] == 1
    assert r["services_skipped"] == 1 and not r["errors"]
    parts = {p["name"]: p for p in c.get("/api/parts").json["parts"]}
    cable = parts["Cat 6 cable"]
    assert cable["sku"] == "C6-1000-BLU" and cable["unit_cost"] == 0.45 and cable["last_cost"] == 0.21
    assert cable["vendor"] == "Sample Supply House" and not cable["tracked"]      # QuickBooks stock is not the truck's
    jack = parts["Keystone jack"]
    assert jack["sku"] == "KJ-C6-W" and jack["unit_cost"] == 6.5 and jack["kind"] == "consumable"   # kind kept
    assert "Labor" not in parts


def test_billable_part_without_number_warns(tmp_path):
    c = make(tmp_path)
    job = next(s for s in c.get(f"/api/day/{D}").json["stops"] if s["business_name"] == "Acme")
    c.post(f"/api/stops/{job['id']}/parts", json={"name": "Mystery fitting", "qty": 1, "unit_cost": 3, "billable": True})
    w = c.get(f"/api/export/check?from={D}").json["warnings"]
    assert any("“Mystery fitting” has no part number" in x for x in w)
