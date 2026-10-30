//! Synthetic SQLite transition tests. No process/profile/namespace/ACL effect
//! adapter is called, and no installed state is read or written.
use super::*;
use crate::account_install::{AccountMode, synthetic_ready};

fn identity() -> Identity {
    let binding = synthetic_ready().binding(AccountMode::Offline).unwrap();
    let candidate = crate::account_state::CandidateJournalIdentity::new(
        binding,
        format!("Alysis.Execution.{}", super::super::random_id().unwrap()),
    )
    .unwrap();
    let mut execution = super::super::fixture_record(candidate);
    execution.scope = RESOURCE_SCOPE.into();
    Identity {
        execution,
        session_id: 7,
    }
}
fn connection(identity: &Identity) -> Connection {
    let mut connection = Connection::open_in_memory().unwrap();
    crate::account_state::initialize_header(&mut connection, &identity.execution.identity.binding)
        .unwrap();
    connection
}
fn fixture<'a>(
    connection: &'a Connection,
    identity: &Identity,
    create: bool,
) -> ResourceJournal<'a> {
    ResourceJournal::open(connection, identity.clone(), Authority::Fixture, create).unwrap()
}
fn acl_intent(resource: Resource) -> Option<AclIntent> {
    resource.shared_acl().then(|| AclIntent {
        before: vec![1; 24],
        after: vec![2; 24],
    })
}
fn observation(identity: &Identity, resource: Resource) -> Observation {
    if resource.profile() {
        return Observation::SyntheticProfile {
            folder_id: [if resource == Resource::HostProfile {
                1
            } else {
                2
            }; 24],
        };
    }
    if resource.process() {
        return Observation::Process {
            pid: 100 + resource as u32,
            created: 1234 + resource as u64,
            image_id: [3; 24],
            job: identity.name(resource),
        };
    }
    if resource.shared_acl() {
        Observation::SharedAclApplied
    } else {
        Observation::ExclusiveKernelObject
    }
}
fn events(identity: &Identity) -> Vec<Event> {
    let mut events = Vec::new();
    for resource in ALL {
        let nonce = super::super::random_id().unwrap();
        events.push(Event::CreateIntent {
            resource,
            nonce: nonce.clone(),
            subject: identity.subject(resource).into(),
            name: identity.name(resource),
            acl: acl_intent(resource),
        });
        events.push(Event::Created {
            resource,
            nonce,
            observed: observation(identity, resource),
        });
    }
    for resource in [
        Resource::ChildProcess,
        Resource::RunnerProcess,
        Resource::BootstrapProcess,
        Resource::ChildJob,
        Resource::RunnerJob,
        Resource::BootstrapJob,
        Resource::AccountProfile,
        Resource::NamespaceKeeper,
        Resource::NamespaceRoot,
        Resource::BootstrapDesktop,
        Resource::RunnerDesktop,
        Resource::SessionBaseNamedObjectsGrant,
        Resource::WindowStationGrant,
        Resource::HostProfile,
    ] {
        let nonce = super::super::random_id().unwrap();
        events.push(Event::RemoveIntent {
            resource,
            nonce: nonce.clone(),
        });
        events.push(Event::Removed { resource, nonce });
    }
    events
}

#[test]
fn candidate_resource_complete_plan_and_ordered_teardown_produce_exact_receipt() {
    let identity = identity();
    let connection = connection(&identity);
    let journal = fixture(&connection, &identity, true);
    for event in events(&identity) {
        journal.append(event).unwrap();
    }
    let receipt = journal.recovered().unwrap();
    assert!(receipt.matches(&identity.execution));
    let mut different = identity.execution.clone();
    different.pending_id[0] ^= 1;
    assert!(!receipt.matches(&different));
    // Completion is retained as audit evidence; no implicit history deletion.
    assert_eq!(journal.replay().unwrap().events, 56);
    assert!(ResourceJournal::open(&connection, identity, Authority::Fixture, true).is_err());
}

