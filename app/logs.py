"""Logging: what JobHub writes to the container log (docker logs JobHub) and keeps for Settings → General → Log.

Privacy (CLAUDE.md): never write coordinates. Requests are logged by PATH only (no query string, which can carry lat/lon),
event messages hold counts and times, not customer names or places, and every line passes `scrub`, which blanks anything
that looks like a coordinate (a number with 4+ decimals) as a safety net, tracebacks included.

LOG_LEVEL (env) or Settings → General → Log detail: info (default) or debug. The last RING_LINES lines are also kept in
memory for the Settings page (lost on restart; the container log keeps everything).
"""
import collections
import logging
import re
import sys
import threading
import uuid

RING_LINES = 1000
_ring = collections.deque(maxlen=RING_LINES)
_lock = threading.Lock()
_COORD = re.compile(r"-?\b\d{1,3}\.\d{4,}\b")
_done = False


def scrub(text):
    return _COORD.sub("[coord]", text)


class _Formatter(logging.Formatter):
    def format(self, record):
        return scrub(super().format(record))


class _Ring(logging.Handler):
    def emit(self, record):
        try:
            line = self.format(record)
        except Exception:
            return
        with _lock:
            _ring.extend(line.splitlines() or [""])


def setup(level="info"):
    """Configure the `jobhub` loggers once: stdout (the container log) and the in-memory ring."""
    global _done
    lg = logging.getLogger("jobhub")
    if not _done:
        fmt = _Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
        out = logging.StreamHandler(sys.stdout)
        out.setFormatter(fmt)
        ring = _Ring()
        ring.setFormatter(fmt)
        lg.addHandler(out)
        lg.addHandler(ring)
        lg.propagate = False
        _done = True
    set_level(level)
    return lg


def set_level(level):
    logging.getLogger("jobhub").setLevel(logging.DEBUG if str(level).lower() == "debug" else logging.INFO)


def recent(n=300):
    with _lock:
        return list(_ring)[-n:]


def ref():
    """A short reference for an error, shown to the user and written next to the traceback."""
    return uuid.uuid4().hex[:8]


def get(name):
    return logging.getLogger("jobhub." + name)
