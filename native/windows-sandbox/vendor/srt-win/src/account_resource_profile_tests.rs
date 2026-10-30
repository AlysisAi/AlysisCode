//! SQLite fault injection and sealed synthetic effects. No local SDK profiles.
use super::*;
use crate::account_install::{AccountMode, synthetic_ready};

fn identity() -> Identity {
    let mut binding = synthetic_ready().binding(AccountMode::Offline).unwrap();
    binding.owner_sid = crate::sid::current_user_sid().unwrap();
    let candidate = crate::account_state::CandidateJournalIdentity::new(
        binding,
        format!(
            "Alysis.Execution.{}",
            super::super::super::random_id().unwrap()
        ),
    )
    .unwrap();
    let mut execution = super::super::super::fixture_record(candidate);
    execution.scope = RESOURCE_SCOPE.into();
    let mut session = 0;
    unsafe {
        windows::Win32::System::RemoteDesktop::ProcessIdToSessionId(
            std::process::id(),
            &mut session,
        )
        .unwrap();
    }
    Identity {
        execution,
        session_id: session,
    }
}
fn database(id: &Identity) -> Connection {
    let mut db = Connection::open_in_memory().unwrap();
    crate::account_state::initialize_header(&mut db, &id.execution.identity.binding).unwrap();
    db
}
fn live<'a>(db: &'a Connection, id: &Identity, create: bool) -> ResourceJournal<'a> {
    ResourceJournal::open(db, id.clone(), Authority::FixtureLive, create).unwrap()
}
fn stopped<'a>(db: &'a Connection, id: &Identity) -> ResourceJournal<'a> {
    ResourceJournal::open(db, id.clone(), Authority::FixtureStopped, false).unwrap()
}
fn reject(db: &Connection) {
    db.execute_batch("CREATE TRIGGER fail_profile BEFORE INSERT ON candidate_resource_events BEGIN SELECT RAISE(ABORT,'injected'); END;").unwrap();
}
fn accept(db: &Connection) {
    db.execute_batch("DROP TRIGGER fail_profile;").unwrap();
}

#[derive(Clone, Copy, PartialEq, Default)]
enum EffectFault {
    #[default]
    None,
    CreateBefore,
    CreateAfter,
    CloseBefore,
    CloseAfter,
    DeleteBefore,
    DeleteAfter,
}
struct Model<'a> {
    db: &'a Connection,
    creates: usize,
    closes: usize,
    deletes: usize,
    present: bool,
    open: bool,
    drifted: bool,
    inaccessible: bool,
    fault: EffectFault,
}
impl<'a> Model<'a> {
    fn new(db: &'a Connection) -> Self {
        Self {
            db,
            creates: 0,
            closes: 0,
            deletes: 0,
            present: false,
            open: false,
            drifted: false,
            inaccessible: false,
            fault: EffectFault::None,
        }
    }
    fn last_event(&self) -> Event {
        assert!(self.db.is_autocommit());
        let json: String = self
            .db
            .query_row(
                "SELECT event_json FROM candidate_resource_events ORDER BY sequence DESC LIMIT 1",
                [],
                |r| r.get(0),
            )
            .unwrap();
        serde_json::from_str(&json).unwrap()
    }
}
impl ProfileAccess for Model<'_> {
    fn create(&mut self, id: &Identity) -> Result<[u8; 24]> {
        assert!(
            matches!(self.last_event(), Event::CreateIntent { resource: ROLE, ref name, ref subject, .. }
            if name == &id.name(ROLE) && subject == id.subject(ROLE))
        );
        self.creates += 1;
        ensure!(
            self.fault != EffectFault::CreateBefore,
            "injected before create"
        );
        self.present = true;
        self.open = true;
        ensure!(
            self.fault != EffectFault::CreateAfter,
            "injected after create"
        );
        Ok([7; 24])
    }
    fn unchanged(&self) -> Result<()> {
        ensure!(
            self.present && self.open && !self.drifted && !self.inaccessible,
            "original profile drift or query failure"
        );
        Ok(())
    }
    fn close_trees(&mut self) -> Result<()> {
        assert!(matches!(
            self.last_event(),
            Event::RemoveIntent { resource: ROLE, .. }
        ));
        self.closes += 1;
        ensure!(
            self.fault != EffectFault::CloseBefore,
            "injected before close"
        );
        self.open = false;
        ensure!(
            self.fault != EffectFault::CloseAfter,
            "injected after close"
        );
        Ok(())
    }
    fn delete(&mut self, _: &Identity) -> Result<()> {
        assert!(!self.open && self.closes == 1);
        assert!(matches!(
            self.last_event(),
            Event::RemoveIntent { resource: ROLE, .. }
        ));
        self.deletes += 1;
        ensure!(
            self.fault != EffectFault::DeleteBefore,
            "injected before delete"
        );
        self.present = false;
        ensure!(
            self.fault != EffectFault::DeleteAfter,
            "injected after delete"
        );
        Ok(())
    }
    fn absent(&self) -> Result<()> {
        ensure!(
            !self.present && !self.open && !self.inaccessible && !self.drifted,
            "profile absence uncertain"
        );
        Ok(())
    }
}

