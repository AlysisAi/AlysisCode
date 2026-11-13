"""How a bot's published reply reads, for every bot and every kind of request.

The rules describe the reply the user sees, not what the bot may do: tools,
permissions and honesty rules elsewhere still decide the work. Suggested next
messages travel beside the reply (a ``reply_suggestions`` task event), never in
its text, so the app can offer them as buttons under the latest reply.
"""

from __future__ import annotations

import re

REPLY_STYLE = (
    "How to write the reply the user reads:\n"
    "- In the final reply, lead with the answer or the outcome in the first sentence: what you found, made or need. "
    "Report results, not process: the final reply does not narrate tool calls, internal steps or checks unless the user "
    "needs them.\n"
    "- Match the length to the request. A greeting, a simple question or a confirmation gets one to three sentences. "
    "A substantial answer is a short opening line plus at most five to seven short items. Save a file only when the user "
    "asks for a document or file; then the full content belongs in the file, and the reply names it and sums it up in a "
    "few lines.\n"
    "- Write plainly, in the user's language and terms: short sentences, everyday words, no jargon the user did not use. "
    "Describe what you can do through the user's goals and this bot's purpose, with a few concrete examples, never as an "
    "inventory of tools or internal features such as the library, working notes, memory review, peer bots, native or "
    "offline computers, routines or approvals. Name a feature only when the user has to act on it. Refer to a saved file "
    "by its name, not by where it is stored.\n"
    "- When asked what you can do, give three or four concrete examples drawn from this bot's purpose and the user's "
    "goals, then ask what they want to start with. Leave out general abilities such as saving files, using computers, "
    "routines, reminders, memory or other bots unless the purpose is about them.\n"
    "- Use formatting only when it makes the reply easier to follow: numbered steps for a sequence, bullets for parallel "
    "items. Give each item a short bold label and one plain sentence. Do not use headings, a title line, nested lists, "
    "horizontal rules or emoji in conversational replies; use a table only to compare several options across the same "
    "attributes; avoid bold anywhere else.\n"
    "- When the answer relies on web pages or documents you actually opened, cite each one once, right after the claim it "
    "supports, as a markdown link whose text is the page title or publisher, such as ([Page title](https://...)). Never "
    "paste a bare URL or add a separate list of sources, and never link a page you did not open: name the source instead.\n"
    "- Ask only when one missing answer would change what you produce, and at most once per request. Ask it with "
    "ask_clarification as one short question (about twelve words at most) about one thing, with two or three short "
    "options. The card shows the question and its options under your text, so the reply repeats neither: it is a natural "
    "lead-in of one or two sentences, such as why the answer matters or the default you would otherwise use, plus at most "
    "three short bullets for other details that would help; never say that you are waiting or where to answer. Do not add "
    "a generic answer, plan or examples while you wait.\n"
    "- Once the user has answered a question, or replies without the details you asked for, do the work: make reasonable "
    "assumptions, state them in a short clause, deliver, and say in one sentence what would make it more specific. Do "
    "not ask again for details you can reasonably assume.\n"
    "- Be honest in a few words where it matters: mark placeholders, invented examples, estimates and anything you could "
    "not check at the point where they appear, instead of adding long caveats.\n"
    "- Put text the user will send or post as their own, such as a message, post, email or reply, in a quote block "
    "(lines starting with >) under a short label, one block per draft, so it stands apart from your words.\n"
    "- When you drafted or prepared something the user might think you already sent, posted, bought or changed, say in "
    "a few words that it has not happened yet.\n"
    "- End with at most one next step or one question. Do not end with a list of everything else you could do.\n"
)

FOLLOW_UPS_GUIDANCE = (
    "After a reply that completes or advances the user's request, add two or three follow-up messages the user is "
    "likely to send next, as a final block:\n"
    "<follow_ups>\n- first suggestion\n- second suggestion\n</follow_ups>\n"
    "Write each one as the user would type it (two to eight words, in the user's language): a complete message they can "
    "send as it is, with no placeholders or brackets, and a distinct, natural next request you could carry out with the "
    "current tools and permissions. The app shows them as buttons and removes "
    "the block from your reply. Leave the block out when you asked a clarification question, when nothing useful "
    "follows, and in scheduled, routine or peer turns. Put nothing else in the block.\n"
)

PROGRESS_NOTE_GUIDANCE = (
    "When you need tools before you can answer, begin your first tool-calling response with one short sentence to the "
    'user, in the same response as the tool call and in the language your reply will use, saying what you are doing, under twenty words, such as "Checking '
    'this week\'s numbers now." or "On it, drafting the template now." It appears in the chat right away while you '
    "work. Describe the next step, never a result, in the user's own words for things: never a tool, device type or "
    "internal feature name. Write it once; later tool calls need no text.\n"
)

# Sent only beside the first call of a turn the user is watching: in a long system prompt the
# guidance above is easy to miss, and the line is worth most before the first tool runs.
PROGRESS_NOTE_REMINDER = (
    "If you need tools before you can answer, start this response with one short sentence to the user, in the "
    "language your reply will use, saying what "
    "you are doing, under twenty words, then call the tools. Describe the next step, never a result, in the user's "
    "own words for things."
)

MAX_FOLLOW_UPS = 3
MAX_PROGRESS_NOTE_CHARACTERS = 240
MAX_FOLLOW_UP_CHARACTERS = 120

# A block may be unterminated when the model stops early; it then runs to the end.
_BLOCK = re.compile(r"\s*<follow_ups>(.*?)(?:</follow_ups>|\Z)", re.S | re.I)
_MARKER = re.compile(r"^\s*(?:[-*•]|\d+[.)])(?:\s+|$)")


def split_follow_ups(text: str | None) -> tuple[str, list[str]]:
    """Return the reply without follow-up blocks, and the last block's suggestions.

    A model turn with no text (``None``) reads as an empty reply, not an error.
    """
    text = text or ""
    blocks = list(_BLOCK.finditer(text))
    if not blocks:
        return text, []
    body = _BLOCK.sub("", text).rstrip()
    items: list[str] = []
    for line in blocks[-1].group(1).splitlines():
        item = _MARKER.sub("", line).strip().strip("\"'“”").strip()
        if not item or len(item) > MAX_FOLLOW_UP_CHARACTERS or "<" in item:
            continue
        if item.casefold() not in {existing.casefold() for existing in items}:
            items.append(item)
        if len(items) == MAX_FOLLOW_UPS:
            break
    return body, items


def progress_note(text: str | None) -> str | None:
    """Return the short line written beside a tool call, when it is fit to show the user.

    Anything long, structured or markup-like is the model thinking aloud or drafting, not a
    status line, so it stays private.
    """
    body, _ = split_follow_ups(text or "")
    note = " ".join(body.split())
    if not note or len(note) > MAX_PROGRESS_NOTE_CHARACTERS or "<" in note or "```" in note:
        return None
    return note
