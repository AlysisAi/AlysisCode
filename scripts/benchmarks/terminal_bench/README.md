# Alysis Code Terminal-Bench Adapter

This adapter runs the plain one-shot Alysis Code agent through `alysis run`.
It does not use Forge or subagents.

## Provider Configuration

The adapter works with any OpenAI-compatible endpoint. `ALYSIS_BASE_URL`
and `ALYSIS_MODEL` are required; there are no built-in provider defaults.
Set the key outside the command so it is not written to shell history.

For example, with OpenRouter:

```bash
export OPENROUTER_API_KEY="..."
export ALYSIS_BASE_URL="https://openrouter.ai/api/v1"
export ALYSIS_MODEL="<provider>/<model>"
```

## Alibaba / DashScope

```bash
export DASHSCOPE_API_KEY="..."
export ALYSIS_BASE_URL="https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
export ALYSIS_MODEL="qwen3-coder-plus"
```

For China-region DashScope use the China compatible-mode base URL instead:

```bash
export ALYSIS_BASE_URL="https://dashscope.aliyuncs.com/compatible-mode/v1"
```

## Smoke Run

From the repository root:

```bash
export TB_AGENT_TIMEOUT_SEC=1800
uvx terminal-bench run \
  --dataset terminal-bench-core==0.1.1 \
  --agent-import-path scripts.benchmarks.terminal_bench.alysis_agent:AlysisSimpleAgent \
  --global-agent-timeout-sec "$TB_AGENT_TIMEOUT_SEC" \
  --agent-kwarg managed_host_agent_timeout_sec="$TB_AGENT_TIMEOUT_SEC" \
  --task-id hello-world \
  --n-concurrent 1
```

## Full Run

```bash
export TB_AGENT_TIMEOUT_SEC=1800
uvx terminal-bench run \
  --dataset terminal-bench-core==0.1.1 \
  --agent-import-path scripts.benchmarks.terminal_bench.alysis_agent:AlysisSimpleAgent \
  --global-agent-timeout-sec "$TB_AGENT_TIMEOUT_SEC" \
  --agent-kwarg managed_host_agent_timeout_sec="$TB_AGENT_TIMEOUT_SEC" \
  --n-concurrent 4
```

Optional overrides:

```bash
export ALYSIS_MAX_STEPS=120
export ALYSIS_TEMPERATURE=0.2
export ALYSIS_INSTALL_SPEC="alysis-code"
export ALYSIS_MANAGED_HOST_SHUTDOWN_RESERVE_SEC=30
```

Use `ALYSIS_INSTALL_SPEC` to pin a release or branch, for example:

```bash
export ALYSIS_INSTALL_SPEC="alysis-code==0.1.5"
export ALYSIS_INSTALL_SPEC="git+https://github.com/AlysisAi/AlysisCode.git@main"
```

## Verification Authority

The adapter does not provide a fake default verifier. When no explicit verifier
is supplied, Alysis Code treats the host verifier as unavailable and falls back to
its normal repo-native verification discovery; it does not export
`ALYSIS_VERIFY_CMD`, write `true` into `verify_commands`, or pass
`--verify-cmd true`. Container setup also clears managed-profile explicit
verifier commands and records a managed-host-unavailable marker so repo-native
discovery can run without inheriting a stale verifier.

To provide a real host verifier, pass a non-empty `verify_cmd` or `verify_cmds`
agent kwarg. A sequence becomes one `--verify-cmd` flag per command; commands
are not joined into a shell string. Vacuous or failure-masking commands such as
`true`, `echo ok`, `python -c 'pass'`, or `pytest -q || true` fail closed before
the task container setup starts. Supplying both `verify_cmd` and `verify_cmds`,
an empty string, an empty sequence member, a set, or another unordered iterable
also fails before source copy, setup, session creation, or model invocation.
Command order is preserved for ordered sequences.

## Managed-Host Deadline Contract

Terminal-Bench owns the outer agent timeout. Alysis Code must receive a smaller
one-shot deadline so it can enter its own finalization window, flush artifacts,
and return before Terminal-Bench kills the task.

The current `terminal-bench` runner computes the final agent timeout inside the
harness:

- if `--global-agent-timeout-sec` is set, that value is the final effective
  timeout;
- otherwise it uses the task `max_agent_timeout_sec` multiplied by
  `--global-timeout-multiplier`.

