"""Bound repeated native reads without retaining UI content or declaring task success."""

from __future__ import annotations

import hashlib
import json
from collections import deque
from typing import Any


class ComputerUseObservationProgress:
    """One correction for an eight-read cycle, then stop if that cycle persists.

    A fresh snapshot ID alone is not progress. Only identical semantic projections
    are compared; partial views, assertion sets and coverage remain distinct. Image
    reads and browser pagination are excluded because their progress is not fully
    represented by a native Accessibility projection. Explicit bounded waits use
    the existing task deadline rather than a read-count deadline. Digests never
    leave the turn.
    """

    def __init__(self) -> None:
        self._recent: deque[str] = deque(maxlen=8)
        self._warned_views: frozenset[str] | None = None
        self._switches: deque[str] = deque(maxlen=6)
        self._warned_switches: frozenset[str] | None = None
        self._refusal_reads_remaining: int | None = None

    def action_refused(self, result: dict[str, Any]) -> None:
        """One recovery allowance per unresolved action, not per new snapshot/refusal."""
        feedback = result.get("interaction_feedback")
        if (
            "error" in result
            and isinstance(feedback, dict)
            and feedback.get("dispatch") == "not_dispatched"
            and feedback.get("authority") == "valid_for_reobserve"
            and self._refusal_reads_remaining is None
        ):
            self._refusal_reads_remaining = 4

    def admit_recovery_read(self) -> bool:
        """Count all state/wait attempts before host dispatch, including image/projection changes."""
        if self._refusal_reads_remaining is None:
            return True
        if self._refusal_reads_remaining == 0:
            return False
        self._refusal_reads_remaining -= 1
        return True

    def action_progress(
        self,
        action: str = "",
        result: dict[str, Any] | None = None,
        *,
        validated_receipt: bool = False,
    ) -> str | None:
        feedback = (result or {}).get("interaction_feedback")
        if (
            validated_receipt
            and action not in {"", "focus", "activate_application"}
            and isinstance(feedback, dict)
            and feedback.get("dispatch") == "dispatched"
            and feedback.get("authority") == "valid_for_reobserve"
            and "error" not in (result or {})
        ):
            if feedback.get("effect") == "observed_change":
                self._refusal_reads_remaining = None
            elif feedback.get("effect") == "unknown" and self._refusal_reads_remaining is not None:
                # Acceptance with incomplete effect evidence renews a finite allowance.
                # It must never disable the bound or certify task completion.
                self._refusal_reads_remaining = 4
        if action == "activate_application":
            # Activation has a fresh post-state but is not task progress by itself.
            # Keep this history across explicit reads between switches as well.
            state = (result or {}).get("state")
            if not isinstance(state, dict) or state.get("source") == "chrome-dom":
                self._switches.clear()
                return None
            if not isinstance(state.get("elements"), list) or not state["elements"]:
                self._switches.clear()
                return None
            self._switches.append(self._digest(state, {}))
            views = frozenset(self._switches)
            if len(self._switches) < self._switches.maxlen or len(views) > 2:
                return None
            self._switches.clear()
            if self._warned_switches is not None and views <= self._warned_switches:
                return "switch_stop"
            self._warned_switches = views
            return "switch_correct"
        feedback = (result or {}).get("interaction_feedback")
        if isinstance(feedback, dict) and feedback.get("effect") != "observed_change":
            return None
        self._recent.clear()
        self._warned_views = None
        self._switches.clear()
        self._warned_switches = None
        return None

    def observe(
        self, result: dict[str, Any], arguments: dict[str, Any], *, is_wait: bool = False
    ) -> str | None:
        elements = result.get("elements")
        if (
            is_wait
            or result.get("source") == "chrome-dom"
            or arguments.get("include_image") is True
            or "image" in result
            or not isinstance(elements, list)
            or not elements
        ):
            self._recent.clear()
            return None
        self._recent.append(self._digest(result, arguments))
        views = frozenset(self._recent)
        if len(self._recent) < self._recent.maxlen or len(views) > 2:
            return None
        self._recent.clear()
        if self._warned_views is not None and views <= self._warned_views:
            return "stop"
        self._warned_views = views
        return "correct"

    @staticmethod
    def _digest(result: dict[str, Any], arguments: dict[str, Any]) -> str:
        # Select semantic fields explicitly. Per-snapshot element IDs and image
        # delivery handles must not manufacture apparent progress.
        fields = (
            "role",
            "subrole",
            "title",
            "label",
            "help",
            "value",
            "enabled",
            "selected",
            "checked",
            "bounds",
            "actions",
            "value_settable",
            "attribute_reads",
            "secure",
        )
        projection = {
            "elements": [
                {key: item[key] for key in fields if key in item}
                for item in result["elements"]
                if isinstance(item, dict)
            ],
            "target": result.get("target"),
            "coverage": result.get("coverage"),
            "truncated": result.get("truncated"),
            "assertions": arguments.get("assertions"),
        }
        return hashlib.sha256(
            json.dumps(projection, sort_keys=True, ensure_ascii=True).encode()
        ).hexdigest()
