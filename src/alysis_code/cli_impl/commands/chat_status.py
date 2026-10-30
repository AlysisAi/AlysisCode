# ruff: noqa: F401,F403,F405,I001
# Legacy split module: dependencies are synced by cli_surface.py.
from __future__ import annotations

from .cli_common import *
from .update import _cached_update_status_summary
from ...config import resolve_web_search_policy
from ...personas import normalize_persona, persona_modes_enabled
from ...llm_error_display import classify_llm_error_display, friendly_llm_error_message
from ...llm.protocols import OPENAI_COMPAT_PROTOCOL, get_provider_protocol_capabilities
from ...profiles import get_active_profile, resolve_effective_base_url
from ...provider_telemetry import last_provider_call_summary, last_web_search_summary
from ...step_budget import (
    DEFAULT_CHAT_MAX_STEPS,
    DEFAULT_SUBAGENT_MAX_STEPS,
    DEFAULT_TASK_MAX_STEPS,
    normalize_step_budget_policy,
)


_CHAT_MODE_STATUS_LABELS = {
    "review": "safe (review)",
    "auto": "fast (auto)",
    "readonly": "read (readonly)",
    "fullaccess": "full (fullaccess)",
}


def _chat_mode_status_label(mode: Any) -> str:
    normalized = str(mode or "").strip().lower()
    return _CHAT_MODE_STATUS_LABELS.get(normalized, normalized or "unknown")


def _session_execution_policy(session: Any) -> str:
    return normalize_step_budget_policy(
        getattr(
            session,
            "step_budget_policy",
            getattr(getattr(session, "cfg", None), "step_budget_policy", "autonomous"),
        )
    )


def _session_step_limit_label(session: Any, *, scope: str) -> str:
    policy = _session_execution_policy(session)
    if scope == "chat":
        explicit = getattr(session, "chat_turn_fixed_override", None)
        if isinstance(explicit, int) and explicit > 0:
            return str(explicit)
    if policy == "autonomous":
        return "unlimited"
    field = {
        "chat": "max_steps",
        "task": "task_max_steps",
        "subagent": "subagent_max_steps",
    }[scope]
    default = {
        "chat": DEFAULT_CHAT_MAX_STEPS,
        "task": DEFAULT_TASK_MAX_STEPS,
        "subagent": DEFAULT_SUBAGENT_MAX_STEPS,
    }[scope]
    return str(getattr(session, field, getattr(getattr(session, "cfg", None), field, default)))


def _active_step_limit_label(session: Any) -> str:
    runtime = getattr(session, "step_budget_runtime", None)
    active = getattr(runtime, "active_turn_budget", None)
    if isinstance(active, int) and active > 0:
        return str(active)
    resolution = getattr(runtime, "last_resolution", None)
    if resolution is not None and bool(getattr(resolution, "unlimited", False)):
        return "unlimited"
    resolved = getattr(resolution, "resolved_max_steps", None)
    if isinstance(resolved, int) and resolved > 0:
        return str(resolved)
    return _session_step_limit_label(session, scope="chat")


def _pending_operation_labels(session: Any) -> list[str]:
    provider = getattr(session, "pending_operation_labels", None)
    if not callable(provider):
        return []
    try:
        labels = provider()
    except Exception:  # noqa: BLE001 - status inspection must stay available
        return []
    if not isinstance(labels, list | tuple):
        return []
    return [label for item in labels if (label := str(item or "").strip())]


