//! Test-only immutable data for the fixed station/lowbox diagnostic.
//! Parsing never supplies a live initializer, profile, desktop or launch right.
use super::*;
use crate::account_control::bootstrap::entry::initializer_wire::paired;
use crate::account_install::AccountMode;
use crate::account_state::execution::resources::{FixtureRootBirthKind, FixtureRpcPolicy};

/// Passive completion identity; neither decoding nor validation admits effects.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum LowboxCompletion {
    #[default]
    LegacyDiagnosticV1,
    V3ShadowBoundedV1,
    V3ShadowOnlineBoundedV1,
}

impl LowboxCompletion {
    fn is_legacy(&self) -> bool {
        *self == Self::LegacyDiagnosticV1
    }
}

#[derive(Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct FixtureStationLowboxBinding {
    pub profile: FixtureWriterBinding,
    pub initializer: paired::Request,
    pub policy: u32,
    #[serde(default, skip_serializing_if = "LowboxCompletion::is_legacy")]
    pub completion: LowboxCompletion,
}

impl FixtureStationLowboxBinding {
    /// Fixed wire identity only, never root admission or publication authority.
    pub(crate) fn rpc_policy(&self) -> Result<FixtureRpcPolicy> {
        match self.completion {
            LowboxCompletion::LegacyDiagnosticV1 => {
                FixtureRpcPolicy::from_fixture_binding_policy(self.policy)
            }
            LowboxCompletion::V3ShadowBoundedV1 | LowboxCompletion::V3ShadowOnlineBoundedV1 => {
                ensure!(self.policy == 6, "V3 shadow completion RPC policy differs");
                Ok(FixtureRpcPolicy::SystemNsiReadInheritanceV3)
            }
        }
    }
    pub(crate) fn require_rpc_policy(
        &self,
        original: crate::account_state::execution::resources::FixtureRpcPolicy,
    ) -> Result<()> {
        self.validate()?;
        ensure!(
            self.rpc_policy()? == original,
            "station RPC binding differs from immutable paired plan"
        );
        Ok(())
    }
    pub(crate) fn require_namespace_birth(&self, expected: FixtureRootBirthKind) -> Result<()> {
        self.validate()?;
        let required = match self.completion {
            LowboxCompletion::LegacyDiagnosticV1 => FixtureRootBirthKind::LegacyUnspecified,
            LowboxCompletion::V3ShadowBoundedV1 | LowboxCompletion::V3ShadowOnlineBoundedV1 => {
                FixtureRootBirthKind::SessionShadowDirectoryV1
            }
        };
        ensure!(
            expected == required,
            "station completion differs from immutable namespace birth"
        );
        Ok(())
    }
    pub(crate) fn is_v3_shadow(&self) -> bool {
        self.v3_mode().is_some()
    }
    pub(crate) fn v3_mode(&self) -> Option<AccountMode> {
        match self.completion {
            LowboxCompletion::LegacyDiagnosticV1 => None,
            LowboxCompletion::V3ShadowBoundedV1 => Some(AccountMode::Offline),
            LowboxCompletion::V3ShadowOnlineBoundedV1 => Some(AccountMode::Online),
        }
    }
    pub(crate) fn synthetic_request() -> probe::Request {
        let (birth, _, _) = tests::fixture();
        probe::Request {
            birth,
            nonce: format!("{:032x}", 19),
            pid: 601,
            created: 300,
        }
    }
    pub(crate) fn require_resume_nonce(&self, nonce: &str) -> Result<()> {
        self.validate()?;
        validate_random_id(nonce)?;
        ensure!(
            !self.reserved_nonces()?.contains(&nonce),
            "station resume nonce aliases original resources"
        );
        Ok(())
    }
    /// Shape and internal consistency only; original live-resource comparison
    /// remains mandatory even after this validation succeeds.
    pub(crate) fn validate(&self) -> Result<()> {
        self.profile.validate()?;
        // Keep the initializer's own codec bound in addition to the enclosing
        // BirthRequest's unchanged 4608-byte bound.
        self.initializer.encode()?;
        self.rpc_policy()?;
        let subject = &self.initializer.subject;
        ensure!(
            !subject.omit_profile_environment
                && !self.profile.unsupported_registry
                && !self.profile.descendant_files
                && self.profile.hold_rounds == 0,
            "station lowbox fixture policy or workload differs"
        );
        ensure!(
            self.profile.container == subject.container
                && self.profile.package_sid == subject.package_sid
                && self.profile.owner_pid != subject.bootstrap_pid
                && self.profile.owner_created <= subject.bootstrap_created,
            "station lowbox profile differs from initializer lifetime"
        );
        let nonces = self.reserved_nonces()?;
        for (index, nonce) in nonces.iter().enumerate() {
            validate_random_id(nonce)?;
            ensure!(
                !nonces[..index].contains(nonce),
                "station lowbox binding aliases another lifetime"
            );
        }
        Ok(())
    }

