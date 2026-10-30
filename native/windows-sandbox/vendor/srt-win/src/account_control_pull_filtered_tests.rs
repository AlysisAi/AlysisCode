//! Test-only lower-privilege broker. SAM setup remains in the elevated outer
//! disposable controller; this process cannot receive account-setup authority.
use super::*;
#[path = "account_control_pull_filtered_death_tests.rs"]
pub(super) mod death;
#[path = "account_control_pull_filtered_desktop_tests.rs"]
mod desktop;
#[cfg(target_arch = "x86_64")]
#[path = "account_control_pull_filtered_inflight_tests.rs"]
pub(in crate::account_control::bootstrap::pull::tests) mod inflight;
#[path = "account_control_pull_filtered_loader_tests.rs"]
mod loader;
#[path = "account_control_pull_filtered_security_tests.rs"]
mod security;
const FIXTURE: &str =
    "account_control::bootstrap::pull::tests::hosted::filtered::bootstrap_filtered_fixture";

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Report {
    pub passed: bool,
    pub stage: String,
    pub elevated: bool,
    pub admin_member: bool,
    pub only_traverse_privilege: bool,
    pub dedicated_runner_policy: bool,
    pub hresult: Option<i32>,
    pub worker_exit: Option<u32>,
    environment: Option<desktop::Probe>,
}

fn no_admin(token: HANDLE) -> Result<Report> {
    let mut elevation = TOKEN_ELEVATION::default();
    let mut length = 0;
    unsafe {
        GetTokenInformation(
            token,
            TokenElevation,
            Some((&mut elevation as *mut TOKEN_ELEVATION).cast()),
            size_of_val(&elevation) as u32,
            &mut length,
        )?;
    }
    let mut raw = HANDLE::default();
    unsafe {
        DuplicateTokenEx(
            token,
            TOKEN_QUERY | TOKEN_IMPERSONATE,
            None,
            SecurityImpersonation,
            TokenImpersonation,
            &mut raw,
        )?;
    }
    let copy = OwnedHandle(raw);
    let administrators = crate::sid::LocalPsid::from_string("S-1-5-32-544")?;
    let mut member = windows::core::BOOL::default();
    unsafe {
        CheckTokenMembership(Some(copy.raw()), administrators.as_psid(), &mut member)?;
    }
    let mut words = [0usize; 1024];
    unsafe {
        GetTokenInformation(
            token,
            TokenPrivileges,
            Some(words.as_mut_ptr().cast()),
            size_of_val(&words) as u32,
            &mut length,
        )?;
    }
    ensure!(
        (size_of::<u32>()..=size_of_val(&words)).contains(&(length as usize)),
        "invalid privilege buffer"
    );
    let header = unsafe { &*words.as_ptr().cast::<TOKEN_PRIVILEGES>() };
    let offset = header.Privileges.as_ptr() as usize - words.as_ptr() as usize;
    ensure!(
        offset <= length as usize
            && header.PrivilegeCount as usize
                <= (length as usize - offset) / size_of::<LUID_AND_ATTRIBUTES>(),
        "invalid privilege array"
    );
    let privileges = unsafe {
        std::slice::from_raw_parts(header.Privileges.as_ptr(), header.PrivilegeCount as usize)
    };
    let mut traverse = LUID::default();
    unsafe {
        LookupPrivilegeValueW(
            None,
            pcwstr(&wstr("SeChangeNotifyPrivilege")),
            &mut traverse,
        )?;
    }
    let only_traverse = privileges.iter().all(|p| {
        p.Attributes & SE_PRIVILEGE_ENABLED == TOKEN_PRIVILEGES_ATTRIBUTES(0) || p.Luid == traverse
    });
    Ok(Report {
        passed: false,
        stage: "token".into(),
        elevated: elevation.TokenIsElevated != 0,
        admin_member: member.as_bool(),
        only_traverse_privilege: only_traverse,
        dedicated_runner_policy: false,
        hresult: None,
        worker_exit: None,
        environment: None,
    })
}

