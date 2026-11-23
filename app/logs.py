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


# ---- stuck-request watchdog ---------------------------------------------------------------------------
# Requests in progress, by thread: when one runs longer than STUCK_S its path and the code it is waiting in are logged
# (again every REPEAT_S while it lasts), so a site that "hangs" says why instead of showing nothing.
STUCK_S, REPEAT_S = 15, 30
_active = {}
_active_lock = threading.Lock()


def request_started(method, path):
    import time
    with _active_lock:
        _active[threading.get_ident()] = [method, path, time.monotonic(), 0.0]


def request_ended():
    with _active_lock:
        _active.pop(threading.get_ident(), None)


def check_stuck(now=None):
    """Log requests running longer than STUCK_S with their stack. Returns how many were reported."""
    import sys
    import time
    import traceback
    now = now or time.monotonic()
    lg = get("watchdog")
    frames = sys._current_frames()
    with _active_lock:
        items = [(tid, list(v)) for tid, v in _active.items()]
        busy = len(items)
    n = 0
    for tid, (method, path, t0, last) in items:
        if now - t0 < STUCK_S or (last and now - last < REPEAT_S):
            continue
        with _active_lock:
            if tid in _active:
                _active[tid][3] = now
        fr = frames.get(tid)
        where = "".join(traceback.format_stack(fr)[-8:]) if fr else "(no stack)"
        lg.warning("request still running after %ds: %s %s (%d request%s in progress)\n%s",
                   now - t0, method, path, busy, "" if busy == 1 else "s", where.rstrip())
        n += 1
    return n


def start_watchdog(interval=5):
    import time

    def loop():
        while True:
            time.sleep(interval)
            try:
                check_stuck()
            except Exception as e:                  # never let the watchdog die
                get("watchdog").warning("watchdog check failed: %s", type(e).__name__)
    threading.Thread(target=loop, daemon=True, name="jobhub-watchdog").start()
