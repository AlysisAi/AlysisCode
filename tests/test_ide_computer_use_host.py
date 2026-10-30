from __future__ import annotations

import hashlib
import io
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import alysis_code.agent.session as agent_session_module
import alysis_code.ide.stdio_bridge as stdio_bridge_module
from alysis_code.agent.computer_use_tools import (
    COMPUTER_USE_SYSTEM_PROMPT,
    COMPUTER_USE_TOOL_NAMES,
    build_computer_use_tools,
    computer_use_workflow_status,
)
from alysis_code.agent.sensitive_output import (
    redact_sensitive_tool_arguments,
    sensitive_tool_boundary,
)
from alysis_code.agent.task_artifact_binding import TOOL_NAME as ARTIFACT_BINDING_TOOL
from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig
from alysis_code.ide.artifacts import ArtifactStore
from alysis_code.ide.event_stream import EventContext, EventSequencer, ProtocolEventSurface
from alysis_code.ide.health import capabilities_payload
from alysis_code.ide.protocol import ProtocolError
from alysis_code.ide.stdio_bridge import (
    BridgeCancellationError,
    BridgeCancellationToken,
    BridgeJob,
    BridgeSession,
    ComputerUseHostBinding,
    StdioBridge,
)
from alysis_code.llm.base import (
    RequiredToolCallingUnsupportedError,
    omitted_reasoning_display_required,
    serial_tool_calls_required,
    stateless_responses_required,
)
from alysis_code.llm.types import LLMResponse, ToolCall
from alysis_code.surface.noop_surface import NoopSurface

pytestmark = pytest.mark.usefixtures("mac_computer_use_protocol")


@pytest.mark.parametrize(
    "version,interaction,valid",
    [(1, 2, True), (None, 2, False), (True, 2, False), (2, 2, False), (1, None, False)],
)
def test_application_lifecycle_host_binding_requires_exact_negotiation(version, interaction, valid):
    raw = ComputerUseHostBinding(
        operation_id="operation-lifecycle",
        broker_fingerprint="sha256:" + "a" * 64,
        authority_epoch=1,
        scope_digest="sha256:" + "b" * 64,
    ).to_protocol()
    raw["application_lifecycle_version"] = version
    if interaction is not None:
        raw["interaction_version"] = interaction
    if valid:
        bound = stdio_bridge_module._computer_use_host_param(
            {"computer_use_host": raw}, request_id="lifecycle"
        )
        assert bound.interaction_version == 2 and bound.application_lifecycle_version == 1
    else:
        with pytest.raises(ProtocolError):
            stdio_bridge_module._computer_use_host_param(
                {"computer_use_host": raw}, request_id="lifecycle"
            )


def _request(method: str, params: dict[str, Any], request_id: str = "request") -> str:
    return json.dumps(
        {
            "protocol_version": "1",
            "id": request_id,
            "method": method,
            "params": params,
        },
        sort_keys=True,
    )


def _lines(out: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]


def _wait_for_event(out: io.StringIO, event_type: str) -> dict[str, Any]:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        for line in _lines(out):
            if line.get("type") == event_type:
                return line
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {event_type}")


def _bridge_session(tmp_path: Path) -> tuple[StdioBridge, io.StringIO, BridgeSession, BridgeJob]:
    out = io.StringIO()
    bridge = StdioBridge(stdout=out)
    session_id = "computer-use-session"
    surface = ProtocolEventSurface(
        context=EventContext(session_id=session_id),
        emit=bridge._record_and_write_event,  # noqa: SLF001
        sequencer=EventSequencer(),
    )
    session = BridgeSession(
        session_id=session_id,
        root=tmp_path,
        mode="review",
        surface=surface,
        agent_session=SimpleNamespace(close=lambda: None),
        artifact_store=ArtifactStore([]),
        sensitive_media=True,
        computer_use_host=ComputerUseHostBinding(
            operation_id="operation-1",
            broker_fingerprint="sha256:" + "a" * 64,
            authority_epoch=7,
            scope_digest="sha256:" + "b" * 64,
        ),
    )
    job = BridgeJob(
        job_id="job-1",
        session_id=session_id,
        created_at="2026-08-23T00:00:00.000Z",
        status="running",
    )
    session.active_job = job
    bridge._sessions[session_id] = session  # noqa: SLF001
    bridge._jobs[job.job_id] = job  # noqa: SLF001
    surface.with_job(job.job_id)
    return bridge, out, session, job


def _invoke_host_request(
    bridge: StdioBridge,
    session: BridgeSession,
    tool_name: str,
    arguments: dict[str, Any],
    result: dict[str, Any],
) -> None:
    session.surface.with_job(session.active_job.job_id if session.active_job else None)
    session.surface.emit_tool_call_started("tool-call-1", tool_name, "[redacted]")
    result.update(bridge._exchange_computer_use(session, tool_name, arguments))  # noqa: SLF001


def test_handshake_advertises_ax_only_computer_use_without_an_initial_image() -> None:
    feature = capabilities_payload()["features"]["computer_use_host"]

    assert feature["tools"] == ["computer_state", "computer_action", "computer_wait"]
    assert feature["generic_tools_enabled"] is False
    assert feature["transient_payloads"] is True
    assert feature["vision_required"] is False
    assert feature["initial_image_required"] is False
    assert feature["host_images_optional"] is True
    assert feature["response_modes"] == ["status_only", "host_sanitized_text"]


def test_key_tool_schema_matches_the_sequence_and_modifier_chord_contract() -> None:
    tools = build_computer_use_tools(lambda _name, _arguments: {})
    keys = tools["computer_action"].parameters["properties"]["keys"]

    assert keys["minItems"] == 1
    assert keys["maxItems"] == 64
    assert "ordered 1-64 key sequence" in keys["description"]
    assert "exactly one non-modifier key" in keys["description"]


def test_computer_use_response_mode_is_exact_and_defaults_fail_closed() -> None:
    raw = ComputerUseHostBinding(
        operation_id="operation-mode",
        broker_fingerprint="sha256:" + "a" * 64,
        authority_epoch=7,
        scope_digest="sha256:" + "b" * 64,
        response_mode="host_sanitized_text",
    ).to_protocol()
    parsed = stdio_bridge_module._computer_use_host_param(  # noqa: SLF001
        {"computer_use_host": raw},
        request_id="response-mode",
    )
    assert parsed is not None
    assert parsed.response_mode == "host_sanitized_text"

    raw.pop("response_mode")
    defaulted = stdio_bridge_module._computer_use_host_param(  # noqa: SLF001
        {"computer_use_host": raw},
        request_id="response-mode-default",
    )
    assert defaulted is not None
    assert defaulted.response_mode == "status_only"

    raw["response_mode"] = "raw_provider_text"
    with pytest.raises(ProtocolError, match="response_mode is unsupported"):
        stdio_bridge_module._computer_use_host_param(  # noqa: SLF001
            {"computer_use_host": raw},
            request_id="response-mode-invalid",
        )


