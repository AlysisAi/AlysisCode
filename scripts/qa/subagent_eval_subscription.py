"""Invocation-local configuration for the real CLI; never save credentials/config.

Only the two explicitly authorized subscription models are accepted. The live
runner launches this file with the frozen source first on PYTHONPATH.
"""

from __future__ import annotations

import functools
import json
import os
import sys
from pathlib import Path


def validate_child_models(cfg, root: Path, model: str) -> list[str]:
    from alysis_code.agent.subagent_execution import SubagentLauncher
    from alysis_code.subagents import load_subagent_registry

    selected_models = sorted(
        {
            SubagentLauncher._resolve_subagent_model(definition, cfg)[0]
            for definition in load_subagent_registry(root=root).values()
        }
    )
    if any(selected != model for selected in selected_models):
        raise RuntimeError(
            "An effective child model conflicts with this attempt's authorized model"
        )
    return selected_models


def observe_root_turns(session_class, path: Path) -> None:
    """Observe real terminal boundaries without changing runtime results/history."""
    original = session_class.run_turn
    count = 0

    @functools.wraps(original)
    def observed(self, *args, **kwargs):
        nonlocal count
        if self.subagent_depth != 0:
            return original(self, *args, **kwargs)
        count += 1
        payload = {"turn": count}
        try:
            result = original(self, *args, **kwargs)
            payload.update(exit_code=result, reason="run_turn_returned")
            return result
        except BaseException as error:
            payload.update(
                exit_code=None, reason="run_turn_raised", exception_type=type(error).__name__
            )
            raise
        finally:
            with path.open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps({"event_type": "observed_root_turn_boundary", "payload": payload})
                    + "\n"
                )
                stream.flush()
                os.fsync(stream.fileno())

    session_class.run_turn = observed


def main() -> None:
    from alysis_code import config
    from alysis_code.profiles import get_active_profile, update_profile

    source = Path(os.environ["ALYSIS_EVAL_SOURCE"]).resolve() / "src"
    if not Path(config.__file__).resolve().is_relative_to(source):
        raise RuntimeError("The CLI did not import the frozen evaluation source")
    model = os.environ["ALYSIS_EVAL_MODEL"]
    if model not in {"gpt-5.6-luna", "gpt-5.6-terra"}:
        raise ValueError("This evaluation permits only the explicitly selected Luna/Terra models")
    guidance = json.loads(os.environ.get("ALYSIS_EVAL_GUIDANCE", "null"))
    original_load = config.load_config

    def load_evaluation_config():
        cfg = original_load()
        profile = get_active_profile(cfg)
        if profile.auth_provider != "openai-codex":
            raise RuntimeError("Evaluation requires the existing ChatGPT subscription connection")
        update_profile(
            cfg,
            profile.name,
            default_model=model,
            reasoning_effort="high",
            allow_subscription_selection=True,
        )
        if guidance is not None:
            if not hasattr(config, "PromptGuidanceConfig"):
                raise ValueError("This frozen source does not support prompt guidance profiles")
            cfg.prompt_guidance = config.PromptGuidanceConfig.model_validate(guidance)
        child_models = validate_child_models(cfg, Path(os.environ["ALYSIS_EVAL_WORKSPACE"]), model)
        # Only safe evaluation selectors are recorded, never the user's config,
        # profile connection, credential values or environment.
        selection = {
            "model": model,
            "reasoning_effort": "high",
            "auth_provider": "openai-codex",
            "source": str(source.parent),
            "guidance_override": guidance,
            "effective_guidance": (
                config.resolve_prompt_guidance_profile(cfg, model=model)
                if hasattr(config, "resolve_prompt_guidance_profile")
                else None
            ),
            "effective_child_models": child_models,
        }
        Path(os.environ["ALYSIS_EVAL_SELECTION_PATH"]).write_text(
            json.dumps(selection, indent=2) + "\n", encoding="utf-8"
        )
        return cfg

    config.load_config = load_evaluation_config
    if sys.argv[1:] == ["--evaluation-preflight"]:
        load_evaluation_config()
        print("Frozen import, subscription, model, guidance and child-model preflight passed.")
        return
    from alysis_code.agent.session import AgentSession

    observe_root_turns(AgentSession, Path(os.environ["ALYSIS_EVAL_TURN_BOUNDARIES"]))
    from alysis_code.cli import app

    app()


if __name__ == "__main__":
    main()
