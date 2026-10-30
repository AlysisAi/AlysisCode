//! Candidate-only setup schema and guarded, disabled-account bootstrap.
//! It does not activate accounts, change WFP, or promote a backend.
//! The sibling registry root and distinct SAM principals survive baseline removal.

use anyhow::{Context, Result, ensure};
use serde::{Deserialize, Serialize};
use std::collections::BTreeSet;

pub use crate::account_credentials::CredentialGenerationPlan;
#[path = "account_setup.rs"]
mod setup;
pub(crate) use setup::PasswordUpdateAuthority;
pub use setup::{CredentialReference, OwnedSetupUpdate, UpdatePurpose};
#[path = "account_bootstrap.rs"]
mod bootstrap;
pub use bootstrap::FirstInstall;
#[path = "account_activation.rs"]
mod activation;
#[path = "account_policy.rs"]
mod policy;

pub const REGISTRY_ROOT: &str = r"SOFTWARE\alysis-sandbox-account-v4";
pub const SCHEMA_VERSION: u32 = 4;
pub const OFFLINE_USER: &str = "alysis-ac-off";
pub const ONLINE_USER: &str = "alysis-ac-on";
pub const ACCOUNT_GROUP: &str = "alysis-ac-users";
pub const OFFLINE_SUBLAYER: windows::core::GUID =
    windows::core::GUID::from_u128(0x75e2d07e_1886_4f30_a3b6_5ba74d6b6717);

#[derive(Clone, Copy, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum AccountMode {
    Offline,
    Online,
}

impl AccountMode {
    pub fn name(self) -> &'static str {
        match self {
            Self::Offline => OFFLINE_USER,
            Self::Online => ONLINE_USER,
        }
    }
    pub fn directory(self) -> &'static str {
        match self {
            Self::Offline => "offline",
            Self::Online => "online",
        }
    }
}

impl From<crate::appcontainer::NetworkAccess> for AccountMode {
    fn from(value: crate::appcontainer::NetworkAccess) -> Self {
        match value {
            crate::appcontainer::NetworkAccess::Off => Self::Offline,
            crate::appcontainer::NetworkAccess::On => Self::Online,
        }
    }
}

#[derive(Clone, Copy, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum SetupPhase {
    Provisioning,
    Ready,
    Removing,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct GroupRecord {
    pub name: String,
    pub sid: String,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct AccountRecord {
    pub mode: AccountMode,
    pub name: String,
    pub sid: String,
    /// None until a fresh, identity-bound encrypted generation is persisted.
    pub credential_generation: Option<String>,
    /// Per-account lists; never share baseline install::AmbientDenies tracking.
    pub ambient_paths: Vec<String>,
}

/// Persist as one admin-written JSON value, with MarkerVersion written last.
/// During repair invalidate readiness before any password/account mutation;
/// retain all prior SID ownership records throughout a failed attempt.
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct SetupRecord {
    pub schema: u32,
    pub installation_id: String,
    pub owner_sid: String,
    pub phase: SetupPhase,
    pub group: Option<GroupRecord>,
    pub offline: Option<AccountRecord>,
    pub online: Option<AccountRecord>,
    /// Absent before installer provisioning; a pending create intent is never
    /// sufficient for readiness. No implicit LocalAppData storage fallback.
    #[serde(default)]
    pub storage: Option<crate::account_state::StorageRecord>,
}

/// Immutable identity copied into credential payloads, per-account database
/// headers and execution journals. Credential rotation does not change it.
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct AccountBinding {
    pub installation_id: String,
    pub owner_sid: String,
    pub mode: AccountMode,
    pub account_name: String,
    pub account_sid: String,
    pub group_sid: String,
}

pub(crate) fn validate_random_id(value: &str) -> Result<()> {
    ensure!(
        value.len() == 32
            && value
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte)),
        "invalid candidate installation/generation ID"
    );
    Ok(())
}

