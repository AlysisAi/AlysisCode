from __future__ import annotations

import copy
import hashlib
import io
import json
import math
import os
import stat
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..artifact_filesystem import ArtifactFilesystemOperations
from ..atomic_io import atomic_write_json
from ..checkpoint_settings import CheckpointSettings
from ..runtime_artifacts import is_runtime_artifact_path
from ..sensitive_paths import sensitive_path_category

if TYPE_CHECKING:
    from ..config import AnytimeCheckpointConfig


class CheckpointUnavailable(ValueError):
    pass


def _git_listing_may_apply(root: Path) -> bool:
    """Avoid process startup only when no repository can be discovered.

    A Git indirection file, bare repository, inherited Git location, or an
    unreadable ancestor still requires Git's own decision. Do not cache this:
    the second snapshot must observe changes to repository metadata too.
    """
    if any(os.environ.get(key) for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR")):
        return True
    for directory in (root, *root.parents):
        for name in (".git", "HEAD"):
            try:
                (directory / name).lstat()
            except FileNotFoundError:
                continue
            except OSError:
                return True
            return True
    return False


class AnytimeCheckpointManager:
    """Preserve validated bytes without overwriting the user's working tree.

    With no declared numeric objective the first sufficient candidate remains
    best. Later experiments cannot replace it just by being newer. A configured
    objective reads a finite measurement from the actual required-check output.
    """

    def __init__(
        self,
        *,
        root: Path,
        layout: ArtifactFilesystemOperations,
        config: AnytimeCheckpointConfig | CheckpointSettings,
        excluded_paths: tuple[Path, ...] = (),
    ):
        self.root = root.resolve()
        self.layout = layout
        self.config = copy.deepcopy(config)
        self.excluded_paths = tuple(path.resolve() for path in excluded_paths)
        self.best: dict[str, Any] | None = None
        self._task_id = ""
        self._acceptance_revision = ""
        self._attempted: set[tuple[str, int]] = set()
        self._io_stop_at: float | None = None

    def _worker_state(self) -> dict[str, Any]:
        return {
            "best": self.best,
            "task_id": self._task_id,
            "acceptance_revision": self._acceptance_revision,
            "attempted": sorted(self._attempted),
        }

    def _restore_worker_state(self, state: dict[str, Any]) -> None:
        self.best = state["best"]
        self._task_id = str(state["task_id"])
        self._acceptance_revision = str(state["acceptance_revision"])
        self._attempted = {(str(task), int(generation)) for task, generation in state["attempted"]}

    def _check_io_deadline(self) -> None:
        if self._io_stop_at is not None and time.monotonic() >= self._io_stop_at:
            raise CheckpointUnavailable("checkpoint_time_budget_exhausted")

    def _bounded_operation(
        self, action: str, arguments: dict[str, Any], remaining_seconds: float | None
    ) -> Any:
        from .checkpoint_worker import run_checkpoint_worker

        timeout = min(2.0, max(0.0, remaining_seconds)) if remaining_seconds is not None else 2.0
        result = run_checkpoint_worker(
            {
                "action": action,
                "arguments": arguments,
                "root": str(self.root),
                "artifact_root": str(self.layout.filesystem_root),
                "locator_prefix": self.layout.locator_prefix,
                "config": self.config._asdict()
                if isinstance(self.config, CheckpointSettings)
                else self.config.model_dump(mode="json"),
                "excluded_paths": [str(path) for path in self.excluded_paths],
                "state": self._worker_state(),
            },
            timeout=timeout,
        )
        self._restore_worker_state(result["state"])
        return result["event"]

    def _task_index(self, task_id: str) -> Path:
        identity = hashlib.sha256(
            json.dumps([task_id, self._acceptance_revision]).encode()
        ).hexdigest()
        return self.layout.artifact_fs_path("verified_checkpoints", identity, "best.json")

    def select_task(
        self,
        task_id: str,
        *,
        acceptance_revision: str = "",
        remaining_seconds: float | None = None,
        acknowledged_checkpoint: dict[str, Any] | None = None,
    ) -> None:
        if task_id == self._task_id and acceptance_revision == self._acceptance_revision:
            return
        if acknowledged_checkpoint is None:
            self._task_id = task_id
            self._acceptance_revision = acceptance_revision
            self.best = None
            self._attempted.clear()
            return
        try:
            self._bounded_operation(
                "select_task",
                {
                    "task_id": task_id,
                    "acceptance_revision": acceptance_revision,
                    "acknowledged_checkpoint": acknowledged_checkpoint,
                },
                remaining_seconds,
            )
        except (OSError, ValueError, RuntimeError):
            # Recovery may be unavailable, but cannot confer a prior task's
            # checkpoint or a fresh experiment allowance after a failed read.
            self._task_id = task_id
            self._acceptance_revision = acceptance_revision
            self.best = None
            self._attempted = {(task_id, index) for index in range(self.config.max_candidates)}

    def _select_task_inline(
        self,
        task_id: str,
        *,
        acceptance_revision: str = "",
        acknowledged_checkpoint: dict[str, Any] | None = None,
    ) -> None:
        if task_id == self._task_id and acceptance_revision == self._acceptance_revision:
            return
        self._task_id = task_id
        self._acceptance_revision = acceptance_revision
        self.best = None
        self._attempted.clear()
        if acknowledged_checkpoint is None:
            return
        try:
            path = self._task_index(task_id)
            path.resolve().relative_to(self.layout.filesystem_root.resolve())
            # A worker can publish best.json and then time out before the host
            # acknowledges it. Only the host's durable event receipt grants
            # recovery authority; the on-disk index alone cannot promote bytes.
            saved = acknowledged_checkpoint
            self._check_io_deadline()
            archive = self.layout.resolve_locator(saved["archive"])
            if (
                saved["task_id"] == task_id
                and saved.get("acceptance_revision", "") == acceptance_revision
                and saved.get("workspace_root") == str(self.root)
                and saved.get("objective_config") == self._objective_config()
                and hashlib.sha256(archive.read_bytes()).hexdigest() == saved["archive_sha256"]
            ):
                self.best = saved
                budget = json.loads(path.with_name("budget.json").read_text(encoding="utf-8"))
                self._check_io_deadline()
                if (
                    budget.get("task_id") == task_id
                    and budget.get("acceptance_revision", "") == acceptance_revision
                ):
                    self._attempted = {
                        (task_id, int(value)) for value in budget.get("generations", [])
                    }
        except (OSError, ValueError, KeyError, TypeError):
            if self.best is not None:
                # A corrupt/missing budget cannot grant fresh experiments on resume.
                self._attempted = {(task_id, index) for index in range(self.config.max_candidates)}

    def _objective_config(self) -> dict[str, str]:
        return {
            "command_sha256": hashlib.sha256(self.config.objective_command.encode()).hexdigest(),
            "key": self.config.objective_key,
            "direction": self.config.objective_direction,
        }

    @property
    def experiments_exhausted(self) -> bool:
        return bool(
            self.config.objective_command
            and self.best
            and len(self._attempted) >= self.config.max_candidates
        )

    def _snapshot(
        self, stop_at: float, material_paths: list[str]
    ) -> tuple[dict[str, bytes], list[str]]:
        files: dict[str, bytes] = {}
        omitted: list[str] = []
        total = 0
        allowed_paths: set[str] | None = None
        if _git_listing_may_apply(self.root):
            import subprocess

            try:
                listing = subprocess.run(
                    [
                        "git",
                        "-C",
                        str(self.root),
                        "ls-files",
                        "-z",
                        "--cached",
                        "--others",
                        "--exclude-standard",
                    ],
                    capture_output=True,
                    timeout=max(0.001, min(0.5, stop_at - time.monotonic())),
                    check=False,
                )
                if listing.returncode == 0:
                    allowed_paths = {
                        path.decode("utf-8", errors="surrogateescape")
                        for path in listing.stdout.split(b"\0")
                        if path
                    }
                    allowed_paths.update(material_paths)
            except (OSError, subprocess.TimeoutExpired):
                pass
        for directory, directories, names in os.walk(self.root, followlinks=False):
            self._check_io_deadline()
            base = Path(directory)
            directories[:] = [
                name
                for name in directories
                if not (
                    (base / name).is_symlink()
                    or name in {".git", ".venv", "venv", "node_modules", ".worktrees"}
                    or is_runtime_artifact_path(
                        (base / name).relative_to(self.root).as_posix(), root=self.root
                    )
                    or sensitive_path_category((base / name).relative_to(self.root)) is not None
                    or (base / name).resolve() == self.layout.filesystem_root.resolve()
                    or (base / name).resolve() in self.excluded_paths
                    or (
                        allowed_paths is not None
                        and not any(
                            path.startswith((base / name).relative_to(self.root).as_posix() + "/")
                            for path in allowed_paths
                        )
                    )
                )
            ]
            for name in sorted(names):
                if time.monotonic() >= stop_at:
                    raise CheckpointUnavailable("checkpoint_time_budget_exhausted")
                path = base / name
                relative = path.relative_to(self.root).as_posix()
                if path.resolve() in self.excluded_paths or (
                    allowed_paths is not None and relative not in allowed_paths
                ):
                    continue
                if is_runtime_artifact_path(relative, root=self.root):
                    continue
                if sensitive_path_category(relative) is not None or path.is_symlink():
                    omitted.append(relative)
                    continue
                path.resolve().relative_to(self.root)
                before = path.stat()
                if not stat.S_ISREG(before.st_mode):
                    omitted.append(relative)
                    continue
                total += before.st_size
                if len(files) >= self.config.max_files or total > self.config.max_total_bytes:
                    raise CheckpointUnavailable("checkpoint_size_budget_exhausted")
                content = path.read_bytes()
                self._check_io_deadline()
                after = path.stat()
                if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                ):
                    raise CheckpointUnavailable("workspace_changed_during_checkpoint")
                files[relative] = content
        return files, omitted

    def _measurement(self, result: dict[str, Any], accepted_commands: set[str]) -> float | None:
        command = self.config.objective_command.strip()
        if not command or command not in accepted_commands:
            return None
        runs = result.get("results")
        rows = runs if isinstance(runs, list) else [result]
        for row in rows:
            if not isinstance(row, dict):
                continue
            observed = str(
                row.get("effective_cmd") or row.get("cmd") or row.get("command") or ""
            ).strip()
            if observed != command or row.get("exit_code") != 0:
                continue
            try:
                payload = json.loads(row.get("stdout") or "")
                value = payload[self.config.objective_key]
                if (
                    not isinstance(value, bool)
                    and isinstance(value, (int, float))
                    and math.isfinite(value)
                ):
                    return float(value)
            except (ValueError, KeyError, TypeError):
                continue
        return None

    def consider(
        self,
        *,
        outcome: dict[str, Any],
        result: dict[str, Any],
        accepted_commands: set[str],
        remaining_seconds: float | None = None,
    ) -> dict[str, Any] | None:
        from .checkpoint_worker import CheckpointWorkerExpired

        task_id = str(outcome.get("task_id") or "")
        if not self.config.enabled or not task_id:
            return None
        revision = str(outcome.get("acceptance_revision") or "")
        same_task = task_id == self._task_id and revision == self._acceptance_revision
        if not outcome.get("verified_success") and (not same_task or self.best is None):
            return None
        generation = int(outcome.get("generation") or 0)
        if same_task and (task_id, generation) in self._attempted:
            return None
        try:
            return self._bounded_operation(
                "consider",
                {
                    "outcome": outcome,
                    "result": result,
                    "accepted_commands": sorted(accepted_commands),
                    "remaining_seconds": remaining_seconds,
                },
                remaining_seconds,
            )
        except (OSError, ValueError, RuntimeError) as exc:
            if not same_task:
                self._task_id = task_id
                self._acceptance_revision = revision
                self.best = None
                self._attempted = {(task_id, index) for index in range(self.config.max_candidates)}
            self._attempted.add((task_id, generation))
            return {
                "status": "checkpoint_time_budget_exhausted"
                if isinstance(exc, CheckpointWorkerExpired)
                else "checkpoint_unavailable",
                "reason": str(exc),
                "cleanup_pending": bool(getattr(exc, "cleanup_pending", False)),
                "best": self.best,
            }

    def _consider_inline(
        self,
        *,
        outcome: dict[str, Any],
        result: dict[str, Any],
        accepted_commands: set[str],
        remaining_seconds: float | None = None,
    ) -> dict[str, Any] | None:
        task_id = str(outcome.get("task_id") or "")
        if not self.config.enabled or not task_id:
            return None
        self._select_task_inline(
            task_id, acceptance_revision=str(outcome.get("acceptance_revision") or "")
        )
        sufficient = outcome.get("verified_success") and outcome.get("terminal") is True
        if not sufficient and self.best is None:
            return None
        generation = int(outcome.get("generation") or 0)
        score = self._measurement(result, accepted_commands)
        attempt = (task_id, generation)
        if attempt in self._attempted:
            return None
        if len(self._attempted) >= self.config.max_candidates:
            return {"status": "candidate_budget_exhausted", "best": self.best}
        self._attempted.add(attempt)
        budget_path = self._task_index(task_id).with_name("budget.json")
        try:
            budget_path.resolve().relative_to(self.layout.filesystem_root.resolve())
            self._check_io_deadline()
            atomic_write_json(
                budget_path,
                {
                    "task_id": task_id,
                    "acceptance_revision": self._acceptance_revision,
                    "generations": sorted(generation for _, generation in self._attempted),
                },
            )
            self._check_io_deadline()
        except (OSError, ValueError) as exc:
            return {"status": "checkpoint_unavailable", "reason": str(exc), "best": self.best}
        if not sufficient:
            return {"status": "unverified_candidate_rejected", "best": self.best}
        if self.best is not None:
            previous = self.best.get("objective_value")
            better = score is not None and (
                previous is None
                or (
                    score < previous
                    if self.config.objective_direction == "minimize"
                    else score > previous
                )
            )
            if not better:
                return {"status": "prior_candidate_retained", "best": self.best}
        if self.config.objective_command and score is None and self.best is not None:
            return {"status": "objective_unmeasured", "best": self.best}
        time_budget = (
            min(2.0, max(0.0, remaining_seconds)) if remaining_seconds is not None else 2.0
        )
        if time_budget <= 0:
            return {"status": "checkpoint_time_budget_exhausted", "best": self.best}
        try:
            stop_at = min(time.monotonic() + time_budget, self._io_stop_at or math.inf)
            self._check_io_deadline()
            material_paths = list(outcome.get("material_paths") or [])
            files, omitted = self._snapshot(stop_at, material_paths)
            if not files:
                return None
            digests = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
            # Re-read under the same bounded budget: concurrent writers must not
            # publish a mixed version as a verified candidate.
            confirmed, confirmed_omitted = self._snapshot(stop_at, material_paths)
            self._check_io_deadline()
            if omitted != confirmed_omitted or digests != {
                name: hashlib.sha256(data).hexdigest() for name, data in confirmed.items()
            }:
                raise CheckpointUnavailable("workspace_changed_during_checkpoint")
            manifest = {
                "schema_version": 1,
                "task_id": task_id,
                "acceptance_revision": self._acceptance_revision,
                "generation": generation,
                "files": digests,
                "omitted_paths": omitted,
                "verification": outcome,
                "objective_command_sha256": hashlib.sha256(
                    self.config.objective_command.encode()
                ).hexdigest(),
                "objective_key": self.config.objective_key,
                "objective_value": score,
                "objective_direction": self.config.objective_direction,
            }
            import zipfile

            archive_bytes = io.BytesIO()
            with zipfile.ZipFile(archive_bytes, "w", compression=zipfile.ZIP_STORED) as archive:
                for name, data in sorted(files.items()):
                    self._check_io_deadline()
                    info = zipfile.ZipInfo(name)
                    info.external_attr = ((self.root / name).stat().st_mode & 0xFFFF) << 16
                    archive.writestr(info, data)
            data = archive_bytes.getvalue()
            digest = hashlib.sha256(data).hexdigest()
            archive_path = self.layout.artifact_fs_path("verified_checkpoints", digest + ".zip")
            archive_path.parent.mkdir(parents=True, exist_ok=True)
            archive_path.resolve().relative_to(self.layout.filesystem_root.resolve())
            self._check_io_deadline()
            # Exclusive creation; an existing content-addressed object is checked.
            try:
                descriptor = os.open(archive_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "wb") as output:
                    output.write(data)
                    self._check_io_deadline()
            except FileExistsError:
                if hashlib.sha256(archive_path.read_bytes()).hexdigest() != digest:
                    raise CheckpointUnavailable("checkpoint_archive_identity_mismatch") from None
            manifest_digest = hashlib.sha256(
                json.dumps(manifest, sort_keys=True).encode()
            ).hexdigest()
            manifest_path = self.layout.artifact_fs_path(
                "verified_checkpoints", manifest_digest + ".json"
            )
            atomic_write_json(manifest_path, manifest)
            self._check_io_deadline()
            best = {
                "workspace_root": str(self.root),
                "objective_config": self._objective_config(),
                "task_id": task_id,
                "acceptance_revision": self._acceptance_revision,
                "generation": generation,
                "archive": self.layout.locator_for_path(archive_path),
                "archive_sha256": digest,
                "archive_path": archive_path.as_posix(),
                "manifest": self.layout.locator_for_path(manifest_path),
                "manifest_handle": self.layout.register_artifact(manifest_path),
                "objective_value": score,
                "omitted_paths": omitted,
            }
            self._check_io_deadline()
            atomic_write_json(self._task_index(task_id), best)
            self._check_io_deadline()
            self.best = best
            return {"status": "verified_checkpoint_preserved", "best": best}
        except (OSError, ValueError, RuntimeError) as exc:
            return {"status": "checkpoint_unavailable", "reason": str(exc), "best": self.best}