def test_fixed_computer_tools_are_sensitive_and_redact_typed_text() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def request(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        calls.append((name, arguments))
        if name == "computer_state":
            return {"snapshot_id": "snapshot-fresh", "source": "chrome-dom", "elements": []}
        return {
            "receipt": {"action": arguments["action"]},
            "state": {"snapshot_id": "snapshot-after", "source": "chrome-dom", "elements": []},
        }

    tools = build_computer_use_tools(request)

    assert tuple(tools) == COMPUTER_USE_TOOL_NAMES
    observed = tools["computer_state"].run({})
    assert observed["snapshot_id"] == "snapshot-fresh"
    result = tools["computer_action"].run(
        {
            "snapshot_id": "provider-guessed-stale-snapshot",
            "action": "type",
            "element_id": "private-field",
            "text": "private typed text",
        }
    )
    assert result["state"]["snapshot_id"] == "snapshot-after"
    assert calls[1][1]["text"] == "private typed text"
    assert calls[1][1]["snapshot_id"] == "snapshot-fresh"

    boundary = sensitive_tool_boundary("computer_action", calls[1][1])
    assert boundary.sensitive is True
    assert "host_ui_context" in boundary.categories
    persisted = redact_sensitive_tool_arguments("computer_action", calls[1][1], boundary=boundary)
    assert persisted["text"] != "private typed text"
    assert persisted["snapshot_id"] == "[redacted: transient computer use identifier]"
    assert persisted["element_id"] == "[redacted: transient computer use identifier]"


def test_computer_action_requires_a_trusted_fresh_observation_before_host_dispatch() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    tools = build_computer_use_tools(lambda name, arguments: calls.append((name, arguments)) or {})

    result = tools["computer_action"].run(
        {
            "snapshot_id": "provider-guessed-snapshot",
            "action": "press",
            "element_id": "verify-button",
        }
    )

    assert result["error"] == "computer_use_fresh_observation_required"
    assert calls == []


def test_history_back_injects_only_the_trusted_fresh_snapshot_before_host_dispatch() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def request(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        calls.append((name, arguments))
        if name == "computer_state":
            return {"snapshot_id": "snapshot-history", "elements": [], "source": "chrome-dom"}
        return {
            "receipt": {"action": "history_back"},
            "state": {
                "snapshot_id": "snapshot-after-history",
                "elements": [],
                "source": "chrome-dom",
            },
        }

    tools = build_computer_use_tools(request)
    tools["computer_state"].run({})
    result = tools["computer_action"].run({"action": "history_back"})

    assert result["state"]["snapshot_id"] == "snapshot-after-history"
    assert calls[1] == (
        "computer_action",
        {"action": "history_back", "snapshot_id": "snapshot-history"},
    )


def test_computer_action_clears_snapshot_after_host_error_until_a_fresh_wait() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    action_results = iter(
        [
            {"error": "host_failed", "message": "Action was not verified."},
            {
                "receipt": {"action": "press"},
                "state": {"snapshot_id": "snapshot-after", "elements": []},
            },
        ]
    )

    def request(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        calls.append((name, arguments))
        if name == "computer_state":
            return {"snapshot_id": "snapshot-before", "elements": []}
        if name == "computer_wait":
            return {"snapshot_id": "snapshot-after-wait", "elements": []}
        return next(action_results)

    tools = build_computer_use_tools(request)
    tools["computer_state"].run({})
    first = tools["computer_action"].run({"action": "press", "element_id": "verify-button"})
    refused = tools["computer_action"].run({"action": "press", "element_id": "verify-button"})
    tools["computer_wait"].run({"duration_ms": 50})
    recovered = tools["computer_action"].run({"action": "press", "element_id": "verify-button"})

    assert first["error"] == "host_failed"
    assert refused["error"] == "computer_use_fresh_observation_required"
    assert recovered["state"]["snapshot_id"] == "snapshot-after"
    assert [name for name, _arguments in calls] == [
        "computer_state",
        "computer_action",
        "computer_wait",
        "computer_action",
    ]
    assert calls[1][1]["snapshot_id"] == "snapshot-before"
    assert calls[3][1]["snapshot_id"] == "snapshot-after-wait"


def test_computer_state_cannot_reselect_the_host_bound_target() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    tools = build_computer_use_tools(
        lambda name, arguments: (
            calls.append((name, arguments)) or {"snapshot_id": "snapshot-1", "elements": []}
        )
    )

    result = tools["computer_state"].run(
        {
            "bundle_id": "hallucinated.bundle.identifier",
            "window_title": "Hallucinated window",
            "max_elements": 275,
            "page_cursor": "page_0123456789abcdef_275",
        }
    )

    assert result == {"snapshot_id": "snapshot-1", "elements": []}
    assert calls == [
        (
            "computer_state",
            {"max_elements": 275, "page_cursor": "page_0123456789abcdef_275"},
        )
    ]


def test_computer_use_tools_track_only_valid_host_workflow_progress() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def request(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        calls.append((name, arguments))
        if name == "computer_state":
            return {
                **_lab_state("snapshot-calculator", "10081"),
                "alysis_workflow": {
                    "version": 1,
                    "required": True,
                    "complete": False,
                    "current_application": "Calculator",
                    "next_application": "Google Chrome",
                    "remaining_applications": ["Google Chrome"],
                },
            }
        return {
            "receipt": {"action": "activate_application", "semantic": True},
            "state": _lab_state("snapshot-chrome", "Example Domain"),
            "alysis_workflow": {
                "version": 1,
                "required": True,
                "complete": True,
                "current_application": "Google Chrome",
                "next_application": None,
                "remaining_applications": [],
            },
        }

    tools = build_computer_use_tools(request)
    assert computer_use_workflow_status(tools) == {
        "known": False,
        "required": False,
        "complete": True,
        "next_application": None,
        "remaining_applications": (),
        "site_permission_required": False,
    }

    tools["computer_state"].run({})
    assert computer_use_workflow_status(tools) == {
        "known": True,
        "required": True,
        "complete": False,
        "next_application": "Google Chrome",
        "remaining_applications": ("Google Chrome",),
        "site_permission_required": False,
    }

    tools["computer_action"].run({"action": "activate_application", "application": "Google Chrome"})
    assert computer_use_workflow_status(tools) == {
        "known": True,
        "required": True,
        "complete": True,
        "next_application": None,
        "remaining_applications": (),
        "site_permission_required": False,
    }
    assert [name for name, _arguments in calls] == ["computer_state", "computer_action"]


class _ScriptedComputerUseClient:
    base_url = "https://api.openai.com/v1"
    route_identity = SimpleNamespace(credential_scope="computer-use-test-scope")

    def __init__(self, responses: list[LLMResponse]) -> None:
        self.model = "computer-use-test-model"
        self.temperature = 0.0
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def chat(self, **kwargs: Any) -> LLMResponse:
        self.calls.append(kwargs)
        return self.responses.pop(0)


class _BlockingCancellationAwareComputerUseClient:
    model = "computer-use-test-model"
    base_url = "https://api.openai.com/v1"
    temperature = 0.0
    route_identity = SimpleNamespace(credential_scope="computer-use-test-scope")

    def __init__(self) -> None:
        self.started = threading.Event()
        self.cancellation_observed = threading.Event()
        self.calls: list[dict[str, Any]] = []

    def chat(self, *, cancellation_token: Any, **kwargs: Any) -> LLMResponse:
        self.calls.append({"cancellation_token": cancellation_token, **kwargs})
        self.started.set()
        if not cancellation_token.wait_cancelled(2):
            raise AssertionError("provider did not receive Stop through its cancellation token")
        self.cancellation_observed.set()
        # A transport may report its abort as an ordinary provider failure. Once
        # session.cancel has won, that late exception must not rewrite the job to failed.
        raise RuntimeError("provider transport aborted after cancellation")


def _sensitive_agent_bridge_session(
    tmp_path: Path,
    client: _ScriptedComputerUseClient | _BlockingCancellationAwareComputerUseClient,
) -> tuple[StdioBridge, io.StringIO, BridgeSession]:
    out = io.StringIO()
    bridge = StdioBridge(stdout=out)
    session_id = "computer-use-stop-session"
    session_ref: dict[str, BridgeSession] = {}
    surface = ProtocolEventSurface(
        context=EventContext(session_id=session_id),
        emit=bridge._record_and_write_event,  # noqa: SLF001
        sequencer=EventSequencer(),
    )
    agent_session = create_session(
        cfg=AppConfig(model=client.model, routing_mode="code_only"),
        root=tmp_path,
        mode="review",
        yes=False,
        max_steps=3,
        no_log=True,
        api_key_override="test-key",
        surface=surface,
        sensitive_media=True,
        computer_use_host_request=lambda name, arguments: bridge._request_computer_use(  # noqa: SLF001
            session_ref["session"], name, arguments
        ),
    )
    agent_session.client = client
    session = BridgeSession(
        session_id=session_id,
        root=tmp_path,
        mode="review",
        surface=surface,
        agent_session=agent_session,
        artifact_store=ArtifactStore([]),
        sensitive_media=True,
        computer_use_destination=stdio_bridge_module._actual_computer_use_destination(  # noqa: SLF001
            agent_session,
            request_id="test-destination",
        ),
        computer_use_host=ComputerUseHostBinding(
            operation_id="operation-stop",
            broker_fingerprint="sha256:" + "a" * 64,
            authority_epoch=7,
            scope_digest="sha256:" + "b" * 64,
        ),
    )
    session_ref["session"] = session
    bridge._sessions[session_id] = session  # noqa: SLF001
    return bridge, out, session


class _RecordingSurface(NoopSurface):
    def __init__(self) -> None:
        super().__init__()
        self.final_messages: list[str] = []
        self.closed_errors: list[tuple[str, str, bool]] = []

    def emit_error(self, code: str, message: str, recoverable: bool, **kwargs: Any) -> None:
        self.closed_errors.append((code, message, recoverable))

    def on_assistant_message_done(self, text: str) -> None:
        self.final_messages.append(text)


def test_record_search_guidance_reaches_each_provider_step_without_retaining_prior_rows(
    tmp_path: Path,
) -> None:
    """Exercise prompt delivery/privacy, not a scripted model's search competence."""
    host_calls: list[tuple[str, dict[str, Any]]] = []
    immediate_messages: list[list[dict[str, Any]]] = []
    cursor = "page_0123456789abcdef_20"
    canaries = ["SYNTHETIC_PRIVATE_ROW_A", "SYNTHETIC_PRIVATE_ROW_B", "SYNTHETIC_PRIVATE_ROW_C"]

    def observation(index: int) -> dict[str, Any]:
        return {
            "snapshot_id": f"records-snapshot-{index}",
            "source": "chrome-dom",
            "target": {"bundle_id": "ai.alysis.alysis.chrome-tab"},
            "page": {
                "scope": "document",
                "complete": index != 0,
                "next_cursor": cursor if index == 0 else None,
                "scan_truncated": False,
                "viewport": {"at_bottom": index == 2},
            },
            "elements": [
                {"id": "records_list", "role": "AXScrollArea", "title": "Record history"},
                {
                    "id": "range",
                    "role": "AXStaticText",
                    "value": "Showing 1-2 of 6" if index < 2 else "Showing 3-4 of 6",
                },
                {"id": f"row_{index}", "role": "AXStaticText", "value": canaries[index]},
                {
                    "id": "amount",
                    "role": "AXStaticText",
                    "value": "0.00" if index == 1 else "18.25",
                },
            ],
        }

    def host_request(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        host_calls.append((name, arguments))
        index = len(host_calls) - 1
        if index < 2:
            assert name == "computer_state"
            assert arguments == (
                {"max_elements": 500}
                if index == 0
                else {"max_elements": 500, "page_cursor": cursor}
            )
            return observation(index)
        assert name == "computer_action"
        assert arguments == {
            "action": "scroll",
            "element_id": "records_list",
            "delta_y": 500,
            "snapshot_id": "records-snapshot-1",
        }
        return {"receipt": {"action": "scroll", "semantic": True}, "state": observation(index)}

    partial = "This is a partial result. The remaining history and its total were not verified."
    responses = [
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(id="records_first", name="computer_state", arguments={"max_elements": 500})
            ],
            raw={},
        ),
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(
                    id="records_cursor",
                    name="computer_state",
                    arguments={"max_elements": 500, "page_cursor": cursor},
                )
            ],
            raw={},
        ),
        LLMResponse(
            content="",
            tool_calls=[
                ToolCall(
                    id="records_scroll",
                    name="computer_action",
                    arguments={"action": "scroll", "element_id": "records_list", "delta_y": 500},
                )
            ],
            raw={},
        ),
        LLMResponse(content=partial, tool_calls=[], raw={}),
    ]

    class InspectingClient(_ScriptedComputerUseClient):
        def chat(self, **kwargs: Any) -> LLMResponse:
            # Runtime scrubs the actual request object after this call. Capture only
            # synthetic input here to assert exactly what this provider call saw.
            immediate_messages.append(json.loads(json.dumps(kwargs["messages"])))
            return super().chat(**kwargs)

    surface = _RecordingSurface()
    session = create_session(
        cfg=AppConfig(model="computer-use-test-model", routing_mode="code_only"),
        root=tmp_path,
        mode="review",
        yes=False,
        max_steps=6,
        no_log=True,
        api_key_override="test-key",
        one_shot_execution=True,
        surface=surface,
        sensitive_media=True,
        trusted_system_prompt_override=COMPUTER_USE_SYSTEM_PROMPT,
        computer_use_host_request=host_request,
        computer_use_response_mode="host_sanitized_text",
    )
    client = InspectingClient(responses)
    session.client = client  # type: ignore[assignment]
    try:
        assert session.run_turn("find my invoices") == 0
        durable = json.dumps(session.messages)
        assert not hasattr(session, "task_state")
        assert session._task_evidence_state is None  # noqa: SLF001
        retained_events = json.dumps(session.store.events_snapshot())
        assert session.store.enabled is False
        assert session.store.artifact_persistence_enabled is False
    finally:
        session.close()

    assert len(immediate_messages) == 4
    for index, messages in enumerate(immediate_messages):
        system_text = "\n".join(
            str(message.get("content", ""))
            for message in messages
            if message.get("role") == "system"
        )
        assert COMPUTER_USE_SYSTEM_PROMPT in system_text
        for obligation in (
            "even without the word",
            "loaded DOM was read",
            "Showing X-Y of Z",
            "Report zero-amount records separately",
            "explicitly report a partial result",
            "Earlier observations may be redacted",
        ):
            assert obligation in system_text
        serialized = json.dumps(messages)
        for row_index, canary in enumerate(canaries):
            assert (canary in serialized) is (row_index == index - 1), (
                "only the immediately preceding observation may reach this provider call"
            )
        assert (
            tuple(tool["function"]["name"] for tool in client.calls[index]["tools"])
            == COMPUTER_USE_TOOL_NAMES
        )
    # The runtime adds its own tagged context messages (environment and
    # workspace binding); the user's request itself must reach the provider exactly
    # as typed, with no count, history, or navigation hints folded into it.
    user_contents = [
        str(message.get("content", ""))
        for message in immediate_messages[0]
        if message.get("role") == "user"
    ]
    assert user_contents.count("find my invoices") == 1
    # Each request remains normal conversation context, without a pinned objective.
    task_briefs = [content for content in user_contents if content.startswith("<task_brief>")]
    assert task_briefs == []
    assert [
        content
        for content in user_contents
        if "find my invoices" in content and content != "find my invoices"
    ] == []
    assert surface.final_messages == [partial]
    for canary in canaries:
        assert canary not in durable
        assert canary not in retained_events
        assert canary not in json.dumps(client.calls), "consumed request objects must be scrubbed"
    assert not any(tmp_path.iterdir())


def _lab_state(snapshot: str, result: str) -> dict[str, Any]:
    return {
        "snapshot_id": snapshot,
        "target": {
            "bundle_id": "ai.alysis.alysis.computer-use-lab",
            "pid": 4242,
            "window_title": "Alysis Computer Use Lab",
            "window_title_digest": "sha256:" + "c" * 64,
            "bounds": {"x": 0, "y": 0, "width": 760, "height": 640},
        },
        "elements": [
            {"id": "element_10_0abc", "role": "AXTextField", "label": "Dogfood value"},
            {"id": "element_20_0def", "role": "AXButton", "title": "Verify dogfood value"},
            {"id": "result", "role": "AXStaticText", "value": result},
            {"id": "counter_0", "role": "AXStaticText", "value": "0"},
        ],
        "element_count": 4,
        "secure_values_redacted": True,
        "available_applications": [{"bundle_id": "ai.alysis.alysis.computer-use-lab", "pid": 4242}],
    }


def test_sensitive_session_runs_a_multi_step_computer_use_loop_without_generic_tools(
    tmp_path: Path,
) -> None:
    host_calls: list[tuple[str, dict[str, Any]]] = []
    snapshots = iter(["snapshot-1", "snapshot-2", "snapshot-3", "snapshot-4"])

    def host_request(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        host_calls.append((name, arguments))
        if name == "computer_state":
            return _lab_state(next(snapshots), "READY")
        if name == "computer_action":
            action = arguments.get("action")
            result = "PASS: Alysis dogfood 2026" if action == "press" else "READY"
            return {
                "receipt": {"action": action, "semantic": True},
                "state": _lab_state(next(snapshots), result),
            }
        raise AssertionError(f"unexpected Computer Use tool: {name}")

    session = create_session(
        cfg=AppConfig(model="computer-use-test-model", routing_mode="code_only"),
        root=tmp_path,
        mode="review",
        yes=False,
        max_steps=8,
        no_log=False,
        api_key_override="test-key",
        sensitive_media=True,
        computer_use_host_request=host_request,
    )
    client = _ScriptedComputerUseClient(
        [
            LLMResponse(
                content="I will inspect the approved app.",
                tool_calls=[ToolCall(id="state-1", name="computer_state", arguments={})],
                raw={},
            ),
            LLMResponse(
                content="I found the input.",
                tool_calls=[
                    ToolCall(
                        id="set-1",
                        name="computer_action",
                        arguments={
                            "action": "set_value",
                            "element_id": "element_10_0abc",
                            "text": "Alysis dogfood 2026",
                        },
                    )
                ],
                raw={},
            ),
            LLMResponse(
                content="I will verify it.",
                tool_calls=[
                    ToolCall(
                        id="press-1",
                        name="computer_action",
                        arguments={
                            "action": "press",
                            "element_id": "element_20_0def",
                        },
                    )
                ],
                raw={},
            ),
            LLMResponse(
                content="The post-action observation shows PASS: Alysis dogfood 2026.",
                tool_calls=[],
                raw={},
            ),
        ]
    )
    session.client = client  # type: ignore[assignment]
    try:
        assert session.run_turn("Complete the deterministic Computer Use Lab task.") == 0
        durable_messages = json.dumps(session.messages, ensure_ascii=False)
    finally:
        session.close()

    assert [name for name, _arguments in host_calls] == [
        "computer_state",
        "computer_action",
        "computer_action",
    ]
    assert host_calls[1][1] == {
        "action": "set_value",
        "element_id": "element_10_0abc",
        "text": "Alysis dogfood 2026",
        "snapshot_id": "snapshot-1",
    }
    assert host_calls[2][1] == {
        "action": "press",
        "element_id": "element_20_0def",
        "snapshot_id": "snapshot-2",
    }
    assert "Alysis dogfood 2026" not in durable_messages
    assert "element_10_0abc" not in durable_messages
    assert "snapshot-1" not in durable_messages
    assert tuple(tool["function"]["name"] for tool in client.calls[0]["tools"]) == (
        "computer_state",
        "computer_action",
        "computer_wait",
    )
    for call in client.calls:
        assert tuple(tool["function"]["name"] for tool in call["tools"]) == (
            "computer_state",
            "computer_action",
            "computer_wait",
        )
    assert not any(tmp_path.iterdir())


def test_sensitive_session_recovers_when_model_initially_answers_without_host_call(
    tmp_path: Path,
) -> None:
    host_calls: list[tuple[str, dict[str, Any]]] = []

    def host_request(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        host_calls.append((name, arguments))
        assert name == "computer_state"
        return _lab_state("snapshot-recovered", "READY")

    surface = _RecordingSurface()
    session = create_session(
        cfg=AppConfig(model="computer-use-test-model", routing_mode="code_only"),
        root=tmp_path,
        mode="review",
        yes=False,
        max_steps=6,
        no_log=True,
        api_key_override="test-key",
        surface=surface,
        sensitive_media=True,
        computer_use_host_request=host_request,
    )
    client = _ScriptedComputerUseClient(
        [
            LLMResponse(content="Select a Computer Use target first.", tool_calls=[], raw={}),
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="state-retry", name="computer_state", arguments={})],
                raw={},
            ),
            LLMResponse(
                content=json.dumps(
                    {"outcome": "completed_unverified", "summary": "The approved target is ready."}
                ),
                tool_calls=[],
                raw={},
            ),
        ]
    )
    session.client = client  # type: ignore[assignment]
    try:
        assert session.run_turn("Observe the automatically selected app.") == 0
    finally:
        session.close()

    assert [name for name, _arguments in host_calls] == ["computer_state"]
    assert len(client.calls) == 3
    retry_messages = json.dumps(client.calls[1]["messages"], ensure_ascii=False)
    assert "Call computer_state now" in retry_messages
    assert "Select a Computer Use target first" not in retry_messages
    assert surface.final_messages == [
        "Computer Use run ended. The reported result has not been independently verified."
    ]
    assert not any(tmp_path.iterdir())


