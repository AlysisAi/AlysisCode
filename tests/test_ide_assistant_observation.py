from __future__ import annotations

import base64
import io
import json
from dataclasses import replace

import httpx
import pytest
from PIL import Image

from alysis_code.config import AppConfig
from alysis_code.ide import assistant_observation as observation
from alysis_code.ide import management_protocol
from alysis_code.ide.health import SUPPORTED_METHODS
from alysis_code.ide.protocol import ProtocolError
from alysis_code.profile_presets import get_preset, make_profile_from_preset
from alysis_code.profiles import add_profile, set_active_profile

ACTIVITY = {"trigger": "repeated-switching", "sequence": [{"app": 1, "seconds": 5}]}


@pytest.fixture
def profiles(monkeypatch):
    cfg = AppConfig()
    plan = replace(
        make_profile_from_preset(get_preset("zai-coding-plan"), name="zai-coding-plan"),
        default_model="glm-5.3-flash",
    )
    standard = make_profile_from_preset(get_preset("zai"), name="zai")
    for profile in (plan, standard):
        add_profile(cfg, profile)
    set_active_profile(cfg, plan.name)
    keys = {plan.name: "fixture-plan-key", standard.name: "fixture-standard-key"}
    monkeypatch.setattr(observation, "load_config", lambda: cfg.model_copy(deep=True))
    monkeypatch.setattr("alysis_code.config.load_persisted_profile_keys", lambda: keys.copy())
    return cfg, keys, plan, standard


def request(route, phase, **params):
    return {"route": route, "phase": phase, "activity": ACTIVITY, **params}


def image_base64(format="JPEG", size=(18, 9)):
    with io.BytesIO() as output:
        Image.new("RGB", size, (30, 60, 90)).save(output, format=format)
        return base64.b64encode(output.getvalue()).decode("ascii")


def completion(result, **message):
    return httpx.Response(
        200,
        json={
            "choices": [
                {"message": {"content": json.dumps(result), **message}, "finish_reason": "stop"}
            ]
        },
    )


def test_management_discovery_marks_the_configured_profile_and_model(profiles):
    assert {"assistant.observation.models", "assistant.observation.infer"} <= set(SUPPORTED_METHODS)
    discovery = management_protocol.handle_management_method(
        "assistant.observation.models", {}, request_id="models"
    )
    options = discovery["models"]
    assert discovery["modelStatus"] == "ready"
    active = next(item for item in options if item["id"] == discovery["activeModel"])
    assert active == {
        "id": active["id"],
        "name": "GLM 5.3 Flash",
        "provider": "Z.AI GLM Coding Plan",
        "hosted": True,
    }
    assert len({row["provider"] for row in options}) == 2
    assert len({row["id"] for row in options}) == len(options)
    assert all(row["name"] != "GLM 5.3" for row in options)
    assert not any(secret in repr(options) for secret in profiles[1].values())
    cfg, _, _, standard = profiles
    set_active_profile(cfg, standard.name)
    assert observation.models({}, "standard")["activeModel"] != active["id"]
    add_profile(cfg, replace(standard, default_model="glm-5.3"))
    discovery = observation.models({}, "text-only")
    assert discovery["activeModel"] is None
    assert discovery["modelStatus"] == "no-vision"
    assert discovery["models"]


@pytest.mark.parametrize("format", ["JPEG", "PNG"])
def test_exact_coding_plan_check_and_suggest_wire_contract(profiles, format):
    _, keys, plan, _ = profiles
    route = observation.models({}, "models")["activeModel"]
    calls = []
    image = image_base64(format)
    offer = {"title": "整理库存", "outcome": "生成一份库存差异表。"}

    def handle(req):
        assert str(req.url) == f"{plan.base_url}/chat/completions"
        assert req.headers["authorization"] == f"Bearer {keys[plan.name]}"
        body = json.loads(req.content)
        assert body["model"] == "glm-5.3-flash"
        assert body["reasoning_effort"] == "low"
        assert not body.get("stream", False)
        assert "tools" not in body
        calls.append(body["messages"][-1]["content"])
        if len(calls) == 1:
            assert json.loads(calls[0]) == {"activity": ACTIVITY}
            assert "data:image/" not in repr(body)
            return completion({"check": True})
        text, pixels = calls[1]
        assert json.loads(text["text"]) == {"activity": ACTIVITY, "replyLanguage": "zh-CN"}
        assert pixels == {
            "type": "image_url",
            "image_url": {"url": f"data:image/{format.lower()};base64,{image}"},
        }
        return completion({"offer": offer})

    bridge_transport = httpx.MockTransport(handle)
    assert observation.infer(request(route, "check"), "check", transport=bridge_transport) == {
        "check": True
    }
    assert observation.infer(
        request(route, "suggest", language="zh-CN", imageBase64=image),
        "suggest",
        transport=bridge_transport,
    ) == {"offer": offer}
    assert len(calls) == 2


