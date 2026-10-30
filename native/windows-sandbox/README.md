# Alysis native Windows sandbox preview

This branch implements an **opt-in, unqualified native shell backend**. It is
not completion of the full Windows sandbox roadmap and must not be advertised
as Codex-equivalent or as containment for the whole agent.

The implementation uses the Apache-2.0 Rust Windows helper from Anthropic's
sandbox-runtime, pinned and adapted under `vendor/srt-win`. See
[upstream provenance](vendor/srt-win/UPSTREAM.md). There is no Node.js dependency
for this helper. The engine owns policy; the desktop consumes that engine.

## MXC development alternative

The optional Cargo `mxc` feature uses Microsoft's MIT-licensed MXC SDK at the
exact revision in [mxc-upstream.json](mxc-upstream.json). It calls the strict
native PSEC runner directly, with ACL mutation and automatic fallback disabled.
It does not provision local accounts. The existing backend and installation
commands below remain separate.

Build with `cargo build --locked --features mxc --target-dir target-mxc`.
For a source-bound candidate bundle, run `python scripts/build_windows_mxc.py
--debug --output <new-absolute-directory>` from the engine checkout. The builder
uses Rust 1.90.0, checks the helper's exact policy contract, inventories tracked
native and engine inputs before and after compilation, and preserves earlier
bundles. `sandbox mxc-capabilities` reports compatibility without executing the
helper or claiming setup readiness.

Desktop's local-candidate packager accepts this bundle only with an independent
helper digest, matching engine source inventory, and both upstream attributions.
It remains manual and unqualified. An explicitly selected, verified local
candidate can negotiate the separate MXC engine contract for Desktop tests;
production activation and release attestation are refused. Source inventories
are build provenance, not signed attestations.
The production engine pin is unchanged.

Security revision 8 blocks external USER objects and the global UI namespace
while retaining Win32k for PowerShell. Revision 7 allowed reading an exact hidden
host-window caption in a synthetic test and is no longer accepted by the engine
or candidate packager. That failing evidence is retained. Use `--ui-boundary` and
`--platform-boundary` for hidden-window, private registry and WMI broker checks.
The optional `mxc-nested-probe` Cargo example calls the pinned SDK directly; pass
its binary with `--nested-probe` to test an inner attempt to widen filesystem
authority. It is a test tool and is not included in the distributed helper bundle.

Automatic Windows read access covers existing System32, Microsoft.NET, WinSxS,
assembly, Fonts and Globalization runtime directories. It does not grant the
whole Windows tree: a synthetic Windows Temp file is denied in the native engine
test. These runtime trees remain privileged platform inputs; broader alias and
sensitive-file qualification is still required.

Development engine selection is `windows-mxc`, with an absolute
`ALYSIS_WINDOWS_MXC_HELPER` and matching `ALYSIS_WINDOWS_MXC_SHA256`. This is never
automatically selected, and `sandbox doctor` continues to report not ready.
Keep the helper in a small, trusted bundle directory: the engine inspects its
read root before launch. WSL continues to use Linux backends.

The adapter reuses the engine's environment, workspace, instruction, skill and
metadata protections. In workspace-write mode, preparation exclusively creates
missing instruction files and empty protected directories, including `.git` and
skill directories. These persist after execution, including failed launches.
Existing contents are preserved. Original directory/file handles stay open
through native execution and full descendant drain; no pathname cleanup or host
ACL mutation is used. Read-only mode creates no reservations.

This visible metadata behavior matches the older native backend and remains
development-only. Test in synthetic workspaces: an empty `.git` is not an
initialized repository. Native read-only rules for absent paths did not block
creation, and a separate full-deny experiment blocked creation but allowed a
rename into an absent protected name. That experiment was withdrawn; retained
reservations protect existing objects instead. Missing protected files must be
reserved by the engine before the helper runs. Filesystem races, broader host
and network qualification, installed distribution and Desktop packaging remain
release gates.

Bounded evidence comes from `scripts/qualify_windows_mxc.py` and the opt-in
native cases in `tests/test_windows_mxc.py`. Unsupported Windows hosts must refuse
workload launch. CI reports that refusal separately from native execution.

Add `--lifecycle --internet --filesystem-races --network-boundary` to the harness
for bounded descendant/crash/reuse checks, public Schannel HTTPS, metadata-only
junction attacks, and host TCP/UDP boundaries over IPv4, IPv6 and mapped IPv4.
UDP checks observe a live receiver and use the identical uncontained sender as
a positive control: a successful Winsock send is not proof of packet delivery.
These checks do not qualify other-machine LAN access or external ingress.

## Existing AppContainer backend

- `windows-native` backend in the engine's foreground, verification and session
  background execution paths. `auto` retains the existing backend selection.
- A fresh AppContainer for every command with an explicit network policy, combined with a
  write-restricted token, isolated desktop, and kill-on-close Windows job.
  The AppContainer retains the host's user SID; its package SID gates access.
  The installed dedicated account SID supplies the independent write restriction
  and remains the identity used by installation enforcement probes.
- Workspace read/write grants, read-only workspace policy, protected repository
  metadata and workspace-root instruction files, explicit tool read roots and
  filtered environment variables. Absent `AGENTS.md`, `CLAUDE.md` and
  `CONVENTIONS.md` names are
  reserved before launch so a command cannot plant their contents.
- PowerShell starts on a process-local `Alysis:` drive rooted at the workspace.
  `$PWD.ProviderPath` reports the physical path. Native child processes receive
  the physical working directory. Parent directory listings are not granted.
- Writable workspaces must belong to the current Windows user. The helper retains
  an inheritable grant for that owner so sandbox-created output stays accessible
  after temporary sandbox grants are removed. This owner grant is persistent.
- Read roots owned by the current user retain an explicit owner read/execute
  grant. This lets AppContainer reads work in private Windows folders that use
  `OWNER_RIGHTS` permissions; it adds no write access to read-only workspaces.
