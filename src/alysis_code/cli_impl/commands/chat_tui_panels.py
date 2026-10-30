"""TUI-native panel spec builders for chat slash commands.

Each builder mirrors a classic ``_print_*`` / ``_chat_*_panel`` command but
returns a structured *spec* the full-screen TUI renders as a centered popup
panel (via ``_render_kv_panel_rows`` in :mod:`...cli_impl.tui.app`) instead of
dumping flat, colourless captured Rich text into the transcript.

A panel spec is::

    {"title": str, "hint"?: str, "sections": [(section_name, rows)]}

where ``rows`` is a list of ``(key, value, tone)`` tuples. ``tone`` selects the
value colour: ``"accent"`` (on/healthy, green), ``"plain"`` (neutral),
``"warn"`` (amber), ``"err"`` (red), ``"dim"`` (muted).

Unlike the sibling legacy split modules, this one imports every helper it needs
*explicitly* (no ``from .cli_common import *`` global injection), so the builders
are importable and unit-testable in isolation with a plain fake session.
"""

from __future__ import annotations

from typing import Any

from ..tui.forge_status import forge_status_bucket, forge_status_glyph

# Canonical, self-contained helpers (defined directly in these modules — not
# injected at runtime), so the imports resolve at import time and in tests.
from .startup import (
    _chat_context_percent_value,
    _chat_usage_hud_enabled,
    _format_chat_context_percent,
    _format_exact_token_count,
    _format_reported_token_total,
    _format_usage_billing_for_display,
    _format_usage_source_for_display,
    _known_cost_value,
    _refresh_chat_hud_context_cache,
    _session_skill_listing,
)

PanelSpec = dict[str, Any]