def test_sensitive_session_recovers_when_model_attempts_action_before_observation(
    tmp_path: Path,
) -> None:
    host_calls: list[tuple[str, dict[str, Any]]] = []

    def host_request(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        host_calls.append((name, arguments))
        if name == "computer_state":
            return _lab_state("snapshot-recovered", "READY")
        if name == "computer_action":
            return {
                "receipt": {"action": "press", "semantic": True},
                "state": _lab_state("snapshot-after-press", "PASS"),
            }
        raise AssertionError(f"unexpected Computer Use tool: {name}")

    surface = _RecordingSurface()
    session = create_session(
        cfg=AppConfig(model="computer-use-test-model", routing_mode="code_only"),
        root=tmp_path,
        mode="review",
        yes=False,
        max_steps=7,
        no_log=True,
        api_key_override="test-key",
        surface=surface,
        sensitive_media=True,
        computer_use_host_request=host_request,
    )
    client = _ScriptedComputerUseClient(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="early-press",
                        name="computer_action",
                        arguments={"action": "press", "element_id": "element_20_0def"},
                    )
                ],
                raw={},
            ),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(id="state-after-correction", name="computer_state", arguments={})
                ],
                raw={},
            ),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="verified-press",
                        name="computer_action",
                        arguments={"action": "press", "element_id": "element_20_0def"},
                    )
                ],
                raw={},
            ),
            LLMResponse(
                content=json.dumps(
                    {
                        "outcome": "completed_unverified",
                        "summary": "The verified control now shows PASS.",
                    }
                ),
                tool_calls=[],
                raw={},
            ),
        ]
    )
    session.client = client  # type: ignore[assignment]
    try:
        assert session.run_turn("Press the selected control and verify it.") == 0
    finally:
        session.close()

    # The premature action is handled locally and never reaches the trusted host.
    assert [name for name, _arguments in host_calls] == ["computer_state", "computer_action"]
    correction_messages = json.dumps(client.calls[1]["messages"], ensure_ascii=False)
    assert "prior action was not sent to the host" in correction_messages
    assert surface.final_messages == [
        "Computer Use run ended. The reported result has not been independently verified."
    ]
    assert not any(tmp_path.iterdir())


def test_sensitive_session_switches_only_when_model_requests_for_user_goal(tmp_path: Path) -> None:
    host_calls: list[tuple[str, dict[str, Any]]] = []
    transitioned = False

    def host_request(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        nonlocal transitioned
        host_calls.append((name, arguments))
        if name == "computer_state":
            if transitioned:
                return {
                    **_lab_state("snapshot-chrome-observed", "Example Domain"),
                    "alysis_workflow": {
                        "version": 1,
                        "required": True,
                        "complete": True,
                        "current_application": "Google Chrome",
                        "next_application": None,
                        "remaining_applications": [],
                    },
                }
            return {
                **_lab_state("snapshot-calculator", "10081"),
                "alysis_workflow": {
                    "version": 1,
                    "required": True,
                    "complete": False,
                    "current_application": "Calculator",
                    "next_application": "Google Chrome",
                    "remaining_applications": ["Google Chrome"],
                },
            }
        assert name == "computer_action"
        assert arguments == {
            "action": "activate_application",
            "application": "Google Chrome",
            "snapshot_id": "snapshot-calculator",
        }
        transitioned = True
        return {
            "receipt": {"action": "activate_application", "semantic": True},
            "state": _lab_state("snapshot-chrome", "Example Domain"),
            "alysis_workflow": {
                "version": 1,
                "required": True,
                "complete": True,
                "current_application": "Google Chrome",
                "next_application": None,
                "remaining_applications": [],
            },
        }

    surface = _RecordingSurface()
    session = create_session(
        cfg=AppConfig(model="computer-use-test-model", routing_mode="code_only"),
        root=tmp_path,
        mode="review",
        yes=False,
        max_steps=6,
        no_log=True,
        api_key_override="test-key",
        surface=surface,
        sensitive_media=True,
        computer_use_host_request=host_request,
    )
    client = _ScriptedComputerUseClient(
        [
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="state-calculator", name="computer_state", arguments={})],
                raw={},
            ),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="model-transition",
                        name="computer_action",
                        arguments={
                            "action": "activate_application",
                            "application": "Google Chrome",
                        },
                    )
                ],
                raw={},
            ),
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="state-chrome", name="computer_state", arguments={})],
                raw={},
            ),
            LLMResponse(
                content=json.dumps(
                    {
                        "outcome": "completed_unverified",
                        "summary": "The full itinerary is complete.",
                    }
                ),
                tool_calls=[],
                raw={},
            ),
        ]
    )
    session.client = client  # type: ignore[assignment]
    try:
        assert session.run_turn("Use Calculator, then use Google Chrome.") == 0
    finally:
        session.close()

    assert [name for name, _arguments in host_calls] == [
        "computer_state",
        "computer_action",
        "computer_state",
    ]
    assert len(client.calls) == 4
    assert "approved application itinerary is incomplete" not in json.dumps(client.calls)
    assert surface.final_messages == [
        "Computer Use run ended. The reported result has not been independently verified."
    ]
    assert not any(tmp_path.iterdir())


def test_allowed_app_list_does_not_force_visits_or_claim_verified_completion(
    tmp_path: Path,
) -> None:
    calls = []

    def host(name, args):
        calls.append(name)
        assert name == "computer_state"
        return {
            **_lab_state("snapshot-1", "READY"),
            "alysis_workflow": {
                "version": 1,
                "required": True,
                "complete": False,
                "current_application": "Calculator",
                "next_application": "Google Chrome",
                "remaining_applications": ["Google Chrome"],
            },
        }

    surface = _RecordingSurface()
    session = create_session(
        cfg=AppConfig(model="computer-use-test-model", routing_mode="code_only"),
        root=tmp_path,
        mode="review",
        yes=False,
        max_steps=4,
        no_log=True,
        api_key_override="test-key",
        surface=surface,
        sensitive_media=True,
        computer_use_host_request=host,
    )
    session.client = _ScriptedComputerUseClient(
        [
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="read", name="computer_state", arguments={})],
                raw={},
            ),
            LLMResponse(
                content=json.dumps(
                    {
                        "outcome": "completed_unverified",
                        "summary": "Requested calculator result observed.",
                    }
                ),
                tool_calls=[],
                raw={},
            ),
        ]
    )
    try:
        assert session.run_turn("Read the calculator result. Chrome is available if needed.") == 0
    finally:
        session.close()
    assert calls == ["computer_state"]
    assert surface.final_messages == [
        "Computer Use run ended. The reported result has not been independently verified."
    ]


