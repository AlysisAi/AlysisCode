//! Actual native namespace plus dedicated-account job composition, admitted
//! only by the enclosing disposable-account test. No new machine entry point.
use super::*;
use bootstrap_plan::{
    lifecycle::resume::logon::delivery::birth::{
        BIRTH_NATIVE_CHECKPOINT, retirement::RETIREMENT_CHECKPOINT,
    },
    namespace::{NAMESPACE_CHECKPOINT, fixture_require_absent},
};
use std::{cell::RefCell, rc::Rc};
use windows::Win32::{
    Foundation::{DUPLICATE_SAME_ACCESS, DuplicateHandle, HANDLE},
    System::Threading::GetCurrentProcess,
};

pub(super) fn run(principals: &Principals, password: &Password, case: &str) -> Result<()> {
    struct Reset;
    impl Drop for Reset {
        fn drop(&mut self) {
            NAMESPACE_CHECKPOINT.with(|s| {
                s.borrow_mut().take();
            });
            RETIREMENT_CHECKPOINT.with(|s| {
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
    let marker = std::fs::read(fixture.path())?;
    let record = active.record()?.clone();
    let image = PinnedRunnerImage::prepare(&ready)?;
    let (server, address) =
        ControlServer::prepare(&ready.binding(AccountMode::Offline)?.account_sid)?;
    let plan = bootstrap_plan::CommittedPlan::prepare(&db, &active, &image, &ready, &address)?;
    let job = plan.create_job()?;
    job.record_birth()?;
    let extra = Rc::new(RefCell::new(None));
    let held = extra.clone();
    let delayed_join = Rc::new(RefCell::new(None));
    let release_thread = delayed_join.clone();
    let selected = case.to_owned();
    let _reset = Reset;
    let exit = Rc::new(RefCell::new(None));
    let observed = exit.clone();
    let failure = case.to_owned();
    BIRTH_NATIVE_CHECKPOINT.with(|s| {
        *s.borrow_mut() = Some(Box::new(move |phase, child, sql| {
            if phase == "observed" {
                ensure!(
                    observed.borrow().is_none(),
                    "original account child observed twice"
                );
                *observed.borrow_mut() = Some(child);
                if failure == "namespace-account-birth-refusal" {
                    anyhow::bail!("synthetic account birth refusal");
                }
            }
            if phase == "acknowledged" && failure == "namespace-job-retirement-write-failure" {
                sql.execute_batch("PRAGMA query_only=ON")?;
            }
            Ok(())
        }));
    });
    NAMESPACE_CHECKPOINT.with(|s| {
        *s.borrow_mut() = Some(Box::new(move |phase, sql, root| {
            if phase == "born" && selected == "namespace-success" {
                use crate::account_state::execution::resources::namespace_native as native;
                struct Policy<'a>(&'a ExecutionRecord, u32);
                impl native::NamespacePolicySource for Policy<'_> {
                    fn execution(&self) -> &ExecutionRecord {
                        self.0
                    }
                    fn session(&self) -> u32 {
                        self.1
                    }
                }
                let mut session = 0;
                unsafe {
                    windows::Win32::System::RemoteDesktop::ProcessIdToSessionId(
                        std::process::id(),
                        &mut session,
                    )
                }?;
                let policy = Policy(&record, session);
                let root = root.context("original namespace absent during inventory check")?;
                ensure!(
                    native::inventory::observe_children(root)?.is_empty(),
                    "fresh namespace inventory is not empty"
                );
                let child = native::child_fixture(root, &policy)?;
                ensure!(
                    native::inventory::observe_children(root)?
                        == vec![native::inventory::DirectoryEntry {
                            name: "unexpected-fixture-child".into(),
                            kind: "Directory".into()
                        }],
                    "bounded namespace inventory differs from fresh native child"
                );
                drop(child);
                ensure!(
                    native::inventory::observe_children(root)?.is_empty(),
                    "closed fixture child remains in inventory"
                );
            }
            if phase == "born" && selected == "namespace-birth-receipt-write-failure" {
                sql.execute_batch("PRAGMA query_only=ON")?;
            }
            if phase == "ready" {
                ensure!(
                    atomic_job_counts(&record).is_some(),
                    "namespace born without retained original job"
                );
                if matches!(
                    selected.as_str(),
                    "namespace-held-root" | "namespace-delayed-root-release"
                ) {
                    let mut copy = HANDLE::default();
                    unsafe {
                        DuplicateHandle(
                            GetCurrentProcess(),
                            root.context("missing root")?,
                            GetCurrentProcess(),
                            &mut copy,
                            0,
                            false,
                            DUPLICATE_SAME_ACCESS,
                        )?;
                    }
                    *held.borrow_mut() = Some(crate::util::OwnedHandle(copy));
                }
            }
            if phase == "retiring" || phase == "closed" {
                ensure!(
                    atomic_job_counts(&record).is_none(),
                    "namespace retirement preceded original job exit/closure"
                );
                let rows: usize = sql.query_row(
                    "SELECT count(*) FROM candidate_atomic_job_retirement",
                    [],
                    |r| r.get(0),
                )?;
                ensure!(
                    rows == 2,
                    "namespace retirement preceded exact job completion"
                );
            }
            if phase == "retiring" && selected == "namespace-delayed-root-release" {
                let raw = held
                    .borrow_mut()
                    .take()
                    .context("delayed original root missing")?
                    .into_raw()
                    .0 as usize;
                // Only this fixture's original duplicate crosses threads. A
                // failed thread spawn returns ownership here for closure.
                let spawned = std::thread::Builder::new()
                    .name("namespace-fixture-release".into())
                    .spawn(move || {
                        let original = crate::util::OwnedHandle(HANDLE(raw as *mut _));
                        std::thread::sleep(Duration::from_millis(150));
                        unsafe { windows::Win32::Foundation::CloseHandle(original.into_raw()) }
                            .is_ok()
                    });
                match spawned {
                    Ok(thread) => *release_thread.borrow_mut() = Some(thread),
                    Err(error) => {
                        drop(crate::util::OwnedHandle(HANDLE(raw as *mut _)));
                        return Err(error).context("spawn bounded original namespace release");
                    }
                }
            }
            if phase == "closed" && selected == "namespace-removal-receipt-write-failure" {
                sql.execute_batch("PRAGMA query_only=ON")?;
            }
            Ok(())
        }));
    });
    if case == "namespace-removal-intent-write-failure" {
        RETIREMENT_CHECKPOINT.with(|s| {
            *s.borrow_mut() = Some(Box::new(|sequence, sql| {
                if sequence == 1 {
                    sql.execute_batch("PRAGMA query_only=ON").unwrap();
                }
            }));
        });
    }
    if case == "namespace-intent-write-failure" {
        db.connection()?.execute_batch("PRAGMA query_only=ON")?;
    }
    let result = job.with_empty_namespace().and_then(|bundle| {
        bundle.fixture_observe_account_birth(server, &password.0, Duration::from_secs(10))
    });
    drop(_reset);
    if let Some(thread) = delayed_join.borrow_mut().take() {
        ensure!(
            thread
                .join()
                .map_err(|_| anyhow::anyhow!("delayed release thread panicked"))?,
            "delayed original namespace close failed"
        );
    }
    ensure!(
        result.is_ok() == matches!(case, "namespace-success" | "namespace-delayed-root-release"),
        "unexpected namespace composition outcome"
    );
    if !matches!(
        case,
        "namespace-intent-write-failure" | "namespace-birth-receipt-write-failure"
    ) {
        exit.borrow_mut()
            .take()
            .context("original account child was not observed")?
            .wait(Duration::from_secs(5))?;
    } else {
        ensure!(
            exit.borrow().is_none(),
            "account child was born before namespace receipt"
        );
    }
    ensure!(
        atomic_job_counts(active.record()?).is_none(),
        "namespace path retained original job"
    );
    ensure!(
        std::fs::read(fixture.path())? == marker,
        "namespace path changed pending marker"
    );
    let sql = db.connection()?;
    sql.execute_batch("PRAGMA query_only=OFF")?;
    let rows: usize =
        sql.query_row("SELECT count(*) FROM candidate_atomic_namespace", [], |r| {
            r.get(0)
        })?;
    let expected = match case {
        "namespace-success" | "namespace-delayed-root-release" => 4,
        "namespace-intent-write-failure" => 0,
        "namespace-birth-receipt-write-failure" => 1,
        "namespace-removal-intent-write-failure"
        | "namespace-account-birth-refusal"
        | "namespace-job-retirement-write-failure" => 2,
        "namespace-removal-receipt-write-failure" | "namespace-held-root" => 3,
        _ => anyhow::bail!("unknown namespace case"),
    };
    ensure!(rows == expected, "namespace durable prefix differs");
    if case == "namespace-held-root" {
        ensure!(
            fixture_require_absent(&plan).is_err(),
            "extra original reference was not retained"
        );
    }
    drop(extra.borrow_mut().take());
    fixture_require_absent(&plan)?;
    Ok(())
}
