"""Canonical Alysis identity and configuration contracts."""

from __future__ import annotations

import base64
import json
import os
import runpy
import sys
from pathlib import Path

import pytest

from alysis_code import branding


@pytest.mark.parametrize(
    "source,expected",
    [({}, "fallback"), ({"ALYSIS_MODEL": ""}, ""), ({"ALYSIS_MODEL": "current"}, "current")],
)
def test_canonical_environment_mapping_and_process_agree(source, expected, monkeypatch):
    monkeypatch.delenv("ALYSIS_MODEL", raising=False)
    for name, value in source.items():
        monkeypatch.setenv(name, value)
    assert branding.env_get_from(source, "ALYSIS_MODEL", "fallback") == expected
    assert branding.env_get("ALYSIS_MODEL", "fallback") == expected


def test_canonical_user_directories_do_not_modify_existing_credentials(tmp_path, monkeypatch):
    current = tmp_path / "config"
    current.mkdir()
    credentials = current / "credentials.json"
    credentials.write_text('{"profile_keys":{"alysis":"retained"}}')
    original = credentials.read_bytes()
    calls = []

    def config_dir(app, author):
        calls.append((app, author))
        return os.fspath(current)

    monkeypatch.setattr(branding, "user_config_dir", config_dir)
    assert branding.canonical_user_config_dir() == current
    assert calls == [("alysis", "alysis")]
    assert credentials.read_bytes() == original


def test_canonical_plugin_contract(tmp_path):
    from alysis_code.extensions.manifest import Compatibility, resolve_manifest_path

    assert branding.plugin_manifest_candidates() == ("alysis-plugin.toml",)
    assert resolve_manifest_path(tmp_path) == tmp_path / "alysis-plugin.toml"
    assert Compatibility.model_validate({"alysis": ">=0.1"}).alysis == ">=0.1"


def test_existing_cloud_profile_is_preserved():
    from alysis_code.config import AppConfig
    from alysis_code.profiles import migrate_legacy_to_profiles

    profiles = {"alysis": {"name": "alysis", "protocol": "current"}}
    cfg = AppConfig(extra_fields={"profiles": profiles, "active_profile": "alysis"})
    assert migrate_legacy_to_profiles(cfg) is False
    assert cfg.extra_fields == {"profiles": profiles, "active_profile": "alysis"}


@pytest.mark.parametrize(
    "aad,accepted", [(b"alysis-mcp-oauth-store", True), (b"unrelated-oauth-domain", False)]
)
def test_oauth_envelope_authenticates_only_the_canonical_domain(
    tmp_path, monkeypatch, aad, accepted
):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    from alysis_code.mcp import token_store
    from alysis_code.mcp.errors import McpTokenStoreCorruptError

    key, nonce = bytes(range(32)), bytes(range(12))
    payload = {"servers": {"example": {"access_token": "secret"}}}
    envelope = {
        "version": token_store.CURRENT_ENVELOPE_VERSION,
        "key_source": token_store.KEY_SOURCE_KEYRING,
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ciphertext": base64.b64encode(
            AESGCM(key).encrypt(nonce, json.dumps(payload).encode(), aad)
        ).decode("ascii"),
    }
    monkeypatch.setattr(
        token_store,
        "_key_material_for_source",
        lambda _source, path: token_store._KeyMaterial(token_store.KEY_SOURCE_KEYRING, key),
    )
    monkeypatch.setattr(token_store, "_maybe_rewrite_envelope_after_read", lambda *a, **k: None)
    if accepted:
        assert token_store._decrypt_envelope(tmp_path / "tokens.json", envelope) == payload
    else:
        with pytest.raises(McpTokenStoreCorruptError, match="authentication failed"):
            token_store._decrypt_envelope(tmp_path / "tokens.json", envelope)


