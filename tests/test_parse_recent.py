"""Quick-log item parsing and the recent-foods ranking."""
from datetime import date

import pytest

from loseit.client.parse import default_meal, parse_date, parse_item, query_variants
from loseit.client.recent import RecentFoods, rank_search_results


@pytest.mark.parametrize("text, query, amount, unit, meal", [
    ("150g chicken breast", "chicken breast", 150, "gram", None),
    ("lunch: 2 eggs", "eggs", 2, None, "lunch"),
    ("snack: 28 g cheez it", "cheez it", 28, "gram", "snacks"),
    ("1 1/2 cups rice", "rice", 1.5, "cup", None),
    ("1/2 avocado", "avocado", 0.5, None, None),
    ("3 oz of salmon", "salmon", 3, "ounce", None),
    ("2 fl oz milk", "milk", 2, "fluid ounce", None),
    ("oatmeal 40 g", "oatmeal", 40, "gram", None),
    ("eggs x2", "eggs", 2, None, None),
    ("bagel", "bagel", None, None, None),
    ("7up", "7up", None, None, None),                     # digits glued to a name
    ("dinner: 7up 2 cans", "7up", 2, "can", "dinner"),
    ("100 grand bar", "100 grand bar", None, None, None),  # big unit-less number = name
    ("1bc020d463c8437680d943c114bff8c9", "1bc020d463c8437680d943c114bff8c9", None, None, None),
    ("200 ml 0a57f38b", "0a57f38b", 200, "milliliter", None),
])
def test_parse_item(text, query, amount, unit, meal):
    item = parse_item(text)
    assert (item.query, item.amount, item.unit, item.meal) == (query, amount, unit, meal)
    assert item.raw == text


@pytest.mark.parametrize("text, query, amount, unit, calories", [
    ("1 can cola ~140cal", "cola", 1, "can", 140),
    ("dinner: tuna salad (110 cal)", "tuna salad", None, None, 110),
    ("150g chicken breast ~250 kcal", "chicken breast", 150, "gram", 250),
    ("bagel 250 calories", "bagel", None, None, 250),
    ("calamari", "calamari", None, None, None),
    ("45 calorie wheat bread", "45 calorie wheat bread", None, None, None),
    ("bread 45 calories wheat", "bread 45 calories wheat", None, None, None),
    ("2 slices 45 calorie bread", "45 calorie bread", 2, "slice", None),
    ("4 slices 45 calorie bread ~180 cal", "45 calorie bread", 4, "slice", 180),
    ("bagel @250kcal", "bagel", None, None, 250),
])
def test_parse_item_calorie_hint(text, query, amount, unit, calories):
    item = parse_item(text)
    assert (item.query, item.amount, item.unit, item.calories) == (query, amount, unit, calories)


def test_parse_item_ids():
    assert parse_item("1bc020d4").is_id
    assert not parse_item("bagel").is_id


@pytest.mark.parametrize("text, expected", [
    (None, date(2026, 10, 5)), ("today", date(2026, 10, 5)), ("yesterday", date(2026, 10, 4)),
    ("tomorrow", date(2026, 10, 6)), ("-3", date(2026, 10, 2)), ("+1", date(2026, 10, 6)),
    ("2026-01-02", date(2026, 1, 2)),
])
def test_parse_date(text, expected):
    assert parse_date(text, today=date(2026, 10, 5)) == expected


def test_parse_date_rejects_garbage():
    with pytest.raises(ValueError, match="Bad date"):
        parse_date("someday")


@pytest.mark.parametrize("hour, meal", [(7, "breakfast"), (12, "lunch"), (16, "snacks"),
                                        (19, "dinner"), (23, "snacks")])
def test_default_meal(hour, meal):
    assert default_meal(hour) == meal


def _food(fid, name, category="", amount=1.0, unit="Serving", meal="breakfast", entry=None, brand=""):
    return {"food_id": fid, "entry_id": entry or fid + "-e", "name": name, "brand": brand,
            "category": category, "amount": amount, "unit": unit, "meal": meal, "calories": 100.0}


class TestRecent:
    def test_bagel_prefers_the_one_eaten_most_recently(self):
        r = RecentFoods()
        r.record_day("2026-09-20", [_food("generic", "Bagel, Plain", "Bagel")])
        r.record_day("2026-10-04", [_food("thomas", "Thomas' Everything Bagel", "Bagel",
                                          amount=1, unit="Each", entry="t1")])
        best = r.ranked("bagel")[0]
        assert best["food_id"] == "thomas"
        assert (best["last_amount"], best["last_unit"]) == (1, "Each")

    def test_main_thing_beats_incidental_word(self):
        r = RecentFoods()
        r.record_day("2026-10-01", [_food("eggs", "Egg, Large", "Egg")])
        r.record_day("2026-10-04", [_food("croissant", "Sausage, Bacon, Egg & Cheese Croissant", "Sandwich")])
        assert r.ranked("eggs")[0]["food_id"] == "eggs"

    def test_record_day_replaces_that_day(self):
        r = RecentFoods()
        r.record_day("2026-10-04", [_food("a", "Apple")])
        r.record_day("2026-10-04", [])          # entry deleted
        assert r.ranked("apple") == []

    def test_frequency_counts_entries(self):
        r = RecentFoods()
        for i, day in enumerate(["2026-10-01", "2026-10-02", "2026-10-03"]):
            r.record_day(day, [_food("a", "Apple", entry=f"e{i}")])
        assert r.ranked()[0]["times"] == 3

    def test_no_match(self):
        r = RecentFoods()
        r.record_day("2026-10-04", [_food("a", "Apple")])
        assert r.ranked("pizza") == []

    def test_persistence_and_staleness(self):
        r = RecentFoods()
        assert r.stale
        r.record_day("2026-10-04", [_food("a", "Apple")])
        r.mark_refreshed()
        r.save()
        loaded = RecentFoods.load()
        assert not loaded.stale
        assert loaded.find_by_prefix("a") == ["a"]


    def test_search_hits_resolve_by_prefix(self):
        r = RecentFoods()
        r.remember([{"food_id": "abcdef0123", "name": "Tuna Salad", "brand": "Generic"}])
        r.save()
        assert RecentFoods.load().find_by_prefix("abcdef") == ["abcdef0123"]


def test_rank_search_results_prefers_closest_name():
    hits = [{"name": "Carrot cake w/cream cheese icing", "brand": ""},
            {"name": "Carrots, Medium", "brand": ""}]
    assert rank_search_results("carrot", hits)[0]["name"] == "Carrots, Medium"


def test_query_variants_split_and_join_from_result_names():
    names = ["PEANUTbutter Cookies", "Cookies, PeanutButter"]
    assert query_variants("peanutbutter cookies", names) == ["peanut butter cookies"]
    assert query_variants("peanut butter cookies", names) == ["peanutbutter cookies"]
    assert query_variants("whole milk", names) == []
