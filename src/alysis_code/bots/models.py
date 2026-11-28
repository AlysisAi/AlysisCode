from __future__ import annotations

import json
from typing import Annotated, Any, Literal, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
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

    reasoning_effort: str | None = Field(default=None, min_length=1, max_length=40)


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
    # Without an explicit creation key old callers keep their stable default conversation.
    thread_request_id: str | None = Field(
        default=None, min_length=8, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$"
    )
    title: str = Field(default="New conversation", min_length=1, max_length=160)


class ReadChatInput(Input):
    activity_seq: int = Field(ge=1)


# Files a person can attach to one message: each must already be in the owl's Library.
MAX_MESSAGE_FILES = 10
INTERACTIVE_STATE_BYTES = 1024 * 1024
INTERACTIVE_HTML_BYTES = 8 * 1024 * 1024
InteractiveHash = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$", max_length=64)]
StateVersion = Annotated[int, Field(strict=True, ge=0, le=2**53 - 1)]


class InteractiveRegion(Input):
    x: float = Field(strict=True, ge=0, le=1, allow_inf_nan=False)
    y: float = Field(strict=True, ge=0, le=1, allow_inf_nan=False)
    width: float = Field(strict=True, gt=0, le=1.000001, allow_inf_nan=False)
    height: float = Field(strict=True, gt=0, le=1.000001, allow_inf_nan=False)

    @model_validator(mode="after")
    def inside_viewport(self):
        if self.x + self.width > 1.000001 or self.y + self.height > 1.000001:
            raise ValueError("The selection must fit inside the captured viewport.")
        return self


class InteractiveElement(Input):
    region: InteractiveRegion
    label: str = Field(max_length=240)
    path: str = Field(min_length=1, max_length=1000)


class InteractiveViewport(Input):
    width: int = Field(strict=True, gt=0, le=16384)
    height: int = Field(strict=True, gt=0, le=16384)


class InteractiveMark(Input):
    element: InteractiveElement
    viewport: InteractiveViewport
    note: str = Field(min_length=1, max_length=6000)


class InteractiveTarget(Input):
    file_id: str = Field(min_length=1, max_length=100)
    revision: int = Field(strict=True, ge=1, le=2**53 - 1)
    sha256: InteractiveHash
    state_version: StateVersion
    region: InteractiveRegion
    element: InteractiveElement | None = None
    viewport: InteractiveViewport | None = None
    marks: list[InteractiveMark] | None = Field(default=None, min_length=2, max_length=8)

    @model_validator(mode="after")
    def check_marks(self):
        if self.marks:
            first = self.marks[0]
            if (self.element, self.region, self.viewport) != (
                first.element,
                first.element.region,
                first.viewport,
            ):
                raise ValueError("The first mark must match the selected element and viewport.")
            if sum(len(mark.note) for mark in self.marks) > 12000:
                raise ValueError("The selection notes are too long.")
        return self


class InteractiveStateWrite(Input):
    # The bridge stores JSON as supplied, including significant whitespace inside strings.
    model_config = ConfigDict(str_strip_whitespace=False)
    revision: int = Field(strict=True, ge=1, le=2**53 - 1)
    sha256: InteractiveHash
    expected_version: StateVersion
    data: JsonValue

    @field_validator("data")
    @classmethod
    def bounded_json(cls, value: JsonValue) -> JsonValue:
        try:
            raw = json.dumps(
                value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ).encode("utf-8")
        except (UnicodeError, ValueError, RecursionError) as exc:
            raise ValueError("Result state must be finite UTF-8 JSON.") from exc
        if len(raw) > INTERACTIVE_STATE_BYTES:
            raise ValueError("Result state exceeds 1 MiB.")
        return value


class TaskInput(Input):
    # Words, files or both: a message may be only the files the person attached.
    message: str = Field(max_length=20000)
    request_id: str = Field(min_length=8, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")
    assistant_screen_requests: bool = Field(default=False, strict=True)
    interactive_target: InteractiveTarget | None = None
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
        if self.interactive_target and (
            not self.message.strip() or self.interactive_target.file_id not in self.file_ids
        ):
            raise ValueError(
                "Attach the selected HTML revision and write an instruction for this change."
            )
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


class CoordinatorInput(Input):
    enabled: bool = Field(strict=True)


class AssistantInput(Input):
    enabled: bool = Field(strict=True)


AssignmentScope = Literal["text", "web_research", "workbook", "computer"]
ASSIGNMENT_SITES = 10
ASSIGNMENT_APPS = 16
ASSIGNMENT_FILES = 20


class AssignmentAccess(Input):
    """What a computer task may use, exactly as the person saw it on the handoff card.

    The owl works on its own computer and uses only this: its offline computer, its online
    browser for reading, the websites it may click and type on, the connected apps listed here
    (each still with its own approval effect), and the files the person picked, already in the
    owl's library. Nothing on the person's computer, and nothing else.
    """

    offline_computer: bool = Field(default=False, strict=True)
    online_browser: bool = Field(default=False, strict=True)
    sites: list[str] = Field(default_factory=list, max_length=ASSIGNMENT_SITES)
    apps: list[
        Annotated[str, StringConstraints(pattern=r"^(alysis|custom)\.[a-z][a-z0-9_-]{0,63}$")]
    ] = Field(default_factory=list, max_length=ASSIGNMENT_APPS)
    file_ids: list[Annotated[str, StringConstraints(min_length=1, max_length=100)]] = Field(
        default_factory=list, max_length=ASSIGNMENT_FILES
    )

    @field_validator("sites")
    @classmethod
    def exact_origins(cls, sites: list[str]) -> list[str]:
        from .routine_access import website_origin

        return sorted({website_origin(site) for site in sites})

    @model_validator(mode="after")
    def something_to_use(self):
        # One way to write the same access, so a retried approval compares equal.
        self.apps = sorted(set(self.apps))
        self.file_ids = list(dict.fromkeys(self.file_ids))
        if self.sites:
            self.online_browser = True
        if not (self.offline_computer or self.online_browser or self.apps):
            raise ValueError(
                "A computer task uses the owl's computer, its browser or a connected app."
            )
        return self


# Steps the person taught the Assistant by showing or telling a task once. They travel in the
# brief the owl receives and fit a playbook (playbooks.py), so they can be kept with the owl.
TAUGHT_STEPS = 12
TaughtStep = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]


