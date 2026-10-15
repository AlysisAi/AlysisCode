from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from rich.console import Console

from alysis_code.agent.errors import AgentRuntimeError, ApprovalDeclinedError
from alysis_code.agent.tools_assembly import build_tools
from alysis_code.cli_impl.commands.welcome import _session_build_tools_kwargs
from alysis_code.config import AppConfig
from alysis_code.ide.managed_browser import BrowserArtifact
from alysis_code.session_store import SessionStore
from alysis_code.surface import ApprovalDecision, ApprovalRequest, NoopSurface

_BROWSER_TOOL_NAMES = {
    "browser_start",
    "browser_navigate",
    "browser_snapshot",
    "browser_screenshot",
    "browser_artifact_read",
    "browser_diagnostics",
    "browser_click",
    "browser_type",
    "browser_status",
    "browser_list",
    "browser_close",
}

_READ_ONLY_BROWSER_TOOL_NAMES = {
    "browser_snapshot",
    "browser_screenshot",
    "browser_artifact_read",
    "browser_diagnostics",
    "browser_status",
    "browser_list",
}


class _HostApprovalSurface(NoopSurface):
    host_managed_approvals = True

    def __init__(self, *, allow: bool = True, allow_for_session: bool = False) -> None:
        super().__init__()
        self.allow = allow
        self.allow_for_session = allow_for_session
        self.requests: list[ApprovalRequest] = []

    def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        self.requests.append(request)
        return ApprovalDecision(
            allow=self.allow,
            allow_for_session=self.allow_for_session,
        )


class _FakeManagedBrowserService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.fail_snapshot = False
        self.session_id = "BrowserSessionId1234567890"
        self.local_session_id = "BrowserLocalSessionId1234567890"

    def _record(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))

    def _status(
        self,
        *,
        active_url: str | None = None,
        session_id: str | None = None,
        allow_local_destinations: bool = False,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            session_id=session_id or self.session_id,
            owner_id="ide-owner",
            product="chromium",
            state="ready",
            created_at=123.0,
            allow_local_destinations=allow_local_destinations,
            active_url=active_url,
            artifact_count=1,
        )

    def start(self, owner_id: str, **kwargs: Any) -> SimpleNamespace:
        self._record("start", owner_id, **kwargs)
        return self._status(
            active_url="https://example.com/start/sk-12345678901234567890?token=secret"
        )

    def navigate(self, owner_id: str, session_id: str, url: str, **kwargs: Any) -> dict[str, Any]:
        self._record("navigate", owner_id, session_id, url, **kwargs)
        return {"session_id": session_id, "url": url, "result": {"frame_id": "frame-1"}}

    def snapshot(self, owner_id: str, session_id: str, **kwargs: Any) -> dict[str, Any]:
        self._record("snapshot", owner_id, session_id, **kwargs)
        if self.fail_snapshot:
            raise RuntimeError("provider secret sk-do-not-leak")
        return {"session_id": session_id, "kind": "text", "text": "safe page"}

    def screenshot(self, owner_id: str, session_id: str, **kwargs: Any) -> BrowserArtifact:
        self._record("screenshot", owner_id, session_id, **kwargs)
        return BrowserArtifact(
            artifact_id=f"browser:{session_id}:screenshot-0001-deadbeef.png",
            relative_path="private/secret/filesystem/path.png",
            media_type="image/png",
            size_bytes=16,
            sha256="a" * 64,
        )

    def read_artifact(
        self, owner_id: str, session_id: str, artifact_id: str, **kwargs: Any
    ) -> dict[str, Any]:
        self._record("read_artifact", owner_id, session_id, artifact_id, **kwargs)
        return {
            "artifact_id": artifact_id,
            "media_type": "image/png",
            "encoding": "base64",
            "content": "iVBORw0KGgo=",
            "offset": 0,
            "next_offset": 8,
            "size_bytes": 8,
            "truncated": False,
        }

    def diagnostics(self, owner_id: str, session_id: str, **kwargs: Any) -> dict[str, Any]:
        self._record("diagnostics", owner_id, session_id, **kwargs)
        return {"session_id": session_id, "events": [{"message": "[REDACTED]"}]}

    def click(self, owner_id: str, session_id: str, selector: str, **kwargs: Any) -> dict[str, Any]:
        self._record("click", owner_id, session_id, selector, **kwargs)
        return {"session_id": session_id, "clicked": True}

    def type_text(
        self,
        owner_id: str,
        session_id: str,
        selector: str,
        text: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self._record("type_text", owner_id, session_id, selector, text, **kwargs)
        return {"session_id": session_id, "typed": True, "character_count": len(text)}

    def status(self, owner_id: str, session_id: str) -> SimpleNamespace:
        self._record("status", owner_id, session_id)
        if session_id == self.local_session_id:
            return self._status(
                session_id=session_id,
                allow_local_destinations=True,
                active_url="http://127.0.0.1:3000/private",
            )
        return self._status(
            active_url="https://example.com/status/sk-12345678901234567890?password=secret"
        )

    def list(self, owner_id: str) -> tuple[SimpleNamespace, ...]:
        self._record("list", owner_id)
        return (
            self._status(
                active_url="https://example.com/list/sk-12345678901234567890?api_key=secret"
            ),
        )

    def close(self, owner_id: str, session_id: str, **kwargs: Any) -> bool:
        self._record("close", owner_id, session_id, **kwargs)
        return True


def _store(root: Path) -> SessionStore:
    return SessionStore(
        enabled=False,
        sessions_dir=root / "sessions",
        session_id="managed-browser-agent-tools",
        cwd=str(root),
        repo_root=str(root),
    )


def _build_tools(
    tmp_path: Path,
    *,
    service: _FakeManagedBrowserService | None,
    surface: NoopSurface | None = None,
    mode: str = "auto",
    subagent_depth: int = 0,
    cancel_check: Any = None,
    child_managed_browser_tool_names: tuple[str, ...] | None = None,
    durable_service_manager: Any | None = None,
) -> dict[str, Any]:
    return build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO()),
        surface=surface,
        store=_store(tmp_path),
        mode=mode,
        yes=True,
        cfg=AppConfig(model="test-model", base_url="https://api.openai.com/v1"),
        api_key="test-key",
        non_interactive=True,
        verification_enabled=False,
        subagents_enabled=False,
        subagent_depth=subagent_depth,
        managed_browser_service=service,  # type: ignore[arg-type]
        managed_browser_owner_id="ide-owner",
        managed_browser_cancel_check=cancel_check,
        child_managed_browser_tool_names=child_managed_browser_tool_names,
        durable_service_manager=durable_service_manager,
    )


