//! First-install ownership only. All created accounts remain disabled; policy,
//! membership, password rotation, activation and readiness are separate work.
//! No CLI calls this feature-gated API. Never compensate failures by deleting
//! guessed names: a successful SAM create with no durable SID needs admin repair.

use anyhow::{Context, Result, ensure};
use serde::{Deserialize, Serialize};

use super::{
    ACCOUNT_GROUP, AccountMode, AccountRecord, GroupRecord, SCHEMA_VERSION, SetupPhase, SetupRecord,
};

#[path = "account_bootstrap_registry.rs"]
mod registry;
#[path = "account_bootstrap_sam.rs"]
mod sam;

/// Fixed coordinator context survives sanitization without exposing state.
#[derive(Debug)]
struct FailureStep(&'static str);
impl std::fmt::Display for FailureStep {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(formatter, "candidate bootstrap step {}", self.0)
    }
}
impl std::error::Error for FailureStep {}

#[cfg(test)]
#[derive(Serialize)]
pub(super) struct SanitizedFailure {
    step: Option<&'static str>,
    operation: Option<&'static str>,
    status: Option<u32>,
    parameter: Option<u32>,
}

#[cfg(test)]
pub(super) fn sanitized_failure(error: &anyhow::Error) -> Option<SanitizedFailure> {
    let step = error.downcast_ref::<FailureStep>();
    let native = error.downcast_ref::<sam::NativeFailure>();
    (step.is_some() || native.is_some()).then(|| SanitizedFailure {
        step: step.map(|value| value.0),
        operation: native.map(|value| value.operation),
        status: native.map(|value| value.status),
        parameter: native.and_then(|value| value.parameter),
    })
}

#[derive(Clone, Copy, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
enum Principal {
    Group,
    Offline,
    Online,
}

impl Principal {
    fn mode(self) -> Option<AccountMode> {
        match self {
            Self::Group => None,
            Self::Offline => Some(AccountMode::Offline),
            Self::Online => Some(AccountMode::Online),
        }
    }
}

#[derive(Clone, Copy, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
enum Phase {
    Creating,
    Finalizing,
    HandedOff,
}

/// One authoritative value; the exact SID and cleared intent are persisted in
/// a single replacement. State is materialized only at the final handoff.
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct Ownership {
    version: u32,
    phase: Phase,
    pending: Option<Principal>,
    record: SetupRecord,
}

impl Ownership {
    fn new(owner: &str, installation_id: String) -> Result<Self> {
        let value = Self {
            version: 1,
            phase: Phase::Creating,
            pending: None,
            record: SetupRecord {
                schema: SCHEMA_VERSION,
                installation_id,
                owner_sid: owner.into(),
                phase: SetupPhase::Provisioning,
                group: None,
                offline: None,
                online: None,
                storage: None,
            },
        };
        value.validate()?;
        Ok(value)
    }

    fn next(&self) -> Option<Principal> {
        if self.record.group.is_none() {
            Some(Principal::Group)
        } else if self.record.offline.is_none() {
            Some(Principal::Offline)
        } else if self.record.online.is_none() {
            Some(Principal::Online)
        } else {
            None
        }
    }

    fn validate(&self) -> Result<()> {
        self.record.validate_layout()?;
        ensure!(self.version == 1, "unsupported candidate bootstrap version");
        ensure!(
            self.record.phase == SetupPhase::Provisioning,
            "bootstrap cannot assert readiness or removal"
        );
        ensure!(
            self.record.storage.is_none(),
            "bootstrap must not own storage attestation"
        );
        ensure!(
            self.record.online.is_none() || self.record.offline.is_some(),
            "bootstrap account order is invalid"
        );
        for account in [&self.record.offline, &self.record.online]
            .into_iter()
            .flatten()
        {
            ensure!(
                account.credential_generation.is_none() && account.ambient_paths.is_empty(),
                "bootstrap must not own credentials or policy"
            );
        }
        if let Some(pending) = self.pending {
            ensure!(
                self.phase == Phase::Creating && self.next() == Some(pending),
                "bootstrap create intent disagrees with recorded ownership"
            );
        }
        if self.phase != Phase::Creating {
            ensure!(
                self.next().is_none() && self.pending.is_none(),
                "bootstrap handoff requires all recorded principals"
            );
        }
        Ok(())
    }

