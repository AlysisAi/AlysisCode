from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import html
import json
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from ..cancellation import CooperativeCancellationError
from ..chatgpt_codex_static_provider import (
    load_chatgpt_codex_static_models,
    resolve_chatgpt_codex_static_model,
)
from ..host_browser import open_url
from ..llm.cache_capabilities import (
    CACHE_STRATEGY_IMPLICIT_PROVIDER,
    CACHE_USAGE_SCHEMA_OPENAI,
    CacheCapabilitySpec,
)
from ..llm.cancellable_http import cancellation_aware_httpx_client
from .base import (
    ProviderAccountStatus,
    ProviderAuthError,
    ProviderCredentialScopeMismatchError,
    ProviderLoginRequiredError,
    ProviderModel,
    ProviderReasoningEffort,
    ProviderRequestAuthorization,
    reasoning_effort_label,
)
from .store import (
    ProviderTokenRecord,
    delete_provider_token,
    load_provider_token,
    remember_subscription_registration,
    save_provider_token,
    save_refreshed_provider_token,
    subscription_registration,
)

SESSION_EXPIRED_DETAIL = "session expired"
"""Stable ``detail`` value meaning the stored session must be re-authorized."""

_ISSUER = "https://auth.openai.com"
_CODEX_BASE_URL = "https://api.openai.com/v1"
# The ChatGPT Codex backend this adapter used before the Alysis app registration.
# Profiles saved with it are moved to _CODEX_BASE_URL when the config loads.
_LEGACY_CODEX_BASE_URLS = ("https://chatgpt.com/backend-api/codex",)
_RESPONSES_URL = f"{_CODEX_BASE_URL}/responses"
_MODELS_URL = f"{_CODEX_BASE_URL}/models"
_DEFAULT_CLIENT_ID = "dynamic_agent_client"
_RESOURCE = "https://api.openai.com/v1"
_PLAN_SCOPE = "chatgpt.tokens.use.direct"
_TOKEN_URL = f"{_ISSUER}/api/accounts/oauth/token"
_CALLBACK_PORTS = (1455, 1457, 0)
_BROWSER_TIMEOUT_SECONDS = 300.0
_REFRESH_SKEW_SECONDS = 300.0
_DEFAULT_TOKEN_LIFETIME_SECONDS = 3600.0
_ALLOWED_REQUESTS = frozenset({_RESPONSES_URL, _MODELS_URL})
_REFRESH_LOCKS: dict[str, threading.Lock] = {}
_REFRESH_LOCKS_GUARD = threading.Lock()
_LOGIN_LOCK = threading.Lock()
_CANCELLATION_POLL_SECONDS = 0.05


def _throw_if_cancelled(cancellation_token: Any | None) -> None:
    if cancellation_token is None:
        return
    throw_if_cancelled = getattr(cancellation_token, "throw_if_cancelled", None)
    if callable(throw_if_cancelled):
        throw_if_cancelled("cancelled_by_user")
        return
    if bool(getattr(cancellation_token, "is_cancelled", False)):
        raise CooperativeCancellationError("cancelled_by_user")


