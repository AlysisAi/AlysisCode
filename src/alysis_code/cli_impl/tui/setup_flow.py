"""Presentation-agnostic state machine for the full-screen TUI setup wizard.

The classic first-run wizard (:mod:`cli_impl.setup_wizard`) is a Rich-print +
prompt_toolkit-prompt flow. When ``ALYSIS_TUI`` is on we want the *onboarding*
to match the alt-screen launch experience too, so this module expresses the flow
as a step machine that renders a :class:`Screen` description. Unlike the classic
wizard's method-first branching, the TUI opens with a single merged "Connect a
Provider" list (subscription sign-ins and API-key providers side by side); the
selected row dispatches to the right branch — API-key/model setup or subscription OAuth — followed by workspace and
commit. The prompt_toolkit application in
:mod:`cli_impl.tui.setup_app` reads that description and routes key events back
into the flow; nothing here imports prompt_toolkit, so the whole thing is unit
testable by driving the public methods synchronously.

The wizard *logic* is reused wholesale from :mod:`cli_impl.setup_wizard` (preset
list, key/model validation, persistence) and the low-level sandbox/login modules,
so there is a single source of truth for "what setup does"; this module only owns
"how the TUI walks through it".
"""

from __future__ import annotations

import io
import os
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from rich.console import Console as _RichConsole

from ...config import AppConfig, ConfigError
from ...profile_presets import (
    canonical_model_alias_for_preset,
    make_profile_from_preset,
)
from ...profiles import ProfileSpec
from ...workspace_binding import WorkspaceBindingError, resolve_workspace_binding
from .. import setup_wizard as _wiz

# Reuse the wizard's result dataclasses so the persisted shapes stay identical.
_ExecutionStepResult = _wiz._ExecutionStepResult
_DelegatedAuthResult = _wiz._DelegatedAuthResult
_ProfileStepResult = _wiz._ProfileStepResult
_ApiKeyStepResult = _wiz._ApiKeyStepResult
_ModelStepResult = _wiz._ModelStepResult
_WorkspaceStepResult = _wiz._WorkspaceStepResult
_SandboxStepResult = _wiz._SandboxStepResult

Mode = Literal["list", "input", "busy", "confirm", "message", "done"]
Tone = Literal["ok", "warn", "err", "dim", "plain"]

# Stages that count toward the "Step N of M" progress label (the visible
# decisions the user makes).
_PROGRESS_STAGES = (
    "welcome",
    "connect_provider",
    "api_key",
    "model",
    "workspace",
    "sandbox_choice",
)
_SUBSCRIPTION_PROGRESS_STAGES = ("welcome", "connect_provider", "workspace")
_PROGRESS_LABELS = {
    "welcome": "Welcome",
    "connect_provider": "Provider",
    "api_key": "API key",
    "model": "Model",
    "workspace": "Workspace",
    "sandbox_choice": "Sandbox",
}
_PROGRESS_ALIASES = {
    "provider_advanced": ("connect_provider", "Provider (advanced)"),
    "custom_name": ("connect_provider", "Provider (custom)"),
    "custom_url": ("connect_provider", "Provider (custom)"),
    "custom_headers": ("connect_provider", "Provider (custom)"),
    "validating_key": ("api_key", "API key"),
    "custom_model": ("model", "Model"),
    "validating_model": ("model", "Model"),
    "model_thinking": ("model", "Reasoning"),
    "model_not_found_confirm": ("model", "Model"),
    "workspace_create_confirm": ("workspace", "Workspace"),
    "checking_runtime_auth": ("workspace", "Subscription"),
    "runtime_login_confirm": ("workspace", "Subscription"),
    "runtime_logging_in": ("workspace", "Subscription"),
    "diagnosing_sandbox": ("sandbox_choice", "Sandbox"),
    "installing_sandbox": ("sandbox_choice", "Sandbox"),
    "pulling_sandbox": ("sandbox_choice", "Sandbox"),
    "disabling_sandbox": ("sandbox_choice", "Sandbox"),
    "sandbox_pull_confirm": ("sandbox_choice", "Sandbox"),
}
_MAX_KEY_ATTEMPTS = _wiz._MAX_API_KEY_VALIDATION_ATTEMPTS
_POST_COMMIT_STAGES = frozenset(
    {
        "checking_runtime_auth",
        "runtime_login_confirm",
        "runtime_logging_in",
        "diagnosing_sandbox",
        "sandbox_choice",
        "sandbox_pull_confirm",
        "installing_sandbox",
        "pulling_sandbox",
        "disabling_sandbox",
        "login_confirm",
        "logging_in",
    }
)


# Cheap stage → interaction-mode lookup so the application's key-binding filters
# don't have to build a full :class:`Screen` (and its row list) on every repaint.
_STAGE_MODE: dict[str, Mode] = {
    "welcome": "message",
    "complete": "message",
    "fatal": "message",
    "connect_provider": "list",
    "provider_advanced": "list",
    "model": "list",
    "model_thinking": "list",
    "sandbox_choice": "list",
    "custom_name": "input",
    "custom_url": "input",
    "custom_headers": "input",
    "api_key": "input",
    "custom_model": "input",
    "workspace": "input",
    "workspace_create_confirm": "confirm",
    "validating_key": "busy",
    "validating_model": "busy",
    "committing": "busy",
    "checking_runtime_auth": "busy",
    "runtime_logging_in": "busy",
    "diagnosing_sandbox": "busy",
    "installing_sandbox": "busy",
    "pulling_sandbox": "busy",
    "disabling_sandbox": "busy",
    "logging_in": "busy",
    "sandbox_pull_confirm": "confirm",
    "model_not_found_confirm": "confirm",
    "login_confirm": "confirm",
    "runtime_login_confirm": "confirm",
    "cancel_confirm": "confirm",
    "done": "done",
}


@dataclass
class Row:
    """One selectable option in a ``list`` screen (mirrors the picker rows).

    ``kind`` distinguishes real options ("item") from visual-only rows: a
    "header" renders as a dim section caption and a "spacer" as a blank line;
    neither is selectable. ``tone`` optionally tints the description ("warn"
    highlights a problem summary, e.g. a missing required setting).
    """

    label: str
    description: str = ""
    value: str = ""
    current: bool = False
    kind: str = "item"
    tone: str = ""


@dataclass
class Screen:
    """A render description the application paints; pure data, no widgets."""

    stage: str
    mode: Mode
    title: str = ""
    subtitle: str = ""
    # Free-form lines for ``message`` screens (welcome / complete / fatal).
    lines: list[tuple[str, Tone]] = field(default_factory=list)
    # ``list`` mode.
    rows: list[Row] = field(default_factory=list)
    index: int = 0
    # Whether ``list`` rows carry "N." prefixes and answer 1-9 digit jumps.
    numbered: bool = True
    # ``input`` mode.
    input_label: str = ""
    input_default: str = ""
    input_password: bool = False
    # A status line shown under the body (validation result / busy detail).
    status: str = ""
    status_tone: Tone = "dim"
    # ``busy`` mode: which executor the app should use to run :meth:`run_busy`.
    busy_kind: Literal["thread", "terminal"] = "thread"
    busy_label: str = ""
    hint: str = ""
    progress: str = ""
    # ``confirm`` mode: which option Enter selects.
    confirm_default: bool = False
    # Set once the flow terminates so the app can exit with the right result.
    success: bool | None = None


