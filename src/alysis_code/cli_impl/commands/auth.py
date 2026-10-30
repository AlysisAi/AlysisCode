from __future__ import annotations

import json
from typing import Any, NoReturn

import typer
from rich.table import Table

from ...agent_runtimes.service import (
    RuntimeConnectionError,
    activate_runtime,
    login_runtime,
    logout_runtime,
    runtime_connection_snapshot,
)
from ...auth_diagnostics import credential_backend_fields
from ...config import AppConfig, ConfigError, config_transaction, load_config, save_config
from ...profiles import (
    SUBSCRIPTION_SELECTION_REQUIRED_KEY,
    ProfileSpec,
    add_profile,
    get_active_profile,
    get_profile,
    set_active_profile,
    subscription_selection_supported,
    update_active_profile_defaults,
)
from ...provider_auth import (
    ProviderAuthError,
    create_provider_auth,
    provider_auth_setup_options,
)
from . import _patchable
from ._shared import JSON_OPTION_HELP, _console

auth_app = typer.Typer(
    add_completion=False,
    help="Manage secure AI subscription connections.",
)

ALYSIS_LOGIN_CONNECTION_ID = "alysis"


def _status_payload(
    *,
    connection: str,
    authenticated: bool,
    account_label: str | None = None,
    credential_scope: str | None = None,
    method: str | None = None,
    detail: str | None = None,
    transport: str | None = None,
    error: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build the auth status contract a supervising app can read.

    ``authenticated`` is data, not an outcome: a false value is still a
    successful probe, so the caller never has to disambiguate it from a failure
    of the command itself. The keyring fields explain why a spawned process can
    see different auth state than the terminal the user logged in from.
    """

    payload: dict[str, Any] = {
        "connection": connection,
        "authenticated": bool(authenticated),
        "account_label": account_label,
        "credential_scope": credential_scope,
        "method": method,
        "detail": detail,
        "transport": transport,
        "error": error,
    }
    payload.update(credential_backend_fields())
    payload.update(extra)
    return payload


def _connected_credential_scope(status: Any) -> str | None:
    """Return scope from the same validated snapshot as the account status."""

    if not bool(status.connected) or not bool(status.verified):
        return None
    scope = str(getattr(status, "credential_scope", None) or "").strip()
    return scope or None


def native_provider_status_payload(provider_id: str) -> dict[str, Any]:
    """Read a fresh provider-owned status for CLI and IDE callers; never cache identity."""
    try:
        adapter = create_provider_auth(provider_id)
        status = adapter.account_status()
    except ProviderAuthError as exc:
        return _status_payload(
            connection=provider_id, authenticated=False, verified=False, error=str(exc)
        )
    return _status_payload(
        connection=provider_id,
        authenticated=status.connected,
        account_label=status.account_label,
        credential_scope=_connected_credential_scope(status),
        detail=status.detail,
        transport=f"native Alysis Code client ({adapter.protocol})",
        display_name=adapter.display_name,
        verified=bool(status.verified),
    )


def _emit_json(payload: Any) -> None:
    """Write exactly one JSON value to stdout."""

    typer.echo(json.dumps(payload, sort_keys=True))


def _auth_command_error(
    message: str,
    *,
    json_output: bool,
    code: int,
    cause: BaseException | None = None,
) -> NoReturn:
    if json_output:
        _emit_json({"ok": False, "error": message})
    else:
        _console().print(f"[red]Authentication error:[/red] {message}")
    if cause is None:
        raise typer.Exit(code=code)
    raise typer.Exit(code=code) from cause


def _print_credential_backend_note(console: Any) -> None:
    """Say so when credentials are not protected by the OS keyring.

    Silent degradation is what makes auth state look inconsistent between a
    terminal and a spawned process, so an interactive user gets told too.
    """

    fields = credential_backend_fields()
    if fields["keyring_available"]:
        return
    console.print(
        f"[yellow]OS keyring unavailable[/yellow] "
        f"(backend: {fields['keyring_backend'] or 'unresolved'}) — credentials use the "
        f"{fields['credential_fallback']} key store instead. "
        "Run `alysis doctor auth` for details."
    )


def login_connection_rows() -> list[tuple[str, str, str]]:
    """Return every account connection offered by the unified login picker."""

    rows = [
        (
            ALYSIS_LOGIN_CONNECTION_ID,
            "Alysis Code account",
            "Connect Alysis Code-hosted model access and account entitlements.",
        )
    ]
    rows.extend(
        (option.id, option.label, option.description) for option in provider_auth_setup_options()
    )
    return rows


def login_connection_interactively(
    connection_id: str,
    *,
    console: Any | None = None,
) -> None:
    """Run one connection's browser flow outside the alternate-screen TUI."""

    resolved = str(connection_id or "").strip()
    output = console or _console()
    if resolved == ALYSIS_LOGIN_CONNECTION_ID:
        from ... import account_login

        cfg = _patchable("load_config", load_config)()
        try:
            result = account_login.login(
                cfg,
                output_write=lambda message: output.print(message, highlight=False),
            )
        except (account_login.AlysisLoginError, ConfigError) as exc:
            output.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
        who = f" as [bold]{result.email}[/bold]" if result.email else ""
        output.print(f"[green]Logged in{who}.[/green] Your Alysis Code account is connected.")
        if result.model:
            output.print(
                f"Active profile: [bold]{result.profile_name}[/bold] · default model: "
                f"[bold]{result.model}[/bold]"
            )
        else:
            output.print(
                f"Active profile: [bold]{result.profile_name}[/bold] · choose a model in /config."
            )
        return
    known = {row[0] for row in login_connection_rows()}
    if resolved not in known:
        choices = ", ".join(row[0] for row in login_connection_rows())
        output.print(
            f"[red]Unknown login connection:[/red] {resolved or '(empty)'}. Available: {choices}."
        )
        raise typer.Exit(code=2)
    login_provider_interactively(runtime_id=resolved)


@auth_app.command("list")
def auth_list(
    json_output: bool = typer.Option(False, "--json", help=JSON_OPTION_HELP),
) -> None:
    """List supported subscription connections and account state."""

    cfg = _patchable("load_config", load_config)()
    if json_output is True:
        _emit_json(_auth_list_payload(cfg))
        return
    table = Table(title="AI subscription connections")
    table.add_column("connection")
    table.add_column("availability")
    table.add_column("account")
    table.add_column("active")
    for option in provider_auth_setup_options():
        if _is_direct_provider(option.id):
            try:
                status = create_provider_auth(option.id).account_status()
                installation = "built in"
                account = _account_status_label(status.connected, status.detail)
            except ProviderAuthError as exc:
                installation = "built in"
                account = str(exc)
            active = "yes" if _active_auth_provider(cfg) == option.id else ""
            table.add_row(option.label, installation, account, active)
            continue
        try:
            snapshot = runtime_connection_snapshot(cfg, option.id)
            installation = snapshot.probe.version or (
                "available" if snapshot.probe.available else "missing"
            )
            account = _account_status_label(snapshot.account.authenticated, snapshot.account.detail)
        except RuntimeConnectionError as exc:
            installation = "unavailable"
            account = str(exc)
        active = (
            "yes"
            if cfg.execution.backend == "delegated" and cfg.execution.runtime == option.id
            else ""
        )
        table.add_row(option.label, installation, account, active)
    console = _console()
    console.print(table)
    _print_credential_backend_note(console)


def _auth_list_payload(cfg: AppConfig) -> dict[str, Any]:
    """Describe every offered connection without printing human output."""

    connections: list[dict[str, Any]] = []
    for option in provider_auth_setup_options():
        if _is_direct_provider(option.id):
            active = _active_auth_provider(cfg) == option.id
            try:
                adapter = create_provider_auth(option.id)
                status = adapter.account_status()
            except ProviderAuthError as exc:
                connections.append(
                    _status_payload(
                        connection=option.id,
                        authenticated=False,
                        error=str(exc),
                        display_name=option.label,
                        availability="built in",
                        active=active,
                    )
                )
                continue
            connections.append(
                _status_payload(
                    connection=option.id,
                    authenticated=status.connected,
                    account_label=status.account_label,
                    credential_scope=_connected_credential_scope(status),
                    detail=status.detail,
                    transport=f"native Alysis Code client ({adapter.protocol})",
                    display_name=option.label,
                    availability="built in",
                    verified=bool(status.verified),
                    active=active,
                )
            )
            continue
        active = (
            cfg.execution.backend == "delegated" and str(cfg.execution.runtime or "") == option.id
        )
        try:
            snapshot = runtime_connection_snapshot(cfg, option.id)
        except RuntimeConnectionError as exc:
            connections.append(
                _status_payload(
                    connection=option.id,
                    authenticated=False,
                    error=str(exc),
                    display_name=option.label,
                    availability="unavailable",
                    active=active,
                )
            )
            continue
        connections.append(
            _status_payload(
                connection=option.id,
                authenticated=snapshot.account.authenticated,
                account_label=snapshot.account.account_label,
                method=snapshot.account.auth_method_id,
                detail=snapshot.account.detail,
                transport=f"delegated runtime ({snapshot.option.adapter})",
                display_name=option.label,
                availability=snapshot.probe.version
                or ("available" if snapshot.probe.available else "missing"),
                installed=bool(snapshot.probe.available),
                verified=bool(snapshot.account.verified),
                active=active,
            )
        )
    payload: dict[str, Any] = {"connections": connections, "error": None}
    payload.update(credential_backend_fields())
    return payload


@auth_app.command("status")
def auth_status(
    runtime_id: str | None = typer.Argument(
        None,
        help="Connection id. Defaults to the selected connection (or the only adapter).",
    ),
    json_output: bool = typer.Option(False, "--json", help=JSON_OPTION_HELP),
) -> None:
    """Show availability and provider-owned account status."""

    emit_json = json_output is True
    cfg = _patchable("load_config", load_config)()
    resolved = _cli_runtime_id(cfg, runtime_id)
    if _use_direct_provider(cfg, resolved, requested=runtime_id):
        if emit_json:
            _emit_json(native_provider_status_payload(resolved))
            return
        try:
            adapter = create_provider_auth(resolved)
            status = adapter.account_status()
        except ProviderAuthError as exc:
            _console().print(f"[red]Authentication error:[/red] {exc}")
            raise typer.Exit(code=2) from exc
        console = _console()
        console.print(f"Connection: {adapter.display_name} ({resolved})")
        console.print(f"Transport: native Alysis Code client ({adapter.protocol})")
        console.print(f"Authenticated: {'yes' if status.connected else 'no'}")
        if status.account_label:
            console.print(f"Account: {status.account_label}")
        if status.detail:
            console.print(f"Status: {status.detail}")
        _print_credential_backend_note(console)
        if not status.connected:
            console.print(f"[dim]Run `alysis auth login {resolved}` to reconnect.[/dim]")
            raise typer.Exit(code=1)
        return
    try:
        snapshot = runtime_connection_snapshot(cfg, resolved)
    except RuntimeConnectionError as exc:
        if emit_json:
            _emit_json(
                _status_payload(
                    connection=resolved,
                    authenticated=False,
                    error=str(exc),
                )
            )
            return
        _console().print(f"[red]Runtime error:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    if emit_json:
        _emit_json(
            _status_payload(
                connection=resolved,
                authenticated=snapshot.account.authenticated,
                account_label=snapshot.account.account_label,
                method=snapshot.account.auth_method_id,
                detail=snapshot.account.detail,
                transport=f"delegated runtime ({snapshot.option.adapter})",
                display_name=snapshot.option.label,
                executable=snapshot.probe.executable or snapshot.settings.executable,
                installed=bool(snapshot.probe.available),
                version=snapshot.probe.version,
                verified=bool(snapshot.account.verified),
            )
        )
        return
    console = _console()
    console.print(f"Runtime: {snapshot.option.label} ({snapshot.option.id})")
    console.print(f"Executable: {snapshot.probe.executable or snapshot.settings.executable}")
    console.print(f"Installed: {'yes' if snapshot.probe.available else 'no'}")
    if snapshot.probe.version:
        console.print(f"Version: {snapshot.probe.version}")
    console.print(f"Authenticated: {'yes' if snapshot.account.authenticated else 'no'}")
    if snapshot.account.auth_method_id:
        console.print(f"Method: {snapshot.account.auth_method_id}")
    if snapshot.account.account_label:
        console.print(f"Account: {snapshot.account.account_label}")
    if snapshot.account.detail:
        console.print(f"Status: {snapshot.account.detail}")
    _print_credential_backend_note(console)
    if not snapshot.probe.available or not snapshot.account.authenticated:
        raise typer.Exit(code=1)


@auth_app.command("models")
def auth_models(
    runtime_id: str = typer.Argument(..., help="Native subscription connection id."),
    json_output: bool = typer.Option(False, "--json", help=JSON_OPTION_HELP),
) -> None:
    """List the live model catalog for a connected native subscription."""

    resolved = str(runtime_id or "").strip()
    if not _is_direct_provider(resolved):
        _auth_command_error(
            "Unknown native subscription connection.",
            json_output=json_output,
            code=2,
        )
    try:
        adapter = create_provider_auth(resolved)
        status = adapter.account_status()
        if not status.connected or not status.verified:
            raise ProviderAuthError(status.detail or "The subscription account is not connected.")
        models = tuple(adapter.list_models(refresh=True))
    except ProviderAuthError as exc:
        _auth_command_error(str(exc), json_output=json_output, code=1, cause=exc)

    payload = [
        {
            "id": model.id,
            "label": model.label,
            "description": model.description,
            "is_default": model.is_default,
            "default_reasoning_effort": model.default_reasoning_effort,
            "reasoning_efforts": [
                {
                    "id": effort.id,
                    "label": effort.label,
                    "description": effort.description,
                }
                for effort in model.reasoning_efforts
            ],
            "input_modalities": list(model.input_modalities),
            "context_window_tokens": model.context_window_tokens,
            "max_output_tokens": model.max_output_tokens,
        }
        for model in models
    ]
    if json_output:
        _emit_json(payload)
        return

    table = Table(title=f"{adapter.display_name} models")
    table.add_column("model")
    table.add_column("default")
    table.add_column("reasoning")
    table.add_column("description")
    for model in models:
        table.add_row(
            model.id,
            "yes" if model.is_default else "",
            ", ".join(effort.id for effort in model.reasoning_efforts),
            model.description,
        )
    _console().print(table)


@auth_app.command("select-model")
def auth_select_model(
    runtime_id: str = typer.Argument(..., help="Native subscription connection id."),
    model: str = typer.Argument(..., help="Provider model id."),
    reasoning_effort: str = typer.Option(
        "auto",
        "--reasoning-effort",
        help="Provider-supported reasoning effort, or auto.",
    ),
    json_output: bool = typer.Option(False, "--json", help=JSON_OPTION_HELP),
) -> None:
    """Atomically activate and configure a native subscription model route."""

    resolved = str(runtime_id or "").strip()
    selected_model = str(model or "").strip()
    selected_effort = str(reasoning_effort or "auto").strip().lower() or "auto"
    if not _is_direct_provider(resolved):
        _auth_command_error(
            "Unknown native subscription connection.",
            json_output=json_output,
            code=2,
        )
    if not selected_model:
        _auth_command_error(
            "A subscription model is required.",
            json_output=json_output,
            code=2,
        )

    try:
        adapter = create_provider_auth(resolved)
        status = adapter.account_status()
        if not status.connected or not status.verified:
            raise ProviderAuthError(status.detail or "The subscription account is not connected.")
        models = tuple(adapter.list_models(refresh=True))
        chosen = next((candidate for candidate in models if candidate.id == selected_model), None)
        if chosen is None:
            raise ProviderAuthError("The selected model is not available to this account.")
        supported_efforts = {effort.id for effort in chosen.reasoning_efforts}
        if selected_effort != "auto" and selected_effort not in supported_efforts:
            raise ProviderAuthError("The selected reasoning effort is not supported by this model.")

        # Provider I/O above stays outside the lock. Only the fresh config read,
        # route mutation, and one durable write form the serialized transaction.
        with config_transaction(
            loader=_patchable("load_config", load_config),
            saver=_patchable("save_config", save_config),
        ) as cfg:
            subscription_profile = get_profile(cfg, str(adapter.profile_name))
            if subscription_profile is None or subscription_profile.auth_provider != resolved:
                raise ProviderAuthError("Connect this subscription before choosing its model.")

            set_active_profile(cfg, subscription_profile.name)
            update_active_profile_defaults(
                cfg,
                default_model=selected_model,
                reasoning_effort=selected_effort,
                allow_subscription_selection=True,
            )
            cfg.execution.backend = "native"
            cfg.execution.runtime = None
            cfg.extra_fields.pop(SUBSCRIPTION_SELECTION_REQUIRED_KEY, None)
            cfg.extra_fields.pop("subscription_reconnect_required", None)
            cfg.extra_fields["onboarded"] = True
            active = get_active_profile(cfg)
    except (ConfigError, ProviderAuthError) as exc:
        _auth_command_error(str(exc), json_output=json_output, code=1, cause=exc)

    payload = {
        "id": resolved,
        "profile_name": active.name,
        "model": selected_model,
        "reasoning_effort": selected_effort,
    }
    if json_output:
        _emit_json(payload)
        return
    _console().print(f"[green]Selected:[/green] {selected_model} · reasoning {selected_effort}")


@auth_app.command("reasoning")
def auth_reasoning(
    model: str = typer.Argument(..., help="Exact model ID."),
    profile_name: str = typer.Option(..., "--profile", help="Selected provider profile."),
) -> None:
    """Read effort capabilities without mutating the saved provider selection."""
    from ...config import clone_cfg
    from ...model_reasoning import model_reasoning_capabilities

    try:
        cfg = clone_cfg(_patchable("load_config", load_config)())
        set_active_profile(cfg, profile_name)
        cfg.model = model
        _emit_json([model_reasoning_capabilities(cfg)])
    except (ConfigError, ProviderAuthError) as exc:
        _auth_command_error(str(exc), json_output=True, code=1, cause=exc)


@auth_app.command("login")
def auth_login(
    runtime_id: str | None = typer.Argument(
        None,
        help="Connection id. Defaults to the selected connection (or the only adapter).",
    ),
    device_code: bool = typer.Option(
        False,
        "--device-code",
        help="Use the provider's device-code flow instead of browser callback login.",
    ),
    switch_account: bool = typer.Option(
        False,
        "--switch-account",
        help="Connect a different account without disconnecting the current account first.",
    ),
) -> None:
    """Connect a provider subscription and activate it in Alysis Code."""

    cfg = _patchable("load_config", load_config)()
    resolved = _cli_runtime_id(cfg, runtime_id)
    console = _console()
    if _use_direct_provider(cfg, resolved, requested=runtime_id):
        try:
            adapter = create_provider_auth(resolved)
            # A replacement account becomes active only after validation. Cancellation
            # must preserve the existing subscription and its registration.
            status = adapter.login(
                method="device-code"
                if device_code
                else "browser-new-account"
                if switch_account
                else "browser",
                output_write=lambda message: console.print(message, highlight=False),
            )
            if not status.connected:
                raise ProviderAuthError(status.detail or "Authentication failed.")
            _activate_direct_provider(cfg, resolved, adapter)
            _patchable("save_config", save_config)(cfg)
        except ProviderAuthError as exc:
            console.print(f"[red]Authentication failed:[/red] {exc}")
            raise typer.Exit(code=1) from exc
        console.print(f"[green]Connected:[/green] {status.account_label or resolved}")
        console.print(
            "[dim]Alysis Code stores refreshable credentials in its encrypted provider vault "
            "and keeps the native agent/TUI active.[/dim]"
        )
        console.print(
            "[dim]Choose the subscription model and reasoning effort in "
            "`/config` → Default model.[/dim]"
        )
        return
    try:
        activate_runtime(cfg, resolved)
        if switch_account:
            logout_status = logout_runtime(cfg, resolved)
            if not logout_status.verified or logout_status.authenticated:
                detail = logout_status.detail or "provider credentials could not be removed"
                raise RuntimeConnectionError(f"Could not switch account: {detail}")
        method_id = "device-code" if device_code else "browser"
        status = login_runtime(cfg, resolved, method_id=method_id)
    except RuntimeConnectionError as exc:
        console.print(f"[red]Runtime error:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    if not status.verified or not status.authenticated:
        console.print(f"[red]Authentication failed:[/red] {status.detail or 'unknown error'}")
        raise typer.Exit(code=1)
    _patchable("save_config", save_config)(cfg)
    label = status.account_label or resolved
    console.print(f"[green]Connected:[/green] {label}")
    console.print(
        "[dim]The provider runtime owns and refreshes its credentials; "
        "Alysis Code stores only the selected runtime settings.[/dim]"
    )


@auth_app.command("logout")
def auth_logout(
    runtime_id: str | None = typer.Argument(
        None,
        help="Connection id. Defaults to the selected connection (or the only adapter).",
    ),
) -> None:
    """Disconnect a provider subscription and remove locally stored credentials."""

    cfg = _patchable("load_config", load_config)()
    resolved = _cli_runtime_id(cfg, runtime_id)
    if _use_direct_provider(cfg, resolved, requested=runtime_id):
        try:
            status = create_provider_auth(resolved).logout()
        except ProviderAuthError as exc:
            _console().print(f"[red]Logout failed:[/red] {exc}")
            raise typer.Exit(code=1) from exc
        _console().print(f"[green]Disconnected:[/green] {resolved}")
        if not status.verified and status.detail:
            _console().print(f"[yellow]{status.detail}[/yellow]")
        return
    try:
        status = logout_runtime(cfg, resolved)
    except RuntimeConnectionError as exc:
        _console().print(f"[red]Runtime error:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    if not status.verified or status.authenticated:
        _console().print(f"[red]Logout failed:[/red] {status.detail or 'unknown error'}")
        raise typer.Exit(code=1)
    _console().print(f"[green]Disconnected:[/green] {resolved}")


def login_provider_interactively(
    *,
    runtime_id: str | None = None,
    device_code: bool = False,
) -> None:
    """Run a provider login flow from the unified login entry point."""

    auth_login(runtime_id=runtime_id, device_code=device_code, switch_account=False)


def _cli_runtime_id(cfg: AppConfig, requested: str | None) -> str:
    explicit = str(requested or "").strip()
    if explicit:
        return explicit
    selected = str(cfg.execution.runtime or "").strip()
    if selected:
        return selected
    active_provider = _active_auth_provider(cfg)
    if active_provider:
        return active_provider
    options = provider_auth_setup_options()
    if len(options) == 1:
        return options[0].id
    choices = ", ".join(option.id for option in options) or "none"
    raise typer.BadParameter(f"Choose a connection id. Available connections: {choices}.")


def _account_status_label(authenticated: bool, detail: str | None) -> str:
    if authenticated:
        return detail or "connected"
    return detail or "not connected"


def _is_direct_provider(provider_id: str) -> bool:
    from ...provider_auth import provider_auth_ids

    return str(provider_id or "").strip() in provider_auth_ids()


def _use_direct_provider(
    cfg: AppConfig,
    provider_id: str,
    *,
    requested: str | None,
) -> bool:
    """Disambiguate the native subscription adapter from the legacy runtime.

    ``openai-codex`` was already used by the original delegated ``codex`` CLI
    integration. An explicit provider id now means the native subscription
    connection, while an omitted id continues to honor an already-selected
    delegated runtime so existing advanced configurations remain operable.
    """

    if not _is_direct_provider(provider_id):
        return False
    if str(requested or "").strip():
        return True
    return not (
        cfg.execution.backend == "delegated"
        and str(cfg.execution.runtime or "").strip() == provider_id
    )


def _active_auth_provider(cfg: AppConfig) -> str | None:
    try:
        return get_active_profile(cfg).auth_provider
    except Exception:
        return None


def _activate_direct_provider(cfg: AppConfig, provider_id: str, adapter: object) -> None:
    models = adapter.list_models(refresh=True)  # type: ignore[attr-defined]
    if not models:
        raise ProviderAuthError("The connected account did not advertise any available models.")
    existing_profile = get_profile(cfg, str(adapter.profile_name))  # type: ignore[attr-defined]
    preserve_existing = bool(
        existing_profile is not None and existing_profile.auth_provider == provider_id
    )
    profile = ProfileSpec(
        name=str(adapter.profile_name),  # type: ignore[attr-defined]
        protocol=str(adapter.protocol),  # type: ignore[attr-defined]
        base_url=str(adapter.base_url),  # type: ignore[attr-defined]
        api_key_env=(existing_profile.api_key_env if preserve_existing else None),
        auth_provider=provider_id,
        extra_headers=(dict(existing_profile.extra_headers) if preserve_existing else {}),
        default_model=(existing_profile.default_model if preserve_existing else ""),
        reasoning_effort=(existing_profile.reasoning_effort if preserve_existing else None),
        reasoning_trace_adapter=(
            existing_profile.reasoning_trace_adapter if preserve_existing else "auto"
        ),
        web_search_adapter=(existing_profile.web_search_adapter if preserve_existing else "auto"),
        web_search_model=(existing_profile.web_search_model if preserve_existing else ""),
        notes=(
            existing_profile.notes
            if preserve_existing
            else f"{adapter.display_name}. Uses Alysis Code's native agent loop."  # type: ignore[attr-defined]
        ),
        cache_capability=(existing_profile.cache_capability if preserve_existing else None),
    )
    add_profile(cfg, profile, allow_auth_profile_update=True)
    set_active_profile(cfg, profile.name)
    cfg.execution.backend = "native"
    cfg.execution.runtime = None
    cfg.model = profile.default_model
    effective_effort = (
        None if profile.reasoning_effort in {None, "auto"} else profile.reasoning_effort
    )
    cfg.llm_reasoning_effort = effective_effort
    cfg.llm_enable_thinking = None if effective_effort is None else effective_effort != "none"
    confirmation_pending = str(
        cfg.extra_fields.get(SUBSCRIPTION_SELECTION_REQUIRED_KEY) or ""
    ).strip().lower() in {"true", provider_id}
    selection_ready = subscription_selection_supported(profile, models) and not confirmation_pending
    cfg.extra_fields["onboarded"] = selection_ready
    cfg.extra_fields.pop("subscription_reconnect_required", None)
    if not selection_ready:
        cfg.extra_fields[SUBSCRIPTION_SELECTION_REQUIRED_KEY] = provider_id


__all__ = [
    "auth_app",
    "auth_list",
    "auth_login",
    "auth_logout",
    "auth_models",
    "auth_select_model",
    "auth_status",
    "login_connection_interactively",
    "login_connection_rows",
    "login_provider_interactively",
]
