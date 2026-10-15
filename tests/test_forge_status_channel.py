from __future__ import annotations

import json
import os
import threading

import pytest

from alysis_code.forge_status_channel import (
    FORGE_OPERATION_ID_ENV,
    FORGE_STATUS_EXIT_CODES,
    FORGE_STATUS_FD_ENV,
    FORGE_STATUS_NONCE_ENV,
    FORGE_STATUS_PROTOCOL,
    FORGE_STATUS_SCHEMA_VERSION,
    ForgeExecutionStatusSession,
    ForgePlanResult,
    ForgeStatusChannel,
    ForgeStatusChannelError,
    ForgeTerminalStatus,
)

NONCE = "0123456789abcdef0123456789abcdef"
TERMINAL_KEYS = {
    "protocol",
    "schema_version",
    "operation_id",
    "nonce",
    "run_id",
    "status",
    "reason_code",
    "exit_code",
    "message",
    "action",
    "provider_calls",
    "retryable",
}
PROGRESS_KEYS = {
    "protocol",
    "schema_version",
    "operation_id",
    "nonce",
    "event",
    "status",
    "sequence",
    "phase",
    "message",
    "task_id",
    "run_id",
}


def _read_records(read_fd: int) -> list[dict[str, object]]:
    try:
        return [json.loads(line) for line in os.read(read_fd, 65536).splitlines()]
    finally:
        os.close(read_fd)


def test_status_channel_is_authenticated_redacted_and_terminal_once() -> None:
    read_fd, write_fd = os.pipe()
    try:
        channel = ForgeStatusChannel(
            operation_id="desktop-operation",
            nonce=NONCE,
            fd=write_fd,
        )
        assert channel.emit_progress(
            phase="plan",
            message="token=super-secret",
            run_id="001-test",
        )
        assert channel.emit(
            ForgeTerminalStatus.SUCCEEDED,
            reason_code="completed",
            message="finished with sk-testsecret12345; prompt: private\nmultiline context",
            run_id="001-test",
            provider_calls=2,
        )
        assert channel.emitted
        assert not channel.emit(
            ForgeTerminalStatus.EXECUTION_FAILED,
            reason_code="late_failure",
            message="must be ignored",
        )
        assert not channel.emit_progress(phase="late", message="must be ignored")
    finally:
        os.close(write_fd)

    records = _read_records(read_fd)
    assert [record.get("event", "terminal") for record in records] == [
        "progress",
        "terminal",
    ]
    assert all(record["protocol"] == FORGE_STATUS_PROTOCOL for record in records)
    assert all(record["schema_version"] == FORGE_STATUS_SCHEMA_VERSION for record in records)
    assert all(record["operation_id"] == "desktop-operation" for record in records)
    assert all(record["nonce"] == NONCE for record in records)
    assert set(records[0]) <= PROGRESS_KEYS
    assert set(records[1]) <= TERMINAL_KEYS
    assert "super-secret" not in str(records[0]["message"])
    assert "sk-testsecret12345" not in str(records[1]["message"])
    assert "multiline context" not in str(records[1]["message"])
    assert records[1]["provider_calls"] == 2