def _print_chat_status(
    *,
    console: Console,
    session: Any,
    pending_images: list[str],
) -> None:
    model = getattr(getattr(session, "client", None), "model", "?")
    temperature = getattr(getattr(session, "client", None), "temperature", "?")
    stream = bool(getattr(session, "stream", False))
    mode = getattr(session, "mode", "?")
    root = Path(getattr(session, "root", Path(".")))
    branch = _current_branch_label(root)
    cfg_obj = getattr(session, "cfg", None)
    resolved_cfg = cfg_obj if isinstance(cfg_obj, AppConfig) else None
    session_api_key = (
        str(getattr(getattr(session, "client", None), "api_key", "") or "").strip() or None
    )
    web_search_status = resolve_web_search_runtime_status(
        cfg=resolved_cfg,
        api_key=session_api_key,
    )
    active_protocol, streaming_supported = _active_protocol_status(
        cfg=resolved_cfg,
        client=getattr(session, "client", None),
    )
    table = _Table(title="Chat Status")
    table.add_column("setting")
    table.add_column("value", overflow="fold")
    table.add_row("mode", _chat_mode_status_label(mode))
    if persona_modes_enabled(getattr(session, "cfg", None)):
        table.add_row(
            "persona",
            normalize_persona(
                getattr(session, "persona", "code"),
                getattr(session, "persona_registry", None),
            ),
        )
    table.add_row("trace", _chat_trace_level(session))
    table.add_row("model", str(model))
    table.add_row("protocol", active_protocol)
    table.add_row(
        "api_key_source",
        _api_key_source_label(str(getattr(session, "api_key_source", "missing") or "missing")),
    )
    table.add_row("temperature", str(temperature))
    table.add_row("stream", "on" if stream else "off")
    table.add_row("streaming_supported", streaming_supported)
    table.add_row(
        "subagents",
        "on" if bool(getattr(session, "subagents_enabled", False)) else "off",
    )
    table.add_row(
        "execution_policy",
        _session_execution_policy(session),
    )
    table.add_row("chat_step_limit", _session_step_limit_label(session, scope="chat"))
    table.add_row("task_step_limit", _session_step_limit_label(session, scope="task"))
    table.add_row("subagent_step_limit", _session_step_limit_label(session, scope="subagent"))
    table.add_row("active_step_limit", _active_step_limit_label(session))
    table.add_row("session", str(getattr(getattr(session, "store", None), "session_id", "-")))
    active_workdir = Path(resolve_session_active_workdir_path(session))
    focus_dir = Path(getattr(session, "focus_dir", root) or root)
    focus_relpath = str(getattr(session, "focus_relpath", ".") or ".")
    active_workdir_relpath = str(resolve_session_active_workdir_relpath(session) or ".")
    table.add_row("workspace_root", os.fspath(root))
    table.add_row("focus_dir", os.fspath(focus_dir))
    table.add_row("focus_relpath", focus_relpath)
    table.add_row("active_workdir", os.fspath(active_workdir))
    table.add_row("active_workdir_relpath", active_workdir_relpath)
    table.add_row("branch", branch)
    table.add_row("dirty", "yes" if _is_git_dirty(root) else "no")
    table.add_row("update", _cached_update_status_summary(resolved_cfg))
    table.add_row("task", "-")
    table.add_row("queued_images", str(len(pending_images)))
    for index, label in enumerate(_pending_operation_labels(session), start=1):
        table.add_row(f"pending_{index}", label)
    web_search_policy = resolve_web_search_policy(resolved_cfg)
    web_search_ready = web_search_status.registration_ready and web_search_policy != "off"
    web_search_label = (
        "policy-disabled" if web_search_policy == "off" else web_search_status.availability_label
    )
    table.add_row("web_search", web_search_label)
    table.add_row("web_search_policy", web_search_policy)
    table.add_row("web_search_mode", web_search_status.mode)
    table.add_row("web_search_provider", web_search_status.provider or "(none)")
    table.add_row(
        "web_search_registration",
        "yes" if web_search_ready else "no",
    )
    if web_search_status.provider not in {None, "tavily"}:
        table.add_row("web_search_base_url", web_search_status.base_url or "(missing)")
        table.add_row("web_search_model", web_search_status.model or "(missing)")
    table.add_row(
        "web_search_api_key",
        "available" if web_search_status.api_key_available else "missing",
    )
    table.add_row("web_search_note", web_search_status.summary)
    table.add_row(
        "web_search_setup",
        (
            "Set `alysis config set web_search_policy auto` to expose search to the model."
            if web_search_policy == "off"
            else web_search_status.setup_hint
        ),
    )
    last_call = last_provider_call_summary()
    if last_call:
        table.add_row(
            "last_provider_call",
            (
                f"{last_call.get('provider_key') or '?'} / "
                f"{last_call.get('protocol') or '?'} / "
                f"{last_call.get('status_category') or '?'} / "
                f"{last_call.get('latency_ms') or 0}ms"
            ),
        )
        stream_summary = last_call.get("streaming") if isinstance(last_call, dict) else None
        if isinstance(stream_summary, dict):
            table.add_row(
                "last_stream",
                (
                    f"events={stream_summary.get('event_count') or 0}, "
                    f"deltas={stream_summary.get('text_delta_count') or 0}, "
                    f"first_token_ms={stream_summary.get('first_token_latency_ms')}"
                ),
            )
    last_search = last_web_search_summary()
    if last_search:
        table.add_row(
            "last_web_search",
            (
                f"{last_search.get('web_search_mode') or '?'} / "
                f"{last_search.get('web_search_adapter') or '?'} / "
                f"sources={last_search.get('source_count') or 0}"
            ),
        )
    registry = getattr(session, "model_registry", None)
    resolved_model_name = str(model).strip()
    if registry is not None and resolved_model_name:
        try:
            meta = registry.get(resolved_model_name)
        except Exception as e:  # noqa: BLE001
            table.add_row("model_metadata_source", "error")
            table.add_row("model_metadata_error", str(e))
        else:
            table.add_row("model_metadata_source", str(getattr(meta, "source", "unknown")))
            table.add_row(
                "model_metadata_error",
                str(getattr(registry, "last_error", None) or "-"),
            )
            table.add_row(
                "model_metadata_warning",
                "; ".join(getattr(meta, "warnings", ())[:3]) or "-",
            )
            field_sources = getattr(meta, "field_sources", {}) or {}
            table.add_row(
                "context_window_source",
                str(field_sources.get("context_window_tokens", "unknown")),
            )
            table.add_row(
                "max_output_source",
                str(field_sources.get("max_output_tokens", "unknown")),
            )
            for key, value in _bundled_catalog_provenance_rows(meta=meta, registry=registry):
                table.add_row(key, value)
    console.print(table)


def _active_protocol_status(*, cfg: AppConfig | None, client: Any) -> tuple[str, str]:
    protocol = str(getattr(client, "protocol", "") or "").strip()
    provider_key = str(getattr(client, "provider_key", "") or "").strip()
    if cfg is not None:
        try:
            profile = get_active_profile(cfg)
            protocol = protocol or str(profile.protocol or OPENAI_COMPAT_PROTOCOL).strip()
            provider_key = provider_key or str(profile.name or "").strip()
            base_url = resolve_effective_base_url(cfg=cfg, profile=profile)
        except Exception:  # noqa: BLE001
            base_url = str(getattr(client, "base_url", "") or "")
    else:
        base_url = str(getattr(client, "base_url", "") or "")
    protocol = protocol or OPENAI_COMPAT_PROTOCOL
    capabilities = get_provider_protocol_capabilities(
        provider_key=provider_key,
        protocol=protocol,
    )
    if capabilities is not None:
        supported = "yes" if capabilities.supports_streaming else "no"
    else:
        supported = "yes" if protocol == OPENAI_COMPAT_PROTOCOL else "unknown"
    if not provider_key and base_url:
        provider_key = base_url
    return protocol, supported


