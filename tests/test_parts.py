from dataclasses import replace

from app import create_app, db, parts, sample
from tests.test_detect import CFG

D = "2026-11-18"


def make(tmp_path):
    cfg = replace(CFG, data_dir=str(tmp_path))
    c = create_app(cfg, client=sample.ListRecorder(sample.normal_day())).test_client()
    return c, cfg


def conn_for(cfg):
    return db.connect(cfg.data_dir)


def part(c, **f):
    r = c.post("/api/parts", json=f)
    assert r.status_code == 200, r.json
    return r.json


def on_hand(c, pid):
    return next(p for p in c.get("/api/parts?all=1").json["parts"] if p["id"] == pid)["on_hand"]


def stop_ids(c):
    return [s["id"] for s in c.get(f"/api/day/{D}").json["stops"]]


# ---- catalog ----------------------------------------------------------
def test_create_validate_and_unique_sku(tmp_path):
    c, _ = make(tmp_path)
    p = part(c, name="Ball valve", sku="BV-1", unit_cost="8.75", on_hand="12")
    assert p["tracked"] and p["on_hand"] == 12 and p["billable"] and p["unit"] == "ea"
    assert c.post("/api/parts", json={"name": ""}).status_code == 400
    assert c.post("/api/parts", json={"name": "X", "unit_cost": "abc"}).status_code == 400
    assert c.post("/api/parts", json={"name": "X", "unit_cost": "-1"}).status_code == 400
    r = c.post("/api/parts", json={"name": "Other", "sku": "bv-1"})  # case-insensitive
    assert r.status_code == 400 and "already used" in r.json["error"]
    assert not part(c, name="Loose screws")["tracked"]  # no count given = not tracked


def test_adjust_and_low_flag(tmp_path):
    c, _ = make(tmp_path)
    p = part(c, name="Valve", on_hand="5", reorder_at="4")
    assert c.post(f"/api/parts/{p['id']}/adjust", json={"delta": -2}).json["low"] is True
    assert c.post(f"/api/parts/{p['id']}/adjust", json={"delta": 10, "note": "bought"}).json["on_hand"] == 13
    r = c.post(f"/api/parts/{p['id']}/adjust", json={"set": 7})
    assert r.json["on_hand"] == 7 and not r.json["low"]
    assert c.post(f"/api/parts/{p['id']}/adjust", json={}).status_code == 400
    assert c.post(f"/api/parts/{p['id']}/adjust", json={"delta": 1, "set": 2}).status_code == 400


# ---- CSV --------------------------------------------------------------
def test_csv_import_aliases_upsert_and_errors(tmp_path):
    c, cfg = make(tmp_path)
    csv1 = ("Part Number,Description,UOM,Price,Billable?,Qty,Min,Junk\r\n"
            "BV-050,1/2 in. ball valve,ea,$8.75,Yes,12,4,x\r\n"
            "TAPE,PTFE tape,roll,1.2,no,,,y\r\n"
            ",,,,,,,\r\n"
            "BAD,,ea,1,yes,1,1,z\r\n"
            "NUM,Washer,ea,abc,yes,1,1,z\r\n")
    r = c.post("/api/parts/import", data=csv1, content_type="text/csv").json
    assert (r["created"], r["updated"], r["skipped"]) == (2, 0, 2) and "Junk" in r["ignored_columns"]
    assert any("Row 5" in e for e in r["errors"]) and any("Row 6" in e for e in r["errors"])
    ps = {p["sku"]: p for p in c.get("/api/parts").json["parts"]}
    assert ps["BV-050"]["on_hand"] == 12 and ps["BV-050"]["tracked"] and ps["BV-050"]["unit_cost"] == 8.75
    assert ps["BV-050"]["reorder_at"] == 4 and ps["TAPE"]["billable"] is False and not ps["TAPE"]["tracked"]
    # re-import: matches by sku, blank cells don't overwrite, on_hand sets absolute count
    r = c.post("/api/parts/import", content_type="text/csv",
               data="sku,name,on_hand,unit_cost\nBV-050,,20,\nTAPE,,,2.5\n").json
    assert (r["created"], r["updated"]) == (0, 2)
    ps = {p["sku"]: p for p in c.get("/api/parts").json["parts"]}
    assert ps["BV-050"]["on_hand"] == 20 and ps["BV-050"]["name"] == "1/2 in. ball valve"
    assert ps["TAPE"]["unit_cost"] == 2.5 and ps["TAPE"]["name"] == "PTFE tape"
    log = conn_for(cfg).execute("SELECT reason, delta FROM inventory_log WHERE part_id=?",
                                (ps["BV-050"]["id"],)).fetchall()
    assert [(r["reason"], r["delta"]) for r in log] == [("initial", 12), ("import", 8)] or \
           [(r["reason"], r["delta"]) for r in log] == [("import", 12), ("import", 8)]


