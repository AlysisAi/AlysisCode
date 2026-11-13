from __future__ import annotations

import base64
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from alysis_code.ide import managed_browser as managed_browser_module
from alysis_code.ide.managed_browser import (
    BrowserCancelledError,
    BrowserControlError,
    BrowserDependencyError,
    BrowserError,
    BrowserLaunchError,
    BrowserLaunchSpec,
    BrowserLimitError,
    BrowserNotFoundError,
    BrowserOwnershipError,
    BrowserPageUnavailableError,
    BrowserSecurityError,
    BrowserTimeoutError,
    BrowserValidationError,
    DefaultBrowserProcessLauncher,
    ManagedBrowserConfig,
    ManagedBrowserService,
    discover_browser_executable,
    validate_browser_url,
)


def test_default_data_root_honors_alysis_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured_root = tmp_path / "isolated-data"
    monkeypatch.setenv("ALYSIS_DATA_DIR", os.fspath(configured_root))

    assert ManagedBrowserConfig().data_root == configured_root / "ide-browser"


class FakeProcess:
    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        if self.returncode is None:
            raise TimeoutError
        return self.returncode

    def terminate(self) -> None:
        self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


class FakeLauncher:
    def __init__(self, *, active_port: str = "9223\n/devtools/browser/browser-1\n") -> None:
        self.active_port = active_port
        self.specs: list[BrowserLaunchSpec] = []
        self.processes: list[FakeProcess] = []

    def launch(self, spec: BrowserLaunchSpec) -> FakeProcess:
        self.specs.append(spec)
        process = FakeProcess(pid=4200 + len(self.specs))
        self.processes.append(process)
        (spec.profile_dir / "DevToolsActivePort").write_text(self.active_port, encoding="ascii")
        return process


class FakeTerminator:
    def __init__(self) -> None:
        self.calls: list[tuple[int, float]] = []

    def terminate(self, process: FakeProcess, *, grace_seconds: float) -> None:
        self.calls.append((process.pid, grace_seconds))
        process.kill()


class FakeTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], str | None]] = []
        self.closed = False
        self.redirects: list[str] = []
        self.events: list[dict[str, Any]] = []
        self.navigation_entered: threading.Event | None = None
        self.navigation_release: threading.Event | None = None

    @property
    def healthy(self) -> bool:
        return not self.closed

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
        timeout: float,
        cancel=None,
    ) -> dict[str, Any]:
        del timeout
        if cancel is not None and cancel():
            raise BrowserCancelledError("Browser operation was cancelled.")
        values = dict(params or {})
        self.calls.append((method, values, session_id))
        if method == "Target.createTarget":
            return {"targetId": "target-1"}
        if method == "Target.attachToTarget":
            return {"sessionId": "cdp-session-1"}
        if method == "DOM.getDocument":
            return {"root": {"nodeId": 1}}
        if method == "DOM.querySelector":
            return {"nodeId": 9}
        if method == "DOM.describeNode":
            return {"node": {"backendNodeId": 90}}
        if method == "DOM.getBoxModel":
            return {"model": {"content": [0, 0, 100, 0, 100, 20, 0, 20]}}
        if method == "DOM.getNodeForLocation":
            return {"backendNodeId": 90}
        if method == "Runtime.evaluate":
            return {"result": {"value": "page text token=super-secret-value"}}
        if method == "Accessibility.getPartialAXTree":
            return {
                "nodes": [
                    {
                        "backendDOMNodeId": 90,
                        "properties": [{"name": "focused", "value": {"value": True}}],
                    }
                ]
            }
        if method == "Accessibility.getFullAXTree":
            return {"nodes": [{"role": {"value": "button"}}]}
        if method == "DOMSnapshot.captureSnapshot":
            return {"documents": [{"nodes": [1, 2, 3]}]}
        if method == "Page.captureScreenshot":
            return {"data": base64.b64encode(b"\x89PNG\r\n\x1a\nsmall").decode("ascii")}
        return {}

    def guarded_navigate(
        self,
        url: str,
        *,
        session_id: str,
        authorize_url,
        timeout: float,
        cancel=None,
        root_target_id=None,
    ) -> dict[str, Any]:
        del timeout
        authorize_url(url)
        for redirect in self.redirects:
            authorize_url(redirect)
        if self.navigation_entered is not None:
            self.navigation_entered.set()
        if self.navigation_release is not None:
            self.navigation_release.wait(timeout=2)
        if cancel is not None and cancel():
            raise BrowserCancelledError("Browser operation was cancelled.")
        self.calls.append(("guarded_navigate", {"url": url}, session_id))
        return {"frameId": "frame-1"}

    def drain_events(
        self,
        *,
        session_id: str,
        max_events: int,
        timeout: float,
        cancel=None,
    ) -> list[dict[str, Any]]:
        del session_id, timeout, cancel
        return self.events[:max_events]

    def close(self, *, timeout: float) -> None:
        del timeout
        self.closed = True


class FakeFactory:
    def __init__(self, transport: FakeTransport | None = None) -> None:
        self.transport = transport or FakeTransport()
        self.endpoints: list[str] = []

    def connect(self, websocket_url: str, *, timeout: float, cancel=None) -> FakeTransport:
        del timeout, cancel
        self.endpoints.append(websocket_url)
        return self.transport


class FakeEgressProxy:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.host = "127.0.0.1"
        self.port = 43123
        self.healthy = False
        self.terminal_error: BaseException | None = None
        self.close_calls: list[float] = []
        self.denied_endpoints: list[tuple[str, int]] = []

    @property
    def proxy_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> FakeEgressProxy:
        self.healthy = True
        return self

    def close(self, *, timeout: float = 2.0) -> None:
        self.close_calls.append(timeout)
        self.healthy = False

    def deny_endpoint(self, host: str, port: int) -> None:
        self.denied_endpoints.append((host, port))


class FakeEgressProxyFactory:
    def __init__(self) -> None:
        self.instances: list[FakeEgressProxy] = []

    def __call__(self, **kwargs: Any) -> FakeEgressProxy:
        proxy = FakeEgressProxy(**kwargs)
        self.instances.append(proxy)
        return proxy


def public_resolver(host: str, port: int) -> tuple[str, ...]:
    del host, port
    return ("93.184.216.34",)


def private_resolver(host: str, port: int) -> tuple[str, ...]:
    del host, port
    return ("127.0.0.1",)


def make_service(tmp_path: Path, **overrides: Any):
    executable = tmp_path / ("chrome.exe" if os.name == "nt" else "google-chrome")
    executable.write_bytes(b"fake")
    executable.chmod(0o700)
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    config = ManagedBrowserConfig(
        data_root=tmp_path / "private-browser-data",
        workspace_roots=(workspace,),
        max_sessions_total=overrides.pop("max_sessions_total", 3),
        max_sessions_per_owner=overrides.pop("max_sessions_per_owner", 2),
        max_snapshot_bytes=overrides.pop("max_snapshot_bytes", 2 * 1024 * 1024),
        linux_display=overrides.pop("linux_display", None),
    )
    launcher = FakeLauncher(
        active_port=overrides.pop("active_port", "9223\n/devtools/browser/id-1\n")
    )
    terminator = FakeTerminator()
    factory = FakeFactory(overrides.pop("transport", None))
    egress_proxy_factory = overrides.pop("egress_proxy_factory", FakeEgressProxyFactory())
    service = ManagedBrowserService(
        config=config,
        launcher=launcher,
        terminator=terminator,
        transport_factory=factory,
        resolver=overrides.pop("resolver", public_resolver),
        egress_proxy_factory=egress_proxy_factory,
    )
    assert not overrides
    return service, executable, launcher, terminator, factory


