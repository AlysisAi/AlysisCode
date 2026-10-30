"""Non-fakeable build identity: which commit is actually running.

Three behaviourally different builds once all reported themselves as version
``0.9.8``, and one benchmark campaign turned out to have run against an
unpinned "latest main". Every score those runs produced is therefore
unattributable: there is no way, now, to say which source tree earned which
number. A version string is a promise the developer makes; a commit hash
recorded at build time is a fact about the artifact.

This module owns three things:

*The generated fact.* ``scripts/generate_build_info.py`` writes
``_build_info.py`` next to this file, stamping the commit, its subject line, an
ISO-8601 UTC build timestamp, whether the working tree carried uncommitted
changes, and the SHA ``origin/main`` resolved to at build time. The copy
committed to the repository is a deliberate *dev default* -- no commit, marked
dirty -- so that an artifact built without running the generator is correctly
reported as unidentifiable rather than quietly inheriting a stale hash.

The last two exist because a hash alone did not stop the failure they were
added for: a campaign was built from the tip of an unmerged fork arm while
labelled "latest main", and the only tell was a stale version string noticed in
post-hoc teardown. The subject makes the wrong commit legible to a human; the
resolved ``origin/main`` makes "this is latest main" a checkable claim rather
than an assertion.

*The reading.* :func:`load_build_info` tolerates the file being absent,
truncated or hand-edited, because the failure mode that matters is a build
that cannot prove its identity, and that must surface as a clear refusal
rather than an import error.

*The refusal.* :func:`decide_clean_build` is the policy behind
``--require-clean-build`` / ``ALYSIS_REQUIRE_CLEAN_BUILD``. Benchmark
harnesses set it so a campaign cannot start against a build whose provenance
is unknown -- the precise mistake that cost the earlier campaign its meaning.

Stdlib only, including the shared command decoder: the generator script runs
this in a bare interpreter before dependencies are installed. The standalone
tests exercise the same package imports.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .command_text import command_text

BUILD_INFO_FILENAME = "_build_info.py"
#: Bumped to 2 when the commit subject and the resolved ``origin/main`` joined
#: the stamp. A schema-1 file still reads: the two new fields fall back to
#: empty and :data:`UNAVAILABLE`, which is exactly what such a stamp can prove.
BUILD_INFO_SCHEMA_VERSION = 2

REQUIRE_CLEAN_BUILD_ENV = "ALYSIS_REQUIRE_CLEAN_BUILD"

#: Written into the committed dev default. Any artifact still reporting this
#: source was built without the release generator.
DEV_DEFAULT_SOURCE = "dev-default"
GENERATED_SOURCE = "git"

#: The generator rewrites this file immediately before the wheel is built, so
#: git necessarily sees it as modified. Counting that self-inflicted edit as a
#: dirty tree would make every release build report itself dirty, so the one
#: path the generator writes is excluded from the dirty computation.
DIRTY_CHECK_EXCLUDED_PATHS = ("src/alysis_code/" + BUILD_INFO_FILENAME,)

_TRUTHY = frozenset({"1", "true", "yes", "on", "enabled", "require"})
_COMMIT_RE = re.compile(r"\A[0-9a-f]{7,64}\Z")
_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

_BUILD_INFO_ATTRIBUTES = (
    "BUILD_COMMIT",
    "BUILD_COMMIT_SUBJECT",
    "BUILD_TIMESTAMP",
    "BUILD_DIRTY",
    "BUILD_SOURCE",
    "BUILD_RESOLVED_ORIGIN_MAIN",
    "BUILD_INFO_SCHEMA_VERSION",
)

UNKNOWN = "unknown"

#: Recorded for ``origin/main`` when the build host could not resolve it -- no
#: network, no remote, no such ref. A build that cannot see the remote is a
#: normal offline build and must still stamp; it simply cannot claim to be
#: main. Distinct from the empty string, which would read as "not recorded".
UNAVAILABLE = "unavailable"


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BuildInfo:
    """What the artifact can prove about its own provenance."""

    commit: str = ""
    commit_subject: str = ""
    timestamp: str = ""
    dirty: bool = True
    source: str = DEV_DEFAULT_SOURCE
    resolved_origin_main: str = UNAVAILABLE
    schema_version: int = BUILD_INFO_SCHEMA_VERSION

    @property
    def commit_short(self) -> str:
        return self.commit[:12] if self.commit else ""

    @property
    def resolved_origin_main_short(self) -> str:
        if not _COMMIT_RE.fullmatch(self.resolved_origin_main.strip().casefold()):
            return ""
        return self.resolved_origin_main[:12]

    @property
    def origin_main_resolved(self) -> bool:
        """True when the build host actually resolved ``origin/main``.

        False for an offline build, which is not an error: it means the
        latest-main claim simply cannot be checked against anything.
        """
        return bool(self.resolved_origin_main_short)

    @property
    def is_origin_main(self) -> bool:
        """True when this build is exactly the commit ``origin/main`` pointed at.

        False when the two differ *and* when the remote was never resolved --
        a build that cannot see main must not be able to claim it is main.
        """
        if not (self.is_identifiable and self.origin_main_resolved):
            return False
        return self.commit.strip().casefold() == self.resolved_origin_main.strip().casefold()

    @property
    def is_identifiable(self) -> bool:
        """True when the recorded commit is a plausible git object name.

        Shape is checked rather than trusted: a hand-edited ``_build_info.py``
        containing ``BUILD_COMMIT = "probably main"`` must not pass for
        provenance.
        """
        return bool(_COMMIT_RE.fullmatch(self.commit.strip().casefold()))

    @property
    def is_clean(self) -> bool:
        return self.is_identifiable and not self.dirty

    def describe(self) -> str:
        """One-line human summary, as printed by ``alysis --version``."""
        return (
            f"commit: {self.commit_short or UNKNOWN}, "
            f"built: {self.timestamp or UNKNOWN}, "
            f"dirty: {'yes' if self.dirty else 'no'}, "
            f"source: {self.source or UNKNOWN}"
        )

    def telemetry_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "commit": self.commit,
            "commit_short": self.commit_short,
            "commit_subject": self.commit_subject,
            "timestamp": self.timestamp,
            "dirty": self.dirty,
            "source": self.source,
            "identifiable": self.is_identifiable,
            "clean": self.is_clean,
            "resolved_origin_main": self.resolved_origin_main,
            "origin_main_resolved": self.origin_main_resolved,
            "is_origin_main": self.is_origin_main,
        }


DEV_DEFAULT_BUILD_INFO = BuildInfo()


def version_line(version: str, info: BuildInfo | None = None) -> str:
    """Render the ``--version`` line.

    The bare version stays the leading token and the whole thing stays on one
    line: the VS Code extension, the managed-CLI smoke test and the release
    distribution validator all read this output, and they read it by prefix,
    substring and non-emptiness respectively.
    """
    resolved = load_build_info() if info is None else info
    return f"{str(version).strip()} ({resolved.describe()})"


# ---------------------------------------------------------------------------
# Reading the generated file
# ---------------------------------------------------------------------------


def _coerce_build_info(namespace: Mapping[str, Any]) -> BuildInfo:
    commit = str(namespace.get("BUILD_COMMIT") or "").strip()
    commit_subject = str(namespace.get("BUILD_COMMIT_SUBJECT") or "").strip()
    timestamp = str(namespace.get("BUILD_TIMESTAMP") or "").strip()
    source = str(namespace.get("BUILD_SOURCE") or "").strip() or DEV_DEFAULT_SOURCE
    # Absent (schema 1) and empty both mean "the remote was never resolved",
    # which is what UNAVAILABLE says. Failing closed here matters: the
    # latest-main guard treats an unresolved remote as "cannot claim main".
    resolved_origin_main = (
        str(namespace.get("BUILD_RESOLVED_ORIGIN_MAIN") or "").strip() or UNAVAILABLE
    )
    raw_dirty = namespace.get("BUILD_DIRTY", True)
    # Anything unparseable resolves to dirty. Provenance failures must fail
    # closed: a build that cannot say whether it was clean is not clean.
    if isinstance(raw_dirty, bool):
        dirty = raw_dirty
    elif isinstance(raw_dirty, (int, float)):
        dirty = bool(raw_dirty)
    elif isinstance(raw_dirty, str):
        dirty = raw_dirty.strip().casefold() not in {"false", "0", "no", "off"}
    else:
        dirty = True
    try:
        schema_version = int(namespace.get("BUILD_INFO_SCHEMA_VERSION", BUILD_INFO_SCHEMA_VERSION))
    except (TypeError, ValueError):
        schema_version = BUILD_INFO_SCHEMA_VERSION
    return BuildInfo(
        commit=commit,
        commit_subject=commit_subject,
        timestamp=timestamp,
        dirty=dirty,
        source=source,
        resolved_origin_main=resolved_origin_main,
        schema_version=schema_version,
    )


def _namespace_from_path(path: Path) -> dict[str, Any] | None:
    try:
        if not path.is_file():
            return None
        spec = importlib.util.spec_from_file_location("_alysis_build_info", path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception:  # noqa: BLE001 - a corrupt stamp must not break startup
        return None
    return {name: getattr(module, name) for name in _BUILD_INFO_ATTRIBUTES if hasattr(module, name)}


def default_build_info_path() -> Path:
    return Path(__file__).resolve().parent / BUILD_INFO_FILENAME


def read_build_info(path: str | os.PathLike[str] | None = None) -> BuildInfo:
    """Read build info from disk, uncached.

    Prefers the installed package module so a frozen or zipped distribution
    resolves the same way the rest of the package does, and falls back to
    loading the sibling file by path -- which is also what makes this readable
    from a bare interpreter that has never imported the package.
    """
    if path is None:
        try:
            from . import _build_info as module  # noqa: PLC0415
        except ImportError:
            module = None
        except Exception:  # noqa: BLE001 - corrupt stamp, fall through to path load
            module = None
        if module is not None:
            namespace = {
                name: getattr(module, name)
                for name in _BUILD_INFO_ATTRIBUTES
                if hasattr(module, name)
            }
            if namespace:
                return _coerce_build_info(namespace)
        path = default_build_info_path()
    namespace = _namespace_from_path(Path(path))
    if namespace is None:
        return DEV_DEFAULT_BUILD_INFO
    return _coerce_build_info(namespace)


_CACHED_BUILD_INFO: BuildInfo | None = None


def load_build_info() -> BuildInfo:
    """Read build info once per process."""
    global _CACHED_BUILD_INFO
    if _CACHED_BUILD_INFO is None:
        _CACHED_BUILD_INFO = read_build_info()
    return _CACHED_BUILD_INFO


def reset_build_info_cache_for_tests() -> None:
    global _CACHED_BUILD_INFO
    _CACHED_BUILD_INFO = None


# ---------------------------------------------------------------------------
# Generating the file
# ---------------------------------------------------------------------------

GitRunner = Callable[[Sequence[str]], "tuple[int, str]"]


def _run_git(args: Sequence[str], *, cwd: Path) -> tuple[int, str]:
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            **command_text(),
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return completed.returncode, completed.stdout


def build_timestamp(
    now: datetime | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> str:
    """ISO-8601 UTC build timestamp, honouring ``SOURCE_DATE_EPOCH``.

    The reproducible-builds variable is respected so that a deliberately
    reproducible release does not become irreproducible because of this stamp.
    """
    source: Mapping[str, str] = os.environ if environ is None else environ
    raw_epoch = str(source.get("SOURCE_DATE_EPOCH") or "").strip()
    if raw_epoch:
        try:
            return datetime.fromtimestamp(int(raw_epoch), tz=timezone.utc).strftime(
                _TIMESTAMP_FORMAT
            )
        except (TypeError, ValueError, OSError, OverflowError):
            pass
    moment = datetime.now(timezone.utc) if now is None else now
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime(_TIMESTAMP_FORMAT)


def parse_dirty_status(porcelain_output: str) -> bool:
    """Decide dirtiness from ``git status --porcelain`` output.

    The generator's own output file is excluded (see
    :data:`DIRTY_CHECK_EXCLUDED_PATHS`); everything else that git reports --
    modified, staged, or untracked-and-not-ignored -- counts, because any of
    them can change what the built artifact actually executes.
    """
    for line in str(porcelain_output or "").splitlines():
        entry = line.strip()
        if not entry:
            continue
        # Porcelain v1: two status characters, a space, then the path. A rename
        # reads "R  old -> new"; the destination is what ships.
        path = entry[2:].strip() if len(entry) > 2 else ""
        if "->" in path:
            path = path.split("->")[-1].strip()
        path = path.strip('"')
        if path and path not in DIRTY_CHECK_EXCLUDED_PATHS:
            return True
    return False


#: A commit subject is one line by construction, but nothing bounds its length.
#: The stamp is a generated source file read on every startup, so a pathological
#: subject is truncated rather than embedded whole.
COMMIT_SUBJECT_MAX_CHARS = 200


def read_commit_subject(runner: GitRunner, commit: str = "HEAD") -> str:
    """The commit's first message line, or ``""`` when git cannot say.

    Recorded because a hash alone does not tell an operator reading a run
    manifest *which* commit they are looking at. The subject is what makes a
    wrong build recognisable at a glance -- a benchmark that ran a merge commit
    off an unmerged fork arm looked, as a bare hash, exactly as plausible as
    the right one.
    """
    code, out = runner(["log", "-1", "--format=%s", commit])
    if code != 0 or not out.strip():
        return ""
    return out.strip().splitlines()[0].strip()[:COMMIT_SUBJECT_MAX_CHARS]


def resolve_origin_main(runner: GitRunner, *, fetch: bool = True) -> str:
    """The SHA ``origin/main`` resolves to, or :data:`UNAVAILABLE`.

    Fetched first so the answer describes the remote as it is now rather than
    whenever this checkout last synced -- a stale remote-tracking ref would let
    a build that is behind main still call itself main.

    Every failure lands on ``UNAVAILABLE`` rather than raising: an offline
    build, a checkout with no ``origin``, and a repository whose default branch
    is not ``main`` are all normal, and none should stop a build from stamping.
    What they cost is the ability to *claim* to be main, which is the honest
    outcome and is what the latest-main guard enforces.
    """
    if not fetch:
        # A cached remote ref does not establish the current remote tip. This
        # hermetic mode deliberately makes no latest-main provenance claim.
        return UNAVAILABLE
    # Best effort by design. A failed or timed-out fetch leaves the
    # remote-tracking ref where it was; the rev-parse below still answers,
    # and the guard compares against whatever could actually be resolved.
    runner(["fetch", "--quiet", "origin"])
    code, out = runner(["rev-parse", "origin/main"])
    if code != 0 or not out.strip():
        return UNAVAILABLE
    candidate = out.strip().splitlines()[0].strip().casefold()
    return candidate if _COMMIT_RE.fullmatch(candidate) else UNAVAILABLE


def generate_build_info(
    *,
    repo_root: str | os.PathLike[str],
    now: datetime | None = None,
    git_runner: GitRunner | None = None,
    environ: Mapping[str, str] | None = None,
    fetch: bool = True,
) -> BuildInfo:
    """Interrogate git and return the build stamp for the current tree.

    A tree with no git available, or no repository, yields the dev default:
    unidentifiable and dirty. That is the honest answer, and it is the answer
    ``--require-clean-build`` is designed to reject.

    ``fetch=False`` skips the network probe for ``origin/main``, which then
    stamps as :data:`UNAVAILABLE`. Use it for a deliberately hermetic build;
    the cost is that such a build can never satisfy the latest-main guard.
    """
    root = Path(repo_root)
    runner: GitRunner = git_runner if git_runner is not None else (lambda a: _run_git(a, cwd=root))

    code, out = runner(["rev-parse", "HEAD"])
    commit = out.strip().splitlines()[0].strip().casefold() if code == 0 and out.strip() else ""
    if not _COMMIT_RE.fullmatch(commit):
        return BuildInfo(
            commit="",
            timestamp=build_timestamp(now, environ=environ),
            dirty=True,
            source=DEV_DEFAULT_SOURCE,
        )

    status_code, status_out = runner(["status", "--porcelain"])
    # A failed status probe is treated as dirty for the same fail-closed reason
    # as an unparseable stamp: not knowing is not the same as being clean.
    dirty = True if status_code != 0 else parse_dirty_status(status_out)
    return BuildInfo(
        commit=commit,
        commit_subject=read_commit_subject(runner, commit),
        timestamp=build_timestamp(now, environ=environ),
        dirty=dirty,
        source=GENERATED_SOURCE,
        resolved_origin_main=resolve_origin_main(runner, fetch=fetch),
    )


def render_build_info_module(info: BuildInfo) -> str:
    """Render the generated ``_build_info.py`` source text.

    String literals are emitted via ``json.dumps`` rather than ``repr`` so the
    generated file is double-quoted and survives the repository's
    ``ruff format`` check unchanged.
    """
    return (
        '"""Generated build identity. Do not edit by hand.\n'
        "\n"
        "Regenerate with ``python3 scripts/generate_build_info.py``. The copy\n"
        "committed to the repository is the dev default and deliberately reports an\n"
        "unidentifiable build, so that an artifact built without running the\n"
        "generator is refused by ``--require-clean-build`` instead of inheriting a\n"
        "stale commit hash.\n"
        '"""\n'
        "\n"
        f"BUILD_COMMIT = {json.dumps(info.commit)}\n"
        f"BUILD_COMMIT_SUBJECT = {json.dumps(info.commit_subject)}\n"
        f"BUILD_TIMESTAMP = {json.dumps(info.timestamp)}\n"
        f"BUILD_DIRTY = {bool(info.dirty)!r}\n"
        f"BUILD_SOURCE = {json.dumps(info.source)}\n"
        f"BUILD_RESOLVED_ORIGIN_MAIN = {json.dumps(info.resolved_origin_main)}\n"
        f"BUILD_INFO_SCHEMA_VERSION = {int(info.schema_version)!r}\n"
    )


