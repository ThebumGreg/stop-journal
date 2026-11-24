"""Add stops from receipts (Truck → Receipts): catch up past gas and vendor stops from photos of their receipts.

1. Upload receipt photos (one request each). Each is shrunk and stored under photos/inbox/ and read once by the Claude
   reader (store, date, TIME, total, tax, gallons, line items); it counts against RECEIPT_MONTHLY_CAP.
2. The Receipts tab lists them with a proposal: Gas when the receipt is fuel, else Vendor; the stop times from the printed
   time (gas 8 min before to 2 after; vendor 20 before to 2 after); an existing stop on that day near that time is offered
   instead of a new one (days with GPS), otherwise a new manual stop is made (days without GPS).
3. You review and Add. Each line item of a store receipt gets its own choice:
     stock   bought for the truck: stock goes up and the purchase log gets the price (dated the receipt day)
     job     bought for a customer job: a billable part on the job stop you pick (catalog price, or the receipt price for a
             new part); the purchase is logged and, for a tracked part, stock is added and used again (nets to zero)
     expense a company expense on the vendor stop ("Store purchase", the item in its notes)
     skip    personal / not needed
   Tax can be added as its own expense line. A fuel receipt sets the fill-up (total, gallons, odometer).
   The receipt picture is attached to the stop. The stop is marked `from_receipt` (the report says so).
Nothing is added without the Add tap; Skip / Delete leave everything else alone.
"""
import json
import os
import time
from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import parts, photos, receipts, service
from .errors import EditError

ACTIONS = ("stock", "job", "expense", "skip")
TYPES = ("gas", "vendor")
NEAR_BEFORE_S, NEAR_AFTER_S = 45 * 60, 15 * 60
EXPENSE_NAME = "Store purchase"


def _path(iid):
    return os.path.join("photos", "inbox", f"{iid}-receipt.jpg")


def add(conn, cfg, raw):
    """Store and read one receipt photo. Returns the inbox item."""
    if not cfg.anthropic_key:
        raise EditError("Reading receipts needs the Anthropic API key (Settings → AI).")
    jpeg, _, _ = photos.normalize(raw)
    cur = conn.execute("INSERT INTO receipt_inbox(created, status) VALUES (?, 'new')", (int(time.time()),))
    iid = cur.lastrowid
    rel = _path(iid)
    photos._write(cfg.data_dir, rel, jpeg)
    conn.execute("UPDATE receipt_inbox SET path=? WHERE id=?", (rel, iid))
    conn.commit()
    out = receipts.extract(conn, replace(cfg, receipt_reader="claude"), jpeg)
    conn.execute("UPDATE receipt_inbox SET read_json=? WHERE id=?", (json.dumps(out), iid))
    conn.commit()
    return item(conn, cfg, _row(conn, iid))


def read_again(conn, cfg, iid):
    r = _row(conn, iid)
    out = receipts.extract(conn, replace(cfg, receipt_reader="claude"), image(cfg, r))
    conn.execute("UPDATE receipt_inbox SET read_json=? WHERE id=?", (json.dumps(out), iid))
    conn.commit()
    return item(conn, cfg, _row(conn, iid))


def _row(conn, iid):
    r = conn.execute("SELECT * FROM receipt_inbox WHERE id=?", (iid,)).fetchone()
    if not r:
        raise EditError("That receipt is gone.")
    return r


def image(cfg, r):
    try:
        with open(os.path.join(cfg.data_dir, r["path"]), "rb") as f:
            return f.read()
    except (OSError, TypeError):
        raise EditError("The receipt picture is missing.")


def _hm(t, delta_s):
    d = datetime.strptime(t, "%H:%M") + timedelta(seconds=delta_s)
    return d.strftime("%H:%M") if d.date() == datetime.strptime(t, "%H:%M").date() else t


