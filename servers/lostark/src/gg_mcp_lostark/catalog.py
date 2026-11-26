"""Search catalogs for the market (거래소) and auction house (경매장).

Both search APIs require a numeric CategoryCode and answer 400 with an empty
body without one. Auction option filters take numeric option codes, and their
values are codes too: a polish effect of 0.40% is sent as 40. The catalogs from
/markets/options and /auctions/options map the names a person would use onto
those codes. They change only with game updates, so they are cached for a day.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .cache import TTL_OPTIONS, TtlCache
from .client import LostarkClient, LostarkError
from .fields import num

# Which gem kinds carry which auction gem effect. Taken from live listings:
# 겁화 gems carry GEM_SKILL_DAMAGE, 작열 gems GEM_SKILL_COOLDOWN_REDUCTION.
# 멸화/홍염 are the tier-3 equivalents.
GEM_KIND_EFFECT = {
    "겁화": "피해 증가",
    "멸화": "피해 증가",
    "작열": "재사용 대기시간 감소",
    "홍염": "재사용 대기시간 감소",
}


def norm(text: str) -> str:
    return "".join(str(text).split()).lower()


@dataclass(frozen=True)
class Category:
    code: int
    name: str
    parent_code: int | None = None
    parent_name: str | None = None

    @property
    def label(self) -> str:
        return f"{self.parent_name} > {self.name}" if self.parent_name else self.name

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "name": self.label}


@dataclass(frozen=True)
class EtcOption:
    group_code: int
    group: str
    code: int
    name: str
    categories: tuple[int, ...]
    tiers: tuple[int, ...]
    values: tuple[tuple[str, int], ...]  # (display, code)

    @property
    def label(self) -> str:
        return f"{self.group} > {self.name}"


@dataclass(frozen=True)
class SkillOption:
    code: int
    name: str
    character_class: str
    gem_effects: tuple[tuple[str, int], ...]  # (effect name, code)


def _categories(raw: dict[str, Any]) -> list[Category]:
    out: list[Category] = []
    for parent in raw.get("Categories") or []:
        parent_cat = Category(int(parent["Code"]), str(parent["CodeName"]))
        out.append(parent_cat)
        for sub in parent.get("Subs") or []:
            out.append(Category(int(sub["Code"]), str(sub["CodeName"]), parent_cat.code, parent_cat.name))
    return out


def _resolve_category(categories: list[Category], value: Any, where: str) -> Category:
    if isinstance(value, int) or (isinstance(value, str) and value.strip().isdigit()):
        code = int(value)
        for cat in categories:
            if cat.code == code:
                return cat
        raise LostarkError(
            f"Unknown {where} category code {code}.",
            kind="bad_request",
            candidates=[c.as_dict() for c in categories],
        )
    wanted = norm(str(value).split(">")[-1])
    exact = [c for c in categories if norm(c.name) == wanted]
    if len(exact) == 1:
        return exact[0]
    partial = exact or [c for c in categories if wanted in norm(c.name)]
    if len(partial) == 1:
        return partial[0]
    raise LostarkError(
        f"Category {value!r} is {'ambiguous' if partial else 'unknown'} in the {where}.",
        kind="bad_request",
        candidates=[c.as_dict() for c in (partial or categories)],
    )


class Catalog:
    def __init__(self, client: LostarkClient, cache: TtlCache) -> None:
        self._client = client
        self._cache = cache

    async def market_raw(self, *, refresh: bool = False) -> dict[str, Any]:
        fetched = await self._cache.get(
            "options:market", TTL_OPTIONS, lambda: self._client.get("/markets/options"), refresh=refresh
        )
        return fetched.value or {}

    async def auction_raw(self, *, refresh: bool = False) -> dict[str, Any]:
        fetched = await self._cache.get(
            "options:auction", TTL_OPTIONS, lambda: self._client.get("/auctions/options"), refresh=refresh
        )
        return fetched.value or {}

    # -- categories ------------------------------------------------------

    async def market_categories(self) -> list[Category]:
        return _categories(await self.market_raw())

    async def auction_categories(self) -> list[Category]:
        return _categories(await self.auction_raw())

    async def market_category(self, value: Any) -> Category:
        return _resolve_category(await self.market_categories(), value, "market")

    async def auction_category(self, value: Any) -> Category:
        return _resolve_category(await self.auction_categories(), value, "auction house")

    async def quality_steps(self) -> list[int]:
        return sorted(int(q) for q in (await self.auction_raw()).get("ItemGradeQualities") or [])

    # -- auction option filters -----------------------------------------

    async def etc_options(self) -> list[EtcOption]:
        out: list[EtcOption] = []
        for group in (await self.auction_raw()).get("EtcOptions") or []:
            for sub in group.get("EtcSubs") or []:
                out.append(
                    EtcOption(
                        group_code=int(group["Value"]),
                        group=str(group["Text"]),
                        code=int(sub["Value"]),
                        name=str(sub["Text"]),
                        categories=tuple(int(c) for c in sub.get("Categorys") or []),
                        tiers=tuple(int(t) for t in sub.get("Tiers") or group.get("Tiers") or []),
                        values=tuple(
                            (str(v["DisplayValue"]), int(v["Value"])) for v in sub.get("EtcValues") or []
                        ),
                    )
                )
        return out

    @staticmethod
    def applies(option: EtcOption, category: Category | None, categories: list[Category]) -> bool:
        """Whether an option may be used in a category (or the parent of one it lists)."""
        if category is None or not option.categories:
            return True
        if category.code in option.categories:
            return True
        parents = {c.parent_code for c in categories if c.code in option.categories}
        return category.code in parents

    async def resolve_etc_option(self, spec: str, category: Category | None) -> EtcOption:
        """Find an option by name, optionally "group > name" or "group:name"."""
        categories = await self.auction_categories()
        group_part, _, name_part = spec.replace(":", ">").rpartition(">")
        wanted = norm(name_part)
        wanted_group = norm(group_part) if group_part else None
        options = await self.etc_options()

        matches = [
            o
            for o in options
            if norm(o.name) == wanted and (wanted_group is None or norm(o.group) == wanted_group)
        ]
        applicable = [o for o in matches if self.applies(o, category, categories)]
        if len(applicable) == 1:
            return applicable[0]
        if len(applicable) > 1:
            raise LostarkError(
                f"Option {spec!r} exists in several groups; prefix it with the group, e.g. "
                f"'{applicable[0].group} > {applicable[0].name}'.",
                kind="bad_request",
                candidates=[o.label for o in applicable],
            )
        if matches:
            names = {c.code: c.label for c in categories}
            allowed = sorted({names.get(code, str(code)) for o in matches for code in o.categories})
            raise LostarkError(
                f"Option {spec!r} does not apply to {category.label if category else 'this category'}; "
                f"it applies to: {', '.join(allowed)}.",
                kind="bad_request",
            )
        # Tooltips say "적에게 주는 피해 +1.20%" where the catalog says "적에게 주는
        # 피해 증가"; accept a prefix when it picks out exactly one usable option.
        near_options = [
            o
            for o in options
            if wanted
            and (wanted in norm(o.name) or norm(o.name) in wanted)
            and (wanted_group is None or norm(o.group) == wanted_group)
        ]
        usable = [o for o in near_options if norm(o.name).startswith(wanted) and self.applies(o, category, categories)]
        if len(usable) == 1:
            return usable[0]
        near = [o.label for o in near_options]
        raise LostarkError(
            f"No auction option named {spec!r}. Use auction_options(kind='etc_options') to browse.",
            kind="bad_request",
            candidates=near[:20] or None,
        )

    @staticmethod
    def encode_value(option: EtcOption, value: Any, bound: str) -> int:
        """Turn a displayed value (0.95 or "0.95%") into the code the API expects.

        Options with a value table are matched against it: a minimum picks the
        smallest code shown at or above the value, a maximum the largest at or
        below. Options without a table take the number as is.
        """
        number = num(str(value).replace("%", ""))
        if number is None:
            raise LostarkError(f"Value {value!r} for {option.label} is not a number.", kind="bad_request")
        if not option.values:
            return int(round(number))
        table = sorted(
            ((num(display.replace("%", "")), code) for display, code in option.values if num(display.replace("%", "")) is not None),
        )
        eps = 1e-9
        if bound == "min":
            for shown, code in table:
                if shown >= number - eps:
                    return code
        else:
            for shown, code in reversed(table):
                if shown <= number + eps:
                    return code
        raise LostarkError(
            f"{option.label} has no value {'at or above' if bound == 'min' else 'at or below'} {value}.",
            kind="bad_request",
            candidates=[display for display, _ in option.values],
        )

    # -- skills (gem search) --------------------------------------------

    async def skills(self) -> list[SkillOption]:
        out = []
        for skill in (await self.auction_raw()).get("SkillOptions") or []:
            out.append(
                SkillOption(
                    code=int(skill["Value"]),
                    name=str(skill["Text"]),
                    character_class=str(skill.get("Class") or ""),
                    gem_effects=tuple(
                        (str(t["Text"]), int(t["Value"])) for t in skill.get("Tripods") or [] if t.get("IsGem")
                    ),
                )
            )
        return out

    async def resolve_skill(self, name: str, character_class: str | None) -> SkillOption:
        wanted = norm(name)
        skills = [
            s
            for s in await self.skills()
            if norm(s.name) == wanted
            and (not character_class or norm(s.character_class) == norm(character_class))
        ]
        if len(skills) == 1:
            return skills[0]
        if skills:
            raise LostarkError(
                f"Skill {name!r} exists for several classes; pass character_class.",
                kind="bad_request",
                candidates=[f"{s.character_class} > {s.name}" for s in skills],
            )
        near = [
            f"{s.character_class} > {s.name}"
            for s in await self.skills()
            if wanted in norm(s.name) and (not character_class or norm(s.character_class) == norm(character_class))
        ]
        raise LostarkError(f"No skill named {name!r}.", kind="bad_request", candidates=near[:20] or None)

    @staticmethod
    def gem_effect_code(skill: SkillOption, effect: str) -> int:
        for text, code in skill.gem_effects:
            if norm(text) == norm(effect):
                return code
        raise LostarkError(
            f"{skill.name} has no gem effect {effect!r}.",
            kind="bad_request",
            candidates=[text for text, _ in skill.gem_effects],
        )
