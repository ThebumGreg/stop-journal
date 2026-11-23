"""Tools and equipment you own (drill, meter, ladder…): mainly to document what was bought, when, from whom and for
how much, with the receipt and a photo. Not stock (that is parts), not linked to stops.

Files: `<data>/photos/tools/<tool id>-<file id>-<kind>.jpg` (re-encoded like stop photos: upright, 1600 px, no
metadata), listed in `tool_files`. The full backup zip includes them (it takes every .jpg under photos/).

CSV (Truck → Import / export): name (required), make, model, serial, category, purchased (YYYY-MM-DD), price, vendor,
warranty_until, location, notes. Rows match an existing tool by serial, else by name + model; blank cells never
overwrite.
"""
import csv
import io
import json
import os
import time
from datetime import date

from . import photos
from .errors import EditError

LOCATIONS = {"truck": "On the truck", "shop": "Shop / home", "repair": "In for repair", "lost": "Lost / stolen",
             "retired": "Retired / sold"}
KINDS = {"receipt": "Receipt", "photo": "Photo"}
COLUMNS = ["name", "make", "model", "serial", "category", "purchased", "price", "vendor", "warranty_until",
           "location", "notes"]
MAX_FILES = 8
MAX_ROWS = 2000


def _text(v, what, n=120):
    v = str(v if v is not None else "").strip()
    if len(v) > n:
        raise EditError(f"{what} is too long ({n} characters at most).")
    return v or None


def _date(v, what):
    v = str(v or "").strip()
    if not v:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return time.strftime("%Y-%m-%d", time.strptime(v, fmt))
        except ValueError:
            pass
    raise EditError(f"{what} must be a date like 2026-03-14.")


def _price(v):
    v = str(v if v is not None else "").strip().replace("$", "").replace(",", "")
    if not v:
        return None
    try:
        f = float(v)
    except ValueError:
        raise EditError("Price must be a number.")
    if not 0 <= f < 1_000_000:
        raise EditError("Price is out of range.")
    return round(f, 2)


def _fields(f, partial):
    out = {}
    if not partial or "name" in f:
        name = _text(f.get("name"), "Name")
        if not name:
            raise EditError("A tool needs a name.")
        out["name"] = name
    for k, label in (("make", "Make"), ("model", "Model"), ("serial", "Serial number"), ("category", "Category"),
                     ("vendor", "Vendor")):
        if k in f:
            out[k] = _text(f[k], label, 80)
    if "notes" in f:
        out["notes"] = _text(f["notes"], "Notes", 1000)
    if "purchased" in f:
        out["purchased"] = _date(f["purchased"], "Purchase date")
    if "warranty_until" in f:
        out["warranty_until"] = _date(f["warranty_until"], "Warranty date")
    if "price" in f:
        out["price"] = _price(f["price"])
    if "location" in f or not partial:
        loc = str(f.get("location") or "truck").strip().lower()
        loc = {"on the truck": "truck", "home": "shop", "shop / home": "shop", "stolen": "lost", "sold": "retired",
               "in for repair": "repair"}.get(loc, loc)
        if loc not in LOCATIONS:
            raise EditError("Where must be one of: " + ", ".join(LOCATIONS) + ".")
        out["location"] = loc
    return out


def tool_dict(conn, r):
    today = date.today().isoformat()
    files = []
    for x in conn.execute("SELECT id, kind, read_json FROM tool_files WHERE tool_id=? ORDER BY id", (r["id"],)):
        try:
            read = json.loads(x["read_json"]) if x["read_json"] else None
        except ValueError:
            read = None
        files.append({"id": x["id"], "kind": x["kind"], "read": read})
    return {"id": r["id"], "name": r["name"], "make": r["make"] or "", "model": r["model"] or "",
            "serial": r["serial"] or "", "category": r["category"] or "", "purchased": r["purchased"] or "",
            "price": r["price"], "vendor": r["vendor"] or "", "warranty_until": r["warranty_until"] or "",
            "under_warranty": bool(r["warranty_until"] and r["warranty_until"] >= today),
            "location": r["location"] or "truck", "location_label": LOCATIONS.get(r["location"] or "truck", ""),
            "notes": r["notes"] or "", "files": files,
            "has_receipt": any(x["kind"] == "receipt" for x in files)}


def get(conn, tid):
    r = conn.execute("SELECT * FROM tools WHERE id=?", (tid,)).fetchone()
    if not r:
        raise EditError("That tool no longer exists.")
    return r


