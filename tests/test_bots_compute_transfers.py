import io
import json
import subprocess
import sys
import tarfile
import time
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest
from test_bots_compute import AzureWire, identity

from alysis_code.bots.compute import (
    MAX_TRANSFER_BYTES,
    TRANSFER_CHUNK_BYTES,
    ComputeError,
    ComputerRef,
    ComputerSpec,
    ComputeUncertain,
    transfer_chunks,
    validate_transfer,
)
from alysis_code.bots.compute_docker import DockerComputers


class Stream(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    def __iter__(self):
        yield from self.chunks

    def close(self):
        self.closed = True


class FileWire(httpx.BaseTransport):
    def __init__(self):
        self.base = AzureWire()
        self.base.provider._client.close()
        self.base.provider._client = httpx.Client(transport=self, follow_redirects=False)
        self.provider, self.ref = self.base.provider, self.base.ref
        self.requests = []
        self.sizes = []
        self.uploaded = bytearray()
        self.chunks = []
        self.code = 200
        self.headers = {}
        self.consume = True
        self.fail = False
        self.response = None

    def handle_request(self, request):
        if not request.url.path.endswith("/files"):
            return self.base.handle(request)
        self.requests.append(request)
        assert request.headers["authorization"] == "Bearer synthetic-test-token"
        assert request.headers["accept-encoding"] == "identity"
        if request.method == "PUT" and self.consume:
            for chunk in request.stream:
                self.sizes.append(len(chunk))
                self.uploaded.extend(chunk)
        if self.fail:
            raise httpx.ReadError("sensitive upstream body")
        self.response = Stream(self.chunks)
        return httpx.Response(self.code, headers=self.headers, stream=self.response)


@pytest.mark.parametrize("value", [-1, 0, 59, 901, True, 60.0, "180"])
def test_idle_allowance_rejects_invalid(value):
    with pytest.raises(ValueError):
        ComputerSpec("prepared-image", idle_seconds=value)


@pytest.mark.parametrize("value", [60, 180, 900])
def test_azure_idle_allowance_is_explicit_and_keeps_auto_delete(value):
    wire = AzureWire()
    wire.provider.create(wire.identity, ComputerSpec(str(uuid4()), idle_seconds=value))
    body = json.loads(wire.requests[-1].content)
    assert body["lifecycle"]["autoSuspendPolicy"] == {
        "enabled": True,
        "interval": value,
        "mode": "Disk",
    }
    assert body["lifecycle"]["autoDeletePolicy"]["enabled"] is True


@pytest.mark.parametrize(
    "path",
    [
        "relative",
        "/",
        "//host/file",
        "/x/../y",
        "/x/./y",
        "/x//y",
        "/x/",
        "/x\0y",
        "/x\ny",
        "/\ud800",
        1,
    ],
)
def test_transfer_rejects_noncanonical_paths(path):
    with pytest.raises(ValueError):
        validate_transfer(path, 0)


@pytest.mark.parametrize("size", [-1, MAX_TRANSFER_BYTES + 1, True, 1.0])
def test_size_limit_prevents_source_or_provider_access(size):
    wire = FileWire()
    with pytest.raises(ValueError):
        wire.provider.upload(wire.ref, "/tmp/file", None, size=size)
    assert not wire.requests and not wire.base.requests


@pytest.mark.parametrize("data", [b"", bytes(range(256)) * 1025])
def test_azure_exact_unicode_binary_roundtrip(data):
    wire = FileWire()
    path = "/tmp/Νεφέλη 東京;$(literal).bin"
    wire.provider.upload(wire.ref, path, [data], size=len(data))
    assert bytes(wire.uploaded) == data
    assert all(n <= TRANSFER_CHUNK_BYTES for n in wire.sizes)
    request = wire.requests[0]
    assert request.url.params["path"] == path
    assert request.url.params["createDirs"] == "true"
    assert request.headers["content-length"] == str(len(data))
    wire.chunks = [data]
    assert b"".join(wire.provider.download(wire.ref, path, size=len(data))) == data
    assert wire.response.closed


@pytest.mark.parametrize(
    "chunks,size",
    [([b"ab"], 3), ([b"abcd"], 3), ([None], 0), ([b"ab", None, b"c"], 2), (["ab"], 2)],
)
def test_azure_upload_requires_exact_byte_source(chunks, size):
    wire = FileWire()
    with pytest.raises(ComputeUncertain):
        wire.provider.upload(wire.ref, "/tmp/file", chunks, size=size)
    assert len(wire.requests) == 1


def test_azure_early_success_is_not_upload_confirmation():
    wire = FileWire()
    wire.consume = False
    with pytest.raises(ComputeUncertain, match="fully consumed"):
        wire.provider.upload(wire.ref, "/tmp/file", [b"ab"], size=2)
    assert len(wire.requests) == 1


@pytest.mark.parametrize("operation", ["upload", "download"])
@pytest.mark.parametrize("failure", ["owner", "scope", "stopped", "cancelled"])
def test_transfer_requires_live_owned_resource_before_file_request(operation, failure):
    wire = FileWire()
    ref = wire.ref
    if failure == "owner":
        ref = replace(ref, identity=replace(ref.identity, owner_id="different"))
    elif failure == "scope":
        ref = replace(ref, scope="wrong")
    elif failure == "stopped":
        wire.base.state = "Stopped"
    with pytest.raises(ComputeError):
        if operation == "upload":
            wire.provider.upload(
                ref, "/tmp/file", None, size=0, cancel=lambda: failure == "cancelled"
            )
        else:
            list(
                wire.provider.download(
                    ref, "/tmp/file", size=0, cancel=lambda: failure == "cancelled"
                )
            )
    assert not wire.requests


@pytest.mark.parametrize("operation", ["upload", "download"])
@pytest.mark.parametrize("code", [202, 307, 403, 500])
def test_azure_file_failure_never_follows_redirect_or_replays(operation, code):
    wire = FileWire()
    wire.code = code
    wire.headers = {"location": "https://unrelated.invalid"}
    with pytest.raises(ComputeError):
        if operation == "upload":
            wire.provider.upload(wire.ref, "/tmp/file", [], size=0)
        else:
            list(wire.provider.download(wire.ref, "/tmp/file", size=0))
    assert len(wire.requests) == 1 and wire.response.closed


@pytest.mark.parametrize(
    "headers,chunks",
    [
        ({"content-length": "4"}, [b"ab"]),
        ({}, [b"a"]),
        ({}, [b"abc"]),
        ({"content-encoding": "gzip"}, [b"ab"]),
        ({"content-length": "bad"}, [b"ab"]),
    ],
)
def test_azure_download_rejects_bad_size_or_encoding(headers, chunks):
    wire = FileWire()
    wire.headers, wire.chunks = headers, chunks
    with pytest.raises(ComputeError):
        list(wire.provider.download(wire.ref, "/tmp/file", size=2))
    assert wire.response.closed


def test_download_cancellation_and_abandonment_close_response():
    wire = FileWire()
    wire.chunks = [b"a" * TRANSFER_CHUNK_BYTES, b"b" * TRANSFER_CHUNK_BYTES]
    cancelled = False
    stream = wire.provider.download(
        wire.ref, "/tmp/file", size=2 * TRANSFER_CHUNK_BYTES, cancel=lambda: cancelled
    )
    assert len(next(stream)) == TRANSFER_CHUNK_BYTES
    cancelled = True
    with pytest.raises(ComputeError):
        next(stream)
    assert wire.response.closed
    stream = wire.provider.download(wire.ref, "/tmp/file", size=2 * TRANSFER_CHUNK_BYTES)
    next(stream)
    stream.close()
    assert wire.response.closed


def test_upload_cancelled_between_chunks_does_not_replay():
    wire = FileWire()
    cancelled = False

    def source():
        nonlocal cancelled
        yield b"a"
        cancelled = True
        yield b"b"

    with pytest.raises(ComputeUncertain):
        wire.provider.upload(wire.ref, "/tmp/file", source(), size=2, cancel=lambda: cancelled)
    assert wire.uploaded == b"a" and len(wire.requests) == 1


def test_maximum_size_is_streamed_with_bounded_chunks():
    chunk = bytes(TRANSFER_CHUNK_BYTES)
    sizes = [
        len(x)
        for x in transfer_chunks(
            (chunk for _ in range(MAX_TRANSFER_BYTES // len(chunk))),
            MAX_TRANSFER_BYTES,
            lambda: None,
        )
    ]
    assert sum(sizes) == MAX_TRANSFER_BYTES and max(sizes) == TRANSFER_CHUNK_BYTES


def test_source_and_transport_errors_do_not_expose_contents():
    def source():
        raise ComputeError("sensitive source data")
        yield b""

    with pytest.raises(ComputeError) as failure:
        list(transfer_chunks(source(), 0, lambda: None))
    assert "sensitive" not in str(failure.value)
    wire = FileWire()
    wire.fail = True
    with pytest.raises(ComputeUncertain) as failure:
        wire.provider.upload(wire.ref, "/tmp/file", [], size=0)
    assert "sensitive" not in str(failure.value)
    assert len(wire.requests) == 1


def test_cancellation_callback_failure_does_not_expose_state():
    wire = FileWire()

    def cancel():
        raise RuntimeError("sensitive callback state")

    with pytest.raises(ComputeError) as failure:
        wire.provider.upload(wire.ref, "/tmp/file", [], size=0, cancel=cancel)
    assert "sensitive" not in str(failure.value)
    assert not wire.requests and not wire.base.requests


def test_azure_receipt_size_and_total_deadline_are_bounded(monkeypatch):
    from alysis_code.bots import compute_azure

    wire = FileWire()
    wire.chunks = [bytes(2 * 1024 * 1024 + 1)]
    with pytest.raises(ComputeUncertain, match="allowance"):
        wire.provider.upload(wire.ref, "/tmp/file", [], size=0)
    assert wire.response.closed
    monkeypatch.setattr(compute_azure, "TRANSFER_SECONDS", 0)
    wire = FileWire()
    with pytest.raises(ComputeError, match="timed out"):
        list(wire.provider.download(wire.ref, "/tmp/file", size=0))
    assert not wire.requests and not wire.base.requests


def docker_provider(monkeypatch):
    provider = DockerComputers("desktop-linux")
    ref = ComputerRef("docker", provider.scope, "a" * 64, identity())
    monkeypatch.setattr(
        provider,
        "_inspect",
        lambda _: {"Config": {"Labels": ref.identity.labels}, "State": {"Status": "running"}},
    )
    return provider, ref


def archive_bytes(data, kind=tarfile.REGTYPE, extra=False):
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        info = tarfile.TarInfo("file.bin")
        info.type, info.size = kind, len(data)
        tar.addfile(info, io.BytesIO(data))
        if extra:
            tar.addfile(tarfile.TarInfo("another"))
    return archive.getvalue()


def test_docker_upload_preserves_worker_access_and_download_exact_bytes(monkeypatch):
    provider, ref = docker_provider(monkeypatch)
    data = bytes(range(256)) * 1025
    stored = b""
    calls = []

    def call(args, **kwargs):
        nonlocal stored
        calls.append(args)
        if args[0:2] == ["cp", "-a"]:
            stored = kwargs["stdin"].read()
            with tarfile.open(fileobj=io.BytesIO(stored)) as tar:
                (member,) = tar.getmembers()
                assert (member.uid, member.gid, member.mode) == (1000, 1000, 0o600)
                assert member.name == "東京.bin"
                assert tar.extractfile(member).read() == data
        elif args[0] == "cp":
            kwargs["output_file"].write(stored)
        return subprocess.CompletedProcess([], 0, b"", b"")

    monkeypatch.setattr(provider, "_call", call)
    provider.upload(ref, "/tmp/private/東京.bin", [data], size=len(data))
    assert b"".join(provider.download(ref, "/tmp/private/東京.bin", size=len(data))) == data
    assert len(calls) == 3


@pytest.mark.parametrize(
    "kind,extra,size",
    [
        (tarfile.SYMTYPE, False, 2),
        (tarfile.DIRTYPE, False, 2),
        (tarfile.REGTYPE, True, 2),
        (tarfile.REGTYPE, False, 3),
    ],
)
def test_docker_rejects_non_single_regular_file(monkeypatch, kind, extra, size):
    provider, ref = docker_provider(monkeypatch)

    def call(args, **kwargs):
        kwargs["output_file"].write(archive_bytes(b"ab", kind, extra))
        return subprocess.CompletedProcess([], 0, b"", b"")

    monkeypatch.setattr(provider, "_call", call)
    with pytest.raises(ComputeError):
        list(provider.download(ref, "/tmp/file", size=size))


def test_docker_wrong_owner_and_short_source_do_not_dispatch(monkeypatch):
    provider, ref = docker_provider(monkeypatch)
    calls = []
    monkeypatch.setattr(provider, "_call", lambda *a, **k: calls.append(a))
    with pytest.raises(ComputeError):
        provider.upload(replace(ref, identity=identity()), "/tmp/file", [b"ab"], size=2)
    with pytest.raises(ComputeError):
        provider.upload(ref, "/tmp/file", [b"ab"], size=3)
    assert not calls


def test_docker_partial_upload_is_uncertain_and_staging_is_closed(monkeypatch):
    import tempfile

    provider, ref = docker_provider(monkeypatch)
    calls, staging = [], []
    original = tempfile.TemporaryFile

    def temporary():
        stream = original()
        staging.append(stream)
        return stream

    def call(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(
            [], 1 if args[0] == "cp" else 0, b"", b"sensitive failure"
        )

    monkeypatch.setattr("alysis_code.bots.compute_docker.tempfile.TemporaryFile", temporary)
    monkeypatch.setattr(provider, "_call", call)
    with pytest.raises(ComputeUncertain) as failure:
        provider.upload(ref, "/tmp/file", [b"ab"], size=2)
    assert "sensitive" not in str(failure.value)
    assert len(calls) == 2 and all(stream.closed for stream in staging)


def test_docker_download_failure_or_abandonment_closes_staging(monkeypatch):
    import tempfile

    provider, ref = docker_provider(monkeypatch)
    staging = []
    original = tempfile.TemporaryFile
    code = 0

    def temporary():
        stream = original()
        staging.append(stream)
        return stream

    def call(args, **kwargs):
        kwargs["output_file"].write(archive_bytes(bytes(TRANSFER_CHUNK_BYTES * 2)))
        return subprocess.CompletedProcess([], code, b"", b"sensitive failure")

    monkeypatch.setattr("alysis_code.bots.compute_docker.tempfile.TemporaryFile", temporary)
    monkeypatch.setattr(provider, "_call", call)
    stream = provider.download(ref, "/tmp/file", size=TRANSFER_CHUNK_BYTES * 2)
    next(stream)
    stream.close()
    assert staging[-1].closed
    code = 1
    with pytest.raises(ComputeError) as failure:
        list(provider.download(ref, "/tmp/file", size=TRANSFER_CHUNK_BYTES * 2))
    assert "sensitive" not in str(failure.value)
    assert staging[-1].closed


@pytest.mark.parametrize("mode", ["cancel", "overflow", "deadline"])
def test_docker_copy_process_is_killed_when_bounded_transfer_stops(monkeypatch, mode):
    # A real child process verifies selector draining and cleanup without Docker.
    provider = DockerComputers("desktop-linux")
    popen = subprocess.Popen
    children = []

    def child(*args, **kwargs):
        script = "import os,time;os.write(1,b'x'*10000);time.sleep(10)"
        process = popen([sys.executable, "-c", script], **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr("alysis_code.bots.compute_docker.subprocess.Popen", child)
    start = time.monotonic()
    with io.BytesIO() as output, pytest.raises(ComputeError):
        provider._call(
            ["cp"],
            output_file=output,
            output_limit=10 if mode == "overflow" else 20000,
            timeout=0.1 if mode == "deadline" else 2,
            cancel=lambda: mode == "cancel" and time.monotonic() - start > 0.1,
        )
    assert time.monotonic() - start < 3
    assert len(children) == 1 and children[0].poll() is not None
