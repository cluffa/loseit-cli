"""Recipe support: parsing, create/delete request building, recipe-food sizing.

No network: `api.call` is replaced with a fake that serves getRecipes from
Recipe objects built in this file and records saveRecipe / removeRecipe.
"""
import copy
import json
from pathlib import Path

import pytest

from loseit.client.api import (
    LoseItAPI, _calories, key_to_hex, summarize_food, summarize_recipe,
)
from loseit.client.gwt_schema import (
    GEnum, GObject, RequestDecoder, RequestEncoder, RpcCall, box,
)
from loseit.client.parse import parse_item
from loseit.client.recent import RecentFoods
from loseit.client.session import Session, SessionStore

FIXTURES = Path(__file__).parent / "fixtures"
TYPES = json.loads((FIXTURES / "gwt_schema.json").read_text())["types"]
MS = 1_700_000_000_000
YOGURT = "86e4d1da19954ee9b4f250e21fce2a75"
HONEY = "a86cceaaa882438ea07e999848235421"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text().strip()


def make_food(api, food_id: str, name: str, calories: float, measure: int = 8):
    """A getFood result like the recorded pie, with another id/name/calories/unit
    (nutrients are per 1 unit of the measure, e.g. per gram)."""
    from loseit.client.gwt_schema import ResponseDecoder
    food = ResponseDecoder(TYPES).decode(fixture("getFood_response_01.txt")).fields[3]
    food.fields[0].fields[3] = name
    food.fields[0].fields[9] = api.key(food_id)
    pairs = food.fields[1].fields[2].fields
    pairs[:] = [(k, box(calories, v.signature) if k.ordinal == 0 else v) for k, v in pairs]
    option = food.fields[2].fields[0][0]
    option.fields[2] = GEnum("FoodMeasure", measure, option.fields[2].signature)
    option.fields[3] = option.fields[4] = option.fields[5] = 1.0
    return food


def make_recipe(api, name: str, key_hex: str, servings: float = 4.0,
                total_calories: float = 500.0, instructions: str = "",
                current: bool = True, created_ms: int = MS):
    """A Recipe object with the layout summarize_recipe documents: one
    ingredient (100 g of a 2 cal/g sauce = 200 of the total calories).
    `current` sets the ActiveFood [6]/[7] current-version flags."""
    sig = api.sig
    key = api.key(key_hex)
    measure = GEnum("FoodMeasure", 27, sig("FoodMeasure"))
    pairs = [(GEnum("FoodMeasurement", 0, sig("FoodMeasurement")), box(total_calories, sig("java.lang.Double"))),
             (GEnum("FoodMeasurement", 13, sig("FoodMeasurement")), box(40.0, sig("java.lang.Double")))]
    nutrients = GObject("FoodNutrients", [1.0, 1.0, GObject("HashMap", pairs, sig("java.util.HashMap"))],
                        sig("FoodNutrients"))
    size = GObject("FoodServingSize", [1.0, True, measure, 1.0, servings, servings], sig("FoodServingSize"))
    ident = GObject("FoodIdentifier",
                    [-1, "Recipe", None, name, "", GEnum("FoodProductType", 0, sig("FoodProductType")),
                     -1, None, created_ms, key], sig("FoodIdentifier"))
    active = GObject("ActiveFood",
                     [-1, ident, GObject("FoodServing", [nutrients, size], sig("FoodServing")),
                      GObject("HourDate", [box(created_ms, sig("java.util.Date")), 10_000], sig("HourDate")),
                      False, 7, current, current, created_ms, key], sig("ActiveFood"))

    ing_ident = GObject("FoodIdentifier",
                        [-1, "Sauce", None, "Sauce", "Brand", GEnum("FoodProductType", 0, sig("FoodProductType")),
                         -1, None, MS, api.key("11" * 16)], sig("FoodIdentifier"))
    ing_pairs = [(GEnum("FoodMeasurement", 0, sig("FoodMeasurement")), box(200.0, sig("java.lang.Double")))]
    ing_nutrients = GObject("FoodNutrients",
                            [100.0, 100.0, GObject("HashMap", ing_pairs, sig("java.util.HashMap"))],
                            sig("FoodNutrients"))
    ing_size = GObject("FoodServingSize",
                       [100.0, True, GEnum("FoodMeasure", 8, sig("FoodMeasure")), 100.0, 100.0, 100.0],
                       sig("FoodServingSize"))
    ingredient = GObject("RecipeIngredient",
                         [False, ing_ident, GObject("FoodServing", [ing_nutrients, ing_size], sig("FoodServing")),
                          -1, False, -1, key, MS, api.key("22" * 16)], sig("RecipeIngredient"))
    wrapper = GObject("RecipeIngredient;", [[ingredient]],
                      sig("[Lcom.loseit.core.client.model.RecipeIngredient;"))
    return GObject("Recipe",
                   [active, None, None, False, servings, None, wrapper, True, instructions,
                    measure, box(1.0, sig("java.lang.Double")), measure, True, created_ms, key],
                   sig("Recipe"))


