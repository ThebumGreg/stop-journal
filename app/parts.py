"""Parts catalog + truck inventory.

* `parts` is both the catalog and the truck stock (on_hand). Only parts with
  tracked=1 move stock; ad-hoc parts typed into a stop are added untracked.
* Every stock change is written to inventory_log with a reason
  (initial, import, restock, adjust, count, used, return).
* Stock may go negative (the truck count is often behind reality); the UI flags it.

CSV import format (header row required, order free, extra columns ignored):
  name (required), sku, type, unit, unit_cost, billable, category, vendor, on_hand, reorder_at, pack_size, pack_unit, shelf, bin,
  vendor_item, vendor_unit_cost, qbo_item
  unit_cost is the price CHARGED to the customer (the price in reports). vendor_unit_cost is what you PAY per JobHub unit
  (per ft for cable, each for jacks); it is the part's last_cost ("Our cost"). A column called just "cost" means what
  you pay, never the sell price. vendor_item is the supplier's part number; qbo_item the QuickBooks product/service name.
  shelf and bin say where it lives on the truck ("Shelf 2", bin "14"); Count the truck goes shelf by shelf, bin by bin.
  type is "part" (default), "consumable" (tape, gloves, solvent: used up, normally not billed;
  consumables default to not billable) or "expense" (parking, tolls, a purchase: money, not stock;
  never tracked or counted, not billable unless ticked).
Common alternative headers are accepted (see ALIASES). Rows are matched to an
existing part by sku, else by name; blank cells never overwrite existing values.
A non-blank on_hand sets the truck count and switches tracking on.
unit is one of UNITS (a row with any other unit is rejected; common spellings like "each" or "feet" are accepted).
pack_size / pack_unit describe how the item is bought ("bag" of 25): stock is always counted in `unit`.
"""
import csv
import difflib
import io
import math
import re
import sqlite3
import time

from .errors import EditError

COLUMNS = ["sku", "name", "type", "unit", "unit_cost", "billable", "category", "vendor", "on_hand",
           "reorder_at", "pack_size", "pack_unit", "shelf", "bin", "vendor_item", "vendor_unit_cost", "qbo_item"]
UNITS = {"ea": "Each", "pair": "Pair", "pack": "Pack", "box": "Box", "bag": "Bag", "case": "Case",
         "roll": "Roll", "ft": "Foot", "gal": "Gallon", "lb": "Pound"}
UNIT_WORDS = {"each": "ea", "pc": "ea", "pcs": "ea", "piece": "ea", "pieces": "ea", "feet": "ft", "foot": "ft",
              "bx": "box", "boxes": "box", "bags": "bag", "packs": "pack", "pairs": "pair", "rolls": "roll",
              "cases": "case", "gallon": "gal", "gallons": "gal", "pound": "lb", "pounds": "lb", "lbs": "lb"}
KINDS = ("part", "consumable", "expense")
KIND_WORDS = {"part": "part", "parts": "part", "billable": "part", "material": "part",
              "consumable": "consumable", "consumables": "consumable", "supply": "consumable",
              "supplies": "consumable", "expense": "expense", "expenses": "expense"}