class SetupFlow:
    """Drives the TUI setup wizard one screen at a time.

    The application calls :meth:`screen` to render, the navigation/selection
    methods on key events, and — whenever the current screen is ``busy`` —
    :meth:`run_busy` on a worker (or in a restored terminal for ``terminal``
    busy kinds). Every transition is synchronous and side-effect-explicit so
    tests can walk the whole flow without a terminal.
    """

    def __init__(self, *, report: Callable[[str], None] | None = None) -> None:
        self._report = report or (lambda _msg: None)
        self.stage = "welcome"
        self.index = 0
        self.status = ""
        self.status_tone: Tone = "dim"
        # Collected results.
        self.execution_result: _ExecutionStepResult | None = None
        self.profile_result: _ProfileStepResult | None = None
        self.api_key_result: _ApiKeyStepResult | None = None
        self.model_result: _ModelStepResult | None = None
        self.workspace_result: _WorkspaceStepResult | None = None
        self.sandbox_result: _SandboxStepResult | None = None
        self.cfg: AppConfig | None = None
        self.login_summary: str = ""
        self.login_ok: bool = False
        self.runtime_auth_summary: str = ""
        self.runtime_auth_connected: bool | None = None
        self.diagnostic_lines: list[str] = []
        self.fatal_error: str = ""
        self.success: bool | None = None
        # Transient working state.
        self._key_attempts = 0
        self._custom_name = ""
        self._custom_url = ""
        self._custom_headers: dict[str, str] = {}
        self._last_model_not_found = ""
        self._sandbox_diag: Any = None
        self._sandbox_plan: Any = None
        self._resume_stage = ""  # stage to return to after a cancel prompt
        self._pending_profile: ProfileSpec | None = None
        self._pending_key = ""
        self._pending_workspace_path: Path | None = None
        self._discovered_model_options: tuple[Any, ...] = ()
        self._model_catalog_warning = ""

    # ---------------------------------------------------------------- helpers

    def set_report(self, report: Callable[[str], None]) -> None:
        self._report = report

    def current_mode(self) -> Mode:
        return _STAGE_MODE.get(self.stage, "message")

    def busy_kind(self) -> str:
        return (
            "terminal"
            if self.stage in {"installing_sandbox", "pulling_sandbox", "runtime_logging_in"}
            else "thread"
        )

    def _hosted_alysis(self) -> bool:
        preset = self.profile_result.preset if self.profile_result else None
        if preset is None:
            return False
        try:
            from ...alysis_cloud import PROFILE_KEY
        except Exception:
            return False
        return preset.key == PROFILE_KEY

    def _progress(self, stage: str) -> str:
        uses_subscription = (
            self.execution_result is not None and self.execution_result.backend == "delegated"
        )
        progress_stages = _SUBSCRIPTION_PROGRESS_STAGES if uses_subscription else _PROGRESS_STAGES
        if stage in _PROGRESS_ALIASES:
            base_stage, label = _PROGRESS_ALIASES[stage]
            if base_stage in progress_stages:
                number = progress_stages.index(base_stage) + 1
                return f"Step {number} of {len(progress_stages)} · {label}"
        if stage in progress_stages:
            number = progress_stages.index(stage) + 1
            label = _PROGRESS_LABELS.get(stage, stage.title())
            return f"Step {number} of {len(progress_stages)} · {label}"
        return f"Finishing · {stage.replace('_', ' ')}"

    def _set_status(self, text: str, tone: Tone = "dim") -> None:
        self.status = text
        self.status_tone = tone

    # ------------------------------------------------------------- rendering

    def screen(self) -> Screen:
        builder = getattr(self, f"_screen_{self.stage}", None)
        if builder is None:
            return Screen(
                stage=self.stage,
                mode="message",
                title="Setup",
                lines=[(f"(unknown stage {self.stage})", "err")],
            )
        scr: Screen = builder()
        scr.progress = self._progress(self.stage)
        if not scr.status:
            scr.status = self.status
            scr.status_tone = self.status_tone
        scr.index = self.index
        scr.success = self.success
        return scr

    def _screen_welcome(self) -> Screen:
        return Screen(
            stage="welcome",
            mode="message",
            title="Welcome to Alysis Code",
            subtitle="A coding agent that runs in your terminal.",
            lines=[
                ("First pick the AI provider Alysis Code should connect to.", "plain"),
                ("", "plain"),
                (
                    "One list covers everything: subscription sign-ins, API-key "
                    "providers, and local endpoints.",
                    "dim",
                ),
                ("Setup then asks for the workspace folder you want to work on.", "dim"),
            ],
            hint="▶  Press Enter to continue",
        )

    def _screen_connect_provider(self) -> Screen:
        # One merged list (Kilo-style): subscription sign-ins first, then
        # API-key providers, with local/custom endpoints behind the advanced
        # row. The description column carries the auth method so no upfront
        # method choice is needed.
        rows = [
            Row(label=label, description=description, value=value)
            for value, label, description in _wiz._connect_provider_picker_rows()
        ]
        return Screen(
            stage="connect_provider",
            mode="list",
            title="Connect a Provider",
            subtitle="Pick the provider you want Alysis Code to use.",
            rows=rows,
            hint="↑↓ move · Enter select · Esc back",
        )

    def _screen_provider_advanced(self) -> Screen:
        rows = [
            Row(label=_wiz.preset_selection_label(preset), value=preset.key)
            for preset in _wiz._advanced_setup_presets()
        ]
        return Screen(
            stage="provider_advanced",
            mode="list",
            title="Advanced Provider Profile",
            subtitle=(
                "Local endpoints (Ollama, LM Studio, vLLM), custom URLs, legacy "
                "OpenAI-compatible presets, and Alysis Code account sign-in."
            ),
            rows=rows,
            hint="↑↓ move · Enter select · Esc back",
        )

    def _screen_custom_name(self) -> Screen:
        return Screen(
            stage="custom_name",
            mode="input",
            title="Custom endpoint",
            subtitle="Name this profile (lowercase, no spaces).",
            input_label="Profile name",
            input_default=self._custom_name or "custom",
            hint="Enter next · Esc back",
        )

    def _screen_custom_url(self) -> Screen:
        return Screen(
            stage="custom_url",
            mode="input",
            title="Custom endpoint",
            subtitle="The OpenAI-compatible base URL for this provider.",
            input_label="Base URL",
            input_default=self._custom_url,
            hint="Enter next · Esc back",
        )

    def _screen_custom_headers(self) -> Screen:
        existing = ", ".join(f"{k}={v}" for k, v in (self._custom_headers or {}).items())
        return Screen(
            stage="custom_headers",
            mode="input",
            title="Custom endpoint",
            subtitle="Extra request headers, if your endpoint needs them (org id, x-api-key…).",
            input_label="Extra headers (k=v, comma-separated)",
            input_default=existing or "(none — Enter to skip)",
            hint="Enter next · Esc back",
        )

    def _screen_api_key(self) -> Screen:
        profile = self.profile_result.profile  # type: ignore[union-attr]
        required = bool(profile.api_key_env)
        has_key = self.api_key_result is not None and bool(self.api_key_result.api_key)
        if required:
            label = "Paste your API key"
            if has_key:
                label += " (Enter to keep current)"
        else:
            label = "API key (optional — Enter to skip)"
        sub = f"Alysis Code will use {self.profile_result.label}."  # type: ignore[union-attr]
        if profile.api_key_env:
            sub += f"  The key can also be set via {profile.api_key_env}."
        return Screen(
            stage="api_key",
            mode="input",
            title="API Key",
            subtitle=sub,
            input_label=label,
            input_password=True,
            hint="Enter validate · Esc back",
        )

    def _screen_model(self) -> Screen:
        rows: list[Row] = []
        for value, label, description in _wiz._model_picker_rows(  # type: ignore[arg-type]
            self.profile_result,
            discovered_models=self._discovered_model_options,
        ):
            rows.append(Row(label=label, description=description, value=value))
        return Screen(
            stage="model",
            mode="list",
            title="Default Model",
            subtitle=f"Pick the model Alysis Code will use by default for {self.profile_result.label}.",  # type: ignore[union-attr]
            rows=rows,
            status=self._model_catalog_warning,
            status_tone="warn" if self._model_catalog_warning else "dim",
            hint="↑↓ move · 1-9 pick · Enter select · Esc back",
        )

    def _screen_model_thinking(self) -> Screen:
        model = self.model_result.model if self.model_result is not None else ""
        current = self.model_result.reasoning_label if self.model_result is not None else "auto"
        labels = _wiz._setup_reasoning_labels(  # type: ignore[arg-type]
            self.profile_result,
            model=model,
            current=current,
        )
        descriptions = {
            "off": "disable reasoning with this model's documented NVIDIA control",
            "low": "lower reasoning effort",
            "medium": "balanced reasoning effort",
            "high": "full reasoning effort",
            "max": "maximum reasoning effort",
            "auto": "use the NVIDIA-hosted model's default",
        }
        return Screen(
            stage="model_thinking",
            mode="list",
            title="Reasoning Effort",
            subtitle=f"Verified reasoning modes for {model} on NVIDIA.",
            rows=[
                Row(
                    label=label,
                    description=descriptions.get(label, "provider-documented reasoning effort"),
                    value=label,
                    current=label == current,
                )
                for label in labels
            ],
            hint="↑↓ move · Enter select · Esc back",
        )

    def _screen_custom_model(self) -> Screen:
        return Screen(
            stage="custom_model",
            mode="input",
            title="Default Model",
            subtitle="Type any model id supported by this provider.",
            input_label="Model",
            input_default=self._last_model_not_found,
            hint="Enter next · Esc back",
        )

    def _screen_workspace(self) -> Screen:
        default = _wiz._suggest_workspace_default()
        prev = self.workspace_result.workspace if self.workspace_result else os.fspath(default)
        return Screen(
            stage="workspace",
            mode="input",
            title="Workspace",
            subtitle="Alysis Code reads and edits files inside one folder.",
            input_label="Workspace folder",
            input_default=prev,
            hint="Enter confirm · Esc back",
        )

    def _screen_workspace_create_confirm(self) -> Screen:
        pending = os.fspath(self._pending_workspace_path or "")
        return Screen(
            stage="workspace_create_confirm",
            mode="confirm",
            title="Workspace",
            subtitle="That folder does not exist yet.",
            lines=[(f"Create it now? {pending}", "plain")],
            hint="Y create · N choose another · Esc back",
            confirm_default=True,
        )

    # ----- busy screens (the app runs run_busy(), they transition themselves)

    def _screen_validating_key(self) -> Screen:
        return Screen(
            stage="validating_key", mode="busy", title="API Key", busy_label="Validating key…"
        )

    def _screen_validating_model(self) -> Screen:
        return Screen(
            stage="validating_model",
            mode="busy",
            title="Default Model",
            busy_label="Validating model…",
        )

    def _screen_committing(self) -> Screen:
        return Screen(stage="committing", mode="busy", title="Saving", busy_label="Saving setup…")

    def _screen_checking_runtime_auth(self) -> Screen:
        return Screen(
            stage="checking_runtime_auth",
            mode="busy",
            title="AI subscription",
            busy_label="Checking subscription connection…",
        )

    def _screen_runtime_logging_in(self) -> Screen:
        return Screen(
            stage="runtime_logging_in",
            mode="busy",
            busy_kind="terminal",
            title="AI subscription",
            busy_label="Opening provider sign-in…",
        )

    def _screen_diagnosing_sandbox(self) -> Screen:
        return Screen(
            stage="diagnosing_sandbox",
            mode="busy",
            title="Sandbox",
            busy_label="Checking sandbox readiness…",
        )

    def _screen_installing_sandbox(self) -> Screen:
        return Screen(
            stage="installing_sandbox",
            mode="busy",
            busy_kind="terminal",
            title="Sandbox",
            busy_label="Installing bubblewrap… (watch the terminal — you may be asked for sudo)",
        )

    def _screen_pulling_sandbox(self) -> Screen:
        return Screen(
            stage="pulling_sandbox",
            mode="busy",
            busy_kind="terminal",
            title="Sandbox",
            busy_label="Pulling sandbox image… (this can take a minute)",
        )

    def _screen_disabling_sandbox(self) -> Screen:
        return Screen(
            stage="disabling_sandbox", mode="busy", title="Sandbox", busy_label="Disabling sandbox…"
        )

    def _screen_logging_in(self) -> Screen:
        return Screen(
            stage="logging_in", mode="busy", title="Alysis Code account", busy_label="Connecting…"
        )

    # ----- confirm screens

    def _screen_sandbox_choice(self) -> Screen:
        rows: list[Row] = []
        if self._sandbox_plan is not None:
            rows.append(
                Row(
                    label="Install bubblewrap now (keeps sandbox on)",
                    description=f"Runs: {self._sandbox_plan.display}",
                    value="install_bwrap",
                )
            )
        rows.append(
            Row(
                label="Run without sandbox for now (less safe)",
                description="Commands run on the host shell. Re-enable anytime in /config.",
                value="disable",
            )
        )
        rows.append(
            Row(
                label="Decide later",
                description="Finish with `alysis sandbox setup`.",
                value="later",
            )
        )
        return Screen(
            stage="sandbox_choice",
            mode="list",
            title="Sandbox",
            subtitle="No sandbox backend (bubblewrap or Docker) was found. How do you want to proceed?",
            rows=rows,
            hint="↑↓ move · Enter select · Esc skip",
        )

    def _screen_sandbox_pull_confirm(self) -> Screen:
        return Screen(
            stage="sandbox_pull_confirm",
            mode="confirm",
            title="Sandbox",
            subtitle="The sandbox image was not found locally.",
            lines=[("Pull the Alysis Code sandbox image now? (~50MB)", "plain")],
            hint="Y pull · N skip · Esc skip",
            confirm_default=True,
        )

    def _screen_model_not_found_confirm(self) -> Screen:
        return Screen(
            stage="model_not_found_confirm",
            mode="confirm",
            title="Default Model",
            subtitle="The provider reported this model is missing.",
            lines=[(f"Use custom model '{self._last_model_not_found}' anyway?", "warn")],
            hint="Y use it · N pick again · Esc back",
        )

    def _screen_login_confirm(self) -> Screen:
        return Screen(
            stage="login_confirm",
            mode="confirm",
            title="Alysis Code account",
            subtitle="Your provider is the Alysis Code endpoint.",
            lines=[("Connect your Alysis Code account now?", "plain")],
            hint="Y connect · N later · Esc skip",
            confirm_default=True,
        )

    def _screen_runtime_login_confirm(self) -> Screen:
        runtime_label = (
            self.execution_result.label if self.execution_result is not None else "AI subscription"
        )
        return Screen(
            stage="runtime_login_confirm",
            mode="confirm",
            title="Connect subscription",
            subtitle=f"{runtime_label} is not connected.",
            lines=[
                ("Connect now using the provider's browser login?", "plain"),
                (
                    "Provider login is an immediate external side effect. Cancelling or "
                    "leaving setup later does not undo it.",
                    "warn",
                ),
            ],
            hint="Y connect · N later · Esc skip",
            confirm_default=True,
        )

    def _screen_cancel_confirm(self) -> Screen:
        settings_saved = self._resume_stage in _POST_COMMIT_STAGES
        return Screen(
            stage="cancel_confirm",
            mode="confirm",
            title="Cancel setup",
            lines=(
                [
                    ("Leave setup? Your model access settings are already saved.", "warn"),
                    ("Leaving does not undo provider sign-in or sign-out actions.", "dim"),
                ]
                if settings_saved
                else [("Cancel setup? No changes will be saved.", "warn")]
            ),
            hint="Y cancel · N keep going",
        )

    def _screen_complete(self) -> Screen:
        lines = self._summary_lines()
        delegated = self.cfg is not None and self.cfg.execution.backend == "delegated"
        subscription_selection_required = False
        if self.cfg is not None and not delegated:
            try:
                from ...profiles import active_subscription_selection_ready, get_active_profile

                subscription_selection_required = bool(
                    get_active_profile(self.cfg).auth_provider
                    and not active_subscription_selection_ready(self.cfg)
                )
            except Exception:
                subscription_selection_required = False
        return Screen(
            stage="complete",
            mode="message",
            title="Setup complete",
            lines=lines,
            hint=(
                "▶  Press Enter to finish setup"
                if delegated
                else (
                    "▶  Press Enter to choose model and reasoning in /config"
                    if subscription_selection_required
                    else "▶  Press Enter to start chatting"
                )
            ),
        )

    def _screen_fatal(self) -> Screen:
        return Screen(
            stage="fatal",
            mode="message",
            title="Setup failed",
            lines=[(self.fatal_error or "Setup could not be completed.", "err")],
            hint="▶  Press Enter to exit",
        )

    # ------------------------------------------------------------- summaries

    def _summary_lines(self) -> list[tuple[str, Tone]]:
        out: list[tuple[str, Tone]] = []
        delegated = (
            self.execution_result is not None and self.execution_result.backend == "delegated"
        )
        if delegated and self.execution_result is not None:
            out.append(("Connection  AI subscription", "ok"))
            out.append((f"Provider    {self.execution_result.label}", "plain"))
            auth_summary = self.runtime_auth_summary or "not connected"
            out.append(
                (
                    f"Sign-in     {auth_summary}",
                    "ok" if self.runtime_auth_connected else "warn",
                )
            )
        if self.profile_result is not None:
            out.append((f"Profile    {self.profile_result.label} (active)", "ok"))
        if self.api_key_result is not None:
            out.append((f"API key    {self._api_key_summary()}", self._api_key_tone()))
        if self.model_result is not None:
            out.append((f"Model      {self.model_result.model}", "plain"))
            if (
                self.profile_result is not None
                and self.profile_result.preset is not None
                and self.profile_result.preset.key in _wiz._MODEL_REASONING_SETUP_PRESET_KEYS
            ):
                out.append((f"Reasoning  {self.model_result.reasoning_label}", "plain"))
        elif self.cfg is not None and self.cfg.execution.backend == "native":
            try:
                from ...profiles import active_subscription_selection_ready, get_active_profile

                active = get_active_profile(self.cfg)
                selection_ready = active_subscription_selection_ready(self.cfg)
            except Exception:
                active = None
                selection_ready = True
            if active is not None and active.auth_provider and not selection_ready:
                out.append(("Model      choose in /config → Default Model", "warn"))
                out.append(("Reasoning  choose with the model in /config", "warn"))
            else:
                out.append((f"Model      {self.cfg.model}", "plain"))
                if active is not None and active.reasoning_effort is not None:
                    out.append((f"Reasoning  {active.reasoning_effort}", "plain"))
        if self.workspace_result is not None:
            out.append((f"Workspace  {self.workspace_result.workspace}", "plain"))
        if self.sandbox_result is not None:
            out.append((f"Sandbox    {self._sandbox_summary()}", self._sandbox_tone()))
        if self.login_summary:
            out.append((f"Account    {self.login_summary}", "ok" if self.login_ok else "warn"))
        for issue in self.diagnostic_lines:
            out.append((f"Diagnostic: {issue}", "warn"))
        out.append(("", "plain"))
        if delegated and self.execution_result is not None:
            runtime_id = str(self.execution_result.runtime or "").strip()
            if self.runtime_auth_connected:
                out.append(("Your AI subscription is connected.", "dim"))
                out.append(
                    (
                        "Provider sign-in is immediate; leaving setup does not sign you out.",
                        "dim",
                    )
                )
            else:
                out.append(
                    (
                        f"Next: run `alysis auth login {runtime_id}` before using this subscription.",
                        "warn",
                    )
                )
                if self.execution_result.auth_hint:
                    out.append((self.execution_result.auth_hint, "dim"))
        else:
            out.append(("Type your first message to start chatting.", "dim"))
        return out

    def _api_key_summary(self) -> str:
        r = self.api_key_result
        if r is None or not r.api_key:
            return "not stored"
        reason = (r.validation_message or "").strip()
        if r.validation_status == "validated":
            return "stored, validated"
        if r.validation_status == "inconclusive":
            return "stored, validation inconclusive" + (f" ({reason})" if reason else "")
        if r.validation_status == "failed":
            return "stored, but provider rejected it. Re-enter via /config."
        if r.validation_status == "skipped":
            return "stored, validation skipped"
        if r.validation_status == "model_not_found":
            return "stored, model validation failed"
        return "stored"

    def _api_key_tone(self) -> Tone:
        r = self.api_key_result
        if r is None or not r.api_key:
            return "warn"
        if r.validation_status == "validated":
            return "ok"
        if r.validation_status in {"failed", "model_not_found"}:
            return "err"
        return "warn"

    def _sandbox_summary(self) -> str:
        r = self.sandbox_result
        if r is None:
            return "skipped"
        if r.ready:
            return f"ready ({r.status})"
        if r.status == "disabled":
            return "off — host execution (less safe); re-enable via /config"
        if r.status == "skipped":
            return "skipped — run `alysis sandbox setup` to finish"
        return f"{r.status} — run `alysis sandbox setup` to finish"

    def _sandbox_tone(self) -> Tone:
        r = self.sandbox_result
        if r is not None and r.ready:
            return "ok"
        return "warn"

    # ----------------------------------------------------------- navigation

    def move(self, delta: int) -> None:
        scr = self.screen()
        if scr.mode != "list" or not scr.rows:
            return
        self.index = (self.index + delta) % len(scr.rows)

    def choose_index(self, idx: int) -> None:
        scr = self.screen()
        if scr.mode != "list" or not scr.rows:
            return
        if 0 <= idx < len(scr.rows):
            self.index = idx
            self.choose(scr.rows[idx].value)

    def choose_current(self) -> None:
        scr = self.screen()
        if scr.mode != "list" or not scr.rows:
            return
        self.choose(scr.rows[self.index].value)

    def choose(self, value: str) -> None:
        if self.stage == "connect_provider":
            self._choose_connect_provider(value)
        elif self.stage == "provider_advanced":
            self._choose_provider(value)
        elif self.stage == "model":
            self._choose_model(value)
        elif self.stage == "model_thinking":
            self._choose_model_thinking(value)
        elif self.stage == "sandbox_choice":
            self._choose_sandbox(value)

    def submit_input(self, text: str) -> None:
        text = str(text)
        if self.stage == "custom_name":
            self._submit_custom_name(text)
        elif self.stage == "custom_url":
            self._submit_custom_url(text)
        elif self.stage == "custom_headers":
            self._submit_custom_headers(text)
        elif self.stage == "api_key":
            self._submit_api_key(text)
        elif self.stage == "custom_model":
            self._submit_custom_model(text)
        elif self.stage == "workspace":
            self._submit_workspace(text)

    def confirm(self, yes: bool) -> None:
        if self.stage == "cancel_confirm":
            if yes:
                self._finish(False)
            else:
                self._goto(self._resume_stage or "welcome")
        elif self.stage == "sandbox_pull_confirm":
            if yes:
                self._goto("pulling_sandbox")
            else:
                self._sandbox_skip()
        elif self.stage == "model_not_found_confirm":
            if yes:
                self._accept_unconfirmed_model()
            else:
                self._goto("model")
        elif self.stage == "workspace_create_confirm":
            if yes:
                self._accept_workspace_path(
                    self._pending_workspace_path,
                    create_if_missing=True,
                )
            else:
                self._pending_workspace_path = None
                self._goto("workspace")
                self._set_status(
                    "Type an existing folder, or enter a new folder and choose Create.",
                    "warn",
                )
        elif self.stage == "login_confirm":
            if yes:
                self._goto("logging_in")
            else:
                self.login_summary = ""
                self._goto("complete")
        elif self.stage == "runtime_login_confirm":
            if yes:
                self._goto("runtime_logging_in")
            else:
                self._skip_runtime_login()

    def advance_message(self) -> None:
        """Enter on a ``message`` screen."""
        if self.stage == "welcome":
            self._goto("connect_provider")
        elif self.stage == "complete":
            self._finish(True)
        elif self.stage == "fatal":
            self._finish(False)

    def back(self) -> None:
        custom_profile = self.profile_result is not None and self.profile_result.preset is None
        prev = {
            "connect_provider": "welcome",
            "provider_advanced": "connect_provider",
            "custom_name": "provider_advanced",
            "custom_url": "custom_name",
            "custom_headers": "custom_url",
            "api_key": "custom_headers" if custom_profile else "connect_provider",
            "model": "api_key",
            "custom_model": "model",
            "model_thinking": "model",
            "workspace": (
                "connect_provider"
                if self.execution_result is not None
                and self.execution_result.backend == "delegated"
                else (
                    "model_thinking"
                    if self.model_result is not None
                    and self.profile_result is not None
                    and _wiz._setup_reasoning_labels(
                        self.profile_result,
                        model=self.model_result.model,
                        current=self.model_result.reasoning_label,
                    )
                    != ("auto",)
                    else "model"
                )
            ),
            "workspace_create_confirm": "workspace",
        }.get(self.stage)
        if self.stage == "welcome":
            self.request_cancel()
            return
        if prev is None:
            # On a confirm/busy/terminal screen Esc is a soft "skip back".
            if self.stage in {"sandbox_choice", "sandbox_pull_confirm"}:
                self._sandbox_skip()
                return
            if self.stage == "login_confirm":
                self.login_summary = ""
                self._goto("complete")
                return
            if self.stage == "runtime_login_confirm":
                self._skip_runtime_login()
                return
            if self.stage == "model_not_found_confirm":
                self._goto("model")
                return
            return
        self._set_status("", "dim")
        self._goto(prev)

    def request_cancel(self) -> None:
        """Ctrl+C / Esc-at-welcome: confirm before discarding."""
        if self.stage in {"cancel_confirm", "complete", "fatal", "done"}:
            return
        self._resume_stage = self.stage
        self._goto("cancel_confirm", keep_status=True)

    # --------------------------------------------------------- stage helpers

    def _goto(self, stage: str, *, reset_index: bool = True, keep_status: bool = False) -> None:
        self.stage = stage
        if reset_index:
            self.index = 0
        if not keep_status:
            self._set_status("", "dim")

    def _finish(self, success: bool) -> None:
        self.success = success
        self.stage = "done"

    # --------------------------------------------------------- provider step

    def _choose_connect_provider(self, value: str) -> None:
        """Dispatch a merged-picker row to the right branch.

        Rows are either a subscription (``runtime:<id>`` → workspace, OAuth
        happens post-commit), the advanced sub-list, or an API-key/local
        provider preset (→ API key step). Switching between a subscription and
        a provider — or between different subscriptions — invalidates the
        dependent step results, mirroring the old method-first reset.
        """
        if value == _wiz._ADVANCED_PROVIDER_PRESETS_VALUE:
            self._goto("provider_advanced")
            return
        if value.startswith(_wiz._RUNTIME_EXECUTION_PREFIX):
            try:
                selected = _wiz._execution_result_from_value(value)
            except ConfigError as exc:
                self._set_status(str(exc), "err")
                return
            if self.execution_result is not None and selected != self.execution_result:
                self.profile_result = None
                self.api_key_result = None
                self.model_result = None
                self._discovered_model_options = ()
                self._model_catalog_warning = ""
            self.execution_result = selected
            self._goto("workspace")
            return
        selected = _ExecutionStepResult(backend="native")
        if self.execution_result is not None and selected != self.execution_result:
            self.profile_result = None
            self.api_key_result = None
            self.model_result = None
            self._discovered_model_options = ()
            self._model_catalog_warning = ""
        self.execution_result = selected
        self._choose_provider(value)

    def _choose_provider(self, key: str) -> None:
        if key == _wiz._ADVANCED_PROVIDER_PRESETS_VALUE:
            self._goto("provider_advanced")
            return
        preset = _wiz._preset_by_key(key)
        if preset is None:
            self._set_status(f"Unknown provider preset: {key}", "err")
            return
        if preset.key == _wiz._CUSTOM_PROVIDER_KEY:
            self._goto("custom_name")
            return
        profile = make_profile_from_preset(preset)
        new = _ProfileStepResult(profile=profile, label=preset.label, preset=preset)
        # Re-selecting a different provider invalidates the dependent steps.
        if self.profile_result is not None and new != self.profile_result:
            self.api_key_result = None
            self.model_result = None
            self._discovered_model_options = ()
            self._model_catalog_warning = ""
        self.profile_result = new
        warning = (preset.setup_warning or "").strip()
        if self._hosted_alysis():
            # The Alysis Code account needs no API key and login picks the
            # hosted default model — skip both steps and head straight to the
            # workspace; the post-commit login_confirm step connects the
            # account in the browser.
            self.api_key_result = _ApiKeyStepResult(
                api_key="",
                validation_status="skipped",
                validation_message="Alysis Code account uses browser sign-in — no API key.",
            )
            from ... import account_login

            self.model_result = _ModelStepResult(model=account_login._FRESH_LOGIN_DEFAULT_MODEL)
            self._goto("workspace")
            self._set_status(
                "Alysis Code account: no API key needed — you'll sign in after setup.",
                "ok",
            )
            return
        self._goto("api_key")
        if warning:
            self._set_status(warning, "warn")

    def _submit_custom_name(self, text: str) -> None:
        name = text.strip().lower() or "custom"
        self._custom_name = name
        self._goto("custom_url")

    def _submit_custom_url(self, text: str) -> None:
        url = text.strip()
        if not url:
            self._set_status("Base URL is required.", "err")
            return
        self._custom_url = url
        self._goto("custom_headers")

    def _submit_custom_headers(self, text: str) -> None:
        headers, ok = self._parse_extra_headers(text)
        if not ok:
            self._set_status("Extra headers must use k=v syntax.", "err")
            return
        self._custom_headers = headers
        profile = ProfileSpec(
            name=self._custom_name,
            protocol="openai_compat",
            base_url=self._custom_url,
            extra_headers=headers,
            notes="Custom OpenAI-compatible endpoint.",
        )
        new = _ProfileStepResult(profile=profile, label=profile.name, preset=None)
        if self.profile_result is not None and new != self.profile_result:
            self.api_key_result = None
            self.model_result = None
            self._discovered_model_options = ()
            self._model_catalog_warning = ""
        self.profile_result = new
        self._goto("api_key")

    @staticmethod
    def _parse_extra_headers(text: str) -> tuple[dict[str, str], bool]:
        """Parse ``k=v, k=v`` into a header dict (mirrors the classic wizard).

        Returns ``(headers, ok)``; ``ok`` is False when an item lacks ``=`` so
        the caller can re-prompt. An empty string yields ``({}, True)``.
        """
        headers: dict[str, str] = {}
        for item in str(text).split(","):
            piece = item.strip()
            if not piece:
                continue
            if "=" not in piece:
                return {}, False
            key, value = piece.split("=", 1)
            if key.strip() and value.strip():
                headers[key.strip()] = value.strip()
        return headers, True

    # --------------------------------------------------------- api key step

    def _submit_api_key(self, text: str) -> None:
        profile = self.profile_result.profile  # type: ignore[union-attr]
        value = text.strip()
        required = bool(profile.api_key_env)
        has_key = self.api_key_result is not None and bool(self.api_key_result.api_key)
        if not value:
            if has_key:
                # Returning to this step (e.g. Esc from model) keeps the already
                # validated key instead of forcing a re-paste — matches the
                # classic wizard's "Enter to keep current".
                self._goto("model")
                return
            if required:
                # Empty has no cap of its own (it just re-prompts), so it must not
                # consume the genuine validation-failure budget (_key_attempts).
                self._set_status("API key is required to continue.", "err")
                return
            self.api_key_result = _ApiKeyStepResult(
                api_key="", validation_status="skipped", validation_message="No API key provided."
            )
            self._goto("model")
            return
        # Stash the candidate and validate on the worker.
        self._pending_key = value
        self._goto("validating_key")

    def _run_validating_key(self) -> None:
        value = getattr(self, "_pending_key", "")
        self._report("Validating key…")
        validation = _wiz._validate_api_key(
            profile=self.profile_result.profile,  # type: ignore[union-attr]
            api_key=value,
            suggested_models=_wiz._suggested_models(self.profile_result),  # type: ignore[arg-type]
            validation_model=_wiz._validation_model_hint(self.profile_result),  # type: ignore[arg-type]
        )
        status = validation.status
        message = validation.message
        if status == "validated":
            self.api_key_result = _ApiKeyStepResult(
                api_key=value, validation_status="validated", validation_message=message
            )
            self._load_provider_model_catalog(value)
            self._goto("model")
            if message:
                self._set_status(message, "warn")
            return
        if status in {"inconclusive", "model_not_found"}:
            self.api_key_result = _ApiKeyStepResult(
                api_key=value, validation_status=status, validation_message=message
            )
            self._load_provider_model_catalog(value)
            self._goto("model")
            self._set_status(
                (message or "")
                + (
                    " We'll verify your chosen model next."
                    if status == "model_not_found"
                    else " Continuing without validation."
                ),
                "warn",
            )
            return
        # Failed: allow a few retries, then continue with the last key.
        self._key_attempts += 1
        if self._key_attempts >= _MAX_KEY_ATTEMPTS:
            self.api_key_result = _ApiKeyStepResult(
                api_key=value, validation_status="failed", validation_message=message
            )
            self._discovered_model_options = ()
            self._model_catalog_warning = (
                "Live model discovery was skipped because the provider rejected the API key."
            )
            self._goto("model")
            self._set_status("Continuing with the last key. Fix it later in /config.", "warn")
            return
        self._goto("api_key")
        self._set_status(
            f"Key validation failed: {message or 'provider rejected the key'} (attempt {self._key_attempts}/{_MAX_KEY_ATTEMPTS})",
            "err",
        )

    def _load_provider_model_catalog(self, api_key: str) -> None:
        options, warning = _wiz._discover_setup_provider_models(  # type: ignore[arg-type]
            self.profile_result,
            api_key=api_key,
        )
        self._discovered_model_options = options
        self._model_catalog_warning = warning

    # ----------------------------------------------------------- model step

    def _choose_model(self, value: str) -> None:
        if value == _wiz._CUSTOM_MODEL_VALUE:
            self._goto("custom_model")
            return
        self._set_model(value, custom=False)

    def _submit_custom_model(self, text: str) -> None:
        model = text.strip()
        if not model:
            self._set_status("Model is required.", "err")
            return
        self._set_model(model, custom=True)

    def _set_model(self, model: str, *, custom: bool) -> None:
        preset = self.profile_result.preset if self.profile_result else None
        if preset is not None:
            model = canonical_model_alias_for_preset(preset, model)
        model = model.strip()
        if not model:
            self._set_status("Default model is required.", "err")
            return
        self.model_result = _ModelStepResult(model=model, custom=custom)
        self._goto("validating_model")

    def _advance_after_model_validation(self) -> None:
        if self.model_result is None or self.profile_result is None:
            self._goto("workspace")
            return
        labels = _wiz._setup_reasoning_labels(
            self.profile_result,
            model=self.model_result.model,
            current=self.model_result.reasoning_label,
        )
        if labels == ("auto",):
            self.model_result = replace(self.model_result, reasoning_label="auto")
            self._goto("workspace")
            preset = self.profile_result.preset
            if preset is not None and preset.key == _wiz._NVIDIA_PRESET_KEY:
                self._set_status(
                    "No verified reasoning control is known for this NVIDIA-hosted model; "
                    "the provider default will be used.",
                    "warn",
                )
            return
        self._goto("model_thinking")

    def _choose_model_thinking(self, value: str) -> None:
        if self.model_result is None:
            self._goto("model")
            return
        labels = _wiz._setup_reasoning_labels(  # type: ignore[arg-type]
            self.profile_result,
            model=self.model_result.model,
            current=self.model_result.reasoning_label,
        )
        if value not in labels:
            self._set_status("That reasoning effort is not supported by this model.", "err")
            return
        self.model_result = replace(self.model_result, reasoning_label=value)
        self._goto("workspace")

    def _run_validating_model(self) -> None:
        key = self.api_key_result
        if key is None or not key.api_key or key.validation_status == "failed":
            # Nothing to validate against; accept the model and move on.
            self._advance_after_model_validation()
            return
        self._report("Validating model…")
        validation = _wiz._validate_api_key(
            profile=self.profile_result.profile,  # type: ignore[union-attr]
            api_key=key.api_key,
            model=self.model_result.model,  # type: ignore[union-attr]
            suggested_models=_wiz._suggested_models(self.profile_result),  # type: ignore[arg-type]
        )
        self.api_key_result = replace(
            key, validation_status=validation.status, validation_message=validation.message
        )
        if validation.status == "model_not_found":
            self._last_model_not_found = self.model_result.model  # type: ignore[union-attr]
            if self.model_result is not None and self.model_result.custom:
                self._goto("model_not_found_confirm")
            else:
                self._goto("model")
                self._set_status(validation.message or "Model not found at this provider.", "err")
            return
        if validation.status == "validated":
            self._advance_after_model_validation()
            self._set_status("Model validated.", "ok")
            return
        if validation.status == "failed":
            self._advance_after_model_validation()
            self._set_status(validation.message or "Provider rejected the key.", "err")
            return
        self._advance_after_model_validation()
        if validation.message:
            self._set_status(validation.message, "warn")

    def _accept_unconfirmed_model(self) -> None:
        warning = (
            f"Model '{self._last_model_not_found}' was not confirmed; "
            "provider reported it missing and you chose to use it anyway."
        )
        if self.api_key_result is not None:
            self.api_key_result = replace(
                self.api_key_result, validation_status="inconclusive", validation_message=warning
            )
        self._advance_after_model_validation()
        self._set_status(warning, "warn")

    # ------------------------------------------------------- workspace step

    def _submit_workspace(self, text: str) -> None:
        default = _wiz._suggest_workspace_default()
        raw = text.strip() or os.fspath(default)
        selected = Path(raw).expanduser().resolve(strict=False)
        if not selected.exists():
            self._pending_workspace_path = selected
            self._goto("workspace_create_confirm")
            return
        self._accept_workspace_path(selected, create_if_missing=False)

    def _accept_workspace_path(
        self,
        selected: Path | None,
        *,
        create_if_missing: bool,
    ) -> None:
        if selected is None:
            self._goto("workspace")
            self._set_status("Choose a workspace folder.", "err")
            return
        try:
            binding = resolve_workspace_binding(
                selected,
                create_if_missing=create_if_missing,
                allow_broad_workspace=True,
                source="setup_wizard",
            )
        except WorkspaceBindingError as exc:
            self._goto("workspace")
            self._set_status(self._workspace_error_message(selected, exc), "err")
            return
        self.workspace_result = _WorkspaceStepResult(workspace=os.fspath(binding.requested_path))
        self._pending_workspace_path = None
        self._goto("committing")

    @staticmethod
    def _workspace_error_message(selected: Path, exc: WorkspaceBindingError) -> str:
        message = str(exc)
        if "create_if_missing=True" in message:
            return f"Folder does not exist: {selected}. Type an existing folder or choose Create."
        if "not a directory" in message:
            return f"That path is not a folder: {selected}"
        return message

    # ---------------------------------------------------------- commit step

    def _run_committing(self) -> None:
        self._report("Saving setup…")
        sink = _RichConsole(file=io.StringIO(), force_terminal=False, no_color=True)
        try:
            if self.execution_result is not None and self.execution_result.backend == "delegated":
                cfg = _wiz._commit_delegated_setup(
                    execution_result=self.execution_result,
                    workspace_result=self.workspace_result,  # type: ignore[arg-type]
                    console=sink,
                )
            else:
                cfg = _wiz._commit_setup(
                    profile_result=self.profile_result,  # type: ignore[arg-type]
                    api_key_result=self.api_key_result,  # type: ignore[arg-type]
                    model_result=self.model_result,  # type: ignore[arg-type]
                    workspace_result=self.workspace_result,  # type: ignore[arg-type]
                    console=sink,
                )
        except (ConfigError, OSError) as exc:
            self.fatal_error = f"Failed to save setup: {exc}"
            self._goto("fatal")
            return
        self.cfg = cfg
        if self.execution_result is not None and self.execution_result.backend == "delegated":
            self.diagnostic_lines = []
            self.sandbox_result = None
            self._goto("checking_runtime_auth")
            return
        self.diagnostic_lines = self._collect_diagnostics(cfg)
        self._goto("diagnosing_sandbox")

    def _collect_diagnostics(self, cfg: AppConfig) -> list[str]:
        try:
            from ...provider_diagnostics import provider_diagnostic_warning_lines

            return list(provider_diagnostic_warning_lines(cfg))
        except Exception:
            return []

    # ------------------------------------------------ runtime account step

    def _run_checking_runtime_auth(self) -> None:
        self._report("Checking subscription connection…")
        result: _DelegatedAuthResult = _wiz._check_delegated_runtime_connection(
            self.cfg,  # type: ignore[arg-type]
            self.execution_result,  # type: ignore[arg-type]
        )
        self.runtime_auth_connected = result.connected
        self.runtime_auth_summary = result.summary
        if result.connected and self.cfg is not None and self.cfg.execution.backend == "native":
            try:
                _wiz._sync_direct_subscription_model(
                    self.cfg,
                    str(self.execution_result.runtime or ""),  # type: ignore[union-attr]
                )
            except Exception as exc:  # noqa: BLE001
                self._goto("runtime_login_confirm")
                self._set_status(f"Connected, but model discovery failed: {exc}", "err")
                return
            self.diagnostic_lines = self._collect_diagnostics(self.cfg)
            self._goto("diagnosing_sandbox")
            return
        self._goto("complete" if result.connected else "runtime_login_confirm")

    def _skip_runtime_login(self) -> None:
        self.runtime_auth_connected = False
        if not self.runtime_auth_summary:
            self.runtime_auth_summary = "not connected"
        self._goto("complete")

    def _run_runtime_logging_in(self) -> None:
        self._report("Opening provider sign-in…")
        result: _DelegatedAuthResult = _wiz._login_delegated_runtime(
            self.cfg,  # type: ignore[arg-type]
            self.execution_result,  # type: ignore[arg-type]
        )
        self.runtime_auth_connected = result.connected
        self.runtime_auth_summary = result.summary
        if result.connected and self.cfg is not None and self.cfg.execution.backend == "native":
            self.diagnostic_lines = self._collect_diagnostics(self.cfg)
            self._goto("diagnosing_sandbox")
        else:
            self._goto("complete")

    # --------------------------------------------------------- sandbox step

    def _run_diagnosing_sandbox(self) -> None:
        from ...sandbox_doctor import detect_bubblewrap_install_plan, diagnose_sandbox

        self._report("Checking sandbox readiness…")
        try:
            result = diagnose_sandbox(self.cfg, include_smoke=False)
        except Exception as exc:  # noqa: BLE001 - backends fail in environment-specific ways
            self.sandbox_result = _SandboxStepResult(ready=False, status="check failed")
            self._after_sandbox()
            self._set_status(f"Sandbox check failed: {exc}", "warn")
            return
        self._sandbox_diag = result
        if result.ready:
            self.sandbox_result = _SandboxStepResult(
                ready=True, status=result.selected_backend or result.status
            )
            self._after_sandbox()
            return
        if result.can_pull:
            self._goto("sandbox_pull_confirm")
            return
        self._sandbox_plan = detect_bubblewrap_install_plan()
        self._goto("sandbox_choice")

    def _choose_sandbox(self, value: str) -> None:
        if value == "install_bwrap":
            self._goto("installing_sandbox")
        elif value == "disable":
            self._goto("disabling_sandbox")
        else:
            self._sandbox_skip()

    def _sandbox_skip(self) -> None:
        self.sandbox_result = _SandboxStepResult(ready=False, status="not ready")
        self._after_sandbox()

    def _run_disabling_sandbox(self) -> None:
        from ...config import save_config
        from ...sandbox_settings import apply_sandbox_mode_to_config

        apply_sandbox_mode_to_config(self.cfg, "off")
        try:
            save_config(self.cfg)
            self.sandbox_result = _SandboxStepResult(ready=False, status="disabled")
        except (ConfigError, OSError) as exc:
            self.sandbox_result = _SandboxStepResult(ready=False, status="not ready")
            self._set_status(f"Could not save the sandbox setting: {exc}", "warn")
        self._after_sandbox()

    def _run_installing_sandbox(self) -> None:
        from ...sandbox_doctor import diagnose_sandbox, install_bubblewrap

        self._report("Installing bubblewrap…")
        install = install_bubblewrap(plan=self._sandbox_plan)
        if not install.ok:
            self.sandbox_result = _SandboxStepResult(ready=False, status="not ready")
            self._after_sandbox()
            self._set_status(f"Bubblewrap install did not complete: {install.detail}", "warn")
            return
        self._recheck_sandbox(diagnose_sandbox)

    def _run_pulling_sandbox(self) -> None:
        from ...sandbox_doctor import diagnose_sandbox, pull_sandbox_images

        self._report("Pulling sandbox image…")
        try:
            pull = pull_sandbox_images(timeout_s=900)
        except Exception as exc:  # noqa: BLE001
            self.sandbox_result = _SandboxStepResult(ready=False, status="pull failed")
            self._after_sandbox()
            self._set_status(f"Sandbox image pull failed: {exc}", "warn")
            return
        if not pull.ok:
            self.sandbox_result = _SandboxStepResult(ready=False, status="pull failed")
            self._after_sandbox()
            self._set_status(
                f"Sandbox image pull failed: {pull.error or 'image pull failed'}", "warn"
            )
            return
        self._recheck_sandbox(diagnose_sandbox)

    def _recheck_sandbox(self, diagnose_sandbox: Callable[..., Any]) -> None:
        try:
            result = diagnose_sandbox(self.cfg, include_smoke=True)
        except Exception as exc:  # noqa: BLE001
            self.sandbox_result = _SandboxStepResult(ready=False, status="check failed")
            self._after_sandbox()
            self._set_status(f"Sandbox check failed after setup: {exc}", "warn")
            return
        if result.ready:
            self.sandbox_result = _SandboxStepResult(
                ready=True, status=result.selected_backend or result.status
            )
        else:
            self.sandbox_result = _SandboxStepResult(ready=False, status="not ready")
        self._after_sandbox()

    def _after_sandbox(self) -> None:
        if self._hosted_alysis():
            self._goto("login_confirm")
        else:
            self._goto("complete")

    # ----------------------------------------------------------- login step

    def _run_logging_in(self) -> None:
        from ... import account_login

        self._report("Connecting…")
        captured: list[str] = []
        try:
            result = account_login.login(self.cfg, output_write=lambda m: captured.append(str(m)))
        except Exception as exc:  # noqa: BLE001 - login failures are non-fatal here
            # Non-fatal: setup is already saved; the summary row (in warn tone)
            # carries the message, so we don't also raise a redundant status line.
            self.login_ok = False
            self.login_summary = f"not connected ({exc}). Run `alysis login` later."
            self._goto("complete")
            return
        who = f" as {result.email}" if getattr(result, "email", None) else ""
        self.login_ok = True
        self.login_summary = f"connected{who} — Alysis Code account ready"
        self._goto("complete")

    # ----------------------------------------------------------- busy driver

    def run_busy(self) -> None:
        """Execute the current ``busy`` stage's blocking work (worker/terminal).

        Each handler transitions :attr:`stage` to the next screen before
        returning, so the application only needs to re-render afterwards.
        """
        handler = getattr(self, f"_run_{self.stage}", None)
        if handler is None:
            return
        handler()
