"""Minimal CLI entry-point for atuout."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _zsh_hook_text() -> str | None:
    """Locate the zsh hook, preferring installed package data over the source tree."""
    import importlib.resources as resources

    try:
        res = resources.files("atuout").joinpath("shell/atuout.zsh")
        if res.is_file():
            return res.read_text()
    except (FileNotFoundError, TypeError, ModuleNotFoundError):
        pass

    source_path = Path(__file__).resolve().parent.parent.parent / "shell" / "atuout.zsh"
    if source_path.exists():
        return source_path.read_text()
    return None


def cmd_list(args: argparse.Namespace) -> int:
    from atuout import store

    conn = store.connect(Path(args.db) if args.db else None)
    recs = store.list_recordings(conn, limit=args.limit)
    if not recs:
        print("No recordings found.")
        return 0
    for rec in recs:
        print(rec)
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    from atuout import store

    conn = store.connect(Path(args.db) if args.db else None)
    rec = store.get_recording(conn, args.atuin_id)
    if rec is None:
        print(f"No recording for atuin id: {args.atuin_id}", file=sys.stderr)
        return 1
    print(rec)
    print("--- output ---")
    print(rec.output)
    return 0


def cmd_init_zsh(_args: argparse.Namespace) -> int:
    """Print the zsh hook script to stdout for eval."""
    hook_text = _zsh_hook_text()
    if hook_text is None:
        print("# atuout: zsh hook not found", file=sys.stderr)
        return 1
    print(hook_text)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="atuout",
        description="Harvester for Atuin's native command-output captures.",
    )
    parser.add_argument("--db", default=None, help="Override the SQLite database path.")
    sub = parser.add_subparsers(dest="subcommand")

    p_ls = sub.add_parser("list", help="List stored recordings.")
    p_ls.add_argument("--limit", type=int, default=None, help="Max recordings to show.")
    p_ls.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="Show a stored recording by Atuin history id.")
    p_show.add_argument("atuin_id", help="Atuin history id.")
    p_show.set_defaults(func=cmd_show)

    p_init = sub.add_parser("init-zsh", help="Print the zsh hook for eval.")
    p_init.set_defaults(func=cmd_init_zsh)

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
