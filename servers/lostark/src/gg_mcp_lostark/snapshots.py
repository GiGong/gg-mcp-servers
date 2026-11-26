"""Local store of parsed character snapshots, for population statistics.

The Open API has no ranking or search endpoint, so the population is whatever
names the operator lists in the rankers file and collects with
`gg-mcp-lostark collect`. Rows hold parsed sections rather than raw tooltips.

The database is made of other players' character names; keep it out of version
control (the default location, servers/lostark/local/, is gitignored).
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    server      TEXT,
    class       TEXT,
    item_level  REAL,
    combat_power REAL,
    build       TEXT,
    source      TEXT NOT NULL,
    fetched_at  REAL NOT NULL,
    doc         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS snapshots_name ON snapshots (name, fetched_at);
CREATE INDEX IF NOT EXISTS snapshots_class ON snapshots (class, fetched_at);
"""


class SnapshotStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.executescript(_SCHEMA)

    def close(self) -> None:
        self._db.close()

    def save(self, doc: dict[str, Any], *, source: str) -> None:
        profile = doc.get("profile") or {}
        with self._db:
            self._db.execute(
                "INSERT INTO snapshots (name, server, class, item_level, combat_power, build,"
                " source, fetched_at, doc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    profile.get("name"),
                    profile.get("server"),
                    profile.get("class"),
                    profile.get("item_level"),
                    profile.get("combat_power"),
                    profile.get("build"),
                    source,
                    time.time(),
                    json.dumps(doc, ensure_ascii=False),
                ),
            )

    def last_fetched(self, name: str) -> float | None:
        row = self._db.execute(
            "SELECT MAX(fetched_at) FROM snapshots WHERE name = ?", (name,)
        ).fetchone()
        return row[0] if row and row[0] is not None else None

    def latest(
        self,
        *,
        source: str = "rankers",
        character_class: str | None = None,
        min_item_level: float | None = None,
        max_age_days: float | None = None,
        build: str | None = None,
    ) -> list[tuple[dict[str, Any], float]]:
        """The newest snapshot per character that passes the filters."""
        since = time.time() - max_age_days * 86_400 if max_age_days else None
        rows = self._db.execute(
            """
            SELECT s.doc, s.fetched_at
              FROM snapshots s
              JOIN (SELECT name, MAX(fetched_at) AS newest
                      FROM snapshots WHERE source = ? GROUP BY name) m
                ON s.name = m.name AND s.fetched_at = m.newest
             WHERE s.source = ?
               AND (? IS NULL OR s.class = ?)
               AND (? IS NULL OR s.item_level >= ?)
               AND (? IS NULL OR s.fetched_at >= ?)
               AND (? IS NULL OR s.build = ?)
            """,
            (
                source,
                source,
                character_class,
                character_class,
                min_item_level,
                min_item_level,
                since,
                since,
                build,
                build,
            ),
        ).fetchall()
        return [(json.loads(doc), fetched) for doc, fetched in rows]

    def classes(self, *, source: str = "rankers") -> dict[str, int]:
        rows = self._db.execute(
            "SELECT class, COUNT(DISTINCT name) FROM snapshots WHERE source = ? GROUP BY class",
            (source,),
        ).fetchall()
        return {cls or "?": count for cls, count in rows}
