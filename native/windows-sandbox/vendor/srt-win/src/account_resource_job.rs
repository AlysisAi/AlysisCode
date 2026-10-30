//! Retained empty-job prerequisite only. No assignment/launch/raw-handle API.
//! Kernel handles never become durable ownership tokens. A restarted broker
//! cannot reconstruct this guard, including when the recorded name is absent.

use super::*;
use crate::util::{OwnedHandle, OwnedSd, pcwstr, win32_ok, wstr};
use std::mem::{size_of, size_of_val};
use windows::Win32::{
    Foundation::{
        CloseHandle, ERROR_ALREADY_EXISTS, ERROR_FILE_NOT_FOUND, ERROR_NO_TOKEN,
        GetHandleInformation, GetLastError, HANDLE, HANDLE_FLAG_INHERIT,
    },
    Security::{
        Authorization::{GetSecurityInfo, SE_KERNEL_OBJECT},
        DACL_SECURITY_INFORMATION, GetSecurityDescriptorControl, GetSecurityDescriptorDacl,
        GetSecurityDescriptorOwner, OWNER_SECURITY_INFORMATION, PSECURITY_DESCRIPTOR, PSID,
        SECURITY_ATTRIBUTES, TOKEN_QUERY,
    },
    System::{
        JobObjects::*,
        RemoteDesktop::ProcessIdToSessionId,
        SystemServices::JOB_OBJECT_QUERY,
        Threading::{GetCurrentProcess, GetCurrentThread, OpenThreadToken},
    },
};

const ROLE: Resource = Resource::BootstrapJob;
// SDK winnt.h JOB_OBJECT_ALL_ACCESS = STANDARD_RIGHTS_REQUIRED | SYNCHRONIZE | 0x3f.
const JOB_ALL: u32 = 0x001f003f;
const POLICY_VERSION: u32 = 1;

/// No public constructor, raw handle, assignment or deserialization. Ownership
/// survives a database reopen, not a broker restart. Drop closes an owned
/// reference only; it never claims journal cleanup, calls TerminateJobObject,
/// or opens a name for termination. If a trusted external actor assigned a
/// process, dropping the last reference can invoke kernel kill-on-close. An
/// explicit retirement refusal retains the handle while the caller keeps it.
pub(crate) struct EmptyBootstrapJob {
    identity: Identity,
    create_nonce: String,
    phase: Phase,
    remove_nonce: Option<String>,
    #[cfg(test)]
    fault: Option<Fault>,
    #[cfg(test)]
    close_calls: usize,
}

enum Phase {
    Open(OwnedHandle),
    Closed,
    // CloseHandle was called once but its result was not trustworthy. Do not
    // retry a possibly reused handle value, including from Drop. The empty
    // resource and its durable intent remain unresolved until explicit repair.
    CloseUncertain,
}

#[cfg(test)]
#[derive(Clone, Copy, PartialEq)]
enum Fault {
    CreatedAfterCommit,
    RemoveIntentAfterCommit,
    RemovedAfterCommit,
}

