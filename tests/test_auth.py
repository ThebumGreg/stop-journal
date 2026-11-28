"""Sign-in: accounts, throttle, password reset by email, the tunnel rules and the safety headers."""
from dataclasses import replace

import pytest

from app import auth, create_app, db, mailer, notify
from app.config import Config

CFG = Config.from_env()
PW = "correct horse battery"
TUNNEL = {"Cf-Connecting-Ip": "44.9.9.9", "X-Forwarded-Proto": "https"}


def make(tmp_path, **kw):
    cfg = replace(CFG, data_dir=str(tmp_path), app_password=kw.pop("app_password", ""), **kw)
    return create_app(cfg, client=object())


def with_user(tmp_path, email="me@example.com", **kw):
    app = make(tmp_path, **kw)
    conn = db.connect(str(tmp_path))
    auth.create(conn, "greg", PW, email)
    conn.close()
    return app


def sign_in(c, user="greg", pw=PW, **kw):
    return c.post("/login", data={"username": user, "password": pw}, **kw)


def test_no_login_yet_open_at_home_refused_through_tunnel(tmp_path):
    c = make(tmp_path).test_client()
    assert c.get("/settings").status_code == 200                        # first install on the home network
    assert "Create your login" in c.get("/settings").data.decode()
    r = c.get("/settings", headers=TUNNEL)
    assert r.status_code == 403 and "no login yet" in r.data.decode()
    assert c.get("/account", headers=TUNNEL).status_code == 403
    assert c.get("/health", headers=TUNNEL).status_code == 200


def test_create_account_then_sign_in_required(tmp_path):
    app = make(tmp_path)
    c = app.test_client()
    r = c.post("/account", data={"action": "create", "username": "greg", "email": "me@example.com",
                                 "password": "short", "password2": "short"})
    assert r.status_code == 400 and "at least 12" in r.data.decode()
    r = c.post("/account", data={"action": "create", "username": "greg", "email": "me@example.com",
                                 "password": PW, "password2": PW})
    assert r.status_code == 302 and c.get("/settings").status_code == 200   # signed in on this device
    anon = app.test_client()
    assert anon.get("/settings").status_code == 302
    assert anon.get("/api/day/2026-11-18").status_code == 401
    assert anon.post("/account", data={"action": "create", "username": "x", "password": PW,
                                       "password2": PW}).status_code == 401         # no second account this way
    assert sign_in(anon, pw="wrong password!!").status_code == 401
    assert sign_in(anon, user="GREG").status_code == 302                 # username is not case-sensitive
    assert anon.get("/settings").status_code == 200
    conn = db.connect(str(tmp_path))
    row = conn.execute("SELECT pw_hash FROM users").fetchone()[0]
    assert PW not in row and row.startswith(("scrypt:", "pbkdf2:"))


def test_app_password_works_until_account_exists(tmp_path):
    app = make(tmp_path, app_password="oldpw")
    c = app.test_client()
    assert "Username" not in c.get("/login").data.decode()
    assert c.post("/login", data={"password": "oldpw"}).status_code == 302
    assert c.get("/account").status_code == 200
    c.post("/account", data={"action": "create", "username": "greg", "password": PW, "password2": PW})
    other = app.test_client()
    assert other.post("/login", data={"password": "oldpw"}).status_code == 401
    assert c.get("/settings").status_code == 200                         # the creator moved to the new login


def test_cookie_is_long_lived_and_secure_through_the_tunnel(tmp_path):
    c = with_user(tmp_path).test_client()
    home = sign_in(c).headers.get_all("Set-Cookie")[0]
    assert "Expires=" in home and "HttpOnly" in home and "SameSite=Lax" in home and "Secure" not in home
    c2 = with_user(tmp_path / "b").test_client()
    tun = sign_in(c2, headers=TUNNEL).headers.get_all("Set-Cookie")[0]
    assert "Secure" in tun


