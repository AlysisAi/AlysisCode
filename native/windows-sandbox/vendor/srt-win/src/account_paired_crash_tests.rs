//! Real broker termination with both sole job handles still in that process.
//! Controller retains only original child query/synchronize references. No
//! account/profile effects, job ownership transfer or cold cleanup authority.
use super::*;
use std::time::Instant;
use windows::Win32::{
    Foundation::{GetHandleInformation, HANDLE_FLAG_INHERIT},
    System::Threading::{GetProcessId, TerminateProcess},
};

const TEST: &str = "account_state::execution::tests::paired::crash::paired_held_broker_fixture";
const ROOT: &str = "ALYSIS_TEST_PAIRED_CRASH_ROOT";
const NONCE: &str = "ALYSIS_TEST_PAIRED_CRASH_NONCE";

#[derive(Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub(super) struct Reference {
    pub(super) handle: u64,
    pub(super) pid: u32,
    pub(super) created: u64,
}
impl Reference {
    pub(super) fn original(handle: &OwnedHandle) -> Self {
        Self {
            handle: handle.raw().0 as usize as u64,
            pid: unsafe { GetProcessId(handle.raw()) },
            created: process_created(handle.raw()).unwrap(),
        }
    }
}
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Proof {
    nonce: String,
    point: String,
    execution: ExecutionRecord,
    plan: String,
    events: Vec<(u32, u32, String)>,
    owner: Reference,
    consumers: Reference,
    family: Vec<Reference>,
}
pub(super) fn rows(sql: &Connection) -> Vec<(u32, u32, String)> {
    sql.prepare("SELECT role,sequence,event_json FROM candidate_paired_bootstrap_events ORDER BY role,sequence").unwrap()
        .query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?))).unwrap()
        .collect::<std::result::Result<_, _>>().unwrap()
}
fn hold(
    fixture: &PairFixture,
    control: &DiskCrashControl,
    owner: &OwnedHandle,
    consumers: &OwnedHandle,
    family: &[OwnedHandle],
) -> ! {
    let sql = fixture.db.connection().unwrap();
    assert!(sql.is_autocommit());
    let proof = Proof {
        nonce: control.nonce.clone(),
        point: control.checkpoint.clone(),
        execution: fixture.active.record().unwrap().clone(),
        plan: sql
            .query_row(
                "SELECT plan_json FROM candidate_paired_bootstrap_plan",
                [],
                |r| r.get(0),
            )
            .unwrap(),
        events: rows(sql),
        owner: Reference::original(owner),
        consumers: Reference::original(consumers),
        family: family.iter().map(Reference::original).collect(),
    };
    let path = fixture._fixture.root.join("paired-checkpoint.part");
    std::fs::write(&path, serde_json::to_vec(&proof).unwrap()).unwrap();
    std::fs::rename(path, fixture._fixture.root.join("paired-checkpoint.json")).unwrap();
    std::thread::sleep(Duration::from_secs(20));
    panic!("controller did not terminate held paired broker");
}

