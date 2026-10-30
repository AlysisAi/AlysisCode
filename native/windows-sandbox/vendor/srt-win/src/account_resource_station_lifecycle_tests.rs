//! Synthetic descriptor effects only. No native station setter is reachable.
use super::super::tests::{db, identity, snapshot};
use super::*;
use std::cell::{Cell, RefCell};

#[derive(Clone, Copy, Default)]
enum WriteFault {
    #[default]
    None,
    Before,
    After,
    Readback,
    WrongReadback,
    Identity,
}
struct Model<'a> {
    db: &'a Connection,
    current: RefCell<Snapshot>,
    writes: usize,
    fault: WriteFault,
    unreadable: Cell<bool>,
    valid: Cell<bool>,
}
impl StationAccess for Model<'_> {
    fn verify(&self) -> Result<()> {
        ensure!(self.valid.get(), "model identity changed");
        Ok(())
    }
    fn read(&self) -> Result<Snapshot> {
        ensure!(!self.unreadable.get(), "model readback unavailable");
        Ok(self.current.borrow().clone())
    }
    fn write(&mut self, wanted: &Snapshot) -> Result<()> {
        // Every simulated native call requires an already committed event, not
        // a pending SQLite transaction or an arbitrary callback success.
        assert!(self.db.is_autocommit());
        let json: String = self
            .db
            .query_row(
                "SELECT event_json FROM candidate_resource_events ORDER BY sequence DESC LIMIT 1",
                [],
                |r| r.get(0),
            )
            .unwrap();
        match serde_json::from_str::<Event>(&json).unwrap() {
            Event::CreateIntent {
                resource: ROLE,
                acl: Some(acl),
                ..
            } => assert_eq!(
                serde_json::from_slice::<Snapshot>(&acl.after).unwrap(),
                *wanted
            ),
            Event::RemoveIntent { resource: ROLE, .. } => {}
            other => panic!("write without committed station intent: {other:?}"),
        }
        self.writes += 1;
        if matches!(self.fault, WriteFault::Before) {
            anyhow::bail!("setter failed before effect");
        }
        *self.current.borrow_mut() = wanted.clone();
        match self.fault {
            WriteFault::After => anyhow::bail!("setter failed after effect"),
            WriteFault::Readback => self.unreadable.set(true),
            WriteFault::WrongReadback => self.current.borrow_mut().owner = "S-1-5-32-544".into(),
            WriteFault::Identity => self.valid.set(false),
            _ => {}
        }
        Ok(())
    }
}
struct Case {
    id: Identity,
    db: Connection,
}
impl Case {
    fn new() -> Self {
        let id = identity();
        let db = db(&id);
        ResourceJournal::open(&db, id.clone(), Authority::FixtureLive, true).unwrap();
        Self { id, db }
    }
    fn live(&self) -> ResourceJournal<'_> {
        ResourceJournal::open(&self.db, self.id.clone(), Authority::FixtureLive, false).unwrap()
    }
    fn stopped(&self) -> ResourceJournal<'_> {
        ResourceJournal::open(&self.db, self.id.clone(), Authority::FixtureStopped, false).unwrap()
    }
    fn model(&self) -> (Operation, Model<'_>) {
        let before = snapshot(
            "O:SYD:PAI(D;;0x40000;;;WD)(A;;0x0;;;BA)(A;CIIO;0x2037f;;;SY)(A;ID;0x20000;;;WD)",
        );
        let after = proposed(&before, &self.id.execution.identity.package_sid).unwrap();
        (
            Operation::new(self.id.clone(), before.clone(), after),
            Model {
                db: &self.db,
                current: RefCell::new(before),
                writes: 0,
                fault: WriteFault::None,
                unreadable: Cell::new(false),
                valid: Cell::new(true),
            },
        )
    }
}
fn reject(db: &Connection) {
    db.execute_batch("CREATE TRIGGER fail_station BEFORE INSERT ON candidate_resource_events BEGIN SELECT RAISE(ABORT,'injected'); END;").unwrap();
}
fn accept(db: &Connection) {
    db.execute_batch("DROP TRIGGER fail_station").unwrap();
}

#[test]
fn station_lifecycle_exact_roundtrip_and_idempotent_receipts() {
    let case = Case::new();
    let live = case.live();
    let (mut operation, mut station) = case.model();
    operation.apply(&live, &mut station).unwrap();
    assert_eq!(station.read().unwrap(), operation.after);
    assert_eq!(live.replay().unwrap().events, 1);
    assert!(case.stopped().recovered().is_err());
    operation.record(&live, &station).unwrap();
    operation.record(&live, &station).unwrap();
    assert!(operation.apply(&live, &mut station).is_err());
    assert!(operation.restore(&live, &mut station).is_err());
    assert_eq!(station.writes, 1);
    operation.restore(&case.stopped(), &mut station).unwrap();
    operation.restore(&case.stopped(), &mut station).unwrap();
    assert_eq!(station.read().unwrap(), operation.before);
    assert_eq!(station.writes, 2);
    assert_eq!(live.replay().unwrap().events, 4);
    case.stopped().recovered().unwrap();
}

