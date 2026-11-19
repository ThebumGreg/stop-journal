"""Parts catalog + truck inventory.

* `parts` is both the catalog and the truck stock (on_hand). Only parts with
  tracked=1 move stock; ad-hoc parts typed into a stop are added untracked.
* Every stock change is written to inventory_log with a reason
  (initial, import, restock, adjust, count, used, return).
* Stock may go negative (the truck count is often behind reality); the UI flags it.

CSV import format (header row required, order free, extra columns ignored):
  name (required), sku, unit, unit_cost, billable, category, on_hand, reorder_at
Common alternative headers are accepted (see ALIASES). Rows are matched to an
existing part by sku, else by name; blank cells never overwrite existing values.
A non-blank on_hand sets the truck count and switches tracking on.
"""
import csv
import io
import math
import time

from .errors import EditError

COLUMNS = ["sku", "name", "unit", "unit_cost", "billable", "category", "on_hand", "reorder_at"]
ALIASES = {
    "part_number": "sku", "part_no": "sku", "partnumber": "sku", "part_#": "sku",
    "item_number": "sku", "item_no": "sku", "item_#": "sku",
    "description": "name", "part_name": "name", "item_name": "name", "item": "name", "part": "name",
    "uom": "unit", "units": "unit",
    "cost": "unit_cost", "price": "unit_cost", "unit_price": "unit_cost", "unitcost": "unit_cost",
    "qty": "on_hand", "quantity": "on_hand", "onhand": "on_hand", "stock": "on_hand",
    "in_stock": "on_hand", "truck_qty": "on_hand", "qty_on_hand": "on_hand",
    "reorder": "reorder_at", "reorder_point": "reorder_at", "min": "reorder_at",
    "minimum": "reorder_at", "min_qty": "reorder_at", "par": "reorder_at",
    "type": "category", "group": "category",
}
MAX_ROWS = 20000
TRUE = {"1", "y", "yes", "true", "t", "billable"}
FALSE = {"0", "n", "no", "false", "f", "nonbillable", "non-billable", "non_billable"}


# ---- parsing helpers ---------------------------------------------------
def _num(v, what, minimum=None):
    if v is None:
        return None
    t = str(v).strip().replace("$", "").replace(",", "")
    if t == "":
        return None
    try:
        f = float(t)
    except ValueError:
        raise EditError(f"{what} must be a number.")
    if not math.isfinite(f):
        raise EditError(f"{what} must be a number.")
    if minimum is not None and f < minimum:
        raise EditError(f"{what} must be at least {minimum:g}.")
    return round(f, 4)


def _bool(v):
    if isinstance(v, bool):
        return v
    t = str(v if v is not None else "").strip().lower()
    if t == "":
        return None
    if t in TRUE:
        return True
    if t in FALSE:
        return False
    raise EditError("Billable must be yes or no.")


def _text(v, what, limit=200, required=False):
    t = str(v if v is not None else "").strip()
    if required and not t:
        raise EditError(f"{what} is required.")
    if len(t) > limit:
        raise EditError(f"{what} is too long (max {limit}).")
    return t


def part_dict(r):
    tracked = bool(r["tracked"])
    low = (tracked and r["reorder_at"] is not None and r["on_hand"] <= r["reorder_at"])
    return {"id": r["id"], "sku": r["sku"] or "", "name": r["name"], "unit": r["unit"] or "ea",
            "unit_cost": r["unit_cost"], "billable": bool(r["billable"]),
            "category": r["category"] or "", "on_hand": r["on_hand"] or 0,
            "reorder_at": r["reorder_at"], "tracked": tracked, "active": bool(r["active"]),
            "low": bool(low), "negative": bool(tracked and (r["on_hand"] or 0) < 0)}


# ---- catalog -----------------------------------------------------------
def list_parts(conn, q="", include_inactive=False):
    rows = conn.execute("SELECT * FROM parts" + ("" if include_inactive else " WHERE active=1") +
                        " ORDER BY lower(name)").fetchall()
    q = q.strip().lower()
    return [part_dict(r) for r in rows
            if not q or q in r["name"].lower() or q in (r["sku"] or "").lower()
            or q in (r["category"] or "").lower()]


