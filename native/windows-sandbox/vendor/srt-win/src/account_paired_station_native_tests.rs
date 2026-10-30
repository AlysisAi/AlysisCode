//! Disposable-hosted station effects only; not a locally runnable native test.
use super::*;
use crate::account_state::execution::resources::station::{Station, proposed};
use paired_plan::native::namespace::{fixture_require_absent, station::STATION_CHECKPOINT};
struct Reset;
impl Drop for Reset {
    fn drop(&mut self) {
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
            "applied",
            "before-restore",
            "restoring",
            "restored",
            "drift-ready",
            "drift-before-restore"
        ]
        .contains(&case),
        "unknown paired station fixture"
    );
    let mut fixture = PairFixture::from_fixture_image(fixture, true);
    let owner = fixture.owner_server.take().unwrap();
    let consumer = fixture.consumer_server.take().unwrap();
    let marker = std::fs::read(fixture._fixture.path())?;
    let plan = fixture.plan();
    let before = Station::fixture_snapshot_current()?;
    let after = proposed(&before, &fixture.active.record.identity.package_sid)?;
    let expected = if case.starts_with("drift-") {
        // Fresh unrelated package ACE models another host editor. It must not
        // be lost when restoration observes a changed descriptor.
        proposed(
            &after,
            if case == "drift-ready" {
                "S-1-15-2-987654321-123456789-987654321-123456789-987654321-123456789-1"
            } else {
                "S-1-15-2-987654321-123456789-987654321-123456789-987654321-123456789-2"
            },
        )?
    } else if ["success", "prepared", "restored"].contains(&case) {
        before.clone()
    } else {
        after.clone()
    };
    let cut = case.to_owned();
    let observed = Rc::new(RefCell::new(Vec::new()));
    let phases = observed.clone();
    let hits = Rc::new(Cell::new(0));
    let hit = hits.clone();
    let desired = expected.clone();
    let owner_name = fixture.name(Role::Owner);
    let consumer_name = fixture.name(Role::Consumers);
    let _reset = Reset;
    STATION_CHECKPOINT.with(|s| {
        *s.borrow_mut() = Some(Box::new(move |phase, sql, native| {
            phases.borrow_mut().push(phase.to_owned());
            if ["before-restore", "restoring", "restored", "completed"].contains(&phase) {
                ensure!(
                    counts(&owner_name).is_some_and(|n| n.0 == 0)
                        && counts(&consumer_name).is_some_and(|n| n.0 == 0),
                    "station restoration preceded pair drain"
                );
                let roots: usize =
                    sql.query_row("SELECT count(*) FROM candidate_paired_namespace", [], |r| {
                        r.get(0)
                    })?;
                ensure!(
                    roots == 4,
                    "station restoration preceded original namespace completion"
                );
            }
            if (cut == "drift-ready" && phase == "ready")
                || (cut == "drift-before-restore" && phase == "before-restore")
            {
                hit.set(hit.get() + 1);
                let current = native.read()?;
                native.write_exact(&current, &desired)?;
            } else if phase == cut {
                hit.set(hit.get() + 1);
                if phase == "restoring" {
                    anyhow::bail!("injected paired station pre-restore failure");
                }
                sql.execute_batch("PRAGMA query_only=ON")?;
            }
            Ok(())
        }));
    });
    let retained = Rc::new(RefCell::new(Vec::new()));
    let refs = retained.clone();
    NATIVE_CHECKPOINT.with(|s| {
        *s.borrow_mut() = Some(Box::new(move |phase, _, _, raw| {
            if phase == "bootstrap-born" {
                refs.borrow_mut().push(retain_original(raw.unwrap()));
            }
        }));
    });
    let result = plan
        .create_jobs()?
        .with_empty_namespace()?
        .with_station()
        .and_then(|pair| pair.fixture_run_host_family(owner, consumer));
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
        let e = result.err().context("station fault passed")?;
        if case.starts_with("drift-") {
            ensure!(
                e.chain().any(|cause| cause.to_string()
                    == "paired station descriptor changed; do not overwrite"),
                "descriptor drift failed for another reason: {e:#}"
            );
        } else if case == "restoring" {
            ensure!(e.chain().any(|cause| cause.to_string() == "injected paired station pre-restore failure"), "pre-restore fault failed for another reason: {e:#}");
        }
        if !case.starts_with("drift-") && case != "restoring" {
            require_readonly(&e);
        }
    }
    ensure!(
        hits.get() == u32::from(case != "success"),
        "station fault count differs"
    );
    let started = !["prepared", "applied", "drift-ready"].contains(&case);
    ensure!(
        retained.borrow().len() == if started { 2 } else { 0 },
        "station original process refs differ"
    );
    for original in retained.borrow_mut().drain(..) {
        dead(&original);
    }
    let rows: Vec<serde_json::Value> = fixture
        .db
        .connection()?
        .prepare("SELECT event_json FROM candidate_paired_station ORDER BY sequence")?
        .query_map([], |r| r.get::<_, String>(0))?
        .map(|r| Ok(serde_json::from_str(&r?)?))
        .collect::<Result<_>>()?;
    let expected_rows = match case {
        "prepared" => 0,
        "applied" => 1,
        "success" => 4,
        "restoring" | "restored" => 3,
        _ => 2,
    };
    ensure!(rows.len() == expected_rows, "station prefix differs");
    let root_rows: usize = fixture.db.connection()?.query_row(
        "SELECT count(*) FROM candidate_paired_namespace",
        [],
        |r| r.get(0),
    )?;
    ensure!(
        root_rows == if started { 4 } else { 2 },
        "station root prefix differs"
    );
    ensure!(
        fixture.rows(Role::Owner) == if started { 9 } else { 2 }
            && fixture.rows(Role::Consumers) == if started { 9 } else { 2 },
        "station job prefix differs"
    );
    fixture_require_absent(&plan)?;
    ensure!(
        counts(&fixture.name(Role::Owner)).is_none()
            && counts(&fixture.name(Role::Consumers)).is_none(),
        "station jobs survived"
    );
    ensure!(
        Station::fixture_snapshot_current()? == expected,
        "station Drop retried or overwrote preserved descriptor"
    );
    ensure!(
        std::fs::read(fixture._fixture.path())? == marker,
        "station marker changed"
    );
    Ok(
        serde_json::json!({"case":case,"passed":true,"events":rows,"phases":*observed.borrow(),"namespace_rows":root_rows,
        "role_rows":[fixture.rows(Role::Owner),fixture.rows(Role::Consumers)],"original_bootstraps_exited":if started{2}else{0},
        "original_jobs_absent":true,"original_namespace_absent":true,"pending_marker_unchanged":true,
        "before":before,"granted":after,"preserved":expected,"cold_cleanup_authorized":false,"untrusted_command_qualified":false}),
    )
}
