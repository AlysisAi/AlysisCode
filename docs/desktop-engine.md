# Desktop engine contracts

The Alysis Code desktop app and CLI share the same engine. At startup, the app
checks the engine's identity and negotiates the capabilities it needs.

## Adding a capability

Declare new desktop behavior in `DESKTOP_FEATURES` in
`src/alysis_code/capabilities.py`, and back it with implementation and regression
tests. A breaking transport change needs an explicit protocol/version agreement
with the app. Do not reuse an existing feature name for incompatible behavior.

The desktop validates `alysis capabilities`. The engine must report the expected
contract version and required features, version 2 of the Forge host and execution
contracts, and runtime status v1. The IDE bridge negotiates the sensitive-host and Computer Use handshakes and implements
`computer.respond`, `browser_start.respond` and `task_budget.respond`.

Windows advertises `ide.private-utf8-stdio-v1`. Its JSONL protocol uses private,
non-inheritable descriptors. Child processes receive null-device standard input
and output, and protocol text uses strict UTF-8 regardless of the system code page.

## Engine identity and compatibility

The package is `alysis-code`, its Python import is `alysis_code`, and its canonical
commands are `alysis` and `alysis-ide-bridge`. Workflow metadata uses
`alysis_workflow`, and settings use `ALYSIS_*` names.
See [migration guidance](migration-alysis-code.md) for compatibility behavior.

Before bundling an engine update, check the app's capability validator, protocol
clients, context markers and storage paths. Preserve existing user state during
any migration. The desktop bundle ID, installation path and user-data root are
owned by the app and should not change as a side effect of an engine update.

## Host-selected verification

The Forge host protocol advertises `forge.host-authoritative-verification-v1`.
Commands selected explicitly by the host use `HOST_AUTHORITATIVE` provenance.
[`alysis-code.schema.json`](../src/alysis_code/forge_host_protocol/alysis-code.schema.json)
and the Python validator accept that value alongside `PREEXISTING_REPO_NATIVE`.

Both profiles require `pytest` or `pytest -q`, ARGV execution, trusted/required
status, assertive analysis, an independently bound external pytest executable and
an approved command identity. Repository scan suggestions are advisory and cannot
create an executable host contract. A consumer using an older conditional schema
must update it before accepting the host-authoritative profile.

## Contract checks

The `desktop-contracts` workflow tests Python 3.12 on native macOS and Windows with
frozen dependencies and no provider credentials. It checks the capabilities CLI,
bridge, stdio and host contracts.

Run these checks locally with:

```sh
uv sync --frozen --no-editable --python 3.12 --extra dev
uv run --frozen --no-sync alysis capabilities
uv run --frozen --no-sync pytest -q tests/test_capabilities_cli.py tests/test_desktop_run_contract.py tests/test_ide_protocol_contract.py tests/test_ide_stdio_bridge.py tests/test_ide_protocol_stream_isolation.py tests/test_forge_host_stdio.py tests/test_forge_host_protocol_v2.py tests/test_ide_computer_use_host.py tests/test_task_budget_channel.py
```
