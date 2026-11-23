"""Logging: requests by path only, errors with a reference code, events, coordinates never written."""
from app import logs
from tests.test_commute import D, make


def test_scrub_blanks_coordinates():
    assert logs.scrub("at 34.052235, -118.243683 ok 12.5 and 2026-11-18") == "at [coord], [coord] ok 12.5 and 2026-11-18"


def test_requests_logged_by_path_only(tmp_path):
    c = make(tmp_path)
    c.get(f"/api/day/{D}?lat=34.052235&lon=-118.243683")
    lines = logs.recent(50)
    hit = [l for l in lines if f"GET /api/day/{D} 200" in l]
    assert hit and not any("lat=" in l or "34.0522" in l for l in lines)
    assert any("JobHub started:" in l for l in lines)


def test_crash_gives_a_reference_and_logs_it(tmp_path, monkeypatch):
    c = make(tmp_path)
    from app import service
    monkeypatch.setattr(service, "day_payload", lambda *a, **k: 1 / 0)
    r = c.get(f"/api/day/{D}")
    assert r.status_code == 500 and "error " in r.json["error"]
    code = r.json["error"].split("error ")[1].split(")")[0]
    text = "\n".join(logs.recent(200))
    assert f"error {code} on GET /api/day/{D}" in text and "ZeroDivisionError" in text


def test_log_page_and_download(tmp_path):
    c = make(tmp_path)
    r = c.get("/settings/log")
    assert r.status_code == 200 and r.mimetype == "text/plain" and "JobHub started" in r.get_data(as_text=True)
    assert "attachment" in c.get("/settings/log?download=1").headers["Content-Disposition"]


def test_page_errors_reach_the_log(tmp_path):
    c = make(tmp_path)
    assert c.post("/api/client-log", json={"level": "error", "msg": "boom at day.js:12 near 34.052235",
                                           "page": "/day/2026-11-18?x=1"}).status_code == 204
    text = "\n".join(logs.recent(50))
    assert "jobhub.page: /day/2026-11-18 | boom at day.js:12 near [coord]" in text
    assert 'name="jobhub-log" content="info"' in c.get("/day").get_data(as_text=True)


def test_watchdog_reports_a_stuck_request():
    import threading
    import time
    go = threading.Event()

    def stuck_handler():
        logs.request_started("POST", "/api/stops/16/parts")
        go.wait(5)                      # pretend to be stuck here
        logs.request_ended()

    t = threading.Thread(target=stuck_handler)
    t.start()
    time.sleep(0.1)
    assert logs.check_stuck(now=time.monotonic() + logs.STUCK_S + 1) == 1
    assert logs.check_stuck(now=time.monotonic() + logs.STUCK_S + 2) == 0          # not again within REPEAT_S
    go.set(); t.join()
    text = "\n".join(logs.recent(80))
    assert "request still running after" in text and "POST /api/stops/16/parts" in text and "go.wait(5)" in text
    assert logs.check_stuck(now=time.monotonic() + 999) == 0                        # finished requests are forgotten


def test_delete_sent_as_post_with_override(tmp_path):
    c = make(tmp_path)
    job = next(s for s in c.get(f"/api/day/{D}").json["stops"] if s["business_name"] == "Acme")
    c.post(f"/api/stops/{job['id']}/parts", json={"name": "Widget", "qty": 1})
    line = next(p for p in next(s for s in c.get(f"/api/day/{D}").json["stops"] if s["id"] == job["id"])["parts"])
    r = c.post(f"/api/stop-parts/{line['id']}", headers={"X-Method-Override": "DELETE"})
    assert r.status_code == 200
    assert not next(s for s in c.get(f"/api/day/{D}").json["stops"] if s["id"] == job["id"])["parts"]
    # an override only applies to POST, and only to DELETE / PUT / PATCH
    assert c.get(f"/api/day/{D}", headers={"X-Method-Override": "DELETE"}).status_code == 200
