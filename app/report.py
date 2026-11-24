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
            "unit_cost": p["unit_cost"], "line_cost": p["line_cost"], "billable": p["billable"],
            "qbo_item": p.get("qbo_item") or "", "notes": p.get("notes") or ""}


TICKET_ACTIONS = {"continue": "continue", "new": "create", "unsure": "find"}


def _ticket_action(s):
    """What the agent should do about the HubSpot ticket: use (an ID is given), continue (find the open ticket for this
    company), create (new job: make a ticket), find (one open ticket: use it; none: create one and flag it)."""
    if (s.get("ticket") or "").strip():
        return "use"
    choice = s.get("ticket_action") or s.get("ticket_action_default") or ""
    return TICKET_ACTIONS.get(choice, "find")


def attachment_name(file):
    """The file name a report picture gets as an email attachment (its zip path with / turned into _). The JSON's
    photos[].attachment and the mailer both use this, so they cannot drift apart."""
    return file.replace("/", "_")


def _split_parts(lines):
    """Parts go to `parts_used`; consumables (tape, solvent...) to `consumables_used`, so the
    reader does not mistake them for billable materials; expenses (parking, tolls) to `expenses`."""
    return ([_part(p) for p in lines if not p.get("consumable") and not p.get("expense")],
            [_part(p) for p in lines if p.get("consumable")],
            [_part(p) for p in lines if p.get("expense")])


def _slug(name, n=30):
    """'Acme Hardware & Co.' -> 'acme-hardware-co' (for file names)."""
    out = "".join(c.lower() if c.isalnum() else "-" for c in (name or ""))
    out = "-".join(x for x in out.split("-") if x)[:n].strip("-")
    return out or "no-name"


def _line_total(p):
    return p["line_cost"] if p["line_cost"] is not None else 0.0


NO_CATEGORY = "No category"
PLACE_TYPE = {"home": "Home", "office": "Office", "vendor": "Vendor", "client": "Client"}


def _timesheet(stops, drives, lunch_paid=True):
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

    LUNCH, BREAK = "Lunch", "Clocked out (personal break)"
    for s in stops:
        st = s["status"]
        if st == "Confirmed":
            add(s["arrive_ts"], s["arrive"], s["depart"], "on_site", name(s), s["depart_ts"] - s["arrive_ts"], True)
        elif st == "Traffic":
            text = (f"Fuel stop{' (' + s['business_name'] + ')' if s['business_name'] else ''}, counted as drive time"
                    if s.get("stop_type") == "gas" else "Short break (paid, counted as drive time)"
                    if s.get("stop_type") == "break" else "Traffic (counted as drive time)")
            add(s["arrive_ts"], s["arrive"], s["depart"], "drive", text, s["depart_ts"] - s["arrive_ts"], True)
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
                add(g["arrive_ts"], g["arrive"], g["depart"], "lunch", LUNCH, g["depart_ts"] - g["arrive_ts"], lunch_paid)
            else:                                                   # back to the same site: the whole gap
                add(x["depart_ts"], x["depart"], y["arrive"], "lunch", LUNCH, y["arrive_ts"] - x["depart_ts"], lunch_paid)
        else:                                        # lunch and a personal stop together: each by its own times
            for g in gap:
                lunch = g["status"] == "Lunch"
                add(g["arrive_ts"], g["arrive"], g["depart"], "lunch" if lunch else "break",
                    LUNCH if lunch else BREAK, g["depart_ts"] - g["arrive_ts"], lunch and lunch_paid)
    for d in drives:
        if d["counts"]:
            frm, to = by_id[d["from_stop_id"]], by_id[d["to_stop_id"]]
            add(d["depart_ts"], d["depart"], d["arrive"], "drive",
                f"Drive: {name(frm)} to {name(to)}" + (" (estimated, no location data)" if d.get("estimated") else ""),
                d["arrive_ts"] - d["depart_ts"], True)
    return [r for _, r in sorted(rows, key=lambda x: x[0])]


