//! Disabled candidate's shared execution marker and scoped authority. Legacy
//! ACL/resource receipts cannot retire the atomic-bootstrap scope. Its new
//! trusted-bootstrap start requires committed intent; production begin is gated.
//!
//! An immutable, private version-3 record occupies the SAME pending name as the
//! baseline. The legacy version-2 recovery reader refuses it. The shared lease
//! and retained file/ancestor handles span grant and revocation. Drop preserves
//! the file; only an exact completed object-recovery receipt permits deletion.

use super::{CandidateJournalIdentity, PrivateDatabase, fs};
use crate::{
    account_install::{
        AccountBinding, AccountMode, ReadyInstallation, SetupRecord, validate_random_id,
    },
    execution_lease::ExecutionLease,
    util::{OwnedHandle, OwnedSd, pcwstr, wstr},
};
use anyhow::{Context, Result, ensure};
use serde::{Deserialize, Serialize};
use std::{mem::size_of, path::Path};
use windows::Win32::{
    Foundation::{ERROR_INVALID_PARAMETER, FILETIME, HANDLE, WAIT_OBJECT_0, WAIT_TIMEOUT},
    Security::{
        Cryptography::{BCRYPT_USE_SYSTEM_PREFERRED_RNG, BCryptGenRandom},
        SECURITY_ATTRIBUTES,
    },
    Storage::FileSystem::{
        CREATE_NEW, CreateFileW, FILE_BEGIN, FILE_DISPOSITION_INFO, FILE_FLAG_OPEN_REPARSE_POINT,
        FILE_SHARE_READ, FileDispositionInfo, OPEN_EXISTING, SetFileInformationByHandle,
        SetFilePointerEx,
    },
    System::Threading::{
        GetCurrentProcess, GetProcessTimes, OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION,
        PROCESS_SYNCHRONIZE, WaitForSingleObject,
    },
};

const SCOPE: &str = "explicit-read-acls-v1";
const RESOURCE_SCOPE: &str = "resource-plan-v1";
const BOOTSTRAP_PLAN_SCOPE: &str = "atomic-bootstrap-plan-v1";
const PAIRED_PLAN_SCOPE: &str = "paired-bootstrap-plan-v1";

#[path = "account_bootstrap_plan.rs"]
pub(crate) mod bootstrap_plan;

#[path = "account_paired_plan.rs"]
pub(crate) mod paired_plan;

#[path = "account_resource_journal.rs"]
pub(crate) mod resources;

/// Durable evidence, not permission to grant or recover. Only the opaque
/// ActiveExecution/StoppedExecution guards below provide those capabilities.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ExecutionRecord {
    pub version: u32,
    pub owner: String,
    pub pid: u32,
    pub created: u64,
    pub scope: String,
    pub identity: CandidateJournalIdentity,
    pub job: String,
    pub transaction: String,
    pub database_id: [u8; 24],
    pub pending_id: [u8; 24],
}

impl ExecutionRecord {
    pub(crate) fn validate(&self, binding: &AccountBinding, database_id: [u8; 24]) -> Result<()> {
        self.identity.validate(binding)?;
        ensure!(
            self.version == super::JOURNAL_VERSION
                && matches!(
                    self.scope.as_str(),
                    SCOPE | RESOURCE_SCOPE | BOOTSTRAP_PLAN_SCOPE | PAIRED_PLAN_SCOPE
                ),
            "unsupported candidate pending execution scope/version"
        );
        ensure!(
            self.owner == binding.owner_sid && self.pid != 0 && self.created != 0,
            "invalid candidate broker identity"
        );
        validate_random_id(&self.transaction)?;
        ensure!(
            self.job
                == if self.scope == PAIRED_PLAN_SCOPE {
                    crate::job::paired_execution_name(&self.identity.container, false)?
                } else {
                    crate::job::execution_name(&self.identity.container)?
                },
            "candidate pending job identity mismatch"
        );
        ensure!(
            self.database_id == database_id
                && self.database_id != [0; 24]
                && self.pending_id != [0; 24],
            "candidate pending database/object identity mismatch"
        );
        Ok(())
    }
}

struct PendingFile {
    // File first: it closes before ancestor names become replaceable.
    file: fs::Pinned,
    _parents: Vec<fs::Pinned>,
    policy: fs::Policy,
}

