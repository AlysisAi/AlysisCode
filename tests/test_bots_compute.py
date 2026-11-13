from dataclasses import replace
from uuid import uuid4

import httpx
import pytest

from alysis_code.bots.compute import (
    ComputeError,
    ComputerIdentity,
    ComputerRef,
    ComputerSpec,
    ComputerStatus,
    ComputeUncertain,
    validate_command,
    wait_for,
)
from alysis_code.bots.compute_azure import AzureComputers
from alysis_code.bots.compute_docker import DockerComputers


def identity():
    return ComputerIdentity("synthetic-owner", "synthetic-bot", uuid4().hex)


class AzureWire:
    def __init__(self):
        self.identity = identity()
        self.id = str(uuid4())
        self.requests = []
        self.state = "Running"
        self.labels = self.identity.labels
        self.exec_reply = {"exitCode": 0, "stdout": "Ω 日本語", "stderr": ""}
        self.drop = False
        self.list_reply = {"value": []}
        self.provider = AzureComputers(
            subscription=str(uuid4()),
            resource_group="test-rg",
            sandbox_group="test-group",
            region="swedencentral",
            access_token=lambda: "synthetic-test-token",
            transport=httpx.MockTransport(self.handle),
        )
        self.ref = ComputerRef("azure-sandboxes", self.provider.scope, self.id, self.identity)

    def detail(self):
        return {"id": self.id, "state": self.state, "labels": self.labels}

    def handle(self, request):
        self.requests.append(request)
        assert request.headers["Authorization"] == "Bearer synthetic-test-token"
        if request.url.path.endswith("/executeShellCommand"):
            if self.drop:
                raise httpx.ReadError("sensitive upstream text", request=request)
            return httpx.Response(200, json=self.exec_reply)
        if request.url.path.endswith("/stop"):
            self.state = "Stopping"
            return httpx.Response(204)
        if request.url.path.endswith("/resume"):
            self.state = "Resuming"
            return httpx.Response(204)
        if request.method == "DELETE":
            self.state = "Deleting"
            return httpx.Response(204)
        if request.method == "PUT":
            import json
            import re

            # Real Azure rejects label values longer than 63 characters.
            labels = json.loads(request.content)["labels"]
            assert all(re.fullmatch(r"[a-z0-9-]{1,63}", v) for v in labels.values())
            self.state = "Creating"
            return httpx.Response(200, json=self.detail())
        if request.url.path.endswith("/sandboxes"):
            return httpx.Response(200, json=self.list_reply)
        return httpx.Response(200, json=self.detail())


def test_cloud_creates_only_private_owned_disk_restart_computer():
    import json

    wire = AzureWire()
    disk_id = str(uuid4())
    ref = wire.provider.create(wire.identity, ComputerSpec(disk_id))
    assert ref.resource_id == wire.id
    body = json.loads(wire.requests[-1].content)
    assert body["sourcesRef"] == {"diskImage": {"id": disk_id}}
    assert body["labels"] == wire.identity.labels
    assert body["egressPolicy"] == {"defaultAction": "Deny"}
    assert body["ports"] == body["connections"] == []
    assert body["environment"] == {}
    assert body["lifecycle"]["autoSuspendPolicy"]["mode"] == "Disk"
    assert wire.provider.status(ref).state == "transitioning"


def test_owner_labels_preserve_identity_without_exposing_unicode_names():
    original = ComputerIdentity("Ιδιοκτήτης" * 20, "東京" * 100, uuid4().hex)
    assert original.labels == replace(original).labels
    assert (
        original.labels["alysis-owner"]
        != replace(original, bot_id="別のボット").labels["alysis-owner"]
    )
    assert (
        original.labels["alysis-owner"]
        != replace(original, owner_id="another").labels["alysis-owner"]
    )
    assert all(value.isascii() and len(value) <= 63 for value in original.labels.values())


def test_cloud_creation_sets_provider_expiry_and_refuses_after_cutoff(monkeypatch):
    import json

    wire = AzureWire()
    wire.provider.expires_at = 2000
    clock = [1000]
    monkeypatch.setattr("alysis_code.bots.compute_azure.time.time", lambda: clock[0])
    original = wire.provider.find

    def slow_inventory(identity):
        clock[0] += 30
        return original(identity)

    monkeypatch.setattr(wire.provider, "find", slow_inventory)
    wire.provider.create(wire.identity, ComputerSpec(str(uuid4())))
    body = json.loads(wire.requests[-1].content)
    assert body["lifecycle"]["autoDeletePolicy"] == {
        "enabled": True,
        "deleteIntervalInSeconds": 910,
    }
    clock[0] = 1900
    wire.requests.clear()
    with pytest.raises(ComputeError, match="window has ended"):
        wire.provider.create(wire.identity, ComputerSpec(str(uuid4())))
    assert all(request.method == "GET" for request in wire.requests)


def test_creation_conflict_never_creates_second_computer():
    wire = AzureWire()
    wire.list_reply = {"value": [wire.detail()]}
    with pytest.raises(ComputeError, match="already exists"):
        wire.provider.create(wire.identity, ComputerSpec(str(uuid4())))
    assert all(r.method == "GET" for r in wire.requests)
    assert wire.provider.find(wire.identity) == wire.ref