#[test]
#[ignore = "fixed bounded trusted subprocess for paired broker-death qualification"]
fn paired_held_broker_fixture() {
    let root = PathBuf::from(std::env::var_os(ROOT).unwrap());
    assert_eq!(root.parent().unwrap(), crate::util::test_temp_dir());
    assert!(
        root.file_name()
            .unwrap()
            .to_str()
            .unwrap()
            .starts_with("alysis-execution-test-")
    );
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
    let bytes = std::fs::read(root.join("paired-control.json")).unwrap();
    assert!(bytes.len() < 65536);
    let control: DiskCrashControl = serde_json::from_slice(&bytes).unwrap();
    validate_random_id(&control.nonce).unwrap();
    assert_eq!(std::env::var(NONCE).unwrap(), control.nonce);
    assert!(matches!(
        control.checkpoint.as_str(),
        "both-suspended" | "both-ready" | "family-ready"
    ));
    let mut fixture = PairFixture::from_fixture(Fixture {
        root,
        setup: control.setup.clone(),
        preserve: true,
    });
    let owner_server = fixture.owner_server.take().unwrap();
    let consumer_server = fixture.consumer_server.take().unwrap();
    let plan = fixture.plan();
    let jobs = plan.create_jobs().unwrap();
    let owner = jobs.prepare_bootstrap(Role::Owner).unwrap();
    let consumers = if control.checkpoint == "family-ready" {
        jobs.prepare_family_fixture().unwrap()
    } else {
        jobs.prepare_bootstrap(Role::Consumers).unwrap()
    };
    let owner_ref = owner.fixture_readonly_observer().unwrap();
    let consumer_ref = consumers.fixture_readonly_observer().unwrap();
    if control.checkpoint == "both-suspended" {
        hold(&fixture, &control, &owner_ref, &consumer_ref, &[]);
    }
    let _owner = owner.start(owner_server, Duration::from_secs(5)).unwrap();
    let mut consumers = consumers
        .start(consumer_server, Duration::from_secs(5))
        .unwrap();
    let family = if control.checkpoint == "family-ready" {
        consumers.fixture_family_references().unwrap()
    } else {
        vec![]
    };
    hold(&fixture, &control, &owner_ref, &consumer_ref, &family);
}

pub(super) struct Broker(pub(super) std::process::Child);
impl Drop for Broker {
    fn drop(&mut self) {
        if self.0.try_wait().ok().flatten().is_none() {
            let _ = self.0.kill();
            let _ = self.0.wait();
        }
    }
}
pub(super) fn original_reference(
    broker: HANDLE,
    value: &Reference,
    image: &PinnedRunnerImage,
) -> OwnedHandle {
    assert!(value.handle > 0 && value.handle < isize::MAX as u64);
    let mut raw = HANDLE::default();
    // Interpret the test-only integer in the authenticated original broker's
    // table. The observer never opens or duplicates either job handle.
    unsafe {
        DuplicateHandle(
            broker,
            HANDLE(value.handle as usize as *mut _),
            GetCurrentProcess(),
            &mut raw,
            (PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SYNCHRONIZE).0,
            false,
            DUPLICATE_HANDLE_OPTIONS(0),
        )
        .unwrap();
    }
    let observed = OwnedHandle(raw);
    assert_eq!(unsafe { GetProcessId(raw) }, value.pid);
    assert_eq!(process_created(raw).unwrap(), value.created);
    let mut flags = 0;
    unsafe {
        GetHandleInformation(raw, &mut flags).unwrap();
    }
    assert_eq!(flags & HANDLE_FLAG_INHERIT.0, 0);
    assert!(unsafe { TerminateProcess(raw, 1) }.is_err());
    image.verify_process(raw).unwrap();
    alive(&observed);
    observed
}
pub(super) fn wait_absent(name: &str) {
    let end = Instant::now() + Duration::from_secs(5);
    while counts(name).is_some() {
        assert!(
            Instant::now() < end,
            "original paired job survived broker exit"
        );
        std::thread::sleep(Duration::from_millis(10));
    }
}

