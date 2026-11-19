import hmac
import json
import os
import time
from dataclasses import asdict, replace
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from flask import (Flask, Response, jsonify, redirect, render_template, request,
                   send_file, session, url_for)

from . import alerts, backup, db, live, notify, parts, pdf, places, points, sample, service, settings
from . import report as reports
from .config import Config
from .places import PlacesError, UsageCounter, known, ranking
from . import recorder as recorder_mod
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
            client = RecorderClient(base, resolve=lambda: eff())
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

    def fetch_day(conn, cfg, date, refresh=False):
        if not (cfg.recorder_url and cfg.recorder_user and cfg.recorder_device):
            raise service.EditError("Set up the Recorder first: Settings → Recorder → Find on Recorder.")
        points.load_day(conn, client, cfg, date, refresh=refresh)

    def ensure_day(conn, cfg, date, refresh=False):
        if refresh or not service.is_processed(conn, date):
            fetch_day(conn, cfg, date, refresh=refresh)
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

    # ---- export (PDF + JSON report) -------------------------------------
    def build_report(conn, cfg):
        start = request.args.get("from", "")
        dates = reports.date_range(start, request.args.get("to") or start)
        return reports.build(conn, cfg, dates, lambda c, cf, d: ensure_day(c, cf, d))

    def download(body, mimetype, rep, ext):
        a, b = rep["date_from"], rep["date_to"]
        name = f"jobhub-{a}" if a == b else f"jobhub-{a}_to_{b}"
        return Response(body, mimetype=mimetype,
                        headers={"Content-Disposition": f"attachment; filename={name}.{ext}"})

    @app.get("/api/export/check")
    def export_check():
        return with_conn(lambda conn, cfg: jsonify(reports.check(build_report(conn, cfg))))

    @app.get("/api/export.json")
    def export_json():
        def go(conn, cfg):
            rep = build_report(conn, cfg)
            return download(json.dumps(rep, indent=2, ensure_ascii=False), "application/json", rep, "json")
        return with_conn(go)

    @app.get("/api/export.pdf")
    def export_pdf():
        def go(conn, cfg):
            rep = build_report(conn, cfg)
            return download(pdf.render(rep), "application/pdf", rep, "pdf")
        return with_conn(go)

    @app.post("/api/day/<date>/sync")
    def day_sync(date):
        """Fetch new points from the Recorder and add stops detected since last time."""
        def go(conn, cfg):
            check_date(date)
            ensure_day(conn, cfg, date)
            fetch_day(conn, cfg, date, refresh=True)
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

    def run_suggest(lat, lon, minutes, qtext, force_google):
        """Ranked business candidates around a point; optional free-text query.

        Cascade: known sites always; then OSM (modes osm, osm_google); Google only
        in mode google, when forced, or in osm_google mode when no known site is
        nearby and OSM found fewer than google_fallback_min business-like places."""
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
        ranked = ranking.rank(cands + external, lat, lon, minutes,
                              search_radius_m=radius if not qtext else TEXT_SEARCH_FALLOFF_M)
        return {"candidates": [asdict(c) for c in ranked[:25]],
                "error": " ".join(errors) or None, "sources": sources,
                "google_used": "google" in sources,
                "google_available": bool(cfg.google_places_key) or isinstance(provider, dict)}

    @app.get("/api/suggest")
    def suggest():
        try:
            lat, lon = float(request.args["lat"]), float(request.args["lon"])
        except (KeyError, ValueError):
            return jsonify(error="lat and lon are required"), 400
        return jsonify(run_suggest(lat, lon, request.args.get("minutes", 0, type=int),
                                   request.args.get("q", "").strip(),
                                   request.args.get("google") == "1"))

    # ---- places: home / office / vendors / clients ----------------------
    def current_position(conn):
        """Phone's latest position: Recorder first, else the newest cached point."""
        try:
            p = client.last_point()
            if p:
                return p["lat"], p["lon"]
        except Exception:
            pass
        r = conn.execute("SELECT lat, lon FROM points ORDER BY tst_utc DESC LIMIT 1").fetchone()
        if r:
            return r["lat"], r["lon"]
        raise service.EditError("No location available yet. Open OwnTracks on your phone and try again.")

    @app.get("/places")
    def places_page():
        return render_template("places.html")

    @app.get("/api/places")
    def places_list():
        return catalog(lambda conn: {"places": known.list_sites(
            conn, request.args.get("kind", ""), request.args.get("q", ""))})

    @app.post("/api/places")
    def places_create():
        b = request.get_json(silent=True) or {}
        return catalog(lambda conn: known.site_dict(known.get_site(conn, known.create_site(conn, b))))

    def reverse_address(lat, lon):
        """Best-effort street address for a coordinate (OSM); "" if unavailable."""
        try:
            prov = provider.get("osm") if isinstance(provider, dict) else provider
            if prov is None or not hasattr(prov, "reverse"):
                prov = places.get_provider(eff(), "osm")
            return prov.reverse(lat, lon)
        except Exception:
            return ""

    @app.get("/api/live")
    def live_status():
        """Where the phone last reported and how long it has been there."""
        cfg = eff()
        if not (cfg.recorder_url and cfg.recorder_user and cfg.recorder_device):
            return jsonify(error="Set up the Recorder first: Settings → Recorder."), 400
        now = int(time.time())
        try:
            pts = client.fetch(datetime.fromtimestamp(now - live.LOOKBACK_S, timezone.utc),
                               datetime.fromtimestamp(now, timezone.utc))
            if not pts:
                lp = client.last_point()
                pts = [lp] if lp else []
        except Exception as e:
            return jsonify(error=f"Could not reach the Recorder ({type(e).__name__})."), 502
        st = live.status(pts, now, cfg.stop_radius_m, cfg.max_acc_m)
        if st is None:
            return jsonify(error="No location data yet. Open OwnTracks on your phone."), 404
        conn = db.connect(base.data_dir)
        try:
            site = known.match_any(conn, st["lat"], st["lon"])
        finally:
            conn.close()
        st["place"] = site["name"] if site else ""
        st["kind"] = (site["kind"] if site else "") or ""
        st["address"] = ""
        if not site and not st["moving"]:
            st["address"] = reverse_address(st["lat"], st["lon"])
        st["now"] = now
        st["tz"] = cfg.app_tz
        return jsonify(st)

    @app.post("/api/places/from-last")
    def places_from_last():
        """Save wherever the phone is right now (handy when standing at the office)."""
        b = request.get_json(silent=True) or {}

        def go(conn):
            lat, lon = current_position(conn)
            kind = b.get("kind") or "vendor"
            name = str(b.get("name") or "").strip() or known.KIND_LABEL.get(kind, "Place")
            address = str(b.get("address") or "").strip() or reverse_address(lat, lon)
            sid = known.create_site(conn, {"name": name, "address": address,
                                           "lat": lat, "lon": lon, "kind": kind})
            return known.site_dict(known.get_site(conn, sid))
        return catalog(go)

    @app.get("/api/places/search")
    def places_search():
        """Address / business search for adding a place, biased to where the phone is."""
        q = request.args.get("q", "").strip()
        if len(q) < 3:
            return jsonify(error="Type at least 3 letters."), 400

        def go(conn):
            try:
                lat, lon = current_position(conn)
            except service.EditError:
                lat, lon = 39.5, -98.35
            r = run_suggest(lat, lon, 0, q, request.args.get("google") == "1")
            r["candidates"] = [c for c in r["candidates"] if c["source"] != "known"]
            return r
        return catalog(go)

    @app.patch("/api/places/<int:sid>")
    def places_patch(sid):
        b = request.get_json(silent=True) or {}

        def go(conn):
            known.update_site(conn, sid, b)
            return known.site_dict(known.get_site(conn, sid))
        return catalog(go)

    @app.delete("/api/places/<int:sid>")
    def places_delete(sid):
        return catalog(lambda conn: known.delete_site(conn, sid) or {"ok": True})

    @app.post("/api/stops/<int:sid>/save-place")
    def stop_save_place(sid):
        b = request.get_json(silent=True) or {}

        def go(conn, cfg):
            s = conn.execute("SELECT address, lat, lon FROM stops WHERE id=?", (sid,)).fetchone()
            if s and not (s["address"] or "").strip() and s["lat"] is not None:
                b["address"] = reverse_address(s["lat"], s["lon"])
            return service.save_place(conn, cfg, sid, b)
        return mutate(go)

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
                    backups=backup.list_backups(base.data_dir),
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

    @app.post("/settings/recorder/discover")
    def settings_recorder_discover():
        """List the users/devices the Recorder has, so they can be picked instead of typed."""
        cfg = eff()
        url = (request.form.get("recorder_url") or cfg.recorder_url).strip().rstrip("/")
        if not url.startswith(("http://", "https://")):
            return jsonify(ok=False, message="Enter the Recorder address first (http://host:8083).")
        try:
            found = recorder_mod.discover(url, cfg.recorder_auth)
        except Exception as e:
            return jsonify(ok=False, message=f"Could not reach the Recorder ({type(e).__name__}).")
        if not found:
            return jsonify(ok=False, message="Connected, but the Recorder has no users yet.")
        for u in found:
            for d in u["devices"]:
                d["last"] = (datetime.fromtimestamp(d["last_tst"], tz).strftime("%b %-d, %-I:%M %p")
                             if d["last_tst"] else "no data")
        return jsonify(ok=True, users=found)

    @app.post("/settings/backup-now")
    def settings_backup_now():
        cfg = eff()
        try:
            name = backup.snapshot(cfg.data_dir, cfg.app_tz, cfg.backup_keep or 14, stamp_time=True)
            return jsonify(ok=True, message=f"Saved {name}. Reload to see it in the list.")
        except Exception as e:
            return jsonify(ok=False, message=f"Backup failed ({type(e).__name__}).")

    @app.get("/settings/backup/<name>")
    def settings_backup_download(name):
        if not backup.NAME.match(name):
            return jsonify(error="not found"), 404
        path = os.path.join(backup.folder(base.data_dir), name)
        if not os.path.isfile(path):
            return jsonify(error="not found"), 404
        return send_file(path, as_attachment=True, download_name=name)

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
        backup.start(base, eff)

    return app
