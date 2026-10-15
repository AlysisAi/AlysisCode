from __future__ import annotations

import asyncio
import importlib
import json
import subprocess
import threading
from types import SimpleNamespace

import pytest
from prompt_toolkit.application import Application as BaseApplication
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from alysis_code.cli_impl.tui import review_client
from alysis_code.cli_impl.tui.review import ReviewModal
from alysis_code.cli_impl.tui.state import TuiState
from alysis_code.config import AppConfig, ConfigError
from alysis_code.review_service import ReviewCancellationToken, ReviewController, make_request


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "profile"))
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    # No metadata discovery or paid network requests in regression tests.
    monkeypatch.setattr(review_client, "ModelRegistry", lambda **kw: object())
    monkeypatch.setattr(review_client, "evaluate_active_model_metadata_policy", lambda **kw: None)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(root), *args], check=True, capture_output=True
        ).stdout

    git("init", "-q")
    git("config", "user.name", "Review Test")
    git("config", "user.email", "review@example.invalid")
    (root / "app.py").write_text("value = 1\n")
    git("add", "app.py")
    git("commit", "-qm", "first")
    (root / "app.py").write_text("value = 2\n")
    git("commit", "-qam", "second")
    (root / "app.py").write_text("value = 3\n")
    return root


def response(findings=True):
    return SimpleNamespace(
        content=json.dumps(
            {
                "verdict": "comment",
                "overview": "Reviewed snapshot.",
                "findings": [
                    {
                        "severity": "high",
                        "title": "Wrong value",
                        "explanation": "Wrong return",
                        "path": "app.py",
                        "line_start": 1,
                        "line_end": 1,
                        "evidence": "value changed",
                        "suggested_fix": "Check callers",
                        "confidence": "high",
                    }
                ]
                if findings
                else [],
            }
        )
    )


async def eventually(predicate):
    for _ in range(2500):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("Review UI did not reach requested state")


def escape(app, pipe):
    pipe.send_text("\x1b")
    app.key_processor.feed_multiple(pipe.flush_keys())
    app.key_processor.process_keys()


def drive(monkeypatch, root, scenario, factory, *, running=False):
    module = importlib.import_module("alysis_code.cli_impl.tui.app")
    original, real_modal = BaseApplication, ReviewModal
    modals, errors, chat_calls = [], [], []
    release = threading.Event()

    def modal(*args, **kwargs):
        value = real_modal(*args, **kwargs)
        modals.append(value)
        return value

    class Session:
        # Accessing this shared client must never be necessary for review.
        @property
        def client(self):
            raise AssertionError("Shared chat client accessed")

        def run_turn(self, text, **kwargs):
            chat_calls.append(text)
            if running:
                release.wait(15)

    with create_pipe_input() as pipe:

        class Application(original):
            def run(self, *, pre_run):
                async def task():
                    try:
                        await scenario(self, pipe, modals, chat_calls)
                    except BaseException as exc:
                        exc.add_note(
                            f"status={[m.controller.status for m in modals]}; focus={self.layout.current_control!r}"
                        )
                        errors.append(exc)
                    finally:
                        release.set()
                        if not self.is_done:
                            self.exit()

                def begin():
                    pre_run()

                    def failed(loop, context):
                        errors.append(context.get("exception", AssertionError(str(context))))
                        release.set()
                        if not self.is_done:
                            self.exit()

                    asyncio.get_running_loop().set_exception_handler(failed)
                    self.create_background_task(task())

                return super().run(pre_run=begin, set_exception_handler=False)

        monkeypatch.setattr(module, "Application", Application)
        monkeypatch.setattr(module, "ReviewModal", modal)
        _, transcript = module.run_tui(
            TuiState(model_name="test"),
            input=pipe,
            output=DummyOutput(),
            owl_color=False,
            workspace_root=root,
            session_builder=lambda _: Session(),
            background_turns=running,
            review_engine_factory=factory,
        )
    if errors:
        raise errors[0]
    assert all(not m.is_open for m in modals)
    assert not any(text.startswith("/review") for text in chat_calls)
    return transcript


