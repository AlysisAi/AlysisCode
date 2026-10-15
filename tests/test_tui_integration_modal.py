from concurrent.futures import Future, ThreadPoolExecutor
from threading import Event

import pytest
from prompt_toolkit.application import Application
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.layout import Layout
from prompt_toolkit.output import DummyOutput
from test_tui_integration_inspector import FakeManager, server

from alysis_code.cli_impl.tui.integration_inspector import IntegrationController
from alysis_code.cli_impl.tui.integration_modal import IntegrationModal


class Executor:
    def __init__(self, immediate=True):
        self.jobs = []
        self.immediate = immediate

    def submit(self, fn, *args):
        future = Future()
        self.jobs.append((future, fn, args))
        if self.immediate:
            self.run(len(self.jobs) - 1)
        return future

    def run(self, index):
        future, fn, args = self.jobs[index]
        if not future.cancelled():
            future.set_result(fn(*args))


class Dispatcher:
    """Fake owning loop: no work executes before explicit idle reservation."""

    def __init__(self):
        self.state = "empty"
        self.reserved = False

    def submit(self, work, *, started, completed):
        self.work, self.started, self.completed = work, started, completed
        self.state = "queued"
        return self

    def cancel_pending(self):
        if self.state != "queued":
            return False
        self.state = "cancelled"
        return True

    def idle(self):
        assert self.state == "queued"
        self.state = "applying"
        self.reserved = True
        self.started()
        try:
            result = self.work()
        finally:
            self.reserved = False
        self.state = "done"
        self.completed(result)
        return result


def make_modal(tmp_path, **kwargs):
    manager = kwargs.pop("manager", FakeManager())
    dispatcher = kwargs.pop("dispatcher", Dispatcher())
    outcomes, closes = [], []
    controller = kwargs.pop(
        "controller", IntegrationController(repo_root=tmp_path, manager_provider=lambda: manager)
    )
    modal = IntegrationModal(
        kind="mcp",
        controller=controller,
        dispatcher=dispatcher,
        executor=kwargs.pop("executor", Executor()),
        schedule_ui=kwargs.pop("schedule_ui", lambda fn: fn()),
        context_identity=kwargs.pop("context_identity", lambda: ("session", tmp_path)),
        on_close=lambda: closes.append("restore draft and focus"),
        on_outcome=outcomes.append,
        invalidate=lambda: None,
        **kwargs,
    )
    return modal, manager, dispatcher, outcomes, closes


def test_inspection_and_queued_cancel_never_connect(tmp_path):
    modal, manager, dispatcher, outcomes, closes = make_modal(tmp_path)
    modal.details_open = True
    modal._show_details()
    assert "search" in modal.details.text
    modal.request_action("disable")
    assert "Queued" in modal.status
    assert not manager.calls
    modal.close()
    modal.close()
    assert dispatcher.state == "cancelled"
    assert outcomes[0].status == "cancelled"
    assert closes == ["restore draft and focus"]
    assert not manager.calls


