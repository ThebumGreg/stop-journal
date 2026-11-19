"""known_sites table: places the user has confirmed before."""
from datetime import datetime, timezone

from ..geo import haversine_m
from .base import Candidate

DEFAULT_SITE_RADIUS_M = 100
MERGE_NAME_RADIUS_M = 150  # same name within this distance = same site


def _cand(r):
    return Candidate(name=r["name"], address=r["address"] or "", lat=r["lat"],
                     lon=r["lon"], category=r["category"] or "", source="known",
                     known_site_id=r["id"],
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


def learn(conn, name, address, lat, lon, category="", radius_m=None):
    """Record a confirmed business. Returns the known_sites id."""
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
    cur = conn.execute(
        "INSERT INTO known_sites(name,address,lat,lon,radius_m,category,"
        "times_confirmed,last_confirmed) VALUES (?,?,?,?,?,?,1,?)",
        (name, address, lat, lon, radius_m or DEFAULT_SITE_RADIUS_M, category, now))
    conn.commit()
    return cur.lastrowid