def _write_owned_marker(
    path: Path,
    session_id: str,
    owner_pid: int,
    *,
    owner_identity: str | None = None,
    browser_pid: int | None = None,
    browser_identity: str | None = None,
) -> None:
    path.mkdir(parents=True)
    (path / ".alysis-owned").write_text(
        json.dumps(
            {
                "schema_version": 3,
                "session_id": session_id,
                "owner_pid": owner_pid,
                "owner_identity": owner_identity or f"owner-{owner_pid}",
                "browser_pid": browser_pid,
                "browser_identity": browser_identity,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="ascii",
    )


def test_process_liveness_probe_is_read_only_for_current_process() -> None:
    assert managed_browser_module._process_identity(os.getpid()) is not None
    assert managed_browser_module._process_is_alive(os.getpid()) is True


def test_startup_scavenges_only_versioned_dirs_from_dead_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root = tmp_path / "private-browser-data"
    stale_session = "staleSessionIdentifier01"
    live_session = "liveSessionIdentifier001"
    legacy_session = "legacySessionIdentifier1"
    stale_dirs = [data_root / root / f"{stale_session}-stale" for root in ("profiles", "artifacts")]
    live_dir = data_root / "profiles" / f"{live_session}-live"
    legacy_dir = data_root / "artifacts" / f"{legacy_session}-legacy"
    untrusted_dir = data_root / "profiles" / "untrusted"
    for path in stale_dirs:
        _write_owned_marker(path, stale_session, 1111)
    _write_owned_marker(live_dir, live_session, 2222)
    legacy_dir.mkdir(parents=True)
    (legacy_dir / ".alysis-owned").write_text(legacy_session, encoding="ascii")
    untrusted_dir.mkdir(parents=True)
    (untrusted_dir / ".alysis-owned").write_text("not-json", encoding="ascii")
    monkeypatch.setattr(
        managed_browser_module,
        "_process_identity",
        lambda pid: "owner-2222" if pid == 2222 else None,
    )

    make_service(tmp_path)

    assert all(not path.exists() for path in stale_dirs)
    assert live_dir.is_dir()
    assert legacy_dir.is_dir()
    assert untrusted_dir.is_dir()


def test_startup_fails_closed_when_stale_owned_cleanup_cannot_finish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = "staleSessionIdentifier01"
    stale_dir = tmp_path / "private-browser-data" / "profiles" / f"{session_id}-stale"
    _write_owned_marker(stale_dir, session_id, 1111)
    monkeypatch.setattr(managed_browser_module, "_process_identity", lambda _pid: None)
    monkeypatch.setattr(managed_browser_module, "_remove_owned_session_dir", lambda *_args: False)

    with pytest.raises(BrowserSecurityError, match="cleanup is incomplete"):
        make_service(tmp_path)


def test_startup_terminates_exact_orphan_browser_before_removing_owned_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_root = tmp_path / "private-browser-data"
    session_id = "staleSessionIdentifier01"
    paths = [data_root / root / f"{session_id}-stale" for root in ("profiles", "artifacts")]
    for path in paths:
        _write_owned_marker(
            path,
            session_id,
            1111,
            owner_identity="old-owner",
            browser_pid=3333,
            browser_identity="exact-browser",
        )
    identities = {1111: None, 3333: "exact-browser"}
    terminated: list[tuple[int, str]] = []
    monkeypatch.setattr(
        managed_browser_module, "_process_identity", lambda pid: identities.get(pid)
    )

    def terminate(pid: int, identity: str, *, grace_seconds: float) -> bool:
        assert grace_seconds > 0
        terminated.append((pid, identity))
        identities[pid] = None
        return True

    monkeypatch.setattr(managed_browser_module, "_terminate_recovered_browser_process", terminate)

    make_service(tmp_path)

    assert terminated == [(3333, "exact-browser")]
    assert all(not path.exists() for path in paths)


def test_startup_does_not_delete_recovery_data_when_orphan_cannot_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = "staleSessionIdentifier01"
    stale_dir = tmp_path / "private-browser-data" / "profiles" / f"{session_id}-stale"
    _write_owned_marker(
        stale_dir,
        session_id,
        1111,
        owner_identity="old-owner",
        browser_pid=3333,
        browser_identity="exact-browser",
    )
    monkeypatch.setattr(managed_browser_module, "_process_identity", lambda _pid: None)
    monkeypatch.setattr(
        managed_browser_module,
        "_terminate_recovered_browser_process",
        lambda *_args, **_kwargs: False,
    )

    with pytest.raises(BrowserSecurityError, match="cleanup is incomplete"):
        make_service(tmp_path)

    assert stale_dir.is_dir()


def test_startup_does_not_confuse_reused_owner_pid_with_original_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = "staleSessionIdentifier01"
    stale_dir = tmp_path / "private-browser-data" / "profiles" / f"{session_id}-stale"
    _write_owned_marker(
        stale_dir,
        session_id,
        2222,
        owner_identity="original-process",
    )
    monkeypatch.setattr(
        managed_browser_module,
        "_process_identity",
        lambda pid: "unrelated-reused-pid" if pid == 2222 else None,
    )

    make_service(tmp_path)

    assert not stale_dir.exists()


@pytest.mark.parametrize(
    "url",
    (
        "file:///etc/passwd",
        "javascript:alert(1)",
        "data:text/html,hello",
        "ftp://example.com/file",
        "https://user:password@example.com/",
    ),
)
def test_url_policy_rejects_non_web_schemes_and_credentials(url: str) -> None:
    with pytest.raises(BrowserSecurityError):
        validate_browser_url(url, resolver=public_resolver)


@pytest.mark.parametrize(
    "url",
    (
        "http://127.0.0.1/",
        "http://[::1]/",
        "http://169.254.169.254/latest/meta-data/",
        "http://localhost/",
        "http://service.local/",
    ),
)
def test_url_policy_rejects_local_and_private_destinations(url: str) -> None:
    with pytest.raises(BrowserSecurityError):
        validate_browser_url(url, resolver=private_resolver)


def test_url_policy_rejects_mixed_public_private_dns_answers() -> None:
    with pytest.raises(BrowserSecurityError):
        validate_browser_url(
            "https://example.test",
            resolver=lambda _host, _port: ("93.184.216.34", "10.0.0.1"),
        )


def test_url_policy_normalizes_public_url_and_has_explicit_local_opt_in() -> None:
    assert (
        validate_browser_url("HTTPS://Example.COM?q=1", resolver=public_resolver)
        == "https://example.com/?q=1"
    )


def test_url_resolution_has_a_hard_timeout() -> None:
    release = threading.Event()

    def hanging_resolver(_host: str, _port: int) -> tuple[str, ...]:
        release.wait(timeout=2)
        return ("93.184.216.34",)

    try:
        with pytest.raises(TimeoutError, match="resolution timed out"):
            validate_browser_url(
                "https://example.test",
                resolver=hanging_resolver,
                resolution_timeout=0.05,
            )
    finally:
        release.set()
    assert (
        validate_browser_url("http://127.0.0.1:3000/test", allow_local_destinations=True)
        == "http://127.0.0.1:3000/test"
    )


def test_url_policy_loopback_scope_denies_private_lan() -> None:
    assert (
        validate_browser_url(
            "http://localhost:3000/test",
            allow_local_destinations=True,
            local_destinations_loopback_only=True,
            resolver=lambda _host, _port: ("127.0.0.1",),
        )
        == "http://localhost:3000/test"
    )
    with pytest.raises(BrowserSecurityError, match="public and loopback"):
        validate_browser_url(
            "http://devbox.test:3000/test",
            allow_local_destinations=True,
            local_destinations_loopback_only=True,
            resolver=lambda _host, _port: ("192.168.1.20",),
        )


def test_discovery_requires_absolute_explicit_path(tmp_path: Path) -> None:
    with pytest.raises(BrowserValidationError):
        discover_browser_executable("chrome.exe")
    executable = tmp_path / ("msedge.exe" if os.name == "nt" else "microsoft-edge")
    executable.write_bytes(b"fake")
    executable.chmod(0o700)
    discovered = discover_browser_executable(executable)
    assert discovered.path == executable.resolve()
    assert discovered.product == "edge"
    assert discovered.source == "explicit"


def test_start_is_owned_loopback_isolated_and_has_sanitized_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "do-not-leak-this-secret")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:9999")
    monkeypatch.setenv("DISPLAY", "remote.invalid:0")
    service, executable, launcher, _terminator, factory = make_service(tmp_path)

    status = service.start("workspace:one", executable_path=executable)

    assert status.state == "running"
    assert factory.endpoints == ["ws://127.0.0.1:9223/devtools/browser/id-1"]
    spec = launcher.specs[0]
    assert "--remote-debugging-address=127.0.0.1" in spec.arguments
    assert "--remote-debugging-port=0" in spec.arguments
    assert "--no-proxy-server" not in spec.arguments
    assert "--proxy-server=http://127.0.0.1:43123" in spec.arguments
    assert "--proxy-bypass-list=<-loopback>" in spec.arguments
    assert "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1" in spec.arguments
    assert "--disable-quic" in spec.arguments
    assert "--force-webrtc-ip-handling-policy=disable_non_proxied_udp" in spec.arguments
    assert str(tmp_path / "workspace") not in str(spec.profile_dir)
    assert "OPENAI_API_KEY" not in spec.environment
    assert "HTTPS_PROXY" not in spec.environment
    assert "NO_PROXY" not in spec.environment
    assert "no_proxy" not in spec.environment
    assert "DISPLAY" not in spec.environment
    assert "--headless=new" in spec.arguments
    assert factory.transport.calls[2][:3] == (
        "Browser.setDownloadBehavior",
        {"behavior": "deny", "eventsEnabled": False},
        None,
    )
    assert all(call[2] == "cdp-session-1" for call in factory.transport.calls[3:])
    session = service._sessions[status.session_id]
    assert session.egress_proxy.denied_endpoints == [
        ("127.0.0.1", 43123),
        ("127.0.0.1", 9223),
    ]


def test_visible_browser_uses_only_explicit_local_display_and_keeps_egress_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DISPLAY", "remote.invalid:0")
    monkeypatch.setenv("API_KEY", "not-in-browser")
    service, executable, launcher, _terminator, factory = make_service(
        tmp_path, linux_display=":99"
    )
    status = service.start("visible-lab", executable_path=executable)
    spec = launcher.specs[0]
    assert spec.environment["DISPLAY"] == ":99"
    assert "API_KEY" not in spec.environment
    assert "--headless=new" not in spec.arguments
    assert "--ozone-platform=x11" in spec.arguments
    assert "--no-sandbox" not in spec.arguments
    assert "--proxy-server=http://127.0.0.1:43123" in spec.arguments
    assert "--proxy-bypass-list=<-loopback>" in spec.arguments
    assert factory.transport.calls[0][1]["background"] is False
    with pytest.raises(BrowserSecurityError):
        service.navigate("visible-lab", status.session_id, "http://127.0.0.1:7070")
    with pytest.raises(BrowserOwnershipError):
        service.snapshot("another-bot", status.session_id)
    service.close_all()


@pytest.mark.parametrize("display", [True, 99, "", "localhost:0", "host:99", ":99\n", ":1;echo"])
def test_visible_browser_rejects_nonlocal_or_malformed_displays(display) -> None:
    with pytest.raises(BrowserValidationError):
        ManagedBrowserConfig(linux_display=display)


def test_default_launcher_never_uses_a_shell_and_isolates_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / ("chrome.exe" if os.name == "nt" else "chrome")
    executable.write_bytes(b"fake")
    executable.chmod(0o700)
    profile = tmp_path / "profile"
    profile.mkdir()
    captured: dict[str, Any] = {}
    sentinel = FakeProcess()

    def fake_popen(**kwargs: Any) -> FakeProcess:
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr("alysis_code.ide.managed_browser.subprocess.Popen", fake_popen)
    spec = BrowserLaunchSpec(
        executable=executable.resolve(),
        arguments=("--headless=new",),
        environment={"TEMP": str(tmp_path)},
        profile_dir=profile,
    )
    assert DefaultBrowserProcessLauncher().launch(spec) is sentinel
    assert captured["args"] == [str(executable.resolve()), "--headless=new"]
    assert captured["shell"] is False
    assert captured["close_fds"] is True
    if os.name == "nt":
        assert captured["creationflags"]
        assert "start_new_session" not in captured
    else:
        assert captured["start_new_session"] is True
        assert "creationflags" not in captured


def test_missing_transport_fails_before_launch(tmp_path: Path) -> None:
    service = ManagedBrowserService(
        config=ManagedBrowserConfig(data_root=tmp_path / "data"),
        transport_factory=None,
    )
    with pytest.raises(BrowserDependencyError, match="CDP WebSocket transport"):
        service.start("owner", executable_path=tmp_path / "does-not-matter.exe")


def test_egress_proxy_start_failure_launches_no_browser_and_cleans_dirs(
    tmp_path: Path,
) -> None:
    class FailingEgressProxy(FakeEgressProxy):
        def start(self) -> FakeEgressProxy:
            raise RuntimeError("bind failed with secret detail")

    proxy_factory = FakeEgressProxyFactory()

    def create_proxy(**kwargs: Any) -> FakeEgressProxy:
        proxy = FailingEgressProxy(**kwargs)
        proxy_factory.instances.append(proxy)
        return proxy

    service, executable, launcher, _terminator, _factory = make_service(
        tmp_path,
        egress_proxy_factory=create_proxy,
    )
    with pytest.raises(BrowserLaunchError, match="could not be initialized"):
        service.start("owner", executable_path=executable)
    assert launcher.specs == []
    assert proxy_factory.instances[0].close_calls == [2.0]
    assert not list((tmp_path / "private-browser-data" / "profiles").glob("*"))
    assert not list((tmp_path / "private-browser-data" / "artifacts").glob("*"))


def test_workspace_contained_data_root_is_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(BrowserSecurityError):
        ManagedBrowserService(
            config=ManagedBrowserConfig(
                data_root=workspace / ".browser",
                workspace_roots=(workspace,),
            ),
            transport_factory=FakeFactory(),
        )


def test_invalid_devtools_metadata_terminates_owned_process_and_removes_profile(
    tmp_path: Path,
) -> None:
    cleanup_order: list[str] = []

    class OrderedProxy(FakeEgressProxy):
        def close(self, *, timeout: float = 2.0) -> None:
            cleanup_order.append("proxy")
            super().close(timeout=timeout)

    proxy_factory = FakeEgressProxyFactory()

    def create_proxy(**kwargs: Any) -> FakeEgressProxy:
        proxy = OrderedProxy(**kwargs)
        proxy_factory.instances.append(proxy)
        return proxy

    service, executable, launcher, terminator, _factory = make_service(
        tmp_path,
        active_port="9223\nws://attacker.invalid/devtools/browser/id\n",
        egress_proxy_factory=create_proxy,
    )
    original_terminate = terminator.terminate

    def terminate(process: FakeProcess, *, grace_seconds: float) -> None:
        cleanup_order.append("process")
        original_terminate(process, grace_seconds=grace_seconds)

    terminator.terminate = terminate  # type: ignore[method-assign]
    with pytest.raises(BrowserSecurityError):
        service.start("owner", executable_path=executable)
    assert terminator.calls == [(launcher.processes[0].pid, 2.0)]
    assert proxy_factory.instances[0].close_calls == [2.0]
    assert cleanup_order == ["proxy", "process"]
    assert not list((tmp_path / "private-browser-data" / "profiles").glob("*"))


def test_start_rejects_non_boolean_local_destination_policy(tmp_path: Path) -> None:
    service, executable, launcher, _terminator, _factory = make_service(tmp_path)
    with pytest.raises(BrowserValidationError, match="must be a boolean"):
        service.start(
            "owner",
            executable_path=executable,
            allow_local_destinations="false",  # type: ignore[arg-type]
        )
    assert launcher.specs == []


def test_startup_cleanup_failure_is_visible_after_process_termination(tmp_path: Path) -> None:
    class FailingCloseProxy(FakeEgressProxy):
        def close(self, *, timeout: float = 2.0) -> None:
            self.close_calls.append(timeout)
            raise RuntimeError("proxy cleanup failed")

    proxies: list[FailingCloseProxy] = []

    def create_proxy(**kwargs: Any) -> FailingCloseProxy:
        proxy = FailingCloseProxy(**kwargs)
        proxies.append(proxy)
        return proxy

    service, executable, launcher, terminator, _factory = make_service(
        tmp_path,
        active_port="invalid",
        egress_proxy_factory=create_proxy,
    )
    with pytest.raises(BrowserLaunchError, match="resource cleanup is incomplete"):
        service.start("owner", executable_path=executable)
    assert proxies[0].close_calls == [2.0]
    assert terminator.calls == [(launcher.processes[0].pid, 2.0)]


def test_redirects_are_reauthorized_and_private_redirect_is_blocked(tmp_path: Path) -> None:
    transport = FakeTransport()
    transport.redirects = ["http://127.0.0.1/admin"]
    service, executable, _launcher, _terminator, _factory = make_service(
        tmp_path, transport=transport
    )
    status = service.start("owner", executable_path=executable)
    with pytest.raises(BrowserSecurityError):
        service.navigate("owner", status.session_id, "https://example.test/")


def test_explicit_local_opt_in_applies_to_guarded_navigation(tmp_path: Path) -> None:
    proxy_factory = FakeEgressProxyFactory()
    service, executable, _launcher, _terminator, _factory = make_service(
        tmp_path,
        egress_proxy_factory=proxy_factory,
    )
    status = service.start("owner", executable_path=executable, allow_local_destinations=True)
    assert proxy_factory.instances[0].kwargs["allow_local_destinations"] is True
    result = service.navigate("owner", status.session_id, "http://127.0.0.1:3000/")
    assert result["url"] == "http://127.0.0.1:3000/"
    assert service.status("owner", status.session_id).active_url == result["url"]


def test_session_owned_preview_origin_is_live_and_port_exact(tmp_path: Path) -> None:
    preview_urls = {"http://127.0.0.1:3000/"}
    service, executable, _launcher, _terminator, _factory = make_service(
        tmp_path,
        resolver=private_resolver,
    )
    status = service.start(
        "owner",
        executable_path=executable,
        allowed_preview_urls_provider=lambda: tuple(preview_urls),
    )

    result = service.navigate(
        "owner",
        status.session_id,
        "http://127.0.0.1:3000/incidents/42?view=mobile",
    )
    assert result["url"] == "http://127.0.0.1:3000/incidents/42?view=mobile"

    with pytest.raises(BrowserSecurityError, match="Local and private"):
        service.navigate("owner", status.session_id, "http://127.0.0.1:3001/")

    preview_urls.clear()
    with pytest.raises(BrowserSecurityError, match="Local and private"):
        service.navigate("owner", status.session_id, "http://127.0.0.1:3000/")


def test_loopback_navigation_without_owned_preview_stays_blocked(tmp_path: Path) -> None:
    service, executable, _launcher, _terminator, _factory = make_service(
        tmp_path,
        resolver=private_resolver,
    )
    status = service.start("owner", executable_path=executable)

    with pytest.raises(BrowserSecurityError, match="Local and private"):
        service.navigate("owner", status.session_id, "http://127.0.0.1:3000/")


def test_snapshot_is_bounded_and_secret_redacted(tmp_path: Path) -> None:
    service, executable, _launcher, _terminator, _factory = make_service(
        tmp_path, max_snapshot_bytes=20
    )
    status = service.start("owner", executable_path=executable)
    result = service.snapshot("owner", status.session_id, kind="text")
    assert result["truncated"] is True
    assert "super-secret-value" not in result["text"]


def test_screenshot_is_private_contained_and_hashed(tmp_path: Path) -> None:
    service, executable, _launcher, _terminator, _factory = make_service(tmp_path)
    status = service.start("owner", executable_path=executable)
    artifact = service.screenshot("owner", status.session_id)
    artifact_path = tmp_path / "private-browser-data" / "artifacts" / artifact.relative_path
    assert artifact_path.read_bytes().startswith(b"\x89PNG")
    assert artifact.size_bytes == artifact_path.stat().st_size
    assert artifact.sha256
    first = service.read_artifact("owner", status.session_id, artifact.artifact_id, max_bytes=8)
    assert base64.b64decode(first["content"]) == b"\x89PNG\r\n\x1a\n"
    assert first["truncated"] is True
    second = service.read_artifact(
        "owner",
        status.session_id,
        artifact.artifact_id,
        offset=first["next_offset"],
        max_bytes=8,
    )
    assert base64.b64decode(second["content"]) == b"small"
    assert second["truncated"] is False
    with pytest.raises(BrowserValidationError):
        service.read_artifact(
            "owner", status.session_id, f"browser:{status.session_id}:../secret.png"
        )
    if os.name != "nt":
        assert artifact_path.stat().st_mode & 0o077 == 0


def test_screenshot_refuses_replaced_artifact_ownership_marker(tmp_path: Path) -> None:
    service, executable, _launcher, _terminator, _factory = make_service(tmp_path)
    status = service.start("owner", executable_path=executable)
    artifact_dirs = list((tmp_path / "private-browser-data" / "artifacts").glob("*"))
    assert len(artifact_dirs) == 1
    (artifact_dirs[0] / ".alysis-owned").write_text("different-session", encoding="ascii")
    with pytest.raises(BrowserSecurityError, match="no longer trusted"):
        service.screenshot("owner", status.session_id)


def test_click_and_type_use_structured_cdp_without_script_interpolation(tmp_path: Path) -> None:
    service, executable, _launcher, _terminator, factory = make_service(tmp_path)
    status = service.start("owner", executable_path=executable)
    malicious_selector = "input[data-value=\"'); fetch('http://bad') //\"]"
    service.click("owner", status.session_id, malicious_selector)
    service.type_text("owner", status.session_id, malicious_selector, "hello'\"<world>")
    methods = [call[0] for call in factory.transport.calls]
    assert "DOM.querySelector" in methods
    assert "Input.dispatchMouseEvent" in methods
    assert "Input.insertText" in methods
    assert not any("fetch(" in method for method in methods)
    query_calls = [call for call in factory.transport.calls if call[0] == "DOM.querySelector"]
    assert query_calls[0][1]["selector"] == malicious_selector
    select_all = [
        call
        for call in factory.transport.calls
        if call[0] == "Input.dispatchKeyEvent" and call[1].get("key") == "a"
    ]
    assert {call[1]["modifiers"] for call in select_all} == (
        {4} if os.sys.platform == "darwin" else {2}
    )


def test_click_measures_viewport_coordinates_after_scrolling(tmp_path: Path) -> None:
    class ScrollingTransport(FakeTransport):
        scrolled = False

        def call(self, method, params=None, **kwargs):
            result = super().call(method, params, **kwargs)
            if method == "DOM.scrollIntoViewIfNeeded":
                self.scrolled = True
            if method == "DOM.getBoxModel" and not self.scrolled:
                return {"model": {"content": [0, 1800, 100, 1800, 100, 1820, 0, 1820]}}
            return result

    transport = ScrollingTransport()
    service, executable, *_ = make_service(tmp_path, transport=transport)
    status = service.start("owner", executable_path=executable)
    service.click("owner", status.session_id, "button")
    clicks = [
        params for method, params, _ in transport.calls if method == "Input.dispatchMouseEvent"
    ]
    assert [event["type"] for event in clicks] == ["mousePressed", "mouseReleased"]
    assert all((event["x"], event["y"]) == (50, 10) for event in clicks)


@pytest.mark.parametrize("cancel_after_scroll", [False, True])
def test_failed_or_cancelled_scroll_never_dispatches_click(tmp_path: Path, cancel_after_scroll):
    class FailedScrollTransport(FakeTransport):
        scrolled = False

        def call(self, method, params=None, **kwargs):
            result = super().call(method, params, **kwargs)
            if method == "DOM.scrollIntoViewIfNeeded":
                self.scrolled = True
                if not cancel_after_scroll:
                    raise BrowserNotFoundError("Element has no layout box")
            return result

    transport = FailedScrollTransport()
    service, executable, *_ = make_service(tmp_path, transport=transport)
    status = service.start("owner", executable_path=executable)
    error = BrowserCancelledError if cancel_after_scroll else BrowserNotFoundError
    with pytest.raises(error):
        service.click(
            "owner",
            status.session_id,
            "button",
            cancel=lambda: cancel_after_scroll and transport.scrolled,
        )
    assert not any(method == "Input.dispatchMouseEvent" for method, *_ in transport.calls)


@pytest.mark.parametrize("kind", ["click", "type_text"])
def test_observed_node_identity_prevents_input_to_replacement(tmp_path: Path, kind: str):
    service, executable, _, _, factory = make_service(tmp_path)
    status = service.start("owner", executable_path=executable)
    args = ("value",) if kind == "type_text" else ()
    with pytest.raises(BrowserNotFoundError, match="fresh snapshot"):
        getattr(service, kind)(
            "owner", status.session_id, "input", *args, expected_backend_node_id=89
        )
    methods = [method for method, *_ in factory.transport.calls]
    assert "DOM.describeNode" in methods
    assert "DOM.focus" not in methods
    assert "DOM.scrollIntoViewIfNeeded" not in methods
    assert not any(method.startswith("Input.") for method in methods)
    getattr(service, kind)("owner", status.session_id, "input", *args, expected_backend_node_id=90)
    assert any(method.startswith("Input.") for method, *_ in factory.transport.calls)


@pytest.mark.parametrize(
    "hit,nested,allowed", [(91, False, False), (91, True, True), (None, False, False)]
)
def test_observed_click_rejects_overlay_but_accepts_nested_label(tmp_path, hit, nested, allowed):
    from alysis_code.ide.managed_browser import BrowserTargetObscuredError

    class CoveredTransport(FakeTransport):
        def call(self, method, params=None, **kwargs):
            result = super().call(method, params, **kwargs)
            if method == "DOM.getNodeForLocation":
                return {"backendNodeId": hit}
            if method == "DOM.describeNode" and nested:
                result["node"]["children"] = [{"backendNodeId": 91}]
            return result

    transport = CoveredTransport()
    service, executable, *_ = make_service(tmp_path, transport=transport)
    status = service.start("owner", executable_path=executable)
    if allowed:
        service.click("owner", status.session_id, "button", expected_backend_node_id=90)
    else:
        with pytest.raises(BrowserTargetObscuredError):
            service.click("owner", status.session_id, "button", expected_backend_node_id=90)
    clicks = [m for m, *_ in transport.calls if m == "Input.dispatchMouseEvent"]
    assert len(clicks) == (2 if allowed else 0)


@pytest.mark.parametrize(
    "hit,labels,allowed", [(77, [77, 78], True), (79, [77, 78], False), (77, [], False)]
)
def test_a_click_on_the_targets_own_label_counts_as_the_target(tmp_path, hit, labels, allowed):
    """Styled checkboxes show their label over the real input; a click there activates it."""
    from alysis_code.ide.managed_browser import BrowserTargetObscuredError

    class LabelTransport(FakeTransport):
        def call(self, method, params=None, **kwargs):
            result = super().call(method, params, **kwargs)
            if method == "DOM.getNodeForLocation":
                return {"backendNodeId": hit}
            return result

    transport = LabelTransport()
    service, executable, *_ = make_service(tmp_path, transport=transport)
    status = service.start("owner", executable_path=executable)
    click = lambda: service.click(  # noqa: E731
        "owner",
        status.session_id,
        "input",
        expected_backend_node_id=90,
        label_backend_node_ids=labels,
    )
    if allowed:
        click()
    else:
        with pytest.raises(BrowserTargetObscuredError):
            click()
    clicks = [m for m, *_ in transport.calls if m == "Input.dispatchMouseEvent"]
    assert len(clicks) == (2 if allowed else 0)


@pytest.mark.parametrize("label_hit,allowed", [(78, True), (1, False)])
def test_a_hidden_input_is_clicked_where_its_own_label_shows(tmp_path, label_hit, allowed):
    """A visually hidden checkbox sits under its label: click the label's centre instead."""
    from alysis_code.ide.managed_browser import BrowserTargetObscuredError

    class HiddenInputTransport(FakeTransport):
        def call(self, method, params=None, **kwargs):
            result = super().call(method, params, **kwargs)
            params = params or {}
            if method == "DOM.describeNode" and params.get("backendNodeId") == 77:
                return {"node": {"backendNodeId": 77, "nodeName": "LABEL"}}
            if method == "DOM.describeNode" and params.get("backendNodeId") == 78:
                return {"node": {"backendNodeId": 78, "nodeName": "SPAN"}}
            if method == "DOM.getBoxModel" and params.get("backendNodeId") == 77:
                return {"model": {"content": [200, 100, 300, 100, 300, 120, 200, 120]}}
            if method == "DOM.getNodeForLocation":
                # The input's own centre is covered by its container; the label's is the label.
                return {
                    "backendNodeId": label_hit if (params["x"], params["y"]) == (250, 110) else 1
                }
            return result

    transport = HiddenInputTransport()
    service, executable, *_ = make_service(tmp_path, transport=transport)
    status = service.start("owner", executable_path=executable)
    click = lambda: service.click(  # noqa: E731
        "owner",
        status.session_id,
        "input",
        expected_backend_node_id=90,
        label_backend_node_ids=[78, 77],
    )
    if allowed:
        click()
    else:
        with pytest.raises(BrowserTargetObscuredError):
            click()
    clicks = [
        values for method, values, _ in transport.calls if method == "Input.dispatchMouseEvent"
    ]
    assert [(c["x"], c["y"]) for c in clicks] == ([(250, 110), (250, 110)] if allowed else [])


@pytest.mark.parametrize("labels", [[0], [-3], ["77"], [True], list(range(1, 600)), "77"])
def test_invalid_label_nodes_are_refused_before_any_click(tmp_path, labels):
    service, executable, _, _, factory = make_service(tmp_path)
    status = service.start("owner", executable_path=executable)
    with pytest.raises(BrowserValidationError, match="label"):
        service.click(
            "owner",
            status.session_id,
            "input",
            expected_backend_node_id=90,
            label_backend_node_ids=labels,
        )
    with pytest.raises(BrowserValidationError, match="identity"):
        service.click("owner", status.session_id, "input", label_backend_node_ids=[77])
    assert not any(method == "Input.dispatchMouseEvent" for method, *_ in factory.transport.calls)


def test_observed_click_on_a_scrolled_page_hit_tests_the_document_position(tmp_path):
    """Scrolling into view moves the page; the hit test must add that offset."""

    class ScrolledTransport(FakeTransport):
        def call(self, method, params=None, **kwargs):
            result = super().call(method, params, **kwargs)
            if method == "Page.getLayoutMetrics":
                return {"cssVisualViewport": {"pageX": 0, "pageY": 300.4, "scale": 1}}
            if method == "DOM.getNodeForLocation":
                # Only the document position (viewport centre + scroll) holds the control.
                return {"backendNodeId": 90 if (params["x"], params["y"]) == (50, 310) else 1}
            return result

    transport = ScrolledTransport()
    service, executable, *_ = make_service(tmp_path, transport=transport)
    status = service.start("owner", executable_path=executable)
    service.click("owner", status.session_id, "a", expected_backend_node_id=90)
    clicks = [
        values for method, values, _ in transport.calls if method == "Input.dispatchMouseEvent"
    ]
    assert [(c["x"], c["y"]) for c in clicks] == [(50, 10), (50, 10)]  # mouse stays in viewport


@pytest.mark.parametrize("identity", [True, 0, -1, "90", 90.0])
def test_invalid_observed_node_identity_fails_before_dom_lookup(tmp_path: Path, identity):
    service, executable, _, _, factory = make_service(tmp_path)
    status = service.start("owner", executable_path=executable)
    with pytest.raises(BrowserValidationError, match="identity"):
        service.click("owner", status.session_id, "input", expected_backend_node_id=identity)
    assert not any(method == "DOM.getDocument" for method, *_ in factory.transport.calls)


def test_diagnostics_filters_bounds_and_redacts(tmp_path: Path) -> None:
    transport = FakeTransport()
    typed_text = "my-arbitrary-entered-passphrase-zyx987"
    transport.events = [
        {
            "method": "Runtime.consoleAPICalled",
            "params": {
                "type": "log",
                "args": [
                    {
                        "type": "string",
                        "className": "String",
                        "value": typed_text,
                        "description": f"page echoed {typed_text}",
                    }
                ],
            },
        },
        {
            "method": "Log.entryAdded",
            "params": {
                "entry": {
                    "source": "javascript",
                    "level": "warning",
                    "text": f"browser input was {typed_text}",
                    "lineNumber": 7,
                }
            },
        },
        {"method": "Debugger.paused", "params": {"why": "ignored"}},
        {"method": "Network.loadingFailed", "params": {"errorText": "failed"}},
        {
            "method": "Network.responseReceived",
            "params": {
                "requestId": "request-1",
                "response": {
                    "url": (
                        "https://url-user:url-password@example.test/account/"
                        "sk-12345678901234567890?code=oauth-secret#fragment-secret"
                    ),
                    "status": 200,
                    "mimeType": "text/html",
                    "headers": {
                        "Set-Cookie": "session=browser-cookie-secret",
                        "Cookie": "session=request-cookie-secret",
                        "Authorization": "Bearer browser-authorization-secret",
                    },
                },
            },
        },
    ]
    service, executable, _launcher, _terminator, _factory = make_service(
        tmp_path, transport=transport
    )
    status = service.start("owner", executable_path=executable)
    result = service.diagnostics("owner", status.session_id, max_events=4)
    assert [event["category"] for event in result["events"]] == [
        "console",
        "console",
        "network",
        "network",
    ]
    rendered = repr(result)
    assert typed_text not in rendered
    assert "page echoed" not in rendered
    assert "browser input was" not in rendered
    assert "className" not in rendered
    assert "javascript" in rendered
    assert "browser-cookie-secret" not in rendered
    assert "request-cookie-secret" not in rendered
    assert "browser-authorization-secret" not in rendered
    assert "url-user" not in rendered
    assert "url-password" not in rendered
    assert "oauth-secret" not in rendered
    assert "fragment-secret" not in rendered
    assert "https://example.test/account/<redacted>" in rendered
    assert "headers" not in rendered.lower()


def test_owner_scope_limits_and_exact_cleanup(tmp_path: Path) -> None:
    proxy_factory = FakeEgressProxyFactory()
    service, executable, launcher, terminator, factory = make_service(
        tmp_path,
        max_sessions_total=1,
        max_sessions_per_owner=1,
        egress_proxy_factory=proxy_factory,
    )
    status = service.start("owner-a", executable_path=executable)
    assert service.list("owner-a") == (status,)
    assert service.list("owner-b") == ()
    with pytest.raises(BrowserOwnershipError):
        service.status("owner-b", status.session_id)
    with pytest.raises(BrowserLimitError):
        service.start("owner-b", executable_path=executable)
    profile = launcher.specs[0].profile_dir
    assert service.close("owner-a", status.session_id, delete_artifacts=True) is True
    assert factory.transport.closed is True
    assert terminator.calls == [(launcher.processes[0].pid, 2.0)]
    assert proxy_factory.instances[0].close_calls == [2.0]
    assert not profile.exists()
    assert service.close("owner-a", status.session_id) is False


def test_unhealthy_egress_proxy_blocks_further_browser_operations(tmp_path: Path) -> None:
    proxy_factory = FakeEgressProxyFactory()
    service, executable, _launcher, _terminator, _factory = make_service(
        tmp_path,
        egress_proxy_factory=proxy_factory,
    )
    status = service.start("owner", executable_path=executable)
    proxy_factory.instances[0].healthy = False
    proxy_factory.instances[0].terminal_error = RuntimeError("accept loop failed")

    assert service.status("owner", status.session_id).state == "crashed"
    with pytest.raises(BrowserLaunchError, match="egress proxy"):
        service.snapshot("owner", status.session_id)


def test_disconnected_transport_is_not_reported_as_running(tmp_path: Path) -> None:
    service, executable, launcher, _terminator, factory = make_service(tmp_path)
    status = service.start("owner", executable_path=executable)
    assert service.status("owner", status.session_id).state == "running"
    factory.transport.closed = True
    assert launcher.processes[0].poll() is None
    assert service.status("owner", status.session_id).state == "disconnected"
    assert service.list("owner")[0].state == "disconnected"
    calls_before = len(factory.transport.calls)
    with pytest.raises(BrowserLaunchError, match="control connection"):
        service.type_text("owner", status.session_id, "#field", "must not dispatch")
    assert len(factory.transport.calls) == calls_before
    assert service.close("owner", status.session_id)


def test_close_cuts_egress_before_transport_and_process(tmp_path: Path) -> None:
    events: list[str] = []

    class OrderedProxy(FakeEgressProxy):
        def close(self, *, timeout: float = 2.0) -> None:
            events.append("proxy")
            super().close(timeout=timeout)

    def create_proxy(**kwargs: Any) -> FakeEgressProxy:
        return OrderedProxy(**kwargs)

    transport = FakeTransport()
    original_transport_close = transport.close

    def close_transport(*, timeout: float) -> None:
        events.append("transport")
        original_transport_close(timeout=timeout)

    transport.close = close_transport  # type: ignore[method-assign]
    service, executable, _launcher, terminator, _factory = make_service(
        tmp_path,
        transport=transport,
        egress_proxy_factory=create_proxy,
    )
    original_terminate = terminator.terminate

    def terminate(process: FakeProcess, *, grace_seconds: float) -> None:
        events.append("process")
        original_terminate(process, grace_seconds=grace_seconds)

    terminator.terminate = terminate  # type: ignore[method-assign]
    status = service.start("owner", executable_path=executable)

    assert service.close("owner", status.session_id) is True
    assert events == ["proxy", "transport", "process"]


def test_proxy_close_failure_still_terminates_and_remains_retryable(tmp_path: Path) -> None:
    class RetryProxy(FakeEgressProxy):
        def close(self, *, timeout: float = 2.0) -> None:
            self.close_calls.append(timeout)
            if len(self.close_calls) == 1:
                raise RuntimeError("close failed")
            self.healthy = False

    proxies: list[RetryProxy] = []

    def create_proxy(**kwargs: Any) -> RetryProxy:
        proxy = RetryProxy(**kwargs)
        proxies.append(proxy)
        return proxy

    service, executable, launcher, terminator, factory = make_service(
        tmp_path,
        egress_proxy_factory=create_proxy,
    )
    status = service.start("owner", executable_path=executable)

    with pytest.raises(BrowserLaunchError, match="retry close"):
        service.close("owner", status.session_id)
    assert factory.transport.closed is True
    assert terminator.calls == [(launcher.processes[0].pid, 2.0)]
    assert service.list("owner")
    assert service.close("owner", status.session_id) is True
    assert proxies[0].close_calls == [2.0, 2.0]


def test_close_all_keeps_session_registered_when_profile_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, executable, _launcher, _terminator, _factory = make_service(tmp_path)
    status = service.start("owner", executable_path=executable)
    monkeypatch.setattr(
        "alysis_code.ide.managed_browser._remove_owned_session_dir",
        lambda *_args, **_kwargs: False,
    )

    with pytest.raises(BrowserLaunchError, match="retry shutdown cleanup"):
        service.close_all()
    assert service.list("owner")[0].session_id == status.session_id


def test_close_all_removes_ephemeral_screenshot_artifacts(tmp_path: Path) -> None:
    service, executable, _launcher, _terminator, _factory = make_service(tmp_path)
    status = service.start("owner", executable_path=executable)
    service.screenshot("owner", status.session_id)
    artifact_root = tmp_path / "private-browser-data" / "artifacts"
    assert list(artifact_root.glob("*"))

    service.close_all()

    assert not list(artifact_root.glob("*"))


def test_concurrent_operation_on_same_session_is_rejected(tmp_path: Path) -> None:
    transport = FakeTransport()
    transport.navigation_entered = threading.Event()
    transport.navigation_release = threading.Event()
    service, executable, _launcher, _terminator, _factory = make_service(
        tmp_path, transport=transport
    )
    status = service.start("owner", executable_path=executable)
    failures: list[BaseException] = []

    def navigate() -> None:
        try:
            service.navigate("owner", status.session_id, "https://example.test/")
        except BaseException as exc:  # noqa: BLE001 - asserted below
            failures.append(exc)

    worker = threading.Thread(target=navigate)
    worker.start()
    assert transport.navigation_entered.wait(timeout=1)
    with pytest.raises(BrowserLimitError):
        service.snapshot("owner", status.session_id)
    transport.navigation_release.set()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert failures == []


def test_cancellation_is_fail_closed(tmp_path: Path) -> None:
    service, executable, launcher, _terminator, _factory = make_service(tmp_path)
    with pytest.raises(BrowserCancelledError):
        service.start("owner", executable_path=executable, cancel=lambda: True)
    assert launcher.specs == []


def test_pause_fences_immediately_and_drains_without_cancelling_active_operation(tmp_path: Path):
    transport = FakeTransport()
    transport.navigation_entered = threading.Event()
    transport.navigation_release = threading.Event()
    service, executable, *_ = make_service(tmp_path, transport=transport)
    sid = service.start("owner", executable_path=executable).session_id
    paused = threading.Event()

    def observe_pause():
        if service.status("owner", sid).commands_paused:
            paused.set()
        return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        navigation = pool.submit(service.navigate, "owner", sid, "https://example.test/")
        try:
            assert transport.navigation_entered.wait(1)
            draining = pool.submit(service.pause_commands, "owner", sid, cancel=observe_pause)
            assert paused.wait(1)
            state = service.status("owner", sid)
            assert state.commands_paused and state.operation_active
            assert state.command_generation == 1
            assert not draining.done()
            calls = list(transport.calls)
            with pytest.raises(BrowserControlError, match="paused"):
                service.click("owner", sid, "#submit")
            with pytest.raises(BrowserControlError, match="paused"):
                service.snapshot("owner", sid)
            assert transport.calls == calls
        finally:
            transport.navigation_release.set()
        assert navigation.result(2)["command_generation"] == 0
        state = draining.result(2)
        assert state.commands_paused and not state.operation_active
        assert state.command_generation == 1
    fresh = service.resume_commands("owner", sid, expected_generation=1)
    assert fresh["kind"] == "dom" and fresh["command_generation"] == 2
    assert transport.calls[-1][0] == "DOMSnapshot.captureSnapshot"
    assert not service.status("owner", sid).commands_paused


@pytest.mark.parametrize("cancel_wait", [False, True])
def test_interrupted_pause_stays_fenced_and_cannot_resume_active_operation(
    tmp_path: Path, cancel_wait
):
    transport = FakeTransport()
    transport.navigation_entered = threading.Event()
    transport.navigation_release = threading.Event()
    service, executable, *_ = make_service(tmp_path, transport=transport)
    sid = service.start("owner", executable_path=executable).session_id
    with ThreadPoolExecutor(max_workers=1) as pool:
        navigation = pool.submit(service.navigate, "owner", sid, "https://example.test/")
        try:
            assert transport.navigation_entered.wait(1)
            expected_error = BrowserCancelledError if cancel_wait else BrowserTimeoutError
            with pytest.raises(expected_error):
                service.pause_commands(
                    "owner",
                    sid,
                    timeout=0.05,
                    cancel=lambda: cancel_wait and service.status("owner", sid).commands_paused,
                )
            assert service.status("owner", sid).commands_paused
            with pytest.raises(BrowserLimitError, match="not drained"):
                service.resume_commands("owner", sid, expected_generation=1)
            with pytest.raises(BrowserControlError):
                service.type_text("owner", sid, "#draft", "stale", expected_generation=0)
        finally:
            transport.navigation_release.set()
        assert navigation.result(2)["command_generation"] == 0
    state = service.pause_commands("owner", sid)
    assert state.command_generation == 2 and not state.operation_active
    assert service.resume_commands("owner", sid, expected_generation=2)["command_generation"] == 3


@pytest.mark.parametrize("method", ["navigate", "click", "type_text"])
@pytest.mark.parametrize("generation", [None, 0, 1, True, -1, "2"])
def test_mutations_require_current_generation_after_resume(tmp_path: Path, method, generation):
    service, executable, _launcher, _terminator, factory = make_service(tmp_path)
    sid = service.start("owner", executable_path=executable).session_id
    service.pause_commands("owner", sid)
    fresh = service.resume_commands("owner", sid, expected_generation=1)
    calls = list(factory.transport.calls)
    args = {
        "navigate": ("https://example.test/",),
        "click": ("#submit",),
        "type_text": ("#draft", "new draft"),
    }[method]
    with pytest.raises(BrowserControlError, match="generation"):
        getattr(service, method)("owner", sid, *args, expected_generation=generation)
    assert factory.transport.calls == calls
    result = getattr(service, method)(
        "owner", sid, *args, expected_generation=fresh["command_generation"]
    )
    assert result["command_generation"] == 2


class BlockingSnapshotTransport(FakeTransport):
    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def call(self, method, params=None, **kwargs):
        if method == "DOMSnapshot.captureSnapshot":
            self.entered.set()
            assert self.release.wait(2), "Test did not release snapshot"
        return super().call(method, params, **kwargs)


def test_inflight_observation_keeps_original_generation_after_pause(tmp_path: Path):
    transport = BlockingSnapshotTransport()
    service, executable, *_ = make_service(tmp_path, transport=transport)
    sid = service.start("owner", executable_path=executable).session_id
    with ThreadPoolExecutor(max_workers=1) as pool:
        observation = pool.submit(service.snapshot, "owner", sid, kind="dom")
        try:
            assert transport.entered.wait(1)
            with pytest.raises(BrowserTimeoutError):
                service.pause_commands("owner", sid, timeout=0.05)
        finally:
            transport.release.set()
        assert observation.result(2)["command_generation"] == 0
    assert service.status("owner", sid).command_generation == 1


def test_new_pause_supersedes_resume_while_fresh_snapshot_is_inflight(tmp_path: Path):
    transport = BlockingSnapshotTransport()
    service, executable, *_ = make_service(tmp_path, transport=transport)
    sid = service.start("owner", executable_path=executable).session_id
    service.pause_commands("owner", sid)
    with ThreadPoolExecutor(max_workers=1) as pool:
        resuming = pool.submit(service.resume_commands, "owner", sid, expected_generation=1)
        try:
            assert transport.entered.wait(1)
            with pytest.raises(BrowserTimeoutError):
                service.pause_commands("owner", sid, timeout=0.05)
        finally:
            transport.release.set()
        with pytest.raises(BrowserControlError, match="generation"):
            resuming.result(2)
    status = service.status("owner", sid)
    assert status.commands_paused and status.command_generation == 2
    assert not status.operation_active
    with pytest.raises(BrowserControlError):
        service.resume_commands("owner", sid, expected_generation=1)
    assert service.resume_commands("owner", sid, expected_generation=2)["command_generation"] == 3


@pytest.mark.parametrize("failure", ["capture", "cancel", "disconnect"])
def test_resume_failure_does_not_reopen_commands(tmp_path: Path, failure):
    cancelled = threading.Event()

    class FailingSnapshot(FakeTransport):
        def call(self, method, params=None, **kwargs):
            result = super().call(method, params, **kwargs)
            if method == "DOMSnapshot.captureSnapshot":
                if failure == "capture":
                    raise BrowserTimeoutError("capture failed")
                if failure == "cancel":
                    cancelled.set()
                if failure == "disconnect":
                    self.closed = True
            return result

    transport = FailingSnapshot()
    service, executable, *_ = make_service(tmp_path, transport=transport)
    sid = service.start("owner", executable_path=executable).session_id
    service.pause_commands("owner", sid)
    error = {
        "capture": BrowserTimeoutError,
        "cancel": BrowserCancelledError,
        "disconnect": BrowserLaunchError,
    }[failure]
    with pytest.raises(error):
        service.resume_commands("owner", sid, expected_generation=1, cancel=cancelled.is_set)
    status = service.status("owner", sid)
    assert status.commands_paused and status.command_generation == 1
    assert not status.operation_active


def test_shutdown_supersedes_inflight_resume(tmp_path: Path):
    transport = BlockingSnapshotTransport()
    service, executable, *_ = make_service(tmp_path, transport=transport)
    sid = service.start("owner", executable_path=executable).session_id
    service.pause_commands("owner", sid)
    with ThreadPoolExecutor(max_workers=1) as pool:
        resuming = pool.submit(service.resume_commands, "owner", sid, expected_generation=1)
        try:
            assert transport.entered.wait(1)
            service.close_all()
        finally:
            transport.release.set()
        with pytest.raises(BrowserControlError, match="closing"):
            resuming.result(2)
    assert service.list("owner") == ()


def test_pause_checks_owner_and_preflight_before_fencing_and_isolates_sessions(tmp_path: Path):
    service, executable, *_ = make_service(tmp_path)
    sid = service.start("owner", executable_path=executable).session_id
    sibling = service.start("owner", executable_path=executable).session_id
    with pytest.raises(BrowserOwnershipError):
        service.pause_commands("other", sid)
    with pytest.raises(BrowserValidationError):
        service.pause_commands("owner", sid, timeout=-1)
    with pytest.raises(BrowserCancelledError):
        service.pause_commands("owner", sid, cancel=lambda: True)
    assert service.status("owner", sid).command_generation == 0
    assert not service.status("owner", sid).commands_paused
    service.pause_commands("owner", sid)
    with pytest.raises(BrowserOwnershipError):
        service.resume_commands("other", sid, expected_generation=1)
    assert service.snapshot("owner", sibling)["command_generation"] == 0
    assert service.navigate("owner", sibling, "https://example.test/")["command_generation"] == 0
    assert service.status("owner", sid).commands_paused
    assert service.close("owner", sid)


def test_viewer_claim_is_owned_exclusive_and_blocks_resume(tmp_path: Path):
    service, executable, *_ = make_service(tmp_path, linux_display=":99")
    sid = service.start("owner", executable_path=executable).session_id
    with pytest.raises(BrowserOwnershipError):
        service.claim_viewer_control("other", sid)
    display, claim = service.claim_viewer_control("owner", sid)
    assert display == ":99"
    with pytest.raises(BrowserControlError):
        service.claim_viewer_control("owner", sid)
    state = service.pause_commands("owner", sid)
    with pytest.raises(BrowserControlError, match="Revoke the input viewer"):
        service.close("owner", sid)
    with pytest.raises(BrowserLaunchError):
        service.close_all()
    assert service.status("owner", sid).state == "running"
    with pytest.raises(BrowserControlError, match="not been revoked"):
        service.resume_commands("owner", sid, expected_generation=state.command_generation)
    with pytest.raises(BrowserOwnershipError):
        service.release_viewer_control("other", sid, claim)
    with pytest.raises(BrowserControlError):
        service.release_viewer_control("owner", sid, "not-the-claim")
    service.release_viewer_control("owner", sid, claim)
    service.resume_commands("owner", sid, expected_generation=state.command_generation)
    assert not service.status("owner", sid).commands_paused
    service.close_all()


def test_headless_session_cannot_claim_a_viewer(tmp_path: Path):
    service, executable, *_ = make_service(tmp_path)
    sid = service.start("owner", executable_path=executable).session_id
    with pytest.raises(BrowserControlError, match="no local X11 display"):
        service.claim_viewer_control("owner", sid)
    assert not service.status("owner", sid).commands_paused
    service.close_all()


def test_viewer_claim_during_fresh_capture_prevents_resume(tmp_path: Path):
    transport = BlockingSnapshotTransport()
    service, executable, *_ = make_service(tmp_path, transport=transport, linux_display=":99")
    sid = service.start("owner", executable_path=executable).session_id
    service.pause_commands("owner", sid)
    with ThreadPoolExecutor(max_workers=1) as pool:
        resuming = pool.submit(service.resume_commands, "owner", sid, expected_generation=1)
        try:
            assert transport.entered.wait(1)
            _, claim = service.claim_viewer_control("owner", sid)
        finally:
            transport.release.set()
        with pytest.raises(BrowserControlError, match="not been revoked"):
            resuming.result(2)
    assert service.status("owner", sid).commands_paused
    service.release_viewer_control("owner", sid, claim)
    service.close_all()


def test_visible_display_cannot_be_shared_by_sessions_or_service_instances(tmp_path: Path):
    other_root = tmp_path / "other"
    other_root.mkdir()
    service, executable, *_ = make_service(tmp_path, linux_display=":124")
    other, other_executable, *_ = make_service(other_root, linux_display=":124.0")
    sid = service.start("owner", executable_path=executable).session_id
    try:
        with pytest.raises(BrowserControlError, match="already owns"):
            service.start("another-owner", executable_path=executable)
        with pytest.raises(BrowserControlError, match="already owns"):
            other.start("owner", executable_path=other_executable)
        service.close("owner", sid)
        assert other.start("owner", executable_path=other_executable).session_id
    finally:
        service.close_all()
        other.close_all()


class ControlsTransport(FakeTransport):
    reject_selection = False

    def call(self, method, params=None, **kwargs):
        result = super().call(method, params, **kwargs)
        if method == "DOM.resolveNode":
            return {"object": {"objectId": "resolved-control"}}
        if method == "Runtime.callFunctionOn":
            return {"result": {"value": {"dispatched": not self.reject_selection}}}
        if method == "Page.getLayoutMetrics":
            return {"cssLayoutViewport": {"clientWidth": 1280, "clientHeight": 800}}
        return result


@pytest.mark.parametrize(
    "method,args,kwargs",
    [
        ("select_option", ("select", "option"), {"expected_option_backend_node_id": 90}),
        ("press_key", ("input", "Enter"), {}),
        ("scroll", ("section",), {"delta_y": 500}),
    ],
)
def test_controls_enforce_owner_generation_and_node_identity(tmp_path, method, args, kwargs):
    transport = ControlsTransport()
    service, executable, *_ = make_service(tmp_path, transport=transport)
    sid = service.start("owner", executable_path=executable).session_id
    service.pause_commands("owner", sid)
    service.resume_commands("owner", sid, expected_generation=1)
    for owner, generation, identity in [("other", 2, 90), ("owner", 0, 90), ("owner", 2, 89)]:
        previous = len(transport.calls)
        with pytest.raises(BrowserError):
            getattr(service, method)(
                owner,
                sid,
                *args,
                expected_generation=generation,
                expected_backend_node_id=identity,
                **kwargs,
            )
        assert not any(
            m.startswith("Input.") or m == "Runtime.callFunctionOn"
            for m, *_ in transport.calls[previous:]
        )
    result = getattr(service, method)(
        "owner", sid, *args, expected_generation=2, expected_backend_node_id=90, **kwargs
    )
    assert result["dispatched"] is True


def test_select_uses_fixed_script_and_releases_objects_on_rejected_option(tmp_path):
    transport = ControlsTransport()
    service, executable, *_ = make_service(tmp_path, transport=transport)
    sid = service.start("owner", executable_path=executable).session_id
    transport.reject_selection = True
    with pytest.raises(BrowserValidationError):
        service.select_option(
            "owner",
            sid,
            'select[data-name="\'); evil()"]',
            "option",
            expected_option_backend_node_id=90,
        )
    script = next(p for m, p, _ in transport.calls if m == "Runtime.callFunctionOn")
    assert "evil()" not in script["functionDeclaration"]
    assert script["arguments"] == [{"objectId": "resolved-control"}]
    assert transport.calls[-1][0] == "Runtime.releaseObjectGroup"


def test_key_release_still_runs_when_cancelled_after_key_down(tmp_path):
    class CancelAfterDown(ControlsTransport):
        pressed = False

        def call(self, method, params=None, **kwargs):
            result = super().call(method, params, **kwargs)
            if method == "Input.dispatchKeyEvent" and params["type"] == "keyDown":
                self.pressed = True
            return result

    transport = CancelAfterDown()
    service, executable, *_ = make_service(tmp_path, transport=transport)
    sid = service.start("owner", executable_path=executable).session_id
    with pytest.raises(BrowserCancelledError):
        service.press_key("owner", sid, "input", "Enter", cancel=lambda: transport.pressed)
    events = [p for m, p, _ in transport.calls if m == "Input.dispatchKeyEvent"]
    assert [p["type"] for p in events] == ["keyDown", "keyUp"]
    assert events[0]["text"] == "\r"


@pytest.mark.parametrize(
    "method,args,kwargs",
    [
        ("press_key", ("input", "Ctrl+L"), {}),
        ("press_key", ("input", "Tab"), {"shift": 1}),
        ("scroll", ("section",), {"delta_y": 0}),
        ("scroll", ("section",), {"delta_y": float("inf")}),
        ("scroll", ("section",), {"delta_y": True}),
        ("scroll", ("section",), {"delta_y": -2001}),
    ],
)
def test_controls_reject_invalid_input_before_dom_lookup(tmp_path, method, args, kwargs):
    service, executable, _, _, factory = make_service(tmp_path)
    sid = service.start("owner", executable_path=executable).session_id
    with pytest.raises(BrowserValidationError):
        getattr(service, method)("owner", sid, *args, **kwargs)
    assert not any(m == "DOM.getDocument" for m, *_ in factory.transport.calls)


class VisualTransport(ControlsTransport):
    def __init__(self):
        super().__init__()
        import io

        from PIL import Image

        out = io.BytesIO()
        Image.new("RGB", (64, 64), "blue").save(out, format="PNG")
        self.png = out.getvalue()
        self.url = "https://example.test/"
        self.cancelled = False
        self.cancel_on_press = False

    def call(self, method, params=None, **kwargs):
        result = super().call(method, params, **kwargs)
        if method == "Page.captureScreenshot":
            return {"data": base64.b64encode(self.png).decode()}
        if method == "Page.getLayoutMetrics":
            return {"cssLayoutViewport": {"clientWidth": 32, "clientHeight": 32}}
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"url": self.url}}}
        if (
            method == "Input.dispatchMouseEvent"
            and params["type"] == "mousePressed"
            and self.cancel_on_press
        ):
            self.cancelled = True
        return result