impl PendingFile {
    fn open(
        path: &Path,
        policy: fs::Policy,
        parents: Vec<fs::Pinned>,
        create: bool,
    ) -> Result<Self> {
        let descriptor = OwnedSd::from_sddl(&policy.sddl(false))?;
        let security = SECURITY_ATTRIBUTES {
            nLength: size_of::<SECURITY_ATTRIBUTES>() as u32,
            lpSecurityDescriptor: descriptor.ptr.0,
            bInheritHandle: false.into(),
        };
        // READ_DATA activates share checks; DELETE allows exact-handle removal.
        // No existing descriptor is repaired and no path is reopened to delete.
        let handle = OwnedHandle(
            unsafe {
                CreateFileW(
                    pcwstr(&wstr(path.to_str().context("non-Unicode pending path")?)),
                    1 | 0x80 | 0x20000 | 0x10000 | if create { 2 } else { 0 },
                    FILE_SHARE_READ,
                    if create { Some(&security) } else { None },
                    if create { CREATE_NEW } else { OPEN_EXISTING },
                    FILE_FLAG_OPEN_REPARSE_POINT,
                    None,
                )
            }
            .context("pin shared candidate pending journal; preserve existing state")?,
        );
        let file = fs::Pinned::from_handle(handle, path, false)?;
        policy.validate(&file, false)?;
        Ok(Self {
            file,
            _parents: parents,
            policy,
        })
    }

    fn read(&self) -> Result<ExecutionRecord> {
        self.policy.validate(&self.file, false)?;
        unsafe {
            SetFilePointerEx(self.file.handle.raw(), 0, None, FILE_BEGIN)?;
        }
        let record: ExecutionRecord = serde_json::from_slice(&self.file.read_small()?)
            .context("malformed/partial candidate pending journal; preserve for explicit repair")?;
        ensure!(
            record.pending_id == self.file.id.as_bytes(),
            "candidate pending file was replaced; preserve ambiguous state"
        );
        Ok(record)
    }

    fn write_new(&self, record: &ExecutionRecord) -> Result<()> {
        self.file.write_new(&serde_json::to_vec(record)?)?;
        ensure!(
            self.read()? == *record,
            "candidate pending journal readback mismatch"
        );
        Ok(())
    }

    fn remove(self, expected: &ExecutionRecord) -> Result<()> {
        ensure!(
            self.read()? == *expected,
            "candidate pending journal changed before retirement"
        );
        let info = FILE_DISPOSITION_INFO {
            DeleteFile: true.into(),
        };
        unsafe {
            SetFileInformationByHandle(
                self.file.handle.raw(),
                FileDispositionInfo,
                &info as *const _ as *const _,
                size_of::<FILE_DISPOSITION_INFO>() as u32,
            )?;
        }
        // Close the sole DELETE handle while the namespace remains pinned.
        drop(self);
        Ok(())
    }
}

/// Live ACL authority. Not Clone; consuming it into stopped authority prevents
/// another grant through the same API. Drop NEVER retires the pending marker.
pub struct ActiveExecution<'lease> {
    pending: PendingFile,
    record: ExecutionRecord,
    _lease: Option<&'lease ExecutionLease>,
}

/// Proof that this exact execution can no longer acquire new grants and its
/// recorded broker (crash recovery) and named job cannot still use them.
pub struct StoppedExecution<'lease> {
    pending: PendingFile,
    record: ExecutionRecord,
    _lease: Option<&'lease ExecutionLease>,
}

/// Only produced after all admitted per-object rows have been safely retired.
#[derive(Debug)]
pub struct RecoveredReadTree {
    record: ExecutionRecord,
}

impl RecoveredReadTree {
    pub(crate) fn new(record: ExecutionRecord) -> Self {
        Self { record }
    }
}

impl<'lease> ActiveExecution<'lease> {
    /// Generate a fresh execution identity and publish/flush intent before any
    /// ACL mutation. Inventory uses identity() afterwards; neither step launches.
    pub fn begin(
        installation: &ReadyInstallation,
        database: &PrivateDatabase<'_>,
        lease: &'lease ExecutionLease,
        mode: AccountMode,
    ) -> Result<Self> {
        Self::begin_scope(installation, database, lease, mode, SCOPE)
    }