class OpenAICodexSubscriptionAuth:
    """Sign in with ChatGPT plan usage for the native Alysis Code agent.

    The compatibility provider ID/profile is retained. Only public OpenAI
    model/Responses endpoints receive a validated subscription credential.
    """

    provider_id = "openai-codex"
    display_name = "ChatGPT subscription"
    description = "Use eligible models from your ChatGPT plan with Alysis Code's native agent."
    profile_name = "chatgpt-codex"
    auth_hint = "Continue with ChatGPT; credentials use Alysis Code's encrypted vault."
    base_url = _CODEX_BASE_URL
    legacy_base_urls = _LEGACY_CODEX_BASE_URLS
    protocol = "openai_responses"
    supports_previous_response_id = False
    supports_input_token_count = False
    supports_temperature = False
    requires_streaming = True
    cache_capability = CacheCapabilitySpec(
        strategy=CACHE_STRATEGY_IMPLICIT_PROVIDER,
        enabled=True,
        reports_cache_read_tokens=True,
        reports_cache_write_tokens=False,
        usage_schema=CACHE_USAGE_SCHEMA_OPENAI,
        emits_request_fields=False,
        notes=(
            "Subscription responses report cached input without explicit cache request fields.",
        ),
        source="auth_adapter",
    )

    def __init__(self, *, transport: httpx.BaseTransport | None = None) -> None:
        self._transport = transport
        self._models_cache: tuple[ProviderModel, ...] | None = None
        self._models_cache_scope = ""

    def account_status(self) -> ProviderAccountStatus:
        """Report account state without ever raising.

        A supervising app polls this to decide what to show the user, so every
        failure has to come back as data. Reading the credential store can fail
        on its own — a spawned process may not reach the OS keyring holding the
        store's master key — so the load is guarded too, not just the refresh.
        """

        try:
            record = load_provider_token(self.provider_id)
        except ProviderAuthError as exc:
            return ProviderAccountStatus(
                connected=False,
                verified=False,
                detail=str(exc),
            )
        except Exception as exc:  # noqa: BLE001 - a status probe must not crash its caller
            return ProviderAccountStatus(
                connected=False,
                verified=False,
                detail=f"Could not read stored ChatGPT credentials ({exc.__class__.__name__}).",
            )
        if record is None:
            return ProviderAccountStatus(
                connected=False,
                detail="No ChatGPT subscription account is connected.",
            )
        if not record.client_id or not record.subject:
            return ProviderAccountStatus(
                connected=False, detail="Reconnect ChatGPT to authorize Alysis Code for plan usage."
            )
        if _PLAN_SCOPE not in record.scopes:
            return ProviderAccountStatus(
                connected=False,
                verified=True,
                account_label=record.account_label,
                detail="ChatGPT plan usage is not enabled. Continue with ChatGPT to review permissions, or choose an API provider.",
            )
        try:
            record = self._valid_record(record)
        except ProviderLoginRequiredError:
            # A stable marker, not the thrown message: this is the one state a
            # supervising app must be able to branch on to prompt a re-login.
            return ProviderAccountStatus(
                connected=False,
                verified=True,
                account_label=record.account_label,
                detail=SESSION_EXPIRED_DETAIL,
            )
        except ProviderAuthError as exc:
            return ProviderAccountStatus(
                connected=False,
                verified=False,
                account_label=record.account_label,
                detail=str(exc),
            )
        except Exception as exc:  # noqa: BLE001 - a status probe must not crash its caller
            return ProviderAccountStatus(
                connected=False,
                verified=False,
                account_label=record.account_label,
                detail=f"Could not verify ChatGPT credentials ({exc.__class__.__name__}).",
            )
        return ProviderAccountStatus(
            connected=True,
            account_label=record.account_label,
            detail="Connected with ChatGPT.",
            credential_scope=_credential_scope(record),
        )

    def route_credential_scope(self) -> str:
        """Return a non-secret stable account scope for continuation isolation."""

        record = load_provider_token(self.provider_id)
        if record is None:
            return ""
        return _credential_scope(record)

    def login(
        self,
        method: str = "browser",
        *,
        output_write: Callable[[str], None] | None = None,
    ) -> ProviderAccountStatus:
        normalized = str(method or "browser").strip().lower()
        write = output_write or print
        if normalized == "device-code":
            raise ProviderAuthError(
                "Use Continue with ChatGPT. Device-code registration is not supported for this app."
            )
        if normalized not in {"browser", "browser-new-account"}:
            raise ProviderAuthError(f"Unsupported ChatGPT sign-in method: {method!r}.")
        if not _LOGIN_LOCK.acquire(blocking=False):
            raise ProviderAuthError("Another ChatGPT sign-in is already in progress.")
        try:
            record = self._browser_login(write, new_account=normalized == "browser-new-account")
            if _PLAN_SCOPE not in record.scopes:
                previous = load_provider_token(self.provider_id)
                if previous is not None and _PLAN_SCOPE in previous.scopes:
                    remember_subscription_registration(self.provider_id, record)
                    raise ProviderAuthError(
                        "ChatGPT plan usage was not granted. Your previous account is still selected; try Continue with ChatGPT again."
                    )
            save_provider_token(self.provider_id, record)
            self._models_cache = None
            self._models_cache_scope = ""
        finally:
            _LOGIN_LOCK.release()
        return self.account_status()

    def logout(self) -> ProviderAccountStatus:
        record = load_provider_token(self.provider_id)
        revoke_error: str | None = None
        if record is not None:
            try:
                with self._client(timeout=10.0) as client:
                    response = client.post(
                        self._revocation_endpoint(),
                        data={
                            "token": record.refresh_token,
                            "token_type_hint": "refresh_token",
                            "client_id": record.client_id or _DEFAULT_CLIENT_ID,
                        },
                    )
                if response.status_code >= 400:
                    revoke_error = f"remote revocation returned status {response.status_code}"
            except Exception as exc:  # noqa: BLE001
                # Local credential removal is still deterministic. Remote
                # revocation is best effort because auth services can be offline.
                revoke_error = f"remote revocation failed: {type(exc).__name__}"
        delete_provider_token(self.provider_id)
        self._models_cache = None
        self._models_cache_scope = ""
        return ProviderAccountStatus(
            connected=False,
            verified=revoke_error is None,
            detail=(
                "Disconnected locally."
                if revoke_error is None
                else f"Disconnected locally; {revoke_error}."
            ),
        )

    def authorization_headers(
        self,
        url: str,
        *,
        force_refresh: bool = False,
        session_id: str | None = None,
        expected_credential_scope: str | None = None,
        cancellation_token: Any | None = None,
    ) -> Mapping[str, str]:
        return self.authorize_request(
            url,
            force_refresh=force_refresh,
            session_id=session_id,
            expected_credential_scope=expected_credential_scope,
            cancellation_token=cancellation_token,
        ).headers

    def authorize_request(
        self,
        url: str,
        *,
        force_refresh: bool = False,
        session_id: str | None = None,
        expected_credential_scope: str | None = None,
        cancellation_token: Any | None = None,
    ) -> ProviderRequestAuthorization:
        """Bind proof and headers to one validated credential-store snapshot."""

        normalized_url = _canonical_request_url(url)
        if normalized_url not in _ALLOWED_REQUESTS:
            raise ProviderAuthError(
                "Refusing to attach ChatGPT credentials to a non-OpenAI subscription destination."
            )
        record = load_provider_token(self.provider_id)
        if record is None:
            raise ProviderLoginRequiredError(
                "ChatGPT is not connected. Run `alysis auth login openai-codex`."
            )
        record = self._valid_record(
            record,
            force_refresh=force_refresh,
            cancellation_token=cancellation_token,
        )
        credential_scope = _credential_scope(record)
        expected_scope = str(expected_credential_scope or "").strip()
        if expected_scope and not hmac.compare_digest(credential_scope, expected_scope):
            raise ProviderCredentialScopeMismatchError(
                "The selected ChatGPT account changed before the provider request was sent."
            )
        headers: dict[str, str] = {
            "Authorization": f"Bearer {record.access_token}",
            "User-Agent": "Alysis-Code",
        }
        return ProviderRequestAuthorization(
            credential_scope=credential_scope,
            headers=headers,
        )

    def adapt_responses_payload(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        adapted = copy.deepcopy(dict(payload))
        reasoning = adapted.get("reasoning")
        if isinstance(reasoning, dict) and str(reasoning.get("effort") or "").casefold() == "ultra":
            raise ProviderAuthError(
                "Ultra is a Codex orchestration mode, not a subscription API reasoning effort. "
                "Choose Max or another supported effort in /config > Default Model."
            )
        input_items = adapted.get("input")
        if isinstance(input_items, list):
            instructions: list[str] = []
            retained: list[Any] = []
            in_instruction_prefix = True
            for item in input_items:
                host_message = (
                    isinstance(item, dict)
                    and item.get("type", "message") == "message"
                    and item.get("role") in {"system", "developer"}
                )
                if in_instruction_prefix and host_message:
                    text = _message_text(item.get("content"))
                    if text is not None:
                        if text:
                            instructions.append(text)
                        continue
                in_instruction_prefix = False
                if isinstance(item, dict):
                    item = copy.deepcopy(item)
                    item.pop("id", None)
                    item.pop("status", None)
                    if item.get("type") == "function_call" and not item.get("namespace"):
                        item["namespace"] = "alysis"
                    if host_message:
                        # Native Codex keeps later host context as developer input.
                        # Hoisting these notices would rewrite the instruction prefix
                        # and move them ahead of the observations they qualify.
                        item["role"] = "developer"
                retained.append(item)
            adapted["input"] = retained
            if instructions:
                existing = str(adapted.get("instructions") or "").strip()
                combined = "\n\n".join(instructions)
                adapted["instructions"] = f"{existing}\n\n{combined}" if existing else combined

        adapted["store"] = False
        adapted["stream"] = True
        adapted.pop("temperature", None)
        adapted.pop("max_output_tokens", None)
        adapted.pop("prompt_cache_retention", None)
        adapted.pop("previous_response_id", None)
        for key in (
            "background",
            "conversation",
            "max_tool_calls",
            "metadata",
            "moderation",
            "multi_agent",
            "prompt",
            "safety_identifier",
            "top_logprobs",
            "top_p",
            "truncation",
            "user",
        ):
            adapted.pop(key, None)

        include = adapted.get("include")
        include_values = list(include) if isinstance(include, list) else []
        if "reasoning.encrypted_content" not in include_values:
            include_values.append("reasoning.encrypted_content")
        adapted["include"] = include_values

        text_config = adapted.get("text")
        if isinstance(text_config, dict):
            text_config.setdefault("verbosity", "low")
        else:
            adapted["text"] = {"verbosity": "low"}

        tools = adapted.get("tools")
        if isinstance(tools, list):
            functions = []
            retained = []
            for tool in tools:
                if isinstance(tool, dict) and tool.get("type") in {"function", "custom"}:
                    if tool.get("type") == "function":
                        tool["strict"] = False
                        tool["parameters"] = _sanitize_openai_tool_schema(
                            tool.get("parameters", {"type": "object"})
                        )
                    functions.append(tool)
                elif isinstance(tool, dict) and tool.get("type") in {"namespace", "web_search"}:
                    retained.append(tool)
                else:
                    raise ProviderAuthError(
                        "This hosted tool is not supported by ChatGPT plan usage."
                    )
            if functions:
                retained.append(
                    {
                        "type": "namespace",
                        "name": "alysis",
                        "description": "Alysis Code local tools",
                        "tools": functions,
                    }
                )
            adapted["tools"] = retained

        choice = adapted.get("tool_choice")
        if isinstance(choice, dict) and choice.get("type") in {"function", "custom"}:
            # A bare forced function name cannot address a namespaced tool.
            # Keep the namespace (including replay identity), but expose only
            # the exact selected tool with required, serial calling. This has
            # the same single-tool constraint without an invalid wire name.
            matches = []
            for namespace in adapted.get("tools") or []:
                if not isinstance(namespace, dict) or namespace.get("type") != "namespace":
                    continue
                if choice.get("namespace") and choice["namespace"] != namespace.get("name"):
                    continue
                for tool in namespace.get("tools") or []:
                    if (
                        isinstance(tool, dict)
                        and tool.get("type") == choice["type"]
                        and tool.get("name") == choice.get("name")
                    ):
                        matches.append({**namespace, "tools": [tool]})
            if len(matches) != 1:
                raise ProviderAuthError(
                    "The forced subscription tool is not uniquely available in this request."
                )
            adapted["tools"] = matches
            adapted["tool_choice"] = "required"
            adapted["parallel_tool_calls"] = False
        return adapted

    def list_models(self, *, refresh: bool = False) -> tuple[ProviderModel, ...]:
        if self._models_cache is not None and not refresh:
            live_scope = self.route_credential_scope()
            if live_scope and hmac.compare_digest(live_scope, self._models_cache_scope):
                return self._models_cache
            self._models_cache = None
            self._models_cache_scope = ""
        url = _MODELS_URL
        authorization = self.authorize_request(_MODELS_URL)
        headers = dict(authorization.headers)
        try:
            with self._client(timeout=20.0) as client:
                response = client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            raise ProviderAuthError("Could not load models for this ChatGPT account.") from exc
        if response.status_code == 401:
            headers = dict(
                self.authorize_request(
                    _MODELS_URL,
                    force_refresh=True,
                    expected_credential_scope=authorization.credential_scope,
                ).headers
            )
            try:
                with self._client(timeout=20.0) as client:
                    response = client.get(url, headers=headers)
            except httpx.HTTPError as exc:
                raise ProviderAuthError(
                    "Could not load models for this ChatGPT account after refreshing credentials."
                ) from exc
        if response.status_code >= 400:
            raise ProviderAuthError(
                f"ChatGPT model discovery failed with status {response.status_code}."
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderAuthError("ChatGPT returned an invalid model catalog.") from exc
        raw_models = data.get("models") if isinstance(data, dict) else None
        if not isinstance(raw_models, list):
            raise ProviderAuthError("ChatGPT returned an invalid model catalog.")

        parsed: list[tuple[int, ProviderModel]] = []
        for raw in raw_models:
            if not isinstance(raw, dict):
                continue
            model_id = str(raw.get("slug") or raw.get("id") or "").strip()
            visibility = str(raw.get("visibility") or "list").strip().lower()
            if not model_id or visibility != "list":
                continue
            efforts: list[ProviderReasoningEffort] = []
            raw_efforts = raw.get("supported_reasoning_levels")
            if isinstance(raw_efforts, list):
                for entry in raw_efforts:
                    if not isinstance(entry, dict):
                        continue
                    effort_id = str(entry.get("effort") or entry.get("id") or "").strip()
                    # Ultra is advertised for the Codex app's orchestration,
                    # but /responses rejects it as a reasoning.effort value.
                    # Documented separately from public Responses reasoning effort.
                    if not effort_id or effort_id.casefold() == "ultra":
                        continue
                    efforts.append(
                        ProviderReasoningEffort(
                            id=effort_id,
                            label=reasoning_effort_label(effort_id),
                            description=str(entry.get("description") or "").strip(),
                        )
                    )
            static_model = resolve_chatgpt_codex_static_model(model_id)
            if raw_efforts is None:
                from ..model_reasoning import official_openai_efforts

                efforts = list(official_openai_efforts(model_id))
                if not efforts and static_model is not None:
                    efforts = [
                        ProviderReasoningEffort(
                            id=effort_id,
                            label=reasoning_effort_label(effort_id),
                            description=description,
                        )
                        for effort_id, description in static_model.reasoning_efforts
                        if effort_id.casefold() != "ultra"
                    ]
            reviewed_priority = static_model.priority if static_model is not None else 9999
            try:
                priority = (
                    int(raw["priority"]) if raw.get("priority") is not None else reviewed_priority
                )
            except (TypeError, ValueError):
                priority = reviewed_priority
            input_modalities = _input_modalities(raw)
            if (
                static_model is not None
                and "input_modalities" not in raw
                and "supports_image_input" not in raw
            ):
                input_modalities = static_model.input_modalities
            parsed.append(
                (
                    priority,
                    ProviderModel(
                        id=model_id,
                        label=str(raw.get("display_name") or model_id).strip(),
                        description=str(raw.get("description") or "").strip(),
                        reasoning_efforts=tuple(efforts),
                        default_reasoning_effort=(
                            str(raw.get("default_reasoning_level") or "").strip()
                            or ("medium" if efforts else None)
                        ),
                        input_modalities=input_modalities,
                        context_window_tokens=(
                            _positive_int(raw.get("context_window"))
                            or (
                                static_model.context_window_tokens
                                if static_model is not None
                                else None
                            )
                        ),
                        max_output_tokens=(
                            _positive_int(raw.get("max_output_tokens"))
                            or (
                                static_model.max_output_tokens if static_model is not None else None
                            )
                        ),
                    ),
                )
            )
        # /models lists only part of what a ChatGPT plan can use on this route. On
        # 2026-10-04 an account whose catalog listed five models also completed
        # /responses requests with gpt-6-luna, gpt-6-sol and gpt-6.1-sol. Offer every
        # reviewed ChatGPT model the account's catalog does not mention, with its
        # reviewed capabilities, so the picker matches what the plan can use.
        # Entries the catalog hides stay hidden and failed discovery adds nothing.
        # Only a catalog that already lists a reviewed full-tier (non-Luna) model
        # gains the rest: a Luna-only catalog (a limited plan) or one the review
        # does not cover is never broadened. OpenAI still authorizes every request.
        advertised_ids = {
            str(raw.get("slug") or raw.get("id") or "").strip().casefold()
            for raw in raw_models
            if isinstance(raw, dict)
        }
        listed_ids = {model.id.casefold() for _priority, model in parsed}
        parsed.sort(key=lambda pair: (pair[0], pair[1].label.casefold(), pair[1].id))
        # The account's own first listed model stays the default; a reviewed
        # addition never becomes the default.
        default_id = parsed[0][1].id if parsed else ""
        reviewed_models = load_chatgpt_codex_static_models()
        reviewed_ids = {reviewed.id.casefold() for reviewed in reviewed_models}
        if any(model_id in reviewed_ids and "luna" not in model_id for model_id in listed_ids):
            for reviewed in reviewed_models:
                if reviewed.id.casefold() in advertised_ids:
                    continue
                default_effort = reviewed.default_reasoning_effort
                if default_effort and default_effort.casefold() == "ultra":
                    default_effort = None
                parsed.append(
                    (
                        reviewed.priority,
                        ProviderModel(
                            id=reviewed.id,
                            label=reviewed.label,
                            description=_unlisted_model_description(reviewed.description),
                            reasoning_efforts=tuple(
                                ProviderReasoningEffort(
                                    effort, reasoning_effort_label(effort), description
                                )
                                for effort, description in reviewed.reasoning_efforts
                                if effort.casefold() != "ultra"
                            ),
                            default_reasoning_effort=default_effort,
                            input_modalities=reviewed.input_modalities,
                            context_window_tokens=reviewed.context_window_tokens,
                            max_output_tokens=reviewed.max_output_tokens,
                        ),
                    )
                )
            parsed.sort(key=lambda pair: (pair[0], pair[1].label.casefold(), pair[1].id))
        models = [
            replace(model, is_default=True) if model.id == default_id else model
            for _priority, model in parsed
        ]
        # Show the latest Sol first while preserving the provider's default
        # and the relative order of every other model.
        models.sort(key=lambda model: model.id != "gpt-6.1-sol")
        self._models_cache = tuple(models)
        self._models_cache_scope = authorization.credential_scope
        return self._models_cache

    def _valid_record(
        self,
        record: ProviderTokenRecord,
        *,
        force_refresh: bool = False,
        cancellation_token: Any | None = None,
    ) -> ProviderTokenRecord:
        if not record.client_id or not record.subject:
            raise ProviderLoginRequiredError(
                "Reconnect ChatGPT to authorize Alysis Code for plan usage."
            )
        if _PLAN_SCOPE not in record.scopes:
            raise ProviderAuthError(
                "ChatGPT plan usage is not enabled. Continue with ChatGPT to review permissions, or choose an API provider."
            )
        if not force_refresh and record.expires_at > time.time() + _REFRESH_SKEW_SECONDS:
            return record
        from ..task_budget import refuse_budgeted_auth_refresh

        refuse_budgeted_auth_refresh()
        lock = _refresh_lock(self.provider_id)
        acquired = False
        try:
            if cancellation_token is None:
                lock.acquire()
                acquired = True
            else:
                while not acquired:
                    _throw_if_cancelled(cancellation_token)
                    acquired = lock.acquire(timeout=_CANCELLATION_POLL_SECONDS)
            _throw_if_cancelled(cancellation_token)
            current = load_provider_token(self.provider_id)
            if current is None:
                raise ProviderLoginRequiredError("ChatGPT credentials were removed; connect again.")
            if force_refresh and current.access_token != record.access_token:
                return current
            if not force_refresh and current.expires_at > time.time() + _REFRESH_SKEW_SECONDS:
                return current
            return self._refresh(
                current,
                allow_unexpired_fallback=not force_refresh,
                cancellation_token=cancellation_token,
            )
        finally:
            if acquired:
                lock.release()

    def _refresh(
        self,
        record: ProviderTokenRecord,
        *,
        allow_unexpired_fallback: bool,
        cancellation_token: Any | None = None,
    ) -> ProviderTokenRecord:
        try:
            client_context = (
                cancellation_aware_httpx_client(
                    timeout_s=30.0,
                    transport=self._transport,
                    cancellation_token=cancellation_token,
                )
                if cancellation_token is not None
                else self._client(timeout=30.0)
            )
            with client_context as client:
                response = client.post(
                    _TOKEN_URL,
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": record.refresh_token,
                        "client_id": record.client_id,
                        "resource": _RESOURCE,
                    },
                )
        except httpx.HTTPError as exc:
            if allow_unexpired_fallback and record.expires_at > time.time():
                return record
            raise ProviderAuthError(
                "ChatGPT credential refresh is temporarily unavailable; try again shortly."
            ) from exc
        if response.status_code >= 400:
            error_code = _oauth_error_code(response)
            if response.status_code in {400, 401} or error_code in {
                "invalid_grant",
                "invalid_token",
            }:
                # Another process may have consumed the rotating refresh token
                # and committed its successor while this request was in flight.
                # Reuse only a newer, still-valid record from the *same* public
                # credential generation; a replacement login remains fail-closed.
                try:
                    current = load_provider_token(self.provider_id)
                except ProviderAuthError:
                    current = None
                if (
                    current is not None
                    and current != record
                    and hmac.compare_digest(
                        current.credential_generation,
                        record.credential_generation,
                    )
                    and current.expires_at > time.time()
                ):
                    return current
                raise ProviderLoginRequiredError(
                    "ChatGPT session expired; connect your account again."
                )
            if allow_unexpired_fallback and record.expires_at > time.time():
                return record
            raise ProviderAuthError(
                "ChatGPT credential refresh is temporarily unavailable; try again shortly."
            )
        data = _json_object(response, "ChatGPT returned an invalid refresh response.")
        access_token = str(data.get("access_token") or "").strip()
        refresh_token = str(data.get("refresh_token") or record.refresh_token).strip()
        if not access_token or not refresh_token:
            raise ProviderLoginRequiredError("ChatGPT session expired; connect your account again.")
        id_token = str(data.get("id_token") or "").strip()
        claims = self._validate_id_token(id_token, record.client_id, None) if id_token else {}
        if claims and claims["sub"] != record.subject:
            raise ProviderLoginRequiredError(
                "ChatGPT credential refresh resolved to a different account; connect again."
            )
        scopes = tuple(str(data["scope"]).split()) if "scope" in data else record.scopes
        if _PLAN_SCOPE not in scopes:
            raise ProviderLoginRequiredError("ChatGPT plan permission was removed; connect again.")
        refreshed = ProviderTokenRecord(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=_token_expiry(data, access_token),
            account_id=record.account_id,
            account_label=record.account_label,
            credential_generation=record.credential_generation,
            client_id=record.client_id,
            subject=record.subject,
            id_token=id_token or record.id_token,
            scopes=scopes,
        )
        return save_refreshed_provider_token(
            self.provider_id,
            expected=record,
            refreshed=refreshed,
        )

    def _browser_login(
        self, write: Callable[[str], None], *, new_account: bool = False
    ) -> ProviderTokenRecord:
        registration = subscription_registration(self.provider_id)
        active = load_provider_token(self.provider_id)
        saved_client = "" if new_account else str(registration.get("client_id") or "")
        client_id = saved_client or _DEFAULT_CLIENT_ID
        verifier = _pkce_verifier()
        expected_state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        server = _bind_callback_server()
        redirect_uri = f"http://127.0.0.1:{int(server.server_address[1])}/auth/callback"
        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct",
            "resource": _RESOURCE,
            "ext_agent_host_id": registration["host_id"],
            "state": expected_state,
            "nonce": nonce,
            "code_challenge": _pkce_challenge(verifier),
            "code_challenge_method": "S256",
        }
        if not saved_client:
            params["agent_name_hint"] = "Alysis Code"
        elif active and active.client_id == saved_client and active.id_token:
            params["id_token_hint"] = active.id_token
            if _PLAN_SCOPE not in active.scopes:
                params["prompt"] = "consent"
        auth_url = f"{_ISSUER}/api/accounts/authorize?{urlencode(params)}"
        callback: dict[str, str] = {}
        server.RequestHandlerClass = _callback_handler(callback, expected_state)
        server.timeout = 0.5
        try:
            write("Opening Continue with ChatGPT in your browser…")
            if not open_url(auth_url):
                # A retained ID token must never enter live logs even on a browser failure.
                raise ProviderAuthError(
                    "Could not open the system browser. Check your default browser and try Continue with ChatGPT again."
                )
            deadline = time.monotonic() + _BROWSER_TIMEOUT_SECONDS
            while time.monotonic() < deadline and "done" not in callback:
                server.handle_request()
        finally:
            server.server_close()
        if "done" not in callback:
            raise ProviderAuthError(
                "ChatGPT browser sign-in timed out after five minutes. Try again."
            )
        if callback.get("error"):
            raise ProviderAuthError(
                "ChatGPT sign-in was declined or rejected. No account was changed."
            )
        issued_client = callback.get("client_id") or saved_client
        if (
            not issued_client
            or issued_client == _DEFAULT_CLIENT_ID
            or (saved_client and issued_client != saved_client)
        ):
            raise ProviderAuthError(
                "ChatGPT returned an incomplete or different app registration. Try again."
            )
        code = callback.get("code") or ""
        if not code:
            raise ProviderAuthError("ChatGPT sign-in did not return an authorization code.")
        return self._exchange_code(
            code=code,
            verifier=verifier,
            redirect_uri=redirect_uri,
            client_id=issued_client,
            nonce=nonce,
            expected_subject=None if new_account else registration.get("subject"),
        )

    def _exchange_code(
        self,
        *,
        code: str,
        verifier: str,
        redirect_uri: str,
        client_id: str,
        nonce: str,
        expected_subject: str | None = None,
    ) -> ProviderTokenRecord:
        try:
            with self._client(timeout=30.0) as client:
                response = client.post(
                    _TOKEN_URL,
                    data={
                        "grant_type": "authorization_code",
                        "code": code,
                        "redirect_uri": redirect_uri,
                        "client_id": client_id,
                        "code_verifier": verifier,
                        "resource": _RESOURCE,
                    },
                )
        except httpx.HTTPError as exc:
            raise ProviderAuthError("Could not complete ChatGPT sign-in. Try again.") from exc
        if response.status_code >= 400:
            raise ProviderAuthError(
                f"ChatGPT token exchange failed (status {response.status_code}). Start a fresh sign-in."
            )
        data = _json_object(response, "ChatGPT returned an invalid token response.")
        access_token = str(data.get("access_token") or "").strip()
        refresh_token = str(data.get("refresh_token") or "").strip()
        id_token = str(data.get("id_token") or "").strip()
        if (
            not access_token
            or not refresh_token
            or str(data.get("token_type") or "").lower() != "bearer"
        ):
            raise ProviderAuthError("ChatGPT token response was incomplete.")
        claims = self._validate_id_token(id_token, client_id, nonce)
        if expected_subject and claims["sub"] != expected_subject:
            raise ProviderAuthError(
                "The sign-in belongs to a different ChatGPT account. Use Switch account."
            )
        return ProviderTokenRecord(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=_token_expiry(data, access_token),
            account_id=claims["sub"],
            account_label=str(claims.get("email") or "ChatGPT account"),
            client_id=client_id,
            subject=claims["sub"],
            id_token=id_token,
            scopes=tuple(str(data.get("scope") or "").split()),
        )

    def _discovery(self) -> dict[str, Any]:
        try:
            with self._client(timeout=20.0) as client:
                response = client.get(f"{_ISSUER}/.well-known/openid-configuration")
                response.raise_for_status()
            data = _json_object(response, "Could not validate ChatGPT identity configuration.")
            if data.get("issuer") != _ISSUER:
                raise ValueError()
            return data
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderAuthError("Could not validate ChatGPT identity configuration.") from exc

    def _revocation_endpoint(self) -> str:
        endpoint = str(self._discovery().get("revocation_endpoint") or "")
        _validate_auth_endpoint(endpoint)
        return endpoint

    def _validate_id_token(self, token: str, client_id: str, nonce: str | None) -> dict[str, Any]:
        from .subscription_identity import validate_id_token

        return validate_id_token(
            token,
            client_id=client_id,
            nonce=nonce,
            discovery=self._discovery(),
            client_factory=self._client,
        )

    def _client(self, *, timeout: float) -> httpx.Client:
        from ..task_budget import refuse_budgeted_auth_refresh

        refuse_budgeted_auth_refresh()
        return httpx.Client(
            timeout=timeout,
            transport=self._transport,
            follow_redirects=False,
        )