    fn record_created(&mut self, principal: Principal, sid: String) -> Result<()> {
        ensure!(
            self.pending == Some(principal),
            "missing durable create intent"
        );
        if let Some(mode) = principal.mode() {
            let record = AccountRecord {
                mode,
                name: mode.name().into(),
                sid,
                credential_generation: None,
                ambient_paths: Vec::new(),
            };
            match mode {
                AccountMode::Offline => self.record.offline = Some(record),
                AccountMode::Online => self.record.online = Some(record),
            }
        } else {
            self.record.group = Some(GroupRecord {
                name: ACCOUNT_GROUP.into(),
                sid,
            });
        }
        self.pending = None;
        self.validate()
    }
}

#[derive(Clone, Default)]
struct Observed {
    group: Option<String>,
    // The flag is observed UF_ACCOUNTDISABLE, not an expectation from metadata.
    offline: Option<(String, bool)>,
    online: Option<(String, bool)>,
}

impl Observed {
    fn validate(&self, value: &Ownership) -> Result<()> {
        value.record.validate_observed_principals(
            self.group.as_deref(),
            self.offline.as_ref().map(|v| v.0.as_str()),
            self.online.as_ref().map(|v| v.0.as_str()),
        ).context("bootstrap refuses missing, replaced or created-but-unrecorded principals; explicit administrator repair is required")?;
        ensure!(
            [&self.offline, &self.online]
                .into_iter()
                .flatten()
                .all(|v| v.1),
            "candidate bootstrap account is unexpectedly enabled"
        );
        Ok(())
    }
}

trait Store {
    fn observe(&self) -> Result<Observed>;
    fn write_marker_zero(&mut self) -> Result<()>;
    /// Must flush and verify before returning success.
    fn write_ownership(&mut self, value: &Ownership) -> Result<()>;
    /// Create only; all preexisting-name outcomes are errors. No fallback.
    fn create_principal(&mut self, principal: Principal) -> Result<String>;
    /// Same handle, exact final policy; only after all SIDs are durable.
    fn protect_final_root(&mut self, value: &Ownership) -> Result<()>;
    fn write_state(&mut self, value: &SetupRecord) -> Result<()>;
}

struct Bootstrap<S: Store> {
    store: S,
    value: Ownership,
    poisoned: bool,
}

impl<S: Store> Bootstrap<S> {
    fn start(mut store: S, value: Ownership) -> Result<Self> {
        value.validate()?;
        ensure!(
            value.next() == Some(Principal::Group) && value.pending.is_none(),
            "initial bootstrap must have no ownership"
        );
        store.observe()?.validate(&value)?;
        // Even a marker/readback failure leaves no SAM effects. A root without
        // the first ownership value is deliberately not automatically adopted.
        store.write_marker_zero()?;
        store.write_ownership(&value)?;
        Ok(Self {
            store,
            value,
            poisoned: false,
        })
    }

    fn resume(store: S, value: Ownership, marker: u32, owner: &str) -> Result<Self> {
        value.validate()?;
        ensure!(marker == 0, "bootstrap requires an unready marker");
        ensure!(
            value.record.owner_sid == owner,
            "bootstrap belongs to another owner"
        );
        ensure!(
            value.phase != Phase::HandedOff,
            "bootstrap completed; use the owned setup transaction"
        );
        store.observe()?.validate(&value)?;
        Ok(Self {
            store,
            value,
            poisoned: false,
        })
    }

