# Security model

This document covers the extension-side trust boundaries. The repository security policy and
vulnerability reporting process live in
[`SECURITY.md`](https://github.com/AlysisAi/AlysisCode/blob/main/SECURITY.md).

## Workspace Trust

Untrusted workspaces allow bridge health, provider setup, doctor checks, readonly placeholders, and
readonly Forge Execute Preview when executable-origin guards pass. Forge Plan requires Workspace
Trust in this build because the current backend records Forge artifacts under the workspace.
`review`/`auto` Forge Execute Preview modes, real Forge execution, and future write-capable or
shell-capable actions require Workspace Trust.

If the workspace is untrusted and `alysis.defaultMode` is `review`, `auto`, or any future
write-capable mode, chat submission is blocked before the bridge process starts, before
SecretStorage is read, and before any API key can be forwarded. Switch the mode to `readonly` or
grant Workspace Trust.

## Restricted Mode settings

Workspace settings that can affect execution are restricted in VS Code metadata and checked again in
extension code. In Restricted Mode, workspace-scoped values for `alysis.cliPath`,
`alysis.defaultMode`, `alysis.defaultModel`, `alysis.baseUrl`, `alysis.provider`,
`alysis.transport`, `alysis.sandboxProfile`, `alysis.forgeExecuteMaxSteps`,
`alysis.forgeExecuteNoLog`, `alysis.autoStartBridge`, and `alysis.enableForge` are
ignored.

`alysis.cliPath` uses VS Code's `machine` scope: the executable path does not roam between
operating systems through Settings Sync, and a workspace cannot override which executable the
extension launches.

## Executable origin

The extension blocks process execution when an untrusted workspace tries to define the Alysis Code CLI
path, when a configured CLI path is workspace-relative, when an absolute CLI path resolves inside
the open workspace, or when default PATH lookup resolves `alysis` inside the open workspace.
Workspace-local CLI paths and write-capable flows require Workspace Trust.

## Credentials

- Provider API keys are stored through VS Code SecretStorage
  (`src/secrets/secretStorage.ts`), one secret per profile plus a small index recording which
  environment variable each profile's key must be exported as. The legacy single-key secret is still
  honored so existing installs keep working.
- Keys reach the runtime only through the child process environment. The bridge is spawned as
  `alysis ide-bridge --stdio`; no credential is ever placed on argv, in a setting, or in a log
  line.
- SecretStorage API keys are never forwarded to a child process when the resolved Alysis Code CLI path
  is not trusted (`canForwardApiKey` in `src/client/CliDiscovery.ts`). The bridge also strips
  inherited `ALYSIS_API_KEY` from the child environment in that case.
- Non-credential child processes — version, health, executable validation, and sandbox doctor probes
  — are launched through `cliCommandEnvironment`, which deletes every credential-bearing variable
  from the inherited environment first.
- The extension tracks the running bridge launch profile. Safe readonly/no-secret operations such as
  Forge plan list/open and Forge Execute Preview start with `stripApiKey` when possible. Chat, Run
  Task, and Forge Plan require a credential-capable launch profile; an idle no-secret bridge is
  restarted with credentials, while pending requests, active jobs, or pending approvals block the
  restart with a clear error instead of silently reusing the weaker process.
- Command, error, and result surfaces redact secret-like values before display, and config/profile
  payloads report secret status only — never raw secret values.

## Managed browser

Managed Browser start, navigation, click, and type require Workspace Trust. Normal
`network_scope: "public"` sessions are shared by the agent and the native Browser surface.
**Start local testing** is a distinct direct-IDE action with a modal confirmation; it sends
`network_scope: "public_loopback"` and `confirm: true`, never the legacy `allow_local_destinations`
flag. Its session is hidden from agent tools and permits loopback plus public destinations while
denying LAN and link-local. Snapshot, screenshot, diagnostics, and exact owner-scoped close remain
host controlled; close is deliberately available after trust is revoked so an owned browser cannot
be stranded. Nested subagents receive no browser tools, and neither path can select a Chromium
executable or profile path.

## Webview

The sidebar webview uses a crypto-backed nonce, a strict CSP, packaged local assets only, and
text-only rendering for all model, tool, and workspace output. Webview messages are validated by
method payload; there is no unsafe HTML insertion path.

## MCP OAuth

MCP OAuth on local extension hosts uses flow-id-bound start/status/cancel/logout, state + S256 PKCE,
an owned loopback callback, bounded polling, encrypted token persistence, and shutdown fencing
without returning authorization codes or raw tokens through JSONL. It is unavailable in every remote
VS Code extension host — SSH, containers, WSL, and tunnels — because the loopback callback would run
remotely while the authorization browser opens locally (`src/security/remoteHost.ts`). MCP login is
shown only when the bridge advertises the local-host OAuth lifecycle and stays hidden or explicitly
unavailable on unsupported remote hosts.
