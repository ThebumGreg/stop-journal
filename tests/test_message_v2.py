"""Message format 2 (the agent's spec, agreed 2026-11-24): one envelope, shared objects, ISO times, money as text,
UUIDs on everything updatable, no missing keys. Also the groundwork: stable uids, the Waiting split, address parts."""
import importlib.util
import json
import os
import re
from dataclasses import replace

import pytest

from app import address, db, invoice_request, late, mailer, message_v2
from app.places import known

D = "2026-11-18"
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
ISO = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d$")
MONEY = re.compile(r"^\d+\.\d{2,4}$")
TIME_KEYS = {"sent_at", "arrival", "departure", "start", "end", "day_start", "day_end", "billed_through", "taken_at",
             "new_eta", "time"}
MONEY_KEYS = {"unit_price", "amount", "unit_cost", "amount_paid", "total", "price_per_gallon", "expenses_total",
              "stock_bought_total", "fuel_total"}
UID_KEYS = {"message_id", "stop_uid", "line_uid", "photo_uid", "follow_up_uid"}


def check_format(obj, path="$"):
    """Every v2 rule that can be checked mechanically."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            here = f"{path}.{k}"
            assert re.match(r"^[a-z][a-z0-9_]*$", k), f"{here}: not snake_case"
            if k in TIME_KEYS and isinstance(v, str):
                assert v == "" or ISO.match(v), f"{here}: {v!r} is not ISO with seconds and offset"
            if k in MONEY_KEYS:
                assert isinstance(v, str) and (v == "" or MONEY.match(v)), f"{here}: {v!r} is not money text"
            if k in UID_KEYS and v not in (None, ""):
                assert UUID.match(v), f"{here}: {v!r} is not a UUID"
            if k.endswith("_minutes") and v is not None:
                assert isinstance(v, int), f"{here}: minutes must be whole"
            check_format(v, here)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            check_format(v, f"{path}[{i}]")


@pytest.fixture
def sample(tmp_path, monkeypatch):
    path = os.path.join(os.path.dirname(__file__), "..", "scripts", "make_sample_report.py")
    spec = importlib.util.spec_from_file_location("mk", path)
    mk = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mk)
    c, cfg = mk.build(str(tmp_path))
    conn = db.connect(str(tmp_path))
    for k, v in {"mail_to": "agent@work.example", "smtp_user": "t@gmail.example", "smtp_password": "x",
                 "report_signing_key": "shared phrase", "tech_name": "Sample Tech", "tech_email": "tech@work.example"}.items():
        conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)", (k, v))
    conn.commit(); conn.close()
    sent = []
    monkeypatch.setattr(mailer, "send", lambda cfg, msg: sent.append(msg))
    return c, sent, str(tmp_path)


def setting(data_dir, key, value):
    conn = db.connect(data_dir)
    conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)", (key, value))
    conn.commit(); conn.close()


def v2(c, omit=""):
    r = c.get(f"/api/export.json?from={D}&format=2" + (f"&omit={omit}" if omit else ""))
    assert r.status_code == 200, r.json
    return r.json


def attachment(m, suffix):
    p = next(p for p in m.iter_attachments() if p.get_filename().endswith(suffix))
    data = p.get_content()
    return p.get_filename(), (data if isinstance(data, bytes) else data.encode())


# ---- the daily summary -----------------------------------------------------------------------------------------
def test_daily_summary_format_and_numbers_match_format_1(sample):
    c, _, _ = sample
    m = v2(c)
    check_format(m)
    assert m["schema_version"] == "2" and m["type"] == "daily_summary" and m["subject_key"] == D
    assert m["revision"] == 1 and m["supersedes"] is None and m["app"] == {"name": "JobHub", "version": "2.0.0"}
    assert "records_to_update" not in m and "follow_ups" not in m             # removed in v2
    v1 = c.get(f"/api/export.json?from={D}").json["days"][0]
    day = m["days"][0]
    assert [s["stop_uid"] for s in day["stops"]] == [s["stop_uid"] for s in v1["stops"]]
    assert [s["time_on_site_minutes"] for s in day["stops"]] == [s["time_on_site_minutes"] for s in v1["stops"]]
    assert day["totals"]["paid_minutes"] == v1["summary"]["paid_minutes"]
    assert day["totals"]["miles"] == v1["summary"]["miles_approx"]
    kinds = {ln["kind"] for s in day["stops"] for ln in s["lines"]}
    assert kinds == {"part", "consumable", "expense", "stock"}
    for s in day["stops"]:
        assert set(s) >= {"stop_uid", "date", "place_type", "customer", "job", "arrival", "departure",
                          "time_on_site_minutes", "breaks", "drive_from_previous", "visit_result", "work_performed",
                          "lines", "follow_ups", "photo_uids", "invoice_request", "calendar_event_uid"}
        for ln in s["lines"]:
            assert UUID.match(ln["line_uid"])
            if ln["kind"] in ("consumable", "stock"):
                assert ln["billable"] is False
    tm = day["stops"][0]
    assert tm["place_type"] == "client" and tm["visit_result"] == "complete"
    assert tm["customer"]["service_address"]["city"] == "Los Angeles" and tm["customer"]["service_address"]["zip"] == "90012"
    assert tm["job"] == {"category": "Time & Material", "billing": "hourly", "hubspot_ticket_id": "24681357913",
                         "ticket_action": "use", "qbo_estimate_number": "", "tech_typed_reference": ""}
    cable = next(ln for ln in tm["lines"] if ln["sku"] == "C6-1000-BLU")
    assert cable["unit_price"] == "0.45" and cable["amount"] == "63.00" and cable["quantity"] == 140
    assert cable["unit_cost"] != ""                                              # our purchase cost (documentation)
    vendor = next(s for s in day["stops"] if s["place_type"] == "vendor")
    stock = [ln for ln in vendor["lines"] if ln["kind"] == "stock"]
    assert stock and all(ln["unit_price"] == "" and ln["amount"] == "" and ln["amount_paid"] for ln in stock)
    assert day["totals"]["stock_bought_total"] == "251.50"
    # every photo is listed once with a uid, and its stop lists it
    uids = {p["photo_uid"] for p in day["photos"]}
    assert uids == {u for s in day["stops"] for u in s["photo_uids"]} | {
        p["photo_uid"] for p in day["photos"] if p["stop_uid"] in {f["stop_uid"] for f in day["fuel"]}}
    assert all(p["attachment"] == f"photo-{p['photo_uid']}.jpg" for p in day["photos"])
    # the mileage legs chain by stop uid (the first starts at Home)
    legs = day["mileage"]["legs"]
    assert legs[0]["from_stop_uid"] is None and all(a["to_stop_uid"] == b["from_stop_uid"] for a, b in zip(legs, legs[1:]))
    assert day["mileage"]["basis"] == "home_to_home"


def test_omitted_sections_are_empty_not_missing(sample):
    c, _, _ = sample
    full = v2(c)["days"][0]
    m = v2(c, "costs,photos,coordinates,follow_ups,fuel,recap,mileage,categories,expenses,materials")
    check_format(m)
    assert m["omitted"] == ["costs", "expenses", "fuel", "photos", "materials", "categories", "follow_ups", "mileage",
                            "recap", "coordinates"]
    day = m["days"][0]
    assert day["photos"] == [] and day["fuel"] == [] and day["timesheet"] == [] and day["mileage"] is None
    t = day["totals"]
    assert t["expenses_total"] == "" and t["stock_bought_total"] == "" and t["fuel_total"] == "" and t["by_category"] == []
    for s in day["stops"]:
        assert s["photo_uids"] == [] and s["follow_ups"] == []
        assert s["customer"]["service_address"]["latitude"] is None
        assert {ln["kind"] for ln in s["lines"]} <= {"part", "stock"}
        for ln in s["lines"]:
            assert ln["unit_price"] == ln["amount"] == ln["unit_cost"] == ""
    assert len(day["stops"]) == len(full["stops"])


def test_email_format_2_names_signature_and_revisions(sample):
    c, sent, data_dir = sample
    r = c.post(f"/api/export/email?from={D}", json={"json": True, "pdf": True, "photos": True, "format": "2"})
    assert r.status_code == 200, r.json
    m = sent[0]
    assert m["Subject"] == f"JobHub Daily Summary {D}"
    name, data = attachment(m, ".json")
    assert name == f"jobhub-daily-summary-{D}-r1.json"
    msg = json.loads(data)
    assert msg["schema_version"] == "2" and msg["revision"] == 1
    body = m.get_body(preferencelist=("plain",)).get_content()
    assert "Signature: hmac-sha256=" + mailer.signature("shared phrase", data) in body
    assert f"Message ID: {msg['message_id']}" in body and "Message format: 2\nRevision: 1\n" in body
    pics = {p.get_filename() for p in m.iter_attachments() if p.get_content_maintype() == "image"}
    assert pics == {p["attachment"] for p in msg["days"][0]["photos"]} and pics
    assert all(p["attached"] for p in msg["days"][0]["photos"])
    assert any(p.get_filename() == f"jobhub-daily-summary-{D}-r1.pdf" for p in m.iter_attachments())
    # the second send: revision 2, "(update 2)", supersedes the first
    c.post(f"/api/export/email?from={D}", json={"json": True, "format": "2"})
    _, data2 = attachment(sent[1], ".json")
    msg2 = json.loads(data2)
    assert msg2["days"][0]["photos"] and not any(p["attached"] for p in msg2["days"][0]["photos"])   # not ticked
    assert sent[1]["Subject"] == f"JobHub Daily Summary {D} (update 2)"
    assert msg2["revision"] == 2 and msg2["supersedes"] == msg["message_id"] and msg2["message_id"] != msg["message_id"]
    assert c.get(f"/api/export/emails?from={D}").json["history"][0]["format"] == "2"
    # without the test tick, the setting decides (format 1 here)
    c.post(f"/api/export/email?from={D}", json={"json": True})
    _, data3 = attachment(sent[2], ".json")
    assert json.loads(data3)["schema_version"] == "1"


# ---- the invoice request ---------------------------------------------------------------------------------------
def tm_stop(c):
    return next(s for s in c.get(f"/api/day/{D}").json["stops"] if s.get("work_category") == "Time & Material")


def test_invoice_request_format_2_same_stop_as_summary(sample):
    c, sent, data_dir = sample
    setting(data_dir, "message_format", "2")
    s = tm_stop(c)
    before = next(x for x in v2(c)["days"][0]["stops"] if x["stop_uid"] == s["uid"])
    pv = c.get(f"/api/stops/{s['id']}/invoice-request").json
    assert pv["format"] == "2" and pv["billing_email"] == ""
    r = c.post(f"/api/stops/{s['id']}/invoice-request",
               json={"pdf": True, "photos": True, "invoice_to_email": "ap@dental.example"})
    assert r.status_code == 200, r.json
    m = sent[0]
    assert m["Subject"] == f"JobHub Invoice Request {D} – {s['business_name']}"
    name, data = attachment(m, ".json")
    assert name == f"jobhub-invoice-request-{D}-stop-1-sample-dental-office-r1.json"
    msg = json.loads(data)
    check_format(msg)
    assert msg["type"] == "invoice_request" and msg["subject_key"] == s["uid"] and msg["revision"] == 1
    assert msg["still_on_site"] is False and msg["billed_through"] == before["departure"]
    assert msg["invoice_to_email"] == "ap@dental.example"
    assert msg["stop"] == before                                         # one Stop object, the same in both
    assert [p["photo_uid"] for p in msg["photos"]] == before["photo_uids"]
    pics = {p.get_filename() for p in m.iter_attachments() if p.get_content_maintype() == "image"}
    assert pics == {p["attachment"] for p in msg["photos"]} and pics and all(p["attached"] for p in msg["photos"])
    body = m.get_body(preferencelist=("plain",)).get_content()
    assert "Signature: hmac-sha256=" + mailer.signature("shared phrase", data) in body
    assert "Customer billing email: ap@dental.example" in body and "Message format: 2" in body
    # remembered on the place for next time
    assert c.get(f"/api/stops/{s['id']}/invoice-request").json["billing_email"] == "ap@dental.example"
    # the daily summary now marks the stop as invoiced, with the request's message_id
    after = next(x for x in v2(c)["days"][0]["stops"] if x["stop_uid"] == s["uid"])
    assert after["invoice_request"]["message_id"] == msg["message_id"] and after["invoice_request"]["revision"] == 1
    assert after["invoice_request"]["billed_through"] == msg["billed_through"]          # the same, with seconds
    # sent again: revision 2 supersedes 1
    c.post(f"/api/stops/{s['id']}/invoice-request", json={})
    _, data2 = attachment(sent[1], ".json")
    msg2 = json.loads(data2)
    assert sent[1]["Subject"].endswith("(update 2)") and msg2["revision"] == 2 and msg2["supersedes"] == msg["message_id"]
    # a bad billing email is refused before anything is sent
    r = c.post(f"/api/stops/{s['id']}/invoice-request", json={"invoice_to_email": "not an email"})
    assert r.status_code == 400 and len(sent) == 2


def test_invoice_request_format_1_unchanged_plus_billing_email(sample):
    c, sent, _ = sample
    s = tm_stop(c)
    c.post(f"/api/stops/{s['id']}/invoice-request", json={"invoice_to_email": "ap@dental.example"})
    _, data = attachment(sent[0], ".json")
    req = json.loads(data)
    assert req["type"] == "jobhub_invoice_request" and req["schema_version"] == "1"
    assert req["invoice_to_email"] == "ap@dental.example" and "labor" in req


# ---- the late notice -------------------------------------------------------------------------------------------
def test_late_notice_format_2(tmp_path, monkeypatch):
    from tests.test_late import setup
    conn, cfg, now, live, search = setup(tmp_path, monkeypatch, start_in_min=5)
    cfg = replace(cfg, message_format="2", report_signing_key="k")
    known.create_site(conn, {"name": "Beta Plumbing", "kind": "client", "lat": 34.0, "lon": -118.0,
                             "line1": "9 Oak Ave", "line2": "Suite 4", "city": "Testville", "state": "ca", "zip": "90001",
                             "hubspot_company_id": "123456789"})
    msgs = []
    monkeypatch.setattr(mailer, "send", lambda cfg, msg: msgs.append(msg))
    late.send(conn, cfg, D, "ev1@1", now, live, search, note="Traffic")
    m = msgs[0]
    assert m["Subject"] == f"JobHub Late Notice {D} – Beta Plumbing"
    name, data = attachment(m, ".json")
    assert name.startswith(f"jobhub-late-notice-{D}-") and name.endswith("-r1.json")
    n = json.loads(data)
    check_format(n)
    assert n["type"] == "late_notice" and n["subject_key"] == "ev1@1" and n["stop_uid"] == ""
    assert n["customer"]["hubspot_company_id"] == "123456789"
    assert n["customer"]["service_address"]["line2"] == "Suite 4" and n["customer"]["service_address"]["state"] == "CA"
    assert n["job"]["hubspot_ticket_id"] == "" and n["job"]["tech_typed_reference"] == "77"    # a short number
    assert n["job"]["qbo_estimate_number"] == "2210" and n["contact_from_calendar"] == "Jane 555-0100"
    a = n["appointment"]
    assert a["calendar_event_uid"] == "ev1@1" and a["minutes_late"] >= 10 and a["eta_method"] in ("road_route", "estimate")
    assert n["note_for_customer"] == "Traffic"
    late.send(conn, cfg, D, "ev1@1", now, live, search)
    n2 = json.loads(attachment(msgs[1], ".json")[1])
    assert n2["revision"] == 2 and n2["supersedes"] == n["message_id"] and msgs[1]["Subject"].endswith("(update 2)")


# ---- groundwork ------------------------------------------------------------------------------------------------
def test_ticket_rule():
    assert message_v2.ticket_fields("362636074707") == ("362636074707", "")
    assert message_v2.ticket_fields("#1043") == ("", "1043")
    assert message_v2.ticket_fields("12345678") == ("", "12345678")      # 8 digits: not a HubSpot ID
    assert message_v2.ticket_fields("") == ("", "")
    assert message_v2.job("Time & Material", "1043", "continue", "")["ticket_action"] == "continue"
    assert message_v2.job("Time & Material", "362636074707", "", "")["ticket_action"] == "use"


def test_money_and_quantity():
    assert message_v2.money(0.45) == "0.45" and message_v2.money(63) == "63.00" and message_v2.money(None) == ""
    assert message_v2.money(0.215, unit=True) == "0.215" and message_v2.money(0.2150, unit=True) == "0.215"
    assert message_v2.money(4.796, unit=True) == "4.796" and message_v2.money(2.5, unit=True) == "2.50"
    assert message_v2.qty(140.0) == 140 and message_v2.qty(2.5) == 2.5


def test_line_uids_survive_merge_and_undo(sample):
    c, _, _ = sample
    stops = c.get(f"/api/day/{D}").json["stops"]
    i = next(k for k, s in enumerate(stops) if s["parts"])
    a, b = stops[i], stops[i + 1]
    uids = [p["uid"] for p in a["parts"]]
    assert uids and all(UUID.match(u) for u in uids)
    photo_uids = [p["uid"] for p in a["photos"]]
    c.post("/api/stops/merge", json={"ids": [a["id"], b["id"]]})
    merged = next(s for s in c.get(f"/api/day/{D}").json["stops"] if s["id"] == a["id"])
    assert set(uids) <= {p["uid"] for p in merged["parts"]}
    c.post(f"/api/day/{D}/undo")
    back = next(s for s in c.get(f"/api/day/{D}").json["stops"] if s["id"] == a["id"])
    assert [p["uid"] for p in back["parts"]] == uids and [p["uid"] for p in back["photos"]] == photo_uids


def test_waiting_split_and_old_value(tmp_path):
    from tests.test_export import make
    c = make(tmp_path)
    s = c.get(f"/api/day/{D}").json["stops"][0]
    c.patch(f"/api/stops/{s['id']}", json={"stop_type": "job", "business_name": "Acme", "work_category": "Time & Material",
                                          "notes": "Ordered the part."})
    r = c.post(f"/api/stops/{s['id']}/finish", json={"visit_result": "waiting_customer"})
    st = next(x for x in r.json["stops"] if x["id"] == s["id"])
    assert st["visit_result"] == "waiting_customer"
    assert [f["text"] for f in st["follow_ups"]] == ["Waiting on customer"]
    assert c.get(f"/api/export.json?from={D}").json["days"][0]["stops"][0]["visit_result"] == "Waiting for customer"
    # an answer saved before the split ("waiting") reads as waiting for parts
    conn = db.connect(str(tmp_path))
    conn.execute("UPDATE stops SET visit_result='waiting' WHERE id=?", (s["id"],)); conn.commit(); conn.close()
    conn = db.connect(str(tmp_path))       # the migration runs on connect
    assert conn.execute("SELECT visit_result FROM stops WHERE id=?", (s["id"],)).fetchone()[0] == "waiting_parts"
    assert c.patch(f"/api/stops/{s['id']}", json={"visit_result": "waiting"}).json["stops"][0]["visit_result"] == "waiting_parts"


@pytest.mark.parametrize("text,parts", [
    ("650 Town Center Drive, Suite 100, Costa Mesa, CA 92626", ("650 Town Center Drive", "Suite 100", "Costa Mesa", "CA", "92626")),
    ("650 Town Center Dr #100, Costa Mesa, CA 92626, USA", ("650 Town Center Dr", "#100", "Costa Mesa", "CA", "92626")),
    ("12 Main St, Irvine, California, 92618", ("12 Main St", "", "Irvine", "CA", "92618")),
    ("12 Main St Ste B-2, Irvine, CA", ("12 Main St", "Ste B-2", "Irvine", "CA", "")),
    ("9 Oak Ave, Testville", ("9 Oak Ave", "", "Testville", "", "")),
    ("Somewhere vague", ("Somewhere vague", "", "", "", "")),
])
def test_address_split(text, parts):
    assert tuple(address.split(text).values()) == parts


def test_place_address_parts_ids_and_billing_email(tmp_path):
    from tests.test_export import make
    c = make(tmp_path)
    p = c.post("/api/places", json={"name": "Bohm", "kind": "client", "lat": 33.69, "lon": -117.88,
                                    "address": "650 Town Center Drive, Costa Mesa, CA 92626"}).json
    pid = p["id"] if "id" in p else next(x["id"] for x in c.get("/api/places").json["places"] if x["name"] == "Bohm")
    site = next(x for x in c.get("/api/places").json["places"] if x["id"] == pid)
    assert site["address_parts"]["city"] == "Costa Mesa" and site["address_parts"]["line2"] == ""
    r = c.patch(f"/api/places/{pid}", json={"line1": "650 Town Center Drive", "line2": "Suite 100", "city": "Costa Mesa",
                                            "state": "ca", "zip": "92626", "qbo_customer_id": "58",
                                            "billing_email": "ap@bohm.example"})
    assert r.status_code == 200, r.json
    assert r.json["address"] == "650 Town Center Drive, Suite 100, Costa Mesa, CA 92626"
    assert r.json["address_parts"]["line2"] == "Suite 100" and r.json["qbo_customer_id"] == "58"
    assert r.json["billing_email"] == "ap@bohm.example"
    assert c.patch(f"/api/places/{pid}", json={"billing_email": "nope"}).status_code == 400


def test_lunch_at_the_same_site_is_a_break(tmp_path):
    from tests.test_lunch_cases import JOB, build
    c = build(tmp_path, JOB)
    s = c.get(f"/api/export.json?from={D}&format=2").json["days"][0]["stops"][0]
    assert s["time_on_site_minutes"] == 180
    assert s["breaks"] == [{"kind": "lunch", "start": s["breaks"][0]["start"], "end": s["breaks"][0]["end"],
                            "minutes": 60, "paid": True, "billable": False}]
    assert s["breaks"][0]["start"].startswith(f"{D}T10:00:") and s["breaks"][0]["end"].startswith(f"{D}T11:00:")


def test_invoice_format_2_still_on_site(tmp_path, monkeypatch):
    from tests.test_detect import CFG
    from tests.test_export import make
    c = make(tmp_path)
    s = c.get(f"/api/day/{D}").json["stops"][-1]
    c.patch(f"/api/stops/{s['id']}", json={"stop_type": "job", "business_name": "Beta", "work_category": "Time & Material",
                                          "notes": "Fixed it.", "ticket": "1043"})
    c.post(f"/api/stops/{s['id']}/finish", json={"visit_result": "complete"})
    conn = db.connect(str(tmp_path))
    cfg = replace(CFG, data_dir=str(tmp_path))
    import app.service as svc
    now = s["depart_ts"] + 5 * 60
    monkeypatch.setattr(svc.time, "time", lambda: now)
    msg, _, _ = invoice_request.build_v2(conn, cfg, s["id"], now)
    check_format(msg)
    assert msg["still_on_site"] is True and msg["billed_through"] == message_v2.iso(now, cfg.app_tz)
    assert msg["stop"]["departure"] == msg["billed_through"]
    req, _, _ = invoice_request.build(conn, cfg, s["id"], now)
    assert msg["stop"]["time_on_site_minutes"] == req["labor"]["time_on_site_minutes"]
    assert msg["stop"]["job"]["hubspot_ticket_id"] == "" and msg["stop"]["job"]["tech_typed_reference"] == "1043"


# ---- fixes after the agent's sample review (2026-11-24) ------------------------------------------------------------
def test_calendar_appointment_kept_on_the_stop_and_its_ticket(tmp_path, monkeypatch):
    from app import calendar_feed
    from tests.test_export import make
    c = make(tmp_path, calendar_url="https://x/cal.ics")
    s = c.get(f"/api/day/{D}").json["stops"][0]
    ev = {"uid": "AAMk1@1", "start_ts": s["arrive_ts"], "end_ts": s["depart_ts"], "title": "Onsite", "name": "Acme",
          "location": "12 Main St", "ticket": "362600000002", "contact": "Front desk 555-0142", "estimate": ""}
    monkeypatch.setattr(calendar_feed, "events_for_day", lambda cfg, date: [ev])
    c.patch(f"/api/stops/{s['id']}", json={"stop_type": "job", "business_name": "Acme", "work_category": "Time & Material",
                                          "notes": "Done.", "ticket_action": "continue"})
    c.post(f"/api/stops/{s['id']}/finish", json={"visit_result": "complete"})
    st = v2(c)["days"][0]["stops"][0]
    assert st["calendar_event_uid"] == "AAMk1@1" and st["customer"]["site_contact"] == "Front desk 555-0142"
    assert st["job"]["hubspot_ticket_id"] == "362600000002" and st["job"]["ticket_action"] == "use"
    # the published calendar later drops the day: the stop keeps the appointment
    monkeypatch.setattr(calendar_feed, "events_for_day", lambda cfg, date: [])
    st = v2(c)["days"][0]["stops"][0]
    assert st["calendar_event_uid"] == "AAMk1@1" and st["job"]["hubspot_ticket_id"] == "362600000002"
    # a real ticket typed on the stop wins; a short calendar number is only a reference
    c.patch(f"/api/stops/{s['id']}", json={"ticket": "999888777666"})
    assert v2(c)["days"][0]["stops"][0]["job"]["hubspot_ticket_id"] == "999888777666"
    c.patch(f"/api/stops/{s['id']}", json={"ticket": ""})
    monkeypatch.setattr(calendar_feed, "events_for_day", lambda cfg, date: [dict(ev, ticket="1043")])
    j = v2(c)["days"][0]["stops"][0]["job"]
    assert j["hubspot_ticket_id"] == "" and j["tech_typed_reference"] == "1043" and j["ticket_action"] == "continue"


def test_consumable_has_no_price_only_cost(sample):
    c, _, _ = sample
    zt = next(ln for s in v2(c)["days"][0]["stops"] for ln in s["lines"] if ln["kind"] == "consumable")
    assert zt["unit_price"] == "" and zt["amount"] == "" and zt["unit_cost"] == "4.00" and zt["billable"] is False


def exif_jpeg(when):
    import io
    from PIL import Image
    im = Image.new("RGB", (40, 30), "white")
    ex = Image.Exif()
    ex.get_ifd(0x8769)[0x9003] = when
    out = io.BytesIO(); im.save(out, "JPEG", exif=ex)
    return out.getvalue()


def test_photo_taken_at(sample):
    import io
    c, _, _ = sample
    s = next(x for x in c.get(f"/api/day/{D}").json["stops"] if x["business_name"] == "Sample Medical Clinic")
    up = lambda raw, **f: c.post(f"/api/stops/{s['id']}/photos", content_type="multipart/form-data",
                                 data={"kind": "photo", "file": (io.BytesIO(raw), "p.jpg"), **f})
    up(exif_jpeg("2026:11:18 13:05:09"))                                  # full size: the file still has it
    up(exif_jpeg("2026:11:18 13:05:09"), taken="2026:11:18 13:40:00")    # the phone read it before shrinking
    from PIL import Image
    import io as _io
    plain = _io.BytesIO(); Image.new("RGB", (40, 30)).save(plain, "JPEG")
    up(plain.getvalue())                                                  # no capture time at all
    pics = [p for p in v2(c)["days"][0]["photos"] if p["stop_uid"] == s["uid"]]
    assert [p["taken_at"] for p in pics] == ["2026-11-18T13:05:09-08:00", "2026-11-18T13:40:00-08:00", ""]
    # the stored picture itself still has no metadata
    pid = next(x for x in c.get(f"/api/day/{D}").json["stops"] if x["id"] == s["id"])["photos"][0]["id"]
    assert not Image.open(_io.BytesIO(c.get(f"/api/photos/{pid}.jpg").data)).getexif()


def test_bodies_and_receipts_for_billed_expenses(sample):
    c, sent, data_dir = sample
    setting(data_dir, "message_format", "2")
    s = tm_stop(c)
    parking = next(p for p in s["parts"] if p["name"] == "Parking")
    c.patch(f"/api/stop-parts/{parking['id']}", json={"billable": True})
    c.post(f"/api/stops/{s['id']}/invoice-request", json={"pdf": False, "photos": False})
    m = sent[0]
    pics = [p.get_filename() for p in m.iter_attachments() if p.get_content_maintype() == "image"]
    msg = json.loads(attachment(m, ".json")[1])
    receipt = next(p for p in msg["photos"] if p["kind"] == "receipt")
    assert pics == [receipt["attachment"]] and receipt["attached"]
    assert not next(p for p in msg["photos"] if p["kind"] == "photo")["attached"]
    body = m.get_body(preferencelist=("plain",)).get_content()
    assert f"Message format: 2\nRevision: 1\nMessage ID: {msg['message_id']}\n" in body
    assert "Parts (JobHub prices; the invoice uses QuickBooks prices):" in body
    # the daily summary body names only what is attached
    c.post(f"/api/export/email?from={D}", json={"json": True, "pdf": False})
    body = sent[1].get_body(preferencelist=("plain",)).get_content()
    assert "The JSON attachment holds every field." in body and "PDF" not in body
    assert "Message format: 2\nRevision: 1\nMessage ID: " in body


def test_late_notice_body_is_neutral(tmp_path, monkeypatch):
    from tests.test_late import setup
    conn, cfg, now, live, search = setup(tmp_path, monkeypatch, start_in_min=5)
    msgs = []
    monkeypatch.setattr(mailer, "send", lambda cfg, msg: msgs.append(msg))
    late.send(conn, replace(cfg, message_format="2"), D, "ev1@1", now, live, search)
    body = msgs[0].get_body(preferencelist=("plain",)).get_content()
    assert "let the customer know" not in body and "Message format: 2\nRevision: 1\nMessage ID: " in body


def test_revision_lists_removed_and_merged(sample):
    c, sent, data_dir = sample
    c.post(f"/api/export/email?from={D}", json={"json": True, "format": "2"})
    first = json.loads(attachment(sent[0], ".json")[1])["days"][0]
    assert first["removed_stops"] == [] and first["removed_line_uids"] == [] and first["removed_follow_up_uids"] == []
    stops = c.get(f"/api/day/{D}").json["stops"]
    clinic = next(s for s in stops if s["business_name"] == "Sample Medical Clinic")
    vendor = next(s for s in stops if s["business_name"] == "Sample Supply House")
    cable = next(p for p in clinic["parts"] if p["name"] == "Cat 6 cable")
    jack = next(p for p in clinic["parts"] if p["name"].startswith("Keystone"))
    fu = clinic["follow_ups"][0]
    c.patch(f"/api/stop-parts/{cable['id']}", json={"qty": 80})          # changed: same line_uid, new quantity
    c.delete(f"/api/stop-parts/{jack['id']}")                           # removed line
    c.delete(f"/api/follow-ups/{fu['id']}")                             # removed follow-up
    c.delete(f"/api/stops/{vendor['id']}")                              # removed stop (its stock lines go with it)
    c.post(f"/api/export/email?from={D}", json={"json": True, "format": "2"})
    msg = json.loads(attachment(sent[1], ".json")[1])
    day = msg["days"][0]
    assert msg["revision"] == 2 and sent[1]["Subject"].endswith("(update 2)")
    assert day["removed_stops"] == [{"stop_uid": vendor["uid"], "merged_into": None}]
    assert jack["uid"] in day["removed_line_uids"] and cable["uid"] not in day["removed_line_uids"]
    assert {b["uid"] for b in vendor["bought"]} <= set(day["removed_line_uids"])
    assert day["removed_follow_up_uids"] == [fu["uid"]]
    cl = next(s for s in day["stops"] if s["stop_uid"] == clinic["uid"])
    assert next(ln for ln in cl["lines"] if ln["line_uid"] == cable["uid"])["quantity"] == 80
    # a merge: both stops were in the last send; the second one's lines now sit in the first -> merged_into
    c.post(f"/api/day/{D}/undo")
    stops = c.get(f"/api/day/{D}").json["stops"]
    i = next(k for k, s in enumerate(stops) if s["business_name"] == "Sample Dental Office")
    a, b = stops[i], stops[i + 1]
    c.patch(f"/api/stops/{b['id']}", json={"stop_type": "job", "business_name": "Sample Dental Office",
                                          "work_category": "Time & Material", "notes": "Came back for a cable."})
    c.post(f"/api/stops/{b['id']}/parts", json={"name": "Keystone jack Cat 6 - white", "qty": 1})
    c.post(f"/api/stops/{b['id']}/finish", json={"visit_result": "complete"})
    c.post(f"/api/export/email?from={D}", json={"json": True, "format": "2"})
    assert b["uid"] in {x["stop_uid"] for x in json.loads(attachment(sent[2], ".json")[1])["days"][0]["stops"]}
    c.post("/api/stops/merge", json={"ids": [a["id"], b["id"]]})
    c.post(f"/api/export/email?from={D}", json={"json": True, "format": "2"})
    day = json.loads(attachment(sent[3], ".json")[1])["days"][0]
    assert day["removed_stops"] == [{"stop_uid": b["uid"], "merged_into": a["uid"]}]
    assert day["removed_line_uids"] == []                                  # the lines moved, nothing was removed


def test_invoice_revision_lists_removed_lines(sample):
    c, sent, data_dir = sample
    setting(data_dir, "message_format", "2")
    s = tm_stop(c)
    c.post(f"/api/stops/{s['id']}/invoice-request", json={})
    jack = next(p for p in s["parts"] if p["name"].startswith("Keystone"))
    c.delete(f"/api/stop-parts/{jack['id']}")
    c.post(f"/api/stops/{s['id']}/invoice-request", json={})
    msg = json.loads(attachment(sent[1], ".json")[1])
    assert msg["revision"] == 2 and msg["removed_line_uids"] == [jack["uid"]]
