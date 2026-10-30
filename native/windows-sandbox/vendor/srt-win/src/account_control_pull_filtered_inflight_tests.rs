//! Test-only inherited-token control and real-account native-call observation.
//! Every process reference is pulled from an original authenticated source;
//! the controller never owns the strict job. Credentials use only the existing
//! scrubbed control frames.
use super::*;
use crate::account_control::bootstrap::tests::logon_inflight as native;
use std::path::{Path, PathBuf};

#[path = "account_control_pull_filtered_inflight_observer.rs"]
mod observer;
pub(in crate::account_control::bootstrap::pull::tests::hosted) use observer::qualify;

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Spec {
    target: PathBuf,
    account: String,
}
impl Spec {
    fn validate(&self) -> Result<()> {
        ensure!(
            self.target.is_absolute()
                && self.target.to_str().is_some_and(|s| !s.contains('\0'))
                && !self.target.components().any(|part| matches!(
                    part,
                    std::path::Component::ParentDir | std::path::Component::CurDir
                ))
                && self.target.file_name().and_then(|s| s.to_str()) == Some("never-resumed.exe")
                && self
                    .target
                    .parent()
                    .and_then(Path::file_name)
                    .and_then(|s| s.to_str())
                    .is_some_and(|s| s
                        .strip_prefix("account-inflight-")
                        .is_some_and(
                            |id| id.len() == 32 && id.bytes().all(|c| c.is_ascii_hexdigit())
                        )),
            "invalid private in-flight image shape"
        );
        crate::sid::LocalPsid::from_string(&self.account)?;
        Ok(())
    }
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct ProcessRef {
    source: u64,
    created: u64,
}
impl ProcessRef {
    fn new(process: HANDLE) -> Result<Self> {
        Ok(Self {
            source: process.0 as usize as u64,
            created: creation_time(process)?,
        })
    }
    fn pull(&self, source: HANDLE, rights: PROCESS_ACCESS_RIGHTS) -> Result<OwnedHandle> {
        let raw = pull_handle(source, self.source, rights.0)?;
        ensure!(
            self.created != 0
                && creation_time(raw.raw())? == self.created
                && self.created >= creation_time(source)?
                && !unsafe { CompareObjectHandles(raw.raw(), source) }.as_bool(),
            "in-flight original process identity differs"
        );
        Ok(raw)
    }
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct ThreadFrame {
    source: u64,
    frame: usize,
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct BootstrapRef {
    process: ProcessRef,
    thread: ThreadFrame,
}

fn pull_handle(source: HANDLE, value: u64, access: u32) -> Result<OwnedHandle> {
    ensure!(
        value > 0 && value < isize::MAX as u64,
        "invalid original handle value"
    );
    let mut raw = HANDLE::default();
    unsafe {
        DuplicateHandle(
            source,
            HANDLE(value as usize as *mut _),
            GetCurrentProcess(),
            &mut raw,
            access,
            false,
            DUPLICATE_HANDLE_OPTIONS(0),
        )?;
    }
    let owned = OwnedHandle(raw);
    noninheritable(owned.raw())?;
    Ok(owned)
}

fn session() -> Result<u32> {
    let mut session = 0;
    unsafe {
        windows::Win32::System::RemoteDesktop::ProcessIdToSessionId(
            std::process::id(),
            &mut session,
        )?;
    }
    Ok(session)
}

fn require_runner(process: HANDLE, spec: &Spec, separate: bool) -> Result<()> {
    let mut phase = 0;
    require_runner_observed(process, spec, separate, &mut phase)
        .with_context(|| format!("in-flight runner verification phase {phase:#x}"))
}

fn require_runner_observed(
    process: HANDLE,
    spec: &Spec,
    separate: bool,
    phase: &mut u32,
) -> Result<()> {
    // Keep every original admission check. These fixed subphases distinguish
    // image access from token access in the filtered hosted worker's exit code.
    *phase = 0x51;
    let target = PinnedRunnerImage::fixture(&spec.target)?;
    *phase = 0x52;
    let mut raw = HANDLE::default();
    unsafe {
        windows::Win32::System::Threading::OpenProcessToken(
            process,
            windows::Win32::Security::TOKEN_QUERY,
            &mut raw,
        )?;
    }
    // Query-only preflight, never a supplied token or a substitute for the
    // retained primary-token/process continuity checks immediately below.
    drop(OwnedHandle(raw));
    *phase = 0x53;
    let peer = ExpectedPeer::retain(process)?;
    *phase = 0x54;
    let peer = peer.with_image(&target)?;
    *phase = 0x55;
    let session = session()?;
    *phase = 0x56;
    if separate {
        peer.token
            .require_dedicated_runner(&spec.account, session, "S-1-5-32-545")?;
    } else {
        peer.token.require_account_runner(&spec.account, session)?;
    }
    *phase = 0x57;
    peer.alive()?;
    Ok(())
}

fn read_secret(channel: &mut ControlChannel<'_>, separate: bool) -> Result<SecretLogon> {
    if !separate {
        return Ok(SecretLogon {
            name: "AlysisSyntheticNeverValidated".into(),
            password: wstr("NotARealCredential!SyntheticOnly"),
        });
    }
    let frame = SecretFrame(channel.receive(HOST_TIMEOUT)?);
    let secret: SecretLogon = serde_json::from_slice(&frame.0)?;
    ensure!(
        secret.name.len() == 19
            && secret.name.starts_with("aQA")
            && secret.name[3..].bytes().all(|c| c.is_ascii_hexdigit()),
        "invalid fresh test account name"
    );
    ensure!(
        (32..=128).contains(&secret.password.len())
            && secret.password.last() == Some(&0)
            && !secret.password[..secret.password.len() - 1].contains(&0),
        "invalid test credential shape"
    );
    Ok(secret)
}

fn send_secret(channel: &mut ControlChannel<'_>, secret: &SecretLogon) -> Result<()> {
    let frame = SecretFrame(serde_json::to_vec(secret)?);
    channel.send(&frame.0, HOST_TIMEOUT)
}

fn check_secret(secret: &SecretLogon, spec: &Spec, separate: bool) -> Result<()> {
    spec.validate()?;
    let actual = if separate {
        crate::user::local_user_sid(&secret.name)?.context("fresh test account missing")?
    } else {
        crate::sid::current_user_sid()?
    };
    ensure!(
        actual == spec.account,
        "selected test account identity differs"
    );
    if separate {
        ensure!(
            actual != crate::sid::current_user_sid()?,
            "account must differ from broker"
        );
    }
    Ok(())
}

pub(in crate::account_control::bootstrap::pull::tests) fn offer(
    channel: ControlChannel<'_>,
    separate: bool,
) -> Result<()> {
    let mut phase = 1u32;
    let result = offer_inner(channel, separate, &mut phase);
    if let Err(error) = &result {
        let native = error
            .chain()
            .find_map(|c| c.downcast_ref::<windows::core::Error>())
            .map_or(0, |e| e.code().0 as u32 & 0xffff);
        // Fixed numeric test diagnostics only. All inner guards have unwound;
        // the original broker still owns the strict containing job.
        std::process::exit((0x43000000 | (phase << 16) | native) as i32);
    }
    result
}
fn offer_inner(mut channel: ControlChannel<'_>, separate: bool, phase: &mut u32) -> Result<()> {
    let secret = read_secret(&mut channel, separate)?;
    *phase = 2;
    let spec: Spec = serde_json::from_slice(&channel.receive(HOST_TIMEOUT)?)?;
    check_secret(&secret, &spec, separate)?;
    *phase = 3;
    // Pseudo handles are valid only for this explicit local duplication, never
    // for a received wire reference (pull_handle deliberately rejects them).
    let mut raw = HANDLE::default();
    unsafe {
        DuplicateHandle(
            GetCurrentProcess(),
            GetCurrentThread(),
            GetCurrentProcess(),
            &mut raw,
            (THREAD_GET_CONTEXT
                | THREAD_SUSPEND_RESUME
                | THREAD_SYNCHRONIZE
                | THREAD_QUERY_LIMITED_INFORMATION)
                .0,
            false,
            DUPLICATE_HANDLE_OPTIONS(0),
        )?;
    }
    let thread = OwnedHandle(raw);
    *phase = 4;
    let before = |frame| {
        channel.send(
            &serde_json::to_vec(&ThreadFrame {
                source: thread.raw().0 as usize as u64,
                frame,
            })?,
            HOST_TIMEOUT,
        )
    };
    let child = if separate {
        native::observed_logon(
            &spec.target,
            &secret.name,
            &secret.password,
            CREATE_PROCESS_LOGON_FLAGS(0),
            before,
        )?
    } else {
        // The ordinary control owns its filtered token and default DACL.
        // NETCREDENTIALS_ONLY introduces a Secondary Logon token whose query
        // access is not established by that ownership (hosted error 5).
        // Run 37615407972 retained the refusal as worker_exit 0x43520005;
        // this control does not qualify or fix that synthetic logon context.
        // Keep the full token checks; use ordinary same-user creation here.
        native::observed_same_user_create(&spec.target, before)?
    };
    drop(secret);
    *phase = 5;
    require_runner_observed(child.process(), &spec, separate, phase)?;
    *phase = 6;
    channel.send(
        &serde_json::to_vec(&ProcessRef::new(child.process())?)?,
        HOST_TIMEOUT,
    )?;
    ensure!(
        channel.receive(HOST_TIMEOUT)? == b"retire-child",
        "invalid in-flight retirement command"
    );
    *phase = 7;
    drop(child); // Original creation handles; no child was ever resumed.
    channel.send(b"retired", HOST_TIMEOUT)?;
    // Retain the authenticated worker through the broker's ordinary receive.
    // Only a distinct terminal request allows the worker to exit afterward.
    ensure!(
        channel.receive(HOST_TIMEOUT)? == b"finish",
        "invalid worker finish request"
    );
    Ok(())
}

pub(super) fn broker(
    channel: &mut ControlChannel<'_>,
    image: &PinnedRunnerImage,
    separate: bool,
    report: &mut super::Report,
) -> Result<()> {
    let secret = read_secret(channel, separate)?;
    let spec: Spec = serde_json::from_slice(&channel.receive(HOST_TIMEOUT)?)?;
    check_secret(&secret, &spec, separate)?;
    report.stage = "inflight_atomic_bootstrap".into();
    let job = Job::new(false)?;
    let (server, address) = ControlServer::prepare(CLIENT)?;
    let mut birth = SuspendedBootstrap::create(image, &job, &address, Entry::OfferFixture)?;
    let peer = birth.peer()?;
    birth.resume_fixture()?;
    let mut control = server.accept(peer, HOST_TIMEOUT)?;
    control.send(
        if separate {
            b"hosted-account-inflight"
        } else {
            b"synthetic-inflight"
        },
        HOST_TIMEOUT,
    )?;
    if separate {
        send_secret(&mut control, &secret)?;
    }
    drop(secret);
    control.send(&serde_json::to_vec(&spec)?, HOST_TIMEOUT)?;
    report.stage = "inflight_native_call".into();
    let thread: ThreadFrame = serde_json::from_slice(&control.receive(HOST_TIMEOUT)?)?;
    let reference = BootstrapRef {
        process: ProcessRef::new(birth.child.process())?,
        thread,
    };
    channel.send(&serde_json::to_vec(&reference)?, HOST_TIMEOUT)?;
    // While blocked here this process alone owns the job. The external fault
    // injector terminates this original broker and bypasses every Rust Drop.
    ensure!(
        channel.receive(HOST_TIMEOUT)? == b"observe-child",
        "invalid in-flight observation command"
    );
    report.stage = "inflight_child_observation".into();
    let frame = control.receive(HOST_TIMEOUT);
    if frame.is_err()
        && unsafe { WaitForSingleObject(birth.child.process(), 1000) } == WAIT_OBJECT_0
    {
        let mut code = 0;
        unsafe {
            GetExitCodeProcess(birth.child.process(), &mut code)?;
        }
        report.worker_exit = Some(code);
    }
    let child: ProcessRef = serde_json::from_slice(&frame?)?;
    let child = child.pull(
        birth.child.process(),
        PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SYNCHRONIZE,
    )?;
    assert_read_only(child.raw());
    let target = PinnedRunnerImage::fixture(&spec.target)?;
    ExpectedPeer::retain(child.raw())?
        .with_image(&target)?
        .with_job(&job)?
        .alive()?;
    require_runner(child.raw(), &spec, separate)?;
    report.dedicated_runner_policy = separate;
    channel.send(
        &serde_json::to_vec(&ProcessRef::new(child.raw())?)?,
        HOST_TIMEOUT,
    )?;
    ensure!(
        channel.receive(HOST_TIMEOUT)? == b"retire-child",
        "invalid in-flight retirement command"
    );
    control.send(b"retire-child", HOST_TIMEOUT)?;
    ensure!(
        control.receive(HOST_TIMEOUT)? == b"retired",
        "child retirement unverified"
    );
    control.write_in_flight(b"finish", HOST_TIMEOUT)?;
    ensure!(
        unsafe { WaitForSingleObject(child.raw(), 5000) } == WAIT_OBJECT_0,
        "original child survived retirement"
    );
    require_exit(birth.child.process(), 0)?;
    report.stage = "complete".into();
    Ok(())
}

fn require_exit(process: HANDLE, expected: u32) -> Result<()> {
    ensure!(
        unsafe { WaitForSingleObject(process, 5000) } == WAIT_OBJECT_0,
        "original fixture did not exit"
    );
    let mut code = u32::MAX;
    unsafe {
        GetExitCodeProcess(process, &mut code)?;
    }
    ensure!(
        code == expected,
        "original fixture exit differed ({code}, expected {expected})"
    );
    Ok(())
}

#[test]
fn inflight_reference_refuses_pseudo_handles_and_source_process_as_child() {
    for value in [0, isize::MAX as u64, u64::MAX] {
        assert!(
            pull_handle(
                unsafe { GetCurrentProcess() },
                value,
                PROCESS_QUERY_LIMITED_INFORMATION.0
            )
            .is_err()
        );
    }
    let process = ExpectedPeer::retain(unsafe { GetCurrentProcess() }).unwrap();
    let reference = ProcessRef::new(process.process.raw()).unwrap();
    assert!(
        reference
            .pull(
                unsafe { GetCurrentProcess() },
                PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SYNCHRONIZE
            )
            .is_err()
    );
}

#[test]
fn inflight_spec_refuses_parent_navigation_and_non_private_image_shape() {
    let parent = std::env::current_exe()
        .unwrap()
        .parent()
        .unwrap()
        .to_owned();
    let name = "account-inflight-0123456789abcdef0123456789abcdef";
    let account = crate::sid::current_user_sid().unwrap();
    assert!(
        Spec {
            target: parent.join(name).join("never-resumed.exe"),
            account: account.clone()
        }
        .validate()
        .is_ok()
    );
    for target in [
        PathBuf::from(name).join("never-resumed.exe"),
        parent.join("..").join(name).join("never-resumed.exe"),
        parent.join(name).join("other.exe"),
        parent
            .join("account-inflight-short")
            .join("never-resumed.exe"),
    ] {
        assert!(
            Spec {
                target,
                account: account.clone()
            }
            .validate()
            .is_err()
        );
    }
}