#[test]
fn candidate_resource_every_interrupted_phase_preserves_exact_committed_history() {
    let identity = identity();
    let events = events(&identity);
    for interrupted in 0..=events.len() {
        let connection = connection(&identity);
        {
            let journal = fixture(&connection, &identity, true);
            for event in &events[..interrupted] {
                journal.append(event.clone()).unwrap();
            }
        }
        let journal = fixture(&connection, &identity, false);
        let replay = journal.replay().unwrap();
        assert_eq!(replay.events, interrupted);
        if interrupted == 0 || interrupted == events.len() {
            assert!(journal.recovered().is_ok());
        } else {
            assert!(
                journal.recovered().is_err(),
                "retired interruption {interrupted}"
            );
        }
        if let Some(Event::CreateIntent { resource, .. }) = events.get(interrupted.wrapping_sub(1))
        {
            assert!(matches!(
                replay.states.get(resource),
                Some(State::Creating { .. })
            ));
            assert!(
                journal.remove_intent(*resource).is_err(),
                "assumed ownership after interrupted creation"
            );
        }
        if let Some(Event::RemoveIntent { resource, .. }) = events.get(interrupted.wrapping_sub(1))
        {
            assert!(matches!(
                replay.states.get(resource),
                Some(State::Removing { .. })
            ));
            assert!(
                journal.remove_intent(*resource).is_err(),
                "repeated ambiguous destructive effect"
            );
        }
        assert_eq!(journal.replay().unwrap().events, interrupted);
    }
}

#[test]
fn candidate_resource_write_ahead_failure_runs_no_effect_and_failed_receipt_stays_ambiguous() {
    let identity = identity();
    let connection = connection(&identity);
    let journal = fixture(&connection, &identity, true);
    connection.execute_batch("CREATE TRIGGER fail_write BEFORE INSERT ON candidate_resource_events BEGIN SELECT RAISE(ABORT,'injected'); END;").unwrap();
    let mut effects = 0;
    if journal.create_intent(Resource::HostProfile, None).is_ok() {
        effects += 1;
    }
    assert_eq!(effects, 0);
    assert_eq!(journal.replay().unwrap().events, 0);
    connection.execute_batch("DROP TRIGGER fail_write").unwrap();
    let nonce = journal.create_intent(Resource::HostProfile, None).unwrap();
    effects += 1; // simulate successful exclusive native creation
    connection.execute_batch("CREATE TRIGGER fail_write BEFORE INSERT ON candidate_resource_events BEGIN SELECT RAISE(ABORT,'injected'); END;").unwrap();
    assert!(
        journal
            .created(
                Resource::HostProfile,
                nonce,
                observation(&identity, Resource::HostProfile)
            )
            .is_err()
    );
    assert_eq!(effects, 1);
    assert!(matches!(
        journal.replay().unwrap().states[&Resource::HostProfile],
        State::Creating { .. }
    ));
    assert!(journal.recovered().is_err());
    assert!(journal.remove_intent(Resource::HostProfile).is_err());
}

#[test]
fn candidate_resource_receipts_require_nonce_exact_type_and_derived_identity() {
    let identity = identity();
    let connection = connection(&identity);
    let journal = fixture(&connection, &identity, true);
    let first = events(&identity).remove(0);
    if let Event::CreateIntent {
        resource,
        nonce,
        subject,
        name,
        acl,
    } = first
    {
        for (subject, name) in [
            ("S-1-5-18".into(), name.clone()),
            (subject.clone(), "Other.Profile".into()),
        ] {
            assert!(
                journal
                    .append(Event::CreateIntent {
                        resource,
                        nonce: nonce.clone(),
                        subject,
                        name,
                        acl: acl.clone()
                    })
                    .is_err()
            );
        }
        journal
            .append(Event::CreateIntent {
                resource,
                nonce: nonce.clone(),
                subject,
                name,
                acl,
            })
            .unwrap();
        assert!(
            journal
                .created(
                    resource,
                    super::super::random_id().unwrap(),
                    observation(&identity, resource)
                )
                .is_err()
        );
        assert!(
            journal
                .created(resource, nonce.clone(), Observation::ExclusiveKernelObject)
                .is_err()
        );
        assert!(
            journal
                .created(
                    resource,
                    nonce.clone(),
                    Observation::SyntheticProfile { folder_id: [0; 24] }
                )
                .is_err()
        );
        assert!(
            journal
                .create_intent(
                    Resource::WindowStationGrant,
                    acl_intent(Resource::WindowStationGrant)
                )
                .is_err()
        );
        journal
            .created(resource, nonce, observation(&identity, resource))
            .unwrap();
    }
    assert_eq!(journal.replay().unwrap().events, 2);
}

#[test]
fn candidate_resource_teardown_requires_process_quiescence_and_host_profile_last() {
    let identity = identity();
    let connection = connection(&identity);
    let journal = fixture(&connection, &identity, true);
    let events = events(&identity);
    for event in &events[..28] {
        journal.append(event.clone()).unwrap();
    }
    for resource in [
        Resource::HostProfile,
        Resource::AccountProfile,
        Resource::NamespaceKeeper,
        Resource::NamespaceRoot,
        Resource::WindowStationGrant,
        Resource::ChildJob,
        Resource::RunnerJob,
    ] {
        assert!(journal.remove_intent(resource).is_err());
    }
    for event in &events[28..54] {
        journal.append(event.clone()).unwrap();
    }
    let nonce = journal.remove_intent(Resource::HostProfile).unwrap();
    assert!(journal.recovered().is_err());
    journal.removed(Resource::HostProfile, nonce).unwrap();
    journal.recovered().unwrap();
}

