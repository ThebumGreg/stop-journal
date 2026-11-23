"""The home page: where you are, what is next, and what is still open today.

- Next appointment: the first on-site calendar event of today that has not been visited yet, with "in 45 min".
- Drive time to it from where the phone is now (free OSRM, no traffic; rounded coordinates): while driving it is an
  ETA ("arriving 9:48, 10 min early"); while parked it is "leave by 9:36".
- Today: stops, finished / unfinished (each links to its sheet), on-site and drive time, miles.

The event's address is looked up once: a saved place with that name or address first, else OpenStreetMap search
near where you are (cached). Nothing here is sent anywhere except that lookup and the route.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from . import routing, service
from .geo import haversine_m

ETA_WITHIN_S = 4 * 3600        # only work out drive time for appointments in the next few hours
SLACK_MIN = 5                  # "leave by" keeps a few minutes in hand


def locate(conn, cfg, ev, near, search):
    """(lat, lon, how) for an event, or None. Saved places by name or address first, then `search(text, lat, lon)`."""
    name, loc = ev["name"].strip().lower(), (ev["location"] or "").strip().lower()
    for r in conn.execute("SELECT * FROM known_sites WHERE lat IS NOT NULL"):
        if (name and (r["name"] or "").strip().lower() == name) or (loc and (r["address"] or "").strip().lower() == loc):
            return r["lat"], r["lon"], "saved place"
    q = ev["location"] or ev["name"]
    if not q or not search:
        return None
    try:
        found = search(q, near[0], near[1]) if near else []
    except Exception:
        return None
    for c in found:
        if c.lat or c.lon:
            return c.lat, c.lon, "map search"
    return None


def build(conn, cfg, date, now, live=None, search=None):
    tz = ZoneInfo(cfg.app_tz)
    clock = lambda ts: datetime.fromtimestamp(ts, tz).strftime("%-I:%M %p")
    day = service.day_payload(conn, cfg, date)
    stops = [s for s in day["stops"] if s["kind"] == "stop"]
    work = [s for s in stops if s["status"] not in ("Personal", "Lunch") and not
            (s["status"] == "Traffic" and s["stop_type"] != "gas")]
    unfinished = [s for s in work if s["needs"] or (s["stop_type"] != "gas" and s["status"] != "Confirmed")]
    num = {s["id"]: i + 1 for i, s in enumerate(day["stops"])}
    here = next((s for s in stops if s.get("here_now")), None)
    t = day["totals"]
    out = {
        "date": date, "now": now, "now_text": clock(now), "tz": cfg.app_tz, "day_url": f"/day/{date}",
        "live": live,
        "here": ({"id": here["id"], "num": num[here["id"]], "name": here["business_name"] or service.STOP_TYPES.get(here["stop_type"], "Stop"),
                  "since": here["arrive"], "minutes": round((now - here["arrive_ts"]) / 60),
                  "needs": here["needs"], "url": f"/day/{date}?stop={here['id']}"} if here else None),
        "today": {"stops": len(work), "finished": len(work) - len(unfinished),
                  "on_site_min": round((t.get("on_site_s") or 0) / 60), "drive_min": round((t.get("drive_s") or 0) / 60),
                  "miles": t.get("miles_approx") or 0, "started": (day["marks"] or {}).get("start"),
                  "ended": (day["marks"] or {}).get("end")},
        "unfinished": [{"id": s["id"], "num": num[s["id"]], "name": s["business_name"] or service.STOP_TYPES.get(s["stop_type"], "Stop"),
                        "arrive": s["arrive"], "depart": s["depart"], "needs": s["needs"] or ["finish"],
                        "here": bool(s.get("here_now")), "url": f"/day/{date}?stop={s['id']}"} for s in unfinished],
        "calendar_on": bool(cfg.calendar_url), "calendar_error": day.get("calendar_error", ""),
        "appointments": [{k: e[k] for k in ("name", "start", "end", "location", "state", "minutes_to_start", "stop_id")}
                         for e in day.get("calendar", [])],
        "next": None,
    }
    nxt = next((e for e in day.get("calendar", []) if e["state"] == "upcoming"), None)
    if not nxt:
        return out
    n = {"name": nxt["name"], "start": nxt["start"], "end": nxt["end"], "location": nxt["location"],
         "minutes_to_start": nxt["minutes_to_start"], "ticket": nxt["ticket"], "eta": None}
    out["next"] = n
    pos = (live["lat"], live["lon"]) if live and live.get("lat") is not None else None
    if not pos or nxt["start_ts"] - now > ETA_WITHIN_S:
        return out
    where = locate(conn, cfg, nxt, pos, search)
    if not where:
        n["eta_note"] = "Could not find that address on the map."
        return out
    if haversine_m(pos[0], pos[1], where[0], where[1]) < 150:
        n["eta"] = {"arrived": True}
        return out
    secs, meters, method = routing.drive_eta(conn, cfg, pos, (where[0], where[1]))
    arrive = now + int(secs)
    early = round((nxt["start_ts"] - arrive) / 60)
    moving = bool(live.get("moving"))
    n["eta"] = {"minutes": max(1, round(secs / 60)), "miles": round(meters / 1609.344, 1), "method": method,
                "found_by": where[2], "driving": moving, "arrive": clock(arrive), "early_min": early,
                "leave_by": clock(nxt["start_ts"] - int(secs) - SLACK_MIN * 60)}
    return out
