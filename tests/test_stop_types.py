"""Stop types (picked first on the card) and Gas stops with fuel details."""
from tests.test_export import D, make, pdf_text


def stops(c, date=D):
    return c.get(f"/api/day/{date}").json["stops"]


def test_type_sets_status_and_back(tmp_path):
    c = make(tmp_path)
    s = stops(c)[0]
    assert s["stop_type"] == "" and s["status"] == "Draft"          # new stop: type not chosen yet
    r = c.patch(f"/api/stops/{s['id']}", json={"stop_type": "lunch"}).json["stops"][0]
    assert (r["stop_type"], r["status"]) == ("lunch", "Lunch")
    r = c.patch(f"/api/stops/{s['id']}", json={"stop_type": "job"}).json["stops"][0]
    assert (r["stop_type"], r["status"]) == ("job", "Draft")       # off the lunch status, waiting to be confirmed
    r = c.patch(f"/api/stops/{s['id']}", json={"stop_type": "vendor", "status": "Confirmed",
                                               "business_name": "Supply Co"}).json["stops"][0]
    assert (r["stop_type"], r["status"]) == ("vendor", "Confirmed")
    assert r["work_category"] == "Internal operations"
    r = c.patch(f"/api/stops/{s['id']}", json={"status": "Personal"}).json["stops"][0]   # the old way still works
    assert (r["stop_type"], r["status"]) == ("personal", "Personal")
    assert c.patch(f"/api/stops/{s['id']}", json={"stop_type": "boat"}).status_code == 400


def test_confirmed_stops_without_type_are_jobs(tmp_path):
    c = make(tmp_path)
    s = stops(c)[0]
    r = c.patch(f"/api/stops/{s['id']}", json={"status": "Confirmed", "business_name": "Acme"}).json["stops"][0]
    assert r["stop_type"] == "job"


def test_gas_stop_counts_as_drive_time_and_reports_fuel(tmp_path):
    c = make(tmp_path)
    ids = [s["id"] for s in stops(c)]
    c.patch(f"/api/stops/{ids[0]}", json={"status": "Confirmed", "business_name": "Acme", "notes": "Fixed it"})
    r = c.patch(f"/api/stops/{ids[1]}", json={"stop_type": "gas", "business_name": "Corner Fuel"}).json
    g = next(x for x in r["stops"] if x["id"] == ids[1])
    assert (g["stop_type"], g["status"]) == ("gas", "Traffic")
    r = c.put(f"/api/stops/{ids[1]}/fuel", json={"gallons": "12.5", "total": "$51.25", "odometer": "48210"})
    assert r.status_code == 200
    g = next(x for x in r.json["stops"] if x["id"] == ids[1])
    assert g["fuel"] == {"gallons": 12.5, "total": 51.25, "odometer": 48210, "price_per_gallon": 4.1}
    j = c.get(f"/api/export.json?from={D}").json
    day = j["days"][0]
    assert day["fuel"][0]["station"] == "Corner Fuel" and day["summary"]["fuel_total"] == 51.25
    assert day["summary"]["traffic_minutes"] == g["minutes"]           # time at the pump is drive time
    assert any(e["reason"].startswith("Fuel stop") for e in day["excluded"])
    assert any(r["description"].startswith("Fuel stop (Corner Fuel)") for r in day["timesheet"])
    assert all(s["business_name"] != "Corner Fuel" for s in day["stops"])   # not a job in Records to update
    text = pdf_text(c.get(f"/api/export.pdf?from={D}"))
    assert "Corner Fuel" in text and "51.25" in text and "48210" in text and "4.100" in text
    j = c.get(f"/api/export.json?from={D}&omit=fuel").json
    assert "fuel" not in j["days"][0] and "fuel_total" not in j["days"][0]["summary"]


