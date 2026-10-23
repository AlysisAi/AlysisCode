from __future__ import annotations

import json
import tomllib
from pathlib import Path

from typer.testing import CliRunner

from alysis_code import __version__
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


def test_capabilities_advertise_host_v2_as_the_only_planner_authority() -> None:
    payload = build_capabilities_payload()

    assert (
        payload["protocols"]
        == dict(DESKTOP_PROTOCOLS)
        == {
            "forge-host": 2,
            "forge-contract-v2": 2,
            "forge-status": 1,
        }
    )
    assert "forge-planner" not in payload["protocols"]
    assert payload["features"] == {feature: True for feature in DESKTOP_FEATURES}
    assert payload["features"]["ide.bridge"] is True
    assert payload["features"]["sensitive-host-context-v1"] is True
    assert payload["features"]["computer-use-host-v1"] is True
    assert payload["features"]["task-budget-host-v1"] is True
    assert payload["features"]["browser-start-host-v1"] is True
    assert payload["features"]["ide.private-utf8-stdio-v1"] is True
    assert payload["features"]["forge.host-v2"] is True
    assert payload["features"]["forge.host-authoritative-verification-v1"] is True
    assert payload["features"]["forge.planning-profiles-v1"] is True
    assert "forge.plan.desktop-protocol" not in payload["features"]


def test_capabilities_command_emits_machine_readable_json() -> None:
    result = CliRunner().invoke(app, ["capabilities"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == build_capabilities_payload()
