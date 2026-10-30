//! Hosted-only original-broker death after actual account child observation.
//! The controller holds query/synchronize process authority only, never a job.
use super::*;
use crate::account_control::bootstrap::pull::AccountExitObserver;
use crate::account_state::execution::resources::station::Station;
use crate::acl::object_grants::Snapshot as StationSnapshot;
use bootstrap_plan::lifecycle::resume::logon::delivery::birth::BIRTH_NATIVE_CHECKPOINT;
use serde::{Deserialize, Serialize};
use std::{
    io::{Read, Write},
    process::{Command, Stdio},
    time::Instant,
};
use windows::Win32::{
    Foundation::{
        DUPLICATE_HANDLE_OPTIONS, DuplicateHandle, GetHandleInformation, HANDLE_FLAG_INHERIT,
    },
    System::Threading::{GetProcessId, TerminateProcess},
};
#[path = "account_bootstrap_account_profile_crash_tests.rs"]
mod account_profile;
#[path = "account_bootstrap_host_profile_crash_tests.rs"]
mod profile_use;
#[path = "account_bootstrap_account_resume_crash_tests.rs"]
mod self_check;

const TEST: &str = "account_state::execution::tests::atomic_account_birth::hosted::crash::account_birth_held_broker";
const ROOT_ENV: &str = "ALYSIS_TEST_ACCOUNT_BIRTH_CRASH_ROOT";
const NONCE_ENV: &str = "ALYSIS_TEST_ACCOUNT_BIRTH_CRASH_NONCE";
// Disposable crash evidence includes complete station DACLs. Negative lifetime
// cases intentionally preserve grants, so their snapshots grow across this
// serial VM batch. Match the existing paired crash-report ceiling on all paths;
// this is not a runtime control-frame, operation or containment policy limit.
const MAX_PROOF_BYTES: usize = 65_536;

