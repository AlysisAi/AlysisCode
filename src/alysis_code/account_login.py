"""Device-flow login for Alysis Code Pro (`alysis login`).

The CLI asks the account backend for a short user code (``ABCD-EFGH``), opens
the website's /activate page where the signed-in user approves it, and polls
until approval — then receives the user's long-lived gateway key (``slk_…``),
stores it locally, and activates the ``alysis`` provider profile pointed at
the Alysis Code LLM gateway (OpenAI-compatible; credits are metered server-side).

The device_code secret and the gateway key only ever travel in HTTPS request
and response bodies — never in URLs. The short user_code does appear in the
opened URL, but it is single-use, expires in minutes, and is worthless without
the device_code it is paired with.

This flow needs no localhost listener, so it works over SSH and inside WSL the
same as on a desktop.
"""

from __future__ import annotations

import json
import math
import platform
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from . import alysis_cloud as cloud
from .config import (
    AppConfig,
    clear_persisted_profile_key,
    load_persisted_profile_keys,
    save_config,
    save_persisted_profile_key,
)
from .device_identity import get_installation_id
from .host_browser import open_url
from .profile_presets import canonical_model_alias_for_preset, get_preset, make_profile_from_preset
from .profiles import ProfileSpec, add_profile, get_profile, set_active_profile

# Matches the server-side device-code expiry (15 min) so we poll until the code
# itself dies rather than giving up while the user is still signing in.
_DEFAULT_TIMEOUT_S = 900.0
_HTTP_TIMEOUT_S = 15.0
_DEFAULT_POLL_INTERVAL_S = 5.0
# Kept short: model discovery runs inline while rendering the interactive `/config`
# model picker, so an offline/slow gateway must not stall the menu for long.
_MODELS_TIMEOUT_S = 6.0

_FRESH_LOGIN_DEFAULT_MODEL = "gpt-6-luna"


class AlysisLoginError(Exception):
    """Raised when the device login handshake fails."""


@dataclass(frozen=True)
class LoginResult:
    email: str | None
    profile_name: str
    base_url: str
    model: str


@dataclass(frozen=True)
class LoginStatus:
    logged_in: bool
    profile_name: str
    base_url: str
    active: bool
    key_preview: str | None


@dataclass(frozen=True)
class TrialStatus:
    """Account status snapshot (kept name-compatible with the old MiMo trial)."""

    plan: str | None
    email: str | None
    trial_ends_at: str | None
    tokens_total: int | None
    tokens_used: int | None
    tokens_remaining: int | None
    display_name: str | None = None
    # Credits are monetary ledger units, not model tokens. Preserve the website contract.
    billing: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def login(
    cfg: AppConfig,
    *,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    browser_opener: Callable[[str], bool] | None = None,
    output_write: Callable[[str], None] | None = None,
) -> LoginResult:
    """Run the device-flow login and persist the resulting gateway key."""
    writer = output_write or (lambda _msg: None)

    # Fail fast on an insecure (non-https) cloud URL before starting, so
    # neither the device_code nor the gateway key could leave over http://.
    try:
        cloud.device_code_url()
        cloud.device_token_url()
        cloud.gateway_base_url()
    except cloud.AlysisCloudConfigError as exc:
        raise AlysisLoginError(str(exc)) from exc

    grant = _request_device_code()

    writer(f"Your one-time code: [bold]{grant.user_code}[/bold]")
    writer(f"Approve it in your browser:\n  {grant.verification_url}")
    opener = browser_opener or _open_browser
    if not _safe_open(opener, grant.verification_url):
        writer("Could not open a browser automatically. Open the URL above manually.")
    writer("Waiting for approval…")

    key = _poll_for_key(grant, timeout_s=timeout_s)

    try:
        # Create + activate the profile first (this saves config); only then
        # persist the key, so a save failure never leaves a stored key pointing
        # at an unconfigured profile. resolve_api_key then returns the gateway
        # key as the Bearer for the `alysis` profile (no other wiring).
        result = _activate_alysis_profile(cfg, email=None)
        save_persisted_profile_key(cloud.PROFILE_KEY, key)
    except OSError as exc:
        raise AlysisLoginError(f"Logged in, but couldn't save your session locally: {exc}") from exc
    return result


