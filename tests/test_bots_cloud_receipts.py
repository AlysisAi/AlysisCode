"""A Cloud computer's request whose receipt does not arrive whole: what is recorded, and what
happens to the computer. Azure's sandbox API is emulated over httpx's mock transport, so the
facts come from the real adapter (compute_azure) through the real Cloud desktop."""

import base64
import json
import shlex
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from alysis_code.bots import diagnostics
from alysis_code.bots.cloud_desktop import (
    GUEST_PROBE,
    MODULE,
    RECEIPT_EVENT,
    CloudDesktopFactory,
    ComputerReadUnconfirmed,
)
from alysis_code.bots.compute import ComputerSpec, ComputeUncertain
from alysis_code.bots.compute_azure import MAX_RESPONSE_BYTES, AzureComputers
from alysis_code.bots.desktop_linux import ComputerActionUncertain, ComputerReplyTooLarge
from alysis_code.ide.managed_browser import BrowserValidationError

# Content that must never reach a record: a page's text, a URL with a token, an upstream message.
PAGE = "Σελίδα με κείμενο 猫头鹰 <b>secret-page-text</b> & more"
URL = "https://private.example/κουκουβάγια?token=secret-url-token"
UPSTREAM = "upstream says secret-upstream-detail"
SECRETS = ("secret-page-text", "secret-url-token", "secret-upstream-detail", "κουκουβάγια")

READ = ("browser_call", {"method": "snapshot", "args": [], "kwargs": {"kind": "dom"}})
WRITE = ("browser_call", {"method": "navigate", "args": [URL], "kwargs": {}})


def receipt(stdout, *, exit_code=0, stderr=""):
    return httpx.Response(200, json={"exitCode": exit_code, "stdout": stdout, "stderr": stderr})


class AzureCloud:
    """Azure's sandbox API as the adapter sees it: a computer that runs at once, and a guest
    whose answer to each worker request a test chooses (``answer``)."""

    def __init__(self):
        self.id = str(uuid4())
        self.state = None
        self.labels = None
        self.requests = []
        self.probes = 0
        self.probe = {"oom_kills": 3, "mem_total_kib": 2_000_000, "mem_available_kib": 41_000}
        self.answer = None
        self.stdout = None
        self.provider = AzureComputers(
            subscription=str(uuid4()),
            resource_group="test-rg",
            sandbox_group="test-group",
            region="swedencentral",
            access_token=lambda: "synthetic-test-token",
            transport=httpx.MockTransport(self.handle),
        )

    def detail(self):
        return {"id": self.id, "state": self.state, "labels": self.labels}

    def handle(self, request):
        path = request.url.path
        if path.endswith("/executeShellCommand"):
            return self.execute(request, shlex.split(json.loads(request.content)["command"]))
        if path.endswith("/stop"):
            self.state = "Stopped"
            return httpx.Response(204)
        if request.method == "DELETE":
            self.state = None
            return httpx.Response(204)
        if request.method == "PUT":
            self.labels, self.state = json.loads(request.content)["labels"], "Running"
            return httpx.Response(200, json=self.detail())
        if path.endswith("/sandboxes"):
            return httpx.Response(200, json={"value": [self.detail()] if self.state else []})
        if self.state is None:
            return httpx.Response(404)
        return httpx.Response(200, json=self.detail())

    def execute(self, request, argv):
        if argv[3] == GUEST_PROBE:
            self.probes += 1
            return receipt(json.dumps({**self.probe, "bridges": 0}) + "\n")
        mode, session = argv[4], argv[5]
        if mode == "start":
            return receipt(json.dumps({"ready": True, "session": session, "uid": 1000}) + "\n")
        sent = json.loads(base64.b64decode(argv[6]))
        self.requests.append(sent)
        reply = {"id": sent["id"], "session": sent["session"], "result": {"text": PAGE}}
        reply = json.dumps(reply, ensure_ascii=False) + "\n"
        response = receipt(reply) if self.answer is None else self.answer(request, reply)
        if response.status_code == 200 and response.headers.get("content-type", "").endswith(
            "json"
        ):
            self.stdout = json.loads(response.content).get("stdout")
        return response

    @property
    def running(self):
        return self.state == "Running"


