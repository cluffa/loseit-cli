"""LoseIt CLI entry point.

Agent-first: output is JSON whenever stdout is not a terminal (or with
--json / LOSEIT_OUTPUT=json), errors are JSON on stderr with distinct exit
codes, and every write returns the day's updated totals so no follow-up
call is needed.
"""
from __future__ import annotations

import functools
import json
import os
import sys

import click
import httpx

from loseit import __version__

# Exit codes
EXIT_ERROR, EXIT_USAGE, EXIT_AUTH, EXIT_NOT_FOUND, EXIT_REJECTED, EXIT_NETWORK = 1, 2, 3, 4, 5, 6

AGENT_HELP = """\
LoseIt CLI — calorie tracking from the terminal.

\b
Quick reference (JSON output when piped; dates: today|yesterday|tomorrow|±N|YYYY-MM-DD):
  loseit status                         today's budget, eaten, remaining, macros
  loseit summary [--date D]             day's entries (with entry ids) + totals
  loseit log "ITEM" ["ITEM"...] [--date D]   log foods; returns entries + day totals
      ITEM = "[meal:] [amount][unit] food [~CAL cal]", e.g. "lunch: 150g chicken breast",
      "2 eggs", "bagel" (no amount = what you logged last time), a food id (prefix ok),
      or "1 can mike's hard lemonade ~95cal" (picks the match closest to 95 cal)
  loseit edit ENTRY [--amount N --unit U | --servings N] [--meal M] [--move-to D]
                    [--food "FOOD"]        --food swaps the food, keeping meal and amount
  loseit delete ENTRY... | --all [--meal M]   [--date D]
  loseit copy --from D [--meal M] [--date D2] [--to-meal M2]   copy a meal/day
  loseit recent [QUERY]                 your foods, most recent first
  loseit search QUERY                   foods with default serving + calories
  loseit food ID                        a food's serving sizes and nutrition
  loseit weight [--days N]              weigh-ins
  loseit skill [install]                agent skill (SKILL.md) for AI assistants
Foods resolve from your recent foods first, then search. Ids from recent or
search output work as 8-char prefixes. Count units (can, bottle, slice, each)
fall back to the food's own serving unit; results carry "notes" when that or a
calorie mismatch happens, and "alternatives" (with calories) to switch to via
edit --food. ENTRY is an entry id (unique prefix ok) or the food's name.
"""


def json_mode(flag: bool = False) -> bool:
    env = os.environ.get("LOSEIT_OUTPUT", "").lower()
    if flag or env == "json":
        return True
    if env == "human":
        return False
    return not sys.stdout.isatty()


def emit(data, as_json: bool, human) -> None:
    if json_mode(as_json):
        click.echo(json.dumps(data, indent=2, default=str))
    else:
        human(data)


def fail(message: str, code: int, kind: str, as_json: bool = False) -> None:
    if json_mode(as_json):
        click.echo(json.dumps({"error": {"type": kind, "message": message}}), err=True)
    else:
        click.echo(f"Error: {message}", err=True)
    raise SystemExit(code)


