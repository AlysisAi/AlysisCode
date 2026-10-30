"""Reproduction-first protocol for bug-fix-shaped tasks (verification step 5).

The failure this targets is a *plausible patch that misses the reported symptom*:
the agent fixes its interpretation of the bug, verifies that interpretation, and
finalizes believing it is done. Nothing in steps 1-4 catches it, because every
signal the agent produced (edits, passing tests, confirmed expectations) is about
the agent's own reading of the task.

The protocol adds one fact the agent cannot manufacture from its interpretation:

1. before touching product code, derive a minimal reproduction of the *reported*
   symptom and run it;
2. it must FAIL on the unpatched tree. A repro that passes pre-fix is evidence the
   interpretation is wrong -- revise the repro (bounded rounds), do not start
   editing;
3. after patching, the same repro must pass;
4. the summary states the repro status either way -- never silent success;
5. repro scaffolding never reaches the delivered diff.

Design invariants (identical in spirit to steps 2-4):

* Every function here is pure and unit-testable without an LLM call. The single
  exception, ``surviving_repro_artifacts``, takes an explicit root and only stats
  paths (mirroring ``acceptance_contract``'s bounded filesystem probes).
* Phase classification is a *fact*, never a heuristic: a repro run is pre-fix iff
  no non-artifact repo path had been touched when it ran. Writing the repro is
  itself a material edit, so edit generation cannot discriminate -- the artifact
  set can.
* Task shape comes from the validated provider-neutral semantic router contract.
  This module maps that enum into protocol state and never interprets user
  language itself.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any

from ..branding import env_get
from ..verification_command_analysis import analyze_verification_command
from .turn_contract import TurnSemantics, TurnTaskShape

# ---------------------------------------------------------------------------
# Kill-switch (mirrors the evidence-v2 / regression / turn-contract idiom)
# ---------------------------------------------------------------------------


def _reproduction_first_enabled(cfg: Any | None) -> bool:
    """Kill-switch for the reproduction-first protocol (step 5).

    ``ALYSIS_REPRODUCTION_FIRST`` (off/0/false/no/disabled) wins over the config
    value; default is on. When off, repro runs are still captured for telemetry but
    the completion-gate policy and the turn directives revert to legacy.
    """
    env_value = env_get("ALYSIS_REPRODUCTION_FIRST")
    if env_value is not None:
        normalized = str(env_value).strip().lower()
        if normalized in {"off", "0", "false", "no", "disabled"}:
            return False
        if normalized in {"on", "1", "true", "yes", "enabled"}:
            return True
    return bool(getattr(cfg, "reproduction_first_enabled", True))


# ---------------------------------------------------------------------------
# Task shape (derived from the semantic contract)
# ---------------------------------------------------------------------------


class TaskShape(StrEnum):
    """Whether a task reports a symptom that can be reproduced."""

    #: The task reports defective behavior -- a repro can demonstrate it.
    BUG_FIX = "bug_fix"
    #: Anything else (feature, docs, refactor, question). No repro phase.
    OTHER = "other"


def task_shape_from_turn_semantics(semantics: TurnSemantics | None) -> TaskShape:
    """Map a validated router verdict to the reproduction protocol's state."""

    if semantics is None:
        return TaskShape.OTHER
    return TaskShape.BUG_FIX if semantics.task_shape is TurnTaskShape.BUG_FIX else TaskShape.OTHER


# ---------------------------------------------------------------------------
# Repro runs (facts)
# ---------------------------------------------------------------------------


class ReproPhase(StrEnum):
    """When a repro run happened relative to the first product edit."""

    #: Ran while no non-artifact repo path had been touched -- the unpatched tree.
    PRE_FIX = "pre_fix"
    #: Ran after product code had already been edited.
    POST_FIX = "post_fix"