fn validate_sid(value: &str) -> Result<()> {
    let sid = crate::sid::LocalPsid::from_string(value)?;
    ensure!(
        crate::sid::psid_to_string(sid.as_psid())? == value,
        "candidate SID must be canonical"
    );
    ensure!(
        value.starts_with("S-1-5-21-"),
        "candidate requires local account/domain SIDs"
    );
    Ok(())
}

impl AccountBinding {
    pub fn validate(&self) -> Result<()> {
        validate_random_id(&self.installation_id)?;
        ensure!(
            self.account_name == self.mode.name(),
            "candidate account name/mode mismatch"
        );
        let identities = [&self.owner_sid, &self.account_sid, &self.group_sid];
        for sid in identities {
            validate_sid(sid)?;
        }
        ensure!(
            identities.into_iter().collect::<BTreeSet<_>>().len() == 3,
            "candidate owner, account and group must be distinct"
        );
        Ok(())
    }
}

impl SetupRecord {
    pub fn validate_layout(&self) -> Result<()> {
        ensure!(
            self.schema == SCHEMA_VERSION,
            "unsupported candidate setup schema"
        );
        validate_random_id(&self.installation_id)?;
        validate_sid(&self.owner_sid)?;
        let mut identities = BTreeSet::from([self.owner_sid.as_str()]);
        if let Some(group) = &self.group {
            ensure!(
                group.name == ACCOUNT_GROUP,
                "candidate group namespace mismatch"
            );
            validate_sid(&group.sid)?;
            ensure!(
                identities.insert(&group.sid),
                "duplicate candidate identity"
            );
        }
        for (mode, record) in [
            (AccountMode::Offline, &self.offline),
            (AccountMode::Online, &self.online),
        ] {
            if let Some(record) = record {
                ensure!(
                    self.group.is_some(),
                    "account identity lacks its owning group record"
                );
                ensure!(
                    record.mode == mode && record.name == mode.name(),
                    "candidate account namespace mismatch"
                );
                validate_sid(&record.sid)?;
                ensure!(
                    identities.insert(&record.sid),
                    "duplicate candidate identity"
                );
                if let Some(reference) = &record.credential_generation {
                    crate::account_credentials::generation_path(
                        &self.installation_id,
                        mode,
                        reference,
                    )?;
                }
                for path in &record.ambient_paths {
                    ensure!(
                        std::path::Path::new(path).is_absolute() && !path.contains('\0'),
                        "invalid candidate ambient path"
                    );
                }
            }
        }
        if let Some(storage) = &self.storage {
            storage.validate_for(self)?;
        }
        Ok(())
    }

    pub fn binding(&self, mode: AccountMode) -> Result<AccountBinding> {
        self.validate_layout()?;
        let account = self.account(mode)?;
        let binding = AccountBinding {
            installation_id: self.installation_id.clone(),
            owner_sid: self.owner_sid.clone(),
            mode,
            account_name: account.name.clone(),
            account_sid: account.sid.clone(),
            group_sid: self
                .group
                .as_ref()
                .context("candidate group missing")?
                .sid
                .clone(),
        };
        binding.validate()?;
        Ok(binding)
    }

    pub fn account(&self, mode: AccountMode) -> Result<&AccountRecord> {
        match mode {
            AccountMode::Offline => &self.offline,
            AccountMode::Online => &self.online,
        }
        .as_ref()
        .context("candidate account is not provisioned")
    }

    pub fn validate_ready(&self, marker: u32, caller: &str) -> Result<()> {
        self.validate_layout()?;
        ensure!(
            marker == SCHEMA_VERSION && self.phase == SetupPhase::Ready,
            "candidate provisioning is incomplete; no fallback account is allowed"
        );
        ensure!(
            caller == self.owner_sid,
            "candidate setup belongs to a different Windows owner"
        );
        ensure!(
            self.storage
                .as_ref()
                .is_some_and(|storage| storage.is_complete()),
            "candidate storage attestation is incomplete"
        );
        for mode in [AccountMode::Offline, AccountMode::Online] {
            self.binding(mode)?;
            let account = self.account(mode)?;
            ensure!(
                account.credential_generation.is_some() && !account.ambient_paths.is_empty(),
                "candidate credential or ambient protection record is incomplete"
            );
        }
        Ok(())
    }

