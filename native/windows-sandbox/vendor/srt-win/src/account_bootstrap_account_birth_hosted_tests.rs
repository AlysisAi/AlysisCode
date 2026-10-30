//! Fixed candidate SAM principals only on an explicitly selected disposable VM.
//! Fixture storage and a synthetic secret do not assert installation readiness.
use super::*;
use crate::account_install::{ACCOUNT_GROUP, OFFLINE_USER, ONLINE_USER};
use crate::util::{pcwstr, wstr};
use windows::Win32::NetworkManagement::NetManagement::*;
use windows::core::{PCWSTR, PWSTR};

#[path = "account_bootstrap_account_birth_crash_tests.rs"]
mod crash;

#[path = "account_rpc_v3_lifecycle_hosted_tests.rs"]
mod rpc_v3;
#[test]
#[ignore = "V3 original writer lifecycle; separately admitted disposable Windows VM only"]
fn account_rpc_v3_lifecycle_disposable() {
    rpc_v3::run();
}
#[test]
#[ignore = "V3 held writer broker; exact admitted controller and stdin-only synthetic secret"]
fn paired_rpc_v3_profile_held_broker() {
    require_host().unwrap();
    let _admission = crate::account_rpc_v3_lifecycle_fixture::enter().unwrap();
    use std::io::Read;
    let mut password = Password(String::with_capacity(128));
    std::io::stdin()
        .take(128)
        .read_to_string(&mut password.0)
        .unwrap();
    assert!(password.0.len() == 68 && password.0.is_ascii());
    super::super::paired::hosted_rpc_v3_profile_crash_broker(&password.0);
}

#[test]
#[ignore = "V3 shadow writer lifecycle; separately admitted disposable Windows VM only"]
fn account_rpc_v3_shadow_lifecycle_disposable() {
    rpc_v3::run_shadow();
}
#[test]
#[ignore = "V3 shadow held writer broker; exact admitted controller and stdin-only synthetic secret"]
fn paired_rpc_v3_shadow_profile_held_broker() {
    require_host().unwrap();
    let _admission = crate::account_rpc_v3_lifecycle_fixture::enter_shadow().unwrap();
    use std::io::Read;
    let mut password = Password(String::with_capacity(128));
    std::io::stdin()
        .take(128)
        .read_to_string(&mut password.0)
        .unwrap();
    assert!(password.0.len() == 68 && password.0.is_ascii());
    super::super::paired::hosted_rpc_v3_shadow_profile_crash_broker(&password.0);
}

fn require_host() -> Result<()> {
    ensure!(
        cfg!(all(windows, target_arch = "x86_64")),
        "hosted account birth requires Windows x64"
    );
    for (key, value) in [
        (
            "ALYSIS_RUN_DISPOSABLE_ACCOUNT_BIRTH",
            "github-hosted-account-birth-only",
        ),
        ("GITHUB_ACTIONS", "true"),
        ("RUNNER_ENVIRONMENT", "github-hosted"),
        ("RUNNER_OS", "Windows"),
        ("GITHUB_REPOSITORY", "AlysisAi/alysis-code-internal"),
        ("GITHUB_EVENT_NAME", "workflow_dispatch"),
    ] {
        ensure!(
            std::env::var(key).ok().as_deref() == Some(value),
            "hosted account birth admission refused"
        );
    }
    for key in ["GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT"] {
        let value = std::env::var(key)?;
        ensure!(
            (1..=24).contains(&value.len()) && value.bytes().all(|c| c.is_ascii_digit()),
            "hosted run identity invalid"
        );
    }
    crate::local_account_host::require_local_sam()
}
struct Password(String);
impl Drop for Password {
    fn drop(&mut self) {
        unsafe {
            for b in self.0.as_mut_vec() {
                std::ptr::write_volatile(b, 0);
            }
        }
        std::sync::atomic::compiler_fence(std::sync::atomic::Ordering::SeqCst);
    }
}
fn success(status: u32) -> Result<()> {
    ensure!(status == 0, "owned hosted SAM operation failed: {status}");
    Ok(())
}
struct Principals {
    offline: Option<String>,
    online: Option<String>,
    group: Option<String>,
}
impl Principals {
    fn disabled(&self, name: &str, sid: &str, disabled: bool) -> Result<()> {
        ensure!(
            crate::user::local_user_sid(name)?.as_deref() == Some(sid),
            "owned hosted principal changed"
        );
        let flags = UF_SCRIPT
            | UF_DONT_EXPIRE_PASSWD
            | if disabled {
                UF_ACCOUNTDISABLE
            } else {
                USER_ACCOUNT_FLAGS(0)
            };
        let info = USER_INFO_1008 {
            usri1008_flags: flags,
        };
        success(unsafe {
            NetUserSetInfo(
                PCWSTR::null(),
                pcwstr(&wstr(name)),
                1008,
                (&info as *const USER_INFO_1008).cast(),
                None,
            )
        })?;
        let mut buffer = std::ptr::null_mut();
        success(unsafe { NetUserGetInfo(PCWSTR::null(), pcwstr(&wstr(name)), 1, &mut buffer) })?;
        ensure!(!buffer.is_null(), "owned hosted account flags missing");
        let (actual, privilege) = unsafe {
            let value = &*buffer.cast::<USER_INFO_1>();
            (value.usri1_flags, value.usri1_priv)
        };
        unsafe {
            let _ = NetApiBufferFree(Some(buffer.cast()));
        }
        ensure!(
            (actual & UF_ACCOUNTDISABLE != USER_ACCOUNT_FLAGS(0)) == disabled
                && privilege == USER_PRIV_USER,
            "owned hosted account flags differ"
        );
        Ok(())
    }
    fn disable_all(&self) -> Result<()> {
        let mut failure = None;
        for (name, sid) in [(OFFLINE_USER, &self.offline), (ONLINE_USER, &self.online)] {
            if let Some(sid) = sid {
                if let Err(error) = self.disabled(name, sid, true) {
                    failure = Some(error);
                }
            }
        }
        if let Some(error) = failure {
            return Err(error);
        }
        Ok(())
    }
}
impl Drop for Principals {
    fn drop(&mut self) {
        let _ = self.disable_all();
    }
}

