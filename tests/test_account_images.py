"""Pictures through the Alysis account: what the account declares, and which declaration a model
connection uses (account_images.py)."""

from __future__ import annotations

from dataclasses import replace

import pytest

import alysis_code.config as config_module
from alysis_code import alysis_cloud
from alysis_code.account_images import (
    account_answer,
    connection_image_generation,
    declared_by_account,
    forget_account_answers,
    picture_models,
    session_image_generation,
)
from alysis_code.config import (
    ApiKeyResolution,
    AppConfig,
    ImageGenerationConfig,
    save_persisted_profile_key,
)
from alysis_code.profile_presets import get_preset, make_profile_from_preset
from alysis_code.profiles import add_profile, set_active_profile

CLOUD = "https://backend.example.invalid/functions/v1"
GATEWAY = f"{CLOUD}/llm/v1"
ACCOUNT_KEY = "slk_account_key_for_tests_0123456789"
# Any ids the image service names: the engine only passes them on.
ANSWER = {
    "available": True,
    "models": {"generate": "picture-maker-a", "edit": "picture-editor-b"},
    "qualities": ["low", "medium", "high", "xhigh", "max"],
}


@pytest.fixture(autouse=True)
def hosted(monkeypatch, tmp_path):
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ALYSIS_CLOUD_API_URL", CLOUD)
    monkeypatch.setenv("ALYSIS_GATEWAY_URL", GATEWAY)
    forget_account_answers()
    yield
    forget_account_answers()


def answered(answer):
    calls: list[str] = []

    def fetch(key: str):
        calls.append(key)
        return answer

    return fetch, calls


def test_the_account_declares_its_models_and_qualities_and_keeps_the_persons_setting():
    person = ImageGenerationConfig(quality="max", timeout_s=240, max_images_per_call=2)
    declared = declared_by_account(ANSWER, person)

    assert declared is not None
    assert declared.enabled is True
    assert (declared.model, declared.edit_model) == ("picture-maker-a", "picture-editor-b")
    assert declared.qualities == ("low", "medium", "high", "xhigh", "max")
    assert declared.base_url == f"{CLOUD}/images/v1"
    assert declared.key_from == "connection" and declared.api_key_env is None
    # The person's own choices stay theirs.
    assert (declared.quality, declared.timeout_s, declared.max_images_per_call) == ("max", 240, 2)


@pytest.mark.parametrize(
    "answer",
    [
        None,
        {"available": False, "reason": "plan"},
        {**ANSWER, "available": "yes"},
        {**ANSWER, "models": {"edit": "picture-editor-b"}},
        {**ANSWER, "models": {"generate": "two words"}},
        {**ANSWER, "models": {"generate": "picture-maker-a", "edit": ""}},
        {**ANSWER, "qualities": []},
        {**ANSWER, "qualities": ["ultra", "sharpest"]},
    ],
)
def test_no_pictures_when_the_account_declares_none_or_answers_badly(answer):
    assert declared_by_account(answer, ImageGenerationConfig()) is None


def test_unknown_qualities_are_left_out_and_one_model_serves_both_without_an_edit_model():
    declared = declared_by_account(
        {"available": True, "models": {"generate": "m"}, "qualities": ["HIGH", "ultra", "low"]},
        ImageGenerationConfig(),
    )
    assert declared is not None
    assert declared.qualities == ("high", "low")
    assert declared.edit_model is None


def test_the_answer_is_read_once_per_key_while_it_is_kept():
    fetch, calls = answered(ANSWER)
    assert account_answer(ACCOUNT_KEY, fetch=fetch, now=1000.0) == ANSWER
    assert account_answer(ACCOUNT_KEY, fetch=fetch, now=1000.0 + 599) == ANSWER
    assert account_answer("slk_another_key_0123456789", fetch=fetch, now=1001.0) == ANSWER
    assert calls == [ACCOUNT_KEY, "slk_another_key_0123456789"]
    # Kept no longer than _KEEP_S, and a missing answer is kept too (no request per turn).
    account_answer(ACCOUNT_KEY, fetch=fetch, now=1000.0 + 601)
    assert calls[-1] == ACCOUNT_KEY and len(calls) == 3


