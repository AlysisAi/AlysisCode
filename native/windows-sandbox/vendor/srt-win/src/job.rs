//! Job-object wrapper for the sandbox child.
//!
//! Two roles:
//!  1. `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` containment — the sandboxed
//!     process tree dies with the broker.
//!  2. `JOBOBJECT_BASIC_UI_RESTRICTIONS` — block clipboard, global
//!     atoms, system-parameter writes, display-settings changes,
//!     desktop switching, ExitWindows, and cross-job USER/GDI handle
//!     access. The bits are listed individually rather than relying
//!     on a hypothetical `_ALL` so the enforced surface is auditable
//!     from this call site.
//!
//! Both must be set BEFORE `AssignProcessToJobObject` so they're in
//! effect from the moment the suspended child is assigned (the caller
//! resumes the thread only after `assign`).

use anyhow::{Context, Result, ensure};
use std::ffi::c_void;
use std::mem::{size_of, zeroed};
use std::time::{Duration, Instant};
use windows::Win32::Foundation::{
    CloseHandle, ERROR_ALREADY_EXISTS, ERROR_FILE_NOT_FOUND, GetLastError, HANDLE,
};
use windows::Win32::Security::SECURITY_ATTRIBUTES;
use windows::Win32::System::JobObjects::{
    AssignProcessToJobObject, CreateJobObjectW, IsProcessInJob, JOB_OBJECT_LIMIT_BREAKAWAY_OK,
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK,
    JOB_OBJECT_UILIMIT_DESKTOP, JOB_OBJECT_UILIMIT_DISPLAYSETTINGS, JOB_OBJECT_UILIMIT_EXITWINDOWS,
    JOB_OBJECT_UILIMIT_GLOBALATOMS, JOB_OBJECT_UILIMIT_HANDLES, JOB_OBJECT_UILIMIT_READCLIPBOARD,
    JOB_OBJECT_UILIMIT_SYSTEMPARAMETERS, JOB_OBJECT_UILIMIT_WRITECLIPBOARD,
    JOBOBJECT_BASIC_ACCOUNTING_INFORMATION, JOBOBJECT_BASIC_UI_RESTRICTIONS,
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JobObjectBasicAccountingInformation,
    JobObjectBasicUIRestrictions, JobObjectExtendedLimitInformation, OpenJobObjectW,
    QueryInformationJobObject, SetInformationJobObject, TerminateJobObject,
};
use windows::Win32::System::SystemServices::JOB_OBJECT_QUERY;
use windows::Win32::System::Threading::GetCurrentProcess;
use windows::core::PCWSTR;

/// Refuse a known incompatible host before changing profiles or permissions.
/// A NULL-job query observes one associated job, not the entire nested chain.
/// Passing this check is not launch authority: atomic job assignment and all
/// child checks still run, and a restrictive outer job can still deny creation.
pub fn require_compatible_host_job() -> Result<bool> {
    let mut member = windows::core::BOOL::default();
    unsafe { IsProcessInJob(GetCurrentProcess(), None, &mut member) }
        .context("cannot inspect host job membership; sandbox launch refused")?;
    if !member.as_bool() {
        return Ok(false);
    }
    let limits = read_job_limits(None)?;
    require_host_breakaway(limits.BasicLimitInformation.LimitFlags.0)?;
    Ok(true)
}

fn read_job_limits(job: Option<HANDLE>) -> Result<JOBOBJECT_EXTENDED_LIMIT_INFORMATION> {
    let mut info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
    let mut returned = 0;
    unsafe {
        QueryInformationJobObject(
            job,
            JobObjectExtendedLimitInformation,
            (&mut info as *mut JOBOBJECT_EXTENDED_LIMIT_INFORMATION).cast(),
            size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            Some(&mut returned),
        )
    }
    .context("cannot inspect host job restrictions; sandbox launch refused")?;
    ensure!(
        returned as usize == size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>(),
        "host job restriction query was incomplete; sandbox launch refused"
    );
    Ok(info)
}

fn require_host_breakaway(flags: u32) -> Result<()> {
    ensure!(
        flags & (JOB_OBJECT_LIMIT_BREAKAWAY_OK.0 | JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK.0) != 0,
        "native Windows sandbox cannot start from this host: its Windows job forbids child breakaway. Launch Alysis from a normal desktop or terminal outside that restricted job. Containment limits remain required; no unrestricted fallback was attempted."
    );
    Ok(())
}

/// The execution profile name is cryptographically random and journaled before
/// process creation. Global scope makes recovery independent of logon session.
pub fn execution_name(container: &str) -> Result<String> {
    let suffix = container
        .strip_prefix("Alysis.Execution.")
        .context("invalid execution container name")?;
    ensure!(
        suffix.len() == 32 && suffix.bytes().all(|c| c.is_ascii_hexdigit()),
        "invalid execution container name"
    );
    Ok(format!("Global\\AlysisSandboxJob.{suffix}"))
}

