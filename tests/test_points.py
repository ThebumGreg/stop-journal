import json
from datetime import datetime, timezone

from app import db, points
from app.config import Config
from app.recorder import FakeRecorder, local_day_bounds


def make_cfg(tmp_path):
    return Config(
        recorder_url="http://x", recorder_user="u", recorder_device="d",
        recorder_auth="", app_tz="America/Los_Angeles", places_provider="osm",
        google_places_key="", stop_min_minutes=10, stop_radius_m=100,
        stop_max_gap_min=15, max_acc_m=100, app_password="",
        data_dir=str(tmp_path))


def test_day_bounds_local_midnight():
    s, e = local_day_bounds("2026-11-18", "America/Los_Angeles")
    assert s == datetime(2026, 11, 18, 8, 0, tzinfo=timezone.utc)  # PST = UTC-8
    assert e.hour == 7 and e.day == 19


def test_cache_and_accuracy_filter(tmp_path):
    cfg = make_cfg(tmp_path)
    s, _ = local_day_bounds("2026-11-18", cfg.app_tz)
    base = int(s.timestamp()) + 3600
    fx = tmp_path / "pts.json"
    fx.write_text(json.dumps([
        {"tst": base, "lat": 33.0, "lon": -117.0, "acc": 5},
        {"tst": base + 60, "lat": 33.0, "lon": -117.0, "acc": 500},
    ]))
    client = FakeRecorder(fx)
    conn = db.connect(cfg.data_dir)
    pts = points.load_day(conn, client, cfg, "2026-11-18")
    assert len(pts) == 2
    # second load must come from cache, not the client
    client.points = []
    assert len(points.load_day(conn, client, cfg, "2026-11-18")) == 2
    assert len(points.filter_accuracy(pts, cfg.max_acc_m)) == 1
