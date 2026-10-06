"""Browser-cookie login tests (synthetic data, no network)."""
import json

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
