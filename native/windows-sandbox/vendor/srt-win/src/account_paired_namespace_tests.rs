//! Disk evidence cannot construct a live pair or authorize native cleanup.
use super::super::super::tests::{Disk, plan, prefix};
use super::*;

fn binding(plan: &Plan) -> Binding {
    Binding {
        operation: plan.operation.clone(),
        nonce: random_id().unwrap(),
        session: plan.session,
        package_sid: plan.execution.identity.package_sid.clone(),
        policy: POLICY,
        birth_kind: plan.namespace_birth,
    }
}
fn setup() -> (Plan, Disk, Binding) {
    let p = plan();
    let disk = Disk::new(&p);
    super::super::super::persist(disk.sql(), &p).unwrap();
    let b = binding(&p);
    (p, disk, b)
}
#[test]
fn paired_namespace_constructor_identity_is_independent_and_legacy_is_unspecified() {
    use root_native::RootBirthKind as Kind;
    let legacy = plan();
    let wire = serde_json::to_value(&legacy).unwrap();
    assert!(wire.get("namespace_birth").is_none());
    assert_eq!(
        serde_json::from_value::<Plan>(wire)
            .unwrap()
            .namespace_birth,
        Kind::LegacyUnspecified
    );
    let kinds = [
        Kind::LegacyUnspecified,
        Kind::OrdinaryDirectoryV1,
        Kind::SessionShadowDirectoryV1,
    ];
    for kind in kinds {
        let mut selected = legacy.clone();
        selected.namespace_birth = kind;
        selected.validate().unwrap();
        assert_eq!(
            selected.require_live_policy().is_ok(),
            kind == Kind::LegacyUnspecified
        );
        let mut d = Disk::new(&selected);
        super::super::super::persist(d.sql(), &selected).unwrap();
        let b = binding(&selected);
        assert_eq!(b.policy, 2); // ACL/scope policy is not a constructor selector.
        birth(d.sql(), &selected, &b);
        d.reopen();
        assert_eq!(replay(d.sql(), &selected).unwrap().len(), 2);
        for expected in kinds {
            // A consistent pair of reported plan/binding still cannot choose
            // the independently expected constructor for a fixture.
            assert_eq!(
                selected.fixture_require_namespace_birth(expected).is_ok(),
                kind == expected
            );
            let mut crossed = selected.clone();
            crossed.namespace_birth = expected;
            assert_eq!(replay(d.sql(), &crossed).is_ok(), kind == expected);
            let mut wrong = b.clone();
            wrong.birth_kind = expected;
            assert_eq!(
                validate(d.sql(), &selected, &[Event::Intent { binding: wrong }]).is_ok(),
                kind == expected
            );
        }
        if kind != Kind::LegacyUnspecified {
            let mut omitted_plan = serde_json::to_value(&selected).unwrap();
            omitted_plan
                .as_object_mut()
                .unwrap()
                .remove("namespace_birth");
            let omitted_plan: Plan = serde_json::from_value(omitted_plan).unwrap();
            assert_eq!(omitted_plan.namespace_birth, Kind::LegacyUnspecified);
            assert!(omitted_plan.fixture_require_namespace_birth(kind).is_err());
            assert!(replay(d.sql(), &omitted_plan).is_err());
            let mut omitted_binding = serde_json::to_value(&b).unwrap();
            omitted_binding
                .as_object_mut()
                .unwrap()
                .remove("birth_kind");
            let omitted_binding: Binding = serde_json::from_value(omitted_binding).unwrap();
            assert_eq!(omitted_binding.birth_kind, Kind::LegacyUnspecified);
            assert!(
                validate(
                    d.sql(),
                    &selected,
                    &[Event::Intent {
                        binding: omitted_binding
                    }]
                )
                .is_err()
            );
            // Refusal must precede parent verification or any native birth.
            let error = root_native::create_native(&selected, HANDLE::default()).unwrap_err();
            let message = format!("{error:#}");
            assert!(
                message.contains("constructor lifecycle is unqualified")
                    || message.contains("original thread admission")
            );
        }
    }
}
#[test]
fn paired_namespace_reserved_constructor_never_inherits_rpc_admission() {
    use root_native::RootBirthKind as Kind;
    for kind in [Kind::OrdinaryDirectoryV1, Kind::SessionShadowDirectoryV1] {
        for rpc in [
            root_native::rpc::PolicyKind::BaselineV1,
            root_native::rpc::PolicyKind::SystemNsiReadInheritanceV3,
            root_native::rpc::PolicyKind::NsiSystemInheritanceV1,
        ] {
            let mut p = plan();
            p.namespace_birth = kind;
            p.rpc_policy = rpc;
            p.validate().unwrap();
            assert!(p.require_live_policy().is_err());
            let serialized = serde_json::to_value(&p).unwrap();
            assert_eq!(
                serialized["namespace_birth"],
                serde_json::to_value(kind).unwrap()
            );
        }
    }
}
fn birth(sql: &Connection, p: &Plan, b: &Binding) {
    append(sql, p, Event::Intent { binding: b.clone() }).unwrap();
    append(sql, p, Event::Created { binding: b.clone() }).unwrap();
}
fn complete_roles(sql: &Connection, p: &Plan) {
    // Owner readiness precedes consumer retirement, then owner retirement.
    prefix(sql, p, Role::Owner, 7);
    prefix(sql, p, Role::Consumers, 9);
    super::super::super::append(
        sql,
        p,
        Role::Owner,
        super::super::super::Event::RetireIntent,
    )
    .unwrap();
    super::super::super::append(sql, p, Role::Owner, super::super::super::Event::Retired).unwrap();
}
#[test]
fn paired_namespace_four_rows_reopen_immutable_exact_pair_evidence() {
    let (p, mut d, b) = setup();
    birth(d.sql(), &p, &b);
    complete_roles(d.sql(), &p);
    let retirements = retirement_nonces(d.sql(), &p).unwrap();
    append(
        d.sql(),
        &p,
        Event::RemoveIntent {
            binding: b.clone(),
            retirements: retirements.clone(),
        },
    )
    .unwrap();
    append(
        d.sql(),
        &p,
        Event::Removed {
            binding: b,
            retirements,
        },
    )
    .unwrap();
    let expected = replay(d.sql(), &p).unwrap();
    for stmt in [
        "UPDATE candidate_paired_namespace SET event_json='{}'",
        "DELETE FROM candidate_paired_namespace",
        "INSERT OR REPLACE INTO candidate_paired_namespace VALUES(0,'{}')",
    ] {
        assert!(d.sql().execute_batch(stmt).is_err());
    }
    d.reopen();
    assert_eq!(replay(d.sql(), &p).unwrap(), expected);
    assert!(append(d.sql(), &p, expected[3].clone()).is_err());
}
#[test]
fn paired_namespace_refuses_removal_before_both_retirements_and_swapped_roles() {
    let (p, d, b) = setup();
    birth(d.sql(), &p, &b);
    let retired = [
        p.roles[0].retire_nonce.clone(),
        p.roles[1].retire_nonce.clone(),
    ];
    assert!(
        append(
            d.sql(),
            &p,
            Event::RemoveIntent {
                binding: b.clone(),
                retirements: retired.clone()
            }
        )
        .is_err()
    );
    prefix(d.sql(), &p, Role::Owner, 7);
    prefix(d.sql(), &p, Role::Consumers, 9);
    assert!(
        append(
            d.sql(),
            &p,
            Event::RemoveIntent {
                binding: b.clone(),
                retirements: retired.clone()
            }
        )
        .is_err()
    );
    super::super::super::append(
        d.sql(),
        &p,
        Role::Owner,
        super::super::super::Event::RetireIntent,
    )
    .unwrap();
    super::super::super::append(
        d.sql(),
        &p,
        Role::Owner,
        super::super::super::Event::Retired,
    )
    .unwrap();
    assert!(
        append(
            d.sql(),
            &p,
            Event::RemoveIntent {
                binding: b,
                retirements: [retired[1].clone(), retired[0].clone()]
            }
        )
        .is_err()
    );
    assert_eq!(replay(d.sql(), &p).unwrap().len(), 2);
}
#[test]
fn paired_namespace_refuses_binding_changes_and_all_phase_aliases() {
    let (p, d, b) = setup();
    let mut bad = Vec::new();
    let mut changed = b.clone();
    changed.session += 1;
    bad.push(changed);
    let mut changed = b.clone();
    changed.policy += 1;
    bad.push(changed);
    let mut changed = b.clone();
    changed.operation = random_id().unwrap();
    bad.push(changed);
    let mut changed = b.clone();
    changed.package_sid = "S-1-5-18".into();
    bad.push(changed);
    let mut aliases = vec![p.operation.clone(), p.execution.transaction.clone()];
    for r in &p.roles {
        aliases.extend([
            r.job_nonce.clone(),
            r.birth_nonce.clone(),
            r.resume_nonce.clone(),
            r.retire_nonce.clone(),
            r.control_nonce.iter().map(|b| format!("{b:02x}")).collect(),
        ]);
    }
    for nonce in aliases {
        let mut changed = b.clone();
        changed.nonce = nonce;
        bad.push(changed);
    }
    for binding in bad {
        assert!(append(d.sql(), &p, Event::Intent { binding }).is_err());
    }
    assert!(replay(d.sql(), &p).unwrap().is_empty());
}
#[test]
fn paired_namespace_missing_trigger_and_foreign_schema_refuse_plan() {
    let (p, d, _) = setup();
    d.sql()
        .execute_batch("DROP TRIGGER paired_namespace_no_delete")
        .unwrap();
    assert!(require_plan(d.sql(), &p).is_err());
    assert!(replay(d.sql(), &p).is_err());
}
#[test]
fn paired_namespace_failed_write_preserves_birth_without_removal() {
    let (p, mut d, b) = setup();
    birth(d.sql(), &p, &b);
    complete_roles(d.sql(), &p);
    let expected = replay(d.sql(), &p).unwrap();
    d.sql().execute_batch("PRAGMA query_only=ON").unwrap();
    assert!(
        append(
            d.sql(),
            &p,
            Event::RemoveIntent {
                binding: b,
                retirements: retirement_nonces(d.sql(), &p).unwrap()
            }
        )
        .is_err()
    );
    d.sql().execute_batch("PRAGMA query_only=OFF").unwrap();
    d.reopen();
    assert_eq!(replay(d.sql(), &p).unwrap(), expected);
}
#[test]
fn paired_namespace_replay_refuses_gap_oversize_blob_and_unknown_field() {
    for case in 0..4 {
        let (p, d, b) = setup();
        let json = serde_json::to_string(&Event::Intent { binding: b }).unwrap();
        match case {
            0 => {
                d.sql()
                    .execute(
                        "INSERT INTO candidate_paired_namespace VALUES(1,?1)",
                        [json],
                    )
                    .unwrap();
            }
            1 => {
                d.sql()
                    .execute(
                        "INSERT INTO candidate_paired_namespace VALUES(0,?1)",
                        ["x".repeat(2049)],
                    )
                    .unwrap();
            }
            2 => {
                d.sql()
                    .execute(
                        "INSERT INTO candidate_paired_namespace VALUES(0,?1)",
                        [json.as_bytes()],
                    )
                    .unwrap();
            }
            _ => {
                let mut v: serde_json::Value = serde_json::from_str(&json).unwrap();
                v["extra"] = true.into();
                d.sql()
                    .execute(
                        "INSERT INTO candidate_paired_namespace VALUES(0,?1)",
                        [v.to_string()],
                    )
                    .unwrap();
            }
        }
        assert!(replay(d.sql(), &p).is_err());
    }
}
