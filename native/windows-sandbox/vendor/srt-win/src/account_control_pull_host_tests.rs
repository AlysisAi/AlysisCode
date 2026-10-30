//! Opt-in real-account observations on one disposable hosted Windows VM. No
//! production install or SDK profile. The account process never resumes.
use super::*;
use std::io::{Seek, SeekFrom};
use windows::Win32::NetworkManagement::NetManagement::*;
use windows::Win32::Security::*;
use windows::core::{PCWSTR, PWSTR};

const SENTINEL: &str = "github-hosted-bootstrap-account-only";
const TEST: &str = "bootstrap_account_disposable_original_observation";
const HOST_TIMEOUT: Duration = Duration::from_secs(10);

#[path = "account_control_pull_filtered_tests.rs"]
pub(super) mod filtered;

fn admission(values: &[Option<String>], run: &str, attempt: &str) -> bool {
    let expected = [
        SENTINEL,
        "true",
        "github-hosted",
        "Windows",
        "AlysisAi/alysis-code-internal",
        "workflow_dispatch",
    ];
    values.len() == expected.len()
        && values
            .iter()
            .zip(expected)
            .all(|(v, x)| v.as_deref() == Some(x))
        && [run, attempt]
            .iter()
            .all(|s| !s.is_empty() && s.len() <= 24 && s.bytes().all(|b| b.is_ascii_digit()))
        && cfg!(all(windows, target_arch = "x86_64"))
}

#[test]
fn bootstrap_account_host_admission_requires_all_operator_gates() {
    let good = [
        SENTINEL,
        "true",
        "github-hosted",
        "Windows",
        "AlysisAi/alysis-code-internal",
        "workflow_dispatch",
    ]
    .map(|s| Some(s.to_owned()));
    assert!(admission(&good, "1", "2"));
    for i in 0..good.len() {
        let mut bad = good.clone();
        bad[i] = None;
        assert!(!admission(&bad, "1", "2"));
        bad[i] = Some("wrong".into());
        assert!(!admission(&bad, "1", "2"));
    }
    for bad in ["", "x", "1234567890123456789012345"] {
        assert!(!admission(&good, bad, "1"));
        assert!(!admission(&good, "1", bad));
    }
}

