from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..config import (
    resolve_anthropic_prompt_cache_enabled,
    resolve_anthropic_prompt_cache_ttl,
    resolve_prompt_cache_mode,
)
from .cache_capabilities import (
    CACHE_CONTROL_FIELD,
    CACHE_STRATEGY_GEMINI_IMPLICIT,
    CACHE_STRATEGY_IMPLICIT_PROVIDER,
    CACHED_CONTENT_FIELD,
    OPENROUTER_SESSION_ID_FIELD,
    OPENROUTER_SESSION_ID_HEADER_FIELD,
    PROMPT_CACHE_KEY_FIELD,
    PROMPT_CACHE_RETENTION_FIELD,
    XAI_CONVERSATION_ID_HEADER_FIELD,
    EffectiveCacheCapability,
    resolve_effective_cache_capability,
)
from .protocols import ProviderProtocolCapabilities

if TYPE_CHECKING:
    from ..config import AppConfig


_CACHE_KEY_LABEL_RE = re.compile(r"[^a-z0-9_.:-]+")


@dataclass(frozen=True)
class ResolvedPromptCachePolicy:
    prompt_cache_key: str | None = None
    prompt_cache_retention: str | None = None
    anthropic_cache_control_enabled: bool = False
    anthropic_cache_control_ttl: str = "5m"
    gemini_explicit_cached_content_enabled: bool = False
    gemini_cached_content_ttl: str | None = None
    implicit_cache_enabled: bool = False
    mode: str = "manual"
    strategy: str = "none"
    capability_source: str = "default"
    allowed_fields: tuple[str, ...] = ()
    emitted_fields: tuple[str, ...] = ()
    trusted_usage_fields: tuple[str, ...] = ()
    usage_schema: str = "none"
    min_cacheable_tokens: int | None = None
    request_field_values: tuple[tuple[str, str], ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def status(self) -> str:
        if self.mode == "off":
            return "disabled"
        if self.emitted_fields or self.implicit_cache_enabled:
            return "enabled"
        if self.strategy == "none":
            return "unsupported"
        if not self.allowed_fields and self.warnings:
            return "unsupported"
        return "available"

    def telemetry_metadata(self) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "status": self.status,
            "strategy": self.strategy,
            "mode": "automatic" if self.mode == "auto" else self.mode,
            "enabled": self.status == "enabled",
            "implicit_cache_enabled": self.implicit_cache_enabled,
            "capability_source": self.capability_source,
            "source": self.capability_source,
            "allowed_fields": list(self.allowed_fields),
            "emitted_fields": list(self.emitted_fields),
            "trusted_usage_fields": list(self.trusted_usage_fields),
            "usage_schema": self.usage_schema,
            "emits_request_fields": bool(self.allowed_fields),
        }
        if self.prompt_cache_retention:
            metadata["retention"] = self.prompt_cache_retention
        if self.prompt_cache_key and PROMPT_CACHE_KEY_FIELD in self.emitted_fields:
            metadata["prompt_cache_key_hash"] = hashlib.sha256(
                self.prompt_cache_key.encode("utf-8")
            ).hexdigest()
        if self.anthropic_cache_control_enabled:
            metadata["ttl"] = self.anthropic_cache_control_ttl
        elif self.gemini_explicit_cached_content_enabled and self.gemini_cached_content_ttl:
            metadata["ttl"] = self.gemini_cached_content_ttl
        if self.min_cacheable_tokens is not None:
            metadata["min_tokens"] = self.min_cacheable_tokens
        if self.warnings:
            metadata["warnings"] = list(self.warnings)
        return metadata


