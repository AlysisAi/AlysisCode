//! Disk evidence only. No process/job, account, SDK profile, desktop, token,
//! RPC directory or ACL is created. Replayed rows cannot construct live guards.
use super::*;
use crate::account_state::execution::paired_plan::native::namespace::station::{
    self, tests as station_tests,
};
use crate::account_state::execution::paired_plan::{
    self as plan_journal,
    tests::{Disk, prefix},
};
use serde_json::json;

fn rows(sql: &Connection, table: &str, values: &[serde_json::Value]) {
    for (sequence, value) in values.iter().enumerate() {
        sql.execute(
            &format!("INSERT INTO {table} VALUES(?1,?2)"),
            params![sequence, value.to_string()],
        )
        .unwrap();
    }
}

fn setup(consumer_rows: usize) -> (Plan, Disk, paired::Request) {
    let (p, d, root) = station_tests::setup();
    let profile_nonce = random_id().unwrap();
    let profile_use = random_id().unwrap();
    let profile = json!({
        "operation":p.operation,"namespace":root.nonce,"nonce":profile_nonce,
        "owner":p.execution.owner,"container":p.execution.identity.container,
        "package_sid":p.execution.identity.package_sid,"policy":1
    });
    rows(
        d.sql(),
        "candidate_paired_host_profile",
        &[
            json!({"kind":"intent","binding":profile}),
            json!({"kind":"created","binding":profile,"folder_id":vec![7;24]}),
            json!({"kind":"use_intent","binding":profile,"folder_id":vec![7;24],"nonce":profile_use}),
        ],
    );
    let profile = host_profile::resource_binding(d.sql(), &p)
        .unwrap()
        .unwrap();
    assert_eq!(profile.birth, profile_nonce);
    assert_eq!(profile.use_nonce.as_deref(), Some(profile_use.as_str()));
    let first = station_tests::intent(d.sql(), &p);
    let station = station_tests::bound(&first);
    station::append(d.sql(), &p, first).unwrap();
    station::append(
        d.sql(),
        &p,
        station::Event::Applied {
            binding: station.clone(),
        },
    )
    .unwrap();
    let desktop_nonce = random_id().unwrap();
    let desktop = json!({
        "operation":p.operation,"nonce":desktop_nonce,"station_nonce":station.nonce,
        "namespace":root.nonce,"profile":profile,"session":p.session,
        "name":format!("alysis-paired-desktop-{}",p.operation),
        "package_sid":p.execution.identity.package_sid,"policy":1
    });
    rows(
        d.sql(),
        "candidate_paired_desktop",
        &[
            json!({"kind":"intent","binding":desktop}),
            json!({"kind":"created","binding":desktop}),
        ],
    );
    assert_eq!(desktop::birth_nonce(d.sql(), &p).unwrap(), desktop_nonce);
    prefix(d.sql(), &p, Role::Owner, 7);
    prefix(d.sql(), &p, Role::Consumers, consumer_rows);
    let request = paired::Request {
        subject: initializer_wire::Request {
            operation: p.operation.clone(),
            nonce: random_id().unwrap(),
            bootstrap_pid: p.execution.pid + 2,
            bootstrap_created: p.execution.created + 2,
            container: p.execution.identity.container.clone(),
            package_sid: p.execution.identity.package_sid.clone(),
            owner: p.execution.owner.clone(),
            session: p.session,
            image_id: [8; 24],
            omit_profile_environment: false,
        },
        resources: resources(d.sql(), &p).unwrap(),
    };
    request.validate().unwrap();
    (p, d, request)
}

fn events(p: &Plan, request: &paired::Request) -> Vec<Event> {
    let birth = Birth {
        pid: p.execution.pid + 10,
        created: p.execution.created + 10,
    };
    let policy =
        crate::account_state::execution::resources::namespace_native::rpc::fixture_access_policy(p);
    policy.validate(p).unwrap();
    let retirements = [
        p.roles[0].retire_nonce.clone(),
        p.roles[1].retire_nonce.clone(),
    ];
    vec![
        Event::Intent {
            request: request.clone(),
        },
        Event::Born {
            request: request.clone(),
            birth: birth.clone(),
        },
        Event::Stopped {
            request: request.clone(),
            birth,
        },
        Event::RpcIntent {
            request: request.clone(),
            policy: policy.clone(),
        },
        Event::RpcGranted {
            request: request.clone(),
            policy,
        },
        Event::ReleaseIntent {
            request: request.clone(),
            retirements: retirements.clone(),
        },
        Event::Released {
            request: request.clone(),
            retirements,
        },
    ]
}

