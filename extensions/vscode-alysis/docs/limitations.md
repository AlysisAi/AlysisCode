# Known IDE v1 limitations

These limits must stay explicit and consistent in the README, changelog, release notes, and
Marketplace copy. Do not ship copy that implies any of them is solved.

## Summary list

- `fullaccess` execution is unavailable in the IDE.
- Cancellation is cooperative checkpoint only, with no hard interrupt.
- MCP OAuth is unavailable in all remote VS Code extension hosts (SSH, containers, WSL, tunnels).
- Agent-controlled Managed Browser access is public-web only and unavailable to nested subagents.
- Direct IDE local testing is loopback-only.
- `hooks.watch` is unavailable.
- Raw trace output is unavailable.
- Arbitrary terminal start and interactive terminal streaming are unavailable.
- Setup/config terminal menus and inline API key setters are unavailable.

## Detail

- Chat sessions are routed by explicit ownership. Unknown or unowned bridge events are ignored by
  the extension surface instead of being consumed by the wrong controller.
- Active runtime cancellation is only reported as supported when the backend supports it. Current
  IDE Protocol v1 uses cooperative checkpoints, suppresses repeated cancel requests after
  `cancellation_requested`, and never marks a job cancelled without backend terminal-state
  reconciliation.
- Event replay is a bounded in-memory bridge buffer, not durable history.
- Trace and terminal parity is intentionally narrow: `/trace` exposes backend-redacted bounded
  status/events/artifacts with explicit confirmation for `full`, and `/terminals` can list, show,
  kill, and clear existing managed background terminals. Arbitrary shell start, interactive PTY
  streaming, and raw unredacted trace output are not IDE v1 features.
