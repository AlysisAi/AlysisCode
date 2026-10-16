from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .agent.prompt_context import prepare_session_prompt_context
from .agent.tools_assembly import build_tools
from .config import AppConfig
from .model_registry import ModelRegistry
from .request_estimation import estimate_message_tokens, estimate_tool_schema_tokens
from .session_store import SessionStore, resolve_sessions_dir
from .subagents import SubagentDefinition
from .surface import NoopSurface
from .workspace_binding import WorkspaceBinding

DEFAULT_EXECUTION_SAFETY_MARGIN_TOKENS = 512
DEFAULT_EXECUTION_RESPONSE_RESERVE_TOKENS = 1024
DEFAULT_EXECUTION_HEADROOM_RESERVE_TOKENS = 1024
DEFAULT_MINIMUM_EXECUTION_INSTRUCTION_BUDGET_TOKENS = 1200
DEFAULT_MIN_EXECUTION_RESPONSE_RESERVE_TOKENS = 256
DEFAULT_EXECUTION_IMAGE_RESERVE_TOKENS_PER_IMAGE = 1024


@dataclass(frozen=True)
class ExecutionReserveResolution:
    requested_execution_response_reserve_tokens: int
    effective_execution_response_reserve_tokens: int
    requested_execution_headroom_reserve_tokens: int
    effective_execution_headroom_reserve_tokens: int
    minimum_instruction_budget_tokens: int
    reserve_adjustment_applied: bool
    reserve_adjustment_reason: str | None
    final_instruction_budget: int


@dataclass(frozen=True)
class ExecutionPromptBudget:
    model: str
    context_window_tokens: int
    max_output_tokens: int
    safety_margin_tokens: int
    trusted_system_prompt_override_applied: bool
    trusted_system_prompt_append_applied: bool
    untrusted_prompt_prelude_applied: bool
    subagents_enabled: bool
    pinned_prefix_token_estimate: int
    tool_schema_token_estimate: int
    requested_execution_response_reserve_tokens: int
    effective_execution_response_reserve_tokens: int
    requested_execution_headroom_reserve_tokens: int
    effective_execution_headroom_reserve_tokens: int
    minimum_instruction_budget_tokens: int
    reserve_adjustment_applied: bool
    reserve_adjustment_reason: str | None
    image_count: int
    image_budget_reserve_tokens: int
    final_instruction_budget: int

    @property
    def execution_response_reserve_tokens(self) -> int:
        return self.effective_execution_response_reserve_tokens

    @property
    def execution_headroom_reserve_tokens(self) -> int:
        return self.effective_execution_headroom_reserve_tokens

    def to_payload(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "context_window_tokens": self.context_window_tokens,
            "max_output_tokens": self.max_output_tokens,
            "safety_margin_tokens": self.safety_margin_tokens,
            "trusted_system_prompt_override_applied": (self.trusted_system_prompt_override_applied),
            "trusted_system_prompt_append_applied": self.trusted_system_prompt_append_applied,
            "untrusted_prompt_prelude_applied": self.untrusted_prompt_prelude_applied,
            "subagents_enabled": self.subagents_enabled,
            "pinned_prefix_token_estimate": self.pinned_prefix_token_estimate,
            "tool_schema_token_estimate": self.tool_schema_token_estimate,
            "execution_response_reserve_tokens": self.execution_response_reserve_tokens,
            "execution_headroom_reserve_tokens": self.execution_headroom_reserve_tokens,
            "requested_execution_response_reserve_tokens": (
                self.requested_execution_response_reserve_tokens
            ),
            "effective_execution_response_reserve_tokens": (
                self.effective_execution_response_reserve_tokens
            ),
            "requested_execution_headroom_reserve_tokens": (
                self.requested_execution_headroom_reserve_tokens
            ),
            "effective_execution_headroom_reserve_tokens": (
                self.effective_execution_headroom_reserve_tokens
            ),
            "minimum_instruction_budget_tokens": self.minimum_instruction_budget_tokens,
            "reserve_adjustment_applied": self.reserve_adjustment_applied,
            "reserve_adjustment_reason": self.reserve_adjustment_reason,
            "image_count": self.image_count,
            "image_budget_reserve_tokens": self.image_budget_reserve_tokens,
            "final_instruction_budget": self.final_instruction_budget,
        }


@dataclass(frozen=True)
class ExecutionPromptBudgetInputs:
    budget: ExecutionPromptBudget
    prefix_messages: tuple[dict[str, Any], ...]
    tool_list: tuple[dict[str, Any], ...]


