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
    def __init__(self, cfg, timeout=30, resolve=None):
        self._cfg = cfg
        self._resolve = resolve  # callable returning the current effective config
        self.timeout = timeout

    @property
    def cfg(self):
        return self._resolve() if self._resolve else self._cfg

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


def _last_tst(self):
    """Epoch seconds of the newest point the Recorder has for this device, or None."""
    auth = None
    if self.cfg.recorder_auth:
        u, _, p = self.cfg.recorder_auth.partition(":")
        auth = (u, p)
    r = requests.get(f"{self.cfg.recorder_url}/api/0/last",
                     params={"user": self.cfg.recorder_user, "device": self.cfg.recorder_device},
                     auth=auth, timeout=15)
    r.raise_for_status()
    ts = [p["tst"] for p in r.json() if "tst" in p
          and p.get("device", self.cfg.recorder_device) == self.cfg.recorder_device]
    return max(ts) if ts else None


def _last_point(self):
    """Newest point the Recorder has for this device as {lat, lon, tst}, or None."""
    auth = None
    if self.cfg.recorder_auth:
        u, _, p = self.cfg.recorder_auth.partition(":")
        auth = (u, p)
    r = requests.get(f"{self.cfg.recorder_url}/api/0/last",
                     params={"user": self.cfg.recorder_user, "device": self.cfg.recorder_device},
                     auth=auth, timeout=15)
    r.raise_for_status()
    pts = [p for p in r.json() if "tst" in p and "lat" in p and "lon" in p
           and p.get("device", self.cfg.recorder_device) == self.cfg.recorder_device]
    if not pts:
        return None
    p = max(pts, key=lambda x: x["tst"])
    return {"lat": p["lat"], "lon": p["lon"], "tst": p["tst"]}


def _get(url, auth_str, path, params=None, timeout=10):
    auth = tuple(auth_str.partition(":")[::2]) if auth_str else None
    r = requests.get(f"{url.rstrip('/')}{path}", params=params, auth=auth, timeout=timeout)
    r.raise_for_status()
    return r.json()


def discover(url, auth_str=""):
    """Everything the Recorder knows: [{user, devices:[{device, last_tst}]}].

    /api/0/list -> {"results": [users]}; /api/0/list?user=U -> {"results": [devices]};
    /api/0/last?user=U&device=D -> [point] (used only for the "last seen" time).
    """
    out = []
    for user in _get(url, auth_str, "/api/0/list").get("results", []):
        devices = []
        for dev in _get(url, auth_str, "/api/0/list", {"user": user}).get("results", []):
            try:
                pts = _get(url, auth_str, "/api/0/last", {"user": user, "device": dev})
                last = max((p["tst"] for p in pts if "tst" in p), default=None)
            except (requests.RequestException, ValueError):
                last = None
            devices.append({"device": dev, "last_tst": last})
        out.append({"user": user, "devices": devices})
    return out


RecorderClient.last_tst = _last_tst
RecorderClient.last_point = _last_point


class FakeRecorder:
    """Serves points from a JSON fixture (same shape as the 'data' array)."""

    def __init__(self, path):
        with open(path) as f:
            self.points = json.load(f)

    def fetch(self, start_utc, end_utc):
        s, e = start_utc.timestamp(), end_utc.timestamp()
        return [p for p in self.points if s <= p["tst"] <= e]

    def last_tst(self):
        return max((p["tst"] for p in self.points), default=None)

    def last_point(self):
        p = max(self.points, key=lambda x: x["tst"], default=None)
        return p and {"lat": p["lat"], "lon": p["lon"], "tst": p["tst"]}


def local_day_bounds(date_str, tz_name):
    """Local midnight-to-midnight for YYYY-MM-DD, as aware UTC datetimes."""
    tz = ZoneInfo(tz_name)
    start = datetime.fromisoformat(date_str).replace(tzinfo=tz)
    end = datetime.fromisoformat(date_str).replace(
        hour=23, minute=59, second=59, tzinfo=tz
    )
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)