def test_sensitive_stop_after_provider_return_prevents_tool_dispatch(tmp_path: Path) -> None:
    cancellation_event = threading.Event()
    host_calls: list[tuple[str, dict[str, Any]]] = []

    class _CancelOnReturnClient(_ScriptedComputerUseClient):
        def chat(self, **kwargs: Any) -> LLMResponse:
            response = super().chat(**kwargs)
            cancellation_event.set()
            return response

    session = create_session(
        cfg=AppConfig(model="computer-use-test-model", routing_mode="code_only"),
        root=tmp_path,
        mode="review",
        yes=False,
        max_steps=3,
        no_log=True,
        api_key_override="test-key",
        sensitive_media=True,
        computer_use_host_request=lambda name, arguments: (
            host_calls.append((name, arguments)) or _lab_state("snapshot-never", "READY")
        ),
    )
    session.client = _CancelOnReturnClient(
        [
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="state-after-stop", name="computer_state", arguments={})],
                raw={},
            )
        ]
    )  # type: ignore[assignment]
    try:
        with pytest.raises(BridgeCancellationError, match="cancelled_by_user") as raised:
            session.run_turn(
                "Observe the selected app.",
                cancellation_token=BridgeCancellationToken(cancellation_event),
            )
    finally:
        session.close()

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert host_calls == []


@pytest.mark.parametrize("bind_first", [False, True])
def test_stdio_bridge_stop_cancels_blocked_sensitive_computer_use_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bind_first: bool
) -> None:
    client = _BlockingCancellationAwareComputerUseClient()
    instruction = "Observe the approved application, then act only if needed."
    accepted_bindings: list[Any] = []
    if bind_first:
        from alysis_code.agent import task_artifact_binding

        bind = task_artifact_binding.bind_path_interpretation

        def record_accepted_binding(**kwargs: Any) -> Any:
            refs = bind(**kwargs)
            accepted_bindings.append(refs)
            return refs

        monkeypatch.setattr(
            task_artifact_binding, "bind_path_interpretation", record_accepted_binding
        )
        instruction = "Inspect Planning notes.txt in the approved application."
        blocking_chat = client.chat

        def bind_then_block(*, cancellation_token: Any, **kwargs: Any) -> LLMResponse:
            if not client.calls:
                client.calls.append({"cancellation_token": cancellation_token, **kwargs})
                return LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCall(
                            id="declare-before-stop",
                            name=ARTIFACT_BINDING_TOOL,
                            arguments={
                                "paths": [
                                    {
                                        "name": "Planning notes.txt",
                                        "directory": "",
                                        "role": "existing_input",
                                        "evidence_quote": instruction,
                                    }
                                ]
                            },
                        )
                    ],
                    raw={},
                )
            return blocking_chat(cancellation_token=cancellation_token, **kwargs)

        client.chat = bind_then_block  # type: ignore[method-assign]
    bridge, out, session = _sensitive_agent_bridge_session(tmp_path, client)
    job: BridgeJob | None = None
    try:
        assert session.agent_session.sensitive_media is True
        bridge.process_line(
            _request(
                "chat.send",
                {
                    "session_id": session.session_id,
                    "message": instruction,
                },
                "start-sensitive-turn",
            )
            + "\n"
        )
        started = next(line for line in _lines(out) if line.get("id") == "start-sensitive-turn")
        assert started["ok"] is True
        job_id = started["result"]["job_id"]
        job = bridge._jobs[job_id]  # noqa: SLF001
        assert client.started.wait(2), "provider invocation did not start"
        if bind_first:
            assert accepted_bindings == []

        bridge.process_line(
            _request(
                "session.cancel",
                {"session_id": session.session_id, "reason": "Stop"},
                "stop-sensitive-turn",
            )
            + "\n"
        )
        stopped = next(line for line in _lines(out) if line.get("id") == "stop-sensitive-turn")
        assert stopped["ok"] is True
        assert stopped["result"]["status"] == "cancellation_requested"

        final_status: dict[str, Any] | None = None
        deadline = time.monotonic() + 2
        poll = 0
        while time.monotonic() < deadline:
            request_id = f"sensitive-status-{poll}"
            bridge.process_line(_request("job.status", {"job_id": job_id}, request_id) + "\n")
            response = next(line for line in _lines(out) if line.get("id") == request_id)
            assert response["ok"] is True
            final_status = response["result"]
            if final_status["status"] == "cancelled":
                break
            assert final_status["status"] == "cancellation_requested"
            poll += 1
            time.sleep(0.01)

        assert final_status is not None
        assert final_status["status"] == "cancelled"
        assert final_status["state"] == "cancelled"
        assert final_status["exit_code"] == 130
        assert job.thread is not None
        job.thread.join(timeout=2)
        assert not job.thread.is_alive()
        assert client.cancellation_observed.is_set()
        assert len(client.calls) == (2 if bind_first else 1)
        if bind_first:
            assert accepted_bindings == []
            contract = session.agent_session._turn_execution_state.acceptance_contract
            assert contract is None
        assert (
            tuple(tool["function"]["name"] for tool in client.calls[0]["tools"])
            == COMPUTER_USE_TOOL_NAMES
        )

        output = _lines(out)
        terminal_events = [
            line
            for line in output
            if line.get("type") == "warning_emitted"
            and str(line.get("payload", {}).get("message") or "").startswith(
                f"job_cancelled {job_id} "
            )
        ]
        assert len(terminal_events) == 1
        assert not any(line.get("type") == "computer_use_request" for line in output)
    finally:
        if job is not None and job.thread is not None:
            job.thread.join(timeout=2)
        bridge.close()


def test_sensitive_stop_after_provider_checkpoint_suppresses_late_job_events(
    tmp_path: Path,
) -> None:
    client = _ScriptedComputerUseClient(
        [
            LLMResponse(
                content="This provider response must not be emitted after Stop.",
                tool_calls=[],
                raw={},
            )
        ]
    )
    bridge, out, session = _sensitive_agent_bridge_session(tmp_path, client)
    usage_started = threading.Event()
    release_usage = threading.Event()
    original_record_usage = session.agent_session._record_llm_usage

    def blocking_record_usage(*args: Any, **kwargs: Any) -> Any:
        usage_started.set()
        assert release_usage.wait(2), "test did not release post-provider usage recording"
        return original_record_usage(*args, **kwargs)

    session.agent_session._record_llm_usage = blocking_record_usage
    job: BridgeJob | None = None
    try:
        bridge.process_line(
            _request(
                "chat.send",
                {
                    "session_id": session.session_id,
                    "message": "Report whether the approved workflow is complete.",
                },
                "start-post-provider-turn",
            )
            + "\n"
        )
        started = next(line for line in _lines(out) if line.get("id") == "start-post-provider-turn")
        job_id = started["result"]["job_id"]
        job = bridge._jobs[job_id]  # noqa: SLF001
        assert usage_started.wait(2), "turn did not reach the post-provider barrier"
        assert len(client.calls) == 1
        output_before_stop = len(_lines(out))

        bridge.process_line(
            _request(
                "session.cancel",
                {"session_id": session.session_id, "reason": "Stop"},
                "stop-post-provider-turn",
            )
            + "\n"
        )
        stopped = next(line for line in _lines(out) if line.get("id") == "stop-post-provider-turn")
        assert stopped["result"]["status"] == "cancellation_requested"
        release_usage.set()

        final_status: dict[str, Any] | None = None
        deadline = time.monotonic() + 2
        poll = 0
        while time.monotonic() < deadline:
            request_id = f"post-provider-status-{poll}"
            bridge.process_line(_request("job.status", {"job_id": job_id}, request_id) + "\n")
            response = next(line for line in _lines(out) if line.get("id") == request_id)
            final_status = response["result"]
            if final_status["status"] == "cancelled":
                break
            assert final_status["status"] == "cancellation_requested"
            poll += 1
            time.sleep(0.01)

        assert final_status is not None
        assert final_status["status"] == "cancelled"
        assert final_status["exit_code"] == 130
        assert job.thread is not None
        job.thread.join(timeout=2)
        assert not job.thread.is_alive()

        output = _lines(out)
        stop_ack_index = next(
            index
            for index, line in enumerate(output)
            if line.get("id") == "stop-post-provider-turn"
        )
        after_stop = output[output_before_stop:]
        after_ack = output[stop_ack_index + 1 :]
        terminal_events = [
            line
            for line in output
            if line.get("type") == "warning_emitted"
            and str(line.get("payload", {}).get("message") or "").startswith(
                f"job_cancelled {job_id} reason="
            )
        ]
        assert len(terminal_events) == 1
        assert not any(
            line.get("type")
            in {
                "message_delta",
                "message_end",
                "tool_call_started",
                "tool_call_progress",
                "tool_call_completed",
            }
            for line in after_stop
        )
        assert [line for line in after_ack if line.get("type")] == terminal_events
    finally:
        release_usage.set()
        if job is not None and job.thread is not None:
            job.thread.join(timeout=2)
        bridge.close()


def test_stdio_bridge_stop_cancels_pending_host_request_without_post_ack_failure(
    tmp_path: Path,
) -> None:
    client = _ScriptedComputerUseClient(
        [
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="pending-state", name="computer_state", arguments={})],
                raw={},
            )
        ]
    )
    bridge, out, session = _sensitive_agent_bridge_session(tmp_path, client)
    job: BridgeJob | None = None
    try:
        bridge.process_line(
            _request(
                "chat.send",
                {
                    "session_id": session.session_id,
                    "message": "Observe the approved application.",
                },
                "start-pending-host-turn",
            )
            + "\n"
        )
        started = next(line for line in _lines(out) if line.get("id") == "start-pending-host-turn")
        job_id = started["result"]["job_id"]
        job = bridge._jobs[job_id]  # noqa: SLF001
        host_request = _wait_for_event(out, "computer_use_request")
        assert host_request["job_id"] == job_id
        assert host_request["payload"]["tool_name"] == "computer_state"
        output_before_stop = len(_lines(out))

        bridge.process_line(
            _request(
                "session.cancel",
                {"session_id": session.session_id, "reason": "Stop"},
                "stop-pending-host-turn",
            )
            + "\n"
        )
        stopped = next(line for line in _lines(out) if line.get("id") == "stop-pending-host-turn")
        assert stopped["result"]["status"] == "cancellation_requested"

        final_status: dict[str, Any] | None = None
        deadline = time.monotonic() + 2
        poll = 0
        while time.monotonic() < deadline:
            request_id = f"pending-host-status-{poll}"
            bridge.process_line(_request("job.status", {"job_id": job_id}, request_id) + "\n")
            response = next(line for line in _lines(out) if line.get("id") == request_id)
            final_status = response["result"]
            if final_status["status"] == "cancelled":
                break
            assert final_status["status"] == "cancellation_requested"
            poll += 1
            time.sleep(0.01)

        assert final_status is not None
        assert final_status["status"] == "cancelled"
        assert final_status["exit_code"] == 130
        assert job.thread is not None
        job.thread.join(timeout=2)
        assert not job.thread.is_alive()
        assert len(client.calls) == 1
        assert session.pending_computer_use == {}

        output = _lines(out)
        stop_ack_index = next(
            index for index, line in enumerate(output) if line.get("id") == "stop-pending-host-turn"
        )
        after_stop = output[output_before_stop:]
        after_ack = output[stop_ack_index + 1 :]
        terminal_events = [
            line
            for line in output
            if line.get("type") == "warning_emitted"
            and str(line.get("payload", {}).get("message") or "").startswith(
                f"job_cancelled {job_id} "
            )
        ]
        assert len(terminal_events) == 1
        assert "computer_use_host_failure" not in json.dumps(after_stop, ensure_ascii=False)
        assert not any(line.get("type") == "message_end" for line in after_stop)
        assert not any(line.get("type") == "tool_call_completed" for line in after_stop)
        assert "computer_use_host_failure" not in json.dumps(after_ack, ensure_ascii=False)
        assert not any(line.get("type") == "message_end" for line in after_ack)
        assert not any(line.get("type") == "tool_call_completed" for line in after_ack)
    finally:
        if job is not None and job.thread is not None:
            job.thread.join(timeout=2)
        bridge.close()


