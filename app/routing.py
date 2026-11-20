"""Road distance between two points, used only to document the miles from where the day ended to
Home. Public OSRM demo server by default (free, no key). Privacy: coordinates are rounded to 3
decimals (~100 m) before they leave the server, results are cached, and a blank URL turns the
call off (a straight-line x1.25 estimate is used instead, labelled as such).
"""
import time

import requests

from .geo import haversine_m

USER_AGENT = "stop-journal/0.1 (self-hosted personal job-site logger)"
CROW_FLIES_FACTOR = 1.25
_down_until = 0.0     # after a failure, skip the network for a while so pages stay fast


def _r(v):
    return round(float(v), 3)


def route_m(conn, cfg, a, b):
    """(meters, method) for the drive from a=(lat, lon) to b. method is 'routed' or 'estimate'."""
    global _down_until
    a, b = (_r(a[0]), _r(a[1])), (_r(b[0]), _r(b[1]))
    key = f"{a[0]},{a[1]}>{b[0]},{b[1]}"
    row = conn.execute("SELECT meters FROM route_cache WHERE key=?", (key,)).fetchone()
    if row:
        return row["meters"], "routed"
    url = (cfg.routing_url or "").strip().rstrip("/")
    if url and time.time() >= _down_until:
        try:
            r = requests.get(f"{url}/route/v1/driving/{a[1]},{a[0]};{b[1]},{b[0]}",
                             params={"overview": "false"}, headers={"User-Agent": USER_AGENT}, timeout=5)
            r.raise_for_status()
            m = float(r.json()["routes"][0]["distance"])
            conn.execute("INSERT OR REPLACE INTO route_cache(key,meters,ts) VALUES (?,?,?)",
                         (key, m, int(time.time())))
            conn.commit()
            return m, "routed"
        except Exception:
            _down_until = time.time() + 600
    return haversine_m(a[0], a[1], b[0], b[1]) * CROW_FLIES_FACTOR, "estimate"