def _chat_status_panel_spec(*, session: Any, pending_images: list[str]) -> dict[str, Any]:
    """Build the structured spec for the TUI ``/status`` popup.

    Mirrors :func:`_print_chat_status` (same getters) but returns grouped
    key/value sections with a health ``tone`` per value instead of a flat Rich
    table, so the full-screen TUI can render it as a centered panel. ``tone`` is
    one of "accent" (on/healthy, green), "plain", "warn", "err", "dim". Green is
    the only accent; degraded states stay "plain", real failures use "err".
    """

    def tone(flag: bool, true_tone: str = "accent", false_tone: str = "plain") -> str:
        return true_tone if flag else false_tone

    model = getattr(getattr(session, "client", None), "model", "?")
    temperature = getattr(getattr(session, "client", None), "temperature", "?")
    stream = bool(getattr(session, "stream", False))
    mode = str(getattr(session, "mode", "?"))
    pending_mode = str(getattr(session, "pending_permissions_mode", "") or "").strip()
    mode_display = _chat_mode_status_label(mode)
    if pending_mode and pending_mode != mode:
        mode_display = f"{mode_display} → {_chat_mode_status_label(pending_mode)} next"
    elif pending_mode:
        mode_display = f"{mode_display} (selected as next base)"
    root = Path(getattr(session, "root", Path(".")))
    branch = _current_branch_label(root)
    dirty = _is_git_dirty(root)
    subagents = bool(getattr(session, "subagents_enabled", False))
    cfg_obj = getattr(session, "cfg", None)
    resolved_cfg = cfg_obj if isinstance(cfg_obj, AppConfig) else None
    session_api_key = (
        str(getattr(getattr(session, "client", None), "api_key", "") or "").strip() or None
    )
    web = resolve_web_search_runtime_status(cfg=resolved_cfg, api_key=session_api_key)

    def _attr(name: str, default: Any) -> Any:
        return getattr(session, name, getattr(getattr(session, "cfg", None), name, default))

    session_rows: list[tuple[str, str, str]] = [
        ("mode", mode_display, "accent"),
        *(
            [
                (
                    "persona",
                    normalize_persona(
                        getattr(session, "persona", "code"),
                        getattr(session, "persona_registry", None),
                    ),
                    "accent",
                )
            ]
            if persona_modes_enabled(getattr(session, "cfg", None))
            else []
        ),
        ("model", str(model), "accent"),
        ("trace", _chat_trace_level(session), "plain"),
        ("session", str(getattr(getattr(session, "store", None), "session_id", "-")), "dim"),
        ("branch", branch, "plain"),
        ("dirty", "yes" if dirty else "no", tone(not dirty)),
        ("update", _cached_update_status_summary(resolved_cfg), "plain"),
    ]

    model_rows: list[tuple[str, str, str]] = [
        (
            "api_key_source",
            _api_key_source_label(str(getattr(session, "api_key_source", "missing") or "missing")),
            "plain",
        ),
        ("temperature", str(temperature), "plain"),
        ("stream", "on" if stream else "off", tone(stream)),
        ("subagents", "on" if subagents else "off", tone(subagents)),
        ("execution_policy", _session_execution_policy(session), "plain"),
        ("chat_step_limit", _session_step_limit_label(session, scope="chat"), "plain"),
        ("task_step_limit", _session_step_limit_label(session, scope="task"), "plain"),
        (
            "subagent_step_limit",
            _session_step_limit_label(session, scope="subagent"),
            "plain",
        ),
    ]
    model_rows.append(
        (
            "active_step_limit",
            _active_step_limit_label(session),
            "plain",
        )
    )

    active_workdir = Path(resolve_session_active_workdir_path(session))
    focus_dir = Path(getattr(session, "focus_dir", root) or root)
    workspace_rows: list[tuple[str, str, str]] = [
        ("workspace_root", os.fspath(root), "plain"),
        ("focus_dir", os.fspath(focus_dir), "plain"),
        ("focus_relpath", str(getattr(session, "focus_relpath", ".") or "."), "plain"),
        ("active_workdir", os.fspath(active_workdir), "accent"),
        (
            "active_workdir_relpath",
            str(resolve_session_active_workdir_relpath(session) or "."),
            "plain",
        ),
        ("queued_images", str(len(pending_images)), "plain"),
    ]

    web_policy = resolve_web_search_policy(resolved_cfg)
    web_ready = web.registration_ready and web_policy != "off"
    web_label = "policy-disabled" if web_policy == "off" else web.availability_label
    web_unavailable = not web_ready
    web_rows: list[tuple[str, str, str]] = [
        (
            "status",
            web_label,
            "err" if web_unavailable else tone(web_ready),
        ),
        ("policy", web_policy, "plain"),
        ("mode", web.mode, "plain"),
        ("provider", web.provider or "(none)", "plain"),
        ("registration", "yes" if web_ready else "no", tone(web_ready)),
    ]
    if web.provider not in {None, "tavily"}:
        web_rows.append(("base_url", web.base_url or "(missing)", "plain"))
        web_rows.append(("model", web.model or "(missing)", "plain"))
    web_rows.append(
        (
            "api_key",
            "available" if web.api_key_available else "missing",
            tone(web.api_key_available),
        )
    )
    web_rows.append(("note", web.summary, "plain"))
    web_rows.append(
        (
            "setup",
            (
                "Set `alysis config set web_search_policy auto` to expose search to the model."
                if web_policy == "off"
                else web.setup_hint
            ),
            "plain",
        )
    )

    sections: list[tuple[str, list[tuple[str, str, str]]]] = [
        ("Session", session_rows),
    ]
    pending_labels = _pending_operation_labels(session)
    if pending_labels:
        sections.append(
            (
                "Pending operations",
                [
                    (str(index), label, "warn")
                    for index, label in enumerate(pending_labels, start=1)
                ],
            )
        )
    sections.extend(
        [
            ("Model", model_rows),
            ("Workspace", workspace_rows),
            ("Web search", web_rows),
        ]
    )

    registry = getattr(session, "model_registry", None)
    resolved_model_name = str(model).strip()
    if registry is not None and resolved_model_name:
        meta_rows: list[tuple[str, str, str]] = []
        try:
            meta = registry.get(resolved_model_name)
        except Exception as e:  # noqa: BLE001
            meta_rows.append(("source", "error", "err"))
            meta_rows.append(("error", str(e), "err"))
        else:
            meta_rows.append(("source", str(getattr(meta, "source", "unknown")), "plain"))
            last_error = str(getattr(registry, "last_error", None) or "-")
            meta_rows.append(("error", last_error, "err" if last_error != "-" else "plain"))
            warning = "; ".join(getattr(meta, "warnings", ())[:3]) or "-"
            meta_rows.append(("warning", warning, "warn" if warning != "-" else "plain"))
            field_sources = getattr(meta, "field_sources", {}) or {}
            meta_rows.append(
                (
                    "context_window_source",
                    str(field_sources.get("context_window_tokens", "unknown")),
                    "plain",
                )
            )
            meta_rows.append(
                (
                    "max_output_source",
                    str(field_sources.get("max_output_tokens", "unknown")),
                    "plain",
                )
            )
            for key, value in _bundled_catalog_provenance_rows(meta=meta, registry=registry):
                meta_rows.append((key, value, "dim"))
        sections.append(("Model metadata", meta_rows))

    return {"title": "Session Status", "sections": sections}