def proposal(conn, cfg, rd):
    """What the review form starts with for a read receipt."""
    fuel = bool(rd.get("gallons"))
    typ = "gas" if fuel else "vendor"
    date, t = rd.get("date") or "", rd.get("time") or ""
    p = {"type": typ, "store": rd.get("store") or "", "date": date, "time": t, "arrive": "", "depart": "",
         "stop_id": None, "candidates": []}
    if t:
        p["arrive"] = _hm(t, -(8 if fuel else 20) * 60)
        p["depart"] = _hm(t, 2 * 60)
    if date:
        tz = ZoneInfo(cfg.app_tz)
        at = int(datetime.fromisoformat(f"{date}T{t or '12:00'}").replace(tzinfo=tz).timestamp())
        for s in conn.execute("SELECT * FROM stops WHERE date=? AND kind='stop' ORDER BY arrive_utc", (date,)):
            near = s["arrive_utc"] - NEAR_AFTER_S <= at <= s["depart_utc"] + NEAR_BEFORE_S if t else True
            st = service.stop_type(s)
            if near and st in ("", "gas", "vendor"):
                p["candidates"].append({"id": s["id"], "arrive": service.fmt_time(cfg, s["arrive_utc"]),
                                        "depart": service.fmt_time(cfg, s["depart_utc"]),
                                        "name": s["business_name"] or "", "type": st or ""})
        if t and len(p["candidates"]) == 1:
            p["stop_id"] = p["candidates"][0]["id"]
    return p


def _part_info(r):
    return {"id": r["id"], "name": r["name"], "unit": r["unit"] or "ea", "pack_size": r["pack_size"],
            "pack_unit": r["pack_unit"] or "", "last_cost": r["last_cost"]}


def _guess_part(conn, it):
    """A catalog part for a receipt line: same part # / vendor item #, else a nearly identical name."""
    sku = (it.get("sku") or "").strip()
    if sku:
        r = conn.execute("SELECT * FROM parts WHERE active=1 AND (lower(sku)=lower(?) OR lower(vendor_item)=lower(?))",
                         (sku, sku)).fetchone()
        if r:
            return _part_info(r)
    hits = parts.similar(conn, it.get("description") or "", sku, limit=1)
    if hits and hits[0].get("why") in ("same part #", "similar name", "same words"):
        return _part_info(parts.get_part(conn, hits[0]["id"]))
    return None


def in_packs(part, unit_price):
    """Does the receipt count packs (sticks, boxes, bags) of this part rather than its stock unit? Decided by the price:
    closer to (our cost x pack size) than to our cost per unit. Unknown when the part has no pack size or no cost."""
    if not part or not part.get("pack_size") or unit_price is None:
        return False
    cost = part.get("last_cost")
    if not cost:
        return False
    return abs(unit_price - cost * part["pack_size"]) < abs(unit_price - cost)


def item(conn, cfg, r):
    rd = json.loads(r["read_json"] or "{}")
    out = {"id": r["id"], "status": r["status"], "created": r["created"], "stop_id": r["stop_id"],
           "image": f"/api/receipts/inbox/{r['id']}.jpg", "read": rd, "error": "" if rd.get("ok", True) else rd.get("error", "")}
    if r["status"] == "new" and rd.get("ok"):
        out["proposal"] = proposal(conn, cfg, rd)
        out["items"] = []
        for k, it in enumerate(rd.get("items") or []):
            g = _guess_part(conn, it)
            out["items"].append({"i": k, "description": it.get("description", ""), "sku": it.get("sku", ""),
                                 "qty": it.get("qty") or 1, "unit_price": it.get("unit_price"), "amount": it.get("amount"),
                                 "part": g, "action": "stock" if g else "expense",
                                 "packs": in_packs(g, it.get("unit_price"))})
    if r["applied_json"]:
        out["applied"] = json.loads(r["applied_json"])
    return out


def listing(conn, cfg, include_done=True):
    rows = conn.execute("SELECT * FROM receipt_inbox ORDER BY CASE status WHEN 'new' THEN 0 ELSE 1 END, id DESC "
                        "LIMIT 200").fetchall()
    return [item(conn, cfg, r) for r in rows if include_done or r["status"] == "new"]