class FakeRecipes:
    """Stands in for LoseItAPI.call: an in-memory recipe store + food DB."""

    def __init__(self, api, recipes, foods):
        self.api = api
        self.recipes = list(recipes)
        self.foods = foods          # food_id -> GObject (getFood results)
        self.calls = []

    def __call__(self, method, *params, overload=None):
        self.calls.append((method, params))
        if method == "getRecipes":
            return GObject("Recipe;", [list(self.recipes)],
                           self.api.sig("[Lcom.loseit.core.client.model.Recipe;"))
        if method == "getFood":
            return self.foods.get(key_to_hex(params[1]))
        if method == "saveRecipe":
            self.recipes.append(params[1])
            return box(1, self.api.sig("java.lang.Integer"))
        if method == "removeRecipe":
            doomed = {id(r) for r in params[1].fields[0]}
            self.recipes = [r for r in self.recipes if id(r) not in doomed]
            return box(len(doomed), self.api.sig("java.lang.Integer"))
        raise AssertionError(f"unexpected call {method}")

    def methods(self):
        return [m for m, _ in self.calls]


@pytest.fixture
def api(monkeypatch):
    SessionStore.save(Session(user_id=12345678, username="test", cookies={"liauth": "x"}))
    recent = RecentFoods()
    recent.mark_refreshed()
    recent.save()
    api = LoseItAPI()
    recipes = [make_recipe(api, "Test Soup", "aa" * 16, instructions="Boil everything."),
               make_recipe(api, "Test Soup", "bb" * 16),
               make_recipe(api, "Plain Rice", "cc" * 16, servings=2.0, total_calories=300.0)]
    foods = {YOGURT: make_food(api, YOGURT, "Greek Yogurt", 0.9),   # per gram
             HONEY: make_food(api, HONEY, "Honey", 64.0, measure=2)}  # per tablespoon
    fake = FakeRecipes(api, recipes, foods)
    monkeypatch.setattr(api, "call", fake)
    api.fake = fake
    return api


def encode_call(api, method, *params):
    """The params encode with the fixture schema and decode back intact."""
    types = api.schema["methods"][method][0]
    body = RequestEncoder(TYPES).encode(RpcCall(method, types, list(params), "base", "hash", "svc"))
    return RequestDecoder(TYPES).decode(body)


class TestSummarize:
    def test_recipe_fields(self, api):
        s = summarize_recipe(api.fake.recipes[0])
        assert s["name"] == "Test Soup"
        assert s["recipe_id"] == "aa" * 16 and s["food_id"] == "aa" * 16
        assert s["servings"] == 4.0 and s["serving_unit"] == "Serving"
        assert s["current"] is True
        assert s["created"] == "2023-11-14"     # MS = 2023-11-14 (UTC)
        assert s["calories_per_serving"] == 125.0
        assert s["nutrients_per_serving"]["protein_g"] == 10.0
        assert s["instructions"] == "Boil everything."
        (ing,) = s["ingredients"]
        assert ing == {"food_id": "11" * 16, "name": "Sauce", "brand": "Brand",
                       "amount": 100.0, "unit": "Gram", "calories": 200.0}

    def test_list_and_query(self, api):
        assert [r["name"] for r in api.list_recipes()] == ["Test Soup", "Test Soup", "Plain Rice"]
        hits = api.list_recipes("rice")
        assert [r["name"] for r in hits] == ["Plain Rice"]
        assert hits[0]["ingredient_count"] == 1 and "ingredients" not in hits[0]

    def test_resolve_by_id_and_name(self, api):
        assert api.get_recipe("cccc")["name"] == "Plain Rice"
        assert api.get_recipe("plain rice")["servings"] == 2.0
        with pytest.raises(LookupError, match="matches 2 recipes"):
            api.get_recipe("test soup")
        with pytest.raises(LookupError, match="No recipe matching"):
            api.get_recipe("nonexistent stew")