def test_session_cancel_wins_when_completion_is_waiting_for_the_state_lock(
    tmp_path: Path,
) -> None:
    class _CompletionRaceLock:
        def __init__(self) -> None:
            self._lock = threading.RLock()
            self.cancel_holds_lock = threading.Event()
            self.worker_waiting = threading.Event()

        def __enter__(self) -> _CompletionRaceLock:
            if self.cancel_holds_lock.is_set() and threading.current_thread().name.startswith(
                "alysis-ide-"
            ):
                self.worker_waiting.set()
            self._lock.acquire()
            return self

        def __exit__(self, *_args: Any) -> None:
            self._lock.release()

    class _SuccessfulTurn:
        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()

        def run_turn(self, _message: str, *, cancellation_token: Any) -> int:
            assert cancellation_token is not None
            self.started.set()
            assert self.release.wait(2), "test did not release the successful turn"
            return 0

        def close(self) -> None:
            return

    bridge, out, session, job = _bridge_session(tmp_path)
    session.sensitive_media = False
    successful_turn = _SuccessfulTurn()
    session.agent_session = successful_turn
    race_lock = _CompletionRaceLock()
    bridge._state_lock = race_lock  # type: ignore[assignment]  # noqa: SLF001
    worker = threading.Thread(
        target=bridge._run_session_turn,  # noqa: SLF001
        args=(session, job, "Finish successfully.", [], []),
        name=f"alysis-ide-{job.job_id}",
        daemon=True,
    )
    bridge._start_job_thread(session, job, worker)  # noqa: SLF001
    try:
        assert successful_turn.started.wait(2), "turn worker did not start"
        with race_lock:
            race_lock.cancel_holds_lock.set()
            try:
                successful_turn.release.set()
                assert race_lock.worker_waiting.wait(2), "worker did not reach final transition"
                bridge.process_line(
                    _request(
                        "session.cancel",
                        {"session_id": session.session_id, "reason": "Stop"},
                        "cancel-at-completion",
                    )
                    + "\n"
                )
            finally:
                race_lock.cancel_holds_lock.clear()

        worker.join(timeout=2)
        assert not worker.is_alive()
        bridge.process_line(
            _request("job.status", {"job_id": job.job_id}, "completion-race-status") + "\n"
        )
        response = next(line for line in _lines(out) if line.get("id") == "completion-race-status")
        assert response["result"]["status"] == "cancelled"
        assert response["result"]["exit_code"] == 130
        assert not any(
            line.get("type") == "info_emitted"
            and str(line.get("payload", {}).get("message") or "").startswith("job_completed ")
            for line in _lines(out)
        )
    finally:
        successful_turn.release.set()
        worker.join(timeout=2)
        bridge.close()


def test_sensitive_max_step_termination_never_starts_a_summary_provider_call(
    tmp_path: Path,
) -> None:
    host_calls: list[tuple[str, dict[str, Any]]] = []
    surface = _RecordingSurface()
    session = create_session(
        cfg=AppConfig(model="computer-use-test-model", routing_mode="code_only"),
        root=tmp_path,
        mode="review",
        yes=False,
        max_steps=1,
        no_log=True,
        api_key_override="test-key",
        surface=surface,
        sensitive_media=True,
        computer_use_host_request=lambda name, arguments: (
            host_calls.append((name, arguments)) or _lab_state("snapshot-max-step", "READY")
        ),
    )
    client = _ScriptedComputerUseClient(
        [
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="max-step-state", name="computer_state", arguments={})],
                raw={},
            )
        ]
    )
    session.client = client  # type: ignore[assignment]
    try:
        assert session.run_turn("Observe the selected app once.") == 1
    finally:
        session.close()

    assert len(client.calls) == 1
    assert surface.closed_errors[-1:] == [
        (
            "computer_use_step_limit",
            "Computer Use stopped without verified completion.",
            False,
        )
    ]
    assert [name for name, _arguments in host_calls] == ["computer_state"]


def test_sensitive_explicit_reply_language_uses_only_bounded_protocol_retries(
    tmp_path: Path,
) -> None:
    host_calls: list[tuple[str, dict[str, Any]]] = []
    session = create_session(
        cfg=AppConfig(
            model="computer-use-test-model",
            routing_mode="code_only",
            reply_language="Greek",
        ),
        root=tmp_path,
        mode="review",
        yes=False,
        max_steps=2,
        no_log=True,
        api_key_override="test-key",
        sensitive_media=True,
        computer_use_host_request=lambda name, arguments: (
            host_calls.append((name, arguments)) or {}
        ),
    )
    client = _ScriptedComputerUseClient(
        [
            LLMResponse(content="The selected workflow is complete.", tool_calls=[], raw={}),
            LLMResponse(content="The selected workflow is complete.", tool_calls=[], raw={}),
        ]
    )
    session.client = client  # type: ignore[assignment]
    try:
        assert session.run_turn("Complete the selected workflow.") == 1
    finally:
        session.close()

    assert len(client.calls) == 2
    assert host_calls == []


def test_sensitive_missing_host_call_fails_closed_at_single_step_limit(
    tmp_path: Path,
) -> None:
    host_calls: list[tuple[str, dict[str, Any]]] = []
    session = create_session(
        cfg=AppConfig(model="computer-use-test-model", routing_mode="code_only"),
        root=tmp_path,
        mode="review",
        yes=False,
        max_steps=1,
        no_log=True,
        api_key_override="test-key",
        sensitive_media=True,
        computer_use_host_request=lambda name, arguments: (
            host_calls.append((name, arguments)) or {}
        ),
    )
    client = _ScriptedComputerUseClient(
        [LLMResponse(content="The selected workflow is complete.", tool_calls=[], raw={})]
    )
    session.client = client  # type: ignore[assignment]
    try:
        assert session.run_turn("Complete the selected workflow.") == 1
    finally:
        session.close()

    assert len(client.calls) == 1
    assert host_calls == []


def test_one_shot_computer_use_uses_host_owned_final_without_repo_completion_gate(
    tmp_path: Path,
) -> None:
    host_calls: list[tuple[str, dict[str, Any]]] = []

    def host_request(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        host_calls.append((name, arguments))
        assert name == "computer_state"
        return _lab_state("snapshot-1", "READY")

    surface = _RecordingSurface()
    session = create_session(
        cfg=AppConfig(model="computer-use-test-model", routing_mode="code_only"),
        root=tmp_path,
        mode="review",
        yes=False,
        max_steps=4,
        no_log=True,
        api_key_override="test-key",
        one_shot_execution=True,
        surface=surface,
        sensitive_media=True,
        computer_use_host_request=host_request,
    )
    client = _ScriptedComputerUseClient(
        [
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="state-1", name="computer_state", arguments={})],
                raw={},
            ),
            LLMResponse(
                content=json.dumps({"outcome": "completed_unverified", "summary": "STATE_OK"}),
                tool_calls=[],
                raw={},
            ),
            LLMResponse(
                content=("Blocker: no relevant editable path was found. No changes made: other."),
                tool_calls=[],
                raw={},
            ),
        ]
    )
    session.client = client  # type: ignore[assignment]
    try:
        assert session.run_turn("Inspect the selected app and report its state.") == 0
    finally:
        session.close()

    assert [name for name, _arguments in host_calls] == ["computer_state"]
    assert len(client.calls) == 2
    assert surface.final_messages == [
        "Computer Use run ended. The reported result has not been independently verified."
    ]
    assert not any(tmp_path.iterdir())


def test_authenticated_host_text_is_transient_while_cli_history_stays_status_only(
    tmp_path: Path,
) -> None:
    visible_description = (
        "The enabled Chrome tab shows Google results for τζίμης. "
        "The main local business result is Σουβλάκια ο Τζίμης."
    )

    def host_request(name: str, _arguments: dict[str, Any]) -> dict[str, Any]:
        assert name == "computer_state"
        state = _lab_state("snapshot-chrome", "READY")
        state["elements"] = [
            {
                "id": "query",
                "role": "AXTextField",
                "value": "τζίμης",
            },
            {
                "id": "business",
                "role": "AXLink",
                "title": "Σουβλάκια ο Τζίμης",
            },
        ]
        return state

    surface = _RecordingSurface()
    session = create_session(
        cfg=AppConfig(model="computer-use-test-model", routing_mode="code_only"),
        root=tmp_path,
        mode="review",
        yes=False,
        max_steps=4,
        no_log=True,
        api_key_override="test-key",
        one_shot_execution=True,
        surface=surface,
        sensitive_media=True,
        computer_use_host_request=host_request,
        computer_use_response_mode="host_sanitized_text",
    )
    session.client = _ScriptedComputerUseClient(  # type: ignore[assignment]
        [
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="state-chrome", name="computer_state", arguments={})],
                raw={},
            ),
            LLMResponse(
                content=json.dumps(
                    {"outcome": "completed_unverified", "summary": visible_description}
                ),
                tool_calls=[],
                raw={},
            ),
        ]
    )
    try:
        assert session.run_turn("Describe the enabled Chrome tab.") == 0
        durable_messages = json.dumps(session.messages, ensure_ascii=False)
    finally:
        session.close()

    assert surface.final_messages == [visible_description]
    assert visible_description not in durable_messages
    assert (
        "Computer Use run ended. The reported result has not been independently verified."
        in durable_messages
    )
    assert not any(tmp_path.iterdir())


def test_computer_use_snapshot_binding_is_revoked_between_turns(tmp_path: Path) -> None:
    host_calls: list[tuple[str, dict[str, Any]]] = []

    def host_request(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        host_calls.append((name, arguments))
        if name == "computer_state":
            return _lab_state("snapshot-turn-one", "READY")
        raise AssertionError("A second-turn action must not reach the host without observation")

    surface = _RecordingSurface()
    session = create_session(
        cfg=AppConfig(model="computer-use-test-model", routing_mode="code_only"),
        root=tmp_path,
        mode="review",
        yes=False,
        max_steps=4,
        no_log=True,
        api_key_override="test-key",
        sensitive_media=True,
        computer_use_host_request=host_request,
        surface=surface,
    )
    client = _ScriptedComputerUseClient(
        [
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="state-1", name="computer_state", arguments={})],
                raw={},
            ),
            LLMResponse(
                content=json.dumps(
                    {"outcome": "completed_unverified", "summary": "Observed the approved app."}
                ),
                tool_calls=[],
                raw={},
            ),
        ]
    )
    session.client = client  # type: ignore[assignment]
    try:
        assert session.run_turn("Observe the selected app.") == 0

        # Normal turn cleanup must revoke the first turn's trusted capability even
        # before the next run_turn entry performs its defensive reset.
        refused_after_cleanup = session.tools["computer_action"].run(
            {"action": "press", "element_id": "element_20_0def"}
        )
        assert refused_after_cleanup["error"] == "computer_use_fresh_observation_required"

        client.responses = [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="press-without-state",
                        name="computer_action",
                        arguments={"action": "press", "element_id": "element_20_0def"},
                    )
                ],
                raw={},
            ),
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="second-turn-state", name="computer_state", arguments={})],
                raw={},
            ),
            LLMResponse(
                content=json.dumps(
                    {
                        "outcome": "completed_unverified",
                        "summary": "The selected control was freshly observed.",
                    }
                ),
                tool_calls=[],
                raw={},
            ),
        ]
        assert session.run_turn("Press the selected control.") == 0
    finally:
        session.close()

    # Turn cleanup revoked the old snapshot: the premature second-turn action stayed local,
    # and only the subsequent corrected observation reached the host.
    assert [name for name, _arguments in host_calls] == ["computer_state", "computer_state"]
    assert len(client.calls) == 5
    assert surface.final_messages[-1] == (
        "Computer Use run ended. The reported result has not been independently verified."
    )
    assert not any(tmp_path.iterdir())