class ReproStatus(StrEnum):
    """The reproduction protocol's outcome for a turn."""

    #: No run of an agent-created artifact was observed at all.
    NOT_ATTEMPTED = "not_attempted"
    #: Pre-fix runs happened but none failed -- the interpretation is unconfirmed.
    NOT_REPRODUCING = "not_reproducing"
    #: A pre-fix run failed; no post-fix run has been observed yet.
    FAILING_PRE_FIX = "failing_pre_fix"
    #: A pre-fix run failed and the latest post-fix run still fails.
    FAILING_POST_FIX = "failing_post_fix"
    #: A post-fix run passes but no failing pre-fix run was ever observed.
    PASSING_UNVALIDATED = "passing_unvalidated"
    #: Failed before the fix, passes after it. The only satisfied state.
    PASSING_POST_FIX = "passing_post_fix"


#: Bounded revision rounds for a repro that does not reproduce the symptom.
#: After this many, the run proceeds to the fix and finalizes honestly rather
#: than deadlocking on a symptom the environment may genuinely not expose.
MAX_REPRO_REVISION_ROUNDS = 2

#: Cap on retained repro runs / artifact paths, so a long turn's state stays small.
MAX_REPRO_RUNS = 40
MAX_REPRO_ARTIFACTS = 20

#: Trailing punctuation and selector syntax stripped from a command token before
#: comparing it to an artifact path (``pytest repro_x.py::test_case,``).
_COMMAND_TOKEN_SPLIT_RE = re.compile(r"[\s;|&()<>]+")
_TOKEN_TRAILING_JUNK = ",;:'\"`)]}"


def _normalize_artifact_path(path: str) -> str:
    cleaned = str(path or "").strip().strip("`'\"").replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned.strip("/")


def _command_path_tokens(command: str) -> list[str]:
    if analyze_verification_command(command, trusted=True).uses_opaque_inline_code:
        return []
    tokens: list[str] = []
    for raw in _COMMAND_TOKEN_SPLIT_RE.split(str(command or "")):
        token = raw.strip().strip("`'\"").rstrip(_TOKEN_TRAILING_JUNK)
        if not token:
            continue
        # pytest node ids (``file.py::test``) and line suffixes (``file.py:12``).
        token = token.split("::", 1)[0]
        normalized = _normalize_artifact_path(token)
        if normalized:
            tokens.append(normalized)
    return tokens


def match_repro_artifacts(command: str, agent_created_paths: Iterable[str]) -> tuple[str, ...]:
    """Return the agent-created paths this command executes.

    Mechanical: a command "runs an artifact" when one of its path-shaped tokens is
    a path the agent created this turn. Matching is exact on the normalized path,
    or on a trailing path segment boundary (``pytest repro_x.py`` executed from the
    directory holding ``tests/repro_x.py``). Bare-basename matching against an
    unrelated file is impossible -- the segment boundary is required.
    """
    created = [
        normalized
        for normalized in (_normalize_artifact_path(path) for path in agent_created_paths)
        if normalized
    ]
    if not created:
        return ()
    tokens = _command_path_tokens(command)
    if not tokens:
        return ()
    matched: list[str] = []
    for artifact in created:
        for token in tokens:
            if (
                token == artifact
                or artifact.endswith(f"/{token}")
                or token.endswith(f"/{artifact}")
            ):
                matched.append(artifact)
                break
    return tuple(dict.fromkeys(matched))


@dataclass(frozen=True)
class ReproRun:
    """One observed execution of an agent-created artifact."""

    command: str
    artifact_paths: tuple[str, ...]
    phase: ReproPhase
    passed: bool
    exit_code: int | None = None
    #: Non-artifact repo paths already touched when the run happened (bounded).
    product_paths: tuple[str, ...] = ()

    def as_payload(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "artifact_paths": list(self.artifact_paths),
            "phase": self.phase.value,
            "passed": self.passed,
            "exit_code": self.exit_code,
            "product_paths": list(self.product_paths),
        }


