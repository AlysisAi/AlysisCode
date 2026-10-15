"""Literal transcript navigation independent of rendering and composer state."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

_TERMINAL_SEQUENCE = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-_]"
)


def clean_response_text(text: str) -> str:
    """Keep source Markdown, removing terminal controls (including OSC links)."""
    text = _TERMINAL_SEQUENCE.sub("", text).replace("\r\n", "\n")
    return "".join(
        c for c in text if c in "\n\t" or ord(c) >= 32 and not 127 <= ord(c) < 160
    ).strip()


@dataclass(frozen=True, slots=True)
class TranscriptMatch:
    entry_index: int
    start: int
    end: int


class TranscriptNavigator:
    """Case-insensitive literal matches with offsets into the original entry.

    New searches select the first match. Next/previous wrap. Calling find again
    refreshes the snapshot; callers should do that after a transcript replacement.
    Multiline queries are literal and Unicode casefold expansions retain offsets.
    """

    def __init__(self) -> None:
        self.matches: list[TranscriptMatch] = []
        self.index = -1
        self.query = ""

    @property
    def current(self) -> TranscriptMatch | None:
        return self.matches[self.index] if self.matches and self.index >= 0 else None

    def find(self, entries: Sequence[tuple[str, str]], query: str) -> TranscriptMatch | None:
        self.query = query
        self.matches = []
        needle = query.casefold()
        if needle:
            for entry_index, (_, text) in enumerate(entries):
                folded = text.casefold()
                offsets = [i for i, char in enumerate(text) for _ in char.casefold()]
                cursor = 0
                previous: tuple[int, int] | None = None
                while (position := folded.find(needle, cursor)) >= 0:
                    span = (offsets[position], offsets[position + len(needle) - 1] + 1)
                    if span != previous:
                        self.matches.append(TranscriptMatch(entry_index, *span))
                    previous = span
                    cursor = position + len(needle)
        self.index = 0 if self.matches else -1
        return self.current

    def next(self) -> TranscriptMatch | None:
        if self.matches:
            self.index = (self.index + 1) % len(self.matches)
        return self.current

    def previous(self) -> TranscriptMatch | None:
        if self.matches:
            self.index = (self.index - 1) % len(self.matches)
        return self.current
