from dataclasses import replace


from app import create_app, db, sample
from tests.test_detect import CFG

D = "2026-11-18"


def make(tmp_path, points):
    cfg = replace(CFG, data_dir=str(tmp_path))
    rec = sample.ListRecorder(points)
    return create_app(cfg, client=rec).test_client(), rec, cfg


def get(c, date=D):
    return c.get(f"/api/day/{date}").json


def test_materialize_once_and_not_resurrected(tmp_path):
    c, _, _ = make(tmp_path, sample.normal_day())
    d = get(c)
    assert [s["kind"] for s in d["stops"]] == ["stop"] * 3 and len(d["drives"]) == 2
    assert d["stops"][0]["arrive"] == "09:00" and d["totals"]["draft"] == 3
    sid = d["stops"][1]["id"]
    assert c.delete(f"/api/stops/{sid}").status_code == 200
    d = get(c)
    assert len(d["stops"]) == 2 and len(d["drives"]) == 1  # not re-detected


def test_confirm_requires_name_and_teaches_known_sites_once(tmp_path):
    c, _, cfg = make(tmp_path, sample.normal_day())
    sid = get(c)["stops"][0]["id"]
    r = c.patch(f"/api/stops/{sid}", json={"status": "Confirmed"})
    assert r.status_code == 400 and "business name" in r.json["error"]
    r = c.patch(f"/api/stops/{sid}", json={"status": "Confirmed", "business_name": "Acme",
                                          "address": "1 Main St"})
    assert r.status_code == 200 and r.json["totals"]["confirmed"] == 1
    c.patch(f"/api/stops/{sid}", json={"notes": "fixed the pump\nsecond line"})
    rows = db.connect(cfg.data_dir).execute("SELECT * FROM known_sites").fetchall()
    assert len(rows) == 1 and rows[0]["times_confirmed"] == 1
    s = get(c)["stops"][0]
    assert s["notes"] == "fixed the pump\nsecond line" and s["known_site_id"] == rows[0]["id"]


def test_time_edit_validation_and_drive_follows(tmp_path):
    c, _, _ = make(tmp_path, sample.normal_day())
    d = get(c)
    a, b = d["stops"][0], d["stops"][1]
    assert c.patch(f"/api/stops/{a['id']}", json={"depart": "10:30"}).status_code == 400  # overlaps B
    assert c.patch(f"/api/stops/{a['id']}", json={"depart": "08:00"}).status_code == 400  # before arrive
    assert c.patch(f"/api/stops/{a['id']}", json={"depart": "xx"}).status_code == 400
    r = c.patch(f"/api/stops/{a['id']}", json={"depart": "10:10"})
    assert r.status_code == 200
    dr = r.json["drives"][0]
    assert dr["depart"] == "10:10" and dr["arrive"] == b["arrive"]


def test_drive_edit_notes_and_times(tmp_path):
    c, _, _ = make(tmp_path, sample.normal_day())
    did = get(c)["drives"][0]["id"]
    r = c.patch(f"/api/drives/{did}", json={"notes": "traffic", "depart": "10:05"})
    dr = r.json["drives"][0]
    assert dr["notes"] == "traffic" and dr["depart"] == "10:05" and dr["edited"]
    # editing a neighbouring stop must not clobber the edited drive time
    sid = r.json["stops"][0]["id"]
    r = c.patch(f"/api/stops/{sid}", json={"notes": "x"})
    assert r.json["drives"][0]["depart"] == "10:05"
    assert c.patch(f"/api/drives/{did}", json={"arrive": "09:00"}).status_code == 400


def test_split_and_merge(tmp_path):
    c, _, _ = make(tmp_path, sample.normal_day())
    s0 = get(c)["stops"][0]
    assert c.post(f"/api/stops/{s0['id']}/split", json={"at": "08:00"}).status_code == 400
    r = c.post(f"/api/stops/{s0['id']}/split", json={"at": "09:30"})
    st = r.json["stops"]
    assert len(st) == 4 and st[0]["depart"] == "09:30" and st[1]["arrive"] == "09:30"
    assert len(r.json["drives"]) == 3
    r = c.post("/api/stops/merge", json={"ids": [st[0]["id"], st[1]["id"]]})
    assert len(r.json["stops"]) == 3 and r.json["stops"][0]["depart"] == "10:00"
    far = r.json["stops"]
    assert c.post("/api/stops/merge", json={"ids": [far[0]["id"], far[2]["id"]]}).status_code == 400