    fn create_next(&mut self) -> Result<bool> {
        ensure!(
            !self.poisoned,
            "bootstrap failed; reopen and validate persisted ownership"
        );
        ensure!(
            self.value.phase == Phase::Creating,
            "bootstrap is finalizing"
        );
        self.poisoned = true;
        self.store
            .observe()
            .context(FailureStep("observe_before_intent"))?
            .validate(&self.value)
            .context(FailureStep("validate_before_intent"))?;
        let Some(principal) = self.value.next() else {
            self.poisoned = false;
            return Ok(false);
        };
        self.value.pending = Some(principal);
        self.store
            .write_ownership(&self.value)
            .context(FailureStep("persist_create_intent"))?;
        let sid = self
            .store
            .create_principal(principal)
            .context(FailureStep("create_principal"))?;
        self.value
            .record_created(principal, sid)
            .context(FailureStep("record_created_identity"))?;
        // No second SAM mutation (including membership or flags) occurs before
        // this SID is durably recorded. Failure makes this object unusable.
        self.store
            .write_ownership(&self.value)
            .context(FailureStep("persist_created_identity"))?;
        self.store
            .observe()
            .context(FailureStep("observe_created_identity"))?
            .validate(&self.value)
            .context(FailureStep("validate_created_identity"))?;
        self.poisoned = false;
        Ok(true)
    }

    fn finish(mut self) -> Result<SetupRecord> {
        ensure!(!self.poisoned, "bootstrap failed; reopen before handoff");
        ensure!(
            self.value.next().is_none(),
            "bootstrap ownership is incomplete"
        );
        self.store.observe()?.validate(&self.value)?;
        self.value.phase = Phase::Finalizing;
        self.value.validate()?;
        self.store.write_ownership(&self.value)?;
        self.store.protect_final_root(&self.value)?;
        self.store.write_state(&self.value.record)?;
        self.value.phase = Phase::HandedOff;
        self.store.write_ownership(&self.value)?;
        // Marker remains zero. Even this return means only ownership transfer,
        // not usable credentials, enabled accounts, network policy or readiness.
        Ok(self.value.record)
    }
}

/// Require the bootstrap handoff latch before an existing setup writer can
/// mutate a materialized State value. Older already-owned schema-4 fixtures
/// without Bootstrap retain their prior validation path.
pub(super) fn validate_handoff(text: Option<&str>, record: &SetupRecord) -> Result<()> {
    let Some(text) = text else {
        return Ok(());
    };
    ensure!(
        text.len() <= registry::MAX_VALUE,
        "bootstrap value exceeds size limit"
    );
    let value: Ownership = serde_json::from_str(text).context("invalid bootstrap ownership")?;
    value.validate()?;
    ensure!(
        value.phase == Phase::HandedOff,
        "candidate bootstrap handoff is unfinished"
    );
    for mode in [AccountMode::Offline, AccountMode::Online] {
        ensure!(
            value.record.binding(mode)? == record.binding(mode)?,
            "bootstrap handoff identity mismatch"
        );
    }
    Ok(())
}

fn require_authority(expected: &str, actual: &str, elevated: bool, pending: bool) -> Result<()> {
    super::validate_sid(expected)?;
    ensure!(
        expected == actual,
        "candidate bootstrap requires consent elevation as its requested owner"
    );
    ensure!(elevated, "candidate bootstrap requires elevation");
    ensure!(
        !pending,
        "candidate bootstrap refuses an unfinished sandbox execution"
    );
    Ok(())
}

/// Feature-only first-install primitive. There is no activation, default/CLI
/// route, automatic repair/deletion or raw execution API. Each successful create
/// records its exact SID; its initial password is discarded while still disabled.
pub struct FirstInstall {
    bootstrap: Bootstrap<registry::MachineStore>,
    _lease: crate::execution_lease::ExecutionLease,
}

impl FirstInstall {
    fn authority(expected_owner: &str) -> Result<crate::execution_lease::ExecutionLease> {
        let actual = crate::sid::current_user_sid()?;
        let elevated = super::setup::is_elevated()?;
        require_authority(expected_owner, &actual, elevated, false)?;
        let lease = crate::execution_lease::ExecutionLease::acquire()?;
        require_authority(
            expected_owner,
            &actual,
            elevated,
            crate::account_state::pending_journal_path()?.try_exists()?,
        )?;
        Ok(lease)
    }

