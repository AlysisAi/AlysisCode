# QA scripts

## Production reliability release gate

## Repository checks

Check that imports from the runtime package have matching dependency declarations:

```bash
uv run python scripts/qa/check_deps.py
```

Check that the public tree excludes internal documentation, caches, local runtime output,
credential-bearing file types, and oversized artifacts. The check also validates relative Markdown
links, including exact filename and directory capitalization, so links work on Linux as well as
case-insensitive filesystems:

```bash
uv run python scripts/qa/check_public_repo.py
```

## Runtime harness

`raw_agent_proxy.py` runs deterministic one-shot scenarios against the local mock provider. It
exercises completion, verification, and diff-review behavior without contacting a hosted model.

```bash
PYTHONPATH=src:. uv run python -m scripts.qa.raw_agent_proxy --list
PYTHONPATH=src:. uv run python -m scripts.qa.raw_agent_proxy \
  --output qa_reports/raw_agent_proxy
```

Reports, transcripts, temporary repositories, and session files are written beneath the selected
output directory. `qa_reports/` is ignored by Git.

`mock_llm.py` supports this harness and is not intended as a standalone command.

Session logs may contain project information. Do not commit them or attach them to public issues
without reviewing and sanitizing their contents.

## Production reliability qualification

The release qualification script runs focused source reliability and safety tests,
builds a wheel, and checks the installed package with local fault scenarios. Run
it from a clean, locked checkout when preparing a release:

```bash
uv sync --frozen --extra dev
uv run --frozen --no-sync python scripts/qa/qualify_production_reliability.py --require-clean
```

It writes logs, JUnit results, a wheel, and an evidence manifest to a new output
directory. Use `--output-dir` to select that location. This focused gate complements
the full test suite and distribution validation in the [release process](../../docs/RELEASING.md).

Each unique output directory retains stage logs, JUnit results, the wheel, and a
manifest with Python/platform/dependency versions, Git commit, dirty status,
source inventory hashes, lockfile hash, wheel hash, and command outcomes. Source
changes during the run fail qualification. `--output-dir` chooses a new directory;
existing evidence is never overwritten. Omitting `--require-clean` permits a
clearly labeled development run; it is not a clean release qualification.

CI makes this gate blocking on Python 3.12 across Linux, Windows, and macOS, in
addition to the complete Linux Python 3.12 suite and distribution checks.
The manually dispatched `ci` workflow runs that full suite once by default.
Enable **full_python_matrix** for compatibility checks on Python 3.11, 3.12,
and 3.14, especially when changing dependencies, interpreter-sensitive code,
or preparing a release. Both modes retain all platform qualification, native
macOS sandbox, extension, and package gates.
Routine CI runs every MCP transport scenario once. The full Python matrix and
release workflow retain the repeated transport race and stderr stress checks.
Local pytest runs also keep those repetitions by default; set
`ALYSIS_TEST_MCP_STRESS=0` to match routine CI's single pass per scenario.
The extension job runs unit tests and the complete Extension Host suite against
the packaged VSIX on Linux, Windows, and macOS. The source Extension Host command
remains available locally; CI avoids repeating the same suite before packaging.
Platform-specific skips remain explicit in JUnit; unavailable Docker/desktop or
provider environments are not certified by a passing local fixture. The fault
tests enforce their declared cancellation thresholds and retain artifacts while
checking owned child cleanup. They do not run paid providers or establish model
quality, benchmark gains, or universal reliability.

For subsequent evaluations, compare exact old/new wheels in paired randomized or
interleaved runs with identical provider routes and declared resource limits.
Keep passing controls and failures in the raw results; report verified completion,
honest partial outcomes, provider/host failures, latency and cost separately.
Record infrastructure corrections as secondary analyses. Never feed hidden
answers, verifier internals, or task-name strategies into the production runtime.

This harness runs user-facing Alysis Code UX smoke scenarios as real CLI subprocesses, captures transcripts, applies explicit quality rules, and writes a Markdown report under `qa_reports/`.

## Run locally

