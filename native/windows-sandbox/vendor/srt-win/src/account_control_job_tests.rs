//! Synthetic jobs and bounded disposable children only. No installed account,
//! host-job mutation, privilege enablement or resource-lifecycle authority.
use super::*;
use crate::account_control::ExpectedPeer;
use windows::Win32::{
    Foundation::{HANDLE_FLAG_INHERIT, HANDLE_FLAGS, SetHandleInformation, WAIT_OBJECT_0},
    Security::{
        Authorization::SetSecurityInfo, PROTECTED_DACL_SECURITY_INFORMATION,
        UNPROTECTED_DACL_SECURITY_INFORMATION,
    },
    System::Threading::{GetCurrentProcess, WaitForSingleObject},
};

#[test]
fn control_job_requires_native_success_and_exact_original_membership() {
    let job = Job::new(false).unwrap();
    let other = Job::new(false).unwrap();
    let child = crate::account_control::tests::suspended();
    assert!(JobBinding::observe(&job, child.process()).is_err());
    job.assign(child.process()).unwrap();
    let binding = JobBinding::observe(&job, child.process()).unwrap();
    binding.verify(child.process()).unwrap();
    assert!(JobBinding::observe(&other, child.process()).is_err());
    assert!(require_member(HANDLE::default(), job.raw()).is_err());
    assert!(require_member(child.process(), HANDLE::default()).is_err());
    assert!(require_member(unsafe { GetCurrentProcess() }, job.raw()).is_err());
    let peer = ExpectedPeer::retain(child.process())
        .unwrap()
        .with_job(&job)
        .unwrap();
    drop(child);
    assert!(peer.alive().is_err());
}

#[test]
fn control_job_rejects_initial_breakaway_and_missing_kill_policy() {
    let job = Job::new(true).unwrap();
    let child = crate::account_control::tests::suspended();
    job.assign(child.process()).unwrap();
    assert!(JobBinding::observe(&job, child.process()).is_err());
    // Only this disposable job is modified; never alter the containing host job.
    set_limits(&job, JOB_OBJECT_LIMIT(0));
    assert!(JobBinding::observe(&job, child.process()).is_err());
}

#[test]
fn control_job_each_native_policy_drift_is_refused() {
    for kind in [
        "breakaway",
        "silent-breakaway",
        "kill",
        "ui",
        "dacl",
        "inherit-dacl",
        "inherit-handle",
    ] {
        let job = Job::new(false).unwrap();
        let child = crate::account_control::tests::suspended();
        job.assign(child.process()).unwrap();
        let binding = JobBinding::observe(&job, child.process()).unwrap();
        with_fault(&job, kind, || {
            assert!(binding.verify(child.process()).is_err(), "accepted {kind}");
            assert!(
                JobBinding::observe(&job, child.process()).is_err(),
                "admitted {kind}"
            );
        });
        // A read-only observation is not itself a permanently poisoned channel.
        binding.verify(child.process()).unwrap();
    }
}

#[test]
fn control_job_security_compares_owner_and_exact_ace_shape() {
    let job = Job::new(false).unwrap();
    let owner = crate::sid::current_user_sid().unwrap();
    verify_security(job.raw(), &owner).unwrap();
    assert!(verify_security(job.raw(), "S-1-5-21-1-2-3-9999").is_err());
    let base = OwnedSd::from_sddl(&format!(
        "O:{owner}D:P(A;;0x{JOB_ALL:x};;;SY)(A;;0x{JOB_ALL:x};;;{owner})"
    ))
    .unwrap();
    for sddl in [
        format!("O:SYD:P(A;;0x{JOB_ALL:x};;;SY)(A;;0x{JOB_ALL:x};;;{owner})"),
        format!("O:{owner}D:P(A;;0x{JOB_ALL:x};;;SY)(A;;0x{JOB_ALL:x};;;{owner})(A;;RC;;;OW)"),
        format!("O:{owner}D:P(A;;0x{JOB_ALL:x};;;{owner})(A;;0x{JOB_ALL:x};;;SY)"),
        format!("O:{owner}D:(A;;0x{JOB_ALL:x};;;SY)(A;;0x{JOB_ALL:x};;;{owner})"),
    ] {
        let changed = OwnedSd::from_sddl(&sddl).unwrap();
        assert_ne!(shape(base.ptr).unwrap(), shape(changed.ptr).unwrap());
    }
}

