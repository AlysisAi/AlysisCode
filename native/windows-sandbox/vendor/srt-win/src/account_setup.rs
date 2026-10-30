//! Conservative writes for an already-owned candidate installation. First-time
//! bootstrap creation lives in account_bootstrap; destructive account/WFP
//! removal remains outside this module. No write path adopts unknown state.

#[cfg(test)]
#[path = "account_setup_disposable_host_tests.rs"]
mod disposable_host_tests;

use anyhow::{Context, Result, ensure};
use serde::{Deserialize, Serialize};

use super::{
    AccountBinding, AccountMode, CredentialGenerationPlan, REGISTRY_ROOT, SCHEMA_VERSION,
    SetupPhase, SetupRecord,
};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum UpdatePurpose {
    Repair,
    Remove,
}

/// Every attempted generation is recorded before its key is created. Failed
/// writes leave an explicit inventory, rather than requiring guessed deletion.
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct CredentialReference {
    pub binding: AccountBinding,
    pub reference: String,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct CredentialLedger {
    version: u32,
    installation_id: String,
    owner_sid: String,
    entries: Vec<CredentialReference>,
}

impl CredentialLedger {
    fn from_record(record: &SetupRecord) -> Result<Self> {
        record.validate_layout()?;
        let mut entries = Vec::new();
        for mode in [AccountMode::Offline, AccountMode::Online] {
            if let Some(reference) = &record.account(mode)?.credential_generation {
                entries.push(CredentialReference {
                    binding: record.binding(mode)?,
                    reference: reference.clone(),
                });
            }
        }
        Ok(Self {
            version: 1,
            installation_id: record.installation_id.clone(),
            owner_sid: record.owner_sid.clone(),
            entries,
        })
    }

    fn validate(&self, record: &SetupRecord) -> Result<()> {
        record.validate_layout()?;
        ensure!(
            self.version == 1
                && self.installation_id == record.installation_id
                && self.owner_sid == record.owner_sid,
            "candidate credential ledger identity mismatch"
        );
        ensure!(
            self.entries.len() <= 4096,
            "candidate credential ledger requires explicit cleanup"
        );
        let mut unique = std::collections::BTreeSet::new();
        for entry in &self.entries {
            let mode = entry.binding.mode;
            ensure!(
                entry.binding == record.binding(mode)?,
                "candidate credential ledger principal mismatch"
            );
            let path = crate::account_credentials::generation_path(
                &record.installation_id,
                mode,
                &entry.reference,
            )?;
            ensure!(
                unique.insert(path),
                "duplicate candidate credential ledger entry"
            );
        }
        for mode in [AccountMode::Offline, AccountMode::Online] {
            if let Some(reference) = &record.account(mode)?.credential_generation {
                ensure!(
                    self.entries
                        .iter()
                        .any(|entry| entry.binding.mode == mode && entry.reference == *reference),
                    "selected candidate credential is absent from ledger"
                );
            }
        }
        Ok(())
    }
}

struct Snapshot {
    record: SetupRecord,
    marker: u32,
    ledger: CredentialLedger,
}

trait Store {
    fn write_marker(&mut self, value: u32) -> Result<()>;
    fn write_record(&mut self, record: &SetupRecord) -> Result<()>;
    fn write_ledger(&mut self, ledger: &CredentialLedger) -> Result<()>;
    fn write_generation(
        &mut self,
        binding: &AccountBinding,
        plan: &CredentialGenerationPlan,
    ) -> Result<()>;
    fn verify_generations(&self, record: &SetupRecord) -> Result<()>;
}

/// Pure transaction coordinator; all synthetic crash tests use an in-memory
/// Store. A failed mutation poisons this instance. Retrying requires reopening
/// and validating persisted state while holding a fresh maintenance lease.
struct Update<S: Store> {
    store: S,
    snapshot: Snapshot,
    poisoned: bool,
    staged: [bool; 2],
}

fn mode_index(mode: AccountMode) -> usize {
    match mode {
        AccountMode::Offline => 0,
        AccountMode::Online => 1,
    }
}

fn validate_authority(owner: &str, caller: &str, elevated: bool, pending: bool) -> Result<()> {
    ensure!(
        owner == caller,
        "candidate maintenance belongs to another Windows owner"
    );
    ensure!(
        elevated,
        "candidate maintenance requires consent elevation as its owner"
    );
    ensure!(
        !pending,
        "candidate maintenance refuses an unfinished sandbox execution"
    );
    Ok(())
}

impl<S: Store> Update<S> {
    fn begin(
        mut store: S,
        mut snapshot: Snapshot,
        purpose: UpdatePurpose,
        caller: &str,
    ) -> Result<Self> {
        snapshot.record.validate_layout()?;
        snapshot.ledger.validate(&snapshot.record)?;
        ensure!(
            snapshot.record.owner_sid == caller,
            "candidate update owner mismatch"
        );
        ensure!(
            snapshot.marker == 0 || snapshot.marker == SCHEMA_VERSION,
            "unsupported candidate setup marker"
        );
        if snapshot.marker == SCHEMA_VERSION {
            snapshot.record.validate_ready(snapshot.marker, caller)?;
        }
        ensure!(
            purpose == UpdatePurpose::Remove || snapshot.record.phase != SetupPhase::Removing,
            "candidate removal is unfinished; refusing repair/adoption"
        );
        // The first write invalidates readiness. No credential/account mutation
        // is permitted before this succeeds. The old SID ownership stays intact.
        store.write_marker(0)?;
        snapshot.marker = 0;
        snapshot.record.phase = match purpose {
            UpdatePurpose::Repair => SetupPhase::Provisioning,
            UpdatePurpose::Remove => SetupPhase::Removing,
        };
        store.write_record(&snapshot.record)?;
        // Also persist an inventory migrated from this exact owned record when
        // older candidate state did not yet have a ledger. No enumeration/adoption.
        store.write_ledger(&snapshot.ledger)?;
        Ok(Self {
            store,
            snapshot,
            poisoned: false,
            staged: [false; 2],
        })
    }

    fn ensure_repair(&self) -> Result<()> {
        ensure!(
            !self.poisoned,
            "candidate update failed; reopen validated state before retrying"
        );
        ensure!(
            self.snapshot.record.phase == SetupPhase::Provisioning,
            "candidate credential writes are forbidden during removal"
        );
        Ok(())
    }

    fn stage(&mut self, mode: AccountMode, plan: &CredentialGenerationPlan) -> Result<()> {
        self.ensure_repair()?;
        let binding = self.snapshot.record.binding(mode)?;
        plan.validate_for(&binding)?;
        let entry = CredentialReference {
            binding: binding.clone(),
            reference: plan.reference.clone(),
        };
        ensure!(
            !self.snapshot.ledger.entries.contains(&entry),
            "candidate credential generation is already recorded"
        );
        let mut ledger = self.snapshot.ledger.clone();
        ledger.entries.push(entry);
        ledger.validate(&self.snapshot.record)?;
        self.poisoned = true;
        self.store.write_ledger(&ledger)?;
        self.snapshot.ledger = ledger;
        self.store.write_generation(&binding, plan)?;
        let mut record = self.snapshot.record.clone();
        account_mut(&mut record, mode)?.credential_generation = Some(plan.reference.clone());
        self.store.write_record(&record)?;
        self.snapshot.record = record;
        self.staged[mode_index(mode)] = true;
        self.poisoned = false;
        Ok(())
    }

    fn ambient(&mut self, mode: AccountMode, paths: Vec<String>) -> Result<()> {
        self.ensure_repair()?;
        let mut record = self.snapshot.record.clone();
        account_mut(&mut record, mode)?.ambient_paths = paths;
        record.validate_layout()?;
        self.poisoned = true;
        self.store.write_record(&record)?;
        self.snapshot.record = record;
        self.poisoned = false;
        Ok(())
    }

    fn storage(&mut self, value: crate::account_state::StorageRecord) -> Result<()> {
        self.ensure_repair()?;
        value.validate_for(&self.snapshot.record)?;
        value.validate_transition(self.snapshot.record.storage.as_ref())?;
        let mut record = self.snapshot.record.clone();
        record.storage = Some(value);
        record.validate_layout()?;
        self.poisoned = true;
        self.store.write_record(&record)?;
        self.snapshot.record = record;
        self.poisoned = false;
        Ok(())
    }

    fn commit(mut self, verify: impl FnOnce(&SetupRecord) -> Result<()>) -> Result<()> {
        self.ensure_repair()?;
        ensure!(
            self.staged == [true, true],
            "both candidate accounts require fresh credentials before commit"
        );
        let mut ready = self.snapshot.record.clone();
        ready.phase = SetupPhase::Ready;
        ready.validate_ready(SCHEMA_VERSION, &ready.owner_sid)?;
        self.snapshot.ledger.validate(&ready)?;
        self.store.verify_generations(&ready)?;
        // The installer must prove completed password rotations, group/logon
        // policy, per-account ambient denies and offline WFP policy here. This
        // callback is trusted setup code, never a request from the sandbox.
        verify(&ready)?;
        self.store.write_record(&ready)?;
        // Last mutation is the sole readiness commit. No automatic cleanup of
        // retired generations follows; their exact references remain inventoried.
        self.store.write_marker(SCHEMA_VERSION)
    }
}

fn account_mut(record: &mut SetupRecord, mode: AccountMode) -> Result<&mut super::AccountRecord> {
    match mode {
        AccountMode::Offline => &mut record.offline,
        AccountMode::Online => &mut record.online,
    }
    .as_mut()
    .context("candidate account is not recorded")
}

struct RegistryStore {
    key: crate::reg::Key,
}

impl RegistryStore {
    fn open_owned(caller: &str, purpose: UpdatePurpose) -> Result<(Self, Snapshot)> {
        let key = crate::reg::Key::open(REGISTRY_ROOT, true)?
            .context("candidate ownership root is absent; candidate bootstrap is required")?;
        let record: SetupRecord =
            read_json(&key, "State")?.context("candidate ownership record is missing")?;
        record.validate_layout()?;
        ensure!(
            record.owner_sid == caller,
            "candidate store belongs to another owner"
        );
        let binding = record.binding(AccountMode::Offline)?;
        record.binding(AccountMode::Online)?;
        key.validate_credential_security(
            &binding.owner_sid,
            &binding.account_sid,
            &binding.group_sid,
        )?;
        super::bootstrap::validate_handoff(key.get_sz("Bootstrap")?.as_deref(), &record)?;
        let observed = [
            crate::user::local_group_sid(super::ACCOUNT_GROUP)?,
            crate::user::local_user_sid(super::OFFLINE_USER)?,
            crate::user::local_user_sid(super::ONLINE_USER)?,
        ];
        if purpose == UpdatePurpose::Remove && record.phase == SetupPhase::Removing {
            // Resume only this recorded removal. A missing owned principal may
            // have been deleted before the crash; a replacement is never touched.
            let recorded = [
                record.group.as_ref().map(|item| item.sid.as_str()),
                record.offline.as_ref().map(|item| item.sid.as_str()),
                record.online.as_ref().map(|item| item.sid.as_str()),
            ];
            for (prior, current) in recorded.into_iter().zip(&observed) {
                ensure!(
                    current.is_none() || current.as_deref() == prior,
                    "candidate removal encountered a replacement principal"
                );
            }
        } else {
            record.validate_observed_principals(
                observed[0].as_deref(),
                observed[1].as_deref(),
                observed[2].as_deref(),
            )?;
        }
        let marker = key
            .get_dword("MarkerVersion")?
            .context("candidate marker missing")?;
        let ledger =
            read_json(&key, "CredentialLedger")?.unwrap_or(CredentialLedger::from_record(&record)?);
        ledger.validate(&record)?;
        Ok((
            Self { key },
            Snapshot {
                record,
                marker,
                ledger,
            },
        ))
    }
}

fn read_json<T: serde::de::DeserializeOwned>(
    key: &crate::reg::Key,
    name: &str,
) -> Result<Option<T>> {
    key.get_sz(name)?
        .map(|text| {
            ensure!(
                text.len() <= 1024 * 1024,
                "candidate setup value exceeds size limit"
            );
            serde_json::from_str(&text).context("invalid candidate setup value")
        })
        .transpose()
}

impl Store for RegistryStore {
    fn write_marker(&mut self, value: u32) -> Result<()> {
        self.key.set_dword("MarkerVersion", value)?;
        self.key.flush()?;
        ensure!(
            self.key.get_dword("MarkerVersion")? == Some(value),
            "candidate marker write verification failed"
        );
        Ok(())
    }
    fn write_record(&mut self, record: &SetupRecord) -> Result<()> {
        write_json(&self.key, "State", record)
    }
    fn write_ledger(&mut self, ledger: &CredentialLedger) -> Result<()> {
        write_json(&self.key, "CredentialLedger", ledger)
    }
    fn write_generation(
        &mut self,
        binding: &AccountBinding,
        plan: &CredentialGenerationPlan,
    ) -> Result<()> {
        plan.write_fresh(binding)
    }
    fn verify_generations(&self, record: &SetupRecord) -> Result<()> {
        let offline = record.binding(AccountMode::Offline)?;
        self.key.validate_credential_security(
            &offline.owner_sid,
            &offline.account_sid,
            &offline.group_sid,
        )?;
        super::activation::wfp::require_complete(
            record,
            self.key.get_sz("WfpOwnership")?.as_deref(),
        )?;
        super::policy::require_complete(record, self.key.get_sz(super::policy::VALUE)?.as_deref())?;
        record.validate_observed_principals(
            crate::user::local_group_sid(super::ACCOUNT_GROUP)?.as_deref(),
            crate::user::local_user_sid(super::OFFLINE_USER)?.as_deref(),
            crate::user::local_user_sid(super::ONLINE_USER)?.as_deref(),
        )?;
        for mode in [AccountMode::Offline, AccountMode::Online] {
            let binding = record.binding(mode)?;
            crate::account_credentials::password::require_applied(
                record,
                mode,
                self.key
                    .get_sz(crate::account_credentials::password::value_name(mode))?
                    .as_deref(),
            )?;
            ensure!(
                crate::sam::is_member_of(&binding.group_sid, &binding.account_sid)?,
                "candidate account group membership is incomplete"
            );
            crate::account_credentials::verify_stored_generation(
                &binding,
                record
                    .account(mode)?
                    .credential_generation
                    .as_deref()
                    .context("candidate credential missing")?,
            )?;
        }
        Ok(())
    }
}

fn write_json(key: &crate::reg::Key, name: &str, value: &impl Serialize) -> Result<()> {
    let text = serde_json::to_string(value)?;
    ensure!(
        text.len() <= 1024 * 1024,
        "candidate setup value exceeds size limit"
    );
    key.set_sz(name, &text)?;
    key.flush()?;
    ensure!(
        key.get_sz(name)?.as_ref() == Some(&text),
        "candidate setup write verification failed"
    );
    Ok(())
}

/// Owns the shared execution lease until commit/drop, with no guessed rollback
/// or deletion. After invalidation, failures before the final marker write leave
/// MarkerVersion=0. The final marker can persist before its readback fails; that
/// is an uncertain commit outcome requiring validated state to be read again.
/// This does not create principals or change ambient ACLs. The private password
/// and offline-fence hooks use separate durable intents; neither enables an
/// account or makes an incomplete installation ready.
pub struct OwnedSetupUpdate {
    update: Update<RegistryStore>,
    storage: Option<crate::account_state::storage::StoragePins>,
    _lease: crate::execution_lease::ExecutionLease,
}

/// This authority cannot be constructed from a serialized record. Its only
/// constructor is the checked marker-zero method below, while its immutable
/// borrow keeps the owning maintenance lease and same registry handle alive.
pub(crate) struct PasswordUpdateAuthority<'a> {
    owner: &'a OwnedSetupUpdate,
    mode: AccountMode,
}