def cli_error_handler(func):
    """Map exceptions to clear messages and exit codes."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        from loseit.client.api import LoseItError, PartialLogError
        as_json = kwargs.get("as_json", False)
        try:
            return func(*args, **kwargs)
        except PartialLogError as e:
            if json_mode(as_json):
                click.echo(json.dumps({"logged": e.logged, "error": {
                    "type": "partial", "message": str(e), "failed_input": e.failed_input}}, indent=2))
            else:
                for x in e.logged:
                    click.echo(f"✓ {x['amount']:g} {x['unit']} {x['name']} → {x['meal']} ({x['entry_id'][:8]})")
                click.echo(f"Error: {e}", err=True)
            raise SystemExit(EXIT_ERROR)
        except LoseItError as e:
            fail(str(e), EXIT_REJECTED, "rejected", as_json)
        except LookupError as e:
            fail(str(e.args[0] if e.args else e), EXIT_NOT_FOUND, "not_found", as_json)
        except ValueError as e:
            fail(str(e), EXIT_USAGE, "invalid", as_json)
        except RuntimeError as e:
            if "Not authenticated" in str(e):
                fail(str(e), EXIT_AUTH, "auth", as_json)
            fail(str(e), EXIT_ERROR, "error", as_json)
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (401, 403):
                fail("Session expired. Run 'loseit login' to re-authenticate.", EXIT_AUTH, "auth", as_json)
            fail(f"LoseIt API error (HTTP {e.response.status_code})", EXIT_ERROR, "http", as_json)
        except httpx.RequestError as e:
            fail(f"Connection error: {e}", EXIT_NETWORK, "network", as_json)
    return wrapper


def json_option(f):
    return click.option("--json", "as_json", is_flag=True,
                        help="JSON output (default when stdout is not a terminal)")(f)


def date_option(f):
    return click.option("--date", "target_date", default=None,
                        help="today, yesterday, tomorrow, ±N or YYYY-MM-DD [default: today]")(f)


def _date(value):
    from loseit.client.parse import parse_date
    return parse_date(value)


MEAL_CHOICE = click.Choice(["breakfast", "lunch", "dinner", "snack", "snacks"], case_sensitive=False)


def _meal(value):
    if value is None:
        return None
    value = value.lower()
    return "snacks" if value == "snack" else value


@click.group(help=AGENT_HELP)
@click.version_option(version=__version__)
def main():
    pass


# ──────────────────────────────────────────────
# Auth
# ──────────────────────────────────────────────

@main.command()
@click.option("--browser", type=click.Choice(["chrome", "brave", "edge", "firefox", "safari"]),
              default=None,
              help="Import cookies from this browser instead of opening a login window "
                   "(macOS: needs Full Disk Access)")
@click.option("--export", "do_export", is_flag=True,
              help="Print the saved session as one line (for LOSEIT_SESSION on another machine)")
@click.option("--import", "import_blob", default=None, metavar="STRING",
              help="Save a session from an --export string ('-' reads stdin)")
def login(browser: str | None, do_export: bool, import_blob: str | None):
    """Log in through a pop-up browser window (or import/export a session)."""
    from loseit.client.auth import AuthManager
    from loseit.client.session import Session, SessionStore

    if do_export:
        session = SessionStore.load()
        if not session or not session.is_valid():
            fail("Not logged in. Run 'loseit login' first.", EXIT_AUTH, "auth")
        click.echo(session.export())
        return

    if import_blob:
        if import_blob == "-":
            import_blob = click.get_text_stream("stdin").read()
        try:
            session = Session.from_export(import_blob)
        except ValueError as e:
            fail(str(e), EXIT_USAGE, "invalid")
        if not session.is_valid():
            fail("Imported session is missing a token or user ID.", EXIT_USAGE, "invalid")
        SessionStore.save(session)
        click.echo(f"Imported session for {session.username} (ID: {session.user_id})")
        return

    auth = AuthManager()
    try:
        if browser:
            auth.login_from_browser(browser)
        else:
            auth.login_with_window()
    except RuntimeError as e:
        fail(str(e), EXIT_AUTH, "auth")


# ──────────────────────────────────────────────
# Agent skill
# ──────────────────────────────────────────────

SKILL_DIRS = {"claude": "~/.claude/skills", "codex": "~/.codex/skills", "agents": "~/.agents/skills"}


def skill_text() -> str:
    from importlib.resources import files
    return files("loseit").joinpath("skill", "SKILL.md").read_text()


@main.group(invoke_without_command=True)
@click.pass_context
def skill(ctx):
    """Print the agent skill (SKILL.md) for using this CLI; see 'skill install'."""
    if ctx.invoked_subcommand is None:
        click.echo(skill_text(), nl=False)


@skill.command("install")
@click.option("--agent", type=click.Choice(list(SKILL_DIRS)), default="claude", show_default=True,
              help="Install into this agent's user skills directory")
@click.option("--dir", "skills_dir", default=None, type=click.Path(file_okay=False),
              help="Skills directory to install into instead (e.g. .claude/skills for one project)")
def skill_install(agent: str, skills_dir: str | None):
    """Install the skill as <skills dir>/loseit/SKILL.md (overwrites an older copy)."""
    from pathlib import Path

    target = Path(skills_dir or SKILL_DIRS[agent]).expanduser() / "loseit" / "SKILL.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(skill_text())
    click.echo(f"Installed loseit skill: {target}")


# ──────────────────────────────────────────────
# Reading
# ──────────────────────────────────────────────

@main.command()
@date_option
@json_option
@cli_error_handler
def status(target_date: str | None, as_json: bool):
    """Calorie budget, eaten, remaining and macros for a day."""
    from loseit.client.api import LoseItAPI
    from loseit.cli.formatting import print_status

    summary = LoseItAPI().get_daily_summary(_date(target_date))
    data = {k: v for k, v in summary.items() if k != "foods"}
    emit(data, as_json, lambda d: print_status(summary))


@main.command()
@date_option
@json_option
@cli_error_handler
def summary(target_date: str | None, as_json: bool):
    """A day's food entries (with entry ids) and totals."""
    from loseit.client.api import LoseItAPI
    from loseit.cli.formatting import print_status, print_table

    result = LoseItAPI().get_daily_summary(_date(target_date))

    def human(r):
        print_table(["Entry", "Meal", "Food", "Amount", "Cal"],
                    [[f["entry_id"][:8], f["meal"], f["name"], f"{f['amount']:g} {f['unit']}",
                      f"{f['calories']:g}"] for f in r["foods"]],
                    title=f"Summary: {r['date']}")
        print_status(r)
    emit(result, as_json, human)


