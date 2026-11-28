"""Owls make and change pictures with the image generation their model connection declares.

The engine's own image request runs for real (request building, the provider's answer, decoding
and checking the bytes); only the image provider is a local fake transport. Real store and
runtime; prompts and names in Greek and Chinese.
"""

from __future__ import annotations

import base64
import io
import json
import threading
import time
from pathlib import Path

import httpx
import pytest
from PIL import Image
from test_bots_browser_runtime import ClientFixture, response

from alysis_code.bots import owl_documents, owl_images
from alysis_code.bots.app import Settings
from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.owl_images import IMAGE_TOOLS
from alysis_code.bots.owl_models import CatalogModel, ModelCatalog, ModelConnection, PlaceModels
from alysis_code.bots.runtime import BotRuntime, RuntimeLimits
from alysis_code.bots.store import Conflict, LimitReached, Store
from alysis_code.cancellation import CooperativeCancellationError
from alysis_code.config import ImageGenerationConfig

GREEK = "Μια κουκουβάγια διαβάζει εφημερίδα σε ένα καφενείο στην Αθήνα, ακουαρέλα"
CHINESE = "一只猫头鹰在北京的茶馆里看报纸，水彩画风格"
ENDPOINT = "https://models.example.invalid/v1"
IMAGE_MODEL = "image-model-under-test"


def png(size=(8, 6), color=(200, 30, 60, 255)) -> bytes:
    output = io.BytesIO()
    Image.new("RGBA", size, color).save(output, format="PNG")
    return output.getvalue()


def settings(tmp_path, *, images=True, protocol="openai_compat", model="text-model") -> Settings:
    return Settings(
        database=tmp_path / "service.sqlite3",
        token="t" * 40,
        api_key="connection-key",
        base_url=ENDPOINT,
        model=model,
        protocol=protocol,
        image_generation={"enabled": True, "model": IMAGE_MODEL} if images else None,
    )


