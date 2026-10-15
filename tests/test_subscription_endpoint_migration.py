"""A subscription adapter owns its endpoint; profiles saved before it moved follow it.

The ChatGPT subscription adapter moved from the ChatGPT Codex backend to the public
OpenAI API. Profiles saved by earlier releases still name the old endpoint; the client
factory and the startup check reject a profile whose endpoint differs from the
adapter's, so every turn failed until the profile was set up again.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from alysis_code.cli_impl.commands import startup
from alysis_code.config import AppConfig, load_config, save_config
from alysis_code.llm import factory
from alysis_code.llm.factory import make_llm_client
from alysis_code.llm.protocols import OPENAI_RESPONSES_PROTOCOL, UnsupportedProtocolError
from alysis_code.profiles import (
    ProfileSpec,
    add_profile,
    get_active_profile,
    migrate_subscription_endpoints,
    set_active_profile,
)
from alysis_code.provider_auth import (
    ProviderAccountStatus,
    provider_auth_connection,
    reasoning_effort_label,
    registry,
)
from alysis_code.provider_auth.openai_codex import OpenAICodexSubscriptionAuth

LEGACY_CODEX_URL = "https://chatgpt.com/backend-api/codex"
CURRENT_CODEX_URL = OpenAICodexSubscriptionAuth.base_url


def _write_config(directory: Path, profile: dict[str, object], *, top_level_base_url: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.json").write_text(
        json.dumps(
            {
                "base_url": top_level_base_url,
                "model": "gpt-6-luna",
                "onboarded": True,
                "active_profile": "chatgpt-codex",
                "profiles": {"chatgpt-codex": profile},
            }
        ),
        encoding="utf-8",
    )


def _legacy_profile(**overrides: object) -> dict[str, object]:
    profile: dict[str, object] = {
        "protocol": "openai_responses",
        "base_url": LEGACY_CODEX_URL,
        "auth_provider": "openai-codex",
        "default_model": "gpt-6-luna",
        "reasoning_effort": "medium",
    }
    profile.update(overrides)
    return profile


def test_the_codex_adapter_declares_its_former_endpoint() -> None:
    connection = provider_auth_connection("openai-codex")

    assert connection is not None
    assert connection.protocol == OPENAI_RESPONSES_PROTOCOL
    assert connection.base_url == CURRENT_CODEX_URL
    assert LEGACY_CODEX_URL in connection.legacy_base_urls
    assert provider_auth_connection("no-such-adapter") is None


def test_a_profile_saved_with_the_former_endpoint_loads_with_the_current_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    _write_config(tmp_path, _legacy_profile(), top_level_base_url=LEGACY_CODEX_URL)

    cfg = load_config()
    profile = get_active_profile(cfg)

    assert profile.base_url == CURRENT_CODEX_URL
    assert profile.protocol == OPENAI_RESPONSES_PROTOCOL
    assert cfg.base_url == CURRENT_CODEX_URL
    # Everything the user chose is kept.
    assert profile.auth_provider == "openai-codex"
    assert profile.default_model == "gpt-6-luna"
    assert profile.reasoning_effort == "medium"


def test_loading_does_not_write_and_the_next_save_persists_the_current_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    _write_config(tmp_path, _legacy_profile(), top_level_base_url=LEGACY_CODEX_URL)
    path = tmp_path / "config.json"
    before = path.read_bytes()

    cfg = load_config()

    assert path.read_bytes() == before
    save_config(cfg)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["profiles"]["chatgpt-codex"]["base_url"] == CURRENT_CODEX_URL
    assert saved["base_url"] == CURRENT_CODEX_URL


def test_a_different_endpoint_is_an_override_and_is_left_for_the_factory_to_reject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    _write_config(
        tmp_path,
        _legacy_profile(base_url="https://override.example/v1"),
        top_level_base_url="https://override.example/v1",
    )

    cfg = load_config()

    assert get_active_profile(cfg).base_url == "https://override.example/v1"
    with pytest.raises(UnsupportedProtocolError, match="owns its endpoint"):
        make_llm_client(cfg=cfg, api_key="", model="gpt-6-luna")


def test_profiles_without_a_subscription_or_with_an_unknown_adapter_are_untouched() -> None:
    cfg = AppConfig()
    add_profile(cfg, ProfileSpec(name="custom", base_url=LEGACY_CODEX_URL))
    add_profile(
        cfg,
        ProfileSpec(name="other", base_url=LEGACY_CODEX_URL, auth_provider="other-adapter"),
        allow_auth_profile_update=True,
    )

    assert migrate_subscription_endpoints(cfg) is False
    profiles = cfg.extra_fields["profiles"]
    assert profiles["custom"]["base_url"] == LEGACY_CODEX_URL
    assert profiles["other"]["base_url"] == LEGACY_CODEX_URL


def test_an_adapter_without_class_attributes_is_read_from_its_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = SimpleNamespace(
        protocol=OPENAI_RESPONSES_PROTOCOL,
        base_url="https://new.example/v1",
        legacy_base_urls=("https://old.example/v1/",),
    )
    monkeypatch.setitem(registry._FACTORIES, "instance-only", lambda transport=None: instance)
    cfg = AppConfig()
    add_profile(
        cfg,
        ProfileSpec(
            name="instance-only",
            protocol=OPENAI_RESPONSES_PROTOCOL,
            base_url="https://old.example/v1",
            auth_provider="instance-only",
        ),
        allow_auth_profile_update=True,
    )

    assert migrate_subscription_endpoints(cfg) is True
    assert cfg.extra_fields["profiles"]["instance-only"]["base_url"] == "https://new.example/v1"


def test_the_client_factory_accepts_a_migrated_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    _write_config(tmp_path, _legacy_profile(), top_level_base_url=LEGACY_CODEX_URL)
    adapter = SimpleNamespace(protocol=OPENAI_RESPONSES_PROTOCOL, base_url=CURRENT_CODEX_URL)
    monkeypatch.setattr(
        factory, "create_provider_auth", lambda provider_id, *, transport=None: adapter
    )

    client = make_llm_client(cfg=load_config(), api_key="", model="gpt-6-luna")

    assert client.base_url == CURRENT_CODEX_URL


def test_startup_no_longer_reports_the_profile_as_incompatible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    _write_config(tmp_path, _legacy_profile(), top_level_base_url=LEGACY_CODEX_URL)
    adapter = SimpleNamespace(
        protocol=OPENAI_RESPONSES_PROTOCOL,
        base_url=CURRENT_CODEX_URL,
        account_status=lambda: ProviderAccountStatus(connected=True),
    )
    monkeypatch.setattr(
        "alysis_code.provider_auth.create_provider_auth",
        lambda provider_id, *, transport=None: adapter,
    )

    availability = startup._subscription_availability(load_config())

    assert "incompatible" not in availability.message
    assert availability.ready is True


def test_the_delegated_codex_migration_uses_the_adapter_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(tmp_path))
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "onboarded": True,
                "execution": {"backend": "delegated", "runtime": "openai-codex"},
                "agent_runtimes": {
                    "openai-codex": {
                        "adapter": "codex-cli",
                        "executable": "codex",
                        "model": "gpt-6-luna",
                        "reasoning_effort": "high",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    profile = get_active_profile(load_config())

    assert profile.name == "chatgpt-codex"
    assert profile.base_url == CURRENT_CODEX_URL
    assert profile.protocol == OPENAI_RESPONSES_PROTOCOL


def test_set_active_profile_keeps_working_after_the_migration() -> None:
    cfg = AppConfig()
    add_profile(
        cfg,
        ProfileSpec(
            name="chatgpt-codex",
            protocol=OPENAI_RESPONSES_PROTOCOL,
            base_url=LEGACY_CODEX_URL,
            auth_provider="openai-codex",
        ),
        allow_auth_profile_update=True,
    )
    assert migrate_subscription_endpoints(cfg) is True
    set_active_profile(cfg, "chatgpt-codex")

    assert get_active_profile(cfg).base_url == CURRENT_CODEX_URL


@pytest.mark.parametrize(
    ("effort", "label"),
    [
        ("none", "None"),
        ("minimal", "Minimal"),
        ("low", "Low"),
        ("medium", "Medium"),
        ("high", "High"),
        ("xhigh", "Extra high"),
        ("max", "Max"),
        ("very_high", "Very high"),
        ("ultra-fast", "Ultra fast"),
    ],
)
def test_the_engine_names_reasoning_efforts(effort: str, label: str) -> None:
    assert reasoning_effort_label(effort) == label
