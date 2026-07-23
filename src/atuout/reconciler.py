"""Long-lived safety-net reconciler.

Holds a History.TailHistory stream open and, on every ENDED event, backfills any capture the
fast path missed. Single system-wide instance, guarded by an flock + pidfile.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import signal
import sqlite3
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import IO

from atuout import store
from atuout._proto import history_pb2
from atuout.daemon_client import DaemonClient, DaemonError
from atuout.log import get_logger
from atuout.settings import daemon_socket_path, runtime_dir

# More patient than the fast path — the reconciler isn't blocking anything.
RECONCILE_ATTEMPTS = 8
RECONCILE_DELAY_MS = 250

_RECONNECT_MIN_S = 1.0
_RECONNECT_MAX_S = 30.0


def pidfile_path() -> Path:
    return runtime_dir() / "atuout-reconciler.pid"


def lockfile_path() -> Path:
    return runtime_dir() / "atuout-reconciler.lock"


# ---------------------------------------------------------------------------
# Single-instance locking
# ---------------------------------------------------------------------------


def _acquire_lock() -> IO[str] | None:
    """Acquire the exclusive advisory lock. Returns the held file object, or None if another
    instance holds it. The caller must keep the returned handle open for its whole lifetime."""
    runtime_dir().mkdir(parents=True, exist_ok=True)
    fh = lockfile_path().open("w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def is_running() -> bool:
    """True if a reconciler currently holds the lock."""
    probe = _acquire_lock()
    if probe is None:
        return True
    fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
    probe.close()
    return False


def _write_pidfile() -> None:
    pidfile_path().write_text(f"{os.getpid()}\n{int(time.time() * 1000)}\n")


def _remove_pidfile() -> None:
    with contextlib.suppress(OSError):
        pidfile_path().unlink()


def read_pid() -> int | None:
    try:
        first = pidfile_path().read_text().splitlines()[0]
        return int(first)
    except (OSError, ValueError, IndexError):
        return None


# ---------------------------------------------------------------------------
# Core reconcile logic
# ---------------------------------------------------------------------------


def reconcile_ended(
    conn: sqlite3.Connection,
    client: DaemonClient,
    entry: history_pb2.HistoryEntry,
    *,
    attempts: int = RECONCILE_ATTEMPTS,
    delay_ms: int = RECONCILE_DELAY_MS,
    sleep: Callable[[float], object] = time.sleep,
) -> bool:
    """Backfill the capture for one ENDED history entry if missing. Returns True if stored."""
    log = get_logger()
    if store.has_recording(conn, entry.id):
        return False

    for attempt in range(1, attempts + 1):
        try:
            reply = client.command_output(entry.id)
        except DaemonError as e:
            if e.kind == "unimplemented":
                return False
            if not e.retryable or attempt == attempts:
                log.warning("reconcile %s: daemon error (%s)", entry.id, e)
                return False
            sleep(delay_ms / 1000.0)
            continue

        if reply.found:
            inserted = store.upsert_recording(
                conn,
                atuin_id=entry.id,
                command=entry.command or None,
                output=reply.output,
                exit_code=entry.exit,
                total_bytes=reply.total_bytes,
                total_lines=reply.total_lines,
                captured_at_ms=int(time.time() * 1000),
                source="reconciler",
            )
            if inserted:
                log.info("reconcile %s: stored (%d bytes)", entry.id, reply.total_bytes)
            return inserted

        if attempt < attempts:
            sleep(delay_ms / 1000.0)

    log.warning("reconcile %s: not found after %d attempts", entry.id, attempts)
    return False


def _run_loop(stop_flag: dict[str, bool]) -> None:
    log = get_logger()
    conn = store.connect()
    socket_path = daemon_socket_path()
    backoff = _RECONNECT_MIN_S

    while not stop_flag["stop"]:
        try:
            with DaemonClient(socket_path) as client:
                log.info("reconciler: tailing history")
                backoff = _RECONNECT_MIN_S
                for reply in client.tail_history():
                    if stop_flag["stop"]:
                        return
                    if reply.kind == history_pb2.HISTORY_EVENT_KIND_ENDED:
                        reconcile_ended(conn, client, reply.history)
        except DaemonError as e:
            log.warning("reconciler: stream error (%s); reconnecting in %.0fs", e, backoff)
        except Exception as e:  # keep the reconciler alive across unexpected errors
            log.error("reconciler: unexpected error: %s; reconnecting in %.0fs", e, backoff)
        if stop_flag["stop"]:
            return
        time.sleep(backoff)
        backoff = min(backoff * 2, _RECONNECT_MAX_S)


def run() -> int:
    """Entry point for the daemonized reconciler process. Blocks until signalled."""
    lock = _acquire_lock()
    if lock is None:
        return 0  # another instance already running

    stop_flag = {"stop": False}

    def _handle(_signum: int, _frame: object) -> None:
        stop_flag["stop"] = True

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)

    _write_pidfile()
    try:
        _run_loop(stop_flag)
    finally:
        _remove_pidfile()
        with contextlib.suppress(OSError):
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()
    return 0


# ---------------------------------------------------------------------------
# Management (called from init-zsh / CLI)
# ---------------------------------------------------------------------------


def ensure(spawn: bool = True) -> bool:
    """Start the reconciler if not already running. Returns True if a new one was spawned."""
    if is_running():
        return False
    if not spawn:
        return False
    subprocess.Popen(
        ["atuout", "reconcile", "--daemonize"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    return True


def stop() -> bool:
    """Signal a running reconciler to stop. Returns True if a signal was sent."""
    pid = read_pid()
    if pid is None:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        _remove_pidfile()
        return False
    return True
