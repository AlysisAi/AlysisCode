"""Sampling determinism controls and provider-response provenance.

Two production incidents motivate this module, and both were *unprovable* after
the fact rather than merely unfixed.

*Silent model drift.* Three Terminal-Bench trials of one pinned build under one
pinned config disagreed on 30 of 89 tasks. Two SWE-bench runs of a
byte-identical build scored 80 and then 71 four days apart, with the delegation
pattern changing beyond recognition (one subagent went from 128 invocations to
zero). Server-side drift was the obvious suspect and stayed a suspicion,
because nothing in the run recorded which model actually answered. The response
``model`` field, the ``system_fingerprint`` and a small set of routing headers
are the only evidence a client can collect without the provider's cooperation,
so this module selects them and hands them to provider telemetry.

*Unpinned sampling.* The client pinned no seed, requested no ``top_p``, and let
``temperature`` follow whichever code path built the request, so two runs of one
build were free to sample differently from an identical prompt. The three
settings here are opt-in precisely because switching them on changes the wire
request: when none is configured the payload is left exactly as the transport
built it. :func:`apply_sampling_to_payload` guarantees that by construction --
it is the single place any of the three fields can enter a payload -- and
``tests/test_sampling_config.py`` asserts it byte-for-byte.

Stdlib only, no package imports: the transport, the telemetry recorder and the
session bootstrap all pull this in, and the tests load it straight from this
file path in a bare interpreter.

Redaction boundary
------------------
This module *shapes* values, it never persists them. Every caller writes
through a sink that funnels its serialized line through
``logging_redaction.redact_log_text`` immediately before the write
(``SessionStore.append`` and ``provider_telemetry._append_to_sink``), and
``provider_telemetry`` redacts each selected header value a second time before
it reaches the in-memory history. Independently of redaction, the header
allowlist here is bounded by a deny list that no user-supplied allowlist can
widen, so a credential-bearing header cannot be selected in the first place.
"""

from __future__ import annotations

import fnmatch
import math
import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Sampling determinism controls
# ---------------------------------------------------------------------------

SAMPLING_TEMPERATURE_ENV = "ALYSIS_SAMPLING_TEMPERATURE"
SAMPLING_TOP_P_ENV = "ALYSIS_SAMPLING_TOP_P"
SAMPLING_SEED_ENV = "ALYSIS_SAMPLING_SEED"

SAMPLING_TEMPERATURE_CONFIG_KEY = "sampling_temperature"
SAMPLING_TOP_P_CONFIG_KEY = "sampling_top_p"
SAMPLING_SEED_CONFIG_KEY = "sampling_seed"

#: Accepted ranges. Deliberately the widest range every OpenAI-compatible
#: endpoint we target documents, so a legitimate value is never dropped; a
#: provider that is stricter still rejects it on the wire, which is visible.
TEMPERATURE_RANGE = (0.0, 2.0)
TOP_P_RANGE = (0.0, 1.0)
#: Signed 64-bit, the widest seed any of these endpoints accepts.
SEED_RANGE = (-(2**63), 2**63 - 1)

#: Longest raw value echoed back in a warning event. A misconfigured variable
#: is sometimes a pasted credential, and while the sink redacts on write, a
#: bounded echo keeps the blast radius small even before that.
MAX_WARNING_VALUE_CHARS = 40

_SAMPLING_SETTING_NAMES = ("temperature", "top_p", "seed")


@dataclass(frozen=True)
class SamplingWarning:
    """One sampling value that was rejected and ignored.

    An invalid sampling setting must never abort a run: an operator typo in a
    benchmark harness would otherwise take down the whole campaign. The value
    is dropped, the default behavior stands, and this record explains why.
    """

    setting: str
    source: str
    reason: str
    raw: str

    def message(self) -> str:
        return (
            f"ignoring {self.source}={self.raw!r}: {self.reason}; "
            f"sampling {self.setting} is left unset"
        )

    def payload(self) -> dict[str, str]:
        return {
            "setting": self.setting,
            "source": self.source,
            "reason": self.reason,
            "raw": self.raw,
        }


