"""Azure sandbox data-plane adapter for the private pilot.

Wire contract checked against Microsoft's azure-containerapps-sandbox 0.1.0b4,
API 2026-02-01-preview. Azure-specific paths, auth and image IDs stay here.
No resource group, registry, role assignment, ingress or account is created.
This adapter has transport tests; actual Azure qualification is still required.
"""

from __future__ import annotations

import json
import math
import re
import shlex
import time
from collections.abc import Callable
from urllib.parse import quote
from uuid import UUID

import httpx

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

API_VERSION = "2026-02-01-preview"
TOKEN_SCOPE = "https://dynamicsessions.io/.default"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class AzureComputers:
    name = "azure-sandboxes"
    supports_memory_sleep = True

    def __init__(
        self,
        *,
        subscription: str,
        resource_group: str,
        sandbox_group: str,
        region: str,
        access_token: Callable[[], str],
        expires_at: float | None = None,
        transport: httpx.BaseTransport | None = None,
    ):
        subscription = str(UUID(subscription))
        if not re.fullmatch(r"[a-z0-9]{1,40}", region):
            raise ValueError("Use an Azure region identifier.")
        for name in (resource_group, sandbox_group):
            if not re.fullmatch(r"[A-Za-z0-9_.()-]{1,90}", name):
                raise ValueError("Invalid Azure group name.")
        self._endpoint = f"https://management.{region}.azuredevcompute.io"
        self._path = (
            f"/subscriptions/{subscription}/resourceGroups/{resource_group}"
            f"/sandboxGroups/{sandbox_group}/sandboxes"
        )
        self.scope = self._endpoint + self._path
        if expires_at is not None and (
            type(expires_at) not in (int, float) or not math.isfinite(expires_at)
        ):
            raise ValueError("Computer expiry must be a finite UTC epoch timestamp.")
        self.expires_at = expires_at
        self._token = access_token
        self._client = httpx.Client(transport=transport, follow_redirects=False, trust_env=False)

    def close(self):
        self._client.close()

    def _request(self, method, path, *, body=None, params=None, timeout=30):
        mutation = method != "GET"
        error = ComputeUncertain if mutation else ComputeError
        headers = self._authorization()
        deadline = time.monotonic() + timeout
        try:
            with self._client.stream(
                method,
                self._endpoint + path,
                headers=headers,
                params={"api-version": API_VERSION, **(params or {})},
                json=body,
                timeout=timeout,
            ) as response:
                if method == "GET" and response.status_code == 404:
                    return None
                if not 200 <= response.status_code < 300:
                    raise error(f"Azure returned HTTP {response.status_code}; no automatic retry.")
                data = bytearray()
                for chunk in response.iter_bytes():
                    if time.monotonic() >= deadline:
                        raise error("Azure receipt deadline expired; outcome not confirmed.")
                    if len(data) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise error("Azure receipt exceeded its allowance; outcome not confirmed.")
                    data.extend(chunk)
                if not data:
                    return {}
                value = json.loads(data)
                if not isinstance(value, (dict, list)):
                    raise ValueError
                return value
        except (httpx.HTTPError, ValueError):
            raise error(
                "Azure request failed or returned an invalid receipt; reconcile before retrying."
            ) from None

    def _authorization(self):
        try:
            token = self._token()
        except Exception:
            raise ComputeError(
                "Azure authentication is unavailable; no request was sent."
            ) from None
        if not isinstance(token, str) or not token or any(c.isspace() for c in token):
            raise ComputeError("Azure authentication returned an invalid token.")
        return {"Authorization": "Bearer " + token}

    def upload(self, ref, guest_path, chunks, *, size, cancel=lambda: False):
        validate_transfer(guest_path, size)
        deadline = time.monotonic() + TRANSFER_SECONDS
        transfer_checkpoint(cancel, deadline)
        if self.status(ref).state != "running":
            raise ComputeError("Start the owned computer explicitly before transfer.")
        headers = {
            **self._authorization(),
            "Content-Type": "application/octet-stream",
            "Content-Length": str(size),
            "Accept-Encoding": "identity",
        }
        transfer_checkpoint(cancel, deadline)

        def check():
            return transfer_checkpoint(cancel, deadline, ComputeUncertain)

        complete = False

        def source():
            nonlocal complete
            yield from transfer_chunks(chunks, size, check, ComputeUncertain)
            complete = True

        body = source()
        try:
            with self._client.stream(
                "PUT",
                self._endpoint + self._resource_path(ref) + "/files",
                params={"api-version": API_VERSION, "path": guest_path, "createDirs": "true"},
                headers=headers,
                content=body,
                timeout=10,
            ) as response:
                check()
                if response.status_code not in (200, 201, 204):
                    raise ComputeUncertain("Azure upload was not confirmed; no automatic retry.")
                if not complete:
                    raise ComputeUncertain("Azure upload source was not fully consumed.")
                count = 0
                for chunk in response.iter_raw(chunk_size=TRANSFER_CHUNK_BYTES):
                    check()
                    count += len(chunk)
                    if count > MAX_RESPONSE_BYTES:
                        raise ComputeUncertain("Azure upload receipt exceeded its allowance.")
                check()
        except ComputeError:
            raise
        except Exception:
            raise ComputeUncertain(
                "Azure upload failed; reconcile partial data before retrying."
            ) from None
        finally:
            body.close()

    def download(self, ref, guest_path, *, size, cancel=lambda: False):
        """Only successful exhaustion confirms a complete file; close if abandoned."""
        validate_transfer(guest_path, size)
        deadline = time.monotonic() + TRANSFER_SECONDS

        def check():
            return transfer_checkpoint(cancel, deadline)

        check()
        if self.status(ref).state != "running":
            raise ComputeError("Start the owned computer explicitly before transfer.")
        headers = {**self._authorization(), "Accept-Encoding": "identity"}
        check()
        try:
            with self._client.stream(
                "GET",
                self._endpoint + self._resource_path(ref) + "/files",
                params={"api-version": API_VERSION, "path": guest_path},
                headers=headers,
                timeout=10,
            ) as response:
                check()
                if (
                    response.status_code != 200
                    or response.headers.get("Content-Encoding", "identity") != "identity"
                ):
                    raise ComputeError(
                        "Azure download did not return a complete identity-encoded file."
                    )
                length = response.headers.get("Content-Length")
                if length is not None and int(length) != size:
                    raise ComputeError("Azure download length differs from its declared size.")
                yield from transfer_chunks(
                    response.iter_raw(chunk_size=TRANSFER_CHUNK_BYTES), size, check
                )
        except ComputeError:
            raise
        except Exception:
            raise ComputeError(
                "Azure download failed; discard partial data before retrying."
            ) from None

    def _ref(self, resource_id, identity):
        try:
            resource_id = str(UUID(resource_id))
        except (ValueError, TypeError, AttributeError):
            raise ComputeUncertain("Azure did not return a valid resource identity.") from None
        return ComputerRef("azure-sandboxes", self.scope, resource_id, identity)

    def _resource_path(self, ref):
        if ref.provider != "azure-sandboxes" or ref.scope != self.scope:
            raise ComputeError("Computer belongs to another provider or scope.")
        try:
            valid = str(UUID(ref.resource_id)) == ref.resource_id
        except (ValueError, TypeError, AttributeError):
            valid = False
        if not valid:
            raise ComputeError("Invalid Azure resource identity.")
        return self._path + "/" + quote(ref.resource_id, safe="")

    def _get(self, ref):
        detail = self._request("GET", self._resource_path(ref))
        if detail is not None:
            if not isinstance(detail, dict):
                raise ComputeError("Azure returned an invalid resource receipt.")
            if detail.get("id") != ref.resource_id:
                raise ComputeError("Azure returned a different resource identity.")
            owned(detail.get("labels"), ref.identity)
        return detail

    def find(self, identity: ComputerIdentity):
        # An uncertain create is reconciled using the original allocation ID.
        # Fail on pagination rather than silently accepting an incomplete list.
        detail = self._request(
            "GET",
            self._path,
            params={"labels": ",".join(f"{k}={v}" for k, v in identity.labels.items())},
        )
        if isinstance(detail, list):
            values = detail
        elif (
            isinstance(detail, dict)
            and not detail.get("nextLink")
            and isinstance(detail.get("value"), list)
        ):
            values = detail["value"]
        else:
            raise ComputeError("Azure allocation inventory was incomplete.")
        if not values:
            return None
        if len(values) != 1 or not isinstance(values[0], dict):
            raise ComputeError("Allocation inventory is ambiguous; inspect before continuing.")
        owned(values[0].get("labels"), identity)
        return self._ref(values[0].get("id"), identity)

    def create(self, identity: ComputerIdentity, spec: ComputerSpec):
        # Operator imports a prepared OCI image separately. No implicit public
        # image/preset or credential-bearing marketplace fallback.
        try:
            disk_id = str(UUID(spec.image))
        except ValueError:
            raise ValueError("Azure requires the imported private disk image ID.") from None
        if self.find(identity) is not None:
            raise ComputeError(
                "Allocation already exists; reconcile it instead of creating another."
            )
        # Re-evaluate after inventory, which may take time. Leave a full request
        # allowance before the operator's cutoff. Never create a computer whose
        # provider-side expiry would outlive the approved unattended window.
        lifetime = 86400
        if self.expires_at is not None:
            lifetime = min(lifetime, math.floor(self.expires_at - time.time() - 60))
            if lifetime < 60:
                raise ComputeError("Computer allocation window has ended; no resource was created.")
        detail = self._request(
            "PUT",
            self._path,
            body={
                "sourcesRef": {"diskImage": {"id": disk_id}},
                "resources": {"cpu": f"{spec.cpu * 1000}m", "memory": f"{spec.memory_mib}Mi"},
                "labels": identity.labels,
                "lifecycle": {
                    "autoSuspendPolicy": {
                        "enabled": True,
                        "interval": spec.idle_seconds,
                        "mode": spec.suspend_mode,
                    },
                    "autoDeletePolicy": {
                        "enabled": spec.suspend_mode != "Memory" or self.expires_at is not None,
                        "deleteIntervalInSeconds": lifetime,
                    },
                },
                "egressPolicy": {"defaultAction": "Allow" if spec.internet else "Deny"},
                "ports": [],
                "connections": [],
                "environment": {},
            },
        )
        if not isinstance(detail, dict):
            raise ComputeUncertain("Azure creation did not return a resource receipt.")
        ref = self._ref(detail.get("id"), identity)
        try:
            owned(detail.get("labels"), identity)
        except ComputeError:
            raise ComputeUncertain(
                "Azure creation returned mismatched ownership; reconcile the allocation."
            ) from None
        # Returning a handle is not a readiness claim. The controller must poll
        # status; it cannot run work until Running is actually observed.
        return ref

    def status(self, ref):
        detail = self._get(ref)
        if detail is None:
            return ComputerStatus("missing", "missing")
        raw = detail.get("state")
        disabled = detail.get("stateDetails") or {}
        if not isinstance(disabled, dict):
            raise ComputeError("Azure returned invalid lifecycle details.")
        if raw == "Running":
            state = "running"
        elif raw == "Stopped" and disabled.get("stoppedReason") != "Disabled":
            state = "stopped"
        elif raw in ("Creating", "Resuming", "Stopping", "Deleting"):
            state = "transitioning"
        else:
            state = "unknown"
        return ComputerStatus(state, raw if isinstance(raw, str) else "unknown")

    def _change(self, ref, operation, target, required):
        status = self.status(ref)
        if status.state == target:
            return status
        if status.state != required:
            raise ComputeError("Computer is not in the required lifecycle state.")
        self._request("POST", self._resource_path(ref) + "/" + operation)
        # Accepted/204 is not a completed transition. Return observed state.
        return self._after_change(ref)

    def _after_change(self, ref):
        try:
            return self.status(ref)
        except ComputeError:
            raise ComputeUncertain(
                "Azure operation was dispatched but its resulting state is unconfirmed."
            ) from None

    def start(self, ref):
        return self._change(ref, "resume", "running", "stopped")

    def stop(self, ref):
        return self._change(ref, "stop", "stopped", "running")

    def delete(self, ref):
        status = self.status(ref)
        if status.state == "missing":
            return status
        if status.state != "stopped":
            raise ComputeError("Stop the owned computer before deleting it.")
        self._request("DELETE", self._resource_path(ref))
        return self._after_change(ref)

    def open_stream(self, ref, argv, *, valid):
        from websockets.sync.client import connect

        from .compute_azure_stream import AzureComputerStream, AzureExecStream

        validate_command(argv, 15)

        def admitted():
            return valid() and (self.expires_at is None or time.time() < self.expires_at)

        if not admitted() or self.status(ref).state != "running":
            raise ComputeError("Start the owned computer before opening its stream.")
        url = (
            self._endpoint.replace("https:", "wss:")
            + self._resource_path(ref)
            + "/exec/stream?api-version="
            + API_VERSION
        )
        links = []
        try:
            # Two bounded relays on the same owned computer. A slow screen
            # cannot queue ahead of a gesture; input still has one ordered stream.
            for _ in range(2):
                if not admitted():
                    raise ComputeUncertain("Computer stream ownership ended.")
                websocket = connect(
                    url,
                    additional_headers=self._authorization(),
                    proxy=None,
                    open_timeout=10,
                    close_timeout=2,
                    max_size=6 * 1024 * 1024,
                    max_queue=2,
                    compression=None,
                )
                link = AzureExecStream(websocket, ref.identity.allocation_id, admitted)
                links.append(link)
                link.start(argv)
            return AzureComputerStream(*links)
        except Exception:
            for link in links:
                link.close()
            raise ComputeUncertain("Azure computer stream could not be established.") from None

    def run(self, ref, argv, *, timeout=30):
        validate_command(argv, timeout)
        if self.expires_at is not None and time.time() >= self.expires_at:
            raise ComputeError("The approved computer window has ended.")
        if self.status(ref).state != "running":
            raise ComputeError("Start the computer explicitly before execution.")
        value = self._request(
            "POST",
            self._resource_path(ref) + "/executeShellCommand",
            body={"command": shlex.join(argv)},
            timeout=timeout,
        )
        # SDK 0.1.0b4 defaults a missing exitCode to 0. Require the actual field;
        # lost/partial replies cannot become success or trigger command replay.
        if (
            not isinstance(value, dict)
            or type(value.get("exitCode")) is not int
            or not isinstance(value.get("stdout"), str)
            or not isinstance(value.get("stderr"), str)
        ):
            raise ComputeUncertain("Azure command has no complete execution receipt.")
        return CommandReceipt(value["exitCode"], value["stdout"], value["stderr"])
