"""Post-run mirror that makes session artifacts harvestable by the harness.

The defect this exists for
--------------------------
The harness-side adapter harvests a task's session artifacts from
``<workspace>/.alysis`` -- that is what ``--artifact /app/.alysis`` in
``run_harbor_tbench.sh`` collects. In normal mode nothing
ever creates that directory: the box adapter points ``session_log_dir`` at
``/logs/artifacts/alysis-session`` instead. The result is that two full
89-task trials retained **zero** session logs. Every question afterwards --
which stop reason fired, how many steps ran, whether redaction held -- had to
be answered from console text, or not at all.

Why a symlink, and why after the run
------------------------------------
The obvious fix, pointing ``session_log_dir`` at ``<workspace>/.alysis``,
writes agent state into the task workspace *while the task is being solved*.
Terminal-Bench tasks are graded on the workspace: some verifiers inspect tree
state, and agents routinely run ``git add -A``. A directory that materialises
mid-run can be committed into the solution or trip a cleanliness check, which
would corrupt the very scores the logs are meant to explain.

A symlink created *after* the agent exits has neither problem. Nothing exists
during the run, so no verifier and no ``git add`` can see it; by the time it
appears the workspace is already graded, and the harvester -- which runs later
still -- follows it to the real directory.

Exit-code discipline
--------------------
The mirror must run whether the agent succeeded or failed (a failed run's logs
are the ones worth having), and it must not become the command's exit status.
So the status is captured immediately after the agent's pipeline and re-raised
with an explicit ``exit`` once the mirror is done. The captured value is the
status of the existing pipeline exactly as before -- this module changes what
the command *does after* the run, never what it reports.

Stdlib-only and free of package imports, so ``tests/test_session_mirror.py``
can load it by file path in a bare interpreter.
"""

from __future__ import annotations

import shlex

# Host switch, default on. Off is for a campaign that has some other reason to
# keep the workspace pristine even after grading.
MIRROR_ENV_VAR = "ALYSIS_TBENCH_MIRROR_SESSION"
DEFAULT_MIRROR_ENABLED = "1"

# The name the harness-side adapter harvests from the task workspace.
WORKSPACE_MIRROR_NAME = ".alysis"

# Shell variable the agent's exit status is parked in. Prefixed to avoid
# colliding with anything a task's own environment may have set.
EXIT_STATUS_VAR = "__alysis_rc"

# Where the agent's true exit status is written by the tee'd run pipeline.
# Outside the workspace and outside /logs on purpose: a task's own tooling
# (`git add -A`, tree-state verifiers) must never see it, and the harvester
# must never mistake it for an artifact.
AGENT_EXIT_STATUS_FILE = "/tmp/alysis-agent-exit-status"

_DISABLED_VALUES = frozenset({"0", "false", "no", "off"})


def mirror_enabled(raw: str | None) -> bool:
    """Whether to append the mirror, defaulting to on when unset.

    Opt-out rather than opt-in: the failure this guards against is silent and
    only discovered afterwards, when the logs that would have explained a
    campaign turn out not to exist.
    """
    if raw is None:
        raw = DEFAULT_MIRROR_ENABLED
    text = str(raw).strip().lower()
    if not text:
        return True
    return text not in _DISABLED_VALUES


def session_mirror_clause(
    session_dir: str,
    *,
    link_name: str = WORKSPACE_MIRROR_NAME,
) -> str:
    """Shell that links ``<workspace>/<link_name>`` at ``session_dir``.

    ``-f`` replaces an existing link and ``-n`` stops a link that already
    points at a directory from being followed, which would otherwise nest the
    new link *inside* the old target. Failure is swallowed: a missing session
    directory means there was nothing to harvest anyway, and must not turn a
    finished run into a failed one.
    """
    # $PWD stays unquoted-by-shlex on purpose: it has to expand in the
    # container, where the working directory is the task workspace. Wrapping
    # it in double quotes keeps a space-bearing path safe.
    return f'ln -sfn {shlex.quote(session_dir)} "$PWD/{link_name}" 2>/dev/null || true'


def agent_pipeline_command(
    agent_command: str,
    *,
    tee_targets: tuple[str, ...],
    status_file: str = AGENT_EXIT_STATUS_FILE,
) -> str:
    """Tee the agent's output without letting tee's status replace the agent's.

    A POSIX pipeline reports the *last* command's status, so the plain
    ``agent | tee`` shape hands the harness tee's exit code -- 0 almost always
    -- and whether the agent's real code survives then depends entirely on the
    executor's shell choosing to enable ``pipefail``. One Harbor version doing
    so is why a full campaign's exit codes arrived intact; nothing pins the
    next one to keep doing it. This shape removes the dependence: the agent's
    status is written to a file from inside the subshell, read back after the
    pipeline, and re-raised.

    The fallback matters for the kill case: a SIGKILL that takes down the
    subshell before the status is written leaves the file absent, and the
    pipeline's own status is then the only truthful answer available. The
    final ``(exit ...)`` is a subshell so this command can be further composed
    -- ``compose_run_command`` appends the mirror after it and re-raises
    ``$?`` itself; a bare ``exit`` here would skip the mirror entirely.
    """
    if not tee_targets:
        raise ValueError("agent_pipeline_command needs at least one tee target")
    quoted_status = shlex.quote(status_file)
    tees = " ".join(shlex.quote(target) for target in tee_targets)
    return (
        f"rm -f {quoted_status}; "
        f"( {agent_command}; printf '%s' \"$?\" >{quoted_status} ) 2>&1 | tee {tees}; "
        f"{EXIT_STATUS_VAR}_pipe=$?; "
        f"{EXIT_STATUS_VAR}_agent=$(cat {quoted_status} 2>/dev/null); "
        f"rm -f {quoted_status}; "
        f'(exit "${{{EXIT_STATUS_VAR}_agent:-${EXIT_STATUS_VAR}_pipe}}")'
    )


def compose_run_command(
    run_command: str,
    *,
    mirror_clause: str | None,
    exit_status_var: str = EXIT_STATUS_VAR,
) -> str:
    """Append the mirror to ``run_command`` without masking its exit status.

    With no clause the command is returned untouched, so a disabled mirror is
    byte-identical to the behaviour that existed before this module.
    """
    if not mirror_clause:
        return run_command
    return f"{run_command}; {exit_status_var}=$?; {mirror_clause}; exit ${exit_status_var}"
