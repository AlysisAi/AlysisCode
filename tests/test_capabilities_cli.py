from __future__ import annotations

import json
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from alysis_code import __version__, computer_use_platform_policy
from alysis_code.capabilities import (
    DESKTOP_FEATURES,
    DESKTOP_PROTOCOLS,
    build_capabilities_payload,
)
from alysis_code.cli_impl.commands.root import app

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def declared_version() -> str:
    """The version the build system publishes, read from the file it reads."""
    with PYPROJECT.open("rb") as handle:
        return tomllib.load(handle)["project"]["version"]


def test_engine_reports_one_version_everywhere_it_is_written() -> None:
    """The desktop app pins the engine by version, so the three places that
    declare it must agree: the capabilities handshake the app gates on, the
    module constant, and the package metadata the wheel is built from. This
    reads pyproject.toml rather than restating the version as a literal, because
    a hand-copied literal is one more place to forget during a version bump -
    which is exactly how this assertion came to name a version eleven builds
    behind the engine it was testing."""
    payload = build_capabilities_payload()

    assert payload["version"] == __version__ == declared_version()


@pytest.mark.parametrize("host", ["darwin", "win32"])
def test_capabilities_advertise_desktop_features_without_retired_planner_protocols(
    monkeypatch, host
) -> None:
    monkeypatch.setattr(computer_use_platform_policy, "sys", SimpleNamespace(platform=host))
    payload = build_capabilities_payload()

    assert payload["protocols"] == dict(DESKTOP_PROTOCOLS) == {}
    cu = {
        "sensitive-host-context-v1",
        "computer-use-host-v1",
        "computer-discovery-v1",
        "task-budget-host-v1",
        "browser-start-host-v1",
    }
    expected = set(DESKTOP_FEATURES) - (cu if host != "darwin" else set())
    assert payload["features"] == {feature: True for feature in expected}
    assert all((feature in payload["features"]) == (host == "darwin") for feature in cu)
    assert payload["features"]["ide.bridge"] is True
    assert payload["features"]["ide.private-utf8-stdio-v1"] is True
    assert not [feature for feature in payload["features"] if feature.startswith("forge.")]


def test_capabilities_command_emits_machine_readable_json() -> None:
    result = CliRunner().invoke(app, ["capabilities"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == build_capabilities_payload()
