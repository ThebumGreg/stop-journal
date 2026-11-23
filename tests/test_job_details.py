import io
import re

from tests.test_export import D, make, pdf_text, scenario
from tests.test_parts import part


def day(c):
    return c.get(f"/api/day/{D}").json


def ids(c):
    return [s["id"] for s in day(c)["stops"]]


def confirm(c, sid, name, **extra):
    r = c.patch(f"/api/stops/{sid}", json={"status": "Confirmed", "business_name": name, **extra})
    assert r.status_code == 200, r.json
    return r.json


# ---- fields on the stop ---------------------------------------------------
def test_category_ticket_and_result_save_and_validate(tmp_path):
    c = make(tmp_path)
    sid = ids(c)[0]
    r = c.patch(f"/api/stops/{sid}", json={"work_category": "Time & Material", "ticket": " #1042 ",
                                           "visit_result": "return"})
    s = next(x for x in r.json["stops"] if x["id"] == sid)
    assert (s["work_category"], s["ticket"], s["visit_result"]) == ("Time & Material", "1042", "return")
    assert c.patch(f"/api/stops/{sid}", json={"work_category": "Golf"}).status_code == 400
    assert c.patch(f"/api/stops/{sid}", json={"visit_result": "maybe"}).status_code == 400
    assert c.patch(f"/api/stops/{sid}", json={"ticket": "x" * 81}).status_code == 400
    r = c.patch(f"/api/stops/{sid}", json={"work_category": "", "visit_result": ""})
    s = next(x for x in r.json["stops"] if x["id"] == sid)
    assert s["work_category"] == "" and s["visit_result"] == "" and s["ticket"] == "1042"


def test_category_is_remembered_per_business(tmp_path):
    c = make(tmp_path)
    a, b = ids(c)[:2]
    confirm(c, a, "Acme Hardware", work_category="Quoted project")
    r = confirm(c, b, "acme hardware")                      # same business (case-insensitive), nothing chosen
    assert next(x for x in r["stops"] if x["id"] == b)["work_category"] == "Quoted project"
    # an explicit choice is never overwritten by the hint
    c.patch(f"/api/stops/{b}", json={"status": "Draft", "work_category": "Training"})
    r = confirm(c, b, "Acme Hardware")
    assert next(x for x in r["stops"] if x["id"] == b)["work_category"] == "Training"
    third = ids(c)[2]
    c.patch(f"/api/stops/{third}", json={"business_name": "Acme Hardware"})
    assert next(x for x in day(c)["stops"] if x["id"] == third)["category_hint"] in ("Quoted project", "Training")


# ---- follow-ups -----------------------------------------------------------
def test_follow_up_crud_and_validation(tmp_path):
    c = make(tmp_path)
    sid = ids(c)[0]
    r = c.post(f"/api/stops/{sid}/follow-ups", json={"text": "Order valve", "owner": "Me", "due": "2026-11-25"})
    assert r.status_code == 200
    fu = next(x for x in r.json["stops"] if x["id"] == sid)["follow_ups"]
    assert fu == [{"id": fu[0]["id"], "text": "Order valve", "owner": "Me", "due": "2026-11-25"}]
    assert c.post(f"/api/stops/{sid}/follow-ups", json={"text": "  "}).status_code == 400
    assert c.post(f"/api/stops/{sid}/follow-ups", json={"text": "x", "due": "25/11"}).status_code == 400
    r = c.patch(f"/api/follow-ups/{fu[0]['id']}", json={"owner": "Dana", "due": ""})
    got = next(x for x in r.json["stops"] if x["id"] == sid)["follow_ups"][0]
    assert (got["owner"], got["due"], got["text"]) == ("Dana", "", "Order valve")
    assert c.patch(f"/api/follow-ups/{fu[0]['id']}", json={"text": ""}).status_code == 400
    r = c.delete(f"/api/follow-ups/{fu[0]['id']}")
    assert next(x for x in r.json["stops"] if x["id"] == sid)["follow_ups"] == []
    assert c.delete(f"/api/follow-ups/{fu[0]['id']}").status_code == 400


