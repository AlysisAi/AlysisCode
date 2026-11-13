# CLI and core TUI platform qualification — 2026-10-02

This report records historical local qualification of the revisions named below.
Its results apply to those revisions and cohorts; they do not certify a later
checkout. The archived qualification runner and experimental platform fixes
are not part of the selected change set.

The final bounded pass exercised product core `ace2ffc7`, installed from wheel
build checkout `6052de2f`. Final UI tests used `3603132b`, followed by the shell
fixture corrections `bbe8758f` and `27c25a78`; none changed the installed product. Both
installed trees matched all 532 checkout Python product files (normalizing line
endings only). This extends the earlier `bda5ecdc` pass; its Windows path finding
and fix are retained below.

This is automated installed-package qualification, not a visible usability or
release-distribution certification. No visible Windows Terminal session,
screen-reader trial, real account login, paid provider request, global installer
change, native desktop notification, or desktop clipboard operation was performed.

## Environment and support matrix

| Environment | Measured runtime | Evidence | Current qualification limit |
| --- | --- | --- | --- |
| Native Windows x64, PowerShell 7.6.5 | Windows build 10.0.26200; CPython 3.11.15 | Isolated local-package reinstall; actual CLI startup/help; mocked account/update boundaries; filesystem/process and key-event checks | Installed CLI and headless core TUI qualified within the cohort. Docker daemon unavailable; visible Windows Terminal/ConPTY and image paste unqualified |
| Ubuntu under WSL 2 x86_64 | Ubuntu 24.04.3 LTS; kernel 6.6.87.2-microsoft-standard-WSL2; CPython 3.12.3; glibc 2.39 | Reinstalled local wheel in the task's isolated venv; actual CLI and POSIX PTY startup; same bounded suite and mock build/process cleanup | Installed CLI, headless core TUI and real POSIX terminal rendering/input qualified within the cohort. Existing Bubblewrap strict smoke passed; visible terminal and image paste unqualified |
| Native Linux, macOS, SSH, tmux | Not available for this pass | Platform-adapter tests are not substitutes for those environments | Not qualified by this report |

Both isolated installations used `alysis-code 0.14.2`, `prompt-toolkit 3.0.53`,
`pytest 9.1.1`, `typer 0.27.2`, `Pillow 12.3.0`, and `psutil 7.2.2`.
The original Windows development interpreter used CPython 3.11.15,
`prompt-toolkit 3.0.52`, and `pytest 9.0.3`; it was also used for the source-backed
atomic/session/checkpoint regression run. The fresh Windows dependency resolution
therefore exercised a distinct, newer permitted dependency set.

Windows qualification reused the isolated environment created during the early
pass with `uv venv` and `uv pip install '.[dev]'`, then reinstalled the final local
package with `uv pip install --reinstall-package alysis-code --no-deps .`. WSL
reused an isolated venv established earlier in this task,
then rebuilt/reinstalled the checkout with `pip install --no-deps
--no-build-isolation <checkout>`; this was not a fresh WSL OS installation.
Package imports for startup and installed-package cohorts were verified under
the respective venv's `site-packages`, not checkout `src`.

## Executed checks

| Check | Native Windows | WSL | Boundary |
| --- | --- | --- | --- |
| `python -m alysis_code --version`, root/chat/auth-login/update help | All exit 0 | All exit 0 | Real installed CLI, temporary config/data profile |
| `alysis sandbox doctor --smoke` | Exit 1, Docker daemon unavailable | Exit 0, strict Bubblewrap smoke passed | Real discovery; no image pull, daemon start or backend installation |
| Main installed-package qualification cohort | 242 passed, 1 expected unknown-fixture-model warning | 242 passed, 1 expected unknown-fixture-model warning | Auth/update mocked; local TLS cancellation, resume, approval, clipboard adapters, external editor, keymap, composer |
| Additional probe/build/process cohort | 6 passed, 1 POSIX-only PTY skip | 7 passed | Real Unicode filesystem/process check, local mock-provider edit/verify/diff, owned orphan-process cleanup; WSL real PTY `--version` |
| Installed atomic/session/checkpoint cohort | 83 passed, 7 fixture-model warnings | 80 passed, 3 Windows-only skips, 8 fixture-model warnings | Rebuilt local packages; real 246/251/259-character destination tests on Windows |