fn retire(sql: &Connection, p: &Plan, role: Role) {
    plan_journal::append(sql, p, role, plan_journal::Event::RetireIntent).unwrap();
    plan_journal::append(sql, p, role, plan_journal::Event::Retired).unwrap();
}

#[test]
fn paired_initializer_every_prefix_reopens_without_live_release_authority() {
    for count in 0..=7 {
        let (p, mut d, request) = setup(7);
        let expected = events(&p, &request);
        retire(d.sql(), &p, Role::Consumers);
        retire(d.sql(), &p, Role::Owner);
        for event in &expected[..count] {
            append(d.sql(), &p, event.clone()).unwrap();
        }
        d.reopen();
        assert_eq!(replay(d.sql(), &p).unwrap(), expected[..count]);
        if count > 0 {
            assert!(append(d.sql(), &p, expected[count - 1].clone()).is_err());
            assert_eq!(replay(d.sql(), &p).unwrap(), expected[..count]);
        }
    }
}

#[test]
fn paired_initializer_requires_consumer_readiness_and_exact_resource_bindings() {
    for count in 0..7 {
        let (p, d, request) = setup(count);
        assert!(
            append(d.sql(), &p, Event::Intent { request }).is_err(),
            "readiness prefix {count}"
        );
        assert!(replay(d.sql(), &p).unwrap().is_empty());
    }
    let (p, d, good) = setup(7);
    for case in 0..11 {
        let mut bad = good.clone();
        match case {
            0 => bad.subject.operation = random_id().unwrap(),
            1 => bad.subject.nonce = p.roles[0].birth_nonce.clone(),
            2 => bad.subject.bootstrap_pid += 1,
            3 => bad.subject.bootstrap_created += 1,
            4 => bad.subject.owner = "S-1-5-18".into(),
            5 => bad.subject.session += 1,
            6 => bad.subject.container = "Alysis.Execution.0123456789abcdef0123456789abcdef".into(),
            7 => bad.subject.package_sid.push_str("-1"),
            8 => bad.subject.image_id = [0; 24],
            9 => bad.subject.nonce = p.execution.transaction.clone(),
            _ => bad.resources.consumer_job = bad.resources.owner_job.clone(),
        }
        assert!(
            append(d.sql(), &p, Event::Intent { request: bad }).is_err(),
            "binding case {case}"
        );
        assert!(replay(d.sql(), &p).unwrap().is_empty());
    }
    for field in [
        "owner_job",
        "consumer_job",
        "namespace",
        "host_profile",
        "host_profile_use",
        "station",
        "desktop",
    ] {
        let mut value = serde_json::to_value(&good).unwrap();
        value["resources"][field] = json!(random_id().unwrap());
        let bad = serde_json::from_value(value).unwrap();
        assert!(
            append(d.sql(), &p, Event::Intent { request: bad }).is_err(),
            "resource {field}"
        );
    }
    append(d.sql(), &p, Event::Intent { request: good }).unwrap();
    assert_eq!(replay(d.sql(), &p).unwrap().len(), 1);
}

