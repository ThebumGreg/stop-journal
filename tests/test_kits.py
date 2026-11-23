"""Install kits: a named set of parts added to a stop in one tap."""
from tests.test_export import D, make


def test_kit_crud_and_add_to_stop(tmp_path):
    c = make(tmp_path)
    flex = c.post("/api/parts", json={"name": "Flex connector", "unit_cost": "12", "on_hand": "3"}).json
    tp = c.post("/api/parts", json={"name": "T&P valve", "unit_cost": "25", "on_hand": "1"}).json
    tape = c.post("/api/parts", json={"name": "PTFE tape", "type": "consumable", "unit": "roll", "on_hand": "5"}).json
    park = c.post("/api/parts", json={"name": "Parking", "type": "expense"}).json
    k = c.post("/api/kits", json={"name": "Water heater install", "items": [
        {"part_id": flex["id"], "qty": 2}, {"part_id": tp["id"], "qty": 1}, {"part_id": tape["id"], "qty": 1}]}).json
    assert k["price"] == 49 and k["short"] == [] and len(k["items"]) == 3
    assert c.post("/api/kits", json={"name": "water heater install", "items": [{"part_id": flex["id"], "qty": 1}]}).status_code == 400
    assert c.post("/api/kits", json={"name": "X", "items": [{"part_id": park["id"], "qty": 1}]}).status_code == 400
    assert c.post("/api/kits", json={"name": "Empty", "items": []}).status_code == 400
    sid = c.get(f"/api/day/{D}").json["stops"][0]["id"]
    r = c.post(f"/api/stops/{sid}/kits/{k['id']}", json={}).json
    used = {p["name"]: p["qty"] for p in next(s for s in r["stops"] if s["id"] == sid)["parts"]}
    assert used == {"Flex connector": 2, "T&P valve": 1, "PTFE tape": 1}
    kits = c.get("/api/kits").json["kits"]
    assert kits[0]["short"] == ["Flex connector", "T&P valve"]          # 1 flex and 0 valves left: not enough for another
    c.post(f"/api/stops/{sid}/kits/{k['id']}", json={"times": 2})
    stock = {p["name"]: p["on_hand"] for p in c.get("/api/parts").json["parts"]}
    assert stock["Flex connector"] == -3 and stock["T&P valve"] == -2  # stock may go negative, as with any part
    k2 = c.put(f"/api/kits/{k['id']}", json={"name": "WH install", "notes": "40 gal", "items": [{"part_id": flex["id"], "qty": 1}]}).json
    assert k2["name"] == "WH install" and len(k2["items"]) == 1
    assert c.delete(f"/api/kits/{k['id']}").status_code == 200 and c.get("/api/kits").json["kits"] == []