@dataclass(frozen=True)
class SamplingSettings:
    """The effective sampling overrides for a run.

    ``None`` means "not configured", which is not the same as "configured to
    the provider default": an unconfigured setting is never written to a
    request at all.
    """

    temperature: float | None = None
    top_p: float | None = None
    seed: int | None = None
    sources: Mapping[str, str] = field(default_factory=dict)
    warnings: tuple[SamplingWarning, ...] = ()

    @property
    def is_configured(self) -> bool:
        return self.temperature is not None or self.top_p is not None or self.seed is not None

    def telemetry_payload(self) -> dict[str, Any]:
        """Per-request sampling record. Present on every call, configured or not.

        The absence of sampling controls is itself the finding when two runs
        of one build diverge, so this never collapses to ``None``.
        """
        return {
            "configured": self.is_configured,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "seed": self.seed,
            "sources": {str(k): str(v) for k, v in sorted(dict(self.sources).items())},
        }

    def session_event_payload(self) -> dict[str, Any]:
        """Once-per-run record, including anything that was rejected."""
        payload = self.telemetry_payload()
        payload["warnings"] = [warning.payload() for warning in self.warnings]
        payload["warning_count"] = len(self.warnings)
        return payload


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "..."


def _parse_bounded_float(
    raw: str,
    *,
    setting: str,
    source: str,
    low: float,
    high: float,
) -> tuple[float | None, SamplingWarning | None]:
    echo = _truncate(raw, MAX_WARNING_VALUE_CHARS)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None, SamplingWarning(setting, source, "not a number", echo)
    if not math.isfinite(value):
        return None, SamplingWarning(setting, source, "not finite", echo)
    if value < low or value > high:
        return None, SamplingWarning(
            setting,
            source,
            f"outside the accepted range [{low}, {high}]",
            echo,
        )
    return value, None


def _parse_bounded_int(
    raw: str,
    *,
    setting: str,
    source: str,
    low: int,
    high: int,
) -> tuple[int | None, SamplingWarning | None]:
    echo = _truncate(raw, MAX_WARNING_VALUE_CHARS)
    try:
        value = int(raw, 10) if isinstance(raw, str) else int(raw)
    except (TypeError, ValueError):
        return None, SamplingWarning(setting, source, "not an integer", echo)
    if value < low or value > high:
        return None, SamplingWarning(
            setting,
            source,
            "outside the accepted 64-bit range",
            echo,
        )
    return value, None


def _pick_source(
    *,
    env_name: str,
    config_key: str,
    environ: Mapping[str, str],
    config_values: Mapping[str, Any],
) -> tuple[str, str] | None:
    """Return ``(raw_value, source_label)``, env winning over config.

    Matches the house precedence used everywhere else in the CLI: an
    environment variable is how a benchmark harness pins a run, and it must
    beat whatever happens to be in the operator's ``config.json``.
    """
    resolved_env_name = env_name
    raw_env = _clean_text(environ.get(env_name))
    if raw_env:
        return raw_env, f"env:{resolved_env_name}"
    if config_key in config_values:
        raw_config = _clean_text(config_values.get(config_key))
        if raw_config:
            return raw_config, f"config:{config_key}"
    return None


def resolve_sampling_settings(
    *,
    config_values: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
) -> SamplingSettings:
    """Resolve the three sampling controls from env, then config.

    Every failure mode -- unparseable, non-finite, out of range -- resolves to
    "not configured" plus a warning. Nothing here can raise.
    """
    source_env: Mapping[str, str] = os.environ if environ is None else environ
    values: Mapping[str, Any] = {} if config_values is None else config_values

    resolved: dict[str, Any] = {}
    sources: dict[str, str] = {}
    warnings: list[SamplingWarning] = []

    plan = (
        ("temperature", SAMPLING_TEMPERATURE_ENV, SAMPLING_TEMPERATURE_CONFIG_KEY),
        ("top_p", SAMPLING_TOP_P_ENV, SAMPLING_TOP_P_CONFIG_KEY),
        ("seed", SAMPLING_SEED_ENV, SAMPLING_SEED_CONFIG_KEY),
    )
    for setting, env_name, config_key in plan:
        picked = _pick_source(
            env_name=env_name,
            config_key=config_key,
            environ=source_env,
            config_values=values,
        )
        if picked is None:
            continue
        raw, source = picked
        if setting == "seed":
            value, warning = _parse_bounded_int(
                raw,
                setting=setting,
                source=source,
                low=SEED_RANGE[0],
                high=SEED_RANGE[1],
            )
        else:
            low, high = TEMPERATURE_RANGE if setting == "temperature" else TOP_P_RANGE
            value, warning = _parse_bounded_float(
                raw,
                setting=setting,
                source=source,
                low=low,
                high=high,
            )
        if warning is not None:
            warnings.append(warning)
            continue
        resolved[setting] = value
        sources[setting] = source

    return SamplingSettings(
        temperature=resolved.get("temperature"),
        top_p=resolved.get("top_p"),
        seed=resolved.get("seed"),
        sources=sources,
        warnings=tuple(warnings),
    )


