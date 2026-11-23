"""Lunch is paid (default) or unpaid (Settings → General → Pay rules)."""
import pytest

from app import db
from tests.test_paid_invariant import DAYS, check, run_day


@pytest.mark.parametrize("paid", ["on", "off"])
def test_lunch_paid_setting(tmp_path, paid):
    conn = db.connect(str(tmp_path))
    conn.execute("INSERT INTO settings(key,value) VALUES ('lunch_paid',?)", (paid,))
    conn.commit(); conn.close()
    day = run_day(tmp_path, DAYS["lunch after fuel"])
    sm = day["summary"]
    check(day)                                                   # paid_minutes = paid timesheet rows either way
    lunch_rows = [r for r in day["timesheet"] if r["type"] == "lunch"]
    assert lunch_rows and all(r["paid"] == (paid == "on") for r in lunch_rows)
    assert sm["lunch_paid"] == (paid == "on") and sm["lunch_minutes"] == sum(r["minutes"] for r in lunch_rows)
    assert sm["paid_minutes"] == sm["on_site_minutes"] + sm["drive_minutes"] + (sm["lunch_minutes"] if paid == "on" else 0)