def _resolve_execution_reserves(
    *,
    context_window_tokens: int,
    model_max_output_tokens: int,
    safety_margin_tokens: int,
    pinned_prefix_token_estimate: int,
    tool_schema_token_estimate: int,
    requested_execution_response_reserve_tokens: int,
    requested_execution_headroom_reserve_tokens: int,
    minimum_instruction_budget_tokens: int,
    image_budget_reserve_tokens: int,
    minimum_execution_response_reserve_tokens: int = DEFAULT_MIN_EXECUTION_RESPONSE_RESERVE_TOKENS,
) -> ExecutionReserveResolution:
    available_before_reserves = max(
        0,
        int(context_window_tokens)
        - max(0, int(safety_margin_tokens))
        - max(0, int(pinned_prefix_token_estimate))
        - max(0, int(tool_schema_token_estimate))
        - max(0, int(image_budget_reserve_tokens)),
    )
    requested_response = max(
        0,
        min(
            int(model_max_output_tokens),
            int(requested_execution_response_reserve_tokens),
        ),
    )
    requested_headroom = max(0, int(requested_execution_headroom_reserve_tokens))
    minimum_budget = max(0, int(minimum_instruction_budget_tokens))
    effective_response = requested_response
    effective_headroom = requested_headroom
    reserve_adjustment_applied = False
    reserve_adjustment_reason: str | None = None

    final_instruction_budget = max(
        0,
        available_before_reserves - effective_response - effective_headroom,
    )
    if final_instruction_budget < minimum_budget:
        shortfall = minimum_budget - final_instruction_budget
        headroom_reduction = min(shortfall, effective_headroom)
        effective_headroom -= headroom_reduction
        shortfall -= headroom_reduction

        minimum_response = min(
            requested_response,
            max(0, int(minimum_execution_response_reserve_tokens)),
        )
        response_reduction = 0
        if shortfall > 0:
            reducible_response = max(0, effective_response - minimum_response)
            response_reduction = min(shortfall, reducible_response)
            effective_response -= response_reduction
            shortfall -= response_reduction

        final_instruction_budget = max(
            0,
            available_before_reserves - effective_response - effective_headroom,
        )
        reserve_adjustment_applied = headroom_reduction > 0 or response_reduction > 0
        if reserve_adjustment_applied:
            if response_reduction > 0 and headroom_reduction > 0:
                reserve_adjustment_reason = (
                    "reduced execution headroom and response reserves to preserve the minimum "
                    "instruction budget"
                )
            elif headroom_reduction > 0:
                reserve_adjustment_reason = (
                    "reduced execution headroom reserve to preserve the minimum instruction budget"
                )
            else:
                reserve_adjustment_reason = (
                    "reduced execution response reserve to preserve the minimum instruction budget"
                )
            if final_instruction_budget < minimum_budget:
                reserve_adjustment_reason += " (minimum floor still limited by context)"

    return ExecutionReserveResolution(
        requested_execution_response_reserve_tokens=requested_response,
        effective_execution_response_reserve_tokens=effective_response,
        requested_execution_headroom_reserve_tokens=requested_headroom,
        effective_execution_headroom_reserve_tokens=effective_headroom,
        minimum_instruction_budget_tokens=minimum_budget,
        reserve_adjustment_applied=reserve_adjustment_applied,
        reserve_adjustment_reason=reserve_adjustment_reason,
        final_instruction_budget=final_instruction_budget,
    )


def _estimation_store(
    *,
    cfg: AppConfig,
    root: Path,
) -> SessionStore:
    return SessionStore(
        enabled=False,
        sessions_dir=resolve_sessions_dir(cfg),
        session_id="execution_budget_estimate",
        cwd=str(root),
        repo_root=str(root),
        workspace_root=str(root),
        focus_dir=str(root),
    )