ALIASES = {
    "part_number": "sku", "part_no": "sku", "partnumber": "sku", "part_#": "sku",
    "item_number": "sku", "item_no": "sku", "item_#": "sku",
    "description": "name", "part_name": "name", "item_name": "name", "item": "name", "part": "name",
    "uom": "unit", "units": "unit",
    "price": "unit_cost", "unit_price": "unit_cost", "unitcost": "unit_cost", "sell_price": "unit_cost",
    # what you PAY (never the sell price): "cost" alone is a purchase cost on a supplier's price list
    "cost": "vendor_unit_cost", "last_cost": "vendor_unit_cost", "purchase_cost": "vendor_unit_cost",
    "our_cost": "vendor_unit_cost", "vendor_cost": "vendor_unit_cost", "dlv_unit_cost": "vendor_unit_cost",
    "vendor_item": "vendor_item", "dlv_item": "vendor_item", "vendor_part": "vendor_item", "vendor_sku": "vendor_item",
    "vendor_part_number": "vendor_item", "vendor_item_number": "vendor_item", "supplier_item": "vendor_item",
    "qbo_item": "qbo_item", "qbo_name": "qbo_item", "quickbooks_item": "qbo_item", "qbo_product": "qbo_item",
    "qty": "on_hand", "quantity": "on_hand", "onhand": "on_hand", "stock": "on_hand",
    "in_stock": "on_hand", "truck_qty": "on_hand", "qty_on_hand": "on_hand",
    "reorder": "reorder_at", "reorder_point": "reorder_at", "min": "reorder_at",
    "minimum": "reorder_at", "min_qty": "reorder_at", "par": "reorder_at",
    "group": "category", "kind": "type", "item_type": "type", "part_type": "type",
    "supplier": "vendor", "preferred_vendor": "vendor", "buy_from": "vendor",
    "location": "shelf", "shelf_location": "shelf", "rack": "shelf", "bin_location": "bin", "bin_#": "bin", "drawer": "bin",
    "pack": "pack_size", "case_qty": "pack_size", "pack_qty": "pack_size", "buy_unit": "pack_unit",
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


def _unit(v, current=None):
    """One of UNITS (blank = each). A unit the part already has is left alone, so older data still saves."""
    t = str(v or "").strip().lower().rstrip(".")
    if not t:
        return "ea"
    t = UNIT_WORDS.get(t, t)
    if t in UNITS:
        return t
    if current and str(v).strip() == current:
        return current
    raise EditError("Unit must be one of: " + ", ".join(UNITS.values()) + ".")


def _pack(f, current_unit=None):
    """(pack_size or None, pack_unit) from a form/CSV record. A pack size needs a buy unit (default pack)."""
    size = _num(f.get("pack_size"), "Pack size", 0)
    if size is not None and size <= 0:
        size = None
    unit = str(f.get("pack_unit") or "").strip().lower().rstrip(".")
    unit = UNIT_WORDS.get(unit, unit)
    if unit and unit not in UNITS:
        raise EditError("Pack unit must be one of: " + ", ".join(UNITS.values()) + ".")
    return (size, unit or "pack") if size else (None, None)      # no size, no buy unit


def _kind(v):
    """'part' | 'consumable' | 'expense' | None (blank). Anything else is an error."""
    t = str(v or "").strip().lower()
    if not t:
        return None
    if t not in KIND_WORDS:
        raise EditError("Type must be “part”, “consumable” or “expense”.")
    return KIND_WORDS[t]


def part_dict(r):
    tracked = bool(r["tracked"])
    low = (tracked and r["reorder_at"] is not None and r["on_hand"] <= r["reorder_at"])
    return {"id": r["id"], "sku": r["sku"] or "", "name": r["name"], "unit": r["unit"] or "ea",
            "unit_cost": r["unit_cost"], "billable": bool(r["billable"]),
            "category": r["category"] or "", "on_hand": r["on_hand"] or 0,
            "reorder_at": r["reorder_at"], "tracked": tracked, "active": bool(r["active"]),
            "kind": r["kind"] or "part", "vendor": r["vendor"] or "",
            "pack_size": r["pack_size"], "pack_unit": r["pack_unit"] or "", "shelf": r["shelf"] or "", "bin": r["bin"] or "",
            "last_cost": r["last_cost"], "vendor_item": r["vendor_item"] or "", "qbo_item": r["qbo_item"] or "",
            **_margin(r["unit_cost"], r["last_cost"]),
            "low": bool(low), "negative": bool(tracked and (r["on_hand"] or 0) < 0)}


def _margin(sell, cost):
    """What you make on one unit: sell (unit_cost) minus what you pay (last_cost), and as % of the sell price."""
    if sell is None or cost is None:
        return {"margin": None, "margin_pct": None, "below_cost": False}
    m = round(sell - cost, 4)
    return {"margin": m, "margin_pct": round(m / sell * 100, 1) if sell else None, "below_cost": sell < cost}


# ---- catalog -----------------------------------------------------------
def list_parts(conn, q="", include_inactive=False, kind=None):
    rows = conn.execute("SELECT * FROM parts" + ("" if include_inactive else " WHERE active=1") +
                        " ORDER BY lower(name)").fetchall()
    q = q.strip().lower()
    return [part_dict(r) for r in rows
            if (not kind or (r["kind"] or "part") == kind)
            and (not q or q in r["name"].lower() or q in (r["sku"] or "").lower()
                 or q in (r["category"] or "").lower() or q in (r["vendor"] or "").lower())]


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
    kind = _kind(f.get("type", f.get("kind"))) or "part"
    billable = _bool(f.get("billable"))
    if billable is None:
        billable = kind == "part"            # consumables and expenses are not billed unless said otherwise
    on_hand = _num(f.get("on_hand"), "On hand")
    if kind == "expense":                    # an expense is money, not stock
        on_hand, tracked = None, False
    reorder = _num(f.get("reorder_at"), "Reorder level", 0)
    if on_hand is not None:
        tracked = True
    cur = conn.execute(
        "INSERT INTO parts(sku,name,unit,unit_cost,billable,category,on_hand,reorder_at,tracked,active,"
        "kind,vendor,pack_size,pack_unit,shelf,bin) VALUES (?,?,?,?,?,?,?,?,?,1,?,?,?,?,?,?)",
        (sku, name, _unit(f.get("unit")), cost,
         1 if billable else 0, _text(f.get("category"), "Category", 80) or None,
         0 if on_hand is None else on_hand, reorder, 1 if tracked else 0, kind,
         _text(f.get("vendor"), "Vendor", 80) or None, *_pack(f), _text(f.get("shelf"), "Shelf", 40) or None,
         _text(f.get("bin"), "Bin", 40) or None))
    extra = {k: v for k, v in _extras(f).items() if v is not None}
    if extra:
        conn.execute(f"UPDATE parts SET {', '.join(k + '=?' for k in extra)} WHERE id=?", (*extra.values(), cur.lastrowid))
    if on_hand:
        _log(conn, cur.lastrowid, on_hand, "initial")
    return cur.lastrowid


def _extras(f):
    """vendor_item, qbo_item and what you pay (vendor_unit_cost / last_cost) from a form or CSV row."""
    out = {}
    if "vendor_item" in f:
        out["vendor_item"] = _text(f["vendor_item"], "Vendor item", 80) or None
    if "qbo_item" in f:
        out["qbo_item"] = _text(f["qbo_item"], "QuickBooks item", 120) or None
    for k in ("vendor_unit_cost", "last_cost"):
        if k in f and str(f[k] if f[k] is not None else "").strip() != "":
            out["last_cost"] = _num(str(f[k]).replace("$", "").replace(",", ""), "Our cost", 0)
    return out


def update_part(conn, pid, f):
    p = get_part(conn, pid)
    upd = {}
    upd.update(_extras(f))
    if "name" in f:
        upd["name"] = _text(f["name"], "Name", required=True)
    if "sku" in f:
        sku = _text(f["sku"], "Part number", 80) or None
        _check_sku_free(conn, sku, pid)
        upd["sku"] = sku
    if "unit" in f:
        upd["unit"] = _unit(f["unit"], p["unit"])
    if "unit_cost" in f:
        upd["unit_cost"] = _num(f["unit_cost"], "Unit cost", 0)
    if "reorder_at" in f:
        upd["reorder_at"] = _num(f["reorder_at"], "Reorder level", 0)
    if "category" in f:
        upd["category"] = _text(f["category"], "Category", 80) or None
    if "vendor" in f:
        upd["vendor"] = _text(f["vendor"], "Vendor", 80) or None
    if "shelf" in f:
        upd["shelf"] = _text(f["shelf"], "Shelf", 40) or None
    if "bin" in f:
        upd["bin"] = _text(f["bin"], "Bin", 40) or None
    if "pack_size" in f or "pack_unit" in f:
        size, unit = _pack({"pack_size": f.get("pack_size", p["pack_size"]),
                            "pack_unit": f.get("pack_unit", p["pack_unit"])})
        upd["pack_size"], upd["pack_unit"] = size, unit
    if "type" in f or "kind" in f:
        k = _kind(f.get("type", f.get("kind")))
        if k:
            upd["kind"] = k
            if k == "expense":
                upd["tracked"] = 0
    if "billable" in f:
        b = _bool(f["billable"])
        upd["billable"] = 1 if b is None or b else 0
    if "tracked" in f and upd.get("kind", p["kind"]) != "expense":
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
    if not part or not part["tracked"] or not delta or part["kind"] == "expense":
        return
    conn.execute("UPDATE parts SET on_hand=round(COALESCE(on_hand,0)+?, 4) WHERE id=?",
                 (delta, part["id"]))
    _log(conn, part["id"], delta, reason, sp_id, note)


def adjust(conn, pid, delta=None, set_to=None, note="", packs=None, paid=None, vendor=""):
    """Change stock. When stock is added with `paid` (the total for what was added), it is a purchase."""
    p = get_part(conn, pid)
    packs = _num(packs, "Packs")
    if packs is not None:             # received whole packs: add pack_size units for each
        if not p["pack_size"]:
            raise EditError("Set a pack size on this item first.")
        delta = round(packs * p["pack_size"], 4)
        note = note or f"{packs:g} x {p['pack_unit'] or 'pack'} of {p['pack_size']:g}"
    delta, set_to = _num(delta, "Change"), _num(set_to, "Count")
    if (delta is None) == (set_to is None):
        raise EditError("Give either a change or a new count.")
    if set_to is not None:
        delta, reason = round(set_to - (p["on_hand"] or 0), 4), "count"
    else:
        reason = "restock" if delta > 0 else "adjust"
    conn.execute("UPDATE parts SET tracked=1 WHERE id=?", (pid,))
    paid = _num(paid, "Paid", 0)
    if paid is not None and delta > 0 and set_to is None:
        vendor = _text(vendor, "Vendor", 80) or p["vendor"] or ""
        note = note or ("Bought at " + vendor if vendor else "Bought")
        move(conn, get_part(conn, pid), delta, "bought", note=_text(note, "Note", 200))
        record_purchase(conn, p, delta, paid, vendor, note)
    else:
        move(conn, get_part(conn, pid), delta, reason, note=_text(note, "Note", 200))
    conn.commit()


def record_purchase(conn, p, qty, total, vendor="", note=""):
    """One line of the purchase log. The part's last_cost becomes what was paid per unit (its Unit cost, the price in
    reports, is left alone)."""
    conn.execute("INSERT INTO purchases(part_id, ts, vendor, qty, total, note) VALUES (?,?,?,?,?,?)",
                 (p["id"], int(time.time()), vendor or None, qty, total, note or None))
    if total is not None and qty:
        conn.execute("UPDATE parts SET last_cost=? WHERE id=?", (round(total / qty, 4), p["id"]))


# ---- CSV ---------------------------------------------------------------
def _raw_header(h):
    h = (h or "").strip().lower().replace(" ", "_").replace("-", "_").replace("/", "_").rstrip("?")
    return re.sub(r"_+", "_", h).strip("_")


def _norm_header(h):
    h = _raw_header(h)
    return ALIASES.get(h, h)


# QuickBooks Online "Products and Services" export (Sales → Products and services → Export). Recognised by its
# "Product/Service Name" header. Sales price is what the customer is charged, which is JobHub's unit_cost (the price in
# reports); Purchase cost is what you pay, which is last_cost ("Last paid"). Quantity on hand and Reorder point are the
# company's stock, not the truck's, so they are NOT imported. Service rows WITH a SKU are imported as parts (some
# QuickBooks companies keep inventory tracking off, so every item is a Service; QBO_SERVICE_WITH_SKU, default on);
# Service rows without a SKU (labor, fees), Bundles, and anything named or SKU'd "Labor" are skipped. Rows match existing parts by SKU, then by name, so the QuickBooks SKU becomes the part number.
QBO_COLUMNS = {"product_service_name": "name", "product_service": "name", "sku": "sku",
               "sales_price_rate": "unit_cost", "sales_price": "unit_cost", "purchase_cost": "last_cost",
               "type": "type", "preferred_vendor": "vendor", "category": "category"}
LABOR = re.compile(r"^\s*labou?r\b", re.I)
QBO_TYPES = {"inventory": "part", "non_inventory": "part", "noninventory": "part"}


def _qbo_import(conn, header, reader, service_with_sku=True):
    cols = [QBO_COLUMNS.get(_raw_header(h)) for h in header]
    ignored = sorted({h for h, c in zip(header, cols) if not c and h.strip()})
    res = {"created": 0, "updated": 0, "skipped": 0, "errors": [], "ignored_columns": ignored,
           "format": "QuickBooks Products and Services", "services_skipped": 0}
    for n, row in enumerate(reader, 2):
        if n - 1 > MAX_ROWS:
            res["errors"].append(f"Stopped at {MAX_ROWS} rows.")
            break
        if not any((c or "").strip() for c in row):
            continue
        rec = {c: _unsafe(row[i].strip() if i < len(row) else "") for i, c in enumerate(cols) if c}
        qtype = _raw_header(rec.pop("type", "") or "inventory")
        is_labor = bool(LABOR.match(rec.get("name", "").rsplit(":", 1)[-1]) or LABOR.match(rec.get("sku", "")))
        if is_labor or (qtype not in QBO_TYPES and not (qtype == "service" and service_with_sku and rec.get("sku"))):
            res["services_skipped"] += 1
            continue
        rec["type"] = QBO_TYPES.get(qtype, "part")
        if rec.get("name", "").count(":"):          # QuickBooks sub-items: "Cable:Cat 6" -> "Cat 6"
            rec["name"] = rec["name"].rsplit(":", 1)[1].strip()
        if not rec.get("unit_cost"):
            rec.pop("unit_cost", None)
        for k in ("unit_cost", "last_cost"):
            if rec.get(k):
                rec[k] = rec[k].replace("$", "").replace(",", "")
        last = rec.pop("last_cost", "")
        try:
            existing = find_part(conn, None, rec.get("sku")) if rec.get("sku") else None
            if existing is None and rec.get("name"):
                existing = conn.execute("SELECT * FROM parts WHERE lower(name)=lower(?)", (rec["name"],)).fetchone()
            if existing is None:
                pid = create_part(conn, rec)
                res["created"] += 1
            else:
                rec.pop("type", None)      # QuickBooks has no consumables: keep the kind JobHub already has
                _update_from_row(conn, existing, rec)
                pid = existing["id"]
                res["updated"] += 1
            if last:
                conn.execute("UPDATE parts SET last_cost=? WHERE id=?", (_num(last, "Purchase cost", 0), pid))
        except EditError as e:
            res["skipped"] += 1
            res["errors"].append(f"Row {n}: {e}")
    conn.commit()
    return res


def import_csv(conn, text, service_with_sku=True):
    text = text.lstrip("﻿")
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise EditError("The file is empty.")
    if any(_raw_header(h) in ("product_service_name", "product_service") for h in header):
        return _qbo_import(conn, header, reader, service_with_sku)
    cols = [_norm_header(h) for h in header]
    if "name" not in cols:
        raise EditError("No name column found. The first row must be headers, including “name”.")
    ignored = sorted({h for h, c in zip(header, cols) if c not in COLUMNS and h.strip()})
    res = {"created": 0, "updated": 0, "skipped": 0, "errors": [], "ignored_columns": ignored, "notes": []}
    for h, c in zip(header, cols):
        if c == "vendor_unit_cost" and _raw_header(h) in ("cost", "purchase_cost", "dlv_unit_cost", "our_cost", "vendor_cost"):
            res["notes"].append(f"“{h.strip()}” was read as what you pay (Our cost), not the price you charge"
                                + (" (that is the unit_cost column)." if "unit_cost" in cols else "."))
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


def preview_csv(conn, text, service_with_sku=True):
    """Run the import on a throw-away in-memory copy of the database: the same counts and errors,
    nothing saved."""
    mem = sqlite3.connect(":memory:")
    mem.row_factory = sqlite3.Row
    try:
        conn.backup(mem)
        res = import_csv(mem, text, service_with_sku)
    finally:
        mem.close()
    res["dry_run"] = True
    return res


def _update_from_row(conn, existing, rec):
    f = {k: v for k, v in rec.items() if v != "" and k != "on_hand"}
    f["active"] = True
    update_part(conn, existing["id"], f)
    oh = _num(rec.get("on_hand"), "On hand")
    if oh is not None and get_part(conn, existing["id"])["kind"] != "expense":
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
        w.writerow([_safe(p["sku"]), _safe(p["name"]), p["kind"], _safe(p["unit"]),
                    _g(p["unit_cost"]), "yes" if p["billable"] else "no",
                    _safe(p["category"]), _safe(p["vendor"]),
                    _g(p["on_hand"]) if p["tracked"] else "", _g(p["reorder_at"]),
                    _g(p["pack_size"]), p["pack_unit"], _safe(p["shelf"]), _safe(p["bin"]),
                    _safe(p["vendor_item"]), _g(p["last_cost"]), _safe(p["qbo_item"])])
    return out.getvalue()


def template_csv():
    return ("sku,name,type,unit,unit_cost,billable,category,vendor,on_hand,reorder_at,pack_size,pack_unit,shelf,bin,"
            "vendor_item,vendor_unit_cost,qbo_item\r\n"
            "BV-050,1/2 in. brass ball valve,part,ea,8.75,yes,Plumbing,Ferguson,12,4,,,Shelf 2,3,FBV-050,4.10,Ball valve 1/2\r\n"
            "JACK-1,Telephone jack,part,ea,1.10,yes,Cabling,Graybar,40,10,25,bag,Shelf 1,12,GB-J1,0.42,\r\n"
            "TAPE-PTFE,PTFE thread tape,consumable,roll,1.20,no,Plumbing,Home Depot,30,10,,,Door,,0.89,\r\n")


# ---- per-stop usage ----------------------------------------------------
def stop_part_dict(r):
    cost = r["unit_cost"]
    return {"id": r["id"], "stop_id": r["stop_id"], "part_id": r["part_id"], "name": r["name"],
            "sku": r["sku"] or "", "unit": r["unit"] or "ea", "qty": r["qty"], "unit_cost": cost,
            "line_cost": None if cost is None else round(cost * r["qty"], 2),
            "billable": bool(r["billable"]), "notes": r["notes"] or "",
            "kind": r["kind"] or "part", "consumable": (r["kind"] or "part") == "consumable",
            "expense": (r["kind"] or "part") == "expense",
            "qbo_item": (r["qbo_item"] if "qbo_item" in r.keys() else None) or ""}


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
                                                      "unit_cost": f.get("unit_cost"), "type": f.get("type"), "unit": f.get("unit"),
                                                      "billable": f.get("billable")}))
    expense = part["kind"] == "expense"
    cost = part["unit_cost"]
    if expense and str(f.get("unit_cost") if f.get("unit_cost") is not None else "").strip():
        cost = _num(f.get("unit_cost"), "Amount", 0)        # each receipt has its own amount
    row = None if expense else conn.execute("SELECT * FROM stop_parts WHERE stop_id=? AND part_id=?",
                                           (stop_id, part["id"])).fetchone()   # two receipts stay two lines
    if row:
        conn.execute("UPDATE stop_parts SET qty=round(qty+?,4) WHERE id=?", (qty, row["id"]))
        sp_id = row["id"]
    else:
        sp_id = conn.execute(
            "INSERT INTO stop_parts(stop_id,part_id,name,sku,unit,qty,unit_cost,billable,notes,kind) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (stop_id, part["id"], part["name"], part["sku"], part["unit"], qty, cost,
             part["billable"], _text(f.get("notes"), "Notes", 500) or None,
             part["kind"] or "part")).lastrowid
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
    rows = conn.execute("SELECT sp.*, p.qbo_item AS qbo_item FROM stop_parts sp JOIN stops s ON s.id=sp.stop_id "
                        "LEFT JOIN parts p ON p.id=sp.part_id WHERE s.date=? ORDER BY sp.id", (date,)).fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["stop_id"], []).append(stop_part_dict(r))
    return out