    // No executable adapter consumes this authority yet. Keeping a different
    // durable scope prevents an ACL receipt from retiring full-resource intent.
    #[allow(dead_code)]
    pub(crate) fn begin_resources(
        installation: &ReadyInstallation,
        database: &PrivateDatabase<'_>,
        lease: &'lease ExecutionLease,
        mode: AccountMode,
    ) -> Result<Self> {
        Self::begin_scope(installation, database, lease, mode, RESOURCE_SCOPE)
    }

    /// New marker scope prevents legacy ACL/resource receipts from authorizing
    /// the atomically contained host bootstrap. No production activation yet.
    #[allow(dead_code)]
    pub(crate) fn begin_bootstrap_plan(
        installation: &ReadyInstallation,
        database: &PrivateDatabase<'_>,
        lease: &'lease ExecutionLease,
        mode: AccountMode,
    ) -> Result<Self> {
        crate::account_launch::require_lifecycle_authority()?;
        Self::begin_scope(installation, database, lease, mode, BOOTSTRAP_PLAN_SCOPE)
    }

    /// Separate durable scope: neither single-job nor legacy resource receipts
    /// can stop or recover the two original jobs. Production remains gated.
    #[allow(dead_code)]
    pub(crate) fn begin_paired_plan(
        installation: &ReadyInstallation,
        database: &PrivateDatabase<'_>,
        lease: &'lease ExecutionLease,
        mode: AccountMode,
    ) -> Result<Self> {
        crate::account_launch::require_lifecycle_authority()?;
        Self::begin_scope(installation, database, lease, mode, PAIRED_PLAN_SCOPE)
    }

    fn begin_scope(
        installation: &ReadyInstallation,
        database: &PrivateDatabase<'_>,
        lease: &'lease ExecutionLease,
        mode: AccountMode,
        scope: &str,
    ) -> Result<Self> {
        let current = reload(installation, lease)?;
        let identity = CandidateJournalIdentity::new(
            current.binding(mode)?,
            format!("Alysis.Execution.{}", random_id()?),
        )?;
        super::validate_header(database.connection()?, &identity.binding)?;
        let (path, policy, parents) = shared_path(&current.record)?;
        let pending = PendingFile::open(&path, policy, parents, true)?;
        let record = ExecutionRecord {
            version: super::JOURNAL_VERSION,
            owner: identity.binding.owner_sid.clone(),
            pid: std::process::id(),
            created: process_created(unsafe { GetCurrentProcess() })?,
            scope: scope.into(),
            job: if scope == PAIRED_PLAN_SCOPE {
                crate::job::paired_execution_name(&identity.container, false)?
            } else {
                crate::job::execution_name(&identity.container)?
            },
            identity,
            transaction: random_id()?,
            database_id: database.file_identity(),
            pending_id: pending.file.id.as_bytes(),
        };
        record.validate(&record.identity.binding, database.file_identity())?;
        pending.write_new(&record)?;
        Ok(Self {
            pending,
            record,
            _lease: Some(lease),
        })
    }

    pub fn identity(&self) -> &CandidateJournalIdentity {
        &self.record.identity
    }
    pub(crate) fn record(&self) -> Result<&ExecutionRecord> {
        ensure!(
            self.pending.read()? == self.record,
            "live candidate pending record changed"
        );
        ensure!(
            self.record.pid == std::process::id()
                && self.record.created == process_created(unsafe { GetCurrentProcess() })?,
            "candidate live authority belongs to another broker"
        );
        Ok(&self.record)
    }

    pub(crate) fn acl_record(&self) -> Result<&ExecutionRecord> {
        let record = self.record()?;
        ensure!(
            matches!(record.scope.as_str(), SCOPE | RESOURCE_SCOPE),
            "atomic bootstrap scope cannot use legacy ACL grant authority"
        );
        Ok(record)
    }

    pub fn stop(self) -> Result<StoppedExecution<'lease>> {
        self.record()?;
        ensure!(
            self.record.scope != PAIRED_PLAN_SCOPE,
            "paired execution requires both original jobs; single-job stop refused"
        );
        crate::job::wait_for_execution_exit(&self.record.identity.container)?;
        Ok(StoppedExecution {
            pending: self.pending,
            record: self.record,
            _lease: self._lease,
        })
    }
}