def test_extension_summary_keeps_warnings_and_full_details(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from alysis_code.cli_impl.tui import integration_inspector as inspector

    record = SimpleNamespace(enabled=False, component_ids={"skills": ["review", "publish"]})
    monkeypatch.setattr(inspector, "load_global_state", lambda: SimpleNamespace(installed={}))
    monkeypatch.setattr(
        inspector, "load_project_state", lambda _: SimpleNamespace(installed={"demo": record})
    )
    monkeypatch.setattr(
        inspector,
        "load_project_overrides",
        lambda _: SimpleNamespace(enabled=("demo",), disabled=()),
    )
    monkeypatch.setattr(
        inspector,
        "resolve_active_plugins",
        lambda **_: SimpleNamespace(
            enabled_plugin_ids=(),
            untrusted_project_plugin_ids=("demo",),
            workspace_trust_granted=False,
        ),
    )
    modal = make_modal(tmp_path)[0]
    modal.kind = "extensions"
    modal.snapshot = inspector.inspect_extensions(tmp_path)
    modal._show_details()
    assert "Installed record: disabled" in modal.details.text
    assert "Project override: enabled" in modal.details.text
    assert "Project overrides need workspace trust." in modal.details.text
    assert "Restart the session after changes." in modal.details.text
    assert "trust required" in modal._list_text()[0][1]
    assert "skills:" not in modal.details.text
    assert "shared by plugin ID" not in modal.details.text
    assert "0 tools" not in modal.details.text
    modal.details_open = True
    modal._show_details()
    assert "blocked by workspace trust" in modal.details.text
    assert "skills: review, publish" in modal.details.text
    assert "shared by plugin ID" in modal.details.text
    assert "ignored until workspace trust" in modal.details.text
    assert "restart after changes" in modal.details.text
    modal.close()


@pytest.mark.parametrize("change", ["session", "workspace", "manager"])
def test_queued_action_cannot_migrate(tmp_path, change):
    context = ["session", tmp_path]
    managers = [FakeManager()]
    original = managers[0]
    controller = IntegrationController(repo_root=tmp_path, manager_provider=lambda: managers[0])
    modal, _, dispatcher, outcomes, _ = make_modal(
        tmp_path, controller=controller, context_identity=lambda: tuple(context)
    )
    modal.request_action("disable")
    if change == "manager":
        managers[0] = FakeManager()
    elif change == "session":
        context[0] = "new session"
    else:
        context[1] = tmp_path / "other"
    assert dispatcher.idle().status == "cancelled"
    assert not original.calls
    assert outcomes[0].status == "cancelled"


def test_close_during_backend_keeps_reservation_and_reports_real_outcome(tmp_path):
    modal, manager, dispatcher, outcomes, closes = make_modal(tmp_path)
    entered, release = Event(), Event()
    original = manager.disable_server

    def blocked(**kwargs):
        assert dispatcher.reserved
        entered.set()
        assert release.wait(5)
        assert dispatcher.reserved
        return original(**kwargs)

    manager.disable_server = blocked
    modal.request_action("disable")
    with ThreadPoolExecutor(1) as executor:
        future = executor.submit(dispatcher.idle)
        assert entered.wait(5)
        assert "Applying" in modal.status
        modal.close()
        assert dispatcher.reserved
        assert not outcomes
        release.set()
        assert future.result(5).status == "applied"
    assert outcomes[0].status == "applied"
    assert manager.calls == [("disable", "demo")]
    assert closes


def test_refresh_is_async_and_stale_results_are_ignored(tmp_path):
    executor = Executor(False)
    scheduled = []
    modal, manager, _, _, _ = make_modal(tmp_path, executor=executor, schedule_ui=scheduled.append)
    assert not modal.snapshot.rows
    executor.run(0)
    manager.connected = False
    modal.refresh()
    executor.run(1)
    scheduled[1]()
    scheduled[0]()
    assert modal.snapshot.rows[0].state == "disconnected"
    modal.refresh()
    executor.run(2)
    modal.close()
    scheduled[2]()
    assert modal.snapshot.rows[0].state == "disconnected"


def test_reordered_started_callback_does_not_overwrite_completion(tmp_path):
    scheduled = []
    modal, manager, dispatcher, outcomes, _ = make_modal(tmp_path, schedule_ui=scheduled.append)
    scheduled.pop(0)()
    modal.request_action("disable")
    dispatcher.idle()
    started, completed = scheduled[:]
    scheduled.clear()
    completed()
    started()
    assert "Applied" in modal.status
    assert outcomes[0].status == "applied"
    assert manager.calls == [("disable", "demo")]


def test_backend_failure_redacted_and_unsupported_shortcut_inert(tmp_path):
    modal, manager, dispatcher, outcomes, _ = make_modal(tmp_path)
    modal.request_action("install")
    assert dispatcher.state == "empty"

    def failed(**kwargs):
        raise RuntimeError("https://user:password@host/?token=credential")

    manager.restart_server = failed
    modal.request_action("restart")
    dispatcher.idle()
    assert "Failed" in modal.status
    assert "credential" not in modal.status
    assert outcomes[0].status == "failed"


def test_real_keys_select_details_queue_and_close(tmp_path):
    import asyncio

    asyncio.run(_real_keys(tmp_path))


async def _real_keys(tmp_path):
    import asyncio

    manager = FakeManager()
    manager.resolved_servers.append(server("second"))
    modal, _, dispatcher, outcomes, closes = make_modal(tmp_path, manager=manager)
    with create_pipe_input() as pipe:
        app = Application(
            layout=Layout(modal.container, focused_element=modal.focus_target),
            input=pipe,
            output=DummyOutput(),
            full_screen=False,
        )
        app.ttimeoutlen = 0.01
        app.timeoutlen = 0.05
        task = asyncio.create_task(app.run_async())
        await asyncio.sleep(0.05)
        pipe.send_text("\x1b[B")
        await asyncio.sleep(0.05)
        assert modal.selected == 1
        pipe.send_text("\r")
        await asyncio.sleep(0.05)
        assert app.layout.has_focus(modal.details)
        pipe.send_text("d")
        await asyncio.sleep(0.05)
        assert dispatcher.state == "queued"
        pipe.send_text("\x1b")
        await asyncio.sleep(0.6)
        assert closes
        assert outcomes[0].status == "cancelled"
        app.exit()
        await task


def test_refresh_cannot_publish_into_changed_session(tmp_path):
    owner = ["first"]
    scheduled = []
    modal, _, _, _, _ = make_modal(
        tmp_path, schedule_ui=scheduled.append, context_identity=lambda: owner[0]
    )
    owner[0] = "second"
    scheduled.pop()()
    assert not modal.snapshot.rows


def test_restart_required_feedback_survives_refresh(tmp_path):
    from alysis_code.cli_impl.tui.integration_inspector import IntegrationRow, IntegrationSnapshot

    calls = []
    controller = IntegrationController(
        repo_root=tmp_path,
        manager_provider=lambda: None,
        extension_reader=lambda _: IntegrationSnapshot(
            "extensions",
            (
                IntegrationRow(
                    "demo", "demo", False, "disabled", actions=("enable",), scope="project"
                ),
            ),
        ),
        extension_enable=lambda **kwargs: calls.append(kwargs),
    )
    modal, _, dispatcher, outcomes, _ = make_modal(tmp_path, controller=controller)
    modal.kind = "extensions"
    modal.refresh()
    modal.request_action("enable")
    assert not calls
    dispatcher.idle()
    assert "Restart required" in modal.status
    assert outcomes[0].status == "restart_required"
    assert calls[0]["project"] is True


def test_missing_refresh_executor_is_actionable(tmp_path):
    class Unavailable:
        def submit(self, *args):
            raise RuntimeError("secret bearer-token")

    modal, _, _, _, _ = make_modal(tmp_path, executor=Unavailable())
    assert modal.status == "Refresh unavailable. Reopen the inspector to retry."
    assert not modal.snapshot.rows


def test_long_list_selection_has_visible_cursor_anchor(tmp_path):
    manager = FakeManager()
    manager.resolved_servers = [server(f"server-{i}") for i in range(25)]
    modal, _, _, _, _ = make_modal(tmp_path, manager=manager)
    modal.select(24)
    assert modal.list_control.create_content(80, 8).cursor_position.y == 24