#[test]
fn unused_profile_exact_lifecycle_and_receipt_retries_do_not_repeat_effects() {
    for fault in [
        None,
        Some(Fault::IntentCommitted),
        Some(Fault::CreatedCommitted),
        Some(Fault::RemoveCommitted),
        Some(Fault::RemovedCommitted),
    ] {
        let id = identity();
        let db = database(&id);
        let j = live(&db, &id, true);
        let mut op = Operation::new(id.clone());
        let mut native = Model::new(&db);
        if fault == Some(Fault::IntentCommitted) {
            op.fault = fault;
            assert!(op.birth(&j, &mut native).is_err());
            assert_eq!(native.creates, 0);
        }
        op.birth(&j, &mut native).unwrap();
        if fault == Some(Fault::CreatedCommitted) {
            op.fault = fault;
            assert!(op.record(&j, &native).is_err());
        }
        op.record(&j, &native).unwrap();
        op.record(&j, &native).unwrap();
        assert!(op.birth(&j, &mut native).is_err());
        assert!(op.retire(&j, &mut native).is_err());
        let s = stopped(&db, &id);
        if matches!(
            fault,
            Some(Fault::RemoveCommitted | Fault::RemovedCommitted)
        ) {
            op.fault = fault;
            assert!(op.retire(&s, &mut native).is_err());
        }
        op.retire(&s, &mut native).unwrap();
        op.retire(&s, &mut native).unwrap();
        assert_eq!((native.creates, native.closes, native.deletes), (1, 1, 1));
        assert_eq!(s.replay().unwrap().events, 4);
        assert!(s.recovered().unwrap().matches(&id.execution));
    }
}

#[test]
fn unused_profile_rejected_intents_and_receipts_preserve_original_proof() {
    let id = identity();
    let db = database(&id);
    let j = live(&db, &id, true);
    let mut op = Operation::new(id.clone());
    let mut native = Model::new(&db);
    reject(&db);
    assert!(op.birth(&j, &mut native).is_err());
    assert_eq!(native.creates, 0);
    let nonce = op.create_nonce.clone();
    accept(&db);
    op.birth(&j, &mut native).unwrap();
    assert_eq!(op.create_nonce, nonce);
    reject(&db);
    assert!(op.record(&j, &native).is_err());
    native.unchanged().unwrap();
    assert_eq!(j.replay().unwrap().events, 1);
    accept(&db);
    op.record(&j, &native).unwrap();
    let s = stopped(&db, &id);
    reject(&db);
    assert!(op.retire(&s, &mut native).is_err());
    assert_eq!(native.closes, 0);
    let removal = op.remove_nonce.clone();
    accept(&db);
    op.fault = Some(Fault::RemoveCommitted);
    assert!(op.retire(&s, &mut native).is_err());
    assert_eq!(op.remove_nonce, removal);
    assert_eq!(native.closes, 0);
    reject(&db);
    assert!(op.retire(&s, &mut native).is_err());
    assert_eq!((native.closes, native.deletes), (1, 1));
    assert_eq!(s.replay().unwrap().events, 3);
    assert!(s.recovered().is_err());
    accept(&db);
    op.retire(&s, &mut native).unwrap();
    assert_eq!(native.deletes, 1);
}

#[test]
fn unused_profile_uncertain_native_effects_are_never_retried_or_retired() {
    for fault in [
        EffectFault::CreateBefore,
        EffectFault::CreateAfter,
        EffectFault::CloseBefore,
        EffectFault::CloseAfter,
        EffectFault::DeleteBefore,
        EffectFault::DeleteAfter,
    ] {
        let id = identity();
        let db = database(&id);
        let j = live(&db, &id, true);
        let mut op = Operation::new(id.clone());
        let mut native = Model::new(&db);
        if matches!(fault, EffectFault::CreateBefore | EffectFault::CreateAfter) {
            native.fault = fault;
            assert!(op.birth(&j, &mut native).is_err());
            assert!(op.birth(&j, &mut native).is_err());
            assert!(op.record(&j, &native).is_err());
        } else {
            op.birth(&j, &mut native).unwrap();
            op.record(&j, &native).unwrap();
            native.fault = fault;
        }
        let s = stopped(&db, &id);
        assert!(op.retire(&s, &mut native).is_err());
        let counts = (native.creates, native.closes, native.deletes);
        native.fault = EffectFault::None;
        assert!(op.retire(&s, &mut native).is_err());
        assert!(s.recovered().is_err());
        assert_eq!((native.creates, native.closes, native.deletes), counts);
    }
}