def test_signed_in_alysis_account_uses_its_own_glm_vision_route(profiles):
    cfg, keys, _, _ = profiles
    alysis = make_profile_from_preset(get_preset("alysis"), name="alysis")
    add_profile(cfg, alysis)
    keys[alysis.name] = "fixture-saved-device-key"
    routes = observation.models({}, "models")["models"]
    route = next(
        item
        for item in routes
        if item["provider"] == "Alysis Code" and item["name"] == "GLM 5.3 Flash"
    )
    assert route["hosted"] is True
    image = image_base64()
    offer = {
        "title": "Organiser les rapports",
        "outcome": "Créer une liste des rapports en attente.",
    }

    def handle(req):
        assert str(req.url) == f"{alysis.base_url}/chat/completions"
        assert req.headers["authorization"] == f"Bearer {keys[alysis.name]}"
        body = json.loads(req.content)
        assert body["model"] == "glm-5.3-flash"
        assert "tools" not in body
        assert (
            body["messages"][-1]["content"][1]["image_url"]["url"]
            == f"data:image/jpeg;base64,{image}"
        )
        return completion({"offer": offer})

    assert observation.infer(
        request(route["id"], "suggest", language="fr", imageBase64=image),
        "alysis-model",
        transport=httpx.MockTransport(handle),
    ) == {"offer": offer}


def test_a_requested_screen_check_sends_one_image_without_activity_timings(profiles):
    route = observation.models({}, "models")["activeModel"]
    manual = {"trigger": "user-request", "sequence": []}
    calls = []
    offer = {"title": "Revoir le rapport", "outcome": "Je peux relever les écarts à expliquer."}

    def handle(req):
        body = json.loads(req.content)
        assert "explicitly asked for help" in body["messages"][0]["content"]
        text, pixels = body["messages"][-1]["content"]
        assert json.loads(text["text"]) == {"activity": manual, "replyLanguage": "fr"}
        assert pixels["type"] == "image_url"
        calls.append(req)
        return completion({"offer": offer})

    transport = httpx.MockTransport(handle)
    with pytest.raises(ProtocolError, match="Invalid Assist observation request"):
        observation.infer(
            request(route, "check", activity=manual), "manual-check", transport=transport
        )
    assert not calls
    result = observation.infer(
        request(route, "suggest", activity=manual, imageBase64=image_base64(), language="fr"),
        "manual-suggest",
        transport=transport,
    )
    assert result == {"offer": offer}
    assert len(calls) == 1


def test_watch_review_carries_goal_frame_order_and_only_accepts_available_owls(profiles):
    cfg, keys, _, _ = profiles
    profile = replace(
        make_profile_from_preset(get_preset("alysis"), name="alysis"), default_model="glm-5.3-flash"
    )
    add_profile(cfg, profile)
    set_active_profile(cfg, profile.name)
    keys[profile.name] = "fixture-active-key"
    route = observation.models({}, "watch-model")["activeModel"]
    candidate = {"id": "c" * 32, "name": "仕入れ確認", "purpose": "入荷状況を確認する"}
    images = [image_base64(size=(18, 9)), image_base64(size=(20, 10))]
    params = {
        "route": route,
        "phase": "watch",
        "activity": {"trigger": "user-request", "sequence": []},
        "language": "ja",
        "frames": images,
        "goal": "入荷記録を確認したい",
        "notes": "",
        "canExtend": True,
        "candidates": [candidate],
    }
    reason = (
        "入荷記録の表示が切り替わるところを確認します。"
        "数量欄が空白の場合と、記入された数量が納品書と一致しない場合とで、"
        "担当者がどのように印を付けているかを次の画面で見たいです。"
        "確定済みの行はそのままにして、担当者に確認を依頼するために必要な項目だけを"
        "抜き出せるかどうかを確認します。"
        "確認対象を誤って広げないよう、画面に表示される対応状況の欄も見ます。"
        "この手順を見届ければ、すでに確認が済んだ行と再確認が必要な行を区別できます。"
    )
    assert 200 < len(reason) <= observation._EXTENSION_LIMITS["reason"]
    more = {"more": {"reason": reason, "notes": "確認対象の記録が表示されています。"}}
    draft = {
        "recipientId": candidate["id"],
        "reason": "入荷状況を確認する担当です。",
        "brief": "入荷記録の不明点を整理してください。",
        "expectedResult": "確認項目の一覧",
    }
    result = {"offer": {"title": "入荷確認", "outcome": "一覧を作ります。", "existingOwl": draft}}

    def handle(req):
        assert str(req.url) == f"{profile.base_url}/chat/completions"
        text, *frames = json.loads(req.content)["messages"][-1]["content"]
        assert json.loads(text["text"]) == {
            "activity": params["activity"],
            "replyLanguage": "ja",
            "owls": [candidate],
            "goal": params["goal"],
            "notes": params["notes"],
            "canExtend": params["canExtend"],
        }
        assert [frame["image_url"]["url"] for frame in frames] == [
            f"data:image/jpeg;base64,{image}" for image in images
        ]
        return completion(more if params["canExtend"] else result)

    assert observation.infer(params, "more", transport=httpx.MockTransport(handle)) == more
    params.update(notes=more["more"]["notes"], canExtend=False)
    assert observation.infer(params, "match", transport=httpx.MockTransport(handle)) == result
    sibling_draft = {
        "offer": {"title": "入荷確認", "outcome": "一覧を作ります。"},
        "existingOwl": draft,
    }
    assert (
        observation.infer(
            params,
            "provider-layout",
            transport=httpx.MockTransport(lambda _: completion(sibling_draft)),
        )
        == result
    )
    with pytest.raises(ProtocolError) as ambiguous:
        observation.infer(
            params,
            "ambiguous-layout",
            transport=httpx.MockTransport(lambda _: completion({**result, "existingOwl": draft})),
        )
    assert ambiguous.value.code == observation.INVALID_REPLY
    with pytest.raises(ProtocolError) as retry:
        observation.infer(
            params, "limit", transport=httpx.MockTransport(lambda _: completion(more))
        )
    assert retry.value.code == observation.INVALID_REPLY
    draft["recipientId"] = "e" * 32
    with pytest.raises(ProtocolError) as unknown:
        observation.infer(params, "unknown", transport=httpx.MockTransport(handle))
    assert unknown.value.code == observation.INVALID_REPLY
    set_active_profile(cfg, profiles[2].name)
    with pytest.raises(ProtocolError) as changed:
        observation.infer(params, "switched", transport=httpx.MockTransport(handle))
    assert changed.value.code == observation.ROUTE_CHANGED