def get_part(conn, pid):
    r = conn.execute("SELECT * FROM parts WHERE id=?", (pid,)).fetchone()
    if not r:
        raise EditError("That part no longer exists.")
    return r


def find_part(conn, name=None, sku=None):
    if sku and str(sku).strip():
        r = conn.execute("SELECT * FROM parts WHERE lower(sku)=lower(?)", (str(sku).strip(),)).fetchone()
        if r:
            return r
    if name and str(name).strip():
        # a typed sku in the name box should also match
        r = conn.execute("SELECT * FROM parts WHERE lower(name)=lower(?) ORDER BY active DESC",
                         (str(name).strip(),)).fetchone()
        if r:
            return r
        return conn.execute("SELECT * FROM parts WHERE lower(sku)=lower(?)",
                            (str(name).strip(),)).fetchone()
    return None


def _check_sku_free(conn, sku, own_id=None):
    if not sku:
        return
    r = conn.execute("SELECT id, name FROM parts WHERE lower(sku)=lower(?)", (sku,)).fetchone()
    if r and r["id"] != own_id:
        raise EditError(f"Part number {sku} is already used by “{r['name']}”.")


def create_part(conn, f, tracked=False):
    name = _text(f.get("name"), "Name", required=True)
    sku = _text(f.get("sku"), "Part number", 80) or None
    _check_sku_free(conn, sku)
    cost = _num(f.get("unit_cost"), "Unit cost", 0)
    billable = _bool(f.get("billable"))
    on_hand = _num(f.get("on_hand"), "On hand")
    reorder = _num(f.get("reorder_at"), "Reorder level", 0)
    if on_hand is not None:
        tracked = True
    cur = conn.execute(
        "INSERT INTO parts(sku,name,unit,unit_cost,billable,category,on_hand,reorder_at,tracked,active)"
        " VALUES (?,?,?,?,?,?,?,?,?,1)",
        (sku, name, _text(f.get("unit"), "Unit", 20) or "ea", cost,
         1 if billable is None or billable else 0, _text(f.get("category"), "Category", 80) or None,
         0 if on_hand is None else on_hand, reorder, 1 if tracked else 0))
    if on_hand:
        _log(conn, cur.lastrowid, on_hand, "initial")
    return cur.lastrowid


def update_part(conn, pid, f):
    p = get_part(conn, pid)
    upd = {}
    if "name" in f:
        upd["name"] = _text(f["name"], "Name", required=True)
    if "sku" in f:
        sku = _text(f["sku"], "Part number", 80) or None
        _check_sku_free(conn, sku, pid)
        upd["sku"] = sku
    if "unit" in f:
        upd["unit"] = _text(f["unit"], "Unit", 20) or "ea"
    if "unit_cost" in f:
        upd["unit_cost"] = _num(f["unit_cost"], "Unit cost", 0)
    if "reorder_at" in f:
        upd["reorder_at"] = _num(f["reorder_at"], "Reorder level", 0)
    if "category" in f:
        upd["category"] = _text(f["category"], "Category", 80) or None
    if "billable" in f:
        b = _bool(f["billable"])
        upd["billable"] = 1 if b is None or b else 0
    if "tracked" in f:
        upd["tracked"] = 1 if f["tracked"] else 0
    if "active" in f:
        upd["active"] = 1 if f["active"] else 0
    if upd:
        sets = ", ".join(f"{k}=?" for k in upd)  # keys are whitelisted above
        conn.execute(f"UPDATE parts SET {sets} WHERE id=?", (*upd.values(), pid))
    conn.commit()
    return p["id"]


# ---- stock -------------------------------------------------------------
def _log(conn, pid, delta, reason, sp_id=None, note=""):
    conn.execute("INSERT INTO inventory_log(part_id,ts,delta,reason,stop_part_id,note) "
                 "VALUES (?,?,?,?,?,?)", (pid, int(time.time()), delta, reason, sp_id, note or None))


def move(conn, part, delta, reason, sp_id=None, note=""):
    """Change truck stock for a tracked part (no-op for untracked parts)."""
    if not part or not part["tracked"] or not delta:
        return
    conn.execute("UPDATE parts SET on_hand=round(COALESCE(on_hand,0)+?, 4) WHERE id=?",
                 (delta, part["id"]))
    _log(conn, part["id"], delta, reason, sp_id, note)


