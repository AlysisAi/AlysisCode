//! Disk-only paired account evidence. No credentials, jobs, accounts, profile
//! effects or live native capabilities are created by these fixtures.
use super::*;
use crate::account_state::execution::paired_plan as paired;

fn plan() -> Plan {
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
    let role = |role: Role, control_nonce| paired::RolePlan {
        role,
        job_name: crate::job::paired_execution_name(
            &execution.identity.container,
            role == Role::Consumers,
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
        format: paired::FORMAT.into(),
        execution,
        operation: random_id().unwrap(),
        credential_generation: setup.offline.unwrap().credential_generation.unwrap(),
        image_id: [3; 24],
        session: 7,
        roles,
        rpc_policy: paired::PolicyKind::BaselineV1,
        namespace_birth: Default::default(),
    }
}
struct Disk {
    connection: Option<Connection>,
    path: std::path::PathBuf,
}
impl Disk {
    fn new(plan: &Plan) -> Self {
        let path = crate::util::test_temp_dir()
            .join(format!("paired-account-{}.db", random_id().unwrap()));
        drop(
            std::fs::OpenOptions::new()
                .create_new(true)
                .write(true)
                .open(&path)
                .unwrap(),
        );
        let mut sql = Connection::open(&path).unwrap();
        sql.execute_batch("PRAGMA journal_mode=DELETE; PRAGMA synchronous=FULL")
            .unwrap();
        sql.busy_timeout(std::time::Duration::ZERO).unwrap();
        crate::account_state::initialize_header(&mut sql, &plan.execution.identity.binding)
            .unwrap();
        // The registration patch must create the account schema in the same
        // parent-plan transaction. No tests silently adopt an absent schema.
        paired::persist(&sql, plan).unwrap();
        require_schema(&sql).unwrap();
        Self {
            connection: Some(sql),
            path,
        }
    }
    fn sql(&self) -> &Connection {
        self.connection.as_ref().unwrap()
    }
    fn reopen(&mut self) {
        self.connection.take().unwrap().close().unwrap();
        self.connection = Some(Connection::open(&self.path).unwrap());
        paired::durability(self.sql()).unwrap();
    }
    fn ready(&self, plan: &Plan) {
        for role in [Role::Owner, Role::Consumers] {
            parent_prefix(self.sql(), plan, role, 7);
        }
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
fn parent_events(plan: &Plan, role: Role) -> Vec<paired::Event> {
    vec![
        paired::Event::JobIntent,
        paired::Event::JobCreated,
        paired::Event::BirthIntent,
        paired::Event::Born {
            pid: plan.execution.pid + 1 + role.index() as u32,
            created: plan.execution.created + 1 + role.index() as u64,
            image_id: plan.image_id,
        },
        paired::Event::ResumeIntent,
        paired::Event::Resumed,
        paired::Event::Ready,
        paired::Event::RetireIntent,
        paired::Event::Retired,
    ]
}
fn parent_prefix(sql: &Connection, plan: &Plan, role: Role, count: usize) {
    for event in parent_events(plan, role).into_iter().take(count) {
        paired::append(sql, plan, role, event).unwrap();
    }
}
fn selection(plan: &Plan, role: Role) -> Selection {
    Selection {
        protocol: Selection::protocol(),
        operation: plan.operation.clone(),
        logon: random_id().unwrap(),
        ready: plan.roles[role.index()].resume_nonce.clone(),
        bootstrap_pid: plan.execution.pid + 1 + role.index() as u32,
        bootstrap_created: plan.execution.created + 1 + role.index() as u64,
        image_id: plan.image_id,
        session: plan.session,
        account: plan.execution.identity.binding.clone(),
        credential_generation: plan.credential_generation.clone(),
    }
}
fn events(plan: &Plan, role: Role) -> Vec<Event> {
    let selection = selection(plan, role);
    let request = BirthRequest::new(selection.clone(), random_id().unwrap()).unwrap();
    let pid = plan.execution.pid + 10 + role.index() as u32;
    let created = plan.execution.created + 10 + role.index() as u64;
    vec![
        Event::Intent {
            selection: selection.clone(),
        },
        Event::Delivered { selection },
        Event::BirthIntent {
            request: request.clone(),
        },
        Event::Born {
            request: request.clone(),
            pid,
            created,
            image_id: plan.image_id,
        },
        Event::Acknowledged {
            request,
            pid,
            created,
            image_id: plan.image_id,
        },
    ]
}
fn prefix(sql: &Connection, plan: &Plan, role: Role, events: &[Event]) {
    for event in events {
        append(sql, plan, role, event.clone()).unwrap();
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
fn paired_account_every_prefix_reopens_and_duplicate_or_reordered_events_are_refused() {
    let plan = plan();
    for role in [Role::Owner, Role::Consumers] {
        let expected = events(&plan, role);
        for cut in 0..=5 {
            let mut disk = Disk::new(&plan);
            disk.ready(&plan);
            prefix(disk.sql(), &plan, role, &expected[..cut]);
            disk.reopen();
            assert!(replay(disk.sql(), &plan, role).unwrap() == expected[..cut]);
            for (index, event) in expected.iter().enumerate() {
                if index != cut {
                    assert!(append(disk.sql(), &plan, role, event.clone()).is_err());
                }
            }
            assert!(replay(disk.sql(), &plan, role).unwrap() == expected[..cut]);
        }
    }
}

#[test]
fn rpc_policy_account_birth_journal_rejects_cross_policy_credentials_and_cold_rows() {
    for kind in [
        paired::PolicyKind::KernelBaseAccountFullV1,
        paired::PolicyKind::SystemInheritanceV1,
        paired::PolicyKind::NsiSystemInheritanceV1,
        paired::PolicyKind::SystemReadInheritanceV2,
        paired::PolicyKind::SystemNsiReadInheritanceV3,
    ] {
        rpc_policy_account_birth_journal_rejects_cross_policy_credentials_and_cold_rows_case(kind);
    }
}
fn rpc_policy_account_birth_journal_rejects_cross_policy_credentials_and_cold_rows_case(
    kind: paired::PolicyKind,
) {
    use crate::account_control::bootstrap::entry::credential_wire::child::FixtureStationLowboxBinding;
    let p = plan();
    let selected = selection(&p, Role::Consumers);
    let mut request = FixtureStationLowboxBinding::synthetic_request().birth;
    request.selection = selected.clone();
    request.nonce = random_id().unwrap();
    let Purpose::FixtureStationLowboxV1 { binding } = &mut request.purpose else {
        unreachable!()
    };
    binding.profile.container = p.execution.identity.container.clone();
    binding.profile.package_sid = p.execution.identity.package_sid.clone();
    binding.profile.owner_pid = selected.bootstrap_pid + 50;
    binding.profile.owner_created = selected.bootstrap_created - 1;
    let subject = &mut binding.initializer.subject;
    subject.operation = p.operation.clone();
    subject.bootstrap_pid = selected.bootstrap_pid;
    subject.bootstrap_created = selected.bootstrap_created;
    subject.container = p.execution.identity.container.clone();
    subject.package_sid = p.execution.identity.package_sid.clone();
    subject.owner = p.execution.owner.clone();
    subject.session = p.session;
    binding.initializer.resources.owner_job = p.roles[0].job_nonce.clone();
    binding.initializer.resources.consumer_job = p.roles[1].job_nonce.clone();
    validate_request(&request, &selected, Role::Consumers, &p).unwrap();
    let mut variant = p.clone();
    variant.rpc_policy = kind;
    assert!(validate_request(&request, &selected, Role::Consumers, &variant).is_err());
    let mut alternate = request.clone();
    let Purpose::FixtureStationLowboxV1 { binding } = &mut alternate.purpose else {
        unreachable!()
    };
    binding.policy = variant.rpc_policy.fixture_binding_policy();
    assert!(validate_request(&alternate, &selected, Role::Consumers, &p).is_err());
    if matches!(
        kind,
        paired::PolicyKind::SystemReadInheritanceV2
            | paired::PolicyKind::SystemNsiReadInheritanceV3
    ) {
        // Production evidence is not an admission to the old fixture protocol.
        assert!(validate_request(&alternate, &selected, Role::Consumers, &variant).is_err());
    } else {
        validate_request(&alternate, &selected, Role::Consumers, &variant).unwrap();
    }
    if kind == paired::PolicyKind::SystemNsiReadInheritanceV3 {
        use crate::account_control::bootstrap::entry::credential_wire::child::LowboxCompletion;
        use crate::account_state::execution::resources::FixtureRootBirthKind;
        let mut completed = alternate.clone();
        let Purpose::FixtureStationLowboxV1 { binding } = &mut completed.purpose else {
            unreachable!()
        };
        binding.completion = LowboxCompletion::V3ShadowBoundedV1;
        let mut shadow = variant.clone();
        shadow.namespace_birth = FixtureRootBirthKind::SessionShadowDirectoryV1;
        validate_request(&completed, &selected, Role::Consumers, &shadow).unwrap();
        // Matched RPC policy alone cannot authorize a different constructor.
        for root in [
            FixtureRootBirthKind::LegacyUnspecified,
            FixtureRootBirthKind::OrdinaryDirectoryV1,
        ] {
            let mut wrong = shadow.clone();
            wrong.namespace_birth = root;
            assert!(validate_request(&completed, &selected, Role::Consumers, &wrong).is_err());
        }
        assert!(validate_request(&alternate, &selected, Role::Consumers, &shadow).is_err());
        let d = Disk::new(&shadow);
        d.ready(&shadow);
        append(
            d.sql(),
            &shadow,
            Role::Consumers,
            Event::Intent {
                selection: selected.clone(),
            },
        )
        .unwrap();
        append(
            d.sql(),
            &shadow,
            Role::Consumers,
            Event::Delivered {
                selection: selected.clone(),
            },
        )
        .unwrap();
        append(
            d.sql(),
            &shadow,
            Role::Consumers,
            Event::BirthIntent { request: completed },
        )
        .unwrap();
        assert_eq!(replay(d.sql(), &shadow, Role::Consumers).unwrap().len(), 3);
        assert!(replay(d.sql(), &variant, Role::Consumers).is_err());
    }
    let disk = Disk::new(&p);
    disk.ready(&p);
    for event in [
        Event::Intent {
            selection: selected.clone(),
        },
        Event::Delivered {
            selection: selected,
        },
    ] {
        append(disk.sql(), &p, Role::Consumers, event).unwrap();
    }
    let crossed = Event::BirthIntent { request: alternate };
    assert!(append(disk.sql(), &p, Role::Consumers, crossed.clone()).is_err());
    assert_eq!(replay(disk.sql(), &p, Role::Consumers).unwrap().len(), 2);
    disk.sql()
        .execute(
            "INSERT INTO candidate_paired_account_events VALUES(1,2,?1)",
            [serde_json::to_string(&crossed).unwrap()],
        )
        .unwrap();
    assert!(replay(disk.sql(), &p, Role::Consumers).is_err());
    assert!(replay(disk.sql(), &variant, Role::Consumers).is_err());
}

#[test]
fn paired_account_requires_own_durable_ready_and_exact_bootstrap_selection() {
    let plan = plan();
    for role in [Role::Owner, Role::Consumers] {
        for count in 0..7 {
            let disk = Disk::new(&plan);
            parent_prefix(disk.sql(), &plan, role, count);
            let intent = events(&plan, role).remove(0);
            assert!(append(disk.sql(), &plan, role, intent.clone()).is_err());
            // A cold insertion cannot turn an incomplete parent into readiness.
            disk.sql()
                .execute(
                    "INSERT INTO candidate_paired_account_events VALUES(?1,0,?2)",
                    params![role.index(), serde_json::to_string(&intent).unwrap()],
                )
                .unwrap();
            assert!(replay(disk.sql(), &plan, Role::Owner).is_err());
            assert!(replay(disk.sql(), &plan, Role::Consumers).is_err());
        }
        let disk = Disk::new(&plan);
        disk.ready(&plan);
        let changes: Vec<Box<dyn Fn(&mut Selection)>> = vec![
            Box::new(|s| s.protocol.push('x')),
            Box::new(|s| s.operation = random_id().unwrap()),
            Box::new(|s| s.ready = random_id().unwrap()),
            Box::new(|s| s.bootstrap_pid += 1),
            Box::new(|s| s.bootstrap_created += 1),
            Box::new(|s| s.image_id[0] ^= 1),
            Box::new(|s| s.session += 1),
            Box::new(|s| s.account.mode = AccountMode::Online),
            Box::new(|s| s.account.account_sid.push_str("-1")),
            Box::new(|s| s.account.group_sid.push_str("-1")),
            Box::new(|s| s.account.account_name.push('x')),
            Box::new(|s| s.account.owner_sid = "S-1-5-18".into()),
            Box::new(|s| s.account.installation_id = random_id().unwrap()),
            Box::new(|s| {
                s.credential_generation = "CredOwner-2123456789abcdef0123456789abcdef".into()
            }),
        ];
        for change in changes {
            let mut selected = selection(&plan, role);
            change(&mut selected);
            assert!(
                append(
                    disk.sql(),
                    &plan,
                    role,
                    Event::Intent {
                        selection: selected
                    }
                )
                .is_err()
            );
        }
        let other = if role == Role::Owner {
            Role::Consumers
        } else {
            Role::Owner
        };
        assert!(
            append(
                disk.sql(),
                &plan,
                role,
                Event::Intent {
                    selection: selection(&plan, other)
                }
            )
            .is_err()
        );
        assert!(replay(disk.sql(), &plan, role).unwrap().is_empty());
    }
}

#[test]
fn paired_account_fresh_nonces_cannot_reuse_any_plan_or_opposite_role_account_nonce() {
    let plan = plan();
    for role in [Role::Owner, Role::Consumers] {
        let disk = Disk::new(&plan);
        disk.ready(&plan);
        for nonce in reserved_nonces(&plan) {
            let mut selected = selection(&plan, role);
            selected.logon = nonce;
            assert!(
                append(
                    disk.sql(),
                    &plan,
                    role,
                    Event::Intent {
                        selection: selected
                    }
                )
                .is_err()
            );
        }
        let expected = events(&plan, role);
        prefix(disk.sql(), &plan, role, &expected[..2]);
        for nonce in reserved_nonces(&plan) {
            let mut event = expected[2].clone();
            if let Event::BirthIntent { request } = &mut event {
                request.nonce = nonce;
            }
            assert!(append(disk.sql(), &plan, role, event).is_err());
        }
        assert!(replay(disk.sql(), &plan, role).unwrap() == expected[..2]);
    }
    for first in [Role::Owner, Role::Consumers] {
        let second = if first == Role::Owner {
            Role::Consumers
        } else {
            Role::Owner
        };
        let disk = Disk::new(&plan);
        disk.ready(&plan);
        let a = events(&plan, first);
        prefix(disk.sql(), &plan, first, &a);
        let Event::BirthIntent { request: a } = &a[2] else {
            unreachable!()
        };
        for reused in [&a.selection.logon, &a.nonce] {
            let mut selected = selection(&plan, second);
            selected.logon = reused.clone();
            assert!(
                append(
                    disk.sql(),
                    &plan,
                    second,
                    Event::Intent {
                        selection: selected
                    }
                )
                .is_err()
            );
        }
        let b = events(&plan, second);
        prefix(disk.sql(), &plan, second, &b[..2]);
        for reused in [&a.selection.logon, &a.nonce] {
            let mut event = b[2].clone();
            if let Event::BirthIntent { request } = &mut event {
                request.nonce = reused.clone();
            }
            assert!(append(disk.sql(), &plan, second, event).is_err());
        }
    }
}

#[test]
fn paired_account_delivery_and_birth_are_exact_and_owner_remains_observe_only() {
    let plan = plan();
    let role = Role::Owner;
    let disk = Disk::new(&plan);
    disk.ready(&plan);
    let expected = events(&plan, role);
    prefix(disk.sql(), &plan, role, &expected[..1]);
    let mut delivered = expected[1].clone();
    if let Event::Delivered { selection } = &mut delivered {
        selection.logon = random_id().unwrap();
    }
    assert!(append(disk.sql(), &plan, role, delivered).is_err());
    append(disk.sql(), &plan, role, expected[1].clone()).unwrap();
    for purpose in [
        Purpose::TrustedSelfCheckV1,
        Purpose::TrustedHandoffSelfCheckV1,
        Purpose::TrustedUnusedProfileV1 {
            container: plan.execution.identity.container.clone(),
            package_sid: plan.execution.identity.package_sid.clone(),
            policy: 1,
        },
        Purpose::TrustedRpcSelfCheckV1 {
            package_sid: plan.execution.identity.package_sid.clone(),
            initializer: random_id().unwrap(),
        },
    ] {
        let mut event = expected[2].clone();
        if let Event::BirthIntent { request } = &mut event {
            request.purpose = purpose;
        }
        assert!(append(disk.sql(), &plan, role, event).is_err());
    }
    let mut event = expected[2].clone();
    if let Event::BirthIntent { request } = &mut event {
        request.selection.logon = random_id().unwrap();
    }
    assert!(append(disk.sql(), &plan, role, event).is_err());
    assert!(replay(disk.sql(), &plan, role).unwrap() == expected[..2]);
}

#[test]
fn paired_account_original_birth_times_images_and_acknowledgments_cannot_change() {
    let plan = plan();
    let role = Role::Owner;
    let disk = Disk::new(&plan);
    disk.ready(&plan);
    let expected = events(&plan, role);
    prefix(disk.sql(), &plan, role, &expected[..3]);
    for fault in 0..7 {
        let mut event = expected[3].clone();
        if let Event::Born {
            request,
            pid,
            created,
            image_id,
        } = &mut event
        {
            match fault {
                0 => *pid = 0,
                1 => *pid = plan.execution.pid,
                2 => *pid = request.selection.bootstrap_pid,
                3 => *created = request.selection.bootstrap_created - 1,
                4 => image_id[0] ^= 1,
                5 => request.nonce = random_id().unwrap(),
                _ => {
                    // The consumer bootstrap is newer than the owner, so only
                    // the cross-bootstrap identity refusal catches this case.
                    *pid = plan.execution.pid + 2;
                    *created = plan.execution.created + 2;
                }
            }
        }
        assert!(append(disk.sql(), &plan, role, event).is_err());
    }
    append(disk.sql(), &plan, role, expected[3].clone()).unwrap();
    for fault in 0..4 {
        let mut event = expected[4].clone();
        if let Event::Acknowledged {
            request,
            pid,
            created,
            image_id,
        } = &mut event
        {
            match fault {
                0 => *pid += 1,
                1 => *created += 1,
                2 => image_id[0] ^= 1,
                _ => request.nonce = random_id().unwrap(),
            }
        }
        assert!(append(disk.sql(), &plan, role, event).is_err());
    }
    append(disk.sql(), &plan, role, expected[4].clone()).unwrap();
    assert!(replay(disk.sql(), &plan, role).unwrap() == expected);
}

#[test]
fn paired_account_two_roles_cannot_claim_the_same_original_account_process() {
    let plan = plan();
    let disk = Disk::new(&plan);
    disk.ready(&plan);
    let owner = events(&plan, Role::Owner);
    let consumer = events(&plan, Role::Consumers);
    prefix(disk.sql(), &plan, Role::Owner, &owner);
    prefix(disk.sql(), &plan, Role::Consumers, &consumer[..3]);
    let Event::Born {
        pid: owner_pid,
        created: owner_created,
        ..
    } = owner[3]
    else {
        unreachable!()
    };
    let mut alias = consumer[3].clone();
    if let Event::Born { pid, created, .. } = &mut alias {
        *pid = owner_pid;
        *created = owner_created;
    }
    assert!(append(disk.sql(), &plan, Role::Consumers, alias.clone()).is_err());
    // Direct durable evidence does not bypass the cross-role check either.
    disk.sql()
        .execute(
            "INSERT INTO candidate_paired_account_events VALUES(1,3,?1)",
            [serde_json::to_string(&alias).unwrap()],
        )
        .unwrap();
    for role in [Role::Owner, Role::Consumers] {
        assert!(replay(disk.sql(), &plan, role).is_err());
    }
}

#[test]
fn paired_account_retirement_preserves_every_existing_prefix_but_forbids_more_work() {
    let plan = plan();
    for count in 0..=5 {
        let mut disk = Disk::new(&plan);
        disk.ready(&plan);
        let owner = events(&plan, Role::Owner);
        let consumer = events(&plan, Role::Consumers);
        prefix(disk.sql(), &plan, Role::Owner, &owner[..count]);
        prefix(disk.sql(), &plan, Role::Consumers, &consumer[..count]);
        for role in [Role::Consumers, Role::Owner] {
            paired::append(disk.sql(), &plan, role, paired::Event::RetireIntent).unwrap();
            let expected = if role == Role::Owner {
                &owner
            } else {
                &consumer
            };
            assert!(replay(disk.sql(), &plan, role).unwrap() == expected[..count]);
            let next = expected[count.min(4)].clone();
            let error = append(disk.sql(), &plan, role, next).err().unwrap();
            assert!(error.to_string().contains("live Ready phase"));
            paired::append(disk.sql(), &plan, role, paired::Event::Retired).unwrap();
        }
        disk.reopen();
        assert!(replay(disk.sql(), &plan, Role::Owner).unwrap() == owner[..count]);
        assert!(replay(disk.sql(), &plan, Role::Consumers).unwrap() == consumer[..count]);
    }
}

#[test]
fn paired_account_schema_and_rows_are_immutable_and_invalid_inventory_is_preserved() {
    let plan = plan();
    let disk = Disk::new(&plan);
    disk.ready(&plan);
    let intent = events(&plan, Role::Owner).remove(0);
    append(disk.sql(), &plan, Role::Owner, intent.clone()).unwrap();
    for statement in [
        "UPDATE candidate_paired_account_events SET event_json='{}'",
        "DELETE FROM candidate_paired_account_events",
        "INSERT OR REPLACE INTO candidate_paired_account_events VALUES(0,0,'{}')",
        "INSERT INTO candidate_paired_account_events VALUES(2,0,'{}')",
        "INSERT INTO candidate_paired_account_events VALUES(0,5,'{}')",
    ] {
        assert!(disk.sql().execute_batch(statement).is_err());
    }
    assert!(create_schema(disk.sql()).is_err());
    assert!(replay(disk.sql(), &plan, Role::Owner).unwrap() == [intent.clone()]);
    disk.sql()
        .execute_batch("DROP TRIGGER paired_account_no_replace")
        .unwrap();
    assert!(require_schema(disk.sql()).is_err());
    assert!(replay(disk.sql(), &plan, Role::Owner).is_err());
    disk.sql().execute_batch(SCHEMA[3]).unwrap();
    require_schema(disk.sql()).unwrap();
    disk.sql()
        .execute_batch(
            "CREATE INDEX unexpected_account_index ON candidate_paired_account_events(event_json)",
        )
        .unwrap();
    assert!(require_schema(disk.sql()).is_err());
    let mut unknown = serde_json::to_value(&intent).unwrap();
    unknown["selection"]["extra"] = true.into();
    let mut missing = serde_json::to_value(&intent).unwrap();
    missing["selection"]
        .as_object_mut()
        .unwrap()
        .remove("logon");
    for (sequence, json) in [
        (1, serde_json::to_string(&intent).unwrap()),
        (0, "{".into()),
        (0, unknown.to_string()),
        (0, missing.to_string()),
        (
            0,
            format!(
                "{{\"kind\":\"intent\",{}",
                &serde_json::to_string(&intent).unwrap()[1..]
            ),
        ),
        (0, "é".repeat(MAX_BYTES / 2 + 1)),
    ] {
        let disk = Disk::new(&plan);
        disk.ready(&plan);
        disk.sql()
            .execute(
                "INSERT INTO candidate_paired_account_events VALUES(0,?1,?2)",
                params![sequence, json],
            )
            .unwrap();
        assert!(replay(disk.sql(), &plan, Role::Consumers).is_err());
        assert!(
            append(
                disk.sql(),
                &plan,
                Role::Consumers,
                events(&plan, Role::Consumers).remove(0)
            )
            .is_err()
        );
        let saved: String = disk
            .sql()
            .query_row(
                "SELECT event_json FROM candidate_paired_account_events",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(saved, json);
    }
    let disk = Disk::new(&plan);
    disk.ready(&plan);
    disk.sql()
        .execute(
            "INSERT INTO candidate_paired_account_events VALUES(0,0,?1)",
            [serde_json::to_vec(&intent).unwrap()],
        )
        .unwrap();
    assert!(replay(disk.sql(), &plan, Role::Owner).is_err());
    let kind: String = disk
        .sql()
        .query_row(
            "SELECT typeof(event_json) FROM candidate_paired_account_events",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(kind, "blob");
}

#[test]
fn paired_account_wrong_plan_durability_and_failed_commit_preserve_the_prefix() {
    let plan = plan();
    let mut disk = Disk::new(&plan);
    disk.ready(&plan);
    let expected = events(&plan, Role::Owner);
    prefix(disk.sql(), &plan, Role::Owner, &expected[..1]);
    let mut other = plan.clone();
    other.operation = random_id().unwrap();
    assert!(replay(disk.sql(), &other, Role::Owner).is_err());
    assert!(append(disk.sql(), &other, Role::Owner, expected[1].clone()).is_err());
    for statement in [
        "BEGIN",
        "PRAGMA synchronous=NORMAL",
        "PRAGMA journal_mode=MEMORY",
    ] {
        disk.sql().execute_batch(statement).unwrap();
        assert!(append(disk.sql(), &plan, Role::Owner, expected[1].clone()).is_err());
        if !disk.sql().is_autocommit() {
            disk.sql().execute_batch("ROLLBACK").unwrap();
        }
        disk.sql()
            .execute_batch("PRAGMA journal_mode=DELETE; PRAGMA synchronous=FULL")
            .unwrap();
    }
    disk.sql().execute_batch("PRAGMA query_only=ON").unwrap();
    let error = append(disk.sql(), &plan, Role::Owner, expected[1].clone())
        .err()
        .unwrap();
    assert_eq!(sqlite_code(&error), Some(rusqlite::ErrorCode::ReadOnly));
    disk.sql().execute_batch("PRAGMA query_only=OFF").unwrap();
    let reader = Connection::open(&disk.path).unwrap();
    reader.execute_batch("BEGIN").unwrap();
    let _: String = reader
        .query_row(
            "SELECT plan_json FROM candidate_paired_bootstrap_plan",
            [],
            |r| r.get(0),
        )
        .unwrap();
    let error = append(disk.sql(), &plan, Role::Owner, expected[1].clone())
        .err()
        .unwrap();
    assert_eq!(sqlite_code(&error), Some(rusqlite::ErrorCode::DatabaseBusy));
    reader.execute_batch("ROLLBACK").unwrap();
    disk.reopen();
    assert!(replay(disk.sql(), &plan, Role::Owner).unwrap() == expected[..1]);
    assert!(
        replay(disk.sql(), &plan, Role::Consumers)
            .unwrap()
            .is_empty()
    );
}
