"""Accepted prompt history, scoped to the OS account/data profile and workspace.

Only accepted prose is stored (including queues and steering); slash commands,
rejected submissions and abandoned drafts are excluded. Pasted
payloads retain opaque boundaries; attachment chips retain explicit path metadata.
The newest 200 entries / 2 MiB are retained. This is separate from classic chat's
global FileHistory, whose command/draft policy cannot safely be imported here.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ...config import default_chat_history_path
from .attachments import Attachment


@contextmanager
def _history_lock(path: Path, timeout: float = 0.15):
    """Nonblocking OS lock attempts with a short upper bound on UI waiting."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if not handle.tell():
                handle.write(b"0")
                handle.flush()
        else:
            import fcntl
        deadline = time.monotonic() + timeout
        while True:
            try:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Prompt history is busy") from None
                time.sleep(0.01)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@dataclass(frozen=True)
class PromptEntry:
    text: str
    attachments: tuple[Attachment, ...] = ()
    pastes: tuple[tuple[int, str], ...] = ()

    @property
    def searchable_text(self) -> str:
        payloads = dict(self.pastes)
        return re.sub(
            r"\[pasted #([1-9]\d*) \+\d+ lines\]",
            lambda match: payloads.get(int(match[1]), match[0]),
            self.text,
        )

    def encoded(self) -> dict:
        return {
            "text": self.text,
            "pastes": self.pastes,
            "attachments": [
                {"kind": a.kind, "path": str(a.path), "index": a.index, "size_bytes": a.size_bytes}
                for a in self.attachments
            ],
        }


class PromptHistory:
    def __init__(
        self,
        workspace: Path,
        *,
        limit: int = 200,
        byte_limit: int = 2 * 1024 * 1024,
        persistence: bool = True,
    ):
        # No-log callers opt out before construction: neither existing history
        # nor opaque prompt/paste/attachment payloads touch durable storage.
        self.persistence = persistence
        scope = os.path.normcase(str(workspace.resolve()))
        digest = hashlib.sha256(scope.encode("utf-8")).hexdigest()
        self.path = default_chat_history_path().parent / "prompt_history" / f"{digest}.json"
        self.limit, self.byte_limit = limit, byte_limit
        self.entries: list[PromptEntry] = []
        self.index: int | None = None
        self.draft = None
        self._pending: list[PromptEntry] = []
        self.persistence_error: str | None = None
        self._load()

    def _load(self) -> None:
        if not self.persistence:
            return
        try:
            with self.path.open("rb") as stream:
                data = stream.read(self.byte_limit + 1)
            if len(data) > self.byte_limit:
                return
            raw = json.loads(data.decode("utf-8"))
            self.entries = [
                PromptEntry(
                    str(e["text"]),
                    tuple(
                        Attachment(
                            kind=a["kind"],
                            path=Path(a["path"]),
                            index=int(a["index"]),
                            size_bytes=a.get("size_bytes"),
                        )
                        for a in e.get("attachments", [])
                    ),
                    tuple((int(key), str(value)) for key, value in e.get("pastes", [])),
                )
                for e in raw[-self.limit :]
            ]
        except (OSError, ValueError, KeyError, TypeError):
            self.entries = []

    def accept(self, entry: PromptEntry) -> None:
        self.index, self.draft = None, None
        # Consecutive identical resubmissions remain a single recall entry.
        if self.entries and self.entries[-1] == entry:
            return
        if len(json.dumps([entry.encoded()], ensure_ascii=False).encode("utf-8")) > self.byte_limit:
            return
        self._pending.append(entry)
        self.entries.append(entry)
        self._prune()
        if not self.persistence:
            self._pending.clear()
            return
        temporary = None
        try:
            with _history_lock(self.path):
                # Refresh under the lock: never replace another TUI's accepted
                # prompts with this instance's stale startup snapshot.
                self._load()
                for pending in self._pending:
                    if not self.entries or self.entries[-1] != pending:
                        self.entries.append(pending)
                payload = self._prune()
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=self.path.parent, delete=False
                ) as stream:
                    temporary = Path(stream.name)
                    stream.write(payload)
                temporary.replace(self.path)
                self._pending.clear()
                self.persistence_error = None
        except OSError as exc:
            self.persistence_error = str(exc)
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def _prune(self) -> str:
        self.entries = self.entries[-self.limit :]
        payload = json.dumps([e.encoded() for e in self.entries], ensure_ascii=False)
        while self.entries and len(payload.encode("utf-8")) > self.byte_limit:
            self.entries.pop(0)
            payload = json.dumps([e.encoded() for e in self.entries], ensure_ascii=False)
        # Pending unsaved entries are bounded independently, too.
        self._pending = [entry for entry in self._pending if entry in self.entries][-self.limit :]
        return payload

    def search(self, query: str) -> list[tuple[int, PromptEntry]]:
        words = query.casefold().split()
        return [
            (i, e)
            for i, e in reversed(list(enumerate(self.entries)))
            if all(word in e.searchable_text.casefold() for word in words)
        ]
