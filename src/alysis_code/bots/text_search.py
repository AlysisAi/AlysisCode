"""Finding a phrase in an owl's messages the same way for every language and script.

The exact match is Unicode-normalised (NFC) and case-folded, as the database's
``unicode_fold`` does. When nothing matches exactly, a loose match also ignores accents and
other combining marks, compatibility forms (full-width letters, ligatures) and differences in
whitespace. Excerpts are cut from the message around its first match.
"""

from __future__ import annotations

import sys
import unicodedata
from functools import cache

EXCERPT_BEFORE = 60
EXCERPT_AFTER = 140


@cache
def _marks() -> dict[int, None]:
    """Every combining mark (a nonzero canonical combining class), built once on first use."""
    return {code: None for code in range(sys.maxunicode + 1) if unicodedata.combining(chr(code))}


def _without_marks(text: str) -> str:
    return text if text.isascii() else text.translate(_marks())


def _loose_piece(text: str) -> str:
    # Case folding can add marks (a dotted capital I folds to i and a dot), so decompose twice.
    once = _without_marks(unicodedata.normalize("NFKD", text)).casefold()
    return _without_marks(unicodedata.normalize("NFKD", once))


def exact_fold(text: str) -> str:
    return unicodedata.normalize("NFC", text).casefold()


def loose_fold(text: str) -> str:
    return " ".join(_loose_piece(unicodedata.normalize("NFC", text)).split())


def loose_contains(content: str, folded_query: str) -> bool:
    return bool(folded_query) and folded_query in loose_fold(content)


def _fold_with_origins(text: str, *, loose: bool) -> tuple[str, list[int]]:
    """The folded text, and for each of its characters the index it came from in ``text``.

    Folding a character at a time gives the same text as folding the whole message, apart
    from leading and trailing whitespace, so a match found here is the one search found.
    """
    folded: list[str] = []
    origins: list[int] = []
    for index, ch in enumerate(text):
        piece = _loose_piece(ch) if loose else ch.casefold()
        if loose and piece and piece.isspace():
            if folded and folded[-1] == " ":
                continue
            piece = " "
        for part in piece:
            folded.append(part)
            origins.append(index)
    return "".join(folded), origins


def _flat(text: str) -> str:
    return " ".join(text.split())


def excerpt(content: str, query: str, *, loose: bool) -> dict:
    """The words around the first match: before it, the match itself, and after it.

    Parts come from the message's own text, so a loose match shows the words as written.
    """
    text = unicodedata.normalize("NFC", content)
    needle = loose_fold(query) if loose else exact_fold(query)
    folded, origins = _fold_with_origins(text, loose=loose)
    at = folded.find(needle) if needle else -1
    if at < 0:
        head = text[: EXCERPT_BEFORE + EXCERPT_AFTER]
        return {
            "before": "",
            "match": "",
            "after": _flat(head) + ("…" if len(text) > len(head) else ""),
        }
    start = origins[at]
    end = origins[at + len(needle) - 1] + 1
    # Cuts never fall between a letter and its accents.
    while end < len(text) and unicodedata.combining(text[end]):
        end += 1
    first = max(0, start - EXCERPT_BEFORE)
    while first > 0 and unicodedata.combining(text[first]):
        first -= 1
    last = min(len(text), end + EXCERPT_AFTER)
    while last < len(text) and unicodedata.combining(text[last]):
        last += 1
    before, after = text[first:start], text[end:last]
    return {
        "before": ("…" if first else "")
        + _flat(before)
        + (" " if before[-1:].isspace() and before.strip() else ""),
        "match": text[start:end],
        "after": (" " if after[:1].isspace() and after.strip() else "")
        + _flat(after)
        + ("…" if last < len(text) else ""),
    }
