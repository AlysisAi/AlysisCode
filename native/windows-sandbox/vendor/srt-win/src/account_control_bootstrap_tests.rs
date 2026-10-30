use super::*;
#[path = "account_logon_attribute_probe.rs"]
mod logon_attributes;
#[cfg(target_arch = "x86_64")]
#[path = "account_logon_inflight_probe.rs"]
pub(in crate::account_control) mod logon_inflight;
const CLIENT: &str = "S-1-5-21-1-2-3-9999";
const TIMEOUT: Duration = Duration::from_secs(3);
fn image() -> PinnedRunnerImage {
    PinnedRunnerImage::fixture(&std::env::current_exe().unwrap()).unwrap()
}

#[test]
fn bootstrap_birth_prepares_image_and_job_before_native_child_and_exchanges() {
    let image = image();
    let job = Job::new(false).unwrap();
    let (server, address) = ControlServer::prepare(CLIENT).unwrap();
    let mut birth =
        SuspendedBootstrap::create(&image, &job, &address, Entry::ControlFixture).unwrap();
    let peer = birth.peer().unwrap();
    birth.resume_fixture().unwrap();
    assert!(birth.resume_fixture().is_err());
    let mut channel = server.accept(peer, TIMEOUT).unwrap();
    assert_eq!(channel.receive(TIMEOUT).unwrap(), b"ready");
    assert_eq!(channel.receive(TIMEOUT).unwrap(), b"buffered-while-alive");
    let body = vec![0x71; MAX_FRAME];
    channel.send(&body, TIMEOUT).unwrap();
    assert_eq!(channel.receive(TIMEOUT).unwrap(), body);
    channel.write_in_flight(b"finish", TIMEOUT).unwrap();
    assert_eq!(
        unsafe { WaitForSingleObject(birth.child.process(), 5000) },
        WAIT_OBJECT_0
    );
    let mut code = u32::MAX;
    unsafe {
        GetExitCodeProcess(birth.child.process(), &mut code).unwrap();
    }
    assert_eq!(code, 0);
    assert!(channel.receive(TIMEOUT).is_err());
}

#[test]
fn bootstrap_birth_production_gate_does_not_create_a_process() {
    let image = image();
    let job = Job::new(false).unwrap();
    let (_server, address) = ControlServer::prepare(CLIENT).unwrap();
    let error = SuspendedBootstrap::prepare(&image, &job, &address)
        .err()
        .unwrap();
    assert!(
        error
            .to_string()
            .contains("lifecycle authority unavailable")
    );
    let mut info =
        windows::Win32::System::JobObjects::JOBOBJECT_BASIC_ACCOUNTING_INFORMATION::default();
    unsafe {
        windows::Win32::System::JobObjects::QueryInformationJobObject(
            Some(job.raw()),
            windows::Win32::System::JobObjects::JobObjectBasicAccountingInformation,
            (&mut info
                as *mut windows::Win32::System::JobObjects::JOBOBJECT_BASIC_ACCOUNTING_INFORMATION)
                .cast(),
            size_of_val(&info) as u32,
            None,
        )
        .unwrap();
    }
    assert_eq!(info.TotalProcesses, 0);
}

#[test]
fn bootstrap_birth_refuses_weaker_job_and_never_resumes_after_drift() {
    let image = image();
    let weak = Job::new(true).unwrap();
    let (_server, address) = ControlServer::prepare(CLIENT).unwrap();
    assert!(SuspendedBootstrap::create(&image, &weak, &address, Entry::ControlFixture).is_err());
    let job = Job::new(false).unwrap();
    let mut birth =
        SuspendedBootstrap::create(&image, &job, &address, Entry::ControlFixture).unwrap();
    containment::tests::with_fault(&job, "breakaway", || {
        assert!(birth.resume_fixture().is_err());
    });
    assert!(birth.resume_fixture().is_err());
    assert_eq!(
        unsafe { WaitForSingleObject(birth.child.process(), 0) },
        WAIT_TIMEOUT
    );
    let retained = ExpectedPeer::retain(birth.child.process()).unwrap();
    drop(birth);
    assert_eq!(
        unsafe { WaitForSingleObject(retained.process.raw(), 5000) },
        WAIT_OBJECT_0
    );
}

