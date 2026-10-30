from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import typer

from ...branding import env_get
from ...config import ConfigError, clone_cfg, load_config
from ...forge import (
    ForgeError,
    attach_asset,
    current_run_pointer_path,
    find_task,
    load_current_run_paths,
    load_plan,
    requirement_text,
)
from ...forge_events import (
    EVENT_PLAN_INVALID,
    EVENT_PLAN_SAVED,
    EVENT_REVIEW_RESULT,
    EVENT_RUN_STARTED,
    EXIT_ERROR,
    ForgeEventEmitter,
    machine_session,
)
from ...plan_repair import (
    PLAN_STATUS_DRAFT,
    plan_repair_event_payload,
    plan_repair_metadata,
    plan_status,
    plan_status_detail,
)
from ...profiles import apply_runtime_base_url_override
from ...review_gate import ReviewError, review_task
from ..assets_cli import assets_app as forge_assets_app
from . import _patchable
from ._shared import Mode, _console, _Table
from .forge_asset_view import forge_asset_view_count, forge_asset_view_entries


def _cli_module() -> Any:
    module = sys.modules.get("alysis_code.cli")
    if module is not None:
        return module
    from ... import cli

    return cli


def _resolve_cli_host_pytest(candidate: Path | None) -> Path:
    """Resolve an explicit or engine-local pytest identity for Host Phase 0."""

    from ...forge_host_runtime import resolve_local_trusted_pytest

    selected = candidate
    if selected is None and not str(env_get("ALYSIS_FORGE_TRUSTED_PYTEST") or "").strip():
        selected = Path(sys.executable).with_name("pytest.exe" if os.name == "nt" else "pytest")
    return resolve_local_trusted_pytest(selected)


forge_app = typer.Typer(
    add_completion=False,
    help=(
        "Forge commands. `forge run` executes a plan sequentially in this checkout and is the "
        "default path; `forge swarm` is the parallel optimization for independent tasks."
    ),
)
forge_app.add_typer(forge_assets_app, name="assets")

_MACHINE_CONTEXT_KEY = "alysis_forge_machine"

MACHINE_OPTION_HELP = (
    "Emit newline-delimited JSON events on stdout instead of human output. "
    "One JSON object per line; exactly one terminal event (run_completed or error)."
)


def _store_machine_flag(ctx: typer.Context | None, value: bool) -> None:
    if ctx is None or value is not True:
        return
    if not isinstance(getattr(ctx, "obj", None), dict):
        ctx.obj = {}
    ctx.obj[_MACHINE_CONTEXT_KEY] = True


def _machine_enabled(ctx: typer.Context | None, machine: bool) -> bool:
    """True when either `forge --machine <cmd>` or `forge <cmd> --machine` was used.

    Only a real ``True`` counts. These commands are also called directly as Python
    functions (for example from the interactive quick-action menu), where unfilled
    parameters arrive as truthy typer ``OptionInfo`` objects rather than booleans --
    that must not silently switch a human invocation into the machine protocol.
    """
    if machine is True:
        return True
    current: Any = ctx
    while current is not None:
        obj = getattr(current, "obj", None)
        if isinstance(obj, dict) and obj.get(_MACHINE_CONTEXT_KEY):
            return True
        current = getattr(current, "parent", None)
    return False


def _forge_error_exit(
    console: Any,
    events: ForgeEventEmitter,
    error: Exception,
    *,
    code: int = EXIT_ERROR,
    kind: str = "forge_error",
    data: dict[str, Any] | None = None,
) -> typer.Exit:
    """Report a Forge error on both surfaces and build the matching Exit."""
    console.print(f"[red]Forge error:[/red] {error}")
    events.error(message=str(error), kind=kind, exit_code=code, data=data)
    return typer.Exit(code=code)


def _run_lifecycle_snapshot(paths: Any) -> dict[str, Any]:
    """The run's lifecycle block, so consumers never infer "active" from its absence."""
    from ...forge import read_current_run_pointer
    from ...run_state import describe_run_status, pointer_status, status_is_resumable

    pointer = read_current_run_pointer(paths.root) or {}
    status = pointer_status(pointer)
    return {
        "run_status": status,
        "run_status_description": describe_run_status(status),
        "run_status_updated_at": pointer.get("status_updated_at"),
        "run_status_reason": pointer.get("status_reason"),
        "run_owner": pointer.get("run_owner"),
        "resumable": status_is_resumable(status),
    }


def _plan_snapshot(paths: Any, plan: dict[str, Any]) -> dict[str, Any]:
    tasks = plan.get("tasks") or []
    return {
        **_run_lifecycle_snapshot(paths),
        "run_id": paths.run_id,
        "run_dir": os.fspath(paths.run_dir),
        "plan_json": os.fspath(paths.plan_json_path),
        "plan_md": os.fspath(paths.plan_md_path),
        "project_goal": str(plan.get("project_goal") or "").strip() or None,
        "task_count": len(tasks),
        "tasks": [
            {
                "id": str(task.get("id", "")),
                "status": str(task.get("status", "")),
                "title": str(task.get("title", "")),
                "dependencies": [str(dep) for dep in (task.get("dependencies") or [])],
            }
            for task in tasks
        ],
        **plan_repair_event_payload(plan),
    }


@forge_app.callback()
def forge_main(
    ctx: typer.Context,
    machine: bool = typer.Option(False, "--machine", help=MACHINE_OPTION_HELP),
) -> None:
    """Forge commands. Human output is the default; --machine switches to NDJSON."""
    _store_machine_flag(ctx, machine)


@forge_app.command("_host-stdio", hidden=True)
def forge_host_stdio(
    path: Path = typer.Option(
        Path("."),
        "--path",
        help="Workspace path or repository subdirectory.",
    ),
    create_path: bool = typer.Option(
        False,
        "--create-path",
        help="Create --path before binding the Host generation.",
    ),
    allow_broad_workspace: bool = typer.Option(
        False,
        "--allow-broad-workspace",
        help="Allow a guarded broad workspace for this Host generation.",
    ),
    trusted_pytest: Path | None = typer.Option(
        None,
        "--trusted-pytest",
        help="Exact engine-trusted pytest executable (normally auto-resolved).",
    ),
    model: str | None = typer.Option(
        None,
        "--model",
        help="Model bound to this exact Host generation.",
    ),
    base_url: str | None = typer.Option(
        None,
        "--base-url",
        help="Provider endpoint bound to this exact Host generation.",
    ),
    planning_profile: str = typer.Option(
        "quick",
        "--planning-profile",
        help="Canonical planner profile: quick or deep.",
    ),
) -> None:
    """Serve one canonical local Forge Host-v2 generation over stdin/stdout."""

    from ... import __version__
    from ...forge_host_protocol.models import Peer
    from ...forge_host_runtime import build_local_forge_host_runtime
    from ...forge_host_stdio import ForgeHostStdioFatalError
    from ...forge_planning_profile import normalize_planning_profile

    class _SilentHostConsole:
        def print(self, *_args: object, **_kwargs: object) -> None:
            return

        def rule(self, *_args: object, **_kwargs: object) -> None:
            return

    def _generation_config():
        config = clone_cfg(load_config())
        if model is not None:
            config.model = model
        if base_url is not None:
            config.base_url = base_url
        return config

    try:
        runtime = build_local_forge_host_runtime(
            root=path,
            cli_module=_cli_module(),
            console=_SilentHostConsole(),
            trusted_pytest=_resolve_cli_host_pytest(trusted_pytest),
            config_loader=_generation_config,
            create_if_missing=create_path,
            allow_broad_workspace=allow_broad_workspace,
            planning_profile=normalize_planning_profile(planning_profile),
        )
        runtime.stdio_server(
            input_stream=sys.stdin.buffer,
            output_stream=sys.stdout.buffer,
            server=Peer(name="AlysisEngine", version=__version__, host_kind="engine"),
        ).serve()
    except ForgeHostStdioFatalError:
        typer.echo("Forge Host transport failed.", err=True)
        raise typer.Exit(code=1) from None
    except Exception:
        typer.echo("Forge Host startup failed.", err=True)
        raise typer.Exit(code=1) from None


_WORKSPACE_CHECK_ACTIONS = {
    "forge": "forge",
    "forge_plan": "forge_plan",
    "swarm": "swarm",
}
_COMPLETED_VERIFICATION_TASK_STATUSES = {
    "complete",
    "completed",
    "done",
    "merged",
    "skipped",
}
_FORGE_PREFLIGHT_SCHEMA_VERSION = 1
_FORGE_PERMISSION_MODES = frozenset({"read-only", "workspace-write", "full-access"})
_FORGE_PREFLIGHT_ACTIONS = frozenset({"plan", "execute"})
_FORGE_TERMINAL_TASK_STATUSES = frozenset(
    {
        "already_satisfied",
        "complete",
        "completed",
        "completed_unverified",
        "done",
        "invalidated",
        "merged",
        "skipped",
        "superseded",
    }
)


def _stable_forge_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load_desktop_forge_contract(path: Path):  # type: ignore[no-untyped-def]
    from ...verification_contract import (
        ForgeVerificationContractError,
        ForgeVerificationContractV2,
    )

    try:
        if path.is_symlink() or not path.is_file():
            raise ForgeVerificationContractError(
                "verification contract is not a regular non-symlink file"
            )
        size = path.stat().st_size
        if size <= 0 or size > 2_000_000:
            raise ForgeVerificationContractError("verification contract has an invalid size")
        raw = path.read_bytes()
        contract = ForgeVerificationContractV2.from_json(raw)
        decoded = json.loads(raw)
        if not isinstance(decoded, dict) or not decoded.get("contract_fingerprint"):
            raise ForgeVerificationContractError(
                "verification contract must include its canonical contract_fingerprint"
            )
        return contract
    except OSError as error:
        raise ForgeVerificationContractError(
            f"unable to read verification contract: {error}"
        ) from error


def _desktop_verification_contract_error(
    contract: Any,
    *,
    plan: dict[str, Any] | None,
    task_required: bool,
    integration_required: bool,
) -> str | None:
    from ...swarm_scheduler import canonical_task_status
    from ...verification_contract import (
        VerificationCommandEvidentiaryCapability,
        VerificationCommandRequirement,
        VerificationCommandTrustLevel,
        VerificationCommandValidationStatus,
        build_verification_command_spec,
    )

    def command_error(
        label: str,
        commands: tuple[Any, ...],
        *,
        required: bool,
    ) -> str | None:
        if required and not commands:
            return f"Strict verification has no command for {label}."
        for command in commands:
            if command.validation_status != VerificationCommandValidationStatus.VALID:
                return f"Verification rejected unsafe command {command.command_id}."
            source = (
                "environment.authoritative_verification_commands"
                if str(command.provenance) == "HOST_AUTHORITATIVE"
                else "cli.verify_cmd"
            )
            rebuilt = build_verification_command_spec(
                command.original_text,
                source=source,
                contract_type=(
                    "authoritative_override"
                    if source == "environment.authoritative_verification_commands"
                    else "explicit_override"
                ),
                acceptance_criterion_ids=tuple(command.acceptance_criterion_ids),
            )
            if (
                rebuilt.validation_status != VerificationCommandValidationStatus.VALID
                or tuple(rebuilt.argv) != tuple(command.argv)
                or command.working_directory != "."
            ):
                return f"Verification rejected malformed command {command.command_id}."
            if required and (
                command.trust_level != VerificationCommandTrustLevel.TRUSTED
                or command.requirement != VerificationCommandRequirement.REQUIRED
                or command.evidentiary_capability
                != VerificationCommandEvidentiaryCapability.ASSERTIVE
            ):
                return f"Strict verification rejected command {command.command_id}."
        return None

    if plan is not None:
        plan_task_ids = {
            str(task.get("id") or "").strip()
            for task in (plan.get("tasks") or [])
            if isinstance(task, dict) and str(task.get("id") or "").strip()
        }
        executable_task_ids = {
            str(task.get("id") or "").strip()
            for task in (plan.get("tasks") or [])
            if isinstance(task, dict)
            and task.get("analysis_only") is not True
            and canonical_task_status(str(task.get("status") or "planned"))
            not in _FORGE_TERMINAL_TASK_STATUSES
            and str(task.get("id") or "").strip()
        }
        contract_task_ids = {str(task.task_id) for task in contract.tasks}
        unknown = sorted(contract_task_ids - plan_task_ids)
        if unknown:
            return "Verification contract contains unknown task IDs: " + ", ".join(unknown)
        if task_required:
            missing = sorted(executable_task_ids - contract_task_ids)
            if missing:
                return "Strict verification is missing task IDs: " + ", ".join(missing)
    for task in contract.tasks:
        if task.cwd != ".":
            return f"Verification rejected malformed working directory for {task.task_id}."
        error = command_error(str(task.task_id), task.commands, required=task_required)
        if error:
            return error
    if contract.integration is not None:
        if contract.integration.cwd != ".":
            return "Verification rejected malformed working directory for integration."
        error = command_error(
            "integration",
            contract.integration.commands,
            required=integration_required,
        )
        if error:
            return error
    elif integration_required:
        return "Strict integration verification has no contract."
    return None


