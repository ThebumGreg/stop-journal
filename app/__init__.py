import os
from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, render_template, request

from . import db, detect, points, sample
from .config import Config
from .recorder import RecorderClient


def create_app(cfg=None, client=None):
    cfg = cfg or Config.from_env()
    if client is None:
        if os.environ.get("SAMPLE_DATA") == "1":
            client = sample.ListRecorder(
                sample.normal_day("2026-11-18") + sample.short_stop_day("2026-11-17"))
        else:
            client = RecorderClient(cfg)
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

    return app