def usual_for(conn, stop, limit=5):
    """Parts most often used at this business before (not already on this stop).
    Matches by saved place, else by business name. Quantity is the one used last time."""
    if stop["known_site_id"]:
        where, arg = "s.known_site_id=?", stop["known_site_id"]
    elif stop["business_name"]:
        where, arg = "lower(s.business_name)=lower(?)", stop["business_name"]
    else:
        return []
    rows = conn.execute(
        f"SELECT sp.part_id, sp.name, sp.sku, sp.qty, sp.id AS last_id "
        f"FROM stop_parts sp JOIN stops s ON s.id=sp.stop_id "
        f"JOIN parts p ON p.id=sp.part_id AND p.active=1 "
        f"WHERE {where} AND s.id != ? AND sp.part_id NOT IN "
        f"(SELECT part_id FROM stop_parts WHERE stop_id=? AND part_id IS NOT NULL) "
        f"ORDER BY sp.id DESC", (arg, stop["id"], stop["id"])).fetchall()
    agg = {}
    for r in rows:   # newest first: first sighting carries the latest quantity
        a = agg.setdefault(r["part_id"], {"part_id": r["part_id"], "name": r["name"],
                                          "sku": r["sku"], "qty": r["qty"], "times": 0})
        a["times"] += 1
    return sorted(agg.values(), key=lambda a: -a["times"])[:limit]