def build_day(payload, cfg):
    """payload: service.day_payload() for one date."""
    tz = ZoneInfo(cfg.app_tz)
    stops, drives = payload["stops"], payload["drives"]
    status = {s["id"]: s["status"] for s in stops}
    from .settings import owner_emails
    owners = owner_emails(cfg)
    drive_into = {d["to_stop_id"]: d for d in drives}

    def iso(ts):
        return datetime.fromtimestamp(ts, tz).isoformat(timespec="seconds")

    def chain_minutes(d):
        """Minutes of a counted drive plus every counted drive before it through Lunch and Traffic-status stops
        (gas, short break, traffic): a work stop's drive covers the whole trip from the previous work stop. The time
        spent AT those stops is not included (traffic time is added separately; lunch is lunch)."""
        minutes, prev, seen = d["minutes"], d["from_stop_id"], set()
        while status.get(prev) in ("Lunch", "Traffic") and prev not in seen:
            seen.add(prev)
            d0 = drive_into.get(prev)
            if not d0 or not d0.get("counts"):
                break
            minutes += d0["minutes"]
            prev = d0["from_stop_id"]
        return minutes

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
                                            "minutes": s["minutes"], "follow_ups": list(s["follow_ups"]),
                                            "ticket": s["ticket"], "qbo_estimate": s.get("qbo_estimate", ""),
                                            "visit_result": s["visit_result"],
                                            "work_category": s["work_category"], "photos": list(s.get("photos") or [])})
            m["gaps"].append({"kind": stops[cur + 1]["status"], "from": left["depart"], "to": nxt["arrive"],
                              "secs": nxt["arrive_ts"] - left["depart_ts"]})
            m["notes"].append(nxt["notes"])
            m["parts"] += nxt["parts"]
            m["follow_ups"] += nxt["follow_ups"]
            m["photos"] += nxt.get("photos") or []
            m["ticket"] = m["ticket"] or nxt["ticket"]
            m["qbo_estimate"] = m["qbo_estimate"] or nxt.get("qbo_estimate", "")
            m["work_category"] = m["work_category"] or nxt["work_category"]
            m["visit_result"] = nxt["visit_result"] or m["visit_result"]
            m["minutes"] += nxt["minutes"]
            m["depart"], m["depart_ts"] = nxt["depart"], nxt["depart_ts"]
            absorbed.add(nxt["id"])
            cur += 2
    out_stops, excluded, pic_from = [], [], []   # pic_from: (label, photos) per confirmed stop and fill-up
    prev_type = "Home"          # what kind of place the previous work stop was (the first leg starts at Home)
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
                     minutes=m["minutes"], follow_ups=m["follow_ups"], ticket=m["ticket"], qbo_estimate=m["qbo_estimate"],
                     visit_result=m["visit_result"], work_category=m["work_category"], photos=m["photos"])
        if s["status"] != "Confirmed":
            reason = {"Personal": "End Day" if s["id"] == end_id else "Personal", "Lunch": "Lunch",
                      "Traffic": "Fuel stop (counted as drive time)" if s.get("stop_type") == "gas"
                      else "Short break (paid, counted as drive time)" if s.get("stop_type") == "break"
                      else "Traffic (counted as drive time)"}.get(s["status"]) or \
                "Not confirmed" + (" (short stop)" if s["kind"] == "short" else "")
            # End Day: only when the day ended matters (the time spent at home afterwards means nothing)
            excluded.append({"arrival": s["arrive"], "departure": "" if reason == "End Day" else s["depart"],
                             "reason": reason})
            continue
        d = drive_into.get(s["id"])
        counted = bool(d and d.get("counts", status.get(d["from_stop_id"]) not in ("Personal", "Lunch")))
        leg = leg_into.get(s["id"])
        drive = None
        if leg:      # the Home-to-Home mileage leg that ends at this stop; minutes only when it counts as work time
            minutes = chain_minutes(d) if counted else None    # lunch / fuel / break between jobs: the whole trip
            drive = {"minutes": minutes, "miles_approx": leg["miles"], "miles_method": leg.get("method", ""),
                     "from": leg["from"], "from_type": "Home" if leg["from"] == "Home" else prev_type,
                     "notes": d["notes"] if d else "", **_gap_fields(d, counted)}
        elif counted:
            drive = {"minutes": chain_minutes(d), "miles_approx": d["miles_approx"], "miles_method": "gps",
                     "from": "", "from_type": "",
                     "notes": d["notes"], **_gap_fields(d, counted)}
        used, consumed, spent = _split_parts(s["parts"])
        cat = s["work_category"]
        prev_type = PLACE_TYPE.get(s.get("site_kind"), "Client")
        out_stops.append({
            "stop_number": len(out_stops) + 1,
            "stop_uid": s.get("uid") or "",
            "added_from_receipt": bool(s.get("from_receipt")),
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
            "category": cat,
            "billing": service.WORK_CATEGORIES.get(cat, ""),
            "job_reference": s["ticket"],
            "hubspot_ticket": (s["ticket"] or "").strip().lstrip("#").strip(),
            "ticket_action": _ticket_action(s),
            "qbo_estimate": s.get("qbo_estimate") or "",
            "visit_result": service.VISIT_RESULTS.get(s["visit_result"], ""),
            "work_performed": s["notes"],
            "parts_used": used,
            "consumables_used": consumed,
            "expenses": spent,
            "bought_here": [{"name": b["name"], "part_number": b["sku"], "quantity": b["qty"], "unit": b["unit"],
                             "paid": b["paid"]} for b in s.get("bought") or []],
            "follow_ups": [{"what": f["text"], "owner": f["owner"],
                            "owner_email": owners.get((f["owner"] or "").strip().lower(), ("", ""))[1],
                            "due": f["due"], "follow_up_id": f.get("id")}
                           for f in s["follow_ups"]],
            "photos": [p["id"] for p in s.get("photos") or []],
            # sent for invoice from the stop (Send for invoice): the agent already invoiced this; flag differences
            "invoice_request": ({k: s["invoice_request"][k] for k in ("version", "sent_at", "billed_through",
                                                                      "time_on_site_minutes")}
                                if s.get("invoice_request") else None),
        })
        pic_from.append((f"Stop {len(out_stops)}, {s['business_name'] or 'no name'}, {s['arrive']}",
                         f"stop-{len(out_stops)}-{_slug(s['business_name'])}", s.get("photos") or []))
    drive_min = sum(s["drive_from_previous_stop"]["minutes"] or 0 for s in out_stops if s["drive_from_previous_stop"])
    miles = sum(s["drive_from_previous_stop"]["miles_approx"] for s in out_stops if s["drive_from_previous_stop"])
    traffic = sum(s["minutes"] for s in stops if s["status"] == "Traffic")
    fuel_stop_min = sum(s["minutes"] for s in stops if s["status"] == "Traffic" and s.get("stop_type") == "gas")
    short_break_min = sum(s["minutes"] for s in stops if s["status"] == "Traffic" and s.get("stop_type") == "break")
    drive_min += traffic
    final = None   # the drive into the Personal stop where the day ended (home), counted when the day ends there
    fd = drive_into.get(end_id) if end_id else None
    if fd and fd.get("counts") and status.get(fd["to_stop_id"]) == "Personal":
        final = {"minutes": chain_minutes(fd), "miles_approx": fd["miles_approx"]}
        drive_min += final["minutes"]
        miles += final["miles_approx"]
    lunch_paid = getattr(cfg, "lunch_paid", "on") != "off"
    sheet = _timesheet(stops, drives, lunch_paid)
    lunch_min = sum(r["minutes"] for r in sheet if r["type"] == "lunch")
    break_min = sum(r["minutes"] for r in sheet if r["type"] == "break")
    mil = payload.get("mileage")
    if mil:
        miles = mil["total_miles"]     # billable mileage is always Home to Home, personal stops left out
    marks = payload.get("marks") or {}
    by_cat = {}
    for o in out_stops:
        row = by_cat.setdefault(o["category"] or NO_CATEGORY, {"category": o["category"] or NO_CATEGORY,
                                                                "billing": o["billing"], "on_site_minutes": 0})
        row["on_site_minutes"] += o["time_on_site_minutes"]
    expenses_total = round(sum(_line_total(e) for o in out_stops for e in o["expenses"]), 2)
    bought_total = round(sum(b["paid"] or 0 for o in out_stops for b in o["bought_here"]), 2)
    # pictures of confirmed stops and fill-ups only (never of Personal, Lunch or unconfirmed stops)
    pic_from += [(f"Fuel stop{', ' + s['business_name'] if s['business_name'] else ''}, {s['arrive']}",
                  f"fuel-{s['arrive'].replace(':', '')}-{_slug(s['business_name'])}", s.get("photos") or [])
                 for s in stops if s.get("stop_type") == "gas" and s["status"] == "Traffic"]
    y, mo, dd = payload["date"].split("-")
    pics = []
    for where, stem, ps in pic_from:
        for k, p in enumerate(ps, 1):
            # the same name the picture has in the zip export: photos/YYYY/MM/DD/stop-1-acme-receipt-1.jpg
            f = f"photos/{y}/{mo}/{dd}/{stem.strip('-')}-{p['kind']}-{k}.jpg"
            pics.append({"id": p["id"], "kind": p["kind"], "caption": p["caption"],
                         "file": f, "attachment": attachment_name(f),
                         "label": f"{'Receipt' if p['kind'] == 'receipt' else 'Photo'}: {where}" +
                                  (f". {p['caption']}" if p["caption"] else "")})
    fuel = [{"arrival": s["arrive"], "departure": s["depart"], "station": s["business_name"] or "",
             "added_from_receipt": bool(s.get("from_receipt")),
             **{k: (s.get("fuel") or {}).get(k) for k in ("gallons", "total", "price_per_gallon", "odometer")}}
            for s in stops if s.get("stop_type") == "gas" and s["status"] == "Traffic"]
    return {"date": payload["date"], "report_key": payload["date"],
            "day_start": marks.get("start", ""), "day_end": marks.get("end", ""),
            "summary": {"on_site_minutes": sum(s["time_on_site_minutes"] for s in out_stops),
                        "billable_on_site_minutes": sum(s["time_on_site_minutes"] for s in out_stops
                                                        if s["billing"] != "non_billable"),
                        "drive_minutes": drive_min, "traffic_minutes": traffic,
                        "fuel_stop_minutes": fuel_stop_min, "short_break_minutes": short_break_min,
                        "lunch_minutes": lunch_min, "break_minutes_unpaid": break_min,
                        "paid_minutes": sum(s["time_on_site_minutes"] for s in out_stops) + drive_min + (lunch_min if lunch_paid else 0),
                        "lunch_paid": lunch_paid, "miles_approx": round(miles, 1),
                        "stops_confirmed": len(out_stops), "stops_excluded": len(excluded),
                        "hours_by_category": list(by_cat.values()), "expenses_total": expenses_total,
                        "bought_total": bought_total,
                        "fuel_total": round(sum(f["total"] or 0 for f in fuel), 2)},
            "fuel": fuel,
            "photos": pics,
            "drive_after_last_stop": final,
            "timesheet": sheet,
            "mileage": mil,
            "end_day": end_day and {k: end_day[k] for k in ("time", "kind", "place", "miles_to_home", "method", "counted")},
            "stops": out_stops, "excluded": excluded}


