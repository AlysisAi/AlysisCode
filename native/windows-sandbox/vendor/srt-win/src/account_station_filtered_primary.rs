//! Sealed test-only primary-token comparison. No caller-selected executable,
//! token repair, network operation, permission change or production authority.
use super::*;
use crate::account_token::runner_policy::filtered;

const TEST: &str = "account_control::station_lowbox_fixture::native::filtered_primary::station_filtered_primary_child";
const EXIT: u32 = 0x4180;
// Source-pinned second control. Lower only a private duplicate; the original
// filtered base and actual lowbox stay unchanged. This cannot qualify production.
const INTEGRITY_RID: u32 = 0x1000;
const STARTED: &[u8] = b"alysis-filtered-primary-started-v1";

#[derive(Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
enum Stage {
    Base,
    Prepare,
    Create,
    Suspended,
    Resume,
    Loader,
    Started,
    Report,
    Acknowledge,
    Exit,
    Complete,
}
#[derive(Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
enum ChildStage {
    Identity,
    Parent,
    UserPair,
    Schannel,
    Complete,
}
#[derive(Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Subject {
    original: ChildRequest,
    parent_stamp: Stamp,
    token_stamp: Stamp,
}
impl Subject {
    fn require(&self) -> Result<()> {
        self.original.require()?;
        ensure!(
            self.token_stamp.0 != [0; 2]
                && self.parent_stamp.0 != [0; 2]
                && self.token_stamp.1 == self.parent_stamp.1
                && self.original.authentication_id == self.parent_stamp.1,
            "filtered control token or parent binding differs"
        );
        Ok(())
    }
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct ChildObservation {
    stage: ChildStage,
    hresult: Option<i32>,
    primary_verified: bool,
    parent_verified: bool,
    user_pair_verified: bool,
    result: Option<schannel::Proof>,
}
impl ChildObservation {
    fn new() -> Self {
        Self {
            stage: ChildStage::Identity,
            hresult: None,
            primary_verified: false,
            parent_verified: false,
            user_pair_verified: false,
            result: None,
        }
    }
    fn require(&self) -> Result<()> {
        ensure!(
            !self.parent_verified || self.primary_verified,
            "parent proof precedes identity"
        );
        ensure!(
            !self.user_pair_verified || self.parent_verified,
            "USER proof precedes parent"
        );
        if let Some(result) = &self.result {
            result.require()?;
            ensure!(
                self.user_pair_verified,
                "Schannel preceded fixed subject checks"
            );
        }
        ensure!(
            (self.stage == ChildStage::Complete) == self.hresult.is_none(),
            "filtered child completion/error differs"
        );
        ensure!(
            self.hresult.is_none_or(|code| code < 0),
            "filtered child error is not a failure"
        );
        let expected = match self.stage {
            ChildStage::Identity => (false, false, false),
            ChildStage::Parent => (true, false, false),
            ChildStage::UserPair => (true, true, false),
            ChildStage::Schannel | ChildStage::Complete => (true, true, true),
        };
        ensure!(
            (
                self.primary_verified,
                self.parent_verified,
                self.user_pair_verified
            ) == expected,
            "filtered child stage contradicts admission checks"
        );
        if self.stage == ChildStage::Complete {
            ensure!(
                self.result.is_some()
                    && self.primary_verified
                    && self.parent_verified
                    && self.user_pair_verified,
                "filtered child completion incomplete"
            );
        }
        Ok(())
    }
    fn exit_code(&self) -> u32 {
        if self.stage == ChildStage::Complete && self.result.as_ref().is_some_and(|p| p.passed) {
            EXIT
        } else {
            EXIT + 1
        }
    }
}
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Report {
    subject: Subject,
    observation: ChildObservation,
}

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Observation {
    diagnostic_only: bool,
    integrity_rid: u32,
    stage: Stage,
    hresult: Option<i32>,
    base_stamp: Option<Stamp>,
    selected_stamp: Option<Stamp>,
    child_born: bool,
    original_child: Option<(u32, u64)>,
    subject: Option<Subject>,
    suspended_verified: bool,
    inherited_job_verified: bool,
    startup: Option<startup::Probe>,
    child_started: bool,
    failure: Option<ChildFailure>,
    #[serde(default)]
    loader_comparison: bool,
    #[serde(default)]
    loader: Option<loader::LoaderTrace>,
    child: Option<ChildObservation>,
    checked_exit: Option<u32>,
    child_drained: bool,
}
impl Observation {
    fn new() -> Self {
        Self {
            diagnostic_only: true,
            integrity_rid: INTEGRITY_RID,
            stage: Stage::Base,
            hresult: None,
            base_stamp: None,
            selected_stamp: None,
            child_born: false,
            original_child: None,
            subject: None,
            suspended_verified: false,
            inherited_job_verified: false,
            startup: None,
            child_started: false,
            failure: None,
            loader_comparison: false,
            loader: None,
            child: None,
            checked_exit: None,
            child_drained: false,
        }
    }
    pub(crate) fn require(&self, receipt: &Receipt) -> Result<()> {
        ensure!(
            self.diagnostic_only
                && self.integrity_rid == INTEGRITY_RID
                && !receipt.passed
                && receipt.original_child_exit.is_some()
                && receipt.child.as_ref().is_some_and(|p| p.passed)
                && receipt.desktop_child.as_ref().is_some_and(|p| p.passed)
                && receipt.schannel_child.is_some(),
            "filtered control cannot precede original USER proofs/exit or qualify containment"
        );
        ensure!(
            (self.stage == Stage::Complete) == self.hresult.is_none(),
            "filtered control completion/error differs"
        );
        ensure!(
            self.hresult.is_none_or(|code| code < 0) && self.child_drained == self.child_born,
            "filtered control error/drain evidence differs"
        );
        ensure!(
            !self.child_born
                || matches!(
                    self.stage,
                    Stage::Create
                        | Stage::Suspended
                        | Stage::Resume
                        | Stage::Loader
                        | Stage::Started
                        | Stage::Report
                        | Stage::Acknowledge
                        | Stage::Exit
                        | Stage::Complete
                ),
            "filtered control birth precedes create stage"
        );
        ensure!(
            self.stage == Stage::Base || self.base_stamp.is_some(),
            "filtered stage lacks base admission"
        );
        if matches!(
            self.stage,
            Stage::Suspended
                | Stage::Resume
                | Stage::Loader
                | Stage::Started
                | Stage::Report
                | Stage::Acknowledge
                | Stage::Exit
                | Stage::Complete
        ) {
            ensure!(
                self.original_child.is_some(),
                "filtered stage lacks original process identity"
            );
        }
        ensure!(
            self.suspended_verified == self.inherited_job_verified,
            "filtered suspended and job admission differ"
        );
        if matches!(
            self.stage,
            Stage::Resume
                | Stage::Loader
                | Stage::Started
                | Stage::Report
                | Stage::Acknowledge
                | Stage::Exit
                | Stage::Complete
        ) {
            ensure!(
                self.suspended_verified,
                "filtered stage preceded suspended admission"
            );
        }
        if matches!(
            self.stage,
            Stage::Acknowledge | Stage::Exit | Stage::Complete
        ) {
            ensure!(self.child.is_some(), "filtered terminal stage lacks report");
        }
        if matches!(
            self.stage,
            Stage::Report | Stage::Acknowledge | Stage::Exit | Stage::Complete
        ) {
            ensure!(self.child_started, "filtered report preceded entry marker");
        }
        ensure!(
            !self.child_started || self.suspended_verified,
            "filtered entry preceded suspended admission"
        );
        if let Some(probe) = &self.startup {
            ensure!(
                self.suspended_verified,
                "filtered startup probe precedes admission"
            );
            probe.require(
                self.original_child
                    .context("filtered startup child absent")?,
            )?;
        }
        if matches!(
            self.stage,
            Stage::Resume
                | Stage::Loader
                | Stage::Started
                | Stage::Report
                | Stage::Acknowledge
                | Stage::Exit
                | Stage::Complete
        ) {
            ensure!(
                self.startup.is_some(),
                "filtered resume lacks startup observation"
            );
        }
        if self.failure.is_some() {
            ensure!(
                self.child_born && self.stage != Stage::Complete && self.checked_exit.is_none(),
                "filtered early-exit diagnostic cannot qualify completion"
            );
        }
        ensure!(
            !self.loader_comparison
                || (FILTERED_LOADER_COMPARISON
                    && receipt.loader.is_none()
                    && receipt.inherited_user.is_none()
                    && self.stage != Stage::Complete
                    && self.child.is_none()
                    && !self.child_started
                    && self.checked_exit.is_none()),
            "filtered loader comparison cannot claim normal child completion or mix original instrumentation"
        );
        if let Some(trace) = &self.loader {
            trace.require()?;
            ensure!(
                self.loader_comparison
                    && self.stage == Stage::Loader
                    && self.suspended_verified
                    && self.original_child == Some((trace.original_pid, trace.original_created))
                    && serde_json::to_vec(trace)?.len() <= loader::FILTERED_MAX_WIRE,
                "filtered loader evidence differs from original comparison child or budget"
            );
        }
        ensure!(
            self.stage != Stage::Loader || self.loader.is_some(),
            "filtered loader stage lacks bound trace"
        );
        if let Some(base) = self.base_stamp {
            ensure!(
                base.0 != [0; 2] && base.1 == receipt.token_stamp.1,
                "filtered base logon differs from original account"
            );
        }
        if let Some(selected) = self.selected_stamp {
            ensure!(
                self.base_stamp
                    .is_some_and(|base| base.0 != selected.0 && base.1 == selected.1)
                    && selected.0 != [0; 2],
                "lowered private token is not distinct or changed logon"
            );
        }
        ensure!(
            !self.child_born || self.selected_stamp.is_some(),
            "control birth lacks lowered token admission"
        );
        if let Some(original) = self.original_child {
            require_reference(&receipt.request, (1, original.0, original.1))?;
            ensure!(
                Some(original) != receipt.original_child && self.child_born && self.child_drained,
                "comparison child reused baseline identity or was not drained"
            );
        }
        if let Some(subject) = &self.subject {
            subject.require()?;
            let original = &subject.original;
            ensure!(
                original.request == receipt.request
                    && subject.parent_stamp == receipt.token_stamp
                    && Some((original.pid, original.created)) == self.original_child
                    && Some(&original.station) == receipt.station_name.as_ref()
                    && Some(&original.desktop) == receipt.desktop_name.as_ref()
                    && Some(&original.descriptor) == receipt.station_before.as_ref()
                    && Some(&original.desktop_descriptor) == receipt.desktop_before.as_ref()
                    && self.base_stamp.is_some(),
                "filtered subject differs from original fixture"
            );
        }
        ensure!(
            !self.suspended_verified || self.subject.is_some(),
            "suspended proof lacks subject"
        );
        if let Some(child) = &self.child {
            child.require()?;
            ensure!(
                self.suspended_verified && self.inherited_job_verified,
                "filtered report precedes suspended admission"
            );
        }
        if let Some(code) = self.checked_exit {
            ensure!(
                self.child.as_ref().is_some_and(|p| p.exit_code() == code) && self.child_drained,
                "filtered exit does not match bounded report"
            );
            ensure!(
                self.stage == Stage::Complete,
                "filtered checked exit precedes completion"
            );
        }
        if self.stage == Stage::Complete {
            ensure!(
                self.checked_exit.is_some() && self.child_drained,
                "filtered comparison completion lacks checked exit"
            );
        }
        Ok(())
    }
}
fn failure(error: &anyhow::Error) -> i32 {
    native_code(error).unwrap_or(E_FAIL.0)
}

fn token(process: HANDLE, subject: &ChildRequest) -> Result<Stamp> {
    crate::account_impersonation::require_process_identity()?;
    let mut raw = HANDLE::default();
    unsafe {
        OpenProcessToken(process, TOKEN_QUERY, &mut raw)?;
    }
    let raw = OwnedHandle(raw);
    filtered::require_low(
        raw.raw(),
        &subject.request.birth.selection.account.account_sid,
        subject.request.birth.selection.session,
        subject.authentication_id,
    )
}
fn in_job(process: HANDLE) -> Result<()> {
    let mut value = windows::core::BOOL::default();
    unsafe {
        IsProcessInJob(process, None, &mut value)?;
    }
    ensure!(value.as_bool(), "filtered comparison escaped original job");
    Ok(())
}
fn finished(subject: &Subject) -> Result<Vec<u8>> {
    subject.require()?;
    encode(&("filtered-primary-finished-v1", subject))
}

fn lowered_copy(base: HANDLE) -> Result<OwnedHandle> {
    crate::account_impersonation::require_process_identity()?;
    let mut raw = HANDLE::default();
    unsafe {
        DuplicateTokenEx(
            base,
            TOKEN_QUERY | TOKEN_DUPLICATE | TOKEN_ASSIGN_PRIMARY | TOKEN_ADJUST_DEFAULT,
            None,
            SecurityImpersonation,
            TokenPrimary,
            &mut raw,
        )?;
    }
    let copy = OwnedHandle(raw);
    noninheritable(copy.raw())?;
    let sid = crate::sid::LocalPsid::from_string("S-1-16-4096")?;
    let label = TOKEN_MANDATORY_LABEL {
        Label: SID_AND_ATTRIBUTES {
            Sid: sid.as_psid(),
            Attributes: 0x20, // SE_GROUP_INTEGRITY
        },
    };
    unsafe {
        SetTokenInformation(
            copy.raw(),
            TokenIntegrityLevel,
            (&label as *const TOKEN_MANDATORY_LABEL).cast(),
            size_of::<TOKEN_MANDATORY_LABEL>() as u32 + GetLengthSid(sid.as_psid()),
        )?;
    }
    Ok(copy)
}

// Returns only after its armed child guard has terminated/waited on every
// failure. The outer UserPair and profile/job pins still live at this point.
pub(super) fn observe(
    original: &ChildRequest,
    parent_stamp: Stamp,
    base: &OwnedHandle,
    image: &AccountImageObserver,
    users: &UserPair,
) -> Observation {
    let mut observation = Observation::new();
    let result = (|| -> Result<()> {
        original.require()?;
        crate::account_impersonation::require_process_identity()?;
        observation.base_stamp = Some(filtered::require(
            base.raw(),
            &original.request.birth.selection.account.account_sid,
            original.request.birth.selection.session,
            original.authentication_id,
        )?);
        image.verify_current()?;
        let current = ExpectedPeer::retain(unsafe { GetCurrentProcess() })?;
        ensure!(
            current.pid == original.request.pid
                && bootstrap::pull::creation_time(current.process.raw())?
                    == original.request.created
                && current.token.fixture_stamp(current.process.raw())? == parent_stamp,
            "filtered comparison parent changed"
        );
        in_job(current.process.raw())?;
        observation.stage = Stage::Prepare;
        let selected = lowered_copy(base.raw())?;
        observation.selected_stamp = Some(filtered::require_low(
            selected.raw(),
            &original.request.birth.selection.account.account_sid,
            original.request.birth.selection.session,
            original.authentication_id,
        )?);
        ensure!(
            Some(filtered::require(
                base.raw(),
                &original.request.birth.selection.account.account_sid,
                original.request.birth.selection.session,
                original.authentication_id
            )?) == observation.base_stamp,
            "lowered copy changed original filtered base"
        );
        noninheritable(HANDLE(users.station.raw()?.0))?;
        noninheritable(HANDLE(
            users.desktop.0.context("original desktop closed")?.0,
        ))?;
        let (endpoint, inherited) =
            endpoints(&original.request.birth.selection.account.account_sid)?;
        let app = std::env::current_exe()?;
        let app = app.to_str().context("filtered fixed image not Unicode")?;
        let (_, debug_loader) = loader_targets(
            std::path::Path::new(app)
                .file_name()
                .and_then(|n| n.to_str())
                .context("filtered image name invalid")?,
        )?;
        observation.loader_comparison = debug_loader;
        let mut command = wstr(&format!(
            "{} --exact {TEST} --ignored --nocapture --test-threads=1",
            crate::launch::quote_arg(app)
        ));
        use crate::account_state::execution::bootstrap_plan::initializer_image::known_profile_folder;
        use windows::Win32::UI::Shell::{
            FOLDERID_LocalAppData, FOLDERID_Profile, FOLDERID_RoamingAppData,
        };
        let directory = bootstrap::os_directory(false)?;
        let mut environment = child_environment(
            bootstrap::os_directory(true)?,
            directory.clone(),
            known_profile_folder(&FOLDERID_LocalAppData)?,
            known_profile_folder(&FOLDERID_Profile)?,
            known_profile_folder(&FOLDERID_RoamingAppData)?,
            inherited.raw().0 as usize,
        )?;
        let mut attrs = ProcThreadAttrs::new(1)?;
        attrs.set_handle_list(&[inherited.raw()])?;
        let mut desktop = wstr(&format!("{}\\{}", original.station, original.desktop));
        let mut startup = STARTUPINFOEXW::default();
        startup.StartupInfo.cb = size_of::<STARTUPINFOEXW>() as u32;
        startup.StartupInfo.lpDesktop = PWSTR(desktop.as_mut_ptr());
        startup.lpAttributeList = attrs.list();
        ensure!(
            unsafe { GetErrorMode() } & 3 == 3,
            "filtered parent headless mode differs"
        );
        image.verify_current()?;
        observation.stage = Stage::Create;
        let mut info = PROCESS_INFORMATION::default();
        unsafe {
            CreateProcessAsUserW(
                Some(selected.raw()),
                pcwstr(&wstr(app)),
                Some(PWSTR(command.as_mut_ptr())),
                None,
                None,
                true,
                CREATE_SUSPENDED
                    | CREATE_NO_WINDOW
                    | CREATE_UNICODE_ENVIRONMENT
                    | EXTENDED_STARTUPINFO_PRESENT
                    | if debug_loader {
                        DEBUG_ONLY_THIS_PROCESS
                    } else {
                        PROCESS_CREATION_FLAGS(0)
                    },
                Some(environment.as_mut_ptr().cast()),
                pcwstr(&wstr(&directory)),
                &startup.StartupInfo,
                &mut info,
            )?;
        }
        let debug_birth = debug_loader.then(|| loader::DebugBirth::new(info));
        let ordinary_birth = (!debug_loader).then(|| SpawnedChild::new(info));
        let child = debug_birth
            .as_ref()
            .map(loader::DebugBirth::child)
            .or(ordinary_birth.as_ref())
            .context("filtered birth owner absent")?;
        observation.child_born = true;
        let child_result = (|| -> Result<()> {
            // Declared after both birth owners: detach/restore always precedes
            // independent SpawnedChild drain, including suspended-check errors.
            let mut debug = if let Some(birth) = &debug_birth {
                Some(loader::Guard::new_filtered(
                    birth,
                    users
                        .desktop
                        .0
                        .as_ref()
                        .context("original desktop closed")?,
                )?)
            } else {
                None
            };
            drop(inherited);
            observation.original_child = Some((
                unsafe { GetProcessId(child.process()) },
                bootstrap::pull::creation_time(child.process())?,
            ));
            observation.stage = Stage::Suspended;
            let reference = observation.original_child.unwrap();
            require_reference(
                &original.request,
                (child.process().0 as usize as u64, reference.0, reference.1),
            )?;
            ensure!(
                reference != (original.pid, original.created),
                "comparison reused original lowbox identity"
            );
            let mut selected = original.clone();
            selected.pid = reference.0;
            selected.created = reference.1;
            let subject = Subject {
                token_stamp: token(child.process(), &selected)?,
                original: selected,
                parent_stamp,
            };
            subject.require()?;
            image.verify_process(child.process())?;
            in_job(child.process())?;
            current.alive()?;
            observation.inherited_job_verified = true; // no BREAKAWAY flag; original parent already in its retained job
            observation.subject = Some(subject.clone());
            observation.suspended_verified = true;
            // Observe this actual suspended primary's self-access, not the
            // pre-birth copy. All opens are read-only observations; no DACL,
            // label, mitigation or token policy is changed on failure.
            let mut actual = HANDLE::default();
            unsafe {
                OpenProcessToken(
                    child.process(),
                    TOKEN_QUERY | TOKEN_DUPLICATE | TOKEN_ACCESS_MASK(READ_CONTROL.0),
                    &mut actual,
                )?;
            }
            let actual = OwnedHandle(actual);
            noninheritable(actual.raw())?;
            observation.startup = Some(startup::Probe::observe(
                child.process(),
                &actual,
                reference,
            )?);
            ensure!(
                token(child.process(), &subject.original)? == subject.token_stamp
                    && current.token.fixture_stamp(current.process.raw())? == parent_stamp,
                "filtered startup observation changed child or caller primary"
            );
            drop(actual);
            observation.stage = Stage::Resume;
            ensure!(
                unsafe { ResumeThread(child.thread()) } == 1,
                "filtered thread was not singly suspended"
            );
            if let Some(mut debug) = debug.take() {
                observation.stage = Stage::Loader;
                let mut trace = loader::LoaderTrace::new();
                let captured = debug.observe(&mut trace);
                let compacted = trace.compact_filtered();
                observation.loader = Some(trace); // retain before debugger cleanup
                drop(debug);
                compacted?;
                captured?;
                anyhow::bail!("filtered loader trace is diagnostic only; no child completion");
            }
            let end = deadline(TIMEOUT)?;
            observation.stage = Stage::Started;
            ensure!(
                endpoint.receive_before::<Vec<u8>>(end)? == STARTED,
                "filtered entry marker differs"
            );
            observation.child_started = true;
            endpoint.send(&subject)?;
            observation.stage = Stage::Report;
            let report: Report = endpoint.receive_before(end)?;
            ensure!(
                report.subject == subject,
                "filtered child report rebound subject"
            );
            report.observation.require()?;
            ensure!(
                token(child.process(), &subject.original)? == subject.token_stamp,
                "filtered child primary changed during Schannel probe"
            );
            image.verify_process(child.process())?;
            current.alive()?;
            let expected = report.observation.exit_code();
            observation.child = Some(report.observation);
            observation.stage = Stage::Acknowledge;
            endpoint.send(&finished(&subject)?)?;
            observation.stage = Stage::Exit;
            exited(child.process(), reference, expected)?;
            observation.checked_exit = Some(expected);
            Ok(())
        })();
        if child_result.is_err() {
            // Snapshot original process/thread before armed drain changes state.
            // These statuses are diagnostic only; never cleanup authority.
            observation.failure = Some(observe_failed_child(&child));
        }
        child_result?;
        drop(ordinary_birth);
        drop(debug_birth);
        observation.stage = Stage::Complete;
        Ok(())
    })();
    // An error unwinds the closure's SpawnedChild before reaching this point.
    // SpawnedChild aborts the process if original-child drain cannot be proven.
    observation.child_drained = observation.child_born;
    if let Err(error) = result {
        observation.hresult = Some(failure(&error));
    }
    observation
}

fn image_path(process: HANDLE) -> Result<Vec<u16>> {
    let mut path = vec![0u16; 32768];
    let mut length = path.len() as u32;
    unsafe {
        QueryFullProcessImageNameW(
            process,
            PROCESS_NAME_WIN32,
            PWSTR(path.as_mut_ptr()),
            &mut length,
        )?;
    }
    ensure!(
        length > 0 && (length as usize) < path.len(),
        "fixed image path exceeds bound"
    );
    path.truncate(length as usize);
    Ok(path)
}
fn parent(subject: &Subject, endpoint: &Endpoint) -> Result<ExpectedPeer<'static>> {
    let mut pid = 0;
    unsafe {
        GetNamedPipeServerProcessId(endpoint.0.raw(), &mut pid)?;
    }
    ensure!(
        pid == subject.original.request.pid,
        "filtered inherited pipe parent differs"
    );
    // This PID comes from the connected inherited pipe, never from a requested
    // launch target. Pin it immediately and compare the authenticated birth.
    let process = OwnedHandle(unsafe {
        OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SYNCHRONIZE,
            false,
            pid,
        )?
    });
    let peer = ExpectedPeer::retain(process.raw())?;
    ensure!(
        bootstrap::pull::creation_time(peer.process.raw())? == subject.original.request.created
            && peer.token.fixture_stamp(peer.process.raw())? == subject.parent_stamp,
        "filtered pipe parent creation or token differs"
    );
    let selected = &subject.original.request.birth.selection;
    peer.require_profile_runner(
        &selected.account.account_sid,
        selected.session,
        &selected.account.group_sid,
    )?;
    ensure!(
        image_path(peer.process.raw())? == image_path(unsafe { GetCurrentProcess() })?,
        "filtered parent and fixed child images differ"
    );
    peer.alive()?;
    Ok(peer)
}
fn user_pair(subject: &ChildRequest) -> Result<()> {
    let attached = unsafe { GetProcessWindowStation()? };
    let desktop = unsafe { GetThreadDesktop(GetCurrentThreadId())? };
    noninheritable(HANDLE(attached.0))?;
    noninheritable(HANDLE(desktop.0))?;
    ensure!(
        user_access::original_name(HANDLE(attached.0))? == subject.native_station
            && text(HANDLE(desktop.0), UOI_NAME)? == subject.desktop,
        "filtered USER association differs from original pair"
    );
    let mut station = Station(Some(open_subject_station(subject, 0x20002)?));
    ensure!(
        unsafe { CompareObjectHandles(HANDLE(attached.0), HANDLE(station.raw()?.0)) }.as_bool()
            && security(HANDLE(station.raw()?.0), INFO)?.1 == subject.descriptor
            && security(HANDLE(desktop.0), INFO)?.1 == subject.desktop_descriptor,
        "filtered original USER object or descriptor differs"
    );
    station.close()
}
fn probe(subject: &Subject, endpoint: &Endpoint) -> ChildObservation {
    let mut observation = ChildObservation::new();
    let result = (|| -> Result<()> {
        subject.require()?;
        crate::account_impersonation::require_process_identity()?;
        let current = ExpectedPeer::retain(unsafe { GetCurrentProcess() })?;
        ensure!(
            current.pid == subject.original.pid
                && bootstrap::pull::creation_time(current.process.raw())?
                    == subject.original.created
                && token(current.process.raw(), &subject.original)? == subject.token_stamp,
            "filtered original primary process differs"
        );
        in_job(current.process.raw())?;
        observation.primary_verified = true;
        observation.stage = ChildStage::Parent;
        let parent = parent(subject, endpoint)?;
        observation.parent_verified = true;
        observation.stage = ChildStage::UserPair;
        user_pair(&subject.original)?;
        observation.user_pair_verified = true;
        observation.stage = ChildStage::Schannel;
        observation.result = Some(schannel::probe()?);
        crate::account_impersonation::require_process_identity()?;
        ensure!(
            token(current.process.raw(), &subject.original)? == subject.token_stamp,
            "filtered primary changed after Schannel"
        );
        current.alive()?;
        parent.alive()?;
        observation.stage = ChildStage::Complete;
        Ok(())
    })();
    if let Err(error) = result {
        observation.hresult = Some(failure(&error));
    }
    observation
}
fn inherited_endpoint(value: &str) -> Result<usize> {
    ensure!(
        !value.is_empty() && value.len() <= 20 && value.bytes().all(|b| b.is_ascii_digit()),
        "filtered endpoint encoding differs"
    );
    let value: usize = value.parse()?;
    ensure!(
        value > 0 && value < isize::MAX as usize,
        "filtered endpoint invalid"
    );
    Ok(value)
}
#[test]
#[ignore = "fixed inherited filtered-primary endpoint only; admitted disposable station fixture only"]
fn station_filtered_primary_child() {
    if FILTERED_LOADER_COMPARISON {
        unsafe {
            OutputDebugStringW(w!("ALYSIS_STATION_FILTERED_MAIN_ENTERED"));
        }
    }
    // Distinct bounded early-entry failure codes preserve the failed phase when
    // a report cannot be written. They never count as successful child exits.
    let mut entry_stage = 0u32;
    let result = (|| -> Result<u32> {
        ensure!(
            std::env::args().skip(1).collect::<Vec<_>>()
                == [
                    "--exact",
                    TEST,
                    "--ignored",
                    "--nocapture",
                    "--test-threads=1"
                ],
            "filtered fixed child arguments differ"
        );
        entry_stage = 1;
        let value = inherited_endpoint(&std::env::var(ENV)?)?;
        let inherited = OwnedHandle(HANDLE(value as *mut _));
        entry_stage = 2;
        let mut flags = 0;
        unsafe {
            GetHandleInformation(inherited.raw(), &mut flags)?;
        }
        ensure!(
            flags & HANDLE_FLAG_INHERIT.0 != 0
                && unsafe { GetFileType(inherited.raw()) } == FILE_TYPE_PIPE,
            "filtered endpoint is not inherited pipe"
        );
        entry_stage = 3;
        let mut raw = HANDLE::default();
        unsafe {
            DuplicateHandle(
                GetCurrentProcess(),
                inherited.raw(),
                GetCurrentProcess(),
                &mut raw,
                0,
                false,
                DUPLICATE_SAME_ACCESS,
            )?;
        }
        let endpoint = Endpoint(OwnedHandle(raw));
        drop(inherited);
        entry_stage = 4;
        noninheritable(raw)?;
        ensure!(
            unsafe { GetErrorMode() } & 3 == 3,
            "filtered headless mode differs"
        );
        entry_stage = 5;
        endpoint.send(&STARTED)?;
        entry_stage = 6;
        let subject: Subject = endpoint.receive()?;
        let observation = probe(&subject, &endpoint);
        entry_stage = 7;
        observation.require()?;
        let code = observation.exit_code();
        entry_stage = 8;
        endpoint.send(&Report {
            subject: subject.clone(),
            observation,
        })?;
        entry_stage = 9;
        ensure!(
            endpoint.receive::<Vec<u8>>()? == finished(&subject)?,
            "filtered acknowledgment differs"
        );
        Ok(code)
    })();
    std::process::exit(result.unwrap_or(0x4190 + entry_stage) as i32);
}

