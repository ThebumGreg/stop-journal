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
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import calendar_feed, detect, parts, photos as photo_store, points as point_store, routing
from .errors import EditError
from .geo import haversine_m
from .places import known

STATUSES = ("Draft", "Confirmed", "Personal", "Lunch", "Traffic")
OFF_CLOCK = ("Personal", "Lunch")   # not work: the stop and the drives to and from it are excluded

# What a stop is for (`stops.stop_type`), picked first on the stop card. The time rules still run on the status:
# a type listed in TYPE_STATUS sets that status (Gas is on the road like Traffic: its minutes are drive time,
# not on-site time); the others are Draft until confirmed. Stops saved before types existed derive one.
STOP_TYPES = {"job": "Job", "vendor": "Vendor", "office": "Office", "gas": "Gas", "break": "Short break",
              "lunch": "Lunch", "personal": "Personal", "traffic": "Traffic"}
# gas and a short break (restroom, coffee) run on the Traffic time rules: paid, counted as drive time, not on-site time
TYPE_STATUS = {"gas": "Traffic", "break": "Traffic", "lunch": "Lunch", "personal": "Personal", "traffic": "Traffic"}
STATUS_TYPE = {"Lunch": "lunch", "Personal": "personal", "Traffic": "traffic"}
FUEL_CATEGORIES = ("amenity=fuel", "place=fuel", "google=gas_station")


def _type_fits(t, status):
    return TYPE_STATUS.get(t) == status if t in TYPE_STATUS else status in ("Draft", "Confirmed")


def stop_type(row, site_kind=""):
    """The stop's type: the stored one when it agrees with the status, else one derived from the status and the
    saved place ('' = not chosen yet, a new Draft stop)."""
    st, t = row["status"] or "Draft", row["stop_type"]
    if t in STOP_TYPES and _type_fits(t, st):
        return t
    if st in STATUS_TYPE:
        return STATUS_TYPE[st]
    if site_kind in ("vendor", "office"):
        return site_kind
    return "job" if st == "Confirmed" or site_kind in ("client", "home") else ""

# Work categories (the SOP's seven). The value is how the hours are billed: hourly (time and material),
# project (tracked against a quote or migration) or non_billable. The technician only picks the category.
WORK_CATEGORIES = {
    "Quoted project": "project", "Time & Material": "hourly", "White-label migration": "project",
    "Internal operations": "non_billable", "Administrative": "non_billable",
    "Customer outreach": "non_billable", "Training": "non_billable",
}
VISIT_RESULTS = {"complete": "Complete", "return": "Return visit needed",
                 "waiting": "Waiting (parts or customer)"}




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


GAP_SPEED_MS = 11.2      # about 25 mph: used to estimate a drive the phone did not record
GAP_MIN_DRIVE_S = 300
HERE_NOW_S = 900         # a stop is 'here now' while its newest point is less than 15 minutes old


def data_gap(conn, cfg, date, start, end):
    """The longest stretch with no location points between start and end, as (from_ts, to_ts), or None
    when it is not longer than the max-gap setting."""
    times = [start] + [p["tst"] for p in kept_points(conn, cfg, date, start, end)] + [end]
    best = max(zip(times, times[1:]), key=lambda g: g[1] - g[0], default=None)
    if best and best[1] - best[0] > cfg.stop_max_gap_min * 60:
        return best
    return None


def _times_typed(a, b):
    """True when you typed the departure of stop a or the arrival of stop b: those times are trusted as they are."""
    return bool(a["times_edited"] or a["depart_set"] or b["times_edited"] or b["arrive_set"])


def _gap_estimate(conn, cfg, date, a, b, dep, arr, dist):
    """A drive that crosses a hole in the data (phone off or frozen) is not an hour long just because the phone was
    silent. Unless you typed one of the times yourself, assume the trip took distance / ~25 mph (min 5 min) and ended
    when the next point arrived. Returns the departure to use."""
    if _times_typed(a, b):
        return dep
    if not data_gap(conn, cfg, date, dep, arr):
        return dep
    if not dist and a["lat"] is not None and b["lat"] is not None:
        dist = haversine_m(a["lat"], a["lon"], b["lat"], b["lon"]) * CROW_FLIES_FACTOR
    est = max(GAP_MIN_DRIVE_S, int(dist / GAP_SPEED_MS))
    return max(dep, arr - est)


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
        if not (d and d["edited"]):
            dep = _gap_estimate(conn, cfg, date, a, b, dep, arr, dist)
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
        elif (not s["times_edited"] and
              ((not s["depart_set"] and it["depart"] > s["depart_utc"]) or
               (not s["arrive_set"] and it["arrive"] < s["arrive_utc"]))):
            arrive = s["arrive_utc"] if s["arrive_set"] else min(s["arrive_utc"], it["arrive"])
            depart = s["depart_utc"] if s["depart_set"] else max(s["depart_utc"], it["depart"])
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
    gone = [r["id"] for r in conn.execute("SELECT id FROM stops WHERE date=? AND source!='manual'", (date,))]
    # photos are kept: each goes to the new stop that covers the middle of its old stop's time
    pics = conn.execute(f"SELECT p.id, (s.arrive_utc + s.depart_utc) / 2 AS mid FROM photos p JOIN stops s ON s.id=p.stop_id "
                        f"WHERE s.date=? AND s.source!='manual'", (date,)).fetchall()
    parts.release_stops(conn, gone)
    conn.executemany("DELETE FROM follow_ups WHERE stop_id=?", [(i,) for i in gone])
    conn.executemany("DELETE FROM fuel WHERE stop_id=?", [(i,) for i in gone])
    conn.execute("DELETE FROM stops WHERE date=? AND source!='manual'", (date,))
    conn.execute("DELETE FROM drives WHERE date=?", (date,))
    materialize(conn, cfg, date, kept, run_cfg)
    for p in pics:
        to = conn.execute("SELECT id FROM stops WHERE date=? AND arrive_utc<=? AND depart_utc>=? ORDER BY arrive_utc LIMIT 1",
                          (date, p["mid"], p["mid"])).fetchone()
        if to:
            conn.execute("UPDATE photos SET stop_id=? WHERE id=?", (to["id"], p["id"]))
    conn.commit()


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