#[test]
fn unused_profile_drift_and_replacement_never_authorize_delete_or_receipt() {
    let id = identity();
    let db = database(&id);
    let j = live(&db, &id, true);
    let mut op = Operation::new(id.clone());
    let mut native = Model::new(&db);
    op.birth(&j, &mut native).unwrap();
    native.drifted = true;
    assert!(op.record(&j, &native).is_err());
    native.drifted = false;
    op.record(&j, &native).unwrap();
    let s = stopped(&db, &id);
    native.drifted = true;
    assert!(op.retire(&s, &mut native).is_err());
    assert_eq!(native.closes, 0);
    native.drifted = false;
    op.fault = Some(Fault::RemoveCommitted);
    assert!(op.retire(&s, &mut native).is_err());
    native.inaccessible = true;
    assert!(op.retire(&s, &mut native).is_err());
    assert_eq!(native.closes, 0);
    native.inaccessible = false;
    reject(&db);
    assert!(op.retire(&s, &mut native).is_err());
    accept(&db);
    native.present = true; // replacement after successful original SDK delete
    assert!(op.retire(&s, &mut native).is_err());
    assert_eq!(native.deletes, 1);
    assert_eq!(s.replay().unwrap().events, 3);
    assert!(s.recovered().is_err());
    native.present = false;
    op.retire(&s, &mut native).unwrap();
}

#[test]
fn unused_profile_receipt_cannot_unlock_other_resources_or_be_adopted() {
    let id = identity();
    let db = database(&id);
    let j = live(&db, &id, true);
    let mut op = Operation::new(id.clone());
    let mut native = Model::new(&db);
    op.birth(&j, &mut native).unwrap();
    op.record(&j, &native).unwrap();
    for role in ALL {
        assert!(j.create_intent(role, None).is_err());
    }
    let mut lost = Operation::new(id.clone());
    assert!(lost.birth(&j, &mut Model::new(&db)).is_err());
    assert!(lost.record(&j, &native).is_err());
    assert!(lost.retire(&stopped(&db, &id), &mut native).is_err());
    let nonce = op.create_nonce.clone();
    op.create_nonce = Some(super::super::super::random_id().unwrap());
    assert!(op.record(&j, &native).is_err());
    op.create_nonce = nonce;
    let mut other = id.clone();
    other.execution.pending_id[0] ^= 1;
    let db2 = database(&other);
    let foreign = live(&db2, &other, true);
    assert!(op.record(&foreign, &native).is_err());
    op.retire(&stopped(&db, &id), &mut native).unwrap();
    for role in ALL {
        assert!(j.create_intent(role, None).is_err());
    }
}

#[test]
fn unused_profile_native_admission_rejects_foreign_actor_and_nonlive_before_sdk() {
    for defect in ["pid", "created", "session", "owner"] {
        let mut id = identity();
        match defect {
            "pid" => id.execution.pid += 100000,
            "created" => id.execution.created += 1,
            "session" => id.session_id += 1,
            "owner" => {
                id.execution.owner = "S-1-5-21-1-2-3-9001".into();
                id.execution.identity.binding.owner_sid = id.execution.owner.clone();
            }
            _ => unreachable!(),
        }
        let db = database(&id);
        let j = live(&db, &id, true);
        assert!(j.prepare_unused_host_profile().is_err());
        assert_eq!(j.replay().unwrap().events, 0);
    }
    let id = identity();
    let db = database(&id);
    live(&db, &id, true);
    assert!(stopped(&db, &id).prepare_unused_host_profile().is_err());
}

#[test]
fn unused_profile_receipt_rejects_wrong_role_policy_identity_and_mixed_history() {
    let id = identity();
    for (role, policy, folder, mixed) in [
        (ROLE, 0, [7; 24], false),
        (ROLE, 2, [7; 24], false),
        (ROLE, 1, [0; 24], false),
        (Resource::AccountProfile, 1, [7; 24], false),
        (ROLE, 1, [7; 24], true),
    ] {
        let nonce = super::super::super::random_id().unwrap();
        let mut replay = Replay::default();
        replay.states.insert(
            role,
            State::Creating {
                nonce: nonce.clone(),
            },
        );
        if mixed {
            replay.states.insert(Resource::BootstrapJob, State::Removed);
        }
        assert!(
            replay
                .apply(
                    &id,
                    &Event::Created {
                        resource: role,
                        nonce,
                        observed: Observation::UnusedHostProfile {
                            policy_version: policy,
                            folder_id: folder
                        }
                    }
                )
                .is_err()
        );
        assert!(!replay.unused_profile_only);
        assert!(matches!(replay.states[&role], State::Creating { .. }));
    }
}

#[path = "account_resource_profile_host_tests.rs"]
mod disposable;