def _print_chat_pwd(*, console: Console, session: Any) -> None:
    root = Path(getattr(session, "root", Path("."))).resolve()
    focus_dir = Path(getattr(session, "focus_dir", root) or root)
    focus_relpath = str(getattr(session, "focus_relpath", ".") or ".")
    active_workdir = Path(resolve_session_active_workdir_path(session))
    active_workdir_relpath = str(resolve_session_active_workdir_relpath(session) or ".")
    console.print(f"active_workdir: {active_workdir}")
    console.print(f"active_workdir_relpath: {active_workdir_relpath}")
    console.print(f"focus_dir: {focus_dir}")
    console.print(f"focus_relpath: {focus_relpath}")
    console.print(f"workspace_root: {root}")


def _model_metadata_uses_bundled_catalog(*, meta: Any, registry: Any) -> bool:
    if str(getattr(meta, "source", "")).strip() == BUNDLED_MODEL_CATALOG_SOURCE:
        return True
    field_sources = getattr(meta, "field_sources", {}) or {}
    if any(
        str(source).strip() == BUNDLED_MODEL_CATALOG_SOURCE for source in field_sources.values()
    ):
        return True
    last_error = str(getattr(registry, "last_error", "") or "").strip().casefold()
    return "bundled model catalog" in last_error


def _bundled_catalog_provenance_rows(*, meta: Any, registry: Any) -> list[tuple[str, str]]:
    if not _model_metadata_uses_bundled_catalog(meta=meta, registry=registry):
        return []
    provenance = _patchable(
        "get_bundled_model_catalog_provenance",
        get_bundled_model_catalog_provenance,
    )()
    if provenance.error is not None:
        return [("bundled_catalog_provenance", provenance.error)]
    rows: list[tuple[str, str]] = []
    if provenance.upstream_commit_sha is not None:
        rows.append(("bundled_catalog_commit", provenance.upstream_commit_sha))
    if provenance.fetched_at_utc is not None:
        rows.append(("bundled_catalog_fetched_at", provenance.fetched_at_utc))
    return rows