    /// Account/group creation is permitted only when no principal exists and
    /// none was previously recorded. Rotation/removal requires an exact prior
    /// SID match; missing/replaced identities are never silently adopted.
    pub fn validate_observed_principals(
        &self,
        group: Option<&str>,
        offline: Option<&str>,
        online: Option<&str>,
    ) -> Result<()> {
        self.validate_layout()?;
        for (recorded, observed) in [
            (self.group.as_ref().map(|record| record.sid.as_str()), group),
            (
                self.offline.as_ref().map(|record| record.sid.as_str()),
                offline,
            ),
            (
                self.online.as_ref().map(|record| record.sid.as_str()),
                online,
            ),
        ] {
            ensure!(
                recorded == observed,
                "candidate SAM identity differs from prior ownership record; refusing adoption or rotation"
            );
        }
        Ok(())
    }
}

/// Created only from validated machine state. Secret access stays in the
/// private credential module and is limited to typed launch/profile recovery.
pub struct ReadyInstallation {
    pub(crate) record: SetupRecord,
    // Every consumer, including typed credential launch/recovery, retains the
    // attested storage identity. Metadata readiness alone is insufficient.
    _storage: crate::account_state::storage::StoragePins,
}

impl ReadyInstallation {
    /// Synthetic account identities plus genuinely pinned private storage.
    /// Native integration fixtures never read/provision machine installation.
    #[cfg(test)]
    pub(crate) fn fixture(record: SetupRecord, root: &std::path::Path) -> Result<Self> {
        record.validate_ready(SCHEMA_VERSION, &crate::sid::current_user_sid()?)?;
        let storage = crate::account_state::storage::StoragePins::open_fixture(&record, root)?;
        Ok(Self {
            record,
            _storage: storage,
        })
    }

    pub fn read(_lease: &crate::execution_lease::ExecutionLease) -> Result<Self> {
        let key = crate::reg::Key::open(REGISTRY_ROOT, false)?
            .context("candidate setup is not installed")?;
        let marker = key
            .get_dword("MarkerVersion")?
            .context("candidate marker missing")?;
        let data = key.get_sz("State")?.context("candidate state missing")?;
        ensure!(
            data.len() <= 1024 * 1024,
            "candidate setup record exceeds size limit"
        );
        let record: SetupRecord =
            serde_json::from_str(&data).context("invalid candidate setup record")?;
        let current = crate::sid::current_user_sid()?;
        record.validate_ready(marker, &current)?;
        let offline = record.binding(AccountMode::Offline)?;
        key.validate_credential_security(
            &record.owner_sid,
            &offline.account_sid,
            &offline.group_sid,
        )?;
        bootstrap::validate_handoff(key.get_sz("Bootstrap")?.as_deref(), &record)?;
        activation::wfp::require_complete(&record, key.get_sz("WfpOwnership")?.as_deref())?;
        policy::require_complete(&record, key.get_sz(policy::VALUE)?.as_deref())?;
        let group = crate::user::local_group_sid(ACCOUNT_GROUP)?;
        let observed_offline = crate::user::local_user_sid(OFFLINE_USER)?;
        let observed_online = crate::user::local_user_sid(ONLINE_USER)?;
        record.validate_observed_principals(
            group.as_deref(),
            observed_offline.as_deref(),
            observed_online.as_deref(),
        )?;
        for mode in [AccountMode::Offline, AccountMode::Online] {
            let binding = record.binding(mode)?;
            crate::account_credentials::password::require_applied(
                &record,
                mode,
                key.get_sz(crate::account_credentials::password::value_name(mode))?
                    .as_deref(),
            )?;
            ensure!(
                crate::sam::is_member_of(&binding.group_sid, &binding.account_sid)?,
                "candidate account is no longer in its recorded security group"
            );
        }
        let storage = crate::account_state::storage::StoragePins::open(&record)?;
        Ok(Self {
            record,
            _storage: storage,
        })
    }

