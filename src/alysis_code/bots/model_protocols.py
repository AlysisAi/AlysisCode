"""The model APIs an owls service can think with.

Kept apart from the service app (``app.py``), which loads every part of the service: reading the
model list (``alysis bots profiles``, asked before owls on this computer start) needs only this,
and loading the whole service for it took about as long as starting the service itself.
"""

from __future__ import annotations

from ..llm.protocols import (
    ANTHROPIC_MESSAGES_PROTOCOL,
    OPENAI_COMPAT_PROTOCOL,
    OPENAI_RESPONSES_PROTOCOL,
)

MODEL_PROTOCOLS = frozenset(
    (OPENAI_COMPAT_PROTOCOL, ANTHROPIC_MESSAGES_PROTOCOL, OPENAI_RESPONSES_PROTOCOL)
)
