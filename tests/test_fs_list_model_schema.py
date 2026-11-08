from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from alysis_code import agent_loop
from alysis_code.agent import session as session_mod
from alysis_code.config import AppConfig
from alysis_code.subagents import built_in_subagents


def _listing_schema(session: session_mod.AgentSession) -> dict[str, Any]:
    return next(
        copy.deepcopy(tool["function"])
        for tool in session.tool_list
        if tool["function"]["name"] == "fs_list"
    )


@pytest.mark.parametrize("profile", ["compact", "balanced", "expanded"])
@pytest.mark.parametrize("one_shot", [False, True], ids=["chat", "run"])
def test_prepared_parent_and_child_schemas_retain_listing_scope_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: str,
    one_shot: bool,
) -> None:
    children: list[dict[str, Any]] = []

    def record_without_provider(self: session_mod.AgentSession, task: str, **_kwargs: Any) -> int:
        children.append(
            {
                "schema": _listing_schema(self),
                "mode": self.mode,
                "profile": self.prompt_guidance_profile,
            }
        )
        report = "Inspected the prepared tool surface; no provider or filesystem work was needed."
        self.messages.append({"role": "assistant", "content": report})
        self.store.append("final", {"content": report})
        return 0

    monkeypatch.setattr(session_mod.AgentSession, "run_turn", record_without_provider)
    monkeypatch.setattr(agent_loop, "create_session", session_mod.create_session)
    cfg = AppConfig(
        model="gpt-5.6-luna",
        prompt_guidance={"default": profile, "model_profiles": {}},
        skills_enabled=False,
        bundled_skills_enabled=False,
        web_search_mode="off",
        subagents_enabled=True,
        stream=False,
    )
    session = session_mod.create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        no_log=True,
        max_steps=None,
        api_key_override="test-key",
        enable_compaction=False,
        verification_enabled=False,
        subagent_registry=built_in_subagents(include_visual_designer=False),
        session_log_dir_override=tmp_path / "sessions",
        non_interactive=one_shot,
        one_shot_execution=one_shot,
        enable_chat_turn_step_budget=True,
    )
    try:
        parent = _listing_schema(session)
        description = parent["description"]
        assert "Files only under root_path" in description
        assert "Empty/omitted globs: '**/*' recursively" in description
        assert "'*': direct files" in description
        assert "ignore: exact path components" in description
        assert "Truncation covers filtered scope" in description
        assert set(parent["parameters"]["properties"]) == {
            "root_path",
            "path_base",
            "globs",
            "ignore",
        }
        assert parent["parameters"]["required"] == []
        assert session.mode == "auto"
        assert session.prompt_guidance_profile == profile
        for role in ("general", "explorer"):
            result = session.tools["subagent_run"].run(
                {"name": role, "task": "Inspect the prepared listing tool surface."}
            )
            assert "error" not in result, result
        assert len(children) == 2
        assert [child["mode"] for child in children] == ["auto", "readonly"]
        # Each explicit generic parent density uses the same general child
        # base, even though the configured model is normally in the GPT family.
        assert all(child["profile"] == "general-subagent" for child in children)
        for child in children:
            schema = child["schema"]
            assert schema["name"] == parent["name"]
            assert schema["parameters"] == parent["parameters"]
            # Approval guidance differs by mode: a readonly explorer must not
            # advertise the interactive parent's outside-project access path.
            for contract in (
                "Files only under root_path",
                "Empty/omitted globs: '**/*' recursively",
                "'*': direct files",
                "ignore: exact path components",
                "Truncation covers filtered scope",
            ):
                assert contract in schema["description"]
            assert ("requests scoped access" in schema["description"]) == (
                not one_shot and child["mode"] == "auto"
            )
    finally:
        session.close()