def _line_text(p):
    return f"{p['quantity']:g} x {p['name']}" + (f" ({p['part_number']})" if p["part_number"] else "")


def records_to_update(days, technician=None):
    rows = []
    for day in days:
        att = {p["id"]: p["attachment"] for p in day.get("photos") or []}
        for s in day["stops"]:
            rows.append({"date": day["date"], "stop_number": s["stop_number"], "stop_uid": s["stop_uid"],
                         "business_name": s["business_name"], "address": s["address"],
                         "arrival": s["arrival"], "departure": s["departure"],
                         "category": s["category"], "job_reference": s["job_reference"],
                         "hubspot_ticket": s["hubspot_ticket"], "qbo_estimate": s["qbo_estimate"],
                         "ticket_action": s["ticket_action"],
                         "work_performed": s["work_performed"],
                         "parts_used": [_line_text(p) for p in s["parts_used"]],
                         "hubspot_company_id": s["hubspot_company_id"],
                         "billing": s["billing"], "visit_result": s["visit_result"],
                         "time_on_site_minutes": s["time_on_site_minutes"],
                         "technician_email": (technician or {}).get("email", ""),
                         "consumables_used": [_line_text(p) for p in s.get("consumables_used") or []],
                         "expenses": [p["name"] + (f" ({p['notes']})" if p.get("notes") else "")
                                      + (f" ${p['line_cost']:.2f}" if p["line_cost"] is not None else "")
                                      for p in s.get("expenses") or []],
                         "bought_here": [f"{b['quantity']:g} x {b['name']}" + (f" ({b['part_number']})" if b["part_number"] else "")
                                         + (f" ${b['paid']:.2f}" if b.get("paid") is not None else "")
                                         for b in s.get("bought_here") or []],
                         "photo_attachments": [att[i] for i in s.get("photos") or [] if i in att]})
    return rows