class Provider:
    """A fake image service: records each request and answers with a picture or a refusal."""

    def __init__(self, *, status=200, error=None, picture=None, wait=None):
        self.requests: list[httpx.Request] = []
        self.status, self.error = status, error
        self.picture = picture or png((12, 9))
        self.wait = wait

    def __call__(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.requests.append(request)
        if self.wait:
            self.wait()
        if self.status != 200:
            return httpx.Response(self.status, json={"error": self.error})
        return httpx.Response(
            200,
            json={
                "data": [{"b64_json": base64.b64encode(self.picture).decode("ascii")}],
                "usage": {"input_tokens": 50, "output_tokens": 4160, "total_tokens": 4210},
            },
        )


@pytest.fixture
def provider(monkeypatch):
    fake = Provider()
    real = owl_images.generate_images

    def through_fake(**arguments):
        return real(**arguments, transport=httpx.MockTransport(fake))

    monkeypatch.setattr(owl_images, "generate_images", through_fake)
    return fake


def owl(tmp_path, *, files_enabled=True, model=None, message="Φτιάξε μια εικόνα"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    store = Store(tmp_path / "bots.sqlite3")
    bot = store.save_bot(
        "owner",
        BotInput(name="Ζωγράφος", purpose="Pictures", files_enabled=files_enabled, model=model),
    )
    chat = store.create_chat("owner", bot["id"], "Conversation")
    store.enqueue("owner", chat["id"], TaskInput(message=message, request_id=f"images-{bot['id']}"))
    return store, bot, store.claim("owner")


def runtime_for(store, connections, client=None, *, limits=None):
    runtime = BotRuntime(store, "owner", lambda _: client, limits=limits)
    runtime.models = PlaceModels(lambda: list(connections), lambda _task, _settings: client)
    return runtime


def place(tmp_path, *, images=True):
    return [ModelConnection("default", "default", settings(tmp_path, images=images), default=True)]


def call(runtime, task, name, arguments, *, image_input=False):
    return runtime._tool(
        task,
        name,
        arguments,
        list(IMAGE_TOOLS),
        image_input=image_input,
        token=runtime.task_token(task["id"]),
    )


def library(store, bot):
    return store.list_files("owner", bot["id"])


def scratch_left(store) -> list[Path]:
    folder = Path(store.path).parent / owl_documents.SCRATCH
    return [path for path in folder.rglob("*") if path.is_file()] if folder.exists() else []


def sent_json(request: httpx.Request) -> dict:
    return json.loads(request.content.decode("utf-8"))


def sent_form(request: httpx.Request) -> tuple[dict, bytes]:
    """The fields and the picture of a multipart edit request."""
    boundary = request.headers["content-type"].split("boundary=", 1)[1].encode()
    fields, picture = {}, b""
    for part in request.content.split(b"--" + boundary)[1:-1]:
        head, _, body = part.strip(b"\r\n").partition(b"\r\n\r\n")
        name = head.split(b'name="', 1)[1].split(b'"', 1)[0].decode()
        if b"filename=" in head:
            picture = body
        else:
            fields[name] = body.decode("utf-8")
    return fields, picture


def offered(client) -> set[str]:
    return {tool["function"]["name"] for tool in client.requests[0]["tools"] or []}


# ---------------------------------------------------------------- offered only where they work


@pytest.mark.parametrize(("files_enabled", "images"), [(True, True), (True, False), (False, True)])
def test_the_tools_are_offered_only_with_files_on_and_a_declared_capability(
    tmp_path, files_enabled, images
):
    store, _bot, task = owl(tmp_path, files_enabled=files_enabled)
    client = ClientFixture([response("Εντάξει.")])
    runtime = runtime_for(store, place(tmp_path, images=images), client)
    runtime.run(task, runtime.task_token(task["id"]))
    names = offered(client)
    if files_enabled and images:
        assert set(IMAGE_TOOLS) <= names
    else:
        assert not set(IMAGE_TOOLS) & names
    # A service with no model connections listed (no declaration at all) offers neither.
    store2, _, task2 = owl(tmp_path / "bare")
    bare = ClientFixture([response("Εντάξει.")])
    plain = BotRuntime(store2, "owner", lambda _: bare)
    plain.run(task2, plain.task_token(task2["id"]))
    assert not set(IMAGE_TOOLS) & offered(bare)


def test_two_connections_with_different_declared_capabilities(tmp_path, provider):
    connections = [
        ModelConnection("default", "default", settings(tmp_path, images=True), default=True),
        ModelConnection("other", "other", settings(tmp_path, images=False, model="other-model")),
    ]
    # The place lists what each model can do, for the app.
    listed = PlaceModels(lambda: connections, lambda *_: None).list()
    assert [(m["id"], m["image_generation"]) for m in listed] == [
        ("default", True),
        ("other", False),
    ]
    seen = {}
    for model in (None, "other"):
        store, bot, task = owl(tmp_path / str(model), model=model)
        client = ClientFixture([response("Εντάξει.")])
        runtime = runtime_for(store, connections, client)
        runtime.run(task, runtime.task_token(task["id"]))
        seen[model] = set(IMAGE_TOOLS) <= offered(client)
        if model == "other":
            assert not set(IMAGE_TOOLS) & offered(client)
            # Called anyway (a stale plan), it is refused plainly and nothing reaches a provider.
            store.enqueue(
                "owner",
                store.list_chats("owner", bot["id"])[0]["id"],
                TaskInput(message="Ζωγράφισε", request_id="images-again"),
            )
            again = store.claim("owner")
            with pytest.raises(Conflict, match="isn't set up"):
                call(runtime, again, "create_image", {"prompt": GREEK, "name": "α.png"})
    assert seen == {None: True, "other": False}
    assert provider.requests == []


def test_a_catalog_model_makes_pictures_as_its_connection_declares(tmp_path, provider):
    """A model picked from a connection's catalog thinks through that connection, so it gets
    the connection's image generation, also after its catalog stops opening it."""
    listed = [CatalogModel("catalog-model", "Μοντέλο καταλόγου")]
    connections = [
        ModelConnection("default", "default", settings(tmp_path, images=False), default=True),
        ModelConnection(
            "drawing",
            "drawing",
            settings(tmp_path, images=True, model="drawing-model"),
            catalog=lambda: ModelCatalog(tuple(listed)),
        ),
    ]
    rows = PlaceModels(lambda: connections, lambda *_: None).list()
    assert [(row["id"], row["image_generation"]) for row in rows] == [
        ("default", False),
        ("drawing", True),
        ("drawing/catalog-model", True),
    ]
    arguments = {"prompt": GREEK, "name": "εικόνα.png"}
    for index, picked in enumerate(("drawing/catalog-model", "drawing/catalog-model", "default")):
        store, bot, task = owl(tmp_path / str(index), model=picked)
        if picked == "default":
            client = ClientFixture([response("Εντάξει.")])
        else:
            client = ClientFixture(
                [response(name="create_image", arguments=arguments), response("Έγινε.")]
            )
        runtime = runtime_for(store, connections, client)
        runtime.run(task, runtime.task_token(task["id"]))
        if picked == "default":
            assert not set(IMAGE_TOOLS) & offered(client)
            continue
        assert set(IMAGE_TOOLS) <= offered(client)
        finished = store.task("owner", task["id"])
        assert finished["status"] == "completed"
        assert [file["name"] for file in library(store, bot)] == [arguments["name"]]
        fallbacks = [e for e in finished["events"] if e["kind"] == "model_fallback"]
        assert len(fallbacks) == (1 if index else 0)
        # The second time the catalog no longer lists it: the connection's own model thinks.
        listed.clear()
    assert [request.headers["authorization"] for request in provider.requests] == [
        "Bearer connection-key"
    ] * 2
    assert {sent_json(request)["model"] for request in provider.requests} == {IMAGE_MODEL}


def test_a_saved_connection_declares_it_only_where_the_image_request_reaches(tmp_path):
    on = ImageGenerationConfig(enabled=True, model=IMAGE_MODEL)
    off = ImageGenerationConfig(enabled=False, model=IMAGE_MODEL)
    own_endpoint = ImageGenerationConfig(
        enabled=True, model=IMAGE_MODEL, base_url="https://images.example.invalid/v1"
    )
    assert owl_images.for_connection(on, "openai_compat") is on
    assert owl_images.for_connection(on, "anthropic_messages") is None
    assert owl_images.for_connection(own_endpoint, "anthropic_messages") is own_endpoint
    assert owl_images.for_connection(off, "openai_compat") is None
    # The service settings carry the declaration from bootstrap JSON, validated like the
    # engine's own settings.
    declared = settings(tmp_path)
    assert owl_images.declared(declared).model == IMAGE_MODEL
    assert owl_images.declared(settings(tmp_path, images=False)) is None
    with pytest.raises(ValueError):
        Settings(
            database=tmp_path / "x.sqlite3",
            token="t" * 40,
            api_key="k",
            base_url=ENDPOINT,
            model="m",
            image_generation={"enabled": True, "max_images_per_call": 9},
        )


# ---------------------------------------------------------------- making and changing


@pytest.mark.parametrize("image_input", [False, True])
def test_a_chat_turn_makes_a_picture_that_shows_as_a_file_card(tmp_path, provider, image_input):
    store, bot, task = owl(tmp_path)
    arguments = {"prompt": GREEK, "name": "Κουκουβάγια 猫头鹰.png", "size": "1536x1024"}
    client = ClientFixture(
        [response(name="create_image", arguments=arguments), response("Την έφτιαξα.")]
    )
    client._alysis_browser_image_input = image_input
    runtime = runtime_for(store, place(tmp_path), client)
    runtime.run(task, runtime.task_token(task["id"]))
    finished = store.task("owner", task["id"])
    assert finished["status"] == "completed"
    (file,) = library(store, bot)
    assert file["name"] == arguments["name"] and file["source_task_id"] == task["id"]
    assert file["binary"] and Image.open(
        io.BytesIO(store.file_data("owner", bot["id"], file["id"]))
    ).size == (12, 9)
    started = [e for e in finished["events"] if e["kind"] == "tool_started"]
    assert started[-1]["data"] == {"name": "create_image", "file": arguments["name"]}
    completed = [e for e in finished["events"] if e["kind"] == "tool_completed"]
    assert completed[-1]["data"] == {"name": "create_image", "id": file["id"]}
    # The request went to the connection's own endpoint, with its key and the declared model.
    (request,) = provider.requests
    assert str(request.url) == f"{ENDPOINT}/images/generations"
    assert request.headers["authorization"] == "Bearer connection-key"
    body = sent_json(request)
    assert body["model"] == IMAGE_MODEL and body["n"] == 1 and body["size"] == "1536x1024"
    # The call is one of the task's own, charged in the ledger with what the provider reported.
    image_calls = [c for c in store.task("owner", task["id"])["usage"] if c["model"] == IMAGE_MODEL]
    assert [c["status"] for c in image_calls] == ["completed"]
    last = client.requests[-1]
    (receipt,) = [json.loads(m["content"]) for m in last["messages"] if m["role"] == "tool"]
    assert receipt["saved"] is True and receipt["id"] == file["id"] and receipt["version"] == 1
    images = [
        part
        for message in last["messages"]
        if isinstance(message.get("content"), list)
        for part in message["content"]
        if part.get("type") == "image_url"
    ]
    if image_input:
        (image,) = images
        assert image["image_url"]["url"].startswith("data:image/png;base64,")
    else:
        assert images == [] and "can't see images" in receipt["next"]
    assert scratch_left(store) == []


@pytest.mark.parametrize("prompt", [GREEK, CHINESE])
def test_a_prompt_in_any_language_reaches_the_image_service_unchanged(tmp_path, provider, prompt):
    store, bot, task = owl(tmp_path)
    runtime = runtime_for(store, place(tmp_path))
    call(runtime, task, "create_image", {"prompt": prompt, "name": "εικόνα.webp"})
    original = library(store, bot)[0]
    call(
        runtime,
        task,
        "edit_image",
        {"file_id": original["id"], "instructions": prompt, "name": "图片.png"},
    )
    create, edit = provider.requests
    assert sent_json(create)["prompt"] == prompt
    assert sent_form(edit)[0]["prompt"] == prompt


def test_editing_saves_a_new_version_or_a_separate_file_and_keeps_the_original(tmp_path, provider):
    store, bot, task = owl(tmp_path)
    runtime = runtime_for(store, place(tmp_path))
    source = png((10, 10), (0, 120, 255, 255))
    original = store.save_bytes("owner", bot["id"], "Λογότυπο.png", source)
    provider.picture = png((16, 16), (255, 200, 0, 255))
    version = call(
        runtime,
        task,
        "edit_image",
        {"file_id": original["id"], "instructions": "Κάνε το φόντο κίτρινο", "size": "1024x1024"},
    )
    assert version["saved"] and version["version"] == 2 and version["name"] == "Λογότυπο.png"
    assert version["source_file_id"] == original["id"]
    # The original version is still there, byte for byte.
    assert store.file_data("owner", bot["id"], original["id"]) == source
    separate = call(
        runtime,
        task,
        "edit_image",
        {"file_id": version["id"], "instructions": "黑白", "name": "Λογότυπο ασπρόμαυρο.jpg"},
    )
    assert separate["version"] == 1 and separate["format"] == "jpeg"
    names = sorted((f["name"], f["revision"]) for f in library(store, bot))
    assert names == [("Λογότυπο ασπρόμαυρο.jpg", 1), ("Λογότυπο.png", 1), ("Λογότυπο.png", 2)]
    # The picture went to the edit endpoint as a file, with the instructions and the model.
    first_edit = provider.requests[0]
    assert str(first_edit.url) == f"{ENDPOINT}/images/edits"
    fields, picture = sent_form(first_edit)
    assert picture == source
    assert fields["model"] == IMAGE_MODEL and fields["size"] == "1024x1024"
    # An older version can't be edited into the file's next version, and a file that isn't a
    # picture is refused before any request.
    with pytest.raises(Conflict, match="older version"):
        call(runtime, task, "edit_image", {"file_id": original["id"], "instructions": "Ξανά"})
    text = store.save_bytes("owner", bot["id"], "σημειώσεις.png", b"not a picture at all")
    with pytest.raises(Conflict, match="isn't a PNG, JPEG or WebP"):
        call(runtime, task, "edit_image", {"file_id": text["id"], "instructions": "Ξανά"})
    assert len(provider.requests) == 2


def test_a_name_that_is_taken_is_never_overwritten(tmp_path, provider):
    store, bot, task = owl(tmp_path)
    runtime = runtime_for(store, place(tmp_path))
    first = call(runtime, task, "create_image", {"prompt": GREEK, "name": "α.png"})
    with pytest.raises(Conflict, match="already exists"):
        call(runtime, task, "create_image", {"prompt": GREEK, "name": "α.png"})
    second = call(
        runtime,
        task,
        "create_image",
        {"prompt": CHINESE, "name": "α.png", "previous_file_id": first["id"]},
    )
    assert second["version"] == 2
    assert sorted(f["id"] for f in library(store, bot)) == sorted([first["id"], second["id"]])
    assert store.file("owner", bot["id"], first["id"])["is_latest"] is False


# ---------------------------------------------------------------- limits, refusals and Stop


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (429, {"message": "You exceeded your current quota.", "code": "insufficient_quota"}),
        (402, {"message": "Out of credits.", "code": "plan_credits_exhausted"}),
    ],
)
def test_an_over_limit_answer_is_said_plainly_and_nothing_is_kept(
    tmp_path, provider, status, error
):
    store, bot, task = owl(tmp_path)
    runtime = runtime_for(store, place(tmp_path))
    provider.status, provider.error = status, error
    with pytest.raises(Conflict) as refused:
        call(runtime, task, "create_image", {"prompt": GREEK, "name": "α.png"})
    message = str(refused.value)
    assert message.startswith("No picture was made: the image service refused it because")
    assert error["message"].rstrip(".") in message
    assert library(store, bot) == [] and scratch_left(store) == []
    # The ledger never shows a refused call as free or finished.
    (image_call,) = [
        c for c in store.task("owner", task["id"])["usage"] if c["model"] == IMAGE_MODEL
    ]
    assert image_call["status"] == "unknown"


