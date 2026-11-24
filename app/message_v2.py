"""Message format version 2: one envelope and one set of shared objects for every email to the work AI agent.

The agent's spec (agreed 2026-11-24) is in docs/sample-report/README.md. In short:
- every JSON attachment starts with the envelope (`schema_version` "2", `type`, `message_id`, `subject_key`,
  `revision`, `supersedes`, `sent_at`, `timezone`, `app`, `technician`, `omitted`);
- a stop is the same Stop object in the daily summary and the invoice request, with Customer, Job, Line, Follow-up
  and Photo objects inside;
- every point in time is ISO 8601 with offset and seconds, durations are whole `_minutes`, money is a decimal string,
  updatable items carry a UUID (`stop_uid`, `line_uid`, `follow_up_uid`, `photo_uid`), and a key is never left out
  because it is empty ("" / [] / null instead).

The numbers come from the same day calculation as format 1 (`report.build_day(..., keep_src=True)`), so the two can
never disagree. Within format 2 fields may only be ADDED (the agent ignores fields it does not know).
"""
import uuid
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from . import address, report, service
from .places import known
from .version import APP_VERSION

SCHEMA = "2"
TYPES = {"daily_summary": "Daily Summary", "invoice_request": "Invoice Request", "late_notice": "Late Notice"}
VISIT = {"complete": "complete", "return": "return_visit", "waiting_parts": "waiting_parts",
         "waiting_customer": "waiting_customer"}
REAL_TICKET_MIN = 9          # a HubSpot ticket ID is all digits and at least this long (ComDirect's are 12)


def new_id():
    return str(uuid.uuid4())


def iso(ts, tz):
    """Seconds since 1970 -> '2026-11-18T08:00:00-08:00' ('' when unknown)."""
    if ts is None:
        return ""
    return datetime.fromtimestamp(int(ts), ZoneInfo(tz) if isinstance(tz, str) else tz).isoformat(timespec="seconds")


def money(v, unit=False):
    """0.45 -> '0.45', 63 -> '63.00'; a unit price keeps up to 4 decimals ('0.215'). None -> ''."""
    if v is None:
        return ""
    d = Decimal(str(v))
    two = d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if not unit or two == d:
        return str(two)
    return str(d.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)).rstrip("0")


def qty(v):
    """140.0 -> 140, 2.5 -> 2.5."""
    return int(v) if isinstance(v, float) and v.is_integer() else v


def ticket_fields(text):
    """(hubspot_ticket_id, tech_typed_reference): a real ID is all digits and 9+ long; anything else is what was typed."""
    t = (text or "").strip().lstrip("#").strip()
    if t.isdigit() and len(t) >= REAL_TICKET_MIN:
        return t, ""
    return "", t


# ---- envelope -------------------------------------------------------------------------------------------------
def previous(conn, table, where, args):
    """(revision of the next send, message_id it supersedes or None) from a send log."""
    n = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE ok=1 AND {where}", args).fetchone()[0]
    r = conn.execute(f"SELECT message_id FROM {table} WHERE ok=1 AND {where} ORDER BY id DESC LIMIT 1", args).fetchone()
    return n + 1, (r["message_id"] or None) if r else None


def envelope(cfg, type_, subject_key, revision, supersedes, now, omitted=(), message_id=None):
    return {"schema_version": SCHEMA, "type": type_, "message_id": message_id or new_id(), "subject_key": subject_key,
            "revision": revision, "supersedes": supersedes, "sent_at": iso(now, cfg.app_tz), "timezone": cfg.app_tz,
            "app": {"name": "JobHub", "version": APP_VERSION},
            "technician": {"name": cfg.tech_name or "", "email": cfg.tech_email or ""},
            "omitted": list(omitted)}


def subject(type_, date_text, customer, revision):
    """'JobHub Invoice Request 2026-11-18 – Sample Dental Office (update 2)'."""
    return (f"JobHub {TYPES[type_]} {date_text}" + (f" – {customer}" if customer else "")
            + (f" (update {revision})" if revision > 1 else ""))


def file_stem(type_, date_text, key, revision):
    """'jobhub-invoice-request-2026-11-18-stop-1-sample-dental-office-r1' (and .json / .pdf)."""
    return "-".join(x for x in ("jobhub", type_.replace("_", "-"), date_text, key, f"r{revision}") if x)


def photo_attachment(uid):
    return f"photo-{uid}.jpg"


# ---- shared objects --------------------------------------------------------------------------------------------
def sites_by_id(conn):
    return {r["id"]: r for r in conn.execute("SELECT * FROM known_sites")}