# ---- truck count and shopping list ---------------------------------------
def count_sheet(conn, kind=None):
    """Tracked parts with the quantity the app thinks is on the truck, when each was last
    counted, and how much was used since then (from stops, net of returns)."""
    out = []
    for p in list_parts(conn, kind=kind):
        if not p["tracked"]:
            continue
        last = conn.execute("SELECT MAX(ts) FROM inventory_log WHERE part_id=? AND reason='count'",
                            (p["id"],)).fetchone()[0]
        used = conn.execute(
            "SELECT COALESCE(-SUM(delta), 0) FROM inventory_log WHERE part_id=? "
            "AND reason IN ('used','return') AND ts > ?", (p["id"], last or 0)).fetchone()[0]
        out.append({**p, "last_counted": last, "used_since": round(used, 4)})
    return sorted(out, key=shelf_order)   # walk the truck shelf by shelf, bin by bin


def shelf_order(p):
    return (not p["shelf"], _natural(p["shelf"]), not p["bin"], _natural(p["bin"]), p["name"].lower())


def split_shelf_bin(conn):
    """One-time (2026-11-23): the first version had one "Shelf / bin" field (stored in `bin`). Values that name a shelf
    ("Shelf 2", "Door", "Top rack") move to `shelf`; values that name a bin ("Bin 14", "B-3", "14") stay in `bin`."""
    if conn.execute("SELECT 1 FROM settings WHERE key='_split_shelf_bin'").fetchone():
        return
    for r in conn.execute("SELECT id, bin FROM parts WHERE COALESCE(bin,'')!='' AND COALESCE(shelf,'')=''").fetchall():
        v = r["bin"].strip()
        if not re.match(r"^(bin|drawer|box|b)\b|^b?-?\d+$", v, re.I):
            conn.execute("UPDATE parts SET shelf=?, bin=NULL WHERE id=?", (v, r["id"]))
    conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES ('_split_shelf_bin','1')")
    conn.commit()


