"""Fixed package-owned workflow for protected Computer Use continuations."""

from __future__ import annotations

from importlib import resources

from ..computer_use_platform_policy import (
    UNAVAILABLE_MESSAGE,
    computer_use_blocked,
    mac_w7_computer_use_available,
)
from ..frontmatter_utils import split_frontmatter


def computer_use_workflow_instructions() -> str:
    """Load only the shipped resource, never a user/project skill registry entry.

    No caller-controlled name or path is accepted. Missing package data fails
    closed instead of falling back to a same-named discovered skill.
    """
    if computer_use_blocked():
        return UNAVAILABLE_MESSAGE
    text = (
        resources.files("alysis_code.skills")
        .joinpath("bundled", "computer-use", "SKILL.md")
        .read_text(encoding="utf-8")
    )
    _, instructions = split_frontmatter(text)
    if not instructions.strip():
        raise ValueError("The packaged Computer Use workflow is empty.")
    return "\n\n<computer_use_workflow>\n" + instructions.strip() + "\n</computer_use_workflow>\n"


def _entry_section(text: str, mode: str) -> str:
    start, end = f"<!-- entry:{mode} -->", f"<!-- /entry:{mode} -->"
    if text.count(start) != 1 or text.count(end) != 1:
        raise ValueError("Missing packaged Computer Use entry guidance")
    value = text.split(start)[1].split(end)[0].strip()
    if not value:
        raise ValueError("Invalid packaged Computer Use entry guidance")
    return value


def ordinary_entry_guidance(*, resolved: bool, application_lifecycle_v1: bool = False) -> str:
    """Render only the package-owned route section; no registry or caller path.

    The legacy section is the original route text. It is rendered unchanged only
    when this request's host-captured discovery negotiated application lifecycle
    v1 and the live Mac W7 predicate admits native key/type/scroll/drag plans
    (the same predicate as the computer_start action enum). Otherwise the fixed
    restrictive fragments replace the offered ones. Absent flags fail closed.
    """
    if computer_use_blocked() or (resolved and not mac_w7_computer_use_available()):
        return UNAVAILABLE_MESSAGE
    text = (
        resources.files("alysis_code.skills")
        .joinpath("bundled", "computer-use", "references", "ordinary-entry.md")
        .read_text(encoding="utf-8")
    )
    value = _entry_section(text, "resolved" if resolved else "legacy")
    if resolved:
        if len(value.encode("utf-8")) > 4096:
            raise ValueError("Invalid packaged Computer Use entry guidance")
        return value
    native_input = mac_w7_computer_use_available()
    lifecycle = application_lifecycle_v1 is True and native_input
    for fragment, replacement in (
        ("quit", None if lifecycle else "quit:no-lifecycle" if native_input else "quit:legacy"),
        ("quit-operates", None if lifecycle else "quit-operates:unavailable"),
        ("native-input", None if native_input else "native-input:unavailable"),
    ):
        offered = _entry_section(text, f"{fragment}:offered")
        if value.count(offered) != 1:
            raise ValueError("Invalid packaged Computer Use entry guidance")
        if replacement is not None:
            value = value.replace(offered, _entry_section(text, replacement))
    return value


_V1_ROUTE_NOTE = (
    "This protected session negotiated only the legacy v1 interaction route. Interaction v2 "
    "(semantic/focused_input/visual_point), input_mode/focused-target, quit_application and new "
    "window selection/reopen routes are not offered. Conditional workflow guidance for those routes "
    "cannot enable them. Use only the advertised v1 tools; never fall back after effects."
)
_NO_LIFECYCLE_NOTE = (
    "This protected session did not negotiate application lifecycle v1: quit_application is not "
    "offered, and conditional guidance for it cannot enable it."
)
_NO_WINDOW_SELECTION_NOTE = (
    "This protected session did not negotiate window selection v1: list_windows/window_ref selection "
    "is not offered, and conditional guidance for it cannot enable it."
)


def computer_use_route_availability(
    *, interaction_v2: bool, application_lifecycle_v1: bool, window_selection_v1: bool
) -> str:
    """Restrictive note for routes this protected session did NOT negotiate.

    Inputs are only the host binding's negotiated versions. When interaction v2,
    lifecycle v1 and window selection v1 are all negotiated, nothing is added and
    the workflow is exactly the original conditional text.
    """
    if computer_use_blocked():
        return ""
    if interaction_v2 is not True:
        notes = [_V1_ROUTE_NOTE]
    else:
        notes = [
            note
            for negotiated, note in (
                (application_lifecycle_v1, _NO_LIFECYCLE_NOTE),
                (window_selection_v1, _NO_WINDOW_SELECTION_NOTE),
            )
            if negotiated is not True
        ]
    if not notes:
        return ""
    return (
        "\n<computer_use_route_availability>\n"
        + " ".join(notes)
        + "\n</computer_use_route_availability>\n"
    )
