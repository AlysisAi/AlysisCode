//! Disposable hosted fixture only: fixed suspended initializer, original host
//! bootstrap roles, and the complete original resource bundle. No user command.
use super::*;
use crate::account_state::execution::resources::{
    NativeHostProfile,
    station::{Station, proposed},
};
use paired_plan::native::namespace::{
    fixture_require_absent as root_absent,
    station::{
        desktop::fixture_require_absent as desktop_absent,
        initializer::{INITIALIZER_CHECKPOINT, INITIALIZER_EXIT, INITIALIZER_PROCESS},
    },
};
pub(super) const CASES: [&str; 10] = [
    "success",
    "intent",
    "born",
    "stopped",
    "rpc-intent",
    "rpc-granted",
    "ready",
    "releasing",
    "closed",
    "completed",
];
struct Reset;
impl Drop for Reset {
    fn drop(&mut self) {
        INITIALIZER_CHECKPOINT.with(|s| {
            s.borrow_mut().take();
        });
        INITIALIZER_PROCESS.with(|s| {
            s.borrow_mut().take();
        });
        INITIALIZER_EXIT.with(|s| {
            s.borrow_mut().take();
        });
        NATIVE_CHECKPOINT.with(|s| {
            s.borrow_mut().take();
        });
    }
}
pub(super) fn hosted(fixture: Fixture, case: &str) -> Result<serde_json::Value> {
    ensure!(CASES.contains(&case), "unknown paired initializer case");
    let mut fixture = PairFixture::from_fixture_image(fixture, true);
    let owner = fixture.owner_server.take().unwrap();
    let consumer = fixture.consumer_server.take().unwrap();
    let marker = std::fs::read(fixture._fixture.path())?;
    let plan = fixture.plan();
    let before = Station::fixture_snapshot_current()?;
    let granted = proposed(&before, &fixture.active.record.identity.package_sid)?;
    let profile_before = NativeHostProfile::fixture_effect_counts();
    let processes = Rc::new(RefCell::new(Vec::new()));
    let refs = processes.clone();
    let initializers = Rc::new(RefCell::new(Vec::<OwnedHandle>::new()));
    let unwind_reference = initializers.clone();
    let unwind_exits = Rc::new(Cell::new(0));
    let unwind_count = unwind_exits.clone();
    let born = initializers.clone();
    let stopped = initializers.clone();
    let births = Rc::new(Cell::new(0));
    let birth_count = births.clone();
    let exits = Rc::new(Cell::new(0));
    let exit_count = exits.clone();
    let phases = Rc::new(RefCell::new(Vec::new()));
    let observed = phases.clone();
    let hits = Rc::new(Cell::new(0));
    let hit = hits.clone();
    let cut = case.to_owned();
    let owner_name = fixture.name(Role::Owner);
    let consumer_name = fixture.name(Role::Consumers);
    let _reset = Reset;
    INITIALIZER_EXIT.with(|s| {
        *s.borrow_mut() = Some(Box::new(move |original| {
            let refs = unwind_reference.borrow();
            assert_eq!(refs.len(), 1);
            assert!(
                unsafe {
                    windows::Win32::Foundation::CompareObjectHandles(original, refs[0].raw())
                }
                .as_bool()
            );
            assert_eq!(
                unsafe { WaitForSingleObject(refs[0].raw(), 0) },
                WAIT_OBJECT_0,
                "initializer resources released before original process signaled"
            );
            unwind_count.set(unwind_count.get() + 1);
        }));
    });
    NATIVE_CHECKPOINT.with(|s| {
        *s.borrow_mut() = Some(Box::new(move |phase, _, _, raw| {
            if phase == "bootstrap-born" {
                refs.borrow_mut().push(retain_original(raw.unwrap()));
            }
        }));
    });
    INITIALIZER_PROCESS.with(|s| {
        *s.borrow_mut() = Some(Box::new(move |raw| {
            ensure!(birth_count.get() == 0, "initializer repeated birth");
            born.borrow_mut().push(retain_original(raw));
            birth_count.set(1);
            Ok(())
        }));
    });
    INITIALIZER_CHECKPOINT.with(|s| {
        *s.borrow_mut() = Some(Box::new(move |phase, sql| {
            observed.borrow_mut().push(phase.to_owned());
            for (table, expected) in [
                ("candidate_paired_namespace", 2),
                ("candidate_paired_host_profile", 3),
                ("candidate_paired_station", 2),
                ("candidate_paired_desktop", 2),
            ] {
                let n: usize =
                    sql.query_row(&format!("SELECT count(*) FROM {table}"), [], |r| r.get(0))?;
                ensure!(
                    n == expected,
                    "outer resource retired before initializer at {phase}"
                );
            }
            if phase == "stopped" {
                // Release query-only observers before normal token/root retirement.
                for r in stopped.borrow_mut().drain(..) {
                    dead(&r);
                    exit_count.set(exit_count.get() + 1);
                }
            }
            if ["releasing", "closed", "completed"].contains(&phase) {
                ensure!(
                    counts(&owner_name).is_some_and(|n| n.0 == 0)
                        && counts(&consumer_name).is_some_and(|n| n.0 == 0),
                    "initializer release preceded both jobs"
                );
            }
            if phase == cut {
                hit.set(hit.get() + 1);
                if ["intent", "ready", "releasing", "completed"].contains(&phase) {
                    anyhow::bail!("injected paired initializer boundary failure");
                }
                sql.execute_batch("PRAGMA query_only=ON")?;
            }
            Ok(())
        }));
    });
    let result = plan
        .create_jobs()?
        .with_empty_namespace()?
        .with_host_profile_for_use()?
        .with_station()?
        .with_unused_desktop()?
        .fixture_run_initializer(owner, consumer);
    INITIALIZER_CHECKPOINT.with(|s| {
        s.borrow_mut().take();
    });
    INITIALIZER_PROCESS.with(|s| {
        s.borrow_mut().take();
    });
    INITIALIZER_EXIT.with(|s| {
        s.borrow_mut().take();
    });
    NATIVE_CHECKPOINT.with(|s| {
        s.borrow_mut().take();
    });
    fixture
        .db
        .connection()?
        .execute_batch("PRAGMA query_only=OFF")?;
    if case == "success" {
        result?.verify()?;
    } else {
        let error = result.err().context("initializer fault passed")?;
        if ["intent", "ready", "releasing", "completed"].contains(&case) {
            ensure!(
                error
                    .chain()
                    .any(|e| e.to_string() == "injected paired initializer boundary failure"),
                "wrong initializer refusal: {error:#}"
            );
        } else {
            require_readonly(&error);
        }
    }
    ensure!(
        processes.borrow().len() == 2,
        "initializer host bootstrap observations differ"
    );
    for r in processes.borrow_mut().drain(..) {
        dead(&r);
    }
    for r in initializers.borrow_mut().drain(..) {
        dead(&r);
        exits.set(exits.get() + 1);
    }
    ensure!(
        births.get() == u32::from(case != "intent")
            && exits.get() == births.get()
            && hits.get() == u32::from(case != "success"),
        "initializer birth/exit/fault evidence differs"
    );
    ensure!(
        unwind_exits.get() == u32::from(case == "born"),
        "initializer original error-exit proof differs"
    );
    let events: Vec<serde_json::Value> = fixture
        .db
        .connection()?
        .prepare("SELECT event_json FROM candidate_paired_initializer ORDER BY sequence")?
        .query_map([], |r| r.get::<_, String>(0))?
        .map(|r| Ok(serde_json::from_str(&r?)?))
        .collect::<Result<_>>()?;
    let expected = match case {
        "intent" | "born" => 1,
        "stopped" => 2,
        "rpc-intent" => 3,
        "rpc-granted" => 4,
        "ready" => 5,
        "releasing" | "closed" => 6,
        _ => 7,
    };
    ensure!(
        events.len() == expected,
        "initializer durable prefix differs"
    );
    let roles_completed = ["success", "releasing", "closed", "completed"].contains(&case);
    let role_rows = [fixture.rows(Role::Owner), fixture.rows(Role::Consumers)];
    ensure!(
        role_rows == if roles_completed { [9, 9] } else { [7, 7] },
        "initializer role prefix differs"
    );
    let mut outer_rows = Vec::new();
    for (table, expected) in [
        (
            "candidate_paired_host_profile",
            if case == "success" { 6 } else { 3 },
        ),
        (
            "candidate_paired_namespace",
            if case == "success" { 4 } else { 2 },
        ),
        (
            "candidate_paired_desktop",
            if case == "success" { 4 } else { 2 },
        ),
        (
            "candidate_paired_station",
            if case == "success" { 4 } else { 2 },
        ),
    ] {
        let n: usize = fixture.db.connection()?.query_row(
            &format!("SELECT count(*) FROM {table}"),
            [],
            |r| r.get(0),
        )?;
        ensure!(
            n == expected,
            "initializer outer durable prefix differs: {table}"
        );
        outer_rows.push(n);
    }
    ensure!(
        counts(&fixture.name(Role::Owner)).is_none()
            && counts(&fixture.name(Role::Consumers)).is_none(),
        "initializer original jobs survived closure"
    );
    root_absent(&plan)?;
    desktop_absent(&plan)?;
    ensure!(
        Station::fixture_snapshot_current()? == if case == "success" { before } else { granted },
        "initializer changed station on failed lifetime"
    );
    let profile_after = NativeHostProfile::fixture_effect_counts();
    let effects = [
        profile_after.0 - profile_before.0,
        profile_after.1 - profile_before.1,
        profile_after.2 - profile_before.2,
    ];
    ensure!(
        effects
            == if case == "success" {
                [1, 1, 1]
            } else {
                [1, 0, 0]
            },
        "initializer profile effects differ"
    );
    ensure!(
        std::fs::read(fixture._fixture.path())? == marker,
        "initializer pending marker changed"
    );
    Ok(
        serde_json::json!({"case":case,"passed":true,"events":events,"phases":*phases.borrow(),
        "role_rows":role_rows,"outer_rows":outer_rows,"host_profile_effects":effects,
        "original_bootstraps_exited":2,"original_initializers_exited":exits.get(),"unwind_original_exit_checks":unwind_exits.get(),
        "original_jobs_absent":true,"original_namespace_absent":true,"original_desktop_absent":true,
        "station_restored":case=="success","pending_marker_unchanged":true,
        "cold_cleanup_authorized":false,"untrusted_command_qualified":false}),
    )
}
