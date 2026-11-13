# Slash commands and native chat

Slash input in the Alysis Code sidebar is routed by an extension-side command router to typed
controllers and command-palette handlers instead of being sent to the model as chat text. Unknown
slash commands fail closed with help and are not forwarded to `chat.send`.

The composer uses **Permissions** (`readonly`, `review`, `auto`) and the agent's dynamic
**Persona** picker. Chat Plan/Act mode and `/plan` are retired. Use `/persona architect` for
design work or `/forge plan <instruction>` for a Forge plan. `/permissions` opens a picker;
named and numeric choices match the CLI, except full access is intentionally unavailable in
the IDE. `/mode` and `/subagent` provide migration guidance to `/permissions` and `/subagents`.
The one-turn `/ask` command is CLI-only; `/persona ask` persists until you switch persona.
Help and autocomplete show only commands supported by the connected agent and current task.

Supported commands: `/help`, `/permissions readonly`, `/permissions review`, `/permissions auto`, `/forge plan <instruction>`, `/execute preview`, `/execute plan`, `/plans`, `/open plan [plan_id]`,
`/diffs`, `/artifacts`, `/cancel`, `/doctor`, `/config`, `/config set <key=value>`,
`/update [cached|online]`, `/usage`, `/history <query>`, `/context`, `/ctx`, `/compact [focus]`,
`/resume <session_id>`, `/model <model>`, `/model-info [model]`, `/subagents status|on|off`,
`/stream on|off`, `/cd <path>`, `/pwd`, `/status`, `/clear`, `/image [path]`,
`/paste-image [path]`, `/images`, `/clear-images`, `/skills`, `/skill <name>`, `/tools`, `/hooks`,
`/mcp`, `/assets`, `/asset list`, `/asset show <asset_id>`, `/asset add <path>`,
`/asset delete <asset_id>`, `/asset edit <asset_id>`, `/asset refresh <asset_id>`,
`/asset cancel-pending`, `/asset check`, `/asset prune`, `/forge show [plan_id]`,
`/forge plan show [plan_id]`, `/profiles`, `/profile <name>`, `/profile use <name>`, `/forge plan state`,
`/forge plan validate`, `/forge plan regenerate [focus|instruction]`, `/assistant [show|text]`, `/goal [show|text]`,
`/task <task_id> ...`, `/trace`, `/trace events`, `/trace full`, `/terminals`, `/terminals list`,
`/terminals show <id>`, `/terminals kill <id>`, `/terminals clear <id>`, `/report [feedback]`, and
`/feedback [feedback]`.

Bare `/execute`, `/forge exec`, and `/forge execute` route only to Forge Execute Review; broad
execute arguments return an IDE v1 warning.

## Reference

