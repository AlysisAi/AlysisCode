from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class BotInput(Input):
    name: str = Field(min_length=1, max_length=80)
    # Optional (AL, 2026-11-07): an owl is created with a name and where it works; purpose and
    # instructions can be added later in its settings.
    purpose: str = Field(default="", max_length=1000)
    instructions: str = Field(default="", max_length=6000)
    memory_enabled: bool = True
    files_enabled: bool = True


class ChatInput(Input):
    title: str = Field(default="New conversation", min_length=1, max_length=160)


class ReadChatInput(Input):
    activity_seq: int = Field(ge=1)


class TaskInput(Input):
    message: str = Field(min_length=1, max_length=20000)
    request_id: str = Field(min_length=8, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")


class ReplyReviewSettings(Input):
    enabled: bool = Field(strict=True)


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
