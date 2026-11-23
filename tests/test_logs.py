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
