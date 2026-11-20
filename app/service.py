"""The persisted day: detected stops/drives are materialized into editable rows.

Edits live in the stops/drives tables on top of detection; raw points are never
touched. Drives are derived from the stop order (one between each neighbouring
pair) and keep their notes / edited times while the pair still exists.

Totals rules (documented here because the UI and PDF rely on them):
  * on-site time counts every stop that is a real stop and not Personal, Lunch or Traffic, plus
    short stops the user has Confirmed;
  * a drive counts unless one of its two ends is a Personal or Lunch stop, except that lunch between two different
    work stops is part of the trip, so the drives before and after it count (see drive_counts);
  * a Traffic stop (a slow-down mistaken for a stop) is not on-site time; its minutes count as drive time;
  * "draft" counts Draft stops of kind 'stop' (Draft short stops are clutter and
    are counted separately).
"""
import json
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import detect, parts, routing
from .errors import EditError
from .geo import haversine_m
from .places import known

STATUSES = ("Draft", "Confirmed", "Personal", "Lunch", "Traffic")
OFF_CLOCK = ("Personal", "Lunch")   # not work: the stop and the drives to and from it are excluded




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


CROW_FLIES_FACTOR = 1.25   # straight-line to road distance, only when a drive has no GPS points


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
        if not dist and a["lat"] is not None and b["lat"] is not None:
            dist = haversine_m(a["lat"], a["lon"], b["lat"], b["lon"]) * CROW_FLIES_FACTOR  # no points: estimate
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


def _after_end(conn, cfg, date, arrive):
    """Once End day is set, nothing that begins after it is added to the day."""
    end = day_marks(conn, cfg, date)["end_ts"]
    return bool(end) and arrive > end + END_SLACK_S


def materialize(conn, cfg, date, kept, run_cfg):
    items, _ = detect.detect(kept, run_cfg)
    for it in items:
        if _after_end(conn, cfg, date, it["arrive"]):
            continue
        _insert_detected(conn, date, it)
    _mark_processed(conn, date, {"min": run_cfg.stop_min_minutes,
                                 "radius": run_cfg.stop_radius_m,
                                 "gap": run_cfg.stop_max_gap_min})
    apply_places(conn, date)
    _finish(conn, cfg, date)


def sync_new(conn, cfg, date, kept, run_cfg):
    """Add stops detected since the day was materialized, and let stops that are still
    going (you are on site) grow. Untouched Draft stops follow the detection exactly; a
    stop you edited or confirmed keeps its details and pin, and only grows in time
    unless you set its times yourself."""
    items, _ = detect.detect(kept, run_cfg)
    added = 0
    for it in items:
        ov = [s for s in _ordered(conn, date)
              if s["arrive_utc"] < it["depart"] and it["arrive"] < s["depart_utc"]]
        if not ov:
            if _after_end(conn, cfg, date, it["arrive"]):
                continue
            _insert_detected(conn, date, it)
            added += 1
            continue
        if len(ov) != 1 or ov[0]["source"] != "detected":
            continue
        s = ov[0]
        if not s["edited"] and s["status"] == "Draft":
            conn.execute("UPDATE stops SET arrive_utc=?, depart_utc=?, lat=?, lon=?, "
                         "kind=? WHERE id=?", (it["arrive"], it["depart"], it["lat"],
                                               it["lon"], it["kind"], s["id"]))
        elif not s["times_edited"] and (it["depart"] > s["depart_utc"] or it["arrive"] < s["arrive_utc"]):
            arrive, depart = min(s["arrive_utc"], it["arrive"]), max(s["depart_utc"], it["depart"])
            if _free(conn, date, s["id"], arrive, depart):
                kind = "stop" if depart - arrive >= run_cfg.stop_min_minutes * 60 else s["kind"]
                conn.execute("UPDATE stops SET arrive_utc=?, depart_utc=?, kind=? WHERE id=?",
                             (arrive, depart, kind, s["id"]))
    apply_places(conn, date)
    _finish(conn, cfg, date)
    return added


def _free(conn, date, sid, a, d):
    """True when [a, d] overlaps no other stop (so growing a stop can't collide)."""
    return not any(o["id"] != sid and o["arrive_utc"] < d and a < o["depart_utc"]
                   for o in _ordered(conn, date))