def classify_repro_phase(
    *,
    touched_repo_paths: Iterable[str],
    created_paths: Iterable[str],
) -> tuple[ReproPhase, tuple[str, ...]]:
    """Classify a run as pre-fix or post-fix, and return the product paths.

    "Product code" is code that existed before this turn: a run is pre-fix exactly
    when no pre-existing repo path has been modified yet. Files the agent authored
    this turn -- the reproduction itself, and any new test it added -- are excluded,
    since creating them changes no existing behavior.

    This is a fact about recorded edits, never an inference about ordering intent.
    Writing the reproduction is itself a material edit, which is why the edit
    generation cannot discriminate here but the created-path set can.
    """
    created = {
        normalized
        for normalized in (_normalize_artifact_path(path) for path in created_paths)
        if normalized
    }
    product = sorted(
        normalized
        for normalized in (_normalize_artifact_path(path) for path in touched_repo_paths)
        if normalized and normalized not in created
    )
    phase = ReproPhase.PRE_FIX if not product else ReproPhase.POST_FIX
    return phase, tuple(product[:MAX_REPRO_ARTIFACTS])


# ---------------------------------------------------------------------------
# Assessment (pure)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReproAssessment:
    """The mechanical state of the reproduction protocol at a decision point."""

    status: ReproStatus = ReproStatus.NOT_ATTEMPTED
    #: True when the protocol applies at all (bug-fix-shaped execute turn, enabled).
    applicable: bool = False
    artifact_paths: tuple[str, ...] = ()
    failing_pre_fix_command: str = ""
    latest_post_fix_command: str = ""
    revision_rounds: int = 0
    #: Artifacts edited after product code was already changed (guardrail signal).
    edited_after_fix: tuple[str, ...] = ()
    #: Recorded artifacts still present in the tree at finalization.
    surviving_artifacts: tuple[str, ...] = ()

    @property
    def satisfied(self) -> bool:
        return self.status == ReproStatus.PASSING_POST_FIX

    @property
    def contradicted(self) -> bool:
        """The repro proves the delivered patch does not fix the reported symptom."""
        return self.status == ReproStatus.FAILING_POST_FIX

    @property
    def needs_revision(self) -> bool:
        return (
            self.status == ReproStatus.NOT_REPRODUCING
            and self.revision_rounds < MAX_REPRO_REVISION_ROUNDS
        )

    def as_payload(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "applicable": self.applicable,
            "satisfied": self.satisfied,
            "artifact_paths": list(self.artifact_paths),
            "failing_pre_fix_command": self.failing_pre_fix_command,
            "latest_post_fix_command": self.latest_post_fix_command,
            "revision_rounds": self.revision_rounds,
            "edited_after_fix": list(self.edited_after_fix),
            "surviving_artifacts": list(self.surviving_artifacts),
        }


def assess_reproduction(
    *,
    runs: Sequence[ReproRun],
    applicable: bool,
    artifact_paths: Iterable[str] = (),
    revision_rounds: int = 0,
    edited_after_fix: Iterable[str] = (),
    surviving_artifacts: Iterable[str] = (),
) -> ReproAssessment:
    """Derive the protocol status from the observed runs. Pure and deterministic.

    Precedence, most-specific first:

    * a failing pre-fix run is the validation anchor -- with one, the latest
      post-fix run decides pass/fail;
    * without one, a passing post-fix run is ``passing_unvalidated`` (the repro
      never demonstrated the bug, so its passing proves nothing);
    * pre-fix runs that all pass are ``not_reproducing`` -- the interpretation is
      wrong and the repro must be revised, not the product code edited.
    """
    ordered = list(runs)
    artifacts = tuple(
        dict.fromkeys(
            normalized
            for normalized in (_normalize_artifact_path(path) for path in artifact_paths)
            if normalized
        )
    )
    common = {
        "applicable": bool(applicable),
        "artifact_paths": artifacts,
        "revision_rounds": max(0, int(revision_rounds)),
        "edited_after_fix": tuple(
            dict.fromkeys(str(item) for item in edited_after_fix if str(item).strip())
        ),
        "surviving_artifacts": tuple(
            dict.fromkeys(str(item) for item in surviving_artifacts if str(item).strip())
        ),
    }
    if not ordered:
        return ReproAssessment(status=ReproStatus.NOT_ATTEMPTED, **common)

    pre_fix = [run for run in ordered if run.phase == ReproPhase.PRE_FIX]
    post_fix = [run for run in ordered if run.phase == ReproPhase.POST_FIX]
    failing_pre_fix = next((run for run in pre_fix if not run.passed), None)

    if failing_pre_fix is None:
        if pre_fix:
            return ReproAssessment(status=ReproStatus.NOT_REPRODUCING, **common)
        # Only post-fix runs exist: the repro was authored after product code
        # changed, so it never observed the unpatched behavior.
        latest = post_fix[-1]
        status = ReproStatus.PASSING_UNVALIDATED if latest.passed else ReproStatus.FAILING_POST_FIX
        return ReproAssessment(
            status=status,
            latest_post_fix_command=latest.command,
            **common,
        )

    if not post_fix:
        return ReproAssessment(
            status=ReproStatus.FAILING_PRE_FIX,
            failing_pre_fix_command=failing_pre_fix.command,
            **common,
        )
    latest = post_fix[-1]
    return ReproAssessment(
        status=ReproStatus.PASSING_POST_FIX if latest.passed else ReproStatus.FAILING_POST_FIX,
        failing_pre_fix_command=failing_pre_fix.command,
        latest_post_fix_command=latest.command,
        **common,
    )


