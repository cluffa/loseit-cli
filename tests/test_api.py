"""API client against a fake LoseIt server built from recorded fixtures."""
import copy
import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from loseit.client.api import (
    LoseItAPI, build_serving_size, key_to_hex, summarize_daily_details, summarize_food,
)
from loseit.client.gwt_schema import GEnum, GObject, RequestDecoder, ResponseDecoder
from loseit.client.parse import parse_item
from loseit.client.recent import RecentFoods, rank_search_results
from loseit.client.session import Session, SessionStore

FIXTURES = Path(__file__).parent / "fixtures"
TYPES = json.loads((FIXTURES / "gwt_schema.json").read_text())["types"]
DAY = date(2026, 10, 6)


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text().strip()


def decode(name: str):
    return ResponseDecoder(TYPES).decode(fixture(name)).fields[3]


class FakeLoseIt:
    """Answers RPCs with recorded responses and records every decoded call."""

    RESPONSES = {
        "getFood": "getFood_response_01.txt",
        "getUnsavedFoodLogEntry": "getUnsavedFoodLogEntry_response_01.txt",
        "updateFoodLogEntry": "updateFoodLogEntry_response_01.txt",
        "deleteFoodLogEntry": "updateFoodLogEntry_response_01.txt",
        "searchFoods": "searchFoods_response_03.txt",
    }

    def __init__(self):
        self.calls = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        call = RequestDecoder(TYPES).decode(request.content.decode())
        self.calls.append(call)
        return httpx.Response(200, text=fixture(self.RESPONSES[call.method]))

    def methods(self):
        return [c.method for c in self.calls]

    def saved(self):
        return [c.params[1] for c in self.calls if c.method == "updateFoodLogEntry"]


@pytest.fixture
def day_details():
    """The recorded DailyDetails with two food entries added (copies of the
    recorded pre-filled pie entry, as breakfast and dinner)."""
    details = decode("getDailyDetailsIncludingPendingForDate_response_01.txt")
    pie = decode("getUnsavedFoodLogEntry_response_01.txt")
    dinner = copy.deepcopy(pie)
    dinner.fields[1].fields[8] = GEnum("FoodLogEntryType", 2, pie.fields[1].fields[8].signature)
    dinner.fields[6].fields[0].fields[0][0] = 0x11  # distinct entry key
    details.fields[5].fields[0] = [pie, dinner]
    return details


@pytest.fixture
def api(monkeypatch, day_details):
    SessionStore.save(Session(user_id=12345678, username="test", cookies={"liauth": "x"}))
    recent = RecentFoods()
    recent.mark_refreshed()   # no history refresh over the network
    recent.save()
    api = LoseItAPI()
    fake = FakeLoseIt()
    api._client = httpx.Client(transport=httpx.MockTransport(fake))
    api.fake = fake
    monkeypatch.setattr(api, "_daily_details", lambda d: day_details)
    return api


class TestSummaries:
    def test_daily_details_fixture(self):
        details = decode("getDailyDetailsIncludingPendingForDate_response_01.txt")
        s = summarize_daily_details(details, date(2026, 7, 27))
        assert s["calorie_budget"] == 1403.8
        assert s["calorie_burn_target"] == 2403.8
        assert s["calorie_deficit"] == 1000.0
        assert s["exercise_calories"] == 22.0
        assert s["calories_remaining"] == 1425.8
        assert s["weight"] == pytest.approx(213.2)
        assert s["goal_weight"] == 195.0

    def test_entries_scale_by_servings_logged(self, day_details):
        pie = day_details.fields[5].fields[0][0]
        pie.fields[2].fields[0].fields[1] = 2.0   # 2 servings logged of a 1-serving nutrient set
        s = summarize_daily_details(day_details, DAY)
        assert s["foods"][0]["calories"] == 820.0
        assert s["foods"][0]["meal"] == "breakfast"
        assert s["foods"][1]["meal"] == "dinner"
        assert s["foods"][0]["entry_id"] != s["foods"][1]["entry_id"]

    def test_food(self):
        info = summarize_food(decode("getFood_response_01.txt"))
        assert info["name"] == "Peanut Butter Creme Pie"
        assert info["servings"] == [{"amount": 1.0, "unit": "Each", "calories": 410.0}]


class TestServingSize:
    # Chicken breast option from the HAR: "3 Ounce = 0.850485 base servings"
    OPTION = GObject("FoodServingSize", [0.850485, True, GEnum("FoodMeasure", 6), 0.850485, 3.0, 3.0], "sig")

    def test_amount_in_option_unit(self):
        size = build_serving_size(self.OPTION, 6.666, None, None)
        assert size.fields[0] == pytest.approx(1.88977767, rel=1e-6)   # as the web app saved it
        assert size.fields[5] == 6.666

    def test_converted_unit(self):
        size = build_serving_size(self.OPTION, 170.0, "g", None)
        assert size.fields[2].ordinal == 8
        assert size.fields[0] == pytest.approx(170.0 / 28.3495 / 3.0 * 0.850485)

    def test_servings(self):
        assert build_serving_size(self.OPTION, None, None, 2).fields[5] == 6.0

    def test_incompatible_unit(self):
        with pytest.raises(ValueError, match="convert"):
            build_serving_size(self.OPTION, 1, "cup", None)


