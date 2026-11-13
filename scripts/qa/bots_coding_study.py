"""Opt-in live pilot: general tool loop versus the real AlysisCode runtime.

This is a research harness, not a production coding route. Both arms use the
same model, task text, call limits, and offline Docker execution environment.
The direct arm has a general instruction and shell tool; AlysisCode retains its
normal coding prompt, editing tools, and verification. Thus this compares the
two complete harnesses, not an isolated causal effect of delegation.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

TASKS = [
    """Build a Python 3 standard-library-only CLI for fictional Aster Notes in this empty workspace.
Interface: python notes.py --db PATH import INPUT.jsonl; python notes.py --db PATH list;
python notes.py --db PATH export OUTPUT.jsonl. Each input nonblank line is an object with
exactly id, client, text, all nonempty strings; preserve Unicode and text exactly.
Import is atomic: any invalid line or conflicting duplicate ID means nonzero exit and no
rows changed. Reimporting an identical ID/client/text is a no-op, including repeated
identical rows within one batch. List prints a JSON array sorted by id; export writes
JSONL sorted by id. Handle missing inputs and malformed JSON without a traceback.
Split storage logic into storage.py. Add meaningful unittest tests and run them. Include
a short README. No external dependencies, network, deployment, or account access.
Implement the files and verify the actual behavior; a proposed plan is not completion.""",
    """Extend the existing CLI without regressing import behavior. Both list and export
