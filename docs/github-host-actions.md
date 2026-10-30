# Selected-account desktop GitHub actions v1

Desktop repository and pull-request APIs now use the installed
[official GitHub MCP plugin](github-mcp-plugin.md). The three native Git remote
actions below remain host-owned. Legacy `github.repo.read` / `github.pr.*` tools
are excluded whenever the desktop policy sentinel is present, including when the
plugin is disabled or disconnected; they also cannot compete in an MCP session.
Their schemas remain documented for non-desktop protocol compatibility.

The desktop host retains GitHub credential and authorization authority. Python
never retrieves a GitHub token for these tools, invokes `gh`, or accepts arbitrary
URLs, repositories, account overrides, Git arguments, or shell commands.

The CLI capability payload advertises `features["github-host-actions-v1"] = true`.
The live IDE `initialize`/`getCapabilities` payload advertises
`features.github_host_actions = {"version": 1, "actions": [...]}`. A host must check
this live handshake before enabling these tools; older pinned engines lack them.
This addition uses the existing host action protocol version `"1"` unchanged.

## Session and operation binding

Create a trusted ordinary chat session with `host_capabilities` containing
`protocol_version: "1"` and the exact subset of actions the host will implement.
The response's `host_actions` provides the negotiated actions, `workspace_fence`,
and `capability_fingerprint`. Sensitive and untrusted sessions receive no actions.
Readonly sessions expose only the repository and pull request read tools.

The host snapshots selected account identity/revision, repository, canonical
workspace, and runtime operation when it creates the session. These remain host
state; model arguments cannot change them. Changing or disconnecting the account,
changing the selected workspace/repository, cancelling the operation, or ending
the job invalidates outstanding authority. The host must revalidate immediately
before dispatch, after asynchronous authentication/approval, and before accepting
results. It must verify the actual repository origin and approved checkout.

`host_action_requested` events carry the session ID in the envelope and
`protocol_version`, `host_action_id`, `action`, `arguments`, `workspace_root`,
`workspace_fence`, `capability_fingerprint`, `expires_at`, `job_id`, and
`max_result_bytes` in the payload. The host checks them against its recorded
session and active job rather than trusting them as target selectors. Respond via
`host.action.respond` with `session_id`, `host_action_id`, `workspace_fence`,
`capability_fingerprint`, and either `ok: true, result: {...}` or
`ok: false, error: {code, message, retryable}`. Never return credentials or raw
credential-helper diagnostics. Results are bounded to 64 KiB; arguments to 8 KiB.
Errors must use bounded non-secret text. The existing channel rejects mismatched
fences, duplicate or expired responses and emits `host_action_cancelled` on
cancellation or timeout. The host cancels owned subprocesses on that event and on
operation/session cancellation; it cannot undo already completed remote writes.

## Fixed action schemas

Every success result includes `repository` (`owner/name`) and `account_login`.
Repository URLs must be the exact GitHub repository or its matching PR URL.
Branch names are bounded to 256 characters and reject options and refspecs.

| Action / tool | Arguments | Remaining success fields |
| --- | --- | --- |
| `github.repo.read` / `github_repo_read` | `{}` | `url`, `private` boolean, `default_branch` |
| `github.pr.list` / `github_pr_list` | optional `base`, `head` | `pull_requests` (at most 30 open PRs), `truncated` boolean |
| `github.pr.create` / `github_pr_create` | `base`, `head`, `title` (256 chars), `body` (4096 chars), optional `draft` (defaults true) | positive `number`, `url` |
| `github.git.fetch` / `github_git_fetch` | `{}` | `output` (at most 16384 chars) |
| `github.git.pull` / `github_git_pull` | `branch` | `output` (at most 16384 chars) |
| `github.git.push` / `github_git_push` | `branch` | `output` (at most 16384 chars) |

Each PR list item contains `number`, `url`, `title`, `base`, `head`, and `draft`.
Extra fields and nonconforming values are rejected. Git actions operate only on
the host-bound origin: fetch without pruning, pull fast-forward only into the
selected branch without discarding changes, push one explicitly named branch to
the same named origin branch without force or deletion.

The host must obtain existing runtime approval bound to the exact operation,
account, repository, action and arguments for PR creation and all three Git
actions (fetch mutates local refs). Python requires a host-managed approval
surface and blocks mutation tools in readonly mode or restricted persona write
scope; it does not show a duplicate approval prompt. GitHub actions do not imply
permission to run arbitrary local commands or bypass shell policy.

## Standalone and Forge behavior

Standalone CLI remote sync retains its existing `gh`/`glab` and Git behavior.
When `ALYSIS_DESKTOP_GITHUB_POLICY` is present, remote_sync refuses push and PR/MR
lookup/creation without executing commands, regardless of the policy's value or
local PATH, CLI path, credentials or login environment. This also blocks the
current Forge remote-sync path because it has no negotiated host-action broker.
Ordinary desktop chat uses the plugin's discovered MCP tools and native Git remote actions. Existing local file, Git
status/diff/history and policy-controlled shell tools remain separate capabilities.
This contract does not claim that arbitrary shell code is sandboxed from all
external credential sources.

## Account questions without a project

A desktop host can advertise `desktop_connections.version = 1` and negotiate
`connectors.status` (`connection_status`) and
`connectors.github.repositories` (`github_repositories`).
These read-only tools use the normal agent loop. They do not require a Git
repository; the internal chat directory only fences the host protocol.

Status reports the selected account, plugin availability, observation time,
connection evidence and next action. GitHub authenticated identity is separate
from repository permission; Google's saved grant is not live API verification.
Unknown state remains unknown. Missing or disabled plugins cannot list repositories.
Repository discovery is bounded to 100 entries per page and the selected account;
arguments cannot select an account, repository scope or arbitrary endpoint.

These tools expose no credentials, do not connect accounts or install plugins,
and cannot clone, commit, push or create PRs. Existing local Git and scoped MCP
operations retain their separate requirements. The host must revalidate the
operation, session, selected project and account/plugin revision around each read.
Cancelled or expired operations must discard late responses.