fn create(principals: &mut Principals, password: &Password) -> Result<()> {
    create_mode(principals, password, AccountMode::Offline)
}
fn create_mode(principals: &mut Principals, password: &Password, mode: AccountMode) -> Result<()> {
    for name in [OFFLINE_USER, ONLINE_USER] {
        ensure!(
            crate::user::local_user_sid(name)?.is_none(),
            "hosted principal already exists; no adoption"
        );
    }
    ensure!(
        crate::user::local_group_sid(ACCOUNT_GROUP)?.is_none(),
        "hosted group already exists; no adoption"
    );
    let mut group_name = wstr(ACCOUNT_GROUP);
    let group = LOCALGROUP_INFO_1 {
        lgrpi1_name: PWSTR(group_name.as_mut_ptr()),
        ..Default::default()
    };
    success(unsafe {
        NetLocalGroupAdd(
            PCWSTR::null(),
            1,
            (&group as *const LOCALGROUP_INFO_1).cast(),
            None,
        )
    })?;
    principals.group =
        Some(crate::user::local_group_sid(ACCOUNT_GROUP)?.context("fresh group missing")?);
    for name in [OFFLINE_USER, ONLINE_USER] {
        let mut wide = wstr(&password.0);
        let mut account = wstr(name);
        let info = USER_INFO_1 {
            usri1_name: PWSTR(account.as_mut_ptr()),
            usri1_password: PWSTR(wide.as_mut_ptr()),
            usri1_priv: USER_PRIV_USER,
            usri1_flags: UF_SCRIPT | UF_DONT_EXPIRE_PASSWD | UF_ACCOUNTDISABLE,
            ..Default::default()
        };
        let status = unsafe {
            NetUserAdd(
                PCWSTR::null(),
                1,
                (&info as *const USER_INFO_1).cast(),
                None,
            )
        };
        for unit in &mut wide {
            unsafe {
                std::ptr::write_volatile(unit, 0);
            }
        }
        std::sync::atomic::compiler_fence(std::sync::atomic::Ordering::SeqCst);
        success(status)?;
        let sid = crate::user::local_user_sid(name)?.context("fresh account missing")?;
        if name == OFFLINE_USER {
            principals.offline = Some(sid.clone());
        } else {
            principals.online = Some(sid.clone());
        }
        let local = crate::sid::LocalPsid::from_string(&sid)?;
        let member = LOCALGROUP_MEMBERS_INFO_0 {
            lgrmi0_sid: local.as_psid(),
        };
        for group in [
            crate::sid::lookup_account_name("S-1-5-32-545")?,
            ACCOUNT_GROUP.into(),
        ] {
            success(unsafe {
                NetLocalGroupAddMembers(
                    PCWSTR::null(),
                    pcwstr(&wstr(&group)),
                    0,
                    (&member as *const LOCALGROUP_MEMBERS_INFO_0).cast(),
                    1,
                )
            })?;
        }
    }
    let sid = match mode {
        AccountMode::Offline => principals.offline.as_deref().unwrap(),
        AccountMode::Online => principals.online.as_deref().unwrap(),
    };
    principals.disabled(mode.name(), sid, false)
}

fn bound_fixture(offline: &str, online: &str, group: &str) -> Result<Fixture> {
    let mut fixture = Fixture::new();
    fixture.preserve = true;
    // Select principal identities BEFORE storage birth. Replacing a real
    // storage attestation with synthetic IDs afterward must remain refused.
    fixture.setup.offline.as_mut().unwrap().sid = offline.into();
    fixture.setup.online.as_mut().unwrap().sid = online.into();
    fixture.setup.group.as_mut().unwrap().sid = group.into();
    fixture.setup.storage = None;
    let plan = crate::account_state::storage::PreparedStorage::prepare_fixture(
        &fixture.setup,
        &fixture.root,
    )?;
    fixture.setup.storage = Some(plan.intent().clone());
    let pins = plan.create()?;
    fixture.setup.storage = Some(pins.attestation().clone());
    Ok(fixture)
}

#[test]
fn account_birth_fixture_preserves_actual_storage_identity_after_binding_accounts() {
    let fixture = bound_fixture(
        "S-1-5-21-111-222-333-9001",
        "S-1-5-21-111-222-333-9002",
        "S-1-5-21-111-222-333-9003",
    )
    .unwrap();
    let database = protected_database(&fixture);
    let expected = fixture.setup.storage.clone();
    drop(database);
    let reopened = protected_database(&fixture);
    assert_eq!(fixture.setup.storage, expected);
    drop(reopened);
    let mut wrong = fixture.setup.clone();
    wrong.storage = Some(crate::account_state::storage::synthetic_attestation(&wrong));
    assert!(
        PrivateDatabase::open_under(&fixture.root, &wrong, AccountMode::Offline, None).is_err()
    );
    // The rejected fabricated identity cannot alter the original admission.
    drop(protected_database(&fixture));
}

// A query/synchronize reference to the original observed child proves exit on
// each error path without keeping the job alive or acquiring cleanup authority.
fn post_birth_failure(principals: &Principals, password: &Password, case: &str) -> Result<()> {
    use bootstrap_plan::lifecycle::resume::logon::delivery::birth::BIRTH_NATIVE_CHECKPOINT;
    use bootstrap_plan::lifecycle::resume::logon::delivery::birth::retirement::RETIREMENT_CHECKPOINT;
    use std::{cell::RefCell, rc::Rc};
    struct ResetHooks;
    impl Drop for ResetHooks {
        fn drop(&mut self) {
            BIRTH_NATIVE_CHECKPOINT.with(|s| {
                s.borrow_mut().take();
            });
            BIRTH_CHECKPOINT.with(|s| {
                s.borrow_mut().take();
            });
            RETIREMENT_CHECKPOINT.with(|s| {
                s.borrow_mut().take();
            });
        }
    }
    let fixture = bound_fixture(
        principals.offline.as_deref().unwrap(),
        principals.online.as_deref().unwrap(),
        principals.group.as_deref().unwrap(),
    )?;
    let db = protected_database(&fixture);
    let ready = ReadyInstallation::fixture(fixture.setup.clone(), &fixture.root)?;
    let active = atomic_fixture_active(&fixture, &db);
    let marker = std::fs::read(fixture.path())?;
    let image = PinnedRunnerImage::prepare(&ready)?;
    let (server, address) =
        ControlServer::prepare(&ready.binding(AccountMode::Offline)?.account_sid)?;
    let plan = bootstrap_plan::CommittedPlan::prepare(&db, &active, &image, &ready, &address)?;
    let job = plan.create_job()?;
    let exit = Rc::new(RefCell::new(None));
    let captured = exit.clone();
    let selected = case.to_owned();
    let _reset = ResetHooks;
    BIRTH_NATIVE_CHECKPOINT.with(|s| {
        *s.borrow_mut() = Some(Box::new(move |phase, observer, sql| {
            if phase == "observed" {
                ensure!(
                    captured.borrow().is_none(),
                    "account child observation repeated"
                );
                *captured.borrow_mut() = Some(observer);
                if selected == "receipt-write-failure" {
                    sql.execute_batch("PRAGMA query_only=ON")?;
                }
            }
            if phase == "acknowledged" && selected == "post-ack-refusal" {
                anyhow::bail!("synthetic post-ack refusal");
            }
            if phase == "acknowledged" && selected == "retirement-intent-write-failure" {
                sql.execute_batch("PRAGMA query_only=ON")?;
            }
            Ok(())
        }));
    });
    if case == "receipt-readback-drift" {
        BIRTH_CHECKPOINT.with(|s| {
            *s.borrow_mut() = Some(Box::new(|phase, sequence, sql| {
                if phase == "committed" && sequence == 1 {
                    sql.execute_batch("DROP TRIGGER atomic_account_birth_no_update")
                        .unwrap();
                }
            }));
        });
    }
    if matches!(
        case,
        "retirement-receipt-write-failure" | "retirement-readback-drift"
    ) {
        let selected = case.to_owned();
        RETIREMENT_CHECKPOINT.with(|s| {
            *s.borrow_mut() = Some(Box::new(move |sequence, sql| {
                if sequence == 0 && selected == "retirement-receipt-write-failure" {
                    sql.execute_batch("PRAGMA query_only=ON").unwrap();
                }
                if sequence == 1 && selected == "retirement-readback-drift" {
                    sql.execute_batch("DROP TRIGGER atomic_job_retirement_no_update")
                        .unwrap();
                }
            }));
        });
    }
    let result = job.fixture_observe_account_birth(server, &password.0, Duration::from_secs(10));
    drop(_reset);
    ensure!(result.is_err(), "post-birth fault was not refused");
    let original = exit
        .borrow_mut()
        .take()
        .context("real account child was not observed before failure")?;
    original.wait(Duration::from_secs(5))?;
    ensure!(
        atomic_job_counts(active.record()?).is_none(),
        "failed birth retained original job"
    );
    ensure!(
        std::fs::read(fixture.path())? == marker,
        "failed birth changed pending marker"
    );
    let sql = db.connection()?;
    sql.execute_batch("PRAGMA query_only=OFF")?;
    let rows: usize = sql.query_row(
        "SELECT count(*) FROM candidate_atomic_account_birth",
        [],
        |r| r.get(0),
    )?;
    ensure!(
        rows == if case == "receipt-write-failure" {
            1
        } else {
            2
        },
        "failed birth changed durable prefix"
    );
    let retired: usize = sql.query_row(
        "SELECT count(*) FROM candidate_atomic_job_retirement",
        [],
        |r| r.get(0),
    )?;
    ensure!(
        retired
            == match case {
                "retirement-receipt-write-failure" => 1,
                "retirement-readback-drift" => 2,
                _ => 0,
            },
        "failed retirement changed durable prefix"
    );
    Ok(())
}