def test_csv_import_needs_name_column_and_handles_excel_encoding(tmp_path):
    c, _ = make(tmp_path)
    assert c.post("/api/parts/import", data="sku,qty\nA,1\n", content_type="text/csv").status_code == 400
    assert c.post("/api/parts/import", data=b"", content_type="text/csv").status_code == 400
    r = c.post("/api/parts/import", data="name\nCaf\xe9 fitting\n".encode("latin-1"), content_type="text/csv")
    assert r.json["created"] == 1


def test_csv_export_roundtrip_and_formula_guard(tmp_path):
    c, _ = make(tmp_path)
    part(c, name="=HYPERLINK(\"x\")", sku="Z1", on_hand="-3")
    text = c.get("/api/parts/export.csv").get_data(as_text=True)
    assert "'=HYPERLINK" in text and ",-3," in text  # text guarded, numbers untouched
    assert c.get("/api/parts/template.csv").get_data(as_text=True).startswith("sku,name")
    c2, _ = make(tmp_path / "other")
    r = c2.post("/api/parts/import", data=text, content_type="text/csv").json
    assert r["created"] == 1
    back = c2.get("/api/parts").json["parts"][0]
    assert back["name"] == '=HYPERLINK("x")' and back["on_hand"] == -3 and back["sku"] == "Z1"


# ---- per-stop usage + inventory ---------------------------------------
def test_use_part_decrements_and_remove_returns(tmp_path):
    c, _ = make(tmp_path)
    p = part(c, name="Valve", sku="V1", unit_cost="8.75", on_hand="10")
    sid = stop_ids(c)[0]
    r = c.post(f"/api/stops/{sid}/parts", json={"part_id": p["id"], "qty": 3})
    sp = r.json["stops"][0]["parts"][0]
    assert sp["qty"] == 3 and sp["line_cost"] == 26.25 and sp["name"] == "Valve" and on_hand(c, p["id"]) == 7
    c.post(f"/api/stops/{sid}/parts", json={"name": "valve", "qty": 1})  # name match, same line
    r = c.get(f"/api/day/{D}").json
    assert len(r["stops"][0]["parts"]) == 1 and r["stops"][0]["parts"][0]["qty"] == 4 and on_hand(c, p["id"]) == 6
    r = c.patch(f"/api/stop-parts/{sp['id']}", json={"qty": 1})
    assert on_hand(c, p["id"]) == 9 and r.json["stops"][0]["parts"][0]["qty"] == 1
    assert c.patch(f"/api/stop-parts/{sp['id']}", json={"qty": 0}).status_code == 400
    r = c.delete(f"/api/stop-parts/{sp['id']}")
    assert r.json["stops"][0]["parts"] == [] and on_hand(c, p["id"]) == 10


def test_snapshot_survives_catalog_edit(tmp_path):
    c, _ = make(tmp_path)
    p = part(c, name="Valve", unit_cost="5", on_hand="5")
    sid = stop_ids(c)[0]
    c.post(f"/api/stops/{sid}/parts", json={"part_id": p["id"], "qty": 1})
    c.patch(f"/api/parts/{p['id']}", json={"name": "Valve v2", "unit_cost": "9"})
    sp = c.get(f"/api/day/{D}").json["stops"][0]["parts"][0]
    assert sp["name"] == "Valve" and sp["unit_cost"] == 5


