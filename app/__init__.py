import hmac
import os
from dataclasses import asdict, replace
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import (Flask, jsonify, redirect, render_template, request, session,
                   url_for)

from . import db, detect, places, points, sample, settings
from .config import Config
from .places import PlacesError, UsageCounter, known, ranking
from .recorder import RecorderClient


TEXT_SEARCH_FALLOFF_M = 50000  # free-text hits: nearer-to-stop still ranks first


def create_app(cfg=None, client=None, provider=None):
    base = cfg or Config.from_env()
    if client is None:
        if os.environ.get("SAMPLE_DATA") == "1":
            client = sample.ListRecorder(
                sample.normal_day("2026-11-18") + sample.short_stop_day("2026-11-17"))
        else:
            client = RecorderClient(base)
    app = Flask(__name__)
    tz = ZoneInfo(base.app_tz)
    app.config.update(SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_HTTPONLY=True)

    def eff():
        """Env defaults with Settings-page overrides applied (read per request)."""
        conn = db.connect(base.data_dir)
        try:
            return settings.effective(base, conn)
        finally:
            conn.close()

    conn0 = db.connect(base.data_dir)
    app.secret_key = settings.secret_key(conn0)
    conn0.close()

    @app.before_request
    def gate():
        if not base.app_password or request.endpoint in ("health", "login", "logout"):
            return None
        if session.get("auth") == 1:
            return None
        if request.path.startswith("/api/") or request.method != "GET":
            return jsonify(error="login required"), 401
        return redirect(url_for("login", next=request.path))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        err = None
        if request.method == "POST":
            if hmac.compare_digest(request.form.get("password", "").encode(),
                                   base.app_password.encode()):
                session["auth"] = 1
                nxt = request.args.get("next", "/")
                return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else "/")
            err = "Wrong password."
        return render_template("login.html", error=err), (401 if err else 200)

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    def iso(t):
        return datetime.fromtimestamp(t, tz).isoformat(timespec="seconds")

    @app.get("/health")
    def health():
        return jsonify(ok=True)

    @app.get("/")
    def index():
        today = datetime.now(tz).date().isoformat()
        return render_template("day.html", date=today, tz=base.app_tz)

    @app.get("/day/<date>")
    def day_page(date):
        datetime.fromisoformat(date)  # 400s on junk via ValueError -> 500; fine for v1
        return render_template("day.html", date=date, tz=base.app_tz)

    @app.get("/api/day/<date>")
    def day_api(date):
        q = request.args
        cfg = eff()
        run_cfg = replace(
            cfg,
            stop_min_minutes=q.get("min", cfg.stop_min_minutes, type=int),
            stop_radius_m=q.get("radius", cfg.stop_radius_m, type=int),
            stop_max_gap_min=q.get("gap", cfg.stop_max_gap_min, type=int))
        conn = db.connect(base.data_dir)
        try:
            raw = points.load_day(conn, client, base, date,
                                  refresh=q.get("refresh") == "1")
        finally:
            conn.close()
        kept = points.filter_accuracy(raw, run_cfg.max_acc_m)
        items, drives = detect.detect(kept, run_cfg)
        return jsonify(
            date=date, tz=base.app_tz,
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
        cfg = eff()
        radius = cfg.places_radius_m
        conn = db.connect(base.data_dir)
        try:
            cands = known.nearby(conn, lat, lon, radius)
            if qtext:
                cands += known.search(conn, qtext)
        finally:
            conn.close()
        error = None
        try:
            prov = provider or places.get_provider(cfg)
            cands += (prov.search_text(qtext, lat, lon) if qtext
                      else prov.search_nearby(lat, lon, radius))
        except PlacesError as e:  # message is safe to show; known sites still returned
            error = str(e)
        except Exception as e:
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
        conn = db.connect(base.data_dir)
        try:
            sid = known.learn(conn, name, str(b.get("address", "")), lat, lon,
                              str(b.get("category", "")))
        finally:
            conn.close()
        return jsonify(id=sid)

    # ---- settings page ------------------------------------------------
    def settings_context(**extra):
        conn = db.connect(base.data_dir)
        try:
            ov = settings.overrides(conn)
        finally:
            conn.close()
        cfg = eff()
        values = {k: ov.get(k, "") for k in settings.FIELDS}
        defaults = {k: getattr(base, k) for k in settings.FIELDS}
        return dict(spec=settings.SPEC, values=values, defaults=defaults,
                    key_hint=settings.key_hint(cfg.google_places_key),
                    key_from_env=bool(base.google_places_key) and not ov.get(settings.SECRET),
                    usage=UsageCounter(base.data_dir, "google").count(),
                    cap=cfg.google_daily_cap, provider=cfg.places_provider,
                    has_password=bool(base.app_password), **extra)

    @app.get("/settings")
    def settings_page():
        return render_template("settings.html", errors={}, saved=request.args.get("saved"),
                               **settings_context())

    @app.post("/settings")
    def settings_save():
        conn = db.connect(base.data_dir)
        try:
            errors = settings.save(conn, request.form)
        finally:
            conn.close()
        if errors:
            ctx = settings_context()
            ctx["values"] = {k: request.form.get(k, "") for k in settings.FIELDS}
            return render_template("settings.html", errors=errors, saved=None, **ctx), 400
        return redirect(url_for("settings_page", saved=1))

    @app.post("/settings/test-google")
    def settings_test_google():
        """One real Google request (counts toward the daily cap) to validate the key."""
        cfg = replace(eff(), places_provider="google")
        try:
            found = places.get_provider(cfg).search_text("post office", 39.5, -98.35)
            return jsonify(ok=True, message=f"Google key works ({len(found)} results).")
        except PlacesError as e:
            return jsonify(ok=False, message=str(e))
        except Exception as e:
            return jsonify(ok=False, message=f"Test failed ({type(e).__name__}).")

    return app
