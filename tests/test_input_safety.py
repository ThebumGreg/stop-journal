"""Outside text and files: pictures, CSV files, page scripts (from the 2026-11-28 security review)."""
import io
import pathlib
import re
from dataclasses import replace

import pytest
from PIL import Image

from app import create_app, db, photos
from app.config import Config
from app.errors import EditError

CFG = Config.from_env()
APP = pathlib.Path(__file__).resolve().parent.parent / "app"


def client(tmp_path):
    return create_app(replace(CFG, data_dir=str(tmp_path), app_password=""), client=object()).test_client()


def picture(fmt, size=(32, 32), mode="RGB"):
    buf = io.BytesIO()
    Image.new(mode, size).save(buf, fmt)
    return buf.getvalue()


@pytest.mark.parametrize("fmt", ["JPEG", "PNG", "WEBP", "GIF"])
def test_ordinary_pictures_are_read(fmt):
    data, w, h = photos.normalize(picture(fmt))
    assert data[:2] == b"\xff\xd8" and (w, h) == (32, 32)


@pytest.mark.parametrize("fmt", ["BMP", "TIFF", "PPM", "ICO"])
def test_other_formats_are_not_decoded(fmt):
    with pytest.raises(EditError, match="not a picture"):
        photos.normalize(picture(fmt))


@pytest.mark.parametrize("side", [8000, 12000, 14000])
def test_huge_pictures_are_refused(side):
    with pytest.raises(EditError, match="too many pixels|not a picture"):
        photos.normalize(picture("PNG", (side, side), "1"))


def test_a_48_megapixel_picture_fits():
    assert photos.normalize(picture("PNG", (8064, 6048), "1"))[1] == photos.MAX_SIDE


@pytest.mark.parametrize("url", ["/api/parts/import", "/api/tools/import", "/api/places/import"])
def test_unreadable_csv_is_explained(tmp_path, url):
    body = ("name,place_id,hubspot_company_id\n\"" + "a" * 200000).encode()
    r = client(tmp_path).post(url, data={"file": (io.BytesIO(body), "x.csv")}, content_type="multipart/form-data")
    assert r.status_code == 400 and "CSV" in r.json["error"]


def test_places_export_cannot_start_a_formula(tmp_path):
    c = client(tmp_path)
    c.post("/api/places", json={"name": "=HYPERLINK(\"http://evil.example\")", "address": "@SUM(A1)", "lat": 34.0,
                                "lon": -118.0})
    text = c.get("/api/places/export.csv").get_data(as_text=True)
    assert "'=HYPERLINK" in text and "'@SUM" in text


def test_pages_have_no_inline_script():
    """The security policy blocks it, so a new one would silently not run."""
    for page in (APP / "templates").glob("*.html"):
        text = page.read_text()
        assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", text), page.name
        assert not re.search(r"<[^>]+\son[a-z]+\s*=|javascript:", text), page.name


def test_outside_scripts_are_pinned():
    for page in (APP / "templates").glob("*.html"):
        for tag in re.findall(r"<(?:script|link)\b[^>]*https://[^>]*>", page.read_text()):
            assert "integrity=" in tag, (page.name, tag)


def test_popups_never_take_text_as_html():
    for script in (APP / "static").glob("*.js"):
        text = script.read_text()
        assert not re.search(r"bind(Popup|Tooltip)\((?!el\()", text), script.name
