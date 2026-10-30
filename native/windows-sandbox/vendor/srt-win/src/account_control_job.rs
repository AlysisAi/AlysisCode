//! Read-only, repeated observations of the original broker-owned job. Borrowing
//! keeps the owning guard alive without duplicating a handle (a duplicate could
//! defer kernel kill-on-last-close). No PID/name lookup, assignment or mutation.
//! This does not prove atomic process birth, executable identity, a complete
//! nested-job chain, historical policy or crash-safe resource retirement.

use crate::{
    job::Job,
    util::{OwnedSd, win32_ok},
};
use anyhow::{Context, Result, ensure};
use std::mem::{size_of, size_of_val};
use windows::Win32::{
    Foundation::HANDLE,
    Security::{
        Authorization::{GetSecurityInfo, SE_KERNEL_OBJECT},
        DACL_SECURITY_INFORMATION, GetSecurityDescriptorControl, GetSecurityDescriptorDacl,
        GetSecurityDescriptorOwner, OWNER_SECURITY_INFORMATION, PSECURITY_DESCRIPTOR, PSID,
    },
    System::JobObjects::*,
};

const JOB_ALL: u32 = 0x001f003f;

pub(crate) fn prepare(job: &Job) -> Result<()> {
    crate::account_impersonation::require_process_identity()?;
    super::noninheritable(job.raw())?;
    verify_security(job.raw(), &crate::sid::current_user_sid()?)?;
    verify_policy(job.raw())
}

pub(super) struct JobBinding<'a> {
    job: &'a Job,
    owner: String,
}

impl<'a> JobBinding<'a> {
    pub(super) fn observe(job: &'a Job, process: HANDLE) -> Result<Self> {
        crate::account_impersonation::require_process_identity()?;
        let binding = Self {
            job,
            owner: crate::sid::current_user_sid()?,
        };
        binding.verify(process)?;
        Ok(binding)
    }

    pub(super) fn verify(&self, process: HANDLE) -> Result<()> {
        crate::account_impersonation::require_process_identity()?;
        ensure!(
            self.owner == crate::sid::current_user_sid()?,
            "control job broker owner changed"
        );
        super::noninheritable(self.job.raw())?;
        verify_security(self.job.raw(), &self.owner)?;
        verify_policy(self.job.raw())?;
        // Never use job::is_process_in_job here: that diagnostic helper returns
        // true on query failure. Authentication requires native success AND true.
        require_member(process, self.job.raw())?;
        verify_policy(self.job.raw())?;
        verify_security(self.job.raw(), &self.owner)?;
        super::noninheritable(self.job.raw())
    }
}

fn require_member(process: HANDLE, job: HANDLE) -> Result<()> {
    // Win32 treats a NULL job as "any job", even when wrapped in Some.
    // That observation can never authenticate the original containment object.
    ensure!(
        !job.is_invalid(),
        "missing original control containment job"
    );
    let mut member = windows::core::BOOL::default();
    unsafe { IsProcessInJob(process, Some(job), &mut member) }
        .context("cannot verify control peer job membership")?;
    ensure!(
        member.as_bool(),
        "control peer is not in the original containment job"
    );
    Ok(())
}

fn ui_policy() -> JOB_OBJECT_UILIMIT {
    JOB_OBJECT_UILIMIT_READCLIPBOARD
        | JOB_OBJECT_UILIMIT_WRITECLIPBOARD
        | JOB_OBJECT_UILIMIT_HANDLES
        | JOB_OBJECT_UILIMIT_GLOBALATOMS
        | JOB_OBJECT_UILIMIT_SYSTEMPARAMETERS
        | JOB_OBJECT_UILIMIT_DISPLAYSETTINGS
        | JOB_OBJECT_UILIMIT_DESKTOP
        | JOB_OBJECT_UILIMIT_EXITWINDOWS
}

fn verify_policy(job: HANDLE) -> Result<()> {
    let mut limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
    let mut ui = JOBOBJECT_BASIC_UI_RESTRICTIONS::default();
    let mut returned = 0;
    unsafe {
        QueryInformationJobObject(
            Some(job),
            JobObjectExtendedLimitInformation,
            (&mut limits as *mut JOBOBJECT_EXTENDED_LIMIT_INFORMATION).cast(),
            size_of_val(&limits) as u32,
            Some(&mut returned),
        )?;
    }
    ensure!(
        returned as usize == size_of_val(&limits),
        "incomplete control job limits"
    );
    ensure!(
        limits.BasicLimitInformation.LimitFlags == JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
        "control job containment flags changed or permit breakaway"
    );
    returned = 0;
    unsafe {
        QueryInformationJobObject(
            Some(job),
            JobObjectBasicUIRestrictions,
            (&mut ui as *mut JOBOBJECT_BASIC_UI_RESTRICTIONS).cast(),
            size_of::<JOBOBJECT_BASIC_UI_RESTRICTIONS>() as u32,
            Some(&mut returned),
        )?;
    }
    ensure!(
        returned as usize == size_of_val(&ui),
        "incomplete control job UI limits"
    );
    ensure!(
        ui.UIRestrictionsClass == ui_policy(),
        "control job UI restrictions changed"
    );
    Ok(())
}

fn verify_security(job: HANDLE, owner: &str) -> Result<()> {
    let expected = OwnedSd::from_sddl(&format!(
        "O:{owner}D:P(A;;0x{JOB_ALL:x};;;SY)(A;;0x{JOB_ALL:x};;;{owner})"
    ))?;
    let mut actual = PSECURITY_DESCRIPTOR::default();
    win32_ok(
        unsafe {
            GetSecurityInfo(
                job,
                SE_KERNEL_OBJECT,
                OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION,
                None,
                None,
                None,
                None,
                Some(&mut actual),
            )
        },
        "inspect original control job security",
    )?;
    let actual = OwnedSd::from_raw(actual);
    ensure!(
        shape(actual.ptr)? == shape(expected.ptr)?,
        "control job owner/protected DACL differs from broker-only policy"
    );
    Ok(())
}

fn shape(raw: PSECURITY_DESCRIPTOR) -> Result<(String, u16, Vec<Vec<u8>>)> {
    let mut owner = PSID::default();
    let mut defaulted = false.into();
    let mut present = false.into();
    let mut dacl = std::ptr::null_mut();
    let mut control = 0;
    let mut revision = 0;
    unsafe {
        GetSecurityDescriptorOwner(raw, &mut owner, &mut defaulted)?;
        GetSecurityDescriptorControl(raw, &mut control, &mut revision)?;
        GetSecurityDescriptorDacl(raw, &mut present, &mut dacl, &mut defaulted)?;
    }
    ensure!(
        !owner.0.is_null() && present.as_bool() && !dacl.is_null(),
        "missing control job security"
    );
    let mut aces = Vec::new();
    crate::acl::filter_aces(dacl, |_, bytes| {
        aces.push(bytes.to_vec());
        true
    })?;
    // DACL present/defaulted, auto-inherit requested/inherited, protected.
    Ok((crate::sid::psid_to_string(owner)?, control & 0x150c, aces))
}

#[cfg(test)]
#[path = "account_control_job_tests.rs"]
pub(super) mod tests;
