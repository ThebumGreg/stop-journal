"""Home page summary: next appointment with drive time / ETA, unfinished stops."""
from dataclasses import replace

from app import calendar_feed, db, summary
from app.places.base import Candidate
from tests.test_export import D, make
from tests.test_detect import CFG


def test_summary_next_appointment_eta_and_unfinished(tmp_path, monkeypatch):
    c = make(tmp_path)
    stops = c.get(f"/api/day/{D}").json["stops"]
    last = stops[-1]
    now = last["depart_ts"] + 600
    ev = {"uid": "x", "start_ts": now + 3600, "end_ts": now + 5400, "title": "Onsite Beta", "name": "Beta Plumbing",
          "location": "9 Oak Ave, Testville", "ticket": "77"}
    monkeypatch.setattr(calendar_feed, "events_for_day", lambda cfg, date: [ev])
    import app.service as svc
    monkeypatch.setattr(svc.time, "time", lambda: now)
    cfg = replace(CFG, data_dir=str(tmp_path), calendar_url="https://x/cal.ics", routing_url="")
    conn = db.connect(str(tmp_path))
    live = {"lat": last["lat"], "lon": last["lon"], "moving": True}
    target = Candidate(name="9 Oak Ave", lat=last["lat"] + 0.05, lon=last["lon"])
    out = summary.build(conn, cfg, D, now, live, lambda q, lat, lon: [target])
    n = out["next"]
    assert n["name"] == "Beta Plumbing" and n["minutes_to_start"] == 60
    e = n["eta"]
    assert e["driving"] and e["method"] == "estimate" and e["found_by"] == "map search"
    assert 5 <= e["minutes"] <= 20 and e["early_min"] > 0 and e["arrive"] and e["leave_by"]
    assert out["today"]["stops"] == len(out["unfinished"])          # nothing finished yet
    assert out["unfinished"][0]["url"].startswith(f"/day/{D}?stop=")
    # a saved place with that name is used before any map search
    conn.execute("INSERT INTO known_sites(name,address,lat,lon,kind) VALUES ('Beta Plumbing','',?,?,'client')",
                 (last["lat"] + 0.01, last["lon"]))
    conn.commit()
    out = summary.build(conn, cfg, D, now, dict(live, moving=False), None)
    assert out["next"]["eta"]["found_by"] == "saved place" and not out["next"]["eta"]["driving"]
    out = summary.build(conn, cfg, D, now, {"lat": last["lat"] + 0.01, "lon": last["lon"]}, None)
    assert out["next"]["eta"] == {"arrived": True}
    conn.close()


def test_home_and_day_pages(tmp_path):
    c = make(tmp_path)
    home = c.get("/").data.decode()
    assert 'home.js' in home and '<a href="/" aria-current="page">Home</a>' in home
    assert '<a href="/day" aria-current="page">Day</a>' in c.get("/day").data.decode()
    d = c.get("/api/summary").json
    assert d["calendar_on"] is False and "today" in d and d["next"] is None