def test_which_declaration_a_connection_uses():
    fetch, _ = answered(ANSWER)
    own_provider = ImageGenerationConfig(enabled=True, base_url="https://pictures.example.test/v1")
    person_on = ImageGenerationConfig(enabled=True)
    person_off = ImageGenerationConfig()

    # 1. The person's own image provider wins everywhere.
    assert (
        connection_image_generation(
            own_provider, base_url=GATEWAY, api_key=ACCOUNT_KEY, fetch=fetch
        )
        is own_provider
    )
    # 2. On the account gateway: what the account declares, whatever the person's switch says.
    for person in (person_on, person_off):
        declared = connection_image_generation(
            person, base_url=GATEWAY, api_key=ACCOUNT_KEY, fetch=fetch
        )
        assert declared is not None and declared.model == "picture-maker-a"
    assert (
        connection_image_generation(person_on, base_url=GATEWAY, api_key=None, fetch=fetch) is None
    )
    # 3. Elsewhere: the person's settings, for a connection the request can reach.
    other = "https://models.example.test/v1"
    assert connection_image_generation(person_on, base_url=other, api_key="k") is person_on
    assert (
        connection_image_generation(
            person_on, base_url=other, api_key="k", protocol="anthropic_messages"
        )
        is None
    )
    assert connection_image_generation(person_off, base_url=other, api_key="k") is None


def test_signed_in_the_account_declares_pictures_for_every_other_connection():
    """Signed in to Alysis, the person makes pictures with their account whatever model they
    think with: their own provider's key, a plan subscription, another protocol."""
    save_persisted_profile_key(alysis_cloud.PROFILE_KEY, ACCOUNT_KEY)
    fetch, calls = answered(ANSWER)
    other = "https://models.example.test/v1"
    person_on = ImageGenerationConfig(enabled=True)
    for protocol in ("openai_compat", "anthropic_messages", "openai_responses"):
        declared = connection_image_generation(
            ImageGenerationConfig(quality="high"),
            base_url=other,
            api_key="sk-their-own-provider",
            protocol=protocol,
            fetch=fetch,
        )
        assert declared is not None and declared.model == "picture-maker-a"
        # Only the account key goes to the account's service, never the connection's.
        assert declared.key_from == "account" and declared.quality == "high"
    assert calls == [ACCOUNT_KEY]
    # The person's own image settings, where the request can reach them, still come first.
    assert connection_image_generation(person_on, base_url=other, api_key="k") is person_on
    declared = connection_image_generation(
        person_on, base_url=other, api_key="k", protocol="anthropic_messages", fetch=fetch
    )
    assert declared is not None and declared.key_from == "account"


def test_signed_out_or_without_pictures_no_account_declaration_elsewhere():
    fetch, calls = answered(ANSWER)
    other = "https://models.example.test/v1"
    assert (
        connection_image_generation(
            ImageGenerationConfig(), base_url=other, api_key="k", fetch=fetch
        )
        is None
    )
    assert calls == []
    save_persisted_profile_key(alysis_cloud.PROFILE_KEY, ACCOUNT_KEY)
    fetch, _ = answered({"available": False})
    assert (
        connection_image_generation(
            ImageGenerationConfig(), base_url=other, api_key="k", fetch=fetch
        )
        is None
    )


def test_an_account_without_pictures_declares_none_on_the_gateway():
    fetch, _ = answered({"available": False, "reason": "plan"})
    assert (
        connection_image_generation(
            ImageGenerationConfig(enabled=True), base_url=GATEWAY, api_key=ACCOUNT_KEY, fetch=fetch
        )
        is None
    )


def account_session_config(**image_settings) -> AppConfig:
    profile = replace(make_profile_from_preset(get_preset("alysis")), default_model="gpt-6-luna")
    cfg = AppConfig(
        model="gpt-6-luna", base_url=profile.base_url, image_generation=image_settings or {}
    )
    add_profile(cfg, profile)
    set_active_profile(cfg, profile.name)
    return cfg


def test_a_chat_on_the_account_uses_what_the_account_declares(monkeypatch):
    # The account key saved for the gateway connection.
    monkeypatch.setattr(
        config_module,
        "resolve_profile_api_key",
        lambda *_args: ApiKeyResolution(key=ACCOUNT_KEY, source="stored"),
    )
    fetch, calls = answered(ANSWER)
    cfg = account_session_config(quality="low")
    declared = session_image_generation(cfg, fetch=fetch)
    assert declared.enabled and declared.model == "picture-maker-a"
    assert declared.quality == "low"
    assert calls == [ACCOUNT_KEY]

    forget_account_answers()
    fetch, _ = answered({"available": False})
    assert (
        session_image_generation(account_session_config(enabled=True), fetch=fetch).enabled is False
    )


