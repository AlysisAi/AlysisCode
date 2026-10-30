//! Shared station and paired desktop effects are disposable-hosted only.
use super::*;
use crate::account_state::execution::resources::station::{Station, proposed};
use paired_plan::native::namespace::{
    fixture_require_absent as root_absent,
    station::{
        STATION_CHECKPOINT,
        desktop::{DESKTOP_CHECKPOINT, fixture_foreign, fixture_require_absent as desktop_absent},
    },
};
struct Reset;
impl Drop for Reset {
    fn drop(&mut self) {
        DESKTOP_CHECKPOINT.with(|s| {
            s.borrow_mut().take();
        });
        STATION_CHECKPOINT.with(|s| {
            s.borrow_mut().take();
        });
        NATIVE_CHECKPOINT.with(|s| {
            s.borrow_mut().take();
        });
    }
}
pub(super) fn hosted(fixture: Fixture, case: &str) -> Result<serde_json::Value> {
    ensure!(
        [
            "success",
            "prepared",
            "born",
            "ready",
            "before-close",
            "closing",
            "closed",
            "held-reference",
            "collision"
        ]
        .contains(&case),
        "unknown paired desktop case"
    );
    let mut fixture = PairFixture::from_fixture_image(fixture, true);
    let owner = fixture.owner_server.take().unwrap();
    let consumer = fixture.consumer_server.take().unwrap();
    let marker = std::fs::read(fixture._fixture.path())?;
    let plan = fixture.plan();
    let before = Station::fixture_snapshot_current()?;
    let granted = proposed(&before, &fixture.active.record.identity.package_sid)?;
    let desktop_phases = Rc::new(RefCell::new(Vec::new()));
    let phases = desktop_phases.clone();
    let station_phases = Rc::new(RefCell::new(Vec::new()));
    let station_observed = station_phases.clone();
    let held = Rc::new(RefCell::new(None));
    let retained_desktop = held.clone();
    let hits = Rc::new(Cell::new(0));
    let hit = hits.clone();
    let cut = case.to_owned();
    let owner_name = fixture.name(Role::Owner);
    let consumer_name = fixture.name(Role::Consumers);
    let _reset = Reset;
    DESKTOP_CHECKPOINT.with(|s| {
        *s.borrow_mut() = Some(Box::new(move |phase, sql, native| {
            phases.borrow_mut().push(phase.to_owned());
            let rows: usize =
                sql.query_row("SELECT count(*) FROM candidate_paired_station", [], |r| {
                    r.get(0)
                })?;
            ensure!(rows == 2, "desktop changed after station restoration began");
            if ["before-close", "closing", "closed", "completed"].contains(&phase) {
                ensure!(
                    counts(&owner_name).is_some_and(|n| n.0 == 0)
                        && counts(&consumer_name).is_some_and(|n| n.0 == 0),
                    "desktop close preceded both original jobs"
                );
                let roots: usize =
                    sql.query_row("SELECT count(*) FROM candidate_paired_namespace", [], |r| {
                        r.get(0)
                    })?;
                ensure!(roots == 4, "desktop close preceded root completion");
            }
            if cut == "held-reference" && phase == "ready" {
                hit.set(hit.get() + 1);
                *retained_desktop.borrow_mut() = Some(
                    native
                        .context("original desktop missing")?
                        .fixture_original_observer()?,
                );
            } else if phase == cut {
                hit.set(hit.get() + 1);
                if ["ready", "closing"].contains(&phase) {
                    anyhow::bail!("injected paired desktop boundary failure");
                }
                sql.execute_batch("PRAGMA query_only=ON")?;
            }
            Ok(())
        }));
    });
    STATION_CHECKPOINT.with(|s| {
        *s.borrow_mut() = Some(Box::new(move |phase, sql, _| {
            station_observed.borrow_mut().push(phase.to_owned());
            if ["before-restore", "restoring", "restored", "completed"].contains(&phase) {
                let rows: usize =
                    sql.query_row("SELECT count(*) FROM candidate_paired_desktop", [], |r| {
                        r.get(0)
                    })?;
                ensure!(rows == 4, "station restore preceded desktop Closed receipt");
            }
            Ok(())
        }));
    });
    let processes = Rc::new(RefCell::new(Vec::new()));
    let refs = processes.clone();
    NATIVE_CHECKPOINT.with(|s| {
        *s.borrow_mut() = Some(Box::new(move |phase, _, _, raw| {
            if phase == "bootstrap-born" {
                refs.borrow_mut().push(retain_original(raw.unwrap()));
            }
        }));
    });
    let pair = plan.create_jobs()?.with_empty_namespace()?.with_station()?;
    let mut foreign = if case == "collision" {
        Some(fixture_foreign(&plan)?)
    } else {
        None
    };
    let result = pair
        .with_unused_desktop()
        .and_then(|p| p.fixture_run_host_family(owner, consumer));
    DESKTOP_CHECKPOINT.with(|s| {
        s.borrow_mut().take();
    });
    STATION_CHECKPOINT.with(|s| {
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
        let e = result.err().context("desktop fault passed")?;
        match case {
            "held-reference" => ensure!(
                e.chain()
                    .any(|e| e.to_string()
                        == "desktop remains referenced; preserve unresolved state"),
                "wrong held-desktop refusal: {e:#}"
            ),
            "collision" => ensure!(
                e.chain()
                    .filter_map(|e| e.downcast_ref::<windows::core::Error>())
                    .any(|e| e.code()
                        == windows::core::HRESULT::from_win32(
                            windows::Win32::Foundation::ERROR_ALREADY_EXISTS.0
                        )),
                "wrong desktop collision refusal: {e:#}"
            ),
            "ready" | "closing" => ensure!(
                e.chain()
                    .any(|e| e.to_string() == "injected paired desktop boundary failure"),
                "wrong injected desktop refusal: {e:#}"
            ),
            _ => require_readonly(&e),
        }
    }
    ensure!(
        hits.get() == u32::from(!["success", "collision"].contains(&case)),
        "desktop fault count differs"
    );
    let started = [
        "success",
        "before-close",
        "closing",
        "closed",
        "held-reference",
    ]
    .contains(&case);
    ensure!(
        processes.borrow().len() == if started { 2 } else { 0 },
        "desktop original process refs differ"
    );
    for r in processes.borrow_mut().drain(..) {
        dead(&r);
    }
    let events = |table: &str| -> Result<Vec<serde_json::Value>> {
        let query = match table {
            "desktop" => "SELECT event_json FROM candidate_paired_desktop ORDER BY sequence",
            _ => "SELECT event_json FROM candidate_paired_station ORDER BY sequence",
        };
        fixture
            .db
            .connection()?
            .prepare(query)?
            .query_map([], |r| r.get::<_, String>(0))?
            .map(|r| Ok(serde_json::from_str(&r?)?))
            .collect()
    };
    let desktop_events = events("desktop")?;
    let station_events = events("station")?;
    let expected = match case {
        "success" => 4,
        "prepared" => 0,
        "born" | "collision" => 1,
        "ready" | "before-close" => 2,
        _ => 3,
    };
    ensure!(
        desktop_events.len() == expected
            && station_events.len() == if case == "success" { 4 } else { 2 },
        "desktop/station durable prefix differs"
    );
    let roots: usize = fixture.db.connection()?.query_row(
        "SELECT count(*) FROM candidate_paired_namespace",
        [],
        |r| r.get(0),
    )?;
    ensure!(
        roots == if started { 4 } else { 2 }
            && fixture.rows(Role::Owner) == if started { 9 } else { 2 }
            && fixture.rows(Role::Consumers) == if started { 9 } else { 2 },
        "desktop original pair prefix differs"
    );
    ensure!(
        counts(&fixture.name(Role::Owner)).is_none()
            && counts(&fixture.name(Role::Consumers)).is_none(),
        "desktop jobs survived closure"
    );
    root_absent(&plan)?;
    if let Some(observer) = held.borrow_mut().take() {
        ensure!(
            desktop_absent(&plan).is_err(),
            "held original desktop disappeared"
        );
        observer.verify()?;
        observer.close()?;
    }
    if let Some(original) = foreign.as_mut() {
        original.verify()?;
        original.close()?;
    }
    desktop_absent(&plan)?;
    ensure!(
        Station::fixture_snapshot_current()? == if case == "success" { before } else { granted },
        "desktop failure restored or changed station"
    );
    ensure!(
        std::fs::read(fixture._fixture.path())? == marker,
        "desktop marker changed"
    );
    Ok(
        serde_json::json!({"case":case,"passed":true,"desktop_events":desktop_events,"station_events":station_events,"desktop_phases":*desktop_phases.borrow(),"station_phases":*station_phases.borrow(),"namespace_rows":roots,"role_rows":[fixture.rows(Role::Owner),fixture.rows(Role::Consumers)],"original_bootstraps_exited":if started{2}else{0},"original_jobs_absent":true,"original_namespace_absent":true,"original_desktop_absent_after_observer_release":true,"station_restored":case=="success","late_absence_authorized_restoration":false,"pending_marker_unchanged":true,"untrusted_command_qualified":false}),
    )
}