def _refresh_lock(provider_id: str) -> threading.Lock:
    with _REFRESH_LOCKS_GUARD:
        return _REFRESH_LOCKS.setdefault(provider_id, threading.Lock())


def _validate_auth_endpoint(endpoint: str) -> None:
    split = urlsplit(endpoint)
    if (
        split.scheme != "https"
        or split.hostname != "auth.openai.com"
        or split.port not in {None, 443}
        or split.username
        or split.password
        or split.query
        or split.fragment
    ):
        raise ProviderAuthError("ChatGPT returned an invalid identity endpoint.")


def _canonical_request_url(url: str) -> str:
    split = urlsplit(str(url or ""))
    if (
        split.scheme != "https"
        or split.hostname != "api.openai.com"
        or split.username
        or split.password
        or split.query
        or split.fragment
    ):
        return ""
    port = split.port
    if port not in {None, 443}:
        return ""
    return f"https://api.openai.com{split.path.rstrip('/')}"


def _credential_scope(record: ProviderTokenRecord) -> str:
    """Derive the public route proof from a random, non-secret vault generation."""

    return hashlib.sha256(
        (f"alysis-provider-credential-generation:v1:{record.credential_generation}").encode("ascii")
    ).hexdigest()


def _pkce_verifier() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(64)).decode("ascii").rstrip("=")


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _bind_callback_server() -> HTTPServer:
    last_error: OSError | None = None
    for port in _CALLBACK_PORTS:
        try:
            return HTTPServer(("127.0.0.1", port), BaseHTTPRequestHandler)
        except OSError as exc:
            last_error = exc
    raise ProviderAuthError("Could not open the local ChatGPT login callback port.") from last_error


