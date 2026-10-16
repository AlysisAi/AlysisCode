"""Host-visible capabilities must survive validation and reach the actual wire."""

import json

import httpx
import pytest

from alysis_code.config import AppConfig
from alysis_code.llm.factory import make_llm_client
from alysis_code.model_reasoning import model_reasoning_capabilities
from alysis_code.profile_presets import PRESET_BY_KEY, make_profile_from_preset
from alysis_code.provider_auth.base import ProviderAuthError


def config(preset, model):
    profile = make_profile_from_preset(PRESET_BY_KEY[preset], name="renamed-provider")
    cfg = AppConfig(model=model)
    cfg.extra_fields = {
        "profiles": {profile.name: profile.to_dict()},
        "active_profile": profile.name,
    }
    return cfg


def response():
    # A single offline response carries the three protocols' response shapes;
    # assertions below examine real requests, not mock client implementations.
    return httpx.Response(
        200,
        json={
            "id": "fixture",
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
            "candidates": [
                {"content": {"role": "model", "parts": [{"text": "ok"}]}, "finishReason": "STOP"}
            ],
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "ok"}],
                }
            ],
        },
    )


@pytest.mark.parametrize(
    "preset,model,effort,expected",
    [
        *(
            ("zai-coding-plan", "glm-5.3-flash", value, {"reasoning_effort": value})
            for value in ("low", "high", "max")
        ),
        ("zhipu", "glm-5.2", "max", {"reasoning_effort": "max"}),
        ("zhipu", "glm-5.2", "none", {"reasoning_effort": "none"}),
        ("zhipu", "glm-4.7-flash", "disabled", {"thinking": {"type": "disabled"}}),
        (
            "qwen-intl",
            "qwen3.8-flash",
            "xhigh",
            {"reasoning_effort": "xhigh", "enable_thinking": True},
        ),
        ("qwen-us", "qwen3.7-plus", "enabled", {"enable_thinking": True}),
        ("qwen-cn", "qwen3.7-plus", "none", {"enable_thinking": False}),
        (
            "deepseek",
            "deepseek-flash",
            "low",
            {"reasoning_effort": "low", "thinking": {"type": "enabled"}},
        ),
        ("deepseek", "deepseek-v4-pro", "none", {"thinking": {"type": "disabled"}}),
        ("moonshot", "kimi-k3", "max", {"reasoning_effort": "max"}),
        ("kimi-code", "k3-256k", "low", {"reasoning_effort": "low"}),
        ("moonshot", "kimi-k2.6", "disabled", {"thinking": {"type": "disabled"}}),
        ("minimax", "MiniMax-M3", "adaptive", {"thinking": {"type": "adaptive"}}),
        ("minimax", "MiniMax-M3", "disabled", {"thinking": {"type": "disabled"}}),
        ("groq", "openai/gpt-oss-120b", "high", {"reasoning_effort": "high"}),
        ("cerebras", "gemma-4-31b", "none", {"reasoning_effort": "none"}),
        ("cerebras", "gemma-4-31b", "medium", {"reasoning_effort": "medium"}),
        ("xai", "grok-4.6", "xhigh", {"reasoning_effort": "xhigh"}),
        ("xai", "grok-4.5", "low", {"reasoning_effort": "low"}),
        ("cohere", "command-a-reasoning-08-2025", "high", {"reasoning_effort": "high"}),
        ("mistral", "zai-glm-5-3", "max", {"reasoning_effort": "max"}),
        ("mistral", "mistral-small-2603", "none", {"reasoning_effort": "none"}),
        ("together", "MiniMaxAI/MiniMax-M3", "enabled", {"reasoning": {"enabled": True}}),
        ("together", "openai/gpt-oss-120b", "none", {"reasoning": {"enabled": False}}),
        (
            "fireworks",
            "accounts/fireworks/models/deepseek-v4-pro-0813",
            "max",
            {"reasoning_effort": "max"},
        ),
        ("openai-responses", "gpt-5.6-sol", "max", {"reasoning": {"effort": "max"}}),
        ("anthropic", "claude-opus-5-5", "xhigh", {"output_config": {"effort": "xhigh"}}),
        ("anthropic", "claude-opus-5", "none", {"thinking": {"type": "disabled"}}),
        (
            "anthropic",
            "claude-sonnet-5-5",
            "between_tools",
            {"thinking": {"type": "between_tools"}, "output_config": {"effort": "high"}},
        ),
        ("anthropic", "claude-haiku-4-5", "enabled", {}),
        ("gemini", "gemini-3.8-flash", "low", {}),
        ("gemini-compat", "gemini-3.8-flash", "medium", {"reasoning_effort": "medium"}),
    ],
)
def test_advertised_option_reaches_wire(monkeypatch, preset, model, effort, expected):
    cfg = config(preset, model)
    before = cfg.model_dump(mode="json")
    capabilities = model_reasoning_capabilities(cfg)
    assert effort in {item["id"] for item in capabilities["reasoning_efforts"]}
    monkeypatch.setenv("ALYSIS_RUN_REASONING_EFFORT", effort)
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return response()

    client = make_llm_client(
        cfg=cfg, api_key="fixture", model=model, transport=httpx.MockTransport(handler)
    )
    client.chat(messages=[{"role": "user", "content": "fixture"}], max_tokens=8192)
    payload = requests[-1]
    for key, value in expected.items():
        assert payload[key] == value
    if model == "claude-haiku-4-5":
        assert payload["thinking"]["type"] == "enabled"
        assert 1024 <= payload["thinking"]["budget_tokens"] < payload["max_tokens"]
        assert "output_config" not in payload
    if preset == "gemini":
        assert payload["generationConfig"]["thinkingConfig"]["thinkingLevel"] == effort
    assert cfg.model_dump(mode="json") == before