impl<'lease> StoppedExecution<'lease> {
    pub(crate) fn acl_recovery_record(
        &self,
        resources: Option<&resources::RecoveredResources>,
    ) -> Result<&ExecutionRecord> {
        let record = self.record()?;
        ensure!(
            record.scope == SCOPE
                || (record.scope == RESOURCE_SCOPE
                    && resources.is_some_and(|receipt| receipt.matches(record))),
            "resource execution requires proven complete resource teardown before ACL recovery"
        );
        Ok(record)
    }

    /// No process is terminated and no journal is removed on error. Current
    /// setup, exact pending/DB identities, broker death and job emptiness must
    /// all be proven before this typed recovery capability is returned.
    pub fn recover(
        installation: &ReadyInstallation,
        database: &PrivateDatabase<'_>,
        lease: &'lease ExecutionLease,
        mode: AccountMode,
    ) -> Result<Self> {
        let current = reload(installation, lease)?;
        let binding = current.binding(mode)?;
        super::validate_header(database.connection()?, &binding)?;
        let (path, policy, parents) = shared_path(&current.record)?;
        let pending = PendingFile::open(&path, policy, parents, false)?;
        let record = pending.read()?;
        record.validate(&binding, database.file_identity())?;
        ensure!(
            record.scope != PAIRED_PLAN_SCOPE,
            "paired execution cold recovery is unavailable; preserve original state"
        );
        broker_stopped(record.pid, record.created)?;
        crate::job::wait_for_execution_exit(&record.identity.container)?;
        Ok(Self {
            pending,
            record,
            _lease: Some(lease),
        })
    }

    pub(crate) fn record(&self) -> Result<&ExecutionRecord> {
        ensure!(
            self.pending.read()? == self.record,
            "stopped candidate pending record changed"
        );
        ensure!(
            self.record.scope != PAIRED_PLAN_SCOPE,
            "paired execution cannot use single-job stopped authority"
        );
        crate::job::wait_for_execution_exit(&self.record.identity.container)?;
        Ok(&self.record)
    }

    pub fn finish(self, receipt: RecoveredReadTree) -> Result<()> {
        ensure!(
            self.record.scope == SCOPE,
            "resource execution requires a complete resource recovery receipt"
        );
        ensure!(
            receipt.record == *self.record()?,
            "recovery receipt belongs to another execution"
        );
        self.pending.remove(&self.record)
    }

    #[allow(dead_code)]
    pub(crate) fn finish_resources(
        self,
        acls: RecoveredReadTree,
        resources: resources::RecoveredResources,
    ) -> Result<()> {
        ensure!(
            self.record.scope == RESOURCE_SCOPE,
            "not a resource execution"
        );
        ensure!(
            acls.record == *self.record()? && resources.matches(&self.record),
            "resource/ACL recovery receipts belong to another execution"
        );
        self.pending.remove(&self.record)
    }
}

fn reload(installation: &ReadyInstallation, lease: &ExecutionLease) -> Result<ReadyInstallation> {
    let current = ReadyInstallation::read(lease)?;
    ensure!(
        current.record == installation.record,
        "candidate setup changed; reload before execution"
    );
    Ok(current)
}

fn shared_path(record: &SetupRecord) -> Result<(std::path::PathBuf, fs::Policy, Vec<fs::Pinned>)> {
    let expected = fs::known_folder_anchor()?
        .join("alysis-sandbox")
        .join("execution-pending.json");
    let actual = super::pending_journal_path()?;
    ensure!(
        actual
            .to_str()
            .context("non-Unicode shared pending path")?
            .eq_ignore_ascii_case(
                expected
                    .to_str()
                    .context("non-Unicode KnownFolder pending path")?
            ),
        "shared pending journal environment differs from OS KnownFolder; refusing split recovery stores"
    );
    let policy = policy(record)?;
    // Existing parent only. No candidate-specific redirect, ancestor repair,
    // adoption of public storage, or directory creation is permitted here.
    let parents = fs::pin_session_path(expected.parent().unwrap(), &policy)?;
    Ok((expected, policy, parents))
}

