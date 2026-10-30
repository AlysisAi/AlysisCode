//! Disk-only evidence: saved completion never creates a native desktop guard.
use super::super::tests as station_tests;
use super::*;
use crate::account_state::execution::paired_plan::{self as paired, tests::Disk};
fn setup() -> (
    Plan,
    Disk,
    super::super::super::Binding,
    super::super::Binding,
) {
    let (p, d, root) = station_tests::setup();
    let first = station_tests::intent(d.sql(), &p);
    let station = station_tests::bound(&first);
    super::super::append(d.sql(), &p, first).unwrap();
    super::super::append(
        d.sql(),
        &p,
        super::super::Event::Applied {
            binding: station.clone(),
        },
    )
    .unwrap();
    (p, d, root, station)
}
fn intent(sql: &Connection, p: &Plan) -> Event {
    Event::Intent {
        binding: Binding {
            operation: p.operation.clone(),
            nonce: random_id().unwrap(),
            station_nonce: super::super::grant_nonce(sql, p).unwrap(),
            namespace: super::super::super::birth_nonce(sql, p).unwrap(),
            profile: host_profile::resource_binding(sql, p).unwrap(),
            session: p.session,
            name: desktop_native::paired_name(p).unwrap(),
            package_sid: p.execution.identity.package_sid.clone(),
            policy: 1,
        },
    }
}
fn binding(e: &Event) -> Binding {
    let Event::Intent { binding } = e else {
        unreachable!()
    };
    binding.clone()
}
#[test]
fn paired_desktop_every_prefix_reopens_and_complete_station_remains_valid() {
    for n in 0..=4 {
        let (p, mut d, root, station) = setup();
        let first = intent(d.sql(), &p);
        let b = binding(&first);
        station_tests::retire(d.sql(), &p, &root, 2);
        let r = super::super::retired_namespace(d.sql(), &p).unwrap();
        let events = [
            first,
            Event::Created { binding: b.clone() },
            Event::CloseIntent {
                binding: b.clone(),
                retirements: r.clone(),
            },
            Event::Closed {
                binding: b,
                retirements: r.clone(),
            },
        ];
        for e in &events[..n] {
            append(d.sql(), &p, e.clone()).unwrap();
        }
        if n == 4 {
            for e in [
                super::super::Event::RestoreIntent {
                    binding: station.clone(),
                    retirements: r.clone(),
                },
                super::super::Event::Restored {
                    binding: station,
                    retirements: r,
                },
            ] {
                super::super::append(d.sql(), &p, e).unwrap();
                assert_eq!(replay(d.sql(), &p).unwrap(), events);
            }
        }
        d.reopen();
        assert_eq!(replay(d.sql(), &p).unwrap(), events[..n]);
        if n > 0 {
            assert!(append(d.sql(), &p, events[n - 1].clone()).is_err());
        }
    }
}
#[test]
fn paired_desktop_close_requires_both_jobs_and_final_namespace_receipt() {
    for count in 0..2 {
        let (p, d, root, _) = setup();
        let first = intent(d.sql(), &p);
        let b = binding(&first);
        append(d.sql(), &p, first).unwrap();
        append(d.sql(), &p, Event::Created { binding: b.clone() }).unwrap();
        let r = [
            p.roles[0].retire_nonce.clone(),
            p.roles[1].retire_nonce.clone(),
        ];
        let e = Event::CloseIntent {
            binding: b,
            retirements: r,
        };
        assert!(append(d.sql(), &p, e.clone()).is_err());
        station_tests::retire(d.sql(), &p, &root, count);
        assert!(append(d.sql(), &p, e).is_err());
        assert_eq!(replay(d.sql(), &p).unwrap().len(), 2);
    }
}
#[test]
fn paired_desktop_refuses_wrong_resource_binding_names_and_phase_aliases() {
    let (p, d, _, _) = setup();
    for case in 0..11 {
        let mut e = intent(d.sql(), &p);
        let Event::Intent { binding } = &mut e else {
            unreachable!()
        };
        match case {
            0 => binding.operation = random_id().unwrap(),
            1 => binding.nonce = p.execution.transaction.clone(),
            2 => binding.nonce = p.roles[0].birth_nonce.clone(),
            3 => binding.nonce = binding.station_nonce.clone(),
            4 => binding.nonce = binding.namespace.clone(),
            5 => binding.station_nonce = random_id().unwrap(),
            6 => binding.namespace = random_id().unwrap(),
            7 => binding.session += 1,
            8 => binding.name = format!("alysis-atomic-desktop-{}", p.operation),
            9 => binding.package_sid.push_str("-1"),
            _ => binding.policy += 1,
        }
        assert!(append(d.sql(), &p, e).is_err(), "case {case}");
        assert!(replay(d.sql(), &p).unwrap().is_empty());
    }
}
#[test]
fn paired_desktop_failed_writes_and_schema_drift_preserve_prefix() {
    let (p, mut d, _, _) = setup();
    let first = intent(d.sql(), &p);
    d.sql().execute_batch("PRAGMA query_only=ON").unwrap();
    assert!(append(d.sql(), &p, first.clone()).is_err());
    d.sql().execute_batch("PRAGMA query_only=OFF").unwrap();
    let lock = Connection::open(d.sql().path().unwrap()).unwrap();
    lock.execute_batch("BEGIN IMMEDIATE").unwrap();
    assert!(append(d.sql(), &p, first.clone()).is_err());
    lock.execute_batch("ROLLBACK").unwrap();
    append(d.sql(), &p, first).unwrap();
    for q in [
        "UPDATE candidate_paired_desktop SET event_json='{}'",
        "DELETE FROM candidate_paired_desktop",
        "INSERT OR REPLACE INTO candidate_paired_desktop VALUES(0,'{}')",
    ] {
        assert!(d.sql().execute_batch(q).is_err());
    }
    d.reopen();
    assert_eq!(replay(d.sql(), &p).unwrap().len(), 1);
    d.sql()
        .execute_batch("DROP TRIGGER paired_desktop_no_delete")
        .unwrap();
    assert!(replay(d.sql(), &p).is_err());
}
#[test]
fn paired_desktop_malformed_gapped_and_nontext_rows_fail_closed() {
    for case in 0..4 {
        let (p, d, _, _) = setup();
        match case {
            0 => {
                d.sql()
                    .execute(
                        "INSERT INTO candidate_paired_desktop VALUES(0,?1)",
                        params![" ".repeat(MAX_EVENT + 1)],
                    )
                    .unwrap();
            }
            1 => {
                d.sql()
                    .execute_batch("INSERT INTO candidate_paired_desktop VALUES(0,'{}')")
                    .unwrap();
            }
            2 => {
                d.sql()
                    .execute(
                        "INSERT INTO candidate_paired_desktop VALUES(0,?1)",
                        params![vec![0u8; 4]],
                    )
                    .unwrap();
            }
            _ => {
                d.sql()
                    .execute(
                        "INSERT INTO candidate_paired_desktop VALUES(1,?1)",
                        params![serde_json::to_string(&intent(d.sql(), &p)).unwrap()],
                    )
                    .unwrap();
            }
        }
        assert!(replay(d.sql(), &p).is_err());
    }
}
#[test]
fn paired_desktop_native_subject_keeps_distinct_validated_plan_domains() {
    let (p, _, _, _) = setup();
    assert_eq!(
        desktop_native::paired_name(&p).unwrap(),
        format!("alysis-paired-desktop-{}", p.operation)
    );
    for case in 0..3 {
        let mut invalid = p.clone();
        match case {
            0 => invalid.execution.scope = "atomic-bootstrap-plan-v1".into(),
            1 => invalid.image_id = [0; 24],
            _ => invalid.roles[0].job_nonce = invalid.roles[1].job_nonce.clone(),
        }
        assert!(desktop_native::paired_name(&invalid).is_err());
    }
    let _ = paired::Role::Owner;
}
