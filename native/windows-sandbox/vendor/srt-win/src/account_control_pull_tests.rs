use super::*;
const CLIENT: &str = "S-1-5-21-1-2-3-9999";
const TIMEOUT: Duration = Duration::from_secs(3);

fn image() -> PinnedRunnerImage {
    PinnedRunnerImage::fixture(&std::env::current_exe().unwrap()).unwrap()
}

#[repr(C)]
#[derive(Default)]
struct ObjectBasic {
    attributes: u32,
    granted_access: u32,
    handle_count: u32,
    pointer_count: u32,
    reserved: [u32; 10],
}

#[link(name = "ntdll")]
unsafe extern "system" {
    fn NtQueryObject(
        handle: HANDLE,
        class: u32,
        data: *mut std::ffi::c_void,
        length: u32,
        returned: *mut u32,
    ) -> i32;
}

fn assert_read_only(handle: HANDLE) {
    let mut data = ObjectBasic::default();
    let mut returned = 0;
    assert_eq!(
        unsafe {
            NtQueryObject(
                handle,
                0,
                (&mut data as *mut ObjectBasic).cast(),
                size_of_val(&data) as u32,
                &mut returned,
            )
        },
        0
    );
    assert_eq!(returned as usize, size_of_val(&data));
    assert_eq!(
        data.granted_access,
        (PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SYNCHRONIZE).0
    );
}

fn child(image: &PinnedRunnerImage, config: &str) -> SpawnedChild {
    let wrong_image = config == "wrong-image";
    let path = if wrong_image {
        std::path::PathBuf::from(os_directory(false).unwrap()).join("cmd.exe")
    } else {
        image.application().unwrap().to_owned()
    };
    let app = wstr(path.to_str().unwrap());
    let mut command = wstr(&crate::launch::quote_arg(path.to_str().unwrap()));
    let startup = STARTUPINFOW {
        cb: size_of::<STARTUPINFOW>() as u32,
        ..Default::default()
    };
    let mut info = PROCESS_INFORMATION::default();
    unsafe {
        // No new job and no breakaway: this never-resumed synthetic grandchild
        // must inherit the bootstrap's original strict job.
        if config == "logon-good" {
            // Synthetic credentials are deliberately unvalidated and never used
            // for network traffic. This preserves the caller's local identity;
            // it tests Secondary Logon inheritance, NOT a separate account.
            CreateProcessWithLogonW(
                pcwstr(&wstr("AlysisSyntheticNeverValidated")),
                pcwstr(&wstr(".")),
                pcwstr(&wstr("NotARealCredential!SyntheticOnly")),
                LOGON_NETCREDENTIALS_ONLY,
                pcwstr(&app),
                Some(windows::core::PWSTR(command.as_mut_ptr())),
                CREATE_SUSPENDED | CREATE_NO_WINDOW,
                None,
                pcwstr(&wstr(&os_directory(false).unwrap())),
                &startup,
                &mut info,
            )
            .unwrap();
        } else {
            CreateProcessW(
                pcwstr(&app),
                Some(windows::core::PWSTR(command.as_mut_ptr())),
                None,
                None,
                false,
                CREATE_SUSPENDED | CREATE_NO_WINDOW,
                None,
                pcwstr(&wstr(&os_directory(false).unwrap())),
                &startup,
                &mut info,
            )
            .unwrap();
        }
    }
    SpawnedChild::new(info)
}