class TestVersioning:
    """Edits create new Recipe objects; the ActiveFood [6]/[7] flags mark the
    current version(s). Default views match the app (current only)."""

    @pytest.fixture(autouse=True)
    def store(self, api):
        # a retired-only recipe, plus a retired copy of a current recipe
        api.fake.recipes.append(make_recipe(api, "Old Stew", "dd" * 16, current=False,
                                            created_ms=1_600_000_000_000))
        api.fake.recipes.append(make_recipe(api, "Plain Rice", "ee" * 16, current=False,
                                            servings=2.0, total_calories=999.0))
        return api

    def test_summarize_retired(self, api):
        s = summarize_recipe(api.fake.recipes[-2])
        assert s["current"] is False
        assert s["created"] == "2020-09-13"

    def test_list_defaults_to_current(self, api):
        assert [r["name"] for r in api.list_recipes()] == ["Test Soup", "Test Soup", "Plain Rice"]
        names = [r["name"] for r in api.list_recipes(all_versions=True)]
        assert names == ["Test Soup", "Test Soup", "Plain Rice", "Old Stew", "Plain Rice"]
        # the query applies within the selected set
        assert api.list_recipes("old stew") == []
        assert [r["recipe_id"] for r in api.list_recipes("old stew", all_versions=True)] == ["dd" * 16]

    def test_retired_name_errors_without_all(self, api):
        with pytest.raises(LookupError, match="retired recipe versions"):
            api.get_recipe("old stew")
        with pytest.raises(LookupError, match="--all"):
            api.get_recipe("old stew")
        out = api.get_recipe("old stew", all_versions=True)
        assert out["name"] == "Old Stew" and out["current"] is False

    def test_id_reaches_retired(self, api):
        out = api.get_recipe("dddd")
        assert out["name"] == "Old Stew" and out["current"] is False

    def test_name_resolution_prefers_current(self, api):
        # "Plain Rice" exists as current (cc, 150 cal/serving) and retired (ee)
        assert api.get_recipe("plain rice")["recipe_id"] == "cc" * 16
        assert api.get_recipe("plain rice", all_versions=True)["recipe_id"] == "cc" * 16

    def test_delete_never_picks_superseded(self, api):
        out = api.delete_recipe("plain rice")
        assert out["deleted"]["recipe_id"] == "cc" * 16
        remaining = {r["recipe_id"] for r in api.list_recipes(all_versions=True)}
        assert "ee" * 16 in remaining and "cc" * 16 not in remaining

    def test_delete_retired_needs_all(self, api):
        with pytest.raises(LookupError, match="retired"):
            api.delete_recipe("old stew")
        assert len(api.fake.recipes) == 5
        out = api.delete_recipe("old stew", all_versions=True)
        assert out["deleted"]["name"] == "Old Stew"
        assert len(api.fake.recipes) == 4


