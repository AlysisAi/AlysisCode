import pytest

import hatch_build


def test_local_mac_wheel_cannot_claim_windows_or_release_qualification(tmp_path, monkeypatch):
    monkeypatch.setenv("ALYSIS_LOCAL_CANDIDATE", "1")
    monkeypatch.delenv("ALYSIS_WINDOWS_SANDBOX_BUNDLE", raising=False)
    monkeypatch.setattr(hatch_build.sys, "platform", "darwin")
    monkeypatch.setattr(hatch_build.sysconfig, "get_platform", lambda: "macosx-11.0-arm64")
    hook = hatch_build.CustomBuildHook(str(tmp_path), {}, None, None, "unused", "wheel")
    data = {}
    hook.initialize("standard", data)
    assert data["tag"] == "py3-none-macosx_11_0_arm64"
    assert data["pure_python"] is False
    monkeypatch.setenv("ALYSIS_RELEASE_BUILD", "1")
    with pytest.raises(ValueError, match="non-release"):
        hook.initialize("standard", {})


def test_windows_candidate_still_requires_native_bundle(tmp_path, monkeypatch):
    monkeypatch.setenv("ALYSIS_LOCAL_CANDIDATE", "1")
    monkeypatch.delenv("ALYSIS_WINDOWS_SANDBOX_BUNDLE", raising=False)
    monkeypatch.setattr(hatch_build.sys, "platform", "win32")
    hook = hatch_build.CustomBuildHook(str(tmp_path), {}, None, None, "unused", "wheel")
    with pytest.raises(ValueError, match="bundle and independent"):
        hook.initialize("standard", {})