def follow_ups(days):
    rows = []
    for day in days:
        for s in day["stops"]:
            for f in s["follow_ups"]:
                rows.append({"date": day["date"], "stop_number": s["stop_number"], "stop_uid": s["stop_uid"],
                             "business_name": s["business_name"], **f})
    return rows


def _gap_fields(d, counted):
    """Additive drive fields: `estimated` when the phone recorded nothing for part of the trip and the minutes are
    distance-based, and the stretch with no data."""
    gap = d and d.get("gap")
    est = bool(counted and gap and d.get("estimated"))
    # minutes_estimated is the same as estimated, under a name that says it is about the MINUTES (the miles have their
    # own miles_method: gps = recorded drive, routed = road route, estimate = straight line x1.25)
    return {"estimated": est, "minutes_estimated": est,
            "no_location_data": {"from": gap["from"], "to": gap["to"]} if gap else None}


LENGTH_WORDS = ("cable", "wire", "conduit", "pipe", "tubing", "hose", "rope", "tape measure")


REFERENCE_CATEGORIES = ("Time & Material", "Quoted project", "White-label migration")
ESTIMATE_CATEGORIES = ("Quoted project", "White-label migration")


def warnings(report):
    """Things worth a look before exporting. They never block: each names a stop or follow-up."""
    out = []
    tech = report.get("technician") or {}
    if not tech.get("name") or not tech.get("email"):
        out.append("Technician " + ("name and email are" if not tech.get("name") and not tech.get("email")
                                    else "name is" if not tech.get("name") else "email is")
                   + " not set (Settings → General → Technician): the report does not say who did the work.")
    for day in report["days"]:
        for s in day["stops"]:
            who = f"{day['date']} stop {s['stop_number']} ({s['business_name']})"
            if not s["category"]:
                out.append(f"{who}: no category chosen.")
            if not s["work_performed"].strip():
                out.append(f"{who}: no work performed written.")
            if s["hubspot_ticket"] and not s["hubspot_ticket"].isdigit():
                out.append(f"{who}: HubSpot ticket “{s['hubspot_ticket']}” does not look like a HubSpot ticket ID or link "
                           "(paste the ticket's link, or leave it blank and the agent finds or creates the ticket).")
            if s["category"] in REFERENCE_CATEGORIES:
                if s["category"] in ESTIMATE_CATEGORIES and not s["qbo_estimate"]:
                    out.append(f"{who}: no QuickBooks estimate # (a {s['category']} job).")
            if s["billing"] != "non_billable" and not s["visit_result"]:
                out.append(f"{who}: no visit result (complete, return visit or waiting).")
            dv = s["drive_from_previous_stop"] or {}
            if dv.get("estimated"):
                nd = dv["no_location_data"]
                out.append(f"{who}: the drive time is an estimate (no location data from {nd['from']} to {nd['to']}). "
                           "Correct the departure and arrival times if you remember them.")
            for p in s["parts_used"]:
                if p["billable"] and not p["unit_cost"]:
                    out.append(f"{who}: billable part “{p['name']}” has no unit cost.")
                if p["billable"] and not p["part_number"]:
                    out.append(f"{who}: billable part “{p['name']}” has no part number (QuickBooks matches items by SKU).")
                if (p["unit"] or "ea") == "ea" and any(w in p["name"].lower() for w in LENGTH_WORDS):
                    out.append(f"{who}: “{p['name']}” is counted in each; is the quantity really a count (not feet)?")
            for f in s["follow_ups"]:
                miss = [w for w, v in (("an owner", f["owner"]), ("a due date", f["due"])) if not v]
                if miss:
                    out.append(f"{who}: follow-up “{f['what']}” has no {' or '.join(m.split(' ', 1)[1] for m in miss)}.")
                if f["owner"] and not f.get("owner_email"):
                    out.append(f"{who}: follow-up “{f['what']}”: " + (
                        "“Me” has no email (set Technician email in Settings → General)." if f["owner"].strip().lower() == "me"
                        else f"owner “{f['owner']}” is not in Settings → General → Follow-up owners, so it has no email."))
        for f in day.get("fuel") or []:
            if not f["total"]:
                out.append(f"{day['date']} fuel stop at {f['arrival']}: no total paid entered.")
        if day["stops"] and day.get("day_end"):
            last = max(st["departure"] for st in day["stops"])
            if day["day_end"] < last:
                out.append(f"{day['date']}: End Day is {day['day_end']} but the last stop departs {last}. "
                           "Change End Day, or the stop's departure, so they agree.")
    return out