class TestCreateDelete:
    def test_create_builds_save_recipe(self, api, monkeypatch):
        choices = {parse_item("100 grams greek yogurt").query: YOGURT,
                   parse_item("1 tbsp honey").query: HONEY}
        monkeypatch.setattr(api, "resolve_item",
                            lambda item: {"food_id": choices[item.query], "source": "search",
                                          "name": None, "alternatives": [],
                                          "amount": item.amount, "unit": item.unit})
        out = api.create_recipe("New Stew", 2.0, "Simmer.", [parse_item("100 grams greek yogurt"),
                                                             parse_item("1 tbsp honey")])
        assert api.fake.methods() == ["getRecipes", "getFood", "getFood", "saveRecipe", "getRecipes"]
        saved = next(p for m, p in api.fake.calls if m == "saveRecipe")[1]
        ident = saved.fields[0].fields[1]
        assert ident.fields[3] == "New Stew" and ident.fields[1] == "Recipe"
        # a new recipe is the current version
        assert saved.fields[0].fields[6] is True and saved.fields[0].fields[7] is True
        assert saved.fields[4] == 2.0 and saved.fields[8] == "Simmer."
        # the recipe key is shared, and each ingredient points back at it
        assert saved.fields[14] is ident.fields[9] is saved.fields[0].fields[9]
        ings = saved.fields[6].fields[0]
        assert [(i.fields[1].fields[3], i.fields[2].fields[1].fields[5]) for i in ings] == [
            ("Greek Yogurt", 100.0), ("Honey", 1.0)]
        assert all(i.fields[6] is saved.fields[14] for i in ings)
        # whole-recipe calories are the ingredients' sum: 100 g * 0.9 + 1 tbsp * 64
        from loseit.client.api import entry_nutrients
        assert entry_nutrients(saved.fields[0].fields[2])[0] == pytest.approx(154.0)
        # the request encodes and round-trips
        back = encode_call(api, "saveRecipe", api.token(), saved)
        assert back.params[1].fields[0].fields[1].fields[3] == "New Stew"
        assert back.params[1].fields[4] == 2.0
        # and the created recipe is reported from the re-fetch
        created = out["created"]
        assert created["name"] == "New Stew" and created["calories_per_serving"] == 77.0
        assert created["current"] is True
        assert [i["name"] for i in created["ingredients"]] == ["Greek Yogurt", "Honey"]

    def test_create_from_retired_template_still_current(self, api, monkeypatch):
        # the template is the store's first recipe; retire it so create would
        # inherit cleared current flags if it didn't set them explicitly
        template = api.fake.recipes[0]
        template.fields[0].fields[6] = template.fields[0].fields[7] = False
        monkeypatch.setattr(api, "resolve_item",
                            lambda item: {"food_id": YOGURT, "source": "search",
                                          "name": None, "alternatives": [],
                                          "amount": item.amount, "unit": item.unit})
        out = api.create_recipe("Fresh Stew", 1.0, "", [parse_item("100 grams greek yogurt")])
        saved = next(p for m, p in api.fake.calls if m == "saveRecipe")[1]
        assert saved.fields[0].fields[6] is True and saved.fields[0].fields[7] is True
        assert out["created"]["current"] is True
        # ... and so it shows in the default (current-only) list
        assert "Fresh Stew" in [r["name"] for r in api.list_recipes()]

    def test_create_validation(self, api):
        with pytest.raises(ValueError, match="at least one ingredient"):
            api.create_recipe("Empty", 1.0, "", [])
        with pytest.raises(ValueError, match="Servings must be positive"):
            api.create_recipe("Zero", 0, "", [parse_item("1 egg")])

    def test_delete_sends_recipe_array(self, api):
        out = api.delete_recipe("plain rice")
        assert out["deleted"]["name"] == "Plain Rice"
        params = next(p for m, p in api.fake.calls if m == "removeRecipe")
        array = params[1]
        assert array.signature.startswith("[Lcom.loseit.core.client.model.Recipe;")
        assert [r.fields[0].fields[1].fields[3] for r in array.fields[0]] == ["Plain Rice"]
        back = encode_call(api, "removeRecipe", api.token(), array)
        assert back.params[1].fields[0][0].fields[0].fields[1].fields[3] == "Plain Rice"
        assert [r["name"] for r in api.list_recipes()] == ["Test Soup", "Test Soup"]


class TestRecipeFood:
    def test_recipe_food_sizes_like_a_food(self, api):
        food = api._food("aa" * 16)     # getFood returns None for recipes
        assert food is not None and food.type == "FoodForFoodDatabase"
        info = summarize_food(food)
        assert info["name"] == "Test Soup"
        assert info["servings"] == [{"amount": 1.0, "unit": "Serving", "calories": 125.0}]
        size, note = api._serving_for(food, None, None, 2.0)
        assert _calories(food, size) == 250.0
        # a gram-counted recipe takes gram amounts (per-serving = per gram)
        gram_recipe = make_recipe(api, "Fudge", "dd" * 16, servings=320.0, total_calories=1312.0)
        gram_recipe.fields[9] = gram_recipe.fields[11] = GEnum(
            "FoodMeasure", 8, gram_recipe.fields[9].signature)
        api.fake.recipes.append(gram_recipe)
        api._foods.clear()
        food = api._food("dd" * 16)
        size, _ = api._serving_for(food, 40.0, "g", None)
        assert _calories(food, size) == pytest.approx(164.0)

    def test_unknown_food_still_none(self, api):
        assert api._food("ff" * 16) is None


