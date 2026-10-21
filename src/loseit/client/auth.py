"""Authentication: browser login, cookie import, password login and refresh."""
from __future__ import annotations

import json
import os
import re
import sys

import httpx

from .session import AUTH_COOKIES, SESSION_DIR, Session, SessionStore

WEB_HOME_URL = "https://www.loseit.com/"
# The web app itself. "/" is an edge-cached marketing page whatever the cookies
# are, so session checks must load the app page, which embeds the user.
WEB_APP_URL = "https://www.loseit.com/index.jsp"
API_LOGIN_URL = "https://api.loseit.com/account/login"
USER_ID_TYPE = "com.loseit.core.client.model.UserId/4281239478"
SUPPORTED_BROWSERS = ("chrome", "brave", "edge", "firefox", "safari")

# Cookie domains the browser would send to www.loseit.com
_WWW_COOKIE_DOMAINS = {"loseit.com", ".loseit.com", "www.loseit.com", ".www.loseit.com"}


def parse_embedded_user(html: str) -> tuple[int, str]:
    """Extract (user_id, username) from the logged-in www.loseit.com page.

    The page embeds a GWT-serialized LoseItRemoteServiceResponse in
    window.embedded_4ppl1c4710n. A UserId object serializes as
    [username_string_index, user_id, UserId_type_index], so we find the
    UserId type in the string table and read the two preceding values.
    """
    m = re.search(r'window\.embedded_4ppl1c4710n\s*=\s*"((?:[^"\\]|\\.)*)"', html)
    if not m:
        raise ValueError("Page has no embedded user data (not logged in?)")
    payload = json.loads(json.loads(f'"{m.group(1)}"'))
    table = next((x for x in payload if isinstance(x, list)), None)
    if not table or USER_ID_TYPE not in table:
        raise ValueError("Embedded data has no UserId")

    type_idx = table.index(USER_ID_TYPE) + 1
    for i in range(2, len(payload)):
        name_idx, user_id = payload[i - 2], payload[i - 1]
        if (payload[i] == type_idx and isinstance(user_id, int) and user_id > 0
                and isinstance(name_idx, int) and 0 < name_idx <= len(table)):
            return user_id, table[name_idx - 1]
    raise ValueError("Could not locate UserId in embedded data")


def discover_gwt_versions(client: httpx.Client, gwt_base_url: str) -> tuple[str, str]:
    """Return (permutation, policy_hash) for the currently deployed web app.

    LoseIt redeploys periodically; a stale policy hash makes every call fail
    with IncompatibleRemoteServiceException. The bootstrap script lists the
    permutation strong names, and the permutation script registers the main
    service proxy as `<ctor>.call(this, <base>, null, '<POLICY_HASH>', ...)`.
    """
    bootstrap = client.get(gwt_base_url + "web.nocache.js").text
    perms = re.findall(r"'([0-9A-F]{32})'", bootstrap)
    if not perms:
        raise ValueError("No permutations found in web.nocache.js")
    script = client.get(f"{gwt_base_url}{perms[0]}.cache.js").text
    m = re.search(r",null,'([0-9A-F]{32})',", script)
    if not m:
        raise ValueError("Service policy hash not found")
    return perms[0], m.group(1)


def read_browser_cookies(browser: str) -> dict[str, str]:
    """Read www.loseit.com cookies from a local browser profile."""
    import browser_cookie3

    if browser not in SUPPORTED_BROWSERS:
        raise ValueError(f"Unsupported browser: {browser}")
    jar = getattr(browser_cookie3, browser)(domain_name="loseit.com")
    return {c.name: c.value for c in jar if c.domain in _WWW_COOKIE_DOMAINS}