The final Windows core TUI cohort passed **417 tests**, with one POSIX-only skip
and 14 fixture-model warnings (174.44 seconds). Its corrected shell fixture and
new non-truncated stderr case then passed separately: **2 passed** (2.89 seconds).
The next WSL cohort had **418 passed, 1 failed** (160.37 seconds): the corrected
shell checks passed, but an inaccurate queued-message fixture failed during
rendering because it lacked `display_text`. Replacing that stub with the actual
message type and forcing a redraw passed on Windows (1 test, 4.86 seconds) and
WSL (1 test, 4.80 seconds). That separate failure is retained in the evidence.
The final WSL rerun passed **419 tests**, with 15 fixture-model warnings
(153.71 seconds), including the real rendered PTY test.

Both UI runs audited 455 imported product modules inside the intended installed
package. The WSL rendered PTY test exercised actual terminal stdin/stdout/stderr,
ANSI rendering, resize from 40 to 80 columns, bracketed Unicode multiline paste,
modal Escape draft restoration, fake-turn interruption and idle exit. Child and
parent package import origins matched. This is terminal/process evidence, not a
visible human or screen-reader trial.

The initial WSL UI cohort had **417 passed, 1 failed** (170.00 seconds). A noisy
shell fixture assumed its final stderr marker must survive a bounded combined
output ring. Independent stdout/stderr pipes do not guarantee that arrival order;
buffered stdout can evict an earlier-read stderr marker. A focused rerun confirmed
the same assertion failure. The test-only correction retains truncation/bounds
checks and separately verifies stderr capture, control-character sanitization,
and exit code 7 without truncation. Both corrected cases passed on WSL (4.83
seconds). No product change or timeout increase was made. Initial failure JSON
and JUnit artifacts were retained alongside corrected results.

The same atomic/session/checkpoint cohort also passed all 83 tests using the
original Windows interpreter with checkout `src`. The installed `alysis` console
entry point separately returned version 0.14.2 on both platforms. These are local
development wheels, not signed/managed release-runtime qualification.

The main cohort was enumerated by the archived runner's `QUALIFICATION_TESTS`.
Its tests ran with
`pytest -o pythonpath=.`: repository test helpers are available, while automatic
`src` injection is disabled. The extra mock-build subprocess deliberately
prepends checkout `src`, so that specific end-to-end build is **source-backed**;
it is not evidence that the installed wheel executed that build. Its provider
was a local fake HTTP server and its workspace was disposable. Sandbox mode was
explicitly off only for that bounded fixture; no user setting was changed.

Authentication checks exercised provider routing and persisted state through
fake adapters. Update checks used mocked HTTP and mocked execution plans; no
user installation was upgraded. Clipboard/image checks used fake platform
readers and image fixtures, not the user's clipboard. Command-approval tests
covered denial without retry; they do not establish visible modal usability.
External-editor tests launched harmless fixture processes and exercised cancel,
failure, and text restoration. Cancellation included a genuinely blocked local
TLS read. Resume tests covered stored conversations and checkpoint state.

The initial test harness incorrectly forced update checks off, producing two
failures in tests that intentionally assert enabled defaults. That harness
setting was removed for the mocked test cohort; both early 241-test runs passed.
A probe-test subprocess mock was also narrowed away from platform discovery
before the final extra-cohort runs. These were harness failures, not product
platform regressions.

## Native Windows path issue and fix

Before the measured experimental fix, `atomic_io` built a temporary name from the entire destination
filename. This added 14 characters. On the measured Windows configuration:

| Destination length | Old temporary path length | Before fix | Short-name control |
| --- | --- | --- | --- |
| 245 | 259 | Direct and atomic write passed | Passed |
| 246 | 260 | Direct write passed; atomic write failed with `FileNotFoundError` | Passed |
| 251 (`budget.json`) | 265 | Direct write passed; atomic write failed with `FileNotFoundError` | Passed |
| 259 | 273 | Direct write passed; atomic write failed with `FileNotFoundError` | Passed |
| 260 or longer | 274 or longer | Final file or parent creation was unavailable | No support claim |

The 251/265 case matches the checkpoint failure found by the parallel checkpoint
workstream. The fix uses an 11-character randomized temporary basename in the
same directory. It retains `mkstemp` exclusive creation, existing private-file
permissions, flush/fsync, atomic replacement, and cleanup behavior. Real Windows
regressions now write successfully at destination lengths 246, 251, and 259.
Shortening the test fixture path alone is not counted as fixing this issue.

