import io
import re
from dataclasses import replace

import pytest
from pypdf import PdfReader

from app import create_app, sample
from tests.test_detect import CFG

D = "2026-11-18"
WORK = "Replaced shutoff valve → tested at 60 psi ≈ OK, 5% over.\nSecond line with <b>tags</b> & ampersand."


def make(tmp_path, points=None, **kw):
    cfg = replace(CFG, data_dir=str(tmp_path), **kw)
    return create_app(cfg, client=sample.ListRecorder(sample.normal_day() if points is None else points)).test_client()


def pdf_text(resp):
    assert resp.mimetype == "application/pdf"
    r = PdfReader(io.BytesIO(resp.data))
    return "\n".join(page.extract_text() for page in r.pages)


def scenario(c):
    """S1 confirmed (parts + unicode notes), S2 confirmed, S3 personal, plus a draft manual stop."""
    ids = [s["id"] for s in c.get(f"/api/day/{D}").json["stops"]]
    v = c.post("/api/parts", json={"name": "1/2 in. ball valve", "sku": "BV-050", "unit_cost": "8.75",
                                   "on_hand": "10"}).json
    c.patch(f"/api/stops/{ids[0]}", json={"status": "Confirmed", "business_name": "Acme Hardware",
                                         "address": "12 Main St, Testville", "notes": WORK})
    c.post(f"/api/stops/{ids[0]}/parts", json={"part_id": v["id"], "qty": 2})
    c.patch(f"/api/stops/{ids[1]}", json={"status": "Confirmed", "business_name": "Beta Plumbing",
                                         "address": "9 Oak Ave", "notes": "Quoted job."})
    c.patch(f"/api/stops/{ids[2]}", json={"status": "Personal", "business_name": "Secret Diner"})
    c.post(f"/api/day/{D}/stops", json={"arrive": "13:30", "depart": "14:00", "business_name": "Phone job"})
    return ids


def test_json_report_structure_and_values(tmp_path):
    c = make(tmp_path)
    scenario(c)
    r = c.get(f"/api/export.json?from={D}")
    assert r.status_code == 200 and "attachment; filename=jobhub-daily-summary-2026-11-18.json" in r.headers["Content-Disposition"]
    rep = r.json
    assert rep["schema_version"] == "1" and rep["timezone"] == "America/Los_Angeles"
    day = rep["days"][0]
    s1, s2 = day["stops"]
    assert (s1["stop_number"], s1["business_name"], s1["address"]) == (1, "Acme Hardware", "12 Main St, Testville")
    assert (s1["arrival"], s1["departure"], s1["time_on_site_minutes"]) == ("09:00", "10:00", 60)
    assert s1["arrival_iso"].startswith("2026-11-18T09:00:00-08:00")
    assert s1["work_performed"] == WORK and s1["hubspot_company_id"] == ""
    assert s1["drive_from_previous_stop"] is None                       # first stop of the day
    assert s1["parts_used"][0]["part_number"] == "BV-050" and s1["parts_used"][0]["quantity"] == 2
    assert s2["stop_number"] == 2 and 18 <= s2["drive_from_previous_stop"]["minutes"] <= 22
    assert 4 < s2["drive_from_previous_stop"]["miles_approx"] < 5
    assert day["summary"]["stops_confirmed"] == 2 and day["summary"]["on_site_minutes"] == 120
    assert day["summary"]["drive_minutes"] == s2["drive_from_previous_stop"]["minutes"]
    assert [(e["arrival"], e["reason"]) for e in day["excluded"]] == [
        ("12:00", "Personal"), ("13:30", "Not confirmed")]
    rec = rep["records_to_update"]
    assert [x["business_name"] for x in rec] == ["Acme Hardware", "Beta Plumbing"]
    assert rec[0]["parts_used"] == ["2 x 1/2 in. ball valve (BV-050)"]
    assert "Secret Diner" not in r.get_data(as_text=True)                       # excluded stops carry no detail


def test_drive_from_personal_stop_is_blank(tmp_path):
    c = make(tmp_path)
    ids = [s["id"] for s in c.get(f"/api/day/{D}").json["stops"]]
    c.patch(f"/api/stops/{ids[1]}", json={"status": "Personal"})
    c.patch(f"/api/stops/{ids[2]}", json={"status": "Confirmed", "business_name": "Gamma"})
    stop = c.get(f"/api/export.json?from={D}").json["days"][0]["stops"][0]
    assert stop["business_name"] == "Gamma" and stop["drive_from_previous_stop"] is None


