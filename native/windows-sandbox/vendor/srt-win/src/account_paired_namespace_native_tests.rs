//! Host-only real job/namespace effects, with no account logon or profile API.
use super::*;
use crate::account_state::execution::resources::FixtureRpcPolicy as PolicyKind;
use crate::account_state::execution::resources::namespace_native::RootBirthKind;
use paired_plan::native::namespace::{NAMESPACE_CHECKPOINT, fixture_require_absent};

fn rows(f: &PairFixture) -> usize {
    f.db.connection()
        .unwrap()
        .query_row("SELECT count(*) FROM candidate_paired_namespace", [], |r| {
            r.get(0)
        })
        .unwrap()
}
struct Reset;
impl Drop for Reset {
    fn drop(&mut self) {
        NAMESPACE_CHECKPOINT.with(|s| {
            s.borrow_mut().take();
        });
    }
}
#[test]
fn paired_namespace_native_original_family_completion_closes_root_last() {
    let mut fixture = PairFixture::new();
    let owner = fixture.owner_server.take().unwrap();
    let consumer = fixture.consumer_server.take().unwrap();
    let plan = fixture.plan();
    let hits = Rc::new(RefCell::new(Vec::new()));
    let observed = hits.clone();
    let owner_name = fixture.name(Role::Owner);
    let consumer_name = fixture.name(Role::Consumers);
    let _reset = Reset;
    NAMESPACE_CHECKPOINT.with(|s| {
        *s.borrow_mut() = Some(Box::new(move |phase, _, root| {
            observed.borrow_mut().push(phase.to_owned());
            if phase == "using" {
                assert!(root.is_some());
                assert!(counts(&owner_name).unwrap().0 >= 1);
                assert!(counts(&consumer_name).unwrap().0 >= 3);
            }
            if matches!(phase, "retiring" | "closed" | "completed") {
                assert_eq!(counts(&owner_name).unwrap().0, 0);
                assert_eq!(counts(&consumer_name).unwrap().0, 0);
            }
            Ok(())
        }));
    });
    let completed = plan
        .create_jobs()
        .unwrap()
        .with_empty_namespace()
        .unwrap()
        .fixture_run_host_family(owner, consumer)
        .unwrap();
    completed.verify().unwrap();
    assert_eq!(rows(&fixture), 4);
    assert_eq!(
        *hits.borrow(),
        [
            "born",
            "ready",
            "using",
            "before-retire",
            "retiring",
            "closed",
            "completed"
        ]
    );
    fixture_require_absent(&plan).unwrap();
    drop(completed);
    assert_eq!(counts(&fixture.name(Role::Owner)), None);
    assert_eq!(counts(&fixture.name(Role::Consumers)), None);
    assert!(fixture._fixture.path().is_file());
}
#[test]
fn paired_namespace_native_failed_receipts_preserve_prefix_and_release_jobs() {
    for (phase, expected_rows) in [
        ("born", 1),
        ("ready", 2),
        ("using", 2),
        ("before-retire", 2),
        ("retiring", 3),
        ("closed", 3),
    ] {
        let mut fixture = PairFixture::new();
        let owner = fixture.owner_server.take().unwrap();
        let consumer = fixture.consumer_server.take().unwrap();
        let plan = fixture.plan();
        let hits = Rc::new(Cell::new(0));
        let observed = hits.clone();
        let _reset = Reset;
        NAMESPACE_CHECKPOINT.with(|s| {
            *s.borrow_mut() = Some(Box::new(move |at, sql, _| {
                if at == phase {
                    observed.set(observed.get() + 1);
                    if at == "ready" || at == "using" {
                        anyhow::bail!("injected original namespace boundary failure");
                    }
                    sql.execute_batch("PRAGMA query_only=ON")?;
                }
                Ok(())
            }));
        });
        let result = plan
            .create_jobs()
            .unwrap()
            .with_empty_namespace()
            .and_then(|pair| pair.fixture_run_host_family(owner, consumer));
        assert!(result.is_err(), "{phase}");
        assert_eq!(hits.get(), 1, "{phase}");
        fixture
            .db
            .connection()
            .unwrap()
            .execute_batch("PRAGMA query_only=OFF")
            .unwrap();
        assert_eq!(rows(&fixture), expected_rows, "{phase}");
        fixture_require_absent(&plan).unwrap();
        assert_eq!(counts(&fixture.name(Role::Owner)), None);
        assert_eq!(counts(&fixture.name(Role::Consumers)), None);
        assert!(fixture._fixture.path().is_file());
    }
}
#[test]
fn paired_namespace_native_birth_refuses_used_original_job() {
    let fixture = PairFixture::new();
    let plan = fixture.plan();
    let jobs = plan.create_jobs().unwrap();
    drop(jobs.prepare_bootstrap(Role::Owner).unwrap());
    assert!(jobs.with_empty_namespace().is_err());
    assert_eq!(rows(&fixture), 0);
    fixture_require_absent(&plan).unwrap();
    assert_eq!(counts(&fixture.name(Role::Owner)), None);
    assert_eq!(counts(&fixture.name(Role::Consumers)), None);
}