    /// Refuses any existing root, regardless of its ACL, marker or contents.
    pub fn create(expected_owner: &str) -> Result<Self> {
        let lease = Self::authority(expected_owner)?;
        let value = Ownership::new(expected_owner, registry::random_id()?)?;
        sam::observe()?.validate(&value)?;
        let store = registry::MachineStore::create_fresh(expected_owner)?;
        Ok(Self {
            bootstrap: Bootstrap::start(store, value)?,
            _lease: lease,
        })
    }

    /// Accepts only a validated partial journal. Root/intent without a recorded
    /// SID is not ownership of an observed account, even when names match.
    pub fn resume(expected_owner: &str) -> Result<Self> {
        let lease = Self::authority(expected_owner)?;
        let (store, value, marker) = registry::MachineStore::open_partial(expected_owner)?;
        Ok(Self {
            bootstrap: Bootstrap::resume(store, value, marker, expected_owner)?,
            _lease: lease,
        })
    }

    pub fn record(&self) -> &SetupRecord {
        &self.bootstrap.value.record
    }

    /// Creates at most one principal, in group/offline/online order. Returns
    /// false when all three are already recorded; never enables an account.
    pub fn create_next_principal(&mut self) -> Result<bool> {
        self.bootstrap.create_next()
    }

    /// Materializes the owned transaction's State and final DACL. Releases this
    /// lease on return; OwnedSetupUpdate must reacquire and fully revalidate.
    /// A final readback failure may mean handoff committed; reopen, never guess.
    pub fn finish(self) -> Result<SetupRecord> {
        self.bootstrap.finish()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{cell::RefCell, rc::Rc};

    const OWNER: &str = "S-1-5-21-1-2-3-1001";
    const GROUP: &str = "S-1-5-21-1-2-3-1002";
    const OFFLINE: &str = "S-1-5-21-1-2-3-1003";
    const ONLINE: &str = "S-1-5-21-1-2-3-1004";

    #[test]
    fn bootstrap_sanitized_failure_keeps_numeric_api_reason_without_source_text() {
        let error = anyhow::Error::new(sam::NativeFailure {
            operation: "NetUserAdd1",
            status: 87,
            parameter: Some(7),
        })
        .context("private credential text must not be emitted")
        .context(FailureStep("create_principal"));
        assert_eq!(
            serde_json::to_value(sanitized_failure(&error).unwrap()).unwrap(),
            serde_json::json!({"step": "create_principal", "operation": "NetUserAdd1", "status": 87, "parameter": 7})
        );
        assert!(sanitized_failure(&anyhow::anyhow!("untyped secret text")).is_none());
    }

    #[derive(Default)]
    struct Disk {
        marker: Option<u32>,
        ownership: Option<Ownership>,
        state: Option<SetupRecord>,
        observed: Observed,
        final_acl: bool,
        events: Vec<&'static str>,
        operations: usize,
        fail_before: Option<usize>,
        fail_after: Option<usize>,
    }

    #[derive(Clone, Default)]
    struct Fake(Rc<RefCell<Disk>>);

    impl Fake {
        fn effect(&self, name: &'static str, mutate: impl FnOnce(&mut Disk)) -> Result<()> {
            let mut disk = self.0.borrow_mut();
            disk.operations += 1;
            ensure!(
                disk.fail_before != Some(disk.operations),
                "injected before-write failure"
            );
            mutate(&mut disk);
            disk.events.push(name);
            ensure!(
                disk.fail_after != Some(disk.operations),
                "injected after-write failure"
            );
            Ok(())
        }

        fn reopen(&self) -> Result<Bootstrap<Self>> {
            let disk = self.0.borrow();
            let value = disk
                .ownership
                .clone()
                .context("missing bootstrap ownership")?;
            let marker = disk.marker.context("missing marker")?;
            drop(disk);
            Bootstrap::resume(self.clone(), value, marker, OWNER)
        }
    }

    impl Store for Fake {
        fn observe(&self) -> Result<Observed> {
            Ok(self.0.borrow().observed.clone())
        }
        fn write_marker_zero(&mut self) -> Result<()> {
            self.effect("marker0", |disk| disk.marker = Some(0))
        }
        fn write_ownership(&mut self, value: &Ownership) -> Result<()> {
            value.validate()?;
            self.effect("ownership", |disk| disk.ownership = Some(value.clone()))
        }
        fn create_principal(&mut self, principal: Principal) -> Result<String> {
            let sid = match principal {
                Principal::Group => GROUP,
                Principal::Offline => OFFLINE,
                Principal::Online => ONLINE,
            };
            self.effect("create", |disk| {
                // A durable intent and every earlier SID precede this SAM call.
                let value = disk.ownership.as_ref().unwrap();
                assert_eq!(disk.marker, Some(0));
                assert_eq!(value.pending, Some(principal));
                disk.observed.validate(value).unwrap();
                match principal {
                    Principal::Group => disk.observed.group = Some(sid.into()),
                    Principal::Offline => disk.observed.offline = Some((sid.into(), true)),
                    Principal::Online => disk.observed.online = Some((sid.into(), true)),
                }
            })?;
            Ok(sid.into())
        }
        fn protect_final_root(&mut self, value: &Ownership) -> Result<()> {
            self.effect("final_acl", |disk| {
                assert_eq!(disk.ownership.as_ref(), Some(value));
                assert_eq!(value.phase, Phase::Finalizing);
                assert!(value.next().is_none());
                disk.final_acl = true;
            })
        }
        fn write_state(&mut self, value: &SetupRecord) -> Result<()> {
            self.effect("state", |disk| {
                assert!(disk.final_acl);
                if let Some(prior) = &disk.state {
                    assert_eq!(prior, value);
                }
                disk.state = Some(value.clone());
            })
        }
    }

    fn initial() -> Ownership {
        Ownership::new(OWNER, "0123456789abcdef0123456789abcdef".into()).unwrap()
    }

    fn complete(mut bootstrap: Bootstrap<Fake>) -> Result<SetupRecord> {
        if bootstrap.value.phase == Phase::Creating {
            while bootstrap.create_next()? {}
        }
        bootstrap.finish()
    }

    #[test]
    fn bootstrap_records_each_sid_before_next_create_and_never_marks_ready() {
        let fake = Fake::default();
        let record = complete(Bootstrap::start(fake.clone(), initial()).unwrap()).unwrap();
        let disk = fake.0.borrow();
        assert_eq!(
            disk.events,
            [
                "marker0",
                "ownership",
                "ownership",
                "create",
                "ownership",
                "ownership",
                "create",
                "ownership",
                "ownership",
                "create",
                "ownership",
                "ownership",
                "final_acl",
                "state",
                "ownership"
            ]
        );
        assert_eq!(disk.marker, Some(0));
        assert!(record.validate_ready(0, OWNER).is_err());
        assert!(
            record
                .offline
                .as_ref()
                .unwrap()
                .credential_generation
                .is_none()
        );
        assert!(
            record
                .online
                .as_ref()
                .unwrap()
                .credential_generation
                .is_none()
        );
        let value = disk.ownership.as_ref().unwrap();
        assert_eq!(value.phase, Phase::HandedOff);
        validate_handoff(Some(&serde_json::to_string(value).unwrap()), &record).unwrap();
    }

    #[test]
    fn bootstrap_interruptions_at_every_effect_preserve_or_refuse_exact_ownership() {
        for after in [false, true] {
            for operation in 1..=15 {
                let fake = Fake::default();
                if after {
                    fake.0.borrow_mut().fail_after = Some(operation);
                } else {
                    fake.0.borrow_mut().fail_before = Some(operation);
                }
                assert!(
                    Bootstrap::start(fake.clone(), initial())
                        .and_then(complete)
                        .is_err(),
                    "operation={operation}, after={after}"
                );
                let mut disk = fake.0.borrow_mut();
                assert_ne!(disk.marker, Some(SCHEMA_VERSION));
                assert!(
                    [&disk.observed.offline, &disk.observed.online]
                        .into_iter()
                        .flatten()
                        .all(|value| value.1)
                );
                let can_resume = disk.marker == Some(0)
                    && disk.ownership.as_ref().is_some_and(|value| {
                        value.phase != Phase::HandedOff && disk.observed.validate(value).is_ok()
                    });
                disk.fail_before = None;
                disk.fail_after = None;
                let previous_effects = disk.events.len();
                drop(disk);
                let reopened = fake.reopen();
                assert_eq!(
                    reopened.is_ok(),
                    can_resume,
                    "operation={operation}, after={after}"
                );
                assert_eq!(fake.0.borrow().events.len(), previous_effects);
                if let Ok(resumed) = reopened {
                    complete(resumed).unwrap();
                }
            }
        }
    }

    #[test]
    fn bootstrap_created_but_unrecorded_never_adopts_any_of_the_three_names() {
        for create_operation in [4, 7, 10] {
            let fake = Fake::default();
            fake.0.borrow_mut().fail_after = Some(create_operation);
            assert!(
                Bootstrap::start(fake.clone(), initial())
                    .and_then(complete)
                    .is_err()
            );
            let disk = fake.0.borrow();
            let value = disk.ownership.as_ref().unwrap();
            assert!(value.pending.is_some());
            assert!(disk.observed.validate(value).is_err());
            let effects = disk.events.len();
            drop(disk);
            assert!(fake.reopen().is_err());
            assert_eq!(fake.0.borrow().events.len(), effects);
        }
    }

    #[test]
    fn bootstrap_before_create_interruption_can_retry_only_when_name_absent() {
        let fake = Fake::default();
        fake.0.borrow_mut().fail_before = Some(4);
        assert!(
            Bootstrap::start(fake.clone(), initial())
                .and_then(complete)
                .is_err()
        );
        assert_eq!(
            fake.0.borrow().ownership.as_ref().unwrap().pending,
            Some(Principal::Group)
        );
        fake.0.borrow_mut().fail_before = None;
        complete(fake.reopen().unwrap()).unwrap();
        assert_eq!(
            fake.0
                .borrow()
                .events
                .iter()
                .filter(|event| **event == "create")
                .count(),
            3
        );
    }

    #[test]
    fn bootstrap_refuses_preexisting_names_without_writing_even_marker() {
        for principal in [Principal::Group, Principal::Offline, Principal::Online] {
            let fake = Fake::default();
            match principal {
                Principal::Group => fake.0.borrow_mut().observed.group = Some(GROUP.into()),
                Principal::Offline => {
                    fake.0.borrow_mut().observed.offline = Some((OFFLINE.into(), true))
                }
                Principal::Online => {
                    fake.0.borrow_mut().observed.online = Some((ONLINE.into(), true))
                }
            }
            assert!(Bootstrap::start(fake.clone(), initial()).is_err());
            assert!(fake.0.borrow().events.is_empty());
        }
    }

    #[test]
    fn bootstrap_refuses_missing_replaced_enabled_or_foreign_owned_principals() {
        let fake = Fake::default();
        let mut bootstrap = Bootstrap::start(fake.clone(), initial()).unwrap();
        bootstrap.create_next().unwrap();
        bootstrap.create_next().unwrap();
        let observed = fake.observe().unwrap();
        let value = bootstrap.value.clone();
        for changed in [
            None,
            Some(("S-1-5-21-1-2-3-9999".into(), true)),
            Some((OFFLINE.into(), false)),
        ] {
            fake.0.borrow_mut().observed.offline = changed;
            assert!(fake.reopen().is_err());
        }
        fake.0.borrow_mut().observed = observed;
        assert!(Bootstrap::resume(fake.clone(), value.clone(), 0, "S-1-5-21-1-2-3-9999").is_err());
        assert!(Bootstrap::resume(fake.clone(), value, 4, OWNER).is_err());
    }

    #[test]
    fn bootstrap_failed_write_poisons_object_and_requires_validated_reopen() {
        let fake = Fake::default();
        let mut bootstrap = Bootstrap::start(fake.clone(), initial()).unwrap();
        fake.0.borrow_mut().fail_after = Some(5);
        assert!(bootstrap.create_next().is_err());
        let effects = fake.0.borrow().events.len();
        assert!(bootstrap.create_next().is_err());
        assert_eq!(fake.0.borrow().events.len(), effects);
        fake.0.borrow_mut().fail_after = None;
        complete(fake.reopen().unwrap()).unwrap();
    }

    #[test]
    fn bootstrap_effect_failures_report_the_exact_safe_coordinator_step() {
        for (operation, expected) in [
            (3, "persist_create_intent"),
            (4, "create_principal"),
            (5, "persist_created_identity"),
        ] {
            let fake = Fake::default();
            let mut bootstrap = Bootstrap::start(fake.clone(), initial()).unwrap();
            fake.0.borrow_mut().fail_after = Some(operation);
            let error = bootstrap.create_next().unwrap_err();
            assert_eq!(sanitized_failure(&error).unwrap().step, Some(expected));
            assert!(bootstrap.poisoned);
        }
    }

    #[test]
    fn bootstrap_handoff_latch_blocks_owned_transaction_until_complete() {
        let fake = Fake::default();
        fake.0.borrow_mut().fail_after = Some(14);
        assert!(
            Bootstrap::start(fake.clone(), initial())
                .and_then(complete)
                .is_err()
        );
        let disk = fake.0.borrow();
        let json = serde_json::to_string(disk.ownership.as_ref().unwrap()).unwrap();
        assert!(validate_handoff(Some(&json), disk.state.as_ref().unwrap()).is_err());
        drop(disk);
        fake.0.borrow_mut().fail_after = None;
        let record = complete(fake.reopen().unwrap()).unwrap();
        let json = serde_json::to_string(fake.0.borrow().ownership.as_ref().unwrap()).unwrap();
        validate_handoff(Some(&json), &record).unwrap();
        let mut other = record;
        other.installation_id = "1123456789abcdef0123456789abcdef".into();
        assert!(validate_handoff(Some(&json), &other).is_err());
    }

    #[test]
    fn bootstrap_final_readback_failure_can_leave_completed_unready_handoff() {
        let fake = Fake::default();
        fake.0.borrow_mut().fail_after = Some(15);
        assert!(
            Bootstrap::start(fake.clone(), initial())
                .and_then(complete)
                .is_err()
        );
        let disk = fake.0.borrow();
        let json = serde_json::to_string(disk.ownership.as_ref().unwrap()).unwrap();
        validate_handoff(Some(&json), disk.state.as_ref().unwrap()).unwrap();
        assert_eq!(disk.marker, Some(0));
        drop(disk);
        assert!(fake.reopen().is_err());
    }

    #[test]
    fn bootstrap_requires_same_owner_elevation_and_no_pending_execution() {
        require_authority(OWNER, OWNER, true, false).unwrap();
        assert!(require_authority(OWNER, OWNER, false, false).is_err());
        assert!(require_authority(OWNER, OWNER, true, true).is_err());
        assert!(require_authority(OWNER, "S-1-5-21-1-2-3-9999", true, false).is_err());
        assert!(require_authority("S-1-5-18", "S-1-5-18", true, false).is_err());
    }

    #[test]
    fn bootstrap_partial_record_rejects_forged_stage_intent_and_policy() {
        let mut value = initial();
        value.pending = Some(Principal::Online);
        assert!(value.validate().is_err());
        value.pending = None;
        value.phase = Phase::HandedOff;
        assert!(value.validate().is_err());
        value.phase = Phase::Creating;
        value.record.phase = SetupPhase::Ready;
        assert!(value.validate().is_err());
        let json = serde_json::to_string(&initial())
            .unwrap()
            .replace("\"version\":1", "\"version\":1,\"adopt\":true");
        assert!(serde_json::from_str::<Ownership>(&json).is_err());
    }
}
