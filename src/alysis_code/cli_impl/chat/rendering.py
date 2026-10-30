# ruff: noqa: F821
from __future__ import annotations

from pathlib import Path
from typing import Any

from ...surface.styles import STYLE_EMPHASIS

_PROTECTED_RENDERING_GLOBAL_NAMES: set[str] = set()


def _sync_rendering_globals(source_globals: dict[str, Any]) -> None:
    module_globals = globals()
    if not _PROTECTED_RENDERING_GLOBAL_NAMES:
        for local_name, local_value in module_globals.items():
            if callable(local_value):
                _PROTECTED_RENDERING_GLOBAL_NAMES.add(local_name)
    for name, value in source_globals.items():
        if name.startswith("__") or name in _PROTECTED_RENDERING_GLOBAL_NAMES:
            continue
        module_globals[name] = value


def _forge_plan_command_guidance_lines() -> tuple[str, ...]:
    return (
        "[yellow]Usage:[/yellow] /plan tasks|table|markdown|md|edit",
        "/plan tasks     show the current plan summary (same as /show)",
        "/plan markdown  preview PLAN.md for the current run",
        "/plan edit      edit plan.json and reload Forge state",
    )


def _print_forge_plan_command_guidance(*, console: Any) -> None:
    for line in _forge_plan_command_guidance_lines():
        console.print(line)


def _chat_skill_usage_lines() -> tuple[str, ...]:
    return (
        "[yellow]Usage:[/yellow] /skill",
        "                 /skill <name>",
        "                 /skill <name> <task>",
    )


def _artifact_display_ref(*, root: Path, store_obj: Any, artifact_path: Path | None) -> str:
    if artifact_path is None:
        return "-"
    layout = getattr(store_obj, "session_artifact_layout", None)
    display_fn = getattr(layout, "display_reference_for_path", None)
    if callable(display_fn):
        try:
            return str(display_fn(artifact_path=artifact_path, workspace_root=root))
        except Exception:  # noqa: BLE001
            pass
    try:
        return artifact_path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return artifact_path.name


def _render_planner_reply(
    *, console: Any, message: str, questions: list[str] | None = None
) -> None:
    console.print("[bold]Planner:[/bold]")
    console.print(message)
    if questions:
        console.print("[dim]Planner questions[/dim]")
        for question in questions:
            console.print(f"- {question}")


def _render_labeled_chat_message(*, console: Any, label: str, message: str) -> None:
    clean = str(message or "").strip() or "(empty message)"
    prefix = ""
    if label and str(label).strip().lower() not in {"you", "user"}:
        prefix = f"{str(label).strip()} · "
    console.print(
        _forge_bar_text(
            text=f"{prefix}{clean}",
            style=STYLE_EMPHASIS,
            bar_style=STYLE_EMPHASIS,
        ),
        highlight=False,
    )