def _natural(s):
    """'Shelf 10' after 'Shelf 2'."""
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", (s or "").lower())]


def apply_counts(conn, counts):
    """counts: {part id: counted quantity}. Only parts whose count differs are changed.
    Returns {changed, unchanged, details}."""
    changed, details = 0, []
    for pid, qty in counts.items():
        p = get_part(conn, int(pid))
        q = _num(qty, f"Count for {p['name']}", 0)
        if q is None:
            continue
        diff = round(q - (p["on_hand"] or 0), 4)
        # always log a count (even when equal) so "used since last count" restarts
        conn.execute("UPDATE parts SET tracked=1, on_hand=? WHERE id=?", (q, p["id"]))
        _log(conn, p["id"], diff, "count", note="truck count")
        if diff:
            changed += 1
            details.append({"name": p["name"], "was": p["on_hand"] or 0, "now": q, "diff": diff})
    conn.commit()
    return {"changed": changed, "unchanged": len(counts) - changed, "details": details}


def shopping_list(conn):
    """Tracked parts at or below their reorder level, grouped by vendor. The suggested
    quantity restocks to twice the reorder level."""
    groups = {}
    for p in list_parts(conn):
        if not (p["tracked"] and p["low"]):
            continue
        need = max(round(p["reorder_at"] * 2 - p["on_hand"], 4), 1)
        packs = None
        if p["pack_size"]:                 # bought in packs: round up to whole packs
            packs = max(math.ceil(need / p["pack_size"]), 1)
            need = round(packs * p["pack_size"], 4)
        groups.setdefault(p["vendor"] or "No vendor set", []).append({
            "id": p["id"], "name": p["name"], "sku": p["sku"], "unit": p["unit"], "kind": p["kind"],
            "vendor_item": p["vendor_item"] or "",
            "on_hand": p["on_hand"], "reorder_at": p["reorder_at"], "suggest": need,
            "packs": packs, "pack_unit": p["pack_unit"] if packs else "", "pack_size": p["pack_size"],
            "unit_cost": p["unit_cost"]})
    return [{"vendor": v, "items": items} for v, items in sorted(groups.items(),
            key=lambda kv: (kv[0] == "No vendor set", kv[0].lower()))]


