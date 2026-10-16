from __future__ import annotations

import codecs
import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any

from .types import LLMError

# How streamed chat-completions chunks are assembled into content and tool-call
# arguments. "concat" appends each chunk as received: the OpenAI streaming
# contract, where a chunk carries only new text. "cumulative" is for a provider
# that re-sends text already received (the reply so far, or a restart of it)
# and trims each chunk with de-duplication heuristics, which also drop
# legitimate repeats from an ordinary incremental stream. "auto" resolves per
# provider and is "concat" unless the provider is known to need "cumulative".
STREAM_ASSEMBLY_AUTO = "auto"
STREAM_ASSEMBLY_CONCAT = "concat"
STREAM_ASSEMBLY_CUMULATIVE = "cumulative"
STREAM_ASSEMBLY_SETTINGS = (
    STREAM_ASSEMBLY_AUTO,
    STREAM_ASSEMBLY_CONCAT,
    STREAM_ASSEMBLY_CUMULATIVE,
)


def iter_lines_from_text_chunks(chunks: Iterator[str]) -> Iterator[str]:
    """Frame decoded chunks in linear time while preserving CR/LF boundaries."""

    fragments: list[str] = []
    pending_cr = False
    for chunk in chunks:
        if pending_cr:
            if chunk.startswith("\n"):
                fragments.append("\r\n")
                yield "".join(fragments)
                fragments.clear()
                chunk = chunk[1:]
            else:
                fragments.append("\r")
                yield "".join(fragments)
                fragments.clear()
            pending_cr = False

        start = 0
        index = 0
        while index < len(chunk):
            char = chunk[index]
            if char == "\n":
                end = index + 1
            elif char == "\r":
                fragments.append(chunk[start:index])
                if index + 1 == len(chunk):
                    pending_cr = True
                    start = len(chunk)
                    break
                end = index + 2 if chunk[index + 1] == "\n" else index + 1
                fragments.append(chunk[index:end])
                yield "".join(fragments)
                fragments.clear()
                start = end
                index = end
                continue
            else:
                index += 1
                continue
            fragments.append(chunk[start:end])
            yield "".join(fragments)
            fragments.clear()
            start = end
            index = end
        if start < len(chunk):
            fragments.append(chunk[start:])
    if pending_cr:
        fragments.append("\r")
    if fragments:
        yield "".join(fragments)


def decode_text_chunks(chunks: Iterator[bytes], *, encoding: str) -> Iterator[str]:
    """Incrementally decode byte chunks without splitting multi-byte characters."""

    decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
    for chunk in chunks:
        text = decoder.decode(chunk)
        if text:
            yield text
    trailing = decoder.decode(b"", final=True)
    if trailing:
        yield trailing


@dataclass(frozen=True)
class SSEFrame:
    """A parsed Server-Sent Events frame.

    The parser intentionally keeps this structure provider-neutral so native
    Responses, Messages, and GenerateContent streaming paths can share it.
    """

    event: str
    data: str
    event_id: str | None = None
    retry: int | None = None


def iter_sse_frames(
    lines: Iterable[str | bytes],
    *,
    done_sentinels: tuple[str, ...] = ("[DONE]",),
    include_done: bool = False,
) -> Iterator[SSEFrame]:
    """Yield SSE frames from an iterable of response lines.

    Handles `event:`, multi-line `data:`, comments, blank-line frame
    boundaries, optional `id:` / `retry:`, and `[DONE]`-style sentinels.
    """

    event = "message"
    event_id: str | None = None
    retry: int | None = None
    data_lines: list[str] = []

    def _flush() -> SSEFrame | None:
        nonlocal event, event_id, retry, data_lines
        if not data_lines and event == "message" and event_id is None and retry is None:
            return None
        frame = SSEFrame(
            event=event or "message",
            data="\n".join(data_lines),
            event_id=event_id,
            retry=retry,
        )
        event = "message"
        event_id = None
        retry = None
        data_lines = []
        return frame

    for raw_line in lines:
        if isinstance(raw_line, bytes):
            line = raw_line.decode("utf-8", errors="replace")
        else:
            line = raw_line
        line = line.rstrip("\r\n")
        if line == "":
            frame = _flush()
            if frame is None:
                continue
            if frame.data.strip() in done_sentinels and not include_done:
                return
            yield frame
            continue
        if line.startswith(":"):
            continue

        if ":" in line:
            field, value = line.split(":", 1)
            if value.startswith(" "):
                value = value[1:]
        else:
            field, value = line, ""

        if field == "event":
            event = value
        elif field == "data":
            data_lines.append(value)
        elif field == "id":
            event_id = value
        elif field == "retry":
            try:
                retry = int(value)
            except ValueError:
                retry = None

    frame = _flush()
    if frame is not None:
        if frame.data.strip() in done_sentinels and not include_done:
            return
        yield frame


def parse_sse_json_frame(frame: SSEFrame, *, stream_name: str) -> Any:
    """Parse a frame's JSON payload with a deterministic provider error."""

    try:
        return json.loads(frame.data)
    except json.JSONDecodeError as exc:
        event_suffix = f" event={frame.event!r}" if frame.event else ""
        raise LLMError(f"{stream_name} emitted malformed JSON{event_suffix}: {exc.msg}") from exc