def test_throttle_pauses_an_address_and_alerts_once(tmp_path, monkeypatch):
    app = with_user(tmp_path, pushover_token="t", pushover_user="u")
    sent = []
    monkeypatch.setattr(notify, "send", lambda cfg, title, text, *a, **k: sent.append(text))
    c = app.test_client()
    for _ in range(10):
        assert sign_in(c, pw="nope nope nope", headers=TUNNEL).status_code == 401
    r = sign_in(c, headers=TUNNEL)                                       # right password, still paused
    assert r.status_code == 429 and "Try again in" in r.data.decode()
    assert len(sent) == 1 and "greg" not in sent[0] and "44.9" not in sent[0]
    assert sign_in(c).status_code == 302                                 # a different address (home) still works


def test_throttle_per_username_spares_home_network():
    clock = [1000.0]
    t = auth.Throttle(clock=lambda: clock[0])
    for i in range(30):
        t.failed(f"44.1.2.{i}", "greg")
    assert t.wait_s("44.1.2.200", "greg") > 0                        # new outside address: paused
    assert t.wait_s("192.168.1.20", "greg", home=True) == 0              # home network: allowed
    clock[0] += 15 * 60 + 1
    assert t.wait_s("44.1.2.200", "greg") == 0


def test_made_up_cloudflare_address_does_not_skip_the_throttle(tmp_path):
    """Something that reaches JobHub without Cloudflare can invent Cf-Connecting-Ip, a new private one each try."""
    app = with_user(tmp_path)
    c = app.test_client()
    codes = [sign_in(c, pw=f"wrong password {i}", headers={"Cf-Connecting-Ip": f"10.0.0.{i}"}).status_code
             for i in range(31)]
    assert codes == [401] * 30 + [429]
    assert sign_in(c).status_code == 302                                 # a real home request still gets in


def test_at_home_goes_by_the_connection():
    assert auth.at_home({}, "192.168.128.10") and auth.at_home({}, "127.0.0.1")
    assert not auth.at_home({}, "44.1.2.3") and not auth.at_home({}, None)
    assert not auth.at_home({"Cf-Connecting-Ip": "192.168.1.2"}, "192.168.128.10")
    assert not auth.at_home({"Cf-Ray": "abc"}, "192.168.128.10")


def test_nothing_signed_in_is_cached_and_manifest_sends_the_cookie(tmp_path):
    c = with_user(tmp_path).test_client()
    sign_in(c)
    for path in ("/", "/day", "/api/places", "/api/parts/export.csv", "/api/parts/template.csv", "/settings",
                 "/settings/log", "/settings/backup-full.zip", "/health"):
        r = c.get(path)
        assert r.status_code == 200 and r.headers["Cache-Control"] == "no-store", path
    assert 'crossorigin="use-credentials"' in c.get("/").data.decode()
    assert c.get("/sw.js").headers["Cache-Control"] == "no-cache"
    assert "no-store" not in c.get("/static/day.js").headers["Cache-Control"]


def test_lookups_take_nothing_from_the_address(tmp_path):
    """Coordinates, search text and names are posted: a proxy's access log only ever sees the path."""
    c = make(tmp_path).test_client()
    for path in ("/api/suggest?lat=34.1&lon=-118.2", "/api/places/search?q=acme", "/api/vendors/detail?name=x",
                 "/api/places/group-history?name=x", "/api/gas-stations/history?key=x"):
        assert c.get(path).status_code == 405, path
    assert c.post("/api/suggest?lat=34.1&lon=-118.2").status_code == 400
    assert c.post("/api/suggest", json=["not", "a", "dict"]).status_code == 400


def test_change_password_signs_out_other_devices(tmp_path):
    app = with_user(tmp_path)
    phone, pc = app.test_client(), app.test_client()
    sign_in(phone), sign_in(pc)
    r = pc.post("/account", data={"action": "password", "current": "wrong", "password": "a new long password",
                                  "password2": "a new long password"})
    assert r.status_code == 400 and "current password is wrong" in r.data.decode()
    pc.post("/account", data={"action": "password", "current": PW, "password": "a new long password",
                              "password2": "a new long password"})
    assert pc.get("/settings").status_code == 200                        # this device stays in
    assert phone.get("/settings").status_code == 302                     # the phone is signed out
    assert sign_in(phone, pw="a new long password").status_code == 302
    pc.post("/account", data={"action": "everywhere"})
    assert phone.get("/settings").status_code == 302 and pc.get("/settings").status_code == 200


