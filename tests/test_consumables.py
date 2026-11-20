import io

from app import db
from tests.test_parts import D, make, on_hand, part, stop_ids


def parts_api(c, **q):
    qs = "&".join(f"{k}={v}" for k, v in q.items())
    return c.get("/api/parts?" + qs).json["parts"]


def test_consumables_default_to_not_billable_and_filter(tmp_path):
    c, _ = make(tmp_path)
    t = part(c, name="PTFE tape", type="consumable", on_hand="30", unit="roll", vendor="Home Depot")
    v = part(c, name="Valve", sku="V1", on_hand="5")
    assert t["kind"] == "consumable" and t["billable"] is False and t["vendor"] == "Home Depot"
    assert v["kind"] == "part" and v["billable"] is True
    assert [p["name"] for p in parts_api(c, kind="consumable")] == ["PTFE tape"]
    assert [p["name"] for p in parts_api(c, kind="part")] == ["Valve"]
    assert len(parts_api(c)) == 2
    assert [p["name"] for p in parts_api(c, q="depot")] == ["PTFE tape"]      # vendor search
    forced = part(c, name="Solvent", type="consumable", billable=True)
    assert forced["billable"] is True                                          # explicit wins
    assert c.post("/api/parts", json={"name": "X", "type": "gadget"}).status_code == 400
    c.patch(f"/api/parts/{v['id']}", json={"type": "consumable"})
    assert parts_api(c, kind="consumable")[-1]["name"] in ("Valve", "PTFE tape")


def test_csv_type_and_vendor_round_trip(tmp_path):
    c, _ = make(tmp_path)
    csv = ("Item,Part Number,Type,Supplier,Qty,Min\n"
           "Wire nuts,WN-1,Supplies,Home Depot,200,50\n"
           "Ball valve,BV-1,part,Ferguson,10,3\n")
    r = c.post("/api/parts/import", data={"file": (io.BytesIO(csv.encode()), "p.csv")},
               content_type="multipart/form-data").json
    assert r["created"] == 2 and not r["errors"], r
    items = {p["name"]: p for p in parts_api(c)}
    assert items["Wire nuts"]["kind"] == "consumable" and items["Wire nuts"]["billable"] is False
    assert items["Wire nuts"]["vendor"] == "Home Depot" and items["Ball valve"]["billable"] is True
    out = c.get("/api/parts/export.csv").get_data(as_text=True)
    assert out.splitlines()[0].startswith("sku,name,type,unit,unit_cost,billable,category,vendor")
    again = c.post("/api/parts/import", data={"file": (io.BytesIO(out.encode()), "p.csv")},
                   content_type="multipart/form-data").json
    assert again["created"] == 0 and again["updated"] == 2 and not again["errors"]
    bad = c.post("/api/parts/import", data={"file": (io.BytesIO(b"name,type\nA,gadget\n"), "p.csv")},
                 content_type="multipart/form-data").json
    assert bad["skipped"] == 1


def test_typed_part_is_matched_by_a_later_import(tmp_path):
    c, _ = make(tmp_path)
    sid = stop_ids(c)[0]
    c.post(f"/api/stops/{sid}/parts", json={"name": "Brass elbow", "qty": 2})      # typed on the job
    typed = parts_api(c)[0]
    assert typed["tracked"] is False
    csv = "name,on_hand,reorder_at\nbrass elbow,40,10\n"
    r = c.post("/api/parts/import", data={"file": (io.BytesIO(csv.encode()), "p.csv")},
               content_type="multipart/form-data").json
    assert r["created"] == 0 and r["updated"] == 1
    p = parts_api(c)[0]
    assert p["id"] == typed["id"] and p["tracked"] and p["on_hand"] == 40
    day = c.get(f"/api/day/{D}").json
    assert day["stops"][0]["parts"][0]["part_id"] == typed["id"]


