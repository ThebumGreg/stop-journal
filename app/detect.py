"""Stop and drive detection.

Pipeline (all thresholds come from Config and can be overridden per run):
 1. Greedy clustering (isolated outlier fixes are skipped): consecutive points stay in one cluster while each is
    within stop_radius_m of the cluster's running centroid and no more than
    stop_max_gap_min after the previous point.
 2. A cluster lasting >= stop_min_minutes is a 'stop'. One lasting
    SHORT_STOP_MIN_S..stop_min_minutes is a 'short' stop (shown, dismissible,
    never counted in on-site time). Anything shorter is noise.
 3. Merge adjacent items (any kind; result kind re-evaluated) whose centroids are within
    stop_radius_m * MERGE_RADIUS_FACTOR and whose gap is <= stop_merge_gap_min
    (GPS drift splits, phone-off gaps at one site).
 4. Everything between two consecutive items is a drive. Distance is the sum of
    haversine hops over the points in between (approximate).
"""
from .geo import haversine_m

SHORT_STOP_MIN_S = 120
MERGE_RADIUS_FACTOR = 1.5
OUTLIER_LOOKAHEAD = 2  # points to peek ahead before ending a cluster


def _cluster(points, radius_m, max_gap_s):
    clusters, i, n = [], 0, len(points)
    while i < n:
        cl = [points[i]]
        clat, clon = points[i]["lat"], points[i]["lon"]
        j = i + 1
        while j < n:
            p = points[j]
            if p["tst"] - cl[-1]["tst"] > max_gap_s:
                break
            if haversine_m(clat, clon, p["lat"], p["lon"]) > radius_m:
                # Isolated bad fix? Skip it if a following point is back inside.
                if any(q["tst"] - cl[-1]["tst"] <= max_gap_s
                       and haversine_m(clat, clon, q["lat"], q["lon"]) <= radius_m
                       for q in points[j + 1:j + 1 + OUTLIER_LOOKAHEAD]):
                    j += 1
                    continue
                break
            cl.append(p)
            k = len(cl)
            clat += (p["lat"] - clat) / k
            clon += (p["lon"] - clon) / k
            j += 1
        clusters.append((i, j - 1, clat, clon))
        i = j
    return clusters


def _make(points, i0, i1, lat, lon, kind):
    return {"kind": kind, "lat": lat, "lon": lon,
            "arrive": points[i0]["tst"], "depart": points[i1]["tst"],
            "i0": i0, "i1": i1}


def detect(points, cfg):
    """points: time-sorted, accuracy-filtered dicts. Returns (items, drives)."""
    min_s = cfg.stop_min_minutes * 60
    items = []
    for i0, i1, lat, lon in _cluster(points, cfg.stop_radius_m,
                                     cfg.stop_max_gap_min * 60):
        dur = points[i1]["tst"] - points[i0]["tst"]
        if dur >= min_s:
            items.append(_make(points, i0, i1, lat, lon, "stop"))
        elif dur >= SHORT_STOP_MIN_S:
            items.append(_make(points, i0, i1, lat, lon, "short"))

    merged = []
    for it in items:
        prev = merged[-1] if merged else None
        if (prev
                and it["arrive"] - prev["depart"] <= cfg.stop_merge_gap_min * 60
                and haversine_m(prev["lat"], prev["lon"], it["lat"], it["lon"])
                <= cfg.stop_radius_m * MERGE_RADIUS_FACTOR):
            w1 = max(prev["depart"] - prev["arrive"], 1)
            w2 = max(it["depart"] - it["arrive"], 1)
            prev["lat"] = (prev["lat"] * w1 + it["lat"] * w2) / (w1 + w2)
            prev["lon"] = (prev["lon"] * w1 + it["lon"] * w2) / (w1 + w2)
            prev["depart"], prev["i1"] = it["depart"], it["i1"]
            prev["kind"] = "stop" if prev["depart"] - prev["arrive"] >= min_s else "short"
        else:
            merged.append(dict(it))
    items = merged
    for idx, it in enumerate(items):
        it["index"] = idx

    drives = []
    for a, b in zip(items, items[1:]):
        seg = points[a["i1"]:b["i0"] + 1]
        dist = sum(haversine_m(p["lat"], p["lon"], q["lat"], q["lon"])
                   for p, q in zip(seg, seg[1:]))
        drives.append({"from": a["index"], "to": b["index"],
                       "depart": a["depart"], "arrive": b["arrive"],
                       "distance_m": dist})
    return items, drives


def totals(items, drives):
    on_site = sum(i["depart"] - i["arrive"] for i in items if i["kind"] == "stop")
    drive_s = sum(d["arrive"] - d["depart"] for d in drives)
    miles = sum(d["distance_m"] for d in drives) / 1609.344
    return {"on_site_s": on_site, "drive_s": drive_s,
            "miles_approx": round(miles, 1),
            "stops": sum(1 for i in items if i["kind"] == "stop")}