def write_build_info(info: BuildInfo, path: str | os.PathLike[str]) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_build_info_module(info), encoding="utf-8", newline="\n")
    return target


# ---------------------------------------------------------------------------
# The refusal
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CleanBuildDecision:
    """Whether startup may proceed under ``--require-clean-build``."""

    required: bool
    allowed: bool
    reason: str
    message: str = ""

    def payload(self) -> dict[str, Any]:
        return {
            "required": self.required,
            "allowed": self.allowed,
            "reason": self.reason,
        }


def require_clean_build_requested(
    *,
    environ: Mapping[str, str] | None = None,
    flag: bool | None = None,
) -> bool:
    """True when this run must refuse an unidentifiable build.

    An explicit ``--require-clean-build`` wins over the environment; otherwise
    the harness-set variable decides. An unrecognised value means "not
    requested" rather than an error, so a typo cannot silently *enable* a
    refusal that then looks like a build problem.
    """
    if flag:
        return True
    source: Mapping[str, str] = os.environ if environ is None else environ
    raw = str(source.get(REQUIRE_CLEAN_BUILD_ENV) or "").strip().casefold()
    return raw in _TRUTHY


def decide_clean_build(
    *,
    info: BuildInfo | None = None,
    environ: Mapping[str, str] | None = None,
    flag: bool | None = None,
) -> CleanBuildDecision:
    """Decide whether a run may start, and say why not in words an operator can act on."""
    resolved = load_build_info() if info is None else info
    required = require_clean_build_requested(environ=environ, flag=flag)
    if not required:
        return CleanBuildDecision(required=False, allowed=True, reason="not_required")
    if not resolved.is_identifiable:
        return CleanBuildDecision(
            required=True,
            allowed=False,
            reason="missing_commit",
            message=(
                "This build cannot identify itself: no commit hash was recorded at "
                f"build time (source: {resolved.source or UNKNOWN}). "
                f"{REQUIRE_CLEAN_BUILD_ENV} is set, so the run is refused rather than "
                "producing results that cannot be attributed to a source tree. "
                "Rebuild after running scripts/generate_build_info.py, or unset "
                f"{REQUIRE_CLEAN_BUILD_ENV} to proceed with an unidentifiable build."
            ),
        )
    if resolved.dirty:
        return CleanBuildDecision(
            required=True,
            allowed=False,
            reason="dirty_tree",
            message=(
                f"This build was made from a dirty working tree at commit "
                f"{resolved.commit_short} (built {resolved.timestamp or UNKNOWN}). "
                f"{REQUIRE_CLEAN_BUILD_ENV} is set, so the run is refused: the commit "
                "alone does not describe the code that would run. Commit or stash the "
                f"changes and rebuild, or unset {REQUIRE_CLEAN_BUILD_ENV}."
            ),
        )
    return CleanBuildDecision(required=True, allowed=True, reason="clean")