def test_adhoc_part_is_cataloged_untracked(tmp_path):
    c, _ = make(tmp_path)
    sid = stop_ids(c)[0]
    c.post(f"/api/stops/{sid}/parts", json={"name": "Odd gasket", "qty": 2})
    p = next(p for p in c.get("/api/parts").json["parts"] if p["name"] == "Odd gasket")
    assert not p["tracked"] and p["on_hand"] == 0  # no phantom negative stock
    assert c.post(f"/api/stops/{sid}/parts", json={"name": "", "qty": 1}).status_code == 400
    assert c.post(f"/api/stops/{sid}/parts", json={"name": "x", "qty": "two"}).status_code == 400


def test_stock_can_go_negative_and_is_flagged(tmp_path):
    c, _ = make(tmp_path)
    p = part(c, name="Valve", on_hand="1")
    c.post(f"/api/stops/{stop_ids(c)[0]}/parts", json={"part_id": p["id"], "qty": 3})
    got = next(x for x in c.get("/api/parts").json["parts"] if x["id"] == p["id"])
    assert got["on_hand"] == -2 and got["negative"]


def test_deleting_stop_returns_parts(tmp_path):
    c, cfg = make(tmp_path)
    p = part(c, name="Valve", on_hand="10")
    sid = stop_ids(c)[0]
    c.post(f"/api/stops/{sid}/parts", json={"part_id": p["id"], "qty": 4})
    assert on_hand(c, p["id"]) == 6
    c.delete(f"/api/stops/{sid}")
    assert on_hand(c, p["id"]) == 10
    assert conn_for(cfg).execute("SELECT count(*) c FROM stop_parts").fetchone()["c"] == 0


def test_redetect_returns_parts_of_detected_stops_only(tmp_path):
    c, _ = make(tmp_path)
    p = part(c, name="Valve", on_hand="10")
    c.post(f"/api/stops/{stop_ids(c)[0]}/parts", json={"part_id": p["id"], "qty": 2})
    m = c.post(f"/api/day/{D}/stops", json={"arrive": "13:30", "depart": "14:00"}).json["stops"][-1]
    c.post(f"/api/stops/{m['id']}/parts", json={"part_id": p["id"], "qty": 1})
    assert on_hand(c, p["id"]) == 7
    r = c.post(f"/api/day/{D}/redetect", json={}).json
    assert on_hand(c, p["id"]) == 9  # detected stop's 2 returned; manual stop keeps its 1
    assert [x["qty"] for s in r["stops"] for x in s["parts"]] == [1]


def test_merge_moves_parts(tmp_path):
    c, _ = make(tmp_path)
    ids = stop_ids(c)
    p = part(c, name="Valve", on_hand="10")
    c.post(f"/api/stops/{ids[1]}/parts", json={"part_id": p["id"], "qty": 2})
    r = c.post("/api/stops/merge", json={"ids": [ids[0], ids[1]]}).json
    assert [len(s["parts"]) for s in r["stops"]] == [1, 0] and on_hand(c, p["id"]) == 8


def test_deactivated_part_hidden_but_history_kept(tmp_path):
    c, _ = make(tmp_path)
    p = part(c, name="Old valve", on_hand="3")
    c.post(f"/api/stops/{stop_ids(c)[0]}/parts", json={"part_id": p["id"], "qty": 1})
    c.patch(f"/api/parts/{p['id']}", json={"active": False})
    assert all(x["id"] != p["id"] for x in c.get("/api/parts").json["parts"])
    assert c.get(f"/api/day/{D}").json["stops"][0]["parts"][0]["name"] == "Old valve"
    assert c.get("/api/parts?q=old&all=1").json["parts"][0]["active"] is False


