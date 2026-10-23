from __future__ import annotations

import base64
import ctypes
import json
import os
import stat
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from alysis_code.mcp import token_store as token_store_mod
from alysis_code.mcp.errors import (
    McpTokenStoreCorruptError,
    McpTokenStoreMigrationError,
    McpTokenStoreUnavailableError,
    McpTokenStoreVersionError,
)
from alysis_code.mcp.oauth import (
    McpOAuthCallbackError,
    McpOAuthConfigError,
    McpOAuthDiscoveryError,
    McpOAuthTokenExchangeError,
    _redact_token,
    build_authorization_url,
    build_pkce_challenge,
    canonical_mcp_resource_uri,
    discover_authorization_server_metadata,
    discover_authorization_server_metadata_from_url,
    discover_protected_resource_metadata,
    exchange_authorization_code,
    generate_oauth_state,
    generate_pkce_verifier,
    refresh_access_token,
    resolve_requested_scopes,
)
from alysis_code.mcp.oauth_runtime import perform_authorization_code_login
from alysis_code.mcp.oauth_store import (
    McpOAuthTokenRecord,
    McpOAuthTokenStoreError,
    delete_oauth_token_record,
    list_oauth_token_server_ids,
    load_oauth_token_record,
    mcp_oauth_token_store_path,
    save_oauth_token_record,
)


def _fixed_dt(
    *, year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0
) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


def _record(
    *, access_token: str, refresh_token: str | None, scopes: tuple[str, ...] = ("openid",)
) -> McpOAuthTokenRecord:
    return McpOAuthTokenRecord(
        access_token=access_token,
        token_type="Bearer",
        expires_at=_fixed_dt(year=2026, month=1, day=2, hour=3, minute=4, second=5),
        refresh_token=refresh_token,
        granted_scopes=scopes,
        obtained_at=_fixed_dt(year=2026, month=1, day=1, hour=1, minute=2, second=3),
    )


def _install_memory_keyring(monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, str], str]:
    values: dict[tuple[str, str], str] = {}

    def _get(service: str, account: str) -> str | None:
        return values.get((service, account))

    def _set(service: str, account: str, password: str) -> None:
        values[(service, account)] = password

    monkeypatch.setattr(token_store_mod, "_get_keyring_password", _get)
    monkeypatch.setattr(token_store_mod, "_set_keyring_password", _set)
    return values


@pytest.fixture(autouse=True)
def _default_memory_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    # Keep generic keyring tests independent of the host running pytest. Tests
    # for platform policy override this explicitly.
    monkeypatch.delenv(token_store_mod.OAUTH_KEY_SOURCE_ENV, raising=False)
    monkeypatch.setattr(token_store_mod, "_platform_system", lambda: "Linux")
    _install_memory_keyring(monkeypatch)


def _disable_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*args: object, **kwargs: object) -> None:
        raise RuntimeError("keyring unavailable")

    monkeypatch.setattr(token_store_mod, "_get_keyring_password", _raise)
    monkeypatch.setattr(token_store_mod, "_set_keyring_password", _raise)


def _read_store_envelope() -> dict[str, Any]:
    payload = json.loads(mcp_oauth_token_store_path().read_text(encoding="utf-8"))
    assert set(payload) == {"version", "key_source", "nonce", "ciphertext"}
    return payload


def _assert_encrypted_envelope(
    *,
    expected_key_source: str = token_store_mod.KEY_SOURCE_KEYRING,
    forbidden_values: tuple[str, ...] = ("access-one", "refresh-one"),
) -> dict[str, Any]:
    raw = mcp_oauth_token_store_path().read_text(encoding="utf-8")
    envelope = _read_store_envelope()
    assert envelope["version"] == token_store_mod.CURRENT_ENVELOPE_VERSION
    assert envelope["key_source"] == expected_key_source
    assert len(base64.b64decode(envelope["nonce"], validate=True)) == 12
    assert base64.b64decode(envelope["ciphertext"], validate=True)
    for value in forbidden_values:
        assert value not in raw
    return envelope


def _filesystem_enforces_private_mode(path: Path) -> bool:
    if os.name == "nt":
        return False
    probe = path.parent / ".mode_probe"
    probe.parent.mkdir(parents=True, exist_ok=True)
    probe.write_text("", encoding="utf-8")
    os.chmod(probe, 0o600)
    return stat.S_IMODE(probe.stat().st_mode) == 0o600


def _legacy_payload(record: McpOAuthTokenRecord | None = None) -> dict[str, Any]:
    return {
        "alpha": (
            record or _record(access_token="access-one", refresh_token="refresh-one")
        ).as_payload()
    }


def _write_legacy_store(payload: dict[str, Any]) -> str:
    path = mcp_oauth_token_store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    path.write_text(raw, encoding="utf-8")
    return raw


class _FakeWinApiFunction:
    def __init__(self, callback: Any) -> None:
        self.callback = callback
        self.argtypes: list[Any] | None = None
        self.restype: Any = None
        self.calls: list[tuple[Any, ...]] = []

    def __call__(self, *args: Any) -> Any:
        self.calls.append(args)
        return self.callback(*args)


@dataclass
class _FakeWinApis:
    crypt32: Any
    kernel32: Any


def _fake_winapis(*, success: bool = True, output: bytes = b"protected") -> _FakeWinApis:
    allocated: list[Any] = []

    class _Crypt32:
        pass

    class _Kernel32:
        pass

    def _crypt(*args: Any) -> bool:
        if not success:
            return False
        out_blob = args[-1]._obj
        buffer = ctypes.create_string_buffer(output)
        allocated.append(buffer)
        out_blob.cbData = len(output)
        out_blob.pbData = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
        return True

    def _local_free(pointer: Any) -> None:
        return None

    crypt32 = _Crypt32()
    crypt32.CryptProtectData = _FakeWinApiFunction(_crypt)
    crypt32.CryptUnprotectData = _FakeWinApiFunction(_crypt)
    kernel32 = _Kernel32()
    kernel32.LocalFree = _FakeWinApiFunction(_local_free)
    return _FakeWinApis(crypt32=crypt32, kernel32=kernel32)


def test_token_store_path_uses_config_dir_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))

    assert mcp_oauth_token_store_path() == cfg_dir / "mcp_oauth_tokens.json"


def test_token_store_save_load_delete_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))

    record = _record(access_token="access-one", refresh_token="refresh-one")
    save_oauth_token_record("alpha", record)

    loaded = load_oauth_token_record("alpha")
    assert loaded == record
    assert list_oauth_token_server_ids() == ("alpha",)
    _assert_encrypted_envelope()
    assert delete_oauth_token_record("alpha") is True
    assert load_oauth_token_record("alpha") is None
    assert delete_oauth_token_record("alpha") is False
    _assert_encrypted_envelope()


def test_token_store_atomic_overwrite_replaces_entry_and_preserves_other_servers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))

    save_oauth_token_record(
        "alpha", _record(access_token="access-one", refresh_token="refresh-one")
    )
    save_oauth_token_record("beta", _record(access_token="access-beta", refresh_token=None))
    save_oauth_token_record(
        "alpha",
        _record(
            access_token="access-two",
            refresh_token="refresh-two",
            scopes=("openid", "profile"),
        ),
    )

    alpha = load_oauth_token_record("alpha")
    beta = load_oauth_token_record("beta")
    assert alpha is not None
    assert beta is not None
    assert alpha.access_token == "access-two"
    assert alpha.refresh_token == "refresh-two"
    assert alpha.granted_scopes == ("openid", "profile")
    assert beta.access_token == "access-beta"
    _assert_encrypted_envelope(
        forbidden_values=("access-one", "refresh-one", "access-two", "refresh-two", "access-beta")
    )


