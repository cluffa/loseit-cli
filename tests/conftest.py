# tests/conftest.py
import json
from pathlib import Path

import shutil

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_har():
    """Load the HAR file."""
    har_path = Path(__file__).parent.parent / "www.loseit.com.har"
    with open(har_path) as f:
        return json.load(f)


@pytest.fixture(autouse=True)
def _isolate_session(tmp_path, monkeypatch):
    """Keep every test away from the real ~/.config/loseit session."""
    monkeypatch.setattr("loseit.client.session.SESSION_DIR", tmp_path)
    monkeypatch.setattr("loseit.client.session.SESSION_FILE", tmp_path / "session.json")
    monkeypatch.delenv("LOSEIT_SESSION", raising=False)
    # Offline GWT schema for the default app version (see gwt_schema.load_schema)
    from loseit.client.session import Session
    shutil.copy(FIXTURES_DIR / "gwt_schema.json",
                tmp_path / f"gwt-schema-{Session().gwt_permutation}.json")
