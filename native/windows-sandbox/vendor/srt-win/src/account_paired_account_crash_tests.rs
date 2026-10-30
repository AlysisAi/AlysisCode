//! Disposable-host cuts around real CPWLW children in both original sibling jobs.
//! Birth cuts keep account children suspended; fixed resume cuts independently
//! retain the original child before it starts. Controllers own process read
//! handles only, never job ownership or reconstructed cleanup rights.
use super::crash::{Broker, Reference, original_reference, rows, wait_absent};
use super::*;
use crate::account_control::bootstrap::pull::AccountExitObserver;
use paired_plan::native::account::ACCOUNT_CRASH_CHECKPOINT;
use std::{
    io::{Read, Write},
    process::{Command, Stdio},
    time::Instant,
};
use windows::Win32::System::Threading::TerminateProcess;

const TEST: &str =
    "account_state::execution::tests::atomic_account_birth::hosted::paired_account_held_broker";
const ROOT: &str = "ALYSIS_TEST_PAIRED_ACCOUNT_CRASH_ROOT";
const NONCE: &str = "ALYSIS_TEST_PAIRED_ACCOUNT_CRASH_NONCE";
const POINTS: [&str; 8] = [
    "paired-owner-born",
    "paired-owner-acknowledged",
    "paired-consumer-born",
    "paired-consumer-acknowledged",
    "paired-both-acknowledged",
    "paired-self-check-intent",
    "paired-self-check-resumed-hot-receipt",
    "paired-self-check-exited",
];

#[path = "account_paired_resume_crash_tests.rs"]
mod resume;

#[derive(Serialize, Deserialize, PartialEq, Debug)]
#[serde(deny_unknown_fields)]
struct Snapshot {
    plan: String,
    bootstrap: Vec<(u32, u32, String)>,
    account: Vec<(u32, u32, String)>,
    resume: Vec<(u32, u32, String)>,
}
impl Snapshot {
    fn read(sql: &Connection) -> Self {
        assert!(sql.is_autocommit());
        Self {
            plan: sql.query_row("SELECT plan_json FROM candidate_paired_bootstrap_plan", [], |r| r.get(0)).unwrap(),
            bootstrap: rows(sql),
            account: sql.prepare("SELECT role,sequence,event_json FROM candidate_paired_account_events ORDER BY role,sequence").unwrap()
                .query_map([], |r| Ok((r.get(0)?,r.get(1)?,r.get(2)?))).unwrap()
                .collect::<std::result::Result<_,_>>().unwrap(),
            resume: sql.prepare("SELECT role,sequence,event_json FROM candidate_paired_account_resume_events ORDER BY role,sequence").unwrap()
                .query_map([], |r| Ok((r.get(0)?,r.get(1)?,r.get(2)?))).unwrap()
                .collect::<std::result::Result<_,_>>().unwrap(),
        }
    }
    fn require_prefix(&self, point: &str) {
        self.require_account_prefix(point);
        resume::require_prefix(self, point);
    }
    fn require_account_prefix(&self, point: &str) {
        assert!(POINTS.contains(&point));
        let expected = match point {
            "paired-owner-born" => [3, 0],
            "paired-owner-acknowledged" => [4, 0],
            "paired-consumer-born" => [5, 3],
            "paired-consumer-acknowledged" => [5, 4],
            "paired-both-acknowledged"
            | "paired-self-check-intent"
            | "paired-self-check-resumed-hot-receipt"
            | "paired-self-check-exited" => [5, 5],
            _ => unreachable!(),
        };
        for (role, count) in expected.into_iter().enumerate() {
            let bootstrap: Vec<_> = self
                .bootstrap
                .iter()
                .filter(|r| r.0 as usize == role)
                .collect();
            assert_eq!(bootstrap.len(), 7, "crash cannot claim retirement");
            assert!(bootstrap.iter().enumerate().all(|(i, r)| r.1 as usize == i));
            let account: Vec<_> = self
                .account
                .iter()
                .filter(|r| r.0 as usize == role)
                .collect();
            assert_eq!(account.len(), count);
            for (i, row) in account.iter().enumerate() {
                assert_eq!(row.1 as usize, i);
                let value: serde_json::Value = serde_json::from_str(&row.2).unwrap();
                assert_eq!(
                    value["kind"],
                    [
                        "intent",
                        "delivered",
                        "birth_intent",
                        "born",
                        "acknowledged"
                    ][i]
                );
                if i >= 2 {
                    assert_eq!(
                        value["request"]["purpose"],
                        if role == 1 && resume::is_resume(point) {
                            "trusted_self_check_v1"
                        } else {
                            "observe_only"
                        }
                    );
                }
            }
        }
        assert_eq!(self.bootstrap.len(), 14);
        assert_eq!(self.account.len(), expected.iter().sum::<usize>());
    }
}
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Proof {
    nonce: String,
    point: String,
    execution: ExecutionRecord,
    snapshot: Snapshot,
    processes: Vec<(String, Reference)>,
}
struct Held {
    root: PathBuf,
    nonce: String,
    point: String,
    execution: ExecutionRecord,
    processes: Vec<(String, OwnedHandle)>,
}
fn retain_account(observer: &AccountExitObserver) -> OwnedHandle {
    let (handle, _, _) = observer.fixture_source_identity().unwrap();
    retain_original(HANDLE(handle as *mut _))
}
impl Held {
    fn hold(mut self, sql: &Connection, current: Option<(Role, AccountExitObserver)>) -> ! {
        if let Some((role, observer)) = current {
            self.processes.push((
                if role == Role::Owner {
                    "owner-account"
                } else {
                    "consumer-account"
                }
                .into(),
                retain_account(&observer),
            ));
        }
        self.hold_snapshot(Snapshot::read(sql))
    }
    fn publish(&self, snapshot: Snapshot, name: &str) {
        let proof = Proof {
            nonce: self.nonce.clone(),
            point: self.point.clone(),
            execution: self.execution.clone(),
            snapshot,
            processes: self
                .processes
                .iter()
                .map(|(kind, handle)| (kind.clone(), Reference::original(handle)))
                .collect(),
        };
        let bytes = serde_json::to_vec(&proof).unwrap();
        assert!(bytes.len() < 65536);
        let path = self.root.join(format!("{name}.part"));
        let destination = self.root.join(name);
        assert!(!destination.exists());
        std::fs::write(&path, bytes).unwrap();
        std::fs::rename(path, destination).unwrap();
    }
    fn hold_snapshot(self, snapshot: Snapshot) -> ! {
        snapshot.require_prefix(&self.point);
        for (kind, process) in &self.processes {
            resume::require_process_state(&self.point, kind, process);
        }
        self.publish(snapshot, "paired-account-cut.json");
        std::thread::sleep(Duration::from_secs(30));
        // A watchdog cannot run Rust destructors and masquerade as broker death.
        std::process::exit(91);
    }
}
fn install(held: Held, selected: Role, phase: &'static str) {
    let mut held = Some(held);
    ACCOUNT_CRASH_CHECKPOINT.with(|slot| {
        *slot.borrow_mut() = Some(Box::new(move |actual, role, sql, observer| {
            if actual == phase && role == selected {
                held.take().unwrap().hold(sql, Some((role, observer)));
            }
        }))
    });
}