/// A policy writer must retain the entire elevated maintenance transaction and
/// shared lease. No serialized record, bare registry key or arbitrary callback
/// can construct this authority.
pub(super) struct PolicyUpdateAuthority<'a> {
    owner: &'a OwnedSetupUpdate,
}
impl PolicyUpdateAuthority<'_> {
    pub(super) fn record(&self) -> &SetupRecord {
        &self.owner.update.snapshot.record
    }
    pub(super) fn key(&self) -> &crate::reg::Key {
        &self.owner.update.store.key
    }
}
impl PasswordUpdateAuthority<'_> {
    pub(crate) fn record(&self) -> &SetupRecord {
        &self.owner.update.snapshot.record
    }
    pub(crate) fn mode(&self) -> AccountMode {
        self.mode
    }
    pub(crate) fn key(&self) -> &crate::reg::Key {
        &self.owner.update.store.key
    }
    pub(crate) fn generations(&self) -> std::collections::BTreeSet<String> {
        self.owner
            .update
            .snapshot
            .ledger
            .entries
            .iter()
            .filter(|entry| entry.binding.mode == self.mode)
            .map(|entry| entry.reference.clone())
            .collect()
    }
}

impl OwnedSetupUpdate {
    pub fn begin(purpose: UpdatePurpose) -> Result<Self> {
        let caller = crate::sid::current_user_sid()?;
        let elevated = is_elevated()?;
        // Preflight fails before touching installed state. The second check is
        // under the exact same lease as baseline execution and maintenance.
        validate_authority(&caller, &caller, elevated, false)?;
        let lease = crate::execution_lease::ExecutionLease::acquire()?;
        let pending = crate::account_state::pending_journal_path()?.try_exists()?;
        validate_authority(&caller, &caller, elevated, pending)?;
        crate::local_account_host::require_local_sam()?;
        let (store, snapshot) = RegistryStore::open_owned(&caller, purpose)?;
        validate_authority(&snapshot.record.owner_sid, &caller, elevated, pending)?;
        let update = Update::begin(store, snapshot, purpose, &caller)?;
        Ok(Self {
            update,
            storage: None,
            _lease: lease,
        })
    }

