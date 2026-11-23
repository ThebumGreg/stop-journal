"""Reading receipts: turn a receipt photo into suggested numbers (fuel total, gallons, price; a store total).

Nothing is filled in automatically: the stop sheet shows "Receipt says ..." and you tap Use these.

Readers (Settings → Receipts, RECEIPT_READER):
- off (default)
- google_vision: Google Cloud Vision text detection. Uses RECEIPT_KEY, or the Google Places key when that is blank (the
  key's project needs "Cloud Vision API" enabled, and the key must be allowed to call it). The receipt image goes to
  Google; it carries no GPS (photos are re-encoded without metadata first). First 1000 a month are free.
- Later: an AI reader (Claude or similar) slots in as another Reader returning the same dict; see READERS.

A reader returns text; `parse` finds the numbers. An AI reader may return the numbers directly (then `read` skips
`parse`). Every call is counted per month (`api_usage`, provider "receipts") and stops at RECEIPT_MONTHLY_CAP.
"""
import base64
import json
import re
from datetime import datetime, timezone

import requests

from .errors import EditError

TIMEOUT_S = 20
VISION_URL = "https://vision.googleapis.com/v1/images:annotate"


class ReceiptError(Exception):
    pass


# ---- readers ------------------------------------------------------------------
def google_vision(cfg, jpeg, session=None):
    key = cfg.receipt_key or cfg.google_places_key
    if not key:
        raise ReceiptError("No Google key: add one in Settings → Receipts (or Places).")
    body = {"requests": [{"image": {"content": base64.b64encode(jpeg).decode()},
                          "features": [{"type": "DOCUMENT_TEXT_DETECTION"}],
                          "imageContext": {"languageHints": ["en"]}}]}
    try:
        r = (session or requests).post(VISION_URL, params={"key": key}, json=body, timeout=TIMEOUT_S)
    except requests.RequestException as e:
        raise ReceiptError(f"Could not reach Google Vision ({type(e).__name__}).")
    try:
        d = r.json()
    except ValueError:
        d = {}
    if r.status_code != 200:
        msg = ((d.get("error") or {}).get("message") or "")[:160]
        if r.status_code == 403:
            raise ReceiptError("Google refused the key for Vision. Enable “Cloud Vision API” in that Google Cloud "
                               "project and allow the key to use it. " + msg)
        raise ReceiptError(f"Google Vision answered {r.status_code}. {msg}")
    res = (d.get("responses") or [{}])[0]
    if res.get("error"):
        raise ReceiptError("Google Vision: " + str(res["error"].get("message", ""))[:160])
    return {"text": (res.get("fullTextAnnotation") or {}).get("text", "")}


def claude(cfg, jpeg, session=None):
    """Claude reads the receipt itself (no text step): store, date, totals, fuel numbers and line items."""
    from . import ai
    try:
        d = ai.read_receipt(cfg, jpeg)
    except ai.AIError as e:
        raise ReceiptError(str(e))
    num = lambda v: round(float(v), 4) if isinstance(v, (int, float)) and v >= 0 else None
    total, gallons, ppg = num(d.get("total")), num(d.get("gallons")), num(d.get("price_per_gallon"))
    if not d.get("is_fuel"):
        gallons = ppg = None
    if gallons is not None and not 0.5 <= gallons <= 60:
        gallons = ppg = None
    if ppg is not None and not 1.0 <= ppg <= 15:
        ppg = None
    if gallons is None and ppg and total:
        gallons = round(total / ppg, 3)
    if ppg is None and gallons and total:
        ppg = round(total / gallons, 3)
    items = []
    for it in (d.get("items") or [])[:30]:
        desc = str(it.get("description") or "").strip()[:120]
        if not desc:
            continue
        qty, unit, amount = num(it.get("qty")), num(it.get("unit_price")), num(it.get("amount"))
        if unit is None and amount is not None:
            unit = round(amount / qty, 4) if qty else amount
        items.append({"description": desc, "sku": str(it.get("sku") or "").strip()[:40], "qty": qty or 1,
                      "unit_price": unit, "amount": amount})
    date = str(d.get("date") or "").strip()
    return {"fields": {
        "total": total, "gallons": gallons, "price_per_gallon": ppg,
        "checks": bool(total and gallons and ppg and abs(gallons * ppg - total) <= max(0.02 * total, 0.05)),
        "store": str(d.get("store") or "").strip()[:80], "date": date if re.match(r"^\d{4}-\d{2}-\d{2}$", date) else "",
        "tax": num(d.get("tax")), "items": items}}


READERS = {"google_vision": google_vision, "claude": claude}