#[test]
fn station_lifecycle_each_committed_transition_retries_only_acknowledgment() {
    for point in [
        Fault::IntentCommitted,
        Fault::CreatedCommitted,
        Fault::RemoveCommitted,
        Fault::RemovedCommitted,
    ] {
        let case = Case::new();
        let live = case.live();
        let (mut op, mut station) = case.model();
        op.fault = Some(point);
        if point == Fault::IntentCommitted {
            assert!(op.apply(&live, &mut station).is_err());
            assert_eq!(station.writes, 0);
            assert_eq!(live.replay().unwrap().events, 1);
        }
        op.apply(&live, &mut station).unwrap();
        if point == Fault::CreatedCommitted {
            assert!(op.record(&live, &station).is_err());
            assert_eq!(station.writes, 1);
        }
        op.record(&live, &station).unwrap();
        if matches!(point, Fault::RemoveCommitted | Fault::RemovedCommitted) {
            assert!(op.restore(&case.stopped(), &mut station).is_err());
            assert_eq!(
                station.writes,
                if point == Fault::RemoveCommitted {
                    1
                } else {
                    2
                }
            );
        }
        op.restore(&case.stopped(), &mut station).unwrap();
        assert_eq!(station.writes, 2);
        assert_eq!(live.replay().unwrap().events, 4);
        assert_eq!(station.read().unwrap(), op.before);
    }
}

#[test]
fn station_lifecycle_rejected_journal_writes_preserve_handle_and_exact_phase() {
    for stage in 0..4 {
        let case = Case::new();
        let live = case.live();
        let (mut op, mut station) = case.model();
        if stage == 0 {
            reject(&case.db);
            assert!(op.apply(&live, &mut station).is_err());
            assert_eq!(station.writes, 0);
            accept(&case.db);
        }
        op.apply(&live, &mut station).unwrap();
        if stage == 1 {
            reject(&case.db);
            assert!(op.record(&live, &station).is_err());
            assert_eq!(station.writes, 1);
            accept(&case.db);
        }
        op.record(&live, &station).unwrap();
        if stage == 2 {
            reject(&case.db);
            assert!(op.restore(&case.stopped(), &mut station).is_err());
            assert_eq!(station.writes, 1);
            accept(&case.db);
        }
        if stage == 3 {
            op.fault = Some(Fault::RemoveCommitted);
            assert!(op.restore(&case.stopped(), &mut station).is_err());
            reject(&case.db);
            assert!(op.restore(&case.stopped(), &mut station).is_err());
            assert_eq!(op.phase, Phase::Restored);
            assert_eq!(station.writes, 2);
            accept(&case.db);
        }
        op.restore(&case.stopped(), &mut station).unwrap();
        assert_eq!(station.writes, 2);
        case.stopped().recovered().unwrap();
    }
}

#[test]
fn station_lifecycle_uncertain_apply_never_retries_adopts_or_restores() {
    for fault in [
        WriteFault::Before,
        WriteFault::After,
        WriteFault::Readback,
        WriteFault::WrongReadback,
        WriteFault::Identity,
    ] {
        let case = Case::new();
        let live = case.live();
        let (mut op, mut station) = case.model();
        station.fault = fault;
        assert!(op.apply(&live, &mut station).is_err());
        assert_eq!(op.phase, Phase::ApplyUncertain);
        station.valid.set(true);
        station.unreadable.set(false);
        // Even a now-exact after snapshot is not ownership/success evidence.
        *station.current.borrow_mut() = op.after.clone();
        assert!(op.apply(&live, &mut station).is_err());
        assert!(op.record(&live, &station).is_err());
        assert!(op.restore(&case.stopped(), &mut station).is_err());
        assert_eq!(station.writes, 1);
        assert_eq!(live.replay().unwrap().events, 1);
        assert!(case.stopped().recovered().is_err());
    }
}

#[test]
fn station_lifecycle_uncertain_restore_never_retries_or_guesses_completion() {
    for fault in [
        WriteFault::Before,
        WriteFault::After,
        WriteFault::Readback,
        WriteFault::WrongReadback,
        WriteFault::Identity,
    ] {
        let case = Case::new();
        let live = case.live();
        let (mut op, mut station) = case.model();
        op.apply(&live, &mut station).unwrap();
        op.record(&live, &station).unwrap();
        station.fault = fault;
        assert!(op.restore(&case.stopped(), &mut station).is_err());
        assert_eq!(op.phase, Phase::RestoreUncertain);
        station.valid.set(true);
        station.unreadable.set(false);
        *station.current.borrow_mut() = op.before.clone();
        assert!(op.restore(&case.stopped(), &mut station).is_err());
        assert_eq!(station.writes, 2);
        assert_eq!(live.replay().unwrap().events, 3);
        assert!(case.stopped().recovered().is_err());
    }
}