# ---- saved place for a picked type ---------------------------------------
HOME_NEAR_M = 300      # a Personal stop this close to Home is Home


def _place_for_type(conn, s, t):
    """Picking Office fills the saved Office (nearest one); picking Personal at Home fills Home.
    Only when the stop has no name yet, or its name came from a saved place of another kind."""
    kind = {"office": "office", "personal": "home"}.get(t)
    if not kind:
        return None
    if (s["business_name"] or "").strip():
        cur = conn.execute("SELECT kind FROM known_sites WHERE id=?", (s["known_site_id"],)).fetchone() \
            if s["known_site_id"] else None
        if not cur or (cur["kind"] or "client") == kind:
            return None
    rows = conn.execute("SELECT * FROM known_sites WHERE kind=?", (kind,)).fetchall()
    if not rows:
        return None
    if s["lat"] is None:
        return rows[0] if len(rows) == 1 and kind == "office" else None
    dist = lambda r: haversine_m(s["lat"], s["lon"], r["lat"], r["lon"]) if r["lat"] is not None else 1e12
    best = min(rows, key=dist)
    if kind == "home" and dist(best) > max(HOME_NEAR_M, best["radius_m"] or 0):
        return None        # a Personal errand somewhere else stays unnamed
    return best


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
    fu = [dict(r) for r in conn.execute(
        f"SELECT * FROM follow_ups WHERE stop_id IN ({marks})", ids)] if ids else []
    fl = [dict(r) for r in conn.execute(
        f"SELECT * FROM fuel WHERE stop_id IN ({marks})", ids)] if ids else []
    drives = [dict(r) for r in conn.execute("SELECT * FROM drives WHERE date=?", (date,))]
    conn.execute("INSERT INTO undo_log(date,ts,label,payload) VALUES (?,?,?,?)",
                 (date, int(time.time()), label,
                  json.dumps({"stops": stops, "drives": drives, "stop_parts": sp, "follow_ups": fu, "fuel": fl})))
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
    gone = [r["id"] for r in conn.execute("SELECT id FROM stops WHERE date=?", (date,))]
    parts.release_stops(conn, gone)
    conn.executemany("DELETE FROM follow_ups WHERE stop_id=?", [(i,) for i in gone])
    conn.executemany("DELETE FROM fuel WHERE stop_id=?", [(i,) for i in gone])
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
    for rec in snap.get("follow_ups", []):
        put("follow_ups", rec)
    for rec in snap.get("fuel", []):
        put("fuel", rec)
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
    if "work_category" in f:
        wc = str(f["work_category"] or "").strip()
        if wc and wc not in WORK_CATEGORIES:
            raise EditError("Choose one of the listed categories.")
        upd["work_category"] = wc or None
    if "ticket" in f:
        t = str(f["ticket"] or "").strip()
        if len(t) > 80:
            raise EditError("The job or ticket reference is too long (80 characters at most).")
        upd["ticket"] = t or None
    if "visit_result" in f:
        vr = str(f["visit_result"] or "").strip()
        if vr and vr not in VISIT_RESULTS:
            raise EditError("Choose one of the listed visit results.")
        upd["visit_result"] = vr or None
    if "status" in f:
        if f["status"] not in STATUSES:
            raise EditError("Status must be Draft, Confirmed, Personal, Lunch or Traffic.")
        upd["status"] = f["status"]
    if "stop_type" in f:          # the type decides the status for gas/lunch/personal/traffic
        t = str(f["stop_type"] or "").strip().lower() or None
        if t and t not in STOP_TYPES:
            raise EditError("Stop type must be one of: " + ", ".join(STOP_TYPES.values()) + ".")
        upd["stop_type"] = t
        if t in TYPE_STATUS:
            upd["status"] = TYPE_STATUS[t]
        elif upd.get("status", s["status"]) not in ("Draft", "Confirmed"):
            upd["status"] = "Draft"
        fill = _place_for_type(conn, s, t) if t != s["stop_type"] and "business_name" not in f else None
        if fill:
            upd.update(business_name=fill["name"], address=fill["address"] or "", known_site_id=fill["id"])
    elif "status" in f:           # status set the old way: keep the stored type in step
        new = upd["status"]
        if not _type_fits(s["stop_type"], new):
            upd["stop_type"] = STATUS_TYPE.get(new)
    if "lat" in f or "lon" in f:
        upd["lat"] = _float(f.get("lat"), -90, 90, "Latitude")
        upd["lon"] = _float(f.get("lon"), -180, 180, "Longitude")
    if "arrive" in f or "depart" in f:
        a = parse_time(cfg, date, f["arrive"]) if "arrive" in f else s["arrive_utc"]
        d = parse_time(cfg, date, f["depart"]) if "depart" in f else s["depart_utc"]
        _check_window(conn, cfg, date, sid, a, d)
        upd["arrive_utc"], upd["depart_utc"] = a, d
        # only the side you actually changed is pinned: edit the arrival and the departure keeps growing while you are on site
        if a != s["arrive_utc"]:
            upd["arrive_set"] = 1
        if d != s["depart_utc"]:
            upd["depart_set"] = 1
            end_ts = day_marks(conn, cfg, date)["end_ts"]
            if end_ts and end_ts == s["depart_utc"] and s["status"] not in OFF_CLOCK:
                set_mark(conn, cfg, date, "end", ts=d)       # End Day was "here": it follows the new departure

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
                upd.get("category", s["category"] or ""), kind=upd.get("stop_type", s["stop_type"]))
        if s["status"] != "Confirmed" and not upd.get("work_category", s["work_category"]):
            hint = last_category(conn, s["id"], name, upd.get("known_site_id", s["known_site_id"]))
            if not hint:     # a vendor stop (parts pickup) is internal work unless you say otherwise
                site = conn.execute("SELECT kind FROM known_sites WHERE id=?",
                                    (upd.get("known_site_id", s["known_site_id"]),)).fetchone()
                if (site and site["kind"] == "vendor") or upd.get("stop_type", s["stop_type"]) == "vendor":
                    hint = "Internal operations"
            if hint:         # same business as before: start from the category used last time
                upd["work_category"] = hint
    upd["edited"] = 1
    sets = ", ".join(f"{k}=?" for k in upd)  # keys are whitelisted above
    conn.execute(f"UPDATE stops SET {sets} WHERE id=?", (*upd.values(), sid))
    _finish(conn, cfg, date)
    cur = _stop(conn, sid)
    if (status == "Personal" and s["status"] != "Personal" and name and cur["lat"] is not None
            and not cur["known_site_id"] and "home" in name.lower()):
        # a Personal stop named Home is saved as the Home place so it is pre-filled next time. Anything else (a
        # vendor, an office) is saved on purpose with "Save as place" in the card's More menu.
        save_place(conn, cfg, sid, {"kind": "home", "name": name})
    return date