def _format_terminal_runtime_s(runtime_s: float) -> str:
    """Mirror of the classic terminal runtime formatter (kept local so this
    module never imports the injected ``chat/commands.py`` legacy module)."""
    if runtime_s < 0:
        runtime_s = 0.0
    if runtime_s < 60:
        return f"{runtime_s:.1f}s"
    minutes = int(runtime_s // 60)
    seconds = int(runtime_s % 60)
    return f"{minutes}m{seconds:02d}s"


# ------------------------------------------------------------------- /usage
def _chat_usage_panel_spec(*, session: Any) -> PanelSpec:
    """Token count & cost as a panel (mirrors ``_print_chat_usage``)."""
    summary = getattr(session, "usage_summary", None)
    if summary is None:
        return {
            "title": "Usage",
            "sections": [
                ("Usage", [("status", "Usage tracking unavailable for this session.", "plain")])
            ],
        }
    try:
        rows = summary.by_model_rows()
    except Exception:  # noqa: BLE001 - never crash the UI on a panel
        rows = []
    if not rows:
        hud = "on" if _chat_usage_hud_enabled(session) else "off"
        return {
            "title": "Usage",
            "sections": [
                (
                    "Usage",
                    [
                        ("status", "No usage events yet in this session.", "plain"),
                        ("hud", hud, "accent" if hud == "on" else "plain"),
                    ],
                )
            ],
            "hint": "/usage hud on|off to toggle the toolbar HUD · Esc close",
        }

    # Index keys (not the model name) so a long, namespaced model id never blows
    # past the panel's narrow key column — the full name lives in the wrap-safe
    # value column instead.
    model_rows: list[tuple[str, str, str]] = []
    for idx, row in enumerate(rows, start=1):
        cost = _format_usage_billing_for_display(row, compact=True)
        source = _format_usage_source_for_display(row)
        cache_read = _format_reported_token_total(
            row,
            token_key="cache_read_input_tokens",
            reported_calls_key="cache_read_reported_calls",
        )
        cache_write = _format_reported_token_total(
            row,
            token_key="cache_creation_input_tokens",
            reported_calls_key="cache_creation_reported_calls",
        )
        # A provider that reports no cache figures at all says nothing useful by
        # repeating "not reported" twice on every row; the /usage table still
        # spells the state out per column.
        cache_bits = (
            ""
            if cache_read == "not reported" and cache_write == "not reported"
            else f"  cache r:{cache_read} w:{cache_write}"
        )
        value = (
            f"{row.get('model') or '-'}   "
            f"↓ {_format_exact_token_count(row.get('prompt_tokens'))}  "
            f"↑ {_format_exact_token_count(row.get('completion_tokens'))}  "
            f"Σ {_format_exact_token_count(row.get('total_tokens'))}   "
            f"{cost}{cache_bits}  ({source})"
        )
        model_rows.append((str(idx), value, "plain"))

    totals = summary.totals()
    unknown_total = int(totals.get("unknown_cost_calls") or 0)
    total_cost = _format_usage_billing_for_display(totals)
    # Only paint the cost green when it is fully known; an unknown/partial total
    # stays neutral (never reads as healthy) — matching the classic "[yellow]Total
    # cost is partial" warning.
    cost_tone = (
        "accent"
        if (
            _known_cost_value(totals) is not None
            and unknown_total == 0
            and int(totals.get("catalog_estimated_cost_calls") or 0) == 0
        )
        else "plain"
    )
    total_rows: list[tuple[str, str, str]] = [
        ("processed", _format_exact_token_count(totals.get("total_tokens")), "accent"),
        ("input total", _format_exact_token_count(totals.get("prompt_tokens")), "plain"),
        (
            "input uncached",
            _format_reported_token_total(
                totals,
                token_key="input_tokens_uncached",
                reported_calls_key="input_tokens_uncached_reported_calls",
                derived_calls_key="input_tokens_uncached_derived_calls",
            ),
            "plain",
        ),
        (
            "cache read",
            _format_reported_token_total(
                totals,
                token_key="cache_read_input_tokens",
                reported_calls_key="cache_read_reported_calls",
            ),
            "plain",
        ),
        (
            "cache write",
            _format_reported_token_total(
                totals,
                token_key="cache_creation_input_tokens",
                reported_calls_key="cache_creation_reported_calls",
            ),
            "plain",
        ),
        ("output", _format_exact_token_count(totals.get("completion_tokens")), "plain"),
        ("billing", total_cost, cost_tone),
        (
            "meaning",
            "processed is cumulative across calls, not current context",
            "dim",
        ),
    ]
    cache_efficiency = totals.get("cache_efficiency")
    if isinstance(cache_efficiency, dict) and cache_efficiency.get("hit_ratio") is not None:
        largest = cache_efficiency.get("largest_uncached_call")
        largest_text = ""
        if isinstance(largest, dict):
            largest_text = (
                f"; max {_format_exact_token_count(largest.get('uncached_prompt_tokens'))} "
                f"at call {int(largest.get('call_position') or 0)}"
            )
        total_rows.append(
            (
                "cache hit",
                f"{float(cache_efficiency['hit_ratio']) * 100:.2f}%{largest_text}",
                "plain",
            )
        )
    if unknown_total > 0:
        total_rows.append(
            ("note", f"{unknown_total} call(s) have unknown cost (missing pricing)", "warn")
        )
    cache_cost_unknown = int(totals.get("cache_cost_pricing_missing_calls") or 0)
    if cache_cost_unknown > 0:
        total_rows.append(
            (
                "note",
                f"{cache_cost_unknown} cached call(s) need provider-specific cache pricing",
                "warn",
            )
        )
    corrected_total = int(totals.get("corrected_usage_calls") or 0)
    if corrected_total > 0:
        total_rows.append(
            ("note", f"{corrected_total} provider usage record(s) corrected before display", "warn")
        )
    request_estimate = totals.get("request_token_estimate")
    if isinstance(request_estimate, dict):
        schema_over_calls = int(request_estimate.get("tool_schema_budget_exceeded_calls") or 0)
        if schema_over_calls > 0:
            schema_overage = int(request_estimate.get("tool_schema_budget_overage_tokens") or 0)
            largest_tool = int(request_estimate.get("tool_schema_largest_tool_tokens") or 0)
            total_rows.append(
                (
                    "note",
                    (
                        f"{schema_over_calls} call(s) exceeded tool schema shadow budget "
                        f"(+{_format_exact_token_count(schema_overage)}; "
                        f"largest tool {_format_exact_token_count(largest_tool)})"
                    ),
                    "warn",
                )
            )
    return {
        "title": "Usage",
        "sections": [("Per model", model_rows), ("Total", total_rows)],
    }


# --------------------------------------------------------------- /context (/ctx alias)
def _chat_context_panel_spec(*, session: Any) -> PanelSpec:
    """Context-window usage as a panel (mirrors the primary ``/context`` table)."""
    if not callable(getattr(session, "context_left", None)):
        return {
            "title": "Context Window",
            "sections": [
                ("Context", [("status", "Context tracking unavailable for this session.", "plain")])
            ],
        }
    _refresh_chat_hud_context_cache(session)
    ctx = getattr(session, "_hud_context_cache", None)
    if ctx is None:
        return {
            "title": "Context Window",
            "sections": [
                ("Context", [("status", "Context tracking unavailable for this session.", "plain")])
            ],
        }
    context_window = getattr(ctx, "context_window_tokens", getattr(ctx, "max_input_tokens", "n/a"))
    remaining = getattr(
        ctx, "context_window_remaining_tokens", getattr(ctx, "remaining_tokens", "n/a")
    )
    percent = getattr(ctx, "context_window_percent_left", getattr(ctx, "percent_left", None))
    used = getattr(ctx, "used_input_tokens", "n/a")
    effective_budget = getattr(ctx, "effective_input_budget", None)
    effective_remaining = getattr(ctx, "effective_remaining_tokens", None)
    effective_percent = getattr(ctx, "effective_percent_left", None)
    startup_baseline = getattr(ctx, "startup_baseline_tokens", None)
    dynamic_budget = getattr(ctx, "dynamic_context_budget_tokens", None)
    dynamic_used = getattr(ctx, "dynamic_context_used_tokens", None)
    dynamic_remaining = getattr(ctx, "dynamic_context_remaining_tokens", None)
    dynamic_percent = getattr(ctx, "dynamic_context_percent_left", None)

    # Colour the percent from the SAME metric we display (the context-window
    # percent), not the dynamic-budget percent — otherwise a healthy 60%-left
    # window could be painted red because the baseline-subtracted dynamic value
    # is much lower. Fall back to the session helper only when the window value
    # is missing.
    displayed_percent = percent
    if displayed_percent is None:
        displayed_percent = _chat_context_percent_value(session)
    try:
        percent_float = float(displayed_percent) if displayed_percent is not None else None
    except (TypeError, ValueError):
        percent_float = None
    if percent_float is None:
        pct_tone = "plain"
    elif percent_float < 10.0:
        pct_tone = "err"
    elif percent_float < 25.0:
        pct_tone = "warn"
    else:
        pct_tone = "accent"
    percent_display = _format_chat_context_percent(displayed_percent)

    context_rows: list[tuple[str, str, str]] = [
        ("model", str(getattr(ctx, "model_name", "-")), "plain"),
        ("metadata source", str(getattr(ctx, "source", "-")), "dim"),
        (
            "capacity route",
            str(getattr(ctx, "capacity_provider_key", None) or "-"),
            "dim",
        ),
        ("token source", str(getattr(ctx, "token_count_source", "-")), "dim"),
        ("confidence", str(getattr(ctx, "token_count_confidence", "-")), "dim"),
        ("context window", str(context_window), "plain"),
        ("used (projected)", str(used), "plain"),
        ("left tokens", str(remaining), "plain"),
        ("left %", percent_display, pct_tone),
    ]
    anchor_source = getattr(ctx, "anchor_token_count_source", None)
    if anchor_source:
        context_rows.insert(
            4,
            ("request anchor", str(anchor_source), "dim"),
        )
    budget_rows: list[tuple[str, str, str]] = [
        ("input budget", _context_metric_display(effective_budget), "plain"),
        ("budget left", _context_metric_display(effective_remaining), "plain"),
        (
            "budget left %",
            _format_chat_context_percent(_context_float(effective_percent)),
            _context_percent_tone(effective_percent),
        ),
    ]
    conversation_rows: list[tuple[str, str, str]] = [
        ("startup baseline", _context_metric_display(startup_baseline), "plain"),
        ("conversation budget", _context_metric_display(dynamic_budget), "plain"),
        ("conversation used", _context_metric_display(dynamic_used), "plain"),
        ("conversation left", _context_metric_display(dynamic_remaining), "plain"),
        (
            "conversation left %",
            _format_chat_context_percent(_context_float(dynamic_percent)),
            _context_percent_tone(dynamic_percent),
        ),
    ]
    from .startup import _chat_provider_usage_rows

    sections: list[tuple[str, list[tuple[str, str, str]]]] = [
        ("Provider Usage · Last Response", _chat_provider_usage_rows(session)),
        ("Context", context_rows),
    ]
    if any(row[1] != "n/a" for row in budget_rows):
        sections.append(("Effective Input Budget", budget_rows))
    if any(row[1] != "n/a" for row in conversation_rows):
        sections.append(("Conversation Context", conversation_rows))
    return {
        "title": "Context Window",
        "sections": sections,
        "hint": "/compact to reduce context · Esc close",
    }


def _context_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _context_percent_tone(value: Any) -> str:
    percent = _context_float(value)
    if percent is None:
        return "plain"
    if percent < 10.0:
        return "err"
    if percent < 25.0:
        return "warn"
    return "accent"


def _context_metric_display(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return str(value)
    return str(max(0, parsed))


# ----------------------------------------------------------------- /model-info
def _chat_model_info_panel_spec(*, session: Any, model_ref: str = "") -> PanelSpec:
    """Resolved model metadata as a panel (mirrors ``/model-info``)."""
    model_name = (model_ref or "").strip() or str(
        getattr(getattr(session, "client", None), "model", "") or ""
    ).strip()
    if not model_name:
        return {
            "title": "Model Info",
            "sections": [
                ("Model", [("status", "Model info unavailable (missing model name).", "plain")])
            ],
        }
    registry = getattr(session, "model_registry", None)
    if registry is None:
        return {
            "title": "Model Info",
            "sections": [
                ("Model", [("status", "Model info unavailable for this session.", "plain")])
            ],
        }
    try:
        meta = registry.get(model_name)
    except Exception as exc:  # noqa: BLE001 - surface a resolve failure inline
        return {
            "title": f"Model Info ({model_name})",
            "sections": [("Model", [("error", str(exc), "err")])],
        }

    field_sources = getattr(meta, "field_sources", {}) or {}

    def _src(key: str) -> str:
        return str(field_sources.get(key, "unknown"))

    rows: list[tuple[str, str, str]] = [
        ("resolved", str(getattr(meta, "model_name", model_name)), "accent"),
        ("source", str(getattr(meta, "source", "unknown")), "plain"),
        (
            "context window",
            f"{getattr(meta, 'context_window_tokens', '-')} ({_src('context_window_tokens')})",
            "plain",
        ),
        (
            "max output",
            f"{getattr(meta, 'max_output_tokens', '-')} ({_src('max_output_tokens')})",
            "plain",
        ),
        (
            "vision",
            f"{getattr(meta, 'supports_vision', '-')} ({_src('supports_vision')})",
            "plain",
        ),
        (
            "input $/tok",
            f"{getattr(meta, 'input_cost_per_token', '-')} ({_src('input_cost_per_token')})",
            "plain",
        ),
        (
            "output $/tok",
            f"{getattr(meta, 'output_cost_per_token', '-')} ({_src('output_cost_per_token')})",
            "plain",
        ),
    ]
    last_error = getattr(registry, "last_error", None)
    if last_error:
        rows.append(("registry error", str(last_error), "err"))
    warnings = "; ".join(getattr(meta, "warnings", ())[:5])
    if warnings:
        rows.append(("warnings", warnings, "warn"))
    return {"title": f"Model Info ({model_name})", "sections": [("Model", rows)]}


# -------------------------------------------------------------------- /config
def _chat_config_panel_spec(*, session: Any) -> PanelSpec:
    """Tracked-model config as a panel (mirrors ``_chat_config_panel``).

    The interactive config menu cannot run inside the alt-screen, so the bare
    ``/config`` shows this read-only panel; ``/config set|clear`` still apply via
    the command runner.
    """
    from .chat_status import _chat_used_models  # self-contained getter

    models = _chat_used_models(session)
    registry = getattr(session, "model_registry", None)

    model_rows: list[tuple[str, str, str]] = []
    if models:
        for idx, name in enumerate(models, start=1):
            summary = ""
            if registry is not None:
                try:
                    meta = registry.get(name)
                except Exception as exc:  # noqa: BLE001
                    summary = f"error={exc}"
                else:
                    field_sources = getattr(meta, "field_sources", {}) or {}
                    src = field_sources.get(
                        "context_window_tokens", getattr(meta, "source", "unknown")
                    )
                    summary = (
                        f"ctx={getattr(meta, 'context_window_tokens', '-')}  "
                        f"out={getattr(meta, 'max_output_tokens', '-')}  src={src}"
                    )
            value = f"{name}   {summary}".rstrip() if summary else str(name)
            model_rows.append((str(idx), value, "accent" if idx == 1 else "plain"))
    else:
        model_rows.append(("models", "No tracked models in this session yet.", "plain"))

    usage_rows: list[tuple[str, str, str]] = [
        ("set", "/config set <model|index> <ctx> <max_out> [vision] [in$] [out$]", "dim"),
        ("clear", "/config clear <model|index>", "dim"),
        ("example", "/config set 1 128000 4096 false", "dim"),
    ]
    return {
        "title": "Model Config",
        "sections": [("Tracked models", model_rows), ("Manage", usage_rows)],
    }


# ------------------------------------------------------------------- /toolbar
def _chat_toolbar_panel_spec(*, session: Any) -> PanelSpec:
    """Status-toolbar items as a panel (mirrors bare ``/toolbar``)."""
    from .cli_common import (
        _CHAT_TOOLBAR_ITEM_ORDER,
        _DEFAULT_TOOLBAR_ITEMS,
        _VALID_TOOLBAR_ITEMS,
    )

    cfg_obj = getattr(session, "cfg", None)
    raw_items = getattr(cfg_obj, "toolbar_items", None) if cfg_obj is not None else None
    active: list[str] = []
    seen: set[str] = set()
    for raw_item in list(raw_items or list(_DEFAULT_TOOLBAR_ITEMS)):
        item = str(raw_item).strip().lower()
        if not item or item in seen or item not in _VALID_TOOLBAR_ITEMS:
            continue
        seen.add(item)
        active.append(item)
    available = [item for item in _CHAT_TOOLBAR_ITEM_ORDER if item not in set(active)]

    item_rows: list[tuple[str, str, str]] = [
        ("active", ", ".join(active) if active else "(none)", "accent"),
        ("available", ", ".join(available) if available else "(none)", "plain"),
    ]
    manage_rows: list[tuple[str, str, str]] = [
        ("add", "/toolbar add <item>", "dim"),
        ("remove", "/toolbar remove <item>", "dim"),
        ("reset", "/toolbar reset", "dim"),
        ("save", "/toolbar save", "dim"),
    ]
    return {
        "title": "Toolbar Items",
        "sections": [("Items", item_rows), ("Manage", manage_rows)],
    }


# ----------------------------------------------------------------- /terminals
def _chat_terminals_panel_spec(*, session: Any) -> PanelSpec:
    """Background processes as a panel (mirrors ``/terminals list``)."""
    terminal_manager = getattr(session, "terminal_manager", None)
    if terminal_manager is None:
        return {
            "title": "Background Terminals",
            "sections": [
                (
                    "Terminals",
                    [("status", "Background terminals are unavailable in this session.", "plain")],
                )
            ],
        }
    try:
        summaries = terminal_manager.list()
    except Exception as exc:  # noqa: BLE001
        return {
            "title": "Background Terminals",
            "sections": [("Terminals", [("error", str(exc), "err")])],
        }
    if not summaries:
        process_rows: list[tuple[str, str, str]] = [("status", "No background processes.", "plain")]
    else:
        process_rows = []
        for summary in summaries:
            status = str(getattr(summary, "status", "-"))
            status_lc = status.casefold()
            if status_lc in {"failed", "error"}:
                tone = "err"
            elif status_lc == "running":
                tone = "accent"
            else:
                tone = "plain"
            exit_code = getattr(summary, "exit_code", None)
            runtime = _format_terminal_runtime_s(float(getattr(summary, "runtime_s", 0) or 0))
            value = (
                f"{status}  ·  exit {('-' if exit_code is None else exit_code)}  "
                f"·  {runtime}  ·  {getattr(summary, 'cmd', '')}"
            )
            process_rows.append((str(getattr(summary, "process_id", "-")), value, tone))
    manage_rows: list[tuple[str, str, str]] = [
        ("show", "/terminals show <id>", "dim"),
        ("kill", "/terminals kill <id>", "dim"),
    ]
    return {
        "title": "Background Terminals",
        "sections": [("Processes", process_rows), ("Manage", manage_rows)],
    }


# --------------------------------------------------------------------- /skill
def _chat_skill_listing_panel_spec(*, session: Any) -> PanelSpec:
    """Discovered skills as a panel (mirrors bare ``/skill``)."""
    enabled, ordered, issues = _session_skill_listing(session)
    if not enabled:
        return {
            "title": "Skills",
            "sections": [
                ("Skills", [("status", "Skills are disabled for this session config.", "plain")])
            ],
        }
    if not ordered:
        return {
            "title": "Skills",
            "sections": [
                (
                    "Skills",
                    [("status", "No skills discovered in the supported skill roots.", "plain")],
                )
            ],
        }
    skill_rows = [
        (str(getattr(skill, "name", "")), str(getattr(skill, "description", "") or ""), "plain")
        for skill in ordered
    ]
    sections: list[tuple[str, list[tuple[str, str, str]]]] = [
        (f"Skills ({len(ordered)})", skill_rows)
    ]
    issue_rows: list[tuple[str, str, str]] = []
    for issue in issues:
        source_path = getattr(issue, "source_path", None)
        message = str(getattr(issue, "message", "") or "").strip()
        if source_path is not None and message:
            issue_rows.append((str(source_path), message, "warn"))
    if issue_rows:
        sections.append(("Skipped", issue_rows))
    return {
        "title": "Skills",
        "sections": sections,
        "hint": "/skill <name> for info · /skill <name> <task> to attach · Esc close",
    }


# ------------------------------------------------------- subagent descriptions
# Leading boilerplate stripped from a subagent description before summarising
# (longest-first so the most specific prefix wins). The blurbs all open with some
# variant of "Use this when you need to …" / "Catch-all subagent for …", which
# carries no information for a one-line picker label.
_SUBAGENT_DESC_BOILERPLATE = (
    "use this agent when you need to ",
    "use this agent when you need ",
    "use this agent when ",
    "use this when you need to ",
    "use this when you need an ",
    "use this when you need a ",
    "use this when you need ",
    "use this when answering means ",
    "use this when ",
    "use this for ",
    "use for ",
    "catch-all subagent for ",
    "catch-all agent for ",
    "catch-all for ",
)


def _short_subagent_desc(text: str, limit: int = 46) -> str:
    """Condense a (potentially huge) subagent description to a short, scannable
    summary shown to the right of the option in the spawn picker.

    Collapses whitespace, strips the boilerplate "Use this when you need to …"
    lead-in, keeps only the first clause (up to the first ``:`` / ``.`` / dash),
    capitalises it, and hard-truncates with an ellipsis so each option stays a
    single readable line.
    """
    compact = " ".join(str(text or "").split())
    if not compact:
        return ""
    lowered = compact.lower()
    for prefix in _SUBAGENT_DESC_BOILERPLATE:
        if lowered.startswith(prefix):
            compact = compact[len(prefix) :]
            break
    # Keep only the first clause/sentence — the crisp "what it does".
    cut = len(compact)
    for sep in (": ", ". ", "; ", " — ", " - "):
        idx = compact.find(sep)
        if idx != -1:
            cut = min(cut, idx)
    summary = compact[:cut].rstrip(" ,;:.—-")
    if summary:
        summary = summary[0].upper() + summary[1:]
    if len(summary) > limit:
        head = summary[: max(1, limit - 1)]
        # Prefer breaking on a word boundary so we never clip mid-word ("recent c…").
        space = head.rfind(" ")
        if space >= limit // 2:
            head = head[:space]
        summary = head.rstrip(" ,;:.—-") + "…"
    return summary


# --------------------------------------------------------------------- Forge
# The status→bucket authority lives in ``...tui.forge_status`` so the live swarm
# table, the ``/show`` plan panel here, and the end-of-run summary can never
# disagree about whether a task is done, failed, or still running.
def _forge_status_tone(canonical: str) -> str:
    """Map a canonical task status to a panel value tone (accent/err/dim/plain).

    done → accent (green), failed → err (red), obsolete → dim, everything else
    (running/planned) → plain — the SAME semantic colours the live swarm table
    uses, so a task reads identically in ``/show`` and while it runs."""
    bucket = forge_status_bucket(canonical)
    if bucket == "done":
        return "accent"
    if bucket == "failed":
        return "err"
    if bucket == "obsolete":
        return "dim"
    return "plain"


# The "how Forge works" guidance shown in the centered popup when the user runs a
# plain ``/forge``. Markdown so the doc panel renders headings/lists; the closing
# call-to-action pairs with the panel's "Enter to begin · Esc to cancel" hint.
_FORGE_INTRO_BODY = """\
# Forge — autonomous build mode

Forge turns a goal into a reviewed plan, then runs a **swarm of agents** to build
it task by task. You stay in control — nothing is built until you say so.

## How it works

1. **Set the goal** — describe what you want built (just type it, or `/goal`).
2. **Shape the plan** — add or refine tasks with `/task`, or let the planner
   assistant draft them for you.
3. **Review** — inspect the plan and task table with `/show` and `/plan`.
4. **Execute** — `/execute plan` runs the swarm to build every task, live.
5. **Finish** — `/done` (or `/back`) returns you to normal chat.

## Handy commands

- `/show` — plan summary: goal, requirements, tasks
- `/plan` — view or edit the plan (tasks · markdown · edit)
- `/assets` — attach and review reference files
- `/execute plan` — run the build
- `/done` · `/back` — leave Forge

Press **Enter** to choose your planner and begin, or **Esc** to stay in chat."""


def _chat_forge_intro_panel_spec() -> PanelSpec:
    """Guidance popup shown for a plain ``/forge`` before entering the session.

    Returns a document-panel spec (rendered through the TUI markdown pipeline)
    carrying a ``confirm`` command: the centered popup explains how Forge works
    and, on Enter, routes ``/forge`` to the command runner to actually enter the
    planning session; Esc cancels. ``/forge resume`` and re-entry while already in
    Forge skip this intro (handled by the caller's gating).
    """
    return {
        "title": "Forge",
        "body": _FORGE_INTRO_BODY,
        "hint": "↵ Enter to begin  ·  Esc to cancel",
        "confirm": "/forge",
        "accent": "forge",
    }


def _chat_forge_plan_panel_spec(*, paths: Any, plan: dict[str, Any]) -> PanelSpec:
    """Forge plan summary as a panel — mirrors ``_show_forge_plan_summary``.

    Renders the goal/summary + per-task table that the classic ``/show`` and
    ``/plan tasks`` print, as a centered popup instead of a flat Rich table dump.
    Used in Forge mode for ``/show`` and ``/plan tasks|table|view``.
    """
    from ...forge import requirement_text
    from ...swarm_scheduler import canonical_task_status
    from .cli_common import _forge_task_mcp_summary_label, _forge_task_status_counts
    from .forge_asset_view import forge_asset_view_count

    goal = str(plan.get("project_goal") or "").strip() or "(not set)"
    summary = str(plan.get("summary") or "").strip() or "(not set)"
    tasks_obj = plan.get("tasks") or []
    tasks = tasks_obj if isinstance(tasks_obj, list) else []
    task_count = len(tasks)
    try:
        asset_count = forge_asset_view_count(paths, plan)
    except Exception:  # noqa: BLE001 - never crash the UI on a panel
        asset_count = 0
    try:
        done, failed, remaining = _forge_task_status_counts(plan)
    except Exception:  # noqa: BLE001
        done = failed = remaining = 0

    overview: list[tuple[str, str, str]] = [
        ("run", str(getattr(paths, "run_id", "-")), "plain"),
        ("goal", goal, "plain"),
        ("summary", summary, "plain"),
        (
            "tasks",
            f"{task_count} total · {done} done · {failed} failed · {remaining} remaining",
            "accent" if (task_count and failed == 0) else "plain",
        ),
        ("assets", str(asset_count), "plain"),
    ]
    sections: list[tuple[str, list[tuple[str, str, str]]]] = [("Plan", overview)]

    requirements_obj = plan.get("requirements") or []
    requirements = requirements_obj if isinstance(requirements_obj, list) else []
    if requirements:
        req_rows: list[tuple[str, str, str]] = []
        for idx, req in enumerate(requirements[:12], start=1):
            text = " ".join(str(requirement_text(req) or "").split())
            if not text:
                continue
            req_rows.append((str(idx), text, "plain"))
        if len(requirements) > 12:
            req_rows.append(("…", f"({len(requirements) - 12} more)", "dim"))
        if req_rows:
            sections.append((f"Requirements ({len(requirements)})", req_rows))

    if tasks:
        task_rows: list[tuple[str, str, str]] = []
        for task in tasks:
            if not isinstance(task, dict):
                continue
            tid = str(task.get("id") or "-")
            raw_status = str(task.get("status") or "planned")
            canonical = canonical_task_status(raw_status)
            tone = _forge_status_tone(canonical)
            # Prefix the SAME glyph the live swarm table uses (✓/✗/○/·) so a task
            # looks identical here and while it executes.
            glyph = forge_status_glyph(canonical)
            title = str(task.get("title") or "-")
            value = f"{glyph} {raw_status}  ·  {title}"
            extra: list[str] = []
            deps = task.get("dependencies") or []
            if isinstance(deps, list) and deps:
                extra.append("deps: " + ", ".join(str(dep) for dep in deps))
            try:
                mcp = _forge_task_mcp_summary_label(task)
            except Exception:  # noqa: BLE001
                mcp = "off"
            if mcp and mcp != "off":
                extra.append(f"mcp: {mcp}")
            if extra:
                value += "  ·  " + " · ".join(extra)
            task_rows.append((tid, value, tone))
        sections.append((f"Tasks ({task_count})", task_rows))
    else:
        sections.append(("Tasks", [("-", "(no tasks yet)", "dim")]))

    return {
        "title": f"Forge Plan · {getattr(paths, 'run_id', '')}".rstrip(" ·"),
        "sections": sections,
        "hint": "/execute plan to run · /plan markdown for PLAN.md · Esc close",
        "accent": "forge",
    }


def _chat_forge_markdown_panel_spec(*, paths: Any, plan: dict[str, Any]) -> PanelSpec:
    """PLAN.md preview as a scrollable doc panel — replaces the classic pager.

    Saves the in-memory plan first (so PLAN.md reflects edits, like the classic
    ``_show_forge_plan_markdown``), reads PLAN.md, and returns a ``body`` spec the
    TUI renders through its markdown renderer in the centered popup.
    """
    from ...forge import save_plan

    try:
        save_plan(paths, plan)
    except Exception:  # noqa: BLE001 - still try to show whatever is on disk
        pass
    plan_md_path = getattr(paths, "plan_md_path", None)
    body = ""
    if plan_md_path is not None:
        try:
            body = plan_md_path.read_text(encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            return {
                "title": "PLAN.md",
                "sections": [("PLAN.md", [("error", f"Failed to read PLAN.md: {exc}", "err")])],
            }
    if not body.strip():
        body = "(PLAN.md is empty)"
    return {
        "title": f"PLAN.md · {getattr(paths, 'run_id', '')}".rstrip(" ·"),
        "body": body,
        "hint": "↑↓/PgUp/PgDn scroll · Esc close",
        "accent": "forge",
    }


# ---------------------------------------------------------------- Forge assets
def _format_asset_size(size_bytes: int) -> str:
    """Human-readable byte size (mirror of assets_modal._format_size)."""
    size = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size_bytes} B"


def _asset_status_tone(status: str) -> str:
    s = str(status or "").strip().lower()
    if s == "ready":
        return "accent"
    if s == "failed":
        return "err"
    if s in {"pending", "running"}:
        return "warn"
    return "plain"


def _chat_assets_picker_spec(*, cfg: Any, paths: Any) -> dict[str, Any] | None:
    """Selectable list of Forge assets — replaces the dead stdin assets modal.

    Returns ``None`` (so the caller falls through) when the asset surface can't be
    built or there are no assets to pick. Choosing a row submits ``/assets <id>``
    to open the detail panel."""
    from ...assets.surface import build_asset_surface
    from ...forge import load_plan

    try:
        load_plan(paths)
    except Exception:  # noqa: BLE001 - listing still works without a loaded plan
        pass
    try:
        surface = build_asset_surface(cfg=cfg, run_paths=paths)
        entries = surface.list_assets()
    except Exception:  # noqa: BLE001
        return None
    if not entries:
        return None
    rows: list[dict[str, Any]] = []
    for entry in entries:
        rec = entry.record
        kind = "img" if str(getattr(rec, "kind", "")) == "image" else "txt"
        size = _format_asset_size(int(getattr(rec, "size_bytes", 0) or 0))
        status = str(getattr(entry, "comprehension_status", "") or "")
        pin = "★ " if getattr(rec, "pinned", False) else ""
        rows.append(
            {
                "label": str(rec.id),
                "description": f"{pin}{rec.title}  ·  {kind} · {status} · {size}",
                "value": str(rec.id),
                "current": False,
            }
        )
    return {
        "title": f"Assets ({len(rows)})",
        "rows": rows,
        "on_select": lambda value: {"submit": f"/assets {value}"},
        "hint": "↑↓ select · Enter to view · Esc cancel",
    }


def _chat_asset_detail_panel_spec(*, cfg: Any, paths: Any, asset_id: str) -> PanelSpec:
    """One asset's metadata + comprehension summary as a panel (replaces the
    interactive modal's detail view)."""
    from ...assets import AssetError
    from ...assets.surface import build_asset_surface

    try:
        surface = build_asset_surface(cfg=cfg, run_paths=paths)
        detail = surface.show_asset(asset_id)
    except AssetError as exc:
        return {
            "title": f"Asset · {asset_id}",
            "sections": [("Asset", [("error", str(exc), "err")])],
        }
    except Exception as exc:  # noqa: BLE001
        return {"title": "Asset", "sections": [("Asset", [("error", str(exc), "err")])]}

    rec = detail.record
    deleted = getattr(rec, "deleted_at", None) is not None
    status = "deleted" if deleted else str(getattr(detail, "comprehension_status", "") or "")
    overview: list[tuple[str, str, str]] = [
        ("id", str(rec.id), "accent"),
        ("title", str(rec.title), "plain"),
        ("kind", str(getattr(rec, "kind", "-")), "plain"),
        ("size", _format_asset_size(int(getattr(rec, "size_bytes", 0) or 0)), "plain"),
        ("mime", str(getattr(rec, "mime", "-")), "plain"),
        (
            "pinned",
            "yes" if getattr(rec, "pinned", False) else "no",
            "accent" if getattr(rec, "pinned", False) else "plain",
        ),
        ("status", status, "err" if deleted else _asset_status_tone(status)),
        ("file", str(getattr(rec, "original_filename", "-")), "dim"),
    ]
    sections: list[tuple[str, list[tuple[str, str, str]]]] = [("Asset", overview)]
    description = str(getattr(rec, "description", "") or "").strip()
    if description:
        sections.append(("Description", [("text", description, "plain")]))
    comp = getattr(detail, "comprehension", None)
    if comp is not None:
        data = getattr(comp, "data", None)
        comp_rows: list[tuple[str, str, str]] = [
            ("summary", str(getattr(data, "semantic_summary", "") or "-"), "plain"),
            ("language", str(getattr(comp, "detected_language", "") or "-"), "dim"),
            ("source", str(getattr(comp, "source", "") or "-"), "dim"),
        ]
        sections.append(("Comprehension", comp_rows))
        facts = list(getattr(data, "stated_facts", None) or [])
        if facts:
            sections.append(
                (
                    "Stated facts",
                    [(str(i + 1), str(fact), "plain") for i, fact in enumerate(facts[:8])],
                )
            )
    return {
        "title": f"Asset · {rec.id}",
        "sections": sections,
        "hint": "Esc close",
        "accent": "forge",
    }


__all__ = [
    "_chat_usage_panel_spec",
    "_chat_context_panel_spec",
    "_chat_model_info_panel_spec",
    "_chat_config_panel_spec",
    "_chat_toolbar_panel_spec",
    "_chat_terminals_panel_spec",
    "_chat_skill_listing_panel_spec",
    "_short_subagent_desc",
    "_forge_status_tone",
    "_chat_forge_intro_panel_spec",
    "_chat_forge_plan_panel_spec",
    "_chat_forge_markdown_panel_spec",
    "_format_asset_size",
    "_asset_status_tone",
    "_chat_assets_picker_spec",
    "_chat_asset_detail_panel_spec",
]