def compute_execution_prompt_budget_inputs(
    *,
    cfg: AppConfig,
    root: Path,
    mode: str,
    yes: bool,
    deny_write_prefixes: list[str] | None = None,
    allow_write_globs: list[str] | None = None,
    non_interactive: bool = False,
    one_shot_execution: bool = False,
    verification_enabled: bool = True,
    authoritative_verification_commands: list[str] | None = None,
    trusted_system_prompt_override: str | None = None,
    trusted_system_prompt_append: str | None = None,
    untrusted_prompt_prelude: str | None = None,
    subagents_enabled: bool | None = None,
    subagent_depth: int = 0,
    subagent_registry: dict[str, SubagentDefinition] | None = None,
    workspace_binding: WorkspaceBinding | None = None,
    api_key: str | None = None,
    model_registry: ModelRegistry | None = None,
    safety_margin_tokens: int = DEFAULT_EXECUTION_SAFETY_MARGIN_TOKENS,
    execution_response_reserve_tokens: int = DEFAULT_EXECUTION_RESPONSE_RESERVE_TOKENS,
    execution_headroom_reserve_tokens: int = DEFAULT_EXECUTION_HEADROOM_RESERVE_TOKENS,
    minimum_instruction_budget_tokens: int = DEFAULT_MINIMUM_EXECUTION_INSTRUCTION_BUDGET_TOKENS,
    image_count: int = 0,
    image_budget_reserve_tokens_per_image: int = DEFAULT_EXECUTION_IMAGE_RESERVE_TOKENS_PER_IMAGE,
) -> ExecutionPromptBudgetInputs:
    prompt_context = prepare_session_prompt_context(
        cfg=cfg,
        root=root,
        mode=mode,
        yes=yes,
        deny_write_prefixes=deny_write_prefixes,
        allow_write_globs=allow_write_globs,
        non_interactive=non_interactive,
        one_shot_execution=one_shot_execution,
        verification_enabled=verification_enabled,
        authoritative_verification_commands=authoritative_verification_commands,
        trusted_system_prompt_override=trusted_system_prompt_override,
        trusted_system_prompt_append=trusted_system_prompt_append,
        untrusted_prompt_prelude=untrusted_prompt_prelude,
        subagents_enabled=subagents_enabled,
        subagent_depth=subagent_depth,
        subagent_registry=subagent_registry,
        workspace_binding=workspace_binding,
    )
    registry = model_registry or ModelRegistry(cfg=prompt_context.session_cfg, api_key=api_key)
    meta = registry.get(prompt_context.session_cfg.model)

    store = _estimation_store(cfg=prompt_context.session_cfg, root=prompt_context.root)
    try:
        tools = build_tools(
            root=prompt_context.root,
            console=None,
            surface=NoopSurface(),
            store=store,
            mode=mode,
            yes=yes,
            cfg=prompt_context.session_cfg,
            api_key=api_key,
            max_steps=None,
            no_log=True,
            usage_role="execution_budget_estimate",
            usage_summary=None,
            model_registry=registry,
            deny_write_prefixes=deny_write_prefixes,
            allow_write_globs=allow_write_globs,
            non_interactive=non_interactive,
            shell_runner=None,
            shell_environment=prompt_context.shell_environment,
            verification_enabled=verification_enabled,
            authoritative_verification_commands=prompt_context.authoritative_verify_commands,
            subagents_enabled=prompt_context.resolved_subagents_enabled,
            subagent_depth=subagent_depth,
            subagent_registry=prompt_context.resolved_subagent_registry,
            skills_enabled=prompt_context.resolved_skills_enabled,
            skill_registry=prompt_context.discovered_skills.skills,
            session_log_dir_override=None,
            # Budget estimation assembles a scratch tool surface. It must not
            # rewrite live sessions' process-global compatibility state.
            tool_availability_sink=lambda _snapshot: None,
        )
    finally:
        store.close()

    tool_list = [tool.as_openai_tool() for tool in tools.values()]
    prefix_tokens = estimate_message_tokens(prompt_context.messages)
    tool_schema_tokens = estimate_tool_schema_tokens(tool_list)

    safety_margin = max(0, int(safety_margin_tokens))
    resolved_image_count = max(0, int(image_count))
    image_budget_reserve = resolved_image_count * max(0, int(image_budget_reserve_tokens_per_image))
    reserve_resolution = _resolve_execution_reserves(
        context_window_tokens=int(meta.context_window_tokens),
        model_max_output_tokens=int(meta.max_output_tokens),
        safety_margin_tokens=safety_margin,
        pinned_prefix_token_estimate=prefix_tokens,
        tool_schema_token_estimate=tool_schema_tokens,
        requested_execution_response_reserve_tokens=execution_response_reserve_tokens,
        requested_execution_headroom_reserve_tokens=execution_headroom_reserve_tokens,
        minimum_instruction_budget_tokens=minimum_instruction_budget_tokens,
        image_budget_reserve_tokens=image_budget_reserve,
    )

    return ExecutionPromptBudgetInputs(
        budget=ExecutionPromptBudget(
            model=prompt_context.session_cfg.model,
            context_window_tokens=int(meta.context_window_tokens),
            max_output_tokens=int(meta.max_output_tokens),
            safety_margin_tokens=safety_margin,
            trusted_system_prompt_override_applied=bool(
                trusted_system_prompt_override and trusted_system_prompt_override.strip()
            ),
            trusted_system_prompt_append_applied=bool(
                trusted_system_prompt_append and trusted_system_prompt_append.strip()
            ),
            untrusted_prompt_prelude_applied=bool(
                untrusted_prompt_prelude and untrusted_prompt_prelude.strip()
            ),
            subagents_enabled=prompt_context.resolved_subagents_enabled,
            pinned_prefix_token_estimate=prefix_tokens,
            tool_schema_token_estimate=tool_schema_tokens,
            requested_execution_response_reserve_tokens=(
                reserve_resolution.requested_execution_response_reserve_tokens
            ),
            effective_execution_response_reserve_tokens=(
                reserve_resolution.effective_execution_response_reserve_tokens
            ),
            requested_execution_headroom_reserve_tokens=(
                reserve_resolution.requested_execution_headroom_reserve_tokens
            ),
            effective_execution_headroom_reserve_tokens=(
                reserve_resolution.effective_execution_headroom_reserve_tokens
            ),
            minimum_instruction_budget_tokens=reserve_resolution.minimum_instruction_budget_tokens,
            reserve_adjustment_applied=reserve_resolution.reserve_adjustment_applied,
            reserve_adjustment_reason=reserve_resolution.reserve_adjustment_reason,
            image_count=resolved_image_count,
            image_budget_reserve_tokens=image_budget_reserve,
            final_instruction_budget=reserve_resolution.final_instruction_budget,
        ),
        prefix_messages=tuple(prompt_context.messages),
        tool_list=tuple(tool_list),
    )