def test_follow_ups_survive_merge_and_undo_and_go_with_a_deleted_stop(tmp_path):
    c = make(tmp_path)
    a, b = ids(c)[:2]
    c.post(f"/api/stops/{a}/follow-ups", json={"text": "A thing", "owner": "Me", "due": "2026-12-01"})
    c.post(f"/api/stops/{b}/follow-ups", json={"text": "B thing", "owner": "Me", "due": "2026-12-02"})
    r = c.post("/api/stops/merge", json={"ids": [a, b]})
    merged = next(x for x in r.json["stops"] if x["id"] == a)
    assert sorted(f["text"] for f in merged["follow_ups"]) == ["A thing", "B thing"]
    c.post(f"/api/day/{D}/undo")
    texts = {x["id"]: [f["text"] for f in x["follow_ups"]] for x in day(c)["stops"]}
    assert texts[a] == ["A thing"] and texts[b] == ["B thing"]
    c.delete(f"/api/stops/{b}")
    from app import db
    conn = db.connect(str(tmp_path))
    assert conn.execute("SELECT COUNT(*) FROM follow_ups WHERE stop_id=?", (b,)).fetchone()[0] == 0
    c.post(f"/api/day/{D}/undo")
    assert [f["text"] for x in day(c)["stops"] if x["id"] == b for f in x["follow_ups"]] == ["B thing"]


# ---- expenses are parts of a third kind -----------------------------------
def test_expense_is_money_not_stock(tmp_path):
    c = make(tmp_path)
    e = part(c, name="Parking", type="expense", unit_cost="5", on_hand="30", reorder_at="10")
    assert e["kind"] == "expense" and not e["tracked"] and e["billable"] is False and e["on_hand"] == 0
    assert c.get("/api/parts/shopping").json["groups"] == []
    assert c.get("/api/parts/count-sheet").json["parts"] == []
    assert part(c, name="Tape", type="consumable")["billable"] is False
    assert part(c, name="Valve")["billable"] is True
    c.patch(f"/api/parts/{e['id']}", json={"tracked": True})
    assert not next(p for p in c.get("/api/parts").json["parts"] if p["id"] == e["id"])["tracked"]


def test_expense_lines_take_their_own_amount_and_are_never_merged(tmp_path):
    c = make(tmp_path)
    sid = ids(c)[0]
    c.post(f"/api/stops/{sid}/parts", json={"name": "Parking", "qty": 1, "unit_cost": "12.50", "type": "expense"})
    c.post(f"/api/stops/{sid}/parts", json={"name": "parking", "qty": 1, "unit_cost": "3.25"})
    lines = [p for p in next(x for x in day(c)["stops"] if x["id"] == sid)["parts"]]
    assert len(lines) == 2 and all(p["expense"] and p["kind"] == "expense" for p in lines)
    assert sorted(p["unit_cost"] for p in lines) == [3.25, 12.5]
    cat = [p for p in c.get("/api/parts?all=1").json["parts"] if p["name"] == "Parking"]
    assert len(cat) == 1 and cat[0]["kind"] == "expense" and not cat[0]["tracked"]


def test_csv_accepts_expense_type(tmp_path):
    c = make(tmp_path)
    csv = "name,type,unit_cost,on_hand\nTolls,expense,4.00,50\nValve,part,5,2\n"
    r = c.post("/api/parts/import", data={"file": (io.BytesIO(csv.encode()), "p.csv")},
               content_type="multipart/form-data").json
    assert r["created"] == 2 and not r["errors"]
    tolls = next(p for p in c.get("/api/parts?all=1").json["parts"] if p["name"] == "Tolls")
    assert tolls["kind"] == "expense" and not tolls["tracked"] and tolls["on_hand"] == 0


# ---- the report -----------------------------------------------------------
def prepare(c):
    ids_ = scenario(c)
    c.patch(f"/api/stops/{ids_[0]}", json={"work_category": "Time & Material", "ticket": "#1042",
                                           "visit_result": "complete"})
    c.patch(f"/api/stops/{ids_[1]}", json={"work_category": "Quoted project", "notes": ""})
    c.post(f"/api/stops/{ids_[0]}/parts", json={"name": "Parking", "qty": 1, "unit_cost": "12.50", "type": "expense"})
    c.post(f"/api/stops/{ids_[0]}/follow-ups", json={"text": "Order the second valve", "owner": "Me", "due": "2026-11-25"})
    c.post(f"/api/stops/{ids_[1]}/follow-ups", json={"text": "Send the quote", "owner": "", "due": ""})
    return ids_


