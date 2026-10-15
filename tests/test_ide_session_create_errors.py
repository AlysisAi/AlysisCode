"""session.create tells the host why a session could not start.

The bridge returns only explicitly safe messages. Configuration errors already passed
through; provider-auth errors (a ChatGPT sign-in that must be renewed, an effort the
selected model does not offer) became "The IDE bridge encountered an unexpected internal
error", so the desktop app could only say "Repair the managed runtime".
"""

from __future__ import annotations

import io
import os
from pathlib import Path

import pytest

from alysis_code.config import AppConfig, ConfigError
from alysis_code.ide import stdio_bridge
from alysis_code.ide.stdio_bridge import ProtocolError, StdioBridge
from alysis_code.provider_auth import ProviderAuthError, ProviderLoginRequiredError


def _create(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception) -> ProtocolError:
    monkeypatch.setattr(stdio_bridge, "load_config", lambda: AppConfig(model="m", stream=True))
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")

    def _fail(**_kwargs: object) -> object:
        raise error

    bridge = StdioBridge(stdout=io.StringIO(), create_session_fn=_fail)
    request = stdio_bridge.ProtocolRequest(
        id="create",
        method="session.create",
        params={"workspace": os.fspath(tmp_path), "mode": "auto"},
        protocol_version="1",
    )
    with pytest.raises(ProtocolError) as raised:
        bridge._dispatch(request)
    return raised.value


@pytest.mark.parametrize(
    "error",
    [
        ProviderLoginRequiredError("Reconnect ChatGPT to authorize Alysis Code for plan usage."),
        ProviderAuthError(
            "The selected reasoning effort is not supported by this provider and model."
        ),
    ],
)
def test_provider_auth_errors_reach_the_host_with_the_engine_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: ProviderAuthError
) -> None:
    raised = _create(tmp_path, monkeypatch, error)

    assert raised.code == "provider_auth_error"
    assert raised.message == str(error)


def test_configuration_errors_keep_their_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raised = _create(tmp_path, monkeypatch, ConfigError("No active profile configured."))

    assert raised.code == "config_error"
    assert raised.message == "No active profile configured."


def test_other_errors_still_stay_inside_the_bridge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(stdio_bridge, "load_config", lambda: AppConfig(model="m", stream=True))
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")

    def _fail(**_kwargs: object) -> object:
        raise RuntimeError("token=secret-value")

    out = io.StringIO()
    bridge = StdioBridge(stdout=out, create_session_fn=_fail)
    request = stdio_bridge.ProtocolRequest(
        id="create",
        method="session.create",
        params={"workspace": os.fspath(tmp_path), "mode": "auto"},
        protocol_version="1",
    )

    with pytest.raises(RuntimeError):
        bridge._dispatch(request)
