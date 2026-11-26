"""Character data from the armory (전투정보실).

One call to /armories/characters/{name} returns every section, about 500 KB of
which most is tooltip markup. It is fetched once and cached; every view below
is cut from that cached copy, so drilling into detail costs no quota.

Detail levels:
  brief     one line per item
  standard  parsed fields
  full      standard plus the tooltip as plain-text lines
"""

from __future__ import annotations

import collections
import json
import re
from typing import Any

from . import tooltip
from .cache import TTL_ARMORY, TTL_ROSTER, TtlCache
from .client import LostarkClient, LostarkError, quote_name
from .fields import clean, compact, integer, kst_iso, lines, num

SECTIONS = (
    "profile",
    "stats",
    "equipment",
    "accessories",
    "bracelet",
    "ability_stone",
    "misc_equipment",
    "engravings",
    "gems",
    "cards",
    "skills",
    "arkpassive",
    "arkgrid",
    "avatars",
    "collectibles",
    "colosseums",
)
DETAIL_LEVELS = ("brief", "standard", "full")

ARMOR_SLOTS = ("무기", "투구", "상의", "하의", "장갑", "어깨")
ACCESSORY_SLOTS = ("목걸이", "귀걸이", "반지")
BRACELET_SLOT = "팔찌"
STONE_SLOT = "어빌리티 스톤"
COMBAT_STATS = ("치명", "특화", "제압", "신속", "인내", "숙련")

# Responses above this size are cut down rather than flooding the context.
MAX_RESPONSE_CHARS = 40_000

_ARK_POINTS_LABEL = "아크 패시브 포인트 효과"
_PENALTY_COLOR = "#fe2e2e"  # red: the penalty engraving on an ability stone

_RE_REFINE = re.compile(r"^\+(\d+)\s")
_RE_ITEM_LEVEL = re.compile(r"아이템 레벨\s*(\d+)")
_RE_TIER = re.compile(r"티어\s*(\d+)")
_RE_ADV_REFINE = re.compile(r"\[상급 재련\]\s*(\d+)\s*단계")
_RE_POINTS = re.compile(r"^(\S+)\s*\+(\d+)")
_RE_STONE = re.compile(r"\[(.+?)\].*?Lv\.(\d+)")
_RE_GEM = re.compile(r"(\d+)레벨\s+(.+?)의 보석")
_RE_NODE = re.compile(r"^(\S+)\s+(\d+)티어\s+(.+?)\s+Lv\.(\d+)$")
_RE_CORE = re.compile(r"^(질서|혼돈)의\s+(해|달|별)\s+코어\s*:\s*(.+)$")
_RE_CORE_OPTION = re.compile(r"^\[(\d+)P\]\s*(.+)$")


def _first_int(pattern: re.Pattern[str], *texts: str | None) -> int | None:
    for text in texts:
        if text:
            match = pattern.search(text)
            if match:
                return int(match.group(1))
    return None


def _keep(keys: list[str | None], match: list[str] | None) -> bool:
    """Substring filter used by every list section."""
    if not match:
        return True
    needles = [m.strip() for m in match if m and m.strip()]
    if not needles:
        return True
    return any(needle in key for key in keys if key for needle in needles)


def _ark_points(body: list[str]) -> dict[str, int] | None:
    points: dict[str, int] = {}
    for line in body:
        match = _RE_POINTS.match(line)
        if match:
            points[match.group(1)] = int(match.group(2))
    return points or None


def _points_text(points: dict[str, int] | None) -> str | None:
    return " ".join(f"{k}+{v}" for k, v in points.items()) if points else None