fn create(
    image: &PinnedRunnerImage,
    address: &ControlAddress,
    private_station: bool,
    debug_entry: Option<&'static str>,
) -> Result<(SpawnedChild, OwnedHandle, desktop::Prepared, desktop::Probe)> {
    let original = OwnedHandle(crate::token::open_self_token()?);
    let administrators = crate::sid::LocalPsid::from_string("S-1-5-32-544")?;
    let disabled = [SID_AND_ATTRIBUTES {
        Sid: administrators.as_psid(),
        Attributes: 0,
    }];
    let mut raw = HANDLE::default();
    unsafe {
        CreateRestrictedToken(
            original.raw(),
            LUA_TOKEN | DISABLE_MAX_PRIVILEGE,
            Some(&disabled),
            None,
            None,
            &mut raw,
        )?;
    }
    let token = OwnedHandle(raw);
    security::prepare_owned_token(&token)?;
    // LUA_TOKEN removes administrative groups/privileges but, on the hosted
    // elevated controller, preserves High IL. Set the test-owned duplicate to
    // Medium explicitly; never change the controller's original token.
    let medium = crate::sid::LocalPsid::from_string("S-1-16-8192")?;
    let mandatory = TOKEN_MANDATORY_LABEL {
        Label: SID_AND_ATTRIBUTES {
            Sid: medium.as_psid(),
            Attributes: 0x20, // SE_GROUP_INTEGRITY (winnt.h)
        },
    };
    unsafe {
        SetTokenInformation(
            token.raw(),
            TokenIntegrityLevel,
            (&mandatory as *const TOKEN_MANDATORY_LABEL).cast(),
            (size_of_val(&mandatory) + medium.as_bytes().len()) as u32,
        )?;
    }
    let state = no_admin(token.raw())?;
    ensure!(
        !state.elevated && !state.admin_member && state.only_traverse_privilege,
        "test broker token still has administrative authority"
    );
    let mut probe = desktop::Probe::observe(token.raw())?;
    probe.startup_security = Some(security::Probe::observe(&token)?);
    probe.require_medium()?;
    let mut desktop = desktop::Prepared::create(private_station)?;
    desktop.observe(token.raw(), &mut probe)?;
    let mut inherited = HANDLE::default();
    unsafe {
        DuplicateHandle(
            GetCurrentProcess(),
            GetCurrentProcess(),
            GetCurrentProcess(),
            &mut inherited,
            (PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SYNCHRONIZE).0,
            true,
            DUPLICATE_HANDLE_OPTIONS(0),
        )?;
    }
    let parent = OwnedHandle(inherited);
    let security = SECURITY_ATTRIBUTES {
        nLength: size_of::<SECURITY_ATTRIBUTES>() as u32,
        lpSecurityDescriptor: std::ptr::null_mut(),
        bInheritHandle: true.into(),
    };
    let mut read = HANDLE::default();
    let mut write = HANDLE::default();
    unsafe {
        CreatePipe(&mut read, &mut write, Some(&security), 4096)?;
    }
    let read = OwnedHandle(read);
    let write = OwnedHandle(write);
    unsafe {
        SetHandleInformation(read.raw(), HANDLE_FLAG_INHERIT.0, HANDLE_FLAGS(0))?;
    }
    let input = OwnedHandle(unsafe {
        CreateFileW(
            pcwstr(&wstr("NUL")),
            GENERIC_READ.0,
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            Some(&security),
            OPEN_EXISTING,
            FILE_ATTRIBUTE_NORMAL,
            None,
        )?
    });
    let path = image.application()?;
    let app = wstr(path.to_str().context("invalid fixture image")?);
    let entry = debug_entry.unwrap_or(FIXTURE);
    let mut command = wstr(&format!(
        "{} --exact {entry} --ignored --nocapture --test-threads=1",
        crate::launch::quote_arg(path.to_str().unwrap())
    ));
    let directory = os_directory(false)?;
    let mut env = crate::launch::build_env_block_with_inheritance(
        &[
            ("SystemRoot".into(), os_directory(true)?),
            ("PATH".into(), directory.clone()),
            (
                "ALYSIS_FILTERED_PEER".into(),
                (parent.raw().0 as usize).to_string(),
            ),
            (
                "ALYSIS_FILTERED_ADDRESS".into(),
                serde_json::to_string(address)?,
            ),
            (
                "ALYSIS_FILTERED_DESKTOP".into(),
                String::from_utf16(&desktop.path[..desktop.path.len() - 1])?,
            ),
        ],
        false,
    );
    let mut attributes = ProcThreadAttrs::new(1)?;
    attributes.set_handle_list(&[parent.raw(), input.raw(), write.raw()])?;
    let mut startup = STARTUPINFOEXW::default();
    startup.StartupInfo.cb = size_of::<STARTUPINFOEXW>() as u32;
    startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
    startup.StartupInfo.hStdInput = input.raw();
    startup.StartupInfo.hStdOutput = write.raw();
    startup.StartupInfo.hStdError = write.raw();
    startup.StartupInfo.lpDesktop = PWSTR(desktop.path.as_mut_ptr());
    startup.lpAttributeList = attributes.list();
    let mut info = PROCESS_INFORMATION::default();
    unsafe {
        CreateProcessAsUserW(
            Some(token.raw()),
            pcwstr(&app),
            Some(PWSTR(command.as_mut_ptr())),
            None,
            None,
            true,
            CREATE_SUSPENDED
                | CREATE_NO_WINDOW
                | CREATE_UNICODE_ENVIRONMENT
                | EXTENDED_STARTUPINFO_PRESENT
                | if debug_entry.is_some() {
                    DEBUG_ONLY_THIS_PROCESS
                } else {
                    PROCESS_CREATION_FLAGS(0)
                },
            Some(env.as_mut_ptr().cast()),
            pcwstr(&wstr(&directory)),
            &startup.StartupInfo,
            &mut info,
        )?;
    }
    let child = SpawnedChild::new(info);
    if debug_entry.is_none() {
        probe
            .startup_security
            .as_mut()
            .unwrap()
            .child(&child, &token)?;
        let peer = ExpectedPeer::retain(child.process())?.with_image(image)?;
        peer.alive()?;
    }
    // Diagnostic mode returns immediately so its debugger guard can be armed
    // before any fallible observations. It supplies no account/launch authority.
    Ok((child, read, desktop, probe))
}