#[test]
fn paired_namespace_native_held_root_prevents_false_removal_receipt() {
    let mut fixture = PairFixture::new();
    let owner = fixture.owner_server.take().unwrap();
    let consumer = fixture.consumer_server.take().unwrap();
    let plan = fixture.plan();
    let held = Rc::new(RefCell::new(None));
    let retained = held.clone();
    let _reset = Reset;
    NAMESPACE_CHECKPOINT.with(|s| {
        *s.borrow_mut() = Some(Box::new(move |phase, _, root| {
            if phase == "ready" {
                let mut copy = HANDLE::default();
                unsafe {
                    DuplicateHandle(
                        GetCurrentProcess(),
                        root.unwrap(),
                        GetCurrentProcess(),
                        &mut copy,
                        0,
                        false,
                        windows::Win32::Foundation::DUPLICATE_SAME_ACCESS,
                    )?;
                }
                *retained.borrow_mut() = Some(OwnedHandle(copy));
            }
            Ok(())
        }));
    });
    let result = plan
        .create_jobs()
        .unwrap()
        .with_empty_namespace()
        .unwrap()
        .fixture_run_host_family(owner, consumer);
    assert!(result.is_err());
    assert_eq!(rows(&fixture), 3);
    assert!(fixture_require_absent(&plan).is_err());
    assert_eq!(counts(&fixture.name(Role::Owner)), None);
    assert_eq!(counts(&fixture.name(Role::Consumers)), None);
    drop(held.borrow_mut().take());
    fixture_require_absent(&plan).unwrap();
    assert_eq!(
        rows(&fixture),
        3,
        "late absence is not completion authority"
    );
}

