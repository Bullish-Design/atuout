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


def cmd_harvest(args: argparse.Namespace) -> int:
    from atuout.harvest import harvest

    harvest(
        args.atuin_id,
        command=args.command,
        exit_code=args.exit_code,
        db_path=Path(args.db) if args.db else None,
    )
    # Always succeed: this runs detached from the shell hook and must never disturb it.
    return 0


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


def cmd_reconcile(args: argparse.Namespace) -> int:
    from atuout import reconciler

    if args.daemonize:
        return reconciler.run()
    action = args.action or "ensure"
    if action == "ensure":
        spawned = reconciler.ensure()
        print("started reconciler" if spawned else "reconciler already running")
        return 0
    if action == "status":
        running = reconciler.is_running()
        print(f"reconciler: {'running' if running else 'stopped'} (pid={reconciler.read_pid()})")
        return 0
    if action == "stop":
        print("sent stop" if reconciler.stop() else "reconciler not running")
        return 0
    if action == "restart":
        reconciler.stop()
        reconciler.ensure()
        print("restarted reconciler")
        return 0
    print(f"unknown reconcile action: {action}", file=sys.stderr)
    return 2


def _capture_warning(p: object) -> str | None:
    """One-line warning if the daemon is reachable but lacks command-output capture."""
    from atuout.probe import Probe

    assert isinstance(p, Probe)
    if p.reachable and p.capture_supported is False:
        return (
            "atuout: this atuin daemon has no command-output capture (needs atuin with "
            "PR #3510, i.e. >= 18.18.0-beta.2). Captures will not be harvested."
        )
    return None


def cmd_status(args: argparse.Namespace) -> int:
    from atuout import reconciler, settings, store
    from atuout.probe import probe

    conn = store.connect(Path(args.db) if args.db else None)
    p = probe(settings.daemon_socket_path())

    if not p.reachable:
        daemon_line = f"unreachable ({p.detail})"
    else:
        cap = {True: "yes", False: "no", None: "unknown"}[p.capture_supported]
        daemon_line = f"reachable (version {p.version}, protocol {p.protocol}, capture: {cap})"

    print(f"daemon socket:   {settings.daemon_socket_path()}")
    print(f"daemon enabled:  {settings.daemon_enabled()}")
    print(f"daemon:          {daemon_line}")
    print(f"reconciler:      {'running' if reconciler.is_running() else 'stopped'}")
    print(f"recordings:      {store.count_recordings(conn)}")

    warning = _capture_warning(p)
    if warning:
        print(warning, file=sys.stderr)
    return 0


def cmd_check(_args: argparse.Namespace) -> int:
    """Startup capability check for the shell hook: warn (to stderr) if the reachable daemon
    can't capture output. Silent when the daemon is unreachable (it may start lazily) or fine.
    Always exits 0 so it never disrupts shell startup."""
    from atuout.probe import probe

    warning = _capture_warning(probe())
    if warning:
        print(warning, file=sys.stderr)
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

    p_harvest = sub.add_parser("harvest", help="Fetch and store a capture by Atuin history id.")
    p_harvest.add_argument("atuin_id", help="Atuin history id.")
    p_harvest.add_argument("--command", default=None, help="Command text (from the shell hook).")
    p_harvest.add_argument("--exit-code", type=int, default=None, help="Command exit code.")
    p_harvest.set_defaults(func=cmd_harvest)

    p_ls = sub.add_parser("list", help="List stored recordings.")
    p_ls.add_argument("--limit", type=int, default=None, help="Max recordings to show.")
    p_ls.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="Show a stored recording by Atuin history id.")
    p_show.add_argument("atuin_id", help="Atuin history id.")
    p_show.set_defaults(func=cmd_show)

    p_rec = sub.add_parser("reconcile", help="Manage the background reconciler.")
    p_rec.add_argument(
        "action",
        nargs="?",
        choices=["ensure", "status", "stop", "restart"],
        default=None,
        help="Management action (default: ensure).",
    )
    p_rec.add_argument(
        "--daemonize", action="store_true", help="Run the reconciler loop (internal)."
    )
    p_rec.set_defaults(func=cmd_reconcile)

    p_status = sub.add_parser("status", help="Show daemon/reconciler/store status.")
    p_status.set_defaults(func=cmd_status)

    p_check = sub.add_parser(
        "check", help="Warn if the atuin daemon lacks command-output capture (used by init-zsh)."
    )
    p_check.set_defaults(func=cmd_check)

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