    pub fn record(&self) -> &SetupRecord {
        &self.update.snapshot.record
    }

    /// Exact known generation references only; no registry enumeration and no
    /// automatic deletion. Removal must keep this inventory until all referenced
    /// credentials, owned principals and policy are independently cleaned.
    pub fn credential_inventory(&self) -> &[CredentialReference] {
        &self.update.snapshot.ledger.entries
    }

    /// Call before password mutation. If rotation fails, the setup stays
    /// unready and the new credential's exact reference remains recorded.
    pub fn stage_credential(
        &mut self,
        mode: AccountMode,
        plan: &CredentialGenerationPlan,
    ) -> Result<()> {
        self.update.stage(mode, plan)
    }

    pub fn record_ambient_paths(&mut self, mode: AccountMode, paths: Vec<String>) -> Result<()> {
        self.update.ambient(mode, paths)
    }

    /// Apply only this update's fresh persisted credential to an exact disabled
    /// account. No enablement, flag repair, retry of an uncertain attempt or
    /// caller-provided effect callback is available.
    #[allow(dead_code)]
    pub(crate) fn apply_staged_password(&mut self, mode: AccountMode) -> Result<()> {
        self.update.ensure_repair()?;
        ensure!(
            self.update.staged[mode_index(mode)],
            "candidate password application requires a freshly staged generation"
        );
        let record = &self.update.snapshot.record;
        let key = &self.update.store.key;
        let binding = record.binding(AccountMode::Offline)?;
        validate_authority(
            &record.owner_sid,
            &crate::sid::current_user_sid()?,
            is_elevated()?,
            crate::account_state::pending_journal_path()?.try_exists()?,
        )?;
        crate::local_account_host::require_local_sam()?;
        key.validate_credential_security(
            &binding.owner_sid,
            &binding.account_sid,
            &binding.group_sid,
        )?;
        self.update.snapshot.ledger.validate(record)?;
        ensure!(
            key.get_dword("MarkerVersion")? == Some(0)
                && read_json::<SetupRecord>(key, "State")?.as_ref() == Some(record)
                && read_json::<CredentialLedger>(key, "CredentialLedger")?.as_ref()
                    == Some(&self.update.snapshot.ledger),
            "candidate password ownership, inventory or marker changed"
        );
        record.validate_observed_principals(
            crate::user::local_group_sid(super::ACCOUNT_GROUP)?.as_deref(),
            crate::user::local_user_sid(super::OFFLINE_USER)?.as_deref(),
            crate::user::local_user_sid(super::ONLINE_USER)?.as_deref(),
        )?;
        self.update.poisoned = true;
        crate::account_credentials::password::apply(&PasswordUpdateAuthority {
            owner: self,
            mode,
        })?;
        self.update.poisoned = false;
        Ok(())
    }

