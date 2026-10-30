# Upstream provenance

Source: https://github.com/anthropics/sandbox-runtime

Commit: `117eb928202b53c80d3cb6527d88d1b90e4ca7a9`

Imported directory: `vendor/srt-win-src` (src, tests, Cargo metadata and rustfmt config).

License: Apache-2.0, reproduced in LICENSE. Original source comments are retained.

Local changes: installation strings `sandbox-runtime` and `srt-sandbox` are
namespaced to `alysis-sandbox`; the persistent WFP sublayer GUID is
`49e9dc19-a523-49b7-ae6a-5e2ffb908e47`. The Rust crate name remains `srt-win`
so its internal APIs and tests remain recognizable. No Anthropic installation
is read, repaired, or removed by Alysis.

This is an alpha upstream implementation, not an assertion of security
qualification. Alysis must qualify its composed installed runtime separately.

The raw ACL test fixture adds a process-local atomic sequence to its temporary
directory name. Windows clock resolution can otherwise give simultaneous tests
the same directory. Explicit security descriptors and all ACL assertions stay
unchanged; this adjustment affects test fixtures only.

Additional Alysis changes:

- Setup schema 3 records the owning Windows user; this preview does not share
  its account between real Windows users.
- The former loopback proxy exceptions now BLOCK the sandbox SID. Host users
  are unaffected. Existing tag names are retained for status/migration.
- Broker job assignment, runner process protection, and session-directory
  protection fail closed.
- Ordinary ACL release and orphan recovery use transactions and preserve failed
  revocations. Grants are recovered before deny entries.
- ACL policy application runs inside the owning broker; missing deny targets
  fail closed rather than being silently omitted.
- Fixed sandbox-account filesystem probes verify ambient enforcement when the
  host cannot read system-folder security descriptors; status reports unknown
  separately from absent.
- Parent delete protection and private-path traversal are object-only ACLs.
  Their updates preserve inheritance control and avoid recursive propagation
  through unrelated profile contents. Write denies also cover child deletion
  and ACL/ownership changes.
- Writable workspace owners retain an inheritable owner grant, and the child
  token's default DACL includes the trusted host, so returned output is readable.
- The certificate parser's public test certificate is copied from upstream
  `test/fixtures/tls-terminate/ca.crt`; its include path is adjusted.

The containing Alysis crate adds the execution request protocol, single
execution lease, engine-parent lifetime monitor, enforcement probes and journal.

- Added a host-broker AppContainer entry point with zero network capabilities by default,
  isolated per-execution profiles and a separate WRITE_RESTRICTED token. The
  restricting SID list contains the sandbox-account SID and Everyone for standard
  runtime IPC compatibility; AppContainer access checks remain required. Package
  deny ACEs alone are never treated as enforcement. The actual suspended child
  token is checked for package identity, capabilities, restricted SIDs and Low IL,
  then impersonated for effective protected-file write checks before resume.
- Per-execution package/window-station ACEs and profile removal have an independent
  durable journal, separate from the account-scoped ACL database.
- Normal job teardown explicitly terminates remaining processes and waits for
  zero active processes before returning to ACL/profile cleanup. An unverifiable
  termination exits the broker without removing the recovery journal. This
  accounts for the asynchronous termination behavior documented by Microsoft:
  https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-terminatejobobject
  and https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_basic_accounting_information.
- Protocol 3 can explicitly request the outbound `WinCapabilityInternetClientSid`
  capability. The child must have exactly the requested capability set; private
  network/server, enterprise authentication and credential-sharing capabilities
  are never added. Earlier Node/OpenSSL HTTPS checks passed, but current
  qualification records host failures and a sandbox timeout. Schannel reports
  `SEC_E_NO_CREDENTIALS` under the write-restricted token. Network compatibility
  remains a release blocker; the token has not been weakened to accommodate it.
  Capability references:
  https://learn.microsoft.com/en-us/windows/win32/api/winnt/ne-winnt-well_known_sid_type
  and https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-security_capabilities.