def _print_chat_usage(*, console: Console, session: Any) -> None:
    summary = getattr(session, "usage_summary", None)
    if summary is None:
        console.print("Usage tracking unavailable for this session.")
        return
    rows = summary.by_model_rows()
    if not rows:
        console.print("No usage events yet in this session.")
        return
    table = _Table(title="Usage")
    table.add_column("model")
    table.add_column("input total", justify="right")
    table.add_column("uncached", justify="right")
    table.add_column("cache read", justify="right")
    table.add_column("cache write", justify="right")
    table.add_column("output", justify="right")
    table.add_column("total", justify="right")
    table.add_column("cost", justify="right")
    table.add_column("source", justify="right")
    for row in rows:
        cost_display = _format_usage_billing_for_display(row)
        table.add_row(
            str(row.get("model") or "-"),
            _format_exact_token_count(row.get("prompt_tokens")),
            _format_reported_token_total(
                row,
                token_key="input_tokens_uncached",
                reported_calls_key="input_tokens_uncached_reported_calls",
                derived_calls_key="input_tokens_uncached_derived_calls",
            ),
            _format_reported_token_total(
                row,
                token_key="cache_read_input_tokens",
                reported_calls_key="cache_read_reported_calls",
            ),
            _format_reported_token_total(
                row,
                token_key="cache_creation_input_tokens",
                reported_calls_key="cache_creation_reported_calls",
            ),
            _format_exact_token_count(row.get("completion_tokens")),
            _format_exact_token_count(row.get("total_tokens")),
            cost_display,
            _format_usage_source_for_display(row),
        )
    totals = summary.totals()
    total_cost = _format_usage_billing_for_display(totals)
    unknown_total = int(totals.get("unknown_cost_calls") or 0)
    table.add_row(
        "TOTAL",
        _format_exact_token_count(totals.get("prompt_tokens")),
        _format_reported_token_total(
            totals,
            token_key="input_tokens_uncached",
            reported_calls_key="input_tokens_uncached_reported_calls",
            derived_calls_key="input_tokens_uncached_derived_calls",
        ),
        _format_reported_token_total(
            totals,
            token_key="cache_read_input_tokens",
            reported_calls_key="cache_read_reported_calls",
        ),
        _format_reported_token_total(
            totals,
            token_key="cache_creation_input_tokens",
            reported_calls_key="cache_creation_reported_calls",
        ),
        _format_exact_token_count(totals.get("completion_tokens")),
        _format_exact_token_count(totals.get("total_tokens")),
        total_cost,
        _format_usage_source_for_display(totals),
    )
    console.print(table)
    cache_efficiency = totals.get("cache_efficiency")
    if isinstance(cache_efficiency, dict) and cache_efficiency.get("hit_ratio") is not None:
        largest = cache_efficiency.get("largest_uncached_call")
        largest_text = ""
        if isinstance(largest, dict):
            largest_text = (
                "; largest uncached "
                f"{_format_exact_token_count(largest.get('uncached_prompt_tokens'))} "
                f"at call {int(largest.get('call_position') or 0)}"
            )
        console.print(
            "[dim]Cache efficiency: "
            f"{float(cache_efficiency['hit_ratio']) * 100:.2f}% hit "
            f"({_format_exact_token_count(cache_efficiency.get('cached_prompt_tokens'))} cached, "
            f"{_format_exact_token_count(cache_efficiency.get('uncached_prompt_tokens'))} uncached)"
            f"{largest_text}.[/dim]"
        )
    console.print(
        "[dim]Processed tokens are cumulative input + output across calls; they are not the "
        "current context-window size. Cache read/write are input subdivisions and appear only "
        "when the provider reports them. A ~ dollar amount is a catalog estimate, not an invoice.[/dim]"
    )
    if unknown_total > 0:
        console.print(
            "[yellow]Total cost is partial:[/yellow] "
            f"{unknown_total} call(s) have unknown cost because pricing metadata is missing."
        )
    cache_cost_unknown = int(totals.get("cache_cost_pricing_missing_calls") or 0)
    if cache_cost_unknown > 0:
        console.print(
            "[yellow]Cache pricing omitted:[/yellow] "
            f"{cache_cost_unknown} cached call(s) need provider-specific read/write prices."
        )
    if int(totals.get("cache_read_input_tokens") or 0) or int(
        totals.get("cache_creation_input_tokens") or 0
    ):
        console.print(
            "[dim]Cache read/write tokens can reduce provider cost or latency, but they still "
            "occupy the current request context window.[/dim]"
        )
    corrected_total = int(totals.get("corrected_usage_calls") or 0)
    if corrected_total > 0:
        console.print(
            "[yellow]Usage corrected:[/yellow] "
            f"{corrected_total} provider usage record(s) contained inconsistent or character-like "
            "token fields and were corrected before display."
        )
    request_estimate = totals.get("request_token_estimate")
    if isinstance(request_estimate, dict):
        schema_over_calls = int(request_estimate.get("tool_schema_budget_exceeded_calls") or 0)
        if schema_over_calls > 0:
            schema_overage = int(request_estimate.get("tool_schema_budget_overage_tokens") or 0)
            largest_tool = int(request_estimate.get("tool_schema_largest_tool_tokens") or 0)
            console.print(
                "[yellow]Tool schema shadow budget:[/yellow] "
                f"{schema_over_calls} call(s) exceeded the schema budget by "
                f"{_format_exact_token_count(schema_overage)} total tokens "
                f"(largest single tool {_format_exact_token_count(largest_tool)})."
            )


def _print_chat_context(*, console: Console, session: Any) -> None:
    from ..chat import _print_chat_context_impl

    return _print_chat_context_impl(_cli_module_for_legacy_impl(), console=console, session=session)


def _chat_used_models(session: Any) -> list[str]:
    models: list[str] = []
    seen: set[str] = set()

    def _add(raw_value: Any) -> None:
        value = str(raw_value or "").strip()
        if not value:
            return
        key = value.casefold()
        if key in seen:
            return
        seen.add(key)
        models.append(value)

    _add(getattr(getattr(session, "client", None), "model", ""))
    _add(getattr(getattr(session, "cfg", None), "model", ""))

    summary = getattr(session, "usage_summary", None)
    rows_fn = getattr(summary, "by_model_rows", None)
    if callable(rows_fn):
        try:
            rows = rows_fn()
        except Exception:  # noqa: BLE001
            rows = []
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                _add(row.get("model"))

    return models


def _chat_config_usage_lines() -> list[str]:
    return [
        "/config",
        (
            "/config set <model|index> <context_window_tokens> <max_output_tokens> "
            "[supports_vision] [input_cost_per_token] [output_cost_per_token]"
        ),
        "/config clear <model|index>",
        "Example: /config set 1 128000 4096 false",
    ]


def _print_chat_config_usage(*, console: Console) -> None:
    console.print("[yellow]Usage:[/yellow]")
    for line in _chat_config_usage_lines():
        console.print(f"  {line}")


def _resolve_chat_model_ref(*, session: Any, raw_model_ref: str) -> tuple[str | None, str | None]:
    model_ref = raw_model_ref.strip()
    if not model_ref:
        return None, "Missing model reference."

    if model_ref.isdigit():
        models = _chat_used_models(session)
        if not models:
            return None, "No tracked models yet. Set a model first with /model <name>."
        index = int(model_ref)
        if index < 1 or index > len(models):
            return None, f"Invalid model index: {index}. Run /config to list tracked models."
        return models[index - 1], None

    return model_ref, None


def _merge_model_metadata_override(
    *,
    cfg: AppConfig,
    model_name: str,
    fields: dict[str, Any],
) -> dict[str, Any]:
    raw_overrides = cfg.extra_fields.get("model_metadata_overrides")
    overrides = dict(raw_overrides) if isinstance(raw_overrides, dict) else {}
    raw_models = overrides.get("models")
    models = dict(raw_models) if isinstance(raw_models, dict) else {}

    raw_existing = models.get(model_name)
    merged = dict(raw_existing) if isinstance(raw_existing, dict) else {}
    merged.update(fields)
    models[model_name] = merged
    overrides["models"] = models
    cfg.extra_fields["model_metadata_overrides"] = overrides
    return merged


