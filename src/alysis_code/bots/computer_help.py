"""When an owl needs the person on its computer: it waits, reloads once, then asks once.

What it needs (a human check, a one-time code, a sign-in, a permission prompt or another
step) comes from the owl's own tool call, never from reading the page. Many checks clear by
themselves after a moment or a reload, so the first request of a kind on a site in a task only
waits and reloads the page, and the owl looks again; a second request parks the task with
one card for the person (Take control, then "I've done it"). The owl never types passwords
or codes and never solves human checks itself.
"""

from __future__ import annotations

import json
from urllib.parse import urldefrag, urlsplit

from .browser_sign_ins import display_name, normal_host

# What the person is asked for. "other" is any further step only they can do.
HELP_KINDS = ("human_check", "code", "sign_in", "permission", "other")
# Kinds that can clear on their own: the owl waits once before asking.
WAIT_FIRST = frozenset({"human_check", "code", "sign_in", "permission"})
# Reloading a page that waits for a one-time code can void the code already sent, so code
# pages are only waited on, never reloaded.
RELOAD_FIRST = frozenset({"human_check", "sign_in", "permission"})
WAIT_SECONDS = 8


def help_site(url) -> dict | None:
    """The site a help request is about, from the browser's own page address."""
    try:
        parts = urlsplit(str(url or ""))
        host = normal_host(parts.hostname)
    except ValueError:
        return None
    if parts.scheme not in {"http", "https"} or not host:
        return None
    return {"site": host, "site_name": display_name(host)}


class ComputerHelp:
    def __init__(self, runtime, *, wait_seconds=WAIT_SECONDS):
        self.runtime, self.wait_seconds = runtime, wait_seconds

    def _waited(self, task, kind, site) -> bool:
        with self.runtime.store.connect() as db:
            rows = db.execute(
                "SELECT data FROM events WHERE task_id=? AND kind='computer_help_waited'",
                (task["id"],),
            ).fetchall()
        return any(
            json.loads(row["data"]).get("kind") == kind
            and json.loads(row["data"]).get("site") == site
            for row in rows
        )

    def request(self, task, args, token, *, image_input=False):
        browsers = self.runtime.browser_computers
        url = browsers.page_url(task) if browsers else None
        place = help_site(url)
        site = place["site"] if place else None
        if args.kind in WAIT_FIRST and not self._waited(task, args.kind, site):
            # Recorded first, so a restart asks instead of waiting again.
            self.runtime.store.event(
                self.runtime.owner,
                task["id"],
                "computer_help_waited",
                {"kind": args.kind, "site": site},
            )
            if token.wait(self.wait_seconds):
                token.throw_if_cancelled()
            result = {
                "executed": True,
                "status": "waited",
                "kind": args.kind,
                "waited_seconds": self.wait_seconds,
                "reloaded": False,
            }
            if args.kind in RELOAD_FIRST and url:
                result["reloaded"] = True
                result["page"] = browsers.call(
                    task,
                    "browser_navigate",
                    {"url": urldefrag(url)[0]},
                    token,
                    image_input=image_input,
                )
            result["next"] = (
                "Nothing was asked of the person yet. Look at the current page or screen again. "
                "If it still needs them, call request_computer_control again with the same kind "
                "and they get one card to take control. Otherwise carry on with the task."
            )
            return result
        return self.runtime.computer_controls.offer(
            task, args.reason, help={"kind": args.kind, **(place or {})}
        )
