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
