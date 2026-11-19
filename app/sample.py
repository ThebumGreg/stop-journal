"""Synthetic GPS days for tests and for running the app without a Recorder.
Coordinates are made up (downtown Los Angeles area), not real user locations."""
import math
import random
from datetime import datetime
from zoneinfo import ZoneInfo

LAT0, LON0 = 34.05, -118.25
M_LAT = 111320.0


def m_lon(lat):
    return 111320.0 * math.cos(math.radians(lat))


def offset(lat, lon, north_m, east_m):
    return lat + north_m / M_LAT, lon + east_m / m_lon(lat)


def midnight_utc(date_str, tz="America/Los_Angeles"):
    d = datetime.fromisoformat(date_str).replace(tzinfo=ZoneInfo(tz))
    return int(d.timestamp())


class Day:
    def __init__(self, date_str="2026-11-18", seed=1):
        self.t0 = midnight_utc(date_str)
        self.pts = []
        self.rng = random.Random(seed)

    def at(self, hh, mm=0):
        return self.t0 + hh * 3600 + mm * 60

    def dwell(self, lat, lon, start, minutes, step=60, jitter_m=8, acc=10):
        t = start
        while t <= start + minutes * 60:
            n = self.rng.gauss(0, jitter_m)
            e = self.rng.gauss(0, jitter_m)
            la, lo = offset(lat, lon, n, e)
            self.add(t, la, lo, acc)
            t += step
        return start + minutes * 60

    def drive(self, a, b, start, minutes, step=30):
        steps = max(int(minutes * 60 / step), 1)
        for k in range(1, steps):
            f = k / steps
            self.add(start + k * step, a[0] + (b[0] - a[0]) * f,
                     a[1] + (b[1] - a[1]) * f, 15)
        return start + minutes * 60

    def add(self, t, lat, lon, acc=10):
        self.pts.append({"tst": int(t), "lat": lat, "lon": lon, "acc": acc,
                         "vel": 0, "batt": 80})

    def points(self):
        return sorted(self.pts, key=lambda p: p["tst"])


SITE_A = (LAT0, LON0)
SITE_B = offset(LAT0, LON0, 6000, 4000)
SITE_C = offset(LAT0, LON0, -3000, 9000)
GAS = offset(LAT0, LON0, 3000, 2000)


def normal_day(date_str="2026-11-18"):
    d = Day(date_str)
    d.dwell(*SITE_A, d.at(9), 60)
    d.drive(SITE_A, SITE_B, d.at(10), 20)
    d.dwell(*SITE_B, d.at(10, 20), 60)
    d.drive(SITE_B, SITE_C, d.at(11, 20), 40)
    d.dwell(*SITE_C, d.at(12), 45)
    return d.points()


def drift_day(date_str="2026-11-18"):
    d = Day(date_str)
    d.dwell(*SITE_A, d.at(9), 60, jitter_m=40)
    far = offset(*SITE_A, 180, 0)  # one bad fix well outside the radius
    d.add(d.at(9, 30) + 5, far[0], far[1], 30)
    d.drive(SITE_A, SITE_B, d.at(10), 20)
    d.dwell(*SITE_B, d.at(10, 20), 30)
    return d.points()


def gap_day(date_str="2026-11-18"):
    d = Day(date_str)
    d.dwell(*SITE_A, d.at(9), 30)
    d.dwell(*SITE_A, d.at(10, 15), 30)  # phone off 9:30-10:15
    d.drive(SITE_A, SITE_B, d.at(10, 45), 20)
    d.dwell(*SITE_B, d.at(11, 5), 30)
    return d.points()


def short_stop_day(date_str="2026-11-18"):
    d = Day(date_str)
    d.dwell(*SITE_A, d.at(9), 45)
    d.drive(SITE_A, GAS, d.at(9, 45), 15)
    d.dwell(*GAS, d.at(10), 6)  # gas station: under the 10 min threshold
    d.drive(GAS, SITE_B, d.at(10, 6), 15)
    d.dwell(*SITE_B, d.at(10, 21), 40)
    return d.points()


class ListRecorder:
    """Recorder stand-in serving a fixed point list."""

    def __init__(self, points):
        self.points = points

    def fetch(self, start_utc, end_utc):
        s, e = start_utc.timestamp(), end_utc.timestamp()
        return [p for p in self.points if s <= p["tst"] <= e]

    def last_tst(self):
        return max((p["tst"] for p in self.points), default=None)
