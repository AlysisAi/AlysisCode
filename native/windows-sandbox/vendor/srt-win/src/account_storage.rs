//! Installer-attested storage for the disabled account candidate. An OS-derived
//! x64 Program Files anchor replaces unsafe profile ancestry. No API repairs
//! ancestor ACLs, adopts an existing installation root, or guesses cleanup.

use anyhow::{Context, Result, ensure};
use serde::{Deserialize, Serialize};
use std::path::Path;
#[cfg(test)]
use std::path::PathBuf;

use super::fs;
use crate::account_install::{AccountBinding, AccountMode, SetupRecord};

const PREFIX: &str = "AlysisSandboxState-";

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct Identity {
    installation_id: String,
    owner_sid: String,
    offline_sid: String,
    online_sid: String,
    group_sid: String,
}

impl Identity {
    // Do not call SetupRecord::binding/validate_layout here: layout validation
    // itself checks this attestation. The two independent bindings validate SID
    // syntax/uniqueness; SetupRecord validates cross-account distinctness.
    fn from_record(record: &SetupRecord) -> Result<Self> {
        let identity = Self {
            installation_id: record.installation_id.clone(),
            owner_sid: record.owner_sid.clone(),
            offline_sid: record.account(AccountMode::Offline)?.sid.clone(),
            online_sid: record.account(AccountMode::Online)?.sid.clone(),
            group_sid: record
                .group
                .as_ref()
                .context("storage group missing")?
                .sid
                .clone(),
        };
        for (mode, sid) in [
            (AccountMode::Offline, &identity.offline_sid),
            (AccountMode::Online, &identity.online_sid),
        ] {
            AccountBinding {
                installation_id: identity.installation_id.clone(),
                owner_sid: identity.owner_sid.clone(),
                mode,
                account_name: mode.name().into(),
                account_sid: sid.clone(),
                group_sid: identity.group_sid.clone(),
            }
            .validate()?;
        }
        Ok(identity)
    }