def build(conn, cfg, dates, ensure, now=None):
    """`ensure(conn, cfg, date)` makes sure a day has been detected (it may fetch points)."""
    days = []
    for date in dates:
        ensure(conn, cfg, date)
        days.append(build_day(service.day_payload(conn, cfg, date), cfg))
    now = now or datetime.now(ZoneInfo(cfg.app_tz))
    return {"schema_version": SCHEMA_VERSION, "report": "JobHub daily summary",
            "technician": {"name": cfg.tech_name, "email": cfg.tech_email},
            "date_from": dates[0], "date_to": dates[-1], "timezone": cfg.app_tz,
            "generated_at": now.isoformat(timespec="seconds"),
            "days": days, "records_to_update": records_to_update(days, {"name": cfg.tech_name, "email": cfg.tech_email}),
            "follow_ups": follow_ups(days)}


# Sections the user can leave out of an export (Export page "Include" ticks). The report says which were
# left out (`omitted`, and a line in the PDF's reading notes) so a reader never takes a missing section for "none".
OMIT_SECTIONS = {
    "costs": "Part and material costs",
    "expenses": "Expenses",
    "fuel": "Fuel",
    "photos": "Receipts and photos",
    "materials": "Non-billable materials",
    "categories": "Hours by category",
    "follow_ups": "Follow-ups",
    "mileage": "Mileage table",
    "recap": "Day recap (timesheet)",
    "coordinates": "GPS coordinates",
}