@pytest.mark.parametrize("change", ["key", "endpoint", "headers"])
def test_a_saved_route_change_fails_before_any_provider_request(profiles, change):
    cfg, keys, plan, standard = profiles
    route = observation.models({}, "models")["activeModel"]
    if change == "key":
        keys[plan.name] = "fixture-rotated-key"
    elif change == "endpoint":
        add_profile(cfg, replace(plan, base_url=standard.base_url))
    else:
        add_profile(cfg, replace(plan, extra_headers={"x-account": "other"}))

    def unexpected(_):
        pytest.fail("an unapproved route reached the transport")

    with pytest.raises(ProtocolError) as exc:
        observation.infer(
            request(route, "check"), "changed", transport=httpx.MockTransport(unexpected)
        )
    assert exc.value.code == observation.ROUTE_CHANGED


def test_rejects_invalid_frames_and_does_not_retry_provider_errors(profiles):
    route = observation.models({}, "models")["activeModel"]
    calls = []

    def failing(req):
        calls.append(req)
        return httpx.Response(503, text="provider-private-error-body")

    transport = httpx.MockTransport(failing)
    for params in [
        request(route, "check", imageBase64=image_base64()),
        request(route, "suggest"),
        request(route, "suggest", imageBase64=image_base64(size=(2049, 2))),
        request(route, "suggest", imageBase64=base64.b64encode(b"not an image").decode()),
        request(route, "suggest", imageBase64="A" * (1024 * 1024)),
        request(route, "check", activity={**ACTIVITY, "screen_title": "private"}),
    ]:
        with pytest.raises(ProtocolError) as exc:
            observation.infer(params, "invalid", transport=transport)
        assert exc.value.code == "invalid_params"
    assert calls == []

    with pytest.raises(ProtocolError) as exc:
        observation.infer(
            request(route, "suggest", imageBase64=image_base64()), "fail", transport=transport
        )
    assert exc.value.code == "assistant_observation_unavailable"
    assert "provider-private-error-body" not in str(exc.value)
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("phase", "decline", "invalid"),
    [
        ("check", {"check": False}, {"check": "false"}),
        ("suggest", {"offer": None}, {"offer": {"title": "整理库存"}}),
    ],
)
def test_malformed_reply_is_distinct_from_a_valid_decline(profiles, phase, decline, invalid):
    route = observation.models({}, "models")["activeModel"]
    params = request(route, phase)
    if phase == "suggest":
        params["imageBase64"] = image_base64()

    declined = observation.infer(
        params, "decline", transport=httpx.MockTransport(lambda _: completion(decline))
    )
    assert declined == decline
    for reply in (completion(invalid), completion(None, content="")):
        with pytest.raises(ProtocolError) as exc:
            observation.infer(
                params, "invalid", transport=httpx.MockTransport(lambda _, reply=reply: reply)
            )
        assert exc.value.code == observation.INVALID_REPLY