@pytest.mark.parametrize(
    "scope,index", [("working_tree", 0), ("branch", 1), ("commit", 2), ("range", 3)]
)
def test_scope_start_findings_snapshot_and_readonly(monkeypatch, repo, scope, index):
    clients, provider_calls = [], []

    class Client:
        closed = False

        def chat(self, **kwargs):
            provider_calls.append(kwargs)
            return response()

        def close(self):
            self.closed = True

    def make(**kwargs):
        client = Client()
        clients.append(client)
        return client

    monkeypatch.setattr(review_client, "make_llm_client", make)
    cfg = AppConfig(model="test-model")
    before = {p: p.read_bytes() for p in (repo / "app.py", repo / ".git/index")}

    def factory(workspace):
        return lambda token: review_client.create_review_engine(
            workspace, cfg, token, api_key_override="test-key"
        )

    async def scenario(app, pipe, modals, chat_calls):
        composer = app.current_buffer
        pipe.send_text("/review\r")
        await eventually(lambda: bool(modals))
        modal = modals[-1]
        assert clients == provider_calls == []
        pipe.send_text("\x1b[B" * index + " ")
        # The default scope already matches; allow its Space key to be consumed
        # before moving focus to an action (Space also activates buttons).
        await asyncio.sleep(0.05)
        await eventually(lambda: modal.scope.current_value == scope)
        modal.base.text = "HEAD~1"
        modal.head.text = "HEAD"
        modal.revision.text = "HEAD"
        app.layout.focus(modal.start_button)
        pipe.send_text("\r")
        await eventually(lambda: not modal.controller.running and bool(provider_calls))
        assert modal.controller.result is not None, modal.controller.status
        assert modal.controller.result.scope.value == scope
        assert clients[0].closed
        assert provider_calls[0]["cancellation_token"] is modal.controller._token
        assert {p: p.read_bytes() for p in before} == before
        saved_patch = modal.controller.result.diff.patch
        (repo / "app.py").write_text("changed after review\n")
        app.layout.focus(modal.findings)
        pipe.send_text("\r")
        await eventually(lambda: app.layout.has_focus(modal.details))
        assert "app.py:1-1 / high" in modal.details.text
        assert "Evidence: value changed" in modal.details.text
        assert "Reviewed snapshot location" in modal.details.text
        assert "changed after review" not in modal.details.text
        assert modal.controller.result.diff.patch == saved_patch
        escape(app, pipe)
        await eventually(lambda: not modal.is_open)
        assert app.current_buffer is composer and composer.text == "/review"
        assert chat_calls == []

    drive(monkeypatch, repo, scenario, factory)


