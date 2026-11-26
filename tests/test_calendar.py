"""Outlook (ICS) on-site appointments next to the day's stops."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app import calendar_feed
from tests.test_export import D, make

TZ = ZoneInfo("America/Los_Angeles")

# What Outlook publishes: Windows time zone names with a VTIMEZONE block, CRLF lines.
VTZ = """BEGIN:VTIMEZONE
TZID:Pacific Standard Time
BEGIN:STANDARD
DTSTART:16010101T020000
TZOFFSETFROM:-0700
TZOFFSETTO:-0800
RRULE:FREQ=YEARLY;INTERVAL=1;BYDAY=1SU;BYMONTH=11
END:STANDARD
BEGIN:DAYLIGHT
DTSTART:16010101T020000
TZOFFSETFROM:-0800
TZOFFSETTO:-0700
RRULE:FREQ=YEARLY;INTERVAL=1;BYDAY=2SU;BYMONTH=3
END:DAYLIGHT
END:VTIMEZONE"""


def ev(uid, start, end, summary, location="", desc="", extra=""):
    return (f"BEGIN:VEVENT\nUID:{uid}\nSUMMARY:{summary}\nDTSTART;TZID=Pacific Standard Time:{start}\n"
            f"DTEND;TZID=Pacific Standard Time:{end}\nLOCATION:{location}\nDESCRIPTION:{desc}\n{extra}END:VEVENT")


def ics(*events):
    body = "BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:Microsoft Exchange Server 2010\n" + VTZ + "\n" + "\n".join(events) + "\nEND:VCALENDAR\n"
    return body.replace("\n", "\r\n").encode()


class Resp:
    def __init__(self, content, status=200):
        self.content, self.status_code = content, status


@pytest.fixture
def feed(monkeypatch):
    box = {"ics": ics()}
    calendar_feed.clear_cache()
    monkeypatch.setattr(calendar_feed.requests, "get", lambda url, **kw: Resp(box["ics"], box.get("status", 200)))
    yield box
    calendar_feed.clear_cache()


def hhmm(ts):
    return datetime.fromtimestamp(ts, TZ).strftime("%Y%m%dT%H%M%S")


def test_onsite_events_match_stops_and_offer_the_name(tmp_path, feed):
    c = make(tmp_path, calendar_url="https://outlook.office365.com/owa/calendar/x/calendar.ics")
    stops = c.get(f"/api/day/{D}").json["stops"]
    s0 = stops[0]
    feed["ics"] = ics(
        ev("a1", hhmm(s0["arrive_ts"] + 600), hhmm(s0["depart_ts"]), "Onsite - Acme Plumbing", "12 Main St\\nTestville",
           "Ticket #10422 please call first\\nContact: Jane Smith 555-0100\\nWork: Replace the shutoff\\ncategory: time & material"),
        ev("a2", "20261118T060000", "20261118T063000", "Team call"),                        # not on site
        ev("a3", "20261118T230000", "20261118T233000", "Late visit", "", "", "CATEGORIES:On-site\n"),  # category, no stop
        ev("a4", "20261118T200000", "20261118T203000", "Canceled: on site Beta", ""),
        "BEGIN:VEVENT\nUID:a5\nSUMMARY:Onsite all day\nDTSTART;VALUE=DATE:20261118\nDTEND;VALUE=DATE:20261119\nEND:VEVENT",
        ev("a6", "20261111T070000", "20261111T073000", "Weekly onsite check", "", "", "RRULE:FREQ=WEEKLY;COUNT=3\n"),
    )
    calendar_feed.clear_cache()
    d = c.get(f"/api/day/{D}").json
    assert d["calendar_error"] == ""
    names = [e["name"] for e in d["calendar"]]
    assert names == ["Weekly check", "Acme Plumbing", "Late visit"]          # recurring expanded, sorted
    acme = d["calendar"][1]
    assert acme["stop_id"] == s0["id"] and acme["state"] == "done"
    assert acme["location"] == "12 Main St, Testville" and acme["ticket"] == "10422"
    s = next(x for x in d["stops"] if x["id"] == s0["id"])
    assert s["calendar"]["name"] == "Acme Plumbing"
    assert d["calendar"][2]["stop_id"] is None
    assert s["calendar"]["offer"] and s["calendar"]["contact"] == "Jane Smith 555-0100"
    assert s["calendar"]["work"] == "Replace the shutoff" and s["calendar"]["category"] == "Time & Material"
    # once the stop has the name, the details stay but "Use calendar" is no longer offered
    r = c.patch(f"/api/stops/{s0['id']}", json={"business_name": "Acme Plumbing"}).json
    assert next(x for x in r["stops"] if x["id"] == s0["id"])["calendar"]["offer"] is False


def test_calendar_errors_and_settings(tmp_path, feed):
    c = make(tmp_path, calendar_url="https://example.com/cal.ics")
    feed["ics"], feed["status"] = b"nope", 404
    assert "404" in c.get(f"/api/day/{D}").json["calendar_error"]
    feed["status"] = 200
    calendar_feed.clear_cache()
    assert "did not return a calendar" in c.get(f"/api/day/{D}").json["calendar_error"]
    from datetime import datetime as _dt, timedelta as _td
    tomorrow = (_dt.now() + _td(days=1)).strftime("%Y%m%d")          # the check looks at the next 7 days from now
    feed["ics"] = ics(ev("x", tomorrow + "T090000", tomorrow + "T100000", "on site: Acme"))
    r = c.post("/settings/test-calendar", data={"calendar_keyword": "onsite"}).json
    assert r["ok"] and "1 marked" in r["message"]
    assert c.post("/settings", data={"calendar_url": "http://insecure"}).status_code in (200, 400)
    assert make(tmp_path / "b").get(f"/api/day/{D}").json["calendar"] == []                  # off by default


def test_clean_title():
    assert calendar_feed.clean_title("Onsite - Acme Plumbing", "onsite") == "Acme Plumbing"
    assert calendar_feed.clean_title("Acme Plumbing (on-site)", "onsite") == "Acme Plumbing"
    assert calendar_feed.clean_title("ONSITE", "onsite") == "ONSITE"


def test_labelled_lines():
    got = calendar_feed.labelled("Ticket: 1\nContact: Bob 555-0100\nWork: Fix it\nWork: second\nnot a label\nCategory: Quoted project")
    assert got == {"ticket": "1", "contact": "Bob 555-0100", "work": "Fix it", "category": "Quoted project"}


def test_name_and_address_beat_time_overlap(tmp_path, feed):
    """A real day (2026-11-25): Largo Concrete's appointment went to the next stop because it overlapped longer, and
    the Systems Source meeting then landed on the office stop. Names and addresses come first; offices never match on
    time alone."""
    c = make(tmp_path, calendar_url="https://outlook.office365.com/owa/calendar/x/calendar.ics")
    s1, s2, s3 = c.get(f"/api/day/{D}").json["stops"]                   # 9:00-10:00, 10:20-11:20, 12:00-12:45
    c.patch(f"/api/stops/{s1['id']}", json={"business_name": "Largo Concrete"})
    c.patch(f"/api/stops/{s2['id']}", json={"business_name": "Systems Source - Haworth Dealer"})
    c.patch(f"/api/stops/{s3['id']}", json={"business_name": "ComDirect, Inc.", "stop_type": "office"})
    feed["ics"] = ics(
        ev("l", "20261118T100500", "20261118T110000", "Onsite - Largo Concrete"),          # booked after you left
        ev("o", "20261118T113000", "20261118T123000", "Onsite Weekly OAC - Systems Source #700", "4695 MacArthur Court"),
    )
    calendar_feed.clear_cache()
    d = c.get(f"/api/day/{D}").json
    by = {e["name"]: e["stop_id"] for e in d["calendar"]}
    assert by == {"Largo Concrete": s1["id"], "Weekly OAC - Systems Source #700": s2["id"]}
    stops = {s["id"]: s for s in d["stops"]}
    assert stops[s1["id"]]["calendar"]["offer"] is False and "calendar" not in stops[s3["id"]]
    # an appointment with no name or address in common never goes to the office, even right on top of it
    feed["ics"] = ics(ev("x", "20261118T120000", "20261118T124500", "Onsite - Somebody Else"))
    calendar_feed.clear_cache()
    assert c.get(f"/api/day/{D}").json["calendar"][0]["stop_id"] is None


def test_affinity():
    from app.service import _cal_affinity as aff
    st = {"business_name": "ComDirect, Inc.", "address": "100 Main St, Irvine"}
    assert aff(st, {"name": "ComDirect", "location": ""}) == 2
    assert aff(st, {"name": "Network check", "location": "100 Main Street"}) == 1
    assert aff({"business_name": "Systems Source - Haworth Dealer"}, {"name": "OAC - Systems Source #700"}) == 1
    assert aff({"business_name": "Largo Concrete"}, {"name": "Weekly OAC - Systems Source #700"}) == 0
    assert aff({"business_name": ""}, {"name": "Acme"}) == 0