def test_count_sheet_used_since_and_apply(tmp_path):
    c, _ = make(tmp_path)
    tape = part(c, name="Tape", type="consumable", on_hand="30")["id"]
    sid = stop_ids(c)[0]
    c.post(f"/api/stops/{sid}/parts", json={"part_id": tape, "qty": 4})
    sheet = c.get("/api/parts/count-sheet?kind=consumable").json["parts"]
    assert sheet[0]["on_hand"] == 26 and sheet[0]["used_since"] == 4 and sheet[0]["last_counted"] is None
    r = c.post("/api/parts/count", json={"counts": {str(tape): "20"}}).json
    assert r["changed"] == 1 and r["details"][0]["diff"] == -6 and on_hand(c, tape) == 20
    sheet = c.get("/api/parts/count-sheet").json["parts"]
    assert sheet[0]["used_since"] == 0 and sheet[0]["last_counted"]
    same = c.post("/api/parts/count", json={"counts": {str(tape): "20"}}).json
    assert same["changed"] == 0 and same["unchanged"] == 1
    assert c.post("/api/parts/count", json={"counts": {}}).status_code == 400
    assert c.post("/api/parts/count", json={"counts": {str(tape): "abc"}}).status_code == 400


def test_shopping_list_groups_by_vendor(tmp_path):
    c, _ = make(tmp_path)
    part(c, name="Tape", type="consumable", on_hand="3", reorder_at="10", vendor="Home Depot")
    part(c, name="Valve", sku="V1", on_hand="1", reorder_at="4", vendor="Ferguson")
    part(c, name="Pipe", on_hand="2", reorder_at="5")                       # no vendor
    part(c, name="Plenty", on_hand="50", reorder_at="5", vendor="Ferguson")  # not low
    g = c.get("/api/parts/shopping").json["groups"]
    assert [x["vendor"] for x in g] == ["Ferguson", "Home Depot", "No vendor set"]
    tape = g[1]["items"][0]
    assert tape["suggest"] == 17 and tape["kind"] == "consumable"           # 2*10 - 3


def test_report_separates_consumables(tmp_path):
    c, _ = make(tmp_path)
    ids = stop_ids(c)
    valve = part(c, name="Valve", sku="V1", on_hand="9")["id"]
    tape = part(c, name="Tape", type="consumable", on_hand="9")["id"]
    c.patch(f"/api/stops/{ids[0]}", json={"business_name": "Acme", "status": "Confirmed"})
    c.post(f"/api/stops/{ids[0]}/parts", json={"part_id": valve, "qty": 1})
    c.post(f"/api/stops/{ids[0]}/parts", json={"part_id": tape, "qty": 2})
    j = c.get(f"/api/export.json?from={D}").json
    stop = j["days"][0]["stops"][0]
    assert [p["name"] for p in stop["parts_used"]] == ["Valve"]
    assert [p["name"] for p in stop["consumables_used"]] == ["Tape"]
    assert stop["consumables_used"][0]["billable"] is False
    assert j["records_to_update"][0]["parts_used"] == ["1 x Valve (V1)"]
    assert c.get(f"/api/export.pdf?from={D}").status_code == 200


def test_old_database_gets_the_new_columns(tmp_path):
    import sqlite3
    path = tmp_path / "stop_journal.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE parts (id INTEGER PRIMARY KEY, sku TEXT, name TEXT NOT NULL, unit TEXT, "
                "unit_cost REAL, billable INTEGER, category TEXT, on_hand REAL, reorder_at REAL, "
                "tracked INTEGER, active INTEGER)")
    old.execute("INSERT INTO parts(name,billable,tracked,active) VALUES ('Old part',1,0,1)")
    old.commit()
    old.close()
    conn = db.connect(str(tmp_path))
    row = conn.execute("SELECT kind, vendor FROM parts").fetchone()
    assert row["kind"] == "part" and row["vendor"] is None


def test_csv_check_changes_nothing(tmp_path):
    c, _ = make(tmp_path)
    csv = ("name,sku,type,unit_cost,on_hand\r\nPreview valve,PV-1,part,5.50,3\r\n"
           "Preview tape,PT-1,consumable,1.00,9\r\n,,,,\r\nBad,PB-1,part,abc,1\r\n")
    r = c.post("/api/parts/import?dry=1", data=csv.encode(), content_type="text/csv").json
    assert r["dry_run"] and r["created"] == 2 and r["skipped"] == 1 and r["errors"]
    assert c.get("/api/parts?all=1").json == c.get("/api/parts?all=1").json
    assert not [p for p in c.get("/api/parts?all=1").json["parts"] if p["sku"] in ("PV-1", "PT-1")]
    r = c.post("/api/parts/import", data=csv.encode(), content_type="text/csv").json
    assert r["created"] == 2 and "dry_run" not in r
    assert len([p for p in c.get("/api/parts?all=1").json["parts"] if p["sku"] in ("PV-1", "PT-1")]) == 2