def test_visual_click_uses_owned_current_pixels_and_scales_coordinates(tmp_path):
    transport = VisualTransport()
    service, executable, *_ = make_service(tmp_path, transport=transport)
    sid = service.start("owner", executable_path=executable).session_id
    frame = service.capture_visual_frame("owner", sid)
    assert frame.width == frame.height == 64 and frame.png == transport.png
    assert "png=" not in repr(frame)
    result = service.click_visual_frame("owner", sid, frame, 32, 24)
    events = [p for m, p, _ in transport.calls if m == "Input.dispatchMouseEvent"]
    assert [p["type"] for p in events] == ["mousePressed", "mouseReleased"]
    assert all((p["x"], p["y"]) == (16, 12) for p in events)
    assert result["verification_required"] and frame.consumed
    with pytest.raises(BrowserError, match="used"):
        service.click_visual_frame("owner", sid, frame, 32, 24)
    assert len([m for m, *_ in transport.calls if m == "Input.dispatchMouseEvent"]) == 2


@pytest.mark.parametrize("change", ["pixels", "url", "expired", "generation", "owner", "session"])
def test_stale_or_foreign_frame_never_sends_input(tmp_path, change):
    transport = VisualTransport()
    service, executable, *_ = make_service(tmp_path, transport=transport)
    sid = service.start("owner", executable_path=executable).session_id
    frame = service.capture_visual_frame("owner", sid)
    if change == "pixels":
        transport.png = transport.png[:-1] + b"z"
    elif change == "url":
        transport.url = "https://elsewhere.example/"
    elif change == "expired":
        frame.created_at -= 61
    elif change == "generation":
        service.pause_commands("owner", sid)
        service.resume_commands("owner", sid, expected_generation=1)
    elif change == "owner":
        frame.owner_id = "other"
    else:
        frame.session_id = "other"
    with pytest.raises(BrowserError):
        service.click_visual_frame("owner", sid, frame, 32, 24)
    assert not any(m.startswith("Input.") for m, *_ in transport.calls)