class Store:
    shared = False
    objects = None

    def __init__(self):
        self.events = []

    def event(self, owner, task_id, kind, data):
        self.events.append((owner, task_id, kind, data))


@pytest.fixture
def cloud(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostics, "_path", tmp_path / "diagnostics.jsonl")
    azure, store = AzureCloud(), Store()
    factory = CloudDesktopFactory(
        "owner", azure.provider, ComputerSpec(str(uuid4())), tmp_path / "ledger", store=store
    )
    computer = factory("bot", tmp_path, cancel=lambda: False)
    computer.bind_task("task-1")
    yield azure, store, computer
    computer.close()
    azure.provider.close()


def receipts(store):
    return [data for _, task, kind, data in store.events if kind == RECEIPT_EVENT]


def diagnostic_entries(tmp_path):
    path = tmp_path / "diagnostics.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    return [entry for entry in map(json.loads, lines) if entry["event"] == RECEIPT_EVENT]


def refused(request, reply):
    return httpx.Response(503, json={"error": {"code": "ServiceUnavailable", "message": UPSTREAM}})


def refused_with_header(request, reply):
    return httpx.Response(500, headers={"x-ms-error-code": "InternalServerError"}, text=UPSTREAM)


def timed_out(request, reply):
    raise httpx.ReadTimeout(UPSTREAM, request=request)