def list_tools(conn):
    tools = [tool_dict(conn, r) for r in conn.execute("SELECT * FROM tools ORDER BY lower(name), id")]
    year = str(date.today().year)
    owned = [t for t in tools if t["location"] not in ("lost", "retired")]
    return {"tools": tools,
            "totals": {"count": len(owned), "value": round(sum(t["price"] or 0 for t in owned), 2),
                       "this_year": round(sum(t["price"] or 0 for t in tools if t["purchased"].startswith(year)), 2),
                       "no_receipt": sum(1 for t in owned if not t["has_receipt"]),
                       "year": year},
            "locations": [{"value": k, "label": v} for k, v in LOCATIONS.items()],
            "categories": sorted({t["category"] for t in tools if t["category"]}, key=str.lower)}


def create(conn, f):
    v = _fields(f, partial=False)
    v["created"] = int(time.time())
    cur = conn.execute("INSERT INTO tools(%s) VALUES (%s)" % (",".join(v), ",".join("?" * len(v))), tuple(v.values()))
    conn.commit()
    return tool_dict(conn, get(conn, cur.lastrowid))


def update(conn, tid, f):
    get(conn, tid)
    v = _fields(f, partial=True)
    if v:
        conn.execute("UPDATE tools SET " + ", ".join(f"{k}=?" for k in v) + " WHERE id=?", (*v.values(), tid))
        conn.commit()
    return tool_dict(conn, get(conn, tid))


def delete(conn, data_dir, tid):
    get(conn, tid)
    for x in conn.execute("SELECT path FROM tool_files WHERE tool_id=?", (tid,)).fetchall():
        try:
            os.remove(os.path.join(data_dir, x["path"]))
        except OSError:
            pass
    conn.execute("DELETE FROM tool_files WHERE tool_id=?", (tid,))
    conn.execute("DELETE FROM tools WHERE id=?", (tid,))
    conn.commit()


# ---- receipts and photos ----------------------------------------------------------------
def add_file(conn, data_dir, tid, raw, kind="receipt", reader=None):
    """Attach a picture. With `reader` (receipts on, kind receipt) the receipt is read once and the suggestion kept."""
    get(conn, tid)
    if kind not in KINDS:
        raise EditError("Choose Receipt or Photo.")
    n = conn.execute("SELECT COUNT(*) FROM tool_files WHERE tool_id=?", (tid,)).fetchone()[0]
    if n >= MAX_FILES:
        raise EditError(f"A tool can have {MAX_FILES} pictures at most.")
    data, _w, _h = photos.normalize(raw)
    cur = conn.execute("INSERT INTO tool_files(tool_id, kind, path, created) VALUES (?,?,'',?)", (tid, kind, int(time.time())))
    rel = os.path.join("photos", "tools", f"{tid}-{cur.lastrowid}-{kind}.jpg")
    try:
        photos._write(data_dir, rel, data)
    except OSError:
        conn.rollback()
        raise EditError("Could not save the picture on the server (is the data folder writable?).")
    fid = cur.lastrowid
    conn.execute("UPDATE tool_files SET path=? WHERE id=?", (rel, fid))
    conn.commit()
    if reader and kind == "receipt":
        set_read(conn, fid, reader(data))
    return tool_dict(conn, get(conn, tid))


def set_read(conn, fid, d):
    conn.execute("UPDATE tool_files SET read_json=? WHERE id=?", (json.dumps(d), fid))
    conn.commit()


def dismiss_read(conn, fid):
    r = conn.execute("SELECT tool_id, read_json FROM tool_files WHERE id=?", (fid,)).fetchone()
    if not r:
        raise EditError("That picture is gone.")
    d = json.loads(r["read_json"] or "{}")
    d["dismissed"] = True
    set_read(conn, fid, d)
    return tool_dict(conn, get(conn, r["tool_id"]))


def get_file(conn, data_dir, fid):
    r = conn.execute("SELECT path FROM tool_files WHERE id=?", (fid,)).fetchone()
    if not r or not r["path"]:
        return None
    try:
        with open(os.path.join(data_dir, r["path"]), "rb") as fh:
            return fh.read()
    except OSError:
        return None