impl ResourceJournal<'_> {
    /// Commit intent, exclusively create and verify, then return ownership.
    /// A separate acknowledgment keeps the guard with the caller if persisting
    /// successful birth fails. Failed/unverified birth leaves Creating intact.
    pub(crate) fn create_empty_bootstrap_job(&self) -> Result<EmptyBootstrapJob> {
        require_live(self)?;
        self.replay()?;
        verify_actor(&self.identity)?;
        ensure!(
            self.replay()?.states.is_empty(),
            "empty-job adapter requires a resource plan with no other effects"
        );
        let nonce = self.create_intent(ROLE, None)?;
        let handle = create_native(&self.identity)
            .context("empty job birth/configuration unverified; preserve creation intent")?;
        Ok(EmptyBootstrapJob {
            identity: self.identity.clone(),
            create_nonce: nonce,
            phase: Phase::Open(handle),
            remove_nonce: None,
            #[cfg(test)]
            fault: None,
            #[cfg(test)]
            close_calls: 0,
        })
    }

    pub(crate) fn record_empty_bootstrap_job(&self, guard: &mut EmptyBootstrapJob) -> Result<()> {
        require_live(self)?;
        guard.verify_identity(self)?;
        guard.verify_open()?;
        let event = guard.created_event();
        let replay = self.replay()?;
        match replay.states.get(&ROLE) {
            Some(State::Creating { nonce }) if nonce == &guard.create_nonce => {
                self.append(event.clone())?;
                #[cfg(test)]
                guard.fail(Fault::CreatedAfterCommit)?;
            }
            Some(State::Present { observed }) if observed == &observation() => {
                // append can commit then fail its readback. Match the exact
                // original nonce/event rather than accepting Present by name.
                ensure!(recorded(self, &event)?, "empty job birth receipt mismatch");
            }
            _ => anyhow::bail!("empty job birth has changed or ambiguous journal state"),
        }
        ensure!(
            recorded(self, &event)?,
            "empty job birth acknowledgment missing"
        );
        Ok(())
    }

    /// Original retained ownership and stopped authority are both required.
    /// This is deliberately not a cold-recovery-by-name API.
    pub(crate) fn retire_empty_bootstrap_job(&self, guard: &mut EmptyBootstrapJob) -> Result<()> {
        require_stopped(self)?;
        guard.verify_identity(self)?;
        ensure!(
            recorded(self, &guard.created_event())?,
            "empty job cannot retire without its exact committed birth"
        );
        let replay = self.replay()?;
        if matches!(replay.states.get(&ROLE), Some(State::Removed)) {
            let nonce = guard
                .remove_nonce
                .as_ref()
                .context("missing retained removal nonce")?;
            ensure!(
                matches!(guard.phase, Phase::Closed)
                    && recorded(
                        self,
                        &Event::Removed {
                            resource: ROLE,
                            nonce: nonce.clone()
                        }
                    )?,
                "removed state lacks the original retained close proof"
            );
            return Ok(());
        }
        ensure!(
            !matches!(guard.phase, Phase::CloseUncertain),
            "empty job close is uncertain; retain recovery state without retry"
        );
        match replay.states.get(&ROLE) {
            Some(State::Present { observed }) if observed == &observation() => {
                guard.verify_open()?;
                let nonce = match &guard.remove_nonce {
                    Some(nonce) => nonce.clone(),
                    None => {
                        let nonce = super::super::random_id()?;
                        guard.remove_nonce = Some(nonce.clone());
                        nonce
                    }
                };
                self.append(Event::RemoveIntent {
                    resource: ROLE,
                    nonce,
                })?;
                #[cfg(test)]
                guard.fail(Fault::RemoveIntentAfterCommit)?;
            }
            Some(State::Removing { nonce, observed })
                if observed == &observation() && guard.remove_nonce.as_ref() == Some(nonce) => {}
            _ => anyhow::bail!("empty job removal differs from retained operation"),
        }
        let nonce = guard
            .remove_nonce
            .clone()
            .context("missing retained removal nonce")?;
        ensure!(
            recorded(
                self,
                &Event::RemoveIntent {
                    resource: ROLE,
                    nonce: nonce.clone()
                }
            )?,
            "empty job removal intent is not durable"
        );
        if matches!(guard.phase, Phase::Open(_)) {
            // Recheck after journaling. An unexpected process, altered ACL or
            // policy never reaches this explicit retirement CloseHandle. Dropping
            // the caller-owned guard still closes its original reference.
            guard.verify_open()?;
            let Phase::Open(handle) = std::mem::replace(&mut guard.phase, Phase::CloseUncertain)
            else {
                unreachable!()
            };
            #[cfg(test)]
            {
                guard.close_calls += 1;
            }
            unsafe { CloseHandle(handle.into_raw()) }
                .context("empty job close unverified; do not retry the handle")?;
            guard.phase = Phase::Closed;
        }
        ensure!(
            matches!(guard.phase, Phase::Closed),
            "missing exact retained close proof"
        );
        require_absent(&guard.identity.name(ROLE))?;
        self.append(Event::Removed {
            resource: ROLE,
            nonce: nonce.clone(),
        })?;
        #[cfg(test)]
        guard.fail(Fault::RemovedAfterCommit)?;
        ensure!(
            recorded(
                self,
                &Event::Removed {
                    resource: ROLE,
                    nonce
                }
            )?,
            "empty job retirement acknowledgment missing"
        );
        Ok(())
    }
}