#[test]
fn candidate_resource_malformed_foreign_or_changed_history_is_never_repaired() {
    let identity = identity();
    for defect in 0..4 {
        let connection = connection(&identity);
        let journal = fixture(&connection, &identity, true);
        match defect {
            0 => {
                connection
                    .execute("UPDATE candidate_resource_runs SET session_id=8", [])
                    .unwrap();
            }
            1 => {
                connection
                    .execute(
                        "INSERT INTO candidate_resource_events VALUES('foreign',0,'{}')",
                        [],
                    )
                    .unwrap();
            }
            2 => {
                connection
                    .execute(
                        "INSERT INTO candidate_resource_events VALUES(?1,3,'{}')",
                        [&identity.execution.identity.container],
                    )
                    .unwrap();
            }
            _ => {
                connection
                    .execute("UPDATE candidate_resource_runs SET execution_json='{}'", [])
                    .unwrap();
            }
        }
        let before: i64 = connection
            .query_row("SELECT count(*) FROM candidate_resource_events", [], |r| {
                r.get(0)
            })
            .unwrap();
        assert!(journal.recovered().is_err());
        assert_eq!(
            connection
                .query_row("SELECT count(*) FROM candidate_resource_events", [], |r| {
                    r.get::<_, i64>(0)
                })
                .unwrap(),
            before
        );
    }
    let mut acl_only = identity.clone();
    acl_only.execution.scope = super::super::SCOPE.into();
    let connection = connection(&acl_only);
    assert!(ResourceJournal::open(&connection, acl_only, Authority::Fixture, true).is_err());
}

#[test]
fn candidate_resource_bootstrap_job_is_independent_but_process_needs_desktop_and_job() {
    let identity = identity();
    for (desktop, job, allowed) in [
        (false, false, false),
        (false, true, false),
        (true, false, false),
        (true, true, true),
    ] {
        let connection = connection(&identity);
        let journal = fixture(&connection, &identity, true);
        if desktop {
            for event in events(&identity).into_iter().take(8) {
                journal.append(event).unwrap();
            }
        }
        if job {
            let nonce = journal.create_intent(Resource::BootstrapJob, None).unwrap();
            journal
                .created(
                    Resource::BootstrapJob,
                    nonce,
                    Observation::EmptyBootstrapJob { policy_version: 1 },
                )
                .unwrap();
        }
        assert_eq!(
            journal
                .create_intent(Resource::BootstrapProcess, None)
                .is_ok(),
            allowed
        );
    }
    // Existing complete resource histories and host-profile-last teardown are
    // still covered above; the independent empty job does not waive any
    // profile, namespace, window-station or desktop prerequisite.
    let connection = connection(&identity);
    let journal = fixture(&connection, &identity, true);
    let nonce = journal.create_intent(Resource::BootstrapJob, None).unwrap();
    assert!(
        journal
            .created(
                Resource::BootstrapJob,
                nonce.clone(),
                Observation::EmptyBootstrapJob { policy_version: 2 }
            )
            .is_err()
    );
    journal
        .created(
            Resource::BootstrapJob,
            nonce,
            Observation::EmptyBootstrapJob { policy_version: 1 },
        )
        .unwrap();
    // Namespace birth is independently admissible; its adapter still rejects
    // mixed plans. A namespace intent cannot waive desktop/profile prerequisites.
    journal
        .create_intent(Resource::NamespaceRoot, None)
        .unwrap();
    assert!(
        journal
            .create_intent(Resource::BootstrapDesktop, None)
            .is_err()
    );
}