    fn reserved_nonces(&self) -> Result<[&str; 13]> {
        let subject = &self.initializer.subject;
        let resources = &self.initializer.resources;
        Ok([
            &subject.operation,
            &subject.nonce,
            subject
                .container
                .strip_prefix("Alysis.Execution.")
                .context("station lowbox execution container differs")?,
            &resources.owner_job,
            &resources.consumer_job,
            &resources.namespace,
            &resources.host_profile,
            &resources.host_profile_use,
            &resources.station,
            &resources.desktop,
            &self.profile.owner_birth,
            &self.profile.owner_resume,
            &self.profile.use_nonce,
        ])
    }

    /// Bind to the exact original consumer bootstrap selected for credentials.
    /// The initializer image is System32's fixed image, not Selection.image_id
    /// (the account helper). require_initializer compares that image separately.
    pub(crate) fn validate_for(&self, selected: &Selection, birth_nonce: &str) -> Result<()> {
        self.validate()?;
        selected.validate()?;
        ensure!(
            self.v3_mode()
                .is_none_or(|mode| selected.account.mode == mode),
            "V3 completion differs from selected account mode"
        );
        let subject = &self.initializer.subject;
        ensure!(
            subject.operation == selected.operation
                && subject.bootstrap_pid == selected.bootstrap_pid
                && subject.bootstrap_created == selected.bootstrap_created
                && subject.session == selected.session
                && subject.owner == selected.account.owner_sid,
            "station lowbox initializer differs from original selection"
        );
        let reserved = self.reserved_nonces()?;
        let fresh = [
            birth_nonce,
            selected.logon.as_str(),
            selected.ready.as_str(),
        ];
        for (index, nonce) in fresh.iter().enumerate() {
            validate_random_id(nonce)?;
            ensure!(
                !reserved.contains(nonce) && !fresh[..index].contains(nonce),
                "station lowbox credential or birth nonce aliases a lifetime"
            );
        }
        Ok(())
    }

    /// Compare the original owner request and exact SDK folder/use receipt.
    /// The profile coordinator must additionally preserve its existing full
    /// owner/consumer AccountBinding, helper-image and generation comparisons.
    pub(crate) fn require_owner(
        &self,
        owner: &probe::Request,
        folder: [u8; 24],
        use_nonce: &str,
    ) -> Result<()> {
        self.validate()?;
        self.profile.require_owner(owner, folder, use_nonce)?;
        let selected = &owner.birth.selection;
        ensure!(
            self.v3_mode()
                .is_none_or(|mode| selected.account.mode == mode),
            "V3 completion differs from original profile owner mode"
        );
        let subject = &self.initializer.subject;
        ensure!(
            selected.operation == subject.operation
                && selected.session == subject.session
                && selected.account.owner_sid == subject.owner
                && selected.bootstrap_pid != subject.bootstrap_pid
                && owner.created <= subject.bootstrap_created,
            "station lowbox original profile owner differs from initializer"
        );
        let reserved = self.reserved_nonces()?;
        for nonce in [selected.logon.as_str(), selected.ready.as_str()] {
            ensure!(
                !reserved.contains(&nonce),
                "station lowbox owner credential nonce aliases a lifetime"
            );
        }
        Ok(())
    }

    /// The caller supplies the request from its original live initializer,
    /// never a reconstruction from a diagnostic name or untrusted saved row.
    pub(crate) fn require_initializer(&self, original: &paired::Request) -> Result<()> {
        self.validate()?;
        original.validate()?;
        ensure!(
            &self.initializer == original,
            "station lowbox differs from complete original initializer resources"
        );
        Ok(())
    }

    /// Fixed derivation only. The wire contains no caller-supplied desktop path.
    pub(crate) fn desktop_name(&self) -> Result<String> {
        self.validate()?;
        self.initializer.desktop_name()
    }
}

#[path = "account_station_lowbox_binding_tests.rs"]
mod tests;