#[test]
#[ignore = "bounded original-handle offer fixture invoked by bootstrap pull tests"]
fn bootstrap_offer_fixture() {
    let value: usize = std::env::var("ALYSIS_TEST_CONTROL_ORIGINAL_PEER")
        .unwrap()
        .parse()
        .unwrap();
    assert!(value > 0 && value < isize::MAX as usize);
    let parent = OwnedHandle(HANDLE(value as *mut _));
    assert_read_only(parent.raw()); // No DUP_HANDLE authority in the bootstrap.
    let image = image();
    let peer = ExpectedPeer::retain(parent.raw())
        .unwrap()
        .with_image(&image)
        .unwrap();
    let address: ControlAddress =
        serde_json::from_str(&std::env::var("ALYSIS_TEST_CONTROL_ADDRESS").unwrap()).unwrap();
    let mut channel = ControlChannel::connect(&address, peer).unwrap();
    let config = channel.receive(TIMEOUT).unwrap();
    let config = std::str::from_utf8(&config).unwrap();
    if config == "hosted-account" {
        hosted::offer_account_child(channel, &image).unwrap();
        return;
    }
    #[cfg(target_arch = "x86_64")]
    if config == "hosted-account-inflight" || config == "synthetic-inflight" {
        hosted::filtered::inflight::offer(channel, config == "hosted-account-inflight").unwrap();
        return;
    }
    let child = child(&image, config);
    if matches!(config, "good" | "logon-good" | "broker-kill") {
        let mut pending = PendingProcessOffer::send(&child, channel, TIMEOUT).unwrap();
        if config == "broker-kill" {
            // Do not react to pipe closure. Only the original containing job
            // may end this fixture before the bounded emergency timeout.
            unsafe {
                Sleep(30000);
            }
            panic!("strict job did not kill its bootstrap");
        }
        // Test acknowledgment is only a request to terminate the never-resumed
        // fixture. It does not model durable commit or permission to run it.
        assert_eq!(
            pending._channel.receive(TIMEOUT).unwrap(),
            b"test-drop-child"
        );
        drop(pending);
        drop(child);
        return;
    }
    let request: Request = serde_json::from_slice(&channel.receive(TIMEOUT).unwrap()).unwrap();
    let mut offer = Offer {
        request,
        source_handle: child.process().0 as usize as u64,
        created: creation_time(child.process()).unwrap(),
    };
    let mut extra = None;
    match config {
        "nonce" => offer.request.nonce[0] ^= 1,
        "protocol" => offer.request.protocol.push('x'),
        "null" => offer.source_handle = 0,
        "pseudo" => offer.source_handle = u64::MAX,
        "invalid" => offer.source_handle = 0x6fffffff,
        "not-process" => {
            let event = OwnedHandle(unsafe { CreateEventW(None, true, false, None).unwrap() });
            offer.source_handle = event.raw().0 as usize as u64;
            extra = Some(event);
        }
        "self" => {
            let mut raw = HANDLE::default();
            unsafe {
                DuplicateHandle(
                    GetCurrentProcess(),
                    GetCurrentProcess(),
                    GetCurrentProcess(),
                    &mut raw,
                    (PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SYNCHRONIZE).0,
                    false,
                    DUPLICATE_HANDLE_OPTIONS(0),
                )
                .unwrap();
            }
            let original = OwnedHandle(raw);
            offer.source_handle = original.raw().0 as usize as u64;
            offer.created = creation_time(original.raw()).unwrap();
            extra = Some(original);
        }
        "parent" => {
            offer.source_handle = parent.raw().0 as usize as u64;
            offer.created = creation_time(parent.raw()).unwrap();
        }
        "created" => offer.created ^= 1,
        "exited" => {
            unsafe {
                TerminateProcess(child.process(), 17).unwrap();
            }
            assert_eq!(
                unsafe { WaitForSingleObject(child.process(), 5000) },
                WAIT_OBJECT_0
            );
        }
        "closed" => {
            let mut raw = HANDLE::default();
            unsafe {
                DuplicateHandle(
                    GetCurrentProcess(),
                    child.process(),
                    GetCurrentProcess(),
                    &mut raw,
                    (PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SYNCHRONIZE).0,
                    false,
                    DUPLICATE_HANDLE_OPTIONS(0),
                )
                .unwrap();
            }
            offer.source_handle = raw.0 as usize as u64;
            drop(OwnedHandle(raw));
        }
        "unknown-field" | "wrong-image" | "wrong-account" | "wrong-job" => (),
        other => panic!("unknown test configuration {other}"),
    }
    let mut bytes = serde_json::to_vec(&offer).unwrap();
    if config == "unknown-field" {
        bytes.pop();
        bytes.extend_from_slice(b",\"unexpected\":true}");
    }
    channel.send(&bytes, TIMEOUT).unwrap();
    // Original child stays owned until receiver refuses and closes the pipe.
    assert!(channel.receive(TIMEOUT).is_err());
    drop(extra);
    drop(child);
}

#[path = "account_control_pull_host_tests.rs"]
mod hosted;

#[test]
fn bootstrap_pull_retains_only_read_observation_and_source_still_owns_child() {
    positive_offer("good");
}

#[test]
fn bootstrap_pull_synthetic_secondary_logon_inherits_original_strict_job() {
    positive_offer("logon-good");
}

fn positive_offer(config: &str) {
    let image = image();
    let job = Job::new(false).unwrap();
    let (server, address) = ControlServer::prepare(CLIENT).unwrap();
    let mut birth =
        SuspendedBootstrap::create(&image, &job, &address, Entry::OfferFixture).unwrap();
    let peer = birth.peer().unwrap();
    birth.resume_fixture().unwrap();
    let mut channel = server.accept(peer, TIMEOUT).unwrap();
    channel.send(config.as_bytes(), TIMEOUT).unwrap();
    let receiver =
        ProcessReceiver::create(&mut birth, channel, crate::sid::current_user_sid().unwrap())
            .unwrap();
    let mut observed = receiver.receive(TIMEOUT).unwrap();
    assert_read_only(observed.peer.process.raw());
    noninheritable(observed.peer.process.raw()).unwrap();
    assert_eq!(
        observed.created,
        creation_time(observed.peer.process.raw()).unwrap()
    );
    observed.peer.alive().unwrap();
    observed
        .channel
        .write_in_flight(b"test-drop-child", TIMEOUT)
        .unwrap();
    assert_eq!(
        unsafe { WaitForSingleObject(observed.peer.process.raw(), 5000) },
        WAIT_OBJECT_0
    );
    assert_eq!(
        unsafe { WaitForSingleObject(birth.child.process(), 5000) },
        WAIT_OBJECT_0
    );
    let mut code = u32::MAX;
    unsafe {
        GetExitCodeProcess(birth.child.process(), &mut code).unwrap();
    }
    assert_eq!(code, 0);
    assert!(birth.offer_attempted);
}