This does not enable arbitrary long paths or modify Windows policy. A stress
check also observed `WinError 5` when multiple threads replaced the **same**
destination simultaneously. Atomic replacement is not a writer lock; callers
needing same-file serialization must supply it. This change preserves failure
propagation and does not introduce locks or retries. Tests cover concurrent
unique temporary creation, independent destination writes, original-content
preservation on replacement/fsync failure, and temporary-file cleanup.

## Setup paths and operational distinctions

For native Windows, use an isolated Python environment and invoke its executable
explicitly. The tested repository-install pattern is:

```powershell
# Run from the checkout. Choose a new directory outside it; uv must already exist.
$qualRoot = Join-Path $env:LOCALAPPDATA 'alysis-qualification\example'
uv venv --python 3.11 "$qualRoot\venv"
uv pip install --python "$qualRoot\venv\Scripts\python.exe" '.[dev]'
& "$qualRoot\venv\Scripts\alysis.exe" --version
& "$qualRoot\venv\Scripts\alysis.exe" sandbox doctor --smoke
```

For WSL, install/run the Linux package in a Linux venv, not the Windows venv.
Given an existing Python with venv/pip and a local checkout:

```bash
python3 -m venv "$HOME/.cache/alysis-qualification-example"
"$HOME/.cache/alysis-qualification-example/bin/python" -m pip install '.[dev]'
"$HOME/.cache/alysis-qualification-example/bin/alysis" --version
"$HOME/.cache/alysis-qualification-example/bin/alysis" sandbox doctor --smoke
```

These are repository qualification templates, not a claim that a current PyPI
release includes this branch's changes. The WSL environment creation above was
not repeated during this pass; its local-wheel reinstall was tested.

Native Windows sandbox selection uses Docker; Bubblewrap is Linux-only. Install
and start an appropriate backend separately, then re-run the doctor and follow
[the sandbox guide](shell_sandbox.md). In this pass, Docker CLI 29.5.3 existed on
Windows but its daemon did not respond. The early WSL pass missed Bubblewrap
and discovered a Windows Docker shim without usable integration. During the final
pass, the login-shell PATH exposed an existing user-local `bwrap` (Bubblewrap
0.9.0, ELF x86-64); strict sandbox smoke passed. No sandbox executable was
installed or changed by qualification. That result establishes this WSL backend's
smoke readiness, not Docker readiness or availability on other machines. `strict` requires a usable sandbox, and `warn` does not authorize a
silent fallback to host execution.

Launching Alysis from PowerShell does not make every agent command a PowerShell
command: the host runner uses Python `shell=True` (`cmd.exe` semantics on Windows,
POSIX shell semantics on Linux); sandbox commands execute in the selected
backend. Use an explicit shell executable when shell-specific syntax matters.
The tested process fixtures used explicit Python executables.

`alysis auth login openai-codex --device-code` is the existing alternative when a
browser callback is impractical; see [quickstart](quickstart.md). This report
validated command help and fake authentication boundaries, not a real account
login on either platform. Clipboard support has native/Pillow and
Wayland/X11/PowerShell bridge paths, but availability depends on the actual
terminal/session. Prefer file attachments until image paste is visibly checked.
`alysis update --dry-run --cached` previews the detected installation plan without
running an upgrade or requesting a fresh version check; update behavior was
mock-tested here. Keep updates inside the intended environment.

## Reproduce the qualification

The archived probe was run using the interpreter being qualified. Its invocation
is retained here as historical evidence, rather than an available checkout command.
Supply that venv's
actual `site-packages` directory as `--expected-import-root`, and an output path
outside the checkout. The probe creates fresh external temporary profile and
test directories, verifies import origin, disables passive updates for CLI help,
and leaves update tests' mocked defaults intact.

```text
<venv-python> scripts/qa/qualify_tui_platform.py
  --expected-import-root <venv-site-packages>
  --output <external-evidence-directory>/qualification.json
  --smoke --tests --extra-tests --atomic-tests --long-paths --tui-tests
```