def test_cancel_reopen_serializes_provider_and_disposes_after_call(monkeypatch, repo):
    entered, release = threading.Event(), threading.Event()
    clients, calls = [], []

    class Client:
        closed = False

        def chat(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                entered.set()
                assert release.wait(10)
            return response(False)

        def close(self):
            self.closed = True

    def make(**kwargs):
        client = Client()
        clients.append(client)
        return client

    monkeypatch.setattr(review_client, "make_llm_client", make)

    def factory(workspace):
        return lambda token: review_client.create_review_engine(
            workspace, AppConfig(model="test-model"), token, api_key_override="test"
        )

    async def scenario(app, pipe, modals, chat_calls):
        try:
            pipe.send_text("/review\r")
            await eventually(lambda: bool(modals))
            first = modals[-1]
            app.layout.focus(first.start_button)
            pipe.send_text("\r")
            await eventually(entered.is_set)
            escape(app, pipe)
            await eventually(lambda: not first.is_open)
            assert not clients[0].closed and calls[0]["cancellation_token"].is_cancelled
            pipe.send_text("\r")
            await eventually(lambda: len(modals) == 2)
            second = modals[-1]
            assert second.controller is first.controller
            app.layout.focus(second.start_button)
            pipe.send_text("\r")
            await eventually(lambda: second.controller.running)
            assert "Waiting for the previous" in second.controller.status
            assert len(clients) == len(calls) == 1
            release.set()
            await eventually(lambda: not second.controller.running)
            assert len(clients) == 2 and all(c.closed for c in clients)
            assert second.controller.result is not None
            assert "0 findings" in second.controller.status
        finally:
            release.set()

    drive(monkeypatch, repo, scenario, factory)


def test_provider_failure_and_constructor_cleanup(monkeypatch, repo):
    class Client:
        closed = False

        def chat(self, **kwargs):
            raise RuntimeError("secret-provider-header")

        def close(self):
            self.closed = True

    client = Client()
    monkeypatch.setattr(review_client, "make_llm_client", lambda **kwargs: client)

    def factory(workspace):
        return lambda token: review_client.create_review_engine(
            workspace, AppConfig(model="test-model"), token, api_key_override="test"
        )

    async def scenario(app, pipe, modals, chat_calls):
        pipe.send_text("/review\r")
        await eventually(lambda: bool(modals))
        modal = modals[-1]
        app.layout.focus(modal.start_button)
        pipe.send_text("\r")
        await eventually(lambda: "Review failed" in modal.controller.status)
        assert "secret-provider-header" not in modal.controller.status
        assert modal.controller.result is None and client.closed

    drive(monkeypatch, repo, scenario, factory)
    client.closed = False
    monkeypatch.setattr(
        review_client,
        "DedicatedReviewEngine",
        lambda *a: (_ for _ in ()).throw(ValueError("constructor")),
    )
    with pytest.raises(ValueError, match="constructor"):
        review_client.create_review_engine(
            repo, AppConfig(model="test-model"), ReviewCancellationToken(), api_key_override="test"
        )
    assert client.closed


def test_active_turn_and_unavailable_never_submit_review(monkeypatch, repo):
    async def scenario(app, pipe, modals, calls):
        pipe.send_text("work\r")
        await eventually(lambda: calls == ["work"])
        pipe.send_text("/review\r")
        await eventually(lambda: app.current_buffer.text == "/review")
        assert not modals

    transcript = drive(monkeypatch, repo, scenario, lambda root: lambda token: None, running=True)
    assert any("active turn" in text for _, text in transcript)

    async def unavailable(app, pipe, modals, calls):
        pipe.send_text("/review\r")
        await eventually(lambda: app.current_buffer.text == "/review")
        assert not modals and not calls

    transcript = drive(monkeypatch, repo, unavailable, None)
    assert any("unavailable" in text for _, text in transcript)


def test_missing_key_and_profile_resolution_before_client(monkeypatch, repo):
    made = []
    monkeypatch.setattr(review_client, "make_llm_client", lambda **kw: made.append(kw))

    def no_key(*args, **kwargs):
        raise ConfigError("missing test key")

    monkeypatch.setattr(review_client, "resolve_model_access_api_key", no_key)
    with pytest.raises(ConfigError):
        review_client.create_review_engine(
            repo, AppConfig(model="test-model"), ReviewCancellationToken()
        )
    assert made == []


def test_slash_close_preserves_draft_and_accepts_payloads_without_creating_client(
    monkeypatch, repo
):
    file = repo / "notes.txt"
    file.write_text("notes")
    payload = "[File #1]\n" + "\n".join(f"opaque {i}" for i in range(20))
    created = []

    def factory(workspace):
        def engine(token):
            created.append(token)
            raise AssertionError("Opening review must not create a provider")

        return engine

    async def scenario(app, pipe, modals, calls):
        composer = app.current_buffer
        composer.text = "/review"
        composer.cursor_position = 3
        saved = composer.document
        pipe.send_text("\r")
        await eventually(lambda: bool(modals))
        assert not created
        escape(app, pipe)
        await eventually(lambda: not modals[-1].is_open)
        assert composer.document == saved
        composer.text = ""
        pipe.send_text(f"\x1b[200~{file}\x1b[201~\x1b[200~{payload}\x1b[201~ draft")
        await eventually(lambda: composer.text.endswith(" draft"))
        pipe.send_text("\r")
        await eventually(lambda: bool(calls))
        assert calls == [f"{file.resolve()} {payload} draft"]

    drive(monkeypatch, repo, scenario, factory)


def test_start_captures_factory_and_disposal_before_worker_runs():
    async def scenario():
        called, disposed = [], []

        class Engine:
            def review(self, request, **kwargs):
                called.append("original")
                return SimpleNamespace(findings=(), summary=SimpleNamespace(overview="done"))

        controller = ReviewController(
            lambda token: Engine(),
            on_change=lambda: None,
            dispose_engine=lambda engine: disposed.append("original"),
        )
        task = controller.start(make_request("working_tree"))
        controller.engine_factory = lambda token: (_ for _ in ()).throw(
            AssertionError("redirected")
        )
        controller.dispose_engine = lambda engine: disposed.append("redirected")
        await task
        assert called == disposed == ["original"]
        assert controller.result is not None

    asyncio.run(scenario())


def test_subscription_profile_uses_auth_placeholder_and_configured_model(monkeypatch, repo):
    from alysis_code.profiles import ProfileSpec

    profile = ProfileSpec(name="subscription", auth_provider="openai-codex")
    monkeypatch.setattr("alysis_code.profiles.get_active_profile", lambda cfg: profile)
    monkeypatch.setattr(review_client, "get_active_profile", lambda cfg: profile)
    monkeypatch.setattr(
        review_client, "resolve_model_for_role", lambda **kwargs: "subscription-model"
    )
    captured = []

    class Client:
        def close(self):
            captured.append("closed")

    def make(**kwargs):
        captured.append(kwargs)
        return Client()

    monkeypatch.setattr(review_client, "make_llm_client", make)
    engine = review_client.create_review_engine(
        repo, AppConfig(model="subscription-model"), ReviewCancellationToken()
    )
    assert captured[0]["api_key"] == ""
    assert captured[0]["profile"] is profile
    assert captured[0]["model"] == "subscription-model"
    engine.close()
    assert captured[-1] == "closed"