#[test]
fn paired_initializer_release_requires_both_completed_original_role_histories() {
    let (p, d, request) = setup(7);
    let expected = events(&p, &request);
    for event in &expected[..5] {
        append(d.sql(), &p, event.clone()).unwrap();
    }
    assert!(append(d.sql(), &p, expected[5].clone()).is_err());
    plan_journal::append(
        d.sql(),
        &p,
        Role::Consumers,
        plan_journal::Event::RetireIntent,
    )
    .unwrap();
    assert!(append(d.sql(), &p, expected[5].clone()).is_err());
    plan_journal::append(d.sql(), &p, Role::Consumers, plan_journal::Event::Retired).unwrap();
    assert!(append(d.sql(), &p, expected[5].clone()).is_err());
    plan_journal::append(d.sql(), &p, Role::Owner, plan_journal::Event::RetireIntent).unwrap();
    assert!(append(d.sql(), &p, expected[5].clone()).is_err());
    plan_journal::append(d.sql(), &p, Role::Owner, plan_journal::Event::Retired).unwrap();
    let mut wrong = expected[5].clone();
    let Event::ReleaseIntent { retirements, .. } = &mut wrong else {
        unreachable!()
    };
    retirements.swap(0, 1);
    assert!(append(d.sql(), &p, wrong).is_err());
    append(d.sql(), &p, expected[5].clone()).unwrap();
    let mut wrong = expected[6].clone();
    let Event::Released { retirements, .. } = &mut wrong else {
        unreachable!()
    };
    retirements[0] = random_id().unwrap();
    assert!(append(d.sql(), &p, wrong).is_err());
    append(d.sql(), &p, expected[6].clone()).unwrap();
    assert_eq!(replay(d.sql(), &p).unwrap(), expected);
}

#[test]
fn paired_initializer_rejects_reordered_child_and_rpc_policy_receipts() {
    let (p, d, request) = setup(7);
    let expected = events(&p, &request);
    assert!(append(d.sql(), &p, expected[1].clone()).is_err());
    append(d.sql(), &p, expected[0].clone()).unwrap();
    assert!(append(d.sql(), &p, expected[2].clone()).is_err());
    for case in 0..4 {
        let mut bad = expected[1].clone();
        let Event::Born { birth, .. } = &mut bad else {
            unreachable!()
        };
        match case {
            0 => birth.pid = 0,
            1 => birth.pid = p.execution.pid,
            2 => birth.pid = request.subject.bootstrap_pid,
            _ => birth.created = request.subject.bootstrap_created - 1,
        }
        assert!(append(d.sql(), &p, bad).is_err());
    }
    append(d.sql(), &p, expected[1].clone()).unwrap();
    let mut changed = expected[2].clone();
    let Event::Stopped { birth, .. } = &mut changed else {
        unreachable!()
    };
    birth.created += 1;
    assert!(append(d.sql(), &p, changed).is_err());
    append(d.sql(), &p, expected[2].clone()).unwrap();
    assert!(append(d.sql(), &p, expected[4].clone()).is_err());
    let mut invalid = serde_json::to_value(&expected[3]).unwrap();
    invalid["policy"]["after"] = invalid["policy"]["before"].clone();
    let invalid = serde_json::from_value(invalid).unwrap();
    assert!(append(d.sql(), &p, invalid).is_err());
    append(d.sql(), &p, expected[3].clone()).unwrap();
    let mut changed = expected[4].clone();
    let Event::RpcGranted { request, .. } = &mut changed else {
        unreachable!()
    };
    request.subject.nonce = random_id().unwrap();
    assert!(append(d.sql(), &p, changed).is_err());
    append(d.sql(), &p, expected[4].clone()).unwrap();
    assert_eq!(replay(d.sql(), &p).unwrap(), expected[..5]);
}