#[test]
fn paired_broker_death_closes_both_original_jobs_and_preserves_each_committed_prefix() {
    for (point, expected) in [
        ("both-suspended", 4),
        ("both-ready", 7),
        ("family-ready", 7),
    ] {
        let fixture = protected_fixture();
        let nonce = random_id().unwrap();
        std::fs::write(
            fixture.root.join("paired-control.json"),
            serde_json::to_vec(&DiskCrashControl {
                setup: fixture.setup.clone(),
                checkpoint: point.into(),
                nonce: nonce.clone(),
            })
            .unwrap(),
        )
        .unwrap();
        let stdout =
            std::fs::File::create_new(fixture.root.join("paired-broker.stdout.log")).unwrap();
        let stderr =
            std::fs::File::create_new(fixture.root.join("paired-broker.stderr.log")).unwrap();
        let mut broker = Broker(
            std::process::Command::new(std::env::current_exe().unwrap())
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
                .stdin(std::process::Stdio::null())
                .stdout(stdout)
                .stderr(stderr)
                .spawn()
                .unwrap(),
        );
        let original = HANDLE(broker.0.as_raw_handle());
        let created = process_created(original).unwrap();
        let end = Instant::now() + Duration::from_secs(10);
        let checkpoint = fixture.root.join("paired-checkpoint.json");
        while !checkpoint.exists() {
            assert!(
                broker.0.try_wait().unwrap().is_none(),
                "held paired broker exited; preserve logs"
            );
            assert!(
                Instant::now() < end,
                "paired checkpoint timeout; preserve logs"
            );
            std::thread::sleep(Duration::from_millis(10));
        }
        let bytes = std::fs::read(checkpoint).unwrap();
        assert!(bytes.len() < 32768);
        let proof: Proof = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(proof.nonce, nonce);
        assert_eq!(proof.point, point);
        assert_eq!(
            (proof.execution.pid, proof.execution.created),
            (broker.0.id(), created)
        );
        assert_eq!(proof.execution.scope, PAIRED_PLAN_SCOPE);
        assert_ne!(proof.owner.pid, proof.consumers.pid);
        for role in 0..2 {
            let events: Vec<_> = proof.events.iter().filter(|e| e.0 == role).collect();
            assert_eq!(events.len(), expected);
            assert!(
                events
                    .iter()
                    .enumerate()
                    .all(|(i, event)| event.1 as usize == i)
            );
        }
        let marker = std::fs::read(fixture.path()).unwrap();
        assert_eq!(
            serde_json::from_slice::<ExecutionRecord>(&marker).unwrap(),
            proof.execution
        );
        let image = PinnedRunnerImage::fixture(&std::env::current_exe().unwrap()).unwrap();
        let owner = original_reference(original, &proof.owner, &image);
        let consumers = original_reference(original, &proof.consumers, &image);
        assert_eq!(
            proof.family.len(),
            if point == "family-ready" { 2 } else { 0 }
        );
        let family: Vec<_> = proof
            .family
            .iter()
            .map(|r| original_reference(original, r, &image))
            .collect();
        let mut pids = vec![proof.owner.pid, proof.consumers.pid];
        pids.extend(proof.family.iter().map(|r| r.pid));
        let count = pids.len();
        pids.sort();
        pids.dedup();
        assert_eq!(pids.len(), count);
        let retirement_deadline = Instant::now() + Duration::from_secs(10);
        unsafe {
            TerminateProcess(original, 74).unwrap();
        }
        assert_eq!(
            unsafe { WaitForSingleObject(original, 5000) },
            WAIT_OBJECT_0
        );
        assert_eq!(broker.0.wait().unwrap().code(), Some(74));
        dead(&owner);
        dead(&consumers);
        for process in &family {
            let code = exited_code(process);
            eprintln!("paired broker-death family original exit code={code}");
            assert_ne!(code, 91, "family watchdog is not broker-death containment");
            // KILL_ON_JOB_CLOSE is observed to report 0 on this host. The fixed
            // ready fixture cannot return normally: its only voluntary exit is
            // the rejected watchdog91, after30s. Original live references, the
            // broker termination and the10s deadline establish this crash cut.
        }
        assert!(Instant::now() < retirement_deadline);
        drop(owner);
        drop(consumers);
        drop(family);
        for consumer in [false, true] {
            wait_absent(
                &crate::job::paired_execution_name(&proof.execution.identity.container, consumer)
                    .unwrap(),
            );
        }
        // Reopening evidence after death does not construct either live plan or
        // consumer completion. Neither scope nor the pending marker is retired.
        let db = protected_database(&fixture);
        assert_eq!(db.file_identity(), proof.execution.database_id);
        let sql = db.connection().unwrap();
        let retained: String = sql
            .query_row(
                "SELECT plan_json FROM candidate_paired_bootstrap_plan",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(retained, proof.plan);
        assert_eq!(rows(sql), proof.events);
        assert_eq!(std::fs::read(fixture.path()).unwrap(), marker);
    }
}
