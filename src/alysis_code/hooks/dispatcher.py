from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import subprocess
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from ..command_text import command_text
from .audit import build_hook_audit_event
from .config import ResolvedHookConfig, ResolvedHookMatcherGroup
from .models import (
    CanonicalHookEventName,
    CommandHookSpec,
    HookDispatchResult,
    HookInvocationContext,
    MutableHookDispatchState,
    ParsedHookOutput,
)

_BLOCKING_HOOK_EVENTS: frozenset[str] = frozenset(
    {"PreToolUse", "SessionStart", "UserPromptSubmit"}
)
_PARALLELIZABLE_EVENTS: frozenset[str] = frozenset(
    {
        "PostToolUse",
        "PostWrite",
        "TurnComplete",
        "Notification",
        "PreCompact",
        "SessionEnd",
        "SubagentStop",
    }
)
_TOOL_MATCHER_EVENTS: frozenset[str] = frozenset({"PreToolUse", "PostToolUse", "SubagentStop"})
_SESSION_END_EVENT = "SessionEnd"
_SESSION_START_EVENT = "SessionStart"
_TURN_COMPLETE_EVENT = "TurnComplete"


def _legacy_event_alias(event_name: CanonicalHookEventName) -> str | None:
    if event_name == _TURN_COMPLETE_EVENT:
        return "Stop"
    return None


def _normalize_text_messages(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, list):
        normalized: list[str] = []
        for item in value:
            if not isinstance(item, str):
                continue
            text = item.strip()
            if text:
                normalized.append(text)
        return normalized
    return []


def _coerce_decision(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().lower()


_SECRET_KEY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r".*_API_KEY$"),
    re.compile(r".*_TOKEN$"),
    re.compile(r".*_SECRET$"),
    re.compile(r"^AWS_"),
    re.compile(r"^OPENAI_"),
    re.compile(r"^ANTHROPIC_"),
    re.compile(r"^GITHUB_TOKEN$"),
    re.compile(r"^GH_TOKEN$"),
)
_SAFE_ENV_BASELINE: frozenset[str] = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
        "SHELL",
        "TERM",
        "PWD",
        "TMPDIR",
    }
)


def _is_secret_env_key(key: str) -> bool:
    return any(p.match(key) for p in _SECRET_KEY_PATTERNS)


def _build_hook_env(
    hook: CommandHookSpec, *, base: dict[str, str] | os._Environ[str]
) -> dict[str, str]:
    allow = set(hook.env_allow or ())
    if hook.env_passthrough == "all":
        env = dict(base)
    elif hook.env_passthrough == "explicit":
        env = {
            k: v
            for k, v in base.items()
            if k in _SAFE_ENV_BASELINE or k in allow or k.startswith("ALYSIS_")
        }
    else:  # safe
        env = {k: v for k, v in base.items() if not _is_secret_env_key(k) or k in allow}
    if hook.env:
        env.update(hook.env)
    return env


_TRUNCATABLE_PAYLOAD_KEYS: frozenset[str] = frozenset(
    {"content", "patch", "diff", "stdout", "stderr", "output", "text", "body", "data"}
)
_DEFAULT_TRUNCATE_FIELD_BYTES = 65_536


