"""The management install route accepts catalog IDs, not developer sources."""

from __future__ import annotations

import pytest

from alysis_code.extensions import install as installer
from alysis_code.ide import management_protocol as management
from alysis_code.ide.protocol import ProtocolError


@pytest.mark.parametrize(
    "source",
    [
        "git+https://example.invalid/plugin.git@" + "a" * 40,
        "https://example.invalid/plugin.git#" + "a" * 40,
        "https://example.invalid/plugin.git",
        "custom:custom.probe",
        "../plugin",
        "/tmp/plugin",
    ],
)
@pytest.mark.parametrize("approval", [{}, {"yes": True}, {"yes": True, "trust_approval": {}}])
def test_developer_source_refused_before_install_or_review(tmp_path, monkeypatch, source, approval):
    calls = []

    def install(**kwargs):
        calls.append(kwargs["source"])
        raise installer.PluginInstallError("synthetic install port reached")

    monkeypatch.setattr(management, "install_plugin", install)
    with pytest.raises(ProtocolError) as error:
        management.handle_management_method(
            "ext.install",
            {
                "workspace": str(tmp_path),
                "workspace_trusted": True,
                "source": source,
                **approval,
            },
            request_id=23,
        )
    assert error.value.code == "invalid_extension_source"
    assert calls == []


def test_capability_reports_catalog_only():
    assert (
        management.MANAGEMENT_METHOD_CAPABILITIES["ext.install"]["source_policy"]
        == "registry_id_only"
    )


def test_unknown_catalog_id_cannot_reach_clone(tmp_path, monkeypatch):
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(
        installer, "_clone_pinned_git_repo", lambda **kwargs: pytest.fail("No clone")
    )
    with pytest.raises(ProtocolError, match="registry id not found"):
        management.handle_management_method(
            "ext.install",
            {
                "workspace": str(tmp_path),
                "workspace_trusted": True,
                "source": "missing.fixture",
                "yes": True,
            },
            request_id=24,
        )