def test_browser_tools_are_capability_gated_and_hidden_from_subagents(tmp_path: Path) -> None:
    without_service = _build_tools(tmp_path, service=None)
    nested = _build_tools(tmp_path, service=_FakeManagedBrowserService(), subagent_depth=1)

    assert _BROWSER_TOOL_NAMES.isdisjoint(without_service)
    assert _BROWSER_TOOL_NAMES.isdisjoint(nested)


def test_verifier_browser_tools_require_capability_and_exact_child_allowlist(
    tmp_path: Path,
) -> None:
    verifier_tools = (
        "browser_start",
        "browser_navigate",
        "browser_snapshot",
        "browser_screenshot",
        "browser_artifact_read",
        "browser_diagnostics",
        "browser_click",
        "browser_type",
        "browser_status",
        "browser_list",
    )

    enabled = _build_tools(
        tmp_path,
        service=_FakeManagedBrowserService(),
        subagent_depth=1,
        child_managed_browser_tool_names=verifier_tools,
    )
    disabled = _build_tools(
        tmp_path,
        service=None,
        subagent_depth=1,
        child_managed_browser_tool_names=verifier_tools,
    )

    assert set(enabled).intersection(_BROWSER_TOOL_NAMES) == set(verifier_tools)
    assert set(disabled).isdisjoint(_BROWSER_TOOL_NAMES)


def test_readonly_mode_exposes_only_observational_browser_tools(tmp_path: Path) -> None:
    tools = _build_tools(
        tmp_path,
        service=_FakeManagedBrowserService(),
        surface=_HostApprovalSurface(),
        mode="readonly",
    )

    assert _READ_ONLY_BROWSER_TOOL_NAMES <= set(tools)
    assert (_BROWSER_TOOL_NAMES - _READ_ONLY_BROWSER_TOOL_NAMES).isdisjoint(tools)