    /// Fixed additive policy for the exact disabled owned accounts. This does
    /// not enable accounts or repair/remove unexpected rights or memberships.
    #[allow(dead_code)]
    pub(crate) fn prepare_account_policy(&mut self) -> Result<()> {
        self.update.ensure_repair()?;
        let record = &self.update.snapshot.record;
        let key = &self.update.store.key;
        let binding = record.binding(AccountMode::Offline)?;
        validate_authority(
            &record.owner_sid,
            &crate::sid::current_user_sid()?,
            is_elevated()?,
            crate::account_state::pending_journal_path()?.try_exists()?,
        )?;
        crate::local_account_host::require_local_sam()?;
        key.validate_credential_security(
            &binding.owner_sid,
            &binding.account_sid,
            &binding.group_sid,
        )?;
        ensure!(
            key.get_dword("MarkerVersion")? == Some(0)
                && read_json::<SetupRecord>(key, "State")?.as_ref() == Some(record),
            "candidate ownership or marker changed before account policy update"
        );
        record.validate_observed_principals(
            crate::user::local_group_sid(super::ACCOUNT_GROUP)?.as_deref(),
            crate::user::local_user_sid(super::OFFLINE_USER)?.as_deref(),
            crate::user::local_user_sid(super::ONLINE_USER)?.as_deref(),
        )?;
        self.update.poisoned = true;
        super::policy::apply(&PolicyUpdateAuthority { owner: self })?;
        self.update.poisoned = false;
        Ok(())
    }

