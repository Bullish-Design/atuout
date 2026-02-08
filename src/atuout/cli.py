"""Minimal CLI entry-point for atuout."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def cmd_record(args: argparse.Namespace) -> int:
    from atuout.recorder import record_command

    rec = record_command(
        args.command,
        atuin_id=args.atuin_id,
        data_dir=Path(args.data_dir) if args.data_dir else None,
    )
    print(rec)
    if not rec.success:
        return 1
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    from atuout.recorder import list_recordings

    recs = list_recordings(data_dir=Path(args.data_dir) if args.data_dir else None)
    if not recs:
        print("No recordings found.")
        return 0
    for rec in recs:
        print(rec)
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    from atuout.recording import Recording

    path = Path(args.cast_file)
    if not path.exists():
        print(f"File not found: {path}", file=sys.stderr)
        return 1
    rec = Recording(cast_path=path, command="<loaded>")
    print(rec)
    print("--- output ---")
    print(rec.output)
    return 0


def cmd_init_zsh(_args: argparse.Namespace) -> int:
    """Print the zsh hook script to stdout for eval."""
    hook_path = Path(__file__).resolve().parent.parent.parent.parent / "shell" / "atuout.zsh"
    if not hook_path.exists():
        # Fallback: try installed location
        import importlib.resources as resources

        try:
            hook_text = resources.files("atuout").joinpath("shell/atuout.zsh").read_text()
        except (FileNotFoundError, TypeError):
            print(f"# atuout: zsh hook not found (looked at {hook_path})", file=sys.stderr)
            return 1
    else:
        hook_text = hook_path.read_text()
    print(hook_text)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="atuout", description="Shell session recorder powered by asciinema.")
    parser.add_argument("--data-dir", default=None, help="Override recording storage directory.")
    sub = parser.add_subparsers(dest="subcommand")

    # record
    p_rec = sub.add_parser("record", help="Record a single command.")
    p_rec.add_argument("command", help="Command to record.")
    p_rec.add_argument("--atuin-id", default=None, help="Atuin history ID to link.")
    p_rec.set_defaults(func=cmd_record)

    # list
    p_ls = sub.add_parser("list", help="List stored recordings.")
    p_ls.set_defaults(func=cmd_list)

    # show
    p_show = sub.add_parser("show", help="Show output from a .cast file.")
    p_show.add_argument("cast_file", help="Path to a .cast file.")
    p_show.set_defaults(func=cmd_show)

    # init-zsh
    p_init = sub.add_parser("init-zsh", help="Print the zsh hook for eval.")
    p_init.set_defaults(func=cmd_init_zsh)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