@main.command()
@click.argument("query", required=False)
@click.option("--limit", type=int, default=15, show_default=True)
@click.option("--refresh", is_flag=True, help="Re-read the last 30 days from LoseIt first")
@json_option
@cli_error_handler
def recent(query: str | None, limit: int, refresh: bool, as_json: bool):
    """Foods you've logged, best match / most recent first (like the app)."""
    from loseit.client.api import LoseItAPI
    from loseit.cli.formatting import print_table

    foods = LoseItAPI().recent_foods(query, limit=limit, refresh=refresh)
    emit({"foods": foods}, as_json, lambda d: print_table(
        ["Food ID", "Name", "Last", "Usual amount", "Times"],
        [[f["food_id"][:8], f["name"], f["last_date"], f"{f['last_amount']:g} {f['last_unit']}",
          str(f["times"])] for f in d["foods"]],
        title="Recent foods" + (f": {query}" if query else "")))


@main.command()
@click.argument("query")
@click.option("--limit", type=int, default=10, show_default=True)
@json_option
@cli_error_handler
def search(query: str, limit: int, as_json: bool):
    """Search LoseIt's food database."""
    from loseit.client.api import LoseItAPI
    from loseit.cli.formatting import print_table

    results = LoseItAPI().search_foods(query, limit=limit)
    emit({"results": results}, as_json, lambda d: print_table(
        ["Food ID", "Name", "Brand", "Serving", "Cal"],
        [[r["food_id"][:8], r["name"], r["brand"], r.get("serving", ""),
          f"{r['calories']:g}" if "calories" in r else ""] for r in d["results"]],
        title=f"Search: {query}"))


@main.command()
@click.argument("food_id")
@json_option
@cli_error_handler
def food(food_id: str, as_json: bool):
    """A food's serving sizes and nutrition (id or recent-food id prefix)."""
    from loseit.client.api import LoseItAPI
    from loseit.cli.formatting import print_table

    info = LoseItAPI().get_food(food_id)
    emit(info, as_json, lambda d: print_table(
        ["Amount", "Unit", "Calories"],
        [[f"{s['amount']:g}", s["unit"], f"{s['calories']:g}"] for s in d["servings"]],
        title=d["name"] + (f" ({d['brand']})" if d["brand"] else "")))


@main.command()
@click.option("--days", type=int, default=90, show_default=True)
@json_option
@cli_error_handler
def weight(days: int, as_json: bool):
    """Recorded weigh-ins."""
    from loseit.client.api import LoseItAPI
    from loseit.cli.formatting import print_table

    results = LoseItAPI().get_weight_history(days)

    def human(d):
        if not d["weight_history"]:
            click.echo(f"No weigh-ins in the last {days} days.")
            return
        rows, prev = [], None
        for r in d["weight_history"]:
            rows.append([r["date"], f"{r['weight']:.1f}", "" if prev is None else f"{r['weight'] - prev:+.1f}"])
            prev = r["weight"]
        print_table(["Date", "Weight (lbs)", "Change"], rows, title=f"Weight, last {days} days")
    emit({"weight_history": results, "days": days}, as_json, human)


