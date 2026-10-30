//! Disk-only resume evidence; no process, account, credential or resource effect.
use super::*;
use crate::account_state::execution::paired_plan::account_journal as account;

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
        let path =
            crate::util::test_temp_dir().join(format!("paired-resume-{}.db", random_id().unwrap()));
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
        // Parent persistence must register this schema atomically. Do not make
        // the fixture silently adopt a schema missing from the real plan path.
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
    fn seed(&self, plan: &Plan, counts: [usize; 2], purpose: Purpose) -> [Vec<account::Event>; 2] {
        let births = [
            account_events(plan, Role::Owner, Purpose::ObserveOnly),
            account_events(plan, Role::Consumers, purpose),
        ];
        for role in [Role::Owner, Role::Consumers] {
            for event in [
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
            ] {
                paired::append(self.sql(), plan, role, event).unwrap();
            }
            for event in births[role.index()].iter().take(counts[role.index()]) {
                account::append(self.sql(), plan, role, event.clone()).unwrap();
            }
        }
        births
    }
    fn prepared(&self, plan: &Plan) -> Vec<Event> {
        resume_events(&self.seed(plan, [5, 5], Purpose::TrustedSelfCheckV1))
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

fn account_events(plan: &Plan, role: Role, purpose: Purpose) -> Vec<account::Event> {
    let selection = Selection {
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
    };
    let mut request = BirthRequest::new(selection.clone(), random_id().unwrap()).unwrap();
    request.purpose = purpose;
    let pid = plan.execution.pid + 10 + role.index() as u32;
    let created = plan.execution.created + 10 + role.index() as u64;
    vec![
        account::Event::Intent {
            selection: selection.clone(),
        },
        account::Event::Delivered { selection },
        account::Event::BirthIntent {
            request: request.clone(),
        },
        account::Event::Born {
            request: request.clone(),
            pid,
            created,
            image_id: plan.image_id,
        },
        account::Event::Acknowledged {
            request,
            pid,
            created,
            image_id: plan.image_id,
        },
    ]
}
fn resume_events(accounts: &[Vec<account::Event>; 2]) -> Vec<Event> {
    let account::Event::Acknowledged {
        request: birth,
        pid,
        created,
        ..
    } = &accounts[1][4]
    else {
        unreachable!()
    };
    let request = probe::Request {
        birth: birth.clone(),
        nonce: random_id().unwrap(),
        pid: *pid,
        created: *created,
    };
    vec![
        Event::Intent {
            request: request.clone(),
        },
        Event::Resumed {
            request: request.clone(),
        },
        Event::Exited {
            request,
            code: probe::EXIT_CODE,
        },
    ]
}
fn prefix(sql: &Connection, plan: &Plan, events: &[Event]) {
    for event in events {
        append(sql, plan, Role::Consumers, event.clone()).unwrap();
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
fn saved(sql: &Connection) -> Vec<(i64, i64, String)> {
    sql.prepare("SELECT role,sequence,event_json FROM candidate_paired_account_resume_events ORDER BY role,sequence")
        .unwrap()
        .query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))
        .unwrap()
        .collect::<std::result::Result<_, _>>()
        .unwrap()
}

#[test]
fn paired_resume_every_prefix_reopens_and_cannot_repeat_or_reorder() {
    let plan = plan();
    for cut in 0..=3 {
        let mut disk = Disk::new(&plan);
        let expected = disk.prepared(&plan);
        prefix(disk.sql(), &plan, &expected[..cut]);
        disk.reopen();
        assert!(replay(disk.sql(), &plan, Role::Consumers).unwrap() == expected[..cut]);
        for (index, event) in expected.iter().enumerate() {
            if index != cut {
                assert!(append(disk.sql(), &plan, Role::Consumers, event.clone()).is_err());
            }
        }
        assert!(replay(disk.sql(), &plan, Role::Owner).is_err());
        assert!(append(disk.sql(), &plan, Role::Owner, expected[cut.min(2)].clone()).is_err());
        assert!(replay(disk.sql(), &plan, Role::Consumers).unwrap() == expected[..cut]);
    }
}

#[test]
fn paired_writer_resume_refuses_ordinary_owner_even_with_complete_births() {
    use crate::account_control::bootstrap::entry::credential_wire::child::FixtureWriterBinding;
    let plan = plan();
    let disk = Disk::new(&plan);
    let purpose = Purpose::FixtureProfileWriterV1 {
        binding: FixtureWriterBinding {
            container: plan.execution.identity.container.clone(),
            package_sid: plan.execution.identity.package_sid.clone(),
            folder: [8; 24],
            owner_birth: random_id().unwrap(),
            owner_resume: random_id().unwrap(),
            owner_pid: plan.execution.pid + 10,
            owner_created: plan.execution.created + 10,
            use_nonce: random_id().unwrap(),
            unsupported_registry: false,
            hold_rounds: 0,
            descendant_files: false,
        },
    };
    let expected = resume_events(&disk.seed(&plan, [5, 5], purpose));
    assert!(append(disk.sql(), &plan, Role::Consumers, expected[0].clone()).is_err());
    assert!(saved(disk.sql()).is_empty());
    disk.sql()
        .execute(
            "INSERT INTO candidate_paired_account_resume_events VALUES(1,0,?1)",
            [serde_json::to_string(&expected[0]).unwrap()],
        )
        .unwrap();
    let before = saved(disk.sql());
    assert!(replay(disk.sql(), &plan, Role::Consumers).is_err());
    assert_eq!(saved(disk.sql()), before);
}

#[test]
fn paired_resume_requires_both_complete_account_acknowledgments_and_fixed_purpose() {
    let plan = plan();
    for role in [Role::Owner, Role::Consumers] {
        for cut in 0..5 {
            let disk = Disk::new(&plan);
            let mut counts = [5, 5];
            counts[role.index()] = cut;
            let expected = resume_events(&disk.seed(&plan, counts, Purpose::TrustedSelfCheckV1));
            assert!(append(disk.sql(), &plan, Role::Consumers, expected[0].clone()).is_err());
            assert!(saved(disk.sql()).is_empty());
            // Cold insertion does not manufacture the missing original parent.
            disk.sql()
                .execute(
                    "INSERT INTO candidate_paired_account_resume_events VALUES(1,0,?1)",
                    [serde_json::to_string(&expected[0]).unwrap()],
                )
                .unwrap();
            let before = saved(disk.sql());
            assert!(replay(disk.sql(), &plan, Role::Consumers).is_err());
            assert_eq!(saved(disk.sql()), before);
        }
    }
    let disk = Disk::new(&plan);
    let expected = resume_events(&disk.seed(&plan, [5, 5], Purpose::ObserveOnly));
    assert!(append(disk.sql(), &plan, Role::Consumers, expected[0].clone()).is_err());
    assert!(saved(disk.sql()).is_empty());
}

#[test]
fn paired_resume_binds_every_original_request_identity_and_refuses_other_purposes() {
    let plan = plan();
    let disk = Disk::new(&plan);
    let expected = disk.prepared(&plan);
    let Event::Intent { request } = &expected[0] else {
        unreachable!()
    };
    let changes: Vec<Box<dyn Fn(&mut probe::Request)>> = vec![
        Box::new(|r| r.pid += 1),
        Box::new(|r| r.created += 1),
        Box::new(|r| r.birth.nonce = random_id().unwrap()),
        Box::new(|r| r.birth.selection.protocol.push('x')),
        Box::new(|r| r.birth.selection.operation = random_id().unwrap()),
        Box::new(|r| r.birth.selection.logon = random_id().unwrap()),
        Box::new(|r| r.birth.selection.ready = random_id().unwrap()),
        Box::new(|r| r.birth.selection.bootstrap_pid += 1),
        Box::new(|r| r.birth.selection.bootstrap_created += 1),
        Box::new(|r| r.birth.selection.image_id[0] ^= 1),
        Box::new(|r| r.birth.selection.session += 1),
        Box::new(|r| r.birth.selection.account.account_sid.push_str("-1")),
        Box::new(|r| r.birth.selection.account.group_sid.push_str("-1")),
        Box::new(|r| r.birth.selection.account.account_name.push('x')),
        Box::new(|r| r.birth.selection.account.mode = AccountMode::Online),
        Box::new(|r| r.birth.selection.account.owner_sid = "S-1-5-18".into()),
        Box::new(|r| r.birth.selection.account.installation_id = random_id().unwrap()),
        Box::new(|r| {
            r.birth.selection.credential_generation =
                "CredOwner-2123456789abcdef0123456789abcdef".into()
        }),
    ];
    for change in changes {
        let mut wrong = request.clone();
        change(&mut wrong);
        assert!(
            append(
                disk.sql(),
                &plan,
                Role::Consumers,
                Event::Intent { request: wrong }
            )
            .is_err()
        );
    }
    for purpose in [
        Purpose::ObserveOnly,
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
        let mut wrong = request.clone();
        wrong.birth.purpose = purpose;
        assert!(
            append(
                disk.sql(),
                &plan,
                Role::Consumers,
                Event::Intent { request: wrong }
            )
            .is_err()
        );
    }
    assert!(saved(disk.sql()).is_empty());
    prefix(disk.sql(), &plan, &expected[..1]);
    let mut wrong = request.clone();
    wrong.nonce = random_id().unwrap();
    assert!(
        append(
            disk.sql(),
            &plan,
            Role::Consumers,
            Event::Resumed { request: wrong }
        )
        .is_err()
    );
    append(disk.sql(), &plan, Role::Consumers, expected[1].clone()).unwrap();
    for code in [0, 1, probe::EXIT_CODE + 1] {
        assert!(
            append(
                disk.sql(),
                &plan,
                Role::Consumers,
                Event::Exited {
                    request: request.clone(),
                    code
                }
            )
            .is_err()
        );
    }
    let mut wrong = request.clone();
    wrong.pid += 1;
    assert!(
        append(
            disk.sql(),
            &plan,
            Role::Consumers,
            Event::Exited {
                request: wrong,
                code: probe::EXIT_CODE
            }
        )
        .is_err()
    );
    assert!(replay(disk.sql(), &plan, Role::Consumers).unwrap() == expected[..2]);
}

#[test]
fn paired_resume_nonce_cannot_reuse_plan_or_either_accounts_logon_or_birth() {
    let plan = plan();
    let disk = Disk::new(&plan);
    let accounts = disk.seed(&plan, [5, 5], Purpose::TrustedSelfCheckV1);
    let expected = resume_events(&accounts);
    let Event::Intent { request } = &expected[0] else {
        unreachable!()
    };
    let mut forbidden = account::reserved_nonces(&plan);
    for events in &accounts {
        let account::Event::BirthIntent { request } = &events[2] else {
            unreachable!()
        };
        forbidden.extend([request.nonce.clone(), request.selection.logon.clone()]);
    }
    forbidden.extend([String::new(), "0".repeat(31), "A".repeat(32)]);
    for nonce in forbidden {
        let mut wrong = request.clone();
        wrong.nonce = nonce;
        assert!(
            append(
                disk.sql(),
                &plan,
                Role::Consumers,
                Event::Intent { request: wrong }
            )
            .is_err()
        );
    }
    // Rebinding the request to the original owner does not make it a consumer.
    let account::Event::Acknowledged {
        request: birth,
        pid,
        created,
        ..
    } = &accounts[0][4]
    else {
        unreachable!()
    };
    let mut wrong = request.clone();
    wrong.birth = birth.clone();
    wrong.birth.purpose = Purpose::TrustedSelfCheckV1;
    wrong.pid = *pid;
    wrong.created = *created;
    assert!(
        append(
            disk.sql(),
            &plan,
            Role::Consumers,
            Event::Intent { request: wrong }
        )
        .is_err()
    );
    assert!(saved(disk.sql()).is_empty());
}

#[test]
fn paired_resume_retirement_keeps_prefix_but_never_allows_more_work() {
    let plan = plan();
    for cut in 0..=3 {
        let mut disk = Disk::new(&plan);
        let expected = disk.prepared(&plan);
        prefix(disk.sql(), &plan, &expected[..cut]);
        for event in [paired::Event::RetireIntent, paired::Event::Retired] {
            paired::append(disk.sql(), &plan, Role::Consumers, event).unwrap();
            assert!(replay(disk.sql(), &plan, Role::Consumers).unwrap() == expected[..cut]);
            let error = append(
                disk.sql(),
                &plan,
                Role::Consumers,
                expected[cut.min(2)].clone(),
            )
            .err()
            .unwrap();
            assert!(error.to_string().contains("live Ready phase"));
        }
        paired::append(disk.sql(), &plan, Role::Owner, paired::Event::RetireIntent).unwrap();
        paired::append(disk.sql(), &plan, Role::Owner, paired::Event::Retired).unwrap();
        disk.reopen();
        assert!(replay(disk.sql(), &plan, Role::Consumers).unwrap() == expected[..cut]);
    }
}

#[test]
fn paired_resume_schema_and_rows_are_exact_and_immutable() {
    let plan = plan();
    let disk = Disk::new(&plan);
    let expected = disk.prepared(&plan);
    prefix(disk.sql(), &plan, &expected[..1]);
    for statement in [
        "UPDATE candidate_paired_account_resume_events SET event_json='{}'",
        "DELETE FROM candidate_paired_account_resume_events",
        "INSERT OR REPLACE INTO candidate_paired_account_resume_events VALUES(1,0,'{}')",
        "INSERT INTO candidate_paired_account_resume_events VALUES(0,0,'{}')",
        "INSERT INTO candidate_paired_account_resume_events VALUES(1,3,'{}')",
    ] {
        assert!(disk.sql().execute_batch(statement).is_err());
    }
    assert!(create_schema(disk.sql()).is_err());
    assert!(replay(disk.sql(), &plan, Role::Consumers).unwrap() == expected[..1]);
    for index in 1..4 {
        let trigger = [
            "",
            "paired_account_resume_no_update",
            "paired_account_resume_no_delete",
            "paired_account_resume_no_replace",
        ][index];
        disk.sql()
            .execute_batch(&format!("DROP TRIGGER {trigger}"))
            .unwrap();
        assert!(require_schema(disk.sql()).is_err());
        assert!(replay(disk.sql(), &plan, Role::Consumers).is_err());
        disk.sql().execute_batch(SCHEMA[index]).unwrap();
        require_schema(disk.sql()).unwrap();
    }
    disk.sql().execute_batch("CREATE INDEX unexpected_resume_index ON candidate_paired_account_resume_events(event_json)").unwrap();
    assert!(require_schema(disk.sql()).is_err());
    assert!(replay(disk.sql(), &plan, Role::Consumers).is_err());
    disk.sql()
        .execute_batch("DROP INDEX unexpected_resume_index")
        .unwrap();
    disk.sql()
        .execute_batch("DROP TABLE candidate_paired_account_resume_events")
        .unwrap();
    assert!(require_schema(disk.sql()).is_err());
    assert!(replay(disk.sql(), &plan, Role::Consumers).is_err());
}

#[test]
fn paired_resume_malformed_gap_nontext_and_oversize_evidence_is_preserved() {
    let plan = plan();
    for case in 0..10 {
        let disk = Disk::new(&plan);
        let expected = disk.prepared(&plan);
        let intent = serde_json::to_value(&expected[0]).unwrap();
        let mut value = intent.clone();
        let (role, sequence, json) = match case {
            0 => (1, 1, intent.to_string()),
            1 => (1, 0, "{".into()),
            2 => {
                value["command"] = "forbidden".into();
                (1, 0, value.to_string())
            }
            3 => {
                value["request"]["extra"] = true.into();
                (1, 0, value.to_string())
            }
            4 => {
                value["request"].as_object_mut().unwrap().remove("nonce");
                (1, 0, value.to_string())
            }
            5 => (
                1,
                0,
                format!("{{\"kind\":\"intent\",{}", &intent.to_string()[1..]),
            ),
            6 => (1, 0, "Ã©".repeat(MAX_BYTES / 2 + 1)),
            7 => (0, 0, intent.to_string()),
            8 => (1, -1, intent.to_string()),
            _ => (1, 0, serde_json::to_string(&expected[1]).unwrap()),
        };
        disk.sql()
            .execute_batch("PRAGMA ignore_check_constraints=ON")
            .unwrap();
        disk.sql()
            .execute(
                "INSERT INTO candidate_paired_account_resume_events VALUES(?1,?2,?3)",
                params![role, sequence, json],
            )
            .unwrap();
        disk.sql()
            .execute_batch("PRAGMA ignore_check_constraints=OFF")
            .unwrap();
        let before = saved(disk.sql());
        assert!(
            replay(disk.sql(), &plan, Role::Consumers).is_err(),
            "case {case}"
        );
        assert!(append(disk.sql(), &plan, Role::Consumers, expected[0].clone()).is_err());
        assert_eq!(saved(disk.sql()), before);
    }
    let disk = Disk::new(&plan);
    let expected = disk.prepared(&plan);
    let bytes = serde_json::to_vec(&expected[0]).unwrap();
    disk.sql()
        .execute(
            "INSERT INTO candidate_paired_account_resume_events VALUES(1,0,?1)",
            [bytes.clone()],
        )
        .unwrap();
    assert!(replay(disk.sql(), &plan, Role::Consumers).is_err());
    let (kind, stored): (String, Vec<u8>) = disk
        .sql()
        .query_row(
            "SELECT typeof(event_json),event_json FROM candidate_paired_account_resume_events",
            [],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .unwrap();
    assert_eq!(kind, "blob");
    assert_eq!(stored, bytes);
    let disk = Disk::new(&plan);
    let expected = disk.prepared(&plan);
    prefix(disk.sql(), &plan, &expected);
    disk.sql()
        .execute_batch("PRAGMA ignore_check_constraints=ON")
        .unwrap();
    disk.sql()
        .execute(
            "INSERT INTO candidate_paired_account_resume_events VALUES(1,3,?1)",
            [serde_json::to_string(&expected[2]).unwrap()],
        )
        .unwrap();
    disk.sql()
        .execute_batch("PRAGMA ignore_check_constraints=OFF")
        .unwrap();
    let before = saved(disk.sql());
    assert!(replay(disk.sql(), &plan, Role::Consumers).is_err());
    assert_eq!(saved(disk.sql()), before);
}

#[test]
fn paired_resume_wrong_plan_or_nondurable_connection_cannot_append() {
    let plan = plan();
    let disk = Disk::new(&plan);
    let expected = disk.prepared(&plan);
    let mut other = plan.clone();
    other.operation = random_id().unwrap();
    assert!(replay(disk.sql(), &other, Role::Consumers).is_err());
    assert!(append(disk.sql(), &other, Role::Consumers, expected[0].clone()).is_err());
    for statement in [
        "BEGIN",
        "PRAGMA synchronous=NORMAL",
        "PRAGMA journal_mode=MEMORY",
    ] {
        disk.sql().execute_batch(statement).unwrap();
        assert!(append(disk.sql(), &plan, Role::Consumers, expected[0].clone()).is_err());
        if !disk.sql().is_autocommit() {
            disk.sql().execute_batch("ROLLBACK").unwrap();
        }
        disk.sql()
            .execute_batch("PRAGMA journal_mode=DELETE; PRAGMA synchronous=FULL")
            .unwrap();
    }
    assert!(saved(disk.sql()).is_empty());
}

#[test]
fn paired_resume_readonly_and_busy_commit_leave_exact_prior_prefix() {
    let plan = plan();
    for cut in 0..3 {
        for fault in ["readonly", "busy-commit"] {
            let mut disk = Disk::new(&plan);
            let expected = disk.prepared(&plan);
            prefix(disk.sql(), &plan, &expected[..cut]);
            let before = saved(disk.sql());
            let reader = Connection::open(&disk.path).unwrap();
            if fault == "readonly" {
                disk.sql().execute_batch("PRAGMA query_only=ON").unwrap();
            } else {
                reader.execute_batch("BEGIN").unwrap();
                let _: String = reader
                    .query_row(
                        "SELECT plan_json FROM candidate_paired_bootstrap_plan",
                        [],
                        |r| r.get(0),
                    )
                    .unwrap();
            }
            let error = append(disk.sql(), &plan, Role::Consumers, expected[cut].clone())
                .err()
                .unwrap();
            assert_eq!(
                sqlite_code(&error),
                Some(if fault == "readonly" {
                    rusqlite::ErrorCode::ReadOnly
                } else {
                    rusqlite::ErrorCode::DatabaseBusy
                })
            );
            if fault == "readonly" {
                disk.sql().execute_batch("PRAGMA query_only=OFF").unwrap();
            } else {
                assert!(error.to_string().contains("commit uncertain"));
                reader.execute_batch("ROLLBACK").unwrap();
            }
            drop(reader);
            assert!(disk.sql().is_autocommit());
            assert_eq!(saved(disk.sql()), before);
            disk.reopen();
            assert!(replay(disk.sql(), &plan, Role::Consumers).unwrap() == expected[..cut]);
            // A cold prefix is evidence only. Do not retry the failed action.
        }
    }
}
