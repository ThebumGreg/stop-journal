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


def _timesheet(stops, drives):
    """The day in time order, as paid and unpaid rows: on-site visits, counted drives, traffic (drive time), lunch
    (PAID, not billed to the customer) and personal breaks (unpaid: clocked out). Personal stops that are not between
    two work stops (home before and after the day) are not listed. A lunch or break with no counted drives around it
    covers the whole gap (leave the last site, arrive at the next), so a trip back to the same site is one row."""
    def name(s):
        return {"Personal": "Home", "Lunch": "Lunch"}.get(s["status"]) or s["business_name"] or f"Stop at {s['arrive']}"

    by_id = {s["id"]: s for s in stops}
    by_from = {d["from_stop_id"]: d for d in drives}
    by_to = {d["to_stop_id"]: d for d in drives}
    rows = []

    def add(ts, inn, out, kind, text, secs, paid):
        rows.append((ts, {"in": inn, "out": out, "type": kind, "description": text,
                          "minutes": round(secs / 60), "paid": paid}))

    LUNCH, BREAK = "Lunch (paid, not billed)", "Clocked out (personal break)"
    for s in stops:
        st = s["status"]
        if st == "Confirmed":
            add(s["arrive_ts"], s["arrive"], s["depart"], "on_site", name(s), s["depart_ts"] - s["arrive_ts"], True)
        elif st == "Traffic":
            add(s["arrive_ts"], s["arrive"], s["depart"], "drive", "Traffic (counted as drive time)",
                s["depart_ts"] - s["arrive_ts"], True)
        elif st not in ("Personal", "Lunch"):
            add(s["arrive_ts"], s["arrive"], s["depart"], "unconfirmed", "Not confirmed (left out)",
                s["depart_ts"] - s["arrive_ts"], False)
    conf = [i for i, s in enumerate(stops) if s["status"] == "Confirmed"]
    for xi, yi in zip(conf, conf[1:]):
        x, y = stops[xi], stops[yi]
        gap = [stops[k] for k in range(xi + 1, yi) if stops[k]["status"] in ("Lunch", "Personal")]
        if not gap:
            continue
        has_p = any(g["status"] == "Personal" for g in gap)
        has_l = any(g["status"] == "Lunch" for g in gap)
        if has_p and not has_l:                      # clocked out from leaving one job until arriving at the next
            add(x["depart_ts"], x["depart"], y["arrive"], "break", BREAK, y["arrive_ts"] - x["depart_ts"], False)
        elif has_l and not has_p and len(gap) == 1:
            g = gap[0]
            dp, dn = by_to.get(g["id"]), by_from.get(g["id"])
            if (dp and dp["counts"]) or (dn and dn["counts"]):      # lunch between jobs: the drives are listed
                add(g["arrive_ts"], g["arrive"], g["depart"], "lunch", LUNCH, g["depart_ts"] - g["arrive_ts"], True)
            else:                                                   # back to the same site: the whole gap
                add(x["depart_ts"], x["depart"], y["arrive"], "lunch", LUNCH, y["arrive_ts"] - x["depart_ts"], True)
        else:                                        # lunch and a personal stop together: each by its own times
            for g in gap:
                lunch = g["status"] == "Lunch"
                add(g["arrive_ts"], g["arrive"], g["depart"], "lunch" if lunch else "break",
                    LUNCH if lunch else BREAK, g["depart_ts"] - g["arrive_ts"], lunch)
    for d in drives:
        if d["counts"]:
            frm, to = by_id[d["from_stop_id"]], by_id[d["to_stop_id"]]
            add(d["depart_ts"], d["depart"], d["arrive"], "drive", f"Drive: {name(frm)} to {name(to)}",
                d["arrive_ts"] - d["depart_ts"], True)
    return [r for _, r in sorted(rows, key=lambda x: x[0])]