def adjust(conn, pid, delta=None, set_to=None, note=""):
    p = get_part(conn, pid)
    delta, set_to = _num(delta, "Change"), _num(set_to, "Count")
    if (delta is None) == (set_to is None):
        raise EditError("Give either a change or a new count.")
    if set_to is not None:
        delta, reason = round(set_to - (p["on_hand"] or 0), 4), "count"
    else:
        reason = "restock" if delta > 0 else "adjust"
    conn.execute("UPDATE parts SET tracked=1 WHERE id=?", (pid,))
    move(conn, get_part(conn, pid), delta, reason, note=_text(note, "Note", 200))
    conn.commit()


# ---- CSV ---------------------------------------------------------------
def _norm_header(h):
    h = (h or "").strip().lower().replace(" ", "_").replace("-", "_").replace("/", "_").rstrip("?")
    return ALIASES.get(h, h)


def import_csv(conn, text):
    text = text.lstrip("﻿")
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise EditError("The file is empty.")
    cols = [_norm_header(h) for h in header]
    if "name" not in cols:
        raise EditError("No name column found. The first row must be headers, including “name”.")
    ignored = sorted({h for h, c in zip(header, cols) if c not in COLUMNS and h.strip()})
    res = {"created": 0, "updated": 0, "skipped": 0, "errors": [], "ignored_columns": ignored}
    for n, row in enumerate(reader, 2):
        if n - 1 > MAX_ROWS:
            res["errors"].append(f"Stopped at {MAX_ROWS} rows.")
            break
        if not any((c or "").strip() for c in row):
            continue
        rec = {c: _unsafe(row[i].strip() if i < len(row) else "")
               for i, c in enumerate(cols) if c in COLUMNS}
        try:
            existing = find_part(conn, None, rec.get("sku")) if rec.get("sku") else None
            if existing is None and rec.get("name"):
                existing = conn.execute("SELECT * FROM parts WHERE lower(name)=lower(?)",
                                        (rec["name"],)).fetchone()
            if existing is None:
                create_part(conn, rec)
                res["created"] += 1
            else:
                _update_from_row(conn, existing, rec)
                res["updated"] += 1
        except EditError as e:
            res["skipped"] += 1
            res["errors"].append(f"Row {n}: {e}")
    conn.commit()
    return res


def _update_from_row(conn, existing, rec):
    f = {k: v for k, v in rec.items() if v != "" and k != "on_hand"}
    f["active"] = True
    update_part(conn, existing["id"], f)
    oh = _num(rec.get("on_hand"), "On hand")
    if oh is not None:
        conn.execute("UPDATE parts SET tracked=1 WHERE id=?", (existing["id"],))
        move(conn, get_part(conn, existing["id"]), round(oh - (existing["on_hand"] or 0), 4), "import")


def _safe(v):
    t = "" if v is None else str(v)
    return "'" + t if t[:1] in ("=", "+", "-", "@", "\t", "\r") else t  # spreadsheet formula guard


def _unsafe(v):
    """Undo _safe() on re-import so an export round-trips unchanged."""
    return v[1:] if len(v) > 1 and v[0] == "'" and v[1] in "=+-@" else v


def _g(v):
    return "" if v is None else f"{v:g}"


def export_csv(conn):
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(COLUMNS)
    for p in list_parts(conn):
        w.writerow([_safe(p["sku"]), _safe(p["name"]), _safe(p["unit"]),
                    _g(p["unit_cost"]), "yes" if p["billable"] else "no",
                    _safe(p["category"]), _g(p["on_hand"]) if p["tracked"] else "",
                    _g(p["reorder_at"])])
    return out.getvalue()


def template_csv():
    return ("sku,name,unit,unit_cost,billable,category,on_hand,reorder_at\r\n"
            "BV-050,1/2 in. brass ball valve,ea,8.75,yes,Plumbing,12,4\r\n"
            "TAPE-PTFE,PTFE thread tape,roll,1.20,yes,Plumbing,30,10\r\n")


