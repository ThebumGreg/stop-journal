"""Live status: where the phone last reported and how long it has been there.

Pure logic over recent points (newest last). "Here since" is the start of the unbroken
run of recent points that stay within the stop radius of the newest point. OwnTracks
may send nothing while the phone sits still, so the time at the place runs up to *now*,
not just to the last point; `last_update_s` says how fresh the data is.
"""
from .geo import haversine_m

RUN_RADIUS_FACTOR = 1.5   # a little looser than a stop: GPS wanders
MOVING_AFTER_S = 120      # less than this at one spot counts as "just arrived / moving"
LOOKBACK_S = 12 * 3600


def status(points, now, radius_m, max_acc_m=100):
    pts = sorted((p for p in points if p.get("acc", 0) <= max_acc_m and p["tst"] <= now),
                 key=lambda p: p["tst"])
    if not pts:
        return None
    last = pts[-1]
    arrive = last["tst"]
    for p in reversed(pts):
        if haversine_m(p["lat"], p["lon"], last["lat"], last["lon"]) > radius_m * RUN_RADIUS_FACTOR:
            break
        arrive = p["tst"]
    here_s = max(0, now - arrive)
    return {
        "lat": last["lat"], "lon": last["lon"],
        "last_tst": last["tst"], "last_update_s": max(0, now - last["tst"]),
        "arrive_tst": arrive, "here_s": here_s,
        "moving": here_s < MOVING_AFTER_S,
        "batt": last.get("batt"),
    }
