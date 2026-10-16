"""Central inspector/planning routing, actual keyboard and reserved backend tests."""

import asyncio
import importlib
from threading import Event
from types import SimpleNamespace

import pytest
from prompt_toolkit.application import Application as BaseApplication
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from test_tui_integration_inspector import FakeManager

from alysis_code.cli_impl.tui.integration_inspector import ActionOutcome
from alysis_code.cli_impl.tui.local_action_lifecycle import (
    IdleBoundaryError,
    TuiLocalActionLifecycle,
)
from alysis_code.cli_impl.tui.local_mode_host import ReservedIntegrationDispatcher
from alysis_code.cli_impl.tui.planning_backend import ChatPlanningBackend
from alysis_code.cli_impl.tui.state import TuiState
from alysis_code.config import AppConfig


def session_at(root):
    return SimpleNamespace(
        root=root,
        mode="auto",
        persona="code",
        cfg=AppConfig(model="test"),
        store=SimpleNamespace(
            enabled=False, session_id="s", workspace_root=root, append=lambda *_: None
        ),
        mcp_manager=FakeManager(),
        run_turn=lambda *_a, **_k: pytest.fail("No model turns"),
    )


@pytest.mark.parametrize("blocked", ["turn", "retiring", "queue", "recovery", "session", "closed"])
def test_integration_dispatcher_refuses_nonidle_or_stale_owner(tmp_path, blocked):
    session = session_at(tmp_path)
    current = [session]
    life = TuiLocalActionLifecycle()
    life.session_provider = lambda: current[0]
    life.idle = lambda: blocked not in {"retiring", "queue"}
    if blocked == "turn":
        life.begin_turn(session)
    if blocked == "session":
        current[0] = session_at(tmp_path)
    if blocked == "recovery":

        def gate():
            raise IdleBoundaryError("Pending recovery")

        life.ordinary_gate = gate
    if blocked == "closed":
        life.shutdown()
    dispatcher = ReservedIntegrationDispatcher(life, lambda: current[0], session)
    calls, outcomes = [], []
    try:
        try:
            pending = dispatcher.submit(
                lambda: calls.append(True), started=lambda: None, completed=outcomes.append
            )
        except RuntimeError:
            pass
        else:
            try:
                pending.future.result(2)
            except IdleBoundaryError:
                pass
        assert calls == []
    finally:
        life.finish_turn()
        life.shutdown()


def test_reservation_survives_view_close_and_blocks_new_turn(tmp_path):
    session = session_at(tmp_path)
    life = TuiLocalActionLifecycle()
    life.session_provider = lambda: session
    life.idle = lambda: True
    entered, release, done = Event(), Event(), Event()
    outcomes = []

    def work():
        entered.set()
        assert release.wait(3)
        return ActionOutcome("applied", "Completed")

    def completed(outcome):
        outcomes.append(outcome)
        done.set()

    try:
        pending = ReservedIntegrationDispatcher(life, lambda: session, session).submit(
            work, started=lambda: None, completed=completed
        )
        assert entered.wait(2)
        assert not pending.cancel_pending()
        assert life.mutating
        with pytest.raises(IdleBoundaryError):
            life.begin_turn(session)
        release.set()
        assert done.wait(2)
        assert outcomes[0].status == "applied"
        assert not life.mutating
    finally:
        release.set()
        life.shutdown()


@pytest.mark.parametrize(
    "mode,scope,allow,expected",
    [
        ("readonly", None, True, False),
        ("auto", ["*.md"], True, False),
        ("auto", [], True, False),
        ("review", None, False, False),
        ("review", None, True, True),
        ("fullaccess", None, False, False),
    ],
)
def test_forge_requires_explicit_approval_and_respects_scopes(
    tmp_path, mode, scope, allow, expected
):
    session = session_at(tmp_path)
    session.mode, session.allow_write_globs = mode, scope
    requests = []
    session.surface = SimpleNamespace(
        request_approval=lambda req: requests.append(req) or SimpleNamespace(allow=allow)
    )
    forge = SimpleNamespace(ui_mode="chat", plan={"goal": "retained"})
    backend = ChatPlanningBackend(
        lambda: session,
        forge,
        TuiState(),
        apply_persona=lambda **_: None,
        enter_forge=lambda **_: pytest.fail("Approval check must not enter"),
    )
    assert backend.forge_allowed() is expected
    assert bool(requests) is (mode != "readonly" and scope is None)
    backend.leave_forge()
    assert forge.plan == {"goal": "retained"}


