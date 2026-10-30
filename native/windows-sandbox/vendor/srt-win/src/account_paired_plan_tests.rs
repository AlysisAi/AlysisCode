//! Disk-only journal mechanics. No account, process, native job, profile or ACL
//! is created, and parsed evidence cannot construct a live CommittedPlan.
use super::*;

pub(super) fn plan() -> Plan {
    let setup = crate::account_install::synthetic_ready();
    let identity = CandidateJournalIdentity::new(
        setup.binding(AccountMode::Offline).unwrap(),
        format!("Alysis.Execution.{}", random_id().unwrap()),
    )
    .unwrap();
    let mut execution = fixture_record(identity);
    execution.scope = PAIRED_PLAN_SCOPE.into();
    execution.job =
        crate::job::paired_execution_name(&execution.identity.container, false).unwrap();
    let role = |kind: Role, control_nonce| RolePlan {
        role: kind,
        job_name: crate::job::paired_execution_name(
            &execution.identity.container,
            kind == Role::Consumers,
        )
        .unwrap(),
        control_nonce,
        job_nonce: random_id().unwrap(),
        birth_nonce: random_id().unwrap(),
        resume_nonce: random_id().unwrap(),
        retire_nonce: random_id().unwrap(),
    };
    let roles = [role(Role::Owner, [4; 16]), role(Role::Consumers, [5; 16])];
    Plan {
        format: FORMAT.into(),
        execution,
        operation: random_id().unwrap(),
        credential_generation: setup.offline.unwrap().credential_generation.unwrap(),
        image_id: [3; 24],
        session: 7,
        roles,
        rpc_policy: PolicyKind::BaselineV1,
        namespace_birth: Default::default(),
    }
}

