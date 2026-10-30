use super::*;
use std::{cell::RefCell, rc::Rc};

type Events = Rc<RefCell<Vec<&'static str>>>;

fn selected(mode: AccountMode) -> Identity {
    Identity::selected(&crate::account_install::synthetic_ready(), mode).unwrap()
}
fn older(selected: &Identity) -> Identity {
    let mut value = selected.clone();
    value.generation = "CredOwner-2123456789abcdef0123456789abcdef".into();
    value
}
fn account(selected: &Identity) -> Account {
    Account {
        sid: selected.binding.account_sid.clone(),
        flags: UF_NORMAL_ACCOUNT | UF_SCRIPT.0 | UF_DONT_EXPIRE_PASSWD.0 | UF_ACCOUNTDISABLE.0,
        privilege: USER_PRIV_USER.0,
        operator_flags: 0,
        password_expired: 0,
    }
}
struct MemoryStore {
    record: Option<Record>,
    events: Events,
    writes: usize,
    fail_write: Option<(usize, bool)>,
    fail_read: bool,
}
impl Store for MemoryStore {
    fn read(&self, _: &Identity) -> Result<Option<Record>> {
        self.events.borrow_mut().push("read");
        ensure!(!self.fail_read, "synthetic record read error");
        Ok(self.record.clone())
    }
    fn write(&mut self, record: &Record) -> Result<()> {
        self.writes += 1;
        ensure!(
            self.fail_write != Some((self.writes, false)),
            "synthetic failure before durable record"
        );
        self.record = Some(record.clone());
        self.events.borrow_mut().push(match record.phase {
            Phase::Pending => "pending",
            Phase::Applied => "applied",
        });
        ensure!(
            self.fail_write != Some((self.writes, true)),
            "synthetic lost durable write acknowledgement"
        );
        Ok(())
    }
}
struct FakeSam {
    account: Account,
    events: Events,
    observations: usize,
    preparations: usize,
    setters: usize,
    changed_password: bool,
    fail_observe: Option<usize>,
    drift: Option<(usize, Account)>,
    fail_prepare: bool,
    fail_setter: Option<bool>,
}
impl Sam for FakeSam {
    fn observe(&mut self) -> Result<Account> {
        self.observations += 1;
        self.events.borrow_mut().push("observe");
        ensure!(
            self.fail_observe != Some(self.observations),
            "synthetic observation error"
        );
        if let Some((at, changed)) = &self.drift {
            if self.observations == *at {
                return Ok(changed.clone());
            }
        }
        Ok(self.account.clone())
    }
    fn prepare_credential(&mut self) -> Result<()> {
        self.preparations += 1;
        self.events.borrow_mut().push("prepare");
        ensure!(!self.fail_prepare, "synthetic credential read error");
        Ok(())
    }
    fn set_password(&mut self) -> Result<()> {
        self.setters += 1;
        self.events.borrow_mut().push("set");
        ensure!(self.fail_setter != Some(false), "synthetic setter error");
        self.changed_password = true;
        ensure!(
            self.fail_setter != Some(true),
            "synthetic lost setter result"
        );
        Ok(())
    }
}
fn fixture(selected: &Identity) -> (MemoryStore, FakeSam, BTreeSet<String>) {
    let events = Events::default();
    (
        MemoryStore {
            record: None,
            events: events.clone(),
            writes: 0,
            fail_write: None,
            fail_read: false,
        },
        FakeSam {
            account: account(selected),
            events,
            observations: 0,
            preparations: 0,
            setters: 0,
            changed_password: false,
            fail_observe: None,
            drift: None,
            fail_prepare: false,
            fail_setter: None,
        },
        BTreeSet::from([selected.generation.clone()]),
    )
}