def choices(conn):
    """Values for the Truck page dropdowns: vendors (saved vendor places plus any used on a part) and categories."""
    vend = {r[0].strip() for r in conn.execute(
        "SELECT vendor FROM parts WHERE COALESCE(vendor,'')!='' UNION "
        "SELECT name FROM known_sites WHERE kind='vendor' AND COALESCE(name,'')!='' UNION "
        "SELECT vendor FROM tools WHERE COALESCE(vendor,'')!='' UNION "
        "SELECT name FROM vendors")}
    cats = {r[0].strip() for r in conn.execute("SELECT category FROM parts WHERE COALESCE(category,'')!=''")}
    key = lambda x: x.lower()
    return {"vendors": sorted(vend, key=key), "categories": sorted(cats, key=key),
            "units": [{"value": k, "label": v} for k, v in UNITS.items()]}


def mark_bought(conn, items, vendor=""):
    """Shopping list → truck: add what was bought. items: [{id, packs} or {id, qty}] (packs for items bought in packs).
    Each line is logged as "bought" with the vendor, so a later purchase log can build on it. One transaction."""
    vendor = _text(vendor, "Vendor", 80)
    if not items:
        raise EditError("Nothing to mark as bought.")
    done = []
    for it in items:
        p = get_part(conn, int(it.get("id") or 0))
        packs = _num(it.get("packs"), "Packs", 0)
        qty = _num(it.get("qty"), "Quantity", 0)
        if packs:
            if not p["pack_size"]:
                raise EditError(f"{p['name']} has no pack size.")
            qty = round(packs * p["pack_size"], 4)
        if not qty:
            continue
        note = ("Bought at " + vendor if vendor and vendor != "No vendor set" else "Bought") + \
               (f" ({packs:g} x {p['pack_unit'] or 'pack'} of {p['pack_size']:g})" if packs else "")
        conn.execute("UPDATE parts SET tracked=1 WHERE id=?", (p["id"],))
        move(conn, get_part(conn, p["id"]), qty, "bought", note=note)
        paid = _num(it.get("paid"), f"Paid for {p['name']}", 0)
        record_purchase(conn, p, qty, paid, "" if vendor == "No vendor set" else vendor, note)
        done.append({"id": p["id"], "name": p["name"], "added": qty, "unit": p["unit"] or "ea", "paid": paid})
    conn.commit()
    if not done:
        raise EditError("Enter how many you bought.")
    return {"bought": done}


