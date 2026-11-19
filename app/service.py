"""The persisted day: detected stops/drives are materialized into editable rows.

Edits live in the stops/drives tables on top of detection; raw points are never
touched. Drives are derived from the stop order (one between each neighbouring
pair) and keep their notes / edited times while the pair still exists.

Totals rules (documented here because the UI and PDF rely on them):
  * on-site time counts every non-Personal stop that is a real stop, plus short
    stops the user has Confirmed;
  * a drive counts unless one of its two ends is a Personal stop;
  * "draft" counts Draft stops of kind 'stop' (Draft short stops are clutter and
    are counted separately).
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import detect, parts
from .errors import EditError
from .geo import haversine_m
from .places import known

STATUSES = ("Draft", "Confirmed", "Personal")




# ---- time helpers ------------------------------------------------------
def day_range(date, tz):
    d = datetime.fromisoformat(date).replace(tzinfo=ZoneInfo(tz))
    return int(d.timestamp()), int((d + timedelta(days=1)).timestamp()) - 1


def parse_time(cfg, date, text):
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            t = datetime.strptime(str(text).strip(), fmt)
            break
        except ValueError:
            continue
    else:
        raise EditError("Times must look like 14:30.")
    d = datetime.fromisoformat(date).replace(
        hour=t.hour, minute=t.minute, second=t.second, tzinfo=ZoneInfo(cfg.app_tz))
    return int(d.timestamp())


def fmt_time(cfg, ts):
    return datetime.fromtimestamp(ts, ZoneInfo(cfg.app_tz)).strftime("%H:%M")


# ---- points ------------------------------------------------------------
def kept_points(conn, cfg, date, lo=None, hi=None):
    s, e = day_range(date, cfg.app_tz)
    rows = conn.execute(
        "SELECT tst_utc AS tst, lat, lon, acc FROM points "
        "WHERE device=? AND tst_utc BETWEEN ? AND ? ORDER BY tst_utc",
        (cfg.recorder_device, s if lo is None else lo, e if hi is None else hi))
    return [dict(r) for r in rows if r["acc"] is None or r["acc"] <= cfg.max_acc_m]


def _dist(conn, cfg, date, a, b):
    pts = kept_points(conn, cfg, date, a, b)
    return sum(haversine_m(p["lat"], p["lon"], q["lat"], q["lon"])
               for p, q in zip(pts, pts[1:]))


# ---- row access --------------------------------------------------------
def _ordered(conn, date):
    return conn.execute("SELECT * FROM stops WHERE date=? ORDER BY arrive_utc, id",
                        (date,)).fetchall()


def _stop(conn, sid):
    r = conn.execute("SELECT * FROM stops WHERE id=?", (sid,)).fetchone()
    if not r:
        raise EditError("That stop no longer exists.")
    return r


def _resequence(conn, date):
    for i, r in enumerate(_ordered(conn, date), 1):
        conn.execute("UPDATE stops SET seq=? WHERE id=?", (i, r["id"]))


def rebuild_drives(conn, cfg, date):
    stops = _ordered(conn, date)
    existing = {(d["from_stop_id"], d["to_stop_id"]): d for d in
                conn.execute("SELECT * FROM drives WHERE date=?", (date,))}
    keep = set()
    for seq, (a, b) in enumerate(zip(stops, stops[1:]), 1):
        d = existing.get((a["id"], b["id"]))
        if d and d["edited"]:
            dep, arr = d["depart_utc"], d["arrive_utc"]
        else:
            dep, arr = a["depart_utc"], max(b["arrive_utc"], a["depart_utc"])
        dist = _dist(conn, cfg, date, dep, arr)
        if d:
            conn.execute("UPDATE drives SET seq=?, depart_utc=?, arrive_utc=?, "
                         "distance_m=? WHERE id=?", (seq, dep, arr, dist, d["id"]))
            keep.add(d["id"])
        else:
            cur = conn.execute(
                "INSERT INTO drives(date,seq,from_stop_id,to_stop_id,depart_utc,"
                "arrive_utc,distance_m,edited) VALUES (?,?,?,?,?,?,?,0)",
                (date, seq, a["id"], b["id"], dep, arr, dist))
            keep.add(cur.lastrowid)
    for d in existing.values():
        if d["id"] not in keep:
            conn.execute("DELETE FROM drives WHERE id=?", (d["id"],))


def _finish(conn, cfg, date):
    _resequence(conn, date)
    rebuild_drives(conn, cfg, date)
    conn.commit()


# ---- materialize / sync ------------------------------------------------
def is_processed(conn, date):
    r = conn.execute("SELECT processed_at FROM days WHERE date=?", (date,)).fetchone()
    return bool(r and r["processed_at"])


def _mark_processed(conn, date, params):
    import json
    conn.execute(
        "INSERT INTO days(date, processed_at, params_json) VALUES (?,?,?) "
        "ON CONFLICT(date) DO UPDATE SET processed_at=excluded.processed_at, "
        "params_json=excluded.params_json",
        (date, datetime.now(timezone.utc).isoformat(), json.dumps(params)))


def _insert_detected(conn, date, it):
    conn.execute(
        "INSERT INTO stops(date,lat,lon,arrive_utc,depart_utc,kind,status,source,edited)"
        " VALUES (?,?,?,?,?,?,'Draft','detected',0)",
        (date, it["lat"], it["lon"], it["arrive"], it["depart"], it["kind"]))


def apply_places(conn, date):
    """Pre-fill untouched Draft stops that sit on a saved Home/Office/Vendor place."""
    for s in _ordered(conn, date):
        if s["status"] != "Draft" or s["edited"] or s["business_name"] or s["lat"] is None:
            continue
        site = known.match_place(conn, s["lat"], s["lon"])
        if site:
            conn.execute(
                "UPDATE stops SET business_name=?, address=?, category=?, known_site_id=?, status=? "
                "WHERE id=?", (site["name"], site["address"] or "", f"place={site['kind']}",
                               site["id"], site["default_status"] or "Draft", s["id"]))


def materialize(conn, cfg, date, kept, run_cfg):
    items, _ = detect.detect(kept, run_cfg)
    for it in items:
        _insert_detected(conn, date, it)
    _mark_processed(conn, date, {"min": run_cfg.stop_min_minutes,
                                 "radius": run_cfg.stop_radius_m,
                                 "gap": run_cfg.stop_max_gap_min})
    apply_places(conn, date)
    _finish(conn, cfg, date)


def sync_new(conn, cfg, date, kept, run_cfg):
    """Add stops detected since the day was materialized. Untouched Draft stops
    that grew (e.g. you are still on site) are extended; edited ones are left alone."""
    items, _ = detect.detect(kept, run_cfg)
    added = 0
    for it in items:
        ov = [s for s in _ordered(conn, date)
              if s["arrive_utc"] < it["depart"] and it["arrive"] < s["depart_utc"]]
        if not ov:
            _insert_detected(conn, date, it)
            added += 1
        elif (len(ov) == 1 and ov[0]["source"] == "detected" and not ov[0]["edited"]
              and ov[0]["status"] == "Draft"):
            conn.execute("UPDATE stops SET arrive_utc=?, depart_utc=?, lat=?, lon=?, "
                         "kind=? WHERE id=?", (it["arrive"], it["depart"], it["lat"],
                                               it["lon"], it["kind"], ov[0]["id"]))
    apply_places(conn, date)
    _finish(conn, cfg, date)
    return added


def redetect(conn, cfg, date, kept, run_cfg):
    """Discard detected stops (and all their edits) and detect again. Manual stops stay."""
    parts.release_stops(conn, [r["id"] for r in conn.execute(
        "SELECT id FROM stops WHERE date=? AND source!='manual'", (date,))])
    conn.execute("DELETE FROM stops WHERE date=? AND source!='manual'", (date,))
    conn.execute("DELETE FROM drives WHERE date=?", (date,))
    materialize(conn, cfg, date, kept, run_cfg)


# ---- validation --------------------------------------------------------
def _check_window(conn, cfg, date, sid, a, d):
    lo, hi = day_range(date, cfg.app_tz)
    if not (lo <= a <= hi and lo <= d <= hi):
        raise EditError("Times must fall within this day.")
    if d <= a:
        raise EditError("Departure must be after arrival.")
    for o in _ordered(conn, date):
        if o["id"] != sid and o["arrive_utc"] < d and a < o["depart_utc"]:
            raise EditError(f"Overlaps another stop ({fmt_time(cfg, o['arrive_utc'])}"
                            f"–{fmt_time(cfg, o['depart_utc'])}).")


def _float(v, lo, hi, what):
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise EditError(f"{what} must be a number.")
    if not lo <= f <= hi:
        raise EditError(f"{what} is out of range.")
    return f


# ---- stop operations ---------------------------------------------------
def patch_stop(conn, cfg, sid, f):
    s = _stop(conn, sid)
    date = s["date"]
    upd = {}
    for k in ("business_name", "address", "notes", "category"):
        if k in f:
            upd[k] = str(f[k] or "").strip() if k != "notes" else str(f[k] or "")
    if "known_site_id" in f:
        upd["known_site_id"] = int(f["known_site_id"]) if f["known_site_id"] else None
    if "status" in f:
        if f["status"] not in STATUSES:
            raise EditError("Status must be Draft, Confirmed or Personal.")
        upd["status"] = f["status"]
    if "lat" in f or "lon" in f:
        upd["lat"] = _float(f.get("lat"), -90, 90, "Latitude")
        upd["lon"] = _float(f.get("lon"), -180, 180, "Longitude")
    if "arrive" in f or "depart" in f:
        a = parse_time(cfg, date, f["arrive"]) if "arrive" in f else s["arrive_utc"]
        d = parse_time(cfg, date, f["depart"]) if "depart" in f else s["depart_utc"]
        _check_window(conn, cfg, date, sid, a, d)
        upd["arrive_utc"], upd["depart_utc"] = a, d

    status = upd.get("status", s["status"])
    name = upd.get("business_name", s["business_name"] or "")
    if status == "Confirmed":
        if not name:
            raise EditError("Add a business name before confirming.")
        lat, lon = upd.get("lat", s["lat"]), upd.get("lon", s["lon"])
        if s["status"] != "Confirmed" and lat is not None:
            # confirming teaches the known-sites table (once, on the transition)
            upd["known_site_id"] = known.learn(
                conn, name, upd.get("address", s["address"] or ""), lat, lon,
                upd.get("category", s["category"] or ""))
    upd["edited"] = 1
    sets = ", ".join(f"{k}=?" for k in upd)  # keys are whitelisted above
    conn.execute(f"UPDATE stops SET {sets} WHERE id=?", (*upd.values(), sid))
    _finish(conn, cfg, date)
    return date


def delete_stop(conn, cfg, sid):
    date = _stop(conn, sid)["date"]
    parts.release_stops(conn, [sid])
    conn.execute("DELETE FROM stops WHERE id=?", (sid,))
    _finish(conn, cfg, date)
    return date


def add_manual(conn, cfg, date, f):
    a = parse_time(cfg, date, f.get("arrive", ""))
    d = parse_time(cfg, date, f.get("depart", ""))
    _check_window(conn, cfg, date, None, a, d)
    conn.execute(
        "INSERT INTO stops(date,arrive_utc,depart_utc,business_name,address,notes,"
        "kind,status,source,edited) VALUES (?,?,?,?,?,?,'stop','Draft','manual',1)",
        (date, a, d, str(f.get("business_name", "")).strip(),
         str(f.get("address", "")).strip(), str(f.get("notes", ""))))
    _finish(conn, cfg, date)


def split_stop(conn, cfg, sid, at):
    s = _stop(conn, sid)
    date = s["date"]
    t = parse_time(cfg, date, at)
    if not s["arrive_utc"] < t < s["depart_utc"]:
        raise EditError("Split time must be between arrival and departure.")

    def centre(lo, hi, fallback):
        pts = kept_points(conn, cfg, date, lo, hi)
        if not pts:
            return fallback
        return (sum(p["lat"] for p in pts) / len(pts), sum(p["lon"] for p in pts) / len(pts))

    fb = (s["lat"], s["lon"])
    c1 = centre(s["arrive_utc"], t, fb) if s["lat"] is not None else fb
    c2 = centre(t, s["depart_utc"], fb) if s["lat"] is not None else fb
    conn.execute("UPDATE stops SET depart_utc=?, lat=?, lon=?, edited=1 WHERE id=?",
                 (t, c1[0], c1[1], sid))
    conn.execute(
        "INSERT INTO stops(date,lat,lon,arrive_utc,depart_utc,kind,status,source,edited)"
        " VALUES (?,?,?,?,?,?,'Draft',?,1)",
        (date, c2[0], c2[1], t, s["depart_utc"], s["kind"], s["source"]))
    _finish(conn, cfg, date)
    return date


def merge_stops(conn, cfg, id_a, id_b):
    a, b = _stop(conn, id_a), _stop(conn, id_b)
    if a["date"] != b["date"]:
        raise EditError("Stops are on different days.")
    order = [r["id"] for r in _ordered(conn, a["date"])]
    if abs(order.index(a["id"]) - order.index(b["id"])) != 1:
        raise EditError("Only neighbouring stops can be merged.")
    if order.index(a["id"]) > order.index(b["id"]):
        a, b = b, a
    first = a if (a["business_name"] or not b["business_name"]) else b
    la, lb = a["depart_utc"] - a["arrive_utc"], b["depart_utc"] - b["arrive_utc"]
    if a["lat"] is not None and b["lat"] is not None and (la + lb) > 0:
        w = max(la, 1) / (max(la, 1) + max(lb, 1))
        lat, lon = a["lat"] * w + b["lat"] * (1 - w), a["lon"] * w + b["lon"] * (1 - w)
    else:
        lat, lon = (a["lat"], a["lon"]) if a["lat"] is not None else (b["lat"], b["lon"])
    notes = "\n".join(n for n in (a["notes"], b["notes"]) if n)
    status = a["status"] if a["status"] == b["status"] else "Draft"
    kind = "stop" if "stop" in (a["kind"], b["kind"]) else "short"
    conn.execute(
        "UPDATE stops SET arrive_utc=?, depart_utc=?, lat=?, lon=?, business_name=?, "
        "address=?, category=?, known_site_id=?, notes=?, status=?, kind=?, edited=1 "
        "WHERE id=?",
        (a["arrive_utc"], b["depart_utc"], lat, lon, first["business_name"],
         first["address"], first["category"], first["known_site_id"], notes, status,
         kind, a["id"]))
    conn.execute("UPDATE stop_parts SET stop_id=? WHERE stop_id=?", (a["id"], b["id"]))
    conn.execute("DELETE FROM stops WHERE id=?", (b["id"],))
    _finish(conn, cfg, a["date"])
    return a["date"]


def save_place(conn, cfg, sid, f):
    """Save a stop's location as a Home/Office/Vendor place and pre-fill the day's matching stops."""
    s = _stop(conn, sid)
    if s["lat"] is None:
        raise EditError("This stop has no location to save (it was added by hand).")
    kind = f.get("kind")
    if kind not in ("home", "office", "vendor"):
        raise EditError("Choose Home, Office or Vendor.")
    name = str(f.get("name") or "").strip() or s["business_name"] or known.KIND_LABEL[kind]
    site_id = known.create_site(conn, {"name": name, "lat": s["lat"], "lon": s["lon"], "kind": kind,
                                       "address": s["address"] or str(f.get("address") or "")})
    conn.execute("UPDATE stops SET business_name=?, known_site_id=?, category=?, edited=1 WHERE id=?",
                 (name, site_id, f"place={kind}", sid))
    apply_places(conn, s["date"])
    _finish(conn, cfg, s["date"])
    return s["date"]


# ---- parts used at a stop ------------------------------------------------
def add_stop_part(conn, cfg, stop_id, f):
    date = _stop(conn, stop_id)["date"]
    parts.add_to_stop(conn, stop_id, f)
    conn.execute("UPDATE stops SET edited=1 WHERE id=?", (stop_id,))
    conn.commit()
    return date


def patch_stop_part(conn, cfg, spid, f):
    stop_id = parts.patch_stop_part(conn, spid, f)
    conn.commit()
    return _stop(conn, stop_id)["date"]


def remove_stop_part(conn, cfg, spid):
    stop_id = parts.remove_from_stop(conn, spid)
    conn.commit()
    return _stop(conn, stop_id)["date"]


# ---- drive operations --------------------------------------------------
def patch_drive(conn, cfg, did, f):
    d = conn.execute("SELECT * FROM drives WHERE id=?", (did,)).fetchone()
    if not d:
        raise EditError("That drive no longer exists.")
    date, upd = d["date"], {}
    if "notes" in f:
        upd["notes"] = str(f["notes"] or "")
    if "depart" in f or "arrive" in f:
        dep = parse_time(cfg, date, f["depart"]) if "depart" in f else d["depart_utc"]
        arr = parse_time(cfg, date, f["arrive"]) if "arrive" in f else d["arrive_utc"]
        lo, hi = day_range(date, cfg.app_tz)
        if not (lo <= dep <= hi and lo <= arr <= hi):
            raise EditError("Times must fall within this day.")
        if arr < dep:
            raise EditError("Arrival must not be before departure.")
        upd["depart_utc"], upd["arrive_utc"], upd["edited"] = dep, arr, 1
    if upd:
        sets = ", ".join(f"{k}=?" for k in upd)
        conn.execute(f"UPDATE drives SET {sets} WHERE id=?", (*upd.values(), did))
        _finish(conn, cfg, date)
    return date


# ---- read side ---------------------------------------------------------
def totals(stops, drives):
    def counts(s):
        return s["status"] != "Personal" and (s["kind"] == "stop" or s["status"] == "Confirmed")

    personal = {s["id"] for s in stops if s["status"] == "Personal"}
    on_site = sum(s["depart_ts"] - s["arrive_ts"] for s in stops if counts(s))
    live = [d for d in drives if d["from_stop_id"] not in personal and d["to_stop_id"] not in personal]
    return {
        "on_site_s": on_site,
        "drive_s": sum(d["arrive_ts"] - d["depart_ts"] for d in live),
        "miles_approx": round(sum(d["distance_m"] for d in live) / 1609.344, 1),
        "confirmed": sum(1 for s in stops if s["status"] == "Confirmed"),
        "draft": sum(1 for s in stops if s["status"] == "Draft" and s["kind"] == "stop"),
        "draft_short": sum(1 for s in stops if s["status"] == "Draft" and s["kind"] == "short"),
        "personal": len(personal),
    }


def day_payload(conn, cfg, date, include_path=False):
    used = parts.for_day(conn, date)
    stops = [{
        "parts": used.get(r["id"], []),
        "id": r["id"], "seq": r["seq"], "kind": r["kind"] or "stop", "source": r["source"],
        "lat": r["lat"], "lon": r["lon"],
        "arrive": fmt_time(cfg, r["arrive_utc"]), "depart": fmt_time(cfg, r["depart_utc"]),
        "arrive_ts": r["arrive_utc"], "depart_ts": r["depart_utc"],
        "minutes": round((r["depart_utc"] - r["arrive_utc"]) / 60),
        "business_name": r["business_name"] or "", "address": r["address"] or "",
        "notes": r["notes"] or "", "category": r["category"] or "",
        "status": r["status"] or "Draft", "known_site_id": r["known_site_id"],
        "hubspot_company_id": r["hubspot_company_id"] or "",
        "edited": bool(r["edited"]),
    } for r in _ordered(conn, date)]
    drives = [{
        "id": r["id"], "from_stop_id": r["from_stop_id"], "to_stop_id": r["to_stop_id"],
        "depart": fmt_time(cfg, r["depart_utc"]), "arrive": fmt_time(cfg, r["arrive_utc"]),
        "depart_ts": r["depart_utc"], "arrive_ts": r["arrive_utc"],
        "minutes": round((r["arrive_utc"] - r["depart_utc"]) / 60),
        "miles_approx": round((r["distance_m"] or 0) / 1609.344, 1),
        "distance_m": r["distance_m"] or 0,
        "notes": r["notes"] or "", "edited": bool(r["edited"]),
    } for r in conn.execute("SELECT * FROM drives WHERE date=? ORDER BY seq", (date,))]
    out = {"date": date, "tz": cfg.app_tz, "stops": stops, "drives": drives,
           "totals": totals(stops, drives)}
    if include_path:
        pts = kept_points(conn, cfg, date)
        out["path"] = [[p["lat"], p["lon"]] for p in pts]
        out["kept_count"] = len(pts)
    return out
