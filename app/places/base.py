from dataclasses import dataclass
from typing import Optional


class PlacesError(Exception):
    """Provider failure whose message is safe to show the user (never has secrets)."""


@dataclass
class Candidate:
    name: str
    address: str = ""
    lat: float = 0.0
    lon: float = 0.0
    category: str = ""            # e.g. "shop=hardware"
    source: str = "osm"           # known | osm | google
    known_site_id: Optional[int] = None
    kind: str = ""                # known sites only: client | home | office | vendor
    times_confirmed: int = 0
    distance_m: float = 0.0       # filled in by ranking
    score: float = 0.0            # filled in by ranking


class Provider:
    """Places provider interface. Implementations must cache and rate limit."""

    def search_nearby(self, lat, lon, radius_m) -> "list[Candidate]":
        raise NotImplementedError

    def search_text(self, query, lat, lon) -> "list[Candidate]":
        raise NotImplementedError
