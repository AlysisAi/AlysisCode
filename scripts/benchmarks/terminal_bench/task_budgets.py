"""Historical offline budget analysis; never imported by production adapters.

Managed launchers now require host deadline metadata and use the shared deadline
resolver. This archived table and its generator remain for inspecting old runs;
task names must never determine production behavior or supply deadline authority.

Why this table exists
---------------------
The adapter used to export one flat ``ALYSIS_RUN_BUDGET_SECONDS=10800`` for
every task. Harbor's allowance is *per task*, not flat, and for most of the
suite it is far below three hours. The consequence was not a slow run but a
lost one: the agent never reached its own budget, so it never stopped itself
cleanly, and Harbor killed it mid-flight instead. Across two 89-task trials
(``20260823T081401Z`` and ``20260824T092151Z``) that produced 7 and then 14
``AgentTimeoutError`` kills, roughly 60% wasted compute, and four tasks that
had been solved in every earlier campaign. A kill also truncates the run
wherever it happens, which sometimes discarded a verification step that had
already been earned.

A budget the agent owns is strictly better than a kill it does not: reaching
it triggers the clean-stop path (salvage what is on disk, write an honest
summary, exit 0) instead of a hard SIGKILL at an arbitrary instruction.

Provenance of the numbers
-------------------------
Each entry is ``0.85 x the observed per-task limit`` for the 16 tasks whose
kill times were recorded in those two trials. Kill times clustered tightly on
exact minute boundaries, which is what identifies them as the harness limit
rather than incidental slowness. Where a task was observed killed anywhere in
a range across the two trials (e.g. 62-63 min), the **lower** bound is used --
the budget has to fit under the earliest kill actually seen, not the latest.

The 0.85 factor is headroom for the clean-stop path itself: salvage, final
summary, and session flush all happen *after* the budget trips, and they must
complete inside the harness window or the exercise is pointless.

These values are transcribed from run artifacts, not read from the dataset.
``scripts/generate_tb_task_budgets.py`` regenerates this file with exact
values from a local terminal-bench-2-1 checkout, on a box where the dataset
is present; it reads each task's configured agent time limit rather than
inferring it from when a kill happened to land. Prefer that over hand-editing.

This module is stdlib-only so the offline generator and
``tests/test_task_budgets.py`` can load it in a bare interpreter.
"""

from __future__ import annotations

import re
from pathlib import Path

# Per-task Harbor limits, in minutes. Kept next to the derived table so the
# arithmetic can be rechecked without digging the trials out again.
#
# Two provenance grades. The 22 tasks marked "exact" are the configured limit
# Harbor itself reported when it killed the agent -- the
# ``AgentTimeoutError: Agent execution timed out after N seconds`` message in
# ``result.json`` -- across the three focus-64 trials of campaign
# 20260831T110121Z. Every one is a round multiple of Harbor's 4x agent
# multiplier (750/900/1200/1800/2400 s bases), which is what the generator
# would read from the dataset. The 3 marked "wall-clock" were never killed in
# that campaign and keep the earlier estimate from trials 20260823T081401Z /
# 20260824T092151Z, which was the observed kill *wall time* and so includes
# 1-4 min of container setup; that over-estimate is on the safe side (budget
# stays under the true limit) but leaves less clean-stop headroom than the
# fraction intends. ``scripts/generate_tb_task_budgets.py`` against a local
# dataset checkout replaces all of this with exact values for every task.
#
#   exact:
#   overfull-hbox                  50        code-from-image               80
#   adaptive-rejection-sampler     60        extract-moves-from-video     120
#   chess-best-move                60        make-mips-interpreter        120
#   db-wal-recovery                60        mcmc-sampling-stan           120
#   fix-code-vulnerability         60        mteb-retrieve                120
#   gcode-to-text                  60        path-tracing                 120
#   gpt2-codegolf                  60        path-tracing-reverse         120
#   make-doom-for-mips             60        schemelike-metacircular-eval 160
#   password-recovery              60
#   qemu-alpine-ssh                60
#   qemu-startup                   60
#   raman-fitting                  60
#   tune-mjcf                      60
#   write-compressor               60
#
#   wall-clock (unobserved in 20260831T110121Z; regenerate to make exact):
#   custom-memory-heap-crash      122        mailman                      123
#   dna-assembly                  122
#
# The earlier wall-clock estimates for the tasks above that are now exact were
# 3-8% high (e.g. overfull-hbox 55 min observed vs 50 min configured), which
# left overfull-hbox only 195 s of headroom for salvage + summary + flush.
#
# Fraction applied to the observed limit to leave room for the clean stop.
BUDGET_FRACTION_OF_OBSERVED_LIMIT = 0.85