def _forge_execution_policy_payload(
    *,
    permission_mode: str,
    verify_mode: str,
    integration_mode: str,
    network: bool,
    strict_sandbox: bool,
    action: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    sandboxed = permission_mode != "full-access" or strict_sandbox
    isolation = "sandbox_required" if sandboxed else "host"
    verification_requirement = (
        "required"
        if "strict" in {verify_mode, integration_mode}
        else "off"
        if verify_mode == integration_mode == "off"
        else "advisory"
    )
    surface_names = (
        ["planner"]
        if action == "plan"
        else ["worker", "verifier", "integration", "review", "merge"]
    )
    surfaces: list[dict[str, Any]] = []
    for surface in surface_names:
        filesystem_access = permission_mode
        if sandboxed and surface in {"verifier", "integration", "review"}:
            filesystem_access = "read-only"
        if sandboxed and permission_mode == "full-access":
            filesystem_access = "workspace-write"
        surfaces.append(
            {
                "surface": surface,
                "ok": True,
                "backend": "sandbox" if sandboxed else "host",
                "isolation": isolation,
                "filesystem_access": filesystem_access,
                "network": bool(network),
                "network_enforced": sandboxed,
                "reason_codes": [],
                "checks": [],
            }
        )
    policy = {
        "permission": permission_mode,
        "verification_requirement": verification_requirement,
        "strict_sandbox": bool(strict_sandbox and permission_mode == "full-access"),
        "network": bool(network),
        "action": action,
        "surfaces": [
            {
                "surface": surface["surface"],
                "isolation": surface["isolation"],
                "filesystem_access": surface["filesystem_access"],
                "network": surface["network"],
            }
            for surface in surfaces
        ],
    }
    return policy, surfaces


def _forge_preflight_payload(
    *,
    path: Path,
    permission_mode: str,
    action: str,
    verify: str,
    integration_verify: str,
    verification_contract: Path | None,
    requested_plan_fingerprint: str | None,
    network: bool,
    strict_sandbox: bool,
) -> dict[str, Any]:
    from ...verification_contract import (
        ForgeVerificationContractError,
        approved_plan_fingerprint,
    )
    from ...verify_gate import normalize_verify_mode
    from ...workspace_binding import (
        WorkspaceAction,
        WorkspaceBindingError,
        WorkspaceRiskLevel,
        resolve_workspace_binding,
        workspace_policy_violation_message,
    )

    normalized_action = str(action or "").strip().lower()
    if normalized_action not in _FORGE_PREFLIGHT_ACTIONS:
        raise ValueError("action must be plan or execute")
    normalized_permission = str(permission_mode or "").strip().lower()
    if normalized_permission not in _FORGE_PERMISSION_MODES:
        raise ValueError("permission mode must be read-only, workspace-write, or full-access")
    if normalized_permission == "read-only":
        raise ValueError(f"Forge {normalized_action} requires workspace-write or full-access")
    verify_mode = normalize_verify_mode(verify)
    integration_mode = normalize_verify_mode(integration_verify)
    binding = resolve_workspace_binding(path, source="desktop_preflight")
    if binding.risk_level != WorkspaceRiskLevel.HEALTHY:
        raise WorkspaceBindingError(
            workspace_policy_violation_message(binding, action=WorkspaceAction.FORGE)
        )
    root = binding.workspace_context.workspace_root.resolve()

    contract = None
    plan_fingerprint: str | None = None
    if verification_contract is not None:
        contract = _load_desktop_forge_contract(verification_contract)
        if Path(contract.workspace_root).expanduser().resolve() != root:
            raise ForgeVerificationContractError(
                "verification contract workspace does not match --path"
            )
    if normalized_action == "execute":
        if contract is None:
            raise ForgeVerificationContractError("Forge execution requires --verification-contract")
        requested_fingerprint = str(requested_plan_fingerprint or "").strip()
        if not requested_fingerprint:
            raise ForgeVerificationContractError(
                "Forge execution requires --approved-plan-fingerprint"
            )
        if requested_fingerprint != contract.approved_plan_fingerprint:
            raise ForgeVerificationContractError(
                "verification contract does not match the approved plan fingerprint"
            )
        plan: dict[str, Any] | None = None
        if current_run_pointer_path(root).exists():
            paths = load_current_run_paths(root)
            plan = load_plan(paths)
            contract.validate_approved_plan(plan)
            plan_fingerprint = approved_plan_fingerprint(plan)
        else:
            plan_fingerprint = requested_fingerprint
        contract_error = _desktop_verification_contract_error(
            contract,
            plan=plan,
            task_required=verify_mode == "strict",
            integration_required=integration_mode == "strict",
        )
        if contract_error:
            raise ForgeVerificationContractError(contract_error)
    elif requested_plan_fingerprint:
        if contract is None or requested_plan_fingerprint != contract.approved_plan_fingerprint:
            raise ForgeVerificationContractError(
                "approved plan fingerprint requires a matching verification contract"
            )
        plan_fingerprint = requested_plan_fingerprint

    policy, surfaces = _forge_execution_policy_payload(
        permission_mode=normalized_permission,
        verify_mode=verify_mode,
        integration_mode=integration_mode,
        network=network,
        strict_sandbox=strict_sandbox,
        action=normalized_action,
    )
    fingerprint_input = {
        "schema_version": _FORGE_PREFLIGHT_SCHEMA_VERSION,
        "workspace_root": os.fspath(root),
        "contract_fingerprint": contract.contract_fingerprint if contract else None,
        "approved_plan_fingerprint": plan_fingerprint,
        "verify_mode": verify_mode,
        "integration_verify_mode": integration_mode,
        "policy": policy,
    }
    fingerprint = hashlib.sha256(_stable_forge_json(fingerprint_input).encode("utf-8")).hexdigest()
    return {
        "schema_version": _FORGE_PREFLIGHT_SCHEMA_VERSION,
        "ok": True,
        "permission_mode": normalized_permission,
        "reason_code": None,
        "message": "Forge preflight passed.",
        "action": None,
        "preflight_fingerprint": fingerprint,
        "contract_fingerprint": contract.contract_fingerprint if contract else None,
        "approved_plan_fingerprint": plan_fingerprint,
        "workspace_root": os.fspath(root),
        "verify_mode": verify_mode,
        "integration_verify_mode": integration_mode,
        "policy": policy,
        "surfaces": surfaces,
    }


_APP_FORGE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_APP_FORGE_GENERATION_ID = re.compile(r"^gen_[A-Za-z0-9][A-Za-z0-9_-]{7,127}$")


def _load_app_bound_forge_contract(
    *,
    path: Path,
    verification_contract: Path | None,
    run_id: str | None,
    generation_id: str | None,
    verify_mode: str,
    integration_verify_mode: str,
) -> tuple[Any, dict[str, Any], Any | None, str | None]:
    """Bind an App materialized contract to the exact current native run and generation."""

    from ...verification_contract import ForgeVerificationContractError
    from ...verify_gate import normalize_verify_mode

    paths = load_current_run_paths(path)
    plan = load_plan(paths)
    requested_run_id = str(run_id or "").strip()
    requested_generation_id = str(generation_id or "").strip()
    if requested_run_id and (
        not _APP_FORGE_ID.fullmatch(requested_run_id) or requested_run_id != paths.run_id
    ):
        raise ForgeError("The App-bound Forge run ID does not match the current run.")
    if requested_generation_id and not _APP_FORGE_GENERATION_ID.fullmatch(requested_generation_id):
        raise ForgeError("The App-bound Forge generation ID is invalid.")
    if verification_contract is None:
        if requested_run_id or requested_generation_id:
            raise ForgeError("Forge run/generation binding requires --verification-contract.")
        return paths, plan, None, None
    if not requested_run_id or not requested_generation_id:
        raise ForgeError(
            "App-bound Forge execution requires exact --run-id and --generation-id values."
        )
    try:
        contract = _load_desktop_forge_contract(verification_contract)
        if Path(contract.workspace_root).expanduser().resolve() != paths.root.resolve():
            raise ForgeVerificationContractError(
                "verification contract workspace does not match the bound Forge run"
            )
        contract.validate_approved_plan(plan)
    except ForgeVerificationContractError as error:
        raise ForgeError(f"Invalid App-bound verification contract: {error}") from error
    contract_error = _desktop_verification_contract_error(
        contract,
        plan=plan,
        task_required=normalize_verify_mode(verify_mode) == "strict",
        integration_required=normalize_verify_mode(integration_verify_mode) == "strict",
    )
    if contract_error:
        raise ForgeError(contract_error)
    return paths, plan, contract, requested_generation_id


def _load_app_task_bound_forge_contract(
    *,
    path: Path,
    verification_contract: Path | None,
    task_id: str,
    require_task_commands: bool,
) -> tuple[Any, dict[str, Any], Any | None, Any | None]:
    """Bind legacy one-task adapters to one exact App v2 task contract."""

    from ...verification_contract import ForgeVerificationContractError

    paths = load_current_run_paths(path)
    plan = load_plan(paths)
    if verification_contract is None:
        return paths, plan, None, None
    try:
        contract = _load_desktop_forge_contract(verification_contract)
        if Path(contract.workspace_root).expanduser().resolve() != paths.root.resolve():
            raise ForgeVerificationContractError(
                "verification contract workspace does not match the bound Forge run"
            )
        contract.validate_approved_plan(plan)
    except ForgeVerificationContractError as error:
        raise ForgeError(f"Invalid App-bound verification contract: {error}") from error
    contract_error = _desktop_verification_contract_error(
        contract,
        plan=plan,
        task_required=False,
        integration_required=False,
    )
    if contract_error:
        raise ForgeError(contract_error)
    task_contract = next(
        (item for item in contract.tasks if str(item.task_id) == task_id),
        None,
    )
    if task_contract is None:
        raise ForgeError(f"App verification authority is missing task {task_id}.")
    if require_task_commands and not task_contract.commands:
        raise ForgeError(f"App verification authority for {task_id} has no required command.")
    return paths, plan, contract, task_contract


def _artifact_safe_task_id(task_id: str) -> str:
    """Keep ordinary task IDs readable; never let one steer the artifact path."""

    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", task_id):
        return task_id
    return "sha256-" + hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:16]


