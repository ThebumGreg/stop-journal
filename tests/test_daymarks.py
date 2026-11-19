import time
from dataclasses import replace

from app import alerts, create_app, db, sample, service
from tests.test_detect import CFG
from tests.test_parts import D, make, part

TODAY = time.strftime("%Y-%m-%d")


def test_marks_set_edit_clear_and_validate(tmp_path):
    c, _ = make(tmp_path)
    r = c.post(f"/api/day/{D}/mark", json={"which": "start", "time": "07:05"})
    assert r.get_json()["marks"]["start"] == "07:05"
    assert c.post(f"/api/day/{D}/mark", json={"which": "end", "time": "06:00"}).status_code == 400
    assert c.post(f"/api/day/{D}/mark", json={"which": "start"}).status_code == 400  # not today: needs a time
    c.post(f"/api/day/{D}/mark", json={"which": "end", "time": "16:40"})
    d = c.get(f"/api/day/{D}").get_json()["marks"]
    assert (d["start"], d["end"]) == ("07:05", "16:40")
    c.post(f"/api/day/{D}/mark", json={"which": "end", "clear": True})
    assert c.get(f"/api/day/{D}").get_json()["marks"]["end"] == ""


def test_marks_reach_report_json_and_pdf(tmp_path):
    c, _ = make(tmp_path)
    c.post(f"/api/day/{D}/mark", json={"which": "start", "time": "07:05"})
    c.post(f"/api/day/{D}/mark", json={"which": "end", "time": "16:40"})
    j = c.get(f"/api/export.json?from={D}").get_json()
    assert j["days"][0]["day_start"] == "07:05" and j["days"][0]["day_end"] == "16:40"
    assert c.get(f"/api/export.pdf?from={D}").status_code == 200


def alert_env(tmp_path, **kw):
    cfg = replace(CFG, data_dir=str(tmp_path), pushover_token="t", pushover_user="u",
                  work_days="Mon,Tue,Wed,Thu,Fri,Sat,Sun", work_start="07:00", work_end="17:00",
                  alert_no_data_min=30, **kw)
    return cfg


class Client:
    def __init__(self, last):
        self.last = last

    def last_tst(self):
        return self.last


def at(hhmm):
    h, m = hhmm.split(":")
    t = time.strptime(f"{TODAY} {h}:{m}", "%Y-%m-%d %H:%M")
    from zoneinfo import ZoneInfo
    from datetime import datetime
    return int(datetime(t.tm_year, t.tm_mon, t.tm_mday, int(h), int(m), tzinfo=ZoneInfo(CFG.app_tz)).timestamp())


def test_end_day_silences_no_data_and_start_day_extends_window(tmp_path):
    cfg = alert_env(tmp_path)
    conn = db.connect(cfg.data_dir)
    sent = []
    send = lambda *a: sent.append(a[1])  # noqa: E731
    now = at("19:00")
    alerts.run_once(cfg, Client(now - 7200), now=now, send=send)       # outside hours: quiet
    assert not [s for s in sent if "no location" in s]
    conn.execute("INSERT INTO day_marks(date,start_utc) VALUES (?,?)", (TODAY, at("18:00")))
    conn.commit()
    alerts.run_once(cfg, Client(now - 7200), now=now, send=send)       # started 18:00, silent since
    assert [s for s in sent if "no location" in s]
    conn.execute("UPDATE day_marks SET end_utc=?", (at("18:30"),))
    conn.commit()
    sent.clear()
    conn.execute("DELETE FROM alert_state")
    conn.commit()
    alerts.run_once(cfg, Client(now - 7200), now=now, send=send)       # day ended: quiet
    assert not [s for s in sent if "no location" in s]


def test_draft_review_and_low_stock_alerts(tmp_path):
    cfg = alert_env(tmp_path)
    conn = db.connect(cfg.data_dir)
    conn.execute("INSERT INTO stops(date,arrive_utc,depart_utc,status,kind) VALUES (?,?,?,'Draft','stop')",
                 (TODAY, at("09:00"), at("10:00")))
    conn.execute("INSERT INTO parts(name,tracked,active,on_hand,reorder_at) VALUES ('Valve',1,1,2,5)")
    conn.commit()
    sent = []
    send = lambda *a: sent.append((a[1], a[2]))  # noqa: E731
    alerts.run_once(cfg, Client(at("16:00")), now=at("16:00"), send=send)
    assert [t for t, _ in sent] == ["JobHub – truck stock low"]         # drafts not due yet
    assert "Valve" not in sent[0][1]
    alerts.run_once(cfg, Client(at("17:40")), now=at("17:40"), send=send)
    assert [t for t, _ in sent].count("JobHub – review today's stops") == 1
    alerts.run_once(cfg, Client(at("17:45")), now=at("17:45"), send=send)
    assert len(sent) == 2                                                # no repeats
