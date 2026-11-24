"""known_sites table: places the user has confirmed before."""
from datetime import datetime, timezone

import math
import re

from .. import address as addr
from ..errors import EditError
from ..geo import haversine_m
from .base import Candidate

KINDS = ("client", "home", "office", "vendor")
KIND_LABEL = {"client": "Client", "home": "Home", "office": "Office", "vendor": "Vendor"}
DEFAULT_STATUS = {"home": "Personal"}       # everything else starts as Draft (review me)

ADDR_COLS = {"line1": "addr_line1", "line2": "addr_line2", "city": "addr_city", "state": "addr_state", "zip": "addr_zip"}
EMAIL = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")

DEFAULT_SITE_RADIUS_M = 100
MERGE_NAME_RADIUS_M = 150  # same name within this distance = same site


def _cand(r):
    return Candidate(name=r["name"], address=r["address"] or "", lat=r["lat"],
                     lon=r["lon"], category=r["category"] or "", source="known",
                     known_site_id=r["id"], kind=r["kind"] or "client",
                     times_confirmed=r["times_confirmed"] or 0)


def nearby(conn, lat, lon, radius_m):
    """Known sites whose own radius (or radius_m) covers the point."""
    out = []
    for r in conn.execute("SELECT * FROM known_sites WHERE lat IS NOT NULL"):
        reach = max(radius_m, r["radius_m"] or DEFAULT_SITE_RADIUS_M)
        if haversine_m(lat, lon, r["lat"], r["lon"]) <= reach:
            out.append(_cand(r))
    return out


def search(conn, query, limit=10):
    like = f"%{query.strip()}%"
    rows = conn.execute(
        "SELECT * FROM known_sites WHERE name LIKE ? OR address LIKE ? "
        "ORDER BY times_confirmed DESC LIMIT ?", (like, like, limit))
    return [_cand(r) for r in rows]


def branch_kind(conn, name):
    """The kind another place with this name already has (a new branch of a vendor or office is one too)."""
    r = conn.execute("SELECT kind FROM known_sites WHERE lower(name)=lower(?) AND kind IN ('vendor','office') "
                     "ORDER BY times_confirmed DESC LIMIT 1", (name,)).fetchone()
    return r["kind"] if r else None


def repair_vendor_branches(conn):
    """One-time fix (2026-11-23): branches of a vendor saved as 'client' by the old learn() become vendors.
    Only places whose name matches a vendor or office place; customer places are never touched."""
    done = conn.execute("SELECT value FROM settings WHERE key='_fix_vendor_branches'").fetchone()
    if done:
        return 0
    n = conn.execute("UPDATE known_sites SET kind=(SELECT k.kind FROM known_sites k WHERE lower(k.name)=lower(known_sites.name) "
                     "AND k.kind IN ('vendor','office') ORDER BY k.times_confirmed DESC LIMIT 1) "
                     "WHERE COALESCE(kind,'client')='client' AND lower(name) IN "
                     "(SELECT lower(name) FROM known_sites WHERE kind IN ('vendor','office'))").rowcount
    conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES ('_fix_vendor_branches', ?)", (str(n),))
    conn.commit()
    return n


def learn(conn, name, address, lat, lon, category="", radius_m=None, kind=None):
    """Record a confirmed business. Returns the known_sites id. A new place takes `kind` (the stop's type: vendor or
    office), else the kind of another place with the same name (a new branch), else it is a client."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for r in conn.execute("SELECT * FROM known_sites WHERE lower(name)=lower(?)",
                          (name,)):
        if r["lat"] is None or haversine_m(lat, lon, r["lat"], r["lon"]) <= MERGE_NAME_RADIUS_M:
            n = (r["times_confirmed"] or 0) + 1
            # running average keeps the site pinned where visits really happen
            nlat = ((r["lat"] or lat) * (n - 1) + lat) / n
            nlon = ((r["lon"] or lon) * (n - 1) + lon) / n
            conn.execute(
                "UPDATE known_sites SET times_confirmed=?, last_confirmed=?, "
                "lat=?, lon=?, address=COALESCE(NULLIF(?, ''), address) WHERE id=?",
                (n, now, nlat, nlon, address or "", r["id"]))
            if address and address != (r["address"] or ""):
                split_address(conn, r["id"])
            conn.commit()
            return r["id"]
    kind = kind if kind in ("vendor", "office") else branch_kind(conn, name)
    cur = conn.execute(
        "INSERT INTO known_sites(name,address,lat,lon,radius_m,category,kind,"
        "times_confirmed,last_confirmed) VALUES (?,?,?,?,?,?,?,1,?)",
        (name, address, lat, lon, radius_m or DEFAULT_SITE_RADIUS_M, category, kind, now))
    split_address(conn, cur.lastrowid)
    conn.commit()
    return cur.lastrowid


# ---- the address in parts (street, suite, city, state, ZIP) ----------------------------------------------------
def split_address(conn, sid):
    """Store the place's one-line address split into parts (after the line changed without parts being typed)."""
    r = conn.execute("SELECT address FROM known_sites WHERE id=?", (sid,)).fetchone()
    if r:
        p = addr.split(r["address"] or "")
        conn.execute("UPDATE known_sites SET " + ", ".join(f"{c}=?" for c in ADDR_COLS.values()) + " WHERE id=?",
                     (*[p[k] for k in ADDR_COLS], sid))