def build_prompt_cache_namespace(
    *,
    workspace_root: str | os.PathLike[str] | None = None,
    role: str | None = None,
    profile_name: str | None = None,
    session_id: str | None = None,
) -> str | None:
    parts: list[tuple[str, str]] = []
    if workspace_root is not None:
        parts.append(("workspace", _normalize_workspace_root(workspace_root)))
    normalized_role = str(role or "").strip().lower()
    if normalized_role:
        parts.append(("role", normalized_role))
    normalized_profile = str(profile_name or "").strip().lower()
    if normalized_profile:
        parts.append(("profile", normalized_profile))
    normalized_session = str(session_id or "").strip().lower()
    if normalized_session:
        parts.append(("session", normalized_session))
    if not parts:
        return None
    return json.dumps(parts, ensure_ascii=False, separators=(",", ":"))


def derive_prompt_cache_stream_key(
    *,
    session_id: str,
    parent_session_id: str | None = None,
    digest_only: bool = False,
) -> str | None:
    """Return one stable cache-affinity key for a parent or its child stream.

    ``digest_only`` returns a blake2s digest for a parent too, never the raw id.
    """
    normalized_parent = str(parent_session_id or "").strip()
    if normalized_parent:
        digest = hashlib.blake2s(
            f"{normalized_parent}:children".encode(),
            digest_size=12,
        ).hexdigest()
        return f"alysis:children:{digest}"
    normalized_session = str(session_id or "").strip()
    if normalized_session and digest_only:
        digest = hashlib.blake2s(
            f"{normalized_session}:session".encode(),
            digest_size=12,
        ).hexdigest()
        return f"alysis:session:{digest}"
    return normalized_session or None