def leave_now(conn, cfg, sid):
    """You are leaving this stop right now: set its departure to the current time (today only)."""
    s = _stop(conn, sid)
    now = int(time.time())
    lo, hi = day_range(s["date"], cfg.app_tz)
    if not lo <= now <= hi:
        raise EditError("Leaving now only works for today. Enter the departure time instead.")
    if now <= s["arrive_utc"] + 60:
        raise EditError("You arrived less than a minute ago.")
    return patch_stop(conn, cfg, sid, {"depart": fmt_time(cfg, now)})


def follow_gps(conn, cfg, sid):
    """Undo a typed departure (or Leaving now): the stop's "Left" follows the GPS again. The departure is set back to
    where detection puts it now, and keeps growing on sync while you are there."""
    s = _stop(conn, sid)
    if s["source"] != "detected":
        raise EditError("This stop was added by hand, so there is no GPS to follow.")
    if s["times_edited"]:
        raise EditError("This stop's times came from a split or merge; type the departure instead.")
    date = s["date"]
    conn.execute("UPDATE stops SET depart_set=0 WHERE id=?", (sid,))
    items, _ = detect.detect(kept_points(conn, cfg, date), cfg)
    best, best_o = None, 0
    for it in items:                               # the detected visit that overlaps this stop the most
        o = min(it["depart"], s["depart_utc"]) - max(it["arrive"], s["arrive_utc"])
        if o > best_o:
            best, best_o = it, o
    if best:
        depart = max(best["depart"], s["arrive_utc"] + 60)
        if depart != s["depart_utc"] and _free(conn, date, sid, s["arrive_utc"], depart):
            end_ts = day_marks(conn, cfg, date)["end_ts"]
            conn.execute("UPDATE stops SET depart_utc=? WHERE id=?", (depart, sid))
            if end_ts and end_ts == s["depart_utc"] and s["status"] not in OFF_CLOCK:
                set_mark(conn, cfg, date, "end", ts=depart)
    _finish(conn, cfg, date)
    return date


def last_notes(conn, sid, name, site_id):
    """The Work performed from the last finished visit to the same place (saved place, else same name): {date, text}."""
    r = conn.execute(
        "SELECT date, notes FROM stops WHERE id!=? AND status='Confirmed' AND COALESCE(trim(notes),'')!='' "
        "AND ((? IS NOT NULL AND known_site_id=?) OR (?!='' AND lower(business_name)=lower(?))) "
        "ORDER BY date DESC, arrive_utc DESC LIMIT 1", (sid, site_id, site_id, name or "", name or "")).fetchone()
    return {"date": r["date"], "text": r["notes"]} if r else None


def last_category(conn, sid, name, site_id):
    """The work category last used for this business (saved place, else same name), or ''."""
    r = conn.execute(
        "SELECT work_category FROM stops WHERE id!=? AND status='Confirmed' AND COALESCE(work_category,'')!='' "
        "AND ((? IS NOT NULL AND known_site_id=?) OR (?!='' AND lower(business_name)=lower(?))) "
        "ORDER BY date DESC, arrive_utc DESC LIMIT 1", (sid, site_id, site_id, name or "", name or "")).fetchone()
    return r["work_category"] if r else ""


def delete_stop(conn, cfg, sid):
    date = _stop(conn, sid)["date"]
    snapshot_day(conn, date, "Deleted a stop")
    parts.release_stops(conn, [sid])
    conn.execute("DELETE FROM follow_ups WHERE stop_id=?", (sid,))
    conn.execute("DELETE FROM fuel WHERE stop_id=?", (sid,))
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
    sid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    if f.get("stop_type"):
        patch_stop(conn, cfg, sid, {"stop_type": f["stop_type"]})
        return
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
        "address=?, category=?, known_site_id=?, notes=?, status=?, kind=?, work_category=?, ticket=?, "
        "visit_result=?, stop_type=?, edited=1, times_edited=1 WHERE id=?",
        (a["arrive_utc"], b["depart_utc"], lat, lon, first["business_name"],
         first["address"], first["category"], first["known_site_id"], notes, status,
         kind, a["work_category"] or b["work_category"], a["ticket"] or b["ticket"],
         b["visit_result"] or a["visit_result"],
         a["stop_type"] if a["stop_type"] == b["stop_type"] and _type_fits(a["stop_type"], status) else None, a["id"]))
    conn.execute("UPDATE stop_parts SET stop_id=? WHERE stop_id=?", (a["id"], b["id"]))
    conn.execute("UPDATE follow_ups SET stop_id=? WHERE stop_id=?", (a["id"], b["id"]))
    conn.execute("UPDATE OR IGNORE fuel SET stop_id=? WHERE stop_id=?", (a["id"], b["id"]))
    conn.execute("UPDATE photos SET stop_id=? WHERE stop_id=?", (a["id"], b["id"]))
    conn.execute("DELETE FROM fuel WHERE stop_id=?", (b["id"],))
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
    if kind in ("vendor", "office") and s["status"] in ("Draft", "Confirmed"):
        conn.execute("UPDATE stops SET stop_type=? WHERE id=?", (kind, sid))
    if kind == "vendor" and not s["work_category"]:
        conn.execute("UPDATE stops SET work_category='Internal operations' WHERE id=?", (sid,))
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


