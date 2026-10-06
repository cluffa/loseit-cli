"""Parse quick-log item text like "lunch: 150g chicken breast" or "2 eggs"."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from fractions import Fraction

MEAL_WORDS = {"breakfast": "breakfast", "lunch": "lunch", "dinner": "dinner",
              "snack": "snacks", "snacks": "snacks"}

# Unit words accepted in item text -> canonical unit name (see api.find_measure)
UNIT_WORDS = {
    "g": "gram", "gram": "gram", "grams": "gram", "gr": "gram",
    "kg": "kilogram", "kilogram": "kilogram", "kilograms": "kilogram",
    "mg": "milligram", "milligram": "milligram", "milligrams": "milligram",
    "oz": "ounce", "ounce": "ounce", "ounces": "ounce",
    "lb": "pound", "lbs": "pound", "pound": "pound", "pounds": "pound",
    "ml": "milliliter", "milliliter": "milliliter", "milliliters": "milliliter",
    "l": "liter", "liter": "liter", "liters": "liter",
    "floz": "fluid ounce", "fl oz": "fluid ounce", "fluid ounce": "fluid ounce",
    "fluid ounces": "fluid ounce",
    "tsp": "teaspoon", "teaspoon": "teaspoon", "teaspoons": "teaspoon",
    "tbsp": "tablespoon", "tablespoon": "tablespoon", "tablespoons": "tablespoon",
    "cup": "cup", "cups": "cup",
    "serving": "serving", "servings": "serving",
    "piece": "piece", "pieces": "piece", "slice": "slice", "slices": "slice",
    "each": "each", "scoop": "scoop", "scoops": "scoop",
    "can": "can", "cans": "can", "bottle": "bottle", "bottles": "bottle",
    "package": "package", "packages": "package", "container": "container",
}

_NUM = r"(?:\d+\s+\d+/\d+|\d+/\d+|\d*\.\d+|\d+)"
_HEX_ID = re.compile(r"^[0-9a-f]{6,32}$")
# Calorie hint: marked anywhere ("~95cal", "(95 cal)", "@95kcal") or unmarked at
# the end ("bagel 250 calories"). Unmarked mid-text it's a name ("45 calorie bread").
_CAL_WORD = r"(?:k?cals?|kcals?|calories|calorie)\b"
_CAL_HINT = re.compile(
    rf"[\s,]*(?:\(\s*~?\s*(\d+(?:\.\d+)?)\s*{_CAL_WORD}\s*\)"
    rf"|[~@≈]\s*(\d+(?:\.\d+)?)\s*{_CAL_WORD}"
    rf"|(?<=\s)(\d+(?:\.\d+)?)\s*{_CAL_WORD}\s*$)",
    re.IGNORECASE)
_NAME_WORDS_AFTER_NUMBER = {"calorie", "calories", "cal", "kcal"}


@dataclass
class Item:
    """One food to log. amount is in `unit` if set, else a count of servings.
    `calories` is an optional hint ("~95cal") used to pick between candidates."""
    query: str
    amount: float | None = None
    unit: str | None = None
    meal: str | None = None
    raw: str = ""
    calories: float | None = None

    @property
    def is_id(self) -> bool:
        return bool(_HEX_ID.match(self.query))


def _number(text: str) -> float:
    text = text.strip()
    if " " in text:
        whole, frac = text.split()
        return float(int(whole) + Fraction(frac))
    return float(Fraction(text))


def _take_unit(words: list[str]) -> tuple[str | None, int]:
    """Match a unit at the start of words; returns (unit, words consumed)."""
    for n in (2, 1):
        if len(words) >= n:
            cand = " ".join(words[:n]).lower().rstrip(".")
            if cand in UNIT_WORDS:
                return UNIT_WORDS[cand], n
    return None, 0


def _hex_or_text(words: list[str]) -> str:
    text = " ".join(words)
    return text.lower() if _HEX_ID.match(text.lower()) else text


def parse_item(text: str) -> Item:
    calories = None
    hint = _CAL_HINT.search(text)
    stripped = text
    if hint:
        calories = float(next(g for g in hint.groups() if g))
        stripped = (text[:hint.start()] + " " + text[hint.end():]).strip()
    item = _parse_item(stripped)
    item.raw, item.calories = text.strip(), calories
    return item


def _parse_item(text: str) -> Item:
    """Parse "[meal:] [amount][unit] [of] food" or "food [amount][unit]".

    Without a unit the number counts servings ("2 eggs"). Examples:
      "150g chicken breast"  -> 150 gram of "chicken breast"
      "lunch: 2 eggs"        -> 2 servings of "eggs", meal lunch
      "1 1/2 cups rice"      -> 1.5 cup of "rice"
      "oatmeal 40 g"         -> 40 gram of "oatmeal"
    """
    s = text.strip()
    meal = None
    m = re.match(r"^(\w+)\s*:\s*(.+)$", s)
    if m and m.group(1).lower() in MEAL_WORDS:
        meal, s = MEAL_WORDS[m.group(1).lower()], m.group(2).strip()
    if not s:
        raise ValueError(f"Nothing to log in {text!r}")

    if _HEX_ID.match(s.lower()):
        return Item(s.lower(), None, None, meal)

    # leading quantity: "150g x", "150 g x", "2 x", "1 1/2 cups x"
    m = re.match(rf"^({_NUM})(\s*)(\S.*)$", s)
    if m:
        amount = _number(m.group(1))
        words = m.group(3).split()
        if not m.group(2):  # glued: "150g rice" ok, "7up" is a name
            glued = re.match(r"^([a-zA-Z.]+)$", words[0])
            unit = UNIT_WORDS.get(words[0].lower().rstrip(".")) if glued else None
            words = [words[0]] + words[1:] if unit else None
        if words:
            unit, used = _take_unit(words)
            rest = words[used:]
            if rest and rest[0].lower() == "of":
                rest = rest[1:]
            if not unit and rest and rest[0].lower() in _NAME_WORDS_AFTER_NUMBER:
                rest = None   # "45 calorie bread" is a name
            if rest and (unit or amount <= 50):
                return Item(_hex_or_text(rest), amount, unit, meal)

    # trailing quantity: "chicken breast 150g", "eggs x2"
    m = re.match(rf"^(.*?)\s+(?:x\s*)?({_NUM})\s*([a-zA-Z][a-zA-Z.]*(?:\s+[a-zA-Z.]+)?)?$", s)
    if m and m.group(1):
        unit, used = _take_unit((m.group(3) or "").split())
        amount = _number(m.group(2))
        if used == len((m.group(3) or "").split()) and (unit or amount <= 50):
            return Item(_hex_or_text(m.group(1).split()), amount, unit, meal)

    return Item(s, None, None, meal)


def parse_date(value: str | None, today: date | None = None) -> date:
    """today | yesterday | tomorrow | +N | -N | YYYY-MM-DD."""
    today = today or date.today()
    if not value or value.lower() == "today":
        return today
    v = value.lower()
    if v == "yesterday":
        return today - timedelta(days=1)
    if v == "tomorrow":
        return today + timedelta(days=1)
    if re.fullmatch(r"[+-]\d+", v):
        return today + timedelta(days=int(v))
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"Bad date {value!r}: use YYYY-MM-DD, today, yesterday, tomorrow or ±N")


def default_meal(hour: int) -> str:
    """Meal for the time of day, like the LoseIt apps."""
    if hour < 11:
        return "breakfast"
    if hour < 15:
        return "lunch"
    if 17 <= hour < 21:
        return "dinner"
    return "snacks"


def normalize_words(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return [w[:-1] if len(w) > 3 and w.endswith("s") else w for w in words]


def _word_parts(word: str) -> list[str]:
    """Split a CamelCase/CAPSlower word: "PEANUTbutter" -> ["PEANUT", "butter"]."""
    return re.findall(r"[A-Z]{2,}(?=[a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+", word)


def query_variants(query: str, names: list[str]) -> list[str]:
    """Other spellings of `query` worth searching, learned from result names.

    LoseIt's search matches whole words, so "peanutbutter" misses foods named
    "Peanut Butter" and vice versa. A query word is split when both halves occur
    in the names (including CamelCase parts: "PEANUTbutter" -> peanut, butter), and
    adjacent query words are joined when the joined word occurs.
    """
    vocab = set()
    for name in names:
        for word in re.findall(r"[A-Za-z0-9]+", name):
            vocab.add(word.lower())
            vocab.update(p.lower() for p in _word_parts(word))
    words = query.split()
    out = []
    for i, w in enumerate(words):
        lw = re.sub(r"[^a-z0-9]", "", w.lower())
        for cut in range(3, len(lw) - 2):
            if lw[:cut] in vocab and lw[cut:] in vocab:
                out.append(" ".join(words[:i] + [lw[:cut], lw[cut:]] + words[i + 1:]))
                break
        if i + 1 < len(words):
            joined = re.sub(r"[^a-z0-9]", "", (w + words[i + 1]).lower())
            if joined in vocab:
                out.append(" ".join(words[:i] + [joined] + words[i + 2:]))
    return list(dict.fromkeys(v for v in out if v.lower() != query.lower()))