async def eventually(predicate):
    for _ in range(300):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("UI state did not arrive")


def test_actual_chat_keys_inspect_change_plan_and_cancel_preserve_draft(monkeypatch, tmp_path):
    module = importlib.import_module("alysis_code.cli_impl.tui.app")
    session = session_at(tmp_path)
    forge = SimpleNamespace(ui_mode="chat", plan={"goal": "keep"})
    state = TuiState()
    life = TuiLocalActionLifecycle()
    calls, modals, errors = [], [], []
    approval_entered = Event()
    forge_entries, forge_pickers = [], []

    def persona(**kwargs):
        calls.append(kwargs)
        session.persona = kwargs["persona"]
        session.mode = "review" if session.persona == "architect" else "auto"

    def enter_forge(**kwargs):
        forge_entries.append(kwargs)
        forge.ui_mode = "forge"
        return True

    backend = ChatPlanningBackend(
        lambda: session, forge, state, apply_persona=persona, enter_forge=enter_forge
    )
    original = module.IntegrationModal

    def modal(**kwargs):
        instance = original(**kwargs)
        modals.append(instance)
        return instance

    monkeypatch.setattr(module, "IntegrationModal", modal)
    with create_pipe_input() as pipe:

        class Application(BaseApplication):
            def run(self, *, pre_run):
                self.ttimeoutlen = self.timeoutlen = 0.01

                async def scenario():
                    try:
                        composer = self.current_buffer
                        pipe.send_text("/mcp\r")
                        await eventually(lambda: modals and modals[-1].snapshot.rows)
                        assert session.mcp_manager.calls == []
                        pipe.send_text("d")
                        await eventually(lambda: session.mcp_manager.calls == [("disable", "demo")])
                        await eventually(lambda: not life.mutating)
                        pipe.send_text("\x1b")
                        await eventually(lambda: not modals[-1].is_open)
                        assert composer.text == "/mcp"
                        composer.text = "/plan"
                        pipe.send_text("\r")
                        await asyncio.sleep(0.08)
                        pipe.send_text("\x1b")
                        await asyncio.sleep(0.08)
                        assert not calls and composer.text == "/plan"
                        pipe.send_text("\r")
                        await asyncio.sleep(0.08)
                        pipe.send_text("\r")
                        await eventually(lambda: session.persona == "architect")
                        assert composer.text == "/plan"
                        assert calls[0]["permission_ceiling"] == "auto"
                        assert forge.plan == {"goal": "keep"}
                        await eventually(lambda: not life.mutating)
                        composer.text = "/planning"
                        pipe.send_text("\r")
                        await asyncio.sleep(0.08)
                        pipe.send_text("\x1b[B\x1b[B\r")
                        await eventually(lambda: session.persona == "code" and not life.mutating)
                        composer.text = "/plan"
                        pipe.send_text("\r")
                        await asyncio.sleep(0.08)
                        pipe.send_text("\x1b[B\r")
                        await eventually(approval_entered.is_set)
                        assert life.mutating and not forge_entries
                        pipe.send_text("n")
                        await eventually(lambda: not life.mutating)
                        assert forge.ui_mode == "chat" and not forge_entries
                        assert composer.text == "/plan"
                        approval_entered.clear()
                        pipe.send_text("\r")
                        await asyncio.sleep(0.08)
                        pipe.send_text("\x1b[B\r")
                        await eventually(approval_entered.is_set)
                        pipe.send_text("y")
                        await eventually(lambda: forge.ui_mode == "forge" and not life.mutating)
                        assert forge_entries[0]["planner_assistant_default"] is False
                        assert forge.plan == {"goal": "keep"}
                        pipe.send_text("\r")
                        await eventually(lambda: bool(forge_pickers))
                    except BaseException as exc:
                        errors.append(exc)
                    finally:
                        self.exit()

                def start():
                    pre_run()
                    self.create_background_task(scenario())

                return super().run(pre_run=start, set_exception_handler=False)

        monkeypatch.setattr(module, "Application", Application)

        def builder(surface):
            original_approval = surface.request_approval

            def request_approval(request):
                approval_entered.set()
                return original_approval(request)

            surface.request_approval = request_approval
            session.surface = surface
            return session

        module.run_tui(
            state,
            input=pipe,
            output=DummyOutput(),
            owl_color=False,
            workspace_root=tmp_path,
            session_builder=builder,
            local_lifecycle=life,
            planning_backend=backend,
            history_persistence=False,
            picker_providers={
                "/plan": lambda: (
                    forge_pickers.append(True)
                    or {"rows": [{"label": "Existing Forge view", "value": "view"}]}
                )
            },
        )
    if errors:
        raise errors[0]