fn read_bounded_proof(path: &std::path::Path) -> Result<Vec<u8>> {
    let mut bytes = Vec::new();
    std::fs::File::open(path)?
        .take(MAX_PROOF_BYTES as u64)
        .read_to_end(&mut bytes)?;
    ensure!(
        bytes.len() < MAX_PROOF_BYTES,
        "account crash proof exceeds bound"
    );
    Ok(bytes)
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Snapshot {
    plan: String,
    delivery: String,
    birth: Vec<String>,
    retirement: Vec<String>,
    namespace: Vec<String>,
    station: Vec<String>,
    desktop: Vec<String>,
    host_profile: Vec<String>,
    initializer: Vec<String>,
    account_resume: Vec<String>,
    account_profile: Vec<String>,
}
impl Snapshot {
    fn read(sql: &Connection) -> Result<Self> {
        Ok(Self {
            account_profile: sql
                .prepare(
                    "SELECT event_json FROM candidate_atomic_account_profile ORDER BY sequence",
                )?
                .query_map([], |r| r.get(0))?
                .collect::<std::result::Result<_, _>>()?,
            plan: sql.query_row(
                "SELECT plan_json FROM candidate_atomic_bootstrap_plan",
                [],
                |r| r.get(0),
            )?,
            delivery: sql.query_row(
                "SELECT receipt_json FROM candidate_atomic_credential_delivery",
                [],
                |r| r.get(0),
            )?,
            birth: sql
                .prepare("SELECT event_json FROM candidate_atomic_account_birth ORDER BY sequence")?
                .query_map([], |r| r.get(0))?
                .collect::<std::result::Result<_, _>>()?,
            namespace: sql
                .prepare("SELECT event_json FROM candidate_atomic_namespace ORDER BY sequence")?
                .query_map([], |r| r.get(0))?
                .collect::<std::result::Result<_, _>>()?,
            station: sql
                .prepare("SELECT event_json FROM candidate_atomic_station ORDER BY sequence")?
                .query_map([], |r| r.get(0))?
                .collect::<std::result::Result<_, _>>()?,
            desktop: sql
                .prepare("SELECT event_json FROM candidate_atomic_desktop ORDER BY sequence")?
                .query_map([], |r| r.get(0))?
                .collect::<std::result::Result<_, _>>()?,
            host_profile: sql
                .prepare("SELECT event_json FROM candidate_atomic_host_profile ORDER BY sequence")?
                .query_map([], |r| r.get(0))?
                .collect::<std::result::Result<_, _>>()?,
            initializer: sql
                .prepare("SELECT event_json FROM candidate_atomic_initializer ORDER BY sequence")?
                .query_map([], |r| r.get(0))?
                .collect::<std::result::Result<_, _>>()?,
            account_resume: sql
                .prepare(
                    "SELECT event_json FROM candidate_atomic_account_resume ORDER BY sequence",
                )?
                .query_map([], |r| r.get(0))?
                .collect::<std::result::Result<_, _>>()?,
            retirement: sql
                .prepare(
                    "SELECT event_json FROM candidate_atomic_job_retirement ORDER BY sequence",
                )?
                .query_map([], |r| r.get(0))?
                .collect::<std::result::Result<_, _>>()?,
        })
    }
    fn require_equal(&self, sql: &Connection) -> Result<()> {
        let actual = Self::read(sql)?;
        ensure!(
            self.plan == actual.plan
                && self.delivery == actual.delivery
                && self.birth == actual.birth
                && self.retirement == actual.retirement
                && self.namespace == actual.namespace
                && self.station == actual.station
                && self.desktop == actual.desktop
                && self.host_profile == actual.host_profile
                && self.initializer == actual.initializer
                && self.account_resume == actual.account_resume
                && self.account_profile == actual.account_profile,
            "account crash durable prefix changed"
        );
        Ok(())
    }
}
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Proof {
    nonce: String,
    point: String,
    execution: ExecutionRecord,
    source_handle: u64,
    child_pid: u32,
    child_created: u64,
    snapshot: Snapshot,
}

fn hold(root: &std::path::Path, proof: &Proof, _original: AccountExitObserver) -> ! {
    // _original is retained across publication and until forced broker death.
    let bytes = serde_json::to_vec(proof).unwrap();
    eprintln!(
        "account crash proof bytes={} limit={MAX_PROOF_BYTES}",
        bytes.len()
    );
    assert!(bytes.len() < MAX_PROOF_BYTES);
    let part = root.join("account-birth-cut.part");
    std::fs::write(&part, bytes).unwrap();
    std::fs::rename(&part, root.join("account-birth-cut.json")).unwrap();
    std::thread::sleep(Duration::from_secs(20));
    panic!("controller did not terminate original held broker");
}

#[test]
#[ignore = "bounded hosted broker subprocess; requires original controller and stdin-only synthetic secret"]
fn account_birth_held_broker() {
    require_host().unwrap();
    assert_eq!(
        std::env::args().skip(1).collect::<Vec<_>>(),
        [
            TEST,
            "--exact",
            "--ignored",
            "--nocapture",
            "--test-threads=1"
        ]
    );
    let root = PathBuf::from(std::env::var_os(ROOT_ENV).unwrap());
    assert_eq!(root.parent().unwrap(), crate::util::test_temp_dir());
    assert!(
        root.file_name()
            .unwrap()
            .to_str()
            .unwrap()
            .starts_with("alysis-execution-test-")
    );
    let bytes = std::fs::read(root.join("account-birth-control.json")).unwrap();
    assert!(bytes.len() < 65536);
    let control: DiskCrashControl = serde_json::from_slice(&bytes).unwrap();
    validate_random_id(&control.nonce).unwrap();
    assert_eq!(std::env::var(NONCE_ENV).unwrap(), control.nonce);
    assert!(matches!(
        control.checkpoint.as_str(),
        "observed"
            | "acknowledged"
            | "hot-receipt"
            | "namespace-observed"
            | "namespace-acknowledged"
            | "namespace-hot-receipt"
            | "station-observed"
            | "station-acknowledged"
            | "station-hot-receipt"
            | "desktop-observed"
            | "desktop-acknowledged"
            | "desktop-hot-receipt"
            | "host-profile-observed"
            | "host-profile-acknowledged"
            | "host-profile-hot-receipt"
            | "host-profile-used-captured"
            | "host-profile-used-closed"
            | "host-profile-used-hot-receipt"
            | "initializer-observed"
            | "initializer-acknowledged"
            | "initializer-hot-receipt"
            | "self-check-intent"
            | "self-check-resumed-hot-receipt"
            | "self-check-exited"
            | "account-profile-intent"
            | "account-profile-created-hot-receipt"
            | "account-profile-removed-hot-receipt"
    ));
    let initialized = control.checkpoint.starts_with("initializer-");
    let self_check_resume = control.checkpoint.starts_with("self-check-");
    let account_profile_roundtrip = control.checkpoint.starts_with("account-profile-");
    let host_profile = initialized || control.checkpoint.starts_with("host-profile-");
    let used_profile = control.checkpoint.starts_with("host-profile-used-");
    if host_profile {
        require_profile_host().unwrap();
    }
    let desktop = host_profile || control.checkpoint.starts_with("desktop-");
    let station = desktop || control.checkpoint.starts_with("station-");
    if station {
        super::station::require_station_host().unwrap();
    }
    let mut password = Password(String::with_capacity(128));
    std::io::stdin()
        .take(128)
        .read_to_string(&mut password.0)
        .unwrap();
    assert!(password.0.len() == 68 && password.0.is_ascii());
    let fixture = Fixture {
        root,
        setup: control.setup,
        preserve: true,
    };
    let db = protected_database(&fixture);
    let ready = ReadyInstallation::fixture(fixture.setup.clone(), &fixture.root).unwrap();
    let active = atomic_fixture_active(&fixture, &db);
    let image = PinnedRunnerImage::prepare(&ready).unwrap();
    let (server, address) =
        ControlServer::prepare(&ready.binding(AccountMode::Offline).unwrap().account_sid).unwrap();
    let plan =
        bootstrap_plan::CommittedPlan::prepare(&db, &active, &image, &ready, &address).unwrap();
    let job = plan.create_job().unwrap();
    let execution = active.record().unwrap().clone();
    let root = fixture.root.clone();
    let namespaced = station || control.checkpoint.starts_with("namespace-");
    let native_point = control
        .checkpoint
        .strip_prefix("namespace-")
        .or_else(|| control.checkpoint.strip_prefix("station-"))
        .or_else(|| control.checkpoint.strip_prefix("desktop-"))
        .or_else(|| control.checkpoint.strip_prefix("host-profile-"))
        .or_else(|| control.checkpoint.strip_prefix("initializer-"))
        .unwrap_or(&control.checkpoint)
        .to_owned();
    if account_profile_roundtrip {
        bootstrap_plan::lifecycle::resume::logon::delivery::birth::UNUSED_PROFILE_SELF_CHECK
            .with(|s| s.set(true));
        account_profile::install(&control.checkpoint, &control.nonce, &execution, &root);
    } else if self_check_resume {
        self_check::install(&control.checkpoint, &control.nonce, &execution, &root);
    } else if used_profile {
        profile_use::install(&control.checkpoint, &control.nonce, &execution, &root);
    } else {
        BIRTH_NATIVE_CHECKPOINT.with(|slot| {
            *slot.borrow_mut() = Some(Box::new(move |phase, original, sql| {
                if phase != native_point && !(phase == "observed" && native_point == "hot-receipt")
                {
                    return Ok(());
                }
                let (handle, child_pid, child_created) = original.fixture_source_identity()?;
                let proof = Proof {
                    nonce: control.nonce.clone(),
                    point: control.checkpoint.clone(),
                    execution: execution.clone(),
                    source_handle: handle as u64,
                    child_pid,
                    child_created,
                    snapshot: Snapshot::read(sql)?,
                };
                if native_point != "hot-receipt" {
                    hold(&root, &proof, original);
                }
                let path =
                    crate::account_state::database_path(&root, &proof.execution.identity.binding)?;
                let before = std::fs::read(&path)?;
                ensure!(
                    before.len() <= 1024 * 1024,
                    "account crash database exceeds bound"
                );
                std::fs::write(root.join("account-before-write.db"), &before)?;
                let root = root.clone();
                let mut held = Some((proof, original));
                BIRTH_CHECKPOINT.with(|slot| {
                    *slot.borrow_mut() = Some(Box::new(move |phase, sequence, sql| {
                        if phase != "inserted" || sequence != 1 {
                            return;
                        }
                        assert!(!sql.is_autocommit());
                        sql.cache_flush().unwrap();
                        let bytes = std::fs::read(path.with_extension("db-journal")).unwrap();
                        assert!(bytes.len() > 512 && bytes.len() <= 1024 * 1024);
                        assert_eq!(
                            &bytes[..8],
                            &[0xd9, 0xd5, 0x05, 0xf9, 0x20, 0xa1, 0x63, 0xd7]
                        );
                        assert_ne!(std::fs::read(&path).unwrap(), before);
                        let (proof, original) = held.take().unwrap();
                        hold(&root, &proof, original);
                    }));
                });
                Ok(())
            }));
        });
    }
    let _result = if self_check_resume || account_profile_roundtrip {
        job.fixture_run_account_self_check(server, &password.0, Duration::from_secs(10))
    } else if initialized {
        job.with_empty_namespace()
            .and_then(|bundle| bundle.with_host_profile_for_use())
            .and_then(|bundle| bundle.with_station())
            .and_then(|bundle| bundle.with_unused_desktop())
            .and_then(|bundle| {
                bundle.fixture_observe_initialized_account_birth(
                    server,
                    &password.0,
                    Duration::from_secs(10),
                )
            })
    } else if used_profile {
        job.with_empty_namespace()
            .and_then(|bundle| bundle.with_host_profile_for_use())
            .and_then(|bundle| bundle.with_station())
            .and_then(|bundle| bundle.with_unused_desktop())
            .and_then(|bundle| {
                bundle.fixture_observe_account_birth(server, &password.0, Duration::from_secs(10))
            })
    } else if host_profile {
        job.with_empty_namespace()
            .and_then(|bundle| bundle.with_unused_host_profile())
            .and_then(|bundle| bundle.with_station())
            .and_then(|bundle| bundle.with_unused_desktop())
            .and_then(|bundle| {
                bundle.fixture_observe_account_birth(server, &password.0, Duration::from_secs(10))
            })
    } else if desktop {
        job.with_empty_namespace()
            .and_then(|bundle| bundle.with_station())
            .and_then(|bundle| bundle.with_unused_desktop())
            .and_then(|bundle| {
                bundle.fixture_observe_account_birth(server, &password.0, Duration::from_secs(10))
            })
    } else if station {
        job.with_empty_namespace()
            .and_then(|bundle| bundle.with_station())
            .and_then(|bundle| {
                bundle.fixture_observe_account_birth(server, &password.0, Duration::from_secs(10))
            })
    } else if namespaced {
        job.with_empty_namespace().and_then(|bundle| {
            bundle.fixture_observe_account_birth(server, &password.0, Duration::from_secs(10))
        })
    } else {
        job.fixture_observe_account_birth(server, &password.0, Duration::from_secs(10))
    };
    panic!("account broker did not reach the selected cut");
}

struct Broker(std::process::Child);
impl Drop for Broker {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

struct CutEvidence {
    root: PathBuf,
    point: String,
    passed: bool,
}
impl Drop for CutEvidence {
    fn drop(&mut self) {
        // Fixed synthetic VM files only. No credential/control-input copy.
        let Some(temp) = std::env::var_os("RUNNER_TEMP") else {
            return;
        };
        let temp = PathBuf::from(temp);
        let proof = read_bounded_proof(&self.root.join("account-birth-cut.json"))
            .ok()
            .and_then(|b| serde_json::from_slice::<serde_json::Value>(&b).ok());
        let summary = serde_json::json!({"point":self.point,"controller_passed":self.passed,"original_proof":proof,"observer_owns_job":false,"production_ready":false});
        if let Ok(bytes) = serde_json::to_vec_pretty(&summary) {
            let _ = std::fs::write(
                temp.join(format!("alysis-account-birth-cut-{}.json", self.point)),
                bytes,
            );
        }
        if let Ok(file) = std::fs::File::open(self.root.join("account-broker.stderr.log")) {
            let mut bytes = Vec::new();
            if file.take(32768).read_to_end(&mut bytes).is_ok() {
                let _ = std::fs::write(
                    temp.join(format!("alysis-account-birth-broker-{}.log", self.point)),
                    bytes,
                );
            }
        }
    }
}

pub(super) fn run(principals: &Principals, password: &Password, point: &str) -> Result<()> {
    require_host()?;
    ensure!(
        matches!(
            point,
            "observed"
                | "acknowledged"
                | "hot-receipt"
                | "namespace-observed"
                | "namespace-acknowledged"
                | "namespace-hot-receipt"
                | "station-observed"
                | "station-acknowledged"
                | "station-hot-receipt"
                | "desktop-observed"
                | "desktop-acknowledged"
                | "desktop-hot-receipt"
                | "host-profile-observed"
                | "host-profile-acknowledged"
                | "host-profile-hot-receipt"
                | "host-profile-used-captured"
                | "host-profile-used-closed"
                | "host-profile-used-hot-receipt"
                | "initializer-observed"
                | "initializer-acknowledged"
                | "initializer-hot-receipt"
                | "self-check-intent"
                | "self-check-resumed-hot-receipt"
                | "self-check-exited"
                | "account-profile-intent"
                | "account-profile-created-hot-receipt"
                | "account-profile-removed-hot-receipt"
        ),
        "unknown account crash cut"
    );
    let initialized = point.starts_with("initializer-");
    let self_check = point.starts_with("self-check-");
    let account_profile_roundtrip = point.starts_with("account-profile-");
    let self_check_exited = self_check && point != "self-check-intent";
    let host_profile = initialized || point.starts_with("host-profile-");
    let used_profile = point.starts_with("host-profile-used-");
    if host_profile {
        require_profile_host()?;
    }
    let station_before =
        if host_profile || point.starts_with("station-") || point.starts_with("desktop-") {
            super::station::require_station_host()?;
            Some(Station::fixture_snapshot_current()?)
        } else {
            None
        };
    let fixture = bound_fixture(
        principals.offline.as_deref().unwrap(),
        principals.online.as_deref().unwrap(),
        principals.group.as_deref().unwrap(),
    )?;
    let mut evidence = CutEvidence {
        root: fixture.root.clone(),
        point: point.into(),
        passed: false,
    };
    let nonce = random_id()?;
    let control = DiskCrashControl {
        setup: fixture.setup.clone(),
        checkpoint: point.into(),
        nonce: nonce.clone(),
    };
    std::fs::write(
        fixture.root.join("account-birth-control.json"),
        serde_json::to_vec(&control)?,
    )?;
    let mut broker = Broker(
        Command::new(std::env::current_exe()?)
            .args([
                TEST,
                "--exact",
                "--ignored",
                "--nocapture",
                "--test-threads=1",
            ])
            .env(ROOT_ENV, &fixture.root)
            .env(NONCE_ENV, &nonce)
            .env_remove("RUST_TEST_THREADS")
            .stdin(Stdio::piped())
            .stdout(std::fs::File::create_new(
                fixture.root.join("account-broker.stdout.log"),
            )?)
            .stderr(std::fs::File::create_new(
                fixture.root.join("account-broker.stderr.log"),
            )?)
            .spawn()?,
    );
    // Private pipe into this original trusted test child; never argv/env/disk.
    broker
        .0
        .stdin
        .take()
        .context("broker stdin absent")?
        .write_all(password.0.as_bytes())?;
    let original = HANDLE(broker.0.as_raw_handle());
    let created = process_created(original)?;
    let mut session = 0;
    unsafe {
        windows::Win32::System::RemoteDesktop::ProcessIdToSessionId(broker.0.id(), &mut session)?;
    }
    if station_before.is_some() {
        let mut controller_session = 0;
        unsafe {
            windows::Win32::System::RemoteDesktop::ProcessIdToSessionId(
                std::process::id(),
                &mut controller_session,
            )?;
        }
        ensure!(
            controller_session == session,
            "station observer session differs"
        );
    }
    let held = fixture.root.join("account-birth-cut.json");
    // Profile cases include synchronous first-logon profile loading. This is
    // only the bound for reaching the cut; exact original exit and zero-job
    // checks after the cut keep their existing bounds and requirements.
    let deadline = Instant::now()
        + Duration::from_secs(if point.starts_with("account-profile-") {
            90
        } else {
            10
        });
    while !held.exists() {
        ensure!(
            broker.0.try_wait()?.is_none(),
            "account broker exited before cut; logs retained"
        );
        ensure!(
            Instant::now() < deadline,
            "account broker cut timed out; logs retained"
        );
        std::thread::sleep(Duration::from_millis(10));
    }
    let bytes = read_bounded_proof(&held)?;
    let proof: Proof = serde_json::from_slice(&bytes)?;
    ensure!(
        proof.nonce == nonce
            && proof.point == point
            && proof.execution.pid == broker.0.id()
            && proof.execution.created == created,
        "account crash proof differs from original broker"
    );
    ensure!(
        proof.source_handle > 0
            && proof.source_handle < isize::MAX as u64
            && proof.child_pid != broker.0.id()
            && proof.child_created >= created,
        "account crash child identity invalid"
    );
    ensure!(
        proof.snapshot.birth.len()
            == if point.ends_with("acknowledged")
                || used_profile
                || self_check
                || account_profile_roundtrip
            {
                2
            } else {
                1
            },
        "account crash receipt prefix differs"
    );
    ensure!(
        proof.snapshot.retirement.len() == if used_profile { 2 } else { 0 },
        "account crash job retirement prefix differs"
    );
    ensure!(
        proof.snapshot.namespace.len()
            == if point.starts_with("namespace-") || station_before.is_some() {
                2
            } else {
                0
            },
        "namespace crash birth prefix differs or removal was falsely acknowledged"
    );
    let station_after = if let Some(before) = &station_before {
        let after = verify_station_prefix(&proof, session, before)?;
        ensure!(
            Station::fixture_snapshot_current()? == after,
            "station grant differs before abrupt broker death"
        );
        Some(after)
    } else {
        ensure!(
            proof.snapshot.station.is_empty(),
            "unexpected station grant"
        );
        None
    };
    let desktop_name = if host_profile || point.starts_with("desktop-") {
        let name = verify_desktop_prefix(&proof, session)?;
        observe_desktop(&name, true)?;
        Some(name)
    } else {
        ensure!(
            proof.snapshot.desktop.is_empty(),
            "unexpected desktop birth"
        );
        None
    };
    let profile_id = if host_profile {
        let id = verify_profile_prefix(&proof)?;
        observe_profile(&proof.execution, id)?;
        Some(id)
    } else {
        ensure!(
            proof.snapshot.host_profile.is_empty(),
            "unexpected host profile birth"
        );
        None
    };
    if initialized {
        initializer::verify_prefix(&proof)?;
    } else {
        ensure!(
            proof.snapshot.initializer.is_empty(),
            "unexpected initializer records"
        );
    }
    if account_profile_roundtrip {
        account_profile::verify_prefix(&proof)?;
    } else if self_check {
        self_check::verify_prefix(&proof)?;
    } else {
        ensure!(
            proof.snapshot.account_resume.is_empty(),
            "unexpected account resume"
        );
    }
    if !account_profile_roundtrip {
        ensure!(
            proof.snapshot.account_profile.is_empty(),
            "unexpected account profile records"
        );
    }
    let marker = std::fs::read(fixture.path())?;
    ensure!(
        serde_json::from_slice::<ExecutionRecord>(&marker)? == proof.execution,
        "account crash pending identity differs"
    );
    let mut raw = HANDLE::default();
    unsafe {
        DuplicateHandle(
            original,
            HANDLE(proof.source_handle as usize as *mut _),
            GetCurrentProcess(),
            &mut raw,
            (PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SYNCHRONIZE).0,
            false,
            DUPLICATE_HANDLE_OPTIONS(0),
        )?;
    }
    let observation = OwnedHandle(raw);
    ensure!(
        unsafe { GetProcessId(raw) } == proof.child_pid
            && process_created(raw)? == proof.child_created
            && unsafe { WaitForSingleObject(raw, 0) }
                == if used_profile || self_check_exited {
                    WAIT_OBJECT_0
                } else {
                    WAIT_TIMEOUT
                },
        "original account child identity or exit state differs before cut"
    );
    let mut flags = 0;
    unsafe {
        GetHandleInformation(raw, &mut flags)?;
    }
    ensure!(
        flags & HANDLE_FLAG_INHERIT.0 == 0 && unsafe { TerminateProcess(raw, 1) }.is_err(),
        "observer acquired mutation/inheritance authority"
    );
    if used_profile {
        ensure!(
            atomic_job_counts(&proof.execution).is_none(),
            "profile cleanup preceded job exit"
        );
    } else if self_check_exited {
        let mut code = 0;
        unsafe {
            windows::Win32::System::Threading::GetExitCodeProcess(raw, &mut code)?;
        }
        ensure!(code == crate::account_control::bootstrap::entry::credential_wire::child::probe::EXIT_CODE,
            "controller self-check exit code differs");
    } else {
        let ready = ReadyInstallation::fixture(fixture.setup.clone(), &fixture.root)?;
        PinnedRunnerImage::prepare(&ready)?.verify_process(raw)?;
    }
    unsafe {
        TerminateProcess(original, 76)?;
    }
    ensure!(
        unsafe { WaitForSingleObject(original, 5000) } == WAIT_OBJECT_0
            && broker.0.wait()?.code() == Some(76),
        "original account broker did not terminate"
    );
    ensure!(
        unsafe { WaitForSingleObject(observation.raw(), 5000) } == WAIT_OBJECT_0,
        "account child survived abrupt original broker death"
    );
    ensure!(
        atomic_job_counts(&proof.execution).is_none(),
        "account job survived abrupt original broker death"
    );
    if point.starts_with("namespace-") || station_after.is_some() {
        // Read-only absence observation under the same original session parent.
        // No namespace handle is retained by the controller across broker death.
        use crate::account_state::execution::resources::namespace_native as native;
        struct Policy<'a> {
            execution: &'a ExecutionRecord,
            session: u32,
        }
        impl native::NamespacePolicySource for Policy<'_> {
            fn execution(&self) -> &ExecutionRecord {
                self.execution
            }
            fn session(&self) -> u32 {
                self.session
            }
        }
        let policy = Policy {
            execution: &proof.execution,
            session,
        };
        let parent = native::open_parent(&policy)?;
        native::require_absent(parent.raw(), &policy)?;
    }
    if let Some(after) = &station_after {
        // Shared ACL writes survive broker/job death. Preserve that exact
        // unresolved state; neither an observer nor cold rows can restore it.
        ensure!(
            Station::fixture_snapshot_current()? == *after,
            "station grant changed across abrupt broker death"
        );
    }
    if let Some(name) = &desktop_name {
        observe_desktop(name, false)?;
    }
    if let Some(id) = profile_id {
        observe_profile(&proof.execution, id)?;
    }
    if point.ends_with("hot-receipt") {
        verify_hot(&fixture, &proof, &marker)?;
    } else {
        let db = protected_database(&fixture);
        ensure!(
            db.file_identity() == proof.execution.database_id,
            "account crash database identity changed"
        );
        proof.snapshot.require_equal(db.connection()?)?;
    }
    ensure!(
        std::fs::read(fixture.path())? == marker,
        "account crash pending marker changed"
    );
    if let Some(after) = &station_after {
        ensure!(
            Station::fixture_snapshot_current()? == *after,
            "cold evidence inspection changed the unresolved station grant"
        );
    }
    if let Some(name) = &desktop_name {
        observe_desktop(name, false)?;
    }
    if let Some(id) = profile_id {
        observe_profile(&proof.execution, id)?;
    }
    evidence.passed = true;
    Ok(())
}

