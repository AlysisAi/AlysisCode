from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _read(relpath: str) -> str:
    return (REPO_ROOT / relpath).read_text(encoding="utf-8")


def test_readme_does_not_claim_host_verification_by_default() -> None:
    readme = _read("README.md")
    lowered = readme.lower()

    assert "Verification commands are executed by the platform-native shell" not in readme
    assert "host shell by default" not in lowered
    assert "default to strict sandboxing" in readme
    assert "refuses execution when no backend is available" in readme
    assert "docs/shell_sandbox.md" in readme


def test_sandbox_doc_keeps_strict_fail_closed_verification_language() -> None:
    doc = _read("docs/shell_sandbox.md")

    assert "- `network=off`" in doc
    assert "default to strict sandboxing too" in doc
    assert "`shell_sandbox.mode` | `strict`" in doc
    assert "requests never silently fall back to unsandboxed host execution" in doc
    assert "network policy" in doc