#[test]
fn candidate_password_orders_durable_intent_native_change_and_receipt() {
    for mode in [AccountMode::Offline, AccountMode::Online] {
        let selected = selected(mode);
        let (mut store, mut sam, inventory) = fixture(&selected);
        apply_selected(&mut store, &mut sam, &selected, &inventory).unwrap();
        assert_eq!(
            *store.events.borrow(),
            [
                "read", "observe", "prepare", "pending", "observe", "set", "observe", "applied"
            ]
        );
        assert_eq!(store.record, Some(Record::new(&selected, Phase::Applied)));
        assert_eq!(sam.setters, 1);
        assert!(sam.changed_password);
        assert_eq!(sam.account, account(&selected));
    }
}

#[test]
fn candidate_password_completed_retry_checks_disabled_identity_without_loading_secret() {
    let selected = selected(AccountMode::Offline);
    let (mut store, mut sam, inventory) = fixture(&selected);
    store.record = Some(Record::new(&selected, Phase::Applied));
    apply_selected(&mut store, &mut sam, &selected, &inventory).unwrap();
    assert_eq!(*store.events.borrow(), ["read", "observe"]);
    assert_eq!((sam.preparations, sam.setters, store.writes), (0, 0, 0));
    sam.account.flags &= !UF_ACCOUNTDISABLE.0;
    assert!(apply_selected(&mut store, &mut sam, &selected, &inventory).is_err());
    assert_eq!(sam.preparations, 0);
}

#[test]
fn candidate_password_rotation_requires_completed_prior_generation_inventory() {
    let selected = selected(AccountMode::Online);
    let prior = older(&selected);
    let (mut store, mut sam, mut inventory) = fixture(&selected);
    store.record = Some(Record::new(&prior, Phase::Applied));
    inventory.insert(prior.generation.clone());
    apply_selected(&mut store, &mut sam, &selected, &inventory).unwrap();
    assert_eq!(store.record, Some(Record::new(&selected, Phase::Applied)));
    assert_eq!(sam.setters, 1);
    assert!(inventory.contains(&prior.generation));
}

#[test]
fn candidate_password_pending_outcomes_cannot_be_retried_or_superseded() {
    let selected = selected(AccountMode::Offline);
    for pending in [selected.clone(), older(&selected)] {
        let (mut store, mut sam, mut inventory) = fixture(&selected);
        inventory.insert(pending.generation.clone());
        let original = Record::new(&pending, Phase::Pending);
        store.record = Some(original.clone());
        assert!(apply_selected(&mut store, &mut sam, &selected, &inventory).is_err());
        assert_eq!(store.record, Some(original));
        assert_eq!(*store.events.borrow(), ["read"]);
        assert_eq!((sam.preparations, sam.setters, store.writes), (0, 0, 0));
    }
}

#[test]
fn candidate_password_preserves_every_before_and_after_registry_crash_boundary() {
    let selected = selected(AccountMode::Offline);
    for write in 1..=2 {
        for persisted in [false, true] {
            let (mut store, mut sam, inventory) = fixture(&selected);
            store.fail_write = Some((write, persisted));
            assert!(apply_selected(&mut store, &mut sam, &selected, &inventory).is_err());
            let expected = match (write, persisted) {
                (1, false) => None,
                (2, true) => Some(Record::new(&selected, Phase::Applied)),
                _ => Some(Record::new(&selected, Phase::Pending)),
            };
            assert_eq!(store.record, expected);
            assert_eq!(sam.setters, usize::from(write == 2));
            store.fail_write = None;
            let before_retry = sam.setters;
            let retry = apply_selected(&mut store, &mut sam, &selected, &inventory);
            match expected.map(|value| value.phase) {
                None => {
                    retry.unwrap();
                    assert_eq!(sam.setters, before_retry + 1);
                }
                Some(Phase::Pending) => {
                    assert!(retry.is_err());
                    assert_eq!(sam.setters, before_retry);
                }
                Some(Phase::Applied) => {
                    retry.unwrap();
                    assert_eq!(sam.setters, before_retry);
                }
            }
        }
    }
}

#[test]
fn candidate_password_observation_failures_preserve_the_last_durable_state() {
    let selected = selected(AccountMode::Online);
    for observation in 1..=3 {
        let (mut store, mut sam, inventory) = fixture(&selected);
        sam.fail_observe = Some(observation);
        assert!(apply_selected(&mut store, &mut sam, &selected, &inventory).is_err());
        assert_eq!(sam.setters, usize::from(observation == 3));
        assert_eq!(
            store.record,
            (observation > 1).then(|| Record::new(&selected, Phase::Pending))
        );
    }
}