def redetect(conn, cfg, date, kept, run_cfg):
    """Discard detected stops (and all their edits) and detect again. Manual stops stay."""
    snapshot_day(conn, date, "Re-detected the day")
    parts.release_stops(conn, [r["id"] for r in conn.execute(
        "SELECT id FROM stops WHERE date=? AND source!='manual'", (date,))])
    conn.execute("DELETE FROM stops WHERE date=? AND source!='manual'", (date,))
    conn.execute("DELETE FROM drives WHERE date=?", (date,))
    materialize(conn, cfg, date, kept, run_cfg)


# ---- day start / end marks -----------------------------------------------
def day_marks(conn, cfg, date):
    r = conn.execute("SELECT * FROM day_marks WHERE date=?", (date,)).fetchone()
    s, e = (r["start_utc"], r["end_utc"]) if r else (None, None)
    return {"start": fmt_time(cfg, s) if s else "", "end": fmt_time(cfg, e) if e else "",
            "start_ts": s, "end_ts": e}


def set_mark(conn, cfg, date, which, time_text=None, clear=False, ts=None):
    """Record when the work day started or ended. Time defaults to now (today only)."""
    if which not in ("start", "end"):
        raise EditError("Choose start or end.")
    col = f"{which}_utc"
    if clear:
        conn.execute("UPDATE day_marks SET %s=NULL WHERE date=?" % col, (date,))
        conn.commit()
        return date
    if ts is not None:
        pass
    elif time_text:
        ts = parse_time(cfg, date, time_text)
    else:
        now = int(time.time())
        lo, hi = day_range(date, cfg.app_tz)
        if not lo <= now <= hi:
            raise EditError("Enter a time for days other than today.")
        ts = now
    cur = day_marks(conn, cfg, date)
    other = cur["end_ts"] if which == "start" else cur["start_ts"]
    if other and ((which == "start" and ts >= other) or (which == "end" and ts <= other)):
        raise EditError("The day must end after it starts.")
    conn.execute("INSERT INTO day_marks(date) VALUES (?) ON CONFLICT(date) DO NOTHING", (date,))
    conn.execute("UPDATE day_marks SET %s=? WHERE date=?" % col, (ts, date))
    conn.commit()
    return date


def mark_from_stop(conn, cfg, sid, which):
    """Make a stop the start or the end of the work day. Start = when you arrived there. End = when
    you left it, or when you arrived if it is a Personal stop (home): the day ends once you are home."""
    s = _stop(conn, sid)
    if which not in ("start", "end"):
        raise EditError("Choose start or end.")
    ts = s["arrive_utc"] if which == "start" or s["status"] == "Personal" else s["depart_utc"]
    date = s["date"]
    set_mark(conn, cfg, date, which, ts=ts)       # validates the order before anything else changes
    # everything outside the work day is Personal: stops before the start, stops after the end
    ordered = _ordered(conn, date)
    out = [o for o in ordered if o["id"] != sid and o["status"] != "Personal" and (
        o["arrive_utc"] < s["arrive_utc"] if which == "start" else o["arrive_utc"] > s["arrive_utc"])]
    if out:
        snapshot_day(conn, date, "Start day here" if which == "start" else "End day here")
        for o in out:
            conn.execute("UPDATE stops SET status='Personal', edited=1 WHERE id=?", (o["id"],))
        _finish(conn, cfg, date)
    return date


def end_at_home(conn, cfg, date, time_text=None):
    """Basic end-of-day: end the day at the saved Home place. Uses the last stop if it is already a
    Personal stop there; otherwise adds a one-minute Personal Home stop (no GPS) at the given time/now."""
    home = conn.execute("SELECT * FROM known_sites WHERE kind='home' AND lat IS NOT NULL ORDER BY id LIMIT 1").fetchone()
    stops = _ordered(conn, date)
    last = stops[-1] if stops else None
    if last is not None and last["status"] == "Personal" and (
            home is None or last["known_site_id"] == home["id"]
            or (last["lat"] is not None and haversine_m(last["lat"], last["lon"], home["lat"], home["lon"]) <= 200)):
        return mark_from_stop(conn, cfg, last["id"], "end")
    if home is None:
        raise EditError("Save your Home first (Places page), or tap End day.")
    if time_text:
        a = parse_time(cfg, date, time_text)
    else:
        a = int(time.time())
    lo, hi = day_range(date, cfg.app_tz)
    if time_text is None and not lo <= a <= hi:
        raise EditError("Enter a time for days other than today.")
    d = a + 60
    _check_window(conn, cfg, date, None, a, min(d, hi))
    conn.execute(
        "INSERT INTO stops(date,lat,lon,arrive_utc,depart_utc,business_name,address,known_site_id,category,"
        "kind,status,source,edited) VALUES (?,?,?,?,?,?,?,?,?,'stop','Personal','manual',1)",
        (date, home["lat"], home["lon"], a, min(d, hi), home["name"], home["address"] or "", home["id"], "place=home"))
    _finish(conn, cfg, date)
    set_mark(conn, cfg, date, "end", ts=a)
    return date


