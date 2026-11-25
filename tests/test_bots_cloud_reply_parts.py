"""A Cloud computer's reply larger than its provider returns of one command's output (Azure's
sandboxes: 1 MiB, then the command is stopped) is kept on the computer and read in parts.

The guest side (cloud_reply_parts) is tested directly and as the real command the host sends; the
host side through the real Azure adapter, with Azure emulated over httpx's mock transport, cutting
output where Azure does."""

import base64
import json
import os
import shlex
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from alysis_code.bots import cloud_reply_parts, diagnostics
from alysis_code.bots.cloud_desktop import (
    BOOTSTRAP,
    GUEST_PROBE,
    MAX_KEPT_REPLY,
    PART_ATTEMPTS,
    READ_PART,
    RECEIPT_EVENT,
    REQUEST_IN_PARTS,
    SOURCE_ROOT,
    CloudDesktopFactory,
    ComputerReadUnconfirmed,
    part_bytes,
    worker_argv,
)
from alysis_code.bots.compute import ComputerSpec
from alysis_code.bots.compute_azure import AzureComputers
from alysis_code.bots.desktop_linux import ComputerActionUncertain, ComputerReplyTooLarge

# A long page in several scripts: what a heavy article's snapshot carries.
PARAGRAPH = (
    "Ο Δεύτερος Παγκόσμιος Πόλεμος ήταν παγκόσμια σύγκρουση. "
    "World War II was a global conflict. 第二次世界大战是一场全球战争。 "
)
LIMIT = 1024 * 1024