def test_suggestion_can_carry_a_reviewable_owl_setup_without_credential_leakage(profiles):
    chosen = observation.models({}, "models")["activeModel"]
    params = request(chosen, "suggest", language="el", imageBase64=image_base64())
    draft = {
        "name": "Έλεγχος βαρδιών",
        "purpose": "Εντοπισμός κενών στην κάλυψη.",
        "instructions": "Γράψε σύντομη αναφορά.",
        "brief": "Αναφέρεται μία βάρδια χωρίς ανάθεση.",
        "expectedResult": "Μία γραμμή για το κενό.",
    }
    offer = {"title": "Έλεγχος κάλυψης", "outcome": "Αναφορά για τη βάρδια.", "newOwl": draft}
    valid = observation.infer(
        params, "offer", transport=httpx.MockTransport(lambda _: completion({"offer": offer}))
    )
    assert valid == {"offer": offer}
    secret = profiles[1][profiles[2].name]
    draft["brief"] = secret
    with pytest.raises(ProtocolError) as exc:
        observation.infer(
            params,
            "rejected",
            transport=httpx.MockTransport(lambda _: completion({"offer": offer})),
        )
    assert exc.value.code == observation.INVALID_REPLY


def test_background_suggest_asks_a_question_before_proposing_work(profiles):
    route = observation.models({}, "models")["activeModel"]
    params = request(
        route, "suggest", language="el", imageBase64=image_base64(), askBeforePlanning=True
    )
    offer = {
        "title": "Σύγκριση διαδρομών",
        "outcome": "Σύγκρινε χρόνο και κόστος για τις διαθέσιμες διαδρομές.",
        "question": "Σε ενδιαφέρει περισσότερο ο χρόνος ή το κόστος;",
    }
    for payload in ({"offer": offer}, {"offer": None}):
        result = observation.infer(
            params,
            "offer",
            transport=httpx.MockTransport(lambda _, payload=payload: completion(payload)),
        )
        assert result == payload
    with pytest.raises(ProtocolError) as exc:
        observation.infer(
            params,
            "draft",
            transport=httpx.MockTransport(
                lambda _: completion({"offer": {**offer, "newOwl": {"name": "Unexpected draft"}}})
            ),
        )
    assert exc.value.code == observation.INVALID_REPLY


def test_one_snapshot_can_ask_for_recording_without_requesting_tools(profiles):
    discovery = observation.models({}, "models")
    assert discovery["snapshotPlanning"] is True
    image = image_base64()
    goal = "Δες πώς αλλάζει ο πίνακας όταν εγκρίνω μια γραμμή."
    params = request(
        discovery["activeModel"],
        "snapshot",
        activity={"trigger": "user-request", "sequence": []},
        frames=[image],
        language="el",
        goal=goal,
        canExtend=True,
    )
    answer = {
        "more": {
            "reason": "Μπορώ να δω τι αλλάζει μετά την έγκριση;",
            "notes": "Ο πίνακας δείχνει τη γραμμή πριν από την έγκριση.",
        }
    }
    calls = []

    def handle(req):
        body = json.loads(req.content)
        calls.append(body)
        assert "tools" not in body
        message, image_part = body["messages"][-1]["content"]
        assert json.loads(message["text"])["goal"] == goal
        assert image_part["image_url"]["url"].endswith(image)
        return completion(answer)

    transport = httpx.MockTransport(handle)
    assert observation.infer(params, "snapshot", transport=transport) == answer
    assert len(calls) == 1
    for invalid in ({**params, "frames": [image, image]}, {**params, "notes": "Previous frames"}):
        with pytest.raises(ProtocolError) as exc:
            observation.infer(invalid, "invalid", transport=transport)
        assert exc.value.code == "invalid_params"
    assert len(calls) == 1
    offer = {
        "offer": {
            "title": "Έγκριση γραμμής",
            "outcome": "Μπορώ να ελέγξω τη ροή.",
            "question": "Ποια γραμμή θέλεις να ελέγξουμε;",
        }
    }
    assert (
        observation.infer(
            params, "question", transport=httpx.MockTransport(lambda _: completion(offer))
        )
        == offer
    )
    with pytest.raises(ProtocolError) as exc:
        observation.infer({**params, "canExtend": False}, "no-extension", transport=transport)
    assert exc.value.code == observation.INVALID_REPLY