REASON_LABEL = {"used": "Used on a stop", "bought": "Bought", "return": "Back to the truck (removed from a stop)", "count": "Truck count",
                "restock": "Stock added", "adjust": "Stock removed", "import": "CSV import", "delete": "Stop deleted",
                "redetect": "Stop re-detected"}


def history(conn, pid, limit=100):
    """One part's page: the part, the stops it was used on (newest first), and every stock change."""
    p = get_part(conn, pid)
    uses = [{"stop_id": r["stop_id"], "date": r["date"], "business_name": r["business_name"] or "",
             "qty": r["qty"], "unit": r["unit"] or p["unit"] or "ea", "unit_cost": r["unit_cost"],
             "billable": bool(r["billable"]), "url": f"/day/{r['date']}?stop={r['stop_id']}"}
            for r in conn.execute(
                "SELECT sp.*, s.date, s.business_name FROM stop_parts sp JOIN stops s ON s.id=sp.stop_id "
                "WHERE sp.part_id=? ORDER BY s.date DESC, s.arrive_utc DESC LIMIT ?", (pid, limit))]
    log = [{"ts": r["ts"], "delta": r["delta"], "reason": REASON_LABEL.get(r["reason"], r["reason"]),
            "note": r["note"] or ""}
           for r in conn.execute("SELECT * FROM inventory_log WHERE part_id=? ORDER BY ts DESC, id DESC LIMIT ?",
                                 (pid, limit))]
    used = conn.execute("SELECT COALESCE(SUM(qty),0), COUNT(DISTINCT stop_id) FROM stop_parts WHERE part_id=?",
                        (pid,)).fetchone()
    return {"part": part_dict(p), "uses": uses, "log": log, "totals": {"used": used[0], "stops": used[1]}}


