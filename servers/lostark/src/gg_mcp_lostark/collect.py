"""`gg-mcp-lostark collect`: snapshot every character in the rankers file.

Runs outside the MCP server because a few hundred characters take minutes at
the API's 100 requests per minute. It throttles itself to its own share and
pauses whenever the server reports `LOSTARK_COLLECT_RESERVE` or fewer requests
left, so a running MCP server keeps headroom for interactive use.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from .armory import Armory
from .client import LostarkClient, LostarkError, quote_name
from .config import Settings
from .snapshots import SnapshotStore
from .throttle import MinuteQuota

logger = logging.getLogger(__name__)

# The collector may wait out a full quota window rather than give up.
_COLLECT_MAX_WAIT_SEC = 90.0


def read_names(path: Path) -> list[str]:
    """One name per line. Blank lines and anything after '#' are ignored."""
    names: list[str] = []
    seen: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        name = line.split("#", 1)[0].strip()
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


async def collect(
    settings: Settings,
    *,
    file: Path | None = None,
    max_age_hours: float = 20.0,
    limit: int | None = None,
    dry_run: bool = False,
) -> int:
    path = file or settings.rankers_file
    if not path.exists():
        print(f"rankers file not found: {path}")
        return 1
    names = read_names(path)
    store = SnapshotStore(settings.snapshot_db)
    cutoff = time.time() - max_age_hours * 3600
    due = [n for n in names if (store.last_fetched(n) or 0) < cutoff]
    if limit:
        due = due[:limit]
    print(f"{len(names)} names in {path}; {len(due)} due (older than {max_age_hours:g}h).")
    if dry_run or not due:
        store.close()
        return 0

    quota = MinuteQuota(
        settings.collect_rate_limit_per_min,
        max_wait=_COLLECT_MAX_WAIT_SEC,
        reserve=settings.collect_reserve,
    )
    client = LostarkClient(settings, quota=quota)
    saved = missing = failed = 0
    try:
        for index, name in enumerate(due, 1):
            try:
                raw = await client.get(f"/armories/characters/{quote_name(name)}")
            except LostarkError as exc:
                if exc.kind in {"rate_limited", "maintenance", "unauthorized", "network"}:
                    print(f"[{index}/{len(due)}] stopping: {exc.kind} - {exc}")
                    break
                failed += 1
                print(f"[{index}/{len(due)}] {name}: {exc.kind} - {exc}")
                continue
            if not raw:
                missing += 1
                print(f"[{index}/{len(due)}] {name}: not found (renamed or deleted?)")
                continue
            doc = Armory(raw).snapshot_doc()
            store.save(doc, source="rankers")
            saved += 1
            profile = doc.get("profile") or {}
            print(
                f"[{index}/{len(due)}] {name}: {profile.get('class')} "
                f"{profile.get('item_level')} {profile.get('build') or ''}"
            )
    finally:
        await client.aclose()
        store.close()
    print(f"saved {saved}, not found {missing}, failed {failed}.")
    return 0
