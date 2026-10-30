from __future__ import annotations

import importlib
from typing import Any

_PUBLIC_FACADE_NAMES = {
    "_PUBLIC_LOOP_COPY_VALUES",
    "_copy_loop_globals_to_public",
    "_apply_chat_effective_mode",
    "_apply_chat_persona",
    "_handle_chat_command",
    "_handle_chat_command_impl",
    "_handle_forge_chat_command",
    "_handle_forge_chat_command_impl",
    "_sync_cli_globals",
    "_sync_loop_globals_from_public",
    "chat_impl",
    "run_impl",
}
_PUBLIC_LOOP_COPY_VALUES: dict[str, Any] = {}


def _loop_module() -> Any:
    return importlib.import_module(f"{__name__}.loop")


def _commands_module() -> Any:
    return importlib.import_module(f"{__name__}.commands")


def _copy_loop_globals_to_public(*, overwrite: bool = False) -> None:
    module_globals = globals()
    for name, value in _loop_module().__dict__.items():
        if name.startswith("__") or name in _PUBLIC_FACADE_NAMES:
            continue
        if overwrite or name not in module_globals:
            module_globals[name] = value
            _PUBLIC_LOOP_COPY_VALUES[name] = value


def _public_global_overrides() -> dict[str, Any]:
    loop = _loop_module()
    loop_globals = loop.__dict__
    overrides: dict[str, Any] = {}
    for name, value in globals().items():
        if name.startswith("__") or name in _PUBLIC_FACADE_NAMES:
            continue
        if name not in loop_globals or loop_globals[name] is value:
            continue
        if _PUBLIC_LOOP_COPY_VALUES.get(name) is value:
            continue
        if callable(value) and getattr(value, "__module__", None) == loop.__name__:
            continue
        if name in loop_globals:
            overrides[name] = value
    return overrides


def _sync_loop_globals_from_public() -> None:
    loop_globals = _loop_module().__dict__
    for name, value in globals().items():
        if name.startswith("__") or name in _PUBLIC_FACADE_NAMES:
            continue
        if _PUBLIC_LOOP_COPY_VALUES.get(name) is value:
            continue
        loop_globals[name] = value


def _sync_cli_globals(cli_mod: Any) -> None:
    public_overrides = _public_global_overrides()
    loop = _loop_module()
    loop._sync_cli_globals(cli_mod)
    for name, value in public_overrides.items():
        setattr(loop, name, value)
    _copy_loop_globals_to_public(overwrite=True)


def _handle_chat_command(*args: Any, **kwargs: Any) -> Any:
    _commands_module()._sync_command_globals(globals())
    return _commands_module()._handle_chat_command(*args, **kwargs)


def _handle_forge_chat_command(*args: Any, **kwargs: Any) -> Any:
    _commands_module()._sync_command_globals(globals())
    return _commands_module()._handle_forge_chat_command(*args, **kwargs)


def _apply_chat_effective_mode(*args: Any, **kwargs: Any) -> Any:
    _sync_loop_globals_from_public()
    return _loop_module()._apply_chat_effective_mode(*args, **kwargs)


def _apply_chat_persona(*args: Any, **kwargs: Any) -> Any:
    _sync_loop_globals_from_public()
    return _loop_module()._apply_chat_persona(*args, **kwargs)


def _handle_chat_command_impl(cli_mod: Any, *args: Any, **kwargs: Any) -> Any:
    _sync_cli_globals(cli_mod)
    return _handle_chat_command(*args, **kwargs)


def _handle_forge_chat_command_impl(cli_mod: Any, *args: Any, **kwargs: Any) -> Any:
    _sync_cli_globals(cli_mod)
    return _handle_forge_chat_command(*args, **kwargs)


def _print_chat_context_impl(cli_mod: Any, *args: Any, **kwargs: Any) -> Any:
    _sync_cli_globals(cli_mod)
    _sync_loop_globals_from_public()
    return _loop_module()._print_chat_context(*args, **kwargs)


def chat_impl(cli_mod: Any, *args: Any, **kwargs: Any) -> Any:
    _sync_cli_globals(cli_mod)
    _sync_loop_globals_from_public()
    return _loop_module().chat(*args, **kwargs)


def run_impl(cli_mod: Any, *args: Any, **kwargs: Any) -> Any:
    from ..run import run_impl as execute

    overrides = {
        name: value
        for name, value in globals().items()
        if name not in _PUBLIC_FACADE_NAMES and _PUBLIC_LOOP_COPY_VALUES.get(name) is not value
    }
    return execute(cli_mod, *args, dependency_overrides=overrides, **kwargs)


def __getattr__(name: str) -> Any:
    if name == "__all__":
        return tuple(sorted(_exported_names()))
    if name.startswith("__") and name != "__version__":
        raise AttributeError(name)
    _copy_loop_globals_to_public()
    if name in globals():
        return globals()[name]
    value = getattr(_loop_module(), name)
    globals()[name] = value
    return value


def _exported_names() -> set[str]:
    _copy_loop_globals_to_public()
    return {name for name in globals() if not name.startswith("__") or name == "__version__"}


def __dir__() -> list[str]:
    return sorted(_exported_names())