    pub fn binding(&self, mode: AccountMode) -> Result<AccountBinding> {
        self.record.binding(mode)
    }
    pub fn ambient_paths(&self, mode: AccountMode) -> Result<&[String]> {
        Ok(&self.record.account(mode)?.ambient_paths)
    }
}

#[cfg(test)]
pub(crate) fn synthetic_ready() -> SetupRecord {
    let account = |mode: AccountMode, sid: &str| AccountRecord {
        mode,
        name: mode.name().into(),
        sid: sid.into(),
        credential_generation: Some("CredOwner-1123456789abcdef0123456789abcdef".into()),
        ambient_paths: vec!["C:\\Windows".into()],
    };
    let mut record = SetupRecord {
        schema: SCHEMA_VERSION,
        installation_id: "0123456789abcdef0123456789abcdef".into(),
        owner_sid: "S-1-5-21-1-2-3-1001".into(),
        phase: SetupPhase::Ready,
        group: Some(GroupRecord {
            name: ACCOUNT_GROUP.into(),
            sid: "S-1-5-21-1-2-3-1002".into(),
        }),
        offline: Some(account(AccountMode::Offline, "S-1-5-21-1-2-3-1003")),
        online: Some(account(AccountMode::Online, "S-1-5-21-1-2-3-1004")),
        storage: None,
    };
    record.storage = Some(crate::account_state::storage::synthetic_attestation(
        &record,
    ));
    record
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn candidate_namespace_is_disjoint_and_sam_names_fit_windows() {
        assert!(!REGISTRY_ROOT.starts_with(&format!("{}\\", crate::reg::BASE)));
        assert_ne!(REGISTRY_ROOT, crate::reg::BASE);
        assert_ne!(OFFLINE_SUBLAYER, crate::wfp::DEFAULT_SUBLAYER_GUID);
        for name in [OFFLINE_USER, ONLINE_USER, ACCOUNT_GROUP] {
            assert!(name.len() <= 20);
            assert_ne!(name, crate::user::SANDBOX_USER);
            assert_ne!(name, crate::user::SANDBOX_GROUP);
        }
    }

    #[test]
    fn candidate_setup_refuses_partial_foreign_and_mixed_identity() {
        let mut record = synthetic_ready();
        assert!(record.validate_ready(4, &record.owner_sid).is_ok());
        assert!(record.validate_ready(0, &record.owner_sid).is_err());
        assert!(record.validate_ready(4, "S-1-5-21-1-2-3-9999").is_err());
        record.phase = SetupPhase::Provisioning;
        assert!(record.validate_ready(4, &record.owner_sid).is_err());
        record.phase = SetupPhase::Ready;
        record.online.as_mut().unwrap().sid = record.offline.as_ref().unwrap().sid.clone();
        assert!(record.validate_ready(4, &record.owner_sid).is_err());
        record.online = None;
        assert!(record.validate_ready(4, &record.owner_sid).is_err());
    }

    #[test]
    fn candidate_setup_never_adopts_names_or_replaced_sids() {
        let record = synthetic_ready();
        let group = record.group.as_ref().map(|item| item.sid.as_str());
        let offline = record.offline.as_ref().map(|item| item.sid.as_str());
        let online = record.online.as_ref().map(|item| item.sid.as_str());
        assert!(
            record
                .validate_observed_principals(group, offline, online)
                .is_ok()
        );
        assert!(
            record
                .validate_observed_principals(group, offline, Some("S-1-5-21-1-2-3-9999"))
                .is_err()
        );
        assert!(
            record
                .validate_observed_principals(group, offline, None)
                .is_err()
        );
        let mut partial = record.clone();
        partial.online = None;
        partial.storage = None;
        partial.phase = SetupPhase::Provisioning;
        assert!(
            partial
                .validate_observed_principals(group, offline, online)
                .is_err()
        );
        assert!(
            partial
                .validate_observed_principals(group, offline, None)
                .is_ok()
        );
    }
}