def customer(name, address_text, lat, lon, hubspot_id="", site=None, contact="", coords=True):
    """Customer object. A saved place gives the address parts (as typed on Places), else the one-line address is split."""
    formatted = (site["address"] if site is not None and site["address"] else address_text) or ""
    parts = known.address_parts(site) if site is not None and site["address"] else address.split(formatted)
    return {"business_name": name or "",
            "hubspot_company_id": hubspot_id or ((site["hubspot_company_id"] or "") if site is not None else ""),
            "qbo_customer_id": (site["qbo_customer_id"] or "") if site is not None else "",
            "service_address": {**parts, "formatted": formatted,
                                "latitude": lat if coords else None, "longitude": lon if coords else None},
            "site_contact": contact or ""}


def job(category, ticket, ticket_choice, qbo_estimate):
    real, typed = ticket_fields(ticket)
    action = "use" if real else report.TICKET_ACTIONS.get(ticket_choice or "", "find")
    return {"category": category or "", "billing": service.WORK_CATEGORIES.get(category or "", ""),
            "hubspot_ticket_id": real, "ticket_action": action, "qbo_estimate_number": (qbo_estimate or "").strip(),
            "tech_typed_reference": typed}


def _receipt_uid(src):
    """The stop's receipt picture when it has exactly one (then it is the receipt for its purchases), else ''."""
    rec = [p for p in src.get("photos") or [] if p["kind"] == "receipt"]
    return rec[0].get("uid", "") if len(rec) == 1 else ""


def lines(src, omit):
    """Line objects: parts, consumables and expenses used on the stop, then stock bought there."""
    costs = "costs" not in omit
    out, receipt = [], _receipt_uid(src)
    for p in src.get("parts") or []:
        kind = p.get("kind") or "part"
        if (kind == "expense" and "expenses" in omit) or (kind == "consumable" and "materials" in omit):
            continue
        # what you pay per unit: the part's purchase cost; an expense IS what was paid
        cost = p["unit_cost"] if kind == "expense" else p.get("our_cost")
        out.append({"line_uid": p.get("uid") or "", "kind": kind, "name": p["name"], "sku": p["sku"] or "",
                    "qbo_item_id": "", "qbo_item_name": p.get("qbo_item") or "",
                    "quantity": qty(p["qty"]), "unit": p["unit"] or "ea",
                    "unit_price": money(p["unit_cost"], unit=True) if costs else "",
                    "amount": money(p["line_cost"]) if costs else "",
                    "unit_cost": money(cost, unit=True) if costs else "",
                    "billable": bool(p["billable"]) and kind in ("part", "expense"),
                    "receipt_photo_uid": receipt if kind == "expense" else "", "notes": p.get("notes") or ""})
    for b in src.get("bought") or []:
        out.append({"line_uid": b.get("uid") or "", "kind": "stock", "name": b["name"], "sku": b["sku"] or "",
                    "qbo_item_id": "", "qbo_item_name": b.get("qbo_item") or "",
                    "quantity": qty(b["qty"]), "unit": b["unit"] or "ea", "unit_price": "", "amount": "",
                    "unit_cost": money(b.get("each"), unit=True) if costs else "",
                    "amount_paid": money(b.get("paid")) if costs else "",
                    "billable": False, "receipt_photo_uid": receipt, "notes": b.get("note") or ""})
    return out


def place_type(src):
    k = src.get("site_kind") or ""
    if k in ("vendor", "office", "home"):
        return k
    return src.get("stop_type") if src.get("stop_type") in ("vendor", "office") else "client"


def stop(v1, src, date, tz, sites, from_uid, lunch_paid, omit):
    """The Stop object from a format-1 stop (`build_day(keep_src=True)`) and its source (`_src`)."""
    coords = "coordinates" not in omit
    dv = v1["drive_from_previous_stop"]
    drive = None if dv is None else {
        "from_type": (dv.get("from_type") or "").lower(), "from_name": dv.get("from") or "",
        "from_stop_uid": from_uid, "minutes": dv["minutes"], "minutes_estimated": bool(dv.get("minutes_estimated")),
        "miles": dv["miles_approx"], "miles_method": dv.get("miles_method") or "", "notes": dv.get("notes") or ""}
    cal = src.get("calendar") or {}
    inv = src.get("invoice_request")
    return {
        "stop_uid": v1["stop_uid"], "date": date, "place_type": place_type(src),
        "calendar_event_uid": cal.get("uid", ""),
        "customer": customer(src["business_name"], src["address"], src["lat"], src["lon"], src["hubspot_company_id"],
                             sites.get(src.get("known_site_id")), cal.get("contact", ""), coords),
        "job": job(v1["category"], src.get("ticket"), src.get("ticket_action") or src.get("ticket_action_default"),
                   v1["qbo_estimate"]),
        "arrival": iso(src["arrive_ts"], tz), "departure": iso(src["depart_ts"], tz),
        "time_on_site_minutes": v1["time_on_site_minutes"],
        "breaks": [{"kind": "lunch" if g["kind"] == "Lunch" else "personal", "start": iso(g["from_ts"], tz),
                    "end": iso(g["to_ts"], tz), "minutes": round(g["secs"] / 60),
                    "paid": bool(lunch_paid) if g["kind"] == "Lunch" else False, "billable": False}
                   for g in v1.get("_gaps") or []],
        "drive_from_previous": drive,
        "visit_result": VISIT.get(service.visit_code(src.get("visit_result")), ""),
        "work_performed": v1["work_performed"] or "",
        "lines": lines(src, omit),
        "follow_ups": [] if "follow_ups" in omit else [
            {"follow_up_uid": f.get("uid") or "", "stop_uid": v1["stop_uid"], "what": f1["what"], "owner": f1["owner"] or "",
             "owner_email": f1.get("owner_email") or "", "due": f1["due"] or ""}
            for f, f1 in zip(src.get("follow_ups") or [], v1["follow_ups"])],
        "photo_uids": [] if "photos" in omit else [p["uid"] for p in src.get("photos") or [] if p.get("uid")],
        "invoice_request": None if not inv else {
            "message_id": inv.get("message_id") or "", "revision": inv["version"],
            "sent_at": iso(inv.get("sent_ts"), tz), "billed_through": iso(inv.get("billed_through_ts"), tz),
            "time_on_site_minutes": inv["time_on_site_minutes"]},
    }


