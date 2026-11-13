from types import SimpleNamespace

from alysis_code.bots.browser_trust import PrivateBrowserLauncher
from alysis_code.ide.managed_browser import BrowserLaunchSpec, DefaultBrowserProcessLauncher


def test_private_browser_home_preserves_guarded_flags_and_does_not_inherit_environment(
    monkeypatch, tmp_path
):
    captured = []
    monkeypatch.setenv("HOME", "/untrusted/shared/home")
    monkeypatch.setenv("PRIVATE_PROVIDER_KEY", "must-not-reach-child")
    monkeypatch.setattr(
        DefaultBrowserProcessLauncher,
        "launch",
        lambda self, spec: captured.append(spec) or SimpleNamespace(pid=1),
    )
    spec = BrowserLaunchSpec(
        executable=tmp_path / "chromium",
        arguments=("--proxy-server=127.0.0.1:1234", "about:blank"),
        environment={"LANG": "C.UTF-8"},
        profile_dir=tmp_path / "profile",
    )
    PrivateBrowserLauncher(tmp_path).launch(spec)
    actual = captured[0]
    assert actual.arguments == spec.arguments
    assert actual.environment == {"LANG": "C.UTF-8", "HOME": str(tmp_path)}
    assert not any("ignore-certificate" in arg or "no-sandbox" in arg for arg in actual.arguments)
