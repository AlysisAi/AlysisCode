//! The desktop is unused; this tests composition/order around an actual
//! suspended account child, not attachment, command resume or Schannel.
use super::*;
use crate::account_state::execution::resources::station::Station;
use bootstrap_plan::lifecycle::resume::logon::delivery::birth::BIRTH_NATIVE_CHECKPOINT;
use bootstrap_plan::{desktop::DESKTOP_CHECKPOINT, namespace::NAMESPACE_CHECKPOINT};
use std::{cell::RefCell, rc::Rc};
use windows::Win32::Foundation::ERROR_FILE_NOT_FOUND;
use windows::Win32::System::StationsAndDesktops::*;

struct Extra(HDESK);
impl Drop for Extra {
    fn drop(&mut self) {
        let _ = unsafe { CloseDesktop(self.0) };
    }
}

pub(super) fn run(principals: &Principals, password: &Password, case: &str) -> Result<()> {
    super::station::require_station_host()?;
    struct Reset;
    impl Drop for Reset {
        fn drop(&mut self) {
            DESKTOP_CHECKPOINT.with(|s| {
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
    let plan_json: String = db.connection()?.query_row(
        "SELECT plan_json FROM candidate_atomic_bootstrap_plan",
        [],
        |r| r.get(0),
    )?;
    let plan_value: serde_json::Value = serde_json::from_str(&plan_json)?;
    let name = format!(
        "alysis-atomic-desktop-{}",
        plan_value["operation"]
            .as_str()
            .context("desktop plan operation absent")?
    );
    let before = Station::fixture_snapshot_current()?;
    let after = crate::account_state::execution::resources::station::proposed(
        &before,
        &record.identity.package_sid,
    )?;
    let job = plan.create_job()?.with_empty_namespace()?.with_station()?;
    let exit = Rc::new(RefCell::new(None));
    let captured = exit.clone();
    let held = Rc::new(RefCell::new(None));
    let captured_held = held.clone();
    let selected = case.to_owned();
    let held_name = name.clone();
    let _reset = Reset;
    DESKTOP_CHECKPOINT.with(|s| {
        *s.borrow_mut() = Some(Box::new(move |phase, sql| {
            if matches!(phase, "closing" | "closed" | "completed") {
                ensure!(
                    atomic_job_counts(&record).is_none(),
                    "desktop closure preceded whole-job exit"
                );
                let namespace: usize =
                    sql.query_row("SELECT count(*) FROM candidate_atomic_namespace", [], |r| {
                        r.get(0)
                    })?;
                let station: usize =
                    sql.query_row("SELECT count(*) FROM candidate_atomic_station", [], |r| {
                        r.get(0)
                    })?;
                ensure!(
                    namespace == 4 && station == 2,
                    "desktop closure order differs from namespace/station lifecycle"
                );
            }
            if phase == "born" && selected == "desktop-birth-receipt-write-failure"
                || phase == "closed" && selected == "desktop-close-receipt-write-failure"
            {
                sql.execute_batch("PRAGMA query_only=ON")?;
            }
            if phase == "ready" && selected == "desktop-held-reference" {
                let extra = unsafe {
                    OpenDesktopW(
                        pcwstr(&wstr(&held_name)),
                        DESKTOP_CONTROL_FLAGS(0),
                        false,
                        0x20081,
                    )
                }?;
                ensure!(
                    captured_held.borrow().is_none(),
                    "desktop extra reference captured twice"
                );
                *captured_held.borrow_mut() = Some(Extra(extra));
            }
            Ok(())
        }));
    });
    BIRTH_NATIVE_CHECKPOINT.with(|s| {
        *s.borrow_mut() = Some(Box::new(move |phase, observer, _| {
            if phase == "observed" {
                ensure!(captured.borrow().is_none(), "account observed twice");
                *captured.borrow_mut() = Some(observer);
            }
            Ok(())
        }));
    });
    if case == "desktop-close-intent-write-failure" {
        NAMESPACE_CHECKPOINT.with(|s| {
            *s.borrow_mut() = Some(Box::new(|phase, sql, _| {
                if phase == "completed" {
                    sql.execute_batch("PRAGMA query_only=ON")?;
                }
                Ok(())
            }));
        });
    }
    if case == "desktop-intent-write-failure" {
        db.connection()?.execute_batch("PRAGMA query_only=ON")?;
    }
    let result = job.with_unused_desktop().and_then(|job| {
        job.fixture_observe_account_birth(server, &password.0, Duration::from_secs(10))
    });
    drop(_reset);
    ensure!(
        result.is_ok() == (case == "desktop-success"),
        "unexpected desktop composition outcome"
    );
    ensure!(
        atomic_job_counts(active.record()?).is_none(),
        "desktop path retained original job"
    );
    if matches!(
        case,
        "desktop-success"
            | "desktop-close-intent-write-failure"
            | "desktop-close-receipt-write-failure"
            | "desktop-held-reference"
    ) {
        exit.borrow_mut()
            .take()
            .context("original child unobserved")?
            .wait(Duration::from_secs(5))?;
    } else {
        ensure!(
            exit.borrow().is_none(),
            "child birth preceded desktop receipt"
        );
    }
    let sql = db.connection()?;
    sql.execute_batch("PRAGMA query_only=OFF")?;
    let rows: usize = sql.query_row("SELECT count(*) FROM candidate_atomic_desktop", [], |r| {
        r.get(0)
    })?;
    let expected = match case {
        "desktop-success" => 4,
        "desktop-intent-write-failure" => 0,
        "desktop-birth-receipt-write-failure" => 1,
        "desktop-close-intent-write-failure" => 2,
        "desktop-close-receipt-write-failure" | "desktop-held-reference" => 3,
        _ => anyhow::bail!("unknown desktop case"),
    };
    ensure!(rows == expected, "desktop durable prefix differs");
    if case == "desktop-held-reference" {
        ensure!(held.borrow().is_some(), "desktop extra reference absent");
        ensure!(
            format!("{:#}", result.err().context("desktop refusal missing")?)
                .contains("remains referenced"),
            "desktop refusal had another cause"
        );
    }
    drop(held.borrow_mut().take());
    match unsafe {
        OpenDesktopW(
            pcwstr(&wstr(&name)),
            DESKTOP_CONTROL_FLAGS(0),
            false,
            0x20081,
        )
    } {
        Ok(h) => {
            unsafe { CloseDesktop(h) }?;
            anyhow::bail!("desktop survived original and extra reference closure")
        }
        Err(e) => ensure!(
            e.code() == windows::core::HRESULT::from_win32(ERROR_FILE_NOT_FOUND.0),
            "desktop absence uncertain"
        ),
    }
    bootstrap_plan::namespace::fixture_require_absent(&plan)?;
    ensure!(
        Station::fixture_snapshot_current()?
            == if case == "desktop-success" {
                before
            } else {
                after
            },
        "desktop path station descriptor differs"
    );
    ensure!(
        std::fs::read(fixture.path())? == marker,
        "desktop path changed pending marker"
    );
    Ok(())
}