const BROKER_FIXTURE: &str = "account_control::bootstrap::tests::bootstrap_broker_death_fixture";

#[test]
#[ignore = "bounded test-only broker invoked by the two abrupt-death tests"]
fn bootstrap_broker_death_fixture() {
    let parent_value: usize = std::env::var("ALYSIS_TEST_BIRTH_OBSERVER")
        .unwrap()
        .parse()
        .unwrap();
    assert!(parent_value > 0 && parent_value < isize::MAX as usize);
    let parent = OwnedHandle(HANDLE(parent_value as *mut _));
    let address: ControlAddress =
        serde_json::from_str(&std::env::var("ALYSIS_TEST_BIRTH_ADDRESS").unwrap()).unwrap();
    let image = image();
    let peer = ExpectedPeer::retain(parent.raw())
        .unwrap()
        .with_image(&image)
        .unwrap();
    let mut observer = ControlChannel::connect(&address, peer).unwrap();
    let job = Job::new(false).unwrap();
    let (server, address) = ControlServer::prepare(CLIENT).unwrap();
    let mut birth =
        SuspendedBootstrap::create(&image, &job, &address, Entry::WaitingFixture).unwrap();
    let peer = birth.peer().unwrap();
    birth.resume_fixture().unwrap();
    let mut channel = server.accept(peer, TIMEOUT).unwrap();
    assert_eq!(channel.receive(TIMEOUT).unwrap(), b"ready");
    // Test-only original-handle transfer for external exit observation and
    // emergency cleanup. This is not the production cross-account protocol.
    let mut remote = HANDLE::default();
    unsafe {
        DuplicateHandle(
            GetCurrentProcess(),
            birth.child.process(),
            parent.raw(),
            &mut remote,
            (PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SYNCHRONIZE | PROCESS_TERMINATE).0,
            false,
            DUPLICATE_HANDLE_OPTIONS(0),
        )
        .unwrap();
    }
    observer
        .send(&(remote.0 as usize as u64).to_le_bytes(), TIMEOUT)
        .unwrap();
    let command = observer.receive(TIMEOUT).unwrap();
    if command == b"exit-without-drop" {
        std::process::exit(73);
    }
    assert_eq!(command, b"hold-for-external-kill");
    observer.send(b"holding", TIMEOUT).unwrap();
    unsafe {
        Sleep(30000);
    }
    panic!("test observer did not terminate its broker");
}

#[test]
fn bootstrap_birth_broker_exit_without_unwind_kills_noncooperating_child() {
    abrupt_broker_death(false);
}

#[test]
fn bootstrap_birth_external_broker_termination_kills_noncooperating_child() {
    abrupt_broker_death(true);
}

