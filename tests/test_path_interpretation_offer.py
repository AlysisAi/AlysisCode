"""The file declaration is asked for only when the model has something to decide.

The tool itself is listed in every turn, so the tool list never changes with
the request; what comes and goes is the guidance that asks for it.

It costs a model round trip before any work. Whether to ask is decided on facts
the host can check and on how a mention is written, never on how the request
is worded: the model is asked when a mention the user wrote as a path (with a
folder, or in quotes) does not exist yet or lies outside the workspace. The
same request in another language gets the same answer, and no mention becomes a
requirement by the words around it.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from alysis_code.agent.acceptance_contract import (
    AcceptanceCriterionEnforcement,
    AcceptancePathKind,
    AcceptancePathRef,
    AcceptancePathRole,
    build_acceptance_contract,
)
from alysis_code.agent.task_artifact_binding import (
    TOOL_NAME,
    declared_changed_output_paths,
    earlier_path_refs,
    named_workspace_paths,
    request_needs_path_interpretation,
)
from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig
from alysis_code.llm.openai_compat import LLMResponse
from alysis_code.surface import NoopSurface

EXISTING = ("README.md", "calc.py", "pyproject.toml", "src/app.py", "docs/guide.md")


@pytest.fixture
def workspace(tmp_path) -> Path:
    root = tmp_path / "workspace"
    for name in EXISTING:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text("x\n", encoding="utf-8")
    return root


def needed(root: Path, instruction: str, *, earlier: tuple[str, ...] = (), **options) -> bool:
    refs = list(build_acceptance_contract(root=root, instruction=instruction).path_refs)
    return request_needs_path_interpretation(
        refs, root=root, earlier_refs=earlier_path_refs(root, earlier), **options
    )


# The same kinds of request in English, Greek, Chinese and informal English.
NOTHING_TO_DECIDE = [
    # Existing files.
    "Fix the bug in src/app.py and update docs/guide.md accordingly.",
    "Διόρθωσε το σφάλμα στο src/app.py και ενημέρωσε το docs/guide.md.",
    "修复 src/app.py 里的错误, 然后更新 docs/guide.md",
    "yo src/app.py is busted, sort it + docs/guide.md too pls",
    "Do not change README.md. Tidy calc.py.",
    "Μην αλλάξεις το README.md. Τακτοποίησε το calc.py.",
    # Numbers and versions are written the same everywhere and are not paths.
    "Bump the version in pyproject.toml from 1.2.2 to 1.2.3.",
    "Ανέβασε την έκδοση στο pyproject.toml από 1.2.2 σε 1.2.3.",
    # No file named.
    "Fix the failing tests.",
    "Διόρθωσε τα τεστ που αποτυγχάνουν.",
    # Bare words with a dot: product names, abbreviations, versions, settings,
    # hosts, and possibly a file. Hints, never a reason to stop and ask.
    "We are moving to Node.js 22, e.g. for the test runner (see nodejs.org). Update README.md.",
    "Set user.name to Bob, then update README.md.",
    "Point the client at example.invalid in src/app.py.",
    "Bump calc.py to v1.2.x.",
    "Write the final report to report.pdf.",
    "Γράψε την τελική αναφορά στο report.pdf.",
]

SOMETHING_TO_DECIDE = [
    # Written as a path, with a folder, and not there yet.
    "Read README.md and create notes/summary.md with three bullets.",
    "Διάβασε το README.md και φτιάξε το notes/summary.md με τρεις κουκκίδες.",
    "读 README.md, 然后写 notes/summary.md",
    # Written as a path, in quotes or backticks, and not there yet.
    'Save the chart as "sales chart.png".',
    "Αποθήκευσε το αποτέλεσμα στο `result.json`.",
    "Write the final report to 'report.pdf'.",
    # Outside the workspace.
    'Copy "/outside/data/input.csv" into calc.py.',
    "Αντίγραψε το /outside/data/input.csv στο calc.py.",
    "Update ../sibling/config.toml to match pyproject.toml.",
]


@pytest.mark.parametrize("instruction", NOTHING_TO_DECIDE)
def test_no_declaration_for_existing_workspace_files_or_none(workspace, instruction) -> None:
    assert needed(workspace, instruction) is False


@pytest.mark.parametrize("instruction", SOMETHING_TO_DECIDE)
def test_the_model_is_asked_about_a_new_or_outside_path(workspace, instruction) -> None:
    assert needed(workspace, instruction) is True


@pytest.mark.parametrize("instruction", [*NOTHING_TO_DECIDE, *SOMETHING_TO_DECIDE])
def test_no_role_is_read_from_the_wording_of_a_request(workspace, instruction) -> None:
    contract = build_acceptance_contract(root=workspace, instruction=instruction)
    assert {ref.role for ref in contract.path_refs} <= {AcceptancePathRole.UNKNOWN_REFERENCE}
    # A mention alone never becomes something the turn must do before it ends.
    assert not [
        item
        for item in contract.criteria
        if item.path_refs and item.enforcement == AcceptanceCriterionEnforcement.HARD
    ]


@pytest.mark.parametrize("verb", ["Run", "Τρέξε", "运行", "pls run"])
def test_a_path_inside_a_backticked_command_is_not_a_mention(workspace, verb) -> None:
    instruction = f"{verb} `/opt/tools/bin/check --config /opt/x/config.yaml` and fix calc.py."
    contract = build_acceptance_contract(root=workspace, instruction=instruction)
    assert [ref.display_path for ref in contract.path_refs] == ["calc.py"]
    assert needed(workspace, instruction) is False


def _ref(raw: str, kind=AcceptancePathKind.WORKSPACE_RELATIVE):
    inside = kind != AcceptancePathKind.ABSOLUTE_EXTERNAL
    return AcceptancePathRef(
        raw_text=raw,
        display_path=raw,
        path_kind=kind,
        role=AcceptancePathRole.UNKNOWN_REFERENCE,
        workspace_relative_path=raw if inside else "",
        absolute_path=raw if not inside else "",
    )


@pytest.mark.parametrize(
    "ref",
    [
        _ref("/outside/key", AcceptancePathKind.ABSOLUTE_EXTERNAL),
        _ref("../somewhere/else.txt", AcceptancePathKind.UNRESOLVED),
    ],
)
def test_a_path_the_host_cannot_place_brings_the_declaration(workspace, ref) -> None:
    assert request_needs_path_interpretation([ref], root=workspace)
    assert request_needs_path_interpretation([_ref("calc.py"), ref], root=workspace)


def test_the_model_is_asked_in_a_nested_project(workspace) -> None:
    # A relative name has two possible bases there.
    assert needed(workspace, "Fix the bug in src/app.py.", working_directory="src")
    assert request_needs_path_interpretation([], root=workspace, working_directory="src")


def test_a_request_naming_no_file_needs_no_declaration(workspace) -> None:
    assert not request_needs_path_interpretation([], root=workspace)


def test_an_outside_path_named_earlier_keeps_the_declaration(workspace) -> None:
    request = "Now tidy calc.py the same way."
    outside = ('Read "/outside/data/input.csv".',)
    assert not needed(workspace, request, earlier=("Fix src/app.py first.",))
    assert needed(workspace, request, earlier=outside)
    assert needed(workspace, "Carry on.", earlier=outside)
    # Each earlier message is read on its own, however many paths came before.
    crowded = (" ".join(f"Check notes/n{index}.md." for index in range(60)), *outside)
    assert needed(workspace, request, earlier=crowded)
    # An earlier path that no longer exists, or never did, is no reason to ask.
    assert not needed(workspace, request, earlier=("Create notes/summary.md.", "Use v1.2.x."))


# --- the turn -----------------------------------------------------------------


class _Client:
    model = "test-model"
    temperature = 0.2

    def __init__(self) -> None:
        self.tool_names: list[set[str]] = []

    def chat(self, **kwargs):
        self.tool_names.append(
            {tool.get("function", {}).get("name") for tool in kwargs.get("tools") or []}
        )
        return LLMResponse(content="Nothing was changed.", tool_calls=[], raw={})


def _run(tmp_path, instruction: str, *, output_directory: str | None = None):
    from test_agent_loop_one_shot_follow_through import (
        _commit_repo_file,
        _init_git_repo_with_commit,
    )

    root = tmp_path / "repo"
    _init_git_repo_with_commit(root)
    for name in EXISTING:
        _commit_repo_file(root, name, "x\n")
    session = create_session(
        cfg=AppConfig(
            model="test-model", stream=False, routing_mode="code_only", web_search_mode="off"
        ),
        root=root,
        mode="auto",
        yes=True,
        max_steps=2,
        no_log=False,
        api_key_override="fixture-key",
        surface=NoopSurface(),
        enable_compaction=False,
        session_log_dir_override=tmp_path / "sessions",
    )
    if output_directory is not None:
        (root / output_directory).mkdir(parents=True)
        session.host_task_output_directory = output_directory
    session.client = client = _Client()
    try:
        session.run_turn(instruction)
        details = [
            event["payload"].get("detail")
            for event in session.store.events_snapshot()
            if event["type"] == "controller_intervention"
        ]
    finally:
        session.close()
    return client.tool_names[0], details


@pytest.mark.parametrize(
    "instruction",
    [
        "Fix the bug in src/app.py and update docs/guide.md.",
        "Διόρθωσε το σφάλμα στο src/app.py και ενημέρωσε το docs/guide.md.",
        "Fix the failing tests.",
        "Upgrade to Node.js 22, e.g. for the test runner, and note it in README.md.",
    ],
)
def test_a_request_about_existing_files_goes_straight_to_work(tmp_path, instruction) -> None:
    tools, details = _run(tmp_path, instruction)
    # Listed like every other tool, so the tool list is the same in every turn;
    # what is left out is the guidance that asks for it.
    assert TOOL_NAME in tools
    assert "fs_read" in tools
    assert "task_artifact_interpretation_guidance" not in details
    assert "task_requirements_optional_guidance" not in details


@pytest.mark.parametrize(
    "instruction",
    [
        "Write the final report to reports/final.pdf.",
        "Γράψε την τελική αναφορά στο `report.pdf`.",
        'Copy "/outside/data/input.csv" into calc.py.',
    ],
)
def test_a_request_naming_a_new_or_outside_path_gets_the_declaration(tmp_path, instruction) -> None:
    tools, details = _run(tmp_path, instruction)
    assert TOOL_NAME in tools
    assert "task_artifact_interpretation_guidance" in details


@pytest.mark.parametrize(
    "instruction",
    [
        "Fix the bug in src/app.py.",
        "Γράψε μια σύντομη αναφορά.",
        "Create summary.md from README.md.",
    ],
)
def test_output_folder_is_told_to_the_model_without_the_declaration(tmp_path, instruction) -> None:
    tools, details = _run(tmp_path, instruction, output_directory="outputs/chat-1")
    assert TOOL_NAME in tools
    assert "task_artifact_interpretation_guidance" not in details
    assert "task_output_directory_guidance" in details


def test_the_tools_are_the_same_in_every_turn_of_a_chat(tmp_path) -> None:
    """A provider caches a request by its leading bytes and the tools come first.

    The requests differ in everything that decides whether the declaration is
    asked for: an existing file, a command line, a new file, no file at all.
    """
    import json

    from test_agent_loop_one_shot_follow_through import (
        _commit_repo_file,
        _init_git_repo_with_commit,
    )

    root = tmp_path / "repo"
    _init_git_repo_with_commit(root)
    for name in EXISTING:
        _commit_repo_file(root, name, "x\n")

    class Recorder:
        model = "test-model"
        temperature = 0.2

        def __init__(self) -> None:
            self.tools: list[str] = []

        def chat(self, **kwargs):
            self.tools.append(json.dumps(kwargs.get("tools") or [], sort_keys=True))
            return LLMResponse(content="Nothing was changed.", tool_calls=[], raw={})

    session = create_session(
        cfg=AppConfig(
            model="test-model", stream=False, routing_mode="code_only", web_search_mode="off"
        ),
        root=root,
        mode="auto",
        yes=True,
        max_steps=2,
        no_log=False,
        api_key_override="fixture-key",
        surface=NoopSurface(),
        enable_compaction=False,
        session_log_dir_override=tmp_path / "sessions",
    )
    session.client = client = Recorder()
    try:
        for instruction in (
            "Fix the bug in src/app.py.",
            "Make `python3 calc.py` pass.",
            "Write the final report to reports/final.pdf.",
            "Διόρθωσε τα τεστ που αποτυγχάνουν.",
            "Fix the bug in src/app.py and update docs/guide.md.",
        ):
            session.run_turn(instruction)
    finally:
        session.close()
    assert len(client.tools) >= 5
    assert len(set(client.tools)) == 1


def test_a_new_path_with_an_output_folder_keeps_the_declaration(tmp_path) -> None:
    tools, details = _run(
        tmp_path, "Create `summary.md` from README.md.", output_directory="outputs/chat-1"
    )
    assert TOOL_NAME in tools
    assert "task_output_directory_guidance" not in details


# --- reply attachments ----------------------------------------------------------


def _outputs(contract, touched, root=None, **extra):
    state = SimpleNamespace(acceptance_contract=contract, turn_touched_repo_paths=touched, **extra)
    return declared_changed_output_paths(state, root=root)


@pytest.mark.parametrize(
    "instruction",
    [
        "Fix calc.py and add a test file tests/test_calc.py. Check README.md.",
        "Διόρθωσε το calc.py και πρόσθεσε το tests/test_calc.py. Δες το README.md.",
    ],
)
def test_a_named_file_the_turn_changed_is_an_output(workspace, instruction) -> None:
    contract = build_acceptance_contract(root=workspace, instruction=instruction)
    named = named_workspace_paths(list(contract.path_refs))
    assert named == {"calc.py", "tests/test_calc.py", "README.md"}
    touched = {"calc.py", "tests/test_calc.py", "helper.py"}
    # Named and changed: an output. Named and untouched, or changed and not
    # named: not an output.
    assert _outputs(contract, touched, turn_host_read_paths=named) == [
        "calc.py",
        "tests/test_calc.py",
    ]
    assert _outputs(contract, touched) == []
    # With the workspace at hand, a named path the turn removed is not an output.
    assert _outputs(contract, touched, root=workspace, turn_host_read_paths=named) == ["calc.py"]


def test_a_file_named_earlier_in_the_chat_counts_as_named(workspace) -> None:
    # "Fix calc.py", then "add a docstring to it".
    named = named_workspace_paths(earlier_path_refs(workspace, ("Fix calc.py.",)))
    contract = build_acceptance_contract(root=workspace, instruction="Πρόσθεσε ένα docstring.")
    assert _outputs(contract, {"calc.py"}, root=workspace, turn_host_read_paths=named) == [
        "calc.py"
    ]


def test_in_a_nested_project_a_relative_name_is_kept_at_both_bases(workspace) -> None:
    refs = list(build_acceptance_contract(root=workspace, instruction="Fix app.py.").path_refs)
    assert named_workspace_paths(refs, working_directory="src") == {"app.py", "src/app.py"}


def test_a_file_the_turn_saved_in_the_task_output_folder_is_an_output(workspace) -> None:
    # The request names no path: "write a short report about the project".
    contract = build_acceptance_contract(root=workspace, instruction="Γράψε μια σύντομη αναφορά.")
    folder = workspace / "outputs/chat-1"
    (folder / "charts").mkdir(parents=True)
    (folder / "report.md").write_text("report\n", encoding="utf-8")
    (folder / ".draft.md").write_text("draft\n", encoding="utf-8")
    touched = {
        "outputs/chat-1/report.md",
        "outputs/chat-1/.draft.md",
        "outputs/chat-1/charts",  # a folder the turn made
        "outputs/chat-1/removed.md",  # a file the turn deleted again
        "scratch/notes.md",
    }
    assert _outputs(
        contract, touched, root=workspace, turn_host_output_directory="outputs/chat-1"
    ) == ["outputs/chat-1/report.md"]
    assert _outputs(contract, touched, root=workspace) == []
    # A neighbouring folder with the same prefix is not the output folder, and
    # a folder value that is not a plain subfolder selects nothing.
    for value in ("outputs/chat-10", ".", "", "/", "../outputs/chat-1"):
        assert _outputs(contract, touched, root=workspace, turn_host_output_directory=value) == []


def test_a_declaration_keeps_what_the_model_left_out(tmp_path) -> None:
    """The model declares only the new file; the edited one is still an output."""
    from test_agent_loop_one_shot_follow_through import (
        _commit_repo_file,
        _init_git_repo_with_commit,
    )

    from alysis_code.llm.openai_compat import ToolCall

    root = tmp_path / "repo"
    _init_git_repo_with_commit(root)
    _commit_repo_file(root, "calc.py", "x\n")
    session = create_session(
        cfg=AppConfig(
            model="test-model", stream=False, routing_mode="code_only", web_search_mode="off"
        ),
        root=root,
        mode="fullaccess",
        yes=True,
        max_steps=6,
        no_log=False,
        api_key_override="fixture-key",
        surface=NoopSurface(),
        enable_compaction=False,
        session_log_dir_override=tmp_path / "sessions",
    )
    (root / "outputs/chat-1").mkdir(parents=True)
    session.host_task_output_directory = "outputs/chat-1"
    calls = [
        (
            TOOL_NAME,
            {
                "paths": [
                    {
                        "name": "tests/test_calc.py",
                        "directory": "",
                        "role": "required_output",
                        "location": "workspace",
                    }
                ]
            },
        ),
        ("fs_write", {"path": "tests/test_calc.py", "content": "def test_ok():\n    pass\n"}),
        ("fs_write", {"path": "calc.py", "content": "y\n"}),
        ("fs_write", {"path": "outputs/chat-1/notes.md", "content": "notes\n"}),
    ]

    class Client:
        model = "test-model"
        temperature = 0.2

        def chat(self, **kwargs):
            if calls:
                name, arguments = calls.pop(0)
                return LLMResponse(
                    content="",
                    tool_calls=[ToolCall(id=f"c{len(calls)}", name=name, arguments=arguments)],
                    raw={},
                )
            return LLMResponse(content="Done.", tool_calls=[], raw={})

    session.client = Client()
    try:
        session.run_turn("Fix calc.py and add tests/test_calc.py.")
        assert declared_changed_output_paths(session._turn_execution_state, root=root) == [
            "calc.py",
            "outputs/chat-1/notes.md",
            "tests/test_calc.py",
        ]
    finally:
        session.close()
