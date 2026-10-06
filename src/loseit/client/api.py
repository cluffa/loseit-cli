"""LoseIt API client wrapping GWT-RPC calls.

Requests and responses are encoded/decoded with the schema extracted from
LoseIt's own JavaScript (see gwt_schema.py): `call(method, *params)` looks up
the method's parameter types, encodes the params, posts, and returns the
decoded result object. Field meanings are positional and documented where
they are used; they were verified against live data and the HAR fixtures.
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta

import httpx

from .gwt_schema import (
    GEnum, GObject, RequestEncoder, ResponseDecoder, RpcCall, box, load_schema,
)
from .parse import Item, default_meal, normalize_words, parse_item, query_variants
from .recent import REFRESH_DAYS, RecentFoods, _match_score, rank_search_results
from .session import SessionStore


def _hours_from_gmt() -> int:
    offset = datetime.now().astimezone().utcoffset()
    return int(offset.total_seconds() // 3600) if offset else 0


# FoodMeasurement enum ordinals (from the app's enum table)
_NUTRIENTS = {"calories": 0, "fat_g": 3, "saturated_fat_g": 4, "cholesterol_mg": 8,
              "sodium_mg": 9, "carbs_g": 10, "fiber_g": 11, "sugar_g": 12, "protein_g": 13}
# FoodLogEntryType enum ordinals
MEALS = ["breakfast", "lunch", "dinner", "snacks"]
# FoodMeasure enum, by ordinal: (display name, dimension, size in g or ml)
_MEASURES = [
    ("None", None, 0), ("Teaspoon", "vol", 4.92892), ("Tablespoon", "vol", 14.7868),
    ("Cup", "vol", 236.588), ("Piece", None, 0), ("Each", None, 0), ("Ounce", "wt", 28.3495),
    ("Pound", "wt", 453.592), ("Gram", "wt", 1.0), ("Kilogram", "wt", 1000.0),
    ("Fluid ounce", "vol", 29.5735), ("Milliliter", "vol", 1.0), ("Liter", "vol", 1000.0),
    ("Gallon", "vol", 3785.41), ("Pint", "vol", 473.176), ("Quart", "vol", 946.353),
    ("Milligram", "wt", 0.001), ("Microgram", "wt", 1e-6), ("Intake", None, 0),
    ("Bottle", None, 0), ("Box", None, 0), ("Can", None, 0), ("Cube", None, 0), ("Jar", None, 0),
    ("Stick", None, 0), ("Tablet", None, 0), ("Slice", None, 0), ("Serving", None, 0),
    ("300 Can", None, 0), ("303 Can", None, 0), ("401 Can", None, 0), ("404 Can", None, 0),
    ("Ind Package", None, 0), ("Scoop", None, 0), ("Metric Cup", "vol", 250.0),
    ("Dry Cup", None, 0), ("Imperial Fluid Ounce", None, 0), ("Imperial Gallon", None, 0),
    ("Imperial Quart", None, 0), ("Imperial Pint", None, 0), ("Tablespoon (AU)", "vol", 20.0),
    ("Dessertspoon", "vol", 10.0), ("Pot", None, 0), ("Punnet", None, 0), ("As Entered", None, 0),
    ("Container", None, 0), ("Package", None, 0), ("Pouch", None, 0),
]
_UNIT_ALIASES = {"g": "gram", "grams": "gram", "kg": "kilogram", "mg": "milligram",
                 "oz": "ounce", "ounces": "ounce", "lb": "pound", "lbs": "pound",
                 "ml": "milliliter", "l": "liter", "floz": "fluid ounce", "fl oz": "fluid ounce",
                 "tsp": "teaspoon", "tbsp": "tablespoon", "cups": "cup", "servings": "serving",
                 "pieces": "piece", "slices": "slice"}

_HEX_PREFIX = re.compile(r"^[0-9a-f]{4,32}$")

# A single entry above this gets a "check the amount" note
LARGE_ENTRY_CALORIES = 2000

# DayDate.day counts days from this epoch (day 9409 == 2026-10-05)
DAY_EPOCH = date(2000, 12, 31)


class LoseItError(RuntimeError):
    """The LoseIt server returned an exception."""


class PartialLogError(RuntimeError):
    """A multi-item log failed partway; `logged` lists what was saved."""

    def __init__(self, logged: list[dict], failed_input: str, cause: Exception):
        super().__init__(f"Logged {len(logged)} item(s), then failed on {failed_input!r}: {cause}")
        self.logged, self.failed_input, self.cause = logged, failed_input, cause


# ──────────────────────────────────────────────
# Value helpers
# ──────────────────────────────────────────────

def measure_name(measure) -> str:
    if measure is None:
        return ""
    return _MEASURES[measure.ordinal][0] if measure.ordinal < len(_MEASURES) else f"unit#{measure.ordinal}"


def find_measure(unit: str) -> int:
    """Ordinal of the FoodMeasure matching a user-supplied unit name."""
    u = unit.strip().lower()
    u = _UNIT_ALIASES.get(u, u)
    for i, (name, _, _) in enumerate(_MEASURES):
        if name.lower() in (u, u.rstrip("s")):
            return i
    raise ValueError(f"Unknown unit: {unit}")


def key_to_hex(pk: GObject) -> str:
    """SimplePrimaryKey([B]) -> 32-char hex id."""
    return bytes(b & 0xFF for b in pk.fields[0].fields[0]).hex()


def entry_nutrients(serving: GObject) -> dict[int, float]:
    """Total nutrients for a FoodServing, keyed by FoodMeasurement ordinal.

    FoodNutrients is ([0] servings the values are for, [1] servings logged,
    [2] values), so the logged total is value * [1] / [0].
    """
    base, servings, values = serving.fields[0].fields
    scale = servings / base if base else 1.0
    return {k.ordinal: v * scale for k, v in values.fields}


def _nutrient_dict(by_ordinal: dict[int, float]) -> dict[str, float]:
    return {k: round(by_ordinal.get(o, 0.0), 1) for k, o in _NUTRIENTS.items()}


def summarize_daily_details(details: GObject, target_date: date) -> dict:
    """Turn a decoded DailyDetails object into a summary dict.

    Field positions (verified against live data, 2026-10):
      DailyDetails: [3] DailyLogEntry, [4] ExerciseLogEntry[], [5] FoodLogEntry[],
                    [6] GoalsSummary
      DailyLogEntry: [4] food calories,
                     [6] DailyLogGoalsState([0] calorie budget,
                                            [1] CalorieBurnMetrics([1] daily burn target))
      ExerciseLogEntry: [1] calories burned
      FoodLogEntry: [0] FoodIdentifier([3] name, [4] brand, [9] food key),
                    [1] FoodLogEntryContext([8] meal enum),
                    [2] FoodServing([0] FoodNutrients([2] {FoodMeasurement: value}),
                                    [1] FoodServingSize([0] base servings, [2] unit, [5] amount)),
                    [6] entry key
      GoalsSummary: [6] current weight, [10] goal weight
    """
    f = details.fields
    log_entry, exercises, foods, goals = f[3], f[4], f[5], f[6]

    foods_out = []
    totals = {k: 0.0 for k in _NUTRIENTS}
    for entry in (foods.fields[0] if foods else []):
        ident, ctx, serving = entry.fields[0], entry.fields[1], entry.fields[2]
        nutrients = entry_nutrients(serving)
        meal = ctx.fields[8]
        foods_out.append({
            "entry_id": key_to_hex(entry.fields[6]),
            "food_id": key_to_hex(ident.fields[9]),
            "name": ident.fields[3],
            "brand": ident.fields[4] or "",
            "category": ident.fields[1] or "",
            "meal": MEALS[meal.ordinal] if meal and meal.ordinal < len(MEALS) else "other",
            "amount": round(serving.fields[1].fields[5], 2),
            "unit": measure_name(serving.fields[1].fields[2]),
            "calories": round(nutrients.get(_NUTRIENTS["calories"], 0.0), 1),
        })
        for key, ordinal in _NUTRIENTS.items():
            totals[key] += nutrients.get(ordinal, 0.0)

    exercise = float(sum(e.fields[1] for e in (exercises.fields[0] if exercises else [])))
    goals_state = log_entry.fields[6] if log_entry else None
    budget = goals_state.fields[0] if goals_state else 0.0
    burn = goals_state.fields[1].fields[1] if goals_state and goals_state.fields[1] else None
    consumed = log_entry.fields[4] if log_entry else totals["calories"]

    return {
        "date": target_date.isoformat(),
        "calorie_budget": round(budget, 1),
        "calorie_burn_target": round(burn, 1) if burn else None,
        "calorie_deficit": round(burn - budget, 1) if burn else None,
        "calories_consumed": round(consumed, 1),
        "exercise_calories": round(exercise, 1),
        "calories_remaining": round(budget + exercise - consumed, 1),
        "weight": goals.fields[6] if goals else None,
        "goal_weight": goals.fields[10] if goals else None,
        "nutrients": {k: round(v, 1) for k, v in totals.items() if k != "calories"},
        "foods": foods_out,
    }


def summarize_food(food: GObject) -> dict:
    """FoodForFoodDatabase: [0] FoodIdentifier, [1] FoodNutrients (per base serving),
    [2] FoodServingSize[] options, each "[4] <unit [2]> = [3] base servings"."""
    ident, nutrients, sizes = food.fields[0], food.fields[1], food.fields[2]
    per_base = {k.ordinal: v for k, v in nutrients.fields[2].fields}
    options = []
    for sz in sizes.fields[0]:
        options.append({
            "amount": round(sz.fields[4], 3),
            "unit": measure_name(sz.fields[2]),
            "calories": round(per_base.get(0, 0.0) * sz.fields[3], 1),
        })
    return {
        "food_id": key_to_hex(ident.fields[9]),
        "name": ident.fields[3],
        "brand": ident.fields[4] or "",
        "servings": options,
        "nutrients_per_serving": _nutrient_dict({k: v * sizes.fields[0][0].fields[3]
                                                 for k, v in per_base.items()}) if options else {},
    }


def build_serving_size(option: GObject, amount: float | None, unit: str | None,
                       servings: float | None) -> GObject:
    """Make the FoodServingSize to log from one of the food's serving options.

    An option reads "[4] of unit [2] = [3] base servings". The logged size keeps
    that ratio, sets [5] = amount entered and [0] = base servings. A different
    unit in the same dimension (weight/volume) is converted.
    """
    o = option.fields
    measure, per_amount = o[2], o[4]
    if unit is not None:
        target = find_measure(unit)
        if target != measure.ordinal:
            src, dst = _MEASURES[measure.ordinal], _MEASURES[target]
            if not src[1] or src[1] != dst[1]:
                raise ValueError(f"Can't convert {src[0]} to {dst[0]} for this food")
            per_amount = o[4] * src[2] / dst[2]
            measure = GEnum(measure.type, target, measure.signature)
    if amount is None:
        amount = per_amount * (servings if servings is not None else 1.0)
    base = amount / per_amount * o[3]
    return GObject(option.type, [base, o[1], measure, o[3], per_amount, amount], option.signature)


# ──────────────────────────────────────────────
# Client
# ──────────────────────────────────────────────

class LoseItAPI:
    """High-level LoseIt API client."""

    WEB_SERVICE_URL = "https://www.loseit.com/web/service"
    GWT_BASE_URL = "https://d3hsih69yn4d89.cloudfront.net/web/"
    SERVICE_CLASS = "com.loseit.core.client.service.LoseItRemoteService"
    LOCALE = "en-US"

    def __init__(self):
        session = SessionStore.load()
        if not session or not session.is_valid():
            raise RuntimeError("Not authenticated. Run 'loseit login' first.")
        self._session = session
        headers = {
            "Content-Type": "text/x-gwt-rpc; charset=UTF-8",
            # GWT's RemoteServiceServlet rejects calls without the permutation header
            "X-GWT-Module-Base": session.gwt_base_url,
            "X-GWT-Permutation": session.gwt_permutation,
        }
        if session.token:
            headers["Authorization"] = f"Bearer {session.token}"
        self._client = httpx.Client(timeout=30.0, headers=headers, cookies=session.cookies)
        self._schema: dict | None = None
        self._foods: dict[str, GObject | None] = {}   # getFood results by id

    # schema
    @property
    def schema(self) -> dict:
        if self._schema is None:
            self._schema = load_schema(self._client, self.GWT_BASE_URL, self._session.gwt_permutation)
        return self._schema

    def sig(self, name: str) -> str:
        """Full type signature for a class name ("UserId" or "java.util.Date")."""
        matches = [s for s in self.schema["types"]
                   if s.split("/")[0] == name or s.split("/")[0].endswith("." + name)]
        if len(matches) != 1:
            raise RuntimeError(f"Type {name!r} matches {len(matches)} signatures")
        return matches[0]

    # core
    def call(self, method: str, *params, overload: int | None = None):
        """Invoke a LoseItRemoteService method and return the decoded result."""
        overloads = [o for o in self.schema["methods"].get(method, []) if len(o) == len(params)]
        if not overloads:
            raise RuntimeError(f"Unknown method or arity: {method}/{len(params)}")
        types = overloads[overload or 0]
        call = RpcCall(method, types, list(params), self.GWT_BASE_URL,
                       self._session.policy_hash, self.SERVICE_CLASS)
        body = RequestEncoder(self.schema["types"]).encode(call)
        idempotent = method.startswith(("get", "search"))
        for attempt in range(3 if idempotent else 1):
            try:
                resp = self._client.post(self.WEB_SERVICE_URL, content=body)
                break
            except httpx.TimeoutException:
                if attempt == 2 or not idempotent:
                    raise
        resp.raise_for_status()
        text = resp.text
        if text.startswith("//EX"):
            raise LoseItError(_exception_message(text, method))
        if not text.startswith("//OK"):
            raise RuntimeError(f"Unexpected response from {method}: {text[:120]}")
        return ResponseDecoder(self.schema["types"]).decode(text).fields[3]

    # building blocks
    def token(self) -> GObject:
        """ServiceRequestToken(null, UserId(id, username, hoursFromGMT))."""
        user = GObject("UserId", [self._session.user_id, self._session.username, _hours_from_gmt()],
                       self.sig("UserId"))
        return GObject("ServiceRequestToken", [None, user], self.sig("ServiceRequestToken"))

    def day_date(self, d: date) -> GObject:
        """DayDate(java.util.Date at local midnight, days since DAY_EPOCH, hoursFromGMT)."""
        midnight = datetime.combine(d, time.min).astimezone()
        when = box(int(midnight.timestamp() * 1000), self.sig("java.util.Date"))
        return GObject("DayDate", [when, (d - DAY_EPOCH).days, _hours_from_gmt()], self.sig("DayDate"))

    def key(self, hex_id: str) -> GObject:
        try:
            raw = bytes.fromhex(hex_id)
        except ValueError:
            raise ValueError(f"Not a LoseIt id (expected 32 hex chars): {hex_id}") from None
        if len(raw) != 16:
            raise ValueError(f"Not a LoseIt id (expected 32 hex chars): {hex_id}")
        signed = [b - 256 if b > 127 else b for b in raw]
        return GObject("SimplePrimaryKey", [GObject("[B", [signed], self.sig("[B"))],
                       self.sig("SimplePrimaryKey"))

    # daily log
    def _daily_details(self, target_date: date) -> GObject:
        return self.call("getDailyDetailsIncludingPendingForDate", self.token(), self.day_date(target_date))

    def _day(self, target_date: date) -> tuple[GObject, dict]:
        """Fetch a day; returns (DailyDetails, summary) and feeds the recents index."""
        details = self._daily_details(target_date)
        summary = summarize_daily_details(details, target_date)
        recent = RecentFoods.load()
        recent.record_day(target_date.isoformat(), summary["foods"])
        recent.save()
        return details, summary

    def get_daily_summary(self, target_date: date | None = None) -> dict:
        return self._day(target_date or date.today())[1]

    @staticmethod
    def day_totals(summary: dict) -> dict:
        keys = ("date", "calorie_budget", "calorie_burn_target", "calorie_deficit", "calories_consumed", "exercise_calories", "calories_remaining")
        return {k: summary[k] for k in keys}

    # recents
    def recent_foods(self, query: str | None = None, limit: int = 20, refresh: bool = False) -> list[dict]:
        recent = self._recent(refresh)
        return recent.ranked(query, limit)

    def _recent(self, refresh: bool = False) -> RecentFoods:
        recent = RecentFoods.load()
        if refresh or recent.stale:
            end = date.today()
            for details in self._range(end - timedelta(days=REFRESH_DAYS - 1), end):
                day = DAY_EPOCH + timedelta(days=details.fields[3].fields[1].fields[1])
                recent.record_day(day.isoformat(), summarize_daily_details(details, day)["foods"])
            recent.mark_refreshed()
            recent.save()
        return recent

    # foods
    def search_foods(self, query: str, limit: int = 10, details: bool = True) -> list[dict]:
        """searchFoods(token, query, locale, maxResults, ?, ?) -> SearchResults.

        SearchResults: [0] list of SearchResultHeader / SearchResultFood /
        SearchResultMeal / SearchResult; SearchResultFood: [0] food key,
        [1] category, [3] name, [4] brand. Results carry no nutrition, so with
        `details` each hit's default serving and calories come from getFood
        (fetched in parallel). Hits are remembered so their id prefixes resolve.
        """
        results = self.call("searchFoods", self.token(), query, self.LOCALE, max(limit, 15), True, True)
        foods = []
        for item in results.fields[0].fields if results else []:
            if isinstance(item, GObject) and item.type == "SearchResultFood":
                foods.append({
                    "food_id": key_to_hex(item.fields[0]),
                    "name": item.fields[3],
                    "brand": item.fields[4] or "",
                    "category": item.fields[1] or "",
                })
        foods = foods[:limit]
        if details:
            self._add_serving_info(foods)
        recent = RecentFoods.load()
        recent.remember(foods)
        recent.save()
        return foods

    def _add_serving_info(self, foods: list[dict]) -> None:
        """Add the default serving ("1 Bottle") and its calories to food dicts."""
        self._prefetch([f["food_id"] for f in foods])
        for f in foods:
            food = self._foods.get(f["food_id"])
            options = summarize_food(food)["servings"] if food else []
            if options:
                f["serving"] = f"{options[0]['amount']:g} {options[0]['unit']}"
                f["calories"] = options[0]["calories"]

    def _food(self, food_id: str) -> GObject:
        if self._foods.get(food_id) is None:
            self._foods[food_id] = self.call("getFood", self.token(), self.key(food_id), None)
        return self._foods[food_id]

    def _prefetch(self, food_ids: list[str]) -> None:
        """getFood for several foods in parallel; failures are cached as None."""
        missing = [i for i in dict.fromkeys(food_ids) if i not in self._foods]
        if not missing:
            return
        self.schema, self.token()   # load the schema once, outside the threads

        def fetch(food_id):
            try:
                return self.call("getFood", self.token(), self.key(food_id), None)
            except (LoseItError, httpx.HTTPError, RuntimeError):
                return None
        with ThreadPoolExecutor(max_workers=8) as pool:
            for food_id, food in zip(missing, pool.map(fetch, missing)):
                self._foods[food_id] = food

    def get_food(self, food_id: str) -> dict:
        return summarize_food(self._food(self.resolve_food_id(food_id)))

    def resolve_food_id(self, text: str) -> str:
        """Full food id from a full id or a prefix of a recently logged food."""
        text = text.lower()
        if len(text) == 32:
            return text
        matches = RecentFoods.load().find_by_prefix(text)
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise LookupError(f"No recent or searched food with id prefix {text!r}; "
                              "use the full 32-char id")
        raise LookupError(f"Food id prefix {text!r} is ambiguous: {', '.join(matches)}")

    def resolve_item(self, item: Item) -> dict:
        """Pick the food for a quick-log item.

        Order: explicit id -> the user's recent foods (best match, then most
        recent, then most often) -> server search re-ranked by word match.
        With a calorie hint ("~95cal") recents and search hits are all
        candidates and the one closest to the hint for the amount wins.
        Returns {food_id, source, name, brand, amount, unit, alternatives};
        amount/unit default to what the user logged last time for a recent food.
        """
        if item.is_id:
            return {"food_id": self.resolve_food_id(item.query), "source": "id", "name": None,
                    "alternatives": [], "amount": item.amount, "unit": item.unit}
        candidates = []
        for r in self._recent().ranked(item.query, limit=4):
            amount, unit = item.amount, item.unit
            if amount is None and unit is None:
                amount, unit = r["last_amount"], r["last_unit"]
            candidates.append({"food_id": r["food_id"], "source": "recent", "name": r["name"],
                               "brand": r["brand"], "amount": amount, "unit": unit,
                               "calories": r["last_calories"] if (amount, unit) == (
                                   r["last_amount"], r["last_unit"]) else None})
        self._estimate_calories(candidates)
        if not any(c["fits"] for c in candidates) or item.calories is not None:
            hits = self._add_search_candidates(item.query, item, candidates)
            # Hint not met: try other spellings ("peanutbutter" -> "peanut butter")
            if item.calories is not None and not any(
                    c["fits"] and _near(c["calories"], item.calories) for c in candidates):
                for variant in query_variants(item.query, [f"{h['name']} {h['brand']}" for h in hits])[:2]:
                    self._add_search_candidates(variant, item, candidates)
        if not candidates:
            raise LookupError(f"No foods found for {item.query!r}")
        # Foods that can't take the requested unit ("1 can" of a grams-only food)
        # drop out. Mapping "can" onto a food's own Serving is fine for the best
        # name match, but shouldn't promote a different product ("Single Serve").
        fits = [c for c in candidates if c["fits"]]
        if item.calories is not None and fits:
            known_cal = [c for c in fits if c["calories"] is not None] or fits
            choice = min(known_cal, key=lambda c: abs((c["calories"] or 0) - item.calories))
        elif candidates[0]["fits"]:
            choice = candidates[0]
        elif any(c["fits"] and not c["fallback"] for c in candidates):
            choice = next(c for c in candidates if c["fits"] and not c["fallback"])
        else:
            choice = None
        if choice is not None and item.calories is not None and choice["fallback"] and not _near(
                choice["calories"], item.calories):
            choice = None   # a swapped unit that doesn't match the hint is a guess
        if choice is None:
            options = "; ".join(
                f"{c['name']} ({c['food_id'][:8]}: {', '.join(c.get('units') or ['?'])})"
                + (f" {c['calories']:g} cal per {c['unit_used']}" if c["fits"] else "")
                for c in candidates[:5])
            hint = "" if item.calories is not None else ", or a calorie hint ('~CAL cal')"
            raise ValueError(f"No match for {item.query!r} comes in '{item.unit}'"
                             + (f" at ~{item.calories:g} cal" if item.calories is not None else "")
                             + f". Matches: {options}. Give a weight/volume "
                             f"(e.g. '12 oz {item.query}') or a food id{hint}")
        out = {k: v for k, v in choice.items() if k not in ("fits", "fallback", "units", "unit_used")}
        out["alternatives"] = [_alt(c) for c in fits if c is not choice][:3]
        if item.calories is not None:
            if not _near(choice["calories"], item.calories):
                out["warning"] = (f"closest match is {choice['calories']:g} cal, not ~{item.calories:g}"
                                  if choice["calories"] is not None else "couldn't check calories")
        return out

    def _add_search_candidates(self, query: str, item: Item, candidates: list[dict]) -> list[dict]:
        """Search `query`, add the top 8 new hits (sized for `item`) to
        `candidates`, and return the raw hits."""
        hits = rank_search_results(query, self.search_foods(query, limit=15, details=False))
        known = {c["food_id"] for c in candidates}
        found = [{"food_id": h["food_id"], "source": "search", "name": h["name"],
                  "brand": h["brand"], "amount": item.amount, "unit": item.unit, "calories": None}
                 for h in hits[:8] if h["food_id"] not in known]
        self._estimate_calories(found)
        candidates += found
        return hits

    def _estimate_calories(self, candidates: list[dict]) -> None:
        """Fill in calories for the amount/unit each candidate would be logged at,
        and whether that amount/unit works for the food at all ("fits")."""
        for c in candidates:
            c.update(fits=True, fallback=False)
        todo = [c for c in candidates if c["calories"] is None]
        self._prefetch([c["food_id"] for c in todo])
        for c in todo:
            food = self._foods.get(c["food_id"])
            if food is None:
                continue
            c["units"] = [measure_name(o.fields[2]) for o in food.fields[2].fields[0]]
            try:
                amount, unit, servings = _size_args(c["amount"], c["unit"])
                size, note = self._serving_for(food, amount, unit, servings)
                c.update(calories=_calories(food, size), fallback=note is not None,
                         unit_used=measure_name(size.fields[2]))
            except ValueError:
                c["fits"] = False

    def _serving_for(self, food: GObject, amount: float | None, unit: str | None,
                     servings: float | None = None) -> tuple[GObject, str | None]:
        """Size a serving of `food`: `amount` in `unit` (default: the food's first
        serving size's unit), or `servings` of that serving size.

        A count unit the food doesn't have ("can", "each", "slice") falls back to
        the food's own count unit ("Serving", "Bottle"); the note says so.
        Returns (FoodServingSize, note or None).
        """
        options = food.fields[2].fields[0]
        if not options:
            raise RuntimeError("This food has no serving sizes")
        option, note = options[0], None
        if unit is not None:
            wanted = find_measure(unit)
            dim = _MEASURES[wanted][1]
            option = next((o for o in options if o.fields[2].ordinal == wanted), None) or next(
                (o for o in options if dim and _MEASURES[o.fields[2].ordinal][1] == dim), None)
            if option is None and not dim:
                option = next((o for o in options if not _MEASURES[o.fields[2].ordinal][1]), None)
                if option is not None:
                    note = f"no '{unit}' unit for this food; used {measure_name(option.fields[2])}"
                    unit = None
            if option is None:
                units = ", ".join(measure_name(o.fields[2]) for o in options)
                raise ValueError(f"Unit '{unit}' not available for {food.fields[0].fields[3]} (try: {units})")
        return build_serving_size(option, amount, unit, servings), note

    def _apply_serving(self, entry: GObject, size: GObject) -> None:
        serving = entry.fields[2]
        serving.fields[0].fields[1] = size.fields[0]   # servings logged
        serving.fields[1] = size

    def _meal_enum(self, meal: str) -> GEnum:
        if meal not in MEALS:
            raise ValueError(f"Meal must be one of {', '.join(MEALS)}")
        return GEnum("FoodLogEntryType", MEALS.index(meal), self.sig("FoodLogEntryType"))

    def _save_entry(self, entry: GObject) -> None:
        overload = self._single_entry_overload("updateFoodLogEntry")
        self.call("updateFoodLogEntry", self.token(), entry, overload=overload)

    def _plan(self, food_id: str, amount: float | None, unit: str | None,
              servings: float | None, meal: str) -> dict:
        """Resolve everything needed to log a food, without writing."""
        self._meal_enum(meal)
        food = self._food(food_id)
        size, note = self._serving_for(food, amount, unit, servings)
        calories = _calories(food, size)
        notes = [note] if note else []
        if calories > LARGE_ENTRY_CALORIES:
            notes.append(f"unusually large: {calories:g} cal for {size.fields[5]:g} "
                         f"{measure_name(size.fields[2])}; check the amount")
        return {"food_id": food_id, "food": food, "size": size, "meal": meal,
                "calories": calories, "notes": notes}

    def _commit(self, plan: dict, target_date: date) -> dict:
        food = plan["food"]
        entry = self.call("getUnsavedFoodLogEntry", self.token(), self.key(plan["food_id"]), None,
                          food.fields[0].fields[3])
        ctx = entry.fields[1]
        ctx.fields[1] = self.day_date(target_date)
        ctx.fields[4] = -1
        ctx.fields[8] = self._meal_enum(plan["meal"])
        self._apply_serving(entry, plan["size"])
        self._save_entry(entry)
        return _entry_result(entry, target_date, plan["meal"], plan["food_id"])

    def log_food(self, food_id: str, amount: float | None = None, unit: str | None = None,
                 servings: float | None = None, meal_type: str = "breakfast",
                 target_date: date | None = None) -> dict:
        """Log one food the way the web app does: getFood (serving options),
        getUnsavedFoodLogEntry (server-prefilled entry with a fresh key), set
        day/meal/serving, updateFoodLogEntry."""
        plan = self._plan(self.resolve_food_id(food_id), amount, unit, servings, meal_type)
        return self._commit(plan, target_date or date.today())

    def plan_items(self, items: list[Item], meal: str | None, target_date: date) -> list[dict]:
        """Resolve, size and validate every item before anything is written."""
        default = meal or (default_meal(datetime.now().hour) if target_date == date.today() else None)
        plans = []
        for item in items:
            choice = self.resolve_item(item)
            item_meal = item.meal or meal or default
            if item_meal is None:
                # Another day and no meal given: use the food's usual meal
                usual = next((r for r in RecentFoods.load().ranked(None, 1000)
                              if r["food_id"] == choice["food_id"]), None)
                item_meal = usual["last_meal"] if usual else "snacks"
            plan = self._plan(choice["food_id"], *_size_args(choice["amount"], choice["unit"]), item_meal)
            if choice.get("warning"):
                plan["notes"].append(choice["warning"])
            plan.update(input=item.raw or item.query, matched_by=choice["source"],
                        alternatives=choice["alternatives"])
            plans.append(plan)
        return plans

    @staticmethod
    def describe_plan(plan: dict) -> dict:
        size, ident = plan["size"], plan["food"].fields[0]
        out = {"input": plan["input"], "matched_by": plan["matched_by"], "food_id": plan["food_id"],
               "name": ident.fields[3], "brand": ident.fields[4] or "",
               "amount": round(size.fields[5], 2), "unit": measure_name(size.fields[2]),
               "calories": plan["calories"], "meal": plan["meal"]}
        if plan["alternatives"]:
            out["alternatives"] = plan["alternatives"]
        if plan["notes"]:
            out["notes"] = plan["notes"]
        return out

    def log_items(self, items: list[Item], meal: str | None, target_date: date) -> dict:
        """Log several quick-log items, then return them with the day's totals.

        All items are planned first, so a bad item fails before anything is
        logged. If a write fails midway, PartialLogError carries what was logged.
        """
        plans = self.plan_items(items, meal, target_date)
        logged = []
        for plan in plans:
            try:
                result = self._commit(plan, target_date)
            except Exception as e:
                raise PartialLogError(logged, plan["input"], e) from e
            result.update(input=plan["input"], matched_by=plan["matched_by"])
            for key in ("alternatives", "notes"):
                if plan[key]:
                    result[key] = plan[key]
            logged.append(result)
        summary = self._day(target_date)[1]
        return {"logged": logged, "day": self.day_totals(summary)}

    def _find_entries(self, details: GObject, refs: list[str], meal: str | None = None) -> list[GObject]:
        """Entries by id prefix (hex) or by food name words, e.g. "bagel"."""
        foods = details.fields[5].fields[0] if details.fields[5] else []
        if meal:
            foods = [e for e in foods if MEALS[e.fields[1].fields[8].ordinal] == meal]
        found = []
        for ref in refs:
            ref_l = ref.lower()
            if _HEX_PREFIX.match(ref_l):
                matches = [e for e in foods if key_to_hex(e.fields[6]).startswith(ref_l)]
            else:
                scored = [(_match_score(normalize_words(ref), {"name": e.fields[0].fields[3],
                                                              "brand": e.fields[0].fields[4] or "",
                                                              "category": e.fields[0].fields[1] or ""}), e)
                          for e in foods]
                best = max((sc for sc, _ in scored), default=0)
                matches = [e for sc, e in scored if sc == best and sc > 0]
            if not matches:
                raise LookupError(f"No food entry matching {ref!r} on that day")
            if len(matches) > 1:
                options = ", ".join(f"{key_to_hex(e.fields[6])[:8]} {e.fields[0].fields[3]}" for e in matches)
                raise LookupError(f"{ref!r} matches {len(matches)} entries: {options}")
            found.append(matches[0])
        return found

    def edit_entry(self, entry_id: str, target_date: date, amount: float | None = None,
                   unit: str | None = None, servings: float | None = None,
                   meal: str | None = None, move_to: date | None = None,
                   food: str | None = None) -> dict:
        """Change a logged food's amount/unit, meal or day, or swap the food.
        Keeps the entry id, except when moving to another day or swapping the
        food (those create a new entry and delete the old one)."""
        details = self._daily_details(target_date)
        entry = self._find_entries(details, [entry_id])[0]
        if food is not None:
            return self._replace_food(entry, food, target_date, amount, unit, servings, meal, move_to)
        if amount is not None or unit is not None or servings is not None:
            current = entry.fields[2].fields[1]
            if unit is None and servings is None:
                # Same unit as logged; amount is in that unit
                size = build_serving_size(current, amount, measure_name(current.fields[2]), None)
            elif unit is not None and _convertible(current, unit):
                size = build_serving_size(current, amount, unit, servings)
            else:
                food = self._food(key_to_hex(entry.fields[0].fields[9]))
                size, _ = self._serving_for(food, amount, unit, servings)
            self._apply_serving(entry, size)
        if meal is not None:
            entry.fields[1].fields[8] = self._meal_enum(meal)
        if move_to is not None and move_to != target_date:
            # Changing an entry's day in place leaves it listed on the old day,
            # so a move is: save a fresh entry on the new day, then delete the old.
            food_id = key_to_hex(entry.fields[0].fields[9])
            moved = self.call("getUnsavedFoodLogEntry", self.token(), self.key(food_id), None,
                              entry.fields[0].fields[3])
            moved.fields[1].fields[1] = self.day_date(move_to)
            moved.fields[1].fields[4] = -1
            moved.fields[1].fields[8] = entry.fields[1].fields[8]
            moved.fields[2] = entry.fields[2]
            self._save_entry(moved)
            self.call("deleteFoodLogEntry", self.token(), entry)
            entry = moved
        else:
            move_to = None
            self._save_entry(entry)
        day = move_to or target_date
        result = _entry_result(entry, day, MEALS[entry.fields[1].fields[8].ordinal],
                               key_to_hex(entry.fields[0].fields[9]))
        result["status"] = "updated"
        out = {"updated": result, "day": self.day_totals(self._day(day)[1])}
        if move_to is not None:
            out["from_day"] = self.day_totals(self._day(target_date)[1])
        return out

    def _replace_food(self, entry: GObject, food: str, target_date: date, amount: float | None,
                      unit: str | None, servings: float | None, meal: str | None,
                      move_to: date | None) -> dict:
        """Log `food` in place of `entry` (same meal/day and amount unless given),
        then delete the old entry. `food` is quick-log text or a food id."""
        item = parse_item(food)
        if amount is not None or unit is not None or servings is not None:
            item.amount, item.unit = (servings, None) if servings is not None else (amount, unit)
        elif item.amount is None and item.unit is None:
            current = entry.fields[2].fields[1]
            item.amount, item.unit = round(current.fields[5], 4), measure_name(current.fields[2])
        choice = self.resolve_item(item)
        day = move_to or target_date
        plan = self._plan(choice["food_id"], *_size_args(choice["amount"], choice["unit"]),
                          meal or MEALS[entry.fields[1].fields[8].ordinal])
        result = self._commit(plan, day)
        self.call("deleteFoodLogEntry", self.token(), entry)
        result.update(status="replaced", matched_by=choice["source"],
                      replaced={"entry_id": key_to_hex(entry.fields[6]), "name": entry.fields[0].fields[3]})
        if choice.get("warning"):
            plan["notes"].append(choice["warning"])
        for key, value in (("alternatives", choice["alternatives"]), ("notes", plan["notes"])):
            if value:
                result[key] = value
        out = {"updated": result, "day": self.day_totals(self._day(day)[1])}
        if day != target_date:
            out["from_day"] = self.day_totals(self._day(target_date)[1])
        return out

    def delete_entries(self, target_date: date, entry_ids: list[str] | None = None,
                       all_entries: bool = False, meal: str | None = None) -> dict:
        """Delete entries by id prefix or food name, or all for the day.
        `meal` narrows either (e.g. the "carrots" at dinner)."""
        details = self._daily_details(target_date)
        if all_entries:
            entries = details.fields[5].fields[0] if details.fields[5] else []
            if meal:
                self._meal_enum(meal)
                entries = [e for e in entries if MEALS[e.fields[1].fields[8].ordinal] == meal]
        else:
            entries = self._find_entries(details, entry_ids or [], meal)
        deleted = []
        for e in entries:
            self.call("deleteFoodLogEntry", self.token(), e)
            deleted.append({"entry_id": key_to_hex(e.fields[6]), "name": e.fields[0].fields[3],
                            "meal": MEALS[e.fields[1].fields[8].ordinal]})
        return {"deleted": deleted, "day": self.day_totals(self._day(target_date)[1])}

    def copy_entries(self, from_date: date, to_date: date, meal: str | None = None,
                     to_meal: str | None = None) -> dict:
        """Copy a day's (or one meal's) foods to another day/meal, like the app."""
        details = self._daily_details(from_date)
        entries = details.fields[5].fields[0] if details.fields[5] else []
        if meal:
            self._meal_enum(meal)
            entries = [e for e in entries if MEALS[e.fields[1].fields[8].ordinal] == meal]
        if not entries:
            raise LookupError(f"Nothing logged{' for ' + meal if meal else ''} on {from_date}")
        if to_meal:
            self._meal_enum(to_meal)
        copied = []
        for e in entries:
            food_id = key_to_hex(e.fields[0].fields[9])
            new = self.call("getUnsavedFoodLogEntry", self.token(), self.key(food_id), None,
                            e.fields[0].fields[3])
            new.fields[1].fields[1] = self.day_date(to_date)
            new.fields[1].fields[4] = -1
            new.fields[1].fields[8] = self._meal_enum(to_meal) if to_meal else e.fields[1].fields[8]
            new.fields[2] = e.fields[2]
            self._save_entry(new)
            copied.append(_entry_result(new, to_date, MEALS[new.fields[1].fields[8].ordinal], food_id))
        return {"copied": copied, "day": self.day_totals(self._day(to_date)[1])}

    def delete_food_log_entry(self, entry_id: str, target_date: date | None = None) -> dict:
        return self.delete_entries(target_date or date.today(), [entry_id])

    def _single_entry_overload(self, method: str) -> int:
        for i, types in enumerate(self.schema["methods"][method]):
            if not types[1].startswith("[L"):
                return i
        raise RuntimeError(f"No single-entry overload for {method}")

    # ranges
    RANGE_WINDOW_DAYS = 60

    def _range(self, start: date, end: date) -> list[GObject]:
        """DailyDetails for each day in [start, end], fetched in windows."""
        user = box(self._session.user_id, self.sig("java.lang.Integer"))
        out = []
        window_start = start
        while window_start <= end:
            window_end = min(window_start + timedelta(days=self.RANGE_WINDOW_DAYS - 1), end)
            result = self.call("getDailyDetailsIncludingPendingForDateRange", self.token(), user,
                               self.day_date(window_start), self.day_date(window_end))
            out.extend(result.fields[0] if result else [])
            window_start = window_end + timedelta(days=1)
        return out

    # weight
    def get_weight_history(self, days: int = 30, end: date | None = None) -> list[dict]:
        """Recorded weigh-ins, oldest first.

        getDailyDetailsIncludingPendingForDateRange(token, Integer userId, from, to)
        returns DailyDetails[]; [10] is that day's RecordedWeight or null:
        RecordedWeight([0] DayDate, [1] ?, [2] recorded-at ms, [3] weight).
        """
        end = end or date.today()
        out = []
        for details in self._range(end - timedelta(days=days - 1), end):
            rw = details.fields[10]
            if isinstance(rw, GObject) and rw.type == "RecordedWeight":
                out.append({
                    "date": (DAY_EPOCH + timedelta(days=rw.fields[0].fields[1])).isoformat(),
                    "weight": round(rw.fields[3], 1),
                })
        return out

def _alt(r: dict) -> dict:
    return {k: r[k] for k in ("food_id", "name", "brand", "calories") if r.get(k) is not None}


def _near(calories: float | None, hint: float) -> bool:
    """Is `calories` within 10% (or 10 cal) of a calorie hint?"""
    return calories is not None and abs(calories - hint) <= max(10.0, 0.10 * hint)


def _size_args(amount: float | None, unit: str | None) -> tuple:
    """(amount, unit, servings) for _serving_for: a bare number counts servings."""
    return (None, None, amount) if unit is None else (amount, unit, None)


def _calories(food: GObject, size: GObject) -> float:
    """Calories of `size` (a FoodServingSize) of `food`."""
    per = {k.ordinal: v for k, v in food.fields[1].fields[2].fields}
    return round(per.get(0, 0.0) * size.fields[0] / (food.fields[1].fields[0] or 1.0), 1)


def _convertible(size: GObject, unit: str) -> bool:
    dim = _MEASURES[size.fields[2].ordinal][1]
    return find_measure(unit) == size.fields[2].ordinal or bool(dim and _MEASURES[find_measure(unit)][1] == dim)


def _entry_result(entry: GObject, day: date, meal: str, food_id: str) -> dict:
    size = entry.fields[2].fields[1]
    totals = entry_nutrients(entry.fields[2])
    return {
        "status": "logged",
        "entry_id": key_to_hex(entry.fields[6]),
        "food_id": food_id,
        "name": entry.fields[0].fields[3],
        "brand": entry.fields[0].fields[4] or "",
        "amount": round(size.fields[5], 2),
        "unit": measure_name(size.fields[2]),
        "calories": round(totals.get(0, 0.0), 1),
        "meal": meal,
        "date": day.isoformat(),
    }


def _exception_message(text: str, method: str) -> str:
    try:
        arr = json.loads(text[4:])
        strings = arr[-3]
        exc = next((s for s in strings if "Exception" in s or "Error" in s), "exception")
        detail = next((s for s in strings if s is not exc and "/" not in s), "")
        return f"LoseIt rejected {method}: {exc.split('/')[0].split('.')[-1]}" + (f" — {detail}" if detail else "")
    except (ValueError, IndexError):
        return f"LoseIt rejected {method}: {text[:160]}"
