"""Vendors: one record per vendor NAME, covering all its branches (saved vendor places with that name) and every part
whose `vendor` is that name. The `vendors` table only holds what you add (account #, phone, contact, website, notes).

Rename merges: renaming "HomeDepot" to "Home Depot" moves its parts and places to the existing vendor.
"""
from . import parts, service
from .errors import EditError
from .places import known

FIELDS = ("account", "phone", "contact", "website", "notes")


def _key(name):
    return (name or "").strip().lower()


def names(conn):
    out = {}
    for r in conn.execute("SELECT name FROM known_sites WHERE kind='vendor' AND COALESCE(name,'')!='' UNION ALL "
                          "SELECT vendor FROM parts WHERE COALESCE(vendor,'')!='' AND active=1 UNION ALL "
                          "SELECT name FROM vendors"):
        out.setdefault(_key(r[0]), r[0].strip())
    return out


def list_vendors(conn):
    sites, parts_by, low_by = {}, {}, {}
    for r in conn.execute("SELECT * FROM known_sites WHERE kind='vendor'"):
        sites.setdefault(_key(r["name"]), []).append(r)
    for p in parts.list_parts(conn):
        if p["vendor"]:
            parts_by[_key(p["vendor"])] = parts_by.get(_key(p["vendor"]), 0) + 1
            if p["low"]:
                low_by[_key(p["vendor"])] = low_by.get(_key(p["vendor"]), 0) + 1
    out = []
    for k, name in names(conn).items():
        last = None
        ids = [s["id"] for s in sites.get(k, [])]
        if ids:
            r = conn.execute("SELECT MAX(date) FROM stops WHERE known_site_id IN (%s)" % ",".join("?" * len(ids)), ids).fetchone()
            last = r[0]
        out.append({"name": name, "locations": len(ids), "parts": parts_by.get(k, 0), "low": low_by.get(k, 0),
                    "last_visit": last})
    return sorted(out, key=lambda v: (-v["low"], v["name"].lower()))


def _info(conn, name):
    r = conn.execute("SELECT * FROM vendors WHERE name=?", (name.strip(),)).fetchone()
    return {k: (r[k] or "") if r else "" for k in FIELDS}


def detail(conn, cfg, name):
    k = _key(name)
    all_names = names(conn)
    if k not in all_names:
        raise EditError("No vendor with that name.")
    display = all_names[k]
    sites = conn.execute("SELECT * FROM known_sites WHERE kind='vendor' AND lower(trim(name))=?", (k,)).fetchall()
    ps = [p for p in parts.list_parts(conn) if _key(p["vendor"]) == k]
    visits, totals = (service._history(conn, cfg, sites, 50) if sites else ([], {"visits": 0, "minutes": 0, "last": None}))
    spent = conn.execute(
        "SELECT COALESCE(SUM(sp.unit_cost*sp.qty),0) FROM stop_parts sp JOIN stops s ON s.id=sp.stop_id "
        "WHERE sp.kind='expense' AND (lower(trim(s.business_name))=? OR s.known_site_id IN (%s))"
        % ",".join("?" * max(len(sites), 1)), (k, *([x["id"] for x in sites] or [-1]))).fetchone()[0]
    ids = [p["id"] for p in ps]
    bought = []
    if ids:
        for r in conn.execute("SELECT l.*, p.name, p.unit FROM inventory_log l JOIN parts p ON p.id=l.part_id "
                              "WHERE l.reason='bought' AND l.part_id IN (%s) ORDER BY l.ts DESC LIMIT 30"
                              % ",".join("?" * len(ids)), ids):
            bought.append({"ts": r["ts"], "part_id": r["part_id"], "name": r["name"], "qty": r["delta"],
                           "unit": r["unit"] or "ea", "note": r["note"] or ""})
    shop = next((g for g in parts.shopping_list(conn) if _key(g["vendor"]) == k), None)
    pu = parts.purchases(conn, vendor=display, limit=50)
    return {"name": display, "info": _info(conn, display),
            "locations": [known.site_dict(x) for x in sites],
            "parts": ps, "low": shop["items"] if shop else [],
            "visits": visits, "totals": dict(totals, expenses=round(spent or 0, 2), parts=len(ps), purchased=pu["total"]),
            "bought": bought, "purchases": pu["purchases"]}


def save(conn, name, f):
    """Save details, and optionally rename (merging into another vendor with that name)."""
    old = (name or "").strip()
    if _key(old) not in names(conn):
        raise EditError("No vendor with that name.")
    new = str(f.get("rename") or "").strip() or old
    if len(new) > 80:
        raise EditError("The vendor name is too long (80 characters at most).")
    vals = {k: str(f[k]).strip()[:500] if f.get(k) is not None else None for k in FIELDS if k in f}
    if _key(new) != _key(old) or new != old:
        conn.execute("UPDATE parts SET vendor=? WHERE lower(trim(vendor))=?", (new, _key(old)))
        conn.execute("UPDATE known_sites SET name=? WHERE kind='vendor' AND lower(trim(name))=?", (new, _key(old)))
        conn.execute("UPDATE purchases SET vendor=? WHERE lower(trim(vendor))=?", (new, _key(old)))
        mine = conn.execute("SELECT * FROM vendors WHERE name=?", (old,)).fetchone()
        theirs = conn.execute("SELECT * FROM vendors WHERE name=?", (new,)).fetchone()
        if mine and theirs and _key(new) != _key(old):      # merging: keep their details, fill gaps from mine
            for k in FIELDS:
                if not theirs[k] and mine[k] and k not in vals:
                    vals[k] = mine[k]
            conn.execute("DELETE FROM vendors WHERE id=?", (mine["id"],))
        elif mine:
            conn.execute("UPDATE vendors SET name=? WHERE id=?", (new, mine["id"]))
    conn.execute("INSERT INTO vendors(name) VALUES (?) ON CONFLICT(name) DO NOTHING", (new,))
    if vals:
        conn.execute("UPDATE vendors SET " + ", ".join(f"{k}=?" for k in vals) + " WHERE name=?", (*vals.values(), new))
    conn.commit()
    return new
