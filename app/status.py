"""Status for a dashboard and an uptime monitor: GET /healthz (open) and GET /api/status (STATUS_TOKEN).

These two are the only addresses outside the sign-in that say anything about JobHub, so they stay narrow:
- /healthz answers ok or down, nothing else.
- /api/status gives counts and times only: no names, places, coordinates, addresses, paths or error text. It is off
  (404) until STATUS_TOKEN is set.
- Read-only: the database is opened with mode=ro (never db.connect, which creates and migrates), nothing is synced,
  and no sign-in cookie is read or sent.
- Cheap: the result is kept for CACHE_S seconds and each check gives up after CHECK_TIMEOUT_S.
"""
import logging
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from . import backup, mailer, settings
from .version import APP_VERSION

PATHS = ("/healthz", "/api/status")
CACHE_S = 15
CHECK_TIMEOUT_S = 2.0
HEALTH_DB_TIMEOUT_S = 0.15       # /healthz must answer within 200 ms, even while the database is locked
WINDOW_DAYS = 14                 # how far back unfinished stops and unsent reports are counted
METRICS = ("stops_today", "unfinished_stops", "reports_pending", "last_point_at", "last_stop_at", "last_backup_at")

log = logging.getLogger("jobhub.status")


class _Cached:
    """compute() at most once every CACHE_S; callers arriving meanwhile wait for that one result."""

    def __init__(self, compute):
        self.compute, self.lock = compute, threading.Lock()
        self.at, self.value = None, None

    def get(self):
        with self.lock:
            if self.at is None or time.monotonic() - self.at >= CACHE_S:
                self.value = self.compute()
                self.at = time.monotonic()
            return self.value


def _iso(ts):
    return None if ts is None else datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _check(status, started, detail=None):
    out = {"status": status, "latency_ms": round((time.monotonic() - started) * 1000)}
    if detail:
        out["detail"] = detail
    return out


def _open_read_only(data_dir, timeout):
    uri = Path(data_dir, "stop_journal.db").resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=timeout)
    conn.row_factory = sqlite3.Row
    return conn


def _database_metrics(conn, cfg, now):
    one = lambda sql, *args: conn.execute(sql, args).fetchone()[0]
    today = datetime.fromtimestamp(now, ZoneInfo(cfg.app_tz)).date()
    first, today = (today - timedelta(days=WINDOW_DAYS)).isoformat(), today.isoformat()
    out = {
        "stops_today": one("SELECT COUNT(*) FROM stops WHERE date=? AND kind='stop'", today),
        "unfinished_stops": one("SELECT COUNT(*) FROM stops WHERE date BETWEEN ? AND ? AND kind='stop' "
                                "AND status='Draft'", first, today),
        "reports_pending": None,                     # unknown while Email report is not set up
        "last_stop_at": _iso(one("SELECT MAX(arrive_utc) FROM stops WHERE kind='stop'")),
    }
    if mailer.enabled(cfg):
        out["reports_pending"] = one(
            "SELECT COUNT(DISTINCT s.date) FROM stops s WHERE s.date>=? AND s.date<? AND s.kind='stop' "
            "AND s.status='Confirmed' AND NOT EXISTS (SELECT 1 FROM email_log e WHERE e.ok=1 "
            "AND s.date BETWEEN e.date_from AND e.date_to)", first, today)
    return out


class Status:
    def __init__(self, base, recorder_for, started):
        """base: the env Config; recorder_for(cfg) -> a Recorder client; started: process start (epoch seconds)."""
        self.base, self.recorder_for, self.started = base, recorder_for, started
        self._health = _Cached(self._database_alive)
        self._full = _Cached(self._collect)

    # ---- /healthz -------------------------------------------------------------------------------------------------
    def healthy(self):
        return self._health.get()

    def _database_alive(self):
        try:
            conn = _open_read_only(self.base.data_dir, HEALTH_DB_TIMEOUT_S)
            try:
                conn.execute("SELECT 1 FROM stops LIMIT 1").fetchone()
            finally:
                conn.close()
            return True
        except Exception as e:
            log.debug("health check failed: %s", type(e).__name__)
            return False

    # ---- /api/status ----------------------------------------------------------------------------------------------
    def report(self):
        """(body, HTTP code)."""
        checks, metrics = self._full.get()
        states = {c["status"] for c in checks.values()}
        overall = "down" if "down" in states else "degraded" if "degraded" in states else "ok"
        body = {"app": "jobhub", "version": APP_VERSION, "status": overall, "checked_at": _iso(time.time()),
                "uptime_seconds": int(time.time() - self.started), "checks": checks, "metrics": metrics}
        return body, 503 if overall == "down" else 200

    def _collect(self):
        metrics = dict.fromkeys(METRICS)
        cfg, stored_point = self.base, None
        t0 = time.monotonic()
        try:
            conn = _open_read_only(self.base.data_dir, CHECK_TIMEOUT_S)
            try:
                cfg = settings.effective(self.base, conn)
                metrics.update(_database_metrics(conn, cfg, time.time()))
                stored_point = conn.execute("SELECT MAX(tst_utc) FROM points WHERE device=?",
                                            (cfg.recorder_device,)).fetchone()[0]
            finally:
                conn.close()
            checks = {"database": _check("ok", t0)}
        except Exception as e:                       # the type only: messages can hold paths
            log.debug("database check failed: %s", type(e).__name__)
            checks = {"database": _check("down", t0, "unreadable")}
            metrics = dict.fromkeys(METRICS)
        recorder_point = None
        if cfg.recorder_url and cfg.recorder_user and cfg.recorder_device:
            checks["recorder"], recorder_point = self._ask_recorder(cfg)
        metrics["last_point_at"] = _iso(recorder_point or stored_point)
        try:
            saved = backup.list_backups(self.base.data_dir)
            metrics["last_backup_at"] = _iso(max((b["mtime"] for b in saved), default=None))
        except OSError as e:
            log.debug("backup folder not readable: %s", type(e).__name__)
        return checks, metrics

    def _ask_recorder(self, cfg):
        """(check, newest point time or None). Runs in its own thread so the whole check ends after CHECK_TIMEOUT_S."""
        result = {}

        def ask():
            try:
                result["last"] = self.recorder_for(cfg).last_tst()
            except requests.Timeout:
                result["error"] = "timeout"
            except requests.HTTPError as e:
                code = getattr(e.response, "status_code", None)
                result["error"] = "sign-in refused" if code in (401, 403) else "error answer"
            except requests.ConnectionError:
                result["error"] = "unreachable"
            except Exception:
                result["error"] = "unexpected answer"

        t0 = time.monotonic()
        worker = threading.Thread(target=ask, daemon=True, name="jobhub-status")
        worker.start()
        worker.join(CHECK_TIMEOUT_S)
        if worker.is_alive():
            return _check("degraded", t0, "timeout"), None
        if "error" in result:
            return _check("degraded", t0, result["error"]), None
        return _check("ok", t0), result.get("last")


# ---- the server's delivery log ----------------------------------------------------------------------------------
class _QuietPolls(logging.Filter):
    """Monitors ask every minute: gunicorn's "sent" lines for these paths show only with Log detail = debug."""

    def filter(self, record):
        atoms = record.args
        if isinstance(atoms, dict) and atoms.get("U") in PATHS:
            return logging.getLogger("jobhub").isEnabledFor(logging.DEBUG)
        return True


def quiet_access_log():
    access = logging.getLogger("gunicorn.access")
    if not any(isinstance(f, _QuietPolls) for f in access.filters):
        access.addFilter(_QuietPolls())
