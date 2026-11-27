
from app import create_app, db
from app.places import OsmProvider, PlaceCache
from app.places import known, ranking
from app.places.base import Candidate
from tests.test_detect import CFG

LAT, LON = 34.05, -118.25


class FakeResp:
    def __init__(self, data):
        self.data = data

    def json(self):
        return self.data

    def raise_for_status(self):
        pass


class FakeSession:
    def __init__(self, overpass=None, nominatim=None):
        self.calls = []
        self.overpass, self.nominatim = overpass or {}, nominatim or []

    def post(self, url, **kw):
        self.calls.append(("post", url, kw))
        return FakeResp(self.overpass)

    def get(self, url, **kw):
        self.calls.append(("get", url, kw))
        return FakeResp(self.nominatim)


OVERPASS = {"elements": [
    {"type": "node", "lat": LAT + 0.0003, "lon": LON, "tags": {
        "name": "Acme Hardware", "shop": "hardware", "addr:housenumber": "12",
        "addr:street": "Main St", "addr:city": "Testville"}},
    {"type": "way", "center": {"lat": LAT + 0.0001, "lon": LON},
     "tags": {"name": "Oak Building", "building": "yes", "amenity": "parking"}},
    {"type": "node", "lat": LAT, "lon": LON, "tags": {"shop": "x"}},  # no name
]}


def provider(tmp_path, **kw):
    s = FakeSession(**kw)
    return OsmProvider(PlaceCache(str(tmp_path)), session=s, min_interval=0), s


def test_overpass_parse_and_cache(tmp_path):
    p, s = provider(tmp_path, overpass=OVERPASS)
    out = p.search_nearby(LAT, LON, 150)
    assert [c.name for c in out] == ["Acme Hardware", "Oak Building"]
    assert out[0].address == "12 Main St, Testville"
    assert out[0].category == "shop=hardware"
    assert "User-Agent" in s.calls[0][2]["headers"]
    p.search_nearby(LAT, LON, 150)
    assert len(s.calls) == 1  # second call served from cache


def test_nominatim_parse(tmp_path):
    p, s = provider(tmp_path, nominatim=[{
        "name": "Acme Hardware", "display_name": "Acme Hardware, Main St",
        "lat": "34.0501", "lon": "-118.2502", "category": "shop", "type": "hardware",
        "address": {"house_number": "12", "road": "Main St", "city": "Testville",
                    "state": "CA", "postcode": "90001"}}])
    out = p.search_text("acme", LAT, LON)
    assert out[0].address == "12 Main St, Testville, CA, 90001"
    assert out[0].category == "shop=hardware" and out[0].lat == 34.0501


def test_ranking_known_first_then_distance_then_business():
    near_bare = Candidate("Bare Bldg", lat=LAT + 0.0001, lon=LON,
                          category="building=yes")
    far_biz = Candidate("Far Shop", address="1 A St", lat=LAT + 0.0010, lon=LON,
                        category="shop=hardware")
    near_biz = Candidate("Near Shop", address="2 A St", lat=LAT + 0.0002, lon=LON,
                         category="shop=hardware")
    known_far = Candidate("My Client", lat=LAT + 0.0012, lon=LON, source="known",
                          known_site_id=1, times_confirmed=3)
    r = ranking.rank([near_bare, far_biz, near_biz, known_far], LAT, LON, 30)
    assert r[0].name == "My Client"
    assert [c.name for c in r[1:]] == ["Near Shop", "Far Shop", "Bare Bldg"] or \
           [c.name for c in r[1:]] == ["Near Shop", "Bare Bldg", "Far Shop"]
    assert r.index(near_biz) < r.index(far_biz)
    assert r.index(near_biz) < r.index(near_bare)  # business beats bare building


def test_ranking_dedupes_provider_copy_of_known_site():
    k = Candidate("Acme Hardware", lat=LAT, lon=LON, source="known", known_site_id=1)
    o = Candidate("acme hardware", lat=LAT + 0.0001, lon=LON, category="shop=hardware")
    r = ranking.rank([k, o], LAT, LON)
    assert len(r) == 1 and r[0].source == "known"