- A single machine-wide execution/maintenance lease. Setup and removal refuse
  active commands or a pending recovery journal before requesting UAC and check
  again while holding the same lease in the elevated process.
- A single machine-wide command slot. A second command fails immediately
  while another owns the identity. A background server therefore occupies the
  slot until it exits.
- Ambient filesystem permission probes run under the dedicated account, including
  for system folders whose security descriptors the normal user cannot inspect.
- Effective write-permission probes cover every existing object below protected
  roots. The helper also impersonates the actual suspended child token and checks
  each dangerous right separately before resuming user code. A failed protection
  stops execution. Package SID deny ACEs alone are not a write boundary; the
  independent restricted SID and its existing ACL denies enforce that boundary.
- Workspace preflight rejects existing hardlinks and reparse points before
  granting access. The same scan covers helper and explicit tool read roots;
  read-only grants can otherwise expose a private file through another name.
- Root project skill directories discovered by the engine are protected,
  including `.alysis_skills` and `.github/skills`. Intermediate ancestors such
  as `.github` cannot be renamed, deleted or have their ACL/owner changed;
  ordinary sibling contents such as `.github/workflows` remain writable.
- `network=off` supplies zero AppContainer capabilities. Explicit `network=on`
  supplies only `WinCapabilityInternetClientSid` for outbound internet access.
  The actual child's complete capability set is verified before resume.
  Persistent SID-scoped WFP filters still protect the dedicated-account setup
  probes. They are not the AppContainer command's network boundary.
- Fresh private profile and temporary storage per execution. Profile deletion,
  package ACL revocation and window-station cleanup are recorded for recovery.
- Parent-process identity checking and a lifetime monitor, so engine exit
  terminates the broker and closes its jobs.
- Process creation atomically assigns the child to a named containment job;
  there is no create-then-assign orphan window. Normal cleanup terminates remaining
  children and verifies zero active job processes before removing filesystem
  protections or the execution profile. Crash recovery checks that same job and
  pins the original broker by PID and creation time before removing protections.
  Failed verification retains the recovery journal and exits the broker.
- Failed/interrupted cleanup blocks further use until explicit recovery.
- Helper SHA-256, protocol and security-revision validation before setup or
  execution; no executable lookup through PATH. Helper v0.3.3 uses protocol 3,
  security revision 4 and recovery journal version 2.
- The machine-encrypted account credential is readable only by the recorded
  Windows owner, SYSTEM and administrators. The opened registry key's actual
  owner and protected ACL are checked before reading its credential value.
  Older stores with a broad Users read grant fail closed until elevated setup
  rotates the password into a fresh owner-only registry key. Every rotation uses
  a new key, so handles opened before an ACL repair cannot read the new password.
- ACL updates pin the target and ancestors against replacement, reject reparse
  points, and compare the recorded file ID on the handle used for the update.
  Windows descendant inheritance propagation still requires further hardening.
- Setup, read-only status, diagnostic smoke and recovery commands.
- Desktop environment filtering prevents injected helper paths/digests. Its
  configuration diagnostic distinguishes preview selection from verified
  containment.
- The current engine advertises a separate native-sandbox capability contract.
  Desktop session logs can use an exact host-authorized directory, with alias
  rejection and no persistent user-config changes; retained sessions resume
  through the engine's existing IDE protocol.
- Native Windows managed sessions open each component relative to a pinned
  directory handle. They reject reparse points, hardlinks and foreign-owned
  logs, and retain the same admitted file handle for replay and append. The
  existing POSIX session implementation remains in use under WSL.
- A fixed, trusted `NODE_OPTIONS=--preserve-symlinks --preserve-symlinks-main`
  lets Node load workspace scripts and dependencies without resolving inaccessible
  drive/profile ancestors. Host/caller `NODE_OPTIONS` remains rejected. This does
  not grant access to symlinks: existing workspace aliases remain unsupported.

No account, firewall rule or trust certificate is installed by building or by
the ordinary unit tests.

## Build and run in a disposable Windows x64 test VM

Prerequisites: Rust 1.90 or later with MSVC build tools, Python 3.11 or later,
and the engine's normal dependencies. The preview accepts fixed local NTFS
workspace roots. It rejects UNC roots, drive roots, whole-profile workspaces
and roots/cwds that traverse junctions or other reparse points. Workspaces
containing hardlinked files, symlinks or junctions are also rejected. The
preflight scans the workspace; large trees incur extra startup cost.

```powershell
python scripts/build_windows_sandbox.py
$helper = (Resolve-Path native/windows-sandbox/target/bundle/release/alysis-sandbox.exe).Path
$env:ALYSIS_WINDOWS_SANDBOX_HELPER = $helper
$env:ALYSIS_WINDOWS_SANDBOX_SHA256 = (Get-FileHash -LiteralPath $helper -Algorithm SHA256).Hash.ToLower()
$env:ALYSIS_SHELL_SANDBOX_BACKEND = 'windows-native'
$env:ALYSIS_SHELL_SANDBOX_NETWORK = 'off'
$env:ALYSIS_SHELL_SANDBOX_MODE = 'strict'
alysis sandbox setup --native-windows
alysis sandbox doctor --smoke
```

Setup requests UAC consent. It creates `alysis-sandbox` and
`alysis-sandbox-users`, writes setup/credential records under
`HKLM\SOFTWARE\alysis-sandbox`, adds WFP rules under GUID
`49e9dc19-a523-49b7-ae6a-5e2ffb908e47`, and applies the helper's ambient system
directory protections. Use consent elevation under the same Windows identity;
cross-account credential elevation is not supported in this preview.

The development override pins a locally trusted build. It is not a substitute
for signed release provenance. The build script emits a manifest and license
beside the executable in a small `target/bundle/<profile>` directory, so runtime
ACL grants do not cover Cargo's build cache. It does not put a Windows binary into a universal Python
wheel. The desktop packaging pipeline now accepts an explicitly pinned helper bundle
and binds its executable, license and provenance notice into the managed-runtime
fingerprint. Production inclusion requires a reviewed source pin, a production
qualification marker and a valid pinned Authenticode signer. Unsigned local
candidates remain experimental. Universal Python wheels remain platform-neutral.