def delete_file(conn, data_dir, fid):
    r = conn.execute("SELECT tool_id, path FROM tool_files WHERE id=?", (fid,)).fetchone()
    if not r:
        raise EditError("That picture is gone.")
    conn.execute("DELETE FROM tool_files WHERE id=?", (fid,))
    conn.commit()
    try:
        os.remove(os.path.join(data_dir, r["path"]))
    except OSError:
        pass
    return tool_dict(conn, get(conn, r["tool_id"]))


# ---- CSV ---------------------------------------------------------------------------------
ALIASES = {"tool": "name", "description": "name", "item": "name", "brand": "make", "manufacturer": "make",
           "model_number": "model", "model_#": "model", "serial_number": "serial", "serial_#": "serial", "s/n": "serial",
           "sn": "serial", "date": "purchased", "purchase_date": "purchased", "bought": "purchased", "cost": "price",
           "paid": "price", "amount": "price", "store": "vendor", "supplier": "vendor", "bought_from": "vendor",
           "warranty": "warranty_until", "warranty_end": "warranty_until", "where": "location", "status": "location",
           "type": "category", "group": "category"}


def _safe(v):
    t = "" if v is None else str(v)
    return "'" + t if t[:1] in ("=", "+", "-", "@", "\t", "\r") else t


def export_csv(conn):
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(COLUMNS)
    for t in list_tools(conn)["tools"]:
        w.writerow([_safe(t["name"]), _safe(t["make"]), _safe(t["model"]), _safe(t["serial"]), _safe(t["category"]),
                    t["purchased"], "" if t["price"] is None else f"{t['price']:.2f}", _safe(t["vendor"]),
                    t["warranty_until"], t["location"], _safe(t["notes"])])
    return out.getvalue()


def template_csv():
    return (",".join(COLUMNS) + "\r\n"
            "Cordless drill,DeWalt,DCD791,1234567,Power tools,2025-04-02,179.00,Home Depot,2028-04-02,truck,\r\n"
            "Multimeter,Fluke,117,98765432,Test equipment,2024-11-20,229.99,Graybar,,truck,Came with leads\r\n")


def import_csv(conn, text, dry=False):
    reader = csv.reader(io.StringIO(text.lstrip("﻿")))
    try:
        header = next(reader)
    except StopIteration:
        raise EditError("The file is empty.")
    norm = lambda h: (h or "").strip().lower().replace(" ", "_").replace("-", "_")
    cols = [ALIASES.get(norm(h), norm(h)) for h in header]
    if "name" not in cols:
        raise EditError("No name column found. The first row must be headers, including “name”.")
    res = {"created": 0, "updated": 0, "skipped": 0, "errors": [],
           "ignored_columns": sorted({h for h, c in zip(header, cols) if c not in COLUMNS and h.strip()})}
    for n, row in enumerate(reader, 2):
        if n - 1 > MAX_ROWS:
            res["errors"].append(f"Stopped at {MAX_ROWS} rows.")
            break
        if not any((c or "").strip() for c in row):
            continue
        rec = {c: (row[i].strip() if i < len(row) else "") for i, c in enumerate(cols) if c in COLUMNS}
        rec = {k: (v[1:] if len(v) > 1 and v[0] == "'" and v[1] in "=+-@" else v) for k, v in rec.items()}
        try:
            existing = None
            if rec.get("serial"):
                existing = conn.execute("SELECT * FROM tools WHERE lower(serial)=lower(?)", (rec["serial"],)).fetchone()
            if existing is None and rec.get("name"):
                existing = conn.execute("SELECT * FROM tools WHERE lower(name)=lower(?) AND lower(COALESCE(model,''))="
                                        "lower(?)", (rec["name"], rec.get("model", ""))).fetchone()
            if existing is None:
                v = _fields(rec, partial=False)
                if not dry:
                    v["created"] = int(time.time())
                    conn.execute("INSERT INTO tools(%s) VALUES (%s)" % (",".join(v), ",".join("?" * len(v))), tuple(v.values()))
                res["created"] += 1
            else:
                v = _fields({k: x for k, x in rec.items() if x != ""}, partial=True)
                if v and not dry:
                    conn.execute("UPDATE tools SET " + ", ".join(f"{k}=?" for k in v) + " WHERE id=?", (*v.values(), existing["id"]))
                res["updated"] += 1
        except EditError as e:
            res["skipped"] += 1
            res["errors"].append(f"Row {n}: {e}")
    if dry:
        conn.rollback()
        res["dry_run"] = True
    else:
        conn.commit()
    return res