/// Called only from the existing hosted admission entry, with stdin-only secret.
pub(super) fn broker(password: &str) {
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
    let root = PathBuf::from(std::env::var_os(ROOT).unwrap());
    assert_eq!(root.parent().unwrap(), crate::util::test_temp_dir());
    assert!(
        root.file_name()
            .unwrap()
            .to_str()
            .unwrap()
            .starts_with("alysis-execution-test-")
    );
    let bytes = std::fs::read(root.join("paired-account-control.json")).unwrap();
    assert!(bytes.len() < 65536);
    let control: DiskCrashControl = serde_json::from_slice(&bytes).unwrap();
    validate_random_id(&control.nonce).unwrap();
    assert_eq!(std::env::var(NONCE).unwrap(), control.nonce);
    assert!(POINTS.contains(&control.checkpoint.as_str()));
    let mut fixture = PairFixture::from_fixture_image(
        Fixture {
            root,
            setup: control.setup.clone(),
            preserve: true,
        },
        true,
    );
    let owner_server = fixture.owner_server.take().unwrap();
    let consumer_server = fixture.consumer_server.take().unwrap();
    let plan = fixture.plan();
    let jobs = plan.create_jobs().unwrap();
    let owner = jobs.prepare_bootstrap(Role::Owner).unwrap();
    let consumer = jobs.prepare_bootstrap(Role::Consumers).unwrap();
    let owner_ref = owner.fixture_readonly_observer().unwrap();
    let consumer_ref = consumer.fixture_readonly_observer().unwrap();
    let owner = owner.start(owner_server, Duration::from_secs(10)).unwrap();
    let consumer = consumer
        .start(consumer_server, Duration::from_secs(10))
        .unwrap();
    let held = |owner_account: Option<OwnedHandle>| {
        let mut processes = vec![
            ("owner-bootstrap".into(), retain_original(owner_ref.raw())),
            (
                "consumer-bootstrap".into(),
                retain_original(consumer_ref.raw()),
            ),
        ];
        if let Some(account) = owner_account {
            processes.push(("owner-account".into(), account));
        }
        Held {
            root: fixture._fixture.root.clone(),
            nonce: control.nonce.clone(),
            point: control.checkpoint.clone(),
            execution: fixture.active.record().unwrap().clone(),
            processes,
        }
    };
    let phase = if control.checkpoint.ends_with("-born") {
        "born"
    } else {
        "acknowledged"
    };
    if control.checkpoint.starts_with("paired-owner-") {
        install(held(None), Role::Owner, phase);
    }
    let owner = owner
        .fixture_observe_account(Duration::from_secs(10), password)
        .unwrap();
    let owner_account = owner.fixture_exit_observer().unwrap();
    if resume::is_resume(&control.checkpoint) {
        resume::install(held(Some(retain_account(&owner_account))));
        consumer
            .fixture_run_consumer_self_check(Duration::from_secs(10), password)
            .unwrap();
        panic!("paired consumer resume crash checkpoint was missed");
    }
    if control.checkpoint.starts_with("paired-consumer-") {
        install(
            held(Some(retain_account(&owner_account))),
            Role::Consumers,
            phase,
        );
    }
    let consumer = consumer
        .fixture_observe_account(Duration::from_secs(10), password)
        .unwrap();
    assert_eq!(
        control.checkpoint, "paired-both-acknowledged",
        "paired crash checkpoint was missed"
    );
    held(Some(retain_account(&owner_account))).hold(
        fixture.db.connection().unwrap(),
        Some((Role::Consumers, consumer.fixture_exit_observer().unwrap())),
    );
}