def parse_cookie_export(text: str) -> dict[str, str]:
    """Read www.loseit.com cookies from a browser export.

    Accepts a JSON list of cookie objects (Playwright/CDP/extension exports,
    filtered by domain), a JSON {name: value} object, a Netscape cookies.txt,
    or a plain "name=value; name2=value2" Cookie header.
    """
    text = text.strip()
    if not text:
        return {}
    if text[0] in "[{":
        try:
            data = json.loads(text)
        except ValueError as e:
            raise ValueError(f"Cookie file is not valid JSON: {e}") from e
        if isinstance(data, dict) and isinstance(data.get("cookies"), list):
            data = data["cookies"]  # Playwright storage_state
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
        return {c["name"]: c["value"] for c in data
                if isinstance(c, dict) and "name" in c and "value" in c
                and c.get("domain", "loseit.com") in _WWW_COOKIE_DOMAINS}
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if any(ln.count("\t") >= 6 for ln in lines):
        cookies = {}
        for ln in lines:
            if ln.startswith("#HttpOnly_"):
                ln = ln[len("#HttpOnly_"):]
            elif ln.startswith("#"):
                continue
            parts = ln.split("\t")
            if len(parts) >= 7 and parts[0] in _WWW_COOKIE_DOMAINS:
                cookies[parts[5]] = parts[6]
        return cookies
    if text.lower().startswith("cookie:"):
        text = text[len("cookie:"):]
    pairs = (p.strip().split("=", 1) for p in text.split(";") if "=" in p)
    return {k.strip(): v.strip() for k, v in pairs}


def _token_response_cookies(resp: httpx.Response) -> dict[str, str]:
    """Auth cookies from an account/login response (Set-Cookie, else the JSON body)."""
    cookies = {k: v for k, v in resp.cookies.items() if v}
    if not any(c in cookies for c in AUTH_COOKIES):
        try:
            token = resp.json().get("access_token", "")
        except ValueError:
            token = ""
        if token:
            cookies.update({c: token for c in AUTH_COOKIES})
    return cookies


