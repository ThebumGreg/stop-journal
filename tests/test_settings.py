from dataclasses import replace

import pytest

from app import create_app, db, settings
from app.places import GoogleProvider, PlaceCache, PlacesError, UsageCounter, get_provider
from tests.test_detect import CFG

LAT, LON = 34.05, -118.25


class Resp:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status

    def json(self):
        return self.data


class GSession:
    def __init__(self, data=None, status=200):
        self.data, self.status, self.calls = data or {}, status, []

    def post(self, url, **kw):
        self.calls.append((url, kw))
        return Resp(self.data, self.status)


GOOGLE_OK = {"places": [{
    "id": "abc", "displayName": {"text": "Acme Hardware"},
    "formattedAddress": "12 Main St, Testville, CA", "primaryType": "hardware_store",
    "location": {"latitude": 34.0502, "longitude": -118.2501}}]}


def gp(tmp_path, session, cap=5):
    return GoogleProvider("KEY123", PlaceCache(str(tmp_path)),
                          UsageCounter(str(tmp_path), "google"), cap, session=session)


def app_for(tmp_path, **kw):
    cfg = replace(CFG, data_dir=str(tmp_path), **kw)
    return create_app(cfg, client=object())


def test_google_parse_headers_and_cache(tmp_path):
    s = GSession(GOOGLE_OK)
    p = gp(tmp_path, s)
    out = p.search_nearby(LAT, LON, 150)
    assert out[0].name == "Acme Hardware" and out[0].source == "google"
    assert out[0].category == "google=hardware_store"
    url, kw = s.calls[0]
    assert url.endswith("places:searchNearby") and "KEY123" not in url
    assert kw["headers"]["X-Goog-Api-Key"] == "KEY123"
    p.search_nearby(LAT, LON, 150)
    assert len(s.calls) == 1  # cached, not billed twice


def test_google_daily_cap(tmp_path):
    s = GSession(GOOGLE_OK)
    p = gp(tmp_path, s, cap=2)
    p.search_text("a", LAT, LON)
    p.search_text("b", LAT, LON)
    with pytest.raises(PlacesError, match="cap reached"):
        p.search_text("c", LAT, LON)
    assert len(s.calls) == 2 and UsageCounter(str(tmp_path), "google").count() == 2


def test_google_error_message_has_no_key(tmp_path):
    s = GSession({"error": {"message": "API key not valid."}}, status=400)
    with pytest.raises(PlacesError) as e:
        gp(tmp_path, s).search_text("x", LAT, LON)
    assert "API key not valid" in str(e.value) and "KEY123" not in str(e.value)


def test_get_provider_google_needs_key(tmp_path):
    with pytest.raises(PlacesError, match="no API key"):
        get_provider(replace(CFG, data_dir=str(tmp_path), places_provider="google"))


def test_settings_override_and_revert(tmp_path):
    c = app_for(tmp_path).test_client()
    r = c.post("/settings", data={"stop_radius_m": "250", "places_provider": "osm"})
    assert r.status_code == 302
    conn = db.connect(str(tmp_path))
    assert settings.effective(CFG, conn).stop_radius_m == 250
    c.post("/settings", data={})  # blank everything = back to env defaults
    assert settings.effective(CFG, conn).stop_radius_m == CFG.stop_radius_m


def test_settings_validation_blocks_save(tmp_path):
    c = app_for(tmp_path).test_client()
    r = c.post("/settings", data={"stop_radius_m": "5", "stop_min_minutes": "abc"})
    assert r.status_code == 400
    assert b"must be between" in r.data and b"whole number" in r.data
    assert not db.connect(str(tmp_path)).execute("SELECT 1 FROM settings WHERE key='stop_radius_m'").fetchone()


def test_google_key_is_write_only(tmp_path):
    c = app_for(tmp_path).test_client()
    c.post("/settings", data={"google_places_key": "SECRETKEY9999"})
    page = c.get("/settings").data.decode()
    assert "SECRETKEY9999" not in page and "…9999" in page
    c.post("/settings", data={"clear_google_places_key": "1"})
    assert "not set" in c.get("/settings").data.decode()


def test_password_gate(tmp_path):
    c = app_for(tmp_path, app_password="pw").test_client()
    assert c.get("/health").status_code == 200
    assert c.get("/api/day/2026-11-18").status_code == 401
    assert c.get("/settings").status_code == 302
    assert c.post("/settings", data={}).status_code == 401
    assert c.post("/login", data={"password": "nope"}).status_code == 401
    assert c.post("/login?next=//evil.com", data={"password": "pw"}).headers["Location"] == "/"
    assert c.get("/settings").status_code == 200


def test_suggest_reports_google_error_and_still_lists_known(tmp_path):
    c = app_for(tmp_path).test_client()
    c.post("/settings", data={"places_provider": "google"})  # no key set
    c.post("/api/places", json={"name": "My Client", "lat": LAT, "lon": LON})
    r = c.get(f"/api/suggest?lat={LAT}&lon={LON}").json
    assert "no API key" in r["error"] and r["candidates"][0]["name"] == "My Client"
