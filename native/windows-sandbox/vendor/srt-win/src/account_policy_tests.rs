use super::*;
use std::{cell::RefCell, rc::Rc};

fn identity() -> Identity {
    Identity::selected(&crate::account_install::synthetic_ready()).unwrap()
}
fn initial(identity: &Identity, users: u8) -> Snapshot {
    let account = |binding: &AccountBinding, index| Account {
        sid: binding.account_sid.clone(),
        flags: UF_NORMAL_ACCOUNT | UF_SCRIPT.0 | UF_DONT_EXPIRE_PASSWD.0 | UF_ACCOUNTDISABLE.0,
        privilege: if users & (1u8 << index) != 0u8 {
            USER_PRIV_USER.0
        } else {
            USER_PRIV_GUEST.0
        },
        operator_flags: 0,
        password_expired: 0,
        groups: if users & (1u8 << index) != 0u8 {
            BTreeSet::from([activation::USERS.into()])
        } else {
            BTreeSet::new()
        },
        rights: BTreeSet::new(),
    };
    Snapshot {
        group_sid: identity.offline.group_sid.clone(),
        group_members: BTreeSet::new(),
        group_rights: BTreeSet::new(),
        offline: account(&identity.offline, 0),
        online: account(&identity.online, 1),
    }
}
struct State {
    receipt: Option<Receipt>,
    observed: Snapshot,
    operations: usize,
    fail_operation: Option<(usize, bool)>,
    reads_fail: bool,
    observations: usize,
    fail_observation: Option<usize>,
    effects: Vec<Effect>,
    drift_after_effect: bool,
}
#[derive(Clone)]
struct Fake(Rc<RefCell<State>>, Identity);
impl Fake {
    fn new(users: u8) -> Self {
        let identity = identity();
        Self(
            Rc::new(RefCell::new(State {
                receipt: None,
                observed: initial(&identity, users),
                operations: 0,
                fail_operation: None,
                reads_fail: false,
                observations: 0,
                fail_observation: None,
                effects: vec![],
                drift_after_effect: false,
            })),
            identity,
        )
    }
    fn operation(&self, action: impl FnOnce(&mut State)) -> Result<()> {
        let mut state = self.0.borrow_mut();
        state.operations += 1;
        ensure!(
            state.fail_operation != Some((state.operations, false)),
            "before operation"
        );
        action(&mut state);
        ensure!(
            state.fail_operation != Some((state.operations, true)),
            "after operation"
        );
        Ok(())
    }
    fn run(&self) -> Result<()> {
        prepare(&mut self.clone(), &mut self.clone(), &self.1)
    }
}
impl Store for Fake {
    fn read(&self, _: &Identity) -> Result<Option<Receipt>> {
        let state = self.0.borrow();
        ensure!(!state.reads_fail, "synthetic registry read failure");
        Ok(state.receipt.clone())
    }
    fn write(&mut self, receipt: &Receipt) -> Result<()> {
        self.operation(|state| state.receipt = Some(receipt.clone()))
    }
}
impl Policy for Fake {
    fn observe(&mut self) -> Result<Snapshot> {
        let mut state = self.0.borrow_mut();
        state.observations += 1;
        ensure!(
            state.fail_observation != Some(state.observations),
            "synthetic observation failure"
        );
        Ok(state.observed.clone())
    }
    fn apply(&mut self, effect: Effect) -> Result<()> {
        self.operation(|state| {
            assert!(state.receipt.as_ref().unwrap().pending);
            assert_eq!(state.receipt.as_ref().unwrap().identity, self.1);
            if effect != Effect::Denials {
                assert_eq!(
                    state.observed.group_rights,
                    activation::DENIALS.into_iter().map(str::to_owned).collect()
                );
            }
            state.observed.apply(effect, &self.1);
            state.effects.push(effect);
            if state.drift_after_effect {
                state.observed.offline.flags &= !UF_ACCOUNTDISABLE.0;
            }
        })
    }
}

#[test]
fn candidate_policy_fixed_plan_preserves_existing_users_and_never_enables_accounts() {
    for users in 0..4 {
        let fake = Fake::new(users);
        let plan = effects(&initial(&fake.1, users));
        fake.run().unwrap();
        let state = fake.0.borrow();
        assert_eq!(state.effects, plan);
        assert_eq!(state.effects.first(), Some(&Effect::Denials));
        let receipt = state.receipt.as_ref().unwrap();
        assert!(receipt.complete());
        assert_eq!(state.observed, receipt.expected());
        for account in [&state.observed.offline, &state.observed.online] {
            assert_ne!(account.flags & UF_ACCOUNTDISABLE.0, 0);
            assert_eq!(account.privilege, USER_PRIV_USER.0);
            assert!(account.rights.is_empty());
            assert_eq!(
                account.groups,
                BTreeSet::from([activation::USERS.into(), fake.1.offline.group_sid.clone()])
            );
        }
        assert_eq!(
            state.observed.group_members,
            BTreeSet::from([
                fake.1.offline.account_sid.clone(),
                fake.1.online.account_sid.clone()
            ])
        );
    }
}