def test_pdf_text_extraction_every_field_in_sensible_order(tmp_path):
    c = make(tmp_path)
    scenario(c)
    r = c.get(f"/api/export.pdf?from={D}")
    assert "attachment; filename=jobhub-daily-summary-2026-11-18.pdf" in r.headers["Content-Disposition"]
    text = pdf_text(r)
    flat = re.sub(r"\s+", " ", text)
    s1 = c.get(f"/api/export.json?from={D}").json["days"][0]["stops"][0]
    latlon = f"{s1['latitude']:.6f}, {s1['longitude']:.6f}"

    # header
    for needle in ("JobHub Daily Summary", "Wednesday, 2026-11-18", "schema version 1"):
        assert needle in flat
    top = flat.split("Stop 1")[0]
    assert "Generated at" not in top and "Timezone" not in top and "Report schema version" not in top
    assert "Drive after last stop" not in top
    assert "America/Los_Angeles" in flat.split("How to read this report")[1]       # the timezone is stated in the notes
    # summary
    for needle in ("On-site time", "120 min (2 h 00 min)", "Drive time", "Approximate miles", "Stops confirmed"):
        assert needle in flat
    # every labeled field of the stop block, with values
    for needle in ("Stop number", "Business name", "Acme Hardware", "Address", "12 Main St, Testville",
                   "Latitude/Longitude", latlon, "Arrival", "09:00", "Departure", "10:00",
                   "Time on site", "60 min (1 h 00 min)", "Drive from previous stop", "n/a",
                   "HubSpot company ID", "Work performed", "Replaced shutoff valve → tested at 60 psi ≈ OK, 5% over.",
                   "Second line with <b>tags</b> & ampersand.", "Parts used", "1/2 in. ball valve", "BV-050",
                   "8.75", "Beta Plumbing", "9 Oak Ave", "Quoted job."):
        assert needle in flat, needle
    # records table, excluded section (times only), instructions
    assert "Personal" not in text.split("Day recap")[1].split("How to read")[0]      # personal stops are not listed
    assert "13:30 14:00 Not confirmed (left out)" in flat and "12:00 12:45" not in flat
    assert "Secret Diner" not in text and "Phone job" not in text
    assert "How to read this report" in flat and "do not create records for them" in flat

    pos = 0
    assert "Day summary" not in text and "Excluded" not in text        # header + summary are one section; recap replaces Excluded
    for marker in ("JobHub Daily Summary", "On-site time", "Stop 1", "Stop 2", "Records to update",
                   "Day recap", "How to read this report"):
        i = text.find(marker, pos)
        assert i >= 0, marker
        pos = i + 1


def test_manual_stop_without_gps_and_empty_day(tmp_path):
    c = make(tmp_path, points=[])
    c.get(f"/api/day/{D}")
    c.post(f"/api/day/{D}/stops", json={"arrive": "09:00", "depart": "09:45", "business_name": "Remote job"})
    sid = c.get(f"/api/day/{D}").json["stops"][0]["id"]
    c.patch(f"/api/stops/{sid}", json={"status": "Confirmed", "notes": "Phone support"})
    assert "(no GPS data)" in re.sub(r"\s+", " ", pdf_text(c.get(f"/api/export.pdf?from={D}")))
    empty = make(tmp_path / "e", points=[])
    assert "No confirmed stops." in pdf_text(empty.get(f"/api/export.pdf?from={D}"))
    assert empty.get(f"/api/export.json?from={D}").json["days"][0]["stops"] == []


def test_long_text_spans_pages_without_loss(tmp_path):
    c = make(tmp_path)
    sid = c.get(f"/api/day/{D}").json["stops"][0]["id"]
    long = "\n".join(f"Line {i}: replaced part {i}." for i in range(300))
    c.patch(f"/api/stops/{sid}", json={"status": "Confirmed", "business_name": "Big Job", "notes": long})
    text = pdf_text(c.get(f"/api/export.pdf?from={D}"))
    assert "Line 0: replaced part 0." in text and "Line 299: replaced part 299." in text


def test_check_endpoint_warns_about_drafts(tmp_path):
    c = make(tmp_path)
    scenario(c)
    chk = c.get(f"/api/export/check?from={D}").json
    assert (chk["confirmed"], chk["not_confirmed"], chk["personal"]) == (2, 1, 1)


