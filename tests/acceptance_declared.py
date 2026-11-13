"""Acceptance contracts whose path roles are declared, as the model declares them.

The host reads no role from the wording of a request. A test that needs a
required output, a preserved file or an input states the role the way the
model does: through the path declaration.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from alysis_code.agent.acceptance_contract import AcceptanceContract, build_acceptance_contract
from alysis_code.agent.task_artifact_binding import (
    bind_declared_requirements,
    bind_path_interpretation,
)


def declared_contract(
    *,
    root: Path,
    instruction: str,
    roles: dict[str, str],
    commands: list[str] | None = None,
    formats: dict[str, str] | None = None,
    only_declared_outputs: bool = False,
    keep_running: dict[str, Any] | None = None,
    thresholds: list[dict[str, Any]] | None = None,
    **options: Any,
) -> AcceptanceContract:
    """Build the contract for `instruction` as a declaration states it.

    `roles` maps each path exactly as the instruction writes it to a role the
    declaration accepts, such as "required_output". The other arguments are the
    declaration's fields of the same names; `formats` maps an output to its format.
    """
    baseline = build_acceptance_contract(root=root, instruction=instruction, **options)
    arguments: dict[str, Any] = {
        "paths": [
            {
                "name": name,
                "directory": "",
                "role": role,
                **({"format": formats[name]} if formats and name in formats else {}),
            }
            for name, role in roles.items()
        ]
    }
    if commands:
        arguments["commands"] = list(commands)
    if only_declared_outputs:
        arguments["only_declared_outputs"] = True
    if keep_running is not None:
        arguments["keep_running"] = dict(keep_running)
    if thresholds:
        arguments["thresholds"] = list(thresholds)
    entry_refs: list[Any] = []
    refs = bind_path_interpretation(
        root=root,
        instruction=instruction,
        arguments=arguments,
        prior_refs=baseline.path_refs,
        entry_refs=entry_refs,
    )
    declared = bind_declared_requirements(
        instruction=instruction, arguments=arguments, refs=refs, entry_refs=entry_refs
    )
    options.setdefault("workspace_snapshot", baseline.snapshot)
    return build_acceptance_contract(
        root=root,
        instruction=instruction,
        interpreted_path_refs=refs,
        declared=declared,
        **options,
    )
