# Lifecycle Hooks

Lifecycle hooks let users and teams run deterministic command-based policy around Alysis Code sessions and tool calls.

Hooks are separate from other extension surfaces:

- repo convention files such as `AGENTS.md`, `CLAUDE.md`, and `CONVENTIONS.md` provide read-only guidance
- skills provide reusable task instructions
- MCP servers and custom tools add capabilities
- hooks execute local commands and can allow, block, rewrite, or annotate selected runtime events

Only trust hook configuration you have reviewed.

## Config Layers

Hooks load from three locations, in order:

1. `~/.config/alysis/hooks.json`
2. `<workspace>/.alysis/hooks.json`
3. `<workspace>/.alysis/hooks.local.json`

More specific layers override earlier hooks with the same `id`. Hooks without an `id` accumulate.

Trust rules:

- user hooks are trusted by location
- `hooks.local.json` is trusted by location and should normally be gitignored
- project `hooks.json` is not trusted by default

Trust or untrust a project hook config with:

```bash
alysis hooks trust --path .
alysis hooks untrust --path .
```

Trust is keyed to the workspace, config path, and file hash. Editing the project hook file invalidates trust and requires review again.

`alysis hooks init --path .` writes a starter local config and adds `.alysis/hooks.local.json` to `.gitignore` when needed.

## Events

Common hook events are:

| Event | Blocking | Purpose |
| --- | --- | --- |
| `SessionStart` | Yes | A session starts, resumes, or forks. |
| `UserPromptSubmit` | Yes | A user prompt is submitted for the next turn. |
| `PreToolUse` | Yes | A tool call is about to run. |
| `PostToolUse` | No | A tool call has completed. |
| `TurnComplete` | No | A turn has finished. |
| `SessionEnd` | No | A session has closed. |
| `Notification` | No | The runtime emits an attention or policy signal. |
| `PreCompact` | No | Conversation compaction has just run. |
| `SubagentStop` | No | A subagent-like tool result has completed. |

`Stop` is accepted as an alias for `TurnComplete`.

`SessionEnd` is attempted at most once per session instance. The attempt is
recorded before context preparation and dispatch, so a failure, timeout,
concurrent or reentrant close, or later cleanup failure does not replay it.
This does not certify cleanup success or relax the caller's existing cleanup
rules: a retained close failure remains blocked where the caller requires it.
There is no exactly-once guarantee across process crashes; an uncertain hook
may already have produced external effects.

Blocking events can stop execution. Non-blocking events can record output, inject context for later handling, or stop remaining hooks for the same event, but they cannot retroactively block work that already happened.

## Config Format

Hooks are grouped by event. `matcher` is used for tool-related events such as `PreToolUse`, `PostToolUse`, and `SubagentStop`.

```json
{
  "schema_version": 1,
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "shell_run|fs_write|fs_patch",
        "hooks": [
          {
            "type": "command",
            "id": "policy.block-dangerous-shell",
            "description": "Reject dangerous shell commands.",
            "command": "python docs/examples/hooks/block_dangerous.py",
            "priority": 100,
            "timeout": 5,
            "failurePolicy": "block",
            "envPassthrough": "safe",
            "enabled": true
          }
        ]
      }
    ]
  }
}
```

Supported command-hook fields include:

- `type`: currently `command`
- `command`: shell command executed with JSON on stdin
- `id`: optional stable id for override and enable/disable operations
- `description`: optional human-readable summary
- `priority`: higher values run first within a matcher group
- `timeout` or `timeoutMs`
- `failurePolicy`: `warn`, `continue`, or `block`
- `runtimeKinds`: optional runtime filter
- `sessionSource`: optional `startup`, `resume`, or `fork` filter
- `envPassthrough`: `safe`, `explicit`, or `all`
- `envAllow`: explicit environment variables to pass through
- `env`: explicit environment overrides
- `receivesFullPayload`: opt into full hook payloads when required
- `enabled`: enable or disable the hook entry

## Command Protocol

Each hook receives a JSON object on stdin. Common fields include:

- `hook_event_name`
- `session_id`
- `mode`
- `runtime_kind`
- `cwd`
- `workspace_root`
- `repo_root`
- `active_workdir_relpath`

Tool events also include `tool_name`, `tool_input`, and for post-tool hooks, `tool_response`.

Hooks may emit a JSON object on stdout. Useful fields include:

- `decision: "block"` or `"deny"` to block a blocking event
- `decision: "allow"` or `"approve"` to allow and skip remaining hooks for that event
- `continue: false` to halt the current hook chain
- `reason` or `stopReason` for user-visible explanations
- `modifiedPrompt` for `UserPromptSubmit`
- `modifiedInput` for `PreToolUse`
- `systemMessage` for a transient user-facing banner
- `additionalSystemMessages` or `additionalUserMessages` for injected context
- `hookSpecificOutput.permissionDecision: "ask"` to request an approval prompt on `PreToolUse`

Exit code `0` means continue. Exit code `2` blocks blocking events. Other non-zero exits follow `failurePolicy`.

## Environment Handling

By default, hooks use `envPassthrough: "safe"`, which removes common secret-looking variables before executing the command.

Use:

- `safe` for normal hooks
- `explicit` when the hook should receive only the minimal baseline plus selected variables
- `all` only for trusted personal hooks that intentionally need the full parent environment

Use `envAllow` to pass specific variables and `env` to set explicit values. `env` values are applied last.

## CLI

Inspect hooks:

```bash
alysis hooks list --path .
alysis hooks doctor --path .
alysis hooks effective --event PreToolUse --tool shell_run --path .
```

Trust project hooks:

```bash
alysis hooks trust --path .
alysis hooks untrust --path .
```

Dry-run matching without executing hook commands:

```bash
alysis hooks test --path . --event SessionStart --session-source startup
alysis hooks test --path . --event PreToolUse --tool shell_run
```

Enable or disable hooks by id:

```bash
alysis hooks enable policy.block-dangerous-shell --layer local
alysis hooks disable policy.block-dangerous-shell --layer local
```

Inspect per-session hook audit records:

```bash
alysis hooks trace <session_id>
alysis hooks watch <session_id>
```

## Recipes

Example hook scripts live under `docs/examples/hooks/`:

- `block_dangerous.py`: block dangerous shell commands before execution
- `block_destructive_git.py`: block destructive git-related patches
- `block_env_files.py`: block reads or writes of environment and secret files
- `format_on_write.py`: rewrite file content before a write lands
- `notify_done.py`: send a generic turn-complete notification
- `notify_done_macos.py`: send a macOS notification through `osascript`
- `secret_scanner.py`: block writes that look like secrets
- `sample_hooks.json`: compact starter configuration

Alysis Code also includes `alysis_code.builtin_hooks.notify_done_windows`, which can be referenced as:

```json
{
  "hooks": {
    "TurnComplete": [
      {
        "hooks": [
          {
            "type": "command",
            "id": "personal.notify",
            "command": "python -m alysis_code.builtin_hooks.notify_done_windows",
            "timeout": 2
          }
        ]
      }
    ]
  }
}
```

## Audit And Limits

Hook invocations are recorded in the session transcript and in a redacted per-session artifact under `hooks/hook_runs.jsonl`. The artifact stores counts and previews, not raw command output.

Hook stdin payloads are capped. Large fields such as content, patch, diff, stdout, stderr, and body text may be replaced with a truncation marker unless the hook opts into `receivesFullPayload: true`.

Use full payloads only for hooks that genuinely need them, such as content scanners.

## Security Notes

- Hooks run local commands. Review hook files and config before trusting them.
- Avoid committing `hooks.local.json`; keep it for machine-local automation.
- Keep `envPassthrough` at `safe` or `explicit` unless the hook requires broader environment access.
- Do not rely on `suppressOutput` as a secret boundary. Audit metadata still records output sizes.
- Prefer small, targeted hooks over broad scripts that inspect or mutate unrelated state.