/// Distinct namespace for the two-role candidate. A legacy execution job name
/// must never be mistaken for either original job in this layout.
#[cfg(feature = "account-appcontainer-candidate")]
pub(crate) fn paired_execution_name(container: &str, consumers: bool) -> Result<String> {
    execution_name(container)?;
    let suffix = container.strip_prefix("Alysis.Execution.").unwrap();
    let role = if consumers { "Consumer" } else { "Owner" };
    Ok(format!("Global\\AlysisSandbox{role}Job.{suffix}"))
}

/// Read-only crash recovery. A named job remains alive until its last handle
/// closes AND every associated process terminates. Never revoke ACL denies
/// merely because the broker PID has exited. No termination rights are opened.
pub fn wait_for_execution_exit(container: &str) -> Result<()> {
    let name = crate::util::wstr(&execution_name(container)?);
    let handle = match unsafe { OpenJobObjectW(JOB_OBJECT_QUERY, false, PCWSTR(name.as_ptr())) } {
        Ok(handle) => crate::util::OwnedHandle(handle),
        Err(error) if error.code() == ERROR_FILE_NOT_FOUND.to_hresult() => return Ok(()),
        Err(error) => {
            return Err(error).context("cannot inspect execution job; recovery remains pending");
        }
    };
    wait_until_empty(handle.raw())
}

fn wait_until_empty(handle: HANDLE) -> Result<()> {
    let deadline = Instant::now() + Duration::from_secs(30);
    loop {
        let mut info = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION::default();
        unsafe {
            QueryInformationJobObject(
                Some(handle),
                JobObjectBasicAccountingInformation,
                (&mut info as *mut JOBOBJECT_BASIC_ACCOUNTING_INFORMATION).cast(),
                size_of::<JOBOBJECT_BASIC_ACCOUNTING_INFORMATION>() as u32,
                None,
            )
            .context("QueryInformationJobObject(active processes)")?;
        }
        if info.ActiveProcesses == 0 {
            return Ok(());
        }
        ensure!(
            Instant::now() < deadline,
            "sandbox job still has {} processes; recovery remains pending",
            info.ActiveProcesses
        );
        std::thread::sleep(Duration::from_millis(10));
    }
}

/// RAII job object. `Drop` closes the handle; with
/// `KILL_ON_JOB_CLOSE` set, that terminates every process still in
/// the job.
pub struct Job(HANDLE);

impl Job {
    /// Create an unnamed job with kill-on-close + full UI lockdown.
    /// `breakaway_ok` sets `JOB_OBJECT_LIMIT_BREAKAWAY_OK` — `true`
    /// only on the broker→runner Job (so the runner's child can
    /// `CREATE_BREAKAWAY_FROM_JOB` past it onto the runner's own
    /// load-bearing Job); **`false` on the runner→child Job** (a
    /// child must NOT be able to break away from its containment).
    pub fn new(breakaway_ok: bool) -> Result<Self> {
        Self::create(breakaway_ok, None)
    }

    pub fn for_execution(container: &str) -> Result<Self> {
        Self::create(false, Some(&execution_name(container)?))
    }

    #[cfg(feature = "account-appcontainer-candidate")]
    pub(crate) fn for_paired_role(container: &str, consumers: bool) -> Result<Self> {
        Self::create(false, Some(&paired_execution_name(container, consumers)?))
    }

    /// The trusted account runner owns the handle; the host can inspect the
    /// named job during crash recovery. No package/capability ACE is present.
    #[cfg(feature = "account-appcontainer-candidate")]
    pub fn for_account_execution(
        container: &str,
        account_sid: &str,
        owner_sid: &str,
    ) -> Result<Self> {
        crate::account_token::verify_account_runner(account_sid)?;
        let account = crate::sid::LocalPsid::from_string(account_sid)?;
        let owner = crate::sid::LocalPsid::from_string(owner_sid)?;
        ensure!(
            unsafe {
                windows::Win32::Security::EqualSid(account.as_psid(), owner.as_psid()).is_err()
            },
            "account job owner must differ from the host broker"
        );
        let descriptor = account_job_descriptor(account_sid, owner_sid)?;
        Self::create_with_descriptor(false, Some(&execution_name(container)?), &descriptor)
    }

    fn create(breakaway_ok: bool, name: Option<&str>) -> Result<Self> {
        // AppContainer children share TokenUser with the broker, but cannot
        // pass the additional lowbox access check without a package/capability
        // ACE. Never grant this job to the sandbox account or package SID.
        let owner = crate::sid::current_user_sid()?;
        let descriptor =
            crate::util::OwnedSd::from_sddl(&format!("D:P(A;;GA;;;SY)(A;;GA;;;{owner})"))?;
        Self::create_with_descriptor(breakaway_ok, name, &descriptor)
    }