def repro_blocks_finalization(assessment: ReproAssessment, *, material_edit_count: int) -> bool:
    """True when the gate must not let this turn finalize yet.

    A turn that changed nothing is handled by the ``no_material_edits`` stage; the
    repro requirement only bites once product code was actually changed.
    """
    if not assessment.applicable or material_edit_count <= 0:
        return False
    return not assessment.satisfied


# ---------------------------------------------------------------------------
# Artifact hygiene (the one filesystem-touching helper)
# ---------------------------------------------------------------------------


#: Directory names that mark a path as part of the project's own check surface.
#: Mirrors ``acceptance_contract._CHECK_PATH_MARKERS``.
_CHECK_PATH_MARKERS = frozenset(
    {"check", "checks", "test", "tests", "verify", "validation", "validator"}
)


def is_delivered_test_path(path: str) -> bool:
    """True when a path belongs to the project's test/check surface.

    A reproduction can legitimately *be* a new test the task wanted delivered
    (``tests/test_slugify.py``). Such a file is part of the change, not
    scaffolding, so the cleanup requirement must never demand its deletion. A
    standalone script the agent dropped beside the source is not covered.
    """
    normalized = _normalize_artifact_path(path)
    if not normalized:
        return False
    pure = PurePosixPath(normalized)
    name = pure.name.casefold()
    parts = {part.casefold() for part in pure.parts[:-1]}
    return (
        name.startswith("test_")
        or name.endswith("_test.py")
        or name.endswith(".test.js")
        or name.endswith(".spec.js")
        or bool(parts & _CHECK_PATH_MARKERS)
    )


def surviving_repro_artifacts(root: Path, paths: Iterable[str]) -> tuple[str, ...]:
    """Recorded repro scaffolding that still exists under ``root``.

    Scaffolding must not reach the delivered diff. Files on the project's test
    surface are excluded — a reproduction written as a real test is part of the
    change, not scaffolding. Paths that resolve outside the workspace, or that no
    longer exist, are not reported. Never raises.
    """
    surviving: list[str] = []
    for raw in paths:
        normalized = _normalize_artifact_path(raw)
        if not normalized or is_delivered_test_path(normalized):
            continue
        try:
            candidate = (root / normalized).resolve()
            candidate.relative_to(Path(root).resolve())
        except (OSError, ValueError):
            continue
        try:
            if candidate.exists():
                surviving.append(normalized)
        except OSError:
            continue
    return tuple(dict.fromkeys(surviving))


# ---------------------------------------------------------------------------
# Directives and advisories (agent-facing text)
# ---------------------------------------------------------------------------


