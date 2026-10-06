# HANDOFF — loseit-cli

**Last updated:** 2026-10-06 · **Branch:** `main` · public repo

## What this is

An agent-first Python CLI for LoseIt.com: log/edit/delete foods by plain text in one call, read day summaries, recents and weigh-ins. It speaks LoseIt's private GWT-RPC protocol, using field layouts extracted automatically from LoseIt's own JavaScript.

## Quick start

```bash
cd loseit-cli && uv venv && uv pip install -e . pytest
.venv/bin/loseit login            # pop-up Chrome window; log in; session saved
.venv/bin/loseit log "lunch: 150g chicken breast" "2 eggs"
.venv/bin/loseit summary --date yesterday
.venv/bin/pytest tests/ -q        # 240 passed
```

## Commands (agent-first)

**Agent skill: `src/loseit/skill/SKILL.md`** (`loseit skill install`). Output is JSON whenever stdout isn't a terminal (or `--json`, or `LOSEIT_OUTPUT=json|human`). Errors are JSON on stderr: `{"error": {"type", "message"}}`. Exit codes: 2 invalid/usage, 3 auth, 4 not found/ambiguous, 5 rejected by LoseIt, 6 network, 1 other. Dates: `today|yesterday|tomorrow|±N|YYYY-MM-DD`.

| Command | Notes |
|---|---|
| `log ITEM...` | ITEM = `"[meal:] [amount][unit] food [~CAL cal]"` or a food id (prefix ok for recent/searched foods). Resolves via recents → search; a calorie hint picks the closest candidate. Count units (can/bottle/each) fall back to the best match's own unit, with a note. No amount = last amount used. Everything is planned and validated before any write; `--dry-run`. Returns entries (+`notes`, `alternatives` with calories) and day totals. |
| `edit ENTRY` | ENTRY = entry id prefix or food name. `--amount/--unit/--servings/--meal/--move-to`; `--food X` swaps the food (new entry + delete), keeping the meal and amount. |
| `delete ENTRY...` / `--all` | `--meal` narrows names or `--all`. |
| `copy --from D [--meal M] [--date D2] [--to-meal M2]` | copy a meal/day. |
| `status`, `summary` | day totals (+ entries with ids). |
| `recent [QUERY]` | the user's foods ranked like the app (match, recency, frequency). |
| `search`, `food ID`, `weight [--days]` | search DB (each hit has a default `serving` + `calories` via parallel getFood), serving sizes, weigh-ins. |
| `completion [zsh\|bash\|fish]` | shell completion script (Click); completers in `main.py` read only the local recents index (foods, entries on `--date`, date words). Enable with `eval "$(loseit completion zsh)"` in ~/.zshrc. |
| `login [--browser X] [--export] [--import S]` | `LOSEIT_SESSION` env var = exported session for headless agents. |

## Architecture

```
cli/main.py ── client/api.py ──┬─ gwt_schema.py  (extract layouts from JS; decode/encode RPC)
   formatting.py   parse.py ───┤   recent.py     (local recents index, ranking)
                               ├─ auth.py        (window/browser-cookie login, app version discovery)
                               └─ session.py     (~/.config/loseit/session.json, LOSEIT_SESSION)
```

