from tests.test_parts import D, make, on_hand, part, stop_ids


def day(c):
    return c.get(f"/api/day/{D}").json


def test_undo_delete_restores_stop_edits_parts_and_stock(tmp_path):
    c, _ = make(tmp_path)
    ids = stop_ids(c)
    pid = part(c, name="Valve", sku="V1", on_hand="10")["id"]
    c.patch(f"/api/stops/{ids[0]}", json={"business_name": "Acme", "notes": "fixed it"})
    c.post(f"/api/stops/{ids[0]}/parts", json={"part_id": pid, "qty": 3})
    assert on_hand(c, pid) == 7
    before = day(c)
    assert before["undo"] is None

    c.delete(f"/api/stops/{ids[0]}")
    assert on_hand(c, pid) == 10 and len(day(c)["stops"]) == len(before["stops"]) - 1
    assert day(c)["undo"]["label"] == "Deleted a stop"

    r = c.post(f"/api/day/{D}/undo")
    assert r.status_code == 200
    after = day(c)
    assert on_hand(c, pid) == 7
    assert [s["id"] for s in after["stops"]] == [s["id"] for s in before["stops"]]
    first = after["stops"][0]
    assert first["business_name"] == "Acme" and first["notes"] == "fixed it"
    assert first["parts"][0]["qty"] == 3
    assert len(after["drives"]) == len(before["drives"])
    assert after["undo"] is None
    assert c.post(f"/api/day/{D}/undo").status_code == 400


def test_undo_merge_split_and_redetect(tmp_path):
    c, _ = make(tmp_path)
    ids = stop_ids(c)
    n = len(ids)
    s0 = day(c)["stops"][0]
    mid = s0["arrive"][:2] + ":" + f"{(int(s0['arrive'][3:]) + 5) % 60:02d}"
    # split then undo
    r = c.post(f"/api/stops/{ids[0]}/split", json={"at": mid})
    if r.status_code == 200:
        assert len(day(c)["stops"]) == n + 1
        c.post(f"/api/day/{D}/undo")
        assert len(day(c)["stops"]) == n
    # re-detect then undo keeps the edit made before
    c.patch(f"/api/stops/{ids[0]}", json={"business_name": "Keep me"})
    c.post(f"/api/day/{D}/redetect", json={})
    assert day(c)["stops"][0]["business_name"] != "Keep me"
    c.post(f"/api/day/{D}/undo")
    assert day(c)["stops"][0]["business_name"] == "Keep me"


def test_undo_history_is_capped(tmp_path):
    c, cfg = make(tmp_path)
    for _ in range(12):
        c.post(f"/api/day/{D}/redetect", json={})
    from app import db
    conn = db.connect(cfg.data_dir)
    assert conn.execute("SELECT COUNT(*) FROM undo_log").fetchone()[0] == 10


def test_undo_merge(tmp_path):
    c, _ = make(tmp_path)
    ids = stop_ids(c)
    n = len(ids)
    if n < 2:
        return
    c.patch(f"/api/stops/{ids[1]}", json={"notes": "second"})
    r = c.post("/api/stops/merge", json={"ids": [ids[0], ids[1]]})
    assert r.status_code == 200, r.json
    assert len(day(c)["stops"]) == n - 1
    c.post(f"/api/day/{D}/undo")
    after = day(c)["stops"]
    assert len(after) == n and after[1]["notes"] == "second"


def test_confirm_known_single_and_bulk_with_undo(tmp_path):
    c, _ = make(tmp_path)
    d0 = day(c)
    assert all(s["suggest"] is None for s in d0["stops"])      # nothing saved yet
    first, second = d0["stops"][0], d0["stops"][1]
    c.post("/api/places", json={"name": "Acme Co", "kind": "client", "lat": first["lat"],
                                "lon": first["lon"], "address": "1 Dock Rd"})
    c.post("/api/places", json={"name": "Shop", "kind": "vendor", "lat": second["lat"],
                                "lon": second["lon"]})
    d1 = day(c)
    assert d1["stops"][0]["suggest"]["name"] == "Acme Co"
    r = c.post(f"/api/stops/{first['id']}/confirm-known")
    assert r.status_code == 200
    s = next(x for x in day(c)["stops"] if x["id"] == first["id"])
    assert s["status"] == "Confirmed" and s["business_name"] == "Acme Co" and s["address"] == "1 Dock Rd"
    bulk = c.post(f"/api/day/{D}/confirm-known")
    assert bulk.status_code == 200
    assert all(x["status"] != "Draft" or x["suggest"] is None for x in day(c)["stops"] if x["kind"] == "stop")
    assert c.post(f"/api/day/{D}/confirm-known").status_code == 400   # nothing left
    c.post(f"/api/day/{D}/undo")
    assert next(x for x in day(c)["stops"] if x["id"] == second["id"])["status"] == "Draft"
