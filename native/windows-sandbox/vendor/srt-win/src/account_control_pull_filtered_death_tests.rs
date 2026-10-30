//! Abrupt broker loss after original child observation. The external observer
//! receives only a query/synchronize process reference and no job reference.
//! This does not cover death during the outstanding secondary-logon call.
use super::*;

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Offer {
    source_handle: u64,
    created: u64,
}

#[derive(Serialize)]
pub(in super::super) struct Report {
    pub(super) passed: bool,
    pub(super) separate_account: bool,
    pub(super) broker_exit_code: u32,
    pub(super) child_exit_observed: bool,
    pub(super) observer_has_no_job_reference: bool,
    pub(super) account_child_never_resumed: bool,
    pub(super) dedicated_runner_policy: bool,
}

pub(super) fn hold(channel: &mut ControlChannel<'_>, process: HANDLE) -> Result<()> {
    ensure!(
        unsafe { WaitForSingleObject(process, 0) } == WAIT_TIMEOUT,
        "death fixture child already exited"
    );
    let offer = Offer {
        source_handle: process.0 as usize as u64,
        created: creation_time(process)?,
    };
    channel.send(&serde_json::to_vec(&offer)?, HOST_TIMEOUT)?;
    // No normal child cleanup, job close, or child cooperation after the offer.
    // The controller must terminate this original broker, not send a finish.
    unsafe {
        Sleep(30_000);
    }
    anyhow::bail!("external controller did not terminate held broker")
}

pub(in super::super) fn qualify(
    secret: Option<&SecretLogon>,
    expected_account: &str,
) -> Result<Report> {
    let image = image();
    let (server, address) = desktop::controller_pipe()?;
    let (broker, _diagnostic, _desktop, probe) = create(&image, &address, secret.is_some(), None)?;
    probe.startup_security.as_ref().unwrap().require_owned()?;
    let peer = ExpectedPeer::retain(broker.process())?.with_image(&image)?;
    ensure!(
        unsafe { ResumeThread(broker.thread()) } == 1,
        "death fixture initial suspension differs"
    );
    let mut channel = server.accept(peer, HOST_TIMEOUT)?;
    channel.send(
        if secret.is_some() {
            b"account-hold"
        } else {
            b"local-hold"
        },
        HOST_TIMEOUT,
    )?;
    if let Some(secret) = secret {
        let frame = SecretFrame(serde_json::to_vec(secret)?);
        channel.send(&frame.0, HOST_TIMEOUT)?;
    }
    let offer: Offer = serde_json::from_slice(&channel.receive(HOST_TIMEOUT)?)?;
    ensure!(
        offer.source_handle > 0 && offer.source_handle < isize::MAX as u64,
        "invalid death-fixture source handle"
    );
    let mut raw = HANDLE::default();
    // Pull only from the original authenticated fixture broker's table. Never
    // adopt wire bytes as a local owned handle and never duplicate a job.
    unsafe {
        DuplicateHandle(
            broker.process(),
            HANDLE(offer.source_handle as usize as *mut _),
            GetCurrentProcess(),
            &mut raw,
            (PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SYNCHRONIZE).0,
            false,
            DUPLICATE_HANDLE_OPTIONS(0),
        )?;
    }
    let child = OwnedHandle(raw);
    noninheritable(child.raw())?;
    assert_read_only(child.raw());
    ensure!(
        !unsafe { CompareObjectHandles(child.raw(), broker.process()) }.as_bool(),
        "broker offered itself as child"
    );
    ensure!(
        creation_time(child.raw())? == offer.created
            && offer.created >= creation_time(broker.process())?,
        "death-fixture process creation differs"
    );
    let peer = ExpectedPeer::retain(child.raw())?.with_image(&image)?;
    let mut session = 0;
    unsafe {
        windows::Win32::System::RemoteDesktop::ProcessIdToSessionId(
            std::process::id(),
            &mut session,
        )?;
    }
    peer.token
        .require_account_runner(expected_account, session)?;
    if secret.is_some() {
        peer.token
            .require_dedicated_runner(expected_account, session, "S-1-5-32-545")?;
    }
    peer.alive()?;
    channel.peer.alive()?;
    unsafe {
        TerminateProcess(broker.process(), 74)?;
    }
    ensure!(
        unsafe { WaitForSingleObject(broker.process(), 5000) } == WAIT_OBJECT_0,
        "original death-fixture broker exit unverified"
    );
    let mut broker_exit_code = 0;
    unsafe {
        GetExitCodeProcess(broker.process(), &mut broker_exit_code)?;
    }
    ensure!(
        broker_exit_code == 74,
        "broker exited cooperatively or panicked instead of being terminated"
    );
    ensure!(
        unsafe { WaitForSingleObject(child.raw(), 5000) } == WAIT_OBJECT_0,
        "broker death left original held child alive"
    );
    let mut child_code = 0;
    unsafe {
        GetExitCodeProcess(child.raw(), &mut child_code)?;
    }
    ensure!(
        child_code != 101,
        "held child panicked instead of job termination"
    );
    Ok(Report {
        passed: true,
        separate_account: secret.is_some(),
        broker_exit_code,
        child_exit_observed: true,
        observer_has_no_job_reference: true,
        account_child_never_resumed: true,
        dedicated_runner_policy: secret.is_some(),
    })
}

#[test]
fn bootstrap_filtered_broker_death_kills_original_suspended_child() {
    let report = qualify(None, &crate::sid::current_user_sid().unwrap()).unwrap();
    assert!(report.passed && !report.separate_account && report.child_exit_observed);
}