    /// Private installer building block. Not called by CLI/default setup.
    /// Marker zero, exact same-handle ownership and this update's shared lease
    /// must still hold. Uncertain registry/BFE outcomes poison the instance.
    #[allow(dead_code)]
    pub(crate) fn prepare_offline_fence(&mut self) -> Result<()> {
        self.update.ensure_repair()?;
        let record = &self.update.snapshot.record;
        let key = &self.update.store.key;
        let binding = record.binding(AccountMode::Offline)?;
        validate_authority(
            &record.owner_sid,
            &crate::sid::current_user_sid()?,
            is_elevated()?,
            crate::account_state::pending_journal_path()?.try_exists()?,
        )?;
        crate::local_account_host::require_local_sam()?;
        key.validate_credential_security(
            &binding.owner_sid,
            &binding.account_sid,
            &binding.group_sid,
        )?;
        ensure!(
            key.get_dword("MarkerVersion")? == Some(0)
                && read_json::<SetupRecord>(key, "State")?.as_ref() == Some(record),
            "candidate ownership/marker changed before offline WFP update"
        );
        record.validate_observed_principals(
            crate::user::local_group_sid(super::ACCOUNT_GROUP)?.as_deref(),
            crate::user::local_user_sid(super::OFFLINE_USER)?.as_deref(),
            crate::user::local_user_sid(super::ONLINE_USER)?.as_deref(),
        )?;
        self.update.poisoned = true;
        super::activation::wfp::prepare(record, key)?;
        self.update.poisoned = false;
        Ok(())
    }

    /// No account/process quiescence authority exists yet. Refuse before any
    /// policy or credential access; a setup lease alone cannot authorize this.
    #[allow(dead_code)]
    pub(crate) fn remove_offline_fence(&mut self) -> Result<()> {
        super::activation::wfp::refuse_removal()
    }

    /// Installer-only storage step, under this update's elevated-owner/shared
    /// lease guard. No account activation occurs. A pending intent is flushed
    /// before create-new; its exact root identity is flushed before readiness.
    /// A successful create followed by a failed record write is deliberately
    /// unadoptable on resume. Nothing is deleted on failure or Drop.
    pub fn prepare_storage(&mut self) -> Result<()> {
        self.update.ensure_repair()?;
        ensure!(
            self.storage.is_none(),
            "candidate storage is already pinned for this update"
        );
        if self
            .update
            .snapshot
            .record
            .storage
            .as_ref()
            .is_some_and(|value| value.is_complete())
        {
            self.storage = Some(crate::account_state::storage::StoragePins::open(
                &self.update.snapshot.record,
            )?);
            return Ok(());
        }
        let plan =
            crate::account_state::storage::PreparedStorage::prepare(&self.update.snapshot.record)?;
        self.update.storage(plan.intent().clone())?;
        // Mark poisoned BEFORE filesystem mutation: an uncertain create cannot
        // be retried through this instance, even if no result was returned.
        self.update.poisoned = true;
        let pins = plan.create()?;
        let attestation = pins.attestation().clone();
        self.storage = Some(pins);
        self.update.poisoned = false;
        self.update.storage(attestation)
    }

    /// Mandatory native account, credential, ambient-denial and WFP checks run
    /// around `verify`. The callback must additionally perform the contained
    /// behavioral qualification; a structural policy match does not prove it.
    /// An error after the final marker write does not prove the setup is unready:
    /// read validated state again under the shared lease before deciding to retry.
    pub fn commit_ready(self, verify: impl FnOnce(&SetupRecord) -> Result<()>) -> Result<()> {
        let pins = self
            .storage
            .context("candidate storage must be prepared and pinned before readiness")?;
        self.update.commit(|ready| {
            pins.verify(ready)?;
            super::activation::verify(ready)?;
            verify(ready)?;
            super::activation::verify(ready)?;
            pins.verify(ready)
        })
    }
}