#[derive(Clone, Debug, Serialize)]
pub(super) struct StartupFailure {
    phase: String,
    native_hresult: Option<i32>,
    child_exit_code: Option<u32>,
    environment: Option<desktop::Probe>,
}
impl std::fmt::Display for StartupFailure {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "filtered fixture startup failed; inspect sanitized native diagnostic"
        )
    }
}
impl std::error::Error for StartupFailure {}

fn diagnostic(pipe: HANDLE, code: Option<u32>) -> StartupFailure {
    let mut observed = StartupFailure {
        phase: "no_native_marker".into(),
        native_hresult: None,
        child_exit_code: code,
        environment: None,
    };
    let mut available = 0;
    if unsafe { PeekNamedPipe(pipe, None, 0, None, Some(&mut available), None) }.is_err() {
        return observed;
    }
    let mut bytes = vec![0; (available as usize).min(4096)];
    let mut read = 0;
    if bytes.is_empty()
        || unsafe { ReadFile(pipe, Some(&mut bytes), Some(&mut read), None) }.is_err()
    {
        return observed;
    }
    bytes.truncate(read as usize);
    // Persist only a fixed phase and numeric error, never arbitrary stderr text.
    for line in String::from_utf8_lossy(&bytes).lines() {
        if let Some((_, fields)) = line.split_once("ALYSIS_FILTERED_STARTUP ") {
            let fields: Vec<_> = fields.split_ascii_whitespace().collect();
            if fields.len() == 2
                && [
                    "inherited",
                    "image",
                    "desktop",
                    "parent",
                    "connect",
                    "token",
                    "exchange",
                ]
                .contains(&fields[0])
            {
                observed.phase = fields[0].into();
                observed.native_hresult = fields[1].parse().ok();
            }
        }
    }
    observed
}

#[test]
fn bootstrap_filtered_diagnostic_keeps_only_fixed_stage_and_numeric_error() {
    let mut read = HANDLE::default();
    let mut write = HANDLE::default();
    unsafe {
        CreatePipe(&mut read, &mut write, None, 4096).unwrap();
    }
    let read = OwnedHandle(read);
    let write = OwnedHandle(write);
    let bytes = b"unrelated text must not be persisted\nALYSIS_FILTERED_STARTUP foreign 123\ntest fixture ... ALYSIS_FILTERED_STARTUP parent -2147024891\n";
    unsafe {
        WriteFile(write.raw(), Some(bytes), None, None).unwrap();
    }
    let report = diagnostic(read.raw(), Some(101));
    assert_eq!(report.phase, "parent");
    assert_eq!(report.native_hresult, Some(-2147024891));
    assert_eq!(report.child_exit_code, Some(101));
    assert!(
        !serde_json::to_string(&report)
            .unwrap()
            .contains("unrelated")
    );
}

