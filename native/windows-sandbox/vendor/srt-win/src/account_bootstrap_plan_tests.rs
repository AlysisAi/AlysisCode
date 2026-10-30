//! Isolated disk SQLite mechanics. These tests create no account/profile,
//! process, ACL grant or live CommittedPlan, and cannot enable execution.
use super::*;
use crate::account_install::synthetic_ready;

pub(super) fn plan() -> Plan {
    let setup = synthetic_ready();
    let identity = CandidateJournalIdentity::new(
        setup.binding(AccountMode::Offline).unwrap(),
        format!("Alysis.Execution.{}", random_id().unwrap()),
    )
    .unwrap();
    let mut execution = fixture_record(identity);
    execution.scope = BOOTSTRAP_PLAN_SCOPE.into();
    Plan {
        format: FORMAT.into(),
        execution,
        operation: random_id().unwrap(),
        credential_generation: setup.offline.unwrap().credential_generation.unwrap(),
        image_id: [3; 24],
        session: 7,
        control_nonce: [4; 16],
    }
}

pub(super) struct Disk {
    connection: Option<Connection>,
    path: std::path::PathBuf,
}
impl Disk {
    pub(super) fn new(plan: &Plan) -> Self {
        let path =
            crate::util::test_temp_dir().join(format!("atomic-plan-{}.db", random_id().unwrap()));
        drop(
            std::fs::OpenOptions::new()
                .create_new(true)
                .write(true)
                .open(&path)
                .unwrap(),
        );
        let mut connection = Connection::open(&path).unwrap();
        connection
            .execute_batch("PRAGMA journal_mode=DELETE; PRAGMA synchronous=FULL;")
            .unwrap();
        crate::account_state::initialize_header(&mut connection, &plan.execution.identity.binding)
            .unwrap();
        Self {
            connection: Some(connection),
            path,
        }
    }
    pub(super) fn sql(&self) -> &Connection {
        self.connection.as_ref().unwrap()
    }
    pub(super) fn reopen(&mut self) {
        self.connection.take().unwrap().close().unwrap();
        self.connection = Some(Connection::open(&self.path).unwrap());
        durability(self.sql()).unwrap();
    }
    fn raw_plan(&self) -> String {
        self.sql()
            .query_row(
                "SELECT plan_json FROM candidate_atomic_bootstrap_plan",
                [],
                |r| r.get(0),
            )
            .unwrap()
    }
}
impl Drop for Disk {
    fn drop(&mut self) {
        drop(self.connection.take());
        if !std::thread::panicking() {
            std::fs::remove_file(&self.path).unwrap();
        }
    }
}

#[test]
fn atomic_plan_disk_reopen_preserves_identity_without_reconstructing_authority() {
    let plan = plan();
    let mut disk = Disk::new(&plan);
    persist(disk.sql(), &plan).unwrap();
    let committed = disk.raw_plan();
    // Ordinary trusted SQL mistakes are refused too; these triggers are not
    // the untrusted-writer security boundary (the production opener is).
    for sql in [
        "UPDATE candidate_atomic_bootstrap_plan SET plan_json='{}'",
        "DELETE FROM candidate_atomic_bootstrap_plan",
        "INSERT OR REPLACE INTO candidate_atomic_bootstrap_plan VALUES(1,'{}')",
    ] {
        assert!(disk.sql().execute_batch(sql).is_err());
    }
    assert!(persist(disk.sql(), &plan).is_err());
    disk.reopen();
    require_plan(disk.sql(), &plan).unwrap();
    assert!(persist(disk.sql(), &plan).is_err());
    assert_eq!(disk.raw_plan(), committed);
    // Readback returns () only. The parsed identity supplies no borrowed live
    // execution, image, process, job, resume or cleanup capability.
}

