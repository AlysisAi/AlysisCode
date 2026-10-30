//! Shared WinSta0 effects require an extra explicit disposable-host sentinel.
//! Refused post-grant writes deliberately retain the unresolved unique package
//! ACE and journal on that VM; the test never hides failure with guessed repair.
use super::*;
use crate::account_state::execution::resources::station::Station;
use bootstrap_plan::lifecycle::resume::logon::delivery::birth::BIRTH_NATIVE_CHECKPOINT;
use bootstrap_plan::{
    namespace::{NAMESPACE_CHECKPOINT, fixture_require_absent},
    station::STATION_CHECKPOINT,
};
use std::{cell::RefCell, rc::Rc};

pub(super) fn require_station_host() -> Result<()> {
    require_host()?;
    ensure!(
        std::env::var("ALYSIS_RUN_DISPOSABLE_ATOMIC_STATION")
            .ok()
            .as_deref()
            == Some("github-hosted-atomic-station-only"),
        "atomic station effect admission refused"
    );
    Ok(())
}

pub(super) fn run(principals: &Principals, password: &Password, case: &str) -> Result<()> {
    require_station_host()?;
    struct Reset;
    impl Drop for Reset {
        fn drop(&mut self) {
            STATION_CHECKPOINT.with(|s| {
                s.borrow_mut().take();
            });
            NAMESPACE_CHECKPOINT.with(|s| {
                s.borrow_mut().take();
            });
            BIRTH_NATIVE_CHECKPOINT.with(|s| {
                s.borrow_mut().take();
            });
        }
    }
    let fixture = bound_fixture(
        principals.offline.as_deref().unwrap(),
        principals.online.as_deref().unwrap(),
        principals.group.as_deref().unwrap(),
    )?;
    let db = protected_database(&fixture);
    let ready = ReadyInstallation::fixture(fixture.setup.clone(), &fixture.root)?;
    let active = atomic_fixture_active(&fixture, &db);
    let record = active.record()?.clone();
    let marker = std::fs::read(fixture.path())?;
    let image = PinnedRunnerImage::prepare(&ready)?;
    let (server, address) =
        ControlServer::prepare(&ready.binding(AccountMode::Offline)?.account_sid)?;
    let plan = bootstrap_plan::CommittedPlan::prepare(&db, &active, &image, &ready, &address)?;
    let job = plan.create_job()?.with_empty_namespace()?;
    let (probe, before, after) = Station::prepare(&record.identity.package_sid)?;
    let exit = Rc::new(RefCell::new(None));
    let captured = exit.clone();
    let selected = case.to_owned();
    let _reset = Reset;
    STATION_CHECKPOINT.with(|s| {
        *s.borrow_mut() = Some(Box::new(move |phase, sql| {
            if matches!(phase, "restoring" | "restored") {
                ensure!(
                    atomic_job_counts(&record).is_none(),
                    "station restoration preceded original job closure"
                );
                let n: usize =
                    sql.query_row("SELECT count(*) FROM candidate_atomic_namespace", [], |r| {
                        r.get(0)
                    })?;
                ensure!(n == 4, "station restoration preceded namespace retirement");
            }
            if phase == "applied" && selected == "station-applied-receipt-write-failure"
                || phase == "restored" && selected == "station-restored-receipt-write-failure"
            {
                sql.execute_batch("PRAGMA query_only=ON")?;
            }
            Ok(())
        }));
    });
    BIRTH_NATIVE_CHECKPOINT.with(|s| {
        *s.borrow_mut() = Some(Box::new(move |phase, observer, _sql| {
            if phase == "observed" {
                ensure!(captured.borrow().is_none(), "account child observed twice");
                *captured.borrow_mut() = Some(observer);
            }
            Ok(())
        }));
    });
    if case == "station-restoration-intent-write-failure" {
        // Refuse the next write only after the namespace completion receipt
        // has committed and passed readback; preserve the exact schema.
        NAMESPACE_CHECKPOINT.with(|s| {
            *s.borrow_mut() = Some(Box::new(|phase, sql, _| {
                if phase == "completed" {
                    sql.execute_batch("PRAGMA query_only=ON")?;
                }
                Ok(())
            }));
        });
    }
    if case == "station-intent-write-failure" {
        db.connection()?.execute_batch("PRAGMA query_only=ON")?;
    }
    let result = job.with_station().and_then(|job| {
        job.fixture_observe_account_birth(server, &password.0, Duration::from_secs(10))
    });
    drop(_reset);
    ensure!(
        result.is_ok() == (case == "station-success"),
        "unexpected atomic station outcome"
    );
    ensure!(
        atomic_job_counts(active.record()?).is_none(),
        "station path retained original job"
    );
    if matches!(
        case,
        "station-success"
            | "station-restoration-intent-write-failure"
            | "station-restored-receipt-write-failure"
    ) {
        exit.borrow_mut()
            .take()
            .context("original account child was not observed")?
            .wait(Duration::from_secs(5))?;
    } else {
        ensure!(
            exit.borrow().is_none(),
            "account birth preceded station receipt"
        );
    }
    ensure!(
        std::fs::read(fixture.path())? == marker,
        "station path changed pending marker"
    );
    let sql = db.connection()?;
    sql.execute_batch("PRAGMA query_only=OFF")?;
    let rows: usize = sql.query_row("SELECT count(*) FROM candidate_atomic_station", [], |r| {
        r.get(0)
    })?;
    let expected = match case {
        "station-success" => 4,
        "station-intent-write-failure" => 0,
        "station-applied-receipt-write-failure" => 1,
        "station-restoration-intent-write-failure" => 2,
        "station-restored-receipt-write-failure" => 3,
        _ => anyhow::bail!("unknown station case"),
    };
    ensure!(rows == expected, "atomic station durable prefix differs");
    if case == "station-restoration-intent-write-failure" {
        let namespace_rows: usize =
            sql.query_row("SELECT count(*) FROM candidate_atomic_namespace", [], |r| {
                r.get(0)
            })?;
        ensure!(
            namespace_rows == 4,
            "station fault occurred before namespace completion"
        );
    }
    probe.verify_current()?;
    let restored = matches!(
        case,
        "station-success"
            | "station-intent-write-failure"
            | "station-restored-receipt-write-failure"
    );
    ensure!(
        probe.read()? == if restored { before } else { after },
        "station descriptor differs from exact expected outcome"
    );
    fixture_require_absent(&plan)?;
    Ok(())
}