def _callback_handler(
    result: dict[str, str],
    expected_state: str,
) -> type[BaseHTTPRequestHandler]:
    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
            split = urlsplit(self.path)
            if split.path != "/auth/callback":
                self._respond(404, "Not found")
                return
            query = parse_qs(split.query, keep_blank_values=True)
            state = str((query.get("state") or [""])[0])
            if not hmac.compare_digest(state, expected_state):
                self._respond(400, "State mismatch. Return to Alysis Code and try again.")
                return
            error = str((query.get("error_description") or query.get("error") or [""])[0])
            if error:
                result.update({"done": "1", "error": error})
                self._respond(400, error)
                return
            code = str((query.get("code") or [""])[0])
            if not code:
                result.update({"done": "1", "error": "Missing authorization code."})
                self._respond(400, "Missing authorization code.")
                return
            result.update(
                {"done": "1", "code": code, "client_id": str((query.get("client_id") or [""])[0])}
            )
            self._respond(
                200, "Sign-in response received. Return to Alysis Code to see the result."
            )

        def log_message(self, _format: str, *_args: Any) -> None:
            return

        def _respond(self, status: int, message: str) -> None:
            safe = html.escape(message, quote=True)
            body = (
                "<!doctype html><meta charset='utf-8'><title>Alysis Code sign-in</title>"
                "<style>body{font-family:system-ui;max-width:42rem;margin:4rem auto;"
                "padding:0 1.5rem;line-height:1.5}</style>"
                f"<h1>Alysis Code</h1><p>{safe}</p>"
            ).encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    return CallbackHandler


