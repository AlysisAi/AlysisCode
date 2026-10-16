"""Fresh consumer checks for declared artifacts; observations never grant authority."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .config import AppConfig
from .execution_deadline import DeadlineExhausted
from .process_reaping import ProcessGroupRegistry, reap_tracked_groups
from .sandbox_runner import _SENSITIVE_ENV_KEYS
from .tools.fs import classify_sensitive_path
from .verify_gate import resolve_verify_sandbox_mode, run_task_verification

PROFILES = frozenset({"library", "cli", "service", "distributed", "data", "optimization"})
_MAX_FILES = 2000
_MAX_BYTES = 64 * 1024 * 1024
_MAX_RESULT_BYTES = 4 * 1024 * 1024

_LAUNCHER = """import json, os, sys, subprocess
from pathlib import Path
spec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
for name in list(os.environ):
    if name.upper() in spec["blocked_env"]:
        os.environ.pop(name, None)
base = Path.cwd()
cache = base / ".consumer-cache" / str(spec["rank"])
cache.mkdir(parents=True, exist_ok=True)
os.environ.update({
    "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
    "PIP_NO_CACHE_DIR": "1", "PIP_CONFIG_FILE": os.devnull,
    "XDG_CACHE_HOME": str(cache), "npm_config_cache": str(cache / "npm"),
    "ALYSIS_CONSUMER_RANK": str(spec["rank"]),
    "ALYSIS_CONSUMER_WORLD_SIZE": str(spec["workers"]),
})
if os.name == "nt":
    # cmd.exe parses its own command line and does not undo the backslash quoting that
    # subprocess applies to a list. Hand it the exact line, as Python's shell=True and
    # Node's spawn(shell) do: /d skips AutoRun, /s strips only the outer pair of quotes.
    shell = os.environ.get("COMSPEC", "cmd.exe")
    line = '"' + shell + '" /d /s /c "' + spec["command"] + '"'
    raise SystemExit(subprocess.call(line, env=dict(os.environ), stdin=subprocess.DEVNULL))
