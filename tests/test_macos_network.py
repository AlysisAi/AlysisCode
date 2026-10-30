from __future__ import annotations

import os
import socket
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest

from alysis_code.config import ConfigError
from alysis_code.host_file_policy import HostFilePolicy
from alysis_code.macos_network import network_domains, socket_identity
from alysis_code.sandbox_runner import select_shell_sandbox_backend
from alysis_code.sandbox_settings import ShellSandboxSettings


@pytest.mark.parametrize(
    "value",
    [
        [],
        ["https://example.com"],
        ["127.0.0.1"],
        ["localhost"],
        ["x.localhost"],
        ["host:443"],
        ["x\nHost: evil"],
        ["user:pass@example.com"],
        [True],
        "example.com",
    ],
)
def test_invalid_network_destinations_are_rejected(value):
    with pytest.raises(ConfigError):
        network_domains(value)


def test_network_destinations_are_normalized_without_expanding_scope():
    assert network_domains(["Registry.NPMJS.org", "registry.npmjs.org", "παράδειγμα.gr"]) == (
        "registry.npmjs.org",
        "xn--hxajbheg2az3al.gr",
    )


@pytest.mark.skipif(os.name == "nt", reason="Unix sockets")
def test_socket_grant_tracks_canonical_target_and_rejects_replacement_or_private_state(monkeypatch):
    with (
        tempfile.TemporaryDirectory(prefix="alysis-daemon-", dir="/tmp") as folder,
        socket.socket(socket.AF_UNIX) as first,
        socket.socket(socket.AF_UNIX) as replacement,
    ):
        root = Path(folder).resolve()
        path, link = root / "daemon.sock", root / "alias.sock"
        first.bind(str(path))
        link.symlink_to(path)
        grant = socket_identity(str(link))
        assert grant.path == str(path)
        grant.check()
        path.rename(root / "old.sock")
        replacement.bind(str(path))
        with pytest.raises(ConfigError, match="changed"):
            grant.check()
        monkeypatch.setattr(HostFilePolicy, "capture", lambda: HostFilePolicy((root,), ()))
        with pytest.raises(ConfigError, match="Private application"):
            socket_identity(str(link))


@pytest.mark.parametrize(
    "settings",
    [
        ShellSandboxSettings(mode="off", network_domains=("example.com",)),
        ShellSandboxSettings(backend="docker", network_domains=("example.com",)),
        ShellSandboxSettings(backend="windows-mxc", unix_sockets=("/socket",)),
    ],
)
def test_network_capabilities_never_fall_back_to_a_backend_that_ignores_them(settings):
    with pytest.raises(ConfigError, match="Seatbelt"):
        select_shell_sandbox_backend(replace(settings, unavailable_fallback="ask"), system="darwin")


@pytest.mark.parametrize(
    "value",
    [
        ["*"],
        ["foo*.example.com"],
        ["*.com"],
        ["***.example.com"],
        ["127.0.0.1:80"],
        ["*.127.0.0.1"],
    ],
)
def test_invalid_wildcard_domain_rules(value):
    with pytest.raises(ConfigError):
        network_domains(value)


@pytest.mark.parametrize(
    "value",
    [
        ["0.0.0.0:8080"],
        ["169.254.169.254:80"],
        ["224.0.0.1:80"],
        ["127.0.0.1:0"],
        ["127.0.0.1:65536"],
        ["127.0.0.1:080"],
        ["localhost:8080"],
        ["https://127.0.0.1:8080"],
        ["8.8.8.8:443"],
        [True],
    ],
)
def test_private_endpoints_require_canonical_bounded_numeric_addresses(value):
    from alysis_code.macos_network import private_network_endpoints

    with pytest.raises(ConfigError):
        private_network_endpoints(value)


def test_private_endpoint_and_wildcard_normalization():
    from alysis_code.macos_network import private_network_endpoints

    assert network_domains(["*.EXAMPLE.com", "**.Example.com"]) == (
        "*.example.com",
        "**.example.com",
    )
    assert private_network_endpoints(["127.0.0.1:8080", "192.168.1.10:443"]) == (
        "127.0.0.1:8080",
        "192.168.1.10:443",
    )
