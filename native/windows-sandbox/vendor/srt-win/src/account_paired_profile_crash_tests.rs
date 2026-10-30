//! Disposable-host abrupt cuts in the original paired profile lifetime.
//! The controller pins original roles before resume, and writing descendants live.
//! Interrupted receipts remain evidence; only copied bytes enter a rollback oracle.
use super::crash::{Broker, Reference, original_reference, rows, wait_absent};
use super::*;
use paired_plan::{
    PROFILE_JOURNAL_CHECKPOINT,
    native::account::{ACCOUNT_CRASH_CHECKPOINT, profile::PROFILE_CHECKPOINT},
};
use std::{
    io::{Read, Write},
    process::{Command, Stdio},
    time::Instant,
};
use windows::Win32::System::Threading::TerminateProcess;

#[path = "account_paired_profile_crash_controller.rs"]
mod controller;
pub(super) use controller::{run, run_v3, run_v3_shadow};

const TEST: &str =
    "account_state::execution::tests::atomic_account_birth::hosted::paired_profile_held_broker";
const V3_TEST: &str = "account_state::execution::tests::atomic_account_birth::hosted::paired_rpc_v3_profile_held_broker";
const V3_SHADOW_TEST: &str = "account_state::execution::tests::atomic_account_birth::hosted::paired_rpc_v3_shadow_profile_held_broker";
const ROOT: &str = "ALYSIS_TEST_PAIRED_PROFILE_CRASH_ROOT";
const NONCE: &str = "ALYSIS_TEST_PAIRED_PROFILE_CRASH_NONCE";
use crate::account_state::execution::resources::FixtureRpcPolicy as PolicyKind;
use crate::account_state::execution::resources::namespace_native::RootBirthKind;
// Chosen by distinct source entrypoints, never disk/environment/report selection.
#[derive(Clone, Copy)]
enum Lifecycle {
    Baseline,
    V3,
    V3Shadow,
}
impl Lifecycle {
    fn policy(self) -> PolicyKind {
        match self {
            Self::Baseline => PolicyKind::BaselineV1,
            Self::V3 | Self::V3Shadow => crate::account_rpc_v3_lifecycle_fixture::POLICY,
        }
    }
    fn birth(self) -> RootBirthKind {
        match self {
            Self::Baseline | Self::V3 => RootBirthKind::LegacyUnspecified,
            Self::V3Shadow => RootBirthKind::SessionShadowDirectoryV1,
        }
    }
    fn broker_test(self) -> &'static str {
        match self {
            Self::Baseline => TEST,
            Self::V3 => V3_TEST,
            Self::V3Shadow => V3_SHADOW_TEST,
        }
    }
    fn require(self, point: &str) -> Result<()> {
        ensure!(POINTS.contains(&point), "unknown profile crash cut");
        match self {
            Self::Baseline => (),
            Self::V3 => crate::account_rpc_v3_lifecycle_fixture::require_ordinary_live()?,
            Self::V3Shadow => crate::account_rpc_v3_lifecycle_fixture::require_shadow_live()?,
        }
        if !matches!(self, Self::Baseline) {
            ensure!(initialized(point), "V3 requires an initialized writer cut");
        }
        Ok(())
    }
}
const POINTS: [&str; 10] = [
    "paired-profile-created-hot-receipt",
    "paired-profile-using",
    "paired-profile-captured-hot-receipt",
    "paired-profile-removed-hot-receipt",
    "writer-family-live-use",
    "writer-family-captured-hot-receipt",
    "writer-family-removed-hot-receipt",
    "initialized-writer-family-live-use",
    "initialized-writer-family-captured-hot-receipt",
    "initialized-writer-family-removed-hot-receipt",
];
const MAX_PROOF: usize = 65536;
const MAX_INITIALIZED_PROOF: usize = 131072;
fn proof_limit(expected_point: &str) -> Result<usize> {
    ensure!(
        POINTS.contains(&expected_point),
        "unknown profile proof point"
    );
    // Only the three initialized diagnostics carry the five additional full
    // resource prefixes. This never widens the control frame or any transport.
    Ok(if initialized(expected_point) {
        MAX_INITIALIZED_PROOF
    } else {
        MAX_PROOF
    })
}
fn writer(point: &str) -> bool {
    POINTS[4..].contains(&point)
}
fn initialized(point: &str) -> bool {
    POINTS[7..].contains(&point)
}
fn live_writer(point: &str) -> bool {
    point == POINTS[4] || point == POINTS[7]
}
fn captured_writer(point: &str) -> bool {
    point == POINTS[5] || point == POINTS[8]
}
fn removed_writer(point: &str) -> bool {
    point == POINTS[6] || point == POINTS[9]
}
fn created(point: &str) -> bool {
    point == POINTS[0]
}
fn hot(point: &str) -> bool {
    point != POINTS[1] && !live_writer(point)
}
fn retired(point: &str, stage: &str) -> bool {
    if writer(point) {
        matches!(stage, "armed" | "cut") && !live_writer(point)
    } else {
        stage != "observe" && POINTS[2..4].contains(&point)
    }
}
fn before_phase(point: &str) -> &'static str {
    match point {
        "paired-profile-created-hot-receipt" => "before-create",
        "paired-profile-using" => "before-use",
        "paired-profile-captured-hot-receipt" => "before-capture",
        "paired-profile-removed-hot-receipt" => "before-remove",
        "writer-family-live-use" | "initialized-writer-family-live-use" => "",
        "writer-family-captured-hot-receipt" | "initialized-writer-family-captured-hot-receipt" => {
            "before-capture"
        }
        "writer-family-removed-hot-receipt" | "initialized-writer-family-removed-hot-receipt" => {
            "before-remove"
        }
        _ => panic!("unknown paired profile cut"),
    }
}
fn counts_for(point: &str, stage: &str) -> (usize, [usize; 2], usize) {
    assert!(POINTS.contains(&point));
    assert!(["observe", "armed", "cut"].contains(&stage) || (writer(point) && stage == "family"));
    if writer(point) {
        let profile = match stage {
            "observe" => 4,
            "family" => 6,
            "armed" => {
                if removed_writer(point) {
                    8
                } else {
                    6
                }
            }
            "cut" => {
                if live_writer(point) {
                    6
                } else if captured_writer(point) {
                    7
                } else {
                    9
                }
            }
            _ => unreachable!(),
        };
        return (
            profile,
            [7, if retired(point, stage) { 9 } else { 7 }],
            if stage == "observe" {
                0
            } else if retired(point, stage) {
                3
            } else {
                2
            },
        );
    }
    let profile = match stage {
        "observe" => {
            if created(point) {
                2
            } else {
                4
            }
        }
        "armed" => match point {
            p if p == POINTS[0] => 2,
            p if p == POINTS[1] => 4,
            p if p == POINTS[2] => 6,
            _ => 8,
        },
        _ => match point {
            p if p == POINTS[0] => 3,
            p if p == POINTS[1] => 6,
            p if p == POINTS[2] => 7,
            _ => 9,
        },
    };
    (
        profile,
        [
            7,
            if created(point) {
                2
            } else if retired(point, stage) {
                9
            } else {
                7
            },
        ],
        if retired(point, stage) { 3 } else { 0 },
    )
}
fn resource_counts_for(point: &str) -> [usize; 5] {
    assert!(POINTS.contains(&point));
    if initialized(point) {
        [2, 3, 2, 2, 5]
    } else {
        [0; 5]
    }
}