fn require_profile_host() -> Result<()> {
    super::station::require_station_host()?;
    ensure!(
        std::env::var("ALYSIS_RUN_DISPOSABLE_ATOMIC_PROFILE")
            .ok()
            .as_deref()
            == Some("github-hosted-atomic-profile-only"),
        "atomic host profile crash admission refused"
    );
    Ok(())
}
fn verify_profile_prefix(proof: &Proof) -> Result<[u8; 24]> {
    let expected = match proof.point.as_str() {
        "host-profile-used-captured" => 4,
        "host-profile-used-closed" => 5,
        "host-profile-used-hot-receipt" => 3,
        "initializer-observed" | "initializer-acknowledged" | "initializer-hot-receipt" => 3,
        _ => 2,
    };
    ensure!(
        proof.snapshot.host_profile.len() == expected,
        "host profile crash birth prefix differs"
    );
    let intent: serde_json::Value = serde_json::from_str(&proof.snapshot.host_profile[0])?;
    let created: serde_json::Value = serde_json::from_str(&proof.snapshot.host_profile[1])?;
    let plan: serde_json::Value = serde_json::from_str(&proof.snapshot.plan)?;
    let binding = &intent["binding"];
    ensure!(
        intent["kind"] == "intent"
            && created["kind"] == "created"
            && binding == &created["binding"]
            && binding["operation"] == plan["operation"]
            && binding["owner"] == proof.execution.owner
            && binding["container"] == proof.execution.identity.container
            && binding["package_sid"] == proof.execution.identity.package_sid
            && binding["policy"] == 1
            && binding["nonce"] != binding["operation"],
        "host profile crash binding differs"
    );
    validate_random_id(binding["nonce"].as_str().context("profile nonce absent")?)?;
    let id: [u8; 24] = serde_json::from_value(created["folder_id"].clone())?;
    ensure!(
        id != [0; 24],
        "host profile original storage identity absent"
    );
    if proof.point.starts_with("initializer-") {
        initializer::verify_profile_use(proof)?;
    } else if expected > 2 {
        profile_use::verify_prefix(proof)?;
    }
    Ok(id)
}
fn observe_profile(execution: &ExecutionRecord, id: [u8; 24]) -> Result<()> {
    // Diagnostic filesystem observation only. No lowbox token is created and no
    // storage handle survives broker death. Reopened objects never authorize SDK
    // cleanup or recover a live profile guard from the cold receipt.
    let files = crate::account_profile_filesystem::RetainedProfileFilesystem::open(
        &execution.identity.container,
        &execution.owner,
    )?;
    ensure!(
        files.original_root_id()? == id,
        "unresolved host profile root changed"
    );
    files.close()
}