impl EmptyBootstrapJob {
    /// Operation data only, for a future trusted launch handoff. Possessing this
    /// nonce cannot reconstruct the original handle or commit an observation.
    pub(crate) fn birth_operation_nonce(&self) -> &str {
        &self.create_nonce
    }
    fn created_event(&self) -> Event {
        Event::Created {
            resource: ROLE,
            nonce: self.create_nonce.clone(),
            observed: observation(),
        }
    }
    fn verify_identity(&self, journal: &ResourceJournal<'_>) -> Result<()> {
        ensure!(
            self.identity == journal.identity,
            "empty job belongs to another execution/session"
        );
        verify_actor(&self.identity)?;
        let replay = journal.replay()?;
        ensure!(
            replay.states.keys().all(|role| *role == ROLE),
            "empty-job adapter cannot clean an execution with other resource effects"
        );
        ensure!(
            recorded(
                journal,
                &Event::CreateIntent {
                    resource: ROLE,
                    nonce: self.create_nonce.clone(),
                    subject: self.identity.subject(ROLE).into(),
                    name: self.identity.name(ROLE),
                    acl: None,
                }
            )?,
            "empty job original creation intent changed"
        );
        Ok(())
    }
    fn verify_open(&self) -> Result<()> {
        let Phase::Open(handle) = &self.phase else {
            anyhow::bail!("empty job has no retained open ownership handle");
        };
        verify_native(handle.raw(), &self.identity)
    }
    #[cfg(test)]
    fn fail(&mut self, point: Fault) -> Result<()> {
        if self.fault == Some(point) {
            self.fault = None;
            anyhow::bail!("injected failure after durable empty-job transition");
        }
        Ok(())
    }
}

impl super::control::ResourceControl<'_> {
    /// Receive an exact confirmation request, verify the retained native job,
    /// commit/readback its birth, then acknowledge. No native creation or retry
    /// is driven by wire input. A lost/failed acknowledgment preserves the job
    /// guard and journal; it never rolls back the resource or repeats its birth.
    pub(crate) fn acknowledge_empty_bootstrap_job_birth(
        &mut self,
        journal: &ResourceJournal<'_>,
        guard: &mut EmptyBootstrapJob,
        timeout: std::time::Duration,
    ) -> Result<()> {
        let reply = self.begin_empty_job_birth(journal, &guard.create_nonce, timeout)?;
        journal.record_empty_bootstrap_job(guard)?;
        // Journal I/O may take time. Recheck the original native resource after
        // commit, before reporting a successful birth over the control pipe.
        guard.verify_identity(journal)?;
        guard.verify_open()?;
        self.finish_empty_job_birth(reply, journal, timeout)
    }
}

fn require_live(journal: &ResourceJournal<'_>) -> Result<()> {
    match &journal.authority {
        Authority::Live(_) => Ok(()),
        #[cfg(test)]
        Authority::FixtureLive => Ok(()),
        _ => anyhow::bail!("empty job birth requires live execution authority"),
    }
}
fn require_stopped(journal: &ResourceJournal<'_>) -> Result<()> {
    match &journal.authority {
        Authority::Stopped(_) => Ok(()),
        #[cfg(test)]
        Authority::FixtureStopped => Ok(()),
        _ => anyhow::bail!("empty job retirement requires stopped execution authority"),
    }
}

