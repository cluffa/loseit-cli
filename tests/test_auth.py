"""Browser-cookie login tests (synthetic data, no network)."""
import base64
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from loseit.client import auth as auth_mod
from loseit.client.auth import AuthManager, USER_ID_TYPE, parse_embedded_user


def make_page(user_id=12345678, username="testuser"):
    """Build a fake logged-in page with GWT-style embedded data."""
    table = [
        "com.loseit.core.client.service.responses.LoseItRemoteServiceResponse/1071940969",
        "java.lang.Integer/3438268394",
        USER_ID_TYPE,
        username,
    ]
    payload = [0, -4, 4, user_id, 3, 29, 1, table, 0, 7]
    js_string = json.dumps(json.dumps(payload))  # JS string literal, quotes escaped
    return f"<html><script>window.embedded_4ppl1c4710n = {js_string};</script></html>"


@pytest.fixture
def isolated_session(tmp_path, monkeypatch):
    monkeypatch.setattr("loseit.client.session.SESSION_DIR", tmp_path)
    monkeypatch.setattr("loseit.client.session.SESSION_FILE", tmp_path / "session.json")
    monkeypatch.delenv("LOSEIT_SESSION", raising=False)


def manager_with(handler):
    mgr = AuthManager()
    mgr._client = httpx.Client(transport=httpx.MockTransport(handler))
    return mgr


class TestParseEmbeddedUser:
    def test_extracts_user(self):
        assert parse_embedded_user(make_page(987, "alice")) == (987, "alice")

    def test_no_embedded_data(self):
        with pytest.raises(ValueError, match="not logged in"):
            parse_embedded_user("<html>login page</html>")


class TestLoginFromBrowser:
    def test_success_saves_cookie_session(self, isolated_session, monkeypatch):
        from loseit.client.session import SessionStore

        monkeypatch.setattr(auth_mod, "read_browser_cookies", lambda b: {"sid": "abc"})
        seen = {}

        def handler(request):
            if request.url.host == "www.loseit.com":
                seen["cookie"] = request.headers.get("cookie")
            return httpx.Response(200, text=make_page(42, "bob"))

        session = manager_with(handler).login_from_browser("chrome")
        assert seen["cookie"] == "sid=abc"
        assert (session.user_id, session.username) == (42, "bob")
        loaded = SessionStore.load()
        assert loaded.cookies == {"sid": "abc"} and loaded.is_valid()

    def test_redirect_means_not_logged_in(self, isolated_session, monkeypatch):
        monkeypatch.setattr(auth_mod, "read_browser_cookies", lambda b: {"sid": "old"})
        mgr = manager_with(lambda r: httpx.Response(302, headers={"location": "https://my.loseit.com/login"}))
        with pytest.raises(RuntimeError, match="not logged in"):
            mgr.login_from_browser("chrome")

    def test_no_cookies(self, isolated_session, monkeypatch):
        monkeypatch.setattr(auth_mod, "read_browser_cookies", lambda b: {})
        with pytest.raises(RuntimeError, match="No loseit.com cookies"):
            AuthManager().login_from_browser("chrome")


def test_api_sends_cookies_and_gwt_headers(isolated_session):
    from loseit.client.api import LoseItAPI
    from loseit.client.session import Session, SessionStore

    SessionStore.save(Session(user_id=1, username="u", cookies={"sid": "abc"}))
    api = LoseItAPI()
    assert api._client.cookies.get("sid") == "abc"
    assert api._client.headers["X-GWT-Permutation"]
    assert "Authorization" not in api._client.headers


