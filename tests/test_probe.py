from __future__ import annotations

from atuout.probe import probe
from atuout.settings import daemon_socket_path
from tests.support.fake_daemon import FakeDaemon


def test_probe_capture_supported(fake_daemon: FakeDaemon) -> None:
    p = probe(daemon_socket_path())
    assert p.reachable is True
    assert p.capture_supported is True
    assert p.version == "fake-daemon"
    assert p.protocol == 1


def test_probe_capture_unsupported(fake_daemon_unimplemented: FakeDaemon) -> None:
    p = probe(daemon_socket_path())
    assert p.reachable is True
    assert p.capture_supported is False


def test_probe_unreachable(tmp_path) -> None:
    p = probe(str(tmp_path / "nope.sock"))
    assert p.reachable is False
    assert p.capture_supported is None
