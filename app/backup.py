"""Daily SQLite snapshots. The database file is the whole state of the app.

Snapshots use SQLite's online backup API, so they are consistent while the app is
running. They live in <data_dir>/backups/ and the newest `keep` are retained.
Copy that folder off the server too: a backup on the same disk does not survive a disk failure.
"""
import logging
import os
import re
import sqlite3
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

log = logging.getLogger("jobhub.backup")
NAME = re.compile(r"^jobhub-\d{4}-\d{2}-\d{2}(-\d{6})?\.db$")


def folder(data_dir):
    return os.path.join(data_dir, "backups")


def list_backups(data_dir):
    """Newest first: [{name, size, mtime}]."""
    d = folder(data_dir)
    if not os.path.isdir(d):
        return []
    out = []
    for n in os.listdir(d):
        if NAME.match(n):
            st = os.stat(os.path.join(d, n))
            out.append({"name": n, "size": st.st_size, "mtime": st.st_mtime})
    return sorted(out, key=lambda b: b["name"], reverse=True)


def snapshot(data_dir, tz_name, keep, stamp_time=False, now=None):
    """Write one snapshot and prune old ones. Returns the file name."""
    now = now or datetime.now(ZoneInfo(tz_name))
    name = "jobhub-" + now.strftime("%Y-%m-%d-%H%M%S" if stamp_time else "%Y-%m-%d") + ".db"
    os.makedirs(folder(data_dir), exist_ok=True)
    final = os.path.join(folder(data_dir), name)
    tmp = final + ".part"
    src = sqlite3.connect(os.path.join(data_dir, "stop_journal.db"))
    dst = sqlite3.connect(tmp)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    os.replace(tmp, final)
    prune(data_dir, keep)
    return name


def prune(data_dir, keep):
    for b in list_backups(data_dir)[max(keep, 1):]:
        os.remove(os.path.join(folder(data_dir), b["name"]))


def due(data_dir, tz_name, now=None):
    """True when no snapshot exists yet for today's local date."""
    today = (now or datetime.now(ZoneInfo(tz_name))).strftime("%Y-%m-%d")
    return not any(b["name"].startswith(f"jobhub-{today}") for b in list_backups(data_dir))


def run_once(base_cfg, resolve, now=None):
    cfg = resolve()
    if cfg.backup_keep > 0 and due(cfg.data_dir, cfg.app_tz, now):
        return snapshot(cfg.data_dir, cfg.app_tz, cfg.backup_keep, now=now)
    return None


def start(base_cfg, resolve, interval=3600):
    def loop():
        while True:
            try:
                run_once(base_cfg, resolve)
            except Exception as e:  # keep the thread alive; log the type only
                log.warning("backup failed: %s", type(e).__name__)
            time.sleep(interval)

    threading.Thread(target=loop, daemon=True, name="jobhub-backup").start()