The wrapped lines above represent one command. `--long-paths` is a native Windows
measurement; on other platforms it reports not applicable. JSON and JUnit files
retain exact command results locally. Do not commit personal machine paths or
profiles to the general support documentation.

## Installed core UI cohort and import guard

The archived runner's `--tui-tests` flag ran its fixed `TUI_QUALIFICATION_TESTS`
cohort after the
final local wheel is installed. It includes actual full-app keyboard tests for
editor, diff/review/navigation, checkpoints/recovery, keymaps, integration/planning,
notification preferences, presentation, and shell/task controls, plus their
reservation, scope, persistence and service regressions. The required
`test_tui_rendered_pty.py` is a POSIX terminal/render/input check; native Windows
skips it explicitly. Missing cohort files are failures, not silently omitted.
The cohort is bounded by a ten-minute subprocess timeout and uses short unique
external fixture/profile paths to avoid accidental verbose-path-only failures.

The runner imports the package before pytest configuration, overrides repository
`pythonpath` to `.`, then audits product module origins again after configuration
and at pytest completion. JSON includes the observed import path, module count,
any module outside the expected installation, and the effective pytest path.
An absent/incomplete/incorrect audit fails qualification even if pytest returns
zero. User `PYTHONPATH` and pytest environment overrides are removed. Repository
test helpers remain source files; the application under test must be installed.
The separately labeled `--extra-tests` mock-build subprocess still explicitly
uses checkout `src` and is not installed-wheel build evidence.

Add `--tui-tests --source-revision <built-commit>` to the command above only after
rebuilding the final approved core revision. The source revision is an operator
build annotation; the import audit verifies location, not wheel provenance.
Local development console metadata reports version 0.14.2 with commit/build
unknown (`dev-default`); it does not establish a release build identity. The
separate 532-file installed/source comparison establishes this pass's product
match. JSON/JUnit evidence includes exact commands, import audits and results;
the initial failing WSL artifacts remain separate from corrected artifacts.

## Side-question integration follow-up

Both isolated installations were rebuilt from pure integration revision
`5f2ac0a3de044fdbd98e470e7c57897fb1c6bef7`. All 535 Python product files matched
the installed package after line-ending normalization. Runtime/dependency
versions remained as listed above. This is a separate claim from any later
merge with concurrent work in another checkout.

The bounded follow-up selected nine files: side questions (17 cases), keymaps,
review, recovery, navigation, shell integration, actions, slash completion and
real rendered POSIX PTY. Side-question providers/owned clients were fake;
no real provider, account, notification or clipboard operation was invoked.
The mandatory future `--tui-tests` cohort now includes side questions.

- WSL: **126 passed**, 72.97 seconds, including real PTY. Installed import auditing
  passed, with no product modules outside the expected environment.
- Windows: **124 passed, 1 failed, 1 POSIX-only skip**, 108.25 seconds. All 17
  side-question cases passed. The existing shell mutation/checkpoint case
  `test_local_mutation_failure_captures_after_actual_unwind` exhausted its
  worker-entry wait. A standalone rerun failed similarly (12.34 seconds); a
  diagnostic rerun with pytest's five-second thread-dump option passed (7.97
  seconds), without a dump. No timeout, fixture or production change was made.
  At this stage the intermittent failure remained unresolved; the successful
  diagnostic did not make the affected Windows cohort green. The later timer
  diagnosis and targeted correction are recorded below.
  A final external timestamp/thread-dump probe confirmed the wait's actual
  constants (2,000 iterations, 0.01-second sleeps) and located the worker in
  `prepare_turn` → ledger baseline reconciliation → Git subprocess pipe joins.
  The interpreter then terminated with Windows access violation `0xC0000005`
  during the thread dump, so no completed preparation duration or deadlock
  diagnosis is supported. This incomplete diagnostic is retained separately;
  its failed import-audit completion is not counted as qualification evidence.

The initial WSL helper launch could not resolve a newly created Windows evidence
file through the mounted filesystem; no tests ran in that attempt. Piping the
same helper to the WSL interpreter allowed the recorded cohort to execute.
Installation receipts, exact source comparisons, import audits, JUnit and both
Windows failure receipts were retained in separate follow-up evidence artifacts.
The earlier full core Windows/WSL receipts remain distinct and valid for their
stated revision. The QA cohort-enumeration regression passed seven tests using
the original Windows development interpreter and isolated profiles.

