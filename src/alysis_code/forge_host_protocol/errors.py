"""Errors raised at the Forge host protocol boundary."""

from __future__ import annotations


class ForgeHostProtocolError(ValueError):
    """Base class for a rejected Forge host protocol operation."""


class ProtocolValidationError(ForgeHostProtocolError):
    """A wire value does not conform without echoing attacker-controlled input."""

    def __init__(self, code: str, summary: str) -> None:
        self.code = code
        self.summary = summary
        super().__init__(f"{code}: {summary}")


class UnsupportedVersionError(ForgeHostProtocolError):
    """The peers have no mutually supported protocol version."""


class StaleIdentityError(ForgeHostProtocolError):
    """A message refers to a different session, generation, or run."""


class StaleRevisionError(ForgeHostProtocolError):
    """A command or event refers to an obsolete state revision."""


class RevisionGapError(ForgeHostProtocolError):
    """An event skipped an authoritative state revision."""


class IdempotencyConflictError(ForgeHostProtocolError):
    """An idempotency key was reused for a different semantic request."""


class EventSequenceError(ForgeHostProtocolError):
    """An event was missing, duplicated, or delivered out of order."""


class ReplayUnavailableError(ForgeHostProtocolError):
    """Replay was requested without an authenticated durable checkpoint."""


class TerminalEventError(ForgeHostProtocolError):
    """A run attempted to emit more than one terminal event."""


class ReferenceResolutionError(ForgeHostProtocolError):
    """A terminal or evidence record refers to an unknown protocol object."""


class PlanStateError(ForgeHostProtocolError):
    """A command does not match the current canonical plan or approval state."""


class ApprovalStateError(ForgeHostProtocolError):
    """An approval is unsolicited, mismatched, expired, or already consumed."""


class UnsafeSerializationError(ForgeHostProtocolError):
    """A message would expose a local path or credential material."""
