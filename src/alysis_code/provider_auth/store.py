from __future__ import annotations

import copy
import errno
import os
import re
import secrets
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from io import BufferedRandom
from pathlib import Path
from typing import Any

from ..config import config_path
from ..mcp.token_store import (
    load_token_payload_result,
    migrate_legacy_token_payload,
    save_token_payload,
)
from .base import ProviderAuthError

_STORE_LOCK = threading.RLock()
_CREDENTIAL_GENERATION_RE = re.compile(r"^cg_[a-f0-9]{32}$")
_STORE_LOCK_TIMEOUT_SECONDS = 10.0
_STORE_LOCK_POLL_SECONDS = 0.05

if os.name == "nt":  # pragma: no cover - exercised on Windows
    import msvcrt
else:  # pragma: no cover - exercised on POSIX
    import fcntl


def _new_credential_generation() -> str:
    return f"cg_{secrets.token_hex(16)}"


@dataclass(frozen=True, slots=True)
class ProviderTokenRecord:
    access_token: str
    refresh_token: str
    expires_at: float
    account_id: str | None = None
    account_label: str | None = None
    # Random, non-secret route generation. New login records get a new value;
    # refreshes explicitly preserve it.
    credential_generation: str = field(default_factory=_new_credential_generation)
    # SIWC registration is bound to one validated subject and workspace/client.
    client_id: str | None = None
    subject: str | None = None
    id_token: str | None = field(default=None, repr=False)
    scopes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not _CREDENTIAL_GENERATION_RE.fullmatch(self.credential_generation):
            raise ProviderAuthError(
                "Stored provider credential identity is invalid; connect again."
            )

    @classmethod
    def from_dict(cls, value: Any) -> ProviderTokenRecord:
        if not isinstance(value, dict):
            raise ProviderAuthError("Stored provider credentials are invalid; connect again.")
        access_token = str(value.get("access_token") or "").strip()
        refresh_token = str(value.get("refresh_token") or "").strip()
        try:
            expires_at = float(value.get("expires_at") or 0)
        except (TypeError, ValueError) as exc:
            raise ProviderAuthError(
                "Stored provider credentials are invalid; connect again."
            ) from exc
        if not access_token or not refresh_token or expires_at <= 0:
            raise ProviderAuthError("Stored provider credentials are incomplete; connect again.")
        account_id = str(value.get("account_id") or "").strip() or None
        account_label = str(value.get("account_label") or "").strip() or None
        credential_generation = str(value.get("credential_generation") or "").strip()
        return cls(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=expires_at,
            account_id=account_id,
            account_label=account_label,
            client_id=str(value.get("client_id") or "").strip() or None,
            subject=str(value.get("subject") or "").strip() or None,
            id_token=str(value.get("id_token") or "").strip() or None,
            scopes=tuple(item for item in (value.get("scopes") or []) if isinstance(item, str)),
            **({"credential_generation": credential_generation} if credential_generation else {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def provider_token_store_path() -> Path:
    return config_path().parent / "provider_auth_tokens.json"


def load_provider_token(provider_id: str) -> ProviderTokenRecord | None:
    normalized = _provider_id(provider_id)
    path = provider_token_store_path()
    with _locked_provider_store(path):
        try:
            payload = _load_provider_payload(path)
        except Exception as exc:  # noqa: BLE001 - secret-store backends vary by OS
            raise ProviderAuthError(
                "Could not read the encrypted provider credential store."
            ) from exc
        value = payload.get(normalized)
        if value is None:
            return None
        record = ProviderTokenRecord.from_dict(copy.deepcopy(value))
        if isinstance(value, dict) and not str(value.get("credential_generation") or "").strip():
            # One-time legacy migration under the interprocess lock. Concurrent
            # readers cannot mint competing generations for the same credential.
            payload[normalized] = record.to_dict()
            try:
                save_token_payload(path, payload)
            except Exception as exc:  # noqa: BLE001 - secret-store backends vary by OS
                raise ProviderAuthError(
                    "Could not migrate the encrypted provider credential store."
                ) from exc
        return record


def save_provider_token(provider_id: str, record: ProviderTokenRecord) -> None:
    """Persist a login/account replacement record with its newly minted generation."""

    normalized = _provider_id(provider_id)
    path = provider_token_store_path()
    with _locked_provider_store(path):
        try:
            payload = _load_provider_payload(path)
            payload[normalized] = record.to_dict()
            _remember_registration(payload, normalized, record)
            save_token_payload(path, payload)
        except Exception as exc:  # noqa: BLE001 - never leak token values through backend errors
            raise ProviderAuthError(
                "Could not save credentials in the encrypted provider store."
            ) from exc


def save_refreshed_provider_token(
    provider_id: str,
    *,
    expected: ProviderTokenRecord,
    refreshed: ProviderTokenRecord,
) -> ProviderTokenRecord:
    """CAS a refresh without allowing stale account A to overwrite replacement B."""

    normalized = _provider_id(provider_id)
    if refreshed.credential_generation != expected.credential_generation:
        raise ProviderAuthError("Provider credential refresh changed its route identity.")
    path = provider_token_store_path()
    with _locked_provider_store(path):
        try:
            payload = _load_provider_payload(path)
            value = payload.get(normalized)
            if value is None:
                raise ProviderAuthError("Provider credentials were removed; connect again.")
            current = ProviderTokenRecord.from_dict(copy.deepcopy(value))
            if current != expected:
                # Another process either completed this refresh or replaced the
                # account. Its generation is authoritative; never restore stale A.
                return current
            payload[normalized] = refreshed.to_dict()
            _remember_registration(payload, normalized, refreshed)
            save_token_payload(path, payload)
            return refreshed
        except ProviderAuthError:
            raise
        except Exception as exc:  # noqa: BLE001 - never leak token values through backend errors
            raise ProviderAuthError(
                "Could not update credentials in the encrypted provider store."
            ) from exc


def delete_provider_token(provider_id: str) -> bool:
    normalized = _provider_id(provider_id)
    path = provider_token_store_path()
    with _locked_provider_store(path):
        try:
            payload = _load_provider_payload(path)
            removed_record = payload.pop(normalized, None)
            removed = removed_record is not None
            if isinstance(removed_record, dict) and removed_record.get("client_id"):
                metadata = payload.get("$subscription_registrations", {}).get(normalized, {})
                registrations = metadata.get("registrations", {})
                client_id = removed_record["client_id"]
                if client_id in registrations:
                    # Retain registration identity, clear every token on sign-out.
                    registrations[client_id] = {
                        key: removed_record.get(key)
                        for key in ("client_id", "subject", "account_label")
                    }
            if removed:
                save_token_payload(path, payload)
            return removed
        except Exception as exc:  # noqa: BLE001
            raise ProviderAuthError(
                "Could not update the encrypted provider credential store."
            ) from exc


def _remember_registration(
    payload: dict[str, Any], provider_id: str, record: ProviderTokenRecord
) -> None:
    if not record.client_id:
        return
    metadata = payload.setdefault("$subscription_registrations", {}).setdefault(provider_id, {})
    metadata.setdefault("registrations", {})[record.client_id] = record.to_dict()
    metadata["selected_client_id"] = record.client_id


def remember_subscription_registration(provider_id: str, record: ProviderTokenRecord) -> None:
    """Remember a validated but inactive registration without replacing the account."""
    path = provider_token_store_path()
    with _locked_provider_store(path):
        try:
            payload = _load_provider_payload(path)
            metadata = payload.setdefault("$subscription_registrations", {}).setdefault(
                provider_id, {}
            )
            metadata.setdefault("registrations", {})[record.client_id] = record.to_dict()
            save_token_payload(path, payload)
        except Exception as exc:  # noqa: BLE001
            raise ProviderAuthError("Could not save the encrypted ChatGPT registration.") from exc


def subscription_registration(provider_id: str) -> dict[str, Any]:
    """Selected registration hints and stable host identity; never expose to the UI."""
    path = provider_token_store_path()
    with _locked_provider_store(path):
        try:
            payload = _load_provider_payload(path)
            metadata = payload.setdefault("$subscription_registrations", {}).setdefault(
                provider_id, {}
            )
            if not metadata.get("host_id"):
                metadata["host_id"] = f"urn:uuid:{uuid.uuid4()}"
                save_token_payload(path, payload)
            selected = metadata.get("selected_client_id")
            registration = metadata.get("registrations", {}).get(selected, {})
            return {**copy.deepcopy(registration), "host_id": metadata["host_id"]}
        except Exception as exc:  # noqa: BLE001
            raise ProviderAuthError("Could not read the encrypted ChatGPT registration.") from exc


def _provider_id(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ProviderAuthError("Provider id cannot be empty.")
    return normalized


def _load_provider_payload(path: Path) -> dict[str, Any]:
    result = load_token_payload_result(path)
    if result.legacy_plaintext:
        migrate_legacy_token_payload(path, result.payload)
    return result.payload


@contextmanager
def _locked_provider_store(path: Path) -> Iterator[None]:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ProviderAuthError("Could not open the provider credential store.") from exc
    lock_path = path.with_name(f"{path.name}.lock")
    if not _STORE_LOCK.acquire(timeout=_STORE_LOCK_TIMEOUT_SECONDS):
        raise ProviderAuthError("The provider credential store is busy; try again shortly.")
    try:
        try:
            handle = lock_path.open("a+b")
        except OSError as exc:
            raise ProviderAuthError("Could not lock the provider credential store.") from exc
        with handle:
            if os.name != "nt":
                try:
                    os.chmod(lock_path, 0o600)
                except OSError:
                    pass
            _acquire_file_lock(handle)
            try:
                yield
            finally:
                _release_file_lock(handle)
    finally:
        _STORE_LOCK.release()


def _acquire_file_lock(handle: BufferedRandom) -> None:
    deadline = time.monotonic() + _STORE_LOCK_TIMEOUT_SECONDS
    while True:
        try:
            if os.name == "nt":  # pragma: no cover - exercised on Windows
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise ProviderAuthError("Could not lock the provider credential store.") from exc
            if time.monotonic() >= deadline:
                raise ProviderAuthError(
                    "The provider credential store is busy; try again shortly."
                ) from exc
            time.sleep(_STORE_LOCK_POLL_SECONDS)


def _release_file_lock(handle: BufferedRandom) -> None:
    if os.name == "nt":  # pragma: no cover - exercised on Windows
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


__all__ = [
    "ProviderTokenRecord",
    "delete_provider_token",
    "load_provider_token",
    "provider_token_store_path",
    "save_refreshed_provider_token",
    "save_provider_token",
]
