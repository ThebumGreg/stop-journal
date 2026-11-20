"""Lunch and Traffic tags, the start-on-arrival tolerance, and report wording."""
from dataclasses import replace

from app import create_app, sample
from tests.test_detect import CFG

D = "2026-11-18"
A, B = sample.SITE_A, sample.SITE_B


def make(tmp_path):
    d = sample.Day(D)
    t = d.dwell(*A, d.at(8), 60)            # 08:00-09:00 job
    t = d.drive(A, B, t, 20)                # 09:00-09:20
    t = d.dwell(*B, t, 40)                  # 09:20-10:00 lunch / traffic
    t = d.drive(B, A, t, 20)                # 10:00-10:20
    d.dwell(*A, t, 60)                      # 10:20-11:20 job
    c = create_app(replace(CFG, data_dir=str(tmp_path)),
                   client=sample.ListRecorder(d.points())).test_client()
    day = c.get(f"/api/day/{D}").json
    for s, name in zip(day["stops"], ("Acme", "Mid", "Acme")):
        c.patch(f"/api/stops/{s['id']}", json={"business_name": name})
    return c, [s["id"] for s in day["stops"]]


def day(c):
    return c.get(f"/api/day/{D}").json


def test_lunch_excluded_with_its_drives(tmp_path):
    c, ids = make(tmp_path)
    c.patch(f"/api/stops/{ids[0]}", json={"status": "Confirmed"})
    c.patch(f"/api/stops/{ids[2]}", json={"status": "Confirmed"})
    assert c.patch(f"/api/stops/{ids[1]}", json={"status": "Lunch"}).status_code == 200
    d = day(c)
    assert d["totals"]["drive_s"] == 0 and d["totals"]["lunch"] == 1
    assert not any(x["counts"] for x in d["drives"])
    on_site = sum(s["depart_ts"] - s["arrive_ts"] for s in d["stops"] if s["status"] == "Confirmed")
    assert d["totals"]["on_site_s"] == on_site
    r = c.get(f"/api/export.json?from={D}&to={D}").json["days"][0]
    assert any(e["reason"] == "Lunch" for e in r["excluded"])


def test_traffic_time_counts_as_drive_time(tmp_path):
    c, ids = make(tmp_path)
    for i in (0, 2):
        c.patch(f"/api/stops/{ids[i]}", json={"status": "Confirmed"})
    before = day(c)["totals"]
    c.patch(f"/api/stops/{ids[1]}", json={"status": "Traffic"})
    d = day(c)
    mid = d["stops"][1]
    t = d["totals"]
    assert t["traffic_s"] == mid["depart_ts"] - mid["arrive_ts"]
    assert all(x["counts"] for x in d["drives"])
    assert t["drive_s"] >= t["traffic_s"] + 2 * 15 * 60
    assert t["on_site_s"] == before["on_site_s"] - mid["minutes"] * 60
    r = c.get(f"/api/export.json?from={D}&to={D}").json["days"][0]
    assert r["summary"]["traffic_minutes"] == mid["minutes"]
    assert r["summary"]["drive_minutes"] >= mid["minutes"]
    assert any(e["reason"].startswith("Traffic") for e in r["excluded"])
    assert c.get(f"/api/export/check?from={D}&to={D}").json["not_confirmed"] == 0


def test_draft_still_counts_as_not_confirmed(tmp_path):
    c, ids = make(tmp_path)
    assert c.get(f"/api/export/check?from={D}&to={D}").json["not_confirmed"] == 3


def test_bad_status_rejected(tmp_path):
    c, ids = make(tmp_path)
    assert c.patch(f"/api/stops/{ids[0]}", json={"status": "Nap"}).status_code == 400


def test_start_within_slack_of_arrival_is_not_a_commute(tmp_path):
    c, ids = make(tmp_path)
    c.patch(f"/api/stops/{ids[0]}", json={"status": "Personal"})
    c.patch(f"/api/stops/{ids[1]}", json={"status": "Confirmed"})
    arrive = day(c)["drives"][0]["arrive_ts"]
    from app.service import day_marks  # noqa: F401  (minute precision: tap sits just before arrival)
    hh = arrive // 60 * 60
    # start tapped in the same minute as arrival (seconds dropped) must not count the commute
    from datetime import datetime
    from zoneinfo import ZoneInfo
    local = datetime.fromtimestamp(hh, ZoneInfo(CFG.app_tz)).strftime("%H:%M")
    c.post(f"/api/day/{D}/mark", json={"which": "start", "time": local})
    assert day(c)["drives"][0]["counts"] is False
    c.post(f"/api/day/{D}/mark", json={"which": "start", "clear": True})
    c.post(f"/api/day/{D}/mark", json={"which": "start", "time": "08:30"})
    assert day(c)["drives"][0]["counts"] is True