def test_token_store_refresh_token_replacement_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))

    save_oauth_token_record(
        "alpha", _record(access_token="access-one", refresh_token="refresh-one")
    )
    save_oauth_token_record(
        "alpha", _record(access_token="access-two", refresh_token="refresh-rotated")
    )

    loaded = load_oauth_token_record("alpha")
    assert loaded is not None
    assert loaded.refresh_token == "refresh-rotated"
    _assert_encrypted_envelope(forbidden_values=("refresh-one", "refresh-rotated"))


def test_token_store_keyring_envelope_happy_path_generates_master_key_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    keyring_values = _install_memory_keyring(monkeypatch)
    set_calls: list[str] = []

    def _set(service: str, account: str, password: str) -> None:
        set_calls.append(password)
        keyring_values[(service, account)] = password

    monkeypatch.setattr(token_store_mod, "_set_keyring_password", _set)

    record = _record(
        access_token="access-one",
        refresh_token="refresh-one",
        scopes=("openid", "profile"),
    )
    save_oauth_token_record("alpha", record)
    save_oauth_token_record(
        "beta",
        _record(access_token="access-two", refresh_token="refresh-two"),
    )

    assert len(set_calls) == 1
    assert load_oauth_token_record("alpha") == record
    assert list_oauth_token_server_ids() == ("alpha", "beta")
    _assert_encrypted_envelope(
        expected_key_source=token_store_mod.KEY_SOURCE_KEYRING,
        forbidden_values=("access-one", "refresh-one", "access-two", "refresh-two"),
    )


def test_token_store_filesystem_random_fallback_round_trip_is_silent_and_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _disable_keyring(monkeypatch)
    monkeypatch.setattr(token_store_mod, "_platform_system", lambda: "Linux")

    with caplog.at_level("WARNING", logger=token_store_mod.__name__):
        save_oauth_token_record(
            "alpha", _record(access_token="access-one", refresh_token="refresh-one")
        )

    key_path = token_store_mod.filesystem_master_key_path(mcp_oauth_token_store_path())
    first_key = key_path.read_bytes()
    loaded = load_oauth_token_record("alpha")
    loaded_again = load_oauth_token_record("alpha")
    assert loaded is not None
    assert loaded.access_token == "access-one"
    assert loaded_again == loaded
    assert len(first_key) == 32
    assert key_path.read_bytes() == first_key
    assert "weak-derived-fallback" not in caplog.text
    _assert_encrypted_envelope(
        expected_key_source=token_store_mod.KEY_SOURCE_FILESYSTEM,
        forbidden_values=("access-one", "refresh-one"),
    )
    if _filesystem_enforces_private_mode(key_path):
        assert stat.S_IMODE(key_path.stat().st_mode) == 0o600


def test_filesystem_master_key_creation_is_race_safe(tmp_path: Path) -> None:
    store_path = tmp_path / "oauth_tokens.json"
    barrier = threading.Barrier(8)
    lock = threading.Lock()
    keys: list[bytes] = []
    errors: list[BaseException] = []

    def _load_key() -> None:
        try:
            barrier.wait(timeout=5)
            material = token_store_mod._filesystem_key_material(store_path)
            with lock:
                keys.append(material.key)
        except BaseException as exc:  # noqa: BLE001 - preserve worker failure for assertion
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=_load_key) for _ in range(barrier.parties)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    assert len(keys) == barrier.parties
    assert len(set(keys)) == 1
    key_path = token_store_mod.filesystem_master_key_path(store_path)
    assert key_path.read_bytes() == keys[0]
    assert not tuple(tmp_path.glob(f".{key_path.name}.*.tmp"))


def test_filesystem_master_key_rejects_invalid_length(tmp_path: Path) -> None:
    store_path = tmp_path / "oauth_tokens.json"
    key_path = token_store_mod.filesystem_master_key_path(store_path)
    key_path.write_bytes(b"too-short")

    with pytest.raises(McpTokenStoreCorruptError, match="invalid length"):
        token_store_mod._filesystem_key_material(store_path)


def test_current_envelope_writer_rejects_legacy_deterministic_key_source(tmp_path: Path) -> None:
    path = tmp_path / "oauth_tokens.json"
    legacy_material = token_store_mod._KeyMaterial(
        token_store_mod.KEY_SOURCE_WEAK_FALLBACK,
        b"0" * 32,
    )

    with pytest.raises(McpTokenStoreUnavailableError, match="legacy key source"):
        token_store_mod._write_encrypted_payload(
            path,
            _legacy_payload(),
            key_material=legacy_material,
        )

    assert not path.exists()


def test_token_store_keyring_read_failure_logs_without_leaking_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _disable_keyring(monkeypatch)
    monkeypatch.setattr(token_store_mod, "_platform_system", lambda: "Linux")

    with caplog.at_level("DEBUG", logger=token_store_mod.__name__):
        save_oauth_token_record(
            "alpha", _record(access_token="secret-access", refresh_token="secret-refresh")
        )

    assert "OS keyring is unavailable" in caplog.text
    assert "reason=read-failed" in caplog.text
    assert "secret-access" not in caplog.text
    assert "secret-refresh" not in caplog.text


def test_token_store_keyring_write_failure_logs_without_leaking_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    monkeypatch.setattr(token_store_mod, "_get_keyring_password", lambda service, account: None)

    def _raise(*args: object, **kwargs: object) -> None:
        raise RuntimeError("keyring write unavailable")

    monkeypatch.setattr(token_store_mod, "_set_keyring_password", _raise)
    monkeypatch.setattr(token_store_mod, "_platform_system", lambda: "Linux")

    with caplog.at_level("DEBUG", logger=token_store_mod.__name__):
        save_oauth_token_record(
            "alpha", _record(access_token="secret-access", refresh_token="secret-refresh")
        )

    assert "OS keyring is unavailable" in caplog.text
    assert "reason=write-failed" in caplog.text
    assert "secret-access" not in caplog.text
    assert "secret-refresh" not in caplog.text


def test_token_store_falls_back_when_the_keyring_silently_discards_the_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """``keyring.backends.null`` accepts writes and drops them.

    Claiming the keyring key source against such a backend produces an envelope
    that no later process can decrypt, so the write must fall back instead.
    """

    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    monkeypatch.setattr(token_store_mod, "_get_keyring_password", lambda service, account: None)
    monkeypatch.setattr(
        token_store_mod,
        "_set_keyring_password",
        lambda service, account, password: None,
    )
    monkeypatch.setattr(token_store_mod, "_platform_system", lambda: "Linux")

    with caplog.at_level("WARNING", logger=token_store_mod.__name__):
        save_oauth_token_record(
            "alpha", _record(access_token="secret-access", refresh_token="secret-refresh")
        )

    assert "reason=write-not-persisted" in caplog.text
    assert "secret-access" not in caplog.text
    outcome = token_store_mod.last_keyring_outcome()
    assert outcome is not None and outcome.available is False
    _assert_encrypted_envelope(
        expected_key_source=token_store_mod.KEY_SOURCE_FILESYSTEM,
        forbidden_values=("secret-access", "secret-refresh"),
    )
    # The credentials survive a second process reading the same store.
    assert load_oauth_token_record("alpha") is not None


def test_keyring_availability_rejects_placeholder_backends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import keyring
    import keyring.backends.fail
    import keyring.backends.null

    for backend in (keyring.backends.null.Keyring, keyring.backends.fail.Keyring):
        monkeypatch.setattr(keyring, "get_keyring", backend)
        outcome = token_store_mod.keyring_availability()
        assert outcome.available is False
        assert outcome.reason == "unusable-backend"
        assert outcome.backend == f"{backend.__module__}.{backend.__name__}"


