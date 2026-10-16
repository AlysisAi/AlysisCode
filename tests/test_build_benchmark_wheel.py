from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def benchmark_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo with spaces"
    for name in (
        "scripts/build_benchmark_wheel.sh",
        "scripts/generate_build_info.py",
        "src/alysis_code/__init__.py",
        "src/alysis_code/build_identity.py",
        "src/alysis_code/command_text.py",
        "src/alysis_code/_build_info.py",
    ):
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_ROOT / name, target)
    (repo / ".gitignore").write_text("dist/\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "seed",
        ],
        cwd=repo,
        check=True,
    )
    (repo / "src/alysis_code/_build_info.py").write_text(
        "# User's local build metadata\nBUILD_COMMIT = 'local-edit'\n", encoding="utf-8"
    )
    (repo / "notes.txt").write_text("Uncommitted work\n", encoding="utf-8")
    (repo / "dist").mkdir()
    (repo / "dist/existing.whl").write_bytes(b"existing wheel must survive")

    # Exercise the real stamping/restoration flow while controlling build outcomes.
    # The fake builder packages the actual stamp rather than inventing provenance.
    builder = tmp_path / "builder.py"
    builder.write_text(
        "import os, signal, sys, zipfile\n"
        "from pathlib import Path\n"
        "mode = os.environ.get('BENCHMARK_TEST_BUILD_MODE', 'success')\n"
        "if mode == 'failure':\n"
        "    sys.exit(7)\n"
        "if mode == 'interrupt':\n"
        "    os.kill(os.getppid(), signal.SIGTERM)\n"
        "    sys.exit(0)\n"
        "args = sys.argv[1:]\n"
        "output = Path(args[args.index('--out-dir') + 1]) if '--out-dir' in args else Path('dist')\n"
        "output.mkdir(parents=True, exist_ok=True)\n"
        "with zipfile.ZipFile(output / 'alysis_code-test.whl', 'w') as wheel:\n"
        "    wheel.write('src/alysis_code/_build_info.py', 'alysis_code/_build_info.py')\n",
        encoding="utf-8",
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text(
        f'#!/bin/sh\nexec {shlex.join([sys.executable, str(builder)])} "$@"\n',
        encoding="utf-8",
    )
    uv.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    return repo


@pytest.mark.parametrize(
    ("mode", "args", "expected_exit"),
    [
        ("success", [], 1),
        ("failure", ["--allow-dirty"], 7),
        ("interrupt", ["--allow-dirty"], 143),
        ("success", ["--allow-dirty"], 0),
    ],
)
def test_build_preserves_local_metadata_and_existing_wheels(
    benchmark_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    args: list[str],
    expected_exit: int,
) -> None:
    stamp = benchmark_repo / "src/alysis_code/_build_info.py"
    original = stamp.read_bytes()
    monkeypatch.setenv("BENCHMARK_TEST_BUILD_MODE", mode)

    result = subprocess.run(
        ["bash", "scripts/build_benchmark_wheel.sh", *args],
        cwd=benchmark_repo,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert stamp.read_bytes() == original
    assert (benchmark_repo / "dist/existing.whl").read_bytes() == b"existing wheel must survive"
    assert result.returncode == expected_exit, result.stdout + result.stderr
    if expected_exit == 0:
        wheels = list((benchmark_repo / "dist").glob("benchmark.*/alysis_code-test.whl"))
        assert len(wheels) == 1
        with zipfile.ZipFile(wheels[0]) as wheel:
            built_stamp = wheel.read("alysis_code/_build_info.py").decode()
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=benchmark_repo, text=True
        ).strip()
        assert commit in built_stamp
        assert 'BUILD_SOURCE = "git"' in built_stamp
        assert str(wheels[0]) in result.stdout