#[test]
#[ignore = "bounded filtered broker invoked only by original-handle parent tests"]
fn bootstrap_filtered_fixture() {
    unsafe {
        windows::Win32::System::Diagnostics::Debug::OutputDebugStringA(windows::core::s!(
            "ALYSIS_FILTERED_MAIN_ENTERED"
        ));
    }
    let mut stage = "inherited";
    let result = fixture_body(&mut stage);
    if let Err(error) = &result {
        let code = error
            .chain()
            .find_map(|e| e.downcast_ref::<windows::core::Error>())
            .map(|e| e.code().0)
            .unwrap_or(0);
        eprintln!("ALYSIS_FILTERED_STARTUP {stage} {code}");
    }
    assert!(
        result.is_ok(),
        "filtered fixture refused; inspect sanitized diagnostic"
    );
}

fn fixture_body(stage: &mut &'static str) -> Result<()> {
    let value: usize = std::env::var("ALYSIS_FILTERED_PEER")?.parse()?;
    ensure!(
        value > 0 && value < isize::MAX as usize,
        "invalid inherited fixture peer"
    );
    let inherited = OwnedHandle(HANDLE(value as *mut _));
    assert_read_only(inherited.raw());
    *stage = "desktop";
    let expected = std::env::var("ALYSIS_FILTERED_DESKTOP")?;
    let actual = format!(
        "{}\\{}",
        crate::winsta::current_winsta_name()?,
        crate::winsta::current_desktop_name().context("missing filtered desktop")?
    );
    ensure!(
        actual == expected && actual.contains("\\alysis-filtered-test-"),
        "filtered fixture attached to unexpected desktop"
    );
    *stage = "image";
    let image = PinnedRunnerImage::fixture(&std::env::current_exe()?)?;
    *stage = "parent";
    let peer = ExpectedPeer::retain(inherited.raw())?.with_image(&image)?;
    let address: ControlAddress = serde_json::from_str(&std::env::var("ALYSIS_FILTERED_ADDRESS")?)?;
    *stage = "connect";
    let mut channel = ControlChannel::connect(&address, peer)?;
    *stage = "token";
    let token = OwnedHandle(crate::token::open_self_token()?);
    desktop::Probe::observe(token.raw())?.require_medium()?;
    let mut report = no_admin(token.raw())?;
    *stage = "exchange";
    let result = (|| -> Result<()> {
        ensure!(
            !report.elevated && !report.admin_member && report.only_traverse_privilege,
            "filtered broker effective authority differs"
        );
        let mode = channel.receive(HOST_TIMEOUT)?;
        #[cfg(target_arch = "x86_64")]
        if mode == b"account-inflight" || mode == b"local-inflight" {
            return inflight::broker(
                &mut channel,
                &image,
                mode == b"account-inflight",
                &mut report,
            );
        }
        let hold = mode == b"account-hold" || mode == b"local-hold";
        let secret = if mode == b"account" || mode == b"account-hold" {
            let frame = SecretFrame(channel.receive(HOST_TIMEOUT)?);
            Some(serde_json::from_slice::<SecretLogon>(&frame.0)?)
        } else {
            ensure!(
                mode == b"local" || mode == b"local-hold",
                "invalid filtered fixture command"
            );
            None
        };
        report.stage = "atomic_bootstrap".into();
        let job = Job::new(false)?;
        let (server, address) = ControlServer::prepare(CLIENT)?;
        let entry = if secret.is_some() {
            Entry::OfferFixture
        } else {
            Entry::WaitingFixture
        };
        let mut birth = SuspendedBootstrap::create(&image, &job, &address, entry)?;
        if let Some(secret) = secret {
            let account = crate::user::local_user_sid(&secret.name)?
                .context("missing fresh account identity")?;
            let peer = birth.peer()?;
            birth.resume_fixture()?;
            let mut control = server.accept(peer, HOST_TIMEOUT)?;
            control.send(b"hosted-account", HOST_TIMEOUT)?;
            let frame = SecretFrame(serde_json::to_vec(&secret)?);
            control.send(&frame.0, HOST_TIMEOUT)?;
            drop(frame);
            drop(secret);
            report.stage = "real_account_logon".into();
            let response: LogonResult = serde_json::from_slice(&control.receive(HOST_TIMEOUT)?)?;
            report.hresult = response.hresult.or(response.observation_hresult);
            ensure!(
                response.created
                    && response.source_observable
                    && response.runner_policy_verified
                    && report.hresult.is_none(),
                "filtered account logon/source observation refused"
            );
            report.stage = "original_account_observation".into();
            let mut observed = ProcessReceiver::create(&mut birth, control, account.clone())?
                .receive(HOST_TIMEOUT)?;
            assert_read_only(observed.peer.process.raw());
            let mut session = 0;
            unsafe {
                windows::Win32::System::RemoteDesktop::ProcessIdToSessionId(
                    std::process::id(),
                    &mut session,
                )?;
            }
            observed
                .peer
                .token
                .require_dedicated_runner(&account, session, "S-1-5-32-545")?;
            report.dedicated_runner_policy = true;
            if hold {
                return death::hold(&mut channel, observed.peer.process.raw());
            }
            report.stage = "original_child_exit".into();
            observed
                .channel
                .write_in_flight(b"test-drop-child", HOST_TIMEOUT)?;
            ensure!(
                unsafe { WaitForSingleObject(observed.peer.process.raw(), 5000) } == WAIT_OBJECT_0,
                "account child exit unverified"
            );
            ensure!(
                unsafe { WaitForSingleObject(birth.child.process(), 5000) } == WAIT_OBJECT_0,
                "bootstrap exit unverified"
            );
            let mut code = u32::MAX;
            unsafe {
                GetExitCodeProcess(birth.child.process(), &mut code)?;
            }
            ensure!(code == 0, "bootstrap fixture did not finish");
        } else if hold {
            return death::hold(&mut channel, birth.child.process());
        }
        drop(birth);
        drop(job);
        report.stage = "complete".into();
        Ok(())
    })();
    if let Err(error) = &result {
        report.hresult = report.hresult.or_else(|| {
            error
                .chain()
                .find_map(|e| e.downcast_ref::<windows::core::Error>())
                .map(|e| e.code().0)
        });
    }
    report.passed = result.is_ok();
    channel.send(&serde_json::to_vec(&report)?, HOST_TIMEOUT)?;
    ensure!(
        channel.receive(HOST_TIMEOUT)? == b"finish",
        "invalid fixture finish request"
    );
    Ok(())
}