def _run_app_contract_verification(
    *,
    contract: Any,
    paths: Any,
    run_timeout_ms: int,
    include_integration: bool,
) -> dict[str, Any]:
    """Run the exact App contract after Host-v2 has integrated the workspace."""

    from ...forge_contract_verification import run_forge_contract_verification

    timeout_s = max(1.0, min(900.0, float(run_timeout_ms) / 1000.0))
    task_results: list[dict[str, Any]] = []
    task_all_passed = True
    for task_contract in contract.tasks:
        task_id = str(task_contract.task_id)
        result = run_forge_contract_verification(
            root=paths.root,
            contract=task_contract,
            artifact_path=(
                paths.execution_integration_dir
                / "app-contract"
                / f"task-{_artifact_safe_task_id(task_id)}-verification.json"
            ),
            cfg=load_config(),
            timeout_s=timeout_s,
        )
        payload = result.to_payload(root=paths.root)
        task_results.append(payload)
        task_all_passed = task_all_passed and result.all_passed

    integration_payload: dict[str, Any] | None = None
    integration_all_passed = True
    if include_integration and contract.integration is not None:
        integration_result = run_forge_contract_verification(
            root=paths.root,
            contract=contract.integration,
            artifact_path=(
                paths.execution_integration_dir / "app-contract" / "integration-verification.json"
            ),
            cfg=load_config(),
            timeout_s=timeout_s,
        )
        integration_payload = integration_result.to_payload(root=paths.root)
        integration_all_passed = integration_result.all_passed

    return {
        "contract_fingerprint": contract.contract_fingerprint,
        "all_passed": task_all_passed and integration_all_passed,
        "task_all_passed": task_all_passed,
        "integration_all_passed": integration_all_passed,
        "tasks": task_results,
        "integration": integration_payload,
    }


