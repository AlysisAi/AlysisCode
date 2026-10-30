"""Run real ordinary-agent tasks against an invocation-local subscription selection.

Inputs and expected-output checks belong outside this runner. It records observed
outcomes and artifacts; it does not grade a task from the model's final assertion.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")


def file_manifest(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink() and "__pycache__" not in path.parts
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--tasks", type=Path, required=True, help="JSON array of user instructions")
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--effort", required=True)
    parser.add_argument("--deadline-seconds", type=float, default=900)
    parser.add_argument(
        "--compact-after-tool-results",
        type=int,
        default=0,
        help="Exercise real compaction at a controlled milestone in one live turn",
    )
    parser.add_argument(
        "--mcp-fixture",
        type=Path,
        help="Synthetic local MCP data; the agent and subscription remain real",
    )
    parser.add_argument("--execute-live", action="store_true")
    args = parser.parse_args()
    if not args.execute_live:
        parser.error("Live subscription use requires --execute-live")
    tasks = json.loads(args.tasks.read_text())
    if (
        not isinstance(tasks, list)
        or not tasks
        or any(not isinstance(t, str) or not t.strip() for t in tasks)
    ):
        parser.error("tasks must be a nonempty JSON array of nonempty instructions")
    workspace = args.workspace.resolve(strict=True)
    evidence = args.evidence.resolve()
    if evidence.is_relative_to(workspace) or workspace.is_relative_to(evidence):
        parser.error("Evidence and workspace must be separate directories")
    evidence.mkdir(parents=True, exist_ok=False)
    source = Path(__file__).resolve().parents[2] / "src"
    frozen = evidence / "source"
    shutil.copytree(source, frozen, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    write_json(evidence / "source-manifest.json", file_manifest(frozen))
    sys.path.insert(0, str(frozen))
    # Shell-launched helpers must use the same snapshot as the agent process.
    os.environ["PYTHONPATH"] = str(frozen) + (
        os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else ""
    )

    from alysis_code.agent.session import create_session
    from alysis_code.config import load_config
    from alysis_code.execution_deadline import ExecutionDeadline
    from alysis_code.profiles import get_active_profile, update_profile

    cfg = load_config()
    profile = get_active_profile(cfg)
    if profile.auth_provider != "openai-codex":
        raise RuntimeError("The selected connection is not the authorized ChatGPT subscription")
    update_profile(
        cfg,
        profile.name,
        default_model=args.model,
        reasoning_effort=args.effort,
        allow_subscription_selection=True,
    )
    # Auxiliary model calls must honor the same explicit live-test selection.
    from alysis_code.model_router import ROLE_ENV_VARS

    for variable in ROLE_ENV_VARS.values():
        os.environ[variable] = args.model
    manager = None
    if args.mcp_fixture:
        from alysis_code.mcp.manager import McpManager
        from alysis_code.mcp.models import ResolvedMcpConfig, ResolvedMcpServer
        from alysis_code.runtime_kind import RuntimeKind

        fixture = evidence / "mcp-fixture.json"
        shutil.copy2(args.mcp_fixture.resolve(strict=True), fixture)
        fixture_script = (
            Path(__file__).resolve().parents[2]
            / "tests/fixtures/mcp_servers/minimal_stdio_server.py"
        )
        shutil.copy2(fixture_script, evidence / "mcp-server.py")
        manager = McpManager(
            resolved_config=ResolvedMcpConfig(
                workspace_root=workspace,
                user_config_path=evidence / "unused-user-mcp.json",
                project_config_path=evidence / "unused-project-mcp.json",
                user_config_present=False,
                project_config_present=False,
                servers=(
                    ResolvedMcpServer(
                        id="qualification",
                        transport="stdio",
                        enabled=True,
                        enabled_in=None,
                        trust="explicit",
                        allowed_tools=(),
                        denied_tools=(),
                        startup_timeout_s=10,
                        call_timeout_s=10,
                        tool_prefix=None,
                        command=sys.executable,
                        args=(str(evidence / "mcp-server.py"),),
                        env={"ALYSIS_TEST_MCP_CONFIG": str(fixture)},
                    ),
                ),
            ),
            workspace_root=workspace,
            runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
        )
    write_json(
        evidence / "selection.json",
        {
            "auth_provider": profile.auth_provider,
            "model": args.model,
            "effort": args.effort,
            "workspace": str(workspace),
            "source": str(source),
            "frozen_source": str(frozen),
            "controlled_compaction_after_tool_results": args.compact_after_tool_results,
            "mcp_fixture": bool(args.mcp_fixture),
        },
    )
    write_json(evidence / "inputs.json", file_manifest(workspace))
    session = None
    outcomes = []
    started = time.monotonic()
    try:
        os.chdir(workspace)
        session = create_session(
            cfg=cfg,
            root=workspace,
            mode="auto",
            yes=True,
            max_steps=None,
            no_log=False,
            non_interactive=True,
            one_shot_execution=False,
            subagents_enabled=False,
            session_log_dir_override=evidence / "sessions",
            execution_deadline=ExecutionDeadline.from_duration(args.deadline_seconds),
            mcp_manager=manager,
            compaction_profile="execution",
        )
        if session.client.model != args.model:
            raise RuntimeError("Runtime model differs from the requested test model")
        if session.root.resolve() != workspace:
            raise RuntimeError(
                "Workspace expanded to a parent repository; use an isolated folder or repository root"
            )
        compactor = session.conversation_compactor
        if compactor is not None and compactor.compactor_client.model != args.model:
            raise RuntimeError("Compactor differs from the authorized live-test model")
        if args.compact_after_tool_results:
            if compactor is None:
                raise RuntimeError("The selected runtime has no conversation compactor")
            original_maybe = compactor.maybe_compact
            compacted = False

            def compact_at_milestone(**kwargs):
                nonlocal compacted
                results = sum(m.get("role") == "tool" for m in kwargs["messages"])
                if compacted or results < args.compact_after_tool_results:
                    return original_maybe(**kwargs)
                before = len(kwargs["messages"])
                messages, changed = compactor.compact_now(**kwargs)
                compacted = bool(changed)
                event = {
                    "changed": changed,
                    "messages_before": before,
                    "messages_after": len(messages),
                    "tool_results_before": results,
                    "model": compactor.compactor_client.model,
                }
                write_json(evidence / "controlled-compaction.json", event)
                print(json.dumps({"phase": "controlled_compaction", **event}), flush=True)
                return messages, changed

            compactor.maybe_compact = compact_at_milestone
        for task in tasks:
            code = session.run_turn(task)
            outcomes.append({"exit_code": code, "outcome": session.last_turn_outcome})
            write_json(evidence / "outcomes.json", outcomes)
            check_tool = session.tools.get("artifact_check")
            write_json(
                evidence / "artifact-checks.json",
                check_tool.metadata.get("artifact_reports", []) if check_tool else [],
            )
            print(
                json.dumps(
                    {
                        "turn": len(outcomes),
                        "exit_code": code,
                        "outcome": session.last_turn_outcome.get("outcome"),
                    }
                ),
                flush=True,
            )
            if code:
                break
    finally:
        if session is not None:
            session.close()
        elif manager is not None:
            manager.close()
        write_json(evidence / "outputs.json", file_manifest(workspace))
        write_json(evidence / "timing.json", {"elapsed_seconds": time.monotonic() - started})
    return int(any(outcome["exit_code"] for outcome in outcomes))


if __name__ == "__main__":
    raise SystemExit(main())