fn policy(record: &SetupRecord) -> Result<fs::Policy> {
    let offline = record.binding(AccountMode::Offline)?;
    let online = record.binding(AccountMode::Online)?;
    Ok(fs::Policy {
        owner: record.owner_sid.clone(),
        offline: offline.account_sid,
        online: online.account_sid,
        group: offline.group_sid,
    })
}

pub(crate) fn random_id() -> Result<String> {
    let mut bytes = [0u8; 16];
    unsafe {
        BCryptGenRandom(None, &mut bytes, BCRYPT_USE_SYSTEM_PREFERRED_RNG).ok()?;
    }
    Ok(bytes.iter().map(|byte| format!("{byte:02x}")).collect())
}

fn process_created(handle: HANDLE) -> Result<u64> {
    let mut values = [FILETIME::default(); 4];
    unsafe {
        GetProcessTimes(
            handle,
            &mut values[0] as *mut _,
            &mut values[1] as *mut _,
            &mut values[2] as *mut _,
            &mut values[3] as *mut _,
        )?;
    }
    Ok((u64::from(values[0].dwHighDateTime) << 32) | u64::from(values[0].dwLowDateTime))
}

fn broker_stopped(pid: u32, created: u64) -> Result<()> {
    ensure!(pid != 0 && created != 0, "invalid recorded broker identity");
    let handle = match unsafe {
        OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SYNCHRONIZE,
            false,
            pid,
        )
    } {
        Ok(handle) => OwnedHandle(handle),
        // A generic error code is not absence evidence. A complete documented
        // process enumeration must independently show the old PID absent.
        Err(error) if error.code() == ERROR_INVALID_PARAMETER.to_hresult() => {
            return absent_process(pid);
        }
        Err(error) => {
            return Err(error).context("cannot prove recorded broker exit; keep recovery pending");
        }
    };
    if process_created(handle.raw())? != created {
        return Ok(());
    }
    let status = unsafe { WaitForSingleObject(handle.raw(), 0) };
    ensure!(
        status != WAIT_TIMEOUT,
        "recorded candidate broker is still alive"
    );
    ensure!(
        status == WAIT_OBJECT_0,
        "cannot establish recorded broker exit"
    );
    Ok(())
}

fn absent_process(pid: u32) -> Result<()> {
    #[link(name = "kernel32")]
    unsafe extern "system" {
        fn K32EnumProcesses(ids: *mut u32, bytes: u32, written: *mut u32) -> i32;
    }
    let mut capacity = 1024;
    while capacity <= 131_072 {
        let mut ids = vec![0u32; capacity];
        let mut written = 0;
        ensure!(
            unsafe { K32EnumProcesses(ids.as_mut_ptr(), (ids.len() * 4) as u32, &mut written) }
                != 0,
            "cannot enumerate processes to prove broker absence: {}",
            std::io::Error::last_os_error()
        );
        if let Some(observed) = complete_process_list(&ids, written)? {
            ensure!(
                observed.contains(&std::process::id()),
                "process enumeration omitted the live caller"
            );
            ensure!(
                !observed.contains(&pid),
                "recorded broker PID exists but cannot be inspected"
            );
            return Ok(());
        }
        capacity *= 2;
    }
    anyhow::bail!("process enumeration exceeded recovery bound; broker absence unverifiable")
}

fn complete_process_list(ids: &[u32], written: u32) -> Result<Option<&[u32]>> {
    let written = written as usize;
    ensure!(
        written > 0 && written % 4 == 0 && written <= ids.len() * 4,
        "invalid process enumeration byte count"
    );
    // Equal size may be truncated per the documented EnumProcesses contract.
    if written == ids.len() * 4 {
        return Ok(None);
    }
    Ok(Some(&ids[..written / 4]))
}

#[cfg(test)]
pub(crate) fn fixture_record(identity: CandidateJournalIdentity) -> ExecutionRecord {
    ExecutionRecord {
        version: super::JOURNAL_VERSION,
        owner: identity.binding.owner_sid.clone(),
        pid: std::process::id(),
        created: process_created(unsafe { GetCurrentProcess() }).unwrap(),
        scope: SCOPE.into(),
        job: crate::job::execution_name(&identity.container).unwrap(),
        transaction: random_id().unwrap(),
        identity,
        database_id: [1; 24],
        pending_id: [2; 24],
    }
}

#[cfg(test)]
#[path = "account_execution_tests.rs"]
mod tests;
