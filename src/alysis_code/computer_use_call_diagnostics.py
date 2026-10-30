"""Content-free call tracing. Never used for dispatch, recovery or completion authority."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from .computer_use_diagnostics import DIAGNOSTIC_CLASSES, _source_site

# Evidence only: original resolved Entry terminal refusals, never authority.
RESOLVED_START_REFUSAL_CODES = frozenset(
    {
        "computer_start_inventory_incomplete",
        "computer_start_inventory_empty",
        "computer_use_inventory_unavailable",
        "computer_use_target_stale",
        "computer_start_owner_invalid",
        "computer_start_capability_lost",
        "computer_start_response_invalid",
        "browser_start_unavailable",
        "browser_start_refused",
        "browser_start_timeout",
        "computer_selection_recovery_exhausted",
    }
)

TOOLS = frozenset(
    {
        "computer_state",
        "computer_action",
        "computer_wait",
        "computer_verify",
        "computer_download",
        "computer_file_status",
        "computer_upload",
        "computer_upload_status",
    }
)
BOUNDARIES = frozenset(
    {
        "turn_admission",
        "tool_entry",
        "local_arguments",
        "host_request",
        "host_result",
        "snapshot_validation",
        "observation_assertions",
        "image_projection",
        "result_copy",
    }
)
RESULT_CODES = frozenset(
    {
        "none",
        "other",
        "invalid_observation_assertions",
        "computer_use_observation_recovery_required",
        "invalid_host_response",
        "invalid_host_observation",
        "unexpected_host_image",
        "invalid_host_image",
        "computer_observation_unavailable",
        "invalid_observation_arguments",
        "computer_use_permission_required",
        "invalid_upload_arguments",
        "invalid_upload_response",
        "invalid_file_arguments",
        "invalid_file_response",
        "invalid_verification_proposal",
        "invalid_upload_objective_response",
        "invalid_host_workflow",
        "computer_use_fresh_observation_required",
        "computer_use_approval_expired",
        "computer_use_approval_denied",
        "computer_use_action_outcome_uncertain",
        "computer_use_key_target_not_routable",
        "computer_use_invalid_keys_not_dispatched",
        "computer_use_stale_element_not_dispatched",
        "computer_use_stale_window_not_dispatched",
        "background_synthetic_input_unsupported",
        "stale_page_cursor",
        "stale_element",
        "unknown_tool",
        "tool_contract_recovery_blocked",
        # Closed host controller answers and local guard codes. Keep aligned with the
        # app's computerUseCallDiagnostics.ts; anything else is reported as "other".
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
        "out_of_scope",
        "user_takeover",
        "target_changed",
        "target_window_changed",
        "target_window_ambiguous",
        "target_window_not_found",
        "target_app_changed",
        "window_content_protected",
        "window_reference_unknown",
        "window_reference_invalid",
        "window_reference_stale",
        "window_selection_unsupported",
        "window_reselection_required",
        "window_selection_pending",
        "computer_use_window_selection_exhausted",
        "target_closed",
        "secure_target_refused",
        "post_action_observation_missing",
        "control_proof_rejected",
        "invalid_tool_arguments_json",
        "repeated_tool_failure_guard",
    }
)
_active: ContextVar[ComputerUseCallTrace | None] = ContextVar(
    "computer_use_call_trace", default=None
)


def mark_call_boundary(boundary: str) -> None:
    trace = _active.get()
    if trace is not None and boundary in BOUNDARIES:
        trace.boundary = boundary
        if boundary in {"host_request", "host_result"}:
            trace.emit("boundary")


class ComputerUseCallTrace:
    def __init__(self, surface: object, ordinal: int, call_id: str, tool: str) -> None:
        self.surface, self.ordinal, self.call_id = surface, ordinal, call_id
        self.tool = tool if tool in TOOLS else "other"
        self.boundary = "turn_admission"
        self._exception_id: int | None = None
        self._observation_emitted = False
        self.emit("begin")

    def emit(self, phase: str, **fields: Any) -> None:
        # Diagnostics cannot throw into the task. The surface and app independently
        # restrict the payload; the call ID is transient correlation, never a log field.
        try:
            handler = getattr(self.surface, "emit_computer_use_call_diagnostic", None)
            if callable(handler) and type(self.ordinal) is int and 1 <= self.ordinal <= 100_000:
                handler(
                    {
                        "version": 1,
                        "callOrdinal": self.ordinal,
                        "call_id": self.call_id,
                        "tool": self.tool,
                        "callPhase": phase,
                        "boundary": self.boundary,
                        **fields,
                    }
                )
        except Exception:
            pass

    def run(self, run: Any, arguments: dict[str, Any]) -> Any:
        token = _active.set(self)
        self.boundary = "tool_entry"
        try:
            return run(arguments)
        except BaseException as error:
            self.capture_exception(error)
            raise
        finally:
            _active.reset(token)

    def capture_exception(self, error: BaseException) -> None:
        if self._exception_id == id(error):
            return
        self._exception_id = id(error)
        try:
            pending = [(error, "root")]
            seen: set[int] = set()
            while pending and len(seen) < 4:
                current, relation = pending.pop(0)
                if not isinstance(current, BaseException) or id(current) in seen:
                    continue
                seen.add(id(current))
                name = type(current).__name__
                site, line = _source_site(current)
                self.emit(
                    "exception",
                    exceptionClass=name if name in DIAGNOSTIC_CLASSES else "UnknownError",
                    exceptionSite=site,
                    exceptionLine=line,
                    chainIndex=len(seen) - 1,
                    chainRelation=relation,
                )
                # Preserve explicit cause versus implicit context without copying prose.
                cause = current.__cause__
                context = current.__context__
                if cause is not None:
                    pending.append((cause, "cause"))
                elif context is not None and not current.__suppress_context__:
                    pending.append((context, "context"))
                if isinstance(current, BaseExceptionGroup):
                    pending.extend((child, "group") for child in current.exceptions[:4])
        except Exception:
            pass

    def finish(self, result: Any) -> None:
        # A local refusal may be a dict subclass; it is still a failed call result.
        failed = isinstance(result, dict) and "error" in result
        code = result.get("error") if failed else "none"
        if failed and not (type(code) is str and code in RESULT_CODES):
            # Local guards put prose in "error" and their closed code in "error_code".
            code = result.get("error_code")
        self.emit(
            "result",
            result="failed" if failed else "returned",
            resultCode=code if type(code) is str and code in RESULT_CODES else "other",
        )


def sanitize_call_diagnostic(value: Any) -> dict[str, Any] | None:
    from .computer_use_diagnostics import DIAGNOSTIC_SITES

    if type(value) is not dict or type(value.get("version")) is not int or value["version"] != 1:
        return None
    ordinal = value.get("callOrdinal")
    if type(ordinal) is not int or not 1 <= ordinal <= 100_000:
        return None
    out: dict[str, Any] = {"version": 1, "callOrdinal": ordinal}
    enums = {
        "tool": TOOLS | {"other"},
        "callPhase": {"begin", "boundary", "exception", "result", "turn_failure"},
        "boundary": BOUNDARIES,
        "result": {"returned", "failed"},
        "resultCode": RESULT_CODES,
        "exceptionClass": DIAGNOSTIC_CLASSES,
        "exceptionSite": DIAGNOSTIC_SITES,
        "chainRelation": {"root", "cause", "context", "group"},
    }
    for key, allowed in enums.items():
        item = value.get(key)
        if type(item) is str and item in allowed:
            out[key] = item
    if not all(k in out for k in ("tool", "callPhase", "boundary")):
        return None
    for key, maximum in (("exceptionLine", 99_999), ("chainIndex", 3)):
        item = value.get(key)
        if type(item) is int and 0 <= item <= maximum:
            out[key] = item
    return out


# This schema is intentionally independent of legacy call diagnostics. New evidence
# rejects a whole record rather than dropping an unrecognised/private field.
_AVAILABILITY = frozenset({"available", "unavailable", "unknown"})
_IMAGE_REASONS = frozenset(
    {
        "none",
        "not_requested",
        "model_capability_unavailable",
        "decoder_unavailable",
        "capture_refused",
        "frame_invalid",
        "cancelled",
        "not_prepared",
        "projection_dropped",
        "adapter_dropped",
        "other",
        "unknown",
    }
)
_CAPABILITY_SOURCES = frozenset(
    {"explicit_override", "registry", "provider_metadata", "default", "unknown"}
)
_COUNT_GROUP = frozenset(
    {
        "elementCount",
        "ordinaryCount",
        "protectedCount",
        "unknownCount",
        "pressEligibleCount",
        "writableTextEligibleCount",
    }
)
_OBSERVATION_REQUIRED = frozenset(
    {"observationOrdinal", "source", "tool", "projectionState", "imageRequested"}
)
_OBSERVATION_ENUMS = {
    "source": {"controller_projection", "engine_projection"},
    "tool": {"computer_state", "computer_action", "computer_wait", "computer_verify"},
    "projectionState": {"returned", "historical", "withheld"},
    "focusedKey": _AVAILABILITY,
    "focusedType": _AVAILABILITY,
    "surfaceState": {
        "selected",
        "selection_required",
        "none",
        "closed_window",
        "application_terminated",
        "unknown",
    },
    "coverageState": {"complete", "partial", "unknown"},
    "imageReadiness": {"ready", "unavailable", "unknown"},
    "imageUnavailableReason": _IMAGE_REASONS,
    "captureState": {"present", "absent", "refused", "unknown"},
    "frameBinding": {"valid", "invalid", "absent", "unknown"},
    "decoderAvailable": _AVAILABILITY,
    "effectiveVision": _AVAILABILITY,
    "capabilitySource": _CAPABILITY_SOURCES,
}
_PROVIDER_REQUIRED = frozenset({"providerOrdinal", "attemptOrdinal", "phase", "generationRole"})
_PROVIDER_ENUMS = {
    "phase": {
        "context_projected",
        "serialized",
        "payload_prepared_at_transport",
        "transport_attempted",
        "response_received",
        "finished",
    },
    "generationRole": {"protected", "ordinary", "unknown"},
    "decoderAvailable": _AVAILABILITY,
    "effectiveVision": _AVAILABILITY,
    "capabilitySource": _CAPABILITY_SOURCES,
    "adapter": {"none", "openai_codex_subscription", "other", "unknown"},
    "outcome": {"returned", "failed", "cancelled", "deadline", "unknown"},
}
_TIMING_ENUMS = {
    "timingScope": {"host", "engine"},
    "generationRole": {"protected", "ordinary", "unknown"},
    "span": {
        "generation_startup",
        "discovery",
        "readiness",
        "admission",
        "child_shutdown",
        "child_restart",
        "host_call",
        "provider_call",
        "final_projection",
    },
    "phase": {"begin", "end"},
    "outcome": {"returned", "failed", "cancelled", "deadline", "unknown"},
}
_PROVIDER_COUNTS = frozenset(
    {"projectedImageCount", "serializedImageCount", "adaptedImageCount", "transportImageCount"}
)


def _integer(value: object, minimum: int = 0, maximum: int = 100_000) -> bool:
    return type(value) is int and minimum <= value <= maximum


def sanitize_computer_use_evidence(value: object) -> dict[str, object] | None:
    """Return copied closed scalars, or reject the entire prospective v1 record."""
    if (
        type(value) is not dict
        or type(value.get("version")) is not int
        or value["version"] != 1
        or type(value.get("kind")) is not str
        or any(type(key) is not str for key in value)
    ):
        return None
    kind = value.get("kind")
    common = {"version", "kind"}
    if kind == "observation":
        required = _OBSERVATION_REQUIRED
        enums = _OBSERVATION_ENUMS
        counts = _COUNT_GROUP | {"preparedImageCount", "projectedImageCount"}
        allowed = common | required | set(enums) | counts
        if (
            not _integer(value.get("observationOrdinal"), 1)
            or type(value.get("imageRequested")) is not bool
        ):
            return None
    elif kind == "provider":
        required, enums, counts = _PROVIDER_REQUIRED, _PROVIDER_ENUMS, _PROVIDER_COUNTS
        allowed = common | required | set(enums) | counts | {"durationMs"}
        if not _integer(value.get("providerOrdinal"), 1) or not _integer(
            value.get("attemptOrdinal"), 0, 16
        ):
            return None
        phase = value.get("phase")
        if type(phase) is not str:
            return None
        if (
            value.get("attemptOrdinal") == 0
            and phase
            in {"payload_prepared_at_transport", "transport_attempted", "response_received"}
            or "transportImageCount" in value
            and phase != "transport_attempted"
            or phase == "finished"
            and "outcome" not in value
            or phase != "finished"
            and ("outcome" in value or "durationMs" in value)
        ):
            return None
    elif kind == "terminal":
        required = frozenset(
            {"terminalOrigin", "reportedOutcome", "stateCalls", "actionCalls", "knownHostFailure"}
        )
        enums = {
            "terminalOrigin": {
                "model_report",
                "host_error",
                "cancel",
                "deadline",
                "validation",
                "unknown",
            },
            "reportedOutcome": {"completed", "blocked", "failed", "cancelled", "unknown"},
            "hostFailureCode": RESULT_CODES,
            "resolvedStartRefusalCode": RESOLVED_START_REFUSAL_CODES,
        }
        counts = frozenset({"stateCalls", "actionCalls"})
        allowed = common | required | set(enums)
        if (
            type(value.get("knownHostFailure")) is not bool
            or value.get("knownHostFailure") is False
            and "hostFailureCode" in value
        ):
            return None
    elif kind == "timing":
        required = frozenset({"spanOrdinal", "timingScope", "generationRole", "span", "phase"})
        enums, counts = _TIMING_ENUMS, frozenset()
        allowed = common | required | set(enums) | {"durationMs"}
        if not _integer(value.get("spanOrdinal"), 1):
            return None
        if (
            value.get("phase") == "begin"
            and ("durationMs" in value or "outcome" in value)
            or value.get("phase") == "end"
            and not {"durationMs", "outcome"} <= value.keys()
        ):
            return None
    else:
        return None
    if not common | required <= value.keys() or not value.keys() <= allowed:
        return None
    for key, choices in enums.items():
        if key in value and (type(value[key]) is not str or value[key] not in choices):
            return None
    if any(not _integer(value[key]) for key in counts if key in value):
        return None
    if "durationMs" in value and not _integer(value["durationMs"], 0, 86_400_000):
        return None
    if kind == "observation":
        present = _COUNT_GROUP & value.keys()
        if present and (
            present != _COUNT_GROUP
            or value["ordinaryCount"] + value["protectedCount"] + value["unknownCount"]
            != value["elementCount"]
            or value["pressEligibleCount"] > value["ordinaryCount"]
            or value["writableTextEligibleCount"] > value["ordinaryCount"]
        ):
            return None
        projection = value["projectionState"]
        if (
            projection == "withheld"
            and (_COUNT_GROUP | {"focusedKey", "focusedType", "surfaceState", "coverageState"})
            & value.keys()
        ):
            return None
        if projection in {"withheld", "historical"} and (
            value.get("captureState") == "present"
            or value.get("frameBinding") == "valid"
            or value.get("imageReadiness") == "ready"
            or value.get("preparedImageCount", 0) != 0
            or value.get("projectedImageCount", 0) != 0
        ):
            return None
        if projection == "historical" and (
            value.get("surfaceState", "unknown")
            not in {"closed_window", "application_terminated", "unknown"}
            or {"captureState", "frameBinding", "preparedImageCount", "projectedImageCount"}
            & value.keys()
            or value.get("focusedKey") == "available"
            or value.get("focusedType") == "available"
        ):
            return None
    try:
        if len(json.dumps(value, ensure_ascii=True, allow_nan=False).encode("utf-8")) > 4096:
            return None
    except (TypeError, ValueError, OverflowError):
        return None
    return dict(value)


def _availability(value: object) -> str:
    return "available" if value is True else "unavailable" if value is False else "unknown"


def _readiness_evidence(readiness: Mapping[str, object] | None) -> dict[str, object]:
    if readiness is None:
        return {}
    state = readiness.get("state")
    source = readiness.get("model_capability_source")
    category = "unknown"
    if isinstance(source, str):
        if source.startswith(("env:", "user:")):
            category = "explicit_override"
        elif source.startswith("provider_auth"):
            category = "provider_metadata"
        elif source == "fallback":
            category = "default"
        elif source.startswith(
            ("official_provider", "canonical_model", "bundled", "built_in", "litellm")
        ):
            category = "registry"
    return {
        "imageReadiness": state
        if type(state) is str and state in {"ready", "unavailable"}
        else "unknown",
        "decoderAvailable": _availability(readiness.get("image_decoder_available")),
        "effectiveVision": _availability(readiness.get("model_supports_vision")),
        "capabilitySource": category,
    }


def _observation_summary(
    tool: str, result: object, image_requested: bool, readiness: Mapping[str, object] | None
) -> dict[str, object]:
    summary: dict[str, object] = {
        "tool": tool,
        "projectionState": "withheld",
        "imageRequested": image_requested,
        **_readiness_evidence(readiness),
    }
    if summary.get("imageReadiness") == "ready":
        summary.pop("imageReadiness")
    if not isinstance(result, dict) or "error" in result:
        if isinstance(result, dict) and result.get("error") == "invalid_host_image":
            summary.update(imageUnavailableReason="frame_invalid", frameBinding="invalid")
        return summary
    state = (
        result.get("state")
        if tool == "computer_action" or isinstance(result.get("state"), dict)
        else result
    )
    if not isinstance(state, dict) or not isinstance(state.get("elements"), list):
        return summary
    historical = readiness is not None and readiness.get("_projection_state") == "historical"
    summary["projectionState"] = "historical" if historical else "returned"
    if not historical:
        summary.update(_readiness_evidence(readiness))
    elements = state["elements"]
    ordinary = protected = unknown = press = writable = 0
    for element in elements:
        if not isinstance(element, dict):
            unknown += 1
            continue
        security = element.get("security_state")
        if (
            security == "ordinary"
            and element.get("secure") is False
            and element.get("content_redacted") is False
        ):
            ordinary += 1
            if element.get("enabled") is True:
                actions = element.get("actions")
                press += isinstance(actions, list) and "AXPress" in actions
                role = element.get("role")
                writable += (
                    type(role) is str
                    and role in {"AXTextField", "AXTextArea", "AXComboBox", "AXSearchField"}
                    and element.get("value_settable") is True
                )
        elif (
            security == "protected"
            and element.get("secure") is True
            and element.get("content_redacted") is True
        ):
            protected += 1
        else:
            unknown += 1
    summary.update(
        elementCount=len(elements),
        ordinaryCount=ordinary,
        protectedCount=protected,
        unknownCount=unknown,
        pressEligibleCount=int(press),
        writableTextEligibleCount=int(writable),
    )
    focus = state.get("focused_target") if isinstance(state.get("focused_target"), dict) else {}
    for field, key in (("focusedKey", "key"), ("focusedType", "type")):
        summary[field] = (
            "unavailable"
            if focus.get("available") is False or focus.get(key) is False
            else (
                "available"
                if focus.get("available") is True
                and focus.get(key) is True
                and state.get("interaction_version") == 2
                and isinstance(state.get("snapshot_id"), str)
                and state["snapshot_id"]
                and focus.get("recipient_ref") == "recipient_" + state["snapshot_id"]
                else "unknown"
            )
        )
    target = state.get("target") if isinstance(state.get("target"), dict) else {}
    summary["surfaceState"] = (
        "application_terminated"
        if target.get("application_status") == "terminated"
        else "closed_window"
        if target.get("window_status") == "closed"
        else "selection_required"
        if state.get("selection_required") is True
        else "selected"
        if isinstance(state.get("selected_surface"), str)
        and state["selected_surface"] == state.get("surface_ref")
        else "none"
        if state.get("selected_surface") is None and state.get("selection_required") is False
        else "unknown"
    )
    coverage = state.get("coverage") if isinstance(state.get("coverage"), dict) else {}
    summary["coverageState"] = (
        "partial"
        if state.get("truncated") is True or coverage.get("complete") is False
        else "complete"
        if coverage.get("complete") is True and state.get("truncated") is False
        else "unknown"
    )
    if historical:
        summary["focusedKey"] = summary["focusedType"] = (
            "unavailable" if focus.get("available") is False else "unknown"
        )
        if summary["surfaceState"] not in {"closed_window", "application_terminated"}:
            summary["surfaceState"] = "unknown"
        return summary
    prepared = isinstance(result.get("visual_delivery_id"), str) and bool(
        result["visual_delivery_id"]
    )
    frame = state.get("image_frame")
    summary["preparedImageCount"] = int(prepared)
    # The private message is not projected into a provider request until core binds it.
    summary["captureState"] = (
        "present" if prepared else "absent" if "image" not in state else "unknown"
    )
    summary["frameBinding"] = (
        "valid"
        if prepared and isinstance(frame, dict)
        else "absent"
        if frame is None
        else "unknown"
    )
    visual = state.get("visual") if isinstance(state.get("visual"), dict) else {}
    if not image_requested:
        reason = "not_requested"
    elif prepared:
        reason = "none"
    elif readiness and readiness.get("image_decoder_available") is False:
        reason = "decoder_unavailable"
    elif readiness and readiness.get("model_supports_vision") is False:
        reason = "model_capability_unavailable"
    elif visual.get("status") == "unavailable":
        reason = "capture_refused"
        summary["captureState"] = "refused"
    else:
        reason = "not_prepared"
    summary["imageUnavailableReason"] = reason
    return summary


def note_observation_projection(
    *,
    tool: str,
    result: object,
    image_requested: bool,
    readiness: Mapping[str, object] | None = None,
) -> None:
    trace = _active.get()
    if trace is None or trace._observation_emitted:
        return
    try:
        summary = _observation_summary(tool, result, image_requested, readiness)
        record = sanitize_computer_use_evidence(
            {
                "version": 1,
                "kind": "observation",
                "source": "engine_projection",
                "observationOrdinal": trace.ordinal,
                **summary,
            }
        )
        emit = getattr(trace.surface, "emit_computer_use_evidence", None)
        if record is not None and callable(emit):
            trace._observation_emitted = True
            emit(record)
    except Exception:
        pass


@dataclass
class _ProviderEvidenceScope:
    emit: Callable[[dict[str, object]], None]
    attempt: int = 0
    prepared: int = 0


_provider_evidence: ContextVar[_ProviderEvidenceScope | None] = ContextVar(
    "computer_use_provider_evidence", default=None
)


@contextmanager
def bind_computer_use_provider_evidence(
    emit: Callable[[dict[str, object]], None] | None,
) -> Iterator[None]:
    token = _provider_evidence.set(_ProviderEvidenceScope(emit) if callable(emit) else None)
    try:
        yield
    finally:
        _provider_evidence.reset(token)


def note_computer_use_provider_evidence(payload: Mapping[str, object]) -> None:
    scope = _provider_evidence.get()
    if scope is None:
        return
    try:
        candidate = dict(payload)
        if set(candidate) & {"version", "kind", "providerOrdinal", "generationRole"}:
            return
        safe = sanitize_computer_use_evidence(
            {
                "version": 1,
                "kind": "provider",
                "providerOrdinal": 1,
                "generationRole": "unknown",
                **candidate,
            }
        )
        if safe is not None:
            for key in ("version", "kind", "providerOrdinal", "generationRole"):
                safe.pop(key)
            scope.emit(dict(safe))
    except Exception:
        pass


def _provider_attempt_ordinal(*, prepare: bool = False) -> int | None:
    scope = _provider_evidence.get()
    if scope is None or scope.attempt >= 16:
        return None
    ordinal = scope.attempt + 1
    if prepare:
        scope.prepared = ordinal
    return ordinal


def _provider_serialization_ordinal() -> int | None:
    scope = _provider_evidence.get()
    if scope is None or scope.attempt >= 16:
        return None
    return 0 if scope.attempt == 0 else scope.attempt + 1


def _provider_evidence_active() -> bool:
    return _provider_evidence.get() is not None


def _begin_provider_transport_evidence() -> int | None:
    scope = _provider_evidence.get()
    if scope is None or scope.prepared != scope.attempt + 1 or scope.prepared > 16:
        return None
    scope.attempt = scope.prepared
    scope.prepared = 0
    return scope.attempt


def _count_provider_images(payload: object) -> int | None:
    """Bounded structural count; never look for image-looking prose or retain content."""
    if not isinstance(payload, dict):
        return None
    items = payload.get("input")
    if isinstance(items, str):
        return 0
    if not isinstance(items, list) or len(items) > 100_000:
        return None
    count = 0
    visited = 0
    for item in items:
        if not isinstance(item, dict):
            return None
        parts = item.get("content")
        if isinstance(parts, str) or parts is None:
            continue
        if not isinstance(parts, list) or len(parts) > 100_000:
            return None
        for part in parts:
            visited += 1
            if visited > 100_000:
                return None
            if not isinstance(part, dict):
                return None
            part_type = part.get("type")
            if type(part_type) is not str:
                return None
            if part_type in {"input_image", "image_url"}:
                count += 1
                if count > 100_000:
                    return None
    return count