def logout(cfg: AppConfig) -> bool:
    """Revoke the gateway key server-side and forget it locally.

    The server revocation is what actually ends access: any client still
    holding the key in memory (including the current chat session) gets a
    401 on its next request. Best-effort — offline logout still clears the
    local key, and the orphaned server key can be revoked from the website
    later. Returns True if a stored key was cleared.
    """
    stored = load_persisted_profile_keys().get(cloud.PROFILE_KEY)

    if stored:
        try:
            request = urllib.request.Request(
                f"{cloud.gateway_base_url()}/logout",
                data=b"{}",
                headers={
                    "Authorization": f"Bearer {stored}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_S).close()
        except Exception:  # noqa: BLE001 - revocation is best-effort
            pass

    return clear_persisted_profile_key(cloud.PROFILE_KEY)


def login_status(cfg: AppConfig) -> LoginStatus:
    """Report whether an Alysis Code Pro session is connected."""
    stored = load_persisted_profile_keys().get(cloud.PROFILE_KEY)
    active = str((cfg.extra_fields or {}).get("active_profile") or "").strip()
    profile = get_profile(cfg, cloud.PROFILE_KEY)
    base_url = profile.base_url if profile is not None else cloud.gateway_base_url()
    preview = None
    if stored:
        preview = stored[:8] + "…" if len(stored) > 8 else stored
    return LoginStatus(
        logged_in=bool(stored),
        profile_name=cloud.PROFILE_KEY,
        base_url=base_url,
        active=active == cloud.PROFILE_KEY,
        key_preview=preview,
    )


class _AccountNoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        # Never forward the saved device credential to a redirect target.
        return None


def _account_text(value: object) -> str | None:
    return value.strip()[:200] if isinstance(value, str) and value.strip() else None


def _account_int(value: object, *, allow_none: bool = False) -> int | None | bool:
    """A finite whole number within JSON-safe range; False means invalid."""
    if value is None and allow_none:
        return None
    if type(value) not in (int, float):
        return False
    # Bound integers before float conversion: JSON can contain arbitrarily large
    # ints, and math.isfinite(10**400) raises rather than rejecting the section.
    if not -(2**53 - 1) <= value <= 2**53 - 1:  # type: ignore[operator]
        return False
    if not math.isfinite(value) or value != int(value):  # type: ignore[arg-type]
        return False
    return int(value)  # type: ignore[arg-type]


def _account_paid(value: object) -> dict[str, Any] | None:
    """Validate the optional paid-plan section (Plan 1 / Plan 5).

    An invalid section is dropped rather than failing the whole account status,
    so older and newer servers never make `whoami` lose the balance line.
    """
    if not isinstance(value, dict):
        return None
    plan = _account_text(value.get("plan"))
    name = _account_text(value.get("plan_name"))
    if plan is None or name is None:
        return None
    from .model_reasoning import hosted_model_capabilities

    result: dict[str, Any] = {"plan": plan, "plan_name": name}
    base = _account_int(value.get("base_monthly_credits_microusd"), allow_none=True)
    unit = _account_int(value.get("ledger_microusd_per_base_credit"), allow_none=True)
    monthly = _account_int(value.get("monthly_credits_microusd"))
    if (
        value.get("credit_unit_version") == "paid-base-display-v1"
        and type(base) is int
        and base > 0
        and type(unit) is int
        and unit > 0
        and type(monthly) is int
        and base <= monthly
        and monthly * 10000 == base * unit
    ):
        result.update(
            credit_unit_version="paid-base-display-v1",
            base_monthly_credits_microusd=base,
            ledger_microusd_per_base_credit=unit,
        )
    maximum = _account_int(value.get("max_usage_value_microusd"), allow_none=True)
    if maximum is not False and maximum is not None and maximum >= 0:
        result["max_usage_value_microusd"] = maximum
    for field in (
        "monthly_credits_microusd",
        "cap_5h_microusd",
        "burn_5h_microusd",
        "cap_day_microusd",
        "burn_day_microusd",
        "max_output_tokens",
    ):
        number = _account_int(value.get(field))
        if number is False or number is None or number < 0:
            return None
        result[field] = number
    for field in (
        "premium_month_limit_microusd",
        "premium_month_used_microusd",
        "premium_day_limit_microusd",
        "premium_day_used_microusd",
    ):
        number = _account_int(value.get(field), allow_none=True)
        if number is False or (number is not None and number < 0):
            return None
        result[field] = number
    for field in ("credits_reset_at", "period_start", "upgrade_to"):
        result[field] = _account_text(value.get(field))
    models = value.get("models")
    if not isinstance(models, list) or len(models) > 50:
        return None
    result["models"] = []
    for row in models:
        if not isinstance(row, dict) or not _account_text(row.get("model")):
            return None
        rates = row.get("microusd_per_million")
        if not isinstance(rates, dict):
            return None
        parsed: dict[str, int] = {}
        for key in ("input", "cached_input", "cache_write", "output"):
            number = _account_int(rates.get(key))
            if number is False or number is None or number < 0:
                return None
            parsed[key] = number
        cap = _account_int(row.get("max_input_tokens"), allow_none=True)
        if cap is False or (cap is not None and cap < 0):
            return None
        result["models"].append(
            {
                "model": _account_text(row["model"]),
                "price_tier": _account_text(row.get("price_tier")),
                "max_input_tokens": cap,
                "microusd_per_million": parsed,
                **hosted_model_capabilities(str(row["model"])),
            }
        )
        # Older servers omit conversion metadata. Preserve that distinction:
        # rates must never silently claim a 1x conversion for a premium model.
        multiplier = row.get("credit_conversion_multiplier", row.get("multiplier"))
        if type(row.get("available")) is bool:
            result["models"][-1]["available"] = row["available"]
            result["models"][-1]["availability_reason"] = _account_text(
                row.get("availability_reason")
            )
        if type(multiplier) in (int, float) and math.isfinite(multiplier) and multiplier > 0:
            result["models"][-1]["credit_conversion_multiplier"] = multiplier
        value_multiplier = row.get("value_multiplier")
        allowance = _account_int(row.get("usage_allowance_microusd"), allow_none=True)
        if (
            row.get("shared_allowance") is True
            and type(value_multiplier) in (int, float)
            and math.isfinite(value_multiplier)
            and value_multiplier > 0
            and allowance is not False
            and allowance is not None
            and allowance >= 0
        ):
            result["models"][-1].update(
                value_multiplier=value_multiplier,
                usage_allowance_microusd=allowance,
                shared_allowance=True,
            )
    return result


def _account_billing(value: object) -> dict[str, Any] | None:
    """Validate the website's numerical contract without inventing missing balances."""
    if not isinstance(value, dict):
        return None
    result: dict[str, Any] = {}
    for field in ("status", "tier"):
        result[field] = _account_text(value.get(field))
        if result[field] is None:
            return None
    for field in (
        "balance_microusd",
        "month_burn_microusd",
        "cycle_burn_microusd",
        "burn_5h_microusd",
        "burn_7d_microusd",
    ):
        number = value.get(field)
        if type(number) not in (int, float) or not math.isfinite(number):
            return None
        if number != int(number) or abs(number) > 2**53 - 1:
            return None
        if field != "balance_microusd" and number < 0:
            return None
        result[field] = int(number)
    for field in ("cap_5h_microusd", "cap_7d_microusd", "free_allowance_microusd"):
        if field not in value:
            return None
        number = value.get(field)
        if number is not None and (
            type(number) not in (int, float)
            or not math.isfinite(number)
            or number < 0
            or number > 2**53 - 1
            or number != int(number)
        ):
            return None
        result[field] = number
    for field in ("unlimited_credits", "free_granted_period", "cancel_at_period_end"):
        if not isinstance(value.get(field), bool):
            return None
        result[field] = value[field]
    if result["free_allowance_microusd"] is None and not result["unlimited_credits"]:
        return None
    for field in ("free_next_refill_at", "current_period_end"):
        result[field] = _account_text(value.get(field))
    # Paid plans (server 2026-10-21+): keys appear only when the server sends them,
    # so the Free contract stays byte-for-byte what older clients and tests expect.
    if "credits_reset_at" in value:
        result["credits_reset_at"] = _account_text(value.get("credits_reset_at"))
    paid = _account_paid(value.get("paid"))
    if paid is not None:
        result["paid"] = paid
    promotions = value.get("model_promotions")
    if not isinstance(promotions, list) or len(promotions) > 100:
        return None
    result["model_promotions"] = []
    for row in promotions:
        if not isinstance(row, dict):
            return None
        multiplier = row.get("multiplier")
        if (
            not _account_text(row.get("model"))
            or not _account_text(row.get("label"))
            or type(multiplier) not in (int, float)
            or not math.isfinite(multiplier)
            or multiplier < 1
            or multiplier > 2**53 - 1
        ):
            return None
        result["model_promotions"].append(
            {
                "model": _account_text(row["model"]),
                "label": _account_text(row["label"]),
                "multiplier": multiplier,
            }
        )
    return result


def fetch_trial_status(cfg: AppConfig) -> TrialStatus | None:
    """Read live billing for the saved Alysis account, regardless of active provider.

    The historical name remains for CLI callers. Offline, revoked, undeployed and
    malformed responses return None; the stored sign-in itself is left untouched.
    """
    key = load_persisted_profile_keys().get(cloud.PROFILE_KEY)
    if not key:
        return None
    try:
        request = urllib.request.Request(
            cloud.account_status_url(),
            method="GET",
            headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
        )
        with urllib.request.build_opener(_AccountNoRedirect()).open(request, timeout=8) as response:
            raw = response.read(65_537)
        if len(raw) > 65_536:
            return None
        payload = json.loads(raw)
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            return None
        billing = _account_billing(payload.get("billing"))
        if billing is None:
            return None
        return TrialStatus(
            plan=billing["tier"],
            email=_account_text(payload.get("email")),
            trial_ends_at=None,
            tokens_total=None,
            tokens_used=None,
            tokens_remaining=None,
            display_name=_account_text(payload.get("display_name")),
            billing=billing,
        )
    except (
        cloud.AlysisCloudConfigError,
        urllib.error.URLError,
        TimeoutError,
        OSError,
        ValueError,
        UnicodeDecodeError,
    ):
        return None


def list_trial_models(cfg: AppConfig) -> list[str]:
    """List the model ids the gateway currently serves, via ``/v1/models``.

    The gateway exposes its allowlist as an OpenAI-shaped
    ``{"data": [{"id": …}]}``. Surfacing it lets `/config` show the live model
    list instead of pinning static ids. Best-effort: returns ``[]`` on any
    failure (offline, non-200, malformed) so callers fall back to the static
    preset models. The stored gateway key is sent when present (the gateway may
    require it) and never required for the call to be attempted.
    """
    try:
        url = cloud.models_url()
    except cloud.AlysisCloudConfigError:
        return []
    headers = {}
    access_key = load_persisted_profile_keys().get(cloud.PROFILE_KEY)
    if access_key:
        headers["Authorization"] = f"Bearer {access_key}"
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=_MODELS_TIMEOUT_S) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, UnicodeDecodeError):
        return []
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list):
        return []
    models: list[str] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or "").strip()
        if model_id and model_id not in models:
            models.append(model_id)
    return models


