"""Road distance: rounding, caching, and fallback to the straight-line estimate."""
import sqlite3
from dataclasses import replace

from app import routing
from tests.test_detect import CFG


def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE route_cache (key TEXT PRIMARY KEY, meters REAL NOT NULL, ts INTEGER NOT NULL)")
    return c


class Resp:
    def raise_for_status(self): pass
    def json(self): return {"routes": [{"distance": 16093.44}]}


def test_routed_rounded_and_cached(monkeypatch):
    seen = []
    monkeypatch.setattr(routing.requests, "get", lambda url, **kw: seen.append(url) or Resp())
    monkeypatch.setattr(routing, "_down_until", 0.0)
    cfg, c = replace(CFG, routing_url="http://osrm.test"), conn()
    m, how = routing.route_m(c, cfg, (34.123456, -118.654321), (34.2, -118.7))
    assert (round(m / 1609.344, 1), how) == (10.0, "routed")
    assert "-118.654,34.123;-118.7,34.2" in seen[0]          # rounded before leaving the server
    routing.route_m(c, cfg, (34.12349, -118.65432), (34.2, -118.7))
    assert len(seen) == 1                                    # second call served from the cache


def test_falls_back_to_estimate_and_backs_off(monkeypatch):
    calls = []

    def boom(url, **kw):
        calls.append(url)
        raise OSError("down")
    monkeypatch.setattr(routing.requests, "get", boom)
    monkeypatch.setattr(routing, "_down_until", 0.0)
    cfg, c = replace(CFG, routing_url="http://osrm.test"), conn()
    m, how = routing.route_m(c, cfg, (34.0, -118.0), (34.1, -118.0))
    assert how == "estimate" and 13000 < m < 15000            # ~11.1 km straight x 1.25
    routing.route_m(c, cfg, (34.0, -118.0), (34.2, -118.0))
    assert len(calls) == 1                                   # no hammering while it is down


def test_blank_url_never_calls_out(monkeypatch):
    monkeypatch.setattr(routing.requests, "get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("called")))
    assert routing.route_m(conn(), CFG, (34.0, -118.0), (34.1, -118.0))[1] == "estimate"
