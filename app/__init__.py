import hmac
import json
import os
from dataclasses import asdict, replace
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import (Flask, Response, jsonify, redirect, render_template, request,
                   session, url_for)

from . import alerts, db, notify, parts, places, points, sample, service, settings
from .config import Config
from .places import PlacesError, UsageCounter, known, ranking
from .recorder import RecorderClient


TEXT_SEARCH_FALLOFF_M = 50000  # free-text hits: nearer-to-stop still ranks first


def create_app(cfg=None, client=None, provider=None, start_background=None):
    base = cfg or Config.from_env()
    client_given = client is not None
    if client is None:
        if os.environ.get("SAMPLE_DATA") == "1":
            client = sample.ListRecorder(
                sample.normal_day("2026-11-18") + sample.short_stop_day("2026-11-17"))
        else:
            client = RecorderClient(base)
    app = Flask(__name__)
    tz = ZoneInfo(base.app_tz)
    app.jinja_env.filters["ts_local"] = lambda t: datetime.fromtimestamp(t, tz).strftime("%b %-d, %-I:%M %p")
    app.config.update(SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_HTTPONLY=True,
                      MAX_CONTENT_LENGTH=4 * 1024 * 1024)

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
        if not base.app_password or request.endpoint in ("health", "login", "logout", "static", "manifest"):
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

    # ---- the editable day ---------------------------------------------
    def with_conn(fn, *a, **kw):
        """Run fn(conn, cfg, ...) with effective settings; EditError -> 400."""
        conn = db.connect(base.data_dir)
        try:
            return fn(conn, settings.effective(base, conn), *a, **kw)
        except service.EditError as e:
            return jsonify(error=str(e)), 400
        finally:
            conn.close()

    def check_date(date):
        try:
            datetime.fromisoformat(date)
        except ValueError:
            raise service.EditError("Dates look like 2026-11-18.")

    def ensure_day(conn, cfg, date, refresh=False):
        if refresh or not service.is_processed(conn, date):
            points.load_day(conn, client, base, date, refresh=refresh)
        if not service.is_processed(conn, date):
            service.materialize(conn, cfg, date, service.kept_points(conn, cfg, date), cfg)

    def payload(conn, cfg, date, path=False):
        return jsonify(service.day_payload(conn, cfg, date, include_path=path))

    @app.get("/api/day/<date>")
    def day_api(date):
        def go(conn, cfg):
            check_date(date)
            ensure_day(conn, cfg, date)
            return payload(conn, cfg, date, path=True)
        return with_conn(go)

    @app.post("/api/day/<date>/sync")
    def day_sync(date):
        """Fetch new points from the Recorder and add stops detected since last time."""
        def go(conn, cfg):
            check_date(date)
            ensure_day(conn, cfg, date)
            points.load_day(conn, client, base, date, refresh=True)
            n = service.sync_new(conn, cfg, date, service.kept_points(conn, cfg, date), cfg)
            return jsonify({**service.day_payload(conn, cfg, date, include_path=True),
                            "added": n})
        return with_conn(go)

    @app.post("/api/day/<date>/redetect")
    def day_redetect(date):
        """Discard detected stops and their edits; detect again with new thresholds."""
        b = request.get_json(silent=True) or {}

        def num(k, d):
            try:
                return int(b.get(k, d))
            except (TypeError, ValueError):
                raise service.EditError("Thresholds must be whole numbers.")

        def go(conn, cfg):
            check_date(date)
            ensure_day(conn, cfg, date)
            run = replace(cfg, stop_min_minutes=num("min", cfg.stop_min_minutes),
                          stop_radius_m=num("radius", cfg.stop_radius_m),
                          stop_max_gap_min=num("gap", cfg.stop_max_gap_min))
            service.redetect(conn, cfg, date, service.kept_points(conn, run, date), run)
            return payload(conn, cfg, date, path=True)
        return with_conn(go)

    @app.post("/api/day/<date>/stops")
    def stop_add(date):
        def go(conn, cfg):
            check_date(date)
            ensure_day(conn, cfg, date)
            service.add_manual(conn, cfg, date, request.get_json(silent=True) or {})
            return payload(conn, cfg, date)
        return with_conn(go)

    def mutate(fn, *args):
        def go(conn, cfg):
            date = fn(conn, cfg, *args)
            return payload(conn, cfg, date)
        return with_conn(go)

    @app.patch("/api/stops/<int:sid>")
    def stop_patch(sid):
        return mutate(service.patch_stop, sid, request.get_json(silent=True) or {})

    @app.delete("/api/stops/<int:sid>")
    def stop_delete(sid):
        return mutate(service.delete_stop, sid)

    @app.post("/api/stops/<int:sid>/split")
    def stop_split(sid):
        return mutate(service.split_stop, sid, (request.get_json(silent=True) or {}).get("at", ""))

    @app.post("/api/stops/merge")
    def stop_merge():
        ids = (request.get_json(silent=True) or {}).get("ids") or []
        if len(ids) != 2:
            return jsonify(error="Pick two stops to merge."), 400
        return mutate(service.merge_stops, int(ids[0]), int(ids[1]))

    @app.patch("/api/drives/<int:did>")
    def drive_patch(did):
        return mutate(service.patch_drive, did, request.get_json(silent=True) or {})

    # ---- parts used at a stop -----------------------------------------
    @app.post("/api/stops/<int:sid>/parts")
    def stop_part_add(sid):
        return mutate(service.add_stop_part, sid, request.get_json(silent=True) or {})

    @app.patch("/api/stop-parts/<int:spid>")
    def stop_part_patch(spid):
        return mutate(service.patch_stop_part, spid, request.get_json(silent=True) or {})

    @app.delete("/api/stop-parts/<int:spid>")
    def stop_part_delete(spid):
        return mutate(service.remove_stop_part, spid)

    # ---- catalog / truck inventory ------------------------------------
    def catalog(fn):
        """Run fn(conn) -> JSON-able; EditError -> 400."""
        return with_conn(lambda conn, cfg: jsonify(fn(conn)))

    @app.get("/truck")
    def truck_page():
        return render_template("truck.html")

    @app.get("/api/parts")
    def parts_list():
        q, everything = request.args.get("q", ""), request.args.get("all") == "1"
        return catalog(lambda conn: {"parts": parts.list_parts(conn, q, everything)})

    @app.post("/api/parts")
    def parts_create():
        b = request.get_json(silent=True) or {}

        def go(conn):
            pid = parts.create_part(conn, b)
            conn.commit()
            return parts.part_dict(parts.get_part(conn, pid))
        return catalog(go)

    @app.patch("/api/parts/<int:pid>")
    def parts_patch(pid):
        b = request.get_json(silent=True) or {}

        def go(conn):
            parts.update_part(conn, pid, b)
            return parts.part_dict(parts.get_part(conn, pid))
        return catalog(go)

    @app.post("/api/parts/<int:pid>/adjust")
    def parts_adjust(pid):
        b = request.get_json(silent=True) or {}

        def go(conn):
            parts.adjust(conn, pid, b.get("delta"), b.get("set"), b.get("note", ""))
            return parts.part_dict(parts.get_part(conn, pid))
        return catalog(go)

    @app.post("/api/parts/import")
    def parts_import():
        f = request.files.get("file")
        raw = f.read() if f else request.get_data()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")  # Excel "CSV" exports are often not UTF-8
        return catalog(lambda conn: parts.import_csv(conn, text))

    def csv_response(body, name):
        return Response(body, mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename={name}"})

    @app.get("/api/parts/export.csv")
    def parts_export():
        conn = db.connect(base.data_dir)
        try:
            return csv_response(parts.export_csv(conn), "parts.csv")
        finally:
            conn.close()

    @app.get("/api/parts/template.csv")
    def parts_template():
        return csv_response(parts.template_csv(), "parts-template.csv")

    @app.get("/api/suggest")
    def suggest():
        """Ranked business candidates for a stop; optional free-text query q.

        Cascade: known sites always; then OSM (modes osm, osm_google); Google only
        in mode google, when forced with ?google=1, or in osm_google mode when no
        known site is nearby and OSM found fewer than google_fallback_min
        business-like places."""
        try:
            lat, lon = float(request.args["lat"]), float(request.args["lon"])
        except (KeyError, ValueError):
            return jsonify(error="lat and lon are required"), 400
        minutes = request.args.get("minutes", 0, type=int)
        qtext = request.args.get("q", "").strip()
        force_google = request.args.get("google") == "1"
        cfg = eff()
        radius = cfg.places_radius_m
        conn = db.connect(base.data_dir)
        try:
            cands = known.nearby(conn, lat, lon, radius)
            known_near = len(cands)
            if qtext:
                cands += known.search(conn, qtext)
        finally:
            conn.close()
        errors, sources = [], ["your sites"]

        def attempt(name):
            try:
                if isinstance(provider, dict):
                    prov = provider[name]
                else:
                    prov = provider or places.get_provider(cfg, name)
                found = (prov.search_text(qtext, lat, lon) if qtext
                         else prov.search_nearby(lat, lon, radius))
                sources.append(name)
                return found
            except PlacesError as e:  # message is safe to show
                errors.append(str(e))
            except Exception as e:
                errors.append(f"{name} lookup failed ({type(e).__name__})")
            return []

        mode = cfg.places_provider
        external = attempt("osm") if mode in ("osm", "osm_google") else []
        if mode == "google":
            want_google = True
        else:
            thin = (len(external) if qtext else
                    sum(1 for c in external if ranking.is_business(c.category)))
            want_google = force_google or (
                mode == "osm_google" and not known_near and thin < cfg.google_fallback_min)
        if want_google:
            external += attempt("google")
        cands += external
        ranked = ranking.rank(cands, lat, lon, minutes,
                              search_radius_m=radius if not qtext else TEXT_SEARCH_FALLOFF_M)
        return jsonify(candidates=[asdict(c) for c in ranked[:25]],
                       error=" ".join(errors) or None, sources=sources,
                       google_used="google" in sources,
                       google_available=bool(cfg.google_places_key) or isinstance(provider, dict))

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
            recent = notify.recent(conn)
        finally:
            conn.close()
        cfg = eff()
        values = {k: ov.get(k, "") for k, f in settings.FIELDS.items() if f["kind"] != "secret"}
        defaults = {k: getattr(base, k) for k, f in settings.FIELDS.items() if f["kind"] != "secret"}
        secret_info = {k: {"hint": settings.key_hint(getattr(cfg, k)),
                           "from_env": bool(getattr(base, k)) and not ov.get(k)}
                       for k in settings.SECRETS}
        return dict(spec=settings.SPEC, values=values, defaults=defaults, secret_info=secret_info,
                    usage=UsageCounter(base.data_dir, "google").count(),
                    cap=cfg.google_daily_cap, provider=cfg.places_provider,
                    pushover_ready=notify.configured(cfg), recent=recent,
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
            ctx["values"] = {k: request.form.get(k, "") for k, f in settings.FIELDS.items()
                             if f["kind"] != "secret"}
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

    @app.post("/settings/test-pushover")
    def settings_test_pushover():
        """Send one real test notification to validate the Pushover keys."""
        cfg = eff()
        try:
            notify.send(cfg, "JobHub", "Test notification – Pushover is working.", 0,
                        cfg.app_url or None)
            ok, msg = True, "Sent. Check your phone."
        except notify.NotifyError as e:
            ok, msg = False, str(e)
        conn = db.connect(base.data_dir)
        try:
            notify.record(conn, "JobHub test", "Test notification", ok, None if ok else msg)
        finally:
            conn.close()
        return jsonify(ok=ok, message=msg)

    @app.get("/manifest.webmanifest")
    def manifest():
        icon = lambda n: url_for("static", filename=f"icon-{n}.png")  # noqa: E731
        return Response(json.dumps({
            "name": "JobHub", "short_name": "JobHub", "start_url": "/", "scope": "/",
            "display": "standalone", "background_color": "#ffffff", "theme_color": "#0b66d0",
            "icons": [{"src": icon(192), "sizes": "192x192", "type": "image/png"},
                      {"src": icon(512), "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}],
        }), mimetype="application/manifest+json")

    if start_background is None:  # real deployments only: not tests, not sample mode
        start_background = (not client_given and os.environ.get("SAMPLE_DATA") != "1"
                            and os.environ.get("DISABLE_ALERTS") != "1")
    if start_background:
        alerts.start(base, client)

    return app
