"""Calendar invites: each of today's stops becomes an Outlook event, updated on change, cancelled when gone."""
from dataclasses import replace

import icalendar

from app import calendar_feed, calendar_out, db, mailer, settings
from tests.test_commute import D, make
from tests.test_email import FakeSMTP, SENT, setup_mail  # noqa: F401  (autouse fixture comes with it)
from tests.test_email import fake_smtp  # noqa: F401


def run(tmp_path, now):
    conn = db.connect(str(tmp_path))
    from tests.test_detect import CFG
    cfg = settings.effective(replace(CFG, data_dir=str(tmp_path)), conn)
    try:
        return calendar_out.sync(conn, cfg, D, now)
    finally:
        conn.close()


def events(start=0):
    out = []
    for _, msg in SENT[start:]:
        part = next(p for p in msg.walk() if p.get_content_type() == "text/calendar")
        cal = icalendar.Calendar.from_ical(part.get_payload(decode=True))
        ev = cal.walk("VEVENT")[0]
        out.append((str(cal["METHOD"]), str(ev["SUMMARY"]), str(ev.get("LOCATION", "")), int(ev["SEQUENCE"]),
                    str(ev["UID"]), str(ev.get("DESCRIPTION", ""))))
    return out


def test_stops_become_events_updates_and_cancels(tmp_path):
    c = make(tmp_path)
    setup_mail(tmp_path, calendar_invites="on")
    stops = c.get(f"/api/day/{D}").json["stops"]
    job = next(s for s in stops if s["business_name"] == "Acme")
    c.patch(f"/api/stops/{job['id']}", json={"address": "1 Main St", "ticket": "1042", "notes": "secret notes"})
    now = job["depart_ts"] + 3600
    sent, cancelled = run(tmp_path, now)
    assert sent == 3 and cancelled == 0
    evs = events()
    acme = next(e for e in evs if "Acme" in e[1])
    assert acme[0] == "REQUEST" and acme[1].startswith("JobHub · ") and acme[2] == "1 Main St" and acme[3] == 0
    assert "Ticket: 1042" in acme[5] and "secret" not in acme[5]
    home = [e for e in evs if e[1] == "JobHub · Personal"]
    assert len(home) == 2 and all(e[2] == "" for e in home)                 # personal: no place
    assert run(tmp_path, now) == (0, 0)                                       # nothing changed, nothing sent
    n = len(SENT)
    c.patch(f"/api/stops/{job['id']}", json={"business_name": "Acme Dental"})
    assert run(tmp_path, now) == (1, 0)
    upd = events(n)[0]
    assert "Acme Dental" in upd[1] and upd[3] == 1 and upd[4] == acme[4]      # same event, next sequence
    n = len(SENT)
    c.delete(f"/api/stops/{job['id']}")
    assert run(tmp_path, now) == (0, 1)
    cancel = events(n)[0]
    assert cancel[0] == "CANCEL" and cancel[4] == acme[4]


def test_off_by_default_and_our_events_are_not_read_back(tmp_path):
    from tests.test_detect import CFG
    assert not calendar_out.enabled(CFG)
    assert calendar_out.is_ours("stop-1-2@jobhub") and not calendar_out.is_ours("abc@outlook.com")


def test_finished_job_title(tmp_path):
    c = make(tmp_path)
    setup_mail(tmp_path, calendar_invites="on")
    job = next(s for s in c.get(f"/api/day/{D}").json["stops"] if s["business_name"] == "Acme")
    c.patch(f"/api/stops/{job['id']}", json={"stop_type": "job", "work_category": "Time & Material", "notes": "x"})
    c.post(f"/api/stops/{job['id']}/finish", json={"visit_result": "return", "due": "2026-11-25"})
    run(tmp_path, job["depart_ts"] + 3600)
    t = next(e[1] for e in events() if "Acme" in e[1])
    assert t == "JobHub · ✓ Acme · Return visit needed"


def test_settings_invite_button(tmp_path):
    c = make(tmp_path)
    assert not c.post("/settings/test-invite").json["ok"]
    setup_mail(tmp_path)
    r = c.post("/settings/test-invite").json
    assert r["ok"] and events()[0][1] == "JobHub · Test event"
