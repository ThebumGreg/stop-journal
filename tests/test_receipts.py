"""Receipt reading: Google Vision text -> suggested numbers. Nothing is filled in without a tap."""
import pytest

from app import receipts
from tests.test_export import D, make
from tests.test_photos import jpeg_with_gps, upload

GAS = """CHEVRON
1234 ANYWHERE RD
11/18/2026 10:42
PUMP # 06
REGULAR
GALLONS 12.504
PRICE/GAL $4.099
FUEL SALE $51.25
TOTAL $51.25
VISA ****1234"""

STORE = """THE HOME DEPOT
SUBTOTAL 79.80
SALES TAX 6.60
TOTAL $86.40
CASH 100.00
CHANGE 13.60"""


def test_parse_gas_and_store():
    g = receipts.parse(GAS)
    assert (g["total"], g["gallons"], g["price_per_gallon"], g["checks"]) == (51.25, 12.504, 4.099, True)
    s = receipts.parse(STORE)
    assert s["total"] == 86.40 and s["gallons"] is None and not s["checks"]
    assert receipts.parse("12.50 G @ 3.999\nTOTAL 49.99")["gallons"] == 12.5
    assert receipts.parse("")["total"] is None


class Fake:
    def __init__(self, text="", status=200, body=None):
        self.text, self.status, self.body, self.calls = text, status, body, []

    def __call__(self, url, params=None, json=None, timeout=None):
        self.calls.append((url, params, json))
        fake = self

        class R:
            status_code = fake.status
            def json(self):
                return fake.body if fake.body is not None else {"responses": [{"fullTextAnnotation": {"text": fake.text}}]}
        return R()


@pytest.fixture
def vision(monkeypatch):
    f = Fake(GAS)
    monkeypatch.setattr(receipts.requests, "post", f)
    return f


def test_gas_receipt_suggests_fuel_and_counts_usage(tmp_path, vision):
    c = make(tmp_path, receipt_reader="google_vision", google_places_key="gkey")
    sid = c.get(f"/api/day/{D}").json["stops"][0]["id"]
    c.patch(f"/api/stops/{sid}", json={"stop_type": "gas"})
    r = upload(c, sid, jpeg_with_gps(80, 60))
    ph = next(s for s in r.json["stops"] if s["id"] == sid)["photos"][0]
    assert ph["read"]["ok"] and ph["read"]["total"] == 51.25 and ph["read"]["gallons"] == 12.504
    url, params, body = vision.calls[0]
    assert params == {"key": "gkey"} and body["requests"][0]["features"][0]["type"] == "DOCUMENT_TEXT_DETECTION"
    s = next(x for x in r.json["stops"] if x["id"] == sid)
    assert s["fuel"] is None                                   # suggestion only
    upload(c, sid, jpeg_with_gps(80, 60), "photo")             # plain photos are never sent
    assert len(vision.calls) == 1
    d = c.post(f"/api/photos/{ph['id']}/dismiss-read").json
    assert next(x for x in d["stops"] if x["id"] == sid)["photos"][0]["read"]["dismissed"]


def test_reader_off_errors_and_cap(tmp_path, vision):
    c = make(tmp_path)                                          # off by default: nothing is sent
    sid = c.get(f"/api/day/{D}").json["stops"][0]["id"]
    r = upload(c, sid, jpeg_with_gps(80, 60))
    assert next(s for s in r.json["stops"] if s["id"] == sid)["photos"][0]["read"] is None and not vision.calls
    c2 = make(tmp_path / "b", receipt_reader="google_vision", google_places_key="k", receipt_monthly_cap=1)
    sid = c2.get(f"/api/day/{D}").json["stops"][0]["id"]
    vision.status, vision.body = 403, {"error": {"message": "API not enabled"}}
    p = next(s for s in upload(c2, sid, jpeg_with_gps(80, 60)).json["stops"] if s["id"] == sid)["photos"][0]
    assert not p["read"]["ok"] and "Cloud Vision API" in p["read"]["error"]
    p2 = next(s for s in upload(c2, sid, jpeg_with_gps(80, 60)).json["stops"] if s["id"] == sid)["photos"][1]
    assert "limit reached" in p2["read"]["error"] and len(vision.calls) == 1


# ---- Claude reader -------------------------------------------------------------------
import io
import json as _json
from types import SimpleNamespace as NS


