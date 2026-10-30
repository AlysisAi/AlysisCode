"""The router-free unified turn path.

No router client is provisioned, every text turn goes straight to the main
model with the full per-mode agent surface, and execution posture derives
from the execution mode instead of a pre-turn routing call. The legacy
routed path has been removed: ``ALYSIS_UNIFIED_TURN_PATH`` and
``unified_turn_path_enabled`` remain accepted for one release but are
ignored — every turn takes the unified path.
"""

from __future__ import annotations

from typing import Any, Literal, cast

from ..branding import env_get
from ..language_policy import normalize_language_name, normalize_script_name
from ..runtime_kind import RuntimeKind

#: System prompt for explicit `/chat` turns: one bounded conversational reply
#: with no tools and no workspace context. This is the deliberate cheap-small-
#: talk surface; automatic classification of "cheap" turns is exactly what the
#: unified turn path removes.
CHAT_ONLY_SYSTEM_PROMPT = (
    "You are Alysis Code, a coding assistant, replying in plain conversation. "
    "No tools are available on this turn and the workspace is not attached. "
    "Answer directly and concisely, in the user's language. If the request "
    "actually needs repository files, commands, or edits, say so briefly and "
    "suggest sending it as a normal message instead of /chat."
)


def unified_turn_path_enabled(cfg: Any | None) -> bool:
    """``ALYSIS_UNIFIED_TURN_PATH`` (on/off) wins over the config value.

    Mirrors the kill-switch idiom used by ``semantic_turn_contract_enabled``
    and ``reproduction_first_enabled``; the default is on.
    """
    env_value = env_get("ALYSIS_UNIFIED_TURN_PATH")
    if env_value is not None:
        normalized = str(env_value).strip().lower()
        if normalized in {"on", "1", "true", "yes", "enabled"}:
            return True
        if normalized in {"off", "0", "false", "no", "disabled"}:
            return False
    return bool(getattr(cfg, "unified_turn_path_enabled", True))


def _flag_enabled(env_name: str, cfg: Any | None, attr: str, *, default: bool = True) -> bool:
    """Kill-switch idiom shared by the zero-activity gate flags: env wins."""
    env_value = env_get(env_name)
    if env_value is not None:
        normalized = str(env_value).strip().lower()
        if normalized in {"on", "1", "true", "yes", "enabled"}:
            return True
        if normalized in {"off", "0", "false", "no", "disabled"}:
            return False
    return bool(getattr(cfg, attr, default))


def zero_activity_gate_downgrade_enabled(cfg: Any | None) -> bool:
    """Zero-activity guard: gate posture downgrades for turns with zero observed tool activity.

    A turn that ran no tools produced no work the completion gate could verify;
    holding its reply to execute-turn enforcement is what displaced correct
    answers. ``ALYSIS_ZERO_ACTIVITY_GATE_DOWNGRADE`` (on/off)
    wins over the config value; the default is on.
    """
    return _flag_enabled(
        "ALYSIS_ZERO_ACTIVITY_GATE_DOWNGRADE",
        cfg,
        "zero_activity_gate_downgrade_enabled",
    )


def zero_activity_disposition_check_enabled(cfg: Any | None) -> bool:
    """LLM disposition check guarding the zero-activity downgrade.

    Model-based on purpose: it must hold in every reply language, which
    deterministic English pattern matching cannot. When off, zero-activity
    turns downgrade unconditionally with no extra model call.
    ``ALYSIS_ZERO_ACTIVITY_DISPOSITION_CHECK`` (on/off) wins over config;
    the default is on.
    """
    return _flag_enabled(
        "ALYSIS_ZERO_ACTIVITY_DISPOSITION_CHECK",
        cfg,
        "zero_activity_disposition_check_enabled",
    )