# ---- follow-ups (what is still open, who owns it, when it is due) --------
def _followup_fields(f, full):
    out = {}
    if full or "text" in f:
        t = str(f.get("text") or "").strip()
        if not t:
            raise EditError("Say what the follow-up is.")
        if len(t) > 300:
            raise EditError("The follow-up is too long (300 characters at most).")
        out["text"] = t
    if full or "owner" in f:
        o = str(f.get("owner") or "").strip()
        if len(o) > 60:
            raise EditError("The owner is too long (60 characters at most).")
        out["owner"] = o
    if full or "due" in f:
        d = str(f.get("due") or "").strip()
        if d:
            try:
                datetime.strptime(d, "%Y-%m-%d")
            except ValueError:
                raise EditError("The due date looks like 2026-11-18.")
        out["due"] = d
    return out


def add_followup(conn, cfg, stop_id, f):
    date = _stop(conn, stop_id)["date"]
    v = _followup_fields(f, True)
    conn.execute("INSERT INTO follow_ups(stop_id,text,owner,due) VALUES (?,?,?,?)",
                 (stop_id, v["text"], v["owner"], v["due"]))
    conn.execute("UPDATE stops SET edited=1 WHERE id=?", (stop_id,))
    conn.commit()
    return date


def _followup(conn, fid):
    r = conn.execute("SELECT * FROM follow_ups WHERE id=?", (fid,)).fetchone()
    if not r:
        raise EditError("That follow-up no longer exists.")
    return r


def patch_followup(conn, cfg, fid, f):
    r = _followup(conn, fid)
    v = _followup_fields(f, False)
    if v:
        sets = ", ".join(f"{k}=?" for k in v)   # keys come from _followup_fields
        conn.execute(f"UPDATE follow_ups SET {sets} WHERE id=?", (*v.values(), fid))
        conn.commit()
    return _stop(conn, r["stop_id"])["date"]


def remove_followup(conn, cfg, fid):
    r = _followup(conn, fid)
    conn.execute("DELETE FROM follow_ups WHERE id=?", (fid,))
    conn.commit()
    return _stop(conn, r["stop_id"])["date"]


def followups_for_day(conn, date):
    out = {}
    for r in conn.execute("SELECT f.* FROM follow_ups f JOIN stops s ON s.id=f.stop_id WHERE s.date=? "
                          "ORDER BY f.id", (date,)):
        out.setdefault(r["stop_id"], []).append({"id": r["id"], "text": r["text"],
                                                  "owner": r["owner"] or "", "due": r["due"] or ""})
    return out


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


VENDOR_SHOPS = {"hardware", "doityourself", "trade", "electrical", "electronics", "computer", "building_materials",
                "tool_hire", "plumbing", "paint", "car_parts", "wholesale"}


def _looks_like_vendor(stop, vendor_names):
    """Offer 'Save as Vendor' when the name matches a vendor used on parts or the place type is a supply shop."""
    name = (stop["business_name"] or "").strip().lower()
    if not name:
        return False
    if any(v and (v in name or name in v) for v in vendor_names):
        return True
    cat = stop["category"] or ""
    kind, _, val = cat.partition("=")
    return (kind == "shop" and val in VENDOR_SHOPS) or (
        kind == "google" and val in ("hardware_store", "home_improvement_store", "electronics_store"))


