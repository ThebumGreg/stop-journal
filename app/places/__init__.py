from .base import Candidate, PlacesError, Provider
from .cache import PlaceCache, UsageCounter
from .google import CACHE_SECONDS, GoogleProvider
from .osm import OsmProvider


def get_provider(cfg, name=None):
    """Provider by name; default is the configured one (osm_google starts with osm)."""
    name = name or cfg.places_provider
    if name == "osm_google":
        name = "osm"
    if name == "osm":
        return OsmProvider(PlaceCache(cfg.data_dir), overpass_url=cfg.overpass_url)
    if name == "google":
        if not cfg.google_places_key:
            raise PlacesError("Google is selected but no API key is set. Add one in Settings.")
        return GoogleProvider(cfg.google_places_key,
                              PlaceCache(cfg.data_dir, max_age_s=CACHE_SECONDS),
                              UsageCounter(cfg.data_dir, "google"), cfg.google_daily_cap)
    raise PlacesError(f"Unknown places provider: {name!r}")


__all__ = ["Candidate", "PlacesError", "Provider", "PlaceCache", "UsageCounter",
           "OsmProvider", "GoogleProvider", "get_provider"]