def billing_with_model_availability(
    cfg: AppConfig, billing: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Intersect a new paid account catalog with the authenticated gateway catalog.

    Membership, balances and rates remain unchanged. A failed availability read
    closes model selection until retry rather than advertising an unverified route.
    """
    if not billing or not isinstance(billing.get("paid"), dict):
        return billing
    paid = billing["paid"]
    if paid.get("credit_unit_version") != "paid-base-display-v1":
        return billing
    ids: set[str] | None = None
    try:
        key = load_persisted_profile_keys().get(cloud.PROFILE_KEY)
        if key:
            request = urllib.request.Request(
                cloud.models_url(),
                method="GET",
                headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
            )
            with urllib.request.build_opener(_AccountNoRedirect()).open(
                request, timeout=_MODELS_TIMEOUT_S
            ) as response:
                raw = response.read(65_537)
            if len(raw) <= 65_536:
                body = json.loads(raw)
                rows = body.get("data") if isinstance(body, dict) else None
                if (
                    isinstance(rows, list)
                    and len(rows) <= 100
                    and all(isinstance(row, dict) and _account_text(row.get("id")) for row in rows)
                ):
                    ids = {str(row["id"]).strip() for row in rows}
    except (
        cloud.AlysisCloudConfigError,
        urllib.error.URLError,
        TimeoutError,
        OSError,
        ValueError,
        UnicodeDecodeError,
    ):
        pass
    models = []
    for model in paid["models"]:
        current = dict(model)
        if current.get("available") is not False:
            current["available"] = ids is not None and current["model"] in ids
            current["availability_reason"] = (
                None
                if current["available"]
                else (
                    "Model availability could not be checked. Retry shortly."
                    if ids is None
                    else "Hosted access is not available yet."
                )
            )
        models.append(current)
    return {**billing, "paid": {**paid, "models": models}}


def format_trial_status_line(status: TrialStatus) -> str | None:
    """A one-line account summary for `whoami`, or None if there's nothing to show."""
    if status.billing is not None:
        billing = status.billing
        paid = billing.get("paid")
        if paid is not None and not billing["unlimited_credits"]:
            unit = paid.get("ledger_microusd_per_base_credit", 10_000)
            line = f"{paid['plan_name']}: {max(0, billing['balance_microusd']) / unit:,.1f} credits left"
            reset = _format_date(paid.get("credits_reset_at"))
            return line + (f" · resets {reset}" if reset else "")
        if billing["unlimited_credits"]:
            return f"{status.plan or 'Account'}: unlimited credits"
        balance = billing["balance_microusd"]
        if billing["tier"] == "free" and not billing["free_granted_period"]:
            balance = billing["free_allowance_microusd"]
        return f"{status.plan or 'Account'}: {max(0, balance) / 10_000:,.1f} credits left"
    parts: list[str] = []
    days = _days_left(status.trial_ends_at)
    if days is not None:
        parts.append("expired" if days <= 0 else f"{days} day{'s' if days != 1 else ''} left")
    ends = _format_date(status.trial_ends_at)
    if ends:
        parts.append(f"ends {ends}")
    tokens = _format_token_usage(status.tokens_used, status.tokens_total)
    if tokens:
        parts.append(tokens)
    if not parts:
        return None
    label = (status.plan or "Account").capitalize()
    return f"{label}: " + " · ".join(parts)


def format_paid_plan_details(status: TrialStatus) -> list[str]:
    """Extra `whoami` lines for a paid plan: windows, Premium use and model rates."""
    from .model_reasoning import hosted_model_capabilities

    billing = status.billing or {}
    paid = billing.get("paid")
    if not paid:
        return []

    credit_unit = paid.get("ledger_microusd_per_base_credit", 10_000)

    def credits(microusd: int | None) -> str:
        value = (microusd or 0) / credit_unit
        return f"{value:,.1f}".removesuffix(".0") if credit_unit != 10_000 else f"{value:,.0f}"

    def usage_credits(microusd: int) -> str:
        return f"{microusd / 10_000:,.0f}"

    lines = [
        f"Last 5 hours: {credits(paid['burn_5h_microusd'])} of {credits(paid['cap_5h_microusd'])} credits"
        f" · last 24 hours: {credits(paid['burn_day_microusd'])} of {credits(paid['cap_day_microusd'])}",
    ]
    if paid.get("premium_month_limit_microusd"):
        lines.append(
            f"Premium models this month: {credits(paid.get('premium_month_used_microusd'))}"
            f" of {credits(paid['premium_month_limit_microusd'])} credits"
            + (
                f" · last 24 hours: {credits(paid.get('premium_day_used_microusd'))}"
                f" of {credits(paid['premium_day_limit_microusd'])}"
                if paid.get("premium_day_limit_microusd")
                else ""
            )
        )
    lines.extend(
        [
            f"{credits(paid['monthly_credits_microusd'])} monthly shared credits"
            f" · {len(paid['models'])} included models",
            "Switching models uses the same credit balance.",
        ]
    )
    if (
        paid.get("max_usage_value_microusd", 0) / 10_000
        > paid["monthly_credits_microusd"] / credit_unit
    ):
        lines.append(
            f"Up to {usage_credits(paid['max_usage_value_microusd'])} usage credits, depending on the model."
        )
    lines.append("Rates, credits per 1M tokens (input / cached / output):")
    for row in paid["models"]:
        rates = row["microusd_per_million"]
        lines.append(
            f"  {row['model']}: {rates['input'] / credit_unit:g} / {rates['cached_input'] / credit_unit:g}"
            f" / {rates['output'] / credit_unit:g}"
            + (
                f" · up to {row['max_input_tokens']:,} input tokens"
                if row.get("max_input_tokens")
                else ""
            )
        )
        capabilities = hosted_model_capabilities(row["model"])
        efforts = capabilities["reasoning_efforts"]
        choices = ", ".join(item["label"] for item in efforts)
        lines.append(f"    Effort: Auto{', ' + choices if choices else ' (provider default)'}")
        default = capabilities["default_reasoning_effort"]
        metadata = []
        if row.get("available") is False:
            metadata.append(
                "Unavailable"
                + (f": {row['availability_reason']}" if row.get("availability_reason") else "")
            )
        if default:
            label = next(item["label"] for item in efforts if item["id"] == default)
            metadata.append(f"Default: {label}")
        multiplier = row.get("credit_conversion_multiplier")
        if multiplier is not None:
            metadata.append(f"Credit rate: {multiplier:.3g}×")
        if row.get("shared_allowance") is True:
            metadata.append(
                f"Model value: {row['value_multiplier']:.3g}×"
                f" · up to {usage_credits(row['usage_allowance_microusd'])} usage credits"
            )
        if metadata:
            lines.append("    " + " · ".join(metadata))
    if paid.get("upgrade_to"):
        lines.append("Need more? Run `alysis upgrade` to review available plans.")
    return lines


# ---------------------------------------------------------------------------
# Device flow internals
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _DeviceGrant:
    device_code: str
    user_code: str
    verification_url: str
    interval_s: float
    expires_in_s: float


def _function_headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    public_key = cloud.public_client_key()
    if public_key:
        headers["apikey"] = public_key
        headers["Authorization"] = f"Bearer {public_key}"
    return headers


def _post_json(url: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=_function_headers(),
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_S) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise AlysisLoginError(_http_error_message(exc)) from exc
    except urllib.error.URLError as exc:
        raise AlysisLoginError(
            f"Could not reach the Alysis Code login service: {exc.reason}"
        ) from exc
    except (ValueError, UnicodeDecodeError) as exc:
        raise AlysisLoginError("Login service returned an invalid response.") from exc
    except (TimeoutError, OSError) as exc:
        raise AlysisLoginError(f"Login request failed: {exc}") from exc
    if not isinstance(body, dict):
        raise AlysisLoginError("Login service returned an invalid response.")
    return body


def _http_error_message(exc: urllib.error.HTTPError) -> str:
    try:
        detail = json.loads(exc.read().decode("utf-8"))
        message = detail.get("error")
        if isinstance(message, dict):
            message = message.get("message")
        if message:
            return str(message)
    except Exception:  # noqa: BLE001 - best-effort error extraction
        pass
    return f"Login request failed (HTTP {exc.code})."


def _approval_url(server_url: str, *, user_code: str) -> str:
    """Build the /activate URL to open, pinning the host to the configured site.

    The device endpoint returns its own ``verification_url`` /
    ``verification_url_complete``. Opening that verbatim lets the *server*
    decide which website the CLI sends the user to — which is how a deployment
    still carrying the retired product host kept
    launching the retired site long after the client had been rebranded, and
    which would equally let a compromised or misconfigured backend point
    sign-in anywhere. Only the path and query are taken from the response; the
    origin always comes from ``alysis_cloud.site_url()`` (env-overridable via
    ALYSIS_SITE_URL for staging/local stubs), so the host the user lands on is
    the one this client trusts.
    """
    base = cloud.site_url()
    path = "/activate"
    query = f"code={user_code}" if user_code else ""

    if server_url:
        try:
            parts = urlsplit(server_url)
        except ValueError:
            parts = None
        if parts is not None:
            if parts.path:
                path = parts.path if parts.path.startswith("/") else f"/{parts.path}"
            if parts.query:
                query = parts.query

    return f"{base}{path}?{query}" if query else f"{base}{path}"


def _request_device_code() -> _DeviceGrant:
    client_name = f"alysis-cli @ {platform.node() or 'unknown-host'}"[:80]
    try:
        installation_id = get_installation_id()
    except (OSError, ValueError) as exc:
        raise AlysisLoginError(
            "Could not read or save the CLI installation identity. Check your Alysis data directory."
        ) from exc
    body = _post_json(
        cloud.device_code_url(),
        {"client_name": client_name, "installation_id": installation_id},
    )

    device_code = str(body.get("device_code") or "").strip()
    user_code = str(body.get("user_code") or "").strip()
    if not device_code or not user_code:
        raise AlysisLoginError("Login service did not return a device code.")

    verification_url = _approval_url(
        str(body.get("verification_url_complete") or body.get("verification_url") or "").strip(),
        user_code=user_code,
    )

    try:
        interval_s = max(float(body.get("interval", _DEFAULT_POLL_INTERVAL_S)), 0.0)
    except (TypeError, ValueError):
        interval_s = _DEFAULT_POLL_INTERVAL_S
    try:
        expires_in_s = max(float(body.get("expires_in", _DEFAULT_TIMEOUT_S)), 1.0)
    except (TypeError, ValueError):
        expires_in_s = _DEFAULT_TIMEOUT_S

    return _DeviceGrant(
        device_code=device_code,
        user_code=user_code,
        verification_url=verification_url,
        interval_s=interval_s,
        expires_in_s=expires_in_s,
    )


def _poll_for_key(grant: _DeviceGrant, *, timeout_s: float) -> str:
    deadline = time.monotonic() + max(1.0, min(timeout_s, grant.expires_in_s))
    while True:
        body = _post_json(cloud.device_token_url(), {"device_code": grant.device_code})
        status = str(body.get("status") or "").strip().lower()

        if status == "approved":
            key = str(body.get("key") or "").strip()
            if not key:
                raise AlysisLoginError("Login service did not return a key.")
            return key
        if status == "denied":
            raise AlysisLoginError("Login was rejected on the website.")
        if status in {"expired", "not_found", "already_claimed"}:
            raise AlysisLoginError("This login code expired. Run `alysis login` again.")
        # status == "pending" (or anything unknown): keep waiting.

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AlysisLoginError(
                f"Timed out after {int(timeout_s)}s waiting for approval. Run `alysis login` again."
            )
        time.sleep(min(grant.interval_s, max(remaining, 0.0)) if grant.interval_s else 0.0)


# ---------------------------------------------------------------------------
# Profile + formatting internals
# ---------------------------------------------------------------------------
def _str_or_none(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _int_or_none(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _days_left(ends_at: str | None) -> int | None:
    ends = _parse_iso(ends_at)
    if ends is None:
        return None
    seconds = (ends - datetime.now(UTC)).total_seconds()
    return 0 if seconds <= 0 else math.ceil(seconds / 86400)


def _format_date(value: str | None) -> str | None:
    dt = _parse_iso(value)
    return dt.date().isoformat() if dt is not None else None


def _format_token_usage(used: int | None, total: int | None) -> str | None:
    if used is None and total is None:
        return None
    if total:
        return f"{used or 0:,} / {total:,} tokens used"
    return f"{used or 0:,} tokens used"


def _activate_alysis_profile(cfg: AppConfig, *, email: str | None) -> LoginResult:
    """Create + activate the `alysis` profile, keeping any model the user chose.

    Fresh logins default to GPT-6 Luna (``gpt-6-luna``, free credits) so
    connect → login → chat works with zero extra steps. Re-logins preserve
    whatever model the user selected since, so logging in again never undoes
    supported choices. Legacy hosted DeepSeek ids (the Free default before Oct 22,
    2026) are remapped to `glm-5.3-flash` via the preset's model_aliases, including
    on re-login; paid plans offer DeepSeek as `deepseek-v4.1-flash`.
    """
    preset = get_preset(cloud.PROFILE_KEY)
    if preset is not None:
        profile = make_profile_from_preset(preset, name=cloud.PROFILE_KEY)
    else:  # pragma: no cover - preset is always present
        profile = ProfileSpec(name=cloud.PROFILE_KEY, protocol="openai_compat")

    existing = get_profile(cfg, cloud.PROFILE_KEY)
    existing_model = str(getattr(existing, "default_model", "") or "").strip() if existing else ""
    chosen_model = existing_model or _FRESH_LOGIN_DEFAULT_MODEL
    if preset is not None:
        chosen_model = canonical_model_alias_for_preset(preset, chosen_model)

    # Always pin to the live gateway URL (env-overridable for tests); keep the
    # user's chosen model.
    profile = ProfileSpec(
        name=profile.name,
        protocol=profile.protocol,
        base_url=cloud.gateway_base_url(),
        api_key_env=None,
        extra_headers=dict(profile.extra_headers),
        default_model=chosen_model,
        reasoning_effort=(existing.reasoning_effort if existing is not None else None),
        reasoning_trace_adapter=(
            existing.reasoning_trace_adapter
            if existing is not None
            else profile.reasoning_trace_adapter
        ),
        web_search_adapter=profile.web_search_adapter,
        web_search_model=profile.web_search_model,
        notes=profile.notes,
    )

    add_profile(cfg, profile)
    set_active_profile(cfg, profile.name)
    # Hosted account access uses the native agent loop. Keep configured
    # delegated runtimes available, but stop routing this session through one.
    cfg.execution.backend = "native"
    cfg.execution.runtime = None
    save_config(cfg)

    return LoginResult(
        email=email,
        profile_name=profile.name,
        base_url=profile.base_url,
        model=profile.default_model,
    )


def _open_browser(url: str) -> bool:
    return open_url(url)


def _safe_open(opener: Callable[[str], bool], url: str) -> bool:
    try:
        return bool(opener(url))
    except Exception:  # noqa: BLE001 - browser launch is best-effort
        return False