pub(super) fn qualify(secret: Option<&SecretLogon>) -> Result<Report> {
    let image = image();
    // This is only the elevated test-controller/filtered-broker pipe. Production
    // broker/bootstrap peers share an integrity context; their policy is untouched.
    let (server, address) = desktop::controller_pipe()?;
    let (child, diagnostic_pipe, _desktop, probe) =
        create(&image, &address, secret.is_some(), None)?;
    let result = (|| -> Result<Report> {
        probe.startup_security.as_ref().unwrap().require_owned()?;
        let peer = ExpectedPeer::retain(child.process())?.with_image(&image)?;
        ensure!(
            unsafe { ResumeThread(child.thread()) } == 1,
            "filtered fixture initial suspension differs"
        );
        let mut channel = server.accept(peer, HOST_TIMEOUT)?;
        channel.send(
            if secret.is_some() {
                b"account"
            } else {
                b"local"
            },
            HOST_TIMEOUT,
        )?;
        if let Some(secret) = secret {
            let frame = SecretFrame(serde_json::to_vec(secret)?);
            channel.send(&frame.0, HOST_TIMEOUT)?;
        }
        let mut report: Report = serde_json::from_slice(&channel.receive(HOST_TIMEOUT)?)?;
        ensure!(
            report.environment.is_none(),
            "unexpected child environment claim"
        );
        report.environment = Some(probe.clone());
        channel.write_in_flight(b"finish", HOST_TIMEOUT)?;
        ensure!(
            unsafe { WaitForSingleObject(child.process(), 5000) } == WAIT_OBJECT_0,
            "filtered broker exit unverified"
        );
        let mut code = u32::MAX;
        unsafe {
            GetExitCodeProcess(child.process(), &mut code)?;
        }
        ensure!(code == 0, "filtered broker fixture failed");
        Ok(report)
    })();
    match result {
        Ok(report) => Ok(report),
        Err(error) => {
            let mut code = 0;
            let code = unsafe { GetExitCodeProcess(child.process(), &mut code) }
                .ok()
                .map(|_| code);
            drop(child); // Verified exit before reading bounded startup output.
            let mut observed = diagnostic(diagnostic_pipe.raw(), code);
            observed.environment = Some(probe);
            Err(error).context(observed)
        }
    }
}

#[test]
fn bootstrap_filtered_broker_has_no_admin_authority_and_creates_strict_bootstrap() {
    let report = qualify(None).unwrap();
    eprintln!(
        "filtered startup security: {}",
        serde_json::to_string(&report.environment.as_ref().unwrap().startup_security).unwrap()
    );
    assert!(!report.elevated && !report.admin_member && report.only_traverse_privilege);
    assert!(
        report.passed,
        "filtered fixture stage={} hresult={:?}",
        report.stage, report.hresult
    );
}