struct Evidence {
    root: PathBuf,
    point: String,
    passed: bool,
}
impl Drop for Evidence {
    fn drop(&mut self) {
        let Some(temp) = std::env::var_os("RUNNER_TEMP") else {
            return;
        };
        let temp = PathBuf::from(temp);
        let proof = std::fs::read(self.root.join("paired-account-cut.json"))
            .ok()
            .filter(|b| b.len() < 65536)
            .and_then(|b| serde_json::from_slice::<serde_json::Value>(&b).ok());
        let observation = std::fs::read(self.root.join("paired-account-observe.json"))
            .ok()
            .filter(|b| b.len() < 65536)
            .and_then(|b| serde_json::from_slice::<serde_json::Value>(&b).ok());
        let report = serde_json::json!({"point":self.point,"controller_passed":self.passed,
            "original_proof":proof,"original_observation":observation,
            "controller_pinned_before_resume":self.passed && resume::is_resume(&self.point),
            "observer_owns_job":false,"production_ready":false});
        if let Ok(bytes) = serde_json::to_vec_pretty(&report) {
            let _ = std::fs::write(
                temp.join(format!("alysis-account-birth-cut-{}.json", self.point)),
                bytes,
            );
        }
        if let Ok(file) = std::fs::File::open(self.root.join("paired-account-broker.stderr.log")) {
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

/// The hosted caller has admitted the VM and created/bound its owned principals.
pub(super) fn run(fixture: Fixture, password: &str, point: &str) -> Result<()> {
    ensure!(POINTS.contains(&point), "unknown paired account crash cut");
    let mut evidence = Evidence {
        root: fixture.root.clone(),
        point: point.into(),
        passed: false,
    };
    let nonce = random_id()?;
    std::fs::write(
        fixture.root.join("paired-account-control.json"),
        serde_json::to_vec(&DiskCrashControl {
            setup: fixture.setup.clone(),
            checkpoint: point.into(),
            nonce: nonce.clone(),
        })?,
    )?;
    // Every source receive timeout is at least ten seconds after this instant.
    // Refuse a slow pre-kill run rather than crediting source timeout cleanup.
    let began = Instant::now();
    let mut broker = Broker(
        Command::new(std::env::current_exe()?)
            .args([
                TEST,
                "--exact",
                "--ignored",
                "--nocapture",
                "--test-threads=1",
            ])
            .env(ROOT, &fixture.root)
            .env(NONCE, &nonce)
            .env_remove("RUST_TEST_THREADS")
            .stdin(Stdio::piped())
            .stdout(std::fs::File::create_new(
                fixture.root.join("paired-account-broker.stdout.log"),
            )?)
            .stderr(std::fs::File::create_new(
                fixture.root.join("paired-account-broker.stderr.log"),
            )?)
            .spawn()?,
    );
    broker
        .0
        .stdin
        .take()
        .context("paired broker stdin absent")?
        .write_all(password.as_bytes())?;
    let original = HANDLE(broker.0.as_raw_handle());
    let created = process_created(original)?;
    let deadline = Instant::now() + Duration::from_secs(25);
    let resumed = resume::is_resume(point);
    let cut = fixture.root.join(if resumed {
        "paired-account-observe.json"
    } else {
        "paired-account-cut.json"
    });
    while !cut.exists() {
        ensure!(
            broker.0.try_wait()?.is_none(),
            "paired account broker exited before cut; logs retained"
        );
        ensure!(
            Instant::now() < deadline,
            "paired account cut timed out; logs retained"
        );
        std::thread::sleep(Duration::from_millis(10));
    }
    let bytes = std::fs::read(cut)?;
    ensure!(bytes.len() < 65536, "paired account proof exceeds bound");
    let mut proof: Proof = serde_json::from_slice(&bytes)?;
    ensure!(
        proof.nonce == nonce
            && proof.point == point
            && proof.execution.pid == broker.0.id()
            && proof.execution.created == created
            && proof.execution.scope == PAIRED_PLAN_SCOPE,
        "paired original broker binding differs"
    );
    if resumed {
        resume::require_before(&proof);
    } else {
        proof.snapshot.require_prefix(point);
    }
    let expected_kinds = if point.starts_with("paired-owner-") {
        vec!["owner-bootstrap", "consumer-bootstrap", "owner-account"]
    } else {
        vec![
            "owner-bootstrap",
            "consumer-bootstrap",
            "owner-account",
            "consumer-account",
        ]
    };
    ensure!(
        proof
            .processes
            .iter()
            .map(|(kind, _)| kind.as_str())
            .collect::<Vec<_>>()
            == expected_kinds,
        "paired original process roles differ"
    );
    let mut pids = proof
        .processes
        .iter()
        .map(|(_, r)| r.pid)
        .collect::<Vec<_>>();
    pids.sort();
    pids.dedup();
    ensure!(
        pids.len() == proof.processes.len() && pids.iter().all(|pid| *pid != broker.0.id()),
        "paired process identities alias"
    );
    let marker = std::fs::read(fixture.path())?;
    ensure!(
        serde_json::from_slice::<ExecutionRecord>(&marker)? == proof.execution,
        "paired pending marker differs"
    );
    let ready = ReadyInstallation::fixture(fixture.setup.clone(), &fixture.root)?;
    let image = PinnedRunnerImage::prepare(&ready)?;
    let observed = proof
        .processes
        .iter()
        .map(|(_, r)| original_reference(original, r, &image))
        .collect::<Vec<_>>();
    for consumer in [false, true] {
        let name =
            crate::job::paired_execution_name(&proof.execution.identity.container, consumer)?;
        let expected = if consumer && point.starts_with("paired-owner-") {
            1
        } else {
            2
        };
        ensure!(
            counts(&name)
                .context("paired original job absent before cut")?
                .0
                >= expected,
            "paired original job lacks expected processes"
        );
        // Membership checks use a short-lived query-only job handle. It is closed
        // before killing the broker, so it cannot mask leaked job ownership.
        let job =
            OwnedHandle(unsafe { OpenJobObjectW(JOB_OBJECT_QUERY, false, pcwstr(&wstr(&name)))? });
        for ((kind, _), process) in proof.processes.iter().zip(&observed) {
            let mut member = windows::core::BOOL::default();
            unsafe {
                IsProcessInJob(process.raw(), Some(job.raw()), &mut member)?;
            }
            ensure!(
                member.as_bool() == kind.starts_with(if consumer { "consumer-" } else { "owner-" }),
                "paired child escaped or joined sibling job"
            );
        }
    }
    if resumed {
        proof = resume::advance(&fixture, &proof, began)?;
    }
    let stopped = Instant::now() + Duration::from_secs(10);
    for ((kind, _), process) in proof.processes.iter().zip(&observed) {
        resume::require_process_state(point, kind, process);
    }
    ensure!(
        began.elapsed() < Duration::from_secs(8),
        "paired pre-kill work reached source timeout margin"
    );
    unsafe {
        TerminateProcess(original, 74)?;
    }
    ensure!(
        began.elapsed() < Duration::from_secs(8),
        "paired kill crossed source timeout margin"
    );
    ensure!(
        unsafe { WaitForSingleObject(original, 5000) } == WAIT_OBJECT_0,
        "paired broker did not exit"
    );
    ensure!(
        broker.0.wait()?.code() == Some(74),
        "paired broker was not forcibly stopped"
    );
    for process in &observed {
        dead(process);
    }
    ensure!(
        Instant::now() < stopped,
        "paired children exceeded death bound"
    );
    drop(observed);
    for consumer in [false, true] {
        wait_absent(&crate::job::paired_execution_name(
            &proof.execution.identity.container,
            consumer,
        )?);
    }
    if point == "paired-self-check-resumed-hot-receipt" {
        resume::verify_hot(&fixture, &proof, &marker)?;
    } else {
        let db = protected_database(&fixture);
        ensure!(
            db.file_identity() == proof.execution.database_id,
            "paired cold database changed"
        );
        assert_eq!(Snapshot::read(db.connection()?), proof.snapshot);
    }
    ensure!(
        std::fs::read(fixture.path())? == marker,
        "paired crash claimed cleanup"
    );
    evidence.passed = true;
    Ok(())
}