#[test]
fn station_filtered_primary_codec_refuses_endpoint_and_rebound_token() {
    for value in ["", "0", "-1", " 1", "1\0", "99999999999999999999999"] {
        assert!(inherited_endpoint(value).is_err());
    }
    assert_eq!(inherited_endpoint("123").unwrap(), 123);
    let mut subject = Subject {
        original: synthetic_child_subject(),
        parent_stamp: ([1, 0], [1, 0], [2, 0]),
        token_stamp: ([3, 0], [1, 0], [4, 0]),
    };
    subject.require().unwrap();
    let decoded: Subject = decode(&encode(&subject).unwrap()).unwrap();
    assert!(decoded == subject);
    let mut extra = serde_json::to_value(&subject).unwrap();
    extra["command"] = "whoami".into();
    assert!(serde_json::from_value::<Subject>(extra).is_err());
    subject.token_stamp.1[0] ^= 1;
    assert!(subject.require().is_err());
    subject.token_stamp.1[0] ^= 1;
    subject.parent_stamp.0 = [0; 2];
    assert!(subject.require().is_err());
}
#[test]
fn station_filtered_primary_refuses_unchecked_result_and_success_without_drain() {
    let mut child = ChildObservation::new();
    assert!(child.require().is_err());
    child.hresult = Some(E_FAIL.0);
    child.require().unwrap();
    child.user_pair_verified = true;
    assert!(child.require().is_err());
    child.user_pair_verified = false;
    child.stage = ChildStage::Complete;
    child.hresult = None;
    assert!(child.require().is_err());
    let request = FixtureStationLowboxBinding::synthetic_request();
    let receipt = Receipt::new(&request, ([1, 0], [1, 0], [2, 0])).unwrap();
    let mut observed = Observation::new();
    observed.hresult = Some(E_FAIL.0);
    assert!(observed.require(&receipt).is_err()); // no original lowbox exit/USER proof
}