@pytest.mark.parametrize("x,y", [(True, 5), (-1, 2), (64, 2), (2, 64), (1.5, 2)])
def test_visual_coordinates_are_bounded_image_pixels(tmp_path, x, y):
    transport = VisualTransport()
    service, executable, *_ = make_service(tmp_path, transport=transport)
    sid = service.start("owner", executable_path=executable).session_id
    frame = service.capture_visual_frame("owner", sid)
    with pytest.raises(BrowserValidationError):
        service.click_visual_frame("owner", sid, frame, x, y)
    assert not any(m.startswith("Input.") for m, *_ in transport.calls)


def test_visual_click_releases_mouse_on_mid_dispatch_cancellation(tmp_path):
    transport = VisualTransport()
    service, executable, *_ = make_service(tmp_path, transport=transport)
    sid = service.start("owner", executable_path=executable).session_id
    frame = service.capture_visual_frame("owner", sid)
    transport.cancel_on_press = True
    with pytest.raises(BrowserCancelledError):
        service.click_visual_frame("owner", sid, frame, 32, 24, cancel=lambda: transport.cancelled)
    assert [p["type"] for m, p, _ in transport.calls if m == "Input.dispatchMouseEvent"] == [
        "mousePressed",
        "mouseReleased",
    ]


@pytest.mark.parametrize(
    "method,args", [("type_text", ("input", "do not type")), ("press_key", ("input", "Enter"))]
)
def test_redirected_focus_cannot_receive_keyboard_input(tmp_path, method, args):
    class RedirectedFocus(FakeTransport):
        def call(self, name, params=None, **kwargs):
            result = super().call(name, params, **kwargs)
            if name == "Accessibility.getPartialAXTree":
                return {
                    "nodes": [
                        {
                            "backendDOMNodeId": 91,
                            "properties": [{"name": "focused", "value": {"value": True}}],
                        }
                    ]
                }
            return result

    transport = RedirectedFocus()
    service, executable, *_ = make_service(tmp_path, transport=transport)
    sid = service.start("owner", executable_path=executable).session_id
    with pytest.raises(BrowserControlError, match="Focus moved"):
        getattr(service, method)("owner", sid, *args)
    assert not any(m.startswith("Input.") for m, *_ in transport.calls)