# ---- possible duplicates ----------------------------------------------------------
SIMILAR_AT = 0.82        # difflib ratio on normalized names


def _norm_name(s):
    s = (s or "").lower().replace('"', " in ").replace("inch", " in ")
    return " ".join(re.findall(r"[a-z0-9/.]+", s))


def similar(conn, name="", sku="", exclude=None, limit=5):
    """Parts that may be the same thing: same part # (ignoring case, spaces and dashes), or a name that is nearly
    the same ("1/2 ball valve" vs "1/2 in. ball valve"). Newest-used first is not needed: best match first."""
    n, k = _norm_name(name), re.sub(r"[\s-]", "", (sku or "").lower())
    out = []
    for r in conn.execute("SELECT * FROM parts"):
        if exclude and r["id"] == exclude:
            continue
        why, score = None, 0.0
        if k and re.sub(r"[\s-]", "", (r["sku"] or "").lower()) == k:
            why, score = "same part #", 2.0
        elif n and len(n) >= 3:
            other = _norm_name(r["name"])
            ta, tb = set(n.split()) - {"in"}, set(other.split()) - {"in"}
            ratio = max(difflib.SequenceMatcher(None, n, other).ratio(),            # typos
                        difflib.SequenceMatcher(None, " ".join(sorted(ta)), " ".join(sorted(tb))).ratio())  # word order
            same_words = len(ta) >= 2 and len(tb) >= 2 and (ta <= tb or tb <= ta)
            if ratio >= SIMILAR_AT or same_words or (len(n) >= 6 and (n in other or other in n)):
                why, score = "similar name", max(ratio, 0.9 if same_words else 0)
        if why:
            out.append((score, {**part_dict(r), "why": why}))
    return [p for _, p in sorted(out, key=lambda x: -x[0])[:limit]]


# ---- purchase log ---------------------------------------------------------------
def purchases(conn, start_ts=None, end_ts=None, vendor=None, part_id=None, limit=500):
    """Purchases newest first, with totals by vendor and by type (parts vs consumables)."""
    q = "SELECT pu.*, p.name, p.unit, p.kind, p.sku FROM purchases pu JOIN parts p ON p.id=pu.part_id WHERE 1=1"
    args = []
    if start_ts:
        q += " AND pu.ts >= ?"; args.append(start_ts)
    if end_ts:
        q += " AND pu.ts < ?"; args.append(end_ts)
    if vendor:
        q += " AND lower(trim(COALESCE(pu.vendor,'')))=lower(trim(?))"; args.append(vendor)
    if part_id:
        q += " AND pu.part_id=?"; args.append(part_id)
    rows = conn.execute(q + " ORDER BY pu.ts DESC, pu.id DESC LIMIT ?", (*args, limit)).fetchall()
    items = [{"id": r["id"], "ts": r["ts"], "part_id": r["part_id"], "name": r["name"], "sku": r["sku"] or "",
              "unit": r["unit"] or "ea", "kind": r["kind"] or "part", "vendor": r["vendor"] or "", "qty": r["qty"],
              "total": r["total"], "each": round(r["total"] / r["qty"], 4) if r["total"] is not None and r["qty"] else None,
              "note": r["note"] or ""} for r in rows]
    by_vendor, by_kind = {}, {}
    for it in items:
        if it["total"] is None:
            continue
        v = it["vendor"] or "No vendor"
        by_vendor[v] = round(by_vendor.get(v, 0) + it["total"], 2)
        by_kind[it["kind"]] = round(by_kind.get(it["kind"], 0) + it["total"], 2)
    return {"purchases": items, "total": round(sum(by_vendor.values()), 2),
            "by_vendor": sorted(({"vendor": k, "total": v} for k, v in by_vendor.items()), key=lambda x: -x["total"]),
            "by_kind": by_kind, "unpriced": sum(1 for it in items if it["total"] is None)}
