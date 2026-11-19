"""Raw point cache: fetch a local day once, store it, reuse it."""
import json
from datetime import datetime, timezone

from .recorder import local_day_bounds


def load_day(conn, client, cfg, date_str, refresh=False):
    """Return the day's points (sorted, unfiltered) as dicts, using the cache."""
    cached = conn.execute(
        "SELECT fetched_at FROM days WHERE date=? AND fetched_at IS NOT NULL",
        (date_str,),
    ).fetchone()
    start, end = local_day_bounds(date_str, cfg.app_tz)
    if refresh or not cached:
        raw = client.fetch(start, end)
        rows = [
            (cfg.recorder_device, int(p["tst"]), p["lat"], p["lon"],
             p.get("acc"), json.dumps(p))
            for p in raw
            if "lat" in p and "lon" in p and "tst" in p
        ]
        conn.executemany(
            "INSERT OR REPLACE INTO points(device,tst_utc,lat,lon,acc,raw_json)"
            " VALUES (?,?,?,?,?,?)", rows)
        conn.execute(
            "INSERT INTO days(date, fetched_at) VALUES(?,?) "
            "ON CONFLICT(date) DO UPDATE SET fetched_at=excluded.fetched_at",
            (date_str, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    cur = conn.execute(
        "SELECT tst_utc AS tst, lat, lon, acc FROM points "
        "WHERE device=? AND tst_utc BETWEEN ? AND ? ORDER BY tst_utc",
        (cfg.recorder_device, int(start.timestamp()), int(end.timestamp())),
    )
    return [dict(r) for r in cur]


def filter_accuracy(points, max_acc_m):
    """Drop poor-accuracy points (points with unknown accuracy are kept)."""
    return [p for p in points if p.get("acc") is None or p["acc"] <= max_acc_m]
