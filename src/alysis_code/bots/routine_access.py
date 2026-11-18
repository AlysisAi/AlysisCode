"""What a routine may use beyond chat: the owl's offline computer, its online browser, websites.

The owl declares what a routine's job needs in structured fields when it proposes or edits the
routine, and the person sees them. The person allows them once, for that routine only. Each run
then gets exactly what was allowed when it was queued, without a card. Anything else a run asks
for pauses the routine with a plain reason until the person allows it too. Nothing here reads
the routine's words: needs and grants are compared as data.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import AnyHttpUrl, BeforeValidator, Field, model_validator

from .models import Input

# A routine works on a handful of sites; a longer list is a sign it is not one routine.
MAX_SITES = 10


def website_origin(value: object) -> str:
    """One exact website origin (scheme://host[:port]); anything else says what to change."""
    if not isinstance(value, str) or any(ord(char) < 33 for char in value):
        raise ValueError("Use an HTTP(S) website origin without whitespace.")
    url = AnyHttpUrl(value)
    if "*" in url.host:
        raise ValueError("Use one exact website origin, without wildcards.")
    if url.username is not None or url.password is not None or url.path not in (None, "/"):
        raise ValueError("Use a website origin without credentials or a page path.")
    if url.query is not None or url.fragment is not None:
        raise ValueError("Use a website origin without a query or fragment.")
    return str(url).rstrip("/")


Site = Annotated[str, BeforeValidator(website_origin), Field(max_length=2048)]


class RoutineNeeds(Input):
    """The owl's computer, browser and websites a routine's runs use."""

    offline_computer: bool = Field(
        default=False,
        strict=True,
        description="True when each run needs commands, applications or generated binary files on the owl's private offline computer (no internet).",
    )
    online_browser: bool = Field(
        default=False,
        strict=True,
        description="True when each run reads websites in the owl's online browser.",
    )
    sites: list[Site] = Field(
        default_factory=list,
        max_length=MAX_SITES,
        description="Exact website origins (scheme://host[:port], no path) where each run must click, type, upload or download. Reading needs only online_browser.",
    )

    @model_validator(mode="after")
    def _normalized(self):
        # One way to write the same needs, so what the person saw compares equal to what they
        # allow: each site once, in order, and websites are used through the browser.
        self.sites = sorted(set(self.sites))
        if self.sites:
            self.online_browser = True
        return self

    @property
    def empty(self) -> bool:
        return not (self.offline_computer or self.online_browser or self.sites)

    def covers(self, other: RoutineNeeds) -> bool:
        """Everything `other` asks for is in this."""
        return (
            (self.offline_computer or not other.offline_computer)
            and (self.online_browser or not other.online_browser)
            and set(other.sites) <= set(self.sites)
        )

    def union(self, *others: RoutineNeeds | None) -> RoutineNeeds:
        items = [self, *(other for other in others if other is not None)]
        return RoutineNeeds(
            offline_computer=any(item.offline_computer for item in items),
            online_browser=any(item.online_browser for item in items),
            sites=[site for item in items for site in item.sites],
        )

    def beyond(self, allowed: RoutineNeeds | None) -> RoutineNeeds:
        """What this asks for that `allowed` doesn't cover."""
        allowed = allowed or RoutineNeeds()
        return RoutineNeeds(
            offline_computer=self.offline_computer and not allowed.offline_computer,
            # A new site is asked for as a site, not as the browser it is used through.
            online_browser=self.online_browser and not allowed.online_browser and not self.sites,
            sites=[site for site in self.sites if site not in allowed.sites],
        )


def parse_needs(value: str | dict | None) -> RoutineNeeds | None:
    """Stored needs (JSON text or a dict), or None when there are none."""
    if value is None:
        return None
    needs = (
        RoutineNeeds.model_validate_json(value)
        if isinstance(value, str)
        else RoutineNeeds.model_validate(value)
    )
    return None if needs.empty else needs


def approval_reason(need: RoutineNeeds) -> str:
    """Why a run paused its routine, in plain words for older apps and for the owl.

    Apps with the routine_access capability show their own translation from the structured
    request instead.
    """
    parts = []
    if need.sites:
        parts.append(
            ("a new site: " if len(need.sites) == 1 else "new sites: ") + ", ".join(need.sites)
        )
    if need.offline_computer:
        parts.append("the owl's offline computer")
    if need.online_browser and not need.sites:
        parts.append("the owl's online browser")
    return (
        "Needs approval for "
        + "; ".join(parts)
        + ". Allow it for this routine in the app, then run it again."
    )


class RoutineAccessDecision(Input):
    """The person allows what a routine needs, exactly as their app showed it."""

    expected_revision: int = Field(ge=1)
    allow: RoutineNeeds
    # Allow and turn on: one click for a routine that is off or paused.
    turn_on: bool = False
    # Allow and run again: also run the routine once now (the Run now request ID).
    run_request_id: str | None = Field(default=None, min_length=8, max_length=100)