def apply_sampling_to_payload(
    payload: dict[str, Any],
    settings: SamplingSettings,
    *,
    allow_temperature_override: bool = True,
) -> tuple[str, ...]:
    """Write the configured sampling fields into an already-built payload.

    This is the *only* place any of the three fields may enter a
    chat-completions request, which is what makes the "unset means unchanged"
    guarantee checkable rather than aspirational: with an unconfigured
    ``settings`` this returns ``()`` and leaves ``payload`` untouched -- same
    object, same keys, same insertion order, hence the same serialized bytes
    as the pre-PR6 transport produced.

    ``temperature`` is only *overridden*, never introduced. When the transport
    deliberately omitted it (a documented model policy, or a cached provider
    rejection) that omission is load-bearing, and re-adding the field would
    reintroduce the 400 the omission exists to avoid. ``allow_temperature_override``
    lets the caller additionally decline when it has already rewritten the
    value for provider-compatibility reasons.

    Returns the field names actually written, in insertion order.
    """
    applied: list[str] = []
    override_temperature = (
        settings.temperature is not None and allow_temperature_override and "temperature" in payload
    )
    if override_temperature:
        payload["temperature"] = settings.temperature
        applied.append("temperature")
    if settings.top_p is not None:
        payload["top_p"] = settings.top_p
        applied.append("top_p")
    if settings.seed is not None:
        payload["seed"] = settings.seed
        applied.append("seed")
    return tuple(applied)


# The process-wide active settings. The transport is several call layers below
# the place that knows the effective config, and threading a settings object
# through every constructor would be the refactor this change is meant to
# avoid; this mirrors the existing process-wide provider-telemetry sink.
_ACTIVE_LOCK = threading.Lock()
_ACTIVE_SETTINGS: SamplingSettings | None = None


def set_active_sampling_settings(settings: SamplingSettings | None) -> None:
    """Install the run's resolved sampling settings (``None`` restores env-only)."""
    global _ACTIVE_SETTINGS
    with _ACTIVE_LOCK:
        _ACTIVE_SETTINGS = settings


def active_sampling_settings() -> SamplingSettings:
    """Return the installed settings, falling back to an env-only resolution.

    The fallback matters for every entry point that reaches the transport
    without building a session (``config`` subcommands, doctors, probes): those
    still honor the environment, and still report "not configured" when the
    environment is empty.
    """
    global _ACTIVE_SETTINGS
    with _ACTIVE_LOCK:
        if _ACTIVE_SETTINGS is None:
            _ACTIVE_SETTINGS = resolve_sampling_settings()
        return _ACTIVE_SETTINGS


def reset_active_sampling_settings_for_tests() -> None:
    global _ACTIVE_SETTINGS
    with _ACTIVE_LOCK:
        _ACTIVE_SETTINGS = None


# ---------------------------------------------------------------------------
# Response fingerprint capture
# ---------------------------------------------------------------------------

#: Headers worth keeping by name. Each one has, at some point, been the only
#: thing distinguishing two responses that claimed the same model.
DEFAULT_RESPONSE_HEADER_NAMES = (
    "openai-organization",
    "server",
    "via",
    "x-model-version",
    "x-request-id",
    "x-served-by",
)
#: Plus anything a custom endpoint chose to call a model or a version. Custom
#: OpenAI-compatible gateways name these fields freely; the MiMo endpoint that
#: exposed the drift is exactly such a gateway.
DEFAULT_RESPONSE_HEADER_PATTERNS = ("*model*", "*version*")

RESPONSE_HEADER_ALLOWLIST_ENV = "ALYSIS_RESPONSE_HEADER_ALLOWLIST"

#: Never captured, whatever the allowlist says. An operator who sets the
#: allowlist to ``*`` is asking for provenance, not for their bearer token in
#: a JSONL file, and PR1 exists because that exact class of mistake already
#: put a live credential on disk once.
RESPONSE_HEADER_DENY_PATTERNS = (
    "*auth*",
    "*cookie*",
    "*credential*",
    "*key*",
    "*password*",
    "*secret*",
    "*token*",
)

MAX_RESPONSE_HEADER_VALUE_CHARS = 200
MAX_RESPONSE_HEADERS = 24