_REPRODUCTION_FIRST_DIRECTIVE_BODY = (
    "- BEFORE editing product code, reproduce the EXACT symptom with the smallest "
    "relevant existing test or an inline assertion when possible. Use "
    "the same inputs the task describes, and an assertion on the expected result "
    "versus what the report says actually happens. Prefer any concrete code block the "
    "report quotes; do not generalize it.\n"
    "- Run the check on the current, unpatched tree and confirm the reported failure. "
    "Respect the user's file and execution scope throughout: a request to change only "
    "named files also excludes temporary reproduction files elsewhere. Do not create "
    "a separate reproduction when an existing test already demonstrates the symptom. "
    "Create a temporary check file only when needed and permitted by the request. "
    "If the task also asked for a test, "
    "writing it as a real test under the project's test directory counts - that one you "
    "keep.\n"
    "- If it passes before you change anything, your reading of the symptom is wrong: "
    "re-read the report and revise the reproduction. Do not start editing product code "
    "to make a passing reproduction meaningful.\n"
    "- After your fix, run the SAME reproduction unchanged; it must pass. If it does "
    "not, keep iterating on the fix. Never weaken, narrow, or delete the reproduction "
    "to make it pass, and do not edit it after the fix unless its expectation is "
    "demonstrably wrong - if you must, say so explicitly in your final answer.\n"
    "- Remove only temporary check files you created within the authorized scope; "
    "never remove an existing test. The delivered diff must contain "
    "only the real fix (plus any tests the task actually asked for).\n"
    "- Explain the observed behavior and check results in plain language: whether the "
    "same check failed before the fix and passed after it, or could not demonstrate "
    "the problem. Keep internal protocol names out of user-facing updates."
)

REPRODUCTION_FIRST_TURN_DIRECTIVE = (
    "Reproduction-first protocol (this task reports defective behavior):\n"
    + _REPRODUCTION_FIRST_DIRECTIVE_BODY
)

#: Router-free variant: no pre-turn task-shape prediction exists, so the model
#: decides applicability itself. The completion gate binds only after a failing
#: pre-fix run has actually been observed.
REPRODUCTION_FIRST_CONDITIONAL_DIRECTIVE = (
    "Reproduction-first protocol — apply it whenever the task reports defective "
    "behavior (an error, a crash, wrong output, a failing command); skip it for "
    "tasks that report no symptom. The user's scope limits still apply:\n"
    + _REPRODUCTION_FIRST_DIRECTIVE_BODY
)

REPRO_PRE_EDIT_ADVISORY = (
    "Reproduction advisory: you are changing product code and no reproduction of the "
    "reported symptom has been observed failing on the unpatched tree. Without that, a "
    "passing test after your change only confirms your interpretation of the report, "
    "not the report itself. If you can still do it, revert nothing - just write the "
    "smallest permitted check now, preferring existing tests or inline assertions, "
    "run it, and confirm what it shows. Do not create extra files outside the user's "
    "requested scope. Advisory only - this "
    "does not block your edit."
)

REPRO_NOT_REPRODUCING_ADVISORY = (
    "Your reproduction passed on the unpatched tree, so it does not exercise the "
    "reported symptom. That is a signal your reading of the report is wrong, not a "
    "signal to start editing. Re-read the report - the exact inputs, the exact expected "
    "value, the exact observed value - and revise the reproduction so it fails first. "
    "Do not weaken it to make it fail."
)

REPRO_ARTIFACT_EDITED_AFTER_FIX_ADVISORY = (
    "You edited a reproduction file after changing product code. A reproduction is only "
    "evidence while it stays the one that failed before the fix. Change it only if its "
    "expectation is demonstrably wrong, and say so explicitly in your final answer."
)


def build_repro_pre_edit_advisory(*, status: ReproStatus) -> str:
    """Pre-edit advisory, specialized for a repro that already failed to reproduce."""
    if status == ReproStatus.NOT_REPRODUCING:
        return REPRO_NOT_REPRODUCING_ADVISORY
    return REPRO_PRE_EDIT_ADVISORY


# ---------------------------------------------------------------------------
# Gate nudge / finalization markers (fail honest, never silent)
# ---------------------------------------------------------------------------


