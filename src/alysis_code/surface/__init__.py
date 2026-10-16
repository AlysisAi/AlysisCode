from .base import Surface
from .events import (
    EVENT_REGISTRY,
    ApprovalReviewed,
    ConfigFormRequest,
    ErrorRaised,
    Event,
    InfoEmitted,
    MessageDelta,
    MessageEnd,
    ModeChanged,
    PlanNodeUpdated,
    PromptForInput,
    ReviewGateDecision,
    StatusUpdate,
    SwarmWorkerStateChanged,
    ToolCallCompleted,
    ToolCallProgress,
    ToolCallStarted,
    VerifyGateResult,
    WarningEmitted,
    event_from_dict,
)
from .hidden_surface import HiddenApprovalSurface, NestedSubagentSurface
from .noop_surface import NoopSurface
from .types import (
    ApprovalDecision,
    ApprovalRequest,
    PatchEvent,
    StandingApproval,
    StatusEvent,
    SubagentEndEvent,
    SubagentStartEvent,
    ToolEndEvent,
    ToolOutputEvent,
    ToolStartEvent,
)


def __getattr__(name: str):
    if name == "RichSurface":
        from .rich_surface import RichSurface

        return RichSurface
    raise AttributeError(name)


__all__ = [
    "ApprovalDecision",
    "ApprovalReviewed",
    "ApprovalRequest",
    "ConfigFormRequest",
    "EVENT_REGISTRY",
    "ErrorRaised",
    "Event",
    "HiddenApprovalSurface",
    "InfoEmitted",
    "MessageDelta",
    "MessageEnd",
    "ModeChanged",
    "NestedSubagentSurface",
    "NoopSurface",
    "PatchEvent",
    "StandingApproval",
    "PlanNodeUpdated",
    "PromptForInput",
    "ReviewGateDecision",
    "RichSurface",
    "StatusEvent",
    "StatusUpdate",
    "SwarmWorkerStateChanged",
    "ToolCallCompleted",
    "ToolCallProgress",
    "ToolCallStarted",
    "SubagentEndEvent",
    "SubagentStartEvent",
    "Surface",
    "ToolEndEvent",
    "ToolOutputEvent",
    "ToolStartEvent",
    "VerifyGateResult",
    "WarningEmitted",
    "event_from_dict",
]
