import io

from app import db
from tests.test_parts import make, part


def import_csv(c, text, dry=False):
    return c.post("/api/parts/import" + ("?dry=1" if dry else ""), data=text.encode(), content_type="text/csv").json


def get(c, name):
    return next(p for p in c.get("/api/parts?all=1").json["parts"] if p["name"] == name)


def test_unit_is_a_fixed_list(tmp_path):
    c, _ = make(tmp_path)
    assert part(c, name="A")["unit"] == "ea"                          # blank means each
    assert part(c, name="B", unit="Gallon")["unit"] == "gal"          # common spellings are accepted
    assert part(c, name="C", unit="feet")["unit"] == "ft"
    r = c.post("/api/parts", json={"name": "D", "unit": "bundle"})
    assert r.status_code == 400 and "Unit must be one of" in r.json["error"] and "Each" in r.json["error"]
    pid = get(c, "A")["id"]
    assert c.patch(f"/api/parts/{pid}", json={"unit": "bundle"}).status_code == 400
    assert c.patch(f"/api/parts/{pid}", json={"unit": "box"}).status_code == 200


def test_an_older_unit_still_saves_when_unchanged(tmp_path):
    c, cfg = make(tmp_path)
    pid = part(c, name="Legacy")["id"]
    conn = db.connect(cfg.data_dir)
    conn.execute("UPDATE parts SET unit='bundle' WHERE id=?", (pid,))
    conn.commit()
    assert c.patch(f"/api/parts/{pid}", json={"unit": "bundle", "category": "Cabling"}).status_code == 200
    assert c.patch(f"/api/parts/{pid}", json={"unit": "skein"}).status_code == 400


def test_csv_rejects_rows_with_an_unknown_unit(tmp_path):
    c, _ = make(tmp_path)
    csv = "name,unit\nGood,roll\nAlso good,Each\nBad,bundle\n"
    chk = import_csv(c, csv, dry=True)
    assert chk["created"] == 2 and chk["skipped"] == 1 and "Unit must be one of" in chk["errors"][0]
    assert not [p for p in c.get("/api/parts?all=1").json["parts"] if p["name"] == "Good"]
    r = import_csv(c, csv)
    assert r["created"] == 2 and r["skipped"] == 1
    assert get(c, "Also good")["unit"] == "ea" and not [p for p in c.get("/api/parts?all=1").json["parts"] if p["name"] == "Bad"]


def test_add_form_fields_category_and_reorder(tmp_path):
    c, _ = make(tmp_path)
    p = part(c, name="Jack", category="Cabling", reorder_at="10", on_hand="4", vendor="Graybar")
    assert (p["category"], p["reorder_at"], p["vendor"], p["low"]) == ("Cabling", 10, "Graybar", True)


def test_pack_size_and_receiving_whole_packs(tmp_path):
    c, _ = make(tmp_path)
    p = part(c, name="Telephone jack", on_hand="10", pack_size="25", pack_unit="bag")
    assert (p["pack_size"], p["pack_unit"], p["unit"]) == (25, "bag", "ea")
    r = c.post(f"/api/parts/{p['id']}/adjust", json={"packs": "2"}).json
    assert r["on_hand"] == 60
    assert c.post(f"/api/parts/{p['id']}/adjust", json={"packs": ""}).status_code == 400      # needs delta, set or packs
    plain = part(c, name="Plain", on_hand="1")
    r = c.post(f"/api/parts/{plain['id']}/adjust", json={"packs": 1})
    assert r.status_code == 400 and "pack size" in r.json["error"]
    assert part(c, name="Loose", pack_size="12")["pack_unit"] == "pack"                        # default buy unit
    assert c.post("/api/parts", json={"name": "X", "pack_size": "5", "pack_unit": "crate"}).status_code == 400
    c.patch(f"/api/parts/{p['id']}", json={"pack_size": ""})
    assert get(c, "Telephone jack")["pack_size"] is None and get(c, "Telephone jack")["pack_unit"] == ""


def test_shopping_list_rounds_up_to_whole_packs(tmp_path):
    c, _ = make(tmp_path)
    part(c, name="Jack", on_hand="3", reorder_at="10", pack_size="25", pack_unit="bag", vendor="Graybar")
    part(c, name="Valve", on_hand="1", reorder_at="4", vendor="Graybar")
    items = {i["name"]: i for i in c.get("/api/parts/shopping").json["groups"][0]["items"]}
    assert items["Jack"]["packs"] == 1 and items["Jack"]["suggest"] == 25 and items["Jack"]["pack_unit"] == "bag"
    assert items["Valve"]["packs"] is None and items["Valve"]["suggest"] == 7


def test_csv_round_trips_pack_columns(tmp_path):
    c, _ = make(tmp_path)
    part(c, name="Jack", pack_size="25", pack_unit="bag")
    out = c.get("/api/parts/export.csv").get_data(as_text=True)
    assert out.splitlines()[0].endswith("pack_size,pack_unit,shelf,bin,vendor_item,vendor_unit_cost,qbo_item") and "25,bag" in out
    again = import_csv(c, out)
    assert again["updated"] == 1 and not again["errors"]
    assert import_csv(c, c.get("/api/parts/template.csv").get_data(as_text=True))["skipped"] == 0
    assert get(c, "Telephone jack")["pack_size"] == 25


def test_dropdown_choices_come_from_parts_and_vendor_places(tmp_path):
    c, _ = make(tmp_path)
    part(c, name="A", vendor="Ferguson", category="Plumbing")
    part(c, name="B", vendor="ferguson ", category="Cabling")
    c.post("/api/places", json={"name": "Home Depot", "kind": "vendor", "lat": 34.0, "lon": -118.0})
    c.post("/api/places", json={"name": "My House", "kind": "home", "lat": 34.1, "lon": -118.1})
    ch = c.get("/api/parts/choices").json
    assert "Home Depot" in ch["vendors"] and "Ferguson" in ch["vendors"] and "My House" not in ch["vendors"]
    assert ch["categories"] == ["Cabling", "Plumbing"]
    assert [u["value"] for u in ch["units"]] == ["ea", "pair", "pack", "box", "bag", "case", "roll", "ft", "gal", "lb"]
