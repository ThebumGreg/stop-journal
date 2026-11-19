from .base import Candidate, Provider
from .cache import PlaceCache
from .osm import OsmProvider


def get_provider(cfg):
    """Provider chosen by PLACES_PROVIDER. Google is deliberately not wired up:
    it costs money, so it needs the owner's explicit go-ahead first."""
    if cfg.places_provider == "osm":
        return OsmProvider(PlaceCache(cfg.data_dir), overpass_url=cfg.overpass_url)
    raise ValueError(f"Unsupported PLACES_PROVIDER: {cfg.places_provider!r}")


__all__ = ["Candidate", "Provider", "PlaceCache", "OsmProvider", "get_provider"]