fn verify_desktop_prefix(proof: &Proof, session: u32) -> Result<String> {
    ensure!(
        proof.snapshot.desktop.len() == 2,
        "desktop crash birth prefix differs"
    );
    let intent: serde_json::Value = serde_json::from_str(&proof.snapshot.desktop[0])?;
    let created: serde_json::Value = serde_json::from_str(&proof.snapshot.desktop[1])?;
    let station: serde_json::Value = serde_json::from_str(&proof.snapshot.station[0])?;
    let plan: serde_json::Value = serde_json::from_str(&proof.snapshot.plan)?;
    let operation = plan["operation"]
        .as_str()
        .context("desktop operation absent")?;
    validate_random_id(operation)?;
    let name = format!("alysis-atomic-desktop-{operation}");
    let binding = &intent["binding"];
    ensure!(
        intent["kind"] == "intent"
            && created["kind"] == "created"
            && binding == &created["binding"]
            && binding["operation"] == operation
            && binding["station_nonce"] == station["binding"]["nonce"]
            && binding["session"] == session
            && plan["session"] == session
            && binding["package_sid"] == proof.execution.identity.package_sid
            && binding["policy"] == 1
            && binding["name"] == name,
        "desktop crash binding differs"
    );
    validate_random_id(binding["nonce"].as_str().context("desktop nonce absent")?)?;
    ensure!(
        binding["nonce"] != binding["operation"] && binding["nonce"] != binding["station_nonce"],
        "desktop nonce reused"
    );
    Ok(name)
}

