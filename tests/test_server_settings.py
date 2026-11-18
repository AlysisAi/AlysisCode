from __future__ import annotations

from pathlib import Path

import pytest

import alysis_code.server.settings as server_settings_mod
from alysis_code.config import ConfigError
from alysis_code.server.settings import resolve_server_settings


def test_resolve_server_settings_defaults(tmp_path: Path) -> None:
    settings = resolve_server_settings(host="127.0.0.1", port=7070, data_dir=tmp_path)
    assert settings.default_model is None
    assert settings.default_base_url is None
    assert settings.allow_client_base_url is False
    assert settings.allow_client_model is True


def test_resolve_server_settings_parses_model_and_base_url_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ALYSIS_SERVER_MODEL", "gpt-test")
    monkeypatch.setenv("ALYSIS_SERVER_BASE_URL", "https://api.example.com/v1")
    monkeypatch.setenv("ALYSIS_SERVER_ALLOW_CLIENT_BASE_URL", "yes")
    monkeypatch.setenv("ALYSIS_SERVER_ALLOW_CLIENT_MODEL", "0")

    settings = resolve_server_settings(host="127.0.0.1", port=7070, data_dir=tmp_path)
    assert settings.default_model == "gpt-test"
    assert settings.default_base_url == "https://api.example.com/v1"
    assert settings.allow_client_base_url is True
    assert settings.allow_client_model is False


def test_resolve_server_settings_invalid_base_url_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ALYSIS_SERVER_BASE_URL", "ftp://api.example.com/v1")
    with pytest.raises(ConfigError, match="ALYSIS_SERVER_BASE_URL"):
        resolve_server_settings(host="127.0.0.1", port=7070, data_dir=tmp_path)


def test_resolve_server_settings_invalid_bool_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ALYSIS_SERVER_ALLOW_CLIENT_BASE_URL", "maybe")
    with pytest.raises(ConfigError, match="ALYSIS_SERVER_ALLOW_CLIENT_BASE_URL"):
        resolve_server_settings(host="127.0.0.1", port=7070, data_dir=tmp_path)


def test_resolve_server_settings_non_linux_bwrap_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ALYSIS_SERVER_WORKER_BACKEND", "bwrap")
    monkeypatch.setattr(server_settings_mod.platform, "system", lambda: "Darwin")
    with pytest.raises(ConfigError, match="requires Linux"):
        resolve_server_settings(host="127.0.0.1", port=7070, data_dir=tmp_path)


def test_resolve_server_settings_default_worker_sandbox_mode_is_strict_for_all_backends(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("ALYSIS_SERVER_WORKER_SANDBOX_MODE", raising=False)
    monkeypatch.setenv("ALYSIS_SERVER_WORKER_BACKEND", "docker")
    settings = resolve_server_settings(host="127.0.0.1", port=7070, data_dir=tmp_path)
    assert settings.worker_sandbox_mode == "strict"

    monkeypatch.delenv("ALYSIS_SERVER_WORKER_BACKEND")
    monkeypatch.setenv("ALYSIS_SERVER_WORKER_BACKEND", "bwrap")
    monkeypatch.setattr(server_settings_mod.platform, "system", lambda: "Linux")
    settings = resolve_server_settings(host="127.0.0.1", port=7070, data_dir=tmp_path)
    assert settings.worker_sandbox_mode == "strict"