def fill_address_parts(conn):
    """Once per place: split the addresses saved before the parts existed (2026-11-24)."""
    ids = [r["id"] for r in conn.execute("SELECT id FROM known_sites WHERE addr_line1 IS NULL")]
    for sid in ids:
        split_address(conn, sid)
    if ids:
        conn.commit()
    return len(ids)


def address_parts(r):
    """{line1, line2, city, state, zip} of a known_sites row ('' for each part not known)."""
    if r["addr_line1"] is None:                       # not split yet
        return addr.split(r["address"] or "")
    return {k: r[c] or "" for k, c in ADDR_COLS.items()}


def billing_email(v):
    v = str(v if v is not None else "").strip()
    if v and (len(v) > 200 or not EMAIL.match(v)):
        raise EditError("The billing email does not look like an email address.")
    return v or None


# ---- managing places (home / office / vendors / clients) -------------------
def site_dict(r):
    return {"id": r["id"], "name": r["name"], "address": r["address"] or "",
            "lat": r["lat"], "lon": r["lon"], "radius_m": r["radius_m"] or DEFAULT_SITE_RADIUS_M,
            "category": r["category"] or "", "kind": r["kind"] or "client",
            "default_status": r["default_status"] or "Draft",
            "times_confirmed": r["times_confirmed"] or 0, "last_confirmed": r["last_confirmed"],
            "link_id": r["link_id"], "old_address": bool(r["old_address"]),
            "hubspot_company_id": r["hubspot_company_id"] or "",
            "address_parts": address_parts(r), "qbo_customer_id": r["qbo_customer_id"] or "",
            "billing_email": r["billing_email"] or ""}


def list_sites(conn, kind="", q=""):
    q = q.strip().lower()
    rows = [site_dict(r) for r in conn.execute("SELECT * FROM known_sites")]
    rows = [r for r in rows if (not kind or r["kind"] == kind)
            and (not q or q in r["name"].lower() or q in r["address"].lower())]
    return sorted(rows, key=lambda r: (r["kind"] == "client", -r["times_confirmed"], r["name"].lower()))


def get_site(conn, sid):
    r = conn.execute("SELECT * FROM known_sites WHERE id=?", (sid,)).fetchone()
    if not r:
        raise EditError("That place no longer exists.")
    return r


def _coord(v, lo, hi, what):
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise EditError(f"{what} must be a number.")
    if not math.isfinite(f) or not lo <= f <= hi:
        raise EditError(f"{what} is out of range.")
    return f


def _fields(f, partial):
    out = {}
    if not partial or "name" in f:
        name = str(f.get("name") or "").strip()
        if not name:
            raise EditError("A place needs a name.")
        if len(name) > 200:
            raise EditError("The name is too long.")
        out["name"] = name
    if any(k in f for k in ADDR_COLS):              # typed in parts: the one-line address is made from them
        parts = {k: str(f.get(k) or "").strip()[:120] for k in ADDR_COLS}
        parts["state"] = parts["state"].upper() if len(parts["state"]) == 2 else parts["state"]
        out.update({c: parts[k] for k, c in ADDR_COLS.items()})
        out["address"] = addr.join(parts)[:300]
    elif "address" in f:
        out["address"] = str(f["address"] or "").strip()[:300]
        out.update({c: v for c, v in zip(ADDR_COLS.values(), addr.split(out["address"]).values())})
    if "qbo_customer_id" in f:
        q = str(f["qbo_customer_id"] or "").strip()
        if len(q) > 40:
            raise EditError("The QuickBooks customer ID is too long.")
        out["qbo_customer_id"] = q or None
    if "billing_email" in f:
        out["billing_email"] = billing_email(f["billing_email"])
    if "category" in f:
        out["category"] = str(f["category"] or "").strip()[:80]
    if not partial or "kind" in f:
        kind = f.get("kind") or "client"
        if kind not in KINDS:
            raise EditError("Kind must be client, home, office or vendor.")
        out["kind"] = kind
    if "default_status" in f and f["default_status"]:
        if f["default_status"] not in ("Draft", "Personal"):
            raise EditError("Pre-mark stops as Draft or Personal.")
        out["default_status"] = f["default_status"]
    if "radius_m" in f and f["radius_m"] not in (None, ""):
        r = _coord(f["radius_m"], 20, 2000, "Radius")
        out["radius_m"] = r
    if "hubspot_company_id" in f:
        out["hubspot_company_id"] = hubspot_id(f["hubspot_company_id"])
    if not partial or "lat" in f or "lon" in f:
        out["lat"] = _coord(f.get("lat"), -90, 90, "Latitude")
        out["lon"] = _coord(f.get("lon"), -180, 180, "Longitude")
    return out


