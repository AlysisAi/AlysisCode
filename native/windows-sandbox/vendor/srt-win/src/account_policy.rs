//! Fixed additive policy for the two owned disabled candidate accounts.
//! Every machine effect needs a durable exact-identity intent. Unknown,
//! conflicting or interrupted policy is preserved; this module never removes
//! rights/memberships, enables an account, or exposes a CLI entry point.

use super::{AccountBinding, AccountMode, SetupRecord, activation};
use anyhow::{Context, Result, ensure};
use serde::{Deserialize, Serialize};
use std::collections::BTreeSet;
use windows::Win32::NetworkManagement::NetManagement::*;

pub(super) const VALUE: &str = "AccountPolicyOwnership";

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Identity {
    offline: AccountBinding,
    online: AccountBinding,
}
impl Identity {
    fn selected(record: &SetupRecord) -> Result<Self> {
        Ok(Self {
            offline: record.binding(AccountMode::Offline)?,
            online: record.binding(AccountMode::Online)?,
        })
    }
    fn binding(&self, mode: AccountMode) -> &AccountBinding {
        match mode {
            AccountMode::Offline => &self.offline,
            AccountMode::Online => &self.online,
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Account {
    sid: String,
    flags: u32,
    privilege: u32,
    operator_flags: u32,
    password_expired: u32,
    groups: BTreeSet<String>,
    rights: BTreeSet<String>,
}
impl Account {
    fn validate_disabled(&self, binding: &AccountBinding) -> Result<()> {
        let required =
            UF_NORMAL_ACCOUNT | UF_SCRIPT.0 | UF_DONT_EXPIRE_PASSWD.0 | UF_ACCOUNTDISABLE.0;
        // A newly created local account without BUILTIN Users membership can
        // report USER_PRIV_GUEST. Observe the membership and its derived value
        // together; never accept an administrator, foreign group, or an owned
        // membership that skipped the preceding Users step.
        let users = self.groups.contains(activation::USERS);
        let expected_privilege = if users {
            USER_PRIV_USER
        } else {
            USER_PRIV_GUEST
        };
        ensure!(
            self.sid == binding.account_sid
                && self.flags & required == required
                && self.flags & !(required | UF_PASSWD_CANT_CHANGE.0) == 0
                && self.privilege == expected_privilege.0
                && self.operator_flags == 0
                && self.password_expired == 0,
            "candidate policy target is not the exact disabled ordinary account"
        );
        ensure!(
            self.groups.is_subset(&BTreeSet::from([
                activation::USERS.to_owned(),
                binding.group_sid.clone(),
            ])) && (users || !self.groups.contains(&binding.group_sid)),
            "candidate policy account has unexpected group membership"
        );
        ensure!(
            self.rights.is_empty(),
            "candidate policy account has direct LSA rights"
        );
        Ok(())
    }
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Snapshot {
    group_sid: String,
    group_members: BTreeSet<String>,
    group_rights: BTreeSet<String>,
    offline: Account,
    online: Account,
}
impl Snapshot {
    fn account(&self, mode: AccountMode) -> &Account {
        match mode {
            AccountMode::Offline => &self.offline,
            AccountMode::Online => &self.online,
        }
    }
    fn account_mut(&mut self, mode: AccountMode) -> &mut Account {
        match mode {
            AccountMode::Offline => &mut self.offline,
            AccountMode::Online => &mut self.online,
        }
    }
    fn validate_initial(&self, identity: &Identity) -> Result<()> {
        ensure!(
            self.group_sid == identity.offline.group_sid
                && self.group_members.is_empty()
                && self.group_rights.is_empty(),
            "candidate policy refuses unrecorded group memberships or rights"
        );
        for mode in [AccountMode::Offline, AccountMode::Online] {
            let account = self.account(mode);
            account.validate_disabled(identity.binding(mode))?;
            ensure!(
                account
                    .groups
                    .is_subset(&BTreeSet::from([activation::USERS.to_owned()])),
                "candidate policy account has unrecorded group membership"
            );
        }
        Ok(())
    }
    fn apply(&mut self, effect: Effect, identity: &Identity) {
        match effect {
            Effect::Denials => {
                self.group_rights = activation::DENIALS.into_iter().map(str::to_owned).collect()
            }
            Effect::Users(mode) => {
                let account = self.account_mut(mode);
                account.groups.insert(activation::USERS.into());
                account.privilege = USER_PRIV_USER.0;
            }
            Effect::Owned(mode) => {
                self.group_members
                    .insert(identity.binding(mode).account_sid.clone());
                let group = self.group_sid.clone();
                self.account_mut(mode).groups.insert(group);
            }
        }
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Effect {
    Denials,
    Users(AccountMode),
    Owned(AccountMode),
}
fn effects(initial: &Snapshot) -> Vec<Effect> {
    // Denials precede every new membership. Users may be assigned by Windows at
    // account creation; that exact pre-existing state is recorded, never removed.
    let mut result = vec![Effect::Denials];
    for mode in [AccountMode::Offline, AccountMode::Online] {
        if !initial.account(mode).groups.contains(activation::USERS) {
            result.push(Effect::Users(mode));
        }
        result.push(Effect::Owned(mode));
    }
    result
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Receipt {
    version: u32,
    identity: Identity,
    initial: Snapshot,
    completed: usize,
    pending: bool,
}
impl Receipt {
    fn new(identity: &Identity, initial: Snapshot) -> Result<Self> {
        initial.validate_initial(identity)?;
        Ok(Self {
            version: 1,
            identity: identity.clone(),
            initial,
            completed: 0,
            pending: false,
        })
    }
    fn validate(&self, selected: &Identity) -> Result<()> {
        ensure!(
            self.version == 1 && self.identity == *selected,
            "candidate policy receipt belongs to another installation or principal"
        );
        self.initial.validate_initial(selected)?;
        let count = effects(&self.initial).len();
        ensure!(
            self.completed <= count && (!self.pending || self.completed < count),
            "candidate policy receipt has an invalid progress boundary"
        );
        Ok(())
    }
    fn expected(&self) -> Snapshot {
        let mut expected = self.initial.clone();
        for effect in effects(&self.initial).into_iter().take(self.completed) {
            expected.apply(effect, &self.identity);
        }
        expected
    }
    fn complete(&self) -> bool {
        !self.pending && self.completed == effects(&self.initial).len()
    }
}

fn decode(text: Option<&str>, identity: &Identity) -> Result<Option<Receipt>> {
    text.map(|text| {
        ensure!(
            text.len() <= 32 * 1024,
            "candidate policy receipt exceeds bound"
        );
        let receipt: Receipt = serde_json::from_str(text)?;
        receipt.validate(identity)?;
        Ok(receipt)
    })
    .transpose()
}
pub(super) fn require_complete(record: &SetupRecord, text: Option<&str>) -> Result<()> {
    let identity = Identity::selected(record)?;
    ensure!(
        decode(text, &identity)?.is_some_and(|value| value.complete()),
        "candidate account policy ownership is missing or incomplete"
    );
    Ok(())
}

trait Store {
    fn read(&self, identity: &Identity) -> Result<Option<Receipt>>;
    fn write(&mut self, value: &Receipt) -> Result<()>;
}
trait Policy {
    fn observe(&mut self) -> Result<Snapshot>;
    fn apply(&mut self, effect: Effect) -> Result<()>;
}
fn prepare(store: &mut impl Store, policy: &mut impl Policy, identity: &Identity) -> Result<()> {
    let mut receipt = if let Some(value) = store.read(identity)? {
        value.validate(identity)?;
        ensure!(
            !value.pending,
            "candidate policy mutation outcome is uncertain; preserve pending intent without retry"
        );
        value
    } else {
        let value = Receipt::new(identity, policy.observe()?)?;
        store.write(&value)?;
        value
    };
    ensure!(
        policy.observe()? == receipt.expected(),
        "candidate policy differs from its exact recorded completed prefix"
    );
    let plan = effects(&receipt.initial);
    while receipt.completed < plan.len() {
        let before = receipt.expected();
        ensure!(
            policy.observe()? == before,
            "candidate policy changed before intent"
        );
        receipt.pending = true;
        store.write(&receipt)?;
        ensure!(
            policy.observe()? == before,
            "candidate policy changed after durable intent"
        );
        policy
            .apply(plan[receipt.completed])
            .context("candidate policy outcome may be uncertain; preserve pending intent")?;
        let mut after = before;
        after.apply(plan[receipt.completed], identity);
        ensure!(
            policy.observe()? == after,
            "candidate policy differs after native mutation"
        );
        receipt.completed += 1;
        receipt.pending = false;
        store.write(&receipt)?;
    }
    Ok(())
}
struct RegistryStore<'a>(&'a crate::reg::Key);
impl Store for RegistryStore<'_> {
    fn read(&self, identity: &Identity) -> Result<Option<Receipt>> {
        decode(self.0.get_sz(VALUE)?.as_deref(), identity)
    }
    fn write(&mut self, value: &Receipt) -> Result<()> {
        let text = serde_json::to_string(value)?;
        ensure!(
            text.len() <= 32 * 1024,
            "candidate policy receipt exceeds bound"
        );
        self.0.set_sz(VALUE, &text)?;
        self.0.flush()?;
        ensure!(
            self.0.get_sz(VALUE)?.as_ref() == Some(&text),
            "candidate policy receipt readback failed; preserve uncertain outcome"
        );
        Ok(())
    }
}

#[path = "account_policy_native.rs"]
mod native;
pub(super) fn apply(authority: &super::setup::PolicyUpdateAuthority<'_>) -> Result<()> {
    let identity = Identity::selected(authority.record())?;
    prepare(
        &mut RegistryStore(authority.key()),
        &mut native::NativePolicy::new(identity.clone()),
        &identity,
    )
}

#[cfg(test)]
#[path = "account_policy_tests.rs"]
mod tests;