class TestLoginWithWindow:
    def test_window_cookies_are_validated_and_saved(self, isolated_session, monkeypatch):
        from loseit.client.session import SessionStore

        monkeypatch.setattr(auth_mod, "capture_cookies_via_window", lambda: {"sid": "win"})
        mgr = manager_with(lambda r: httpx.Response(200, text=make_page(77, "carol")))
        session = mgr.login_with_window()
        assert (session.user_id, session.username) == (77, "carol")
        assert SessionStore.load().cookies == {"sid": "win"}

    def test_window_failure_surfaces_as_runtime_error(self, isolated_session, monkeypatch):
        def boom():
            raise OSError("display unavailable")
        monkeypatch.setattr(auth_mod, "capture_cookies_via_window", boom)
        with pytest.raises(RuntimeError, match="Browser login failed"):
            AuthManager().login_with_window()

    def test_cli_login_defaults_to_window(self, isolated_session, monkeypatch):
        from click.testing import CliRunner
        from loseit.cli.main import main

        called = {}
        monkeypatch.setattr(AuthManager, "login_with_window", lambda self: called.setdefault("window", True))
        monkeypatch.setattr(AuthManager, "login_from_browser", lambda self, b: called.setdefault("browser", b))

        assert CliRunner().invoke(main, ["login"]).exit_code == 0
        assert CliRunner().invoke(main, ["login", "--browser", "brave"]).exit_code == 0
        assert called == {"window": True, "browser": "brave"}


def test_discover_gwt_versions():
    from loseit.client.auth import discover_gwt_versions

    perm, policy = "A" * 32, "B" * 32

    def handler(request):
        if request.url.path.endswith("web.nocache.js"):
            return httpx.Response(200, text=f"ic='{perm}',rc='{'C' * 32}'")
        if request.url.path.endswith(f"{perm}.cache.js"):
            return httpx.Response(200, text=f"Wud.call(this,ai(),'remote_logging','{'D' * 32}',x);"
                                            f"Wud.call(this,ai(),null,'{policy}',Edr)")
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert discover_gwt_versions(client, "https://cdn.example/web/") == (perm, policy)


# ──────────────────────────────────────────────
# Headless login, cookie import, expiry and refresh
# ──────────────────────────────────────────────

