"""Real local lifecycle adapter. Docker isolation is development-only.

No host mounts, credentials, ports, image pulls or network. Stop/start preserves
the container filesystem but restarts processes; it is not VM memory suspend.
"""

from __future__ import annotations

import json
import os
import re
import selectors
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path, PurePosixPath

from .compute import (
    TRANSFER_CHUNK_BYTES,
    TRANSFER_SECONDS,
    CommandReceipt,
    ComputeError,
    ComputerIdentity,
    ComputerRef,
    ComputerSpec,
    ComputerStatus,
    ComputeUncertain,
    owned,
    transfer_checkpoint,
    transfer_chunks,
    validate_command,
    validate_transfer,
)


class DockerComputers:
    def __init__(self, context: str, *, namespace_profile: Path | None = None):
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", context):
            raise ValueError("Choose an explicit Docker context.")
        self.scope = context
        self._namespace_options = []
        if namespace_profile is not None:
            # Explicit operator opt-in to the existing lab's reviewed nested
            # namespace profile. Never granted by a model or used as a fallback.
            profile = namespace_profile.resolve(strict=True)
            if not profile.is_file() or not isinstance(json.loads(profile.read_text()), dict):
                raise ValueError("Use the reviewed local namespace profile.")
            self._namespace_options = [
                "--security-opt",
                "seccomp=" + str(profile),
                "--security-opt",
                "systempaths=unconfined",
            ]

    def _call(
        self,
        args,
        *,
        timeout=30,
        mutation=False,
        cancel=lambda: False,
        stdin=None,
        output_file=None,
        output_limit=2 * 1024 * 1024,
    ):
        error = ComputeUncertain if mutation else ComputeError
        argv = ["docker", "--context", self.scope, *args]
        try:
            with subprocess.Popen(
                argv, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            ) as process:
                buffers = {process.stdout: bytearray(), process.stderr: bytearray()}
                deadline = time.monotonic() + timeout
                written = 0
                try:
                    with selectors.DefaultSelector() as reader:
                        for pipe in buffers:
                            reader.register(pipe, selectors.EVENT_READ)
                        while reader.get_map():
                            transfer_checkpoint(cancel, deadline, error)
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                raise subprocess.TimeoutExpired(argv, timeout)
                            for key, _ in reader.select(min(0.25, remaining)):
                                chunk = os.read(key.fd, 65536)
                                if not chunk:
                                    reader.unregister(key.fileobj)
                                    continue
                                if key.fileobj is process.stdout and output_file is not None:
                                    written += len(chunk)
                                    if written > output_limit:
                                        raise error("Docker file stream exceeded its allowance.")
                                    output_file.write(chunk)
                                    continue
                                if (
                                    sum(len(b) for b in buffers.values()) + len(chunk)
                                    > 2 * 1024 * 1024
                                ):
                                    raise error(
                                        "Docker receipt exceeded its allowance; outcome not confirmed."
                                    )
                                buffers[key.fileobj].extend(chunk)
                    while process.poll() is None:
                        transfer_checkpoint(cancel, deadline, error)
                        try:
                            process.wait(timeout=0.1)
                        except subprocess.TimeoutExpired:
                            pass
                    transfer_checkpoint(cancel, deadline, error)
                    code = process.returncode
                    result = subprocess.CompletedProcess(
                        argv, code, bytes(buffers[process.stdout]), bytes(buffers[process.stderr])
                    )
                except BaseException:
                    process.kill()
                    process.wait()
                    raise
        except subprocess.TimeoutExpired:
            raise error(
                "Docker request timed out; inspect resource state before continuing."
            ) from None
        except OSError:
            raise error("Docker transport is unavailable; resource state is unconfirmed.") from None
        return result

    def upload(self, ref, guest_path, chunks, *, size, cancel=lambda: False):
        """Stage exact bytes privately, then copy once with worker ownership."""
        validate_transfer(guest_path, size)
        deadline = time.monotonic() + TRANSFER_SECONDS

        def check():
            return transfer_checkpoint(cancel, deadline)

        check()
        if self.status(ref).state != "running":
            raise ComputeError("Start the owned computer explicitly before transfer.")
        try:
            # TemporaryFile is private (0600); neither archive nor payload is
            # extracted onto the controller filesystem or retained after use.
            with tempfile.TemporaryFile() as payload, tempfile.TemporaryFile() as archive:
                for chunk in transfer_chunks(chunks, size, check):
                    payload.write(chunk)
                payload.seek(0)
                path = PurePosixPath(guest_path)
                with tarfile.open(fileobj=archive, mode="w") as tar:
                    info = tarfile.TarInfo(path.name)
                    info.size, info.mode, info.uid, info.gid = size, 0o600, 1000, 1000
                    tar.addfile(info, payload)
                archive.seek(0)
                check()
                if self.status(ref).state != "running":
                    raise ComputeError("Computer state changed while staging the transfer.")
                check()

                # Existing parents keep their mode. These are trusted staging
                # paths, not model-selected workspace paths or a path sandbox.
                def mutation_check():
                    return transfer_checkpoint(cancel, deadline, ComputeUncertain)

                for args, input_file in (
                    (
                        [
                            "exec",
                            ref.resource_id,
                            "mkdir",
                            "-p",
                            "-m",
                            "700",
                            "--",
                            str(path.parent),
                        ],
                        None,
                    ),
                    (["cp", "-a", "-", f"{ref.resource_id}:{path.parent}"], archive),
                ):
                    mutation_check()
                    result = self._call(
                        args,
                        timeout=max(0.001, deadline - time.monotonic()),
                        mutation=True,
                        cancel=cancel,
                        stdin=input_file,
                    )
                    if result.returncode:
                        raise ComputeUncertain(
                            "Docker upload was not confirmed; no automatic retry."
                        )
                mutation_check()
        except ComputeError:
            raise
        except Exception:
            raise ComputeUncertain(
                "Docker upload failed; reconcile partial data before retrying."
            ) from None

    def download(self, ref, guest_path, *, size, cancel=lambda: False):
        """Validate a bounded one-file archive without extracting host paths.

        Only successful exhaustion confirms a complete file. Close abandoned
        iterators promptly to release the private staging file.
        """
        validate_transfer(guest_path, size)
        deadline = time.monotonic() + TRANSFER_SECONDS

        def check():
            return transfer_checkpoint(cancel, deadline)

        check()
        if self.status(ref).state != "running":
            raise ComputeError("Start the owned computer explicitly before transfer.")
        check()
        try:
            with tempfile.TemporaryFile() as archive:
                result = self._call(
                    ["cp", f"{ref.resource_id}:{guest_path}", "-"],
                    timeout=max(0.001, deadline - time.monotonic()),
                    cancel=cancel,
                    output_file=archive,
                    output_limit=size + 1024 * 1024,
                )
                check()
                if result.returncode:
                    raise ComputeError("Docker download was not confirmed.")
                archive.seek(0)
                with tarfile.open(fileobj=archive, mode="r:") as tar:
                    members = tar.getmembers()
                    if (
                        len(members) != 1
                        or not members[0].isreg()
                        or members[0].size != size
                        or members[0].sparse is not None
                    ):
                        raise ComputeError(
                            "Docker download must contain exactly one regular file of the declared size."
                        )
                    with tar.extractfile(members[0]) as payload:
                        chunks = iter(lambda: payload.read(TRANSFER_CHUNK_BYTES), b"")
                        yield from transfer_chunks(chunks, size, check)
        except ComputeError:
            raise
        except Exception:
            raise ComputeError(
                "Docker download failed; discard partial data before retrying."
            ) from None

    def _inspect(self, resource_id):
        result = self._call(["inspect", "--type", "container", resource_id])
        if result.returncode:
            # Do not mistake a failed daemon or permission denial for deletion.
            if b"No such container:" in result.stderr or b"No such object:" in result.stderr:
                return None
            raise ComputeError("Docker could not establish computer state.")
        try:
            values = json.loads(result.stdout)
            if len(values) != 1:
                raise ValueError
            if not isinstance(values[0], dict):
                raise ValueError
            return values[0]
        except (ValueError, TypeError):
            raise ComputeError("Invalid Docker inspection receipt.") from None

    def _check(self, ref):
        if (
            ref.provider != "docker"
            or ref.scope != self.scope
            or not re.fullmatch(r"[a-f0-9]{64}", ref.resource_id)
        ):
            raise ComputeError("Computer belongs to another provider or scope.")
        detail = self._inspect(ref.resource_id)
        if detail is not None:
            owned(detail.get("Config", {}).get("Labels"), ref.identity)
        return detail

    def create(self, identity: ComputerIdentity, spec: ComputerSpec) -> ComputerRef:
        if spec.internet:
            raise ComputeError("This local provider only qualifies offline computers.")
        name = "alysis-compute-" + identity.allocation_id
        if self._inspect(name) is not None:
            raise ComputeError(
                "Allocation already exists; reconcile it instead of creating another."
            )
        # Immutable local image ID prevents a later tag change from silently
        # changing what the operator prepared. --pull=never prevents downloads.
        image = self._call(["image", "inspect", "--format", "{{.Id}}", spec.image])
        image_id = image.stdout.decode().strip()
        if image.returncode or not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id):
            raise ComputeError("The prepared local image is unavailable.")
        labels = [
            part for key, value in identity.labels.items() for part in ("--label", f"{key}={value}")
        ]
        result = self._call(
            [
                "create",
                "--pull=never",
                "--name",
                name,
                *labels,
                "--network",
                "none",
                "--user",
                "1000:1000",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                *self._namespace_options,
                "--pids-limit",
                "512" if self._namespace_options else "128",
                "--cpus",
                str(spec.cpu),
                "--memory",
                f"{spec.memory_mib}m",
                "--memory-swap",
                f"{spec.memory_mib}m",
                "--init",
                "--log-driver",
                "none",
                "--entrypoint",
                "/bin/sh",
                image_id,
                "-c",
                "exec sleep infinity",
            ],
            mutation=True,
        )
        if result.returncode:
            raise ComputeUncertain(
                "Creation did not return a resource; reconcile the allocation name."
            )
        ref = ComputerRef("docker", self.scope, result.stdout.decode().strip(), identity)
        try:
            self._check(ref)
            self.start(ref)
        except ComputeError:
            raise ComputeUncertain(
                "Docker creation was dispatched; reconcile its allocation and state."
            ) from None
        return ref

    def find(self, identity):
        detail = self._inspect("alysis-compute-" + identity.allocation_id)
        if detail is None:
            return None
        ref = ComputerRef("docker", self.scope, detail["Id"], identity)
        self._check(ref)
        return ref

    def status(self, ref):
        detail = self._check(ref)
        if detail is None:
            return ComputerStatus("missing", "missing")
        raw = detail.get("State", {}).get("Status", "unknown")
        if raw == "running":
            state = "running"
        elif raw in ("created", "exited"):
            state = "stopped"
        elif raw == "paused":
            state = "paused"
        elif raw in ("restarting", "removing"):
            state = "transitioning"
        else:
            state = "unknown"
        return ComputerStatus(state, raw)

    def _change(self, ref, operation, expected):
        status = self.status(ref)
        if status.state == expected:
            return status
        required = "stopped" if operation == "start" else "running"
        if status.state != required:
            raise ComputeError("Computer is not in the required lifecycle state.")
        args = [operation, ref.resource_id]
        if operation == "stop":
            args = ["stop", "--time", "5", ref.resource_id]
        result = self._call(args, mutation=True)
        if result.returncode:
            raise ComputeUncertain(
                "Lifecycle operation was not confirmed; inspect before continuing."
            )
        status = self._after_change(ref)
        if status.state != expected:
            raise ComputeUncertain("Lifecycle target state was not observed.")
        return status

    def _after_change(self, ref):
        try:
            return self.status(ref)
        except ComputeError:
            raise ComputeUncertain(
                "Docker operation was dispatched but its resulting state is unconfirmed."
            ) from None

    def start(self, ref):
        return self._change(ref, "start", "running")

    def stop(self, ref):
        return self._change(ref, "stop", "stopped")

    def delete(self, ref):
        status = self.status(ref)
        if status.state == "missing":
            return status
        if status.state != "stopped":
            raise ComputeError("Stop the owned computer before deleting it.")
        result = self._call(["rm", ref.resource_id], mutation=True)
        if result.returncode or self._after_change(ref).state != "missing":
            raise ComputeUncertain("Computer deletion was not confirmed.")
        return ComputerStatus("missing", "missing")

    def run(self, ref, argv, *, timeout=30):
        validate_command(argv, timeout)
        if self.status(ref).state != "running":
            raise ComputeError("Start the computer explicitly before execution.")
        result = self._call(["exec", ref.resource_id, *argv], timeout=timeout, mutation=True)
        if result.returncode in (125, 126, 127) or result.returncode < 0:
            raise ComputeUncertain("Docker execution did not provide an unambiguous guest receipt.")
        return CommandReceipt(
            result.returncode,
            result.stdout.decode("utf-8", "replace"),
            result.stderr.decode("utf-8", "replace"),
        )