def test_oauth_keyring_lookup_uses_the_canonical_service(monkeypatch):
    from alysis_code.mcp import token_store

    calls = []

    def read(service, account):
        calls.append((service, account))
        return "master-key"

    monkeypatch.setattr(token_store, "_get_keyring_password", read)
    assert token_store._read_keyring_master_key() == "master-key"
    assert calls == [("alysis-code", "mcp-oauth-token-store-master-key")]


def test_product_site_override_reaches_trial_error_links(monkeypatch):
    from alysis_code import alysis_cloud
    from alysis_code.llm.openai_compat import (
        _ALYSIS_PROXY_ERROR_MESSAGES,
        alysis_trial_error_message,
    )
    from alysis_code.llm.types import LLMError

    monkeypatch.setenv("ALYSIS_SITE_URL", "https://example.com")
    assert alysis_cloud.account_url() == "https://example.com/account"
    err = LLMError(
        "LLM error 402: "
        + json.dumps({"error": {"message": "Trial window passed.", "code": "trial_expired"}})
    )
    message = alysis_trial_error_message(err)
    assert "https://example.com/account" in message
    assert "alysiscode.com" not in message
    for template in _ALYSIS_PROXY_ERROR_MESSAGES.values():
        assert "https://alysiscode.com" not in template
        assert "https://alysisai.com" not in template


def test_canonical_gateway_is_classified():
    from alysis_code.provider_url import known_provider_key_from_base_url

    assert known_provider_key_from_base_url("https://api.alysiscode.com/v1") == "deepseek"


def test_child_repetition_replay_uses_canonical_package_and_brand() -> None:
    root = Path(__file__).resolve().parents[1]
    namespace = runpy.run_path(os.fspath(root / "scripts" / "qa" / "replay_child_repetition.py"))

    fingerprint = namespace["_child_tool_outcome_fingerprint"]
    parser = namespace["_parser"]()
    assert fingerprint.__module__ == "alysis_code.agent.turn.core"
    assert "Alysis Code's real repetition" in parser.description


def test_release_signer_and_verifier_preserve_the_pinned_signature_domain() -> None:
    import re

    root = Path(__file__).resolve().parents[1]
    python_domain = re.search(
        r'^DOMAIN = "([^"]+)"',
        (root / "scripts" / "release" / "build_managed_cli_manifest.py").read_text(
            encoding="utf-8"
        ),
        re.M,
    )
    ts_domain = re.search(
        r'RELEASE_ATTESTATION_DOMAIN = "([^"]+)"',
        (
            root
            / "extensions"
            / "vscode-alysis"
            / "src"
            / "runtime"
            / "ManagedCliReleaseSecurity.ts"
        ).read_text(encoding="utf-8"),
    )

    assert python_domain is not None and ts_domain is not None
    # This is a cryptographic protocol identifier, not a user-facing alias.
    # Changing it during a branding update invalidates existing signed runtimes.
    assert python_domain.group(1) == "sylliptor-managed-cli-release-v3"
    assert python_domain.group(1) == ts_domain.group(1)


def test_custom_tool_runtime_still_loads_standalone() -> None:
    """``custom_tools/runtime.py`` must import cleanly with no parent package.

    The tool worker loads it via ``spec_from_file_location`` with a synthetic
    module name, so it has no ``__package__``. A package-relative import at
    module scope raises ImportError before any custom tool can run — and the
    failure surfaces only as ``success: False`` from every tool, not as an
    import error anyone would notice while editing.

    This is not hypothetical: adding ``from ..branding import ...`` here during
    the rename broke all 36 runtime tests exactly that way.
    """
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    runtime_path = root / "src" / "alysis_code" / "custom_tools" / "runtime.py"

    spec = importlib.util.spec_from_file_location(
        "_standalone_runtime_probe", os.fspath(runtime_path)
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)

    # Both spellings must be blocked from passthrough, since the runtime
    # injects each of them itself.
    injected = module._INJECTED_ENV_NAMES
    assert "ALYSIS_TOOL_NAME" in injected
    assert "ALYSIS_TOOL_NAME" in injected
