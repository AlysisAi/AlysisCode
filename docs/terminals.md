# Background terminals

## Overview

Background terminals let Alysis Code start long-running shell commands without blocking the agent loop.
They are meant for dev servers, file watchers, test runners in watch mode, log tailers, and similar
processes where the agent needs to keep working while output accumulates in the background.

The feature is session-scoped. A background process belongs to the current chat session, can be
listed or inspected later, and is shut down when the session closes.

When a task explicitly requires a server or daemon to remain alive after the agent finalizes, use
the durable service tools instead. Durable services are tracked by `service_id`, write detached logs
under the runtime sessions directory, and are not reaped by `AgentSession.close`.

## Tools

The LLM can use background-terminal tools in write-capable chat modes.

| Tool | Description |
|---|---|
| `shell_background` | Start a background process under the workspace root. |
| `shell_output` | Read accumulated stdout and stderr from a background process. |
| `shell_wait` | Wait briefly for new output or process exit without busy polling. |
| `shell_kill` | Terminate a background process by `process_id`. |
| `shell_list` | List background processes tracked by the current session. |
| `shell_service_start` | Start an explicit durable service with a readiness probe. |
| `shell_service_status` | Recheck durable service status and readiness by `service_id`. |
| `shell_service_stop` | Stop a durable service by `service_id`. |

`shell_background` uses the same command safety checks as `shell_run`. In review mode, starting a
background process can request approval before the process is spawned.

`shell_output`, `shell_wait`, `shell_kill`, and `shell_list` operate on
already-started processes. They do not run new shell commands. `shell_wait`
accepts a `since` cursor and an `until` mode of `output_available`,
`process_exited`, or `either`; its wait time is clamped by the active run
deadline and by the finalization window.

`shell_service_start` uses the same shell safety checks, plus the same checks for command-based
readiness probes. Supported readiness probes are process-alive, TCP host/port, Unix socket
existence, and bounded command probes. `shell_service_status` re-runs the stored readiness probe.
`shell_service_stop` removes runtime metadata after stopping the service.

## Slash command

The chat UI also provides a direct user command:

```text
/terminals
/terminals list
/terminals show <process_id>
/terminals kill <process_id>
/terminals help
```

`/terminals` is an alias for `/terminals list`.

`/terminals list` prints all tracked background processes in start order. The table includes:

- `process_id`
- command preview
- status
- exit code
- runtime

`/terminals show <process_id>` prints the current output snapshot from the beginning of the process
buffer. It includes status, exit code, failure reason when present, runtime, dropped-line count, and
up to 200 displayed output lines.

`/terminals kill <process_id>` terminates the process and prints the resulting status and exit code.
Unknown process ids are reported as normal command output and do not crash the chat loop.

`/terminals help` prints the usage block.

Readonly chat sessions can use `/terminals list`, `/terminals show`, and `/terminals help`.
Readonly sessions cannot use `/terminals kill`; the command prints a message and leaves the process
unchanged.

## Output buffering

Each background process has a bounded in-memory output buffer. Stdout and stderr are decoded as
UTF-8 with replacement for invalid bytes, then stored as line records with:

- sequence number
- stream name, `stdout` or `stderr`
- text
- timestamp

The buffer is a ring buffer. It is designed so a chatty process cannot block the reader threads or
grow memory without bound.

Two caps apply to the combined process output buffer:

- `background_output_max_lines` limits the number of retained stdout and stderr lines together.
- `background_output_max_bytes` limits retained stdout and stderr bytes together.

When either cap is exceeded, the oldest retained lines are dropped. The process snapshot reports a
cumulative `dropped_lines` counter so the agent or user can tell that output was lost.

Partial lines are handled without blocking. If a process writes `abc` without a trailing newline, the
reader keeps that text as pending data. When the stream closes, the pending partial line is flushed
into the output buffer.

For very chatty commands, poll more often with `shell_output` or inspect the process through
`/terminals show`. If `dropped_lines` is greater than zero, older output has already been evicted.

## Lifecycle

Background terminals are scoped to one Alysis Code session.

Starting a process returns a `process_id`. That id is stable for the lifetime of the process record
and can be passed to:

- `shell_output`
- `shell_kill`
- `/terminals show`
- `/terminals kill`

When a process exits naturally, the session keeps its summary and recent output until the manager
prunes old terminal records.

When a process is killed, Alysis Code first sends a graceful termination signal:

- POSIX process-group runners receive `SIGTERM`.
- Windows runners receive the platform control-break equivalent where supported.
- Direct runners, such as Docker background cleanup, terminate the tracked process directly.

If the process does not exit within `background_kill_timeout_s`, Alysis Code escalates to a forced
kill.

When the chat session closes, all running background processes are terminated. Dev servers,
watchers, and log tailers started by `shell_background` do not survive past the chat session.
Durable services started by `shell_service_start` survive session close until stopped explicitly.