    fn create_with_descriptor(
        breakaway_ok: bool,
        name: Option<&str>,
        descriptor: &crate::util::OwnedSd,
    ) -> Result<Self> {
        let security = SECURITY_ATTRIBUTES {
            nLength: size_of::<SECURITY_ATTRIBUTES>() as u32,
            lpSecurityDescriptor: descriptor.ptr.0,
            bInheritHandle: false.into(),
        };
        let wide = name.map(crate::util::wstr);
        let raw = unsafe {
            let raw = CreateJobObjectW(
                Some(&security),
                wide.as_deref()
                    .map(crate::util::pcwstr)
                    .unwrap_or(PCWSTR::null()),
            )
            .context("CreateJobObjectW")?;
            // Check before constructing Job: Drop must never terminate a
            // pre-existing, potentially unrelated job on name collision.
            let existed = name.is_some() && GetLastError() == ERROR_ALREADY_EXISTS;
            if existed {
                let _ = CloseHandle(raw);
                anyhow::bail!("execution job name already exists; refusing to reuse it");
            }
            raw
        };
        // Wrap the raw handle in `Self` immediately so a `?` from
        // either `SetInformationJobObject` below still closes it.
        let job = Self(raw);
        unsafe {
            let mut ext: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = zeroed();
            ext.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            if breakaway_ok {
                ext.BasicLimitInformation.LimitFlags |= JOB_OBJECT_LIMIT_BREAKAWAY_OK;
            }
            SetInformationJobObject(
                job.0,
                JobObjectExtendedLimitInformation,
                &ext as *const _ as *const c_void,
                size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            )
            .context("SetInformationJobObject(KILL_ON_JOB_CLOSE)")?;

            //   READCLIPBOARD     — block OpenClipboard for read
            //   WRITECLIPBOARD    — block SetClipboardData
            //   HANDLES           — block USER/GDI handles from outside the job
            //   GLOBALATOMS       — block GlobalAddAtom (atom-table IPC)
            //   SYSTEMPARAMETERS  — block SystemParametersInfoW(SPI_SET*)
            //   DISPLAYSETTINGS   — block ChangeDisplaySettings
            //   DESKTOP           — block SwitchDesktop / SetThreadDesktop
            //   EXITWINDOWS       — block sandbox-initiated logoff/shutdown
            let ui_bits = JOB_OBJECT_UILIMIT_READCLIPBOARD
                | JOB_OBJECT_UILIMIT_WRITECLIPBOARD
                | JOB_OBJECT_UILIMIT_HANDLES
                | JOB_OBJECT_UILIMIT_GLOBALATOMS
                | JOB_OBJECT_UILIMIT_SYSTEMPARAMETERS
                | JOB_OBJECT_UILIMIT_DISPLAYSETTINGS
                | JOB_OBJECT_UILIMIT_DESKTOP
                | JOB_OBJECT_UILIMIT_EXITWINDOWS;
            let ui = JOBOBJECT_BASIC_UI_RESTRICTIONS {
                UIRestrictionsClass: ui_bits,
            };
            SetInformationJobObject(
                job.0,
                JobObjectBasicUIRestrictions,
                &ui as *const _ as *const c_void,
                size_of::<JOBOBJECT_BASIC_UI_RESTRICTIONS>() as u32,
            )
            .context("SetInformationJobObject(BasicUIRestrictions)")?;
        }
        Ok(job)
    }

    /// Raw job handle (for `IsProcessInJob` diagnostics).
    pub fn raw(&self) -> HANDLE {
        self.0
    }

    /// Assign a (suspended) process to the job.
    pub fn assign(&self, proc: HANDLE) -> Result<()> {
        unsafe { AssignProcessToJobObject(self.0, proc).context("AssignProcessToJobObject") }
    }

    /// Termination is asynchronous. Keep the job handle until the kernel
    /// confirms that no process remains, before callers revoke filesystem
    /// denies or remove the execution profile. Callers must close child
    /// process/thread handles first so they do not retain job accounting refs.
    pub(crate) fn terminate_and_wait(&self) -> Result<()> {
        unsafe { TerminateJobObject(self.0, 1).context("TerminateJobObject")? };
        wait_until_empty(self.0)
    }
}

#[cfg(feature = "account-appcontainer-candidate")]
fn account_job_descriptor(account_sid: &str, owner_sid: &str) -> Result<crate::util::OwnedSd> {
    crate::util::OwnedSd::from_sddl(&format!(
        "D:P(A;;GA;;;SY)(A;;GA;;;{account_sid})(A;;0x4;;;{owner_sid})(A;;RC;;;OW)"
    ))
}

