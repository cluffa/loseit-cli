"""LoseIt CLI - calorie tracking from the terminal."""
import os

__version__ = "0.1.0"


def _sanitize_no_proxy():
    """Drop bracketed IPv6 entries ("[::1]") from NO_PROXY: httpx fails to parse them
    and every request then crashes with "Invalid port"."""
    for var in ("NO_PROXY", "no_proxy"):
        value = os.environ.get(var)
        if value and "[" in value:
            os.environ[var] = ",".join(h for h in value.split(",") if not h.strip().startswith("["))


_sanitize_no_proxy()
