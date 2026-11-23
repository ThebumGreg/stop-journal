"""Photos and receipts: stored in the database, cleaned of metadata, listed in the report and the PDF."""
import io

from PIL import Image

from tests.test_export import D, make, pdf_text


def jpeg_with_gps(w=3000, h=2000):
    im = Image.new("RGB", (w, h), (200, 30, 30))
    exif = Image.Exif()
    exif[0x0110] = "Test Phone"               # model
    exif[0x8825] = {2: (37.0, 46.0, 30.0)}    # GPS IFD: latitude
    b = io.BytesIO()
    im.save(b, "JPEG", exif=exif.tobytes())
    return b.getvalue()


def upload(c, sid, data, kind="receipt"):
    return c.post(f"/api/stops/{sid}/photos", data={"file": (io.BytesIO(data), "x.jpg"), "kind": kind},
                  content_type="multipart/form-data")


def test_upload_shrinks_and_strips_metadata(tmp_path):
    c = make(tmp_path)
    sid = c.get(f"/api/day/{D}").json["stops"][0]["id"]
    r = upload(c, sid, jpeg_with_gps())
    assert r.status_code == 200, r.json
    p = next(s for s in r.json["stops"] if s["id"] == sid)["photos"][0]
    assert p["kind"] == "receipt"
    img = c.get(f"/api/photos/{p['id']}.jpg")
    assert img.mimetype == "image/jpeg"
    out = Image.open(io.BytesIO(img.data))
    assert max(out.size) == 1600
    assert not out.getexif() and b"Test Phone" not in img.data


def test_bad_uploads_are_refused(tmp_path):
    c = make(tmp_path)
    sid = c.get(f"/api/day/{D}").json["stops"][0]["id"]
    assert upload(c, sid, b"not a picture").status_code == 400
    assert upload(c, sid, b"").status_code == 400
    assert upload(c, sid, jpeg_with_gps(10, 10), kind="selfie").status_code == 400


def test_photos_in_report_only_for_confirmed_and_fuel(tmp_path):
    c = make(tmp_path)
    ids = [s["id"] for s in c.get(f"/api/day/{D}").json["stops"]]
    c.patch(f"/api/stops/{ids[0]}", json={"status": "Confirmed", "business_name": "Acme", "notes": "x"})
    c.patch(f"/api/stops/{ids[1]}", json={"stop_type": "gas", "business_name": "Corner Fuel"})
    c.patch(f"/api/stops/{ids[2]}", json={"stop_type": "personal"})
    upload(c, ids[0], jpeg_with_gps(800, 600), "photo")
    upload(c, ids[1], jpeg_with_gps(600, 800))
    upload(c, ids[2], jpeg_with_gps(600, 800))           # personal: never in the report
    j = c.get(f"/api/export.json?from={D}").json
    labels = [p["label"] for p in j["days"][0]["photos"]]
    assert labels[0].startswith("Photo: Stop 1, Acme")
    assert labels[1].startswith("Receipt: Fuel stop, Corner Fuel")
    assert len(labels) == 2
    assert len(j["days"][0]["stops"][0]["photos"]) == 1
    text = pdf_text(c.get(f"/api/export.pdf?from={D}"))
    assert "Receipts and photos" in text and "Receipt: Fuel stop, Corner Fuel" in text and "1 attached" in text
    text = pdf_text(c.get(f"/api/export.pdf?from={D}&omit=photos"))
    assert "Receipt: Fuel stop" not in text
    assert "photos" not in c.get(f"/api/export.json?from={D}&omit=photos").json["days"][0]


