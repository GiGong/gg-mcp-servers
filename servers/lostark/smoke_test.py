#!/usr/bin/env python
"""Live verification against the Lost Ark API. Requires LOSTARK_API_KEY in .env.

    .venv/bin/python servers/lostark/smoke_test.py <character name>

Calls every tool once through an in-memory MCP client and checks that key
fields are populated. Uses roughly 25 requests of the 100-per-minute quota.
The character name is an argument so that no real name lives in the repository.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "src"))
os.environ.setdefault("AUTH_MODE", "none")

from fastmcp import Client  # noqa: E402

from lostark_mcp.config import load_settings  # noqa: E402
from lostark_mcp.logging_setup import setup_logging  # noqa: E402
from lostark_mcp.server import build_server  # noqa: E402

OK = "\033[32m OK \033[0m"
BAD = "\033[31mFAIL\033[0m"


def dig(payload: Any, path: str) -> Any:
    node = payload
    for part in path.split("."):
        if isinstance(node, list):
            node = node[int(part)] if part.isdigit() and int(part) < len(node) else None
        elif isinstance(node, dict):
            node = node.get(part)
        else:
            return None
    return node


async def main(name: str) -> int:
    setup_logging("WARNING")
    mcp, client = build_server(load_settings(for_server=False))
    failures = 0
    cases: list[tuple[str, dict[str, Any], list[str]]] = [
        ("character_overview", {"name": name}, ["character.item_level", "character.class", "equipment.0"]),
        ("character_detail", {"name": name, "sections": ["skills", "gems"], "detail": "standard"}, ["skills.0.name", "gems.summary.count"]),
        ("character_roster", {"name": name}, ["count", "servers"]),
        ("market_prices", {"preset": "생활-벌목"}, ["items.0.unit_price.current_min"]),
        ("market_history", {"item": "아비도스 목재"}, ["entries.0.summary.avg_7d"]),
        ("auction_options", {"kind": "etc_options", "category": "목걸이", "query": "피해"}, ["options.0.option"]),
        ("auction_search", {"category": "목걸이", "grade": "고대", "options": [{"option": "추가 피해", "min": 1.6}]}, ["items.0.buy_price", "filters_applied.0.min"]),
        ("auction_gem_board", {"levels": [7, 8]}, ["board.0.min_buy_price"]),
        ("calc_craft_profit", {}, ["usage"]),
        ("stats_builds", {"aspect": "summary"}, []),
    ]
    async with Client(mcp) as c:
        for tool, args, paths in cases:
            result = (await c.call_tool(tool, args)).structured_content or {}
            size = len(json.dumps(result, ensure_ascii=False))
            missing = [p for p in paths if dig(result, p) in (None, "", [])]
            failed = (result.get("error") and result.get("kind") != "no_data") or missing
            failures += bool(failed)
            detail = result.get("message") if result.get("error") else (f"missing {missing}" if missing else "")
            print(f"{BAD if failed else OK}  {tool:20} {size:6} chars  {detail}")
        quota = client.quota() or {}
        print(f"\nquota remaining: {quota.get('remaining')} (resets {quota.get('resets_at_kst')})")
    await client.aclose()
    return 1 if failures else 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    sys.exit(asyncio.run(main(sys.argv[1])))