def greenfield_verification_bootstrap_enabled(cfg: Any | None) -> bool:
    """Greenfield bootstrap: a passing agent-authored suite bootstraps the verification contract.

    A brand-new project can never have the "trustworthy pre-existing test
    surface" the contract selection demands, so greenfield work ran without
    completion-gate governance (the greenfield governance gap) and shipped defects behind
    vacuous self-checks. With this on, the first observed passing test
    execution in an unavailable-contract session is adopted as a best-effort
    contract, arming the gate for every later edit.
    ``ALYSIS_GREENFIELD_VERIFY_BOOTSTRAP`` (on/off) wins over config; default on.
    """
    return _flag_enabled(
        "ALYSIS_GREENFIELD_VERIFY_BOOTSTRAP",
        cfg,
        "greenfield_verification_bootstrap_enabled",
    )


def zero_coverage_advisory_enabled(cfg: Any | None) -> bool:
    """Zero-coverage guard: flag newly created modules that no test references.

    Appended as a visible advisory at finalization (append-only, never
    blocking). ``ALYSIS_ZERO_COVERAGE_ADVISORY`` (on/off) wins over config;
    default off. This name-reference heuristic is not executed coverage and can
    mislabel temporary work-product scripts. Required checks remain independent.
    """
    return _flag_enabled(
        "ALYSIS_ZERO_COVERAGE_ADVISORY",
        cfg,
        "zero_coverage_advisory_enabled",
        default=False,
    )


# ---------------------------------------------------------------------------
# Turn execution intent and reply-language directives (router-independent)
# ---------------------------------------------------------------------------


_OneShotRepoTurnIntent = Literal["execute", "plan_or_analysis_only", "advisory_non_execution"]


_ROUTER_EXECUTION_POSTURES = {
    "execute",
    "advisory_non_execution",
    "plan_or_analysis_only",
}


def _repo_turn_execution_posture(*, mode_allows_execution: bool) -> _OneShotRepoTurnIntent:
    """Return the mode's execution posture without interpreting user language.

    The main model handles natural-language meaning. Controller completion gates
    refine this capability posture from observed tool effects, so a report turn
    that only reads remains non-mutating in every language.
    """

    return "execute" if mode_allows_execution else "advisory_non_execution"


def _resolve_repo_turn_execution_intent(
    *,
    one_shot_execution: bool,
    runtime_kind: RuntimeKind,
    route_execution_posture: str,
    classified_turn_intent: _OneShotRepoTurnIntent,
) -> _OneShotRepoTurnIntent:
    normalized_posture = str(route_execution_posture or "").strip().lower()
    if (
        not one_shot_execution
        and runtime_kind == RuntimeKind.INTERACTIVE_CHAT
        and normalized_posture in _ROUTER_EXECUTION_POSTURES
    ):
        if classified_turn_intent != "execute" and normalized_posture == "execute":
            return classified_turn_intent
        return cast(_OneShotRepoTurnIntent, normalized_posture)
    return classified_turn_intent


def _normalize_turn_language_name(raw: Any) -> str:
    return normalize_language_name(raw)


def _normalize_turn_script_name(raw: Any) -> str:
    return normalize_script_name(raw)


def _build_turn_language_system_message(
    language: str,
    script: str,
    *,
    explicit_language_override: bool = False,
) -> str | None:
    resolved_language = _normalize_turn_language_name(language)
    resolved_script = _normalize_turn_script_name(script)
    if not resolved_language and not resolved_script:
        return None
    request_label = (
        "The user explicitly requested a language/script override for this reply. "
        if explicit_language_override
        else "The selected reply language/script for this turn is model-determined. "
    )
    if resolved_language and resolved_script:
        scope = (
            f"{request_label}"
            f"Respond in {resolved_language} using the {resolved_script} writing system. "
        )
    elif resolved_language:
        scope = f"{request_label}Respond in {resolved_language} using its standard writing system. "
    else:
        scope = f"{request_label}Respond in English using the {resolved_script} writing system. "
    return (
        scope
        + "Do not output transliteration/romanization unless the user explicitly requested it. "
        + "Never translate code identifiers, file paths, CLI commands, config keys, or code blocks."
    )