#[test]
fn atomic_plan_rejects_cross_operation_and_every_bound_identity_mismatch() {
    let plan = plan();
    let disk = Disk::new(&plan);
    persist(disk.sql(), &plan).unwrap();
    let committed = disk.raw_plan();
    let changes: Vec<Box<dyn Fn(&mut Plan)>> = vec![
        Box::new(|p| p.operation = random_id().unwrap()),
        Box::new(|p| p.credential_generation = "CredOwner-2123456789abcdef0123456789abcdef".into()),
        Box::new(|p| p.image_id[0] ^= 1),
        Box::new(|p| p.session += 1),
        Box::new(|p| p.control_nonce[0] ^= 1),
        Box::new(|p| p.execution.pid += 1),
        Box::new(|p| p.execution.created += 1),
        Box::new(|p| p.execution.transaction = random_id().unwrap()),
        Box::new(|p| p.execution.database_id[0] ^= 1),
        Box::new(|p| p.execution.pending_id[0] ^= 1),
        Box::new(|p| p.execution.identity.binding.account_sid.push_str("-1")),
        Box::new(|p| p.execution.identity.binding.mode = AccountMode::Online),
        Box::new(|p| {
            p.execution.identity.binding.installation_id = "2123456789abcdef0123456789abcdef".into()
        }),
        Box::new(|p| p.execution.owner = "S-1-5-18".into()),
        Box::new(|p| p.execution.job.push('x')),
        Box::new(|p| p.execution.identity.package_sid.push_str("-1")),
    ];
    for change in changes {
        let mut different = plan.clone();
        change(&mut different);
        assert!(require_plan(disk.sql(), &different).is_err());
        assert_eq!(disk.raw_plan(), committed);
    }
    require_plan(disk.sql(), &plan).unwrap();
}

