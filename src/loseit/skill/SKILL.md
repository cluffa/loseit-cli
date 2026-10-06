---
name: loseit
description: Log foods and check calories in the user's Lose It! (LoseIt.com) food diary with the `loseit` CLI. Use when the user mentions eating or drinking something, wants a meal logged, edited, moved or deleted, or asks about calories, their budget, deficit, macros or weight.
---

# LoseIt food logging

The `loseit` CLI reads and writes the user's Lose It! diary. When it's piped, output is JSON. Errors go to stderr as `{"error": {"type", "message"}}` with these exit codes: 2 invalid, 3 not logged in, 4 not found or ambiguous, 5 rejected by LoseIt, 6 network. Dates are `today|yesterday|tomorrow|±N|YYYY-MM-DD`.

**Setup check:** if a command exits 3, ask the user to run `loseit login` in a terminal. It opens a browser window; you can't do it for them.

## Logging

Log everything the user mentioned in **one** call. Put the meal, amount and any calorie number they gave into each item:

```bash
loseit log --date yesterday \
  "dinner: 6 oz chicken breast" \
  "dinner: 2 slices whole wheat bread" \
  "dinner: 1 slice cheddar cheese" \
  "dinner: 2 tbsp ranch dressing" \
  "snacks: 1 can cola ~140cal"
```

- **Calorie hint:** `~95cal` (also `(95 cal)`, `@95kcal`, or `95 calories` at the end). The user's recent foods and the top search hits are all candidates, and the one closest to the hint for that amount wins. Whenever the user says how many calories something has, pass it. It's the best protection against regular vs. light vs. zero-sugar mix-ups (a regular cola is ~140 cal, a diet one ~0) and bad database entries.
- **No amount:** a recent food is logged at the amount the user used last time.
- **Count units** (`can`, `bottle`, `each`, `slice`): these map onto the best match's own unit ("1 Serving"), with a note. They never pull in a different product. If nothing comes in that unit, the error lists the matches and their units; retry with a weight (`5 oz tuna`) or a calorie hint.
- **Meal:** if no meal is given, today's items are placed by time of day, and other days use the food's usual meal. After midnight, "dinner" usually means yesterday, so pass `--date yesterday`.
- **Preview:** `--dry-run` shows what would be logged without writing anything.

## Reading the result

Each item in `logged` has `name`, `amount`, `unit`, `calories`, `meal` and `entry_id`. It may also have:

- `notes`: a unit was swapped, the hint didn't match, or the entry is unusually large. Mention these to the user.
- `alternatives`: other candidates, with `calories` and `food_id`.

`day` holds the updated totals: `calorie_budget` (what the app shows), `calories_consumed`, `calories_remaining` (negative means over), `calorie_burn_target` and `calorie_deficit` (burn − budget).

Reply briefly: what was logged, with calories, then the day's total versus the budget. If you had to choose between variants (light/regular, brand), say which one you picked.

## Fixing entries

ENTRY is the food's name (`"ranch dressing"`) or an entry id prefix. Add `--date` for days other than today.

```bash
loseit edit "ranch dressing" --amount 1 --date yesterday             # same unit as logged
loseit edit bread --food "sourdough bread" --date yesterday          # swap food, keep meal + amount
loseit edit bread --food 1a2b3c4d --date yesterday                   # swap to an alternative's id
loseit edit coffee --meal breakfast        # or --move-to today
loseit delete cookies --date yesterday     # or --all [--meal dinner]
loseit copy --from yesterday --meal breakfast   # repeat a meal today
```

## Looking things up

- `loseit status [--date D]` shows the day's totals; `loseit summary [--date D]` adds the entries with their ids.
- `loseit search "greek yogurt"` returns results with a default `serving` and `calories`.
- `loseit recent chicken` lists the user's own foods, with the last amount and calories.
- `loseit food <id>` shows every serving size and the full nutrients.
- `loseit weight [--days N]` lists weigh-ins.
- Ids from `recent` or `search` output work as 8-character prefixes everywhere.

## Caveats

- LoseIt's food database includes user-entered errors (for example "2 slices = 90 cal" filed as 1 slice). If a number looks off, check `alternatives` or ask the user.
- Exercise, weight logging, recipes and custom foods aren't supported yet.
