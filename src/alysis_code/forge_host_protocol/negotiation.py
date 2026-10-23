"""Handshake and capability negotiation for Forge Host Protocol v2."""

from __future__ import annotations

from collections.abc import Iterable

from .errors import ReplayUnavailableError, UnsupportedVersionError
from .models import (
    PROTOCOL_VERSION,
    Capability,
    HandshakeRequest,
    HandshakeResponse,
    Identity,
    Peer,
)


def _version_key(version: str) -> tuple[int, int]:
    major, minor = version.split(".", maxsplit=1)
    return (int(major), int(minor))


def negotiate_handshake(
    request: HandshakeRequest,
    *,
    server: Peer,
    server_capabilities: Iterable[Capability],
    identity: Identity,
    server_versions: Iterable[str] = (PROTOCOL_VERSION,),
    replay_through_sequence: int | None = None,
) -> HandshakeResponse:
    """Select the highest common protocol and intersect capability versions."""

    common_versions = set(request.supported_versions).intersection(server_versions)
    if PROTOCOL_VERSION not in common_versions:
        offered = ", ".join(sorted(request.supported_versions, key=_version_key))
        raise UnsupportedVersionError(
            f"Forge Host Protocol {PROTOCOL_VERSION} is required; client offered: {offered}"
        )
    if server.host_kind != "engine":
        raise ValueError("the handshake server peer must use hostKind=engine")
    requested = {capability.name: capability.version for capability in request.capabilities}
    available: dict[str, int] = {}
    for capability in server_capabilities:
        if capability.name in available:
            raise ValueError("server capability names must be unique")
        available[capability.name] = capability.version
    negotiated = [
        Capability(name=name, version=min(version, available[name]))
        for name, version in requested.items()
        if name in available
    ]
    negotiated.sort(key=lambda capability: capability.name)
    if request.replay_cursor is not None:
        raise ReplayUnavailableError("replay requires an authenticated durable protocol checkpoint")
    return HandshakeResponse(
        type="handshake.response",
        server=server,
        selected_version=PROTOCOL_VERSION,
        capabilities=negotiated,
        identity=identity,
        replay_window=None,
    )