def test_another_refusal_is_not_called_a_limit(tmp_path, provider):
    store, bot, task = owl(tmp_path)
    runtime = runtime_for(store, place(tmp_path))
    provider.status = 400
    provider.error = {"message": "Your request was rejected by the safety system.", "code": "x"}
    with pytest.raises(Conflict, match="rejected by the safety system") as refused:
        call(runtime, task, "create_image", {"prompt": GREEK, "name": "α.png"})
    assert "limit" not in str(refused.value)
    assert library(store, bot) == []


def test_a_used_up_call_allowance_refuses_before_any_request(tmp_path, provider):
    store, bot, task = owl(tmp_path)
    runtime = runtime_for(store, place(tmp_path), limits=RuntimeLimits(max_calls=2))
    with pytest.raises(LimitReached, match="allowance has no calls left"):
        call(runtime, task, "create_image", {"prompt": GREEK, "name": "α.png"})
    per_task = runtime_for(store, place(tmp_path), limits=RuntimeLimits(calls_per_task=2))
    with pytest.raises(LimitReached, match="allowance has no calls left"):
        call(per_task, task, "create_image", {"prompt": GREEK, "name": "α.png"})
    assert provider.requests == [] and library(store, bot) == []


def test_stop_while_the_service_answers_returns_at_once_and_keeps_nothing(tmp_path, provider):
    store, bot, task = owl(tmp_path)
    runtime = runtime_for(store, place(tmp_path))
    release = threading.Event()
    answered = threading.Event()

    def stop_then_answer_late():
        runtime.cancel(task["id"])  # the person presses Stop while the picture is being made
        release.wait(10)
        answered.set()

    provider.wait = stop_then_answer_late
    started = time.monotonic()
    with pytest.raises(CooperativeCancellationError):
        call(runtime, task, "create_image", {"prompt": GREEK, "name": "α.png"})
    assert time.monotonic() - started < 5  # it did not wait for the service's answer
    release.set()
    assert answered.wait(10)
    # The late answer is handled by the request's own thread: once it has, still nothing.
    for thread in threading.enumerate():
        if thread.name == "alysis-owl-image":
            thread.join(10)
            assert not thread.is_alive()
    assert library(store, bot) == [] and scratch_left(store) == []
    (image_call,) = [
        c for c in store.task("owner", task["id"])["usage"] if c["model"] == IMAGE_MODEL
    ]
    assert image_call["status"] == "unknown"