_ALLOWLIST_DISABLED_WORDS = frozenset({"none", "off", "-"})
_GLOB_CHARS = ("*", "?", "[")


def _normalize_header_name(name: Any) -> str:
    return _clean_text(name).casefold()


def header_name_is_denied(name: str) -> bool:
    """True when a header may never be captured, allowlist notwithstanding."""
    normalized = _normalize_header_name(name)
    if not normalized:
        return True
    return any(
        fnmatch.fnmatchcase(normalized, pattern) for pattern in RESPONSE_HEADER_DENY_PATTERNS
    )


@dataclass(frozen=True)
class ResponseHeaderAllowlist:
    """Exact names plus glob patterns, matched case-insensitively."""

    names: frozenset[str] = frozenset()
    patterns: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.names and not self.patterns

    def matches(self, header_name: str) -> bool:
        normalized = _normalize_header_name(header_name)
        if not normalized or header_name_is_denied(normalized):
            return False
        if normalized in self.names:
            return True
        return any(fnmatch.fnmatchcase(normalized, pattern) for pattern in self.patterns)

    def describe(self) -> dict[str, Any]:
        return {
            "names": sorted(self.names),
            "patterns": list(self.patterns),
        }


DEFAULT_RESPONSE_HEADER_ALLOWLIST = ResponseHeaderAllowlist(
    names=frozenset(DEFAULT_RESPONSE_HEADER_NAMES),
    patterns=DEFAULT_RESPONSE_HEADER_PATTERNS,
)


def parse_response_header_allowlist(raw: str | None) -> ResponseHeaderAllowlist:
    """Parse a comma/whitespace-separated allowlist spec.

    Unset or blank keeps the default. The words ``none``/``off``/``-`` disable
    header capture entirely, which is the escape hatch for an operator who
    considers response headers sensitive in their deployment.
    """
    text = _clean_text(raw)
    if not text:
        return DEFAULT_RESPONSE_HEADER_ALLOWLIST
    entries = [
        _normalize_header_name(part)
        for chunk in text.split(",")
        for part in chunk.split()
        if _clean_text(part)
    ]
    entries = [entry for entry in entries if entry]
    if not entries:
        return DEFAULT_RESPONSE_HEADER_ALLOWLIST
    if len(entries) == 1 and entries[0] in _ALLOWLIST_DISABLED_WORDS:
        return ResponseHeaderAllowlist()
    names = {entry for entry in entries if not any(char in entry for char in _GLOB_CHARS)}
    patterns = tuple(
        dict.fromkeys(entry for entry in entries if any(char in entry for char in _GLOB_CHARS))
    )
    return ResponseHeaderAllowlist(names=frozenset(names), patterns=patterns)


def resolve_response_header_allowlist(
    *,
    environ: Mapping[str, str] | None = None,
) -> ResponseHeaderAllowlist:
    source: Mapping[str, str] = os.environ if environ is None else environ
    return parse_response_header_allowlist(source.get(RESPONSE_HEADER_ALLOWLIST_ENV))


def select_response_headers(
    headers: Any,
    *,
    allowlist: ResponseHeaderAllowlist | None = None,
) -> dict[str, str]:
    """Return the allowlisted response headers, lowercased, bounded and sorted.

    Accepts anything mapping-like, including ``httpx.Headers``, and tolerates a
    ``None`` or a broken object -- provenance capture must never be the reason
    a provider call fails.
    """
    active = DEFAULT_RESPONSE_HEADER_ALLOWLIST if allowlist is None else allowlist
    if active.is_empty:
        return {}
    try:
        items = list(headers.items())
    except Exception:  # noqa: BLE001 - never let capture break a provider call
        return {}
    selected: dict[str, str] = {}
    for raw_name, raw_value in items:
        name = _normalize_header_name(raw_name)
        if not active.matches(name):
            continue
        selected[name] = _truncate(_clean_text(raw_value), MAX_RESPONSE_HEADER_VALUE_CHARS)
        if len(selected) >= MAX_RESPONSE_HEADERS:
            break
    return dict(sorted(selected.items()))


def extract_system_fingerprint(raw: Any) -> str | None:
    """Pull ``system_fingerprint`` out of a parsed response body, if present."""
    if not isinstance(raw, Mapping):
        return None
    value = _clean_text(raw.get("system_fingerprint"))
    return value or None