def compute_execution_prompt_budget(
    *,
    cfg: AppConfig,
    root: Path,
    mode: str,
    yes: bool,
    deny_write_prefixes: list[str] | None = None,
    allow_write_globs: list[str] | None = None,
    non_interactive: bool = False,
    one_shot_execution: bool = False,
    verification_enabled: bool = True,
    authoritative_verification_commands: list[str] | None = None,
    trusted_system_prompt_override: str | None = None,
    trusted_system_prompt_append: str | None = None,
    untrusted_prompt_prelude: str | None = None,
    subagents_enabled: bool | None = None,
    subagent_depth: int = 0,
    subagent_registry: dict[str, SubagentDefinition] | None = None,
    workspace_binding: WorkspaceBinding | None = None,
    api_key: str | None = None,
    model_registry: ModelRegistry | None = None,
    safety_margin_tokens: int = DEFAULT_EXECUTION_SAFETY_MARGIN_TOKENS,
    execution_response_reserve_tokens: int = DEFAULT_EXECUTION_RESPONSE_RESERVE_TOKENS,
    execution_headroom_reserve_tokens: int = DEFAULT_EXECUTION_HEADROOM_RESERVE_TOKENS,
    minimum_instruction_budget_tokens: int = DEFAULT_MINIMUM_EXECUTION_INSTRUCTION_BUDGET_TOKENS,
    image_count: int = 0,
    image_budget_reserve_tokens_per_image: int = DEFAULT_EXECUTION_IMAGE_RESERVE_TOKENS_PER_IMAGE,
) -> ExecutionPromptBudget:
    return compute_execution_prompt_budget_inputs(
        cfg=cfg,
        root=root,
        mode=mode,
        yes=yes,
        deny_write_prefixes=deny_write_prefixes,
        allow_write_globs=allow_write_globs,
        non_interactive=non_interactive,
        one_shot_execution=one_shot_execution,
        verification_enabled=verification_enabled,
        authoritative_verification_commands=authoritative_verification_commands,
        trusted_system_prompt_override=trusted_system_prompt_override,
        trusted_system_prompt_append=trusted_system_prompt_append,
        untrusted_prompt_prelude=untrusted_prompt_prelude,
        subagents_enabled=subagents_enabled,
        subagent_depth=subagent_depth,
        subagent_registry=subagent_registry,
        workspace_binding=workspace_binding,
        api_key=api_key,
        model_registry=model_registry,
        safety_margin_tokens=safety_margin_tokens,
        execution_response_reserve_tokens=execution_response_reserve_tokens,
        execution_headroom_reserve_tokens=execution_headroom_reserve_tokens,
        minimum_instruction_budget_tokens=minimum_instruction_budget_tokens,
        image_count=image_count,
        image_budget_reserve_tokens_per_image=image_budget_reserve_tokens_per_image,
    ).budget