def test_files_turned_off_during_a_task_stop_picture_work(tmp_path, provider):
    store, bot, task = owl(tmp_path)
    runtime = runtime_for(store, place(tmp_path))
    with store.connect(write=True) as db:
        db.execute("UPDATE bots SET files_enabled=0 WHERE id=?", (bot["id"],))
    with pytest.raises(Conflict, match="Files are turned off"):
        call(runtime, task, "create_image", {"prompt": GREEK, "name": "α.png"})
    assert provider.requests == []


def test_owls_on_this_computer_declare_it_per_saved_connection(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from alysis_code.bots.app import create_app
    from alysis_code.bots.local_service import saved_connections, settings_from_bootstrap
    from alysis_code.config import AppConfig, save_config

    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_LOCAL_TEST_KEY", "placeholder-connection-key")
    cfg = AppConfig(
        model="text-model",
        base_url=ENDPOINT,
        image_generation={"enabled": True, "model": IMAGE_MODEL},
    )
    profile = {"base_url": ENDPOINT, "api_key_env": "ALYSIS_LOCAL_TEST_KEY"}
    cfg.extra_fields["profiles"] = {
        "compatible": {**profile, "protocol": "openai_compat", "default_model": "text-model"},
        "messages": {
            **profile,
            "protocol": "anthropic_messages",
            "base_url": "https://messages.example.invalid",
            "default_model": "messages-model",
        },
    }
    cfg.extra_fields["active_profile"] = "compatible"
    save_config(cfg)
    service = settings_from_bootstrap(
        {"directory": str(tmp_path / "data"), "token": "t" * 40, "profile": "compatible"}
    )
    connections = saved_connections(service)
    # The image request is OpenAI-compatible: only the connection whose endpoint takes it
    # declares it, with the person's own image settings.
    assert [
        (item.id, owl_images.declared(item.settings) is not None) for item in connections()
    ] == [
        ("compatible", True),
        ("messages", False),
    ]
    with TestClient(create_app(service, model_connections=connections)) as client:
        client.headers["Authorization"] = "Bearer " + service.token
        assert "owl_images" in client.get("/v1/status").json()["capabilities"]
        listed = client.get("/v1/models").json()["models"]
        assert [(item["id"], item["image_generation"]) for item in listed] == [
            ("compatible", True),
            ("messages", False),
        ]
        assert "placeholder-connection-key" not in str(listed)
    # Turned off in the app, it counts from the next read, for the default connection too.
    cfg.image_generation = ImageGenerationConfig(enabled=False, model=IMAGE_MODEL)
    save_config(cfg)
    assert [owl_images.declared(item.settings) for item in connections()] == [None, None]


def test_an_edit_step_shows_the_name_of_the_picture_it_saves(tmp_path):
    store, bot, task = owl(tmp_path)
    runtime = runtime_for(store, place(tmp_path))
    original = store.save_bytes("owner", bot["id"], "Αφίσα 海报.png", png())
    step = {"file_id": original["id"], "instructions": "Πιο φωτεινή"}
    assert runtime._step_started(task, "edit_image", step) == {
        "name": "edit_image",
        "file": "Αφίσα 海报.png",
    }
    assert runtime._step_started(task, "edit_image", {**step, "name": "新海报.png"}) == {
        "name": "edit_image",
        "file": "新海报.png",
    }
    missing = {"file_id": "not-a-library-file", "instructions": "Πιο φωτεινή"}
    assert runtime._step_started(task, "edit_image", missing) == {"name": "edit_image"}


def test_what_the_library_would_refuse_is_refused_before_a_charged_request(tmp_path, provider):
    store, bot, task = owl(tmp_path)
    runtime = runtime_for(store, place(tmp_path))
    store.save_bytes("owner", bot["id"], "α.png", png())
    with pytest.raises(Conflict, match="already exists"):
        call(runtime, task, "create_image", {"prompt": GREEK, "name": "α.png"})
    store.set_library_limit("owner", store.library_usage("owner")["used_bytes"])
    with pytest.raises(LimitReached, match="Library allowance"):
        call(runtime, task, "create_image", {"prompt": CHINESE, "name": "β.png"})
    assert provider.requests == []
    assert [c for c in store.task("owner", task["id"])["usage"] if c["model"] == IMAGE_MODEL] == []


# ---------------------------------------------------------------- quality and the account's service


def test_owls_use_the_persons_quality_unless_asked_and_the_declared_edit_model(tmp_path, provider):
    """The account's image service declares its own endpoint, models and qualities; the owl's
    requests carry the connection's own key there, the person's quality unless the person
    asked for another, and changes go to the edit model."""
    images = "https://images.example.invalid/v1"
    declared = {
        "enabled": True,
        "model": "maker-model",
        "edit_model": "editor-model",
        "base_url": images,
        "key_from": "connection",
        "quality": "low",
        "qualities": ["low", "medium", "high", "xhigh", "max"],
    }
    connection = Settings(
        database=tmp_path / "service.sqlite3",
        token="t" * 40,
        api_key="connection-key",
        base_url=ENDPOINT,
        model="text-model",
        image_generation=declared,
    )
    store, bot, task = owl(tmp_path)
    runtime = runtime_for(store, [ModelConnection("default", "default", connection, default=True)])
    made = call(runtime, task, "create_image", {"prompt": GREEK, "name": "κουκουβάγια.png"})
    call(runtime, task, "create_image", {"prompt": CHINESE, "name": "猫头鹰.png", "quality": "max"})
    call(
        runtime,
        task,
        "edit_image",
        {"file_id": made["id"], "instructions": "Πιο φωτεινό φόντο"},
    )
    first, second, edit = provider.requests
    assert [str(request.url) for request in provider.requests] == [
        f"{images}/images/generations",
        f"{images}/images/generations",
        f"{images}/images/edits",
    ]
    assert {request.headers["authorization"] for request in provider.requests} == {
        "Bearer connection-key"
    }
    assert (sent_json(first)["model"], sent_json(first)["quality"]) == ("maker-model", "low")
    assert sent_json(second)["quality"] == "max"
    fields, _picture = sent_form(edit)
    assert (fields["model"], fields["quality"]) == ("editor-model", "low")
    # A quality the service does not accept is refused plainly, before any request.
    narrower = {**declared, "qualities": ["low", "medium"]}
    store2, _bot2, task2 = owl(tmp_path / "narrower")
    runtime2 = runtime_for(
        store2,
        [
            ModelConnection(
                "default",
                "default",
                Settings(
                    database=tmp_path / "service2.sqlite3",
                    token="t" * 40,
                    api_key="connection-key",
                    base_url=ENDPOINT,
                    model="text-model",
                    image_generation=narrower,
                ),
                default=True,
            )
        ],
    )
    with pytest.raises(Conflict, match="quality must be one of: low, medium"):
        call(runtime2, task2, "create_image", {"prompt": GREEK, "name": "β.png", "quality": "max"})
    assert len(provider.requests) == 3