fn completed_fixture() -> (Receipt, Observation, Stamp) {
    let baseline = synthetic_child_subject();
    let stamp = ([1, 0], [1, 0], [2, 0]);
    let mut receipt = Receipt::new(&baseline.request, stamp).unwrap();
    receipt.stage = "child_checks".into();
    receipt.station_name = Some(baseline.station.clone());
    receipt.desktop_name = Some(baseline.desktop.clone());
    receipt.station_before = Some(baseline.descriptor.clone());
    receipt.desktop_before = Some(baseline.desktop_descriptor.clone());
    receipt.original_child = Some((baseline.pid, baseline.created));
    receipt.original_child_exit = Some(super::super::EXIT + 1);
    let denied = ERROR_ACCESS_DENIED.to_hresult().0;
    let native_denied = 0xc0000022u32 as i32;
    let mut station = ChildProof::new();
    station.passed = true;
    station.stage = "complete".into();
    station.positive_open = true;
    station.native_name_verified = true;
    station.attached_same_object = true;
    station.open_denials = vec![(0x40000, denied), (0x80000, denied), (0xc0000, denied)];
    station.set_user_dacl = Some(denied);
    station.set_user_owner = Some(denied);
    station.nt_set_dacl = Some(native_denied);
    station.nt_set_owner = Some(native_denied);
    station.attached_set_user_dacl = Some(denied);
    station.attached_set_user_owner = Some(denied);
    station.attached_nt_set_dacl = Some(native_denied);
    station.attached_nt_set_owner = Some(native_denied);
    let mut desktop = serde_json::to_value(&station).unwrap();
    desktop
        .as_object_mut()
        .unwrap()
        .remove("native_name_verified");
    desktop["name"] = baseline.desktop.clone().into();
    desktop["descriptor"] = baseline.desktop_descriptor.clone().into();
    desktop["owner_sid"] = baseline
        .request
        .birth
        .selection
        .account
        .account_sid
        .clone()
        .into();
    for key in [
        "actor_verified",
        "attached_name_verified",
        "descriptor_matches",
        "fresh_closed",
    ] {
        desktop[key] = true.into();
    }
    desktop["open_denials"] =
        serde_json::json!([[0x40081, denied], [0x80081, denied], [0xc0081, denied]]);
    desktop["close_hresult"] = serde_json::Value::Null;
    receipt.child = Some(station);
    receipt.desktop_child = Some(serde_json::from_value(desktop).unwrap());
    let failed: schannel::Proof = serde_json::from_value(serde_json::json!({
        "passed": false, "flags": 0x420010, "acquire_status": 0x80090305u32 as i32,
        "release_status": null, "https_qualified": false, "network_io_attempted": false,
        "package_query_status": 0x80090305u32 as i32, "package_buffer_returned": false,
        "package_buffer_free_status": null,
        "dependencies": {"diagnostic_only": true, "schannel_module_status": 0,
            "lsa_connect_ntstatus": 0, "lsa_handle_returned": true,
            "lsa_close_ntstatus": 0, "lsa_rpc_error": null,
            "lsa_ready_event": {"open_ntstatus": 0, "handle_returned": true,
                "zero_wait_result": 0, "close_hresult": 0}}
    }))
    .unwrap();
    receipt.schannel_child = Some(failed.clone());
    let mut selected = baseline;
    selected.pid -= 1;
    selected.created += 1;
    let subject = Subject {
        original: selected,
        parent_stamp: stamp,
        token_stamp: ([3, 0], stamp.1, [4, 0]),
    };
    let mut observed = Observation::new();
    observed.stage = Stage::Complete;
    observed.base_stamp = Some(([5, 0], stamp.1, [6, 0]));
    observed.selected_stamp = Some(([7, 0], stamp.1, [8, 0]));
    observed.child_born = true;
    observed.child_drained = true;
    observed.original_child = Some((subject.original.pid, subject.original.created));
    observed.subject = Some(subject);
    observed.suspended_verified = true;
    observed.startup = Some(
        serde_json::from_value(serde_json::json!({
            "diagnostic_only":true,"original_child":observed.original_child.unwrap(),
            "selected_token_dacl":{"access":0,"hresult":null},
            "child_process_dacl":{"access":0,"hresult":null},
            "child_token_dacl":{"access":0,"hresult":null},
            "native_process_query":null,"native_token_full_access":null,"impersonation_hresult":null
        }))
        .unwrap(),
    );
    observed.inherited_job_verified = true;
    observed.child_started = true;
    observed.child = Some(ChildObservation {
        stage: ChildStage::Complete,
        hresult: None,
        primary_verified: true,
        parent_verified: true,
        user_pair_verified: true,
        result: Some(failed),
    });
    observed.checked_exit = Some(EXIT + 1);
    (receipt, observed, stamp)
}