def day_payload(conn, cfg, date, include_path=False):
    used = parts.for_day(conn, date)
    fups = followups_for_day(conn, date)
    fuel = fuel_for_day(conn, date)
    pics = photo_store.for_day(conn, date)
    site_kind = {r["id"]: r["kind"] for r in conn.execute("SELECT id, kind FROM known_sites")}
    vendor_names = {(r["vendor"] or "").strip().lower() for r in
                    conn.execute("SELECT DISTINCT vendor FROM parts WHERE vendor IS NOT NULL")} - {""}
    stops = [{
        "site_kind": site_kind.get(r["known_site_id"]) or "",
        "vendor_hint": site_kind.get(r["known_site_id"]) in (None, "client") and _looks_like_vendor(r, vendor_names),
        "parts": used.get(r["id"], []), "follow_ups": fups.get(r["id"], []),
        "work_category": r["work_category"] or "", "ticket": r["ticket"] or "",
        "visit_result": r["visit_result"] or "",
        "category_hint": "" if r["work_category"] else last_category(
            conn, r["id"], r["business_name"], r["known_site_id"]),
        "stop_type": stop_type(r, site_kind.get(r["known_site_id"]) or ""),
        "gas_hint": (r["category"] or "") in FUEL_CATEGORIES or r["fuel_check"] == 1,
        "fuel": fuel.get(r["id"]), "photos": pics.get(r["id"], []),
        "needs": stop_needs(r, stop_type(r, site_kind.get(r["known_site_id"]) or ""),
                            "" if r["work_category"] else last_category(conn, r["id"], r["business_name"], r["known_site_id"]),
                            fuel.get(r["id"])),
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
        "depart_pinned": bool(r["depart_set"] or r["times_edited"]),
        "last_notes": None if (r["notes"] or "").strip() or r["status"] in OFF_CLOCK + ("Traffic",)
        else last_notes(conn, r["id"], r["business_name"], r["known_site_id"]),
        "can_follow_gps": r["source"] == "detected" and not r["times_edited"] and bool(r["depart_set"]),
        "usual": parts.usual_for(conn, r) if r["status"] not in OFF_CLOCK + ("Traffic",) else [],
    } for r in _ordered(conn, date)]
    now = int(time.time())
    lo, hi = day_range(date, cfg.app_tz)
    pts = kept_points(conn, cfg, date) if lo <= now <= hi else []
    last_tst = pts[-1]["tst"] if pts else None
    fresh = bool(last_tst and now - last_tst <= HERE_NOW_S)
    for i, st in enumerate(stops):
        # still on site: the newest point is recent and sits inside this (the last) stop
        st["here_now"] = bool(fresh and i == len(stops) - 1 and st["depart_ts"] >= last_tst - 300
                              and st["arrive_ts"] <= last_tst)
    drives = [{
        "id": r["id"], "from_stop_id": r["from_stop_id"], "to_stop_id": r["to_stop_id"],
        "depart": fmt_time(cfg, r["depart_utc"]), "arrive": fmt_time(cfg, r["arrive_utc"]),
        "depart_ts": r["depart_utc"], "arrive_ts": r["arrive_utc"],
        "minutes": round((r["arrive_utc"] - r["depart_utc"]) / 60),
        "miles_approx": round((r["distance_m"] or 0) / 1609.344, 1),
        "distance_m": r["distance_m"] or 0,
        "notes": r["notes"] or "", "edited": bool(r["edited"]),
    } for r in conn.execute("SELECT * FROM drives WHERE date=? ORDER BY seq", (date,))]
    by_id = {r["id"]: r for r in _ordered(conn, date)}
    for d in drives:
        a, b = by_id.get(d["from_stop_id"]), by_id.get(d["to_stop_id"])
        gap = data_gap(conn, cfg, date, a["depart_utc"], b["arrive_utc"]) if a and b else None
        d["gap"] = {"from": fmt_time(cfg, gap[0]), "to": fmt_time(cfg, gap[1]),
                    "minutes": round((gap[1] - gap[0]) / 60)} if gap else None
        d["estimated"] = bool(gap and not d["edited"] and not _times_typed(a, b))
    marks = day_marks(conn, cfg, date)
    ok = drive_counts(stops, drives, marks)
    for d in drives:
        d["counts"] = ok[d["id"]]
    tot = totals(stops, drives, marks)
    mil = mileage(conn, cfg, stops, drives)
    if mil:
        tot["miles_approx"] = mil["total_miles"]
    out = {"date": date, "tz": cfg.app_tz, "auto_sync_min": cfg.auto_sync_min, "stops": stops, "drives": drives,
           "totals": tot, "undo": undo_available(conn, date),
           "marks": marks, "mileage": mil, "end_day": end_info(conn, cfg, stops, marks, mil)}
    out["calendar"], out["calendar_error"] = calendar_for_day(cfg, date, stops, now)
    if include_path:
        pts = kept_points(conn, cfg, date)
        out["path"] = [[p["lat"], p["lon"]] for p in pts]
        out["kept_count"] = len(pts)
    return out


# ---- fuel (Gas stops) ----------------------------------------------------
def _fuel_row(r):
    g, t = r["gallons"], r["total"]
    return {"gallons": g, "total": t, "odometer": r["odometer"],
            "price_per_gallon": round(t / g, 3) if g and t else None}


def fuel_for_day(conn, date):
    return {r["stop_id"]: _fuel_row(r) for r in conn.execute(
        "SELECT f.* FROM fuel f JOIN stops s ON s.id=f.stop_id WHERE s.date=?", (date,))}


def set_fuel(conn, cfg, sid, f):
    """Gallons, total paid and odometer for a Gas stop. Blank clears a field."""
    s = _stop(conn, sid)
    if stop_type(s) != "gas":
        raise EditError("Mark this stop as Gas first.")

    def num(key, hi, what, cast=float):
        v = f.get(key)
        if v in (None, ""):
            return None
        try:
            x = cast(float(str(v).replace(",", "").replace("$", "").strip()))
        except ValueError:
            raise EditError(f"{what} must be a number.")
        if not 0 < x <= hi:
            raise EditError(f"{what} is out of range.")
        return x

    cur = conn.execute("SELECT * FROM fuel WHERE stop_id=?", (sid,)).fetchone()
    vals = {k: (cur[k] if cur else None) for k in ("gallons", "total", "odometer")}
    if "gallons" in f:
        vals["gallons"] = num("gallons", 500, "Gallons")
    if "total" in f:
        vals["total"] = num("total", 10000, "Total")
        vals["total"] = None if vals["total"] is None else round(vals["total"], 2)
    if "odometer" in f:
        vals["odometer"] = num("odometer", 3_000_000, "Odometer", int)
    conn.execute("INSERT INTO fuel(stop_id,gallons,total,odometer) VALUES (?,?,?,?) "
                 "ON CONFLICT(stop_id) DO UPDATE SET gallons=excluded.gallons, total=excluded.total, "
                 "odometer=excluded.odometer", (sid, vals["gallons"], vals["total"], vals["odometer"]))
    conn.commit()
    return s["date"]


MPG_MAX_MILES = 1500   # odometer gaps larger than this (a missed fill-up, a typo) are not used for MPG