def test_fuel_needs_a_gas_stop_and_valid_numbers(tmp_path):
    c = make(tmp_path)
    sid = stops(c)[0]["id"]
    assert c.put(f"/api/stops/{sid}/fuel", json={"gallons": 10}).status_code == 400   # not a Gas stop
    c.patch(f"/api/stops/{sid}", json={"stop_type": "gas"})
    assert c.put(f"/api/stops/{sid}/fuel", json={"gallons": "lots"}).status_code == 400
    assert c.put(f"/api/stops/{sid}/fuel", json={"gallons": -3}).status_code == 400
    c.put(f"/api/stops/{sid}/fuel", json={"gallons": 10, "total": 40})
    c.put(f"/api/stops/{sid}/fuel", json={"odometer": 1000})            # other fields are kept
    g = stops(c)[0]["fuel"]
    assert (g["gallons"], g["total"], g["odometer"]) == (10, 40, 1000)
    w = c.get(f"/api/export/check?from={D}").json["warnings"]
    assert not any("no total paid" in x for x in w)
    c.put(f"/api/stops/{sid}/fuel", json={"total": ""})
    w = c.get(f"/api/export/check?from={D}").json["warnings"]
    assert any("no total paid" in x for x in w)


def test_mpg_and_stats_across_days(tmp_path):
    c = make(tmp_path)
    days = ["2026-11-18", "2026-11-19", "2026-11-20"]
    for date, (gal, tot, odo) in zip(days, [(10, 40, 50000), (12, 48, 50300), (8, 36, 50500)]):
        r = c.post(f"/api/day/{date}/stops", json={"arrive": "06:00", "depart": "06:08", "stop_type": "gas",
                                                     "business_name": "Pump"})
        assert r.status_code == 200, r.json
        sid = next(s["id"] for s in stops(c, date) if s["arrive"] == "06:00")
        c.put(f"/api/stops/{sid}/fuel", json={"gallons": gal, "total": tot, "odometer": odo})
    st = c.get("/api/fuel").json
    assert [f["mpg"] for f in st["fills"]] == [25.0, 25.0, None]          # newest first; first fill has no MPG
    sm = st["summary"]
    assert sm["fill_ups"] == 3 and sm["gallons"] == 30 and sm["total"] == 124
    assert sm["miles"] == 500 and sm["mpg"] == 25.0 and sm["cost_per_mile"] == round(84 / 500, 3)
    one = c.get("/api/fuel?from=2026-11-20&to=2026-11-20").json
    assert one["fills"][0]["mpg"] == 25.0                                # uses the odometer from before the range


def test_undo_and_delete_keep_fuel_in_step(tmp_path):
    c = make(tmp_path)
    sid = stops(c)[0]["id"]
    c.patch(f"/api/stops/{sid}", json={"stop_type": "gas"})
    c.put(f"/api/stops/{sid}/fuel", json={"gallons": 9})
    c.delete(f"/api/stops/{sid}")
    assert c.get("/api/fuel").json["summary"]["fill_ups"] == 0
    c.post(f"/api/day/{D}/undo")
    assert stops(c)[0]["fuel"]["gallons"] == 9


def test_office_and_home_fill_from_saved_places(tmp_path):
    c = make(tmp_path)
    ss = stops(c)
    a, b = ss[0], ss[1]
    office = c.post("/api/places", json={"name": "Main Office", "address": "1 Office Way",
                                         "lat": a["lat"] + 0.05, "lon": a["lon"], "kind": "office"}).json
    c.post("/api/places", json={"name": "Home", "lat": b["lat"] + 0.0005, "lon": b["lon"], "kind": "home"})
    r = c.patch(f"/api/stops/{a['id']}", json={"stop_type": "office"}).json["stops"]
    s = next(x for x in r if x["id"] == a["id"])
    assert (s["business_name"], s["address"], s["known_site_id"]) == ("Main Office", "1 Office Way", office["id"])
    r = c.patch(f"/api/stops/{b['id']}", json={"stop_type": "personal"}).json["stops"]
    assert next(x for x in r if x["id"] == b["id"])["business_name"] == "Home"       # ~55 m from Home
    # a Personal errand far from Home stays unnamed; a typed name is never replaced
    r = c.patch(f"/api/stops/{a['id']}", json={"stop_type": "job", "business_name": "Acme"}).json["stops"]
    r = c.patch(f"/api/stops/{a['id']}", json={"stop_type": "office"}).json["stops"]
    assert next(x for x in r if x["id"] == a["id"])["business_name"] == "Acme"
    r = c.patch(f"/api/stops/{a['id']}", json={"stop_type": "personal"}).json["stops"]
    assert next(x for x in r if x["id"] == a["id"])["business_name"] == "Acme"