#[test]
fn station_filtered_primary_failed_schannel_keeps_checked_exit_and_never_qualifies() {
    let (mut receipt, observed, stamp) = completed_fixture();
    receipt.filtered_primary_control = Some(observed.clone());
    receipt.require(&receipt.request, stamp).unwrap();
    let wire = encode(&receipt).unwrap();
    let decoded: Receipt = decode(&wire).unwrap();
    decoded.require(&receipt.request, stamp).unwrap();
    for key in [
        "diagnostic_only",
        "child_born",
        "child_drained",
        "suspended_verified",
        "inherited_job_verified",
        "child_started",
    ] {
        let mut changed = serde_json::to_value(&observed).unwrap();
        changed[key] = false.into();
        assert!(
            serde_json::from_value::<Observation>(changed)
                .unwrap()
                .require(&receipt)
                .is_err(),
            "{key}"
        );
    }
    let mut changed = observed.clone();
    changed.integrity_rid = 0x2000;
    assert!(changed.require(&receipt).is_err());
    let mut changed = observed.clone();
    changed.selected_stamp = changed.base_stamp;
    assert!(changed.require(&receipt).is_err());
    let mut changed = observed.clone();
    changed.checked_exit = Some(EXIT);
    assert!(changed.require(&receipt).is_err());
    let mut changed = observed.clone();
    changed.subject.as_mut().unwrap().original.created += 1;
    assert!(changed.require(&receipt).is_err());
    let mut changed = observed;
    changed.stage = Stage::Create;
    changed.hresult = Some(0);
    assert!(changed.require(&receipt).is_err());
    // A setup error is serialized independently and leaves baseline failure intact.
    let mut setup = Observation::new();
    setup.hresult = Some(E_FAIL.0);
    receipt.filtered_primary_control = Some(setup);
    receipt.require(&receipt.request, stamp).unwrap();
    assert!(!receipt.schannel_child.as_ref().unwrap().passed);
    receipt.passed = true;
    receipt.stage = "complete".into();
    assert!(receipt.require(&receipt.request, stamp).is_err());
}

