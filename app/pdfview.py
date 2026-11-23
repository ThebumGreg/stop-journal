"""The report PDF as a page of pictures, for the in-app viewer on iPhone.

iOS draws only the first page of a PDF inside a Home Screen app (and does not fit it to the screen), so the viewer asks
for this instead: each page rendered to a PNG with pdfium (`pypdfium2`), stacked in one plain HTML page that scrolls and
fits the width. Nothing is stored; the PDF itself is unchanged and still what Share / download give you.
"""
import base64
import html
import io

MAX_PAGES = 60
SCALE = 1.6          # 72 dpi x 1.6 -> letter pages ~980 px wide: sharp on a phone, ~100-250 KB per page


class ViewError(Exception):
    pass


def page_images(pdf_bytes, scale=SCALE, max_pages=MAX_PAGES):
    """PDF bytes -> list of PNG bytes, one per page (at most max_pages)."""
    try:
        import pypdfium2 as pdfium
    except ImportError:
        raise ViewError("The page viewer needs the pypdfium2 package: rebuild the Docker image.")
    doc = pdfium.PdfDocument(pdf_bytes)
    try:
        out = []
        for i in range(min(len(doc), max_pages)):
            page = doc[i]
            try:
                img = page.render(scale=scale).to_pil()
            finally:
                page.close()
            buf = io.BytesIO()
            img.convert("RGB").save(buf, "PNG", optimize=False)
            out.append(buf.getvalue())
        return out, len(doc)
    finally:
        doc.close()


def html_page(pdf_bytes, title):
    imgs, total = page_images(pdf_bytes)
    t = html.escape(title)
    parts = [f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,"
             f"initial-scale=1'><title>{t}</title><style>body{{margin:0;background:#8a8f98;font:14px -apple-system,"
             f"sans-serif}}img{{display:block;width:calc(100% - 16px);max-width:900px;margin:8px auto;background:#fff;"
             f"box-shadow:0 1px 4px rgba(0,0,0,.35)}}p{{text-align:center;color:#fff;margin:6px 0 14px}}</style></head><body>"]
    for n, png in enumerate(imgs, 1):
        parts.append(f"<img alt='Page {n} of {total}' src='data:image/png;base64,{base64.b64encode(png).decode()}'>"
                     f"<p>Page {n} of {total}</p>")
    if total > len(imgs):
        parts.append(f"<p>Showing the first {len(imgs)} of {total} pages. Share or download the PDF for the rest.</p>")
    parts.append("</body></html>")
    return "".join(parts)
