from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx

from .llm.protocols import (
    ANTHROPIC_MESSAGES_PROTOCOL,
    GEMINI_GENERATE_CONTENT_PROTOCOL,
    GEMINI_INTERACTIONS_PROTOCOL,
    OPENAI_COMPAT_PROTOCOL,
)
from .profiles import _REASONING_EFFORT_RE, ProfileSpec
from .reasoning_contracts import (
    ALWAYS_ON,
    OFF_EXPLICIT,
    OFF_IMPOSSIBLE,
    OPTIONAL,
    WIRE_REASONING_EFFORT,
    WIRE_REASONING_OBJECT,
    WIRE_THINKING_ADAPTIVE,
    WIRE_THINKING_LEVEL,
    ReasoningContract,
    model_reasoning_contract,
)

ProviderModelCatalogStrategy = Literal["gemini", "anthropic", "openai"]
ModelChatCompatibility = Literal["chat", "unknown"]

_DEFAULT_TIMEOUT_S = 6.0
_MAX_RESPONSE_BYTES = 5 * 1024 * 1024
_MAX_PAGES = 25
_MAX_MODELS = 5_000
_MAX_MODEL_ID_LENGTH = 512
_STREAM_CHUNK_SIZE = 64 * 1024
_USER_AGENT = "alysis-code"
_ANTHROPIC_VERSION = "2023-06-01"

_NON_CHAT_MODES = frozenset(
    {
        "audio_speech",
        "audio_transcription",
        "completion",
        "completions",
        "embedding",
        "embeddings",
        "image",
        "image_generation",
        "moderation",
        "rerank",
        "reranking",
        "speech",
        "transcription",
        "text_completion",
        "text_completions",
        "video",
        "video_generation",
    }
)
_CHAT_CAPABILITY_NAMES = frozenset(
    {
        "chat",
        "chat_completion",
        "chat_completions",
        "completion_chat",
        "generate_content",
        "generatecontent",
        "messages",
        "responses",
        "text_generation",
    }
)
_ANSI_ESCAPE_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|[@-_])")
_TERMINAL_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


@dataclass(frozen=True, slots=True)
class ProviderModelOption:
    """A model advertised by the active API-key provider."""

    id: str
    label: str
    description: str = ""
    chat_compatibility: ModelChatCompatibility = "unknown"
    reasoning_contract: ReasoningContract | None = None


def openrouter_reasoning_contract(raw: Any, *, model: str = "") -> ReasoningContract | None:
    """Decode the gateway's documented per-model reasoning metadata.

    https://openrouter.ai/docs/guides/best-practices/reasoning-tokens
    Missing, null and empty supported_efforts have different meanings.
    """
    if not isinstance(raw, dict):
        raw = {}
    if "supported_efforts" not in raw:
        contract = model_reasoning_contract(model)
        if (
            contract.wire
            not in {
                WIRE_REASONING_EFFORT,
                WIRE_REASONING_OBJECT,
                WIRE_THINKING_ADAPTIVE,
                WIRE_THINKING_LEVEL,
            }
            or not contract.values
        ):
            return None
        # Fill only absent fields; partial gateway restrictions remain authoritative.
        raw = {
            "supported_efforts": list(contract.values),
            "mandatory": not contract.toggleable,
            "default_effort": contract.default,
            **raw,
        }
    values = raw["supported_efforts"]
    if values is None:
        values = ["none", "minimal", "low", "medium", "high", "xhigh", "max"]
    if not isinstance(values, list) or any(
        not isinstance(value, str) or _REASONING_EFFORT_RE.fullmatch(value) is None
        for value in values
    ):
        return None
    mandatory = raw.get("mandatory")
    if not isinstance(mandatory, bool):
        # Unknown off semantics must not introduce a disable choice.
        mandatory = True
    efforts = tuple(dict.fromkeys(value for value in values if value != "none" or not mandatory))
    default = raw.get("default_effort")
    if default not in efforts:
        default = ""
    return ReasoningContract(
        mode=ALWAYS_ON if mandatory else OPTIONAL,
        wire=WIRE_REASONING_OBJECT,
        values=efforts,
        default=default,
        off=OFF_IMPOSSIBLE if mandatory else OFF_EXPLICIT,
    )