#[test]
#[ignore = "bounded hosted paired broker; original controller and stdin-only synthetic secret"]
fn paired_account_held_broker() {
    require_host().unwrap();
    use std::io::Read;
    let mut password = Password(String::with_capacity(128));
    std::io::stdin()
        .take(128)
        .read_to_string(&mut password.0)
        .unwrap();
    assert!(password.0.len() == 68 && password.0.is_ascii());
    super::super::paired::hosted_account_crash_broker(&password.0);
}

#[test]
#[ignore = "bounded hosted profile broker; original controller and stdin-only synthetic secret"]
fn paired_profile_held_broker() {
    require_host().unwrap();
    use std::io::Read;
    let mut password = Password(String::with_capacity(128));
    std::io::stdin()
        .take(128)
        .read_to_string(&mut password.0)
        .unwrap();
    assert!(password.0.len() == 68 && password.0.is_ascii());
    super::super::paired::hosted_profile_crash_broker(&password.0);
}

#[test]
#[ignore = "three initialized writer crash diagnostics; explicitly selected disposable GitHub-hosted VM only"]
fn account_initialized_writer_crashes_disposable() {
    use std::io::{Seek, SeekFrom, Write};
    const TEST: &str = "account_initialized_writer_crashes_disposable";
    const CASES: [&str; 3] = [
        "initialized-writer-family-live-use",
        "initialized-writer-family-captured-hot-receipt",
        "initialized-writer-family-removed-hot-receipt",
    ];
    // This includes the unchanged account-birth host admission. The extra
    // sentinel prevents selecting this effectful diagnostic accidentally.
    station::require_station_host().unwrap();
    for (key, value) in [
        (
            "ALYSIS_RUN_DISPOSABLE_ATOMIC_PROFILE",
            "github-hosted-atomic-profile-only",
        ),
        (
            "ALYSIS_RUN_DISPOSABLE_INITIALIZED_CRASH_FOCUS",
            "github-hosted-initialized-crash-focus-only",
        ),
    ] {
        assert!(
            std::env::var(key).ok().as_deref() == Some(value),
            "focused initialized writer admission refused"
        );
    }
    let root = std::path::PathBuf::from(std::env::var_os("RUNNER_TEMP").unwrap());
    let mut report =
        std::fs::File::create_new(root.join("alysis-initialized-crash-focus.json")).unwrap();
    let mut publish = |stage: &str,
                       completed: &[&str],
                       failure_kind: Option<&str>,
                       hresult: Option<i32>,
                       disabled: Option<bool>,
                       passed: bool|
     -> Result<()> {
        // Only fixed labels and native error numbers enter this report; no
        // error/panic text, principal identities, or synthetic secret frames.
        let proof = serde_json::json!({
            "schema":1,"test":TEST,
            "run_id":std::env::var("GITHUB_RUN_ID")?,
            "run_attempt":std::env::var("GITHUB_RUN_ATTEMPT")?,
            "selected_cases":CASES,"completed_cases":completed,
            "stage":stage,"passed":passed,"failure_kind":failure_kind,
            "hresult":hresult,"accounts_disabled_afterward":disabled,
            "production_ready":false,"full_matrix_qualified":false
        });
        let bytes = serde_json::to_vec(&proof)?;
        ensure!(
            bytes.len() <= 8192,
            "focused diagnostic report exceeds bound"
        );
        report.seek(SeekFrom::Start(0))?;
        report.write_all(&bytes)?;
        report.set_len(bytes.len() as u64)?;
        report.sync_all()?;
        Ok(())
    };
    let mut stage = "fresh_principals";
    let mut completed = Vec::new();
    publish(stage, &completed, None, None, None, false).unwrap();
    let mut principals = Principals {
        offline: None,
        online: None,
        group: None,
    };
    let password = Password(format!(
        "Aa1!{}{}",
        random_id().unwrap(),
        random_id().unwrap()
    ));
    // Keep the same provisioning/secret guards intact and explicitly disable
    // owned accounts even when a controller assertion unwinds. Each unchanged
    // crash controller retains its existing process/job/error cleanup ordering.
    let outcome = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| -> Result<()> {
        create(&mut principals, &password)?;
        for point in CASES {
            stage = point;
            publish(stage, &completed, None, None, None, false)?;
            let fixture = bound_fixture(
                principals.offline.as_deref().unwrap(),
                principals.online.as_deref().unwrap(),
                principals.group.as_deref().unwrap(),
            )?;
            // Reuse the full matrix's ReadyInstallation/image admission and
            // original resource policy path; no preliminary matrix or retry.
            super::super::paired::hosted_profile_crash(fixture, &password.0, point)?;
            completed.push(point);
            publish(stage, &completed, None, None, None, false)?;
        }
        Ok(())
    }));
    let disabled = principals.disable_all();
    let (result_ok, failure_kind, error) = match &outcome {
        Ok(Ok(())) if disabled.is_ok() => (true, None, None),
        Ok(Ok(())) => (true, Some("account_disable_error"), disabled.as_ref().err()),
        Ok(Err(error)) => (false, Some("test_error"), Some(error)),
        Err(_) => (false, Some("panic"), None),
    };
    let hresult = error.and_then(|error| {
        error
            .chain()
            .find_map(|cause| cause.downcast_ref::<windows::core::Error>())
            .map(|error| error.code().0)
    });
    let passed = result_ok && disabled.is_ok() && completed.as_slice() == CASES.as_slice();
    if result_ok {
        stage = if passed {
            "complete"
        } else {
            "disable_accounts"
        };
    }
    publish(
        stage,
        &completed,
        failure_kind,
        hresult,
        Some(disabled.is_ok()),
        passed,
    )
    .unwrap();
    assert!(
        passed,
        "focused initialized writer diagnostics failed at {stage}; native HRESULT={hresult:?}; sanitized report preserved"
    );
}