def _response_refresh_token(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return ""
    return body.get("refresh_token", "") if isinstance(body, dict) else ""


def _login_error(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        detail = body.get("error_description") or body.get("error") or ""
    except ValueError:
        detail = resp.text[:160]
    if resp.status_code == 404:
        return "LoseIt has no account for that email."
    if "captcha" in str(detail).lower():
        return ("LoseIt asked for a captcha, so password login can't finish headlessly. "
                "Import browser cookies instead: loseit login --cookies FILE.")
    if resp.status_code in (400, 401, 403):
        return f"LoseIt rejected the login (HTTP {resp.status_code}: {detail or 'bad credentials'})."
    return f"LoseIt login failed (HTTP {resp.status_code}: {detail})."


def refresh_session(session: Session, client: httpx.Client) -> bool:
    """Renew the login with the stored refresh token. Saves and returns True on success."""
    access = next((session.cookies[c] for c in AUTH_COOKIES if session.cookies.get(c)), "")
    if not (session.refresh_token and access):
        return False
    try:
        resp = client.post(API_LOGIN_URL, headers={"Accept": "application/json"}, data={
            "grant_type": "refresh_token", "access_token": access,
            "refresh_token": session.refresh_token})
    except httpx.HTTPError:
        return False
    if resp.status_code != 200:
        return False
    fresh = _token_response_cookies(resp)
    if not any(c in fresh for c in AUTH_COOKIES):
        return False
    session.cookies.update(fresh)
    session.refresh_token = _response_refresh_token(resp) or session.refresh_token
    SessionStore.save(session)
    return True


def _has_display() -> bool:
    if sys.platform != "linux":
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def capture_cookies_via_window(timeout_s: int = 300) -> dict[str, str]:
    """Open a browser window, let the user log in, and return www.loseit.com cookies.

    Uses the installed Google Chrome (falling back to Playwright's Chromium) with
    a dedicated profile under ~/.config/loseit, so the user's own browser data is
    never read and "remember me" persists between logins.
    """
    from playwright.sync_api import sync_playwright, Error as PlaywrightError

    if not _has_display():
        raise RuntimeError(
            "No display for a login window on this machine. Use 'loseit login --password' "
            "(LOSEIT_EMAIL / LOSEIT_PASSWORD) or 'loseit login --cookies FILE' instead."
        )
    profile_dir = SESSION_DIR / "browser-profile"
    profile_dir.mkdir(parents=True, exist_ok=True)

    def logged_in(url: str) -> bool:
        return url.startswith(WEB_HOME_URL) and "/login" not in url

    with sync_playwright() as p:
        try:
            ctx = p.chromium.launch_persistent_context(str(profile_dir), channel="chrome", headless=False)
        except PlaywrightError:
            try:
                ctx = p.chromium.launch_persistent_context(str(profile_dir), headless=False)
            except PlaywrightError as e:
                raise RuntimeError(
                    "Could not start a browser. Install Google Chrome, or run "
                    "'playwright install chromium'."
                ) from e
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(WEB_HOME_URL)
            if not logged_in(page.url):
                print("Log in to LoseIt in the browser window that just opened...")
                try:
                    page.wait_for_url(logged_in, timeout=timeout_s * 1000)
                except PlaywrightError as e:
                    raise RuntimeError(
                        f"Login not completed within {timeout_s}s (or the window was closed)."
                    ) from e
            cookies = ctx.cookies(WEB_HOME_URL)
        finally:
            ctx.close()
    return {c["name"]: c["value"] for c in cookies}


class AuthManager:
    """Manages LoseIt authentication."""

    def __init__(self):
        self._client = httpx.Client(timeout=30.0)

    def login_with_window(self) -> Session:
        """Log in through a pop-up browser window and save the session."""
        try:
            cookies = capture_cookies_via_window()
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"Browser login failed: {e}") from e
        return self._save_cookie_session(cookies, source="browser window")

    def login_from_browser(self, browser: str = "chrome") -> Session:
        """Import the LoseIt session from a browser where the user is logged in."""
        try:
            cookies = read_browser_cookies(browser)
        except ValueError:
            raise
        except Exception as e:
            hint = ""
            if sys.platform == "darwin":
                hint = (
                    "\nmacOS is likely blocking access to the browser's data. Grant your "
                    "terminal app Full Disk Access (System Settings > Privacy & Security > "
                    "Full Disk Access), restart the terminal, and retry. Or run plain "
                    "'loseit login' to log in through a pop-up window instead."
                )
            raise RuntimeError(f"Could not read {browser} cookies: {e}{hint}") from e
        if not cookies:
            raise RuntimeError(
                f"No loseit.com cookies found in {browser}. "
                f"Log in at https://www.loseit.com in {browser}, then retry."
            )
        return self._save_cookie_session(cookies, source=f"{browser} cookies")

    def login_from_cookies(self, text: str) -> Session:
        """Save a session from exported browser cookies (see parse_cookie_export)."""
        cookies = parse_cookie_export(text)
        if not any(c in cookies for c in AUTH_COOKIES):
            raise RuntimeError(
                "No LoseIt login cookie (liauth) in the import. Export www.loseit.com "
                "cookies, including HttpOnly ones, from a browser that is logged in."
            )
        return self._save_cookie_session(cookies, source="imported cookies")

    def login_with_password(self, email: str, password: str) -> Session:
        """Log in over HTTP with email and password (no browser needed)."""
        try:
            resp = self._client.post(API_LOGIN_URL, headers={"Accept": "application/json"},
                                     data={"username": email.strip(), "password": password,
                                           "grant_type": "password"})
        except httpx.HTTPError as e:
            raise RuntimeError(f"Could not reach LoseIt to log in: {e}") from e
        if resp.status_code != 200:
            raise RuntimeError(_login_error(resp))
        cookies = _token_response_cookies(resp)
        if not any(c in cookies for c in AUTH_COOKIES):
            raise RuntimeError("LoseIt accepted the login but returned no session token.")
        return self._save_cookie_session(cookies, source="password login",
                                         refresh_token=_response_refresh_token(resp))

    def _save_cookie_session(self, cookies: dict[str, str], source: str,
                             refresh_token: str = "") -> Session:
        """Validate cookies against www.loseit.com, then save them as the session."""
        cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())
        resp = self._client.get(WEB_APP_URL, headers={"Cookie": cookie_header},
                                follow_redirects=False)
        if resp.is_redirect or resp.status_code != 200:
            raise RuntimeError(
                f"Session from {source} is not logged in to LoseIt (HTTP {resp.status_code})."
            )
        try:
            user_id, username = parse_embedded_user(resp.text)
        except ValueError as e:
            raise RuntimeError(f"Logged-in page not recognized: {e}") from e

        # Keep cookies the page itself set (e.g. a fresh JSESSIONID)
        cookies = {**cookies, **{k: v for k, v in resp.cookies.items() if v}}
        session = Session(user_id=user_id, username=username, cookies=cookies,
                          refresh_token=refresh_token)
        try:
            session.gwt_permutation, session.policy_hash = discover_gwt_versions(
                self._client, session.gwt_base_url)
        except (ValueError, httpx.HTTPError) as e:
            print(f"Warning: could not detect LoseIt app version ({e}); using built-in defaults")
        SessionStore.save(session)
        expires = session.expires_at()
        until = f"; session valid until {expires:%Y-%m-%d %H:%M} UTC" if expires else ""
        print(f"Logged in as {username} (ID: {user_id}) using {source}{until}")
        return session
