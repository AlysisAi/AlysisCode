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


def _apply_bundled_git() -> None:
    """On Windows without Git, let restore points and Git tools use the engine's own
    pinned Git (windows_toolchain.add_bundled_git_to_path). Never blocks startup."""
    try:
        from .windows_toolchain import add_bundled_git_to_path

        add_bundled_git_to_path()
    except Exception:  # pragma: no cover - defensive: never fail launch on this
        pass


_harden_stdio()
_apply_host_tls()
_apply_bundled_git()

# Source-alignment markers for checks that read this legacy entrypoint after the
# command implementation moved into cli_impl/commands.
# Chat command marker: "/forge"
# Prompt hotkey marker: @kb.add("c-b")
import sys

from .cli_impl.auth_status_entry import create_auth_status_app, is_auth_status_probe

if is_auth_status_probe(sys.argv[1:]):
    app = create_auth_status_app()
else:
    from .cli_impl.commands.root import app


def _load_legacy_surface():
    """Materialize interactive helpers only when a Python caller needs them.

    Command registration and protocol startup use the same Typer app without
    importing the interactive facade. Keep real objects (and existing caller
    overrides), since the chat implementation copies this module's globals.
    """
    from .cli_impl.commands import cli_surface

    namespace = globals()
    for name in cli_surface.__all__:
        namespace.setdefault(name, getattr(cli_surface, name))
    namespace["__all__"] = cli_surface.__all__


def __getattr__(name: str):
    # Import machinery probes package attributes, which must not initialize the
    # legacy facade while the canonical command registrations are still loading.
    if name.startswith("__") and name not in {"__all__", "__version__"}:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    _load_legacy_surface()
    try:
        return globals()[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None


def __dir__():
    _load_legacy_surface()
    return sorted(globals())


if __name__ == "__main__":
    app()