def test_photos_survive_delete_undo_merge_and_redetect(tmp_path):
    c = make(tmp_path)
    ids = [s["id"] for s in c.get(f"/api/day/{D}").json["stops"]]
    upload(c, ids[1], jpeg_with_gps(50, 50))
    c.post("/api/stops/merge", json={"ids": [ids[0], ids[1]]})
    st = c.get(f"/api/day/{D}").json["stops"]
    assert len(st[0]["photos"]) == 1                     # moved to the merged stop
    c.post(f"/api/day/{D}/undo")
    c.post(f"/api/day/{D}/redetect", json={})
    st = c.get(f"/api/day/{D}").json["stops"]
    assert sum(len(s["photos"]) for s in st) == 1        # re-attached to the new stop at the same time
    pid = next(p["id"] for s in st for p in s["photos"])
    c.delete(f"/api/photos/{pid}")
    assert c.get(f"/api/photos/{pid}.jpg").status_code == 404


def test_files_are_stored_by_year_month_day(tmp_path):
    c = make(tmp_path)
    sid = c.get(f"/api/day/{D}").json["stops"][0]["id"]
    upload(c, sid, jpeg_with_gps(40, 40))
    files = list((tmp_path / "photos" / "2026" / "11" / "18").glob("*.jpg"))
    assert len(files) == 1 and files[0].name.endswith("-receipt.jpg")
    pid = c.get(f"/api/day/{D}").json["stops"][0]["photos"][0]["id"]
    c.delete(f"/api/photos/{pid}")
    assert not files[0].exists()


def test_pictures_in_the_database_move_to_files(tmp_path):
    from app import db
    c = make(tmp_path)
    sid = c.get(f"/api/day/{D}").json["stops"][0]["id"]
    conn = db.connect(str(tmp_path))
    conn.execute("INSERT INTO photos(stop_id,kind,created,data) VALUES (?,?,?,?)", (sid, "photo", 1, b"\xff\xd8old"))
    conn.commit(); conn.close()
    conn = db.connect(str(tmp_path))          # the move happens on connect
    r = conn.execute("SELECT path, length(data) n FROM photos").fetchone()
    assert r["path"].startswith("photos/2026/11/18/") and r["n"] == 0
    assert (tmp_path / r["path"]).read_bytes() == b"\xff\xd8old"


def test_zip_has_pdf_json_and_dated_photos(tmp_path):
    import json, zipfile
    c = make(tmp_path)
    ids = [s["id"] for s in c.get(f"/api/day/{D}").json["stops"]]
    c.patch(f"/api/stops/{ids[0]}", json={"status": "Confirmed", "business_name": "Acme Hardware & Co.", "notes": "x"})
    c.patch(f"/api/stops/{ids[1]}", json={"stop_type": "gas", "business_name": "Corner Fuel"})
    upload(c, ids[0], jpeg_with_gps(80, 60))
    upload(c, ids[0], jpeg_with_gps(80, 60), "photo")
    upload(c, ids[1], jpeg_with_gps(60, 80))
    r = c.get(f"/api/export.zip?from={D}")
    assert r.mimetype == "application/zip"
    assert r.headers["Content-Disposition"] == f"attachment; filename=jobhub-daily-summary-{D}.zip"
    z = zipfile.ZipFile(io.BytesIO(r.data))
    names = sorted(z.namelist())
    assert f"jobhub-daily-summary-{D}.pdf" in names and f"jobhub-daily-summary-{D}.json" in names
    pics = [n for n in names if n.startswith("photos/")]
    assert "photos/2026/11/18/stop-1-acme-hardware-co-receipt-1.jpg" in pics
    assert "photos/2026/11/18/stop-1-acme-hardware-co-photo-2.jpg" in pics
    assert any(n.startswith("photos/2026/11/18/fuel-") and n.endswith("corner-fuel-receipt-1.jpg") for n in pics)
    rep = json.loads(z.read(f"jobhub-daily-summary-{D}.json"))
    assert sorted(p["file"] for p in rep["days"][0]["photos"]) == sorted(pics)   # the JSON names match the zip
    assert Image.open(io.BytesIO(z.read(pics[0]))).format == "JPEG"
    z2 = zipfile.ZipFile(io.BytesIO(c.get(f"/api/export.zip?from={D}&omit=photos").data))
    assert not [n for n in z2.namelist() if n.startswith("photos/")]
