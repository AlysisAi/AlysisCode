"""How a bot's published reply reads, for every bot and every kind of request.

The rules describe the reply the user sees, not what the bot may do: tools,
permissions and honesty rules elsewhere still decide the work. Suggested next
messages travel beside the reply (a ``reply_suggestions`` task event), never in
its text, so the app can offer them as buttons under the latest reply.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterable

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
    "items. Give each item a short bold label and one plain sentence. Do not use headings, a title line, nested lists "
    "or emoji in conversational replies; use a table only to compare several options across the same attributes; avoid "
    "bold anywhere else.\n"
    "- A reply with distinct parts, such as the answer, a comparison table and your pick or next step, may send each "
    "part as its own chat message: put a line holding only --- between them, at most four parts. A short reply stays "
    "one message.\n"
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
    "not check at the point where they appear, instead of adding long caveats. When facts such as prices, dates or "
    "availability come from pages you checked, say in a few words how fresh they are, such as checked today on each "
    "store's page.\n"
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
    "Short updates while you work: when the person waits on work that needs tools, the app asks you for a short "
    "update at a few moments (a first line as the work starts, later what they will get, and on a long job what is "
    "done). Write one only when asked, in the same response as your tool calls, in the language your reply will use "
    "and in the person's own words for things: never a tool, device type, error message or internal feature name. "
    "Text you write beside tool calls at other times is not shown to the person, so do not narrate your steps.\n"
)

# Each update is asked for only beside the call where it is due: in a long system prompt the
# guidance above is easy to miss, and a line asked for at the moment reads like a person's.
ACK_REMINDER = (
    "If you need tools before you can answer, start this response with one short sentence to the person, in the "
    "language your reply will use, saying what you are doing now and naming the concrete thing, under twenty words, "
    "then call the tools. Describe the next step, never a result, in the person's own words for things."
)
PLAN_REMINDER = (
    "Beside this response's tool calls, write one or two sentences to the person, under forty-five words, in the "
    "language your reply will use: what they will get and its shape (a short list, a table, a file, a link), any "
    "default you are assuming, and the one check you will do before you send it. Promise only a check you will really "
    "do, then do it. If what you already told them covers this, write no text."
)
MILESTONE_REMINDER = (
    "You have been working a while without an update. If a real piece of the work is done, or your plan changed "
    "because a route was blocked, write one short line to the person beside this response's tool calls, under "
    "thirty-five words, in the language your reply will use: what is done, with numbers where they help, and what "
    "comes next. Never mention retries, errors you got past or how a tool behaved. If nothing real changed, write no text."
)
PROGRESS_NOTE_REMINDER = ACK_REMINDER

# An owl's first message, asked for by the app, never typed by the person: what it says comes
# from the owl's own setup and tools in its system prompt, not from this text.
INTRO_REQUEST = (
    "This message comes from the Alysis app, not from the person. You were just created, and this "
    "is your first message to them, before they have said anything. Write it in the language with "
    "the tag {language}. In two or three short sentences: say hello with your name, name two or "
    "three concrete everyday jobs you could take on for them right now with what you have here, in "
    "their words rather than as tools or features, and ask what they would like to start with. "
    "Warm and plain, as a person would write: no list, heading or emoji, and no claim about work "
    "you have not done. Then add the follow-up block with three short first requests they could "
    "send you."
)


def intro_request(config: dict) -> str:
    """The app's request for an owl's first message, in the language the app gave."""
    language = (config.get("intro") or {}).get("language") or "en"
    return INTRO_REQUEST.format(language=language)


# The person skipped a question the owl ended its turn on: the request goes on without the answer.
SKIPPED_QUESTION_REQUEST = (
    "This message comes from the Alysis app, not from the person. The person skipped your "
    "question {question} and did not answer it. Carry on with their request without that answer: "
    "where you needed it, choose sensible, common defaults, say in one short line which ones you "
    "chose so they can change them, and finish the work now. Do not ask that question again; ask "
    "something only if the work truly cannot go on without it. Reply in the language the person "
    "has been using with you."
)


def skipped_question_request(config: dict) -> str:
    """The app's request to carry on after the person skipped the question in config."""
    question = (config.get("skipped_question") or {}).get("question") or ""
    return SKIPPED_QUESTION_REQUEST.format(question=json.dumps(question, ensure_ascii=False))


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


