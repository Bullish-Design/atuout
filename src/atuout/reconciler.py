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
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import IO

import grpc

from atuout import store
from atuout._proto import history_pb2
from atuout.daemon_client import DaemonClient, DaemonError
from atuout.log import get_logger
from atuout.recording import reply_output_text
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
                output=reply_output_text(reply),
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


class _Control:
    """Shared state between the signal handler (main thread) and the tail worker thread."""

    def __init__(self) -> None:
        self.stop = threading.Event()
        self._lock = threading.Lock()
        self._call: grpc.Future | None = None

    def set_call(self, call: grpc.Future | None) -> None:
        with self._lock:
            self._call = call

    def request_stop(self) -> None:
        """Signal-handler-safe: flag stop and cancel any in-flight tail call to unblock it."""
        self.stop.set()
        with self._lock:
            if self._call is not None:
                with contextlib.suppress(Exception):
                    self._call.cancel()


def _run_loop(control: _Control) -> None:
    """Tail history and reconcile ENDED events until stop is requested.

    Runs on a worker thread so the main thread can observe SIGTERM and cancel the (otherwise
    signal-opaque) blocking tail iterator via ``control.request_stop()``.
    """
    log = get_logger()
    conn = store.connect()  # created on this thread; sqlite connections are thread-affine
    socket_path = daemon_socket_path()
    backoff = _RECONNECT_MIN_S

    while not control.stop.is_set():
        try:
            with DaemonClient(socket_path) as client:
                call = client.tail_history_call()
                control.set_call(call)
                if control.stop.is_set():  # stop raced in before we registered the call
                    call.cancel()
                    return
                log.info("reconciler: tailing history")
                backoff = _RECONNECT_MIN_S
                for reply in call:
                    if control.stop.is_set():
                        return
                    if reply.kind == history_pb2.HISTORY_EVENT_KIND_ENDED:
                        reconcile_ended(conn, client, reply.history)
        except grpc.RpcError as e:
            if control.stop.is_set():  # cancelled by request_stop()
                return
            log.warning("reconciler: stream error (%s); reconnecting in %.0fs", e, backoff)
        except DaemonError as e:
            log.warning("reconciler: daemon error (%s); reconnecting in %.0fs", e, backoff)
        except Exception as e:  # keep the reconciler alive across unexpected errors
            log.error("reconciler: unexpected error: %s; reconnecting in %.0fs", e, backoff)
        finally:
            control.set_call(None)
        if control.stop.wait(backoff):
            return
        backoff = min(backoff * 2, _RECONNECT_MAX_S)


def run() -> int:
    """Entry point for the daemonized reconciler process. Blocks until signalled."""
    lock = _acquire_lock()
    if lock is None:
        return 0  # another instance already running

    control = _Control()

    def _handle(_signum: int, _frame: object) -> None:
        control.request_stop()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)

    _write_pidfile()
    worker = threading.Thread(target=_run_loop, args=(control,), name="reconciler-tail")
    worker.start()
    try:
        # Poll so the main thread stays responsive to signals (their handler sets the event).
        while not control.stop.wait(0.25):
            if not worker.is_alive():  # worker only exits after stop; guard against surprises
                break
        worker.join(timeout=5)
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
