"""Build a made-up day that uses every report field and save what the work AI agent would receive.

    python scripts/make_sample_report.py [out_dir]       (default docs/sample-report)

Writes the JSON and PDF report, the email exactly as Email report would send it (.eml with attachments, via a fake
mail server: nothing is sent), and the email body as text; the invoice request; and in v2/ the same day, invoice
request and a late notice in message format 2. All names, addresses and coordinates are invented
(downtown Los Angeles test coordinates from app/sample.py). Re-run after report changes to refresh the sample.
"""
import io
import os
import sys
import tempfile
from dataclasses import replace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image, ImageDraw  # noqa: E402

from app import create_app, db, mailer, sample  # noqa: E402
from app.config import Config  # noqa: E402

D = "2026-11-18"
HOME = sample.SITE_A
DENTAL = sample.offset(*HOME, 6000, 4000)
GAS = sample.offset(*HOME, 7000, 6500)
SUPPLY = sample.offset(*HOME, 5000, 9000)
DELI = sample.offset(*HOME, 3000, 9500)
CLINIC = sample.offset(*HOME, -3000, 9000)


def picture(lines, w=600, h=800, taken=None):
    """A made-up picture; `taken` ("2026:11:18 08:41:05") is its capture time, as a phone camera writes it."""
    img = Image.new("RGB", (w, h), "white")
    dr = ImageDraw.Draw(img)
    for i, t in enumerate(lines):
        dr.text((30, 30 + i * 28), t, fill="black")
    buf = io.BytesIO()
    ex = Image.Exif()
    if taken:
        ex.get_ifd(0x8769)[0x9003] = taken
    img.save(buf, "JPEG", quality=85, exif=ex)
    return buf.getvalue()


def ok(r):
    assert r.status_code < 400, (r.status_code, r.get_json())
    return r.get_json()