#[test]
fn candidate_password_identity_or_account_state_drift_never_receives_applied_receipt() {
    let selected = selected(AccountMode::Offline);
    let mut wrong_sid = account(&selected);
    wrong_sid.sid = "S-1-5-21-1-2-3-1100".into();
    let mut enabled = account(&selected);
    enabled.flags &= !UF_ACCOUNTDISABLE.0;
    let mut privileged = account(&selected);
    privileged.privilege = USER_PRIV_ADMIN.0;
    for drift in [wrong_sid, enabled, privileged] {
        for observation in 1..=3 {
            let (mut store, mut sam, inventory) = fixture(&selected);
            sam.drift = Some((observation, drift.clone()));
            assert!(apply_selected(&mut store, &mut sam, &selected, &inventory).is_err());
            assert_eq!(sam.setters, usize::from(observation == 3));
            assert_eq!(
                store.record,
                (observation > 1).then(|| Record::new(&selected, Phase::Pending))
            );
        }
    }
}

#[test]
fn candidate_password_credential_failure_precedes_durable_mutation_intent() {
    let selected = selected(AccountMode::Offline);
    let (mut store, mut sam, inventory) = fixture(&selected);
    sam.fail_prepare = true;
    assert!(apply_selected(&mut store, &mut sam, &selected, &inventory).is_err());
    assert_eq!(store.record, None);
    assert_eq!((sam.setters, store.writes), (0, 0));
}

#[test]
fn candidate_password_setter_error_and_lost_result_both_preserve_pending_without_retry() {
    let selected = selected(AccountMode::Offline);
    for changed in [false, true] {
        let (mut store, mut sam, inventory) = fixture(&selected);
        sam.fail_setter = Some(changed);
        assert!(apply_selected(&mut store, &mut sam, &selected, &inventory).is_err());
        assert_eq!(sam.changed_password, changed);
        assert_eq!(store.record, Some(Record::new(&selected, Phase::Pending)));
        sam.fail_setter = None;
        assert!(apply_selected(&mut store, &mut sam, &selected, &inventory).is_err());
        assert_eq!(sam.setters, 1);
    }
}

#[test]
fn candidate_password_missing_inventory_or_record_read_error_never_reaches_sam() {
    let selected = selected(AccountMode::Offline);
    for failure in 0..3 {
        let (mut store, mut sam, mut inventory) = fixture(&selected);
        match failure {
            0 => inventory.clear(),
            1 => store.record = Some(Record::new(&older(&selected), Phase::Applied)),
            _ => store.fail_read = true,
        }
        assert!(apply_selected(&mut store, &mut sam, &selected, &inventory).is_err());
        assert_eq!(
            (
                sam.observations,
                sam.preparations,
                sam.setters,
                store.writes
            ),
            (0, 0, 0, 0)
        );
    }
}

#[test]
fn candidate_password_receipts_reject_every_foreign_identity_component() {
    let selected = selected(AccountMode::Offline);
    for field in 0..8 {
        let (mut store, mut sam, inventory) = fixture(&selected);
        let mut foreign = selected.clone();
        match field {
            0 => foreign.binding.installation_id = "3123456789abcdef0123456789abcdef".into(),
            1 => foreign.binding.owner_sid = "S-1-5-21-1-2-3-1200".into(),
            2 => foreign.binding.account_sid = "S-1-5-21-1-2-3-1201".into(),
            3 => foreign.binding.group_sid = "S-1-5-21-1-2-3-1202".into(),
            4 => foreign.peer_sid = "S-1-5-21-1-2-3-1203".into(),
            5 => foreign.binding.mode = AccountMode::Online,
            6 => foreign.binding.account_name = "unowned_account".into(),
            _ => foreign.generation = "../Blob".into(),
        }
        store.record = Some(Record::new(&foreign, Phase::Applied));
        assert!(apply_selected(&mut store, &mut sam, &selected, &inventory).is_err());
        assert_eq!(
            (
                sam.observations,
                sam.preparations,
                sam.setters,
                store.writes
            ),
            (0, 0, 0, 0)
        );
    }
}