def _clear_model_metadata_override(*, cfg: AppConfig, model_name: str) -> bool:
    raw_overrides = cfg.extra_fields.get("model_metadata_overrides")
    if not isinstance(raw_overrides, dict):
        return False
    raw_models = raw_overrides.get("models")
    if not isinstance(raw_models, dict):
        return False
    if model_name not in raw_models:
        return False

    models = dict(raw_models)
    models.pop(model_name, None)

    overrides = dict(raw_overrides)
    if models:
        overrides["models"] = models
    else:
        overrides.pop("models", None)

    if overrides:
        cfg.extra_fields["model_metadata_overrides"] = overrides
    else:
        cfg.extra_fields.pop("model_metadata_overrides", None)
    return True


def _save_chat_model_metadata_override(
    *,
    session: Any,
    model_name: str,
    fields: dict[str, Any],
) -> tuple[Path, dict[str, Any]]:
    cfg = load_config()
    merged = _merge_model_metadata_override(cfg=cfg, model_name=model_name, fields=fields)
    save_config(cfg)

    runtime_cfg = getattr(session, "cfg", None)
    if isinstance(runtime_cfg, AppConfig):
        _merge_model_metadata_override(cfg=runtime_cfg, model_name=model_name, fields=fields)
    return config_path(), merged


def _clear_chat_model_metadata_override(*, session: Any, model_name: str) -> tuple[Path, bool]:
    cfg = load_config()
    cleared = _clear_model_metadata_override(cfg=cfg, model_name=model_name)
    if cleared:
        save_config(cfg)

    runtime_cfg = getattr(session, "cfg", None)
    if isinstance(runtime_cfg, AppConfig):
        _clear_model_metadata_override(cfg=runtime_cfg, model_name=model_name)
    return config_path(), cleared


def _chat_config_panel(*, session: Any) -> Panel:
    models = _chat_used_models(session)
    registry = getattr(session, "model_registry", None)
    title = f"Model Config ({config_path()})"

    if _patchable("_is_narrow_terminal", _is_narrow_terminal)():
        lines: list[str] = []
        if models:
            lines.append("Tracked models:")
            for idx, model_name in enumerate(models, start=1):
                summary = ""
                if registry is not None:
                    try:
                        meta = registry.get(model_name)
                    except Exception as e:  # noqa: BLE001
                        summary = f"error={e}"
                    else:
                        field_sources = getattr(meta, "field_sources", {}) or {}
                        ctx_source = field_sources.get(
                            "context_window_tokens",
                            getattr(meta, "source", "unknown"),
                        )
                        summary = (
                            f"ctx={meta.context_window_tokens}, out={meta.max_output_tokens}, "
                            f"src={ctx_source}"
                        )
                if summary:
                    lines.append(f"{idx}) {model_name} - {summary}")
                else:
                    lines.append(f"{idx}) {model_name}")
        else:
            lines.append("No tracked models in this session yet.")
        lines.append("")
        lines.extend(_chat_config_usage_lines())
        return _Panel("\n".join(lines), title=title, border_style="bright_black")

    table = _Table(show_header=True, box=None, expand=True, padding=(0, 1), collapse_padding=True)
    table.add_column("#", style=STYLE_EMPHASIS, no_wrap=True, width=3)
    table.add_column("model", style=STYLE_CONTENT, no_wrap=False, ratio=4, overflow="fold")
    table.add_column("ctx", style=STYLE_CONTENT, no_wrap=True, ratio=2)
    table.add_column("max_out", style=STYLE_CONTENT, no_wrap=True, ratio=2)
    table.add_column("source", style=STYLE_CONTENT, no_wrap=False, ratio=2)
    table.add_column("note", style=STYLE_CONTENT, no_wrap=False, ratio=3, overflow="fold")

    if models:
        for idx, model_name in enumerate(models, start=1):
            ctx_value = "-"
            max_out_value = "-"
            source = "-"
            note = "-"
            if registry is not None:
                try:
                    meta = registry.get(model_name)
                except Exception as e:  # noqa: BLE001
                    note = str(e)
                else:
                    field_sources = getattr(meta, "field_sources", {}) or {}
                    ctx_value = str(getattr(meta, "context_window_tokens", "-"))
                    max_out_value = str(getattr(meta, "max_output_tokens", "-"))
                    source = str(
                        field_sources.get(
                            "context_window_tokens",
                            getattr(meta, "source", "unknown"),
                        )
                    )
                    warnings = list(getattr(meta, "warnings", ())[:1])
                    note = warnings[0] if warnings else "-"
            table.add_row(str(idx), model_name, ctx_value, max_out_value, source, note)
    else:
        table.add_row("-", "(no tracked models yet)", "-", "-", "-", "-")

    usage_text = "\n".join(_chat_config_usage_lines())
    content = _table_grid(expand=True)
    content.add_row(table)
    content.add_row("")
    content.add_row(usage_text)
    return _Panel(content, title=title, border_style="bright_black")


