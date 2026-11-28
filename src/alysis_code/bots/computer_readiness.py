"""Where an owl's own computer stands while it can't be used, as a typed value.

An owl on the person's computer gets its own computer (a private VM) once the desktop app has
downloaded the computer's image and, on Windows, the person said Yes once in a Windows prompt
(the app's windowsOwlsComputer.ts and macOwlsComputer.ts). Until then this service runs without
one, and only the app knows how far that setup is. The app writes it to ``SETUP_FILE`` in this
service's folder whenever it changes (owlsComputerDownload.ts), and each turn of an owl without a
computer reads it (``read_setup``). The owl is told the state as data, never as the app's words,
so it can say in plain words, in the person's language, where its computer is and what will work
once it is ready, point at the card on its page that shows it, and meanwhile do what needs no
computer instead of stopping.

A computer that is set up can still fail to start, on this device or in the Cloud. A provider's
typed refusal (``ComputeCreationRefused.code``) and a start that failed are given the same kind
of state (``for_refusal``, ``START_FAILED``) in the step's result.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

# The app's setup states (owlsComputerDownload.ts, readinessOf). Keep the two lists in step.
DOWNLOADING = "downloading"
UNPACKING = "unpacking"
DOWNLOAD_PAUSED = "download_paused"
WAITING_FOR_APPROVAL = "waiting_for_windows_approval"
RESTART_NEEDED = "restart_needed"
VIRTUALIZATION_OFF = "virtualization_off"
NOT_ENOUGH_DISK_SPACE = "not_enough_disk_space"
SETUP_FAILED = "setup_failed"
NOT_SET_UP = "not_set_up"
NOT_SUPPORTED_HERE = "not_supported_here"
# Set up: this service moves onto it as soon as no owl here is working (the app restarts it).
READY = "ready"
# Only from a computer that could not start (this device or the Cloud).
ALL_COMPUTERS_BUSY = "all_computers_busy"
START_FAILED = "start_failed"

SETUP_STATES = frozenset(
    {
        DOWNLOADING,
        UNPACKING,
        DOWNLOAD_PAUSED,
        WAITING_FOR_APPROVAL,
        RESTART_NEEDED,
        VIRTUALIZATION_OFF,
        NOT_ENOUGH_DISK_SPACE,
        SETUP_FAILED,
        NOT_SET_UP,
        NOT_SUPPORTED_HERE,
        READY,
    }
)
STATES = SETUP_STATES | {ALL_COMPUTERS_BUSY, START_FAILED}
# States whose setup progress the app reports, as a whole percent.
WITH_PROGRESS = frozenset({DOWNLOADING, UNPACKING, DOWNLOAD_PAUSED})

# The app writes it into this service's folder (the bootstrap ``directory``).
SETUP_FILE = "computer-setup.json"
SETUP_SCHEMA = 1
# The file is one small object; anything bigger is not the app's.
MAX_SETUP_BYTES = 1024

# What each state means, for the model (the owl says it in the person's own words). Never the
# app's text: the owl's reply follows the conversation's language.
MEANING = {
    DOWNLOADING: "is being downloaded onto the person's computer, once",
    UNPACKING: "is downloaded and being unpacked",
    DOWNLOAD_PAUSED: "is part downloaded and paused; Continue on the card picks it up from there",
    WAITING_FOR_APPROVAL: "is downloaded and waits for the person's OK: Windows asks once to "
    "allow it (Finish setup on the card shows the prompt again; choose Yes)",
    RESTART_NEEDED: "needs the person's PC to restart once to finish setting up",
    VIRTUALIZATION_OFF: "can't run on this PC until virtualization is turned on in its BIOS or "
    "UEFI settings; owls in Alysis Cloud can work meanwhile",
    NOT_ENOUGH_DISK_SPACE: "needs more free disk space before it can be set up or started",
    SETUP_FAILED: "stopped setting up; Try again or Continue on the card resumes it",
    NOT_SET_UP: "is not set up yet; one download from the card sets it up",
    NOT_SUPPORTED_HERE: "can't run on this computer; owls in Alysis Cloud have one",
    READY: "is set up and connects as soon as no owl on this computer is working",
    ALL_COMPUTERS_BUSY: "waits for a free computer: every computer this place runs is busy "
    "with other work",
    START_FAILED: "could not start this time",
}

# The app's private computer card shows these on the owl's own page, with the step each needs
# (Continue, Finish setup, Try again); it hides itself once set up or where none can run.
ON_THE_CARD = SETUP_STATES - {READY, NOT_SUPPORTED_HERE}

# Said with a computer that is not connected yet: what it adds once it is.
ONCE_READY = (
    "once ready it adds websites it can click and type on, apps, commands and files that need a "
    "computer"
)

# The model's part, in a step's result that could not start the computer.
START_NOTE = (
    "Nothing was done on the computer. Tell the person in plain words that its computer is not "
    "available now and why, and what to do if the reason says, then deliver what works without "
    "it (web search and page reads need no computer) instead of stopping."
)


@dataclass(frozen=True)
class Readiness:
    """One of ``STATES``, with the setup's progress as a whole percent when the app knows it."""

    state: str
    percent: int | None = None

    def __post_init__(self):
        if self.state not in STATES:
            raise ValueError("Unknown computer state.")
        if self.percent is not None and (
            type(self.percent) is not int
            or not 0 <= self.percent <= 100
            or self.state not in WITH_PROGRESS
        ):
            raise ValueError("Invalid computer setup progress.")

    def data(self) -> dict:
        return {
            "state": self.state,
            **({"percent": self.percent} if self.percent is not None else {}),
        }


