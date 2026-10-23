"""Baseline-first regression attribution (verification protocol, step 3).

Attribution needs facts recorded *before* the first edit. A baseline is the
parsed, per-test outcome of a test run that actually executed before any
verification-relevant edit. Attribution is then a set difference against the
observed post-edit outcomes of the **same normalized executed command**:

* ``pre_existing`` — a failing/errored test id already failing in the
  same-command baseline (the change did not cause it);
* ``regression`` — a failing id absent from that baseline (new since the
  change);
* ``unattributed`` — a failing id with no comparable (same normalized command)
  baseline, so its relationship to the change is unknown — a distinct honest
  state, never silently treated as pre-existing OR as a regression;
* ``agent_authored`` — a failing id whose test *file* the agent created this
  turn (a failing repro test the agent just wrote is signal, not a regression).

This module is pure and side-effect free. It provides:

* format parsers for pytest short-summary output and unittest/Django
  ``runtests`` output (fact extraction, not NL heuristics); on any ambiguity a
  parser returns *counts-unknown* rather than guessing, and never raises;
* the normalized baseline key (reusing step 2's command normalization) so a
  post-edit run is compared only against a baseline of the same executed
  command — comparability by identity, never by fuzzy scope inference;
* the pure diff classifier and its aggregation across several post-edit runs.

No git state is ever mutated to reconstruct a baseline: baselines come only from
runs that actually happened pre-edit.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from ..branding import env_get
from ..config import split_command_words
from ..pipeline_facts import pipeline_meaningful_stage
from .verification_commands import _normalize_shell_command_for_match

# ---------------------------------------------------------------------------
# Kill-switch (mirrors the route-arbitration / evidence-v2 idiom of steps 1-2)
# ---------------------------------------------------------------------------


def _regression_baseline_enabled(cfg: Any | None) -> bool:
    """Kill-switch for the baseline-first regression protocol (step 3).

    ``ALYSIS_REGRESSION_BASELINE`` (off/0/false/no/disabled) wins over the
    config value; default is on. When off, capture may still record telemetry
    but the completion-gate policy is fully legacy.
    """
    env_value = env_get("ALYSIS_REGRESSION_BASELINE")
    if env_value is not None:
        normalized = str(env_value).strip().lower()
        if normalized in {"off", "0", "false", "no", "disabled"}:
            return False
        if normalized in {"on", "1", "true", "yes", "enabled"}:
            return True
    return bool(getattr(cfg, "regression_baseline_enabled", True))


# ---------------------------------------------------------------------------
# Parsed test-run report (fact extraction)
# ---------------------------------------------------------------------------

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# pytest short-summary node-id lines, e.g. "FAILED path::test - reason".
_PYTEST_SUMMARY_LINE_RE = re.compile(r"^(FAILED|ERROR)\s+(\S.*?)\s*$")
# The "short test summary info" section header; failing node-id lines follow it.
_PYTEST_SUMMARY_HEADER_RE = re.compile(r"short test summary info")
# pytest final counts line: decorated with '=' (full verbosity) — e.g.
# "==== 2 failed, 116 passed in 1.2s ====".
_PYTEST_COUNTS_LINE_RE = re.compile(r"^=+\s*(?P<body>.*?)\s*=+\s*$")
# ...or undecorated (quiet mode, ``-q``) — e.g. "2 failed, 116 passed in 1.2s".
# Requires a trailing "in <time>s" so it never matches a node-id/reason line.
_PYTEST_UNDECORATED_COUNTS_RE = re.compile(r"\bin\s+\d+(?:\.\d+)?s\b\s*$")
_PYTEST_COUNT_TOKEN_RE = re.compile(
    r"(\d+)\s+(passed|failed|errors?|skipped|xfailed|xpassed|deselected|warnings?)\b"
)
# unittest / Django runtests.
_UNITTEST_RESULT_LINE_RE = re.compile(r"^(FAIL|ERROR):\s+(\S.*?)\s*$")
_UNITTEST_RAN_RE = re.compile(r"^Ran\s+(\d+)\s+tests?\s+in\b")
_UNITTEST_FAILED_HEADER_RE = re.compile(r"^FAILED\s*\((?P<body>.*)\)\s*$")
_UNITTEST_OK_RE = re.compile(r"^OK\b")
_UNITTEST_COUNT_TOKEN_RE = re.compile(r"(failures|errors|skipped|expected failures)\s*=\s*(\d+)")


@dataclass(frozen=True)
class TestReport:
    """Parsed per-test outcome of one executed test run.

    ``counts_known`` is False when the runner's summary could not be parsed
    (truncated/garbled output); such a report can never serve as a baseline.
    ``ids_complete`` is True only when every counted failure/error also produced
    a parsed node id — a truncated run (``pytest … | tail -5``) yields
    counts_known=True but ids_complete=False, so it is not comparable and its
    failures fall to ``unattributed`` rather than being guessed.
    """

    runner: str = "unknown"
    failed_ids: tuple[str, ...] = ()
    error_ids: tuple[str, ...] = ()
    passed: int | None = None
    failed: int | None = None
    skipped: int | None = None
    errors: int | None = None
    counts_known: bool = False

    @property
    def failing_ids(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((*self.failed_ids, *self.error_ids)))

    @property
    def ids_complete(self) -> bool:
        if not self.counts_known:
            return False
        if self.failed is not None and len(self.failed_ids) != self.failed:
            return False
        if self.errors is not None and len(self.error_ids) != self.errors:
            return False
        return True

    @property
    def usable_as_baseline(self) -> bool:
        return self.counts_known and self.ids_complete

    def as_payload(self) -> dict[str, Any]:
        return {
            "runner": self.runner,
            "failed_ids": list(self.failed_ids),
            "error_ids": list(self.error_ids),
            "passed": self.passed,
            "failed": self.failed,
            "skipped": self.skipped,
            "errors": self.errors,
            "counts_known": self.counts_known,
            "ids_complete": self.ids_complete,
        }

    @classmethod
    def from_payload(cls, payload: Any) -> TestReport | None:
        """Restore host-parsed facts, recomputing completeness rather than trusting it."""
        if not isinstance(payload, dict):
            return None
        runner = payload.get("runner")
        counts_known = payload.get("counts_known")
        if not isinstance(runner, str) or not runner or type(counts_known) is not bool:
            return None
        identifiers = {}
        for key in ("failed_ids", "error_ids"):
            value = payload.get(key)
            if not isinstance(value, list) or not all(
                isinstance(item, str) and item for item in value
            ):
                return None
            identifiers[key] = tuple(value)
        counts = {}
        for key in ("passed", "failed", "skipped", "errors"):
            value = payload.get(key)
            if value is not None and (type(value) is not int or value < 0):
                return None
            counts[key] = value
        if counts_known and (
            runner == "unknown" or counts["failed"] is None or counts["errors"] is None
        ):
            return None
        return cls(runner=runner, counts_known=counts_known, **identifiers, **counts)


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", str(text or ""))


def _pytest_node_id(raw: str) -> str | None:
    """Extract a node id from a pytest FAILED/ERROR summary line body.

    The reason separator is ``" - "`` at bracket depth 0, so a parametrized id
    whose param value contains a literal ``" - "`` (e.g. ``test[a - 2]``) is kept
    whole rather than truncated. Keeps the candidate only when it looks like a
    real node id (contains ``::`` or ends in ``.py``); a candidate that fails this
    shape check is dropped rather than guessed at, so a mis-parsed line never
    becomes a phantom regression.
    """
    candidate = str(raw or "").strip()
    depth = 0
    index = 0
    length = len(candidate)
    reason_at = -1
    while index < length:
        char = candidate[index]
        if char == "[":
            depth += 1
        elif char == "]":
            if depth > 0:
                depth -= 1
        elif depth == 0 and candidate.startswith(" - ", index):
            reason_at = index
            break
        index += 1
    if reason_at != -1:
        candidate = candidate[:reason_at].strip()
    if not candidate:
        return None
    if "::" in candidate or candidate.endswith(".py"):
        return candidate
    return None


def _pytest_counts_body(line: str) -> str | None:
    """Return the count body of a pytest summary line, decorated or not."""
    stripped = line.strip()
    match = _PYTEST_COUNTS_LINE_RE.match(stripped)
    if match is not None:
        body = match.group("body")
    elif _PYTEST_UNDECORATED_COUNTS_RE.search(stripped):
        body = stripped
    else:
        return None
    if not body:
        return None
    lowered = body.lower()
    if "no tests ran" in lowered or _PYTEST_COUNT_TOKEN_RE.search(lowered):
        return lowered
    return None


def parse_pytest_report(output: str) -> TestReport | None:
    """Parse pytest output; return ``None`` when it is not pytest output.

    Failing node ids are read only from the "short test summary info" section, so
    a stray ``FAILED …`` line in captured stdout or logs never becomes a phantom
    id. Counts are read from the final summary line — decorated (``=== … ===``)
    or the undecorated quiet-mode (``-q``) form.
    """
    text = _strip_ansi(output)
    lines = text.splitlines()

    counts_body: str | None = None
    for line in lines:
        body = _pytest_counts_body(line)
        if body is not None:
            counts_body = body  # keep scanning; the final summary line wins
    if counts_body is None:
        return None

    passed = failed = skipped = errors = 0
    if "no tests ran" not in counts_body:
        for value, word in _PYTEST_COUNT_TOKEN_RE.findall(counts_body):
            amount = int(value)
            if word == "passed":
                passed = amount
            elif word == "failed":
                failed = amount
            elif word.startswith("error"):
                errors = amount
            elif word == "skipped":
                skipped = amount

    failed_ids: list[str] = []
    error_ids: list[str] = []
    in_summary_section = False
    for line in lines:
        if _PYTEST_SUMMARY_HEADER_RE.search(line):
            in_summary_section = True
            continue
        if not in_summary_section:
            continue
        match = _PYTEST_SUMMARY_LINE_RE.match(line.strip())
        if match is None:
            continue
        node_id = _pytest_node_id(match.group(2))
        if node_id is None:
            continue
        if match.group(1) == "FAILED":
            failed_ids.append(node_id)
        else:
            error_ids.append(node_id)

    return TestReport(
        runner="pytest",
        failed_ids=tuple(dict.fromkeys(failed_ids)),
        error_ids=tuple(dict.fromkeys(error_ids)),
        passed=passed,
        failed=failed,
        skipped=skipped,
        errors=errors,
        counts_known=True,
    )


def parse_unittest_report(output: str) -> TestReport | None:
    """Parse unittest/Django ``runtests`` output; ``None`` when not that shape."""
    text = _strip_ansi(output)
    lines = text.splitlines()

    ran_at = next(
        (
            index
            for index in range(len(lines) - 1, -1, -1)
            if _UNITTEST_RAN_RE.match(lines[index].strip())
        ),
        None,
    )
    if ran_at is None:
        return None

    failed_ids: list[str] = []
    error_ids: list[str] = []
    for line in lines:
        match = _UNITTEST_RESULT_LINE_RE.match(line.strip())
        if match is None:
            continue
        identifier = match.group(2).strip()
        if not identifier:
            continue
        if match.group(1) == "FAIL":
            failed_ids.append(identifier)
        else:
            error_ids.append(identifier)

    failures = errors = skipped = 0
    terminal_seen = False
    for line in lines[ran_at + 1 :]:
        stripped = line.strip()
        if _UNITTEST_OK_RE.match(stripped):
            terminal_seen = True
            for word, value in _UNITTEST_COUNT_TOKEN_RE.findall(stripped):
                if word == "skipped":
                    skipped = int(value)
            continue
        header = _UNITTEST_FAILED_HEADER_RE.match(stripped)
        if header is None:
            continue
        terminal_seen = True
        for word, value in _UNITTEST_COUNT_TOKEN_RE.findall(header.group("body")):
            amount = int(value)
            if word == "failures":
                failures = amount
            elif word == "errors":
                errors = amount
            elif word == "skipped":
                skipped = amount

    return TestReport(
        runner="unittest",
        failed_ids=tuple(dict.fromkeys(failed_ids)),
        error_ids=tuple(dict.fromkeys(error_ids)),
        passed=None,
        failed=failures if terminal_seen else None,
        skipped=skipped if terminal_seen else None,
        errors=errors if terminal_seen else None,
        counts_known=terminal_seen,
    )


def parse_test_report(output: str) -> TestReport:
    """Best-effort format parse of test-runner output.

    Tries the pytest summary shape, then unittest/Django. Neither matching (or
    truncated/garbled output) yields a counts-unknown report — never an
    exception, never a guess. Deterministic for a given input.
    """
    try:
        report = parse_pytest_report(output)
        if report is not None:
            return report
        report = parse_unittest_report(output)
        if report is not None:
            return report
    except Exception:  # noqa: BLE001 - a parser must never raise on hostile text
        return TestReport(runner="unknown", counts_known=False)
    return TestReport(runner="unknown", counts_known=False)


# ---------------------------------------------------------------------------
# Baseline keying + records
# ---------------------------------------------------------------------------


def baseline_command_key(command: str) -> str:
    """Normalized identity of an executed command for baseline comparison.

    Reuses step 2's pipeline-stage identification and command normalization, so
    ``pytest foo``, ``pytest foo | tail -40`` and ``pytest foo | tail -5`` all
    key to the same baseline (comparability by identity, invariant to piping),
    while a genuinely different command keys differently and is not comparable.
    """
    meaningful = pipeline_meaningful_stage(command)
    target = meaningful if meaningful is not None else str(command or "")
    return _normalize_shell_command_for_match(target)


_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _program_basename(token: str) -> str:
    """Basename of a program token, normalizing both path separators.

    posix ``shlex`` would have eaten backslashes, so a Windows venv path
    (``C:\\venv\\Scripts\\pytest.exe``) is handled here by normalizing ``\\`` to
    ``/`` before taking the basename and stripping a ``.exe`` suffix.
    """
    name = token.replace("\\", "/").rsplit("/", 1)[-1].casefold()
    if name.endswith(".exe"):
        name = name[:-4]
    return name


def command_is_test_runner(command: str) -> bool:
    """True when the meaningful first-stage program is pytest or unittest/Django.

    Baseline capture is scoped to the runners the parsers understand; other
    (qualifying) executions — repo-native validation scripts, linters — do not
    emit per-test ids and are out of scope for regression attribution.
    """
    meaningful = pipeline_meaningful_stage(command)
    target = meaningful if meaningful is not None else str(command or "")
    tokens = split_command_words(target)
    if tokens is None:
        return False
    index = 0
    while index < len(tokens):
        token = tokens[index]
        # Strip a leading env-var prefix (``PYTHONPATH=. pytest`` / ``env FOO=b``).
        if token == "env" or _ENV_ASSIGNMENT_RE.match(token):
            index += 1
            continue
        break
    tokens = tokens[index:]
    if not tokens:
        return False
    head = _program_basename(tokens[0])
    lowered = [token.casefold() for token in tokens]
    if head in {"pytest", "py.test"}:
        return True
    if head in {"python", "python3", "py"}:
        # ``-m pytest`` / ``-m unittest`` and the glued ``-mpytest`` / ``-munittest``.
        if len(lowered) >= 3 and lowered[1] == "-m" and lowered[2] in {"pytest", "unittest"}:
            return True
        if len(lowered) >= 2 and lowered[1] in {"-mpytest", "-munittest"}:
            return True
        # manage.py test / runtests.py style django test entrypoints.
        if (
            any(_program_basename(token) == "manage.py" for token in tokens[1:])
            and "test" in lowered
        ):
            return True
        if any(_program_basename(token) in {"runtests.py", "runtests"} for token in tokens[1:]):
            return True
    if head in {"runtests", "runtests.py"}:
        return True
    if head == "django-admin" and "test" in lowered:
        return True
    return False


@dataclass(frozen=True)
class BaselineRecord:
    """A test run recorded before any verification-relevant edit (a baseline)."""

    command: str
    command_key: str
    report: TestReport
    edit_generation: int
    timestamp: str = ""

    @property
    def failing_ids(self) -> frozenset[str]:
        return frozenset(self.report.failing_ids)

    @property
    def usable(self) -> bool:
        return self.report.usable_as_baseline

    def as_payload(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "command_key": self.command_key,
            "edit_generation": self.edit_generation,
            "timestamp": self.timestamp,
            "report": self.report.as_payload(),
        }


@dataclass(frozen=True)
class PostEditTestRun:
    """A qualifying test run observed after a verification-relevant edit."""

    command: str
    command_key: str
    report: TestReport
    generation: int

    def as_payload(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "command_key": self.command_key,
            "generation": self.generation,
            "report": self.report.as_payload(),
        }


# ---------------------------------------------------------------------------
# Diff classification (pure)
# ---------------------------------------------------------------------------


def _created_path_components(path: str) -> tuple[str, ...]:
    cleaned = str(path or "").strip().replace("\\", "/").casefold()
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return tuple(part for part in cleaned.split("/") if part not in {"", "."})


def node_id_file_path(test_id: str) -> str | None:
    """Return the source-file portion of a pytest node id, else ``None``.

    ``tests/test_foo.py::TestX::test_bar`` -> ``tests/test_foo.py``. unittest ids
    (``test_bar (pkg.mod.Class)``) carry no file path and return ``None`` — so
    agent-authored attribution is available for pytest ids only. (Named without a
    ``test_`` prefix so importing it into a test module never trips pytest's
    test-collection.)
    """
    text = str(test_id or "").strip()
    if "::" in text:
        return text.split("::", 1)[0].strip() or None
    if text.endswith(".py"):
        return text
    return None


def _id_file_is_agent_created(test_id: str, created_components: list[tuple[str, ...]]) -> bool:
    file_path = node_id_file_path(test_id)
    if not file_path:
        return False
    id_components = _created_path_components(file_path)
    if not id_components:
        return False
    # Exact normalized-path match only. A suffix/basename match would let a
    # created ``test_foo.py`` collide with a pre-existing ``tests/test_foo.py``
    # and wrongly mark that file's genuine regression as agent-authored — the
    # dangerous direction (a shipped regression). Under-matching instead lets a
    # genuinely agent-authored failure be treated as a regression (a wasted
    # session at worst); we bias toward that safe direction.
    return any(id_components == created for created in created_components if created)


@dataclass(frozen=True)
class RegressionDiffResult:
    pre_existing: tuple[str, ...] = ()
    regressions: tuple[str, ...] = ()
    unattributed: tuple[str, ...] = ()
    agent_authored: tuple[str, ...] = ()
    has_comparable_baseline: bool = False
    baseline_command: str | None = None

    @property
    def blocks(self) -> bool:
        return bool(self.regressions)

    @property
    def has_failures(self) -> bool:
        return bool(
            self.pre_existing or self.regressions or self.unattributed or self.agent_authored
        )

    @property
    def all_failures_benign(self) -> bool:
        """True when there are failures and none is a regression or unattributed."""
        return self.has_failures and not self.regressions and not self.unattributed

    def as_payload(self) -> dict[str, Any]:
        return {
            "pre_existing": list(self.pre_existing),
            "regressions": list(self.regressions),
            "unattributed": list(self.unattributed),
            "agent_authored": list(self.agent_authored),
            "has_comparable_baseline": self.has_comparable_baseline,
            "baseline_command": self.baseline_command,
        }


def classify_regression_diff(
    *,
    post_report: TestReport,
    baseline: BaselineRecord | None,
    agent_created_paths: Iterable[str] = (),
) -> RegressionDiffResult:
    """Classify one post-edit run's failures against its same-command baseline.

    A comparable baseline requires both the baseline and the post-edit report to
    be counts-known with complete ids; otherwise every failure is
    ``unattributed`` (honest — never guessed as pre-existing or regression).
    Agent-authored test files win first: a failing test the agent just wrote is
    signal, not a regression.
    """
    created_components = [
        components
        for components in (_created_path_components(path) for path in agent_created_paths)
        if components
    ]
    comparable = baseline is not None and baseline.usable and post_report.usable_as_baseline
    baseline_failing = baseline.failing_ids if baseline is not None else frozenset()

    pre_existing: list[str] = []
    regressions: list[str] = []
    unattributed: list[str] = []
    agent_authored: list[str] = []
    for test_id in post_report.failing_ids:
        if _id_file_is_agent_created(test_id, created_components):
            agent_authored.append(test_id)
        elif not comparable:
            unattributed.append(test_id)
        elif test_id in baseline_failing:
            pre_existing.append(test_id)
        else:
            regressions.append(test_id)

    return RegressionDiffResult(
        pre_existing=tuple(pre_existing),
        regressions=tuple(regressions),
        unattributed=tuple(unattributed),
        agent_authored=tuple(agent_authored),
        has_comparable_baseline=comparable,
        baseline_command=baseline.command if (comparable and baseline is not None) else None,
    )


def aggregate_regression_results(
    results: Iterable[RegressionDiffResult],
) -> RegressionDiffResult:
    """Combine per-run diffs with honest precedence over shared ids.

    agent_authored > pre_existing > regression > unattributed: a file the agent
    created wins everywhere; an id seen failing in any comparable baseline is
    pre-existing before it can be called a regression; an id is only
    unattributed when no comparable run ever explained it.
    """
    results = list(results)
    agent_authored: list[str] = []
    pre_existing: list[str] = []
    regressions: list[str] = []
    unattributed: list[str] = []
    baseline_commands: list[str] = []
    has_comparable = False
    for result in results:
        agent_authored.extend(result.agent_authored)
        pre_existing.extend(result.pre_existing)
        regressions.extend(result.regressions)
        unattributed.extend(result.unattributed)
        has_comparable = has_comparable or result.has_comparable_baseline
        if result.regressions and result.baseline_command:
            baseline_commands.append(result.baseline_command)

    authored = list(dict.fromkeys(agent_authored))
    authored_set = set(authored)
    pre = [tid for tid in dict.fromkeys(pre_existing) if tid not in authored_set]
    pre_set = set(pre)
    reg = [
        tid for tid in dict.fromkeys(regressions) if tid not in authored_set and tid not in pre_set
    ]
    reg_set = set(reg)
    un = [
        tid
        for tid in dict.fromkeys(unattributed)
        if tid not in authored_set and tid not in pre_set and tid not in reg_set
    ]
    baseline_command = ", ".join(dict.fromkeys(baseline_commands)) if baseline_commands else None
    return RegressionDiffResult(
        pre_existing=tuple(pre),
        regressions=tuple(reg),
        unattributed=tuple(un),
        agent_authored=tuple(authored),
        has_comparable_baseline=has_comparable,
        baseline_command=baseline_command,
    )


EMPTY_REGRESSION_DIFF = RegressionDiffResult()