fn observation() -> Observation {
    Observation::EmptyBootstrapJob {
        policy_version: POLICY_VERSION,
    }
}

pub(super) fn recorded(journal: &ResourceJournal<'_>, expected: &Event) -> Result<bool> {
    // replay validates identity, contiguity, counts and byte bounds before this
    // bounded exact-history comparison; names or generic Present never suffice.
    journal.replay()?;
    let mut statement = journal.connection.prepare(
        "SELECT event_json FROM candidate_resource_events WHERE container=?1 ORDER BY sequence LIMIT 129")?;
    let rows = statement.query_map([&journal.identity.execution.identity.container], |row| {
        row.get::<_, String>(0)
    })?;
    let mut found = false;
    for (index, row) in rows.enumerate() {
        let json = row?;
        ensure!(
            index < MAX_EVENTS && json.len() <= MAX_EVENT_BYTES,
            "resource history changed bounds"
        );
        found |= serde_json::from_str::<Event>(&json)? == *expected;
    }
    Ok(found)
}

pub(super) fn verify_actor(identity: &Identity) -> Result<()> {
    identity.validate()?;
    ensure!(
        crate::sid::current_user_sid()? == identity.execution.owner
            && std::process::id() == identity.execution.pid
            && super::super::process_created(unsafe { GetCurrentProcess() })?
                == identity.execution.created,
        "empty job requires its original host broker identity"
    );
    let mut session = 0;
    unsafe {
        ProcessIdToSessionId(std::process::id(), &mut session)?;
    }
    ensure!(
        session == identity.session_id,
        "empty job broker session changed"
    );
    let mut token = HANDLE::default();
    match unsafe { OpenThreadToken(GetCurrentThread(), TOKEN_QUERY, true, &mut token) } {
        Ok(()) => {
            drop(OwnedHandle(token));
            anyhow::bail!("empty job broker must not impersonate");
        }
        Err(error) if error.code() == ERROR_NO_TOKEN.to_hresult() => Ok(()),
        Err(error) => Err(error).context("cannot verify empty job broker impersonation state"),
    }
}

