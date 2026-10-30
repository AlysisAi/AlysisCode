//! Real WinSta0 DACL effects ONLY on an explicitly opted-in disposable VM.
//! No local execution, account/profile creation, desktop/process launch or TLS.
use super::super::tests::identity;
use super::*;
use std::path::PathBuf;

const SENTINEL: &str = "github-hosted-station-grant-only";
const TEST: &str = "candidate_station_disposable_host_lifecycle";
fn admission(values: &[Option<String>], run: &str, attempt: &str) -> bool {
    let expected = [
        SENTINEL,
        "true",
        "github-hosted",
        "Windows",
        "AlysisAi/alysis-code-internal",
        "workflow_dispatch",
    ];
    values.len() == expected.len()
        && values
            .iter()
            .zip(expected)
            .all(|(a, b)| a.as_deref() == Some(b))
        && [run, attempt]
            .into_iter()
            .all(|s| !s.is_empty() && s.bytes().all(|b| b.is_ascii_digit()))
        && cfg!(all(windows, target_arch = "x86_64"))
}
#[test]
fn station_disposable_admission_requires_every_explicit_gate() {
    let good = [
        SENTINEL,
        "true",
        "github-hosted",
        "Windows",
        "AlysisAi/alysis-code-internal",
        "workflow_dispatch",
    ]
    .map(|s| Some(s.into()));
    assert!(admission(&good, "123", "1"));
    for at in 0..good.len() {
        let mut bad = good.clone();
        bad[at] = None;
        assert!(!admission(&bad, "123", "1"));
        bad[at] = Some("wrong".into());
        assert!(!admission(&bad, "123", "1"));
    }
    assert!(!admission(&good, "", "1"));
    assert!(!admission(&good, "123", "wrong"));
}
struct Case {
    id: Identity,
    db: Connection,
    path: PathBuf,
}
impl Case {
    fn new() -> Self {
        let id = identity();
        let path = crate::util::test_temp_dir().join(format!(
            "alysis-station-{}.db",
            super::super::super::super::random_id().unwrap()
        ));
        std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&path)
            .unwrap();
        let mut db = Connection::open(&path).unwrap();
        db.execute_batch("PRAGMA journal_mode=DELETE; PRAGMA synchronous=FULL;")
            .unwrap();
        crate::account_state::initialize_header(&mut db, &id.execution.identity.binding).unwrap();
        ResourceJournal::open(&db, id.clone(), Authority::FixtureLive, true).unwrap();
        Self { id, db, path }
    }
    fn live(&self) -> ResourceJournal<'_> {
        ResourceJournal::open(&self.db, self.id.clone(), Authority::FixtureLive, false).unwrap()
    }
    fn stopped(&self) -> ResourceJournal<'_> {
        ResourceJournal::open(&self.db, self.id.clone(), Authority::FixtureStopped, false).unwrap()
    }
    fn reopen(&mut self) {
        let replacement = Connection::open(&self.path).unwrap();
        replacement
            .execute_batch("PRAGMA journal_mode=DELETE; PRAGMA synchronous=FULL;")
            .unwrap();
        let old = std::mem::replace(&mut self.db, replacement);
        old.close().unwrap();
    }
}
fn reject(db: &Connection) {
    db.execute_batch("CREATE TRIGGER fail_station BEFORE INSERT ON candidate_resource_events BEGIN SELECT RAISE(ABORT,'injected'); END;").unwrap();
}
fn accept(db: &Connection) {
    db.execute_batch("DROP TRIGGER fail_station").unwrap();
}
fn finished(case: &Case, guard: &mut PreparedWindowStationGrant) {
    case.stopped().restore_window_station_grant(guard).unwrap();
    assert_eq!(guard.station.read().unwrap(), guard.operation.before);
    case.stopped().recovered().unwrap();
    assert_eq!(case.live().replay().unwrap().events, 4);
}
fn native_roundtrip() {
    let mut case = Case::new();
    let mut guard = case.live().prepare_window_station_grant().unwrap();
    let before = guard.station.read().unwrap();
    assert!(
        case.stopped()
            .apply_window_station_grant(&mut guard)
            .is_err()
    );
    assert_eq!(guard.station.read().unwrap(), before);
    case.live().apply_window_station_grant(&mut guard).unwrap();
    assert_eq!(guard.station.read().unwrap(), guard.operation.after);
    assert!(
        case.live()
            .restore_window_station_grant(&mut guard)
            .is_err()
    );
    case.live().record_window_station_grant(&mut guard).unwrap();
    assert!(case.live().apply_window_station_grant(&mut guard).is_err());
    case.reopen(); // Original station handle retained across SQLite reopen.
    case.live().record_window_station_grant(&mut guard).unwrap();
    finished(&case, &mut guard);
    finished(&case, &mut guard);
    let borrowed = unsafe { GetProcessWindowStation() }.unwrap();
    drop(guard);
    assert_eq!(Snapshot::read(HANDLE(borrowed.0)).unwrap(), before);
}
fn native_committed_failures() {
    for point in [
        Fault::IntentCommitted,
        Fault::CreatedCommitted,
        Fault::RemoveCommitted,
        Fault::RemovedCommitted,
    ] {
        let case = Case::new();
        let mut guard = case.live().prepare_window_station_grant().unwrap();
        guard.operation.fault = Some(point);
        if point == Fault::IntentCommitted {
            assert!(case.live().apply_window_station_grant(&mut guard).is_err());
            assert_eq!(guard.station.read().unwrap(), guard.operation.before);
        }
        case.live().apply_window_station_grant(&mut guard).unwrap();
        if point == Fault::CreatedCommitted {
            assert!(case.live().record_window_station_grant(&mut guard).is_err());
        }
        case.live().record_window_station_grant(&mut guard).unwrap();
        if matches!(point, Fault::RemoveCommitted | Fault::RemovedCommitted) {
            assert!(
                case.stopped()
                    .restore_window_station_grant(&mut guard)
                    .is_err()
            );
        }
        finished(&case, &mut guard);
    }
}
fn native_rejected_writes() {
    for stage in 0..4 {
        let case = Case::new();
        let mut guard = case.live().prepare_window_station_grant().unwrap();
        if stage == 0 {
            reject(&case.db);
            assert!(case.live().apply_window_station_grant(&mut guard).is_err());
            assert_eq!(guard.station.read().unwrap(), guard.operation.before);
            accept(&case.db);
        }
        case.live().apply_window_station_grant(&mut guard).unwrap();
        if stage == 1 {
            reject(&case.db);
            assert!(case.live().record_window_station_grant(&mut guard).is_err());
            assert_eq!(guard.station.read().unwrap(), guard.operation.after);
            accept(&case.db);
        }
        case.live().record_window_station_grant(&mut guard).unwrap();
        if stage == 2 {
            reject(&case.db);
            assert!(
                case.stopped()
                    .restore_window_station_grant(&mut guard)
                    .is_err()
            );
            assert_eq!(guard.station.read().unwrap(), guard.operation.after);
            accept(&case.db);
        }
        if stage == 3 {
            guard.operation.fault = Some(Fault::RemoveCommitted);
            assert!(
                case.stopped()
                    .restore_window_station_grant(&mut guard)
                    .is_err()
            );
            reject(&case.db);
            assert!(
                case.stopped()
                    .restore_window_station_grant(&mut guard)
                    .is_err()
            );
            assert_eq!(guard.station.read().unwrap(), guard.operation.before);
            assert_eq!(guard.operation.phase, Phase::Restored);
            accept(&case.db);
        }
        finished(&case, &mut guard);
    }
}
fn native_original_guard_only() {
    let case = Case::new();
    let mut guard = case.live().prepare_window_station_grant().unwrap();
    case.live().apply_window_station_grant(&mut guard).unwrap();
    case.live().record_window_station_grant(&mut guard).unwrap();
    let mut other_case = Case::new();
    assert!(
        other_case
            .live()
            .record_window_station_grant(&mut guard)
            .is_err()
    );
    assert!(
        other_case
            .stopped()
            .restore_window_station_grant(&mut guard)
            .is_err()
    );
    assert_eq!(guard.station.read().unwrap(), guard.operation.after);
    other_case.reopen(); // Reopening a database never authorizes another execution.
    assert!(
        other_case
            .stopped()
            .restore_window_station_grant(&mut guard)
            .is_err()
    );
    finished(&case, &mut guard);
}

