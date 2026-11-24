"""Recipe/food icons: the category vocabulary and an auto-picker.

A food's icon is its category string ("Cake", "FrostingChocolate", ...).
The vocabulary is a fixed set on LoseIt's side with no enumerating API, so
icons.json ships the values harvested from real database foods, recipes and
custom foods. Matching tokenizes names and icon values the same way
(CamelCase split, lowercase, light de-pluralization) so "FrostingChocolate"
is the token set {frosting, chocolate}.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from importlib.resources import files

from .parse import _word_parts, normalize_words

#: The generic category a recipe starts with; never a picker result.
DEFAULT_ICON = "Recipe"

#: Search evidence below this (summed Jaccard of the best hits) is too thin
#: to trust on its own; a perfect vocabulary-token match can still carry it.
_MIN_EVIDENCE = 0.5
_MIN_EVIDENCE_WITH_VOCAB = 0.34
_MIN_CONFIDENCE = 0.3
#: Confidence assigned to a pure vocabulary fallback pick (no search hits).
_FALLBACK_CONFIDENCE = 0.4


@lru_cache(maxsize=1)
def icon_vocabulary() -> tuple[str, ...]:
    """The known icon/category values, sorted (icons.json, harvested live)."""
    data = files("loseit.client").joinpath("icons.json").read_text()
    return tuple(json.loads(data))


def icon_tokens(icon: str) -> set[str]:
    """An icon's word tokens: "FrostingChocolate" -> {frosting, chocolate}."""
    tokens = set()
    for part in _word_parts(icon):
        tokens.update(normalize_words(part))
    return tokens


def name_tokens(name: str) -> set[str]:
    """A food/recipe name's word tokens, tokenized like icon_tokens.
    Single letters are dropped (possessive remnants, "w/" abbreviations)."""
    tokens = set()
    for word in re.findall(r"[A-Za-z0-9]+", name or ""):
        for part in _word_parts(word):
            tokens.update(normalize_words(part))
    return {t for t in tokens if len(t) > 1}


def filter_icons(query: str | None, vocabulary=None) -> list[str]:
    """Vocabulary filtered by a fuzzy query: substring match, or every query
    word prefix-matching one of the icon's CamelCase tokens ("choc frost"
    finds FrostingChocolate). No query returns the whole vocabulary."""
    vocab = list(vocabulary) if vocabulary is not None else list(icon_vocabulary())
    if not query:
        return vocab
    needle = query.lower()
    words = name_tokens(query)
    out = []
    for icon in vocab:
        if needle in icon.lower():
            out.append(icon)
            continue
        tokens = icon_tokens(icon)
        if words and all(any(t.startswith(w) or w.startswith(t) for t in tokens)
                         for w in words):
            out.append(icon)
    return out


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def pick_icon(name: str, search_fn, vocabulary=None) -> dict | None:
    """Pick the best icon for a food/recipe name, or None when the evidence
    is too weak to do better than a guess.

    Primary signal: database evidence. `search_fn(query, limit, details)`
    (LoseItAPI.search_foods) finds real foods named like `name`; each hit
    votes for its own category with the Jaccard similarity of the hit's name
    tokens to the target's, and a category's evidence is the sum of its
    three best votes. The same food listed by many users (identical names
    under one category) votes only once — duplicates are one food's
    evidence, not several. Secondary signal: how much of the icon's own
    tokens appear in the name (a boost, and a fallback when search finds
    nothing).
    Returns {"icon", "confidence", "evidence", "score"} where evidence lists
    the winning category's best hits as {"name", "category", "score"}.
    """
    vocab = list(vocabulary) if vocabulary is not None else list(icon_vocabulary())
    vocab_set = set(vocab)
    target = name_tokens(name)
    if not target:
        return None

    try:
        hits = search_fn(name, limit=25, details=False) or []
    except Exception:
        hits = []
    votes: dict[str, dict[str, tuple[float, str]]] = {}
    for hit in hits:
        category = hit.get("category") or ""
        if category not in vocab_set or category == DEFAULT_ICON:
            continue
        sim = _jaccard(target, name_tokens(hit.get("name", "")))
        if sim > 0:
            per_name = votes.setdefault(category, {})
            key = (hit.get("name") or "").strip().lower()
            if sim > per_name.get(key, (0.0, ""))[0]:
                per_name[key] = (sim, hit.get("name", ""))

    scored = []
    for icon in vocab:
        if icon == DEFAULT_ICON:
            continue
        icon_votes = sorted(votes.get(icon, {}).values(), reverse=True)
        evidence = sum(sim for sim, _ in icon_votes[:3])
        tokens = icon_tokens(icon)
        overlap = len(tokens & target) / len(tokens) if tokens else 0.0
        total = evidence + 0.3 * overlap
        if total > 0:
            scored.append((total, icon, evidence, overlap, icon_votes))
    scored.sort(key=lambda row: (-row[0], row[1]))

    if scored:
        total, icon, evidence, overlap, icon_votes = scored[0]
        second = scored[1][0] if len(scored) > 1 else 0.0
        confidence = total / (total + second + 0.75)
        strong = evidence >= _MIN_EVIDENCE or (
            overlap >= 1.0 and evidence >= _MIN_EVIDENCE_WITH_VOCAB)
        if strong and confidence >= _MIN_CONFIDENCE:
            return {"icon": icon, "confidence": round(confidence, 2),
                    "score": round(total, 2),
                    "evidence": [{"name": hit_name, "category": icon,
                                  "score": round(sim, 2)}
                                 for sim, hit_name in icon_votes[:3]]}

    # Fallback: an icon whose tokens all appear in the name ("Banana
    # Muffins" -> Banana). Most tokens wins (most specific); ties by name.
    subsets = [(len(icon_tokens(icon)), icon) for icon in vocab
               if icon != DEFAULT_ICON and icon_tokens(icon)
               and icon_tokens(icon) <= target]
    if subsets:
        subsets.sort(key=lambda row: (-row[0], row[1]))
        icon = subsets[0][1]
        return {"icon": icon, "confidence": _FALLBACK_CONFIDENCE, "score": 0.0,
                "evidence": []}
    return None
