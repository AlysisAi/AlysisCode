from __future__ import annotations

from pathlib import Path

import pytest

from alysis_code.agent.session import create_session
from alysis_code.config import AppConfig, ConfigError
from alysis_code.llm.types import BillingMode
from alysis_code.profiles import ProfileSpec, add_profile, set_active_profile


@pytest.fixture(autouse=True)
def isolated_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ALYSIS_API_KEY", "")
    monkeypatch.setenv("OPENAI_API_KEY", "")


def config(base_url: str) -> AppConfig:
    cfg = AppConfig(model="local-model", web_search_mode="off")
    cfg.extra_fields = {"profiles": {}, "active_profile": ""}
    add_profile(cfg, ProfileSpec(name="custom-runtime", base_url=base_url))
    set_active_profile(cfg, "custom-runtime")
    return cfg


def start(cfg: AppConfig, root: Path, **kwargs):
    return create_session(
        cfg=cfg,
        root=root,
        mode="review",
        yes=True,
        max_steps=1,
        no_log=True,
        non_interactive=True,
        **kwargs,
    )


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://localhost:11434/v1",
        "http://127.42.0.9:9137/v1",
        "http://[::1]:8123/v1",
        "http://[::ffff:127.0.0.2]:8123/v1",
        "https://model.localhost.:9443/v1",
    ],
)
def test_keyless_loopback_session_starts_without_a_placeholder_key(
    tmp_path: Path,
    endpoint: str,
) -> None:
    session = start(config(endpoint), tmp_path)
    try:
        assert session.api_key == ""
        assert session.client.api_key == ""
        assert session.api_key_source == "missing"
        assert session.client.usage_contract.billing_mode == BillingMode.LOCAL
    finally:
        session.close()


def test_remote_session_still_requires_a_key(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="Missing API key"):
        start(config("https://models.example.test/v1"), tmp_path)


def test_explicit_empty_key_override_still_rejects(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="API key is empty"):
        start(config("http://localhost:11434/v1"), tmp_path, api_key_override=" ")


def test_loopback_session_keeps_configured_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = config("http://127.42.0.9:9137/v1")
    cfg.extra_fields["profiles"]["custom-runtime"]["api_key_env"] = "LOCAL_TEST_CREDENTIAL"
    monkeypatch.setenv("LOCAL_TEST_CREDENTIAL", "test-credential-placeholder")
    session = start(cfg, tmp_path)
    try:
        assert session.api_key == "test-credential-placeholder"
        assert session.client.api_key == session.api_key
        assert session.api_key_source == "env:LOCAL_TEST_CREDENTIAL"
        assert session.client.usage_contract.billing_mode != BillingMode.LOCAL
    finally:
        session.close()