/// `IsProcessInJob(proc, job)` — `job = None` means "any job".
///
/// On API failure returns **`true`** (conservative): the only
/// non-diagnostic caller gates `CREATE_BREAKAWAY_FROM_JOB` on it, and
/// breakaway is harmless when the caller is NOT in a job (the kernel
/// just ignores the flag) but failing to set it when the caller IS in
/// one breaks the runner→child Job assign.
pub fn is_process_in_job(proc: HANDLE, job: Option<HANDLE>) -> bool {
    let mut r = windows::core::BOOL(0);
    match unsafe { IsProcessInJob(proc, job, &mut r) } {
        Ok(()) => r.as_bool(),
        Err(_) => true,
    }
}

impl Drop for Job {
    fn drop(&mut self) {
        if let Err(error) = self.terminate_and_wait() {
            // Do not unwind into ACL/profile cleanup with unverified process
            // exit. OS handle teardown still enforces KILL_ON_JOB_CLOSE, while
            // the wrapper's durable journal remains available for recovery.
            crate::util::exit_for_unverified_cleanup("job", &error);
        }
        unsafe {
            let _ = CloseHandle(self.0);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn host_job_preflight_refuses_native_nonbreakaway_limits_without_changing_them() {
        let blocked = Job::new(false).unwrap();
        let before = read_job_limits(Some(blocked.raw())).unwrap();
        let error = require_host_breakaway(before.BasicLimitInformation.LimitFlags.0).unwrap_err();
        assert!(
            error
                .to_string()
                .contains("Windows job forbids child breakaway")
        );
        let after = read_job_limits(Some(blocked.raw())).unwrap();
        assert_eq!(
            before.BasicLimitInformation.LimitFlags,
            after.BasicLimitInformation.LimitFlags
        );
        assert!(
            after
                .BasicLimitInformation
                .LimitFlags
                .contains(JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)
        );
        let allowed = Job::new(true).unwrap();
        require_host_breakaway(
            read_job_limits(Some(allowed.raw()))
                .unwrap()
                .BasicLimitInformation
                .LimitFlags
                .0,
        )
        .unwrap();
        require_host_breakaway(JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK.0).unwrap();
    }

    #[test]
    fn execution_names_reject_external_names_and_short_suffixes() {
        assert!(execution_name("Global\\unrelated").is_err());
        assert!(execution_name("Alysis.Execution.1234").is_err());
        assert!(execution_name("Alysis.Execution.0000000000000000000000000000000g").is_err());
        assert_eq!(
            execution_name("Alysis.Execution.0123456789abcdef0123456789abcdef").unwrap(),
            "Global\\AlysisSandboxJob.0123456789abcdef0123456789abcdef"
        );
    }

    #[test]
    fn named_job_collision_never_reuses_an_existing_job() {
        let container = format!("Alysis.Execution.{:032x}", std::process::id());
        let original = Job::for_execution(&container).unwrap();
        assert!(Job::for_execution(&container).is_err());
        wait_for_execution_exit(&container).unwrap();
        // Verify a failed creation did not close our original handle.
        wait_until_empty(original.raw()).unwrap();
        drop(original);
        wait_for_execution_exit(&container).unwrap();
    }

    #[cfg(feature = "account-appcontainer-candidate")]
    #[test]
    fn account_job_host_can_query_but_cannot_mutate_recovery_job() {
        use windows::Win32::Security::Cryptography::{
            BCRYPT_USE_SYSTEM_PREFERRED_RNG, BCryptGenRandom,
        };
        let mut random = [0u8; 16];
        unsafe {
            BCryptGenRandom(None, &mut random, BCRYPT_USE_SYSTEM_PREFERRED_RNG)
                .ok()
                .unwrap();
        }
        let suffix: String = random.iter().map(|byte| format!("{byte:02x}")).collect();
        let container = format!("Alysis.Execution.{suffix}");
        let owner = crate::sid::current_user_sid().unwrap();
        // Use an unallocated principal for the runner: this test provisions no
        // account and proves the real host's rights against the kernel job DACL.
        let descriptor = account_job_descriptor("S-1-5-21-1-2-3-9999", &owner).unwrap();
        let name = execution_name(&container).unwrap();
        let job = Job::create_with_descriptor(false, Some(&name), &descriptor).unwrap();
        wait_for_execution_exit(&container).unwrap();
        let name = crate::util::wstr(&name);
        for access in [0x1u32, 0x2, 0x8, 0x10, 0x40000, 0x80000] {
            match unsafe { OpenJobObjectW(access, false, PCWSTR(name.as_ptr())) } {
                Ok(handle) => {
                    drop(crate::util::OwnedHandle(handle));
                    panic!("host recovery ACE permits mutation {access:#x}");
                }
                Err(error) => assert_eq!(
                    error.code(),
                    windows::Win32::Foundation::ERROR_ACCESS_DENIED.to_hresult()
                ),
            }
        }
        drop(job);
    }
}