#[test]
fn bootstrap_pull_refuses_untrusted_values_and_wrong_native_identity() {
    for config in [
        "nonce",
        "protocol",
        "null",
        "pseudo",
        "invalid",
        "not-process",
        "self",
        "parent",
        "created",
        "exited",
        "closed",
        "unknown-field",
        "wrong-image",
        "wrong-account",
        "wrong-job",
    ] {
        let image = image();
        let job = Job::new(false).unwrap();
        let (server, address) = ControlServer::prepare(CLIENT).unwrap();
        let mut birth =
            SuspendedBootstrap::create(&image, &job, &address, Entry::OfferFixture).unwrap();
        let peer = birth.peer().unwrap();
        birth.resume_fixture().unwrap();
        let mut channel = server.accept(peer, TIMEOUT).unwrap();
        channel.send(config.as_bytes(), TIMEOUT).unwrap();
        let account = if config == "wrong-account" {
            CLIENT.into()
        } else {
            crate::sid::current_user_sid().unwrap()
        };
        let receiver = ProcessReceiver::create(&mut birth, channel, account).unwrap();
        let result = if config == "wrong-job" {
            // Test-only native policy drift between construction and exchange.
            containment::tests::with_fault(&job, "breakaway", || receiver.receive(TIMEOUT))
        } else {
            receiver.receive(TIMEOUT)
        };
        assert!(result.is_err(), "accepted {config}");
        drop(result);
        // Failed receive consumes the endpoint and the source owns cleanup.
        assert!(birth.offer_attempted);
        if config != "wrong-job" {
            assert_eq!(
                unsafe { WaitForSingleObject(birth.child.process(), 5000) },
                WAIT_OBJECT_0,
                "source cleanup {config}"
            );
            let mut code = u32::MAX;
            unsafe {
                GetExitCodeProcess(birth.child.process(), &mut code).unwrap();
            }
            assert_eq!(code, 0, "source fixture failed for {config}");
        }
    }
}

#[test]
fn bootstrap_pull_observation_does_not_keep_job_or_grandchild_alive() {
    let image = image();
    let job = Job::new(false).unwrap();
    let (server, address) = ControlServer::prepare(CLIENT).unwrap();
    let mut birth =
        SuspendedBootstrap::create(&image, &job, &address, Entry::OfferFixture).unwrap();
    let peer = birth.peer().unwrap();
    birth.resume_fixture().unwrap();
    let mut channel = server.accept(peer, TIMEOUT).unwrap();
    channel.send(b"broker-kill", TIMEOUT).unwrap();
    let observed =
        ProcessReceiver::create(&mut birth, channel, crate::sid::current_user_sid().unwrap())
            .unwrap()
            .receive(TIMEOUT)
            .unwrap();
    // Separate test-only read reference lets us observe death after releasing
    // the job borrow. It cannot delay last-job-handle closure.
    let retained = ExpectedPeer::retain(observed.peer.process.raw()).unwrap();
    drop(observed);
    drop(birth);
    // Bootstrap exit alone does not kill its still-suspended grandchild.
    assert_eq!(
        unsafe { WaitForSingleObject(retained.process.raw(), 0) },
        WAIT_TIMEOUT
    );
    drop(job);
    assert_eq!(
        unsafe { WaitForSingleObject(retained.process.raw(), 5000) },
        WAIT_OBJECT_0
    );
}

#[test]
fn bootstrap_pull_channel_from_different_original_birth_is_rejected() {
    let image = image();
    let job = Job::new(false).unwrap();
    let (server, address) = ControlServer::prepare(CLIENT).unwrap();
    let mut birth =
        SuspendedBootstrap::create(&image, &job, &address, Entry::WaitingFixture).unwrap();
    let peer = birth.peer().unwrap();
    birth.resume_fixture().unwrap();
    let mut channel = server.accept(peer, TIMEOUT).unwrap();
    assert_eq!(channel.receive(TIMEOUT).unwrap(), b"ready");
    let (_unused, other_address) = ControlServer::prepare(CLIENT).unwrap();
    let mut other =
        SuspendedBootstrap::create(&image, &job, &other_address, Entry::WaitingFixture).unwrap();
    assert!(
        ProcessReceiver::create(&mut other, channel, crate::sid::current_user_sid().unwrap())
            .is_err()
    );
    assert!(other.offer_attempted);
}