```bash
python -m scripts.qa list
python -m scripts.qa run
python -m scripts.qa run --scenarios C,D,G
python -m scripts.qa run --output qa_reports/manual
```

By default the harness runs the local source CLI with `python -m alysis_code.cli`. To force an installed binary:

```bash
python -m scripts.qa run --cli alysis
```

The harness starts a local stdlib OpenAI-compatible mock server. It never calls real LLM providers.

## Frozen subagent quality cases

Prepare a new disposable Git fixture and its frozen acceptance checks:

```bash
python scripts/qa/subagent_quality_cases.py prepare /tmp/subagent-natural-case
python scripts/qa/subagent_quality_cases.py prepare /tmp/subagent-followup-case --scenario followup
```

The destination must not already exist. Each case includes `workspace/`, `prompt.txt`,
`chat-input.txt`, `acceptance.py`, and a manifest with prompt and acceptance hashes. Run the
implementation separately against `workspace/`, using the supplied prompt, then check the result:

```bash
python scripts/qa/subagent_quality_cases.py check /tmp/subagent-natural-case
python scripts/qa/subagent_quality_cases.py check /tmp/subagent-followup-case
```

This script uses only Python's standard library and Git; it does not launch Alysis or call a model
provider. The initial fixture intentionally fails acceptance until implemented. The natural case
does not require delegation. The follow-up case requires a successful isolated child, an explicit
continuation in the same retained candidate, and final application to the parent. Acceptance checks
test the resulting behavior and preserved dirty setting; review the recorded delegation trace
separately for the follow-up mechanism. Do not alter the frozen acceptance file after an attempt.

## Reports

Each run writes:

- `qa_reports/<timestamp>/report.md`
- `qa_reports/<timestamp>/transcripts/*.log` for `NO_COLOR=1` rule checks
- `qa_reports/<timestamp>/transcripts_color/*.log` for human visual review
- `qa_reports/<timestamp>/sessions/*.jsonl` when Alysis Code produced session logs
- `qa_reports/<timestamp>/workdirs/` preserved per scenario for inspection

The committed baseline lives at `qa_reports/baseline/report.md`.

## Add a scenario

Add one `Scenario` entry in the relevant file under `scripts/qa/scenarios/`. A scenario provides:

- `name`
- `family`
- `description`
- `setup(work_dir)` to create a synthetic repo
- `drive(harness, work_dir)` returning a `RunSpec`

If a scenario cannot be automated yet, add it with `skip_reason`. Skips are explicit in the report.

## Add a quality rule

Add one `QualityRule` entry in `scripts/qa/quality_rules.py`. Rules return excerpts for violations and include severity: `blocker`, `high`, `medium`, or `low`.

## Pytest

Harness sanity tests run with default pytest:

```bash
pytest tests/qa/test_harness.py
```

The full battery is marked `qa` and is skipped by default unless explicitly selected:

```bash
pytest -m qa tests/qa/test_qa_battery.py
```

## Raw Agent proxy harness

The raw-agent proxy harness exercises the normal `alysis run --benchmark`
path as real CLI subprocesses against a local OpenAI-compatible mock provider.
It is meant for benchmark-readiness regressions around material work,
verification repair, completion-gate nudges, focused test discovery, and
required diff review without contacting live providers.

```bash
PYTHONPATH=src:. python -m scripts.qa.raw_agent_proxy --list
PYTHONPATH=src:. python -m scripts.qa.raw_agent_proxy \
  --output qa_reports/raw_agent_proxy
PYTHONPATH=src:. python -m scripts.qa.raw_agent_proxy \
  --scenarios pytest_contract_smoke,completion_gate_forced_diff \
  --scenario-timeout-s 120 \
  --output qa_reports/raw_agent_proxy_focus
```

By default the harness starts the local mock provider, writes an isolated config
and data directory for each scenario, and runs the source CLI with
`python -m alysis_code.cli run --benchmark`. Use
`--cli-command alysis` only when intentionally testing an installed binary. Use
`--no-mock-provider` only for explicit live/provider shakedowns; that path uses
the caller's environment and is not part of the deterministic local gate.