def fuel_log(conn, cfg, date_from=None, date_to=None):
    """Fill-ups (Gas stops) in time order, with MPG from the odometer change since the previous fill-up
    (full-tank method: the gallons at this fill-up replace what was burned since the last one)."""
    rows = conn.execute(
        "SELECT s.id, s.date, s.arrive_utc, s.business_name, f.gallons, f.total, f.odometer FROM fuel f "
        "JOIN stops s ON s.id=f.stop_id WHERE s.stop_type='gas' AND s.status='Traffic' "
        "ORDER BY s.date, s.arrive_utc").fetchall()
    out, prev_odo = [], None
    for r in rows:
        miles = mpg = None
        if r["odometer"] and prev_odo and 0 < r["odometer"] - prev_odo <= MPG_MAX_MILES:
            miles = r["odometer"] - prev_odo
            mpg = round(miles / r["gallons"], 1) if r["gallons"] else None
        if r["odometer"]:
            prev_odo = r["odometer"]
        if (date_from and r["date"] < date_from) or (date_to and r["date"] > date_to):
            continue
        out.append({"stop_id": r["id"], "date": r["date"], "time": fmt_time(cfg, r["arrive_utc"]),
                    "station": r["business_name"] or "", **_fuel_row(r), "miles": miles, "mpg": mpg})
    return out


def fuel_stats(conn, cfg, date_from=None, date_to=None):
    log = fuel_log(conn, cfg, date_from, date_to)
    gal = sum(x["gallons"] or 0 for x in log)
    tot = sum(x["total"] or 0 for x in log)
    both = [x for x in log if x["gallons"] and x["total"]]
    bg, bt = sum(x["gallons"] for x in both), sum(x["total"] for x in both)
    m = [x for x in log if x["mpg"]]
    mi, mg = sum(x["miles"] for x in m), sum(x["gallons"] for x in m)
    mc = sum(x["total"] or 0 for x in m)
    return {"fills": log[::-1],   # newest first
            "summary": {"fill_ups": len(log), "gallons": round(gal, 2), "total": round(tot, 2),
                        "avg_price_per_gallon": round(bt / bg, 3) if bg else None,
                        "miles": mi, "mpg": round(mi / mg, 1) if mg else None,
                        "cost_per_mile": round(mc / mi, 3) if mi and mc else None}}


# ---- finishing a stop ------------------------------------------------------
FOLLOW_UP_FOR = {"return": "Return visit", "waiting": "Waiting on parts or customer"}


def stop_needs(row, stype, category_hint="", fuel=None):
    """What a stop still needs before it can be finished (shown on the card and the sheet, checked by Finish)."""
    if not stype:
        return ["stop type"]
    name = (row["business_name"] or "").strip()
    out = []
    if stype == "job":
        if not name:
            out.append("business name")
        if not row["work_category"] and not category_hint:
            out.append("category")
        if not (row["notes"] or "").strip():
            out.append("work performed")
        if not row["visit_result"]:
            out.append("visit result")
    elif stype in ("vendor", "office"):
        if not name:
            out.append("vendor name" if stype == "vendor" else "name")
    elif stype == "gas":
        if not (fuel and fuel.get("total")):
            out.append("total paid")
    return out


def finish_stop(conn, cfg, sid, f):
    """The end of a stop: check it has what its type needs, save the visit result (a return visit or waiting adds a
    follow-up for you), and confirm it. The departure is NOT set here: until you type it or tap Leaving now it keeps
    following the GPS, so finishing before you drive away is fine."""
    s = _stop(conn, sid)
    if "visit_result" in f:
        patch_stop(conn, cfg, sid, {"visit_result": f["visit_result"]})
        s = _stop(conn, sid)
    site = conn.execute("SELECT kind FROM known_sites WHERE id=?", (s["known_site_id"],)).fetchone() \
        if s["known_site_id"] else None
    stype = stop_type(s, site["kind"] if site else "")
    if stype in ("lunch", "personal", "traffic"):
        return s["date"]                      # nothing to finish: the type is the whole answer
    hint = "" if s["work_category"] else last_category(conn, sid, s["business_name"], s["known_site_id"])
    fuel = fuel_for_day(conn, s["date"]).get(sid)
    need = stop_needs(s, stype, hint, fuel)
    if need:
        raise EditError("Still needed: " + ", ".join(need) + ".")
    text = FOLLOW_UP_FOR.get(s["visit_result"]) if stype == "job" else None
    if text and not conn.execute("SELECT 1 FROM follow_ups WHERE stop_id=? AND text=?", (sid, text)).fetchone():
        add_followup(conn, cfg, sid, {"text": text, "owner": "Me", "due": f.get("due") or ""})
    if stype != "gas" and s["status"] != "Confirmed":
        patch_stop(conn, cfg, sid, {"status": "Confirmed"})
    return s["date"]


# ---- catching up without the page open, and stops left unfinished --------
_catch_up_lock = threading.Lock()     # the page's auto-sync and the alert thread never add the same stop twice


def catch_up(conn, client, cfg, date):
    """Fetch what is new on the Recorder for a day and add it (the page's auto-sync, and the alert thread's).
    Returns True when new points came in."""
    with _catch_up_lock:
        return _catch_up(conn, client, cfg, date)


def _catch_up(conn, client, cfg, date):
    if not is_processed(conn, date):
        point_store.load_day(conn, client, cfg, date)
        materialize(conn, cfg, date, kept_points(conn, cfg, date), cfg)
        return True
    last = client.last_tst()
    stored = conn.execute("SELECT MAX(tst_utc) FROM points WHERE device=?", (cfg.recorder_device,)).fetchone()[0] or 0
    if not (last and last > stored):
        return False
    point_store.load_day(conn, client, cfg, date, refresh=True, incremental=True)
    sync_new(conn, cfg, date, kept_points(conn, cfg, date), cfg)
    return True


LEFT_AFTER_S = 300        # gone from a stop this long (newer points elsewhere) = you drove away
LEFT_WINDOW_S = 4 * 3600  # only stops you left in the last few hours (no flood of old ones)