#[test]
fn rpc_policy_initializer_journal_rejects_variant_receipt_under_baseline_plan() {
    for kind in [
        plan_journal::PolicyKind::KernelBaseAccountFullV1,
        plan_journal::PolicyKind::SystemInheritanceV1,
        plan_journal::PolicyKind::NsiSystemInheritanceV1,
        plan_journal::PolicyKind::SystemReadInheritanceV2,
        plan_journal::PolicyKind::SystemNsiReadInheritanceV3,
    ] {
        rpc_policy_initializer_journal_rejects_variant_receipt_under_baseline_plan_case(kind);
    }
}
fn rpc_policy_initializer_journal_rejects_variant_receipt_under_baseline_plan_case(
    kind: plan_journal::PolicyKind,
) {
    let (p, d, request) = setup(7);
    let baseline = events(&p, &request);
    for event in &baseline[..3] {
        append(d.sql(), &p, event.clone()).unwrap();
    }
    let mut variant = p.clone();
    variant.rpc_policy = kind;
    let variant_rows = events(&variant, &request);
    assert!(append(d.sql(), &p, variant_rows[3].clone()).is_err());
    assert!(append(d.sql(), &variant, baseline[3].clone()).is_err());
    assert_eq!(replay(d.sql(), &p).unwrap(), baseline[..3]);
    // Simulate cold rows without going through the append validator. Their
    // complete valid descriptor still cannot select a policy for the live plan.
    d.sql()
        .execute(
            "INSERT INTO candidate_paired_initializer VALUES(3,?1)",
            [serde_json::to_string(&variant_rows[3]).unwrap()],
        )
        .unwrap();
    assert!(replay(d.sql(), &p).is_err());
    assert!(replay(d.sql(), &variant).is_err());
}

#[test]
fn paired_initializer_failed_writes_immutability_and_schema_drift_preserve_prefix() {
    let (p, mut d, request) = setup(7);
    let first = Event::Intent { request };
    d.sql().execute_batch("PRAGMA query_only=ON").unwrap();
    assert!(append(d.sql(), &p, first.clone()).is_err());
    d.sql().execute_batch("PRAGMA query_only=OFF").unwrap();
    let lock = Connection::open(d.sql().path().unwrap()).unwrap();
    lock.execute_batch("BEGIN IMMEDIATE").unwrap();
    assert!(append(d.sql(), &p, first.clone()).is_err());
    lock.execute_batch("ROLLBACK").unwrap();
    assert!(replay(d.sql(), &p).unwrap().is_empty());
    append(d.sql(), &p, first.clone()).unwrap();
    for query in [
        "UPDATE candidate_paired_initializer SET event_json='{}'",
        "DELETE FROM candidate_paired_initializer",
        "INSERT OR REPLACE INTO candidate_paired_initializer VALUES(0,'{}')",
    ] {
        assert!(d.sql().execute_batch(query).is_err());
    }
    d.reopen();
    assert_eq!(replay(d.sql(), &p).unwrap(), [first]);
    d.sql()
        .execute_batch("DROP TRIGGER paired_initializer_no_delete")
        .unwrap();
    assert!(replay(d.sql(), &p).is_err());
}

#[test]
fn paired_initializer_malformed_gapped_nontext_and_unbounded_rows_fail_closed() {
    for case in 0..7 {
        let (p, d, request) = setup(7);
        let first = serde_json::to_value(Event::Intent { request }).unwrap();
        if case == 5 {
            d.sql()
                .execute(
                    "INSERT INTO candidate_paired_initializer VALUES(0,?1)",
                    params![vec![0u8; 4]],
                )
                .unwrap();
        } else {
            let (sequence, bytes) = match case {
                0 => (0, " ".repeat(MAX_EVENT + 1)),
                1 => (0, "{}".into()),
                2 => (1, first.to_string()),
                3 => (0, r#"{"kind":"adopt_token"}"#.into()),
                4 => {
                    let mut unknown = first.clone();
                    unknown["token_handle"] = json!(123);
                    (0, unknown.to_string())
                }
                _ => (0, format!("{}{{}}", first)),
            };
            d.sql()
                .execute(
                    "INSERT INTO candidate_paired_initializer VALUES(?1,?2)",
                    params![sequence, bytes],
                )
                .unwrap();
        }
        assert!(replay(d.sql(), &p).is_err(), "malformed case {case}");
        let count: usize = d
            .sql()
            .query_row(
                "SELECT count(*) FROM candidate_paired_initializer",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(count, 1, "replay must preserve failed evidence");
    }
    let (p, d, request) = setup(7);
    let first = serde_json::to_string(&Event::Intent { request }).unwrap();
    assert!(
        d.sql()
            .execute(
                "INSERT INTO candidate_paired_initializer VALUES(7,?1)",
                params![first]
            )
            .is_err()
    );
    assert!(replay(d.sql(), &p).unwrap().is_empty());
}