def test_sensitive_computer_use_host_failure_cannot_be_reported_as_success(
    tmp_path: Path,
) -> None:
    host_calls: list[tuple[str, dict[str, Any]]] = []

    def host_request(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        host_calls.append((name, arguments))
        if name == "computer_state":
            return _lab_state("snapshot-1", "READY")
        return {
            "error": "computer_use_live_evidence_failed",
            "message": "The trusted Computer Use host rejected the request.",
        }

    surface = _RecordingSurface()
    session = create_session(
        cfg=AppConfig(model="computer-use-test-model", routing_mode="code_only"),
        root=tmp_path,
        mode="review",
        yes=False,
        max_steps=6,
        no_log=True,
        api_key_override="test-key",
        sensitive_media=True,
        computer_use_host_request=host_request,
        surface=surface,
    )
    client = _ScriptedComputerUseClient(
        [
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="state-1", name="computer_state", arguments={})],
                raw={},
            ),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="set-1",
                        name="computer_action",
                        arguments={
                            "action": "set_value",
                            "element_id": "element_10_0abc",
                            "text": "Alysis dogfood 2026",
                        },
                    )
                ],
                raw={},
            ),
            LLMResponse(content="Completed the requested verification.", tool_calls=[], raw={}),
        ]
    )
    session.client = client  # type: ignore[assignment]
    try:
        assert session.run_turn("Complete the deterministic Computer Use Lab task.") == 1
    finally:
        session.close()

    assert [name for name, _arguments in host_calls] == [
        "computer_state",
        "computer_action",
    ]
    assert len(client.calls) == 2
    assert "No successful completion was verified" in surface.final_messages[-1]
    assert [item[0] for item in surface.closed_errors] == ["computer_use_host_failure"]
    assert all(
        "Completed the requested verification" not in text for text in surface.final_messages
    )
    assert not any(tmp_path.iterdir())


def test_sensitive_session_refuses_a_batched_snapshot_dependency_before_host_dispatch(
    tmp_path: Path,
) -> None:
    host_calls: list[tuple[str, dict[str, Any]]] = []
    snapshots = iter(["snapshot-1", "snapshot-2"])

    def host_request(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        host_calls.append((name, arguments))
        if name == "computer_state":
            return _lab_state(next(snapshots), "READY")
        if name == "computer_action":
            return {
                "receipt": {"action": arguments.get("action"), "semantic": True},
                "state": _lab_state(next(snapshots), "READY"),
            }
        raise AssertionError(f"unexpected Computer Use tool: {name}")

    session = create_session(
        cfg=AppConfig(model="computer-use-test-model", routing_mode="code_only"),
        root=tmp_path,
        mode="review",
        yes=False,
        max_steps=6,
        no_log=False,
        api_key_override="test-key",
        sensitive_media=True,
        computer_use_host_request=host_request,
    )
    client = _ScriptedComputerUseClient(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(id="batched-state", name="computer_state", arguments={}),
                    ToolCall(
                        id="batched-action",
                        name="computer_action",
                        arguments={
                            "snapshot_id": "snapshot-not-yet-observed",
                            "action": "set_value",
                            "element_id": "element_10_0abc",
                            "text": "Alysis dogfood 2026",
                        },
                    ),
                ],
                raw={},
            ),
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="state-1", name="computer_state", arguments={})],
                raw={},
            ),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="set-1",
                        name="computer_action",
                        arguments={
                            "snapshot_id": "snapshot-1",
                            "action": "set_value",
                            "element_id": "element_10_0abc",
                            "text": "Alysis dogfood 2026",
                        },
                    )
                ],
                raw={},
            ),
            LLMResponse(content="Done.", tool_calls=[], raw={}),
        ]
    )
    session.client = client  # type: ignore[assignment]
    try:
        assert session.run_turn("Complete the deterministic Computer Use Lab task.") == 0
    finally:
        session.close()

    assert [name for name, _arguments in host_calls] == [
        "computer_state",
        "computer_action",
    ]
    assert len(client.calls) == 4
    retry_messages = client.calls[1]["messages"]
    assert any(
        message.get("role") == "system"
        and "exactly one Computer Use tool call" in str(message.get("content") or "")
        for message in retry_messages
    )
    assert not any(tmp_path.iterdir())


def test_sensitive_session_rejects_a_known_non_tool_model_before_any_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = SimpleNamespace(supports_tool_calling=False)
    monkeypatch.setattr(
        agent_session_module,
        "_make_session_llm_client",
        lambda **_kwargs: client,
    )

    with pytest.raises(
        RequiredToolCallingUnsupportedError,
        match="does not support the required Computer Use tools",
    ):
        create_session(
            cfg=AppConfig(model="known-text-only-model", routing_mode="code_only"),
            root=tmp_path,
            mode="review",
            yes=False,
            max_steps=3,
            no_log=True,
            api_key_override="test-key",
            sensitive_media=True,
            computer_use_host_request=lambda _name, _arguments: {},
        )


def test_sensitive_computer_use_session_marks_client_serial_stateless_and_reasoning_omitted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _ScriptedComputerUseClient([])
    monkeypatch.setattr(
        agent_session_module,
        "_make_session_llm_client",
        lambda **_kwargs: client,
    )

    session = create_session(
        cfg=AppConfig(model="computer-use-test-model", routing_mode="code_only"),
        root=tmp_path,
        mode="review",
        yes=False,
        max_steps=3,
        no_log=True,
        api_key_override="test-key",
        sensitive_media=True,
        computer_use_host_request=lambda _name, _arguments: {},
    )
    try:
        assert omitted_reasoning_display_required(client) is True
        assert serial_tool_calls_required(client) is True
        assert stateless_responses_required(client) is True
    finally:
        session.close()


def test_bridge_surfaces_a_precise_required_tool_capability_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # session.create loads user configuration; never read the developer's own.
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    out = io.StringIO()
    bridge = StdioBridge(stdout=out)

    def reject_session(**_kwargs: Any) -> Any:
        raise RequiredToolCallingUnsupportedError(
            "The selected model does not support the required Computer Use tools."
        )

    bridge._create_session = reject_session  # type: ignore[method-assign]  # noqa: SLF001
    bridge.process_line(
        _request(
            "session.create",
            {
                "workspace": str(tmp_path),
                "mode": "review",
                "model": "known-text-only-model",
            },
            "unsupported-model",
        )
        + "\n"
    )

    response = next(line for line in _lines(out) if line.get("id") == "unsupported-model")
    assert response["ok"] is False
    assert response["error"]["code"] == "computer_use_tool_calling_unsupported"
    assert "required Computer Use tools" in response["error"]["message"]


def test_host_request_is_transient_bound_and_one_shot(tmp_path: Path) -> None:
    bridge, out, session, _job = _bridge_session(tmp_path)
    result: dict[str, Any] = {}
    private_text = "sk-abcdefghijklmnop"
    expected_arguments = {
        "snapshot_id": "snapshot-1",
        "action": "type",
        "text": private_text,
    }

    worker = threading.Thread(
        target=_invoke_host_request,
        args=(
            bridge,
            session,
            "computer_action",
            expected_arguments,
            result,
        ),
    )
    worker.start()
    event = _wait_for_event(out, "computer_use_request")
    payload = event["payload"]
    assert payload["arguments"] == expected_arguments
    expected_fingerprint = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                payload["arguments"],
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
    )
    assert payload["arguments_fingerprint"] == expected_fingerprint
    assert event not in bridge._event_buffers.get(session.session_id, ())  # noqa: SLF001
    replay_buffers = {  # noqa: SLF001
        session_id: list(events) for session_id, events in bridge._event_buffers.items()
    }
    assert private_text not in json.dumps(replay_buffers, ensure_ascii=False)

    response_params = {
        "session_id": session.session_id,
        "request_id": payload["request_id"],
        "response_nonce": payload["response_nonce"],
        "operation_id": payload["operation_id"],
        "job_id": event["job_id"],
        "tool_call_id": payload["tool_call_id"],
        "tool_name": payload["tool_name"],
        "arguments_fingerprint": payload["arguments_fingerprint"],
        "expires_at": payload["expires_at"],
        "ok": True,
        "result": {
            "snapshot_id": "snapshot-2",
            "elements": [{"id": "element-1", "label": "Result"}],
        },
    }
    bridge.process_line(_request("computer.respond", response_params, "respond-1") + "\n")
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert result["snapshot_id"] == "snapshot-2"

    bridge.process_line(_request("computer.respond", response_params, "respond-duplicate") + "\n")
    duplicate = next(line for line in _lines(out) if line.get("id") == "respond-duplicate")
    assert duplicate["ok"] is False
    assert duplicate["error"]["code"] == "duplicate_response"
    ordinary_output = [line for line in _lines(out) if line.get("type") != "computer_use_request"]
    assert private_text not in json.dumps(ordinary_output, ensure_ascii=False)


def test_failed_host_response_wakes_waiter_with_exact_controller_error(tmp_path: Path) -> None:
    bridge, out, session, _job = _bridge_session(tmp_path)
    result: dict[str, Any] = {}
    message = "The native key target is not keyboard-routable."

    worker = threading.Thread(
        target=_invoke_host_request,
        args=(
            bridge,
            session,
            "computer_action",
            {
                "snapshot_id": "snapshot-1",
                "action": "key",
                "element_id": "calculator-button",
                "keys": ["3"],
            },
            result,
        ),
    )
    worker.start()
    event = _wait_for_event(out, "computer_use_request")
    payload = event["payload"]

    bridge.process_line(
        _request(
            "computer.respond",
            {
                "session_id": session.session_id,
                "request_id": payload["request_id"],
                "response_nonce": payload["response_nonce"],
                "operation_id": payload["operation_id"],
                "job_id": event["job_id"],
                "tool_call_id": payload["tool_call_id"],
                "tool_name": payload["tool_name"],
                "arguments_fingerprint": payload["arguments_fingerprint"],
                "expires_at": payload["expires_at"],
                "ok": False,
                "error": {
                    "code": "computer_use_key_target_not_routable",
                    "message": message,
                },
            },
            "respond-failed-key-target",
        )
        + "\n"
    )
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert result == {
        "error": "computer_use_key_target_not_routable",
        "message": message,
    }
    acknowledgement = next(
        line for line in _lines(out) if line.get("id") == "respond-failed-key-target"
    )
    assert acknowledgement["ok"] is True
    assert acknowledgement["result"]["status"] == "resolved"
    assert acknowledgement["result"]["ok"] is False


def test_cancelled_job_cannot_emit_a_new_computer_use_request(tmp_path: Path) -> None:
    bridge, out, session, job = _bridge_session(tmp_path)
    job.cancellation_event.set()

    with pytest.raises(BridgeCancellationError, match="cancelled_by_user"):
        _invoke_host_request(
            bridge,
            session,
            "computer_state",
            {},
            {},
        )

    assert session.pending_computer_use == {}
    assert not any(line.get("type") == "computer_use_request" for line in _lines(out))


def test_host_response_rejects_binding_mismatch_and_cancel_unblocks_waiter(
    tmp_path: Path,
) -> None:
    bridge, out, session, job = _bridge_session(tmp_path)
    result: dict[str, Any] = {}
    failures: list[BaseException] = []

    def invoke() -> None:
        try:
            _invoke_host_request(bridge, session, "computer_state", {}, result)
        except BaseException as exc:  # cancellation is the expected worker outcome
            failures.append(exc)

    worker = threading.Thread(
        target=invoke,
    )
    worker.start()
    event = _wait_for_event(out, "computer_use_request")
    payload = event["payload"]
    bridge.process_line(
        _request(
            "computer.respond",
            {
                "session_id": session.session_id,
                "request_id": payload["request_id"],
                "response_nonce": "forged",
                "operation_id": payload["operation_id"],
                "job_id": event["job_id"],
                "tool_call_id": payload["tool_call_id"],
                "tool_name": payload["tool_name"],
                "arguments_fingerprint": payload["arguments_fingerprint"],
                "expires_at": payload["expires_at"],
                "ok": True,
                "result": {},
            },
            "forged-response",
        )
        + "\n"
    )
    mismatch = next(line for line in _lines(out) if line.get("id") == "forged-response")
    assert mismatch["error"]["code"] == "computer_use_binding_mismatch"

    bridge.process_line(
        _request(
            "session.cancel",
            {"session_id": session.session_id, "reason": "user_stop"},
            "cancel-host-waiter",
        )
        + "\n"
    )
    cancelled = next(line for line in _lines(out) if line.get("id") == "cancel-host-waiter")
    assert cancelled["result"]["status"] == "cancellation_requested"
    assert job.cancellation_event.is_set()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert result == {}
    assert len(failures) == 1
    assert isinstance(failures[0], BridgeCancellationError)
    assert str(failures[0]) == "user_stop"


