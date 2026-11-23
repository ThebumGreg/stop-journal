"""OSM provider: Overpass for nearby named POIs, Nominatim for text search.

Usage policy: identifying User-Agent, at most ~1 request/second per host,
every response cached (coordinates rounded to ~11 m for the cache key).
"""
import threading
import time

import requests

from .base import Candidate, Provider
from .cache import PlaceCache

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
USER_AGENT = "stop-journal/0.1 (self-hosted personal job-site logger)"
POI_KEYS = ["shop", "office", "amenity", "craft", "industrial",
            "healthcare", "tourism", "leisure"]

_lock = threading.Lock()
_last_call = {}


def _throttle(host, min_interval):
    with _lock:
        wait = _last_call.get(host, 0) + min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_call[host] = time.monotonic()


def _address(tags):
    street = " ".join(x for x in (tags.get("addr:housenumber"),
                                  tags.get("addr:street")) if x)
    parts = [street, tags.get("addr:city"), tags.get("addr:state"),
             tags.get("addr:postcode")]
    return ", ".join(p for p in parts if p)


def _category(tags):
    if tags.get("amenity") == "fuel":       # a gas station with a shop is still a gas station
        return "amenity=fuel"
    for k in POI_KEYS:
        if k in tags:
            return f"{k}={tags[k]}"
    return ""


class OsmProvider(Provider):
    def __init__(self, cache: PlaceCache, session=None, min_interval=1.1,
                 overpass_url=OVERPASS_URL):
        self.overpass_url = overpass_url
        self.cache = cache
        self.session = session or requests
        self.min_interval = min_interval
        self.headers = {"User-Agent": USER_AGENT}

    # -- nearby (Overpass) ---------------------------------------------
    def search_nearby(self, lat, lon, radius_m):
        key = PlaceCache.key("overpass", round(lat, 4), round(lon, 4), int(radius_m))
        data = self.cache.get(key)
        if data is None:
            around = f"(around:{int(radius_m)},{lat},{lon})"
            body = "".join(f'nwr{around}["name"]["{k}"];' for k in POI_KEYS)
            q = f"[out:json][timeout:15];({body});out center tags 80;"
            data, last_err = None, None
            for url in [u.strip() for u in self.overpass_url.replace(",", " ").split() if u.strip()]:
                _throttle("overpass", self.min_interval)
                try:   # public Overpass servers are often overloaded: try the next mirror
                    r = self.session.post(url, data={"data": q}, headers=self.headers, timeout=12)
                    r.raise_for_status()
                    data = r.json()
                    break
                except (requests.RequestException, ValueError) as e:
                    last_err = e
            if data is None:
                raise last_err or requests.RequestException("no Overpass server configured")
            self.cache.set(key, data)
        out = []
        for el in data.get("elements", []):
            tags = el.get("tags", {})
            la = el.get("lat", (el.get("center") or {}).get("lat"))
            lo = el.get("lon", (el.get("center") or {}).get("lon"))
            if la is None or lo is None or not tags.get("name"):
                continue
            out.append(Candidate(name=tags["name"], address=_address(tags),
                                 lat=la, lon=lo, category=_category(tags),
                                 source="osm"))
        return out

    # -- address of a point (Nominatim reverse) --------------------------
    def reverse(self, lat, lon):
        """Street address for a coordinate, or "" when none is known. Cached."""
        key = PlaceCache.key("nominatim-rev", round(lat, 5), round(lon, 5))
        data = self.cache.get(key)
        if data is None:
            _throttle("nominatim", self.min_interval)
            r = self.session.get(NOMINATIM_REVERSE_URL,
                                 params={"lat": lat, "lon": lon, "format": "jsonv2",
                                         "addressdetails": 1, "zoom": 18},
                                 headers=self.headers, timeout=15)
            r.raise_for_status()
            data = r.json()
            self.cache.set(key, data)
        a = data.get("address") or {}
        street = " ".join(x for x in (a.get("house_number"), a.get("road")) if x)
        city = a.get("city") or a.get("town") or a.get("village") or a.get("hamlet") or ""
        return ", ".join(p for p in (street, city, a.get("state"), a.get("postcode")) if p)

    # -- text (Nominatim) ----------------------------------------------
    def search_text(self, query, lat, lon):
        key = PlaceCache.key("nominatim", query.strip().lower(),
                             round(lat, 2), round(lon, 2))
        data = self.cache.get(key)
        if data is None:
            d = 0.5  # bias (not bound) results to ~50 km around the stop
            params = {"q": query, "format": "jsonv2", "addressdetails": 1,
                      "limit": 8,
                      "viewbox": f"{lon - d},{lat + d},{lon + d},{lat - d}"}
            _throttle("nominatim", self.min_interval)
            r = self.session.get(NOMINATIM_URL, params=params,
                                 headers=self.headers, timeout=30)
            r.raise_for_status()
            data = r.json()
            self.cache.set(key, data)
        out = []
        for it in data:
            a = it.get("address", {})
            street = " ".join(x for x in (a.get("house_number"), a.get("road")) if x)
            city = a.get("city") or a.get("town") or a.get("village") or ""
            address = ", ".join(p for p in (street, city, a.get("state"),
                                            a.get("postcode")) if p)
            name = it.get("name") or it.get("display_name", "").split(",")[0]
            out.append(Candidate(
                name=name, address=address or it.get("display_name", ""),
                lat=float(it["lat"]), lon=float(it["lon"]),
                category=f'{it.get("category", "")}={it.get("type", "")}',
                source="osm"))
        return out
