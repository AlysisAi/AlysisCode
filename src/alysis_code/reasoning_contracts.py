"""Static per-model reasoning contracts — the wire truth table.

This module records provider-specific reasoning behavior: whether a model's
reasoning is ``always-on`` / ``optional`` / ``none``, the
exact wire spelling that controls it, the allowed value strings, and what
"off" actually does (omit the param, send an explicit disable, impossible, or
— worst — silently swap the model).

Emission rule this table exists to enforce: **allowlist-based emission** —
a client may send a reasoning
parameter only when the contract says this model accepts that exact spelling
and value. ``UNKNOWN`` means send nothing.

The mechanisms here are provider-agnostic; only the data is per-provider.
Clients keep their own payload shaping (e.g. ``anthropic_messages`` builds the
``thinking`` config) and consult this table for the *decision*.
"""

from __future__ import annotations

from dataclasses import dataclass

# Reasoning modes.
ALWAYS_ON = "always-on"
OPTIONAL = "optional"
NONE = "none"
UNKNOWN = "unknown"

# Wire spellings (how the knob is expressed on this surface).
WIRE_REASONING_EFFORT = "reasoning_effort"  # flat string param (openai-chat style)
WIRE_THINKING_TYPE = "thinking_type"  # thinking: {"type": ...}
WIRE_THINKING_ADAPTIVE = "thinking_adaptive"  # thinking:{"type":"adaptive"} + output_config effort
WIRE_BUDGET_TOKENS = "budget_tokens"  # thinking:{"type":"enabled","budget_tokens":N}
WIRE_THINKING_LEVEL = "thinking_level"  # generationConfig.thinkingConfig.thinkingLevel
WIRE_ENABLE_THINKING = "enable_thinking"  # boolean (+ optional thinking_budget)
WIRE_CHAT_TEMPLATE_ENABLE_THINKING = (
    "chat_template_enable_thinking"  # chat_template_kwargs: {enable_thinking: bool}
)
WIRE_REASONING_OBJECT = "reasoning_object"  # reasoning: {effort|max_tokens|enabled|exclude}
WIRE_REASONING_ENABLED = "reasoning_enabled"  # reasoning: {"enabled": bool}
WIRE_NONE = "none"  # no reasoning knob exists on this surface

# Off-path semantics.
OFF_OMIT = "omit-param"  # leaving the param out means no thinking
OFF_EXPLICIT = "explicit-disable"  # a documented disable value exists
OFF_BETWEEN_TOOLS = "between-tools"  # disables up-front thinking, not thinking between tools
OFF_IMPOSSIBLE = "impossible"  # reasoning cannot be turned off (disable = error)
OFF_SWAPS_MODEL = "swaps-model"  # "off" silently substitutes a different model
OFF_UNKNOWN = "unknown"


@dataclass(frozen=True)
class ReasoningContract:
    """What one model accepts on one provider surface."""

    mode: str = UNKNOWN
    wire: str = WIRE_NONE
    values: tuple[str, ...] = ()  # exact allowed effort/level strings ("" knob → empty)
    default: str = ""  # server default value; "" when unknown / not applicable
    off: str = OFF_UNKNOWN
    replay_reasoning_content: bool = False
    accepts_tool_choice_while_reasoning: bool = True
    emits_flat_reasoning_effort: bool = False  # verified on Chat Completions
    notes: str = ""

    def allows_value(self, value: str | None) -> bool:
        normalized = str(value or "").strip().casefold()
        if not normalized:
            return False
        return normalized in self.values

    @property
    def toggleable(self) -> bool:
        return self.mode == OPTIONAL and self.off in {OFF_OMIT, OFF_EXPLICIT}


UNKNOWN_CONTRACT = ReasoningContract()


def reasoning_labels_allowed_by_contract(
    contract: ReasoningContract,
    labels: list[str] | tuple[str, ...],
    *,
    current: str,
) -> list[str]:
    """Filter a reasoning picker to the active model's documented contract."""

    if contract is UNKNOWN_CONTRACT:
        return list(labels)
    effort_wires = {
        WIRE_REASONING_EFFORT,
        WIRE_REASONING_OBJECT,
        WIRE_THINKING_LEVEL,
        WIRE_THINKING_ADAPTIVE,
    }
    filter_by_values = bool(contract.values) and contract.wire in effort_wires
    toggle_only = contract.wire == WIRE_CHAT_TEMPLATE_ENABLE_THINKING
    allowed: list[str] = []
    for label in labels:
        normalized = label.casefold()
        # An inherited Off setting must not survive a switch to an always-on
        # model such as Opus 5.5, even when it is the current saved value.
        if normalized == "off" and contract.mode == ALWAYS_ON and contract.off != OFF_SWAPS_MODEL:
            continue
        if label == current:
            allowed.append(label)
            continue
        if normalized == "off":
            allowed.append(label)
            continue
        if normalized == "auto":
            allowed.append(label)
            continue
        if toggle_only:
            continue
        if filter_by_values and normalized not in contract.values:
            continue
        allowed.append(label)
    return allowed


