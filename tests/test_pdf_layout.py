"""PDF layout: one top section, mileage as label/value rows with the total last, mileage on each stop, day recap."""
import re
from dataclasses import replace

from app import create_app, sample
from tests.test_detect import CFG
from tests.test_export import pdf_text

D = "2026-11-18"
HOME, JOB = sample.SITE_A, sample.SITE_B


def make(tmp_path):
    d = sample.Day(D)
    t = d.dwell(*HOME, d.at(7), 60)
    t = d.drive(HOME, JOB, t, 20)
    t = d.dwell(*JOB, t, 120)
    t = d.drive(JOB, HOME, t, 20)
    d.dwell(*HOME, t, 60)
    c = create_app(replace(CFG, data_dir=str(tmp_path)), client=sample.ListRecorder(d.points())).test_client()
    c.post("/api/places", json={"name": "Home", "kind": "home", "lat": HOME[0], "lon": HOME[1]})
    st = c.get(f"/api/day/{D}").json["stops"]
    c.patch(f"/api/stops/{st[0]['id']}", json={"business_name": "Home", "status": "Personal"})
    c.patch(f"/api/stops/{st[1]['id']}", json={"business_name": "Acme", "status": "Confirmed"})
    c.patch(f"/api/stops/{st[2]['id']}", json={"business_name": "Home", "status": "Personal"})
    c.post(f"/api/day/{D}/mark", json={"which": "end", "time": "10:45"})
    return c


def test_layout(tmp_path):
    c = make(tmp_path)
    text = pdf_text(c.get(f"/api/export.pdf?from={D}"))
    flat = re.sub(r"\s+", " ", text)
    assert "Day summary" not in flat and "Excluded" not in flat
    top = flat.split("Mileage")[0]
    assert "Timezone" in top and "On-site time" in top and "Generated at" in top      # one combined section
    m = c.get(f"/api/export.json?from={D}").json["days"][0]["mileage"]
    mil = flat.split("Mileage (Home to Home, miles)")[1].split("Stop 1")[0]
    assert "From To Miles" in mil and "1 Home Acme" in mil and "2 Acme Home" in mil          # a table: #, From, To, Miles
    assert f"{m['total_miles']:.1f}" in mil and mil.rindex("Total mileage") > mil.rindex("Acme Home")   # total last
    assert "by road" not in mil and "recorded" not in mil and "Personal stops left out" not in mil   # no notes
    # the stop shows the leg that ends at it
    assert f"Drive from previous stop {m['legs'][0]['miles']} mi from Home" in flat
    j = c.get(f"/api/export.json?from={D}").json["days"][0]["stops"][0]["drive_from_previous_stop"]
    assert j["miles_approx"] == m["legs"][0]["miles"] and j["from"] == "Home"
    # recap: a timesheet (in, out, activity, hours), no personal stops, totals and mileage at the bottom
    recap = flat.split("Day recap (timesheet)")[1].split("How to read")[0]
    assert "Clock out 10:45" in recap and "In Out Activity Hours" in recap
    assert "08:20 10:20 Acme 2.00" in recap and "Total paid hours 2.33" in recap and "Drive 0.33" in recap
    assert f"Mileage, Home to Home (miles) {m['total_miles']:.1f}" in recap
    assert "Personal" not in recap and "07:00" not in recap
    assert "JobHub Daily Summary" in flat and "JobHub Job Report" not in flat
    t = c.get(f"/api/export.json?from={D}").json["days"][0]["timesheet"]
    assert [r["type"] for r in t] == ["on_site", "drive"] and t[1]["description"] == "Drive: Acme to Home"
