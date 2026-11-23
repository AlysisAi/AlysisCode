"""Owls make and change pictures with the image generation their model connection declares.

The engine's own image request (tools/image_generation.py) does the work, as it does for the
agent's sessions: the same OpenAI-compatible image endpoint, sizes, limits and checks of the
returned bytes. An owl is offered create_image and edit_image only while its files are on and
the model connection it thinks with declares image generation (``Settings.image_generation``:
on this computer, the person's own image generation settings, for a connection whose endpoint
takes image requests; in the Cloud, the service's settings). Nothing is decided from a model's
name or from the words of a request.

Accounting: each request is one of the task's own provider calls in the owls' call ledger
(Store._reserve_call), counted against the workspace allowance and the task's ceiling like a
model call and leaving ``KEEP_FOR_OWL`` calls for the owl's answer, as research helpers do. The
image provider charges and limits it for the account its key belongs to; when it refuses for a
usage limit or credits, the owl is told so plainly and nothing is kept.

Every call works in the document tools' scratch folder for its task (owl_documents.py) and keeps
the picture in memory until it is saved: Stop while the provider is still answering returns at
once, and an answer that arrives later is dropped. A result reaches the Library as a new file or
a new version (never over another file) and shows as a file card with the reply. A model that
declares image input is shown the picture, so it can check it.
"""

from __future__ import annotations

import json
import threading
from typing import Any, Literal

from pydantic import Field, field_validator

from ..config import AppConfig, ImageGenerationConfig
from ..llm.protocols import OPENAI_COMPAT_PROTOCOL
from ..tools.image_generation import SIZES, ImageEndpoint, ImageGenerationError, generate_images
from . import documents
from .browser_images import FileImageResult, file_image
from .models import Input
from .owl_documents import STOP_POLL_SECONDS, OwlDocuments, _library_name
from .research_helpers import KEEP_FOR_OWL
from .store import Conflict, LimitReached

# In the order a turn offers them.
IMAGE_TOOLS = ("create_image", "edit_image")
EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")
Size = Literal[SIZES]  # type: ignore[valid-type]
# The longest description a request may give; the engine's own limit.
MAX_PROMPT = 32_000
# HTTP answers that mean the account's usage limit, rate limit or credits stopped the request,
# and the provider error codes that say the same (the hosted plans' and the common quota ones).
LIMIT_STATUSES = frozenset({402, 429})
LIMIT_CODES = frozenset(
    {
        "plan_limit_reached",
        "plan_credits_exhausted",
        "rate_limit_exceeded",
        "usage_limit_reached",
        "insufficient_quota",
        "billing_hard_limit_reached",
    }
)

FILES_OFF = "Files are turned off for this owl, so it can't make or change pictures."
NOT_SET_UP = (
    "Making pictures isn't set up for the model connection this owl uses, so nothing was made."
)
NAME_EXTENSION = "name must end with .png, .jpg, .jpeg or .webp"
NOT_A_PICTURE = (
    "This file isn't a PNG, JPEG or WebP picture, so it can't be edited here. Make a new picture "
    "with create_image instead."
)
NEEDS_NAME = (
    "This file's name doesn't end with .png, .jpg, .jpeg or .webp: give name, the file name to "
    "save the changed picture as."
)
OLDER_VERSION = (
    "This is an older version of {name}. Edit its latest version (list_files shows it), or give "
    "a new name to save the result as a separate file."
)
NO_CALLS_LEFT = (
    "No picture was made: this task's model-call allowance has no calls left for it. Answer "
    "with what you have."
)
OVER_LIMIT = (
    "No picture was made: the image service refused it because a usage limit, rate limit or the "
    "credits of the account it uses ran out ({detail}). Nothing was saved. Tell the person "
    "plainly; try again only after the limit resets or the plan changes."
)
REFUSED = "No picture was made: {detail}. Nothing was saved."
SAVED = "Saved in the Library as {what}; it shows as a file card with your reply."
LOOK = " Look at the picture before calling it done; edit_image changes it."
NOT_VISIBLE = " The current model can't see images, so the picture isn't shown."


def declared(settings: Any) -> ImageGenerationConfig | None:
    """The image generation a model connection declares, or None."""
    config = getattr(settings, "image_generation", None)
    if isinstance(config, ImageGenerationConfig) and config.enabled:
        return config
    return None


def for_connection(config: ImageGenerationConfig, protocol: str) -> ImageGenerationConfig | None:
    """The image generation a saved model connection on this computer declares.

    The person's image generation settings, when they are on, for a connection the image request
    can reach: one with an image endpoint of its own, or one whose endpoint speaks the
    OpenAI-compatible protocol, as the request does.
    """
    if not config.enabled:
        return None
    if config.base_url or protocol == OPENAI_COMPAT_PROTOCOL:
        return config
    return None


