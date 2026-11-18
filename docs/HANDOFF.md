# HANDOFF — loseit-cli

**Last updated:** 2026-11-18 · **Branch:** `main` · public repo

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
| `recipes [QUERY] [--all]`, `recipe REF [--all]` | list recipes (name, servings, cal/serving, recipe id, `current`, `created`) / show one (ingredients, instructions, per-serving nutrients). Like the app, listing and name resolution default to current versions only; `--all` includes superseded versions. REF = name (best match among current) or recipe id prefix (an id reaches any version). A name matching only retired versions errors and points at `--all`. |
| `recipe-create NAME --servings N [--instructions "..."] INGREDIENT...` | create via `saveRecipe`; ingredients are log-style items resolved like `log`. New recipes are counted in Servings and flagged current. |
| `recipe-delete REF [--all]` | delete via `removeRecipe` (no confirmation, like `delete`); same current-first resolution as `recipe`. Recipes can also be logged: `log` by recipe food id (prefix ok once seen) sizes them from the recipe itself. |
| `completion [zsh\|bash\|fish]` | shell completion script (Click); completers in `main.py` read only the local recents index (foods, entries on `--date`, date words). Enable with `eval "$(loseit completion zsh)"` in ~/.zshrc. |
| `login [--browser X] [--password] [--cookies F] [--export] [--import S]` | `--password`: headless HTTP login from LOSEIT_EMAIL/LOSEIT_PASSWORD. `--cookies`: import a browser cookie export. `LOSEIT_SESSION` env var = exported session for headless agents. |

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

- **Auth:** cookies (`liauth`, `fn_auth`, `JSESSIONID`, …) plus `X-GWT-Permutation`. Login is `POST api.loseit.com/account/login` (form: `grant_type=password`, `username`, `password`). The web form adds a reCAPTCHA Enterprise token, but the API answers without one (an unknown account gets `404 invalid_grant NOT_FOUND`). `liauth`/`fn_auth` hold the same JWT, with `exp` set 14 days after login; it isn't extended by use. A successful login (verified 2026-10-21) returns 200 with tokens only in Set-Cookie (`liauth`, `fn_auth`, `fn_authed`, `Max-Age=1209600`) and JSON `{user_id, username}`. There's no refresh or access token, so the `grant_type=refresh_token` path, which takes `access_token` + `refresh_token`, stays dormant. Session checks load `/index.jsp`: `/` is now an edge-cached marketing page with no embedded user data, whatever the cookies are. `UserAuthenticationFailedException` → `LoseItAuthError` → exit 3.
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
- **Recipes (verified live 2026-11, create + delete round-trip):** `getRecipes(token)` returns the `Recipe[]` array object (its `[0]` is the list). `Recipe`: `[0]` ActiveFood (`[1]` FoodIdentifier with category "Recipe" and the food key at `[9]`; `[2]` FoodServing whose FoodNutrients are the WHOLE recipe's totals, base = logged = 1), `[4]` servings the recipe makes (counted in the `[9]` FoodMeasure), `[6]` RecipeIngredient array, `[8]` instructions, `[10]` serving size amount (boxed), `[11]` serving size measure, `[13]` created ms, `[14]` recipe key (== food key). Per-serving nutrients = totals / `[4]`. `RecipeIngredient`: `[1]` FoodIdentifier, `[2]` FoodServing for the amount used, `[6]` owning recipe's key, `[7]` food's created ms, `[8]` own key. `saveRecipe(token, Recipe)`: the server rejects null keys — give the recipe a fresh random key shared by FoodIdentifier/ActiveFood/`[14]`, each ingredient its own key with `[6]` pointing back. `removeRecipe(token, Recipe[])` takes the array GObject (`fields = [[recipe]]`), not a Python list. Recipe foods have NO `getFood` result (it returns null), but `getUnsavedFoodLogEntry` accepts recipe keys, so logging works: `api._recipe_food` synthesizes a FoodForFoodDatabase from the recipe (one option, "1 \<serving unit\>", per-serving nutrients) for sizing. Nutrient map values must stay BoxedFloat with their `java.lang.Double` signature or the encoder fails.
- **Recipe versioning (verified live 2026-11):** LoseIt never edits a recipe in place — every edit saves a NEW Recipe object, and all versions stay on the server (`getRecipes` returns every version ever saved; Alex's account: 302 objects). Versions of a recipe are linked ONLY by sharing a name — no version number, no parent pointer. The current-version marker is ActiveFood `[6]` and `[7]` (bools, always equal): exactly 130 of Alex's 302 are current, matching the app's recipe list, which shows only current versions. `Recipe[13]` is each version's creation time (epoch ms, mirrored at ActiveFood `[8]` / FoodIdentifier `[8]`); summaries expose it as `created` (UTC date) plus a `current` bool. Quirks: 14 names have 2–3 versions flagged current at once, 9 names have NO current version (retired in the app but still on the server, still reachable/loggable by id), and the newest version is the current one in most but not all name groups. CLI behavior mirrors the app: `recipes`/`recipe`/`recipe-delete` default to current versions, `--all` covers everything (current preferred on ties), name resolution against only-retired matches errors with a pointer to `--all`, and an explicit id prefix reaches any version. `create_recipe` sets ActiveFood `[6]`/`[7]` explicitly — its template may be a superseded version with them cleared. ActiveFood `[5]` is an unidentified int (it climbs with reuse but is NOT a version index); don't surface it as one.

## Tests

`.venv/bin/pytest tests/ -q` → **297 passed**. `tests/conftest.py` isolates every test from the real `~/.config/loseit` and installs the schema fixture (`tests/fixtures/gwt_schema.json`, regenerate from live JS if LoseIt changes types). `test_api.py` runs log/edit/move/delete/copy against a fake server built from HAR fixtures and asserts on the decoded requests.

## Known gaps / non-goals

- **Weight logging:** not implemented. `saveRecordedWeight(token, double, DayDate)` exists, but there's no delete method, so it couldn't be tested safely.
- **Not supported yet:** exercise logging, custom foods, water and notes.
- **Recipe create from scratch:** `create_recipe` models the new Recipe on an existing one (opaque fields inherited). An account with no recipes at all takes a from-scratch path with the same layout — unverified, since it can't be tested without deleting the user's recipes.
- **Session expiry:** the shape of an expired-session response hasn't been observed; HTTP 401/403 are mapped to the auth exit code.

## Next steps

1. Weight logging (`saveRecordedWeight`), once there's a safe way to undo it.
2. Exercise logging (`saveCustomExerciseLogEntry`, `updateExerciseLogEntry`, `deleteExerciseLogEntry`).
3. Custom foods if wanted.

## Dependencies

httpx, click, rich, browser-cookie3 (`--browser`), playwright (login window; uses installed Chrome via `channel="chrome"`). Python ≥3.10, uv + hatchling. Test fixtures were extracted from a recorded HAR (`scripts/extract_fixtures.py`) and scrubbed of account identifiers. HAR files hold live session cookies: `*.har` is gitignored; never commit one.