@pytest.mark.parametrize(
    "reported,expected",
    [
        (None, "incomplete"),
        ("blocked", "blocked"),
        ("incomplete", "incomplete"),
        ("completed_unverified", "completed_unverified"),
    ],
)
def test_goal_report_preserves_prose_and_never_promotes_state_read_to_verified_success(
    tmp_path: Path, reported: str | None, expected: str
) -> None:
    prose = (
        "I could not submit the request or read a reply."
        if reported != "completed_unverified"
        else "The requested state was observed."
    )
    final = prose if reported is None else json.dumps({"outcome": reported, "summary": prose})
    surface = _RecordingSurface()
    session = create_session(
        cfg=AppConfig(model="computer-use-test-model", routing_mode="code_only"),
        root=tmp_path,
        mode="review",
        yes=False,
        max_steps=3,
        no_log=True,
        api_key_override="test-key",
        sensitive_media=True,
        surface=surface,
        computer_use_response_mode="host_sanitized_text",
        computer_use_host_request=lambda name, arguments: _lab_state("snapshot-report", "READY"),
    )
    session.client = _ScriptedComputerUseClient(
        [
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="report-state", name="computer_state", arguments={})],
                raw={},
            ),
            LLMResponse(content=final, tool_calls=[], raw={}),
        ]
    )
    try:
        assert session.run_turn("Submit a request and report its reply.") == 0
        assert session.last_turn_outcome["outcome"] == expected
        assert surface.final_messages == [prose]
        assert prose not in json.dumps(session.messages)
    finally:
        session.close()


def test_model_requested_transition_uses_real_stdio_request_identity(
    tmp_path: Path, monkeypatch
) -> None:
    from test_computer_use_approval import challenge

    from alysis_code.surface.types import ApprovalDecision

    client = _ScriptedComputerUseClient(
        [
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="first-state", name="computer_state", arguments={})],
                raw={},
            ),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="model-transition",
                        name="computer_action",
                        arguments={
                            "action": "activate_application",
                            "application": "Google Chrome",
                        },
                    )
                ],
                raw={},
            ),
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="second-state", name="computer_state", arguments={})],
                raw={},
            ),
            LLMResponse(
                content=json.dumps(
                    {
                        "outcome": "completed_unverified",
                        "summary": "Both applications were observed.",
                    }
                ),
                tool_calls=[],
                raw={},
            ),
        ]
    )
    bridge, out, session = _sensitive_agent_bridge_session(tmp_path, client)
    session.agent_session.max_steps = 6
    monkeypatch.setattr(
        bridge, "_request_approval", lambda *args, **kwargs: ApprovalDecision(allow=True)
    )
    job = None
    try:
        bridge.process_line(
            _request(
                "chat.send",
                {
                    "session_id": session.session_id,
                    "message": "Use Calculator and then Google Chrome.",
                },
                "start-transition",
            )
            + "\n"
        )
        started = next(row for row in _lines(out) if row.get("id") == "start-transition")
        job = bridge._jobs[started["result"]["job_id"]]
        seen = set()
        calls = []
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            for event in _lines(out):
                if event.get("type") != "computer_use_request":
                    continue
                payload = event["payload"]
                if payload["request_id"] in seen:
                    continue
                seen.add(payload["request_id"])
                calls.append(payload)
                index = len(calls)
                complete = index > 1
                workflow = {
                    "version": 1,
                    "required": True,
                    "complete": complete,
                    "current_application": "Google Chrome" if complete else "Calculator",
                    "next_application": None if complete else "Google Chrome",
                    "remaining_applications": [] if complete else ["Google Chrome"],
                }
                state = {
                    **_lab_state(f"snapshot-transition-{index}", "READY"),
                    "alysis_workflow": workflow,
                }
                result = (
                    {
                        "receipt": {"action": "activate_application", "semantic": True},
                        "state": state,
                        "alysis_workflow": workflow,
                    }
                    if payload["arguments"].get("approval_phase") == "execute"
                    else state
                )
                if payload["arguments"].get("approval_phase") == "prepare":
                    result = challenge(required=False)
                binding = {
                    key: payload[key]
                    for key in [
                        "request_id",
                        "response_nonce",
                        "operation_id",
                        "tool_call_id",
                        "tool_name",
                        "arguments_fingerprint",
                        "expires_at",
                    ]
                }
                bridge.process_line(
                    _request(
                        "computer.respond",
                        {
                            **binding,
                            "session_id": session.session_id,
                            "job_id": event["job_id"],
                            "ok": True,
                            "result": result,
                        },
                        f"respond-transition-{index}",
                    )
                    + "\n"
                )
            if job.thread and not job.thread.is_alive():
                break
            time.sleep(0.01)
        assert job.thread is not None
        job.thread.join(timeout=1)
        assert not job.thread.is_alive()
        assert [p["tool_name"] for p in calls] == [
            "computer_state",
            "computer_action",
            "computer_action",
            "computer_state",
        ]
        assert calls[1]["tool_call_id"].startswith("alysis_cu_call_")
        assert calls[1]["tool_call_id"] == calls[2]["tool_call_id"]
        assert calls[1]["arguments"]["snapshot_id"] == "snapshot-transition-1"
        assert len({p["tool_call_id"] for p in calls}) == 3
        assert session.pending_computer_use == {}
        assert session.agent_session.last_turn_outcome["outcome"] == "completed_unverified"
        assert "missing_tool_call_id" not in json.dumps(_lines(out))
    finally:
        if job and job.thread:
            job.thread.join(timeout=1)
        bridge.close()


def test_general_evidence_captures_thread_context_and_never_enters_replay():
    replay, transient = [], []
    surface = ProtocolEventSurface(
        context=EventContext(session_id="evidence-session"),
        emit=replay.append,
        emit_transient=transient.append,
        computer_use_evidence_version=1,
    )
    surface.with_job("owned-job")
    emit = surface.bind_computer_use_evidence_emitter()
    surface.with_job("later-job")
    payload = {
        "version": 1,
        "kind": "terminal",
        "terminalOrigin": "model_report",
        "reportedOutcome": "blocked",
        "stateCalls": 2,
        "actionCalls": 0,
        "knownHostFailure": False,
    }
    worker = threading.Thread(target=lambda: emit(payload))
    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert not replay
    assert len(transient) == 1
    assert transient[0]["job_id"] == "owned-job" and transient[0]["run_id"] is None
    assert transient[0]["payload"] == payload
    emit({**payload, "private_text": "SECRET"})
    assert len(transient) == 1
    disabled = ProtocolEventSurface(
        context=EventContext(session_id="old-peer"),
        emit=replay.append,
        emit_transient=transient.append,
    )
    disabled.emit_computer_use_evidence(payload)
    assert len(transient) == 1


def test_general_evidence_emission_failure_does_not_change_execution():
    def fail(_value):
        raise RuntimeError("PRIVATE callback failure")

    surface = ProtocolEventSurface(
        context=EventContext(session_id="evidence-session"),
        emit=fail,
        emit_transient=fail,
        computer_use_evidence_version=1,
    )
    surface.emit_computer_use_evidence(
        {
            "version": 1,
            "kind": "timing",
            "spanOrdinal": 1,
            "timingScope": "engine",
            "generationRole": "protected",
            "span": "provider_call",
            "phase": "begin",
        }
    )


@pytest.mark.parametrize("version", [True, 0, 2, "1", None])
def test_evidence_negotiation_rejects_nonexact_version_before_session_creation(
    tmp_path, monkeypatch, version
):
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    out = io.StringIO()
    bridge = StdioBridge(stdout=out)
    bridge.process_line(
        _request(
            "session.create",
            {
                "workspace": str(tmp_path),
                "mode": "review",
                "computer_use_evidence_version": version,
            },
            "bad-evidence",
        )
        + "\n"
    )
    result = next(line for line in _lines(out) if line.get("id") == "bad-evidence")
    assert result["ok"] is False and result["error"]["code"] == "invalid_params"
    assert not bridge._sessions