fn abrupt_broker_death(external: bool) {
    let image = image();
    let (server, address) = ControlServer::prepare(CLIENT).unwrap();
    let mut raw = HANDLE::default();
    unsafe {
        DuplicateHandle(
            GetCurrentProcess(),
            GetCurrentProcess(),
            GetCurrentProcess(),
            &mut raw,
            (PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SYNCHRONIZE | PROCESS_DUP_HANDLE).0,
            true,
            DUPLICATE_HANDLE_OPTIONS(0),
        )
        .unwrap();
    }
    let parent = OwnedHandle(raw);
    let path = image.application().unwrap();
    let app = wstr(path.to_str().unwrap());
    let mut command = wstr(&format!(
        "{} --exact {BROKER_FIXTURE} --ignored --nocapture --test-threads=1",
        crate::launch::quote_arg(path.to_str().unwrap())
    ));
    let directory = os_directory(false).unwrap();
    let mut environment = crate::launch::build_env_block_with_inheritance(
        &[
            ("SystemRoot".into(), os_directory(true).unwrap()),
            ("PATH".into(), directory.clone()),
            (
                "ALYSIS_TEST_BIRTH_OBSERVER".into(),
                (parent.raw().0 as usize).to_string(),
            ),
            (
                "ALYSIS_TEST_BIRTH_ADDRESS".into(),
                serde_json::to_string(&address).unwrap(),
            ),
        ],
        false,
    );
    let mut attributes = ProcThreadAttrs::new(1).unwrap();
    attributes.set_handle_list(&[parent.raw()]).unwrap();
    let mut startup = STARTUPINFOEXW::default();
    startup.StartupInfo.cb = size_of::<STARTUPINFOEXW>() as u32;
    startup.lpAttributeList = attributes.list();
    let mut info = PROCESS_INFORMATION::default();
    // The trusted test broker lives OUTSIDE its own execution job, just as the
    // real broker must. Only it creates/owns that unnamed job; the observer and
    // subordinate fixture never receive any reference to the job object.
    unsafe {
        CreateProcessW(
            pcwstr(&app),
            Some(windows::core::PWSTR(command.as_mut_ptr())),
            None,
            None,
            true,
            CREATE_SUSPENDED
                | CREATE_NO_WINDOW
                | CREATE_UNICODE_ENVIRONMENT
                | EXTENDED_STARTUPINFO_PRESENT,
            Some(environment.as_mut_ptr().cast()),
            pcwstr(&wstr(&directory)),
            &startup.StartupInfo,
            &mut info,
        )
        .unwrap();
    }
    let broker = SpawnedChild::new(info);
    let peer = ExpectedPeer::retain(broker.process())
        .unwrap()
        .with_image(&image)
        .unwrap();
    assert_eq!(unsafe { ResumeThread(broker.thread()) }, 1);
    drop(parent);
    let mut channel = server.accept(peer, TIMEOUT).unwrap();
    let bytes = channel.receive(TIMEOUT).unwrap();
    let raw = u64::from_le_bytes(bytes.try_into().expect("one test process handle"));
    assert!(raw > 0 && raw < (isize::MAX as u64));
    // Only this known, authenticated test fixture sends an original duplicate.
    // Production never constructs ownership from handle numbers in messages.
    let process = HANDLE(raw as usize as *mut _);
    let child = SpawnedChild::new(PROCESS_INFORMATION {
        hProcess: process,
        ..Default::default()
    });
    assert_ne!(unsafe { GetProcessId(child.process()) }, 0);
    if let Err(error) = image.verify_process(child.process()) {
        let mut broker_code = u32::MAX;
        let mut child_code = u32::MAX;
        unsafe {
            GetExitCodeProcess(broker.process(), &mut broker_code).unwrap();
            GetExitCodeProcess(child.process(), &mut child_code).unwrap();
        }
        panic!(
            "pre-cut original image check failed: {error:#}; broker_exit={broker_code}, child_exit={child_code}, broker_wait={:?}, child_wait={:?}",
            unsafe { WaitForSingleObject(broker.process(), 0) },
            unsafe { WaitForSingleObject(child.process(), 0) }
        );
    }
    assert_eq!(
        unsafe { WaitForSingleObject(child.process(), 0) },
        WAIT_TIMEOUT
    );
    if external {
        channel.send(b"hold-for-external-kill", TIMEOUT).unwrap();
        assert_eq!(channel.receive(TIMEOUT).unwrap(), b"holding");
        unsafe {
            TerminateProcess(broker.process(), 74).unwrap();
        }
    } else {
        // This fixed command deliberately terminates the authenticated broker.
        // Keep prewrite checks, then prove that exact exit below rather than
        // racing the ordinary send's postwrite live-process image query.
        channel
            .write_in_flight(b"exit-without-drop", TIMEOUT)
            .unwrap();
    }
    assert_eq!(
        unsafe { WaitForSingleObject(broker.process(), 5000) },
        WAIT_OBJECT_0
    );
    assert_eq!(
        unsafe { WaitForSingleObject(child.process(), 5000) },
        WAIT_OBJECT_0,
        "broker death left the noncooperating bootstrap alive"
    );
    let mut broker_code = u32::MAX;
    let mut child_code = u32::MAX;
    unsafe {
        GetExitCodeProcess(broker.process(), &mut broker_code).unwrap();
        GetExitCodeProcess(child.process(), &mut child_code).unwrap();
    }
    assert_eq!(broker_code, if external { 74 } else { 73 });
    assert_ne!(
        child_code, 101,
        "fixture panicked rather than being killed by its job"
    );
}
