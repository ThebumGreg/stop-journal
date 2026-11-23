"""Every stop has a stable uid that survives edits, merge, split, re-detect and undo, and is in the report."""
import re

from app import db
from tests.test_commute import D, make

UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


def stops(c):
    return c.get(f"/api/day/{D}").json["stops"]


def job(c):
    return next(s for s in stops(c) if s["business_name"] == "Acme")


def test_every_stop_has_a_unique_uuid(tmp_path):
    c = make(tmp_path)
    uids = [s["uid"] for s in stops(c)]
    assert all(UUID.match(u) for u in uids) and len(set(uids)) == len(uids)


def test_uid_survives_edit_split_merge_redetect_undo(tmp_path):
    c = make(tmp_path)
    j = job(c)
    uid = j["uid"]
    c.patch(f"/api/stops/{j['id']}", json={"notes": "edited"})
    assert job(c)["uid"] == uid
    # split: the first part keeps the uid, the new part gets a new one
    r = c.post(f"/api/stops/{j['id']}/split", json={"at": "09:20"})
    assert r.status_code == 200, r.json
    parts = [s for s in stops(c) if j["arrive_ts"] <= s["arrive_ts"] < j["depart_ts"]]
    assert parts[0]["uid"] == uid and parts[1]["uid"] != uid and UUID.match(parts[1]["uid"])
    # undo the split: back to one stop with the same uid
    c.post(f"/api/day/{D}/undo")
    assert [s["uid"] for s in stops(c)].count(uid) == 1 and len(stops(c)) == 3
    # merge the job with the next stop: the earlier (surviving) stop keeps its uid
    ss = stops(c)
    k = next(i for i, s in enumerate(ss) if s["uid"] == uid)
    assert c.post("/api/stops/merge", json={"ids": [ss[k]["id"], ss[k + 1]["id"]]}).status_code == 200
    assert uid in [s["uid"] for s in stops(c)]
    c.post(f"/api/day/{D}/undo")
    # re-detect: the new stop at the same time gets the old uid
    before = {s["arrive"]: s["uid"] for s in stops(c)}
    assert c.post(f"/api/day/{D}/redetect", json={}).status_code == 200
    after = {s["arrive"]: s["uid"] for s in stops(c)}
    assert after == before
    c.post(f"/api/day/{D}/undo")
    assert {s["arrive"]: s["uid"] for s in stops(c)} == before


def test_uid_in_all_report_lists_and_report_key(tmp_path):
    c = make(tmp_path)
    j = job(c)
    c.post(f"/api/stops/{j['id']}/follow-ups", json={"text": "Call back", "owner": "Me", "due": "2026-11-20"})
    r = c.get(f"/api/export.json?from={D}").json
    day = r["days"][0]
    assert day["report_key"] == D
    assert day["stops"][0]["stop_uid"] == j["uid"]
    assert r["records_to_update"][0]["stop_uid"] == j["uid"]
    assert r["follow_ups"][0]["stop_uid"] == j["uid"] and r["follow_ups"][0]["follow_up_id"]
    assert day["stops"][0]["follow_ups"][0]["follow_up_id"] == r["follow_ups"][0]["follow_up_id"]


def test_existing_rows_get_a_uid_on_connect(tmp_path):
    c = make(tmp_path)
    conn = db.connect(str(tmp_path))
    conn.execute("DROP INDEX idx_stops_uid")
    conn.execute("UPDATE stops SET uid=NULL")
    conn.commit(); conn.close()
    conn = db.connect(str(tmp_path))
    uids = [r[0] for r in conn.execute("SELECT uid FROM stops")]
    assert all(u and UUID.match(u) for u in uids) and len(set(uids)) == len(uids)
