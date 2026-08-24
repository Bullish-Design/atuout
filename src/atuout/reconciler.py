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
from dataclasses import dataclass
from pathlib import Path
from typing import IO

import grpc

from atuout import agent_ingest, store
from atuout._proto import history_pb2
from atuout.agent_ingest import AGENT_AUTHORS, ingest_entry
from atuout.daemon_client import DaemonClient, DaemonError
from atuout.log import get_logger
from atuout.recording import reply_output_text
from atuout.settings import daemon_socket_path, runtime_dir

# More patient than the fast path — the reconciler isn't blocking anything.
RECONCILE_ATTEMPTS = 8
RECONCILE_DELAY_MS = 250

_RECONNECT_MIN_S = 1.0
_RECONNECT_MAX_S = 30.0

# An agent writes the command's result to its transcript *after* the atuin hook
# records the command, so the first lookup almost always misses. Retry on this
# schedule (seconds after the previous try) before giving up on the entry.
AGENT_RETRY_DELAYS_S = (2.0, 5.0, 15.0, 60.0, 300.0)

# Final safety net: re-scan recent agent history for entries still missing
# output. Catches whatever the retry queue lost to a restart or a long stall.
AGENT_SWEEP_INTERVAL_S = 900.0
AGENT_SWEEP_LOOKBACK_S = 6 * 3600.0


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


@dataclass
class _PendingAgent:
    """An agent entry whose transcript had no match yet, waiting for a retry."""

    atuin_id: str
    command: str
    author: str
    exit_code: int
    timestamp_ns: int
    attempt: int = 0
    due_at: float = 0.0


