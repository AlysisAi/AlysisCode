"""Skills while an owl works on its computer.

The actual Alysis Code loop and Bots queue with a scripted model and computer. The owl's session
offers the engine's skills, each only when the owl has every tool the skill names; skills in the
controller's own home folders are not the owl's.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from test_bots_browser_runtime import response
from test_bots_desktop import Executor

from alysis_code.bots.computer import ComputerViews
from alysis_code.bots.desktop import DesktopComputers
from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.proposals import ActionDecision, ActionProposal, ProposalStore
from alysis_code.bots.runtime import BotRuntime
from alysis_code.bots.store import Store
from alysis_code.skills import discover_skills

# The work the person approved, in more than one language.
REQUEST = "Ετοίμασε την αναφορά του Μαρτίου · 三月の報告書を作成してください"
REPORT = "Η αναφορά είναι έτοιμη · 報告書が完成しました"


@pytest.fixture(autouse=True)
def controller_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The controller's home holds a skill of its own; the owl must not be offered it."""
    home = tmp_path / "controller-home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(
        "alysis_code.skills.discovery.canonical_user_config_dir", lambda: home / "config"
    )
    for folder in (
        home / ".config" / "agents" / "skills" / "host-only",
        home / "config" / "skills" / "mine",
    ):
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text(
            f"---\nname: {folder.name}\ndescription: Controller host workflow\n---\n\nSteps.\n",
            encoding="utf-8",
        )
    return home


@pytest.fixture
def approved_work(tmp_path):
    """An owl whose person approved work on its computer, asked for in Greek and Japanese."""
    store = Store(tmp_path / "bots.db")
    bot = store.save_bot("owner", BotInput(name="Μίρα", purpose="Αναφορές · 報告"))
    chat = store.create_chat("owner", bot["id"], "Συνομιλία")
    store.enqueue("owner", chat["id"], TaskInput(message=REQUEST, request_id="ask-for-work"))
    task = store.claim("owner")
    proposals = ProposalStore(store, "owner")
    proposal = proposals.propose(
        task,
        ActionProposal(
            question="Να ξεκινήσω; · 始めますか？", instructions=REQUEST, offline_computer=True
        ),
    )
    store.finish("owner", task["id"], "completed", content="Περιμένω έγκριση · 承認待ち")
    proposals.decide(
        bot["id"],
        proposal["id"],
        ActionDecision(action="accept", expected_revision=1, offline_computer=True),
    )
    task = store.claim("owner")
    manager = DesktopComputers("owner", ComputerViews(), factory=Executor)
    yield store, task, manager
    manager.close()


class ScriptedModel:
    """Reads the named skills one call at a time, then reports. Records every request."""

    supports_tool_calling = True
    model = "owl-fixture-model"

    def __init__(self, reads):
        self.reads = list(reads)
        self.requests = []

    def chat(self, **kwargs):
        self.requests.append(kwargs)
        if len(self.requests) <= len(self.reads):
            return response(
                "", name="skill_read", arguments={"name": self.reads[len(self.requests) - 1]}
            )
        return response(REPORT)


def work(approved_work, model):
    store, task, manager = approved_work
    runtime = BotRuntime(store, "owner", lambda _task: model, desktop_computers=manager)
    runtime.reply_review = None
    runtime.limits = replace(runtime.limits, compaction_enabled=False)
    runtime.run(task, runtime.task_token(task["id"]))
    queued = runtime.background.claim()
    assert queued["id"] == task["id"]
    runtime.run(queued, runtime.task_token(task["id"]), background=True)
    finished = store.task("owner", task["id"])
    assert finished["status"] == "completed", finished
    return finished


def listed_skills(request) -> list[str]:
    catalog = next(
        (
            str(message["content"])
            for message in request["messages"]
            if str(message.get("content") or "").startswith("<skill_context>")
        ),
        "",
    )
    return [
        line.split(" | ", 1)[0].removeprefix("- ")
        for line in catalog.splitlines()
        if line.startswith("- ") and " | " in line
    ]


def tool_names(request) -> set[str]:
    return {schema["function"]["name"] for schema in request["tools"] or []}


def read_results(model) -> list[dict]:
    """What each skill_read returned, in order."""
    return [
        json.loads(
            next(
                message["content"]
                for message in reversed(request["messages"])
                if message.get("role") == "tool"
            )
        )
        for request in model.requests[1 : len(model.reads) + 1]
    ]