def jobs_near(conn, cfg, date, days_before=60, days_after=60):
    """Job stops around a receipt date, for "used on a job" (newest first)."""
    d = datetime.fromisoformat(date) if date else datetime.now()
    lo, hi = (d - timedelta(days=days_before)).date().isoformat(), (d + timedelta(days=days_after)).date().isoformat()
    out = []
    for s in conn.execute("SELECT * FROM stops WHERE date BETWEEN ? AND ? AND kind='stop' ORDER BY date DESC, arrive_utc DESC",
                          (lo, hi)):
        if service.stop_type(s) == "job" and (s["business_name"] or "").strip():
            out.append({"id": s["id"], "date": s["date"], "arrive": service.fmt_time(cfg, s["arrive_utc"]),
                        "name": s["business_name"], "ticket": s["ticket"] or ""})
    return out[:80]


def _money(v, what):
    if v in (None, ""):
        return None
    try:
        x = round(float(str(v).replace("$", "").replace(",", "")), 2)
    except ValueError:
        raise EditError(f"{what} must be a number.")
    if x < 0 or x > 100000:
        raise EditError(f"{what} looks wrong.")
    return x


def apply(conn, cfg, iid, f, ensure_day=None):
    """Turn a reviewed receipt into a stop (new or existing) with its fuel / items. Returns the stop's date."""
    r = _row(conn, iid)
    if r["status"] != "new":
        raise EditError("This receipt was already handled.")
    typ = f.get("type")
    if typ not in TYPES:
        raise EditError("Choose Gas or Vendor.")
    date = str(f.get("date") or "").strip()
    try:
        datetime.fromisoformat(date)
    except ValueError:
        raise EditError("The receipt needs a date (YYYY-MM-DD).")
    store = str(f.get("store") or "").strip()[:200]
    if not store:
        raise EditError("Give the store or station name.")
    total = _money(f.get("total"), "Total")
    lines = f.get("items") or []
    # check every line before anything is written
    plan = []
    for ln in lines:
        act = ln.get("action")
        if act not in ACTIONS:
            raise EditError("Each item needs: truck stock, used on a job, expense or skip.")
        if act == "skip":
            continue
        qty = parts._qty(ln.get("qty") or 1)
        amount = _money(ln.get("amount"), "Item amount")
        if ln.get("packs") and ln.get("part_id"):          # the receipt counts sticks / boxes: convert to stock units
            pk = parts.get_part(conn, int(ln["part_id"]))
            if pk["pack_size"]:
                qty = round(qty * pk["pack_size"], 4)
        name = str(ln.get("name") or ln.get("description") or "").strip()
        if not name:
            raise EditError("An item has no name.")
        if act == "job":
            js = conn.execute("SELECT * FROM stops WHERE id=?", (ln.get("job_stop_id"),)).fetchone()
            if not js:
                raise EditError(f"Pick the job “{name}” was used on.")
        if ln.get("part_id"):
            parts.get_part(conn, int(ln["part_id"]))
        plan.append((act, qty, amount, name, ln))
    if ensure_day:
        try:
            ensure_day(conn, cfg, date)
        except Exception:
            pass                                    # no Recorder data that day is fine: the stop is made by hand
    sid = f.get("stop_id")
    if sid:
        s = service._stop(conn, int(sid))
        if s["date"] != date:
            raise EditError("That stop is on another day.")
        sid = s["id"]
        body = {"stop_type": typ}
        if not (s["business_name"] or "").strip():
            body["business_name"] = store
        service.patch_stop(conn, cfg, sid, body)
    else:
        sid = service.add_manual(conn, cfg, date, {"arrive": f.get("arrive") or "", "depart": f.get("depart") or "",
                                                   "business_name": store, "notes": "",
                                                   "stop_type": typ})
    site = conn.execute("SELECT id FROM known_sites WHERE lower(name)=lower(?) AND kind IN ('vendor','office') "
                        "ORDER BY id LIMIT 1", (store,)).fetchone()
    if site and typ == "vendor":
        service.patch_stop(conn, cfg, sid, {"known_site_id": site["id"]})
    conn.execute("UPDATE stops SET from_receipt=1 WHERE id=?", (sid,))
    conn.commit()
    when = int(datetime.fromisoformat(f"{date}T{(f.get('time') or '12:00')}").replace(
        tzinfo=ZoneInfo(cfg.app_tz)).timestamp())
    done = {"stop_id": sid, "date": date, "type": typ, "items": []}
    if typ == "gas":
        service.set_fuel(conn, cfg, sid, {"total": total, "gallons": f.get("gallons"), "odometer": f.get("odometer")})
    else:
        for act, qty, amount, name, ln in plan:
            if act == "expense":
                parts.add_to_stop(conn, sid, {"name": EXPENSE_NAME, "type": "expense", "qty": 1,
                                              "unit_cost": amount if amount is not None else 0, "notes": name})
            else:
                if ln.get("part_id"):
                    p = parts.get_part(conn, int(ln["part_id"]))
                else:
                    p = parts.find_part(conn, name, ln.get("sku")) or parts.get_part(conn, parts.create_part(conn, {
                        "name": name, "sku": ln.get("sku") or None, "unit": ln.get("unit") or "ea",
                        "unit_cost": round(amount / qty, 4) if (act == "job" and amount) else None,
                        "billable": act == "job"}))
                if act == "stock" or p["tracked"]:
                    parts.adjust(conn, p["id"], delta=qty, paid=amount, vendor=store, ts=when,
                                 note=f"Bought at {store} (receipt {date})", stop_id=sid)
                else:
                    parts.record_purchase(conn, p, qty, amount, store, f"Bought at {store} (receipt {date})", ts=when,
                                          stop_id=sid)
                if act == "job":
                    parts.add_to_stop(conn, int(ln["job_stop_id"]), {"part_id": p["id"], "qty": qty})
            done["items"].append({"name": name, "action": act, "qty": qty, "amount": amount})
        tax = _money(f.get("tax"), "Tax")
        if tax and f.get("tax_expense"):
            parts.add_to_stop(conn, sid, {"name": EXPENSE_NAME, "type": "expense", "qty": 1, "unit_cost": tax,
                                          "notes": "Sales tax"})
            done["items"].append({"name": "Sales tax", "action": "expense", "qty": 1, "amount": tax})
        if f.get("whole_as_expense") and not plan and total:
            parts.add_to_stop(conn, sid, {"name": EXPENSE_NAME, "type": "expense", "qty": 1, "unit_cost": total,
                                          "notes": f"Receipt {date}"})
            done["items"].append({"name": "Whole receipt", "action": "expense", "qty": 1, "amount": total})
        conn.commit()
        try:
            service.finish_stop(conn, cfg, sid, {})
        except EditError:
            pass                                    # left as Draft if something is still missing
    pid, _ = photos.add(conn, cfg.data_dir, sid, image(cfg, r), "receipt", f"Receipt {store} {date}")
    rd = json.loads(r["read_json"] or "{}")
    rd["dismissed"] = True
    conn.execute("UPDATE photos SET read_json=? WHERE id=?", (json.dumps(rd), pid))
    conn.execute("UPDATE receipt_inbox SET status='done', stop_id=?, applied_json=? WHERE id=?",
                 (sid, json.dumps(done), iid))
    conn.commit()
    return date


def skip(conn, iid):
    _row(conn, iid)
    conn.execute("UPDATE receipt_inbox SET status='skipped' WHERE id=?", (iid,))
    conn.commit()


def delete(conn, cfg, iid):
    r = _row(conn, iid)
    if r["path"]:
        try:
            os.remove(os.path.join(cfg.data_dir, r["path"]))
        except OSError:
            pass
    conn.execute("DELETE FROM receipt_inbox WHERE id=?", (iid,))
    conn.commit()
