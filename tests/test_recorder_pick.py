from app import create_app, recorder
from tests.test_settings import app_for


def fake_get(responses):
    def _get(url, auth_str, path, params=None, timeout=10):
        key = (path, (params or {}).get("user"), (params or {}).get("device"))
        v = responses[key]
        if isinstance(v, Exception):
            raise v
        return v
    return _get


def test_discover_lists_users_devices_and_last_seen(monkeypatch):
    monkeypatch.setattr(recorder, "_get", fake_get({
        ("/api/0/list", None, None): {"results": ["user", "greg01"]},
        ("/api/0/list", "user", None): {"results": []},
        ("/api/0/list", "greg01", None): {"results": ["phone"]},
        ("/api/0/last", "greg01", "phone"): [{"tst": 100}, {"tst": 300}],
    }))
    out = recorder.discover("http://rec:8083")
    assert out == [{"user": "user", "devices": []},
                   {"user": "greg01", "devices": [{"device": "phone", "last_tst": 300}]}]


def test_discover_endpoint_and_errors(tmp_path, monkeypatch):
    app = app_for(tmp_path)
    c = app.test_client()
    d = c.post("/settings/recorder/discover", data={"recorder_url": "nonsense"}).get_json()
    assert not d["ok"]
    monkeypatch.setattr(recorder, "discover", lambda url, auth="": [
        {"user": "u", "devices": [{"device": "d", "last_tst": 1700000000}]}])
    d = c.post("/settings/recorder/discover", data={"recorder_url": "http://rec:8083"}).get_json()
    assert d["ok"] and d["users"][0]["devices"][0]["last"] != "no data"

    def boom(url, auth=""):
        raise OSError("down")
    monkeypatch.setattr(recorder, "discover", boom)
    d = c.post("/settings/recorder/discover", data={"recorder_url": "http://rec:8083"}).get_json()
    assert not d["ok"] and "OSError" in d["message"]


def test_saved_recorder_settings_reach_the_client(tmp_path):
    cfg_client = recorder.RecorderClient(object(), resolve=lambda: "live")
    assert cfg_client.cfg == "live"
    app = app_for(tmp_path)
    c = app.test_client()
    r = c.post("/settings", data={"recorder_url": "http://x:8083", "recorder_user": "u1",
                                  "recorder_device": "d1"})
    assert r.status_code == 302
    page = c.get("/settings").get_data(as_text=True)
    assert 'value="u1"' in page and "Find on Recorder" in page


def test_unconfigured_recorder_gives_a_clear_message(tmp_path):
    c = app_for(tmp_path, recorder_url="", recorder_user="", recorder_device="").test_client()
    r = c.get("/api/day/2026-11-18")
    assert r.status_code == 400 and "Settings" in r.get_json()["error"]


def test_points_are_keyed_by_the_saved_device(tmp_path):
    from dataclasses import replace
    from tests.test_detect import CFG
    from app import sample
    cfg = replace(CFG, data_dir=str(tmp_path), recorder_url="", recorder_user="", recorder_device="")
    app = create_app(cfg, client=sample.ListRecorder(sample.normal_day("2026-11-18")))
    c = app.test_client()
    c.post("/settings", data={"recorder_url": "http://rec:8083", "recorder_user": "u",
                              "recorder_device": "phone"})
    d = c.get("/api/day/2026-11-18").get_json()
    assert d["stops"], d