def test_new_part_from_the_sheet_keeps_unit_and_cost(tmp_path):
    c = make(tmp_path)
    sid = stops(c)[0]["id"]
    r = c.post(f"/api/stops/{sid}/parts", json={"name": "Copper pipe 1/2", "qty": 6, "unit": "ft",
                                                "unit_cost": "3.10", "type": "part", "billable": True}).json
    p = next(x for x in r["stops"] if x["id"] == sid)["parts"][0]
    assert (p["unit"], p["unit_cost"], p["billable"]) == ("ft", 3.1, True)
    cat = c.get("/api/parts?q=Copper").json["parts"][0]
    assert cat["unit"] == "ft" and cat["unit_cost"] == 3.1


def test_short_stop_at_a_gas_station_is_flagged_once(tmp_path):
    from app import db, service
    from app.places.base import Candidate
    c = make(tmp_path)
    ss = stops(c)
    conn = db.connect(str(tmp_path))
    short = min(ss, key=lambda s: s["minutes"])
    conn.execute("UPDATE stops SET depart_utc = arrive_utc + 600 WHERE id=?", (short["id"],))
    conn.commit()
    calls = []

    def nearby(lat, lon, radius):
        calls.append((lat, lon))
        if abs(lat - short["lat"]) < 1e-9:
            return [Candidate(name="Corner Fuel", lat=lat + 0.0002, lon=lon, category="amenity=fuel"),
                    Candidate(name="Far Fuel", lat=lat + 0.01, lon=lon, category="amenity=fuel")]
        return [Candidate(name="Cafe", lat=lat, lon=lon, category="amenity=cafe")]
    n = service.check_fuel_stops(conn, D, nearby)
    assert n >= 1 and len(calls) == n
    s = next(x for x in stops(c) if x["id"] == short["id"])
    assert s["gas_hint"] and s["stop_type"] == ""                   # a hint only: the type is still yours to pick
    assert service.check_fuel_stops(conn, D, nearby) == 0           # checked once
    def boom(*a):
        raise OSError("map down")
    conn.execute("UPDATE stops SET fuel_check=NULL")
    conn.commit()
    assert service.check_fuel_stops(conn, D, boom) == 0             # lookup down: nothing marked, tried again later
    assert conn.execute("SELECT COUNT(*) FROM stops WHERE fuel_check IS NOT NULL").fetchone()[0] == 0
    conn.close()


def test_short_break_is_paid_drive_time(tmp_path):
    c = make(tmp_path)
    ids = [s["id"] for s in stops(c)]
    c.patch(f"/api/stops/{ids[0]}", json={"status": "Confirmed", "business_name": "Acme", "notes": "Fixed it"})
    r = c.patch(f"/api/stops/{ids[1]}", json={"stop_type": "break"}).json
    b = next(x for x in r["stops"] if x["id"] == ids[1])
    assert (b["stop_type"], b["status"], b["needs"]) == ("break", "Traffic", [])
    day = c.get(f"/api/export.json?from={D}").json["days"][0]
    assert day["summary"]["traffic_minutes"] == b["minutes"]                  # paid, as drive time
    assert any(r["description"].startswith("Short break (paid") for r in day["timesheet"])
    assert any(e["reason"].startswith("Short break") for e in day["excluded"])
    assert "Short break (paid" in pdf_text(c.get(f"/api/export.pdf?from={D}"))
