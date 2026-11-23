"""Install kits: a named set of parts ("Water heater install": 2 flex connectors, 1 T&P valve, 1 roll tape) added to a
stop in one tap. Each line goes on the stop like a part typed by hand (catalog price, stock moves, one line per part, an
existing line on the stop gets the quantity added). Expenses cannot be in a kit (each receipt has its own amount)."""
from . import parts
from .errors import EditError


def _items(conn, kid):
    out = []
    for r in conn.execute("SELECT ki.id, ki.part_id, ki.qty, p.name, p.sku, p.unit, p.unit_cost, p.tracked, p.on_hand, "
                          "p.active, p.kind FROM kit_items ki JOIN parts p ON p.id=ki.part_id WHERE ki.kit_id=? "
                          "ORDER BY ki.id", (kid,)):
        out.append({"part_id": r["part_id"], "qty": r["qty"], "name": r["name"], "sku": r["sku"] or "",
                    "unit": r["unit"] or "ea", "unit_cost": r["unit_cost"], "active": bool(r["active"]),
                    "kind": r["kind"] or "part",
                    "short": bool(r["tracked"] and (r["on_hand"] or 0) < r["qty"]), "on_hand": r["on_hand"]})
    return out


def kit_dict(conn, r):
    items = _items(conn, r["id"])
    cost = [i["unit_cost"] * i["qty"] for i in items if i["unit_cost"] is not None and i["kind"] == "part"]
    return {"id": r["id"], "name": r["name"], "notes": r["notes"] or "", "items": items,
            "price": round(sum(cost), 2) if cost else None, "short": [i["name"] for i in items if i["short"]]}


def list_kits(conn):
    return [kit_dict(conn, r) for r in conn.execute("SELECT * FROM kits ORDER BY lower(name)")]


def get_kit(conn, kid):
    r = conn.execute("SELECT * FROM kits WHERE id=?", (kid,)).fetchone()
    if not r:
        raise EditError("That kit no longer exists.")
    return r


def save_kit(conn, f, kid=None):
    """Create or replace a kit: {name, notes, items: [{part_id, qty}]}."""
    name = str(f.get("name") or "").strip()
    if not name:
        raise EditError("A kit needs a name.")
    if len(name) > 80:
        raise EditError("The kit name is too long (80 characters at most).")
    clash = conn.execute("SELECT id FROM kits WHERE name=?", (name,)).fetchone()
    if clash and clash["id"] != kid:
        raise EditError(f"There is already a kit called “{name}”.")
    items = []
    for it in f.get("items") or []:
        p = parts.get_part(conn, int(it.get("part_id") or 0))
        if (p["kind"] or "part") == "expense":
            raise EditError(f"“{p['name']}” is an expense; kits hold parts and consumables only.")
        q = parts._num(it.get("qty"), f"Quantity for {p['name']}", 0)
        if q:
            items.append((p["id"], q))
    if not items:
        raise EditError("Add at least one part to the kit.")
    notes = str(f.get("notes") or "").strip()[:500] or None
    if kid:
        get_kit(conn, kid)
        conn.execute("UPDATE kits SET name=?, notes=? WHERE id=?", (name, notes, kid))
        conn.execute("DELETE FROM kit_items WHERE kit_id=?", (kid,))
    else:
        kid = conn.execute("INSERT INTO kits(name, notes) VALUES (?,?)", (name, notes)).lastrowid
    conn.executemany("INSERT INTO kit_items(kit_id, part_id, qty) VALUES (?,?,?)", [(kid, pid, q) for pid, q in items])
    conn.commit()
    return kit_dict(conn, get_kit(conn, kid))


def delete_kit(conn, kid):
    get_kit(conn, kid)
    conn.execute("DELETE FROM kit_items WHERE kit_id=?", (kid,))
    conn.execute("DELETE FROM kits WHERE id=?", (kid,))
    conn.commit()


def add_to_stop(conn, stop_id, kid, times=1):
    """Every line of the kit onto the stop (times = how many kits). Removed parts are skipped."""
    times = parts._num(times, "How many kits", 0) or 1
    added = 0
    for it in _items(conn, get_kit(conn, kid)["id"]):
        if not it["active"]:
            continue
        parts.add_to_stop(conn, stop_id, {"part_id": it["part_id"], "qty": round(it["qty"] * times, 4)})
        added += 1
    if not added:
        raise EditError("Every part in this kit has been removed from the catalog.")
    return added