# A line holding only dashes ends one chat message and starts the next.
_SEPARATOR = re.compile(r"^[ \t]*-{3,}[ \t]*$", re.M)


def update_lines(text: str | None) -> list[str]:
    """Return the chat messages an update written beside tool calls becomes, when fit to show.

    Anything long, structured or markup-like is the model thinking aloud or drafting, not a
    short update, so that part stays private.
    """
    body, _ = split_follow_ups(text or "")
    lines = []
    for part in _SEPARATOR.split(body):
        line = " ".join(part.split())
        if (
            line
            and len(line) <= MAX_PROGRESS_NOTE_CHARACTERS
            and "<" not in line
            and "```" not in line
        ):
            lines.append(line)
    return lines


def progress_note(text: str | None) -> str | None:
    """Return the first short update written beside a tool call, when it is fit to show."""
    lines = update_lines(text)
    return lines[0] if lines else None


class TurnUpdates:
    """When a turn the person waits on asks its owl for a short update, and which ones they see.

    The rhythm follows the work, never its wording: a first line as tools start; once the work
    has run a while, what the person will get and how it will be checked; on a long job, what is
    done after a quiet stretch. Text written beside tool calls at any other moment stays private,
    so step-by-step narration never reaches the chat.
    """

    PLAN_AFTER_SECONDS = 12
    QUIET_SECONDS = 90
    STEPS_BETWEEN_MILESTONES = 2
    MAX_MILESTONES = 3
    _REMINDERS = {"ack": ACK_REMINDER, "plan": PLAN_REMINDER, "milestone": MILESTONE_REMINDER}
    _LINES = {"ack": 2, "plan": 1, "milestone": 1}

    def __init__(
        self,
        *,
        watched: bool,
        started_at: float,
        said: Iterable[str] = (),
        clock: Callable[[], float] = time.time,
    ) -> None:
        """``said``: the kinds of update this task already sent, as when background work resumes it."""
        kinds = list(said)
        self.watched = watched
        self._clock = clock
        self._started = started_at
        self._acked = "ack" in kinds
        self._planned = "plan" in kinds
        self._milestones = kinds.count("milestone")
        self._last = clock()
        self._steps_at_last = 0
        self._asked: str | None = None

    def reminder(self, completed_tools: int, *, tools_offered: bool = True) -> str | None:
        """The request to add beside the next model call, or None when no update is due.

        An update is written beside tool calls, so a call that offers no tools asks for none.
        """
        self._asked = None
        if not self.watched or not tools_offered:
            return None
        now = self._clock()
        if not self._acked:
            self._asked = "ack"
        elif (
            not self._planned
            and completed_tools > 0
            and now - self._started >= self.PLAN_AFTER_SECONDS
        ):
            self._asked = "plan"
        elif (
            self._milestones < self.MAX_MILESTONES
            and now - self._last >= self.QUIET_SECONDS
            and completed_tools - self._steps_at_last >= self.STEPS_BETWEEN_MILESTONES
        ):
            self._asked = "milestone"
        return self._REMINDERS.get(self._asked)

    def take(
        self, content: str | None, completed_tools: int, *, card: bool = False
    ) -> tuple[str | None, list[str]]:
        """After a response with tool calls: the update it was asked for, and its lines to show.

        ``card``: the response opens a question or approval card; its text belongs to the card,
        so the update stays due.
        """
        kind, self._asked = self._asked, None
        if kind is None or card:
            return None, []
        if kind == "ack":
            self._acked = True
        elif kind == "plan":
            self._planned = True
        else:
            # A milestone with nothing to say waits for another quiet stretch.
            self._milestones += 1
            self._last, self._steps_at_last = self._clock(), completed_tools
        lines = update_lines(content)[: self._LINES[kind]]
        if lines:
            self._last, self._steps_at_last = self._clock(), completed_tools
        return kind, lines
