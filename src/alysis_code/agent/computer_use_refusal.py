"""Validate existing host refusal feedback before making turn-local decisions.

This is not a diagnostic schema or a source of host authority. In particular a
known cause alone never authorizes another observation.
"""

from __future__ import annotations

# Existing host/controller refusals, independent of the diagnostic allowlist.
_HOST_REFUSALS = frozenset(
    {
        "target_changed",
        "target_window_changed",
        "target_window_not_found",
        "target_window_ambiguous",
        "target_app_changed",
        "target_closed",
        "window_content_protected",
        "secure_target_refused",
        "computer_use_binding_mismatch",
        "computer_use_full_access_revoked",
        "computer_use_lease_renewal_required",
        "computer_use_lease_expired",
        "computer_use_request_expired",
        "computer_use_request_in_progress",
        "computer_use_route_changed",
        "computer_use_cancelled",
        "computer_use_broker_changed",
        "computer_use_budget_refused",
        "computer_use_action_budget_exhausted",
        "computer_use_timeout",
        "computer_use_verification_unavailable",
        "computer_use_permission_required",
        "computer_use_approval_expired",
        "computer_use_approval_denied",
        "computer_use_action_outcome_uncertain",
        "out_of_scope",
        "user_takeover",
        "post_action_observation_missing",
        "control_proof_rejected",
        "window_reference_unknown",
        "window_reference_invalid",
        "window_reference_stale",
        "window_selection_unsupported",
        "window_reselection_required",
        "window_selection_pending",
        "computer_use_window_selection_exhausted",
        # Existing v2 stale-target, argument-correction and unsupported-mechanism answers.
        "stale_snapshot",
        "stale_element",
        "element_not_found",
        "surface_reference_stale",
        "focused_recipient_stale",
        "visual_frame_stale",
        "surface_selection_required",
        "computer_use_stale_element_not_dispatched",
        "computer_use_stale_window_not_dispatched",
        "invalid_interaction_target",
        "visual_point_out_of_bounds",
        "computer_use_invalid_keys_not_dispatched",
        "value_not_writable",
        "action_context_unavailable",
        "action_capabilities_unavailable",
        "background_synthetic_input_unsupported",
        "element_not_actionable",
        "computer_use_key_target_not_routable",
        "key_target_not_routable",
        "focus_target_not_routable",
    }
)
_FEEDBACK_ENUMS = {
    "dispatch": frozenset({"not_dispatched", "dispatched", "uncertain"}),
    "authority": frozenset({"valid_for_reobserve", "invalid", "takeover", "protected"}),
    "effect": frozenset({"observed_change", "observed_no_change", "unknown"}),
    "phase": frozenset(
        {
            "validation",
            "authority_validation",
            "native_validation",
            "preparation",
            "input_dispatch",
            "semantic_dispatch",
            "native_dispatch",
            "post_observation",
            "post_observation_validation",
        }
    ),
}
_TERMINAL_CONTROL_REFUSALS = frozenset(
    {
        "computer_use_cancelled",
        "user_takeover",
        "computer_use_action_budget_exhausted",
        "computer_use_window_selection_exhausted",
        "computer_use_full_access_revoked",
        "computer_use_lease_expired",
        "computer_use_request_expired",
        "computer_use_binding_mismatch",
    }
)


def validated_host_refusal_feedback(result: object) -> dict[str, str] | None:
    """Copy a closed, correlated feedback group; callers must verify host provenance."""
    if type(result) is not dict:
        return None
    error, feedback = result.get("error"), result.get("interaction_feedback")
    if (
        type(error) is not str
        or error not in _HOST_REFUSALS
        or type(feedback) is not dict
        or feedback.keys() != _FEEDBACK_ENUMS.keys() | {"cause"}
        or type(feedback.get("cause")) is not str
        or feedback["cause"] != error
    ):
        return None
    if any(
        type(feedback[key]) is not str or feedback[key] not in allowed
        for key, allowed in _FEEDBACK_ENUMS.items()
    ):
        return None
    if error in _TERMINAL_CONTROL_REFUSALS and feedback["authority"] == "valid_for_reobserve":
        return None
    return dict(feedback)


def validated_local_text_refusal(result: object) -> bool:
    """Existing UTF-16 cap correction; caller must prove the local pre-host boundary."""
    return (
        type(result) is dict
        and result.get("error") == "invalid_text"
        and result.get("interaction_feedback")
        == {
            "dispatch": "not_dispatched",
            "authority": "valid_for_reobserve",
            "effect": "unknown",
            "cause": "invalid_text",
            "phase": "validation",
        }
    )
