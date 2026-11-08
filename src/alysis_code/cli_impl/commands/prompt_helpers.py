# ruff: noqa: F401,F403,F405,I001
# Legacy split module: dependencies are synced by cli_surface.py.
from __future__ import annotations

import shutil

from ...cancellation import InteractiveCancellationToken
from .cli_common import *


def _chat_prompt_session_completion_count(mode: str) -> int:
    from ..chat_slash_completer import max_completions_for_mode

    # Chat keeps one PromptSession while the user can enter/leave Forge, so
    # reserve enough room for the larger static command surface at creation.
    return max(
        max_completions_for_mode(mode),
        max_completions_for_mode("chat"),
        max_completions_for_mode("forge"),
    )


def _chat_prompt_completion_menu_height(mode: str) -> int:
    completion_count = _chat_prompt_session_completion_count(mode)
    terminal_lines = shutil.get_terminal_size((80, 24)).lines
    return min(completion_count, max(8, int(terminal_lines * 0.6)))


def _handle_chat_command(
    *,
    input_text: str,
    root: Path,
    session: Any,
    pending_images: list[str],
    console: Console,
    forge_state: _ForgeChatState,
    record_local_command: bool = True,
) -> str | _ChatExecutionRequest:
    from ..chat import _handle_chat_command_impl

    return _handle_chat_command_impl(
        _cli_module_for_legacy_impl(),
        input_text=input_text,
        root=root,
        session=session,
        pending_images=pending_images,
        console=console,
        forge_state=forge_state,
        record_local_command=record_local_command,
    )


def _maybe_make_chat_prompt_session(
    *,
    console: Console,
    root: Path,
    pending_images: list[str],
    forge_state: _ForgeChatState,
    session: Any | None = None,
) -> Any | None:
    if _patchable("_is_non_interactive_terminal", _is_non_interactive_terminal)():
        return None

    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.application import run_in_terminal
        from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
        from prompt_toolkit.history import FileHistory
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.styles import Style

        from ..chat_slash_completer import ChatSlashCompleter
    except Exception:
        return None

    kb = KeyBindings()

    def _run_paste_image() -> None:
        def _do_paste() -> None:
            try:
                saved = _patchable("paste_clipboard_image", paste_clipboard_image)(
                    root=root, output_path=None
                )
            except ClipboardError as e:
                console.print(f"[red]Clipboard error:[/red] {e}")
                return
            pending_images.append(os.fspath(saved))
            console.print(f"Pasted clipboard image: {saved}")

        run_in_terminal(_do_paste)

    @kb.add("escape")
    def _escape_hotkey(event: Any) -> None:
        # Esc closes an open completion menu; otherwise it is the plain
        # paste-clipboard-image hotkey (same as Ctrl+B / Ctrl+Alt+V).
        current_buffer = getattr(event, "current_buffer", None)
        if getattr(current_buffer, "complete_state", None) is not None:
            cancel_completion = getattr(current_buffer, "cancel_completion", None)
            if callable(cancel_completion):
                cancel_completion()
            return
        _run_paste_image()

    @kb.add("c-b")
    def _paste_image_hotkey(_event: Any) -> None:
        _run_paste_image()

    @kb.add("escape", "c-v")
    def _paste_image_ctrl_alt_v_hotkey(_event: Any) -> None:
        _run_paste_image()

    @kb.add("c-l")
    def _clear_screen(_event: Any) -> None:
        run_in_terminal(console.clear)

    @kb.add("tab")
    def _accept_suggestion_or_complete(event: Any) -> None:
        _accept_chat_suggestion_or_complete(event)

    def _completion_mode() -> str:
        return "forge" if _is_forge_ui_mode(forge_state.ui_mode) else "chat"

    completer = ChatSlashCompleter(
        mode_provider=_completion_mode,
        skill_names_provider=lambda: sorted(
            str(name) for name in getattr(session, "skill_registry", {})
        ),
    )
    prompt_session_kwargs = {
        "key_bindings": kb,
        "auto_suggest": AutoSuggestFromHistory(),
        "completer": completer,
        "complete_while_typing": True,
        "reserve_space_for_menu": _chat_prompt_completion_menu_height(_completion_mode()),
        # Match the full-screen chat's dark panel and green selection. Without
        # an explicit style, the ordinary prompt uses prompt_toolkit's defaults.
        "style": Style.from_dict(
            {
                "completion-menu": "bg:#0d1117",
                "completion-menu.completion": "bg:#0d1117 #c9d1d9",
                "completion-menu.completion.current": "bg:#21262d bold #3fb950",
                "completion-menu.meta.completion": "bg:#0d1117 #6e7681",
                "completion-menu.meta.completion.current": "bg:#21262d #c9d1d9",
                "scrollbar.background": "bg:#1a1a1a",
                "scrollbar.button": "bg:#3fb950",
            }
        ),
    }
    try:
        history_path = default_chat_history_path()
        history_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_session_kwargs["history"] = FileHistory(os.fspath(history_path))
    except OSError:
        pass

    def _prompt_session_supports_cpr(candidate: Any) -> bool:
        output = getattr(getattr(candidate, "app", None), "output", None)
        if output is None:
            return False
        responds_to_cpr = getattr(output, "responds_to_cpr", None)
        if callable(responds_to_cpr):
            try:
                responds_to_cpr = responds_to_cpr()
            except Exception:
                responds_to_cpr = None
        return bool(responds_to_cpr)

    try:
        prompt_session = PromptSession(
            erase_when_done=True,
            **prompt_session_kwargs,
        )
        prompt_session._alysis_erase_when_done = True  # type: ignore[attr-defined]
    except TypeError as e:
        if "erase_when_done" not in str(e):
            raise
        prompt_session = PromptSession(**prompt_session_kwargs)
        prompt_session._alysis_erase_when_done = False  # type: ignore[attr-defined]

    _apply_chat_prompt_escape_sequence_timeout(prompt_session)

    if not _prompt_session_supports_cpr(prompt_session):
        return None
    return prompt_session


