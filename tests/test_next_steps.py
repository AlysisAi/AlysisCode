from __future__ import annotations

from alysis_code.agent.tools_assembly import build_tools
from alysis_code.cli_impl.tui.surface import TuiSurface
from alysis_code.cli_impl.tui.transcript import TuiTranscript
from alysis_code.ide.activity_events import tool_activity
from alysis_code.next_steps import NEXT_STEP_MAX_CHARS, suggest_next_steps
from alysis_code.session_store import SessionStore


def _tools(tmp_path, *, shown: bool, mode: str = "auto"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    surface = TuiSurface(TuiTranscript())
    if shown:
        surface.host_renders_next_steps = True
    store = SessionStore(
        sessions_dir=tmp_path / "session",
        enabled=False,
        session_id="desktop",
        cwd=str(tmp_path),
        repo_root=None,
    )
    return build_tools(
        root=tmp_path,
        console=None,
        surface=surface,
        store=store,
        mode=mode,
        yes=True,
        non_interactive=True,
        runtime_kind="interactive_chat",
    )


def test_steps_in_any_script_are_kept_as_written():
    steps = [
        "Add the 2011 figures for comparison",
        "Πρόσθεσε τα στοιχεία του 2011",
        "把结果另存为表格",
        "می\u200cخواهم نمودار را ببینم",  # a non-joiner is part of the spelling, not a control
    ]
    for pair in (steps[:2], steps[2:]):
        result = suggest_next_steps({"steps": pair})
        assert result == {"ok": True, "shown": 2, "steps": pair}


def test_whitespace_is_collapsed_and_repeats_are_dropped():
    result = suggest_next_steps(
        {"steps": ["  Check   the\nsources ", "check the sources", "Export"]}
    )
    assert result == {"ok": True, "shown": 2, "steps": ["Check the sources", "Export"]}


def test_invalid_steps_are_refused_without_showing_anything():
    cases = [
        {},
        {"steps": "Export"},
        {"steps": ["Only one"]},
        {"steps": ["One", "Two", "Three", "Four"]},
        {"steps": ["Same", "same"]},
        {"steps": ["Fine", ""]},
        {"steps": ["Fine", 3]},
        {"steps": ["Fine", "x" * (NEXT_STEP_MAX_CHARS + 1)]},
        {"steps": ["Fine", "bell\x07"]},
        {"steps": ["Fine", "abc\u202edcba"]},
    ]
    for args in cases:
        result = suggest_next_steps(args)
        assert result["shown"] == 0 and result["error_code"] == "invalid_next_steps", args


def test_offered_only_when_the_host_shows_next_steps(tmp_path):
    assert "suggest_next_steps" in _tools(tmp_path / "shown", shown=True)
    assert "suggest_next_steps" not in _tools(tmp_path / "hidden", shown=False)
    # It changes nothing, so a read-only chat keeps it.
    assert "suggest_next_steps" in _tools(tmp_path / "readonly", shown=True, mode="readonly")


def test_the_step_reads_as_a_plan_activity_with_a_stable_operation():
    event = tool_activity(call_id="c1", name="suggest_next_steps", arguments={"steps": ["a", "b"]})
    payload = event.to_payload()
    assert payload["kind"] == "plan"
    assert payload["operation"] == "suggest_next_steps"


def test_model_written_step_titles_are_marked_for_the_host():
    described = tool_activity(
        call_id="c2",
        name="shell_run",
        arguments={"cmd": "python a.py", "description": "Σύγκριση τριμήνων"},
    ).to_payload()
    assert described["display_title"] == "Σύγκριση τριμήνων"
    assert described["title_source"] == "model"
    plain = tool_activity(call_id="c3", name="fs_read", arguments={"path": "a.txt"}).to_payload()
    assert "title_source" not in plain and plain["operation"] == "read_file"
