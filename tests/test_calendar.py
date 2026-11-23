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
           "Ticket #10422 please call first"),
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
    # once the stop has the name, nothing more to offer
    r = c.patch(f"/api/stops/{s0['id']}", json={"business_name": "Acme Plumbing"}).json
    assert "calendar" not in next(x for x in r["stops"] if x["id"] == s0["id"])


def test_calendar_errors_and_settings(tmp_path, feed):
    c = make(tmp_path, calendar_url="https://example.com/cal.ics")
    feed["ics"], feed["status"] = b"nope", 404
    assert "404" in c.get(f"/api/day/{D}").json["calendar_error"]
    feed["status"] = 200
    calendar_feed.clear_cache()
    assert "did not return a calendar" in c.get(f"/api/day/{D}").json["calendar_error"]
    feed["ics"] = ics(ev("x", "20261125T090000", "20261125T100000", "on site: Acme"))
    r = c.post("/settings/test-calendar", data={"calendar_keyword": "onsite"}).json
    assert r["ok"] and "1 marked" in r["message"]
    assert c.post("/settings", data={"calendar_url": "http://insecure"}).status_code in (200, 400)
    assert make(tmp_path / "b").get(f"/api/day/{D}").json["calendar"] == []                  # off by default


def test_clean_title():
    assert calendar_feed.clean_title("Onsite - Acme Plumbing", "onsite") == "Acme Plumbing"
    assert calendar_feed.clean_title("Acme Plumbing (on-site)", "onsite") == "Acme Plumbing"
    assert calendar_feed.clean_title("ONSITE", "onsite") == "ONSITE"