# ──────────────────────────────────────────────
# Writing
# ──────────────────────────────────────────────

@main.command()
@click.argument("items", nargs=-1, required=True)
@click.option("--meal", type=MEAL_CHOICE, default=None,
              help="Meal for items without a 'meal:' prefix [default: by time of day; "
                   "other days: the food's usual meal]")
@date_option
@click.option("--amount", type=float, default=None, help="Amount in --unit (single item)")
@click.option("--unit", default=None, help="Unit: g, oz, cup, ml, serving, ... (single item)")
@click.option("--servings", type=float, default=None, help="Number of default servings (single item)")
@click.option("--dry-run", is_flag=True, help="Show which foods would be logged, without logging")
@json_option
@cli_error_handler
def log(items: tuple[str, ...], meal: str | None, target_date: str | None, amount: float | None,
        unit: str | None, servings: float | None, dry_run: bool, as_json: bool):
    """Log one or more foods in one call.

    \b
    ITEM is "[meal:] [amount][unit] food" or a food id:
      loseit log bagel                       your usual bagel, usual amount
      loseit log "2 eggs" "coffee" --meal breakfast
      loseit log "lunch: 150g chicken breast" "dinner: 1 cup rice"
      loseit log 1bc020d4 --amount 150 --unit g
      loseit log "1 can mike's hard lemonade ~95cal" --date yesterday
    """
    from loseit.client.api import LoseItAPI
    from loseit.client.parse import parse_item

    parsed = [parse_item(t) for t in items]
    if amount is not None or unit is not None or servings is not None:
        if len(parsed) != 1:
            raise click.UsageError("--amount/--unit/--servings apply to a single item")
        if amount is not None and servings is not None:
            raise click.UsageError("Use either --amount or --servings, not both")
        p = parsed[0]
        p.amount, p.unit = (servings, None) if servings is not None else (amount, unit or p.unit)
        if p.amount is None:
            p.unit = unit
    api = LoseItAPI()
    day = _date(target_date)
    if dry_run:
        plans = [api.describe_plan(p) for p in api.plan_items(parsed, _meal(meal), day)]
        emit({"would_log": plans, "date": day.isoformat()}, as_json,
             lambda d: [(click.echo(f"{x['input']} → {x['amount']:g} {x['unit']} {x['name']} — "
                                    f"{x['calories']:g} cal → {x['meal']} [{x['matched_by']}]"),
                         _human_notes(x)) for x in d["would_log"]])
        return
    result = api.log_items(parsed, _meal(meal), day)

    def human(r):
        for e in r["logged"]:
            src = "" if e["matched_by"] == "id" else f" [{e['matched_by']}]"
            click.echo(f"✓ {e['amount']:g} {e['unit']} {e['name']} — {e['calories']:g} cal → "
                       f"{e['meal']} ({e['entry_id'][:8]}){src}")
            _human_notes(e)
        _human_day(r["day"])
    emit(result, as_json, human)


@main.command()
@click.argument("entry")
@date_option
@click.option("--amount", type=float, default=None, help="New amount (in --unit, or the logged unit)")
@click.option("--unit", default=None, help="New unit")
@click.option("--servings", type=float, default=None, help="New number of default servings")
@click.option("--meal", type=MEAL_CHOICE, default=None, help="Move to another meal")
@click.option("--move-to", default=None, help="Move to another day")
@click.option("--food", default=None,
              help='Swap in another food: id (prefix ok) or text like "sara lee delightful bread"')