def _trunc_marker(text: str) -> dict[str, Any]:
    raw = text.encode("utf-8", errors="replace")
    return {
        "__truncated": True,
        "bytes": len(raw),
        "preview": text[:256],
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def _truncate_large_strings(payload: Any, *, max_field_bytes: int) -> Any:
    if isinstance(payload, dict):
        out: dict[str, Any] = {}
        for k, v in payload.items():
            if (
                k in _TRUNCATABLE_PAYLOAD_KEYS
                and isinstance(v, str)
                and len(v.encode("utf-8")) > max_field_bytes
            ):
                out[k] = _trunc_marker(v)
            else:
                out[k] = _truncate_large_strings(v, max_field_bytes=max_field_bytes)
        return out
    if isinstance(payload, list):
        return [_truncate_large_strings(i, max_field_bytes=max_field_bytes) for i in payload]
    return payload


class HookDispatcher:
    def __init__(
        self,
        *,
        config: ResolvedHookConfig,
        workspace_root: Path,
        repo_root: Path,
        session_id: str,
        mode: str,
        runtime_kind: str,
        warning_callback: Callable[[str], None] | None = None,
        log_callback: Callable[[str, dict[str, Any]], None] | None = None,
        audit_callback: Callable[[dict[str, Any]], None] | None = None,
        max_stdin_bytes: int = 1_048_576,
        parallel_enabled: bool = True,
        max_parallel_workers: int = 4,
    ) -> None:
        self.config = config
        self.workspace_root = workspace_root.resolve()
        self.repo_root = repo_root.resolve()
        self.session_id = session_id
        self.mode = str(mode)
        self.runtime_kind = str(runtime_kind)
        self.warning_callback = warning_callback
        self.log_callback = log_callback
        self.audit_callback = audit_callback
        self.max_stdin_bytes = int(max_stdin_bytes)
        self.parallel_enabled = bool(parallel_enabled)
        self.max_parallel_workers = max(1, int(max_parallel_workers))
        self._compiled_matchers: dict[tuple[str, str, str], re.Pattern[str]] = {}

    @property
    def has_any_hooks(self) -> bool:
        return self.config.has_any_hooks

    def fire_session_start(
        self,
        *,
        cwd: Path,
        active_workdir_relpath: str,
        payload: dict[str, Any] | None = None,
        session_source: str = "startup",
    ) -> HookDispatchResult:
        effective_payload = dict(payload or {})
        effective_payload["session_source"] = session_source
        return self._dispatch(
            event_name="SessionStart",
            cwd=cwd,
            active_workdir_relpath=active_workdir_relpath,
            payload=effective_payload,
        )

    def fire_session_end(
        self,
        *,
        cwd: Path,
        active_workdir_relpath: str,
        payload: dict[str, Any] | None = None,
    ) -> HookDispatchResult:
        return self._dispatch(
            event_name=_SESSION_END_EVENT,
            cwd=cwd,
            active_workdir_relpath=active_workdir_relpath,
            payload=dict(payload or {}),
        )

    def fire_user_prompt_submit(
        self,
        *,
        prompt: str,
        image_paths: list[str] | None,
        cwd: Path,
        active_workdir_relpath: str,
    ) -> HookDispatchResult:
        return self._dispatch(
            event_name="UserPromptSubmit",
            cwd=cwd,
            active_workdir_relpath=active_workdir_relpath,
            payload={
                "prompt": prompt,
                "image_paths": list(image_paths or []),
            },
        )

    def fire_pre_tool_use(
        self,
        *,
        tool_name: str,
        tool_input: dict[str, Any],
        cwd: Path,
        active_workdir_relpath: str,
        step: int,
    ) -> HookDispatchResult:
        return self._dispatch(
            event_name="PreToolUse",
            cwd=cwd,
            active_workdir_relpath=active_workdir_relpath,
            step=step,
            matcher_target=tool_name,
            payload={"tool_name": tool_name, "tool_input": copy.deepcopy(tool_input)},
        )

    def fire_post_tool_use(
        self,
        *,
        tool_name: str,
        tool_input: dict[str, Any],
        tool_response: dict[str, Any],
        cwd: Path,
        active_workdir_relpath: str,
        step: int,
    ) -> HookDispatchResult:
        return self._dispatch(
            event_name="PostToolUse",
            cwd=cwd,
            active_workdir_relpath=active_workdir_relpath,
            step=step,
            matcher_target=tool_name,
            payload={
                "tool_name": tool_name,
                "tool_input": copy.deepcopy(tool_input),
                "tool_response": copy.deepcopy(tool_response),
            },
        )

    def fire_turn_complete(
        self,
        *,
        cwd: Path,
        active_workdir_relpath: str,
        payload: dict[str, Any],
    ) -> HookDispatchResult:
        return self._dispatch(
            event_name="TurnComplete",
            cwd=cwd,
            active_workdir_relpath=active_workdir_relpath,
            payload=payload,
        )

    def fire_stop(
        self,
        *,
        cwd: Path,
        active_workdir_relpath: str,
        payload: dict[str, Any],
    ) -> HookDispatchResult:
        return self.fire_turn_complete(
            cwd=cwd,
            active_workdir_relpath=active_workdir_relpath,
            payload=payload,
        )

    def fire_notification(
        self,
        *,
        cwd: Path,
        active_workdir_relpath: str,
        message: str,
        level: str = "info",
        cause: str = "",
        payload: dict[str, Any] | None = None,
    ) -> HookDispatchResult:
        effective_payload = dict(payload or {})
        effective_payload.setdefault("message", str(message or ""))
        effective_payload.setdefault("level", str(level or "info"))
        if cause:
            effective_payload.setdefault("cause", str(cause))
        return self._dispatch(
            event_name="Notification",
            cwd=cwd,
            active_workdir_relpath=active_workdir_relpath,
            payload=effective_payload,
        )

    def fire_pre_compact(
        self,
        *,
        cwd: Path,
        active_workdir_relpath: str,
        trigger: str,
        message_count: int,
        payload: dict[str, Any] | None = None,
    ) -> HookDispatchResult:
        effective_payload = dict(payload or {})
        effective_payload.setdefault("trigger", str(trigger or "auto"))
        effective_payload.setdefault("message_count", int(message_count))
        return self._dispatch(
            event_name="PreCompact",
            cwd=cwd,
            active_workdir_relpath=active_workdir_relpath,
            payload=effective_payload,
        )

    def fire_subagent_stop(
        self,
        *,
        cwd: Path,
        active_workdir_relpath: str,
        tool_name: str,
        subagent_name: str = "",
        subagent_session_id: str = "",
        status: str = "success",
        exit_code: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> HookDispatchResult:
        effective_payload = dict(payload or {})
        effective_payload["tool_name"] = str(tool_name or "")
        effective_payload["subagent_name"] = str(subagent_name or "")
        effective_payload["subagent_session_id"] = str(subagent_session_id or "")
        effective_payload["status"] = str(status or "")
        if exit_code is not None:
            effective_payload["exit_code"] = int(exit_code)
        return self._dispatch(
            event_name="SubagentStop",
            cwd=cwd,
            active_workdir_relpath=active_workdir_relpath,
            matcher_target=str(tool_name or ""),
            payload=effective_payload,
        )

    def _build_common_payload(
        self,
        *,
        event_name: CanonicalHookEventName,
        cwd: Path,
        active_workdir_relpath: str,
        hook_payload: dict[str, Any],
        step: int | None,
    ) -> dict[str, Any]:
        legacy_event_name = _legacy_event_alias(event_name)
        common_payload: dict[str, Any] = {
            "hook_event_name": event_name,
            "session_id": self.session_id,
            "mode": self.mode,
            "runtime_kind": self.runtime_kind,
            "cwd": os.fspath(cwd),
            "workspace_root": os.fspath(self.workspace_root),
            "repo_root": os.fspath(self.repo_root),
            "active_workdir_relpath": active_workdir_relpath,
        }
        if legacy_event_name is not None:
            common_payload["legacy_hook_event_name"] = legacy_event_name
            common_payload["hook_event_aliases"] = [legacy_event_name]
        if step is not None:
            common_payload["step"] = step
        common_payload.update(hook_payload)
        return common_payload

    def _dispatch(
        self,
        *,
        event_name: CanonicalHookEventName,
        cwd: Path,
        active_workdir_relpath: str,
        payload: dict[str, Any],
        matcher_target: str | None = None,
        step: int | None = None,
    ) -> HookDispatchResult:
        groups = self.config.groups_for_event(event_name)
        if not groups:
            return HookDispatchResult()

        blocking_supported = event_name in _BLOCKING_HOOK_EVENTS
        use_parallel = (
            self.parallel_enabled
            and event_name in _PARALLELIZABLE_EVENTS
            and self.max_parallel_workers > 1
        )
        if use_parallel:
            return self._dispatch_parallel(
                event_name=event_name,
                cwd=cwd,
                active_workdir_relpath=active_workdir_relpath,
                payload=payload,
                matcher_target=matcher_target,
                step=step,
                groups=groups,
                blocking_supported=blocking_supported,
            )

        state = MutableHookDispatchState()
        original_prompt = str(payload.get("prompt")) if "prompt" in payload else ""
        current_prompt = original_prompt
        current_tool_input = payload.get("tool_input")
        if not isinstance(current_tool_input, dict):
            current_tool_input = None

        short_circuit = False
        for group in groups:
            if short_circuit:
                break
            if not self._matcher_matches(
                group=group,
                event_name=event_name,
                matcher_target=matcher_target,
            ):
                continue

            for hook in group.hooks:
                if not self._hook_runtime_matches(hook):
                    continue
                if not self._hook_session_source_matches(
                    hook=hook,
                    session_source=str(payload.get("session_source") or "").strip().lower(),
                ):
                    continue

                hook_payload = dict(payload)
                if event_name == "UserPromptSubmit":
                    hook_payload["prompt"] = current_prompt
                if current_tool_input is not None and event_name in _TOOL_MATCHER_EVENTS:
                    hook_payload["tool_input"] = copy.deepcopy(current_tool_input)

                common_payload = self._build_common_payload(
                    event_name=event_name,
                    cwd=cwd,
                    active_workdir_relpath=active_workdir_relpath,
                    hook_payload=hook_payload,
                    step=step,
                )

                parsed = self._run_command_hook(
                    group=group,
                    hook=hook,
                    event_name=event_name,
                    payload=common_payload,
                    blocking_supported=blocking_supported,
                )
                state.additional_system_messages.extend(parsed.additional_system_messages)
                state.additional_user_messages.extend(parsed.additional_user_messages)
                state.system_notices.extend(parsed.system_notices)
                if parsed.stop_reason:
                    state.stop_reason = parsed.stop_reason
                if parsed.permission_decision:
                    state.permission_decision = parsed.permission_decision
                if parsed.permission_decision_reason:
                    state.permission_decision_reason = parsed.permission_decision_reason

                if parsed.modified_prompt is not None and event_name == "UserPromptSubmit":
                    current_prompt = parsed.modified_prompt
                    state.modified_prompt = parsed.modified_prompt
                if parsed.modified_input is not None and event_name == "PreToolUse":
                    current_tool_input = parsed.modified_input
                    state.modified_input = copy.deepcopy(parsed.modified_input)

                if parsed.ask_requested and event_name == "PreToolUse":
                    state.ask_requested = True
                    state.ask_reason = parsed.ask_reason
                    return state.freeze()

                if parsed.blocked:
                    if blocking_supported:
                        state.blocked = True
                        state.reason = parsed.reason
                        return state.freeze()
                    self._warn(
                        f"Ignoring blocking decision from {event_name} hook in {group.source_path}."
                    )
                elif parsed.halt and not blocking_supported:
                    self._warn(
                        f"{event_name} hook requested halt via continue=false; stopping remaining "
                        f"hooks for this event ({group.source_path})."
                    )
                    short_circuit = True
                    break
                elif parsed.allow_short_circuit:
                    short_circuit = True
                    break

        if event_name == "UserPromptSubmit":
            state.modified_prompt = current_prompt if current_prompt != original_prompt else None
        if current_tool_input is not None and event_name == "PreToolUse":
            original_input = payload.get("tool_input")
            if isinstance(original_input, dict) and current_tool_input != original_input:
                state.modified_input = copy.deepcopy(current_tool_input)
        return state.freeze()

    def _dispatch_parallel(
        self,
        *,
        event_name: CanonicalHookEventName,
        cwd: Path,
        active_workdir_relpath: str,
        payload: dict[str, Any],
        matcher_target: str | None,
        step: int | None,
        groups: tuple[ResolvedHookMatcherGroup, ...],
        blocking_supported: bool,
    ) -> HookDispatchResult:
        state = MutableHookDispatchState()
        plan: list[tuple[int, ResolvedHookMatcherGroup, CommandHookSpec, dict[str, Any]]] = []
        index = 0
        session_source = str(payload.get("session_source") or "").strip().lower()
        for group in groups:
            if not self._matcher_matches(
                group=group,
                event_name=event_name,
                matcher_target=matcher_target,
            ):
                continue
            for hook in group.hooks:
                if not self._hook_runtime_matches(hook):
                    continue
                if not self._hook_session_source_matches(
                    hook=hook,
                    session_source=session_source,
                ):
                    continue
                hook_payload = dict(payload)
                common_payload = self._build_common_payload(
                    event_name=event_name,
                    cwd=cwd,
                    active_workdir_relpath=active_workdir_relpath,
                    hook_payload=hook_payload,
                    step=step,
                )
                plan.append((index, group, hook, common_payload))
                index += 1

        if not plan:
            return state.freeze()

        results: dict[int, ParsedHookOutput] = {}
        with ThreadPoolExecutor(max_workers=self.max_parallel_workers) as executor:
            future_to_index = {
                executor.submit(
                    self._run_command_hook,
                    group=group,
                    hook=hook,
                    event_name=event_name,
                    payload=common_payload,
                    blocking_supported=blocking_supported,
                ): idx
                for idx, group, hook, common_payload in plan
            }
            for future in as_completed(future_to_index):
                idx = future_to_index[future]
                results[idx] = future.result()

        halt_detected = False
        halt_source_path: str | None = None
        for idx, group, _hook, _ in plan:
            parsed = results[idx]
            state.additional_system_messages.extend(parsed.additional_system_messages)
            state.additional_user_messages.extend(parsed.additional_user_messages)
            state.system_notices.extend(parsed.system_notices)
            if parsed.stop_reason:
                state.stop_reason = parsed.stop_reason
            if parsed.permission_decision:
                state.permission_decision = parsed.permission_decision
            if parsed.permission_decision_reason:
                state.permission_decision_reason = parsed.permission_decision_reason
            if parsed.blocked and not blocking_supported:
                self._warn(
                    f"Ignoring blocking decision from {event_name} hook in {group.source_path}."
                )
            if parsed.halt and not blocking_supported and not halt_detected:
                halt_detected = True
                halt_source_path = os.fspath(group.source_path)
        if halt_detected and halt_source_path is not None:
            self._warn(
                f"{event_name} hook requested halt via continue=false; remaining hooks for this "
                f"event already ran in parallel ({halt_source_path})."
            )
        return state.freeze()

    def _hook_runtime_matches(self, hook: CommandHookSpec) -> bool:
        if not hook.runtime_kinds:
            return True
        return self.runtime_kind in set(hook.runtime_kinds)

    def _hook_session_source_matches(
        self,
        *,
        hook: CommandHookSpec,
        session_source: str,
    ) -> bool:
        if not hook.session_source:
            return True
        if not session_source:
            return False
        return session_source in set(hook.session_source)

    def _matcher_matches(
        self,
        *,
        group: ResolvedHookMatcherGroup,
        event_name: CanonicalHookEventName,
        matcher_target: str | None,
    ) -> bool:
        matcher = str(group.matcher or "")
        if event_name not in _TOOL_MATCHER_EVENTS:
            return True
        if not matcher:
            return True
        if matcher_target is None:
            return False
        key = (event_name, matcher, os.fspath(group.source_path))
        compiled = self._compiled_matchers.get(key)
        if compiled is None:
            try:
                compiled = re.compile(matcher)
            except re.error as exc:
                self._warn(
                    f"Invalid {event_name} hook matcher {matcher!r} in {group.source_path}: {exc}"
                )
                self._compiled_matchers[key] = re.compile(r"(?!x)x")
                return False
            self._compiled_matchers[key] = compiled
        return compiled.search(matcher_target) is not None

    def _run_command_hook(
        self,
        *,
        group: ResolvedHookMatcherGroup,
        hook: CommandHookSpec,
        event_name: CanonicalHookEventName,
        payload: dict[str, Any],
        blocking_supported: bool,
    ) -> ParsedHookOutput:
        payload_was_truncated = False
        payload_bytes_original = 0
        if not hook.receives_full_payload:
            candidate = json.dumps(payload, ensure_ascii=True)
            payload_bytes_original = len(candidate.encode("utf-8"))
            if payload_bytes_original > self.max_stdin_bytes:
                payload = _truncate_large_strings(
                    payload, max_field_bytes=_DEFAULT_TRUNCATE_FIELD_BYTES
                )
                payload_was_truncated = True
        payload_json = json.dumps(payload, ensure_ascii=True)
        env = _build_hook_env(hook, base=os.environ)
        legacy_event_name = _legacy_event_alias(event_name)
        env.update(
            {
                "ALYSIS_HOOK_EVENT_NAME": event_name,
                "ALYSIS_SESSION_ID": self.session_id,
                "ALYSIS_WORKSPACE_ROOT": os.fspath(self.workspace_root),
                "ALYSIS_REPO_ROOT": os.fspath(self.repo_root),
                "ALYSIS_ACTIVE_WORKDIR": str(payload.get("cwd") or os.fspath(self.workspace_root)),
                "ALYSIS_ACTIVE_WORKDIR_RELPATH": str(payload.get("active_workdir_relpath") or "."),
            }
        )
        if legacy_event_name is not None:
            env["ALYSIS_HOOK_EVENT_ALIAS"] = legacy_event_name
        tool_name = payload.get("tool_name")
        if isinstance(tool_name, str) and tool_name.strip():
            env["ALYSIS_TOOL_NAME"] = tool_name.strip()

        try:
            started_at = time.monotonic()
            proc = subprocess.run(
                hook.command,
                shell=True,
                cwd=os.fspath(self.workspace_root),
                env=env,
                input=payload_json,
                **command_text(),
                capture_output=True,
                check=False,
                timeout=hook.timeout,
            )
            duration_ms = max(0, int((time.monotonic() - started_at) * 1000))
        except subprocess.TimeoutExpired:
            message = (
                f"{event_name} hook timed out after {hook.timeout:g}s: {hook.command!r} "
                f"({group.source_path})"
            )
            self._log_hook(
                HookInvocationContext(
                    event_name=event_name,
                    source_path=os.fspath(group.source_path),
                    source_scope=group.source_scope,
                    matcher=group.matcher,
                    hook_id=hook.id or "",
                    priority=hook.priority,
                    failure_policy=hook.failure_policy,
                    command=hook.command,
                    timeout_s=hook.timeout,
                    trusted=group.trusted,
                    status="blocked"
                    if blocking_supported and group.trusted and hook.failure_policy == "block"
                    else "warning",
                    warnings=(message,) if hook.failure_policy != "continue" else (),
                    duration_ms=max(0, int(hook.timeout * 1000)),
                )
            )
            return self._hook_failure_output(
                event_name=event_name,
                message=message,
                failure_policy=hook.failure_policy,
                trusted=group.trusted,
                blocking_supported=blocking_supported,
            )
        except OSError as exc:
            message = (
                f"Failed to execute {event_name} hook {hook.command!r} ({group.source_path}): {exc}"
            )
            self._log_hook(
                HookInvocationContext(
                    event_name=event_name,
                    source_path=os.fspath(group.source_path),
                    source_scope=group.source_scope,
                    matcher=group.matcher,
                    hook_id=hook.id or "",
                    priority=hook.priority,
                    failure_policy=hook.failure_policy,
                    command=hook.command,
                    timeout_s=hook.timeout,
                    trusted=group.trusted,
                    status="blocked"
                    if blocking_supported and group.trusted and hook.failure_policy == "block"
                    else "warning",
                    warnings=(message,) if hook.failure_policy != "continue" else (),
                    duration_ms=0,
                )
            )
            return self._hook_failure_output(
                event_name=event_name,
                message=message,
                failure_policy=hook.failure_policy,
                trusted=group.trusted,
                blocking_supported=blocking_supported,
            )

        parsed = self._parse_hook_output(
            event_name=event_name,
            stdout_text=str(proc.stdout or ""),
            stderr_text=str(proc.stderr or ""),
            returncode=int(proc.returncode),
            failure_policy=hook.failure_policy,
            trusted=group.trusted,
            blocking_supported=blocking_supported,
        )
        self._log_hook(
            HookInvocationContext(
                event_name=event_name,
                source_path=os.fspath(group.source_path),
                source_scope=group.source_scope,
                matcher=group.matcher,
                hook_id=hook.id or "",
                priority=hook.priority,
                failure_policy=hook.failure_policy,
                command=hook.command,
                timeout_s=hook.timeout,
                trusted=group.trusted,
                returncode=int(proc.returncode),
                blocked=parsed.blocked,
                modified_input=parsed.modified_input is not None,
                modified_prompt=parsed.modified_prompt is not None,
                additional_system_message_count=len(parsed.additional_system_messages),
                additional_user_message_count=len(parsed.additional_user_messages),
                system_notices_count=len(parsed.system_notices),
                halt_requested=parsed.halt,
                allow_short_circuited=parsed.allow_short_circuit,
                suppress_output=parsed.suppress_output,
                permission_decision=parsed.permission_decision,
                stop_reason=parsed.stop_reason,
                stdout_chars=0 if parsed.suppress_output else parsed.stdout_chars,
                stderr_chars=parsed.stderr_chars,
                modified_input_fields=tuple(sorted((parsed.modified_input or {}).keys())),
                modified_prompt_chars=len(parsed.modified_prompt or ""),
                duration_ms=duration_ms,
                status=parsed.status,
                warnings=parsed.warnings,
                payload_truncated=payload_was_truncated,
                payload_bytes=payload_bytes_original,
            )
        )
        for warning in parsed.warnings:
            self._warn(warning)
        return parsed

    def _parse_hook_output(
        self,
        *,
        event_name: CanonicalHookEventName,
        stdout_text: str,
        stderr_text: str,
        returncode: int,
        failure_policy: str,
        trusted: bool,
        blocking_supported: bool,
    ) -> ParsedHookOutput:
        stdout_text = stdout_text.strip()
        stderr_text = stderr_text.strip()
        warnings: list[str] = []
        output_obj: dict[str, Any] = {}
        blocked = False
        reason = stderr_text or stdout_text
        status = "ok"

        if stdout_text:
            try:
                raw_output = json.loads(stdout_text)
            except json.JSONDecodeError:
                raw_output = None
                if returncode == 0:
                    warnings.append(
                        f"{event_name} hook ignored non-JSON stdout; hooks must return a JSON object."
                    )
            if raw_output is not None:
                if isinstance(raw_output, dict):
                    output_obj = raw_output
                elif returncode == 0:
                    warnings.append(
                        f"{event_name} hook ignored JSON stdout that was not an object."
                    )

        hook_specific = output_obj.get("hookSpecificOutput")
        if not isinstance(hook_specific, dict):
            hook_specific = {}

        decision = _coerce_decision(output_obj.get("decision"))
        if not decision:
            decision = _coerce_decision(hook_specific.get("decision"))

        perm_decision = _coerce_decision(hook_specific.get("permissionDecision"))
        perm_decision_reason = str(hook_specific.get("permissionDecisionReason") or "").strip()

        continue_value = output_obj.get("continue")
        halt = continue_value is False
        stop_reason_text = str(output_obj.get("stopReason") or "").strip()
        if not stop_reason_text:
            stop_reason_text = str(hook_specific.get("stopReason") or "").strip()

        suppress_output = bool(output_obj.get("suppressOutput"))

        system_notices: list[str] = []
        system_message_raw = output_obj.get("systemMessage")
        if isinstance(system_message_raw, str):
            text = system_message_raw.strip()
            if text:
                system_notices.append(text)

        allow_short_circuit = False
        block_decisions = {"block", "blocked", "deny", "denied"}
        allow_decisions = {"allow", "approve", "approved"}
        if returncode == 2 or decision in block_decisions or perm_decision in block_decisions:
            blocked = True
            status = "blocked"
            reason = (
                str(output_obj.get("reason") or "").strip()
                or str(hook_specific.get("reason") or "").strip()
                or perm_decision_reason
                or stderr_text
                or stdout_text
                or "blocked by hook"
            )
        elif halt and blocking_supported:
            blocked = True
            status = "blocked"
            reason = stop_reason_text or reason or "halted by hook"
        elif decision in allow_decisions or perm_decision in allow_decisions:
            allow_short_circuit = True
        elif returncode != 0:
            warning_message = (
                f"{event_name} hook failed with exit code {returncode}."
                if not stderr_text
                else f"{event_name} hook failed with exit code {returncode}: {stderr_text}"
            )
            return self._hook_failure_output(
                event_name=event_name,
                message=warning_message,
                failure_policy=failure_policy,
                trusted=trusted,
                blocking_supported=blocking_supported,
                stdout_chars=len(stdout_text),
                stderr_chars=len(stderr_text),
                reason=reason,
            )

        ask_decisions = {"ask"}
        ask_requested = False
        ask_reason = ""
        if (
            not blocked
            and not allow_short_circuit
            and (decision in ask_decisions or perm_decision in ask_decisions)
        ):
            ask_requested = True
            ask_reason = (
                perm_decision_reason
                or str(output_obj.get("reason") or "").strip()
                or stop_reason_text
                or "hook requested approval"
            )

        modified_input = output_obj.get("modifiedInput")
        if modified_input is None:
            modified_input = hook_specific.get("modifiedInput")
        if modified_input is not None and not isinstance(modified_input, dict):
            warnings.append(
                f"{event_name} hook ignored modifiedInput because it was not an object."
            )
            modified_input = None

        modified_prompt = output_obj.get("modifiedPrompt")
        if modified_prompt is None:
            modified_prompt = hook_specific.get("modifiedPrompt")
        if modified_prompt is not None:
            if isinstance(modified_prompt, str):
                modified_prompt = modified_prompt.strip()
            else:
                warnings.append(
                    f"{event_name} hook ignored modifiedPrompt because it was not a string."
                )
                modified_prompt = None

        additional_system_messages: list[str] = []
        additional_system_messages.extend(
            _normalize_text_messages(output_obj.get("additionalContext"))
        )
        additional_system_messages.extend(
            _normalize_text_messages(output_obj.get("additionalSystemMessages"))
        )
        additional_system_messages.extend(
            _normalize_text_messages(hook_specific.get("additionalContext"))
        )
        additional_system_messages.extend(
            _normalize_text_messages(hook_specific.get("additionalSystemMessages"))
        )
        additional_user_messages = _normalize_text_messages(
            output_obj.get("additionalUserMessages")
        )
        additional_user_messages.extend(
            _normalize_text_messages(hook_specific.get("additionalUserMessages"))
        )

        if returncode == 0 and warnings:
            status = "warning"

        return ParsedHookOutput(
            blocked=blocked,
            reason=reason,
            modified_input=copy.deepcopy(modified_input)
            if isinstance(modified_input, dict)
            else None,
            modified_prompt=modified_prompt,
            additional_system_messages=tuple(additional_system_messages),
            additional_user_messages=tuple(additional_user_messages),
            system_notices=tuple(system_notices),
            halt=halt,
            stop_reason=stop_reason_text,
            allow_short_circuit=allow_short_circuit,
            suppress_output=suppress_output,
            permission_decision=perm_decision or "",
            permission_decision_reason=perm_decision_reason,
            stdout_chars=len(stdout_text),
            stderr_chars=len(stderr_text),
            status=status,
            warnings=tuple(warnings),
            ask_requested=ask_requested,
            ask_reason=ask_reason,
        )

    def _hook_failure_output(
        self,
        *,
        event_name: CanonicalHookEventName,
        message: str,
        failure_policy: str,
        trusted: bool,
        blocking_supported: bool,
        stdout_chars: int = 0,
        stderr_chars: int = 0,
        reason: str = "",
    ) -> ParsedHookOutput:
        if failure_policy == "block" and trusted and blocking_supported:
            return ParsedHookOutput(
                blocked=True,
                reason=reason or message,
                stdout_chars=stdout_chars,
                stderr_chars=stderr_chars,
                status="blocked",
            )
        warnings = () if failure_policy == "continue" else (message,)
        return ParsedHookOutput(
            blocked=False,
            reason=reason or message,
            stdout_chars=stdout_chars,
            stderr_chars=stderr_chars,
            status="warning" if warnings else "ok",
            warnings=warnings,
        )

    def _log_hook(self, context: HookInvocationContext) -> None:
        if self.log_callback is None:
            pass
        else:
            self.log_callback(
                "hook_command",
                {
                    "event_name": context.event_name,
                    "source_path": context.source_path,
                    "source_scope": context.source_scope,
                    "matcher": context.matcher,
                    "hook_id": context.hook_id,
                    "priority": context.priority,
                    "failure_policy": context.failure_policy,
                    "command": context.command,
                    "timeout_s": context.timeout_s,
                    "trusted": context.trusted,
                    "returncode": context.returncode,
                    "blocked": context.blocked,
                    "modified_input": context.modified_input,
                    "modified_input_fields": list(context.modified_input_fields),
                    "modified_prompt": context.modified_prompt,
                    "modified_prompt_chars": context.modified_prompt_chars,
                    "additional_system_message_count": context.additional_system_message_count,
                    "additional_user_message_count": context.additional_user_message_count,
                    "system_notices_count": context.system_notices_count,
                    "halt_requested": context.halt_requested,
                    "allow_short_circuited": context.allow_short_circuited,
                    "suppress_output": context.suppress_output,
                    "permission_decision": context.permission_decision,
                    "stop_reason": context.stop_reason,
                    "stdout_chars": context.stdout_chars,
                    "stderr_chars": context.stderr_chars,
                    "duration_ms": context.duration_ms,
                    "status": context.status,
                    "warnings": list(context.warnings),
                    "payload_truncated": context.payload_truncated,
                    "payload_bytes": context.payload_bytes,
                },
            )
        if self.audit_callback is not None:
            self.audit_callback(build_hook_audit_event(session_id=self.session_id, context=context))

    def _warn(self, message: str) -> None:
        clean = str(message or "").strip()
        if not clean:
            return
        if self.warning_callback is not None:
            self.warning_callback(clean)


__all__ = ["HookDispatcher"]