def test_progress_and_heartbeat_sequences_are_strictly_increasing_under_threads() -> None:
    read_fd, write_fd = os.pipe()
    try:
        channel = ForgeStatusChannel(operation_id="threaded-op", nonce=NONCE, fd=write_fd)
        threads = [
            threading.Thread(
                target=channel.emit_progress,
                kwargs={
                    "phase": "worker",
                    "message": f"Worker {index}",
                    "heartbeat": index % 2 == 0,
                },
            )
            for index in range(24)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        os.close(write_fd)

    records = _read_records(read_fd)
    assert [record["sequence"] for record in records] == list(range(1, 25))
    assert {record["event"] for record in records} == {"progress", "heartbeat"}
    assert all(set(record) <= PROGRESS_KEYS for record in records)


def test_from_environment_is_optional_only_when_the_whole_tuple_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (FORGE_STATUS_FD_ENV, FORGE_STATUS_NONCE_ENV, FORGE_OPERATION_ID_ENV):
        monkeypatch.delenv(name, raising=False)

    assert ForgeStatusChannel.from_environment(required=False) is None
    with pytest.raises(ForgeStatusChannelError, match="environment is incomplete"):
        ForgeStatusChannel.from_environment(required=True)

    monkeypatch.setenv(FORGE_STATUS_FD_ENV, "3")
    with pytest.raises(ForgeStatusChannelError, match="environment is incomplete"):
        ForgeStatusChannel.from_environment(required=False)

    monkeypatch.setenv(FORGE_OPERATION_ID_ENV, "environment-op")
    monkeypatch.setenv(FORGE_STATUS_NONCE_ENV, NONCE)
    channel = ForgeStatusChannel.from_environment(required=True)
    assert channel is not None
    assert channel.fd == 3
    assert channel.operation_id == "environment-op"
    assert channel.nonce == NONCE


@pytest.mark.parametrize(
    ("status", "exit_code"),
    [(status, exit_code) for status, exit_code in FORGE_STATUS_EXIT_CODES.items()],
)
def test_terminal_status_uses_the_canonical_exit_code(
    status: ForgeTerminalStatus,
    exit_code: int,
) -> None:
    read_fd, write_fd = os.pipe()
    try:
        channel = ForgeStatusChannel(operation_id="exit-code-op", nonce=NONCE, fd=write_fd)
        assert channel.emit(status, reason_code="finished", message="Finished")
    finally:
        os.close(write_fd)

    [record] = _read_records(read_fd)
    assert record["status"] == status.value
    assert record["exit_code"] == exit_code


def test_terminal_status_rejects_decoder_incompatible_values_before_writing() -> None:
    read_fd, write_fd = os.pipe()
    try:
        channel = ForgeStatusChannel(operation_id="validation-op", nonce=NONCE, fd=write_fd)
        with pytest.raises(ValueError, match="does not match"):
            channel.emit(ForgeTerminalStatus.SUCCEEDED, exit_code=1)
        with pytest.raises(ValueError, match="safe integer"):
            channel.emit(
                ForgeTerminalStatus.SUCCEEDED,
                provider_calls=2**53,
            )
        with pytest.raises(ValueError, match="retryable"):
            channel.emit(ForgeTerminalStatus.SUCCEEDED, retryable=1)  # type: ignore[arg-type]
    finally:
        os.close(write_fd)

    assert _read_records(read_fd) == []


def test_text_bounds_match_electron_utf16_length_semantics() -> None:
    read_fd, write_fd = os.pipe()
    try:
        channel = ForgeStatusChannel(operation_id="utf16-op", nonce=NONCE, fd=write_fd)
        assert channel.emit(
            ForgeTerminalStatus.EXECUTION_FAILED,
            message="U0001f680" * 5000,
            action="U0001f680" * 5000,
        )
    finally:
        os.close(write_fd)

    [record] = _read_records(read_fd)
    assert len(str(record["message"]).encode("utf-16-le")) // 2 <= 4096
    assert len(str(record["action"]).encode("utf-16-le")) // 2 <= 2048


def test_execution_status_session_emits_content_free_fallback_and_preserves_exception() -> None:
    class ProviderExit(RuntimeError):
        exit_code = 4

    read_fd, write_fd = os.pipe()
    caught: ProviderExit | None = None
    try:
        channel = ForgeStatusChannel(operation_id="fallback-op", nonce=NONCE, fd=write_fd)
        try:
            with ForgeExecutionStatusSession(
                channel,
                command="forge-run",
                run_id="001-test",
            ):
                raise ProviderExit("prompt: secret customer transcript")
        except ProviderExit as exc:
            caught = exc
    finally:
        os.close(write_fd)

    assert caught is not None
    [record] = _read_records(read_fd)
    assert record["status"] == ForgeTerminalStatus.PROVIDER_FAILED.value
    assert record["exit_code"] == 4
    assert record["reason_code"] == "forge_run_provider_failed"
    assert record["run_id"] == "001-test"
    assert "customer transcript" not in str(record["message"])


def test_specific_terminal_status_wins_over_execution_session_fallback() -> None:
    read_fd, write_fd = os.pipe()
    try:
        channel = ForgeStatusChannel(operation_id="specific-op", nonce=NONCE, fd=write_fd)
        with ForgeExecutionStatusSession(channel, command="forge_run"):
            assert channel.emit(
                ForgeTerminalStatus.VERIFICATION_FAILED,
                reason_code="verification_failed",
                message="Verification failed.",
                retryable=True,
            )
    finally:
        os.close(write_fd)

    [record] = _read_records(read_fd)
    assert record["status"] == ForgeTerminalStatus.VERIFICATION_FAILED.value
    assert record["reason_code"] == "verification_failed"


def test_dry_run_fallback_reports_the_known_zero_provider_calls() -> None:
    read_fd, write_fd = os.pipe()
    try:
        channel = ForgeStatusChannel(operation_id="dry-run-op", nonce=NONCE, fd=write_fd)
        with ForgeExecutionStatusSession(
            channel,
            command="forge_run",
            run_id="run_abcdefgh",
            dry_run=True,
        ):
            pass
    finally:
        os.close(write_fd)

    [record] = _read_records(read_fd)
    assert record["status"] == ForgeTerminalStatus.SUCCEEDED.value
    assert record["provider_calls"] == 0


def test_plan_result_bool_and_exit_code_contract() -> None:
    completed = ForgePlanResult.completed(provider_calls=3)
    incomplete = ForgePlanResult.incomplete(provider_calls=2)

    assert completed
    assert completed.exit_code == 0
    assert completed.provider_calls == 3
    assert not incomplete
    assert incomplete.exit_code == 1
    assert incomplete.retryable