- Security revision 2 shares an execution/maintenance lease, blocks setup and
  removal while commands or recovery are pending, creates children atomically
  inside named jobs, and checks that job during crash recovery. Broker creation
  times prevent PID reuse from masquerading as the old owner. See
  https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects and
  https://devblogs.microsoft.com/oldnewthing/20230209-00/?p=107812.
- ACL root and ancestor handles deny delete/rename sharing and reject reparses.
  Recorded file identity is checked on the handle used by Get/SetSecurityInfo.
  Object-only updates keep their non-propagating behavior under pinned names.
  Descendant inheritance races are not claimed resolved. See
  https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew
  and https://learn.microsoft.com/en-us/windows/win32/api/aclapi/nf-aclapi-setsecurityinfo.
- Security revision 3 reserves existing intermediate ancestors of nested
  protected paths against deletion and ACL/owner changes, using object-only
  denies recorded in the existing ACL journal. Both the account probe and the
  suspended AppContainer probe check each dangerous right before execution.
  Sibling content remains writable. The engine also checks every recursive read
  grant for existing aliases and protects its discovered root skill directories.
- Security revision 4 binds credential reads to the recorded Windows owner and
  validates the actual registry key owner and exact protected DACL through the
  same handle used to read the DPAPI blob. Credential ACLs explicitly deny both
  the sandbox account and group; SYSTEM/administrators retain full access and
  only the recorded owner receives read access. Each rotation creates a fresh
  protected key and atomically commits its validated name in the admin-written
  setup record, so a handle opened before ACL repair never sees a new credential.
  Partial setup invalidates readiness
  before rotating passwords, and account lifecycle changes require a matching
  recorded local SAM identity.
- An opt-in `account-appcontainer-candidate` feature contains separate namespace
  and dedicated-account token experiments for Schannel compatibility. It is not
  enabled by the default helper build and does not change the default backend.
- Private read roots using OWNER_RIGHTS receive an explicit read/execute grant
  only for their verified current owner. This fixes read-only AppContainer
  startup without granting write access or changing foreign-owned roots.
- Recovery ID lookups match the recorded volume and complete object identity,
  distinguish proven absence from errors, and reject still-open unlinked
  objects. The lossless NTFS 64-bit compatibility retry is bounded. Error 87
  remains ambiguous and retains the journal; no deleted-object repair is
  claimed. See the containing `RECOVERY.md` for supported operator actions.
- Candidate-only account modules bind setup, fresh credential generations,
  typed launch/profile cleanup, database headers and journals to separate
  offline/online account identities. Their transactional setup layer requires
  elevation, the original owner and the shared lease, invalidates readiness
  before changes and commits readiness last. Full provisioning and candidate
  ACL/recovery integration remain unfinished; no candidate accounts or policies
  are installed by the default build.
- Candidate bootstrap creates only disabled accounts after durable intent,
  records exact returned SIDs before subsequent mutation, refuses unrecorded
  existing principals, and gates its handoff to the credential/policy stage.
  Synthetic interruption tests cover before/after effects. It has no default
  CLI route and has not been exercised against installed machine state.
- A shared local-machine role guard precedes baseline and candidate account
  maintenance. It permits only standalone/member workstations and servers;
  domain controllers, unknown roles and query failures are refused before
  local SAM calls can affect directory accounts. Normal execution and recovery
  remain independent of this setup guard.
- A separate NTFS file-record evidence module validates bounded driver responses
  and provides read-only observations. Baseline recovery does not call it;
  neither an ambiguous lookup nor a deleted root proves all inherited descendant
  grants were cleaned. Privileged observations and safe integration remain work.
- Candidate-only SQLite storage now admits a protected identity manifest before
  SQLite can inspect a journal, creates private directories through atomic
  handle-relative operations, and retains identity pins with the connection.
  Ambiguous state and existing sidecars are refused and preserved. Production
  ancestry checks currently reject a capability FullControl grant on the test
  host, so isolated filesystem tests are not a production-location pass.