class StubAPI:
    """CLI-level stub: canned recipe data, recorded create/delete calls."""

    instances = []

    def __init__(self):
        self.created = self.deleted = None
        self.list_all = self.get_all = self.delete_all = None
        StubAPI.instances.append(self)

    def list_recipes(self, query=None, all_versions=False):
        self.list_all = all_versions
        return [{"recipe_id": "ab" * 16, "name": "Soup", "servings": 4.0,
                 "serving_unit": "Serving", "calories_per_serving": 125.0,
                 "current": True, "created": "2023-11-14", "ingredient_count": 3}]

    def get_recipe(self, ref, all_versions=False):
        self.get_all = all_versions
        if ref == "missing":
            raise LookupError(f"No recipe matching {ref!r}")
        return {"recipe_id": "ab" * 16, "name": "Soup", "servings": 4.0,
                "serving_unit": "Serving", "calories_per_serving": 125.0,
                "current": True, "created": "2023-11-14",
                "instructions": "Boil.", "ingredients": []}

    def create_recipe(self, name, servings, instructions, items):
        self.created = (name, servings, instructions, items)
        return {"created": {"name": name, "servings": servings, "serving_unit": "Serving",
                            "calories_per_serving": 10.0, "recipe_id": "cd" * 16, "ingredients": []}}

    def delete_recipe(self, ref, all_versions=False):
        self.deleted = ref
        self.delete_all = all_versions
        return {"deleted": {"name": "Soup", "recipe_id": "ab" * 16}}


class TestCLI:
    @pytest.fixture(autouse=True)
    def stub(self, monkeypatch):
        StubAPI.instances = []
        monkeypatch.setattr("loseit.client.api.LoseItAPI", StubAPI)

    def invoke(self, args):
        from click.testing import CliRunner
        from loseit.cli.main import main
        return CliRunner().invoke(main, args)

    def test_recipes_json(self):
        result = self.invoke(["recipes"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["count"] == 1 and data["recipes"][0]["name"] == "Soup"
        assert StubAPI.instances[-1].list_all is False

    def test_recipes_all_flag(self):
        result = self.invoke(["recipes", "--all"])
        assert result.exit_code == 0
        assert StubAPI.instances[-1].list_all is True

    def test_recipe_all_flag(self):
        result = self.invoke(["recipe", "soup", "--all"])
        assert result.exit_code == 0
        assert StubAPI.instances[-1].get_all is True

    def test_recipe_not_found_exit_code(self):
        result = self.invoke(["recipe", "missing"])
        assert result.exit_code == 4
        assert json.loads(result.output)["error"]["type"] == "not_found"

    def test_recipe_create_args(self):
        result = self.invoke(["recipe-create", "Stew", "--servings", "3",
                              "--instructions", "Simmer.", "100g yogurt", "1 tbsp honey"])
        assert result.exit_code == 0
        name, servings, instructions, items = StubAPI.instances[-1].created
        assert (name, servings, instructions) == ("Stew", 3.0, "Simmer.")
        assert [(i.amount, i.unit, i.query) for i in items] == [
            (100.0, "gram", "yogurt"), (1.0, "tablespoon", "honey")]

    def test_recipe_delete(self):
        result = self.invoke(["recipe-delete", "soup"])
        assert result.exit_code == 0
        assert StubAPI.instances[-1].deleted == "soup"
        assert StubAPI.instances[-1].delete_all is False
        assert json.loads(result.output)["deleted"]["name"] == "Soup"

    def test_recipe_delete_all_flag(self):
        result = self.invoke(["recipe-delete", "soup", "--all"])
        assert result.exit_code == 0
        assert StubAPI.instances[-1].delete_all is True