os.execvpe("/bin/sh", ["/bin/sh", "-c", spec["command"]], dict(os.environ))
"""


class _ConsumerProcessRegistry(ProcessGroupRegistry):
    def __init__(self, parent: ProcessGroupRegistry | None):
        super().__init__()
        self.parent = parent

    def register(self, *, pgid: int, command: str, origin: str):
        record = super().register(pgid=pgid, command=command, origin=origin)
        if record is not None and self.parent is not None:
            self.parent.register(pgid=pgid, command=command, origin=origin)
        return record

    def release(self, pgid: int) -> None:
        super().release(pgid)
        if self.parent is not None:
            self.parent.release(pgid)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        reap_tracked_groups(self, grace_seconds=0.1)


def _relative(raw: object) -> Path:
    path = Path(str(raw or ""))
    if not str(raw or "").strip() or path.is_absolute() or ".." in path.parts or path == Path("."):
        raise ValueError("consumer paths must be explicit relative paths within the workspace")
    if path.parts[0].startswith(".consumer-"):
        raise ValueError("consumer paths cannot use the runtime's reserved prefix")
    return path


def validate_consumer_profile(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("profile") not in PROFILES:
        raise ValueError(
            "consumer profile must name library, cli, service, distributed, data, or optimization"
        )
    spec = dict(raw)
    artifacts = spec.get("artifacts", [])
    if not isinstance(artifacts, list) or len(artifacts) > 100:
        raise ValueError("consumer artifacts must be an array with at most 100 entries")
    spec["artifacts"] = [str(_relative(item)) for item in artifacts]
    if not artifacts and spec["profile"] != "service":
        raise ValueError("consumer verification requires declared published artifacts")
    checks = spec.get("checks", [])
    if not isinstance(checks, list) or len(checks) > 12:
        raise ValueError("consumer checks must contain at most 12 checks")
    normalized = []
    for index, check in enumerate(checks):
        if not isinstance(check, dict) or not str(check.get("command") or "").strip():
            raise ValueError("each consumer check requires an executable command")
        expected_exit = check.get("expected_exit", 0)
        if type(expected_exit) is not int or not 0 <= expected_exit <= 255:
            raise ValueError("expected_exit must be an integer between 0 and 255")
        contains = check.get("stdout_contains", [])
        if not isinstance(contains, list) or any(
            not isinstance(item, str) or not item for item in contains
        ):
            raise ValueError("stdout_contains must be an array of nonempty strings")
        kind = str(check.get("kind", "positive"))
        if kind not in {"setup", "positive", "negative", "preservation", "import"}:
            raise ValueError("unknown consumer check kind")
        if kind == "negative" and expected_exit == 0 and not contains:
            raise ValueError("a negative check must assert a rejection exit or output")
        normalized.append(
            {
                "name": str(check.get("name") or f"check-{index + 1}"),
                "command": str(check["command"]).strip(),
                "expected_exit": expected_exit,
                "stdout_contains": contains,
                "kind": kind,
            }
        )
    spec["checks"] = normalized
    module = spec.get("python_module")
    if module is not None:
        if spec["profile"] != "library" or not re.fullmatch(
            r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", str(module)
        ):
            raise ValueError("python_module must be a module name for the library profile")
    roots = spec.get("import_roots", [])
    if not isinstance(roots, list) or len(roots) > 8:
        raise ValueError("import_roots must be an array with at most 8 staged directories")
    spec["import_roots"] = [str(_relative(item)) for item in roots]
    if not normalized and not module:
        raise ValueError("consumer verification requires a real command or Python library import")
    workers = spec.get("workers", 1)
    if type(workers) is not int or not 1 <= workers <= 8:
        raise ValueError("workers must be between 1 and 8")
    if spec["profile"] == "distributed" and workers < 2:
        raise ValueError("distributed consumer verification requires at least two real workers")
    if spec["profile"] != "distributed" and workers != 1:
        raise ValueError("multiple workers require the distributed profile")
    spec["workers"] = workers
    if spec["profile"] == "service" and not str(spec.get("service_id") or "").strip():
        raise ValueError("service consumer verification requires an owned durable service_id")
    if spec.get("service_scope", "endpoint") not in {"endpoint", "process"}:
        raise ValueError("service_scope must be endpoint or process")
    result_checks = spec.get("result_checks", [])
    if not isinstance(result_checks, list) or len(result_checks) > 12:
        raise ValueError("result_checks must be an array with at most 12 entries")
    for check in result_checks:
        if not isinstance(check, dict) or check.get("format") not in {"json", "csv", "bytes"}:
            raise ValueError("result checks require json, csv, or bytes format")
        _relative(check.get("path"))
        for key in ("required_keys", "columns"):
            if key in check and (
                not isinstance(check[key], list)
                or any(not isinstance(item, str) for item in check[key])
            ):
                raise ValueError(f"{key} must be an array of strings")
        for key in ("min_items", "min_rows"):
            if key in check and (type(check[key]) is not int or check[key] < 0):
                raise ValueError(f"{key} must be a nonnegative integer")
        if check["format"] == "json" and not any(
            key in check for key in ("required_keys", "expected", "min_items")
        ):
            raise ValueError("JSON result verification requires a schema or content assertion")
    if spec["profile"] == "data" and not result_checks:
        raise ValueError("data consumer verification requires content or schema checks")
    if spec["profile"] == "optimization":
        metric = spec.get("metric")
        if not isinstance(metric, dict) or not str(metric.get("key") or ""):
            raise ValueError("optimization requires a JSON metric path and key")
        _relative(metric.get("path"))
        if metric.get("direction") not in {"minimize", "maximize"}:
            raise ValueError("metric direction must be minimize or maximize")
        if type(metric.get("baseline")) not in {int, float} or not math.isfinite(
            metric["baseline"]
        ):
            raise ValueError("metric baseline must be a finite number from the declared objective")
    timeout = spec.get("timeout_s", 60)
    if type(timeout) not in {float, int} or not math.isfinite(timeout) or not 0 < timeout <= 300:
        raise ValueError("consumer timeout_s must be finite, positive, and at most 300 seconds")
    return spec


def _check_deadline(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise DeadlineExhausted("consumer profile deadline exhausted")


def _digest(path: Path, deadline: float | None = None) -> str:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while True:
            _check_deadline(deadline)
            chunk = source.read(1024 * 1024)
            if not chunk:
                return digest.hexdigest()
            size += len(chunk)
            if size > _MAX_BYTES:
                raise ValueError("consumer artifact exceeded the staging size limit while reading")
            digest.update(chunk)


def _artifact_files(
    root: Path, paths: list[str], *, deadline: float, authorize: Callable[[str], Any] | None
) -> dict[str, Path]:
    files: dict[str, Path] = {}
    total = 0
    for raw in paths:
        _check_deadline(deadline)
        relative = _relative(raw)
        source = root / relative
        if source.is_symlink() or not source.exists() or not source.resolve().is_relative_to(root):
            raise ValueError(
                f"consumer artifact is missing, linked, or outside the workspace: {raw}"
            )
        entries = source.rglob("*") if source.is_dir() else [source]
        for item in entries:
            _check_deadline(deadline)
            if item.is_symlink() or not item.resolve().is_relative_to(root):
                raise ValueError(
                    f"consumer artifacts cannot contain linked paths: {item.relative_to(root)}"
                )
            if not item.is_file():
                continue
            key = str(item.relative_to(root))
            if key in files:
                continue
            if authorize is not None:
                authorize(key)
            elif classify_sensitive_path(key).sensitive:
                raise PermissionError(
                    f"sensitive consumer artifact requires explicit read authorization: {key}"
                )
            _check_deadline(deadline)
            total += item.stat().st_size
            if total > _MAX_BYTES or len(files) >= _MAX_FILES:
                raise ValueError("consumer artifacts exceed the 64 MiB / 2000-file staging limit")
            files[key] = item
    return files


def _read_result(stage: Path, raw: object) -> Path:
    path = stage / _relative(raw)
    if path.is_symlink() or not path.resolve().is_relative_to(stage) or not path.is_file():
        raise ValueError("consumer result must be a regular file in the consumer directory")
    if path.stat().st_size > _MAX_RESULT_BYTES:
        raise ValueError("consumer result exceeds the 4 MiB inspection limit")
    return path


def _unchanged(path: Path, expected: str, boundary: Path, deadline: float | None = None) -> bool:
    try:
        return (
            path.is_file()
            and not path.is_symlink()
            and path.resolve().is_relative_to(boundary)
            and _digest(path, deadline) == expected
        )
    except OSError:
        return False


def _content_check(stage: Path, spec: dict[str, Any], deadline: float) -> dict[str, Any]:
    try:
        _check_deadline(deadline)
        path = _read_result(stage, spec["path"])
        if spec["format"] == "bytes":
            expected = str(spec.get("sha256") or "")
            if not re.fullmatch(r"[a-fA-F0-9]{64}", expected):
                raise ValueError("byte content checks require a SHA256 digest")
            passed = _digest(path, deadline) == expected.lower()
        elif spec["format"] == "json":
            value = json.loads(path.read_text(encoding="utf-8"))
            required = spec.get("required_keys", [])
            passed = (
                not required or isinstance(value, dict) and all(key in value for key in required)
            )
            if "expected" in spec:
                passed = passed and value == spec["expected"]
            if "min_items" in spec:
                passed = (
                    passed
                    and isinstance(value, (list, dict))
                    and len(value) >= int(spec["min_items"])
                )
        else:
            with path.open(encoding="utf-8", newline="") as stream:
                reader = csv.DictReader(stream)
                rows = []
                for row in reader:
                    _check_deadline(deadline)
                    if len(rows) >= 100000:
                        raise ValueError("CSV result exceeds the 100000-row inspection limit")
                    rows.append(row)
                passed = all(
                    column in (reader.fieldnames or []) for column in spec.get("columns", [])
                )
            passed = passed and len(rows) >= int(spec.get("min_rows", 1))
            if "expected_rows" in spec:
                passed = passed and rows == spec["expected_rows"]
        _check_deadline(deadline)
        return {"path": spec["path"], "passed": bool(passed), "format": spec["format"]}
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return {"path": spec.get("path"), "passed": False, "detail": str(exc)}


def run_consumer_profile(
    *,
    root: Path,
    spec: dict[str, Any],
    artifact_path: Path,
    cfg: AppConfig,
    timeout_s: float,
    process_group_registry: ProcessGroupRegistry | None = None,
    service_status: Callable[..., dict[str, Any]] | None = None,
    authorize_artifact_read: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """Execute proposed checks without replacing any host/user verification contract."""
    spec = validate_consumer_profile(spec)
    root = root.resolve()
    deadline = time.monotonic() + min(float(spec.get("timeout_s", 60)), timeout_s)
    files = _artifact_files(
        root, spec["artifacts"], deadline=deadline, authorize=authorize_artifact_read
    )
    sensitive_artifacts = any(classify_sensitive_path(name).sensitive for name in files)
    source_hashes = {name: _digest(path, deadline) for name, path in files.items()}
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "profile": spec["profile"],
        "authority": "supplemental",
        "checks": [],
        "artifact_hashes": source_hashes,
        "workers": spec["workers"],
        "fresh_working_directory": True,
        "declared_artifacts_only": True,
        "dependency_environment": "configured runtime; isolated caches and no user Python site/PYTHONPATH",
    }

    def healthy_service() -> bool:
        if spec["profile"] != "service":
            return True
        if service_status is None:
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        payload = service_status(str(spec["service_id"]), timeout_s=min(remaining, 2.0))
        report["service"] = payload
        ready = payload.get("readiness") or {}
        return (
            payload.get("alive") is True
            and payload.get("identity_valid") is True
            and (
                spec.get("service_scope", "endpoint") == "process"
                or ready.get("endpoint_owned") is True
            )
        )

    with (
        tempfile.TemporaryDirectory(prefix="alysis-consumer-") as temporary,
        _ConsumerProcessRegistry(process_group_registry) as consumer_registry,
    ):
        stage = Path(temporary).resolve()
        for name, source in files.items():
            _check_deadline(deadline)
            target = stage / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            _check_deadline(deadline)
        (stage / ".consumer-launch.py").write_text(_LAUNCHER, encoding="utf-8")
        runtime_hashes = {".consumer-launch.py": _digest(stage / ".consumer-launch.py", deadline)}
        python = sys.executable if resolve_verify_sandbox_mode(cfg) == "off" else "python3"
        quote = (
            subprocess.list2cmdline
            if os.name == "nt" and resolve_verify_sandbox_mode(cfg) == "off"
            else shlex.join
        )
        checks = list(spec["checks"])
        if spec.get("python_module"):
            import_code = (
                "import importlib, sys; from pathlib import Path\n"
                f"roots = {spec['import_roots']!r}\n"
                "assert all((Path.cwd() / p).resolve().is_relative_to(Path.cwd()) for p in roots)\n"
                "sys.path[:0] = [str((Path.cwd() / p).resolve()) for p in roots]\n"
                "before = {str(p.relative_to(Path.cwd())) for p in Path.cwd().rglob('*') if p.is_file()}\n"
                f"module = importlib.import_module({spec['python_module']!r})\n"
                "origins = [module.__file__] if getattr(module, '__file__', None) else list(module.__path__)\n"
                "assert origins and all(Path(p).resolve().is_relative_to(Path.cwd()) for p in origins), 'import used an unstaged installed/source copy'\n"
                "after = {str(p.relative_to(Path.cwd())) for p in Path.cwd().rglob('*') if p.is_file()}\n"
                "assert after == before, 'library import created or removed consumer files'\n"
                "print('consumer import completed')\n"
            )
            (stage / ".consumer-import.py").write_text(import_code, encoding="utf-8")
            runtime_hashes[".consumer-import.py"] = _digest(stage / ".consumer-import.py", deadline)
            checks.append(
                {
                    "name": "fresh-library-import",
                    "kind": "import",
                    "command": quote([python, ".consumer-import.py"]),
                    "expected_exit": 0,
                    "stdout_contains": ["consumer import completed"],
                }
            )

        def _run_check(
            index: int, check: dict[str, Any], rank: int, workers: int
        ) -> dict[str, Any]:
            remaining = deadline - time.monotonic()
            receipt = {
                "name": check["name"],
                "kind": check["kind"],
                "command": check["command"],
                "rank": rank,
                "world_size": workers,
            }
            if remaining <= 0:
                return {
                    **receipt,
                    "passed": False,
                    "executed": False,
                    "detail": "consumer deadline exhausted",
                }
            if not _unchanged(
                stage / ".consumer-launch.py",
                runtime_hashes[".consumer-launch.py"],
                stage,
                deadline,
            ):
                return {
                    **receipt,
                    "passed": False,
                    "executed": False,
                    "detail": "consumer launcher was modified",
                }
            payload_name = f".consumer-check-{index}-{rank}.json"
            (stage / payload_name).write_text(
                json.dumps(
                    {
                        "command": check["command"],
                        "rank": rank,
                        "workers": workers,
                        "blocked_env": sorted(
                            set(_SENSITIVE_ENV_KEYS)
                            | {"PYTHONPATH", "PYTHONHOME", "NODE_PATH", "CLASSPATH"}
                        ),
                    }
                ),
                encoding="utf-8",
            )
            runtime_hashes[payload_name] = _digest(stage / payload_name, deadline)
            _check_deadline(deadline)
            result = run_task_verification(
                root=stage,
                commands=[quote([python, ".consumer-launch.py", payload_name])],
                artifact_path=(
                    stage / f".consumer-output-{index}-{rank}.txt"
                    if sensitive_artifacts
                    else artifact_path.with_name(f"{artifact_path.stem}-{index}-{rank}.txt")
                ),
                cfg=cfg,
                timeout_s=deadline - time.monotonic(),
                process_group_registry=consumer_registry,
            )
            if not result.command_results:
                return {
                    **receipt,
                    "passed": False,
                    "executed": False,
                    "detail": "consumer command was not run",
                }
            observed = result.command_results[0]
            executed = observed.real_execution is not False
            passed = (
                executed
                and observed.exit_code == check["expected_exit"]
                and all(text in observed.stdout for text in check["stdout_contains"])
            )
            return {
                **receipt,
                "passed": passed,
                "executed": executed,
                "exit_code": observed.exit_code,
                "output": "[sensitive consumer output redacted]"
                if sensitive_artifacts
                else observed.output[-2000:],
            }

        def run_check(index: int, check: dict[str, Any], rank: int, workers: int) -> dict[str, Any]:
            try:
                return _run_check(index, check, rank, workers)
            except DeadlineExhausted:
                return {
                    "name": check["name"],
                    "rank": rank,
                    "world_size": workers,
                    "passed": False,
                    "executed": False,
                    "detail": "consumer deadline exhausted",
                }

        before_service = healthy_service()
        if before_service:
            for index, check in enumerate(checks):
                workers = spec["workers"] if check["kind"] != "setup" else 1
                if workers == 1:
                    receipts = [run_check(index, check, 0, 1)]
                else:
                    with ThreadPoolExecutor(max_workers=workers) as pool:
                        receipts = list(
                            pool.map(
                                lambda rank, index=index, check=check, workers=workers: run_check(
                                    index, check, rank, workers
                                ),
                                range(workers),
                            )
                        )
                report["checks"].extend(receipts)
                if check["kind"] == "setup" and not all(item["passed"] for item in receipts):
                    break
        report["result_checks"] = []
        report["runtime_metadata_unchanged"] = False
        report["preserved_artifacts"] = False
        report["original_artifacts_unchanged"] = False
        after_service = False
        try:
            _check_deadline(deadline)
            for item in spec.get("result_checks", []):
                report["result_checks"].append(_content_check(stage, item, deadline))
            if spec["profile"] == "optimization":
                metric = spec["metric"]
                try:
                    _check_deadline(deadline)
                    value = json.loads(
                        _read_result(stage, metric["path"]).read_text(encoding="utf-8")
                    )[metric["key"]]
                    _check_deadline(deadline)
                    finite = type(value) in {int, float} and math.isfinite(value)
                    better = finite and (
                        value <= metric["baseline"]
                        if metric["direction"] == "minimize"
                        else value >= metric["baseline"]
                    )
                    report["metric"] = {
                        "value": value if finite else None,
                        "baseline": metric["baseline"],
                        "direction": metric["direction"],
                        "passed": bool(better),
                    }
                except (OSError, ValueError, TypeError, KeyError) as exc:
                    report["metric"] = {"passed": False, "detail": str(exc)}
            report["runtime_metadata_unchanged"] = all(
                _unchanged(stage / name, expected, stage, deadline)
                for name, expected in runtime_hashes.items()
            )
            report["preserved_artifacts"] = all(
                _unchanged(stage / name, expected, stage, deadline)
                for name, expected in source_hashes.items()
            )
            report["original_artifacts_unchanged"] = all(
                _unchanged(path, source_hashes[name], root, deadline)
                for name, path in files.items()
            )
            _check_deadline(deadline)
            after_service = healthy_service()
            _check_deadline(deadline)
        except DeadlineExhausted:
            report["deadline_exhausted"] = True
            report["result_checks"].append(
                {
                    "passed": False,
                    "detail": "consumer deadline exhausted; remaining observations unavailable",
                }
            )
        if spec["profile"] == "service":
            report["service_scope"] = spec.get("service_scope", "endpoint")
            report["service_identity_before_and_after"] = before_service and after_service
            if report["service_scope"] == "endpoint":
                report["service_owned_before_and_after"] = before_service and after_service
    report["passed"] = (
        bool(report["checks"])
        and all(item["passed"] for item in report["checks"])
        and all(item["passed"] for item in report["result_checks"])
        and report["preserved_artifacts"]
        and report["runtime_metadata_unchanged"]
        and report["original_artifacts_unchanged"]
        and report.get("service_identity_before_and_after", True)
        and report.get("metric", {}).get("passed", True)
    )
    report["status"] = "passed" if report["passed"] else "failed"
    report["result_id"] = (
        "sha256:" + hashlib.sha256(json.dumps(report, sort_keys=True).encode()).hexdigest()
    )
    persisted_report = (
        {
            "status": report["status"],
            "profile": report["profile"],
            "sensitive": True,
            "detail": "Sensitive consumer evidence omitted from persistent logs.",
        }
        if sensitive_artifacts
        else report
    )
    artifact_path.write_text(
        json.dumps(persisted_report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return {
        "consumer_profile_report": report,
        "status": report["status"],
        "profile_passed": report["passed"],
        "verification_evidence_allowed": False,
        "verification_evidence_supplemental_only": True,
        "artifact_path": str(artifact_path),
        "note": "Consumer observations supplement existing requirements; passing this profile does not establish whole-task verification.",
    }
