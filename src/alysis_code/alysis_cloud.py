"""Endpoints and identifiers for the hosted Alysis Code Pro service.

`alysis login` runs an RFC 8628-style device flow against the configured hosted API (`device-code` / `device-token`); the user approves the code on the
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

# Product site: serves the /activate device-approval page and /account.
#
# ---------------------------------------------------------------------------
# Everything user-visible that names the site derives from _DEFAULT_SITE_URL —
# help text, proxy error messages, the /activate and /account links, and the
# tests asserting on them. Keep it that way: re-hardcoding the host anywhere
# else is caught by test_rebrand_compatibility.py.
#
# The device-login flow depends on this host actually serving /activate against
# the configured hosted API. The CLI sends Pro users there to approve a login
# code, so the two must move together — a client pointed at a host that does
# not serve the page breaks sign-in with no client-side workaround.
#
# ---------------------------------------------------------------------------
PRODUCT_SITE_URL = "https://alysiscode.com"
_DEFAULT_SITE_URL = PRODUCT_SITE_URL

# Public device-login and gateway API. This address is not a credential; the
# service authenticates device keys server-side. No project keys are bundled.
DEFAULT_CLOUD_API_URL = "https://vzigujbcjjmpntxhmyvr.supabase.co/functions/v1"

# The profile/preset key used for the hosted Pro provider.
PROFILE_KEY = "alysis"

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


def cloud_api_url() -> str:
    """Hosted API by default, with explicit overrides for custom deployments."""
    override = _clean(env_get("ALYSIS_CLOUD_API_URL"))
    if override:
        return _checked(override)
    # Preserve existing explicitly configured installations.
    legacy = _clean(env_get("ALYSIS_SUPABASE_URL"))
    if legacy:
        return f"{_checked(legacy)}/functions/v1"
    return _checked(DEFAULT_CLOUD_API_URL)


def public_client_key() -> str:
    """Optional public routing key; never a server/service-role credential."""
    return (env_get("ALYSIS_CLOUD_PUBLIC_KEY") or "").strip()


def _function_url(name: str) -> str:
    base = cloud_api_url()
    if not base:
        raise AlysisCloudConfigError(
            "Alysis hosted services are not configured in this build. "
            "Set ALYSIS_CLOUD_API_URL to your deployment's API base URL. "
            "Other configured providers remain available."
        )
    return f"{base}/{name}"


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

    The hosted service holds upstream provider credentials and meters account credits.
    Device login validates the API configuration before sending any requests.
    """
    override = env_get("ALYSIS_GATEWAY_URL")
    if override:
        return _checked(override)
    base = cloud_api_url()
    return f"{base}/llm/v1" if base else ""


def device_code_url() -> str:
    """POST here to start a device login (returns user_code + device_code)."""
    return _function_url("device-code")


def device_token_url() -> str:
    """POST device_code here until the user approves (returns the slk_ key)."""
    return _function_url("device-token")


def feedback_url() -> str:
    """Private feedback intake, authenticated with the saved Alysis device key."""
    return _function_url("feedback")


def account_status_url() -> str:
    """Read the website billing summary with the saved Alysis device key."""
    return _function_url("account-status")


def activate_url() -> str:
    """The website page where a signed-in user approves a device code."""
    return f"{site_url()}/activate"


def models_url() -> str:
    """The gateway's OpenAI-shaped model listing."""
    base = gateway_base_url()
    if not base:
        raise AlysisCloudConfigError("Alysis hosted gateway is not configured.")
    return f"{base}/models"