def _picture_name(value: str) -> str:
    name = _library_name(value)
    if not name.casefold().endswith(EXTENSIONS):
        raise ValueError(NAME_EXTENSION)
    return name


class CreateImage(Input):
    prompt: str = Field(
        min_length=1,
        max_length=MAX_PROMPT,
        description="What the picture shows: subject, setting, style, colours and mood, and "
        "any text it must contain, written exactly as it should appear.",
    )
    name: str = Field(
        min_length=1,
        max_length=160,
        description="The Library file name, ending with .png, .jpg, .jpeg or .webp.",
    )
    size: Size = Field(
        default="auto",
        description="Square 1024x1024, landscape 1536x1024 or portrait 1024x1536; auto lets "
        "the image service choose.",
    )
    previous_file_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        description="For a new version of a Library file with this exact name: its latest file "
        "id. Leave out for a new file.",
    )

    @field_validator("name")
    @classmethod
    def picture_name(cls, value: str) -> str:
        return _picture_name(value)


class EditImage(Input):
    file_id: str = Field(
        min_length=1, max_length=100, description="The Library id of the picture to change."
    )
    instructions: str = Field(
        min_length=1,
        max_length=MAX_PROMPT,
        description="What to change and what to keep; any text it must contain, written "
        "exactly as it should appear.",
    )
    name: str | None = Field(
        default=None,
        min_length=1,
        max_length=160,
        description="Save the result as a separate Library file with this name. Left out, it "
        "is saved as a new version of this file.",
    )
    size: Size = Field(
        default="auto",
        description="Square 1024x1024, landscape 1536x1024 or portrait 1024x1536; auto lets "
        "the image service choose.",
    )

    @field_validator("name")
    @classmethod
    def picture_name(cls, value: str | None) -> str | None:
        return None if value is None else _picture_name(value)