## Windows stop and asynchronous-test follow-up

A later combined source gate exposed a task remaining `running` after Stop.
The ordinary process-tree test passed in isolated installed and source-backed
runs, but a controlled child that ignores Ctrl+Break reproduced a real escalation
defect: the parent exited, PID-based tree termination could no longer reach its
child, and inherited pipes kept the task waiter open. This is distinct from the
checkpoint readiness-test issue above.

The narrow fix retains descendant process identities while the owned launcher is
live, revalidates creation times before termination, and avoids PID-based
`taskkill` on an exited launcher. It also handles retained descendants that close
their inherited pipes. The observation/kill list is capped at 256; inventory
processing has a 0.2-second budget after the synchronous OS snapshot. This does
not impose a hard deadline on the OS snapshot and is not Job Object containment:
new descendants created after inventory or an already-exited launcher cannot be
safely rediscovered by this mechanism. Failed observations never authorize an
unknown PID. The existing `psutil` dependency is reused.

The final source-backed Windows regression suite passed **42 tests** in 32.00
seconds (11 fixture-model warnings), including normal stop, Ctrl+Break-resistant
children, resistant children with closed pipes, unrelated-task survival, PID
reuse, access-denied/missing roots and slow inventory. Ruff passed for all five
affected Python files. These receipts cover commits `07fde5ae` and `32a2f9f7`;
they do not retroactively change the earlier installed-wheel results.

Separately, a measured Windows asyncio timer behavior explained the counted-sleep
readiness fixture: 200 sleeps of 0.01 seconds took 3.1029 seconds when idle but
only 0.0028 seconds with a continuously ready callback; the loop clock resolution
was 0.015625 seconds. Counting 2,000 such sleeps therefore did not enforce the
intended 20-second wait. Commit `08b4b005` uses a monotonic 20-second deadline,
without increasing that intended bound. Against the unchanged installed
`5f2ac0a3` package, the previously failing checkpoint test then passed in 7.02
seconds. The initial failures and diagnostic crash remain in the evidence;
this targeted result is not a rerun of the entire affected Windows cohort.

## Final installed stop-fix gate

Both isolated environments were rebuilt from `92c71862`, whose product source
matches integrated runtime `4a08a47f`. All **535 Python product files** matched
both installed packages after line-ending normalization. Versions remained the
same as above. The final nine-file affected cohort covered Windows owned-tree
identity checks, terminal manager/ownership, shell entry and UI integration,
side questions, keymaps, navigation and real rendered PTY.

| Platform | Final result | Import audit |
| --- | --- | --- |
| Native Windows | **105 passed, 1 POSIX-only skip**, 11 fixture-model warnings; 71.40 seconds | Bootstrap/configured/finished: 1/46/443 modules, all inside the isolated installation |
| WSL | **104 passed, 2 Windows-only skips**, 9 fixture-model warnings; 61.57 seconds | Bootstrap/configured/finished: 1/46/443 modules, all inside the isolated installation |

The Windows run includes normal and Ctrl+Break-resistant process trees, closed
pipes, unrelated-task survival, and the formerly failing checkpoint readiness
case. WSL includes the actual rendered PTY check. No faulthandler instrumentation
or timeout inflation was used. These green affected-cohort results supersede the
earlier affected failures for this corrected runtime, while retaining those
failure receipts. They supplement the earlier broad installed core cohorts;
they are not a rerun of every test or a qualification of another checkout's
concurrent feature merge. Installation logs, source comparisons, JUnit and
complete import audits remain in the separate final-stop evidence artifacts.

## Remaining qualification limits

- Later product changes, including optional polish after this core gate, require
  an appropriate installed-package follow-up; these results cover the stated core.
- Perform visible Windows Terminal and WSL trials for setup, account login,
  approval, image paste, editor suspension/resumption, interruption, resume,
  notifications, and update prompts before advertising those user journeys as
  visibly validated. Native Windows ConPTY was not exercised by the POSIX test.
- Validate native Linux, macOS, SSH/tmux and screen-reader behavior separately if
  those combinations are advertised. Fake clipboard/notification/auth adapters
  and headless real-key tests do not establish desktop integration availability.
- Windows Docker remains unavailable on this host. Follow the sandbox guide;
  unavailable sandboxing never authorizes an implicit host-shell fallback.