Each run writes:

- `raw_agent_proxy_report.md`
- `raw_agent_proxy_metrics.json`
- `raw_agent_proxy_runs.json`
- `transcripts/*.log`
- `workdirs/<scenario>/` with the disposable repo, config, data, and session
  logs

## VS Code extension dogfood

The VS Code extension release gate has a separate deterministic dogfood harness:

```bash
python scripts/qa/vscode_extension_dogfood.py --mode mock --extension-host skip
python scripts/qa/vscode_extension_dogfood.py --mode mock --extension-host run
VSCODE_TEST_EXECUTABLE_PATH=/absolute/path/to/code \
  python scripts/qa/vscode_extension_dogfood.py --mode mock --extension-host require-cached
python scripts/qa/vscode_extension_dogfood.py --mode manual-real-provider \
  --vsix-path /absolute/path/to/vscode-alysis-<target>.vsix --package-mode require
```

Mock mode copies `tests/fixtures/vscode_dogfood/simple-python-project` into a disposable git repo,
verifies the fixture, checks `alysis ide-bridge health` with the active Python interpreter plus
`PYTHONPATH=src`, packages or verifies a VSIX, and writes a report under
`qa_reports/vscode_dogfood/`. It does not require real model credentials and does not default to
`uv run`; pass `--cli-command` only when intentionally testing an installed binary.

`--extension-host skip` is a deterministic local smoke path. `--extension-host run` or
`--extension-host require-cached` provides fuller generic component validation. Use
`--vscode-executable /absolute/path/to/code` or `VSCODE_TEST_EXECUTABLE_PATH=/absolute/path/to/code`
to avoid downloading VS Code. When no executable is provided, `run` preflights
`update.code.visualstudio.com` and reports a clean failure if download is unavailable instead of
leaving only a raw DNS stack trace. Use `--package-mode require` after `npm run package` in CI.
Every generic report records `component_valid: true/false`, `release_valid: false`,
`extension_host: passed/skipped/failed`, and an `extension_host_preflight` object. A generic VSIX
does not contain a signed managed runtime and cannot become release-valid. Production validation is
performed by `.github/workflows/managed-cli-vsix-release.yml` against each target-specific VSIX.

Manual real-provider mode prepares the same disposable repo and writes an exact checklist plus a
draft JSON template. Completed beta evidence must be a separate schema-v5
`completed-manual-report` JSON file, bound to the exact target VSIX and managed-runtime hashes, and
validated by
`python scripts/qa/validate_vscode_manual_provider_report.py <report.json> <target.vsix> <production-dogfood.json>`.
The validator opens the exact candidate and production summary and
verifies their SHA-256 values, immutable release/candidate/host identity, target runtime hash,
extension and artifact versions, pre-release marker, repository-pinned public key, signed
managed-runtime record, and structured timestamped check receipts. The
required checks cover provider setup through SecretStorage, normal chat, Run Task, `/doctor`,
`/model-info`, `/subagent`, images, trace, managed terminals, Forge plan/show/status/edit/regenerate,
Execute Preview, review-mode Execute, diffs/artifacts, Manage tree, missing-provider recovery,
broken-CLI recovery, and secret redaction.

## IDE/CLI parity

The VS Code backend parity contract is validated separately from dogfood:

```bash
python scripts/qa/check_ide_cli_parity.py
python scripts/qa/check_ide_cli_parity.py --list-extracted
```

The contract lives in `docs/generated/ide_cli_parity_matrix.json`, with a readable summary in
`docs/ide_cli_parity_matrix.md`. The validator statically extracts Python CLI command surfaces,
CLI chat slash commands, Forge chat commands, IDE bridge methods, VS Code package commands, and the
extension slash registry. It fails when a new feature surface is missing from the JSON matrix, when
an IDE method is missing from `docs/ide_protocol.md`, or when an extension slash command is missing
from `docs/vscode_extension.md`. Package commands also need `extension_command_routes` metadata so
command-palette commands, hidden context-menu commands, Sessions/Manage tree commands, backend
action groups, backend actions, and documented handlers cannot drift silently. Management backend
methods are part of this contract; mutating methods must stay capability-flagged, Workspace Trust
gated, and free of raw secret params/results.