# ---- finding the numbers ----------------------------------------------------------
MONEY = r"\$?\s*(\d{1,4}(?:[.,]\d{2}))"
_total_words = re.compile(r"\b(?:grand\s+)?total(?:\s+sale)?\b|\bamount\s+(?:due|paid)\b|\bsale\b|\bfuel\s+sale\b|\bpurchase\b", re.I)
_not_total = re.compile(r"sub\s*-?\s*total|tax|savings|discount|change|cash\s+back|balance|points", re.I)
_gallons = re.compile(r"(\d{1,3}[.,]\d{2,3})\s*(?:gal|g\b|gallons)|(?:gal(?:lons)?|volume|qty)\s*[:=]?\s*(\d{1,3}[.,]\d{2,3})", re.I)
_ppg = re.compile(r"(?:price\s*/?\s*(?:gal|g)|\$\s*/\s*gal|per\s+gal(?:lon)?|ppg|unit\s+price|@)\s*[:=]?\s*\$?\s*(\d\.\d{2,3})"
                  r"|(\d\.\d{2,3})\s*/\s*(?:gal|g)\b", re.I)


def _num(s):
    return float(s.replace(",", "."))


def parse(text):
    """Receipt text -> {total, gallons, price_per_gallon, checks}. Missing values are None.

    total: the amount on a line saying TOTAL / SALE / AMOUNT (not subtotal, tax, change); else the largest amount.
    checks: True when gallons x price matches the total within 2% (a good sign nothing was misread)."""
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    total = None
    for i, l in enumerate(lines):
        if _total_words.search(l) and not _not_total.search(l):
            m = re.findall(MONEY, l) or (re.findall(MONEY, lines[i + 1]) if i + 1 < len(lines) else [])
            if m:
                total = _num(m[-1])
    amounts = [_num(m) for l in lines if not _not_total.search(l) for m in re.findall(r"\$\s*(\d{1,4}[.,]\d{2})\b", l)]
    if total is None and amounts:
        total = max(amounts)
    gallons = ppg = None
    for l in lines:
        if gallons is None:
            m = _gallons.search(l)
            if m:
                gallons = _num(m.group(1) or m.group(2))
        if ppg is None:
            m = _ppg.search(l)
            if m:
                ppg = _num(m.group(1) or m.group(2))
    if gallons is None and ppg and total:
        gallons = round(total / ppg, 3)
    if ppg is None and gallons and total:
        ppg = round(total / gallons, 3)
    checks = bool(total and gallons and ppg and abs(gallons * ppg - total) <= max(0.02 * total, 0.05))
    if gallons is not None and not 0.5 <= gallons <= 60:     # not a vehicle fill-up: forget the fuel numbers
        gallons = ppg = None
        checks = False
    if ppg is not None and not 1.0 <= ppg <= 15:
        ppg = None
        checks = False
    return {"total": total, "gallons": gallons, "price_per_gallon": ppg, "checks": checks}


# ---- running a reader on a stored receipt ------------------------------------------
def _month():
    return datetime.now(timezone.utc).strftime("%Y-%m")


def used_this_month(conn):
    r = conn.execute("SELECT COALESCE(SUM(count),0) FROM api_usage WHERE provider='receipts' AND day LIKE ?",
                     (_month() + "%",)).fetchone()
    return r[0]


def _count(conn, cap):
    conn.execute("BEGIN IMMEDIATE")
    if used_this_month(conn) >= cap:
        conn.rollback()
        raise ReceiptError(f"Monthly receipt-reading limit reached ({cap}). Change it in Settings → Receipts.")
    day = datetime.now(timezone.utc).date().isoformat()
    conn.execute("INSERT INTO api_usage(day, provider, count) VALUES (?, 'receipts', 1) "
                 "ON CONFLICT(day, provider) DO UPDATE SET count=count+1", (day,))
    conn.commit()


def read(conn, cfg, photo_id, jpeg, session=None):
    """Read one receipt and store the result on the photo (photos.read_json). Returns the stored dict.
    Errors are stored too, so the sheet can say why nothing was suggested."""
    out = extract(conn, cfg, jpeg, session)
    conn.execute("UPDATE photos SET read_json=? WHERE id=?", (json.dumps(out), photo_id))
    conn.commit()
    return out


def extract(conn, cfg, jpeg, session=None):
    """Run the chosen reader on a receipt (counted against the monthly cap): {ok, reader, total, …} or {ok: False, error}."""
    reader = READERS.get(cfg.receipt_reader)
    if not reader:
        raise EditError("Receipt reading is off (Settings → Receipts).")
    try:
        _count(conn, cfg.receipt_monthly_cap)
        got = reader(cfg, jpeg, session)
        fields = got.get("fields") or parse(got.get("text", ""))
        out = {"ok": True, "reader": cfg.receipt_reader, **fields}
        if not any(fields.get(k) for k in ("total", "gallons")):
            out = {"ok": False, "reader": cfg.receipt_reader, "error": "No total found on this receipt. "
                   "Try a flat, well-lit photo that fills the screen."}
    except ReceiptError as e:
        out = {"ok": False, "reader": cfg.receipt_reader, "error": str(e)}
    return out
