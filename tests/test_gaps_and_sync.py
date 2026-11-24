"""Data gaps, still-on-site stops, End Day following, auto-sync, vendor marking."""
from dataclasses import replace
from unittest import mock

from app import create_app, sample
from tests.test_detect import CFG

D = "2026-11-18"
HOME, A, B = sample.SITE_A, sample.SITE_B, (sample.SITE_B[0] + 0.03, sample.SITE_B[1])


def day_with_gap(tmp_path):
    """Work at A 08:00-08:54, the phone is silent until 09:55, then at B (2-3 km away) for 60 min."""
    d = sample.Day(D)
    d.dwell(*A, d.at(8), 54)
    d.dwell(*B, d.at(9) + 55 * 60, 60)
    c = create_app(replace(CFG, data_dir=str(tmp_path)), client=sample.ListRecorder(d.points())).test_client()
    return c


def test_a_drive_across_a_data_gap_is_estimated_not_an_hour(tmp_path):
    c = day_with_gap(tmp_path)
    day = c.get(f"/api/day/{D}").json
    assert len(day["stops"]) == 2
    dr = day["drives"][0]
    assert dr["gap"] and dr["gap"]["minutes"] >= 55 and dr["estimated"] is True
    assert 5 <= dr["minutes"] <= 15                                    # distance / ~25 mph, not the 60 silent minutes
    assert dr["arrive"] == day["stops"][1]["arrive"]


def test_typed_times_are_trusted_over_the_estimate(tmp_path):
    c = day_with_gap(tmp_path)
    st = c.get(f"/api/day/{D}").json["stops"]
    c.patch(f"/api/stops/{st[0]['id']}", json={"depart": "09:10"})
    c.patch(f"/api/stops/{st[1]['id']}", json={"arrive": "09:40"})
    dr = c.get(f"/api/day/{D}").json["drives"][0]
    assert dr["estimated"] is False and dr["minutes"] == 30


def test_editing_arrival_keeps_departure_growing(tmp_path):
    from app import db
    c = day_with_gap(tmp_path)
    st = c.get(f"/api/day/{D}").json["stops"]
    r = c.patch(f"/api/stops/{st[1]['id']}", json={"arrive": "10:00"}).json["stops"][1]
    assert r["arrive"] == "10:00"
    conn = db.connect(str(tmp_path))
    row = conn.execute("SELECT arrive_set, depart_set FROM stops WHERE id=?", (st[1]["id"],)).fetchone()
    assert (row["arrive_set"], row["depart_set"]) == (1, 0)


def test_end_day_follows_the_last_stops_departure(tmp_path):
    c = day_with_gap(tmp_path)
    st = c.get(f"/api/day/{D}").json["stops"]
    c.patch(f"/api/stops/{st[1]['id']}", json={"business_name": "Beta", "status": "Confirmed"})
    c.post(f"/api/stops/{st[1]['id']}/mark", json={"which": "end"})
    end0 = c.get(f"/api/day/{D}").json["marks"]["end"]
    assert end0 == st[1]["depart"]
    c.patch(f"/api/stops/{st[1]['id']}", json={"depart": "11:30"})
    assert c.get(f"/api/day/{D}").json["marks"]["end"] == "11:30"


def test_leave_now_refuses_other_days(tmp_path):
    c = day_with_gap(tmp_path)
    st = c.get(f"/api/day/{D}").json["stops"]
    r = c.post(f"/api/stops/{st[1]['id']}/leave")
    assert r.status_code == 400 and "today" in r.json["error"]


def test_report_flags_an_estimated_drive_and_the_json_carries_it(tmp_path):
    c = day_with_gap(tmp_path)
    for s in c.get(f"/api/day/{D}").json["stops"]:
        c.patch(f"/api/stops/{s['id']}", json={"business_name": "Acme" if s["seq"] == 1 else "Beta",
                                              "status": "Confirmed", "notes": "x", "work_category": "Time & Material"})
    j = c.get(f"/api/export.json?from={D}").json
    dv = j["days"][0]["stops"][1]["drive_from_previous_stop"]
    assert dv["estimated"] and dv["no_location_data"]["from"]
    warn = c.get(f"/api/export/check?from={D}").json["warnings"]
    assert any("estimate" in w for w in warn)
    from tests.test_export import pdf_text
    import re
    assert "estimated: no location data" in re.sub(r"\s+", " ", pdf_text(c.get(f"/api/export.pdf?from={D}")))


