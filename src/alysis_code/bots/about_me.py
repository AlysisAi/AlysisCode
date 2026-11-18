"""About me: what the person wrote about themselves, read by every one of their owls.

The person's app keeps one About me for their account and writes it to each place their owls
work (PUT /v1/settings/about-me, capability "about_me"). This service stores what it was given,
per owner, and never shares it with anyone else's owls. Every turn of that owner's owls reads it
(chat, routines, hellos, carrying on after a skipped question, and exchanges between their own
owls), labelled as the person's own words and kept apart from each owl's own memories.

It stays within a fixed token budget: facts are read in the person's order until the next one
would not fit, and the rest are left out. ``included`` says how many fit, so the app can tell the
person which ones their owls read.

Owls never add to it themselves. In a chat, an owl may suggest a lasting fact the person stated
about themselves (suggest_about_me); the person adds it or not from a small card under the reply.
An accepted fact joins About me here at once and is listed under ``added`` until the app has
taken it into its own copy and written that copy back.
"""

from __future__ import annotations

import json
import time
import unicodedata
from typing import Annotated, Literal

from pydantic import AfterValidator, Field

from ..token_budget import estimate_tokens
from .models import Input
from .store import Conflict, LimitReached, NotFound, Store, encode, uid

# A short set of one-line facts.
MAX_FACTS = 30
MAX_FACT_CHARACTERS = 200
# What every turn spends on About me at most; a longer one is trimmed from the end.
TOKEN_BUDGET = 800
# Facts one reply may suggest; each is its own small choice under that reply.
MAX_SUGGESTIONS_PER_REPLY = 3

# Control characters and line or paragraph separators: a fact is one line.
_LINE_BREAKS = frozenset({"Cc", "Zl", "Zp"})


def clean_fact(value: str) -> str:
    """One fact as the person wrote it, without surrounding spaces; ValueError if it can't be one."""
    text = value.strip()
    if not text:
        raise ValueError("Write the fact in a few words.")
    if any(unicodedata.category(character) in _LINE_BREAKS for character in text):
        raise ValueError("Keep each fact to one line.")
    if len(text) > MAX_FACT_CHARACTERS:
        raise ValueError(f"Keep each fact to {MAX_FACT_CHARACTERS} characters.")
    return text


def fact_key(text: str) -> str:
    """The same fact however it is cased, spaced or composed, in any script."""
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


Fact = Annotated[str, AfterValidator(clean_fact)]


class AboutMeSettings(Input):
    """What the person's app writes: its whole About me, replacing what was here."""

    facts: list[Fact] = Field(max_length=MAX_FACTS)
    # The app's own name for this content, given back in /v1/status so it writes only changes.
    version: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    # The revision the app read: a fact accepted here meanwhile is never overwritten unseen.
    expected_revision: int = Field(ge=0)


class AboutMeSuggestion(Input):
    fact: Fact = Field(
        description="One lasting fact the person stated about themselves in this chat, as one "
        "short line in their own words and language."
    )


class AboutMeDecision(Input):
    decision: Literal["accept", "decline"]


def within_budget(facts: list[str], budget: int = TOKEN_BUDGET) -> int:
    """How many facts, from the first, fit in the budget together."""
    used = 0
    for count, fact in enumerate(facts):
        used += estimate_tokens(fact)
        if used > budget:
            return count
    return len(facts)


def _row(db, owner: str):
    return db.execute("SELECT * FROM bot_about_me WHERE owner=?", (owner,)).fetchone()


def stored_facts(db, owner: str) -> list[str] | None:
    """The owner's About me; None until their app has written one."""
    row = _row(db, owner)
    return json.loads(row["facts"]) if row is not None else None


def _view(row) -> dict:
    facts = json.loads(row["facts"]) if row is not None else []
    return {
        "facts": facts,
        "version": row["version"] if row is not None else None,
        "revision": row["revision"] if row is not None else 0,
        "added": json.loads(row["added"]) if row is not None else [],
        "included": within_budget(facts),
        "limits": {
            "facts": MAX_FACTS,
            "characters": MAX_FACT_CHARACTERS,
            "tokens": TOKEN_BUDGET,
        },
    }