# Per provider_key: ordered (prefix, contract) rules; first match wins. Exact
# ids sort naturally before shorter prefixes where it matters.
_C = ReasoningContract
_CONTRACTS: dict[str, tuple[tuple[str, ReasoningContract], ...]] = {
    "openai": (
        (
            "gpt-6.1-sol",
            _C(
                mode=ALWAYS_ON,
                wire=WIRE_REASONING_EFFORT,
                values=("low", "medium", "high", "xhigh", "max"),
                default="medium",
                off=OFF_IMPOSSIBLE,
                notes="GPT-6.1 Sol always reasons; function tools require Responses",
            ),
        ),
        # tools + reasoning_effort != "none" on /v1/chat/completions is a 400
        # on the 5.6/5.4 families (report hazard #1) — the *value* allowlist is
        # identical on both endpoints; routing is the client's decision.
        # Source: https://developers.openai.com/api/docs/models/gpt-6-astra —
        # reasoning.effort supports low|medium|high|xhigh|max; no "none".
        (
            "gpt-6-astra",
            _C(
                mode=ALWAYS_ON,
                wire=WIRE_REASONING_EFFORT,
                values=("low", "medium", "high", "xhigh", "max"),
                default="medium",
                off=OFF_IMPOSSIBLE,
                notes="GPT-6 Astra always reasons; Chat Completions and Responses both "
                "supported; 'none' is not a documented effort",
            ),
        ),
        # September 22 releases share the API effort range, but Codex's
        # subscription catalog owns its separate per-model efforts.
        *tuple(
            (
                model,
                _C(
                    mode=OPTIONAL,
                    wire=WIRE_REASONING_EFFORT,
                    values=("none", "low", "medium", "high", "xhigh", "max"),
                    default="medium",
                    off=OFF_EXPLICIT,
                    notes="Chat Completions function calling requires effort none; "
                    "use Responses for tools with reasoning",
                ),
            )
            for model in ("gpt-6-sol", "gpt-6-luna")
        ),
        (
            "gpt-5.6-",
            _C(
                mode=OPTIONAL,
                wire=WIRE_REASONING_EFFORT,
                values=("none", "low", "medium", "high", "xhigh", "max"),
                default="medium",
                off=OFF_EXPLICIT,
                notes="chat surface 400s on tools + effort!=none; 'minimal' is dead on 5.x",
            ),
        ),
        (
            "gpt-5.5",
            _C(
                mode=OPTIONAL,
                wire=WIRE_REASONING_EFFORT,
                values=("none", "low", "medium", "high", "xhigh"),
                off=OFF_EXPLICIT,
                notes="previous flagship (snapshot gpt-5.5-2026-04-23); effort none..xhigh "
                "per the model page — treat the chat-surface tools+effort 400 as live",
            ),
        ),
        (
            "gpt-5.4-",
            _C(
                mode=OPTIONAL,
                wire=WIRE_REASONING_EFFORT,
                values=("none", "low", "medium", "high", "xhigh", "max"),
                off=OFF_EXPLICIT,
                notes="same chat-surface tools+effort 400 as 5.6 when effort raised",
            ),
        ),
        (
            "gpt-5.3-codex",
            _C(
                mode=ALWAYS_ON,
                wire=WIRE_REASONING_EFFORT,
                values=("low", "medium", "high", "xhigh"),
                off=OFF_IMPOSSIBLE,
                notes="effort 'none' is a 400 unsupported_value",
            ),
        ),
    ),
    "anthropic": (
        # https://platform.claude.com/docs/en/build-with-claude/effort
        (
            "claude-haiku-5-5",
            _C(
                mode=OPTIONAL,
                wire=WIRE_THINKING_ADAPTIVE,
                values=("low", "medium", "high", "xhigh", "max"),
                default="medium",
                off=OFF_EXPLICIT,
                notes="adaptive by default; disabled only at high or below; forced tools skip thinking",
            ),
        ),
        # Source: https://platform.claude.com/docs/en/models/fable-5-1/overview
        (
            "claude-fable-5-1",
            _C(
                mode=ALWAYS_ON,
                wire=WIRE_THINKING_ADAPTIVE,
                values=("low", "medium", "high", "xhigh", "max"),
                default="high",
                off=OFF_IMPOSSIBLE,
                accepts_tool_choice_while_reasoning=False,
                notes="always adaptive; forced tool_choice (any/tool) returns an error; "
                "its thinking blocks cannot be replayed to earlier models",
            ),
        ),
        (
            "claude-fable-5",
            _C(
                mode=ALWAYS_ON,
                wire=WIRE_THINKING_ADAPTIVE,
                values=("low", "medium", "high", "xhigh", "max"),
                off=OFF_IMPOSSIBLE,
                notes="thinking:{'type':'disabled'} is rejected; always adaptive",
            ),
        ),
        (
            "claude-haiku-4-5",
            _C(
                mode=OPTIONAL,
                wire=WIRE_BUDGET_TOKENS,
                off=OFF_EXPLICIT,
                notes="extended thinking only (budget_tokens >= 1024); no effort param",
            ),
        ),
        # https://platform.claude.com/docs/en/models/sonnet-5-5/overview
        (
            "claude-sonnet-5-5",
            _C(
                mode=OPTIONAL,
                wire=WIRE_THINKING_ADAPTIVE,
                values=("low", "medium", "high", "xhigh", "max"),
                default="high",
                off=OFF_BETWEEN_TOOLS,
                accepts_tool_choice_while_reasoning=False,
                notes="adaptive by default; between_tools disables only up-front thinking "
                "and requires high effort or below; forced tools unsupported",
            ),
        ),
        (
            "claude-sonnet-5",
            _C(
                mode=OPTIONAL,
                wire=WIRE_THINKING_ADAPTIVE,
                values=("low", "medium", "high", "xhigh", "max"),
                off=OFF_OMIT,
                notes="extended shape = 400; non-default temperature/top_p/top_k = 400",
            ),
        ),
        # Must precede the Opus 5 prefix: 5.5 removed thinking-off support.
        # https://platform.claude.com/docs/en/models/opus-5-5/overview
        (
            "claude-opus-5-5",
            _C(
                mode=ALWAYS_ON,
                wire=WIRE_THINKING_ADAPTIVE,
                values=("low", "medium", "high", "xhigh", "max"),
                default="medium",
                off=OFF_IMPOSSIBLE,
                accepts_tool_choice_while_reasoning=False,
                notes="always adaptive; forced tool_choice (any/tool) returns an error",
            ),
        ),
        (
            "claude-opus-5",
            _C(
                mode=OPTIONAL,
                wire=WIRE_THINKING_ADAPTIVE,
                values=("low", "medium", "high", "xhigh", "max"),
                default="high",
                off=OFF_EXPLICIT,
                notes="thinks by DEFAULT — omitting the param is adaptive, not off "
                "(unlike 4.8/4.7); disable is accepted only at effort <= high, so "
                "'disabled' + xhigh/max = 400; extended shape and non-default "
                "sampling params = 400",
            ),
        ),
        (
            "claude-opus-4-8",
            _C(
                mode=OPTIONAL,
                wire=WIRE_THINKING_ADAPTIVE,
                values=("low", "medium", "high", "xhigh", "max"),
                off=OFF_OMIT,
                notes="extended shape = 400; non-default sampling params = 400",
            ),
        ),
        (
            "claude-opus-4-7",
            _C(
                mode=OPTIONAL,
                wire=WIRE_THINKING_ADAPTIVE,
                values=("low", "medium", "high", "xhigh", "max"),
                off=OFF_OMIT,
                notes="extended shape = 400; non-default sampling params = 400",
            ),
        ),
    ),
    "gemini": (
        (
            "gemini-3.8-flash",
            _C(
                mode=ALWAYS_ON,
                wire=WIRE_THINKING_LEVEL,
                values=("low", "medium", "high"),
                default="medium",
                off=OFF_IMPOSSIBLE,
                notes="'minimal' errors on Gemini 3.8 Flash, same as 3.7",
            ),
        ),
        (
            "gemini-3.7-flash",
            _C(
                mode=ALWAYS_ON,
                wire=WIRE_THINKING_LEVEL,
                values=("low", "medium", "high"),
                default="medium",
                off=OFF_IMPOSSIBLE,
                notes="'minimal' is explicitly unsupported on Gemini 3.7 Flash",
            ),
        ),
        (
            "gemini-3.1-pro",
            _C(
                mode=ALWAYS_ON,
                wire=WIRE_THINKING_LEVEL,
                values=("low", "medium", "high"),
                off=OFF_IMPOSSIBLE,
                notes="'minimal' documented Not supported on 3.1 Pro",
            ),
        ),
        (
            "gemini-3",
            _C(
                mode=ALWAYS_ON,
                wire=WIRE_THINKING_LEVEL,
                values=("minimal", "low", "medium", "high"),
                off=OFF_IMPOSSIBLE,
                notes="no 3.x model can disable thinking; economy calls pin 'minimal'",
            ),
        ),
    ),
    "deepseek": (
        (
            "deepseek-",
            _C(
                mode=OPTIONAL,
                wire=WIRE_REASONING_EFFORT,
                values=("low", "high", "max"),
                default="high",
                off=OFF_EXPLICIT,
                replay_reasoning_content=True,
                accepts_tool_choice_while_reasoning=False,
                notes="low|high|max are the exact effort values; thinking:{type} is the "
                "separate on/off toggle; "
                "reasoning_content is required across every tools-enabled assistant turn; "
                "thinking mode rejects tool_choice; compatibility aliases are not exposed",
            ),
        ),
    ),
    # Hosted models retain their upstream reasoning and replay contracts.
    "alysis": (
        (
            "minimax-m3.1-flash",
            _C(
                mode=ALWAYS_ON,
                wire=WIRE_REASONING_EFFORT,
                values=("low",),
                default="low",
                off=OFF_IMPOSSIBLE,
                replay_reasoning_content=True,
                emits_flat_reasoning_effort=True,
                notes="Together hosted: tested text/tool contract; higher effort settings remain unverified",
            ),
        ),
        (
            "gpt-oss-120b",
            _C(
                mode=ALWAYS_ON,
                wire=WIRE_REASONING_EFFORT,
                values=("low",),
                default="low",
                off=OFF_IMPOSSIBLE,
                replay_reasoning_content=True,
                emits_flat_reasoning_effort=True,
                notes="Together hosted: tested text/tool contract; higher effort settings remain unverified",
            ),
        ),
        (
            "reflection-beam",
            _C(
                mode=ALWAYS_ON,
                wire=WIRE_REASONING_EFFORT,
                values=("low",),
                default="low",
                off=OFF_IMPOSSIBLE,
                replay_reasoning_content=True,
                emits_flat_reasoning_effort=True,
                notes="Together hosted: tested text/tool contract; higher effort settings remain unverified",
            ),
        ),
        (
            "nemotron-3.5-super-vl",
            _C(
                mode=ALWAYS_ON,
                wire=WIRE_REASONING_EFFORT,
                values=("low",),
                default="low",
                off=OFF_IMPOSSIBLE,
                replay_reasoning_content=True,
                emits_flat_reasoning_effort=True,
                notes="Together hosted: tested text/tool contract; higher effort settings remain unverified",
            ),
        ),
        (
            "qwen3.8-2.4t",
            _C(
                mode=ALWAYS_ON,
                wire=WIRE_REASONING_EFFORT,
                values=("low",),
                default="low",
                off=OFF_IMPOSSIBLE,
                replay_reasoning_content=True,
                emits_flat_reasoning_effort=True,
                notes="Together hosted: tested text/tool contract; higher effort settings remain unverified",
            ),
        ),
        (
            "kimi-k3",
            _C(
                mode=ALWAYS_ON,
                wire=WIRE_REASONING_EFFORT,
                values=("low",),
                default="low",
                off=OFF_IMPOSSIBLE,
                replay_reasoning_content=True,
                emits_flat_reasoning_effort=True,
                notes="Together hosted: tested text/tool contract; higher effort settings remain unverified",
            ),
        ),
        (
            "muse-spark-open",
            _C(
                mode=ALWAYS_ON,
                wire=WIRE_REASONING_EFFORT,
                values=("low",),
                default="low",
                off=OFF_IMPOSSIBLE,
                replay_reasoning_content=True,
                emits_flat_reasoning_effort=True,
                notes="Together hosted: tested text/tool contract; higher effort settings remain unverified",
            ),
        ),
        (
            "claude-haiku-5-5",
            _C(
                mode=OPTIONAL,
                wire=WIRE_THINKING_ADAPTIVE,
                values=("low", "medium", "high", "xhigh", "max"),
                default="medium",
                off=OFF_EXPLICIT,
                notes="native Messages; disabled only at high or below; shared Free credits",
            ),
        ),
        (
            "gpt-6.1-sol",
            _C(
                mode=ALWAYS_ON,
                wire=WIRE_REASONING_OBJECT,
                values=("low", "medium", "high", "xhigh", "max"),
                default="medium",
                off=OFF_IMPOSSIBLE,
                notes="paid hosted Responses with stateless reasoning and function-tool replay",
            ),
        ),
        (
            "claude-sonnet-5-5",
            _C(
                mode=OPTIONAL,
                wire=WIRE_THINKING_ADAPTIVE,
                values=("low", "medium", "high", "xhigh", "max"),
                default="medium",
                off=OFF_BETWEEN_TOOLS,
                accepts_tool_choice_while_reasoning=False,
                notes="native Messages; reduced thinking means between_tools at high effort; forced tools unsupported",
            ),
        ),
        (
            "gpt-6-luna",
            _C(
                mode=OPTIONAL,
                wire=WIRE_REASONING_OBJECT,
                values=("none", "low", "medium", "high", "xhigh", "max"),
                default="medium",
                off=OFF_EXPLICIT,
                notes="hosted Responses with full stateless reasoning and function-tool replay",
            ),
        ),
        (
            "deepseek-",
            _C(
                mode=OPTIONAL,
                wire=WIRE_REASONING_EFFORT,
                values=("low", "high", "max"),
                default="high",
                off=OFF_EXPLICIT,
                replay_reasoning_content=True,
                accepts_tool_choice_while_reasoning=False,
                notes="hosted proxy passes reasoning fields through to Together AI's DeepSeek "
                "unchanged (route drop/extra adapt them); mirrors the deepseek provider contract",
            ),
        ),
        (
            "glm-5.3-flash",
            _C(
                mode=ALWAYS_ON,
                wire=WIRE_REASONING_EFFORT,
                values=("low", "high", "max"),
                default="max",
                off=OFF_IMPOSSIBLE,
                emits_flat_reasoning_effort=True,
                replay_reasoning_content=True,
                notes="Z.ai hosted pay-as-you-go; preserved thinking requires complete reasoning replay",
            ),
        ),
        (
            "glm-5.3",
            _C(
                mode=ALWAYS_ON,
                wire=WIRE_REASONING_EFFORT,
                values=("low",),
                default="low",
                off=OFF_IMPOSSIBLE,
                replay_reasoning_content=True,
                emits_flat_reasoning_effort=True,
                notes="Together hosted: tested text/tool contract; higher effort settings remain unverified",
            ),
        ),
    ),
    "nvidia": (
        (
            "deepseek-ai/deepseek-v4-pro",
            _C(
                mode=OPTIONAL,
                wire=WIRE_REASONING_EFFORT,
                values=("high", "max"),
                default="high",
                off=OFF_EXPLICIT,
                notes="NVIDIA-hosted DeepSeek V4 Pro accepts none|high|max on the flat "
                "reasoning_effort field",
            ),
        ),
        (
            "deepseek-ai/deepseek-v4-flash",
            _C(
                mode=OPTIONAL,
                wire=WIRE_REASONING_EFFORT,
                values=("high", "max"),
                default="high",
                off=OFF_EXPLICIT,
                notes="NVIDIA-hosted DeepSeek V4 Flash accepts none|high|max on the flat "
                "reasoning_effort field",
            ),
        ),
        (
            "nvidia/nemotron-3-super-120b-a12b",
            _C(
                mode=OPTIONAL,
                wire=WIRE_REASONING_EFFORT,
                values=("low", "high"),
                default="high",
                off=OFF_EXPLICIT,
                notes="NVIDIA's hosted endpoint accepts none|low|high and returns private "
                "reasoning on the structured reasoning_content channel",
            ),
        ),
        (
            "nvidia/nemotron-3-ultra-550b-a55b",
            _C(
                mode=OPTIONAL,
                wire=WIRE_REASONING_EFFORT,
                values=("medium", "high"),
                default="high",
                off=OFF_EXPLICIT,
                notes="NVIDIA's hosted endpoint accepts none|medium|high",
            ),
        ),
        (
            "nvidia/nemotron-3-nano-30b-a3b",
            _C(
                mode=OPTIONAL,
                wire=WIRE_CHAT_TEMPLATE_ENABLE_THINKING,
                off=OFF_EXPLICIT,
                notes="the hosted endpoint documents the chat-template toggle, not a flat "
                "reasoning_effort enum",
            ),
        ),
        # Source: https://build.nvidia.com/nvidia/nemotron-3.5-lightning-30b-a3b
        # The build page shows enable_thinking + reasoning_budget kwargs but
        # does not document their wire location; probe before emitting.
        (
            "nvidia/nemotron-3.5-lightning-30b-a3b",
            _C(
                mode=UNKNOWN,
                wire=WIRE_ENABLE_THINKING,
                off=OFF_UNKNOWN,
                notes="documented as enable_thinking + reasoning_budget on the hosted "
                "sample; exact placement unverified — send nothing until probed",
            ),
        ),
        (
            "moonshotai/kimi-k3",
            _C(
                mode=ALWAYS_ON,
                wire=WIRE_REASONING_EFFORT,
                off=OFF_IMPOSSIBLE,
                notes="always-on upstream; NVIDIA's page mentions effort up to 'max' but "
                "publishes no value set — emit nothing until probed",
            ),
        ),
    ),
    "qwen": (
        (
            "qwen3.8-max",
            _C(
                mode=OPTIONAL,
                wire=WIRE_REASONING_EFFORT,
                values=("low", "medium", "xhigh"),
                default="xhigh",
                off=OFF_EXPLICIT,
                replay_reasoning_content=True,
                notes="effort is flat; enable_thinking is the separate on/off toggle",
            ),
        ),
        # Same documented effort enum on the Flash route.
        # https://docs.modelstudio.console.alibabacloud.com/en/model-studio/qwen-api-via-openai-chat-completions
        (
            "qwen3.8-flash",
            _C(
                mode=OPTIONAL,
                wire=WIRE_REASONING_EFFORT,
                values=("low", "medium", "xhigh"),
                default="xhigh",
                off=OFF_EXPLICIT,
                replay_reasoning_content=True,
                notes="effort is flat; enable_thinking is the separate on/off toggle",
            ),
        ),
        (
            "qwen3-coder",
            _C(mode=NONE, wire=WIRE_NONE, off=OFF_OMIT, notes="coder line has no thinking"),
        ),
        (
            "qwen",
            _C(
                mode=OPTIONAL,
                wire=WIRE_ENABLE_THINKING,
                off=OFF_EXPLICIT,
                notes="3.7 line thinks by default — omitting the flag bills thinking tokens",
            ),
        ),
    ),
    "zhipu": (
        # Source: https://docs.bigmodel.cn/cn/guide/models/text/glm-5.3 and
        # .../vlm/glm-5.3-flash — thinking.type=disabled is rejected on both;
        # reasoning_effort low|high|max (default max) is the only knob.
        (
            "glm-5.3",
            _C(
                mode=ALWAYS_ON,
                wire=WIRE_REASONING_EFFORT,
                values=("low", "high", "max"),
                default="max",
                off=OFF_IMPOSSIBLE,
                emits_flat_reasoning_effort=True,
                notes="covers glm-5.3-flash too; thinking:{'type':'disabled'} = error",
            ),
        ),
        (
            "glm-5.2",
            _C(
                mode=OPTIONAL,
                wire=WIRE_REASONING_EFFORT,
                values=("none", "high", "max"),
                default="max",
                off=OFF_EXPLICIT,
                emits_flat_reasoning_effort=True,
                notes="canonical levels from https://docs.z.ai/guides/capabilities/thinking; "
                "none disables thinking, aliases are not separate depth levels",
            ),
        ),
        (
            "glm-4.7-flash",
            _C(mode=OPTIONAL, wire=WIRE_THINKING_TYPE, off=OFF_EXPLICIT),
        ),
        (
            "glm-4.7",
            _C(
                mode=ALWAYS_ON,
                wire=WIRE_THINKING_TYPE,
                off=OFF_IMPOSSIBLE,
                notes="thinks compulsorily despite a documented toggle",
            ),
        ),
        (
            "glm-",
            _C(mode=OPTIONAL, wire=WIRE_THINKING_TYPE, off=OFF_EXPLICIT),
        ),
    ),
    "zai_coding_plan": (
        # Preserved thinking is enabled by default on this surface and requires
        # complete reasoning_content replay, including ordinary assistant turns.
        # https://docs.z.ai/guides/capabilities/thinking-mode
        # Sources: https://docs.z.ai/guides/llm/glm-5.3
        # https://docs.z.ai/guides/vlm/glm-5.3-flash
        # https://docs.z.ai/devpack/overview — the plan serves exactly glm-5.3
        # and glm-5.3-flash; glm-5.2/5.1 route to 5.3 and glm-5-turbo/4.7 to
        # 5.3-flash server-side, so one prefix rule covers every plan id.
        (
            "glm-5.3",
            _C(
                mode=ALWAYS_ON,
                wire=WIRE_REASONING_EFFORT,
                values=("low", "high", "max"),
                default="max",
                off=OFF_IMPOSSIBLE,
                emits_flat_reasoning_effort=True,
                replay_reasoning_content=True,
                notes=(
                    "Coding Plan surface: disabling thinking is coerced to low rather than "
                    "turning reasoning off; aliases are normalized server-side"
                ),
            ),
        ),
        (
            "glm-",
            _C(
                mode=ALWAYS_ON,
                wire=WIRE_REASONING_EFFORT,
                values=("low", "high", "max"),
                default="max",
                off=OFF_IMPOSSIBLE,
                emits_flat_reasoning_effort=True,
                replay_reasoning_content=True,
                notes="every other glm id is server-routed to glm-5.3 or glm-5.3-flash",
            ),
        ),
    ),
    "moonshot": (
        # Platform surface (api.moonshot.ai / .cn). The kimi-code membership
        # surface has different contracts — provider_key "kimi-code" below.
        # Source: https://www.kimi.com/help/kimi-api/api-model-selection
        (
            "kimi-k2.7-code",
            _C(
                mode=ALWAYS_ON,
                wire=WIRE_THINKING_TYPE,
                values=("enabled",),
                off=OFF_IMPOSSIBLE,
                notes="thinking:{'type':'disabled'} = 400 'invalid thinking'; "
                "temperature!=1.0 or top_p!=0.95 = 400",
            ),
        ),
        (
            "kimi-k3",
            _C(
                mode=ALWAYS_ON,
                wire=WIRE_REASONING_EFFORT,
                values=("low", "high", "max"),
                default="max",
                off=OFF_IMPOSSIBLE,
                emits_flat_reasoning_effort=True,
                notes="uses reasoning_effort, not thinking; do not send the K2.x param",
            ),
        ),
        (
            "kimi-k2.6",
            _C(
                mode=OPTIONAL,
                wire=WIRE_THINKING_TYPE,
                off=OFF_EXPLICIT,
                notes="the only toggleable model on this surface; replay reasoning_content "
                "in tool loops",
            ),
        ),
    ),
    "kimi-code": (
        # Source: https://www.kimi.com/code/docs/en/kimi-code/models.html
        (
            "k3",
            _C(
                mode=ALWAYS_ON,
                wire=WIRE_REASONING_EFFORT,
                values=("low", "high", "max"),
                default="high",
                off=OFF_SWAPS_MODEL,
                emits_flat_reasoning_effort=True,
                notes="disabling thinking silently routes the request to K2.6 — surface "
                "the substitution, never treat it as a speed knob",
            ),
        ),
        (
            "kimi-for-coding",
            _C(
                mode=ALWAYS_ON,
                wire=WIRE_THINKING_TYPE,
                values=("enabled",),
                off=OFF_SWAPS_MODEL,
                notes="thinking defaults on; 'off' is a silent K2.6 substitution",
            ),
        ),
    ),
    "minimax": (
        # https://platform.minimax.io/docs/api-reference/text-openai-api
        (
            "MiniMax-M3.1-Flash-Preview",
            _C(
                mode=ALWAYS_ON,
                wire=WIRE_REASONING_EFFORT,
                values=("low", "medium", "high", "xhigh", "max"),
                default="max",
                off=OFF_IMPOSSIBLE,
                emits_flat_reasoning_effort=True,
            ),
        ),
        (
            "MiniMax-M3",
            _C(
                mode=OPTIONAL,
                wire=WIRE_THINKING_TYPE,
                values=("adaptive", "disabled"),
                default="adaptive",
                off=OFF_EXPLICIT,
                notes="Chat Completions defaults on; https://platform.minimax.io/docs/api-reference/text-openai-api",
            ),
        ),
        (
            "MiniMax-M2",
            _C(
                mode=NONE,
                wire=WIRE_NONE,
                off=OFF_OMIT,
                notes="M2.x exposes no reasoning control — send nothing",
            ),
        ),
    ),
    "bytedance": (
        (
            "doubao-",
            _C(
                mode=UNKNOWN,
                wire=WIRE_THINKING_TYPE,
                off=OFF_UNKNOWN,
                notes="probe-gated; reasoning_effort 'high' live-observed as 400 "
                "InvalidParameter on the coding surface — send nothing until probed",
            ),
        ),
    ),
    "groq": (
        # Source: https://console.groq.com/docs/reasoning
        (
            "groq/compound",
            _C(
                mode=NONE,
                wire=WIRE_NONE,
                off=OFF_OMIT,
                notes="also rejects client-provided tools entirely",
            ),
        ),
        # Source: https://console.groq.com/docs/model/qwen/qwen3.8-27b
        (
            "qwen/qwen3.8-27b",
            _C(
                mode=OPTIONAL,
                wire=WIRE_REASONING_EFFORT,
                values=("none", "low", "medium", "high", "default"),
                off=OFF_EXPLICIT,
                emits_flat_reasoning_effort=True,
            ),
        ),
        (
            "qwen/qwen3.6-27b",
            _C(
                mode=OPTIONAL,
                wire=WIRE_REASONING_EFFORT,
                values=("none", "default"),
                off=OFF_EXPLICIT,
                emits_flat_reasoning_effort=True,
            ),
        ),
        (
            "openai/gpt-oss-",
            _C(
                mode=ALWAYS_ON,
                wire=WIRE_REASONING_EFFORT,
                values=("low", "medium", "high"),
                default="medium",
                off=OFF_IMPOSSIBLE,
                emits_flat_reasoning_effort=True,
                notes="reasoning_format 'raw' + tools/JSON = 400; "
                "include_reasoning + reasoning_format together = 400",
            ),
        ),
    ),
    "cerebras": (
        # Source: https://inference-docs.cerebras.ai/api-reference/chat-completions
        (
            "gpt-oss-120b",
            _C(
                mode=ALWAYS_ON,
                wire=WIRE_REASONING_EFFORT,
                values=("low", "medium", "high"),
                default="medium",
                off=OFF_IMPOSSIBLE,
                emits_flat_reasoning_effort=True,
                notes="no 'none'; disable_reasoning dead since 2026-07-21; "
                "tools + response_format together = rejected",
            ),
        ),
        # zai-glm-4.7 was deprecated 2026-08-17 and removed from the public
        # catalog; the preset aliases it to gpt-oss-120b. The contract stays so
        # a saved config that bypasses the alias still emits nothing unsafe.
        (
            "zai-glm-4.7",
            _C(
                mode=OPTIONAL,
                wire=WIRE_REASONING_EFFORT,
                values=("none",),
                off=OFF_EXPLICIT,
                emits_flat_reasoning_effort=True,
                notes="deprecated 2026-08-17; only 'none' was ever documented as a "
                "control value on this Cerebras model",
            ),
        ),
        (
            "gemma-4-31b",
            _C(
                mode=OPTIONAL,
                wire=WIRE_REASONING_EFFORT,
                values=("none", "low", "medium", "high"),
                default="none",
                off=OFF_EXPLICIT,
                emits_flat_reasoning_effort=True,
            ),
        ),
    ),
    "mistral": (
        (
            "mistral-medium-",
            _C(
                mode=OPTIONAL,
                wire=WIRE_REASONING_EFFORT,
                values=("none", "high"),
                off=OFF_EXPLICIT,
                notes="only 'high'/'none' have documented semantics; content becomes "
                "typed chunks (thinking/text) while reasoning — replay ThinkChunks",
            ),
        ),
        (
            "mistral-small-",
            _C(
                mode=OPTIONAL,
                wire=WIRE_REASONING_EFFORT,
                values=("none", "high"),
                off=OFF_EXPLICIT,
            ),
        ),
        ("mistral-large-", _C(mode=NONE, wire=WIRE_NONE, off=OFF_OMIT)),
        ("codestral-", _C(mode=NONE, wire=WIRE_NONE, off=OFF_OMIT)),
        ("ministral-", _C(mode=NONE, wire=WIRE_NONE, off=OFF_OMIT)),
        # https://docs.mistral.ai/studio/conversations/reasoning
        (
            "zai-glm-5-3",
            _C(
                mode=ALWAYS_ON,
                wire=WIRE_REASONING_EFFORT,
                values=("low", "high", "max"),
                off=OFF_IMPOSSIBLE,
                emits_flat_reasoning_effort=True,
            ),
        ),
        # Other hosted GLM routes still require their own documented values.
        # https://docs.mistral.ai/models/zai-glm-5-2
        (
            "zai-glm-",
            _C(
                mode=UNKNOWN,
                wire=WIRE_REASONING_EFFORT,
                off=OFF_UNKNOWN,
                notes="third-party route; reasoning controls undocumented on Mistral — "
                "send nothing until probed",
            ),
        ),
    ),
    "xai": (
        # Grok 4.7 launch verification (2026-09-21): ChatRequest in xAI's
        # OpenAPI schema explicitly accepts the flat reasoning_effort field.
        # 4.5 and 4.6 revalidated against the same reasoning guide, 2026-10-06.
        # Sources: https://docs.x.ai/developers/model-capabilities/text/reasoning
        # https://api.x.ai/api-docs/openapi.json (ChatRequest.reasoning_effort)
        (
            "grok-4.7",
            _C(
                mode=ALWAYS_ON,
                wire=WIRE_REASONING_EFFORT,
                values=("low", "medium", "high", "xhigh"),
                default="high",
                off=OFF_IMPOSSIBLE,
                emits_flat_reasoning_effort=True,
                notes="stop/presence_penalty/frequency_penalty = documented error",
            ),
        ),
        (
            "grok-4.6",
            _C(
                mode=ALWAYS_ON,
                wire=WIRE_REASONING_EFFORT,
                values=("low", "medium", "high", "xhigh"),
                default="high",
                off=OFF_IMPOSSIBLE,
                emits_flat_reasoning_effort=True,
                notes="stop/presence_penalty/frequency_penalty = documented error",
            ),
        ),
        (
            "grok-4.5",
            _C(
                mode=ALWAYS_ON,
                wire=WIRE_REASONING_EFFORT,
                values=("low", "medium", "high"),
                default="high",
                off=OFF_IMPOSSIBLE,
                emits_flat_reasoning_effort=True,
                notes="stop/presence_penalty/frequency_penalty = documented error",
            ),
        ),
        (
            "grok-build-0.1",
            _C(mode=ALWAYS_ON, wire=WIRE_REASONING_EFFORT, off=OFF_IMPOSSIBLE),
        ),
        (
            "grok-4.20-0309-reasoning",
            _C(mode=ALWAYS_ON, wire=WIRE_NONE, off=OFF_IMPOSSIBLE, notes="mode-by-slug"),
        ),
        (
            "grok-4.20-0309-non-reasoning",
            _C(mode=NONE, wire=WIRE_NONE, off=OFF_OMIT, notes="mode-by-slug"),
        ),
        (
            "grok-4.3",
            _C(
                mode=OPTIONAL,
                wire=WIRE_REASONING_EFFORT,
                default="low",
                off=OFF_EXPLICIT,
                notes="has an off switch; full value set unpublished — emit only "
                "values confirmed by probe",
            ),
        ),
    ),
    "cohere": (
        # Source: https://docs.cohere.com/v2/docs/compatibility-api
        (
            "command-a-reasoning-",
            _C(
                mode=OPTIONAL,
                wire=WIRE_REASONING_EFFORT,
                values=("none", "high"),
                off=OFF_EXPLICIT,
                emits_flat_reasoning_effort=True,
                notes="medium/low documented not supported on the compat surface; native "
                "thinking param likely does not pass through /compatibility/v1",
            ),
        ),
        ("command-", _C(mode=NONE, wire=WIRE_NONE, off=OFF_OMIT)),
    ),
    "openrouter": (
        (
            "anthropic/claude-opus-5.5",
            _C(
                mode=ALWAYS_ON,
                wire=WIRE_REASONING_OBJECT,
                off=OFF_IMPOSSIBLE,
                accepts_tool_choice_while_reasoning=False,
                notes="Opus 5.5 always thinks; OpenRouter uses its reasoning object",
            ),
        ),
        (
            "",
            _C(
                mode=UNKNOWN,
                wire=WIRE_REASONING_OBJECT,
                off=OFF_UNKNOWN,
                notes="drive from live /api/v1/models: per-model supported_efforts + "
                "default_effort; effort and max_tokens are mutually exclusive",
            ),
        ),
    ),
    "perplexity": (
        # Agent API (Responses format). Source:
        # https://docs.perplexity.ai/docs/agent-api/migrate-from-sonar/how-to —
        # reasoning_effort maps to reasoning.effort, but per-route value sets
        # are unpublished; the Sonar route itself exposes no reasoning knob.
        (
            "perplexity/sonar",
            _C(mode=NONE, wire=WIRE_NONE, off=OFF_OMIT, notes="search model; no effort knob"),
        ),
        (
            "sonar",
            _C(mode=NONE, wire=WIRE_NONE, off=OFF_OMIT, notes="retired Sonar chat id"),
        ),
        (
            "perplexity/",
            _C(
                mode=UNKNOWN,
                wire=WIRE_REASONING_OBJECT,
                off=OFF_UNKNOWN,
                notes="reasoning.effort is accepted on the Agent API; allowed values per "
                "hosted route are unpublished — send nothing until probed",
            ),
        ),
    ),
    "together": (
        (
            "deepseek-ai/DeepSeek-V4-Pro",
            _C(
                mode=OPTIONAL,
                wire=WIRE_REASONING_EFFORT,
                values=("high", "max"),
                default="high",
                off=OFF_EXPLICIT,
                notes="reasoning_effort controls depth; reasoning:{enabled:false} is the "
                "separate off wire shape; reasoning must be replayed after tool calls",
            ),
        ),
        (
            "moonshotai/Kimi-K2.7-Code",
            _C(
                mode=ALWAYS_ON,
                wire=WIRE_REASONING_ENABLED,
                off=OFF_IMPOSSIBLE,
                notes="thinking cannot be disabled on Together (id left serverless "
                "2026-08-27; kept for dedicated-endpoint users)",
            ),
        ),
        (
            "moonshotai/Kimi-K3",
            _C(
                mode=ALWAYS_ON,
                wire=WIRE_REASONING_ENABLED,
                off=OFF_IMPOSSIBLE,
                notes="always-on upstream; Together publishes no toggle for K3 — emit "
                "no reasoning field",
            ),
        ),
        (
            "Qwen/",
            _C(
                mode=UNKNOWN,
                wire=WIRE_REASONING_ENABLED,
                off=OFF_UNKNOWN,
                notes="Qwen3.5-9B is hybrid-thinking upstream; Together's wire shape is "
                "unverified — probe before emitting",
            ),
        ),
        (
            "MiniMaxAI/MiniMax-M3",
            _C(
                mode=OPTIONAL,
                wire=WIRE_REASONING_ENABLED,
                off=OFF_EXPLICIT,
                notes="toggleable per Together's model page; exact wire spelling "
                "unconfirmed — probe before emitting",
            ),
        ),
        (
            "deepseek-ai/DeepSeek-V4-Flash",
            UNKNOWN_CONTRACT,
        ),
        (
            "deepseek-ai/",
            _C(
                mode=OPTIONAL,
                wire=WIRE_REASONING_ENABLED,
                off=OFF_EXPLICIT,
                notes="effort low/medium coerce to high",
            ),
        ),
        (
            "openai/gpt-oss-",
            _C(mode=OPTIONAL, wire=WIRE_REASONING_ENABLED, off=OFF_EXPLICIT),
        ),
        (
            "zai-org/",
            _C(
                mode=UNKNOWN,
                wire=WIRE_REASONING_ENABLED,
                off=OFF_UNKNOWN,
                notes="not classified by the capability report — probe before emitting",
            ),
        ),
    ),
    "fireworks": (
        (
            "accounts/fireworks/models/deepseek-v4-",
            _C(
                mode=OPTIONAL,
                wire=WIRE_REASONING_EFFORT,
                values=("none", "high", "max"),
                default="high",
                off=OFF_EXPLICIT,
                emits_flat_reasoning_effort=True,
                notes="low/medium normalize to high and xhigh normalizes to max; expose only "
                "the three distinct wire behaviors",
            ),
        ),
        # The endpoint accepts reasoning_effort, but the current reference does
        # not define values for MiniMax M3. Leave this model probe-gated.
        # Source: https://docs.fireworks.ai/api-reference/post-chatcompletions
        (
            "accounts/fireworks/models/minimax-m3",
            _C(
                mode=OPTIONAL,
                wire=WIRE_REASONING_EFFORT,
                off=OFF_UNKNOWN,
                notes="doc-classified adaptive; thinking + reasoning_effort together = "
                "documented validation error",
            ),
        ),
        (
            "accounts/fireworks/models/",
            _C(
                mode=UNKNOWN,
                wire=WIRE_REASONING_EFFORT,
                off=OFF_UNKNOWN,
                notes="per-model acceptance probe-gated (5 of 6 unknown)",
            ),
        ),
    ),
}