def _print_chat_context(*, console: Console, session: Any) -> None:
    context_fn = getattr(session, "context_left", None)
    if not callable(context_fn):
        console.print("Context tracking unavailable for this session.")
        return
    _refresh_chat_hud_context_cache(session)
    ctx = getattr(session, "_hud_context_cache", None)
    if ctx is None:
        console.print("Context tracking unavailable for this session.")
        return
    context_window_tokens = getattr(
        ctx,
        "context_window_tokens",
        getattr(ctx, "max_input_tokens", "n/a"),
    )
    context_window_remaining = getattr(
        ctx,
        "context_window_remaining_tokens",
        getattr(ctx, "remaining_tokens", "n/a"),
    )
    context_window_percent = getattr(
        ctx,
        "context_window_percent_left",
        getattr(ctx, "percent_left", None),
    )
    from ..commands.startup import _chat_provider_usage_rows

    provider_table = _Table(title="Provider Usage · Last Response")
    provider_table.add_column("field")
    provider_table.add_column("value")
    for label, value, _tone in _chat_provider_usage_rows(session):
        provider_table.add_row(label, value)
    console.print(provider_table)

    table = _Table(title="Context Window Projection")
    table.add_column("field")
    table.add_column("value")
    table.add_row("model", str(getattr(ctx, "model_name", "-")))
    table.add_row("model_metadata_source", str(getattr(ctx, "source", "-")))
    table.add_row(
        "capacity_provider_key",
        str(getattr(ctx, "capacity_provider_key", None) or "-"),
    )
    table.add_row(
        "context_window_source",
        str(getattr(ctx, "context_window_source", None) or "-"),
    )
    table.add_row(
        "max_output_source",
        str(getattr(ctx, "max_output_source", None) or "-"),
    )
    table.add_row("token_count_source", str(getattr(ctx, "token_count_source", "-")))
    table.add_row(
        "token_count_confidence",
        str(getattr(ctx, "token_count_confidence", "-")),
    )
    anchor_source = getattr(ctx, "anchor_token_count_source", None)
    if anchor_source:
        table.add_row("request_anchor_source", str(anchor_source))
        table.add_row(
            "request_anchor_confidence",
            str(getattr(ctx, "anchor_token_count_confidence", "-")),
        )
    table.add_row("context_window_tokens", str(context_window_tokens))
    table.add_row(
        "projected_total_request_tokens",
        str(getattr(ctx, "used_input_tokens", "n/a")),
    )
    table.add_row(
        "local_request_estimate_tokens",
        str(getattr(ctx, "local_request_estimate_tokens", "n/a")),
    )
    table.add_row("context_window_left_tokens", str(context_window_remaining))
    table.add_row(
        "context_window_left_percent",
        f"{float(context_window_percent):.1f}%" if context_window_percent is not None else "n/a",
    )
    console.print(table)

    messages_obj = getattr(session, "messages", [])
    messages = messages_obj if isinstance(messages_obj, list) else []
    tool_list_obj = getattr(session, "tool_list", None)
    tool_list = tool_list_obj if isinstance(tool_list_obj, list) else None
    compactor = getattr(session, "conversation_compactor", None)
    registry = getattr(session, "model_registry", None)
    model_name = str(getattr(getattr(session, "client", None), "model", "") or "").strip()
    if not model_name:
        model_name = str(getattr(ctx, "model_name", "") or "").strip()

    cfg = getattr(session, "cfg", None)
    if isinstance(cfg, AppConfig):
        compaction_settings = _resolve_compaction_settings(cfg)
    else:
        compaction_settings = _resolve_compaction_settings(AppConfig())

    model_meta = None
    if registry is not None and model_name:
        try:
            model_meta = registry.get(model_name)
        except Exception:  # noqa: BLE001
            model_meta = None

    model_context_window_tokens = getattr(model_meta, "context_window_tokens", None)
    max_output_tokens = getattr(model_meta, "max_output_tokens", None)
    safety_margin_tokens = compaction_settings.safety_margin_tokens
    effective_input_budget = (
        getattr(ctx, "effective_input_budget", None)
        or compute_input_budget(model_meta, safety_margin=safety_margin_tokens)
        if model_meta is not None
        else None
    )

    pinned_prefix_len = int(getattr(session, "pinned_prefix_len", 0) or 0)
    if pinned_prefix_len <= 0 and compactor is not None and hasattr(compactor, "state"):
        pinned_prefix_len = int(getattr(compactor.state, "pinned_prefix_len", 0) or 0)
    if pinned_prefix_len <= 0:
        for idx, msg in enumerate(messages):
            if str(msg.get("role") or "") != "user":
                continue
            content = msg.get("content")
            if isinstance(content, str) and content.startswith("<environment_context>"):
                pinned_prefix_len = idx + 1
                break

    request_breakdown = estimate_request_token_breakdown(
        messages=messages,
        tool_list=tool_list,
        pinned_prefix_len=pinned_prefix_len,
    )
    try:
        total_request_tokens = max(0, int(getattr(ctx, "used_input_tokens", None)))
    except (TypeError, ValueError):
        total_request_tokens = request_breakdown.total_tokens
    tokens_tools = max(0, int(getattr(request_breakdown, "tool_schema_tokens", 0) or 0))
    if tokens_tools > total_request_tokens:
        tokens_tools = total_request_tokens
    tokens_messages = max(0, total_request_tokens - tokens_tools)
    remaining_to_budget = getattr(ctx, "effective_remaining_tokens", None)
    if remaining_to_budget is None and effective_input_budget is not None:
        remaining_to_budget = max(0, effective_input_budget - total_request_tokens)
    percent_budget_left = getattr(ctx, "effective_percent_left", None)
    if (
        percent_budget_left is None
        and effective_input_budget is not None
        and effective_input_budget > 0
        and remaining_to_budget is not None
    ):
        percent_budget_left = (remaining_to_budget / effective_input_budget) * 100.0

    budget_table = _Table(title="Effective Input Budget")
    budget_table.add_column("field")
    budget_table.add_column("value")
    budget_table.add_row("context_window_tokens", str(model_context_window_tokens or "n/a"))
    budget_table.add_row("max_output_tokens", str(max_output_tokens or "n/a"))
    budget_table.add_row("safety_margin_tokens", str(safety_margin_tokens))
    budget_table.add_row(
        "effective_input_budget",
        str(effective_input_budget) if effective_input_budget is not None else "n/a",
    )
    budget_table.add_row("estimated_messages_tokens", str(tokens_messages))
    budget_table.add_row("estimated_tools_tokens", str(tokens_tools))
    budget_table.add_row("estimated_total_request_tokens", str(total_request_tokens))
    budget_table.add_row(
        "remaining_to_budget",
        str(remaining_to_budget) if remaining_to_budget is not None else "n/a",
    )
    budget_table.add_row(
        "percent_budget_left",
        f"{percent_budget_left:.1f}%" if percent_budget_left is not None else "n/a",
    )
    console.print(budget_table)

    dynamic_percent_left = getattr(ctx, "dynamic_context_percent_left", None)
    dynamic_table = _Table(title="Conversation Context Left")
    dynamic_table.add_column("field")
    dynamic_table.add_column("value")
    dynamic_table.add_row(
        "startup_baseline_tokens",
        str(getattr(ctx, "startup_baseline_tokens", "n/a")),
    )
    dynamic_table.add_row(
        "dynamic_context_budget_tokens",
        str(getattr(ctx, "dynamic_context_budget_tokens", "n/a")),
    )
    dynamic_table.add_row(
        "dynamic_context_used_tokens",
        str(getattr(ctx, "dynamic_context_used_tokens", "n/a")),
    )
    dynamic_table.add_row(
        "dynamic_context_left_tokens",
        str(getattr(ctx, "dynamic_context_remaining_tokens", "n/a")),
    )
    dynamic_table.add_row(
        "dynamic_context_left_percent",
        f"{dynamic_percent_left:.1f}%" if dynamic_percent_left is not None else "n/a",
    )
    console.print(dynamic_table)

    if effective_input_budget is not None and total_request_tokens > effective_input_budget:
        console.print(
            "[yellow]Request estimate exceeds effective input budget.[/yellow] "
            "Run /compact to reduce context."
        )

    sanitized_messages = _sanitize_messages_for_estimation(messages)
    pinned_messages: list[dict[str, Any]] = []
    memory_messages: list[dict[str, Any]] = []
    pins_messages: list[dict[str, Any]] = []
    conversation_messages: list[dict[str, Any]] = []
    for idx, msg in enumerate(sanitized_messages):
        if idx < pinned_prefix_len:
            pinned_messages.append(msg)
            continue
        content = msg.get("content")
        if isinstance(content, str) and content.startswith(_memory_marker()):
            memory_messages.append(msg)
            continue
        if isinstance(content, str) and content.startswith(_pins_marker()):
            pins_messages.append(msg)
            continue
        conversation_messages.append(msg)

    def _bucket_tokens(bucket: list[dict[str, Any]]) -> int:
        if not bucket:
            return 0
        return estimate_tokens(json.dumps(bucket, ensure_ascii=False, sort_keys=True))

    pinned_tokens = _bucket_tokens(pinned_messages)
    memory_tokens = _bucket_tokens(memory_messages)
    pins_tokens = _bucket_tokens(pins_messages)
    conversation_tokens = _bucket_tokens(conversation_messages)

    breakdown_table = _Table(title="Context Breakdown")
    breakdown_table.add_column("field")
    breakdown_table.add_column("value")
    breakdown_table.add_row("pinned_prefix_tokens", str(pinned_tokens))
    breakdown_table.add_row("memory_tokens", str(memory_tokens))
    breakdown_table.add_row("pins_tokens", str(pins_tokens))
    breakdown_table.add_row("conversation_tokens", str(conversation_tokens))
    breakdown_table.add_row("total_messages_count", str(len(messages)))
    console.print(breakdown_table)

    root = Path(getattr(session, "root", Path(".")))
    store_obj = getattr(session, "store", None)
    session_id = str(getattr(store_obj, "session_id", "") or "")
    history_session_dir = None
    if compactor is not None and hasattr(compactor, "history_dir"):
        history_dir_obj = getattr(compactor, "history_dir", None)
        if isinstance(history_dir_obj, Path):
            history_session_dir = history_dir_obj.parent
    if history_session_dir is None and session_id:
        history_session_dir = root / ".alysis" / "sessions" / _safe_component(session_id)
    tool_output_session_dir = getattr(store_obj, "session_artifact_root", None)
    history_chunks_count = 0
    offloaded_tool_outputs_count = 0
    summary_exists = False
    pins_file_exists = False
    if history_session_dir is not None:
        history_chunks_count = len(list((history_session_dir / "history").glob("chunk_*.jsonl")))
        summary_exists = (history_session_dir / "memory" / "summary.json").exists()
        pins_file_exists = (history_session_dir / "memory" / "pins.json").exists()
    if (
        history_session_dir is not None
        and session_id
        and history_chunks_count == 0
        and not summary_exists
        and not pins_file_exists
    ):
        legacy_history_session_dir = root / ".alysis" / "sessions" / _safe_component(session_id)
        if legacy_history_session_dir != history_session_dir:
            history_chunks_count = len(
                list((legacy_history_session_dir / "history").glob("chunk_*.jsonl"))
            )
            summary_exists = (legacy_history_session_dir / "memory" / "summary.json").exists()
            pins_file_exists = (legacy_history_session_dir / "memory" / "pins.json").exists()
    if isinstance(tool_output_session_dir, Path):
        offloaded_tool_outputs_count = len(
            list((tool_output_session_dir / "tool_outputs").glob("*.json"))
        )

    pins_count = 0
    if compactor is not None and hasattr(compactor, "state"):
        history_chunks_count = max(
            history_chunks_count,
            int(getattr(compactor.state, "history_chunk_index", 0) or 0),
        )
        pins = getattr(compactor.state, "pins", [])
        if isinstance(pins, list):
            pins_count = len(pins)

    compaction_table = _Table(title="Compaction Artifacts")
    compaction_table.add_column("field")
    compaction_table.add_column("value")
    compaction_table.add_row("compaction_enabled", "yes" if compactor is not None else "no")
    compaction_table.add_row(
        "compaction_profile",
        str(getattr(compactor, "profile_name", "-")) if compactor is not None else "-",
    )
    compaction_table.add_row(
        "compactor_model",
        str(getattr(getattr(compactor, "compactor_client", None), "model", "-")),
    )
    compaction_table.add_row("history_chunks_count", str(history_chunks_count))
    compaction_table.add_row("pins_count", str(pins_count))
    compaction_table.add_row("offloaded_tool_outputs_count", str(offloaded_tool_outputs_count))
    compaction_table.add_row("summary_file_exists", "yes" if summary_exists else "no")
    compaction_table.add_row("pins_file_exists", "yes" if pins_file_exists else "no")
    console.print(compaction_table)