#[test]
fn candidate_resource_legacy_plan_refuses_atomic_bootstrap_scope() {
    let mut identity = identity();
    identity.execution.scope = super::super::BOOTSTRAP_PLAN_SCOPE.into();
    let connection = connection(&identity);
    assert!(ResourceJournal::open(&connection, identity, Authority::Fixture, true).is_err());
    let count: i64 = connection
        .query_row(
            "SELECT count(*) FROM sqlite_schema WHERE name='candidate_resource_runs'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(count, 0);
}

#[test]
fn candidate_resource_folder_only_profile_receipt_cannot_admit_or_retire_storage() {
    for resource in [Resource::HostProfile, Resource::AccountProfile] {
        let identity = identity();
        let connection = connection(&identity);
        let journal = fixture(&connection, &identity, true);
        let mut pending = None;
        for event in events(&identity) {
            if let Event::CreateIntent {
                resource: role,
                nonce,
                ..
            } = &event
            {
                if *role == resource {
                    pending = Some(nonce.clone());
                }
            }
            journal.append(event).unwrap();
            if pending.is_some() {
                break;
            }
        }
        let nonce = pending.unwrap();
        let count = journal.replay().unwrap().events;
        for folder_id in [[0; 24], [9; 24]] {
            let result =
                journal.created(resource, nonce.clone(), Observation::Profile { folder_id });
            assert!(result.is_err(), "folder-only profile receipt was admitted");
        }
        assert_eq!(journal.replay().unwrap().events, count);
        assert!(matches!(
            journal.replay().unwrap().states[&resource],
            State::Creating { .. }
        ));
        assert!(journal.remove_intent(resource).is_err());
        assert!(journal.recovered().is_err());
        assert!(
            journal
                .create_intent(Resource::BootstrapDesktop, None)
                .is_err()
        );
        assert!(journal.create_intent(Resource::ChildJob, None).is_err());
    }
}

#[test]
fn candidate_resource_folder_only_profile_history_is_preserved_and_refused_on_reopen() {
    let identity = identity();
    let connection = connection(&identity);
    let journal = fixture(&connection, &identity, true);
    let nonce = journal.create_intent(Resource::HostProfile, None).unwrap();
    let legacy = serde_json::to_string(&Event::Created {
        resource: Resource::HostProfile,
        nonce,
        observed: Observation::Profile { folder_id: [9; 24] },
    })
    .unwrap();
    connection
        .execute(
            "INSERT INTO candidate_resource_events VALUES(?1,1,?2)",
            params![identity.execution.identity.container, legacy],
        )
        .unwrap();
    assert!(journal.recovered().is_err());
    assert!(ResourceJournal::open(&connection, identity, Authority::Fixture, false).is_err());
    let saved: String = connection
        .query_row(
            "SELECT event_json FROM candidate_resource_events WHERE sequence=1",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(saved, legacy);
    let rows: i64 = connection
        .query_row(
            "SELECT count(*) FROM candidate_resource_events",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(rows, 2);
}

#[test]
fn candidate_resource_synthetic_profile_cannot_enter_live_or_stopped_journal() {
    for authority in [Authority::FixtureLive, Authority::FixtureStopped] {
        let identity = identity();
        let connection = connection(&identity);
        let journal = fixture(&connection, &identity, true);
        let nonce = journal.create_intent(Resource::HostProfile, None).unwrap();
        let guarded =
            ResourceJournal::open(&connection, identity.clone(), authority, false).unwrap();
        assert!(
            guarded
                .created(
                    Resource::HostProfile,
                    nonce.clone(),
                    observation(&identity, Resource::HostProfile),
                )
                .is_err()
        );
        assert_eq!(journal.replay().unwrap().events, 1);
        journal
            .created(
                Resource::HostProfile,
                nonce,
                observation(&identity, Resource::HostProfile),
            )
            .unwrap();
        // Native-effect test authorities cannot reopen model-only history.
        assert!(guarded.replay().is_err());
        assert!(guarded.recovered().is_err());
        assert_eq!(journal.replay().unwrap().events, 2);
    }
}

#[test]
fn candidate_resource_folder_only_profile_cannot_be_promoted_by_a_cleanup_receipt() {
    let identity = identity();
    let connection = connection(&identity);
    let journal = fixture(&connection, &identity, true);
    let nonce = journal.create_intent(Resource::HostProfile, None).unwrap();
    let remove_nonce = super::super::random_id().unwrap();
    let history = [
        Event::Created {
            resource: Resource::HostProfile,
            nonce,
            observed: Observation::Profile { folder_id: [8; 24] },
        },
        Event::RemoveIntent {
            resource: Resource::HostProfile,
            nonce: remove_nonce.clone(),
        },
        Event::Removed {
            resource: Resource::HostProfile,
            nonce: remove_nonce,
        },
    ];
    for (index, event) in history.iter().enumerate() {
        connection
            .execute(
                "INSERT INTO candidate_resource_events VALUES(?1,?2,?3)",
                params![
                    identity.execution.identity.container,
                    index + 1,
                    serde_json::to_string(event).unwrap()
                ],
            )
            .unwrap();
    }
    // Even an apparently complete old log is no composite ownership proof.
    assert!(journal.recovered().is_err());
    assert!(ResourceJournal::open(&connection, identity, Authority::Fixture, false).is_err());
    let count: usize = connection
        .query_row(
            "SELECT count(*) FROM candidate_resource_events",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(count, 4);
}
