"""Next steps the desktop shows as one-click replies under a final answer.

The model writes each step for the result it just produced, in the user's language. The host only
displays them, and a click sends that exact text as the user's own next message, so a step grants
no permission and runs nothing by itself. Validation is about transport and display safety only:
it never reads meaning from the words, so every language and script is treated the same.
"""

from __future__ import annotations

import unicodedata
from typing import Any

NEXT_STEPS_MIN = 2
# Calls that only show something to the person and give the model nothing new to act on.
DISPLAY_ONLY_TOOL_NAMES = frozenset({"suggest_next_steps"})
NEXT_STEPS_MAX = 3
NEXT_STEP_MAX_CHARS = 120

# Characters that could make one step display as something else (bidirectional overrides and
# isolates). Joiners and non-joiners stay allowed: several scripts need them to spell words.
_DISPLAY_CONTROLS = frozenset(
    chr(code) for code in (*range(0x202A, 0x202F), *range(0x2066, 0x206A))
)

NEXT_STEPS_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "steps": {
            "type": "array",
            "minItems": NEXT_STEPS_MIN,
            "maxItems": NEXT_STEPS_MAX,
            "items": {"type": "string", "minLength": 1, "maxLength": NEXT_STEP_MAX_CHARS},
            "description": (
                "2-3 requests the user could send next, each specific to this result and written "
                "in the user's language."
            ),
        }
    },
    "required": ["steps"],
    "additionalProperties": False,
}

NEXT_STEPS_DESCRIPTION = (
    "Offer 2-3 short next steps as one-click buttons under your final reply. Write each as a "
    "request the user could send next, specific to this result, in the user's language; a click "
    "sends that exact text as the user's message. Call it once, before writing the final reply, "
    "whenever this turn delivered a result (an answer from your work, a file, a change), even if "
    "some steps failed or checks remain open. Skip it only when the reply needed no tools, "
    "nothing was delivered, or you are asking the user a question. Never suggest steps that "
    "send messages, buy, publish, delete, or need new permissions."
)


def _error(message: str) -> dict[str, Any]:
    return {"error": message, "error_code": "invalid_next_steps", "shown": 0}


def suggest_next_steps(args: dict[str, Any]) -> dict[str, Any]:
    raw = args.get("steps") if isinstance(args, dict) else None
    if not isinstance(raw, list):
        return _error(f"steps must be a list of {NEXT_STEPS_MIN}-{NEXT_STEPS_MAX} short requests.")
    steps: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            return _error("Each step must be text.")
        text = " ".join(unicodedata.normalize("NFC", item).split())
        if not text:
            return _error("A step is empty.")
        if any(char in _DISPLAY_CONTROLS or unicodedata.category(char) == "Cc" for char in text):
            return _error("A step contains control characters.")
        if len(text) > NEXT_STEP_MAX_CHARS:
            return _error(f"Each step must be at most {NEXT_STEP_MAX_CHARS} characters.")
        key = text.casefold()
        if key not in seen:
            seen.add(key)
            steps.append(text)
    if not NEXT_STEPS_MIN <= len(steps) <= NEXT_STEPS_MAX:
        return _error(
            f"Give {NEXT_STEPS_MIN}-{NEXT_STEPS_MAX} different steps, or skip suggestions."
        )
    return {"ok": True, "shown": len(steps), "steps": steps}
