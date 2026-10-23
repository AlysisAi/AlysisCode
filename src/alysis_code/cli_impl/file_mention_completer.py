"""Cursor-local, text-only workspace path references for prompt_toolkit."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from prompt_toolkit.completion import Completer, Completion

from .workspace_file_index import FileIndexSnapshot, WorkspaceFileIndex


@dataclass(frozen=True)
class Mention:
    start: int
    prefix: str
    suffix: str


def mention_at_cursor(text: str, cursor: int) -> Mention | None:
    """Recognize @path and @\"quoted path\"; emails and escaped @ are prose."""
    cursor = max(0, min(cursor, len(text)))
    for match in reversed(list(re.finditer(r"(?<!\S)@", text[:cursor]))):
        start = match.start()
        pos = start + 1
        quoted = pos < len(text) and text[pos] == '"'
        if quoted:
            pos += 1
        value = ""
        prefix = ""
        while pos < len(text):
            char = text[pos]
            if (quoted and char == '"') or (not quoted and char.isspace()):
                break
            if quoted and char == "\\" and pos + 1 < len(text) and text[pos + 1] in '\\"':
                if pos + 1 == cursor:
                    return None
                pos += 1
                char = text[pos]
            value += char
            pos += 1
            if pos <= cursor:
                prefix = value
        end = pos + (1 if quoted and pos < len(text) and text[pos] == '"' else 0)
        if cursor > end or cursor <= start:
            continue
        if cursor == end:
            prefix = value
        if prefix.startswith(("/", "\\")) or ".." in prefix.replace("\\", "/").split("/"):
            return None
        return Mention(start, prefix, text[cursor:end])
    return None


def quote_reference(path: str) -> str:
    if all(char.isalnum() or char in "/._-" for char in path):
        return "@" + path
    return "@" + json.dumps(path, ensure_ascii=False)


class FileMentionCompleter(Completer):
    def __init__(self, *, workdir_provider, index=None, max_results: int = 50) -> None:
        self.workdir_provider = workdir_provider
        self.index = index if index is not None else WorkspaceFileIndex()
        self.max_results = max_results

    def _context(self, document):
        mention = mention_at_cursor(document.text, document.cursor_position)
        if mention is None:
            return None
        try:
            return mention, Path(self.workdir_provider())
        except (OSError, TypeError, ValueError):
            return None

    def _completions(self, document, mention: Mention, snapshot: FileIndexSnapshot):
        matches = []
        for path in snapshot.paths:
            if not path.casefold().startswith(mention.prefix.casefold()):
                continue
            replacement = quote_reference(path)
            # Completion replaces only text BEFORE the cursor. Keep the entire
            # suffix verbatim and offer only references compatible with it.
            if mention.suffix:
                if not replacement.endswith(mention.suffix):
                    continue
                replacement = replacement[: -len(mention.suffix)]
            matches.append((path, replacement))
            if len(matches) > self.max_results:
                break
        limited = snapshot.truncated or len(matches) > self.max_results
        meta = "Workspace path reference" + (
            " · results limited; narrow the path" if limited else ""
        )
        for path, replacement in matches[: self.max_results]:
            yield Completion(
                replacement,
                start_position=mention.start - document.cursor_position,
                display=path,
                display_meta=meta,
            )

    def get_completions(self, document, complete_event):
        context = self._context(document)
        if context:
            mention, root = context
            yield from self._completions(document, mention, self.index.cached(root))

    async def get_completions_async(self, document, complete_event):
        context = self._context(document)
        if context:
            mention, root = context
            snapshot = await self.index.ready(root)
            if Path(self.workdir_provider()) != root:
                return
            for completion in self._completions(document, mention, snapshot):
                yield completion


class ChatComposerCompleter(Completer):
    """Wrap the existing slash/skill completer without changing its semantics."""

    def __init__(self, delegate, *, workdir_provider, index=None) -> None:
        self.delegate = delegate
        self.mentions = FileMentionCompleter(workdir_provider=workdir_provider, index=index)

    def _choose(self, document):
        return (
            self.mentions
            if mention_at_cursor(document.text, document.cursor_position)
            else self.delegate
        )

    def get_completions(self, document, complete_event):
        yield from self._choose(document).get_completions(document, complete_event)

    async def get_completions_async(self, document, complete_event):
        async for completion in self._choose(document).get_completions_async(
            document, complete_event
        ):
            yield completion

    def close(self) -> None:
        self.mentions.index.close()
