"""OwnTracks Recorder client.

Response shape (verified against live Recorder):
  GET /api/0/locations?user=U&device=D&from=ISO&to=ISO&format=json
  -> {"count": N, "data": [ {lat, lon, tst, acc, vel, batt, ...}, ... ],
      "status": 200, "version": "..."}
"""
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests


class RecorderClient:
    def __init__(self, cfg, timeout=30):
        self.cfg = cfg
        self.timeout = timeout

    def fetch(self, start_utc: datetime, end_utc: datetime):
        """Return raw point dicts between two aware UTC datetimes."""
        auth = None
        if self.cfg.recorder_auth:
            u, _, p = self.cfg.recorder_auth.partition(":")
            auth = (u, p)
        fmt = "%Y-%m-%dT%H:%M:%S"
        r = requests.get(
            f"{self.cfg.recorder_url}/api/0/locations",
            params={
                "user": self.cfg.recorder_user,
                "device": self.cfg.recorder_device,
                "from": start_utc.strftime(fmt),
                "to": end_utc.strftime(fmt),
                "format": "json",
            },
            auth=auth,
            timeout=self.timeout,
        )
        r.raise_for_status()
        return r.json().get("data", [])


class FakeRecorder:
    """Serves points from a JSON fixture (same shape as the 'data' array)."""

    def __init__(self, path):
        with open(path) as f:
            self.points = json.load(f)

    def fetch(self, start_utc, end_utc):
        s, e = start_utc.timestamp(), end_utc.timestamp()
        return [p for p in self.points if s <= p["tst"] <= e]


def local_day_bounds(date_str, tz_name):
    """Local midnight-to-midnight for YYYY-MM-DD, as aware UTC datetimes."""
    tz = ZoneInfo(tz_name)
    start = datetime.fromisoformat(date_str).replace(tzinfo=tz)
    end = datetime.fromisoformat(date_str).replace(
        hour=23, minute=59, second=59, tzinfo=tz
    )
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)
