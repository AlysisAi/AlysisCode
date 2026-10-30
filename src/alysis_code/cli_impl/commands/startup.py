# ruff: noqa: F401,F403,F405,I001
# Legacy split module: dependencies are synced by cli_surface.py.
from __future__ import annotations

from ..execution_admission import (
    _apply_temperature_override,
    _SubscriptionAvailability,
    _subscription_availability,
    _provider_auth_ready_for_chat,
    _require_active_subscription_ready,
    _resolve_api_key_override,
)

from .cli_common import *
from .update import _cached_update_notice
from ...command_text import command_text
from ...provider_usage import session_provider_usage


def _is_narrow_terminal() -> bool:
    return _terminal_width() < 88


def _home_panel() -> Panel:
    lines = [
        "Quick actions:",
        "- 1) chat              interactive chat loop",
        "- 2) run               one-shot agent execution (you will be prompted for instruction)",
        "- 3) setup             first-run setup wizard",
        "- 4) doctor            environment checks",
        "- 5) plan              Forge plan command",
        "- 6) quit              exit home prompt",
        "- forge (in chat)    enter planning mode with /forge",
        "- forge show         summarize current plan artifacts",
    ]
    update_notice = _cached_update_notice()
    if update_notice:
        lines.append("")
        lines.append(update_notice)
    content = "\n".join(lines)
    return _Panel(content, title="Alysis Code Home", border_style="bright_black")