def resolve_prompt_cache_policy(
    *,
    cfg: AppConfig | None,
    capabilities: ProviderProtocolCapabilities | None,
    provider_key: str,
    protocol: str,
    model: str,
    prompt_cache_key: str | None,
    prompt_cache_retention: str | None,
    prompt_cache_namespace: str | None = None,
    cache_capability: EffectiveCacheCapability | None = None,
) -> ResolvedPromptCachePolicy:
    mode = resolve_prompt_cache_mode(cfg)
    ttl = resolve_anthropic_prompt_cache_ttl(cfg)
    hosted_ttl_warning: tuple[str, ...] = ()
    if provider_key == "alysis" and protocol == "anthropic_messages" and ttl != "5m":
        ttl = "5m"
        hosted_ttl_warning = ("Hosted Sonnet supports only five-minute prompt caching.",)
    effective_capability = cache_capability or resolve_effective_cache_capability(
        provider_key=provider_key,
        protocol=protocol,
        model=model,
        transport_capabilities=capabilities,
    )
    if mode == "off" or not effective_capability.enabled:
        return ResolvedPromptCachePolicy(
            mode=mode,
            anthropic_cache_control_ttl=ttl,
            strategy=effective_capability.strategy,
            capability_source=effective_capability.source,
            allowed_fields=effective_capability.emitted_fields,
            emitted_fields=(),
            trusted_usage_fields=effective_capability.trusted_usage_fields,
            usage_schema=effective_capability.usage_schema,
            min_cacheable_tokens=effective_capability.min_cacheable_tokens,
            warnings=(*effective_capability.warnings, *hosted_ttl_warning),
        )

    strategy = effective_capability.strategy
    cache_config = getattr(cfg, "cache", None)
    prompt_cache_key_enabled = cache_config is None or bool(
        getattr(cache_config, "prompt_cache_key_enabled", True)
    )
    explicit_key = _clean_optional(prompt_cache_key) if prompt_cache_key_enabled else None
    explicit_retention = _clean_optional(prompt_cache_retention)
    effective_key = explicit_key
    needs_affinity_key = any(
        field
        in {
            PROMPT_CACHE_KEY_FIELD,
            OPENROUTER_SESSION_ID_FIELD,
            OPENROUTER_SESSION_ID_HEADER_FIELD,
            XAI_CONVERSATION_ID_HEADER_FIELD,
        }
        for field in effective_capability.emitted_fields
    )
    if prompt_cache_key_enabled and mode == "auto" and effective_key is None and needs_affinity_key:
        effective_key = _auto_prompt_cache_key(
            provider_key=provider_key,
            protocol=protocol,
            model=model,
            namespace=prompt_cache_namespace,
        )

    anthropic_enabled = False
    if effective_capability.supports_cache_control:
        anthropic_enabled = mode == "auto" or resolve_anthropic_prompt_cache_enabled(cfg)
    gemini_explicit_enabled = (
        effective_capability.supports_explicit_cached_content and mode == "auto"
    )
    warnings = (*effective_capability.warnings, *hosted_ttl_warning)
    gemini_ttl: str | None = None
    if gemini_explicit_enabled:
        gemini_ttl, gemini_ttl_fallback = _google_duration_from_retention(explicit_retention)
        if gemini_ttl_fallback:
            warnings = (
                *warnings,
                f"gemini_cached_content_ttl_fallback_{gemini_ttl}_for_unparseable_retention",
            )
    request_field_values = _policy_request_field_values(
        emitted_fields=effective_capability.emitted_fields,
        prompt_cache_key=(
            effective_key
            if effective_capability.supports_prompt_cache_key and effective_key
            else None
        ),
        prompt_cache_retention=(
            explicit_retention
            if effective_capability.supports_prompt_cache_retention and explicit_retention
            else None
        ),
        affinity_key=effective_key,
        anthropic_cache_control_enabled=anthropic_enabled,
        gemini_explicit_cached_content_enabled=gemini_explicit_enabled,
    )
    emitted_fields = _policy_emitted_fields(
        request_field_values=request_field_values,
    )

    return ResolvedPromptCachePolicy(
        prompt_cache_key=(
            effective_key
            if effective_capability.supports_prompt_cache_key and effective_key
            else None
        ),
        prompt_cache_retention=(
            explicit_retention
            if effective_capability.supports_prompt_cache_retention and explicit_retention
            else None
        ),
        anthropic_cache_control_enabled=anthropic_enabled,
        anthropic_cache_control_ttl=ttl,
        gemini_explicit_cached_content_enabled=gemini_explicit_enabled,
        gemini_cached_content_ttl=gemini_ttl,
        # Implicit strategies cache eligible prefixes without a request marker.
        # Diagnostic-only explicit strategies remain merely available.
        implicit_cache_enabled=strategy
        in {CACHE_STRATEGY_IMPLICIT_PROVIDER, CACHE_STRATEGY_GEMINI_IMPLICIT},
        mode=mode,
        strategy=strategy,
        capability_source=effective_capability.source,
        allowed_fields=effective_capability.emitted_fields,
        emitted_fields=emitted_fields,
        trusted_usage_fields=effective_capability.trusted_usage_fields,
        usage_schema=effective_capability.usage_schema,
        min_cacheable_tokens=effective_capability.min_cacheable_tokens,
        request_field_values=request_field_values,
        warnings=warnings,
    )


def prompt_cache_policy_diagnostic_rows(
    policy: ResolvedPromptCachePolicy,
) -> tuple[tuple[str, str], ...]:
    allowed_fields = ", ".join(policy.allowed_fields) if policy.allowed_fields else "none"
    emitted_fields = ", ".join(policy.emitted_fields) if policy.emitted_fields else "none"
    usage_fields = ", ".join(policy.trusted_usage_fields) if policy.trusted_usage_fields else "none"
    warnings = "; ".join(policy.warnings) if policy.warnings else "none"
    return (
        ("cache_status", policy.status),
        ("cache_strategy", policy.strategy),
        ("cache_capability_source", policy.capability_source),
        ("cache_allowed_fields", allowed_fields),
        ("cache_emitted_fields", emitted_fields),
        ("cache_usage_fields", usage_fields),
        ("cache_usage_schema", policy.usage_schema),
        ("cache_warnings", warnings),
    )


