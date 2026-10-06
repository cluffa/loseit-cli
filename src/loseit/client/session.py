"""Session persistence for LoseIt credentials."""
from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


SESSION_DIR = Path.home() / ".config" / "loseit"
SESSION_FILE = SESSION_DIR / "session.json"
SESSION_ENV_VAR = "LOSEIT_SESSION"


@dataclass
class Session:
    """Stored authentication session."""
    version: int = 1
    created_at: str = ""
    api_base: str = "https://api.loseit.com"
    web_base: str = "https://www.loseit.com"
    user_id: int = 0
    username: str = ""
    token: str = ""
    cookies: dict[str, str] = field(default_factory=dict)
    service_request_token: str = ""
    policy_hash: str = "108644F06370DEF41E9A7D9DFEDBBC80"
    gwt_base_url: str = "https://d3hsih69yn4d89.cloudfront.net/web/"
    gwt_permutation: str = "0740EF43D22FF4BC274288833AA500A3"

    def is_valid(self) -> bool:
        return bool((self.token or self.cookies) and self.user_id > 0)

    def export(self) -> str:
        """Encode the session as a single-line string for copying elsewhere."""
        raw = json.dumps(asdict(self), separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode()

    @classmethod
    def from_export(cls, blob: str) -> "Session":
        """Decode a string produced by export(). Raises ValueError if malformed."""
        try:
            data = json.loads(base64.urlsafe_b64decode(blob.strip().encode()))
            return cls(**data)
        except (ValueError, TypeError) as e:
            raise ValueError(f"Invalid session export: {e}") from e


class SessionStore:
    """Read/write session to disk."""

    @staticmethod
    def load() -> Optional[Session]:
        """Load session, returning None if missing or corrupt.

        The LOSEIT_SESSION env var (an export() string) takes precedence over
        the session file, so headless environments need no file on disk.
        """
        blob = os.environ.get(SESSION_ENV_VAR)
        if blob:
            try:
                return Session.from_export(blob)
            except ValueError:
                return None
        if not SESSION_FILE.exists():
            return None
        try:
            data = json.loads(SESSION_FILE.read_text())
            return Session(**data)
        except (json.JSONDecodeError, TypeError):
            return None

    @staticmethod
    def save(session: Session):
        """Save session to disk."""
        session.created_at = datetime.now(timezone.utc).isoformat()
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        SESSION_FILE.write_text(json.dumps(asdict(session), indent=2))

    @staticmethod
    def clear():
        """Delete the session file."""
        if SESSION_FILE.exists():
            SESSION_FILE.unlink()

    @staticmethod
    def exists() -> bool:
        return SESSION_FILE.exists()