That computed value is passed to `asyncio.wait_for(agent.perform_task, ...)` but
is not exposed to imported agents through constructor kwargs, `perform_task`,
session metadata, or `TerminalCommand`. For this adapter, use
`--global-agent-timeout-sec` and pass the same already-effective value through
the required `managed_host_agent_timeout_sec` agent kwarg. Do not multiply it
again in the adapter.

Deadline arithmetic:

```text
alysis_deadline_seconds =
  managed_host_agent_timeout_sec
  - monotonic_elapsed_before_alysis_launch
  - managed_host_shutdown_reserve_sec
```

`managed_host_shutdown_reserve_sec` is host-owned reserve for process
collection, tmux/session teardown, result serialization, and artifact flushing.
It is separate from Alysis Code's internal finalization reserve. Precedence is:

1. `--agent-kwarg managed_host_shutdown_reserve_sec=<seconds>`
2. `ALYSIS_MANAGED_HOST_SHUTDOWN_RESERVE_SEC`
3. default `30`

The adapter fails closed when `managed_host_agent_timeout_sec` is absent,
non-finite, non-positive, consumed by reserve, or too small after elapsed time.
On success, the generated command includes `--deadline-seconds`,
`--require-deadline`, and a positional `--` before the complete instruction.

All three launch adapters also set `ALYSIS_MANAGED_HOST_DEADLINE_UNIX_SECONDS`
for that invocation immediately before dispatch, so interpreter and CLI startup
consume the same managed budget. This absolute anchor only narrows the existing
relative deadline; an inherited tighter or expired anchor is preserved, and an
invalid anchor fails clearly. It is passed in the launch environment (a
command-local assignment for the legacy terminal transport), never saved in
configuration. The host and execution environment must have synchronized Unix
clocks. The runtime converts the remaining wall-clock interval to a monotonic
deadline once; subsequent enforcement does not restart the budget. Existing
outer host timeouts and shutdown reserves remain unchanged.
The instruction remains one shell-quoted argument, so dash-leading, quoted,
multiline, Unicode, and shell-looking text stays data.

When Terminal-Bench provides an agent logging directory, the adapter writes
`agent-logs/managed-host-deadline.json`. The artifact contains sanitized
metadata only: schema version, timeout source, final effective host agent
timeout, elapsed pre-launch time, host shutdown reserve, computed Alysis Code
deadline, requirement status, validation status, command timeout, host verifier
status/source/count, and stable verifier command hashes. It does not record raw
verifier commands, API keys, authorization headers, environment dumps, task
instructions, prompts, tool arguments, command output, or source contents.

### Box/Harbor launchers

`box_harbor_agent.AlysisAgent` consumes host-owned metadata at `run` entry,
after installation. Supply `context.metadata["managed_host_deadline"]` with
`remaining_seconds` (or `timeout_seconds`), and optionally
`shutdown_reserve_seconds`. A host may instead provide `deadline_unix_seconds`;
the adapter converts it once to remaining time, then uses monotonic elapsed time.
The host must pass the final effective allowance after its own multipliers.
Constructor `managed_host_agent_timeout_sec` and then
`ALYSIS_MANAGED_HOST_AGENT_TIMEOUT_SEC` are supported explicit fallbacks when
the host cannot provide context metadata. Missing/invalid authority refuses launch.

The box adapter deducts every prelaunch setup call from that same allowance.
It preserves the conservative 85% maximum fraction, the 10,800-second ceiling,
and at least the configured shutdown reserve (default 30 seconds). Legacy
`ALYSIS_RUN_BUDGET_SECONDS`/`ALYSIS_DEADLINE_SECONDS` may only tighten this bound;
they cannot supply missing host authority or enlarge a budget. Extra CLI
arguments cannot override the required deadline. Task names do not supply
deadline authority.

The alternate `harbor_agent.AlysisHarborAgent` likewise requires an explicit
host command timeout rather than silently assuming 7,200 seconds. It derives
the agent deadline using the shared resolver and bounds artifact collection by
the remaining host allowance.

Both Harbor launchers set a unique `ALYSIS_TASK_OUTCOME_PATH` for each root
invocation and expose the resulting terminal record as `alysis_task_outcome`,
independently of the legacy `alysis_outcome`/process status. A missing,
nonterminal, malformed, or contradictory record is unavailable, never verified
success. Exit zero alone does not prove completion. Deadline failures retain
the available artifacts and report the deadline outcome.