def build_day(payload, cfg):
    """payload: service.day_payload() for one date."""
    tz = ZoneInfo(cfg.app_tz)
    stops, drives = payload["stops"], payload["drives"]
    status = {s["id"]: s["status"] for s in stops}
    drive_into = {d["to_stop_id"]: d for d in drives}

    def iso(ts):
        return datetime.fromtimestamp(ts, tz).isoformat(timespec="seconds")

    end_day = payload.get("end_day")
    end_id = end_day["stop_id"] if end_day else None
    leg_into = {lg["to_id"]: lg for lg in ((payload.get("mileage") or {}).get("legs") or []) if lg.get("to_id")}
    # A visit, a lunch trip, then the same site again is ONE stop: lunch time is shown as non-billable
    absorbed, merged = set(), {}
    for i, s in enumerate(stops):
        if s["status"] != "Confirmed" or s["id"] in absorbed:
            continue
        cur = i
        while (cur + 2 < len(stops) and stops[cur + 1]["status"] in ("Lunch", "Personal") and stops[cur + 2]["status"] == "Confirmed"
               and service.same_site(s, stops[cur + 2])):
            left, nxt = stops[cur], stops[cur + 2]
            m = merged.setdefault(s["id"], {"gaps": [], "notes": [s["notes"]], "parts": list(s["parts"]),
                                            "minutes": s["minutes"]})
            m["gaps"].append({"kind": stops[cur + 1]["status"], "from": left["depart"], "to": nxt["arrive"],
                              "secs": nxt["arrive_ts"] - left["depart_ts"]})
            m["notes"].append(nxt["notes"])
            m["parts"] += nxt["parts"]
            m["minutes"] += nxt["minutes"]
            m["depart"], m["depart_ts"] = nxt["depart"], nxt["depart_ts"]
            absorbed.add(nxt["id"])
            cur += 2
    out_stops, excluded = [], []
    for s in stops:
        if s["id"] in absorbed:
            continue
        lunch, breaks = [], []
        if s["id"] in merged:
            m = merged[s["id"]]
            lunch = [{"from": g["from"], "to": g["to"], "minutes": round(g["secs"] / 60)}
                     for g in m["gaps"] if g["kind"] == "Lunch"]
            breaks = [{"from": g["from"], "to": g["to"], "minutes": round(g["secs"] / 60)}
                      for g in m["gaps"] if g["kind"] == "Personal"]
            s = dict(s, depart=m["depart"], depart_ts=m["depart_ts"], parts=m["parts"],
                     notes="\n\n".join(n for n in m["notes"] if n),
                     minutes=m["minutes"])
        if s["status"] != "Confirmed":
            reason = {"Personal": "End Day" if s["id"] == end_id else "Personal", "Lunch": "Lunch",
                      "Traffic": "Traffic (counted as drive time)"}.get(s["status"]) or \
                "Not confirmed" + (" (short stop)" if s["kind"] == "short" else "")
            excluded.append({"arrival": s["arrive"], "departure": s["depart"], "reason": reason})
            continue
        d = drive_into.get(s["id"])
        counted = bool(d and d.get("counts", status.get(d["from_stop_id"]) not in ("Personal", "Lunch")))
        leg = leg_into.get(s["id"])
        drive = None
        if leg:      # the Home-to-Home mileage leg that ends at this stop; minutes only when it counts as work time
            minutes = d["minutes"] if counted else None
            if counted and status.get(d["from_stop_id"]) == "Lunch":      # lunch between jobs: one drive before and after it
                d0 = drive_into.get(d["from_stop_id"])
                if d0 and d0.get("counts"):
                    minutes += d0["minutes"]
            drive = {"minutes": minutes, "miles_approx": leg["miles"],
                     "from": leg["from"], "notes": d["notes"] if d else ""}
        elif counted:
            drive = {"minutes": d["minutes"], "miles_approx": d["miles_approx"], "from": "", "notes": d["notes"]}
        used, consumed = _split_parts(s["parts"])
        out_stops.append({
            "stop_number": len(out_stops) + 1,
            "business_name": s["business_name"], "address": s["address"],
            "place_type": _place_type(s["category"]),
            "latitude": s["lat"], "longitude": s["lon"],
            "arrival": s["arrive"], "departure": s["depart"],
            "arrival_iso": iso(s["arrive_ts"]), "departure_iso": iso(s["depart_ts"]),
            "time_on_site_minutes": s["minutes"],
            "lunch_non_billable": lunch,
            "personal_breaks_unpaid": breaks,
            "drive_from_previous_stop": drive,
            "hubspot_company_id": s["hubspot_company_id"],
            "work_performed": s["notes"],
            "parts_used": used,
            "consumables_used": consumed,
        })
    drive_min = sum(s["drive_from_previous_stop"]["minutes"] or 0 for s in out_stops if s["drive_from_previous_stop"])
    miles = sum(s["drive_from_previous_stop"]["miles_approx"] for s in out_stops if s["drive_from_previous_stop"])
    traffic = sum(s["minutes"] for s in stops if s["status"] == "Traffic")
    drive_min += traffic
    final = None   # the drive into the Personal stop where the day ended (home), counted when the day ends there
    fd = drive_into.get(end_id) if end_id else None
    if fd and fd.get("counts") and status.get(fd["to_stop_id"]) == "Personal":
        final = {"minutes": fd["minutes"], "miles_approx": fd["miles_approx"]}
        drive_min += final["minutes"]
        miles += final["miles_approx"]
    sheet = _timesheet(stops, drives)
    lunch_min = sum(r["minutes"] for r in sheet if r["type"] == "lunch")
    break_min = sum(r["minutes"] for r in sheet if r["type"] == "break")
    mil = payload.get("mileage")
    if mil:
        miles = mil["total_miles"]     # billable mileage is always Home to Home, personal stops left out
    marks = payload.get("marks") or {}
    return {"date": payload["date"],
            "day_start": marks.get("start", ""), "day_end": marks.get("end", ""),
            "summary": {"on_site_minutes": sum(s["time_on_site_minutes"] for s in out_stops),
                        "drive_minutes": drive_min, "traffic_minutes": traffic,
                        "lunch_minutes": lunch_min, "break_minutes_unpaid": break_min,
                        "paid_minutes": sum(s["time_on_site_minutes"] for s in out_stops) + drive_min + lunch_min, "miles_approx": round(miles, 1),
                        "stops_confirmed": len(out_stops), "stops_excluded": len(excluded)},
            "drive_after_last_stop": final,
            "timesheet": sheet,
            "mileage": mil,
            "end_day": end_day and {k: end_day[k] for k in ("time", "kind", "place", "miles_to_home", "method", "counted")},
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
    return {"schema_version": SCHEMA_VERSION, "report": "JobHub daily summary",
            "date_from": dates[0], "date_to": dates[-1], "timezone": cfg.app_tz,
            "generated_at": now.isoformat(timespec="seconds"),
            "days": days, "records_to_update": records_to_update(days)}


def check(report):
    """Counts shown before exporting so drafts don't get left out by surprise."""
    out = {"days": [], "confirmed": 0, "not_confirmed": 0, "personal": 0}
    for d in report["days"]:
        nc = sum(1 for e in d["excluded"] if e["reason"].startswith("Not confirmed"))
        pe = sum(1 for e in d["excluded"] if e["reason"] in ("Personal", "Lunch", "End Day") or e["reason"].startswith("Traffic"))
        out["days"].append({"date": d["date"], "confirmed": d["summary"]["stops_confirmed"],
                            "not_confirmed": nc, "personal": pe})
        out["confirmed"] += d["summary"]["stops_confirmed"]
        out["not_confirmed"] += nc
        out["personal"] += pe
    return out