def make_jwt(exp: datetime) -> str:
    def b64(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")
    return f"{b64({'alg': 'HS256'})}.{b64({'sub': '1', 'exp': int(exp.timestamp())})}.sig"


NOW = datetime.now(timezone.utc)
FIXTURES = Path(__file__).parent / "fixtures"
MARKETING_PAGE = "<!DOCTYPE html><html><head><title>Lose It! - Weight Loss That Fits</title></head></html>"
AUTH_EXCEPTION = ('//EX[2,1,["com.loseit.core.client.service.'
                  'UserAuthenticationFailedException/1234567890",""],0,7]')


class TestParseCookieExport:
    def test_playwright_list_filters_domain(self):
        data = [{"name": "liauth", "value": "t", "domain": ".loseit.com"},
                {"name": "x", "value": "y", "domain": "my.loseit.com"},
                {"name": "ga", "value": "z", "domain": ".google.com"}]
        assert auth_mod.parse_cookie_export(json.dumps(data)) == {"liauth": "t"}

    def test_storage_state_and_dict(self):
        state = {"cookies": [{"name": "liauth", "value": "t", "domain": "www.loseit.com"}]}
        assert auth_mod.parse_cookie_export(json.dumps(state)) == {"liauth": "t"}
        assert auth_mod.parse_cookie_export('{"liauth": "t"}') == {"liauth": "t"}

    def test_netscape_and_header(self):
        txt = ("# Netscape HTTP Cookie File\n"
               "#HttpOnly_.loseit.com\tTRUE\t/\tTRUE\t0\tliauth\tt\n"
               ".example.com\tTRUE\t/\tTRUE\t0\tother\tz\n")
        assert auth_mod.parse_cookie_export(txt) == {"liauth": "t"}
        assert auth_mod.parse_cookie_export("Cookie: liauth=t; JSESSIONID=j") == {
            "liauth": "t", "JSESSIONID": "j"}


class TestCookieAndPasswordLogin:
    def test_cookie_import_validates_and_saves(self, isolated_session):
        from loseit.client.session import SessionStore
        mgr = manager_with(lambda r: httpx.Response(200, text=make_page(5, "dee")))
        mgr.login_from_cookies("liauth=t; fn_auth=t")
        assert SessionStore.load().cookies["liauth"] == "t"

    def test_cookie_import_without_login_cookie(self, isolated_session):
        with pytest.raises(RuntimeError, match="liauth"):
            AuthManager().login_from_cookies("_ga=1")

    def test_real_login_response_yields_working_session(self, isolated_session):
        """Regression: real success shape (tokens only in Set-Cookie, no refresh
        token) and "/" serving a cached marketing page while /index.jsp is the app."""
        from loseit.client.session import SessionStore

        fx = json.loads((FIXTURES / "account_login_success.json").read_text())
        token = make_jwt(NOW + timedelta(days=14))
        seen = {}

        def handler(request):
            if request.url.host == "api.loseit.com":
                seen["form"] = dict(httpx.QueryParams(request.content.decode()))
                headers = [("set-cookie", c.replace("{TOKEN}", token)) for c in fx["set_cookie"]]
                return httpx.Response(fx["status"], json=fx["body"], headers=headers)
            if request.url.host == "www.loseit.com":
                if request.url.path != "/index.jsp":
                    return httpx.Response(200, text=MARKETING_PAGE)
                seen["cookie"] = request.headers["cookie"]
                return httpx.Response(200, text=make_page(12345678, "Alex"), headers=[
                    ("set-cookie", "JSESSIONID=j1; Path=/; Secure; HttpOnly"),
                    ("set-cookie", "liwebconfig=w1; Path=/")])
            return httpx.Response(404)

        session = manager_with(handler).login_with_password("user@example.com", "pw")
        assert seen["form"] == {"username": "user@example.com", "password": "pw",
                                "grant_type": "password"}
        assert f"liauth={token}" in seen["cookie"] and f"fn_auth={token}" in seen["cookie"]
        assert (session.user_id, session.username) == (12345678, "Alex")
        saved = SessionStore.load()
        assert saved.cookies["liauth"] == token and saved.cookies["JSESSIONID"] == "j1"
        assert saved.refresh_token == ""
        assert saved.expires_at() is not None

    def test_marketing_page_is_not_a_login(self, isolated_session):
        """The cached "/" page has no user data; validation must not rely on it."""
        assert auth_mod.WEB_APP_URL.endswith("/index.jsp")
        mgr = manager_with(lambda r: httpx.Response(200, text=MARKETING_PAGE))
        with pytest.raises(RuntimeError, match="not logged in"):
            mgr.login_from_cookies("liauth=t")

    def test_password_login_unknown_account(self, isolated_session):
        mgr = manager_with(lambda r: httpx.Response(
            404, json={"error": "invalid_grant", "error_description": "NOT_FOUND"}))
        with pytest.raises(RuntimeError, match="no account"):
            mgr.login_with_password("x@example.com", "pw")

    def test_cli_password_login_reads_env(self, isolated_session, monkeypatch):
        from click.testing import CliRunner
        from loseit.cli.main import main

        got = {}
        monkeypatch.setattr(AuthManager, "login_with_password",
                            lambda self, e, p: got.update(email=e, password=p))
        monkeypatch.setenv("LOSEIT_EMAIL", "a@example.com")
        monkeypatch.setenv("LOSEIT_PASSWORD", "pw")
        assert CliRunner().invoke(main, ["login", "--password"]).exit_code == 0
        assert got == {"email": "a@example.com", "password": "pw"}

    def test_cli_password_login_without_env_is_usage_error(self, isolated_session, monkeypatch):
        from click.testing import CliRunner
        from loseit.cli.main import EXIT_USAGE, main
        monkeypatch.delenv("LOSEIT_EMAIL", raising=False)
        monkeypatch.delenv("LOSEIT_PASSWORD", raising=False)
        assert CliRunner().invoke(main, ["login", "--password"]).exit_code == EXIT_USAGE

    def test_no_display_fails_fast(self, monkeypatch):
        monkeypatch.setattr(auth_mod.sys, "platform", "linux")
        monkeypatch.delenv("DISPLAY", raising=False)
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        with pytest.raises(RuntimeError, match="No display"):
            auth_mod.capture_cookies_via_window()


class TestExpiryAndRefresh:
    def test_expired_session_without_refresh_token_is_auth_error(self, isolated_session):
        from click.testing import CliRunner
        from loseit.cli.main import EXIT_AUTH, main
        from loseit.client.api import LoseItAPI, LoseItAuthError
        from loseit.client.session import Session, SessionStore

        SessionStore.save(Session(user_id=1, username="u",
                                  cookies={"liauth": make_jwt(NOW - timedelta(days=1))}))
        with pytest.raises(LoseItAuthError, match="expired"):
            LoseItAPI()
        result = CliRunner().invoke(main, ["status", "--json"])
        assert result.exit_code == EXIT_AUTH
        assert '"auth"' in result.output and "loseit login" in result.output

    def test_expiring_session_is_refreshed(self, isolated_session, monkeypatch):
        from loseit.client.api import LoseItAPI
        from loseit.client.session import Session, SessionStore

        fresh = make_jwt(NOW + timedelta(days=14))
        SessionStore.save(Session(user_id=1, username="u", refresh_token="r1",
                                  cookies={"liauth": make_jwt(NOW + timedelta(hours=3))}))

        def fake_refresh(session, client):
            session.cookies["liauth"] = fresh
            SessionStore.save(session)
            return True
        monkeypatch.setattr(auth_mod, "refresh_session", fake_refresh)
        api = LoseItAPI()
        assert api._client.cookies.get("liauth") == fresh
        assert SessionStore.load().cookies["liauth"] == fresh

    def test_refresh_session_posts_refresh_grant(self, isolated_session):
        from loseit.client.session import Session, SessionStore
        seen = {}

        def handler(request):
            seen.update(httpx.QueryParams(request.content.decode()))
            return httpx.Response(200, json={"access_token": "new", "refresh_token": "r2"})

        session = Session(user_id=1, username="u", refresh_token="r1", cookies={"liauth": "old"})
        client = httpx.Client(transport=httpx.MockTransport(handler))
        assert auth_mod.refresh_session(session, client)
        assert seen == {"grant_type": "refresh_token", "access_token": "old", "refresh_token": "r1"}
        assert SessionStore.load().cookies["liauth"] == "new"
        assert SessionStore.load().refresh_token == "r2"

    def test_refresh_without_token_is_noop(self):
        from loseit.client.session import Session
        session = Session(user_id=1, cookies={"liauth": "old"})
        assert auth_mod.refresh_session(session, httpx.Client()) is False

    def test_server_auth_rejection_maps_to_auth_error(self, isolated_session):
        from loseit.client.api import LoseItAPI, LoseItAuthError
        from loseit.client.session import Session, SessionStore

        SessionStore.save(Session(user_id=1, username="u", cookies={"sid": "s"}))
        api = LoseItAPI()
        api._client = httpx.Client(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, text=AUTH_EXCEPTION)))
        with pytest.raises(LoseItAuthError, match="loseit login"):
            api.call("getDailyDetailsForDate", api.token(), api.day_date(NOW.date()))

    def test_rotated_cookies_are_persisted(self, isolated_session):
        from loseit.client.api import LoseItAPI
        from loseit.client.session import Session, SessionStore

        SessionStore.save(Session(user_id=1, username="u", cookies={"liauth": "old"}))
        api = LoseItAPI()
        api._keep_rotated_cookies(httpx.Response(
            200, headers={"set-cookie": "liauth=new; Domain=loseit.com"},
            request=httpx.Request("POST", api.WEB_SERVICE_URL)))
        assert SessionStore.load().cookies["liauth"] == "new"


def test_no_proxy_bracketed_ipv6_is_dropped(monkeypatch):
    import loseit
    monkeypatch.setenv("NO_PROXY", "localhost,::1,[::1],10.0.0.1,[fd00::1]")
    loseit._sanitize_no_proxy()
    import os
    assert os.environ["NO_PROXY"] == "localhost,::1,10.0.0.1"
    httpx.Client()  # would raise InvalidURL with the bracketed entries