@pytest.mark.parametrize(
    "preset,model,invalid",
    [
        ("zai-coding-plan", "glm-5.3-flash", "none"),
        ("anthropic", "claude-opus-5-5", "none"),
        ("anthropic", "claude-sonnet-5-5", "none"),
        ("gemini", "gemini-3.8-flash", "minimal"),
        ("kimi-code", "k3", "none"),
        ("minimax", "MiniMax-M2.7", "high"),
        ("minimax", "MiniMax-M3", "high"),
        ("anthropic-compat", "claude-opus-5", "high"),
        ("xai", "grok-4.20-0309-reasoning", "low"),
    ],
)
def test_invalid_or_unimplemented_controls_fail_before_request(monkeypatch, preset, model, invalid):
    cfg = config(preset, model)
    monkeypatch.setenv("ALYSIS_RUN_REASONING_EFFORT", invalid)
    with pytest.raises(ProviderAuthError, match="not supported"):
        make_llm_client(
            cfg=cfg,
            api_key="fixture",
            model=model,
            transport=httpx.MockTransport(lambda request: pytest.fail("network call")),
        )


def test_glm_default_and_auto_preserve_provider_behavior(monkeypatch):
    cfg = config("zai-coding-plan", "glm-5.3-flash")
    caps = model_reasoning_capabilities(cfg)
    assert [item["id"] for item in caps["reasoning_efforts"]] == ["low", "high", "max"]
    assert caps["default_reasoning_effort"] == "max"
    monkeypatch.setenv("ALYSIS_RUN_REASONING_EFFORT", "auto")
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return response()

    make_llm_client(
        cfg=cfg, api_key="fixture", model=cfg.model, transport=httpx.MockTransport(handler)
    ).chat(messages=[{"role": "user", "content": "fixture"}])
    assert "reasoning_effort" not in requests[0]
    assert "thinking" not in requests[0]


@pytest.mark.parametrize("mandatory", [True, False])
def test_openrouter_live_catalog_owns_options_and_request(monkeypatch, mandatory):
    cfg = config("openrouter", "vendor/new-model")
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path.endswith("/models"):
            assert "Authorization" not in request.headers
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": cfg.model,
                            "reasoning": {
                                "supported_efforts": ["max", "high", "low"],
                                "default_effort": "high",
                                "mandatory": mandatory,
                            },
                        }
                    ]
                },
            )
        return response()

    transport = httpx.MockTransport(handler)
    caps = model_reasoning_capabilities(cfg, transport=transport)
    values = [item["id"] for item in caps["reasoning_efforts"]]
    assert ("none" in values) is (not mandatory)
    assert "medium" not in values
    assert caps["default_reasoning_effort"] == "high"
    monkeypatch.setenv("ALYSIS_RUN_REASONING_EFFORT", "low")
    make_llm_client(cfg=cfg, api_key="fixture", model=cfg.model, transport=transport).chat(
        messages=[{"role": "user", "content": "fixture"}]
    )
    assert json.loads(calls[-1].content)["reasoning"] == {"effort": "low"}