def test_forgot_password_email_link_once(tmp_path, monkeypatch):
    app = with_user(tmp_path, smtp_user="jobhub@gmail.com", smtp_password="app pw", app_url="https://jobhub.example.com")
    mails = []
    monkeypatch.setattr(mailer, "send", lambda cfg, msg: mails.append(msg))
    c = app.test_client()
    assert "Forgot password?" in c.get("/login").data.decode()
    same = c.post("/forgot", data={"who": "nobody"}).data.decode()
    assert not mails
    r = c.post("/forgot", data={"who": "ME@example.com"})
    assert r.data.decode() == same                                       # never says whether the account exists
    assert len(mails) == 1 and mails[0]["To"] == "me@example.com"
    body = mails[0].get_content()
    link = next(w for w in body.split() if w.startswith("https://jobhub.example.com/reset/"))
    path = link.replace("https://jobhub.example.com", "")
    assert c.get(path).status_code == 200
    assert c.post(path, data={"password": "x", "password2": "x"}).status_code == 400
    r = c.post(path, data={"password": "brand new password", "password2": "brand new password"})
    assert r.status_code == 302 and "m=reset" in r.headers["Location"]
    assert c.get(path).status_code == 404                                # used once
    assert sign_in(c, pw=PW).status_code == 401
    assert sign_in(c, pw="brand new password").status_code == 302


def test_reset_link_expires_and_new_one_replaces_old(tmp_path):
    with_user(tmp_path)
    conn = db.connect(str(tmp_path))
    uid = auth.find(conn, "greg")["id"]
    old = auth.new_reset(conn, uid, now=1000)
    new = auth.new_reset(conn, uid, now=1001)
    assert auth.reset_user(conn, old, now=1002) is None
    assert auth.reset_user(conn, new, now=1002)["username"] == "greg"
    assert auth.reset_user(conn, new, now=1001 + auth.RESET_TTL_S + 1) is None
    assert auth.reset_user(conn, "not-a-token") is None


def test_reset_emails_limited(tmp_path, monkeypatch):
    app = with_user(tmp_path, smtp_user="jobhub@gmail.com", smtp_password="app pw")
    mails = []
    monkeypatch.setattr(mailer, "send", lambda cfg, msg: mails.append(msg))
    c = app.test_client()
    for _ in range(5):
        c.post("/forgot", data={"who": "greg"})
    assert len(mails) == 3


def test_changes_from_another_site_refused(tmp_path):
    c = make(tmp_path).test_client()
    assert c.post("/settings", data={}, headers={"Origin": "https://evil.example"}).status_code == 403
    assert c.post("/api/client-log", json={}, headers={"Referer": "https://evil.example/x"}).status_code == 403
    assert c.post("/settings", data={}, headers={"Origin": "http://localhost"}).status_code in (200, 302)
    assert c.post("/api/client-log", json={}, headers={"Origin": "http://localhost"}).status_code == 204
    pub = make(tmp_path / "p", app_url="https://jobhub.example.com").test_client()
    assert pub.post("/api/client-log", json={}, headers={"Origin": "https://jobhub.example.com"}).status_code == 204


def test_safety_headers_and_no_backup_download_links(tmp_path):
    app = with_user(tmp_path)
    c = app.test_client()
    r = c.get("/login")
    assert r.headers["X-Content-Type-Options"] == "nosniff" and r.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert "frame-ancestors 'self'" in r.headers["Content-Security-Policy"] and r.headers["Cache-Control"] == "no-store"
    sign_in(c)
    tok = c.post("/api/file-token", json={"url": "/settings/log"}).json["token"]
    anon = app.test_client()
    assert anon.get(f"/settings/backup-full.zip?ft={tok}").status_code == 302
    assert anon.get(f"/api/parts/export.csv?ft={tok}").status_code == 401      # made for the log only
    assert anon.get(f"/settings/log?ft={tok}").status_code == 200
    c.post("/account", data={"action": "everywhere"})
    assert anon.get(f"/settings/log?ft={tok}").status_code == 302              # signing out everywhere ends links
    csp = c.get("/").headers["Content-Security-Policy"]
    assert "script-src 'self' https://unpkg.com/leaflet@1.9.4/;" in csp and "unsafe" not in csp


