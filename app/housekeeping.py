"""Weekly trim of logs and caches that would otherwise grow forever (run from the alert thread).

Trimmed (only working data that is no longer used):
- place_cache older than its 30-day lifetime (never read again), route_cache older than 90 days (re-fetched if needed)
- cal_sync / cal_sync_log for days more than 30 days ago (old days' calendar events are never updated again)
- alert_state rows for one-off alerts (left_stop:*, late:*) last tried more than 30 days ago
- trips ("Go to a place") older than 30 days

Never trimmed (records the user or the agent may need): stops, parts, inventory_log, purchases, photos, email_log,
late_notices, invoice_requests, receipt_inbox, undo_log (already capped per day), notifications (capped at 100).
"""
import logging
import time
from datetime import datetime, timedelta, timezone

log = logging.getLogger("jobhub.housekeeping")
EVERY_S = 7 * 86400
KEY = "_housekeeping_ts"


def prune(conn, now=None):
    """Trim now. Returns {table: rows deleted}."""
    now = int(now or time.time())
    day = lambda n: (datetime.fromtimestamp(now, timezone.utc) - timedelta(days=n)).date().isoformat()
    jobs = [
        ("place_cache", "DELETE FROM place_cache WHERE fetched_at < ?", now - 30 * 86400),
        ("route_cache", "DELETE FROM route_cache WHERE ts < ?", now - 90 * 86400),
        ("cal_sync", "DELETE FROM cal_sync WHERE date < ?", day(30)),
        ("cal_sync_log", "DELETE FROM cal_sync_log WHERE day < ?", day(30)),
        ("alert_state", "DELETE FROM alert_state WHERE (key LIKE 'left_stop:%' OR key LIKE 'late:%') "
                        "AND COALESCE(last_attempt, last_sent, 0) < ?", now - 30 * 86400),
        ("trips", "DELETE FROM trips WHERE set_at < ?", now - 30 * 86400),
    ]
    out = {}
    for table, sql, arg in jobs:
        out[table] = conn.execute(sql, (arg,)).rowcount
    conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (KEY, str(now)))
    conn.commit()
    gone = {k: v for k, v in out.items() if v}
    log.info("weekly trim: %s", ", ".join(f"{k} {v}" for k, v in gone.items()) if gone else "nothing to remove")
    return out


def maybe_prune(conn, now=None):
    """Prune when the last run was a week ago or more. Returns the counts, or None when not due."""
    now = int(now or time.time())
    r = conn.execute("SELECT value FROM settings WHERE key=?", (KEY,)).fetchone()
    if r and r["value"].isdigit() and now - int(r["value"]) < EVERY_S:
        return None
    return prune(conn, now)