class TestLogging:
    def test_log_food_builds_entry_like_the_web_app(self, api):
        food_id = "94bc756214614b11bdfe5f9e6463e334"
        result = api.log_food(food_id, servings=2, meal_type="dinner", target_date=DAY)
        assert api.fake.methods() == ["getFood", "getUnsavedFoodLogEntry", "updateFoodLogEntry"]
        entry = api.fake.saved()[0]
        ctx, serving = entry.fields[1], entry.fields[2]
        assert ctx.fields[1].fields[1] == (DAY - date(2000, 12, 31)).days
        assert ctx.fields[8].ordinal == 2                       # dinner
        assert ctx.fields[4] == -1
        assert serving.fields[1].fields[5] == 2.0               # amount entered
        assert serving.fields[0].fields[1] == 2.0               # servings logged
        assert result["calories"] == 820.0
        assert result["entry_id"] == key_to_hex(entry.fields[6])

    def test_log_items_by_id_returns_day_totals(self, api):
        out = api.log_items([parse_item("lunch: 1.5 94bc756214614b11bdfe5f9e6463e334")], None, DAY)
        assert out["logged"][0]["meal"] == "lunch"
        assert out["logged"][0]["amount"] == 1.5
        assert out["day"]["date"] == DAY.isoformat()

    def test_bad_item_fails_before_anything_is_written(self, api):
        items = [parse_item("94bc756214614b11bdfe5f9e6463e334"),
                 parse_item("1 cup 94bc756214614b11bdfe5f9e6463e334")]
        with pytest.raises(ValueError, match="not available"):
            api.log_items(items, "lunch", DAY)
        assert "updateFoodLogEntry" not in api.fake.methods()

    def test_search_fallback_reranks(self, api):
        choice = api.resolve_item(parse_item("carrots medium"))
        assert choice["source"] == "search"
        assert choice["name"] == "Carrots, Medium"


class TestEditDelete:
    def test_edit_amount_keeps_unit_and_entry(self, api, day_details):
        out = api.edit_entry(key_to_hex(day_details.fields[5].fields[0][0].fields[6])[:6], DAY, amount=3)
        saved = api.fake.saved()[0]
        assert saved.fields[2].fields[1].fields[5] == 3.0
        assert out["updated"]["calories"] == 1230.0
        assert "getUnsavedFoodLogEntry" not in api.fake.methods()

    def test_ambiguous_name_lists_candidates(self, api):
        with pytest.raises(LookupError, match="matches 2 entries"):
            api.edit_entry("peanut butter pie", DAY, amount=1)

    def test_name_with_meal_filter(self, api):
        out = api.delete_entries(DAY, ["pie"], meal="dinner")
        assert [d["meal"] for d in out["deleted"]] == ["dinner"]

    def test_delete_all(self, api):
        out = api.delete_entries(DAY, [], all_entries=True)
        assert len(out["deleted"]) == 2
        assert api.fake.methods() == ["deleteFoodLogEntry", "deleteFoodLogEntry"]

    def test_move(self, api, day_details):
        entry_id = key_to_hex(day_details.fields[5].fields[0][0].fields[6])
        api.edit_entry(entry_id, DAY, move_to=date(2026, 10, 7))
        assert api.fake.methods()[-3:] == ["getUnsavedFoodLogEntry", "updateFoodLogEntry", "deleteFoodLogEntry"]
        moved = api.fake.saved()[-1]
        assert moved.fields[1].fields[1].fields[1] == (date(2026, 10, 7) - date(2000, 12, 31)).days

    def test_copy_meal(self, api):
        out = api.copy_entries(DAY, date(2026, 10, 7), meal="dinner", to_meal="lunch")
        assert [c["meal"] for c in out["copied"]] == ["lunch"]


def make_food(api, food_id: str, name: str, calories: float, measure: int = 5):
    """A getFood result like the recorded pie, with another id/name/calories/unit."""
    food = copy.deepcopy(decode("getFood_response_01.txt"))
    food.fields[0].fields[3] = name
    food.fields[0].fields[9] = api.key(food_id)
    pairs = food.fields[1].fields[2].fields
    pairs[:] = [(k, calories if k.ordinal == 0 else v) for k, v in pairs]
    option = food.fields[2].fields[0][0]
    option.fields[2] = GEnum("FoodMeasure", measure, option.fields[2].signature)
    api._foods[food_id] = food
    return food


