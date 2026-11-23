"""AI note polishing: only the notes are sent; a suggestion comes back; errors become plain messages."""
from dataclasses import replace
from types import SimpleNamespace as NS

import anthropic
import pytest

from app import ai, db
from tests.test_detect import CFG
from tests.test_export import make


class FakeMessages:
    def __init__(self, reply="Replaced the valve and tested it at 60 psi.", stop="end_turn", boom=None):
        self.reply, self.stop, self.boom, self.calls = reply, stop, boom, []

    def create(self, **kw):
        self.calls.append(kw)
        if self.boom:
            raise self.boom
        return NS(stop_reason=self.stop, content=[NS(type="thinking", thinking=""), NS(type="text", text=self.reply)])


def fake(**kw):
    m = FakeMessages(**kw)
    return NS(beta=NS(messages=m)), m


def test_polish_sends_only_the_notes(tmp_path):
    cfg = replace(CFG, data_dir=str(tmp_path), anthropic_key="sk-test")
    conn = db.connect(str(tmp_path))
    client, m = fake()
    out = ai.polish(conn, cfg, "  replacd valve tested 60psi  ", client)
    assert out == "Replaced the valve and tested it at 60 psi."
    kw = m.calls[0]
    assert kw["model"] == "claude-opus-5-5" and kw["fallbacks"] == "default"
    assert kw["messages"] == [{"role": "user", "content": "<notes>\nreplacd valve tested 60psi\n</notes>"}]
    used = conn.execute("SELECT SUM(count) FROM api_usage WHERE provider='ai'").fetchone()[0]
    assert used == 1


def test_polish_errors_and_limits(tmp_path):
    cfg = replace(CFG, data_dir=str(tmp_path), anthropic_key="sk-test", ai_monthly_cap=2)
    conn = db.connect(str(tmp_path))
    with pytest.raises(ai.EditError, match="Write some notes"):
        ai.polish(conn, cfg, "   ", fake()[0])
    with pytest.raises(ai.EditError, match="declined"):
        ai.polish(conn, cfg, "x", fake(stop="refusal")[0])
    assert ai.polish(conn, cfg, "x", fake()[0])                   # the 2nd counted request (empty notes are not sent)
    with pytest.raises(ai.EditError, match="Monthly AI limit"):
        ai.polish(conn, cfg, "x", fake()[0])                      # the cap (2) is reached
    conn.execute("DELETE FROM api_usage")
    conn.commit()
    with pytest.raises(ai.EditError, match="Could not reach"):
        ai.polish(conn, cfg, "x", fake(boom=anthropic.APIConnectionError(request=None))[0])
    with pytest.raises(ai.EditError, match="off"):
        ai.polish(conn, replace(cfg, anthropic_key=""), "x", fake()[0])


def test_status_and_route(tmp_path, monkeypatch):
    c = make(tmp_path)
    assert c.get("/api/ai/status").json == {"notes": False}
    assert c.post("/api/notes/polish", json={"text": "hi"}).status_code == 400
    client, _ = fake(reply="Hi.")
    monkeypatch.setattr(ai, "_client", lambda cfg: client)
    c2 = make(tmp_path / "b", anthropic_key="sk-test")
    assert c2.get("/api/ai/status").json == {"notes": True}
    assert c2.post("/api/notes/polish", json={"text": "hi"}).json == {"text": "Hi."}