def reasoning_contract_for(
    provider_key: str | None, model: str | None, *, preset_key: str | None = None
) -> ReasoningContract:
    """Resolve the reasoning contract for ``model`` on ``provider_key``.

    ``preset_key`` takes precedence when it names a distinct surface — e.g. the
    ``kimi-code`` preset carries ``provider_key="moonshot"``, but the membership
    endpoint's contracts differ from the platform's (same vendor, different
    rules; "off" even means different things). First matching prefix rule wins;
    anything unmatched returns :data:`UNKNOWN_CONTRACT` — and per the allowlist
    emission rule, unknown means *send no reasoning parameter at all*.
    """
    name = str(model or "").strip()
    if not name:
        return UNKNOWN_CONTRACT
    folded = name.casefold()
    for key in (preset_key, provider_key):
        rules = _CONTRACTS.get(str(key or "").strip())
        if not rules:
            continue
        for prefix, contract in rules:
            if folded.startswith(prefix.casefold()):
                return contract
    return UNKNOWN_CONTRACT


def reasoning_off_is_safe(provider_key: str | None, model: str | None) -> bool:
    """Whether a reasoning-off toggle can be honoured without error or deceit."""
    return reasoning_contract_for(provider_key, model).toggleable


def reasoning_off_hazard(provider_key: str | None, model: str | None) -> str:
    """Human-readable reason a reasoning-off toggle must not be emitted.

    Empty string when off is safe. Drives the TUI warning (and the
    ``doctor --live`` assertion mode, per implementation-order step 7).
    """
    contract = reasoning_contract_for(provider_key, model)
    if contract.toggleable:
        return ""
    if contract.off == OFF_SWAPS_MODEL:
        return f"disabling reasoning on {model} silently substitutes a different model"
    if contract.off == OFF_IMPOSSIBLE:
        return f"{model} cannot disable reasoning; lowest effort is the floor"
    if contract.off == OFF_BETWEEN_TOOLS:
        return f"{model} can disable up-front thinking only; thinking between tools remains active"
    if contract.mode == NONE:
        return f"{model} has no reasoning control; nothing to disable"
    return f"reasoning contract for {model} is unknown; emitting a disable flag is unsafe"


__all__ = [
    "ALWAYS_ON",
    "NONE",
    "OPTIONAL",
    "UNKNOWN",
    "UNKNOWN_CONTRACT",
    "OFF_BETWEEN_TOOLS",
    "OFF_EXPLICIT",
    "OFF_IMPOSSIBLE",
    "OFF_OMIT",
    "OFF_SWAPS_MODEL",
    "OFF_UNKNOWN",
    "ReasoningContract",
    "WIRE_BUDGET_TOKENS",
    "WIRE_ENABLE_THINKING",
    "WIRE_CHAT_TEMPLATE_ENABLE_THINKING",
    "WIRE_NONE",
    "WIRE_REASONING_EFFORT",
    "WIRE_REASONING_ENABLED",
    "WIRE_REASONING_OBJECT",
    "WIRE_THINKING_ADAPTIVE",
    "WIRE_THINKING_LEVEL",
    "WIRE_THINKING_TYPE",
    "reasoning_contract_for",
    "reasoning_labels_allowed_by_contract",
    "reasoning_off_hazard",
    "reasoning_off_is_safe",
]
