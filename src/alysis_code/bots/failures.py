"""Why an owl's turn stopped: one closed kind the desktop app turns into a way forward.

The kind is decided where the turn fails, from structured data only: the exception's type,
the model service's HTTP status and its closed error code, and whether the owl thinks with the
person's Alysis account. The task's error text stays the technical detail people can copy; it
is never read to decide the kind, so the same failure gets the same kind in every language.
"""

from __future__ import annotations

import sqlite3

# The Alysis account's sign-in was refused or has expired.
SIGN_IN = "sign_in"
# The model, or the person's own connection to it, refused or could not do the work.
MODEL = "model"
# A limit of the person's Alysis plan.
PLAN_LIMIT = "plan_limit"
# The owl's own computer.
COMPUTER = "computer"
# The connection to the model service dropped or timed out.
NETWORK = "network"
# A service the owl needs was busy, unavailable or restarted (model service, storage, runtime).
SERVICE = "service"
# The request needed more than one task may use (its context or model calls).
TASK_LIMIT = "task_limit"
UNKNOWN = "unknown"

KINDS = frozenset({SIGN_IN, MODEL, PLAN_LIMIT, COMPUTER, NETWORK, SERVICE, TASK_LIMIT, UNKNOWN})

# Provider error codes that say the credentials themselves were refused.
_CREDENTIAL_CODES = frozenset({"invalid_api_key", "authentication_error"})


def failure(kind: str, *, status: int | None = None, code: str | None = None) -> dict:
    """The structured cause stored with a failed task; status and code only from a provider."""
    if kind not in KINDS:
        raise ValueError("Unknown failure kind.")
    result: dict = {"kind": kind}
    if status is not None:
        result["status"] = status
    if code:
        result["code"] = code
    return result


# Why a turn a restart cut off was not started again (Store.recover): a step it took changed
# something outside the owl's own records (acted) or may have (unconfirmed), or it had already
# been started again once (already_rerun).
NOT_RERUN = frozenset({"acted", "unconfirmed", "already_rerun"})


def restart_failure(not_rerun: str, step: str | None = None) -> dict:
    """The cause stored with a turn a restart cut off and that was not started again: a service
    cause, why it was not started again, and the step that stopped it when one did (its tool name,
    which the app words)."""
    if not_rerun not in NOT_RERUN:
        raise ValueError("Unknown reason for not starting a turn again.")
    return {**failure(SERVICE), "not_rerun": not_rerun, **({"step": step} if step else {})}


def with_kind(exc: BaseException, kind: str) -> BaseException:
    """Mark an exception, where it is raised, with the failure kind it stands for."""
    if kind not in KINDS:
        raise ValueError("Unknown failure kind.")
    exc.failure_kind = kind  # type: ignore[attr-defined]
    return exc


def _chain(exc: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(chain) < 16:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ if current.__cause__ is not None else current.__context__
    return chain


def _status(chain: list[BaseException]) -> int | None:
    for item in chain:
        for name in ("status_code", "provider_status_code"):
            value = getattr(item, name, None)
            if type(value) is int and 100 <= value <= 599:
                return value
    return None


def _code(chain: list[BaseException]) -> str | None:
    # Only the closed vocabulary set_safe_provider_error_fields keeps; never a provider's text.
    for item in chain:
        value = getattr(item, "provider_error_code", None)
        if isinstance(value, str) and value and value != "none":
            return value
    return None


def classify(exc: BaseException, *, account: bool, transient: bool) -> dict:
    """The kind of an exception that ended a turn.

    ``account``: the owl thinks with the person's Alysis account, so refused credentials mean
    signing in again; with the person's own key they mean fixing the model connection.
    ``transient``: the model call was retried until the runtime gave up (runtime policy).
    """
    # Imported here: the store reads this module's kinds without loading model clients.
    import httpx

    from ..llm.openai_compat import alysis_plan_limit_message
    from ..llm.rate_limit_metadata import HARD_QUOTA_CODES
    from ..llm.types import LLMError
    from .compute import ComputeCreationRefused
    from .desktop_linux import DesktopError
    from .postgres import SharedDatabaseError
    from .store import LimitReached

    chain = _chain(exc)
    # A kind set where the failure was raised wins, even under a wrapping exception.
    marked = next(
        (
            getattr(item, "failure_kind", None)
            for item in chain
            if getattr(item, "failure_kind", None) in KINDS
        ),
        None,
    )
    if marked:
        return failure(marked)
    status, code = _status(chain), _code(chain)
    if alysis_plan_limit_message(exc):
        return failure(PLAN_LIMIT, status=status, code=code)
    if status == 401 or code in _CREDENTIAL_CODES:
        return failure(SIGN_IN if account else MODEL, status=status, code=code)
    if any(
        isinstance(item, (httpx.TransportError, TimeoutError, ConnectionError)) for item in chain
    ):
        return failure(NETWORK, status=status, code=code)
    if transient or any(
        isinstance(item, (sqlite3.OperationalError, SharedDatabaseError)) for item in chain
    ):
        return failure(SERVICE, status=status, code=code)
    if any(isinstance(item, (DesktopError, ComputeCreationRefused)) for item in chain):
        return failure(COMPUTER)
    if any(isinstance(item, LimitReached) for item in chain):
        return failure(TASK_LIMIT)
    if isinstance(exc, LLMError) or code in HARD_QUOTA_CODES:
        # The model's own answer or refusal: a bad request for this model, a model that is not
        # there, a quota on the person's own key, or replies it could not finish.
        return failure(MODEL, status=status, code=code)
    return failure(UNKNOWN)
