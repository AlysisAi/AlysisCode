"""Endpoints and identifiers for the hosted Alysis Code Pro service.

`alysis login` runs an RFC 8628-style device flow against Supabase Edge
Functions (`device-code` / `device-token`); the user approves the code on the
account website's /activate page. The CLI receives a gateway key (``slk_…``)
and talks to the Alysis Code LLM gateway — an OpenAI-compatible proxy that meters
the subscription's credits server-side. The CLI never holds upstream provider
keys for Pro; BYOK profiles are configured separately and take precedence.

Values can be overridden via environment variables to point at a different
deployment (e.g. a staging project or local stubs) during testing.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from .branding import env_get

# Supabase project hosting the device-login edge functions.
_DEFAULT_SUPABASE_URL = "https://vzigujbcjjmpntxhmyvr.supabase.co"

# Product site: serves the /activate device-approval page and /account.
#
# ---------------------------------------------------------------------------
# Everything user-visible that names the site derives from _DEFAULT_SITE_URL —
# help text, proxy error messages, the /activate and /account links, and the
# tests asserting on them. Keep it that way: re-hardcoding the host anywhere
# else is caught by test_rebrand_compatibility.py.
#
# The device-login flow depends on this host actually serving /activate against
# the Supabase project below. The CLI sends Pro users there to approve a login
# code, so the two must move together — a client pointed at a host that does
# not serve the page breaks sign-in with no client-side workaround.
#
# ---------------------------------------------------------------------------
PRODUCT_SITE_URL = "https://alysiscode.com"
_DEFAULT_SITE_URL = PRODUCT_SITE_URL

# Supabase "anon" key: a PUBLIC client identifier (shipped in the website's
# browser bundle too). It grants nothing by itself — the edge functions are
# either public-by-design (device flow) or JWT/secret-guarded server-side.
ANON_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InZ6aWd1amJjamptcG50eGhteXZyIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODA5Mzc0NTIsImV4cCI6MjA5NjUxMzQ1Mn0."
    "vLH9q-BNO8IWIZrVlvCw8pZWXdLgmKG4Tl9toTTD3pg"
)

# The profile/preset key used for the hosted Pro provider.
PROFILE_KEY = "alysis"

# LEGACY: base URL of the retired MiMo-trial proxy (an OpenRouter-forwarding
# Supabase Edge Function). The service is gone, but URL classifiers (web
# search / provider limits) still recognize it so configs from that era keep
# loading with sensible behavior instead of misclassifying.
DEFAULT_PROXY_BASE_URL = f"{_DEFAULT_SUPABASE_URL}/functions/v1/llm/v1"


# Loopback hosts may use http:// (local stubs / tests); every other host must
# be https so device codes and the gateway key never travel in cleartext.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class AlysisCloudConfigError(ValueError):
    """Raised when a configured Alysis Code cloud URL is unsafe (e.g. cleartext http)."""


def _clean(url: str) -> str:
    return str(url or "").strip().rstrip("/")


def _checked(url: str) -> str:
    """Clean a URL and reject cleartext http:// for non-loopback hosts.

    Device codes and the long-lived gateway key travel to these endpoints, so a
    downgraded (http://) origin from an env override would leak them. https is
    required unless the host is loopback (local stubs / tests) or
    ALYSIS_ALLOW_INSECURE_URLS is explicitly set.
    """
    cleaned = _clean(url)
    if not cleaned:
        return cleaned
    parts = urlsplit(cleaned)
    if parts.scheme.lower() == "https":
        return cleaned
    host = (parts.hostname or "").lower()
    if host in _LOOPBACK_HOSTS or env_get("ALYSIS_ALLOW_INSECURE_URLS"):
        return cleaned
    raise AlysisCloudConfigError(
        f"Refusing to use insecure Alysis Code URL {cleaned!r}: https is required "
        "(set ALYSIS_ALLOW_INSECURE_URLS=1 only for trusted local testing)."
    )


def supabase_url() -> str:
    return _checked(env_get("ALYSIS_SUPABASE_URL") or _DEFAULT_SUPABASE_URL)


def site_url() -> str:
    return _checked(env_get("ALYSIS_SITE_URL") or _DEFAULT_SITE_URL)


def site_host() -> str:
    """Bare hostname of the product site, for prose that reads better without a scheme."""
    return urlsplit(site_url()).netloc


def account_url() -> str:
    """Where a Pro user manages their plan and credits.

    Everything user-visible that names the account page goes through here, so
    moving the site is the single constant above and not a search-and-replace
    across help text, error messages, and docs.
    """
    return f"{site_url()}/account"


def gateway_base_url() -> str:
    """OpenAI-compatible base URL; the LLM client appends ``/chat/completions``.

    The hosted proxy runs as the `llm` Supabase Edge Function (the same shape
    the MiMo-trial proxy used), holding the upstream DeepSeek key server-side
    and metering each account's allowance/credits.
    """
    override = env_get("ALYSIS_GATEWAY_URL")
    if override:
        return _checked(override)
    return f"{supabase_url()}/functions/v1/llm/v1"


def device_code_url() -> str:
    """POST here to start a device login (returns user_code + device_code)."""
    return f"{supabase_url()}/functions/v1/device-code"


def device_token_url() -> str:
    """POST device_code here until the user approves (returns the slk_ key)."""
    return f"{supabase_url()}/functions/v1/device-token"


def feedback_url() -> str:
    """Private feedback intake, authenticated with the saved Alysis device key."""
    return f"{supabase_url()}/functions/v1/feedback"


def account_status_url() -> str:
    """Read the website billing summary with the saved Alysis device key."""
    return f"{supabase_url()}/functions/v1/account-status"


def activate_url() -> str:
    """The website page where a signed-in user approves a device code."""
    return f"{site_url()}/activate"


def models_url() -> str:
    """The gateway's OpenAI-shaped model listing."""
    return f"{gateway_base_url()}/models"
