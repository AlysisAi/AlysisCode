//! Synthetic BFE transaction/registry crash controls and in-memory native
//! descriptor builders only. No test opens BFE or changes machine state.
use super::*;
use std::{cell::RefCell, rc::Rc};

fn ready() -> Observed {
    Observed {
        sublayer: true,
        filters: [true; 2],
        complete: true,
    }
}

#[derive(Default)]
struct State {
    ownership: Option<Ownership>,
    policy: Observed,
    events: Vec<String>,
    writes: usize,
    fail_write: Option<(usize, bool)>,
    fail_native: Option<&'static str>,
    foreign: bool,
}
#[derive(Clone)]
struct FakeStore(Rc<RefCell<State>>);
impl FakeStore {
    fn new(prior: Option<Ownership>, policy: Observed) -> Self {
        Self(Rc::new(RefCell::new(State {
            ownership: prior,
            policy,
            ..Default::default()
        })))
    }
    fn transaction(&self) -> FakeTransaction {
        FakeTransaction {
            state: self.0.clone(),
            staged: self.0.borrow().policy,
        }
    }
    fn run(&mut self, record: &SetupRecord, desired: Desired) -> Result<()> {
        let mut transaction = self.transaction();
        reconcile(self, &mut transaction, record, desired)
    }
}
impl Store for FakeStore {
    fn read(&self, record: &SetupRecord) -> Result<Option<Ownership>> {
        let value = self.0.borrow().ownership.clone();
        if let Some(value) = &value {
            value.validate(record)?;
        }
        Ok(value)
    }
    fn write(&mut self, value: &Ownership) -> Result<()> {
        let mut state = self.0.borrow_mut();
        state.writes += 1;
        ensure!(
            state.fail_write != Some((state.writes, false)),
            "crash before ownership write"
        );
        state.ownership = Some(value.clone());
        state.events.push(format!("write:{:?}", value.phase));
        ensure!(
            state.fail_write != Some((state.writes, true)),
            "crash after ownership write/readback"
        );
        Ok(())
    }
}
struct FakeTransaction {
    state: Rc<RefCell<State>>,
    staged: Observed,
}
impl FakeTransaction {
    fn step(&self, name: &'static str) -> Result<()> {
        let mut state = self.state.borrow_mut();
        state.events.push(name.into());
        ensure!(
            state.fail_native != Some(name),
            "injected native {name} failure"
        );
        Ok(())
    }
}
impl Transaction for FakeTransaction {
    fn observe(&self, _: &SetupRecord) -> Result<Observed> {
        let state = self.state.borrow();
        ensure!(
            !state.foreign && state.fail_native != Some("observe"),
            "unowned/unreadable native object"
        );
        if !self.staged.empty() && state.fail_native == Some("readback") {
            anyhow::bail!("injected incomplete native readback");
        }
        Ok(self.staged)
    }
    fn remove(&mut self, _: &SetupRecord, observed: Observed) -> Result<()> {
        assert_eq!(self.staged, observed);
        if !observed.empty() {
            self.step("remove")?;
            self.staged = Observed::default();
        }
        Ok(())
    }
    fn add(&mut self, _: &SetupRecord) -> Result<()> {
        self.step("add")?;
        assert!(self.staged.empty());
        self.staged = ready();
        Ok(())
    }
    fn commit(&mut self) -> Result<()> {
        self.step("commit")?;
        self.state.borrow_mut().policy = self.staged;
        self.step("committed-response")
    }
}

#[test]
fn candidate_wfp_writer_builds_the_exact_verified_offline_filters() {
    let record = crate::account_install::synthetic_ready();
    for index in 0..2 {
        let filter = Filter::new(&record, index).unwrap();
        validate_filter(&filter.value, &record, index).unwrap();
        assert_ne!(filter.value.subLayerKey, crate::wfp::DEFAULT_SUBLAYER_GUID);
    }
    assert!(Filter::new(&record, 2).is_err());
}

#[test]
fn candidate_wfp_writer_security_is_born_admin_only_without_inheritance() {
    validate_security(OwnedSd::from_sddl(SECURITY).unwrap().ptr).unwrap();
    for altered in [
        SECURITY.replace("D:P", "D:"),
        SECURITY.replacen("O:BA", "O:BU", 1),
        SECURITY.replacen("G:BA", "G:BU", 1),
        SECURITY.replace(";;;SY)", ";;;BU)"),
        SECURITY.replace(";;;SY)", ";;;BA)"),
        SECURITY.replace("(A;;", "(A;CI;"),
        SECURITY.replace("0x000f07ff", "0x00020000"),
        format!("{SECURITY}(A;;0x000f07ff;;;WD)"),
        "O:BAG:BAD:NO_ACCESS_CONTROL".into(),
    ] {
        let sd = OwnedSd::from_sddl(&altered).unwrap();
        assert!(validate_security(sd.ptr).is_err(), "accepted {altered}");
    }
}