def test_json_report_carries_the_new_fields(tmp_path):
    c = make(tmp_path)
    prepare(c)
    rep = c.get(f"/api/export.json?from={D}").json
    day_ = rep["days"][0]
    s1, s2 = day_["stops"]
    assert (s1["category"], s1["billing"], s1["job_reference"], s1["visit_result"]) == (
        "Time & Material", "hourly", "1042", "Complete")
    assert s2["billing"] == "project" and s2["visit_result"] == ""
    assert [p["name"] for p in s1["expenses"]] == ["Parking"] and s1["expenses"][0]["line_cost"] == 12.5
    assert s1["parts_used"][0]["part_number"] == "BV-050" and all(p["name"] != "Parking" for p in s1["parts_used"])
    assert day_["summary"]["expenses_total"] == 12.5
    by = {r["category"]: r for r in day_["summary"]["hours_by_category"]}
    assert by["Time & Material"]["on_site_minutes"] == 60 and by["Quoted project"]["billing"] == "project"
    assert [f["what"] for f in rep["follow_ups"]] == ["Order the second valve", "Send the quote"]
    assert rep["follow_ups"][0]["business_name"] == "Acme Hardware" and rep["follow_ups"][0]["due"] == "2026-11-25"
    rec = rep["records_to_update"][0]
    assert rec["category"] == "Time & Material" and rec["job_reference"] == "1042"
    assert rep["schema_version"] == "1"


def test_uncategorised_stop_is_listed_as_no_category(tmp_path):
    c = make(tmp_path)
    scenario(c)
    by = c.get(f"/api/export.json?from={D}").json["days"][0]["summary"]["hours_by_category"]
    assert [r["category"] for r in by] == ["No category"] and by[0]["on_site_minutes"] == 120


def test_export_check_warns_but_does_not_block(tmp_path):
    c = make(tmp_path)
    prepare(c)
    chk = c.get(f"/api/export/check?from={D}").json
    w = " ".join(chk["warnings"])
    assert "stop 2 (Beta Plumbing): no work performed written" in w
    assert "follow-up “Send the quote” has no owner or due date" in w
    assert "no category" not in w
    sid = ids(c)[1]
    c.patch(f"/api/stops/{sid}", json={"work_category": ""})
    assert "stop 2 (Beta Plumbing): no category chosen" in " ".join(c.get(f"/api/export/check?from={D}").json["warnings"])
    assert c.get(f"/api/export.pdf?from={D}").status_code == 200          # a warning never blocks


def test_pdf_shows_the_new_sections(tmp_path):
    c = make(tmp_path)
    prepare(c)
    text = pdf_text(c.get(f"/api/export.pdf?from={D}"))
    flat = re.sub(r"\s+", " ", text)
    for needle in ("Hours by category (on-site time)", "Billed hourly", "Tracked against the project",
                   "Category Time & Material (billed hourly)", "HubSpot ticket 1042", "Visit result Complete",
                   "Expenses", "Parking", "12.50", "Order the second valve", "Send the quote", "2026-11-25"):
        assert needle in flat, needle
    assert "Expenses 12.50" in flat                                  # the top section total
    rec = text.index("Records to update")
    assert rec < text.index("Follow-ups", rec) < text.index("Day recap")       # the section after the records table
    assert "0 h 10 min" not in flat and "0 h 50 min" not in flat


def test_same_site_merge_combines_the_new_fields(tmp_path):
    from tests.test_lunch_cases import JOB, build
    c = build(tmp_path, JOB)
    a, _, b = ids(c)
    c.patch(f"/api/stops/{a}", json={"work_category": "Time & Material", "ticket": "#7"})
    c.post(f"/api/stops/{a}/follow-ups", json={"text": "First", "owner": "Me", "due": "2026-12-01"})
    c.post(f"/api/stops/{b}/follow-ups", json={"text": "Second", "owner": "Me", "due": "2026-12-02"})
    c.patch(f"/api/stops/{b}", json={"visit_result": "return"})
    stops = c.get(f"/api/export.json?from={D}").json["days"][0]["stops"]
    assert len(stops) == 1
    assert stops[0]["category"] == "Time & Material" and stops[0]["job_reference"] == "7"
    assert stops[0]["visit_result"] == "Return visit needed"
    assert [f["what"] for f in stops[0]["follow_ups"]] == ["First", "Second"]