def test_date_range_export(tmp_path):
    pts = sample.normal_day("2026-11-18") + sample.short_stop_day("2026-11-19")
    c = make(tmp_path, points=pts)
    for d in ("2026-11-18", "2026-11-19"):
        st = c.get(f"/api/day/{d}").json["stops"][0]
        c.patch(f"/api/stops/{st['id']}", json={"status": "Confirmed", "business_name": f"Client {d[-2:]}"})
    r = c.get("/api/export.pdf?from=2026-11-18&to=2026-11-19")
    assert "jobhub-daily-summary-2026-11-18_to_2026-11-19.pdf" in r.headers["Content-Disposition"]
    text = re.sub(r"\s+", " ", pdf_text(r))
    assert "2026-11-18 to 2026-11-19" in text and "Client 18" in text and "Client 19" in text
    assert "Date" in text and "Day summary: Thursday, 2026-11-19" in text
    j = c.get("/api/export.json?from=2026-11-18&to=2026-11-19").json
    assert [d["date"] for d in j["days"]] == ["2026-11-18", "2026-11-19"]
    assert [x["date"] for x in j["records_to_update"]] == ["2026-11-18", "2026-11-19"]


@pytest.mark.parametrize("qs", ["", "from=nope", "from=2026-11-19&to=2026-11-18", "from=2026-01-01&to=2026-12-31"])
def test_bad_ranges_rejected(tmp_path, qs):
    c = make(tmp_path)
    for path in ("/api/export.json", "/api/export.pdf", "/api/export/check"):
        assert c.get(f"{path}?{qs}").status_code == 400


def test_export_requires_login(tmp_path):
    c = make(tmp_path, app_password="pw")
    assert c.get(f"/api/export.pdf?from={D}").status_code == 401
    c.post("/login", data={"password": "pw"})
    assert c.get(f"/api/export.pdf?from={D}").status_code == 200


def test_export_lives_on_tools_page(tmp_path):
    c = make(tmp_path)
    page = c.get(f"/tools?date={D}")
    assert page.status_code == 200 and b"Export" in page.data
    assert "/api/export.pdf" in c.get("/static/tools.js").data.decode()
    day = c.get("/static/day.js").data.decode()
    assert "Export…" not in day and "Re-detect…" not in day
    assert b'href="/tools"' in c.get(f"/day/{D}").data


def test_one_huge_line_and_words_are_not_split(tmp_path):
    c = make(tmp_path)
    sid = c.get(f"/api/day/{D}").json["stops"][0]["id"]
    huge = " ".join(f"word{i}" for i in range(6000))               # one line, ~40k characters
    c.patch(f"/api/stops/{sid}", json={"status": "Confirmed", "business_name": "Huge", "notes": huge})
    text = re.sub(r"\s+", " ", pdf_text(c.get(f"/api/export.pdf?from={D}")))
    assert "word0 " in text and "word5999" in text
    c.patch(f"/api/stops/{sid}", json={"notes": "Customer wants a follow-up quote for the water heater."})
    text = re.sub(r"\s+", " ", pdf_text(c.get(f"/api/export.pdf?from={D}")))
    assert text.count("follow-up quote for the water heater.") >= 1   # whole phrase intact in the stop block


def test_omit_sections_from_json_and_pdf(tmp_path):
    c = make(tmp_path)
    scenario(c)
    full = c.get(f"/api/export.json?from={D}").json
    assert full["omitted"] == []
    assert full["days"][0]["stops"][0]["parts_used"][0]["unit_cost"] == 8.75
    j = c.get(f"/api/export.json?from={D}&omit=costs,recap,mileage,follow_ups").json
    assert [o["key"] for o in j["omitted"]] == ["costs", "follow_ups", "mileage", "recap"]
    p = j["days"][0]["stops"][0]["parts_used"][0]
    assert "unit_cost" not in p and "line_cost" not in p and p["name"] == "1/2 in. ball valve"
    assert "timesheet" not in j["days"][0] and "mileage" not in j["days"][0] and "follow_ups" not in j
    text = pdf_text(c.get(f"/api/export.pdf?from={D}&omit=costs,recap,follow_ups"))
    assert "Unit cost" not in text and "8.75" not in text
    assert "Clock in" not in text and "Day recap (timesheet): the day" not in text
    assert "BV-050" in text                       # the part itself is still listed
    assert re.search(r"Left out of this export on purpose.*Part and material costs", text.replace("\n", " "))
    full_text = pdf_text(c.get(f"/api/export.pdf?from={D}"))
    assert "Unit cost" in full_text and "Clock in" in full_text and "Left out of this export" not in full_text


def test_omit_rejects_unknown_and_check_ignores_it(tmp_path):
    c = make(tmp_path)
    scenario(c)
    assert c.get(f"/api/export.json?from={D}&omit=bogus").status_code == 400
    r = c.get(f"/api/export.pdf?from={D}&inline=1")
    assert r.headers["Content-Disposition"].startswith("inline")
    assert c.get(f"/api/export/check?from={D}&omit=costs").status_code == 200