@pytest.mark.parametrize("malformed_type", [[], {}])
def test_context_image_evidence_does_not_preempt_existing_provider_validation(
    tmp_path, monkeypatch, malformed_type
):
    import alysis_code.agent.turn.core as core
    from alysis_code.agent.errors import AgentRuntimeError

    evidence = []
    surface = ProtocolEventSurface(
        context=EventContext(session_id="malformed-evidence"),
        emit=lambda value: None,
        emit_transient=lambda envelope: evidence.append(envelope["payload"]),
        computer_use_evidence_version=1,
    )
    session = create_session(
        cfg=AppConfig(model="computer-use-test-model", routing_mode="code_only"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=1,
        no_log=True,
        api_key_override="fixture-key",
        surface=surface,
        sensitive_media=True,
        computer_use_host_request=lambda name, args: _lab_state("snapshot", "READY"),
        computer_observation_available=True,
    )
    session.messages.append(
        {"role": "user", "content": [{"type": malformed_type, "text": "fixture"}]}
    )
    called = []

    class ExistingProviderValidation(RuntimeError):
        pass

    def provider(**kwargs):
        called.append(
            any(
                isinstance(part, dict) and part.get("type") == malformed_type
                for message in kwargs.get("messages", [])
                if isinstance(message, dict)
                for part in (
                    message.get("content") if isinstance(message.get("content"), list) else []
                )
            )
        )
        raise ExistingProviderValidation("fixture validation boundary")

    monkeypatch.setattr(core, "_main_agent_chat", provider)
    try:
        with pytest.raises(AgentRuntimeError, match="protected provider or host request failed"):
            # Protected failures are deliberately projected by the session;
            # the existing provider boundary must run without leaking its text.
            session.run_turn("Observe the permitted fixture.")
    finally:
        session.close()
    assert called == [True]
    assert any(
        item.get("phase") == "context_projected" and item.get("projectedImageCount") == 0
        for item in evidence
    )


@pytest.mark.parametrize("order", ["first", "invented", "late", "mixed", "premature-final"])
def test_sensitive_artifact_declaration_is_rejected_before_handler_or_path_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    order: str,
) -> None:
    from alysis_code.agent import task_artifact_binding
    from alysis_code.llm.metadata import (
        GEMINI_GENERATE_CONTENT_PROVIDER_METADATA_KEY,
        PROVIDER_METADATA_KEY,
    )

    instruction = "Inspect Planning notes.txt in the approved application."
    private_row = "SYNTHETIC_DECLARATION_PRIVATE_UI_ROW"
    unread_content = "SYNTHETIC_DECLARATION_UNREAD_FILE_CONTENT"
    (tmp_path / "Planning notes.txt").write_text(unread_content, encoding="utf-8")
    host_calls: list[str] = []
    binding_calls: list[dict[str, Any]] = []
    immediate: list[list[dict[str, Any]]] = []
    bind = task_artifact_binding.bind_path_interpretation

    def record_binding(**kwargs: Any) -> Any:
        binding_calls.append(kwargs["arguments"])
        return bind(**kwargs)

    monkeypatch.setattr(task_artifact_binding, "bind_path_interpretation", record_binding)

    def host_request(name: str, _arguments: dict[str, Any]) -> dict[str, Any]:
        host_calls.append(name)
        assert name == "computer_state"
        return _lab_state("declaration-snapshot", private_row)

    declaration = ToolCall(
        id="provider-declaration-capability",
        name=ARTIFACT_BINDING_TOOL,
        arguments={
            "paths": [
                {
                    "name": "Invented.txt" if order == "invented" else "Planning notes.txt",
                    "directory": "",
                    "role": "existing_input",
                    "evidence_quote": instruction,
                }
            ]
        },
    )
    observation = ToolCall(id="observation", name="computer_state", arguments={})
    batches = [[declaration], [observation]]
    if order == "late":
        batches.reverse()
    elif order == "mixed":
        batches = [
            [
                declaration,
                ToolCall(
                    id="unobserved-action",
                    name="computer_action",
                    arguments={"action": "press", "element_id": "unobserved-element"},
                ),
            ],
            [observation],
        ]
    responses = [LLMResponse(content="", tool_calls=batch, raw={}) for batch in batches]
    signature = "SYNTHETIC_DECLARATION_PRIVATE_PROVIDER_SIGNATURE"
    response_id = "SYNTHETIC_DECLARATION_PRIVATE_PROVIDER_RESPONSE"
    native_part = {
        "thoughtSignature": signature,
        "functionCall": {
            "id": declaration.id,
            "name": ARTIFACT_BINDING_TOOL,
            "args": json.loads(json.dumps(declaration.arguments)),
        },
    }
    if order in {"first", "premature-final"}:
        responses[0] = LLMResponse(
            content="",
            tool_calls=[declaration],
            raw={},
            provider_metadata={
                GEMINI_GENERATE_CONTENT_PROVIDER_METADATA_KEY: {
                    "response_id": response_id,
                    "content": {"role": "model", "parts": [native_part]},
                }
            },
        )
    if order == "premature-final":
        responses.insert(1, LLMResponse(content="The task is complete.", tool_calls=[], raw={}))
    responses.append(
        LLMResponse(content="The file contents remain unverified.", tool_calls=[], raw={})
    )

    class InspectingClient(_ScriptedComputerUseClient):
        def chat(self, **kwargs: Any) -> LLMResponse:
            immediate.append(json.loads(json.dumps(kwargs["messages"])))
            return super().chat(**kwargs)

    session = create_session(
        cfg=AppConfig(model="computer-use-test-model", routing_mode="code_only"),
        root=tmp_path,
        mode="review",
        yes=False,
        max_steps=6,
        no_log=False,
        api_key_override="test-key",
        sensitive_media=True,
        computer_use_host_request=host_request,
    )
    client = InspectingClient(responses)
    session.client = client  # type: ignore[assignment]
    try:
        assert session.artifact_output_access is None
        assert session.run_turn(instruction) == 0
        contract = session._turn_execution_state.acceptance_contract  # noqa: SLF001
        assert binding_calls == []
        assert contract is None
        assert getattr(session, "_task_evidence_state", None) is None
        failures = [
            event
            for event in session.store.events_snapshot()
            if event.get("type") == "computer_use_provider_contract_failure"
        ]
        assert len(failures) == 1
        assert failures[0]["payload"]["reason"] == "unknown_computer_use_tool"
        if order == "premature-final":
            assert len(client.calls) == 4
            assert "your prior response made no trusted host call" in json.dumps(immediate[2])
        assert host_calls == ["computer_state"]
        assert session.store.enabled is False
        assert session.store.artifact_persistence_enabled is False
        retained_events = json.dumps(session.store.events_snapshot())
        durable = json.dumps(session.messages)
        for messages in immediate:
            assert any(
                message.get("role") == "user" and message.get("content") == instruction
                for message in messages
            ), "argument taints must not erase the original user instruction"
        if order != "mixed":
            retained_declarations = [
                call
                for message in session.messages
                for call in message.get("tool_calls", [])
                if (call.get("function") or {}).get("name") == ARTIFACT_BINDING_TOOL
            ]
            assert retained_declarations == []
        assert private_row not in durable
        assert "declaration-snapshot" not in durable
        for private_value in (
            private_row,
            "declaration-snapshot",
            unread_content,
            signature,
            response_id,
            declaration.id,
        ):
            assert private_value not in retained_events
        assert unread_content not in json.dumps(immediate)
        if order in {"first", "premature-final"}:
            for messages in immediate[1:]:
                replay = [
                    message[PROVIDER_METADATA_KEY][GEMINI_GENERATE_CONTENT_PROVIDER_METADATA_KEY]
                    for message in messages
                    if GEMINI_GENERATE_CONTENT_PROVIDER_METADATA_KEY
                    in (message.get(PROVIDER_METADATA_KEY) or {})
                ]
                assert not any(item["content"]["parts"] == [native_part] for item in replay)
                assert not any(
                    message.get("role") == "tool" and message.get("tool_call_id") == declaration.id
                    for message in messages
                )
            for capability in (signature, response_id, declaration.id):
                assert capability not in durable
                assert capability not in json.dumps(client.calls, default=str)
        for call in client.calls:
            assert tuple(tool["function"]["name"] for tool in call["tools"]) == (
                *COMPUTER_USE_TOOL_NAMES,
            )
            assert "call task_artifacts alone" not in json.dumps(call["messages"])
        assert tuple(session.tools) == COMPUTER_USE_TOOL_NAMES
    finally:
        session.close()

    assert not session.store.path.exists()
    assert not session.store.path.with_suffix(".outcome.json").exists()
    assert not session.store.session_artifact_root.exists()
    assert list(tmp_path.iterdir()) == [tmp_path / "Planning notes.txt"]
    assert (tmp_path / "Planning notes.txt").read_text(encoding="utf-8") == unread_content


@pytest.mark.parametrize("unoffered_tool", ["shell", "fs_read", "computer_upload"])
def test_sensitive_artifact_surface_still_rejects_unoffered_tools(
    tmp_path: Path,
    unoffered_tool: str,
) -> None:
    host_calls: list[str] = []

    def host_request(name: str, _arguments: dict[str, Any]) -> dict[str, Any]:
        host_calls.append(name)
        assert name == "computer_state"
        return _lab_state("restricted-snapshot", "READY")

    class InspectingClient(_ScriptedComputerUseClient):
        def chat(self, **kwargs: Any) -> LLMResponse:
            if len(self.calls) == 1:
                assert not host_calls, "the rejected response must not dispatch any call"
            return super().chat(**kwargs)

    client = InspectingClient(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="provider-unoffered-capability-389a1f",
                        name=unoffered_tool,
                        arguments={},
                    )
                ],
                raw={},
            ),
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="provider-observation-capability-389a1f",
                        name="computer_state",
                        arguments={},
                    )
                ],
                raw={},
            ),
            LLMResponse(content="Observed the approved application.", tool_calls=[], raw={}),
        ]
    )
    session = create_session(
        cfg=AppConfig(model="computer-use-test-model", routing_mode="code_only"),
        root=tmp_path,
        mode="review",
        yes=False,
        max_steps=5,
        no_log=True,
        api_key_override="test-key",
        sensitive_media=True,
        computer_use_host_request=host_request,
    )
    session.client = client  # type: ignore[assignment]
    try:
        assert session.run_turn("Inspect Planning notes.txt in the approved application.") == 0
        assert host_calls == ["computer_state"]
        assert len(client.calls) == 3
        for call in client.calls:
            names = tuple(tool["function"]["name"] for tool in call["tools"])
            assert names == COMPUTER_USE_TOOL_NAMES
            assert unoffered_tool not in names
        assert session.artifact_output_access is None
        assert session.store.enabled is False
        assert session.store.artifact_persistence_enabled is False
        retained_events = json.dumps(session.store.events_snapshot())
        for capability in (
            "restricted-snapshot",
            "provider-unoffered-capability-389a1f",
            "provider-observation-capability-389a1f",
        ):
            assert capability not in retained_events
    finally:
        session.close()

    assert not session.store.path.exists()
    assert not session.store.path.with_suffix(".outcome.json").exists()
    assert not session.store.session_artifact_root.exists()
    assert not any(tmp_path.iterdir())


@pytest.mark.parametrize("after_observation", [False, True])
def test_sensitive_rejected_artifact_declaration_never_retains_unknown_private_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    after_observation: bool,
) -> None:
    from alysis_code.agent import task_artifact_binding

    # Model text read only from pixels has no preceding AX-string taint. The
    # declaration must redact it before validation, even when it is refused.
    private_pixels_text = "SYNTHETIC_PIXELS_ONLY_PRIVATE_NAME_75C198.txt"
    instruction = "Inspect Planning notes.txt in the approved application."
    events: list[dict[str, Any]] = []
    immediate: list[list[dict[str, Any]]] = []
    binding_calls: list[dict[str, Any]] = []
    host_calls: list[str] = []
    bind = task_artifact_binding.bind_path_interpretation

    def record_binding(**kwargs: Any) -> Any:
        binding_calls.append(kwargs["arguments"])
        return bind(**kwargs)

    monkeypatch.setattr(task_artifact_binding, "bind_path_interpretation", record_binding)

    def host_request(name: str, _arguments: dict[str, Any]) -> dict[str, Any]:
        host_calls.append(name)
        assert name == "computer_state"
        return _lab_state("private-declaration-observation", "READY")

    declaration_arguments = {
        "paths": [
            {
                "name": private_pixels_text,
                "directory": "",
                "role": "existing_input",
                "evidence_quote": "Inspect " + private_pixels_text,
            }
        ]
    }
    declaration = LLMResponse(
        content="",
        tool_calls=[
            ToolCall(
                id="private-declaration-75c198",
                name=ARTIFACT_BINDING_TOOL,
                arguments=declaration_arguments,
            )
        ],
        raw={},
    )
    observation = LLMResponse(
        content="",
        tool_calls=[
            ToolCall(
                id="observe-75c198",
                name="computer_state",
                arguments={},
            )
        ],
        raw={},
    )
    responses = [observation, declaration] if after_observation else [declaration, observation]
    responses.append(LLMResponse(content="Observation completed.", tool_calls=[], raw={}))

    class InspectingClient(_ScriptedComputerUseClient):
        def chat(self, **kwargs: Any) -> LLMResponse:
            immediate.append(json.loads(json.dumps(kwargs["messages"])))
            return super().chat(**kwargs)

    client = InspectingClient(responses)
    surface = ProtocolEventSurface(
        context=EventContext(session_id="private-declaration"),
        emit=events.append,
        sequencer=EventSequencer(),
    )
    session = create_session(
        cfg=AppConfig(model="computer-use-test-model", routing_mode="code_only"),
        root=tmp_path,
        mode="review",
        yes=False,
        max_steps=5,
        no_log=False,
        api_key_override="test-key",
        surface=surface,
        sensitive_media=True,
        computer_use_host_request=host_request,
    )
    session.client = client  # type: ignore[assignment]
    try:
        assert session.run_turn(instruction) == 0
        assert binding_calls == []
        assert host_calls == ["computer_state"]
        immediate_next = immediate[2 if after_observation else 1]
        for messages in immediate:
            assert any(
                message.get("role") == "user" and message.get("content") == instruction
                for message in messages
            )
        assert private_pixels_text not in json.dumps(immediate_next)
        assert any(
            event.get("type") == "computer_use_provider_contract_failure"
            and event["payload"]["reason"] == "unknown_computer_use_tool"
            for event in session.store.events_snapshot()
        )
        assert ARTIFACT_BINDING_TOOL not in json.dumps(events)
        for retained in (session.messages, events, client.calls, session.store.events_snapshot()):
            for private_value in (
                private_pixels_text,
                "private-declaration-observation",
                "private-declaration-75c198",
                "observe-75c198",
            ):
                assert private_value not in json.dumps(retained, default=str)
        contract = session._turn_execution_state.acceptance_contract  # noqa: SLF001
        assert contract is None
        assert getattr(session, "_task_evidence_state", None) is None
        assert session.artifact_output_access is None
        assert session.store.enabled is False
        assert session.store.artifact_persistence_enabled is False
    finally:
        session.close()

    assert not session.store.path.exists()
    assert not session.store.path.with_suffix(".outcome.json").exists()
    assert not session.store.session_artifact_root.exists()
    assert not any(tmp_path.iterdir())