| Command | Behavior | Trust / mutation |
| --- | --- | --- |
| `/help` | Render command help in the chat panel. | Safe |
| `/permissions readonly` | Set the active session mode, or the default mode when no session is active. | Safe |
| `/permissions review` | Set the active session mode, or the default mode when no session is active. | Requires Workspace Trust |
| `/permissions auto` | Set the active session mode, or the default mode when no session is active. | Requires Workspace Trust |
| `/forge plan <instruction>` | Start Forge Plan with the instruction. | Requires Workspace Trust and trusted executable origin |
| `/execute preview` | Run non-mutating Forge Execute Preview for the active plan. | Controller-gated; no SecretStorage forwarding |
| `/execute plan` | Run Forge Execute Review for the active plan after Preview and explicit confirmation. | Requires Workspace Trust |
| `/plans` | Open the persisted Forge plan picker. | Read-only bridge path |
| `/open plan [plan_id]` | Open a persisted plan by id or picker. | Read-only bridge path |
| `/diffs` | Open the diff picker for the active Forge plan. | Read-only diff access |
| `/artifacts` | Refresh active Forge plan artifacts. | Read-only artifact access |
| `/cancel` | Route through **Cancel Current Run**. | Honest backend cancellation result |
| `/doctor` | Run `alysis sandbox doctor --smoke`. | Process guard applies |
| `/config` | Open provider/configuration UI. | Safe; secrets stay in SecretStorage |
| `/config set <key=value>` | Set a non-secret config key through the bridge. | Requires Workspace Trust and confirmation; secret-looking keys/values are rejected |
| `/update [cached\|online]` | Show cached update status or explicitly check online. | Cached by default; network only after explicit `online`/selection |
| `/usage` | Show usage for the active session. | Active session required |
| `/history <query>` | Search active-session history. | Active session required |
| `/context` | Show estimated live context metadata. | Active session required; token counts are approximate |
| `/ctx` | Alias for `/context`. | Active session required |
| `/compact [focus]` | Run live session compaction when available. | Active session and confirmation required |
| `/resume <session_id>` | Replay bounded retained-session context into an existing or newly created live IDE session. | Workspace Trust and confirmation required; replay is redacted and bounded |
| `/model [model]` | Open the model picker or select a model by name; update the active session when one exists. | Provider selection is also available before starting a task |
| `/model-info [model]` | Show redacted active-session or explicit model/provider metadata. | Active session required; no API keys or secret headers |
| `/subagents [status]` | Inspect delegation settings and available roles. | Active session required; the CLI active-child monitor is a separate surface |
| `/subagents on\|off` | Toggle subagents for subsequent live session turns. | Active session and Workspace Trust required |
| `/stream on\|off` | Toggle active session streaming. | Active session required |
| `/cd <path>` | Set the active session workdir. | Active session required; backend validates path scope |
| `/pwd` | Show active session status, including active workdir. | Active session required |
| `/status` | Alias for active session status. | Active session required |
| `/clear` | Clear live session messages. | Active session, Workspace Trust, and confirmation required |
| `/image [path]` | Add a workspace-scoped image for the next turn, or open a VS Code file picker when no path is supplied. | Backend validates path, MIME, size, and symlinks |
| `/paste-image [path]` | Add a workspace-scoped image through the safe path/file-picker flow. | No JSONL image binaries; backend validates path, MIME, size, and symlinks |
| `/images` | List pending images for the next turn. | Active session required |
| `/clear-images` | Clear pending images. | Active session required |
| `/skills` | List available skills. | Read-only bridge path |
| `/skill <name>` | Show skill metadata. | Read-only bridge path |
| `/tools` | Show the built-in tool catalog. | Read-only bridge path |
| `/hooks` | List hooks. | Read-only bridge path |
| `/mcp` | Show MCP status. | Read-only bridge path |
| `/assets` | List Forge assets for the active plan. | Active Forge plan required |
| `/asset list` | List Forge assets for the active plan. | Active Forge plan required |
| `/asset show <asset_id>` | Show one Forge asset. | Active Forge plan required |
| `/asset add <path>` | Add a workspace-scoped Forge asset. | Requires Workspace Trust and confirmation |
| `/asset delete <asset_id>` | Delete a Forge asset. | Requires Workspace Trust and confirmation |
| `/asset edit <asset_id>` | Edit Forge asset metadata. | Requires Workspace Trust and confirmation |
| `/asset refresh <asset_id>` | Refresh asset comprehension. | Requires Workspace Trust and confirmation |
| `/asset cancel-pending` | Cancel in-process asset comprehension owned by the current bridge instance. | Active Forge plan required |
| `/asset check` | Check plan references against Forge assets. | Active Forge plan required |
| `/asset prune` | Prune verified legacy Forge asset files for the active plan. | Requires Workspace Trust and confirmation |
| `/forge show [plan_id]` | Show structured Forge plan details. | Active plan or prompted plan id; read-only |
| `/forge plan show [plan_id]` | Alias for structured Forge plan details. | Active plan or prompted plan id; read-only |
| `/forge plan state` | Show typed active Forge plan state, goal, assistant, tasks, and validation metadata. | Active Forge plan required; read-only |
| `/forge plan validate` | Validate active Forge plan metadata. | Active Forge plan required; read-only |
| `/forge plan regenerate [focus\|instruction]` | Regenerate the active Forge plan through typed protocol. | Active Forge plan and Workspace Trust required; optional optimistic revision |
| `/assistant [show\|instruction]` | Show or update the active Forge plan assistant instruction. | Show is read-only; updates require Workspace Trust |
| `/goal [show\|goal]` | Show or update the active Forge plan goal. | Show is read-only; updates require Workspace Trust |
| `/task <task_id> show` | Show one active Forge plan task. | Active Forge plan required; read-only |
| `/task <task_id> status <status>` | Update one task status. | Workspace Trust required; existing task id only |
| `/task <task_id> title <title>` | Update one task title. | Workspace Trust required; bounded text only |
| `/task <task_id> body <text>` | Update one task body/objective text. | Workspace Trust required; bounded text only |
| `/forge review <task_id>` | Run structured Forge review for one task. | Requires Workspace Trust and confirmation |
| `/review <task_id>` | Alias for structured Forge task review. | Requires Workspace Trust and confirmation |
| `/profiles` | List configured profiles. | Read-only bridge path |
| `/profile <name>` | Show sanitized profile metadata. | Read-only bridge path |
| `/profile use <name>` | Switch the active profile. | Requires Workspace Trust and confirmation |
| `/report [feedback]` | Create an Alysis Code feedback report. | Requires Workspace Trust and confirmation |
| `/feedback [feedback]` | Alias for `/report [feedback]`. | Requires Workspace Trust and confirmation |

Slash commands never auto-approve actions. They do not bypass Workspace Trust, executable-origin
checks, SecretStorage forwarding guards, Preview blockers, or the cooperative active-cancellation
boundary.

## Autocomplete

Slash autocomplete appears when `/` starts the input. Up/down moves selection, Tab or Right Arrow
accepts the highlighted suggestion, Enter submits complete commands or commands with arguments,
Enter completes partial argument-taking commands such as `/pla`, Escape closes suggestions, and
Shift+Enter preserves newlines.

## Lifecycle visibility

Slash command lifecycle is visible in the Timeline. The sidebar records command start, route,
progress, warning, and error events before the command handler finishes, so `/forge plan <instruction>`
cannot fail only as a toast or Output-panel line. `/forge plan` shows validation, executable-origin,
bridge start/reuse, planning start, and planning completion/failure stages. Unknown commands remain
local and render help instead of reaching `chat.send`.

## Native VS Code chat

VS Code 1.90 includes the stable Chat Participant API used by the optional `@alysis`
integration. The Alysis Code sidebar remains the primary conversation. Native chat is a narrow entry
point for users who prefer native chat participant commands:

| Native command | Behavior |
| --- | --- |
| `@alysis /help` | Opens Alysis Code and explains supported native commands. |
| `@alysis /forge <task>` | Opens Alysis Code and routes to Forge Plan. |
| `@alysis /execute` | Opens Alysis Code and routes to Forge Execute Review, starting with Preview and explicit confirmation. |
| `@alysis /doctor` | Opens Alysis Code and routes to Alysis Code doctor. |

Plain `@alysis` chat opens the sidebar instead of sending text directly to a model. Unknown
native slash commands fail closed with help. The participant obeys the same Workspace Trust,
executable-origin, CLI compatibility, SecretStorage, Preview, and approval boundaries as the
extension controllers, and it never auto-approves actions or parses terminal output.