@pytest.mark.parametrize("phase", ["snapshot", "watch"])
@pytest.mark.parametrize("layout", ["nested", "alongside", "separate-draft"])
def test_screen_check_drafts_only_a_work_scope_supported_by_the_owls_service(
    profiles, layout, phase
):
    route = observation.models({}, "models")["activeModel"]
    params = request(
        route,
        phase,
        activity={"trigger": "user-request", "sequence": []},
        frames=[image_base64()],
        goal="Prépare un classeur des inscriptions.",
        workScopes=["workbook"],
    )
    draft = {
        "name": "Registre",
        "purpose": "Préparer les listes d’inscription.",
        "instructions": "Utilise le format demandé.",
        "brief": "Prépare le classeur avec les colonnes atelier et capacité.",
        "expectedResult": "Un classeur .xlsx avec les deux colonnes.",
        "workScope": "workbook",
    }
    payload = {
        "offer": {"title": "Liste d’inscription", "outcome": "Créer un classeur.", "newOwl": draft}
    }
    shown_offer = payload["offer"]
    if layout != "nested":
        shown_offer = {
            **shown_offer,
            "newOwl": {key: value for key, value in draft.items() if key != "workScope"},
            "workScope": "workbook",
        }
    if layout == "separate-draft":
        model_reply = {
            "offer": {key: value for key, value in shown_offer.items() if key != "newOwl"},
            "newOwl": shown_offer["newOwl"],
        }
    else:
        model_reply = {"offer": shown_offer}
    transport = httpx.MockTransport(lambda _: completion(model_reply))
    assert observation.infer(params, "workbook", transport=transport) == payload
    params["workScopes"] = ["web_research"]
    with pytest.raises(ProtocolError) as exc:
        observation.infer(params, "unavailable", transport=transport)
    assert exc.value.code == observation.INVALID_REPLY


@pytest.mark.parametrize("problem", ["duplicate", "no_draft", "two_drafts", "invalid_recipient"])
def test_screen_check_rejects_an_ambiguous_work_scope(profiles, problem):
    route = observation.models({}, "models")["activeModel"]
    recipient_id = "1" * 32
    params = request(
        route,
        "watch",
        activity={"trigger": "user-request", "sequence": []},
        frames=[image_base64()],
        workScopes=["web_research", "workbook"],
        candidates=[{"id": recipient_id, "name": "Archivio", "purpose": "Organizzare registri"}],
    )
    offer = {
        "title": "Registro libri",
        "outcome": "Crea un foglio di lavoro.",
        "workScope": "workbook",
    }
    draft = {
        "name": "Inventario",
        "purpose": "Organizzare i registri della biblioteca.",
        "instructions": "Mantieni i nomi dei libri.",
        "brief": "Prepara un foglio di inventario.",
        "expectedResult": "Un file Excel.",
    }
    existing = {
        "recipientId": recipient_id,
        "reason": "Organizza registri.",
        "brief": draft["brief"],
        "expectedResult": draft["expectedResult"],
    }
    if problem == "duplicate":
        offer["newOwl"] = {**draft, "workScope": "web_research"}
    elif problem == "two_drafts":
        offer.update(newOwl=draft, existingOwl=existing)
    elif problem == "invalid_recipient":
        offer["existingOwl"] = {**existing, "recipientId": "2" * 32}
    with pytest.raises(ProtocolError) as exc:
        observation.infer(
            params,
            "invalid-scope",
            transport=httpx.MockTransport(lambda _: completion({"offer": offer})),
        )
    assert exc.value.code == observation.INVALID_REPLY


def _teach_params(route, **extra):
    return {
        "route": route,
        "phase": "teach",
        "activity": {"trigger": "user-request", "sequence": []},
        "language": "el",
        "frames": [image_base64(size=(18, 9)), image_base64(size=(20, 10))],
        "goal": "Έτσι περνάω τις παραγγελίες στο φύλλο",
        **extra,
    }


def test_a_recording_teaches_numbered_steps_with_a_handoff(profiles):
    route = observation.models({}, "teach-model")["activeModel"]
    candidate = {"id": "c" * 32, "name": "Παραγγελίες", "purpose": "Οργανώνει παραγγελίες"}
    params = _teach_params(route, candidates=[candidate], workScopes=["workbook"])
    taught = {
        "teach": {
            "title": "Καταχώριση παραγγελιών",
            "outcome": "Ένα φύλλο με τις παραγγελίες της ημέρας.",
            "steps": [
                {"text": "Άνοιξε το email με τη νέα παραγγελία.", "frame": 0},
                {"text": "Αντέγραψε   τον κωδικό και την ποσότητα\nστη νέα γραμμή του φύλλου."},
                {"text": "Αποθήκευσε το φύλλο.", "frame": 7},
            ],
            "question": "Από ποιο φάκελο έρχονται οι παραγγελίες;",
            "existingOwl": {
                "recipientId": candidate["id"],
                "reason": "Οργανώνει παραγγελίες.",
                "brief": "Φτιάξε το φύλλο παραγγελιών.",
                "expectedResult": "Ένα φύλλο .xlsx",
                "workScope": "workbook",
            },
        }
    }

    def handle(req):
        body = json.loads(req.content)
        assert body["max_tokens"] == 4096
        system, user = body["messages"]
        assert "recorded their screen while doing a task once" in system["content"]
        text, *frames = user["content"]
        assert json.loads(text["text"]) == {
            "activity": params["activity"],
            "replyLanguage": "el",
            "owls": [candidate],
            "workScopes": ["workbook"],
            "goal": params["goal"],
            "images": 2,
        }
        assert len(frames) == 2
        return completion(taught)

    result = observation.infer(params, "teach", transport=httpx.MockTransport(handle))
    steps = result["teach"]["steps"]
    # Whitespace is folded, and a picture number that names no image is dropped.
    assert steps == [
        {"text": "Άνοιξε το email με τη νέα παραγγελία.", "frame": 0},
        {"text": "Αντέγραψε τον κωδικό και την ποσότητα στη νέα γραμμή του φύλλου."},
        {"text": "Αποθήκευσε το φύλλο."},
    ]
    assert result["teach"]["question"] == "Από ποιο φάκελο έρχονται οι παραγγελίες;"
    assert result["teach"]["existingOwl"]["workScope"] == "workbook"
    nothing = observation.infer(
        params, "nothing", transport=httpx.MockTransport(lambda _: completion({"teach": None}))
    )
    assert nothing == {"teach": None}


