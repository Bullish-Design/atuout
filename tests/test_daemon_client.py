from __future__ import annotations

import threading

import pytest

from atuout._proto import history_pb2
from atuout.daemon_client import DaemonClient, DaemonError
from atuout.recording import reply_output_text
from atuout.settings import daemon_socket_path
from tests.support.fake_daemon import FakeDaemon


def test_command_output_found(fake_daemon: FakeDaemon) -> None:
    fake_daemon.add_capture("abc", "hello\nworld\n")
    with DaemonClient(daemon_socket_path()) as client:
        reply = client.command_output("abc")
    assert reply.found is True
    # The real daemon leaves `output` empty and returns content via `lines`.
    assert reply.output == ""
    assert reply.total_lines == 2
    assert reply.total_bytes == len(b"hello\nworld\n")
    assert [line.content for line in reply.lines] == ["hello", "world"]
    assert reply_output_text(reply) == "hello\nworld"


def test_command_output_not_found(fake_daemon: FakeDaemon) -> None:
    with DaemonClient(daemon_socket_path()) as client:
        reply = client.command_output("missing")
    assert reply.found is False


def test_command_output_connect_error(tmp_path) -> None:
    # No daemon listening at this socket → connect/unavailable error.
    with DaemonClient(str(tmp_path / "nope.sock")) as client:
        with pytest.raises(DaemonError) as exc:
            client.command_output("abc")
    assert exc.value.kind == "unavailable"
    assert exc.value.retryable is True


def test_command_output_unimplemented(fake_daemon_unimplemented: FakeDaemon) -> None:
    with DaemonClient(daemon_socket_path()) as client:
        with pytest.raises(DaemonError) as exc:
            client.command_output("abc")
    assert exc.value.kind == "unimplemented"
    assert exc.value.retryable is False


def test_tail_history_yields_ended(fake_daemon: FakeDaemon) -> None:
    received: list[str] = []
    done = threading.Event()

    def consume() -> None:
        with DaemonClient(daemon_socket_path()) as client:
            for reply in client.tail_history():
                if reply.kind == history_pb2.HISTORY_EVENT_KIND_ENDED:
                    received.append(reply.history.id)
                    done.set()
                    return

    t = threading.Thread(target=consume, daemon=True)
    t.start()
    fake_daemon.end_history("id-1", command="ls", exit=0)
    assert done.wait(timeout=5.0)
    t.join(timeout=5.0)
    assert received == ["id-1"]