Inspect setup without running a command:

```powershell
alysis sandbox native-status
```

After a timeout, forced stop or engine crash, stop any active sandbox commands
and run `alysis sandbox recover-native`. Recovery refuses an active owner or an
unverifiable cleanup. Do not delete the journal to bypass that refusal. Review
the reported path and ACL state; restore an accidentally moved target if needed.
The journal lives under `%LOCALAPPDATA%\alysis-sandbox`. Setup removal is an
explicit operator action: `& $helper uninstall`, which requests UAC. Recover
workspace grants before uninstalling the account.

The native doctor's smoke workspace is removed only after an exact successful
result, confirmed journal absence and a matching workspace identity. A failed
or interrupted smoke retains the directory and reports its path. Keep that
directory intact until normal recovery completes. An unreadable journal is not
treated as absence. This prevents the diagnostic itself from causing the
deleted-workspace recovery limitation.

Missing protected directories and workspace-root `AGENTS.md`, `CLAUDE.md` and
`CONVENTIONS.md` files
are created as persistent empty placeholders.
Their bookkeeping records remain after command cleanup so later grants can
protect their ancestor chain. They are not active sandbox permission grants.
For an approved native command, the engine exclusively creates missing
instruction files through pinned directory handles before launching the helper.
In-process creation receipts establish only those files' initial mutation
baseline. Existing files, later edits and replacement objects remain ordinary
project changes; an empty filename is never a blanket reporting exclusion.

Optional config under `shell_sandbox`:

```json
{
  "backend": "windows-native",
  "mode": "strict",
  "network": "off",
  "filesystem_access": "workspace-write",
  "windows_read_roots": ["C:\\tools\\python", "C:\\tools\\node"]
}
```

Commands use Windows PowerShell with profiles and interactive prompts disabled.
Tools without existing AppContainer read access need explicit read roots. Those
roots must allow the current user to update their ACLs; use a user-owned portable
installation when a system-wide tool does not permit that. The launcher does not
silently change Program Files permissions. Read roots overlapping confidential
read-deny locations are rejected. Metadata is
kept read-only; native safe-Git mutation approval is not implemented yet.

## Network policy and permission-mode integration

Protocol 3 supports `network=off` (the default) and explicit `network=on`. The
engine and helper agree on this policy independently of approval-mode labels.
The Desktop permission policy decides the grant; this helper enforces it.
Selecting online access never disables filesystem or process restrictions.
Older protocols are rejected before execution; upgrade the engine/helper together.

The current integration targets engine 0.14.2 at `755d3ab7380657da3ce108a8e1c0a36433bd7ebf`
and Desktop at `0340d0895f6a1d6832806747bb84b54845fc71c0`:

| Desktop choice | Engine mode | Shell policy |
| --- | --- | --- |
| Read only | `readonly` | Strict sandbox, read-only workspace |
| Ask for approval | `review` | Strict sandbox, writable workspace, human approval |
| Approve for me | `auto` | Strict sandbox, writable workspace, approval reviewer where applicable |
| Full access | `fullaccess` | Host execution by default; strict sandbox when explicitly retained |

Native preview selection remains explicit. It never retries a failed native
launch on the host, including when the general unavailable-backend fallback is
`ask`. Approving a command does not remove its configured containment.
Network access is a separate setting in every sandboxed mode.

Interactive host terminal creation/input/close requires Full access **and** shell
sandbox mode `off`. The engine checks the effective mode at dispatch; Desktop
also rechecks the live grant and strict-sandbox setting. Listing and reading
terminal output remain available. Policy changes block subsequent agent requests;
they do not terminate or sandbox host programs already started under Full access.
These guards cover the new host terminal route;
separately approved IDE tasks, debugger actions and other host tools remain
separate authorities, not operations confined by the native shell sandbox.

Online mode currently grants broad outbound internet capability. It does not
implement domain allowlists, per-request approval, localhost exemptions, private
network/server capabilities, or an authenticated proxy broker. Windows network
classification remains relevant; an absent private-network capability is not an
independently qualified destination/IP filter. Do not advertise it as such.

Earlier Node/OpenSSL HTTPS checks retrieved public package metadata with
certificate validation enabled. Earlier 0.14.1 qualification has intermittent
host failures and a sandboxed online timeout after a successful host control;
this is not a current passing network qualification. Switching back to offline
uses a fresh identity without the internet capability. The HTTPS regression
uses online/offline/online controls and never installs or executes a downloaded
package.

**Known release blocker:** Windows Schannel clients, including Windows PowerShell
HTTPS and the system `curl.exe`, fail with `SEC_E_NO_CREDENTIALS (0x8009030e)` under
the independent restricted token. The compatibility test reports this exact
failure as `xfail`; it is not a passed test. Removing filesystem restrictions or
disabling TLS verification is not an acceptable workaround. Tools requiring
Schannel, enterprise credentials, or system proxies are not qualified. The
doctor's launch smoke verifies token/policy enforcement and makes no internet
request; it does not establish HTTPS client compatibility.

## Node subprocess compatibility

**Known release blocker:** Node 22.20.0's piped `spawn`, `spawnSync`, IPC `fork`
and default `node --test` all hang before completing in this AppContainer.
All eight combinations of these APIs and the two network policies reproduced
the problem with successful host controls and normal recovery. Its libuv creates unique pipes in
the ordinary Windows pipe namespace and retries access denial indefinitely.
Finite host/container controls prove ordinary-pipe creation is denied inside
the container while `LOCAL` pipe creation succeeds. This affects libuv-generated
child stdio and IPC, including piped `spawn`, synchronous subprocesses and
`fork`; passing a simple Node script does not qualify these operations.