def parse_omit(text):
    keys = [k.strip() for k in (text or "").split(",") if k.strip()]
    bad = [k for k in keys if k not in OMIT_SECTIONS]
    if bad:
        raise EditError("Unknown export section: " + ", ".join(bad))
    return [k for k in OMIT_SECTIONS if k in keys]      # fixed order


def apply_omit(report, omit):
    """Strip the chosen sections from a built report (in place) and record them in `omitted`."""
    report["omitted"] = [{"key": k, "label": OMIT_SECTIONS[k]} for k in omit]
    for day in report["days"]:
        sm = day["summary"]
        for s in day["stops"]:
            if "costs" in omit:
                for p in s["parts_used"] + s["consumables_used"]:
                    p.pop("unit_cost", None); p.pop("line_cost", None)
                for b in s.get("bought_here") or []:
                    b.pop("paid", None)
            if "expenses" in omit:
                s.pop("expenses", None)
            if "materials" in omit:
                s.pop("consumables_used", None)
            if "follow_ups" in omit:
                s.pop("follow_ups", None)
            if "coordinates" in omit:
                s.pop("latitude", None); s.pop("longitude", None)
        if "expenses" in omit:
            sm.pop("expenses_total", None)
        if "costs" in omit:
            sm.pop("bought_total", None)
        if "categories" in omit:
            sm.pop("hours_by_category", None)
        if "fuel" in omit:
            sm.pop("fuel_total", None)
            day.pop("fuel", None)
        if "mileage" in omit:
            day.pop("mileage", None)
        if "photos" in omit:
            day.pop("photos", None)
            for s in day["stops"]:
                s.pop("photos", None)
        if "recap" in omit:
            day.pop("timesheet", None)
    if "follow_ups" in omit:
        report.pop("follow_ups", None)
    return report


def check(report):
    """Counts shown before exporting so drafts don't get left out by surprise."""
    out = {"days": [], "confirmed": 0, "not_confirmed": 0, "personal": 0}
    for d in report["days"]:
        nc = sum(1 for e in d["excluded"] if e["reason"].startswith("Not confirmed"))
        pe = sum(1 for e in d["excluded"] if e["reason"] in ("Personal", "Lunch", "End Day") or e["reason"].startswith(("Traffic", "Fuel", "Short break")))
        out["days"].append({"date": d["date"], "confirmed": d["summary"]["stops_confirmed"],
                            "not_confirmed": nc, "personal": pe})
        out["confirmed"] += d["summary"]["stops_confirmed"]
        out["not_confirmed"] += nc
        out["personal"] += pe
    out["warnings"] = warnings(report)
    return out
