"""Catalog-only refusals through raw JSONL, with real dispatch and installer."""

import io
import json

import pytest

from alysis_code.extensions import install as installer
from alysis_code.ide.stdio_bridge import StdioBridge


def request(bridge, out, params):
    out.seek(0)
    out.truncate()
    bridge.process_line(
        json.dumps(
            {
                "protocol_version": "1",
                "id": "catalog",
                "method": "ext.install",
                "params": params,
            }
        )
        + "\n"
    )
    return next(
        json.loads(line)
        for line in out.getvalue().splitlines()
        if json.loads(line).get("id") == "catalog"
    )


@pytest.fixture
def bridge_port(tmp_path, monkeypatch):
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "data"))
    clones = []

    def clone(**kwargs):
        clones.append(kwargs)
        raise installer.PluginInstallError("inert clone reached")

    monkeypatch.setattr(installer, "_clone_pinned_git_repo", clone)
    out = io.StringIO()
    bridge = StdioBridge(stdout=out)
    try:
        yield bridge, out, clones
    finally:
        bridge.close()


@pytest.mark.parametrize(
    "source",
    [
        "git+https://example.invalid/p.git@" + "a" * 40,
        "https://example.invalid/p.git#" + "a" * 40,
        "https://example.invalid/p.git",
        "custom:custom.fixture",
        "../plugin",
        "/tmp/plugin",
    ],
)
@pytest.mark.parametrize("workspace", [False, True])
@pytest.mark.parametrize("approval", [{}, {"yes": True}, {"yes": True, "trust_approval": {}}])
def test_raw_developer_source_refuses_before_clone_review_or_write(
    bridge_port,
    tmp_path,
    source,
    workspace,
    approval,
):
    bridge, out, clones = bridge_port
    params = {"source": source, **approval}
    if workspace:
        params.update(workspace=str(tmp_path), workspace_trusted=True)
    before = {
        str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()
    }
    reply = request(bridge, out, params)
    assert reply["ok"] is False
    assert reply["error"]["code"] == (
        "invalid_extension_source" if workspace else "workspace_trust_required"
    )
    assert clones == []
    assert {
        str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()
    } == before


def test_raw_unknown_catalog_refuses_before_clone(bridge_port, tmp_path):
    bridge, out, clones = bridge_port
    reply = request(
        bridge,
        out,
        {
            "source": "missing.fixture",
            "workspace": str(tmp_path),
            "workspace_trusted": True,
            "yes": True,
        },
    )
    assert reply["ok"] is False
    assert "registry id not found" in reply["error"]["message"]
    assert clones == []


def test_raw_bundled_catalog_still_requires_exact_two_request_review(bridge_port, tmp_path):
    bridge, out, clones = bridge_port
    params = {"source": "alysis.github", "workspace": str(tmp_path), "workspace_trusted": True}
    first = request(bridge, out, params)
    yes_only = request(bridge, out, {**params, "yes": True})
    assert first["ok"] is yes_only["ok"] is True
    assert first["result"]["action"]["kind"] == "requires_extension_trust_review"
    assert yes_only["result"]["action"]["kind"] == "requires_extension_trust_review"
    proof = first["result"]["action"]["required_approval"]
    installed = request(bridge, out, {**params, "yes": True, "trust_approval": proof})
    assert installed["ok"] is True
    assert installed["result"]["result"]["plugin_id"] == "alysis.github"
    assert installed["result"]["changed"] is True
    assert clones == []
