"""Presentation-agnostic state machine for the full-screen TUI ``/config`` menu.

The classic configuration menu (:func:`cli_impl.config_menu.run_config_menu`) is a
Rich ``Live`` + ``create_input().raw_mode()`` flow. It cannot run inside the chat
alt-screen, so when ``ALYSIS_TUI`` is on bare ``/config`` previously degraded to a
read-only panel. This module re-expresses the *same* menu — Provider Profile · API
Key · Default Model · Context & Cache · Execution Limits · Subagent overrides ·
Forge overrides · Sandbox — as a step machine that holds state and renders a small :class:`Screen`
description. The prompt_toolkit overlay in :mod:`cli_impl.tui.config_overlay` reads
that description and routes key events back into the flow; nothing here imports
prompt_toolkit, so the whole thing is unit testable by driving the public methods.

The menu *logic* is reused wholesale from :mod:`cli_impl.config_menu`
(:class:`ConfigMenuState` and its row builders / validators / ``commit_to``), so
there is a single source of truth for "what config does"; this module only owns
"how the TUI walks through it". It mirrors the structure of
:mod:`cli_impl.tui.setup_flow`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ...config import (
    ConfigError,
    _normalize_web_search_mode,
    clear_persisted_api_key,
    clear_persisted_profile_key,
    load_config,
    save_config,
    save_persisted_api_key,
    save_persisted_profile_key,
)
from ...llm.protocols import validate_reasoning_trace_adapter_for_protocol
from ...profile_presets import (
    PROFILE_PRESETS,
    canonical_model_alias_for_preset,
    find_preset_for_profile,
    make_profile_from_preset,
    model_display_name,
    preset_selection_label,
)
from ...profiles import ProfileSpec, validate_base_url
from ...provider_auth import ProviderAccountStatus, ProviderAuthError, create_provider_auth
from ...provider_diagnostics import provider_diagnostic_warning_lines
from ...provider_url import display_endpoint, display_host
from ...reasoning_contracts import (
    ALWAYS_ON,
    OFF_SWAPS_MODEL,
    UNKNOWN_CONTRACT,
    ReasoningContract,
    reasoning_contract_for,
    reasoning_labels_allowed_by_contract,
)
from ...sandbox_settings import normalize_sandbox_mode
from ...web_search_adapters import normalize_web_search_adapter
from ...web_search_policy import normalize_web_search_policy
from ...workspace_binding import (
    WorkspaceBindingError,
    discover_workspace_candidates,
    resolve_workspace_binding,
)
from ..config_menu import (
    _ADVANCED_PROVIDER_PRESETS_VALUE,
    _CLEAR_KEY_PENDING,
    _CUSTOM_MODEL_VALUE,
    _MISSING_REQUIRED,
    _ROLE_TEMPERATURE_FIELDS,
    ANTHROPIC_PROMPT_CACHE_TTLS,
    FORGE_ROLE_ORDER,
    PROMPT_CACHE_MODES,
    ROLE_ORDER,
    ConfigMenuResult,
    ConfigMenuState,
    _active_alysis_profile,
    _active_preset,
    _active_subscription_profile,
    _advanced_profile_presets_for_setup,
    _api_key_summary_text,
    _cache_aware_summary_text,
    _cache_policy_summary_text,
    _default_model_picker_subtitle,
    _default_model_rows,
    _finite_float,
    _format_number,
    _is_direct_subscription_id,
    _looks_like_secret,
    _normalize_anthropic_prompt_cache_ttl,
    _normalize_bool_text,
    _normalize_cache_aware_min_trigger_ratio,
    _normalize_prompt_cache_mode,
    _ordered_profile_presets_for_setup,
    _override_summary_text,
    _parse_header_text,
    _pending_change_count,
    _resolved_cache_policy_for_state,
    _role_description,
    _role_label,
    _runtime_setup_rows,
    _sandbox_mode_env_override,
    _temperature_controls_available,
    _thinking_labels_for_state,
    _web_search_mode_rows,
    _web_search_policy_rows,
)
from ..setup_wizard import _suggest_workspace_default
from .setup_flow import Mode, Row, Screen, Tone

# The literal a user types in a profile-edit field to keep a value that tripped the
# accidental-secret guard (mirrors ``config_menu._SECRET_FORCE_TOKEN``).
_SECRET_FORCE_TOKEN = "force"

# Row value for the Alysis Code account on the AI-subscription screen. It is not
# a delegated runtime: connecting runs the device-flow login and activates the
# native `alysis` profile (free daily hosted DeepSeek).
_ALYSIS_CONNECTION_ID = "alysis"

# Sentinel row values on the top-level menu for the two trailing action rows.
_SAVE = "__save__"
_CANCEL = "__cancel__"

# One-line preset descriptions for the TUI picker; the full protocol prose from
# ``_preset_description`` stays in the classic Rich menu and docs.
_NATIVE_PROTOCOL_SHORT = {
    "openai_responses": "native · OpenAI Responses",
    "anthropic_messages": "native · Anthropic Messages",
    "gemini_generate_content": "native · Gemini GenerateContent",
}


def _short_preset_label(preset: Any) -> str:
    # "OpenAI - Native Responses" → "OpenAI"; the description carries the rest.
    return preset_selection_label(preset).split(" - ")[0]


def _thinking_labels_allowed_by_contract(
    contract: ReasoningContract, labels: list[str], *, current: str
) -> list[str]:
    """Filter thinking-picker options to what the model's contract accepts.

    Rules (capability report, Part A): an ``always-on`` model never shows
    "off" — unless "off" *swaps the model* (kimi-code), which stays visible so
    the substitution can be warned about instead of hidden. When the contract
    publishes an exact value set, out-of-set efforts are dropped. The current
    selection is never hidden (the user must always be able to see and change
    it), and an unknown contract leaves the list untouched.
    """
    return reasoning_labels_allowed_by_contract(contract, labels, current=current)


def _cache_mode_subtitle(state: Any) -> str:
    """Plain-language caching status for the section header.

    The old subtitle was the raw policy dump ("Effective: unsupported;
    strategy=none; source=default; allowed=none; emits=no fields; usage=none")
    — debug output, not UI. The full detail remains available in /status.
    """
    raw = _cache_policy_summary_text(_resolved_cache_policy_for_state(state), compact=True)
    if "unsupported" in raw:
        return (
            "The active provider does not support prompt caching; "
            "these settings apply where a provider does."
        )
    return f"Provider caching: {raw}"


def _pretty_key_source(source: str | None) -> str:
    """Humanize ``api_key_source`` values (``stored:profile=x`` → prose)."""
    raw = str(source or "").strip()
    if raw.startswith("stored:profile="):
        return f"saved in profile {raw.removeprefix('stored:profile=')}"
    if raw.startswith("env:"):
        return f"from ${raw.removeprefix('env:')}"
    if raw == "stored":
        return "saved in config"
    return raw or "not set"


def _short_preset_description(preset: Any) -> str:
    if preset.key == "alysis":
        return "Alysis Code endpoint — account sign-in"
    short = _NATIVE_PROTOCOL_SHORT.get(preset.protocol)
    if short:
        return short
    return display_host(preset.base_url) or "any OpenAI-compatible base URL"


def _profile_identity(raw: Any) -> tuple[str, str, str]:
    """(protocol, normalized base URL, casefolded model) for duplicate detection."""
    profile = raw if isinstance(raw, dict) else {}
    protocol = str(profile.get("protocol") or "openai_compat").strip()
    base_url = str(profile.get("base_url") or "").strip().rstrip("/")
    model = str(profile.get("default_model") or "").strip().casefold()
    return protocol, base_url, model


def _profile_retired_model_replacement(name: str, raw: Any) -> str:
    """The catalog's replacement id when a saved profile pins a retired model id.

    Empty when the model is current, unknown, or the profile matches no
    preset (custom endpoints cannot be judged offline).
    """
    profile = raw if isinstance(raw, dict) else {}
    model = str(profile.get("default_model") or "").strip()
    if not model:
        return ""
    try:
        spec = ProfileSpec(
            name=name,
            protocol=str(profile.get("protocol") or "openai_compat").strip(),
            base_url=str(profile.get("base_url") or "").strip(),
            default_model=model,
        )
        preset = find_preset_for_profile(spec)
    except Exception:  # noqa: BLE001 - a malformed saved profile must not break the picker
        return ""
    if preset is None:
        return ""
    canonical = canonical_model_alias_for_preset(preset, model)
    return canonical if canonical != model else ""


def _profile_row_descriptions(profiles: dict[str, Any], *, active: str | None) -> dict[str, str]:
    """One summary line per saved profile: ``model · host`` plus what needs attention.

    The bare name list hid everything that matters when choosing or pruning a
    profile — which model it pins, which endpoint it hits, whether that model
    id has been retired (the catalog will remap it, but the profile should be
    updated), and whether it duplicates another profile.

    The endpoint is :func:`display_endpoint`, not the raw ``netloc``: that
    keeps any ``user:token@`` credentials out of the list, and names the hosted
    gateway instead of printing its Supabase project host.
    """
    identities: dict[tuple[str, str, str], list[str]] = {}
    for name in sorted(profiles):
        identities.setdefault(_profile_identity(profiles[name]), []).append(name)

    descriptions: dict[str, str] = {}
    for name in sorted(profiles):
        raw = profiles[name]
        profile = raw if isinstance(raw, dict) else {}
        model = str(profile.get("default_model") or "").strip()
        host = display_endpoint(str(profile.get("base_url") or ""))
        parts: list[str] = []
        if name == active:
            parts.append("active")
        parts.append(model or "no model")
        if host:
            parts.append(host)
        replacement = _profile_retired_model_replacement(name, raw)
        if replacement:
            parts.append(f"retired → {replacement}")
        twins = [other for other in identities.get(_profile_identity(raw), []) if other != name]
        if twins and model:
            parts.append(f"same as {twins[0]}")
        descriptions[name] = " · ".join(parts)
    return descriptions


# stage → interaction mode, so the overlay's key-binding filters don't have to
# build a full :class:`Screen` on every repaint.
_STAGE_MODE: dict[str, Mode] = {
    "menu": "list",
    "execution_backend": "list",
    "execution_runtime": "list",
    "subscription_account": "list",
    "subscription_disconnect_confirm": "confirm",
    "subscription_connecting": "busy",
    "subscription_disconnecting": "busy",
    "advanced": "list",
    "subagent_roles": "list",
    "forge_roles": "list",
    "workspace": "list",
    "workspace_path": "input",
    "workspace_action": "list",
    "switching": "busy",
    "sandbox": "list",
    "personas": "list",
    "model": "list",
    "custom_model": "input",
    "model_base_url": "input",
    "model_thinking": "list",
    "request_timeout": "input",
    "web_search_policy": "list",
    "web_search_mode": "list",
    "cache_mode": "list",
    "cache_key": "input",
    "cache_retention": "input",
    "cache_anthropic_enabled": "list",
    "cache_anthropic_ttl": "list",
    "cache_compaction_enabled": "list",
    "cache_compaction_min_trigger": "input",
    "subagent_field": "input",
    "forge_field": "input",
    "api_key": "input",
    "api_key_clear_confirm": "confirm",
    "provider": "list",
    "provider_switch": "list",
    "provider_add_preset": "list",
    "provider_add_preset_advanced": "list",
    "provider_preset_name": "input",
    "provider_preset_url": "input",
    "provider_custom_name": "input",
    "provider_custom_url": "input",
    "provider_custom_headers": "input",
    "provider_edit": "input",
    "provider_remove": "list",
    "provider_remove_confirm": "confirm",
    "saving": "busy",
    "cancel_confirm": "confirm",
    "done": "done",
}

# Esc/back: the stage to return to from non-sequence stages.
_PREV: dict[str, str] = {
    "advanced": "menu",
    "execution_backend": "menu",
    "execution_runtime": "execution_backend",
    "subscription_account": "execution_runtime",
    "subscription_disconnect_confirm": "subscription_account",
    "workspace": "menu",
    "workspace_path": "workspace",
    "workspace_action": "workspace",
    "sandbox": "menu",
    "personas": "menu",
    "model": "menu",
    "custom_model": "model",
    "model_base_url": "model",
    "model_thinking": "model_base_url",
    "request_timeout": "advanced",
    "web_search_policy": "menu",
    "web_search_mode": "web_search_policy",
    "cache_mode": "menu",
    "cache_key": "cache_mode",
    "cache_retention": "cache_key",
    "cache_anthropic_enabled": "cache_retention",
    "cache_anthropic_ttl": "cache_anthropic_enabled",
    "cache_compaction_enabled": "cache_anthropic_ttl",
    "cache_compaction_min_trigger": "cache_compaction_enabled",
    "api_key": "menu",
    "api_key_clear_confirm": "api_key",
    "provider": "menu",
    "provider_switch": "provider",
    "provider_add_preset": "provider",
    "provider_add_preset_advanced": "provider_add_preset",
    "provider_preset_name": "provider_add_preset",
    "provider_preset_url": "provider_preset_name",
    "provider_custom_name": "provider",
    "provider_custom_url": "provider_custom_name",
    "provider_custom_headers": "provider_custom_url",
    "provider_edit": "provider",
    "provider_remove": "provider",
    "provider_remove_confirm": "provider_remove",
}

# stage → footer breadcrumb label.
_BREADCRUMB: dict[str, str] = {
    "menu": "configuration",
    "execution_backend": "model access",
    "execution_runtime": "model access",
    "subscription_account": "model access",
    "subscription_disconnect_confirm": "model access",
    "subscription_connecting": "model access",
    "subscription_disconnecting": "model access",
    "advanced": "advanced",
    "workspace": "project",
    "workspace_path": "project",
    "workspace_action": "project",
    "switching": "saving",
    "sandbox": "sandbox",
    "personas": "personas",
    "model": "default model",
    "custom_model": "default model",
    "model_base_url": "default model",
    "model_thinking": "default model",
    "request_timeout": "advanced",
    "web_search_policy": "web search",
    "web_search_mode": "web search",
    "cache_mode": "context & cache",
    "cache_key": "context & cache",
    "cache_retention": "context & cache",
    "cache_anthropic_enabled": "context & cache",
    "cache_anthropic_ttl": "context & cache",
    "cache_compaction_enabled": "context & cache",
    "cache_compaction_min_trigger": "context & cache",
    "subagent_field": "subagent overrides",
    "forge_field": "forge overrides",
    "api_key": "api key",
    "api_key_clear_confirm": "api key",
    "saving": "saving",
    "cancel_confirm": "configuration",
}

_THINKING_DESCRIPTIONS = {
    "off": "no extra reasoning tokens",
    "minimal": "minimal reasoning budget",
    "low": "small reasoning budget",
    "medium": "medium reasoning budget",
    "high": "large reasoning budget",
    "xhigh": "maximum reasoning budget when supported",
    "max": "provider maximum reasoning budget when supported",
    "ultra": "provider ultra reasoning budget when supported",
    "auto": "let the provider decide",
}
_CACHE_MODE_DESCRIPTIONS = {
    "auto": "use supported provider caching; default for Alysis hosted models",
    "manual": "use the manual key/retention below when supported",
    "off": "disable optional client cache controls",
}
_CACHE_TTL_DESCRIPTIONS = {
    "5m": "lower lifetime; safest default",
    "1h": "longer lifetime for stable coding sessions",
}
_PROVIDER_ACTION_ROWS = (
    ("switch", "Switch active profile", "Choose another configured profile."),
    ("add_preset", "Add from preset", "Pick a known provider (OpenAI, Anthropic, …)"),
    ("add_custom", "Add custom", "Use any OpenAI-compatible base URL"),
    ("edit", "Edit current", "Change URL, model, trace adapter, headers, and notes"),
    ("remove", "Remove", "Delete a profile (applied on save)"),
    ("back", "Back", "Return to the menu"),
)
_EDIT_FIELDS = (
    "base_url",
    "api_key_env",
    "default_model",
    "reasoning_trace_adapter",
    "web_search_adapter",
    "web_search_model",
    "extra_headers",
    "notes",
)
_EDIT_LABELS = {
    "base_url": "Base URL",
    "api_key_env": "API key env var NAME (not the key itself)",
    "default_model": "Default model",
    "reasoning_trace_adapter": "Reasoning trace adapter",
    "web_search_adapter": "Web search adapter",
    "web_search_model": "Web search model",
    "extra_headers": "Extra headers (k=v, comma-separated)",
    "notes": "Notes",
}


def _strip_markup(text: str) -> str:
    """Drop Rich ``[style]…[/style]`` tags so menu summaries render as plain text."""
    import re

    return re.sub(r"\[/?[a-z][a-z0-9 _#-]*\]", "", str(text)).strip()


class ConfigFlow:
    """Drives the TUI configuration menu one screen at a time.

    The overlay calls :meth:`screen` to render, the navigation/selection methods on
    key events, and — on the single ``saving`` busy stage — :meth:`run_busy` on a
    worker. Every transition is synchronous and side-effect-explicit so tests can
    walk the whole flow without a terminal.
    """

    def __init__(self, *, cfg: Any | None = None, current_workspace: str | None = None) -> None:
        self.cfg = cfg if cfg is not None else load_config()
        self.state = ConfigMenuState.from_cfg(self.cfg)
        # The live session's workspace root (display only); the running session is
        # bound to it and cannot change it in place — switching restarts the chat.
        self.current_workspace = str(current_workspace or "")
        self.stage = "menu"
        self.index = 0
        self.status = ""
        self.status_tone: Tone = "dim"
        # Terminal results.
        self.success: bool | None = None
        self.saved: bool = False
        self.result: ConfigMenuResult | None = None
        self.changes_count: int = 0
        # Set when the user chooses "Switch now": the overlay reads it on close and
        # asks the host to relaunch the chat bound to this folder (fresh session).
        self.switch_workspace: str | None = None
        self._switch_target: str | None = None
        self._pending_workspace = ""
        # Discovered project candidates are cached (filesystem scan); screen() is
        # pull-based and runs on every repaint, so we must not re-scan per frame.
        self._ws_candidates_cache: list[Any] | None = None
        # Transient sub-flow state.
        self._resume_stage = "menu"
        self._sub_steps: list[tuple[str, str]] = []
        self._sub_i = 0
        self._forge_role = ""
        self._sub_model_snapshot: dict[str, str] = {}
        self._sub_temp_snapshot: dict[str, str] = {}
        self._forge_i = 0
        self._forge_snapshot: dict[str, str] = {}
        self._pending_preset: Any = None
        self._pending_preset_profile: ProfileSpec | None = None
        self._preset_setup_chain = False
        self._custom_name = ""
        self._custom_url = ""
        self._edit_profile: ProfileSpec | None = None
        self._edit_values: dict[str, str] = {}
        self._edit_i = 0
        self._edit_secret_candidate: str | None = None
        self._pending_remove = ""
        self._subscription_provider_id = ""
        self._subscription_account_status: ProviderAccountStatus | None = None
        self._subscription_switch_account = False
        self._subscription_save_pending = False
        self._subscription_action_outcome: tuple[str, ProviderAccountStatus | None, str] | None = (
            None
        )
        # Save outcome recorded by the blocking I/O step (:meth:`_perform_save`) and
        # applied as a stage transition by :meth:`apply_save_outcome`. Splitting the
        # two lets the overlay run the I/O on a worker while every renderer-visible
        # stage/status write stays on the UI thread.
        self._save_outcome: tuple[str, str] | None = None

    # ---------------------------------------------------------------- helpers

    def current_mode(self) -> Mode:
        return _STAGE_MODE.get(self.stage, "message")

    def busy_kind(self) -> str:
        return "thread"

    def open_default_model(self) -> None:
        """Open the model picker with the configured model selected."""
        self._goto("model", index=self._model_index())

    def field_key(self) -> str:
        """A key that changes whenever the active input field changes.

        The overlay seeds/clears its input box when this changes, so a single
        ``input`` stage that walks several fields (subagent/forge/edit sequences)
        re-seeds correctly between fields.
        """
        if self.stage == "subagent_field":
            return f"subagent_field:{self._sub_i}"
        if self.stage == "forge_field":
            return f"forge_field:{self._forge_role or '0'}"
        if self.stage == "provider_edit":
            return f"provider_edit:{self._edit_i}"
        return self.stage

    def _set_status(self, text: str, tone: Tone = "dim") -> None:
        self.status = text
        self.status_tone = tone

    def _goto(self, stage: str, *, index: int = 0, keep_status: bool = False) -> None:
        if stage == "menu" and index == 0:
            index = getattr(self, "_menu_resume_index", 0)
        self.stage = stage
        self.index = index
        if not keep_status:
            self.status = ""
            self.status_tone = "dim"

    def _finish(self, success: bool) -> None:
        self.success = success
        self.stage = "done"

    def _breadcrumb(self) -> str:
        if self.stage.startswith("provider"):
            return "provider profile"
        return _BREADCRUMB.get(self.stage, "configuration")

    def _provider_diag_first(self) -> str:
        try:
            issues = list(provider_diagnostic_warning_lines(self.state._resolution_cfg()))
        except Exception:  # noqa: BLE001 - diagnostics are best-effort
            return ""
        return issues[0] if issues else ""

    # ------------------------------------------------------------- rendering

    def screen(self) -> Screen:
        builder = getattr(self, f"_screen_{self.stage}", None)
        if builder is None:
            scr = Screen(
                stage=self.stage,
                mode="message",
                title="Configuration",
                lines=[(f"(unknown stage {self.stage})", "err")],
            )
        else:
            scr = builder()
        scr.index = self.index
        # Never leave the cursor on a header/spacer row (e.g. index 0 of the
        # grouped menu): snap forward to the first selectable item.
        if scr.mode == "list" and scr.rows:
            n = len(scr.rows)
            i = scr.index if 0 <= scr.index < n else 0
            for _ in range(n):
                if getattr(scr.rows[i], "kind", "item") == "item":
                    break
                i = (i + 1) % n
            scr.index = i
            self.index = i
        if not scr.status:
            scr.status = self.status
            scr.status_tone = self.status_tone
        scr.progress = self._breadcrumb()
        scr.success = self.success
        return scr

    def _screen_menu(self) -> Screen:
        # Grouped, breathing layout: one short fact per row — the full detail
        # lives inside each section screen. Per-role model overrides (subagent +
        # forge) stay tucked under a single "Advanced" entry.
        st = self.state
        delegated = st.execution_backend == "delegated" and not _is_direct_subscription_id(
            st.execution_runtime
        )
        direct_sub = st.execution_backend == "delegated" and _is_direct_subscription_id(
            st.execution_runtime
        )
        hosted_account = _active_alysis_profile(st) is not None
        native_suffix = " (inactive)" if delegated else ""
        profile_desc, profile_tone = self._short_profile(delegated)
        model_desc, model_tone = self._short_model(delegated)
        key_desc, key_tone = self._short_api_key(delegated, direct_sub)
        access_desc, access_tone = self._short_execution()

        rows = [
            Row(label="Workspace", kind="header"),
            Row(label="Project", description=self._short_workspace(), value="workspace"),
            Row(label="Sandbox", description=self._short_sandbox(), value="sandbox"),
            Row(label="Personas", description=self._short_personas(), value="personas"),
            Row(label="", kind="spacer"),
            Row(label="Model", kind="header"),
            Row(label="Model Access", description=access_desc, value="execution", tone=access_tone),
            Row(
                label=f"Provider Profile{native_suffix}",
                description=profile_desc,
                value="profile",
                tone=profile_tone,
            ),
            Row(
                label=f"Default Model{native_suffix}",
                description=model_desc,
                value="default",
                tone=model_tone,
            ),
            # The status ("not used" / "inactive") lives in the description —
            # repeating it as a label suffix read twice on one row.
            Row(
                label="Account" if hosted_account else "API Key",
                description=key_desc,
                value="api_key",
                tone=key_tone,
            ),
            Row(label="", kind="spacer"),
            Row(label="Behavior", kind="header"),
            Row(label="Web Search", description=self._short_web_search(), value="web_search"),
            Row(label="Context & Cache", description=self._short_cache(), value="cache"),
            Row(label="Advanced", description=self._short_advanced(), value="advanced"),
            Row(label="", kind="spacer"),
            Row(label="Save & exit", description="write changes to disk and close", value=_SAVE),
            Row(label="Discard & close", description="close without saving", value=_CANCEL),
        ]
        pending = _pending_change_count(self.state)
        subtitle = f"{pending} pending change(s)" if pending else "No pending changes."
        if self.state.config_warning:
            subtitle += f"  ·  {self.state.config_warning}"
        return Screen(
            stage="menu",
            mode="list",
            title="Alysis Code configuration",
            subtitle=subtitle,
            rows=rows,
            numbered=False,
            hint="",
        )

    # ------------------------------------------------- short menu summaries
    #
    # The grouped top-level menu shows ONE short fact per row; the dense
    # multi-fragment summaries remain available inside each section screen.

    def _short_workspace(self) -> str:
        if self.current_workspace:
            return Path(self.current_workspace).name
        default = self.state.default_workspace_path
        if default:
            return f"default: {Path(default).name}"
        return "not set"

    def _short_sandbox(self) -> str:
        mode = normalize_sandbox_mode(self.state.fields.get("sandbox_mode", "strict"))
        base = {
            "strict": "strict — sandboxed",
            "warn": "warn — fail-closed",
            "off": "off — host execution",
        }[mode]
        if _sandbox_mode_env_override() is not None:
            return f"{base} · env override"
        return base

    def _short_execution(self) -> tuple[str, str]:
        st = self.state
        if _active_alysis_profile(st) is not None:
            return "Alysis Code subscription", ""
        if st.execution_backend == "native":
            return "API key", ""
        runtime = str(st.execution_runtime or "").strip()
        if not runtime:
            return "subscription not configured", "warn"
        labels = {value: label for value, label, _description in _runtime_setup_rows()}
        return labels.get(runtime, runtime), ""

    def _short_profile(self, delegated: bool) -> tuple[str, str]:
        st = self.state
        if st.active_profile:
            return st.active_profile, ""
        if st.profiles:
            return f"{len(st.profiles)} profiles, none active", "" if delegated else "warn"
        return ("not configured", "") if delegated else ("missing — required", "warn")

    def _short_model(self, delegated: bool) -> tuple[str, str]:
        model = self.state.fields.get("model", "").strip()
        if model:
            return model_display_name(model), ""
        return ("not configured", "") if delegated else ("missing — required", "warn")

    def _short_api_key(self, delegated: bool, direct_sub: bool) -> tuple[str, str]:
        if direct_sub:
            return "not used", ""
        if delegated:
            return "inactive", ""
        if _active_alysis_profile(self.state) is not None:
            from ... import account_login

            if account_login.login_status(self.cfg).logged_in:
                return "connected", ""
            return "not connected — sign in", "warn"
        raw = _api_key_summary_text(self.state)
        if raw == _MISSING_REQUIRED:
            return "missing — required", "warn"
        if raw == _CLEAR_KEY_PENDING:
            return "cleared on save", "warn"
        return _strip_markup(raw), ""

    def _short_web_search(self) -> str:
        policy = normalize_web_search_policy(self.state.fields.get("web_search_policy", "auto"))
        return "model decides" if policy == "auto" else "off"

    def _short_cache(self) -> str:
        mode = _normalize_prompt_cache_mode(self.state.fields.get("prompt_cache_mode", "manual"))
        compaction = _cache_aware_summary_text(self.state).split(" · ")[0]
        return f"{mode} · {compaction}"

    def _short_advanced(self) -> str:
        sub = self._subagent_override_summary()
        forge = _override_summary_text(self.state.forge_role_models)
        if sub == "none" and forge == "none":
            return "model overrides · request timeout"
        return f"subagents {sub} · forge {forge}"

    def _screen_execution_backend(self) -> Screen:
        hosted_account = _active_alysis_profile(self.state) is not None
        return Screen(
            stage="execution_backend",
            mode="list",
            title="Model Access",
            subtitle="How would you like to connect Alysis Code to AI models?",
            rows=[
                Row(
                    label="Use an API key",
                    description="Connect directly to a supported model provider.",
                    value="native",
                    current=self.state.execution_backend == "native" and not hosted_account,
                ),
                Row(
                    label="Use an AI subscription",
                    description=(
                        "Sign in through a supported provider connection; API-key settings stay saved."
                    ),
                    value="delegated",
                    current=self.state.execution_backend == "delegated" or hosted_account,
                ),
            ],
            hint="",
        )

    def _screen_execution_runtime(self) -> Screen:
        # The Alysis Code account leads: browser sign-in, no API key, free daily
        # hosted DeepSeek. It activates the native `alysis` profile rather
        # than a delegated runtime.
        rows = [
            Row(
                label="Alysis Code account",
                description="Free daily DeepSeek included — browser sign-in, no API key.",
                value=_ALYSIS_CONNECTION_ID,
                current=_active_alysis_profile(self.state) is not None,
            )
        ]
        rows.extend(
            Row(
                label=label,
                description=description,
                value=runtime_id,
                current=runtime_id == self.state.execution_runtime,
            )
            for runtime_id, label, description in _runtime_setup_rows()
        )
        return Screen(
            stage="execution_runtime",
            mode="list",
            title="AI Subscription",
            subtitle=("Choose a provider to connect, reconnect, switch accounts, or disconnect."),
            rows=rows,
            hint="",
        )

    def _refresh_subscription_account_status(self) -> ProviderAccountStatus:
        provider_id = self._subscription_provider_id or self.state.execution_runtime
        if provider_id == _ALYSIS_CONNECTION_ID:
            from ... import account_login

            login = account_login.login_status(self.cfg)
            status = ProviderAccountStatus(
                connected=login.logged_in,
                verified=login.logged_in,
                account_label=("Alysis Code account" if login.logged_in else None),
                detail=(
                    "Free daily DeepSeek included."
                    if login.logged_in
                    else "Sign in with your browser — no API key needed."
                ),
            )
            self._subscription_account_status = status
            return status
        try:
            status = create_provider_auth(provider_id).account_status()
        except (ProviderAuthError, ValueError) as exc:
            status = ProviderAccountStatus(
                connected=False,
                verified=False,
                detail=str(exc),
            )
        self._subscription_account_status = status
        return status

    def _screen_subscription_account(self) -> Screen:
        status = self._subscription_account_status or self._refresh_subscription_account_status()
        account = status.account_label or ("connected" if status.connected else "not connected")
        rows = [
            Row(
                label=("Reconnect / switch account" if status.connected else "Connect account"),
                description="Open the provider sign-in flow in your browser.",
                value="connect",
            )
        ]
        if status.connected:
            rows.append(
                Row(
                    label="Disconnect account",
                    description="Remove locally stored subscription credentials.",
                    value="disconnect",
                )
            )
        provider_id = self._subscription_provider_id or self.state.execution_runtime
        if status.connected and provider_id == _ALYSIS_CONNECTION_ID:
            # Alysis Code login already activated + persisted the hosted profile,
            # so there is nothing left to save — offer a direct exit to chat.
            rows.append(
                Row(
                    label="Done — start chatting",
                    description="Close configuration and return to chat.",
                    value="chat",
                )
            )
        elif status.connected:
            rows.append(
                Row(
                    label="Save & start chatting",
                    description="Choose a model if needed, save changes, and return to chat.",
                    value=_SAVE,
                )
            )
        rows.append(Row(label="Back", description="Return to configuration.", value="back"))
        detail = str(status.detail or "").strip()
        subtitle = f"Account: {account}"
        if detail:
            subtitle += f"  ·  {detail}"
        return Screen(
            stage="subscription_account",
            mode="list",
            title="AI Subscription Account",
            subtitle=subtitle,
            rows=rows,
            hint="",
        )

    def _screen_subscription_disconnect_confirm(self) -> Screen:
        return Screen(
            stage="subscription_disconnect_confirm",
            mode="confirm",
            title="Disconnect AI subscription",
            lines=[("Remove the locally stored subscription credentials?", "warn")],
            hint="Y disconnect · N keep · Esc back",
            confirm_default=False,
        )

    def _screen_subscription_connecting(self) -> Screen:
        return Screen(
            stage="subscription_connecting",
            mode="busy",
            title="Connect AI subscription",
            busy_label="Opening provider sign-in in your browser…",
        )

    def _screen_subscription_disconnecting(self) -> Screen:
        return Screen(
            stage="subscription_disconnecting",
            mode="busy",
            title="Disconnect AI subscription",
            busy_label="Removing subscription credentials…",
        )

    def _advanced_summary(self) -> str:
        sub = self._subagent_override_summary()
        forge = _override_summary_text(self.state.forge_role_models)
        if sub == "none" and forge == "none":
            return "per-role model overrides (none set)"
        return f"subagents {sub} · forge {forge}"

    def _workspace_summary(self) -> str:
        cur = Path(self.current_workspace).name if self.current_workspace else "—"
        default = self.state.default_workspace_path
        if default:
            return f"current: {cur} · default: {Path(default).name}"
        return f"current: {cur} · switch project or set a default"

    def _subagent_override_summary(self) -> str:
        values = {role: self.state.role_models.get(role, "") for role in ROLE_ORDER}
        if _temperature_controls_available(self.state):
            values.update(
                {
                    f"{role}_temperature": value
                    for role, value in self.state.role_temperatures.items()
                }
            )
        return _override_summary_text(values)

    def _screen_advanced(self) -> Screen:
        rows = [
            Row(
                label="Request timeout",
                description=f"{self.state.fields['llm_timeout_s']} seconds",
                value="request_timeout",
            ),
            Row(
                label="Subagent model overrides",
                description=self._subagent_override_summary(),
                value="subagents",
            ),
            Row(
                label="Forge model overrides",
                description=_override_summary_text(self.state.forge_role_models),
                value="forge",
            ),
            Row(label="Back", description="Return to the menu", value="back"),
        ]
        return Screen(
            stage="advanced",
            mode="list",
            title="Advanced",
            subtitle="Optional request settings and per-role model overrides.",
            rows=rows,
            hint="",
        )

    def _role_override_row(self, role: str, *, model: str, temperature: str = "") -> Row:
        parts = [model or "inherit default"]
        if temperature:
            parts.append(f"temp {temperature}")
        return Row(label=_role_label(role), description=" · ".join(parts), value=role)

    def _screen_subagent_roles(self) -> Screen:
        rows = [
            self._role_override_row(
                role,
                model=str(self.state.role_models.get(role, "") or ""),
                temperature=str(self.state.role_temperatures.get(role, "") or ""),
            )
            for role in ROLE_ORDER
        ]
        rows.append(Row(label="Back", description="Return to Advanced", value="back"))
        return Screen(
            stage="subagent_roles",
            mode="list",
            title="Subagent model overrides",
            subtitle="Pick a role to edit; roles without an override inherit the default model.",
            rows=rows,
            hint="",
        )

    def _screen_forge_roles(self) -> Screen:
        rows = [
            self._role_override_row(
                role, model=str(self.state.forge_role_models.get(role, "") or "")
            )
            for role in FORGE_ROLE_ORDER
        ]
        rows.append(Row(label="Back", description="Return to Advanced", value="back"))
        return Screen(
            stage="forge_roles",
            mode="list",
            title="Forge model overrides",
            subtitle="Pick a role to edit; roles without an override inherit the default model.",
            rows=rows,
            hint="",
        )

    # ----------------------------------------------------------- project / workspace

    def _workspace_candidates(self) -> list[Any]:
        if self._ws_candidates_cache is None:
            self._ws_candidates_cache = self._discover_workspace_candidates()
        return self._ws_candidates_cache

    def _discover_workspace_candidates(self) -> list[Any]:
        bases: list[Path] = []
        try:
            bases.append(_suggest_workspace_default())
        except Exception:  # noqa: BLE001 - discovery is best-effort
            pass
        if self.current_workspace:
            try:
                parent = Path(self.current_workspace).expanduser().parent
                if parent not in bases:
                    bases.append(parent)
            except Exception:  # noqa: BLE001
                pass
        out: list[Any] = []
        seen: set[str] = set()
        for base in bases:
            try:
                found = discover_workspace_candidates(base)
            except Exception:  # noqa: BLE001
                continue
            for cand in found:
                key = os.fspath(cand.path)
                if key in seen:
                    continue
                seen.add(key)
                out.append(cand)
        return out[:8]

    def _screen_workspace(self) -> Screen:
        rows: list[Row] = []
        for cand in self._workspace_candidates():
            rows.append(
                Row(
                    label=cand.path.name or os.fspath(cand.path),
                    description=getattr(cand, "summary", "") or os.fspath(cand.path),
                    value=os.fspath(cand.path),
                    current=os.fspath(cand.path) == self.current_workspace,
                )
            )
        rows.append(
            Row(label="Type a path…", description="Enter any folder path.", value="__type_path__")
        )
        rows.append(Row(label="Back", description="Return to the menu.", value="back"))
        cur = self.current_workspace or "—"
        default = self.state.default_workspace_path or "none"
        return Screen(
            stage="workspace",
            mode="list",
            title="Project / Workspace",
            subtitle=f"Current: {cur}  ·  default: {default}",
            rows=rows,
            hint="",
        )

    def _screen_workspace_path(self) -> Screen:
        return Screen(
            stage="workspace_path",
            mode="input",
            title="Project / Workspace",
            subtitle="Type the folder you want to work in.",
            input_label="Workspace folder",
            input_default=self.current_workspace,
            hint="Enter next · Esc back",
        )

    def _screen_workspace_action(self) -> Screen:
        rows = [
            Row(
                label="Switch now — restart chat here",
                description="Save changes and reopen the chat in this project (fresh conversation).",
                value="switch",
            ),
            Row(
                label="Set as default — next launch",
                description="Use this project next time you start Alysis Code. Current session stays.",
                value="set_default",
            ),
            Row(label="Back", description="Pick a different folder.", value="back"),
        ]
        return Screen(
            stage="workspace_action",
            mode="list",
            title="Project / Workspace",
            subtitle=f"Selected: {self._pending_workspace}",
            rows=rows,
            hint="",
        )

    def _screen_switching(self) -> Screen:
        return Screen(
            stage="switching",
            mode="busy",
            title="Switching project",
            busy_label="Saving and switching project…",
        )

    _PERSONA_ORDER = ("code", "architect", "ask", "debug")
    _PERSONA_ROWS = (
        ("code", "Code (default)", "Implementation work; keeps your execution mode."),
        ("architect", "Architect", "Plans and designs; writes markdown documents only."),
        ("ask", "Ask", "Read-only questions with inspection tools."),
        ("debug", "Debug", "Reproduce-before-fix; keeps your execution mode."),
    )

    def _current_default_persona(self) -> str:
        persona = str(self.state.fields.get("default_persona") or "code").strip().lower()
        return persona if persona in self._PERSONA_ORDER else "code"

    def _short_personas(self) -> str:
        return self._current_default_persona()

    def _persona_index(self) -> int:
        return self._PERSONA_ORDER.index(self._current_default_persona())

    def _screen_personas(self) -> Screen:
        current = self._current_default_persona()
        rows = [
            Row(label=label, description=description, value=value, current=current == value)
            for value, label, description in self._PERSONA_ROWS
        ]
        return Screen(
            stage="personas",
            mode="list",
            title="Personas",
            subtitle=(
                "The persona chat starts in. Switch live with /persona or Tab; "
                "per-persona model roles via persona_models.<persona>."
            ),
            rows=rows,
            hint="",
        )

    def _choose_personas(self, value: str) -> None:
        persona = str(value or "code").strip().lower()
        if persona not in self._PERSONA_ORDER:
            persona = "code"
        self.state.fields["default_persona"] = persona
        self._goto("menu")
        self._set_status(f"Default persona: {persona}. Save to apply.", "ok")

    def _screen_sandbox(self) -> Screen:
        current = normalize_sandbox_mode(self.state.fields.get("sandbox_mode", "strict"))
        rows = [
            Row(
                label="Strict (recommended)",
                description="Run shell & verification commands in a sandbox (bubblewrap/Docker).",
                value="strict",
                current=current == "strict",
            ),
            Row(
                label="Warn",
                description="Try the sandbox; fail closed if no backend (no host fallback).",
                value="warn",
                current=current == "warn",
            ),
            Row(
                label="Off (less safe)",
                description="Run commands directly on the host shell. No isolation.",
                value="off",
                current=current == "off",
            ),
        ]
        subtitle = "How Alysis Code isolates shell and verification commands."
        override = _sandbox_mode_env_override()
        if override:
            subtitle += f"  ·  Note: {override} overrides this at runtime."
        return Screen(
            stage="sandbox",
            mode="list",
            title="Sandbox",
            subtitle=subtitle,
            rows=rows,
            hint="",
        )

    def _screen_model(self) -> Screen:
        current = str(self.state.fields.get("model", "") or "").strip()
        rows = [
            Row(
                label=label,
                # The "(current)" tag already says it — repeating "current
                # configured model" as the description is noise.
                description="" if desc == "current configured model" else desc,
                value=value,
                current=value == current,
            )
            for value, label, desc in _default_model_rows(self.state)
        ]
        subtitle = _default_model_picker_subtitle(self.state)
        if (
            self.state.execution_backend == "delegated"
            and _active_subscription_profile(self.state) is None
        ):
            subtitle += (
                "  API-key model setting is preserved but inactive while using an AI subscription."
            )
        status = ""
        status_tone: Tone = "dim"
        if not rows:
            # Never render a bare, row-less screen (e.g. subscription mode with
            # no live catalog and no saved model): explain why it is empty and
            # leave the custom-name escape hatch selectable.
            rows = [
                Row(
                    label="Type a custom model name",
                    description="use a model id you know is available",
                    value=_CUSTOM_MODEL_VALUE,
                )
            ]
            status = "No models advertised yet — sign in first (/login) or type a name."
            status_tone = "warn"
        elif self.state._provider_model_catalog_warning:
            status = self.state._provider_model_catalog_warning
            status_tone = "warn"
        return Screen(
            stage="model",
            mode="list",
            title="Default model",
            subtitle=subtitle,
            rows=rows,
            status=status,
            status_tone=status_tone,
            hint="",
        )

    def _screen_custom_model(self) -> Screen:
        return Screen(
            stage="custom_model",
            mode="input",
            title="Default model",
            subtitle="Type any model id supported by the active provider.",
            input_label="Model",
            input_default=str(self.state.fields.get("model", "")),
            hint="Enter next · Esc back",
        )

    def _screen_model_base_url(self) -> Screen:
        return Screen(
            stage="model_base_url",
            mode="input",
            title="Default model",
            subtitle="Base URL for the active provider (blank keeps the current value).",
            input_label="Base URL",
            input_default=str(self.state.fields.get("base_url", "")),
            hint="Enter next · Esc back",
        )

    def _screen_model_thinking(self) -> Screen:
        current = self.state.thinking_label
        labels = _thinking_labels_for_state(self.state)
        preset = _active_preset(self.state)
        contract = reasoning_contract_for(
            getattr(preset, "provider_key", None),
            self.state.fields.get("model", ""),
            preset_key=getattr(preset, "key", None),
        )
        labels = _thinking_labels_allowed_by_contract(contract, labels, current=current)
        rows = []
        for label in labels:
            description = _THINKING_DESCRIPTIONS.get(label, "provider-specific reasoning budget")
            tone = ""
            if label.casefold() == "off" and contract.off == OFF_SWAPS_MODEL:
                # kimi-code: "off" is not a speed knob — a different model answers.
                description = "silently routes the request to K2.6 — a different model answers"
                tone = "warn"
            rows.append(
                Row(
                    label=label,
                    description=description,
                    value=label,
                    current=label == current,
                    tone=tone,
                )
            )
        subtitle = (
            "Reasoning effort supported by the selected subscription model."
            if _active_subscription_profile(self.state) is not None
            else "Reasoning effort. Some providers ignore this until they add native support."
        )
        if preset is not None and preset.key == "nvidia" and contract is UNKNOWN_CONTRACT:
            subtitle = (
                "No verified reasoning control is known for this NVIDIA-hosted model; "
                "Auto sends no guessed parameter."
            )
        if contract.mode == ALWAYS_ON:
            subtitle += "  Reasoning cannot be disabled on this model."
        return Screen(
            stage="model_thinking",
            mode="list",
            title="Default model",
            subtitle=subtitle,
            rows=rows,
            hint="",
        )

    def _screen_request_timeout(self) -> Screen:
        return Screen(
            stage="request_timeout",
            mode="input",
            title="Request timeout",
            subtitle="How long to wait for a model response.",
            input_label="Request timeout (seconds)",
            input_default=str(self.state.fields.get("llm_timeout_s", "")),
            hint="Enter save · Esc back",
        )

    def _screen_web_search_policy(self) -> Screen:
        current = normalize_web_search_policy(self.state.fields.get("web_search_policy", "auto"))
        return Screen(
            stage="web_search_policy",
            mode="list",
            title="Web Search Access",
            subtitle="Whether the active model can decide to search the web.",
            rows=[
                Row(label=label, description=description, value=value, current=value == current)
                for value, label, description in _web_search_policy_rows()
            ],
            hint="",
        )

    def _screen_web_search_mode(self) -> Screen:
        current = _normalize_web_search_mode(self.state.fields.get("web_search_mode", "auto"))
        return Screen(
            stage="web_search_mode",
            mode="list",
            title="Web Search Backend",
            subtitle="Which provider executes searches selected by the model.",
            rows=[
                Row(label=label, description=description, value=value, current=value == current)
                for value, label, description in _web_search_mode_rows()
            ],
            hint="",
        )

    def _screen_cache_mode(self) -> Screen:
        current = _normalize_prompt_cache_mode(self.state.fields.get("prompt_cache_mode", "manual"))
        rows = [
            Row(
                label=value.capitalize(),
                description=_CACHE_MODE_DESCRIPTIONS[value],
                value=value,
                current=value == current,
            )
            for value in PROMPT_CACHE_MODES
        ]
        return Screen(
            stage="cache_mode",
            mode="list",
            title="Context & Cache",
            subtitle=_cache_mode_subtitle(self.state),
            rows=rows,
            hint="",
        )

    def _screen_cache_key(self) -> Screen:
        return Screen(
            stage="cache_key",
            mode="input",
            title="Context & Cache",
            subtitle="Manual key for providers that accept prompt_cache_key.",
            input_label='Manual cache key ("clear" to unset)',
            input_default=str(self.state.fields.get("prompt_cache_key", "")),
            hint="Enter next · Esc back",
        )

    def _screen_cache_retention(self) -> Screen:
        return Screen(
            stage="cache_retention",
            mode="input",
            title="Context & Cache",
            subtitle="Optional provider retention hint, for example 24h.",
            input_label='Manual cache retention ("clear" to unset)',
            input_default=str(self.state.fields.get("prompt_cache_retention", "")),
            hint="Enter next · Esc back",
        )

    def _screen_cache_anthropic_enabled(self) -> Screen:
        current = _normalize_bool_text(
            self.state.fields.get("anthropic_prompt_cache_enabled", "false"),
            label="Anthropic prompt cache",
        )
        rows = [
            Row(
                label="On",
                description="Enable Anthropic cache_control in manual mode.",
                value="true",
                current=current is True,
            ),
            Row(
                label="Off",
                description="Only auto mode enables Anthropic cache_control.",
                value="false",
                current=current is False,
            ),
        ]
        return Screen(
            stage="cache_anthropic_enabled",
            mode="list",
            title="Context & Cache",
            subtitle="Anthropic cache_control override.",
            rows=rows,
            hint="",
        )

    def _screen_cache_anthropic_ttl(self) -> Screen:
        current = _normalize_anthropic_prompt_cache_ttl(
            self.state.fields.get("anthropic_prompt_cache_ttl", "5m")
        )
        rows = [
            Row(
                label=value,
                description=_CACHE_TTL_DESCRIPTIONS[value],
                value=value,
                current=value == current,
            )
            for value in ANTHROPIC_PROMPT_CACHE_TTLS
        ]
        return Screen(
            stage="cache_anthropic_ttl",
            mode="list",
            title="Context & Cache",
            subtitle="Anthropic cache_control TTL.",
            rows=rows,
            hint="",
        )

    def _screen_cache_compaction_enabled(self) -> Screen:
        current = _normalize_bool_text(
            self.state.fields.get("cache_aware_compaction", "true"),
            label="Cache-aware compaction",
        )
        rows = [
            Row(
                label="On",
                description="Compact earlier when the next request is likely to miss provider cache.",
                value="true",
                current=current is True,
            ),
            Row(
                label="Off",
                description="Use only the normal compaction trigger ratio.",
                value="false",
                current=current is False,
            ),
        ]
        return Screen(
            stage="cache_compaction_enabled",
            mode="list",
            title="Context & Cache",
            subtitle="Cache-aware compaction trigger.",
            rows=rows,
            hint="",
        )

    def _screen_cache_compaction_min_trigger(self) -> Screen:
        return Screen(
            stage="cache_compaction_min_trigger",
            mode="input",
            title="Context & Cache",
            subtitle="Lowest trigger ratio cache-aware compaction may use.",
            input_label="Cache-aware min trigger ratio",
            input_default=str(self.state.fields.get("cache_aware_min_trigger_ratio", "")),
            hint="Enter save · Esc back",
        )

    def _screen_subagent_field(self) -> Screen:
        role, kind = self._sub_steps[self._sub_i]
        label = _role_label(role)
        if kind == "model":
            field_label = f'{label} model (blank keeps · "clear" inherits)'
            default = self.state.role_models.get(role, "")
        else:
            field_label = f'{label} temperature (blank keeps · "clear" inherits)'
            default = self.state.role_temperatures.get(role, "")
        subtitle = _role_description(role)
        if len(self._sub_steps) > 1:
            subtitle += f"  ·  step {self._sub_i + 1}/{len(self._sub_steps)}"
        if not _temperature_controls_available(self.state):
            subtitle += "  ·  temperature is managed by the AI subscription"
        return Screen(
            stage="subagent_field",
            mode="input",
            title="Subagent model overrides",
            subtitle=subtitle,
            input_label=field_label,
            input_default=default,
            hint="Enter next · Esc cancel section",
        )

    def _screen_forge_field(self) -> Screen:
        role = self._forge_role or FORGE_ROLE_ORDER[0]
        return Screen(
            stage="forge_field",
            mode="input",
            title="Forge model overrides",
            subtitle=_role_description(role),
            input_label=f'{_role_label(role)} model (blank keeps · "clear" inherits)',
            input_default=self.state.forge_role_models.get(role, ""),
            hint="Enter save · Esc cancel",
        )

    def _screen_api_key(self) -> Screen:
        subtitle = (
            f"Stored: {self.state.masked_api_key} · {_pretty_key_source(self.state.api_key_source)}"
        )
        staged_profile = self.state.staged_api_key_target_profile()
        if staged_profile and staged_profile != (self.state.active_profile or None):
            subtitle += f"; unsaved key remains bound to profile {staged_profile}"
        clear_profile = self.state.clear_stored_key_profile
        if (
            self.state.clear_stored_key_confirmed
            and clear_profile
            and clear_profile != (self.state.active_profile or None)
        ):
            subtitle += f"; stored-key removal remains bound to profile {clear_profile}"
        if self.state.execution_backend == "delegated":
            subtitle += " · API key is preserved but inactive while using an AI subscription."
        return Screen(
            stage="api_key",
            mode="input",
            title="API key",
            subtitle=subtitle,
            input_label='New API key (blank keeps current · "clear" to remove)',
            input_password=True,
            hint="Enter save · Esc back",
        )

    def _screen_api_key_clear_confirm(self) -> Screen:
        return Screen(
            stage="api_key_clear_confirm",
            mode="confirm",
            title="API key",
            lines=[("Clear the stored API key?", "warn")],
            hint="Y clear · N keep · Esc back",
            confirm_default=False,
        )

    def _screen_provider(self) -> Screen:
        active = self.state.active_profile or "(none)"
        rows = [
            Row(label=label, description=desc, value=value)
            for value, label, desc in _PROVIDER_ACTION_ROWS
        ]
        subtitle = f"Active profile: {active}"
        if self.state.execution_backend == "delegated":
            subtitle += " · API-key provider settings are preserved but inactive."
        return Screen(
            stage="provider",
            mode="list",
            title="Provider profile",
            subtitle=subtitle,
            rows=rows,
            hint="",
        )

    def _screen_provider_switch(self) -> Screen:
        descriptions = _profile_row_descriptions(
            self.state.profiles, active=self.state.active_profile
        )
        rows = [
            Row(
                label=name,
                description=descriptions.get(name, ""),
                value=name,
                current=name == self.state.active_profile,
            )
            for name in sorted(self.state.profiles)
        ]
        return Screen(
            stage="provider_switch",
            mode="list",
            title="Switch profile",
            subtitle=f"Active: {self.state.active_profile or 'none'}",
            rows=rows,
            hint="",
        )

    def _screen_provider_add_preset(self) -> Screen:
        rows = [
            Row(
                label=_short_preset_label(preset),
                description=_short_preset_description(preset),
                value=preset.key,
            )
            for preset in _ordered_profile_presets_for_setup()
        ]
        rows.append(
            Row(
                label="Advanced / local / compatibility providers",
                description="Ollama, LM Studio, vLLM, custom URLs",
                value=_ADVANCED_PROVIDER_PRESETS_VALUE,
            )
        )
        return Screen(
            stage="provider_add_preset",
            mode="list",
            title="Add provider preset",
            subtitle="Pick a provider preset.",
            rows=rows,
            hint="",
        )

    def _screen_provider_add_preset_advanced(self) -> Screen:
        rows = [
            Row(
                label=_short_preset_label(preset),
                description=_short_preset_description(preset),
                value=preset.key,
            )
            for preset in _advanced_profile_presets_for_setup()
        ]
        return Screen(
            stage="provider_add_preset_advanced",
            mode="list",
            title="Advanced provider preset",
            subtitle="Compatibility, local, custom, legacy, and account sign-in providers.",
            rows=rows,
            hint="",
        )

    def _screen_provider_preset_name(self) -> Screen:
        return Screen(
            stage="provider_preset_name",
            mode="input",
            title="Add provider preset",
            subtitle="Name this profile (lowercase, no spaces).",
            input_label="Profile name",
            input_default=getattr(self._pending_preset, "key", "custom"),
            hint="Enter next · Esc back",
        )

    def _screen_provider_preset_url(self) -> Screen:
        return Screen(
            stage="provider_preset_url",
            mode="input",
            title="Add provider preset",
            subtitle="This preset needs an OpenAI-compatible base URL.",
            input_label="Base URL",
            input_default="",
            hint="Enter next · Esc back",
        )

    def _screen_provider_custom_name(self) -> Screen:
        return Screen(
            stage="provider_custom_name",
            mode="input",
            title="Add custom profile",
            subtitle="Name this profile (lowercase, no spaces).",
            input_label="Profile name",
            input_default=self._custom_name or "custom",
            hint="Enter next · Esc back",
        )

    def _screen_provider_custom_url(self) -> Screen:
        return Screen(
            stage="provider_custom_url",
            mode="input",
            title="Add custom profile",
            subtitle="The OpenAI-compatible base URL for this provider.",
            input_label="Base URL",
            input_default=self._custom_url,
            hint="Enter next · Esc back",
        )

    def _screen_provider_custom_headers(self) -> Screen:
        return Screen(
            stage="provider_custom_headers",
            mode="input",
            title="Add custom profile",
            subtitle="Extra request headers, if your endpoint needs them.",
            input_label="Extra headers (k=v, comma-separated)",
            input_default="",
            hint="Enter add · Esc back",
        )

    def _screen_provider_edit(self) -> Screen:
        field = _EDIT_FIELDS[self._edit_i]
        name = getattr(self._edit_profile, "name", "")
        return Screen(
            stage="provider_edit",
            mode="input",
            title=f"Edit profile: {name}",
            subtitle=f"Blank keeps the current value  ·  step {self._edit_i + 1}/{len(_EDIT_FIELDS)}",
            input_label=_EDIT_LABELS[field],
            input_default=self._edit_values.get(field, ""),
            hint="Enter next · Esc cancel",
        )

    def _screen_provider_remove(self) -> Screen:
        descriptions = _profile_row_descriptions(
            self.state.profiles, active=self.state.active_profile
        )
        rows = [
            Row(label=name, description=descriptions.get(name, ""), value=name)
            for name in sorted(self.state.profiles)
        ]
        return Screen(
            stage="provider_remove",
            mode="list",
            title="Remove profile",
            subtitle="Pick a profile to remove (applied on save).",
            rows=rows,
            hint="",
        )

    def _screen_provider_remove_confirm(self) -> Screen:
        return Screen(
            stage="provider_remove_confirm",
            mode="confirm",
            title="Remove profile",
            lines=[(f"Remove profile {self._pending_remove}?", "warn")],
            hint="Y remove · N keep · Esc back",
            confirm_default=False,
        )

    def _screen_saving(self) -> Screen:
        return Screen(
            stage="saving", mode="busy", title="Saving", busy_label="Saving configuration…"
        )

    def _screen_cancel_confirm(self) -> Screen:
        return Screen(
            stage="cancel_confirm",
            mode="confirm",
            title="Discard changes",
            lines=[("Discard pending changes and close?", "warn")],
            hint="Y discard · N keep editing",
            confirm_default=False,
        )

    def _screen_done(self) -> Screen:
        return Screen(stage="done", mode="done", title="")

    # ----------------------------------------------------------- navigation

    def move(self, delta: int) -> None:
        scr = self.screen()
        if scr.mode != "list" or not scr.rows:
            return
        # Step over header/spacer rows so the cursor only ever rests on items.
        n = len(scr.rows)
        i = self.index
        for _ in range(n):
            i = (i + delta) % n
            if getattr(scr.rows[i], "kind", "item") == "item":
                break
        self.index = i

    def choose_index(self, idx: int) -> None:
        scr = self.screen()
        if scr.mode != "list" or not scr.rows:
            return
        if not getattr(scr, "numbered", True):
            # Unnumbered lists (the grouped menu) advertise no digit shortcuts.
            return
        if 0 <= idx < len(scr.rows):
            self.index = idx
            self.choose(scr.rows[idx].value)

    def choose_current(self) -> None:
        scr = self.screen()
        if scr.mode != "list" or not scr.rows:
            return
        row = scr.rows[self.index]
        if getattr(row, "kind", "item") != "item":
            return
        self.choose(row.value)

    def choose(self, value: str) -> None:
        handler = {
            "menu": self._choose_menu,
            "execution_backend": self._choose_execution_backend,
            "execution_runtime": self._choose_execution_runtime,
            "subscription_account": self._choose_subscription_account,
            "advanced": self._choose_advanced,
            "subagent_roles": self._choose_subagent_roles,
            "forge_roles": self._choose_forge_roles,
            "workspace": self._choose_workspace,
            "workspace_action": self._choose_workspace_action,
            "sandbox": self._choose_sandbox,
            "personas": self._choose_personas,
            "model": self._choose_model,
            "model_thinking": self._choose_thinking,
            "web_search_policy": self._choose_web_search_policy,
            "web_search_mode": self._choose_web_search_mode,
            "cache_mode": self._choose_cache_mode,
            "cache_anthropic_enabled": self._choose_cache_anthropic_enabled,
            "cache_anthropic_ttl": self._choose_cache_anthropic_ttl,
            "cache_compaction_enabled": self._choose_cache_compaction_enabled,
            "provider": self._choose_provider_action,
            "provider_switch": self._choose_provider_switch,
            "provider_add_preset": self._choose_preset,
            "provider_add_preset_advanced": self._choose_preset,
            "provider_remove": self._choose_remove,
        }.get(self.stage)
        if handler is not None:
            handler(value)

    def submit_input(self, text: str) -> None:
        handler = getattr(self, f"_submit_{self.stage}", None)
        if handler is not None:
            handler(str(text))

    def confirm(self, yes: bool) -> None:
        if self.stage == "api_key_clear_confirm":
            if yes:
                self.state.mark_clear_stored_key_confirmed()
                self._goto("menu")
                self._set_status("Stored API key will be cleared on save.", "warn")
            else:
                self._goto("menu")
        elif self.stage == "provider_remove_confirm":
            if yes:
                staged_key_discarded = self.state.remove_profile_name(self._pending_remove)
                self._goto("provider")
                message = f"Profile {self._pending_remove} will be removed on save."
                if staged_key_discarded:
                    message += " Its unsaved API key was discarded."
                self._set_status(message, "warn")
            else:
                self._goto("provider")
        elif self.stage == "subscription_disconnect_confirm":
            if yes:
                self._goto("subscription_disconnecting")
            else:
                self._goto("subscription_account")
        elif self.stage == "cancel_confirm":
            if yes:
                self._finish(False)
            else:
                self._goto(self._resume_stage or "menu")

    def back(self) -> None:
        if self.stage == "menu":
            self.request_cancel()
            return
        if self.stage == "model" and self._subscription_save_pending:
            self._subscription_save_pending = False
            self._goto("subscription_account")
            return
        if self.stage == "subagent_field":
            self.state.role_models = dict(self._sub_model_snapshot)
            self.state.role_temperatures = dict(self._sub_temp_snapshot)
            self._goto("subagent_roles")
            return
        if self.stage == "forge_field":
            self.state.forge_role_models = dict(self._forge_snapshot)
            self._goto("forge_roles")
            return
        if self.stage in {"subagent_roles", "forge_roles"}:
            self._goto("advanced")
            return
        if self.stage == "cancel_confirm":
            self._goto(self._resume_stage or "menu")
            return
        if self.stage == "model_thinking" and (
            _active_subscription_profile(self.state) is not None
            or _active_alysis_profile(self.state) is not None
        ):
            self._goto("model", index=self._model_index())
            return
        prev = _PREV.get(self.stage)
        if prev is not None:
            self._goto(prev)

    def request_cancel(self) -> None:
        """Ctrl+C / the Discard row: confirm before discarding when dirty."""
        if self.stage in {
            "cancel_confirm",
            "done",
            "saving",
            "subscription_connecting",
            "subscription_disconnecting",
        }:
            return
        if self.state.dirty:
            # Answering "No" returns to wherever the user was (a sub-flow field, a
            # section list, or the menu) rather than always snapping back to the top.
            self._resume_stage = self.stage
            self._goto("cancel_confirm", keep_status=True)
        else:
            self._finish(False)

    # ------------------------------------------------------------- menu step

    def _choose_menu(self, value: str) -> None:
        # Any return to the top menu ends an in-progress add-provider chain.
        self._preset_setup_chain = False
        # Remember where the cursor was so Esc-ing back out of a section lands
        # on the row the user came from, not back at the top.
        self._menu_resume_index = self.index
        if value == _SAVE:
            self._goto("saving")
            return
        if value == _CANCEL:
            self.request_cancel()
            return
        if value == "execution":
            self._goto("execution_backend", index=self._execution_backend_index())
        elif value == "profile":
            self._goto("provider")
        elif value == "api_key":
            if _active_alysis_profile(self.state) is not None:
                self._choose_execution_runtime(_ALYSIS_CONNECTION_ID)
            else:
                self._goto("api_key")
        elif value == "default":
            self.open_default_model()
        elif value == "web_search":
            self._goto("web_search_policy", index=self._web_search_policy_index())
        elif value == "cache":
            self._goto("cache_mode", index=self._cache_mode_index())
        elif value == "workspace":
            self._goto("workspace")
        elif value == "advanced":
            self._goto("advanced")
        elif value == "sandbox":
            self._goto("sandbox", index=self._sandbox_index())
        elif value == "personas":
            self._goto("personas", index=self._persona_index())

    def _execution_backend_index(self) -> int:
        return int(
            self.state.execution_backend == "delegated"
            or _active_alysis_profile(self.state) is not None
        )

    def _choose_execution_backend(self, value: str) -> None:
        if value == "native":
            if _active_alysis_profile(self.state) is not None:
                self._goto("provider")
                self._set_status("Choose or add an API-key provider profile.", "dim")
                return
            self.state.set_execution_backend("native")
            self._goto("menu")
            self._set_status("API-key model access selected. Save to apply.", "ok")
            return
        if value != "delegated":
            self._set_status(f"Unknown model access method: {value}", "err")
            return
        rows = _runtime_setup_rows()
        runtime_ids = [_ALYSIS_CONNECTION_ID] + [
            runtime_id for runtime_id, _label, _description in rows
        ]
        index = (
            runtime_ids.index(self.state.execution_runtime)
            if self.state.execution_runtime in runtime_ids
            else 0
        )
        self._goto("execution_runtime", index=index)

    def _choose_execution_runtime(self, value: str) -> None:
        if value == _ALYSIS_CONNECTION_ID:
            # Not a delegated runtime: the account screen drives the device
            # login, which activates the native alysis profile itself.
            self._subscription_provider_id = _ALYSIS_CONNECTION_ID
            self._subscription_account_status = None
            self._goto("subscription_account")
            return
        known = {runtime_id for runtime_id, _label, _description in _runtime_setup_rows()}
        if value not in known:
            self._set_status(f"Unknown AI subscription connection: {value}", "err")
            return
        self.state.set_execution_backend("delegated", runtime=value)
        self._subscription_provider_id = value
        self._subscription_account_status = None
        self._goto("subscription_account")

    def _choose_subscription_account(self, value: str) -> None:
        if value == _SAVE:
            status = (
                self._subscription_account_status or self._refresh_subscription_account_status()
            )
            if not status.connected:
                return
            if self.state.subscription_selection_required and (
                not self.state.fields.get("model", "").strip()
                or not self.state.thinking_label_explicitly_set
            ):
                self._subscription_save_pending = True
                self.open_default_model()
                self._set_status("Choose a model and reasoning effort to save and start chatting.")
            else:
                self._goto("saving")
            return
        if value == "chat":
            # Alysis Code connect already persisted everything (self.saved is
            # set), so finishing here closes the overlay, reloads the live
            # session with the new key, and drops back into chat.
            self._finish(True)
            return
        if value == "back":
            self._goto("menu")
            return
        if value == "connect":
            status = (
                self._subscription_account_status or self._refresh_subscription_account_status()
            )
            self._subscription_switch_account = bool(status.connected)
            self._goto("subscription_connecting")
            return
        if value == "disconnect":
            self._goto("subscription_disconnect_confirm")
            return
        self._set_status(f"Unknown subscription account action: {value}", "err")

    def _choose_advanced(self, value: str) -> None:
        if value == "back":
            self._goto("menu")
        elif value == "subagents":
            self._goto("subagent_roles")
        elif value == "forge":
            self._goto("forge_roles")
        elif value == "request_timeout":
            self._goto("request_timeout")

    def _choose_subagent_roles(self, value: str) -> None:
        if value == "back":
            self._goto("advanced")
            return
        # Edit exactly the picked role (model, plus temperature where the
        # provider exposes it) — never a forced walk through every role.
        self._sub_model_snapshot = dict(self.state.role_models)
        self._sub_temp_snapshot = dict(self.state.role_temperatures)
        steps: list[tuple[str, str]] = [(value, "model")]
        if _temperature_controls_available(self.state) and value in _ROLE_TEMPERATURE_FIELDS:
            steps.append((value, "temp"))
        self._sub_steps = steps
        self._sub_i = 0
        self._goto("subagent_field")

    def _choose_forge_roles(self, value: str) -> None:
        if value == "back":
            self._goto("advanced")
            return
        self._forge_snapshot = dict(self.state.forge_role_models)
        self._forge_role = value
        self._goto("forge_field")

    # ----------------------------------------------------- project / workspace

    def _resolve_workspace(self, raw: str) -> str | None:
        text = str(raw).strip()
        if not text:
            self._set_status("A folder path is required.", "err")
            return None
        try:
            selected = Path(text).expanduser().resolve()
            binding = resolve_workspace_binding(
                selected, allow_broad_workspace=True, source="config_menu"
            )
        except WorkspaceBindingError as exc:
            self._set_status(str(exc), "err")
            return None
        except Exception as exc:  # noqa: BLE001 - bad path / permission error
            self._set_status(f"Invalid path: {exc}", "err")
            return None
        return os.fspath(binding.requested_path)

    def _choose_workspace(self, value: str) -> None:
        if value == "back":
            self._goto("menu")
            return
        if value == "__type_path__":
            self._goto("workspace_path")
            return
        resolved = self._resolve_workspace(value)
        if resolved is None:
            return
        self._pending_workspace = resolved
        self._goto("workspace_action")

    def _submit_workspace_path(self, text: str) -> None:
        resolved = self._resolve_workspace(text)
        if resolved is None:
            return
        self._pending_workspace = resolved
        self._goto("workspace_action")

    def _choose_workspace_action(self, value: str) -> None:
        if value == "back" or not self._pending_workspace:
            self._goto("workspace")
            return
        if value == "set_default":
            self.state.set_default_workspace_path(self._pending_workspace)
            self._goto("menu")
            self._set_status("Default project set. Save to apply.", "ok")
            return
        if value == "switch":
            # Persist the new default + all pending edits, then signal the host to
            # relaunch the chat bound to this folder (the live root cannot change in
            # place). The full save runs on the busy stage; on success
            # apply_save_outcome promotes _switch_target -> switch_workspace.
            self.state.set_default_workspace_path(self._pending_workspace)
            self._switch_target = self._pending_workspace
            self._goto("switching")

    def _sandbox_index(self) -> int:
        order = ("strict", "warn", "off")
        current = normalize_sandbox_mode(self.state.fields.get("sandbox_mode", "strict"))
        return order.index(current) if current in order else 0

    # ----------------------------------------------------------- sandbox

    def _choose_sandbox(self, value: str) -> None:
        normalized = normalize_sandbox_mode(value)
        self.state.fields["sandbox_mode"] = normalized
        self._goto("menu")
        if normalized == "off":
            self._set_status("Sandbox off — commands run on the host shell. Save to apply.", "warn")
        else:
            self._set_status(f"Sandbox mode: {normalized}. Save to apply.", "ok")

    # ----------------------------------------------------------- default model

    def _model_index(self) -> int:
        rows = _default_model_rows(self.state)
        order = [value for value, _label, _desc in rows]
        current = str(self.state.fields.get("model", "") or "").strip()
        if current in order:
            return order.index(current)
        if current and _CUSTOM_MODEL_VALUE in order:
            return order.index(_CUSTOM_MODEL_VALUE)
        return 0

    def _choose_model(self, value: str) -> None:
        if value == _CUSTOM_MODEL_VALUE:
            self._goto("custom_model")
            return
        self.state.set_field("model", value)
        if self._finish_preset_chain_if_active(model=value):
            return
        if (
            _active_subscription_profile(self.state) is not None
            or _active_alysis_profile(self.state) is not None
        ):
            labels = _thinking_labels_for_state(self.state, model=value)
            if self.state.thinking_label not in labels:
                self.state.set_thinking_label("auto")
            self._goto("model_thinking", index=self._thinking_index())
            return
        self._goto("model_base_url")

    def _submit_custom_model(self, text: str) -> None:
        model = text.strip() or str(self.state.fields.get("model", "")).strip()
        if not model:
            self._set_status("Model is required.", "err")
            return
        self.state.set_field("model", model)
        if self._finish_preset_chain_if_active(model=model):
            return
        if (
            _active_subscription_profile(self.state) is not None
            or _active_alysis_profile(self.state) is not None
        ):
            self._goto("model_thinking", index=self._thinking_index())
        else:
            self._goto("model_base_url")

    def _submit_model_base_url(self, text: str) -> None:
        value = text.strip() or str(self.state.fields.get("base_url", ""))
        try:
            validate_base_url(value, key="Base URL", allow_empty=True)
        except ConfigError as exc:
            self._set_status(str(exc), "err")
            return
        self.state.set_field("base_url", value)
        self._goto("model_thinking", index=self._thinking_index())

    def _thinking_index(self) -> int:
        current = self.state.thinking_label
        labels = _thinking_labels_for_state(self.state)
        return labels.index(current) if current in labels else 0

    def _choose_thinking(self, value: str) -> None:
        self.state.set_thinking_label(value)
        if self._subscription_save_pending:
            self._subscription_save_pending = False
            self._goto("saving")
            return
        if self._preset_setup_chain:
            self._complete_preset_setup_chain(
                model=str(self.state.fields.get("model", "") or "").strip()
            )
            return
        self._goto("menu")
        self._set_status("Default model updated. Save to apply.", "ok")

    def _submit_request_timeout(self, text: str) -> None:
        value = text.strip() or str(self.state.fields.get("llm_timeout_s", ""))
        number = _finite_float(value, fallback=None)
        if number is None or number <= 0:
            self._set_status("Request timeout must be a positive number.", "err")
            return
        self.state.set_field("llm_timeout_s", _format_number(number))
        self._goto("advanced")
        self._set_status("Request timeout updated. Save to apply.", "ok")

    # --------------------------------------------------------------- web search

    def _choose_web_search_policy(self, value: str) -> None:
        self.state.set_field("web_search_policy", normalize_web_search_policy(value))
        self._goto("web_search_mode", index=self._web_search_mode_index())

    def _choose_web_search_mode(self, value: str) -> None:
        self.state.set_field("web_search_mode", _normalize_web_search_mode(value))
        self._goto("menu")
        self._set_status("Web search settings updated. Save to apply.", "ok")

    def _web_search_policy_index(self) -> int:
        current = normalize_web_search_policy(self.state.fields.get("web_search_policy", "auto"))
        order = [value for value, _label, _description in _web_search_policy_rows()]
        return order.index(current) if current in order else 0

    def _web_search_mode_index(self) -> int:
        current = _normalize_web_search_mode(self.state.fields.get("web_search_mode", "auto"))
        order = [value for value, _label, _description in _web_search_mode_rows()]
        return order.index(current) if current in order else 0

    # ----------------------------------------------------------- context & cache

    def _choose_cache_mode(self, value: str) -> None:
        self.state.set_field("prompt_cache_mode", _normalize_prompt_cache_mode(value))
        self._goto("cache_key")

    def _submit_cache_key(self, text: str) -> None:
        value = self._optional_text_value(text, self.state.fields.get("prompt_cache_key", ""))
        self.state.set_field("prompt_cache_key", value)
        self._goto("cache_retention")

    def _submit_cache_retention(self, text: str) -> None:
        value = self._optional_text_value(
            text,
            self.state.fields.get("prompt_cache_retention", ""),
        )
        self.state.set_field("prompt_cache_retention", value)
        self._goto(
            "cache_anthropic_enabled",
            index=self._cache_anthropic_enabled_index(),
        )

    def _choose_cache_anthropic_enabled(self, value: str) -> None:
        enabled = _normalize_bool_text(value, label="Anthropic prompt cache")
        self.state.set_field("anthropic_prompt_cache_enabled", "true" if enabled else "false")
        self._goto("cache_anthropic_ttl", index=self._cache_anthropic_ttl_index())

    def _choose_cache_anthropic_ttl(self, value: str) -> None:
        self.state.set_field(
            "anthropic_prompt_cache_ttl",
            _normalize_anthropic_prompt_cache_ttl(value),
        )
        self._goto(
            "cache_compaction_enabled",
            index=self._cache_compaction_enabled_index(),
        )

    def _choose_cache_compaction_enabled(self, value: str) -> None:
        enabled = _normalize_bool_text(value, label="Cache-aware compaction")
        self.state.set_field("cache_aware_compaction", "true" if enabled else "false")
        self._goto("cache_compaction_min_trigger")

    def _submit_cache_compaction_min_trigger(self, text: str) -> None:
        value = text.strip() or str(self.state.fields.get("cache_aware_min_trigger_ratio", ""))
        try:
            normalized = _normalize_cache_aware_min_trigger_ratio(value)
        except ValueError as exc:
            self._set_status(str(exc), "err")
            return
        self.state.set_field("cache_aware_min_trigger_ratio", normalized)
        self._goto("menu")
        self._set_status("Context/cache settings updated. Save to apply.", "ok")

    def _cache_mode_index(self) -> int:
        current = _normalize_prompt_cache_mode(self.state.fields.get("prompt_cache_mode", "manual"))
        return PROMPT_CACHE_MODES.index(current) if current in PROMPT_CACHE_MODES else 0

    def _cache_anthropic_enabled_index(self) -> int:
        current = _normalize_bool_text(
            self.state.fields.get("anthropic_prompt_cache_enabled", "false"),
            label="Anthropic prompt cache",
        )
        return 0 if current else 1

    def _cache_anthropic_ttl_index(self) -> int:
        current = _normalize_anthropic_prompt_cache_ttl(
            self.state.fields.get("anthropic_prompt_cache_ttl", "5m")
        )
        return (
            ANTHROPIC_PROMPT_CACHE_TTLS.index(current)
            if current in ANTHROPIC_PROMPT_CACHE_TTLS
            else 0
        )

    def _cache_compaction_enabled_index(self) -> int:
        current = _normalize_bool_text(
            self.state.fields.get("cache_aware_compaction", "true"),
            label="Cache-aware compaction",
        )
        return 0 if current else 1

    @staticmethod
    def _optional_text_value(text: str, current: str) -> str:
        value = str(text or "").strip()
        if not value:
            return str(current or "").strip()
        if value.casefold() == "clear":
            return ""
        return value

    def _finish_preset_chain_if_active(self, *, model: str) -> bool:
        """End the add-provider chain after the model is chosen.

        Selecting a model normally advances to base URL / thinking. When
        we arrived here via "Add provider preset" we skip fields already carried by
        the profile. NVIDIA remains on the chain for one more step because reasoning
        controls differ between models hosted behind the same endpoint.
        """
        if not self._preset_setup_chain:
            return False
        preset = _active_preset(self.state)
        if preset is not None and preset.key == "nvidia":
            labels = _thinking_labels_for_state(self.state, model=model)
            if self.state.thinking_label not in labels:
                self.state.set_thinking_label("auto")
            self._goto("model_thinking", index=self._thinking_index())
            return True
        self._complete_preset_setup_chain(model=model)
        return True

    def _complete_preset_setup_chain(self, *, model: str) -> None:
        """Finish an active add-provider chain and return to its provider screen."""

        self._preset_setup_chain = False
        self._goto("provider")
        diag = self._provider_diag_first()
        if diag:
            self._set_status(f"Provider ready · model {model}. Provider diagnostic: {diag}", "warn")
        else:
            self._set_status(f"Provider ready · default model {model}. Save to apply.", "ok")

    # ----------------------------------------------------- subagent overrides

    def _submit_subagent_field(self, text: str) -> None:
        role, kind = self._sub_steps[self._sub_i]
        value = text.strip()
        if kind == "model":
            if value.casefold() == "clear":
                self.state.set_role_model(role, "")
            elif value:
                self.state.set_role_model(role, value)
        else:
            if value.casefold() == "clear":
                self.state.set_role_temperature(role, "")
            elif value:
                number = _finite_float(value, fallback=None)
                if number is None or number < 0:
                    self._set_status("Temperature must be a non-negative number.", "err")
                    return
                self.state.set_role_temperature(role, _format_number(number))
        self._advance_subagent()

    def _advance_subagent(self) -> None:
        role, _kind = self._sub_steps[self._sub_i]
        self._sub_i += 1
        if self._sub_i >= len(self._sub_steps):
            self._goto("subagent_roles")
            self._set_status(f"{_role_label(role)} override updated. Save to apply.", "ok")
        else:
            self.status = ""
            self.status_tone = "dim"

    # -------------------------------------------------------- forge overrides

    def _submit_forge_field(self, text: str) -> None:
        role = self._forge_role or FORGE_ROLE_ORDER[0]
        value = text.strip()
        if value.casefold() == "clear":
            self.state.set_forge_role_model(role, "")
        elif value:
            self.state.set_forge_role_model(role, value)
        self._goto("forge_roles")
        self._set_status(f"{_role_label(role)} override updated. Save to apply.", "ok")

    # ----------------------------------------------------------- api key

    def _submit_api_key(self, text: str) -> None:
        value = text.strip()
        if not value:
            self._after_api_key_step(skipped=True)
            return
        if value.casefold() == "clear":
            self._goto("api_key_clear_confirm")
            return
        self.state.set_field("new_api_key", value)
        self._after_api_key_step(skipped=False)

    def _after_api_key_step(self, *, skipped: bool) -> None:
        if self._preset_setup_chain:
            self.open_default_model()
            if skipped:
                self._set_status(
                    "No key set yet. Pick the default model (add the key later).", "warn"
                )
            else:
                self._set_status("API key set. Now pick the default model.", "ok")
            return
        self._goto("menu")
        if not skipped:
            self._set_status("API key set. Save to apply.", "ok")

    # -------------------------------------------------------- provider profile

    def _choose_provider_action(self, value: str) -> None:
        if value == "back":
            self._goto("menu")
        elif value == "switch":
            if not self.state.profiles:
                self._set_status("No profiles configured yet.", "warn")
                return
            self._goto("provider_switch", index=self._provider_switch_index())
        elif value == "add_preset":
            self._goto("provider_add_preset")
        elif value == "add_custom":
            self._goto("provider_custom_name")
        elif value == "edit":
            self._enter_provider_edit()
        elif value == "remove":
            if not self.state.profiles:
                self._set_status("No profiles to remove.", "warn")
                return
            self._goto("provider_remove")

    def _provider_switch_index(self) -> int:
        names = sorted(self.state.profiles)
        active = self.state.active_profile
        return names.index(active) if active in names else 0

    def _choose_provider_switch(self, value: str) -> None:
        self.state.set_active_profile_name(value)
        self._goto("provider")
        diag = self._provider_diag_first()
        notices = [f"Active profile: {value}."]
        staged_profile = self.state.staged_api_key_target_profile()
        if staged_profile and staged_profile != value:
            notices.append(f"The unsaved API key remains bound to {staged_profile}.")
        clear_profile = self.state.clear_stored_key_profile
        if self.state.clear_stored_key_confirmed and clear_profile and clear_profile != value:
            notices.append(f"Stored-key removal remains bound to {clear_profile}.")
        if diag:
            notices.append(f"Provider diagnostic: {diag}")
        self._set_status(" ".join(notices), "warn" if len(notices) > 1 else "ok")

    def _choose_preset(self, value: str) -> None:
        if value == _ADVANCED_PROVIDER_PRESETS_VALUE:
            self._goto("provider_add_preset_advanced")
            return
        self._pending_preset = next((p for p in PROFILE_PRESETS if p.key == value), None)
        if self._pending_preset is None:
            self._set_status("Unknown preset.", "err")
            return
        self._goto("provider_preset_name")

    def _submit_provider_preset_name(self, text: str) -> None:
        name = text.strip() or getattr(self._pending_preset, "key", "custom")
        profile = make_profile_from_preset(self._pending_preset, name=name)
        if not profile.base_url:
            self._pending_preset_profile = profile
            self._goto("provider_preset_url")
            return
        self._finalize_preset_profile(profile)

    def _submit_provider_preset_url(self, text: str) -> None:
        url = text.strip()
        if not url:
            self._set_status("Base URL is required.", "err")
            return
        base = self._pending_preset_profile
        profile = ProfileSpec(
            name=base.name,
            protocol=base.protocol,
            base_url=url,
            api_key_env=base.api_key_env,
            extra_headers=base.extra_headers,
            default_model=base.default_model,
            reasoning_effort=base.reasoning_effort,
            reasoning_trace_adapter=base.reasoning_trace_adapter,
            web_search_adapter=base.web_search_adapter,
            web_search_model=base.web_search_model,
            notes=base.notes,
        )
        self._finalize_preset_profile(profile)

    def _finalize_preset_profile(self, profile: ProfileSpec) -> None:
        try:
            self.state.add_profile_spec(profile)
        except ConfigError as exc:
            self._goto("provider_preset_name")
            self._set_status(str(exc), "err")
            return
        warning = (getattr(self._pending_preset, "setup_warning", "") or "").strip()
        # Guided chain: provider preset → API key → default model, so adding a
        # provider lands fully configured instead of dropping back to the menu.
        self._preset_setup_chain = True
        self._goto("api_key")
        if warning:
            self._set_status(
                f"Profile {profile.name} added. Next: enter the API key (Esc to skip). {warning}",
                "warn",
            )
        else:
            self._set_status(
                f"Profile {profile.name} added. Next: enter the API key (Esc to skip).",
                "ok",
            )

    def _submit_provider_custom_name(self, text: str) -> None:
        self._custom_name = (text.strip() or "custom").lower()
        self._goto("provider_custom_url")

    def _submit_provider_custom_url(self, text: str) -> None:
        url = text.strip()
        if not url:
            self._set_status("Base URL is required.", "err")
            return
        self._custom_url = url
        self._goto("provider_custom_headers")

    def _submit_provider_custom_headers(self, text: str) -> None:
        try:
            headers = _parse_header_text(text)
        except ConfigError as exc:
            self._set_status(str(exc), "err")
            return
        profile = ProfileSpec(
            name=self._custom_name,
            protocol="openai_compat",
            base_url=self._custom_url,
            extra_headers=headers,
            web_search_adapter="auto",
            web_search_model="",
            notes="Custom OpenAI-compatible endpoint.",
        )
        try:
            self.state.add_profile_spec(profile)
        except ConfigError as exc:
            self._goto("provider_custom_name")
            self._set_status(str(exc), "err")
            return
        self._goto("provider")
        diag = self._provider_diag_first()
        if diag:
            self._set_status(f"Profile {profile.name} added. Provider diagnostic: {diag}", "warn")
        else:
            self._set_status(f"Profile {profile.name} added.", "ok")

    def _enter_provider_edit(self) -> None:
        if not self.state.active_profile or self.state.active_profile not in self.state.profiles:
            self._set_status("No active profile. Switch to one or add a new profile first.", "warn")
            return
        profile = ProfileSpec.from_dict(
            self.state.active_profile, self.state.profiles[self.state.active_profile]
        )
        if profile.auth_provider or _active_alysis_profile(self.state) is not None:
            self._set_status(
                "This subscription connection is provider-managed. Use Default model for "
                "model and reasoning choices.",
                "warn",
            )
            return
        self._edit_profile = profile
        self._edit_values = {
            "base_url": profile.base_url,
            "api_key_env": profile.api_key_env or "",
            "default_model": profile.default_model,
            "reasoning_trace_adapter": profile.reasoning_trace_adapter,
            "web_search_adapter": profile.web_search_adapter,
            "web_search_model": profile.web_search_model,
            "extra_headers": ", ".join(f"{k}={v}" for k, v in profile.extra_headers.items()),
            "notes": profile.notes,
        }
        self._edit_i = 0
        self._edit_secret_candidate = None
        self._goto("provider_edit")

    def _submit_provider_edit(self, text: str) -> None:
        field = _EDIT_FIELDS[self._edit_i]
        value = text.strip()
        if not value:
            value = self._edit_values.get(field, "")
        # Accidental-secret guard (mirrors config_menu._resolve_non_secret_value).
        if value and value != _SECRET_FORCE_TOKEN and _looks_like_secret(value):
            self._edit_secret_candidate = value
            self._set_status(
                "That looks like an API key — paste keys in the API Key section. "
                "Re-enter, or type 'force' to store it anyway.",
                "warn",
            )
            return
        if value == _SECRET_FORCE_TOKEN and self._edit_secret_candidate is not None:
            value = self._edit_secret_candidate
        self._edit_secret_candidate = None
        if field == "web_search_adapter":
            try:
                value = normalize_web_search_adapter(value or "auto")
            except (ConfigError, ValueError) as exc:
                self._set_status(str(exc), "err")
                return
        if field == "reasoning_trace_adapter":
            try:
                profile = ProfileSpec.from_dict(
                    self.state.active_profile,
                    self.state.profiles[self.state.active_profile],
                )
                value = validate_reasoning_trace_adapter_for_protocol(
                    protocol=profile.protocol,
                    adapter=value or "auto",
                )
            except (ConfigError, ValueError) as exc:
                self._set_status(str(exc), "err")
                return
        if field == "extra_headers":
            try:
                _parse_header_text(value)
            except ConfigError as exc:
                self._set_status(str(exc), "err")
                return
        self._edit_values[field] = value
        self._edit_i += 1
        if self._edit_i >= len(_EDIT_FIELDS):
            self._finalize_provider_edit()
        else:
            self.status = ""
            self.status_tone = "dim"

    def _finalize_provider_edit(self) -> None:
        values = self._edit_values
        self.state.update_active_profile_spec(
            base_url=values["base_url"],
            api_key_env=values["api_key_env"] or None,
            default_model=values["default_model"],
            reasoning_trace_adapter=values["reasoning_trace_adapter"] or "auto",
            web_search_adapter=values["web_search_adapter"] or "auto",
            web_search_model=values["web_search_model"],
            extra_headers=_parse_header_text(values["extra_headers"]),
            notes=values["notes"],
        )
        self._goto("provider")
        diag = self._provider_diag_first()
        if diag:
            self._set_status(f"Profile updated. Provider diagnostic: {diag}", "warn")
        else:
            self._set_status("Profile updated. Save to apply.", "ok")

    def _choose_remove(self, value: str) -> None:
        self._pending_remove = value
        self._goto("provider_remove_confirm")

    # ----------------------------------------------------------- busy / save

    def run_busy(self) -> None:
        # Synchronous driver used by tests. The overlay splits these two calls
        # across its worker and UI threads.
        self.perform_busy()
        self.apply_busy_outcome()

    def perform_busy(self) -> None:
        if self.stage in {"subscription_connecting", "subscription_disconnecting"}:
            self._perform_subscription_account_action()
            return
        self.perform_save()

    def apply_busy_outcome(self) -> None:
        if self.stage in {"subscription_connecting", "subscription_disconnecting"}:
            self._apply_subscription_account_outcome()
            return
        self.apply_save_outcome()

    def _perform_subscription_account_action(self) -> None:
        provider_id = self._subscription_provider_id or self.state.execution_runtime
        if provider_id == _ALYSIS_CONNECTION_ID:
            self._perform_alysis_account_action()
            return
        try:
            adapter = create_provider_auth(provider_id)
            if self.stage == "subscription_connecting":
                if self._subscription_switch_account:
                    adapter.logout()
                status = adapter.login(method="browser", output_write=lambda _message: None)
                if not status.connected:
                    raise ProviderAuthError(status.detail or "Provider sign-in did not complete.")
                self._subscription_action_outcome = ("connected", status, "")
            else:
                status = adapter.logout()
                if status.connected:
                    raise ProviderAuthError(status.detail or "Provider credentials remain active.")
                self._subscription_action_outcome = ("disconnected", status, "")
        except (ProviderAuthError, ValueError) as exc:
            self._subscription_action_outcome = ("error", None, str(exc))

    def _perform_alysis_account_action(self) -> None:
        """Connect/disconnect the Alysis Code account (device-flow login).

        Runs on the busy worker like the runtime adapters. `login()` opens the
        browser, waits for approval, and itself activates + persists the
        `alysis` profile; the outcome applier then syncs the TUI state so a
        later Save doesn't revert what login already wrote.
        """
        from ... import account_login

        try:
            if self.stage == "subscription_connecting":
                if self._subscription_switch_account:
                    account_login.logout(self.cfg)
                result = account_login.login(
                    self.cfg, timeout_s=300, output_write=lambda _message: None
                )
                self._alysis_login_result = result
                status = ProviderAccountStatus(
                    connected=True,
                    verified=True,
                    account_label=result.email or "Alysis Code account",
                    detail=f"Hosted model ready: {result.model}",
                )
                self._subscription_action_outcome = ("connected", status, "")
            else:
                account_login.logout(self.cfg)
                status = ProviderAccountStatus(
                    connected=False,
                    verified=False,
                    detail="Key revoked and removed from this machine.",
                )
                self._subscription_action_outcome = ("disconnected", status, "")
        except (account_login.AlysisLoginError, ConfigError) as exc:
            self._subscription_action_outcome = ("error", None, str(exc))

    def _apply_subscription_account_outcome(self) -> None:
        outcome = self._subscription_action_outcome
        self._subscription_action_outcome = None
        self._subscription_switch_account = False
        provider_id = self._subscription_provider_id or self.state.execution_runtime
        if provider_id == _ALYSIS_CONNECTION_ID:
            self._apply_alysis_account_outcome(outcome)
            return
        if outcome is None:
            self._goto("subscription_account")
            self._set_status("Subscription account action did not complete.", "err")
            return
        kind, status, message = outcome
        if kind == "error" or status is None:
            self._subscription_account_status = None
            self._goto("subscription_account")
            self._set_status(message or "Subscription account action failed.", "err")
            return
        self._subscription_account_status = status
        self.state._subscription_models_cache = ()
        self.state._subscription_models_loaded = False
        self._goto("subscription_account")
        if kind == "connected":
            self._set_status(
                "Subscription connected. Press s or choose Save & start chatting.",
                "ok",
            )
        else:
            detail = str(status.detail or "Disconnected locally.").strip()
            self._set_status(f"{detail} The subscription profile remains selected.", "warn")

    def _apply_alysis_account_outcome(
        self, outcome: tuple[str, ProviderAccountStatus | None, str] | None
    ) -> None:
        if outcome is None:
            self._goto("subscription_account")
            self._set_status("Alysis Code account action did not complete.", "err")
            return
        kind, status, message = outcome
        if kind == "error" or status is None:
            self._subscription_account_status = None
            self._goto("subscription_account")
            self._set_status(message or "Alysis Code account action failed.", "err")
            return
        self._subscription_account_status = status
        # Either action changed the on-disk credentials, so the live chat
        # session must reload when config closes (whether via Save or Esc) —
        # otherwise it keeps using the old (now revoked) key in memory.
        self.saved = True
        self.changes_count = max(int(getattr(self, "changes_count", 0) or 0), 1)
        if kind == "connected":
            # login() already activated + saved the alysis profile in cfg;
            # mirror it into the TUI state so a later Save is consistent.
            self._alysis_login_result = None
            from ...profiles import get_profile

            profile = get_profile(self.cfg, _ALYSIS_CONNECTION_ID)
            if profile is not None:
                self.state.profiles[_ALYSIS_CONNECTION_ID] = profile.to_dict()
                self.state.set_active_profile_name(_ALYSIS_CONNECTION_ID)
            self.state.set_execution_backend("native")
            self._goto("subscription_account")
            self._set_status(
                "Alysis Code subscription connected. Esc to exit config.",
                "ok",
            )
        else:
            self.state.refresh_api_key_status()
            self._goto("subscription_account")
            self._set_status(
                "Logged out. Hosted requests will be refused until you sign in again.",
                "warn",
            )

    def _run_saving(self) -> None:
        self.perform_save()
        self.apply_save_outcome()

    def _run_switching(self) -> None:
        # "Switch now" persists everything (incl. the new default workspace) exactly
        # like a save; apply_save_outcome then promotes the pending switch target.
        self.perform_save()
        self.apply_save_outcome()

    def perform_save(self) -> None:
        """Blocking save I/O. Records the outcome; writes NO renderer-visible state.

        Safe to run on a worker thread: it only touches disk/keyring and stashes the
        result on :attr:`_save_outcome` / :attr:`result` / :attr:`changes_count`
        (none of which the render callbacks read). The stage/status transition is
        applied separately by :meth:`apply_save_outcome` on the UI thread.
        """
        result = self.state.commit_to(self.cfg)
        if not result.saved:
            self._save_outcome = ("error", result.error or "Config validation failed.")
            return
        try:
            save_config(self.cfg)
            new_key = self.state.new_api_key.strip()
            if new_key:
                key_profile = self.state.staged_api_key_target_profile()
                if key_profile:
                    save_persisted_profile_key(key_profile, new_key)
                else:
                    save_persisted_api_key(new_key)
            if self.state.clear_stored_key_confirmed:
                clear_profile = self.state.clear_stored_key_profile or self.state.active_profile
                if clear_profile:
                    clear_persisted_profile_key(clear_profile)
                else:
                    clear_persisted_api_key()
        except (ConfigError, OSError) as exc:
            self._save_outcome = ("error", f"Failed to save config: {exc}")
            return
        self.result = result
        self.changes_count = len(result.changes) + (1 if result.api_key_changed else 0)
        self._save_outcome = ("saved", "")

    def set_save_failure(self, message: str) -> None:
        """Record an unexpected save failure (called by the overlay's worker)."""
        self._save_outcome = ("error", message or "Failed to save config.")

    def set_busy_failure(self, message: str) -> None:
        if self.stage in {"subscription_connecting", "subscription_disconnecting"}:
            self._subscription_action_outcome = (
                "error",
                None,
                message or "Subscription account action failed.",
            )
            return
        self.set_save_failure(message)

    def apply_save_outcome(self) -> None:
        """Apply the recorded save outcome as a stage transition (UI thread)."""
        outcome = self._save_outcome
        self._save_outcome = None
        if outcome is None:
            return
        kind, message = outcome
        if kind == "saved":
            self.saved = True
            if self._switch_target:
                self.switch_workspace = self._switch_target
            self._finish(True)
        else:
            self._switch_target = None
            self._goto("menu")
            self._set_status(message, "err")


__all__ = ["ConfigFlow"]
