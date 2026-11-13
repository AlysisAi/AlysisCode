# Bots development guide

Bots (Owls in the desktop app) is shared engine code. App integration uses its own
reviewed engine pin; an engine merge does not update an installed app or publish a
release. See [desktop engine contracts](desktop-engine.md).

## Product and execution boundaries

- Each bot has one continuous conversation. Bots are peers; the product does not
  require a privileged main assistant or a separate user-facing coding agent.
- Longer work runs in the background while the conversation remains responsive.
  Coding uses the engine through a scoped internal worker and retains task
  ownership, accounting, cancellation, and result verification.
- Peer requests stay within the owner's authorization. A message from another bot
  does not grant credentials or permission for an external action.
- Agent computer access and human viewer control have separate permissions.
  Pause, takeover, resume, and Stop must preserve those boundaries.
- Conversations, schedules, approved memories, files, and execution receipts
  belong in durable storage. Disposable computers must not become their only copy.
- Connected apps and credentials retain their existing authorization boundaries;
  do not inherit operator credentials into workers or disposable computers.

Historical test counts, deployment addresses and time-limited pilot approvals in older
records describe those runs only.

## Source and local development

| Area | Entry points |
| --- | --- |
| API and service | [app.py](../src/alysis_code/bots/app.py), [local_service.py](../src/alysis_code/bots/local_service.py), [serving.py](../src/alysis_code/bots/serving.py) |
| Conversation and execution | [runtime.py](../src/alysis_code/bots/runtime.py), [execution.py](../src/alysis_code/bots/execution.py), [background.py](../src/alysis_code/bots/background.py) |
| Coding integration | [coding_api.py](../src/alysis_code/bots/coding_api.py), [coding_worker.py](../src/alysis_code/bots/coding_worker.py) |
| Durable state | [store.py](../src/alysis_code/bots/store.py), [postgres.py](../src/alysis_code/bots/postgres.py), [object_storage.py](../src/alysis_code/bots/object_storage.py) |
| Computers and control | [local_computers.py](../src/alysis_code/bots/local_computers.py), [cloud_desktop.py](../src/alysis_code/bots/cloud_desktop.py), [computer_control.py](../src/alysis_code/bots/computer_control.py) |

Use the [development scripts guide](../scripts/bots-dev/README.md) for local
setup, deterministic checks, and opt-in live studies. Its saved-fixture commands
require the named fixtures; missing data is not permission to make provider calls.
Local Docker development and private browser profiles are not evidence of hostile
tenant isolation. Qualify the actual backend and its trust boundary.

## Feature contracts

- Action review and approval rules: external effects and scoped authority.
- Parallel execution: task ownership, resource limits, and concurrent work.
- Portable storage: durable data and storage-provider boundaries.
- App and cloud integration: app/service connection and cloud qualification.
- Local computer integration: lifecycle, private VM boundaries, and native-app checks.

### Memory and Library

Approved memories remain per bot until explicitly corrected or deleted. Working
note consolidation retains its originals. Bounded retrieval limits what reaches
the model; it must not silently evict approved memories to make room.

The old 40-memory/40-note prototype caps were removed. Library files have a shared
account allowance, including reservations; memories and working notes are separate.
Recheck deployed configuration before describing a historical quota as live policy.

## Operations and qualification

Reconcile actual deployed source, resources, and configuration before acting;
an old pilot's schedule or authorization is not an active operating instruction.

For each change, record the commit, backend, test command, result, and skipped or
unverified cases in the PR. Distinguish deterministic tests, real model
calls, hosted checks, and installed-app qualification. Keep failures and unresolved
security or release gates visible. A successful source test does not establish
native Windows behavior, production availability, or installed-app readiness.

Before an app pin or release, validate the actual candidate and its capability
contract, durable-state recovery, approvals, and backend-specific behavior. Keep
temporary task diaries and handoffs out of main; update these guides and preserve
only the evidence needed to assess the change.
