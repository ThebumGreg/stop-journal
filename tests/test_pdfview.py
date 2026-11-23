import base64
import re

import pytest

from tests.test_commute import D, make

pytest.importorskip("pypdfium2")


def test_view_shows_every_pdf_page_as_a_picture(tmp_path):
    from app import pdfview
    c = make(tmp_path)
    pdf = c.get(f"/api/export.pdf?from={D}").data
    imgs, total = pdfview.page_images(pdf)
    assert total >= 1 and len(imgs) == total and all(i.startswith(b"\x89PNG") for i in imgs)
    r = c.get(f"/api/export.view?from={D}")
    assert r.status_code == 200 and r.mimetype == "text/html"
    body = r.get_data(as_text=True)
    pngs = re.findall(r"data:image/png;base64,([A-Za-z0-9+/=]+)", body)
    assert len(pngs) == total and base64.b64decode(pngs[0]).startswith(b"\x89PNG")
    assert f"Page {total} of {total}" in body


def test_view_caps_pages(tmp_path):
    from app import pdfview
    c = make(tmp_path)
    pdf = c.get(f"/api/export.pdf?from={D}").data
    imgs, total = pdfview.page_images(pdf, max_pages=1)
    assert len(imgs) == 1 and total >= 1
