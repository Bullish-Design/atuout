"""CLI for reading agent recordings and importing agent transcript output."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path


def cmd_list(args: argparse.Namespace) -> int:
    from atuout import store

    conn = store.connect(Path(args.db) if args.db else None)
    try:
        recordings = store.list_recordings(conn, limit=args.limit)
    finally:
        conn.close()
    if not recordings:
        print("No agent recordings found.")
        return 0
    for recording in recordings:
        print(recording)
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    from atuout import store

    conn = store.connect(Path(args.db) if args.db else None)
    try:
        recording = store.get_recording(conn, args.atuin_id)
    finally:
        conn.close()
    if recording is None:
        print(f"No agent recording for Atuin history id: {args.atuin_id}", file=sys.stderr)
        return 1
    print(recording)
    print("--- output ---")
    print(recording.output)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    from atuout import settings, store

    conn = store.connect(Path(args.db) if args.db else None)
    try:
        count = store.count_recordings(conn)
    finally:
        conn.close()
    print(f"agent store: {Path(args.db) if args.db else settings.db_path()}")
    print(f"agent recordings: {count}")
    return 0


def cmd_ingest_agent(args: argparse.Namespace) -> int:
    """Backfill outputs for agent-run commands from agent session transcripts."""
    from atuout import agent_ingest, store

    authors = tuple(args.agents) if args.agents else agent_ingest.AGENT_AUTHORS
    conn = store.connect(Path(args.db) if args.db else None)
    since_ms = None
    if args.since_hours is not None:
        since_ms = int((time.time() - args.since_hours * 3600) * 1000)
    try:
        count = agent_ingest.backfill(
            conn,
            authors=authors,
            limit=args.limit,
            since_ms=since_ms,
            dry_run=args.dry_run,
        )
    finally:
        conn.close()
    verb = "would ingest" if args.dry_run else "ingested"
    print(f"{verb} {count} agent command{'s' if count != 1 else ''}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="atuout",
        description="Agent command-output store and transcript importer.",
    )
    parser.add_argument("--db", default=None, help="Override the SQLite database path.")
    sub = parser.add_subparsers(dest="subcommand")

    list_parser = sub.add_parser("list", help="List agent recordings, newest first.")
    list_parser.add_argument("--limit", type=int, default=None, help="Max recordings to show.")
    list_parser.set_defaults(func=cmd_list)

    show_parser = sub.add_parser("show", help="Show an agent recording by Atuin history id.")
    show_parser.add_argument("atuin_id", help="Atuin history id.")
    show_parser.set_defaults(func=cmd_show)

    status_parser = sub.add_parser("status", help="Show agent-store status.")
    status_parser.set_defaults(func=cmd_status)

    ingest_parser = sub.add_parser(
        "ingest-agent",
        help="Backfill agent command output from session transcripts.",
    )
    ingest_parser.add_argument(
        "--agent",
        dest="agents",
        action="append",
        choices=["pi", "claude-code", "codex"],
        help="Agent to ingest (repeatable; default: all supported).",
    )
    ingest_parser.add_argument("--limit", type=int, default=None, help="Max entries to process.")
    ingest_parser.add_argument(
        "--since-hours",
        type=float,
        default=None,
        help="Only scan history from the last N hours (default: all history).",
    )
    ingest_parser.add_argument("--dry-run", action="store_true", help="Report without storing.")
    ingest_parser.set_defaults(func=cmd_ingest_agent)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 0
    exit_code: int = args.func(args)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