fn scrub<T: Default + Copy>(value: &mut [T]) {
    for item in value {
        unsafe {
            std::ptr::write_volatile(item, T::default());
        }
    }
    std::sync::atomic::compiler_fence(std::sync::atomic::Ordering::SeqCst);
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct SecretLogon {
    name: String,
    password: Vec<u16>,
}
impl Drop for SecretLogon {
    fn drop(&mut self) {
        scrub(&mut self.password);
    }
}
struct SecretFrame(Vec<u8>);
impl Drop for SecretFrame {
    fn drop(&mut self) {
        scrub(&mut self.0);
    }
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct LogonResult {
    created: bool,
    hresult: Option<i32>,
    source_observable: bool,
    observation_hresult: Option<i32>,
    runner_policy_verified: bool,
}

pub(super) fn offer_account_child(
    mut channel: ControlChannel<'_>,
    image: &PinnedRunnerImage,
) -> Result<()> {
    // This test-only command is accepted solely over the original authenticated
    // host-broker channel. No password in argv, environment, disk or diagnostics.
    let frame = SecretFrame(channel.receive(HOST_TIMEOUT)?);
    let secret: SecretLogon =
        serde_json::from_slice(&frame.0).context("invalid synthetic logon frame")?;
    ensure!(
        secret.name.starts_with("aQA")
            && secret.name.len() == 19
            && secret.name[3..].bytes().all(|c| c.is_ascii_hexdigit()),
        "invalid fresh test account name"
    );
    ensure!(
        (32..=128).contains(&secret.password.len())
            && secret.password.last() == Some(&0)
            && !secret.password[..secret.password.len() - 1].contains(&0),
        "invalid synthetic credential shape"
    );
    let account =
        crate::user::local_user_sid(&secret.name)?.context("fresh account SID missing")?;
    let mut session = 0;
    unsafe {
        windows::Win32::System::RemoteDesktop::ProcessIdToSessionId(
            std::process::id(),
            &mut session,
        )?;
    }
    let app = wstr(
        image
            .application()?
            .to_str()
            .context("non-Unicode test image")?,
    );
    let mut command = wstr(&crate::launch::quote_arg(
        image.application()?.to_str().unwrap(),
    ));
    let startup = STARTUPINFOW {
        cb: size_of::<STARTUPINFOW>() as u32,
        ..Default::default()
    };
    let mut info = PROCESS_INFORMATION::default();
    let result = unsafe {
        CreateProcessWithLogonW(
            pcwstr(&wstr(&secret.name)),
            pcwstr(&wstr(".")),
            PCWSTR(secret.password.as_ptr()),
            CREATE_PROCESS_LOGON_FLAGS(0),
            pcwstr(&app),
            Some(PWSTR(command.as_mut_ptr())),
            CREATE_SUSPENDED | CREATE_NO_WINDOW,
            None,
            pcwstr(&wstr(&os_directory(false)?)),
            &startup,
            &mut info,
        )
    };
    drop(secret);
    drop(frame);
    match result {
        Ok(()) => {
            let child = SpawnedChild::new(info); // armed before sending any data
            let observation = ExpectedPeer::retain(child.process()).and_then(|peer| {
                peer.token
                    .require_dedicated_runner(&account, session, "S-1-5-32-545")?;
                Ok(peer)
            });
            let observation_hresult = observation.as_ref().err().and_then(|e| {
                e.chain()
                    .find_map(|c| c.downcast_ref::<windows::core::Error>())
                    .map(|e| e.code().0)
            });
            channel.send(
                &serde_json::to_vec(&LogonResult {
                    created: true,
                    hresult: None,
                    source_observable: observation.is_ok(),
                    observation_hresult,
                    runner_policy_verified: observation.is_ok(),
                })?,
                HOST_TIMEOUT,
            )?;
            if observation.is_err() {
                let _ = channel.receive(HOST_TIMEOUT);
                anyhow::bail!("source account child observation refused");
            }
            let mut pending = PendingProcessOffer::send(&child, channel, HOST_TIMEOUT)?;
            ensure!(
                pending._channel.receive(HOST_TIMEOUT)? == b"test-drop-child",
                "invalid fixture retirement request"
            );
            drop(pending);
            drop(child);
            Ok(())
        }
        Err(error) => {
            channel.send(
                &serde_json::to_vec(&LogonResult {
                    created: false,
                    hresult: Some(error.code().0),
                    source_observable: false,
                    observation_hresult: None,
                    runner_policy_verified: false,
                })?,
                HOST_TIMEOUT,
            )?;
            // Keep the source alive until the controller reads sanitized failure.
            let _ = channel.receive(HOST_TIMEOUT);
            anyhow::bail!("disposable account logon refused")
        }
    }
}

fn native_status(status: u32, stage: &'static str) -> Result<()> {
    ensure!(status == 0, "{stage} native status {status}");
    Ok(())
}

fn create_account() -> Result<(SecretLogon, String)> {
    crate::local_account_host::require_local_sam()?;
    let random = ControlAddress::fresh()?.nonce;
    let suffix: String = random[..8].iter().map(|b| format!("{b:02x}")).collect();
    let name = format!("aQA{suffix}");
    ensure!(
        crate::user::local_user_sid(&name)?.is_none(),
        "test account collision"
    );
    let random = ControlAddress::fresh()?.nonce;
    let mut password = vec![b'A' as u16, b'a' as u16, b'1' as u16, b'!' as u16];
    for byte in random {
        for c in format!("{byte:02x}").bytes() {
            password.push(c as u16);
        }
    }
    password.push(0);
    let mut secret = SecretLogon { name, password };
    let mut name = wstr(&secret.name);
    let info = USER_INFO_1 {
        usri1_name: PWSTR(name.as_mut_ptr()),
        usri1_password: PWSTR(secret.password.as_mut_ptr()),
        usri1_priv: USER_PRIV_USER,
        usri1_flags: UF_SCRIPT | UF_DONT_EXPIRE_PASSWD | UF_ACCOUNTDISABLE,
        ..Default::default()
    };
    native_status(
        unsafe {
            NetUserAdd(
                PCWSTR::null(),
                1,
                (&info as *const USER_INFO_1).cast(),
                None,
            )
        },
        "create fresh disabled account",
    )?;
    let sid = crate::user::local_user_sid(&secret.name)?.context("missing fresh account SID")?;
    let users = crate::sid::lookup_account_name("S-1-5-32-545")?;
    let account_sid = crate::sid::LocalPsid::from_string(&sid)?;
    let member = LOCALGROUP_MEMBERS_INFO_0 {
        lgrmi0_sid: account_sid.as_psid(),
    };
    native_status(
        unsafe {
            NetLocalGroupAddMembers(
                PCWSTR::null(),
                pcwstr(&wstr(&users)),
                0,
                (&member as *const LOCALGROUP_MEMBERS_INFO_0).cast(),
                1,
            )
        },
        "grant builtin Users membership",
    )?;
    set_enabled(&secret.name, &sid, true)?;
    Ok((secret, sid))
}

fn set_enabled(name: &str, sid: &str, enabled: bool) -> Result<()> {
    crate::local_account_host::require_local_sam()?;
    ensure!(
        crate::user::local_user_sid(name)?.as_deref() == Some(sid),
        "test account identity changed"
    );
    let info = USER_INFO_1008 {
        usri1008_flags: UF_SCRIPT
            | UF_DONT_EXPIRE_PASSWD
            | if enabled {
                USER_ACCOUNT_FLAGS(0)
            } else {
                UF_ACCOUNTDISABLE
            },
    };
    native_status(
        unsafe {
            NetUserSetInfo(
                PCWSTR::null(),
                pcwstr(&wstr(name)),
                1008,
                (&info as *const USER_INFO_1008).cast(),
                None,
            )
        },
        "set owned test account flags",
    )?;
    let mut buffer = std::ptr::null_mut();
    native_status(
        unsafe { NetUserGetInfo(PCWSTR::null(), pcwstr(&wstr(name)), 1, &mut buffer) },
        "read owned account flags",
    )?;
    ensure!(!buffer.is_null(), "missing account flags buffer");
    let (flags, privilege) = unsafe {
        let info = &*buffer.cast::<USER_INFO_1>();
        (info.usri1_flags, info.usri1_priv)
    };
    unsafe {
        NetApiBufferFree(Some(buffer.cast()));
    }
    ensure!(
        (flags & UF_ACCOUNTDISABLE != USER_ACCOUNT_FLAGS(0)) == !enabled
            && privilege == USER_PRIV_USER,
        "owned account enabled/ordinary-user state differs"
    );
    Ok(())
}

#[test]
#[ignore = "one fresh account, disposable manually selected GitHub-hosted VM only"]
fn bootstrap_account_disposable_original_observation() {
    let values = [
        "ALYSIS_RUN_DISPOSABLE_BOOTSTRAP_ACCOUNT",
        "GITHUB_ACTIONS",
        "RUNNER_ENVIRONMENT",
        "RUNNER_OS",
        "GITHUB_REPOSITORY",
        "GITHUB_EVENT_NAME",
    ]
    .map(|name| std::env::var(name).ok());
    let run = std::env::var("GITHUB_RUN_ID").unwrap_or_default();
    let attempt = std::env::var("GITHUB_RUN_ATTEMPT").unwrap_or_default();
    assert!(
        admission(&values, &run, &attempt),
        "disposable account observation admission refused"
    );
    let directory = std::path::PathBuf::from(
        std::env::var_os("RUNNER_TEMP").expect("CI evidence directory required"),
    );
    assert!(directory.is_absolute() && directory.is_dir());
    let mut file = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(directory.join("alysis-bootstrap-account.json"))
        .unwrap();
    let mut stages = Vec::<&str>::new();
    let mut stage = "admission";
    let mut logon_hresult = None;
    let mut source_observation_hresult = None;
    let mut broker_elevated = None;
    let mut filtered_report = None;
    let mut filtered_startup = None;
    let mut filtered_death = None;
    let mut filtered_inflight = Vec::new();
    let mut account_disabled_afterward = false;
    macro_rules! enter {
        ($value:literal) => {{
            stage = $value;
            file.seek(SeekFrom::Start(0))?; file.set_len(0)?;
            serde_json::to_writer_pretty(&mut file, &serde_json::json!({"schema":2,"test":TEST,
                "run_id":run,"run_attempt":attempt,"passed":false,"stage":stage,
                "completed_stages":stages,"synthetic_account_limit":1,
                "account_child_never_resumed":true,"production_install_changed":false,
                "resources_preserved_for_vm_disposal":true}))?;
            file.sync_all()?;
        }};
    }
    let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| -> Result<()> {
        enter!("broker_token");
        let token = OwnedHandle(crate::token::open_self_token()?);
        let mut elevation = TOKEN_ELEVATION::default();
        let mut length = 0;
        unsafe {
            GetTokenInformation(
                token.raw(),
                TokenElevation,
                Some((&mut elevation as *mut TOKEN_ELEVATION).cast()),
                size_of_val(&elevation) as u32,
                &mut length,
            )?;
        }
        broker_elevated = Some(elevation.TokenIsElevated != 0);
        stages.push(stage);
        enter!("atomic_bootstrap");
        let image = image();
        let job = Job::new(false)?;
        let (server, address) = ControlServer::prepare(CLIENT)?;
        let mut birth = SuspendedBootstrap::create(&image, &job, &address, Entry::OfferFixture)?;
        stages.push(stage);
        enter!("fresh_account");
        let (secret, account) = create_account()?;
        stages.push(stage);
        let outcome = (|| -> Result<()> {
            enter!("bootstrap_control");
            let peer = birth.peer()?;
            birth.resume_fixture()?;
            let mut channel = server.accept(peer, HOST_TIMEOUT)?;
            channel.send(b"hosted-account", HOST_TIMEOUT)?;
            let frame = SecretFrame(serde_json::to_vec(&secret)?);
            channel.send(&frame.0, HOST_TIMEOUT)?;
            drop(frame);
            stages.push(stage);
            enter!("real_account_logon");
            let response: LogonResult = serde_json::from_slice(&channel.receive(HOST_TIMEOUT)?)?;
            logon_hresult = response.hresult;
            source_observation_hresult = response.observation_hresult;
            ensure!(
                response.created
                    && response.hresult.is_none()
                    && response.source_observable
                    && response.observation_hresult.is_none(),
                "account logon did not create child"
            );
            stages.push(stage);
            enter!("original_account_observation");
            let receiver = ProcessReceiver::create(&mut birth, channel, account.clone())?;
            let mut observed = receiver.receive(HOST_TIMEOUT)?;
            assert_read_only(observed.peer.process.raw());
            ensure!(
                account != crate::sid::current_user_sid()?,
                "separate account equals broker"
            );
            stages.push(stage);
            enter!("dedicated_account_token_policy");
            let mut session = 0;
            unsafe {
                windows::Win32::System::RemoteDesktop::ProcessIdToSessionId(
                    std::process::id(),
                    &mut session,
                )?;
            }
            ensure!(
                response.runner_policy_verified,
                "source dedicated account token policy unverified"
            );
            observed
                .peer
                .token
                .require_dedicated_runner(&account, session, "S-1-5-32-545")?;
            stages.push(stage);
            enter!("original_child_exit");
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
            ensure!(code == 0, "bootstrap test failed");
            stages.push(stage);
            enter!("filtered_broker_account_observation");
            let observed = filtered::qualify(Some(&secret)).map_err(|error| {
                filtered_startup = error.downcast_ref::<filtered::StartupFailure>().cloned();
                error
            })?;
            let accepted = observed.passed
                && !observed.elevated
                && !observed.admin_member
                && observed.only_traverse_privilege;
            filtered_report = Some(observed);
            ensure!(
                accepted,
                "filtered broker account observation refused; inspect sanitized report"
            );
            stages.push(stage);
            enter!("filtered_broker_death");
            filtered_death = Some(filtered::death::qualify(Some(&secret), &account)?);
            stages.push(stage);
            #[cfg(target_arch = "x86_64")]
            {
                enter!("filtered_broker_inflight_death");
                filtered::inflight::qualify(&secret, &account, &mut filtered_inflight)?;
                stages.push(stage);
            }
            Ok(())
        })();
        // Only the fresh original account is disabled; no account deletion or
        // name-based ownership adoption. VM disposal owns partial-failure cleanup.
        let disabled = set_enabled(&secret.name, &account, false);
        account_disabled_afterward = disabled.is_ok();
        outcome?;
        disabled?;
        stage = "original_account_disabled";
        stages.push(stage);
        Ok(())
    }));
    let passed = matches!(&result, Ok(Ok(())));
    let error_hresult = match &result {
        Ok(Err(e)) => e
            .chain()
            .find_map(|c| c.downcast_ref::<windows::core::Error>())
            .map(|e| e.code().0),
        _ => None,
    };
    let report = serde_json::json!({"schema":2,"test":TEST,"run_id":run,"run_attempt":attempt,"passed":passed,
        "stage":stage,"completed_stages":stages,"logon_hresult":logon_hresult,"error_hresult":error_hresult,
        "source_observation_hresult":source_observation_hresult,
        "broker_elevated":broker_elevated,"synthetic_account_limit":1,"account_child_never_resumed":true,
        "filtered_broker":filtered_report,
        "filtered_startup":filtered_startup,
        "filtered_broker_death":filtered_death,
        "filtered_broker_inflight":filtered_inflight,
        "account_disabled_afterward":account_disabled_afterward,
        "production_install_changed":false,"sdk_profile_created":false,"appcontainer_composition_qualified":false,
        "full_lifecycle_qualified":false,"resources_preserved_for_vm_disposal":true});
    file.seek(SeekFrom::Start(0)).unwrap();
    file.set_len(0).unwrap();
    serde_json::to_writer_pretty(&mut file, &report).unwrap();
    file.sync_all().unwrap();
    assert!(
        passed,
        "disposable account observation failed at {stage}; inspect sanitized report"
    );
}