def merge_cache_policy_metadata(
    base: Mapping[str, Any] | None,
    active: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    merged: dict[str, Any] = {}
    if isinstance(base, Mapping):
        merged.update(dict(base))
    if isinstance(active, Mapping):
        merged.update(dict(active))
        if active.get("enabled") is True:
            merged["status"] = "enabled"
            merged["enabled"] = True
    return merged or None


def _policy_emitted_fields(
    *,
    request_field_values: tuple[tuple[str, str], ...],
) -> tuple[str, ...]:
    return tuple(field for field, _value in request_field_values)


def _policy_request_field_values(
    *,
    emitted_fields: tuple[str, ...],
    prompt_cache_key: str | None,
    prompt_cache_retention: str | None,
    affinity_key: str | None,
    anthropic_cache_control_enabled: bool,
    gemini_explicit_cached_content_enabled: bool,
) -> tuple[tuple[str, str], ...]:
    values: list[tuple[str, str]] = []
    if prompt_cache_key and PROMPT_CACHE_KEY_FIELD in emitted_fields:
        values.append((PROMPT_CACHE_KEY_FIELD, prompt_cache_key))
    if prompt_cache_retention and PROMPT_CACHE_RETENTION_FIELD in emitted_fields:
        values.append((PROMPT_CACHE_RETENTION_FIELD, prompt_cache_retention))
    if affinity_key:
        for field in (
            OPENROUTER_SESSION_ID_FIELD,
            OPENROUTER_SESSION_ID_HEADER_FIELD,
            XAI_CONVERSATION_ID_HEADER_FIELD,
        ):
            if field in emitted_fields:
                values.append((field, affinity_key))
    if anthropic_cache_control_enabled and CACHE_CONTROL_FIELD in emitted_fields:
        values.append((CACHE_CONTROL_FIELD, "ephemeral"))
    if gemini_explicit_cached_content_enabled and CACHED_CONTENT_FIELD in emitted_fields:
        values.append((CACHED_CONTENT_FIELD, "enabled"))
    return tuple(values)


def _clean_optional(value: str | None) -> str | None:
    cleaned = str(value or "").strip()
    return cleaned or None


def _normalize_workspace_root(root: str | os.PathLike[str]) -> str:
    try:
        return os.fspath(Path(root).expanduser().resolve(strict=False))
    except (OSError, RuntimeError, ValueError):
        return os.fspath(root)


def _auto_prompt_cache_key(
    *,
    provider_key: str,
    protocol: str,
    model: str,
    namespace: str | None,
) -> str | None:
    normalized_namespace = str(namespace or "").strip()
    if not normalized_namespace:
        return None
    provider_label = _safe_cache_label(provider_key or "provider")
    payload = {
        "version": 1,
        "provider": str(provider_key or "").strip().lower(),
        "protocol": str(protocol or "").strip().lower(),
        "model": str(model or "").strip().lower(),
        "namespace": normalized_namespace,
    }
    digest = hashlib.blake2s(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8"),
        digest_size=10,
    ).hexdigest()
    return f"alysis:{provider_label}:{digest}"


def _safe_cache_label(value: str) -> str:
    normalized = _CACHE_KEY_LABEL_RE.sub("-", str(value or "").strip().lower()).strip("-")
    return normalized[:32] or "provider"


def _google_duration_from_retention(value: str | None) -> tuple[str, bool]:
    cleaned = _clean_optional(value)
    if cleaned is None:
        return "3600s", False
    normalized = cleaned.lower()
    if normalized.endswith("s") and normalized[:-1].isdigit():
        return normalized, False
    if normalized.endswith("m") and normalized[:-1].isdigit():
        return f"{int(normalized[:-1]) * 60}s", False
    if normalized.endswith("h") and normalized[:-1].isdigit():
        return f"{int(normalized[:-1]) * 3600}s", False
    # Retention values from other providers (e.g. "in-memory") are not valid
    # Google durations and would 400 every cachedContents create.
    return "3600s", True