def left_unfinished(conn, cfg, date, now, since=None):
    """Work stops (Job, Vendor, Office, Gas, or no type yet) you have driven away from that are not finished.
    [(stop row, needs)]. Lunch, Personal and Traffic are never 'unfinished'."""
    pts = kept_points(conn, cfg, date)
    if not pts:
        return []
    last = pts[-1]["tst"]
    site_kind = {r["id"]: r["kind"] for r in conn.execute("SELECT id, kind FROM known_sites")}
    fuel = fuel_for_day(conn, date)
    out = []
    for r in _ordered(conn, date):
        if (r["kind"] or "stop") != "stop" or r["status"] in ("Personal", "Lunch") or \
                (r["status"] == "Traffic" and r["stop_type"] != "gas"):
            continue
        if since and r["arrive_utc"] < since:
            continue
        if last - r["depart_utc"] < LEFT_AFTER_S or now - r["depart_utc"] > LEFT_WINDOW_S:
            continue
        t = stop_type(r, site_kind.get(r["known_site_id"]) or "")
        hint = "" if r["work_category"] else last_category(conn, r["id"], r["business_name"], r["known_site_id"])
        needs = stop_needs(r, t, hint, fuel.get(r["id"]))
        if needs or (t != "gas" and r["status"] != "Confirmed"):
            out.append((r, needs or ["finish"]))
    return out


# ---- Places history: past visits to a place, and fill-ups per gas station ------------
SAME_NAME_NEAR_M = 300     # a stop with the place's name this close counts as a visit even if it was never linked


def _visit(conn, cfg, r, site_kind=""):
    t = stop_type(r, site_kind)
    notes = (r["notes"] or "").strip()
    first = notes.splitlines()[0] if notes else ""
    f = conn.execute("SELECT * FROM fuel WHERE stop_id=?", (r["id"],)).fetchone()
    return {"stop_id": r["id"], "date": r["date"], "arrive": fmt_time(cfg, r["arrive_utc"]),
            "depart": fmt_time(cfg, r["depart_utc"]), "minutes": round((r["depart_utc"] - r["arrive_utc"]) / 60),
            "stop_type": t, "type_label": STOP_TYPES.get(t, ""), "status": r["status"] or "Draft",
            "business_name": r["business_name"] or "",
            "work_category": r["work_category"] or "", "visit_result": VISIT_RESULTS.get(r["visit_result"] or "", ""),
            "ticket": r["ticket"] or "",
            "summary": first[:140] + ("…" if len(first) > 140 or len(notes.splitlines()) > 1 else ""),
            "parts": conn.execute("SELECT COUNT(*) FROM stop_parts WHERE stop_id=? AND COALESCE(kind,'part')!='expense'",
                                  (r["id"],)).fetchone()[0],
            "photos": conn.execute("SELECT COUNT(*) FROM photos WHERE stop_id=?", (r["id"],)).fetchone()[0],
            "fuel": _fuel_row(f) if f else None,
            "url": f"/day/{r['date']}?stop={r['id']}"}


def _site_rows(conn, sites):
    """Stops at any of these saved places: linked ones, plus unlinked stops with the same name within 300 m."""
    ids = {x["id"] for x in sites}
    names = {(x["name"] or "").strip().lower() for x in sites}
    rows, seen = [], set()
    q = ("SELECT * FROM stops WHERE COALESCE(kind,'stop')='stop' AND (known_site_id IN (%s) OR "
         "lower(trim(COALESCE(business_name,''))) IN (%s)) ORDER BY date DESC, arrive_utc DESC"
         % (",".join("?" * len(ids)), ",".join("?" * len(names))))
    for r in conn.execute(q, (*ids, *names)):
        if r["id"] in seen:
            continue
        if r["known_site_id"] not in ids:
            if r["known_site_id"] or r["lat"] is None or not any(
                    x["lat"] is not None and haversine_m(r["lat"], r["lon"], x["lat"], x["lon"]) <= SAME_NAME_NEAR_M
                    for x in sites):
                continue
        seen.add(r["id"])
        rows.append(r)
    return rows


def _history(conn, cfg, sites, limit):
    by_id = {x["id"]: x for x in sites}
    rows = _site_rows(conn, sites)
    visits = []
    for r in rows[:limit]:
        site = by_id.get(r["known_site_id"])
        v = _visit(conn, cfg, r, (site["kind"] if site else sites[0]["kind"]) or "")
        v["address"] = (r["address"] or (site["address"] if site else "") or "").strip()
        visits.append(v)
    work = [v for v in visits if v["status"] == "Confirmed" or v["stop_type"] == "gas"]
    return visits, {"visits": len(rows), "finished": len(work), "minutes": sum(v["minutes"] for v in work),
                    "last": visits[0]["date"] if visits else None, "first": visits[-1]["date"] if visits else None}


def place_history(conn, cfg, site_id, limit=200):
    """Every stop at one saved place, newest first, with totals."""
    site = known.get_site(conn, site_id)
    visits, totals = _history(conn, cfg, [site], limit)
    return {"place": known.site_dict(site), "visits": visits, "totals": totals}


def group_history(conn, cfg, name="", group=None, limit=200):
    """All places in a group: linked places (`group` = their link_id: a customer's old and new address) or, without a
    link, places with the same name (a vendor's branches). Every visit, newest first, each with its address."""
    if group:
        sites = conn.execute("SELECT * FROM known_sites WHERE link_id=? ORDER BY COALESCE(old_address,0), id",
                             (int(group),)).fetchall()
    else:
        sites = conn.execute("SELECT * FROM known_sites WHERE lower(trim(name))=lower(trim(?)) ORDER BY "
                             "COALESCE(old_address,0), id", (name,)).fetchall()
    if not sites:
        raise EditError("No saved place has that name.")
    visits, totals = _history(conn, cfg, sites, limit)
    return {"name": sites[0]["name"], "places": [known.site_dict(x) for x in sites], "visits": visits, "totals": totals}


def _gas_rows(conn):
    return conn.execute("SELECT * FROM stops WHERE COALESCE(kind,'stop')='stop' AND "
                        "(stop_type='gas' OR id IN (SELECT stop_id FROM fuel)) ORDER BY date DESC, arrive_utc DESC")


