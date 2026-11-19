"""Google Places API (New) provider. Optional; costs money per request.

Safeguards: every uncached request is counted against a daily cap (settings),
responses are cached briefly to avoid paying twice for the same lookup, and the
API key only ever travels in a request header (never in a URL or log line).
"""
import requests

from .base import Candidate, PlacesError, Provider
from .cache import PlaceCache, UsageCounter

BASE = "https://places.googleapis.com/v1/places"
FIELDS = ("places.id,places.displayName,places.formattedAddress,"
          "places.location,places.types,places.primaryType")
CACHE_SECONDS = 24 * 3600  # short on purpose: Google's terms limit caching of Places content


class GoogleProvider(Provider):
    def __init__(self, api_key, cache: PlaceCache, usage: UsageCounter, daily_cap,
                 session=None):
        self.api_key, self.cache, self.usage = api_key, cache, usage
        self.daily_cap = daily_cap
        self.session = session or requests

    def _call(self, endpoint, body, cache_key):
        data = self.cache.get(cache_key)
        if data is None:
            self.usage.hit(self.daily_cap)  # counted before sending (conservative)
            try:
                r = self.session.post(
                    f"{BASE}:{endpoint}", json=body, timeout=15,
                    headers={"X-Goog-Api-Key": self.api_key,
                             "X-Goog-FieldMask": FIELDS,
                             "Content-Type": "application/json"})
            except requests.RequestException as e:
                raise PlacesError(f"Could not reach Google ({type(e).__name__}).")
            if getattr(r, "status_code", 200) >= 400:
                try:
                    msg = r.json()["error"]["message"]
                except Exception:
                    msg = f"HTTP {r.status_code}"
                raise PlacesError(f"Google: {msg}")
            data = r.json()
            self.cache.set(cache_key, data)
        return self._parse(data)

    @staticmethod
    def _parse(data):
        out = []
        for p in data.get("places", []):
            loc = p.get("location") or {}
            name = (p.get("displayName") or {}).get("text")
            if not name or "latitude" not in loc:
                continue
            kind = p.get("primaryType") or (p.get("types") or [""])[0]
            out.append(Candidate(
                name=name, address=p.get("formattedAddress", ""),
                lat=loc["latitude"], lon=loc["longitude"],
                category=f"google={kind}" if kind else "", source="google"))
        return out

    def search_nearby(self, lat, lon, radius_m):
        body = {"maxResultCount": 20, "locationRestriction": {"circle": {
            "center": {"latitude": lat, "longitude": lon}, "radius": float(radius_m)}}}
        key = PlaceCache.key("g-nearby", round(lat, 4), round(lon, 4), int(radius_m))
        return self._call("searchNearby", body, key)

    def search_text(self, query, lat, lon):
        body = {"textQuery": query, "maxResultCount": 8, "locationBias": {"circle": {
            "center": {"latitude": lat, "longitude": lon}, "radius": 50000.0}}}
        key = PlaceCache.key("g-text", query.strip().lower(), round(lat, 2), round(lon, 2))
        return self._call("searchText", body, key)