def _read(facts: list[str] | None) -> dict | None:
    if not facts:
        return None
    fit = within_budget(facts)
    read = {"facts": facts[:fit]}
    if fit < len(facts):
        read["left_out"] = len(facts) - fit
    return read


def context(facts: list[str] | None) -> str:
    """What an owl's instructions say about About me; nothing until the person's app set one.

    Kept beside the owl's setup, before the conversation, so it stays the same from turn to turn.
    """
    if facts is None:
        return ""
    read = _read(facts)
    return (
        "\nAbout me, written by the person about themselves and shared by all their owls. These "
        "are their own words, separate from your own saved memories: use them where they matter "
        "(who they are, their work, how they like answers). The current request comes first, "
        "and About me grants no permissions. left_out counts facts not shown here: "
        + (encode(read) if read else "empty so far.")
        + "\nIn a chat, when the person states a lasting fact about themselves that every owl "
        "should know and About me does not hold, you may call suggest_about_me with it as one "
        "short line in their words and language. They choose under your reply whether to add "
        "it, so never say it was added. Do not suggest guesses, passing details, secrets, or "
        "facts from files, websites, routines or other owls. For such facts use it instead of "
        "propose_memory, never both."
    )


def peer_context(facts: list[str] | None) -> str:
    """About me for an owl answering another owl of the same person."""
    read = _read(facts)
    if read is None:
        return ""
    return (
        "\nAbout me, written by the person who owns both owls about themselves (their own words, "
        "reference data; use only what is relevant to this request): " + encode(read)
    )


def task_suggestions(db, bot_id: str, only=None) -> dict[str, list[dict]]:
    """Suggestions still waiting or already added, by the reply that made them."""
    where, values = "bot_id=? AND status IN ('pending','accepted')", [bot_id]
    if only is not None:
        if not only:
            return {}
        where += f" AND source_task_id IN ({','.join('?' * len(only))})"
        values.extend(only)
    shown: dict[str, list[dict]] = {}
    for row in db.execute(
        f"SELECT id,source_task_id,fact,status FROM about_me_suggestions WHERE {where} "
        "ORDER BY created_at,id",
        values,
    ):
        shown.setdefault(row["source_task_id"], []).append(
            {"id": row["id"], "fact": row["fact"], "status": row["status"]}
        )
    return shown