@forge_app.command("preflight")
def forge_preflight_check(
    path: Path = typer.Option(Path("."), "--path"),
    permission_mode: str = typer.Option("workspace-write", "--permission-mode"),
    action: str = typer.Option("execute", "--action"),
    verify: str = typer.Option("strict", "--verify"),
    integration_verify: str = typer.Option("off", "--integration-verify"),
    verification_contract: Path | None = typer.Option(None, "--verification-contract"),
    approved_plan_fingerprint_value: str | None = typer.Option(
        None,
        "--approved-plan-fingerprint",
    ),
    network: bool = typer.Option(False, "--network/--no-network"),
    strict_sandbox: bool = typer.Option(False, "--strict-sandbox"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Validate App-owned Forge admission without workers, mutation, or provider calls."""

    from ...verification_contract import ForgeVerificationContractError
    from ...verify_gate import VerifyError
    from ...workspace_binding import WorkspaceBindingError

    try:
        payload = _forge_preflight_payload(
            path=path,
            permission_mode=permission_mode,
            action=action,
            verify=verify,
            integration_verify=integration_verify,
            verification_contract=verification_contract,
            requested_plan_fingerprint=approved_plan_fingerprint_value,
            network=network,
            strict_sandbox=strict_sandbox,
        )
        exit_code = 0
    except (
        ValueError,
        ConfigError,
        ForgeError,
        VerifyError,
        WorkspaceBindingError,
        ForgeVerificationContractError,
    ) as error:
        payload = {
            "schema_version": _FORGE_PREFLIGHT_SCHEMA_VERSION,
            "ok": False,
            "permission_mode": str(permission_mode),
            "reason_code": "preflight_blocked",
            "message": str(error),
            "action": (
                "Review the workspace, permission, approved plan, and verification "
                "contract, then retry."
            ),
            "preflight_fingerprint": None,
            "contract_fingerprint": None,
            "approved_plan_fingerprint": None,
            "workspace_root": None,
            "verify_mode": str(verify),
            "integration_verify_mode": str(integration_verify),
            "policy": None,
            "surfaces": [],
        }
        exit_code = 2
    if json_output:
        typer.echo(_stable_forge_json(payload))
    else:
        typer.echo(payload["message"], err=not payload["ok"])
    if exit_code:
        raise typer.Exit(code=exit_code)


def _emit_forge_assessment(
    payload: dict[str, Any],
    *,
    json_output: bool,
    human_message: str,
) -> None:
    if json_output is True:
        typer.echo(json.dumps(payload, ensure_ascii=True, sort_keys=True))
        return
    _console().print(human_message)


@forge_app.command("workspace-check")
def forge_workspace_check(
    path: Path = typer.Option(
        Path("."),
        "--path",
        help="Workspace path or repository subdirectory to assess.",
    ),
    action: str = typer.Option(
        "forge_plan",
        "--action",
        help="Workspace policy to assess: forge_plan, swarm, or forge.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit the stable App workspace-assessment JSON object.",
    ),
) -> None:
    """Assess canonical workspace scope without starting Forge or requiring credentials."""

    from ...workspace_binding import (
        WorkspaceBindingError,
        WorkspaceRiskLevel,
        discover_workspace_candidates,
        resolve_workspace_binding,
        workspace_policy,
        workspace_policy_violation_message,
    )

    normalized_action = str(action or "").strip().lower()
    if normalized_action not in _WORKSPACE_CHECK_ACTIONS:
        raise typer.BadParameter(
            "must be one of: forge_plan, swarm, forge",
            param_hint="--action",
        )
    canonical_action = _WORKSPACE_CHECK_ACTIONS[normalized_action]
    requested_path = path.expanduser().resolve(strict=False)

    try:
        binding = resolve_workspace_binding(
            requested_path,
            create_if_missing=False,
            allow_broad_workspace=False,
            source="forge.workspace_check",
        )
    except WorkspaceBindingError as error:
        payload: dict[str, Any] = {
            "ok": False,
            "action": canonical_action,
            "risk_level": WorkspaceRiskLevel.BLOCKED,
            "requested_path": os.fspath(requested_path),
            "workspace_root": os.fspath(requested_path),
            "git_root": None,
            "risk_reasons": [str(error)],
            "can_override": False,
            "message": str(error),
            "candidates": [],
        }
        _emit_forge_assessment(
            payload,
            json_output=json_output,
            human_message=f"[red]Workspace blocked:[/red] {error}",
        )
        return

    policy = workspace_policy(canonical_action)
    healthy = binding.risk_level == WorkspaceRiskLevel.HEALTHY
    can_override = (
        binding.risk_level == WorkspaceRiskLevel.GUARDED and policy.allow_guarded_override
    )
    candidates = discover_workspace_candidates(binding.requested_path) if not healthy else ()
    message = (
        "" if healthy else workspace_policy_violation_message(binding, action=canonical_action)
    )
    payload = {
        "ok": healthy,
        "action": canonical_action,
        "risk_level": binding.risk_level,
        "requested_path": os.fspath(binding.requested_path),
        "workspace_root": os.fspath(binding.workspace_context.workspace_root),
        "git_root": (
            os.fspath(binding.workspace_context.git_root)
            if binding.workspace_context.git_root is not None
            else None
        ),
        "risk_reasons": list(binding.risk_reasons),
        "can_override": can_override,
        "message": message,
        "candidates": [
            {
                "path": os.fspath(candidate.path),
                "summary": candidate.summary,
                "project_signals": list(candidate.project_signals),
            }
            for candidate in candidates
        ],
    }
    risk_style = "green" if healthy else "yellow"
    _emit_forge_assessment(
        payload,
        json_output=json_output,
        human_message=(
            f"Workspace: {payload['workspace_root']}\n"
            f"Risk: [{risk_style}]{binding.risk_level}[/{risk_style}]"
            + (f"\n{message}" if message else "")
        ),
    )


def _empty_verification_assessment(
    *,
    verify_mode: str,
    integration_verify_mode: str,
    message: str,
    missing_task_ids: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "ok": False,
        "verify_mode": verify_mode,
        "integration_verify_mode": integration_verify_mode,
        "tasks": [],
        "missing_task_ids": list(missing_task_ids or []),
        "invalid_task_ids": [],
        "integration_commands": [],
        "integration_source": "unavailable",
        "integration_error": "",
        "suggested_commands": [],
        "message": message,
    }


def _dedupe_strings(values: list[str] | tuple[str, ...]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = str(raw or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _integration_verification_contract_type(source: str) -> str:
    normalized = str(source or "").strip()
    if normalized in {"cli.integration_verify_cmd", "cli.verify_cmd_fallback"}:
        return "explicit_override"
    if normalized in {
        "config.integration_verify_commands",
        "config.verify_commands_fallback",
        "repo_scan.likely_test_commands_fallback",
    }:
        return "repo_native"
    if not normalized or normalized.endswith("no_authoritative_commands_fallback"):
        return "unavailable"
    return "selected"


@forge_app.command("verification-check")
def forge_verification_check(
    path: Path = typer.Option(
        Path("."),
        "--path",
        help="Workspace path or repository subdirectory containing the current Forge plan.",
    ),
    verify: str = typer.Option(
        "strict",
        "--verify",
        help="Task verification policy: off, warn, or strict.",
    ),
    verify_cmd: list[str] | None = typer.Option(
        None,
        "--verify-cmd",
        help="Explicit task verification command (repeatable).",
    ),
    integration_verify: str = typer.Option(
        "off",
        "--integration-verify",
        help="Integration verification policy: off, warn, or strict.",
    ),
    integration_verify_cmd: list[str] | None = typer.Option(
        None,
        "--integration-verify-cmd",
        help="Explicit integration verification command (repeatable).",
    ),
    task_id: str | None = typer.Option(
        None,
        "--task",
        help="Assess only this exact plan task id.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit the stable App verification-assessment JSON object.",
    ),
) -> None:
    """Resolve and validate verification authority without executing commands or a provider."""

    from ...integration_gate import (
        IntegrationGateError,
        normalize_integration_verify_mode,
        resolve_integration_verify_commands,
    )
    from ...repo_scan import scan_workspace
    from ...verification_contract import (
        VerificationCommandValidationStatus,
        build_verification_command_specs,
    )
    from ...verify_gate import (
        VerifyError,
        normalize_verify_mode,
        resolve_authoritative_task_verify_command_selection,
        validation_errors_for_selection,
    )
    from ...workspace_binding import WorkspaceBindingError, resolve_workspace_binding

    try:
        verify_mode = normalize_verify_mode(verify)
        integration_mode = normalize_integration_verify_mode(integration_verify)
    except (VerifyError, IntegrationGateError) as error:
        raise typer.BadParameter(str(error)) from error

    requested_task_id = str(task_id or "").strip()
    try:
        binding = resolve_workspace_binding(
            path,
            create_if_missing=False,
            allow_broad_workspace=False,
            source="forge.verification_check",
        )
        paths = load_current_run_paths(binding.workspace_context.workspace_root)
        plan = load_plan(paths)
    except (WorkspaceBindingError, ForgeError) as error:
        payload = _empty_verification_assessment(
            verify_mode=verify_mode,
            integration_verify_mode=integration_mode,
            message=str(error),
            missing_task_ids=([requested_task_id] if requested_task_id else []),
        )
        _emit_forge_assessment(
            payload,
            json_output=json_output,
            human_message=f"[red]Verification unavailable:[/red] {error}",
        )
        return

    raw_tasks = plan.get("tasks") or []
    plan_tasks = [task for task in raw_tasks if isinstance(task, dict)]
    selected_tasks: list[dict[str, Any]]
    unknown_task = False
    if requested_task_id:
        selected = find_task(plan, requested_task_id)
        if selected is None:
            selected_tasks = []
            unknown_task = True
        else:
            selected_tasks = [selected]
    else:
        selected_tasks = [
            task
            for task in plan_tasks
            if str(task.get("status") or "planned").strip().lower()
            not in _COMPLETED_VERIFICATION_TASK_STATUSES
        ]

    try:
        cfg = load_config()
        repo_scan = scan_workspace(context=binding.workspace_context)
    except (ConfigError, OSError) as error:
        payload = _empty_verification_assessment(
            verify_mode=verify_mode,
            integration_verify_mode=integration_mode,
            message=f"Unable to resolve verification configuration: {error}",
            missing_task_ids=([requested_task_id] if requested_task_id else []),
        )
        _emit_forge_assessment(
            payload,
            json_output=json_output,
            human_message=f"[red]Verification unavailable:[/red] {payload['message']}",
        )
        return

    plan_requirements = _dedupe_strings(
        [requirement_text(item) for item in (plan.get("requirements") or [])]
    )
    task_payloads: list[dict[str, Any]] = []
    missing_task_ids: list[str] = []
    invalid_task_ids: list[str] = []

    for task in selected_tasks:
        current_task_id = str(task.get("id") or "").strip()
        status = str(task.get("status") or "planned").strip() or "planned"
        if verify_mode == "off":
            task_payloads.append(
                {
                    "id": current_task_id,
                    "status": status,
                    "commands": [],
                    "source": "session.verification_disabled",
                    "reason": "task verification is disabled for this assessment",
                    "validation_errors": [],
                }
            )
            continue
        try:
            selection = resolve_authoritative_task_verify_command_selection(
                cfg=cfg,
                verify_cmd=verify_cmd,
                task=task,
                root=paths.root,
                repo_scan=repo_scan,
                plan_requirements=plan_requirements,
                allow_empty_config=True,
            )
            validation_errors = validation_errors_for_selection(selection)
            commands = list(selection.commands)
            source = selection.source
            reason = selection.reason
        except VerifyError as error:
            commands = []
            source = "verification_resolution_error"
            reason = str(error)
            validation_errors = [str(error)]

        if validation_errors:
            invalid_task_ids.append(current_task_id)
        elif not commands:
            missing_task_ids.append(current_task_id)
        task_payloads.append(
            {
                "id": current_task_id,
                "status": status,
                "commands": commands,
                "source": source,
                "reason": reason,
                "validation_errors": validation_errors,
            }
        )

    if unknown_task:
        missing_task_ids.append(requested_task_id)

    suggested_commands: list[str] = []
    suggestion_specs = build_verification_command_specs(
        _dedupe_strings(repo_scan.likely_test_commands),
        source="repo_scan.likely_test_commands",
        contract_type="repo_native",
    )
    for spec in suggestion_specs:
        if spec.validation_status == VerificationCommandValidationStatus.VALID:
            suggested_commands.append(spec.original_text)

    integration_commands: list[str] = []
    integration_source = "session.verification_disabled"
    integration_error = ""
    integration_invalid = False
    if integration_mode != "off":
        try:
            integration_selection = resolve_integration_verify_commands(
                cfg=cfg,
                integration_verify_cmd=integration_verify_cmd,
                verify_cmd=verify_cmd,
                root=paths.root,
                repo_scan=repo_scan,
            )
            integration_commands = list(integration_selection.commands)
            integration_source = integration_selection.source
            integration_specs = build_verification_command_specs(
                integration_selection.commands,
                source=integration_selection.source,
                contract_type=_integration_verification_contract_type(integration_selection.source),
            )
            integration_errors = [
                f"{spec.original_text}: {spec.rejection_reason or 'invalid_command'}"
                for spec in integration_specs
                if spec.validation_status == VerificationCommandValidationStatus.INVALID
            ]
            if integration_errors:
                integration_invalid = True
                integration_error = "Integration verification command is invalid: " + "; ".join(
                    integration_errors[:3]
                )
            elif not integration_commands:
                integration_error = (
                    "No authoritative integration verification command is available."
                )
        except (IntegrationGateError, VerifyError) as error:
            integration_source = "unavailable"
            integration_error = str(error)

    missing_task_ids = _dedupe_strings(missing_task_ids)
    invalid_task_ids = _dedupe_strings(invalid_task_ids)
    task_policy_blocked = bool(invalid_task_ids) or (
        verify_mode == "strict" and bool(missing_task_ids)
    )
    integration_policy_blocked = integration_invalid or (
        integration_mode == "strict" and (bool(integration_error) or not integration_commands)
    )
    no_selected_tasks = not selected_tasks and not requested_task_id
    ok = (
        not unknown_task
        and not no_selected_tasks
        and not task_policy_blocked
        and not integration_policy_blocked
    )

    issues: list[str] = []
    if unknown_task:
        issues.append(f"Task not found in the current Forge plan: {requested_task_id}.")
    elif no_selected_tasks:
        issues.append("The current Forge plan has no incomplete tasks to verify.")
    if invalid_task_ids:
        issues.append(
            "Verification commands need attention for tasks: " + ", ".join(invalid_task_ids) + "."
        )
    if verify_mode == "strict" and missing_task_ids:
        issues.append(
            "Strict verification requires an authoritative command for tasks: "
            + ", ".join(missing_task_ids)
            + "."
        )
    if integration_policy_blocked and integration_error:
        issues.append(integration_error)
    message = " ".join(issues)
    payload = {
        "ok": ok,
        "verify_mode": verify_mode,
        "integration_verify_mode": integration_mode,
        "tasks": task_payloads,
        "missing_task_ids": missing_task_ids,
        "invalid_task_ids": invalid_task_ids,
        "integration_commands": integration_commands,
        "integration_source": integration_source,
        "integration_error": integration_error,
        "suggested_commands": suggested_commands,
        "message": message,
    }
    _emit_forge_assessment(
        payload,
        json_output=json_output,
        human_message=(
            "[green]Verification assessment passed.[/green]"
            if ok
            else f"[yellow]Verification requires attention:[/yellow] {message}"
        ),
    )


@forge_app.command("plan")
def forge_plan(
    ctx: typer.Context = None,
    path: Path = typer.Option(
        Path("."),
        "--path",
        help="Workspace path or repository subdirectory.",
    ),
    create_path: bool = typer.Option(
        False,
        "--create-path",
        help="Create --path if it does not exist before binding the workspace.",
    ),
    allow_broad_workspace: bool = typer.Option(
        False,
        "--allow-broad-workspace",
        help="Allow guarded broad workspaces instead of choosing a narrower project folder.",
    ),
    goal: str | None = typer.Option(
        None,
        "--goal",
        help="Explicit planning goal. Prompts when omitted in human mode.",
    ),
    intent: str | None = typer.Option(
        None,
        "--intent",
        help="Phase-0 intent: build, fix, refactor, audit, test, or document.",
    ),
    trusted_pytest: Path | None = typer.Option(
        None,
        "--trusted-pytest",
        help="Exact engine-trusted pytest executable.",
    ),
    model: str | None = typer.Option(None, "--model", help="Model bound to this generation."),
    base_url: str | None = typer.Option(
        None,
        "--base-url",
        help="Provider endpoint bound to this generation.",
    ),
    planning_profile: str = typer.Option(
        "quick",
        "--planning-profile",
        help="Planner profile: quick (lean, one blocker question) or deep (full impact scan).",
    ),
    max_steps: int = typer.Option(
        64,
        "--max-steps",
        min=1,
        max=10_000,
        help="Maximum model steps for the single Phase-0 task.",
    ),
    timeout_seconds: int = typer.Option(
        3600,
        "--timeout-seconds",
        min=1,
        max=86_400,
        help="Whole-run monotonic timeout.",
    ),
    api_key_env: str | None = typer.Option(
        None,
        "--api-key-env",
        help="Read the generation-bound API key from this environment variable.",
    ),
    api_key_stdin: bool = typer.Option(
        False,
        "--api-key-stdin",
        help="Prompt for the generation-bound API key (hidden input).",
    ),
    api_key: str | None = typer.Option(
        None,
        "--api-key",
        help="UNSAFE: provide the generation-bound API key as a CLI argument.",
    ),
    machine: bool = typer.Option(False, "--machine", help=MACHINE_OPTION_HELP),
) -> None:
    with machine_session("forge.plan", machine=_machine_enabled(ctx, machine)) as events:
        events.emit(EVENT_RUN_STARTED, {"command": "forge.plan", "path": os.fspath(path)})
        console = _console()
        cli = _cli_module()
        cli._require_active_subscription_ready(model=model, base_url=base_url)
        from ...forge_host_cli import (
            ForgeHostCliError,
            infer_intent_kind,
            plan_ready_lines,
            plan_with_runtime,
        )
        from ...forge_host_native_adapter import ForgeHostPreflightError
        from ...forge_host_runtime import build_local_forge_host_runtime
        from ...forge_planning_profile import normalize_planning_profile

        try:
            explicit_goal = str(goal or "").strip()
            if not explicit_goal:
                explicit_goal = typer.prompt("Forge goal", err=events.enabled).strip()
            if not explicit_goal:
                raise ForgeHostCliError("Forge planning requires an explicit goal.")
            explicit_intent = str(intent or "").strip().casefold() or infer_intent_kind(
                explicit_goal
            )
            if explicit_intent not in {
                "build",
                "fix",
                "refactor",
                "audit",
                "test",
                "document",
            }:
                if events.enabled:
                    raise ForgeHostCliError(
                        "Machine planning requires --intent when the goal is ambiguous."
                    )
                explicit_intent = (
                    typer.prompt("Intent (build/fix/refactor/audit/test/document)")
                    .strip()
                    .casefold()
                )
            if explicit_intent not in {
                "build",
                "fix",
                "refactor",
                "audit",
                "test",
                "document",
            }:
                raise ForgeHostCliError("Forge received an unsupported Phase-0 intent.")
            api_key_override = cli._resolve_api_key_override(
                api_key=api_key,
                api_key_env=api_key_env,
                api_key_stdin=api_key_stdin,
            )
            try:
                resolved_planning_profile = normalize_planning_profile(planning_profile)
            except ValueError as exc:
                raise ForgeHostCliError(str(exc)) from None

            def generation_config():  # type: ignore[no-untyped-def]
                config = clone_cfg(load_config())
                if model is not None:
                    config.model = model
                if base_url is not None:
                    config.base_url = base_url
                return config

            runtime = build_local_forge_host_runtime(
                root=path,
                cli_module=cli,
                console=console,
                trusted_pytest=_resolve_cli_host_pytest(trusted_pytest),
                config_loader=generation_config,
                api_key_override=api_key_override,
                credential_source=("cli:forge-plan" if api_key_override is not None else None),
                per_managed_task_max_steps=max_steps,
                run_timeout_ms=timeout_seconds * 1000,
                verification_commands=("pytest -q",),
                intent_kind=explicit_intent,
                create_if_missing=create_path,
                allow_broad_workspace=allow_broad_workspace,
                planning_profile=resolved_planning_profile,
            )
            events.set_run_id(runtime.paths.run_id)
            result = plan_with_runtime(
                runtime,
                goal=explicit_goal,
                answer_provider=lambda question: typer.prompt(
                    question.prompt,
                    err=events.enabled,
                ),
                intent_kind=explicit_intent,
                verification_commands=("pytest -q",),
                declared_scope_kinds={},
            )
            if events.enabled:
                events.emit(
                    EVENT_PLAN_SAVED,
                    {
                        "plan_ready": result.planned.plan_ready.model_dump(
                            mode="json", by_alias=True
                        ),
                        "resume_manifest": os.fspath(result.manifest_path),
                        "task_count": len(result.planned.plan_ready.plan_definition.tasks),
                        "planning_profile": resolved_planning_profile,
                    },
                )
            else:
                console.rule("[bold cyan]forge plan[/bold cyan]")
                console.print(f"Run ID: {runtime.paths.run_id}")
                console.print(f"Planning profile: {resolved_planning_profile.title()}")
                for line in plan_ready_lines(result.planned.plan_ready):
                    console.print(line)
                console.print(f"Resume authority: {result.manifest_path}")
                console.print("Plan ready. Run `alysis forge run` to review and approve it.")
            events.run_completed(
                ok=True,
                data={
                    "plan": _plan_snapshot(runtime.paths, load_plan(runtime.paths)),
                    "host_plan_fingerprint": result.manifest.host_plan_fingerprint,
                    "planning_profile": resolved_planning_profile,
                },
            )
        except (ConfigError, ForgeError, ForgeHostCliError, ForgeHostPreflightError) as exc:
            console.print(f"[red]Forge Host error:[/red] {exc}")
            events.error(
                message=str(exc),
                kind="forge_host_error",
                exit_code=EXIT_ERROR,
            )
            raise typer.Exit(code=EXIT_ERROR) from None
        except typer.Exit:
            raise
        except Exception:
            console.print("[red]Forge Host error:[/red] The canonical planner failed.")
            events.error(
                message="The canonical Forge planner failed.",
                kind="forge_host_error",
                exit_code=EXIT_ERROR,
            )
            raise typer.Exit(code=EXIT_ERROR) from None


@forge_app.command("attach")
def forge_attach(
    ctx: typer.Context = None,
    source_path: Path = typer.Argument(..., help="File path to attach."),
    path: Path = typer.Option(
        Path("."),
        "--path",
        help="Workspace path or repository subdirectory.",
    ),
    machine: bool = typer.Option(False, "--machine", help=MACHINE_OPTION_HELP),
) -> None:
    with machine_session("forge.attach", machine=_machine_enabled(ctx, machine)) as events:
        console = _console()
        events.emit(
            EVENT_RUN_STARTED,
            {
                "command": "forge.attach",
                "path": os.fspath(path),
                "source_path": os.fspath(source_path),
            },
        )
        if not events.enabled:
            typer.echo(
                "Deprecation: `alysis forge attach` is the legacy asset attachment flow.\n"
                "Use `/assets` from chat or `alysis forge assets add` for the new flow with\n"
                "LLM comprehension and per-task binding. The legacy command continues to work,\n"
                "and attached assets are migrated on the next plan load.",
                err=True,
            )
        try:
            paths, metadata = attach_asset(path, source_path)
        except ForgeError as e:
            raise _forge_error_exit(console, events, e) from e

        console.print(f"Attached to run: {paths.run_id}")
        console.print(f"- original: {metadata.get('original_path')}")
        console.print(f"- stored: {metadata.get('stored_path')}")
        console.print(f"- size: {metadata.get('size_bytes')} bytes")
        if metadata.get("text_copy_path"):
            console.print(f"- extracted text: {metadata.get('text_copy_path')}")

        events.set_run_id(paths.run_id)
        events.run_completed(
            ok=True,
            data={
                "deprecated": True,
                "asset": {
                    "original_path": metadata.get("original_path"),
                    "stored_path": metadata.get("stored_path"),
                    "size_bytes": metadata.get("size_bytes"),
                    "text_copy_path": metadata.get("text_copy_path"),
                },
            },
        )


@forge_app.command("show")
def forge_show(
    ctx: typer.Context = None,
    path: Path = typer.Option(
        Path("."),
        "--path",
        help="Workspace path or repository subdirectory.",
    ),
    machine: bool = typer.Option(False, "--machine", help=MACHINE_OPTION_HELP),
) -> None:
    with machine_session("forge.show", machine=_machine_enabled(ctx, machine)) as events:
        console = _console()
        events.emit(EVENT_RUN_STARTED, {"command": "forge.show", "path": os.fspath(path)})
        try:
            paths = load_current_run_paths(path)
            plan = load_plan(paths)
        except ForgeError as e:
            events.emit(EVENT_PLAN_INVALID, {"reason": str(e), "source": "plan_load"})
            raise _forge_error_exit(console, events, e) from e

        events.set_run_id(paths.run_id)
        console.rule("[bold]forge show[/bold]")
        goal = str(plan.get("project_goal") or "").strip() or "(not set)"
        console.print(f"Run ID: {paths.run_id}")
        console.print(f"Project goal: {goal}")

        status = plan_status(plan)
        if status == PLAN_STATUS_DRAFT:
            console.print("[yellow]Plan status:[/yellow] draft (not execution-ready)")
            for reason in plan_status_detail(plan)["blocking_reasons"][:5]:
                console.print(f"- {reason}")
        else:
            console.print("Plan status: execution_ready")

        # A plan the host had to salvage says so, with the fields it touched.
        repair = plan_repair_metadata(plan)
        if repair["host_repaired"] or repair["forced_draft"]:
            labels = []
            if repair["host_repaired"]:
                labels.append("host-repaired")
            if repair["forced_draft"]:
                labels.append("forced draft after clarification cap")
            console.print("[yellow]Plan repairs:[/yellow] " + ", ".join(labels))
            if repair["host_repaired_fields"]:
                console.print(
                    "Host-repaired fields: " + ", ".join(repair["host_repaired_fields"][:10])
                )

        tasks = plan.get("tasks") or []
        task_table = _Table(title="Tasks")
        task_table.add_column("id")
        task_table.add_column("status")
        task_table.add_column("title")
        task_table.add_column("dependencies")
        if tasks:
            for task in tasks:
                deps = task.get("dependencies") or []
                task_table.add_row(
                    str(task.get("id", "")),
                    str(task.get("status", "")),
                    str(task.get("title", "")),
                    ", ".join(str(d) for d in deps) if deps else "-",
                )
        console.print(task_table)

        assets = forge_asset_view_entries(paths, plan)
        asset_table = _Table(title="Assets")
        asset_table.add_column("source")
        asset_table.add_column("stored_path")
        asset_table.add_column("size_bytes")
        if assets:
            for asset in assets:
                asset_table.add_row(
                    asset.source,
                    asset.stored_path,
                    "" if asset.size_bytes is None else str(asset.size_bytes),
                )
        console.print(asset_table)
        if assets:
            names = [asset.display_name for asset in assets if asset.display_name]
            if names:
                console.print(f"Asset files: {', '.join(names)}")

        snapshot = _plan_snapshot(paths, plan)
        snapshot["assets"] = [
            {
                "source": asset.source,
                "stored_path": asset.stored_path,
                "size_bytes": asset.size_bytes,
                "display_name": asset.display_name,
            }
            for asset in assets
        ]
        events.run_completed(ok=True, data=snapshot)


@forge_app.command("status")
def forge_status(
    ctx: typer.Context = None,
    path: Path = typer.Option(
        Path("."),
        "--path",
        help="Workspace path or repository subdirectory.",
    ),
    machine: bool = typer.Option(False, "--machine", help=MACHINE_OPTION_HELP),
) -> None:
    with machine_session("forge.status", machine=_machine_enabled(ctx, machine)) as events:
        console = _console()
        events.emit(EVENT_RUN_STARTED, {"command": "forge.status", "path": os.fspath(path)})
        try:
            paths = load_current_run_paths(path)
            plan = load_plan(paths)
        except ForgeError as e:
            events.emit(EVENT_PLAN_INVALID, {"reason": str(e), "source": "plan_load"})
            raise _forge_error_exit(console, events, e) from e

        events.set_run_id(paths.run_id)
        asset_count = forge_asset_view_count(paths, plan)
        table = _Table(title="forge status")
        table.add_column("field")
        table.add_column("value")
        table.add_row("run_id", paths.run_id)
        table.add_row("run_dir", os.fspath(paths.run_dir))
        table.add_row("plan_json", os.fspath(paths.plan_json_path))
        table.add_row("plan_md", os.fspath(paths.plan_md_path))
        table.add_row("tasks", str(len(plan.get("tasks") or [])))
        table.add_row("assets", str(asset_count))
        lifecycle = _run_lifecycle_snapshot(paths)
        table.add_row(
            "run_status",
            f"{lifecycle['run_status']} ({lifecycle['run_status_description']})",
        )
        console.print(table)
        if lifecycle["resumable"]:
            console.print(
                "This run stopped before finishing. Continue it with: alysis forge resume --path ."
            )

        snapshot = _plan_snapshot(paths, plan)
        snapshot["asset_count"] = asset_count
        events.run_completed(ok=True, data=snapshot)


@forge_app.command("review")
def forge_review(
    ctx: typer.Context = None,
    task_id: str = typer.Argument(..., help="Task id from plan.json (for example T01)."),
    path: Path = typer.Option(
        Path("."),
        "--path",
        help="Workspace path or repository subdirectory.",
    ),
    model: str | None = typer.Option(None, "--model", help="Model override."),
    base_url: str | None = typer.Option(None, "--base-url", help="Base URL override."),
    temperature: float | None = typer.Option(None, "--temperature", help="Sampling temperature."),
    api_key_env: str | None = typer.Option(
        None,
        "--api-key-env",
        help=(
            "Read API key from this environment variable (overrides ALYSIS_API_KEY/OPENAI_API_KEY)."
        ),
    ),
    api_key_stdin: bool = typer.Option(
        False,
        "--api-key-stdin",
        help="Prompt for API key (hidden input). Key is kept in memory for this run only.",
    ),
    api_key: str | None = typer.Option(
        None,
        "--api-key",
        help=(
            "UNSAFE: Provide API key via CLI argument (may leak via shell history / process list). "
            "Prefer --api-key-stdin or --api-key-env."
        ),
    ),
    verification_contract: Path | None = typer.Option(
        None,
        "--verification-contract",
        hidden=True,
    ),
    machine: bool = typer.Option(False, "--machine", help=MACHINE_OPTION_HELP),
) -> None:
    from ...forge_status_channel import ForgeExecutionStatusSession, ForgeStatusChannel

    status_channel = ForgeStatusChannel.from_environment(required=verification_contract is not None)
    with (
        ForgeExecutionStatusSession(status_channel, command="forge_review") as status_session,
        machine_session("forge.review", machine=_machine_enabled(ctx, machine)) as events,
    ):
        console = _console()
        events.emit(
            EVENT_RUN_STARTED,
            {"command": "forge.review", "path": os.fspath(path), "task_id": task_id},
        )
        _cli_module()._require_active_subscription_ready(model=model, base_url=base_url)
        cfg = _patchable("load_config", load_config)()
        effective = clone_cfg(cfg)
        if base_url is not None:
            apply_runtime_base_url_override(effective, base_url)
        if model is not None:
            effective.model = model
        if temperature is not None:
            _cli_module()._apply_temperature_override(effective, temperature)

        try:
            if not effective.model:
                raise ConfigError("Model is not set. Run: alysis config set model <MODEL>")
            api_key_override = _cli_module()._resolve_api_key_override(
                api_key=api_key,
                api_key_env=api_key_env,
                api_key_stdin=api_key_stdin,
            )
            paths, plan, contract, _task_contract = _load_app_task_bound_forge_contract(
                path=path,
                verification_contract=verification_contract,
                task_id=task_id,
                require_task_commands=False,
            )
            status_session.bind_run_id(paths.run_id)
            events.set_run_id(paths.run_id)
            task = find_task(plan, task_id)
            if task is None:
                raise ForgeError(f"Task not found: {task_id}")
            outcome = _patchable("review_task", review_task)(
                paths=paths,
                plan=plan,
                task=task,
                cfg=effective,
                api_key_override=api_key_override,
            )
            if contract is not None:
                try:
                    contract.validate_approved_plan(load_plan(paths))
                except Exception as exc:
                    raise ForgeError("The approved plan changed during App-bound review.") from exc
        except (ConfigError, ForgeError, ReviewError) as e:
            raise _forge_error_exit(console, events, e, data={"task_id": task_id}) from e

        console.print(f"Task: {task_id} ({task.get('title', '')})")
        console.print(f"Approved: {'yes' if outcome.approved else 'no'}")
        console.print(f"Confidence: {outcome.confidence}")
        console.print(f"Summary: {outcome.summary}")
        console.print(f"Review JSON: {outcome.json_path}")
        console.print(f"Review Markdown: {outcome.markdown_path}")

        review_payload = {
            "task_id": task_id,
            "title": str(task.get("title", "")),
            "approved": bool(outcome.approved),
            "confidence": outcome.confidence,
            "summary": outcome.summary,
            "blocking_issues": outcome.blocking_issues_count,
            "non_blocking_issues": outcome.non_blocking_issues_count,
            "review_json": os.fspath(outcome.json_path),
            "review_markdown": os.fspath(outcome.markdown_path),
        }
        events.emit(EVENT_REVIEW_RESULT, review_payload)
        # A review that rejects work is a review that did its job. Exit 0 and let the
        # approved flag carry the verdict; nonzero stays reserved for command errors.
        events.run_completed(ok=True, data={"review": review_payload})


@forge_app.command("run")
def forge_run(
    ctx: typer.Context = None,
    path: Path = typer.Option(
        Path("."),
        "--path",
        help="Workspace path or repository subdirectory.",
    ),
    allow_broad_workspace: bool = typer.Option(
        False,
        "--allow-broad-workspace",
        help="Allow guarded broad workspaces instead of requiring a narrower project path.",
    ),
    only: str | None = typer.Option(
        None,
        "--only",
        help="Comma-separated task ids to execute (dependencies are still enforced).",
    ),
    max_tasks: int | None = typer.Option(
        None,
        "--max-tasks",
        min=1,
        help="Stop after this many tasks have been executed.",
    ),
    max_attempts: int | None = typer.Option(
        None,
        "--max-attempts",
        min=1,
        help="Skip tasks that already reached this many recorded attempts.",
    ),
    retry_failed: bool = typer.Option(
        False,
        "--retry-failed",
        help="Include tasks currently marked failed.",
    ),
    retry_changes_requested: bool = typer.Option(
        False,
        "--retry-changes-requested",
        help="Include tasks currently marked changes_requested.",
    ),
    keep_going: bool = typer.Option(
        False,
        "--keep-going",
        help=(
            "Continue with the next independent task after a failure instead of stopping. "
            "Default is to stop, because a failed task usually leaves later tasks unsound."
        ),
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print the execution order and exit without running anything.",
    ),
    scope: str = typer.Option(
        "strict",
        "--scope",
        help="Write-scope enforcement: strict by default; use warn or off to opt out.",
    ),
    verify: str = typer.Option(
        "strict",
        "--verify",
        help="Verification policy. Canonical Forge Phase 0 requires strict.",
    ),
    verify_cmd: list[str] | None = typer.Option(
        None,
        "--verify-cmd",
        help="Override verify command for this run (repeatable).",
    ),
    integration_verify: str = typer.Option(
        "off",
        "--integration-verify",
        help="Final integrated-workspace verification policy: off, warn, or strict.",
    ),
    verify_repair_attempts: int | None = typer.Option(
        None,
        "--verify-repair-attempts",
        help=(
            "How many times a failing strict verification is fed back to the agent "
            "to repair before the task fails (default 2; 0 disables)."
        ),
    ),
    pr: bool = typer.Option(
        True,
        "--pr/--no-pr",
        help=(
            "Run each task as a branch/commit/verify/merge cycle in the main checkout "
            "(default). --no-pr leaves changes uncommitted and runs no verification gate, "
            "so use it only where git flow is unavailable."
        ),
    ),
    review: bool = typer.Option(
        False,
        "--review",
        help="Run the automated review gate before merging each task (requires --pr).",
    ),
    base_branch: str | None = typer.Option(
        None,
        "--base-branch",
        help="Base branch for --pr mode (defaults to the current branch).",
    ),
    keep_branch: bool = typer.Option(
        False,
        "--keep-branch",
        help="Keep each task branch after a successful merge.",
    ),
    auto_resolve_conflicts: bool = typer.Option(
        False,
        "--auto-resolve-conflicts",
        help=(
            "On merge conflict, start a resolver agent in a dedicated worktree instead of "
            "stopping with a conflict report. Off by default."
        ),
    ),
    mode: Mode | None = typer.Option(None, "--mode", help="Mode override."),
    model: str | None = typer.Option(None, "--model", help="Model override."),
    base_url: str | None = typer.Option(None, "--base-url", help="Base URL override."),
    temperature: float | None = typer.Option(None, "--temperature", help="Sampling temperature."),
    stream: bool | None = typer.Option(
        None,
        "--stream/--no-stream",
        help="Enable streamed assistant output.",
    ),
    max_steps: int | None = typer.Option(
        None,
        "--max-steps",
        help="Optional safety limit on each managed agent task.",
    ),
    trusted_pytest: Path | None = typer.Option(
        None, "--trusted-pytest", help="Exact engine-trusted pytest executable."
    ),
    no_log: bool = typer.Option(False, "--no-log", help="Disable JSONL session logging."),
    api_key_env: str | None = typer.Option(
        None,
        "--api-key-env",
        help=(
            "Read API key from this environment variable (overrides ALYSIS_API_KEY/OPENAI_API_KEY)."
        ),
    ),
    api_key_stdin: bool = typer.Option(
        False,
        "--api-key-stdin",
        help="Prompt for API key (hidden input). Key is kept in memory for this run only.",
    ),
    api_key: str | None = typer.Option(
        None,
        "--api-key",
        help=(
            "UNSAFE: Provide API key via CLI argument (may leak via shell history / process list). "
            "Prefer --api-key-stdin or --api-key-env."
        ),
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        help="Approve the exact displayed Host-v2 authority once without prompting.",
    ),
    verification_contract: Path | None = typer.Option(
        None,
        "--verification-contract",
        hidden=True,
    ),
    run_id: str | None = typer.Option(None, "--run-id", hidden=True),
    generation_id: str | None = typer.Option(None, "--generation-id", hidden=True),
    machine: bool = typer.Option(False, "--machine", help=MACHINE_OPTION_HELP),
) -> None:
    """Execute every ready task sequentially, in dependency order, in this checkout.

    This is the default way to execute a Forge plan. One task at a time, no
    worktrees, no batch integration gate: each task branches, commits, verifies
    and merges in the main checkout before the next one starts, so a failure
    stops the run at a state you can inspect and resume from.

    Use `forge swarm` instead when independent tasks should run in parallel and
    the extra machinery (worktrees, batch integration gate, conflict resolution)
    is worth it.
    """
    from ...forge_status_channel import (
        ForgeExecutionStatusSession,
        ForgeStatusChannel,
        ForgeTerminalStatus,
    )

    status_channel = ForgeStatusChannel.from_environment(required=verification_contract is not None)
    with (
        ForgeExecutionStatusSession(
            status_channel,
            command="forge_run",
            dry_run=dry_run,
        ) as status_session,
        machine_session("forge.run", machine=_machine_enabled(ctx, machine)) as events,
    ):
        events.emit(
            EVENT_RUN_STARTED,
            {
                "command": "forge.run",
                "path": os.fspath(path),
                "parallel": 1,
                "worktrees": False,
                "dry_run": dry_run,
                "only": only,
                "max_tasks": max_tasks,
                "pr": pr,
                "review": review,
                "verify": verify,
            },
        )
        console = _console()
        cli = _cli_module()
        from ...forge_host_cli import (
            ForgeHostCliError,
            approval_lines,
            load_resume_manifest,
            run_event_lines,
            run_with_existing_runtime,
        )
        from ...forge_host_native_adapter import ForgeHostPreflightError
        from ...forge_host_protocol.models import ApprovalRequired, RunTerminal
        from ...forge_host_runtime import build_local_forge_host_runtime_for_existing_plan
        from ...verify_gate import VerifyError, normalize_verify_mode

        try:
            paths = load_current_run_paths(path)
            manifest = load_resume_manifest(paths)
            if getattr(manifest, "native_run_id", paths.run_id) != paths.run_id:
                raise ForgeHostCliError("The Host-v2 resume record belongs to another run.")
            plan: dict[str, Any] | None = None
            contract = None
            bound_generation_id: str | None = None
            if verification_contract is not None or run_id or generation_id:
                paths, plan, contract, bound_generation_id = _load_app_bound_forge_contract(
                    path=path,
                    verification_contract=verification_contract,
                    run_id=run_id,
                    generation_id=generation_id,
                    verify_mode=verify,
                    integration_verify_mode=integration_verify,
                )
            elif dry_run:
                plan = load_plan(paths)
            status_session.bind_run_id(paths.run_id)
            if status_channel is not None:
                status_channel.emit_progress(
                    phase="preflight",
                    message="Forge validated the approved run and verification contract.",
                    run_id=paths.run_id,
                )
            try:
                integration_mode = normalize_verify_mode(integration_verify)
            except VerifyError as exc:
                raise ForgeHostCliError(str(exc)) from exc
            unsupported: list[str] = []
            if only is not None:
                unsupported.append("--only")
            if max_tasks not in {None, 1}:
                unsupported.append("--max-tasks")
            if retry_failed:
                unsupported.append("--retry-failed")
            if retry_changes_requested:
                unsupported.append("--retry-changes-requested")
            if keep_going:
                unsupported.append("--keep-going")
            if scope != "strict":
                unsupported.append("--scope")
            if verify != "strict":
                unsupported.append("--verify")
            if contract is not None:
                if verify_cmd:
                    unsupported.append("--verify-cmd")
            elif verify_cmd is not None and tuple(verify_cmd) != manifest.verification_commands:
                unsupported.append("--verify-cmd")
            if contract is None and integration_mode != "off":
                unsupported.append("--integration-verify")
            if verify_repair_attempts not in {None, 0}:
                unsupported.append("--verify-repair-attempts")
            if not pr:
                unsupported.append("--no-pr")
            if review:
                unsupported.append("--review")
            if base_branch is not None and base_branch.strip() != (paths.current_branch or ""):
                unsupported.append("--base-branch")
            if keep_branch:
                unsupported.append("--keep-branch")
            if auto_resolve_conflicts:
                unsupported.append("--auto-resolve-conflicts")
            if mode not in {None, Mode.auto, Mode.fullaccess}:
                unsupported.append("--mode")
            if temperature is not None:
                unsupported.append("--temperature")
            if stream is not None:
                unsupported.append("--stream/--no-stream")
            if max_steps is not None and max_steps != manifest.per_managed_task_max_steps:
                unsupported.append("--max-steps")
            if no_log:
                unsupported.append("--no-log")
            if unsupported:
                names = ", ".join(sorted(set(unsupported)))
                raise ForgeHostCliError(
                    f"Canonical Forge Phase 0 does not permit these execution overrides: {names}."
                )
            if dry_run:
                assert plan is not None
                preview = {
                    "status": "dry_run",
                    "run_id": paths.run_id,
                    "generation_id": bound_generation_id,
                    "contract_fingerprint": (
                        contract.contract_fingerprint if contract is not None else None
                    ),
                    "host_plan_fingerprint": manifest.host_plan_fingerprint,
                    "task_ids": [
                        str(task.get("id") or "")
                        for task in (plan.get("tasks") or [])
                        if isinstance(task, dict)
                        and task.get("analysis_only") is not True
                        and str(task.get("status") or "planned").strip().lower()
                        not in _FORGE_TERMINAL_TASK_STATUSES
                    ],
                    "verify": verify,
                    "integration_verify": integration_mode,
                    "max_attempts": max_attempts,
                    "base_branch": base_branch or paths.current_branch,
                }
                if events.enabled:
                    events.run_completed(ok=True, data=preview)
                else:
                    console.rule("[bold cyan]forge run (dry-run)[/bold cyan]")
                    console.print_json(data=preview)
                    events.run_completed(ok=True, data=preview)
                return
            if events.enabled and not yes:
                raise ForgeHostCliError("Machine execution requires --yes for exact approval.")
            cli._require_active_subscription_ready(model=model, base_url=base_url)
            api_key_override = cli._resolve_api_key_override(
                api_key=api_key,
                api_key_env=api_key_env,
                api_key_stdin=api_key_stdin,
            )

            def generation_config():  # type: ignore[no-untyped-def]
                config = clone_cfg(load_config())
                if model is not None:
                    config.model = model
                if base_url is not None:
                    config.base_url = base_url
                return config

            runtime = build_local_forge_host_runtime_for_existing_plan(
                root=getattr(paths, "root", path),
                planner_input=manifest.planner_input,
                expected_host_plan_fingerprint=manifest.host_plan_fingerprint,
                expected_native_approval_fingerprint=manifest.native_approval_fingerprint,
                cli_module=cli,
                console=console,
                trusted_pytest=_resolve_cli_host_pytest(trusted_pytest),
                config_loader=generation_config,
                api_key_override=api_key_override,
                credential_source=("cli:forge-run" if api_key_override is not None else None),
                per_managed_task_max_steps=manifest.per_managed_task_max_steps,
                run_timeout_ms=manifest.run_timeout_ms,
                verification_commands=manifest.verification_commands,
                intent_kind=manifest.intent_kind,
                declared_scope_kinds=dict(manifest.declared_scope_kinds),
                generation_id=bound_generation_id,
            )
            if runtime.paths.run_id != paths.run_id:
                raise ForgeHostCliError("Host-v2 rebound execution to another native run.")
            if (
                bound_generation_id is not None
                and runtime.service.identity.generation_id != bound_generation_id
            ):
                raise ForgeHostCliError("Host-v2 rebound execution to another generation.")
            events.set_run_id(runtime.paths.run_id)

            def approval_provider(ready, approval: ApprovalRequired):  # type: ignore[no-untyped-def]
                if not events.enabled:
                    console.rule("[bold yellow]Forge approval[/bold yellow]")
                    binding = runtime.provider_authority.binding
                    for line in approval_lines(
                        ready,
                        approval,
                        provider_display=f"{binding.provider} / {binding.model}",
                    ):
                        console.print(line)
                if yes:
                    return "allow_once"
                return "allow_once" if typer.confirm("Allow this exact run once?") else "deny"

            result = run_with_existing_runtime(
                runtime,
                manifest=manifest,
                approval_provider=approval_provider,
            )
            if result.started is None:
                if not events.enabled:
                    console.print("Forge execution was not approved.")
                if status_channel is not None:
                    status_channel.emit(
                        ForgeTerminalStatus.CANCELLED,
                        reason_code="forge_run_not_approved",
                        message="Forge execution was not approved.",
                        run_id=paths.run_id,
                        retryable=False,
                    )
                events.run_completed(
                    ok=False,
                    exit_code=130,
                    data={"status": "denied", "host_events": []},
                )
                raise typer.Exit(code=130)
            host_events = result.started.outcome.events
            terminals = [
                event.payload for event in host_events if isinstance(event.payload, RunTerminal)
            ]
            if len(terminals) != 1:
                raise ForgeHostCliError("Forge execution returned no authoritative terminal.")
            terminal = terminals[0].result
            app_verification: dict[str, Any] | None = None
            if terminal.status == "succeeded" and contract is not None:
                try:
                    contract.validate_approved_plan(load_plan(paths))
                except Exception as exc:
                    raise ForgeHostCliError(
                        "The approved plan changed before App contract verification."
                    ) from exc
                if status_channel is not None:
                    status_channel.emit_progress(
                        phase="verification",
                        message="Forge is validating the exact App verification contract.",
                        run_id=paths.run_id,
                    )
                app_verification = _run_app_contract_verification(
                    contract=contract,
                    paths=paths,
                    run_timeout_ms=manifest.run_timeout_ms,
                    include_integration=integration_mode != "off",
                )
            if events.enabled:
                host_payloads = [
                    event.model_dump(mode="json", by_alias=True) for event in host_events
                ]
            else:
                console.rule("[bold cyan]forge run[/bold cyan]")
                for line in run_event_lines(host_events):
                    console.print(line)
                host_payloads = []
            contract_failed = bool(
                app_verification is not None
                and (
                    not app_verification["task_all_passed"]
                    or (
                        integration_mode == "strict"
                        and not app_verification["integration_all_passed"]
                    )
                )
            )
            ok = terminal.status == "succeeded" and not contract_failed
            if contract_failed:
                exit_code = 1
                resolved_status = ForgeTerminalStatus.VERIFICATION_FAILED
                resolved_reason = "app_contract_verification_failed"
                resolved_message = "Forge failed the exact App verification contract."
                retryable = True
            elif terminal.status == "succeeded":
                exit_code = 0
                resolved_status = ForgeTerminalStatus.SUCCEEDED
                resolved_reason = "forge_run_completed"
                resolved_message = "Forge completed the approved plan and verification."
                retryable = False
            elif terminal.status == "cancelled":
                exit_code = 130
                resolved_status = ForgeTerminalStatus.CANCELLED
                resolved_reason = "forge_run_cancelled"
                resolved_message = "Forge execution was cancelled."
                retryable = False
            else:
                exit_code = 1
                resolved_status = ForgeTerminalStatus.EXECUTION_FAILED
                resolved_reason = "forge_host_execution_failed"
                resolved_message = "Forge Host-v2 execution did not complete successfully."
                retryable = terminal.status == "interrupted"
            if status_channel is not None:
                status_channel.emit(
                    resolved_status,
                    reason_code=resolved_reason,
                    message=resolved_message,
                    action=(
                        "Review the retained Forge evidence and retry the unfinished run."
                        if retryable
                        else None
                    ),
                    run_id=paths.run_id,
                    provider_calls=terminal.usage.model_calls,
                    retryable=retryable,
                )
            events.run_completed(
                ok=ok,
                exit_code=exit_code,
                data={
                    "status": "verification_failed" if contract_failed else terminal.status,
                    "reason_code": resolved_reason,
                    "run_id": paths.run_id,
                    "generation_id": bound_generation_id,
                    "contract_fingerprint": (
                        contract.contract_fingerprint if contract is not None else None
                    ),
                    "app_verification": app_verification,
                    "host_events": host_payloads,
                },
            )
            if exit_code:
                raise typer.Exit(code=exit_code)
        except typer.Exit:
            raise
        except (ConfigError, ForgeError, ForgeHostCliError, ForgeHostPreflightError) as exc:
            console.print(f"[red]Forge Host error:[/red] {exc}")
            events.error(
                message=str(exc),
                kind="forge_host_error",
                exit_code=EXIT_ERROR,
            )
            raise typer.Exit(code=EXIT_ERROR) from None
        except Exception:
            console.print("[red]Forge Host error:[/red] The canonical execution failed.")
            events.error(
                message="The canonical Forge execution failed.",
                kind="forge_host_error",
                exit_code=EXIT_ERROR,
            )
            raise typer.Exit(code=EXIT_ERROR) from None


@forge_app.command("unlock")
def forge_unlock(
    ctx: typer.Context = None,
    path: Path = typer.Option(
        Path("."),
        "--path",
        help="Workspace path or repository subdirectory.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help=(
            "Clear the lock even when the owner is not provably gone. Use only when you "
            "know that process is dead: two concurrent executions can corrupt the run."
        ),
    ),
    machine: bool = typer.Option(False, "--machine", help=MACHINE_OPTION_HELP),
) -> None:
    """Inspect this workspace's Forge locks and clear the ones that are safe to clear.

    Exit 0 when the workspace is unlocked (nothing was there, or everything stale was
    cleared). Exit 1 when a lock was kept because its owner could still be alive --
    that is a reportable refusal, not a command failure.
    """
    with machine_session("forge.unlock", machine=_machine_enabled(ctx, machine)) as events:
        events.emit(
            EVENT_RUN_STARTED,
            {"command": "forge.unlock", "path": os.fspath(path), "force": force is True},
        )
        from ..forge_recovery import forge_unlock_impl

        return forge_unlock_impl(path=path, force=force, events=events)


@forge_app.command("resume")
def forge_resume(
    ctx: typer.Context = None,
    path: Path = typer.Option(
        Path("."),
        "--path",
        help="Workspace path or repository subdirectory.",
    ),
    reapprove: bool = typer.Option(
        False,
        "--reapprove",
        help=(
            "Accept the current plan when it drifted since the run was approved. "
            "Without it, a drifted plan stops the resume and lists what changed."
        ),
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Report what would be resumed and exit without executing anything.",
    ),
    allow_broad_workspace: bool = typer.Option(
        False,
        "--allow-broad-workspace",
        help="Allow guarded broad workspaces instead of requiring a narrower project path.",
    ),
    only: str | None = typer.Option(
        None,
        "--only",
        help="Comma-separated task ids to execute (dependencies are still enforced).",
    ),
    max_tasks: int | None = typer.Option(
        None,
        "--max-tasks",
        min=1,
        help="Stop after this many tasks have been executed.",
    ),
    max_attempts: int | None = typer.Option(
        None,
        "--max-attempts",
        min=1,
        help="Skip tasks that already reached this many recorded attempts.",
    ),
    keep_going: bool = typer.Option(
        False,
        "--keep-going",
        help="Continue with the next independent task after a failure instead of stopping.",
    ),
    scope: str = typer.Option(
        "strict",
        "--scope",
        help="Write-scope enforcement: strict by default; use warn or off to opt out.",
    ),
    verify: str = typer.Option(
        "warn",
        "--verify",
        help="Per-task verification policy: off, warn, or strict.",
    ),
    verify_cmd: list[str] | None = typer.Option(
        None,
        "--verify-cmd",
        help="Override verify command for this run (repeatable).",
    ),
    verify_repair_attempts: int | None = typer.Option(
        None,
        "--verify-repair-attempts",
        help="How many times a failing strict verification is fed back to the agent.",
    ),
    pr: bool = typer.Option(
        True,
        "--pr/--no-pr",
        help="Run each task as a branch/commit/verify/merge cycle in the main checkout.",
    ),
    review: bool = typer.Option(
        False,
        "--review",
        help="Run the automated review gate before merging each task (requires --pr).",
    ),
    base_branch: str | None = typer.Option(
        None,
        "--base-branch",
        help="Base branch for --pr mode (defaults to the current branch).",
    ),
    keep_branch: bool = typer.Option(
        False,
        "--keep-branch",
        help="Keep each task branch after a successful merge.",
    ),
    auto_resolve_conflicts: bool = typer.Option(
        False,
        "--auto-resolve-conflicts",
        help="On merge conflict, start a resolver agent instead of stopping with a report.",
    ),
    mode: Mode | None = typer.Option(None, "--mode", help="Mode override."),
    model: str | None = typer.Option(None, "--model", help="Model override."),
    base_url: str | None = typer.Option(None, "--base-url", help="Base URL override."),
    temperature: float | None = typer.Option(None, "--temperature", help="Sampling temperature."),
    stream: bool | None = typer.Option(
        None,
        "--stream/--no-stream",
        help="Enable streamed assistant output.",
    ),
    max_steps: int | None = typer.Option(
        None,
        "--max-steps",
        help="Optional safety limit on each managed agent task.",
    ),
    no_log: bool = typer.Option(False, "--no-log", help="Disable JSONL session logging."),
    api_key_env: str | None = typer.Option(
        None,
        "--api-key-env",
        help=(
            "Read API key from this environment variable (overrides ALYSIS_API_KEY/OPENAI_API_KEY)."
        ),
    ),
    api_key_stdin: bool = typer.Option(
        False,
        "--api-key-stdin",
        help="Prompt for API key (hidden input). Key is kept in memory for this run only.",
    ),
    api_key: str | None = typer.Option(
        None,
        "--api-key",
        help=(
            "UNSAFE: Provide API key via CLI argument (may leak via shell history / process list). "
            "Prefer --api-key-stdin or --api-key-env."
        ),
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        help="In auto mode, skip confirmations for sensitive commands (hard blocks still apply).",
    ),
    machine: bool = typer.Option(False, "--machine", help=MACHINE_OPTION_HELP),
) -> None:
    """Continue an interrupted run from its last incomplete task.

    Re-checks the plan against the fingerprint recorded when the run started. A plan
    that drifted stops the resume and lists exactly what changed; `--reapprove`
    accepts the current plan and continues. Execution itself is `forge run`, with
    retries enabled for the statuses an interruption leaves behind.
    """
    with machine_session("forge.resume", machine=_machine_enabled(ctx, machine)) as events:
        events.emit(
            EVENT_RUN_STARTED,
            {
                "command": "forge.resume",
                "path": os.fspath(path),
                "reapprove": reapprove is True,
                "dry_run": dry_run is True,
            },
        )
        cli = _cli_module()
        cli._require_active_subscription_ready(model=model, base_url=base_url)
        from ..forge_recovery import forge_resume_impl

        return forge_resume_impl(
            cli,
            path=path,
            reapprove=reapprove,
            dry_run=dry_run,
            events=events,
            allow_broad_workspace=allow_broad_workspace,
            only=only,
            max_tasks=max_tasks,
            max_attempts=max_attempts,
            keep_going=keep_going,
            scope=scope,
            verify=verify,
            verify_cmd=verify_cmd,
            verify_repair_attempts=verify_repair_attempts,
            pr=pr,
            review=review,
            base_branch=base_branch,
            keep_branch=keep_branch,
            auto_resolve_conflicts=auto_resolve_conflicts,
            mode=mode,
            model=model,
            base_url=base_url,
            temperature=temperature,
            stream=stream,
            max_steps=max_steps,
            no_log=no_log,
            api_key_env=api_key_env,
            api_key_stdin=api_key_stdin,
            api_key=api_key,
            yes=yes,
            cli_ctx=ctx,
        )


@forge_app.command("swarm")
def forge_swarm(
    ctx: typer.Context = None,
    path: Path = typer.Option(
        Path("."),
        "--path",
        help="Workspace path or repository subdirectory.",
    ),
    allow_broad_workspace: bool = typer.Option(
        False,
        "--allow-broad-workspace",
        help="Allow guarded broad workspaces instead of requiring a narrower project path.",
    ),
    parallel: int = typer.Option(2, "--parallel", min=1, help="Parallel workers per batch."),
    base_branch: str | None = typer.Option(
        None,
        "--base-branch",
        help="Base branch (defaults to current checked out branch).",
    ),
    max_tasks: int | None = typer.Option(
        None,
        "--max-tasks",
        min=1,
        help="Maximum number of tasks to execute in this swarm run.",
    ),
    max_attempts: int | None = typer.Option(
        None,
        "--max-attempts",
        min=1,
        help="Maximum attempts allowed per task before scheduler skips it.",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print schedule and exit."),
    keep_worktrees: bool = typer.Option(
        False,
        "--keep-worktrees",
        help=(
            "Keep failed task worktrees and branches for debugging; default cleanup removes "
            "successful worktrees and rejected failed branch state."
        ),
    ),
    retry_failed: bool = typer.Option(
        False,
        "--retry-failed",
        help="Include tasks currently marked failed.",
    ),
    retry_changes_requested: bool = typer.Option(
        False,
        "--retry-changes-requested",
        help="Include tasks currently marked changes_requested.",
    ),
    only: str | None = typer.Option(
        None,
        "--only",
        help="Comma-separated task ids to execute (still enforces dependencies).",
    ),
    retry_merge_conflicts: bool = typer.Option(
        False,
        "--retry-merge-conflicts",
        help="Retry tasks marked merge_conflict during merge phase.",
    ),
    scope: str = typer.Option(
        "strict",
        "--scope",
        help="Write-scope enforcement: strict by default; use warn or off to opt out.",
    ),
    verify: str = typer.Option(
        "warn",
        "--verify",
        help="Verification policy: off, warn, or strict.",
    ),
    verify_cmd: list[str] | None = typer.Option(
        None,
        "--verify-cmd",
        help="Override verify command for this run (repeatable).",
    ),
    integration_verify: str | None = typer.Option(
        None,
        "--integration-verify",
        help="Batch integration verification policy: off, warn, or strict (defaults to config: warn).",
    ),
    integration_verify_cmd: list[str] | None = typer.Option(
        None,
        "--integration-verify-cmd",
        help="Override integration verify command for this swarm run (repeatable).",
    ),
    replan: str | None = typer.Option(
        None,
        "--replan",
        help="Between-batch replanning mode: off, suggest, or apply.",
    ),
    review: bool = typer.Option(
        False,
        "--review",
        help="Run automated PR review gate before merging task branches.",
    ),
    mode: Mode | None = typer.Option(None, "--mode", help="Mode override."),
    model: str | None = typer.Option(None, "--model", help="Model override."),
    base_url: str | None = typer.Option(None, "--base-url", help="Base URL override."),
    temperature: float | None = typer.Option(None, "--temperature", help="Sampling temperature."),
    stream: bool | None = typer.Option(
        None,
        "--stream/--no-stream",
        help="Enable streamed assistant output.",
    ),
    max_steps: int | None = typer.Option(
        None,
        "--max-steps",
        help="Optional safety limit on each managed agent task.",
    ),
    no_log: bool = typer.Option(False, "--no-log", help="Disable JSONL session logging."),
    api_key_env: str | None = typer.Option(
        None,
        "--api-key-env",
        help=(
            "Read API key from this environment variable (overrides ALYSIS_API_KEY/OPENAI_API_KEY)."
        ),
    ),
    api_key_stdin: bool = typer.Option(
        False,
        "--api-key-stdin",
        help="Prompt for API key (hidden input). Key is kept in memory for this run only.",
    ),
    api_key: str | None = typer.Option(
        None,
        "--api-key",
        help=(
            "UNSAFE: Provide API key via CLI argument (may leak via shell history / process list). "
            "Prefer --api-key-stdin or --api-key-env."
        ),
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        help="In auto mode, skip confirmations for sensitive commands (hard blocks still apply).",
    ),
    machine: bool = typer.Option(False, "--machine", help=MACHINE_OPTION_HELP),
) -> None:
    """Execute independent ready tasks in parallel, each in its own git worktree.

    This is the parallel optimization, not the default way to execute a plan. It
    brings machinery `forge run` does not have -- per-task worktrees, a batch
    integration gate, and agent-driven merge-conflict resolution -- so it pays off
    only when the plan really does have independent tasks with non-overlapping
    write scopes.

    For a dependency chain, or when you want one linear log and a stop-and-inspect
    failure, use `forge run`. `--parallel 1` delegates there automatically.
    """
    with machine_session("forge.swarm", machine=_machine_enabled(ctx, machine)) as events:
        events.emit(
            EVENT_RUN_STARTED,
            {
                "command": "forge.swarm",
                "path": os.fspath(path),
                "parallel": parallel,
                "dry_run": dry_run,
                "only": only,
                "max_tasks": max_tasks,
                "review": review,
                "verify": verify,
            },
        )
        cli = _cli_module()
        cli._require_active_subscription_ready(model=model, base_url=base_url)
        from ..forge import forge_swarm_impl

        return forge_swarm_impl(
            cli,
            path,
            allow_broad_workspace,
            parallel,
            base_branch,
            max_tasks,
            max_attempts,
            dry_run,
            keep_worktrees,
            retry_failed,
            retry_changes_requested,
            only,
            retry_merge_conflicts,
            scope,
            verify,
            verify_cmd,
            integration_verify,
            integration_verify_cmd,
            replan,
            review,
            mode,
            model,
            base_url,
            temperature,
            stream,
            max_steps,
            no_log,
            api_key_env,
            api_key_stdin,
            api_key,
            yes,
            cli_ctx=ctx,
            events=events,
        )


@forge_app.command("exec")
def forge_exec(
    ctx: typer.Context = None,
    task_id: str = typer.Argument(..., help="Task id from plan.json (for example T01)."),
    path: Path = typer.Option(
        Path("."),
        "--path",
        help="Workspace path or repository subdirectory.",
    ),
    mode: Mode | None = typer.Option(None, "--mode", help="Mode override."),
    model: str | None = typer.Option(None, "--model", help="Model override."),
    base_url: str | None = typer.Option(None, "--base-url", help="Base URL override."),
    temperature: float | None = typer.Option(None, "--temperature", help="Sampling temperature."),
    stream: bool | None = typer.Option(
        None,
        "--stream/--no-stream",
        help="Enable streamed assistant output.",
    ),
    max_steps: int | None = typer.Option(
        None,
        "--max-steps",
        help="Optional safety limit on each managed agent task.",
    ),
    no_log: bool = typer.Option(False, "--no-log", help="Disable JSONL session logging."),
    api_key_env: str | None = typer.Option(
        None,
        "--api-key-env",
        help=(
            "Read API key from this environment variable (overrides ALYSIS_API_KEY/OPENAI_API_KEY)."
        ),
    ),
    api_key_stdin: bool = typer.Option(
        False,
        "--api-key-stdin",
        help="Prompt for API key (hidden input). Key is kept in memory for this run only.",
    ),
    api_key: str | None = typer.Option(
        None,
        "--api-key",
        help=(
            "UNSAFE: Provide API key via CLI argument (may leak via shell history / process list). "
            "Prefer --api-key-stdin or --api-key-env."
        ),
    ),
    pr: bool = typer.Option(
        False,
        "--pr/--no-pr",
        help="Run task in PR-like git flow (branch, commit, patch, merge).",
    ),
    review: bool = typer.Option(
        False,
        "--review",
        help="Run automated PR review gate before merge (requires --pr).",
    ),
    base_branch: str | None = typer.Option(
        None,
        "--base-branch",
        help="Base branch for --pr mode (defaults to current branch).",
    ),
    keep_branch: bool = typer.Option(
        False,
        "--keep-branch",
        help="Keep task branch after successful merge in --pr mode.",
    ),
    scope: str = typer.Option(
        "strict",
        "--scope",
        help="Write-scope enforcement: strict by default; use warn or off to opt out.",
    ),
    verify: str = typer.Option(
        "warn",
        "--verify",
        help="Verification policy for PR flow: off, warn, or strict.",
    ),
    verify_cmd: list[str] | None = typer.Option(
        None,
        "--verify-cmd",
        help="Override verify command for this run (repeatable).",
    ),
    verify_repair_attempts: int | None = typer.Option(
        None,
        "--verify-repair-attempts",
        help=(
            "How many times a failing strict verification is fed back to the agent "
            "to repair before the task fails (default 2; 0 disables)."
        ),
    ),
    auto_resolve_conflicts: bool = typer.Option(
        False,
        "--auto-resolve-conflicts",
        help=(
            "On merge conflict, start a resolver agent in a dedicated worktree instead of "
            "stopping with a conflict report. Off by default: the sequential path reports "
            "the conflict and how to land it."
        ),
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        help="In auto mode, skip confirmations for sensitive commands (hard blocks still apply).",
    ),
    verification_contract: Path | None = typer.Option(
        None,
        "--verification-contract",
        hidden=True,
    ),
    machine: bool = typer.Option(False, "--machine", help=MACHINE_OPTION_HELP),
) -> None:
    """Execute one plan task in this checkout.

    Same execution core as `forge run`, which is just this applied to every ready
    task in dependency order. Use `forge run` to execute a whole plan.
    """
    from ...forge_status_channel import ForgeExecutionStatusSession, ForgeStatusChannel

    status_channel = ForgeStatusChannel.from_environment(required=verification_contract is not None)
    with (
        ForgeExecutionStatusSession(status_channel, command="forge_exec") as status_session,
        machine_session("forge.exec", machine=_machine_enabled(ctx, machine)) as events,
    ):
        events.emit(
            EVENT_RUN_STARTED,
            {
                "command": "forge.exec",
                "path": os.fspath(path),
                "task_id": task_id,
                "pr": pr,
                "review": review,
                "verify": verify,
            },
        )
        cli = _cli_module()
        cli._require_active_subscription_ready(model=model, base_url=base_url)
        from ..forge import forge_exec_impl

        try:
            paths, _plan, contract, task_contract = _load_app_task_bound_forge_contract(
                path=path,
                verification_contract=verification_contract,
                task_id=task_id,
                require_task_commands=verify == "strict",
            )
            status_session.bind_run_id(paths.run_id)
            bound_verify_cmd = verify_cmd
            if task_contract is not None:
                contract_commands = [
                    str(command.original_text).strip() for command in task_contract.commands
                ]
                if verify_cmd and tuple(verify_cmd) != tuple(contract_commands):
                    raise ForgeError(
                        f"Explicit verification for {task_id} does not match App authority."
                    )
                bound_verify_cmd = contract_commands
        except ForgeError as exc:
            raise _forge_error_exit(_console(), events, exc) from exc
        try:
            result = forge_exec_impl(
                cli,
                task_id,
                path,
                mode,
                model,
                base_url,
                temperature,
                stream,
                max_steps,
                no_log,
                api_key_env,
                api_key_stdin,
                api_key,
                pr,
                review,
                base_branch,
                keep_branch,
                scope,
                verify,
                bound_verify_cmd,
                yes,
                verify_repair_attempts=verify_repair_attempts,
                auto_resolve_conflicts=auto_resolve_conflicts,
                events=events,
            )
        except typer.Exit:
            if contract is not None:
                try:
                    contract.validate_approved_plan(load_plan(paths))
                except Exception as exc:
                    raise _forge_error_exit(
                        _console(),
                        events,
                        ForgeError("The approved plan changed during App-bound execution."),
                    ) from exc
            raise
        if contract is not None:
            try:
                contract.validate_approved_plan(load_plan(paths))
            except Exception as exc:
                raise _forge_error_exit(
                    _console(),
                    events,
                    ForgeError("The approved plan changed during App-bound execution."),
                ) from exc
        return result
