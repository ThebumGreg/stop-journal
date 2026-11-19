from dataclasses import replace

from app import sample
from app.config import Config
from app.detect import detect, totals
from app.points import filter_accuracy

CFG = Config(
    recorder_url="", recorder_user="", recorder_device="", recorder_auth="",
    app_tz="America/Los_Angeles", places_provider="osm", google_places_key="",
    stop_min_minutes=10, stop_radius_m=100, stop_max_gap_min=15,
    stop_merge_gap_min=60, max_acc_m=100, app_password="", data_dir="")


def run(points, cfg=CFG):
    return detect(filter_accuracy(points, cfg.max_acc_m), cfg)


def test_normal_day():
    items, drives = run(sample.normal_day())
    assert [i["kind"] for i in items] == ["stop"] * 3
    assert len(drives) == 2
    assert 18 * 60 <= drives[0]["arrive"] - drives[0]["depart"] <= 22 * 60
    assert 4.0 < drives[0]["distance_m"] / 1609.344 < 5.0  # ~7.2 km
    t = totals(items, drives)
    assert t["stops"] == 3 and t["on_site_s"] > 150 * 60


def test_gps_drift_stays_one_stop():
    items, _ = run(sample.drift_day())
    assert [i["kind"] for i in items] == ["stop", "stop"]


def test_gap_phone_off_merges():
    items, drives = run(sample.gap_day())
    assert [i["kind"] for i in items] == ["stop", "stop"]
    assert items[0]["depart"] - items[0]["arrive"] >= 70 * 60
    assert len(drives) == 1


def test_gap_too_long_does_not_merge():
    items, _ = run(sample.gap_day(), replace(CFG, stop_merge_gap_min=20))
    assert [i["kind"] for i in items] == ["stop", "stop", "stop"]


def test_short_stop_not_counted():
    items, drives = run(sample.short_stop_day())
    assert [i["kind"] for i in items] == ["stop", "short", "stop"]
    t = totals(items, drives)
    assert t["stops"] == 2
    on_site = sum(i["depart"] - i["arrive"] for i in items if i["kind"] == "stop")
    assert t["on_site_s"] == on_site


def test_poor_accuracy_ignored():
    pts = sample.normal_day()
    for p in pts[::3]:
        p["acc"] = 500
    items, _ = run(pts)
    assert [i["kind"] for i in items].count("stop") == 3
