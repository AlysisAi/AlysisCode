from alysis_code.profile_presets import get_preset, model_options_for_preset


def test_hosted_picker_uses_live_catalog_for_free_and_paid_models():
    preset = get_preset("alysis")
    available = {"gpt-6-luna", "deepseek-v4.1-flash", "unknown-model"}
    rows = model_options_for_preset(preset, available_models=available)
    assert {row[0] for row in rows} == {"gpt-6-luna", "deepseek-v4.1-flash"}
    assert all("claude" not in row[0] for row in rows)


def test_empty_hosted_catalog_does_not_reintroduce_disabled_suggestions():
    assert model_options_for_preset(get_preset("alysis"), available_models=set()) == ()


def test_unqueried_catalog_and_other_providers_keep_existing_suggestions():
    hosted = get_preset("alysis")
    assert model_options_for_preset(hosted)
    assert not any(row[0] in hosted.paid_models for row in model_options_for_preset(hosted))
    provider = get_preset("openai")
    assert model_options_for_preset(provider, available_models=set()) == model_options_for_preset(
        provider
    )


def test_paid_plan_catalogs_expose_six_and_thirteen_models_without_held_routes():
    plan_1 = {
        "gpt-6-luna",
        "glm-5.3-flash",
        "deepseek-v4.1-flash",
        "minimax-m3.1-flash",
        "reflection-beam",
        "nemotron-3.5-super-vl",
    }
    plan_5 = plan_1 | {
        "gpt-6.1-sol",
        "deepseek-v4-pro",
        "mistral-large-4",
        "glm-5.3",
        "qwen3.8-2.4t",
        "kimi-k3",
        "muse-spark-open",
    }
    preset = get_preset("alysis")
    for catalog, count in ((plan_1, 6), (plan_5, 13)):
        rows = model_options_for_preset(preset, available_models=catalog)
        assert len(rows) == count
        assert {row[0] for row in rows} == catalog


def test_terminal_picker_handles_an_unavailable_catalog_without_crashing(monkeypatch):
    import io

    import pytest
    from rich.console import Console

    from alysis_code.cli_impl import config_menu

    state = config_menu.ConfigMenuState.from_cfg(config_menu.AppConfig())
    state._provider_model_catalog_warning = "The hosted catalog is unavailable."
    monkeypatch.setattr(config_menu, "_default_model_rows", lambda state: [])
    output = io.StringIO()
    with pytest.raises(config_menu.Abort):
        config_menu._prompt_default_model(Console(file=output), state)
    assert "No models are currently available" in output.getvalue()