@pytest.mark.parametrize("mode", ["auto", "review", "fullaccess"])
def test_browser_mutations_use_the_effective_engine_mode(tmp_path: Path, mode: str) -> None:
    service = _FakeManagedBrowserService()
    surface = _HostApprovalSurface(allow=False)
    tools = _build_tools(tmp_path, service=service, surface=surface, mode=mode)

    if mode == "fullaccess":
        tools["browser_start"].run({})
        assert surface.requests == []
        assert len(service.calls) == 1
        assert service.calls[0][0] == "start"
    else:
        with pytest.raises(ApprovalDeclinedError):
            tools["browser_start"].run({})
        assert [request.kind for request in surface.requests] == ["browser_start"]
        assert service.calls == []


def test_browser_mutations_fail_closed_without_host_managed_approvals(tmp_path: Path) -> None:
    service = _FakeManagedBrowserService()
    tools = _build_tools(tmp_path, service=service, surface=NoopSurface())

    with pytest.raises(AgentRuntimeError, match="host-managed approval"):
        tools["browser_start"].run({})

    assert service.calls == []


def test_browser_tool_lifecycle_is_owner_scoped_cancellable_and_secret_safe(tmp_path: Path) -> None:
    service = _FakeManagedBrowserService()
    surface = _HostApprovalSurface()
    tools = _build_tools(
        tmp_path,
        service=service,
        surface=surface,
        cancel_check=lambda: True,
    )
    sid = service.session_id

    start = tools["browser_start"].run({})
    assert start["active_url"] == "https://example.com/start/<redacted>"
    assert "allow_local_destinations" not in tools["browser_start"].parameters["properties"]
    start_call = service.calls[-1]
    assert start_call[0] == "start"
    assert start_call[1] == ("ide-owner",)
    assert start_call[2]["allow_local_destinations"] is False
    assert start_call[2]["cancel"]() is True

    target = "https://example.com/account/sk-12345678901234567890?token=top-secret#private"
    navigated = tools["browser_navigate"].run({"session_id": sid, "url": target})
    assert navigated["url"] == "https://example.com/account/<redacted>"
    assert "top-secret" not in surface.requests[-1].preview

    screenshot = tools["browser_screenshot"].run({"session_id": sid})
    assert screenshot["artifact_id"].startswith(f"browser:{sid}:")
    assert "path" not in screenshot
    assert "private/secret" not in repr(screenshot)
    artifact = tools["browser_artifact_read"].run(
        {"session_id": sid, "artifact_id": screenshot["artifact_id"]}
    )
    assert artifact["encoding"] == "base64"

    assert tools["browser_snapshot"].run({"session_id": sid})["text"] == "safe page"
    assert tools["browser_diagnostics"].run({"session_id": sid})["events"]
    assert tools["browser_click"].run({"session_id": sid, "selector": "#save"})["clicked"]

    typed_secret = "password-that-must-not-be-echoed"
    typed = tools["browser_type"].run(
        {"session_id": sid, "selector": "#password", "text": typed_secret}
    )
    assert typed["character_count"] == len(typed_secret)
    assert typed_secret not in repr(typed)
    assert typed_secret not in surface.requests[-1].preview

    assert tools["browser_status"].run({"session_id": sid})["active_url"] == (
        "https://example.com/status/<redacted>"
    )
    assert tools["browser_list"].run({})["sessions"][0]["active_url"] == (
        "https://example.com/list/<redacted>"
    )
    assert tools["browser_close"].run({"session_id": sid}) == {
        "session_id": sid,
        "closed": True,
    }
    close_call = service.calls[-1]
    assert close_call[0] == "close"
    assert close_call[2]["delete_artifacts"] is True