def photo(p, stop_uid, tz):
    src = p["_src"]
    return {"photo_uid": src.get("uid") or "", "stop_uid": stop_uid, "kind": p["kind"], "label": p["label"],
            "caption": p.get("caption") or "", "attachment": photo_attachment(src.get("uid") or str(p["id"])),
            "attached": False, "taken_at": iso(src.get("created"), tz)}


# ---- the daily summary -----------------------------------------------------------------------------------------
def _from_uids(payload):
    """{stop_uid: uid of the work stop the mileage leg into it started from (None = Home)}."""
    out, prev = {}, None
    for lg in (payload.get("mileage") or {}).get("legs") or []:
        if lg.get("to_stop_uid"):
            out[lg["to_stop_uid"]] = prev
        prev = lg.get("to_stop_uid")
    return out


def day(conn, cfg, payload, sites, omit):
    """(day object, [(attachment name, photo id)])."""
    tz = cfg.app_tz
    d = report.build_day(payload, cfg, keep_src=True)
    lunch_paid = getattr(cfg, "lunch_paid", "on") != "off"
    from_uid = _from_uids(payload)
    stops = [stop(s, s["_src"], payload["date"], tz, sites, from_uid.get(s["stop_uid"]), lunch_paid, omit)
             for s in d["stops"]]
    owner = {}                                       # photo id -> the stop it belongs to in this report
    for s in d["stops"]:
        for p in s["_src"].get("photos") or []:
            owner[p["id"]] = s["stop_uid"]
    for f in d["fuel"]:
        for p in f["_src"].get("photos") or []:
            owner[p["id"]] = f["_src"].get("uid") or ""
    pics = [] if "photos" in omit else [photo(p, owner.get(p["id"], ""), tz) for p in d.get("photos") or []]
    files = [(pic["attachment"], p["id"]) for pic, p in zip(pics, d.get("photos") or [])]
    sm, marks, end = d["summary"], d["_marks"], d["_end_day"]
    mil = payload.get("mileage")
    legs, prev = [], None
    for lg in (mil or {}).get("legs") or []:
        legs.append({"from_stop_uid": prev, "to_stop_uid": lg.get("to_stop_uid"), "from_name": lg["from"],
                     "to_name": lg["to"], "miles": lg["miles"], "method": lg["method"]})
        prev = lg.get("to_stop_uid")
    out = {
        "date": payload["date"],
        "day_start": iso(marks.get("start_ts"), tz), "day_end": iso(marks.get("end_ts"), tz),
        "end_day": None if not end else {"kind": end["kind"], "place": end["place"], "drive_home_counted": bool(end["counted"]),
                                         "time": iso(marks.get("end_ts"), tz), "miles_to_home": end["miles_to_home"]},
        "totals": {
            "on_site_minutes": sm["on_site_minutes"], "billable_on_site_minutes": sm["billable_on_site_minutes"],
            "drive_minutes": sm["drive_minutes"], "traffic_minutes": sm["traffic_minutes"],
            "fuel_stop_minutes": sm["fuel_stop_minutes"], "short_break_minutes": sm["short_break_minutes"],
            "lunch_minutes": sm["lunch_minutes"], "lunch_paid": sm["lunch_paid"],
            "unpaid_break_minutes": sm["break_minutes_unpaid"], "paid_minutes": sm["paid_minutes"],
            "miles": sm["miles_approx"],
            "expenses_total": "" if "expenses" in omit else money(sm["expenses_total"]),
            "stock_bought_total": "" if "costs" in omit else money(sm["bought_total"]),
            "fuel_total": "" if "fuel" in omit else money(sm["fuel_total"]),
            "by_category": [] if "categories" in omit else [
                {"category": c["category"], "billing": c["billing"], "on_site_minutes": c["on_site_minutes"]}
                for c in sm["hours_by_category"]],
            "stops_confirmed": sm["stops_confirmed"], "stops_excluded": sm["stops_excluded"],
        },
        "stops": stops,
        "excluded": [{"start": iso(x["_start"], tz), "end": iso(x["_end"], tz), "reason": x["reason"]}
                     for x in d["excluded"]],
        "timesheet": [] if "recap" in omit else [
            {"start": iso(r["_start"], tz), "end": iso(r["_end"], tz), "type": r["type"], "description": r["description"],
             "minutes": r["minutes"], "paid": r["paid"]} for r in d["timesheet"]],
        "mileage": None if ("mileage" in omit or not mil) else {
            "basis": "home_to_home", "total_miles": mil["total_miles"], "legs": legs},
        "fuel": [] if "fuel" in omit else [
            {"stop_uid": f["_src"].get("uid") or "", "start": iso(f["_src"]["arrive_ts"], tz),
             "end": iso(f["_src"]["depart_ts"], tz), "station": f["station"], "gallons": f["gallons"],
             "total": money(f["total"]), "price_per_gallon": money(f["price_per_gallon"], unit=True),
             "odometer": f["odometer"], "receipt_photo_uid": _receipt_uid(f["_src"])} for f in d["fuel"]],
        "photos": pics,
    }
    return out, files