#[test]
fn control_job_observation_keeps_no_extra_job_reference_and_owner_guard_still_reaps() {
    let job = Job::new(false).unwrap();
    let child = crate::account_control::tests::suspended();
    job.assign(child.process()).unwrap();
    let mut before = 0;
    unsafe {
        windows::Win32::System::Threading::GetProcessHandleCount(GetCurrentProcess(), &mut before)
            .unwrap()
    };
    let binding = JobBinding::observe(&job, child.process()).unwrap();
    let mut after = 0;
    unsafe {
        windows::Win32::System::Threading::GetProcessHandleCount(GetCurrentProcess(), &mut after)
            .unwrap()
    };
    assert_eq!(
        before, after,
        "observation retained an additional kernel handle"
    );
    binding.verify(child.process()).unwrap();
    drop(binding);
    // The process handle is deliberately retained for observation. Job::Drop
    // must still kill the assigned process, with no observer handle left over.
    drop(job);
    assert_eq!(
        unsafe { WaitForSingleObject(child.process(), 5000) },
        WAIT_OBJECT_0
    );
}

fn set_limits(job: &Job, flags: JOB_OBJECT_LIMIT) {
    let mut limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
    limits.BasicLimitInformation.LimitFlags = flags;
    unsafe {
        SetInformationJobObject(
            job.raw(),
            JobObjectExtendedLimitInformation,
            (&limits as *const JOBOBJECT_EXTENDED_LIMIT_INFORMATION).cast(),
            size_of_val(&limits) as u32,
        )
        .unwrap()
    };
}

fn set_ui(job: &Job, flags: JOB_OBJECT_UILIMIT) {
    let ui = JOBOBJECT_BASIC_UI_RESTRICTIONS {
        UIRestrictionsClass: flags,
    };
    unsafe {
        SetInformationJobObject(
            job.raw(),
            JobObjectBasicUIRestrictions,
            (&ui as *const JOBOBJECT_BASIC_UI_RESTRICTIONS).cast(),
            size_of_val(&ui) as u32,
        )
        .unwrap()
    };
}

fn set_dacl(job: &Job, descriptor: &OwnedSd, protected: bool) {
    let mut acl = std::ptr::null_mut();
    let mut present = false.into();
    let mut defaulted = false.into();
    unsafe {
        GetSecurityDescriptorDacl(descriptor.ptr, &mut present, &mut acl, &mut defaulted).unwrap()
    };
    assert!(present.as_bool() && !acl.is_null());
    win32_ok(
        unsafe {
            SetSecurityInfo(
                job.raw(),
                SE_KERNEL_OBJECT,
                DACL_SECURITY_INFORMATION
                    | if protected {
                        PROTECTED_DACL_SECURITY_INFORMATION
                    } else {
                        UNPROTECTED_DACL_SECURITY_INFORMATION
                    },
                None,
                None,
                Some(acl),
                None,
            )
        },
        "mutate disposable job DACL",
    )
    .unwrap();
}

/// Runs only against a newly owned fixture job whose strict policy is verified
/// first. Restoration is for the permanent-refusal test, never a production fix.
pub(crate) fn with_fault<T>(job: &Job, kind: &str, f: impl FnOnce() -> T) -> T {
    let owner = crate::sid::current_user_sid().unwrap();
    verify_policy(job.raw()).unwrap();
    verify_security(job.raw(), &owner).unwrap();
    let mut original = PSECURITY_DESCRIPTOR::default();
    win32_ok(
        unsafe {
            GetSecurityInfo(
                job.raw(),
                SE_KERNEL_OBJECT,
                DACL_SECURITY_INFORMATION,
                None,
                None,
                None,
                None,
                Some(&mut original),
            )
        },
        "retain fixture DACL",
    )
    .unwrap();
    let original = OwnedSd::from_raw(original);
    match kind {
        "breakaway" => set_limits(
            job,
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_BREAKAWAY_OK,
        ),
        "silent-breakaway" => set_limits(
            job,
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK,
        ),
        "kill" => set_limits(job, JOB_OBJECT_LIMIT(0)),
        "ui" => set_ui(job, JOB_OBJECT_UILIMIT(0)),
        "dacl" => set_dacl(
            job,
            &OwnedSd::from_sddl(&format!(
                "D:P(A;;0x{JOB_ALL:x};;;SY)(A;;0x{JOB_ALL:x};;;{owner})(A;;0x4;;;WD)"
            ))
            .unwrap(),
            true,
        ),
        "inherit-dacl" => set_dacl(job, &original, false),
        "inherit-handle" => unsafe {
            SetHandleInformation(job.raw(), HANDLE_FLAG_INHERIT.0, HANDLE_FLAG_INHERIT).unwrap()
        },
        _ => panic!("unknown fixture mutation"),
    }
    let result = f();
    match kind {
        "breakaway" | "silent-breakaway" | "kill" => {
            set_limits(job, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)
        }
        "ui" => set_ui(job, ui_policy()),
        "dacl" | "inherit-dacl" => set_dacl(job, &original, true),
        "inherit-handle" => unsafe {
            SetHandleInformation(job.raw(), HANDLE_FLAG_INHERIT.0, HANDLE_FLAGS(0)).unwrap()
        },
        _ => unreachable!(),
    }
    result
}