must now accept optional --client QUERY. Match client names by Unicode NFC normalization
and casefolded substring comparison; do not remove accents or mutate stored text. An empty
QUERY matches all rows. Treat SQL metacharacters literally. Keep results sorted by id.
Also make import reject whitespace-only field values, while preserving all whitespace in
otherwise nonblank values. Add tests for these changes and run the complete suite.""",
]


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


class Meter:
    def __init__(self, client):
        self.client = client
        self.original = client.chat
        self.records: list[dict] = []
        self.turn_calls = 0

    def chat(self, **kwargs):
        if self.turn_calls >= 12:
            raise RuntimeError("Study model-call allowance reached")
        self.turn_calls += 1
        kwargs["max_tokens"] = min(kwargs.get("max_tokens") or 6000, 6000)
        record = {"call": len(self.records) + 1, "status": "started"}
        self.records.append(record)
        started = time.monotonic()
        try:
            result = self.original(**kwargs)
        except BaseException:
            record.update(status="unconfirmed", seconds=time.monotonic() - started)
            raise
        record.update(
            status="completed",
            seconds=time.monotonic() - started,
            usage=asdict(result.usage) if result.usage else None,
            tools=[call.name for call in result.tool_calls],
        )
        return result


def grade(workspace: Path, stage: int) -> dict:
    result = subprocess.run(
        [
            "docker",
            "--context",
            "desktop-linux",
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt",
            "no-new-privileges",
            "--memory",
            "256m",
            "--cpus",
            "1",
            "--pids-limit",
            "64",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=32m,mode=1777",
            "-v",
            f"{workspace}:/workspace:ro",
            "-v",
            f"{Path(__file__).with_name('bots_coding_checks.py')}:/checks.py:ro",
            "alysis-bots-dev:local",
            "python",
            "/checks.py",
            str(stage),
        ],
        capture_output=True,
        text=True,
        timeout=90,
    )
    return {"exit_code": result.returncode, "output": (result.stdout + result.stderr)[-18000:]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm", choices=["direct", "alysiscode"], required=True)
    parser.add_argument("--profile", default="alysis")
    parser.add_argument("--model", default="claude-haiku-5-5")
    parser.add_argument(
        "--continue-study",
        type=Path,
        help="Retry stage 2 in an existing AlysisCode study workspace, restoring its saved transcript. Recorded as a separate recovery run.",
    )
    parser.add_argument("--execute-live", action="store_true")
    args = parser.parse_args()
    if not args.execute_live:
        parser.error("--execute-live is required; this makes paid/included model calls")
    from alysis_code.config import AppConfig, load_config, resolve_profile_api_key
    from alysis_code.llm.anthropic_messages import AnthropicMessagesClient
    from alysis_code.llm.metadata import PROVIDER_METADATA_KEY
    from alysis_code.llm.provider_limits import ProviderRetrySettings
    from alysis_code.profiles import ProfileSpec, get_profile
    from alysis_code.sandbox_runner import DockerShellRunner

    saved = load_config()
    profile = get_profile(saved, args.profile)
    key = resolve_profile_api_key(saved, args.profile).key
    if profile is None or not key:
        raise RuntimeError("Saved connection unavailable")
    # Invocation-local settings only. Never save or mutate the user's profiles.
    study = Path(tempfile.mkdtemp(prefix=f"alysis-coding-{args.arm}-")).resolve()
    previous = None
    if args.continue_study:
        previous = json.loads((args.continue_study / "report.json").read_text())
        if (
            args.arm != "alysiscode"
            or previous["arm"] != args.arm
            or previous["model"] != args.model
            or previous["turns"][0]["independent_checks"]["exit_code"] != 0
        ):
            raise RuntimeError(
                "Continuation requires a matching AlysisCode study with passing stage 1"
            )
        workspace = Path(previous["workspace"]).resolve(strict=True)
    else:
        workspace = study / "workspace"
        workspace.mkdir()
    os.environ["DOCKER_CONTEXT"] = "desktop-linux"
    os.environ["ALYSIS_MAX_OUTPUT_TOKENS"] = "6000"
    os.environ["ALYSIS_WEB_TOOLS"] = "off"
    write_json(study / "tasks.json", TASKS)
    report = {"arm": args.arm, "model": args.model, "workspace": str(workspace), "turns": []}
    if previous:
        report["continuation_of"] = str(args.continue_study.resolve())
    print(json.dumps({"study": str(study), "arm": args.arm}), flush=True)
    runner = DockerShellRunner(
        network="off",
        docker_image="alysis-bots-dev:local",
        pids_limit=64,
        memory_limit="512m",
        cpus="1",
        read_only_rootfs=True,
    )
    client = AnthropicMessagesClient(
        base_url=profile.base_url,
        api_key=key,
        model=args.model,
        provider_key="alysis",
        timeout_s=90,
        temperature=0.2,
        enable_thinking=False,
        provider_retry_settings=ProviderRetrySettings(max_retries=0, disable_retries=True),
    )
    meter = Meter(client)
    session = None
    messages = [
        {
            "role": "system",
            "content": "You are a general-purpose assistant helping with a fictional product. Use your tools to finish "
            "the user's task and verify results. Report uncertainty honestly. Your shell runs in an isolated "
            "Linux workspace at /workspace with Python 3 and no network. Only this workspace persists. "
            "Do not read secrets, modify unrelated files, or access external services.",
        }
    ]
    shell_schema = [
        {
            "type": "function",
            "function": {
                "name": "shell_run",
                "description": "Run a shell command in the isolated Linux workspace. Read, edit and test files here.",
                "parameters": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                    "additionalProperties": False,
                },
            },
        }
    ]
    try:
        if args.arm == "alysiscode":
            from alysis_code.agent.session import create_session
            from alysis_code.execution_deadline import ExecutionDeadline
            from alysis_code.mcp.manager import McpManager
            from alysis_code.mcp.models import ResolvedMcpConfig
            from alysis_code.runtime_kind import RuntimeKind
            from alysis_code.surface import NoopSurface

            cfg = AppConfig(
                base_url=profile.base_url,
                model=args.model,
                stream=False,
                skills_enabled=False,
                bundled_skills_enabled=False,
                custom_tools_enabled=False,
                web_tools_enabled=False,
                subagents_enabled=False,
                provider_retry_max_retries=0,
                llm_enable_thinking=False,
            )
            cfg.extra_fields = {
                "active_profile": "alysis",
                "profiles": {
                    "alysis": ProfileSpec(
                        name="alysis",
                        base_url=profile.base_url,
                        protocol="anthropic_messages",
                        default_model=args.model,
                    ).to_dict()
                },
                "shell_sandbox": {
                    "mode": "strict",
                    "backend": "docker",
                    "network": "off",
                    "docker_image": "alysis-bots-dev:local",
                    "docker_memory": "512m",
                    "docker_cpus": "1",
                    "docker_pids_limit": 64,
                    "docker_read_only": True,
                },
            }
            manager = McpManager(
                resolved_config=ResolvedMcpConfig(
                    workspace_root=workspace,
                    user_config_path=study / "unused-user-mcp",
                    project_config_path=study / "unused-project-mcp",
                    user_config_present=False,
                    project_config_present=False,
                ),
                workspace_root=workspace,
                runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
            )
            session = create_session(
                cfg=cfg,
                root=workspace,
                mode="auto",
                yes=True,
                max_steps=12,
                no_log=False,
                api_key_override=key,
                surface=NoopSurface(),
                non_interactive=True,
                one_shot_execution=False,
                enable_compaction=False,
                subagents_enabled=False,
                helper_subagents_enabled=False,
                mcp_manager=manager,
                session_log_dir_override=study / "sessions",
                verify_cmd=["python -m unittest discover -v"],
                execution_deadline=ExecutionDeadline.from_duration(600),
            )
            # Measure the actual selected engine transport, retaining the normal engine loop.
            meter = Meter(session.client)
            if session.client.model != args.model or session.client.enable_thinking is not False:
                raise RuntimeError("Study model/thinking selection differs between arms")
            session.client.chat = meter.chat
            if previous:
                from alysis_code.cli_impl.commands.chat_resume_helpers import (
                    _load_chat_resume_messages,
                )

                transcripts = list((args.continue_study / "sessions").glob("*.jsonl"))
                if len(transcripts) != 1:
                    raise RuntimeError("Continuation requires exactly one saved engine transcript")
                restored = _load_chat_resume_messages(transcripts[0])
                if not restored:
                    raise RuntimeError("No conversation to restore")
                session.messages.extend(restored)
                report["restored_messages"] = len(restored)
        stages = [(2, TASKS[1])] if previous else list(enumerate(TASKS, 1))
        for stage, task in stages:
            started = time.monotonic()
            meter.turn_calls = 0
            outcome = {"stage": stage}
            try:
                if session:
                    outcome["exit_code"] = session.run_turn(task)
                    outcome["outcome"] = session.last_turn_outcome
                else:
                    messages.append({"role": "user", "content": task})
                    for _ in range(12):
                        reply = meter.chat(messages=messages, tools=shell_schema, stream=False)
                        assistant = {"role": "assistant", "content": reply.content or None}
                        if reply.tool_calls:
                            assistant["tool_calls"] = [
                                {
                                    "id": c.id,
                                    "type": "function",
                                    "function": {
                                        "name": c.name,
                                        "arguments": json.dumps(c.arguments),
                                    },
                                }
                                for c in reply.tool_calls
                            ]
                        if reply.provider_metadata:
                            assistant[PROVIDER_METADATA_KEY] = reply.provider_metadata
                        messages.append(assistant)
                        if not reply.tool_calls:
                            outcome["reply"] = reply.content
                            break
                        for call in reply.tool_calls:
                            if call.name != "shell_run" or set(call.arguments) != {"command"}:
                                output = {"error": "Invalid tool arguments"}
                            else:
                                result = runner.run(
                                    root=workspace,
                                    cwd=workspace,
                                    cmd=call.arguments["command"],
                                    timeout_s=60,
                                )
                                output = {
                                    "exit_code": result.returncode,
                                    "output": (result.stdout + result.stderr)[-16000:],
                                }
                            messages.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": call.id,
                                    "content": json.dumps(output),
                                }
                            )
                    else:
                        outcome["error"] = "Turn call allowance reached"
            except Exception as exc:
                # Provider exceptions can include request credentials. Never print/persist them.
                outcome["error_type"] = type(exc).__name__
            outcome.update(
                seconds=time.monotonic() - started,
                calls=meter.turn_calls,
                independent_checks=grade(workspace, stage),
            )
            report["turns"].append(outcome)
            report["provider_calls"] = meter.records
            write_json(study / "report.json", report)
            print(
                json.dumps(
                    {
                        "stage": stage,
                        "seconds": outcome["seconds"],
                        "calls": meter.turn_calls,
                        "checks_exit_code": outcome["independent_checks"]["exit_code"],
                        "error_type": outcome.get("error_type"),
                    }
                ),
                flush=True,
            )
            if (
                outcome.get("error_type")
                or outcome.get("error")
                or outcome["independent_checks"]["exit_code"]
            ):
                break
    finally:
        if session:
            session.close()
        report["provider_calls"] = meter.records
        write_json(study / "report.json", report)
    return int(
        any(
            turn.get("error_type")
            or turn.get("error")
            or turn.get("exit_code", 0)
            or turn["independent_checks"]["exit_code"]
            for turn in report["turns"]
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
