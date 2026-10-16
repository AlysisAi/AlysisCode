from __future__ import annotations

import os
import platform
import queue
import subprocess
import sys
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from ..atomic_io import atomic_write_json
from ..branding import default_sandbox_docker_image, env_get
from ..bwrap_etc import ensure_minimal_etc_dir
from ..command_text import command_text
from ..forge_events import EVENT_RUN_COMPLETED, is_terminal_event, parse_event_line
from ..logging_redaction import redact_log_text
from ..run_outcome import task_outcome_fields, task_outcome_record
from .settings import ServerSettings
from .store import JobPaths, RunPaths, ServerStore, ServerStoreError

_DEFAULT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
_TERMINAL_JOB_STATUSES = {"succeeded", "failed", "cancelled"}

# How a job's terminal status was decided. "terminal_event" means the worker told us in
# its own words; "exit_code" means we had to infer it from the process result.
STATUS_SOURCE_TERMINAL_EVENT = "terminal_event"
STATUS_SOURCE_EXIT_CODE = "exit_code"


class WorkerRunnerError(RuntimeError):
    pass


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _make_job_id() -> str:
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"job_{ts}_{uuid4().hex[:8]}"


def _supports_bwrap_unshare_cgroup() -> bool:
    try:
        cp = subprocess.run(
            ["bwrap", "--help"],
            check=False,
            capture_output=True,
            **command_text(),
        )
    except OSError:
        return False
    help_text = (cp.stdout or "") + (cp.stderr or "")
    return cp.returncode == 0 and "--unshare-cgroup" in help_text


def _runtime_roots() -> list[Path]:
    candidates: list[Path] = []
    for raw in [sys.prefix, sys.base_prefix, os.path.dirname(sys.executable)]:
        if not raw:
            continue
        p = Path(raw).resolve()
        if p.exists():
            candidates.append(p)
    for raw in sys.path:
        if not raw or not os.path.isabs(raw):
            continue
        p = Path(raw).resolve()
        if p.exists():
            candidates.append(p)

    unique: list[Path] = []
    seen: set[Path] = set()
    for candidate in sorted(candidates, key=lambda p: len(p.parts)):
        if candidate in seen:
            continue
        if any(candidate == parent or candidate.is_relative_to(parent) for parent in unique):
            continue
        unique.append(candidate)
        seen.add(candidate)
    return unique


class OuterProcessRunner(Protocol):
    def spawn(
        self,
        *,
        workspace: Path,
        job_dir: Path,
        argv: list[str],
        env: dict[str, str],
    ) -> subprocess.Popen[str]: ...


