import os
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from app import backup, db
from app.config import Config
from tests.test_settings import app_for

TZ = "America/Los_Angeles"


def day(d):
    return datetime(2026, 11, d, 9, 0, tzinfo=ZoneInfo(TZ))


def seeded(tmp_path):
    conn = db.connect(str(tmp_path))
    conn.execute("INSERT INTO settings(key,value) VALUES ('marker','yes')")
    conn.commit()
    conn.close()


def test_snapshot_is_readable_copy_and_prunes(tmp_path):
    seeded(tmp_path)
    for d in range(1, 6):
        backup.snapshot(str(tmp_path), TZ, 3, now=day(d))
    names = [b["name"] for b in backup.list_backups(str(tmp_path))]
    assert names == ["jobhub-2026-11-05.db", "jobhub-2026-11-04.db", "jobhub-2026-11-03.db"]
    c = sqlite3.connect(os.path.join(str(tmp_path), "backups", names[0]))
    assert c.execute("SELECT value FROM settings WHERE key='marker'").fetchone()[0] == "yes"


def test_due_once_per_day(tmp_path):
    seeded(tmp_path)
    assert backup.due(str(tmp_path), TZ, day(5))
    backup.snapshot(str(tmp_path), TZ, 3, now=day(5))
    assert not backup.due(str(tmp_path), TZ, day(5))
    assert backup.due(str(tmp_path), TZ, day(6))


def test_run_once_respects_off(tmp_path):
    seeded(tmp_path)
    cfg = Config.from_env()
    from dataclasses import replace
    off = replace(cfg, data_dir=str(tmp_path), backup_keep=0, app_tz=TZ)
    assert backup.run_once(off, lambda: off, now=day(5)) is None
    on = replace(off, backup_keep=2)
    assert backup.run_once(on, lambda: on, now=day(5)).endswith("2026-11-05.db")


def test_endpoints(tmp_path):
    c = app_for(tmp_path).test_client()
    assert c.post("/settings/backup-now").get_json()["ok"]
    name = backup.list_backups(str(tmp_path))[0]["name"]
    r = c.get(f"/settings/backup/{name}")
    assert r.status_code == 200 and r.data[:6] == b"SQLite"
    assert c.get("/settings/backup/..%2Fstop_journal.db").status_code == 404
    assert c.get("/settings/backup/jobhub-2020-01-01.db").status_code == 404
    assert name in c.get("/settings").get_data(as_text=True)