#[test]
#[ignore = "fixed AppContainer station boundary; explicitly admitted disposable VM only"]
fn account_station_lowbox_disposable() {
    account_lowbox_disposable_inner(false);
}
#[test]
#[ignore = "fixed V3 contained Offline completion; separate original disposable admission"]
fn account_v3_contained_offline_disposable() {
    let _admission = crate::account_rpc_v3_lifecycle_fixture::enter_contained_offline().unwrap();
    account_lowbox_disposable_inner(true);
}
#[test]
#[ignore = "fixed V3 contained Online completion; separate original disposable admission"]
fn account_v3_contained_online_disposable() {
    let _admission = crate::account_rpc_v3_lifecycle_fixture::enter_contained_online().unwrap();
    account_lowbox_disposable_inner(true);
}
fn account_lowbox_disposable_inner(v3: bool) {
    use crate::account_control::station_lowbox_fixture::{CHECKPOINT, RECEIPT, Receipt};
    use std::io::{Seek, SeekFrom, Write};
    station::require_station_host().unwrap();
    let mode = if v3 {
        crate::account_rpc_v3_lifecycle_fixture::contained_mode()
            .unwrap()
            .expect("original contained mode absent")
    } else {
        crate::account_control::station_lowbox_fixture::fixture_mode()
    };
    let name = if v3 && mode == AccountMode::Online {
        "account_v3_contained_online_disposable"
    } else if v3 {
        "account_v3_contained_offline_disposable"
    } else {
        "account_station_lowbox_disposable"
    };
    crate::account_control::station_lowbox_fixture::require_source_selection().unwrap();
    for (key, value) in [
        (
            "ALYSIS_RUN_DISPOSABLE_STATION_LOWBOX",
            "github-hosted-station-lowbox-only",
        ),
        (
            "ALYSIS_RUN_DISPOSABLE_ATOMIC_PROFILE",
            "github-hosted-atomic-profile-only",
        ),
        (
            "ALYSIS_RUN_DISPOSABLE_UNUSED_PROFILE",
            "github-hosted-unused-profile-only",
        ),
    ] {
        if v3 && key == "ALYSIS_RUN_DISPOSABLE_STATION_LOWBOX" {
            continue;
        }
        assert!(
            std::env::var(key).ok().as_deref() == Some(value),
            "station lowbox admission refused"
        );
    }
    let root = std::path::PathBuf::from(std::env::var_os("RUNNER_TEMP").unwrap());
    // Keep earlier invocation evidence intact. Update only this new report's
    // original handle as the admitted fixture progresses.
    let mut report = std::fs::File::create_new(root.join(if v3 && mode == AccountMode::Online {
        "alysis-v3-contained-online.json"
    } else if v3 {
        "alysis-v3-contained-offline.json"
    } else {
        "alysis-station-lowbox.json"
    }))
    .unwrap();
    let mut publish = |stage: &str,
                       receipt: Option<&Receipt>,
                       lifecycle: Option<&serde_json::Value>,
                       failure_kind: Option<&str>,
                       hresult: Option<i32>,
                       disabled: Option<bool>,
                       passed: bool|
     -> Result<()> {
        let value = serde_json::json!({
            "schema":1, "test":name,"v3_bounded_completion":v3,
            "shadow_root_comparison":!v3,
            "shadow_root_born":if v3 { lifecycle.is_some() } else { crate::account_control::station_lowbox_fixture::SHADOW_BORN.with(|s|s.get()) },
            "run_id":std::env::var("GITHUB_RUN_ID")?,
            "run_attempt":std::env::var("GITHUB_RUN_ATTEMPT")?,
            "stage":stage, "passed":passed, "failure_kind":failure_kind,
            "hresult":hresult, "accounts_disabled_afterward":disabled,
            "proof":receipt, "lifecycle":lifecycle,
            "diagnostic_checkpoint":CHECKPOINT.with(|slot| slot.borrow().clone()),
            "source_account_logon":crate::account_control::station_lowbox_fixture::LOGON_OBSERVATION.with(|slot| slot.borrow().clone()),
            "production_ready":false, "full_matrix_qualified":false,
            "fixture_storage":true, "network_policy": if mode == AccountMode::Online { "on" } else { "off" },
            "https_comparison": crate::account_control::station_lowbox_fixture::HTTPS_COMPARISON,
            // This fixture binds real accounts to test storage; it does not
            // install the separate production WFP account firewall transaction.
            "network_enforcement_under_test":"appcontainer-capabilities",
            "account_wfp_filters_installed":false,
            "broker_stage": crate::account_control::station_lowbox_fixture::BROKER_STAGE.with(|s|s.get()),
            "station_policy":"private-account-system-package-owner-rights-read-control"
        });
        let bytes = serde_json::to_vec(&value)?;
        ensure!(bytes.len() <= 131072, "station lowbox report exceeds bound");
        report.seek(SeekFrom::Start(0))?;
        report.write_all(&bytes)?;
        report.set_len(bytes.len() as u64)?;
        report.sync_all()?;
        Ok(())
    };
    let mut stage = "fresh_principals";
    publish(stage, None, None, None, None, None, false).unwrap();
    let mut principals = Principals {
        offline: None,
        online: None,
        group: None,
    };
    let password = Password(format!(
        "Aa1!{}{}",
        random_id().unwrap(),
        random_id().unwrap()
    ));
    RECEIPT.with(|slot| {
        assert!(
            slot.borrow().is_none(),
            "station lowbox fixture already attempted"
        )
    });
    let outcome = std::panic::catch_unwind(std::panic::AssertUnwindSafe(
        || -> Result<serde_json::Value> {
            create_mode(&mut principals, &password, mode)?;
            stage = "paired_profile_lifecycle";
            publish(stage, None, None, None, None, None, false)?;
            let fixture = bound_fixture(
                principals.offline.as_deref().unwrap(),
                principals.online.as_deref().unwrap(),
                principals.group.as_deref().unwrap(),
            )?;
            if v3 && mode == AccountMode::Online {
                super::super::paired::hosted_v3_contained_online(fixture, &password.0)
            } else if v3 {
                super::super::paired::hosted_v3_contained_offline(fixture, &password.0)
            } else {
                super::super::paired::hosted_station_lowbox(fixture, &password.0)
            }
        },
    ));
    // Original process/job guards unwind before account disable. Preserve the
    // native failure receipt without treating error cleanup as hot completion.
    let receipt = RECEIPT.with(|slot| slot.borrow_mut().take());
    let disabled = principals.disable_all();
    let (lifecycle, failure_kind, error) = match &outcome {
        Ok(Ok(value)) if disabled.is_ok() => (Some(value), None, None),
        Ok(Ok(value)) => (
            Some(value),
            Some("account_disable_error"),
            disabled.as_ref().err(),
        ),
        Ok(Err(error)) => (None, Some("test_error"), Some(error)),
        Err(_) => (None, Some("panic"), None),
    };
    let hresult = receipt
        .as_ref()
        .and_then(|value| value.hresult)
        .or_else(|| {
            error.and_then(|error| {
                error
                    .chain()
                    .find_map(|cause| cause.downcast_ref::<windows::core::Error>())
                    .map(|error| error.code().0)
            })
        });
    let passed = lifecycle.is_some()
        && disabled.is_ok()
        && receipt.as_ref().is_some_and(|value| value.passed);
    if passed {
        stage = "complete";
    } else if disabled.is_err() {
        stage = "disable_accounts";
    } else if let Some(receipt) = &receipt {
        if !receipt.passed {
            stage = &receipt.stage;
        }
    } else if lifecycle.is_some() {
        stage = "missing_proof";
    } else if stage == "paired_profile_lifecycle" {
        stage = crate::account_control::station_lowbox_fixture::BROKER_STAGE.with(|s| s.get());
    }
    publish(
        stage,
        receipt.as_ref(),
        lifecycle,
        failure_kind,
        hresult,
        Some(disabled.is_ok()),
        passed,
    )
    .unwrap();
    assert!(
        passed,
        "station lowbox failed at {stage}; native HRESULT={hresult:?}; sanitized report preserved"
    );
}

