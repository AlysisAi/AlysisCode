# Security Model

Alysis Code is a local coding agent. The host process owns policy decisions, tool registration,
workspace binding, and session logging. Model-authored text can request tools, but host-owned
runtime checks decide which tools exist and what inputs are accepted.

## Trust Boundaries

Treat these inputs as untrusted:

- repository files and project-local configuration
- MCP server output
- web responses
- custom tool output
- image/OCR/asset text
- model responses

Alysis Code keeps those inputs behind host-owned wrappers where possible. These wrappers reduce prompt
confusion, but they are not a substitute for reviewing changes before running them.

## Execution Modes

- `readonly` exposes inspection tools plus top-level read-only delegation controls; it never
  exposes writes, candidate apply/discard, or nested delegation.
- `review` ("Ask for approval") asks before writes and shell commands. Commands that only
  look (`ls`, `git status`, `rg`, `Get-ChildItem`, …; see `read_only_commands.py`) run
  without asking.
- `auto` ("Approve for me") runs normal work without asking: reads, edits inside the
  workspace, read-only commands and sandboxed commands. The approvals that remain (a command
  without a sandbox, `git push`, package installs, file deletion, discarding work, publishing
  or deploying, credential files; see `command_review_rules.py`) are answered by a reviewer
  model instead of a person (`agent/approval_review.py`). The reviewer sees the user's
  messages and the exact action, not the agent's own messages or tool output. If it declines,
  the agent gets the reason as a tool error and can try another route. If it cannot decide,
  or after 3 declines in a row or 20 in one turn, the person is asked. Approvals that always
  need a person (credential files, folders outside the project, IDE tasks, browser actions,
  Computer Use) never go to the reviewer, and the user's permission rules and session grants
  are applied first. The reviewer is the chat's model unless `role_models.approval_review`
  (or `ALYSIS_MODEL_APPROVAL_REVIEW`) names another.
- `fullaccess` removes Alysis action approval prompts, including mandatory-person prompts,
  and the shell sandbox. The shared authority in `agent/action_approval.py` still honors
  explicit deny rules. Tool admission, provider and operating-system constraints remain
  separate. Do not rely on a final confirmation for critical deletes in this mode.

The execution mode is the only approval authority: if the footer badge says `safe`, writes
and shell commands stop and ask a person, and only `fast` lets the reviewer answer. In the TUI, Shift+Tab cycles
the next-message selection (`read → safe → fast → full`) and `/permissions`
selects one directly. The footer shows active → selected Permissions until the next
real user message activates the choice. Selecting `fullaccess` prints a warning immediately;
it does not widen a turn already in progress.

`--yes` is not a blanket action approval: file deletion, sensitive access and connected
tools still go through the shared authority and the active mode's policy. It does not
relax `review`, override mandatory-person requests in `auto`, or grant Workspace Trust.

`fullaccess` is for trusted workspaces only. It is not a sandbox boundary and should not be used for
untrusted repositories, unknown prompts, or commands you would not run directly.

## Shell And Verification Sandboxing

Shell and verification commands can run through Docker, Linux Bubblewrap, or macOS Seatbelt.
Production-style usage should keep sandbox mode strict, network disabled unless needed, and Docker
images pinned by digest. The Seatbelt profile restricts writes and outbound network access while
allowing host file reads and localhost connections; see the backend differences in the sandbox guide.

The sandbox reduces the blast radius of local command execution. It does not make arbitrary code
safe, and it does not replace source review, dependency review, or operating-system permissions.

Verification and acceptance evidence also has a provenance boundary. Host
verification commands, explicit user checks, pre-existing repo-native tests,
pre-existing task checkers, direct black-box commands, self-authored tests, and
ad hoc observations are recorded separately. Tests or checkers created during
the same turn can help diagnose behavior, but they are supplemental when a
required acceptance criterion still lacks independent evidence or has direct
contrary evidence.

Verification commands carry a separate typed command contract. Exact shell
expressions with pipelines, redirections, or chaining are trusted only when they
come from host configuration, an explicit user command, or a pre-existing
repo/task checker; model-inferred shell chaining remains invalid even if it
looks like a test. Interpreter snippets are accepted only after language and
syntax preflight and are executed through an interpreter invocation rather than
raw `/bin/sh`. Generic pytest fallback is not security authority by itself: it
requires a trustworthy pre-existing Python test surface, and tests created
during the current turn cannot retroactively establish that surface.

