"""Static text + the model-name prettifier for the TUI welcome/footer.

Kept as pure functions so they can be unit-tested without constructing the
prompt_toolkit application.
"""

from __future__ import annotations

import re
import textwrap

from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.utils import get_cwidth

HEADING_TEXT = "Alysis Code"  # plain wordmark fallback (narrow terminals)
CREDIT_TEXT = "crafted by AlysisAI"
# The landing's separator: rows reflow at these seams on a narrow pane.
WELCOME_SEPARATOR = "  ·  "
HINT_TEXT = WELCOME_SEPARATOR.join(
    (
        "/forge for an autonomous run",
        "@ for file paths",
        "tab to switch persona",
        "/help for everything",
    )
)
# Welcome screen: full greeting. Once the conversation is underway the input
# switches to the shorter follow-up placeholder below.
INPUT_PLACEHOLDER = "I'm Alysis Code, your coding buddy — how can I help you?"
INPUT_PLACEHOLDER_FOLLOWUP = "Message Alysis Code…"
# Forge planning session: nudge the forge verbs (and that plain text becomes a
# requirement) so the input reads as the plan editor it now is.
INPUT_PLACEHOLDER_FORGE = "Forge — /goal · /task · /show · /execute plan · /done"

# Tokens that should keep a specific casing instead of naive title-casing.
_ACRONYMS = {
    "ai": "AI",
    "deepseek": "DeepSeek",
    "glm": "GLM",
    "gpt": "GPT",
    "llm": "LLM",
    "mimo": "MiMo",
    "openai": "OpenAI",
    "qwen": "Qwen",
    "xai": "xAI",
}


# Families whose vendors hyphenate the version onto the name ("GPT-6 Astra",
# "GPT-5.6 Terra", "GLM-5.3"). Everything else keeps a space ("Gemini 3.8
# Flash", "Claude Opus 5", "Kimi K3"), which matches those vendors' naming.
_HYPHENATED_VERSION_FAMILIES = frozenset({"GPT", "GLM"})

# Families whose vendors hyphenate every word of the name ("DeepSeek-V4-Flash",
# "DeepSeek-V4.1-Flash", "DeepSeek-V4-Flash-Vision-Exp").
_FULLY_HYPHENATED_FAMILIES = frozenset({"DeepSeek"})

# DeepSeek serves betas under dated ids ("deepseek-v4.1-flash-expires-on-0910",
# "v3.2_speciale_expires_on_20251215"). The expiry is routing detail, not part
# of the model's name.
_EXPIRY_SUFFIX = re.compile(r"[-_]expires[-_]on[-_]\d+$", re.IGNORECASE)


def pretty_model_label(model: str | None) -> str:
    """Turn a raw model id into a friendly footer label.

    ``deepseek-v4.1-flash`` -> ``DeepSeek-V4.1-Flash``; ``gpt-6-astra`` ->
    ``GPT-6 Astra``; ``gpt-4o`` -> ``GPT-4o``. Falls back to the raw id when
    there is nothing sensible to do.
    """
    raw = (model or "").strip()
    if not raw:
        return "model"
    name = _EXPIRY_SUFFIX.sub("", raw.rsplit("/", 1)[-1])
    if name.casefold() == "deepseek-flash":
        return "DeepSeek-V4.1-Flash"
    tokens = [t for t in re.split(r"[-_\s]+", name) if t]
    out: list[str] = []
    for token in tokens:
        low = token.lower()
        if low in _ACRONYMS:
            word = _ACRONYMS[low]
        elif token.isupper():
            word = token
        else:
            word = token[:1].upper() + token[1:]
        if out and out[-1] in _HYPHENATED_VERSION_FAMILIES and word[:1].isdigit():
            out[-1] = f"{out[-1]}-{word}"
            continue
        # Ids that cannot carry a dot spell "5.1" as "5-1" (claude-fable-5-1,
        # doubao-seed-2-1-pro): two adjacent bare numbers are one version.
        if out and out[-1].isdigit() and word.isdigit():
            out[-1] = f"{out[-1]}.{word}"
            continue
        out.append(word)
    if not out:
        return raw
    if out[0] in _FULLY_HYPHENATED_FAMILIES:
        return "-".join(out)
    return " ".join(out)


def heading_fragments() -> FormattedText:
    return FormattedText([("class:tui.heading", HEADING_TEXT)])


def hint_fragments() -> FormattedText:
    return FormattedText([("class:tui.hint", HINT_TEXT)])


def _pack(items: list[str], width: int, separator: str) -> list[str]:
    """Join ``items`` with ``separator`` into lines no wider than ``width``.

    Lines break only between items, so a phrase is never split; an item wider
    than a whole line is word-wrapped on its own.
    """
    width = max(1, width)
    lines: list[str] = []
    current = ""
    for item in items:
        joined = f"{current}{separator}{item}" if current else item
        if get_cwidth(joined) <= width:
            current = joined
            continue
        if current:
            lines.append(current)
        pieces = textwrap.wrap(item, width, break_on_hyphens=False) or [""]
        lines.extend(pieces[:-1])
        current = pieces[-1]
    if current:
        lines.append(current)
    return lines


def welcome_text_rows(width: int, *, setup_hint: str | None = None) -> list[list[tuple[str, str]]]:
    """The landing's text under the owl, reflowed to ``width`` columns.

    The wordmark and its credit share a row while they fit. The hint rows break
    only between its "·"-separated items, so a narrow pane stacks whole
    phrases instead of clipping the last one. Blank rows separate the blocks.
    """
    rows: list[list[tuple[str, str]]] = []
    if get_cwidth(HEADING_TEXT + WELCOME_SEPARATOR + CREDIT_TEXT) <= width:
        rows.append(
            [
                ("class:tui.heading", HEADING_TEXT),
                ("class:tui.credit", WELCOME_SEPARATOR + CREDIT_TEXT),
            ]
        )
    else:
        rows.extend([("class:tui.heading", line)] for line in _pack([HEADING_TEXT], width, ""))
        rows.extend([("class:tui.credit", line)] for line in _pack([CREDIT_TEXT], width, ""))
    rows.append([])
    if setup_hint:
        rows.extend(
            [("class:tui.footer.mode.warn", line)]
            for line in _pack(setup_hint.split(" · "), width, " · ")
        )
        rows.append([])
    rows.extend(
        [("class:tui.hint", line)]
        for line in _pack(HINT_TEXT.split(WELCOME_SEPARATOR), width, WELCOME_SEPARATOR)
    )
    return rows


__all__ = [
    "HEADING_TEXT",
    "HINT_TEXT",
    "INPUT_PLACEHOLDER",
    "INPUT_PLACEHOLDER_FOLLOWUP",
    "INPUT_PLACEHOLDER_FORGE",
    "WELCOME_SEPARATOR",
    "heading_fragments",
    "hint_fragments",
    "pretty_model_label",
    "welcome_text_rows",
]