pub(super) fn hosted(fixture: Fixture, password: &str, case: &str) -> Result<serde_json::Value> {
    hosted_inner(fixture, password, case, None, false, false, false)
}
pub(super) fn hosted_host_profile(
    fixture: Fixture,
    password: &str,
    case: &str,
) -> Result<serde_json::Value> {
    ensure!(
        [
            "unused-success",
            "used-success",
            "used-capture-receipt-failure",
            "used-remove-intent-failure",
            "used-remove-receipt-failure"
        ]
        .contains(&case),
        "unknown paired host profile case"
    );
    hosted_inner(
        fixture,
        password,
        "success",
        Some(case),
        false,
        false,
        false,
    )
}
pub(super) fn hosted_station_profile(
    fixture: Fixture,
    password: &str,
    case: &str,
) -> Result<serde_json::Value> {
    ensure!(
        ["used-success", "used-capture-receipt-failure"].contains(&case),
        "unknown station/profile case"
    );
    hosted_inner(fixture, password, "success", Some(case), true, false, false)
}
pub(super) fn hosted_desktop_profile(
    fixture: Fixture,
    password: &str,
    case: &str,
) -> Result<serde_json::Value> {
    ensure!(
        ["used-success", "used-capture-receipt-failure"].contains(&case),
        "unknown desktop/profile case"
    );
    hosted_inner(fixture, password, "success", Some(case), true, true, false)
}
pub(super) fn hosted_initializer_profile(
    fixture: Fixture,
    password: &str,
    case: &str,
) -> Result<serde_json::Value> {
    ensure!(
        ["used-success", "used-capture-receipt-failure"].contains(&case),
        "unknown initializer/profile case"
    );
    hosted_inner(fixture, password, "success", Some(case), true, true, true)
}
fn hosted_inner(
    fixture: Fixture,
    password: &str,
    case: &str,
    host_case: Option<&str>,
    with_station: bool,
    with_desktop: bool,
    with_initializer: bool,
) -> Result<serde_json::Value> {
    hosted_inner_policy(
        fixture,
        password,
        case,
        host_case,
        with_station,
        with_desktop,
        with_initializer,
        PolicyKind::BaselineV1,
        RootBirthKind::LegacyUnspecified,
    )
}
pub(super) fn hosted_rpc_v3_profile(fixture: Fixture, password: &str) -> Result<serde_json::Value> {
    crate::account_rpc_v3_lifecycle_fixture::require_ordinary_live()?;
    hosted_inner_policy(
        fixture,
        password,
        "success",
        Some("used-success"),
        true,
        true,
        true,
        crate::account_rpc_v3_lifecycle_fixture::POLICY,
        RootBirthKind::LegacyUnspecified,
    )
}
pub(super) fn hosted_rpc_v3_shadow_profile(
    fixture: Fixture,
    password: &str,
) -> Result<serde_json::Value> {
    crate::account_rpc_v3_lifecycle_fixture::require_shadow_live()?;
    hosted_inner_policy(
        fixture,
        password,
        "success",
        Some("used-success"),
        true,
        true,
        true,
        crate::account_rpc_v3_lifecycle_fixture::POLICY,
        RootBirthKind::SessionShadowDirectoryV1,
    )
}
fn hosted_inner_policy(
    fixture: Fixture,
    password: &str,
    case: &str,
    host_case: Option<&str>,
    with_station: bool,
    with_desktop: bool,
    with_initializer: bool,
    expected_policy: PolicyKind,
    expected_birth: RootBirthKind,
) -> Result<serde_json::Value> {
    use crate::account_state::execution::resources::NativeHostProfile;
    use paired_plan::native::account::{ACCOUNT_CRASH_CHECKPOINT, profile::PROFILE_CHECKPOINT};
    use paired_plan::native::namespace::host_profile::HOST_PROFILE_CHECKPOINT;
    ensure!(
        ["success", "before-capture", "closed"].contains(&case),
        "unknown namespace writer composition case"
    );
    let mut fixture = PairFixture::from_fixture_image(fixture, true);
    let owner = fixture.owner_server.take().unwrap();
    let consumer = fixture.consumer_server.take().unwrap();
    let marker = std::fs::read(fixture._fixture.path())?;
    let plan = match expected_policy {
        PolicyKind::BaselineV1 => {
            ensure!(
                expected_birth == RootBirthKind::LegacyUnspecified,
                "baseline root selection differs"
            );
            fixture.plan()
        }
        PolicyKind::SystemNsiReadInheritanceV3 => {
            ensure!(
                case == "success"
                    && host_case == Some("used-success")
                    && with_station
                    && with_desktop
                    && with_initializer,
                "V3 lifecycle fixture shape differs"
            );
            match expected_birth {
                RootBirthKind::LegacyUnspecified => fixture.plan_rpc_v3_lifecycle(),
                RootBirthKind::SessionShadowDirectoryV1 => fixture.plan_rpc_v3_shadow_lifecycle(),
                _ => anyhow::bail!("unselected V3 namespace constructor"),
            }
        }
        _ => anyhow::bail!("unselected lifecycle policy"),
    };
    let raw_plan: String = fixture.db.connection()?.query_row(
        "SELECT plan_json FROM candidate_paired_bootstrap_plan",
        [],
        |r| r.get(0),
    )?;
    let typed_plan: paired_plan::Plan = serde_json::from_str(&raw_plan)?;
    typed_plan.fixture_require_rpc_policy(expected_policy)?;
    typed_plan.fixture_require_namespace_birth(expected_birth)?;
    let host_success = host_case.is_none_or(|c| c.ends_with("-success"));
    let success = case == "success" && host_success;
    let host_before = NativeHostProfile::fixture_effect_counts();
    let host_phases = Rc::new(RefCell::new(Vec::new()));
    let host_hits = Rc::new(Cell::new(0));
    let host_path = Rc::new(RefCell::new(None));
    if let Some(selected) = host_case {
        let selected = selected.to_owned();
        let observed = host_phases.clone();
        let hits = host_hits.clone();
        let retained_path = host_path.clone();
        let container = fixture.active.record.identity.container.clone();
        let actor = fixture.active.record.owner.clone();
        let owner_name = fixture.name(Role::Owner);
        let consumer_name = fixture.name(Role::Consumers);
        HOST_PROFILE_CHECKPOINT.with(|s| {
            *s.borrow_mut() = Some(Box::new(move |phase, sql, used| {
                observed.borrow_mut().push(phase.to_owned());
                if phase == "using" {
                    used.context("original host profile use guard missing")?
                        .fixture_add_registry_data(false)?;
                    let path =
                        crate::account_profile_filesystem::RetainedProfileFilesystem::fixture_ac(
                            &container, &actor,
                        )?
                        .join("paired-host-runtime-data.bin");
                    use std::io::Write;
                    let mut file = std::fs::OpenOptions::new()
                        .create_new(true)
                        .write(true)
                        .open(&path)?;
                    file.write_all(b"bounded paired host profile data")?;
                    file.sync_all()?;
                    *retained_path.borrow_mut() = Some(path);
                }
                if matches!(
                    phase,
                    "before_capture"
                        | "observed_after_use"
                        | "captured"
                        | "retiring"
                        | "closed"
                        | "deleted"
                        | "absent"
                        | "completed"
                ) {
                    ensure!(
                        counts(&owner_name).is_some_and(|n| n.0 == 0)
                            && counts(&consumer_name).is_some_and(|n| n.0 == 0),
                        "host profile cleanup preceded both original jobs stopping"
                    );
                    let roots: usize = sql.query_row(
                        "SELECT count(*) FROM candidate_paired_namespace",
                        [],
                        |r| r.get(0),
                    )?;
                    let accounts: usize = sql.query_row(
                        "SELECT count(*) FROM candidate_paired_profile_events",
                        [],
                        |r| r.get(0),
                    )?;
                    ensure!(
                        (roots, accounts) == (2, 11),
                        "host profile cleanup lost namespace or preceded account profile removal"
                    );
                }
                if phase == "before_capture" {
                    if with_initializer {
                        let rows: usize = sql.query_row(
                            "SELECT count(*) FROM candidate_paired_initializer",
                            [],
                            |r| r.get(0),
                        )?;
                        ensure!(
                            rows == 7,
                            "host capture preceded original initializer release"
                        );
                    }
                    if selected != "unused-success" {
                        used.context("original host profile use guard missing before capture")?
                            .fixture_require_registry_data()?;
                    }
                    if let Some(path) = retained_path.borrow().as_ref() {
                        ensure!(
                            std::fs::read(path)? == b"bounded paired host profile data",
                            "account cleanup damaged original host profile data"
                        );
                    }
                }
                if (selected == "used-capture-receipt-failure" && phase == "observed_after_use")
                    || (selected == "used-remove-intent-failure" && phase == "captured")
                    || (selected == "used-remove-receipt-failure" && phase == "absent")
                {
                    hits.set(hits.get() + 1);
                    sql.execute_batch("PRAGMA query_only=ON")?;
                }
                Ok(())
            }));
        });
    }
    let account_refs = Rc::new(RefCell::new(Vec::new()));
    let retained = account_refs.clone();
    ACCOUNT_CRASH_CHECKPOINT.with(|s| {
        *s.borrow_mut() = Some(Box::new(move |phase, role, _, observed| {
            if phase == "acknowledged" {
                retained.borrow_mut().push((role, observed));
            }
        }));
    });
    let bootstrap_refs = Rc::new(RefCell::new(Vec::new()));
    let retained = bootstrap_refs.clone();
    NATIVE_CHECKPOINT.with(|s| {
        *s.borrow_mut() = Some(Box::new(move |phase, _, _, raw| {
            if phase == "bootstrap-born" {
                retained.borrow_mut().push(retain_original(raw.unwrap()));
            }
        }));
    });
    let family_refs = Rc::new(RefCell::new(Vec::new()));
    let retained = family_refs.clone();
    crate::account_control::writer_fixture::FAMILY_OBSERVED.with(|s| {
        *s.borrow_mut() = Some(Box::new(move |raw| {
            retained.borrow_mut().push(retain_original(raw));
        }));
    });
    crate::account_control::writer_fixture::REPORT.with(|s| {
        s.borrow_mut().take();
    });
    crate::account_control::writer_fixture::PROGRESS.with(|s| s.set(0));
    let cut = case.to_owned();
    let profile_hits = Rc::new(Cell::new(0));
    let hit = profile_hits.clone();
    PROFILE_CHECKPOINT.with(|s| {
        *s.borrow_mut() = Some(Box::new(move |phase, sql| {
            if with_initializer && phase == "before-capture" {
                let rows: usize = sql
                    .query_row(
                        "SELECT count(*) FROM candidate_paired_initializer",
                        [],
                        |r| r.get(0),
                    )
                    .unwrap();
                assert_eq!(
                    rows, 5,
                    "initializer not retained through account profile lifetime"
                );
            }
            if cut == "before-capture" && phase == cut {
                hit.set(hit.get() + 1);
                sql.execute_batch("PRAGMA query_only=ON").unwrap();
            }
        }));
    });
    let cut = case.to_owned();
    let namespace_phases = Rc::new(RefCell::new(Vec::new()));
    let observed = namespace_phases.clone();
    let owner_name = fixture.name(Role::Owner);
    let consumer_name = fixture.name(Role::Consumers);
    NAMESPACE_CHECKPOINT.with(|s| {
        *s.borrow_mut() = Some(Box::new(move |phase, sql, _| {
            observed.borrow_mut().push(phase.to_owned());
            if matches!(phase, "before-retire" | "retiring" | "closed" | "completed") {
                ensure!(
                    counts(&owner_name).is_some_and(|n| n.0 == 0)
                        && counts(&consumer_name).is_some_and(|n| n.0 == 0),
                    "namespace retirement preceded whole original pair drain"
                );
            }
            if cut == "closed" && phase == cut {
                sql.execute_batch("PRAGMA query_only=ON")?;
            }
            Ok(())
        }));
    });
    let station_before = if with_station {
        Some(
            crate::account_state::execution::resources::station::Station::fixture_snapshot_current(
            )?,
        )
    } else {
        None
    };
    let started = std::time::Instant::now();
    let pair = plan.create_jobs()?.with_empty_namespace()?;
    let pair = if host_case == Some("unused-success") {
        pair.with_unused_host_profile()?
    } else if host_case.is_some() {
        pair.with_host_profile_for_use()?
    } else {
        pair
    };
    let result = if with_station {
        let pair = pair.with_station()?;
        let pair = if with_desktop {
            pair.with_unused_desktop()?
        } else {
            pair
        };
        if with_initializer {
            pair.fixture_run_initialized_profile_lifetime(
                owner,
                consumer,
                Duration::from_secs(10),
                password,
            )
            .and_then(|done| done.verify())
        } else {
            pair.fixture_run_profile_lifetime(owner, consumer, Duration::from_secs(10), password)
                .and_then(|done| done.verify())
        }
    } else {
        pair.fixture_run_profile_lifetime(owner, consumer, Duration::from_secs(10), password)
            .and_then(|done| done.verify())
    };
    HOST_PROFILE_CHECKPOINT.with(|s| {
        s.borrow_mut().take();
    });
    NAMESPACE_CHECKPOINT.with(|s| {
        s.borrow_mut().take();
    });
    NATIVE_CHECKPOINT.with(|s| {
        s.borrow_mut().take();
    });
    PROFILE_CHECKPOINT.with(|s| {
        s.borrow_mut().take();
    });
    ACCOUNT_CRASH_CHECKPOINT.with(|s| {
        s.borrow_mut().take();
    });
    crate::account_control::writer_fixture::FAMILY_OBSERVED.with(|s| {
        s.borrow_mut().take();
    });
    let report = crate::account_control::writer_fixture::REPORT
        .with(|s| s.borrow_mut().take())
        .context("namespace writer family report missing")?;
    let progress = crate::account_control::writer_fixture::PROGRESS.with(|s| s.get());
    fixture
        .db
        .connection()?
        .execute_batch("PRAGMA query_only=OFF")?;
    if success {
        result?;
    } else {
        require_readonly(&result.err().context("namespace composition fault passed")?);
    }
    let account_codes = account_refs
        .borrow_mut()
        .drain(..)
        .map(|(role, r)| Ok((role.index(), r.fixture_wait_code()?)))
        .collect::<Result<Vec<_>>>()?;
    let family_codes: Vec<_> = family_refs
        .borrow_mut()
        .drain(..)
        .map(|r| exited_code(&r))
        .collect();
    ensure!(
        account_codes.len() == 2 && bootstrap_refs.borrow().len() == 2 && family_codes == [1, 1],
        "namespace composition original process evidence incomplete"
    );
    for original in bootstrap_refs.borrow_mut().drain(..) {
        dead(&original);
    }
    ensure!(
        progress == 12 && profile_hits.get() == u32::from(case == "before-capture"),
        "namespace composition progress or fault count differs"
    );
    if case != "before-capture" {
        ensure!(
            account_codes.iter().all(|(_, c)| *c == 0x4150),
            "namespace composition original account exit failed"
        );
    }
    let profile_rows: usize = fixture.db.connection()?.query_row(
        "SELECT count(*) FROM candidate_paired_profile_events",
        [],
        |r| r.get(0),
    )?;
    let resume_rows: usize = fixture.db.connection()?.query_row(
        "SELECT count(*) FROM candidate_paired_account_resume_events",
        [],
        |r| r.get(0),
    )?;
    ensure!(
        profile_rows == if case == "before-capture" { 6 } else { 11 }
            && resume_rows == 3
            && rows(&fixture)
                == if success {
                    4
                } else if case == "closed" {
                    3
                } else {
                    2
                }
            && fixture.rows(Role::Owner) == if case == "before-capture" { 7 } else { 9 }
            && fixture.rows(Role::Consumers) == 9,
        "namespace composition durable prefix differs"
    );
    ensure!(
        report["descendants_write_profile"] == true
            && report["descendant_files"]
                .as_array()
                .is_some_and(|a| a.len() == 2)
            && report["live_after_consumer_exit"] == true
            && report["consumer_job_retired_before_capture"] == true,
        "namespace composition fixed writing descendants incomplete"
    );
    fixture_require_absent(&plan)?;
    ensure!(
        counts(&fixture.name(Role::Owner)).is_none()
            && counts(&fixture.name(Role::Consumers)).is_none(),
        "namespace composition original jobs survived closure"
    );
    ensure!(
        std::fs::read(fixture._fixture.path())? == marker,
        "namespace composition marker changed"
    );
    let events: Vec<serde_json::Value> = fixture
        .db
        .connection()?
        .prepare("SELECT event_json FROM candidate_paired_namespace ORDER BY sequence")?
        .query_map([], |r| r.get::<_, String>(0))?
        .map(|r| Ok(serde_json::from_str(&r?)?))
        .collect::<Result<_>>()?;
    let host_events: Vec<serde_json::Value> = fixture
        .db
        .connection()?
        .prepare("SELECT event_json FROM candidate_paired_host_profile ORDER BY sequence")?
        .query_map([], |r| r.get::<_, String>(0))?
        .map(|r| Ok(serde_json::from_str(&r?)?))
        .collect::<Result<_>>()?;
    let host_after = NativeHostProfile::fixture_effect_counts();
    let host_effects = [
        host_after.0 - host_before.0,
        host_after.1 - host_before.1,
        host_after.2 - host_before.2,
    ];
    let mut host_evidence = serde_json::Value::Null;
    if let Some(selected) = host_case {
        let expected = match selected {
            "unused-success" => 4,
            "used-success" => 6,
            "used-capture-receipt-failure" => 3,
            "used-remove-intent-failure" => 4,
            _ => 5,
        };
        ensure!(
            host_events.len() == expected && host_hits.get() == u32::from(!host_success),
            "paired host profile prefix/fault count differs"
        );
        let deleted = host_success || selected == "used-remove-receipt-failure";
        ensure!(
            host_effects == [1, usize::from(deleted), usize::from(deleted)],
            "paired host profile native effects repeated or out of order"
        );
        let account_use_json: String = fixture.db.connection()?.query_row(
            "SELECT event_json FROM candidate_paired_profile_events WHERE sequence=4",
            [],
            |r| r.get(0),
        )?;
        let account_use: serde_json::Value = serde_json::from_str(&account_use_json)?;
        ensure!(
            host_events[1]["folder_id"] != account_use["folder"],
            "host and account profiles alias original storage"
        );
        if let Some(path) = host_path.borrow().as_ref() {
            ensure!(
                path.exists() != deleted,
                "host profile data deletion outcome differs"
            );
        }
        host_evidence = serde_json::json!({"case":selected,"events":host_events,"phases":*host_phases.borrow(),"sdk_effects":host_effects,"folder_differs_from_account":true,"account_folder":account_use["folder"],"host_data_intact_after_account_removal":true,"host_data_written":selected!="unused-success","host_registry_written":selected!="unused-success","host_registry_intact_after_account_removal":selected!="unused-success","original_host_profile_deleted":deleted,"cold_cleanup_authorized":false});
    } else {
        ensure!(
            host_events.is_empty() && host_effects == [0, 0, 0],
            "unexpected host profile effects"
        );
    }
    let station_events: Vec<serde_json::Value> = fixture
        .db
        .connection()?
        .prepare("SELECT event_json FROM candidate_paired_station ORDER BY sequence")?
        .query_map([], |r| r.get::<_, String>(0))?
        .map(|r| Ok(serde_json::from_str(&r?)?))
        .collect::<Result<_>>()?;
    if let Some(before) = &station_before {
        ensure!(
            station_events.len() == if success { 4 } else { 2 },
            "station/profile prefix differs"
        );
        let expected = if success {
            before.clone()
        } else {
            crate::account_state::execution::resources::station::proposed(
                before,
                &fixture.active.record.identity.package_sid,
            )?
        };
        ensure!(
            crate::account_state::execution::resources::station::Station::fixture_snapshot_current(
            )? == expected,
            "station/profile descriptor differs"
        );
    } else {
        ensure!(station_events.is_empty(), "unexpected station grant");
    }
    let desktop_events: Vec<serde_json::Value> = fixture
        .db
        .connection()?
        .prepare("SELECT event_json FROM candidate_paired_desktop ORDER BY sequence")?
        .query_map([], |r| r.get::<_, String>(0))?
        .map(|r| Ok(serde_json::from_str(&r?)?))
        .collect::<Result<_>>()?;
    if with_desktop {
        ensure!(
            desktop_events.len() == if success { 4 } else { 2 },
            "desktop/profile prefix differs"
        );
        paired_plan::native::namespace::station::desktop::fixture_require_absent(&plan)?;
    } else {
        ensure!(
            desktop_events.is_empty(),
            "unexpected paired desktop effects"
        );
    }
    let initializer_events: Vec<serde_json::Value> = fixture
        .db
        .connection()?
        .prepare("SELECT event_json FROM candidate_paired_initializer ORDER BY sequence")?
        .query_map([], |r| r.get::<_, String>(0))?
        .map(|r| Ok(serde_json::from_str(&r?)?))
        .collect::<Result<_>>()?;
    ensure!(
        initializer_events.len() == if with_initializer { 7 } else { 0 },
        "profile initializer completion differs"
    );
    if with_initializer {
        for index in [3, 4] {
            let policy: crate::account_state::execution::resources::namespace_native::rpc::AccessPolicy =
                serde_json::from_value(initializer_events[index]["policy"].clone())?;
            policy.validate(&typed_plan)?;
        }
    }
    let final_plan: String = fixture.db.connection()?.query_row(
        "SELECT plan_json FROM candidate_paired_bootstrap_plan",
        [],
        |r| r.get(0),
    )?;
    ensure!(
        raw_plan == final_plan,
        "lifecycle plan changed during retirement"
    );
    Ok(
        serde_json::json!({"expected_rpc_policy":expected_policy,"expected_namespace_birth":expected_birth,"plan":typed_plan,"case":host_case.unwrap_or(case),"passed":true,"elapsed_ms":started.elapsed().as_millis(),"namespace_rows":events,"namespace_phases":*namespace_phases.borrow(),"profile_rows":profile_rows,"resume_rows":resume_rows,"role_rows":[fixture.rows(Role::Owner),fixture.rows(Role::Consumers)],"original_account_exit_codes":account_codes,"original_family_exit_codes":family_codes,"writer_report":report,"writer_progress":progress,"original_bootstraps_exited":2,"original_jobs_absent":true,"original_namespace_absent":true,"pending_marker_unchanged":true,"untrusted_command_qualified":false,"host_profile":host_evidence,"station_events":station_events,"station_restored":with_station && success,"desktop_events":desktop_events,"desktop_closed":with_desktop && success,"original_desktop_absent":with_desktop,"initializer_events":initializer_events,"initializer_retained_through_account_profile":with_initializer,"initializer_released_before_host_capture":with_initializer}),
    )
}
