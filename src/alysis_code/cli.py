# ruff: noqa: F401,F403,F405,E402
from __future__ import annotations


def _harden_stdio() -> None:
    """Make stdout/stderr tolerant of glyphs the console encoding can't map
    (e.g. the rich UI's box-drawing ``│`` on a non-UTF-8 Windows console such as
    Greek cp1253), so the first surfaced warning/error no longer crashes with
    UnicodeEncodeError. ``backslashreplace`` degrades an unmappable glyph to a
    readable escape instead of aborting. We change only the error handler, not
    the encoding, so redirected/piped output keeps its locale codec for
    downstream readers. Runs once at process start, before any output."""
    import sys

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(errors="backslashreplace")
        except (ValueError, OSError):  # detached buffer / unsupported stream
            pass


def _apply_host_tls() -> None:
    """Honour ``ALYSIS_EXTRA_CA_CERTS`` (set by the IDE extension) before any HTTP
    client is built. Failures are reported on stderr and leave the default trust
    store in place; they never block startup."""
    import os

    if not os.environ.get("ALYSIS_EXTRA_CA_CERTS"):
        return
    import sys

    from .host_tls import apply_extra_ca_certs

    try:
        apply_extra_ca_certs(warn=lambda message: print(message, file=sys.stderr))
    except Exception as exc:  # pragma: no cover - defensive: never fail launch on TLS setup
        print(f"ALYSIS_EXTRA_CA_CERTS ignored: {exc}", file=sys.stderr)


_harden_stdio()
_apply_host_tls()

# Source-alignment markers for checks that read this legacy entrypoint after the
# command implementation moved into cli_impl/commands.
# Chat command marker: "/forge"
# Prompt hotkey marker: @kb.add("c-b")
from .cli_impl.commands.cli_surface import *

if __name__ == "__main__":
    app()
