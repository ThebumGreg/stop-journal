import hashlib
import json
import time

from .. import db


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