def test_pages_and_login_gate(tmp_path):
    cfg = replace(CFG, data_dir=str(tmp_path), app_password="pw")
    c = create_app(cfg, client=sample.ListRecorder([])).test_client()
    assert c.get("/truck").status_code == 302
    assert c.get("/api/parts").status_code == 401
    assert c.post("/api/parts/import", data="name\nx\n").status_code == 401
    c.post("/login", data={"password": "pw"})
    assert c.get("/truck").status_code == 200 and c.get("/api/parts").status_code == 200


def test_part_page_history_and_truck_tabs(tmp_path):
    from tests.test_export import D, make
    c = make(tmp_path)
    p = c.post("/api/parts", json={"name": "Ball valve", "unit_cost": "8.75", "on_hand": "10"}).json
    sid = c.get(f"/api/day/{D}").json["stops"][0]["id"]
    c.post(f"/api/stops/{sid}/parts", json={"part_id": p["id"], "qty": 2})
    c.post(f"/api/parts/{p['id']}/adjust", json={"delta": 5, "note": "Ferguson"})
    h = c.get(f"/api/parts/{p['id']}/history").json
    assert h["part"]["on_hand"] == 13 and h["totals"] == {"used": 2, "stops": 1}
    assert h["uses"][0]["url"] == f"/day/{D}?stop={sid}"
    reasons = [x["reason"] for x in h["log"]]
    assert "Used on a stop" in reasons and "Stock added" in reasons and h["log"][0]["note"] == "Ferguson"
    page = c.get("/truck").data.decode()
    for tab in ('href="#parts"', 'href="#stock"', 'href="#fuel"', 'href="#data"'):
        assert tab in page
    assert ">Reports<" in page and "Import / export</a>" in page


def test_bin_location_sorts_count_and_similar_parts(tmp_path):
    from tests.test_export import make
    c = make(tmp_path)
    for name, b in [("Zip ties", "Shelf 10"), ("Ball valve 1/2 in.", "Shelf 2"), ("Wire nuts", "")]:
        c.post("/api/parts", json={"name": name, "on_hand": "5", "bin": b, "sku": name[:3].upper() + "-1"})
    assert [p["bin"] for p in c.get("/api/parts/count-sheet").json["parts"]] == ["Shelf 2", "Shelf 10", ""]
    sim = c.get("/api/parts/similar?name=1/2 ball valve").json["parts"]
    assert sim[0]["name"] == "Ball valve 1/2 in." and sim[0]["why"] == "similar name"
    assert c.get("/api/parts/similar?sku=zip 1").json["parts"][0]["why"] == "same part #"
    assert c.get("/api/parts/similar?name=Hammer").json["parts"] == []
    csv = c.get("/api/parts/export.csv").data.decode()
    assert csv.splitlines()[0].endswith(",bin") and "Shelf 10" in csv


def test_mark_bought_from_shopping_list(tmp_path):
    from tests.test_export import make
    c = make(tmp_path)
    nuts = c.post("/api/parts", json={"name": "Wire nuts", "on_hand": "3", "reorder_at": "10", "pack_size": "25",
                                      "pack_unit": "bag", "vendor": "Graybar"}).json
    tape = c.post("/api/parts", json={"name": "Tape", "on_hand": "1", "reorder_at": "2", "vendor": "Graybar"}).json
    g = c.get("/api/parts/shopping").json["groups"][0]
    assert g["vendor"] == "Graybar" and {i["name"] for i in g["items"]} == {"Wire nuts", "Tape"}
    r = c.post("/api/parts/bought", json={"vendor": "Graybar", "items": [{"id": nuts["id"], "packs": 1}, {"id": tape["id"], "qty": 3}]})
    assert r.status_code == 200 and [b["added"] for b in r.json["bought"]] == [25, 3]
    assert c.get("/api/parts/shopping").json["groups"] == []
    log = c.get(f"/api/parts/{nuts['id']}/history").json["log"][0]
    assert log["reason"] == "Bought" and log["note"] == "Bought at Graybar (1 x bag of 25)"
    assert c.post("/api/parts/bought", json={"items": [{"id": tape["id"], "qty": 0}]}).status_code == 400
    assert c.post("/api/parts/bought", json={"items": [{"id": tape["id"], "packs": 1}]}).status_code == 400   # no pack size