Every advertised IDE bridge method needs typed coverage in the VS Code client
(`AlysisProtocol.ts` and `AlysisBridgeClient.ts`) unless its matrix entry says the method is for
the desktop host alone: `"desktop_host_only": true` on an `implemented_in_bridge_only` entry, with
its `bridge_only_rationale`. The matrix is the only place that says so. The validator also requires
that declaration for every desktop host response in `computer_use_platform_policy.py`
(`CU_METHODS`), and refuses it for a method the VS Code client requests.

## Current limitations

The live subagent comparison runner is separate from the mock batteries above:

```bash
.venv/bin/python -m scripts.qa.subagent_live_eval list
.venv/bin/python -m scripts.qa.subagent_live_eval describe dev-request-log
.venv/bin/python -m scripts.qa.subagent_live_eval freeze-source /path/to/baseline /tmp/eval/baseline
.venv/bin/python -m scripts.qa.subagent_live_eval freeze-source /path/to/candidate /tmp/eval/candidate
.venv/bin/python -m scripts.qa.subagent_live_eval screen-manifest --baseline /tmp/eval/baseline --candidate /tmp/eval/candidate --output /tmp/eval/results --manifest /tmp/eval/development-plan.json
.venv/bin/python -m scripts.qa.subagent_live_eval plan /tmp/eval/development-plan.json
# Explicitly dispatch one reviewed cell; no automatic next cell or retry:
.venv/bin/python -m scripts.qa.subagent_live_eval run /tmp/eval/development-plan.json --attempt dev-request-log-gpt-5.6-luna-current-v6-r1 --execute-live
.venv/bin/python -m scripts.qa.subagent_live_eval summarize /tmp/eval/results
```

Planning, fixture preparation, source freezing, and `tests/test_subagent_live_eval.py`
do not call a model. Live dispatch uses the real CLI with invocation-local Luna/Terra
high settings on the existing ChatGPT subscription, rejects conflicting effective
child models, and never saves the user's configuration or credentials. Source and
harness hashes are pinned in the plan; task briefs, fixture/oracle hashes, runtime
source, wrapper, full attempt logs and sanitized evidence summaries remain reviewable.
Classic chat keeps one real process/session and sends the next user message only
after observing the previous `run_turn` return or exception; it does not preload
future user messages or replace model output.
The runner preserves the CLI's native streaming default. After each boundary's
external acceptance check, it rechecks the deadline and observed token allowance
before sending a follow-up. Admission reserves the oracle timeout and the existing
shutdown grace periods as well as the attempt's working time.

The initial matrix has 12 cells including two direct controls. Equivalent Terra
balanced/shared and configured-profile cells are omitted explicitly. Limits are
48 parent steps and 12 minutes per attempt, with a three-hour campaign wall budget,
three million uncached input tokens and 250,000 output tokens. Token thresholds
use completed provider telemetry and can be crossed by an in-flight response;
unknown terminal usage or an unfinished earlier attempt pauses later dispatch.
The runner reserves room for a full next attempt plus cleanup and keeps every
interruption. Confirmation repeats require a separate predeclared manifest; the
same attempt directory is never overwritten.

External behavioral checks, dirty-file/index/HEAD preservation, authored test
quality, substantive final delivery, and provider interruptions are separate
outcomes. Complete each frozen manual rubric against source and observed evidence;
there is no keyword quality scorer. Read-only file hashes do not prove that no
forbidden tests ran, and blank-coupon/malformed-row behavior in the inherited quote
fixture still requires manual inspection. The staged fixture starts empty, so its
index check does not establish preservation of pre-existing staged hunks. Keep
the detailed held-out briefs unread until the candidate guidance is frozen.

Raw TTY-only flows such as exact arrow-key navigation in `/config` and live approval prompts are represented as skipped scenarios until a pexpect-backed driver is added. The current battery still exercises non-TTY fallback paths and captures visual transcripts for subprocess-driven flows.