@pytest.mark.parametrize(
    "problem",
    ["no steps", "too many steps", "long step", "long title"],
)
def test_a_taught_reply_that_breaks_its_limits_is_unusable(profiles, problem):
    route = observation.models({}, "teach-limits")["activeModel"]
    params = _teach_params(route)
    teach = {"title": "Παραγγελίες", "outcome": "Ένα φύλλο.", "steps": [{"text": "Άνοιξε."}]}
    if problem == "no steps":
        teach["steps"] = []
    elif problem == "too many steps":
        teach["steps"] = [{"text": f"Βήμα {n}"} for n in range(13)]
    elif problem == "long step":
        teach["steps"] = [{"text": "α" * 301}]
    else:
        teach["title"] = "τ" * 81
    with pytest.raises(ProtocolError) as unusable:
        observation.infer(
            params,
            problem,
            transport=httpx.MockTransport(lambda _: completion({"teach": teach})),
        )
    assert unusable.value.code == observation.INVALID_REPLY


def test_teaching_refuses_too_many_frames_before_any_provider_request(profiles):
    route = observation.models({}, "teach-frames")["activeModel"]

    def refuse(_):
        raise AssertionError("No provider request is expected")

    for frames in ([], [image_base64()] * (observation.MAX_TEACH_FRAMES + 1)):
        with pytest.raises(ProtocolError) as invalid:
            observation.infer(
                _teach_params(route, frames=frames), "frames", transport=httpx.MockTransport(refuse)
            )
        assert invalid.value.code == "invalid_params"
    with pytest.raises(ProtocolError) as notes:
        observation.infer(
            _teach_params(route, notes="earlier"), "notes", transport=httpx.MockTransport(refuse)
        )
    assert notes.value.code == "invalid_params"


def test_a_recording_can_hand_the_task_to_an_owl_as_work_on_its_own_computer(profiles):
    route = observation.models({}, "teach-computer")["activeModel"]
    candidate = {
        "id": "c" * 32,
        "name": "Παραγγελίες",
        "purpose": "Οργανώνει παραγγελίες",
        "apps": ["alysis.gmail"],
    }
    params = _teach_params(route, candidates=[candidate], workScopes=["computer"])
    handoff = {
        "recipientId": candidate["id"],
        "reason": "Έχει το Gmail.",
        "brief": "Πέρασε τις παραγγελίες.",
        "expectedResult": "Ένα φύλλο.",
        "workScope": "computer",
        "access": {"offlineComputer": True, "apps": ["alysis.gmail"], "sites": []},
    }
    teach = {"title": "Παραγγελίες", "outcome": "Ένα φύλλο.", "steps": [{"text": "Άνοιξε."}]}

    def reply(existing):
        return httpx.MockTransport(
            lambda _: completion({"teach": {**teach, "existingOwl": existing}})
        )

    result = observation.infer(params, "computer", transport=reply(handoff))
    assert result["teach"]["existingOwl"]["access"] == {
        "offlineComputer": True,
        "onlineBrowser": False,
        "sites": [],
        "apps": ["alysis.gmail"],
    }
    # A computer handoff names what it may use, and nothing but a computer handoff does: one
    # that breaks this is dropped, and only the steps remain.
    for broken in (
        {k: v for k, v in handoff.items() if k != "access"},
        {**handoff, "workScope": "workbook"},
        {**handoff, "access": {"offlineComputer": False}},
        {**handoff, "access": {"everything": True}},
    ):
        assert observation.infer(params, "broken", transport=reply(broken)) == {"teach": teach}