def test_browser_preview_allowlist_is_derived_live_from_session_services(
    tmp_path: Path,
) -> None:
    class PreviewServices:
        def __init__(self) -> None:
            self.active = [
                {
                    "service_id": "svc-owned",
                    "preview_url": "http://127.0.0.1:3000/",
                    "preview_urls": ["http://127.0.0.1:3000/"],
                }
            ]

        def list_active(self) -> list[dict[str, Any]]:
            return list(self.active)

    services = PreviewServices()
    browser = _FakeManagedBrowserService()
    tools = _build_tools(
        tmp_path,
        service=browser,
        surface=_HostApprovalSurface(),
        durable_service_manager=services,
    )

    started = tools["browser_start"].run({"url": "http://127.0.0.1:3000/incidents"})

    start_call = next(call for call in browser.calls if call[0] == "start")
    provider = start_call[2]["allowed_preview_urls_provider"]
    assert provider() == ("http://127.0.0.1:3000/",)
    assert [call[0] for call in browser.calls[:2]] == ["start", "navigate"]
    assert started["active_url"] == "http://127.0.0.1:3000/incidents"
    assert "url" in tools["browser_start"].parameters["properties"]

    services.active.clear()
    assert provider() == ()


def test_unexpected_browser_failure_does_not_expose_exception_secrets(tmp_path: Path) -> None:
    service = _FakeManagedBrowserService()
    service.fail_snapshot = True
    tools = _build_tools(tmp_path, service=service, surface=_HostApprovalSurface())

    with pytest.raises(AgentRuntimeError) as raised:
        tools["browser_snapshot"].run({"session_id": service.session_id})

    assert "sk-do-not-leak" not in str(raised.value)
    assert str(raised.value) == "Managed browser operation failed safely."


@pytest.mark.parametrize(
    ("tool_name", "args"),
    [
        ("browser_navigate", {"url": "http://127.0.0.1:3000"}),
        ("browser_snapshot", {}),
        ("browser_screenshot", {}),
        ("browser_artifact_read", {"artifact_id": "browser:local:shot.png"}),
        ("browser_diagnostics", {}),
        ("browser_click", {"selector": "#save"}),
        ("browser_type", {"selector": "#password", "text": "secret"}),
        ("browser_status", {}),
        ("browser_close", {}),
    ],
)
def test_agent_tools_cannot_access_direct_localhost_browser_sessions(
    tmp_path: Path,
    tool_name: str,
    args: dict[str, Any],
) -> None:
    service = _FakeManagedBrowserService()
    surface = _HostApprovalSurface()
    tools = _build_tools(tmp_path, service=service, surface=surface)

    with pytest.raises(AgentRuntimeError, match="direct IDE localhost testing"):
        tools[tool_name].run({"session_id": service.local_session_id, **args})

    assert all(call[0] == "status" for call in service.calls)
    assert surface.requests == []


def test_agent_browser_list_filters_direct_localhost_sessions(tmp_path: Path) -> None:
    service = _FakeManagedBrowserService()

    def _list_with_local(owner_id: str) -> tuple[SimpleNamespace, ...]:
        service._record("list", owner_id)
        return (
            service._status(active_url="https://example.com/"),
            service._status(
                session_id=service.local_session_id,
                allow_local_destinations=True,
                active_url="http://localhost:3000/private",
            ),
        )

    service.list = _list_with_local  # type: ignore[method-assign]
    tools = _build_tools(tmp_path, service=service, surface=_HostApprovalSurface())

    listed = tools["browser_list"].run({})

    assert listed["count"] == 1
    assert listed["sessions"][0]["session_id"] == service.session_id
    assert service.local_session_id not in repr(listed)


def test_mode_rebuild_preserves_managed_browser_injection() -> None:
    service = _FakeManagedBrowserService()

    def cancel_check() -> bool:
        return False

    session = SimpleNamespace(
        cfg=AppConfig(model="test-model", base_url="https://api.openai.com/v1"),
        max_steps=12,
        root=Path("."),
        store=SimpleNamespace(),
        managed_browser_service=service,
        managed_browser_owner_id="ide-owner",
        managed_browser_cancel_check=cancel_check,
    )

    kwargs = _session_build_tools_kwargs(session=session, mode="review")

    assert kwargs["managed_browser_service"] is service
    assert kwargs["managed_browser_owner_id"] == "ide-owner"
    assert kwargs["managed_browser_cancel_check"] is cancel_check
