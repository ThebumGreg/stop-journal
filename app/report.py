"""The daily report: one data model, rendered as JSON (as-is) and as a PDF (app/pdf.py).

The PDF is read by another Claude session that updates HubSpot, so the model is
deliberately flat and unambiguous. Rules (also printed in the PDF's instruction block):
  * only Confirmed stops get detail; Personal and unconfirmed stops are listed by time only;
  * totals cover only the confirmed stops listed;
  * "drive from previous stop" is the drive immediately before the stop, left blank when it
    began at a Personal stop (consistent with the day totals in the app), except the first drive
    of the day when the user tapped Start day before arriving at the first stop; a last drive
    into a Personal stop (home) counts as "drive after last stop" when End day was tapped before it ended;
  * times are 24-hour local time in the stated timezone; miles are approximate.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import service
from .errors import EditError

SCHEMA_VERSION = "1"
MAX_DAYS = 31


def date_range(start, end):
    try:
        a, b = datetime.fromisoformat(start).date(), datetime.fromisoformat(end).date()
    except (TypeError, ValueError):
        raise EditError("Dates look like 2026-11-18.")
    if b < a:
        raise EditError("The end date is before the start date.")
    if (b - a).days + 1 > MAX_DAYS:
        raise EditError(f"Choose at most {MAX_DAYS} days at a time.")
    return [(a + timedelta(days=i)).isoformat() for i in range((b - a).days + 1)]


def _minutes(a, b):
    return round((b - a) / 60)


def _place_type(category):
    return category.split("=", 1)[1] if category.startswith("place=") else ""


def _part(p):
    return {"name": p["name"], "part_number": p["sku"], "quantity": p["qty"], "unit": p["unit"],
            "unit_cost": p["unit_cost"], "line_cost": p["line_cost"], "billable": p["billable"]}


def _split_parts(lines):
    """Parts go to `parts_used`; consumables (tape, solvent...) to `consumables_used`, so the
    reader does not mistake them for billable materials."""
    return ([_part(p) for p in lines if not p.get("consumable")],
            [_part(p) for p in lines if p.get("consumable")])


def build_day(payload, cfg):
    """payload: service.day_payload() for one date."""
    tz = ZoneInfo(cfg.app_tz)
    stops, drives = payload["stops"], payload["drives"]
    status = {s["id"]: s["status"] for s in stops}
    drive_into = {d["to_stop_id"]: d for d in drives}

    def iso(ts):
        return datetime.fromtimestamp(ts, tz).isoformat(timespec="seconds")

    out_stops, excluded = [], []
    for s in stops:
        if s["status"] != "Confirmed":
            reason = {"Personal": "Personal", "Lunch": "Lunch",
                      "Traffic": "Traffic (counted as drive time)"}.get(s["status"]) or \
                "Not confirmed" + (" (short stop)" if s["kind"] == "short" else "")
            excluded.append({"arrival": s["arrive"], "departure": s["depart"], "reason": reason})
            continue
        d = drive_into.get(s["id"])
        drive = None
        if d and d.get("counts", status.get(d["from_stop_id"]) not in ("Personal", "Lunch")):
            drive = {"minutes": d["minutes"], "miles_approx": d["miles_approx"], "notes": d["notes"]}
        used, consumed = _split_parts(s["parts"])
        out_stops.append({
            "stop_number": len(out_stops) + 1,
            "business_name": s["business_name"], "address": s["address"],
            "place_type": _place_type(s["category"]),
            "latitude": s["lat"], "longitude": s["lon"],
            "arrival": s["arrive"], "departure": s["depart"],
            "arrival_iso": iso(s["arrive_ts"]), "departure_iso": iso(s["depart_ts"]),
            "time_on_site_minutes": s["minutes"],
            "drive_from_previous_stop": drive,
            "hubspot_company_id": s["hubspot_company_id"],
            "work_performed": s["notes"],
            "parts_used": used,
            "consumables_used": consumed,
        })
    drive_min = sum(s["drive_from_previous_stop"]["minutes"] for s in out_stops if s["drive_from_previous_stop"])
    miles = sum(s["drive_from_previous_stop"]["miles_approx"] for s in out_stops if s["drive_from_previous_stop"])
    traffic = sum(s["minutes"] for s in stops if s["status"] == "Traffic")
    drive_min += traffic
    final = None   # last drive of the day into a Personal stop (e.g. home), counted via End day
    if drives and drives[-1].get("counts") and status.get(drives[-1]["to_stop_id"]) == "Personal":
        final = {"minutes": drives[-1]["minutes"], "miles_approx": drives[-1]["miles_approx"]}
        drive_min += final["minutes"]
        miles += final["miles_approx"]
    marks = payload.get("marks") or {}
    return {"date": payload["date"],
            "day_start": marks.get("start", ""), "day_end": marks.get("end", ""),
            "summary": {"on_site_minutes": sum(s["time_on_site_minutes"] for s in out_stops),
                        "drive_minutes": drive_min, "traffic_minutes": traffic, "miles_approx": round(miles, 1),
                        "stops_confirmed": len(out_stops), "stops_excluded": len(excluded)},
            "drive_after_last_stop": final,
            "stops": out_stops, "excluded": excluded}


def records_to_update(days):
    rows = []
    for day in days:
        for s in day["stops"]:
            rows.append({"date": day["date"], "stop_number": s["stop_number"],
                         "business_name": s["business_name"], "address": s["address"],
                         "arrival": s["arrival"], "departure": s["departure"],
                         "work_performed": s["work_performed"],
                         "parts_used": [f"{p['quantity']:g} x {p['name']}" + (f" ({p['part_number']})" if p["part_number"] else "")
                                        for p in s["parts_used"]],
                         "hubspot_company_id": s["hubspot_company_id"]})
    return rows


def build(conn, cfg, dates, ensure, now=None):
    """`ensure(conn, cfg, date)` makes sure a day has been detected (it may fetch points)."""
    days = []
    for date in dates:
        ensure(conn, cfg, date)
        days.append(build_day(service.day_payload(conn, cfg, date), cfg))
    now = now or datetime.now(ZoneInfo(cfg.app_tz))
    return {"schema_version": SCHEMA_VERSION, "report": "JobHub report",
            "date_from": dates[0], "date_to": dates[-1], "timezone": cfg.app_tz,
            "generated_at": now.isoformat(timespec="seconds"),
            "days": days, "records_to_update": records_to_update(days)}


def check(report):
    """Counts shown before exporting so drafts don't get left out by surprise."""
    out = {"days": [], "confirmed": 0, "not_confirmed": 0, "personal": 0}
    for d in report["days"]:
        nc = sum(1 for e in d["excluded"] if e["reason"].startswith("Not confirmed"))
        pe = sum(1 for e in d["excluded"] if e["reason"] in ("Personal", "Lunch") or e["reason"].startswith("Traffic"))
        out["days"].append({"date": d["date"], "confirmed": d["summary"]["stops_confirmed"],
                            "not_confirmed": nc, "personal": pe})
        out["confirmed"] += d["summary"]["stops_confirmed"]
        out["not_confirmed"] += nc
        out["personal"] += pe
    return out