def test_noticed_work_can_repeat_an_earlier_finished_handoff(profiles):
    cfg, keys, _, _ = profiles
    profile = replace(
        make_profile_from_preset(get_preset("alysis"), name="alysis"), default_model="glm-5.3-flash"
    )
    add_profile(cfg, profile)
    set_active_profile(cfg, profile.name)
    keys[profile.name] = "fixture-active-key"
    discovered = observation.models({}, "repeat-model")
    assert discovered["repeatOffers"] is True
    route = discovered["activeModel"]
    owl, other = (
        {"id": "c" * 32, "name": "Παραγγελίες", "purpose": "Περνά παραγγελίες"},
        {
            "id": "d" * 32,
            "name": "Δελτία",
            "purpose": "Γράφει δελτία",
        },
    )
    earlier = [{"id": "a" * 32, "owlId": owl["id"], "result": "Ένα ενημερωμένο φύλλο παραγγελιών."}]
    params = {
        "route": route,
        "phase": "snapshot",
        "activity": {"trigger": "user-request", "sequence": []},
        "language": "el",
        "frames": [image_base64()],
        "goal": "",
        "notes": "",
        "canExtend": False,
        "candidates": [owl, other],
        "earlier": earlier,
    }
    handoff = {
        "recipientId": owl["id"],
        "reason": "Το έκανε και την προηγούμενη φορά.",
        "brief": "Πέρασε τις νέες παραγγελίες στο φύλλο.",
        "expectedResult": "Ένα ενημερωμένο φύλλο.",
    }
    offer = {"title": "Νέες παραγγελίες", "outcome": "Η ίδια δουλειά ξανά.", "existingOwl": handoff}
    seen = []

    def reply(result):
        def handle(req):
            body = json.loads(req.content)
            seen.append(body)
            return completion(result)

        return httpx.MockTransport(handle)

    repeated = {"offer": {**offer, "repeats": "a" * 32}}
    assert observation.infer(params, "repeat", transport=reply(repeated)) == repeated
    beside = {"offer": offer, "repeats": "a" * 32}
    assert observation.infer(params, "beside", transport=reply(beside)) == repeated
    system, user = seen[-1]["messages"]
    assert "earlier list" in system["content"]
    assert json.loads(user["content"][0]["text"])["earlier"] == earlier
    # A repeat that names an unknown task, or goes to another owl, leaves a plain offer.
    for wrong in (
        {"offer": {**offer, "repeats": "b" * 32}},
        {
            "offer": {
                **offer,
                "existingOwl": {**handoff, "recipientId": other["id"]},
                "repeats": "a" * 32,
            }
        },
    ):
        result = observation.infer(params, "wrong", transport=reply(wrong))
        assert "repeats" not in result["offer"]
    # Earlier work of an owl that is not a candidate is refused before any provider request.
    with pytest.raises(ProtocolError) as invalid:
        observation.infer(
            {**params, "earlier": [{**earlier[0], "owlId": "e" * 32}]},
            "invalid",
            transport=reply(repeated),
        )
    assert invalid.value.code == "invalid_params"
    # Teaching reads no earlier work.
    with pytest.raises(ProtocolError):
        observation.infer(
            {
                "route": route,
                "phase": "teach",
                "activity": params["activity"],
                "frames": params["frames"],
                "goal": "",
                "earlier": earlier,
            },
            "teach",
            transport=reply(repeated),
        )


MOMENT = {"kind": "transfer", "count": 4, "windows": 2, "seconds": 240}


def test_a_quick_look_sends_the_moment_and_one_small_picture_and_says_whether_to_ask(profiles):
    discovered = observation.models({}, "models")
    assert discovered["quickLook"] is True
    route = discovered["activeModel"]
    image = image_base64()
    calls = []

    def handle(req):
        body = json.loads(req.content)
        assert "tools" not in body
        assert body["messages"][0]["content"] == observation._GLANCE_PROMPT
        text, pixels = body["messages"][-1]["content"]
        # Counts and timings, with what they mean; no activity timeline, owls or language.
        assert json.loads(text["text"]) == {
            "moment": {**MOMENT, "meaning": observation._MOMENT_WORDS["transfer"]}
        }
        assert pixels == {
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{image}"},
        }
        calls.append(req)
        return completion({"ask": len(calls) == 1})

    transport = httpx.MockTransport(handle)
    params = {"route": route, "phase": "glance", "moment": MOMENT, "imageBase64": image}
    assert observation.infer(params, "yes", transport=transport) == {"ask": True}
    assert len(calls) == 1
    # Each kind of moment goes with its own meaning; a language changes nothing.
    quiet = {**params, "moment": {"kind": "grind", "windows": 1, "seconds": 1800}, "language": "el"}

    def grind(req):
        text, _ = json.loads(req.content)["messages"][-1]["content"]
        assert json.loads(text["text"])["moment"]["meaning"] == observation._MOMENT_WORDS["grind"]
        return completion({"ask": False})

    assert observation.infer(quiet, "no", transport=httpx.MockTransport(grind)) == {"ask": False}


