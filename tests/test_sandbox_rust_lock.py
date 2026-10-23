from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pytest

from scripts.sandbox.lock_rust_inputs import input_path, prepare


@pytest.mark.parametrize(
    "url",
    [
        "http://static.rust-lang.org/dist/input",
        "https://static.rust-lang.org.evil.example/dist/input",
        "https://user@static.rust-lang.org/dist/input",
        "https://static.rust-lang.org/dist/../escape",
        "https://static.rust-lang.org/dist/%2e%2e/escape",
        "https://static.rust-lang.org/dist/input?redirect=elsewhere",
        "https://static.rust-lang.org/dist/input#fragment",
        "https://static.rust-lang.org/dist/..\\escape",
    ],
)
def test_rust_input_rejects_origin_and_path_escape(url: str) -> None:
    with pytest.raises(ValueError):
        input_path(url)


def _lock(tmp_path: Path, digest: str) -> Path:
    paths = [
        "dist/channel-rust-1.91.1.toml",
        "rustup/archive/1.28.2/x86_64-unknown-linux-gnu/rustup-init",
        "dist/2025-11-10/rustc-1.91.1-x86_64-unknown-linux-gnu.tar.xz",
        "dist/2025-11-10/cargo-1.91.1-x86_64-unknown-linux-gnu.tar.xz",
        "dist/2025-11-10/rust-std-1.91.1-x86_64-unknown-linux-gnu.tar.xz",
    ]
    lock = tmp_path / "lock.json"
    lock.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "rust_version": "1.91.1",
                "rustup_version": "1.28.2",
                "architectures": {
                    "amd64": {
                        "target": "x86_64-unknown-linux-gnu",
                        "inputs": [
                            {"url": "https://static.rust-lang.org/" + path, "sha256": digest}
                            for path in paths
                        ],
                    },
                },
            }
        )
    )
    return lock


def test_rust_input_hash_mismatch_never_creates_installable_file(tmp_path, monkeypatch) -> None:
    lock = _lock(tmp_path, "0" * 64)
    monkeypatch.setattr(
        "scripts.sandbox.lock_rust_inputs.urllib.request.urlopen",
        lambda *args, **kwargs: io.BytesIO(b"tampered"),
    )
    output = tmp_path / "mirror"
    with pytest.raises(ValueError, match="pinned digest"):
        prepare(lock, "amd64", output)
    assert not any(path.is_file() for path in output.rglob("*"))


def test_rust_input_creates_verified_local_mirror_and_checksum_files(tmp_path, monkeypatch) -> None:
    content = b"verified fixture content"
    digest = hashlib.sha256(content).hexdigest()
    lock = _lock(tmp_path, digest)
    monkeypatch.setattr(
        "scripts.sandbox.lock_rust_inputs.urllib.request.urlopen",
        lambda *args, **kwargs: io.BytesIO(content),
    )
    output = tmp_path / "mirror"
    receipt = prepare(lock, "amd64", output)
    assert len(receipt["inputs"]) == 5
    for record in receipt["inputs"]:
        target = output / record["path"]
        assert target.read_bytes() == content
        assert target.with_name(target.name + ".sha256").read_text().startswith(digest + "  ")


def test_rust_input_rejects_version_mismatch_before_network(tmp_path, monkeypatch) -> None:
    lock = _lock(tmp_path, "0" * 64)
    data = json.loads(lock.read_text())
    data["rust_version"] = "1.92.0"
    lock.write_text(json.dumps(data))

    def unexpected(*args, **kwargs):
        pytest.fail("Must validate the manifest version before downloading")

    monkeypatch.setattr("scripts.sandbox.lock_rust_inputs.urllib.request.urlopen", unexpected)
    with pytest.raises(ValueError, match="version differs"):
        prepare(lock, "amd64", tmp_path / "mirror")
