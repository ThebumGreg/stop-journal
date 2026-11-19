import hashlib
import json
import time
from datetime import datetime, timezone

from .. import db
from .base import PlacesError


class PlaceCache:
    """SQLite-backed cache for provider responses (policy: cache every result)."""

    def __init__(self, data_dir, max_age_s=30 * 86400):
        self.data_dir = data_dir
        self.max_age_s = max_age_s

    @staticmethod
    def key(*parts):
        return hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()

    def get(self, key):
        conn = db.connect(self.data_dir)
        try:
            row = conn.execute(
                "SELECT response_json, fetched_at FROM place_cache WHERE key=?",
                (key,)).fetchone()
        finally:
            conn.close()
        if row and time.time() - row["fetched_at"] <= self.max_age_s:
            return json.loads(row["response_json"])
        return None

    def set(self, key, obj):
        conn = db.connect(self.data_dir)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO place_cache(key,response_json,fetched_at)"
                " VALUES (?,?,?)", (key, json.dumps(obj), int(time.time())))
            conn.commit()
        finally:
            conn.close()


class UsageCounter:
    """Per-provider, per-UTC-day request counter used as a spending safeguard."""

    def __init__(self, data_dir, provider):
        self.data_dir, self.provider = data_dir, provider

    @staticmethod
    def today():
        return datetime.now(timezone.utc).date().isoformat()

    def count(self):
        conn = db.connect(self.data_dir)
        try:
            row = conn.execute("SELECT count FROM api_usage WHERE day=? AND provider=?",
                               (self.today(), self.provider)).fetchone()
        finally:
            conn.close()
        return row["count"] if row else 0

    def hit(self, cap):
        """Count one outgoing request, or raise PlacesError if the cap is reached."""
        conn = db.connect(self.data_dir)
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT count FROM api_usage WHERE day=? AND provider=?",
                               (self.today(), self.provider)).fetchone()
            n = row["count"] if row else 0
            if n >= cap:
                conn.rollback()
                raise PlacesError(
                    f"Daily {self.provider} request cap reached ({cap}). "
                    "Raise it in Settings if that is intended.")
            conn.execute(
                "INSERT INTO api_usage(day,provider,count) VALUES (?,?,1) "
                "ON CONFLICT(day,provider) DO UPDATE SET count=count+1",
                (self.today(), self.provider))
            conn.commit()
        finally:
            conn.close()