@pytest.mark.parametrize(
    "change",
    [
        {"activity": ACTIVITY},
        {"candidates": []},
        {"imageBase64": None},
        {"imageBase64": image_base64(size=(1025, 9))},
        {"moment": None},
        {"moment": {"kind": "typing", "windows": 1, "seconds": 5}},
        {"moment": {**MOMENT, "title": "Invoices.xlsx"}},
        {"moment": {**MOMENT, "seconds": -1}},
        {"moment": {**MOMENT, "count": True}},
        {"moment": {"kind": "stuck", "windows": 2}},
    ],
)
def test_a_quick_look_refuses_anything_but_a_moment_and_one_small_picture(profiles, change):
    route = observation.models({}, "models")["activeModel"]
    calls = []
    params = {"route": route, "phase": "glance", "moment": MOMENT, "imageBase64": image_base64()}
    params.update(change)
    if params["imageBase64"] is None:
        del params["imageBase64"]
    with pytest.raises(ProtocolError) as exc:
        observation.infer(params, "invalid", transport=httpx.MockTransport(calls.append))
    assert exc.value.code == "invalid_params"
    assert calls == []


def test_a_quick_look_too_large_is_refused_before_any_provider_request(profiles):
    route = observation.models({}, "models")["activeModel"]
    with io.BytesIO() as output:
        Image.effect_noise((1000, 1000), 90).convert("RGB").save(output, format="PNG")
        large = base64.b64encode(output.getvalue()).decode("ascii")
    assert len(large) > ((observation.MAX_GLANCE_BYTES + 2) // 3) * 4
    calls = []
    with pytest.raises(ProtocolError) as exc:
        observation.infer(
            {"route": route, "phase": "glance", "moment": MOMENT, "imageBase64": large},
            "large",
            transport=httpx.MockTransport(calls.append),
        )
    assert exc.value.code == "invalid_params"
    assert calls == []


@pytest.mark.parametrize(
    "reply", [{"ask": "yes"}, {"ask": True, "why": "invoices"}, {"offer": None}]
)
def test_a_quick_look_reply_other_than_yes_or_no_is_unusable(profiles, reply):
    route = observation.models({}, "models")["activeModel"]
    with pytest.raises(ProtocolError) as exc:
        observation.infer(
            {"route": route, "phase": "glance", "moment": MOMENT, "imageBase64": image_base64()},
            "reply",
            transport=httpx.MockTransport(lambda _: completion(reply)),
        )
    assert exc.value.code == observation.INVALID_REPLY


@pytest.mark.parametrize(
    "problem", ["unknown owl", "two handoffs", "handoff in an offer", "long question"]
)
def test_a_lesson_keeps_its_steps_when_its_optional_parts_are_unusable(profiles, problem):
    # A recording is not lost to its handoff or question: they are dropped, and the person
    # picks an owl for the steps themselves.
    route = observation.models({}, "teach-optional")["activeModel"]
    candidate = {"id": "c" * 32, "name": "Παραγγελίες", "purpose": "Οργανώνει παραγγελίες"}
    params = _teach_params(route, candidates=[candidate])
    steps = [
        {"text": "Άνοιξε τη λίστα.", "frame": 0, "note": "extra"},
        {"text": "Αντέγραψε μια γραμμή."},
    ]
    teach = {"title": "Παραγγελίες", "outcome": "Μια λίστα.", "steps": steps, "summary": "extra"}
    draft = {"name": "Ν", "purpose": "Π", "instructions": "Ο", "brief": "Β", "expectedResult": "Α"}
    assignment = {"reason": "Ρ", "brief": "Β", "expectedResult": "Α"}
    if problem == "unknown owl":
        teach["existingOwl"] = {"recipientId": "d" * 32, **assignment}
    elif problem == "two handoffs":
        teach["newOwl"] = draft
        teach["existingOwl"] = {"recipientId": candidate["id"], **assignment}
    elif problem == "handoff in an offer":
        # As a model wrote it on a live run: the handoff shaped like a suggestion's offer.
        teach["offer"] = {"existingOwl": {"id": candidate["id"], **assignment}}
    else:
        teach["question"] = "ε" * 241

    def handle(req):
        system = json.loads(req.content)["messages"][0]["content"]
        assert "existingOwl inside teach" in system and "inside offer" not in system
        return completion({"teach": teach})

    result = observation.infer(params, problem, transport=httpx.MockTransport(handle))
    assert result == {
        "teach": {
            "title": "Παραγγελίες",
            "outcome": "Μια λίστα.",
            "steps": [{"text": "Άνοιξε τη λίστα.", "frame": 0}, {"text": "Αντέγραψε μια γραμμή."}],
        }
    }
