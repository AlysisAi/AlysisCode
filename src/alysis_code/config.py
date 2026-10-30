from __future__ import annotations

import json
import math
import os
import re
import shlex
import sys
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from io import BufferedRandom
from pathlib import Path
from typing import Any, Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .atomic_io import atomic_write_json
from .branding import (
    canonical_user_config_dir,
    canonical_user_data_dir,
    env_get,
)
from .config_errors import ConfigError
from .llm.provider_defaults import (
    DEFAULT_PROVIDER_CONCURRENCY_CAPS,
    DEFAULT_PROVIDER_RETRY_BASE_DELAY_SECONDS,
    DEFAULT_PROVIDER_RETRY_MAX_DELAY_SECONDS,
    DEFAULT_PROVIDER_RETRY_MAX_RETRIES,
)
from .llm.streaming import STREAM_ASSEMBLY_AUTO, STREAM_ASSEMBLY_SETTINGS
from .prompt_guidance_catalog import (
    GUIDANCE_PROFILES,
    ParentPromptGuidanceProfile,
    PromptGuidanceProfile,
    get_model_prompt_catalog,
    subagent_profile,
)
from .step_budget import (
    AUTONOMOUS_STEP_BUDGET_POLICY,
    DEFAULT_CHAT_MAX_STEPS,
    DEFAULT_SUBAGENT_MAX_STEPS,
    DEFAULT_TASK_MAX_STEPS,
    normalize_step_budget_policy,
)
from .web_research import normalize_trusted_domain_entry
from .web_search_adapters import normalize_web_search_adapter
from .web_search_policy import normalize_web_search_policy
from .windows_verification_command import (
    is_windows_verifier_command,
    split_windows_verifier_command,
)

if os.name == "nt":  # pragma: no cover - exercised on Windows
    import msvcrt
else:  # pragma: no cover - exercised on POSIX
    import fcntl


@dataclass(frozen=True)
class ApiKeyResolution:
    key: str | None
    source: str


_VALID_TOOLBAR_ITEMS: set[str] = {
    "mode",
    "model",
    "stream",
    "trace",
    "images",
    "temp",
    "ctx",
    "subagents",
    "tokens",
    "cost",
    "forge",
}
_DEFAULT_TOOLBAR_ITEMS: tuple[str, ...] = ("mode", "model", "ctx", "subagents")
DEFAULT_SUBAGENT_TIMEOUT_S: float | None = None

# Hosts (plus their subdomains) that web_fetch may fetch without prior session
# provenance. Curated to major public package registries — read-only metadata
# endpoints that search engines do not index, so the search-mediated provenance
# recovery path can never authorize them. The safe-HTTP guard (scheme, DNS/IP,
# redirect, and byte-cap validation) still applies to every fetch.
DEFAULT_WEB_FETCH_TRUSTED_DOMAINS: tuple[str, ...] = (
    "registry.npmjs.org",
    "registry.yarnpkg.com",
    "pypi.org",
    "files.pythonhosted.org",
    "crates.io",
    "index.crates.io",
    "static.crates.io",
    "rubygems.org",
    "repo.maven.apache.org",
    "repo1.maven.org",
    "proxy.golang.org",
    "sum.golang.org",
    "pkg.go.dev",
    "hex.pm",
    "repo.hex.pm",
    "packagist.org",
    "repo.packagist.org",
    "api.nuget.org",
)
DEFAULT_VERIFY_COMMANDS: tuple[str, ...] = ("pytest -q",)
VERIFY_RUNNER_PREFIXES: frozenset[tuple[str, str]] = frozenset(
    {
        ("poetry", "run"),
        ("uv", "run"),
        ("pipenv", "run"),
    }
)
VERIFY_PY_LAUNCHERS: frozenset[str] = frozenset({"py", "py.exe"})
VERIFY_PYTHON_LAUNCHER_RE = re.compile(r"python(?:\d+(?:\.\d+)*)?(?:\.exe)?$")
VERIFY_MODULE_NAMES: frozenset[str] = frozenset({"pytest", "ruff", "unittest"})
_WEB_SEARCH_MODES: set[str] = {"off", "auto", "native", "external"}
_REASONING_EFFORTS: set[str] = {
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "ultra",
}
_PROMPT_CACHE_MODES: set[str] = {"off", "manual", "auto"}
_ANTHROPIC_PROMPT_CACHE_TTLS: set[str] = {"5m", "1h"}
DEFAULT_FEEDBACK_GITHUB_REPO = "AlysisAi/AlysisCode"
_GITHUB_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
_CONFIG_STORE_LOCK = threading.RLock()
_CONFIG_TRANSACTION_STATE = threading.local()

CLOUD_PROFILE_KEY = "alysis"


class AssetsComprehensionConfig(BaseModel):
    role: str = "comprehension"
    vision_fallback_profile: str | None = None
    vision_with_ocr_when_available: bool = True
    ocr_enabled: Literal["auto", "always", "never"] = "auto"
    ocr_provider: str = "tesseract"
    ocr_timeout_seconds: int = 30
    image_max_edge_pixels: int = 2048
    questioning_mode: Literal["assertive", "balanced", "assumption_friendly"] = "balanced"
    schema_version: int = 1


class AssetsPlannerConfig(BaseModel):
    inline_images: bool = True
    max_inline_images: int = 8
    readiness_policy: Literal["soft", "block", "partial"] = "soft"
    readiness_timeout_seconds: float = 60.0
    max_chars_per_asset: int = 2000
    max_primary_per_task: int = 8


class AssetsWorkerConfig(BaseModel):
    inline_images: bool = True
    max_inline_images: int = 8
    fail_on_mirror_error: bool = False
    allocator_role: str = "comprehension"
    allocator_timeout_seconds: int = 30
    max_chars_per_asset_block: int = 4000
    max_focused_extract_chars: int = 4000
    schema_version: int = 1


class AssetsConfig(BaseModel):
    enabled: bool = True
    comprehension: AssetsComprehensionConfig = Field(default_factory=AssetsComprehensionConfig)
    planner: AssetsPlannerConfig = Field(default_factory=AssetsPlannerConfig)
    worker: AssetsWorkerConfig = Field(default_factory=AssetsWorkerConfig)


class ImageGenerationConfig(BaseModel):
    """Opt-in OpenAI-compatible image generation capability.

    Generation is disabled by default because every successful call can incur a
    separate provider charge.  When no image-specific endpoint or credential is
    configured, the active profile endpoint and resolved API key are reused.
    """

    enabled: bool = False
    model: str = "gpt-image-1"
    base_url: str | None = None
    api_key_env: str | None = None
    timeout_s: float = Field(default=180.0, gt=0.0, le=900.0)
    max_images_per_call: int = Field(default=4, ge=1, le=4)
    max_image_bytes: int = Field(default=25_000_000, ge=1024, le=100_000_000)
    max_pixels: int = Field(default=16_777_216, ge=1_000_000, le=67_108_864)

    @model_validator(mode="after")
    def validate_provider_settings(self) -> Self:
        self.model = str(self.model or "").strip()
        if not self.model:
            raise ValueError("Image generation model cannot be empty.")
        self.api_key_env = str(self.api_key_env or "").strip() or None
        if (
            self.api_key_env
            and re.fullmatch(
                r"[A-Za-z_][A-Za-z0-9_]*",
                self.api_key_env,
            )
            is None
        ):
            raise ValueError(
                "Image generation api_key_env must be a valid environment variable name."
            )
        self.base_url = str(self.base_url or "").strip() or None
        if self.base_url:
            parsed = urlsplit(self.base_url)
            if (
                parsed.scheme.lower() not in {"http", "https"}
                or not parsed.netloc
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError(
                    "Image generation base_url must be a credential-free HTTP(S) URL "
                    "without a query or fragment."
                )
        return self


_RUNTIME_SECRET_FIELD_NAMES = frozenset(
    {
        "authorization",
        "bearer",
        "cookie",
        "cookies",
        "credential",
        "credentials",
        "id_token",
        "oauth",
        "password",
        "passwd",
        "secret",
        "token",
    }
)
_RUNTIME_SECRET_FIELD_MARKERS = (
    "access_token",
    "api_key",
    "auth_token",
    "client_secret",
    "private_key",
    "refresh_token",
)
_RUNTIME_SECRET_FIELD_SUFFIXES = (
    "_authorization",
    "_credential",
    "_credentials",
    "_password",
    "_passwd",
    "_secret",
    "_token",
)
_RUNTIME_SECRET_FIELD_SEGMENTS = frozenset(
    {
        "authorization",
        "bearer",
        "cookie",
        "cookies",
        "credential",
        "credentials",
        "oauth",
        "password",
        "passwd",
        "secret",
        "token",
    }
)
_RUNTIME_NON_SECRET_FIELD_ALLOWLIST = frozenset(
    {
        # Describes which provider-owned store is used; it is not a credential,
        # credential path, or credential value.
        "credential_store_backend",
    }
)


def _runtime_secret_field(value: object) -> bool:
    normalized = str(value or "").strip().lower().replace("-", "_")
    if normalized in _RUNTIME_NON_SECRET_FIELD_ALLOWLIST:
        return False
    segments = frozenset(segment for segment in normalized.split("_") if segment)
    return (
        normalized in _RUNTIME_SECRET_FIELD_NAMES
        or any(marker in normalized for marker in _RUNTIME_SECRET_FIELD_MARKERS)
        or normalized.endswith(_RUNTIME_SECRET_FIELD_SUFFIXES)
        or bool(segments & _RUNTIME_SECRET_FIELD_SEGMENTS)
    )


def _find_runtime_secret_field(value: object, path: str = "") -> str | None:
    if isinstance(value, dict):
        for key, nested in value.items():
            current = f"{path}.{key}" if path else str(key)
            if _runtime_secret_field(key):
                return current
            found = _find_runtime_secret_field(nested, current)
            if found:
                return found
    elif isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            found = _find_runtime_secret_field(nested, f"{path}[{index}]")
            if found:
                return found
    return None


def _reject_runtime_secret_fields(value: object, *, label: str) -> None:
    secret_path = _find_runtime_secret_field(value)
    if secret_path:
        raise ValueError(
            f"{label} {secret_path!r} looks secret. "
            "Provider credentials must remain owned by the provider runtime."
        )


class ExecutionConfig(BaseModel):
    """Select the native loop or a provider-managed delegated runtime."""

    model_config = ConfigDict(extra="allow")

    backend: Literal["native", "delegated"] = "native"
    runtime: str | None = None

    @model_validator(mode="after")
    def validate_backend_runtime_pair(self) -> Self:
        _reject_runtime_secret_fields(
            self.model_extra or {},
            label="Execution setting",
        )
        runtime = str(self.runtime or "").strip() or None
        self.runtime = runtime
        if self.backend == "delegated" and runtime is None:
            raise ValueError("Delegated execution requires a selected agent runtime.")
        if self.backend == "native" and runtime is not None:
            raise ValueError("Native execution cannot select a delegated agent runtime.")
        return self


class AgentRuntimeSettings(BaseModel):
    """Provider-neutral settings for one delegated agent runtime."""

    model_config = ConfigDict(extra="allow")

    adapter: str
    executable: str
    provider_managed_auth: bool = True
    model: str | None = None
    reasoning_effort: str | None = None
    timeout_seconds: float = Field(default=3600.0, gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def reject_persisted_secrets(self) -> Self:
        _reject_runtime_secret_fields(
            self.model_extra or {},
            label="Agent runtime setting",
        )
        return self


class SubagentOrchestrationConfig(BaseModel):
    max_background_children: int = Field(default=3, ge=1)
    turn_end_policy: Literal["wait", "cancel"] = "wait"
    workspace_isolation_enabled: bool = True
    parallel_nonwriting_shared: bool = False
    helpers_enabled: bool = True
    helper_max_total_per_child: int = Field(default=2, ge=0)
    helper_timeout_s: float | None = Field(
        default=None,
        gt=0,
        allow_inf_nan=False,
        description=(
            "Optional wall-clock ceiling for a nested helper. Unset means the helper "
            "only inherits an active parent deadline."
        ),
    )
    helper_max_steps: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Optional step ceiling for nested helpers. Unset keeps autonomous helpers "
            "unbounded unless an explicit limit or the limited step policy applies."
        ),
    )
    # Match the established exploration-stagnation threshold: three identical
    # outcomes are enough to identify repetition without treating one retry as a loop.
    repetition_signal_threshold: int = Field(default=3, ge=2)
    # The second occurrence is cheap enough for a child-local advisory; this is
    # deliberately below the parent wake threshold.
    repetition_nudge_occurrence_threshold: int = Field(default=2, ge=2)
    # Five occurrences inside the retained outcome window catch alternating
    # loops while leaving several ordinary retries quiet.
    repetition_occurrence_threshold: int = Field(default=5, ge=3)
    # Emergency containment only: ten warning windows give an available parent
    # ample time to react before a demonstrably stagnant child is stopped.
    repetition_backstop_threshold: int = Field(default=30, ge=3)
    model_response_activity_after_s: float = Field(
        default=15.0,
        gt=0,
        allow_inf_nan=False,
    )
    inactivity_signal_after_s: float = Field(
        default=180.0,
        gt=0,
        allow_inf_nan=False,
    )
    inflight_deadline_grace_s: float = Field(
        default=10.0,
        ge=0,
        allow_inf_nan=False,
    )

    @model_validator(mode="after")
    def validate_repetition_thresholds(self) -> Self:
        if self.repetition_backstop_threshold <= self.repetition_signal_threshold:
            raise ValueError(
                "repetition_backstop_threshold must exceed repetition_signal_threshold"
            )
        if self.repetition_nudge_occurrence_threshold >= self.repetition_occurrence_threshold:
            raise ValueError(
                "repetition_nudge_occurrence_threshold must be lower than "
                "repetition_occurrence_threshold"
            )
        if self.repetition_occurrence_threshold > self.repetition_backstop_threshold:
            raise ValueError(
                "repetition_occurrence_threshold must not exceed repetition_backstop_threshold"
            )
        return self


class CacheConfig(BaseModel):
    prompt_cache_key_enabled: bool = True
    keepalive_enabled: bool = False
    keepalive_idle_threshold_s: float = Field(default=240.0, gt=0, allow_inf_nan=False)


class PromptGuidanceConfig(BaseModel):
    """Prompt templates only; model selection and runtime requirements are unchanged."""

    model_config = ConfigDict(extra="forbid")

    default: ParentPromptGuidanceProfile = "expanded"
    # Explicit, editable assignments, not model-family or capability inference.
    # Supplying a mapping replaces this default mapping, including an empty map.
    model_profiles: dict[str, ParentPromptGuidanceProfile] = Field(
        default_factory=get_model_prompt_catalog
    )

    @model_validator(mode="after")
    def validate_model_ids(self) -> Self:
        if any(not model or model != model.strip() for model in self.model_profiles):
            raise ValueError("prompt_guidance.model_profiles requires nonempty, trimmed model IDs")
        return self


def resolve_prompt_guidance_profile(
    cfg: AppConfig | None,
    *,
    model: str | None = None,
    subagent: bool = False,
) -> PromptGuidanceProfile:
    """Resolve the effective model, then select parent density or its child family.

    Scope comes from the session, never from whether delegation tools are enabled.
    Child roles remain separate from this family-level working guidance.
    """

    guidance = cfg.prompt_guidance if cfg is not None else PromptGuidanceConfig()
    effective_model = str(model if model is not None else getattr(cfg, "model", "") or "").strip()
    profile = guidance.model_profiles.get(effective_model, guidance.default)
    return subagent_profile(profile) if subagent else profile


class AnytimeCheckpointConfig(BaseModel):
    enabled: bool = True
    max_files: int = Field(default=2000, ge=1, le=20000)
    max_total_bytes: int = Field(default=64 * 1024 * 1024, ge=1024, le=512 * 1024 * 1024)
    max_candidates: int = Field(default=4, ge=1, le=20)
    # This is host configuration, never inferred from a task name or supplied
    # by the model. The command must also be an accepted required check.
    objective_command: str = ""
    objective_key: str = "score"
    objective_direction: Literal["minimize", "maximize"] = "minimize"