class FakeClaude:
    def __init__(self, data):
        self.data, self.calls = data, []
        self.beta = NS(messages=self)

    def create(self, **kw):
        self.calls.append(kw)
        return NS(stop_reason="end_turn", content=[NS(type="text", text=_json.dumps(self.data))])


STORE_DATA = {"store": "Home Depot", "date": "2026-11-18", "total": 86.4, "tax": 6.6, "gallons": None,
              "price_per_gallon": None, "is_fuel": False,
              "items": [{"description": "1/2 in. ball valve", "sku": "BV-050", "qty": 2, "unit_price": 8.75, "amount": 17.5},
                        {"description": "PTFE tape", "sku": None, "qty": None, "unit_price": None, "amount": 2.1}]}


def test_claude_reads_store_receipt_with_items(tmp_path, monkeypatch):
    from app import ai
    fake = FakeClaude(STORE_DATA)
    monkeypatch.setattr(ai, "_client", lambda cfg: fake)
    c = make(tmp_path, receipt_reader="claude", anthropic_key="sk-test")
    sid = c.get(f"/api/day/{D}").json["stops"][0]["id"]
    r = upload(c, sid, jpeg_with_gps(80, 60))
    read = next(s for s in r.json["stops"] if s["id"] == sid)["photos"][0]["read"]
    assert read["ok"] and read["reader"] == "claude" and read["store"] == "Home Depot" and read["date"] == "2026-11-18"
    assert read["total"] == 86.4 and read["tax"] == 6.6 and read["gallons"] is None
    assert read["items"][0] == {"description": "1/2 in. ball valve", "sku": "BV-050", "qty": 2, "unit_price": 8.75, "amount": 17.5}
    assert read["items"][1]["unit_price"] == 2.1 and read["items"][1]["qty"] == 1
    kw = fake.calls[0]
    assert kw["model"] == "claude-opus-5-5" and kw["output_config"]["format"]["type"] == "json_schema"
    img = kw["messages"][0]["content"][0]
    assert img["type"] == "image" and img["source"]["media_type"] == "image/jpeg"


def test_claude_fuel_receipt_and_tool_receipt(tmp_path, monkeypatch):
    from app import ai
    fake = FakeClaude({"store": "Chevron", "date": "2026-11-18", "total": 51.25, "tax": None, "gallons": 12.504,
                       "price_per_gallon": 4.099, "is_fuel": True, "items": []})
    monkeypatch.setattr(ai, "_client", lambda cfg: fake)
    c = make(tmp_path, receipt_reader="claude", anthropic_key="sk-test")
    sid = c.get(f"/api/day/{D}").json["stops"][0]["id"]
    c.patch(f"/api/stops/{sid}", json={"stop_type": "gas"})
    read = next(s for s in upload(c, sid, jpeg_with_gps(80, 60)).json["stops"] if s["id"] == sid)["photos"][0]["read"]
    assert read["checks"] and read["store"] == "Chevron"
    # a tool's receipt: read once, the suggestion is kept on the file
    fake.data = {"store": "Home Depot", "date": "2026-03-14", "total": 179.0, "tax": None, "gallons": None,
                 "price_per_gallon": None, "is_fuel": False, "items": [{"description": "DeWalt drill", "sku": None, "qty": 1,
                                                                       "unit_price": 179.0, "amount": 179.0}]}
    t = c.post("/api/tools", json={"name": "Cordless drill"}).json
    r = c.post(f"/api/tools/{t['id']}/files", data={"kind": "receipt", "file": (io.BytesIO(jpeg_with_gps(80, 60)), "r.jpg")},
               content_type="multipart/form-data").json
    f = r["files"][0]
    assert f["read"]["ok"] and f["read"]["total"] == 179.0 and f["read"]["date"] == "2026-03-14"
    d = c.post(f"/api/tool-files/{f['id']}/dismiss-read").json
    assert d["files"][0]["read"]["dismissed"]
    c.post(f"/api/tools/{t['id']}/files", data={"kind": "photo", "file": (io.BytesIO(jpeg_with_gps(80, 60)), "p.jpg")},
           content_type="multipart/form-data")
    assert len(fake.calls) == 2                                    # photos are never sent


def test_claude_reader_needs_a_key(tmp_path):
    c = make(tmp_path, receipt_reader="claude")
    sid = c.get(f"/api/day/{D}").json["stops"][0]["id"]
    read = next(s for s in upload(c, sid, jpeg_with_gps(80, 60)).json["stops"] if s["id"] == sid)["photos"][0]["read"]
    assert not read["ok"] and "Settings → AI" in read["error"]