    fn policy(&self) -> fs::Policy {
        fs::Policy {
            owner: self.owner_sid.clone(),
            offline: self.offline_sid.clone(),
            online: self.online_sid.clone(),
            group: self.group_sid.clone(),
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct ObjectIdentity {
    path: String,
    file_id: [u8; 24],
}

impl ObjectIdentity {
    fn from_pin(pin: &fs::Pinned) -> Result<Self> {
        Ok(Self {
            path: pin
                .path
                .to_str()
                .context("non-Unicode storage ancestor")?
                .to_ascii_lowercase(),
            file_id: pin.id.as_bytes(),
        })
    }
}

/// This value is persisted inside the admin-written candidate State. None in
/// `root_id` means a durable create intent, not permission to adopt a name. Only
/// a successful create-new handle can produce the completed attestation.
/// Fields are private; deserialized values must pass validate_for before use.
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct StorageRecord {
    version: u32,
    identity: Identity,
    // Exact volume/file IDs AND lexical OS-derived paths; no arbitrary caller
    // path and no migration to a newly redirected KnownFolder on reopen.
    ancestors: Vec<ObjectIdentity>,
    root_id: Option<[u8; 24]>,
}

impl StorageRecord {
    pub fn is_complete(&self) -> bool {
        self.root_id.is_some()
    }

    pub(crate) fn validate_for(&self, record: &SetupRecord) -> Result<()> {
        ensure!(
            self.version == 1,
            "unsupported candidate storage attestation"
        );
        ensure!(
            self.identity == Identity::from_record(record)?,
            "candidate storage identity mismatch"
        );
        ensure!(
            (2..=128).contains(&self.ancestors.len()),
            "invalid storage ancestor inventory"
        );
        let first = &self.ancestors[0];
        ensure!(
            first.path.len() == 3
                && first.path.as_bytes()[0].is_ascii_alphabetic()
                && &first.path.as_bytes()[1..] == b":\\",
            "storage ancestry must start at a local drive"
        );
        let mut ids = std::collections::BTreeSet::new();
        for (index, object) in self.ancestors.iter().enumerate() {
            ensure!(
                object.path.len() <= 32_767
                    && !object.path.contains(['\0', '/'])
                    && object.file_id[..8] == first.file_id[..8]
                    && ids.insert(object.file_id),
                "invalid or duplicate storage ancestor identity"
            );
            if index > 0 {
                let path = Path::new(&object.path);
                let component = path
                    .file_name()
                    .and_then(|value| value.to_str())
                    .context("invalid storage component")?;
                ensure!(
                    !component.ends_with([' ', '.'])
                        && !component.contains(':')
                        && !matches!(component, "." | "..")
                        && path.parent() == Some(Path::new(&self.ancestors[index - 1].path)),
                    "storage ancestor inventory is not a contiguous lexical path"
                );
            }
        }
        if let Some(root) = self.root_id {
            ensure!(
                root[..8] == first.file_id[..8] && ids.insert(root),
                "invalid storage root identity"
            );
        }
        Ok(())
    }

    pub(crate) fn validate_transition(&self, previous: Option<&Self>) -> Result<()> {
        if let Some(previous) = previous {
            ensure!(
                !previous.is_complete(),
                "completed storage identity cannot be overwritten"
            );
            let mut pending = self.clone();
            pending.root_id = None;
            ensure!(
                pending == *previous,
                "storage transition changed its durable intent"
            );
        } else {
            ensure!(
                !self.is_complete(),
                "storage result requires a prior durable create intent"
            );
        }
        Ok(())
    }

    fn component(&self) -> String {
        format!("{PREFIX}{}", self.identity.installation_id)
    }

    #[cfg(test)]
    fn root_path(&self) -> Result<PathBuf> {
        Ok(Path::new(
            &self
                .ancestors
                .last()
                .context("storage ancestry missing")?
                .path,
        )
        .join(self.component()))
    }

    fn pending(record: &SetupRecord, ancestors: &[fs::Pinned]) -> Result<Self> {
        let value = Self {
            version: 1,
            identity: Identity::from_record(record)?,
            ancestors: ancestors
                .iter()
                .map(ObjectIdentity::from_pin)
                .collect::<Result<_>>()?,
            root_id: None,
        };
        value.validate_for(record)?;
        Ok(value)
    }

    fn validate_pins(&self, pins: &[fs::Pinned]) -> Result<()> {
        let current = pins
            .iter()
            .map(ObjectIdentity::from_pin)
            .collect::<Result<Vec<_>>>()?;
        ensure!(
            current == self.ancestors,
            "candidate storage ancestor identity changed; preserve ownership record"
        );
        Ok(())
    }
}

/// A read-only preflight. The setup coordinator must durably persist intent()
/// before calling create(). All validated ancestors stay pinned across both.
pub(crate) struct PreparedStorage {
    intent: StorageRecord,
    parents: Vec<fs::Pinned>,
}

impl PreparedStorage {
    pub(crate) fn prepare(record: &SetupRecord) -> Result<Self> {
        record.validate_layout()?;
        let policy = Identity::from_record(record)?.policy();
        let parents = fs::pin_session_path(&fs::program_files_anchor()?, &policy)?;
        Self::from_pins(record, parents)
    }

    #[cfg(test)]
    pub(super) fn prepare_fixture(record: &SetupRecord, anchor: &Path) -> Result<Self> {
        record.validate_layout()?;
        let policy = Identity::from_record(record)?.policy();
        Self::from_pins(record, fs::pin_fixture_path(anchor, &policy)?)
    }

    fn from_pins(record: &SetupRecord, parents: Vec<fs::Pinned>) -> Result<Self> {
        let intent = StorageRecord::pending(record, &parents)?;
        if let Some(prior) = &record.storage {
            prior.validate_for(record)?;
            ensure!(
                !prior.is_complete(),
                "candidate storage already attested; use exact reopen"
            );
            ensure!(
                *prior == intent,
                "pending storage ancestry changed; preserve intent for explicit repair"
            );
        }
        Ok(Self { intent, parents })
    }

    pub(crate) fn intent(&self) -> &StorageRecord {
        &self.intent
    }

    /// No existing-name fallback. Failure after filesystem creation but before
    /// attestation persistence deliberately leaves a pending, unadoptable root.
    pub(crate) fn create(mut self) -> Result<StoragePins> {
        let policy = self.intent.identity.policy();
        let root = fs::create_child_directory(
            self.parents.last().context("storage anchor missing")?,
            &self.intent.component(),
            &policy,
        )?;
        policy.validate(&root, true)?;
        ensure!(
            std::fs::read_dir(&root.path)?.next().is_none(),
            "fresh candidate storage root is not empty"
        );
        self.intent.root_id = Some(root.id.as_bytes());
        self.parents.push(root);
        Ok(StoragePins {
            attestation: self.intent,
            parents: self.parents,
        })
    }
}

/// Pins retained through setup's ready-marker commit and, on runtime open,
/// through the SQLite connection lifetime. No automatic deletion on Drop.
pub(crate) struct StoragePins {
    pub(super) attestation: StorageRecord,
    pub(super) parents: Vec<fs::Pinned>,
}

impl StoragePins {
    pub(crate) fn open(record: &SetupRecord) -> Result<Self> {
        record.validate_layout()?;
        let attestation = record
            .storage
            .as_ref()
            .context("candidate storage is not provisioned")?;
        attestation.validate_for(record)?;
        ensure!(
            attestation.is_complete(),
            "candidate storage create intent is unfinished"
        );
        let policy = attestation.identity.policy();
        let parents = fs::pin_session_path(&fs::program_files_anchor()?, &policy)?;
        Self::open_from_pins(record, parents)
    }

    #[cfg(test)]
    pub(crate) fn open_fixture(record: &SetupRecord, anchor: &Path) -> Result<Self> {
        record.validate_layout()?;
        let policy = Identity::from_record(record)?.policy();
        Self::open_from_pins(record, fs::pin_fixture_path(anchor, &policy)?)
    }

    fn open_from_pins(record: &SetupRecord, mut parents: Vec<fs::Pinned>) -> Result<Self> {
        let attestation = record
            .storage
            .as_ref()
            .context("candidate storage missing")?;
        attestation.validate_for(record)?;
        let expected = attestation
            .root_id
            .context("candidate storage create intent is unfinished")?;
        attestation.validate_pins(&parents)?;
        let root = fs::open_child_directory(
            parents.last().context("storage anchor missing")?,
            &attestation.component(),
            None,
        )?
        .0;
        attestation.identity.policy().validate(&root, true)?;
        ensure!(
            root.id.as_bytes() == expected,
            "candidate storage root was moved/replaced; preserve ownership record"
        );
        parents.push(root);
        Ok(Self {
            attestation: attestation.clone(),
            parents,
        })
    }

    pub(crate) fn attestation(&self) -> &StorageRecord {
        &self.attestation
    }

    pub(crate) fn verify(&self, record: &SetupRecord) -> Result<()> {
        self.attestation.validate_for(record)?;
        ensure!(
            record.storage.as_ref() == Some(&self.attestation),
            "storage attestation changed during setup"
        );
        let root = self.parents.last().context("storage root pin missing")?;
        self.attestation
            .validate_pins(&self.parents[..self.parents.len() - 1])?;
        ensure!(
            Some(root.id.as_bytes()) == self.attestation.root_id,
            "storage pin identity mismatch"
        );
        let policy = self.attestation.identity.policy();
        fs::validate_pinned_ancestors(&self.parents[..self.parents.len() - 1], &policy)?;
        policy.validate(root, true)
    }
}

#[cfg(test)]
pub(crate) fn synthetic_attestation(record: &SetupRecord) -> StorageRecord {
    let mut drive = [0; 24];
    drive[0] = 1;
    drive[8] = 1;
    let mut anchor = drive;
    anchor[8] = 2;
    let mut root = drive;
    root[8] = 3;
    StorageRecord {
        version: 1,
        identity: Identity::from_record(record).unwrap(),
        ancestors: vec![
            ObjectIdentity {
                path: r"c:\".into(),
                file_id: drive,
            },
            ObjectIdentity {
                path: r"c:\program files".into(),
                file_id: anchor,
            },
        ],
        root_id: Some(root),
    }
}

#[cfg(test)]
pub(crate) fn synthetic_intent(record: &SetupRecord) -> StorageRecord {
    let mut value = synthetic_attestation(record);
    value.root_id = None;
    value
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::account_install::synthetic_ready;

    struct Fixture {
        root: PathBuf,
        record: SetupRecord,
    }
    impl Fixture {
        fn new() -> Self {
            let root = crate::util::test_temp_dir().join(format!(
                "alysis-storage-fixture-{}-{}",
                std::process::id(),
                std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .unwrap()
                    .as_nanos()
            ));
            std::fs::create_dir(&root).unwrap();
            let mut record = synthetic_ready();
            record.owner_sid = crate::sid::current_user_sid().unwrap();
            record.storage = None;
            Self { root, record }
        }
        fn prepare(&self) -> PreparedStorage {
            PreparedStorage::prepare_fixture(&self.record, &self.root).unwrap()
        }
        fn install(&mut self) -> StoragePins {
            let plan = self.prepare();
            self.record.storage = Some(plan.intent().clone());
            let pins = plan.create().unwrap();
            self.record.storage = Some(pins.attestation().clone());
            pins
        }
        fn open(&self) -> Result<StoragePins> {
            StoragePins::open_fixture(&self.record, &self.root)
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            assert!(
                self.root.is_absolute()
                    && self
                        .root
                        .file_name()
                        .unwrap()
                        .to_string_lossy()
                        .starts_with("alysis-storage-fixture-")
            );
            std::fs::remove_dir_all(&self.root).expect("remove owned storage fixture");
        }
    }

    #[test]
    fn candidate_storage_attestation_binds_tenant_principals_and_every_ancestor() {
        let record = synthetic_ready();
        let attestation = record.storage.as_ref().unwrap();
        attestation.validate_for(&record).unwrap();
        for field in [
            "installation_id",
            "owner_sid",
            "offline_sid",
            "online_sid",
            "group_sid",
        ] {
            let mut json = serde_json::to_value(attestation).unwrap();
            json["identity"][field] = if field == "installation_id" {
                "1123456789abcdef0123456789abcdef"
            } else {
                "S-1-5-21-1-2-3-9999"
            }
            .into();
            let altered: StorageRecord = serde_json::from_value(json).unwrap();
            assert!(
                altered.validate_for(&record).is_err(),
                "accepted foreign {field}"
            );
        }
        for alter in 0..5 {
            let mut value = attestation.clone();
            match alter {
                0 => value.version = 2,
                1 => value.ancestors[1].file_id = value.ancestors[0].file_id,
                2 => value.ancestors[1].path = r"c:\other\program files".into(),
                3 => value.root_id.as_mut().unwrap()[0] ^= 1,
                _ => value.ancestors[1].path = r"c:\program files.".into(),
            }
            assert!(value.validate_for(&record).is_err());
        }
        let mut value = serde_json::to_value(attestation).unwrap();
        value["fallback_path"] = r"C:\Users\Public".into();
        assert!(serde_json::from_value::<StorageRecord>(value).is_err());
    }

    #[test]
    fn candidate_storage_transition_requires_exact_intent_and_completed_identity_is_immutable() {
        let record = synthetic_ready();
        let intent = synthetic_intent(&record);
        let complete = synthetic_attestation(&record);
        intent.validate_transition(None).unwrap();
        assert!(complete.validate_transition(None).is_err());
        complete.validate_transition(Some(&intent)).unwrap();
        assert!(complete.validate_transition(Some(&complete)).is_err());
        let mut foreign = intent.clone();
        foreign.ancestors[1].file_id[9] ^= 1;
        assert!(complete.validate_transition(Some(&foreign)).is_err());
    }

    #[test]
    fn candidate_storage_create_birth_policy_pins_and_exact_cold_reopen() {
        let mut fixture = Fixture::new();
        let pins = fixture.install();
        let path = pins.attestation.root_path().unwrap();
        pins.attestation
            .identity
            .policy()
            .validate(pins.parents.last().unwrap(), true)
            .unwrap();
        assert!(std::fs::rename(&path, fixture.root.join("moved")).is_err());
        assert!(std::fs::rename(&fixture.root, fixture.root.with_extension("moved")).is_err());
        drop(pins);
        let reopened = fixture.open().unwrap();
        assert_eq!(
            reopened.attestation,
            *fixture.record.storage.as_ref().unwrap()
        );
        drop(reopened);
        for index in 0..fixture.record.storage.as_ref().unwrap().ancestors.len() {
            let mut altered = fixture.record.clone();
            altered.storage.as_mut().unwrap().ancestors[index].file_id[12] ^= 1;
            assert!(StoragePins::open_fixture(&altered, &fixture.root).is_err());
        }
        assert!(path.is_dir());
    }

    #[test]
    fn candidate_storage_existing_or_created_unrecorded_root_is_never_adopted() {
        for created_by_us in [false, true] {
            let mut fixture = Fixture::new();
            let plan = fixture.prepare();
            fixture.record.storage = Some(plan.intent().clone());
            let path = plan.intent().root_path().unwrap();
            if created_by_us {
                drop(plan.create().unwrap());
            } else {
                drop(plan);
                std::fs::create_dir(&path).unwrap();
            }
            std::fs::write(path.join("preserve"), b"unrecorded evidence").unwrap();
            assert!(fixture.prepare().create().is_err());
            assert!(fixture.open().is_err());
            assert_eq!(
                std::fs::read(path.join("preserve")).unwrap(),
                b"unrecorded evidence"
            );
            assert!(!fixture.record.storage.as_ref().unwrap().is_complete());
        }
    }

    #[test]
    fn candidate_storage_moved_replaced_or_missing_root_preserves_exact_inventory() {
        let mut fixture = Fixture::new();
        let pins = fixture.install();
        let prior = pins.attestation.clone();
        let path = prior.root_path().unwrap();
        drop(pins);
        std::fs::write(path.join("owned"), b"owned evidence").unwrap();
        let moved = fixture.root.join("retained-original");
        std::fs::rename(&path, &moved).unwrap();
        assert!(fixture.open().is_err());
        let parents = fs::pin_fixture_path(&fixture.root, &prior.identity.policy()).unwrap();
        let replacement = fs::create_child_directory(
            parents.last().unwrap(),
            &prior.component(),
            &prior.identity.policy(),
        )
        .unwrap();
        std::fs::write(path.join("foreign"), b"replacement evidence").unwrap();
        drop(replacement);
        drop(parents);
        assert!(format!("{:#}", fixture.open().err().unwrap()).contains("moved/replaced"));
        assert_eq!(fixture.record.storage.as_ref(), Some(&prior));
        assert_eq!(
            std::fs::read(moved.join("owned")).unwrap(),
            b"owned evidence"
        );
        assert_eq!(
            std::fs::read(path.join("foreign")).unwrap(),
            b"replacement evidence"
        );
    }

    #[test]
    #[ignore = "explicit normal-user read-only x64 ProgramFiles admission; no state creation"]
    fn candidate_storage_program_files_admission_read_only() {
        let mut record = synthetic_ready();
        record.owner_sid = crate::sid::current_user_sid().unwrap();
        record.storage = None;
        let plan = PreparedStorage::prepare(&record).unwrap();
        println!(
            "PROGRAM_FILES_ADMITTED_READ_ONLY: {} ({} pinned ancestors)",
            plan.intent.ancestors.last().unwrap().path,
            plan.parents.len()
        );
    }
}