def gas_stations(conn, cfg):
    """Stations from your fill-ups (gas stations are not saved places): name, address, fill-ups, spend, last visit."""
    out = {}
    for r in _gas_rows(conn):
        key = (r["business_name"] or "").strip().lower()
        st = out.setdefault(key, {"name": (r["business_name"] or "").strip() or "Unnamed station",
                                  "key": key, "address": r["address"] or "", "fills": 0, "total": 0.0,
                                  "gallons": 0.0, "last": r["date"]})
        f = conn.execute("SELECT * FROM fuel WHERE stop_id=?", (r["id"],)).fetchone()
        st["fills"] += 1
        if f:
            st["total"] = round(st["total"] + (f["total"] or 0), 2)
            st["gallons"] = round(st["gallons"] + (f["gallons"] or 0), 3)
        if not st["address"] and r["address"]:
            st["address"] = r["address"]
    return sorted(out.values(), key=lambda s: s["last"], reverse=True)


def station_history(conn, cfg, key, limit=200):
    key = (key or "").strip().lower()
    rows = [r for r in _gas_rows(conn) if (r["business_name"] or "").strip().lower() == key]
    visits = [_visit(conn, cfg, r) for r in rows[:limit]]
    fills = [v["fuel"] for v in visits if v["fuel"]]
    gal = sum(f["gallons"] or 0 for f in fills)
    tot = sum(f["total"] or 0 for f in fills)
    return {"name": rows[0]["business_name"] if rows and rows[0]["business_name"] else "Unnamed station",
            "visits": visits,
            "totals": {"visits": len(rows), "total": round(tot, 2), "gallons": round(gal, 3),
                       "avg_price_per_gallon": round(tot / gal, 3) if gal else None,
                       "last": visits[0]["date"] if visits else None}}


# ---- calendar: on-site appointments next to the stops ------------------------------
CAL_SLACK_S = 30 * 60      # a stop up to 30 min before or after an appointment still matches it


def calendar_for_day(cfg, date, stops, now):
    """([events], error). Each event gets the stop it matches (most overlap, one event per stop) and a state:
    done (matched), here (matched, still on site), upcoming, missed (ended, no stop). A matched stop whose name is
    not the event's gets stop['calendar'] so the page can offer "Use calendar"."""
    if not cfg.calendar_url:
        return [], ""
    try:
        evs = calendar_feed.events_for_day(cfg, date)
    except calendar_feed.CalendarError as e:
        return [], str(e)
    except Exception as e:          # a calendar problem must never break the day page
        return [], f"Could not read the calendar ({type(e).__name__})."
    taken = set()
    out = []
    for ev in evs:
        best, best_o = None, 0
        for st in stops:
            if st["id"] in taken or st["kind"] != "stop" or st["status"] in ("Personal", "Lunch", "Traffic"):
                continue
            o = min(st["depart_ts"], ev["end_ts"] + CAL_SLACK_S) - max(st["arrive_ts"], ev["start_ts"] - CAL_SLACK_S)
            if o > best_o:
                best, best_o = st, o
        e = dict(ev, start=fmt_time(cfg, ev["start_ts"]), end=fmt_time(cfg, ev["end_ts"]),
                 stop_id=best["id"] if best else None)
        if best:
            taken.add(best["id"])
            e["state"] = "here" if best.get("here_now") else "done"
            cat = next((c for c in WORK_CATEGORIES if c.lower() == (ev.get("category") or "").strip().lower()), "")
            best["calendar"] = {"name": ev["name"], "location": ev["location"], "ticket": ev["ticket"],
                                "start": e["start"], "end": e["end"], "contact": ev.get("contact", ""),
                                "work": ev.get("work", ""), "category": cat,
                                # "Use calendar" is offered while the stop's name differs from the appointment's
                                "offer": (best["business_name"] or "").strip().lower() != ev["name"].strip().lower()}
        else:
            e["state"] = "upcoming" if ev["end_ts"] > now else "missed"
        e["minutes_to_start"] = round((ev["start_ts"] - now) / 60)
        out.append(e)
    return out, ""


# ---- gas stations: is this short stop at a fuel station? -------------------------------
FUEL_CHECK_MAX_MIN = 30     # longer stops are not fill-ups
FUEL_NEAR_M = 60            # the stop's centre must be this close to a mapped gas station
FUEL_CHECKS_PER_CALL = 4    # each check may be a map lookup: keep page loads quick, the rest are done next time


def check_fuel_stops(conn, date, nearby):
    """Look up (once per stop) whether short, untyped, unnamed stops sit on a gas station, using `nearby(lat, lon,
    radius)` (the OSM provider). Sets stops.fuel_check 1/0; the page then asks "Gas fill-up, or a quick personal
    stop?". Never sets the type itself. Returns how many stops were checked."""
    rows = conn.execute(
        "SELECT * FROM stops WHERE date=? AND COALESCE(kind,'stop')='stop' AND fuel_check IS NULL AND lat IS NOT NULL "
        "AND COALESCE(stop_type,'')='' AND status='Draft' AND known_site_id IS NULL AND COALESCE(business_name,'')='' "
        "AND depart_utc - arrive_utc <= ? ORDER BY arrive_utc", (date, FUEL_CHECK_MAX_MIN * 60)).fetchall()
    done = 0
    for r in rows[:FUEL_CHECKS_PER_CALL]:
        if (r["category"] or "") in FUEL_CATEGORIES:
            hit = True
        else:
            try:
                found = nearby(r["lat"], r["lon"], FUEL_NEAR_M)
            except Exception:
                break                   # map lookup down: try again next time
            hit = any(c.category in FUEL_CATEGORIES and haversine_m(r["lat"], r["lon"], c.lat, c.lon) <= FUEL_NEAR_M
                      for c in found)
        conn.execute("UPDATE stops SET fuel_check=? WHERE id=?", (1 if hit else 0, r["id"]))
        done += 1
    if done:
        conn.commit()
    return done
