import hashlib
import io
from pathlib import Path

import pytest

from scripts.release import install_managed_cli_python as installer
from scripts.release.install_managed_cli_python import validate_runtime, verify_archive


def test_distribution_is_verified_before_extraction(tmp_path: Path) -> None:
    archive = tmp_path / "python.tar.gz"
    archive.write_bytes(b"correct distribution")
    pin = {
        "size": archive.stat().st_size,
        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
    }
    verify_archive(archive, pin)
    archive.write_bytes(b"altered distribution")
    with pytest.raises(ValueError, match="SHA-256"):
        verify_archive(archive, pin)
    archive.write_bytes(b"short")
    with pytest.raises(ValueError, match="size"):
        verify_archive(archive, pin)


def test_incomplete_python_download_is_retried_with_the_same_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    complete = b"verified Python distribution"
    downloads = iter([complete[:5], complete])
    pin = {
        "url": "https://example.test/python.tar.gz",
        "size": len(complete),
        "sha256": hashlib.sha256(complete).hexdigest(),
    }
    calls = []

    def download(url: str, *, timeout: int) -> io.BytesIO:
        calls.append((url, timeout))
        return io.BytesIO(next(downloads))

    monkeypatch.setattr(installer.urllib.request, "urlopen", download)
    monkeypatch.setattr(installer.time, "sleep", lambda _seconds: None)
    archive = tmp_path / "python.tar.gz"
    installer.download_verified_archive(archive, pin)
    assert archive.read_bytes() == complete
    assert calls == [(pin["url"], 120)] * 2


@pytest.mark.parametrize("bad_archive", [b"short", b"bad archive", b"oversized archive"])
def test_retry_never_accepts_a_mismatched_python_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_archive: bytes
) -> None:
    complete = b"good binary"
    pin = {
        "url": "https://example.test/python.tar.gz",
        "size": len(complete),
        "sha256": hashlib.sha256(complete).hexdigest(),
    }
    calls = []

    def download(url: str, *, timeout: int) -> io.BytesIO:
        calls.append((url, timeout))
        return io.BytesIO(bad_archive)

    monkeypatch.setattr(installer.urllib.request, "urlopen", download)
    monkeypatch.setattr(installer.time, "sleep", lambda _seconds: None)
    archive = tmp_path / "python.tar.gz"
    with pytest.raises(ValueError, match="size|SHA-256"):
        installer.download_verified_archive(archive, pin)
    assert len(calls) == 6
    assert not archive.exists()


@pytest.mark.parametrize("machine", ["AMD64", "x86_64", "i386"])
def test_arm64_cannot_be_satisfied_by_emulated_intel_python(machine: str) -> None:
    with pytest.raises(ValueError, match="native target"):
        validate_runtime(
            {"version": "3.12.15", "platform": "win32", "machine": machine},
            "win32-arm64",
            "3.12.15",
        )


def test_unpatched_runtime_cannot_satisfy_the_version_pin() -> None:
    with pytest.raises(ValueError, match="version"):
        validate_runtime(
            {"version": "3.12.13", "platform": "linux", "machine": "aarch64"},
            "linux-arm64",
            "3.12.15",
        )
    validate_runtime(
        {"version": "3.12.15", "platform": "linux", "machine": "aarch64"}, "linux-arm64", "3.12.15"
    )


def test_gateway_failures_back_off_before_retrying_the_pinned_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    complete = b"verified archive"
    pin = {
        "url": "https://example.test/python.tar.gz",
        "size": len(complete),
        "sha256": hashlib.sha256(complete).hexdigest(),
    }
    attempts = []
    delays = []

    def download(url: str, *, timeout: int) -> io.BytesIO:
        attempts.append(url)
        if len(attempts) <= 3:
            raise installer.urllib.error.HTTPError(url, 504, "Gateway timeout", {}, None)
        return io.BytesIO(complete)

    monkeypatch.setattr(installer.urllib.request, "urlopen", download)
    monkeypatch.setattr(installer.time, "sleep", delays.append)
    archive = tmp_path / "python.tar.gz"
    installer.download_verified_archive(archive, pin)
    assert archive.read_bytes() == complete
    assert attempts == [pin["url"]] * 4
    assert delays == [2, 4, 8]


@pytest.mark.parametrize("corrupt", [False, True])
def test_official_asset_api_fallback_requires_the_original_archive_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, corrupt: bool
) -> None:
    complete = b"verified archive"
    pin = {
        "url": "https://github.com/astral-sh/python-build-standalone/releases/download/test/python.tar.gz",
        "api_url": "https://api.github.com/repos/astral-sh/python-build-standalone/releases/assets/123",
        "size": len(complete),
        "sha256": hashlib.sha256(complete).hexdigest(),
    }
    calls = []

    def download(source, *, timeout: int) -> io.BytesIO:
        if isinstance(source, str):
            calls.append(source)
            raise installer.urllib.error.HTTPError(source, 504, "Gateway timeout", {}, None)
        calls.append(source.full_url)
        assert source.get_header("Accept") == "application/octet-stream"
        return io.BytesIO(b"x" * len(complete) if corrupt else complete)

    monkeypatch.setattr(installer.urllib.request, "urlopen", download)
    monkeypatch.setattr(installer.time, "sleep", lambda _seconds: None)
    archive = tmp_path / "python.tar.gz"
    if corrupt:
        with pytest.raises(ValueError, match="SHA-256"):
            installer.download_verified_archive(archive, pin)
        assert not archive.exists()
    else:
        installer.download_verified_archive(archive, pin)
        assert archive.read_bytes() == complete
    assert calls == [pin["url"]] * 3 + [pin["api_url"]] * (3 if corrupt else 1)