@json_option
@cli_error_handler
def edit(entry: str, target_date: str | None, amount: float | None, unit: str | None,
         servings: float | None, meal: str | None, move_to: str | None, food: str | None,
         as_json: bool):
    """Change a logged food's amount, unit, meal or day, or swap the food.

    \b
    ENTRY is an entry id (prefix ok) or the food's name, e.g. "bagel".
      loseit edit "honey mustard" --amount 2 --date yesterday
      loseit edit bread --food "sara lee delightful wheat" --date yesterday
    """
    from loseit.client.api import LoseItAPI

    if amount is not None and servings is not None:
        raise click.UsageError("Use either --amount or --servings, not both")
    if all(v is None for v in (amount, unit, servings, meal, move_to, food)):
        raise click.UsageError("Nothing to change: give --amount/--unit/--servings/--meal/--move-to/--food")
    result = LoseItAPI().edit_entry(entry, _date(target_date), amount=amount, unit=unit,
                                    servings=servings, meal=_meal(meal),
                                    move_to=_date(move_to) if move_to else None, food=food)

    def human(r):
        e = r["updated"]
        was = f" (was {e['replaced']['name']})" if "replaced" in e else ""
        click.echo(f"✓ {e['name']}: {e['amount']:g} {e['unit']} — {e['calories']:g} cal, "
                   f"{e['meal']} on {e['date']} ({e['entry_id'][:8]}){was}")
        _human_notes(e)
        _human_day(r["day"])
    emit(result, as_json, human)


@main.command()
@click.argument("entries", nargs=-1)
@date_option
@click.option("--all", "all_entries", is_flag=True, help="Delete every food entry on the day")
@click.option("--meal", type=MEAL_CHOICE, default=None, help="Only entries in this meal")
@json_option
@cli_error_handler
def delete(entries: tuple[str, ...], target_date: str | None, all_entries: bool,
           meal: str | None, as_json: bool):
    """Delete logged foods by entry id (prefix ok) or food name, or --all."""
    from loseit.client.api import LoseItAPI

    if bool(entries) == all_entries:
        raise click.UsageError("Give entry ids, or --all (not both)")
    result = LoseItAPI().delete_entries(_date(target_date), list(entries), all_entries, _meal(meal))

    def human(r):
        if not r["deleted"]:
            click.echo("Nothing to delete.")
        for e in r["deleted"]:
            click.echo(f"✓ Deleted {e['name']} ({e['meal']}, {e['entry_id'][:8]})")
        _human_day(r["day"])
    emit(result, as_json, human)


@main.command()
@click.option("--from", "from_date", required=True, help="Day to copy from (e.g. yesterday)")
@click.option("--meal", type=MEAL_CHOICE, default=None, help="Only this meal [default: whole day]")
@date_option
@click.option("--to-meal", type=MEAL_CHOICE, default=None, help="Put the copies in this meal")
@json_option
@cli_error_handler
def copy(from_date: str, meal: str | None, target_date: str | None, to_meal: str | None, as_json: bool):
    """Copy a meal or whole day to another day (default: today).

    \b
      loseit copy --from yesterday --meal breakfast
      loseit copy --from 2026-10-01 --meal dinner --to-meal lunch --date tomorrow
    """
    from loseit.client.api import LoseItAPI

    result = LoseItAPI().copy_entries(_date(from_date), _date(target_date), _meal(meal), _meal(to_meal))

    def human(r):
        for e in r["copied"]:
            click.echo(f"✓ {e['amount']:g} {e['unit']} {e['name']} — {e['calories']:g} cal → "
                       f"{e['meal']} ({e['entry_id'][:8]})")
        _human_day(r["day"])
    emit(result, as_json, human)


def _human_notes(e: dict) -> None:
    for note in e.get("notes", []):
        click.echo(f"  note: {note}")
    alts = e.get("alternatives") or []
    if alts:
        click.echo("  other matches: " + "; ".join(
            f"{a['name']}" + (f" {a['calories']:g} cal" if "calories" in a else "") + f" ({a['food_id'][:8]})"
            for a in alts))


def _human_day(day: dict) -> None:
    rem = day["calories_remaining"]
    left = f"{rem:.0f} left" if rem >= 0 else f"{-rem:.0f} over"
    click.echo(f"  {day['date']}: {day['calories_consumed']:.0f} / "
               f"{day['calorie_budget'] + day['exercise_calories']:.0f} cal, {left}")
