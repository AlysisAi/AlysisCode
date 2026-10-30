from __future__ import annotations

import math
import re
from functools import lru_cache

from .computer_use_attribution import encoder_acquisition

_TOKEN_ENCODING_NAME = "cl100k_base"
_TOKEN_LIKE_RE = re.compile(r"\w+|[^\w\s]", re.UNICODE)
_WORD_RE = re.compile(r"\w+", re.UNICODE)


@lru_cache(maxsize=1)
def _token_encoder() -> object | None:
    with encoder_acquisition() as span:
        try:
            import tiktoken  # type: ignore[import-not-found]

            encoder = tiktoken.get_encoding(_TOKEN_ENCODING_NAME)
            if span is not None:
                span.acquisition = "acquired" if encoder is not None else "unknown"
            return encoder
        except Exception:
            if span is not None:
                span.acquisition = "fallback"
            return None


def _fallback_estimate_tokens(text: str) -> int:
    pieces = _TOKEN_LIKE_RE.findall(text)
    if not pieces:
        return 1 if text else 0

    total = 0
    for piece in pieces:
        if _WORD_RE.fullmatch(piece):
            total += max(1, math.ceil(len(piece.encode("utf-8")) / 4))
        else:
            total += 1
    return total


def estimate_tokens(text: str) -> int:
    if not text:
        return 0

    encoder = _token_encoder()
    if encoder is not None:
        try:
            return len(encoder.encode(text))  # type: ignore[attr-defined]
        except Exception:
            pass

    return _fallback_estimate_tokens(text)


def trim_text_to_budget(text: str, max_tokens: int) -> tuple[str, bool]:
    if max_tokens <= 0:
        return "", bool(text)

    current = estimate_tokens(text)
    if current <= max_tokens:
        return text, False

    marker = "\n\n...[TRUNCATED FOR TOKEN BUDGET]...\n\n"
    marker_tokens = max(1, estimate_tokens(marker))

    if max_tokens <= marker_tokens + 2:
        return marker, True

    avg_chars_per_token = max(1, len(text) // max(1, current))
    available = max_tokens - marker_tokens
    head_tokens = available // 2
    tail_tokens = available - head_tokens
    head_chars = max(32, head_tokens * avg_chars_per_token)
    tail_chars = max(32, tail_tokens * avg_chars_per_token)

    def _build() -> str:
        if head_chars + tail_chars >= len(text):
            return text
        return text[:head_chars] + marker + text[-tail_chars:]

    candidate = _build()
    while estimate_tokens(candidate) > max_tokens and (head_chars > 32 or tail_chars > 32):
        head_chars = max(32, int(head_chars * 0.9))
        tail_chars = max(32, int(tail_chars * 0.9))
        candidate = _build()

    return candidate, True


def compute_input_budget(cap: object, safety_margin: int = 512) -> int:
    context = int(getattr(cap, "context_window_tokens", 8192))
    output = int(getattr(cap, "max_output_tokens", 2048))
    if output >= context > 0:
        # Shared-window metadata (e.g. the Kimi Code ids, where max_tokens may
        # be raised up to the full context): there is no fixed output
        # reservation, so subtracting the whole window would leave the 512-token
        # floor as the input budget on a 1M-context model — the fresh-session
        # "context: 0% left" footer bug. Reserve a conservative response
        # allowance instead of the entire window.
        output = min(output, max(4096, context // 8))
    budget = context - output - max(0, int(safety_margin))
    return max(512, budget)