pub(super) struct Disk {
    connection: Option<Connection>,
    path: std::path::PathBuf,
}
impl Disk {
    pub(super) fn new(plan: &Plan) -> Self {
        let path =
            crate::util::test_temp_dir().join(format!("paired-plan-{}.db", random_id().unwrap()));
        drop(
            std::fs::OpenOptions::new()
                .create_new(true)
                .write(true)
                .open(&path)
                .unwrap(),
        );
        let mut connection = Connection::open(&path).unwrap();
        connection
            .execute_batch("PRAGMA journal_mode=DELETE; PRAGMA synchronous=FULL")
            .unwrap();
        connection.busy_timeout(std::time::Duration::ZERO).unwrap();
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
                "SELECT plan_json FROM candidate_paired_bootstrap_plan",
                [],
                |r| r.get(0),
            )
            .unwrap()
    }
    fn empty_schema(&self) -> bool {
        self.sql()
            .query_row(
                "SELECT count(*) FROM sqlite_schema WHERE tbl_name IN (?1,?2)",
                params![TABLE, EVENTS_TABLE],
                |r| r.get::<_, usize>(0),
            )
            .unwrap()
            == 0
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
fn events(plan: &Plan, role: Role) -> Vec<Event> {
    vec![
        Event::JobIntent,
        Event::JobCreated,
        Event::BirthIntent,
        Event::Born {
            pid: plan.execution.pid + 1 + role.index() as u32,
            created: plan.execution.created + 1 + role.index() as u64,
            image_id: plan.image_id,
        },
        Event::ResumeIntent,
        Event::Resumed,
        Event::Ready,
        Event::RetireIntent,
        Event::Retired,
    ]
}
pub(super) fn prefix(sql: &Connection, plan: &Plan, role: Role, count: usize) {
    for event in events(plan, role).into_iter().take(count) {
        append(sql, plan, role, event).unwrap();
    }
}
fn sqlite_code(error: &anyhow::Error) -> Option<rusqlite::ErrorCode> {
    error
        .chain()
        .find_map(|e| e.downcast_ref::<rusqlite::Error>())
        .and_then(|e| match e {
            rusqlite::Error::SqliteFailure(code, _) => Some(code.code),
            _ => None,
        })
}

#[test]
fn paired_plan_disk_reopen_is_immutable_and_cannot_be_prepared_again() {
    let plan = plan();
    let mut disk = Disk::new(&plan);
    persist(disk.sql(), &plan).unwrap();
    let committed = disk.raw_plan();
    for statement in [
        "UPDATE candidate_paired_bootstrap_plan SET plan_json='{}'",
        "DELETE FROM candidate_paired_bootstrap_plan",
        "INSERT OR REPLACE INTO candidate_paired_bootstrap_plan VALUES(1,'{}')",
        "REPLACE INTO candidate_paired_bootstrap_plan VALUES(1,'{}')",
    ] {
        assert!(disk.sql().execute_batch(statement).is_err());
    }
    assert!(persist(disk.sql(), &plan).is_err());
    disk.reopen();
    require_plan(disk.sql(), &plan).unwrap();
    assert!(persist(disk.sql(), &plan).is_err());
    assert_eq!(disk.raw_plan(), committed);
    assert!(replay(disk.sql(), &plan, Role::Owner).unwrap().is_empty());
    assert!(
        replay(disk.sql(), &plan, Role::Consumers)
            .unwrap()
            .is_empty()
    );
}

#[test]
fn rpc_policy_plan_journal_refuses_cross_policy_and_omitted_variant_identity() {
    let baseline = plan();
    let mut constructor = baseline.clone();
    constructor.rpc_policy = PolicyKind::KernelBaseAccountFullV1;
    let mut inheritance = baseline.clone();
    inheritance.rpc_policy = PolicyKind::SystemInheritanceV1;
    let mut nsi = baseline.clone();
    nsi.rpc_policy = PolicyKind::NsiSystemInheritanceV1;
    let mut production = baseline.clone();
    production.rpc_policy = PolicyKind::SystemReadInheritanceV2;
    let mut production_nsi = baseline.clone();
    production_nsi.rpc_policy = PolicyKind::SystemNsiReadInheritanceV3;
    assert!(baseline.require_live_policy().is_ok());
    assert!(
        serde_json::to_value(&baseline)
            .unwrap()
            .get("rpc_policy")
            .is_none()
    );
    for variant in [
        &constructor,
        &inheritance,
        &production,
        &nsi,
        &production_nsi,
    ] {
        variant.validate().unwrap(); // Passive evidence, never live authority.
        assert!(variant.require_live_policy().is_err());
        let mut wire = serde_json::to_value(variant).unwrap();
        let decoded: Plan = serde_json::from_value(wire.clone()).unwrap();
        assert_eq!(&decoded, variant);
        assert!(decoded.require_live_policy().is_err());
        wire.as_object_mut().unwrap().remove("rpc_policy");
        let omitted: Plan = serde_json::from_value(wire).unwrap();
        assert_eq!(omitted, baseline);
        assert_ne!(&omitted, variant);
    }
    for selected in [
        &baseline,
        &constructor,
        &inheritance,
        &production,
        &nsi,
        &production_nsi,
    ] {
        let mut disk = Disk::new(selected);
        persist(disk.sql(), selected).unwrap();
        disk.reopen();
        require_plan(disk.sql(), selected).unwrap();
        for other in [
            &baseline,
            &constructor,
            &inheritance,
            &production,
            &nsi,
            &production_nsi,
        ] {
            if selected == other {
                continue;
            }
            assert!(require_plan(disk.sql(), other).is_err());
            assert!(replay(disk.sql(), other, Role::Owner).is_err());
            assert!(append(disk.sql(), other, Role::Owner, Event::JobIntent).is_err());
        }
        assert!(
            replay(disk.sql(), selected, Role::Owner)
                .unwrap()
                .is_empty()
        );
    }
}

#[test]
fn rpc_policy_fixture_oracle_rejects_consistent_but_unselected_plan_and_descriptor() {
    use crate::account_state::execution::resources::namespace_native::rpc::fixture_access_policy;
    let baseline = plan();
    baseline
        .fixture_require_rpc_policy(PolicyKind::BaselineV1)
        .unwrap();
    for kind in [
        PolicyKind::KernelBaseAccountFullV1,
        PolicyKind::SystemInheritanceV1,
        PolicyKind::NsiSystemInheritanceV1,
        PolicyKind::SystemReadInheritanceV2,
        PolicyKind::SystemNsiReadInheritanceV3,
    ] {
        let mut forged = baseline.clone();
        forged.rpc_policy = kind;
        let policy = fixture_access_policy(&forged);
        policy.validate(&forged).unwrap(); // Self-consistent evidence is not selection.
        for expected in [
            PolicyKind::BaselineV1,
            PolicyKind::KernelBaseAccountFullV1,
            PolicyKind::SystemInheritanceV1,
            PolicyKind::NsiSystemInheritanceV1,
            PolicyKind::SystemReadInheritanceV2,
            PolicyKind::SystemNsiReadInheritanceV3,
        ] {
            assert_eq!(
                forged.fixture_require_rpc_policy(expected).is_ok(),
                kind == expected
            );
        }
        assert!(
            forged
                .fixture_require_rpc_policy(PolicyKind::BaselineV1)
                .is_err()
        );
        assert!(policy.validate(&baseline).is_err());
        assert!(baseline.fixture_require_rpc_policy(kind).is_err());
        let mut wire = serde_json::to_value(&forged).unwrap();
        wire.as_object_mut().unwrap().remove("rpc_policy");
        let omitted: Plan = serde_json::from_value(wire).unwrap();
        assert!(omitted.fixture_require_rpc_policy(kind).is_err());
        omitted
            .fixture_require_rpc_policy(PolicyKind::BaselineV1)
            .unwrap();
    }
}

#[test]
fn paired_plan_refuses_mismatched_execution_image_credentials_and_each_role_binding() {
    let plan = plan();
    let disk = Disk::new(&plan);
    persist(disk.sql(), &plan).unwrap();
    let committed = disk.raw_plan();
    let changes: Vec<Box<dyn Fn(&mut Plan)>> = vec![
        Box::new(|p| p.operation = random_id().unwrap()),
        Box::new(|p| p.credential_generation = "CredOwner-2123456789abcdef0123456789abcdef".into()),
        Box::new(|p| p.image_id[0] ^= 1),
        Box::new(|p| p.session += 1),
        Box::new(|p| p.execution.pid += 1),
        Box::new(|p| p.execution.created += 1),
        Box::new(|p| p.execution.transaction = random_id().unwrap()),
        Box::new(|p| p.execution.database_id[0] ^= 1),
        Box::new(|p| p.execution.pending_id[0] ^= 1),
        Box::new(|p| p.execution.owner = "S-1-5-18".into()),
        Box::new(|p| p.execution.job.push('x')),
        Box::new(|p| p.execution.identity.binding.mode = AccountMode::Online),
        Box::new(|p| p.execution.identity.binding.account_sid.push_str("-1")),
        Box::new(|p| p.execution.identity.binding.installation_id = random_id().unwrap()),
        Box::new(|p| p.execution.identity.package_sid.push_str("-1")),
    ];
    for change in changes {
        let mut other = plan.clone();
        change(&mut other);
        assert!(require_plan(disk.sql(), &other).is_err());
        assert_eq!(disk.raw_plan(), committed);
    }
    for role in 0..2 {
        for field in 0..6 {
            let mut other = plan.clone();
            let r = &mut other.roles[role];
            match field {
                0 => r.job_name.push('x'),
                1 => r.control_nonce[0] ^= 1,
                2 => r.job_nonce = random_id().unwrap(),
                3 => r.birth_nonce = random_id().unwrap(),
                4 => r.resume_nonce = random_id().unwrap(),
                _ => r.retire_nonce = random_id().unwrap(),
            }
            assert!(require_plan(disk.sql(), &other).is_err());
            assert_eq!(disk.raw_plan(), committed);
        }
    }
    require_plan(disk.sql(), &plan).unwrap();
}

#[test]
fn paired_plan_refuses_scopes_role_order_and_all_cross_purpose_nonce_collisions() {
    let plan = plan();
    let disk = Disk::new(&plan);
    let changes: Vec<Box<dyn Fn(&mut Plan)>> = vec![
        Box::new(|p| p.format.push('x')),
        Box::new(|p| p.execution.scope = SCOPE.into()),
        Box::new(|p| p.execution.scope = RESOURCE_SCOPE.into()),
        Box::new(|p| p.execution.scope = BOOTSTRAP_PLAN_SCOPE.into()),
        Box::new(|p| p.execution.version = 2),
        Box::new(|p| p.operation = "../invalid".into()),
        Box::new(|p| p.credential_generation = "CredOwner-../invalid".into()),
        Box::new(|p| p.image_id = [0; 24]),
        Box::new(|p| p.roles.swap(0, 1)),
        Box::new(|p| p.roles[1].role = Role::Owner),
        Box::new(|p| p.roles[1].job_name = p.roles[0].job_name.clone()),
        Box::new(|p| p.roles[0].control_nonce = [0; 16]),
        Box::new(|p| p.roles[1].control_nonce = p.roles[0].control_nonce),
        Box::new(|p| p.operation = p.execution.transaction.clone()),
        Box::new(|p| p.roles[0].birth_nonce = "../invalid".into()),
        Box::new(|p| p.roles[1].retire_nonce = p.operation.clone()),
        Box::new(|p| p.roles[1].job_nonce = p.execution.transaction.clone()),
        Box::new(|p| p.roles[0].resume_nonce = "04".repeat(16)),
        Box::new(|p| p.operation = "05".repeat(16)),
    ];
    for change in changes {
        let mut invalid = plan.clone();
        change(&mut invalid);
        assert!(persist(disk.sql(), &invalid).is_err());
        assert!(disk.empty_schema());
    }
    // Every one of the eight role/phase positions must reject reuse of every
    // earlier position, both within one role and across owner/consumers.
    for first in 0..8 {
        for second in first + 1..8 {
            let mut invalid = plan.clone();
            let get = |r: &RolePlan, field| match field {
                0 => r.job_nonce.clone(),
                1 => r.birth_nonce.clone(),
                2 => r.resume_nonce.clone(),
                _ => r.retire_nonce.clone(),
            };
            let reused = get(&invalid.roles[first / 4], first % 4);
            let role = &mut invalid.roles[second / 4];
            match second % 4 {
                0 => role.job_nonce = reused,
                1 => role.birth_nonce = reused,
                2 => role.resume_nonce = reused,
                _ => role.retire_nonce = reused,
            }
            assert!(persist(disk.sql(), &invalid).is_err());
            assert!(disk.empty_schema());
        }
    }
}

#[test]
fn paired_every_role_prefix_survives_reopen_and_duplicate_effects_are_refused() {
    let plan = plan();
    for role in [Role::Owner, Role::Consumers] {
        for cut in 0..=9 {
            let mut disk = Disk::new(&plan);
            persist(disk.sql(), &plan).unwrap();
            if role == Role::Owner && cut >= 8 {
                prefix(disk.sql(), &plan, Role::Consumers, 9);
            }
            prefix(disk.sql(), &plan, role, cut);
            disk.reopen();
            let expected = events(&plan, role);
            assert_eq!(replay(disk.sql(), &plan, role).unwrap(), expected[..cut]);
            for event in &expected[..cut] {
                assert!(append(disk.sql(), &plan, role, event.clone()).is_err());
            }
            assert_eq!(replay(disk.sql(), &plan, role).unwrap(), expected[..cut]);
        }
    }
}

#[test]
fn paired_phase_order_and_original_process_receipts_cannot_be_substituted() {
    let plan = plan();
    for role in [Role::Owner, Role::Consumers] {
        let good = events(&plan, role);
        for cut in 0..9 {
            let disk = Disk::new(&plan);
            persist(disk.sql(), &plan).unwrap();
            if role == Role::Owner {
                prefix(disk.sql(), &plan, Role::Consumers, 9);
            }
            prefix(disk.sql(), &plan, role, cut);
            for (index, event) in good.iter().enumerate() {
                if index != cut {
                    assert!(append(disk.sql(), &plan, role, event.clone()).is_err());
                }
            }
            assert_eq!(replay(disk.sql(), &plan, role).unwrap(), good[..cut]);
        }
    }
    let disk = Disk::new(&plan);
    persist(disk.sql(), &plan).unwrap();
    prefix(disk.sql(), &plan, Role::Owner, 4);
    prefix(disk.sql(), &plan, Role::Consumers, 3);
    let owner = events(&plan, Role::Owner)[3].clone();
    assert!(append(disk.sql(), &plan, Role::Consumers, owner).is_err());
    for fault in 0..4 {
        let mut invalid = events(&plan, Role::Consumers)[3].clone();
        if let Event::Born {
            pid,
            created,
            image_id,
        } = &mut invalid
        {
            match fault {
                0 => *pid = 0,
                1 => *pid = plan.execution.pid,
                2 => *created = plan.execution.created - 1,
                _ => image_id[0] ^= 1,
            }
        }
        assert!(append(disk.sql(), &plan, Role::Consumers, invalid).is_err());
    }
    assert_eq!(replay(disk.sql(), &plan, Role::Consumers).unwrap().len(), 3);
}

#[test]
fn paired_owner_retirement_requires_consumers_retired_and_cold_rows_cannot_bypass_it() {
    let plan = plan();
    for consumer_cut in 0..9 {
        let disk = Disk::new(&plan);
        persist(disk.sql(), &plan).unwrap();
        prefix(disk.sql(), &plan, Role::Owner, 7);
        prefix(disk.sql(), &plan, Role::Consumers, consumer_cut);
        assert!(append(disk.sql(), &plan, Role::Owner, Event::RetireIntent).is_err());
        assert_eq!(replay(disk.sql(), &plan, Role::Owner).unwrap().len(), 7);
        // Directly inserted evidence is still rejected on either role's replay.
        disk.sql()
            .execute(
                "INSERT INTO candidate_paired_bootstrap_events VALUES(0,7,?1)",
                [serde_json::to_string(&Event::RetireIntent).unwrap()],
            )
            .unwrap();
        for role in [Role::Owner, Role::Consumers] {
            assert!(replay(disk.sql(), &plan, role).is_err());
        }
    }
    let mut disk = Disk::new(&plan);
    persist(disk.sql(), &plan).unwrap();
    prefix(disk.sql(), &plan, Role::Owner, 7);
    prefix(disk.sql(), &plan, Role::Consumers, 9);
    append(disk.sql(), &plan, Role::Owner, Event::RetireIntent).unwrap();
    append(disk.sql(), &plan, Role::Owner, Event::Retired).unwrap();
    disk.reopen();
    for role in [Role::Owner, Role::Consumers] {
        assert_eq!(
            replay(disk.sql(), &plan, role).unwrap(),
            events(&plan, role)
        );
    }
}

#[test]
fn paired_event_sql_mutation_gap_unknown_fields_and_oversized_inventory_are_refused() {
    let plan = plan();
    let disk = Disk::new(&plan);
    persist(disk.sql(), &plan).unwrap();
    prefix(disk.sql(), &plan, Role::Owner, 1);
    prefix(disk.sql(), &plan, Role::Consumers, 1);
    for statement in [
        "UPDATE candidate_paired_bootstrap_events SET event_json='{}'",
        "DELETE FROM candidate_paired_bootstrap_events",
        "INSERT OR REPLACE INTO candidate_paired_bootstrap_events VALUES(0,0,'{}')",
        "REPLACE INTO candidate_paired_bootstrap_events VALUES(1,0,'{}')",
        "INSERT INTO candidate_paired_bootstrap_events VALUES(2,0,'{}')",
        "INSERT INTO candidate_paired_bootstrap_events VALUES(0,9,'{}')",
    ] {
        assert!(disk.sql().execute_batch(statement).is_err());
    }
    for role in [Role::Owner, Role::Consumers] {
        assert_eq!(replay(disk.sql(), &plan, role).unwrap(), [Event::JobIntent]);
    }
    for (sequence, invalid) in [
        (1, serde_json::to_string(&Event::JobIntent).unwrap()),
        (0, serde_json::to_string(&Event::Retired).unwrap()),
        (0, "{\"kind\":\"job_intent\",\"extra\":true}".into()),
        (
            0,
            "{\"kind\":\"job_intent\",\"kind\":\"job_intent\"}".into(),
        ),
        (0, "{".into()),
        (0, "é".repeat(MAX_EVENT_BYTES / 2 + 1)),
    ] {
        let disk = Disk::new(&plan);
        persist(disk.sql(), &plan).unwrap();
        disk.sql()
            .execute(
                "INSERT INTO candidate_paired_bootstrap_events VALUES(1,?1,?2)",
                params![sequence, invalid],
            )
            .unwrap();
        assert!(replay(disk.sql(), &plan, Role::Owner).is_err());
        assert!(append(disk.sql(), &plan, Role::Owner, Event::JobIntent).is_err());
        let retained: String = disk
            .sql()
            .query_row(
                "SELECT event_json FROM candidate_paired_bootstrap_events",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(retained, invalid);
    }
}

#[test]
fn paired_plan_preserves_malformed_missing_unknown_duplicate_and_oversized_records() {
    let plan = plan();
    let mut unknown = serde_json::to_value(&plan).unwrap();
    unknown["roles"][0]["unexpected"] = true.into();
    let mut missing = serde_json::to_value(&plan).unwrap();
    missing["roles"][1]
        .as_object_mut()
        .unwrap()
        .remove("retire_nonce");
    for invalid in [
        "{".into(),
        "{}".into(),
        unknown.to_string(),
        missing.to_string(),
        "x".repeat(MAX_BYTES + 1),
        format!(
            "{{\"format\":\"duplicate\",{}",
            &serde_json::to_string(&plan).unwrap()[1..]
        ),
    ] {
        let disk = Disk::new(&plan);
        for statement in SCHEMA.into_iter().chain(EVENT_SCHEMA) {
            disk.sql().execute_batch(statement).unwrap();
        }
        disk.sql()
            .execute(
                "INSERT INTO candidate_paired_bootstrap_plan VALUES(1,?1)",
                [&invalid],
            )
            .unwrap();
        assert!(require_plan(disk.sql(), &plan).is_err());
        assert!(persist(disk.sql(), &plan).is_err());
        assert_eq!(disk.raw_plan(), invalid);
    }
}

#[test]
fn paired_plan_exact_schema_and_durability_are_required_on_every_operation() {
    let plan = plan();
    let disk = Disk::new(&plan);
    disk.sql().execute_batch(SCHEMA[0]).unwrap();
    assert!(persist(disk.sql(), &plan).is_err());
    assert!(require_plan(disk.sql(), &plan).is_err());
    let disk = Disk::new(&plan);
    persist(disk.sql(), &plan).unwrap();
    for (name, restore) in [
        ("paired_plan_no_update", SCHEMA[1]),
        ("paired_events_no_replace", EVENT_SCHEMA[3]),
    ] {
        disk.sql()
            .execute_batch(&format!("DROP TRIGGER {name}"))
            .unwrap();
        assert!(require_plan(disk.sql(), &plan).is_err());
        assert!(append(disk.sql(), &plan, Role::Owner, Event::JobIntent).is_err());
        disk.sql().execute_batch(restore).unwrap();
    }
    for statement in [
        "BEGIN",
        "PRAGMA synchronous=NORMAL",
        "PRAGMA journal_mode=MEMORY",
    ] {
        disk.sql().execute_batch(statement).unwrap();
        assert!(require_plan(disk.sql(), &plan).is_err());
        assert!(append(disk.sql(), &plan, Role::Owner, Event::JobIntent).is_err());
        if !disk.sql().is_autocommit() {
            disk.sql().execute_batch("ROLLBACK").unwrap();
        }
        disk.sql()
            .execute_batch("PRAGMA journal_mode=DELETE; PRAGMA synchronous=FULL")
            .unwrap();
    }
    disk.sql()
        .execute_batch(
            "CREATE INDEX unexpected_paired_index ON candidate_paired_bootstrap_events(event_json)",
        )
        .unwrap();
    assert!(require_plan(disk.sql(), &plan).is_err());
}

#[test]
fn paired_readonly_and_busy_write_failures_preserve_committed_prefixes() {
    let plan = plan();
    let mut disk = Disk::new(&plan);
    disk.sql().execute_batch("PRAGMA query_only=ON").unwrap();
    assert_eq!(
        sqlite_code(&persist(disk.sql(), &plan).unwrap_err()),
        Some(rusqlite::ErrorCode::ReadOnly)
    );
    assert!(disk.empty_schema());
    disk.sql().execute_batch("PRAGMA query_only=OFF").unwrap();
    let blocker = Connection::open(&disk.path).unwrap();
    blocker.execute_batch("BEGIN IMMEDIATE").unwrap();
    assert_eq!(
        sqlite_code(&persist(disk.sql(), &plan).unwrap_err()),
        Some(rusqlite::ErrorCode::DatabaseBusy)
    );
    assert!(disk.empty_schema());
    blocker.execute_batch("ROLLBACK").unwrap();
    persist(disk.sql(), &plan).unwrap();
    for role in [Role::Owner, Role::Consumers] {
        prefix(disk.sql(), &plan, role, 1);
    }
    disk.sql().execute_batch("PRAGMA query_only=ON").unwrap();
    assert_eq!(
        sqlite_code(&append(disk.sql(), &plan, Role::Owner, Event::JobCreated).unwrap_err()),
        Some(rusqlite::ErrorCode::ReadOnly)
    );
    disk.sql().execute_batch("PRAGMA query_only=OFF").unwrap();
    // A reader permits the insert but prevents COMMIT obtaining its exclusive
    // lock. This tests rollback of the real transaction after commit failure.
    blocker.execute_batch("BEGIN").unwrap();
    let _: String = blocker
        .query_row(
            "SELECT plan_json FROM candidate_paired_bootstrap_plan",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(
        sqlite_code(&append(disk.sql(), &plan, Role::Consumers, Event::JobCreated).unwrap_err()),
        Some(rusqlite::ErrorCode::DatabaseBusy)
    );
    blocker.execute_batch("ROLLBACK").unwrap();
    disk.reopen();
    for role in [Role::Owner, Role::Consumers] {
        assert_eq!(replay(disk.sql(), &plan, role).unwrap(), [Event::JobIntent]);
    }
}
