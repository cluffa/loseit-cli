# loseit-cli

A command-line client for [Lose It!](https://www.loseit.com) that's built for AI assistants. You tell your assistant what you ate, and it logs it in one command:

```bash
$ loseit log --date yesterday "dinner: 12 oz spam lite" "dinner: 2 kraft singles" "1 can mike's hard lemonade ~95cal"
✓ 12 Ounce Spam Lite — 660 cal → dinner (5c1e…)
✓ 2 Slice Cheese, American, Slice — 120 cal → dinner (8a02…)
✓ 1 Serving Mikes Hard Lemonade Zero Sugar — 94 cal → snacks (8e4e…)
  note: no 'can' unit for this food; used Serving
  2026-10-05: 874 / 1800 cal, 926 left
```

It can log, edit, swap, move, copy and delete foods; show a day's budget, burn target, deficit and macros; search the food database (results include calories); and list weigh-ins. When piped, output is JSON with structured errors and exit codes.

> Built with AI assistance (Claude Code). It is unofficial and uses LoseIt's private web API, which can change without notice.

## Install

You need Python 3.10+, [uv](https://docs.astral.sh/uv/) and Google Chrome (used for the one-time login window).

```bash
uv tool install git+https://github.com/cluffa/loseit-cli
loseit login              # log in through the pop-up Chrome window; the session is saved locally
loseit status
```

To install from a local checkout: `uv tool install /path/to/loseit-cli`. To upgrade: `uv tool upgrade loseit-cli`, then run `loseit skill install` again.

## Set up your AI assistant

Paste this into Claude Code (or any agent that can run shell commands):

```text
Install the loseit CLI and its agent skill:
1. Run: uv tool install git+https://github.com/cluffa/loseit-cli
   (if uv is missing, install it first: curl -LsSf https://astral.sh/uv/install.sh | sh)
2. Run: loseit skill install
   (Codex: loseit skill install --agent codex; other agents: loseit skill install --dir <their skills dir>)
3. Run: loseit status
   If it exits with code 3 (not logged in), tell me to run `loseit login` myself in a terminal,
   since it opens a browser window, then run `loseit status` again.
4. Read the installed SKILL.md and confirm you're ready to log food for me.
```

The skill teaches the agent the workflow: log everything in one call, pass calorie hints, read `notes` and `alternatives`, and fix mistakes with `edit --food`. It ships with the CLI. `loseit skill` prints it, and `loseit skill install` writes it to `~/.claude/skills/loseit/SKILL.md`.

For an agent on another machine (headless), run `loseit login --export` where you're logged in, then on the other machine set `LOSEIT_SESSION=<that string>` or run `loseit login --import -`.

## Usage

```bash
loseit status [--date yesterday]                 # budget, eaten, remaining, burn target, deficit, macros
loseit summary [--date D]                        # entries (with ids) + totals
loseit log "lunch: 150g chicken breast" "2 eggs" "bagel"     # no amount = your usual amount
loseit log "1 can mike's hard lemonade ~95cal"   # calorie hint picks the closest match
loseit edit "honey mustard" --amount 2 --date yesterday
loseit edit bread --food "sara lee delightful wheat" --date yesterday   # swap food, keep meal/amount
loseit delete spam --date yesterday              # or --all [--meal dinner]
loseit copy --from yesterday --meal breakfast    # repeat a meal today
loseit recent [QUERY] / search QUERY / food ID / weight [--days N]
```

Dates are `today`, `yesterday`, `tomorrow`, `±N` or `YYYY-MM-DD`. Foods resolve from your recent foods first (with the amount you usually log), then from LoseIt's search. Ids from `recent`/`search` output work as 8-character prefixes. Run `loseit --help` or `loseit <command> --help` for details.

## How it works

LoseIt's web app talks to `https://www.loseit.com/web/service` using GWT-RPC, a positional serialization format whose field layouts live only in the app's compiled JavaScript. This client:

1. **Logs in** through a real Chrome window (Playwright). LoseIt's login uses reCAPTCHA Enterprise, so headless login isn't possible. It saves the session cookies plus the app's GWT permutation and policy hash to `~/.config/loseit/session.json`.
2. **Extracts the protocol schema** from LoseIt's own JavaScript (`client/gwt_schema.py`). It downloads the permutation script and its deferred fragments, finds every type's serializer and deserializer functions, compiles them to a small op list, and reads method parameter types from the service proxy. The result is cached per app version and rebuilt automatically when LoseIt redeploys.
3. **Encodes and decodes requests** with that schema. It round-trips recorded requests byte for byte, and the response decoder fails loudly if anything is left unread.
4. **Logs food the way the web app does**: `getFood` (serving options), then `getUnsavedFoodLogEntry` (a server-prefilled entry), then set the day, meal and serving, then `updateFoodLogEntry`.
5. **Resolves plain-text items** against a local index of your recent foods (`~/.config/loseit/recent-foods.json`), then search. Candidates are sized in parallel so calorie hints and unit checks are cheap.

[`docs/HANDOFF.md`](docs/HANDOFF.md) has the architecture, verified field positions and project status.

## Development

```bash
uv venv && uv pip install -e . pytest
.venv/bin/pytest tests/ -q
```

Tests run offline against fixtures decoded from recorded traffic, with a fake LoseIt server.