- **`gwt_schema.py`**: builds `{"types", "methods"}` from the permutation script plus its deferred fragments (`deferredjs/<perm>/1..N.cache.js`). Each type's `instantiate`, `deserialize` and `serialize` function is compiled to ops (`i d l s b o n`, `rep`/`arr` loops) by recognizing the stream primitives by body shape. Service proxies give each method's parameter types, including overloads. `ResponseDecoder` reads `//OK[...]` (including chunked `.concat(...)`) back to front and fails if anything is left unread. `RequestDecoder`/`RequestEncoder` round-trip all 69 recorded requests byte for byte. Cached as `~/.config/loseit/gwt-schema-<perm>.json`; rebuilt automatically after LoseIt redeploys (login re-discovers the permutation and policy hash).
- **`api.py`**: `call(method, *params)` takes the types from the method table, retries timeouts only for `get*`/`search*`, and maps `//EX` to `LoseItError`. Field positions are documented in docstrings (`summarize_daily_details`, `summarize_food`, `build_serving_size`, `get_weight_history`).
- **`recent.py`**: `~/.config/loseit/recent-foods.json`. It's fed by every day fetched, and backfilled over 30 days with one range call when older than 6 h. It also remembers the last 500 search hits (`seen`) so their id prefixes resolve.
- **Resolution (`api.resolve_item`)**: candidates = up to 4 recents (+ 8 search hits when there's no recent fit or a calorie hint). `_estimate_calories` fetches them in parallel and marks whether the unit fits. Choice: closest to the hint, else the top candidate, else the first exact unit fit; otherwise an error listing the matches. A hint counts as met within 10% (min 10 cal); if no candidate meets it, up to 2 respellings learned from result names (`parse.query_variants`: "peanutbutter" ↔ "peanut butter") are searched too. A swapped count unit that doesn't match the hint is rejected. Entries over 2000 cal get a "check the amount" note.

## Key protocol facts (verified live)

- **Auth:** cookies (`liauth`, `fn_auth`, `JSESSIONID`, …) plus `X-GWT-Permutation`. Login is `POST api.loseit.com/account/login` with reCAPTCHA Enterprise, so headless login isn't possible. Use the pop-up window or `--export`.
- **`DayDate`:** (java.util.Date at local midnight, days since **2000-12-31**, hoursFromGMT).
- **`ServiceRequestToken`:** (null, UserId(id, username, hoursFromGMT)).
- **Logging:**
  1. `getFood` returns the serving options. Each reads "`[4]` of unit `[2]` = `[3]` base servings".
  2. `getUnsavedFoodLogEntry(token, foodKey, null, name)` returns a pre-filled entry with a fresh key.
  3. Set the context: `[1]` day, `[4]=-1`, `[8]` meal.
  4. Set `FoodServingSize` (`[0]` base servings, `[5]` amount) and `FoodNutrients[1]` = base servings.
  5. Call `updateFoodLogEntry` (single-entry overload).
- **Moves:** changing an entry's day in place leaves a stale copy on the old day. A move is therefore a new entry plus `deleteFoodLogEntry` of the original.
- **Nutrients:** `FoodNutrients` = ([0] servings the values are for, [1] servings logged, [2] {FoodMeasurement: value}); total = value × [1]/[0]. FoodMeasurement: 0 kcal, 3 fat, 4 sat fat, 8 chol, 9 sodium, 10 carbs, 11 fiber, 12 sugar, 13 protein. FoodLogEntryType: 0 breakfast, 1 lunch, 2 dinner, 3 snacks. FoodMeasure names and sizes are in `api._MEASURES`.
- **Day totals:** budget = `DailyLogEntry[6]` (`DailyLogGoalsState[0]`, matches the budget the app shows), burn target = `DailyLogGoalsState[1]` (`CalorieBurnMetrics[1]`), food kcal = `DailyLogEntry[4]`. Deficit = burn − budget.
- **Weigh-ins:** `getDailyDetailsIncludingPendingForDateRange(token, Integer userId, from, to)` returns `DailyDetails[]`, where `[10]` is the day's `RecordedWeight`.

## Tests

`.venv/bin/pytest tests/ -q` → **240 passed**. `tests/conftest.py` isolates every test from the real `~/.config/loseit` and installs the schema fixture (`tests/fixtures/gwt_schema.json`, regenerate from live JS if LoseIt changes types). `test_api.py` runs log/edit/move/delete/copy against a fake server built from HAR fixtures and asserts on the decoded requests.

## Known gaps / non-goals

- **Weight logging:** not implemented. `saveRecordedWeight(token, double, DayDate)` exists, but there's no delete method, so it couldn't be tested safely.
- **Not supported yet:** exercise logging, custom foods, recipes, water and notes.
- **Session expiry:** the shape of an expired-session response hasn't been observed; HTTP 401/403 are mapped to the auth exit code.

## Next steps

1. Weight logging (`saveRecordedWeight`), once there's a safe way to undo it.
2. Exercise logging (`saveCustomExerciseLogEntry`, `updateExerciseLogEntry`, `deleteExerciseLogEntry`).
3. Recipes and custom foods if wanted.

## Dependencies

httpx, click, rich, browser-cookie3 (`--browser`), playwright (login window; uses installed Chrome via `channel="chrome"`). Python ≥3.10, uv + hatchling. Test fixtures were extracted from a recorded HAR (`scripts/extract_fixtures.py`) and scrubbed of account identifiers. HAR files hold live session cookies: `*.har` is gitignored; never commit one.