def page(size):
    text = PARAGRAPH * (size // len(PARAGRAPH.encode()) + 1)
    return text[: size // 2] + "<b>middle</b>" + text[size // 2 :]


def receipt(stdout, *, exit_code=0, stderr=""):
    return httpx.Response(200, json={"exitCode": exit_code, "stdout": stdout, "stderr": stderr})


# The guest side, in this process.


@pytest.fixture
def guest_root(tmp_path, monkeypatch):
    root = tmp_path / "computer-tmp"
    root.mkdir()
    monkeypatch.setattr(cloud_reply_parts, "ROOT", root)
    return root


def test_a_kept_reply_reads_back_exactly_in_parts_and_then_goes(guest_root):
    data = page(3 * 1024 * 1024).encode()
    folder = cloud_reply_parts.folder_for(uuid4().hex)
    kept = cloud_reply_parts.keep(data, folder)
    assert kept["bytes"] == len(data) and len(kept["sha256"]) == 64
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    assert stat.S_IMODE((folder / kept["name"]).stat().st_mode) == 0o600
    step = 700_001
    pieces = []
    for offset in range(0, len(data), step):
        reply = cloud_reply_parts.read_part(folder, kept["name"], offset, step)
        assert reply["bytes"] == len(data) and reply["offset"] == offset
        pieces.append(base64.b64decode(reply["part"]))
    assert b"".join(pieces) == data
    assert list(folder.iterdir()) == []  # gone with its last part


@pytest.mark.parametrize("name", ["../" + "a" * 29, "A" * 32, "a" * 31, "a" * 33, "", None])
def test_only_a_kept_reply_name_is_read(guest_root, name):
    folder = cloud_reply_parts.folder_for(uuid4().hex)
    cloud_reply_parts.keep(b"x", folder)
    with pytest.raises(ValueError):
        cloud_reply_parts.read_part(folder, name, 0, 10)


@pytest.mark.parametrize("session", ["../etc", "A" * 32, "a" * 31, 7])
def test_only_a_session_identifier_names_a_folder(guest_root, session):
    with pytest.raises(ValueError):
        cloud_reply_parts.folder_for(session)


def test_a_folder_others_can_open_or_a_link_is_never_used(guest_root, tmp_path):
    shared = cloud_reply_parts.folder_for(uuid4().hex)
    shared.mkdir(mode=0o755)
    shared.chmod(0o755)
    with pytest.raises(PermissionError):
        cloud_reply_parts.keep(b"x", shared)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    linked = cloud_reply_parts.folder_for(uuid4().hex)
    linked.symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(PermissionError):
        cloud_reply_parts.keep(b"x", linked)
    assert list(elsewhere.iterdir()) == []
    # A kept reply replaced by a link to another file is not followed.
    folder = cloud_reply_parts.folder_for(uuid4().hex)
    kept = cloud_reply_parts.keep(b"reply", folder)
    secret = tmp_path / "secret.txt"
    secret.write_text("not a reply")
    (folder / kept["name"]).unlink()
    (folder / kept["name"]).symlink_to(secret)
    with pytest.raises(OSError):
        cloud_reply_parts.read_part(folder, kept["name"], 0, 100)
    assert secret.exists()


def test_abandoned_replies_go_by_age_and_count(guest_root, monkeypatch):
    folder = cloud_reply_parts.folder_for(uuid4().hex)
    names = [cloud_reply_parts.keep(b"old", folder)["name"] for _ in range(2)]
    stale = time.time() - cloud_reply_parts.KEEP_SECONDS - 5
    for name in names:
        os.utime(folder / name, (stale, stale))
    recent = []
    for index in range(cloud_reply_parts.KEEP_FILES + 3):
        recent.append(cloud_reply_parts.keep(b"new", folder)["name"])
        moment = time.time() - 100 + index
        os.utime(folder / recent[-1], (moment, moment))
    left = sorted(path.name for path in folder.iterdir())
    assert not set(names) & set(left)  # past KEEP_SECONDS
    assert len(left) == cloud_reply_parts.KEEP_FILES
    assert set(left) == set(recent[-cloud_reply_parts.KEEP_FILES :])  # the newest stay


# The guest side as the real command the host sends, against a stand-in bridge client.

CLIENT = """
import json, os, sys
def main():
    mode, session = sys.argv[1:3]
    request = json.loads(__import__("base64").b64decode(sys.argv[3]))
    if request.get("fail"):
        print(json.dumps({"error": "Computer bridge unavailable; outcome may be uncertain."}))
        sys.exit(1)
    text = (request["unit"] * (request["size"] // len(request["unit"]) + 1))[: request["size"]]
    reply = {"id": request["id"], "session": session, "result": {"text": text}}
    sys.stdout.buffer.write(json.dumps(reply, ensure_ascii=False).encode("utf-8") + b"\\n")
    sys.stdout.flush()
if __name__ == "__main__":
    main()
"""


@pytest.fixture
def computer_files():
    """A stand-in bridge client and a place for kept replies, readable by the user the commands
    run as (never root: the real client gives up root before it answers)."""
    base = Path(tempfile.mkdtemp(prefix="alysis-parts-test-"))
    source = base / "src"
    (source / "alysis_code" / "bots").mkdir(parents=True)
    for package in (source / "alysis_code", source / "alysis_code" / "bots"):
        (package / "__init__.py").write_text("")
    (source / "alysis_code" / "bots" / "cloud_desktop_worker.py").write_text(CLIENT)
    replies = base / "replies"
    replies.mkdir()
    for path in [base, *base.rglob("*")]:
        path.chmod(0o755 if path.is_dir() else 0o644)
    replies.chmod(0o1777)
    yield source, replies
    subprocess.run(["rm", "-rf", str(base)], check=False)


def run_on_computer(snippet, source, replies, *args):
    command = snippet.replace(repr(SOURCE_ROOT), repr(str(source))).replace(
        'ROOT = Path("/tmp")', f"ROOT = Path({str(replies)!r})"
    )
    user = {"user": 65534, "group": 65534} if os.geteuid() == 0 else {}
    return subprocess.run(
        [sys.executable, "-I", "-c", command, *args],
        capture_output=True,
        timeout=60,
        check=False,
        **user,
    )


def test_the_real_command_prints_a_small_reply_and_keeps_a_large_one(computer_files):
    source, replies = computer_files
    session = uuid4().hex
    limit = 64 * 1024
    for size in (100, 300 * 1024):
        request = {"id": uuid4().hex, "unit": PARAGRAPH, "size": size}
        payload = base64.b64encode(json.dumps(request).encode()).decode()
        done = run_on_computer(
            REQUEST_IN_PARTS, source, replies, "request", session, payload, str(limit)
        )
        assert done.returncode == 0, done.stderr
        text = (PARAGRAPH * (size // len(PARAGRAPH) + 1))[:size]
        expected = {"id": request["id"], "session": session, "result": {"text": text}}
        if size < limit:
            assert json.loads(done.stdout) == expected
            continue
        assert len(done.stdout) < 1024
        kept = json.loads(done.stdout)["parts"]
        step = part_bytes(limit)
        data = b""
        for offset in range(0, kept["bytes"], step):
            part = run_on_computer(
                READ_PART, source, replies, session, kept["name"], str(offset), str(step)
            )
            assert part.returncode == 0, part.stderr
            assert len(part.stdout) <= limit
            data += base64.b64decode(json.loads(part.stdout)["part"])
        assert json.loads(data) == expected
        folder = replies / ("alysis-replies-" + session)
        assert list(folder.iterdir()) == []


def test_the_real_command_keeps_the_clients_own_failure_as_it_is(computer_files):
    source, replies = computer_files
    payload = base64.b64encode(json.dumps({"fail": True}).encode()).decode()
    done = run_on_computer(REQUEST_IN_PARTS, source, replies, "request", uuid4().hex, payload, "64")
    assert done.returncode == 1
    assert json.loads(done.stdout) == {
        "error": "Computer bridge unavailable; outcome may be uncertain."
    }


# The host side, through the real Azure adapter.


class CuttingAzure:
    """Azure's sandbox API as the adapter sees it, returning at most LIMIT bytes of a command's
    output and stopping the command beyond it, as Azure does. Its guest answers each request
    with ``text`` and keeps a large reply with the real cloud_reply_parts."""

    def __init__(self, root):
        self.id = str(uuid4())
        self.state = None
        self.labels = None
        self.root = root
        self.text = "short"
        self.requests = []
        self.parts = []
        self.lose_part = lambda offset, attempt: False
        self.change_part = False
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

    def printed(self, data, status=0):
        # Azure: output past its limit is cut, and the command is stopped (status 137).
        if len(data) > LIMIT:
            data, status = data[:LIMIT], 137
        return receipt(data.decode("utf-8", "replace"), exit_code=status)

    def execute(self, request, argv):
        snippet = argv[3]
        if snippet == GUEST_PROBE:
            return receipt(json.dumps({"oom_kills": 0, "bridges": 1}) + "\n")
        if snippet == READ_PART:
            session, name, offset, length = argv[4:8]
            attempt = sum(1 for seen in self.parts if seen == int(offset))
            self.parts.append(int(offset))
            if self.lose_part(int(offset), attempt):
                raise httpx.ReadTimeout("part lost", request=request)
            reply = cloud_reply_parts.read_part(
                cloud_reply_parts.folder_for(session), name, int(offset), int(length)
            )
            if self.change_part and int(offset) == 0:
                data = bytearray(base64.b64decode(reply["part"]))
                data[0] ^= 1
                reply["part"] = base64.b64encode(bytes(data)).decode()
            return self.printed(json.dumps(reply).encode() + b"\n")
        mode, session = argv[4], argv[5]
        if mode == "start":
            return receipt(json.dumps({"ready": True, "session": session, "uid": 1000}) + "\n")
        sent = json.loads(base64.b64decode(argv[6]))
        self.requests.append((snippet, sent))
        reply = {"id": sent["id"], "session": sent["session"], "result": {"text": self.text}}
        data = json.dumps(reply, ensure_ascii=False).encode() + b"\n"
        if snippet == REQUEST_IN_PARTS and len(data) > int(argv[7]):
            kept = cloud_reply_parts.keep(data, cloud_reply_parts.folder_for(session))
            data = json.dumps({"parts": kept}).encode() + b"\n"
        return self.printed(data)

    @property
    def running(self):
        return self.state == "Running"


class Store:
    shared = False
    objects = None

    def __init__(self):
        self.events = []

    def event(self, owner, task_id, kind, data):
        self.events.append((kind, data))


@pytest.fixture
def cloud(tmp_path, monkeypatch, guest_root):
    monkeypatch.setattr(diagnostics, "_path", tmp_path / "diagnostics.jsonl")
    azure, store = CuttingAzure(guest_root), Store()
    factory = CloudDesktopFactory(
        "owner", azure.provider, ComputerSpec(str(uuid4())), tmp_path / "ledger", store=store
    )
    computer = factory("bot", tmp_path, cancel=lambda: False)
    computer.bind_task("task-1")
    yield azure, store, computer
    computer.close()
    azure.provider.close()


READ = ("browser_call", {"method": "snapshot", "args": [], "kwargs": {"kind": "dom"}})
WRITE = ("browser_call", {"method": "click", "args": [], "kwargs": {}})


def kept_files(azure):
    return [path for path in azure.root.rglob("*") if path.is_file()]


@pytest.mark.parametrize("size", [LIMIT - 100_000, LIMIT + 1, int(2.5 * LIMIT)])
@pytest.mark.parametrize("step", [READ, WRITE], ids=["read", "write"])
def test_a_reply_larger_than_azure_returns_arrives_whole(cloud, size, step):
    azure, store, computer = cloud
    azure.text = page(size)
    assert computer._request(*step, cancel=lambda: False) == {"text": azure.text}
    assert computer.alive() and azure.running
    assert len(azure.requests) == 1  # the request itself ran once
    expected_parts = 0 if size < LIMIT else -(-(size + 100) // part_bytes(LIMIT))
    assert len(azure.parts) in (expected_parts, expected_parts + 1)
    assert [kind for kind, _ in store.events if kind == RECEIPT_EVENT] == []
    assert kept_files(azure) == []


def test_without_parts_the_same_reply_is_cut_and_lost(cloud, monkeypatch):
    # What the host did before: the whole reply in one command's output, which Azure cuts.
    azure, store, computer = cloud
    monkeypatch.setattr(type(azure.provider), "command_output_bytes", None)
    azure.text = page(int(1.5 * LIMIT))
    with pytest.raises(ComputerReadUnconfirmed):
        computer._request(*READ, cancel=lambda: False)
    [(kind, facts)] = store.events
    assert facts["exit_code"] == 137 and facts["stdout_bytes"] == LIMIT and facts["stdout_cut"]


def test_a_lost_part_is_asked_for_again(cloud):
    azure, store, computer = cloud
    azure.text = page(int(2.5 * LIMIT))
    second = part_bytes(LIMIT)
    azure.lose_part = lambda offset, attempt: offset == second and attempt < PART_ATTEMPTS - 1
    assert computer._request(*READ, cancel=lambda: False) == {"text": azure.text}
    assert azure.parts.count(second) == PART_ATTEMPTS
    assert len(azure.requests) == 1
    assert store.events == []


@pytest.mark.parametrize("step", [READ, WRITE], ids=["read", "write"])
def test_a_part_that_never_arrives_is_recorded_and_never_replays_the_request(cloud, step):
    azure, store, computer = cloud
    azure.text = page(int(2.5 * LIMIT))
    second = part_bytes(LIMIT)
    azure.lose_part = lambda offset, attempt: offset == second
    read = step is READ
    with pytest.raises(ComputerReadUnconfirmed if read else ComputerActionUncertain):
        computer._request(*step, cancel=lambda: False)
    assert len(azure.requests) == 1 and azure.parts.count(second) == PART_ATTEMPTS
    [(kind, facts)] = store.events
    assert kind == RECEIPT_EVENT
    assert facts["reply"] == "kept" and facts["parts_read"] == 1
    assert facts["kept_bytes"] > 2 * LIMIT and facts["part_error"] == "ComputeUncertain"
    assert facts["computer_kept"] is read
    assert computer.alive() is read


def test_a_reply_that_does_not_match_its_receipt_is_not_used(cloud):
    azure, store, computer = cloud
    azure.text = page(int(1.5 * LIMIT))
    azure.change_part = True
    with pytest.raises(ComputerReadUnconfirmed):
        computer._request(*READ, cancel=lambda: False)
    [(kind, facts)] = store.events
    assert facts["checksum"] is False and facts["reply"] == "kept"
    assert computer.alive()


def test_a_kept_reply_beyond_what_the_host_reads_is_refused_before_reading(cloud, monkeypatch):
    azure, store, computer = cloud
    real_keep = cloud_reply_parts.keep

    def huge(data, folder):
        return {**real_keep(data, folder), "bytes": MAX_KEPT_REPLY + 1}

    monkeypatch.setattr(cloud_reply_parts, "keep", huge)
    azure.text = page(int(1.5 * LIMIT))
    with pytest.raises(ComputerReplyTooLarge):
        computer._request(*READ, cancel=lambda: False)
    assert azure.parts == [] and computer.alive()


def test_cancelling_while_parts_are_read_stops_without_replaying(cloud):
    azure, store, computer = cloud
    azure.text = page(int(2.5 * LIMIT))
    cancelled = []
    azure.lose_part = lambda offset, attempt: cancelled.append(True) or False
    from alysis_code.cancellation import CooperativeCancellationError

    with pytest.raises(CooperativeCancellationError):
        computer._request(*READ, cancel=lambda: bool(cancelled))
    assert len(azure.requests) == 1


def test_the_wrapper_is_used_only_where_it_fits_and_is_needed():
    session = uuid4().hex
    small = base64.b64encode(b"{}").decode()
    assert worker_argv("request", session, small, output_bytes=LIMIT)[3] == REQUEST_IN_PARTS
    # A provider that returns all of a command's output, and every other mode, run as before.
    assert worker_argv("request", session, small)[3] == BOOTSTRAP
    assert worker_argv("screen", session, small, output_bytes=LIMIT)[3] == BOOTSTRAP
    # A request at the allowance still goes: as before, without the wrapper.
    largest = base64.b64encode(b"x" * 48000).decode()
    assert worker_argv("request", session, largest, output_bytes=LIMIT)[3] == BOOTSTRAP
