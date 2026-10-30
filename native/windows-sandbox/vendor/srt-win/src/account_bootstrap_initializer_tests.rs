//! Cold records exercise parsing/bindings only, never construct live guards.
use super::super::super::tests::{Disk, plan};
use super::*;
use serde_json::json;
fn rows(sql: &Connection, table: &str, events: &[serde_json::Value]) {
    for (i, e) in events.iter().enumerate() {
        sql.execute(
            &format!("INSERT INTO {table} VALUES(?1,?2)"),
            params![i, e.to_string()],
        )
        .unwrap();
    }
}
fn fixture() -> (Plan, Disk, Binding) {
    let p = plan();
    let d = Disk::new(&p);
    persist(d.sql(), &p).unwrap();
    let b = Binding {
        operation: p.operation.clone(),
        nonce: random_id().unwrap(),
        bootstrap_pid: p.execution.pid + 1,
        bootstrap_created: 99,
        namespace: random_id().unwrap(),
        profile_use: random_id().unwrap(),
        desktop: random_id().unwrap(),
        station: random_id().unwrap(),
        image_id: [8; 24],
        policy: 4,
    };
    let job = random_id().unwrap();
    let bootstrap = random_id().unwrap();
    for e in [
        Event::JobIntent { nonce: job.clone() },
        Event::JobCreated {
            nonce: job,
            policy: 1,
        },
        Event::BootstrapIntent {
            nonce: bootstrap.clone(),
        },
        Event::BootstrapCreated {
            nonce: bootstrap,
            pid: b.bootstrap_pid,
            created: b.bootstrap_created,
            image_id: p.image_id,
        },
    ] {
        super::super::append(d.sql(), &p, e).unwrap();
    }
    let ns = json!({"operation":p.operation,"nonce":b.namespace,"session":p.session,"package_sid":p.execution.identity.package_sid,"policy":2});
    rows(
        d.sql(),
        "candidate_atomic_namespace",
        &[
            json!({"kind":"intent","binding":ns}),
            json!({"kind":"created","binding":ns}),
        ],
    );
    let hp = json!({"operation":p.operation,"nonce":random_id().unwrap(),"owner":p.execution.owner,"container":p.execution.identity.container,"package_sid":p.execution.identity.package_sid,"policy":1});
    rows(
        d.sql(),
        "candidate_atomic_host_profile",
        &[
            json!({"kind":"intent","binding":hp}),
            json!({"kind":"created","binding":hp,"folder_id":vec![9;24]}),
            json!({"kind":"use_intent","binding":hp,"folder_id":vec![9;24],"nonce":b.profile_use}),
        ],
    );
    let station = json!({"operation":p.operation,"nonce":b.station,"session":p.session,"package_sid":p.execution.identity.package_sid,"policy":1});
    let before = crate::acl::object_grants::Snapshot {
        revision: 2,
        control: 0x1004,
        owner: "S-1-5-18".into(),
        aces: Vec::new(),
    };
    let after = crate::account_state::execution::resources::station::proposed(
        &before,
        &p.execution.identity.package_sid,
    )
    .unwrap();
    rows(
        d.sql(),
        "candidate_atomic_station",
        &[
            json!({"kind":"intent","binding":station,"before":before,"after":after}),
            json!({"kind":"applied","binding":station}),
        ],
    );
    let desktop = json!({"operation":p.operation,"nonce":b.desktop,"station_nonce":b.station,"session":p.session,"name":super::super::super::desktop_native::name(&p).unwrap(),"package_sid":p.execution.identity.package_sid,"policy":1});
    rows(
        d.sql(),
        "candidate_atomic_desktop",
        &[
            json!({"kind":"intent","binding":desktop}),
            json!({"kind":"created","binding":desktop}),
        ],
    );
    validate(d.sql(), &p, &[InitEvent::Intent { binding: b.clone() }]).unwrap();
    (p, d, b)
}
#[test]
fn initializer_cold_prefix_never_authorizes_keeper_release() {
    for cut in 0..=3 {
        let (p, mut d, b) = fixture();
        let birth = Birth {
            pid: p.execution.pid + 2,
            created: 101,
        };
        let events = [
            InitEvent::Intent { binding: b.clone() },
            InitEvent::Born {
                binding: b.clone(),
                birth: birth.clone(),
            },
            InitEvent::Stopped {
                binding: b.clone(),
                birth,
            },
        ];
        for e in &events[..cut] {
            append(d.sql(), &p, e.clone()).unwrap();
        }
        d.reopen();
        assert_eq!(replay(d.sql(), &p).unwrap(), events[..cut]);
        assert!(
            append(
                d.sql(),
                &p,
                InitEvent::ReleaseIntent {
                    binding: b,
                    retirement: random_id().unwrap()
                }
            )
            .is_err()
        );
        assert_eq!(replay(d.sql(), &p).unwrap(), events[..cut]);
    }
}
#[test]
fn initializer_rejects_resource_bootstrap_and_operation_drift() {
    let (p, d, original) = fixture();
    for case in 0..10 {
        let mut b = original.clone();
        match case {
            0 => b.operation = random_id().unwrap(),
            1 => b.nonce = b.operation.clone(),
            2 => b.bootstrap_pid += 1,
            3 => b.bootstrap_created += 1,
            4 => b.namespace = random_id().unwrap(),
            5 => b.profile_use = random_id().unwrap(),
            6 => b.desktop = random_id().unwrap(),
            7 => b.station = random_id().unwrap(),
            8 => b.image_id = [0; 24],
            _ => b.policy += 1,
        }
        assert!(
            append(d.sql(), &p, InitEvent::Intent { binding: b }).is_err(),
            "case {case}"
        );
        assert!(replay(d.sql(), &p).unwrap().is_empty());
    }
}
#[test]
fn initializer_refuses_reordered_or_changed_child_receipts_and_immutable_edits() {
    let (p, d, b) = fixture();
    let birth = Birth {
        pid: p.execution.pid + 2,
        created: 101,
    };
    assert!(
        append(
            d.sql(),
            &p,
            InitEvent::Born {
                binding: b.clone(),
                birth: birth.clone()
            }
        )
        .is_err()
    );
    append(d.sql(), &p, InitEvent::Intent { binding: b.clone() }).unwrap();
    assert!(
        append(
            d.sql(),
            &p,
            InitEvent::Stopped {
                binding: b.clone(),
                birth: birth.clone()
            }
        )
        .is_err()
    );
    append(
        d.sql(),
        &p,
        InitEvent::Born {
            binding: b.clone(),
            birth: birth.clone(),
        },
    )
    .unwrap();
    let mut other = birth;
    other.created += 1;
    assert!(
        append(
            d.sql(),
            &p,
            InitEvent::Stopped {
                binding: b,
                birth: other
            }
        )
        .is_err()
    );
    for statement in [
        "UPDATE candidate_atomic_initializer SET event_json='{}'",
        "DELETE FROM candidate_atomic_initializer",
        "INSERT OR REPLACE INTO candidate_atomic_initializer VALUES(0,'{}')",
    ] {
        assert!(d.sql().execute_batch(statement).is_err());
    }
    d.sql()
        .execute_batch("DROP TRIGGER atomic_initializer_no_update")
        .unwrap();
    assert!(replay(d.sql(), &p).is_err());
}
#[test]
fn initializer_refuses_unbounded_gapped_and_unknown_cold_rows() {
    for (sequence, bytes) in [
        (1, "{}".into()),
        (0, "x".repeat(16385)),
        (0, r#"{"kind":"adopt_token"}"#.into()),
    ] {
        let (p, d, _) = fixture();
        d.sql()
            .execute(
                "INSERT INTO candidate_atomic_initializer VALUES(?1,?2)",
                params![sequence, bytes],
            )
            .unwrap();
        assert!(replay(d.sql(), &p).is_err());
        let actual: String = d
            .sql()
            .query_row(
                "SELECT event_json FROM candidate_atomic_initializer",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(actual, bytes);
    }
}

#[test]
fn initializer_namespace_policy_distinguishes_shared_global_from_private_aliases() {
    let p = plan();
    let entries: Vec<_> = [
        ("Global", "SymbolicLink"),
        ("Local", "SymbolicLink"),
        ("RPC Control", "Directory"),
        ("Session", "SymbolicLink"),
    ]
    .into_iter()
    .map(|(name, kind)| DirectoryEntry {
        name: name.into(),
        kind: kind.into(),
    })
    .collect();
    let private = format!(
        "\\Sessions\\{}\\AppContainerNamedObjects\\{}",
        p.session, p.execution.identity.package_sid
    );
    let good = ["\\BaseNamedObjects".into(), private.clone(), private];
    verify_namespace_observation(&entries, &good, &p).unwrap();
    for index in 0..3 {
        let mut bad = good.clone();
        bad[index].push_str("\\unexpected");
        assert!(verify_namespace_observation(&entries, &bad, &p).is_err());
    }
    let mut wrong_session = p.clone();
    wrong_session.session += 1;
    assert!(verify_namespace_observation(&entries, &good, &wrong_session).is_err());
    let mut bad = entries;
    bad[0].kind = "Directory".into();
    assert!(verify_namespace_observation(&bad, &good, &p).is_err());
    bad.remove(0);
    assert!(verify_namespace_observation(&bad, &good, &p).is_err());
}

#[test]
fn initializer_rpc_cold_policy_requires_stopped_prefix_exact_subjects_and_grant_receipt() {
    let (p, mut d, b) = fixture();
    let policy =
        crate::account_state::execution::resources::namespace_native::rpc::fixture_access_policy(
            &p,
        );
    let birth = Birth {
        pid: p.execution.pid + 2,
        created: 101,
    };
    let intent = InitEvent::RpcIntent {
        binding: b.clone(),
        policy: policy.clone(),
    };
    assert!(append(d.sql(), &p, intent.clone()).is_err());
    for e in [
        InitEvent::Intent { binding: b.clone() },
        InitEvent::Born {
            binding: b.clone(),
            birth: birth.clone(),
        },
        InitEvent::Stopped {
            binding: b.clone(),
            birth,
        },
    ] {
        append(d.sql(), &p, e).unwrap();
    }
    let granted = InitEvent::RpcGranted {
        binding: b.clone(),
        policy: policy.clone(),
    };
    assert!(append(d.sql(), &p, granted.clone()).is_err());
    for component in ["owner", "dacl", "label"] {
        let mut bad = serde_json::to_value(&policy).unwrap();
        bad["after"][component] = json!([1, 2, 3, 4]);
        let bad: AccessPolicy = serde_json::from_value(bad).unwrap();
        assert!(
            append(
                d.sql(),
                &p,
                InitEvent::RpcIntent {
                    binding: b.clone(),
                    policy: bad
                }
            )
            .is_err()
        );
    }
    append(d.sql(), &p, intent).unwrap();
    let mut wrong = serde_json::to_value(&policy).unwrap();
    wrong["before"]["dacl"][8] = json!(99);
    let wrong: AccessPolicy = serde_json::from_value(wrong).unwrap();
    assert!(
        append(
            d.sql(),
            &p,
            InitEvent::RpcGranted {
                binding: b.clone(),
                policy: wrong
            }
        )
        .is_err()
    );
    append(d.sql(), &p, granted).unwrap();
    d.reopen();
    assert_eq!(replay(d.sql(), &p).unwrap().len(), 5);
    // Persisted grant data never supplies the original CompletedJob guard.
    assert!(
        append(
            d.sql(),
            &p,
            InitEvent::ReleaseIntent {
                binding: b,
                retirement: random_id().unwrap()
            }
        )
        .is_err()
    );
}