#[test]
fn candidate_password_registry_receipt_parser_is_strict_and_bounded() {
    let selected = selected(AccountMode::Offline);
    let good = serde_json::to_value(Record::new(&selected, Phase::Applied)).unwrap();
    for field in 0..5 {
        let mut bad = good.clone();
        match field {
            0 => bad["version"] = serde_json::json!(2),
            1 => bad["phase"] = serde_json::json!("verified"),
            2 => bad["unrecognized"] = serde_json::json!(true),
            3 => bad["identity"]["unknown"] = serde_json::json!(true),
            _ => bad["identity"]["binding"]["unknown"] = serde_json::json!(true),
        }
        assert!(decode(Some(&bad.to_string()), &selected).is_err());
    }
    assert!(decode(Some("{"), &selected).is_err());
    assert!(decode(Some(&" ".repeat(16 * 1024 + 1)), &selected).is_err());
    assert!(decode(None, &selected).unwrap().is_none());
}

#[test]
fn candidate_password_readiness_requires_exact_selected_applied_generation() {
    let record = crate::account_install::synthetic_ready();
    for mode in [AccountMode::Offline, AccountMode::Online] {
        let selected = Identity::selected(&record, mode).unwrap();
        let applied = serde_json::to_string(&Record::new(&selected, Phase::Applied)).unwrap();
        require_applied(&record, mode, Some(&applied)).unwrap();
        assert!(require_applied(&record, mode, None).is_err());
        let pending = serde_json::to_string(&Record::new(&selected, Phase::Pending)).unwrap();
        assert!(require_applied(&record, mode, Some(&pending)).is_err());
        let stale = serde_json::to_string(&Record::new(&older(&selected), Phase::Applied)).unwrap();
        assert!(require_applied(&record, mode, Some(&stale)).is_err());
        let other = match mode {
            AccountMode::Offline => AccountMode::Online,
            AccountMode::Online => AccountMode::Offline,
        };
        assert!(require_applied(&record, other, Some(&applied)).is_err());
    }
}

#[test]
fn candidate_password_native_observation_requires_disabled_ordinary_exact_account() {
    let selected = selected(AccountMode::Offline);
    let good = account(&selected);
    good.validate(&selected.binding).unwrap();
    let mut optional = good.clone();
    optional.flags |= UF_PASSWD_CANT_CHANGE.0;
    optional.validate(&selected.binding).unwrap();
    for bit in 0..32 {
        // PASSWD_CANT_CHANGE is the only optional flag.
        if 1 << bit == UF_PASSWD_CANT_CHANGE.0 {
            continue;
        }
        let mut bad = good.clone();
        bad.flags ^= 1 << bit;
        assert!(bad.validate(&selected.binding).is_err(), "flag bit {bit}");
    }
    for field in 0..4 {
        let mut bad = good.clone();
        match field {
            0 => bad.sid = selected.peer_sid.clone(),
            1 => bad.privilege = USER_PRIV_ADMIN.0,
            2 => bad.operator_flags = 1,
            _ => bad.password_expired = 1,
        }
        assert!(bad.validate(&selected.binding).is_err());
    }
}

#[test]
fn candidate_password_observation_reads_no_returned_password_pointer() {
    let selected = selected(AccountMode::Offline);
    let sid = crate::sid::LocalPsid::from_string(&selected.binding.account_sid).unwrap();
    let expected = account(&selected);
    let info = USER_INFO_4 {
        // Deliberately invalid: the level-4 password field must never be read.
        usri4_password: PWSTR(1usize as *mut u16),
        usri4_user_sid: sid.as_psid(),
        usri4_flags: USER_ACCOUNT_FLAGS(expected.flags),
        usri4_priv: USER_PRIV(expected.privilege),
        ..Default::default()
    };
    assert_eq!(account_from_info(&info).unwrap(), expected);
}