# ---- the late notice ------------------------------------------------------------------------------------------
def late_notice(conn, cfg, ev, eta, now, note, stop_uid, revision, supersedes, message_id=None):
    """The late notice (`type` late_notice). The customer comes from the calendar appointment; a saved place with the
    appointment's name fills in the company ID and the address parts."""
    name = (ev.get("name") or "").strip()
    site = conn.execute("SELECT * FROM known_sites WHERE lower(name)=lower(?) AND kind='client' AND COALESCE(old_address,0)=0 "
                        "ORDER BY times_confirmed DESC LIMIT 1", (name,)).fetchone() if name else None
    loc = (ev.get("location") or "").strip()
    if site is not None and loc and address.split(loc)["line1"].lower() != known.address_parts(site)["line1"].lower():
        site = None                       # same name, another address: only what the calendar says
    cust = customer(name, loc, None, None, "", site, ev.get("contact") or "")
    if site is None:
        cust["service_address"]["formatted"] = loc
    if site is not None:
        cust["service_address"]["latitude"], cust["service_address"]["longitude"] = site["lat"], site["lon"]
    return {**envelope(cfg, "late_notice", ev.get("uid") or "", revision, supersedes, now, (), message_id),
            "customer": cust,
            "job": job(next((c for c in service.WORK_CATEGORIES if c.lower() == (ev.get("category") or "").strip().lower()), ""),
                       ev.get("ticket"), "", ev.get("estimate")),
            "stop_uid": stop_uid or "",
            "appointment": {"calendar_event_uid": ev.get("uid") or "", "start": iso(ev["start_ts"], cfg.app_tz),
                            "end": iso(ev["end_ts"], cfg.app_tz), "new_eta": iso(eta["arrive_ts"], cfg.app_tz),
                            "minutes_late": eta["late_min"],
                            "eta_method": "road_route" if eta.get("method") == "routed" else "estimate"},
            "contact_from_calendar": ev.get("contact") or "",
            "note_for_customer": (note or "").strip()[:300]}


def range_key(a, b):
    return a if a == b else f"{a}_{b}"


def daily_summary(conn, cfg, dates, ensure, omit, revision, supersedes, now, message_id=None):
    """(message, [(attachment, photo id)]). `omit` is a list of report.OMIT_SECTIONS keys."""
    sites = sites_by_id(conn)
    days, files = [], []
    for date in dates:
        ensure(conn, cfg, date)
        dd, ff = day(conn, cfg, service.day_payload(conn, cfg, date), sites, omit)
        days.append(dd); files += ff
    msg = {**envelope(cfg, "daily_summary", range_key(dates[0], dates[-1]), revision, supersedes, now, omit, message_id),
           "date_from": dates[0], "date_to": dates[-1], "days": days}
    return msg, files


def daily_revision(conn, a, b):
    return previous(conn, "email_log", "date_from=? AND date_to=?", (a, b))


def daily_subject(a, b, revision):
    return subject("daily_summary", a if a == b else f"{a} to {b}", "", revision)


def daily_stem(a, b, revision):
    return file_stem("daily_summary", a if a == b else f"{a}_to_{b}", "", revision)