def test_open_redirect_blocked(tmp_path):
    c = with_user(tmp_path).test_client()
    for bad in ("//evil.com", "https://evil.com", "/\\evil.com"):
        assert c.post("/login?next=" + bad, data={"username": "greg", "password": PW}).headers["Location"] == "/"
        c.post("/logout")


def test_console_create_and_reset(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    answers = iter([PW, PW, "another long password", "another long password"])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(answers))
    assert auth._main(["create", "greg", "me@example.com"]) == 0
    assert auth._main(["reset", "greg"]) == 0
    conn = db.connect(str(tmp_path))
    assert auth.verify(conn, "greg", "another long password")
    assert auth._main(["reset", "nobody"]) == 1


@pytest.mark.parametrize("pw,ok", [("short", False), ("aaaaaaaaaaaaaaaa", False), ("greg greg greg 1", False),
                                   ("a much longer phrase", True)])
def test_password_rules(pw, ok):
    if ok:
        auth.check_password_rules(pw, username="greg")
    else:
        with pytest.raises(auth.AuthError):
            auth.check_password_rules(pw, username="greg")


@pytest.mark.parametrize("nxt", ["/%09/evil.example", "/%0a/evil.example", "/%0d%0a/evil.example", "/%0b/evil.example"])
def test_redirect_with_hidden_characters_stays_here(tmp_path, nxt):
    c = with_user(tmp_path).test_client()
    r = c.post("/login?next=" + nxt, data={"username": "greg", "password": PW})
    assert r.status_code == 302 and r.headers["Location"] == "/"


def test_changing_the_reset_email_needs_the_password(tmp_path, monkeypatch):
    app = with_user(tmp_path, pushover_token="t", pushover_user="u")
    sent = []
    monkeypatch.setattr(notify, "send", lambda cfg, title, text, *a, **k: sent.append(text))
    c = app.test_client()
    sign_in(c)
    email = lambda: db.connect(str(tmp_path)).execute("SELECT email FROM users").fetchone()[0]
    for form in ({}, {"current": "not the password"}):
        r = c.post("/account", data={"action": "email", "email": "thief@example.com", **form})
        assert r.status_code == 400 and email() == "me@example.com" and not sent
    assert c.post("/account", data={"action": "email", "email": "new@example.com", "current": PW}).status_code == 302
    assert email() == "new@example.com" and len(sent) == 1 and "new@" not in sent[0]
    assert 'name="current"' in c.get("/account").data.decode().split('value="email"')[1].split("</form>")[0]


def test_reset_link_never_uses_a_made_up_host(tmp_path, monkeypatch):
    mails = []
    monkeypatch.setattr(mailer, "send", lambda cfg, msg: mails.append(msg.get_content()))
    gmail = {"smtp_user": "jobhub@gmail.com", "smtp_password": "app pw"}
    app = with_user(tmp_path, **gmail)                                     # no App address saved
    c = app.test_client()
    ask = lambda host, **h: c.post("/forgot", data={"who": "greg"},
                                   headers={"Host": host, "Origin": "http://" + host, **h})
    for host, extra in (("evil.example", {}), ("evil.example:8000", {}), ("192.168.128.245:8000", TUNNEL)):
        assert ask(host, **extra).status_code == 200 and not mails, host
    assert ask("192.168.128.245:8000").status_code == 200
    assert "http://192.168.128.245:8000/reset/" in mails[0]
    fixed = with_user(tmp_path / "b", app_url="https://jobhub.example.com", **gmail).test_client()
    fixed.post("/forgot", data={"who": "greg"}, headers={"Host": "evil.example", "Origin": "http://evil.example"})
    assert "https://jobhub.example.com/reset/" in mails[1] and "evil" not in mails[1]


def test_home_host_names():
    for h in ("192.168.1.5:8000", "10.0.0.2", "localhost:8000", "tower", "tower.local:8000", "jobhub.home.arpa", "[::1]:8000"):
        assert auth.is_home_host(h), h
    for h in ("evil.example", "8.8.8.8", "192.168.1.5.evil.example", "evil.example:80", "[2001:4860:4860::8888]", ""):
        assert not auth.is_home_host(h), h