def hubspot_id(v):
    """A HubSpot company ID: digits only (HubSpot record IDs are numbers). Blank clears it."""
    v = str(v if v is not None else "").strip()
    if v and (not v.isdigit() or len(v) > 20):
        raise EditError("A HubSpot company ID is a number (from the company's page in HubSpot, the digits in its URL).")
    return v or None


def fill_stop_hubspot(conn, site_ids=None):
    """Copy a place's HubSpot company ID to its CONFIRMED stops that have none yet. A stop that already has one is
    never rewritten (the ID it was reported with stays), so later edits on the place do not change history."""
    q = ("UPDATE stops SET hubspot_company_id=(SELECT k.hubspot_company_id FROM known_sites k WHERE k.id=stops.known_site_id) "
         "WHERE status='Confirmed' AND known_site_id IS NOT NULL AND COALESCE(hubspot_company_id,'')='' "
         "AND (SELECT COALESCE(k.hubspot_company_id,'') FROM known_sites k WHERE k.id=stops.known_site_id)!=''")
    if site_ids is not None:
        ids = list(site_ids)
        if not ids:
            return 0
        q += f" AND known_site_id IN ({','.join('?' * len(ids))})"
        return conn.execute(q, ids).rowcount
    return conn.execute(q).rowcount


# ---- places CSV (mainly to fill in HubSpot company IDs in bulk) ------------------------------------------
CSV_COLUMNS = ["place_id", "name", "kind", "address", "hubspot_company_id"]


def export_csv(conn):
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_COLUMNS)
    for r in conn.execute("SELECT * FROM known_sites ORDER BY kind, lower(name), id"):
        w.writerow([r["id"], r["name"], r["kind"] or "client", r["address"] or "", r["hubspot_company_id"] or ""])
    return buf.getvalue()


def import_csv(conn, text, dry=False):
    """Update HubSpot company IDs from a CSV (the export, edited). Rows match by place_id, else by name + address
    (case-insensitive). Only hubspot_company_id is changed; an empty cell leaves the place as it is; places are never
    created or deleted here. Confirmed stops on an updated place that have no ID yet get it too.
    Returns {updated, unchanged, not_found: [rows], errors: [..], stops_filled, dry}."""
    import csv
    import io
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        raise EditError("The file has no rows.")
    norm = {(h or "").strip().lower().replace(" ", "_"): h for h in rows[0].keys()}
    col = lambda *names: next((norm[n] for n in names if n in norm), None)
    c_id, c_name, c_addr = col("place_id", "id"), col("name"), col("address")
    c_hs = col("hubspot_company_id", "hubspot_id", "company_id", "record_id")
    if not c_hs:
        raise EditError("The file needs a hubspot_company_id column.")
    out = {"updated": 0, "unchanged": 0, "not_found": [], "errors": [], "stops_filled": 0, "dry": dry}
    changed = []
    for n, row in enumerate(rows, start=2):
        site = None
        pid = (row.get(c_id) or "").strip() if c_id else ""
        if pid.isdigit():
            site = conn.execute("SELECT * FROM known_sites WHERE id=?", (int(pid),)).fetchone()
        if site is None and c_name:
            name, addr = (row.get(c_name) or "").strip(), (row.get(c_addr) or "").strip() if c_addr else ""
            hits = conn.execute("SELECT * FROM known_sites WHERE lower(name)=lower(?) AND lower(COALESCE(address,''))=lower(?)",
                                (name, addr)).fetchall()
            site = hits[0] if len(hits) == 1 else None
        raw = (row.get(c_hs) or "").strip()
        if site is None:
            if raw:
                out["not_found"].append(f"Row {n}: {(row.get(c_name) or '').strip() if c_name else pid or '?'}")
            continue
        if not raw:
            out["unchanged"] += 1
            continue
        try:
            v = hubspot_id(raw)
        except EditError as e:
            out["errors"].append(f"Row {n} ({site['name']}): {e}")
            continue
        if v == (site["hubspot_company_id"] or None):
            out["unchanged"] += 1
            continue
        changed.append((site["id"], v))
    out["updated"] = len(changed)
    if dry or out["errors"]:
        conn.rollback()
        return out
    for sid, v in changed:
        conn.execute("UPDATE known_sites SET hubspot_company_id=? WHERE id=?", (v, sid))
    out["stops_filled"] = fill_stop_hubspot(conn, [sid for sid, _ in changed])
    conn.commit()
    return out


