"""Canonical local composition root for Forge Host Protocol v2.

Every local presentation surface (CLI, Electron, and VS Code) must talk to the
same ``ForgeHostService`` generation assembled here.  This module owns only
composition and provider-generation binding; planner, execution, approval,
verification, Git integration, and wire semantics stay in their dedicated
modules.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any, BinaryIO

from .branding import env_get_from
from .cli_impl.forge import (
    HostExecutionRuntimeBinding,
    HostProviderRuntimeIdentity,
    build_forge_host_dependency_factory,
    derive_host_provider_runtime_identity,
)
from .config import AppConfig, clone_cfg, load_config, resolve_api_key
from .forge import RunPaths, create_plan_run, load_current_run_paths, load_plan
from .forge_host_engine_bridge import (
    ForgeHostEngineRunner,
    HostDependencyFactory,
)
from .forge_host_native_adapter import (
    ForgeHostPreflightError,
    IntentKind,
    NativeAdapterDependencies,
    NativeAdapterRequest,
    ProviderBinding,
    ScopeKind,
    build_native_plan_admission,
    revalidate_native_plan_admission,
)
from .forge_host_protocol.models import Identity, Peer
from .forge_host_service import (
    ForgeHostPlannerAdapter,
    ForgeHostPlannerAdapterSettings,
    ForgeHostService,
    HostPlannerOutcome,
    HostPlannerRequest,
)
from .forge_host_stdio import ForgeHostStdioServer
from .forge_planner_engine import ForgePlannerDependencies, ForgePlannerState
from .forge_planning_profile import PlanningProfile, normalize_planning_profile
from .profiles import get_active_profile
from .workspace_binding import WorkspaceBinding


def _digest_id(prefix: str, domain: bytes, *parts: str) -> str:
    digest = hashlib.sha256(domain)
    for part in parts:
        encoded = part.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return f"{prefix}_{digest.hexdigest()[:24]}"


def _identity_payload(identity: HostProviderRuntimeIdentity) -> str:
    return json.dumps(
        asdict(identity),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _provider_binding_copy(binding: ProviderBinding) -> ProviderBinding:
    return ProviderBinding(
        provider_binding_ref=binding.provider_binding_ref,
        provider=binding.provider,
        model=binding.model,
        account_fingerprint=binding.account_fingerprint,
    )


@dataclass(frozen=True, slots=True, repr=False)
class ForgeHostProviderAuthority:
    """One immutable provider/config/credential generation binding.

    The raw credential and config snapshot never enter protocol messages or
    object representations.  Fresh config/credential resolution is used only
    to detect drift; planner and execution always receive private copies of the
    original generation snapshot and the originally pinned credential.
    """

    binding: ProviderBinding
    _runtime_identity: HostProviderRuntimeIdentity = field(repr=False)
    _config_snapshot: AppConfig = field(repr=False)
    _config_loader: Callable[[], AppConfig] = field(repr=False, compare=False)
    _credential: str = field(repr=False)
    _credential_source: str = field(repr=False)
    _credential_from_config: bool = field(repr=False)
    _console: Any = field(repr=False, compare=False)
    _base_environment: Mapping[str, str] = field(repr=False, compare=False)

    @classmethod
    def capture(
        cls,
        *,
        config_loader: Callable[[], AppConfig],
        console: Any,
        api_key_override: str | None = None,
        credential_source: str | None = None,
        account_fingerprint: str | None = None,
        base_environment: Mapping[str, str] | None = None,
        generation_nonce: str | None = None,
    ) -> ForgeHostProviderAuthority:
        try:
            config = clone_cfg(config_loader())
            profile = get_active_profile(config)
        except Exception:
            raise ForgeHostPreflightError(
                "provider_binding_unavailable",
                "The selected provider binding is unavailable.",
            ) from None
        if str(profile.auth_provider or "").strip():
            raise ForgeHostPreflightError(
                "subscription_provider_deferred",
                "Forge Host Phase 0 requires a generation-bound API credential.",
            )
        if credential_source is not None and api_key_override is None:
            raise ForgeHostPreflightError(
                "invalid_provider_credential",
                "A credential source requires an explicit credential value.",
            )
        credential_from_config = api_key_override is None
        try:
            if api_key_override is None:
                resolved = resolve_api_key(config, profile_name=profile.name)
                credential = str(resolved.key or "")
                source = str(resolved.source or "").strip()
            else:
                credential = str(api_key_override or "")
                source = str(credential_source or "explicit:forge-host-generation").strip()
        except Exception:
            raise ForgeHostPreflightError(
                "missing_provider_credential",
                "The selected provider has no usable generation-bound credential.",
            ) from None
        if not credential or not source:
            raise ForgeHostPreflightError(
                "missing_provider_credential",
                "The selected provider has no usable generation-bound credential.",
            )

        nonce = generation_nonce or secrets.token_hex(24)
        opaque_source = _digest_id(
            "source",
            b"alysis.forge-host-v2.credential-source\x00",
            source,
        )
        try:
            preliminary = derive_host_provider_runtime_identity(
                config,
                account_fingerprint=None,
                credential_value=credential,
                credential_source=opaque_source,
            )
        except Exception:
            raise ForgeHostPreflightError(
                "invalid_provider_binding",
                "The selected provider binding is invalid.",
            ) from None
        account = account_fingerprint or _digest_id(
            "acct",
            b"alysis.forge-host-v2.account\x00",
            nonce,
            preliminary.provider,
            preliminary.profile_name,
            preliminary.credential_fingerprint or "",
        )
        try:
            identity = derive_host_provider_runtime_identity(
                config,
                account_fingerprint=account,
                credential_value=credential,
                credential_source=opaque_source,
            )
        except Exception:
            raise ForgeHostPreflightError(
                "invalid_provider_binding",
                "The selected provider binding is invalid.",
            ) from None
        provider_ref = _digest_id(
            "provider",
            b"alysis.forge-host-v2.provider\x00",
            nonce,
            _identity_payload(identity),
        )
        environment = {str(key): str(value) for key, value in dict(base_environment or {}).items()}
        return cls(
            binding=ProviderBinding(
                provider_binding_ref=provider_ref,
                provider=identity.provider,
                model=identity.model,
                account_fingerprint=identity.account_fingerprint,
            ),
            _runtime_identity=identity,
            _config_snapshot=clone_cfg(config),
            _config_loader=config_loader,
            _credential=credential,
            _credential_source=source,
            _credential_from_config=credential_from_config,
            _console=console,
            _base_environment=MappingProxyType(environment),
        )

    def __repr__(self) -> str:
        return (
            "ForgeHostProviderAuthority("
            f"provider_binding_ref={self.binding.provider_binding_ref!r})"
        )

    def _fresh_identity(self) -> HostProviderRuntimeIdentity:
        try:
            config = clone_cfg(self._config_loader())
            profile = get_active_profile(config)
            if str(profile.auth_provider or "").strip():
                raise RuntimeError("subscription provider execution is unavailable")
            if self._credential_from_config:
                resolved = resolve_api_key(config, profile_name=profile.name)
                credential = str(resolved.key or "")
                source = str(resolved.source or "").strip()
            else:
                credential = self._credential
                source = self._credential_source
            if not credential or not source:
                raise RuntimeError("the provider credential is unavailable")
            opaque_source = _digest_id(
                "source",
                b"alysis.forge-host-v2.credential-source\x00",
                source,
            )
            return derive_host_provider_runtime_identity(
                config,
                account_fingerprint=self.binding.account_fingerprint,
                credential_value=credential,
                credential_source=opaque_source,
            )
        except Exception:
            raise ForgeHostPreflightError(
                "provider_binding_changed",
                "The selected provider binding is no longer available.",
            ) from None

    def revalidate(self) -> None:
        if self._fresh_identity() != self._runtime_identity:
            raise ForgeHostPreflightError(
                "provider_binding_changed",
                "The selected provider binding changed after generation creation.",
            )

    def current_provider_binding(self) -> ProviderBinding:
        self.revalidate()
        return _provider_binding_copy(self.binding)

    def planner_config(self) -> AppConfig:
        self.revalidate()
        return clone_cfg(self._config_snapshot)

    def runtime_binding_for(self, request: Any) -> HostExecutionRuntimeBinding:
        self.revalidate()
        admission = getattr(request, "native_admission", None)
        if getattr(admission, "provider_binding", None) != self.binding:
            raise ForgeHostPreflightError(
                "provider_binding_changed",
                "The execution request does not bind this provider generation.",
            )
        return HostExecutionRuntimeBinding(
            effective_config=clone_cfg(self._config_snapshot),
            console=self._console,
            provider_binding_ref=self.binding.provider_binding_ref,
            provider=self.binding.provider,
            model=self.binding.model,
            account_fingerprint=self.binding.account_fingerprint,
            admitted_identity=self._runtime_identity,
            api_key_override=self._credential,
            base_environment=dict(self._base_environment),
        )

    def _planner_api_key(self) -> str:
        self.revalidate()
        return self._credential


@dataclass(frozen=True, slots=True, repr=False)
class LocalForgeHostRuntime:
    """One canonical local Forge generation shared by all presentation adapters."""

    paths: RunPaths
    provider_authority: ForgeHostProviderAuthority
    planner: ForgeHostPlannerAdapter | ForgeHostExistingPlanAdapter
    runner: ForgeHostEngineRunner
    service: ForgeHostService

    def stdio_server(
        self,
        *,
        input_stream: BinaryIO,
        output_stream: BinaryIO,
        server: Peer,
    ) -> ForgeHostStdioServer:
        return ForgeHostStdioServer(
            service=self.service,
            input_stream=input_stream,
            output_stream=output_stream,
            server=server,
        )


def _runtime_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(16)}"


class ForgeHostExistingPlanAdapter:
    """Re-admit one persisted finalized plan without invoking a second planner."""

    def __init__(
        self,
        *,
        paths: RunPaths,
        plan: Mapping[str, Any],
        planner_input: str,
        expected_host_plan_fingerprint: str,
        expected_native_approval_fingerprint: str,
        settings: ForgeHostPlannerAdapterSettings,
        native_dependencies: NativeAdapterDependencies,
    ) -> None:
        explicit_input = planner_input.strip()
        if not explicit_input:
            raise ValueError("existing Forge plan admission requires its original planner input")
        self._paths = paths
        self._plan = copy.deepcopy(dict(plan))
        self._planner_input = explicit_input
        self._expected_host_plan_fingerprint = expected_host_plan_fingerprint
        self._expected_native_approval_fingerprint = expected_native_approval_fingerprint
        self._settings = settings
        self._native_dependencies = native_dependencies

    @property
    def native_plan(self) -> dict[str, Any]:
        return copy.deepcopy(self._plan)

    def __call__(self, _request: HostPlannerRequest) -> HostPlannerOutcome:
        canonical_plan = load_plan(self._paths)
        if canonical_plan != self._plan:
            raise ForgeHostPreflightError(
                "native_plan_changed",
                "The persisted Forge plan changed before execution admission.",
            )
        admission = build_native_plan_admission(
            NativeAdapterRequest(
                paths=self._paths,
                plan=copy.deepcopy(canonical_plan),
                planner_input=self._planner_input,
                workspace_ref=self._settings.workspace_ref,
                provider_binding=self._settings.provider_binding,
                per_managed_task_max_steps=self._settings.per_managed_task_max_steps,
                run_timeout_ms=self._settings.run_timeout_ms,
                intent_kind=self._settings.intent_kind,
                verification_commands=self._settings.verification_commands,
                trusted_pytest=self._settings.trusted_pytest,
                declared_scope_kinds=self._settings.declared_scope_kinds,
            ),
            dependencies=self._native_dependencies,
        )
        if (
            admission.plan_ready.host_plan_fingerprint != self._expected_host_plan_fingerprint
            or admission.plan_ready.native_approval_fingerprint
            != self._expected_native_approval_fingerprint
        ):
            raise ForgeHostPreflightError(
                "persisted_plan_authority_changed",
                "The persisted Forge plan no longer matches its approved planning result.",
            )
        self._plan = copy.deepcopy(load_plan(self._paths))
        return HostPlannerOutcome(admission=admission)


def _workspace_ref(paths: RunPaths, generation_id: str) -> str:
    metadata = paths.root.resolve(strict=True).stat()
    return _digest_id(
        "workspace",
        b"alysis.forge-host-v2.workspace\x00",
        generation_id,
        str(metadata.st_dev),
        str(metadata.st_ino),
    )


def resolve_local_trusted_pytest(
    candidate: Path | None = None,
    *,
    environment: Mapping[str, str] | None = None,
) -> Path:
    """Resolve the local pytest executable before any Host authority is issued."""

    selected = candidate
    if selected is None:
        env = environment if environment is not None else os.environ
        configured = str(env_get_from(env, "ALYSIS_FORGE_TRUSTED_PYTEST") or "").strip()
        if configured:
            selected = Path(configured)
    if selected is None:
        raise ForgeHostPreflightError(
            "pytest_identity_failed",
            "Forge Host requires an explicitly trusted pytest executable.",
        )
    try:
        return selected.expanduser().resolve(strict=True)
    except OSError:
        raise ForgeHostPreflightError(
            "pytest_identity_failed",
            "The trusted pytest executable is unavailable.",
        ) from None


def build_local_forge_host_runtime(
    *,
    root: Path,
    cli_module: Any,
    console: Any,
    trusted_pytest: Path,
    config_loader: Callable[[], AppConfig] = load_config,
    api_key_override: str | None = None,
    credential_source: str | None = None,
    account_fingerprint: str | None = None,
    base_environment: Mapping[str, str] | None = None,
    per_managed_task_max_steps: int = 64,
    run_timeout_ms: int = 3_600_000,
    verification_commands: Sequence[str] = ("pytest -q",),
    intent_kind: IntentKind | None = None,
    declared_scope_kinds: Mapping[str, ScopeKind] | None = None,
    create_if_missing: bool = False,
    allow_broad_workspace: bool = False,
    planning_profile: PlanningProfile = "quick",
    workspace_binding: WorkspaceBinding | None = None,
    planner_dependencies: ForgePlannerDependencies | None = None,
    native_dependencies: NativeAdapterDependencies | None = None,
    dependency_factory: HostDependencyFactory | None = None,
    clock: Callable[[], datetime] | None = None,
    monotonic: Callable[[], float] | None = None,
    approval_ttl: timedelta = timedelta(minutes=5),
    host_run_id_factory: Callable[[], str] | None = None,
    session_id: str | None = None,
    generation_id: str | None = None,
) -> LocalForgeHostRuntime:
    """Create the only supported local planner/executor service composition."""

    if per_managed_task_max_steps <= 0 or run_timeout_ms <= 0:
        raise ValueError("Forge Host execution limits must be positive")
    resolved_planning_profile = normalize_planning_profile(planning_profile)
    provider = ForgeHostProviderAuthority.capture(
        config_loader=config_loader,
        console=console,
        api_key_override=api_key_override,
        credential_source=credential_source,
        account_fingerprint=account_fingerprint,
        base_environment=base_environment,
    )
    paths = create_plan_run(
        root,
        create_if_missing=create_if_missing,
        allow_broad_workspace=allow_broad_workspace,
        workspace_binding=workspace_binding,
    )
    actual_generation_id = generation_id or _runtime_id("gen")
    identity = Identity(
        session_id=session_id or _runtime_id("ses"),
        generation_id=actual_generation_id,
        run_id=None,
        revision=0,
    )
    native_deps = replace(
        native_dependencies or NativeAdapterDependencies(),
        current_provider_binding=provider.current_provider_binding,
    )
    planner = ForgeHostPlannerAdapter(
        paths=paths,
        plan=load_plan(paths),
        state=ForgePlannerState(cfg=provider.planner_config()),
        settings=ForgeHostPlannerAdapterSettings(
            workspace_ref=_workspace_ref(paths, actual_generation_id),
            provider_binding=provider.current_provider_binding(),
            per_managed_task_max_steps=per_managed_task_max_steps,
            run_timeout_ms=run_timeout_ms,
            trusted_pytest=trusted_pytest,
            intent_kind=intent_kind,
            verification_commands=tuple(verification_commands),
            declared_scope_kinds=dict(declared_scope_kinds or {}),
            api_key_override=provider._planner_api_key(),
            selection_label="forge-host-v2-planner",
            planning_profile=resolved_planning_profile,
        ),
        cfg_loader=provider.planner_config,
        planner_dependencies=planner_dependencies,
        native_dependencies=native_deps,
    )
    actual_dependency_factory = dependency_factory or build_forge_host_dependency_factory(
        cli_module,
        runtime_binding_for=provider.runtime_binding_for,
    )
    runner = ForgeHostEngineRunner(dependency_factory=actual_dependency_factory)
    service_kwargs: dict[str, Any] = {
        "identity": identity,
        "planner": planner,
        "sequential_runner": runner,
        "current_provider_binding": provider.current_provider_binding,
        "admission_revalidator": lambda admission: revalidate_native_plan_admission(
            admission,
            dependencies=native_deps,
        ),
        "approval_ttl": approval_ttl,
        "host_run_id_factory": host_run_id_factory,
    }
    if clock is not None:
        service_kwargs["clock"] = clock
    if monotonic is not None:
        service_kwargs["monotonic"] = monotonic
    service = ForgeHostService(**service_kwargs)
    return LocalForgeHostRuntime(
        paths=paths,
        provider_authority=provider,
        planner=planner,
        runner=runner,
        service=service,
    )


def build_local_forge_host_runtime_for_existing_plan(
    *,
    root: Path,
    planner_input: str,
    expected_host_plan_fingerprint: str,
    expected_native_approval_fingerprint: str,
    cli_module: Any,
    console: Any,
    trusted_pytest: Path,
    config_loader: Callable[[], AppConfig] = load_config,
    api_key_override: str | None = None,
    credential_source: str | None = None,
    account_fingerprint: str | None = None,
    base_environment: Mapping[str, str] | None = None,
    per_managed_task_max_steps: int = 64,
    run_timeout_ms: int = 3_600_000,
    verification_commands: Sequence[str] = ("pytest -q",),
    intent_kind: IntentKind | None = None,
    declared_scope_kinds: Mapping[str, ScopeKind] | None = None,
    native_dependencies: NativeAdapterDependencies | None = None,
    dependency_factory: HostDependencyFactory | None = None,
    clock: Callable[[], datetime] | None = None,
    monotonic: Callable[[], float] | None = None,
    approval_ttl: timedelta = timedelta(minutes=5),
    host_run_id_factory: Callable[[], str] | None = None,
    session_id: str | None = None,
    generation_id: str | None = None,
) -> LocalForgeHostRuntime:
    """Compose a fresh approval/execution generation for one persisted plan."""

    if per_managed_task_max_steps <= 0 or run_timeout_ms <= 0:
        raise ValueError("Forge Host execution limits must be positive")
    provider = ForgeHostProviderAuthority.capture(
        config_loader=config_loader,
        console=console,
        api_key_override=api_key_override,
        credential_source=credential_source,
        account_fingerprint=account_fingerprint,
        base_environment=base_environment,
    )
    paths = load_current_run_paths(root)
    actual_generation_id = generation_id or _runtime_id("gen")
    identity = Identity(
        session_id=session_id or _runtime_id("ses"),
        generation_id=actual_generation_id,
        run_id=None,
        revision=0,
    )
    native_deps = replace(
        native_dependencies or NativeAdapterDependencies(),
        current_provider_binding=provider.current_provider_binding,
    )
    settings = ForgeHostPlannerAdapterSettings(
        workspace_ref=_workspace_ref(paths, actual_generation_id),
        provider_binding=provider.current_provider_binding(),
        per_managed_task_max_steps=per_managed_task_max_steps,
        run_timeout_ms=run_timeout_ms,
        trusted_pytest=trusted_pytest,
        intent_kind=intent_kind,
        verification_commands=tuple(verification_commands),
        declared_scope_kinds=dict(declared_scope_kinds or {}),
        api_key_override=None,
        selection_label="forge-host-v2-existing-plan",
    )
    planner = ForgeHostExistingPlanAdapter(
        paths=paths,
        plan=load_plan(paths),
        planner_input=planner_input,
        expected_host_plan_fingerprint=expected_host_plan_fingerprint,
        expected_native_approval_fingerprint=expected_native_approval_fingerprint,
        settings=settings,
        native_dependencies=native_deps,
    )
    actual_dependency_factory = dependency_factory or build_forge_host_dependency_factory(
        cli_module,
        runtime_binding_for=provider.runtime_binding_for,
    )
    runner = ForgeHostEngineRunner(dependency_factory=actual_dependency_factory)
    service_kwargs: dict[str, Any] = {
        "identity": identity,
        "planner": planner,
        "sequential_runner": runner,
        "current_provider_binding": provider.current_provider_binding,
        "admission_revalidator": lambda admission: revalidate_native_plan_admission(
            admission,
            dependencies=native_deps,
        ),
        "approval_ttl": approval_ttl,
        "host_run_id_factory": host_run_id_factory,
    }
    if clock is not None:
        service_kwargs["clock"] = clock
    if monotonic is not None:
        service_kwargs["monotonic"] = monotonic
    service = ForgeHostService(**service_kwargs)
    return LocalForgeHostRuntime(
        paths=paths,
        provider_authority=provider,
        planner=planner,
        runner=runner,
        service=service,
    )


__all__ = [
    "ForgeHostProviderAuthority",
    "LocalForgeHostRuntime",
    "build_local_forge_host_runtime",
    "build_local_forge_host_runtime_for_existing_plan",
    "resolve_local_trusted_pytest",
]
