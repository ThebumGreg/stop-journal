"""Photos and receipts attached to a stop.

Stored as files in the data folder by the stop's day: `photos/YYYY/MM/DD/<id>-<kind>.jpg` (the database keeps the
path in `photos.path`). The database backups do NOT contain them, so copy `/data/photos/` along with `/data/backups/`.
Photos are kept when their stop is deleted, so Undo brings them back. Every upload is re-encoded on the server: turned upright, shrunk to MAX_SIDE, saved as
JPEG with no metadata (EXIF carries the GPS position and the phone model, which must not travel with a report).
The phone also shrinks the picture before sending it, so uploads stay quick on a weak signal.
"""
import io
import json
import os
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from PIL import Image, ImageOps, UnidentifiedImageError

from .errors import EditError

MAX_SIDE = 1600          # px, longest side
QUALITY = 80
MAX_UPLOAD = 4 * 1024 * 1024   # matches the app's MAX_CONTENT_LENGTH
FORMATS = ("JPEG", "PNG", "WEBP", "GIF")   # nothing else is decoded: less code exposed to a bad file
MAX_PIXELS = 60_000_000        # a small file can unpack into gigabytes; 48 MP phone pictures fit
KINDS = {"receipt": "Receipt", "photo": "Photo"}
PER_STOP = 12

def normalize(raw):
    """Any image Pillow can read -> (jpeg bytes, width, height), upright, shrunk, metadata dropped."""
    if not raw:
        raise EditError("No picture was sent.")
    if len(raw) > MAX_UPLOAD:
        raise EditError("That picture is too large (4 MB at most).")
    try:
        im = Image.open(io.BytesIO(raw), formats=FORMATS)      # reads the header only
        if im.width * im.height > MAX_PIXELS:
            raise EditError("That picture has too many pixels (60 megapixels at most).")
        im = ImageOps.exif_transpose(im)
        im.thumbnail((MAX_SIDE, MAX_SIDE))
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
    except EditError:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError):
        raise EditError("That file is not a picture this app can read (use JPEG or PNG).")
    out = io.BytesIO()
    im.save(out, "JPEG", quality=QUALITY, optimize=True)     # no exif= argument: metadata is not written
    return out.getvalue(), im.width, im.height


EXIF_DATETIME_ORIGINAL, EXIF_IFD, EXIF_DATETIME = 0x9003, 0x8769, 0x0132
TAKEN = re.compile(r"^(\d{4}):(\d\d):(\d\d)[ T](\d\d):(\d\d):(\d\d)$")


def capture_time(raw, given="", tz="UTC"):
    """When the picture was taken (seconds), or None. The phone sends `given` (its EXIF DateTimeOriginal, read before
    it shrinks the picture, which drops the metadata); a full-size upload still has it in the file. EXIF times are
    local, so they are read in the app's timezone. Only the time is kept, never the rest of the metadata."""
    text = (given or "").strip()
    if not text:
        try:
            ex = Image.open(io.BytesIO(raw)).getexif()
            text = str(ex.get_ifd(EXIF_IFD).get(EXIF_DATETIME_ORIGINAL) or ex.get(EXIF_DATETIME) or "").strip("\x00 ")
        except Exception:
            return None
    m = TAKEN.match(text)
    if not m:
        return None
    try:
        ts = int(datetime(*map(int, m.groups()), tzinfo=ZoneInfo(tz)).timestamp())
    except (ValueError, KeyError):
        return None
    return ts if 946684800 <= ts <= time.time() + 86400 else None      # 2000 .. tomorrow: else a wrong clock


def rel_path(date, pid, kind):
    """Where a picture lives under the data folder: by year, month and day of its stop."""
    y, m, d = date.split("-")
    return os.path.join("photos", y, m, d, f"{pid}-{kind}.jpg")


def _write(data_dir, rel, data):
    full = os.path.join(data_dir, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    tmp = full + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, full)       # never a half-written picture


def add(conn, data_dir, stop_id, raw, kind="receipt", caption="", taken="", tz="UTC"):
    if kind not in KINDS:
        raise EditError("Choose Receipt or Photo.")
    stop = conn.execute("SELECT date FROM stops WHERE id=?", (stop_id,)).fetchone()
    if not stop:
        raise EditError("That stop is gone.")
    n = conn.execute("SELECT COUNT(*) FROM photos WHERE stop_id=?", (stop_id,)).fetchone()[0]
    if n >= PER_STOP:
        raise EditError(f"A stop can have {PER_STOP} pictures at most.")
    data, w, h = normalize(raw)
    cur = conn.execute("INSERT INTO photos(stop_id,kind,caption,created,width,height,data,taken_at) VALUES (?,?,?,?,?,?,?,?)",
                       (stop_id, kind, str(caption or "").strip()[:120], int(time.time()), w, h, b"",
                        capture_time(raw, taken, tz)))
    rel = rel_path(stop["date"], cur.lastrowid, kind)
    try:
        _write(data_dir, rel, data)
    except OSError:
        conn.rollback()
        raise EditError("Could not save the picture on the server (is the data folder writable?).")
    conn.execute("UPDATE photos SET path=? WHERE id=?", (rel, cur.lastrowid))
    conn.commit()
    return cur.lastrowid, data


def move_to_files(conn, data_dir):
    """Pictures saved inside the database by the first version move out to the dated folders."""
    rows = conn.execute("SELECT p.id, p.kind, p.data, s.date FROM photos p JOIN stops s ON s.id=p.stop_id "
                        "WHERE COALESCE(p.path,'')='' AND length(p.data) > 0").fetchall()
    for r in rows:
        rel = rel_path(r["date"], r["id"], r["kind"])
        _write(data_dir, rel, r["data"])
        conn.execute("UPDATE photos SET path=?, data=? WHERE id=?", (rel, b"", r["id"]))
    if rows:
        conn.commit()


def for_day(conn, date):
    out = {}
    for r in conn.execute("SELECT p.id, p.uid, p.stop_id, p.kind, p.caption, p.created, p.taken_at, p.read_json FROM photos p "
                          "JOIN stops s ON s.id=p.stop_id WHERE s.date=? ORDER BY p.id", (date,)):
        read = None
        if r["read_json"]:
            try:
                read = json.loads(r["read_json"])
            except ValueError:
                read = None
        out.setdefault(r["stop_id"], []).append({"id": r["id"], "uid": r["uid"] or "", "kind": r["kind"],
                                                 "caption": r["caption"] or "", "created": r["created"], "taken_at": r["taken_at"],
                                                 "read": read})
    return out


def get(conn, data_dir, pid):
    r = conn.execute("SELECT path, data FROM photos WHERE id=?", (pid,)).fetchone()
    if not r:
        return None
    if r["path"]:
        try:
            with open(os.path.join(data_dir, r["path"]), "rb") as f:
                return f.read()
        except OSError:
            return None
    return r["data"] or None


def delete(conn, data_dir, pid):
    r = conn.execute("SELECT p.path, s.date FROM photos p JOIN stops s ON s.id=p.stop_id WHERE p.id=?",
                     (pid,)).fetchone()
    if not r:
        raise EditError("That picture is gone.")
    conn.execute("DELETE FROM photos WHERE id=?", (pid,))
    conn.commit()
    if r["path"]:
        try:
            os.remove(os.path.join(data_dir, r["path"]))
        except OSError:
            pass
    return r["date"]