#[derive(Clone, Serialize, Deserialize, PartialEq, Debug)]
#[serde(deny_unknown_fields)]
struct Snapshot {
    plan: String,
    bootstrap: Vec<(u32, u32, String)>,
    account: Vec<(u32, u32, String)>,
    resume: Vec<(u32, u32, String)>,
    profile: Vec<(u32, u32, String)>,
    namespace: Vec<(u32, String)>,
    host_profile: Vec<(u32, String)>,
    station: Vec<(u32, String)>,
    desktop: Vec<(u32, String)>,
    initializer: Vec<(u32, String)>,
}
impl Snapshot {
    fn read(sql: &Connection) -> Self {
        assert!(sql.is_autocommit());
        let read = |query| {
            sql.prepare(query)
                .unwrap()
                .query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))
                .unwrap()
                .collect::<std::result::Result<_, _>>()
                .unwrap()
        };
        let resource = |query| {
            sql.prepare(query)
                .unwrap()
                .query_map([], |r| Ok((r.get(0)?, r.get(1)?)))
                .unwrap()
                .collect::<std::result::Result<_, _>>()
                .unwrap()
        };
        Self {
            plan: sql
                .query_row(
                    "SELECT plan_json FROM candidate_paired_bootstrap_plan",
                    [],
                    |r| r.get(0),
                )
                .unwrap(),
            bootstrap: rows(sql),
            account: read(
                "SELECT role,sequence,event_json FROM candidate_paired_account_events ORDER BY role,sequence",
            ),
            resume: read(
                "SELECT role,sequence,event_json FROM candidate_paired_account_resume_events ORDER BY role,sequence",
            ),
            profile: read(
                "SELECT role,sequence,event_json FROM candidate_paired_profile_events ORDER BY role,sequence",
            ),
            namespace: resource(
                "SELECT sequence,event_json FROM candidate_paired_namespace ORDER BY sequence",
            ),
            host_profile: resource(
                "SELECT sequence,event_json FROM candidate_paired_host_profile ORDER BY sequence",
            ),
            station: resource(
                "SELECT sequence,event_json FROM candidate_paired_station ORDER BY sequence",
            ),
            desktop: resource(
                "SELECT sequence,event_json FROM candidate_paired_desktop ORDER BY sequence",
            ),
            initializer: resource(
                "SELECT sequence,event_json FROM candidate_paired_initializer ORDER BY sequence",
            ),
        }
    }
    fn require_resources(
        &self,
        point: &str,
        expected_policy: PolicyKind,
        expected_birth: RootBirthKind,
    ) {
        let typed_plan: paired_plan::Plan = serde_json::from_str(&self.plan).unwrap();
        typed_plan
            .fixture_require_rpc_policy(expected_policy)
            .unwrap();
        // Both identities come from the source entrypoint, never the proof.
        typed_plan
            .fixture_require_namespace_birth(expected_birth)
            .unwrap();
        let tables = [
            &self.namespace,
            &self.host_profile,
            &self.station,
            &self.desktop,
            &self.initializer,
        ];
        assert_eq!(tables.map(|rows| rows.len()), resource_counts_for(point));
        if !initialized(point) {
            return;
        }
        // These complete raw prefixes are evidence only. Parsing never opens
        // an initializer process or reconstructs a live resource/cleanup guard.
        let parse = |rows: &[(u32, String)], kinds: &[&str]| -> Vec<serde_json::Value> {
            assert_eq!(rows.len(), kinds.len());
            rows.iter()
                .enumerate()
                .map(|(i, (sequence, raw))| {
                    assert_eq!(*sequence as usize, i);
                    let event: serde_json::Value = serde_json::from_str(raw).unwrap();
                    assert_eq!(event["kind"], kinds[i]);
                    event
                })
                .collect()
        };
        let namespace = parse(&self.namespace, &["intent", "created"]);
        let birth = namespace[0]["binding"]
            .get("birth_kind")
            .map(|v| {
                serde_json::from_value::<super::super::resources::namespace_native::RootBirthKind>(
                    v.clone(),
                )
                .unwrap()
            })
            .unwrap_or_default();
        assert_eq!(birth, expected_birth);
        let host = parse(&self.host_profile, &["intent", "created", "use_intent"]);
        let station = parse(&self.station, &["intent", "applied"]);
        let desktop = parse(&self.desktop, &["intent", "created"]);
        let initializer = parse(
            &self.initializer,
            &["intent", "born", "stopped", "rpc_intent", "rpc_granted"],
        );
        let plan: serde_json::Value = serde_json::from_str(&self.plan).unwrap();
        for (events, policy) in [(&namespace, 2), (&host, 1), (&station, 1), (&desktop, 1)] {
            let binding = &events[0]["binding"];
            assert_eq!(binding["operation"], plan["operation"]);
            assert_eq!(
                binding["package_sid"],
                plan["execution"]["identity"]["package_sid"]
            );
            assert_eq!(binding["policy"], policy);
            for event in events {
                assert_eq!(&event["binding"], binding);
            }
        }
        let ns = &namespace[0]["binding"];
        let hp = &host[0]["binding"];
        let st = &station[0]["binding"];
        let dt = &desktop[0]["binding"];
        for binding in [ns, st, dt] {
            assert_eq!(binding["session"], plan["session"]);
        }
        for binding in [hp, st, dt] {
            assert_eq!(binding["namespace"], ns["nonce"]);
        }
        assert_eq!(hp["owner"], plan["execution"]["owner"]);
        assert_eq!(hp["container"], plan["execution"]["identity"]["container"]);
        let folder: [u8; 24] = serde_json::from_value(host[1]["folder_id"].clone()).unwrap();
        assert_ne!(folder, [0; 24]);
        assert_eq!(host[2]["folder_id"], host[1]["folder_id"]);
        let profile = serde_json::json!({"birth": hp["nonce"], "use_nonce": host[2]["nonce"]});
        assert_eq!(st["profile"], profile);
        assert_eq!(dt["profile"], profile);
        assert_eq!(dt["station_nonce"], st["nonce"]);
        assert_eq!(
            dt["name"],
            format!(
                "alysis-paired-desktop-{}",
                plan["operation"].as_str().unwrap()
            )
        );
        let station_before: crate::acl::object_grants::Snapshot =
            serde_json::from_value(station[0]["before"].clone()).unwrap();
        let station_after: crate::acl::object_grants::Snapshot =
            serde_json::from_value(station[0]["after"].clone()).unwrap();
        assert_eq!(
            crate::account_state::execution::resources::station::proposed(
                &station_before,
                st["package_sid"].as_str().unwrap()
            )
            .unwrap(),
            station_after
        );

        let request = &initializer[0]["request"];
        let typed_request: crate::account_control::bootstrap::entry::initializer_wire::paired::Request =
            serde_json::from_value(request.clone()).unwrap();
        typed_request.validate().unwrap();
        assert!(!typed_request.subject.omit_profile_environment);
        let subject = &request["subject"];
        let resources = &request["resources"];
        assert_eq!(subject["operation"], plan["operation"]);
        assert_eq!(subject["owner"], plan["execution"]["owner"]);
        assert_eq!(subject["session"], plan["session"]);
        assert_eq!(subject["container"], hp["container"]);
        assert_eq!(subject["package_sid"], ns["package_sid"]);
        let original_consumer: serde_json::Value = serde_json::from_str(
            &self
                .bootstrap
                .iter()
                .find(|r| r.0 == 1 && r.1 == 3)
                .unwrap()
                .2,
        )
        .unwrap();
        assert_eq!(subject["bootstrap_pid"], original_consumer["pid"]);
        assert_eq!(subject["bootstrap_created"], original_consumer["created"]);
        for role in 0..2 {
            let ready: serde_json::Value = serde_json::from_str(
                &self
                    .bootstrap
                    .iter()
                    .find(|r| r.0 == role && r.1 == 6)
                    .unwrap()
                    .2,
            )
            .unwrap();
            assert_eq!(ready["kind"], "ready");
        }
        assert_eq!(resources["owner_job"], plan["roles"][0]["job_nonce"]);
        assert_eq!(resources["consumer_job"], plan["roles"][1]["job_nonce"]);
        assert_eq!(resources["namespace"], ns["nonce"]);
        assert_eq!(resources["host_profile"], hp["nonce"]);
        assert_eq!(resources["host_profile_use"], host[2]["nonce"]);
        assert_eq!(resources["station"], st["nonce"]);
        assert_eq!(resources["desktop"], dt["nonce"]);
        // The typed request rejects collisions among its resource phases;
        // additionally reject aliases with either role's other plan phases.
        for nonce in [
            &subject["nonce"],
            &ns["nonce"],
            &hp["nonce"],
            &host[2]["nonce"],
            &st["nonce"],
            &dt["nonce"],
        ] {
            let nonce = nonce.as_str().unwrap();
            validate_random_id(nonce).unwrap();
            assert_ne!(nonce, plan["execution"]["transaction"].as_str().unwrap());
            for role in plan["roles"].as_array().unwrap() {
                for phase in ["birth_nonce", "resume_nonce", "retire_nonce"] {
                    assert_ne!(nonce, role[phase].as_str().unwrap());
                }
                let control = role["control_nonce"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|b| format!("{:02x}", b.as_u64().unwrap()))
                    .collect::<String>();
                assert_ne!(nonce, control);
            }
        }
        for event in &initializer {
            assert_eq!(&event["request"], request);
        }
        assert_eq!(initializer[1]["birth"], initializer[2]["birth"]);
        let birth = &initializer[1]["birth"];
        assert_ne!(birth["pid"].as_u64().unwrap(), 0);
        assert_ne!(birth["pid"], subject["bootstrap_pid"]);
        assert_ne!(birth["pid"], plan["execution"]["pid"]);
        assert!(
            birth["created"].as_u64().unwrap() >= subject["bootstrap_created"].as_u64().unwrap()
        );
        assert_eq!(initializer[3]["policy"], initializer[4]["policy"]);
        let policy: crate::account_state::execution::resources::namespace_native::rpc::AccessPolicy =
            serde_json::from_value(initializer[3]["policy"].clone()).unwrap();
        policy.validate(&typed_plan).unwrap();
    }
    fn require(
        &self,
        point: &str,
        stage: &str,
        expected_policy: PolicyKind,
        expected_birth: RootBirthKind,
    ) {
        self.require_resources(point, expected_policy, expected_birth);
        let (profile_count, parents, resume_count) = counts_for(point, stage);
        let parse = |rows: &[(u32, u32, String)], role, kinds: &[&str]| -> Vec<serde_json::Value> {
            let rows: Vec<_> = rows.iter().filter(|r| r.0 == role).collect();
            assert_eq!(rows.len(), kinds.len());
            rows.iter()
                .enumerate()
                .map(|(i, r)| {
                    assert_eq!(r.1 as usize, i);
                    let value: serde_json::Value = serde_json::from_str(&r.2).unwrap();
                    assert_eq!(value["kind"], kinds[i]);
                    value
                })
                .collect()
        };
        let parent_kinds = [
            "job_intent",
            "job_created",
            "birth_intent",
            "born",
            "resume_intent",
            "resumed",
            "ready",
            "retire_intent",
            "retired",
        ];
        let account_kinds = [
            "intent",
            "delivered",
            "birth_intent",
            "born",
            "acknowledged",
        ];
        let mut accounts = Vec::new();
        for role in 0..2 {
            parse(
                &self.bootstrap,
                role,
                &parent_kinds[..parents[role as usize]],
            );
            accounts.push(parse(
                &self.account,
                role,
                &account_kinds[..if role == 1 && created(point) { 0 } else { 5 }],
            ));
        }
        assert_eq!(self.bootstrap.len(), parents.iter().sum::<usize>());
        assert_eq!(self.account.len(), if created(point) { 5 } else { 10 });
        let profile = parse(
            &self.profile,
            0,
            &[
                "resume_intent",
                "resumed",
                "create_intent",
                "created",
                "use_intent",
                "using",
                "capture_intent",
                "captured",
                "remove_intent",
            ][..profile_count],
        );
        assert_eq!(self.profile.len(), profile_count);
        let owner = &profile[0]["request"];
        assert_eq!(owner["birth"], accounts[0][4]["request"]);
        assert_eq!(owner["pid"], accounts[0][4]["pid"]);
        assert_eq!(owner["created"], accounts[0][4]["created"]);
        assert_eq!(
            owner["birth"]["purpose"]["trusted_paired_profile_v1"]["policy"],
            1
        );
        for event in &profile {
            assert_eq!(&event["request"], owner);
        }
        for event in profile.iter().skip(4) {
            assert_eq!(event["folder"], profile[3]["folder"]);
            assert_eq!(event["consumer"], accounts[1][4]["request"]);
            assert_eq!(event["nonce"], profile[4]["nonce"]);
            if let Some(retirement) = event.get("retirement") {
                let plan: serde_json::Value = serde_json::from_str(&self.plan).unwrap();
                assert_eq!(retirement, &plan["roles"][1]["retire_nonce"]);
            }
        }
        let resumed = parse(
            &self.resume,
            1,
            &["intent", "resumed", "exited"][..resume_count],
        );
        assert_eq!(self.resume.len(), resume_count);
        if resume_count > 0 {
            assert_eq!(resumed[0]["request"]["birth"], accounts[1][4]["request"]);
            if writer(point) {
                let binding = &resumed[0]["request"]["birth"]["purpose"]["fixture_profile_writer_v1"]
                    ["binding"];
                assert_eq!(binding["descendant_files"], true);
                assert_eq!(binding["hold_rounds"], 12);
                assert_eq!(binding["unsupported_registry"], false);
                assert_eq!(binding["owner_birth"], owner["birth"]["nonce"]);
                assert_eq!(binding["owner_resume"], owner["nonce"]);
                assert_eq!(binding["owner_pid"], owner["pid"]);
                assert_eq!(binding["owner_created"], owner["created"]);
                assert_eq!(binding["folder"], profile[3]["folder"]);
                assert_eq!(binding["use_nonce"], profile[4]["nonce"]);
                assert_eq!(
                    binding["container"],
                    owner["birth"]["purpose"]["trusted_paired_profile_v1"]["container"]
                );
                assert_eq!(
                    binding["package_sid"],
                    owner["birth"]["purpose"]["trusted_paired_profile_v1"]["package_sid"]
                );
            } else {
                assert_eq!(
                    resumed[0]["request"]["birth"]["purpose"],
                    "trusted_self_check_v1"
                );
            }
            if resume_count == 3 {
                assert_eq!(resumed[2]["code"], 0x4150);
            }
            for event in &resumed {
                assert_eq!(event["request"], resumed[0]["request"]);
            }
        }
    }
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Proof {
    nonce: String,
    point: String,
    stage: String,
    execution: ExecutionRecord,
    snapshot: Snapshot,
    processes: Vec<(String, Reference)>,
    consumer_suspended_tick: Option<u64>,
    writer_receipt: Option<serde_json::Value>,
    progress_tick: Option<u64>,
    writer_acknowledged_rounds: u32,
}
struct Held {
    expected_policy: PolicyKind,
    expected_birth: RootBirthKind,
    root: PathBuf,
    nonce: String,
    point: String,
    execution: ExecutionRecord,
    processes: Vec<(String, OwnedHandle)>,
    armed: bool,
    consumer_suspended_tick: Option<u64>,
    writer_receipt: Option<serde_json::Value>,
    progress_tick: Option<u64>,
}
fn require_state(point: &str, stage: &str, kind: &str, handle: &OwnedHandle) {
    if retired(point, stage) && kind.starts_with("consumer-") {
        if kind == "consumer-account" {
            assert_eq!(exited_code(handle), 0x4150);
        } else if kind == "consumer-child" || kind == "consumer-grandchild" {
            assert_eq!(exited_code(handle), 1);
        } else {
            dead(handle);
        }
    } else {
        alive(handle);
    }
}
impl Held {
    fn publish(&self, snapshot: Snapshot, stage: &str) {
        snapshot.require(
            &self.point,
            stage,
            self.expected_policy,
            self.expected_birth,
        );
        for (kind, handle) in &self.processes {
            require_state(&self.point, stage, kind, handle);
        }
        let proof = Proof {
            nonce: self.nonce.clone(),
            point: self.point.clone(),
            stage: stage.into(),
            execution: self.execution.clone(),
            snapshot,
            processes: self
                .processes
                .iter()
                .map(|(k, h)| (k.clone(), Reference::original(h)))
                .collect(),
            consumer_suspended_tick: self.consumer_suspended_tick,
            writer_receipt: self.writer_receipt.clone(),
            progress_tick: self.progress_tick,
            writer_acknowledged_rounds: crate::account_control::writer_fixture::PROGRESS
                .with(|s| s.get()),
        };
        let bytes = serde_json::to_vec(&proof).unwrap();
        let limit = proof_limit(&self.point).unwrap();
        eprintln!(
            "paired profile proof bytes: point={} stage={stage} bytes={} limit={limit}",
            self.point,
            bytes.len()
        );
        assert!(bytes.len() < limit);
        let destination = self.root.join(format!("paired-profile-{stage}.json"));
        assert!(!destination.exists());
        let part = destination.with_extension("part");
        std::fs::write(&part, bytes).unwrap();
        std::fs::rename(part, destination).unwrap();
    }
    fn acknowledge(&self, stage: &str) {
        let path = self.root.join(format!("paired-profile-{stage}-ack.json"));
        let deadline = Instant::now() + Duration::from_secs(3);
        while !path.exists() {
            assert!(
                Instant::now() < deadline,
                "profile controller acknowledgment timed out"
            );
            std::thread::sleep(Duration::from_millis(10));
        }
        assert_eq!(std::fs::read(path).unwrap(), self.nonce.as_bytes());
        for (kind, handle) in &self.processes {
            require_state(&self.point, stage, kind, handle);
        }
    }
}