#[test]
fn station_filtered_loader_receipt_binds_child_and_refuses_normal_completion() {
    let (mut receipt, mut observed, stamp) = completed_fixture();
    observed.stage = Stage::Loader;
    observed.hresult = Some(E_FAIL.0);
    observed.loader_comparison = true;
    observed.child = None;
    observed.checked_exit = None;
    observed.child_started = false;
    let mut trace = loader::LoaderTrace::new();
    (trace.original_pid, trace.original_created) = observed.original_child.unwrap();
    trace.stop = loader::Stop::Exited;
    trace.capture_complete = true;
    trace.event_count = 1;
    trace.exit_code = Some(0xc0000142);
    observed.loader = Some(trace);
    receipt.filtered_primary_control = Some(observed.clone());
    receipt.require(&receipt.request, stamp).unwrap();
    let parsed: Receipt = decode(&encode(&receipt).unwrap()).unwrap();
    parsed.require(&receipt.request, stamp).unwrap();
    for edit in [0, 1, 2, 3, 4] {
        let mut changed = observed.clone();
        match edit {
            0 => changed.loader_comparison = false,
            1 => changed.loader.as_mut().unwrap().original_pid ^= 1,
            2 => changed.loader.as_mut().unwrap().original_created ^= 1,
            3 => {
                changed.stage = Stage::Complete;
                changed.hresult = None;
                changed.checked_exit = Some(EXIT);
            }
            4 => changed.child_started = true,
            _ => unreachable!(),
        }
        assert!(changed.require(&receipt).is_err());
    }
    receipt.loader = observed.loader.clone();
    assert!(observed.require(&receipt).is_err());
    receipt.loader = None;
    // Exercise maximal admitted descriptors: the unchanged envelope limit must
    // reject oversized combined evidence before any write, even after compaction.
    let large = "x".repeat(8192);
    receipt.station_before = Some(large.clone());
    receipt.desktop_before = Some(large.clone());
    receipt.desktop_child.as_mut().unwrap().descriptor = large.clone();
    let selected = &mut observed.subject.as_mut().unwrap().original;
    selected.descriptor = large.clone();
    selected.desktop_descriptor = large;
    let trace = observed.loader.as_mut().unwrap();
    trace.event_count = 97;
    trace.modules = (0..64)
        .map(|i| format!("{i:03}{}.dll", "a".repeat(120)))
        .collect();
    trace.loader_output = vec!["\\\"".repeat(256); 32];
    trace.compact_filtered().unwrap();
    assert!(serde_json::to_vec(trace).unwrap().len() <= loader::FILTERED_MAX_WIRE);
    assert!(trace.text_dropped > 0);
    receipt.filtered_primary_control = Some(observed);
    receipt.require(&receipt.request, stamp).unwrap();
    assert!(encode(&receipt).is_err());
}