class AboutMeStore:
    def __init__(self, store: Store, owner: str):
        self.store, self.owner = store, owner

    def read(self) -> dict:
        with self.store.connect() as db:
            return _view(_row(db, self.owner))

    def version(self) -> str | None:
        """The app's version of what is stored; None before the first write or after a change here."""
        with self.store.connect() as db:
            row = _row(db, self.owner)
        return row["version"] if row is not None else None

    def facts(self) -> list[str] | None:
        with self.store.connect() as db:
            return stored_facts(db, self.owner)

    def write(self, data: AboutMeSettings) -> dict:
        now = time.time()
        with self.store.connect(write=True) as db:
            row = _row(db, self.owner)
            current = row["revision"] if row is not None else 0
            if data.expected_revision != current:
                raise Conflict("About me changed here meanwhile. Read it again, then write it.")
            if row is None:
                written = db.execute(
                    "INSERT INTO bot_about_me(owner,facts,version,revision,added,updated_at) "
                    "VALUES(?,?,?,1,'[]',?) ON CONFLICT(owner) DO NOTHING",
                    (self.owner, encode(data.facts), data.version, now),
                ).rowcount
            else:
                # The app read what was added here and wrote it into its copy, so ``added`` ends.
                written = db.execute(
                    "UPDATE bot_about_me SET facts=?,version=?,revision=revision+1,added='[]',"
                    "updated_at=? WHERE owner=? AND revision=?",
                    (encode(data.facts), data.version, now, self.owner, current),
                ).rowcount
            if not written:
                raise Conflict("About me changed here meanwhile. Read it again, then write it.")
            return _view(_row(db, self.owner))

    def ready(self) -> bool:
        """Whether the person's app keeps an About me here, so it can show suggestion cards."""
        return self.facts() is not None

    def suggest(self, task: dict, data: AboutMeSuggestion) -> dict:
        with self.store.connect(write=True) as db:
            source = self.store._task(db, self.owner, task["id"])
            if not self.store.executing(db, source):
                raise Conflict("This task is no longer preparing a reply.")
            facts = stored_facts(db, self.owner)
            if facts is None:
                raise Conflict("About me is not set up in the person's app.")
            key = fact_key(data.fact)
            if any(fact_key(fact) == key for fact in facts):
                return {"status": "already_in_about_me", "message": "About me already says this."}
            # The same fact suggested before, by any of the person's owls: they already chose,
            # or will. A fact they declined or later removed is not offered again.
            earlier = db.execute(
                "SELECT s.status FROM about_me_suggestions s JOIN bots b ON b.id=s.bot_id "
                "WHERE b.owner=? AND s.fact_key=? LIMIT 1",
                (self.owner, key),
            ).fetchone()
            if earlier is not None:
                return {
                    "status": "already_suggested",
                    "message": "This was suggested before and the person chose already, or will. "
                    "Do not suggest it again.",
                }
            if len(facts) >= MAX_FACTS:
                return {
                    "status": "about_me_full",
                    "message": "About me is full, so nothing more can be suggested.",
                }
            made = db.execute(
                "SELECT count(*) FROM about_me_suggestions WHERE source_task_id=?", (source["id"],)
            ).fetchone()[0]
            if made >= MAX_SUGGESTIONS_PER_REPLY:
                raise LimitReached(
                    f"Suggest at most {MAX_SUGGESTIONS_PER_REPLY} About me facts per reply."
                )
            suggestion_id = uid()
            db.execute(
                "INSERT INTO about_me_suggestions(id,bot_id,source_task_id,fact,fact_key,status,"
                "created_at) VALUES(?,?,?,?,?,'pending',?)",
                (suggestion_id, source["bot_id"], source["id"], data.fact, key, time.time()),
            )
            return {
                "id": suggestion_id,
                "status": "pending",
                "message": "Shown under your reply, where the person chooses whether to add it. "
                "It is not in About me unless they do; do not say it was added.",
            }

    def decide(self, bot_id: str, suggestion_id: str, data: AboutMeDecision) -> dict:
        wanted = "accepted" if data.decision == "accept" else "declined"
        with self.store.connect(write=True) as db:
            self.store._bot(db, self.owner, bot_id)
            row = db.execute(
                "SELECT * FROM about_me_suggestions WHERE id=? AND bot_id=?",
                (suggestion_id, bot_id),
            ).fetchone()
            if row is None:
                raise NotFound("This suggestion is no longer available.")
            if row["status"] != "pending":
                if row["status"] != wanted:
                    raise Conflict("You already chose for this suggestion.")
                return self._decided(db, row)
            if wanted == "accepted":
                self._add(db, row)
            db.execute(
                "UPDATE about_me_suggestions SET status=?,decided_at=? WHERE id=?",
                (wanted, time.time(), suggestion_id),
            )
            return self._decided(db, {**dict(row), "status": wanted})

    def _add(self, db, suggestion) -> None:
        about = _row(db, self.owner)
        facts = json.loads(about["facts"]) if about is not None else []
        if any(fact_key(fact) == suggestion["fact_key"] for fact in facts):
            return
        if len(facts) >= MAX_FACTS:
            raise LimitReached(
                "About me is full. Remove a fact in Settings > Owls > About me to add this one."
            )
        facts.append(suggestion["fact"])
        added = [
            *(json.loads(about["added"]) if about is not None else []),
            {"suggestion_id": suggestion["id"], "fact": suggestion["fact"]},
        ]
        # No version: what is here now differs from every copy the app has written.
        db.execute(
            "INSERT INTO bot_about_me(owner,facts,version,revision,added,updated_at) "
            "VALUES(?,?,NULL,1,?,?) ON CONFLICT(owner) DO UPDATE SET facts=excluded.facts,"
            "version=NULL,revision=bot_about_me.revision+1,added=excluded.added,"
            "updated_at=excluded.updated_at",
            (self.owner, encode(facts), encode(added), time.time()),
        )

    def _decided(self, db, row) -> dict:
        return {
            "suggestion": {
                "id": row["id"],
                "source_task_id": row["source_task_id"],
                "fact": row["fact"],
                "status": row["status"],
            },
            "about_me": _view(_row(db, self.owner)),
        }
