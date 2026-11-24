"""Icon vocabulary, fuzzy filtering, the auto-picker, and the icon CLI.

Offline: the picker's search function is faked with fixed results, and the
write path is exercised in test_recipes.py. Example names are generic.
"""
import json

import pytest
from click.testing import CliRunner

from loseit.cli.main import main
from loseit.client.icons import (
    filter_icons, icon_tokens, icon_vocabulary, name_tokens, pick_icon,
)

VOCAB = ["Recipe", "Cake", "Chocolate", "Cupcake", "CupcakeVanilla",
         "FrostingChocolate", "FrostingYellow", "Banana", "Toast", "Stew",
         "SourCream", "BeanBlack"]


def search_from(hits):
    def fn(query, limit=25, details=False):
        return hits
    return fn


class TestVocabulary:
    def test_shipped_vocabulary(self):
        vocab = icon_vocabulary()
        assert list(vocab) == sorted(vocab)
        assert len(vocab) >= 100
        for known in ("Cake", "FrostingChocolate", "FrostingYellow",
                      "Cupcake", "Toast"):
            assert known in vocab

    def test_tokens(self):
        assert icon_tokens("FrostingChocolate") == {"frosting", "chocolate"}
        assert icon_tokens("BeanBlack") == {"bean", "black"}
        assert name_tokens("Black Beans, Cooked") == {"black", "bean", "cooked"}
        assert name_tokens("Mom's Cake!") == {"mom", "cake"}


class TestFilter:
    def test_substring(self):
        assert filter_icons("frost", VOCAB) == ["FrostingChocolate", "FrostingYellow"]

    def test_token_match(self):
        assert filter_icons("choc frost", VOCAB) == ["FrostingChocolate"]
        assert filter_icons("frosting chocolate", VOCAB) == ["FrostingChocolate"]

    def test_no_query_returns_all_and_no_match_empty(self):
        assert filter_icons(None, VOCAB) == VOCAB
        assert filter_icons("", VOCAB) == VOCAB
        assert filter_icons("zzzzz", VOCAB) == []


class TestPick:
    def test_cake_from_database_evidence(self):
        hits = [
            {"name": "Chocolate Cake", "category": "Cake"},
            {"name": "Cake, Chocolate, w/ Frosting", "category": "Cake"},
            {"name": "Chocolate Bar", "category": "Chocolate"},
            {"name": "Cupcake, Chocolate", "category": "Cupcake"},
        ]
        pick = pick_icon("Chocolate Cake", search_from(hits), VOCAB)
        assert pick["icon"] == "Cake"
        assert pick["evidence"] and all(e["category"] == "Cake" for e in pick["evidence"])
        assert 0 < pick["confidence"] <= 1

    def test_frosting_prefers_compound_icon(self):
        hits = [
            {"name": "Frosting, Chocolate", "category": "FrostingChocolate"},
            {"name": "Frosting, Chocolate, Creamy", "category": "FrostingChocolate"},
            {"name": "Chocolate Cupcake w/ Frosting", "category": "Cupcake"},
            {"name": "Frosting, Vanilla", "category": "FrostingYellow"},
        ]
        pick = pick_icon("Chocolate Frosting", search_from(hits), VOCAB)
        assert pick["icon"] == "FrostingChocolate"
        assert pick["evidence"][0]["name"] == "Frosting, Chocolate"

    def test_weak_evidence_no_pick(self):
        hits = [{"name": "Beef Stew", "category": "Stew"}]
        assert pick_icon("Mystery Dish", search_from(hits), VOCAB) is None
        assert pick_icon("Mystery Dish", search_from([]), VOCAB) is None

    def test_unusable_hits_are_ignored(self):
        # the generic "Recipe" category and values outside the vocabulary
        # are not evidence; with nothing else, no pick
        hits = [{"name": "Mystery Dish", "category": "Recipe"},
                {"name": "Mystery Dish", "category": "NotAnIcon"}]
        assert pick_icon("Mystery Dish", search_from(hits), VOCAB) is None

    def test_search_failure_falls_back_to_vocabulary(self):
        def boom(query, limit=25, details=False):
            raise RuntimeError("offline")
        pick = pick_icon("Banana Muffins", boom, VOCAB)
        assert pick["icon"] == "Banana" and pick["evidence"] == []
        assert pick_icon("Mystery Dish", boom, VOCAB) is None


class IconStubAPI:
    """CLI-level stub for recipe-icon: canned pick/set results."""

    instances = []

    def __init__(self):
        self.icon_sets = []
        IconStubAPI.instances.append(self)

    def get_recipe(self, ref, all_versions=False):
        if ref == "missing":
            raise LookupError(f"No recipe matching {ref!r}")
        name = "Mystery Dish" if ref == "mystery" else "Chocolate Frosting"
        return {"name": name, "recipe_id": "ab" * 16}

    def pick_icon(self, name):
        if name == "Mystery Dish":
            return None
        return {"icon": "FrostingChocolate", "confidence": 0.61, "score": 1.97,
                "evidence": [{"name": "Frosting, Chocolate",
                              "category": "FrostingChocolate", "score": 1.0}]}

    def set_recipe_icon(self, ref, icon, all_versions=False):
        self.icon_sets.append((ref, icon, all_versions))
        return {"recipe_id": "ab" * 16, "name": "Chocolate Frosting", "icon": icon,
                "identifier_category": icon, "recipe_icon": icon, "previous_icon": ""}


class TestCLI:
    @pytest.fixture(autouse=True)
    def stub(self, monkeypatch):
        IconStubAPI.instances = []
        monkeypatch.setattr("loseit.client.api.LoseItAPI", IconStubAPI)

    def invoke(self, args, env=None):
        return CliRunner().invoke(main, args, env=env)

    def test_icons_json(self):
        result = self.invoke(["icons", "frost"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "FrostingChocolate" in data["icons"]
        assert data["count"] == len(data["icons"]) <= data["total"]

    def test_icons_human_lines(self):
        result = self.invoke(["icons", "frost"], env={"LOSEIT_OUTPUT": "human"})
        assert result.exit_code == 0
        assert result.output.splitlines() == filter_icons("frost")

    def test_icons_no_match(self):
        result = self.invoke(["icons", "zzzzz"])
        assert result.exit_code == 0
        assert json.loads(result.output)["icons"] == []

    def test_recipe_icon_auto_picks(self):
        result = self.invoke(["recipe-icon", "frosting"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["icon"] == "FrostingChocolate"
        assert data["pick"]["icon"] == "FrostingChocolate"
        assert IconStubAPI.instances[-1].icon_sets == [
            ("frosting", "FrostingChocolate", False)]

    def test_recipe_icon_explicit(self):
        result = self.invoke(["recipe-icon", "frosting", "Cake"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["icon"] == "Cake" and "pick" not in data
        assert IconStubAPI.instances[-1].icon_sets == [("frosting", "Cake", False)]

    def test_recipe_icon_no_pick(self):
        result = self.invoke(["recipe-icon", "mystery"])
        assert result.exit_code == 1
        assert json.loads(result.output)["error"]["type"] == "no_pick"
        assert IconStubAPI.instances[-1].icon_sets == []

    def test_recipe_icon_not_found(self):
        result = self.invoke(["recipe-icon", "missing"])
        assert result.exit_code == 4
        assert json.loads(result.output)["error"]["type"] == "not_found"
