"""HubSpot company IDs on places: edited on the Places page or in bulk by CSV, copied to confirmed stops."""
import io

from tests.test_commute import D, JOB, make


def acme_place(c):
    c.post("/api/places", json={"name": "Acme", "kind": "client", "lat": JOB[0], "lon": JOB[1], "address": "1 Main St"})
    return next(p for p in c.get("/api/places").json["places"] if p["name"] == "Acme")


def job(c):
    return next(s for s in c.get(f"/api/day/{D}").json["stops"] if s["business_name"] == "Acme")


def report_stop(c):
    return c.get(f"/api/export.json?from={D}").json["days"][0]["stops"][0]


def test_place_id_copied_to_confirmed_stop_and_not_rewritten(tmp_path):
    c = make(tmp_path)
    p = acme_place(c)
    assert c.patch(f"/api/places/{p['id']}", json={"hubspot_company_id": "abc"}).status_code == 400
    r = c.patch(f"/api/places/{p['id']}", json={"hubspot_company_id": " 18234567890 "})
    assert r.status_code == 200 and r.json["hubspot_company_id"] == "18234567890"
    j = job(c)
    c.patch(f"/api/stops/{j['id']}", json={"known_site_id": p["id"], "status": "Confirmed"})
    assert job(c)["hubspot_company_id"] == "18234567890"
    assert report_stop(c)["hubspot_company_id"] == "18234567890"
    assert c.get(f"/api/export.json?from={D}").json["records_to_update"][0]["hubspot_company_id"] == "18234567890"
    # a later change on the place does not rewrite the stop
    c.patch(f"/api/places/{p['id']}", json={"hubspot_company_id": "999"})
    assert job(c)["hubspot_company_id"] == "18234567890"
    # moving the stop to another place takes that place's ID
    c.post("/api/places", json={"name": "Other Co", "kind": "client", "lat": JOB[0] + 0.01, "lon": JOB[1]})
    o = next(x for x in c.get("/api/places").json["places"] if x["name"] == "Other Co")
    c.patch(f"/api/places/{o['id']}", json={"hubspot_company_id": "555"})
    c.patch(f"/api/stops/{j['id']}", json={"known_site_id": o["id"]})
    assert job(c)["hubspot_company_id"] == "555"


def test_csv_export_and_import(tmp_path):
    c = make(tmp_path)
    p = acme_place(c)
    j = job(c)
    c.patch(f"/api/stops/{j['id']}", json={"known_site_id": p["id"], "status": "Confirmed"})
    text = c.get("/api/places/export.csv").get_data(as_text=True)
    lines = text.strip().splitlines()
    assert lines[0] == "place_id,name,kind,address,hubspot_company_id"
    assert f"{p['id']},Acme,client,1 Main St," in lines
    new = "place_id,name,kind,address,hubspot_company_id\n" + f"{p['id']},Acme,client,1 Main St,4242\n" + ",Nobody,client,,77\n"
    up = lambda dry: c.post("/api/places/import" + ("?dry=1" if dry else ""),
                            data={"file": (io.BytesIO(new.encode()), "p.csv")}, content_type="multipart/form-data").json
    r = up(True)
    assert r["updated"] == 1 and r["dry"] and r["not_found"] == ["Row 3: Nobody"]
    assert next(x for x in c.get("/api/places").json["places"] if x["id"] == p["id"])["hubspot_company_id"] == ""
    r = up(False)
    assert r["updated"] == 1 and r["stops_filled"] == 1
    assert job(c)["hubspot_company_id"] == "4242"
    # match by name + address when there is no id; bad IDs block the whole file
    bad = "name,address,hubspot_company_id\nAcme,1 main st,12x\n"
    r = c.post("/api/places/import", data={"file": (io.BytesIO(bad.encode()), "p.csv")},
               content_type="multipart/form-data").json
    assert r["errors"] and r["updated"] == 0