class TestAgentFlow:
    def test_search_includes_default_serving_and_calories(self, api):
        hits = api.search_foods("carrots", limit=3)
        assert all(h["calories"] == 410.0 and h["serving"] == "1 Each" for h in hits)
        assert api.fake.methods().count("getFood") == 3
        # ids from search now resolve by prefix
        assert api.resolve_food_id(hits[0]["food_id"][:8]) == hits[0]["food_id"]

    def test_calorie_hint_picks_closest_candidate(self, api):
        hits = rank_search_results("carrots", api.search_foods("carrots", limit=15, details=False))
        for i, h in enumerate(hits[:8]):
            make_food(api, h["food_id"], h["name"], 220.0 if i != 5 else 94.0)
        choice = api.resolve_item(parse_item("carrots ~95cal"))
        assert choice["food_id"] == hits[5]["food_id"]
        assert choice["calories"] == 94.0
        assert "warning" not in choice
        assert all(a["calories"] == 220.0 for a in choice["alternatives"])

    def test_calorie_hint_far_off_warns(self, api):
        choice = api.resolve_item(parse_item("carrots ~95cal"))   # every fake food is 410
        assert "not ~95" in choice["warning"]

    def test_count_unit_falls_back_to_foods_own_unit(self, api):
        food_id = "94bc756214614b11bdfe5f9e6463e334"
        make_food(api, food_id, "Cola", 140.0, measure=27)   # Serving
        plan = api.describe_plan(api.plan_items([parse_item(f"1 can {food_id}")], "snacks", DAY)[0])
        assert (plan["amount"], plan["unit"], plan["calories"]) == (1.0, "Serving", 140.0)
        assert "used Serving" in plan["notes"][0]

    def test_weight_unit_still_rejected_without_weight_option(self, api):
        with pytest.raises(ValueError, match="not available"):
            api.plan_items([parse_item("1 cup 94bc756214614b11bdfe5f9e6463e334")], "lunch", DAY)

    def test_edit_food_swaps_keeping_meal_and_amount(self, api, day_details):
        old = day_details.fields[5].fields[0][1]                    # dinner pie, 1 Each
        new_id = "f5f3444b5631455e80aaa30104a931f9"
        make_food(api, new_id, "Donut", 400.0, measure=26)          # Slice
        out = api.edit_entry(key_to_hex(old.fields[6]), DAY, food=new_id)
        u = out["updated"]
        assert (u["status"], u["food_id"], u["meal"], u["amount"], u["unit"]) == (
            "replaced", new_id, "dinner", 1.0, "Slice")
        unsaved = next(c for c in api.fake.calls if c.method == "getUnsavedFoodLogEntry")
        assert key_to_hex(unsaved.params[1]) == new_id
        assert u["replaced"]["entry_id"] == key_to_hex(old.fields[6])
        assert api.fake.methods()[-3:] == ["getUnsavedFoodLogEntry", "updateFoodLogEntry",
                                           "deleteFoodLogEntry"]

    def test_count_unit_doesnt_promote_a_different_product(self, api):
        hits = rank_search_results("carrots", api.search_foods("carrots", limit=15, details=False))
        for i, h in enumerate(hits[:8]):
            make_food(api, h["food_id"], h["name"], 50.0, measure=8 if i != 2 else 27)   # grams / Serving
        with pytest.raises(ValueError, match="comes in 'can'"):
            api.resolve_item(parse_item("1 can carrots"))
        # a matching calorie hint makes the swapped unit acceptable
        assert api.resolve_item(parse_item("1 can carrots ~50cal"))["food_id"] == hits[2]["food_id"]
        with pytest.raises(ValueError, match="at ~500 cal"):
            api.resolve_item(parse_item("1 can carrots ~500cal"))

    def test_unmet_hint_searches_other_spelling(self, api, monkeypatch):
        ids = {"peanutbutter cookies": ["aa" * 16], "peanut butter cookies": ["bb" * 16]}
        names = {"aa" * 16: ("PEANUTbutter Cookies", 120.0), "bb" * 16: ("Peanut Butter Cookies", 160.0)}
        for fid, (name, cal) in names.items():
            make_food(api, fid, name, cal)
        searched = []

        def fake_search(query, limit=10, details=True):
            searched.append(query)
            return [{"food_id": f, "name": names[f][0], "brand": "Generic", "category": "Cookie"}
                    for f in ids.get(query, [])]
        monkeypatch.setattr(api, "search_foods", fake_search)
        choice = api.resolve_item(parse_item("peanutbutter cookies ~160cal"))
        assert searched == ["peanutbutter cookies", "peanut butter cookies"]
        assert choice["food_id"] == "bb" * 16 and "warning" not in choice
        # a met hint doesn't trigger extra searches
        searched.clear()
        api.resolve_item(parse_item("peanutbutter cookies ~120cal"))
        assert searched == ["peanutbutter cookies"]