#[test]
fn station_lifecycle_observed_drift_is_preserved_at_each_effect_boundary() {
    for stage in 0..5 {
        let case = Case::new();
        let live = case.live();
        let (mut op, mut station) = case.model();
        if stage > 1 {
            op.apply(&live, &mut station).unwrap();
        }
        if stage > 2 {
            op.record(&live, &station).unwrap();
        }
        if stage == 1 {
            op.fault = Some(Fault::IntentCommitted);
            assert!(op.apply(&live, &mut station).is_err());
        }
        if stage == 4 {
            op.fault = Some(Fault::RemoveCommitted);
            assert!(op.restore(&case.stopped(), &mut station).is_err());
        }
        station
            .current
            .borrow_mut()
            .aces
            .push(snapshot("O:SYD:(A;;0x1;;;WD)").aces[0].clone());
        let changed = station.read().unwrap();
        let writes = station.writes;
        let result = match stage {
            0 | 1 => op.apply(&live, &mut station),
            2 => op.record(&live, &station),
            _ => op.restore(&case.stopped(), &mut station),
        };
        assert!(result.is_err());
        assert_eq!(station.writes, writes);
        assert_eq!(station.read().unwrap(), changed);
    }
}

#[test]
fn station_lifecycle_foreign_nonce_snapshot_or_operation_never_adopts_history() {
    for defect in ["nonce", "snapshot", "guard"] {
        let case = Case::new();
        let live = case.live();
        let (mut op, mut station) = case.model();
        op.apply(&live, &mut station).unwrap();
        match defect {
            "nonce" => op.create_nonce = Some(super::super::super::super::random_id().unwrap()),
            "snapshot" => {
                op.before.aces.clear();
                op.after =
                    proposed(&op.before, &op.identity.execution.identity.package_sid).unwrap();
            }
            _ => {
                let (new, _) = case.model();
                op = new;
            }
        }
        assert!(op.record(&live, &station).is_err());
        assert!(op.restore(&case.stopped(), &mut station).is_err());
        assert_eq!(station.writes, 1);
        assert_eq!(live.replay().unwrap().events, 1);
    }
}

#[test]
fn station_lifecycle_actor_authority_and_other_resource_refusals_precede_effect() {
    for defect in ["pid", "time", "session", "owner", "stopped", "other"] {
        let case = Case::new();
        let mut journal = case.live();
        let (mut op, mut station) = case.model();
        match defect {
            "pid" => op.identity.execution.pid += 100000,
            "time" => op.identity.execution.created += 1,
            "session" => op.identity.session_id += 1,
            "owner" => op.identity.execution.owner = "S-1-5-18".into(),
            "stopped" => journal = case.stopped(),
            _ => {
                journal.create_intent(Resource::HostProfile, None).unwrap();
            }
        }
        assert!(op.apply(&journal, &mut station).is_err());
        assert_eq!(station.writes, 0);
    }
}

#[test]
fn station_lifecycle_standalone_grant_never_waives_profile_or_namespace_for_bootstrap() {
    let case = Case::new();
    let live = case.live();
    let (mut op, mut station) = case.model();
    op.apply(&live, &mut station).unwrap();
    op.record(&live, &station).unwrap();
    assert!(
        live.create_intent(Resource::BootstrapDesktop, None)
            .is_err()
    );
    // Synthetic prerequisite receipt tests the transition model only; there is
    // no native namespace/profile adapter call or fabricated production receipt.
    let nonce = live.create_intent(Resource::NamespaceRoot, None).unwrap();
    live.created(
        Resource::NamespaceRoot,
        nonce,
        Observation::EmptyNamespaceRoot { policy_version: 2 },
    )
    .unwrap();
    assert!(
        live.create_intent(Resource::BootstrapDesktop, None)
            .is_err(),
        "host profile remains required"
    );
    assert!(
        op.restore(&case.stopped(), &mut station).is_err(),
        "standalone adapter cannot clean other effects"
    );
    assert_eq!(station.writes, 1);
}

#[test]
fn station_lifecycle_lost_guard_drop_preserves_pending_history_and_descriptor() {
    let case = Case::new();
    let live = case.live();
    let (mut op, mut station) = case.model();
    op.apply(&live, &mut station).unwrap();
    op.record(&live, &station).unwrap();
    let after = station.read().unwrap();
    drop(op);
    let (mut replacement, _) = case.model();
    assert!(replacement.restore(&case.stopped(), &mut station).is_err());
    assert_eq!(station.read().unwrap(), after);
    assert_eq!(station.writes, 1);
    assert_eq!(live.replay().unwrap().events, 2);
    assert!(case.stopped().recovered().is_err());
}
