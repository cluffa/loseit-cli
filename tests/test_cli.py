"""CLI integration tests."""
import json

import pytest
from click.testing import CliRunner


@pytest.fixture
def runner():
    return CliRunner()


class TestCLI:
    def test_main_help(self, runner):
        from loseit.cli.main import main
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        assert "LoseIt CLI" in result.output
        assert "login" in result.output
        assert "search" in result.output
        assert "status" in result.output
        assert "summary" in result.output
        assert "log" in result.output
        assert "weight" in result.output

    def test_search_help(self, runner):
        from loseit.cli.main import main
        result = runner.invoke(main, ["search", "--help"])
        assert result.exit_code == 0
        assert "Search LoseIt's food database" in result.output

    def test_status_help(self, runner):
        from loseit.cli.main import main
        result = runner.invoke(main, ["status", "--help"])
        assert result.exit_code == 0

    def test_log_help(self, runner):
        from loseit.cli.main import main
        result = runner.invoke(main, ["log", "--help"])
        assert result.exit_code == 0

    def test_search_requires_auth(self, runner, tmp_path, monkeypatch):
        """Search should fail without authentication."""
        # Point at an empty dir — never touch the real ~/.config/loseit session
        monkeypatch.setattr("loseit.client.session.SESSION_FILE", tmp_path / "none.json")
        monkeypatch.delenv("LOSEIT_SESSION", raising=False)

        from loseit.cli.main import EXIT_AUTH, main
        result = runner.invoke(main, ["search", "chicken"])
        assert result.exit_code == EXIT_AUTH
        # Not a terminal -> machine-readable error
        error = json.loads(result.output.strip().splitlines()[-1])["error"]
        assert error["type"] == "auth"
        assert "loseit login" in error["message"]

    def test_help_has_agent_quick_reference(self, runner):
        from loseit.cli.main import main
        out = runner.invoke(main, ["--help"]).output
        for cmd in ("loseit log", "loseit edit", "loseit delete", "loseit copy", "loseit recent"):
            assert cmd in out

    @pytest.mark.parametrize("args, message", [
        (["delete"], "Give entry ids, or --all"),
        (["delete", "abc", "--all"], "Give entry ids, or --all"),
        (["edit", "bagel"], "Nothing to change"),
        (["edit", "bagel", "--amount", "1", "--servings", "2"], "either --amount or --servings"),
        (["log", "a", "b", "--amount", "2"], "single item"),
    ])
    def test_usage_errors(self, runner, args, message):
        from loseit.cli.main import main
        result = runner.invoke(main, args)
        assert result.exit_code == 2
        assert message in result.output

    def test_version(self, runner):
        from loseit.cli.main import main
        result = runner.invoke(main, ["--version"])
        assert result.exit_code == 0
        assert "0.1.0" in result.output


class TestLoginExportImport:
    @pytest.fixture(autouse=True)
    def isolated_session(self, tmp_path, monkeypatch):
        monkeypatch.setattr("loseit.client.session.SESSION_DIR", tmp_path)
        monkeypatch.setattr("loseit.client.session.SESSION_FILE", tmp_path / "session.json")
        monkeypatch.delenv("LOSEIT_SESSION", raising=False)

    def test_export_requires_session(self, runner):
        from loseit.cli.main import main
        result = runner.invoke(main, ["login", "--export"])
        assert result.exit_code == 3
        assert "Not logged in" in result.output

    def test_export_then_import(self, runner):
        from loseit.cli.main import main
        from loseit.client.session import Session, SessionStore

        SessionStore.save(Session(user_id=42, username="tester", token="tok"))
        exported = runner.invoke(main, ["login", "--export"])
        assert exported.exit_code == 0
        blob = exported.output.strip()

        SessionStore.clear()
        imported = runner.invoke(main, ["login", "--import", blob])
        assert imported.exit_code == 0
        assert "tester" in imported.output
        assert SessionStore.load().token == "tok"

    def test_import_from_stdin(self, runner):
        from loseit.cli.main import main
        from loseit.client.session import Session, SessionStore

        blob = Session(user_id=7, username="piped", token="t").export()
        result = runner.invoke(main, ["login", "--import", "-"], input=blob + "\n")
        assert result.exit_code == 0
        assert SessionStore.load().username == "piped"

    def test_import_rejects_garbage(self, runner):
        from loseit.cli.main import main
        result = runner.invoke(main, ["login", "--import", "garbage"])
        assert result.exit_code == 2
        assert "Invalid session export" in result.output


class TestSkill:
    def test_skill_prints_frontmatter(self, runner):
        from loseit.cli.main import main
        result = runner.invoke(main, ["skill"])
        assert result.exit_code == 0
        assert result.output.startswith("---\nname: loseit\n")

    def test_skill_install(self, runner, tmp_path):
        from loseit.cli.main import main, skill_text
        result = runner.invoke(main, ["skill", "install", "--dir", str(tmp_path)])
        assert result.exit_code == 0
        assert (tmp_path / "loseit" / "SKILL.md").read_text() == skill_text()


class TestCompletion:
    @pytest.fixture
    def recents(self):
        from datetime import date, timedelta
        from loseit.client.recent import RecentFoods
        today = date.today()
        r = RecentFoods()
        food = lambda fid, name, day, entry: {"food_id": fid, "entry_id": entry, "name": name, "brand": "",
                                              "category": "", "amount": 1.0, "unit": "Serving",
                                              "meal": "dinner", "calories": 94.0}
        r.record_day((today - timedelta(days=1)).isoformat(), [food("d", "Diet Cola", 1, "e1")])
        r.record_day(today.isoformat(), [food("a", "Almond Milk, Vanilla", 0, "e2")])
        r.save()

    def complete(self, args, incomplete):
        from click.shell_completion import ShellComplete
        from loseit.cli.main import main
        return [c.value for c in ShellComplete(main, {}, "loseit", "_LOSEIT_COMPLETE")
                .get_completions(args, incomplete)]

    def test_commands(self):
        assert "log" in self.complete([], "l")

    def test_log_completes_recent_foods_most_recent_first(self, recents):
        assert self.complete(["log"], "") == ["Almond Milk, Vanilla", "Diet Cola"]
        assert self.complete(["log"], "diet") == ["Diet Cola"]
        assert self.complete(["log"], "cola") == ["Diet Cola"]   # substring

    def test_edit_completes_entries_on_the_date(self, recents):
        assert self.complete(["edit"], "") == ["Almond Milk, Vanilla"]
        assert self.complete(["delete", "--date", "yesterday"], "") == ["Diet Cola"]

    def test_dates_and_meals(self, recents):
        assert self.complete(["status", "--date"], "yes") == ["yesterday"]
        assert self.complete(["log", "--meal"], "d") == ["dinner"]

    def test_completion_script(self, runner):
        from loseit.cli.main import main
        out = runner.invoke(main, ["completion", "zsh"]).output
        assert "compinit" in out and "_LOSEIT_COMPLETE" in out
