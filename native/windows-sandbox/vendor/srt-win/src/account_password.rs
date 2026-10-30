//! Private disabled-account password application. The native entry requires
//! opaque, lease-owning setup authority; it cannot be called with a record or
//! a password alone. No account enablement or ambiguous-attempt retry exists.

use super::{Credential, SecretWide, read_stored_credential};
use crate::account_install::{AccountBinding, AccountMode, PasswordUpdateAuthority, SetupRecord};
use anyhow::{Context, Result, ensure};
use serde::{Deserialize, Serialize};
use std::collections::BTreeSet;
use windows::Win32::NetworkManagement::NetManagement::*;
use windows::core::{PCWSTR, PWSTR};

pub(crate) fn value_name(mode: AccountMode) -> &'static str {
    match mode {
        AccountMode::Offline => "OfflinePasswordUpdate",
        AccountMode::Online => "OnlinePasswordUpdate",
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Identity {
    binding: AccountBinding,
    peer_sid: String,
    generation: String,
}
impl Identity {
    fn selected(record: &SetupRecord, mode: AccountMode) -> Result<Self> {
        let value = Self {
            binding: record.binding(mode)?,
            peer_sid: record
                .binding(match mode {
                    AccountMode::Offline => AccountMode::Online,
                    AccountMode::Online => AccountMode::Offline,
                })?
                .account_sid,
            generation: record
                .account(mode)?
                .credential_generation
                .clone()
                .context("candidate password generation is missing")?,
        };
        value.validate_family(&value)?;
        Ok(value)
    }
    fn validate_family(&self, selected: &Self) -> Result<()> {
        self.binding.validate()?;
        super::generation_path(
            &self.binding.installation_id,
            self.binding.mode,
            &self.generation,
        )?;
        ensure!(
            self.binding == selected.binding && self.peer_sid == selected.peer_sid,
            "candidate password record belongs to another installation or account"
        );
        Ok(())
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
enum Phase {
    Pending,
    Applied,
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Record {
    version: u32,
    identity: Identity,
    phase: Phase,
}
impl Record {
    fn new(identity: &Identity, phase: Phase) -> Self {
        Self {
            version: 1,
            identity: identity.clone(),
            phase,
        }
    }
    fn validate(&self, selected: &Identity) -> Result<()> {
        ensure!(self.version == 1, "unsupported candidate password record");
        self.identity.validate_family(selected)
    }
}
fn decode(text: Option<&str>, selected: &Identity) -> Result<Option<Record>> {
    text.map(|text| {
        ensure!(
            text.len() <= 16 * 1024,
            "candidate password record exceeds bound"
        );
        let record: Record = serde_json::from_str(text)?;
        record.validate(selected)?;
        Ok(record)
    })
    .transpose()
}

pub(crate) fn require_applied(
    record: &SetupRecord,
    mode: AccountMode,
    text: Option<&str>,
) -> Result<()> {
    let selected = Identity::selected(record, mode)?;
    ensure!(
        decode(text, &selected)?
            .is_some_and(|value| value.phase == Phase::Applied && value.identity == selected),
        "candidate selected password has no completed native application"
    );
    Ok(())
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct Account {
    sid: String,
    flags: u32,
    privilege: u32,
    operator_flags: u32,
    password_expired: u32,
}
impl Account {
    fn validate(&self, binding: &AccountBinding) -> Result<()> {
        binding.validate()?;
        let required =
            UF_NORMAL_ACCOUNT | UF_SCRIPT.0 | UF_DONT_EXPIRE_PASSWD.0 | UF_ACCOUNTDISABLE.0;
        let allowed = required | UF_PASSWD_CANT_CHANGE.0;
        ensure!(
            self.sid == binding.account_sid
                && self.flags & required == required
                && self.flags & !allowed == 0
                && self.privilege == USER_PRIV_USER.0
                && self.operator_flags == 0
                && self.password_expired == 0,
            "candidate password target is not the exact disabled ordinary account"
        );
        Ok(())
    }
}

trait Store {
    fn read(&self, selected: &Identity) -> Result<Option<Record>>;
    fn write(&mut self, record: &Record) -> Result<()>;
}
trait Sam {
    fn observe(&mut self) -> Result<Account>;
    fn prepare_credential(&mut self) -> Result<()>;
    fn set_password(&mut self) -> Result<()>;
}

fn apply_selected(
    store: &mut impl Store,
    sam: &mut impl Sam,
    selected: &Identity,
    inventory: &BTreeSet<String>,
) -> Result<()> {
    ensure!(
        inventory.contains(&selected.generation),
        "selected password is absent from inventory"
    );
    let prior = store.read(selected)?;
    if let Some(prior) = &prior {
        prior.validate(selected)?;
        ensure!(
            inventory.contains(&prior.identity.generation),
            "prior password is absent from inventory"
        );
        // There is no safe disabled-account authentication probe that proves
        // whether an interrupted password setter committed. Do not interpret
        // ERROR_ACCOUNT_DISABLED as credential proof or retry a pending call.
        ensure!(
            prior.phase != Phase::Pending,
            "candidate password outcome is uncertain; preserve pending generation without retry"
        );
    }
    let before = sam.observe()?;
    before.validate(&selected.binding)?;
    if prior
        .as_ref()
        .is_some_and(|value| value.identity == *selected)
    {
        return Ok(()); // Same completed record; no secret load or second setter.
    }
    sam.prepare_credential()?;
    store.write(&Record::new(selected, Phase::Pending))?;
    ensure!(
        sam.observe()? == before,
        "candidate password target changed before mutation"
    );
    sam.set_password()
        .context("candidate password outcome may be uncertain; preserve pending generation")?;
    ensure!(
        sam.observe()? == before,
        "candidate password target changed after mutation"
    );
    // SAM and registry are not one transaction. If this persists but readback
    // fails, the result may already be Applied; setup still has marker zero.
    store.write(&Record::new(selected, Phase::Applied))
}

struct RegistryStore<'a> {
    key: &'a crate::reg::Key,
    mode: AccountMode,
}
impl Store for RegistryStore<'_> {
    fn read(&self, selected: &Identity) -> Result<Option<Record>> {
        decode(self.key.get_sz(value_name(self.mode))?.as_deref(), selected)
    }
    fn write(&mut self, record: &Record) -> Result<()> {
        let text = serde_json::to_string(record)?;
        ensure!(
            text.len() <= 16 * 1024,
            "candidate password record exceeds bound"
        );
        self.key.set_sz(value_name(self.mode), &text)?;
        self.key.flush()?;
        ensure!(
            self.key.get_sz(value_name(self.mode))?.as_ref() == Some(&text),
            "candidate password record readback failed; preserve uncertain outcome"
        );
        Ok(())
    }
}

struct NetBuffer(*mut u8);
impl Drop for NetBuffer {
    fn drop(&mut self) {
        if !self.0.is_null() {
            unsafe {
                let _ = NetApiBufferFree(Some(self.0.cast()));
            }
        }
    }
}
fn read_account(binding: &AccountBinding) -> Result<Account> {
    use crate::util::{pcwstr, wstr};
    let name = wstr(&binding.account_name);
    let mut raw = std::ptr::null_mut();
    let code = unsafe { NetUserGetInfo(PCWSTR::null(), pcwstr(&name), 4, &mut raw) };
    let raw = NetBuffer(raw);
    ensure!(
        code == 0 && !raw.0.is_null(),
        "observe candidate password target: {code}"
    );
    // Level 4 reports SID, flags and privilege in the same native result.
    // Its password field is deliberately neither read nor exposed.
    account_from_info(unsafe { &*raw.0.cast::<USER_INFO_4>() })
}
fn account_from_info(info: &USER_INFO_4) -> Result<Account> {
    Ok(Account {
        sid: crate::sid::psid_to_string(info.usri4_user_sid)?,
        flags: info.usri4_flags.0,
        privilege: info.usri4_priv.0,
        operator_flags: info.usri4_auth_flags.0,
        password_expired: info.usri4_password_expired,
    })
}
struct NativeSam {
    selected: Identity,
    credential: Option<Credential>,
}
impl Sam for NativeSam {
    fn observe(&mut self) -> Result<Account> {
        read_account(&self.selected.binding)
    }
    fn prepare_credential(&mut self) -> Result<()> {
        self.credential = Some(read_stored_credential(
            &self.selected.binding,
            &self.selected.generation,
        )?);
        Ok(())
    }
    fn set_password(&mut self) -> Result<()> {
        use crate::util::{pcwstr, wstr};
        let credential = self
            .credential
            .as_ref()
            .context("candidate password was not prepared")?;
        ensure!(
            credential.user == self.selected.binding.account_name,
            "candidate password name changed"
        );
        let name = wstr(&credential.user);
        let mut password = SecretWide(wstr(&credential.password));
        let info = USER_INFO_1003 {
            usri1003_password: PWSTR(password.0.as_mut_ptr()),
        };
        // Name-based SAM APIs do not provide an atomic compare-SID-and-set.
        // Exact pre/post identity checks run under setup's lease; concurrent
        // hostile administrators are outside the candidate's trust boundary.
        let code = unsafe {
            NetUserSetInfo(
                PCWSTR::null(),
                pcwstr(&name),
                1003,
                (&info as *const USER_INFO_1003).cast(),
                None,
            )
        };
        ensure!(code == 0, "apply candidate password: {code}");
        Ok(())
    }
}

pub(crate) fn apply(authority: &PasswordUpdateAuthority<'_>) -> Result<()> {
    let selected = Identity::selected(authority.record(), authority.mode())?;
    let mut store = RegistryStore {
        key: authority.key(),
        mode: authority.mode(),
    };
    let mut sam = NativeSam {
        selected: selected.clone(),
        credential: None,
    };
    apply_selected(&mut store, &mut sam, &selected, &authority.generations())
}

#[cfg(test)]
#[path = "account_password_tests.rs"]
mod tests;