class AppConfig(BaseModel):
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    prompt_guidance: PromptGuidanceConfig = Field(default_factory=PromptGuidanceConfig)
    read_ledger_enabled: bool = True
    agent_runtimes: dict[str, AgentRuntimeSettings] = Field(default_factory=dict)
    base_url: str = "https://api.openai.com/v1"
    model: str = ""
    llm_timeout_s: float = 60.0
    llm_stream_no_progress_timeout_s: float = Field(
        default=240.0,
        gt=0,
        allow_inf_nan=False,
    )
    run_deadline_seconds: float | None = None
    run_deadline_unlimited: bool = False
    run_deadline_degradation_enabled: bool = True
    run_deadline_convergence_fraction: float = 0.75
    run_deadline_wrap_up_fraction: float = 0.90
    llm_enable_thinking: bool | None = None
    llm_reasoning_effort: str | None = None
    provider_concurrency_caps: dict[str, int] = Field(
        default_factory=lambda: dict(DEFAULT_PROVIDER_CONCURRENCY_CAPS),
    )
    provider_retry_max_retries: int = DEFAULT_PROVIDER_RETRY_MAX_RETRIES
    provider_retry_base_delay_seconds: float = DEFAULT_PROVIDER_RETRY_BASE_DELAY_SECONDS
    provider_retry_max_delay_seconds: float = DEFAULT_PROVIDER_RETRY_MAX_DELAY_SECONDS
    model_metadata_policy: str = "warn"
    default_mode: str = "review"  # review|auto|readonly|fullaccess
    # Default persona for interactive chat: code|architect|ask|debug. Personas
    # are conventions (prompt overlay, model role, default execution mode)
    # layered on the execution-mode gate; "code" is the no-op persona and
    # preserves pre-persona behavior exactly. See docs/personas.md.
    default_persona: str = "code"
    # Kill-switch for persona modes; env override ALYSIS_PERSONA_MODES=off
    # wins over the config value. Off: /permissions accepts only execution modes,
    # the switch_mode tool is not registered, and personas stay at Code.
    persona_modes_enabled: bool = True
    max_steps: int = DEFAULT_CHAT_MAX_STEPS
    temperature: float = 0.2  # legacy global override
    coding_temperature: float = 0.2
    review_temperature: float = 0.0
    planner_temperature: float = 0.2
    conflict_review_temperature: float = 0.0
    compactor_temperature: float = 0.2
    chat_temperature: float = 0.7
    # Sampling determinism controls. Unset (the default) means "do not pin",
    # and the request is built exactly as it was before these existed: nothing
    # is added to the payload and the per-role temperatures above still apply.
    # Set, they pin sampling for every chat-completions request in the run so
    # two runs of one build can be compared. Env overrides
    # ALYSIS_SAMPLING_TEMPERATURE / _TOP_P / _SEED win over these values;
    # bounds are shared with run_provenance so both paths accept the same set.
    sampling_temperature: float | None = None
    sampling_top_p: float | None = None
    sampling_seed: int | None = None
    stream: bool = True
    # auto|concat|cumulative: how streamed chunks become content and tool-call
    # arguments. "auto" is "concat" for every provider not known to re-send
    # cumulative text (see llm.openai_compat.resolve_stream_assembly).
    llm_stream_assembly: str = STREAM_ASSEMBLY_AUTO
    routing_mode: str = "auto"  # auto|code_only
    # Kill-switch for capability arbitration over router verdicts; env override
    # ALYSIS_ROUTE_ARBITRATION=off wins over the config value.
    route_arbitration_enabled: bool = True
    # Kill-switch for the router's semantic turn contract; env override
    # ALYSIS_SEMANTIC_TURN_CONTRACT=off keeps the router's legacy posture-only
    # path without restoring language-specific controller classification.
    semantic_turn_contract_enabled: bool = True
    # The router-free unified turn path: no router client is provisioned,
    # every text turn goes straight to the main model with the full per-mode
    # agent surface, and execution posture derives from the execution mode.
    # The legacy pre-turn semantic-router path has been removed; this key
    # (and ALYSIS_UNIFIED_TURN_PATH) stays accepted for one release but is
    # ignored — every turn takes the unified path.
    unified_turn_path_enabled: bool = True
    # Optional fixed reply language (e.g. "Greek") for turns that run without
    # the router. Empty means the model answers in the user's language
    # naturally. When set, the host injects a reply-language directive and
    # keys the final-summary language rewrite to it.
    reply_language: str = ""
    # Kill-switch for the fact-based completion-evidence classifier (evidence v2);
    # env override ALYSIS_EVIDENCE_V2=off reverts to legacy string-shape evidence.
    evidence_v2_enabled: bool = True
    # Kill-switch for the baseline-first regression protocol (step 3); env override
    # ALYSIS_REGRESSION_BASELINE=off keeps capture/telemetry but reverts the
    # completion-gate policy to legacy (no regression/unattributed attribution).
    regression_baseline_enabled: bool = True
    # Kill-switch for turn-contract v2 (step 4: apply-don't-advise + spec literalism);
    # env override ALYSIS_TURN_CONTRACT_V2=off keeps expectation extraction/telemetry
    # but reverts the completion-gate policy (no expectations_unaddressed / advisory
    # completion enforcement). Prompt additions are unconditional either way.
    turn_contract_v2_enabled: bool = True
    # Kill-switch for the reproduction-first protocol on bug-fix-shaped tasks
    # (step 7); env override ALYSIS_REPRODUCTION_FIRST=off keeps repro-run
    # capture/telemetry but reverts the turn directives and the completion-gate
    # policy (no repro_unconfirmed / repro_artifacts_present enforcement).
    reproduction_first_enabled: bool = True
    # Kill-switch for the blast-radius regression gate (a correct fix that breaks
    # neighbouring tests is a failed task); env override ALYSIS_BLAST_RADIUS=off
    # keeps scope-run capture/telemetry but reverts the turn directives and the
    # completion-gate policy (no blast_radius_regressions / blast_radius_unverified).
    blast_radius_gate_enabled: bool = True
    # Ceiling on how many test files one blast-radius scope may name. The scope is a
    # safety net around the change, not a suite run.
    blast_radius_max_scope_files: int = Field(default=40, gt=0)
    # Wall-clock ceiling for one scope run. A run that exceeds it shrinks the scope
    # (nearest tests kept) for the next run; it never disables the gate.
    blast_radius_scope_seconds_cap: float = Field(default=300.0, gt=0, allow_inf_nan=False)
    # Newly-broken test count past which the change is treated as over-broad and the
    # repair directive switches from "fix each failure" to "rewrite the patch narrowly".
    blast_radius_over_broad_threshold: int = Field(default=20, gt=0)
    # Kill-switch for reaping agent-started process groups (step 5); env override
    # ALYSIS_PROCESS_REAPING=off keeps commands in their own process group but
    # never signals one, restoring the legacy leave-it-running behaviour.
    process_reaping_enabled: bool = True
    # Kill-switch for workspace test-runner pre-provisioning (step 5); env override
    # ALYSIS_WORKSPACE_PROVISIONING=off suppresses both the one-shot install in
    # autonomous runs and the env_gap_detected telemetry.
    workspace_provisioning_enabled: bool = True
    # Kill-switch for bounded empty-response handling; env override
    # ALYSIS_EMPTY_RESPONSE_STALL=off restores the legacy behaviour of retrying
    # an endpoint that returns contentless responses until the attempt cap, then
    # terminating non-zero regardless of what the working tree already holds.
    empty_response_stall_guard_enabled: bool = True
    # A stall is declared after this many consecutive responses with no text and
    # no tool calls, or after this many seconds of an unbroken contentless streak,
    # whichever comes first.
    empty_response_stall_threshold: int = Field(default=3, gt=0)
    empty_response_stall_seconds: float = Field(default=300.0, gt=0, allow_inf_nan=False)
    # Ceilings on stall handling: recovery cycles per session, and total wall-clock
    # seconds the session may spend on empty-response handling before salvaging.
    empty_response_max_recovery_cycles: int = Field(default=2, gt=0)
    empty_response_handling_budget_seconds: float = Field(default=600.0, gt=0, allow_inf_nan=False)
    step_budget_policy: str = AUTONOMOUS_STEP_BUDGET_POLICY
    task_max_steps: int = DEFAULT_TASK_MAX_STEPS
    subagent_max_steps: int = DEFAULT_SUBAGENT_MAX_STEPS
    subagent_timeout_s: float | None = Field(
        default=DEFAULT_SUBAGENT_TIMEOUT_S,
        gt=0,
        allow_inf_nan=False,
        description=(
            "Optional wall-clock ceiling in seconds for each ordinary subagent run. "
            "Unset means the child only inherits an active parent deadline."
        ),
    )
    subagent_orchestration: SubagentOrchestrationConfig = Field(
        default_factory=SubagentOrchestrationConfig,
    )
    subagents_enabled: bool = True
    skills_enabled: bool = True
    bundled_skills_enabled: bool = True
    skills_auto_invoke: bool = Field(
        default=True,
        description=(
            "Enable model-decided skill activation from discovered skill descriptions; "
            "explicit false preserves manual/discovery-only behavior."
        ),
    )
    experimental_gemini_interactions_enabled: bool = False
    custom_tools_enabled: bool = True
    web_tools_enabled: bool = Field(
        default=True,
        description=(
            "Master switch for ALL web tools (web_fetch and web_search). When false, "
            "web tools are never registered in the model's tool list and any runtime "
            "web call hard-errors. Overridable via ALYSIS_WEB_TOOLS env var; used "
            "by benchmark/offline runs to guarantee no network-mediated contamination."
        ),
    )
    web_fetch_trusted_domains: list[str] = Field(
        default_factory=lambda: list(DEFAULT_WEB_FETCH_TRUSTED_DOMAINS),
        description=(
            "Hosts (matched exactly or as a subdomain suffix) that web_fetch may "
            "fetch without prior session provenance such as a user-provided URL or "
            "a web_search result. Defaults to major public package registries so "
            "dependency lookups work in fresh sessions and subagents. The safe-HTTP "
            "guard still validates scheme, credentials, DNS/IP ranges, redirects, "
            "and response size on every fetch. Set to [] to require provenance for "
            "every URL. Overridable via ALYSIS_WEB_FETCH_TRUSTED_DOMAINS "
            "(comma-separated hosts; 'none' disables the allowlist)."
        ),
    )
    web_search_mode: str = "auto"
    web_search_policy: str = "auto"
    web_search_adapter: str = "auto"
    web_search_base_url: str | None = None
    web_search_model: str | None = None
    web_search_timeout_s: float = 45.0
    update_check_enabled: bool = True
    update_check_interval_hours: int = 24
    update_check_timeout_s: float = 3.0
    update_prompt_enabled: bool = True
    feedback_github_enabled: bool = True
    feedback_github_repo: str = DEFAULT_FEEDBACK_GITHUB_REPO
    feedback_open_browser: bool = True
    session_log_dir: str | None = None
    crash_diagnostic_log_path: str | None = None
    prompt_cache_mode: str = "manual"
    # Hosted defaults are applied once when an Alysis profile is selected/loaded.
    # Explicit cache choices also set this marker, including reselecting Manual.
    hosted_prompt_cache_defaults_applied: bool = False
    prompt_cache_key: str | None = None
    prompt_cache_retention: str | None = None
    anthropic_prompt_cache_enabled: bool = False
    anthropic_prompt_cache_ttl: str = "5m"
    verify_commands: list[str] = Field(
        default_factory=lambda: list(DEFAULT_VERIFY_COMMANDS),
    )
    integration_verify_mode: str = "warn"
    integration_verify_commands: list[str] = Field(default_factory=list)
    anytime_checkpoint: AnytimeCheckpointConfig = Field(default_factory=AnytimeCheckpointConfig)
    replanning_mode: str = "off"
    toolbar_items: list[str] = Field(
        default_factory=lambda: list(_DEFAULT_TOOLBAR_ITEMS),
    )
    assets: AssetsConfig = Field(default_factory=AssetsConfig)
    image_generation: ImageGenerationConfig = Field(default_factory=ImageGenerationConfig)

    # Internal: allow future keys without crashing older clients.
    extra_fields: dict[str, Any] = Field(default_factory=dict, exclude=True)

    @model_validator(mode="after")
    def validate_delegated_runtime_settings(self) -> Self:
        self.step_budget_policy = normalize_step_budget_policy(self.step_budget_policy)
        if self.execution.backend != "delegated":
            return self
        runtime_id = str(self.execution.runtime or "").strip()
        if runtime_id not in self.agent_runtimes:
            raise ValueError(f"Delegated runtime {runtime_id!r} has no agent_runtimes settings.")
        return self


_ROLE_TEMPERATURE_FIELDS: dict[str, str] = {
    "coding": "coding_temperature",
    "review": "review_temperature",
    "planner": "planner_temperature",
    "conflict_review": "conflict_review_temperature",
    "compactor": "compactor_temperature",
    "chat": "chat_temperature",
}

_ROLE_TEMPERATURE_DEFAULTS: dict[str, float] = {
    "coding": 0.2,
    "review": 0.0,
    "planner": 0.2,
    "conflict_review": 0.0,
    "compactor": 0.2,
    "chat": 0.7,
}


def clone_cfg(cfg: AppConfig) -> AppConfig:
    """Return a deep copy of AppConfig while preserving extra_fields."""
    return cfg.model_copy(deep=True)


def normalize_verify_command_list(commands: Sequence[str] | None) -> tuple[str, ...]:
    if not commands:
        return ()
    normalized: list[str] = []
    for item in commands:
        text = str(item).strip()
        if text:
            normalized.append(text)
    return tuple(normalized)


def is_generic_verify_command_fallback(commands: Sequence[str] | None) -> bool:
    return normalize_verify_command_list(commands) == DEFAULT_VERIFY_COMMANDS


def is_generic_configured_verify_preset(commands: Sequence[str] | None) -> bool:
    normalized = normalize_verify_command_list(commands)
    if not normalized:
        return False
    return all(_is_generic_configured_verify_command(command) for command in normalized)


def split_verify_command_parts(command: str) -> list[str] | None:
    text = str(command or "")
    if is_windows_verifier_command(text):
        return split_windows_verifier_command(text)
    launcher_path_parts = _split_supported_launcher_path_command_parts(text)
    if launcher_path_parts is not None:
        return launcher_path_parts
    try:
        return shlex.split(text, posix=True)
    except ValueError:
        return None


def strip_verify_runner_prefix(parts: Sequence[str]) -> list[str] | None:
    tokens = list(parts)
    if len(tokens) >= 2 and (tokens[0].casefold(), tokens[1].casefold()) in VERIFY_RUNNER_PREFIXES:
        return tokens[2:] or None
    return tokens


def verify_launcher_basename(token: str) -> str:
    normalized = _strip_matching_shell_quotes(str(token).strip()).replace("\\", "/")
    if not normalized:
        return ""
    return normalized.rsplit("/", 1)[-1].casefold()


def normalize_verify_module_invocation(parts: Sequence[str]) -> list[str]:
    tokens = list(parts)
    if len(tokens) < 3 or tokens[1] != "-m":
        return tokens
    launcher = verify_launcher_basename(tokens[0])
    if launcher not in VERIFY_PY_LAUNCHERS and not VERIFY_PYTHON_LAUNCHER_RE.fullmatch(launcher):
        return tokens
    module = tokens[2].casefold()
    if module not in VERIFY_MODULE_NAMES:
        return tokens
    return [module, *tokens[3:]]


def _is_generic_configured_verify_command(command: str) -> bool:
    tokens = split_verify_command_parts(command)
    if not tokens:
        return False
    if any(token in {"||", "&&", ";", "|", "&"} for token in tokens):
        return False
    if tokens[0] == "env" or _looks_like_env_assignment(tokens[0]):
        return False
    lowered = [token.casefold() for token in tokens]
    if _is_generic_pytest_or_ruff_command(lowered):
        return True
    runner_stripped = strip_verify_runner_prefix(lowered)
    if runner_stripped is None:
        return False
    return runner_stripped != lowered and _is_generic_pytest_or_ruff_command(runner_stripped)


def _is_generic_pytest_or_ruff_command(tokens: list[str]) -> bool:
    normalized = normalize_verify_module_invocation(tokens)
    return _is_generic_pytest_command(normalized) or _is_generic_ruff_check_command(normalized)


def _is_generic_pytest_command(tokens: list[str]) -> bool:
    if tokens in (["pytest"], ["pytest", "-q"], ["py.test"], ["py.test", "-q"]):
        return True
    if len(tokens) in {3, 4} and tokens[0] in {"python", "python3"}:
        if tokens[1:3] == ["-m", "pytest"]:
            return len(tokens) == 3 or tokens[3] == "-q"
    return False


def _is_generic_ruff_check_command(tokens: list[str]) -> bool:
    if tokens[:2] != ["ruff", "check"]:
        return False
    targets = tokens[2:]
    if not targets:
        return True
    if len(targets) != 1:
        return False
    return _normalize_generic_ruff_check_target(targets[0]) in {".", "src"}


def _normalize_generic_ruff_check_target(target: str) -> str:
    normalized = target.strip().replace("\\", "/")
    while normalized.endswith("/") and normalized not in {"/", "./"}:
        normalized = normalized[:-1]
    if normalized == "./":
        return "."
    if normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized or "."


def _looks_like_env_assignment(token: str) -> bool:
    if "=" not in token:
        return False
    name, _value = token.split("=", 1)
    if not name:
        return False
    return all(ch == "_" or ch.isalnum() for ch in name)


def split_command_words(command: str) -> list[str] | None:
    """The words of one command line, with backslashes kept literally.

    Quotes group a word and are removed; nothing else is interpreted, so Windows paths
    (``C:\\venv\\Scripts\\pytest.exe``) and quoted paths with spaces
    (``"C:\\Program Files\\Python312\\python.exe"``) come out whole. ``None`` when the
    quotes do not balance.
    """
    try:
        raw_parts = shlex.split(str(command or ""), posix=False)
    except ValueError:
        return None
    return [_strip_matching_shell_quotes(token) for token in raw_parts]


def _split_supported_launcher_path_command_parts(command: str) -> list[str] | None:
    parts = split_command_words(command)
    if not parts:
        return None

    candidate = parts
    runner_stripped = strip_verify_runner_prefix(candidate)
    if runner_stripped is None:
        return None
    if runner_stripped != candidate:
        candidate = runner_stripped

    if len(candidate) < 3 or candidate[1] != "-m":
        return None
    launcher = candidate[0]
    if "\\" not in launcher and "/" not in launcher:
        return None

    launcher_basename = verify_launcher_basename(launcher)
    if launcher_basename not in VERIFY_PY_LAUNCHERS and not VERIFY_PYTHON_LAUNCHER_RE.fullmatch(
        launcher_basename
    ):
        return None
    return parts