class TaughtPlaybook(Input):
    """Keep the taught steps with the owl as a playbook for the next time the job comes."""

    name: str = Field(min_length=1, max_length=80)
    when_to_use: str = Field(min_length=1, max_length=200)
    done_check: str = Field(min_length=1, max_length=300)


class AssistantScreenRequest(Input):
    goal: str = Field(min_length=1, max_length=600)


AssignmentFileIds = Annotated[
    list[Annotated[str, StringConstraints(min_length=1, max_length=100)]],
    Field(max_length=ASSIGNMENT_FILES),
]


class AssignmentFiles(Input):
    input_file_ids: AssignmentFileIds = Field(default_factory=list)

    @field_validator("input_file_ids")
    @classmethod
    def distinct_files(cls, ids: list[str]) -> list[str]:
        if len(ids) != len(set(ids)):
            raise ValueError("Select each file revision once.")
        return ids


class AssistantOwlDraftInput(AssignmentFiles):
    name: str = Field(min_length=1, max_length=80)
    purpose: str = Field(min_length=1, max_length=1000)
    instructions: str = Field(min_length=1, max_length=6000)
    brief: str = Field(min_length=1, max_length=12000)
    expected_result: str = Field(min_length=1, max_length=2000)
    work_scope: AssignmentScope = "text"
    steps: list[TaughtStep] = Field(default_factory=list, max_length=TAUGHT_STEPS)
    access: AssignmentAccess | None = None

    @model_validator(mode="after")
    def access_for_computer(self):
        if (self.work_scope == "computer") != (self.access is not None):
            raise ValueError("A computer task, and only a computer task, lists what it may use.")
        return self


class StepReport(Input):
    """A computer task says which numbered step of its brief it is starting."""

    step: int = Field(ge=1, le=12, description="The number of the step you are starting now.")


class _KeepsTaughtSteps(Input):
    playbook: TaughtPlaybook | None = None

    @model_validator(mode="after")
    def steps_to_keep(self):
        if self.playbook is not None and not getattr(self, "steps", None):
            raise ValueError("Only steps can be kept as a playbook.")
        return self


class AssistantOwlCreateInput(AssistantOwlDraftInput, _KeepsTaughtSteps):
    request_id: str = Field(min_length=8, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")
    start_task: bool = Field(strict=True)


class AssignmentInput(AssignmentFiles):
    recipient_bot_id: str = Field(min_length=1, max_length=100)
    brief: str = Field(min_length=1, max_length=12000)
    expected_result: str = Field(min_length=1, max_length=2000)
    work_scope: AssignmentScope = "text"
    steps: list[TaughtStep] = Field(default_factory=list, max_length=TAUGHT_STEPS)
    access: AssignmentAccess | None = None

    @model_validator(mode="after")
    def access_for_computer(self):
        if (self.work_scope == "computer") != (self.access is not None):
            raise ValueError("A computer task, and only a computer task, lists what it may use.")
        return self


class OwlAssignmentProposal(AssignmentInput):
    # The earlier finished assignment to the same owl that this one repeats, as the coordinator
    # judged it: the person can then make it a routine. A value that names no such assignment is
    # dropped rather than refused, so a wrong guess never costs the handoff itself.
    repeats: str | None = Field(default=None, max_length=100)


class AssistantAssignmentApproval(AssignmentInput, _KeepsTaughtSteps):
    request_id: str = Field(min_length=8, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")


class AssignmentDecision(Input):
    decision: Literal["approve", "decline"]
    work_scope: AssignmentScope | None = None
    input_file_ids: AssignmentFileIds | None = None


class AssignmentPage(Input):
    before: str = Field(pattern=r"^[a-f0-9]{32}$")


class AssignmentVerification(Input):
    status: Literal["met", "needs_review", "unavailable"]
    detail: str | None = Field(default=None, max_length=2000)


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


NewSeries = Annotated[
    bool,
    Field(strict=True, description="True for a distinct output; corrections use previous_file_id."),
]


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


class SaveResultFile(FileInput):
    new_series: NewSeries = False
