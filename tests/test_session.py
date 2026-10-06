"""Session persistence and export/import."""
import pytest


class TestSessionStore:
    def test_save_and_load(self, tmp_path, monkeypatch):
        from loseit.client.session import Session, SessionStore

        monkeypatch.setattr("loseit.client.session.SESSION_FILE", tmp_path / "test_session.json")

        s = Session(user_id=123, username="testuser", token="abc123")
        SessionStore.save(s)

        loaded = SessionStore.load()
        assert loaded is not None
        assert loaded.user_id == 123
        assert loaded.token == "abc123"

    def test_is_valid(self):
        from loseit.client.session import Session
        assert Session(user_id=0, token="").is_valid() is False
        assert Session(user_id=1, token="abc").is_valid() is True

    def test_export_roundtrip(self):
        from loseit.client.session import Session
        s = Session(user_id=123, username="testuser", token="abc123")
        blob = s.export()
        assert "\n" not in blob
        assert Session.from_export(blob) == s

    def test_from_export_rejects_garbage(self):
        from loseit.client.session import Session
        with pytest.raises(ValueError):
            Session.from_export("not-a-session")

    def test_env_var_overrides_file(self, tmp_path, monkeypatch):
        from loseit.client.session import Session, SessionStore

        monkeypatch.setattr("loseit.client.session.SESSION_FILE", tmp_path / "test_session.json")
        SessionStore.save(Session(user_id=1, username="file", token="f"))
        monkeypatch.setenv("LOSEIT_SESSION", Session(user_id=2, username="env", token="e").export())

        loaded = SessionStore.load()
        assert loaded.username == "env"
        assert loaded.user_id == 2