#[test]
#[ignore = "private station in original dedicated-account child; explicitly admitted disposable VM only"]
fn account_private_station_disposable() {
    use crate::account_control::private_station_fixture::{RECEIPT, Receipt};
    use std::io::{Seek, SeekFrom, Write};
    station::require_station_host().unwrap();
    assert!(
        std::env::var("ALYSIS_RUN_DISPOSABLE_PRIVATE_ACCOUNT_STATION")
            .ok()
            .as_deref()
            == Some("github-hosted-private-account-station-only"),
        "private account station admission refused"
    );
    let root = std::path::PathBuf::from(std::env::var_os("RUNNER_TEMP").unwrap());
    // Never truncate evidence from an earlier invocation.
    let mut report =
        std::fs::File::create_new(root.join("alysis-private-account-station.json")).unwrap();
    let mut publish = |stage: &str,
                       receipt: Option<&Receipt>,
                       failure_kind: Option<&str>,
                       hresult: Option<i32>,
                       disabled: Option<bool>,
                       passed: bool|
     -> Result<()> {
        let proof = serde_json::json!({
            "schema":1, "test":"account_private_station_disposable",
            "run_id":std::env::var("GITHUB_RUN_ID")?,
            "run_attempt":std::env::var("GITHUB_RUN_ATTEMPT")?,
            "stage":stage, "passed":passed, "failure_kind":failure_kind, "hresult":hresult,
            "accounts_disabled_afterward":disabled, "receipt":receipt,
            "production_ready":false, "full_matrix_qualified":false,
            "untrusted_containment_qualified":false, "shared_station_acl_written":false,
            "profile_loaded":false, "station_policy":"feasibility-account-and-system-full-access",
            "native_api":"CreateProcessWithLogonW", "logon_flags":0
        });
        let bytes = serde_json::to_vec(&proof)?;
        ensure!(
            bytes.len() <= 49152,
            "private account station report exceeds bound"
        );
        report.seek(SeekFrom::Start(0))?;
        report.write_all(&bytes)?;
        report.set_len(bytes.len() as u64)?;
        report.sync_all()?;
        Ok(())
    };
    let mut stage = "fresh_principals";
    publish(stage, None, None, None, None, false).unwrap();
    let mut principals = Principals {
        offline: None,
        online: None,
        group: None,
    };
    let password = Password(format!(
        "Aa1!{}{}",
        random_id().unwrap(),
        random_id().unwrap()
    ));
    RECEIPT.with(|slot| {
        assert!(
            slot.borrow().is_none(),
            "private station fixture already attempted"
        )
    });
    let outcome = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| -> Result<()> {
        // Reuse the exclusive SAM ownership/disable guards and authenticated
        // credential delivery; no password is added to argv or environment.
        create(&mut principals, &password)?;
        stage = "account_self_check";
        publish(stage, None, None, None, None, false)?;
        self_check::run(&principals, &password, "private-station-success")
    }));
    // Also preserve native refusal evidence when the live job was drained by
    // error/unwind rather than by a successful durable retirement receipt.
    let receipt = RECEIPT.with(|slot| slot.borrow_mut().take());
    let disabled = principals.disable_all();
    let (result_ok, failure_kind, error) = match &outcome {
        Ok(Ok(())) if disabled.is_ok() => (true, None, None),
        Ok(Ok(())) => (true, Some("account_disable_error"), disabled.as_ref().err()),
        Ok(Err(error)) => (false, Some("test_error"), Some(error)),
        Err(_) => (false, Some("panic"), None),
    };
    let hresult = receipt
        .as_ref()
        .and_then(|value| value.hresult)
        .or_else(|| {
            error.and_then(|error| {
                error
                    .chain()
                    .find_map(|cause| cause.downcast_ref::<windows::core::Error>())
                    .map(|error| error.code().0)
            })
        });
    let passed =
        result_ok && disabled.is_ok() && receipt.as_ref().is_some_and(|value| value.passed);
    if result_ok {
        stage = if passed {
            "complete"
        } else if disabled.is_err() {
            "disable_accounts"
        } else {
            "missing_receipt"
        };
    }
    publish(
        stage,
        receipt.as_ref(),
        failure_kind,
        hresult,
        Some(disabled.is_ok()),
        passed,
    )
    .unwrap();
    assert!(
        passed,
        "private account station failed at {stage}; native HRESULT={hresult:?}; sanitized report preserved"
    );
}

