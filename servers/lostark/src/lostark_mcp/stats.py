"""Population statistics over collected snapshots.

The sample is the operator's own list of names, not an official ranking, and
every response says how large it is and how old.
"""

from __future__ import annotations

import collections
import re
import statistics
from typing import Any, Iterable

from .fields import compact, epoch_to_kst, kst_iso
from .snapshots import SnapshotStore

ASPECTS = (
    "summary",
    "build",
    "arkpassive",
    "engravings",
    "skills",
    "gems",
    "stats",
    "accessories",
    "bracelet",
    "cards",
    "arkgrid",
    "equipment",
)

_RE_EFFECT = re.compile(r"^(.+?)\s*\+\s*([\d.,]+%?)$")


def _dist(values: Iterable[float | None]) -> dict[str, float] | None:
    data = sorted(v for v in values if v is not None)
    if not data:
        return None
    if len(data) == 1:
        return {"n": 1, "median": data[0]}
    q1, q2, q3 = statistics.quantiles(data, n=4, method="inclusive")
    return {
        "n": len(data),
        "min": data[0],
        "p25": round(q1, 2),
        "median": round(q2, 2),
        "p75": round(q3, 2),
        "max": data[-1],
    }


def _share(counter: collections.Counter, n: int, top: int) -> list[dict[str, Any]]:
    return [
        {"value": value, "count": count, "pct": round(count / n * 100, 1)}
        for value, count in counter.most_common(top)
    ]


def _split_effect(line: str) -> tuple[str, str]:
    match = _RE_EFFECT.match(line)
    return (match.group(1), match.group(2)) if match else (line, "")


def build_stats(
    store: SnapshotStore,
    *,
    aspect: str,
    character_class: str | None,
    min_item_level: float | None = None,
    build: str | None = None,
    max_age_days: float | None = 30,
    top: int = 10,
) -> dict[str, Any]:
    if aspect not in ASPECTS:
        raise ValueError(f"aspect must be one of {ASPECTS}")
    rows = store.latest(
        character_class=character_class,
        min_item_level=min_item_level,
        max_age_days=max_age_days,
        build=build,
    )
    if not rows:
        return {
            "error": True,
            "kind": "no_data",
            "message": (
                "No collected snapshots match. Add names to the rankers file and run "
                "`lostark-mcp collect`."
            ),
            "classes_collected": store.classes(),
        }

    docs = [doc for doc, _ in rows]
    n = len(docs)
    body = _ASPECT_FUNCS[aspect](docs, n, top)
    oldest, newest = min(f for _, f in rows), max(f for _, f in rows)
    return compact(
        {
            "as_of_kst": kst_iso(),
            "aspect": aspect,
            "filters": compact(
                {
                    "class": character_class,
                    "min_item_level": min_item_level,
                    "build": build,
                    "max_age_days": max_age_days,
                }
            ),
            "sample": {
                "n": n,
                "oldest_snapshot_kst": epoch_to_kst(oldest),
                "newest_snapshot_kst": epoch_to_kst(newest),
                "note": (
                    "Sample = names listed in the rankers file and collected locally; not an "
                    "official ranking. Small samples swing widely."
                ),
            },
            **body,
        }
    )


def _summary(docs, n, top):
    profiles = [d.get("profile") or {} for d in docs]
    return {
        "item_level": _dist(p.get("item_level") for p in profiles),
        "combat_power": _dist(p.get("combat_power") for p in profiles),
        "builds": _share(collections.Counter(p.get("build") or "?" for p in profiles), n, top),
    }


def _build(docs, n, top):
    return {
        "builds": _share(
            collections.Counter((d.get("profile") or {}).get("build") or "?" for d in docs), n, top
        )
    }


def _arkpassive(docs, n, top):
    picks: collections.Counter = collections.Counter()
    levels: dict[tuple, list[int]] = collections.defaultdict(list)
    points: dict[str, list[float]] = collections.defaultdict(list)
    for doc in docs:
        block = doc.get("arkpassive") or {}
        for p in block.get("points") or []:
            points[p.get("name")].append(p.get("value"))
        for node in block.get("nodes") or []:
            key = (node.get("tree"), node.get("tier"), node.get("name"))
            picks[key] += 1
            if node.get("level") is not None:
                levels[key].append(node["level"])
    nodes = [
        compact(
            {
                "tree": tree,
                "tier": tier,
                "node": name,
                "pct": round(count / n * 100, 1),
                "median_level": statistics.median(levels[(tree, tier, name)]) if levels[(tree, tier, name)] else None,
            }
        )
        for (tree, tier, name), count in picks.items()
    ]
    nodes.sort(key=lambda r: (r.get("tree") or "", r.get("tier") or 0, -r["pct"]))
    return {"points": {k: _dist(v) for k, v in points.items()}, "nodes": nodes}


def _engravings(docs, n, top):
    picks: collections.Counter = collections.Counter()
    detail: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for doc in docs:
        for e in doc.get("engravings") or []:
            if isinstance(e, dict):
                picks[e.get("name")] += 1
                detail[e.get("name")][f"{e.get('grade')} Lv{e.get('level')}"] += 1
    return {
        "engravings": [
            {**row, "grades": dict(detail[row["value"]].most_common(4))}
            for row in _share(picks, n, top)
        ]
    }