# ---- one-tap confirm for saved places -----------------------------------
def suggestion(conn, s):
    """The saved place a Draft stop sits on (or is already filled in from), if any."""
    if s["status"] != "Draft" or (s["kind"] or "stop") != "stop":
        return None
    site = None
    if s["business_name"]:
        if s["known_site_id"]:
            site = conn.execute("SELECT * FROM known_sites WHERE id=?", (s["known_site_id"],)).fetchone()
    elif s["lat"] is not None:
        site = known.match_any(conn, s["lat"], s["lon"])
    if not site:
        return None
    return {"id": site["id"], "name": site["name"], "address": site["address"] or "",
            "kind": site["kind"] or "client"}


def confirm_known(conn, cfg, date, sid=None):
    """Confirm Draft stops that match a saved place (one stop, or the whole day)."""
    rows = [r for r in _ordered(conn, date) if sid is None or r["id"] == sid]
    todo = [(r, suggestion(conn, r)) for r in rows]
    todo = [(r, g) for r, g in todo if g]
    if not todo:
        raise EditError("No stops here match a saved place.")
    snapshot_day(conn, date, "Confirmed known stops")
    for r, g in todo:
        patch_stop(conn, cfg, r["id"], {"business_name": g["name"],
                                        "address": g["address"] or r["address"] or "",
                                        "status": "Confirmed"})
    return date


# ---- undo ----------------------------------------------------------------
UNDO_KEEP = 10  # snapshots kept per day


def _columns(conn, table):
    return [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]


def snapshot_day(conn, date, label):
    """Save the day's stops, drives and part lines so a destructive edit can be undone."""
    stops = [dict(r) for r in conn.execute("SELECT * FROM stops WHERE date=?", (date,))]
    ids = [s["id"] for s in stops]
    marks = ",".join("?" * len(ids))
    sp = [dict(r) for r in conn.execute(
        f"SELECT * FROM stop_parts WHERE stop_id IN ({marks})", ids)] if ids else []
    drives = [dict(r) for r in conn.execute("SELECT * FROM drives WHERE date=?", (date,))]
    conn.execute("INSERT INTO undo_log(date,ts,label,payload) VALUES (?,?,?,?)",
                 (date, int(time.time()), label,
                  json.dumps({"stops": stops, "drives": drives, "stop_parts": sp})))
    conn.execute("DELETE FROM undo_log WHERE date=? AND id NOT IN "
                 "(SELECT id FROM undo_log WHERE date=? ORDER BY id DESC LIMIT ?)",
                 (date, date, UNDO_KEEP))


def undo_available(conn, date):
    r = conn.execute("SELECT label, ts FROM undo_log WHERE date=? ORDER BY id DESC LIMIT 1",
                     (date,)).fetchone()
    return {"label": r["label"], "ts": r["ts"]} if r else None


