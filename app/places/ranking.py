"""Candidate ranking. Higher score = earlier in the pick-list.

Score = tier + distance + business-likeness + dwell fit, all tunable via WEIGHTS:
  1. known   : sites from the user's own known_sites table always outrank
               provider results (W_KNOWN is far larger than everything else),
               more-confirmed sites slightly higher.
  2. distance: linear falloff from W_DIST at the stop centroid to 0 at
               search_radius_m (closer is better).
  3. business: named business categories (shop, office, craft, ...) get
               W_BUSINESS; having an address adds W_ADDR; bare buildings and
               addresses get neither.
  4. dwell   : up to W_DWELL for how close the stop's duration is to the typical
               visit length for the candidate's category (TYPICAL_MIN).
"""
from ..geo import haversine_m

WEIGHTS = {"W_KNOWN": 1000.0, "W_CONFIRMED": 2.0, "W_DIST": 50.0,
           "W_BUSINESS": 20.0, "W_ADDR": 5.0, "W_DWELL": 10.0}
BUSINESS_KEYS = {"shop", "office", "craft", "industrial", "healthcare", "tourism"}
NON_BUSINESS_AMENITY = {"bench", "parking", "parking_space", "waste_basket",
                        "toilets", "drinking_water", "bicycle_parking"}
TYPICAL_MIN = {"shop": 20, "office": 60, "craft": 90, "industrial": 90,
               "healthcare": 45, "amenity": 30, "tourism": 45, "leisure": 60}
DEFAULT_TYPICAL_MIN = 30
DEDUPE_RADIUS_M = 30


def _key_value(cat):
    k, _, v = (cat or "").partition("=")
    return k, v


def _is_business(cat):
    k, v = _key_value(cat)
    if k in BUSINESS_KEYS:
        return True
    return k == "amenity" and v not in NON_BUSINESS_AMENITY


def score(c, lat, lon, minutes, search_radius_m, w=WEIGHTS):
    c.distance_m = haversine_m(lat, lon, c.lat, c.lon)
    s = 0.0
    if c.source == "known":
        s += w["W_KNOWN"] + min(c.times_confirmed, 10) * w["W_CONFIRMED"]
    s += w["W_DIST"] * max(0.0, 1 - c.distance_m / search_radius_m)
    if _is_business(c.category):
        s += w["W_BUSINESS"]
    if c.address:
        s += w["W_ADDR"]
    if minutes:
        typical = TYPICAL_MIN.get(_key_value(c.category)[0], DEFAULT_TYPICAL_MIN)
        s += w["W_DWELL"] * (1 - min(abs(minutes - typical) / typical, 1))
    c.score = round(s, 2)
    return c.score


def rank(candidates, lat, lon, minutes=0, search_radius_m=150):
    """Dedupe (a provider hit that duplicates a known site is dropped), score, sort."""
    known = [c for c in candidates if c.source == "known"]
    kept = list(known)
    for c in candidates:
        if c.source == "known":
            continue
        if any(k.name.lower() == c.name.lower()
               and haversine_m(k.lat, k.lon, c.lat, c.lon) <= DEDUPE_RADIUS_M
               for k in known):
            continue
        kept.append(c)
    for c in kept:
        score(c, lat, lon, minutes, search_radius_m)
    return sorted(kept, key=lambda c: -c.score)