class Armory:
    """A parsed view over one raw armory response."""

    def __init__(self, raw: dict[str, Any]) -> None:
        self.raw = raw
        self._gem_rows: list[dict[str, Any]] | None = None

    # ------------------------------------------------------------------
    # profile and stats
    # ------------------------------------------------------------------

    def _profile_raw(self) -> dict[str, Any]:
        return self.raw.get("ArmoryProfile") or {}

    @property
    def name(self) -> str | None:
        return self._profile_raw().get("CharacterName")

    @property
    def build(self) -> str | None:
        """The ark passive title, which names the build (e.g. "잔재된 기운")."""
        return clean((self.raw.get("ArkPassive") or {}).get("Title"))

    def profile(self) -> dict[str, Any]:
        p = self._profile_raw()
        used, total = p.get("UsingSkillPoint"), p.get("TotalSkillPoint")
        return compact(
            {
                "name": p.get("CharacterName"),
                "server": p.get("ServerName"),
                "class": p.get("CharacterClassName"),
                "character_level": p.get("CharacterLevel"),
                "item_level": num(p.get("ItemAvgLevel")),
                "combat_power": num(p.get("CombatPower")),
                "build": self.build,
                "expedition_level": p.get("ExpeditionLevel"),
                "title": clean(p.get("Title")),
                "guild": compact({"name": p.get("GuildName"), "grade": p.get("GuildMemberGrade")})
                or None,
                "estate": compact({"name": p.get("TownName"), "level": p.get("TownLevel")}) or None,
                "skill_points": f"{used}/{total}" if used is not None else None,
                "honor_point": p.get("HonorPoint"),
            }
        )

    def _stats(self, detail: str) -> Any:
        stats = self._profile_raw().get("Stats") or []
        if detail == "brief":
            combat = [s for s in stats if s.get("Type") in COMBAT_STATS]
            combat.sort(key=lambda s: integer(s.get("Value")) or 0, reverse=True)
            others = [s for s in stats if s.get("Type") not in COMBAT_STATS]
            return " · ".join(f"{s['Type']} {integer(s.get('Value'))}" for s in combat + others)
        rows = []
        for s in stats:
            row: dict[str, Any] = {"type": s.get("Type"), "value": integer(s.get("Value"))}
            if detail == "full":
                row["tooltip"] = [line for t in s.get("Tooltip") or [] for line in lines(t)]
            rows.append(row)
        tendencies = [
            {"type": t.get("Type"), "point": t.get("Point"), "max": t.get("MaxPoint")}
            for t in self._profile_raw().get("Tendencies") or []
        ]
        return compact({"stats": rows, "tendencies": tendencies})

    # ------------------------------------------------------------------
    # equipment
    # ------------------------------------------------------------------

    def _equipment(self, slots: tuple[str, ...] | None, *, exclude: tuple[str, ...] = ()) -> list[dict]:
        items = self.raw.get("ArmoryEquipment") or []
        if slots is not None:
            return [i for i in items if i.get("Type") in slots]
        return [i for i in items if i.get("Type") not in exclude]

    @staticmethod
    def _gear(item: dict[str, Any], detail: str) -> tuple[dict[str, Any], list[tooltip.Element]]:
        elements = tooltip.parse(item.get("Tooltip"))
        head = tooltip.title(elements)
        parts = tooltip.part_boxes(elements)
        texts = tooltip.texts(elements)
        name = clean(item.get("Name")) or ""
        level_line = head.get("level_line")
        points = _ark_points(parts.pop(_ARK_POINTS_LABEL, []))
        extra = {
            top: [text for _, text in entries]
            for top, entries in tooltip.indent_groups(elements).items()
        }
        data = compact(
            {
                "slot": item.get("Type"),
                "name": name,
                "grade": item.get("Grade"),
                "tier": _first_int(_RE_TIER, level_line),
                "item_level": _first_int(_RE_ITEM_LEVEL, level_line),
                "quality": head.get("quality"),
                "refine": _first_int(_RE_REFINE, name),
                "advanced_refine": _first_int(_RE_ADV_REFINE, *texts),
                "ark_passive_points": points,
                "effects": {k: v for k, v in parts.items() if k and v},
                "extra": extra,
            }
        )
        if detail == "full":
            data["tooltip"] = tooltip.flatten(elements)
        return data, elements

    def _armor(self, detail: str, match: list[str] | None) -> list[Any]:
        out: list[Any] = []
        for item in self._equipment(ARMOR_SLOTS):
            data, _ = self._gear(item, detail)
            if not _keep([data.get("slot"), data.get("name")], match):
                continue
            if detail == "brief":
                bits = [data.get("slot")]
                if data.get("refine") is not None:
                    bits.append(f"+{data['refine']}")
                if data.get("advanced_refine") is not None:
                    bits.append(f"상급재련{data['advanced_refine']}")
                if data.get("quality") is not None:
                    bits.append(f"품질{data['quality']}")
                if data.get("item_level") is not None:
                    bits.append(f"(Lv{data['item_level']})")
                out.append(" ".join(str(b) for b in bits))
            else:
                out.append(data)
        return out

    def _accessories(self, detail: str, match: list[str] | None) -> list[Any]:
        out: list[Any] = []
        for item in self._equipment(ACCESSORY_SLOTS):
            data, _ = self._gear(item, detail)
            if not _keep([data.get("slot"), data.get("name")], match):
                continue
            if detail == "brief":
                polish = " · ".join(data.get("effects", {}).get("연마 효과", []))
                head = f"{data.get('slot')} 품질{data.get('quality')}"
                tail = _points_text(data.get("ark_passive_points"))
                out.append(" | ".join(x for x in (head, polish, tail) if x))
            else:
                out.append(data)
        return out

    def _bracelet(self, detail: str) -> Any:
        items = self._equipment((BRACELET_SLOT,))
        if not items:
            return None
        data, _ = self._gear(items[0], detail)
        if detail == "brief":
            effects = " · ".join(data.get("effects", {}).get("팔찌 효과", []))
            return " | ".join(
                x for x in (data.get("name"), effects, _points_text(data.get("ark_passive_points"))) if x
            )
        return data

    def _ability_stone(self, detail: str) -> Any:
        items = self._equipment((STONE_SLOT,))
        if not items:
            return None
        data, elements = self._gear(items[0], detail)
        engravings = []
        for top, entries in tooltip.indent_groups(elements).items():
            if "각인" not in top:
                continue
            for raw, text in entries:
                match = _RE_STONE.search(text)
                if match:
                    engravings.append(
                        {
                            "name": match.group(1),
                            "level": int(match.group(2)),
                            "penalty": _PENALTY_COLOR in raw.lower(),
                        }
                    )
            data.get("extra", {}).pop(top, None)
        if not data.get("extra"):
            data.pop("extra", None)
        data["engravings"] = engravings
        if detail == "brief":
            return " · ".join(
                f"{'(감소) ' if e['penalty'] else ''}{e['name']} Lv{e['level']}" for e in engravings
            )
        return data

    def _misc_equipment(self, detail: str, match: list[str] | None) -> list[Any]:
        known = ARMOR_SLOTS + ACCESSORY_SLOTS + (BRACELET_SLOT, STONE_SLOT)
        out: list[Any] = []
        for item in self._equipment(None, exclude=known):
            data, _ = self._gear(item, detail)
            if not _keep([data.get("slot"), data.get("name")], match):
                continue
            if detail == "brief":
                out.append(f"{data.get('slot')} {data.get('name')} ({data.get('grade')})")
            else:
                out.append(data)
        return out

    # ------------------------------------------------------------------
    # engravings, gems, cards
    # ------------------------------------------------------------------

    def _engravings(self, detail: str, match: list[str] | None) -> Any:
        block = self.raw.get("ArmoryEngraving") or {}
        out: list[Any] = []
        for e in block.get("ArkPassiveEffects") or []:
            row = compact(
                {
                    "name": e.get("Name"),
                    "grade": e.get("Grade"),
                    "level": e.get("Level"),
                    "ability_stone_level": e.get("AbilityStoneLevel"),
                    "description": clean(e.get("Description")) if detail != "brief" else None,
                }
            )
            if not _keep([row.get("name")], match):
                continue
            if detail == "brief":
                stone = f" (스톤 Lv{row['ability_stone_level']})" if "ability_stone_level" in row else ""
                out.append(f"{row.get('name')} {row.get('grade')} Lv{row.get('level')}{stone}")
            else:
                out.append(row)
        # Characters without ark passive still use the older engraving model.
        for e in block.get("Effects") or []:
            name = clean(e.get("Name"))
            if not _keep([name], match):
                continue
            out.append(name if detail == "brief" else compact({"name": name, "description": clean(e.get("Description"))}))
        return out

    def gem_rows(self) -> list[dict[str, Any]]:
        if self._gem_rows is not None:
            return self._gem_rows
        block = self.raw.get("ArmoryGem") or {}
        effects = {s.get("GemSlot"): s for s in (block.get("Effects") or {}).get("Skills") or []}
        rows = []
        for gem in block.get("Gems") or []:
            name = clean(gem.get("Name")) or ""
            match = _RE_GEM.search(name)
            effect = effects.get(gem.get("Slot")) or {}
            description = effect.get("Description")
            if isinstance(description, list):
                description = " / ".join(clean(d) or "" for d in description)
            rows.append(
                compact(
                    {
                        "slot": gem.get("Slot"),
                        "name": name,
                        "level": gem.get("Level"),
                        "kind": match.group(2) if match else None,
                        "grade": gem.get("Grade"),
                        "bound": True if "(귀속)" in name else None,
                        "skill": clean(effect.get("Name")),
                        "effect": clean(description),
                        "option": clean(effect.get("Option")),
                        "_tooltip": gem.get("Tooltip"),
                    }
                )
            )
        self._gem_rows = rows
        return rows

    def gems_by_skill(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = collections.defaultdict(list)
        for row in self.gem_rows():
            if row.get("skill"):
                out[row["skill"]].append(f"{row.get('level')}레벨 {row.get('kind')}")
        return dict(out)

    def gem_summary(self) -> dict[str, Any]:
        counts = collections.Counter((r.get("level") or 0, r.get("kind")) for r in self.gem_rows())
        ordered = sorted(counts.items(), key=lambda kv: (kv[0][0], kv[0][1] or ""), reverse=True)
        total = clean(((self.raw.get("ArmoryGem") or {}).get("Effects") or {}).get("Description"))
        return compact(
            {
                "count": sum(counts.values()),
                "by_level": " · ".join(f"{lvl}레벨 {kind} ×{n}" for (lvl, kind), n in ordered),
                "total_effect": total,
            }
        )

    def _gems(self, detail: str, match: list[str] | None) -> Any:
        if detail == "brief":
            return self.gem_summary()
        rows = []
        for row in self.gem_rows():
            if not _keep([row.get("skill"), row.get("name")], match):
                continue
            data = {k: v for k, v in row.items() if not k.startswith("_")}
            if detail == "full":
                data["tooltip"] = tooltip.flatten(tooltip.parse(row.get("_tooltip")))
            rows.append(data)
        return compact({"summary": self.gem_summary(), "gems": rows})

    def _cards(self, detail: str, match: list[str] | None) -> Any:
        block = self.raw.get("ArmoryCard") or {}
        groups = []
        for effect in block.get("Effects") or []:
            items = [
                compact({"name": clean(i.get("Name")), "description": clean(i.get("Description"))})
                for i in effect.get("Items") or []
            ]
            if items:
                groups.append(items)
        if detail == "brief":
            return [group[-1].get("name") for group in groups]
        cards = []
        for card in block.get("Cards") or []:
            row = compact(
                {
                    "slot": card.get("Slot"),
                    "name": card.get("Name"),
                    "grade": card.get("Grade"),
                    "awake": card.get("AwakeCount"),
                    "awake_total": card.get("AwakeTotal"),
                }
            )
            if not _keep([row.get("name")], match):
                continue
            if detail == "full":
                row["tooltip"] = tooltip.flatten(tooltip.parse(card.get("Tooltip")))
            cards.append(row)
        return compact({"cards": cards, "set_effects": groups})

    # ------------------------------------------------------------------
    # skills, ark passive, ark grid
    # ------------------------------------------------------------------

    def _skills(self, detail: str, match: list[str] | None, invested_only: bool) -> list[Any]:
        gems = self.gems_by_skill()
        out: list[Any] = []
        for skill in self.raw.get("ArmorySkills") or []:
            selected = [t for t in skill.get("Tripods") or [] if t.get("IsSelected")]
            rune = skill.get("Rune")
            level = skill.get("Level") or 0
            if invested_only and not (level > 1 or rune or selected):
                continue
            name = clean(skill.get("Name")) or ""
            if not _keep([name], match):
                continue
            tripods = [
                compact({"tier": (t.get("Tier") or 0) + 1, "slot": t.get("Slot"), "name": clean(t.get("Name"))})
                for t in selected
            ]
            rune_data = compact({"name": rune.get("Name"), "grade": rune.get("Grade")}) if rune else None
            if detail == "brief":
                bits = [f"{name} Lv{level}"]
                if tripods:
                    bits.append("/".join(t.get("name", "") for t in tripods))
                if rune_data:
                    bits.append(f"룬 {rune_data.get('name')}({rune_data.get('grade')})")
                if name in gems:
                    bits.append("보석 " + ", ".join(gems[name]))
                out.append(" | ".join(bits))
                continue
            row = compact(
                {
                    "name": name,
                    "level": level,
                    "type": skill.get("Type"),
                    "skill_type": skill.get("SkillType") or None,
                    "tripods": tripods,
                    "rune": rune_data,
                    "gems": gems.get(name),
                }
            )
            if detail == "full":
                # Selected tripods, the rune and the gem are already in the skill
                # tooltip. The API leaves the skill's own description empty.
                row["tooltip"] = tooltip.flatten(tooltip.parse(skill.get("Tooltip")))
            out.append(row)
        return out

    def _arkpassive(self, detail: str, match: list[str] | None) -> Any:
        block = self.raw.get("ArkPassive") or {}
        points = [
            compact({"name": p.get("Name"), "value": p.get("Value"), "rank": clean(p.get("Description"))})
            for p in block.get("Points") or []
        ]
        nodes = []
        for effect in block.get("Effects") or []:
            text = clean(effect.get("Description")) or ""
            parsed = _RE_NODE.match(text)
            node: dict[str, Any] = (
                {
                    "tree": parsed.group(1),
                    "tier": int(parsed.group(2)),
                    "name": parsed.group(3),
                    "level": int(parsed.group(4)),
                }
                if parsed
                else {"tree": effect.get("Name"), "text": text}
            )
            if not _keep([node.get("tree"), node.get("name")], match):
                continue
            if detail == "full":
                node["tooltip"] = tooltip.flatten(tooltip.parse(effect.get("ToolTip")))
            nodes.append(node)

        if detail == "brief":
            by_tree: dict[str, list[str]] = collections.defaultdict(list)
            for node in nodes:
                by_tree[node.get("tree") or "?"].append(
                    f"{node.get('name', node.get('text'))}{node.get('level', '')}"
                )
            return compact(
                {
                    "build": self.build,
                    "points": " · ".join(f"{p.get('name')} {p.get('value')}" for p in points),
                    **{tree: " · ".join(items) for tree, items in by_tree.items()},
                }
            )
        return compact(
            {"build": self.build, "enabled": block.get("IsArkPassive"), "points": points, "nodes": nodes}
        )

    def _arkgrid(self, detail: str, match: list[str] | None) -> Any:
        block = self.raw.get("ArkGrid") or {}
        cores = []
        for slot in block.get("Slots") or []:
            name = clean(slot.get("Name")) or ""
            parsed = _RE_CORE.match(name)
            elements = tooltip.parse(slot.get("Tooltip"))
            parts = tooltip.part_boxes(elements)
            point = integer(slot.get("Point")) or 0
            if not _keep([name], match):
                continue
            core = compact(
                {
                    "index": slot.get("Index"),
                    "name": name,
                    "order": parsed.group(1) if parsed else None,
                    "celestial": parsed.group(2) if parsed else None,
                    "core": parsed.group(3) if parsed else None,
                    "grade": slot.get("Grade"),
                    "points": point,
                }
            )
            if detail != "brief":
                options = []
                for line in parts.get("코어 옵션", []):
                    opt = _RE_CORE_OPTION.match(line)
                    if opt:
                        need = int(opt.group(1))
                        options.append({"points": need, "effect": opt.group(2), "active": point >= need})
                    else:
                        options.append({"effect": line})
                gems = []
                for gem in slot.get("Gems") or []:
                    gem_elements = tooltip.parse(gem.get("Tooltip"))
                    gem_name = next(
                        (clean(e.value) for e in gem_elements if e.type == "NameTagBox"), None
                    )
                    gem_parts = tooltip.part_boxes(gem_elements)
                    gems.append(
                        compact(
                            {
                                "index": gem.get("Index"),
                                "name": gem_name,
                                "grade": gem.get("Grade"),
                                "active": gem.get("IsActive"),
                                "effects": " · ".join(gem_parts.get("젬 효과", [])) or None,
                            }
                        )
                    )
                core.update(
                    compact(
                        {
                            "willpower": " ".join(parts.get("코어 공급 의지력", [])) or None,
                            "options": options,
                            "condition": parts.get("코어 옵션 발동 조건"),
                            "gems": gems,
                        }
                    )
                )
                if detail == "full":
                    core["tooltip"] = tooltip.flatten(elements)
            cores.append(core)

        effects = [
            compact({"name": e.get("Name"), "level": e.get("Level"), "effect": clean(e.get("Tooltip"))})
            for e in block.get("Effects") or []
        ]
        if detail == "brief":
            return compact(
                {
                    "cores": [
                        f"{c.get('order')}의 {c.get('celestial')} · {c.get('core')} ({c.get('grade')} {c.get('points')}P)"
                        for c in cores
                    ],
                    "effects": " · ".join(f"{e.get('effect')} (Lv{e.get('level')})" for e in effects),
                }
            )
        return compact({"cores": cores, "effects": effects})

    # ------------------------------------------------------------------
    # cosmetic and misc sections
    # ------------------------------------------------------------------

    def _avatars(self, detail: str, match: list[str] | None) -> list[Any]:
        out: list[Any] = []
        for avatar in self.raw.get("ArmoryAvatars") or []:
            name = clean(avatar.get("Name"))
            if not _keep([avatar.get("Type"), name], match):
                continue
            if detail == "brief":
                out.append(f"{avatar.get('Type')} {name} ({avatar.get('Grade')})")
                continue
            row = compact(
                {
                    "slot": avatar.get("Type"),
                    "name": name,
                    "grade": avatar.get("Grade"),
                    "set": avatar.get("IsSet") or None,
                    "inner": avatar.get("IsInner") or None,
                }
            )
            if detail == "full":
                row["tooltip"] = tooltip.flatten(tooltip.parse(avatar.get("Tooltip")))
            out.append(row)
        return out

    def _collectibles(self, detail: str, match: list[str] | None) -> list[Any]:
        out: list[Any] = []
        for item in self.raw.get("Collectibles") or []:
            if not _keep([item.get("Type")], match):
                continue
            if detail == "brief":
                out.append(f"{item.get('Type')} {item.get('Point')}/{item.get('MaxPoint')}")
                continue
            row: dict[str, Any] = {
                "type": item.get("Type"),
                "point": item.get("Point"),
                "max": item.get("MaxPoint"),
            }
            if detail == "full":
                row["points"] = [
                    {"name": p.get("PointName"), "point": p.get("Point"), "max": p.get("MaxPoint")}
                    for p in item.get("CollectiblePoints") or []
                ]
            out.append(row)
        return out

    def _colosseums(self, detail: str) -> list[Any]:
        out: list[Any] = []
        for season in (self.raw.get("ColosseumInfo") or {}).get("Colosseums") or []:
            competitive = season.get("Competitive") or {}
            if detail == "brief":
                out.append(
                    f"{season.get('SeasonName')}: {competitive.get('RankName') or '-'} "
                    f"({competitive.get('VictoryCount', 0)}승/{competitive.get('PlayCount', 0)}전)"
                )
                continue
            modes = {}
            for key, value in season.items():
                if isinstance(value, dict):
                    modes[key] = {k: v for k, v in value.items() if not k.endswith("Icon")}
            out.append({"season": season.get("SeasonName"), **modes})
        return out

    # ------------------------------------------------------------------
    # assembled views
    # ------------------------------------------------------------------

    def section(
        self,
        name: str,
        detail: str = "standard",
        match: list[str] | None = None,
        invested_only: bool = True,
    ) -> Any:
        if name == "profile":
            return self.profile()
        if name == "stats":
            return self._stats(detail)
        if name == "equipment":
            return self._armor(detail, match)
        if name == "accessories":
            return self._accessories(detail, match)
        if name == "bracelet":
            return self._bracelet(detail)
        if name == "ability_stone":
            return self._ability_stone(detail)
        if name == "misc_equipment":
            return self._misc_equipment(detail, match)
        if name == "engravings":
            return self._engravings(detail, match)
        if name == "gems":
            return self._gems(detail, match)
        if name == "cards":
            return self._cards(detail, match)
        if name == "skills":
            return self._skills(detail, match, invested_only)
        if name == "arkpassive":
            return self._arkpassive(detail, match)
        if name == "arkgrid":
            return self._arkgrid(detail, match)
        if name == "avatars":
            return self._avatars(detail, match)
        if name == "collectibles":
            return self._collectibles(detail, match)
        if name == "colosseums":
            return self._colosseums(detail)
        raise ValueError(f"unknown section {name!r}; choose from {', '.join(SECTIONS)}")

    def overview(self) -> dict[str, Any]:
        return compact(
            {
                "character": self.profile(),
                "combat_stats": self._stats("brief"),
                "ark_passive": self._arkpassive("brief", None),
                "equipment": self._armor("brief", None),
                "accessories": self._accessories("brief", None),
                "bracelet": self._bracelet("brief"),
                "ability_stone": self._ability_stone("brief"),
                "engravings": self._engravings("brief", None),
                "gems": self.gem_summary(),
                "cards": self._cards("brief", None),
                "ark_grid": self._arkgrid("brief", None),
                "skills": [
                    s.split(" | ")[0] for s in self._skills("brief", None, invested_only=True)
                ],
            }
        )

    def snapshot_doc(self) -> dict[str, Any]:
        """Parsed sections stored by the collector for population statistics."""
        return {
            name: self.section(name, "standard", None, True)
            for name in (
                "profile",
                "stats",
                "equipment",
                "accessories",
                "bracelet",
                "ability_stone",
                "engravings",
                "gems",
                "cards",
                "skills",
                "arkpassive",
                "arkgrid",
            )
        }


def _size(payload: Any) -> int:
    return len(json.dumps(payload, ensure_ascii=False))


def _shrink(sections: dict[str, Any], limit: int) -> list[str]:
    """Halve the longest list section until the payload fits. Returns what was cut."""
    cut: list[str] = []
    while _size(sections) > limit:
        lists = [(k, v) for k, v in sections.items() if isinstance(v, list) and len(v) > 1]
        if not lists:
            break
        key, value = max(lists, key=lambda kv: _size(kv[1]))
        keep = max(1, len(value) // 2)
        sections[key] = value[:keep]
        cut.append(f"{key}: kept {keep} of {len(value)}")
    return cut


class ArmoryService:
    def __init__(self, client: LostarkClient, cache: TtlCache) -> None:
        self._client = client
        self._cache = cache

    async def load(self, name: str, *, refresh: bool = False) -> tuple[Armory, Any]:
        path = f"/armories/characters/{quote_name(name)}"

        async def fetch() -> Armory:
            raw = await self._client.get(path)
            if not raw:
                raise LostarkError(
                    f"No character named {name.strip()!r}.", kind="not_found", http_status=200, path=path
                )
            return Armory(raw)

        fetched = await self._cache.get(f"armory:{name.strip()}", TTL_ARMORY, fetch, refresh=refresh)
        return fetched.value, fetched

    async def overview(self, name: str, *, refresh: bool = False) -> dict[str, Any]:
        armory, fetched = await self.load(name, refresh=refresh)
        return {
            "as_of_kst": kst_iso(),
            "cache": fetched.meta(),
            **armory.overview(),
            "drill_down": (
                "character_detail(name, sections=[...], detail='brief'|'standard'|'full', "
                "match=[...]) reads any part in depth from the same cached data at no quota cost."
            ),
        }

    async def detail(
        self,
        name: str,
        sections: list[str],
        *,
        detail: str = "standard",
        match: list[str] | None = None,
        invested_only: bool = True,
        refresh: bool = False,
    ) -> dict[str, Any]:
        if detail not in DETAIL_LEVELS:
            raise ValueError(f"detail must be one of {DETAIL_LEVELS}")
        unknown = [s for s in sections if s not in SECTIONS]
        if unknown:
            raise ValueError(f"unknown sections {unknown}; choose from {', '.join(SECTIONS)}")
        if not sections:
            raise ValueError("pick at least one section")

        armory, fetched = await self.load(name, refresh=refresh)

        def build(level: str) -> dict[str, Any]:
            return {s: armory.section(s, level, match, invested_only) for s in sections}

        notices: list[str] = []
        body = build(detail)
        if detail == "full" and _size(body) > MAX_RESPONSE_CHARS:
            body = build("standard")
            notices.append(
                "detail='full' exceeded the response budget; returned 'standard'. "
                "Narrow with match=[...] or fewer sections to get full tooltips."
            )
        cut = _shrink(body, MAX_RESPONSE_CHARS)
        if cut:
            notices.append("Response trimmed to fit: " + "; ".join(cut) + ". Narrow with match=[...].")

        out = compact(
            {
                "name": armory.name,
                "as_of_kst": kst_iso(),
                "cache": fetched.meta(),
                "detail": detail,
                "match": match,
            }
        )
        # Sections stay even when empty, so "nothing matched" is visible.
        out.update(body)
        if notices:
            out["notices"] = notices
        return out

    async def roster(self, name: str, *, refresh: bool = False) -> dict[str, Any]:
        path = f"/characters/{quote_name(name)}/siblings"

        async def fetch() -> list[dict[str, Any]]:
            raw = await self._client.get(path)
            if not raw:
                raise LostarkError(
                    f"No character named {name.strip()!r}.", kind="not_found", http_status=200, path=path
                )
            return raw

        fetched = await self._cache.get(f"roster:{name.strip()}", TTL_ROSTER, fetch, refresh=refresh)
        servers: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
        for row in fetched.value:
            servers[row.get("ServerName") or "?"].append(
                compact(
                    {
                        "name": row.get("CharacterName"),
                        "class": row.get("CharacterClassName"),
                        "item_level": num(row.get("ItemAvgLevel")),
                        "character_level": row.get("CharacterLevel"),
                        "queried": True if row.get("CharacterName") == name.strip() else None,
                    }
                )
            )
        for rows in servers.values():
            rows.sort(key=lambda r: r.get("item_level") or 0, reverse=True)
        return {
            "as_of_kst": kst_iso(),
            "cache": fetched.meta(),
            "count": len(fetched.value),
            "servers": dict(servers),
            "note": (
                "Roster item levels can differ from the character profile by 0.01; "
                "character_overview has the authoritative value."
            ),
        }