@dataclass(frozen=True)
class BwrapProcessRunner:
    network: str = "on"

    def build_argv(
        self,
        *,
        workspace: Path,
        job_dir: Path,
        argv: list[str],
        env: dict[str, str],
    ) -> list[str]:
        workspace_abs = workspace.resolve()
        job_dir_abs = job_dir.resolve()
        args: list[str] = [
            "bwrap",
            "--die-with-parent",
            "--new-session",
            "--clearenv",
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-uts",
            "--bind",
            os.fspath(workspace_abs),
            "/workspace",
            "--dir",
            "/alysis_job",
            "--bind",
            os.fspath(job_dir_abs),
            "/alysis_job",
        ]
        if _supports_bwrap_unshare_cgroup():
            args.append("--unshare-cgroup")
        if self.network == "off":
            args.append("--unshare-net")

        for sys_dir in ("/usr", "/bin", "/lib", "/lib64"):
            if Path(sys_dir).exists():
                args.extend(["--ro-bind", sys_dir, sys_dir])

        etc_dir = ensure_minimal_etc_dir(network=self.network)
        args.extend(
            [
                "--dir",
                "/etc",
                "--ro-bind",
                os.fspath(etc_dir / "passwd"),
                "/etc/passwd",
                "--ro-bind",
                os.fspath(etc_dir / "group"),
                "/etc/group",
                "--ro-bind",
                os.fspath(etc_dir / "nsswitch.conf"),
                "/etc/nsswitch.conf",
                "--ro-bind",
                os.fspath(etc_dir / "hosts"),
                "/etc/hosts",
                "--ro-bind",
                os.fspath(etc_dir / "resolv.conf"),
                "/etc/resolv.conf",
                "--dir",
                "/etc/ssl",
                "--dir",
                "/etc/ssl/certs",
            ]
        )
        if Path("/etc/ssl/certs").exists():
            args.extend(["--ro-bind", "/etc/ssl/certs", "/etc/ssl/certs"])
        args.extend(["--dir", "/etc/pki", "--dir", "/etc/pki/tls", "--dir", "/etc/pki/tls/certs"])
        if Path("/etc/pki/tls/certs").exists():
            args.extend(["--ro-bind", "/etc/pki/tls/certs", "/etc/pki/tls/certs"])
        args.extend(["--dir", "/etc/ca-certificates"])
        if Path("/etc/ca-certificates").exists():
            args.extend(["--ro-bind", "/etc/ca-certificates", "/etc/ca-certificates"])

        for root in _runtime_roots():
            if root == workspace_abs or root.is_relative_to(workspace_abs):
                continue
            if root == job_dir_abs or root.is_relative_to(job_dir_abs):
                continue
            args.extend(["--ro-bind", os.fspath(root), os.fspath(root)])

        args.extend(
            [
                "--proc",
                "/proc",
                "--dev",
                "/dev",
                "--tmpfs",
                "/tmp",
                "--dir",
                "/tmp/home",
                "--chdir",
                "/workspace",
            ]
        )
        for key, value in sorted(env.items()):
            args.extend(["--setenv", key, value])
        args.extend(argv)
        return args

    def spawn(
        self,
        *,
        workspace: Path,
        job_dir: Path,
        argv: list[str],
        env: dict[str, str],
    ) -> subprocess.Popen[str]:
        if platform.system().lower() != "linux":
            raise WorkerRunnerError("bwrap worker backend requires Linux.")
        if not shutil_which("bwrap"):
            raise WorkerRunnerError(
                "bwrap worker backend requested but bubblewrap is not available."
            )
        workspace_abs = workspace.resolve()
        args = self.build_argv(workspace=workspace, job_dir=job_dir, argv=argv, env=env)
        return subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            **command_text(),
            cwd=os.fspath(workspace_abs),
            bufsize=1,
            start_new_session=True,
        )


@dataclass(frozen=True)
class DockerProcessRunner:
    image: str = default_sandbox_docker_image("server")
    network: str = "on"

    def build_argv(
        self,
        *,
        workspace: Path,
        job_dir: Path,
        argv: list[str],
        env: dict[str, str],
    ) -> list[str]:
        workspace_abs = workspace.resolve()
        job_dir_abs = job_dir.resolve()
        args: list[str] = [
            "docker",
            "run",
            "--rm",
            "--cap-drop=ALL",
            "--security-opt",
            "no-new-privileges",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,exec,nosuid,nodev",
            "-v",
            f"{os.fspath(workspace_abs)}:/workspace:rw",
            "-v",
            f"{os.fspath(job_dir_abs)}:/alysis_job:rw",
            "-w",
            "/workspace",
        ]
        if self.network == "off":
            args.extend(["--network", "none"])
        if os.name == "posix" and hasattr(os, "getuid") and hasattr(os, "getgid"):
            args.extend(["--user", f"{os.getuid()}:{os.getgid()}"])
        for key, value in sorted(env.items()):
            args.extend(["-e", f"{key}={value}"])
        args.append(self.image)
        args.extend(argv)
        return args

    def spawn(
        self,
        *,
        workspace: Path,
        job_dir: Path,
        argv: list[str],
        env: dict[str, str],
    ) -> subprocess.Popen[str]:
        if not shutil_which("docker"):
            raise WorkerRunnerError("docker worker backend requested but docker is not available.")
        workspace_abs = workspace.resolve()
        args = self.build_argv(workspace=workspace, job_dir=job_dir, argv=argv, env=env)
        return subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            **command_text(),
            cwd=os.fspath(workspace_abs),
            bufsize=1,
            start_new_session=True,
        )


def shutil_which(binary: str) -> str | None:
    import shutil

    return shutil.which(binary)


@dataclass
class JobState:
    job_id: str
    run_id: str
    status: str
    command: list[str]
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    exit_code: int | None = None
    error: str | None = None
    logs_path: str | None = None
    status_source: str | None = None
    terminal_event: dict[str, object] | None = None
    _cancel_requested: bool = False
    _process: subprocess.Popen[str] | None = None


@dataclass(frozen=True)
class JobWorkItem:
    job_id: str
    run_paths: RunPaths
    job_paths: JobPaths