def _home_prompt_enabled() -> bool:
    raw = str(env_get("ALYSIS_HOME_PROMPT") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _run_default_chat_action(
    *,
    path: Path | None = None,
    allow_broad_workspace: bool = False,
    mode: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    temperature: float | None = None,
    stream: bool | None = None,
    max_steps: int | None = None,
    subagents: bool | None = None,
    no_log: bool = False,
    verify_cmd: list[str] | None = None,
    yes: bool = False,
) -> None:
    # Execution-posture flags (mode/yes/no_log/verify_cmd/…) default to the plain
    # launch values, but callers may forward the current session's flags so a
    # relaunch (e.g. /config → switch project) preserves the user's chosen posture
    # instead of silently resetting approval policy/mode/logging.
    _patchable("chat", chat)(
        path=path or Path("."),
        create_path=False,
        allow_broad_workspace=allow_broad_workspace,
        image=None,
        mode=mode,
        model=model,
        base_url=base_url,
        temperature=temperature,
        stream=stream,
        max_steps=max_steps,
        subagents=subagents,
        no_log=no_log,
        verify_cmd=verify_cmd,
        api_key_env=None,
        api_key_stdin=False,
        api_key=None,
        yes=yes,
        diagnostic_log=None,
    )


def _configured_default_workspace() -> Path | None:
    """The workspace folder setup persisted (``default_workspace_path``), if any."""
    try:
        cfg = _patchable("load_config", load_config)()
        raw = str((cfg.extra_fields or {}).get("default_workspace_path") or "").strip()
    except Exception:
        return None
    return Path(raw).expanduser() if raw else None


def _run_chat_after_setup() -> None:
    """Launch chat right after setup, in the workspace the user just configured.

    The broad-workspace consent was already given during setup (the wizard binds
    with ``allow_broad_workspace=True``), so we don't re-prompt the guard for that
    same folder. Falls back to the current directory when no workspace was saved.
    """
    workspace = _configured_default_workspace()
    _run_default_chat_action(path=workspace, allow_broad_workspace=workspace is not None)


def _maybe_run_startup_config_menu() -> None:
    cfg = _patchable("load_config", load_config)()
    execution = getattr(cfg, "execution", None)
    if str(getattr(execution, "backend", "native") or "native") == "delegated":
        runtime_id = str(getattr(execution, "runtime", None) or "").strip()
        if runtime_id and runtime_id in (getattr(cfg, "agent_runtimes", {}) or {}):
            return
        from ..config_menu import run_config_menu

        run_config_menu(cfg=cfg, auto_focus="execution")
        return
    api_key_present = bool(_patchable("resolve_api_key", resolve_api_key)().key)
    provider_auth_present = False
    try:
        from ...profiles import get_active_profile

        active_profile = get_active_profile(cfg)
        provider_auth_present = bool(active_profile.auth_provider)
    except Exception:
        provider_auth_present = False
    if provider_auth_present:
        return
    if cfg.model and (api_key_present or provider_auth_present):
        return
    from ..config_menu import run_config_menu

    focus = "api_key" if not api_key_present else "model"
    run_config_menu(cfg=cfg, auto_focus=focus)


# Persisted by setup (see setup_wizard._ONBOARDED_KEY) once first-run onboarding
# completes. The first-run gate keys on this marker rather than on "a model is
# configured", so a genuine first launch is routed to setup even when a config
# that already carries a model exists on disk (e.g. a pre-staged or partially
# written config) — that case used to fall straight through to chat and land the
# user on the guarded-workspace picker instead of setup.
_ONBOARDED_KEY = "onboarded"


def _is_onboarded() -> bool:
    """Whether the user has already completed first-run onboarding.

    True when the explicit ``onboarded`` marker is present. For configs that
    predate the marker we also treat a persisted ``default_workspace_path`` as an
    equivalent signal: that key is only ever written by a *completed* setup (or by
    explicitly choosing a default workspace in ``/config``), so existing users who
    already finished setup are never re-sent through it. A config that merely has a
    model set (but no completed setup) is intentionally *not* considered onboarded.
    """
    try:
        cfg = _patchable("load_config", load_config)()
    except Exception:
        return False
    extra = cfg.extra_fields or {}
    if extra.get(_ONBOARDED_KEY):
        return True
    return bool(str(extra.get("default_workspace_path") or "").strip())


def _should_run_first_run_setup_wizard() -> bool:
    return not _is_onboarded()


def _try_setup_tui(*, require_flag: bool = True, announce_fallback: bool = False) -> bool | None:
    """Run the alt-screen setup wizard (arrow-key selection screens).

    Returns ``True``/``False`` on a completed run (saved / cancelled), or
    ``None`` when the TUI could not run so the caller falls back to the classic
    Rich wizard. ``require_flag`` honors ``ALYSIS_TUI=0`` as an explicit
    opt-out; the explicit ``alysis setup`` command passes
    ``require_flag=False`` so it still shows the interactive screens. When
    ``announce_fallback`` is set, the reason for dropping to the classic wizard
    is printed (dim) so a launch failure is visible rather than silent.
    """
    reason: str | None = None
    try:
        from ..tui import is_tui_enabled
    except Exception as exc:  # noqa: BLE001 - the TUI is optional
        reason = f"the TUI module is unavailable ({exc})"
    else:
        if require_flag and not is_tui_enabled():
            reason = "ALYSIS_TUI is off"
        # ``_is_non_interactive_terminal`` is defined in this module (via the
        # cli_common surface); call it directly — there is no ``cli_impl.cli``.
        elif _is_non_interactive_terminal():
            reason = "this terminal is not interactive"

    if reason is None:
        try:
            from ..tui.setup_app import run_setup_tui

            return bool(run_setup_tui())
        except Exception as exc:  # noqa: BLE001 - any prompt_toolkit/terminal failure
            # app.run restored the terminal on its way out, so printing is safe.
            reason = f"the setup TUI failed to start ({exc})"

    if announce_fallback and reason:
        try:
            _console().print(f"[dim]Using the classic setup wizard — {reason}.[/dim]")
        except Exception:
            pass
    return None


def _maybe_run_first_run_setup_wizard() -> bool:
    if not _should_run_first_run_setup_wizard():
        return True
    tui_result = _try_setup_tui()
    if tui_result is True:
        return True
    if tui_result is False:
        _console().print("[yellow]Exiting without starting chat.[/yellow]")
        return False
    from ..setup_wizard import run_setup_wizard

    if run_setup_wizard():
        return True
    _console().print("[yellow]Exiting without starting chat.[/yellow]")
    return False


def _run_default_run_action(instruction: str) -> None:
    _patchable("run", run)(
        instruction=instruction,
        path=Path("."),
        create_path=False,
        allow_broad_workspace=False,
        image=None,
        mode=None,
        model=None,
        base_url=None,
        temperature=None,
        stream=None,
        max_steps=None,
        subagents=None,
        no_log=False,
        verify_cmd=None,
        api_key_env=None,
        api_key_stdin=False,
        api_key=None,
        yes=False,
        benchmark=False,
        deadline_seconds=None,
        require_deadline=False,
        diagnostic_log=None,
    )


def _current_branch_label(root: Path) -> str:
    if shutil.which("git") is None:
        return "-"
    proc = subprocess.run(
        ["git", "-C", os.fspath(root), "rev-parse", "--abbrev-ref", "HEAD"],
        check=False,
        capture_output=True,
        **command_text(),
    )
    if proc.returncode != 0:
        return "-"
    branch = proc.stdout.strip()
    return branch or "-"


def _is_git_dirty(root: Path) -> bool:
    if shutil.which("git") is None:
        return False
    proc = subprocess.run(
        ["git", "-C", os.fspath(root), "status", "--porcelain"],
        check=False,
        capture_output=True,
        **command_text(),
    )
    if proc.returncode != 0:
        return False
    return bool(proc.stdout.strip())


def _is_interactive_terminal() -> bool:
    return bool(sys.stdin.isatty() and sys.stdout.isatty())


def _is_non_interactive_terminal() -> bool:
    return not _is_interactive_terminal()


def _api_key_source_label(source: str) -> str:
    normalized = str(source or "").strip()
    if normalized == "env:ALYSIS_API_KEY":
        return "env (ALYSIS_API_KEY)"
    if normalized == "env:OPENAI_API_KEY":
        return "env (OPENAI_API_KEY)"
    if normalized.startswith("env:"):
        return f"env ({normalized.removeprefix('env:')})"
    if normalized.startswith("stored:profile="):
        return f"stored ({normalized.removeprefix('stored:profile=')})"
    if normalized in {"stored", "stored:legacy"}:
        return "stored"
    return "missing"


def _resolved_api_key_value() -> ApiKeyResolution:
    return resolve_api_key()


def _parse_bool_text(value: str) -> bool | None:
    v = value.strip().lower()
    if v in {"1", "true", "yes", "on"}:
        return True
    if v in {"0", "false", "no", "off"}:
        return False
    return None


def _safe_component(raw: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_-]", "_", str(raw).strip())
    return clean or "x"


def _resolve_usage_hud_default(cfg: AppConfig) -> bool:
    raw_env = env_get("ALYSIS_SHOW_USAGE_HUD")
    if raw_env is not None:
        parsed_env = _parse_bool_text(raw_env)
        if parsed_env is not None:
            return parsed_env
    raw_cfg = cfg.extra_fields.get("show_usage_hud")
    if isinstance(raw_cfg, bool):
        return raw_cfg
    if isinstance(raw_cfg, str):
        parsed_cfg = _parse_bool_text(raw_cfg)
        if parsed_cfg is not None:
            return parsed_cfg
    return True


def _chat_usage_hud_enabled(session: Any) -> bool:
    raw = getattr(session, "_usage_hud_enabled", True)
    return bool(raw)


def _set_chat_usage_hud_enabled(session: Any, enabled: bool) -> None:
    session._usage_hud_enabled = bool(enabled)


def _refresh_chat_hud_context_cache(session: Any) -> None:
    context_fn = getattr(session, "context_left", None)
    if callable(context_fn):
        try:
            session._hud_context_cache = context_fn()
        except Exception:  # noqa: BLE001
            session._hud_context_cache = None


def _chat_context_hud_value(session: Any) -> str:
    value = _chat_context_percent_value(session)
    if value is None:
        return "n/a"
    label = f"{value:.1f}%"
    warning_percent = _chat_effective_budget_percent_value(session)
    if warning_percent is None:
        warning_percent = value
    if warning_percent < 10.0:
        return f"{label} !!"
    if warning_percent < 25.0:
        return f"{label} !"
    return label


def _format_cost_with_unknown(
    *,
    known_cost: float | None,
    unknown_calls: int,
    style: str,
) -> str:
    base = format_usd(known_cost, style=style)
    if unknown_calls <= 0:
        return base
    return f"{base} (+{unknown_calls} unknown)"


def _known_cost_value(data: dict[str, Any]) -> float | None:
    if int(data.get("known_cost_calls") or 0) <= 0:
        return None
    raw = data.get("cost_usd")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _format_exact_token_count(value: Any) -> str:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        parsed = 0
    return f"{max(0, parsed):,}"


def _format_compact_token_count(value: Any) -> str:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        parsed = 0
    count = max(0, parsed)
    if count < 10_000:
        return f"{count:,}"
    if count < 1_000_000:
        return f"{count / 1_000:.1f}k"
    return f"{count / 1_000_000:.1f}M"


def _chat_context_percent_value(session: Any) -> float | None:
    ctx = getattr(session, "_hud_context_cache", None)
    if ctx is None:
        return None
    reading = session_provider_usage(session)
    if reading is None or reading["input_scope"] != "request":
        return None
    used = reading["input_tokens"]
    capacity = getattr(ctx, "context_window_tokens", None)
    if used is None or not isinstance(capacity, int) or capacity <= 0:
        return None
    return max(0.0, min(100.0, (capacity - used) / capacity * 100))


def _chat_provider_usage_rows(session: Any) -> list[tuple[str, str, str]]:
    reading = session_provider_usage(session)
    if reading is None:
        return [("status", "Awaiting provider usage", "dim")]
    rows = [
        ("model", reading["model"], "plain"),
        ("measured at", reading["measured_at"], "dim"),
        ("scope", reading["input_scope"], "dim"),
    ]
    for label, field in (
        ("input (includes cache)", "input_tokens"),
        ("cache read", "cached_tokens"),
        ("cache write", "cache_write_tokens"),
        ("uncached" + (" (derived)" if reading["uncached_derived"] else ""), "uncached_tokens"),
        ("output (includes reasoning)", "output_tokens"),
        ("reasoning", "reasoning_tokens"),
        ("total", "total_tokens"),
    ):
        count = reading[field]
        rows.append((label, f"{count:,}" if count is not None else "Not reported", "plain"))
    for name, count in reading.get("input_breakdown", {}).items():
        rows.append((f"input / {name} (provider)", f"{count:,}", "plain"))
    pct = _chat_context_percent_value(session)
    if pct is not None:
        rows.append(("window left % (last request)", f"{pct:.1f}%", "plain"))
    return rows


def _chat_effective_budget_percent_value(session: Any) -> float | None:
    ctx = getattr(session, "_hud_context_cache", None)
    if ctx is None:
        return None
    percent = getattr(ctx, "effective_percent_left", None)
    if percent is None:
        percent = getattr(ctx, "percent_left", None)
    if percent is None:
        return None
    try:
        return float(percent)
    except (TypeError, ValueError):
        return None


def _format_chat_context_percent(percent: float | None) -> str:
    if percent is None:
        return "n/a"
    return f"{percent:.1f}%"


def _chat_usage_warning_line(percent: float | None) -> str | None:
    if percent is None:
        return None
    if percent < 10.0:
        return "\u26a0\ufe0e  Critical input budget — run /compact to reduce context"
    if percent < 20.0:
        return "\u26a0\ufe0e  Low input budget — run /compact to reduce context"
    return None


def _chat_turn_usage_style(session: Any) -> str:
    percent = _chat_effective_budget_percent_value(session)
    if percent is None:
        return "dim"
    if percent < 10.0:
        return "bold red"
    if percent < 20.0:
        return "yellow"
    if percent < 50.0:
        return STYLE_CONTENT
    return "dim"


def _format_usage_billing_for_display(row: dict[str, Any], *, compact: bool = False) -> str:
    """Render charge provenance without presenting estimates as exact bills."""

    known_cost = _known_cost_value(row)
    unknown_calls = int(row.get("unknown_cost_calls", row.get("unknown_cost_count", 0)) or 0)
    estimate_calls = int(row.get("catalog_estimated_cost_calls") or 0)
    subscription_calls = int(row.get("subscription_calls") or 0)
    included_calls = int(row.get("included_calls") or 0)
    local_calls = int(row.get("local_calls") or 0)

    if known_cost is not None:
        value = format_usd(known_cost, style="table")
        if estimate_calls > 0:
            value = f"~{value}"
        suffixes: list[str] = []
        if unknown_calls > 0:
            suffixes.append(f"{unknown_calls} cost unknown")
        if subscription_calls > 0:
            suffixes.append("subscription")
        if included_calls > 0:
            suffixes.append("included")
        if local_calls > 0:
            suffixes.append("local")
        if suffixes:
            joiner = "+" if compact else ", "
            value = f"{value} {joiner.join(suffixes)}"
        return value

    labels = [
        label
        for count, label in (
            (subscription_calls, "subscription"),
            (included_calls, "included"),
            (local_calls, "local"),
        )
        if count > 0
    ]
    if unknown_calls > 0:
        labels.append("cost n/a" if compact else f"unknown ({unknown_calls} cost unknown)")
    if labels:
        return ("+" if compact else ", ").join(labels)
    return "$0.0000"


def _format_reported_token_total(
    row: dict[str, Any],
    *,
    token_key: str,
    reported_calls_key: str,
    derived_calls_key: str | None = None,
) -> str:
    """Keep provider omission, a provider-reported zero, and a derived figure apart.

    A derived figure is arithmetic over counts the provider did send, so it is
    worth showing — but it is not itself reported, and labelling it as such
    would overstate what the provider actually told us.
    """

    total_calls = int(row.get("api_usage_calls") or 0) + int(row.get("estimate_usage_calls") or 0)
    reported_calls = int(row.get(reported_calls_key) or 0)
    derived_calls = int(row.get(derived_calls_key) or 0) if derived_calls_key else 0
    known_calls = reported_calls + derived_calls
    if total_calls <= 0 or known_calls <= 0:
        return "not reported"
    value = _format_exact_token_count(row.get(token_key))
    if reported_calls <= 0:
        suffix = " (derived)"
    elif derived_calls > 0:
        suffix = " (partly derived)"
    else:
        suffix = ""
    if known_calls < total_calls:
        return f"{value} (partial {known_calls}/{total_calls}){suffix}"
    return f"{value}{suffix}"


def _format_usage_source_for_display(row: dict[str, Any]) -> str:
    provider_calls = int(row.get("api_usage_calls") or 0)
    fallback_calls = int(row.get("estimate_usage_calls") or 0)
    corrected_calls = int(row.get("corrected_usage_calls") or 0)
    parts = [f"provider {provider_calls}", f"fallback {fallback_calls}"]
    if corrected_calls > 0:
        parts.append(f"corrected {corrected_calls}")
    return " | ".join(parts)


def _resolve_chat_mode_alias(value: str) -> str | None:
    key = value.strip().lower()
    return _CHAT_MODE_ALIASES.get(key)


def _resolve_trace_level(value: str) -> str | None:
    normalized = value.strip().lower()
    if normalized in _CHAT_TRACE_LEVELS:
        return normalized
    return None


def _chat_trace_level(session: Any) -> str:
    surface = getattr(session, "surface", None)
    level = getattr(surface, "trace_level", None)
    if isinstance(level, str):
        normalized = level.strip().lower()
        if normalized in _CHAT_TRACE_LEVELS:
            return normalized
    return "compact"


def _record_chat_session_setting(*, session: Any, setting: str, value: Any) -> None:
    store = getattr(session, "store", None)
    append = getattr(store, "append", None)
    if not callable(append):
        return
    try:
        append(
            "session_setting_changed",
            {"setting": str(setting), "value": value},
        )
    except Exception:
        pass


def _set_chat_stream_enabled(*, session: Any, enabled: bool) -> bool:
    next_value = bool(enabled)
    previous = bool(getattr(session, "stream", True))
    session.stream = next_value
    cfg = getattr(session, "cfg", None)
    if cfg is not None:
        cfg.stream = next_value
    if next_value != previous:
        _record_chat_session_setting(
            session=session,
            setting="stream",
            value=next_value,
        )
    return next_value


def _session_skill_listing(session: Any) -> tuple[bool, tuple[Any, ...], tuple[Any, ...]]:
    cfg = getattr(session, "cfg", None)
    enabled = resolve_skills_enabled(cfg) if isinstance(cfg, AppConfig) else True
    ordered_obj = getattr(session, "skills_ordered", None)
    ordered = (
        tuple(item for item in ordered_obj if item is not None)
        if isinstance(ordered_obj, tuple)
        else tuple(item for item in (ordered_obj or []) if item is not None)
    )
    registry_obj = getattr(session, "skill_registry", None)
    if ordered or isinstance(registry_obj, dict):
        issues = tuple(getattr(session, "skill_discovery_issues", ()) or ())
        return enabled, ordered, issues
    discovered = _discover_skills_for_path(path=Path(getattr(session, "root", Path("."))))
    return enabled, tuple(discovered.ordered), tuple(discovered.issues)


def _set_chat_trace_level(*, session: Any, level: str) -> str:
    normalized = _resolve_trace_level(level) or "compact"
    previous = _chat_trace_level(session)
    surface = getattr(session, "surface", None)
    if surface is None:
        return normalized
    setter = getattr(surface, "set_trace_level", None)
    if callable(setter):
        applied = setter(normalized)
        if isinstance(applied, str):
            applied_normalized = _resolve_trace_level(applied)
            if applied_normalized is not None:
                if applied_normalized != previous:
                    _record_chat_session_setting(
                        session=session,
                        setting="trace_level",
                        value=applied_normalized,
                    )
                return applied_normalized
        return normalized
    try:
        surface.trace_level = normalized
    except Exception:  # noqa: BLE001
        return normalized
    applied = _resolve_trace_level(str(getattr(surface, "trace_level", normalized))) or normalized
    if applied != previous:
        _record_chat_session_setting(
            session=session,
            setting="trace_level",
            value=applied,
        )
    return applied


def _emit_forge_planner_trace(
    *,
    session: Any,
    message: str,
    full_only: bool = False,
    source: str = "forge_planner",
) -> None:
    clean = message.strip()
    if not clean:
        return
    trace_level = _chat_trace_level(session)
    if trace_level == "off":
        return
    if full_only and trace_level != "full":
        return

    store = getattr(session, "store", None)
    append = getattr(store, "append", None)
    if callable(append):
        try:
            append("progress", {"message": clean, "source": source})
        except Exception:  # noqa: BLE001
            pass

    surface = getattr(session, "surface", None)
    handler = getattr(surface, "on_progress_update", None)
    if callable(handler):
        try:
            handler(clean)
        except Exception:  # noqa: BLE001
            return


def _make_forge_swarm_trace_sink(
    *,
    session: Any,
    paths: Any,
    console: Console,
) -> SerializedSwarmTraceSink:
    surface = getattr(session, "surface", None)
    store = getattr(session, "store", None)
    return SerializedSwarmTraceSink(
        artifact_path=Path(paths.execution_dir) / "trace" / "swarm_trace.jsonl",
        trace_level=_chat_trace_level(session),
        surface=surface,
        session_store=store,
        console=console,
        store_source="forge_swarm",
    )


def _make_forge_planner_delta_trace_callback(*, session: Any) -> Callable[[str], None] | None:
    trace_level = _chat_trace_level(session)
    if trace_level == "off":
        return None

    seen_chars = 0
    started = False
    last_full_bucket = -1

    def _on_text_delta(delta: str) -> None:
        nonlocal seen_chars, started, last_full_bucket
        if not delta:
            return
        seen_chars += len(delta)
        if not started:
            _emit_forge_planner_trace(session=session, message="Receiving planner output...")
            started = True
        if trace_level != "full":
            return
        bucket = seen_chars // 320
        if bucket <= last_full_bucket:
            return
        last_full_bucket = bucket
        _emit_forge_planner_trace(
            session=session,
            message=f"Planner draft progress: ~{seen_chars} chars captured.",
            full_only=True,
        )

    return _on_text_delta


__all__ = [name for name in globals() if (not name.startswith("__") or name == "__version__")]