fn observe_desktop(name: &str, expected_present: bool) -> Result<()> {
    use windows::Win32::System::StationsAndDesktops::{
        CloseDesktop, DESKTOP_CONTROL_FLAGS, OpenDesktopW,
    };
    // Presence observation in the controller's verified matching session.
    // Close this observer before broker kill; it must never keep the object alive.
    match unsafe {
        OpenDesktopW(
            pcwstr(&wstr(name)),
            DESKTOP_CONTROL_FLAGS(0),
            false,
            0x20081,
        )
    } {
        Ok(handle) => {
            unsafe { CloseDesktop(handle) }.context("desktop observer close uncertain")?;
            ensure!(
                expected_present,
                "unused desktop survived abrupt broker death"
            );
        }
        Err(error) => {
            ensure!(
                !expected_present
                    && error.code()
                        == windows::core::HRESULT::from_win32(
                            windows::Win32::Foundation::ERROR_FILE_NOT_FOUND.0
                        ),
                "desktop presence/absence unverified"
            );
        }
    }
    Ok(())
}

fn verify_station_prefix(
    proof: &Proof,
    session: u32,
    before: &StationSnapshot,
) -> Result<StationSnapshot> {
    ensure!(
        proof.snapshot.station.len() == 2,
        "station crash grant prefix differs"
    );
    let intent: serde_json::Value = serde_json::from_str(&proof.snapshot.station[0])?;
    let applied: serde_json::Value = serde_json::from_str(&proof.snapshot.station[1])?;
    let plan: serde_json::Value = serde_json::from_str(&proof.snapshot.plan)?;
    let binding = &intent["binding"];
    ensure!(
        intent["kind"] == "intent"
            && applied["kind"] == "applied"
            && binding == &applied["binding"]
            && binding["operation"] == plan["operation"]
            && binding["session"] == session
            && plan["session"] == session
            && binding["package_sid"] == proof.execution.identity.package_sid
            && binding["policy"] == 1,
        "station crash binding differs"
    );
    validate_random_id(binding["nonce"].as_str().context("station nonce absent")?)?;
    ensure!(
        binding["nonce"] != binding["operation"],
        "station nonce reused"
    );
    let recorded_before: StationSnapshot = serde_json::from_value(intent["before"].clone())?;
    let recorded_after: StationSnapshot = serde_json::from_value(intent["after"].clone())?;
    ensure!(
        recorded_before == *before
            && recorded_after
                == crate::account_state::execution::resources::station::proposed(
                    before,
                    &proof.execution.identity.package_sid
                )?,
        "station crash exact snapshots differ"
    );
    Ok(recorded_after)
}