def create_site(conn, f):
    """Add a place. The same name within 150 m updates the existing place instead."""
    v = _fields(f, partial=False)
    v.setdefault("default_status", DEFAULT_STATUS.get(v["kind"], "Draft"))
    for r in conn.execute("SELECT * FROM known_sites WHERE lower(name)=lower(?)", (v["name"],)):
        if r["lat"] is None or haversine_m(v["lat"], v["lon"], r["lat"], r["lon"]) <= MERGE_NAME_RADIUS_M:
            _update(conn, r["id"], v)
            return r["id"]
    cur = conn.execute(
        "INSERT INTO known_sites(name,address,lat,lon,radius_m,category,kind,default_status,"
        "times_confirmed) VALUES (?,?,?,?,?,?,?,?,0)",
        (v["name"], v.get("address", ""), v["lat"], v["lon"], v.get("radius_m", DEFAULT_SITE_RADIUS_M),
         v.get("category", ""), v["kind"], v["default_status"]))
    extra = {k: v[k] for k in (*ADDR_COLS.values(), "qbo_customer_id", "billing_email", "hubspot_company_id") if k in v}
    if extra:
        _update(conn, cur.lastrowid, extra)
    else:
        split_address(conn, cur.lastrowid)
    conn.commit()
    return cur.lastrowid


def _update(conn, sid, v):
    sets = ", ".join(f"{k}=?" for k in v)  # keys come from _fields (whitelisted)
    conn.execute(f"UPDATE known_sites SET {sets} WHERE id=?", (*v.values(), sid))
    conn.commit()


def update_site(conn, sid, f):
    get_site(conn, sid)
    v = _fields(f, partial=True)
    if "kind" in v and "default_status" not in v:
        v["default_status"] = DEFAULT_STATUS.get(v["kind"], "Draft")
    if "old_address" in f:
        v["old_address"] = 1 if f["old_address"] else 0
    if v:
        _update(conn, sid, v)
    if "link_to" in f:
        link(conn, sid, f["link_to"])


# ---- linked places: one customer (or vendor) at several addresses, e.g. the old and new office after a move ----
def link(conn, sid, other):
    """Put place `sid` in the same group as place `other` (None/"" = take it out of its group). A group is the
    places sharing `link_id` (the id of the group's first place). Linking two groups merges them."""
    me = get_site(conn, sid)
    if other in (None, "", 0, "0"):
        conn.execute("UPDATE known_sites SET link_id=NULL WHERE id=?", (sid,))
        if me["link_id"]:
            left = conn.execute("SELECT id FROM known_sites WHERE link_id=?", (me["link_id"],)).fetchall()
            if len(left) == 1:
                conn.execute("UPDATE known_sites SET link_id=NULL WHERE id=?", (left[0]["id"],))
        conn.commit()
        return
    them = get_site(conn, int(other))
    if them["id"] == sid:
        raise EditError("A place cannot be linked to itself.")
    gid = them["link_id"] or me["link_id"] or min(sid, them["id"])
    for g in {me["link_id"], them["link_id"]} - {None, gid}:
        conn.execute("UPDATE known_sites SET link_id=? WHERE link_id=?", (gid, g))
    conn.execute("UPDATE known_sites SET link_id=? WHERE id IN (?, ?)", (gid, sid, them["id"]))
    conn.commit()


def delete_site(conn, sid):
    get_site(conn, sid)
    link(conn, sid, None)
    conn.execute("UPDATE stops SET known_site_id=NULL WHERE known_site_id=?", (sid,))
    conn.execute("DELETE FROM known_sites WHERE id=?", (sid,))
    conn.commit()


def match_any(conn, lat, lon):
    """Nearest known place of any kind (clients included) whose radius covers the point."""
    best, best_d = None, None
    for r in conn.execute("SELECT * FROM known_sites WHERE lat IS NOT NULL"):
        d = haversine_m(lat, lon, r["lat"], r["lon"])
        if d <= (r["radius_m"] or DEFAULT_SITE_RADIUS_M) and (best is None or d < best_d):
            best, best_d = r, d
    return best


def match_place(conn, lat, lon):
    """Nearest home/office/vendor place whose own radius covers the point, or None."""
    best, best_d = None, None
    for r in conn.execute("SELECT * FROM known_sites WHERE kind IS NOT NULL AND kind != 'client' "
                          "AND lat IS NOT NULL"):
        d = haversine_m(lat, lon, r["lat"], r["lon"])
        if d <= (r["radius_m"] or DEFAULT_SITE_RADIUS_M) and (best is None or d < best_d):
            best, best_d = r, d
    return best