def _apply_chat_prompt_escape_sequence_timeout(prompt_session: Any) -> None:
    """
    Give VT100 escape sequences a slightly longer grace window before treating
    the leading byte as a standalone Esc hotkey.

    This keeps the existing plain-Esc behavior, but reduces accidental
    paste-image triggers on slower terminals where arrow-key sequences can
    arrive late.
    """

    app = getattr(prompt_session, "app", None)
    if app is None:
        return
    current = getattr(app, "ttimeoutlen", None)
    if isinstance(current, (int, float)):
        app.ttimeoutlen = max(float(current), _CHAT_PROMPT_ESCAPE_SEQUENCE_TIMEOUT_S)
        return
    app.ttimeoutlen = _CHAT_PROMPT_ESCAPE_SEQUENCE_TIMEOUT_S


def _cancel_interactive_turn(token: InteractiveCancellationToken) -> None:
    cancel_nonblocking = getattr(token, "cancel_nonblocking", None)
    if callable(cancel_nonblocking):
        cancel_nonblocking()
        return
    token.cancel()


@contextmanager
def _chat_turn_interrupt_monitor(
    *,
    cancellation_token: InteractiveCancellationToken | None = None,
) -> Any:
    token = cancellation_token or InteractiveCancellationToken()
    # Only enable raw-key monitoring while a turn is running in interactive POSIX terminals.
    if not (sys.stdin.isatty() and os.name == "posix"):
        try:
            yield token
        except KeyboardInterrupt:
            _cancel_interactive_turn(token)
            raise
        return
    try:
        import select
        import signal
        import termios
        import tty
    except Exception:
        try:
            yield token
        except KeyboardInterrupt:
            _cancel_interactive_turn(token)
            raise
        return

    stop_event = threading.Event()

    def _watch_escape() -> None:
        try:
            fd = sys.stdin.fileno()
            original = termios.tcgetattr(fd)
        except Exception:
            return
        cbreak_active = False

        def _enter_cbreak() -> None:
            nonlocal cbreak_active
            if cbreak_active:
                return
            tty.setcbreak(fd)
            cbreak_active = True

        def _restore_terminal() -> None:
            nonlocal cbreak_active
            if not cbreak_active:
                return
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, original)
            except Exception:
                pass
            cbreak_active = False

        try:
            while not stop_event.is_set():
                if is_interactive_prompt_active():
                    if not is_interactive_prompt_terminal_owner():
                        _restore_terminal()
                    stop_event.wait(0.05)
                    continue
                _enter_cbreak()
                try:
                    readable, _, _ = select.select([fd], [], [], 0.1)
                except Exception:
                    return
                if not readable:
                    continue
                # A nested approval prompt may have claimed stdin while this
                # watcher was already blocked in select(). Leave the pending
                # byte untouched so the prompt can read it after we restore the
                # terminal to cooked mode.
                if is_interactive_prompt_active():
                    continue
                try:
                    chunk = os.read(fd, 1)
                except Exception:
                    return
                if chunk != b"\x1b":
                    continue
                # Publish cancellation before raising SIGINT in the main
                # thread so AgentSession's finally block can cancel accepted
                # child runs through the existing coordinator path.
                _cancel_interactive_turn(token)
                try:
                    os.kill(os.getpid(), signal.SIGINT)
                except Exception:
                    try:
                        signal.raise_signal(signal.SIGINT)
                    except Exception:
                        return
                return
        finally:
            _restore_terminal()

    watcher = threading.Thread(
        target=_watch_escape,
        name="alysis-chat-escape-interrupt",
        daemon=True,
    )
    watcher.start()
    try:
        yield token
    except KeyboardInterrupt:
        _cancel_interactive_turn(token)
        raise
    finally:
        stop_event.set()
        watcher.join(timeout=0.25)


__all__ = [name for name in globals() if (not name.startswith("__") or name == "__version__")]