def undo_last(conn, cfg, date):
    """Put the day back as it was before the last delete / merge / split / re-detect.
    Edits made after that action are lost with it."""
    row = conn.execute("SELECT * FROM undo_log WHERE date=? ORDER BY id DESC LIMIT 1",
                       (date,)).fetchone()
    if not row:
        raise EditError("Nothing to undo for this day.")
    snap = json.loads(row["payload"])
    parts.release_stops(conn, [r["id"] for r in conn.execute(
        "SELECT id FROM stops WHERE date=?", (date,))])
    conn.execute("DELETE FROM drives WHERE date=?", (date,))
    conn.execute("DELETE FROM stops WHERE date=?", (date,))

    def put(table, rec):
        cols = [c for c in _columns(conn, table) if c in rec]  # column names come from the schema
        conn.execute(f"INSERT INTO {table}({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                     [rec[c] for c in cols])

    for rec in snap["stops"]:
        put("stops", rec)
    for rec in snap["drives"]:
        put("drives", rec)
    for rec in snap["stop_parts"]:
        put("stop_parts", rec)
        part = conn.execute("SELECT * FROM parts WHERE id=?", (rec["part_id"],)).fetchone() \
            if rec.get("part_id") else None
        parts.move(conn, part, -rec["qty"], "used", rec["id"], "undo")
    conn.execute("DELETE FROM undo_log WHERE id=?", (row["id"],))
    _finish(conn, cfg, date)
    return date


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
            raise EditError("Status must be Draft, Confirmed, Personal, Lunch or Traffic.")
        upd["status"] = f["status"]
    if "lat" in f or "lon" in f:
        upd["lat"] = _float(f.get("lat"), -90, 90, "Latitude")
        upd["lon"] = _float(f.get("lon"), -180, 180, "Longitude")
    if "arrive" in f or "depart" in f:
        a = parse_time(cfg, date, f["arrive"]) if "arrive" in f else s["arrive_utc"]
        d = parse_time(cfg, date, f["depart"]) if "depart" in f else s["depart_utc"]
        _check_window(conn, cfg, date, sid, a, d)
        upd["arrive_utc"], upd["depart_utc"] = a, d
        upd["times_edited"] = 1

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
    cur = _stop(conn, sid)
    if (status == "Personal" and s["status"] != "Personal" and name and cur["lat"] is not None
            and not cur["known_site_id"]):
        # a named Personal stop is saved as a place automatically (home if it says so), so it is pre-filled next time
        save_place(conn, cfg, sid, {"kind": "home" if "home" in name.lower() else "vendor", "name": name})
    return date


def delete_stop(conn, cfg, sid):
    date = _stop(conn, sid)["date"]
    snapshot_day(conn, date, "Deleted a stop")
    parts.release_stops(conn, [sid])
    conn.execute("DELETE FROM stops WHERE id=?", (sid,))
    _finish(conn, cfg, date)
    return date


def add_manual(conn, cfg, date, f):
    a = parse_time(cfg, date, f.get("arrive", ""))
    d = parse_time(cfg, date, f.get("depart", ""))
    _check_window(conn, cfg, date, None, a, d)
    if _after_end(conn, cfg, date, a):
        raise EditError("The day has ended. Clear the End time (Day times…) to add a stop after it.")
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
    snapshot_day(conn, date, "Split a stop")

    def centre(lo, hi, fallback):
        pts = kept_points(conn, cfg, date, lo, hi)
        if not pts:
            return fallback
        return (sum(p["lat"] for p in pts) / len(pts), sum(p["lon"] for p in pts) / len(pts))

    fb = (s["lat"], s["lon"])
    c1 = centre(s["arrive_utc"], t, fb) if s["lat"] is not None else fb
    c2 = centre(t, s["depart_utc"], fb) if s["lat"] is not None else fb
    conn.execute("UPDATE stops SET depart_utc=?, lat=?, lon=?, edited=1, times_edited=1 WHERE id=?",
                 (t, c1[0], c1[1], sid))
    conn.execute(
        "INSERT INTO stops(date,lat,lon,arrive_utc,depart_utc,kind,status,source,edited,times_edited)"
        " VALUES (?,?,?,?,?,?,'Draft',?,1,1)",
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
    snapshot_day(conn, a["date"], "Merged two stops")
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
        "address=?, category=?, known_site_id=?, notes=?, status=?, kind=?, edited=1, times_edited=1 "
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
    site = {"name": name, "lat": s["lat"], "lon": s["lon"], "kind": kind,
            "address": s["address"] or str(f.get("address") or "")}
    if s["status"] == "Personal":
        site["default_status"] = "Personal"
    site_id = known.create_site(conn, site)
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
START_SLACK_S = 120


END_SLACK_S = 120


SAME_SITE_M = 300


def same_site(a, b):
    """True when two stops are the same place (same saved place, or the same name within 300 m)."""
    if a.get("known_site_id") and a.get("known_site_id") == b.get("known_site_id"):
        return True
    na, nb = (a.get("business_name") or "").strip().lower(), (b.get("business_name") or "").strip().lower()
    if not na or na != nb:
        return False
    if a.get("lat") is None or b.get("lat") is None:
        return True
    return haversine_m(a["lat"], a["lon"], b["lat"], b["lon"]) <= SAME_SITE_M


def end_stop(stops, marks):
    """The stop the day ended at: the last one you had arrived at when End day was set."""
    end = (marks or {}).get("end_ts")
    if not end:
        return None
    here = [s for s in stops if s["arrive_ts"] <= end + END_SLACK_S]
    return here[-1] if here else None


def drive_counts(stops, drives, marks=None):
    """{drive id: counts toward work totals}. A drive counts unless one end is a Personal or Lunch stop.
    Exceptions, driven by the Start day / End day marks:
      * the day's first drive out of a Personal stop (home) counts if Start day was set before that drive
        ended (you started work before arriving at the first job), unless it was set on arrival;
      * the drive INTO the stop where the day ended counts when that stop is Personal (home, or an errand on
        the way home): work time ends on arriving there. Drives after it never count.
    Ending at a customer or vendor stops the clock when you leave, so the drive home is not counted (its miles
    are documented separately, see end_info). Mid-day Personal stops (errands) always stay excluded."""
    marks = marks or {}
    personal = {s["id"] for s in stops if s["status"] in OFF_CLOCK}
    lunch = {s["id"] for s in stops if s["status"] == "Lunch"}
    last = end_stop(stops, marks)
    idx = {s["id"]: k for k, s in enumerate(stops)}

    def lunch_between_jobs(lid):
        """Lunch between two different work stops is part of the trip: the drives before and after it count
        (together they are one drive). Lunch next to home, or a lunch trip back to the same site, does not."""
        k = idx[lid]
        if k == 0 or k + 1 >= len(stops):
            return False
        x, y = stops[k - 1], stops[k + 1]
        return _counts_as_work(x) and _counts_as_work(y) and not same_site(x, y)

    out = {}
    for i, d in enumerate(drives):
        frm, to = d["from_stop_id"] in personal, d["to_stop_id"] in personal
        ok = not frm and not to
        if d["from_stop_id"] in lunch or d["to_stop_id"] in lunch:
            out[d["id"]] = lunch_between_jobs(d["from_stop_id"] if d["from_stop_id"] in lunch else d["to_stop_id"])
            continue
        # Start day tapped within START_SLACK_S of arriving means "started on arrival": no commute
        if frm and not to and i == 0 and marks.get("start_ts") and marks["start_ts"] < d["arrive_ts"] - START_SLACK_S:
            ok = True
        if (to and not frm and last is not None and d["to_stop_id"] == last["id"]
                and last["status"] == "Personal"):
            ok = True
        out[d["id"]] = ok
    return out


def _home_site(conn):
    return conn.execute("SELECT * FROM known_sites WHERE kind='home' AND lat IS NOT NULL ORDER BY id LIMIT 1").fetchone()


def _is_home_stop(s, home):
    return (home is not None and s["status"] == "Personal" and (
        s["known_site_id"] == home["id"]
        or (s["lat"] is not None and haversine_m(s["lat"], s["lon"], home["lat"], home["lon"]) <= 200)))


def _counts_as_work(s):
    return s["status"] not in OFF_CLOCK + ("Traffic",) and (s["kind"] == "stop" or s["status"] == "Confirmed")


def mileage(conn, cfg, stops, drives):
    """Billable mileage, always Home -> work stops in order -> Home. Personal and Lunch stops are left out:
    where one sat between two work stops (or an errand came before the drive home) the direct road distance
    is used instead of the detour. Traffic stops are on the road, so the real drives through them are summed.
    Legs touching Home use the real drive when the phone recorded it (Home stop right next to the work stop),
    otherwise the road distance. Returns None until a Home place is saved."""
    home = _home_site(conn)
    work = [i for i, s in enumerate(stops) if _counts_as_work(s)]
    if home is None or not work:
        return None
    by_from = {d["from_stop_id"]: d for d in drives}

    def chain(i, j):   # real drives from stop i to stop j
        return sum((by_from.get(stops[k]["id"]) or {}).get("distance_m", 0) for k in range(i, j))

    def only_traffic(i, j):
        return all(stops[k]["status"] == "Traffic" for k in range(i + 1, j))

    def name(i):
        return stops[i]["business_name"] or f"Stop at {stops[i]['arrive']}"

    def road(a_pt, b_pt, fallback):
        if a_pt[0] is None or b_pt[0] is None:
            return fallback, "gps"
        m, how = routing.route_m(conn, cfg, a_pt, b_pt)
        return m, how

    pt = lambda s: (s["lat"], s["lon"])
    hpt = (home["lat"], home["lon"])
    legs = []

    def add(a, b, m, how, to_id=None):
        legs.append({"from": a, "to": b, "to_id": to_id, "miles": round(m / 1609.344, 1), "method": how})

    first, last = work[0], work[-1]
    prev = next((k for k in range(first - 1, -1, -1) if stops[k]["status"] != "Traffic"), None)
    if prev is not None and _is_home_stop(stops[prev], home) and only_traffic(prev, first):
        add("Home", name(first), chain(prev, first), "gps", stops[first]["id"])
    else:
        add("Home", name(first), *road(hpt, pt(stops[first]), 0), to_id=stops[first]["id"])
    for i, j in zip(work, work[1:]):
        if same_site(stops[i], stops[j]):
            continue      # back at the same site (e.g. after lunch): no miles between
        if only_traffic(i, j):
            add(name(i), name(j), chain(i, j), "gps", stops[j]["id"])
        else:
            add(name(i), name(j), *road(pt(stops[i]), pt(stops[j]), chain(i, j)), to_id=stops[j]["id"])
    nxt = next((k for k in range(last + 1, len(stops)) if stops[k]["status"] != "Traffic"), None)
    if nxt is not None and _is_home_stop(stops[nxt], home) and only_traffic(last, nxt):
        add(name(last), "Home", chain(last, nxt), "gps")
    else:
        add(name(last), "Home", *road(pt(stops[last]), hpt, 0))
    return {"basis": "home-to-home", "total_miles": round(sum(l["miles"] for l in legs), 1), "legs": legs}


def end_info(conn, cfg, stops, marks, mil=None):
    """Where the day ended. The miles from the last work stop on to Home are part of the mileage either way;
    `counted` says whether the drive home also counts as work time (only when the day ended at home)."""
    last = end_stop(stops, marks)
    if last is None:
        return None
    home = _home_site(conn)
    at_home = _is_home_stop(last, home)
    kind = "home" if at_home else ("personal" if last["status"] == "Personal" else "job")
    out = {"time": marks["end"], "stop_id": last["id"], "kind": kind, "place": last["business_name"] or "",
           "miles_to_home": None, "method": "", "counted": at_home}
    if mil:
        leg = mil["legs"][-1]
        out["miles_to_home"], out["method"] = leg["miles"], leg["method"]
    return out


def totals(stops, drives, marks=None):
    def counts(s):
        return s["status"] not in OFF_CLOCK + ("Traffic",) and (s["kind"] == "stop" or s["status"] == "Confirmed")

    personal = {s["id"] for s in stops if s["status"] == "Personal"}
    traffic_s = sum(s["depart_ts"] - s["arrive_ts"] for s in stops if s["status"] == "Traffic")
    on_site = sum(s["depart_ts"] - s["arrive_ts"] for s in stops if counts(s))
    ok = drive_counts(stops, drives, marks)
    live = [d for d in drives if ok[d["id"]]]
    return {
        "on_site_s": on_site,
        "drive_s": sum(d["arrive_ts"] - d["depart_ts"] for d in live) + traffic_s,
        "traffic_s": traffic_s,
        "miles_approx": round(sum(d["distance_m"] for d in live) / 1609.344, 1),
        "confirmed": sum(1 for s in stops if s["status"] == "Confirmed"),
        "draft": sum(1 for s in stops if s["status"] == "Draft" and s["kind"] == "stop"),
        "draft_short": sum(1 for s in stops if s["status"] == "Draft" and s["kind"] == "short"),
        "personal": len(personal),
        "lunch": sum(1 for s in stops if s["status"] == "Lunch"),
        "traffic": sum(1 for s in stops if s["status"] == "Traffic"),
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
        "edited": bool(r["edited"]), "suggest": suggestion(conn, r),
        "usual": parts.usual_for(conn, r) if r["status"] not in OFF_CLOCK + ("Traffic",) else [],
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
    marks = day_marks(conn, cfg, date)
    ok = drive_counts(stops, drives, marks)
    for d in drives:
        d["counts"] = ok[d["id"]]
    tot = totals(stops, drives, marks)
    mil = mileage(conn, cfg, stops, drives)
    if mil:
        tot["miles_approx"] = mil["total_miles"]
    out = {"date": date, "tz": cfg.app_tz, "stops": stops, "drives": drives,
           "totals": tot, "undo": undo_available(conn, date),
           "marks": marks, "mileage": mil, "end_day": end_info(conn, cfg, stops, marks, mil)}
    if include_path:
        pts = kept_points(conn, cfg, date)
        out["path"] = [[p["lat"], p["lon"]] for p in pts]
        out["kept_count"] = len(pts)
    return out
