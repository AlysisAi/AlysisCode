# Recovery after moved or deleted grant targets

Recovery first verifies that the recorded engine/broker and contained job have
stopped. It then removes the execution's temporary ACL changes. An unfinished
execution journal continues to block new sandbox execution until every required
cleanup operation succeeds. Do not delete the journal to bypass this check.

## Confirming child exit before cleanup

The armed child guard now requests termination and waits up to thirty seconds
for its original process handle to become signaled before releasing handles or
returning to outer cleanup. A successful termination request alone is insufficient:
[Windows termination is asynchronous](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-terminateprocess).
A failed request is accepted only if that same retained handle already proves
exit. Neither an error code nor an exit-code value such as 259 proves liveness
or death. Normal execution also rejects an unsuccessful process wait.

If exit remains unverified, the helper exits with code 125 without unwinding
into profile or permission cleanup. Diagnostic write failures are ignored in
the child, job and candidate job-exit guards so a broken stderr pipe cannot
panic and run outer destructors. Pending recovery state remains authoritative.

Local regressions reproduced both the earlier asynchronous return and a broken
diagnostic pipe running outer cleanup. The corrected tests cover suspended
children, already-exited processes, wait timeouts, denied termination and invalid
handles, with subprocess sentinels checking that cleanup did not run. One ignored
fixture is deliberately invoked by its bounded parent test. Separate job-based
descendant containment remains required. This guard does not solve external
broker death before runner assignment, authorize profile ownership after a
restart or enable the account candidate.

## ACL ledger admission and durability

The baseline `state.db` is recovery authority, even when its version or contents
are not understood by the current helper. Existing empty, corrupt, unsupported
or structurally different databases are refused. The helper does not rename
them, scrub credential-era rows, recreate missing tables, or replace them with
an empty ledger. Orphaned SQLite sidecars also block creation. Preserve the main
file and its `-wal`, `-shm` and `-journal` files together; deleting them is not a
supported recovery procedure. No automatic migration or repair is provided.

New ledgers require exclusive file creation. Existing ledgers are checked
through SQLite read-only admission before a writable connection, then checked
again on that connection. Exact schema validation includes indexes, triggers,
conflict actions and schema version. Local path and file-identity checks reject
reparse aliases and hardlinked main files. Ancestor pins require directory listing
access and last through admission; custom traverse-only ancestor ACLs may refuse
admission. Windows package redirection is admitted only when both spellings
identify the same main file and parent, keep the same leaf filename, and agree
on sidecar identities. Both requested and physical canonical ancestors are
pinned and rechecked; transparent redirection is not a reparse exception.
SQLite may coordinate or rebuild its
shared-memory sidecar during read-only admission; this is not a promise that
every sidecar byte remains immutable. It must not discard committed recovery
history on refusal.

Writable connections require verified WAL mode, `synchronous=FULL` and enabled
foreign keys before permission effects. FULL requests SQLite's per-commit WAL
synchronization; it does not make the SQLite commit and Windows ACL update one
atomic operation or qualify storage hardware against power loss. The existing
record-before-effect and recovery ordering still applies. Process-exit tests
exercise committed WAL reopen, not a Windows reboot or a physical power cut.

## Partial deny-target placeholders

When a protected target is missing, the helper records each component's intent
before creating permanent empty placeholders. If recording or creation fails,
policy application still fails and execution is refused. Previously created
components and their recorded intent remain in place; there is no rollback by
remembered pathname. A directory may have been moved and replaced between its
creation and an error, so that old name cannot authorize deletion. The same
leave-in-place policy applies when ACLs are later restored.

Creation now validates the complete lexical input, retains the local volume root
and opens each component relative to its pinned parent. Each child is opened
without following its final reparse point; reparse objects, hardlinked files and
delete-pending objects are refused before descent. Below the volume root, actual
directory list/data access and no write/delete sharing block delete opens and
the tested generic directory-write handles for the retained lifetime. A conflicting
directory writer or insufficient list/read access causes refusal. Metadata-only
write access can still convert an empty pinned directory into a junction, so
sharing alone is not a reparse exclusion guarantee. Intent precedes
exclusive creation; a collision is reopened and verified without truncation or
claiming that object as newly created. See Microsoft's [NtCreateFile contract](https://learn.microsoft.com/en-us/windows/win32/api/winternl/nf-winternl-ntcreatefile).