def at(hm):
    """'10:22' on the sample day -> seconds."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return int(datetime.fromisoformat(f"{D}T{hm}").replace(tzinfo=ZoneInfo("America/Los_Angeles")).timestamp())


# the made-up calendar appointment at the clinic (format 2 samples: the summary's stop and the late notice share it)
CLINIC_EVENT = {"uid": "AAMkSample123@1795543200", "title": "Onsite Sample Medical Clinic", "name": "Sample Medical Clinic",
                "location": "300 Example Blvd, Los Angeles, CA 90013", "ticket": "362600000002", "estimate": "2210",
                "contact": "Front desk 555-0142", "category": "Quoted project"}
ON_SITE_SEND = "10:22"         # format 2 sample: Send for invoice while still at the dental office (left 10:30)


def build(data_dir, v2=False):
    """The sample day. v2: the format 2 version: the clinic visit matches a calendar appointment, the keystone jack
    has a real-looking QuickBooks SKU and name, and the dental office is sent for invoice at 10:22 while still there
    (the phone has only sent points up to 10:20 at that moment; the rest of the day comes in afterwards)."""
    d = sample.Day(D)
    t = d.dwell(*HOME, d.at(7), 40)            # 07:00-07:40 home
    t = d.drive(HOME, DENTAL, t, 20)           # 07:40-08:00
    t = d.dwell(*DENTAL, t, 150)               # 08:00-10:30 job 1
    t = d.drive(DENTAL, GAS, t, 12)
    t = d.dwell(*GAS, t, 14)                   # fill-up
    t = d.drive(GAS, SUPPLY, t, 10)
    t = d.dwell(*SUPPLY, t, 25)                # supply house
    t = d.drive(SUPPLY, DELI, t, 8)
    t = d.dwell(*DELI, t, 30)                  # lunch
    t = d.drive(DELI, CLINIC, t, 12)
    t = d.dwell(*CLINIC, t, 140)               # job 2
    t = d.drive(CLINIC, HOME, t, 25)
    d.dwell(*HOME, t, 60)
    cfg = Config(recorder_url="http://sample", recorder_user="u", recorder_device="d", recorder_auth="",
                 app_tz="America/Los_Angeles", places_provider="osm", google_places_key="", stop_min_minutes=10,
                 stop_radius_m=100, stop_max_gap_min=15, stop_merge_gap_min=60, max_acc_m=100, app_password="",
                 data_dir=data_dir, routing_url="", overpass_url="")
    cfg = replace(cfg, tech_name="Sample Technician", tech_email="tech@work.example",
                  followup_owners="Me\nOffice = office@work.example")
    points = d.points()
    if v2:
        cfg = replace(cfg, calendar_url="https://calendar.example/sample.ics")
        mail_settings(data_dir, message_format="2")
    rec = sample.ListRecorder([p for p in points if p["tst"] <= at("10:20")] if v2 else points)
    c = create_app(cfg, client=rec).test_client()

    ok(c.post("/api/places", json={"name": "Home", "kind": "home", "lat": HOME[0], "lon": HOME[1]}))
    ok(c.post("/api/places", json={"name": "Sample Supply House", "kind": "vendor", "lat": SUPPLY[0], "lon": SUPPLY[1],
                                   "address": "200 Vendor Way, Los Angeles, CA 90012"}))
    for name, ll, addr, hs in (("Sample Dental Office", DENTAL, "100 Example Ave, Los Angeles, CA 90012", "18200000001"),
                               ("Sample Medical Clinic", CLINIC, "300 Example Blvd, Los Angeles, CA 90013", "18200000002")):
        ok(c.post("/api/places", json={"name": name, "kind": "client", "lat": ll[0], "lon": ll[1], "address": addr}))
        pid = next(p["id"] for p in ok(c.get("/api/places"))["places"] if p["name"] == name)
        ok(c.patch(f"/api/places/{pid}", json={"hubspot_company_id": hs}))
        if name == "Sample Dental Office":       # the address in parts with a suite, and the QuickBooks customer
            ok(c.patch(f"/api/places/{pid}", json={"line1": "100 Example Ave", "line2": "Suite 210", "city": "Los Angeles",
                                                   "state": "CA", "zip": "90012", "qbo_customer_id": "58"}))

    # catalog: a tracked part, a part, a consumable
    ok(c.post("/api/parts", json={"name": "Cat 6 cable", "sku": "C6-1000-BLU", "unit": "ft", "unit_cost": 0.45,
                                  "billable": True, "tracked": True, "on_hand": 900, "vendor": "Sample Supply House",
                                  "qbo_item": "Cable Cat6 Blue (ft)"}))
    ok(c.post("/api/parts", json={"name": "Keystone jack Cat 6 - white", "sku": "IC1078F6WH" if v2 else "KJ-C6-W",
                                  "unit": "ea", "unit_cost": 6.50, "billable": True,
                                  **({"qbo_item": "Cable CAT6 Insert RJ-45 (White)"} if v2 else {})}))
    ok(c.post("/api/parts", json={"name": "Zip ties 8 in", "unit": "pack", "unit_cost": 4.00, "type": "consumable",
                                  "billable": False}))
    ok(c.post("/api/parts", json={"name": "Parking", "type": "expense"}))

    stops = [s for s in ok(c.get(f"/api/day/{D}"))["stops"] if s["kind"] == "stop"]
    home1, dental = stops[0], stops[1]
    ok(c.patch(f"/api/stops/{home1['id']}", json={"business_name": "Home", "stop_type": "personal"}))
    ok(c.post(f"/api/day/{D}/mark", json={"which": "start", "time": "07:38"}))

    # Job 1: complete, hourly, parts + consumable + expense, receipt and photo
    sid = dental["id"]
    ok(c.patch(f"/api/stops/{sid}", json={
        "stop_type": "job", "business_name": "Sample Dental Office", "address": "100 Example Ave, Suite 210, Los Angeles, CA 90012",
        "work_category": "Time & Material", "ticket": "https://app.hubspot.com/contacts/1234567/ticket/24681357913",
        "notes": "Ran two Cat 6 drops from the network rack to the front desk.\n"
                 "Terminated both ends with keystone jacks and tested: both pass.\n"
                 "Relabeled the patch panel ports 12 and 13."}))
    ok(c.post(f"/api/stops/{sid}/parts", json={"name": "Cat 6 cable", "qty": 140}))
    ok(c.post(f"/api/stops/{sid}/parts", json={"name": "Keystone jack Cat 6 - white", "qty": 4}))
    ok(c.post(f"/api/stops/{sid}/parts", json={"name": "Zip ties 8 in", "qty": 1}))
    # format 2 sample: the parking is billed to the customer, so its receipt goes with the invoice request
    day = ok(c.post(f"/api/stops/{sid}/parts", json={"name": "Parking", "qty": 1, "unit_cost": 12.00}))
    if v2:
        line = next(p for x in day["stops"] if x["id"] == sid for p in x["parts"] if p["name"] == "Parking")
        ok(c.patch(f"/api/stop-parts/{line['id']}", json={"billable": True}))
    ok(c.post(f"/api/stops/{sid}/photos", data={"kind": "receipt", "caption": "Parking receipt",
                                                "file": (io.BytesIO(picture(["SAMPLE PARKING", "Total $12.00"], taken="2026:11:18 08:03:41")), "r.jpg")},
              content_type="multipart/form-data"))
    ok(c.post(f"/api/stops/{sid}/photos", data={"kind": "photo", "caption": "Patch panel after relabel",
                                                "file": (io.BytesIO(picture(["SAMPLE PHOTO", "Patch panel"], taken="2026:11:18 10:12:05")), "p.jpg")},
              content_type="multipart/form-data"))
    ok(c.post(f"/api/stops/{sid}/finish", json={"visit_result": "complete"}))
    if v2:
        import time as clock
        real = clock.time
        clock.time = lambda: at(ON_SITE_SEND)          # still at the dental office: labor runs to the moment of sending
        try:
            ok(c.post(f"/api/stops/{sid}/invoice-request",
                      json={"pdf": False, "photos": False, "invoice_to_email": "billing@dental.example"}))
        finally:
            clock.time = real
        rec.points = points                            # the rest of the day comes in
        ok(c.post(f"/api/day/{D}/sync"))
    stops = [s for s in ok(c.get(f"/api/day/{D}"))["stops"] if s["kind"] == "stop"]
    assert len(stops) == 7, [(s["arrive"], s["depart"]) for s in stops]
    home1, dental, gas, supply, deli, clinic, home2 = stops
    ok(c.patch(f"/api/stops/{home2['id']}", json={"business_name": "Home", "stop_type": "personal"}))

    # Fill-up
    ok(c.patch(f"/api/stops/{gas['id']}", json={"stop_type": "gas", "business_name": "Sample Fuel Station"}))
    ok(c.put(f"/api/stops/{gas['id']}/fuel", json={"gallons": 14.2, "total": 68.10, "odometer": 48213}))

    # Vendor: a purchase as an expense with a note
    ok(c.patch(f"/api/stops/{supply['id']}", json={"stop_type": "vendor", "business_name": "Sample Supply House",
                                                   "notes": "Picked up a 1000 ft box of Cat 6 for the clinic job."}))
    # Bought here: goes into truck stock (report `bought_here`), never billed at the vendor stop
    ok(c.post(f"/api/stops/{supply['id']}/bought", json={"name": "Cat 6 cable", "qty": 1000, "paid": 189.00}))
    ok(c.post(f"/api/stops/{supply['id']}/bought", json={"name": "Keystone jack Cat 6 - white", "qty": 25, "paid": 62.50}))
    ok(c.post(f"/api/stops/{supply['id']}/finish", json={}))

    # Lunch
    ok(c.patch(f"/api/stops/{deli['id']}", json={"stop_type": "lunch", "business_name": "Sample Deli"}))

    # Job 2: quoted project, needs a return visit (follow-up with due date), plus an own follow-up for the office
    sid = clinic["id"]
    ok(c.patch(f"/api/stops/{sid}", json={
        "stop_type": "job", "business_name": "Sample Medical Clinic", "address": "300 Example Blvd, Los Angeles, CA 90013",
        "work_category": "Quoted project", "ticket_action": "continue", "qbo_estimate": "2210",
        "notes": "Mounted the new switch in the IT closet and patched 18 of 24 ports.\n"
                 "Six ports wait on the customer's new patch cords."}))
    ok(c.post(f"/api/stops/{sid}/parts", json={"name": "Cat 6 cable", "qty": 60}))
    ok(c.post(f"/api/stops/{sid}/parts", json={"name": "Keystone jack Cat 6 - white", "qty": 6}))
    ok(c.post(f"/api/stops/{sid}/follow-ups", json={"text": "Send the customer a quote for 6 patch cords",
                                                    "owner": "Office", "due": "2026-11-20"}))
    ok(c.post(f"/api/stops/{sid}/finish", json={"visit_result": "return", "due": "2026-11-24"}))

    ok(c.post(f"/api/day/{D}/end-at-home"))
    return c, cfg


SENT = []


class Fake:
    """A mail server that keeps the messages (nothing is sent)."""
    def __init__(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def starttls(self, **k): pass
    def login(self, *a): pass
    def send_message(self, m): SENT.append(m)


def mail_settings(data_dir, **extra):
    conn = db.connect(data_dir)
    for k, v in {"mail_to": "agent@work.example", "smtp_user": "tech@gmail.example", "smtp_password": "x", **extra}.items():
        conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)", (k, v))
    conn.commit(); conn.close()


def save(out, name, msg, suffix):
    """The attachment of `msg` ending in suffix, saved as out/name."""
    part = next(p for p in msg.iter_attachments() if p.get_filename().endswith(suffix))
    data = part.get_content()
    open(os.path.join(out, name), "wb").write(data if isinstance(data, bytes) else data.encode())
    return part.get_filename()


def body_text(msg):
    return "Subject: " + msg["Subject"] + "\n\n" + msg.get_body(("plain",)).get_content()


def write_v2(out):
    """Message format 2: the invoice request sent while still on site, the daily summary (revision 1), the same day
    after a stop was deleted and a stop's parts changed (revision 2), and a late notice for the clinic appointment."""
    import json
    from app import calendar_feed, late, message_v2
    os.makedirs(out, exist_ok=True)
    ev = dict(CLINIC_EVENT, start_ts=at("12:00"), end_ts=at("14:00"), start="12:00")
    real_events = calendar_feed.events_for_day
    calendar_feed.events_for_day = lambda cfg, date: [ev] if date == D else []
    try:
        with tempfile.TemporaryDirectory() as tmp:
            SENT.clear()
            c, cfg = build(tmp, v2=True)
            inv = SENT.pop(0)
            fn = save(out, "invoice-request-sample.json", inv, ".json")
            open(os.path.join(out, "invoice-request-body.txt"), "w").write(
                body_text(inv) + f"\n[attachment file name: {fn}]\n")
            ok(c.post(f"/api/export/email?from={D}", json={"json": True, "pdf": True, "photos": True}))
            day = SENT.pop(0)
            fn = save(out, "daily-summary-sample.json", day, ".json")
            files = [p.get_filename() for p in day.iter_attachments()]
            open(os.path.join(out, "daily-summary-body.txt"), "w").write(
                body_text(day) + "\n[attachment file names: " + "; ".join(files) + "]\n")
            # revision 2: the supply house stop deleted, the clinic's cable changed 60 -> 80 ft, its jacks removed
            stops = ok(c.get(f"/api/day/{D}"))["stops"]
            supply = next(x for x in stops if x["business_name"] == "Sample Supply House")
            clinic = next(x for x in stops if x["business_name"] == "Sample Medical Clinic")
            ok(c.delete(f"/api/stops/{supply['id']}"))
            ok(c.patch(f"/api/stop-parts/{next(p['id'] for p in clinic['parts'] if p['name'] == 'Cat 6 cable')}",
                       json={"qty": 80}))
            ok(c.delete(f"/api/stop-parts/{next(p['id'] for p in clinic['parts'] if p['name'].startswith('Keystone'))}"))
            ok(c.post(f"/api/export/email?from={D}", json={"json": True, "pdf": False, "photos": False}))
            day2 = SENT.pop(0)
            fn = save(out, "daily-summary-r2-sample.json", day2, ".json")
            open(os.path.join(out, "daily-summary-r2-body.txt"), "w").write(
                body_text(day2) + f"\n[attachment file name: {fn}]\n")
            # a late notice for the clinic appointment (made up: 12:00, arriving 12:21), sent at 11:52
            conn = db.connect(tmp)
            cfg = replace(cfg, message_format="2", mail_to="agent@work.example")
            eta = {"arrive_ts": at("12:21"), "arrive": "12:21 PM", "late_min": 21, "method": "routed"}
            mid = message_v2.new_id()
            n2 = message_v2.late_notice(conn, cfg, ev, eta, at("11:52"), "Stuck in traffic on the 110", "", 1, None, mid)
            open(os.path.join(out, "late-notice-sample.json"), "w").write(json.dumps(n2, indent=2, ensure_ascii=False))
            n1 = late.notice(cfg, ev, eta, at("11:52"), "Stuck in traffic on the 110")
            stem = message_v2.file_stem("late_notice", D, "1200", 1)
            open(os.path.join(out, "late-notice-body.txt"), "w").write(
                "Subject: " + message_v2.subject("late_notice", D, ev["name"], 1) + "\n\n" + late.body(n1, "", mid)
                + f"\n[attachment file name: {stem}.json]\n")
            conn.close()
    finally:
        calendar_feed.events_for_day = real_events


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                                              "docs", "sample-report")
    os.makedirs(out, exist_ok=True)
    mailer.smtplib.SMTP = Fake
    write_v2(os.path.join(out, "v2"))
    with tempfile.TemporaryDirectory() as tmp:
        c, cfg = build(tmp)
        SENT.clear()
        sent = SENT
        mail_settings(tmp)
        # Send for invoice on the Time & Material stop first (as during the day); the day report then shows it
        tm = next(s for s in ok(c.get(f"/api/day/{D}"))["stops"] if s.get("work_category") == "Time & Material")
        ok(c.post(f"/api/stops/{tm['id']}/invoice-request", json={"pdf": True, "photos": False}))
        inv = sent.pop(0)
        for part in inv.iter_attachments():
            fn = part.get_filename()
            if fn.endswith(".json") or fn.endswith(".pdf"):
                data = part.get_content()
                open(os.path.join(out, "invoice-request-sample" + fn[-5 if fn.endswith(".json") else -4:]), "wb").write(
                    data if isinstance(data, bytes) else data.encode())
        open(os.path.join(out, "invoice-request-body.txt"), "w").write(
            "Subject: " + inv["Subject"] + "\n\n" + inv.get_body(("plain",)).get_content())

        chk = ok(c.get(f"/api/export/check?from={D}"))
        name = f"jobhub-daily-summary-{D}"
        open(os.path.join(out, name + ".json"), "wb").write(c.get(f"/api/export.json?from={D}").data)
        open(os.path.join(out, name + ".pdf"), "wb").write(c.get(f"/api/export.pdf?from={D}").data)
        ok(c.post(f"/api/export/email?from={D}", json={"json": True, "pdf": True, "photos": True}))
        open(os.path.join(out, name + ".eml"), "wb").write(bytes(sent[0]))
        open(os.path.join(out, "email-body.txt"), "w").write(
            "Subject: " + sent[0]["Subject"] + "\n\n" + sent[0].get_body(("plain",)).get_content())
    print("Wrote", out, "| export warnings:", chk.get("warnings"))


if __name__ == "__main__":
    main()
