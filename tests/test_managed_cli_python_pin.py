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
    assert len(calls) == 3
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
