"""Command line entry point.

    lostark-mcp            run the MCP server (what launchd starts)
    lostark-mcp collect    snapshot the characters listed in the rankers file
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(prog="lostark-mcp")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the MCP server (default)")
    collect_cmd = sub.add_parser("collect", help="snapshot the characters in the rankers file")
    collect_cmd.add_argument("--file", type=Path, help="names file (default: LOSTARK_RANKERS_FILE)")
    collect_cmd.add_argument(
        "--max-age-hours", type=float, default=20.0, help="skip names snapshotted more recently"
    )
    collect_cmd.add_argument("--limit", type=int, help="collect at most this many names")
    collect_cmd.add_argument("--dry-run", action="store_true", help="only report what is due")
    args = parser.parse_args()

    if args.command in (None, "serve"):
        from .server import main as serve

        serve()
        return

    from .collect import collect
    from .config import load_settings
    from .logging_setup import setup_logging

    settings = load_settings(for_server=False)
    setup_logging("WARNING")
    sys.exit(
        asyncio.run(
            collect(
                settings,
                file=args.file,
                max_age_hours=args.max_age_hours,
                limit=args.limit,
                dry_run=args.dry_run,
            )
        )
    )
