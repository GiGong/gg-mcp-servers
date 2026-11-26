"""Crafting profit, e.g. turning life-skill materials into fusion materials.

Prices come live from the market. Recipes, crafting fees and the market sale
fee are game data the API does not provide; they come from data/recipes.toml
and data/fees.toml or from arguments, and every response states which numbers
were assumed and whether they were verified.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from .client import LostarkError
from .fields import compact, kst_iso
from .market import MarketService


def _load(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError:
        return {}


class CraftService:
    def __init__(self, market: MarketService, data_dir: Path) -> None:
        self._market = market
        self._recipes_path = data_dir / "recipes.toml"
        self._fees_path = data_dir / "fees.toml"

    def recipes(self) -> dict[str, dict[str, Any]]:
        return _load(self._recipes_path).get("recipes", {})

    def fee(self) -> dict[str, Any]:
        return _load(self._fees_path)

    async def profit(
        self,
        *,
        recipe: str | None = None,
        output_item: str | None = None,
        output_qty: float | None = None,
        inputs: list[dict[str, Any]] | None = None,
        craft_gold: float | None = None,
        craft_seconds: float | None = None,
        crafts: int = 1,
        sell_fee_rate: float | None = None,
        refresh: bool = False,
    ) -> dict[str, Any]:
        recipes = self.recipes()
        base: dict[str, Any] = {}
        if recipe:
            if recipe not in recipes:
                raise LostarkError(
                    f"No recipe named {recipe!r} in data/recipes.toml.",
                    kind="bad_request",
                    candidates=sorted(recipes) or None,
                )
            base = recipes[recipe]
        elif not output_item:
            return {
                "recipes": {
                    name: {"output": r.get("output"), "verified": r.get("verified") or None}
                    for name, r in recipes.items()
                },
                "usage": "Pass recipe=<name>, or output_item, output_qty, inputs and craft_gold inline.",
            }

        output_item = output_item or base.get("output")
        output_qty = output_qty if output_qty is not None else base.get("output_qty")
        inputs = inputs if inputs is not None else base.get("inputs")
        craft_gold = craft_gold if craft_gold is not None else base.get("craft_gold")
        craft_seconds = craft_seconds if craft_seconds is not None else base.get("craft_seconds")

        problems = []
        if not output_item:
            problems.append("output_item")
        if not output_qty:
            problems.append("output_qty")
        if not inputs or any(not i.get("name") or not i.get("qty") for i in inputs):
            problems.append("inputs (each needs name and qty)")
        if craft_gold is None:
            problems.append("craft_gold")
        if problems:
            raise LostarkError(
                "Recipe is incomplete: " + ", ".join(problems) + ". Fill data/recipes.toml "
                "from the in-game crafting window, or pass the values inline.",
                kind="bad_request",
            )

        fee_file = self.fee()
        fee = sell_fee_rate if sell_fee_rate is not None else fee_file.get("sell_fee_rate")
        if fee is None:
            raise LostarkError(
                "No sale fee configured. Set sell_fee_rate in data/fees.toml or pass it.",
                kind="bad_request",
            )
        fee = float(fee)

        out_row, _ = await self._market.find(str(output_item), refresh=refresh)
        out_unit = (out_row.get("unit_price") or {}).get("current_min")
        if out_unit is None:
            raise LostarkError(f"{output_item} has no current listing price.", kind="not_found")

        priced_inputs = []
        buy_cost = 0.0
        sell_value = 0.0
        for item in inputs:
            row, _ = await self._market.find(str(item["name"]), refresh=refresh)
            unit = (row.get("unit_price") or {}).get("current_min")
            if unit is None:
                raise LostarkError(f"{item['name']} has no current listing price.", kind="not_found")
            qty = float(item["qty"])
            buy_cost += qty * unit
            sell_value += qty * unit * (1 - fee)
            priced_inputs.append(
                {"name": row.get("name"), "qty": qty, "unit_price": unit, "cost": round(qty * unit, 2)}
            )

        gross = float(output_qty) * out_unit
        net = gross * (1 - fee)
        craft_gold = float(craft_gold)
        per_craft = {
            "output_value_net": round(net, 2),
            "buy_materials_profit": round(net - buy_cost - craft_gold, 2),
            "own_materials_profit_vs_selling_them": round(net - sell_value - craft_gold, 2),
            "materials_buy_cost": round(buy_cost, 2),
            "materials_sell_value_net": round(sell_value, 2),
            "craft_gold": craft_gold,
        }
        total = {k: round(v * crafts, 2) for k, v in per_craft.items()}

        per_hour = None
        if craft_seconds:
            runs = 3600.0 / float(craft_seconds)
            per_hour = {
                "crafts_per_hour_single_queue": round(runs, 2),
                "own_materials_profit": round(per_craft["own_materials_profit_vs_selling_them"] * runs, 2),
            }

        return compact(
            {
                "as_of_kst": kst_iso(),
                "recipe": recipe,
                "output": {
                    "name": out_row.get("name"),
                    "qty_per_craft": output_qty,
                    "unit_price": out_unit,
                    "bundle_count": out_row.get("bundle_count"),
                },
                "inputs": priced_inputs,
                "per_craft": per_craft,
                "crafts": crafts,
                "total": total if crafts != 1 else None,
                "per_hour": per_hour,
                "verdict": (
                    "Crafting beats selling the raw materials."
                    if per_craft["own_materials_profit_vs_selling_them"] > 0
                    else "Selling the raw materials beats crafting."
                ),
                "assumptions": {
                    "sell_fee_rate": fee,
                    "fee_source": (fee_file.get("verified") or "unverified")
                    if sell_fee_rate is None
                    else "passed by caller",
                    "recipe_source": (base.get("verified") or "unverified")
                    if recipe
                    else "passed by caller",
                    "prices": "current lowest listing per unit; large orders move the price",
                },
            }
        )