#[test]
fn atomic_plan_rejects_legacy_scopes_versions_and_missing_bindings_before_sql() {
    let plan = plan();
    let disk = Disk::new(&plan);
    let changes: Vec<Box<dyn Fn(&mut Plan)>> = vec![
        Box::new(|p| p.format = "alysis-atomic-bootstrap-identity-v2".into()),
        Box::new(|p| p.execution.scope = SCOPE.into()),
        Box::new(|p| p.execution.scope = RESOURCE_SCOPE.into()),
        Box::new(|p| p.execution.version = 2),
        Box::new(|p| p.operation = "../invalid".into()),
        Box::new(|p| p.credential_generation = "CredOwner-../invalid".into()),
        Box::new(|p| p.image_id = [0; 24]),
        Box::new(|p| p.control_nonce = [0; 16]),
    ];
    for change in changes {
        let mut invalid = plan.clone();
        change(&mut invalid);
        assert!(persist(disk.sql(), &invalid).is_err());
        let count: i64 = disk
            .sql()
            .query_row(
                "SELECT count(*) FROM sqlite_schema WHERE tbl_name=?1",
                [TABLE],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(count, 0);
    }
}

#[test]
fn atomic_plan_preserves_malformed_unknown_and_oversized_records() {
    let plan = plan();
    let mut unknown = serde_json::to_value(&plan).unwrap();
    unknown["unexpected"] = true.into();
    let mut nested = serde_json::to_value(&plan).unwrap();
    nested["execution"]["unexpected"] = true.into();
    let mut missing = serde_json::to_value(&plan).unwrap();
    missing.as_object_mut().unwrap().remove("control_nonce");
    for invalid in [
        "{".into(),
        "{}".into(),
        unknown.to_string(),
        nested.to_string(),
        missing.to_string(),
        "x".repeat(MAX_BYTES + 1),
        format!(
            "{{\"format\":\"duplicate\",{}",
            &serde_json::to_string(&plan).unwrap()[1..]
        ),
    ] {
        let disk = Disk::new(&plan);
        for sql in SCHEMA {
            disk.sql().execute_batch(sql).unwrap();
        }
        lifecycle::create_schema(disk.sql()).unwrap();
        disk.sql()
            .execute(
                "INSERT INTO candidate_atomic_bootstrap_plan VALUES(1,?1)",
                [&invalid],
            )
            .unwrap();
        assert!(require_plan(disk.sql(), &plan).is_err());
        assert!(persist(disk.sql(), &plan).is_err());
        assert_eq!(disk.raw_plan(), invalid);
    }
}

#[test]
fn atomic_plan_refuses_empty_or_changed_schema_without_adoption() {
    let plan = plan();
    let disk = Disk::new(&plan);
    disk.sql().execute_batch(SCHEMA[0]).unwrap();
    assert!(persist(disk.sql(), &plan).is_err());
    assert!(require_plan(disk.sql(), &plan).is_err());
    let count: i64 = disk
        .sql()
        .query_row(
            "SELECT count(*) FROM candidate_atomic_bootstrap_plan",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(count, 0);
    let disk = Disk::new(&plan);
    persist(disk.sql(), &plan).unwrap();
    let committed = disk.raw_plan();
    disk.sql()
        .execute_batch("DROP TRIGGER atomic_plan_no_update")
        .unwrap();
    assert!(require_plan(disk.sql(), &plan).is_err());
    assert_eq!(disk.raw_plan(), committed);
    disk.sql().execute_batch(SCHEMA[1]).unwrap();
    require_plan(disk.sql(), &plan).unwrap();
    disk.sql()
        .execute_batch(
            "CREATE INDEX unexpected_atomic_index ON candidate_atomic_bootstrap_plan(plan_json)",
        )
        .unwrap();
    assert!(require_plan(disk.sql(), &plan).is_err());
    assert_eq!(disk.raw_plan(), committed);
}

#[test]
fn atomic_plan_refuses_transaction_and_durability_drift() {
    let plan = plan();
    let disk = Disk::new(&plan);
    for pragma in [
        "PRAGMA synchronous=OFF",
        "PRAGMA synchronous=NORMAL",
        "PRAGMA journal_mode=MEMORY",
    ] {
        disk.sql().execute_batch(pragma).unwrap();
        assert!(persist(disk.sql(), &plan).is_err());
        disk.sql()
            .execute_batch("PRAGMA journal_mode=DELETE; PRAGMA synchronous=FULL")
            .unwrap();
    }
    disk.sql().execute_batch("BEGIN IMMEDIATE").unwrap();
    assert!(persist(disk.sql(), &plan).is_err());
    disk.sql().execute_batch("ROLLBACK").unwrap();
    persist(disk.sql(), &plan).unwrap();
    let committed = disk.raw_plan();
    disk.sql().execute_batch("BEGIN").unwrap();
    assert!(require_plan(disk.sql(), &plan).is_err());
    disk.sql()
        .execute_batch("ROLLBACK; PRAGMA synchronous=NORMAL")
        .unwrap();
    assert!(require_plan(disk.sql(), &plan).is_err());
    assert_eq!(disk.raw_plan(), committed);
    disk.sql().execute_batch("PRAGMA synchronous=FULL").unwrap();
    require_plan(disk.sql(), &plan).unwrap();
}

#[test]
fn atomic_plan_write_failure_and_locked_database_leave_no_partial_identity() {
    let plan = plan();
    let disk = Disk::new(&plan);
    disk.sql().execute_batch("PRAGMA query_only=ON").unwrap();
    assert!(persist(disk.sql(), &plan).is_err());
    disk.sql().execute_batch("PRAGMA query_only=OFF").unwrap();
    let blocker = Connection::open(&disk.path).unwrap();
    blocker.execute_batch("BEGIN IMMEDIATE").unwrap();
    disk.sql().busy_timeout(std::time::Duration::ZERO).unwrap();
    assert!(persist(disk.sql(), &plan).is_err());
    blocker.execute_batch("ROLLBACK").unwrap();
    let count: i64 = disk
        .sql()
        .query_row(
            "SELECT count(*) FROM sqlite_schema WHERE tbl_name=?1",
            [TABLE],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(count, 0);
    // No native effect is available through this module on either outcome.
    persist(disk.sql(), &plan).unwrap();
    require_plan(disk.sql(), &plan).unwrap();
}