def test_a_chat_elsewhere_keeps_the_persons_settings():
    save_persisted_profile_key(alysis_cloud.PROFILE_KEY, ACCOUNT_KEY)
    fetch, calls = answered(ANSWER)
    cfg = AppConfig(
        model="m", base_url="https://models.example.test/v1", image_generation={"enabled": True}
    )
    assert session_image_generation(cfg, fetch=fetch) is cfg.image_generation
    assert calls == []


def test_a_chat_elsewhere_signed_in_uses_the_accounts_pictures():
    other = "https://models.example.test/v1"
    fetch, calls = answered(ANSWER)
    cfg = AppConfig(model="m", base_url=other, image_generation={"quality": "low"})
    # Signed out: the settings as they are (off).
    assert session_image_generation(cfg, fetch=fetch) is cfg.image_generation
    assert calls == []
    save_persisted_profile_key(alysis_cloud.PROFILE_KEY, ACCOUNT_KEY)
    declared = session_image_generation(cfg, fetch=fetch)
    assert declared.enabled and declared.key_from == "account"
    assert declared.quality == "low"
    assert calls == [ACCOUNT_KEY]


def test_the_models_pictures_are_made_with_are_named_for_model_pickers_to_leave_out():
    fetch, _ = answered(ANSWER)
    assert picture_models(ACCOUNT_KEY, fetch=fetch) == {"picture-maker-a", "picture-editor-b"}
    forget_account_answers()
    for answer in (None, {"available": False}, {"available": True, "models": "x"}):
        fetch, _ = answered(answer)
        assert picture_models(ACCOUNT_KEY, fetch=fetch) == frozenset()
        forget_account_answers()


def test_the_image_service_sits_beside_the_gateway(monkeypatch):
    from alysis_code import alysis_cloud

    assert alysis_cloud.images_base_url() == f"{CLOUD}/images/v1"
    monkeypatch.setenv("ALYSIS_GATEWAY_URL", "https://other.example.invalid/functions/v1/llm/v1/")
    assert alysis_cloud.images_base_url() == "https://other.example.invalid/functions/v1/images/v1"
    # A gateway on a host of its own: the deployment's images function.
    monkeypatch.setenv("ALYSIS_GATEWAY_URL", "https://gateway.example.invalid/v1")
    assert alysis_cloud.images_base_url() == f"{CLOUD}/images/v1"


def test_the_service_may_allow_fewer_pictures_per_request_and_need_longer():
    person = ImageGenerationConfig(max_images_per_call=4, timeout_s=180)
    declared = declared_by_account({**ANSWER, "max_images_per_call": 1, "timeout_s": 300}, person)
    assert declared is not None
    assert (declared.max_images_per_call, declared.timeout_s) == (1, 300)
    # Never more than the person allows, never a shorter wait than theirs, never past 900 s.
    declared = declared_by_account({**ANSWER, "max_images_per_call": 9, "timeout_s": 5000}, person)
    assert (declared.max_images_per_call, declared.timeout_s) == (4, 900)
    declared = declared_by_account({**ANSWER, "timeout_s": 60}, person)
    assert (declared.max_images_per_call, declared.timeout_s) == (4, 180)


def test_the_apps_model_list_leaves_out_the_models_pictures_are_made_with(monkeypatch):
    from alysis_code import account_images, account_login

    billing = {
        "paid": {
            "credit_unit_version": "paid-base-display-v1",
            "models": [{"model": "thinker"}, {"model": "picture-maker-a"}],
        }
    }
    monkeypatch.setattr(
        account_login, "load_persisted_profile_keys", lambda: {"alysis": ACCOUNT_KEY}
    )
    monkeypatch.setattr(account_login, "gateway_models", lambda key=None: ["thinker"])
    monkeypatch.setattr(
        account_images, "picture_models", lambda key: frozenset({"picture-maker-a"})
    )
    result = account_login.billing_with_model_availability(AppConfig(), billing)
    assert [(m["model"], m["available"]) for m in result["paid"]["models"]] == [("thinker", True)]
    assert len(billing["paid"]["models"]) == 2