@dataclass(frozen=True)
class JobStatus:
    job_id: str
    run_id: str
    status: str
    created_at: str
    started_at: str | None
    finished_at: str | None
    exit_code: int | None
    error: str | None
    status_source: str | None = None
    terminal_event: dict[str, object] | None = None
    task_outcome: dict[str, object] | None = None


def job_task_outcome(
    *, status: str, exit_code: int | None, event: Mapping[str, object] | None
) -> dict[str, object]:
    """The worker command's accepted/exit status is distinct from task verification."""
    if status not in _TERMINAL_JOB_STATUSES:
        return task_outcome_record(exit_code=None, reason="running", terminal=False)
    data = event.get("data") if isinstance(event, Mapping) else None
    record = data.get("task_outcome") if isinstance(data, Mapping) else None
    outcome = task_outcome_fields(
        record, exit_code=exit_code, reason="worker_failed" if status == "failed" else "completed"
    )["task_outcome"]
    if status == "cancelled":
        # Cancellation is the outer worker's authority, but it does not erase
        # the host task identity or the artifacts recorded before cancellation.
        outcome.update(
            outcome="cancelled", verified_success=False, reason="cancelled", exit_code=exit_code
        )
    elif status == "failed" and outcome["outcome"] in {"verified_success", "completed_unverified"}:
        outcome.update(
            outcome="incomplete",
            verified_success=False,
            reason="worker_failed",
            exit_code=exit_code or 1,
        )
    return outcome


def job_status_from_terminal_event(event: Mapping[str, object] | None) -> str | None:
    """Map a terminal Forge event to a job status, or None when it says nothing.

    ``run_completed`` reports whether the command's work was accepted; ``error`` means
    the command itself failed. Everything else is not terminal and decides nothing.
    """
    if not isinstance(event, Mapping) or not is_terminal_event(event):
        return None
    if str(event.get("event")) != EVENT_RUN_COMPLETED:
        return "failed"
    data = event.get("data")
    ok = data.get("ok") if isinstance(data, Mapping) else None
    if isinstance(ok, bool):
        return "succeeded" if ok else "failed"
    # A run_completed without an explicit verdict is not evidence of success.
    return None


def _terminal_event_error(event: Mapping[str, object] | None) -> str | None:
    """Surface the worker's own error message when it reported one."""
    if not isinstance(event, Mapping) or str(event.get("event")) == EVENT_RUN_COMPLETED:
        return None
    data = event.get("data")
    if not isinstance(data, Mapping):
        return None
    message = data.get("message")
    return str(message) if message else None