class ProviderModelCatalogError(RuntimeError):
    """A provider catalog could not be loaded safely.

    Messages intentionally contain neither response bodies nor request exception
    details because both can echo credentials supplied by a user-controlled endpoint.
    """


def provider_model_catalog_strategy(profile: ProfileSpec) -> ProviderModelCatalogStrategy:
    """Return the list-model wire format used by ``profile``.

    First-party compatibility profiles still use their provider-native catalog:
    Gemini's native endpoint exposes ``generateContent`` support and Anthropic's
    native endpoint supplies account-aware model metadata. All other profiles use
    the broadly implemented OpenAI-shaped ``/models`` surface.
    """

    protocol = str(profile.protocol or "").strip().lower()
    if protocol in {GEMINI_GENERATE_CONTENT_PROTOCOL, GEMINI_INTERACTIONS_PROTOCOL}:
        return "gemini"
    if protocol == ANTHROPIC_MESSAGES_PROTOCOL:
        return "anthropic"
    # Compatibility profile names are user-controlled. Dispatching a custom
    # gateway called "gemini-proxy" or "claude" to a first-party wire format
    # would send the wrong auth headers and parse the wrong schema. Only the
    # official first-party hosts opt a compatibility profile into native listing.
    if protocol == OPENAI_COMPAT_PROTOCOL:
        hostname = _url_hostname(profile.base_url)
        if hostname == "generativelanguage.googleapis.com":
            return "gemini"
        if hostname == "api.anthropic.com":
            return "anthropic"
    return "openai"


def discover_provider_models(
    *,
    profile: ProfileSpec,
    api_key: str | None,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    transport: httpx.BaseTransport | None = None,
) -> tuple[ProviderModelOption, ...]:
    """Fetch the active provider's router-compatible model catalog.

    Discovery is deliberately bounded and offline-safe at the API boundary: it
    never follows redirects, caps pages, model count, and response size, and
    translates all provider/transport failures into a sanitized
    :class:`ProviderModelCatalogError`. Callers can then retain their curated
    preset rows as a fallback without exposing secrets in the TUI.
    """

    base_url = str(profile.base_url or "").strip().rstrip("/")
    if not base_url:
        raise ProviderModelCatalogError("Provider model catalog URL is not configured.")
    if not _valid_timeout(timeout_s):
        raise ProviderModelCatalogError("Provider model catalog timeout must be positive.")

    strategy = provider_model_catalog_strategy(profile)
    normalized_key = str(api_key or "").strip()
    deadline = time.monotonic() + float(timeout_s)
    if strategy in {"gemini", "anthropic"} and not normalized_key:
        raise ProviderModelCatalogError(
            "An API key is required to load this provider's model catalog."
        )

    try:
        with httpx.Client(
            timeout=float(timeout_s),
            transport=transport,
            follow_redirects=False,
        ) as client:
            if strategy == "gemini":
                return _discover_gemini_models(
                    client=client,
                    profile=profile,
                    api_key=normalized_key,
                    deadline=deadline,
                )
            if strategy == "anthropic":
                return _discover_anthropic_models(
                    client=client,
                    profile=profile,
                    api_key=normalized_key,
                    deadline=deadline,
                )
            return _discover_openai_models(
                client=client,
                profile=profile,
                api_key=normalized_key,
                deadline=deadline,
            )
    except ProviderModelCatalogError:
        raise
    except Exception as exc:  # noqa: BLE001 - sanitize every provider boundary failure
        raise ProviderModelCatalogError(
            f"Provider model catalog is unavailable ({type(exc).__name__})."
        ) from None