#[test]
#[ignore = "owned fixed candidate accounts; disposable explicitly selected GitHub-hosted VM only"]
fn account_birth_disposable_original_job_and_real_group() {
    require_host().unwrap();
    let root = std::path::PathBuf::from(std::env::var_os("RUNNER_TEMP").unwrap());
    let report = root.join("alysis-account-birth.json");
    assert!(!report.exists());
    let mut stage = "fresh_principals";
    let mut principals = Principals {
        offline: None,
        online: None,
        group: None,
    };
    let password = Password(format!(
        "Aa1!{}{}",
        random_id().unwrap(),
        random_id().unwrap()
    ));
    let mut failure_cases = Vec::new();
    let mut crash_cases = Vec::new();
    let mut retirement_cases = Vec::new();
    let mut namespace_cases = Vec::new();
    let mut namespace_crash_cases = Vec::new();
    let mut station_cases = Vec::new();
    let mut station_crash_cases = Vec::new();
    let mut desktop_cases = Vec::new();
    let mut desktop_crash_cases = Vec::new();
    let mut host_profile_cases = Vec::new();
    let mut host_profile_crash_cases = Vec::new();
    let mut host_profile_used_cases = Vec::new();
    let mut host_profile_registry_cases = Vec::new();
    let mut host_profile_used_crash_cases = Vec::new();
    let mut initializer_cases = Vec::new();
    let mut initializer_failures = Vec::new();
    let mut initializer_crash_cases = Vec::new();
    let mut self_check_cases = Vec::new();
    let mut account_profile_cases = Vec::new();
    let mut paired_account_cases = Vec::new();
    let mut paired_self_check_cases = Vec::new();
    let mut paired_profile_cases = Vec::new();
    let mut paired_namespace_cases = Vec::new();
    let mut paired_host_profile_cases = Vec::new();
    let mut paired_station_cases = Vec::new();
    let mut paired_desktop_cases = Vec::new();
    let mut paired_initializer_cases = Vec::new();
    let mut paired_initializer_profile_cases = Vec::new();
    let mut paired_desktop_profile_cases = Vec::new();
    let mut paired_station_profile_cases = Vec::new();
    let mut paired_profile_crash_cases = Vec::new();
    let mut paired_writer_crash_cases = Vec::new();
    let mut paired_initialized_writer_crash_cases = Vec::new();
    let mut paired_resume_crash_cases = Vec::new();
    let mut paired_account_crash_cases = Vec::new();
    let mut account_profile_crash_cases = Vec::new();
    let mut self_check_failure = None;
    let mut self_check_crash_cases = Vec::new();
    let result = (|| -> Result<(u32, u64)> {
        create(&mut principals, &password)?;
        for case in [
            "success",
            "consumer-born-receipt-failure",
            "consumer-acknowledged-receipt-failure",
            "consumer-retire-intent-failure",
            "consumer-retire-receipt-failure",
        ] {
            stage = "paired_account_children";
            let fixture = bound_fixture(
                principals.offline.as_deref().unwrap(),
                principals.online.as_deref().unwrap(),
                principals.group.as_deref().unwrap(),
            )?;
            super::super::paired::hosted_account_pair(fixture, &password.0, case)?;
            paired_account_cases.push(case);
        }
        for point in [
            "paired-owner-born",
            "paired-owner-acknowledged",
            "paired-consumer-born",
            "paired-consumer-acknowledged",
            "paired-both-acknowledged",
        ] {
            stage = "paired_account_broker_death";
            let fixture = bound_fixture(
                principals.offline.as_deref().unwrap(),
                principals.online.as_deref().unwrap(),
                principals.group.as_deref().unwrap(),
            )?;
            super::super::paired::hosted_account_crash(fixture, &password.0, point)?;
            paired_account_crash_cases.push(point);
        }
        for case in ["success", "before-intent", "resumed", "exited", "retiring"] {
            stage = "paired_consumer_self_check";
            let fixture = bound_fixture(
                principals.offline.as_deref().unwrap(),
                principals.online.as_deref().unwrap(),
                principals.group.as_deref().unwrap(),
            )?;
            super::super::paired::hosted_consumer_self_check(fixture, &password.0, case)?;
            paired_self_check_cases.push(case);
        }
        for point in [
            "paired-self-check-intent",
            "paired-self-check-resumed-hot-receipt",
            "paired-self-check-exited",
        ] {
            stage = point;
            let fixture = bound_fixture(
                principals.offline.as_deref().unwrap(),
                principals.online.as_deref().unwrap(),
                principals.group.as_deref().unwrap(),
            )?;
            super::super::paired::hosted_account_crash(fixture, &password.0, point)?;
            paired_resume_crash_cases.push(point);
        }
        for case in [
            "success",
            "before-resume-intent",
            "resumed",
            "before-create",
            "created",
            "before-use",
            "using",
            "before-capture",
            "captured",
            "before-remove",
            "removed",
            "exited",
            "retiring",
            "writer-success",
            "writer-unsupported",
            "writer-before-capture",
            "writer-long-success",
            "writer-long-interrupted",
            "writer-long-source-exit",
            "writer-descendants-success",
            "writer-descendants-before-capture",
        ] {
            stage = if case.starts_with("writer-") {
                "paired_profile_fixed_writer"
            } else {
                "paired_profile_owner_lifetime"
            };
            let fixture = bound_fixture(
                principals.offline.as_deref().unwrap(),
                principals.online.as_deref().unwrap(),
                principals.group.as_deref().unwrap(),
            )?;
            paired_profile_cases.push(super::super::paired::hosted_profile_pair(
                fixture,
                &password.0,
                case,
            )?);
        }
        for case in ["success", "before-capture", "closed"] {
            stage = "paired_namespace_fixed_writer";
            let fixture = bound_fixture(
                principals.offline.as_deref().unwrap(),
                principals.online.as_deref().unwrap(),
                principals.group.as_deref().unwrap(),
            )?;
            paired_namespace_cases.push(super::super::paired::hosted_namespace_pair(
                fixture,
                &password.0,
                case,
            )?);
        }
        for case in [
            "unused-success",
            "used-success",
            "used-capture-receipt-failure",
            "used-remove-intent-failure",
            "used-remove-receipt-failure",
        ] {
            stage = "paired_host_profile_fixed_writer";
            let fixture = bound_fixture(
                principals.offline.as_deref().unwrap(),
                principals.online.as_deref().unwrap(),
                principals.group.as_deref().unwrap(),
            )?;
            paired_host_profile_cases.push(super::super::paired::hosted_host_profile_pair(
                fixture,
                &password.0,
                case,
            )?);
        }
        for case in [
            "success",
            "prepared",
            "applied",
            "before-restore",
            "restoring",
            "restored",
            "drift-ready",
            "drift-before-restore",
        ] {
            stage = "paired_station";
            let fixture = bound_fixture(
                principals.offline.as_deref().unwrap(),
                principals.online.as_deref().unwrap(),
                principals.group.as_deref().unwrap(),
            )?;
            paired_station_cases.push(super::super::paired::hosted_station_pair(fixture, case)?);
        }
        for case in ["used-success", "used-capture-receipt-failure"] {
            stage = "paired_station_profile";
            let fixture = bound_fixture(
                principals.offline.as_deref().unwrap(),
                principals.online.as_deref().unwrap(),
                principals.group.as_deref().unwrap(),
            )?;
            paired_station_profile_cases.push(super::super::paired::hosted_station_profile_pair(
                fixture,
                &password.0,
                case,
            )?);
        }
        for case in [
            "success",
            "prepared",
            "born",
            "ready",
            "before-close",
            "closing",
            "closed",
            "held-reference",
            "collision",
        ] {
            stage = "paired_desktop";
            let fixture = bound_fixture(
                principals.offline.as_deref().unwrap(),
                principals.online.as_deref().unwrap(),
                principals.group.as_deref().unwrap(),
            )?;
            paired_desktop_cases.push(super::super::paired::hosted_desktop_pair(fixture, case)?);
        }
        for case in ["used-success", "used-capture-receipt-failure"] {
            stage = "paired_desktop_profile";
            let fixture = bound_fixture(
                principals.offline.as_deref().unwrap(),
                principals.online.as_deref().unwrap(),
                principals.group.as_deref().unwrap(),
            )?;
            paired_desktop_profile_cases.push(super::super::paired::hosted_desktop_profile_pair(
                fixture,
                &password.0,
                case,
            )?);
        }
        for case in super::super::paired::hosted_initializer_cases() {
            stage = "paired_initializer";
            let fixture = bound_fixture(
                principals.offline.as_deref().unwrap(),
                principals.online.as_deref().unwrap(),
                principals.group.as_deref().unwrap(),
            )?;
            paired_initializer_cases.push(super::super::paired::hosted_initializer_pair(
                fixture, case,
            )?);
        }
        for case in ["used-success", "used-capture-receipt-failure"] {
            stage = "paired_initializer_profile";
            let fixture = bound_fixture(
                principals.offline.as_deref().unwrap(),
                principals.online.as_deref().unwrap(),
                principals.group.as_deref().unwrap(),
            )?;
            paired_initializer_profile_cases.push(
                super::super::paired::hosted_initializer_profile_pair(fixture, &password.0, case)?,
            );
        }
        for point in [
            "paired-profile-created-hot-receipt",
            "paired-profile-using",
            "paired-profile-captured-hot-receipt",
            "paired-profile-removed-hot-receipt",
        ] {
            stage = point;
            let fixture = bound_fixture(
                principals.offline.as_deref().unwrap(),
                principals.online.as_deref().unwrap(),
                principals.group.as_deref().unwrap(),
            )?;
            super::super::paired::hosted_profile_crash(fixture, &password.0, point)?;
            paired_profile_crash_cases.push(point);
        }
        for point in [
            "writer-family-live-use",
            "writer-family-captured-hot-receipt",
            "writer-family-removed-hot-receipt",
        ] {
            stage = point;
            let fixture = bound_fixture(
                principals.offline.as_deref().unwrap(),
                principals.online.as_deref().unwrap(),
                principals.group.as_deref().unwrap(),
            )?;
            super::super::paired::hosted_profile_crash(fixture, &password.0, point)?;
            paired_writer_crash_cases.push(point);
        }
        for point in [
            "initialized-writer-family-live-use",
            "initialized-writer-family-captured-hot-receipt",
            "initialized-writer-family-removed-hot-receipt",
        ] {
            stage = point;
            let fixture = bound_fixture(
                principals.offline.as_deref().unwrap(),
                principals.online.as_deref().unwrap(),
                principals.group.as_deref().unwrap(),
            )?;
            super::super::paired::hosted_profile_crash(fixture, &password.0, point)?;
            paired_initialized_writer_crash_cases.push(point);
        }
        stage = "fixture_storage";
        let fixture = bound_fixture(
            principals.offline.as_deref().unwrap(),
            principals.online.as_deref().unwrap(),
            principals.group.as_deref().unwrap(),
        )?;
        let db = protected_database(&fixture);
        let ready = ReadyInstallation::fixture(fixture.setup.clone(), &fixture.root)?;
        let active = atomic_fixture_active(&fixture, &db);
        let marker = std::fs::read(fixture.path())?;
        stage = "production_image_admission";
        let image = PinnedRunnerImage::prepare(&ready)?;
        let (server, address) =
            ControlServer::prepare(&ready.binding(AccountMode::Offline)?.account_sid)?;
        let plan = bootstrap_plan::CommittedPlan::prepare(&db, &active, &image, &ready, &address)?;
        let job = plan.create_job()?;
        stage = "recorded_account_birth_and_job_close";
        let child =
            job.fixture_observe_account_birth(server, &password.0, Duration::from_secs(10))?;
        ensure!(
            child.image_id == image.file_identity()?,
            "recorded account image differs"
        );
        ensure!(
            atomic_job_counts(active.record()?).is_none(),
            "original job remains open"
        );
        ensure!(
            std::fs::read(fixture.path())? == marker,
            "pending marker changed"
        );
        let rows: usize = db.connection()?.query_row(
            "SELECT count(*) FROM candidate_atomic_account_birth",
            [],
            |r| r.get(0),
        )?;
        ensure!(rows == 2, "account birth intent/receipt incomplete");
        let retired: usize = db.connection()?.query_row(
            "SELECT count(*) FROM candidate_atomic_job_retirement",
            [],
            |r| r.get(0),
        )?;
        ensure!(retired == 2, "job retirement intent/receipt incomplete");
        for case in [
            "self-check-success",
            "self-check-handoff",
            "self-check-intent-failure",
            "self-check-resumed-receipt-failure",
            "self-check-exit-receipt-failure",
            "self-check-intent-readback-drift",
        ] {
            stage = case;
            if let Err(error) = self_check::run(&principals, &password, case) {
                self_check_failure =
                    Some(format!("{error:#}").chars().take(2048).collect::<String>());
                return Err(error);
            }
            self_check_cases.push(case);
        }
        for case in [
            "self-check-intent",
            "self-check-resumed-hot-receipt",
            "self-check-exited",
        ] {
            stage = case;
            crash::run(&principals, &password, case)?;
            self_check_crash_cases.push(case);
        }
        for case in [
            "account-profile-success",
            "account-profile-intent-failure",
            "account-profile-created-receipt-failure",
            "account-profile-remove-intent-failure",
            "account-profile-removed-receipt-failure",
            "account-profile-intent-readback-drift",
        ] {
            stage = case;
            if let Err(error) = self_check::run(&principals, &password, case) {
                self_check_failure =
                    Some(format!("{error:#}").chars().take(2048).collect::<String>());
                return Err(error);
            }
            account_profile_cases.push(case);
        }
        for case in [
            "account-profile-intent",
            "account-profile-created-hot-receipt",
            "account-profile-removed-hot-receipt",
        ] {
            stage = case;
            crash::run(&principals, &password, case)?;
            account_profile_crash_cases.push(case);
        }
        for case in [
            "receipt-write-failure",
            "receipt-readback-drift",
            "post-ack-refusal",
        ] {
            stage = case;
            post_birth_failure(&principals, &password, case)?;
            failure_cases.push(case);
        }
        for case in [
            "retirement-intent-write-failure",
            "retirement-receipt-write-failure",
            "retirement-readback-drift",
        ] {
            stage = case;
            post_birth_failure(&principals, &password, case)?;
            retirement_cases.push(case);
        }
        for case in ["observed", "acknowledged", "hot-receipt"] {
            stage = case;
            crash::run(&principals, &password, case)?;
            crash_cases.push(case);
        }
        for case in [
            "namespace-success",
            "namespace-delayed-root-release",
            "namespace-intent-write-failure",
            "namespace-birth-receipt-write-failure",
            "namespace-removal-intent-write-failure",
            "namespace-removal-receipt-write-failure",
            "namespace-held-root",
            "namespace-account-birth-refusal",
            "namespace-job-retirement-write-failure",
        ] {
            stage = case;
            namespace::run(&principals, &password, case)?;
            namespace_cases.push(case);
        }
        for case in [
            "namespace-observed",
            "namespace-acknowledged",
            "namespace-hot-receipt",
        ] {
            stage = case;
            crash::run(&principals, &password, case)?;
            namespace_crash_cases.push(case);
        }
        for case in [
            "station-success",
            "station-intent-write-failure",
            "station-applied-receipt-write-failure",
            "station-restoration-intent-write-failure",
            "station-restored-receipt-write-failure",
        ] {
            stage = case;
            station::run(&principals, &password, case)?;
            station_cases.push(case);
        }
        for case in [
            "station-observed",
            "station-acknowledged",
            "station-hot-receipt",
        ] {
            stage = case;
            crash::run(&principals, &password, case)?;
            station_crash_cases.push(case);
        }
        for case in [
            "desktop-success",
            "desktop-intent-write-failure",
            "desktop-birth-receipt-write-failure",
            "desktop-close-intent-write-failure",
            "desktop-close-receipt-write-failure",
            "desktop-held-reference",
        ] {
            stage = case;
            desktop::run(&principals, &password, case)?;
            desktop_cases.push(case);
        }
        for case in [
            "desktop-observed",
            "desktop-acknowledged",
            "desktop-hot-receipt",
        ] {
            stage = case;
            crash::run(&principals, &password, case)?;
            desktop_crash_cases.push(case);
        }
        for case in [
            "host-profile-success",
            "host-profile-intent-failure",
            "host-profile-birth-receipt-failure",
            "host-profile-removal-intent-failure",
            "host-profile-removal-receipt-failure",
            "host-profile-post-close-refusal",
        ] {
            stage = case;
            host_profile::run(&principals, &password, case)?;
            host_profile_cases.push(case);
        }
        for case in [
            "host-profile-observed",
            "host-profile-acknowledged",
            "host-profile-hot-receipt",
        ] {
            stage = case;
            crash::run(&principals, &password, case)?;
            host_profile_crash_cases.push(case);
        }
        for case in [
            "host-profile-used-success",
            "host-profile-used-capture-refusal",
            "host-profile-used-capture-receipt-failure",
            "host-profile-used-removal-receipt-failure",
        ] {
            stage = case;
            host_profile::run(&principals, &password, case)?;
            host_profile_used_cases.push(case);
        }
        for case in [
            "host-profile-used-registry-success",
            "host-profile-used-registry-refusal",
        ] {
            stage = case;
            host_profile::run(&principals, &password, case)?;
            host_profile_registry_cases.push(case);
        }
        for case in [
            "host-profile-used-captured",
            "host-profile-used-closed",
            "host-profile-used-hot-receipt",
        ] {
            stage = case;
            crash::run(&principals, &password, case)?;
            host_profile_used_crash_cases.push(case);
        }
        let mut first_initializer_error = None;
        for case in [
            "initializer-success",
            "initializer-account-rpc-self-check",
            "initializer-account-rpc-intent-failure",
            "initializer-account-rpc-resumed-receipt-failure",
            "initializer-account-rpc-exit-receipt-failure",
            "initializer-missing-profile-environment",
            "initializer-birth-receipt-failure",
            "initializer-stopped-receipt-failure",
            "initializer-rpc-intent-failure",
            "initializer-rpc-receipt-failure",
            "initializer-release-intent-failure",
            "initializer-release-receipt-failure",
        ] {
            stage = case;
            match initializer::run(&principals, &password, case) {
                Ok(()) => initializer_cases.push(case),
                Err(error) => {
                    initializer_failures.push(serde_json::json!({"case":case,
                        "error":format!("{error:#}").chars().take(2048).collect::<String>(),
                        "namespace":initializer::failure_namespace()}));
                    if first_initializer_error.is_none() {
                        first_initializer_error = Some((case, error));
                    }
                }
            }
        }
        if let Some((case, error)) = first_initializer_error {
            stage = case;
            return Err(error);
        }
        for case in [
            "initializer-observed",
            "initializer-acknowledged",
            "initializer-hot-receipt",
        ] {
            stage = case;
            crash::run(&principals, &password, case)?;
            initializer_crash_cases.push(case);
        }
        Ok((child.pid, child.created))
    })();
    let disabled = principals.disable_all();
    let hresult = result.as_ref().err().and_then(|e| {
        e.chain()
            .find_map(|c| c.downcast_ref::<windows::core::Error>())
            .map(|e| e.code().0)
    });
    let proof = serde_json::json!({"schema":1,"test":"account_birth_disposable_original_job_and_real_group","run_id":std::env::var("GITHUB_RUN_ID").unwrap(),"run_attempt":std::env::var("GITHUB_RUN_ATTEMPT").unwrap(),"passed":result.is_ok()&&disabled.is_ok(),"stage":if result.is_ok(){"complete"}else{stage},"hresult":hresult,"accounts_disabled_afterward":disabled.is_ok(),"observation_children_never_resumed":true,"untrusted_commands_executed":false,"installed_credentials_read":false,"fixture_storage":true,"production_image_admission":result.is_ok(),"production_receiver_image_admission":result.is_ok(),"dedicated_installation_group_verified":result.is_ok(),"whole_original_job_closed":result.is_ok(),"original_child_exit_observed":result.is_ok(),"post_birth_failure_cases":failure_cases,"broker_death_cases":crash_cases,"job_retirement_failure_cases":retirement_cases,"production_ready":false});
    let mut proof = proof;
    proof["dedicated_account_image_observation"] = serde_json::json!(
        result.is_ok() && initializer_cases.contains(&"initializer-account-rpc-self-check")
    );
    proof["self_check_failure"] = serde_json::to_value(self_check_failure).unwrap();
    proof["self_check_cases"] = serde_json::to_value(self_check_cases).unwrap();
    proof["account_profile_cases"] = serde_json::to_value(account_profile_cases).unwrap();
    proof["paired_account_cases"] = serde_json::to_value(paired_account_cases).unwrap();
    proof["paired_self_check_cases"] = serde_json::to_value(paired_self_check_cases).unwrap();
    proof["paired_profile_cases"] = serde_json::to_value(paired_profile_cases).unwrap();
    proof["paired_namespace_cases"] = serde_json::to_value(paired_namespace_cases).unwrap();
    proof["paired_host_profile_cases"] = serde_json::to_value(paired_host_profile_cases).unwrap();
    proof["paired_desktop_cases"] = serde_json::to_value(paired_desktop_cases).unwrap();
    proof["paired_initializer_cases"] = serde_json::to_value(paired_initializer_cases).unwrap();
    proof["paired_initializer_profile_cases"] =
        serde_json::to_value(paired_initializer_profile_cases).unwrap();
    proof["paired_desktop_profile_cases"] =
        serde_json::to_value(paired_desktop_profile_cases).unwrap();
    proof["paired_station_cases"] = serde_json::to_value(paired_station_cases).unwrap();
    proof["paired_station_profile_cases"] =
        serde_json::to_value(paired_station_profile_cases).unwrap();
    proof["paired_profile_crash_cases"] = serde_json::to_value(paired_profile_crash_cases).unwrap();
    proof["paired_writer_crash_cases"] = serde_json::to_value(paired_writer_crash_cases).unwrap();
    proof["paired_initialized_writer_crash_cases"] =
        serde_json::to_value(paired_initialized_writer_crash_cases).unwrap();
    proof["paired_resume_crash_cases"] = serde_json::to_value(paired_resume_crash_cases).unwrap();
    proof["paired_account_crash_cases"] = serde_json::to_value(paired_account_crash_cases).unwrap();
    proof["account_profile_crash_cases"] =
        serde_json::to_value(account_profile_crash_cases).unwrap();
    proof["self_check_crash_cases"] = serde_json::to_value(self_check_crash_cases).unwrap();
    proof["namespace_cases"] = serde_json::to_value(namespace_cases).unwrap();
    proof["namespace_crash_cases"] = serde_json::to_value(namespace_crash_cases).unwrap();
    proof["station_cases"] = serde_json::to_value(station_cases).unwrap();
    proof["station_crash_cases"] = serde_json::to_value(station_crash_cases).unwrap();
    proof["desktop_cases"] = serde_json::to_value(desktop_cases).unwrap();
    proof["desktop_crash_cases"] = serde_json::to_value(desktop_crash_cases).unwrap();
    proof["host_profile_cases"] = serde_json::to_value(host_profile_cases).unwrap();
    proof["host_profile_crash_cases"] = serde_json::to_value(host_profile_crash_cases).unwrap();
    proof["host_profile_used_cases"] = serde_json::to_value(host_profile_used_cases).unwrap();
    proof["host_profile_registry_cases"] =
        serde_json::to_value(host_profile_registry_cases).unwrap();
    proof["host_profile_used_crash_cases"] =
        serde_json::to_value(host_profile_used_crash_cases).unwrap();
    proof["initializer_failures"] = serde_json::to_value(initializer_failures).unwrap();
    proof["initializer_cases"] = serde_json::to_value(initializer_cases).unwrap();
    proof["initializer_crash_cases"] = serde_json::to_value(initializer_crash_cases).unwrap();
    // Preserve every evidence field without whitespace amplification of raw ACL arrays.
    std::fs::write(report, serde_json::to_vec(&proof).unwrap()).unwrap();
    assert!(
        result.is_ok() && disabled.is_ok(),
        "account birth qualification failed at {stage}; native HRESULT={hresult:?}; sanitized report preserved"
    );
}

#[path = "account_bootstrap_desktop_hosted_tests.rs"]
mod desktop;
#[path = "account_bootstrap_host_profile_hosted_tests.rs"]
mod host_profile;
#[path = "account_bootstrap_namespace_hosted_tests.rs"]
mod namespace;
#[path = "account_bootstrap_station_hosted_tests.rs"]
mod station;

#[path = "account_bootstrap_initializer_hosted_tests.rs"]
mod initializer;

#[path = "account_bootstrap_account_resume_hosted_tests.rs"]
mod self_check;
