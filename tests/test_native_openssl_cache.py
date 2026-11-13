from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.release.native_openssl_cache import (
    RECEIPT,
    SCRIPTS,
    TARGETS,
    cache_key,
    configuration,
    record,
    reuse_or_clear,
    verify,
)


@pytest.fixture(params=TARGETS)
def installation(tmp_path: Path, request: pytest.FixtureRequest):
    target = request.param
    runner_os, runner_arch, _, builder = TARGETS[target]
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in (builder, "native_openssl_cache.py"):
        shutil.copyfile(SCRIPTS / name, scripts / name)
    environment = {
        "RUNNER_ENVIRONMENT": "github-hosted",
        "RUNNER_OS": runner_os,
        "RUNNER_ARCH": runner_arch,
        "RUNNER_TEMP": str(tmp_path / "runner temp"),
        "ImageOS": "test-image",
        "ImageVersion": "20261110.1",
    }
    prefix, identity = configuration(target, environment, scripts)
    files = ["include/openssl/ssl.h", "include/openssl/opensslv.h"]
    files += (
        ["lib/libssl.lib", "lib/libcrypto.lib"]
        if target == "win32-arm64"
        else ["lib/libssl.a", "lib/libcrypto.a"]
    )
    for name in files:
        path = prefix / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"installed file fixture")
    return prefix, identity, environment, scripts


def test_exact_valid_installation_is_reused_without_rewriting_files(installation):
    prefix, identity, _, _ = installation
    record(prefix, identity)
    before = {p: p.read_bytes() for p in prefix.rglob("*") if p.is_file()}
    assert reuse_or_clear(prefix, identity, exact_hit=True)
    assert {p: p.read_bytes() for p in before} == before


@pytest.mark.parametrize("change", ["modified", "removed", "extra", "empty", "receipt", "identity"])
def test_damaged_installation_is_discarded_before_rebuild(installation, change):
    prefix, identity, _, _ = installation
    record(prefix, identity)
    header = prefix / "include/openssl/ssl.h"
    if change == "modified":
        header.write_bytes(b"different library headers")
    elif change == "removed":
        header.unlink()
    elif change == "extra":
        (prefix / "stale-library.lib").write_bytes(b"stale")
    elif change == "empty":
        header.write_bytes(b"")
    elif change == "receipt":
        (prefix / RECEIPT).write_text("invalid JSON")
    else:
        receipt = json.loads((prefix / RECEIPT).read_text())
        receipt["identity"]["image_version"] = "old-image"
        (prefix / RECEIPT).write_text(json.dumps(receipt))
    with pytest.raises((ValueError, OSError)):
        verify(prefix, identity)
    assert not reuse_or_clear(prefix, identity, exact_hit=True)
    assert not prefix.exists()


def test_partial_hit_is_never_reused_even_with_a_valid_receipt(installation):
    prefix, identity, _, _ = installation
    record(prefix, identity)
    sibling = prefix.parent / "other-build"
    sibling.write_bytes(b"keep")
    assert not reuse_or_clear(prefix, identity, exact_hit=False)
    assert not prefix.exists()
    assert sibling.read_bytes() == b"keep"


def test_cold_miss_needs_no_existing_directory(installation):
    prefix, identity, _, _ = installation
    shutil.rmtree(prefix)
    assert not reuse_or_clear(prefix, identity, exact_hit=False)


@pytest.mark.parametrize("field", ["ImageOS", "ImageVersion", "RUNNER_TEMP"])
def test_runner_or_install_location_changes_invalidate_key(installation, field):
    _, identity, environment, scripts = installation
    changed = dict(environment)
    changed[field] += "-changed"
    _, new_identity = configuration(identity["target"], changed, scripts)
    assert cache_key(new_identity) != cache_key(identity)


@pytest.mark.parametrize("changed_input", ["builder", "validator"])
def test_build_recipe_or_validation_changes_invalidate_key(installation, changed_input):
    _, identity, environment, scripts = installation
    name = (
        TARGETS[identity["target"]][3] if changed_input == "builder" else "native_openssl_cache.py"
    )
    with (scripts / name).open("a") as stream:
        stream.write("\n# changed input\n")
    _, new_identity = configuration(identity["target"], environment, scripts)
    assert cache_key(new_identity) != cache_key(identity)


@pytest.mark.parametrize(
    "field", ["RUNNER_ENVIRONMENT", "RUNNER_OS", "RUNNER_ARCH", "ImageVersion"]
)
def test_unknown_or_wrong_runner_cannot_use_a_cache(installation, field):
    _, identity, environment, scripts = installation
    environment[field] = "" if field == "ImageVersion" else "wrong"
    with pytest.raises(ValueError, match="matching hosted runner"):
        configuration(identity["target"], environment, scripts)


def test_incomplete_build_cannot_get_a_receipt(installation):
    prefix, identity, _, _ = installation
    (prefix / "include/openssl/ssl.h").unlink()
    with pytest.raises(ValueError, match="required file"):
        record(prefix, identity)
    assert not (prefix / RECEIPT).exists()


@pytest.mark.skipif(sys.platform == "win32", reason="requires unprivileged filesystem symlinks")
@pytest.mark.parametrize("redirect", ["prefix", "header", "receipt"])
def test_redirected_cache_is_removed_without_touching_target(installation, tmp_path, redirect):
    prefix, identity, _, _ = installation
    record(prefix, identity)
    outside = tmp_path / "outside"
    outside.mkdir()
    keep = outside / "keep"
    keep.write_bytes(b"unrelated")
    if redirect == "prefix":
        shutil.rmtree(prefix)
        prefix.symlink_to(outside, target_is_directory=True)
    else:
        path = prefix / ("include/openssl/ssl.h" if redirect == "header" else RECEIPT)
        path.unlink()
        path.symlink_to(keep)
    assert not reuse_or_clear(prefix, identity, exact_hit=True)
    assert not prefix.exists()
    assert keep.read_bytes() == b"unrelated"


def test_cli_outputs_support_spaces_and_a_cold_then_warm_cycle(installation, tmp_path):
    prefix, identity, environment, _ = installation
    output = tmp_path / "github output"
    command = [sys.executable, str(SCRIPTS / "native_openssl_cache.py")]
    common = ["--target", identity["target"]]
    env = {**os.environ, **environment}
    subprocess.run(
        command + ["prepare", *common, "--github-output", str(output)], env=env, check=True
    )
    values = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert values["path"] == prefix.as_posix()
    assert values["key"] == cache_key(identity)
    subprocess.run(command + ["record", *common], env=env, check=True)
    subprocess.run(
        command + ["check", *common, "--cache-hit", "true", "--github-output", str(output)],
        env=env,
        check=True,
    )
    assert output.read_text().splitlines()[-1] == "reuse=true"