#[test]
fn station_filtered_loader_checkpoint_preserves_only_precontrol_baseline() {
    let (mut receipt, _, stamp) = completed_fixture();
    receipt.stage = "filtered_primary_control".into();
    require_checkpoint(
        &receipt,
        &receipt.request,
        stamp,
        terminal_checkpoint_stage(),
    )
    .unwrap();
    let parsed: Frame = decode(
        &encode(&Frame::Diagnostic {
            receipt: receipt.clone(),
        })
        .unwrap(),
    )
    .unwrap();
    assert!(matches!(parsed, Frame::Diagnostic { .. }));
    receipt.desktop_closed = true;
    assert!(
        require_checkpoint(
            &receipt,
            &receipt.request,
            stamp,
            terminal_checkpoint_stage()
        )
        .is_err()
    );
    receipt.desktop_closed = false;
    receipt.original_child_exit = None;
    assert!(
        require_checkpoint(
            &receipt,
            &receipt.request,
            stamp,
            terminal_checkpoint_stage()
        )
        .is_err()
    );
}

#[test]
fn station_filtered_primary_lowering_changes_only_owned_copy() {
    // Handle-local token construction only; no account/profile/USER effects,
    // process launch or thread impersonation occurs in this local test.
    let account = crate::sid::current_user_sid().unwrap();
    let current = ExpectedPeer::retain(unsafe { GetCurrentProcess() }).unwrap();
    let current_stamp = current.token.fixture_stamp(current.process.raw()).unwrap();
    let raw = OwnedHandle(crate::token::open_self_token().unwrap());
    let base =
        OwnedHandle(crate::token::make_sandbox_token_for_owner(raw.raw(), Some(&account)).unwrap());
    let mut session = 0u32;
    let mut returned = 0u32;
    unsafe {
        GetTokenInformation(
            base.raw(),
            TokenSessionId,
            Some((&mut session as *mut u32).cast()),
            4,
            &mut returned,
        )
        .unwrap();
    }
    assert_eq!(returned, 4);
    let before = filtered::require(base.raw(), &account, session, current_stamp.1).unwrap();
    let copy = lowered_copy(base.raw()).unwrap();
    let low = filtered::require_low(copy.raw(), &account, session, current_stamp.1).unwrap();
    assert_ne!(low.0, before.0);
    assert_eq!(low.1, before.1);
    assert!(filtered::require(copy.raw(), &account, session, current_stamp.1).is_err());
    assert_eq!(
        filtered::require(base.raw(), &account, session, current_stamp.1).unwrap(),
        before
    );
    assert_eq!(
        current.token.fixture_stamp(current.process.raw()).unwrap(),
        current_stamp
    );
}

#[test]
fn station_filtered_startup_observation_cannot_rebind_or_skip_original_child() {
    let (receipt, mut observed, _) = completed_fixture();
    observed.require(&receipt).unwrap();
    let mut value = serde_json::to_value(observed.startup.as_ref().unwrap()).unwrap();
    value["original_child"][1] = (observed.original_child.unwrap().1 + 1).into();
    observed.startup = Some(serde_json::from_value(value).unwrap());
    assert!(observed.require(&receipt).is_err());
    observed.startup = None;
    assert!(observed.require(&receipt).is_err());
}