fn verify_hot(fixture: &Fixture, proof: &Proof, marker: &[u8]) -> Result<()> {
    let path =
        crate::account_state::database_path(&fixture.root, &proof.execution.identity.binding)?;
    let journal = path.with_extension("db-journal");
    let original = std::fs::read(&path)?;
    let rollback = std::fs::read(&journal)?;
    let manifest = std::fs::read(path.with_file_name("identity.json"))?;
    ensure!(
        original.len() <= 1024 * 1024 && rollback.len() > 512 && rollback.len() <= 1024 * 1024,
        "hot account database/journal exceeds bound"
    );
    ensure!(
        original != std::fs::read(fixture.root.join("account-before-write.db"))?,
        "account receipt had no dirty-page flush"
    );
    ensure!(
        &rollback[..8] == [0xd9, 0xd5, 0x05, 0xf9, 0x20, 0xa1, 0x63, 0xd7],
        "account receipt journal is not hot"
    );
    let unchanged = || -> Result<()> {
        ensure!(
            std::fs::read(&path)? == original
                && std::fs::read(&journal)? == rollback
                && std::fs::read(path.with_file_name("identity.json"))? == manifest
                && std::fs::read(fixture.path())? == marker,
            "hot account evidence changed"
        );
        Ok(())
    };
    for _ in 0..2 {
        let error =
            PrivateDatabase::open_under(&fixture.root, &fixture.setup, AccountMode::Offline, None)
                .err()
                .context("hot account original was admitted")?;
        ensure!(
            format!("{error:#}").contains("auxiliary or unknown object"),
            "hot account refusal had another cause"
        );
        unchanged()?;
    }
    // Isolated rollback oracle only; never replace or repair the hot original.
    let root = fixture.root.join("account-rollback-oracle");
    std::fs::create_dir(&root)?;
    let copy = root.join("state.db");
    std::fs::write(&copy, &original)?;
    std::fs::write(copy.with_extension("db-journal"), &rollback)?;
    let sql = Connection::open_with_flags_and_vfs(
        fs::extended_path(&copy)?,
        rusqlite::OpenFlags::SQLITE_OPEN_READ_WRITE,
        "win32-longpath",
    )?;
    ensure!(
        sql.query_row("PRAGMA integrity_check", [], |r| r.get::<_, String>(0))? == "ok",
        "account receipt rollback copy corrupt"
    );
    crate::account_state::validate_header(&sql, &proof.execution.identity.binding)?;
    proof.snapshot.require_equal(&sql)?;
    drop(sql);
    ensure!(
        !copy.with_extension("db-journal").exists(),
        "account rollback oracle did not finish"
    );
    unchanged()
}

#[path = "account_bootstrap_initializer_crash_tests.rs"]
mod initializer;
