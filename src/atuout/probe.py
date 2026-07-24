"""Capability probe for the atuin daemon.

We deliberately probe *capability* (does the Semantic capture service answer?) rather than
compare version strings: the feature (atuin PR #3510) landed in 18.18.0-beta.2, the eventual
stable version is a moving target, and forks/nightlies make string comparison unreliable.
"""

from __future__ import annotations

from dataclasses import dataclass

from atuout.daemon_client import DaemonClient, DaemonError
from atuout.settings import daemon_socket_path

# A history id that will never exist; a found=False (rather than UNIMPLEMENTED) proves the
# Semantic service is present.
_PROBE_ID = "__atuout_capability_probe__"


@dataclass
class Probe:
    reachable: bool
    version: str | None = None
    protocol: int | None = None
    # None when unknown (e.g. daemon unreachable).
    capture_supported: bool | None = None
    detail: str = ""


def probe(socket_path: str | None = None) -> Probe:
    """Probe the daemon: is it reachable, and does it support command-output capture?"""
    socket_path = socket_path or daemon_socket_path()
    try:
        with DaemonClient(socket_path) as client:
            try:
                status = client.status()
            except DaemonError as e:
                return Probe(reachable=False, detail=f"daemon unreachable ({e.kind})")

            capture_supported = _capture_supported(client)
            return Probe(
                reachable=True,
                version=status.version or None,
                protocol=status.protocol,
                capture_supported=capture_supported,
                detail="ok",
            )
    except Exception as e:  # never raise from a probe
        return Probe(reachable=False, detail=str(e))


def _capture_supported(client: DaemonClient) -> bool:
    try:
        client.command_output(_PROBE_ID)
    except DaemonError as e:
        # Only 'unimplemented' means the service is absent; any other error implies it exists
        # but this particular call failed (still "supported").
        return e.kind != "unimplemented"
    return True
