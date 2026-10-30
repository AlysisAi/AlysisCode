//! Disk-only profile coordination. These fixtures create no native profile,
//! process, account, token, job, ACL or live cleanup capability.
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
fn purpose(plan: &Plan) -> Purpose {
    Purpose::TrustedPairedProfileV1 {
        container: plan.execution.identity.container.clone(),
        package_sid: plan.execution.identity.package_sid.clone(),
        policy: 1,
    }
}

struct Disk {
    connection: Option<Connection>,
    path: std::path::PathBuf,
}
impl Disk {
    fn new(plan: &Plan) -> Self {
        let path = crate::util::test_temp_dir()
            .join(format!("paired-profile-{}.db", random_id().unwrap()));
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
        // Schema creation belongs to the original parent-plan transaction.
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
    fn owner_only(&self, plan: &Plan) -> [Vec<account::Event>; 2] {
        let accounts = [
            account_events(plan, Role::Owner, purpose(plan)),
            account_events(plan, Role::Consumers, Purpose::TrustedSelfCheckV1),
        ];
        host_prefix(self.sql(), plan, Role::Owner, 7);
        account_prefix(self.sql(), plan, Role::Owner, &accounts[0]);
        accounts
    }
    fn prepared(&self, plan: &Plan) -> Vec<Event> {
        let accounts = self.owner_only(plan);
        host_prefix(self.sql(), plan, Role::Consumers, 7);
        account_prefix(self.sql(), plan, Role::Consumers, &accounts[1]);
        events(plan, &accounts)
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
fn host_prefix(sql: &Connection, plan: &Plan, role: Role, count: usize) {
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
    ]
    .into_iter()
    .take(count)
    {
        paired::append(sql, plan, role, event).unwrap();
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
fn account_prefix(sql: &Connection, plan: &Plan, role: Role, events: &[account::Event]) {
    for event in events {
        account::append(sql, plan, role, event.clone()).unwrap();
    }
}
fn events(plan: &Plan, accounts: &[Vec<account::Event>; 2]) -> Vec<Event> {
    let account::Event::Acknowledged {
        request: birth,
        pid,
        created,
        ..
    } = &accounts[0][4]
    else {
        unreachable!()
    };
    let account::Event::BirthIntent { request: consumer } = &accounts[1][2] else {
        unreachable!()
    };
    let request = probe::Request {
        birth: birth.clone(),
        nonce: random_id().unwrap(),
        pid: *pid,
        created: *created,
    };
    let folder = [6; 24];
    let nonce = random_id().unwrap();
    let retirement = plan.roles[Role::Consumers.index()].retire_nonce.clone();
    vec![
        Event::ResumeIntent {
            request: request.clone(),
        },
        Event::Resumed {
            request: request.clone(),
        },
        Event::CreateIntent {
            request: request.clone(),
        },
        Event::Created {
            request: request.clone(),
            folder,
        },
        Event::UseIntent {
            request: request.clone(),
            folder,
            nonce: nonce.clone(),
            consumer: consumer.clone(),
        },
        Event::Using {
            request: request.clone(),
            folder,
            nonce: nonce.clone(),
            consumer: consumer.clone(),
        },
        Event::CaptureIntent {
            request: request.clone(),
            folder,
            nonce: nonce.clone(),
            consumer: consumer.clone(),
            retirement: retirement.clone(),
        },
        Event::Captured {
            request: request.clone(),
            folder,
            nonce: nonce.clone(),
            consumer: consumer.clone(),
            retirement: retirement.clone(),
        },
        Event::RemoveIntent {
            request: request.clone(),
            folder,
            nonce: nonce.clone(),
            consumer: consumer.clone(),
            retirement: retirement.clone(),
        },
        Event::Removed {
            request: request.clone(),
            folder,
            nonce,
            consumer: consumer.clone(),
            retirement,
        },
        Event::Exited {
            request,
            code: probe::EXIT_CODE,
        },
    ]
}
fn retire_consumer(sql: &Connection, plan: &Plan) {
    paired::append(sql, plan, Role::Consumers, paired::Event::RetireIntent).unwrap();
    paired::append(sql, plan, Role::Consumers, paired::Event::Retired).unwrap();
}
fn prefix(sql: &Connection, plan: &Plan, events: &[Event]) {
    for event in events {
        if matches!(event, Event::CaptureIntent { .. }) {
            retire_consumer(sql, plan);
        }
        append(sql, plan, Role::Owner, event.clone()).unwrap();
    }
}
fn saved(sql: &Connection) -> Vec<(i64, i64, String)> {
    sql.prepare("SELECT role,sequence,event_json FROM candidate_paired_profile_events ORDER BY role,sequence").unwrap()
        .query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?))).unwrap()
        .collect::<std::result::Result<_, _>>().unwrap()
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
fn paired_profile_consumer_resume_requires_using_and_distinct_resume_nonce() {
    use super::super::resume_journal as resume;
    for count in 0..=6 {
        let plan = plan();
        let disk = Disk::new(&plan);
        let events = disk.prepared(&plan);
        prefix(disk.sql(), &plan, &events[..count]);
        let Event::Using {
            request: owner,
            nonce: used,
            consumer,
            ..
        } = &events[5]
        else {
            unreachable!()
        };
        let request = probe::Request {
            birth: consumer.clone(),
            nonce: random_id().unwrap(),
            pid: plan.execution.pid + 11,
            created: plan.execution.created + 11,
        };
        let attempt = resume::append(
            disk.sql(),
            &plan,
            Role::Consumers,
            resume::Event::Intent {
                request: request.clone(),
            },
        );
        if count < 6 {
            assert!(attempt.is_err());
            continue;
        }
        attempt.unwrap();
        resume::append(
            disk.sql(),
            &plan,
            Role::Consumers,
            resume::Event::Resumed {
                request: request.clone(),
            },
        )
        .unwrap();
        resume::append(
            disk.sql(),
            &plan,
            Role::Consumers,
            resume::Event::Exited {
                request: request.clone(),
                code: probe::EXIT_CODE,
            },
        )
        .unwrap();
        prefix(disk.sql(), &plan, &events[6..]);
        assert_eq!(
            resume::replay(disk.sql(), &plan, Role::Consumers)
                .unwrap()
                .len(),
            3
        );
        for nonce in [&owner.nonce, used] {
            let fresh = Disk::new(&plan);
            // Preserve exact account births from the original source rows.
            for role in [Role::Owner, Role::Consumers] {
                host_prefix(fresh.sql(), &plan, role, 7);
                let original = account::replay(disk.sql(), &plan, role).unwrap();
                account_prefix(fresh.sql(), &plan, role, &original);
            }
            prefix(fresh.sql(), &plan, &events[..6]);
            let wrong = probe::Request {
                nonce: nonce.clone(),
                ..request.clone()
            };
            assert!(
                resume::append(
                    fresh.sql(),
                    &plan,
                    Role::Consumers,
                    resume::Event::Intent { request: wrong }
                )
                .is_err()
            );
            assert!(
                resume::replay(fresh.sql(), &plan, Role::Consumers)
                    .unwrap()
                    .is_empty()
            );
        }
    }
}

#[test]
fn paired_profile_all_prefixes_reopen_and_refuse_reordering_or_consumer_role() {
    let plan = plan();
    for cut in 0..=11 {
        let mut disk = Disk::new(&plan);
        let expected = disk.prepared(&plan);
        prefix(disk.sql(), &plan, &expected[..cut]);
        disk.reopen();
        assert!(replay(disk.sql(), &plan, Role::Owner).unwrap() == expected[..cut]);
        for (index, event) in expected.iter().enumerate() {
            if index != cut {
                assert!(append(disk.sql(), &plan, Role::Owner, event.clone()).is_err());
            }
        }
        assert!(replay(disk.sql(), &plan, Role::Consumers).is_err());
        assert!(
            append(
                disk.sql(),
                &plan,
                Role::Consumers,
                expected[cut.min(10)].clone()
            )
            .is_err()
        );
        assert!(replay(disk.sql(), &plan, Role::Owner).unwrap() == expected[..cut]);
    }
}

#[test]
fn paired_profile_writer_journal_refuses_wrong_owner_folder_and_use_before_resume() {
    use crate::account_control::bootstrap::entry::credential_wire::child::FixtureWriterBinding;
    for fault in 0..7 {
        let plan = plan();
        let disk = Disk::new(&plan);
        let mut accounts = disk.owner_only(&plan);
        let mut expected = events(&plan, &accounts);
        let Event::UseIntent {
            request,
            folder,
            nonce,
            ..
        } = &expected[4]
        else {
            unreachable!()
        };
        let mut binding = FixtureWriterBinding {
            container: plan.execution.identity.container.clone(),
            package_sid: plan.execution.identity.package_sid.clone(),
            folder: *folder,
            owner_birth: request.birth.nonce.clone(),
            owner_resume: request.nonce.clone(),
            owner_pid: request.pid,
            owner_created: request.created,
            use_nonce: nonce.clone(),
            unsupported_registry: false,
            hold_rounds: 0,
            descendant_files: false,
        };
        match fault {
            0 => {}
            1 => binding.folder[0] ^= 1,
            2 => binding.owner_birth = random_id().unwrap(),
            3 => binding.owner_resume = random_id().unwrap(),
            4 => binding.owner_pid += 1,
            5 => binding.owner_created += 1,
            6 => binding.use_nonce = random_id().unwrap(),
            _ => unreachable!(),
        }
        for event in &mut accounts[1] {
            match event {
                account::Event::BirthIntent { request }
                | account::Event::Born { request, .. }
                | account::Event::Acknowledged { request, .. } => {
                    request.purpose = Purpose::FixtureProfileWriterV1 {
                        binding: binding.clone(),
                    };
                }
                _ => {}
            }
        }
        let account::Event::Acknowledged {
            request: consumer,
            pid,
            created,
            ..
        } = &accounts[1][4]
        else {
            unreachable!()
        };
        for event in &mut expected {
            match event {
                Event::UseIntent {
                    consumer: value, ..
                }
                | Event::Using {
                    consumer: value, ..
                }
                | Event::CaptureIntent {
                    consumer: value, ..
                }
                | Event::Captured {
                    consumer: value, ..
                }
                | Event::RemoveIntent {
                    consumer: value, ..
                }
                | Event::Removed {
                    consumer: value, ..
                } => *value = consumer.clone(),
                _ => {}
            }
        }
        prefix(disk.sql(), &plan, &expected[..4]);
        host_prefix(disk.sql(), &plan, Role::Consumers, 7);
        account_prefix(disk.sql(), &plan, Role::Consumers, &accounts[1]);
        let resume = super::super::resume_journal::Event::Intent {
            request: probe::Request {
                birth: consumer.clone(),
                nonce: random_id().unwrap(),
                pid: *pid,
                created: *created,
            },
        };
        assert!(
            super::super::resume_journal::append(
                disk.sql(),
                &plan,
                Role::Consumers,
                resume.clone()
            )
            .is_err()
        );
        let before = saved(disk.sql());
        let result = append(disk.sql(), &plan, Role::Owner, expected[4].clone());
        if fault == 0 {
            result.unwrap();
            assert!(
                super::super::resume_journal::append(
                    disk.sql(),
                    &plan,
                    Role::Consumers,
                    resume.clone()
                )
                .is_err()
            );
            append(disk.sql(), &plan, Role::Owner, expected[5].clone()).unwrap();
            super::super::resume_journal::append(disk.sql(), &plan, Role::Consumers, resume)
                .unwrap();
        } else {
            assert!(result.is_err(), "fault {fault}");
            assert_eq!(saved(disk.sql()), before);
        }
    }
}

#[test]
fn paired_profile_owner_creates_before_consumer_birth_but_use_waits_for_acknowledgment() {
    let plan = plan();
    for cut in 0..=5 {
        let disk = Disk::new(&plan);
        let accounts = disk.owner_only(&plan);
        let expected = events(&plan, &accounts);
        prefix(disk.sql(), &plan, &expected[..4]);
        assert!(
            paired::replay(disk.sql(), &plan, Role::Consumers)
                .unwrap()
                .is_empty()
        );
        assert!(
            account::replay(disk.sql(), &plan, Role::Consumers)
                .unwrap()
                .is_empty()
        );
        assert!(append(disk.sql(), &plan, Role::Owner, expected[4].clone()).is_err());
        host_prefix(disk.sql(), &plan, Role::Consumers, 7);
        account_prefix(disk.sql(), &plan, Role::Consumers, &accounts[1][..cut]);
        let result = append(disk.sql(), &plan, Role::Owner, expected[4].clone());
        assert_eq!(result.is_ok(), cut == 5);
        assert!(
            replay(disk.sql(), &plan, Role::Owner).unwrap()
                == expected[..if cut == 5 { 5 } else { 4 }]
        );
    }
    for count in 0..7 {
        let disk = Disk::new(&plan);
        let accounts = disk.owner_only(&plan);
        let expected = events(&plan, &accounts);
        prefix(disk.sql(), &plan, &expected[..4]);
        host_prefix(disk.sql(), &plan, Role::Consumers, count);
        assert!(append(disk.sql(), &plan, Role::Owner, expected[4].clone()).is_err());
    }
}

#[test]
fn paired_profile_requires_exact_original_owner_request_and_purpose() {
    let plan = plan();
    for count in 0..5 {
        let disk = Disk::new(&plan);
        host_prefix(disk.sql(), &plan, Role::Owner, 7);
        let accounts = [
            account_events(&plan, Role::Owner, purpose(&plan)),
            account_events(&plan, Role::Consumers, Purpose::TrustedSelfCheckV1),
        ];
        account_prefix(disk.sql(), &plan, Role::Owner, &accounts[0][..count]);
        let expected = events(&plan, &accounts);
        assert!(append(disk.sql(), &plan, Role::Owner, expected[0].clone()).is_err());
        disk.sql()
            .execute(
                "INSERT INTO candidate_paired_profile_events VALUES(0,0,?1)",
                [serde_json::to_string(&expected[0]).unwrap()],
            )
            .unwrap();
        assert!(replay(disk.sql(), &plan, Role::Owner).is_err());
    }
    let disk = Disk::new(&plan);
    let expected = disk.prepared(&plan);
    let request = expected[0].request();
    let changes: Vec<Box<dyn Fn(&mut probe::Request)>> = vec![
        Box::new(|r| r.pid += 1),
        Box::new(|r| r.created += 1),
        Box::new(|r| r.birth.nonce = random_id().unwrap()),
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
        Box::new(|r| {
            if let Purpose::TrustedPairedProfileV1 { policy, .. } = &mut r.birth.purpose {
                *policy = 2;
            }
        }),
        Box::new(|r| {
            if let Purpose::TrustedPairedProfileV1 { container, .. } = &mut r.birth.purpose {
                container.push('x');
            }
        }),
        Box::new(|r| {
            if let Purpose::TrustedPairedProfileV1 { package_sid, .. } = &mut r.birth.purpose {
                package_sid.push_str("-1");
            }
        }),
        Box::new(|r| r.birth.purpose = Purpose::TrustedSelfCheckV1),
        Box::new(|r| r.birth.purpose = Purpose::ObserveOnly),
    ];
    for change in changes {
        let mut wrong = request.clone();
        change(&mut wrong);
        assert!(
            append(
                disk.sql(),
                &plan,
                Role::Owner,
                Event::ResumeIntent { request: wrong }
            )
            .is_err()
        );
    }
    assert!(saved(disk.sql()).is_empty());
    // Even a fully acknowledged ordinary owner cannot acquire profile purpose.
    let disk = Disk::new(&plan);
    host_prefix(disk.sql(), &plan, Role::Owner, 7);
    let accounts = [
        account_events(&plan, Role::Owner, Purpose::ObserveOnly),
        account_events(&plan, Role::Consumers, Purpose::TrustedSelfCheckV1),
    ];
    account_prefix(disk.sql(), &plan, Role::Owner, &accounts[0]);
    assert!(
        append(
            disk.sql(),
            &plan,
            Role::Owner,
            events(&plan, &accounts)[0].clone()
        )
        .is_err()
    );
}

#[test]
fn paired_profile_resume_and_use_nonces_are_fresh_including_later_consumer_birth() {
    let plan = plan();
    let disk = Disk::new(&plan);
    let expected = disk.prepared(&plan);
    let accounts = account::inventory(disk.sql(), &plan).unwrap();
    let forbidden = known_nonces(&plan, &accounts).unwrap();
    for nonce in &forbidden {
        let mut request = expected[0].request().clone();
        request.nonce = nonce.clone();
        assert!(
            append(
                disk.sql(),
                &plan,
                Role::Owner,
                Event::ResumeIntent { request }
            )
            .is_err()
        );
    }
    prefix(disk.sql(), &plan, &expected[..4]);
    for nonce in forbidden
        .into_iter()
        .chain([expected[0].request().nonce.clone()])
    {
        let mut wrong = expected[4].clone();
        if let Event::UseIntent { nonce: value, .. } = &mut wrong {
            *value = nonce;
        }
        assert!(append(disk.sql(), &plan, Role::Owner, wrong).is_err());
    }
    // The owner can begin first, but later account nonces must invalidate replay
    // before any UseIntent. No new account append hook is needed for this check.
    for reuse_logon in [false, true] {
        let disk = Disk::new(&plan);
        let mut accounts = disk.owner_only(&plan);
        let expected = events(&plan, &accounts);
        prefix(disk.sql(), &plan, &expected[..4]);
        let nonce = expected[0].request().nonce.clone();
        for event in &mut accounts[1] {
            match event {
                account::Event::Intent { selection } | account::Event::Delivered { selection } => {
                    if reuse_logon {
                        selection.logon = nonce.clone();
                    }
                }
                account::Event::BirthIntent { request }
                | account::Event::Born { request, .. }
                | account::Event::Acknowledged { request, .. } => {
                    if reuse_logon {
                        request.selection.logon = nonce.clone();
                    } else {
                        request.nonce = nonce.clone();
                    }
                }
            }
        }
        host_prefix(disk.sql(), &plan, Role::Consumers, 7);
        account_prefix(disk.sql(), &plan, Role::Consumers, &accounts[1]);
        let before = saved(disk.sql());
        assert!(replay(disk.sql(), &plan, Role::Owner).is_err());
        assert!(
            append(
                disk.sql(),
                &plan,
                Role::Owner,
                events(&plan, &accounts)[4].clone()
            )
            .is_err()
        );
        assert_eq!(saved(disk.sql()), before);
    }
}

#[test]
fn paired_profile_use_binds_folder_and_consumer_and_must_precede_resume() {
    let plan = plan();
    for stage in [4, 5] {
        let disk = Disk::new(&plan);
        let expected = disk.prepared(&plan);
        prefix(disk.sql(), &plan, &expected[..stage]);
        for fault in 0..4 {
            let mut wrong = expected[stage].clone();
            let (folder, consumer, nonce) = match &mut wrong {
                Event::UseIntent {
                    folder,
                    consumer,
                    nonce,
                    ..
                }
                | Event::Using {
                    folder,
                    consumer,
                    nonce,
                    ..
                } => (folder, consumer, nonce),
                _ => unreachable!(),
            };
            match fault {
                0 => folder[0] ^= 1,
                1 => consumer.nonce = random_id().unwrap(),
                2 => consumer.purpose = Purpose::ObserveOnly,
                _ => {
                    if stage == 4 {
                        consumer.selection.bootstrap_pid += 1;
                    } else {
                        *nonce = random_id().unwrap();
                    }
                }
            }
            assert!(append(disk.sql(), &plan, Role::Owner, wrong).is_err());
        }
        // Any resume evidence blocks use admission; this deliberately does not
        // invoke consumer replay, so that later dependency can remain acyclic.
        disk.sql()
            .execute(
                "INSERT INTO candidate_paired_account_resume_events VALUES(1,0,'{}')",
                [],
            )
            .unwrap();
        let error = append(disk.sql(), &plan, Role::Owner, expected[stage].clone())
            .err()
            .unwrap();
        assert!(error.to_string().contains("precede consumer resume"));
        assert!(replay(disk.sql(), &plan, Role::Owner).unwrap() == expected[..stage]);
    }
    let disk = Disk::new(&plan);
    let expected = disk.prepared(&plan);
    prefix(disk.sql(), &plan, &expected[..3]);
    assert!(
        append(
            disk.sql(),
            &plan,
            Role::Owner,
            Event::Created {
                request: expected[0].request().clone(),
                folder: [0; 24]
            }
        )
        .is_err()
    );
    // Acknowledged observe-only consumers do not become profile users.
    let disk = Disk::new(&plan);
    let mut accounts = disk.owner_only(&plan);
    accounts[1] = account_events(&plan, Role::Consumers, Purpose::ObserveOnly);
    host_prefix(disk.sql(), &plan, Role::Consumers, 7);
    account_prefix(disk.sql(), &plan, Role::Consumers, &accounts[1]);
    let expected = events(&plan, &accounts);
    prefix(disk.sql(), &plan, &expected[..4]);
    assert!(append(disk.sql(), &plan, Role::Owner, expected[4].clone()).is_err());
}

#[test]
fn paired_profile_capture_requires_complete_consumer_retirement_and_exact_use_binding() {
    let plan = plan();
    let disk = Disk::new(&plan);
    let expected = disk.prepared(&plan);
    prefix(disk.sql(), &plan, &expected[..6]);
    assert!(append(disk.sql(), &plan, Role::Owner, expected[6].clone()).is_err());
    paired::append(
        disk.sql(),
        &plan,
        Role::Consumers,
        paired::Event::RetireIntent,
    )
    .unwrap();
    assert!(append(disk.sql(), &plan, Role::Owner, expected[6].clone()).is_err());
    paired::append(disk.sql(), &plan, Role::Consumers, paired::Event::Retired).unwrap();
    for stage in 6..10 {
        for fault in 0..5 {
            let mut wrong = expected[stage].clone();
            let (folder, nonce, consumer, retirement) = match &mut wrong {
                Event::CaptureIntent {
                    folder,
                    nonce,
                    consumer,
                    retirement,
                    ..
                }
                | Event::Captured {
                    folder,
                    nonce,
                    consumer,
                    retirement,
                    ..
                }
                | Event::RemoveIntent {
                    folder,
                    nonce,
                    consumer,
                    retirement,
                    ..
                }
                | Event::Removed {
                    folder,
                    nonce,
                    consumer,
                    retirement,
                    ..
                } => (folder, nonce, consumer, retirement),
                _ => unreachable!(),
            };
            match fault {
                0 => folder[0] ^= 1,
                1 => *nonce = random_id().unwrap(),
                2 => consumer.nonce = random_id().unwrap(),
                3 => *retirement = random_id().unwrap(),
                _ => *retirement = plan.roles[Role::Owner.index()].retire_nonce.clone(),
            }
            assert!(append(disk.sql(), &plan, Role::Owner, wrong).is_err());
        }
        append(disk.sql(), &plan, Role::Owner, expected[stage].clone()).unwrap();
    }
    for code in [0, 1, probe::EXIT_CODE + 1] {
        assert!(
            append(
                disk.sql(),
                &plan,
                Role::Owner,
                Event::Exited {
                    request: expected[0].request().clone(),
                    code
                }
            )
            .is_err()
        );
    }
    let mut wrong = expected[0].request().clone();
    wrong.nonce = random_id().unwrap();
    assert!(
        append(
            disk.sql(),
            &plan,
            Role::Owner,
            Event::Exited {
                request: wrong,
                code: probe::EXIT_CODE
            }
        )
        .is_err()
    );
    append(disk.sql(), &plan, Role::Owner, expected[10].clone()).unwrap();
}

#[test]
fn paired_profile_retirement_preserves_cold_prefixes_but_blocks_new_effects() {
    let plan = plan();
    for cut in 0..=11 {
        let mut disk = Disk::new(&plan);
        let expected = disk.prepared(&plan);
        prefix(disk.sql(), &plan, &expected[..cut]);
        if cut <= 6 {
            retire_consumer(disk.sql(), &plan);
        }
        if cut == 4 || cut == 5 {
            let error = append(disk.sql(), &plan, Role::Owner, expected[cut].clone())
                .err()
                .unwrap();
            assert!(error.to_string().contains("live consumer Ready"));
        }
        for event in [paired::Event::RetireIntent, paired::Event::Retired] {
            paired::append(disk.sql(), &plan, Role::Owner, event).unwrap();
            assert!(replay(disk.sql(), &plan, Role::Owner).unwrap() == expected[..cut]);
            let error = append(
                disk.sql(),
                &plan,
                Role::Owner,
                expected[cut.min(10)].clone(),
            )
            .err()
            .unwrap();
            assert!(error.to_string().contains("live owner Ready"));
        }
        disk.reopen();
        assert!(replay(disk.sql(), &plan, Role::Owner).unwrap() == expected[..cut]);
    }
}

#[test]
fn paired_profile_schema_and_rows_are_exact_immutable_and_bounded() {
    let plan = plan();
    let disk = Disk::new(&plan);
    let expected = disk.prepared(&plan);
    prefix(disk.sql(), &plan, &expected[..1]);
    for statement in [
        "UPDATE candidate_paired_profile_events SET event_json='{}'",
        "DELETE FROM candidate_paired_profile_events",
        "INSERT OR REPLACE INTO candidate_paired_profile_events VALUES(0,0,'{}')",
        "INSERT INTO candidate_paired_profile_events VALUES(1,0,'{}')",
        "INSERT INTO candidate_paired_profile_events VALUES(0,11,'{}')",
    ] {
        assert!(disk.sql().execute_batch(statement).is_err());
    }
    assert!(create_schema(disk.sql()).is_err());
    for index in 1..4 {
        let name = [
            "",
            "paired_profile_no_update",
            "paired_profile_no_delete",
            "paired_profile_no_replace",
        ][index];
        disk.sql()
            .execute_batch(&format!("DROP TRIGGER {name}"))
            .unwrap();
        assert!(require_schema(disk.sql()).is_err());
        assert!(replay(disk.sql(), &plan, Role::Owner).is_err());
        disk.sql().execute_batch(SCHEMA[index]).unwrap();
    }
    disk.sql()
        .execute_batch(
            "CREATE INDEX unexpected_profile_index ON candidate_paired_profile_events(event_json)",
        )
        .unwrap();
    assert!(require_schema(disk.sql()).is_err());
    disk.sql()
        .execute_batch(
            "DROP INDEX unexpected_profile_index; DROP TABLE candidate_paired_profile_events",
        )
        .unwrap();
    assert!(require_schema(disk.sql()).is_err());
    let disk = Disk::new(&plan);
    let expected = disk.prepared(&plan);
    prefix(disk.sql(), &plan, &expected);
    disk.sql()
        .execute_batch("PRAGMA ignore_check_constraints=ON")
        .unwrap();
    disk.sql()
        .execute(
            "INSERT INTO candidate_paired_profile_events VALUES(0,11,?1)",
            [serde_json::to_string(&expected[10]).unwrap()],
        )
        .unwrap();
    disk.sql()
        .execute_batch("PRAGMA ignore_check_constraints=OFF")
        .unwrap();
    let before = saved(disk.sql());
    assert!(replay(disk.sql(), &plan, Role::Owner).is_err());
    assert_eq!(saved(disk.sql()), before);
}

#[test]
fn paired_profile_malformed_gap_unknown_nontext_and_oversize_rows_are_preserved() {
    let plan = plan();
    for case in 0..11 {
        let disk = Disk::new(&plan);
        let expected = disk.prepared(&plan);
        let intent = serde_json::to_value(&expected[0]).unwrap();
        let mut value = intent.clone();
        let (role, sequence, json) = match case {
            0 => (0, 1, intent.to_string()),
            1 => (0, 0, "{".into()),
            2 => {
                value["command"] = true.into();
                (0, 0, value.to_string())
            }
            3 => {
                value["request"]["extra"] = true.into();
                (0, 0, value.to_string())
            }
            4 => {
                value["request"]["birth"]["purpose"]["trusted_paired_profile_v1"]["extra"] =
                    true.into();
                (0, 0, value.to_string())
            }
            5 => {
                value["request"].as_object_mut().unwrap().remove("nonce");
                (0, 0, value.to_string())
            }
            6 => (
                0,
                0,
                format!("{{\"kind\":\"resume_intent\",{}", &intent.to_string()[1..]),
            ),
            7 => (0, 0, "Ã©".repeat(MAX_BYTES / 2 + 1)),
            8 => (1, 0, intent.to_string()),
            9 => (0, -1, intent.to_string()),
            _ => (0, 0, serde_json::to_string(&expected[1]).unwrap()),
        };
        disk.sql()
            .execute_batch("PRAGMA ignore_check_constraints=ON")
            .unwrap();
        disk.sql()
            .execute(
                "INSERT INTO candidate_paired_profile_events VALUES(?1,?2,?3)",
                params![role, sequence, json],
            )
            .unwrap();
        disk.sql()
            .execute_batch("PRAGMA ignore_check_constraints=OFF")
            .unwrap();
        let before = saved(disk.sql());
        assert!(
            replay(disk.sql(), &plan, Role::Owner).is_err(),
            "case {case}"
        );
        assert!(append(disk.sql(), &plan, Role::Owner, expected[0].clone()).is_err());
        assert_eq!(saved(disk.sql()), before);
    }
    let disk = Disk::new(&plan);
    let expected = disk.prepared(&plan);
    let bytes = serde_json::to_vec(&expected[0]).unwrap();
    disk.sql()
        .execute(
            "INSERT INTO candidate_paired_profile_events VALUES(0,0,?1)",
            [bytes.clone()],
        )
        .unwrap();
    assert!(replay(disk.sql(), &plan, Role::Owner).is_err());
    let stored: Vec<u8> = disk
        .sql()
        .query_row(
            "SELECT event_json FROM candidate_paired_profile_events",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(stored, bytes);
}

#[test]
fn paired_profile_readonly_and_busy_commit_preserve_each_original_prefix() {
    let plan = plan();
    for cut in 0..11 {
        for fault in ["readonly", "busy-commit"] {
            let mut disk = Disk::new(&plan);
            let expected = disk.prepared(&plan);
            prefix(disk.sql(), &plan, &expected[..cut]);
            if cut == 6 {
                retire_consumer(disk.sql(), &plan);
            }
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
            let error = append(disk.sql(), &plan, Role::Owner, expected[cut].clone())
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
            assert!(replay(disk.sql(), &plan, Role::Owner).unwrap() == expected[..cut]);
        }
    }
}

#[test]
fn paired_profile_wrong_plan_or_nondurable_connection_never_appends() {
    let plan = plan();
    let disk = Disk::new(&plan);
    let expected = disk.prepared(&plan);
    let mut other = plan.clone();
    other.operation = random_id().unwrap();
    assert!(replay(disk.sql(), &other, Role::Owner).is_err());
    assert!(append(disk.sql(), &other, Role::Owner, expected[0].clone()).is_err());
    for statement in [
        "BEGIN",
        "PRAGMA synchronous=NORMAL",
        "PRAGMA journal_mode=MEMORY",
    ] {
        disk.sql().execute_batch(statement).unwrap();
        assert!(append(disk.sql(), &plan, Role::Owner, expected[0].clone()).is_err());
        if !disk.sql().is_autocommit() {
            disk.sql().execute_batch("ROLLBACK").unwrap();
        }
        disk.sql()
            .execute_batch("PRAGMA journal_mode=DELETE; PRAGMA synchronous=FULL")
            .unwrap();
    }
    assert!(saved(disk.sql()).is_empty());
}