Acceptance paths keep a trust boundary too. A path mentioned by a prompt is
classified with a role before it can affect finalization: required output,
existing input, preservation target, verification checker, or advisory
reference. Workspace-internal absolute paths are normalized to workspace
relative form; external absolute paths and unresolved parent traversal are not
silently joined under the workspace or probed to satisfy a heuristic. Only hard
criteria can block completion. Advisory parser inferences remain visible in
payloads but do not become security-sensitive finalization requirements.

Acceptance preservation checks compare explicit unchanged/only-touch
constraints against material touched paths after filtering runtime artifacts.
Threshold checks remain failed when measured output misses the requested
target; performance-style thresholds require repeated samples before they are
treated as conclusive.

Persistent service evidence has an additional ownership boundary. A
`shell_background` process is owned by the session and is terminated by
`AgentSession.close`, so it is not durable evidence. A `shell_service_start`
process is explicitly durable, stores only sanitized identity/readiness/log
metadata in the runtime sessions directory, detaches stdin/stdout/stderr from
the agent process, and uses PID/container identity checks before `shell_service_stop`
signals anything.

See [Shell sandbox](shell_sandbox.md) for setup, image pinning, signatures, provenance, and troubleshooting.

## HTTP And SSRF Protection

Alysis Code validates outbound HTTP targets used by web fetch/search helpers and MCP OAuth metadata
fetching before connecting.

The safe HTTP guard rejects:

- unsupported schemes such as `file:`, `data:`, `ftp:`, and `javascript:`
- embedded URL credentials
- loopback, link-local, private, unspecified, and multicast IP ranges
- hostnames that resolve to denied IP ranges
- redirect targets that fail the same validation
- responses larger than the configured byte cap

Where supported, requests connect to a validated resolved address while preserving the original
`Host` header, and redirects are revalidated at each hop.

`web_fetch` has a separate provenance gate before the safe HTTP request is
started. A fetchable URL must come from the user, from a configured
`web_search` result, from an observed canonical redirect, from a bounded
same-origin/search-mediated recovery path, or from the configured
trusted-domain allowlist (`web_fetch_trusted_domains`, defaulting to major
public package registries whose API endpoints search engines do not index).
The allowlist widens only the provenance gate for the listed hosts and their
subdomains; the safe HTTP guard's scheme, credential, DNS/IP, redirect, and
byte-cap validation still applies to every fetch, and an empty list restores
strict provenance-only behavior. URLs discovered inside trusted
fetched pages, trusted local file reads, and registered tool or shell output are
kept as bounded source-linked provenance records; source content is not persisted
in the provenance graph. Canonicalization may ignore fragments, default ports,
and harmless trailing slashes, but it does not relax scheme, credential, DNS/IP,
redirect, or byte-cap validation. Recovery search is not attempted during
deadline finalization.

Unknown-tool recovery is also deliberately narrow. The runtime reports available
tool names and nearest suggestions, but does not expose hidden schema details,
environment data, credentials, or plugin implementation metadata. Only
hard-coded schema-compatible compatibility aliases can execute automatically;
ambiguous names produce correction guidance instead.

## MCP Boundaries

Project-local MCP configuration can only narrow or disable exposure relative to user configuration.
Tool, resource, and prompt catalogs are snapshotted per session rather than mutated live in the
model-visible tool surface.

Server-authored MCP tool descriptions are intentionally omitted from model-visible descriptions.
MCP resource text is wrapped as untrusted content and size-limited before prompt inclusion. Task
execution can further restrict MCP access through task-level scope rules.

A successful connector call establishes the returned API fact, not that a
published deliverable satisfies the request. Links need an authorized content
check; an HTTP 200 response alone can still be the wrong page. A newly observed
file establishes existence, not correct contents or a successful transformation.
Unavailable checks leave the outcome unverified.

Invalid provider tool batches are rejected before dispatch. Recovery retains the
same permissions and deadlines and cannot replay writes with uncertain effects.

## Skills, Plugins, Hooks, And Custom Tools

Skills are instruction bundles. They are advertised briefly and read on demand; their text is still
untrusted repo or user content.

Plugins and custom tools require explicit install/trust flows. Project custom tools are trusted by
workspace path and file hash, so edits invalidate trust. Custom tool execution runs in a worker
process with declared capability checks, but it is still trusted code and should be reviewed before
use.

Lifecycle hooks are deterministic policy and automation. Project hook configuration is not trusted
by default and must be explicitly trusted. Hook output can modify prompts or tool inputs, so review
hook configs before trusting them.

## Reporting Security Issues

Report vulnerabilities privately by following the root [Security Policy](../.github/SECURITY.md). Do not
open public GitHub issues for security bugs.