def _skills(docs, n, top):
    usage: collections.Counter = collections.Counter()
    levels: dict[str, list[int]] = collections.defaultdict(list)
    tripods: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    runes: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for doc in docs:
        for s in doc.get("skills") or []:
            if not isinstance(s, dict):
                continue
            name = s.get("name")
            usage[name] += 1
            levels[name].append(s.get("level") or 0)
            combo = " / ".join(t.get("name", "") for t in s.get("tripods") or [])
            if combo:
                tripods[name][combo] += 1
            rune = s.get("rune") or {}
            if rune:
                runes[name][f"{rune.get('name')}({rune.get('grade')})"] += 1
    rows = []
    for name, count in usage.most_common(top * 2):
        rows.append(
            compact(
                {
                    "skill": name,
                    "pct": round(count / n * 100, 1),
                    "median_level": statistics.median(levels[name]),
                    "tripods": _share(tripods[name], count, 3),
                    "runes": _share(runes[name], count, 3),
                }
            )
        )
    return {"skills": rows}


def _gems(docs, n, top):
    per_skill: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    levels: collections.Counter = collections.Counter()
    for doc in docs:
        for g in (doc.get("gems") or {}).get("gems") or []:
            label = f"{g.get('level')}레벨 {g.get('kind')}"
            levels[label] += 1
            if g.get("skill"):
                per_skill[g["skill"]][g.get("kind")] += 1
    skills = sorted(
        (
            {
                "skill": skill,
                "pct_with_gem": round(sum(kinds.values()) / n * 100, 1),
                "kinds": dict(kinds.most_common()),
            }
            for skill, kinds in per_skill.items()
        ),
        key=lambda r: -r["pct_with_gem"],
    )
    return {
        "gems_per_character": round(sum(levels.values()) / n, 1),
        "levels": [{"gem": k, "avg_per_character": round(v / n, 2)} for k, v in levels.most_common(top)],
        "skills": skills[: top * 2],
    }


def _stats(docs, n, top):
    values: dict[str, list[float]] = collections.defaultdict(list)
    for doc in docs:
        for s in (doc.get("stats") or {}).get("stats") or []:
            if s.get("type") in ("치명", "특화", "제압", "신속", "인내", "숙련"):
                values[s["type"]].append(s.get("value"))
    return {"combat_stats": {k: _dist(v) for k, v in values.items()}}


def _effects(docs, n, top, getter) -> list[dict[str, Any]]:
    picks: collections.Counter = collections.Counter()
    vals: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    holders = 0
    for doc in docs:
        for item_lines in getter(doc):
            holders += 1
            for line in item_lines:
                name, value = _split_effect(line)
                picks[name] += 1
                if value:
                    vals[name][value] += 1
    return [
        {
            "effect": name,
            "pct_of_items": round(count / holders * 100, 1) if holders else None,
            "values": dict(vals[name].most_common(4)),
        }
        for name, count in picks.most_common(top * 2)
    ]


def _accessories(docs, n, top):
    def getter(doc):
        return [(a.get("effects") or {}).get("연마 효과", []) for a in doc.get("accessories") or [] if isinstance(a, dict)]

    return {"polish_effects": _effects(docs, n, top, getter)}


def _bracelet(docs, n, top):
    def getter(doc):
        b = doc.get("bracelet")
        return [(b.get("effects") or {}).get("팔찌 효과", [])] if isinstance(b, dict) else []

    return {"bracelet_effects": _effects(docs, n, top, getter)}


def _cards(docs, n, top):
    sets: collections.Counter = collections.Counter()
    for doc in docs:
        for group in (doc.get("cards") or {}).get("set_effects") or []:
            if group:
                sets[group[-1].get("name")] += 1
    return {"set_effects": _share(sets, n, top)}


def _arkgrid(docs, n, top):
    per_slot: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for doc in docs:
        for core in (doc.get("arkgrid") or {}).get("cores") or []:
            slot = f"{core.get('order')}의 {core.get('celestial')}"
            per_slot[slot][f"{core.get('core')} ({core.get('grade')})"] += 1
    return {"cores": {slot: _share(counter, n, 4) for slot, counter in per_slot.items()}}


def _equipment(docs, n, top):
    refine: list[float] = []
    advanced: list[float] = []
    quality: list[float] = []
    for doc in docs:
        for item in doc.get("equipment") or []:
            if isinstance(item, dict):
                refine.append(item.get("refine"))
                advanced.append(item.get("advanced_refine"))
                quality.append(item.get("quality"))
    return {"refine": _dist(refine), "advanced_refine": _dist(advanced), "quality": _dist(quality)}


_ASPECT_FUNCS = {
    "summary": _summary,
    "build": _build,
    "arkpassive": _arkpassive,
    "engravings": _engravings,
    "skills": _skills,
    "gems": _gems,
    "stats": _stats,
    "accessories": _accessories,
    "bracelet": _bracelet,
    "cards": _cards,
    "arkgrid": _arkgrid,
    "equipment": _equipment,
}
