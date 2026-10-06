"""Local index of foods the user has logged, ranked like the app's recents.

Built from DailyDetails (any summary/log call feeds it, and it backfills the
last REFRESH_DAYS with one range call when stale). Lets "log a bagel" pick
the bagel the user actually ate yesterday — with the amount they logged —
instead of the first generic search hit.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from .parse import normalize_words

REFRESH_DAYS = 30
STALE_AFTER = timedelta(hours=6)
MAX_ENTRIES_PER_FOOD = 30
MAX_SEEN = 500


def _path():
    from . import session as session_mod  # read at call time (tests patch it)
    return session_mod.SESSION_DIR / "recent-foods.json"


class RecentFoods:
    def __init__(self, data: dict | None = None):
        self.data = data or {"refreshed_at": None, "foods": {}}

    @classmethod
    def load(cls) -> "RecentFoods":
        try:
            return cls(json.loads(_path().read_text()))
        except (FileNotFoundError, json.JSONDecodeError):
            return cls()

    def save(self) -> None:
        p = _path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.data))

    @property
    def stale(self) -> bool:
        ts = self.data.get("refreshed_at")
        if not ts:
            return True
        return datetime.now(timezone.utc) - datetime.fromisoformat(ts) > STALE_AFTER

    def mark_refreshed(self) -> None:
        self.data["refreshed_at"] = datetime.now(timezone.utc).isoformat()

    def record_day(self, day: str, foods: list[dict]) -> None:
        """Record a day's food entries (summary 'foods' dicts). Replaces that
        day's previous record so deleted/edited entries are reflected."""
        index = self.data["foods"]
        for info in index.values():
            info["entries"] = {k: v for k, v in info["entries"].items() if v["date"] != day}
        for f in foods:
            info = index.setdefault(f["food_id"], {"entries": {}})
            info.update(name=f["name"], brand=f.get("brand", ""), category=f.get("category", ""))
            info["entries"][f["entry_id"]] = {
                "date": day, "amount": f["amount"], "unit": f["unit"],
                "meal": f["meal"], "calories": f["calories"],
            }
            if len(info["entries"]) > MAX_ENTRIES_PER_FOOD:
                keep = sorted(info["entries"].items(), key=lambda kv: kv[1]["date"])[-MAX_ENTRIES_PER_FOOD:]
                info["entries"] = dict(keep)
        self.data["foods"] = {k: v for k, v in index.items() if v["entries"]}

    def ranked(self, query: str | None = None, limit: int = 20) -> list[dict]:
        """Foods ordered by (match quality, last eaten, times eaten)."""
        words = normalize_words(query) if query else []
        out = []
        for food_id, info in self.data["foods"].items():
            entries = sorted(info["entries"].values(), key=lambda e: e["date"])
            if not entries:
                continue
            score = _match_score(words, info) if words else 1.0
            if score <= 0:
                continue
            primary = _primary_hit(words, info) if words else True
            last = entries[-1]
            out.append({
                "food_id": food_id, "name": info["name"], "brand": info.get("brand", ""),
                "category": info.get("category", ""), "times": len(entries),
                "last_date": last["date"], "last_amount": last["amount"], "last_unit": last["unit"],
                "last_meal": last["meal"], "last_calories": last["calories"],
                "_key": (score, primary, last["date"], len(entries)),
            })
        out.sort(key=lambda r: r["_key"], reverse=True)
        for r in out:
            del r["_key"]
        return out[:limit]

    def remember(self, foods: list[dict]) -> None:
        """Remember foods seen in search results so their id prefixes resolve."""
        seen = self.data.setdefault("seen", {})
        for f in foods:
            seen.pop(f["food_id"], None)
            seen[f["food_id"]] = {"name": f["name"], "brand": f.get("brand", "")}
        for fid in list(seen)[:-MAX_SEEN]:
            del seen[fid]

    def find_by_prefix(self, prefix: str) -> list[str]:
        ids = list(self.data["foods"]) + [f for f in self.data.get("seen", {}) if f not in self.data["foods"]]
        return [fid for fid in ids if fid.startswith(prefix)]


def _match_score(words: list[str], info: dict) -> float:
    """1.0 when every query word is in the name/brand/category; partial
    matches score lower; 0 when fewer than all-but-one words match."""
    hay = set(normalize_words(f"{info['name']} {info.get('brand', '')} {info.get('category', '')}"))
    hits = sum(1 for w in words if w in hay or any(h.startswith(w) for h in hay if len(w) >= 3))
    if hits < len(words) - (1 if len(words) >= 3 else 0) or hits == 0:
        return 0.0
    return hits / len(words)


def _primary_hit(words: list[str], info: dict) -> bool:
    """Is the query about this food's main thing (category or first name word)?
    e.g. "eggs" -> "Eggs, Large" (category Egg), not "Sausage, Egg & Cheese"."""
    primary = set(normalize_words(info.get("category", ""))) | set(normalize_words(info["name"])[:1])
    return any(w in primary for w in words)


def rank_search_results(query: str, results: list[dict]) -> list[dict]:
    """Re-rank server search hits: all words matched first, then fewer extra
    words (so "carrot" prefers "Carrots, Medium" over "Carrot cake w/…"),
    keeping server order as the tiebreak."""
    words = normalize_words(query)

    def key(pair):
        i, r = pair
        name = normalize_words(f"{r['name']} {r.get('brand', '')}")
        hits = sum(1 for w in words if w in name)
        return (-hits, len(name) - hits, i)

    return [r for _, r in sorted(enumerate(results), key=key)]