def cut(request, reply):
    return receipt(reply[: len(reply) // 2])


def bridge_failed(request, reply):
    return receipt(
        json.dumps({"error": "Computer bridge unavailable; outcome may be uncertain."}) + "\n",
        exit_code=1,
    )


def killed(request, reply):
    return receipt("", exit_code=137, stderr=UPSTREAM)


def no_exit_status(request, reply):
    return httpx.Response(200, json={"stdout": reply, "stderr": ""})


def too_large(request, reply):
    return httpx.Response(200, content=b" " * (MAX_RESPONSE_BYTES + 1))


def other_request(request, reply):
    return receipt(reply.replace('"id": "', '"id": "0', 1))


# Each way a receipt can fail to arrive whole, with the facts that tell it apart.
FAILURES = {
    "azure refused": (
        refused,
        {"exception": "ComputeUncertain", "http_status": 503, "azure_code": "ServiceUnavailable"},
        False,
    ),
    "azure refused, code in header": (
        refused_with_header,
        {"http_status": 500, "azure_code": "InternalServerError"},
        False,
    ),
    "timed out": (
        timed_out,
        {"exception": "ComputeUncertain", "transport_error": "ReadTimeout", "timed_out": True},
        True,
    ),
    "output cut": (
        cut,
        {"exit_code": 0, "reply": "unreadable", "stdout_cut": True, "stderr_bytes": 0},
        True,
    ),
    "guest bridge failed": (
        bridge_failed,
        {"exit_code": 1, "reply": "bridge_failed", "stdout_cut": False},
        True,
    ),
    "guest process killed": (
        killed,
        {"exit_code": 137, "reply": "empty", "stdout_bytes": 0, "stderr_bytes": len(UPSTREAM)},
        True,
    ),
    "no exit status": (
        no_exit_status,
        {"exception": "ComputeUncertain", "missing_fields": "exitCode"},
        False,
    ),
    "over the allowance": (
        too_large,
        {"exception": "ComputeReplyTooLarge", "over_allowance": True},
        False,
    ),
    "another request's reply": (other_request, {"exit_code": 0, "reply": "other_request"}, False),
}


@pytest.mark.parametrize("name", FAILURES)
@pytest.mark.parametrize("step", [READ, WRITE], ids=["read", "write"])
def test_each_unconfirmed_receipt_is_recorded_with_what_tells_it_apart(cloud, tmp_path, name, step):
    azure, store, computer = cloud
    answer, expected, probed = FAILURES[name]
    azure.answer = answer
    operation, arguments = step
    read = step is READ
    with pytest.raises(
        (ComputerReplyTooLarge if answer is too_large else ComputerReadUnconfirmed)
        if read
        else ComputerActionUncertain
    ):
        computer._request(operation, arguments, cancel=lambda: False)
    [record] = receipts(store)
    for key, value in expected.items():
        assert record[key] == value, key
    assert record["operation"] == "browser_call"
    assert record["method"] == arguments["method"]
    assert record["read_only"] is read
    assert record["computer_kept"] is read
    assert record["timeout_s"] == 50 and 0 <= record["elapsed_ms"] < 50_000
    assert record["provider"] == "azure-sandboxes"
    if "exit_code" in expected:
        assert type(record["stdout_bytes"]) is int and type(record["stderr_bytes"]) is int
        assert record["stdout_bytes"] == len(azure.stdout.encode())
    # A computer that gave no answer of its own is looked at once, for memory and its dispatcher.
    assert azure.probes == int(probed)
    if probed:
        assert record["guest_probe"] is True
        assert record["guest_oom_kills"] == 3 and record["guest_mem_available_kib"] == 41_000
        assert record["guest_bridges"] == 0
    else:
        assert not any(key.startswith("guest_") for key in record)
    [entry] = diagnostic_entries(tmp_path)
    assert {key: entry[key] for key in record} == record
    # Never the request, the page, the output or an upstream message: only counts and names.
    for text in (json.dumps(record, ensure_ascii=False), json.dumps(entry, ensure_ascii=False)):
        assert not any(secret in text for secret in SECRETS)
    assert all(isinstance(v, bool | int) or len(v) <= 80 for v in record.values())
    assert len(azure.requests) == 1  # never replayed


@pytest.mark.parametrize("name", FAILURES)
@pytest.mark.parametrize(
    "operation,arguments",
    [
        ("observe", {}),
        ("read_clipboard", {}),
        ("ping", {}),
        ("browser_call", {"method": "snapshot", "args": [], "kwargs": {"kind": "text"}}),
        ("browser_call", {"method": "page_report", "args": [], "kwargs": {}}),
        ("browser_call", {"method": "capture_image", "args": [], "kwargs": {"full_page": True}}),
    ],
)
def test_a_read_whose_receipt_is_lost_keeps_the_computer(cloud, name, operation, arguments):
    azure, store, computer = cloud
    azure.answer = FAILURES[name][0]
    with pytest.raises((ComputerReadUnconfirmed, ComputerReplyTooLarge)):
        computer._request(operation, arguments, cancel=lambda: False)
    assert computer.alive() and azure.running
    assert len(azure.requests) == 1  # the read itself is not repeated
    azure.answer = None
    assert computer._request(*WRITE, cancel=lambda: False) == {"text": PAGE}
    assert receipts(store)[0]["computer_kept"] is True


@pytest.mark.parametrize("name", FAILURES)
@pytest.mark.parametrize(
    "operation,arguments",
    [
        ("run", {"command": "one-write"}),
        ("browser_call", {"method": "click", "args": [], "kwargs": {}}),
        WRITE,
    ],
)
def test_a_change_whose_receipt_is_lost_still_retires_the_computer(
    cloud, name, operation, arguments
):
    azure, store, computer = cloud
    azure.answer = FAILURES[name][0]
    with pytest.raises(ComputerActionUncertain):
        computer._request(operation, arguments, cancel=lambda: False)
    assert not computer.alive() and azure.state is None
    assert len(azure.requests) == 1
    assert receipts(store)[0]["computer_kept"] is False


@pytest.mark.parametrize("step", [READ, WRITE], ids=["read", "write"])
def test_a_browser_refusing_invalid_input_is_a_complete_answer(cloud, step):
    # The browser's refusal of invalid input is a ValueError; it came back whole, so nothing is
    # uncertain: the computer and its pages stay, and the reader gets the browser's own refusal.
    azure, store, computer = cloud

    def rejected(request, reply):
        sent = json.loads(reply)
        refusal = {
            "id": sent["id"],
            "session": sent["session"],
            "error": "Browser operation rejected; inspect before retrying.",
            "error_type": "BrowserValidationError",
            "error_class": "BrowserValidationError",
            "fatal": False,
        }
        return receipt(json.dumps(refusal) + "\n")

    azure.answer = rejected
    with pytest.raises(BrowserValidationError):
        computer._request(*step, cancel=lambda: False)
    assert computer.alive() and azure.running
    assert receipts(store) == []
    azure.answer = None
    assert computer._request(*READ, cancel=lambda: False) == {"text": PAGE}


def test_an_unavailable_event_store_does_not_hide_the_failure(cloud):
    azure, store, computer = cloud

    def failing_event(*args):
        raise OSError("database unavailable")

    store.event = failing_event
    azure.answer = bridge_failed
    with pytest.raises(ComputerReadUnconfirmed):
        computer._request(*READ, cancel=lambda: False)
    with pytest.raises(ComputerActionUncertain):
        computer._request(*WRITE, cancel=lambda: False)


def test_an_unanswered_probe_records_only_that_it_failed(cloud):
    azure, store, computer = cloud
    azure.answer = killed
    original = azure.execute

    def execute(request, argv):
        if argv[3] == GUEST_PROBE:
            azure.probes += 1
            return receipt("", exit_code=1, stderr=UPSTREAM)
        return original(request, argv)

    azure.execute = execute
    with pytest.raises(ComputerReadUnconfirmed):
        computer._request(*READ, cancel=lambda: False)
    [record] = receipts(store)
    assert record["guest_probe"] is False
    assert not any(key.startswith("guest_") and key != "guest_probe" for key in record)


def test_azure_receipt_facts_are_identifiers_never_upstream_text():
    from alysis_code.bots.compute_azure import _error_code

    request = httpx.Request("POST", "https://management.example.invalid/x")
    for response, code in [
        (httpx.Response(409, json={"error": {"code": "SandboxNotRunning"}}), "SandboxNotRunning"),
        (httpx.Response(500, headers={"x-ms-error-code": "Throttled"}), "Throttled"),
        (httpx.Response(500, json={"error": {"code": UPSTREAM}}), None),
        (httpx.Response(500, json={"error": {"code": "x" * 81}}), None),
        (httpx.Response(500, json={"error": "flat"}), None),
        (httpx.Response(500, json=["list"]), None),
        (httpx.Response(502, text="<html>gateway</html>"), None),
    ]:
        response.request = request
        assert _error_code(response, time.monotonic() + 5) == code


def test_azure_deadline_expiry_is_recorded(monkeypatch):
    from alysis_code.bots import compute_azure

    provider = AzureComputers(
        subscription=str(uuid4()),
        resource_group="test-rg",
        sandbox_group="test-group",
        region="swedencentral",
        access_token=lambda: "synthetic-test-token",
        transport=httpx.MockTransport(lambda request: receipt(PAGE)),
    )
    ticks = iter([0, 31])
    monkeypatch.setattr(compute_azure.time, "monotonic", lambda: next(ticks))
    with pytest.raises(ComputeUncertain) as caught:
        provider._request("POST", provider._path + "/executeShellCommand")
    assert caught.value.facts == {
        "http_status": 200,
        "received_bytes": 0,
        "deadline_expired": True,
    }


@pytest.mark.skipif(not Path("/proc/meminfo").exists(), reason="reads Linux /proc")
def test_the_guest_probe_reads_memory_and_finds_this_sessions_dispatcher():
    session = uuid4().hex
    argv = [sys.executable, "-I", "-c", GUEST_PROBE, session]
    before = json.loads(subprocess.run(argv, capture_output=True, check=True).stdout)
    assert before["bridges"] == 0
    assert (
        before["mem_total_kib"] > 0 and 0 <= before["mem_available_kib"] <= before["mem_total_kib"]
    )
    assert before["oom_kills"] is None or before["oom_kills"] >= 0
    # A process started as the worker starts its dispatcher, for this session and for another.
    sleeper = "import time; time.sleep(30)"
    mine = subprocess.Popen([sys.executable, "-c", sleeper, MODULE, "serve", session])
    other = subprocess.Popen([sys.executable, "-c", sleeper, MODULE, "serve", uuid4().hex])
    try:
        after = json.loads(subprocess.run(argv, capture_output=True, check=True).stdout)
    finally:
        mine.kill()
        other.kill()
        mine.wait()
        other.wait()
    assert after["bridges"] == 1