Readback retains the actual canonical spelling. Valid Unicode case variants of
existing ancestors use [Windows ordinal comparison](https://learn.microsoft.com/en-us/windows/win32/api/stringapiset/nf-stringapiset-comparestringordinal);
ASCII folding and linguistic normalization are not used. A collision that changes
the spelling already recorded for creation is refused.

Deterministic tests cover existing and raced junctions, blocked ancestor mutation,
lock release on error, preserved collision bytes, hardlink refusal, Unicode case
variants and extended long paths. Production policy application carries the
original placeholder handles through target collection and all ACL writes and
rollback, checking them before and after application. A regression attempts a
real ancestor rename at the previous creation-to-ACL gap; the retained handle
blocks it, and dropping the guard releases it. Later batch errors release the
handles while preserving partial objects and their intent records.

Three real metadata-mutation regressions exercise the narrower access rights
described by Microsoft's [reparse-point protocol](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-fsa/4aeefef8-92c3-4abc-af7a-a610caf8a165).
The mutation succeeds with `FILE_WRITE_ATTRIBUTES` while the original pin is
held; the post-intent check then refuses it. A mutation placed after the last
user-mode verification is refused by the actual relative child operation, with
no object created outside the original chain. Changing a retained empty leaf
also fails guard verification before ACL admission. These are local NTFS
observations, not a promise that pins prevent arbitrary metadata or privileged
mutation. Production operations are unchanged by these added tests.

The public creation-only convenience API still ends its pins at return. Existing
targets that did not go through missing-target creation are outside this guard's
scope. These handles are not kept throughout execution and do not make the ACL
and database writes atomic or qualify descendant cleanup after execution.

## Supported recovery

- If the original target still exists at its recorded path, its captured volume
  serial and file ID must match before its permissions are changed.
- If that same object was moved on the same mounted volume, recovery can locate
  it by ID and verify the identity again before removing its grant. If Windows
  cannot locate it, returning the **same object** to its recorded path can allow
  recovery. A new file or directory with the old name has a different identity
  and cannot substitute for it.
- If the recorded volume is unavailable, reconnect that same volume and retry.
  An inaccessible or unmounted volume is not evidence that an object was deleted.
- A definitive file/path-not-found response from the ID lookup proves absence
  of that object only. Baseline object-only traversal grants may then retire.
  Recursive read/modify grants retain their recovery record: a missing root
  does not prove that moved descendants lost inherited access. A successful
  lookup whose full identity differs is rejected.

## Known limitation: deleted NTFS objects

An NTFS object can outlive its pathname while another process has an open handle.
Its final name may be under NTFS's internal deleted-object directory. The helper
checks the object's link count and deletion state; it does not interpret that
internal pathname as proof of deletion. Closing applications that still hold
the object permits deletion to complete, but does not guarantee recovery will
then succeed.

After all handles close, Windows on the tested system returns
`ERROR_INVALID_PARAMETER` (87) when opening the deleted ID. The same response is
possible for invalid or unsupported requests. The helper therefore reports
**unverifiable identity**, preserves the journal, and stops. It tries the 128-bit
ID form and, only when representable without truncation, one 64-bit compatibility
retry. It does not poll indefinitely, search the drive recursively, use the old
pathname's replacement, or treat a generic error 87 as successful cleanup.

This is a current recovery availability limitation: if a tracked grant target is
deleted before an unfinished execution is cleaned up, automatic recovery may
remain blocked. There is no
supported command in this build that clears this ambiguity. A future privileged
NTFS identity-repair path is required; merely running the existing recovery
command as Administrator does not implement that missing proof. Retaining
verified handles through ordinary cleanup could prevent some cases, but would
not alone survive a broker crash.

The doctor's temporary smoke directory is now retained when the command fails,
is interrupted, leaves a journal, or journal inspection fails. Successful smoke
cleanup verifies the exact result and directory identity first. This prevents
the doctor from creating the deleted-object failure, but does not resolve an
already ambiguous recovery journal.

## Root absence does not establish recursive cleanup

The baseline ACL ledger records each granted root's identity, not every object
that inherited its read/modify grant. A child moved elsewhere on the same NTFS
volume can retain that inherited ACE even after the old root is deleted. Both
ordinary release and orphan recovery therefore refuse to retire a recursive
grant on an ID-level not-found result. The failed transaction retains the exact
working row and, for ordinary release, its holder. The diagnostic explicitly
identifies the missing descendant identity inventory. Object-only `traverse`
grants do not propagate and retain their previous definitive-absence behavior.
The old ledger does not record whether a vanished read/modify target was a file
or directory, so this conservative refusal also applies to deleted file grants.
An ID lookup that finds the original moved object still follows the existing
identity-checked revocation path; replacement objects remain untouched.

Four focused native regressions passed on October 4. Disposable fixtures under
`.qa-results/0142-integration/recovery-fixtures` used a synthetic SID and an
in-memory SQLite ledger, never the installed sandbox database or helper. They
proved successful revocation from the exact moved file/tree, unchanged
replacement DACLs, and retention of a moved child's grant after deleting its
original root. Both ordinary and orphan cleanup preserved the real captured
file-ID record when NTFS returned error 87; ordinary cleanup also retained its
holder. Evidence is in
`.qa-results/0142-integration/native-deleted-grant-recovery.log`.
The full pinned Rust 1.90.0 candidate suite then passed **220 tests** with four
manual probes ignored; the default-feature baseline passed **86 tests** with
two manual probes ignored. Those logs are `native-deleted-grant-full.log` and
`native-deleted-grant-baseline.log` in the same evidence directory.

This closes an unsafe success case; it does not make ambiguous deleted-ID
recovery available or complete descendant recovery for the baseline recursive
ACL model. A child moved out while its original root still exists is another
unresolved case: revoking at that live root cannot find the detached child.
The disabled explicit-object candidate below addresses a different
journal design and is not enabled by this correction.

## File-record inspection assessment

The documented `FSCTL_GET_NTFS_FILE_RECORD` operation returns an in-use file record
at or below the requested record number. NTFS 3.0/3.1 records also carry a sequence
number that must match the captured file reference. A future repair tool could
use the exact mounted volume serial, supported NTFS version, record number and
sequence to distinguish a removed/reused object from an unverifiable lookup.
It must validate returned lengths, record header and sequence, and preserve the
journal on access failures or unsupported layouts. It must never infer absence
from an unrelated record without establishing the documented lookup semantics.

A disposable-file probe on this Windows installation used a non-elevated user
token. The volume query succeeded and confirmed matching serial, NTFS 3.1 and a
1024-byte record size. File-record queries were denied with error 5 for ordinary
directory handles; read access to the raw volume was also denied. Consequently
no MFT-based automatic repair or automatic elevation has been added to recovery.
The probe inspected only test-file identity metadata and changed no installed
sandbox state.

A separate [NTFS evidence module](vendor/srt-win/src/ntfs_identity_evidence.md)
now parses strictly checked driver responses and offers a bounded read-only
probe for future privileged qualification. Its results are observations, never
cleanup authority; baseline recovery does not call it. Six synthetic tests
passed and the ordinary-user live probe confirmed access denial. Privileged
live, moved, pending-deletion and deleted-object results remain unqualified.
There is a second integration requirement: proving a granted root was deleted
does not prove that moved descendants carrying inherited grants were cleaned.
The current root-only journal cannot retire all descendant state from that
observation alone.

Primary references:

- [OpenFileById](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-openfilebyid)
- [FSCTL_GET_NTFS_FILE_RECORD](https://learn.microsoft.com/en-us/windows/win32/api/winioctl/ni-winioctl-fsctl_get_ntfs_file_record)
- [NTFS volume data and version](https://learn.microsoft.com/en-us/windows/win32/api/winioctl/ns-winioctl-ntfs_volume_data_buffer)
- [File record header and sequence](https://learn.microsoft.com/en-us/windows/win32/devnotes/file-record-segment-header)
- [MFT segment reference](https://learn.microsoft.com/en-us/windows/win32/devnotes/mft-segment-reference)

## Disabled explicit-object read-tree prototype

The `account-appcontainer-candidate` feature now contains a separate
[`acl::object_grants`](vendor/srt-win/src/object_grants.rs) implementation and
candidate database journal. The baseline runner and its recursive ACL recovery
remain unchanged. This prototype must not be described as qualification of
writable workspace grants or deleted-ID recovery.

Preparation opens and retains an identity-checked handle for every existing
object, refuses reparse points, name aliases and multiply linked files, and
bounds the inventory to 1,024 objects, 64 path components and 8 MiB of journal
records. The per-object transaction records the full 128-bit file ID and volume
serial, exact before/added ACE bytes, owner and DACL control before the first ACL
change. Its database header and execution identity bind the installation, owner,
selected account, group and derived unique package SID.

Each object receives only non-inheriting read/execute ACEs. The candidate uses
[`NtSetSecurityObject`](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/ntifs/nf-ntifs-ntsetsecurityobject)
on the retained object handle with `READ_CONTROL`, `WRITE_DAC`, data/list and
attribute access. Microsoft documents the native API's user-mode entry point
and handle authority. This avoids the broader `DELETE` access requested by
`MAXIMUM_ALLOWED`, which conflicts with ordinary cwd handles. The data/list
handle still denies delete sharing, so it pins the admitted object.

This is a deliberately narrow setter: owner, DACL control and every inherited
or inheritable ACE must remain identical. It accepts only changes to explicit,
non-inheriting ACEs; journal admission further restricts these to the exact
account/package read grants. Every unrelated ACE and relevant control bit
must match target readback. No path setter or descriptor-control fallback is
attempted. `SetKernelObjectSecurity` is not used; its documentation explicitly
excludes filesystem objects.

An isolated normal-user Windows probe exercised exact set/restoration and
unchanged existing children for protected, inherited, nonautomatic and actual
process-cwd fixtures. A separate changed-inheritance control showed that
`SetSecurityInfo` propagated a new inheritable SID while the native setter left
the child's descriptor byte-for-byte unchanged. Preserving an already recorded
`AUTO_INHERITED` bit requires also supplying `AUTO_INHERIT_REQ` in the native
input descriptor; Windows consumes that request and exact target readback must
match the original control. Merely copying `AUTO_INHERITED` loses the bit and
was rejected by the regression suite. The changed-inheritance control is
**not** support for changing inheritable entries. The candidate
rejects such changes before calling the API. These observations still require
qualification across the supported Windows/NTFS matrix.

The prototype inserts new explicit ACEs after all original
explicit ACEs, refuses inherited denies/unsupported inherited ACE types and
refuses noncanonical explicit/inherited ordering. It preserves the order and
bytes of all existing ACEs rather than overriding an inherited denial to force
access to succeed. `DEFAULTED`/`AUTO_INHERIT_REQ` control states remain refused
before mutation. Nonautomatic unprotected descriptors are passed through with
the same exact-control/readback requirement. Other descriptor drift preserves
the per-object intent.

Objects created after inventory receive none of the prototype's grants. Existing
permissions from other principals/capabilities remain unchanged; this statement
is about added grants, not a guarantee that every later object is unreadable.
The tree guard pins existing names until dropped, and must be retained through
execution. Independent recovery reopens and verifies the recorded objects with
the same minimum authority. Any host access needed for account-owned output is outside
this read-only implementation; it never adds a permanent owner grant.

Cleanup requires typed stopped-execution authority and checks the execution job
is empty before processing every recorded object independently. A moved object
is located using the existing
checked full-ID lookup; a replacement at the old path is never changed. New
hardlinks do not authorize an additional grant, but cleanup can remove the
recorded grant through a verified alias of that exact object. Descriptor drift
is preserved for explicit repair rather than overwritten. Rows retire only
after exact readback, an already-restored descriptor, or definitive checked-ID
absence for that individual object. Error 87 and unavailable volumes remain
errors. Root absence cannot retire a surviving descendant's row.

The candidate-only [`account_state::execution`](vendor/srt-win/src/account_execution.rs)
gate now publishes a private, immutable version-3 pending file before it issues
live grant authority. It binds the broker PID and creation time, derived named
job/package SID, full account/install identity, a random grant transaction,
the actual candidate database FileId, and its own FileId. The database run row
persists that entire record before any ACL mutation. Normal cleanup consumes
live authority; crash cleanup verifies the exact broker has exited (including
PID reuse) and the named job is empty. Only complete, identity-matched object
recovery returns the opaque receipt that permits same-handle pending-file
deletion. Drops, partial records, mismatched database/marker identities, SQL
durability changes and failed ACL recovery preserve pending state.

This gate uses exactly the baseline `LOCALAPPDATA\\alysis-sandbox\\execution-pending.json`
name and the same global lease. The environment-derived path must equal the OS
KnownFolder path. Every existing ancestor must pass conservative owner/write
admission and remain pinned; the gate creates neither an alternate store nor
the shared parent. The pending file is born with the exact owner/SYSTEM/admin
policy denying both candidate accounts and their group. Baseline maintenance
refuses a pending marker, and baseline recovery refuses its version 3. The
historically observed unknown-capability write grant in this host's LocalAppData
ancestry remains a compatibility refusal; no capability was whitelisted to
force this path to work. Isolated fixture tests deliberately bypass production
ancestor provenance and installed setup reads, not the file or journal mechanics.

The durable scope is explicitly **read ACLs only**. Candidate profile/namespace
creation, typed credential launch and complete resource teardown are not yet
integrated with this gate. No CLI selection or executable candidate runner is
enabled by this patch. The private candidate database opener admits clean state
only, so hot-journal/power-loss recovery and
privileged NTFS absence evidence still need separate integration/qualification.
Host-owner and administrator processes remain trusted. In particular this API
cannot revoke their historically opened handles or atomically coordinate their
concurrent DACL edits. A persistent dedicated account does not by itself solve
writable new-object discovery or grant revocation for descendants moved outside
the scanned tree.

Moving the authoritative pending record to the attested ProgramFiles store
requires an explicit legacy migration protocol. Invalidating the baseline
version-3 setup marker blocks subsequent credential reads, but historical raw
`exec` did not acquire the global lease and can already hold decrypted
credentials or a logged-on token. Marker invalidation or password rotation does
not revoke an existing token. A migration must prove all legacy brokers,
tokens and jobs are quiescent, retire/rotate credential authority as needed,
durably invalidate old setup readiness before candidate readiness, and add a
cross-store gate to new baseline code. An administrator deliberately repairing
the old installation is a trusted administrative override. None of that machine
migration is performed here, and the strict shared path is retained meanwhile.

The complete normal-host native library suite with the disabled candidate
compiled under Rust 1.90.0 passed 189 tests, with four manual probes ignored.
It includes 15 object-grant tests, the shared execution-journal tests, six
full-resource ledger tests, nine activation tests and 13 WFP writer tests.
These are isolated mechanics/policy tests, not installed candidate qualification.
Evidence is `.qa-results/0142-integration/native-candidate-final-followup.log`.

Primary API references:

- [SetSecurityInfo: handle identity, MAXIMUM_ALLOWED and child propagation](https://learn.microsoft.com/en-us/windows/win32/api/aclapi/nf-aclapi-setsecurityinfo)
- [SetKernelObjectSecurity: filesystem exclusion](https://learn.microsoft.com/en-us/windows/win32/api/securitybaseapi/nf-securitybaseapi-setkernelobjectsecurity)
- [Sequential ACE evaluation and deny precedence](https://learn.microsoft.com/en-us/windows/win32/secauthz/how-dacls-control-access-to-an-object)
- [EnumProcesses: complete bounded PID enumeration](https://learn.microsoft.com/en-us/windows/win32/api/psapi/nf-psapi-enumprocesses)
- [SetFileInformationByHandle: delete the held file object](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-setfileinformationbyhandle)

## Disabled full-resource journal prerequisite

The candidate now has a separate `resource-plan-v1` pending scope and a bounded,
append-only [resource ledger](vendor/srt-win/src/account_resource_journal.rs).
An ACL-only completion receipt cannot retire this scope. The private ledger
also requires complete resource teardown before opening ACL recovery for this
scope; child-job emptiness alone cannot authorize it. The ledger binds the
complete immutable execution record and original logon-session ID,
then commits a resource intent before recording successful creation. Resource
names and subjects derive from the bound execution; arbitrary names/paths are
not accepted. Profile observations include folder FileIds; process observations
include PID, creation time, executable FileId and the exact named job. Replayed
events must be contiguous, bounded and consistent with the complete identity.

The fixed inventory includes host/account profiles, private namespace and keeper,
suspended bootstrap, broker/child jobs and processes, desktops, and shared
WinSta0/BaseNamedObjects ACL edits. Shared ACL edits require before/after bytes
in the write-ahead intent, but no adapter yet consumes those descriptors. Process
quiescence must precede non-process teardown, jobs cannot retire before their
recorded process, and the host profile outlives every other resource. A create
or remove intent lacking its exact completion observation remains unresolved.
The ledger neither adopts a colliding object nor repeats an ambiguous destructive
operation. Completed audit rows remain present; bounded archival/retirement of
that history before a subsequent run is still an integration requirement.

This is a durable state-machine prerequisite, **not an executable full-resource
backend**. No native effect adapter or arbitrary success-callback API is exposed.
`ReadyInstallation::launch` remains unavailable before credential access. The
identity-only `cleanup_account_profile` route is likewise internal and refuses
before credential access; it cannot bypass profile ownership or full quiescence.
The remaining adapter requirements are concrete:

- `CreateAppContainerProfile` creates a per-user data store and reports an
  existing store as a collision. It returns a package SID, not a durable unique
  generation handle. If the broker dies after creation but before recording its
  observation, name/SID/randomness alone cannot establish ownership. Preserve
  that intent for explicit proof or repair. Profile deletion also needs an
  identity-aware account-side adapter; success is not proof that an earlier
  unrecorded creation belonged to this execution.
- The current account runner creates and removes its profile internally, while
  the broker receives only an exit code and stdout shared with command output.
  A dedicated authenticated control channel must carry execution/operation
  nonces and exact resource observations. The child must not inherit that
  channel; the broker must commit observations before acknowledging progress.
  Account-profile teardown occurs after all inventoried processes stop, so its
  adapter must use a constrained account impersonation operation or separately
  inventory a cleanup runner. Spawning the existing cleanup command outside
  the ledger would leave another untracked process and is not admitted.
- The existing credential launch uses `CreateProcessWithLogonW`, then assigns
  its suspended runner to an unnamed broker job. Broker death between those
  steps leaves a process whose identity and job membership were not committed.
  Named, crash-safe runner birth/containment and bounded verification are needed
  before namespace/profile/ACL teardown can rely on runner quiescence.
- Namespace bootstrap also creates transient process/job/desktop resources, and
  the existing logon adapter adds shared station/BaseNamedObjects rights. Those
  effects need actual handle-bound ownership, durable intent and ordered cleanup
  adapters, or a reviewed design that avoids the shared mutation entirely.

No real profile, account, namespace, child process, kernel ACL or machine setup
is modified by the ledger's synthetic tests. The shared AppData admission and
legacy migration/quiescence requirements above are unchanged.

Primary lifecycle references:

- [CreateAppContainerProfile: per-user creation and existing-store result](https://learn.microsoft.com/en-us/windows/win32/api/userenv/nf-userenv-createappcontainerprofile)
- [DeleteAppContainerProfile: per-user scope and partial-failure semantics](https://learn.microsoft.com/en-us/windows/win32/api/userenv/nf-userenv-deleteappcontainerprofile)
- [CreateProcessWithLogonW: suspended process, profile and startup-information contract](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-createprocesswithlogonw)

## Disabled account control transport

The private candidate-only [control transport](vendor/srt-win/src/account_control.rs)
provides a separate duplex named pipe for later broker/runner lifecycle messages.
It is not connected to the runner entry point and does not enable execution.
The server creates one exclusive local instance using a fresh OS-random identity;
neither side retries a name or adopts a replacement. Its protected DACL grants
the distinct account SID only read/write data and synchronization. The account
does not receive create-instance, change-owner or change-DACL access. The broker
owner and SYSTEM remain trusted. Handles are noninheritable and client SQOS is
identification-only.

Each endpoint retains a duplicate of the original peer process reference from
the trusted launcher. Native pipe endpoint PID checks must match that still-live
original object before and after each message. The channel never opens a peer
by PID or accepts a message as process authority. This only establishes endpoint
identity and liveness: authenticated image/token/job validation and secure
cross-account handle/address delivery remain launcher requirements. The local
subprocess test uses two processes of the same user, not installed account
qualification.

Messages bind direction, channel identity and contiguous per-direction sequence.
Each body is at most 64 KiB, with 128 messages and 1 MiB per direction. A whole
frame shares one bounded deadline; partial header/body progress cannot extend it.
Malformed input, peer exit, impersonation, inheritable-handle drift and I/O
failures permanently refuse further operations, including after the immediate
condition is restored. Overlapped cancellation retains every buffer, event and
operation until completion. If cancellation cannot be proven complete within
three seconds, the helper exits without unwinding into resource cleanup.

These are transport guarantees, not durable resource acknowledgments. Payloads
remain untrusted. Integration must bind the complete execution and operation,
validate observations, commit the resource journal before acknowledging progress,
and keep this channel out of every untrusted child. Crash-safe runner birth,
execution-capable profiles, writer quiescence and recovery remain separate gates.

Twelve new local regression groups cover real separate-process exchange, wrong
and exited peers, exclusive creation, actual pipe DACLs, malformed/replayed
frames, partial streams, bounded cancellation and cumulative limits. A native
test first reproduced handle-verification failure leaving the channel retryable;
the corrected channel becomes unusable before verification starts. The complete
candidate library passed 401 tests with 14 ignored entries. One new ignored test
is a bounded subprocess fixture explicitly run by its passing parent. Candidate
and default non-test helper checks also pass. No account/profile provisioning,
installed-helper replacement, live model call or new CI dispatch was needed.

Primary transport references:

- [Named pipe security and the create-instance bit in generic write](https://learn.microsoft.com/en-us/windows/win32/ipc/named-pipe-security-and-access-rights)
- [GetNamedPipeClientProcessId](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-getnamedpipeclientprocessid)
- [CancelIoEx completion and storage lifetime](https://learn.microsoft.com/en-us/windows/win32/api/ioapiset/nf-ioapiset-cancelioex)

### First journal-backed acknowledgment

The retained empty-bootstrap-job adapter now has a narrow
[control exchange](vendor/srt-win/src/account_resource_control.rs). The request
must match the complete execution record, session and original creation nonce.
It cannot ask for another effect or supply an observation. The adapter verifies
its retained native job, commits and reads back the exact birth event, rechecks
the native guard, then sends the acknowledgment. Client-side validation requires
the same operation, event position and empty-job policy observation. It returns
message confirmation only, never a native ownership or retirement receipt.

Any request, guard, journal or reply failure closes the protocol endpoint. A
failed commit leaves the existing intent; failure after commit or a lost reply
preserves the birth record and original guard. Neither case repeats creation or
rolls back native state. Each session permits one confirmation. A newly constructed
session still requires the original native guard and exact journal; serialized
messages cannot restore ownership after a crash.

Nine new local groups exercise these boundaries using real pipes, empty jobs and
synthetic databases. A separate SQLite connection observes the committed birth
before the client consumes its acknowledgment. Failures before commit and after
commit/readback, native policy drift, missing commit, stopped authority, wrong
guards, malformed messages, peer disconnection and timeout are covered. The full
local native suite passes 410 tests, with the same 14 ignored entries, and both
helper configurations compile. This proves the narrow empty-job exchange, not
cross-account execution, power-loss durability or the remaining resource
adapters. The runner entry gate remains closed.

### Peer primary-token continuity

Every private control peer now retains a query-only, noninheritable reference to
its original primary token as well as its original process. Native observations
bind the token object, logon identity, modification identity, group/privilege
counts, user, Windows session and AppContainer state. Before and after messages,
the transport checks both the retained token and the current primary token of
that same process. A changed object or observation, failed query or inherited
handle refuses the channel permanently. No PID lookup, impersonation, privilege
enablement or token supplied by a message is used to perform these checks.

The candidate-specific constructor additionally selects the account from
`ReadyInstallation`, requires the original `SpawnedChild`, and checks that the
peer is the selected raw account runner in the broker's Windows session. It does
not grant permission to launch, resume or clean up. The generic transport remains
usable for non-runner peers and carries no account-runner authority.

A native regression first reproduced a live process still being accepted after
its primary token was changed. Eight new local groups cover that mutation,
same-user token substitution, wrong account/session, impersonation tokens,
restored privilege state, handle inheritance drift and a post-connection change
that permanently blocks the next response. The two-process fixture still passes
with token checks. The full native suite passes 418 tests with the same 14 ignored
entries. Only disposable child or duplicate tokens are modified by the tests;
the mutation fixture verifies the host token is unchanged.

These are observations around communication, not an atomic token freeze or a
history of all primary-token replacements. They do not inspect the peer's thread
impersonation tokens, establish full privilege/group/DACL policy, authenticate
its executable, prove job containment or solve the original handle handoff.
Cross-account qualification and the complete launcher remain open. The account
execution entry gate remains closed.

Primary API references:

- [TOKEN_STATISTICS identity and modification fields](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-token_statistics)
- [GetTokenInformation](https://learn.microsoft.com/en-us/windows/win32/api/securitybaseapi/nf-securitybaseapi-gettokeninformation)
- [OpenProcessToken access requirements](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-openprocesstoken)

### Original control-peer job observations

Candidate runner admission now also requires a borrow of the original trusted
broker-owned `Job`. Before and after communication, the channel checks native
membership in that exact object, kill-on-close without either breakaway flag,
all eight UI restrictions, a noninheritable handle, and the exact protected
owner/SYSTEM-only DACL. Native queries must succeed with complete lengths. A
null job handle is rejected explicitly: Windows otherwise interprets it as
membership in any job, which cannot prove the intended containment boundary.
The generic non-runner transport still confers no account-runner authority.

The borrow survives through the channel and resource-confirmation protocol.
It does not duplicate or reopen the job handle, so observation does not add a
reference that can defer kill-on-last-close. These checks perform no job or
permission changes, no process assignment, and no privileged fallback. Any
failed channel observation permanently refuses both directions, even after
the changed setting is restored. Restoring a fixture for the test cannot
reopen the channel.

A preserved regression first demonstrated the old channel accepting a peer
after its job was changed to allow breakaway. Seven new local test groups cover
wrong or missing job identity, failed native queries, initial weaker policy,
job/UI/DACL/handle drift, exact owner/ACE comparison, observer handle lifetime,
guard-driven process termination and two-process refusal of outgoing and
incoming messages. The first focused run also exposed the null-job API behavior;
the corrected run passes 27 control tests with one existing subprocess-fixture
ignore. The full native suite passes 425 tests with the same 14 ignored entries.
Both candidate and default helper configurations compile. Tests create only
synthetic jobs and disposable same-user children; they do not modify the host's
containing job, installed accounts, profiles or normal configuration.

This qualifies current observations of one strict job, not an atomic freeze,
the whole nested-job chain, historical containment, executable trust or durable
birth/retirement authority. The same-user two-process fixture creates its child
atomically in a job; it does not qualify cross-account process creation. The
legacy breakaway runner job intentionally fails this stricter admission. A
complete launcher still needs trusted executable pinning before creation,
secure delivery of original handles, crash-safe account-runner birth and the
execution-capable resource lifecycle. All account launch gates remain closed.

Primary API references:

- [IsProcessInJob and null-job semantics](https://learn.microsoft.com/en-us/windows/win32/api/jobapi/nf-jobapi-isprocessinjob)
- [Job objects, breakaway and last-handle lifetime](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)
- [QueryInformationJobObject](https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-queryinformationjobobject)

### Pinned control-peer image observations

Candidate runner admission now additionally borrows an installed-identity-bound
image guard. Production selects the current helper executable, pins every lexical
parent without delete sharing, and opens the leaf without write or delete
sharing. Admission refuses untrusted mutation permissions, reparse points,
aliases, directories and multiple hardlinks. Repeated observations check the
original handles' identities, types, link counts, final names, inheritance and
owner/DACL snapshots. The process image name is queried through the original
process handle and must match the pinned image path. A failed channel observation
remains refused after restoration. No executable path is accepted from the wire,
environment or recovery journal, and no descriptor is repaired during admission.

Eight local groups cover replacement/write/delete/parent-rename attempts, an
already-open writer, mutation rights, hardlinks, an actual NTFS junction,
leaf/parent security changes, process-path mismatch and channel refusal after
image-handle inheritance drift. Both peers in the existing two-process exchange
now retain image guards. The full native suite passes 433 tests with 14 existing
ignored entries; both helper configurations compile.

The initial local run correctly refused the build executable because its DACL
granted another account write access. Qualification therefore runs a byte-identical
copy with a protected owner/SYSTEM/Administrators-only DACL in a synthetic fixture.
The build output is unchanged and the refusal remains preserved. The test-only
factory bypasses admission of unrelated host ancestors, but still pins/checks
their native identities and security snapshots and enforces leaf admission.
This is not qualification of the host's installation directory. Production
always admits the entire ancestor chain against the trusted-owner policy.

These observations do not prove that image preparation preceded process birth
or that a reported image pathname identifies the process's original mapped
section. The launcher must enforce pre-birth pinning and retain the pins through
termination. Publisher/distribution trust, loaded-DLL policy and process injection
defenses remain separate requirements. Installed account selection is compiled;
real cross-account image admission and handle delivery remain unqualified. All
account launch gates remain closed.

### Startup API probes and the next containment design

Bounded local probes used synthetic `LOGON_NETCREDENTIALS_ONLY` logons, never
resumed their target processes, and verified termination of created targets.
They created no installed account, SDK profile or normal-configuration change.
The first empty-environment controls failed with error 87 and are preserved as
fixture failures. With the default environment, ordinary suspended creation
succeeded, while extended startup/job-list creation failed with error 87.

A separate probe placed an owned helper subprocess inside a strict job with
kill-on-close, no breakaway and all UI restrictions before allowing it to make
the ordinary logon call. Its newly created target was observed in that exact
original job. The test used a temporary query reference for observation and
explicitly terminated and observed the whole owned job afterward; it does not
qualify production handle delivery or kill-on-last-close behavior.

This suggests an atomically job-bound trusted host bootstrap can make the
account logon call and have its child inherit the broker's strict job. A real
implementation must keep the only owning job handle with the broker, validate
original process handles before resume, preserve pre-birth image pins and durable
intents, and avoid creating nested jobs with UI restrictions. The existing
separate runner/child-job resource plan must not silently claim this new design
is already covered. Real separate-account inheritance, AppContainer composition,
broker-death behavior and resource lifecycle need disposable-host qualification.

- [QueryFullProcessImageNameW](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-queryfullprocessimagenamew)
- [CreateProcessWithLogonW](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-createprocesswithlogonw)
- [Nested job inheritance and UI restrictions](https://learn.microsoft.com/en-us/windows/win32/procthread/nested-jobs)

### Account-logon intent follow-up

The separate-account inheritance and held-call filtered-broker crash diagnostic
now pass on a disposable hosted VM, as recorded in `ACCOUNT_CANDIDATE.md` and
`qualification.json`. Its two-second event window is bounded evidence; it does
not establish arbitrary-late containment or full lifecycle recovery.

The new atomic-bootstrap journal separately records a singleton immutable
account-logon intent after the original bootstrap's durable readiness. It binds
the operation, original process/image identity, selected installation account
and credential generation. There is no password in this record. A missing or
changed schema, partial readiness, altered binding, duplicate preparation or
uncertain write refuses further preparation and preserves the state. Older
records are not migrated or adopted. The original live running-bootstrap guard
is required; a cold intent cannot reconstruct it or authorize a logon, resume,
credential delivery or cleanup.

Real local broker-crash tests cover the autocommit boundaries immediately before
and after this intent. Original bootstrap exit and exact cold marker/database
preservation are required. The existing interrupted-bootstrap-write tests still
refuse hot originals unchanged; a rollback copy remains a diagnostic oracle,
never operational recovery authority. Account-child creation and acknowledgment,
installed credential handoff, resource lifecycle and recovery remain unfinished and the
production account path remains disabled.

### Credential delivery receipt follow-up

The original live prepared-logon guard now supports a one-attempt authenticated
credential transfer. Its receipt records only the exact non-secret selection,
after the original bootstrap acknowledges possession. It is checked against the
immutable logon intent before any write and after commit. Neither a logon intent
nor a delivery receipt permits cold reconstruction, resending a password,
account-child creation/resume or cleanup. Unknown/missing schema and uncertain
results preserve pending state and consume the original live guard.

Synthetic local two-process tests cover discard, guard drop, absence of the
original execution lease, invalid timeouts, failed receipt writes and schema
drift before send, after acknowledgment and after commit. Original-bootstrap exit
and unchanged pending markers/intents are required. Forced broker death before
send, after acknowledgment and after committed receipt verifies exact cold
preservation with no observer job handle. A separate forced death inside the
receipt transaction leaves a real hot journal: production reopening refuses it
unchanged, and only an isolated diagnostic copy is rolled back to prove the
receipt did not commit. No operational repair or replay authority is inferred.

These tests do not read installed credentials, create accounts or SDK profiles,
or qualify production ancestor/account admission. At this milestone the bootstrap
accepted only secret discard after delivery. Account-child birth/commit/resume, full
resource teardown, Schannel execution and final Desktop/CLI/WSL qualification
remain outstanding; the candidate launch gate stays closed.

### Suspended account birth follow-up

The live job now supports a one-attempt account-birth operation that consumes
the entire original job. A bound immutable intent precedes logon; an original
native process/token/image/job observation precedes the birth receipt and exact
acknowledgment. The child remains suspended throughout. Success returns only
data after closing the job and verifying the original child's exit. Errors also
close the owning job, including errors after a successful or uncertain service
call; bootstrap-only termination cannot substitute for whole-job containment.

Birth rows preserve their exact credential selection, request nonce and native
identity. Missing or changed schema, duplicate requests and uncertain writes
are refused without replay or adoption. Cold records do not authorize another
logon, resume, cleanup or automatic SQLite rollback. Local synthetic failure
tests pass. Full resource retirement and operational cold recovery remain
outstanding; bounded real-account results are described below.

Real-account success and normal error teardown now pass in hosted run
37537957522: receipt write refusal, receipt schema drift, and post-acknowledgment
refusal all observe the original child's exit and preserve pending state. This
uses full image ancestry admission on both peers, fresh dedicated account/group
identities and a synthetic secret. It does not prove abrupt-death behavior.

Three hosted abrupt-death cuts cover the observed child before receipt,
acknowledged receipt, and an interrupted receipt transaction after dirty-page
flush. The external controller duplicates only the original query/synchronize
reference, terminates the original broker without Rust unwinding, and requires
child exit and job closure. A hot original must be refused unchanged on repeated
opens; an isolated copy checks SQLite rollback and the exact prior durable
prefix. These cuts grant no production recovery authority.

Hosted run **37538917623**, revision **422b0a6b2**, now passes all three account
broker-death cuts, alongside the success and normal-error cases. The original
account child exits after original-broker termination without Rust unwinding;
the controller holds no job handle. Clean cold prefixes remain exact. The hot
original is refused unchanged twice, and an isolated rollback copy passes
integrity checking and restores the exact prior one-row birth prefix. Both
fresh accounts are verified disabled afterward. This remains a bounded
disposable-host diagnostic, not automatic repair, account-child resume or
complete profile/resource quiescence and retirement authority.

### Original-job retirement intent and completion

The same live owning job now commits a separate stop intent after complete
account birth and acknowledgment. Its private ticket binds the original plan,
operation, account birth nonce and native PID/creation/image. Consuming that
ticket also consumes the original job. Completion is recorded only after the
job destructor has verified zero active processes, closed its handle and the
original account exit observer has signaled. A failed intent write still closes
the whole job. A failed completion write or readback leaves pending evidence
and returns no live completion guard.

The two immutable events are diagnostic data after reopening. They cannot
recreate the live guard, authorize profile deletion, restore shared permissions
or retire the pending execution marker. Hosted run **37540760502**, revision
**32d43cc64**, now passes successful retirement and three actual-account faults:
failed intent write, failed completion write and changed completion readback.
Each requires original child exit, original job closure and unchanged pending
evidence, with zero, one or two retirement rows respectively. Earlier birth
failure cases and all three abrupt broker-death cuts pass in the same run.
Both fixture accounts are verified disabled afterward. This remains a
prerequisite for resource composition, with no profile or ACL cleanup authority.

### Atomic namespace composition

The new composition holds the original empty namespace root and its parent in
the host broker across account birth and original-job retirement. Its four-row
immutable inventory is distinct from the legacy resource plan. Only the live
completion guard for the same plan can start explicit namespace retirement;
saved job or namespace rows cannot supply that guard. Both root identity and
policy are checked through retained handles, and only a successful original
close followed by verified absence can record removal. Extra open references,
unexpected children or changed policy preserve unresolved evidence. Job drain
precedes namespace handle release on errors as well as normal completion.

This adapter creates no profile and issues no profile/ACL cleanup receipt or
pending-marker retirement. Missing new schema is refused without migration.
Local 512-test regression and narrow source review pass. Actual-account hosted
run **37542466086**, revision **8816a1f5a**, passes eight namespace composition
cases covering success, failed birth/removal writes, an extra retained root,
account-birth refusal and failed job-retirement intent. Original child exit,
job closure, exact pending state and final native absence are required. This
is followed by successful run **37543005037**, revision **e7ded9789**, which
adds three namespaced abrupt broker-death cuts. The controller retains neither
job nor namespace ownership, observes original account exit and native namespace
absence after the kill, and preserves the exact two-row namespace birth prefix.
The hot original remains refused unchanged; only an isolated test copy performs
rollback and confirms that prefix. No removed receipt or cold cleanup authority
is fabricated from kernel absence, and both accounts are disabled afterward.

### Atomic station composition

The optional station grant keeps its original native station reference and
complete before/after owner/DACL snapshots. Its immutable four-row inventory
records intent before applying the exact noninheriting package grant. Explicit
restoration requires the live namespace-completion guard for the same plan,
which retains original namespace closure and original-job completion. Cold
rows cannot manufacture this guard. Descriptor drift is refused before mutation;
native writes and readbacks use the retained handle. A failed or uncertain write
preserves the grant and journal without retry or hidden rollback. Sequential
comparisons do not provide an atomic lock against unrelated trusted ACL edits.

Hosted run **37544607368**, revision **913cd9f1b**, passes success and four
failed-write positions, with a separate sentinel for real WinSta0 changes.
The earlier account/namespace cases pass, and both accounts are disabled
afterward. Post-grant refusal cases intentionally
leave a unique-package ACE unresolved on the disposable VM and verify its exact
state; they do not qualify cold repair. No profile or untrusted command runs.

Three further abrupt broker-death cuts with a live grant pass hosted run
**37545112845**, revision **9ef0f752a**. Their controller uses read-only station
snapshots, retains no station/job/namespace handle across termination, and
requires the exact grant to survive unchanged while the original child/job/
namespace exit. Cold journal inspection preserves the unresolved two-row station
prefix; hot originals are refused unchanged, with rollback confined to an
isolated copy. No restoration receipt or authority is reconstructed after death.
Earlier cases pass and both accounts are disabled afterward.

### Private desktop native prerequisite

The new private adapter uses create-only native desktop semantics under an
original pinned station reference. The documented Win32 create operation can
silently reopen an existing desktop, so successful Win32 creation is not used
as exclusive ownership proof. Native collision failure cannot adopt or modify
the existing desktop. Birth is private until the Low label is read back and
then only the intended package rights are published. Native closure consumes
the original reference once and requires relative absence; `Drop` only attempts
handle closure and issues no successful retirement receipt. All five native
checks pass Windows Server2022/2025 in run **37546500376**, revision **7a701fd35**.
No account process or command receives its desktop path.

The new unused-desktop composition records intent/created/close-intent/closed
in its own immutable bounded inventory, linked to the exact original plan and
station grant. Its close intent needs a private live same-plan job/namespace
completion guard; cold event strings cannot supply it. Original desktop closure,
verified absence and committed close readback precede station restoration.
Failed writes/extra references return no success and preserve pending state.
Local523-test regression, final eight focused checks, both build modes and a
narrow source review pass. Six actual-account hosted cases remain pending.
The composition performs no child attach, profile cleanup, launch or cold repair.
