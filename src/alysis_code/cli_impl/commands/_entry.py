"""Startup checks shared by the full CLI and the fresh account-status entry."""

from __future__ import annotations

import sys
from typing import Any

import typer

from ...build_identity import decide_clean_build

#: A build that cannot identify itself is a configuration problem, not a task
#: failure, so it gets its own code rather than the generic 1.
UNIDENTIFIABLE_BUILD_EXIT_CODE = 2


def _cli_module() -> Any:
    module = sys.modules.get("alysis_code.cli")
    if module is not None:
        return module
    from ... import cli

    return cli


def _enforce_clean_build(flag: bool) -> None:
    """Refuse to start when the build cannot prove what it is.

    Benchmark harnesses set ``ALYSIS_REQUIRE_CLEAN_BUILD=1`` so a campaign
    cannot silently run against an unpinned or locally-modified build. One
    earlier campaign did exactly that -- it ran against "latest main" -- and
    none of its results can now be tied to a source tree.

    Deliberately after ``--version``, which is eager: diagnosing a refusal
    starts by asking the binary what it thinks it is, and that has to keep
    working.
    """
    decision = decide_clean_build(flag=flag)
    if decision.allowed:
        return
    typer.echo(f"alysis: {decision.message}", err=True)
    raise typer.Exit(code=UNIDENTIFIABLE_BUILD_EXIT_CODE)