#[test]
fn candidate_policy_privilege_must_match_exact_disabled_membership_phase() {
    let identity = identity();
    for users in [false, true] {
        for privilege in [USER_PRIV_GUEST.0, USER_PRIV_USER.0, USER_PRIV_ADMIN.0, 99] {
            let fake = Fake::new(u8::from(users));
            fake.0.borrow_mut().observed.offline.privilege = privilege;
            let expected = if users {
                USER_PRIV_USER.0
            } else {
                USER_PRIV_GUEST.0
            };
            if privilege == expected {
                fake.run().unwrap();
                assert_eq!(fake.0.borrow().observed.offline.privilege, USER_PRIV_USER.0);
            } else {
                assert!(fake.run().is_err());
                assert_eq!(fake.0.borrow().operations, 0);
                assert!(fake.0.borrow().receipt.is_none());
            }
        }
    }
    // Neither Guest nor User classification substitutes for exact group and
    // direct-right checks, including Guests and a premature owned membership.
    for group in [
        "S-1-5-32-546",
        "S-1-5-32-544",
        identity.offline.group_sid.as_str(),
    ] {
        let mut account = initial(&identity, 0).offline;
        account.groups.insert(group.to_owned());
        assert!(account.validate_disabled(&identity.offline).is_err());
    }
    let mut account = initial(&identity, 0).offline;
    account.rights.insert("SeInteractiveLogonRight".into());
    assert!(account.validate_disabled(&identity.offline).is_err());
}

#[test]
fn candidate_policy_all_before_after_durable_and_native_boundaries_preserve_ownership() {
    for users in 0..4 {
        let count = 1 + effects(&initial(&identity(), users)).len() * 3;
        for operation in 1..=count {
            for after in [false, true] {
                let fake = Fake::new(users);
                fake.0.borrow_mut().fail_operation = Some((operation, after));
                assert!(
                    fake.run().is_err(),
                    "users={users} operation={operation} after={after}"
                );
                let original = fake.0.borrow().receipt.clone();
                let effects_before = fake.0.borrow().effects.len();
                fake.0.borrow_mut().fail_operation = None;
                let retry = fake.run();
                if original.as_ref().is_some_and(|receipt| receipt.pending) {
                    assert!(retry.is_err());
                    assert_eq!(fake.0.borrow().receipt, original);
                    assert_eq!(fake.0.borrow().effects.len(), effects_before);
                } else {
                    retry.unwrap();
                    assert!(fake.0.borrow().receipt.as_ref().unwrap().complete());
                    assert_eq!(
                        fake.0.borrow().effects.len(),
                        effects(&initial(&identity(), users)).len()
                    );
                }
                for account in [
                    &fake.0.borrow().observed.offline,
                    &fake.0.borrow().observed.online,
                ] {
                    assert_ne!(account.flags & UF_ACCOUNTDISABLE.0, 0);
                }
            }
        }
    }
}

#[test]
fn candidate_policy_complete_retry_is_observational_and_requires_exact_state() {
    let fake = Fake::new(0);
    fake.run().unwrap();
    let original = fake.0.borrow().receipt.clone();
    let count = fake.0.borrow().operations;
    fake.run().unwrap();
    assert_eq!(fake.0.borrow().operations, count);
    assert_eq!(fake.0.borrow().receipt, original);
    fake.0
        .borrow_mut()
        .observed
        .group_rights
        .remove(activation::DENIALS[0]);
    assert!(fake.run().is_err());
    assert_eq!(fake.0.borrow().operations, count);
}

#[test]
fn candidate_policy_completed_prefix_never_repairs_unrecorded_drift() {
    let fake = Fake::new(0);
    // initial receipt, denial intent/effect/receipt, then fail before next intent.
    fake.0.borrow_mut().fail_operation = Some((5, false));
    assert!(fake.run().is_err());
    assert_eq!(fake.0.borrow().receipt.as_ref().unwrap().completed, 1);
    assert!(!fake.0.borrow().receipt.as_ref().unwrap().pending);
    let original = fake.0.borrow().receipt.clone();
    let effects_before = fake.0.borrow().effects.len();
    fake.0.borrow_mut().fail_operation = None;
    fake.0
        .borrow_mut()
        .observed
        .group_members
        .insert("S-1-5-21-1-2-3-9000".into());
    assert!(fake.run().is_err());
    assert_eq!(fake.0.borrow().receipt, original);
    assert_eq!(fake.0.borrow().effects.len(), effects_before);
}