# task name -> wall-clock seconds. Sorted by name; regenerate with
# scripts/generate_tb_task_budgets.py rather than editing by hand.
TASK_BUDGET_SECONDS: dict[str, int] = {
    "adaptive-rejection-sampler": 3060,
    "chess-best-move": 3060,
    "code-from-image": 4080,
    "custom-memory-heap-crash": 6222,
    "db-wal-recovery": 3060,
    "dna-assembly": 6222,
    "extract-moves-from-video": 6120,
    "fix-code-vulnerability": 3060,
    "gcode-to-text": 3060,
    "gpt2-codegolf": 3060,
    "mailman": 6273,
    "make-doom-for-mips": 3060,
    "make-mips-interpreter": 6120,
    "mcmc-sampling-stan": 6120,
    "mteb-retrieve": 6120,
    "overfull-hbox": 2550,
    "password-recovery": 3060,
    "path-tracing": 6120,
    "path-tracing-reverse": 6120,
    "qemu-alpine-ssh": 3060,
    "qemu-startup": 3060,
    "raman-fitting": 3060,
    "schemelike-metacircular-eval": 8160,
    "tune-mjcf": 3060,
    "write-compressor": 3060,
}


def normalize_task_name(task_name: str | None) -> str:
    """Reduce a Harbor task identifier to the bare task slug.

    Harbor reports a task as ``"<dataset>/<task>"`` in ``config.json`` and
    ``result.json`` (e.g. ``"terminal-bench/overfull-hbox"``), while a trial
    directory carries the bare slug. Accepting both means a caller does not
    have to know which one it is holding.
    """
    text = str(task_name or "").strip()
    if not text:
        return ""
    # Only the final segment is the task; a dataset or org prefix is not.
    return text.rsplit("/", 1)[-1].strip()


# Harbor names each trial directory ``<task-name>__<7-char-random-token>``.
# The token is random per trial, not an attempt index -- the same task appears
# under different tokens across jobs -- so it carries no information worth
# keeping, but its fixed shape is what makes the split unambiguous. Anchoring
# on the *last* ``__`` matters: task names may themselves contain ``__``
# (``astropy__astropy-13236__9ywoXbN``), so splitting on the first one is wrong.
_TRIAL_DIR_RE = re.compile(r"^(?P<task>.+)__[A-Za-z0-9]{7}$")


def task_name_from_trial_dir(dir_name: str | None) -> str:
    """Task slug from one Harbor trial directory name, or ``""``."""
    match = _TRIAL_DIR_RE.match(str(dir_name or "").strip())
    if match is None:
        return ""
    return match.group("task")


def task_name_from_logs_dir(logs_dir: str | Path | None) -> str:
    """Recover the task slug from the adapter's ``logs_dir``, or ``""``.

    Harbor hands an installed agent a per-trial ``logs_dir`` inside the trial
    directory, and that directory's name is the only task identity the adapter
    is given -- nothing on ``BaseInstalledAgent`` or ``AgentContext`` carries a
    task name, and the instruction text is the task *prompt*, not its id.

    The directory itself and each of its ancestors are checked, because which
    level ``logs_dir`` is bound to (the trial dir, or a subdirectory of it such
    as ``agent/``) is a Harbor detail this repo cannot pin down. Walking
    upwards is correct for either.

    Returns ``""`` rather than raising when nothing matches: an unrecognised
    path must degrade to the flat default, never break the run.
    """
    if logs_dir is None:
        return ""
    try:
        path = Path(logs_dir).resolve()
    except (OSError, ValueError, TypeError):
        try:
            path = Path(str(logs_dir))
        except (ValueError, TypeError):
            return ""
    for candidate in (path, *path.parents):
        name = task_name_from_trial_dir(candidate.name)
        if name:
            return name
    return ""


def budget_lookup(task_name: str, default: int) -> tuple[int, bool]:
    """``(seconds, found_in_table)`` for ``task_name``.

    The flag is the point. A miss and a hit produce the same kind of number, so
    an artifact showing only the number cannot say whether the table was
    consulted at all -- and a run that silently fell back to the flat deadline
    behaves exactly like one from before the table existed. The adapter records
    the flag per task so that fallback is visible while the campaign is running
    rather than inferred from behaviour afterwards.
    """
    budget = TASK_BUDGET_SECONDS.get(normalize_task_name(task_name))
    if budget is None:
        return int(default), False
    return int(budget), True


def budget_for_task(task_name: str, default: int) -> int:
    """Run budget in seconds for ``task_name``, or ``default`` if unlisted.

    Unlisted is the common case and is not an error: the table covers only
    tasks whose limit was actually observed. Everything else keeps the
    adapter's flat default, which is the behaviour that existed before this
    table did.
    """
    return budget_lookup(task_name, default)[0]