class OwlImages(OwlDocuments):
    """One owl's picture work for one call; the runtime makes one per tool call.

    ``settings``: the model connection the owl thinks with (its endpoint, key and declared
    image generation); ``limits``: the turn's allowances.
    """

    def __init__(self, store, owner: str, token, settings: Any, limits) -> None:
        super().__init__(store, owner, token)
        self.settings = settings
        self.limits = limits

    def call(self, task: dict, name: str, args: Input, *, image_input: bool):  # type: ignore[override]
        self.token.throw_if_cancelled()
        if not self.store.bot(self.owner, task["bot_id"])["files_enabled"]:
            raise Conflict(FILES_OFF)
        config = declared(self.settings)
        if config is None:
            raise Conflict(NOT_SET_UP)
        if name == "create_image":
            return self._make(
                task,
                config,
                prompt=args.prompt,
                name=args.name,
                size=args.size,
                previous_file_id=args.previous_file_id,
                source=None,
                image_input=image_input,
            )
        if name == "edit_image":
            file, data = self._library_bytes(task, args.file_id)
            if documents.sniff(data[: documents.SNIFF_BYTES]) not in ("png", "jpeg", "webp"):
                raise Conflict(NOT_A_PICTURE)
            target = args.name or file["name"]
            if not target.casefold().endswith(EXTENSIONS):
                raise Conflict(NEEDS_NAME)
            # Saved under the file's own name, the result is its next version.
            previous = file["id"] if target == file["name"] else None
            if previous and not file["is_latest"]:
                raise Conflict(OLDER_VERSION.format(name=file["name"]))
            return self._make(
                task,
                config,
                prompt=args.instructions,
                name=target,
                size=args.size,
                previous_file_id=previous,
                source=(file["id"], data),
                image_input=image_input,
            )
        raise ValueError("Unsupported image tool.")

    # ------------------------------------------------------------ steps

    def _make(
        self, task, config, *, prompt, name, size, previous_file_id, source, image_input
    ) -> dict | FileImageResult:
        suffix = "." + name.rsplit(".", 1)[1].casefold()
        made: list[bytes] = []
        self._can_save(task, name, previous_file_id)
        with self._scratch(task) as root:
            call_id = self._reserve(task, config.model)
            try:
                result = self._beside_task(
                    lambda: generate_images(
                        root=root,
                        cfg=AppConfig(model=self.settings.model, image_generation=config),
                        fallback_api_key=None,
                        prompt=prompt,
                        output_path="picture" + suffix,
                        size=size,
                        connection=ImageEndpoint(
                            base_url=self.settings.base_url,
                            api_key=self.settings.api_key,
                            extra_headers=dict(self.settings.extra_headers or {}),
                        ),
                        source_image=None if source is None else source[1],
                        # The picture stays in memory: a late answer after Stop writes nothing.
                        publish=lambda _target, data: made.append(data),
                    )
                )
            except BaseException as error:
                # The provider may have started charging; the ledger never shows it as free.
                self.store.finish_call(call_id, "unknown", None)
                if isinstance(error, ImageGenerationError):
                    self.token.throw_if_cancelled()
                    raise Conflict(_refusal(error)) from None
                raise
            self.store.finish_call(call_id, "completed", _usage(result.get("provider_usage")))
        (data,) = made
        (image,) = result["images"]
        # Stop between the answer and the save leaves nothing; the store also refuses a write
        # from a task that is no longer running.
        self.token.throw_if_cancelled()
        saved = self.store.save_bytes(
            self.owner,
            task["bot_id"],
            name,
            data,
            source_task_id=task["id"],
            previous_file_id=previous_file_id,
        )
        what = (
            f"version {saved['revision']} of {saved['name']}"
            if saved["revision"] > 1
            else f"the new file {saved['name']}"
        )
        receipt = {
            "id": saved["id"],
            "saved": True,
            "name": saved["name"],
            "version": saved["revision"],
            **({"source_file_id": source[0]} if source else {}),
            "width": image["width"],
            "height": image["height"],
            "format": image["format"],
            "bytes": saved["size_bytes"],
            "sha256": saved["sha256"],
        }
        if not image_input:
            return {**receipt, "next": SAVED.format(what=what) + NOT_VISIBLE}
        receipt["next"] = SAVED.format(what=what) + LOOK
        try:
            picture = documents.picture(data)
        except documents.DocumentError as error:
            # The picture is saved; only the view of it is missing, and the model is told.
            return {**receipt, "picture": str(error)}
        source_name = json.dumps(saved["name"][:200], ensure_ascii=False)
        return file_image(receipt, picture.data_url, source_name, picture.width, picture.height)

    def _can_save(self, task, name: str, previous_file_id: str | None) -> None:
        """Refuse before the charged request what the Library would refuse after it: a name
        already taken (or a version that is no longer the latest), or a Library with no room
        left at all. The save checks both again."""
        with self.store.connect() as db:
            self.store._revision_base(db, task["bot_id"], name, previous_file_id)
            self.store._check_library_capacity(db, self.owner, 1)

    def _reserve(self, task, model: str) -> str:
        """One of the task's own provider calls, leaving KEEP_FOR_OWL for the owl (one write)."""
        store, limits = self.store, self.limits
        with store.connect(write=True) as db:
            current = store._task(db, self.owner, task["id"])
            if not store.executing(db, current):
                self.token.throw_if_cancelled()
                raise Conflict("Task is no longer running.")
            if (
                limits.calls_per_task is not None
                and current["calls"] >= limits.calls_per_task - KEEP_FOR_OWL
            ):
                raise LimitReached(NO_CALLS_LEFT)
            try:
                return store._reserve_call(
                    db,
                    self.owner,
                    task["id"],
                    model,
                    None if limits.max_calls is None else limits.max_calls - KEEP_FOR_OWL,
                    background=True,
                    purpose="image",
                )
            except LimitReached:
                raise LimitReached(NO_CALLS_LEFT) from None

    def _beside_task(self, request):
        """Run the provider request in its own thread, so Stop returns at once."""
        outcome: dict[str, Any] = {}
        done = threading.Event()

        def run() -> None:
            try:
                outcome["result"] = request()
            except BaseException as error:  # noqa: BLE001 - handed to the waiting call
                outcome["error"] = error
            finally:
                done.set()

        threading.Thread(target=run, name="alysis-owl-image", daemon=True).start()
        while not done.wait(STOP_POLL_SECONDS):
            self.token.throw_if_cancelled()
        self.token.throw_if_cancelled()
        if "error" in outcome:
            raise outcome["error"]
        return outcome["result"]


def _refusal(error: ImageGenerationError) -> str:
    """What to tell the model about a refused request: plainly, a limit when it is one."""
    detail = str(error).rstrip(".") or "the image service gave no reason"
    if error.status_code in LIMIT_STATUSES or error.provider_error_code in LIMIT_CODES:
        return OVER_LIMIT.format(detail=detail)
    return REFUSED.format(detail=detail)


def _usage(raw) -> dict | None:
    """The provider's token counts for the ledger, in the ledger's terms."""
    if not isinstance(raw, dict):
        return None
    counts = {
        "prompt_tokens": raw.get("input_tokens"),
        "completion_tokens": raw.get("output_tokens"),
    }
    usage = {key: value for key, value in counts.items() if type(value) is int and value >= 0}
    return usage or None