def _chat_turn_usage_line(session: Any) -> tuple[str | None, str | None] | None:
    ctx_percent = _chat_context_percent_value(session)
    ctx_display = _format_chat_context_percent(ctx_percent)
    ctx_segment = f"[dim]context left:[/dim] {ctx_display}" if ctx_display != "n/a" else None
    warning_line = _chat_usage_warning_line(_chat_effective_budget_percent_value(session))
    if not _chat_usage_hud_enabled(session):
        return (ctx_segment, warning_line) if ctx_segment or warning_line else None

    summary = getattr(session, "usage_summary", None)
    if summary is None:
        return (ctx_segment, warning_line) if ctx_segment or warning_line else None
    totals = summary.totals()
    total_tokens = int(totals.get("total_tokens") or 0)
    if total_tokens <= 0:
        return (ctx_segment, warning_line) if ctx_segment or warning_line else None

    segments = [
        f"{_format_compact_token_count(total_tokens)} [dim]tokens[/dim]",
        f"[dim]↓[/dim] {_format_compact_token_count(totals.get('prompt_tokens'))}",
        f"[dim]↑[/dim] {_format_compact_token_count(totals.get('completion_tokens'))}",
    ]
    if totals.get("estimate_usage_calls"):
        segments[0] += " [dim](includes estimates)[/dim]"
    cache_read_tokens = int(totals.get("cache_read_input_tokens") or 0)
    cache_write_tokens = int(totals.get("cache_creation_input_tokens") or 0)
    if cache_read_tokens or cache_write_tokens:
        segments.append(
            f"[dim]cache[/dim] r:{_format_compact_token_count(cache_read_tokens)} "
            f"w:{_format_compact_token_count(cache_write_tokens)}"
        )
    if ctx_segment is not None:
        segments.append(ctx_segment)
    return ("   ".join(segments), warning_line)


def _chat_bottom_toolbar(
    *,
    session: Any,
    pending_images: list[str],
    forge_state: _ForgeChatState | None = None,
) -> str:
    model = str(getattr(getattr(session, "client", None), "model", "?"))
    mode = str(getattr(session, "mode", "?"))
    pending_mode = str(getattr(session, "pending_permissions_mode", "") or "").strip()
    if pending_mode and pending_mode != mode:
        mode_display = f"{mode}→{pending_mode} next"
    elif pending_mode:
        mode_display = f"{mode} (next base)"
    else:
        mode_display = mode
    stream_enabled = bool(getattr(session, "stream", False))
    stream = "on" if stream_enabled else "off"
    temperature_raw = getattr(getattr(session, "client", None), "temperature", "?")
    temperature = str(temperature_raw)
    trace = _patchable("_chat_trace_level", _chat_trace_level)(session)
    ctx_hud = _chat_context_hud_value(session)
    subagents = "on" if bool(getattr(session, "subagents_enabled", False)) else "off"
    cfg = getattr(session, "cfg", None)
    toolbar_items_raw = getattr(cfg, "toolbar_items", None)
    if toolbar_items_raw is None:
        visible_items: set[str] = set(_DEFAULT_TOOLBAR_ITEMS)
    else:
        visible_items = {
            str(item).strip().lower() for item in toolbar_items_raw if str(item).strip()
        }

    tokens_part: str | None = None
    cost_part: str | None = None
    if _chat_usage_hud_enabled(session):
        summary = getattr(session, "usage_summary", None)
        if summary is not None:
            totals = summary.totals()
            total_tokens = int(totals.get("total_tokens") or 0)
            known_cost = _known_cost_value(totals)
            unknown_calls = int(totals.get("unknown_cost_calls") or 0)
            cost_hud = _format_cost_with_unknown(
                known_cost=known_cost,
                unknown_calls=unknown_calls,
                style="hud",
            )
            tokens_part = f"tokens={total_tokens}"
            if totals.get("estimate_usage_calls"):
                tokens_part += " (includes estimates)"
            cost_part = f"cost={cost_hud}"

    default_temperature_raw = getattr(session, "_toolbar_default_temperature", None)
    if default_temperature_raw is None:
        default_temperature_raw = getattr(getattr(session, "cfg", None), "chat_temperature", None)
    try:
        current_temperature = float(temperature_raw)
    except (TypeError, ValueError):
        current_temperature = None
    try:
        default_temperature = float(default_temperature_raw)
    except (TypeError, ValueError):
        default_temperature = None
    show_temperature = False
    if current_temperature is None:
        show_temperature = temperature.strip() not in {"", "?"}
    elif default_temperature is None:
        show_temperature = True
    else:
        show_temperature = abs(current_temperature - default_temperature) > 1e-9

    toolbar_parts: list[str] = []
    if "mode" in visible_items:
        toolbar_parts.append(mode_display)
    if "model" in visible_items:
        toolbar_parts.append(model)
    if "stream" in visible_items and stream == "off":
        toolbar_parts.append("no-stream")
    if "trace" in visible_items and trace != "compact":
        toolbar_parts.append(f"trace {trace}")
    if "images" in visible_items and pending_images:
        count = len(pending_images)
        toolbar_parts.append(f"{count} image" + ("" if count == 1 else "s"))
    if "temp" in visible_items and show_temperature:
        toolbar_parts.append(f"temp {temperature}")
    if "ctx" in visible_items:
        toolbar_parts.append(f"context left: {ctx_hud}")
    if "subagents" in visible_items:
        toolbar_parts.append(f"subagents {subagents}")
    if "tokens" in visible_items and tokens_part:
        token_value = tokens_part.split("=", 1)[-1]
        toolbar_parts.append(f"{token_value} tok")
    if "cost" in visible_items and cost_part:
        cost_value = cost_part.split("=", 1)[-1]
        toolbar_parts.append(cost_value)

    if (
        forge_state is not None
        and _is_forge_ui_mode(forge_state.ui_mode)
        and "forge" in visible_items
    ):
        run_id = forge_state.paths.run_id if forge_state.paths is not None else "-"
        task_count = len((forge_state.plan or {}).get("tasks") or [])
        toolbar_parts.append(run_id)
        toolbar_parts.append(f"{task_count} task" + ("" if task_count == 1 else "s"))
    if not toolbar_parts:
        return " /help "
    return " " + " · ".join([*toolbar_parts, "/help"]) + " "


def _chat_prompt_label(*, ui_mode: str = "chat", mode: str = "") -> str:
    if _is_forge_ui_mode(ui_mode):
        # ASCII-safe fallback (used when prompt_toolkit HTML is unavailable);
        # the interactive path uses the ``\u25b8`` form below.
        return "forge > "
    _ = mode
    return _CHAT_PROMPT_TEXT


