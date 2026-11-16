from __future__ import annotations

import pytest

from alysis_code.cli_impl.config_menu import ConfigMenuState
from alysis_code.config import AppConfig
from alysis_code.sandbox_settings import (
    apply_sandbox_mode_to_config,
    normalize_sandbox_mode,
    resolve_shell_sandbox_settings,
    sandbox_mode_from_config,
)
from alysis_code.verify_gate import resolve_verify_sandbox_mode


@pytest.fixture(autouse=True)
def _clear_sandbox_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALYSIS_SHELL_SANDBOX_MODE", raising=False)
    monkeypatch.delenv("ALYSIS_VERIFY_SANDBOX_MODE", raising=False)


def test_normalize_sandbox_mode() -> None:
    assert normalize_sandbox_mode("OFF") == "off"
    assert normalize_sandbox_mode("Strict") == "strict"
    assert normalize_sandbox_mode("warn") == "warn"
    assert normalize_sandbox_mode("bogus") == "strict"
    assert normalize_sandbox_mode("") == "strict"
    assert normalize_sandbox_mode(None) == "strict"


def test_sandbox_mode_from_config_defaults_to_strict() -> None:
    assert sandbox_mode_from_config(AppConfig(model="m")) == "strict"


def test_apply_sandbox_mode_updates_both_sections_and_runtime_resolution() -> None:
    cfg = AppConfig(model="m")
    apply_sandbox_mode_to_config(cfg, "off")
    # Both gates must be written together; the verify gate resolves its mode
    # independently of the shell sandbox.
    assert cfg.extra_fields["shell_sandbox"]["mode"] == "off"
    assert cfg.extra_fields["verify_sandbox"]["mode"] == "off"
    assert sandbox_mode_from_config(cfg) == "off"
    assert resolve_shell_sandbox_settings(cfg).mode == "off"
    assert resolve_verify_sandbox_mode(cfg) == "off"


def test_apply_sandbox_mode_preserves_other_shell_sandbox_keys() -> None:
    cfg = AppConfig(model="m")
    cfg.extra_fields = {"shell_sandbox": {"backend": "docker"}}
    apply_sandbox_mode_to_config(cfg, "warn")
    assert cfg.extra_fields["shell_sandbox"]["backend"] == "docker"
    assert cfg.extra_fields["shell_sandbox"]["mode"] == "warn"
    assert cfg.extra_fields["verify_sandbox"]["mode"] == "warn"


def test_apply_sandbox_mode_normalizes_invalid_input() -> None:
    cfg = AppConfig(model="m")
    apply_sandbox_mode_to_config(cfg, "nonsense")
    assert cfg.extra_fields["shell_sandbox"]["mode"] == "strict"


def test_config_menu_sandbox_mode_round_trip() -> None:
    cfg = AppConfig(model="m")
    state = ConfigMenuState.from_cfg(cfg)
    assert state.fields["sandbox_mode"] == "strict"

    state.fields["sandbox_mode"] = "off"
    assert state.dirty

    result = state.commit_to(cfg)
    assert result.saved
    assert result.changes.get("sandbox_mode") == "off"
    assert cfg.extra_fields["shell_sandbox"]["mode"] == "off"
    assert cfg.extra_fields["verify_sandbox"]["mode"] == "off"


def test_config_menu_sandbox_mode_unchanged_is_not_a_change() -> None:
    cfg = AppConfig(model="m")
    state = ConfigMenuState.from_cfg(cfg)
    result = state.commit_to(cfg)
    assert "sandbox_mode" not in result.changes