## Sandbox compatibility

Background processes honor the same shell sandbox settings as `shell_run`.

In host mode, a background command runs on the host with process isolation appropriate to the
platform.

In Docker mode, each background process gets its own container. Container names use the
`alysis-bgsbx-` prefix so they are distinguishable from synchronous shell sandbox containers.
Cleanup is best-effort and removes the container when the process exits or is killed.

In bubblewrap mode, background commands use the same generated bubblewrap argv as synchronous shell
runs. Bubblewrap background processes use `--die-with-parent` and `--unshare-pid` so the sandbox
process tree is tied to the tracked parent process.

Durable services reuse the sandbox backend but detach from the session parent. Bubblewrap durable
services remove `--die-with-parent`, and Docker durable services keep a named container that
`shell_service_stop` cleans up. Metadata records the backend, PID/PGID or container name, readiness
configuration, and stdout/stderr log paths.

The default network policy is `network=off`. With networking disabled, a background dev server
started inside a sandbox is not reachable from the host browser. Enable shell sandbox networking
only when that access is required and acceptable for the repository.

## Configuration reference

| Setting | Config key | Env var | Default |
|---|---|---|---|
| Max concurrent | `shell_sandbox.background_max_concurrent` | `ALYSIS_SHELL_SANDBOX_BACKGROUND_MAX_CONCURRENT` | `4` |
| Output max lines, combined stdout/stderr | `shell_sandbox.background_output_max_lines` | `ALYSIS_SHELL_SANDBOX_BACKGROUND_OUTPUT_MAX_LINES` | `2000` |
| Output max bytes, combined stdout/stderr | `shell_sandbox.background_output_max_bytes` | `ALYSIS_SHELL_SANDBOX_BACKGROUND_OUTPUT_MAX_BYTES` | `262144` |
| Kill grace period, seconds | `shell_sandbox.background_kill_timeout_s` | `ALYSIS_SHELL_SANDBOX_BACKGROUND_KILL_TIMEOUT_S` | `10.0` |

Example config:

```toml
[shell_sandbox]
background_max_concurrent = 4
background_output_max_lines = 2000
background_output_max_bytes = 262144
background_kill_timeout_s = 10.0
```

Environment variables override config values:

```bash
export ALYSIS_SHELL_SANDBOX_BACKGROUND_MAX_CONCURRENT=6
export ALYSIS_SHELL_SANDBOX_BACKGROUND_OUTPUT_MAX_LINES=4000
export ALYSIS_SHELL_SANDBOX_BACKGROUND_OUTPUT_MAX_BYTES=524288
export ALYSIS_SHELL_SANDBOX_BACKGROUND_KILL_TIMEOUT_S=5.0
```

## Examples

### Start a dev server, read output, then kill it

Ask the agent to start the server:

```text
Use shell_background to run: python -m http.server 8000
```

Read output later:

```text
Use shell_output with the returned process_id and since=0.
```

After the server is no longer needed:

```text
Use shell_kill with the same process_id.
```

You can also inspect it directly:

```text
/terminals
/terminals show <process_id>
/terminals kill <process_id>
```

### Run pytest watch mode and a log tailer in parallel

Start the watcher:

```text
Use shell_background to run: pytest-watch
```

Start the log tailer:

```text
Use shell_background to run: tail -f var/app.log
```

List both:

```text
/terminals list
```

Read incremental output from each process:

```text
Use shell_output with since=<next_seq> from the previous read.
```

### Inspect running work mid-session

When you are unsure what is still active:

```text
/terminals
```

To inspect a process:

```text
/terminals show <process_id>
```

To clean up:

```text
/terminals kill <process_id>
```

## Limitations

Background terminals do not provide PTY support. Interactive prompts inside a background process can
hang because no user can type into the process.

There are no persistent shell sessions. Each `shell_background` call spawns a fresh process. Shell
state such as `cd`, exported variables, activated virtual environments, and shell aliases does not
carry between calls.

Use an explicit command when state is needed:

```bash
cd /path/to/repo && . .venv/bin/activate && python -m http.server 8000
```

Output is buffered, not streamed live. The agent and user can inspect current
output with `shell_output` or `/terminals show`; agents can use `shell_wait`
when a background process is expected to produce output after a short delay.

A single line larger than `background_output_max_bytes` is dropped rather than truncated. This keeps
buffer accounting simple and avoids storing oversized records.

Background processes are not durable jobs. Closing the chat session terminates running processes.
Use `shell_service_start` only when post-finalization service persistence is an explicit acceptance
requirement.

Repeated empty `shell_output` reads on a running process include guidance to use
`shell_wait` with the returned cursor. Use `shell_run` for commands expected to
complete quickly, and `shell_service_start` for explicit durable-service
requirements.