def test_warnings_for_costless_billable_parts_units_and_end_day(tmp_path):
    from tests.test_parts import make, stop_ids
    c, _ = make(tmp_path)
    sid = stop_ids(c)[0]
    c.patch(f"/api/stops/{sid}", json={"business_name": "Acme", "status": "Confirmed", "notes": "x",
                                      "work_category": "Time & Material"})
    c.post(f"/api/stops/{sid}/parts", json={"name": "Cat 6 cable", "qty": 10, "billable": True})
    from tests.test_parts import D as PD
    w = c.get(f"/api/export/check?from={PD}").json["warnings"]
    assert any("no unit cost" in x for x in w) and any("counted in each" in x for x in w)


def test_auto_sync_only_fetches_when_the_recorder_has_something_new(tmp_path):
    cfg = replace(CFG, data_dir=str(tmp_path))
    d = sample.Day(D)
    d.dwell(*A, d.at(8), 30)
    rec = sample.ListRecorder(d.points())
    c = create_app(cfg, client=rec).test_client()
    c.get(f"/api/day/{D}")
    with mock.patch.object(rec, "fetch", wraps=rec.fetch) as f:
        r = c.post(f"/api/day/{D}/auto-sync")
        assert r.status_code == 200 and f.call_count == 0                # nothing newer than what is stored
    d.dwell(*A, d.at(8) + 30 * 60, 20)
    rec.points = d.points()
    with mock.patch.object(rec, "fetch", wraps=rec.fetch) as f:
        r = c.post(f"/api/day/{D}/auto-sync")
        assert f.call_count == 1 and f.call_args.args[0].timestamp() > d.at(8)    # incremental, not the whole day
    assert r.json["stops"][0]["minutes"] >= 45                            # the open stop grew


def test_auto_sync_setting_defaults_to_two_minutes(tmp_path):
    c = create_app(replace(CFG, data_dir=str(tmp_path)), client=sample.ListRecorder([])).test_client()
    assert c.get(f"/api/day/{D}").json["auto_sync_min"] == CFG.auto_sync_min == 2
    assert c.get("/settings").get_data(as_text=True).count("Auto-sync every (min)") == 1


def test_vendor_marking_and_hints(tmp_path):
    from tests.test_parts import make, part, stop_ids
    c, _ = make(tmp_path)
    sid = stop_ids(c)[0]
    c.patch(f"/api/stops/{sid}", json={"business_name": "The Home Depot", "category": "shop=doityourself"})
    st = c.get("/api/day/2026-11-18").json["stops"][0]
    assert st["vendor_hint"] is True and st["site_kind"] != "vendor"
    c.post(f"/api/stops/{sid}/save-place", json={"kind": "vendor"})
    st = c.get("/api/day/2026-11-18").json["stops"][0]
    assert st["site_kind"] == "vendor" and st["vendor_hint"] is False and st["work_category"] == "Internal operations"
    # a part vendor with the same name as a stop also triggers the offer
    part(c, name="Valve", sku="V1", vendor="Ferguson Supply")
    c.patch(f"/api/stops/{stop_ids(c)[1]}", json={"business_name": "Ferguson Supply"})
    assert c.get("/api/day/2026-11-18").json["stops"][1]["vendor_hint"] is True


def test_a_named_personal_stop_is_no_longer_turned_into_a_vendor(tmp_path):
    from tests.test_parts import make, stop_ids
    c, _ = make(tmp_path)
    sid = stop_ids(c)[0]
    c.patch(f"/api/stops/{sid}", json={"business_name": "Starbucks", "status": "Personal"})
    assert c.get("/api/day/2026-11-18").json["stops"][0]["site_kind"] == ""
