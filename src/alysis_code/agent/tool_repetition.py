"""Bounded reassessment for repeated external calls, independent of app/tool names."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

EXTERNAL_TOOL_REASSESSMENT = (
    "You have repeated the same external tool request several times. Repetition alone does not "
    "prove failure or lack of progress. Compare the latest result with the task and previous "
    "observations before deciding what to do next. If useful state is still changing, or you are "
    "waiting for an expected transition, explain that and continue with a justified bounded wait. "
    "Otherwise use the available evidence to choose a different useful observation or an "
    "authorized action, or report the specific blocker. For visual interfaces, consider both "
    "the image and accessibility information; page through a truncated observation when needed. "
    "Use the latest valid state for any action. Never replay input whose outcome is uncertain. "
    "This notice grants no permission, selects no action and is not evidence of task completion."
)

EXTERNAL_TOOL_PROGRESS_CHECKPOINT = (
    "External-tool progress checkpoint. Several external requests have completed; this alone "
    "does not establish progress, success or a loop. Briefly identify the concrete change or "
    "new evidence relevant to the user's goal, what remains unverified, and the next useful "
    "step supported by the latest result. A successful tool receipt is not proof its intended "
    "effect occurred. Compare available visual and accessibility evidence; distinguish changed "
    "state, useful pagination, an expected transition, a refusal, and an uncertain action outcome. "
    "Continue useful work or a justified bounded wait. If evidence shows no progress, reassess "
    "your approach or report the specific blocker. Respect denials and ownership checks; do not "
    "use another route to bypass them or replay input whose outcome is uncertain. This checkpoint "
    "selects no action, grants no permission and supplies no completion evidence."
)


class ExternalToolRepetitionTracker:
    """Only retain an argument digest, not private external tool arguments/results.

    This is a reassessment signal, not a claim that observations are unchanged.
    A new target/page/action resets exact repetition. A separate activity checkpoint
    survives changed arguments and tools, so alternating observations are not invisible.
    At most two notices are emitted per turn. Routine activity may use only one, so
    a longer productive workflow cannot exhaust reassessment before repetition begins.
    Neither notice blocks tools, forces mutations, parses app state or changes policy.
    """

    def __init__(self) -> None:
        self._previous: str | None = None
        self._consecutive = 0
        self._notices = 0
        self._activity_notices = 0
        self._requests_since_notice = 0
        self._failed_since_notice = 0

    def observe(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, Any],
        metadata: Mapping[str, Any],
        succeeded: bool,
    ) -> dict[str, Any] | None:
        if metadata.get("tool_type") != "mcp":
            self._previous = None
            self._consecutive = 0
            return None
        self._requests_since_notice += 1
        if not succeeded:
            self._failed_since_notice += 1
            self._previous = None
            self._consecutive = 0
        else:
            fingerprint = hashlib.sha256(
                json.dumps([tool_name, arguments], sort_keys=True, ensure_ascii=True).encode()
            ).hexdigest()
            self._consecutive = self._consecutive + 1 if fingerprint == self._previous else 1
            self._previous = fingerprint
        repeated = self._consecutive in {3, 6}
        activity_due = self._activity_notices == 0 and self._requests_since_notice >= 8
        if self._notices >= 2 or (not repeated and not activity_due):
            return None
        self._notices += 1
        if not repeated:
            self._activity_notices += 1
        notice = {
            "trigger": "request_repetition" if repeated else "activity_checkpoint",
            "consecutive_requests": self._consecutive,
            "requests_since_notice": self._requests_since_notice,
            "failed_requests_since_notice": self._failed_since_notice,
            "notice": self._notices,
            "state_unchanged": "not_established",
        }
        self._requests_since_notice = 0
        self._failed_since_notice = 0
        return notice
