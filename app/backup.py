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


def full_zip(data_dir, tz_name):
    """Database (a consistent copy) plus every photo and receipt, as one zip in an already-unlinked temp file.
    Returns (open file positioned at 0, download name). Nothing is left behind on the server."""
    import tempfile
    import zipfile
    stamp = datetime.now(ZoneInfo(tz_name)).strftime("%Y-%m-%d-%H%M")
    fd, path = tempfile.mkstemp(prefix=".full-", suffix=".zip", dir=data_dir)
    dbtmp = path + ".db"
    try:
        src, dst = sqlite3.connect(os.path.join(data_dir, "stop_journal.db")), sqlite3.connect(dbtmp)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        with os.fdopen(fd, "w+b") as raw, zipfile.ZipFile(raw, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(dbtmp, "stop_journal.db")
            pics = os.path.join(data_dir, "photos")
            for root, _dirs, files in os.walk(pics):
                for n in sorted(files):
                    if n.endswith(".jpg"):
                        full = os.path.join(root, n)
                        z.write(full, os.path.relpath(full, data_dir), compress_type=zipfile.ZIP_STORED)
        f = open(path, "rb")
        return f, f"jobhub-full-{stamp}.zip"
    finally:
        for p in (path, dbtmp):
            try:
                os.remove(p)
            except OSError:
                pass


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
        out = snapshot(cfg.data_dir, cfg.app_tz, cfg.backup_keep, now=now)
        log.info("daily database backup saved (keeping %d)", cfg.backup_keep)
        return out
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