def test_bad_creation_ownership_receipt_is_uncertain_after_dispatch():
    wire = AzureWire()
    wire.labels = {}
    with pytest.raises(ComputeUncertain, match="reconcile"):
        wire.provider.create(wire.identity, ComputerSpec(str(uuid4())))
    assert len([r for r in wire.requests if r.method == "PUT"]) == 1


@pytest.mark.parametrize("backend", ["azure", "docker"])
def test_failed_readback_after_mutation_is_uncertain(monkeypatch, backend):
    from subprocess import CompletedProcess

    wire = AzureWire()
    provider = wire.provider if backend == "azure" else DockerComputers("desktop-linux")
    dispatches = []
    if backend == "docker":

        def dispatch(*a, **kw):
            dispatches.append(a)
            return CompletedProcess([], 0, b"", b"")

        monkeypatch.setattr(provider, "_call", dispatch)
    observations = 0

    def status(ref):
        nonlocal observations
        observations += 1
        if observations == 1:
            return ComputerStatus("running", "Running")
        raise ComputeError("Readback unavailable")

    monkeypatch.setattr(provider, "status", status)
    with pytest.raises(ComputeUncertain, match="dispatched"):
        provider.stop(wire.ref)
    assert len(dispatches if backend == "docker" else wire.requests) == 1


@pytest.mark.parametrize(
    "reply", [None, {}, {"nextLink": "https://elsewhere.invalid", "value": []}, {"value": [{}, {}]}]
)
def test_incomplete_inventory_cannot_authorize_creation(reply):
    wire = AzureWire()
    wire.list_reply = reply
    with pytest.raises(ComputeError):
        wire.provider.create(wire.identity, ComputerSpec(str(uuid4())))
    assert all(r.method == "GET" for r in wire.requests)


def test_success_requires_actual_command_receipt_and_quotes_arguments():
    import json
    import shlex

    wire = AzureWire()
    argv = ["python3", "-c", "print('Ω 日本語')", "$(touch forbidden); ' literal"]
    result = wire.provider.run(wire.ref, argv)
    assert result.exit_code == 0 and result.stdout == "Ω 日本語"
    assert shlex.split(json.loads(wire.requests[-1].content)["command"]) == argv
    assert "Ω" not in repr(result)


@pytest.mark.parametrize(
    "reply",
    [
        {},
        {"stdout": "ok", "stderr": ""},
        {"exitCode": False, "stdout": "ok", "stderr": ""},
        {"exitCode": "0", "stdout": "ok", "stderr": ""},
        {"exitCode": 0},
    ],
)
def test_partial_receipt_is_uncertain_not_success(reply):
    wire = AzureWire()
    wire.exec_reply = reply
    with pytest.raises(ComputeUncertain):
        wire.provider.run(wire.ref, ["true"])
    assert len([r for r in wire.requests if r.method == "POST"]) == 1


def test_unknown_command_outcome_is_not_replayed_or_exposed():
    wire = AzureWire()
    wire.drop = True
    with pytest.raises(ComputeUncertain) as failure:
        wire.provider.run(wire.ref, ["make-a-change"])
    assert "sensitive" not in str(failure.value)
    assert len([r for r in wire.requests if r.method == "POST"]) == 1


@pytest.mark.parametrize("change", ["owner", "scope", "provider", "resource"])
def test_wrong_identity_is_rejected_before_mutation(change):
    wire = AzureWire()
    ref = wire.ref
    if change == "owner":
        ref = replace(ref, identity=replace(ref.identity, owner_id="someone-else"))
    elif change == "scope":
        ref = replace(ref, scope="https://unrelated.invalid")
    elif change == "provider":
        ref = replace(ref, provider="docker")
    else:
        ref = replace(ref, resource_id="../../escape")
    with pytest.raises(ComputeError):
        wire.provider.run(ref, ["true"])
    assert all(r.method == "GET" for r in wire.requests)


@pytest.mark.parametrize(
    "operation,initial,raw",
    [
        ("stop", "Running", "Stopping"),
        ("start", "Stopped", "Resuming"),
        ("delete", "Stopped", "Deleting"),
    ],
)
def test_accepted_transition_does_not_mean_completed(operation, initial, raw):
    wire = AzureWire()
    wire.state = initial
    result = getattr(wire.provider, operation)(wire.ref)
    assert result == ComputerStatus("transitioning", raw)


def test_stopped_computer_does_not_resume_implicitly_for_command():
    wire = AzureWire()
    wire.state = "Stopped"
    with pytest.raises(ComputeError, match="explicitly"):
        wire.provider.run(wire.ref, ["true"])
    assert all(r.method == "GET" for r in wire.requests)


def test_unknown_lifecycle_never_reports_ready_or_dispatches():
    wire = AzureWire()
    wire.state = "FutureNewState"
    assert wire.provider.status(wire.ref).state == "unknown"
    with pytest.raises(ComputeError):
        wire.provider.start(wire.ref)
    assert all(r.method == "GET" for r in wire.requests)


