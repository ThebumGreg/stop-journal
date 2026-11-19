"""Stops that are still going keep growing on Sync, even after you confirm them."""
from dataclasses import replace

from app import create_app, detect, sample
from app.points import filter_accuracy
from tests.test_detect import CFG

D = "2026-11-18"
A, B = sample.SITE_A, sample.SITE_B


def setup(tmp_path):
    d = sample.Day(D)
    d.dwell(*A, d.at(9), 30)
    rec = sample.ListRecorder(d.points())
    c = create_app(replace(CFG, data_dir=str(tmp_path)), client=rec).test_client()
    return c, rec, d


def stops(c):
    return c.get(f"/api/day/{D}").json["stops"]


def more(rec, d, minutes_from, minutes_to, site=A):
    t0 = d.at(9) + minutes_from * 60
    d.dwell(*site, t0, minutes_to - minutes_from)
    rec.points = d.points()


def test_confirmed_stop_grows_when_you_are_still_there(tmp_path):
    c, rec, d = setup(tmp_path)
    s = stops(c)[0]
    c.patch(f"/api/stops/{s['id']}", json={"business_name": "Acme", "status": "Confirmed",
                                          "notes": "working"})
    before = stops(c)[0]["depart_ts"]
    more(rec, d, 31, 90)
    r = c.post(f"/api/day/{D}/sync").json
    assert r["added"] == 0 and len(r["stops"]) == 1
    after = r["stops"][0]
    assert after["depart_ts"] > before + 50 * 60
    assert after["business_name"] == "Acme" and after["status"] == "Confirmed" and after["notes"] == "working"


def test_times_you_set_yourself_are_not_overridden(tmp_path):
    c, rec, d = setup(tmp_path)
    s = stops(c)[0]
    c.patch(f"/api/stops/{s['id']}", json={"business_name": "Acme", "status": "Confirmed",
                                          "depart": "09:40"})
    fixed = stops(c)[0]["depart_ts"]
    more(rec, d, 31, 90)
    c.post(f"/api/day/{D}/sync")
    assert stops(c)[0]["depart_ts"] == fixed


def test_growth_does_not_swallow_a_later_stop(tmp_path):
    c, rec, d = setup(tmp_path)
    s = stops(c)[0]
    c.patch(f"/api/stops/{s['id']}", json={"business_name": "Acme", "status": "Confirmed"})
    c.post(f"/api/day/{D}/stops", json={"arrive": "09:50", "depart": "10:20", "business_name": "Manual"})
    more(rec, d, 31, 120)
    c.post(f"/api/day/{D}/sync")
    st = stops(c)
    assert [x["business_name"] for x in st][:2] == ["Acme", "Manual"]
    assert st[0]["depart_ts"] <= st[1]["arrive_ts"]


def test_lone_point_before_a_quiet_spell_joins_the_stop():
    d = sample.Day(D)
    d.add(d.at(7, 7), *A, 6)                    # one report, then nothing for 31 minutes
    d.dwell(*A, d.at(7, 38), 60, step=180)
    items, _ = detect.detect(filter_accuracy(d.points(), 100), CFG)
    assert len(items) == 1 and items[0]["arrive"] == d.at(7, 7) and items[0]["kind"] == "stop"


def test_traffic_light_pause_is_ignored_but_a_real_short_stop_stays():
    d = sample.Day(D)
    t = d.dwell(*A, d.at(9), 30)
    t = d.drive(A, B, t, 10)
    t = d.dwell(*B, t, 2, step=30)              # ~2 minute pause
    items, _ = detect.detect(filter_accuracy(d.points(), 100), CFG)
    assert [i["kind"] for i in items] == ["stop"]
    d2 = sample.Day(D)
    t = d2.dwell(*A, d2.at(9), 30)
    t = d2.drive(A, B, t, 10)
    d2.dwell(*B, t, 6, step=30)                 # ~6 minute stop
    items, _ = detect.detect(filter_accuracy(d2.points(), 100), CFG)
    assert [i["kind"] for i in items] == ["stop", "short"]
