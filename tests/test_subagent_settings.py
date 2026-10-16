"""Subagent model settings: `agents.list`, `alysis agents list/set` (subagent_settings.py)."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from alysis_code import cli as cli_mod
from alysis_code import config
from alysis_code.cli import app
from alysis_code.config import AppConfig, ConfigError
from alysis_code.ide import management_protocol
from alysis_code.ide.health import SUPPORTED_METHODS
from alysis_code.ide.protocol import ProtocolError
from alysis_code.profiles import ProfileSpec, add_profile, set_active_profile
from alysis_code.subagent_settings import set_subagent_model, subagent_catalog
from alysis_code.subagents import built_in_subagents, load_subagent_registry


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr("alysis_code.subagents.user_config_dir", lambda *_: str(tmp_path / "user"))
    monkeypatch.setattr(config, "load_persisted_profile_keys", lambda: {"parent": "parent-key"})
    monkeypatch.setattr(config, "load_persisted_api_key", lambda: None)
    for name in ("ALYSIS_API_KEY", "OPENAI_API_KEY", "CHILD_PROFILE_KEY"):
        monkeypatch.delenv(name, raising=False)
    for role in ("CODING", "REVIEW", "PLANNER", "COMPACTOR", "APPROVAL_REVIEW"):
        monkeypatch.delenv(f"ALYSIS_MODEL_{role}", raising=False)
    return tmp_path / "user" / "agents"


def profiles_cfg() -> AppConfig:
    cfg = AppConfig(model="parent-model", stream=False)
    add_profile(
        cfg,
        ProfileSpec(
            name="parent",
            base_url="https://parent.example/v1",
            default_model="parent-model",
        ),
    )
    add_profile(
        cfg,
        ProfileSpec(
            name="child",
            base_url="https://child.example/v1",
            default_model="child-model",
            api_key_env="CHILD_PROFILE_KEY",
        ),
    )
    set_active_profile(cfg, "parent")
    cfg.model = "parent-model"
    return cfg


def entry(payload: dict, name: str) -> dict:
    return next(item for item in payload["agents"] if item["name"] == name)


def test_catalog_lists_every_builtin_with_the_model_a_run_would_use() -> None:
    payload = subagent_catalog(profiles_cfg())

    assert [item["name"] for item in payload["agents"]] == list(built_in_subagents())
    explorer = entry(payload, "explorer")
    assert explorer["builtin"] is True
    assert explorer["model"] is None
    assert explorer["effective_model"] == "parent-model"
    assert explorer["effective_profile"] == "parent"
    assert explorer["settable"] is True
    assert entry(payload, "code-reviewer")["model_role"] == "review"
    frontend = entry(payload, "frontend-engineer")
    assert frontend["available"] is False
    assert frontend["unavailable"]["reason_code"] == "subagent_disabled"
    roles = {item["role"]: item for item in payload["roles"]}
    assert list(roles) == [
        "coding",
        "planner",
        "review",
        "compactor",
        "conflict_review",
        "conflict_resolve",
        "approval_review",
    ]
    assert roles["approval_review"]["forge"] is False
    assert roles["review"]["effective_model"] == "parent-model"
    profiles = {item["name"]: item for item in payload["profiles"]}
    assert profiles["parent"] == {"name": "parent", "active": True, "usable": True, "reason": None}
    assert profiles["child"]["usable"] is False
    assert "needs its own stored key" in profiles["child"]["reason"]


def test_a_provider_with_its_own_key_is_usable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHILD_PROFILE_KEY", "child-key")
    profiles = {item["name"]: item for item in subagent_catalog(profiles_cfg())["profiles"]}
    assert profiles["child"]["usable"] is True
    assert profiles["child"]["active"] is False


def test_setting_a_model_writes_a_metadata_only_override(isolated: Path) -> None:
    cfg = profiles_cfg()

    result = set_subagent_model(cfg, "explore", model="fast-model", profile="parent")

    path = isolated / "explorer.md"
    assert result["path"] == str(path)
    assert path.read_text(encoding="utf-8") == (
        "---\nname: explorer\nmodel: fast-model\nprofile: parent\n---\n"
    )
    definition = load_subagent_registry(root=None)["explorer"]
    assert replace(definition, model=None, profile=None) == built_in_subagents()["explorer"]
    assert result["agent"]["effective_model"] == "fast-model"
    assert result["agent"]["model"] == "fast-model"


def test_a_model_turns_on_the_optional_frontend_engineer() -> None:
    cfg = profiles_cfg()

    result = set_subagent_model(cfg, "frontend", model="ui-model", profile="parent")

    assert result["agent"]["name"] == "frontend-engineer"
    assert result["agent"]["available"] is True


def test_another_provider_needs_its_own_key_and_nothing_is_written(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = profiles_cfg()
    with pytest.raises(ConfigError, match="needs its own stored key"):
        set_subagent_model(cfg, "explorer", model="child-model", profile="child")
    assert not (isolated / "explorer.md").exists()

    monkeypatch.setenv("CHILD_PROFILE_KEY", "child-key")
    result = set_subagent_model(cfg, "explorer", model="child-model", profile="child")
    assert result["agent"]["effective_profile"] == "child"
    assert result["agent"]["effective_model"] == "child-model"


def test_unknown_profile_and_odd_values_are_refused(isolated: Path) -> None:
    cfg = profiles_cfg()
    with pytest.raises(ConfigError, match="Profile not found"):
        set_subagent_model(cfg, "explorer", model="m", profile="nope")
    with pytest.raises(ConfigError, match="model must be one word"):
        set_subagent_model(cfg, "explorer", model="two words", profile=None)
    with pytest.raises(ConfigError, match="model must be one word"):
        set_subagent_model(cfg, "explorer", model="m\nenabled: false", profile=None)
    with pytest.raises(ConfigError, match="Unknown subagent"):
        set_subagent_model(cfg, "nobody", model="m", profile=None)
    assert not isolated.exists() or not any(isolated.iterdir())


def test_clearing_removes_only_what_the_app_set(isolated: Path) -> None:
    cfg = profiles_cfg()
    set_subagent_model(cfg, "explorer", model="fast-model", profile="parent")
    set_subagent_model(cfg, "explorer", model=None, profile=None)
    assert not (isolated / "explorer.md").exists()

    isolated.mkdir(parents=True, exist_ok=True)
    (isolated / "debugger.md").write_text(
        "---\nname: debugger\n# kept\nmode: review\nmodel: old\n---\n", encoding="utf-8"
    )
    set_subagent_model(cfg, "debugger", model=None, profile=None)
    assert (isolated / "debugger.md").read_text(encoding="utf-8") == (
        "---\nname: debugger\n# kept\nmode: review\n---\n"
    )


def test_a_file_with_its_own_prompt_keeps_the_prompt(isolated: Path) -> None:
    isolated.mkdir(parents=True)
    path = isolated / "mine.md"
    path.write_text(
        "---\nname: verifier\ndescription: Mine\nmodel: old\n---\nCheck it twice.\n",
        encoding="utf-8",
    )

    set_subagent_model(profiles_cfg(), "verifier", model="new-model", profile="parent")

    assert path.read_text(encoding="utf-8") == (
        "---\nname: verifier\ndescription: Mine\nmodel: new-model\nprofile: parent\n---\n"
        "Check it twice.\n"
    )
    assert not (isolated / "verifier.md").exists()


def test_a_project_agent_is_listed_but_changed_in_its_own_file(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / ".alysis_agents").mkdir(parents=True)
    (project / ".alysis_agents" / "docs-writer.md").write_text(
        "---\nname: docs-writer\ndescription: Writes docs\n---\nWrite the docs.\n",
        encoding="utf-8",
    )
    cfg = profiles_cfg()

    payload = subagent_catalog(cfg, root=project)
    writer = entry(payload, "docs-writer")
    assert writer["builtin"] is False
    assert writer["settable"] is False
    assert "docs-writer" not in [item["name"] for item in subagent_catalog(cfg)["agents"]]
    with pytest.raises(ConfigError, match="defined by this project"):
        set_subagent_model(cfg, "docs-writer", model="m", profile=None, root=project)


def test_agents_list_is_a_read_only_management_method(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = profiles_cfg()
    monkeypatch.setattr(management_protocol, "load_config", lambda: cfg)

    payload = management_protocol.handle_management_method("agents.list", {}, request_id=1)

    assert "agents.list" in management_protocol.MANAGEMENT_METHODS
    assert "agents.list" not in management_protocol.MUTATING_MANAGEMENT_METHODS
    assert "agents.list" in SUPPORTED_METHODS
    assert entry(payload, "explorer")["effective_model"] == "parent-model"
    assert payload["secret_values_included"] is False
    assert "parent-key" not in json.dumps(payload)
    with pytest.raises(ProtocolError):
        management_protocol.handle_management_method(
            "agents.list", {"workspace": str(Path("does-not-exist"))}, request_id=2
        )


def test_cli_lists_and_sets(isolated: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = profiles_cfg()
    monkeypatch.setattr(cli_mod, "load_config", lambda: cfg)
    runner = CliRunner()

    listed = runner.invoke(app, ["agents", "list", "--json"])
    assert listed.exit_code == 0, listed.output
    assert entry(json.loads(listed.stdout), "general")["effective_model"] == "parent-model"

    saved = runner.invoke(
        app, ["agents", "set", "general", "--model", "big-model", "--profile", "parent"]
    )
    assert saved.exit_code == 0, saved.output
    assert "general uses big-model on parent" in saved.output
    assert (isolated / "general.md").exists()

    refused = runner.invoke(app, ["agents", "set", "general", "--profile", "nope"])
    assert refused.exit_code == 2
    assert "Profile not found" in refused.output

    cleared = runner.invoke(app, ["agents", "set", "general", "--clear"])
    assert cleared.exit_code == 0, cleared.output
    assert not (isolated / "general.md").exists()

    both = runner.invoke(app, ["agents", "set", "general", "--clear", "--model", "x"])
    assert both.exit_code != 0