#[test]
fn candidate_policy_refuses_unowned_initial_policy_without_writing_any_intent() {
    for field in 0..11 {
        let fake = Fake::new(0);
        let mut state = fake.0.borrow_mut();
        match field {
            0 => {
                state
                    .observed
                    .group_members
                    .insert(fake.1.offline.account_sid.clone());
            }
            1 => {
                state
                    .observed
                    .group_rights
                    .insert(activation::DENIALS[0].into());
            }
            2 => {
                state
                    .observed
                    .offline
                    .groups
                    .insert(fake.1.offline.group_sid.clone());
            }
            3 => {
                state.observed.online.groups.insert("S-1-5-32-544".into());
            }
            4 => {
                state
                    .observed
                    .offline
                    .rights
                    .insert("SeTcbPrivilege".into());
            }
            5 => {
                state
                    .observed
                    .online
                    .rights
                    .insert(activation::DENIALS[0].into());
            }
            6 => state.observed.offline.flags &= !UF_ACCOUNTDISABLE.0,
            7 => state.observed.online.privilege = USER_PRIV_ADMIN.0,
            8 => state.observed.group_sid = "S-1-5-21-1-2-3-9000".into(),
            9 => state.observed.offline.sid = fake.1.online.account_sid.clone(),
            _ => state.observed.online.password_expired = 1,
        }
        drop(state);
        assert!(fake.run().is_err(), "field={field}");
        assert_eq!(fake.0.borrow().operations, 0);
        assert!(fake.0.borrow().receipt.is_none());
    }
}

#[test]
fn candidate_policy_native_observation_errors_never_issue_unrecorded_mutation() {
    for observation in 1..=17 {
        let fake = Fake::new(0);
        fake.0.borrow_mut().fail_observation = Some(observation);
        assert!(fake.run().is_err(), "observation={observation}");
        let state = fake.0.borrow();
        assert!(
            state
                .receipt
                .as_ref()
                .is_none_or(|receipt| !receipt.complete())
        );
        assert!(state.effects.len() <= 5);
        assert_ne!(state.observed.offline.flags & UF_ACCOUNTDISABLE.0, 0);
    }
}

#[test]
fn candidate_policy_unexpected_native_result_remains_pending_and_cannot_be_retried() {
    let fake = Fake::new(0);
    fake.0.borrow_mut().drift_after_effect = true;
    assert!(fake.run().is_err());
    let original = fake.0.borrow().receipt.clone();
    assert!(original.as_ref().unwrap().pending);
    fake.0.borrow_mut().observed.offline.flags |= UF_ACCOUNTDISABLE.0;
    fake.0.borrow_mut().drift_after_effect = false;
    assert!(fake.run().is_err());
    assert_eq!(fake.0.borrow().receipt, original);
    assert_eq!(fake.0.borrow().effects.len(), 1);
}

#[test]
fn candidate_policy_foreign_or_malformed_receipt_precedes_all_native_observation() {
    for field in 0..8 {
        let fake = Fake::new(0);
        let mut receipt = Receipt::new(&fake.1, initial(&fake.1, 0)).unwrap();
        match field {
            0 => {
                receipt.identity.offline.installation_id = "1123456789abcdef0123456789abcdef".into()
            }
            1 => receipt.identity.offline.owner_sid = "S-1-5-21-1-2-3-9000".into(),
            2 => receipt.identity.online.account_sid = "S-1-5-21-1-2-3-9000".into(),
            3 => receipt.identity.offline.group_sid = "S-1-5-21-1-2-3-9000".into(),
            4 => receipt.identity.offline.mode = AccountMode::Online,
            5 => receipt.completed = 6,
            6 => {
                receipt.completed = 5;
                receipt.pending = true;
            }
            _ => receipt.version = 2,
        }
        fake.0.borrow_mut().receipt = Some(receipt);
        assert!(fake.run().is_err());
        assert_eq!(fake.0.borrow().observations, 0);
        assert_eq!(fake.0.borrow().operations, 0);
    }
}

#[test]
fn candidate_policy_read_error_does_not_become_missing_inventory() {
    let fake = Fake::new(0);
    fake.0.borrow_mut().reads_fail = true;
    assert!(fake.run().is_err());
    assert_eq!(fake.0.borrow().observations, 0);
    assert_eq!(fake.0.borrow().operations, 0);
}

#[test]
fn candidate_policy_readiness_requires_complete_exact_identity_receipt() {
    let fake = Fake::new(0);
    let record = crate::account_install::synthetic_ready();
    assert!(require_complete(&record, None).is_err());
    let partial = Receipt::new(&fake.1, initial(&fake.1, 0)).unwrap();
    assert!(require_complete(&record, Some(&serde_json::to_string(&partial).unwrap())).is_err());
    fake.run().unwrap();
    let completed = serde_json::to_string(fake.0.borrow().receipt.as_ref().unwrap()).unwrap();
    require_complete(&record, Some(&completed)).unwrap();
    let mut foreign = record;
    foreign.installation_id = "1123456789abcdef0123456789abcdef".into();
    foreign.storage = None;
    assert!(require_complete(&foreign, Some(&completed)).is_err());
}

#[test]
fn candidate_policy_parser_is_bounded_and_rejects_unknown_fields() {
    let fake = Fake::new(0);
    let receipt = Receipt::new(&fake.1, initial(&fake.1, 0)).unwrap();
    let mut json = serde_json::to_value(&receipt).unwrap();
    json["initial"]["offline"]["extra"] = serde_json::json!(true);
    assert!(decode(Some(&json.to_string()), &fake.1).is_err());
    assert!(decode(Some("{"), &fake.1).is_err());
    assert!(decode(Some(&" ".repeat(32 * 1024 + 1)), &fake.1).is_err());
    assert_eq!(decode(None, &fake.1).unwrap(), None);
}
