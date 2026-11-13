"""Provider-free audit of the existing server runner before Bots integration.

Run inside the isolated Linux lab. Uses actual bubblewrap child processes and a
fresh temporary workspace. No HTTP server, saved keys, model or production data.
The synthetic credential checks only construct argv; they never execute it.
"""

from __future__ import annotations

import hashlib
import json
import platform
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

import alysis_code.server.worker_runner as worker_module
from alysis_code.server.settings import ServerSettings
from alysis_code.server.store import ServerStore, ServerStoreError
from alysis_code.server.worker_runner import BwrapProcessRunner, DockerProcessRunner, JobRunner


def settled(runner: JobRunner, job_id: str):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        status = runner.get_status(job_id)
        if status.status in {"succeeded", "failed", "cancelled"}:
            return status
        time.sleep(0.05)
    raise RuntimeError("The synthetic local process did not settle.")


def audit() -> dict:
    if platform.system() != "Linux":
        raise RuntimeError("Run inside the isolated Linux lab.")
    with tempfile.TemporaryDirectory(prefix="bots-worker-audit-") as directory:
        settings = ServerSettings(
            host="127.0.0.1",
            port=7070,
            data_dir=Path(directory) / "state",
            token=None,
            max_upload_bytes=1024,
            max_concurrent_jobs=1,
            worker_backend="bwrap",
            worker_sandbox_mode="strict",
            worker_network="off",
            default_model=None,
            default_base_url=None,
            allow_client_base_url=False,
            allow_client_model=False,
        )
        store = ServerStore(settings)
        run_id = store.create_empty_run()
        paths = store.get_run_paths(run_id)
        command = [
            "python",
            "-c",
            "from pathlib import Path; "
            "p=Path('execution-count.txt'); "
            "p.write_text((p.read_text() if p.exists() else '')+'executed\\n')",
        ]
        runner = JobRunner(settings, store)
        # Keep even ambient provider configuration outside this no-model probe.
        build_env = runner._build_worker_env

        def without_credentials():
            env = build_env()
            env.pop("ALYSIS_API_KEY", None)
            return env

        runner._build_worker_env = without_credentials
        try:
            first = runner.start_job(run_id=run_id, command=command)
            second = runner.start_job(run_id=run_id, command=command)
            statuses = [settled(runner, job_id) for job_id in (first, second)]
            if any(status.status != "succeeded" for status in statuses):
                raise RuntimeError("The actual isolated synthetic process failed.")
            count = (paths.workspace_dir / "execution-count.txt").read_text().count("executed\n")
            outcomes = [status.task_outcome["outcome"] for status in statuses]
        finally:
            runner.close()
        receipts_survive = all(
            (paths.jobs_dir / job_id / "result.json").is_file() for job_id in (first, second)
        )
        restarted = JobRunner(settings, ServerStore(settings))
        try:
            recovered = []
            for job_id in (first, second):
                try:
                    restarted.get_status(job_id)
                except ServerStoreError:
                    recovered.append(False)
                else:
                    recovered.append(True)
        finally:
            restarted.close()
        placeholder = "SYNTHETIC-NOT-A-CREDENTIAL"
        argv_contains_placeholder = {}
        for name, backend in (
            ("docker", DockerProcessRunner(network="off")),
            ("bwrap", BwrapProcessRunner(network="off")),
        ):
            argv = backend.build_argv(
                workspace=paths.workspace_dir,
                job_dir=paths.jobs_dir / first,
                argv=["python", "--version"],
                env={"ALYSIS_API_KEY": placeholder},
            )
            argv_contains_placeholder[name] = any(placeholder in value for value in argv)
        return {
            "observed_at_utc": datetime.now(UTC).isoformat(),
            "kind": "Actual Linux JobRunner/bubblewrap processes; runner-object replacement over the same store; synthetic argv construction",
            "worker_runner_sha256": hashlib.sha256(
                Path(worker_module.__file__).read_bytes()
            ).hexdigest(),
            "same_assignment_created_distinct_job_ids": first != second,
            "actual_execution_count": count,
            "terminal_receipts_survived_runner_replacement": receipts_survive,
            "status_lookup_recovered": recovered,
            "process_success_task_outcomes": outcomes,
            "synthetic_environment_value_in_argv": argv_contains_placeholder,
            "provider_calls": 0,
            "saved_credentials_loaded": False,
            "limits": [
                "Runner-object replacement, not an HTTP daemon crash or worker reconnection test.",
                "Synthetic credential value only; no credential-bearing child was launched.",
                "No provider, Bots integration, tenant boundary or cancellation qualification.",
            ],
        }


if __name__ == "__main__":
    try:
        print(json.dumps(audit(), ensure_ascii=False, indent=2))
    except Exception as exc:
        print(json.dumps({"audit_failed": True, "error_type": type(exc).__name__}))
        raise SystemExit(1) from None