def test_switch_only_guarded_pages_with_owned_fresh_generation(tmp_path):
    service, executable, _, _, factory = make_service(tmp_path)
    transport = factory.transport
    original = transport.call
    transport.guarded_page_sessions = lambda **k: {"popup": "popup-session"}

    def call(method, *args, **kwargs):
        if method == "Target.getTargets":
            return {
                "targetInfos": [
                    dict(
                        targetId="target-1", type="page", url="https://example.org/", title="root"
                    ),
                    dict(
                        targetId="popup", type="page", url="https://other.example/", title="popup"
                    ),
                    dict(
                        targetId="unattached",
                        type="page",
                        url="https://example.org/",
                        title="other",
                    ),
                    dict(
                        targetId="worker", type="worker", url="https://example.org/", title="worker"
                    ),
                ]
            }
        return original(method, *args, **kwargs)

    transport.call = call
    session = service.start("owner", executable_path=executable)
    try:
        pages = service.pages("owner", session.session_id)
        assert {p["page_id"] for p in pages["pages"]} == {"target-1", "popup"}
        with pytest.raises(BrowserOwnershipError):
            service.switch_page("other", session.session_id, "popup", expected_generation=0)
        with pytest.raises(BrowserPageUnavailableError):
            service.switch_page("owner", session.session_id, "unattached", expected_generation=0)
        service.switch_page("owner", session.session_id, "popup", expected_generation=0)
        assert service.status("owner", session.session_id).command_generation == 1
        assert service.status("owner", session.session_id).active_url == "https://other.example/"
        with pytest.raises(BrowserControlError):
            service.switch_page("owner", session.session_id, "target-1", expected_generation=0)
        service.snapshot("owner", session.session_id, kind="dom")
        assert transport.calls[-1][2] == "popup-session"
        service.pause_commands("owner", session.session_id)
        with pytest.raises(BrowserControlError):
            service.pages("owner", session.session_id)
    finally:
        service.close_all()