def _discover_gemini_models(
    *,
    client: httpx.Client,
    profile: ProfileSpec,
    api_key: str,
    deadline: float,
) -> tuple[ProviderModelOption, ...]:
    url = f"{_gemini_catalog_base_url(profile.base_url)}/models"
    headers = _merged_headers(
        {
            "Accept": "application/json",
            "User-Agent": _USER_AGENT,
            "x-goog-api-key": api_key,
        },
        profile.extra_headers,
    )
    options: list[ProviderModelOption] = []
    seen_ids: set[str] = set()
    seen_tokens: set[str] = set()
    page_token = ""

    for _page in range(_MAX_PAGES):
        params: dict[str, str | int] = {"pageSize": 1000}
        if page_token:
            params["pageToken"] = page_token
        payload = _get_json(
            client,
            url=url,
            headers=headers,
            params=params,
            deadline=deadline,
        )
        if not isinstance(payload, dict):
            raise ProviderModelCatalogError(
                "Provider model catalog returned an invalid Gemini response."
            )
        raw_models = payload.get("models", [])
        if not isinstance(raw_models, list):
            raise ProviderModelCatalogError(
                "Provider model catalog returned an invalid Gemini model list."
            )
        for raw in raw_models:
            if not isinstance(raw, dict) or not _gemini_supports_generate_content(raw):
                continue
            model_id = _gemini_model_id(raw)
            if not model_id:
                continue
            _append_option(
                options,
                seen_ids,
                ProviderModelOption(
                    id=model_id,
                    label=_clean_text(raw.get("displayName")) or model_id,
                    description=_clean_text(raw.get("description")),
                    chat_compatibility="chat",
                ),
            )
            _enforce_model_bound(options)

        next_token = str(payload.get("nextPageToken") or "").strip()
        if not next_token:
            return tuple(options)
        if next_token == page_token or next_token in seen_tokens:
            raise ProviderModelCatalogError(
                "Provider model catalog returned a repeated Gemini page token."
            )
        seen_tokens.add(next_token)
        page_token = next_token

    raise ProviderModelCatalogError("Provider model catalog exceeded the Gemini page limit.")


