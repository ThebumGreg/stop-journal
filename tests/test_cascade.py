from dataclasses import replace

from app import create_app
from app.places import PlacesError, ranking
from app.places.base import Candidate
from tests.test_detect import CFG

LAT, LON = 34.05, -118.25


class Fake:
    def __init__(self, items=None, error=None):
        self.items, self.error, self.calls = items or [], error, 0

    def search_nearby(self, *a):
        self.calls += 1
        if self.error:
            raise self.error
        return list(self.items)

    search_text = search_nearby


def biz(name, d=0.0001, src="osm", addr=""):
    return Candidate(name, address=addr, lat=LAT + d, lon=LON, category="shop=hardware", source=src)


def client(tmp_path, osm, google, mode="osm_google", **kw):
    cfg = replace(CFG, data_dir=str(tmp_path), places_provider=mode, **kw)
    return create_app(cfg, client=object(), provider={"osm": osm, "google": google}).test_client()


def q(c, **extra):
    return c.post("/api/suggest", json={"lat": LAT, "lon": LON, **extra}).json


def test_osm_enough_results_skips_google(tmp_path):
    osm, g = Fake([biz("A"), biz("B", .0002), biz("C", .0003)]), Fake([biz("G", src="google")])
    r = q(client(tmp_path, osm, g))
    assert g.calls == 0 and r["sources"] == ["your sites", "osm"] and not r["google_used"]
    assert r["google_available"]  # the manual button is offered


def test_osm_thin_results_falls_back_to_google(tmp_path):
    osm, g = Fake([biz("A")]), Fake([biz("G", .0002, "google")])
    r = q(client(tmp_path, osm, g))
    assert g.calls == 1 and r["google_used"]
    assert {c["name"] for c in r["candidates"]} == {"A", "G"}


def test_osm_outage_falls_back_to_google_and_reports_it(tmp_path):
    osm, g = Fake(error=PlacesError("osm down")), Fake([biz("G", src="google")])
    r = q(client(tmp_path, osm, g))
    assert g.calls == 1 and "osm down" in r["error"] and r["candidates"][0]["name"] == "G"


def test_known_site_nearby_skips_google(tmp_path):
    osm, g = Fake(), Fake([biz("G", src="google")])
    c = client(tmp_path, osm, g)
    c.post("/api/places", json={"name": "My Client", "lat": LAT, "lon": LON})
    r = q(c)
    assert g.calls == 0 and r["candidates"][0]["name"] == "My Client"


def test_manual_google_button(tmp_path):
    osm, g = Fake([biz("A"), biz("B"), biz("C")]), Fake([biz("G", .0004, "google")])
    r = q(client(tmp_path, osm, g), google=True)
    assert g.calls == 1 and r["google_used"] and "G" in {c["name"] for c in r["candidates"]}


def test_osm_only_mode_never_auto_google_but_allows_button(tmp_path):
    osm, g = Fake(), Fake([biz("G", src="google")])
    c = client(tmp_path, osm, g, mode="osm")
    assert g.calls == 0 and q(c)["candidates"] == [] and g.calls == 0
    assert q(c, google=True)["candidates"][0]["name"] == "G"


def test_fallback_threshold_zero_means_button_only(tmp_path):
    osm, g = Fake(), Fake([biz("G", src="google")])
    q(client(tmp_path, osm, g, google_fallback_min=0))
    assert g.calls == 0


def test_dedupe_same_place_prefers_one_with_address():
    a = biz("Acme Hardware")
    b = biz("ACME hardware", .00005, "google", addr="12 Main St")
    out = ranking.rank([a, b], LAT, LON)
    assert len(out) == 1 and out[0].address == "12 Main St"