def test_wait_only_reads_states_and_does_not_repeat_mutation():
    wire = AzureWire()
    assert wait_for(wire.provider, wire.ref, "running").state == "running"
    wire.state = "FutureNewState"
    with pytest.raises(ComputeUncertain):
        wait_for(wire.provider, wire.ref, "running")
    assert all(r.method == "GET" for r in wire.requests)


def test_labels_do_not_contain_owner_or_bot_names():
    value = identity()
    assert value.owner_id not in str(value.labels)
    assert value.bot_id not in str(value.labels)
    assert replace(value, owner_id="other").labels != value.labels


@pytest.mark.parametrize(
    "args,timeout",
    [([], 1), (["true"], 0), (["bad\0arg"], 1), (["x" * 65537], 1), (["true"], True)],
)
def test_invalid_commands_rejected(args, timeout):
    with pytest.raises(ValueError):
        validate_command(args, timeout)


def test_docker_unavailable_is_not_missing(monkeypatch):
    from subprocess import CompletedProcess

    provider = DockerComputers("desktop-linux")
    monkeypatch.setattr(
        provider, "_call", lambda *a, **k: CompletedProcess([], 1, b"", b"Cannot connect to daemon")
    )
    with pytest.raises(ComputeError, match="establish"):
        provider.find(identity())


def test_docker_wrong_owner_cannot_delete(monkeypatch):
    provider = DockerComputers("desktop-linux")
    original = identity()
    ref = ComputerRef("docker", provider.scope, "a" * 64, identity())
    monkeypatch.setattr(provider, "_inspect", lambda _: {"Config": {"Labels": original.labels}})
    with pytest.raises(ComputeError, match="ownership"):
        provider.delete(ref)


def test_azure_plain_list_inventory_is_supported():
    wire = AzureWire()
    wire.list_reply = [wire.detail()]
    assert wire.provider.find(wire.identity) == wire.ref


def test_azure_redirect_does_not_forward_credentials():
    requests = []

    def redirect(request):
        requests.append(request)
        return httpx.Response(307, headers={"location": "https://unrelated.invalid"})

    wire = AzureWire()
    wire.provider._client.close()
    wire.provider._client = httpx.Client(
        transport=httpx.MockTransport(redirect), follow_redirects=False
    )
    with pytest.raises(ComputeError, match="307"):
        wire.provider.status(wire.ref)
    assert len(requests) == 1


def test_azure_large_reply_is_uncertain_and_not_retained():
    wire = AzureWire()
    wire.exec_reply["stdout"] = "x" * (2 * 1024 * 1024)
    with pytest.raises(ComputeUncertain, match="allowance"):
        wire.provider.run(wire.ref, ["true"])


def test_azure_trickling_receipt_does_not_extend_deadline(monkeypatch):
    from alysis_code.bots import compute_azure

    wire = AzureWire()
    ticks = iter([0, 31])
    monkeypatch.setattr(compute_azure.time, "monotonic", lambda: next(ticks))
    with pytest.raises(ComputeUncertain, match="deadline"):
        wire.provider._request("POST", wire.provider._path + "/executeShellCommand")


def test_malformed_azure_lifecycle_blocks_operation():
    wire = AzureWire()
    original = wire.detail
    wire.detail = lambda: {**original(), "stateDetails": ["invalid"]}
    with pytest.raises(ComputeError, match="lifecycle details"):
        wire.provider.stop(wire.ref)
    assert all(r.method == "GET" for r in wire.requests)


def test_disabled_azure_resource_cannot_resume():
    wire = AzureWire()
    wire.state = "Stopped"
    original = wire.detail
    wire.detail = lambda: {**original(), "stateDetails": {"stoppedReason": "Disabled"}}
    with pytest.raises(ComputeError):
        wire.provider.start(wire.ref)
    assert all(r.method == "GET" for r in wire.requests)


@pytest.mark.parametrize(
    "code,timeout,match",
    [
        ("import time;time.sleep(2)", 0.05, "timed out"),
        ("import sys;sys.stdout.write('x'*(3*1024*1024))", 5, "allowance"),
    ],
)
def test_docker_cli_transport_bounds_real_processes(monkeypatch, code, timeout, match):
    import subprocess
    import sys

    original = subprocess.Popen
    monkeypatch.setattr(
        subprocess, "Popen", lambda argv, **kw: original([sys.executable, "-c", code], **kw)
    )
    with pytest.raises(ComputeUncertain, match=match):
        DockerComputers("desktop-linux")._call(["unused"], timeout=timeout, mutation=True)


def test_operator_can_allow_cloud_egress_without_opening_ingress():
    import json

    wire = AzureWire()
    wire.provider.create(wire.identity, ComputerSpec(str(uuid4()), internet=True))
    body = json.loads(next(r for r in wire.requests if r.method == "PUT").content)
    assert body["egressPolicy"] == {"defaultAction": "Allow"}
    assert body["ports"] == []
    assert body["connections"] == []
    assert body["environment"] == {}
    with pytest.raises(ValueError, match="boolean"):
        ComputerSpec("image", internet="yes")