def test_planned_key_source_reports_the_fallback_without_creating_key_material(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store_path = tmp_path / "oauth_tokens.json"
    monkeypatch.setattr(
        token_store_mod,
        "keyring_availability",
        lambda: token_store_mod.KeyringOutcome(available=False, reason="unusable-backend"),
    )
    monkeypatch.setattr(token_store_mod, "_platform_system", lambda: "Linux")

    assert token_store_mod.planned_key_source() == token_store_mod.KEY_SOURCE_FILESYSTEM
    assert not token_store_mod.filesystem_master_key_path(store_path).exists()
    assert not store_path.exists()

    monkeypatch.setattr(token_store_mod, "_platform_system", lambda: "Windows")
    assert token_store_mod.planned_key_source() == token_store_mod.KEY_SOURCE_DPAPI
    assert not token_store_mod.dpapi_master_key_path(store_path).exists()

    monkeypatch.setattr(
        token_store_mod,
        "keyring_availability",
        lambda: token_store_mod.KeyringOutcome(available=True, backend="test.Backend"),
    )
    assert token_store_mod.planned_key_source() == token_store_mod.KEY_SOURCE_KEYRING


def test_planned_key_source_uses_explicit_filesystem_policy_without_probing_keyring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The desktop app sets this policy for its bundled runtime on every platform.
    monkeypatch.setenv(token_store_mod.OAUTH_KEY_SOURCE_ENV, token_store_mod.KEY_SOURCE_FILESYSTEM)
    monkeypatch.setattr(token_store_mod, "_platform_system", lambda: "Darwin")

    def _unexpected_probe() -> token_store_mod.KeyringOutcome:
        raise AssertionError("the filesystem policy must bypass keyring probing")

    monkeypatch.setattr(token_store_mod, "keyring_availability", _unexpected_probe)

    assert token_store_mod.planned_key_source() == token_store_mod.KEY_SOURCE_FILESYSTEM


def test_planned_key_source_keeps_keyring_first_on_macos_without_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(token_store_mod.OAUTH_KEY_SOURCE_ENV, raising=False)
    monkeypatch.delenv("ALYSIS_OAUTH_KEY_SOURCE", raising=False)
    monkeypatch.setattr(token_store_mod, "_platform_system", lambda: "Darwin")
    monkeypatch.setattr(
        token_store_mod,
        "keyring_availability",
        lambda: token_store_mod.KeyringOutcome(available=True),
    )

    assert token_store_mod.planned_key_source() == token_store_mod.KEY_SOURCE_KEYRING


def test_stored_key_source_reads_the_envelope_without_decrypting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _disable_keyring(monkeypatch)
    monkeypatch.setattr(token_store_mod, "_platform_system", lambda: "Linux")
    save_oauth_token_record("alpha", _record(access_token="a", refresh_token="r"))
    path = mcp_oauth_token_store_path()

    assert token_store_mod.stored_key_source(path) == token_store_mod.KEY_SOURCE_FILESYSTEM
    assert token_store_mod.stored_key_source(tmp_path / "absent.json") is None
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    assert token_store_mod.stored_key_source(tmp_path / "broken.json") is None


def test_token_store_windows_dpapi_fallback_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _disable_keyring(monkeypatch)
    monkeypatch.setattr(token_store_mod, "_platform_system", lambda: "Windows")
    calls: dict[str, int] = {"protect": 0, "unprotect": 0}

    def _protect(data: bytes) -> bytes:
        calls["protect"] += 1
        return b"dpapi:" + base64.b64encode(data)

    def _unprotect(data: bytes) -> bytes:
        calls["unprotect"] += 1
        assert data.startswith(b"dpapi:")
        return base64.b64decode(data.removeprefix(b"dpapi:"))

    monkeypatch.setattr(token_store_mod, "_dpapi_protect", _protect)
    monkeypatch.setattr(token_store_mod, "_dpapi_unprotect", _unprotect)

    save_oauth_token_record(
        "alpha", _record(access_token="access-one", refresh_token="refresh-one")
    )

    assert load_oauth_token_record("alpha") is not None
    assert calls["protect"] == 1
    assert calls["unprotect"] >= 1
    assert token_store_mod.dpapi_master_key_path(mcp_oauth_token_store_path()).exists()
    _assert_encrypted_envelope(
        expected_key_source=token_store_mod.KEY_SOURCE_DPAPI,
        forbidden_values=("access-one", "refresh-one"),
    )


def test_dpapi_ctypes_wrapper_configures_signatures_uses_ui_forbidden_and_frees_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    apis = _fake_winapis(output=b"dpapi-output")
    monkeypatch.setattr(token_store_mod, "_is_windows_os", lambda: True)
    monkeypatch.setattr(
        token_store_mod, "_load_windows_crypto_api", lambda: (apis.crypt32, apis.kernel32)
    )
    token_store_mod._configure_windows_crypto_api(apis.crypt32, apis.kernel32)

    protected = token_store_mod._dpapi_protect(b"secret-input")
    unprotected = token_store_mod._dpapi_unprotect(b"protected-input")

    assert protected == b"dpapi-output"
    assert unprotected == b"dpapi-output"
    assert apis.crypt32.CryptProtectData.argtypes is not None
    assert apis.crypt32.CryptProtectData.restype is token_store_mod.wintypes.BOOL
    assert apis.crypt32.CryptUnprotectData.argtypes is not None
    assert apis.crypt32.CryptUnprotectData.restype is token_store_mod.wintypes.BOOL
    assert apis.kernel32.LocalFree.argtypes == [ctypes.c_void_p]
    assert apis.kernel32.LocalFree.restype is ctypes.c_void_p
    assert apis.crypt32.CryptProtectData.calls[0][5] == token_store_mod._CRYPTPROTECT_UI_FORBIDDEN
    assert apis.crypt32.CryptUnprotectData.calls[0][5] == token_store_mod._CRYPTPROTECT_UI_FORBIDDEN
    assert len(apis.kernel32.LocalFree.calls) == 2


def test_dpapi_ctypes_wrapper_raises_typed_error_on_native_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    apis = _fake_winapis(success=False)
    monkeypatch.setattr(token_store_mod, "_is_windows_os", lambda: True)
    monkeypatch.setattr(
        token_store_mod, "_load_windows_crypto_api", lambda: (apis.crypt32, apis.kernel32)
    )

    with pytest.raises(McpOAuthTokenStoreError) as excinfo:
        token_store_mod._dpapi_protect(b"secret-input")

    assert "DPAPI" in str(excinfo.value)
    assert apis.kernel32.LocalFree.calls == []


def test_token_store_legacy_plaintext_migrates_to_encrypted_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    legacy_record = _record(access_token="legacy-access", refresh_token="legacy-refresh")
    _write_legacy_store(_legacy_payload(legacy_record))

    loaded = load_oauth_token_record("alpha")

    assert loaded == legacy_record
    _assert_encrypted_envelope(forbidden_values=("legacy-access", "legacy-refresh"))


def test_token_store_legacy_plaintext_missing_load_still_migrates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    legacy_record = _record(access_token="legacy-access", refresh_token="legacy-refresh")
    _write_legacy_store(_legacy_payload(legacy_record))

    loaded = load_oauth_token_record("missing")

    assert loaded is None
    _assert_encrypted_envelope(forbidden_values=("legacy-access", "legacy-refresh"))
    assert load_oauth_token_record("alpha") == legacy_record


def test_token_store_legacy_plaintext_missing_delete_still_migrates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    legacy_record = _record(access_token="legacy-access", refresh_token="legacy-refresh")
    _write_legacy_store(_legacy_payload(legacy_record))

    deleted = delete_oauth_token_record("missing")

    assert deleted is False
    _assert_encrypted_envelope(forbidden_values=("legacy-access", "legacy-refresh"))
    assert load_oauth_token_record("alpha") == legacy_record


def test_token_store_legacy_plaintext_invalid_record_is_not_migrated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    original = _write_legacy_store(
        {
            "alpha": {
                "access_token": "legacy-access",
                "token_type": "",
                "expires_at": "2026-01-02T03:04:05Z",
                "refresh_token": "legacy-refresh",
                "granted_scopes": ["openid"],
                "obtained_at": "2026-01-01T01:02:03Z",
            }
        }
    )

    with pytest.raises(McpOAuthTokenStoreError):
        load_oauth_token_record("alpha")

    assert mcp_oauth_token_store_path().read_text(encoding="utf-8") == original


def test_token_store_legacy_plaintext_server_id_matching_envelope_field_migrates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    record = _record(access_token="version-access", refresh_token="version-refresh")
    _write_legacy_store({"version": record.as_payload()})

    loaded = load_oauth_token_record("version")

    assert loaded == record
    _assert_encrypted_envelope(forbidden_values=("version-access", "version-refresh"))


def test_token_store_legacy_plaintext_two_envelope_field_server_ids_migrate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    version_record = _record(access_token="version-access", refresh_token="version-refresh")
    key_source_record = _record(access_token="source-access", refresh_token="source-refresh")
    _write_legacy_store(
        {
            "version": version_record.as_payload(),
            "key_source": key_source_record.as_payload(),
        }
    )

    loaded = load_oauth_token_record("version")

    assert loaded == version_record
    assert load_oauth_token_record("key_source") == key_source_record
    _assert_encrypted_envelope(
        forbidden_values=(
            "version-access",
            "version-refresh",
            "source-access",
            "source-refresh",
        )
    )


def test_token_store_legacy_plaintext_three_envelope_field_server_ids_migrate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    version_record = _record(access_token="version-access", refresh_token="version-refresh")
    key_source_record = _record(access_token="source-access", refresh_token="source-refresh")
    nonce_record = _record(access_token="nonce-access", refresh_token="nonce-refresh")
    _write_legacy_store(
        {
            "version": version_record.as_payload(),
            "key_source": key_source_record.as_payload(),
            "nonce": nonce_record.as_payload(),
        }
    )

    loaded = load_oauth_token_record("nonce")

    assert loaded == nonce_record
    assert load_oauth_token_record("version") == version_record
    assert load_oauth_token_record("key_source") == key_source_record
    _assert_encrypted_envelope(
        forbidden_values=(
            "version-access",
            "version-refresh",
            "source-access",
            "source-refresh",
            "nonce-access",
            "nonce-refresh",
        )
    )


def test_token_store_legacy_plaintext_all_envelope_field_server_ids_migrate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    version_record = _record(access_token="version-access", refresh_token="version-refresh")
    key_source_record = _record(access_token="source-access", refresh_token="source-refresh")
    nonce_record = _record(access_token="nonce-access", refresh_token="nonce-refresh")
    ciphertext_record = _record(access_token="cipher-access", refresh_token="cipher-refresh")
    _write_legacy_store(
        {
            "version": version_record.as_payload(),
            "key_source": key_source_record.as_payload(),
            "nonce": nonce_record.as_payload(),
            "ciphertext": ciphertext_record.as_payload(),
        }
    )

    loaded = load_oauth_token_record("ciphertext")

    assert loaded == ciphertext_record
    assert load_oauth_token_record("version") == version_record
    assert load_oauth_token_record("key_source") == key_source_record
    assert load_oauth_token_record("nonce") == nonce_record
    _assert_encrypted_envelope(
        forbidden_values=(
            "version-access",
            "version-refresh",
            "source-access",
            "source-refresh",
            "nonce-access",
            "nonce-refresh",
            "cipher-access",
            "cipher-refresh",
        )
    )


def test_token_store_rejects_envelope_with_extra_fields_as_corrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    path = mcp_oauth_token_store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = {
        "version": token_store_mod.CURRENT_ENVELOPE_VERSION,
        "key_source": token_store_mod.KEY_SOURCE_KEYRING,
        "nonce": base64.b64encode(b"0" * 12).decode("ascii"),
        "ciphertext": base64.b64encode(b"ciphertext").decode("ascii"),
        "extra": "not allowed",
    }
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(McpTokenStoreCorruptError):
        load_oauth_token_record("alpha")


def test_token_store_rejects_partial_envelope_markers_as_corrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    path = mcp_oauth_token_store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = {
        "version": token_store_mod.CURRENT_ENVELOPE_VERSION,
        "key_source": token_store_mod.KEY_SOURCE_KEYRING,
        "nonce": base64.b64encode(b"0" * 12).decode("ascii"),
    }
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(McpTokenStoreCorruptError):
        load_oauth_token_record("alpha")


def test_token_store_rejects_ambiguous_envelope_marker_payload_as_corrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    path = mcp_oauth_token_store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = {
        "version": token_store_mod.CURRENT_ENVELOPE_VERSION,
        "key_source": {"not": "a token record"},
        "nonce": base64.b64encode(b"0" * 12).decode("ascii"),
    }
    original = json.dumps(raw, sort_keys=True)
    path.write_text(original, encoding="utf-8")

    with pytest.raises(McpTokenStoreCorruptError):
        load_oauth_token_record("alpha")

    assert path.read_text(encoding="utf-8") == original


def test_token_store_legacy_plaintext_survives_failed_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    original = _write_legacy_store(_legacy_payload())

    def _boom(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(token_store_mod, "_secure_atomic_write_bytes", _boom)

    with pytest.raises(McpTokenStoreMigrationError):
        load_oauth_token_record("alpha")

    assert mcp_oauth_token_store_path().read_text(encoding="utf-8") == original


def test_token_store_rejects_corrupt_envelope_with_typed_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    path = mcp_oauth_token_store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": token_store_mod.CURRENT_ENVELOPE_VERSION,
                "key_source": token_store_mod.KEY_SOURCE_KEYRING,
                "nonce": base64.b64encode(b"short").decode("ascii"),
                "ciphertext": "not-base64!",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(McpTokenStoreCorruptError):
        load_oauth_token_record("alpha")


def test_token_store_rejects_newer_envelope_version_without_rewrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    path = mcp_oauth_token_store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(
        {
            "version": token_store_mod.CURRENT_ENVELOPE_VERSION + 1,
            "key_source": token_store_mod.KEY_SOURCE_KEYRING,
            "nonce": base64.b64encode(b"0" * 12).decode("ascii"),
            "ciphertext": base64.b64encode(b"ciphertext").decode("ascii"),
        },
        sort_keys=True,
    )
    path.write_text(raw, encoding="utf-8")

    with pytest.raises(McpTokenStoreVersionError):
        load_oauth_token_record("alpha")

    assert path.read_text(encoding="utf-8") == raw


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits only")
def test_token_store_permissions_are_restrictive_on_posix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))

    save_oauth_token_record(
        "alpha", _record(access_token="access-one", refresh_token="refresh-one")
    )

    path = mcp_oauth_token_store_path()
    if _filesystem_enforces_private_mode(path):
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600