def _chat_prompt_label_formatted(*, ui_mode: str = "chat", mode: str = "") -> Any:
    try:
        from prompt_toolkit.formatted_text import HTML
    except Exception:
        return _chat_prompt_label(ui_mode=ui_mode, mode=mode)
    if _is_forge_ui_mode(ui_mode):
        return HTML("<b>forge</b> <ansicyan>\u25b8</ansicyan> ")
    _ = mode
    return HTML("<b>&gt;</b> ")


def _chat_prompt_fallback_label(*, ui_mode: str = "chat", mode: str = "") -> str:
    if _is_forge_ui_mode(ui_mode):
        return "forge"
    _ = mode
    return _CHAT_PROMPT_FALLBACK_LABEL


def _accept_chat_suggestion_or_complete(event: Any) -> None:
    current_buffer = getattr(event, "current_buffer", None)
    if current_buffer is None:
        return
    complete_state = getattr(current_buffer, "complete_state", None)
    if complete_state is not None:
        completion = getattr(complete_state, "current_completion", None)
        apply_completion = getattr(current_buffer, "apply_completion", None)
        if completion is not None and callable(apply_completion):
            apply_completion(completion)
            return
        complete_next = getattr(current_buffer, "complete_next", None)
        if callable(complete_next):
            complete_next()
        return
    suggestion = getattr(current_buffer, "suggestion", None)
    suggestion_text = str(getattr(suggestion, "text", "") or "")
    document = getattr(current_buffer, "document", None)
    if suggestion_text and bool(getattr(document, "is_cursor_at_the_end", False)):
        insert_text = getattr(current_buffer, "insert_text", None)
        if callable(insert_text):
            insert_text(suggestion_text)
        return
    start_completion = getattr(current_buffer, "start_completion", None)
    if callable(start_completion):
        start_completion(select_first=False)


def _submitted_prompt_total_lines(
    *,
    submitted_text: str,
    prompt_label: str,
    terminal_columns: int | None = None,
) -> int:
    columns = int(terminal_columns or 0)
    if columns <= 0:
        columns = _terminal_width(default=80)
    if columns <= 0:
        columns = 80

    normalized = str(submitted_text or "").replace("\r\n", "\n").replace("\r", "\n")
    segments = normalized.split("\n")
    first_prefix = max(1, visibleLength(prompt_label))
    total_lines = 0
    for idx, segment in enumerate(segments):
        segment_width = visibleLength(segment)
        prefix_width = first_prefix if idx == 0 else 0
        visual_width = segment_width + prefix_width
        if visual_width <= 0:
            total_lines += 1
            continue
        total_lines += ((visual_width - 1) // columns) + 1
    return max(1, total_lines)


def _clear_previous_terminal_lines_ansi(line_count: int) -> str:
    count = max(1, int(line_count or 1))
    parts: list[str] = []
    for _ in range(count):
        parts.append("\x1b[1A\r\x1b[2K")
    parts.append("\r")
    return "".join(parts)


def _clear_submitted_prompt_line(
    *,
    submitted_text: str = "",
    prompt_label: str = _CHAT_PROMPT_TEXT,
    terminal_columns: int | None = None,
) -> None:
    if not _patchable("_is_interactive_terminal", _is_interactive_terminal)():
        return
    term = (os.environ.get("TERM") or "").strip().lower()
    if term in {"", "dumb"}:
        return
    try:
        clear_count = _submitted_prompt_total_lines(
            submitted_text=submitted_text,
            prompt_label=prompt_label,
            terminal_columns=terminal_columns,
        )
        sys.stdout.write(_clear_previous_terminal_lines_ansi(clear_count))
        sys.stdout.flush()
    except Exception:
        pass


def _redact_chat_error_text(text: str) -> str:
    out = str(text or "")
    for pattern in _CHAT_LLM_ERROR_REDACT_PATTERNS:
        if pattern.pattern.lower().startswith("(authorization"):
            out = pattern.sub(r"\1[REDACTED]", out)
            continue
        if pattern.pattern.lower().startswith("(bearer"):
            out = pattern.sub(r"\1[REDACTED]", out)
            continue
        out = pattern.sub("[REDACTED]", out)
    return out


def _truncate_chat_error_text(text: str, *, max_chars: int = _CHAT_LLM_ERROR_MAX_CHARS) -> str:
    clean = _redact_chat_error_text(str(text).strip())
    if not clean:
        return "No additional error details."
    if len(clean) <= max_chars:
        return clean
    return clean[: max_chars - 15].rstrip() + "...(truncated)"


def _chat_llm_error_panel(*, message: str) -> Panel:
    friendly_message = friendly_llm_error_message(message)
    display = classify_llm_error_display(friendly_message)
    body_lines = [_truncate_chat_error_text(friendly_message)]
    if display.guidance_lines:
        body_lines.append("")
        body_lines.extend(display.guidance_lines)
    return _Panel("\n".join(body_lines), title=display.title, border_style="red")


def _humanized_chat_llm_error_message(error: Exception) -> str:
    return friendly_llm_error_message(error)


def _render_chat_llm_error(*, session: Any, console: Console, error: Exception) -> None:
    message = _humanized_chat_llm_error_message(error)
    surface = getattr(session, "surface", None)
    on_error = getattr(surface, "on_error", None)
    if callable(on_error):
        try:
            on_error(message)
        except Exception:  # noqa: BLE001
            console.print("")
            console.print(_chat_llm_error_panel(message=message))
            return
        if bool(getattr(surface, "renders_error_panel", False)):
            return
    console.print("")
    console.print(_chat_llm_error_panel(message=message))


__all__ = [name for name in globals() if (not name.startswith("__") or name == "__version__")]