def test_owl_work_offers_bundled_skills_only_when_it_has_their_tools(approved_work, tmp_path):
    model = ScriptedModel(["research", "documents"])
    work(approved_work, model)
    first = model.requests[0]
    listed = listed_skills(first)
    offered_tools = tool_names(first)
    empty = tmp_path / "empty"
    empty.mkdir()
    bundled = {
        skill.name: skill
        for skill in discover_skills(focus_path=empty, workspace_root=empty).ordered
        if skill.source_scope == "bundled"
    }

    assert "skill_read" in offered_tools
    # Every listed skill can be followed with the tools this request offers.
    for name in listed:
        assert set(bundled[name].uses_tools) <= offered_tools, name
    # The rest of the pack is listed exactly when its declared tools are all here.
    assert set(listed) == {
        name for name, skill in bundled.items() if set(skill.uses_tools) <= offered_tools
    }
    assert "research" in listed and "code-review" in listed
    for name in ("documents", "spreadsheets", "presentations", "pdf", "office-files"):
        assert name not in listed
    # Skill folders in the controller's home are not the owl's.
    assert not {"host-only", "mine"} & set(listed)
    research, documents = read_results(model)
    assert research["name"] == "research" and "Use when:" in research["content"]
    assert documents["error"] == "Unknown skill: documents"
    assert sorted(documents["available_skills"]) == sorted(listed)


def test_owl_work_follows_what_each_skill_declares(approved_work, tmp_path, monkeypatch):
    pack = tmp_path / "pack"
    for name, uses_tools in (
        ("computer-steps", "[computer_run]"),  # the owl's computer tool
        ("document-steps", "[document_operation, artifact_check]"),  # the desktop app's
        ("plain-steps", None),
    ):
        (pack / name).mkdir(parents=True)
        declared = f"uses_tools: {uses_tools}\n" if uses_tools else ""
        (pack / name / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Βήματα · 手順\n{declared}---\n\nSteps for {name}.\n",
            encoding="utf-8",
        )
    monkeypatch.setattr("alysis_code.skills.discovery.bundled_skill_root", lambda: pack)
    model = ScriptedModel(["computer-steps", "document-steps"])
    work(approved_work, model)

    assert listed_skills(model.requests[0]) == ["computer-steps", "plain-steps"]
    assert "computer_run" in tool_names(model.requests[0])
    computer, document = read_results(model)
    assert computer["content"].endswith("Steps for computer-steps.\n")
    assert document["error"] == "Unknown skill: document-steps"


@pytest.mark.usefixtures("mac_computer_use_protocol")
def test_coding_work_offers_engine_and_workspace_skills_it_has_tools_for(tmp_path):
    from uuid import uuid4

    from alysis_code.bots.coding_worker import Broker, ScopedRunner, make_session
    from alysis_code.config import AppConfig

    workspace = (tmp_path / "έργο · 作業").resolve()
    (workspace / ".alysis_skills" / "release-steps").mkdir(parents=True)
    (workspace / ".alysis_skills" / "release-steps" / "SKILL.md").write_text(
        "---\nname: release-steps\ndescription: Τα βήματα έκδοσης · リリース手順\n---\n\nSteps.\n",
        encoding="utf-8",
    )
    assert "computer-use" in discover_skills(focus_path=workspace, workspace_root=workspace).skills
    state = tmp_path / "state"
    state.mkdir()
    # A loopback address nothing listens on: building the session makes no request.
    broker = Broker("http://127.0.0.1:1", "fixture", uuid4().hex)
    session = make_session(
        workspace,
        state,
        {"id": uuid4().hex, "session_id": uuid4().hex},
        broker,
        {
            "base_url": AppConfig.model_fields["base_url"].default,
            "model": "owl-fixture-model",
            "image": "fixture-image",
            "verify": [],
        },
        ScopedRunner(workspace=workspace),
    )
    try:
        offered = {skill.name: skill for skill in session.skills_ordered}
        tools = set(session.tools)
    finally:
        session.close()
        broker.client.close()

    assert "skill_read" in tools
    assert {"release-steps", "debug", "code-review", "documents"} <= set(offered)
    # Computer-use hands work to the person's own apps; this session has no such tools.
    assert "computer-use" not in offered
    assert all(set(skill.uses_tools) <= tools for skill in offered.values())
    assert not {"host-only", "mine"} & set(offered)