#[test]
fn candidate_wfp_writer_ownership_binds_all_principals_and_committed_phase() {
    let record = crate::account_install::synthetic_ready();
    let prior = Ownership::new(&record, Phase::Present).unwrap();
    require_complete(&record, Some(&serde_json::to_string(&prior).unwrap())).unwrap();
    assert!(require_complete(&record, None).is_err());
    for phase in [Phase::PendingPresent, Phase::PendingRemove, Phase::Removed] {
        let mut value = prior.clone();
        value.phase = phase;
        assert!(require_complete(&record, Some(&serde_json::to_string(&value).unwrap())).is_err());
    }
    for field in [
        "installation_id",
        "owner_sid",
        "account_name",
        "account_sid",
        "group_sid",
        "mode",
    ] {
        let mut value = serde_json::to_value(&prior).unwrap();
        value["binding"][field] = "changed".into();
        assert!(decode(Some(&value.to_string()), &record).is_err());
    }
    for field in ["version", "online_sid", "unexpected"] {
        let mut value = serde_json::to_value(&prior).unwrap();
        value[field] = "changed".into();
        assert!(decode(Some(&value.to_string()), &record).is_err());
    }
}

#[test]
fn candidate_wfp_writer_refuses_existing_objects_without_prior_ownership() {
    let record = crate::account_install::synthetic_ready();
    for policy in [
        ready(),
        Observed {
            sublayer: true,
            ..Default::default()
        },
    ] {
        let mut store = FakeStore::new(None, policy);
        assert!(
            store
                .run(&record, Desired::Present)
                .unwrap_err()
                .to_string()
                .contains("refusing adoption")
        );
        assert!(store.0.borrow().events.is_empty());
        assert_eq!(store.0.borrow().policy, policy);
        assert!(store.0.borrow().ownership.is_none());
    }
}

#[test]
fn candidate_wfp_writer_persists_intent_before_policy_and_result_after_commit() {
    let record = crate::account_install::synthetic_ready();
    let mut store = FakeStore::new(None, Observed::default());
    store.run(&record, Desired::Present).unwrap();
    let state = store.0.borrow();
    assert_eq!(
        state.events,
        [
            "write:PendingPresent",
            "add",
            "commit",
            "committed-response",
            "write:Present"
        ]
    );
    assert_eq!(state.policy, ready());
    assert_eq!(state.ownership.as_ref().unwrap().phase, Phase::Present);
}

#[test]
fn candidate_wfp_writer_repair_is_atomic_and_owned_only() {
    let record = crate::account_install::synthetic_ready();
    for policy in [
        Observed::default(),
        Observed {
            sublayer: true,
            filters: [true, false],
            complete: false,
        },
        Observed {
            complete: false,
            ..ready()
        },
        ready(),
    ] {
        let prior = Ownership::new(&record, Phase::Present).unwrap();
        let mut store = FakeStore::new(Some(prior), policy);
        store.run(&record, Desired::Present).unwrap();
        assert_eq!(store.0.borrow().policy, ready());
        assert_eq!(
            store.0.borrow().events.iter().any(|e| e == "add"),
            !policy.complete
        );
        assert_eq!(
            store.0.borrow().events.iter().any(|e| e == "remove"),
            !policy.empty() && !policy.complete
        );
    }
}

#[test]
fn candidate_wfp_writer_foreign_or_unreadable_objects_cannot_be_repaired_or_removed() {
    let record = crate::account_install::synthetic_ready();
    for desired in [Desired::Present, Desired::Removed] {
        let prior = Ownership::new(&record, Phase::Present).unwrap();
        let mut store = FakeStore::new(Some(prior.clone()), ready());
        store.0.borrow_mut().foreign = true;
        assert!(store.run(&record, desired).is_err());
        let state = store.0.borrow();
        assert!(state.events.is_empty());
        assert_eq!(state.policy, ready());
        assert_eq!(state.ownership, Some(prior));
    }
}

#[test]
fn candidate_wfp_writer_intent_write_failures_precede_all_native_mutations() {
    let record = crate::account_install::synthetic_ready();
    for after in [false, true] {
        let mut store = FakeStore::new(None, Observed::default());
        store.0.borrow_mut().fail_write = Some((1, after));
        assert!(store.run(&record, Desired::Present).is_err());
        let state = store.0.borrow();
        assert!(state.policy.empty());
        assert!(
            !state
                .events
                .iter()
                .any(|e| matches!(e.as_str(), "remove" | "add" | "commit"))
        );
        assert_eq!(state.ownership.is_some(), after);
    }
}

