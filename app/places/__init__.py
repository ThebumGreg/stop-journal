from .base import Candidate, PlacesError, Provider
from .cache import PlaceCache, UsageCounter
from .google import CACHE_SECONDS, GoogleProvider
from .osm import OsmProvider


def get_provider(cfg):
    """Provider chosen by PLACES_PROVIDER / the settings page."""
    if cfg.places_provider == "osm":
        return OsmProvider(PlaceCache(cfg.data_dir), overpass_url=cfg.overpass_url)
    if cfg.places_provider == "google":
        if not cfg.google_places_key:
            raise PlacesError("Google is selected but no API key is set. Add one in Settings.")
        return GoogleProvider(cfg.google_places_key,
                              PlaceCache(cfg.data_dir, max_age_s=CACHE_SECONDS),
                              UsageCounter(cfg.data_dir, "google"), cfg.google_daily_cap)
    raise PlacesError(f"Unknown places provider: {cfg.places_provider!r}")


__all__ = ["Candidate", "PlacesError", "Provider", "PlaceCache", "UsageCounter",
           "OsmProvider", "GoogleProvider", "get_provider"]