_REPRO_NUDGE_LINES = {
    ReproStatus.NOT_ATTEMPTED: (
        "- No reproduction of the reported symptom was run. Use the smallest permitted "
        "existing test or inline check of the exact symptom, run it, and confirm it now "
        "passes with your fix in place. Respect the user's file and execution limits. "
        "If you cannot make it reproduce at all, say so explicitly instead of implying "
        "the symptom is fixed."
    ),
    ReproStatus.NOT_REPRODUCING: (
        "- Your reproduction never failed on the unpatched tree, so nothing has confirmed "
        "you fixed the reported symptom rather than your reading of it. Revise the "
        "reproduction against the exact reported inputs and expected value, or state "
        "plainly that the symptom could not be reproduced."
    ),
    ReproStatus.FAILING_PRE_FIX: (
        "- Your reproduction failed before the fix and has not been re-run since. Run it "
        "again, unchanged, and confirm it now passes."
    ),
    ReproStatus.FAILING_POST_FIX: (
        "- Your reproduction still fails after the fix: the reported symptom is not "
        "resolved. Keep iterating on the fix and re-run it. Do not weaken or delete the "
        "reproduction to make it pass."
    ),
    ReproStatus.PASSING_UNVALIDATED: (
        "- Your reproduction passes, but it was never observed failing on the unpatched "
        "tree, so it does not show your change fixed anything. Confirm it actually "
        "exercises the reported symptom (for example against the pre-fix behavior), or "
        "say plainly that the fix is unvalidated."
    ),
}


def build_repro_nudge_line(assessment: ReproAssessment) -> str:
    """The bounded repair nudge line for the current repro status."""
    return _REPRO_NUDGE_LINES.get(assessment.status, "")


def build_repro_artifacts_nudge_line(paths: Sequence[str] | tuple[str, ...]) -> str:
    """Repair nudge for scaffolding still present in the working tree."""
    joined = ", ".join(str(item) for item in paths if str(item).strip())
    if not joined:
        return ""
    return (
        f"- Reproduction scaffolding is still in the working tree: {joined}. Delete it "
        "before finalizing so the delivered diff contains only the real change."
    )


_REPRO_STATUS_SUMMARY_LINES = {
    ReproStatus.PASSING_POST_FIX: ("Reproduction: failed before the fix, passes after it{detail}."),
    ReproStatus.FAILING_POST_FIX: (
        "⚠️ Reproduction: failed before the fix and STILL FAILS after it{detail} - the "
        "reported symptom is not resolved."
    ),
    ReproStatus.FAILING_PRE_FIX: (
        "⚠️ Reproduction: failed before the fix{detail}, but was never re-run afterwards - "
        "the fix is unconfirmed against the reported symptom."
    ),
    ReproStatus.NOT_REPRODUCING: (
        "⚠️ Reproduction: could not reproduce the reported symptom on the unpatched tree, "
        "so this change is not validated against the report{detail}."
    ),
    ReproStatus.PASSING_UNVALIDATED: (
        "⚠️ Reproduction: passes, but it was never observed failing before the fix{detail}, "
        "so it does not confirm the reported symptom was resolved."
    ),
    ReproStatus.NOT_ATTEMPTED: (
        "⚠️ Reproduction: none was run{detail}. This change was not validated against the "
        "reported symptom."
    ),
}


def build_repro_status_summary(assessment: ReproAssessment) -> str:
    """The visible reproduction-status line appended to a bug-fix turn's summary.

    Always emitted for an applicable turn -- a satisfied protocol reports plainly,
    an unsatisfied one reports the deficit. Never silence.
    """
    if not assessment.applicable:
        return ""
    template = _REPRO_STATUS_SUMMARY_LINES.get(assessment.status, "")
    if not template:
        return ""
    command = (
        assessment.latest_post_fix_command or assessment.failing_pre_fix_command or ""
    ).strip()
    detail = f" (`{command}`)" if command else ""
    line = template.format(detail=detail)
    if assessment.edited_after_fix:
        joined = ", ".join(assessment.edited_after_fix)
        line += f" Note: the reproduction was edited after the fix ({joined})."
    if assessment.surviving_artifacts:
        joined = ", ".join(assessment.surviving_artifacts)
        line += f" Reproduction scaffolding left in the tree: {joined}."
    return f"\n\n---\n{line}"