def test_token_store_never_writes_into_project_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    project_store_path = tmp_path / ".alysis" / "mcp_oauth_tokens.json"

    save_oauth_token_record(
        "alpha", _record(access_token="access-one", refresh_token="refresh-one")
    )

    assert mcp_oauth_token_store_path().exists() is True
    assert project_store_path.exists() is False


def test_token_store_repr_and_errors_do_not_leak_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))

    record = _record(access_token="secret-access", refresh_token="secret-refresh")
    assert "secret-access" not in repr(record)
    assert "secret-refresh" not in repr(record)

    _write_legacy_store(
        {
            "alpha": {
                "access_token": "secret-access",
                "token_type": "",
                "expires_at": "2026-01-02T03:04:05Z",
                "refresh_token": "secret-refresh",
                "granted_scopes": ["openid"],
                "obtained_at": "2026-01-01T01:02:03Z",
            }
        }
    )

    with pytest.raises(McpOAuthTokenStoreError) as excinfo:
        load_oauth_token_record("alpha")

    message = str(excinfo.value)
    assert "secret-access" not in message
    assert "secret-refresh" not in message
    assert "alpha" in message


def test_token_store_save_preserves_previous_envelope_when_atomic_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    save_oauth_token_record(
        "alpha", _record(access_token="access-one", refresh_token="refresh-one")
    )
    original = mcp_oauth_token_store_path().read_text(encoding="utf-8")

    def _boom(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(token_store_mod, "_secure_atomic_write_bytes", _boom)

    with pytest.raises(McpOAuthTokenStoreError) as excinfo:
        save_oauth_token_record(
            "alpha", _record(access_token="access-two", refresh_token="refresh-two")
        )

    message = str(excinfo.value)
    assert "alpha" in message
    assert "mcp_oauth_tokens.json" in message
    assert mcp_oauth_token_store_path().read_text(encoding="utf-8") == original


def test_token_store_delete_wraps_atomic_write_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    save_oauth_token_record(
        "alpha", _record(access_token="access-one", refresh_token="refresh-one")
    )

    def _boom(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(token_store_mod, "_secure_atomic_write_bytes", _boom)

    with pytest.raises(McpOAuthTokenStoreError) as excinfo:
        delete_oauth_token_record("alpha")

    message = str(excinfo.value)
    assert "alpha" in message
    assert "mcp_oauth_tokens.json" in message


def test_token_store_reencrypts_filesystem_fallback_when_keyring_becomes_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _disable_keyring(monkeypatch)
    monkeypatch.setattr(token_store_mod, "_platform_system", lambda: "Linux")
    save_oauth_token_record(
        "alpha", _record(access_token="access-one", refresh_token="refresh-one")
    )
    _assert_encrypted_envelope(expected_key_source=token_store_mod.KEY_SOURCE_FILESYSTEM)

    _install_memory_keyring(monkeypatch)
    loaded = load_oauth_token_record("alpha")

    assert loaded is not None
    assert loaded.access_token == "access-one"
    _assert_encrypted_envelope(
        expected_key_source=token_store_mod.KEY_SOURCE_KEYRING,
        forbidden_values=("access-one", "refresh-one"),
    )


def test_token_store_migrates_v1_weak_fallback_to_random_filesystem_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _disable_keyring(monkeypatch)
    monkeypatch.setattr(token_store_mod, "_platform_system", lambda: "Linux")
    current_version = token_store_mod.CURRENT_ENVELOPE_VERSION
    path = mcp_oauth_token_store_path()
    monkeypatch.setattr(token_store_mod, "CURRENT_ENVELOPE_VERSION", 1)
    token_store_mod._write_encrypted_payload(
        path,
        _legacy_payload(),
        key_material=token_store_mod._weak_fallback_key_material(path),
    )
    assert _read_store_envelope()["key_source"] == token_store_mod.KEY_SOURCE_WEAK_FALLBACK

    monkeypatch.setattr(token_store_mod, "CURRENT_ENVELOPE_VERSION", current_version)
    loaded = load_oauth_token_record("alpha")

    assert loaded is not None
    assert loaded.access_token == "access-one"
    assert token_store_mod.filesystem_master_key_path(path).exists()
    _assert_encrypted_envelope(
        expected_key_source=token_store_mod.KEY_SOURCE_FILESYSTEM,
        forbidden_values=("access-one", "refresh-one"),
    )


def test_token_store_rotation_preferred_key_failure_is_best_effort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _disable_keyring(monkeypatch)
    monkeypatch.setattr(token_store_mod, "_platform_system", lambda: "Linux")
    record = _record(access_token="secret-access", refresh_token="secret-refresh")
    save_oauth_token_record("alpha", record)
    _assert_encrypted_envelope(
        expected_key_source=token_store_mod.KEY_SOURCE_FILESYSTEM,
        forbidden_values=("secret-access", "secret-refresh"),
    )
    original = mcp_oauth_token_store_path().read_text(encoding="utf-8")
    monkeypatch.setattr(
        token_store_mod,
        "_get_keyring_password",
        lambda service, account: base64.b64encode(b"short").decode("ascii"),
    )

    with caplog.at_level("WARNING", logger=token_store_mod.__name__):
        loaded = load_oauth_token_record("alpha")

    assert loaded == record
    assert mcp_oauth_token_store_path().read_text(encoding="utf-8") == original
    assert "Skipped OAuth credential store rotation after successful decrypt" in caplog.text
    assert "secret-access" not in caplog.text
    assert "secret-refresh" not in caplog.text


def test_token_store_rotation_rewrite_failure_is_best_effort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    record = _record(access_token="secret-access", refresh_token="secret-refresh")
    save_oauth_token_record("alpha", record)
    original = mcp_oauth_token_store_path().read_text(encoding="utf-8")
    monkeypatch.setattr(
        token_store_mod,
        "CURRENT_ENVELOPE_VERSION",
        token_store_mod.CURRENT_ENVELOPE_VERSION + 1,
    )

    def _boom(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(token_store_mod, "_secure_atomic_write_bytes", _boom)

    with caplog.at_level("WARNING", logger=token_store_mod.__name__):
        loaded = load_oauth_token_record("alpha")

    assert loaded == record
    assert mcp_oauth_token_store_path().read_text(encoding="utf-8") == original
    assert "Skipped OAuth credential store rewrite after successful decrypt" in caplog.text
    assert "secret-access" not in caplog.text
    assert "secret-refresh" not in caplog.text


def test_token_store_reencrypts_older_supported_envelope_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    current_version = token_store_mod.CURRENT_ENVELOPE_VERSION
    monkeypatch.setattr(token_store_mod, "CURRENT_ENVELOPE_VERSION", 1)
    save_oauth_token_record(
        "alpha", _record(access_token="access-one", refresh_token="refresh-one")
    )
    envelope = _read_store_envelope()
    assert envelope["version"] == 1
    monkeypatch.setattr(token_store_mod, "CURRENT_ENVELOPE_VERSION", current_version)

    loaded = load_oauth_token_record("alpha")

    assert loaded is not None
    assert _read_store_envelope()["version"] == current_version


def test_redact_token_reports_length_without_leaking_value() -> None:
    assert _redact_token(None) == "[redacted:0chars]"
    assert _redact_token("") == "[redacted:0chars]"
    assert _redact_token("secret-token") == "[redacted:12chars]"


def test_generate_pkce_verifier_uses_allowed_charset_and_length() -> None:
    verifier = generate_pkce_verifier()

    assert 43 <= len(verifier) <= 128
    assert all(ch.isalnum() or ch in "-._~" for ch in verifier)


def test_build_pkce_challenge_matches_rfc7636_vector() -> None:
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    challenge = build_pkce_challenge(verifier)

    assert challenge == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    assert "=" not in challenge


def test_build_pkce_challenge_rejects_invalid_verifier() -> None:
    with pytest.raises(McpOAuthConfigError) as excinfo:
        build_pkce_challenge("short")

    message = str(excinfo.value)
    assert "McpOAuthConfigError" in message
    assert "pkce" in message


def test_generate_oauth_state_is_lower_hex_with_32_random_bytes() -> None:
    state_one = generate_oauth_state()
    state_two = generate_oauth_state()

    assert len(state_one) == 64
    assert len(state_two) == 64
    assert state_one != state_two
    assert all(ch in "0123456789abcdef" for ch in state_one)
    assert all(ch in "0123456789abcdef" for ch in state_two)


def test_oauth_errors_include_server_id_origin_and_redacted_tokens() -> None:
    error = McpOAuthDiscoveryError(
        f"authorization failed for access token {_redact_token('secret-access')}",
        server_id="alpha",
        authorization_server_url="https://auth.example.com/token",
    )

    message = str(error)
    assert "McpOAuthDiscoveryError" in message
    assert "alpha" in message
    assert "https://auth.example.com" in message
    assert "secret-access" not in message
    assert "[redacted:13chars]" in message


def test_oauth_discovery_uses_resource_metadata_from_www_authenticate(oauth_fixture_server) -> None:
    response = httpx.get(oauth_fixture_server.protected_url)

    metadata = discover_authorization_server_metadata(
        server_id="alpha",
        resource_server_url=oauth_fixture_server.protected_url,
        unauthorized_response=response,
    )

    assert metadata.issuer == oauth_fixture_server.authorization_server_url
    assert oauth_fixture_server.resource_metadata_url.endswith(
        "/.well-known/oauth-protected-resource"
    )
    assert "/.well-known/oauth-protected-resource" in oauth_fixture_server.request_log


def test_oauth_discovery_blocks_loopback_authorization_metadata_without_fixture_patch() -> None:
    with pytest.raises(McpOAuthDiscoveryError) as exc_info:
        discover_authorization_server_metadata_from_url(
            server_id="alpha",
            authorization_server_url="http://127.0.0.1:9",
            timeout_s=0.01,
        )

    assert "authorization server discovery failed" in str(exc_info.value)
    assert exc_info.value.__cause__ is not None
    assert "request blocked" in str(exc_info.value.__cause__)


def test_oauth_discovery_falls_back_to_protected_resource_well_known_when_challenge_lacks_metadata(
    oauth_fixture_server,
) -> None:
    oauth_fixture_server.challenge_includes_resource_metadata = False
    response = httpx.get(oauth_fixture_server.protected_url)

    metadata = discover_authorization_server_metadata(
        server_id="alpha",
        resource_server_url=oauth_fixture_server.protected_url,
        unauthorized_response=response,
    )

    assert metadata.authorization_endpoint == oauth_fixture_server.authorization_endpoint
    assert oauth_fixture_server.path_resource_metadata_path in oauth_fixture_server.request_log


def test_oauth_discovery_tries_path_specific_protected_resource_well_known_before_root_fallback(
    oauth_fixture_server,
) -> None:
    oauth_fixture_server.protected_resource_path = "/server/mcp"
    oauth_fixture_server.challenge_includes_resource_metadata = False
    oauth_fixture_server.serve_path_protected_resource_metadata = False

    metadata = discover_protected_resource_metadata(
        server_id="alpha",
        resource_server_url=oauth_fixture_server.protected_url,
    )

    assert metadata.authorization_servers == (oauth_fixture_server.authorization_server_url,)
    assert oauth_fixture_server.request_log[:2] == [
        oauth_fixture_server.path_resource_metadata_path,
        "/.well-known/oauth-protected-resource",
    ]


def test_oauth_discovery_uses_rfc8414_path_for_authorization_server_urls_with_path(
    oauth_fixture_server,
) -> None:
    oauth_fixture_server.authorization_server_path = "/tenant-one"

    metadata = discover_authorization_server_metadata_from_url(
        server_id="alpha",
        authorization_server_url=oauth_fixture_server.authorization_server_url,
    )

    assert metadata.issuer == oauth_fixture_server.authorization_server_url
    assert oauth_fixture_server.rfc8414_metadata_path in oauth_fixture_server.request_log


def test_oauth_discovery_tries_full_path_based_oidc_fallback_chain_for_authorization_server_urls(
    oauth_fixture_server,
) -> None:
    oauth_fixture_server.authorization_server_path = "/tenant-one"
    oauth_fixture_server.serve_rfc8414 = False
    oauth_fixture_server.serve_oidc_inserted_metadata = False

    metadata = discover_authorization_server_metadata_from_url(
        server_id="alpha",
        authorization_server_url=oauth_fixture_server.authorization_server_url,
    )

    assert metadata.authorization_endpoint == oauth_fixture_server.authorization_endpoint
    assert oauth_fixture_server.request_log[:3] == [
        oauth_fixture_server.rfc8414_metadata_path,
        oauth_fixture_server.oidc_inserted_metadata_path,
        oauth_fixture_server.oidc_appended_metadata_path,
    ]


def test_oauth_discovery_path_appended_oidc_fallback_strips_query_and_fragment(
    oauth_fixture_server,
) -> None:
    oauth_fixture_server.authorization_server_path = "/tenant-one"
    oauth_fixture_server.serve_rfc8414 = False
    oauth_fixture_server.serve_oidc_inserted_metadata = False

    metadata = discover_authorization_server_metadata_from_url(
        server_id="alpha",
        authorization_server_url=f"{oauth_fixture_server.authorization_server_url}?foo=bar#frag",
    )

    assert metadata.authorization_endpoint == oauth_fixture_server.authorization_endpoint
    assert oauth_fixture_server.request_log[:3] == [
        oauth_fixture_server.rfc8414_metadata_path,
        oauth_fixture_server.oidc_inserted_metadata_path,
        oauth_fixture_server.oidc_appended_metadata_path,
    ]


def test_oauth_discovery_falls_back_to_oidc_when_rfc8414_lookup_fails(oauth_fixture_server) -> None:
    oauth_fixture_server.serve_rfc8414 = False

    metadata = discover_authorization_server_metadata_from_url(
        server_id="alpha",
        authorization_server_url=oauth_fixture_server.authorization_server_url,
    )

    assert metadata.token_endpoint == oauth_fixture_server.token_endpoint
    assert oauth_fixture_server.rfc8414_metadata_path in oauth_fixture_server.request_log
    assert oauth_fixture_server.oidc_metadata_path in oauth_fixture_server.request_log


def test_oauth_discovery_does_not_fallback_when_rfc8414_metadata_is_malformed(
    oauth_fixture_server,
) -> None:
    oauth_fixture_server.authorization_metadata_override = {
        "issuer": oauth_fixture_server.authorization_server_url,
    }

    with pytest.raises(McpOAuthDiscoveryError) as excinfo:
        discover_authorization_server_metadata_from_url(
            server_id="alpha",
            authorization_server_url=oauth_fixture_server.authorization_server_url,
        )

    message = str(excinfo.value)
    assert "alpha" in message
    assert oauth_fixture_server.base_url in message
    assert oauth_fixture_server.rfc8414_metadata_path in oauth_fixture_server.request_log
    assert oauth_fixture_server.oidc_metadata_path not in oauth_fixture_server.request_log


def test_oauth_discovery_rejects_malformed_oidc_fallback_metadata(oauth_fixture_server) -> None:
    oauth_fixture_server.serve_rfc8414 = False
    oauth_fixture_server.oidc_metadata_override = {
        "issuer": oauth_fixture_server.authorization_server_url,
    }

    with pytest.raises(McpOAuthDiscoveryError) as excinfo:
        discover_authorization_server_metadata_from_url(
            server_id="alpha",
            authorization_server_url=oauth_fixture_server.authorization_server_url,
        )

    message = str(excinfo.value)
    assert "alpha" in message
    assert oauth_fixture_server.base_url in message
    assert oauth_fixture_server.rfc8414_metadata_path in oauth_fixture_server.request_log
    assert oauth_fixture_server.oidc_metadata_path in oauth_fixture_server.request_log


def test_oauth_discovery_normalizes_protected_resource_scopes_supported_deterministically(
    oauth_fixture_server,
) -> None:
    oauth_fixture_server.protected_resource_payload_override = {
        "resource": oauth_fixture_server.protected_url,
        "authorization_servers": [oauth_fixture_server.authorization_server_url],
        "scopes_supported": ["openid", " profile ", "openid", "email", "profile"],
    }

    metadata = discover_protected_resource_metadata(
        server_id="alpha",
        resource_server_url=oauth_fixture_server.protected_url,
    )

    assert metadata.scopes_supported == ("openid", "profile", "email")


def test_resolve_requested_scopes_login_precedence() -> None:
    assert resolve_requested_scopes(
        configured_scopes=("openid", "profile"),
        challenge_scope="ignored email",
        metadata_scopes_supported=("ignored",),
        existing_granted_scopes=("ignored",),
        purpose="login",
    ) == ("openid", "profile")
    assert resolve_requested_scopes(
        configured_scopes=None,
        challenge_scope="openid  profile openid",
        metadata_scopes_supported=("email",),
        existing_granted_scopes=None,
        purpose="login",
    ) == ("openid", "profile")
    assert resolve_requested_scopes(
        configured_scopes=None,
        challenge_scope=None,
        metadata_scopes_supported=("openid", " profile ", "openid"),
        existing_granted_scopes=None,
        purpose="login",
    ) == ("openid", "profile")
    assert (
        resolve_requested_scopes(
            configured_scopes=None,
            challenge_scope=None,
            metadata_scopes_supported=None,
            existing_granted_scopes=None,
            purpose="login",
        )
        == ()
    )


def test_resolve_requested_scopes_refresh_precedence() -> None:
    assert resolve_requested_scopes(
        configured_scopes=("openid",),
        challenge_scope="ignored",
        metadata_scopes_supported=("ignored",),
        existing_granted_scopes=("profile",),
        purpose="refresh",
    ) == ("openid",)
    assert resolve_requested_scopes(
        configured_scopes=None,
        challenge_scope="ignored",
        metadata_scopes_supported=("ignored",),
        existing_granted_scopes=("openid", " profile ", "openid"),
        purpose="refresh",
    ) == ("openid", "profile")
    assert (
        resolve_requested_scopes(
            configured_scopes=None,
            challenge_scope=None,
            metadata_scopes_supported=("ignored",),
            existing_granted_scopes=None,
            purpose="refresh",
        )
        == ()
    )


def _discovered_metadata(oauth_fixture_server) -> object:
    return discover_authorization_server_metadata_from_url(
        server_id="alpha",
        authorization_server_url=oauth_fixture_server.authorization_server_url,
    )


def test_canonical_mcp_resource_uri_normalizes_scheme_host_path_and_strips_query() -> None:
    assert canonical_mcp_resource_uri("https://mcp.example.com/") == "https://mcp.example.com"
    assert (
        canonical_mcp_resource_uri("https://MCP.example.com/MCP") == "https://mcp.example.com/MCP"
    )
    assert (
        canonical_mcp_resource_uri("https://mcp.example.com:8443/server/mcp?x=1#frag")
        == "https://mcp.example.com:8443/server/mcp"
    )


def test_build_authorization_url_includes_canonical_resource_parameter(
    oauth_fixture_server,
) -> None:
    metadata = _discovered_metadata(oauth_fixture_server)
    authorization_url = build_authorization_url(
        server_id="alpha",
        authorization_server_metadata=metadata,
        resource_server_url="https://MCP.example.com/MCP?x=1",
        client_id="test-client",
        redirect_uri="http://127.0.0.1:8765/oauth/callback",
        code_challenge=build_pkce_challenge(generate_pkce_verifier()),
        state=generate_oauth_state(),
        scopes=("openid",),
    )

    params = parse_qs(urlsplit(authorization_url).query)
    assert params["resource"] == ["https://mcp.example.com/MCP"]


def test_exchange_authorization_code_sends_canonical_resource_parameter(
    oauth_fixture_server,
) -> None:
    metadata = _discovered_metadata(oauth_fixture_server)
    verifier = generate_pkce_verifier()
    oauth_fixture_server.expected_code_challenge = build_pkce_challenge(verifier)
    oauth_fixture_server.expected_token_resource = "https://mcp.example.com/MCP"

    exchange_authorization_code(
        server_id="alpha",
        authorization_server_metadata=metadata,
        resource_server_url="https://MCP.example.com/MCP?x=1",
        client_id="test-client",
        code="TEST_CODE",
        redirect_uri="http://127.0.0.1:8765/oauth/callback",
        code_verifier=verifier,
        requested_scopes=("openid",),
    )

    assert oauth_fixture_server.token_requests[-1]["resource"] == "https://mcp.example.com/MCP"


def test_localhost_callback_login_succeeds_and_persists_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, oauth_fixture_server
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    metadata = _discovered_metadata(oauth_fixture_server)
    oauth_fixture_server.expected_authorize_resource = oauth_fixture_server.protected_url
    oauth_fixture_server.expected_token_resource = oauth_fixture_server.protected_url

    def _browser_opener(url: str) -> bool:
        response = httpx.get(url, follow_redirects=True, timeout=5.0)
        assert response.status_code == 200
        return True

    result = perform_authorization_code_login(
        server_id="alpha",
        authorization_server_metadata=metadata,
        resource_server_url=oauth_fixture_server.protected_url,
        client_id="test-client",
        scopes=("openid", "profile"),
        browser_opener=_browser_opener,
    )

    stored = load_oauth_token_record("alpha")
    assert stored == result.token_record
    assert stored is not None
    assert stored.access_token == "test_access"
    assert stored.refresh_token == "test_refresh"
    assert stored.granted_scopes == ("openid", "profile")
    assert result.browser_opened is True
    assert "/authorize" in oauth_fixture_server.request_log
    assert "/token" in oauth_fixture_server.request_log
    assert (
        oauth_fixture_server.authorize_requests[-1]["resource"]
        == oauth_fixture_server.protected_url
    )
    assert oauth_fixture_server.token_requests[-1]["resource"] == oauth_fixture_server.protected_url


def test_browser_open_failure_prints_manual_url_and_login_still_completes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    oauth_fixture_server,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    metadata = _discovered_metadata(oauth_fixture_server)
    requests: list[threading.Thread] = []

    def _browser_opener(url: str) -> bool:
        worker = threading.Thread(
            target=lambda: httpx.get(url, follow_redirects=True, timeout=5.0),
            daemon=True,
        )
        worker.start()
        requests.append(worker)
        return False

    result = perform_authorization_code_login(
        server_id="alpha",
        authorization_server_metadata=metadata,
        resource_server_url=oauth_fixture_server.protected_url,
        client_id="test-client",
        scopes=("openid",),
        browser_opener=_browser_opener,
    )
    for worker in requests:
        worker.join(timeout=5.0)

    output = capsys.readouterr().out
    assert result.browser_opened is False
    assert "Open this URL in a browser to continue MCP OAuth login:" in output
    assert result.authorization_url in output
    assert "Redirect URI:" in output
    assert "test_access" not in output
    assert "test_refresh" not in output


def test_login_fails_on_callback_state_mismatch_without_leaking_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, oauth_fixture_server
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    metadata = _discovered_metadata(oauth_fixture_server)

    def _browser_opener(url: str) -> bool:
        redirect_uri = parse_qs(urlsplit(url).query)["redirect_uri"][0]
        response = httpx.get(f"{redirect_uri}?code=TEST_CODE&state=wrong-state", timeout=5.0)
        assert response.status_code == 200
        return True

    with pytest.raises(McpOAuthCallbackError) as excinfo:
        perform_authorization_code_login(
            server_id="alpha",
            authorization_server_metadata=metadata,
            resource_server_url=oauth_fixture_server.protected_url,
            client_id="test-client",
            browser_opener=_browser_opener,
        )

    message = str(excinfo.value)
    assert "alpha" in message
    assert oauth_fixture_server.base_url in message
    assert "mismatched state" in message
    assert "TEST_CODE" not in message
    assert load_oauth_token_record("alpha") is None


def test_login_times_out_waiting_for_callback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, oauth_fixture_server
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    metadata = _discovered_metadata(oauth_fixture_server)

    with pytest.raises(McpOAuthCallbackError) as excinfo:
        perform_authorization_code_login(
            server_id="alpha",
            authorization_server_metadata=metadata,
            resource_server_url=oauth_fixture_server.protected_url,
            client_id="test-client",
            browser_opener=lambda url: True,
            timeout_s=1.0,
        )

    message = str(excinfo.value)
    assert "timed out waiting 1s" in message
    assert load_oauth_token_record("alpha") is None


def test_login_interrupt_raises_callback_error_and_shuts_down_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, oauth_fixture_server
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    metadata = _discovered_metadata(oauth_fixture_server)
    invoked = {"count": 0}

    def _interrupt() -> None:
        invoked["count"] += 1
        raise KeyboardInterrupt()

    with pytest.raises(McpOAuthCallbackError) as excinfo:
        perform_authorization_code_login(
            server_id="alpha",
            authorization_server_metadata=metadata,
            resource_server_url=oauth_fixture_server.protected_url,
            client_id="test-client",
            browser_opener=lambda url: True,
            interrupt_check=_interrupt,
        )

    message = str(excinfo.value)
    assert invoked["count"] >= 1
    assert "interrupted" in message
    assert load_oauth_token_record("alpha") is None


def test_refresh_access_token_rotates_refresh_token_and_can_be_persisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, oauth_fixture_server
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    metadata = _discovered_metadata(oauth_fixture_server)
    oauth_fixture_server.expected_token_resource = oauth_fixture_server.protected_url
    save_oauth_token_record(
        "alpha", _record(access_token="expired-access", refresh_token="test_refresh")
    )

    refreshed = refresh_access_token(
        server_id="alpha",
        authorization_server_metadata=metadata,
        resource_server_url=oauth_fixture_server.protected_url,
        client_id="test-client",
        refresh_token="test_refresh",
        requested_scopes=("openid",),
    )
    save_oauth_token_record("alpha", refreshed)

    stored = load_oauth_token_record("alpha")
    assert stored is not None
    assert stored.access_token == "refreshed_access"
    assert stored.refresh_token == "rotated_refresh"
    assert stored.granted_scopes == ("openid",)
    assert oauth_fixture_server.token_requests[-1]["scope"] == "openid"
    assert oauth_fixture_server.token_requests[-1]["resource"] == oauth_fixture_server.protected_url
    assert "test_refresh" not in mcp_oauth_token_store_path().read_text(encoding="utf-8")


def test_refresh_access_token_preserves_existing_refresh_token_when_server_does_not_rotate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, oauth_fixture_server
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    oauth_fixture_server.rotate_refresh_token = False
    metadata = _discovered_metadata(oauth_fixture_server)
    oauth_fixture_server.expected_token_resource = oauth_fixture_server.protected_url
    save_oauth_token_record(
        "alpha", _record(access_token="expired-access", refresh_token="test_refresh")
    )

    refreshed = refresh_access_token(
        server_id="alpha",
        authorization_server_metadata=metadata,
        resource_server_url=oauth_fixture_server.protected_url,
        client_id="test-client",
        refresh_token="test_refresh",
        requested_scopes=("openid", "profile"),
    )
    save_oauth_token_record("alpha", refreshed)

    stored = load_oauth_token_record("alpha")
    assert stored is not None
    assert stored.access_token == "refreshed_access"
    assert stored.refresh_token == "test_refresh"
    assert stored.granted_scopes == ("openid", "profile")
    assert oauth_fixture_server.token_requests[-1]["resource"] == oauth_fixture_server.protected_url


def test_refresh_access_token_preserves_existing_granted_scopes_when_response_omits_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, oauth_fixture_server
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    oauth_fixture_server.rotate_refresh_token = False
    oauth_fixture_server.refresh_response_override = {
        "access_token": "refreshed_access",
        "token_type": "Bearer",
        "expires_in": 3600,
    }
    metadata = _discovered_metadata(oauth_fixture_server)
    oauth_fixture_server.expected_token_resource = oauth_fixture_server.protected_url

    refreshed = refresh_access_token(
        server_id="alpha",
        authorization_server_metadata=metadata,
        resource_server_url=oauth_fixture_server.protected_url,
        client_id="test-client",
        refresh_token="test_refresh",
        requested_scopes=None,
        existing_granted_scopes=("openid", "profile"),
    )

    assert refreshed.access_token == "refreshed_access"
    assert refreshed.refresh_token == "test_refresh"
    assert refreshed.granted_scopes == ("openid", "profile")
    assert oauth_fixture_server.token_requests[-1]["resource"] == oauth_fixture_server.protected_url


def test_localhost_callback_login_rejects_non_bearer_token_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, oauth_fixture_server
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    oauth_fixture_server.authorization_code_response_override = {
        "access_token": "test_access",
        "token_type": "MAC",
        "expires_in": 3600,
    }
    metadata = _discovered_metadata(oauth_fixture_server)

    def _browser_opener(url: str) -> bool:
        response = httpx.get(url, follow_redirects=True, timeout=5.0)
        assert response.status_code == 200
        return True

    with pytest.raises(McpOAuthTokenExchangeError) as excinfo:
        perform_authorization_code_login(
            server_id="alpha",
            authorization_server_metadata=metadata,
            resource_server_url=oauth_fixture_server.protected_url,
            client_id="test-client",
            browser_opener=_browser_opener,
        )

    message = str(excinfo.value)
    assert "token_type" in message
    assert "Bearer" in message
    assert load_oauth_token_record("alpha") is None


def test_refresh_access_token_rejects_non_bearer_token_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, oauth_fixture_server
) -> None:
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    oauth_fixture_server.refresh_response_override = {
        "access_token": "refreshed_access",
        "token_type": "MAC",
        "expires_in": 3600,
    }
    metadata = _discovered_metadata(oauth_fixture_server)

    with pytest.raises(McpOAuthTokenExchangeError) as excinfo:
        refresh_access_token(
            server_id="alpha",
            authorization_server_metadata=metadata,
            resource_server_url=oauth_fixture_server.protected_url,
            client_id="test-client",
            refresh_token="test_refresh",
            requested_scopes=("openid",),
        )

    message = str(excinfo.value)
    assert "token_type" in message
    assert "Bearer" in message