def test_catalog_outage_is_not_reported_as_unsupported():
    cfg = config("openrouter", "vendor/new-model")
    with pytest.raises(ProviderAuthError, match="temporarily unavailable"):
        model_reasoning_capabilities(
            cfg, transport=httpx.MockTransport(lambda request: httpx.Response(503))
        )


@pytest.mark.parametrize("adapter", ["none", "openai_compat_passive"])
def test_disabled_or_passive_adapter_does_not_offer_ignored_controls(adapter):
    cfg = config("zai-coding-plan", "glm-5.3-flash")
    cfg.extra_fields["profiles"]["renamed-provider"]["reasoning_trace_adapter"] = adapter
    assert model_reasoning_capabilities(cfg)["reasoning_efforts"] == []


@pytest.mark.parametrize(
    "preset,model,choice,field,expected",
    [
        ("anthropic", "claude-haiku-4-5", "enabled", "thinking", {"type": "enabled"}),
        ("qwen-us", "qwen3.7-plus", "enabled", "enable_thinking", True),
        ("together", "MiniMaxAI/MiniMax-M3", "enabled", "reasoning", {"enabled": True}),
        ("minimax", "MiniMax-M3", "disabled", "thinking", {"type": "disabled"}),
        ("zhipu", "glm-4.7-flash", "disabled", "thinking", {"type": "disabled"}),
        ("anthropic", "claude-sonnet-5-5", "between_tools", "thinking", {"type": "between_tools"}),
    ],
)
def test_toggle_choice_survives_session_config_reload(
    monkeypatch, tmp_path, preset, model, choice, field, expected
):
    from alysis_code.agent_loop import create_session
    from alysis_code.cli_impl.chat import loop
    from alysis_code.runtime_kind import RuntimeKind
    from alysis_code.surface.noop_surface import NoopSurface

    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("RELOAD_FIXTURE_KEY", "fixture-key")
    monkeypatch.setenv("ALYSIS_RUN_REASONING_EFFORT", choice)
    # These UI/tool refresh hooks are injected by the CLI entry point and are
    # unrelated to the real retained-client config reload exercised here.
    for name in (
        "_rebuild_session_tools_for_mode",
        "refresh_session_environment_context_message",
        "_refresh_chat_hud_context_cache",
    ):
        monkeypatch.setattr(loop, name, lambda *args, **kwargs: None, raising=False)
    monkeypatch.setattr(
        httpx.HTTPTransport, "handle_request", lambda *args: pytest.fail("real network call")
    )
    cfg = config(preset, model)
    cfg.skills_enabled = False
    cfg.extra_fields["profiles"]["renamed-provider"]["api_key_env"] = "RELOAD_FIXTURE_KEY"
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="readonly",
        runtime_kind=RuntimeKind.SUBAGENT,
        subagent_depth=1,
        yes=True,
        max_steps=2,
        no_log=True,
        enable_compaction=False,
        subagents_enabled=False,
        surface=NoopSurface(),
    )
    payloads = []

    def handler(request):
        payloads.append(json.loads(request.content))
        return response()

    try:
        session.client._transport = httpx.MockTransport(handler)
        session.client.chat(messages=[{"role": "user", "content": "before"}], max_tokens=8192)
        loop._apply_config_menu_changes_to_session(session=session, cfg=session.cfg)
        session.client.chat(messages=[{"role": "user", "content": "after"}], max_tokens=8192)
        assert len(payloads) == 2
        assert payloads[0][field] == payloads[1][field]
        if isinstance(expected, dict):
            assert expected.items() <= payloads[1][field].items()
        else:
            assert payloads[1][field] == expected
        assert "reasoning_effort" not in payloads[1]
    finally:
        session.close()


@pytest.mark.parametrize("same_name", [False, True])
@pytest.mark.parametrize("preset,model", [("minimax", "MiniMax-M3"), ("zhipu", "glm-5.3-flash")])
def test_other_profile_does_not_inherit_chat_effort(monkeypatch, same_name, preset, model):
    cfg = config("zai-coding-plan", "glm-5.3-flash")
    profile = make_profile_from_preset(
        PRESET_BY_KEY[preset], name="renamed-provider" if same_name else "other-route"
    )
    monkeypatch.setenv("ALYSIS_RUN_REASONING_EFFORT", "max")
    payloads = []

    def handler(request):
        assert str(request.url).startswith(profile.base_url)
        payloads.append(json.loads(request.content))
        return response()

    make_llm_client(
        cfg=cfg,
        profile=profile,
        api_key="fixture",
        model=model,
        transport=httpx.MockTransport(handler),
    ).chat(messages=[{"role": "user", "content": "fixture"}])
    assert "thinking" not in payloads[0]
    assert "reasoning_effort" not in payloads[0]