def test_dismiss_short_stop_joins_drives(tmp_path):
    c, _, _ = make(tmp_path, sample.short_stop_day())
    d = get(c)
    assert [s["kind"] for s in d["stops"]] == ["stop", "short", "stop"]
    assert d["totals"]["draft_short"] == 1 and d["totals"]["draft"] == 2
    r = c.delete(f"/api/stops/{d['stops'][1]['id']}")
    assert len(r.json["stops"]) == 2 and len(r.json["drives"]) == 1
    # direct drive now spans the whole gap between the real stops
    assert r.json["drives"][0]["minutes"] >= 29


def test_confirmed_short_stop_counts_draft_short_does_not(tmp_path):
    c, _, _ = make(tmp_path, sample.short_stop_day())
    d = get(c)
    base_on_site = d["totals"]["on_site_s"]
    sid = d["stops"][1]["id"]
    r = c.patch(f"/api/stops/{sid}", json={"status": "Confirmed", "business_name": "Gas"})
    assert r.json["totals"]["on_site_s"] > base_on_site


def test_personal_excluded_from_totals(tmp_path):
    c, _, _ = make(tmp_path, sample.normal_day())
    d = get(c)
    sid = d["stops"][1]["id"]
    r = c.patch(f"/api/stops/{sid}", json={"status": "Personal"}).json
    assert r["totals"]["personal"] == 1
    assert r["totals"]["on_site_s"] == d["totals"]["on_site_s"] - (d["stops"][1]["depart_ts"] - d["stops"][1]["arrive_ts"])
    assert r["totals"]["drive_s"] == 0 and r["totals"]["miles_approx"] == 0  # both drives touch it


def test_manual_stop(tmp_path):
    c, _, _ = make(tmp_path, sample.normal_day())
    r = c.post(f"/api/day/{D}/stops", json={"arrive": "13:30", "depart": "14:15",
                                           "business_name": "Phone job", "notes": "n"})
    assert r.status_code == 200
    st = r.json["stops"][-1]
    assert st["source"] == "manual" and st["lat"] is None and len(r.json["drives"]) == 3
    assert c.post(f"/api/day/{D}/stops", json={"arrive": "09:10", "depart": "09:20"}).status_code == 400
    r = c.patch(f"/api/stops/{st['id']}", json={"status": "Confirmed"})  # name already set
    assert r.status_code == 200
    assert db.connect(str(tmp_path)).execute("SELECT count(*) c FROM known_sites").fetchone()["c"] == 0


def test_nudge_pin(tmp_path):
    c, _, _ = make(tmp_path, sample.normal_day())
    sid = get(c)["stops"][0]["id"]
    r = c.patch(f"/api/stops/{sid}", json={"lat": 34.06, "lon": -118.26})
    assert r.json["stops"][0]["lat"] == 34.06
    assert c.patch(f"/api/stops/{sid}", json={"lat": 999, "lon": 0}).status_code == 400


def test_redetect_keeps_manual_drops_edits(tmp_path):
    c, _, _ = make(tmp_path, sample.normal_day())
    sid = get(c)["stops"][0]["id"]
    c.patch(f"/api/stops/{sid}", json={"business_name": "Edited"})
    c.post(f"/api/day/{D}/stops", json={"arrive": "13:30", "depart": "14:15", "business_name": "M"})
    r = c.post(f"/api/day/{D}/redetect", json={"min": 10, "radius": 100, "gap": 15})
    names = [s["business_name"] for s in r.json["stops"]]
    assert "Edited" not in names and "M" in names and len(names) == 4
    assert c.post(f"/api/day/{D}/redetect", json={"min": "abc"}).status_code == 400


def test_sync_adds_new_and_preserves_edits(tmp_path):
    full = sample.normal_day()
    cut = sample.midnight_utc(D) + 11 * 3600 + 30 * 60  # 11:30
    c, rec, _ = make(tmp_path, [p for p in full if p["tst"] < cut])
    d = get(c)
    assert len(d["stops"]) == 2
    sid = d["stops"][0]["id"]
    c.patch(f"/api/stops/{sid}", json={"business_name": "Keep me", "status": "Confirmed"})
    rec.points = full  # the rest of the day arrives
    r = c.post(f"/api/day/{D}/sync").json
    assert r["added"] == 1 and len(r["stops"]) == 3
    assert r["stops"][0]["business_name"] == "Keep me" and r["stops"][0]["status"] == "Confirmed"
    assert len(r["drives"]) == 2 and r["path"]


def test_bad_date_and_login_unaffected(tmp_path):
    c, _, _ = make(tmp_path, [])
    assert c.get("/api/day/not-a-date").status_code == 400
    d = get(c)
    assert d["stops"] == [] and d["totals"]["on_site_s"] == 0
