"""Weekly trim: old logs and caches go, records stay."""
import time

from app import db, housekeeping

DAY = 86400


def test_prune_old_working_data_only(tmp_path):
    conn = db.connect(str(tmp_path))
    now = int(time.time())
    old, new = now - 100 * DAY, now - DAY
    conn.execute("INSERT INTO place_cache(key,response_json,fetched_at) VALUES ('a','[]',?),('b','[]',?)", (old, new))
    conn.execute("INSERT INTO route_cache(key,meters,ts) VALUES ('a',1,?),('b',1,?)", (old, new))
    conn.execute("INSERT INTO cal_sync_log(day,ts,stop_id,method) VALUES ('2020-01-01',?,1,'REQUEST'),(date('now'),?,2,'REQUEST')", (old, new))
    conn.execute("INSERT INTO cal_sync(stop_id,date,uid) VALUES (1,'2020-01-01','u1'),(2,date('now'),'u2')")
    conn.execute("INSERT INTO alert_state(key,active,last_attempt) VALUES ('left_stop:1',1,?),('late:x',1,?),"
                 "('no_data',1,?),('left_stop:2',1,?)", (old, old, old, new))
    conn.execute("INSERT INTO trips(site_id,date,set_at) VALUES (1,'2020-01-01',?),(1,date('now'),?)", (old, new))
    conn.execute("INSERT INTO email_log(ts,date_from,date_to,ok) VALUES (?,'2020-01-01','2020-01-01',1)", (old,))
    conn.commit()
    out = housekeeping.maybe_prune(conn, now)
    assert out == {"place_cache": 1, "route_cache": 1, "cal_sync": 1, "cal_sync_log": 1, "alert_state": 2, "trips": 1}
    keys = {r[0] for r in conn.execute("SELECT key FROM alert_state")}
    assert keys == {"no_data", "left_stop:2"}                               # standing alerts and recent ones stay
    assert conn.execute("SELECT COUNT(*) FROM email_log").fetchone()[0] == 1   # records are never trimmed
    assert housekeeping.maybe_prune(conn, now + DAY) is None                 # weekly, not every minute
    assert housekeeping.maybe_prune(conn, now + 8 * DAY) is not None