def _strip_matching_shell_quotes(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in {"'", '"'}:
        return token[1:-1]
    return token


def _config_dir() -> Path:
    override = env_get("ALYSIS_CONFIG_DIR")
    if override:
        return Path(override)
    return canonical_user_config_dir()


def _data_dir() -> Path:
    override = env_get("ALYSIS_DATA_DIR")
    if override:
        return Path(override)
    return canonical_user_data_dir()


def config_path() -> Path:
    return _config_dir() / "config.json"


def _config_lock_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.lock")


def _acquire_config_file_lock(handle: BufferedRandom) -> None:
    if os.name == "nt":  # pragma: no cover - exercised on Windows
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        return
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _release_config_file_lock(handle: BufferedRandom) -> None:
    if os.name == "nt":  # pragma: no cover - exercised on Windows
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def credentials_path() -> Path:
    return _config_dir() / "credentials.json"


def default_sessions_dir() -> Path:
    return _data_dir() / "sessions"


def default_chat_history_path() -> Path:
    return _data_dir() / "chat_history.txt"


def _migrate_legacy_agent_runtime_config(
    raw: dict[str, Any],
    known: dict[str, Any],
) -> set[str]:
    """Translate the discarded ``acli`` prototype shape without writing it yet."""

    if "execution" in raw:
        return set()
    raw_connection = raw.get("connection")
    connection = raw_connection if isinstance(raw_connection, dict) else {}
    connection_kind = str(connection.get("kind") or "").strip().lower()
    connection_provider = str(connection.get("provider") or "").strip().lower()
    legacy_backend = str(raw.get("llm_backend") or "").strip().lower()
    delegated = (
        connection_kind in {"subscription", "account", "subscription-account"}
        and connection_provider in {"", "chatgpt", "chat-gpt", "codex"}
    ) or legacy_backend in {"codex", "codex_cli", "codex-cli"}
    if not delegated:
        return set()

    raw_subscription = raw.get("subscription")
    subscription = raw_subscription if isinstance(raw_subscription, dict) else {}
    raw_chatgpt = subscription.get("chatgpt")
    chatgpt = raw_chatgpt if isinstance(raw_chatgpt, dict) else {}
    raw_nested_codex = chatgpt.get("codex_cli")
    nested_codex = raw_nested_codex if isinstance(raw_nested_codex, dict) else {}
    raw_legacy_codex = raw.get("codex_cli")
    legacy_codex = raw_legacy_codex if isinstance(raw_legacy_codex, dict) else {}
    settings_source = {**legacy_codex, **nested_codex}
    executable = str(settings_source.get("executable") or settings_source.get("path") or "codex")
    settings: dict[str, Any] = {
        "adapter": "codex-cli",
        "executable": executable,
        "provider_managed_auth": True,
    }
    model = str(settings_source.get("model") or "").strip()
    if model:
        settings["model"] = model
    timeout = settings_source.get("timeout_seconds")
    if timeout is not None and str(timeout).strip():
        settings["timeout_seconds"] = timeout
    known["execution"] = {"backend": "delegated", "runtime": "openai-codex"}
    existing_runtimes = raw.get("agent_runtimes")
    runtimes = dict(existing_runtimes) if isinstance(existing_runtimes, dict) else {}
    runtimes.setdefault("openai-codex", settings)
    known["agent_runtimes"] = runtimes
    return {"connection", "subscription", "codex_cli", "llm_backend"}


def load_config() -> AppConfig:
    path = config_path()
    if not path.exists():
        cfg = AppConfig()
        from .profiles import migrate_legacy_to_profiles, sync_active_profile_to_config

        migrate_legacy_to_profiles(cfg)
        sync_active_profile_to_config(cfg)
        return cfg

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001 - user-controlled file
        raise ConfigError(f"Failed to read config: {path}") from e

    if not isinstance(raw, dict):
        raise ConfigError(f"Invalid config format (expected JSON object): {path}")

    # Allow unknown keys; stash them so we can round-trip. Map deprecated
    # web_search keys into the legacy auto/off values when older configs
    # are loaded.
    known = {k: v for k, v in raw.items() if k in AppConfig.model_fields}
    migrated_runtime_keys = _migrate_legacy_agent_runtime_config(raw, known)
    if "web_search_mode" in raw:
        known["web_search_mode"] = _normalize_web_search_mode(
            raw.get("web_search_mode"),
            allow_legacy_on=True,
        )
    elif "web_search_enabled" in raw:
        known["web_search_mode"] = _coerce_legacy_web_search_mode(raw.get("web_search_enabled"))
    if "web_search_adapter" in raw:
        known["web_search_adapter"] = _normalize_web_search_adapter(raw.get("web_search_adapter"))
    if "web_search_policy" in raw:
        known["web_search_policy"] = _normalize_web_search_policy(raw.get("web_search_policy"))
    if "temperature" in raw:
        legacy_temperature = raw.get("temperature")
        for field in _ROLE_TEMPERATURE_FIELDS.values():
            if field not in raw:
                known[field] = legacy_temperature
    unknown = {
        k: v
        for k, v in raw.items()
        if k not in AppConfig.model_fields
        and k != "web_search_enabled"
        and k not in migrated_runtime_keys
    }
    cfg = AppConfig(**known)
    cfg.extra_fields = unknown
    from .profiles import migrate_legacy_to_profiles, sync_active_profile_to_config

    migrate_legacy_to_profiles(cfg)
    # Applied on the loaded object, not just at use time, so `config show`
    # reports the repo that feedback actually goes to. Redirecting only inside
    # resolve_feedback_github_repo() left the two disagreeing in the same
    # output: the migrated profile name beside the stale repo name.
    cfg.feedback_github_repo = str(
        getattr(cfg, "feedback_github_repo", DEFAULT_FEEDBACK_GITHUB_REPO)
    )
    _migrate_delegated_codex_subscription(
        cfg,
        had_non_default_profile=(
            isinstance(raw.get("profiles"), dict)
            and any(str(name) != "default" for name in raw["profiles"])
        ),
    )
    from .profiles import migrate_subscription_endpoints

    migrate_subscription_endpoints(cfg)
    sync_active_profile_to_config(cfg)
    _canonicalize_active_profile_model(cfg)
    return cfg


def _migrate_delegated_codex_subscription(
    cfg: AppConfig,
    *,
    had_non_default_profile: bool,
) -> bool:
    """Move the original Codex subscription choice onto the native transport.

    The first implementation used ``execution.backend=delegated`` and therefore
    replaced the Alysis Code agent with ``codex exec``. Preserve those runtime
    settings as an inactive fallback, but make the user-facing subscription
    selection a native profile so existing installations receive the corrected
    behavior without repeating setup.
    """

    _ = had_non_default_profile  # Retained for callers from the first migration draft.
    if cfg.extra_fields.get("onboarded") is not True:
        return False
    if cfg.extra_fields.get("preserve_delegated_runtime") is True:
        return False
    if cfg.execution.backend != "delegated" or cfg.execution.runtime != "openai-codex":
        return False
    settings = cfg.agent_runtimes.get("openai-codex")
    if settings is None or str(settings.adapter or "").strip() != "codex-cli":
        return False
    if str(settings.executable or "").strip() not in {"", "codex"}:
        return False
    from .profiles import (
        SUBSCRIPTION_SELECTION_REQUIRED_KEY,
        ProfileSpec,
        add_profile,
        set_active_profile,
    )
    from .provider_auth import provider_auth_connection

    connection = provider_auth_connection("openai-codex")
    if connection is None:
        return False
    model = str(settings.model or "").strip()
    reasoning_effort = str(settings.reasoning_effort or "").strip() or None
    profile = ProfileSpec(
        name="chatgpt-codex",
        protocol=connection.protocol,
        base_url=connection.base_url,
        auth_provider="openai-codex",
        default_model=model,
        reasoning_effort=reasoning_effort,
        notes=(
            "ChatGPT Codex subscription compatibility transport. "
            "Migrated from the delegated Codex runtime."
        ),
    )
    add_profile(cfg, profile, allow_auth_profile_update=True)
    set_active_profile(cfg, profile.name)
    cfg.execution.backend = "native"
    cfg.execution.runtime = None
    cfg.model = model
    effective_effort = None if reasoning_effort in {None, "auto"} else reasoning_effort
    cfg.llm_reasoning_effort = effective_effort
    cfg.llm_enable_thinking = None if effective_effort is None else effective_effort != "none"
    # The delegated CLI owned credentials, so native Alysis Code must explicitly
    # connect its own encrypted provider vault before chat starts.
    cfg.extra_fields["onboarded"] = False
    cfg.extra_fields["subscription_reconnect_required"] = True
    # Preserve any explicit legacy values only as a pre-filled suggestion. Migration
    # itself is not a /config confirmation, so execution remains blocked until the
    # user saves a model + effort from Default Model.
    cfg.extra_fields[SUBSCRIPTION_SELECTION_REQUIRED_KEY] = "openai-codex"
    return True


def save_config(cfg: AppConfig) -> None:
    path = config_path()

    try:
        validated = AppConfig.model_validate(cfg.model_dump())
    except ValidationError as exc:
        raise ConfigError(f"Invalid configuration: {exc.errors()[0]['msg']}") from exc
    data = validated.model_dump()
    # Preserve unknown keys.
    if cfg.extra_fields:
        extra_fields = dict(cfg.extra_fields)
        extra_fields.pop("web_search_enabled", None)
        data.update(extra_fields)

    atomic_write_json(path, data, ensure_ascii=True)


@contextmanager
def config_transaction(
    *,
    loader: Callable[[], AppConfig] | None = None,
    saver: Callable[[AppConfig], None] | None = None,
) -> Iterator[AppConfig]:
    """Serialize a fresh config read, in-memory mutation, and one durable save.

    The sidecar lock is intentionally stable instead of locking ``config.json``
    itself: atomic publication replaces that inode. Callers may inject their
    existing patchable loader and saver; non-canonical stores receive in-process
    serialization only. Nesting fails fast because acquiring the same sidecar a
    second time in one thread is not portable across supported platforms.
    """

    load = load_config if loader is None else loader
    save = save_config if saver is None else saver
    canonical_store = (loader is None or loader is load_config) and (
        saver is None or saver is save_config
    )
    with _CONFIG_STORE_LOCK:
        if getattr(_CONFIG_TRANSACTION_STATE, "active", False):
            raise ConfigError("Nested config transactions are not supported.")
        _CONFIG_TRANSACTION_STATE.active = True
        try:
            if not canonical_store:
                cfg = load()
                yield cfg
                save(cfg)
                return

            path = config_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            lock_path = _config_lock_path(path)
            with lock_path.open("a+b") as handle:
                _acquire_config_file_lock(handle)
                try:
                    cfg = load()
                    yield cfg
                    save(cfg)
                finally:
                    _release_config_file_lock(handle)
        finally:
            _CONFIG_TRANSACTION_STATE.active = False


def _canonicalize_active_profile_model(cfg: AppConfig) -> None:
    active_profile = str((cfg.extra_fields or {}).get("active_profile") or "").strip()
    model = str(getattr(cfg, "model", "") or "").strip()
    if not active_profile or not model:
        return

    from .profile_presets import find_preset_for_profile, get_preset
    from .profiles import get_profile, update_active_profile_defaults

    profile = get_profile(cfg, active_profile)
    if profile is not None and profile.auth_provider:
        return
    preset = find_preset_for_profile(profile) if profile is not None else None
    preset = preset or get_preset(active_profile)
    canonical = _canonicalize_model_for_config(cfg, model, active_preset=preset)
    if canonical == model:
        return
    cfg.model = canonical
    update_active_profile_defaults(cfg, default_model=canonical)


def _canonicalize_model_for_config(
    cfg: AppConfig,
    model: str,
    *,
    active_preset: Any | None = None,
) -> str:
    raw = str(model or "").strip()
    if not raw:
        return raw

    active_profile = str((cfg.extra_fields or {}).get("active_profile") or "").strip()
    active_profile_obj = None
    if active_profile:
        from .profiles import get_profile

        active_profile_obj = get_profile(cfg, active_profile)

    if active_preset is None and active_profile:
        from .profile_presets import find_preset_for_profile, get_preset

        active_preset = (
            find_preset_for_profile(active_profile_obj) if active_profile_obj is not None else None
        )
        active_preset = active_preset or get_preset(active_profile)

    if active_preset is not None:
        active_model = _canonicalize_model_for_preset(raw, active_preset)
        if active_model is not None:
            return active_model
        if _provider_switch_is_unsafe(active_preset):
            return raw
    elif active_profile_obj is not None:
        from .profiles import DEFAULT_OPENAI_BASE_URL

        base_url = str(getattr(active_profile_obj, "base_url", "") or "").strip().rstrip("/")
        default_base_url = DEFAULT_OPENAI_BASE_URL.strip().rstrip("/")
        if base_url and base_url != default_base_url:
            return raw

    matches = _matching_model_presets(raw)
    direct_matches = tuple(
        (preset, canonical)
        for preset, canonical in matches
        if not _provider_switch_is_unsafe(preset)
    )
    if len(direct_matches) == 1:
        preset, canonical = direct_matches[0]
        _align_active_profile_to_preset(cfg, preset)
        return canonical
    if len(matches) == 1:
        preset, canonical = matches[0]
        _align_active_profile_to_preset(cfg, preset)
        return canonical
    return raw


def _canonicalize_model_for_preset(model: str, preset: Any) -> str | None:
    lookup = _build_profile_model_lookup_index(preset)
    for alias in _iter_model_lookup_aliases(model):
        canonical = lookup.get(alias.casefold())
        if canonical:
            return canonical
    return None


def _build_profile_model_lookup_index(preset: Any) -> dict[str, str]:
    index: dict[str, str] = {}
    for model in getattr(preset, "suggested_models", ()):
        canonical = str(model or "").strip()
        if not canonical:
            continue
        for alias in _iter_model_lookup_aliases(canonical):
            index.setdefault(alias.casefold(), canonical)
    model_aliases = getattr(preset, "model_aliases", {}) or {}
    if isinstance(model_aliases, dict):
        for raw_alias, raw_target in model_aliases.items():
            alias_text = str(raw_alias or "").strip()
            target_text = str(raw_target or "").strip()
            if not alias_text or not target_text:
                continue
            canonical = next(
                (
                    index[target_alias.casefold()]
                    for target_alias in _iter_model_lookup_aliases(target_text)
                    if target_alias.casefold() in index
                ),
                target_text,
            )
            for alias in _iter_model_lookup_aliases(alias_text):
                index.setdefault(alias.casefold(), canonical)
    return index


def _iter_model_lookup_aliases(raw: str) -> tuple[str, ...]:
    value = str(raw or "").strip()
    if not value:
        return ()

    candidates: list[str] = [value]
    if "/" in value:
        provider_stripped = value.rsplit("/", 1)[-1].strip()
        if provider_stripped:
            candidates.append(provider_stripped)

    out: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        normalized_separators = re.sub(r"[\s_]+", "-", candidate.strip())
        variants = (
            candidate.strip(),
            normalized_separators,
            re.sub(r"(?<=\d)-(?=\d)", ".", normalized_separators),
            re.sub(r"(?<=\d)\.(?=\d)", "-", normalized_separators),
        )
        for variant in variants:
            clean = variant.strip()
            if not clean:
                continue
            folded = clean.casefold()
            if folded in seen:
                continue
            seen.add(folded)
            out.append(clean)
    return tuple(out)


def _matching_model_presets(model: str) -> tuple[tuple[Any, str], ...]:
    from .profile_presets import PROFILE_PRESETS

    matches: list[tuple[Any, str]] = []
    for preset in PROFILE_PRESETS:
        if preset.key in {
            "custom",
            "openai-responses",
            "anthropic-compat",
            "anthropic-native",
            "gemini-compat",
            "gemini-native",
        }:
            continue
        canonical = _canonicalize_model_for_preset(model, preset)
        if canonical is not None:
            matches.append((preset, canonical))
    return tuple(matches)


def _provider_switch_is_unsafe(preset: Any) -> bool:
    # Hosted account credentials cannot authenticate a direct provider. Keep an
    # unsupported model on the selected gateway until the user changes it.
    return str(getattr(preset, "key", "") or "").strip().lower() in {
        "alysis",
        "openrouter",
        "custom",
    }


def _align_active_profile_to_preset(
    cfg: AppConfig,
    preset: Any,
    *,
    base_url: str | None = None,
    default_model: str | None = None,
) -> None:
    from .profile_presets import make_profile_from_preset
    from .profiles import (
        add_profile,
        set_active_profile,
        update_active_profile_defaults,
    )

    profile_name = _find_profile_name_for_preset(cfg, preset)
    if profile_name is None:
        profile_name = _next_profile_name_for_preset(cfg, preset)
        add_profile(cfg, make_profile_from_preset(preset, name=profile_name))
    set_active_profile(cfg, profile_name)
    update_active_profile_defaults(
        cfg,
        base_url=base_url,
        default_model=default_model,
    )


def _find_profile_name_for_preset(cfg: AppConfig, preset: Any) -> str | None:
    from .profile_presets import find_preset_for_profile
    from .profiles import list_profiles

    active = str((cfg.extra_fields or {}).get("active_profile") or "").strip()
    profiles = list_profiles(cfg)
    for profile in profiles:
        if profile.name != active:
            continue
        matched = find_preset_for_profile(profile)
        if matched is not None and matched.key == preset.key:
            return profile.name
    for profile in profiles:
        matched = find_preset_for_profile(profile)
        if matched is not None and matched.key == preset.key:
            return profile.name
    return None


def _next_profile_name_for_preset(cfg: AppConfig, preset: Any) -> str:
    from .profiles import list_profiles

    existing = {profile.name for profile in list_profiles(cfg)}
    base = str(getattr(preset, "key", "") or "provider").strip().lower()
    if base not in existing:
        return base
    for suffix in range(2, 100):
        candidate = f"{base}-{suffix}"
        if candidate not in existing:
            return candidate
    raise ConfigError(f"Could not allocate a profile name for provider preset {base!r}.")


def _load_credentials_data() -> dict[str, Any]:
    path = credentials_path()
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001 - user-controlled file
        raise ConfigError(f"Failed to read persisted API key: {path}") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"Invalid persisted API key format (expected JSON object): {path}")
    return dict(raw)


