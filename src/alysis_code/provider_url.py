from __future__ import annotations

from urllib.parse import urlsplit

from . import alysis_cloud
from .branding import PRODUCT_NAME

_ALYSIS_TRIAL_PROXY_PATH = "/functions/v1/llm"
# Dedicated gateway hostname.
_ALYSIS_GATEWAY_HOSTS = frozenset({"api.alysiscode.com"})
_ZAI_CODING_PLAN_PATH = "/api/coding/paas/v4"
_CHATGPT_CODEX_PATH = "/backend-api/codex"

# What the UI calls the hosted gateway in place of its host. The gateway runs as
# a Supabase Edge Function, so its host is the Supabase project ref — an
# infrastructure id that reads like a leaked credential and changes whenever the
# gateway moves.
ALYSIS_GATEWAY_LABEL = f"{PRODUCT_NAME} gateway"


def is_alysis_gateway_url(base_url: str | None) -> bool:
    """True when ``base_url`` points at the Alysis Code hosted LLM gateway."""

    raw = str(base_url or "").strip()
    if not raw:
        return False
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return False
    host = (parsed.hostname or "").rstrip(".").casefold()
    try:
        configured = alysis_cloud.gateway_base_url()
        api_base = alysis_cloud.cloud_api_url()
    except alysis_cloud.AlysisCloudConfigError:
        configured = ""
        api_base = ""
    if configured and raw.rstrip("/") == configured.rstrip("/"):
        return True
    if api_base and raw.rstrip("/") == f"{api_base}/llm/v1":
        return True
    if host in _ALYSIS_GATEWAY_HOSTS:
        return True
    path = (parsed.path or "").casefold()
    return _ALYSIS_TRIAL_PROXY_PATH in path and (
        host == "supabase.co" or host.endswith(".supabase.co")
    )


def display_host(base_url: str | None) -> str:
    """Host (and explicit port) of a base URL for display; ``""`` when there is none.

    Never ``netloc``: a base URL can carry credentials as userinfo
    (``https://user:token@proxy.example/v1``) and ``netloc`` keeps them.
    """

    raw = str(base_url or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return ""
    host = parsed.hostname or ""
    if not host:
        return ""
    if ":" in host:  # IPv6 literal: urlsplit strips the brackets
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError:  # out-of-range or non-numeric port; the host is still useful
        port = None
    return host if port is None else f"{host}:{port}"


def display_endpoint(base_url: str | None) -> str:
    """Where a profile sends requests, as a short label for pickers and summaries."""

    if is_alysis_gateway_url(base_url):
        return ALYSIS_GATEWAY_LABEL
    return display_host(base_url)


def known_provider_key_from_base_url(base_url: str | None) -> str | None:
    """Classify provider-owned endpoints without guessing from arbitrary hosts.

    Callers deliberately retain their own fallback policies for unknown URLs.
    This function is only the shared truth table for provider endpoints whose
    host (and, where required, path) identifies a documented transport surface.
    """

    raw = str(base_url or "").strip()
    if not raw:
        return None
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return None
    host = (parsed.hostname or "").rstrip(".").casefold()
    path = (parsed.path or "").casefold()
    if not host:
        return None

    # The Alysis Code hosted proxy (the `llm` Supabase Edge Function, or a
    # reserved dedicated gateway host) forwards to DeepSeek upstream, so
    # provider-shaped behavior (limits, capabilities) mirrors DeepSeek's. (In
    # the retired MiMo-trial era the same path forwarded to OpenRouter; that
    # service no longer exists.)
    if is_alysis_gateway_url(raw):
        return "deepseek"
    if "dashscope" in host:
        return "qwen"
    if host == "openrouter.ai" or host.endswith(".openrouter.ai"):
        return "openrouter"
    if host == "api.openai.com":
        return "openai"
    # ChatGPT subscriptions use OpenAI Responses capabilities even though their
    # authentication and endpoint differ from the public API. Treating this as
    # an unknown "chatgpt" provider silently disables reasoning summaries.
    if host == "chatgpt.com" and (
        path == _CHATGPT_CODEX_PATH or path.startswith(f"{_CHATGPT_CODEX_PATH}/")
    ):
        return "openai"
    if (
        host.endswith(".openai.azure.com")
        or host.endswith(".cognitiveservices.azure.com")
        or host.endswith(".services.ai.azure.com")
    ):
        return "azure"
    if host == "api.deepseek.com" or host.endswith(".deepseek.com"):
        return "deepseek"
    if host == "integrate.api.nvidia.com":
        return "nvidia"
    if host == "api.z.ai" and (
        path == _ZAI_CODING_PLAN_PATH or path.startswith(f"{_ZAI_CODING_PLAN_PATH}/")
    ):
        return "zai_coding_plan"
    if host == "generativelanguage.googleapis.com":
        return "gemini"
    if host == "api.mistral.ai" or host.endswith(".mistral.ai"):
        return "mistral"
    if host in {"api.moonshot.ai", "api.moonshot.cn"}:
        return "moonshot"
    if host == "api.kimi.com":
        return "kimi-code"
    if host == "api.x.ai" or host == "x.ai" or host.endswith(".x.ai"):
        return "xai"
    return None


__all__ = [
    "ALYSIS_GATEWAY_LABEL",
    "display_endpoint",
    "display_host",
    "is_alysis_gateway_url",
    "known_provider_key_from_base_url",
]