def test_learning_is_idempotent_and_counts(tmp_path):
    conn = db.connect(str(tmp_path))
    a = known.learn(conn, "Acme Hardware", "12 Main St", LAT, LON, "shop=hardware")
    b = known.learn(conn, "ACME hardware", "", LAT + 0.0002, LON)
    c = known.learn(conn, "Acme Hardware", "", LAT + 0.5, LON)  # far away = new site
    assert a == b and c != a
    row = conn.execute("SELECT * FROM known_sites WHERE id=?", (a,)).fetchone()
    assert row["times_confirmed"] == 2 and row["address"] == "12 Main St"
    assert [x.name for x in known.nearby(conn, LAT, LON, 100)] == ["Acme Hardware"]
    assert known.search(conn, "acme")


def test_suggest_endpoint(tmp_path):
    from dataclasses import replace
    cfg = replace(CFG, data_dir=str(tmp_path))
    p, _ = provider(tmp_path, overpass=OVERPASS)
    c = create_app(cfg, client=object(), provider=p).test_client()
    assert c.post("/api/places", json={
        "name": "My Client", "lat": LAT, "lon": LON}).status_code == 200
    r = c.post("/api/suggest", json={"lat": LAT, "lon": LON, "minutes": 30}).json
    assert r["candidates"][0]["name"] == "My Client"
    assert {x["name"] for x in r["candidates"]} >= {"Acme Hardware"}
    assert c.post("/api/suggest").status_code == 400


def test_suggest_survives_provider_failure(tmp_path):
    from dataclasses import replace

    class Boom:
        def search_nearby(self, *a):
            raise RuntimeError("down")

    cfg = replace(CFG, data_dir=str(tmp_path))
    c = create_app(cfg, client=object(), provider=Boom()).test_client()
    c.post("/api/places", json={"name": "My Client", "lat": LAT, "lon": LON})
    r = c.post("/api/suggest", json={"lat": LAT, "lon": LON}).json
    assert r["candidates"][0]["name"] == "My Client" and "failed" in r["error"]


def test_text_search_prefers_nearby_over_far(tmp_path):
    from dataclasses import replace
    far = {"name": "Hardware Store", "display_name": "Hardware Store, Far",
           "lat": "10.0", "lon": "10.0", "category": "shop", "type": "hardware"}
    near = {"name": "Hardware Store", "display_name": "Hardware Store, Near",
            "lat": "34.06", "lon": "-118.25", "category": "shop", "type": "hardware"}
    p, _ = provider(tmp_path, nominatim=[far, near])
    cfg = replace(CFG, data_dir=str(tmp_path))
    c = create_app(cfg, client=object(), provider=p).test_client()
    r = c.post("/api/suggest", json={"lat": LAT, "lon": LON, "q": "hardware"}).json
    assert r["candidates"][0]["lat"] == 34.06


def test_osm_reverse_formats_and_caches(tmp_path):
    class S:
        calls = 0

        def get(self, url, **kw):
            S.calls += 1
            assert url.endswith("/reverse")
            return FakeResp({"address": {"house_number": "12", "road": "Main St", "town": "Testville",
                                         "state": "California", "postcode": "90001"}})
    p = OsmProvider(PlaceCache(str(tmp_path)), session=S(), min_interval=0)
    assert p.reverse(34.05, -118.25) == "12 Main St, Testville, California, 90001"
    p.reverse(34.05, -118.25)
    assert S.calls == 1


def test_overpass_falls_back_to_next_mirror(tmp_path):
    import requests

    class S:
        urls = []

        def post(self, url, **kw):
            S.urls.append(url)
            if "bad" in url:
                raise requests.ConnectionError("down")
            return FakeResp({"elements": [{"type": "node", "lat": 1.0, "lon": 2.0,
                                           "tags": {"name": "Acme", "shop": "hardware"}}]})
    p = OsmProvider(PlaceCache(str(tmp_path)), session=S(), min_interval=0,
                    overpass_url="https://bad.example/api, https://good.example/api")
    assert [c.name for c in p.search_nearby(1.0, 2.0, 100)] == ["Acme"]
    assert S.urls == ["https://bad.example/api", "https://good.example/api"]
