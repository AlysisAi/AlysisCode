from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

import pytest

from alysis_code.config import AppConfig
from alysis_code.ide import stdio_bridge
from alysis_code.ide.protocol import ProtocolError, ProtocolRequest
from alysis_code.sandbox_settings import resolve_shell_sandbox_settings


@pytest.mark.parametrize("profile", ["default", "strict", "warn", "off"])
def test_session_create_applies_sandbox_profile_without_mutating_shared_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: str
) -> None:
    monkeypatch.delenv("ALYSIS_SHELL_SANDBOX_MODE", raising=False)
    monkeypatch.delenv("ALYSIS_SHELL_SANDBOX_MODE", raising=False)
    source = AppConfig(model="test-model")
    monkeypatch.setattr(stdio_bridge, "load_config", lambda: source)
    configs = []

    def create(**kwargs):
        configs.append(kwargs["cfg"])
        return SimpleNamespace(close=lambda: None)

    bridge = stdio_bridge.StdioBridge(stdout=io.StringIO(), create_session_fn=create)
    result = bridge._session_create(
        ProtocolRequest(
            "create",
            "session.create",
            {
                "workspace": str(tmp_path),
                "mode": "readonly",
                "sandbox_profile": profile,
            },
        )
    )
    expected = "strict" if profile == "default" else profile
    assert result["sandbox_mode"] == expected
    assert resolve_shell_sandbox_settings(configs[0]).mode == expected
    if profile != "default":
        assert configs[0].extra_fields["verify_sandbox"]["mode"] == expected
    assert source.extra_fields == {}


def test_session_profile_preserves_environment_policy_and_rejects_invalid_values(monkeypatch):
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_MODE", "strict")
    cfg = AppConfig(model="test-model")
    stdio_bridge._apply_session_sandbox_profile(cfg, {"sandbox_profile": "off"}, request_id="test")
    assert resolve_shell_sandbox_settings(cfg).mode == "strict"
    with pytest.raises(ProtocolError, match="Unsupported sandbox_profile"):
        stdio_bridge._apply_session_sandbox_profile(
            cfg, {"sandbox_profile": "typo"}, request_id="test"
        )