@pytest.mark.parametrize("preset", ["anthropic", "alysis"])
def test_sonnet_55_catalog_names_its_actual_lowest_mode(preset, configured_hosted_api):
    caps = model_reasoning_capabilities(config(preset, "claude-sonnet-5-5"))
    choices = {item["id"]: item for item in caps["reasoning_efforts"]}
    assert "none" not in choices
    assert choices["between_tools"]["label"] == "Between tools"
    assert "up-front" in choices["between_tools"]["description"]
    assert "High" in choices["between_tools"]["description"]
    if preset == "anthropic":
        assert caps["default_reasoning_effort"] == "high"


@pytest.mark.parametrize("compactor_model", ["glm-4.7", "glm-5.3-flash"])
def test_chat_effort_is_scoped_through_session_start_and_reload(
    monkeypatch, tmp_path, compactor_model
):
    from alysis_code.agent_loop import create_session
    from alysis_code.cli_impl.chat import loop
    from alysis_code.surface.noop_surface import NoopSurface

    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SCOPING_FIXTURE_KEY", "fixture-key")
    monkeypatch.setenv("ALYSIS_RUN_REASONING_EFFORT", "max")
    for name in (
        "_rebuild_session_tools_for_mode",
        "refresh_session_environment_context_message",
        "_refresh_chat_hud_context_cache",
    ):
        monkeypatch.setattr(loop, name, lambda *args, **kwargs: None, raising=False)
    monkeypatch.setattr(
        httpx.HTTPTransport, "handle_request", lambda *args: pytest.fail("real network call")
    )
    cfg = config("zhipu", "glm-5.3-flash")
    cfg.skills_enabled = False
    cfg.extra_fields["role_models"] = {"compactor": compactor_model}
    cfg.extra_fields["profiles"]["renamed-provider"]["api_key_env"] = "SCOPING_FIXTURE_KEY"
    before = cfg.model_dump(mode="json")
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="readonly",
        yes=True,
        max_steps=2,
        no_log=False,
        subagents_enabled=False,
        surface=NoopSurface(),
    )
    payloads = []

    def handler(request):
        payloads.append(json.loads(request.content))
        return response()

    try:
        assert session.conversation_compactor is not None
        clients = [session.client, session.conversation_compactor.compactor_client]
        for client in clients:
            client._transport = httpx.MockTransport(handler)
        # Exercise retained clients as the user changes the chat picker.
        for choice in ("max", "low", "auto", "max"):
            monkeypatch.setenv("ALYSIS_RUN_REASONING_EFFORT", choice)
            if payloads:
                loop._apply_config_menu_changes_to_session(session=session, cfg=session.cfg)
            for client in clients:
                client.chat(messages=[{"role": "user", "content": "fixture"}])
            main, compactor = payloads[-2:]
            expected = None if choice == "auto" else choice
            assert main.get("reasoning_effort") == expected
            assert compactor["model"] == compactor_model
            assert compactor.get("reasoning_effort") == (
                expected if compactor_model == cfg.model else None
            )
            if compactor_model != cfg.model:
                assert "thinking" not in compactor
        assert cfg.model_dump(mode="json") == before
    finally:
        session.close()


@pytest.mark.parametrize("choice", ["low", "auto"])
def test_openrouter_outage_blocks_explicit_choice_but_not_auto(monkeypatch, choice):
    cfg = config("openrouter", "vendor/new-model")
    monkeypatch.setenv("ALYSIS_RUN_REASONING_EFFORT", choice)
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/models"):
            return httpx.Response(503)
        assert "reasoning" not in json.loads(request.content)
        return response()

    def send():
        make_llm_client(
            cfg=cfg, api_key="fixture", model=cfg.model, transport=httpx.MockTransport(handler)
        ).chat(messages=[{"role": "user", "content": "fixture"}])

    if choice == "auto":
        send()
        assert calls == ["/api/v1/chat/completions"]
    else:
        with pytest.raises(ProviderAuthError, match="temporarily unavailable"):
            send()
        assert calls == ["/api/v1/models"]
