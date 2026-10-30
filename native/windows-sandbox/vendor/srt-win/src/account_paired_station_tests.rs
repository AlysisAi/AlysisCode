//! Durable evidence tests only: no station DACL or SDK/account effects.
use super::*;
use crate::account_state::execution::paired_plan::{
    self as paired,
    tests::{Disk, plan, prefix},
};
pub(super) fn setup() -> (Plan, Disk, super::super::Binding) {
    let p = plan();
    let d = Disk::new(&p);
    paired::persist(d.sql(), &p).unwrap();
    let b = super::super::Binding {
        operation: p.operation.clone(),
        nonce: random_id().unwrap(),
        session: p.session,
        package_sid: p.execution.identity.package_sid.clone(),
        policy: 2,
        birth_kind: p.namespace_birth,
    };
    for e in [
        super::super::Event::Intent { binding: b.clone() },
        super::super::Event::Created { binding: b.clone() },
    ] {
        super::super::append(d.sql(), &p, e).unwrap();
    }
    (p, d, b)
}
pub(super) fn intent(sql: &Connection, p: &Plan) -> Event {
    let before = Snapshot {
        revision: 2,
        control: 0x1004,
        owner: "S-1-5-18".into(),
        aces: Vec::new(),
    };
    let after = proposed(&before, &p.execution.identity.package_sid).unwrap();
    Event::Intent {
        binding: Binding {
            operation: p.operation.clone(),
            nonce: random_id().unwrap(),
            namespace: super::super::birth_nonce(sql, p).unwrap(),
            profile: host_profile::resource_binding(sql, p).unwrap(),
            session: p.session,
            package_sid: p.execution.identity.package_sid.clone(),
            policy: 1,
        },
        before,
        after,
    }
}
pub(super) fn bound(e: &Event) -> Binding {
    let Event::Intent { binding, .. } = e else {
        unreachable!()
    };
    binding.clone()
}
pub(super) fn retire(sql: &Connection, p: &Plan, b: &super::super::Binding, namespace_rows: usize) {
    prefix(sql, p, Role::Owner, 7);
    prefix(sql, p, Role::Consumers, 9);
    paired::append(sql, p, Role::Owner, paired::Event::RetireIntent).unwrap();
    paired::append(sql, p, Role::Owner, paired::Event::Retired).unwrap();
    let retirements = super::super::retirement_nonces(sql, p).unwrap();
    let e = [
        super::super::Event::RemoveIntent {
            binding: b.clone(),
            retirements: retirements.clone(),
        },
        super::super::Event::Removed {
            binding: b.clone(),
            retirements,
        },
    ];
    for e in &e[..namespace_rows] {
        super::super::append(sql, p, e.clone()).unwrap();
    }
}
#[test]
fn paired_station_every_prefix_reopens_without_restoration_authority() {
    for n in 0..=4 {
        let (p, mut d, root) = setup();
        let first = intent(d.sql(), &p);
        let b = bound(&first);
        retire(d.sql(), &p, &root, 2);
        let r = retired_namespace(d.sql(), &p).unwrap();
        let e = [
            first,
            Event::Applied { binding: b.clone() },
            Event::RestoreIntent {
                binding: b.clone(),
                retirements: r.clone(),
            },
            Event::Restored {
                binding: b,
                retirements: r,
            },
        ];
        for e in &e[..n] {
            append(d.sql(), &p, e.clone()).unwrap();
        }
        d.reopen();
        assert_eq!(replay(d.sql(), &p).unwrap(), e[..n]);
        if n > 0 {
            assert!(append(d.sql(), &p, e[n - 1].clone()).is_err());
        }
    }
}
#[test]
fn paired_station_requires_both_jobs_and_final_namespace_receipt() {
    for root_rows in 0..2 {
        let (p, d, root) = setup();
        let first = intent(d.sql(), &p);
        let b = bound(&first);
        append(d.sql(), &p, first).unwrap();
        append(d.sql(), &p, Event::Applied { binding: b.clone() }).unwrap();
        let r = [
            p.roles[0].retire_nonce.clone(),
            p.roles[1].retire_nonce.clone(),
        ];
        let e = Event::RestoreIntent {
            binding: b,
            retirements: r,
        };
        assert!(append(d.sql(), &p, e.clone()).is_err());
        retire(d.sql(), &p, &root, root_rows);
        assert!(append(d.sql(), &p, e).is_err());
        assert_eq!(replay(d.sql(), &p).unwrap().len(), 2);
    }
}
#[test]
fn paired_station_rejects_plan_resource_aliases_and_snapshot_drift() {
    let (p, d, _) = setup();
    for case in 0..11 {
        let mut e = intent(d.sql(), &p);
        let Event::Intent {
            binding,
            before,
            after,
        } = &mut e
        else {
            unreachable!()
        };
        match case {
            0 => binding.operation = random_id().unwrap(),
            1 => binding.nonce = p.execution.transaction.clone(),
            2 => binding.nonce = p.roles[0].birth_nonce.clone(),
            3 => binding.nonce = p.roles[1].retire_nonce.clone(),
            4 => binding.nonce = binding.namespace.clone(),
            5 => binding.namespace = random_id().unwrap(),
            6 => binding.session += 1,
            7 => binding.package_sid.push_str("-1"),
            8 => binding.policy += 1,
            9 => after.aces.clear(),
            _ => before.owner = "invalid".into(),
        }
        assert!(append(d.sql(), &p, e).is_err(), "case {case}");
        assert!(replay(d.sql(), &p).unwrap().is_empty());
    }
}
#[test]
fn paired_station_freezes_optional_profile_chain_and_disjoint_nonce() {
    let (p, d, root) = setup();
    let without_profile = intent(d.sql(), &p);
    let birth = random_id().unwrap();
    let used = random_id().unwrap();
    let b = serde_json::json!({"operation":p.operation,"namespace":root.nonce,"nonce":birth,"owner":p.execution.owner,"container":p.execution.identity.container,"package_sid":p.execution.identity.package_sid,"policy":1});
    let folder = vec![7; 24];
    let events = [
        serde_json::json!({"kind":"intent","binding":b}),
        serde_json::json!({"kind":"created","binding":b,"folder_id":folder}),
        serde_json::json!({"kind":"use_intent","binding":b,"folder_id":folder,"nonce":used}),
    ];
    for (i, e) in events.iter().enumerate() {
        d.sql()
            .execute(
                "INSERT INTO candidate_paired_host_profile VALUES(?1,?2)",
                params![i, e.to_string()],
            )
            .unwrap();
    }
    assert!(append(d.sql(), &p, without_profile).is_err());
    for alias in [birth, used] {
        let mut e = intent(d.sql(), &p);
        let Event::Intent { binding, .. } = &mut e else {
            unreachable!()
        };
        binding.nonce = alias;
        assert!(append(d.sql(), &p, e).is_err());
    }
    append(d.sql(), &p, intent(d.sql(), &p)).unwrap();
    assert_eq!(replay(d.sql(), &p).unwrap().len(), 1);
}
#[test]
fn paired_station_readonly_busy_and_schema_failures_preserve_prefix() {
    let (p, mut d, _) = setup();
    let e = intent(d.sql(), &p);
    d.sql().execute_batch("PRAGMA query_only=ON").unwrap();
    assert!(append(d.sql(), &p, e.clone()).is_err());
    d.sql().execute_batch("PRAGMA query_only=OFF").unwrap();
    let path = d.sql().path().unwrap().to_owned();
    let lock = Connection::open(path).unwrap();
    lock.execute_batch("BEGIN IMMEDIATE").unwrap();
    assert!(append(d.sql(), &p, e.clone()).is_err());
    lock.execute_batch("ROLLBACK").unwrap();
    assert!(replay(d.sql(), &p).unwrap().is_empty());
    append(d.sql(), &p, e).unwrap();
    for q in [
        "UPDATE candidate_paired_station SET event_json='{}'",
        "DELETE FROM candidate_paired_station",
        "INSERT OR REPLACE INTO candidate_paired_station VALUES(0,'{}')",
    ] {
        assert!(d.sql().execute_batch(q).is_err());
    }
    d.reopen();
    assert_eq!(replay(d.sql(), &p).unwrap().len(), 1);
    d.sql()
        .execute_batch("DROP TRIGGER paired_station_no_delete")
        .unwrap();
    assert!(replay(d.sql(), &p).is_err());
}
#[test]
fn paired_station_malformed_bounded_rows_fail_closed() {
    for payload in [vec![b' '; MAX_EVENT + 1], b"{}".to_vec()] {
        let (p, d, _) = setup();
        d.sql()
            .execute(
                "INSERT INTO candidate_paired_station VALUES(0,?1)",
                params![String::from_utf8(payload).unwrap()],
            )
            .unwrap();
        assert!(replay(d.sql(), &p).is_err());
    }
    let (p, d, _) = setup();
    d.sql()
        .execute(
            "INSERT INTO candidate_paired_station VALUES(0,?1)",
            params![vec![0u8; 5]],
        )
        .unwrap();
    assert!(replay(d.sql(), &p).is_err());
}
