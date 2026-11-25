from dataclasses import replace
from unittest import mock

from app import create_app, grammar, sample
from tests.test_detect import CFG


def client(tmp_path, url=""):
    cfg = replace(CFG, data_dir=str(tmp_path), languagetool_url=url)
    return create_app(cfg, client=sample.ListRecorder([])).test_client()


def test_off_by_default(tmp_path):
    c = client(tmp_path)
    assert c.get("/api/grammar/status").json["enabled"] is False
    assert c.post("/api/grammar", json={"text": "me and him was there"}).json == {"enabled": False, "issues": []}


def test_forwards_only_the_text_and_trims_the_answer(tmp_path):
    c = client(tmp_path, "http://lt.test:8010")
    reply = mock.Mock(status_code=200)
    reply.json.return_value = {"matches": [{"offset": 0, "length": 2, "message": "Use I", "rule": {"id": "X"},
                                            "replacements": [{"value": "I"}, {"value": "We"}]}]}
    with mock.patch("app.grammar.requests.post", return_value=reply) as post:
        r = c.post("/api/grammar", json={"text": "me went there", "stop": 5}).json
    assert r["enabled"] and r["issues"] == [{"offset": 0, "length": 2, "message": "Use I",
                                             "replacements": ["I", "We"], "rule": "X"}]
    assert post.call_args.args[0] == "http://lt.test:8010/v2/check"
    assert post.call_args.kwargs["data"] == {"text": "me went there", "language": "en-US"}


def test_unreachable_server_is_a_clean_error(tmp_path):
    c = client(tmp_path, "http://lt.test:8010")
    with mock.patch("app.grammar.requests.post", side_effect=OSError("down")):
        r = c.post("/api/grammar", json={"text": "hello"})
    assert r.status_code == 502 and "grammar server" in r.json["error"]


def test_blank_text_makes_no_call():
    with mock.patch("app.grammar.requests.post") as post:
        assert grammar.check(replace(CFG, languagetool_url="http://x"), "  ") == []
    post.assert_not_called()


def test_settings_test_button(tmp_path):
    c = client(tmp_path)
    assert b"Test grammar server" in c.get("/settings").data
    assert c.post("/settings/test-grammar", data={"languagetool_url": ""}).json["ok"] is False
    good = mock.Mock(status_code=200)
    good.json.return_value = {"matches": [{"offset": 5, "length": 3}]}
    with mock.patch("app.grammar.requests.post", return_value=good) as post:
        r = c.post("/settings/test-grammar", data={"languagetool_url": "http://lt.test:8010/"}).json
    assert r["ok"] and post.call_args.args[0] == "http://lt.test:8010/v2/check"
    assert "notes" not in post.call_args.kwargs["data"]["text"].lower()
    good.json.return_value = {"matches": []}
    with mock.patch("app.grammar.requests.post", return_value=good):
        assert c.post("/settings/test-grammar", data={"languagetool_url": "http://x"}).json["ok"] is False
    with mock.patch("app.grammar.requests.post", side_effect=OSError):
        assert "Could not reach" in c.post("/settings/test-grammar", data={"languagetool_url": "http://x"}).json["message"]


def test_settings_page_my_and_app_groups(tmp_path):
    import re
    from app import settings
    c = client(tmp_path)
    html = c.get("/settings").get_data(as_text=True)
    assert html.index("My settings") < html.index("App settings")
    groups = re.findall(r'<details class="grp" id="([a-z0-9-]+)"', html)
    assert groups[:6] == ["me", "work-hours", "my-phone-owntracks", "my-calendar", "my-alerts", "my-notes"]
    assert "work-ai-agent" in groups and "log-and-backups" in groups and len(groups) == 19
    # every setting is on the page exactly once, and each is either the person's or the app's
    laid = [k for _, _, gs in settings.LAYOUT for _, ks in gs for k in ks]
    assert sorted(laid) == sorted(settings.FIELDS) and len(laid) == len(set(laid))
    for k in settings.FIELDS:
        assert html.count(f'name="{k}"') == 1, k
    assert {"tech_name", "tech_email", "work_days", "recorder_device", "calendar_url", "alert_late", "pushover_user",
            "note_phrases", "notify_channel"} <= settings.USER_KEYS
    assert not {"mail_to", "smtp_password", "anthropic_key", "recorder_url", "lunch_paid", "app_url",
                "pushover_token", "stop_radius_m"} & settings.USER_KEYS
    assert 'class="adv"' in html and "Show advanced" in html
    bad = c.post("/settings", data={"max_acc_m": "abc"}).get_data(as_text=True)
    assert "●" in bad and '<details class="grp" id="stop-detection" open>' in bad
    assert '<details class="adv" open>' in bad                         # the bad field is an advanced one: shown
    assert settings.split_help("Your Recorder, e.g. http://x. Use it.") == ("Your Recorder, e.g. http://x.", "Use it.")
