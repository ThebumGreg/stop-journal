"""Build a made-up day that uses every report field and save what the work AI agent would receive.

    python scripts/make_sample_report.py [out_dir]       (default docs/sample-report)

Writes the JSON and PDF report, the email exactly as Email report would send it (.eml with attachments, via a fake
mail server: nothing is sent), and the email body as text. All names, addresses and coordinates are invented
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


def picture(lines, w=600, h=800):
    img = Image.new("RGB", (w, h), "white")
    dr = ImageDraw.Draw(img)
    for i, t in enumerate(lines):
        dr.text((30, 30 + i * 28), t, fill="black")
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return buf.getvalue()


def ok(r):
    assert r.status_code < 400, (r.status_code, r.get_json())
    return r.get_json()


def build(data_dir):
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
    c = create_app(cfg, client=sample.ListRecorder(d.points())).test_client()

    ok(c.post("/api/places", json={"name": "Home", "kind": "home", "lat": HOME[0], "lon": HOME[1]}))
    ok(c.post("/api/places", json={"name": "Sample Supply House", "kind": "vendor", "lat": SUPPLY[0], "lon": SUPPLY[1],
                                   "address": "200 Vendor Way, Los Angeles, CA 90012"}))
    for name, ll, addr, hs in (("Sample Dental Office", DENTAL, "100 Example Ave, Los Angeles, CA 90012", "18200000001"),
                               ("Sample Medical Clinic", CLINIC, "300 Example Blvd, Los Angeles, CA 90013", "18200000002")):
        ok(c.post("/api/places", json={"name": name, "kind": "client", "lat": ll[0], "lon": ll[1], "address": addr}))
        pid = next(p["id"] for p in ok(c.get("/api/places"))["places"] if p["name"] == name)
        ok(c.patch(f"/api/places/{pid}", json={"hubspot_company_id": hs}))
    stops = [s for s in ok(c.get(f"/api/day/{D}"))["stops"] if s["kind"] == "stop"]
    assert len(stops) == 7, [(s["arrive"], s["depart"]) for s in stops]
    home1, dental, gas, supply, deli, clinic, home2 = stops

    # catalog: a tracked part, a part, a consumable
    ok(c.post("/api/parts", json={"name": "Cat 6 cable", "sku": "C6-1000-BLU", "unit": "ft", "unit_cost": 0.45,
                                  "billable": True, "tracked": True, "on_hand": 900, "vendor": "Sample Supply House"}))
    ok(c.post("/api/parts", json={"name": "Keystone jack Cat 6 - white", "sku": "KJ-C6-W", "unit": "ea",
                                  "unit_cost": 6.50, "billable": True}))
    ok(c.post("/api/parts", json={"name": "Zip ties 8 in", "unit": "pack", "unit_cost": 4.00, "type": "consumable",
                                  "billable": False}))
    ok(c.post("/api/parts", json={"name": "Parking", "type": "expense"}))

    ok(c.patch(f"/api/stops/{home1['id']}", json={"business_name": "Home", "stop_type": "personal"}))
    ok(c.patch(f"/api/stops/{home2['id']}", json={"business_name": "Home", "stop_type": "personal"}))
    ok(c.post(f"/api/day/{D}/mark", json={"which": "start", "time": "07:38"}))

    # Job 1: complete, hourly, parts + consumable + expense, receipt and photo
    sid = dental["id"]
    ok(c.patch(f"/api/stops/{sid}", json={
        "stop_type": "job", "business_name": "Sample Dental Office", "address": "100 Example Ave, Los Angeles, CA 90012",
        "work_category": "Time & Material", "ticket": "https://app.hubspot.com/contacts/1234567/ticket/24681357913",
        "notes": "Ran two Cat 6 drops from the network rack to the front desk.\n"
                 "Terminated both ends with keystone jacks and tested: both pass.\n"
                 "Relabeled the patch panel ports 12 and 13."}))
    ok(c.post(f"/api/stops/{sid}/parts", json={"name": "Cat 6 cable", "qty": 140}))
    ok(c.post(f"/api/stops/{sid}/parts", json={"name": "Keystone jack Cat 6 - white", "qty": 4}))
    ok(c.post(f"/api/stops/{sid}/parts", json={"name": "Zip ties 8 in", "qty": 1}))
    ok(c.post(f"/api/stops/{sid}/parts", json={"name": "Parking", "qty": 1, "unit_cost": 12.00}))
    ok(c.post(f"/api/stops/{sid}/photos", data={"kind": "receipt", "caption": "Parking receipt",
                                                "file": (io.BytesIO(picture(["SAMPLE PARKING", "Total $12.00"])), "r.jpg")},
              content_type="multipart/form-data"))
    ok(c.post(f"/api/stops/{sid}/photos", data={"kind": "photo", "caption": "Patch panel after relabel",
                                                "file": (io.BytesIO(picture(["SAMPLE PHOTO", "Patch panel"])), "p.jpg")},
              content_type="multipart/form-data"))
    ok(c.post(f"/api/stops/{sid}/finish", json={"visit_result": "complete"}))

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


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                                              "docs", "sample-report")
    os.makedirs(out, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        c, cfg = build(tmp)
        sent = []

        class Fake:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def starttls(self, **k): pass
            def login(self, *a): pass
            def send_message(self, m): sent.append(m)

        mailer.smtplib.SMTP = Fake
        conn = db.connect(tmp)
        for k, v in {"mail_to": "agent@work.example", "smtp_user": "tech@gmail.example", "smtp_password": "x"}.items():
            conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)", (k, v))
        conn.commit(); conn.close()
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