#[test]
fn candidate_wfp_writer_failed_transaction_preserves_old_rules_and_pending_intent() {
    let record = crate::account_install::synthetic_ready();
    let old = Observed {
        complete: false,
        ..ready()
    };
    for point in ["remove", "add", "commit"] {
        let mut store = FakeStore::new(Some(Ownership::new(&record, Phase::Present).unwrap()), old);
        store.0.borrow_mut().fail_native = Some(point);
        assert!(store.run(&record, Desired::Present).is_err());
        let state = store.0.borrow();
        assert_eq!(state.policy, old);
        assert_eq!(
            state.ownership.as_ref().unwrap().phase,
            Phase::PendingPresent
        );
    }
    let mut store = FakeStore::new(None, Observed::default());
    store.0.borrow_mut().fail_native = Some("readback");
    assert!(store.run(&record, Desired::Present).is_err());
    assert!(store.0.borrow().policy.empty());
    assert_eq!(
        store.0.borrow().ownership.as_ref().unwrap().phase,
        Phase::PendingPresent
    );
}

#[test]
fn candidate_wfp_writer_uncertain_commit_is_owned_and_reopened_without_rollback() {
    let record = crate::account_install::synthetic_ready();
    let mut store = FakeStore::new(None, Observed::default());
    store.0.borrow_mut().fail_native = Some("committed-response");
    assert!(store.run(&record, Desired::Present).is_err());
    assert_eq!(store.0.borrow().policy, ready());
    assert_eq!(
        store.0.borrow().ownership.as_ref().unwrap().phase,
        Phase::PendingPresent
    );
    store.0.borrow_mut().fail_native = None;
    store.0.borrow_mut().events.clear();
    store.run(&record, Desired::Present).unwrap();
    assert!(
        !store
            .0
            .borrow()
            .events
            .iter()
            .any(|e| matches!(e.as_str(), "add" | "remove"))
    );
    assert_eq!(
        store.0.borrow().ownership.as_ref().unwrap().phase,
        Phase::Present
    );
}

#[test]
fn candidate_wfp_writer_final_registry_failure_preserves_committed_policy() {
    let record = crate::account_install::synthetic_ready();
    for after in [false, true] {
        let mut store = FakeStore::new(None, Observed::default());
        store.0.borrow_mut().fail_write = Some((2, after));
        assert!(store.run(&record, Desired::Present).is_err());
        assert_eq!(store.0.borrow().policy, ready());
        assert_eq!(
            store.0.borrow().ownership.as_ref().unwrap().phase,
            if after {
                Phase::Present
            } else {
                Phase::PendingPresent
            }
        );
    }
}

#[test]
fn candidate_wfp_writer_removal_mechanics_keep_exact_identity_and_are_idempotent() {
    let record = crate::account_install::synthetic_ready();
    let mut store = FakeStore::new(
        Some(Ownership::new(&record, Phase::Present).unwrap()),
        ready(),
    );
    store.run(&record, Desired::Removed).unwrap();
    assert!(store.0.borrow().policy.empty());
    assert_eq!(
        store.0.borrow().ownership.as_ref().unwrap().phase,
        Phase::Removed
    );
    assert_eq!(
        store.0.borrow().ownership.as_ref().unwrap().binding,
        record.binding(AccountMode::Offline).unwrap()
    );
    store.run(&record, Desired::Removed).unwrap();
    assert!(store.run(&record, Desired::Present).is_err());
    store.0.borrow_mut().policy = ready();
    assert!(
        store
            .run(&record, Desired::Removed)
            .unwrap_err()
            .to_string()
            .contains("reappeared")
    );
    assert_eq!(store.0.borrow().policy, ready());
}

#[test]
fn candidate_wfp_writer_removal_uncertainty_never_claims_absence_early() {
    let record = crate::account_install::synthetic_ready();
    for point in ["remove", "commit", "committed-response"] {
        let mut store = FakeStore::new(
            Some(Ownership::new(&record, Phase::Present).unwrap()),
            ready(),
        );
        store.0.borrow_mut().fail_native = Some(point);
        assert!(store.run(&record, Desired::Removed).is_err());
        assert_eq!(
            store.0.borrow().ownership.as_ref().unwrap().phase,
            Phase::PendingRemove
        );
        assert_eq!(
            store.0.borrow().policy.empty(),
            point == "committed-response"
        );
    }
    assert!(
        refuse_removal()
            .unwrap_err()
            .to_string()
            .contains("quiescence")
    );
}