class JobRunner:
    def __init__(self, settings: ServerSettings, store: ServerStore) -> None:
        self._settings = settings
        self._store = store
        self._jobs: dict[str, JobState] = {}
        self._job_paths: dict[str, JobPaths] = {}
        self._jobs_lock = threading.Lock()
        # Fixed-size worker pool: jobs queue here until a worker claims them.
        self._job_queue: queue.Queue[JobWorkItem | None] = queue.Queue()
        self._worker_threads: list[threading.Thread] = []
        self._closed = False
        self._docker_image = env_get(
            "ALYSIS_SERVER_DOCKER_IMAGE",
            default=env_get(
                "ALYSIS_SHELL_SANDBOX_DOCKER_IMAGE",
                default=default_sandbox_docker_image("server"),
            ),
        )
        self._start_workers()

    def _start_workers(self) -> None:
        for idx in range(self._settings.max_concurrent_jobs):
            thread = threading.Thread(
                target=self._worker_loop,
                name=f"alysis-server-worker-{idx + 1}",
                daemon=True,
            )
            thread.start()
            self._worker_threads.append(thread)

    def close(self) -> None:
        with self._jobs_lock:
            if self._closed:
                return
            self._closed = True
        for _ in self._worker_threads:
            self._job_queue.put(None)
        for thread in self._worker_threads:
            thread.join(timeout=1.0)

    def start_job(self, *, run_id: str, command: list[str]) -> str:
        run_paths = self._store.get_run_paths(run_id)
        job_id = _make_job_id()
        job_paths = self._store.create_job_paths(run_id, job_id)
        state = JobState(
            job_id=job_id,
            run_id=run_id,
            status="queued",
            command=command,
            created_at=_now_iso(),
            logs_path=os.fspath(job_paths.logs_path),
        )
        with self._jobs_lock:
            if self._closed:
                raise WorkerRunnerError("Job runner is closed.")
            self._jobs[job_id] = state
            self._job_paths[job_id] = job_paths
        self._write_meta(job_paths, state)
        self._job_queue.put(JobWorkItem(job_id=job_id, run_paths=run_paths, job_paths=job_paths))
        return job_id

    def cancel_job(self, job_id: str) -> None:
        persist_terminal = False
        proc: subprocess.Popen[str] | None = None
        with self._jobs_lock:
            state = self._jobs.get(job_id)
            if state is None:
                raise ServerStoreError(f"Job not found: {job_id}")
            if state.status in _TERMINAL_JOB_STATUSES:
                return
            state._cancel_requested = True
            if state.status == "queued":
                state.status = "cancelled"
                state.finished_at = _now_iso()
                state._process = None
                persist_terminal = True
            else:
                proc = state._process
            job_paths = self._job_paths.get(job_id)
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
        if persist_terminal and job_paths is not None:
            self._write_meta(job_paths, state)
            self._write_result(job_paths, state, error=None)

    def get_status(self, job_id: str) -> JobStatus:
        with self._jobs_lock:
            state = self._jobs.get(job_id)
            if state is None:
                raise ServerStoreError(f"Job not found: {job_id}")
            status = state.status
            job_paths = self._job_paths.get(job_id)
            if (
                status in _TERMINAL_JOB_STATUSES
                and job_paths is not None
                and not job_paths.result_path.exists()
            ):
                status = "running" if state.started_at is not None else "queued"
            return JobStatus(
                job_id=state.job_id,
                run_id=state.run_id,
                status=status,
                created_at=state.created_at,
                started_at=state.started_at,
                finished_at=state.finished_at,
                exit_code=state.exit_code,
                error=state.error,
                status_source=state.status_source,
                terminal_event=state.terminal_event,
                task_outcome=job_task_outcome(
                    status=status, exit_code=state.exit_code, event=state.terminal_event
                ),
            )

    def read_logs(self, job_id: str) -> str:
        with self._jobs_lock:
            state = self._jobs.get(job_id)
            if state is None:
                raise ServerStoreError(f"Job not found: {job_id}")
            logs_path = state.logs_path
        if not logs_path:
            raise ServerStoreError(f"Job has no logs path: {job_id}")
        path = Path(logs_path)
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8", errors="replace")

    def _build_worker_env(self) -> dict[str, str]:
        if self._settings.worker_backend == "docker":
            path_value = _DEFAULT_PATH
        else:
            path_value = os.environ.get("PATH") or _DEFAULT_PATH
        env: dict[str, str] = {
            "PATH": path_value,
            "HOME": "/tmp",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
            "PYTHONUNBUFFERED": "1",
            "ALYSIS_SHELL_SANDBOX_MODE": self._settings.worker_sandbox_mode,
            "ALYSIS_SHELL_SANDBOX_BACKEND": "bwrap",
            "ALYSIS_SHELL_SANDBOX_NETWORK": "off",
            "ALYSIS_SHELL_SANDBOX_CLEAR_ENV": "1",
            "ALYSIS_SHELL_SANDBOX_BWRAP_PROFILE": "hardened",
            "ALYSIS_SHELL_SANDBOX_PROTECT_REPO_META": "1",
            "ALYSIS_VERIFY_SANDBOX_MODE": self._settings.worker_sandbox_mode,
        }
        lang = os.environ.get("LANG")
        if lang:
            env["LANG"] = lang
        api_key = env_get("ALYSIS_API_KEY") or os.environ.get("OPENAI_API_KEY")
        if api_key:
            env["ALYSIS_API_KEY"] = api_key
        return env

    def _outer_runner(self) -> OuterProcessRunner:
        if self._settings.worker_backend == "bwrap":
            return BwrapProcessRunner(network=self._settings.worker_network)
        return DockerProcessRunner(image=self._docker_image, network=self._settings.worker_network)

    def _worker_loop(self) -> None:
        while True:
            item = self._job_queue.get()
            try:
                if item is None:
                    return
                with self._jobs_lock:
                    state = self._jobs.get(item.job_id)
                if state is None:
                    continue
                # Workers skip jobs that were cancelled while they were waiting in the queue.
                self._run_job_inner(state, item.run_paths, item.job_paths)
            finally:
                self._job_queue.task_done()

    def _run_job_inner(self, state: JobState, run_paths: RunPaths, job_paths: JobPaths) -> None:
        with self._jobs_lock:
            if state.status != "queued":
                return
            state.status = "running"
            state.started_at = _now_iso()
        self._write_meta(job_paths, state)

        worker_env = self._build_worker_env()
        (job_paths.job_dir / "config").mkdir(parents=True, exist_ok=True)
        (job_paths.job_dir / "data").mkdir(parents=True, exist_ok=True)
        worker_env["ALYSIS_CONFIG_DIR"] = "/alysis_job/config"
        worker_env["ALYSIS_DATA_DIR"] = "/alysis_job/data"

        runner = self._outer_runner()
        exit_code: int | None = None
        error: str | None = None
        terminal_event: dict[str, object] | None = None
        try:
            proc = runner.spawn(
                workspace=run_paths.workspace_dir,
                job_dir=job_paths.job_dir,
                argv=state.command,
                env=worker_env,
            )
            cancel_requested = False
            with self._jobs_lock:
                state._process = proc
                cancel_requested = state._cancel_requested
            if cancel_requested and proc.poll() is None:
                try:
                    proc.terminate()
                except OSError:
                    pass
            with job_paths.logs_path.open("a", encoding="utf-8") as logs:
                logs.write(redact_log_text(f"[{_now_iso()}] starting job {state.job_id}\n"))
                if proc.stdout is not None:
                    for line in proc.stdout:
                        # Only the on-disk copy is redacted; `line` stays verbatim
                        # below so terminal-event parsing is unaffected.
                        logs.write(redact_log_text(line))
                        logs.flush()
                        # Machine-mode workers narrate themselves. Keep the last
                        # terminal event: it, not the exit code, is what the job
                        # actually reported.
                        event = parse_event_line(line)
                        if event is not None and is_terminal_event(event):
                            terminal_event = event
                exit_code = proc.wait()
            if state._cancel_requested:
                with self._jobs_lock:
                    state.status = "cancelled"
                    state.finished_at = _now_iso()
                    state.exit_code = exit_code
                    state.status_source = STATUS_SOURCE_EXIT_CODE
                    state.terminal_event = terminal_event
                    state._process = None
            else:
                reported_status = job_status_from_terminal_event(terminal_event)
                reported_error = _terminal_event_error(terminal_event)
                with self._jobs_lock:
                    if reported_status is not None:
                        state.status = reported_status
                        state.status_source = STATUS_SOURCE_TERMINAL_EVENT
                        if reported_error:
                            state.error = reported_error
                    else:
                        state.status = "succeeded" if exit_code == 0 else "failed"
                        state.status_source = STATUS_SOURCE_EXIT_CODE
                    state.finished_at = _now_iso()
                    state.exit_code = exit_code
                    state.terminal_event = terminal_event
                    state._process = None
        except Exception as e:  # noqa: BLE001
            error = str(e)
            with self._jobs_lock:
                state.status = "failed"
                state.finished_at = _now_iso()
                state.error = error
                state.exit_code = 1 if exit_code is None else exit_code
                state.status_source = STATUS_SOURCE_EXIT_CODE
                state.terminal_event = terminal_event
                state._process = None
            with job_paths.logs_path.open("a", encoding="utf-8") as logs:
                logs.write(redact_log_text(f"[{_now_iso()}] job failed to start: {error}\n"))

        self._write_meta(job_paths, state)
        self._write_result(job_paths, state, error=error)

    def _write_meta(self, job_paths: JobPaths, state: JobState) -> None:
        payload = {
            "job_id": state.job_id,
            "run_id": state.run_id,
            "status": state.status,
            "status_source": state.status_source,
            "command": state.command,
            "created_at": state.created_at,
            "started_at": state.started_at,
            "finished_at": state.finished_at,
            "exit_code": state.exit_code,
            "error": state.error,
            "logs_path": state.logs_path,
        }
        atomic_write_json(job_paths.meta_path, payload)

    def _write_result(self, job_paths: JobPaths, state: JobState, *, error: str | None) -> None:
        payload = {
            "job_id": state.job_id,
            "run_id": state.run_id,
            "status": state.status,
            "status_source": state.status_source,
            "exit_code": state.exit_code,
            "error": error or state.error,
            "started_at": state.started_at,
            "finished_at": state.finished_at,
            "terminal_event": state.terminal_event,
            "task_outcome": job_task_outcome(
                status=state.status, exit_code=state.exit_code, event=state.terminal_event
            ),
        }
        atomic_write_json(job_paths.result_path, payload)
