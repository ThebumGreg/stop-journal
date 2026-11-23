"""Parts CSV import accepts a QuickBooks Online Products and Services export."""
import io

from tests.test_commute import D, make

QBO = """Product/Service Name,Sales Description,SKU,Type,Sales Price / Rate,Income Account,Purchase Description,Purchase Cost,Expense Account,Quantity On Hand,Reorder Point,Preferred Vendor
Cable:Cat 6 cable,Cat 6 plenum,C6-1000-BLU,Inventory,0.45,Sales,,0.21,COGS,5000,500,Sample Supply House
Keystone jack,,KJ-C6-W,Non-inventory,"$6.50",Sales,,2.10,COGS,,,
Labor,Hourly labor,,Service,95,Services,,,,,,
Cable:Cat 6 patch cord 3ft,,PC6-3,Service,6.50,Sales,,1.87,COGS,,,
Install fee,,,Service,50,Services,,,,,,
Labor - Professional Services,,Labor,Service,125,Services,,,,,,
Tech time,,LABOR,Service,125,Services,,,,,,
"""


def upload(c, text):
    return c.post("/api/parts/import", data={"file": (io.BytesIO(text.encode()), "q.csv")},
                  content_type="multipart/form-data").json


def test_qbo_products_and_services(tmp_path):
    c = make(tmp_path)
    c.post("/api/parts", json={"name": "Keystone jack", "type": "consumable", "billable": True})
    r = upload(c, QBO)
    assert r["format"] == "QuickBooks Products and Services" and r["created"] == 2 and r["updated"] == 1
    assert r["services_skipped"] == 4 and not r["errors"]
    parts = {p["name"]: p for p in c.get("/api/parts").json["parts"]}
    cable = parts["Cat 6 cable"]
    assert cable["sku"] == "C6-1000-BLU" and cable["unit_cost"] == 0.45 and cable["last_cost"] == 0.21
    assert cable["vendor"] == "Sample Supply House" and not cable["tracked"]      # QuickBooks stock is not the truck's
    jack = parts["Keystone jack"]
    assert jack["sku"] == "KJ-C6-W" and jack["unit_cost"] == 6.5 and jack["kind"] == "consumable"   # kind kept
    assert "Labor" not in parts and "Install fee" not in parts and "Tech time" not in parts
    assert parts["Cat 6 patch cord 3ft"]["sku"] == "PC6-3" and parts["Cat 6 patch cord 3ft"]["last_cost"] == 1.87


def test_billable_part_without_number_warns(tmp_path):
    c = make(tmp_path)
    job = next(s for s in c.get(f"/api/day/{D}").json["stops"] if s["business_name"] == "Acme")
    c.post(f"/api/stops/{job['id']}/parts", json={"name": "Mystery fitting", "qty": 1, "unit_cost": 3, "billable": True})
    w = c.get(f"/api/export/check?from={D}").json["warnings"]
    assert any("“Mystery fitting” has no part number" in x for x in w)


def test_service_items_setting_off(tmp_path):
    from app import db
    c = make(tmp_path)
    conn = db.connect(str(tmp_path))
    conn.execute("INSERT INTO settings(key,value) VALUES ('qbo_service_with_sku','off')")
    conn.commit(); conn.close()
    r = upload(c, QBO)
    assert r["created"] == 2 and r["services_skipped"] == 5          # Inventory + Non-inventory only


def test_vendor_columns_and_cost_never_overwrites_sell_price(tmp_path):
    c = make(tmp_path)
    r = upload(c, "name,sku,unit,unit_cost,vendor_item,dlv_unit_cost,qbo_item\n"
                  "Plenum Cat6,PL6,ft,0.68,U6500,0.275,Cable Plenum CAT6\n")
    assert r["created"] == 1 and not r["ignored_columns"] and any("dlv_unit_cost" in n for n in r["notes"])
    p = next(x for x in c.get("/api/parts").json["parts"] if x["sku"] == "PL6")
    assert (p["unit_cost"], p["last_cost"], p["vendor_item"], p["qbo_item"]) == (0.68, 0.275, "U6500", "Cable Plenum CAT6")
    assert p["margin"] == 0.405 and p["margin_pct"] == 59.6 and not p["below_cost"]
    upload(c, "name,sku,unit_cost,vendor_unit_cost\nPlenum Cat6,PL6,0.70,\n")          # blank keeps our cost
    p = next(x for x in c.get("/api/parts").json["parts"] if x["sku"] == "PL6")
    assert p["unit_cost"] == 0.70 and p["last_cost"] == 0.275
    r = upload(c, "name,sku,Cost\nPlenum Cat6,PL6,0.99\n")                              # "Cost" = what you pay
    p = next(x for x in c.get("/api/parts").json["parts"] if x["sku"] == "PL6")
    assert p["unit_cost"] == 0.70 and p["last_cost"] == 0.99 and p["below_cost"]
    assert any("“Cost” was read as what you pay" in n for n in r["notes"])