pub(super) fn is_elevated() -> Result<bool> {
    use windows::Win32::{
        Foundation::HANDLE,
        Security::{GetTokenInformation, TOKEN_ELEVATION, TOKEN_QUERY, TokenElevation},
        System::Threading::{GetCurrentProcess, OpenProcessToken},
    };
    let mut handle = HANDLE::default();
    unsafe {
        OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut handle)?;
    }
    let token = crate::util::OwnedHandle(handle);
    let mut elevation = TOKEN_ELEVATION::default();
    let mut size = 0;
    unsafe {
        GetTokenInformation(
            token.raw(),
            TokenElevation,
            Some((&mut elevation as *mut TOKEN_ELEVATION).cast()),
            std::mem::size_of::<TOKEN_ELEVATION>() as u32,
            &mut size,
        )?;
    }
    Ok(elevation.TokenIsElevated != 0)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{cell::RefCell, collections::BTreeMap, rc::Rc};

    struct FakeState {
        marker: u32,
        record: SetupRecord,
        ledger: CredentialLedger,
        generations: BTreeMap<String, Vec<u8>>,
        events: Vec<String>,
        fail_at: Option<usize>,
        fail_readback_at: Option<usize>,
        writes: usize,
    }

    #[derive(Clone)]
    struct FakeStore(Rc<RefCell<FakeState>>);

    impl FakeStore {
        fn new(fail_at: Option<usize>) -> Self {
            let record = super::super::synthetic_ready();
            let ledger = CredentialLedger::from_record(&record).unwrap();
            Self(Rc::new(RefCell::new(FakeState {
                marker: SCHEMA_VERSION,
                record,
                ledger,
                generations: BTreeMap::new(),
                events: vec![],
                fail_at,
                fail_readback_at: None,
                writes: 0,
            })))
        }
        fn snapshot(&self) -> Snapshot {
            let state = self.0.borrow();
            Snapshot {
                record: state.record.clone(),
                marker: state.marker,
                ledger: state.ledger.clone(),
            }
        }
        fn mutate(
            &self,
            label: String,
            change: impl FnOnce(&mut FakeState) -> Result<()>,
        ) -> Result<()> {
            let mut state = self.0.borrow_mut();
            state.writes += 1;
            ensure!(
                state.fail_at != Some(state.writes),
                "synthetic crash before write {}",
                state.writes
            );
            change(&mut state)?;
            state.events.push(label);
            ensure!(
                state.fail_readback_at != Some(state.writes),
                "synthetic readback failure after write {}",
                state.writes
            );
            Ok(())
        }
        fn begin(&self, purpose: UpdatePurpose) -> Result<Update<Self>> {
            let snapshot = self.snapshot();
            let caller = snapshot.record.owner_sid.clone();
            Update::begin(self.clone(), snapshot, purpose, &caller)
        }
    }

    impl Store for FakeStore {
        fn write_marker(&mut self, value: u32) -> Result<()> {
            self.mutate(format!("marker:{value}"), |state| {
                state.marker = value;
                Ok(())
            })
        }
        fn write_record(&mut self, record: &SetupRecord) -> Result<()> {
            self.mutate(format!("state:{:?}", record.phase), |state| {
                state.record = record.clone();
                Ok(())
            })
        }
        fn write_ledger(&mut self, ledger: &CredentialLedger) -> Result<()> {
            self.mutate("ledger".into(), |state| {
                state.ledger = ledger.clone();
                Ok(())
            })
        }
        fn write_generation(
            &mut self,
            binding: &AccountBinding,
            plan: &CredentialGenerationPlan,
        ) -> Result<()> {
            plan.validate_for(binding)?;
            self.mutate(
                format!("credential:{}", binding.mode.directory()),
                |state| {
                    ensure!(
                        !state.generations.contains_key(&plan.path),
                        "synthetic existing credential object"
                    );
                    state
                        .generations
                        .insert(plan.path.clone(), plan.encrypted_blob.clone());
                    Ok(())
                },
            )
        }
        fn verify_generations(&self, record: &SetupRecord) -> Result<()> {
            let mut state = self.0.borrow_mut();
            for mode in [AccountMode::Offline, AccountMode::Online] {
                let path = crate::account_credentials::generation_path(
                    &record.installation_id,
                    mode,
                    record
                        .account(mode)?
                        .credential_generation
                        .as_deref()
                        .unwrap(),
                )?;
                ensure!(
                    state.generations.contains_key(&path),
                    "synthetic credential missing"
                );
            }
            state.events.push("verified:credentials".into());
            Ok(())
        }
    }

    fn plans(record: &SetupRecord) -> [CredentialGenerationPlan; 2] {
        [AccountMode::Offline, AccountMode::Online].map(|mode| {
            CredentialGenerationPlan::prepare(
                &record.binding(mode).unwrap(),
                "Synthetic-setup-password-123!",
            )
            .unwrap()
        })
    }

    #[test]
    fn candidate_setup_authority_rejects_foreign_unprivileged_and_pending() {
        assert!(validate_authority("owner", "owner", true, false).is_ok());
        assert!(validate_authority("owner", "other", true, false).is_err());
        assert!(validate_authority("owner", "owner", false, false).is_err());
        assert!(validate_authority("owner", "owner", true, true).is_err());
    }

    #[test]
    fn candidate_setup_commits_marker_last_after_both_fresh_generations() {
        let store = FakeStore::new(None);
        let before = store.snapshot().record;
        let [offline, online] = plans(&before);
        let mut update = store.begin(UpdatePurpose::Repair).unwrap();
        update.stage(AccountMode::Offline, &offline).unwrap();
        update.stage(AccountMode::Online, &online).unwrap();
        update.commit(|_| Ok(())).unwrap();
        let state = store.0.borrow();
        assert_eq!(state.events.first().unwrap(), "marker:0");
        assert_eq!(state.events.last().unwrap(), "marker:4");
        assert_eq!(
            state
                .events
                .iter()
                .filter(|entry| *entry == "marker:4")
                .count(),
            1
        );
        state
            .record
            .validate_ready(state.marker, &before.owner_sid)
            .unwrap();
        for mode in [AccountMode::Offline, AccountMode::Online] {
            assert_eq!(
                state.record.binding(mode).unwrap(),
                before.binding(mode).unwrap()
            );
            assert_ne!(
                state.record.account(mode).unwrap().credential_generation,
                before.account(mode).unwrap().credential_generation
            );
        }
        assert_eq!(state.ledger.entries.len(), 4);
    }

    #[test]
    fn candidate_setup_final_marker_readback_failure_retains_verified_commit() {
        let store = FakeStore::new(None);
        store.0.borrow_mut().fail_readback_at = Some(11);
        let before = store.snapshot().record;
        let [offline, online] = plans(&before);
        let mut update = store.begin(UpdatePurpose::Repair).unwrap();
        update.stage(AccountMode::Offline, &offline).unwrap();
        update.stage(AccountMode::Online, &online).unwrap();
        let result = update.commit(|ready| {
            let mut state = store.0.borrow_mut();
            assert_eq!(state.marker, 0);
            assert_eq!(state.record.phase, SetupPhase::Provisioning);
            assert_eq!(state.events.last().unwrap(), "verified:credentials");
            ready.validate_ready(SCHEMA_VERSION, &before.owner_sid)?;
            state.events.push("verified:policy".into());
            Ok(())
        });
        assert!(
            result
                .unwrap_err()
                .to_string()
                .contains("readback failure after write 11")
        );
        let state = store.0.borrow();
        // The caller received an error, but the exact fully verified commit is
        // durable in this model. Treating this as marker0 would be incorrect.
        state
            .record
            .validate_ready(state.marker, &before.owner_sid)
            .unwrap();
        assert!(state.events.ends_with(&[
            "verified:credentials".into(),
            "verified:policy".into(),
            "state:Ready".into(),
            "marker:4".into(),
        ]));
        state.ledger.validate(&state.record).unwrap();
        assert_eq!(state.ledger.entries.len(), 4);
        assert_eq!(state.generations.len(), 2);
        for (mode, plan) in [
            (AccountMode::Offline, offline),
            (AccountMode::Online, online),
        ] {
            let binding = before.binding(mode).unwrap();
            assert_eq!(state.record.binding(mode).unwrap(), binding);
            assert_eq!(
                state
                    .record
                    .account(mode)
                    .unwrap()
                    .credential_generation
                    .as_deref(),
                Some(plan.reference.as_str())
            );
            assert!(state.ledger.entries.contains(&CredentialReference {
                binding,
                reference: plan.reference.clone(),
            }));
            assert_eq!(state.generations[&plan.path], plan.encrypted_blob);
        }
    }

    #[test]
    fn candidate_setup_every_interrupted_write_keeps_ownership_and_known_credentials() {
        let before = super::super::synthetic_ready();
        let [offline, online] = plans(&before);
        // 3 begin writes + 3 writes/account + ready state + final marker.
        for failed_write in 1..=11 {
            let store = FakeStore::new(Some(failed_write));
            let result = (|| -> Result<()> {
                let mut update = store.begin(UpdatePurpose::Repair)?;
                update.stage(AccountMode::Offline, &offline)?;
                update.stage(AccountMode::Online, &online)?;
                update.commit(|_| Ok(()))
            })();
            assert!(
                result.is_err(),
                "write {failed_write} unexpectedly completed"
            );
            let state = store.0.borrow();
            assert_eq!(
                state.marker,
                if failed_write == 1 { SCHEMA_VERSION } else { 0 }
            );
            assert!(!state.events.iter().any(|event| event == "marker:4"));
            for mode in [AccountMode::Offline, AccountMode::Online] {
                assert_eq!(
                    state.record.binding(mode).unwrap(),
                    before.binding(mode).unwrap()
                );
            }
            state.ledger.validate(&state.record).unwrap();
            for path in state.generations.keys() {
                assert!(state.ledger.entries.iter().any(|entry| {
                    crate::account_credentials::generation_path(
                        &before.installation_id,
                        entry.binding.mode,
                        &entry.reference,
                    )
                    .unwrap()
                        == *path
                }));
            }
        }
    }

    #[test]
    fn candidate_setup_storage_intent_result_and_ready_crash_ordering() {
        // Actual Update transitions with synthetic persistence and a modeled
        // create-new result. Real creation/cold-reopen ambiguity is separately
        // covered by account_storage's owned-directory fixtures.
        for after_write in [false, true] {
            for failed_write in 1..=13 {
                let store = FakeStore::new(None);
                {
                    let mut state = store.0.borrow_mut();
                    state.marker = 0;
                    state.record.phase = SetupPhase::Provisioning;
                    state.record.storage = None;
                    if after_write {
                        state.fail_readback_at = Some(failed_write);
                    } else {
                        state.fail_at = Some(failed_write);
                    }
                }
                let original = store.snapshot().record;
                let [offline, online] = plans(&original);
                let intent = crate::account_state::storage::synthetic_intent(&original);
                let complete = crate::account_state::storage::synthetic_attestation(&original);
                let mut created = false;
                let result = (|| -> Result<()> {
                    let mut update = store.begin(UpdatePurpose::Repair)?;
                    update.stage(AccountMode::Offline, &offline)?;
                    update.stage(AccountMode::Online, &online)?;
                    update.storage(intent.clone())?;
                    assert_eq!(store.0.borrow().record.storage.as_ref(), Some(&intent));
                    assert_eq!(store.0.borrow().marker, 0);
                    created = true;
                    store.0.borrow_mut().events.push("created:storage".into());
                    update.storage(complete.clone())?;
                    update.commit(|ready| {
                        assert_eq!(ready.storage.as_ref(), Some(&complete));
                        assert_eq!(store.0.borrow().record.storage.as_ref(), Some(&complete));
                        assert_eq!(store.0.borrow().marker, 0);
                        store
                            .0
                            .borrow_mut()
                            .events
                            .push("verified:storage-and-policy".into());
                        Ok(())
                    })
                })();
                assert!(
                    result.is_err(),
                    "missing injection {failed_write}, after={after_write}"
                );
                let state = store.0.borrow();
                for mode in [AccountMode::Offline, AccountMode::Online] {
                    assert_eq!(
                        state.record.binding(mode).unwrap(),
                        original.binding(mode).unwrap()
                    );
                }
                if created {
                    assert!(state.record.storage.is_some());
                }
                if state
                    .record
                    .storage
                    .as_ref()
                    .is_some_and(|value| value.is_complete())
                {
                    assert!(created);
                }
                if state.marker == SCHEMA_VERSION {
                    assert!(after_write && failed_write == 13 && created);
                    assert!(state.events.ends_with(&[
                        "verified:storage-and-policy".into(),
                        "state:Ready".into(),
                        "marker:4".into()
                    ]));
                    state
                        .record
                        .validate_ready(SCHEMA_VERSION, &original.owner_sid)
                        .unwrap();
                } else {
                    assert_eq!(state.marker, 0);
                }
            }
        }
    }

    #[test]
    fn candidate_setup_ready_refuses_absent_pending_or_replaced_storage_attestation() {
        for pending in [false, true] {
            let store = FakeStore::new(None);
            let before = store.snapshot().record;
            let [offline, online] = plans(&before);
            let mut update = store.begin(UpdatePurpose::Repair).unwrap();
            update.snapshot.record.storage = if pending {
                Some(crate::account_state::storage::synthetic_intent(&before))
            } else {
                None
            };
            update.stage(AccountMode::Offline, &offline).unwrap();
            update.stage(AccountMode::Online, &online).unwrap();
            assert!(
                update
                    .commit(|_| panic!("policy callback reached before complete storage"))
                    .is_err()
            );
            assert_eq!(store.0.borrow().marker, 0);
        }
        let store = FakeStore::new(None);
        let before = store.snapshot().record;
        let mut update = store.begin(UpdatePurpose::Repair).unwrap();
        assert!(
            update
                .storage(crate::account_state::storage::synthetic_intent(&before))
                .is_err()
        );
        assert_eq!(store.0.borrow().record.storage, before.storage);
    }

    #[test]
    fn candidate_setup_failed_stage_poisoned_and_failed_verification_unready() {
        let store = FakeStore::new(Some(5));
        let [offline, online] = plans(&store.snapshot().record);
        let mut update = store.begin(UpdatePurpose::Repair).unwrap();
        assert!(update.stage(AccountMode::Offline, &offline).is_err());
        assert!(update.stage(AccountMode::Online, &online).is_err());
        assert!(update.commit(|_| Ok(())).is_err());
        assert_eq!(store.0.borrow().marker, 0);

        let store = FakeStore::new(None);
        let mut update = store.begin(UpdatePurpose::Repair).unwrap();
        update.stage(AccountMode::Offline, &offline).unwrap();
        update.stage(AccountMode::Online, &online).unwrap();
        assert!(
            update
                .commit(|_| anyhow::bail!("synthetic WFP verification failure"))
                .is_err()
        );
        assert_eq!(store.0.borrow().marker, 0);
        assert_eq!(store.0.borrow().record.phase, SetupPhase::Provisioning);
    }

    #[test]
    fn candidate_setup_refuses_cross_account_plans_existing_keys_and_ledger_rebinding() {
        let store = FakeStore::new(None);
        let [offline, online] = plans(&store.snapshot().record);
        let mut update = store.begin(UpdatePurpose::Repair).unwrap();
        assert!(update.stage(AccountMode::Offline, &online).is_err());
        store
            .0
            .borrow_mut()
            .generations
            .insert(offline.path.clone(), vec![1, 2, 3]);
        assert!(update.stage(AccountMode::Offline, &offline).is_err());
        assert_eq!(store.0.borrow().generations[&offline.path], [1, 2, 3]);
        assert_eq!(store.0.borrow().marker, 0);

        let store = FakeStore::new(None);
        store.0.borrow_mut().ledger.entries[0].binding.account_sid = "S-1-5-21-1-2-3-9000".into();
        assert!(store.begin(UpdatePurpose::Repair).is_err());
        assert!(store.0.borrow().events.is_empty());
    }

    #[test]
    fn candidate_setup_removal_only_invalidates_and_preserves_exact_inventory() {
        let store = FakeStore::new(None);
        let before = store.snapshot();
        let [offline, _] = plans(&before.record);
        let mut removal = store.begin(UpdatePurpose::Remove).unwrap();
        assert!(removal.stage(AccountMode::Offline, &offline).is_err());
        assert!(removal.commit(|_| Ok(())).is_err());
        let state = store.0.borrow();
        assert_eq!(state.marker, 0);
        assert_eq!(state.record.phase, SetupPhase::Removing);
        assert_eq!(state.ledger, before.ledger);
        assert!(state.generations.is_empty());
        drop(state);
        let writes = store.0.borrow().writes;
        assert!(store.begin(UpdatePurpose::Repair).is_err());
        assert_eq!(store.0.borrow().writes, writes);
    }
}