def parse_setup(value: object) -> Readiness | None:
    """The app's setup state, or None when ``value`` is not one (an older or a newer app)."""
    if not isinstance(value, dict) or value.get("schema") != SETUP_SCHEMA:
        return None
    if set(value) - {"schema", "state", "percent"} or value.get("state") not in SETUP_STATES:
        return None
    try:
        return Readiness(value["state"], value.get("percent"))
    except ValueError:
        return None


def read_setup(path: Path) -> Readiness | None:
    """Where the app's setup of this device's computer stands, read now.

    None when the app has written nothing (an app from before this file, or a Linux service with
    its own desktop) or the file is not one the app writes: then the owl only knows its computer
    is unavailable, as before.
    """
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_SETUP_BYTES + 1)
    except OSError:
        # Not written (yet), or met while the app replaces it with one rename (on Windows briefly
        # unopenable): the setup is unknown for this turn, and the next one reads it again.
        return None
    if len(raw) > MAX_SETUP_BYTES:
        return None
    try:
        return parse_setup(json.loads(raw.decode("utf-8")))
    except (UnicodeDecodeError, ValueError):
        return None


# Typed refusals of the providers that start computers (local_hcs.py, local_vm.py), by code.
_REFUSALS = {
    "windows_computer_service_missing": WAITING_FOR_APPROVAL,
    "windows_computer_service_outdated": WAITING_FOR_APPROVAL,
    "virtualization_off": VIRTUALIZATION_OFF,
    "insufficient_local_storage": NOT_ENOUGH_DISK_SPACE,
    "local_computer_limit": ALL_COMPUTERS_BUSY,
}


def for_refusal(code: str) -> Readiness:
    """The state a provider's typed refusal leaves the computer in; START_FAILED for a code
    this module does not know yet."""
    return Readiness(_REFUSALS.get(code, START_FAILED))


def task_state(readiness: Readiness) -> str:
    """The task-state entry for an owl whose service has no computer yet (BotRuntime._context)."""
    percent = f" ({readiness.percent}% so far)" if readiness.percent is not None else ""
    card = (
        " The card on this owl's page shows it and any step it needs."
        if readiness.state in ON_THE_CARD
        else ""
    )
    return (
        "\nThis owl's own computer, not connected yet (its setup, reference data): "
        + json.dumps(readiness.data(), separators=(",", ":"))
        + f". It {MEANING[readiness.state]}{percent}; {ONCE_READY}.{card}"
    )


# Said when a computer that is set up did not start (its cause is in diagnostics, never here:
# a provider's error text is not written for people).
START_FAILED_REASON = (
    "This owl's computer could not start, so nothing was done on it. Files it saved are kept; a "
    "later step can try to start it again."
)


def start_failed(error: BaseException) -> dict | None:
    """The result of a step whose computer did not start because of ``error``, or None when
    ``error`` is not a failed start: an uncertain outcome (a computer may exist) and anything
    but a provider's error keep their own handling."""
    from .compute import ComputeError, ComputeUncertain

    if not isinstance(error, ComputeError) or isinstance(error, ComputeUncertain):
        return None
    return start_receipt(Readiness(START_FAILED), START_FAILED_REASON)


def start_receipt(readiness: Readiness, reason: str) -> dict:
    """The result of a step whose computer could not start: nothing ran, and why, as data."""
    return {
        "error": reason,
        "executed": False,
        "computer_created": False,
        "computer": readiness.data(),
        "meaning": f"This owl's computer {MEANING[readiness.state]}.",
        "note": START_NOTE,
    }