/// Admission and the stdin-only synthetic credential belong to the hosted entry.
pub(super) fn broker(password: &str) {
    broker_inner(password, Lifecycle::Baseline)
}
pub(super) fn broker_v3(password: &str) {
    crate::account_rpc_v3_lifecycle_fixture::require_ordinary_live().unwrap();
    broker_inner(password, Lifecycle::V3)
}
pub(super) fn broker_v3_shadow(password: &str) {
    crate::account_rpc_v3_lifecycle_fixture::require_shadow_live().unwrap();
    broker_inner(password, Lifecycle::V3Shadow)
}
fn broker_inner(password: &str, lifecycle: Lifecycle) {
    assert_eq!(
        std::env::args().skip(1).collect::<Vec<_>>(),
        [
            lifecycle.broker_test(),
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
    let mut bytes = Vec::new();
    std::fs::File::open(root.join("paired-profile-control.json"))
        .unwrap()
        .take(MAX_PROOF as u64)
        .read_to_end(&mut bytes)
        .unwrap();
    assert!(bytes.len() < MAX_PROOF);
    let control: DiskCrashControl = serde_json::from_slice(&bytes).unwrap();
    validate_random_id(&control.nonce).unwrap();
    assert_eq!(std::env::var(NONCE).unwrap(), control.nonce);
    lifecycle.require(&control.checkpoint).unwrap();
    let mut fixture = PairFixture::from_fixture_image(
        Fixture {
            root,
            setup: control.setup,
            preserve: true,
        },
        true,
    );
    let owner = fixture.owner_server.take().unwrap();
    let consumer = fixture.consumer_server.take().unwrap();
    let plan = match lifecycle {
        Lifecycle::Baseline => fixture.plan(),
        Lifecycle::V3 => fixture.plan_rpc_v3_lifecycle(),
        Lifecycle::V3Shadow => fixture.plan_rpc_v3_shadow_lifecycle(),
    };
    let jobs = plan.create_jobs().unwrap();
    let held = Rc::new(RefCell::new(Held {
        expected_policy: lifecycle.policy(),
        expected_birth: lifecycle.birth(),
        root: fixture._fixture.root.clone(),
        nonce: control.nonce,
        point: control.checkpoint,
        execution: fixture.active.record().unwrap().clone(),
        processes: Vec::new(),
        armed: false,
        consumer_suspended_tick: None,
        writer_receipt: None,
        progress_tick: None,
    }));
    let retained = held.clone();
    NATIVE_CHECKPOINT.with(|slot| {
        *slot.borrow_mut() = Some(Box::new(move |phase, role, _, raw| {
            if phase == "bootstrap-born" {
                let mut held = retained.borrow_mut();
                if role == Role::Consumers {
                    assert!(held.consumer_suspended_tick.is_none());
                    // Before this original suspended bootstrap can start any
                    // receive. Shared monotonic uptime also bounds the Using
                    // cut's older consumer resume wait in the controller.
                    held.consumer_suspended_tick = Some(unsafe {
                        windows::Win32::System::SystemInformation::GetTickCount64()
                    });
                }
                held.processes.push((
                    if role == Role::Owner {
                        "owner-bootstrap"
                    } else {
                        "consumer-bootstrap"
                    }
                    .into(),
                    retain_original(raw.unwrap()),
                ));
            }
        }))
    });
    let retained = held.clone();
    ACCOUNT_CRASH_CHECKPOINT.with(|slot| {
        *slot.borrow_mut() = Some(Box::new(move |phase, role, _, observer| {
            if phase == "acknowledged" {
                let (raw, _, _) = observer.fixture_source_identity().unwrap();
                retained.borrow_mut().processes.push((
                    if role == Role::Owner {
                        "owner-account"
                    } else {
                        "consumer-account"
                    }
                    .into(),
                    retain_original(HANDLE(raw as *mut _)),
                ));
            }
        }))
    });
    let retained = held.clone();
    crate::account_control::writer_fixture::FAMILY_OBSERVED.with(|slot| {
        *slot.borrow_mut() = Some(Box::new(move |raw| {
            let mut held = retained.borrow_mut();
            assert!(writer(&held.point));
            let kind = match held.processes.len() {
                4 => "consumer-child",
                5 => "consumer-grandchild",
                _ => panic!("writer family reference count differs"),
            };
            // These are broker-local originals, never Offer's bootstrap-local
            // integer handles. Controller duplication uses only this table.
            held.processes.push((kind.into(), retain_original(raw)));
        }));
    });
    let retained = held.clone();
    crate::account_control::writer_fixture::FAMILY_PROGRESS.with(|slot| {
        *slot.borrow_mut() = Some(Box::new(move |round, sql, request, offer, tick| {
            if round != 1 {
                return;
            }
            let mut held = retained.borrow_mut();
            assert!(writer(&held.point) && held.processes.len() == 6);
            assert!(held.writer_receipt.is_none() && held.progress_tick.is_none());
            held.writer_receipt = Some(offer.fixture_proof(request).unwrap());
            held.progress_tick = Some(tick);
            held.publish(Snapshot::read(sql), "family");
            held.acknowledge("family");
            if live_writer(&held.point) {
                assert!(!held.armed);
                held.publish(Snapshot::read(sql), "armed");
                held.acknowledge("armed");
                held.armed = true;
                held.publish(Snapshot::read(sql), "cut");
                // The consumer progress ACK remains withheld. The controller
                // must kill within 3s of the unchanged pre-receive clock.
                std::thread::sleep(Duration::from_secs(30));
                std::process::exit(91);
            }
        }));
    });
    let retained = held.clone();
    PROFILE_CHECKPOINT.with(|slot| {
        *slot.borrow_mut() = Some(Box::new(move |phase, sql| {
            let mut held = retained.borrow_mut();
            let observe = if created(&held.point) {
                "before-create"
            } else {
                "before-use"
            };
            if phase == observe {
                held.publish(Snapshot::read(sql), "observe");
                held.acknowledge("observe");
            }
            if phase == before_phase(&held.point) {
                assert!(!held.armed);
                held.publish(Snapshot::read(sql), "armed");
                held.acknowledge("armed");
                held.armed = true;
            }
        }))
    });
    let mut before = None;
    let is_writer = writer(&held.borrow().point);
    let is_initialized = initialized(&held.borrow().point);
    PROFILE_JOURNAL_CHECKPOINT.with(|slot| {
        *slot.borrow_mut() = Some(Box::new(move |phase, seq, sql| {
            let held = held.borrow();
            if live_writer(&held.point) {
                return;
            }
            let target = match held.point.as_str() {
                p if p == POINTS[0] => 3,
                p if p == POINTS[1] => 5,
                p if p == POINTS[2] || captured_writer(p) => 7,
                _ => 9,
            };
            let path =
                crate::account_state::database_path(&held.root, &held.execution.identity.binding)
                    .unwrap();
            if hot(&held.point) && phase == "committed" && seq == target - 1 {
                assert!(held.armed);
                let bytes = std::fs::read(&path).unwrap();
                assert!(bytes.len() <= 1024 * 1024);
                std::fs::write(held.root.join("paired-profile-before-write.db"), bytes).unwrap();
                before = Some(Snapshot::read(sql));
            }
            if seq != target
                || phase
                    != if hot(&held.point) {
                        "inserted"
                    } else {
                        "committed"
                    }
            {
                return;
            }
            assert!(held.armed);
            let snapshot = if hot(&held.point) {
                assert!(!sql.is_autocommit());
                sql.cache_flush().unwrap();
                let journal = std::fs::read(path.with_extension("db-journal")).unwrap();
                assert!(journal.len() > 512 && journal.len() <= 1024 * 1024);
                assert_eq!(
                    &journal[..8],
                    &[0xd9, 0xd5, 0x05, 0xf9, 0x20, 0xa1, 0x63, 0xd7]
                );
                assert_ne!(
                    std::fs::read(path).unwrap(),
                    std::fs::read(held.root.join("paired-profile-before-write.db")).unwrap()
                );
                before.take().unwrap()
            } else {
                Snapshot::read(sql)
            };
            held.publish(snapshot, "cut");
            std::thread::sleep(Duration::from_secs(30));
            std::process::exit(91);
        }))
    });
    if is_initialized {
        jobs.with_empty_namespace()
            .unwrap()
            .with_host_profile_for_use()
            .unwrap()
            .with_station()
            .unwrap()
            .with_unused_desktop()
            .unwrap()
            .fixture_run_initialized_profile_lifetime(
                owner,
                consumer,
                Duration::from_secs(10),
                password,
            )
            .unwrap();
    } else if is_writer {
        jobs.fixture_run_profile_lifetime(
            owner,
            consumer,
            Duration::from_secs(10),
            password,
            0,
            true,
        )
        .unwrap();
    } else {
        jobs.fixture_run_profile_self_check(owner, consumer, Duration::from_secs(10), password)
            .unwrap();
    }
    panic!("profile crash checkpoint missed");
}
