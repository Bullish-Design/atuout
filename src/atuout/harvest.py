"""Fast-path harvester: fetch a capture from the daemon and persist it."""

from __future__ import annotations

import os
import time
from pathlib import Path

from atuout import store
from atuout.daemon_client import DaemonClient, DaemonError
from atuout.log import get_logger
from atuout.recording import Recording
from atuout.settings import daemon_socket_path

DEFAULT_ATTEMPTS = 3
DEFAULT_DELAY_MS = 50


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def harvest(
    atuin_id: str,
    *,
    command: str | None = None,
    exit_code: int | None = None,
    attempts: int | None = None,
    delay_ms: int | None = None,
    db_path: Path | None = None,
    socket_path: str | None = None,
    source: str = "fast",
) -> Recording | None:
    """Fetch the capture for ``atuin_id`` and persist it. Returns the Recording or None.

    Retries a few times with a short backoff to absorb the pty-proxy -> daemon batching
    window. Never raises: failures are logged and yield None.
    """
    attempts = attempts if attempts is not None else _env_int("ATUOUT_HARVEST_ATTEMPTS", DEFAULT_ATTEMPTS)
    delay_ms = delay_ms if delay_ms is not None else _env_int("ATUOUT_HARVEST_DELAY_MS", DEFAULT_DELAY_MS)
    socket_path = socket_path or daemon_socket_path()
    log = get_logger()

    conn = store.connect(db_path)
    if store.has_recording(conn, atuin_id):
        return store.get_recording(conn, atuin_id)

    try:
        with DaemonClient(socket_path) as client:
            for attempt in range(1, attempts + 1):
                try:
                    reply = client.command_output(atuin_id)
                except DaemonError as e:
                    if e.kind == "unimplemented":
                        log.warning("harvest %s: daemon has no semantic service (%s)", atuin_id, e)
                        return None
                    if not e.retryable or attempt == attempts:
                        log.warning("harvest %s: daemon error (%s)", atuin_id, e)
                        return None
                    time.sleep(delay_ms / 1000.0)
                    continue

                if reply.found:
                    inserted = store.upsert_recording(
                        conn,
                        atuin_id=atuin_id,
                        command=command,
                        output=reply.output,
                        exit_code=exit_code,
                        total_bytes=reply.total_bytes,
                        total_lines=reply.total_lines,
                        captured_at_ms=_now_ms(),
                        source=source,
                    )
                    log.info(
                        "harvest %s: %s (%d bytes)",
                        atuin_id,
                        "stored" if inserted else "existed",
                        reply.total_bytes,
                    )
                    return store.get_recording(conn, atuin_id)

                if attempt < attempts:
                    time.sleep(delay_ms / 1000.0)

        log.warning("harvest %s: not found after %d attempts", atuin_id, attempts)
        return None
    except Exception as e:  # never let a harvest crash the caller
        log.error("harvest %s: unexpected error: %s", atuin_id, e)
        return None


def _now_ms() -> int:
    return int(time.time() * 1000)
