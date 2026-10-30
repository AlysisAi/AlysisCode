//! Disk-only negative evidence. No SDK/profile creation or account effects.
use super::*;
use crate::account_state::execution::paired_plan::{
    self as paired,
    tests::{Disk, plan, prefix},
};
fn setup() -> (Plan, Disk, Binding) {
    let p = plan();
    let d = Disk::new(&p);
    paired::persist(d.sql(), &p).unwrap();
    let root = super::super::Binding {
        operation: p.operation.clone(),
        nonce: random_id().unwrap(),
        session: p.session,
        package_sid: p.execution.identity.package_sid.clone(),
        policy: 2,
        birth_kind: p.namespace_birth,
    };
    super::super::append(
        d.sql(),
        &p,
        super::super::Event::Intent {
            binding: root.clone(),
        },
    )
    .unwrap();
    super::super::append(
        d.sql(),
        &p,
        super::super::Event::Created {
            binding: root.clone(),
        },
    )
    .unwrap();
    let b = Binding {
        operation: p.operation.clone(),
        namespace: root.nonce,
        nonce: random_id().unwrap(),
        owner: p.execution.owner.clone(),
        container: p.execution.identity.container.clone(),
        package_sid: p.execution.identity.package_sid.clone(),
        policy: 1,
    };
    (p, d, b)
}
fn events(b: &Binding, nonce: &str, retired: [String; 2], used: bool) -> Vec<Event> {
    let mut e = vec![
        Event::Intent { binding: b.clone() },
        Event::Created {
            binding: b.clone(),
            folder_id: [7; 24],
        },
    ];
    if used {
        e.push(Event::UseIntent {
            binding: b.clone(),
            folder_id: [7; 24],
            nonce: nonce.into(),
        });
        e.push(Event::Captured {
            binding: b.clone(),
            folder_id: [7; 24],
            nonce: nonce.into(),
            retirement: retired.clone(),
        });
    }
    e.push(Event::RemoveIntent {
        binding: b.clone(),
        folder_id: [7; 24],
        retirement: retired.clone(),
    });
    e.push(Event::Removed {
        binding: b.clone(),
        folder_id: [7; 24],
        retirement: retired,
    });
    e
}
fn retired(sql: &Connection, p: &Plan) -> [String; 2] {
    prefix(sql, p, Role::Owner, 7);
    prefix(sql, p, Role::Consumers, 9);
    paired::append(sql, p, Role::Owner, paired::Event::RetireIntent).unwrap();
    paired::append(sql, p, Role::Owner, paired::Event::Retired).unwrap();
    super::super::retirement_nonces(sql, p).unwrap()
}
#[test]
fn paired_host_profile_every_used_and_unused_prefix_reopens_without_cleanup_authority() {
    for used in [false, true] {
        for count in 0..=if used { 6 } else { 4 } {
            let (p, mut d, b) = setup();
            let retirement = retired(d.sql(), &p);
            let e = events(&b, &random_id().unwrap(), retirement, used);
            for item in &e[..count] {
                append(d.sql(), &p, item.clone()).unwrap();
            }
            d.reopen();
            assert_eq!(replay(d.sql(), &p).unwrap(), e[..count]);
            if count > 0 {
                assert!(append(d.sql(), &p, e[count - 1].clone()).is_err());
            }
        }
    }
}
#[test]
fn paired_host_profile_refuses_capture_until_both_roles_retire() {
    let (p, d, b) = setup();
    let e = events(
        &b,
        &random_id().unwrap(),
        [
            p.roles[0].retire_nonce.clone(),
            p.roles[1].retire_nonce.clone(),
        ],
        true,
    );
    for item in &e[..3] {
        append(d.sql(), &p, item.clone()).unwrap();
    }
    assert!(append(d.sql(), &p, e[3].clone()).is_err());
    prefix(d.sql(), &p, Role::Owner, 7);
    prefix(d.sql(), &p, Role::Consumers, 9);
    assert!(append(d.sql(), &p, e[3].clone()).is_err());
    assert_eq!(replay(d.sql(), &p).unwrap(), e[..3]);
}
#[test]
fn paired_host_profile_completion_stays_valid_after_namespace_retirement() {
    let (p, d, b) = setup();
    let retirement = retired(d.sql(), &p);
    let e = events(&b, &random_id().unwrap(), retirement.clone(), true);
    for item in &e {
        append(d.sql(), &p, item.clone()).unwrap();
    }
    let ns = super::super::replay(d.sql(), &p).unwrap();
    let super::super::Event::Intent { binding } = ns[0].clone() else {
        unreachable!()
    };
    super::super::append(
        d.sql(),
        &p,
        super::super::Event::RemoveIntent {
            binding: binding.clone(),
            retirements: retirement.clone(),
        },
    )
    .unwrap();
    super::super::append(
        d.sql(),
        &p,
        super::super::Event::Removed {
            binding,
            retirements: retirement,
        },
    )
    .unwrap();
    assert_eq!(replay(d.sql(), &p).unwrap(), e);
}
#[test]
fn paired_host_profile_refuses_foreign_namespace_actor_folder_and_phase_aliases() {
    let (p, d, b) = setup();
    let mut aliases = vec![
        p.operation.clone(),
        p.execution.transaction.clone(),
        b.namespace.clone(),
    ];
    for r in &p.roles {
        aliases.extend([
            r.job_nonce.clone(),
            r.birth_nonce.clone(),
            r.resume_nonce.clone(),
            r.retire_nonce.clone(),
            r.control_nonce.iter().map(|b| format!("{b:02x}")).collect(),
        ]);
    }
    for nonce in &aliases {
        let mut bad = b.clone();
        bad.nonce = nonce.clone();
        assert!(append(d.sql(), &p, Event::Intent { binding: bad }).is_err());
    }
    for i in 0..5 {
        let mut bad = b.clone();
        match i {
            0 => bad.namespace = random_id().unwrap(),
            1 => bad.owner.push_str("-1"),
            2 => bad.container.push('x'),
            3 => bad.package_sid.push_str("-1"),
            _ => bad.policy += 1,
        };
        assert!(append(d.sql(), &p, Event::Intent { binding: bad }).is_err());
    }
    append(d.sql(), &p, Event::Intent { binding: b.clone() }).unwrap();
    assert!(
        append(
            d.sql(),
            &p,
            Event::Created {
                binding: b.clone(),
                folder_id: [0; 24]
            }
        )
        .is_err()
    );
    append(
        d.sql(),
        &p,
        Event::Created {
            binding: b.clone(),
            folder_id: [7; 24],
        },
    )
    .unwrap();
    aliases.push(b.nonce.clone());
    for nonce in aliases {
        assert!(
            append(
                d.sql(),
                &p,
                Event::UseIntent {
                    binding: b.clone(),
                    folder_id: [7; 24],
                    nonce
                }
            )
            .is_err()
        );
    }
    assert!(
        append(
            d.sql(),
            &p,
            Event::UseIntent {
                binding: b,
                folder_id: [8; 24],
                nonce: random_id().unwrap()
            }
        )
        .is_err()
    );
}
#[test]
fn paired_host_profile_readonly_busy_and_schema_failures_preserve_exact_prefix() {
    let (p, mut d, b) = setup();
    let retirement = retired(d.sql(), &p);
    let e = events(&b, &random_id().unwrap(), retirement, true);
    for item in &e[..3] {
        append(d.sql(), &p, item.clone()).unwrap();
    }
    d.sql().execute_batch("PRAGMA query_only=ON").unwrap();
    assert!(append(d.sql(), &p, e[3].clone()).is_err());
    d.sql().execute_batch("PRAGMA query_only=OFF").unwrap();
    d.reopen();
    assert_eq!(replay(d.sql(), &p).unwrap(), e[..3]);
    let file: String = d
        .sql()
        .query_row(
            "SELECT file FROM pragma_database_list WHERE name='main'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    let reader = Connection::open(file).unwrap();
    reader
        .execute_batch("BEGIN; SELECT count(*) FROM candidate_paired_host_profile")
        .unwrap();
    d.sql().busy_timeout(std::time::Duration::ZERO).unwrap();
    assert!(append(d.sql(), &p, e[3].clone()).is_err());
    reader.execute_batch("ROLLBACK").unwrap();
    assert_eq!(replay(d.sql(), &p).unwrap(), e[..3]);
    for sql in [
        "UPDATE candidate_paired_host_profile SET event_json='{}'",
        "DELETE FROM candidate_paired_host_profile",
        "INSERT OR REPLACE INTO candidate_paired_host_profile VALUES(0,'{}')",
    ] {
        assert!(d.sql().execute_batch(sql).is_err());
    }
    d.sql()
        .execute_batch("DROP TRIGGER paired_host_profile_no_delete")
        .unwrap();
    assert!(require_plan(d.sql(), &p).is_err());
    assert!(replay(d.sql(), &p).is_err());
}
#[test]
fn paired_host_profile_replay_refuses_malformed_bounded_rows() {
    for case in 0..4 {
        let (p, d, b) = setup();
        let json = serde_json::to_string(&Event::Intent { binding: b }).unwrap();
        match case {
            0 => {
                d.sql()
                    .execute(
                        "INSERT INTO candidate_paired_host_profile VALUES(1,?1)",
                        [json],
                    )
                    .unwrap();
            }
            1 => {
                d.sql()
                    .execute(
                        "INSERT INTO candidate_paired_host_profile VALUES(0,?1)",
                        ["x".repeat(MAX_EVENT + 1)],
                    )
                    .unwrap();
            }
            2 => {
                d.sql()
                    .execute(
                        "INSERT INTO candidate_paired_host_profile VALUES(0,?1)",
                        [json.as_bytes()],
                    )
                    .unwrap();
            }
            _ => {
                let mut v: serde_json::Value = serde_json::from_str(&json).unwrap();
                v["extra"] = true.into();
                d.sql()
                    .execute(
                        "INSERT INTO candidate_paired_host_profile VALUES(0,?1)",
                        [v.to_string()],
                    )
                    .unwrap();
            }
        }
        assert!(replay(d.sql(), &p).is_err());
    }
}