def _json_object(response: httpx.Response, message: str) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError as exc:
        raise ProviderAuthError(message) from exc
    if not isinstance(data, dict):
        raise ProviderAuthError(message)
    return data


def _oauth_error_code(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return ""
    if not isinstance(data, dict):
        return ""
    error = data.get("error")
    if isinstance(error, dict):
        return str(error.get("code") or error.get("type") or "").strip().lower()
    return str(error or "").strip().lower()


def _jwt_claims(token: str) -> dict[str, Any]:
    parts = str(token or "").split(".")
    if len(parts) < 2 or not parts[1]:
        return {}
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        data = json.loads(decoded.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _token_expiry(data: Mapping[str, Any], access_token: str) -> float:
    try:
        expires_in = float(data.get("expires_in") or 0)
    except (TypeError, ValueError):
        expires_in = 0
    if expires_in > 0:
        return time.time() + expires_in
    claims = _jwt_claims(access_token)
    try:
        expiration = float(claims.get("exp") or 0)
    except (TypeError, ValueError):
        expiration = 0
    return expiration if expiration > time.time() else time.time() + _DEFAULT_TOKEN_LIFETIME_SECONDS


def _message_text(content: Any) -> str | None:
    """Flatten only plain text; retain other content in its ordered input item."""
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
        elif (
            isinstance(item, dict)
            and item.get("type") in {"text", "input_text"}
            and isinstance(item.get("text"), str)
            and item.keys() <= {"type", "text"}
        ):
            parts.append(item["text"])
        else:
            return None
    return "\n".join(part for part in parts if part).strip()


def _unlisted_model_description(description: str) -> str:
    """Describe a reviewed model that the account's /models answer did not list."""

    note = "access checked when used"
    return f"{description} · {note}" if description else note.capitalize()


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _input_modalities(raw: Mapping[str, Any]) -> tuple[str, ...]:
    advertised = raw.get("input_modalities")
    if isinstance(advertised, list):
        values = tuple(
            dict.fromkeys(
                str(item or "").strip().lower() for item in advertised if str(item or "").strip()
            )
        )
        if values:
            return values
    if raw.get("supports_image_input") is True:
        return ("text", "image")
    return ("text",)


def _sanitize_openai_tool_schema(value: Any) -> Any:
    """Lower broad JSON Schema/MCP shapes to Codex-compatible tool schemas."""

    supported_types = {"string", "number", "boolean", "integer", "object", "array", "null"}
    composition_keys = ("anyOf", "oneOf", "allOf")
    if isinstance(value, bool):
        return {"type": "string"}
    if isinstance(value, list):
        return [_sanitize_openai_tool_schema(item) for item in value]
    if not isinstance(value, dict):
        return value

    result: dict[str, Any] = {}
    if isinstance(value.get("$ref"), str):
        result["$ref"] = value["$ref"]
    if isinstance(value.get("description"), str):
        result["description"] = value["description"]
    if "const" in value:
        result["enum"] = [value["const"]]
    elif isinstance(value.get("enum"), list):
        result["enum"] = copy.deepcopy(value["enum"])
    properties = value.get("properties")
    if isinstance(properties, dict):
        result["properties"] = {
            str(key): _sanitize_openai_tool_schema(item) for key, item in properties.items()
        }
    required = value.get("required")
    if isinstance(required, list):
        result["required"] = [item for item in required if isinstance(item, str)]
    if "items" in value:
        result["items"] = _sanitize_openai_tool_schema(value["items"])
    if "additionalProperties" in value:
        additional = value["additionalProperties"]
        result["additionalProperties"] = (
            additional if isinstance(additional, bool) else _sanitize_openai_tool_schema(additional)
        )
    for key in composition_keys:
        if isinstance(value.get(key), list):
            result[key] = [_sanitize_openai_tool_schema(item) for item in value[key]]
    for key in ("$defs", "definitions"):
        definitions = value.get(key)
        if isinstance(definitions, dict):
            result[key] = {
                str(name): _sanitize_openai_tool_schema(item) for name, item in definitions.items()
            }

    raw_type = value.get("type")
    if isinstance(raw_type, str):
        schema_types = [raw_type] if raw_type in supported_types else []
    elif isinstance(raw_type, list):
        schema_types = [
            item for item in raw_type if isinstance(item, str) and item in supported_types
        ]
    else:
        schema_types = []
    if not schema_types and ("$ref" in result or any(key in result for key in composition_keys)):
        return result
    if not schema_types:
        if any(key in value for key in ("properties", "required", "additionalProperties")):
            schema_types = ["object"]
        elif any(key in value for key in ("items", "prefixItems")):
            schema_types = ["array"]
        elif "enum" in result or "format" in value:
            schema_types = ["string"]
        elif any(
            key in value
            for key in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf")
        ):
            schema_types = ["number"]
    if not schema_types:
        return {}
    result["type"] = schema_types[0] if len(schema_types) == 1 else schema_types
    if "object" in schema_types and "properties" not in result:
        result["properties"] = {}
    if "array" in schema_types and "items" not in result:
        result["items"] = {"type": "string"}
    return result


__all__ = ["SESSION_EXPIRED_DETAIL", "OpenAICodexSubscriptionAuth"]