class _AgentRetryQueue:
    """Agent entries to re-check once their transcript has caught up.

    Thread-safe: the tail thread adds, the retry thread drains.
    """

    def __init__(
        self,
        delays: tuple[float, ...] = AGENT_RETRY_DELAYS_S,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._delays = delays
        self._now = now
        self._lock = threading.Lock()
        self._items: list[_PendingAgent] = []
        self.wakeup = threading.Event()

    def add(self, entry: history_pb2.HistoryEntry) -> None:
        pending = _PendingAgent(
            atuin_id=entry.id,
            command=entry.command,
            author=entry.author,
            exit_code=entry.exit,
            timestamp_ns=entry.timestamp,
            due_at=self._now() + self._delays[0],
        )
        with self._lock:
            self._items.append(pending)
        self.wakeup.set()

    def take_due(self) -> list[_PendingAgent]:
        """Remove and return every entry whose retry time has arrived."""
        now = self._now()
        with self._lock:
            due = [item for item in self._items if item.due_at <= now]
            if due:
                self._items = [item for item in self._items if item.due_at > now]
        return due

    def requeue(self, pending: _PendingAgent) -> bool:
        """Schedule the next retry. False if the entry is out of attempts."""
        pending.attempt += 1
        if pending.attempt >= len(self._delays):
            return False
        pending.due_at = self._now() + self._delays[pending.attempt]
        with self._lock:
            self._items.append(pending)
        return True

    def next_delay(self, default: float) -> float:
        """Seconds until the earliest retry is due, capped at ``default``."""
        with self._lock:
            if not self._items:
                return default
            soonest = min(item.due_at for item in self._items)
        return max(0.0, min(default, soonest - self._now()))

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


def reconcile_ended(
    conn: sqlite3.Connection,
    client: DaemonClient,
    entry: history_pb2.HistoryEntry,
    *,
    attempts: int = RECONCILE_ATTEMPTS,
    delay_ms: int = RECONCILE_DELAY_MS,
    sleep: Callable[[float], object] = time.sleep,
    pending: _AgentRetryQueue | None = None,
) -> bool:
    """Backfill the capture for one ENDED history entry if missing. Returns True if stored."""
    log = get_logger()
    if store.has_recording(conn, entry.id):
        return False

    # Agent-run commands (recorded by atuin's hooks) never have a daemon
    # capture; recover their output from the agent's session transcript
    # instead. The agent writes that transcript after the hook fires, so a miss
    # here is expected — hand the entry to the retry queue rather than drop it.
    if entry.author in AGENT_AUTHORS:
        stored = ingest_entry(
            conn,
            atuin_id=entry.id,
            command=entry.command,
            author=entry.author,
            exit_code=entry.exit,
            timestamp_ns=entry.timestamp,
        )
        if not stored and pending is not None:
            pending.add(entry)
        return stored

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


def drain_agent_retries(conn: sqlite3.Connection, pending: _AgentRetryQueue) -> int:
    """Re-check every due agent entry. Returns how many stored this pass."""
    log = get_logger()
    stored = 0
    for item in pending.take_due():
        if store.has_recording(conn, item.atuin_id):
            continue
        ok = ingest_entry(
            conn,
            atuin_id=item.atuin_id,
            command=item.command,
            author=item.author,
            exit_code=item.exit_code,
            timestamp_ns=item.timestamp_ns,
        )
        if ok:
            stored += 1
            log.info("reconcile %s: stored from %s transcript", item.atuin_id, item.author)
        elif not pending.requeue(item):
            log.warning(
                "reconcile %s: no %s transcript match after %d tries",
                item.atuin_id,
                item.author,
                item.attempt,
            )
    return stored


def _agent_retry_loop(control: _Control, pending: _AgentRetryQueue) -> None:
    """Drain due agent retries, and periodically sweep recent history.

    Runs on its own thread with its own sqlite connection: the tail thread must
    never block on transcript parsing.
    """
    log = get_logger()
    conn = store.connect()  # sqlite connections are thread-affine
    next_sweep = time.monotonic() + AGENT_SWEEP_INTERVAL_S

    while not control.stop.is_set():
        wait_for = pending.next_delay(min(AGENT_SWEEP_INTERVAL_S, 5.0))
        if pending.wakeup.wait(wait_for):
            pending.wakeup.clear()
        if control.stop.is_set():
            return
        try:
            drain_agent_retries(conn, pending)
        except Exception as e:  # a bad transcript must not kill the worker
            log.error("reconciler: agent retry failed: %s", e)

        if time.monotonic() >= next_sweep:
            next_sweep = time.monotonic() + AGENT_SWEEP_INTERVAL_S
            try:
                since_ms = int((time.time() - AGENT_SWEEP_LOOKBACK_S) * 1000)
                found = agent_ingest.backfill(conn, since_ms=since_ms)
                if found:
                    log.info("reconciler: sweep recovered %d agent command(s)", found)
            except Exception as e:
                log.error("reconciler: agent sweep failed: %s", e)


def _run_loop(control: _Control, pending: _AgentRetryQueue) -> None:
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
                        reconcile_ended(conn, client, reply.history, pending=pending)
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
    pending = _AgentRetryQueue()

    def _handle(_signum: int, _frame: object) -> None:
        control.request_stop()
        pending.wakeup.set()  # unblock the retry thread's wait

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)

    _write_pidfile()
    # Create the DB on this thread before the workers connect. `PRAGMA journal_mode=WAL` takes a
    # brief exclusive lock and does NOT invoke the busy handler, so two threads opening a *new* DB
    # at once leave one with `database is locked`. Once the file exists in WAL mode the PRAGMA is
    # a no-op and cannot fail (measured: 37/200 fresh, 0/200 existing).
    store.connect().close()
    worker = threading.Thread(
        target=_run_loop, args=(control, pending), name="reconciler-tail"
    )
    retrier = threading.Thread(
        target=_agent_retry_loop, args=(control, pending), name="reconciler-agent-retry"
    )
    worker.start()
    retrier.start()
    try:
        # Poll so the main thread stays responsive to signals (their handler sets the event).
        while not control.stop.wait(0.25):
            if not worker.is_alive():  # worker only exits after stop; guard against surprises
                break
        control.stop.set()  # the tail may have died on its own; stop the retrier too
        pending.wakeup.set()
        worker.join(timeout=5)
        retrier.join(timeout=5)
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