#[test]
#[ignore = "real shared WinSta0 DACL changes on explicit disposable hosted VM only; NEVER run locally"]
fn candidate_station_disposable_host_lifecycle() {
    let names = [
        "ALYSIS_RUN_DISPOSABLE_STATION",
        "GITHUB_ACTIONS",
        "RUNNER_ENVIRONMENT",
        "RUNNER_OS",
        "GITHUB_REPOSITORY",
        "GITHUB_EVENT_NAME",
    ];
    let values = names.map(|n| std::env::var(n).ok());
    let run = std::env::var("GITHUB_RUN_ID").unwrap_or_default();
    let attempt = std::env::var("GITHUB_RUN_ATTEMPT").unwrap_or_default();
    assert!(
        admission(&values, &run, &attempt),
        "refusing non-disposable/non-opted-in station effects"
    );
    let evidence =
        PathBuf::from(std::env::var("RUNNER_TEMP").unwrap()).join("alysis-station-resources.json");
    let mut report = serde_json::json!({"test":TEST,"run_id":run,"run_attempt":attempt,"passed":false,
        "completed_cases":[],"no_accounts_profiles_or_processes":true,"no_schannel_or_activation_claim":true});
    let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        for (name, test) in [
            (
                "native_exact_roundtrip_and_file_reopen",
                native_roundtrip as fn(),
            ),
            (
                "native_committed_receipt_failures",
                native_committed_failures as fn(),
            ),
            (
                "native_rejected_database_writes",
                native_rejected_writes as fn(),
            ),
            (
                "native_original_guard_and_execution_only",
                native_original_guard_only as fn(),
            ),
        ] {
            // Read the borrowed current-thread desktop before/after each case.
            // This observes one existing desktop, not all descendants.
            let desktop = unsafe {
                windows::Win32::System::StationsAndDesktops::GetThreadDesktop(
                    windows::Win32::System::Threading::GetCurrentThreadId(),
                )
            }
            .unwrap();
            let desktop_before = Snapshot::read(HANDLE(desktop.0)).unwrap();
            test();
            assert_eq!(
                Snapshot::read(HANDLE(desktop.0)).unwrap(),
                desktop_before,
                "station write propagated to the existing broker desktop"
            );
            report["completed_cases"]
                .as_array_mut()
                .unwrap()
                .push(name.into());
            std::fs::write(&evidence, serde_json::to_vec_pretty(&report).unwrap()).unwrap();
        }
    }));
    report["passed"] = result.is_ok().into();
    std::fs::write(&evidence, serde_json::to_vec_pretty(&report).unwrap()).unwrap();
    if let Err(panic) = result {
        std::panic::resume_unwind(panic);
    }
}