def test_managerless_controller_uses_config_only_without_connecting(tmp_path, monkeypatch):
    from test_tui_integration_inspector import server

    from alysis_code.cli_impl.tui import local_mode_host

    reads = []
    monkeypatch.setattr(
        local_mode_host,
        "load_resolved_mcp_config",
        lambda **kwargs: reads.append(kwargs) or SimpleNamespace(servers=[server()]),
    )
    controller = local_mode_host.integration_controller(lambda: None, tmp_path)
    snapshot = controller.refresh("mcp")
    assert reads == [{"workspace_root": tmp_path}]
    assert snapshot.rows[0].state == "unavailable"
    assert snapshot.rows[0].actions == ()


def test_planning_backend_real_persona_helper_preserves_ask_ceiling(monkeypatch, tmp_path):
    from test_persona_modes import _FakeChatSession

    from alysis_code.cli_impl.chat import loop
    from alysis_code.cli_impl.tui.planning_chooser import PlanningController

    session = _FakeChatSession(mode="auto", persona="code")

    def apply_mode(*, session, next_mode, persist_default_mode):
        session.mode = next_mode

    monkeypatch.setattr(loop, "_apply_chat_effective_mode", apply_mode)
    loop._apply_chat_persona(session=session, persona="ask")
    forge = SimpleNamespace(ui_mode="chat", plan={"goal": "keep"})
    backend = ChatPlanningBackend(
        lambda: session,
        forge,
        TuiState(),
        apply_persona=loop._apply_chat_persona,
        enter_forge=lambda **_: pytest.fail("No Forge"),
    )
    outcomes = []
    controller = PlanningController(
        read_state=backend.state,
        dispatch=lambda fn: outcomes.append(fn()),
        apply_persona=backend.apply_persona,
        enter_forge=backend.enter_forge,
        leave_forge=backend.leave_forge,
        forge_allowed=backend.forge_allowed,
    )
    controller.choose("architect")
    assert session.persona == "architect" and session.mode == "readonly"
    controller.choose("return")
    assert session.persona == "ask" and session.mode == "readonly"
    assert all(o.status == "applied" for o in outcomes)
    assert forge.plan == {"goal": "keep"}


def test_planning_discovery_has_one_context_correct_plan_and_preserves_skills():
    from prompt_toolkit.document import Document

    from alysis_code.cli_impl.chat_slash_completer import (
        NAVIGATION_COMMAND_SPECS,
        ChatSlashCompleter,
    )

    for mode in ("chat", "forge"):
        completer = ChatSlashCompleter(
            mode_provider=lambda mode=mode: mode,
            extra_specs=NAVIGATION_COMMAND_SPECS,
            skill_names_provider=lambda: ["keep-skill"],
        )
        choices = list(completer.get_completions(Document("/"), None))
        assert [c.text for c in choices].count("/plan") == 1
        assert {"/mcp", "/extensions", "/planning", "/skill"} <= {c.text for c in choices}
        assert [c.text for c in completer.get_completions(Document("$keep"), None)] == [
            "$keep-skill"
        ]
        nested = list(completer.get_completions(Document("/plan "), None))
        assert bool(nested) is (mode == "forge")