# ---- per-stop usage ----------------------------------------------------
def stop_part_dict(r):
    cost = r["unit_cost"]
    return {"id": r["id"], "stop_id": r["stop_id"], "part_id": r["part_id"], "name": r["name"],
            "sku": r["sku"] or "", "unit": r["unit"] or "ea", "qty": r["qty"], "unit_cost": cost,
            "line_cost": None if cost is None else round(cost * r["qty"], 2),
            "billable": bool(r["billable"]), "notes": r["notes"] or ""}


def _qty(v):
    q = _num(v, "Quantity")
    if q is None or q <= 0:
        raise EditError("Quantity must be more than 0.")
    if q > 100000:
        raise EditError("Quantity is too large.")
    return q


def add_to_stop(conn, stop_id, f):
    qty = _qty(f.get("qty", 1))
    if f.get("part_id"):
        part = get_part(conn, int(f["part_id"]))
    else:
        name = _text(f.get("name"), "Part name", required=True)
        part = find_part(conn, name, f.get("sku"))
        if part is None:  # ad-hoc: remember it in the catalog, but don't track stock
            part = get_part(conn, create_part(conn, {"name": name, "sku": f.get("sku"),
                                                      "unit_cost": f.get("unit_cost")}))
    row = conn.execute("SELECT * FROM stop_parts WHERE stop_id=? AND part_id=?",
                       (stop_id, part["id"])).fetchone()
    if row:
        conn.execute("UPDATE stop_parts SET qty=round(qty+?,4) WHERE id=?", (qty, row["id"]))
        sp_id = row["id"]
    else:
        sp_id = conn.execute(
            "INSERT INTO stop_parts(stop_id,part_id,name,sku,unit,qty,unit_cost,billable,notes) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (stop_id, part["id"], part["name"], part["sku"], part["unit"], qty, part["unit_cost"],
             part["billable"], _text(f.get("notes"), "Notes", 500) or None)).lastrowid
    move(conn, part, -qty, "used", sp_id)
    return sp_id


def patch_stop_part(conn, spid, f):
    r = conn.execute("SELECT * FROM stop_parts WHERE id=?", (spid,)).fetchone()
    if not r:
        raise EditError("That part line no longer exists.")
    upd = {}
    if "qty" in f:
        upd["qty"] = _qty(f["qty"])
    if "billable" in f:
        b = _bool(f["billable"])
        upd["billable"] = 1 if b is None or b else 0
    if "unit_cost" in f:
        upd["unit_cost"] = _num(f["unit_cost"], "Unit cost", 0)
    if "notes" in f:
        upd["notes"] = _text(f["notes"], "Notes", 500) or None
    if upd:
        sets = ", ".join(f"{k}=?" for k in upd)
        conn.execute(f"UPDATE stop_parts SET {sets} WHERE id=?", (*upd.values(), spid))
        if "qty" in upd and r["part_id"]:
            move(conn, conn.execute("SELECT * FROM parts WHERE id=?", (r["part_id"],)).fetchone(),
                 round(r["qty"] - upd["qty"], 4), "used", spid, "quantity changed")
    return r["stop_id"]


def remove_from_stop(conn, spid, reason="return"):
    r = conn.execute("SELECT * FROM stop_parts WHERE id=?", (spid,)).fetchone()
    if not r:
        raise EditError("That part line no longer exists.")
    if r["part_id"]:
        move(conn, conn.execute("SELECT * FROM parts WHERE id=?", (r["part_id"],)).fetchone(),
             r["qty"], reason, spid)
    conn.execute("DELETE FROM stop_parts WHERE id=?", (spid,))
    return r["stop_id"]


def release_stops(conn, stop_ids):
    """Return all parts used at these stops to the truck (stops being deleted)."""
    for sid in stop_ids:
        for r in conn.execute("SELECT id FROM stop_parts WHERE stop_id=?", (sid,)).fetchall():
            remove_from_stop(conn, r["id"])


def for_day(conn, date):
    rows = conn.execute("SELECT sp.* FROM stop_parts sp JOIN stops s ON s.id=sp.stop_id "
                        "WHERE s.date=? ORDER BY sp.id", (date,)).fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["stop_id"], []).append(stop_part_dict(r))
    return out
