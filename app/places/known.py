"""known_sites table: places the user has confirmed before."""
from datetime import datetime, timezone

import math

from ..errors import EditError
from ..geo import haversine_m
from .base import Candidate

KINDS = ("client", "home", "office", "vendor")
KIND_LABEL = {"client": "Client", "home": "Home", "office": "Office", "vendor": "Vendor"}
DEFAULT_STATUS = {"home": "Personal"}       # everything else starts as Draft (review me)

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
            conn.commit()
            return r["id"]
    kind = kind if kind in ("vendor", "office") else branch_kind(conn, name)
    cur = conn.execute(
        "INSERT INTO known_sites(name,address,lat,lon,radius_m,category,kind,"
        "times_confirmed,last_confirmed) VALUES (?,?,?,?,?,?,?,1,?)",
        (name, address, lat, lon, radius_m or DEFAULT_SITE_RADIUS_M, category, kind, now))
    conn.commit()
    return cur.lastrowid


# ---- managing places (home / office / vendors / clients) -------------------
def site_dict(r):
    return {"id": r["id"], "name": r["name"], "address": r["address"] or "",
            "lat": r["lat"], "lon": r["lon"], "radius_m": r["radius_m"] or DEFAULT_SITE_RADIUS_M,
            "category": r["category"] or "", "kind": r["kind"] or "client",
            "default_status": r["default_status"] or "Draft",
            "times_confirmed": r["times_confirmed"] or 0, "last_confirmed": r["last_confirmed"],
            "link_id": r["link_id"], "old_address": bool(r["old_address"])}


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
    if "address" in f:
        out["address"] = str(f["address"] or "").strip()[:300]
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
    if not partial or "lat" in f or "lon" in f:
        out["lat"] = _coord(f.get("lat"), -90, 90, "Latitude")
        out["lon"] = _coord(f.get("lon"), -180, 180, "Longitude")
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
