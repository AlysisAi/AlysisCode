"""Regression tests for the master web-tools switch (benchmark/offline integrity).

Guards against the bug that invalidated the 2026-07-25 SWE-bench run: web_fetch
was registered unconditionally, so "web off" harness settings had no effect and
the model made live web calls mid-benchmark.

Three layers are covered:
1. Policy resolution (env var precedence over config field).
2. Tool registration: disabled => web_fetch/web_search absent from the model's
   tool list entirely.
3. Runtime guards: direct calls to web_fetch()/web_search() hard-error while
   disabled, without performing any network I/O.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from alysis_code.agent_loop import ToolDef, build_tools
from alysis_code.config import AppConfig, resolve_web_tools_enabled
from alysis_code.session_store import SessionStore
from alysis_code.tools.web import WebFetchError, web_fetch
from alysis_code.tools.web_search import WebSearchError, web_search

_ENV_VAR = "ALYSIS_WEB_TOOLS"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_ENV_VAR, raising=False)


def _store(tmp_path: Path) -> SessionStore:
    return SessionStore(
        enabled=True,
        sessions_dir=tmp_path / "sessions",
        session_id="web-tool-gating",
        cwd=str(tmp_path),
        repo_root=str(tmp_path),
    )


def _build_tools(tmp_path: Path, *, cfg: AppConfig) -> dict[str, ToolDef]:
    return build_tools(
        root=tmp_path,
        console=None,
        surface=None,
        store=_store(tmp_path),
        mode="auto",
        yes=True,
        cfg=cfg,
        api_key="test-key",
        max_steps=3,
    )


# --- Layer 1: policy resolution -------------------------------------------------


def test_web_tools_enabled_by_default() -> None:
    assert resolve_web_tools_enabled(None) is True
    assert resolve_web_tools_enabled(AppConfig(model="test-model")) is True


def test_config_field_disables_web_tools() -> None:
    cfg = AppConfig(model="test-model", web_tools_enabled=False)
    assert resolve_web_tools_enabled(cfg) is False


@pytest.mark.parametrize("value", ["off", "0", "false", "no", "disabled", "OFF", " Off "])
def test_env_var_disables_web_tools(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(_ENV_VAR, value)
    assert resolve_web_tools_enabled(None) is False
    assert resolve_web_tools_enabled(AppConfig(model="test-model")) is False


def test_env_var_takes_precedence_over_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_ENV_VAR, "on")
    cfg = AppConfig(model="test-model", web_tools_enabled=False)
    assert resolve_web_tools_enabled(cfg) is True
    monkeypatch.setenv(_ENV_VAR, "off")
    cfg_on = AppConfig(model="test-model", web_tools_enabled=True)
    assert resolve_web_tools_enabled(cfg_on) is False


# --- Layer 2: tool registration -------------------------------------------------


def test_web_tools_absent_from_tool_list_when_disabled_via_config(tmp_path: Path) -> None:
    cfg = AppConfig(model="test-model", web_tools_enabled=False)
    tools = _build_tools(tmp_path, cfg=cfg)
    assert "web_fetch" not in tools
    assert "web_search" not in tools


def test_web_tools_absent_from_tool_list_when_disabled_via_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_ENV_VAR, "off")
    cfg = AppConfig(model="test-model")
    tools = _build_tools(tmp_path, cfg=cfg)
    assert "web_fetch" not in tools
    assert "web_search" not in tools


def test_web_fetch_registered_by_default(tmp_path: Path) -> None:
    cfg = AppConfig(model="test-model", web_search_mode="off")
    tools = _build_tools(tmp_path, cfg=cfg)
    assert "web_fetch" in tools


# --- Layer 3: runtime guards ----------------------------------------------------


def test_web_fetch_hard_errors_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_ENV_VAR, "off")
    with pytest.raises(WebFetchError) as excinfo:
        web_fetch(url="https://example.com/")
    assert excinfo.value.recoverable is False
    assert "disabled by the web-tools policy" in str(excinfo.value)


def test_web_search_hard_errors_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_ENV_VAR, "off")
    with pytest.raises(WebSearchError) as excinfo:
        web_search(query="anything", cfg=AppConfig(model="test-model"))
    assert excinfo.value.recoverable is False
    assert "disabled by the web-tools policy" in str(excinfo.value)


def test_web_search_hard_errors_when_disabled_via_config() -> None:
    cfg = AppConfig(model="test-model", web_tools_enabled=False)
    with pytest.raises(WebSearchError):
        web_search(query="anything", cfg=cfg)


def test_web_tools_cli_preference_is_validated_persisted_and_gates_tools(tmp_path, monkeypatch):
    from alysis_code.config import ConfigError, load_config, save_config, set_config_value

    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    cfg = set_config_value(AppConfig(model="test-model"), "web_tools_enabled", "false")
    save_config(cfg)
    loaded = load_config()
    assert loaded.web_tools_enabled is False
    tools = _build_tools(tmp_path, cfg=loaded)
    assert "web_fetch" not in tools
    assert "web_search" not in tools
    save_config(set_config_value(loaded, "web_tools_enabled", "true"))
    assert load_config().web_tools_enabled is True
    with pytest.raises(ConfigError, match="web_tools_enabled must be true/false"):
        set_config_value(loaded, "web_tools_enabled", "maybe")