def _discover_anthropic_models(
    *,
    client: httpx.Client,
    profile: ProfileSpec,
    api_key: str,
    deadline: float,
) -> tuple[ProviderModelOption, ...]:
    url = f"{str(profile.base_url).strip().rstrip('/')}/models"
    headers = _merged_headers(
        {
            "Accept": "application/json",
            "User-Agent": _USER_AGENT,
            "x-api-key": api_key,
            "anthropic-version": _ANTHROPIC_VERSION,
        },
        profile.extra_headers,
    )
    options: list[ProviderModelOption] = []
    seen_ids: set[str] = set()
    seen_cursors: set[str] = set()
    after_id = ""

    for _page in range(_MAX_PAGES):
        params: dict[str, str | int] = {"limit": 1000}
        if after_id:
            params["after_id"] = after_id
        payload = _get_json(
            client,
            url=url,
            headers=headers,
            params=params,
            deadline=deadline,
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise ProviderModelCatalogError(
                "Provider model catalog returned an invalid Anthropic response."
            )
        for raw in payload["data"]:
            if not isinstance(raw, dict):
                continue
            model_id = _clean_model_id(raw.get("id"))
            if not model_id:
                continue
            _append_option(
                options,
                seen_ids,
                ProviderModelOption(
                    id=model_id,
                    label=_clean_text(raw.get("display_name")) or model_id,
                    description=_anthropic_description(raw),
                    chat_compatibility="chat",
                ),
            )
            _enforce_model_bound(options)

        if not bool(payload.get("has_more")):
            return tuple(options)
        next_cursor = str(payload.get("last_id") or "").strip()
        if not next_cursor:
            raise ProviderModelCatalogError(
                "Provider model catalog omitted the next Anthropic cursor."
            )
        if next_cursor == after_id or next_cursor in seen_cursors:
            raise ProviderModelCatalogError(
                "Provider model catalog returned a repeated Anthropic cursor."
            )
        seen_cursors.add(next_cursor)
        after_id = next_cursor

    raise ProviderModelCatalogError("Provider model catalog exceeded the Anthropic page limit.")


def _discover_openai_models(
    *,
    client: httpx.Client,
    profile: ProfileSpec,
    api_key: str,
    deadline: float,
) -> tuple[ProviderModelOption, ...]:
    url = f"{str(profile.base_url).strip().rstrip('/')}/models"
    base_headers = {
        "Accept": "application/json",
        "User-Agent": _USER_AGENT,
    }
    if api_key:
        base_headers["Authorization"] = f"Bearer {api_key}"
    headers = _merged_headers(base_headers, profile.extra_headers)
    payload = _get_json(client, url=url, headers=headers, deadline=deadline)
    raw_models: Any
    if isinstance(payload, list):
        raw_models = payload
    elif isinstance(payload, dict):
        raw_models = payload.get("data")
        if raw_models is None:
            raw_models = payload.get("models")
    else:
        raw_models = None
    if not isinstance(raw_models, list):
        raise ProviderModelCatalogError(
            "Provider model catalog returned an invalid OpenAI-style response."
        )

    from .profile_presets import find_preset_for_profile

    preset = find_preset_for_profile(profile)
    is_openrouter = preset is not None and preset.provider_key == "openrouter"
    options: list[ProviderModelOption] = []
    seen_ids: set[str] = set()
    for raw in raw_models:
        if isinstance(raw, str):
            model_id = _clean_model_id(raw)
            if model_id:
                _append_option(
                    options,
                    seen_ids,
                    ProviderModelOption(id=model_id, label=model_id),
                )
                _enforce_model_bound(options)
            continue
        if not isinstance(raw, dict):
            continue
        model_id = _clean_model_id(raw.get("id") or raw.get("model") or raw.get("name"))
        if not model_id or _generic_model_is_non_chat(raw):
            continue
        _append_option(
            options,
            seen_ids,
            ProviderModelOption(
                id=model_id,
                label=_clean_text(raw.get("display_name") or raw.get("displayName")) or model_id,
                description=_clean_text(raw.get("description")),
                chat_compatibility=("chat" if _has_explicit_chat_capability(raw) else "unknown"),
                reasoning_contract=(
                    openrouter_reasoning_contract(raw.get("reasoning"), model=model_id)
                    if is_openrouter
                    else None
                ),
            ),
        )
        _enforce_model_bound(options)
    return tuple(options)


def _get_json(
    client: httpx.Client,
    *,
    url: str,
    headers: dict[str, str],
    params: dict[str, str | int] | None = None,
    deadline: float,
) -> Any:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ProviderModelCatalogError("Provider model catalog request timed out.")
    try:
        with client.stream(
            "GET",
            url,
            headers=headers,
            params=params,
            timeout=remaining,
        ) as response:
            if not 200 <= response.status_code < 300:
                raise ProviderModelCatalogError(
                    f"Provider model catalog request failed (HTTP {response.status_code})."
                )
            content_length = _positive_int(response.headers.get("content-length"))
            if content_length is not None and content_length > _MAX_RESPONSE_BYTES:
                raise ProviderModelCatalogError("Provider model catalog response was too large.")
            body = bytearray()
            for chunk in response.iter_bytes(chunk_size=_STREAM_CHUNK_SIZE):
                if time.monotonic() >= deadline:
                    raise ProviderModelCatalogError("Provider model catalog request timed out.")
                if len(body) + len(chunk) > _MAX_RESPONSE_BYTES:
                    raise ProviderModelCatalogError(
                        "Provider model catalog response was too large."
                    )
                body.extend(chunk)
    except ProviderModelCatalogError:
        raise
    except Exception as exc:  # noqa: BLE001 - never leak request details from provider errors
        raise ProviderModelCatalogError(
            f"Provider model catalog request failed ({type(exc).__name__})."
        ) from None
    try:
        return json.loads(bytes(body))
    except Exception:  # noqa: BLE001 - body is untrusted and must not appear in the error
        raise ProviderModelCatalogError("Provider model catalog returned malformed JSON.") from None


def _gemini_catalog_base_url(base_url: str) -> str:
    normalized = str(base_url or "").strip().rstrip("/")
    for suffix in ("/openai/v1", "/openai"):
        if normalized.casefold().endswith(suffix):
            return normalized[: -len(suffix)].rstrip("/")
    return normalized


def _gemini_supports_generate_content(raw: dict[str, Any]) -> bool:
    methods = raw.get("supportedGenerationMethods")
    return isinstance(methods, list) and any(
        str(method or "").strip().casefold() == "generatecontent" for method in methods
    )


def _gemini_model_id(raw: dict[str, Any]) -> str:
    model_id = _clean_model_id(raw.get("baseModelId"))
    if model_id:
        return model_id.removeprefix("models/")
    return _clean_model_id(raw.get("name")).removeprefix("models/")


def _anthropic_description(raw: dict[str, Any]) -> str:
    description = _clean_text(raw.get("description"))
    if description:
        return description
    details: list[str] = []
    max_input = _positive_int(raw.get("max_input_tokens"))
    max_output = _positive_int(raw.get("max_tokens"))
    if max_input is not None:
        details.append(f"{max_input:,} input tokens")
    if max_output is not None:
        details.append(f"{max_output:,} max output tokens")
    return " · ".join(details)


def _generic_model_is_non_chat(raw: dict[str, Any]) -> bool:
    explicit_chat = _has_explicit_chat_capability(raw)
    if _has_explicit_non_chat_capability(raw, explicit_chat=explicit_chat):
        return True
    return False


def _has_explicit_chat_capability(raw: dict[str, Any]) -> bool:
    mode = _normalized_capability_name(raw.get("mode"))
    if mode in _CHAT_CAPABILITY_NAMES:
        return True

    endpoints = _string_list(raw.get("supported_endpoints"))
    if endpoints and any(_endpoint_is_chat(endpoint) for endpoint in endpoints):
        return True

    capabilities = raw.get("capabilities")
    if isinstance(capabilities, dict):
        for name, enabled in capabilities.items():
            if _normalized_capability_name(name) in _CHAT_CAPABILITY_NAMES and _is_supported(
                enabled
            ):
                return True
    elif isinstance(capabilities, list):
        if any(
            _normalized_capability_name(value) in _CHAT_CAPABILITY_NAMES for value in capabilities
        ):
            return True
    return False


def _has_explicit_non_chat_capability(
    raw: dict[str, Any],
    *,
    explicit_chat: bool,
) -> bool:
    mode = _normalized_capability_name(raw.get("mode"))
    if mode in _NON_CHAT_MODES:
        return True

    object_type = _normalized_capability_name(raw.get("type"))
    if object_type in _NON_CHAT_MODES:
        return True

    output_modalities = _output_modalities(raw)
    if output_modalities and "text" not in output_modalities:
        return True

    endpoints = _string_list(raw.get("supported_endpoints"))
    if endpoints and not explicit_chat and all(_endpoint_is_clearly_non_chat(v) for v in endpoints):
        return True

    capabilities = raw.get("capabilities")
    if isinstance(capabilities, dict) and not explicit_chat:
        if any(
            _normalized_capability_name(name) in _NON_CHAT_MODES and _is_supported(enabled)
            for name, enabled in capabilities.items()
        ):
            return True
        recognized = [
            _is_supported(enabled)
            for name, enabled in capabilities.items()
            if _normalized_capability_name(name) in _CHAT_CAPABILITY_NAMES
        ]
        if recognized and not any(recognized):
            return True
    elif isinstance(capabilities, list) and not explicit_chat:
        if any(_normalized_capability_name(value) in _NON_CHAT_MODES for value in capabilities):
            return True
    return False


def _output_modalities(raw: dict[str, Any]) -> set[str]:
    candidates = [raw.get("output_modalities"), raw.get("supported_output_modalities")]
    architecture = raw.get("architecture")
    if isinstance(architecture, dict):
        candidates.append(architecture.get("output_modalities"))
    modalities: set[str] = set()
    for candidate in candidates:
        modalities.update(value.casefold() for value in _string_list(candidate))
    return modalities


def _endpoint_is_chat(endpoint: str) -> bool:
    normalized = endpoint.strip().casefold().replace("_", "-")
    return any(
        marker in normalized
        for marker in (
            "chat/completions",
            "generatecontent",
            "/messages",
            "/responses",
        )
    )


def _endpoint_is_clearly_non_chat(endpoint: str) -> bool:
    normalized = endpoint.strip().casefold().replace("_", "-")
    if normalized.endswith("/completions") and "chat/completions" not in normalized:
        return True
    return any(
        marker in normalized
        for marker in (
            "/audio/",
            "/embeddings",
            "/images",
            "/moderations",
            "/rerank",
            "/videos",
        )
    )


def _merged_headers(
    defaults: dict[str, str],
    extra_headers: dict[str, str] | None,
) -> dict[str, str]:
    headers = {
        str(key).strip(): str(value).strip()
        for key, value in defaults.items()
        if str(key).strip() and str(value).strip()
    }
    for raw_key, raw_value in (extra_headers or {}).items():
        key = str(raw_key).strip()
        value = str(raw_value).strip()
        if not key or not value:
            continue
        folded = key.casefold()
        headers = {
            existing: item for existing, item in headers.items() if existing.casefold() != folded
        }
        headers[key] = value
    return headers


def _append_option(
    options: list[ProviderModelOption],
    seen_ids: set[str],
    option: ProviderModelOption,
) -> None:
    if not option.id or option.id in seen_ids:
        return
    seen_ids.add(option.id)
    options.append(option)


def _enforce_model_bound(options: list[ProviderModelOption]) -> None:
    if len(options) > _MAX_MODELS:
        raise ProviderModelCatalogError("Provider model catalog exceeded the model limit.")


def _clean_model_id(value: Any) -> str:
    text = _sanitize_terminal_text(value)
    return text if len(text) <= _MAX_MODEL_ID_LENGTH else ""


def _clean_text(value: Any, *, limit: int = 320) -> str:
    text = _sanitize_terminal_text(value)
    if len(text) > limit:
        return text[: max(0, limit - 3)].rstrip() + "..."
    return text


def _sanitize_terminal_text(value: Any) -> str:
    text = str(value or "")
    text = _ANSI_ESCAPE_RE.sub("", text)
    text = _TERMINAL_CONTROL_RE.sub("", text)
    return " ".join(text.split())


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _string_list(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(text for item in value if (text := str(item or "").strip()))


def _normalized_capability_name(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().casefold()).strip("_")


def _is_supported(value: Any) -> bool:
    if isinstance(value, dict):
        return bool(value.get("supported"))
    return bool(value)


def _valid_timeout(value: Any) -> bool:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(parsed) and parsed > 0


def _url_hostname(value: str) -> str:
    try:
        return str(urlsplit(str(value or "").strip()).hostname or "").rstrip(".").casefold()
    except ValueError:
        return ""


__all__ = [
    "ModelChatCompatibility",
    "ProviderModelCatalogError",
    "ProviderModelCatalogStrategy",
    "ProviderModelOption",
    "discover_provider_models",
    "provider_model_catalog_strategy",
]
