"""On-site appointments from a published calendar (Outlook / Office 365 "Publish a calendar" ICS link, or any ICS).

Read-only. The link is a secret (anyone with it can read the calendar), so it is stored write-only like the other
keys. Only events that say they are on site are used: the keyword (default "onsite"; "on-site" and "on site"
match too) in the title, location, notes or categories. All-day events are skipped. From the event notes only the
keyword and these labelled lines are read (the notes are never stored whole):
  Ticket: 1042   Contact: Jane Smith 555-0100   Work: Replace switch   Category: Time & Material

The feed is cached in memory for CACHE_S, so the day page and the alert thread do not hit Outlook every time.
"""
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

CACHE_S = 600
TIMEOUT_S = 10
MAX_BYTES = 8 * 1024 * 1024
_cache = {}            # url -> (fetched_at, calendar object)
_lock = threading.Lock()


class CalendarError(Exception):
    pass


def norm_url(url):
    url = (url or "").strip()
    if url.lower().startswith("webcal://"):
        url = "https://" + url[9:]
    return url


def _word(keyword):
    """'onsite' -> a pattern that also matches 'on-site' and 'on site'."""
    k = re.sub(r"[\s_-]+", "", (keyword or "onsite").lower())
    return re.compile(r"[\s_-]*".join(map(re.escape, k)), re.I) if k else None


def _fetch(url, session=None):
    import icalendar
    now = time.time()
    with _lock:
        hit = _cache.get(url)
        if hit and now - hit[0] < CACHE_S:
            return hit[1]
    try:
        r = (session or requests).get(url, timeout=TIMEOUT_S, headers={"User-Agent": "JobHub calendar reader"})
    except requests.RequestException as e:
        raise CalendarError(f"Could not reach the calendar ({type(e).__name__}).")
    if r.status_code != 200:
        raise CalendarError(f"The calendar link answered {r.status_code}. Check the link is still published.")
    if len(r.content) > MAX_BYTES:
        raise CalendarError("The calendar is too large to read.")
    try:
        cal = icalendar.Calendar.from_ical(r.content)
    except Exception:
        raise CalendarError("That link did not return a calendar (ICS). Use the ICS link, not the HTML one.")
    with _lock:
        _cache[url] = (now, cal)
    return cal


def clear_cache():
    with _lock:
        _cache.clear()


def _text(v):
    if v is None:
        return ""
    if isinstance(v, list):
        return " ".join(_text(x) for x in v)
    if hasattr(v, "cats"):
        return " ".join(map(str, v.cats))
    return str(v)


TICKET = re.compile(r"(?:ticket|job|wo|work order)\s*[#:]?\s*([A-Za-z0-9-]{2,20})|#(\d{3,10})", re.I)


def clean_title(title, keyword):
    """'Onsite - Acme Plumbing' -> 'Acme Plumbing'."""
    pat = _word(keyword)
    t = pat.sub("", title) if pat else title
    t = re.sub(r"^\W+|\W+$", "", re.sub(r"\(\s*\)|\[\s*\]", "", t)).strip()
    return re.sub(r"\s{2,}", " ", t) or title.strip()


LABELS = {"contact": "contact", "phone": "contact", "work": "work", "scope": "work",
          "category": "category", "billing": "category", "estimate": "estimate", "quote": "estimate"}


def labelled(notes):
    """'Contact: Jane 555-0100' style lines from event notes -> {contact, work, category} (first of each, 200 chars)."""
    out = {}
    for line in (notes or "").splitlines():
        m = re.match(r"\s*([A-Za-z ]{3,12})\s*:\s*(.+)", line)
        if not m:
            continue
        key = LABELS.get(m.group(1).strip().lower())
        if key and key not in out:
            out[key] = m.group(2).strip()[:200]
    return out


def events_for_day(cfg, date, session=None):
    """On-site events overlapping a local date, sorted by start: [{uid, start_ts, end_ts, title, name, location, ticket}]."""
    import recurring_ical_events
    url = norm_url(cfg.calendar_url)
    if not url:
        return []
    cal = _fetch(url, session)
    tz = ZoneInfo(cfg.app_tz)
    day = datetime.fromisoformat(date).replace(tzinfo=tz)
    pat = _word(cfg.calendar_keyword)
    out = []
    for ev in recurring_ical_events.of(cal).between(day, day + timedelta(days=1)):
        start, end = ev.get("DTSTART"), ev.get("DTEND")
        if start is None:
            continue
        s = start.dt
        if not isinstance(s, datetime):
            continue                                        # all-day: not an appointment
        e = end.dt if end is not None else s + timedelta(hours=1)
        if s.tzinfo is None:
            s, e = s.replace(tzinfo=tz), e.replace(tzinfo=tz)
        title = _text(ev.get("SUMMARY")).strip()
        location = _text(ev.get("LOCATION")).strip()
        notes = _text(ev.get("DESCRIPTION"))
        cats = _text(ev.get("CATEGORIES"))
        if pat and not any(pat.search(x) for x in (title, location, notes, cats)):
            continue
        if str(ev.get("STATUS", "")).upper() == "CANCELLED" or title.lower().startswith("canceled:"):
            continue
        if title.startswith("JobHub · ") or str(ev.get("UID", "")).split("@")[-1].startswith("jobhub"):
            continue                                        # our own stop invites (calendar_out), not appointments
        m = TICKET.search(title + " " + notes)
        lines = labelled(notes)
        out.append({"contact": lines.get("contact", ""), "work": lines.get("work", ""), "category": lines.get("category", ""),
                    "estimate": lines.get("estimate", "").lstrip("#").strip()[:40],"uid": str(ev.get("UID", "")) + "@" + str(int(s.timestamp())),
                    "start_ts": int(s.timestamp()), "end_ts": int(e.timestamp()),
                    "title": title, "name": clean_title(title, cfg.calendar_keyword),
                    "location": location.replace("\n", ", "), "ticket": (m.group(1) or m.group(2)) if m else ""})
    return sorted(out, key=lambda x: x["start_ts"])


def test(cfg, session=None, days=7):
    """For the Settings button: how many events in the next week, and how many are on site."""
    import recurring_ical_events
    cal = _fetch(norm_url(cfg.calendar_url), session)
    now = datetime.now(timezone.utc)
    evs = [e for e in recurring_ical_events.of(cal).between(now, now + timedelta(days=days))
           if isinstance(e.get("DTSTART").dt, datetime)]
    pat = _word(cfg.calendar_keyword)
    on = [e for e in evs if not pat or any(pat.search(_text(e.get(k))) for k in
                                           ("SUMMARY", "LOCATION", "DESCRIPTION", "CATEGORIES"))]
    return len(evs), len(on)
