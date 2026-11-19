import os
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, render_template, request

from . import db, detect, places, points, sample
from .places import known, ranking
from .config import Config
from .recorder import RecorderClient


TEXT_SEARCH_FALLOFF_M = 50000  # free-text hits: nearer-to-stop still ranks first


def create_app(cfg=None, client=None, provider=None):
    cfg = cfg or Config.from_env()
    if client is None:
        if os.environ.get("SAMPLE_DATA") == "1":
            client = sample.ListRecorder(
                sample.normal_day("2026-11-18") + sample.short_stop_day("2026-11-17"))
        else:
            client = RecorderClient(cfg)
    provider = provider or places.get_provider(cfg)
    app = Flask(__name__)
    tz = ZoneInfo(cfg.app_tz)

    def iso(t):
        return datetime.fromtimestamp(t, tz).isoformat(timespec="seconds")

    @app.get("/health")
    def health():
        return jsonify(ok=True)

    @app.get("/")
    def index():
        today = datetime.now(tz).date().isoformat()
        return render_template("day.html", date=today, tz=cfg.app_tz)

    @app.get("/day/<date>")
    def day_page(date):
        datetime.fromisoformat(date)  # 400s on junk via ValueError -> 500; fine for v1
        return render_template("day.html", date=date, tz=cfg.app_tz)

    @app.get("/api/day/<date>")
    def day_api(date):
        q = request.args
        run_cfg = replace(
            cfg,
            stop_min_minutes=q.get("min", cfg.stop_min_minutes, type=int),
            stop_radius_m=q.get("radius", cfg.stop_radius_m, type=int),
            stop_max_gap_min=q.get("gap", cfg.stop_max_gap_min, type=int))
        conn = db.connect(cfg.data_dir)
        try:
            raw = points.load_day(conn, client, cfg, date,
                                  refresh=q.get("refresh") == "1")
        finally:
            conn.close()
        kept = points.filter_accuracy(raw, run_cfg.max_acc_m)
        items, drives = detect.detect(kept, run_cfg)
        return jsonify(
            date=date, tz=cfg.app_tz,
            params={"min": run_cfg.stop_min_minutes, "radius": run_cfg.stop_radius_m,
                    "gap": run_cfg.stop_max_gap_min},
            raw_count=len(raw), kept_count=len(kept),
            path=[[p["lat"], p["lon"]] for p in kept],
            stops=[{**{k: i[k] for k in ("index", "kind", "lat", "lon")},
                    "arrive": iso(i["arrive"]), "depart": iso(i["depart"]),
                    "minutes": round((i["depart"] - i["arrive"]) / 60)}
                   for i in items],
            drives=[{"from": d["from"], "to": d["to"],
                     "depart": iso(d["depart"]), "arrive": iso(d["arrive"]),
                     "minutes": round((d["arrive"] - d["depart"]) / 60),
                     "miles_approx": round(d["distance_m"] / 1609.344, 1)}
                    for d in drives],
            totals=detect.totals(items, drives))

    @app.get("/api/suggest")
    def suggest():
        """Ranked business candidates for a stop; optional free-text query q."""
        try:
            lat, lon = float(request.args["lat"]), float(request.args["lon"])
        except (KeyError, ValueError):
            return jsonify(error="lat and lon are required"), 400
        minutes = request.args.get("minutes", 0, type=int)
        qtext = request.args.get("q", "").strip()
        radius = cfg.places_radius_m
        conn = db.connect(cfg.data_dir)
        try:
            cands = known.nearby(conn, lat, lon, radius)
            if qtext:
                cands += known.search(conn, qtext)
        finally:
            conn.close()
        error = None
        try:
            cands += (provider.search_text(qtext, lat, lon) if qtext
                      else provider.search_nearby(lat, lon, radius))
        except Exception as e:  # network/provider failure: still return known sites
            error = f"Place lookup failed ({type(e).__name__})"
        ranked = ranking.rank(cands, lat, lon, minutes,
                              search_radius_m=radius if not qtext else TEXT_SEARCH_FALLOFF_M)
        return jsonify(candidates=[asdict(c) for c in ranked[:25]], error=error)

    @app.post("/api/known-sites")
    def teach():
        """Remember a confirmed business (called when a stop is confirmed)."""
        b = request.get_json(silent=True) or {}
        try:
            name = str(b["name"]).strip()
            lat, lon = float(b["lat"]), float(b["lon"])
        except (KeyError, ValueError, TypeError):
            return jsonify(error="name, lat, lon are required"), 400
        if not name:
            return jsonify(error="name is required"), 400
        conn = db.connect(cfg.data_dir)
        try:
            sid = known.learn(conn, name, str(b.get("address", "")), lat, lon,
                              str(b.get("category", "")))
        finally:
            conn.close()
        return jsonify(id=sid)

    return app
