"""Truck → Tools: what you own and what you paid, with receipts; CSV in and out."""
import io

from tests.test_export import make
from tests.test_photos import jpeg_with_gps


def test_tools_crud_files_totals_and_vendor(tmp_path):
    c = make(tmp_path)
    t = c.post("/api/tools", json={"name": "Cordless drill", "make": "DeWalt", "model": "DCD791", "serial": "123",
                                   "purchased": "2026-03-14", "price": "$179.00", "vendor": "Home Depot",
                                   "warranty_until": "2029-03-14"}).json
    assert t["price"] == 179.0 and t["location"] == "truck" and t["under_warranty"] and not t["has_receipt"]
    c.post("/api/tools", json={"name": "Old ladder", "price": "90", "location": "retired", "purchased": "2019-01-02"})
    assert c.post("/api/tools", json={"name": ""}).status_code == 400
    assert c.post("/api/tools", json={"name": "X", "purchased": "someday"}).status_code == 400
    assert c.post("/api/tools", json={"name": "X", "location": "loaned"}).status_code == 400
    d = c.get("/api/tools").json
    assert d["totals"]["count"] == 1 and d["totals"]["value"] == 179.0 and d["totals"]["no_receipt"] == 1
    r = c.post(f"/api/tools/{t['id']}/files", data={"kind": "receipt", "file": (io.BytesIO(jpeg_with_gps(80, 60)), "r.jpg")},
               content_type="multipart/form-data").json
    assert r["has_receipt"] and r["files"][0]["kind"] == "receipt"
    fid = r["files"][0]["id"]
    img = c.get(f"/api/tool-files/{fid}.jpg")
    assert img.mimetype == "image/jpeg" and b"GPS" not in img.data
    assert (tmp_path / "photos" / "tools").is_dir()
    u = c.patch(f"/api/tools/{t['id']}", json={"location": "repair", "notes": "chuck slips"}).json
    assert u["location_label"] == "In for repair"
    v = c.get("/api/vendors/detail?name=Home Depot").json          # a vendor you only bought a tool from
    assert v["tools"][0]["name"] == "Cordless drill" and "Home Depot" in c.get("/api/parts/choices").json["vendors"]
    assert c.delete(f"/api/tool-files/{fid}").json["files"] == []
    assert c.delete(f"/api/tools/{t['id']}").status_code == 200


def test_tools_csv_round_trip(tmp_path):
    c = make(tmp_path)
    tpl = c.get("/api/tools/template.csv").data
    r = c.post("/api/tools/import?dry=1", data={"file": (io.BytesIO(tpl), "t.csv")}, content_type="multipart/form-data").json
    assert r["created"] == 2 and r["dry_run"] and c.get("/api/tools").json["tools"] == []
    r = c.post("/api/tools/import", data={"file": (io.BytesIO(tpl), "t.csv")}, content_type="multipart/form-data").json
    assert r["created"] == 2 and not r["errors"]
    csv = ("Tool,Brand,Serial #,Cost,Date,Where\nMultimeter,Fluke,98765432,250,11/20/2024,shop\n"
           "Bad,,,,notadate,\n").encode()
    r = c.post("/api/tools/import", data={"file": (io.BytesIO(csv), "t.csv")}, content_type="multipart/form-data").json
    assert r["updated"] == 1 and r["skipped"] == 1 and "date" in r["errors"][0]
    m = next(t for t in c.get("/api/tools").json["tools"] if t["name"] == "Multimeter")
    assert (m["price"], m["location"], m["purchased"], m["model"]) == (250.0, "shop", "2024-11-20", "117")
    out = c.get("/api/tools/export.csv").data.decode()
    assert out.splitlines()[0] == "name,make,model,serial,category,purchased,price,vendor,warranty_until,location,notes"
    assert "Fluke,117,98765432" in out
