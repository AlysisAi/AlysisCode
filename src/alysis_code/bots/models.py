from __future__ import annotations

from typing import Annotated, Any, Literal, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


Model = TypeVar("Model", bound=BaseModel)


def _empty(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def tool_input(model: type[Model], arguments: Any) -> Model:
    """A model's tool arguments, validated; an optional field it left empty is not given.

    Models often fill an optional field they mean to leave out with null or an empty string
    (``"replaces_memory_id": ""``). Where that value fails the field's own check, the field
    takes its default instead of failing the call. An empty value a field accepts is kept as
    sent (an empty text can mean "clear it"), and required fields are never filled in.
    """
    try:
        return model.model_validate(arguments)
    except ValidationError as exc:
        if not isinstance(arguments, dict):
            raise
        fields = model.model_fields
        empty = {
            key
            for error in exc.errors()
            if error["loc"]
            and isinstance(key := error["loc"][0], str)
            and key in fields
            and not fields[key].is_required()
            and key in arguments
            and _empty(arguments[key])
        }
        if not empty:
            raise
    return model.model_validate({k: v for k, v in arguments.items() if k not in empty})


# A part of an owl's look (its colour, its background), by the app's name for it; the app draws
# the face and treats a name it doesn't know as no choice.
BotLookName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{0,23}$")]


class BotInput(Input):
    name: str = Field(min_length=1, max_length=80)
    # Optional (AL, 2026-11-07): an owl is created with a name and where it works; purpose and
    # instructions can be added later in its settings.
    purpose: str = Field(default="", max_length=1000)
    instructions: str = Field(default="", max_length=6000)
    memory_enabled: bool = True
    files_enabled: bool = True
    # The look the person picked for its face (capability "owl_looks"): the owl's colour and the
    # background behind it. Null: none, so the app picks a colour from the name and the soft tile
    # of that colour, as for owls made before. Left out of a change, each stays as it was.
    color: BotLookName | None = None
    background: BotLookName | None = None
    # The model it thinks with (capability "owl_models"): the id of one of the models its place
    # offers (GET /v1/models). Null: the place's own, as for owls made before. Left out of a
    # change, it stays as it was.
    model: str | None = Field(
        default=None, min_length=1, max_length=200, pattern=r"^[^\x00-\x1f\x7f]+$"
    )


class BotCreateInput(BotInput):
    # The same id from the same person returns the owl it already made, so a create whose answer
    # was lost can be sent again without making a second owl. Optional for older apps.
    request_id: str | None = Field(
        default=None, min_length=8, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$"
    )


class BotOrganizeInput(Input):
    """Where an owl sits in the person's list (capability "owl_organize"); a part left out stays.

    pinned keeps it at the top of the list. archived moves it to Archived: it keeps everything,
    and its routines and file events rest until it comes back.
    """

    pinned: bool | None = Field(default=None, strict=True)
    archived: bool | None = Field(default=None, strict=True)

    @model_validator(mode="after")
    def _something(self) -> BotOrganizeInput:
        if self.pinned is None and self.archived is None:
            raise ValueError("Say whether the owl is pinned or archived.")
        return self


class BotDuplicateInput(Input):
    """A new owl like this one, in the same place (capability "owl_organize").

    The app names the copy in the person's language. request_id works as for creating an owl:
    the same id returns the copy it already made. memories also copies the approved memories.
    """

    name: str = Field(min_length=1, max_length=80)
    request_id: str = Field(min_length=8, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")
    memories: bool = Field(default=False, strict=True)


class ChatInput(Input):
    title: str = Field(default="New conversation", min_length=1, max_length=160)


class ReadChatInput(Input):
    activity_seq: int = Field(ge=1)


# Files a person can attach to one message: each must already be in the owl's Library.
MAX_MESSAGE_FILES = 10


class TaskInput(Input):
    # Words, files or both: a message may be only the files the person attached.
    message: str = Field(max_length=20000)
    request_id: str = Field(min_length=8, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")
    # Library files the person attached to this message, in the order they added them.
    file_ids: list[Annotated[str, StringConstraints(min_length=1, max_length=100)]] = Field(
        default_factory=list, max_length=MAX_MESSAGE_FILES
    )

    @field_validator("file_ids")
    @classmethod
    def each_file_once(cls, file_ids: list[str]) -> list[str]:
        if len(set(file_ids)) != len(file_ids):
            raise ValueError("Attach each file once.")
        return file_ids

    @model_validator(mode="after")
    def words_or_files(self) -> TaskInput:
        if not self.message.strip() and not self.file_ids:
            raise ValueError("Write a message or attach a file.")
        return self


class RegenerateInput(Input):
    # The same id returns the answer already asked for, so a press whose reply was lost can be
    # sent again without starting a second one.
    request_id: str = Field(min_length=8, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")


class IntroInput(Input):
    request_id: str = Field(min_length=8, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")
    # The app's language, as a language tag: the owl writes its first message in it.
    language: str = Field(
        default="en", max_length=35, pattern=r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{1,8})*$"
    )


class ReplyReviewSettings(Input):
    enabled: bool = Field(strict=True)


class TimeZoneSettings(Input):
    # The person's IANA time zone, from their computer: owls read dates and clock times in it.
    time_zone: str = Field(min_length=1, max_length=100)

    @field_validator("time_zone")
    @classmethod
    def known_zone(cls, value: str) -> str:
        from .time_zone import valid_time_zone

        return valid_time_zone(value)


class MemoryInput(Input):
    title: str = Field(min_length=1, max_length=100)
    content: str = Field(min_length=1, max_length=2000)


class MemoryProposal(MemoryInput):
    replaces_memory_id: str | None = Field(default=None, min_length=1, max_length=100)


class RevisionQuery(Input):
    before_revision: int | None = Field(default=None, ge=1)


class MemoryDecision(Input):
    status: Literal["active", "rejected"]


class NoteInput(Input):
    title: str = Field(min_length=1, max_length=100)
    content: str = Field(min_length=1, max_length=4000)
    expected_revision: int = Field(default=0, ge=0)


class NoteWrite(NoteInput):
    note_id: str | None = Field(default=None, min_length=1, max_length=100)


class NoteSource(Input):
    note_id: str = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=1)


class NoteConsolidation(Input):
    title: str = Field(min_length=1, max_length=100)
    content: str = Field(min_length=1, max_length=4000)
    sources: list[NoteSource] = Field(min_length=2, max_length=30)
    reason: str = Field(min_length=1, max_length=1000)


class HistoryQuery(Input):
    query: str = Field(default="", max_length=500)
    before: int | None = Field(default=None, ge=1)
    limit: int = Field(default=10, ge=1, le=20)


class ConversationPageQuery(Input):
    """One page of an owl's conversation, by message id, so a long chat never loads whole.

    Without ``before`` it is the latest page. With the ``version`` a previous latest page
    returned (and ``after``, the newest message the client has), only what changed since comes
    back. ``before`` reads the page of earlier messages, and ``through_task`` reaches back far
    enough to include that task's first message (a search result, or something waiting for
    the person).
    """

    limit: int = Field(default=40, ge=1, le=200)
    before: str | None = Field(default=None, min_length=1, max_length=100)
    after: str | None = Field(default=None, min_length=1, max_length=100)
    version: str | None = Field(default=None, max_length=20000)
    through_task: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def _one_direction(self) -> ConversationPageQuery:
        if self.after is not None and self.version is None:
            raise ValueError("A newest message goes with the version it was read with.")
        if self.version is not None and (self.before is not None or self.through_task is not None):
            raise ValueError("Read the changes since a version, or an earlier page, not both.")
        return self


class ReadMessage(Input):
    message_id: str = Field(min_length=1, max_length=100)
    offset: int = Field(default=0, ge=0)
    length: int = Field(default=8000, ge=1, le=16000)


class ReferenceQuery(Input):
    query: str = Field(default="", max_length=500)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=30)
    generation: str | None = Field(default=None, min_length=64, max_length=64)


class NoteQuery(ReferenceQuery):
    include_archived: bool = False


class FileInput(Input):
    previous_file_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        description="For a new revision, the latest file ID for this exact name. Omit for a new file. Older attachments are preserved.",
    )
    name: str = Field(min_length=1, max_length=160)
    # File text is data: retain indentation, blank files and final newlines.
    content: Annotated[str, StringConstraints(strip_whitespace=False)] = Field(max_length=100000)

    @field_validator("name")
    @classmethod
    def simple_name(cls, name: str) -> str:
        # Names are labels, never paths. Unicode names are supported.
        if name in {".", ".."} or any(c in name for c in "/\\\x00\r\n"):
            raise ValueError("Use a file name without directory separators or control characters.")
        if any(ord(c) < 32 for c in name):
            raise ValueError("File names cannot contain control characters.")
        return name