def response_fingerprint_payload(
    *,
    response_model: str | None = None,
    system_fingerprint: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Shape the per-response provenance record for provider telemetry.

    Always returns every key. A run where ``system_fingerprint`` is
    consistently absent is a different finding from a run where it changed
    mid-flight, and only an always-present field can tell them apart.

    The header key is ``response_headers``, not ``headers``: provider
    telemetry's own redactor replaces any value under a key named ``headers``
    with ``[omitted]``, so the plain name would silently discard exactly the
    evidence this record exists to carry.
    """
    safe_headers = {str(k): str(v) for k, v in dict(headers or {}).items()}
    return {
        "response_model": _clean_text(response_model) or None,
        "system_fingerprint": _clean_text(system_fingerprint) or None,
        "response_headers": safe_headers,
        "response_header_count": len(safe_headers),
    }


def fingerprint_drift_payload(calls: Any) -> dict[str, Any]:
    """Roll per-call fingerprints up into a drift verdict for one run.

    This is the question the earlier investigations could not answer. Two runs
    of a byte-identical build scored 80 and then 71, and the hypothesis --
    that the endpoint had quietly started serving something else -- could
    neither be confirmed nor dismissed. Grouping by the model *requested*
    answers it directly: one requested model that came back under two different
    ``system_fingerprint`` values, or under two different response ``model``
    names, is drift observed rather than inferred.

    Pure over a list of already-recorded provider-call payloads, so it can be
    run against the in-memory history or against a replayed telemetry JSONL.
    """
    groups: dict[str, dict[str, Any]] = {}
    models: set[str] = set()
    fingerprints: set[str] = set()
    call_count = 0
    fingerprint_present = 0

    for call in calls or []:
        if not isinstance(call, Mapping):
            continue
        call_count += 1
        requested = _clean_text(call.get("model"))
        record = call.get("response_fingerprint")
        record = record if isinstance(record, Mapping) else {}
        response_model = _clean_text(record.get("response_model"))
        fingerprint = _clean_text(record.get("system_fingerprint"))
        if fingerprint:
            fingerprint_present += 1
            fingerprints.add(fingerprint)
        if response_model:
            models.add(response_model)
        group = groups.setdefault(
            requested,
            {"requested_model": requested, "call_count": 0, "_models": set(), "_fps": set()},
        )
        group["call_count"] += 1
        if response_model:
            group["_models"].add(response_model)
        if fingerprint:
            group["_fps"].add(fingerprint)

    by_requested_model = []
    drift = False
    for _key, group in sorted(groups.items()):
        group_models = sorted(group.pop("_models"))
        group_fps = sorted(group.pop("_fps"))
        group_drift = len(group_models) > 1 or len(group_fps) > 1
        drift = drift or group_drift
        group["response_models"] = group_models
        group["system_fingerprints"] = group_fps
        group["drift_detected"] = group_drift
        by_requested_model.append(group)

    return {
        "window_call_count": call_count,
        "response_models": sorted(models),
        "system_fingerprints": sorted(fingerprints),
        "distinct_response_model_count": len(models),
        "distinct_system_fingerprint_count": len(fingerprints),
        "system_fingerprint_present_call_count": fingerprint_present,
        # Absence is a finding too: an endpoint that never sends a fingerprint
        # cannot be monitored this way, and saying so beats an empty field that
        # reads like "no drift".
        "system_fingerprint_absent_call_count": max(0, call_count - fingerprint_present),
        "drift_detected": drift,
        "by_requested_model": by_requested_model,
    }


# ---------------------------------------------------------------------------
# Effective-configuration snapshot
# ---------------------------------------------------------------------------

CONFIG_SNAPSHOT_EVENT = "config_snapshot"
# Version 2 adds the ``effective`` section: values the session will actually
# enforce, resolved through their full precedence chain (CLI/env/config/
# default), each paired with the source that won. Version 1 recorded only the
# raw config dump, and a reader who took ``config.llm_timeout_s`` as the
# enforced timeout got the unset field default while an environment override
# silently governed the run -- an 89-task campaign was misdiagnosed exactly
# that way. The raw dump stays, unchanged, for drift comparison; the
# ``effective`` section answers the question readers were actually asking.
CONFIG_SNAPSHOT_SCHEMA_VERSION = 2

#: Config/env key names whose *value* is masked structurally, before any
#: text-level redaction. Exact names first so that ordinary settings which
#: merely contain a scary substring -- ``max_tokens``, ``reasoning_tokens`` --
#: survive intact and stay analyzable.
_SECRET_EXACT_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "bearer_token",
        "cookie",
        "credential",
        "id_token",
        "passwd",
        "password",
        "secret",
        "token",
    }
)
_SECRET_KEY_FRAGMENTS = (
    "access_token",
    "api_key",
    "apikey",
    "auth_token",
    "client_secret",
    "credential",
    "passwd",
    "password",
    "private_key",
    "refresh_token",
    "secret",
)

MASKED_VALUE = "[secret]"

#: Only Alysis Code's own variables are snapshotted. The full environment is
#: both enormous and the exact thing that leaked a credential into a session
#: log before PR1 existed.
CONFIG_SNAPSHOT_ENV_PREFIX = "ALYSIS_"
MAX_SNAPSHOT_ENV_VARS = 200
MAX_SNAPSHOT_ENV_VALUE_CHARS = 300


def _key_is_secret(key: Any) -> bool:
    normalized = _clean_text(key).casefold().replace("-", "_")
    if not normalized:
        return False
    if normalized in _SECRET_EXACT_KEYS:
        return True
    return any(fragment in normalized for fragment in _SECRET_KEY_FRAGMENTS)


def scrub_config_values(value: Any, *, key: Any = None) -> Any:
    """Recursively mask secret-named entries in an already-dumped config tree.

    Structural masking, deliberately independent of the write-path text
    redactor: a value that never looked credential-shaped -- a short shared
    token, a passphrase of dictionary words -- is caught by its *key* here even
    though no entropy heuristic would flag it.
    """
    if key is not None and _key_is_secret(key):
        return MASKED_VALUE
    if isinstance(value, Mapping):
        return {str(k): scrub_config_values(v, key=k) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub_config_values(item) for item in value]
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    if isinstance(value, str):
        return value
    return str(value)


def snapshot_environment(
    *,
    environ: Mapping[str, str] | None = None,
    prefix: str = CONFIG_SNAPSHOT_ENV_PREFIX,
) -> dict[str, str]:
    """Capture the Alysis Code environment variables that are actually set.

    A benchmark run configures almost everything by environment, so the file on
    disk is not the effective configuration; without this, reconstructing a run
    means guessing at the harness.
    """
    source: Mapping[str, str] = os.environ if environ is None else environ
    captured: dict[str, str] = {}
    for name in sorted(source):
        if not str(name).startswith(prefix):
            continue
        if _key_is_secret(name[len(prefix) :]) or _key_is_secret(name):
            captured[str(name)] = MASKED_VALUE
        else:
            captured[str(name)] = _truncate(
                _clean_text(source.get(name)),
                MAX_SNAPSHOT_ENV_VALUE_CHARS,
            )
        if len(captured) >= MAX_SNAPSHOT_ENV_VARS:
            break
    return captured


def config_snapshot_payload(
    *,
    config_values: Mapping[str, Any] | None = None,
    version: str = "",
    build_info: Mapping[str, Any] | None = None,
    sampling: SamplingSettings | None = None,
    response_header_allowlist: ResponseHeaderAllowlist | None = None,
    environ: Mapping[str, str] | None = None,
    effective_values: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the once-per-run ``config_snapshot`` session-log event payload.

    Takes an already-dumped config mapping (``AppConfig.model_dump()``) rather
    than the model itself, which keeps this module free of pydantic and lets
    the tests exercise it in a bare interpreter.

    ``effective_values`` carries settings as the session will actually enforce
    them, resolved through their full precedence chain, each ideally paired
    with a ``*_source`` key naming the layer that won. It is the caller's
    statement of enforcement, recorded verbatim (after the same secret
    scrubbing as the config dump): this module cannot re-derive precedence
    without importing the config machinery it deliberately stays free of.
    """
    allowlist = (
        DEFAULT_RESPONSE_HEADER_ALLOWLIST
        if response_header_allowlist is None
        else response_header_allowlist
    )
    effective_sampling = resolve_sampling_settings() if sampling is None else sampling
    return {
        "schema_version": CONFIG_SNAPSHOT_SCHEMA_VERSION,
        "version": _clean_text(version),
        "build": dict(build_info or {}),
        "sampling": effective_sampling.session_event_payload(),
        "response_header_allowlist": allowlist.describe(),
        "config": scrub_config_values(dict(config_values or {})),
        "effective": scrub_config_values(dict(effective_values or {})),
        "environment": snapshot_environment(environ=environ),
    }