def _save_credentials_data(data: dict[str, Any]) -> None:
    path = credentials_path()
    clean_data = dict(data)
    if not clean_data:
        if path.exists():
            path.unlink()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(clean_data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def load_persisted_api_key() -> str | None:
    raw = _load_credentials_data()
    value = str(raw.get("api_key") or "").strip()
    return value or None


def save_persisted_api_key(api_key: str) -> None:
    normalized = str(api_key or "").strip()
    if not normalized:
        raise ConfigError("API key is empty.")
    data = _load_credentials_data()
    data["api_key"] = normalized
    _save_credentials_data(data)


def clear_persisted_api_key() -> bool:
    data = _load_credentials_data()
    if "api_key" not in data:
        return False
    data.pop("api_key", None)
    _save_credentials_data(data)
    return True


def load_persisted_profile_keys() -> dict[str, str]:
    raw = _load_credentials_data().get("profile_keys")
    if not isinstance(raw, dict):
        return {}
    keys: dict[str, str] = {}
    for name, value in raw.items():
        profile_name = str(name or "").strip().lower()
        key = str(value or "").strip()
        if profile_name and key:
            keys[profile_name] = key
    return keys


def save_persisted_profile_key(profile_name: str, value: str) -> None:
    from .profiles import ProfileSpec

    normalized_name = ProfileSpec(name=str(profile_name or "").strip().lower()).name
    normalized_key = str(value or "").strip()
    if not normalized_key:
        raise ConfigError("API key is empty.")
    data = _load_credentials_data()
    profile_keys = data.get("profile_keys")
    if not isinstance(profile_keys, dict):
        profile_keys = {}
    profile_keys[normalized_name] = normalized_key
    data["profile_keys"] = dict(sorted(profile_keys.items()))
    _save_credentials_data(data)


def clear_persisted_profile_key(profile_name: str) -> bool:
    from .profiles import ProfileSpec

    normalized_name = ProfileSpec(name=str(profile_name or "").strip().lower()).name
    data = _load_credentials_data()
    profile_keys = data.get("profile_keys")
    if not isinstance(profile_keys, dict) or normalized_name not in profile_keys:
        return False
    profile_keys.pop(normalized_name, None)
    if profile_keys:
        data["profile_keys"] = dict(sorted(profile_keys.items()))
    else:
        data.pop("profile_keys", None)
    _save_credentials_data(data)
    return True


def rename_persisted_profile_key(old_name: str, new_name: str) -> bool:
    from .profiles import ProfileSpec

    old_profile = ProfileSpec(name=str(old_name or "").strip().lower()).name
    new_profile = ProfileSpec(name=str(new_name or "").strip().lower()).name
    data = _load_credentials_data()
    profile_keys = data.get("profile_keys")
    if not isinstance(profile_keys, dict) or old_profile not in profile_keys:
        return False
    profile_keys[new_profile] = profile_keys.pop(old_profile)
    data["profile_keys"] = dict(sorted(profile_keys.items()))
    _save_credentials_data(data)
    return True


def resolve_profile_api_key(
    cfg: AppConfig,
    profile_name: str,
) -> ApiKeyResolution:
    from .profiles import get_profile

    profile = get_profile(cfg, profile_name)
    if profile is None:
        return ApiKeyResolution(key=None, source="missing")
    ide_key = _resolve_ide_profile_api_key(cfg, profile_name)
    if ide_key.key:
        return ide_key
    stored_profile_key = _resolve_stored_profile_api_key(cfg, profile_name)
    if stored_profile_key.key:
        return stored_profile_key
    env_name = str(profile.api_key_env or "").strip()
    if env_name:
        env_key = str(env_get(env_name) or "").strip()
        if env_key:
            return ApiKeyResolution(key=env_key, source=f"env:{env_name}")
    active_profile = str((cfg.extra_fields or {}).get("active_profile") or "").strip()
    if profile.name == active_profile:
        legacy_key = load_persisted_api_key()
        if legacy_key:
            return ApiKeyResolution(key=legacy_key, source="stored:legacy")
    if profile.name == "openai":
        openai_key = str(os.environ.get("OPENAI_API_KEY") or "").strip()
        if openai_key:
            return ApiKeyResolution(key=openai_key, source="env:OPENAI_API_KEY")
    return ApiKeyResolution(key=None, source="missing")


def _resolve_ide_profile_api_key(cfg: AppConfig, profile_name: str) -> ApiKeyResolution:
    """Honor an IDE's explicitly supplied profile key without overwriting CLI credentials."""
    from .profiles import get_profile

    profile = get_profile(cfg, profile_name)
    if profile is not None:
        # Match the extension's encoding; plain env-var names can collide between two profiles.
        encoded = profile.name.encode("utf-8").hex().upper()
        env_name = f"ALYSIS_IDE_PROFILE_{encoded}_API_KEY"
        key = str(os.environ.get(env_name) or "").strip()
        if key:
            return ApiKeyResolution(key=key, source=f"env:{env_name}")
    return ApiKeyResolution(key=None, source="missing")


def _resolve_stored_profile_api_key(cfg: AppConfig, profile_name: str) -> ApiKeyResolution:
    from .profiles import get_profile

    profile = get_profile(cfg, profile_name)
    if profile is None:
        return ApiKeyResolution(key=None, source="missing")
    profile_key = load_persisted_profile_keys().get(profile.name)
    if profile_key:
        return ApiKeyResolution(key=profile_key, source=f"stored:profile={profile.name}")
    return ApiKeyResolution(key=None, source="missing")


def resolve_api_key(
    cfg: AppConfig | None = None,
    *,
    profile_name: str | None = None,
) -> ApiKeyResolution:
    effective_cfg = cfg or load_config()
    if profile_name is None:
        profile_name = str((effective_cfg.extra_fields or {}).get("active_profile") or "").strip()
    if profile_name:
        ide_key = _resolve_ide_profile_api_key(effective_cfg, profile_name)
        if ide_key.key:
            return ide_key
        stored_profile_key = _resolve_stored_profile_api_key(effective_cfg, profile_name)
        if stored_profile_key.key:
            return stored_profile_key
    prefer_profile_scoped = bool(
        profile_name and _should_prefer_profile_scoped_api_key(effective_cfg, profile_name)
    )
    if profile_name and prefer_profile_scoped:
        resolved = resolve_profile_api_key(effective_cfg, profile_name)
        if resolved.key and _is_profile_scoped_api_key_source(resolved.source):
            return resolved
    alysis_key = str(env_get("ALYSIS_API_KEY") or "").strip()
    if alysis_key and not prefer_profile_scoped:
        return ApiKeyResolution(key=alysis_key, source="env:ALYSIS_API_KEY")
    if profile_name:
        resolved = resolve_profile_api_key(effective_cfg, profile_name)
        if resolved.key and (not prefer_profile_scoped or resolved.source != "env:OPENAI_API_KEY"):
            return resolved
    legacy_key = load_persisted_api_key()
    if legacy_key:
        return ApiKeyResolution(key=legacy_key, source="stored:legacy")
    if prefer_profile_scoped:
        return ApiKeyResolution(key=None, source="missing")
    openai_key = str(os.environ.get("OPENAI_API_KEY") or "").strip()
    if openai_key:
        return ApiKeyResolution(key=openai_key, source="env:OPENAI_API_KEY")
    return ApiKeyResolution(key=None, source="missing")


def _should_prefer_profile_scoped_api_key(cfg: AppConfig, profile_name: str) -> bool:
    from .profiles import DEFAULT_OPENAI_BASE_URL, get_profile, resolve_effective_base_url

    profile = get_profile(cfg, profile_name)
    if profile is None:
        return False
    if profile.name not in {"default", "openai"}:
        return True
    effective_base_url = resolve_effective_base_url(cfg=cfg, profile=profile)
    return effective_base_url.rstrip("/") != DEFAULT_OPENAI_BASE_URL.rstrip("/")


def _is_profile_scoped_api_key_source(source: str) -> bool:
    normalized = str(source or "").strip()
    if normalized.startswith("stored:profile="):
        return True
    if not normalized.startswith("env:"):
        return False
    env_name = normalized.removeprefix("env:")
    return env_name not in {"ALYSIS_API_KEY", "OPENAI_API_KEY"}


def get_api_key(cfg: AppConfig | None = None) -> str:
    resolved = resolve_api_key(cfg)
    if not resolved.key:
        raise ConfigError(_missing_api_key_message(cfg or load_config()))
    return resolved.key


def resolve_model_access_api_key(
    cfg: AppConfig,
    *,
    override: str | None = None,
    legacy_resolver: Callable[[], str] | None = None,
) -> str:
    """Resolve a static key, or an empty placeholder for an auth-backed profile."""

    if override is not None:
        normalized = override.strip()
        if normalized:
            return normalized
    try:
        from .profiles import get_active_profile

        if get_active_profile(cfg).auth_provider:
            return ""
    except ConfigError:
        pass
    if override is not None:
        raise ConfigError("API key is empty.")
    if legacy_resolver is not None:
        return legacy_resolver()
    try:
        return get_api_key(cfg)
    except ConfigError:
        # ``ALYSIS_API_KEY`` is an explicit key for the selected Alysis Code
        # connection. Preserve its historical use for custom-base profiles
        # without falling back to a possibly unrelated ``OPENAI_API_KEY``.
        alysis_key = str(env_get("ALYSIS_API_KEY") or "").strip()
        if alysis_key:
            return alysis_key
        raise


def ensure_subscription_menu_managed_key(cfg: AppConfig, key: str) -> None:
    """Reject raw edits that would split an auth profile's model/effort selection."""

    normalized = str(key or "").strip().lower()
    if normalized not in {
        "model",
        "base_url",
        "llm_reasoning_effort",
        "llm_enable_thinking",
    }:
        return
    try:
        from .profiles import get_active_profile

        auth_provider = get_active_profile(cfg).auth_provider
    except ConfigError:
        auth_provider = None
    if auth_provider:
        raise ConfigError(
            "Subscription model, reasoning effort, and endpoint settings are managed in "
            "/config → Default Model."
        )


def _missing_api_key_message(cfg: AppConfig) -> str:
    suggestions: list[str] = ["set ALYSIS_API_KEY"]
    try:
        from .profiles import (
            DEFAULT_OPENAI_BASE_URL,
            get_active_profile,
            resolve_effective_base_url,
        )

        profile = get_active_profile(cfg)
        effective_base_url = resolve_effective_base_url(cfg=cfg, profile=profile)
    except ConfigError:
        profile = None
        effective_base_url = ""
    is_openai_endpoint = effective_base_url.rstrip("/") == DEFAULT_OPENAI_BASE_URL.rstrip("/")
    if profile is not None:
        if profile.api_key_env and (profile.api_key_env != "OPENAI_API_KEY" or is_openai_endpoint):
            suggestions.insert(0, f"set {profile.api_key_env}")
        suggestions.append(f"run `alysis profile set-key {profile.name} --key <key>`")
        if is_openai_endpoint:
            suggestions.append("set OPENAI_API_KEY")
    suggestions.append("run `alysis config set-api-key`")
    sentences = [suggestion[0].upper() + suggestion[1:] for suggestion in suggestions]
    # A missing key is almost always a "which store did this process read?" question, so
    # the error names the exact file and profile this process consulted. Paths and
    # booleans only — never key material.
    try:
        cred_path = credentials_path()
        stored = load_persisted_profile_keys()
        # `exists` alone proved ambiguous in the field (a file that exists for one process and
        # not for another), so say *why*: a raw stat result is unambiguous where Path.exists()
        # swallows the error, and the process identity explains which environment looked.
        try:
            os.stat(cred_path)
            stat_note = "stat=ok"
        except OSError as stat_error:
            stat_note = f"stat={type(stat_error).__name__}:{stat_error.errno}"
        diag = (
            f" [looked in: {cred_path} (exists={cred_path.exists()}, {stat_note}); "
            f"config_dir_override={'set' if env_get('ALYSIS_CONFIG_DIR') else 'unset'}; "
            f"active_profile={profile.name if profile is not None else '<unresolved>'}; "
            f"stored_profiles={sorted(stored.keys()) or 'none'}; "
            f"legacy_key={'yes' if load_persisted_api_key() else 'no'}; "
            f"pid={os.getpid()}; python={sys.executable}; cwd={os.getcwd()}]"
        )
    except Exception:  # noqa: BLE001 - diagnostics must never mask the real error
        diag = ""
    return "Missing API key. " + "; or ".join(sentences) + "." + diag


_SETTABLE_KEYS: set[str] = {
    "execution.backend",
    "execution.runtime",
    "base_url",
    "model",
    "prompt_guidance.default",
    "prompt_guidance.model_profiles",
    "llm_timeout_s",
    "llm_stream_no_progress_timeout_s",
    "llm_stream_assembly",
    "run_deadline_seconds",
    "llm_enable_thinking",
    "llm_reasoning_effort",
    "provider_concurrency_caps",
    "provider_retry_max_retries",
    "provider_retry_base_delay_seconds",
    "provider_retry_max_delay_seconds",
    "model_metadata_policy",
    "default_mode",
    "default_persona",
    "persona_modes_enabled",
    "max_steps",
    "temperature",
    "coding_temperature",
    "review_temperature",
    "planner_temperature",
    "conflict_review_temperature",
    "compactor_temperature",
    "chat_temperature",
    "sampling_temperature",
    "sampling_top_p",
    "sampling_seed",
    "stream",
    "routing_mode",
    "route_arbitration_enabled",
    "semantic_turn_contract_enabled",
    "unified_turn_path_enabled",
    "reply_language",
    "evidence_v2_enabled",
    "regression_baseline_enabled",
    "turn_contract_v2_enabled",
    "reproduction_first_enabled",
    "blast_radius_gate_enabled",
    "blast_radius_max_scope_files",
    "blast_radius_scope_seconds_cap",
    "blast_radius_over_broad_threshold",
    "process_reaping_enabled",
    "workspace_provisioning_enabled",
    "step_budget_policy",
    "subagents_enabled",
    "skills_enabled",
    "bundled_skills_enabled",
    "skills_auto_invoke",
    "experimental_gemini_interactions_enabled",
    "custom_tools_enabled",
    "task_max_steps",
    "subagent_max_steps",
    "subagent_timeout_s",
    "subagent_orchestration.max_background_children",
    "subagent_orchestration.turn_end_policy",
    "subagent_orchestration.workspace_isolation_enabled",
    "subagent_orchestration.parallel_nonwriting_shared",
    "subagent_orchestration.helpers_enabled",
    "subagent_orchestration.helper_max_total_per_child",
    "subagent_orchestration.helper_timeout_s",
    "subagent_orchestration.helper_max_steps",
    "subagent_orchestration.repetition_signal_threshold",
    "subagent_orchestration.repetition_nudge_occurrence_threshold",
    "subagent_orchestration.repetition_occurrence_threshold",
    "subagent_orchestration.repetition_backstop_threshold",
    "subagent_orchestration.model_response_activity_after_s",
    "subagent_orchestration.inactivity_signal_after_s",
    "subagent_orchestration.inflight_deadline_grace_s",
    "cache.prompt_cache_key_enabled",
    "cache.keepalive_enabled",
    "cache.keepalive_idle_threshold_s",
    "read_ledger_enabled",
    "web_fetch_trusted_domains",
    "web_tools_enabled",
    "web_search_mode",
    "web_search_policy",
    "web_search_enabled",
    "web_search_adapter",
    "web_search_base_url",
    "web_search_model",
    "web_search_timeout_s",
    "image_generation.enabled",
    "image_generation.model",
    "image_generation.base_url",
    "image_generation.api_key_env",
    "image_generation.timeout_s",
    "image_generation.max_images_per_call",
    "image_generation.max_image_bytes",
    "image_generation.max_pixels",
    "update_check_enabled",
    "update_check_interval_hours",
    "update_check_timeout_s",
    "update_prompt_enabled",
    "feedback_github_enabled",
    "feedback_github_repo",
    "feedback_open_browser",
    "session_log_dir",
    "crash_diagnostic_log_path",
    "prompt_cache_mode",
    "prompt_cache_key",
    "prompt_cache_retention",
    "anthropic_prompt_cache_enabled",
    "anthropic_prompt_cache_ttl",
    "verify_commands",
    "integration_verify_mode",
    "integration_verify_commands",
    "replanning_mode",
    "toolbar_items",
    "assets.enabled",
    "assets.comprehension.role",
    "assets.comprehension.vision_fallback_profile",
    "assets.comprehension.vision_with_ocr_when_available",
    "assets.comprehension.ocr_enabled",
    "assets.comprehension.ocr_provider",
    "assets.comprehension.ocr_timeout_seconds",
    "assets.comprehension.image_max_edge_pixels",
    "assets.comprehension.questioning_mode",
    "assets.comprehension.schema_version",
    "assets.planner.inline_images",
    "assets.planner.max_inline_images",
    "assets.planner.readiness_policy",
    "assets.planner.readiness_timeout_seconds",
    "assets.planner.max_chars_per_asset",
    "assets.planner.max_primary_per_task",
    "assets.worker.inline_images",
    "assets.worker.max_inline_images",
    "assets.worker.fail_on_mirror_error",
    "assets.worker.allocator_role",
    "assets.worker.allocator_timeout_seconds",
    "assets.worker.max_chars_per_asset_block",
    "assets.worker.max_focused_extract_chars",
    "assets.worker.schema_version",
}

_VERIFY_LIKE_MODES: set[str] = {"off", "warn", "strict"}
_MODEL_METADATA_POLICIES: set[str] = {"warn", "strict"}
_ROLE_MODEL_NAMES: set[str] = {
    "coding",
    "planner",
    "review",
    "compactor",
    "conflict_review",
    "conflict_resolve",
    "comprehension",
    "router",
    "approval_review",
}
_ROLE_MODEL_CONFIG_PREFIXES: set[str] = {"role_models", "forge_role_models"}
# Persona vocabulary for persona_models.<persona> keys and default_persona
# validation. Kept as a local literal set like _ROLE_MODEL_NAMES (the runtime
# registry lives in personas.BUILTIN_PERSONAS).
_PERSONA_NAMES: set[str] = {"code", "architect", "ask", "debug"}
_AGENT_RUNTIME_CONFIG_FIELDS: set[str] = {
    "adapter",
    "executable",
    "provider_managed_auth",
    "model",
    "reasoning_effort",
    "timeout_seconds",
}


def _parse_command_list(value: str, *, key: str, allow_empty: bool) -> list[str]:
    raw = value.strip()
    if not raw:
        raise ConfigError(f"{key} must be a JSON array of command strings")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ConfigError(f"{key} must be valid JSON array") from e
    if not isinstance(parsed, list):
        raise ConfigError(f"{key} must be a JSON array")
    commands: list[str] = []
    for item in parsed:
        cmd = str(item).strip()
        if cmd:
            commands.append(cmd)
    if not commands and not allow_empty:
        raise ConfigError(f"{key} cannot be empty")
    return commands


def _parse_provider_concurrency_caps(value: str, *, key: str) -> dict[str, int]:
    raw = value.strip()
    if not raw:
        raise ConfigError(f"{key} must be a JSON object mapping provider keys to integer caps")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ConfigError(f"{key} must be valid JSON object") from e
    if not isinstance(parsed, dict):
        raise ConfigError(f"{key} must be a JSON object")

    caps: dict[str, int] = {}
    for raw_provider, raw_cap in parsed.items():
        provider = str(raw_provider or "").strip().lower()
        if not provider:
            raise ConfigError(f"{key} provider keys must be non-empty strings")
        try:
            cap = int(raw_cap if raw_cap is not None else 0)
        except (TypeError, ValueError) as e:
            raise ConfigError(f"{key}.{provider} must be an integer >= 0") from e
        if cap < 0:
            raise ConfigError(f"{key}.{provider} must be an integer >= 0")
        caps[provider] = cap
    return caps


def _coerce_non_negative_float(value: str, *, key: str) -> float:
    try:
        parsed = float(value)
    except ValueError as e:
        raise ConfigError(f"{key} must be a number") from e
    if parsed < 0:
        raise ConfigError(f"{key} must be >= 0")
    return parsed


def _coerce_positive_float(value: str, *, key: str) -> float:
    parsed = _coerce_non_negative_float(value, key=key)
    if parsed <= 0 or not math.isfinite(parsed):
        raise ConfigError(f"{key} must be > 0")
    return parsed


def _coerce_optional_positive_float(value: Any, *, key: str) -> float | None:
    if value is None:
        return None
    normalized = str(value).strip()
    if normalized.lower() in {"", "none", "null", "default", "off", "unlimited", "never"}:
        return None
    try:
        parsed = float(normalized)
    except (TypeError, ValueError) as e:
        raise ConfigError(f"{key} must be a finite number > 0") from e
    if parsed <= 0 or not math.isfinite(parsed):
        raise ConfigError(f"{key} must be a finite number > 0")
    return parsed


def _coerce_optional_positive_int(value: Any, *, key: str) -> int | None:
    if value is None:
        return None
    normalized = str(value).strip()
    if normalized.lower() in {"", "none", "null", "default", "off", "unlimited", "never"}:
        return None
    try:
        parsed = int(normalized, 10)
    except (TypeError, ValueError) as e:
        raise ConfigError(f"{key} must be an integer > 0 or unlimited") from e
    if parsed <= 0:
        raise ConfigError(f"{key} must be an integer > 0 or unlimited")
    return parsed


def _coerce_optional_sampling_value(value: Any, *, key: str) -> float | int | None:
    """Validate one sampling determinism control for ``config set``.

    Bounds are imported from :mod:`run_provenance` rather than restated, so a
    value an operator can persist is exactly a value the runtime will honour;
    two copies of these ranges would drift and produce a setting that saves
    cleanly and is then silently ignored at request time.

    Unlike the environment path -- which ignores a bad value and warns, because
    a typo in a benchmark harness must not abort a campaign -- this raises.
    ``config set`` is interactive, and swallowing a value the operator just
    typed would be the worse failure.
    """
    from .run_provenance import SEED_RANGE, TEMPERATURE_RANGE, TOP_P_RANGE

    normalized = "" if value is None else str(value).strip()
    if normalized.lower() in {"", "none", "null", "default", "off", "unset"}:
        return None
    if key == "sampling_seed":
        try:
            parsed_seed = int(normalized, 10)
        except (TypeError, ValueError) as e:
            raise ConfigError(f"{key} must be an integer") from e
        if not SEED_RANGE[0] <= parsed_seed <= SEED_RANGE[1]:
            raise ConfigError(f"{key} must fit in a signed 64-bit integer")
        return parsed_seed
    low, high = TEMPERATURE_RANGE if key == "sampling_temperature" else TOP_P_RANGE
    try:
        parsed = float(normalized)
    except (TypeError, ValueError) as e:
        raise ConfigError(f"{key} must be a number between {low} and {high}") from e
    if not math.isfinite(parsed) or parsed < low or parsed > high:
        raise ConfigError(f"{key} must be a number between {low} and {high}")
    return parsed


def _coerce_optional_bool(value: str, *, key: str) -> bool | None:
    normalized = value.strip().lower()
    if normalized in {"", "auto", "default", "none", "null"}:
        return None
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{key} must be true/false or auto")


def _coerce_reasoning_effort(value: Any, *, key: str) -> str | None:
    normalized = str(value or "").strip().lower()
    if normalized in {"", "auto", "default"}:
        return None
    if normalized in _REASONING_EFFORTS:
        return normalized
    allowed = ", ".join(sorted((*_REASONING_EFFORTS, "auto")))
    raise ConfigError(f"{key} must be one of: {allowed}")


def _coerce_bool(value: str, *, key: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{key} must be true/false")


def _coerce_github_repo(value: str, *, key: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ConfigError(f"{key} must be a GitHub repo in owner/name form")
    if raw.startswith("https://") or raw.startswith("http://"):
        try:
            parsed = urlsplit(raw)
        except ValueError as e:
            raise ConfigError(f"{key} must be a GitHub repo or GitHub URL") from e
        hostname = (parsed.hostname or "").rstrip(".").lower()
        if hostname != "github.com":
            raise ConfigError(f"{key} URL must use github.com")
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) < 2:
            raise ConfigError(f"{key} GitHub URL must include owner and repo")
        raw = f"{parts[0]}/{parts[1]}"
    raw = raw.removesuffix(".git")
    if not _GITHUB_REPO_RE.fullmatch(raw):
        raise ConfigError(f"{key} must be a GitHub repo in owner/name form")
    return raw


def _coerce_positive_int(value: str, *, key: str) -> int:
    try:
        parsed = int(value)
    except ValueError as e:
        raise ConfigError(f"{key} must be an integer") from e
    if parsed <= 0:
        raise ConfigError(f"{key} must be > 0")
    return parsed


def _coerce_non_negative_int(value: str, *, key: str) -> int:
    try:
        parsed = int(value)
    except ValueError as e:
        raise ConfigError(f"{key} must be an integer") from e
    if parsed < 0:
        raise ConfigError(f"{key} must be >= 0")
    return parsed


def _coerce_min_int(value: str, *, key: str, minimum: int) -> int:
    try:
        parsed = int(value)
    except ValueError as e:
        raise ConfigError(f"{key} must be an integer") from e
    if parsed < minimum:
        raise ConfigError(f"{key} must be >= {minimum}")
    return parsed


def _resolve_positive_timeout(raw: Any) -> float | None:
    try:
        parsed = float(raw)
    except (TypeError, ValueError):
        return None
    if parsed <= 0 or not math.isfinite(parsed):
        return None
    return parsed


def _is_dashscope_base_url(base_url: str | None) -> bool:
    normalized = str(base_url or "").strip()
    if not normalized:
        return False
    try:
        parsed = urlsplit(normalized)
    except ValueError:
        return False
    if parsed.scheme.lower() != "https":
        return False
    hostname = (parsed.hostname or "").rstrip(".").lower()
    return (
        hostname == "dashscope.aliyuncs.com"
        or hostname == "dashscope-intl.aliyuncs.com"
        or hostname == "dashscope-us.aliyuncs.com"
        or hostname.endswith(".dashscope.aliyuncs.com")
        or hostname.endswith(".dashscope-intl.aliyuncs.com")
        or hostname.endswith(".dashscope-us.aliyuncs.com")
        or (hostname.endswith(".aliyuncs.com") and ".dashscope-" in f".{hostname}")
    )


def _is_qwen_model(model: str | None) -> bool:
    normalized = str(model or "").strip().lower()
    if "/" in normalized:
        normalized = normalized.rsplit("/", 1)[-1]
    return normalized.startswith("qwen")


def _normalize_web_search_mode(raw: Any, *, allow_legacy_on: bool = False) -> str:
    value = str(raw or "").strip().lower()
    if allow_legacy_on and value == "on":
        return "auto"
    if value in _WEB_SEARCH_MODES:
        return value
    allowed = ", ".join(sorted(_WEB_SEARCH_MODES))
    raise ConfigError(f"web_search_mode must be one of: {allowed}")


def _normalize_web_search_adapter(raw: Any) -> str:
    try:
        return normalize_web_search_adapter(raw)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc


def _normalize_web_search_policy(raw: Any) -> str:
    try:
        return normalize_web_search_policy(raw)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc


def _coerce_legacy_web_search_mode(raw: Any) -> str:
    if isinstance(raw, bool):
        return "auto" if raw else "off"
    value = str(raw or "").strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return "auto"
    if value in {"0", "false", "no", "off"}:
        return "off"
    raise ConfigError("web_search_enabled must be true/false")


def _apply_legacy_temperature_override(cfg: AppConfig, temperature: float) -> None:
    cfg.temperature = temperature
    cfg.coding_temperature = temperature
    cfg.review_temperature = temperature
    cfg.planner_temperature = temperature
    cfg.conflict_review_temperature = temperature
    cfg.compactor_temperature = temperature
    cfg.chat_temperature = temperature


@dataclass(frozen=True)
class ResolvedLlmTimeout:
    """The per-request LLM timeout that will actually be enforced, and why.

    Exists because the config snapshot used to record only the raw
    ``AppConfig.llm_timeout_s`` field. A campaign that set
    ``ALYSIS_LLM_TIMEOUT_S=240`` then produced 89 session logs all reporting
    ``llm_timeout_s: 60.0`` -- the unset field default -- and the teardown
    concluded the operator's value had been dropped. It had not; the artifact
    was answering a different question than the one every reader asked of it.
    A resolved value must carry its source so the snapshot can state what was
    enforced rather than requiring readers to re-derive the precedence chain.
    """

    seconds: float
    source: str


def resolve_llm_timeout(cfg: AppConfig | None) -> ResolvedLlmTimeout:
    """Resolve the enforced LLM timeout: environment, config, then default.

    Source vocabulary matches ``resolve_run_deadline``: ``environment``,
    ``config``, ``runtime_default``.
    """
    env_timeout = _resolve_positive_timeout(env_get("ALYSIS_LLM_TIMEOUT_S"))
    if env_timeout is not None:
        return ResolvedLlmTimeout(seconds=env_timeout, source="environment")
    cfg_timeout = _resolve_positive_timeout(getattr(cfg, "llm_timeout_s", None))
    if cfg_timeout is not None:
        return ResolvedLlmTimeout(seconds=cfg_timeout, source="config")
    return ResolvedLlmTimeout(seconds=60.0, source="runtime_default")


def resolve_llm_timeout_s(cfg: AppConfig | None) -> float:
    return resolve_llm_timeout(cfg).seconds


@dataclass(frozen=True)
class ResolvedRunDeadline:
    seconds: float | None
    source: str

    @property
    def unlimited(self) -> bool:
        """True when no finite budget applies, however that was decided."""
        return self.seconds is None


def resolve_run_deadline(
    cfg: AppConfig | None,
    *,
    cli_deadline_seconds: float | None = None,
    cli_no_deadline: bool = False,
    default_seconds: float | None = None,
) -> ResolvedRunDeadline:
    """Resolve the wall-clock budget for one run.

    Precedence is CLI, environment, config, then ``default_seconds``. Each of
    the first three can select unlimited explicitly, and doing so stops the
    search rather than falling through to the default -- otherwise there would
    be no way to turn the default off. Unlimited is spelled with a word
    (``unlimited``, ``never``, ``off``, ``none``); ``0`` remains invalid, since
    a zero-second budget is exhausted before it starts.
    """
    if cli_no_deadline:
        return ResolvedRunDeadline(seconds=None, source="explicit_cli")

    if cli_deadline_seconds is not None:
        return ResolvedRunDeadline(
            seconds=_coerce_optional_positive_float(
                cli_deadline_seconds,
                key="--deadline-seconds",
            ),
            source="explicit_cli",
        )

    env_value = env_get("ALYSIS_RUN_DEADLINE_SECONDS")
    if env_value is not None and str(env_value).strip() != "":
        # Presence of the variable is itself the explicit choice, so a word form
        # here means unlimited rather than "unset".
        return ResolvedRunDeadline(
            seconds=_coerce_optional_positive_float(
                env_value,
                key="ALYSIS_RUN_DEADLINE_SECONDS",
            ),
            source="environment",
        )

    cfg_value = _coerce_optional_positive_float(
        getattr(cfg, "run_deadline_seconds", None),
        key="run_deadline_seconds",
    )
    if cfg_value is not None:
        return ResolvedRunDeadline(seconds=cfg_value, source="config")
    if bool(getattr(cfg, "run_deadline_unlimited", False)):
        return ResolvedRunDeadline(seconds=None, source="config")

    if default_seconds is not None:
        return ResolvedRunDeadline(
            seconds=_coerce_optional_positive_float(
                default_seconds,
                key="run_deadline_seconds default",
            ),
            source="runtime_default",
        )
    return ResolvedRunDeadline(seconds=None, source="absent")


def resolve_run_deadline_seconds(
    cfg: AppConfig | None,
    *,
    cli_deadline_seconds: float | None = None,
    cli_no_deadline: bool = False,
    default_seconds: float | None = None,
) -> float | None:
    return resolve_run_deadline(
        cfg,
        cli_deadline_seconds=cli_deadline_seconds,
        cli_no_deadline=cli_no_deadline,
        default_seconds=default_seconds,
    ).seconds


def resolve_crash_diagnostic_log_path(
    cfg: AppConfig | None,
    *,
    cli_diagnostic_log_path: str | os.PathLike[str] | None = None,
) -> str | None:
    if cli_diagnostic_log_path is not None:
        raw = str(cli_diagnostic_log_path).strip()
        return raw or None

    env_value = env_get("ALYSIS_CRASH_DIAGNOSTIC_LOG_PATH")
    if env_value is not None:
        raw = str(env_value).strip()
        return raw or None

    raw = str(getattr(cfg, "crash_diagnostic_log_path", "") or "").strip()
    return raw or None


def resolve_llm_enable_thinking(cfg: AppConfig | None) -> bool | None:
    invocation = env_get("ALYSIS_RUN_REASONING_EFFORT")
    if invocation is not None:
        from .model_reasoning import invocation_reasoning_settings

        if cfg is None:
            raise ConfigError("Reasoning effort requires a selected provider/model.")
        return invocation_reasoning_settings(cfg, invocation)[1]
    active_subscription, subscription_effort = _active_subscription_reasoning_effort(cfg)
    if active_subscription:
        return None if subscription_effort is None else subscription_effort != "none"

    env_value = env_get("ALYSIS_LLM_ENABLE_THINKING")
    if env_value is not None:
        return _coerce_optional_bool(str(env_value), key="ALYSIS_LLM_ENABLE_THINKING")

    cfg_value = getattr(cfg, "llm_enable_thinking", None)
    if isinstance(cfg_value, bool):
        return cfg_value
    if cfg_value is not None:
        return _coerce_optional_bool(str(cfg_value), key="llm_enable_thinking")

    if cfg is not None and _is_dashscope_base_url(getattr(cfg, "base_url", None)):
        if _is_qwen_model(getattr(cfg, "model", None)):
            return False
    return None


def _legacy_reasoning_effort_hint(cfg: AppConfig | None) -> str | None:
    if cfg is None:
        return None
    extra_fields = getattr(cfg, "extra_fields", None)
    if not isinstance(extra_fields, dict):
        return None
    try:
        return _coerce_reasoning_effort(
            extra_fields.get("llm_thinking_label"),
            key="llm_thinking_label",
        )
    except ConfigError:
        return None


def resolve_llm_reasoning_effort(cfg: AppConfig | None) -> str | None:
    invocation = env_get("ALYSIS_RUN_REASONING_EFFORT")
    if invocation is not None:
        from .model_reasoning import invocation_reasoning_settings

        if cfg is None:
            raise ConfigError("Reasoning effort requires a selected provider/model.")
        return invocation_reasoning_settings(cfg, invocation)[0]
    active_subscription, subscription_effort = _active_subscription_reasoning_effort(cfg)
    if active_subscription:
        return subscription_effort

    env_value = env_get("ALYSIS_LLM_REASONING_EFFORT")
    if env_value is not None:
        return _coerce_reasoning_effort(env_value, key="ALYSIS_LLM_REASONING_EFFORT")

    cfg_value = getattr(cfg, "llm_reasoning_effort", None)
    if cfg_value is not None:
        return _coerce_reasoning_effort(cfg_value, key="llm_reasoning_effort")

    return _legacy_reasoning_effort_hint(cfg)


def _active_subscription_reasoning_effort(
    cfg: AppConfig | None,
) -> tuple[bool, str | None]:
    if cfg is None:
        return False, None
    try:
        from .profiles import get_active_profile

        profile = get_active_profile(cfg)
    except ConfigError:
        return False, None
    if not profile.auth_provider:
        return False, None
    return (
        True,
        _coerce_reasoning_effort(
            profile.reasoning_effort,
            key="subscription profile reasoning_effort",
        ),
    )


def resolve_web_search_mode(cfg: AppConfig | None) -> str:
    if cfg is None:
        return "auto"
    return _normalize_web_search_mode(
        getattr(cfg, "web_search_mode", "auto"),
        allow_legacy_on=True,
    )


def resolve_web_search_policy(cfg: AppConfig | None) -> str:
    env_policy = str(env_get("ALYSIS_WEB_SEARCH_POLICY") or "").strip()
    if env_policy:
        return _normalize_web_search_policy(env_policy)
    if cfg is None:
        return "auto"
    return _normalize_web_search_policy(getattr(cfg, "web_search_policy", "auto"))


_WEB_TOOLS_DISABLED_ENV_VALUES = frozenset({"0", "false", "off", "no", "disabled"})


def resolve_web_tools_enabled(cfg: AppConfig | None) -> bool:
    """Master switch for all web tools (web_fetch AND web_search).

    Precedence: ALYSIS_WEB_TOOLS env var (any of 0/false/off/no/disabled turns
    web tools off; any other non-empty value turns them on) over the
    ``web_tools_enabled`` config field. Benchmark/offline harnesses should set
    ``ALYSIS_WEB_TOOLS=off`` so the process itself guarantees no web tool is
    exposed or executed, independent of harness-side settings.
    """
    env_value = str(env_get("ALYSIS_WEB_TOOLS") or "").strip().lower()
    if env_value:
        return env_value not in _WEB_TOOLS_DISABLED_ENV_VALUES
    if cfg is None:
        return True
    return bool(getattr(cfg, "web_tools_enabled", True))


_WEB_FETCH_TRUSTED_DOMAINS_DISABLED_ENV_VALUES = frozenset(
    {"none", "off", "0", "false", "disabled"}
)


def _parse_trusted_domain_list(value: str) -> list[str]:
    """Parse a ``config set web_fetch_trusted_domains`` value.

    Comma-separated hosts. ``none``/``off``/``[]``/empty clears the allowlist;
    ``default`` restores the curated registry defaults. Every entry must carry
    a usable host (validated with the same normalizer the session tracker uses).
    """
    text = str(value or "").strip()
    if not text or text.lower() in {"none", "off", "[]"}:
        return []
    if text.lower() in {"default", "defaults"}:
        return list(DEFAULT_WEB_FETCH_TRUSTED_DOMAINS)
    entries: list[str] = []
    for raw_entry in text.split(","):
        entry = raw_entry.strip()
        if not entry:
            continue
        if normalize_trusted_domain_entry(entry) is None:
            raise ConfigError(
                f"web_fetch_trusted_domains entry {entry!r} has no usable host; "
                "use bare hosts like registry.npmjs.org (comma-separated)"
            )
        entries.append(entry)
    if not entries:
        return []
    return entries


def resolve_web_fetch_trusted_domains(cfg: AppConfig | None) -> tuple[str, ...]:
    """Trusted-domain allowlist for the web_fetch provenance gate.

    Precedence: ALYSIS_WEB_FETCH_TRUSTED_DOMAINS env var (comma-separated hosts;
    any of none/off/0/false/disabled empties the allowlist) over the
    ``web_fetch_trusted_domains`` config field, over the curated defaults.
    Entries are host-normalized downstream by the session tracker.
    """
    env_value = str(env_get("ALYSIS_WEB_FETCH_TRUSTED_DOMAINS") or "").strip()
    if env_value:
        if env_value.lower() in _WEB_FETCH_TRUSTED_DOMAINS_DISABLED_ENV_VALUES:
            return ()
        return tuple(entry.strip() for entry in env_value.split(",") if entry.strip())
    if cfg is None:
        return tuple(DEFAULT_WEB_FETCH_TRUSTED_DOMAINS)
    entries = getattr(cfg, "web_fetch_trusted_domains", None)
    if entries is None:
        return tuple(DEFAULT_WEB_FETCH_TRUSTED_DOMAINS)
    return tuple(str(entry) for entry in entries)


def resolve_web_search_enabled(cfg: AppConfig | None) -> bool:
    return resolve_web_search_mode(cfg) != "off"


def resolve_web_search_adapter(cfg: AppConfig | None) -> str:
    env_adapter = str(env_get("ALYSIS_WEB_SEARCH_ADAPTER") or "").strip()
    if env_adapter:
        return _normalize_web_search_adapter(env_adapter)

    cfg_adapter = _normalize_web_search_adapter(getattr(cfg, "web_search_adapter", "auto"))
    if cfg_adapter != "auto":
        return cfg_adapter

    if cfg is not None:
        try:
            from .profiles import get_active_profile

            profile = get_active_profile(cfg)
        except Exception:
            profile = None
        if profile is not None:
            profile_adapter = _normalize_web_search_adapter(profile.web_search_adapter)
            if profile_adapter != "auto":
                return profile_adapter

    return "auto"


def resolve_web_search_api_key(
    cfg: AppConfig | None,
    *,
    api_key_fallback: str | None = None,
) -> str | None:
    _ = cfg
    env_key = str(env_get("ALYSIS_WEB_SEARCH_API_KEY") or "").strip()
    if env_key:
        return env_key
    fallback = str(api_key_fallback or "").strip()
    return fallback or None


def resolve_web_search_explicit_base_url(cfg: AppConfig | None) -> str | None:
    env_base_url = str(env_get("ALYSIS_WEB_SEARCH_BASE_URL") or "").strip()
    if env_base_url:
        return env_base_url.rstrip("/")

    cfg_base_url = str(getattr(cfg, "web_search_base_url", "") or "").strip()
    if cfg_base_url:
        return cfg_base_url.rstrip("/")
    return None


def resolve_web_search_base_url(cfg: AppConfig | None) -> str | None:
    explicit_base_url = resolve_web_search_explicit_base_url(cfg)
    if explicit_base_url:
        return explicit_base_url

    fallback_base_url = str(getattr(cfg, "base_url", "") or "").strip()
    if cfg is not None and not fallback_base_url:
        return None

    if cfg is not None:
        try:
            from .profiles import DEFAULT_OPENAI_BASE_URL, get_active_profile

            profile = get_active_profile(cfg)
        except Exception:
            profile = None
        if profile is not None:
            profile_base_url = str(profile.base_url or "").strip().rstrip("/")
            cfg_base_url = str(getattr(cfg, "base_url", "") or "").strip().rstrip("/")
            default_base_url = DEFAULT_OPENAI_BASE_URL.rstrip("/")
            if cfg_base_url and cfg_base_url not in {profile_base_url, default_base_url}:
                return cfg_base_url
            if profile_base_url:
                return profile_base_url

    if fallback_base_url:
        return fallback_base_url.rstrip("/")
    return None


def is_first_party_openai_base_url(base_url: str | None) -> bool:
    normalized = str(base_url or "").strip()
    if not normalized:
        return False
    try:
        parsed = urlsplit(normalized)
    except ValueError:
        return False
    if parsed.scheme.lower() != "https":
        return False
    hostname = (parsed.hostname or "").rstrip(".").lower()
    return hostname == "api.openai.com"


def resolve_web_search_model(cfg: AppConfig | None) -> str | None:
    env_model = str(env_get("ALYSIS_WEB_SEARCH_MODEL") or "").strip()
    if env_model:
        return env_model
    cfg_model = str(getattr(cfg, "web_search_model", "") or "").strip()
    if cfg_model:
        return cfg_model
    if cfg is not None:
        try:
            from .profiles import get_active_profile

            profile = get_active_profile(cfg)
        except Exception:
            profile = None
        if profile is not None:
            profile_model = str(profile.web_search_model or "").strip()
            if profile_model:
                return profile_model
    fallback_model = str(getattr(cfg, "model", "") or "").strip()
    return fallback_model or None


def resolve_web_search_timeout_s(cfg: AppConfig | None) -> float:
    env_timeout_raw = env_get("ALYSIS_WEB_SEARCH_TIMEOUT_S")
    if env_timeout_raw is not None:
        env_timeout = _resolve_positive_timeout(env_timeout_raw)
        if env_timeout is None:
            raise ConfigError("ALYSIS_WEB_SEARCH_TIMEOUT_S must be > 0")
        return env_timeout

    cfg_timeout = _resolve_positive_timeout(getattr(cfg, "web_search_timeout_s", None))
    if cfg_timeout is None:
        raise ConfigError("web_search_timeout_s must be > 0")
    return cfg_timeout


def resolve_feedback_github_enabled(cfg: AppConfig | None) -> bool:
    env_value = env_get("ALYSIS_FEEDBACK_GITHUB_ENABLED")
    if env_value is not None:
        return _coerce_bool(str(env_value), key="ALYSIS_FEEDBACK_GITHUB_ENABLED")
    return bool(getattr(cfg, "feedback_github_enabled", True))


def resolve_feedback_github_repo(cfg: AppConfig | None) -> str:
    env_value = env_get("ALYSIS_FEEDBACK_GITHUB_REPO")
    if env_value is not None:
        return _coerce_github_repo(str(env_value), key="ALYSIS_FEEDBACK_GITHUB_REPO")
    raw = getattr(cfg, "feedback_github_repo", DEFAULT_FEEDBACK_GITHUB_REPO)
    return _coerce_github_repo(str(raw), key="feedback_github_repo")


def resolve_feedback_open_browser(cfg: AppConfig | None) -> bool:
    env_value = env_get("ALYSIS_FEEDBACK_OPEN_BROWSER")
    if env_value is not None:
        return _coerce_bool(str(env_value), key="ALYSIS_FEEDBACK_OPEN_BROWSER")
    return bool(getattr(cfg, "feedback_open_browser", True))


def resolve_model_metadata_policy(cfg: AppConfig | None) -> str:
    env_policy = str(env_get("ALYSIS_MODEL_METADATA_POLICY") or "").strip().lower()
    if env_policy:
        if env_policy not in _MODEL_METADATA_POLICIES:
            allowed = ", ".join(sorted(_MODEL_METADATA_POLICIES))
            raise ConfigError(f"ALYSIS_MODEL_METADATA_POLICY must be one of: {allowed}")
        return env_policy

    cfg_policy = str(getattr(cfg, "model_metadata_policy", "warn") or "").strip().lower()
    if not cfg_policy:
        return "warn"
    if cfg_policy not in _MODEL_METADATA_POLICIES:
        allowed = ", ".join(sorted(_MODEL_METADATA_POLICIES))
        raise ConfigError(f"model_metadata_policy must be one of: {allowed}")
    return cfg_policy


def _normalize_prompt_cache_mode(value: str | None, *, key: str) -> str:
    normalized = str(value or "").strip().lower() or "manual"
    if normalized not in _PROMPT_CACHE_MODES:
        allowed = ", ".join(sorted(_PROMPT_CACHE_MODES))
        raise ConfigError(f"{key} must be one of: {allowed}")
    return normalized


def resolve_prompt_cache_mode(cfg: AppConfig | None) -> str:
    env_mode = str(env_get("ALYSIS_PROMPT_CACHE_MODE") or "").strip()
    if env_mode:
        return _normalize_prompt_cache_mode(env_mode, key="ALYSIS_PROMPT_CACHE_MODE")
    return _normalize_prompt_cache_mode(
        str(getattr(cfg, "prompt_cache_mode", "manual") or "manual"),
        key="prompt_cache_mode",
    )


def resolve_prompt_cache_key(cfg: AppConfig | None) -> str | None:
    cache_config = getattr(cfg, "cache", None)
    if cache_config is not None and not bool(
        getattr(cache_config, "prompt_cache_key_enabled", True)
    ):
        return None
    env_key = str(env_get("ALYSIS_PROMPT_CACHE_KEY") or "").strip()
    if env_key:
        return env_key
    cfg_key = str(getattr(cfg, "prompt_cache_key", "") or "").strip()
    return cfg_key or None


def resolve_prompt_cache_retention(cfg: AppConfig | None) -> str | None:
    env_retention = str(env_get("ALYSIS_PROMPT_CACHE_RETENTION") or "").strip()
    if env_retention:
        return env_retention
    cfg_retention = str(getattr(cfg, "prompt_cache_retention", "") or "").strip()
    return cfg_retention or None


def _normalize_anthropic_prompt_cache_ttl(value: str | None, *, key: str) -> str:
    normalized = str(value or "").strip().lower() or "5m"
    if normalized not in _ANTHROPIC_PROMPT_CACHE_TTLS:
        allowed = ", ".join(sorted(_ANTHROPIC_PROMPT_CACHE_TTLS))
        raise ConfigError(f"{key} must be one of: {allowed}")
    return normalized


def resolve_anthropic_prompt_cache_enabled(cfg: AppConfig | None) -> bool:
    env_value = env_get("ALYSIS_ANTHROPIC_PROMPT_CACHE_ENABLED")
    if env_value is not None:
        return _coerce_bool(str(env_value), key="ALYSIS_ANTHROPIC_PROMPT_CACHE_ENABLED")
    return bool(getattr(cfg, "anthropic_prompt_cache_enabled", False))


def resolve_anthropic_prompt_cache_ttl(cfg: AppConfig | None) -> str:
    env_ttl = str(env_get("ALYSIS_ANTHROPIC_PROMPT_CACHE_TTL") or "").strip()
    if env_ttl:
        return _normalize_anthropic_prompt_cache_ttl(
            env_ttl,
            key="ALYSIS_ANTHROPIC_PROMPT_CACHE_TTL",
        )
    return _normalize_anthropic_prompt_cache_ttl(
        str(getattr(cfg, "anthropic_prompt_cache_ttl", "5m") or "5m"),
        key="anthropic_prompt_cache_ttl",
    )


def resolve_role_temperature(cfg: AppConfig, *, role: str) -> float:
    default = _ROLE_TEMPERATURE_DEFAULTS.get(role, 0.2)
    legacy_raw = getattr(cfg, "temperature", default)
    try:
        legacy_temperature = float(legacy_raw)
    except (TypeError, ValueError):
        legacy_temperature = default
    if legacy_temperature < 0:
        legacy_temperature = default

    field = _ROLE_TEMPERATURE_FIELDS.get(role)
    if field is None:
        return legacy_temperature

    role_raw = getattr(cfg, field, legacy_temperature)
    try:
        role_temperature = float(role_raw)
    except (TypeError, ValueError):
        return legacy_temperature
    if role_temperature < 0:
        return legacy_temperature
    return role_temperature


def _role_model_key_parts(key: str) -> tuple[str, str] | None:
    namespace, separator, role = str(key or "").partition(".")
    if not separator or namespace not in _ROLE_MODEL_CONFIG_PREFIXES:
        return None
    role_key = role.strip().lower()
    if not role_key:
        raise ConfigError(f"{namespace} config key must include a role name")
    if role_key not in _ROLE_MODEL_NAMES:
        raise ConfigError(
            f"{namespace}.{role_key} is not supported. "
            f"Supported roles: {', '.join(sorted(_ROLE_MODEL_NAMES))}"
        )
    return namespace, role_key


def _set_role_model_config_value(
    cfg: AppConfig,
    *,
    namespace: str,
    role: str,
    value: str,
) -> AppConfig:
    extra_fields = dict(cfg.extra_fields or {})
    raw_models = extra_fields.get(namespace)
    models = dict(raw_models) if isinstance(raw_models, dict) else {}
    normalized = str(value or "").strip()
    if normalized:
        models[role] = normalized
    else:
        models.pop(role, None)
    if models:
        extra_fields[namespace] = dict(sorted(models.items()))
    else:
        extra_fields.pop(namespace, None)
    cfg.extra_fields = extra_fields
    return cfg


def _persona_model_key_parts(key: str) -> str | None:
    namespace, separator, persona = str(key or "").partition(".")
    if not separator or namespace != "persona_models":
        return None
    persona_key = persona.strip().lower()
    if not persona_key:
        raise ConfigError("persona_models config key must include a persona name")
    if persona_key not in _PERSONA_NAMES:
        raise ConfigError(
            f"persona_models.{persona_key} is not supported. "
            f"Supported personas: {', '.join(sorted(_PERSONA_NAMES))}"
        )
    return persona_key


def _set_persona_model_config_value(
    cfg: AppConfig,
    *,
    persona: str,
    value: str,
) -> AppConfig:
    normalized = str(value or "").strip().lower()
    if normalized and normalized not in _ROLE_MODEL_NAMES:
        raise ConfigError(
            f"persona_models.{persona} must name a model role. "
            f"Supported roles: {', '.join(sorted(_ROLE_MODEL_NAMES))}"
        )
    extra_fields = dict(cfg.extra_fields or {})
    raw_map = extra_fields.get("persona_models")
    personas = dict(raw_map) if isinstance(raw_map, dict) else {}
    if normalized:
        personas[persona] = normalized
    else:
        personas.pop(persona, None)
    if personas:
        extra_fields["persona_models"] = dict(sorted(personas.items()))
    else:
        extra_fields.pop("persona_models", None)
    cfg.extra_fields = extra_fields
    return cfg


def _agent_runtime_key_parts(key: str) -> tuple[str, str] | None:
    parts = str(key or "").split(".")
    if not parts or parts[0] != "agent_runtimes":
        return None
    if len(parts) != 3 or not parts[1].strip():
        raise ConfigError("Agent runtime keys must use agent_runtimes.<runtime-id>.<field> syntax.")
    field = parts[2].strip()
    if field not in _AGENT_RUNTIME_CONFIG_FIELDS:
        allowed = ", ".join(sorted(_AGENT_RUNTIME_CONFIG_FIELDS))
        raise ConfigError(f"Unsupported agent runtime field {field!r}. Supported fields: {allowed}")
    return parts[1].strip(), field


def _set_agent_runtime_config_value(
    cfg: AppConfig,
    *,
    runtime_id: str,
    field: str,
    value: str,
) -> AppConfig:
    current = cfg.agent_runtimes.get(runtime_id)
    values = (
        current.model_dump() if current is not None else _default_agent_runtime_values(runtime_id)
    )
    normalized = str(value or "").strip()
    if field in {"adapter", "executable"}:
        if not normalized:
            raise ConfigError(f"agent_runtimes.{runtime_id}.{field} cannot be empty")
        values[field] = normalized
    elif field in {"model", "reasoning_effort"}:
        values[field] = normalized or None
    elif field == "timeout_seconds":
        values[field] = _coerce_positive_float(value, key=f"agent_runtimes.{runtime_id}.{field}")
    elif field == "provider_managed_auth":
        values[field] = _coerce_bool(value, key=f"agent_runtimes.{runtime_id}.{field}")
    cfg.agent_runtimes[runtime_id] = AgentRuntimeSettings(**values)
    return cfg


def _default_agent_runtime_values(runtime_id: str) -> dict[str, object]:
    # Local import avoids coupling the core config model to optional runtime
    # discovery during module import, while still using registered defaults for
    # direct `config set agent_runtimes.<id>...` operations.
    from .agent_runtimes.builtins import runtime_setup_option

    option = runtime_setup_option(runtime_id)
    return {
        "adapter": option.adapter if option is not None else runtime_id,
        "executable": option.default_executable if option is not None else runtime_id,
        "provider_managed_auth": True,
        "model": None,
        "reasoning_effort": None,
        "timeout_seconds": 3600.0,
    }


def _ensure_selected_runtime_settings(cfg: AppConfig, runtime_id: str) -> None:
    if runtime_id not in cfg.agent_runtimes:
        cfg.agent_runtimes[runtime_id] = AgentRuntimeSettings(
            **_default_agent_runtime_values(runtime_id)
        )


def _set_prompt_guidance_config_value(cfg: AppConfig, *, key: str, value: str) -> AppConfig:
    values = cfg.prompt_guidance.model_dump()
    if key == "prompt_guidance.default":
        values["default"] = value.strip().lower()
    elif key == "prompt_guidance.model_profiles":
        try:
            values["model_profiles"] = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise ConfigError("prompt_guidance.model_profiles must be a JSON object") from exc
    elif key.startswith("prompt_guidance.model_profiles."):
        model = key.removeprefix("prompt_guidance.model_profiles.")
        if not model or model != model.strip():
            raise ConfigError(
                "prompt_guidance.model_profiles requires a nonempty, trimmed model ID"
            )
        profile = value.strip().lower()
        if profile:
            values["model_profiles"][model] = profile
        else:
            values["model_profiles"].pop(model, None)
    else:
        raise ConfigError(
            "Supported prompt guidance keys: prompt_guidance.default, "
            "prompt_guidance.model_profiles, prompt_guidance.model_profiles.<model-id>"
        )
    try:
        guidance = PromptGuidanceConfig.model_validate(values)
    except ValidationError as exc:
        raise ConfigError(
            f"Prompt guidance profiles must be one of {', '.join(GUIDANCE_PROFILES)}; "
            "model_profiles must map exact model IDs to profiles."
        ) from exc
    cfg.prompt_guidance = guidance
    return cfg


def set_config_value(
    cfg: AppConfig,
    key: str,
    value: str,
    *,
    allow_subscription_selection: bool = False,
) -> AppConfig:
    if not allow_subscription_selection or str(key or "").strip().lower() == "base_url":
        ensure_subscription_menu_managed_key(cfg, key)
    if key.startswith("prompt_guidance."):
        return _set_prompt_guidance_config_value(cfg, key=key, value=value)
    role_model_parts = _role_model_key_parts(key)
    if role_model_parts is not None:
        namespace, role = role_model_parts
        return _set_role_model_config_value(
            cfg,
            namespace=namespace,
            role=role,
            value=value,
        )

    persona_model_persona = _persona_model_key_parts(key)
    if persona_model_persona is not None:
        return _set_persona_model_config_value(
            cfg,
            persona=persona_model_persona,
            value=value,
        )

    agent_runtime_parts = _agent_runtime_key_parts(key)
    if agent_runtime_parts is not None:
        runtime_id, field = agent_runtime_parts
        return _set_agent_runtime_config_value(
            cfg,
            runtime_id=runtime_id,
            field=field,
            value=value,
        )

    if key not in _SETTABLE_KEYS:
        raise ConfigError(
            f"Unknown/unsupported key: {key}. Supported keys: "
            f"{', '.join(sorted(_SETTABLE_KEYS))}, role_models.<role>, "
            "forge_role_models.<role>, persona_models.<persona>, "
            "agent_runtimes.<runtime-id>.<field>, prompt_guidance.model_profiles.<model-id>"
        )

    if key == "execution.backend":
        backend = str(value or "").strip().lower()
        if backend not in {"native", "delegated"}:
            raise ConfigError("execution.backend must be one of: native, delegated")
        if backend == "native":
            cfg.execution.backend = "native"
            cfg.execution.runtime = None
            return cfg
        runtime_id = str(cfg.execution.runtime or "").strip()
        if not runtime_id:
            configured = tuple(sorted(cfg.agent_runtimes))
            if len(configured) == 1:
                runtime_id = configured[0]
            elif len(configured) > 1:
                raise ConfigError(
                    "Multiple agent runtimes are configured. Set execution.runtime to choose one."
                )
            else:
                from .agent_runtimes.builtins import runtime_setup_options

                options = runtime_setup_options()
                if len(options) != 1:
                    raise ConfigError(
                        "Choose an agent runtime with `alysis config set execution.runtime "
                        "<runtime-id>`."
                    )
                runtime_id = options[0].id
        _ensure_selected_runtime_settings(cfg, runtime_id)
        cfg.execution.runtime = runtime_id
        cfg.execution.backend = "delegated"
        return cfg

    if key == "execution.runtime":
        runtime_id = str(value or "").strip()
        if not runtime_id:
            cfg.execution.backend = "native"
            cfg.execution.runtime = None
            return cfg
        _ensure_selected_runtime_settings(cfg, runtime_id)
        cfg.execution.runtime = runtime_id
        cfg.execution.backend = "delegated"
        return cfg

    if key == "base_url":
        from .profiles import update_active_profile_defaults, validate_base_url

        normalized = validate_base_url(value, key="base_url", allow_empty=True)
        if normalized:
            from .profile_presets import find_preset_for_base_url

            preset = find_preset_for_base_url(normalized)
            if preset is not None:
                current_model = str(getattr(cfg, "model", "") or "").strip()
                default_model = _canonicalize_model_for_preset(current_model, preset)
                if default_model is None and preset.suggested_models:
                    default_model = str(preset.suggested_models[0] or "").strip()
                _align_active_profile_to_preset(
                    cfg,
                    preset,
                    base_url=normalized,
                    default_model=default_model,
                )
                return cfg
        cfg.base_url = normalized
        update_active_profile_defaults(cfg, base_url=normalized)
        return cfg

    if key == "model":
        from .profiles import update_active_profile_defaults

        normalized = _canonicalize_model_for_config(cfg, str(value or "").strip())
        cfg.model = normalized
        update_active_profile_defaults(
            cfg,
            default_model=normalized,
            allow_subscription_selection=allow_subscription_selection,
        )
        return cfg

    if key == "llm_timeout_s":
        cfg.llm_timeout_s = _coerce_positive_float(value, key=key)
        return cfg

    if key == "llm_stream_no_progress_timeout_s":
        cfg.llm_stream_no_progress_timeout_s = _coerce_positive_float(value, key=key)
        return cfg

    if key == "run_deadline_seconds":
        cfg.run_deadline_seconds = _coerce_optional_positive_float(value, key=key)
        # A word form here is a deliberate "no budget", which has to be
        # distinguishable from an unset value now that unset means "default".
        cfg.run_deadline_unlimited = cfg.run_deadline_seconds is None
        return cfg

    if key in {"sampling_temperature", "sampling_top_p", "sampling_seed"}:
        setattr(cfg, key, _coerce_optional_sampling_value(value, key=key))
        return cfg

    if key == "llm_enable_thinking":
        cfg.llm_enable_thinking = _coerce_optional_bool(value, key=key)
        return cfg

    if key == "llm_reasoning_effort":
        resolved_effort = _coerce_reasoning_effort(value, key=key)
        cfg.llm_reasoning_effort = resolved_effort
        from .profiles import update_active_profile_defaults

        profile_effort = resolved_effort or "auto"
        update_active_profile_defaults(
            cfg,
            reasoning_effort=profile_effort,
            allow_subscription_selection=allow_subscription_selection,
        )
        return cfg

    if key == "provider_concurrency_caps":
        cfg.provider_concurrency_caps = _parse_provider_concurrency_caps(value, key=key)
        return cfg

    if key == "provider_retry_max_retries":
        cfg.provider_retry_max_retries = _coerce_non_negative_int(value, key=key)
        return cfg

    if key in {
        "provider_retry_base_delay_seconds",
        "provider_retry_max_delay_seconds",
    }:
        setattr(cfg, key, _coerce_positive_float(value, key=key))
        return cfg

    if key == "model_metadata_policy":
        normalized = value.strip().lower()
        if normalized not in _MODEL_METADATA_POLICIES:
            raise ConfigError("model_metadata_policy must be one of: strict, warn")
        cfg.model_metadata_policy = normalized
        return cfg

    if key == "default_mode":
        if value not in {"review", "auto", "readonly", "fullaccess"}:
            raise ConfigError("default_mode must be one of: review, auto, readonly, fullaccess")
        cfg.default_mode = value
        return cfg

    if key == "default_persona":
        v = value.strip().lower()
        if v not in _PERSONA_NAMES:
            raise ConfigError("default_persona must be one of: code, architect, ask, debug")
        cfg.default_persona = v
        return cfg

    if key == "persona_modes_enabled":
        v = value.strip().lower()
        if v in {"1", "true", "yes", "on"}:
            cfg.persona_modes_enabled = True
            return cfg
        if v in {"0", "false", "no", "off"}:
            cfg.persona_modes_enabled = False
            return cfg
        raise ConfigError("persona_modes_enabled must be true/false")

    if key in {"max_steps", "task_max_steps", "subagent_max_steps"}:
        setattr(cfg, key, _coerce_positive_int(value, key=key))
        return cfg

    if key == "subagent_timeout_s":
        cfg.subagent_timeout_s = _coerce_optional_positive_float(value, key=key)
        return cfg

    if key == "subagent_orchestration.max_background_children":
        cfg.subagent_orchestration.max_background_children = _coerce_positive_int(
            value,
            key=key,
        )
        return cfg

    if key == "subagent_orchestration.turn_end_policy":
        normalized = value.strip().lower()
        if normalized not in {"wait", "cancel"}:
            raise ConfigError("subagent_orchestration.turn_end_policy must be one of: wait, cancel")
        cfg.subagent_orchestration.turn_end_policy = normalized
        return cfg

    if key == "subagent_orchestration.workspace_isolation_enabled":
        cfg.subagent_orchestration.workspace_isolation_enabled = _coerce_bool(value, key=key)
        return cfg

    if key == "subagent_orchestration.parallel_nonwriting_shared":
        cfg.subagent_orchestration.parallel_nonwriting_shared = _coerce_bool(value, key=key)
        return cfg

    if key == "subagent_orchestration.helpers_enabled":
        cfg.subagent_orchestration.helpers_enabled = _coerce_bool(value, key=key)
        return cfg

    if key == "subagent_orchestration.helper_max_total_per_child":
        cfg.subagent_orchestration.helper_max_total_per_child = _coerce_non_negative_int(
            value,
            key=key,
        )
        return cfg

    if key == "subagent_orchestration.helper_timeout_s":
        cfg.subagent_orchestration.helper_timeout_s = _coerce_optional_positive_float(
            value,
            key=key,
        )
        return cfg

    if key == "subagent_orchestration.helper_max_steps":
        cfg.subagent_orchestration.helper_max_steps = _coerce_optional_positive_int(
            value,
            key=key,
        )
        return cfg

    if key == "subagent_orchestration.repetition_signal_threshold":
        threshold = _coerce_positive_int(value, key=key)
        if threshold < 2:
            raise ConfigError(
                "subagent_orchestration.repetition_signal_threshold must be at least 2"
            )
        if threshold >= cfg.subagent_orchestration.repetition_backstop_threshold:
            raise ConfigError(
                "subagent_orchestration.repetition_signal_threshold must be lower than "
                "subagent_orchestration.repetition_backstop_threshold"
            )
        cfg.subagent_orchestration.repetition_signal_threshold = threshold
        return cfg

    if key == "subagent_orchestration.repetition_nudge_occurrence_threshold":
        threshold = _coerce_positive_int(value, key=key)
        if threshold < 2:
            raise ConfigError(
                "subagent_orchestration.repetition_nudge_occurrence_threshold must be at least 2"
            )
        if threshold >= cfg.subagent_orchestration.repetition_occurrence_threshold:
            raise ConfigError(
                "subagent_orchestration.repetition_nudge_occurrence_threshold must be "
                "lower than subagent_orchestration.repetition_occurrence_threshold"
            )
        cfg.subagent_orchestration.repetition_nudge_occurrence_threshold = threshold
        return cfg

    if key == "subagent_orchestration.repetition_occurrence_threshold":
        threshold = _coerce_positive_int(value, key=key)
        if threshold <= cfg.subagent_orchestration.repetition_nudge_occurrence_threshold:
            raise ConfigError(
                "subagent_orchestration.repetition_occurrence_threshold must exceed "
                "subagent_orchestration.repetition_nudge_occurrence_threshold"
            )
        if threshold > cfg.subagent_orchestration.repetition_backstop_threshold:
            raise ConfigError(
                "subagent_orchestration.repetition_occurrence_threshold must not exceed "
                "subagent_orchestration.repetition_backstop_threshold"
            )
        cfg.subagent_orchestration.repetition_occurrence_threshold = threshold
        return cfg

    if key == "subagent_orchestration.repetition_backstop_threshold":
        threshold = _coerce_positive_int(value, key=key)
        if threshold <= cfg.subagent_orchestration.repetition_signal_threshold:
            raise ConfigError(
                "subagent_orchestration.repetition_backstop_threshold must exceed "
                "subagent_orchestration.repetition_signal_threshold"
            )
        if threshold < cfg.subagent_orchestration.repetition_occurrence_threshold:
            raise ConfigError(
                "subagent_orchestration.repetition_backstop_threshold must not be lower "
                "than subagent_orchestration.repetition_occurrence_threshold"
            )
        cfg.subagent_orchestration.repetition_backstop_threshold = threshold
        return cfg

    if key in {
        "subagent_orchestration.model_response_activity_after_s",
        "subagent_orchestration.inactivity_signal_after_s",
    }:
        setattr(
            cfg.subagent_orchestration,
            key.rsplit(".", 1)[1],
            _coerce_positive_float(value, key=key),
        )
        return cfg

    if key == "subagent_orchestration.inflight_deadline_grace_s":
        parsed = _coerce_non_negative_float(value, key=key)
        if not math.isfinite(parsed):
            raise ConfigError(f"{key} must be a finite number >= 0")
        cfg.subagent_orchestration.inflight_deadline_grace_s = parsed
        return cfg

    if key == "cache.prompt_cache_key_enabled":
        cfg.cache.prompt_cache_key_enabled = _coerce_bool(value, key=key)
        return cfg

    if key == "cache.keepalive_enabled":
        cfg.cache.keepalive_enabled = _coerce_bool(value, key=key)
        return cfg

    if key == "cache.keepalive_idle_threshold_s":
        cfg.cache.keepalive_idle_threshold_s = _coerce_positive_float(value, key=key)
        return cfg

    if key == "read_ledger_enabled":
        cfg.read_ledger_enabled = _coerce_bool(value, key=key)
        return cfg

    if key == "temperature":
        _apply_legacy_temperature_override(cfg, _coerce_non_negative_float(value, key=key))
        return cfg

    if key in _ROLE_TEMPERATURE_FIELDS.values():
        setattr(cfg, key, _coerce_non_negative_float(value, key=key))
        return cfg

    if key == "stream":
        v = value.strip().lower()
        if v in {"1", "true", "yes", "on"}:
            cfg.stream = True
            return cfg
        if v in {"0", "false", "no", "off"}:
            cfg.stream = False
            return cfg
        raise ConfigError("stream must be true/false")

    if key == "llm_stream_assembly":
        normalized = value.strip().lower()
        if normalized not in STREAM_ASSEMBLY_SETTINGS:
            raise ConfigError(
                "llm_stream_assembly must be one of: " + ", ".join(STREAM_ASSEMBLY_SETTINGS)
            )
        cfg.llm_stream_assembly = normalized
        return cfg

    if key == "routing_mode":
        v = value.strip().lower()
        if v not in {"auto", "code_only"}:
            raise ConfigError("routing_mode must be one of: auto, code_only")
        cfg.routing_mode = v
        return cfg

    if key == "route_arbitration_enabled":
        v = value.strip().lower()
        if v in {"1", "true", "yes", "on"}:
            cfg.route_arbitration_enabled = True
            return cfg
        if v in {"0", "false", "no", "off"}:
            cfg.route_arbitration_enabled = False
            return cfg
        raise ConfigError("route_arbitration_enabled must be true/false")

    if key == "semantic_turn_contract_enabled":
        v = value.strip().lower()
        if v in {"1", "true", "yes", "on"}:
            cfg.semantic_turn_contract_enabled = True
            return cfg
        if v in {"0", "false", "no", "off"}:
            cfg.semantic_turn_contract_enabled = False
            return cfg
        raise ConfigError("semantic_turn_contract_enabled must be true/false")

    if key == "unified_turn_path_enabled":
        v = value.strip().lower()
        if v in {"1", "true", "yes", "on"}:
            cfg.unified_turn_path_enabled = True
            return cfg
        if v in {"0", "false", "no", "off"}:
            cfg.unified_turn_path_enabled = False
            return cfg
        raise ConfigError("unified_turn_path_enabled must be true/false")

    if key == "reply_language":
        cfg.reply_language = value.strip()
        return cfg

    if key == "evidence_v2_enabled":
        v = value.strip().lower()
        if v in {"1", "true", "yes", "on"}:
            cfg.evidence_v2_enabled = True
            return cfg
        if v in {"0", "false", "no", "off"}:
            cfg.evidence_v2_enabled = False
            return cfg
        raise ConfigError("evidence_v2_enabled must be true/false")

    if key == "regression_baseline_enabled":
        v = value.strip().lower()
        if v in {"1", "true", "yes", "on"}:
            cfg.regression_baseline_enabled = True
            return cfg
        if v in {"0", "false", "no", "off"}:
            cfg.regression_baseline_enabled = False
            return cfg
        raise ConfigError("regression_baseline_enabled must be true/false")

    if key == "turn_contract_v2_enabled":
        v = value.strip().lower()
        if v in {"1", "true", "yes", "on"}:
            cfg.turn_contract_v2_enabled = True
            return cfg
        if v in {"0", "false", "no", "off"}:
            cfg.turn_contract_v2_enabled = False
            return cfg
        raise ConfigError("turn_contract_v2_enabled must be true/false")

    if key == "reproduction_first_enabled":
        v = value.strip().lower()
        if v in {"1", "true", "yes", "on"}:
            cfg.reproduction_first_enabled = True
            return cfg
        if v in {"0", "false", "no", "off"}:
            cfg.reproduction_first_enabled = False
            return cfg
        raise ConfigError("reproduction_first_enabled must be true/false")

    if key == "blast_radius_gate_enabled":
        v = value.strip().lower()
        if v in {"1", "true", "yes", "on"}:
            cfg.blast_radius_gate_enabled = True
            return cfg
        if v in {"0", "false", "no", "off"}:
            cfg.blast_radius_gate_enabled = False
            return cfg
        raise ConfigError("blast_radius_gate_enabled must be true/false")

    if key in {"blast_radius_max_scope_files", "blast_radius_over_broad_threshold"}:
        try:
            parsed_count = int(value.strip())
        except ValueError:
            raise ConfigError(f"{key} must be a positive integer") from None
        if parsed_count <= 0:
            raise ConfigError(f"{key} must be a positive integer")
        setattr(cfg, key, parsed_count)
        return cfg

    if key == "blast_radius_scope_seconds_cap":
        try:
            parsed_seconds = float(value.strip())
        except ValueError:
            raise ConfigError("blast_radius_scope_seconds_cap must be a positive number") from None
        # ``inf``/``nan`` parse fine but would disable the cap silently; the field
        # declares allow_inf_nan=False and direct assignment does not re-validate.
        if not math.isfinite(parsed_seconds) or parsed_seconds <= 0:
            raise ConfigError("blast_radius_scope_seconds_cap must be a positive number")
        cfg.blast_radius_scope_seconds_cap = parsed_seconds
        return cfg

    if key == "process_reaping_enabled":
        v = value.strip().lower()
        if v in {"1", "true", "yes", "on"}:
            cfg.process_reaping_enabled = True
            return cfg
        if v in {"0", "false", "no", "off"}:
            cfg.process_reaping_enabled = False
            return cfg
        raise ConfigError("process_reaping_enabled must be true/false")

    if key == "workspace_provisioning_enabled":
        v = value.strip().lower()
        if v in {"1", "true", "yes", "on"}:
            cfg.workspace_provisioning_enabled = True
            return cfg
        if v in {"0", "false", "no", "off"}:
            cfg.workspace_provisioning_enabled = False
            return cfg
        raise ConfigError("workspace_provisioning_enabled must be true/false")

    if key == "step_budget_policy":
        normalized = value.strip().lower()
        if normalized not in {"autonomous", "limited", "adaptive", "fixed"}:
            raise ConfigError(
                "step_budget_policy must be one of: autonomous, limited "
                "(legacy aliases: adaptive, fixed)"
            )
        cfg.step_budget_policy = normalize_step_budget_policy(normalized)
        return cfg

    if key == "subagents_enabled":
        v = value.strip().lower()
        if v in {"1", "true", "yes", "on"}:
            cfg.subagents_enabled = True
            return cfg
        if v in {"0", "false", "no", "off"}:
            cfg.subagents_enabled = False
            return cfg
        raise ConfigError("subagents_enabled must be true/false")

    if key == "skills_enabled":
        v = value.strip().lower()
        if v in {"1", "true", "yes", "on"}:
            cfg.skills_enabled = True
            return cfg
        if v in {"0", "false", "no", "off"}:
            cfg.skills_enabled = False
            return cfg
        raise ConfigError("skills_enabled must be true/false")

    if key == "bundled_skills_enabled":
        v = value.strip().lower()
        if v in {"1", "true", "yes", "on"}:
            cfg.bundled_skills_enabled = True
            return cfg
        if v in {"0", "false", "no", "off"}:
            cfg.bundled_skills_enabled = False
            return cfg
        raise ConfigError("bundled_skills_enabled must be true/false")

    if key == "skills_auto_invoke":
        v = value.strip().lower()
        if v in {"1", "true", "yes", "on"}:
            cfg.skills_auto_invoke = True
            return cfg
        if v in {"0", "false", "no", "off"}:
            cfg.skills_auto_invoke = False
            return cfg
        raise ConfigError("skills_auto_invoke must be true/false")

    if key == "experimental_gemini_interactions_enabled":
        cfg.experimental_gemini_interactions_enabled = _coerce_bool(value, key=key)
        return cfg

    if key == "custom_tools_enabled":
        v = value.strip().lower()
        if v in {"1", "true", "yes", "on"}:
            cfg.custom_tools_enabled = True
            return cfg
        if v in {"0", "false", "no", "off"}:
            cfg.custom_tools_enabled = False
            return cfg
        raise ConfigError("custom_tools_enabled must be true/false")

    if key == "web_tools_enabled":
        v = value.strip().lower()
        if v in {"1", "true", "yes", "on"}:
            cfg.web_tools_enabled = True
            return cfg
        if v in {"0", "false", "no", "off"}:
            cfg.web_tools_enabled = False
            return cfg
        raise ConfigError("web_tools_enabled must be true/false")

    if key == "web_fetch_trusted_domains":
        cfg.web_fetch_trusted_domains = _parse_trusted_domain_list(value)
        return cfg

    if key == "web_search_mode":
        cfg.web_search_mode = _normalize_web_search_mode(value)
        return cfg

    if key == "web_search_policy":
        cfg.web_search_policy = _normalize_web_search_policy(value)
        return cfg

    if key == "web_search_enabled":
        cfg.web_search_mode = _coerce_legacy_web_search_mode(value)
        return cfg

    if key == "web_search_adapter":
        cfg.web_search_adapter = _normalize_web_search_adapter(value)
        return cfg

    if key == "web_search_base_url":
        cfg.web_search_base_url = value.strip() or None
        return cfg

    if key == "web_search_model":
        cfg.web_search_model = value.strip() or None
        return cfg

    if key == "web_search_timeout_s":
        cfg.web_search_timeout_s = _coerce_positive_float(value, key=key)
        return cfg

    if key == "image_generation.enabled":
        cfg.image_generation.enabled = _coerce_bool(value, key=key)
        return cfg

    if key == "image_generation.model":
        normalized = str(value or "").strip()
        if not normalized:
            raise ConfigError("image_generation.model cannot be empty")
        payload = cfg.image_generation.model_dump()
        payload["model"] = normalized
        cfg.image_generation = ImageGenerationConfig.model_validate(payload)
        return cfg

    if key == "image_generation.base_url":
        from .profiles import validate_base_url

        normalized = str(value or "").strip()
        payload = cfg.image_generation.model_dump()
        payload["base_url"] = (
            validate_base_url(normalized, key=key, allow_empty=True) if normalized else None
        )
        try:
            cfg.image_generation = ImageGenerationConfig.model_validate(payload)
        except ValidationError as exc:
            raise ConfigError(str(exc.errors()[0]["msg"])) from exc
        return cfg

    if key == "image_generation.api_key_env":
        normalized = str(value or "").strip()
        if normalized and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", normalized) is None:
            raise ConfigError(
                "image_generation.api_key_env must be a valid environment variable name"
            )
        payload = cfg.image_generation.model_dump()
        payload["api_key_env"] = normalized or None
        cfg.image_generation = ImageGenerationConfig.model_validate(payload)
        return cfg

    if key == "image_generation.timeout_s":
        payload = cfg.image_generation.model_dump()
        payload["timeout_s"] = _coerce_positive_float(value, key=key)
        try:
            cfg.image_generation = ImageGenerationConfig.model_validate(payload)
        except ValidationError as exc:
            raise ConfigError(str(exc.errors()[0]["msg"])) from exc
        return cfg

    if key in {
        "image_generation.max_images_per_call",
        "image_generation.max_image_bytes",
        "image_generation.max_pixels",
    }:
        field_name = key.rsplit(".", 1)[1]
        payload = cfg.image_generation.model_dump()
        payload[field_name] = _coerce_positive_int(value, key=key)
        try:
            cfg.image_generation = ImageGenerationConfig.model_validate(payload)
        except ValidationError as exc:
            raise ConfigError(str(exc.errors()[0]["msg"])) from exc
        return cfg

    if key == "update_check_enabled":
        cfg.update_check_enabled = _coerce_bool(value, key=key)
        return cfg

    if key == "update_check_interval_hours":
        cfg.update_check_interval_hours = _coerce_positive_int(value, key=key)
        return cfg

    if key == "update_check_timeout_s":
        cfg.update_check_timeout_s = _coerce_positive_float(value, key=key)
        return cfg

    if key == "update_prompt_enabled":
        cfg.update_prompt_enabled = _coerce_bool(value, key=key)
        return cfg

    if key == "feedback_github_enabled":
        cfg.feedback_github_enabled = _coerce_bool(value, key=key)
        return cfg

    if key == "feedback_github_repo":
        cfg.feedback_github_repo = _coerce_github_repo(value, key=key)
        return cfg

    if key == "feedback_open_browser":
        cfg.feedback_open_browser = _coerce_bool(value, key=key)
        return cfg

    if key == "session_log_dir":
        cfg.session_log_dir = value if value.strip() else None
        return cfg

    if key == "crash_diagnostic_log_path":
        cfg.crash_diagnostic_log_path = value.strip() or None
        return cfg

    if key == "prompt_cache_mode":
        cfg.prompt_cache_mode = _normalize_prompt_cache_mode(value, key=key)
        cfg.hosted_prompt_cache_defaults_applied = True
        return cfg

    if key == "prompt_cache_key":
        cfg.prompt_cache_key = value.strip() or None
        cfg.hosted_prompt_cache_defaults_applied = True
        return cfg

    if key == "prompt_cache_retention":
        cfg.prompt_cache_retention = value.strip() or None
        cfg.hosted_prompt_cache_defaults_applied = True
        return cfg

    if key == "anthropic_prompt_cache_enabled":
        cfg.anthropic_prompt_cache_enabled = _coerce_bool(value, key=key)
        cfg.hosted_prompt_cache_defaults_applied = True
        return cfg

    if key == "anthropic_prompt_cache_ttl":
        cfg.anthropic_prompt_cache_ttl = _normalize_anthropic_prompt_cache_ttl(value, key=key)
        cfg.hosted_prompt_cache_defaults_applied = True
        return cfg

    if key == "verify_commands":
        cfg.verify_commands = _parse_command_list(
            value,
            key="verify_commands",
            allow_empty=False,
        )
        return cfg

    if key == "integration_verify_mode":
        normalized = value.strip().lower()
        if normalized not in _VERIFY_LIKE_MODES:
            raise ConfigError("integration_verify_mode must be one of: off, warn, strict")
        cfg.integration_verify_mode = normalized
        return cfg

    if key == "integration_verify_commands":
        cfg.integration_verify_commands = _parse_command_list(
            value,
            key="integration_verify_commands",
            allow_empty=True,
        )
        return cfg

    if key == "replanning_mode":
        normalized = value.strip().lower()
        if normalized not in {"off", "suggest", "apply"}:
            raise ConfigError("replanning_mode must be one of: off, suggest, apply")
        cfg.replanning_mode = normalized
        return cfg

    if key == "assets.enabled":
        cfg.assets.enabled = _coerce_bool(value, key=key)
        return cfg

    if key == "assets.comprehension.role":
        role = value.strip().lower()
        if not role:
            raise ConfigError("assets.comprehension.role must be non-empty")
        cfg.assets.comprehension.role = role
        return cfg

    if key == "assets.comprehension.vision_fallback_profile":
        cfg.assets.comprehension.vision_fallback_profile = value.strip().lower() or None
        return cfg

    if key == "assets.comprehension.vision_with_ocr_when_available":
        cfg.assets.comprehension.vision_with_ocr_when_available = _coerce_bool(value, key=key)
        return cfg

    if key == "assets.comprehension.ocr_enabled":
        normalized = value.strip().lower()
        if normalized not in {"auto", "always", "never"}:
            raise ConfigError(
                "assets.comprehension.ocr_enabled must be one of: auto, always, never"
            )
        cfg.assets.comprehension.ocr_enabled = normalized  # type: ignore[assignment]
        return cfg

    if key == "assets.comprehension.ocr_provider":
        provider = value.strip().lower()
        if not provider:
            raise ConfigError("assets.comprehension.ocr_provider must be non-empty")
        cfg.assets.comprehension.ocr_provider = provider
        return cfg

    if key == "assets.comprehension.ocr_timeout_seconds":
        cfg.assets.comprehension.ocr_timeout_seconds = _coerce_positive_int(value, key=key)
        return cfg

    if key == "assets.comprehension.image_max_edge_pixels":
        cfg.assets.comprehension.image_max_edge_pixels = _coerce_positive_int(value, key=key)
        return cfg

    if key == "assets.comprehension.questioning_mode":
        normalized = value.strip().lower()
        if normalized not in {"assertive", "balanced", "assumption_friendly"}:
            raise ConfigError(
                "assets.comprehension.questioning_mode must be one of: "
                "assertive, balanced, assumption_friendly"
            )
        cfg.assets.comprehension.questioning_mode = normalized  # type: ignore[assignment]
        return cfg

    if key == "assets.comprehension.schema_version":
        cfg.assets.comprehension.schema_version = _coerce_positive_int(value, key=key)
        return cfg

    if key == "assets.planner.inline_images":
        cfg.assets.planner.inline_images = _coerce_bool(value, key=key)
        return cfg

    if key == "assets.planner.max_inline_images":
        cfg.assets.planner.max_inline_images = _coerce_positive_int(value, key=key)
        return cfg

    if key == "assets.planner.readiness_policy":
        normalized = value.strip().lower()
        if normalized not in {"soft", "block", "partial"}:
            raise ConfigError(
                "assets.planner.readiness_policy must be one of: soft, block, partial"
            )
        cfg.assets.planner.readiness_policy = normalized  # type: ignore[assignment]
        return cfg

    if key == "assets.planner.readiness_timeout_seconds":
        cfg.assets.planner.readiness_timeout_seconds = _coerce_positive_float(value, key=key)
        return cfg

    if key == "assets.planner.max_chars_per_asset":
        cfg.assets.planner.max_chars_per_asset = _coerce_positive_int(value, key=key)
        return cfg

    if key == "assets.planner.max_primary_per_task":
        cfg.assets.planner.max_primary_per_task = _coerce_positive_int(value, key=key)
        return cfg

    if key == "assets.worker.inline_images":
        cfg.assets.worker.inline_images = _coerce_bool(value, key=key)
        return cfg

    if key == "assets.worker.max_inline_images":
        cfg.assets.worker.max_inline_images = _coerce_positive_int(value, key=key)
        return cfg

    if key == "assets.worker.fail_on_mirror_error":
        cfg.assets.worker.fail_on_mirror_error = _coerce_bool(value, key=key)
        return cfg

    if key == "assets.worker.allocator_role":
        role = value.strip().lower()
        if not role:
            raise ConfigError("assets.worker.allocator_role must be non-empty")
        cfg.assets.worker.allocator_role = role
        return cfg

    if key == "assets.worker.allocator_timeout_seconds":
        cfg.assets.worker.allocator_timeout_seconds = _coerce_positive_int(value, key=key)
        return cfg

    if key == "assets.worker.max_chars_per_asset_block":
        cfg.assets.worker.max_chars_per_asset_block = _coerce_positive_int(value, key=key)
        return cfg

    if key == "assets.worker.max_focused_extract_chars":
        cfg.assets.worker.max_focused_extract_chars = _coerce_positive_int(value, key=key)
        return cfg

    if key == "assets.worker.schema_version":
        cfg.assets.worker.schema_version = _coerce_positive_int(value, key=key)
        return cfg

    if key == "toolbar_items":
        raw = value.strip()
        if not raw:
            raise ConfigError("toolbar_items must be a JSON array of toolbar item names")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ConfigError("toolbar_items must be valid JSON array") from e
        if not isinstance(parsed, list):
            raise ConfigError("toolbar_items must be a JSON array")
        items: list[str] = []
        seen: set[str] = set()
        valid_items = ", ".join(sorted(_VALID_TOOLBAR_ITEMS))
        for item in parsed:
            if not isinstance(item, str):
                raise ConfigError("toolbar_items must be a JSON array of strings")
            name = item.strip().lower()
            if not name:
                raise ConfigError("toolbar_items cannot contain empty values")
            if name not in _VALID_TOOLBAR_ITEMS:
                raise ConfigError(f"Unknown toolbar item: {name}. Valid items: {valid_items}")
            if name in seen:
                continue
            seen.add(name)
            items.append(name)
        cfg.toolbar_items = items
        return cfg

    raise ConfigError(f"Unhandled key: {key}")