Upstream [libuv 1.53.0](https://github.com/libuv/libuv/releases/tag/v1.53.0)
fixes generated names by detecting AppContainer and inserting `LOCAL` into the
pipe path. The [implementation](https://github.com/libuv/libuv/blob/v1.53.0/src/win/pipe.c)
does not grant access to the ordinary namespace or rewrite application-supplied
pipe names. The October 3 exact-source check of official Node releases 22.23.3,
24.21.0, 26.10.0 and nightly `v27.0.0-nightly20261002a7a978415c` found that all
still generate the ordinary namespace, without the AppContainer branch. A routine
Node upgrade is not a verified fix. Check the [official release index](https://nodejs.org/dist/index.json) and
qualify the exact selected executable when a fixed runtime is available.

A pipe-only backport against exact Node 22.23.3 source was built on the isolated
Windows runner in [PR 45](https://github.com/AlysisAi/alysis-code-internal/pull/45),
run `37195859285`. Its verified binary SHA256 is
`ab970b19b7e9b04da6adb52e74a4782c45fedf888a4cda2322836f08565920d8`.
All ordinary-host subprocess controls pass, but all eight real sandbox cases
fail immediately with `EPERM` instead of hanging. The pipe patch alone therefore
does not qualify this runtime. Ordinary namespace denial and LOCAL creation
controls still pass. Certificate-verified Node HTTPS passed online/offline/online;
Schannel still reproduced `SEC_E_NO_CREDENTIALS`. Cleanup left zero temporary
ACL records and no journal. The unsigned candidate does not replace the user's
runtime. Exact reports are recorded in `qualification.json`.

The follow-up native probe isolates the remaining denial to reopening the LOCAL
pipe client. NUL handles, direct child creation, and inherited/ignored stdio
succeed. NPFS default pipe security omits the independent write SID. An attempted
exact-package token restriction was rejected by Windows and reverted before any
helper replacement. The revised runtime patch supplies the existing token
default DACL only when creating libuv's private first-instance AppContainer
pipes. Exact patched C probes passed stdin/stdout/IPC on the host and in both
network modes, with ordinary pipe namespace denial unchanged. The verified
binary from build `37223682635`, SHA256
`61bc4e4d87df40ce62c7073a3656d0afbeeeb1c55b528628be30ae98d9278e1c`, then passed
all eight actual subprocess/network combinations and the certificate-verified
online/offline/online HTTPS control: **9 passed, no skipped or expected failures**
in 98.35 seconds. The subprocess reports bind all eight executions to that exact
hash. Ordinary namespace denial remained enforced; cleanup showed zero grants,
zero holders and no journal. Filesystem token restrictions remain unchanged.
This qualifies the tested candidate on this host. The user's installed Node was
not replaced. Supported-platform tests, maintained runtime distribution and
signing remain release requirements.

PowerShell file-script authorization now snapshots the host's effective
execution policy with a fixed, isolated, bounded host query before each request.
The query explicitly names `Microsoft.PowerShell.Security\Get-ExecutionPolicy`
and retains its ten-second deadline. Local comparisons preserve effective policy
for normal, `AllSigned`, `Restricted`, and `Undefined` Process preferences. Fresh
CI showed the prior unqualified command taking 24-27 seconds despite fast shell
startup. Fresh Windows2022 and Windows2025 CI then measured the qualified command
at 0.298 and 0.340 seconds. Both now get past policy discovery and reach the
separate inherited-job launch restriction.
The child receives that exact policy at Process scope; group policies retain
precedence. This avoids the inaccessible CurrentUser policy registry key without
granting registry access. Failed policy queries prevent launch. Agent environment
overrides cannot select policy or module search paths. Direct and nested `.ps1`
tests pass locally for `RemoteSigned`, `AllSigned`, and `Restricted`, including
unsigned Internet-zone denial. No persisted execution policy is changed.
Supported-platform qualification remains outstanding; see `qualification.json`.

Support requires bounded successful subprocess, IPC and default test-runner
checks under the actual sandbox policy. A version number alone is insufficient.
Deadlines must be outside Node, since the affected native call can block its own
timeout handling. The installed regression uses host and namespace controls,
a bounded actual API invocation and normal identity-bound recovery.
Only the reproduced old-libuv hang with successful recovery is an expected
failure; other failures remain failures. Ordinary namespace denial must remain
intact when a fixed runtime passes.

By default the Desktop fixture explicitly chooses
`node --test --experimental-test-isolation=none` to exercise verification
without a child runner. This is narrower integration evidence; it neither fixes
nested subprocesses nor qualifies default Node execution. Production commands
are never rewritten to use that flag or executed outside containment. The
dedicated-account AppContainer candidate needs the same libuv fix.
A separate Desktop test opt-in, `ALYSIS_TEST_CANDIDATE_NODE_SUBPROCESS=1`, selects normal
`node --test` for a hash-verified runtime. Unknown values fail, and a failed
subprocess run is never retried with single-process isolation.

## WSL and desktop behavior

A Linux engine inside WSL2 continues to use Bubblewrap or an explicitly selected
Docker backend. Selecting `windows-native` from Linux fails with remediation.
Installing the Windows helper does not secure a WSL distribution. The explicit
`filesystem_access=read-only` grant now mounts the workspace read-only for
Bubblewrap foreground, verification, background and durable-service paths;
workspace-write remains the default. Private scratch space is still writable.
Linux and DrvFS tests exercise actual read success and write denial, including
runtime-path aliases and attempted environment overrides. Broader WSL
interoperability, Windows executables and network namespace escape testing
remain separate release work.

Docker's read-only workspace requests recursive read-only binding and private
mount propagation. Older daemons must fail explicitly instead of silently
leaving nested mounts writable. Docker needs separate live qualification on a
supported daemon; local argv tests alone are insufficient. Seatbelt currently
rejects a read-only workspace policy explicitly. Permission-mode labels do not
change these backend capabilities. Ask for approval/Approve for me/Full access selection
belongs to the authorization layer; the runner consumes explicit filesystem,
network and sandbox settings. Explicit sandbox-off/host execution has no native
sandbox boundary, and explicit `windows-native` never silently falls back.

The desktop's existing read-only/workspace-write environment ceiling is now
consumed by the native engine backend. The desktop now validates packaged helper bytes and negotiates the dedicated
`alysis sandbox native-capabilities` contract before admitting native execution.
That read-only contract describes engine support, not setup or isolation readiness.
The sandbox integration now uses engine **0.14.2**, based on commit
`755d3ab7380657da3ce108a8e1c0a36433bd7ebf`, on branch
`feat/windows-sandbox-0142-integration`. The earlier 0.9.14 and 0.14.1 sandbox
checkouts and their test artifacts are preserved. The local Desktop candidate uses
the engine's full normal capability contract, including retained-session
resume; the temporary compatibility adapter for the older checkout was removed.
The session directory must exactly match the trusted Desktop-selected root.
Managed transcript failure recovery also retains a duplicate of the original
file handle. Retry, truncation where supported, and uncommitted-record markers
use that capability; none reopen the log by pathname. Replay applies the same
in-log failure ranges as ordinary session replay. A preexisting legacy failure
sidecar requires explicit reconciliation before managed admission and is left
unchanged. If neither truncation nor a marker can succeed, the store reports an
indeterminate result and blocks further records; it cannot promise persistence
when all writes fail, and it never falls back to an unadmitted sidecar path.
The production source pin and production resources have not been changed.
A dedicated setup UI and installed-app rollout remain work.
Launching the current packaged app does not activate this
preview. No production signing or installed-app qualification has been performed.

## Known boundaries and release gates

1. **Network isolation and client compatibility need broader qualification.**
   Offline DNS-service isolation has a canary regression. Explicit online access
   preserves the AppContainer and write restrictions, but Schannel HTTPS remains
   a known failure. Additional service/IPC paths, Windows versions, private-network
   classification and destination-scoped approvals still require work. No
   authenticated proxy broker is implemented.
   Node subprocess/IPC compatibility also requires a fixed and qualified runtime
   as described above; the current default runner remains a release blocker.
2. **Filesystem isolation needs qualification.** This is not a mount namespace.
   Windows resources explicitly shared with all AppContainers remain part of the
   platform's ambient surface. Existing workspace aliases are rejected, and
   protected objects are probed with the actual child token. ACL root replacements
   are now guarded by file-ID checks and pinned handles. Concurrent descendant
   replacements during inherited ACL propagation and aliases introduced after
   preflight still need implementation work; linked Git worktrees are unsupported. Local canary tests
   cover creating a hardlink to protected metadata and junction attempts from
   both the workspace and execution profile. They do not cover every reparse
   tag or host-side replacement race.
3. **Concurrency remains serialized.** Execution profiles and package SIDs are
   fresh, but the account-based write policy and recovery lease are shared. A
   background command still occupies the single slot. Registry/IPC cross-command
   isolation needs additional qualification.
4. Durable services fail closed. Develop their lease transfer and readiness
   protocol before enabling them. Interactive PTYs are not supported.
5. Host file tools, custom tools, hooks, MCP servers, browser/computer use and
   provider connections are outside this shell boundary. Their existing
   permissions still apply; a whole-agent containment claim is not warranted.
6. Run the installed VM matrix for identity, denied reads/writes, network/IPC,
   descendants, timeout, engine death, recovery, reboot, tampered setup,
   concurrent users and account cleanup. Test x64/ARM64 and supported Windows
   editions separately; this build path currently targets x64.
7. Require signed distribution manifests, installed-app evidence and rollback
   tests before changing `auto` or enabling the feature by default.
8. Recovery must prove the identity of each granted object. A missing path may
   have been moved, unlinked while open, or deleted. NTFS can return ambiguous
   error 87 after final deletion; the current helper preserves the journal and
   fails closed. It cannot yet safely resolve every deleted-workspace case.
   Deleting the journal or interpreting every failed file-ID lookup as deletion
   would discard that protection.
   See [recovery guidance](RECOVERY.md) for the supported actions and the
   distinction between a moved object and an unverifiably deleted one.
9. **Inherited job restrictions can prevent launch.** The preview's child job
   enforces UI restrictions and requires a compatible job hierarchy. A
   controlled test with the exact helper passed with no added parent job and
   with an owned parent permitting breakaway; an owned parent forbidding it
   reproduced `CreateProcessAsUserW` access denied. All three cases cleaned up.
   Windows2022/2025 hosted-runner diagnostics also report an associated job
   without breakaway. Querying one associated job cannot prove the whole chain
   permits creation. See Microsoft's [nested-job rules](https://learn.microsoft.com/en-us/windows/win32/procthread/nested-jobs).
   The child containment policy remains enforced, and these hosted contexts
   remain unqualified. Both fresh-runner platforms also reproduced a ten-second
   timeout in the host PowerShell policy query before sandbox launch. Bounded
   startup and policy controls run only after that failure, preserving the
   original test result and the production query deadline.

## Validation

The current 0.14.2 integration was tested on October 4, 2026, initially with the
preserved 0.3.3 helper, protocol 3 and security revision 4. A separate helper with
the recovery correction was then built and tested; its exact digest and the
earlier digest identify each result. Current results and remaining
release gates are recorded in `qualification.json`; the earlier record is
preserved in `qualification-014.json` and its original checkout.

- A packaged Desktop test found that an existing protected file at 266 UTF-16
  units was reported missing by the direct Win32 enforcement probe. The probe
  now encodes admitted local paths with the extended prefix, without resolving
  or normalizing objects. It still tests each dangerous right independently
  and accepts only access denied. Regression coverage includes long files and
  directories, both path spellings, each of eight rights individually allowed,
  and missing files. The native suite passed **222 tests, 4 existing ignored**,
  plus **2 wrapper tests**. A separately built helper passed the full native
  Desktop matrix in **93.26 seconds**, with the protected installed file at
  exactly 266 UTF-16 units. The old helper reproduced the failure against that
  same file; the corrected helper reached the real access check. All 555 engine
  Python files matched the preserved wheel, and cleanup left no grants, holders
  or journal. Prior bundles and candidates remain intact.
- An independent, byte-identical shorter Desktop candidate passed all five
  native approval/filesystem cases in 87.19 seconds. A real **gpt-6-luna** run
  passed the model-to-reviewer-to-native-AppContainer canary with three requests
  and 3,187 reported tokens. Normal configuration was unchanged, and cleanup
  showed zero grants, zero holders and no pending journal. Its verification
  used explicitly single-process Node; it does not qualify normal subprocess
  isolation or resolve the longer candidate's failure.

- Combined engine/sandbox/approval regressions after retained-handle recovery:
  **835 passed, 17 platform or privilege skips** in 55.38 seconds, using the exact
  current dependency lock.
  The selection includes both Windows managed-session suites, all approval modes,
  strict host-terminal refusal, and native failure-without-host-fallback checks.
- Focused managed-session and task-durability regressions: **157 passed, 9
  platform/privilege skips** on Windows and **137 passed** on WSL. These selections
  overlap the main suite. They cover replacement attempts after writer failure,
  retry and marker recovery through the retained file, refused-record replay,
  sidecar preservation, and complete marker failure without pathname fallback.
- WSL: **211 passed, 4 platform skips** in 202.55 seconds, including all **15 live
  Linux/DrvFS confinement checks** in a separate 0.14.2 locked environment.
- Full installed Windows suite: **53 passed, 8 expected failures, 2 failures** in
  400.86 seconds. All eight Node subprocess cases reproduced the known hang and
  recovered normally. Both public HTTPS tests stopped at a failed ordinary host
  control; no successful sandbox HTTPS qualification is claimed for this run.
- The two HTTPS tests were then rerun with longer bounded deadlines: **1 host
  control failure and 1 expected native failure** in 28.16 seconds. The .NET host
  control passed and native Schannel reproduced `SEC_E_NO_CREDENTIALS`. Default
  Node still failed on the host before reaching the sandbox. This overlapping
  follow-up does not count as two additional unique tests.
- After the installed suite, no pending journal or active ACL holders remained.
- Separate nested-helper negative controls passed in both network modes
  (**4 passed**). They proved the copied helper could execute inside the outer
  AppContainer, then required its legacy `exec` path to refuse credential
  admission before an inert child marker could run. The raw stdin runner also
  refused at restricted-token creation before its target could execute.
  ACL counts remained zero
  and no journal remained. This tests the actual credential boundary without
  reading or displaying credentials; it does not prove every legacy entry
  point or cross-version upgrade safe.
- Current native library tests with the disabled account feature compiled:
  **220 passed, 4 explicit manual probes ignored**, plus **2 default wrapper
  policy tests passed**. The candidate subset includes nine activation checks,
  shared-journal lifecycle checks, six full-resource ledger checks, thirteen
  WFP writer checks, fifteen password-application checks, eleven disabled-account
  policy checks and fifteen explicit object-grant checks. The policy tests cover
  104 interrupted registry/native boundaries without real account changes.
  Candidate launch
  remains unavailable before credential access until
  profile, namespace and job recovery are integrated. No candidate installation
  or machine-policy changes were made by these tests.
- The recovery follow-up additionally passed four real filesystem regressions
  and the default native suite (**86 passed, 2 manual probes ignored**). A
  missing root can no longer retire recursive grant records without descendant
  evidence. Deleted-ID error 87 and moved descendants remain explicit recovery
  limitations; [recovery notes](RECOVERY.md) describe the retained state.
- The policy/recovery Python follow-up passed **861 tests, 17 skipped** locally.
  Fresh Windows Server 2022 and 2025 CI installations succeeded but both refused
  child creation with `CreateProcessAsUserW` access denied. Recovery and removal
  succeeded on both runners. These are failed platform qualification runs;
  process/job/desktop diagnostics are enabled for the next run.
- Desktop focused tests: **86 passed**; TypeScript and the Electron/PDF-worker
  builds passed. Candidate `c0c705d9ffead9ea` passed fresh offline installation,
  ordinary capability checks and chat continuation, followed by real native
  commands with the automatic reviewer, human allow/deny, and strict Full access
  in **93.37 seconds**. Approval bypass was disabled. Verification used an
  explicitly single-process Node command; default Node subprocesses remain
  unqualified. Cleanup was independently checked again after this integration.
- The broader managed-runtime fixtures were repaired after reproducing the same
  14 failures on untouched Desktop main. Native Windows fixtures now use the
  actual Windows executable layout: **27 passed, 14 explicit platform/privilege
  skips**. The companion WSL run is **38 passed, 3 macOS-only skips**, including
  every POSIX scenario gated out of the Windows run.
- Real ChatGPT subscription tests used `gpt-6-luna`: native tool requests and
  automatic-reviewer allows passed under both filesystem policies; human
  allow/deny behaved correctly; a separate reviewer-only prohibited publish
  proposal was denied without dispatch. A packaged Desktop IPC turn also passed
  through the actual managed CLI and native provider. All prompts/workspaces
  were synthetic; no provider/profile switch was required. These remain bounded
  integration tests, not a claim that model review is a security boundary.
- An isolated, hash-pinned Node/libuv compatibility build completed through
  [draft PR 45](https://github.com/AlysisAi/alysis-code-internal/pull/45).
  Its ordinary-host checks passed. All eight sandbox subprocess cases still
  fail with immediate `EPERM`; Node HTTPS now passes, while Schannel remains
  incompatible. The Node subprocess release blocker remains open.
- Independent integration review found and closed the new host-terminal bypass
  under Full access with strict sandbox retained. Session integration preserves
  main's owner-only DACL verification and 128-bit file identities while adding
  retained ancestor handles.

These results do not meet the release gate. The installed gate requires every
check to run and pass; an expected failure or skipped check cannot satisfy it.
The ordinary/legacy `SessionStore` path also has a separate Windows privacy
limitation: unlike the managed CLI session path, it relies on inherited parent
ACLs. A POSIX `0600` assertion fails on Windows and does not measure its DACL.
Owner-only log admission on that path requires separate work.

### Earlier 0.14.1 evidence

The earlier engine was **0.14.1**, with helper **0.3.3**, protocol 3 and security
revision 4. Its tested helper SHA-256 is
`30acc99d642ef73deac5b843c217aef5475ebb82d50ecf1ce62a05b11e1ffc12`.
The installed credential repair completed with UAC consent; Windows was not
restarted. Exact results and superseded historical evidence are recorded in
`qualification-014.json`. Paths in that historical record refer to the original
`alysis-code-sandbox-014` checkout, whose artifacts remain intact.

- Final combined Windows Python regressions: **511 passed, 21 platform skips**
  in 50.08 seconds. This includes doctor workspace retention, cross-backend
  filesystem policy, and the 93 completion-reporting and
  verification-recovery tests also checked separately after the notice fix;
  completion authority and approval policy remain unchanged.
- Native unit tests: **84 passed** (82 primitives and 2 wrapper tests), with
  two explicit live probes ignored by default. Account maintenance now refuses
  domain controllers and unknown/error machine-role results before SAM changes;
  execution and recovery remain independent of this guard.
- Disabled account-candidate tests: **50 passed**, with two explicit live probes
  ignored by default. Ten new isolated tests cover the private SQLite storage
  mechanics. Its production-location probe refuses this host's AppData capability
  FullControl grant before writing; these tests do not qualify that location.
  The separate read-only role probe passed under a verified
  non-elevated token. Bootstrap interruption and credential tests did not change
  installed accounts, setup registry or WFP policy.
- WSL: **134 passed, 2 platform skips**, including all seven live Linux/DrvFS
  sandbox checks and the current engine's managed-session tests. A subsequent
  bookkeeping and approval/session regression group passed **78 tests**, with
  2 platform skips. The new filesystem-policy checks passed **112 portable tests**
  with 2 platform skips and **15 live WSL tests**, covering both Linux and DrvFS
  workspaces, foreground/background/verification execution and read-only aliases.
- Strict real CLI/read-only checks: **4 passed**. Both filesystem policies
  prove an actual AppContainer, successful parsed tool result and exact stdout.
  Read-only commands can read the workspace; write-denial checks prove the
  command started before accepting an access-denied result.
- The current helper passed all **53 local installed Windows tests** in 199.96
  seconds, plus the real CLI doctor smoke. This rerun covers confinement and
  recovery; the ten public HTTPS and known Node subprocess cases were excluded.
- Latest full installed Windows suite including public HTTPS, on earlier helper
  `692f307e...`: **53 passed, 2 failed, 2 expected
  failures** in 222.83 seconds (`target/014-installed-final-tests.xml`). Node
  HTTPS timed out in the sandbox after a successful host control; the Schannel
  check stopped at a host timeout. Earlier evidence still records the exact
  restricted-token Schannel failure. The two finite-pipe expected failures were
  then replaced with actual Node subprocess checks: all four APIs reproduced
  the known hang in both network modes and recovered normally (**8 expected
  failures**, 180.10 seconds, `target/014-node-subprocess-matrix.xml`). Neither expected
  failures nor host-control failures satisfy the release gate.
- Desktop: **57 focused tests passed**, plus TypeScript and Electron build.
  Candidate `ce75210b5fce0ce9` with the current helper passed fresh installation,
  chat continuation, both filesystem policies and explicitly configured
  single-process verification in 66.25 seconds. These use the full normal
  capability gate and real managed CLI. Exact hashes and results are recorded
  in `qualification.json`. Successful tool results are parsed from the normal
  saved-result artifacts when offloaded. Exclusive instruction-file creation
  receipts now prevent sandbox reservations being reported as project edits.
  Tasks without established completion retain their original status; passing
  checks are no longer described as failed merely because no edits occurred.
  Fourteen broader runtime fixture failures also
  reproduce on untouched Desktop HEAD; this is not an all-app-tests pass.

After the full Windows run the ACL database contained zero working ACEs and
zero holders, with no pending execution journal. The Desktop fixture verified
shutdown and journal absence before removing its workspace. These are local
candidate results, not an installed production-app or cross-Windows release
qualification.

The dedicated-account AppContainer experiment is disabled by default. Earlier
isolated Schannel success does not qualify the current containment policy: the
predecessor namespace grant followed a Global alias into a shared directory and
was removed. Current disposable tests pass original lowbox station/desktop
mutation boundaries, but offline Schannel acquisition and provider lookup fail
while identical raw-account controls pass. Public HTTPS, installed activation
and the final application matrix remain unqualified. See `ACCOUNT_CANDIDATE.md`
for exact current evidence and the separately qualified lifecycle checks.

A separate disposable diagnostic exposed the deleted-workspace recovery gap:
after timeout, its fixture was removed before recovery. The exact synthetic
fixture was recovered with a recorded provenance audit and state backups.
This one-time cleanup is **not** a general recovery mechanism or passing release
test. The helper continues to refuse ambiguous file-ID lookup failures and
retains the recovery journal.

The pre-AppContainer release's 18 passing tests missed the DNS-service bypass.
Protocol 3 prevents this engine from silently using older helpers. The
DNS regression requires a successful host control and proof that the sandboxed
PowerShell command ran before accepting a blocked query.

The opt-in `installed` workflow-dispatch input in
`.github/workflows/windows-native-sandbox.yml` provisions disposable Windows
2022/2025 runners, tests the release build, attempts recovery/uninstallation and
uploads the test report. This workflow has been structurally checked locally;
no remote CI run has been claimed.

The expanded tests first reproduced two bypasses using disposable canaries:
writing an outside file through a workspace hardlink, and writing Git metadata
whose explicit public allow outranked an inherited deny. The current build
refuses those executions; both regression cases pass. The original failing
run is preserved locally in `target/adversarial-before.xml`.

The instruction-file tests also reproduced creation of absent workspace-root
`AGENTS.md` and `CLAUDE.md` before the policy fix. The failing evidence is in
`target/instruction-files-before.xml`; all four absent/existing cases now pass.
New hardlinks to protected Git metadata and private-target junction attempts
were denied. A private-sibling hardlink canary also verified that the sandbox
cannot obtain read/write access and cleanup preserves the private target DACL.
Ordinary hardlinks and host-created junction controls succeeded;
the private canary contents and security descriptors remained unchanged.

Seven live WSL tests also passed on Ubuntu 22.04, WSL2 kernel
`6.6.87.2-microsoft-standard-WSL2`, Bubblewrap 0.6.1. They exercised both Linux
temporary storage and Windows-mounted DrvFS workspaces: writes, read-only Git
metadata, hidden private siblings/Windows executables, secret filtering,
separate network namespaces and refusal to select the Windows helper from WSL.
The current 0.14.1 run passed all seven checks as part of the 134-test WSL
run above, using dependencies synchronized from the current engine lockfile.
An earlier DrvFS stall was cleared by an operator-authorized restart of
**Ubuntu-22.04 only**; Windows was not restarted. These checks still do not
constitute the full WSL interoperability/escape matrix.

The validation host's protection refresh completed with UAC consent. All 7
inspectable system-directory protections are current. The remaining 4 system
paths cannot be inspected by the normal user; live sandbox-account permission
probes verify enforcement on all recorded paths before launch. The real CLI's
`sandbox doctor --smoke` passed with status `experimental_ready`. This means
ready for preview testing, not completion of the remaining release matrix.

The locally built release bundle is under `target/bundle/release`, with its SHA-256
recorded in the adjacent manifest. It is a local developer build, not a signed
production distribution; the packaged desktop app still does not activate it.

```powershell
cargo test --manifest-path native/windows-sandbox/Cargo.toml --locked
cargo test --manifest-path native/windows-sandbox/vendor/srt-win/Cargo.toml --lib --locked
python -m pytest tests/test_windows_sandbox.py
```

The native token-construction test must run under a normal Windows user token;
nesting it under another application's restricted token can produce error 87.
Do not weaken the production token policy to make that test pass.

After explicit setup in a test VM:

```powershell
$env:ALYSIS_RUN_INSTALLED_SANDBOX_TESTS = '1'
python -m pytest tests/test_windows_sandbox_installed.py
```

The installed tests cover identity, workspace writes and host-readable output,
metadata writes/deletion/rename/alternate streams, host-secret environment
filtering, read-only mode, private sibling reads, hardlink/junction refusal,
explicit child ACL bypass refusal, host-process memory access, direct IPv4/IPv6
TCP and UDP, background stop, concurrent execution rejection, timeouts, engine
exit, descendant termination and recovery. They are skipped by default and are
only a subset of the release matrix above.

To additionally test public HTTPS with a fixed anonymous GET to the npm registry:

```powershell
.\scripts\test_windows_sandbox.ps1 -Python python -Installed -Internet
```

Without `-Internet`, external HTTPS tests are skipped even if an inherited test
environment requests them. The script restores the original environment on exit.
The `internet` workflow-dispatch input provides the same opt-in on disposable CI
runners; neither workflow input changes the product's default network policy.

Repeat the Python regressions, real CLI smoke and installed Windows checks from
the engine repository in a normal PowerShell terminal (setup and release build
must already exist; this command does not provision the machine):

```powershell
.\scripts\test_windows_sandbox.ps1 -Python python -Installed
```

Use `-Python <path-to-python.exe>` for a test environment with the repository's
dependencies. Omit `-Installed` for regression tests without live execution.
The script restores its environment variables and original directory on exit.
Installed results are written to `target/installed-release-tests.xml`.

For the opt-in WSL smoke suite, run inside WSL from the engine repository:

```bash
ALYSIS_RUN_INSTALLED_WSL_TESTS=1 .venv/bin/python -m pytest tests/test_wsl_sandbox_installed.py -q
```

For a strict local compatibility gate, add `-RequireAllChecks` to the installed
internet test command. It fails if any installed test is skipped or expected to
fail. The current release intentionally fails that gate because Schannel HTTPS
is unresolved; an ordinary pytest success with an xfail is not release evidence.

Before upgrading from the earlier preview, recover any pending execution with
its original helper. Journal version 1 cannot prove named-job termination and is
refused by the new recovery path; do not delete it to bypass the check.


### Baseline ledger qualification, October 5

The latest local helper at source `74827e678` uses verified WAL/FULL commits
and refuses unknown or damaged ACL history without renaming, scrubbing or
recreating it. Requested and physical file/parent identities, both ancestor
chains and sidecar identity agreement support Windows package redirection
without admitting junctions or hardlinked main files. See [recovery guidance](RECOVERY.md)
for the admission-only pin lifetime, SHM coordination and unsupported migration
limits. A process-exit WAL test is not a power-loss or reboot qualification.

The default native suite passed **119 tests** (5 ignored); the candidate suite
passed **284** (8 ignored), with overlapping counts. The exact release helper
`eab5479372dc830ae65c2903615adb5d30054c081ab6640264b48bb667d89648`
passed all **24** selected installed boundary/recovery/Node checks without
skips or expected failures. The fresh Desktop approval/native matrix passed,
including four normal Node subprocess verifications, 555-file source/wheel/
installed parity and a 274-UTF16-unit protected path. Cleanup was empty and
normal configuration unchanged. The initial packaged-path refusal and all
prior helper bundles remain preserved in the qualification evidence.

This is a local candidate qualification. Public HTTPS/Schannel, full resource
and descendant recovery, hosted launch compatibility, broader platform/GUI/
WSL/security coverage and signed distribution remain release gates.
