"""Sign-in for JobHub: user accounts, the sign-in throttle and password reset by email.

Built for one user now (the owner) but stored as a table so more logins can be added later. Passwords are kept only as
salted scrypt hashes (werkzeug, part of Flask). Each user has a session generation number: changing the password or
"Sign out everywhere" bumps it, which ends every other signed-in device.

Before an account exists the old APP_PASSWORD (env) still works, so nobody is locked out by an update; once the account
is made, APP_PASSWORD is ignored. With no account and no APP_PASSWORD the app stays open on the home network only:
anything arriving through the Cloudflare tunnel is refused until the account is created.

Console (Unraid: docker exec -it JobHub python -m app.auth ...):
    python -m app.auth list
    python -m app.auth create <username> [email]
    python -m app.auth reset <username>          # set a new password (asks twice)
"""
import hashlib
import ipaddress
import re
import secrets
import threading
import time

from werkzeug.security import check_password_hash, generate_password_hash

MIN_PASSWORD = 12
USERNAME = re.compile(r"^[A-Za-z0-9._@-]{2,40}$")
EMAIL = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")
RESET_TTL_S = 30 * 60                       # a reset link works for 30 minutes, once
_DUMMY = generate_password_hash("not a real password, only spends the same time")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    email TEXT NOT NULL DEFAULT '',
    pw_hash TEXT NOT NULL,
    session_gen INTEGER NOT NULL DEFAULT 1,
    active INTEGER NOT NULL DEFAULT 1,
    created_ts INTEGER NOT NULL,
    pw_changed_ts INTEGER NOT NULL,
    last_login_ts INTEGER
);
CREATE TABLE IF NOT EXISTS password_resets (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    created_ts INTEGER NOT NULL,
    expires_ts INTEGER NOT NULL,
    used_ts INTEGER
);
"""


class AuthError(ValueError):
    pass


# ---- accounts ---------------------------------------------------------------------------------------------------
def has_users(conn):
    return conn.execute("SELECT 1 FROM users WHERE active=1 LIMIT 1").fetchone() is not None


def get(conn, uid):
    return conn.execute("SELECT * FROM users WHERE id=? AND active=1", (uid,)).fetchone()


def find(conn, name_or_email):
    v = (name_or_email or "").strip()
    if not v:
        return None
    return conn.execute("SELECT * FROM users WHERE active=1 AND (username=? COLLATE NOCASE OR "
                        "(email<>'' AND email=? COLLATE NOCASE)) ORDER BY id LIMIT 1", (v, v)).fetchone()


def check_password_rules(pw, pw2=None, username=""):
    if pw2 is not None and pw != pw2:
        raise AuthError("The two passwords are not the same.")
    if len(pw) < MIN_PASSWORD:
        raise AuthError(f"Use at least {MIN_PASSWORD} characters (a few words together is easiest to remember).")
    if username and username.lower() in pw.lower():
        raise AuthError("The password must not contain the username.")
    if len(set(pw)) < 5:
        raise AuthError("That password is too simple.")


def create(conn, username, password, email="", now=None):
    username, email = (username or "").strip(), (email or "").strip()
    if not USERNAME.match(username):
        raise AuthError("Username: 2-40 letters, numbers or . _ @ -")
    if email and not EMAIL.match(email):
        raise AuthError(f"“{email}” is not an email address.")
    check_password_rules(password, username=username)
    if conn.execute("SELECT 1 FROM users WHERE username=? COLLATE NOCASE", (username,)).fetchone():
        raise AuthError("That username is taken.")
    now = int(now or time.time())
    cur = conn.execute("INSERT INTO users(username,email,pw_hash,created_ts,pw_changed_ts) VALUES (?,?,?,?,?)",
                       (username, email, generate_password_hash(password), now, now))
    conn.commit()
    return cur.lastrowid


def verify(conn, username, password):
    """The user row when the username and password match, else None (the same time is spent either way)."""
    u = conn.execute("SELECT * FROM users WHERE username=? COLLATE NOCASE AND active=1",
                     ((username or "").strip(),)).fetchone()
    if not u:
        check_password_hash(_DUMMY, password or "")
        return None
    return u if check_password_hash(u["pw_hash"], password or "") else None


def set_password(conn, uid, password, now=None):
    """New password; every other signed-in device is signed out (session generation bumped). Returns the new gen."""
    u = get(conn, uid)
    if not u:
        raise AuthError("No such user.")
    check_password_rules(password, username=u["username"])
    now = int(now or time.time())
    conn.execute("UPDATE users SET pw_hash=?, pw_changed_ts=?, session_gen=session_gen+1 WHERE id=?",
                 (generate_password_hash(password), now, uid))
    conn.execute("UPDATE password_resets SET used_ts=? WHERE user_id=? AND used_ts IS NULL", (now, uid))
    conn.commit()
    return get(conn, uid)["session_gen"]


def set_email(conn, uid, email):
    email = (email or "").strip()
    if email and not EMAIL.match(email):
        raise AuthError(f"“{email}” is not an email address.")
    conn.execute("UPDATE users SET email=? WHERE id=?", (email, uid))
    conn.commit()


def sign_out_everywhere(conn, uid):
    conn.execute("UPDATE users SET session_gen=session_gen+1 WHERE id=?", (uid,))
    conn.commit()
    return get(conn, uid)["session_gen"]


def touch_login(conn, uid, now=None):
    conn.execute("UPDATE users SET last_login_ts=? WHERE id=?", (int(now or time.time()), uid))
    conn.commit()


# ---- password reset ---------------------------------------------------------------------------------------------
def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def new_reset(conn, uid, now=None):
    """A one-time reset token (only its hash is stored). Older unused links for this user stop working."""
    now = int(now or time.time())
    token = secrets.token_urlsafe(32)
    conn.execute("UPDATE password_resets SET used_ts=? WHERE user_id=? AND used_ts IS NULL", (now, uid))
    conn.execute("INSERT INTO password_resets(user_id,token_hash,created_ts,expires_ts) VALUES (?,?,?,?)",
                 (uid, _hash(token), now, now + RESET_TTL_S))
    conn.execute("DELETE FROM password_resets WHERE expires_ts < ?", (now - 30 * 86400,))
    conn.commit()
    return token


def reset_user(conn, token, now=None):
    """The user a reset token belongs to, while it is unused and not expired; else None."""
    if not token or len(token) > 100:
        return None
    r = conn.execute("SELECT * FROM password_resets WHERE token_hash=?", (_hash(token),)).fetchone()
    if not r or r["used_ts"] or r["expires_ts"] < int(now or time.time()):
        return None
    return get(conn, r["user_id"])


def reset_email(username, link):
    return ("Someone (hopefully you) asked to reset the JobHub password for " + username + ".\n\n"
            "Open this link within 30 minutes to choose a new password. It works once:\n" + link + "\n\n"
            "If you did not ask for this, ignore this email: your password stays the same.\n")


# ---- where a request comes from -----------------------------------------------------------------------------------
def via_tunnel(headers):
    """True when the request came through Cloudflare (its edge adds these headers; a visitor cannot remove them)."""
    return bool(headers.get("Cf-Connecting-Ip") or headers.get("Cf-Ray"))


def client_ip(headers, remote_addr):
    return (headers.get("Cf-Connecting-Ip") or remote_addr or "").strip()


def is_private(ip):
    try:
        a = ipaddress.ip_address(ip)
        return a.is_private or a.is_loopback
    except ValueError:
        return False


def at_home(headers, remote_addr):
    """True for a request from the home network. Judged by the connection itself, never by Cf-Connecting-Ip: only
    Cloudflare can be trusted to set that header, and anything that reaches JobHub another way can make it up."""
    return not via_tunnel(headers) and is_private((remote_addr or "").strip())


def is_home_host(host):
    """True when a Host header can only name something on the home network: a private address, localhost, a bare name
    or a .local / .lan / .home.arpa name."""
    h = (host or "").strip().lower()
    h = h[1:].split("]")[0] if h.startswith("[") else h.rsplit(":", 1)[0] if h.count(":") == 1 else h
    if not h:
        return False
    try:
        ipaddress.ip_address(h)
        return is_private(h)
    except ValueError:
        return "." not in h or h.endswith((".local", ".lan", ".home.arpa"))


# ---- sign-in throttle --------------------------------------------------------------------------------------------
class Throttle:
    """Counts failed sign-ins (in memory; a restart clears it).

    Per address: 10 failures in 15 minutes pause sign-in from that address until the oldest failure is 15 minutes old.
    Per username: 30 failures in 15 minutes (from anywhere) pause that username the same way, except from the home
    network, so the owner can still sign in at home while someone is guessing from outside.
    Reset emails: 3 an hour per address and per user."""
    WINDOW_S = 15 * 60
    PER_IP = 10
    PER_USER = 30
    RESETS = 3

    def __init__(self, clock=time.time):
        self.clock = clock
        self.fail = {}
        self.resets = {}
        self.lock = threading.Lock()

    def _recent(self, store, key, window):
        now = self.clock()
        v = [t for t in store.get(key, []) if now - t < window]
        if v:
            store[key] = v
        else:
            store.pop(key, None)
        return v

    def wait_s(self, ip, username, home=False):
        """Seconds until sign-in is allowed again (0 = allowed now). `home` (see at_home) skips the username pause."""
        with self.lock:
            now, waits = self.clock(), [0]
            ips = self._recent(self.fail, ("ip", ip), self.WINDOW_S)
            if len(ips) >= self.PER_IP:
                waits.append(ips[-self.PER_IP] + self.WINDOW_S - now)
            if not home:
                us = self._recent(self.fail, ("user", (username or "").lower()), self.WINDOW_S)
                if len(us) >= self.PER_USER:
                    waits.append(us[-self.PER_USER] + self.WINDOW_S - now)
            return max(0, int(max(waits)) + (1 if max(waits) > 0 else 0))

    def failed(self, ip, username):
        """Record a failure; True when this one started a pause (worth one alert)."""
        with self.lock:
            now = self.clock()
            out = False
            for key, limit in ((("ip", ip), self.PER_IP), (("user", (username or "").lower()), self.PER_USER)):
                v = self._recent(self.fail, key, self.WINDOW_S)
                v.append(now)
                self.fail[key] = v
                out = out or len(v) == limit
            return out

    def succeeded(self, ip):
        with self.lock:
            self.fail.pop(("ip", ip), None)

    def reset_allowed(self, ip, uid=None):
        with self.lock:
            keys = [("ip", ip)] + ([("user", uid)] if uid else [])
            if any(len(self._recent(self.resets, k, 3600)) >= self.RESETS for k in keys):
                return False
            for k in keys:
                self.resets.setdefault(k, []).append(self.clock())
            return True


# ---- console ----------------------------------------------------------------------------------------------------
def _main(argv):
    import getpass
    import os
    import sys

    from . import db
    conn = db.connect(os.environ.get("DATA_DIR", "/data"))
    cmd = argv[0] if argv else ""
    if cmd == "list":
        for u in conn.execute("SELECT * FROM users ORDER BY id"):
            print(f"{u['username']}  email={u['email'] or '-'}  active={u['active']}")
        return 0
    if cmd in ("create", "reset") and len(argv) >= 2:
        u = conn.execute("SELECT * FROM users WHERE username=? COLLATE NOCASE", (argv[1],)).fetchone()
        if cmd == "reset" and not u:
            print("No such user. `python -m app.auth list` shows them.")
            return 1
        pw = getpass.getpass("New password: ")
        if getpass.getpass("Again: ") != pw:
            print("The two passwords are not the same.")
            return 1
        try:
            if cmd == "create":
                create(conn, argv[1], pw, argv[2] if len(argv) > 2 else "")
            else:
                conn.execute("UPDATE users SET active=1 WHERE id=?", (u["id"],))
                set_password(conn, u["id"], pw)
        except AuthError as e:
            print(e)
            return 1
        print("Done. Every other signed-in device is signed out." if cmd == "reset" else "Account created.")
        return 0
    print(__doc__.split("Console", 1)[1].split("\n", 1)[1])
    return 1 if cmd else 0


if __name__ == "__main__":
    import sys
    sys.exit(_main(sys.argv[1:]))