def test_open_window_is_a_viewer_convenience_that_never_waits_or_navigates(tmp_path):
    # The owl computer's Browser button (bots/browser_linux.py): a blank window, nothing else.
    service, executable, _, _, factory = make_service(tmp_path)
    transport = factory.transport
    session = service.start("owner", executable_path=executable)
    sid = session.session_id
    try:
        with pytest.raises(BrowserOwnershipError):
            service.open_window("other", sid)
        before = len(transport.calls)
        # Busy with an owl operation: skipped, not queued behind it.
        with service._operation(service._owned_session("owner", sid), cancel=None):
            assert service.open_window("owner", sid) is False
        assert transport.calls[before:] == []
        assert service.open_window("owner", sid) is True
        assert transport.calls[before:] == [
            ("Target.createTarget", {"url": "about:blank", "newWindow": True}, None)
        ]
        # The person at the screen can open one while the owl's commands are paused.
        status = service.pause_commands("owner", sid)
        assert status.state == "running"
        assert service.open_window("owner", sid) is True
    finally:
        service.close_all()


@pytest.mark.parametrize("dialog", [False, True])
def test_close_page_preserves_dialogs_and_checks_ownership_generation_and_busy(tmp_path, dialog):
    service, executable, _, _, factory = make_service(tmp_path)
    transport = factory.transport
    original = transport.call
    pages = [
        dict(targetId=p, type="page", url="https://example.org/") for p in ("target-1", "popup")
    ]
    transport.guarded_page_sessions = lambda **k: {"popup": "popup-session"}

    def call(method, *args, **kwargs):
        if method == "Target.getTargets":
            return {"targetInfos": list(pages)}
        if method == "Page.close":
            if dialog:
                transport.events = [
                    {"method": "Page.javascriptDialogOpening", "params": {"type": "beforeunload"}}
                ]
            else:
                pages.pop()
        return original(method, *args, **kwargs)

    transport.call = call
    session = service.start("owner", executable_path=executable)
    sid = session.session_id
    try:
        with pytest.raises(BrowserOwnershipError):
            service.close_page("other", sid, "popup", expected_generation=0)
        with pytest.raises(BrowserPageUnavailableError):
            service.close_page("owner", sid, "unattached", expected_generation=0)
        # The same gate is held for the entire download, so cleanup cannot
        # interrupt a transfer that is still being collected and verified.
        with service._operation(service._owned_session("owner", sid), cancel=None):
            with pytest.raises(BrowserLimitError):
                service.close_page("owner", sid, "popup", expected_generation=0)
        with pytest.raises(BrowserCancelledError):
            service.close_page("owner", sid, "popup", expected_generation=0, cancel=lambda: True)
        assert not any(m == "Page.close" for m, *_ in transport.calls)
        result = service.close_page("owner", sid, "popup", expected_generation=0)
        assert result == {
            "closed": not dialog,
            "reason": "dialog_preserved_page" if dialog else "closed",
        }
        assert service.status("owner", sid).command_generation == 1
        with pytest.raises(BrowserControlError):
            service.close_page("owner", sid, "target-1", expected_generation=0)
        if dialog:
            assert transport.calls[-1] == (
                "Page.handleJavaScriptDialog",
                {"accept": False},
                "popup-session",
            )
            assert len(service.pages("owner", sid)["pages"]) == 2
        else:
            assert service.close_page("owner", sid, "target-1", expected_generation=1) == {
                "closed": False,
                "reason": "last_page_retained",
            }
        assert sum(m == "Page.close" for m, *_ in transport.calls) == 1
        assert not any(m == "Target.closeTarget" for m, *_ in transport.calls)
    finally:
        service.close_all()