fn descriptor(identity: &Identity) -> Result<OwnedSd> {
    let owner = &identity.execution.owner;
    OwnedSd::from_sddl(&format!(
        "O:{owner}D:P(A;;0x{JOB_ALL:x};;;SY)(A;;0x{JOB_ALL:x};;;{owner})"
    ))
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

fn create_native(identity: &Identity) -> Result<OwnedHandle> {
    let descriptor = descriptor(identity)?;
    let security = SECURITY_ATTRIBUTES {
        nLength: size_of::<SECURITY_ATTRIBUTES>() as u32,
        lpSecurityDescriptor: descriptor.ptr.0,
        bInheritHandle: false.into(),
    };
    let name = wstr(&identity.name(ROLE));
    let raw = unsafe { CreateJobObjectW(Some(&security), pcwstr(&name)) }?;
    let existed = unsafe { GetLastError() } == ERROR_ALREADY_EXISTS;
    if existed {
        // This reference is foreign: never configure it or use Job::Drop.
        unsafe { CloseHandle(raw) }.context("release colliding job reference")?;
        anyhow::bail!("empty bootstrap job name already exists; refusing adoption");
    }
    let handle = OwnedHandle(raw);
    let mut limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
    let ui = JOBOBJECT_BASIC_UI_RESTRICTIONS {
        UIRestrictionsClass: ui_policy(),
    };
    unsafe {
        SetInformationJobObject(
            handle.raw(),
            JobObjectExtendedLimitInformation,
            (&limits as *const JOBOBJECT_EXTENDED_LIMIT_INFORMATION).cast(),
            size_of_val(&limits) as u32,
        )?;
        SetInformationJobObject(
            handle.raw(),
            JobObjectBasicUIRestrictions,
            (&ui as *const JOBOBJECT_BASIC_UI_RESTRICTIONS).cast(),
            size_of_val(&ui) as u32,
        )?;
    }
    verify_native(handle.raw(), identity)?;
    Ok(handle)
}

fn verify_native(handle: HANDLE, identity: &Identity) -> Result<()> {
    let mut flags = 0;
    unsafe {
        GetHandleInformation(handle, &mut flags)?;
    }
    ensure!(
        flags & HANDLE_FLAG_INHERIT.0 == 0,
        "empty job handle is inheritable"
    );
    let expected = descriptor(identity)?;
    let mut actual = PSECURITY_DESCRIPTOR::default();
    win32_ok(
        unsafe {
            GetSecurityInfo(
                handle,
                SE_KERNEL_OBJECT,
                OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION,
                None,
                None,
                None,
                None,
                Some(&mut actual),
            )
        },
        "inspect retained empty job security",
    )?;
    let actual = OwnedSd::from_raw(actual);
    ensure!(
        security_shape(actual.ptr)? == security_shape(expected.ptr)?,
        "empty job owner/protected DACL differs from exact host/SYSTEM policy"
    );
    let mut limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
    let mut ui = JOBOBJECT_BASIC_UI_RESTRICTIONS::default();
    let mut accounting = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION::default();
    unsafe {
        QueryInformationJobObject(
            Some(handle),
            JobObjectExtendedLimitInformation,
            (&mut limits as *mut JOBOBJECT_EXTENDED_LIMIT_INFORMATION).cast(),
            size_of_val(&limits) as u32,
            None,
        )?;
        QueryInformationJobObject(
            Some(handle),
            JobObjectBasicUIRestrictions,
            (&mut ui as *mut JOBOBJECT_BASIC_UI_RESTRICTIONS).cast(),
            size_of_val(&ui) as u32,
            None,
        )?;
        QueryInformationJobObject(
            Some(handle),
            JobObjectBasicAccountingInformation,
            (&mut accounting as *mut JOBOBJECT_BASIC_ACCOUNTING_INFORMATION).cast(),
            size_of_val(&accounting) as u32,
            None,
        )?;
    }
    ensure!(
        limits.BasicLimitInformation.LimitFlags == JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
        "empty job containment flags changed"
    );
    ensure!(
        ui.UIRestrictionsClass == ui_policy(),
        "empty job UI restrictions changed"
    );
    require_empty_history(&accounting)
}

fn require_empty_history(accounting: &JOBOBJECT_BASIC_ACCOUNTING_INFORMATION) -> Result<()> {
    ensure!(
        accounting.TotalProcesses == 0 && accounting.ActiveProcesses == 0,
        "empty-job adapter observed process history; preserve recovery state"
    );
    Ok(())
}

fn security_shape(raw: PSECURITY_DESCRIPTOR) -> Result<(String, u16, Vec<Vec<u8>>)> {
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
        "missing empty job security"
    );
    let mut aces = Vec::new();
    crate::acl::filter_aces(dacl, |_, bytes| {
        aces.push(bytes.to_vec());
        true
    })?;
    Ok((crate::sid::psid_to_string(owner)?, control & 0x150c, aces))
}

fn require_absent(name: &str) -> Result<()> {
    match unsafe { OpenJobObjectW(JOB_OBJECT_QUERY, false, pcwstr(&wstr(name))) } {
        Ok(handle) => {
            drop(OwnedHandle(handle));
            anyhow::bail!("job name remains present; retain close proof and removal intent");
        }
        Err(error) if error.code() == ERROR_FILE_NOT_FOUND.to_hresult() => Ok(()),
        Err(error) => {
            Err(error).context("job absence unproven; retain close proof and removal intent")
        }
    }
}

#[cfg(test)]
#[path = "account_resource_job_tests.rs"]
mod tests;
