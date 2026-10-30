//! Explicitly opted-in qualification on a fresh GitHub-hosted Windows VM only.
//! Accounts stay disabled and marker zero. There is no logon, launch, readiness
//! commit, removal, or local override. The entire VM is discarded after the job;
//! native ownership records and policy are preserved on success and failure.

use super::*;
use crate::account_install::{
    ACCOUNT_GROUP, FirstInstall, OFFLINE_SUBLAYER, OFFLINE_USER, ONLINE_USER,
};
use std::collections::BTreeSet;
use std::io::{Seek, SeekFrom};
use windows::Win32::Foundation::{ERROR_FILE_NOT_FOUND, HANDLE};
use windows::Win32::NetworkManagement::NetManagement::*;
use windows::Win32::NetworkManagement::WindowsFilteringPlatform::*;
use windows::Win32::System::Registry::*;
use windows::core::PCWSTR;

const OPT_IN: &str = "github-hosted-disabled-accounts-only";
const TEST_NAME: &str = "candidate_disposable_host_disabled_setup_native_effects";

#[derive(Clone)]
struct Admission {
    opt_in: Option<String>,
    github_actions: Option<String>,
    environment: Option<String>,
    runner_os: Option<String>,
    repository: Option<String>,
    event: Option<String>,
    run_id: Option<String>,
    run_attempt: Option<String>,
    windows_x64: bool,
}

impl Admission {
    fn from_environment() -> Self {
        let env = |name| std::env::var(name).ok();
        Self {
            opt_in: env("ALYSIS_RUN_DISPOSABLE_ACCOUNT_SETUP"),
            github_actions: env("GITHUB_ACTIONS"),
            environment: env("RUNNER_ENVIRONMENT"),
            runner_os: env("RUNNER_OS"),
            repository: env("GITHUB_REPOSITORY"),
            event: env("GITHUB_EVENT_NAME"),
            run_id: env("GITHUB_RUN_ID"),
            run_attempt: env("GITHUB_RUN_ATTEMPT"),
            windows_x64: cfg!(all(windows, target_arch = "x86_64")),
        }
    }

    fn validate(&self) -> Result<()> {
        // These operator/CI gates prevent accidental local execution; environment
        // variables are not cryptographic proof of a disposable host. The caller
        // must select a standard GitHub-hosted image in a dedicated manual job.
        ensure!(self.windows_x64, "disposable setup requires Windows x64");
        for (actual, expected) in [
            (&self.opt_in, OPT_IN),
            (&self.github_actions, "true"),
            (&self.environment, "github-hosted"),
            (&self.runner_os, "Windows"),
            (&self.repository, "AlysisAi/alysis-code-internal"),
            (&self.event, "workflow_dispatch"),
        ] {
            ensure!(
                actual.as_deref() == Some(expected),
                "disposable setup CI admission refused"
            );
        }
        for value in [&self.run_id, &self.run_attempt] {
            ensure!(
                value.as_deref().is_some_and(|value| !value.is_empty()
                    && value.len() <= 32
                    && value.bytes().all(|byte| byte.is_ascii_digit())),
                "disposable setup requires a bounded CI run identity"
            );
        }
        Ok(())
    }
}

#[derive(Serialize)]
struct Evidence {
    schema: u32,
    test: &'static str,
    run_id: String,
    run_attempt: String,
    current_stage: &'static str,
    completed_stages: Vec<&'static str>,
    passed: bool,
    failed: bool,
    failure_hresult: Option<i32>,
    failure_os_code: Option<i32>,
    failure_bootstrap: Option<super::super::bootstrap::SanitizedFailure>,
    failure_account_check: Option<AccountCheckFailure>,
    failure_account_observations: Vec<AccountObservation>,
    account_enablement_attempted: bool,
    accounts_verified_disabled_at_completion: bool,
    marker_zero_verified_at_completion: bool,
    no_logon_or_launch_attempted: bool,
    no_ready_commit_attempted: bool,
    resources_preserved_for_vm_disposal: bool,
}

struct Report {
    file: std::fs::File,
    value: Evidence,
}

impl Report {
    fn create(admission: &Admission) -> Result<Self> {
        let parent = std::env::var_os("RUNNER_TEMP").context("RUNNER_TEMP is required")?;
        let parent = std::fs::canonicalize(parent)?;
        ensure!(
            parent.is_dir() && parent.is_absolute(),
            "invalid CI evidence parent"
        );
        let file = std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(parent.join("alysis-disabled-setup-native-effects.json"))?;
        let mut report = Self {
            file,
            value: Evidence {
                schema: 1,
                test: TEST_NAME,
                run_id: admission.run_id.clone().unwrap(),
                run_attempt: admission.run_attempt.clone().unwrap(),
                current_stage: "admitted",
                completed_stages: Vec::new(),
                passed: false,
                failed: false,
                failure_hresult: None,
                failure_os_code: None,
                failure_bootstrap: None,
                failure_account_check: None,
                failure_account_observations: Vec::new(),
                account_enablement_attempted: false,
                accounts_verified_disabled_at_completion: false,
                marker_zero_verified_at_completion: false,
                no_logon_or_launch_attempted: true,
                no_ready_commit_attempted: true,
                resources_preserved_for_vm_disposal: true,
            },
        };
        report.save()?;
        Ok(report)
    }

    fn save(&mut self) -> Result<()> {
        self.file.seek(SeekFrom::Start(0))?;
        self.file.set_len(0)?;
        serde_json::to_writer_pretty(&mut self.file, &self.value)?;
        self.file.sync_all()?;
        Ok(())
    }

    fn step<T>(&mut self, name: &'static str, operation: impl FnOnce() -> Result<T>) -> Result<T> {
        self.value.current_stage = name;
        self.save()?;
        match operation() {
            Ok(value) => {
                self.value.completed_stages.push(name);
                self.save()?;
                Ok(value)
            }
            Err(error) => {
                self.value.failed = true;
                self.value.failure_hresult = error
                    .chain()
                    .find_map(|cause| cause.downcast_ref::<windows::core::Error>())
                    .map(|error| error.code().0);
                self.value.failure_os_code = error
                    .chain()
                    .find_map(|cause| cause.downcast_ref::<std::io::Error>())
                    .and_then(|error| error.raw_os_error());
                self.value.failure_bootstrap = super::super::bootstrap::sanitized_failure(&error);
                self.value.failure_account_check =
                    error.downcast_ref::<AccountCheckFailure>().cloned();
                // Read-only fixed-name SAM observations. Do not adopt, repair,
                // enable, remove, read passwords/profile fields, or log SIDs.
                self.value.failure_account_observations = observe_failure_accounts();
                self.save()?;
                // Do not print arbitrary native/serde messages, captured data,
                // account records, credential plans, or secret buffers.
                anyhow::bail!(
                    "disposable native setup failed at stage {name}; inspect sanitized evidence"
                )
            }
        }
    }
}

#[derive(Clone, Debug, Serialize)]
struct AccountCheckFailure {
    check: &'static str,
    actual: u32,
    expected: u32,
}
impl std::fmt::Display for AccountCheckFailure {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(formatter, "candidate account check {} differs", self.check)
    }
}
impl std::error::Error for AccountCheckFailure {}

fn account_check(check: &'static str, actual: u32, expected: u32) -> Result<()> {
    if actual != expected {
        return Err(AccountCheckFailure {
            check,
            actual,
            expected,
        }
        .into());
    }
    Ok(())
}

#[derive(Serialize)]
struct AccountObservation {
    mode: &'static str,
    net_user_get_info_status: u32,
    exists: Option<bool>,
    flags: Option<u32>,
    disabled: Option<bool>,
}

fn observe_failure_accounts() -> Vec<AccountObservation> {
    [("offline", OFFLINE_USER), ("online", ONLINE_USER)]
        .into_iter()
        .map(|(mode, name)| {
            let name = crate::util::wstr(name);
            let mut raw = std::ptr::null_mut();
            let status =
                unsafe { NetUserGetInfo(PCWSTR::null(), crate::util::pcwstr(&name), 4, &mut raw) };
            let flags = if status == 0 && !raw.is_null() {
                // No pointer/string fields are read, even after a failed setup.
                Some(unsafe { (*raw.cast::<USER_INFO_4>()).usri4_flags.0 })
            } else {
                None
            };
            if !raw.is_null() {
                unsafe {
                    let _ = NetApiBufferFree(Some(raw.cast()));
                }
            }
            AccountObservation {
                mode,
                net_user_get_info_status: status,
                exists: match status {
                    0 => Some(true),
                    value if value == NERR_UserNotFound => Some(false),
                    _ => None,
                },
                flags,
                disabled: flags.map(|flags| flags & UF_ACCOUNTDISABLE.0 != 0),
            }
        })
        .collect()
}

fn require_registry_absent(path: &str) -> Result<()> {
    let wide = crate::util::wstr(path);
    let mut raw = HKEY::default();
    let status = unsafe {
        RegOpenKeyExW(
            HKEY_LOCAL_MACHINE,
            crate::util::pcwstr(&wide),
            Some(REG_OPTION_OPEN_LINK.0),
            KEY_READ | KEY_WOW64_64KEY,
            &mut raw,
        )
    };
    if status.is_ok() {
        unsafe {
            let _ = RegCloseKey(raw);
        }
        anyhow::bail!("disposable setup refuses an existing registry root or link");
    }
    ensure!(
        status == ERROR_FILE_NOT_FOUND,
        "cannot establish registry root absence"
    );
    Ok(())
}

fn require_fence_absent() -> Result<()> {
    struct Engine(HANDLE);
    impl Drop for Engine {
        fn drop(&mut self) {
            unsafe {
                let _ = FwpmEngineClose0(self.0);
            }
        }
    }
    let mut raw = HANDLE::default();
    let code = unsafe { FwpmEngineOpen0(PCWSTR::null(), 0xffff_ffff, None, None, &mut raw) };
    ensure!(code == 0, "cannot inspect disposable WFP namespace");
    let engine = Engine(raw);
    let mut layer = std::ptr::null_mut();
    let code = unsafe { FwpmSubLayerGetByKey0(engine.0, &OFFLINE_SUBLAYER, &mut layer) };
    if !layer.is_null() {
        unsafe {
            FwpmFreeMemory0(&mut (layer.cast()));
        }
    }
    ensure!(
        code == 0x8032_0007,
        "disposable setup refuses existing or unreadable WFP namespace"
    );
    Ok(())
}

fn preflight() -> Result<()> {
    ensure!(
        is_elevated()?,
        "disposable setup needs an elevated runner token"
    );
    crate::local_account_host::require_local_sam()?;
    require_registry_absent(REGISTRY_ROOT)?;
    require_registry_absent(crate::reg::BASE)?;
    for name in [OFFLINE_USER, ONLINE_USER, crate::user::SANDBOX_USER] {
        ensure!(
            crate::user::local_user_sid(name)?.is_none(),
            "disposable setup refuses existing account names"
        );
    }
    for name in [ACCOUNT_GROUP, crate::user::SANDBOX_GROUP] {
        ensure!(
            crate::user::local_group_sid(name)?.is_none(),
            "disposable setup refuses existing group names"
        );
    }
    ensure!(
        !crate::account_state::pending_journal_path()?.try_exists()?,
        "disposable setup refuses pending execution"
    );
    require_fence_absent()
}

fn verify_disabled_accounts(record: &SetupRecord, policy_can_exist: bool) -> Result<()> {
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
    record.validate_layout()?;
    ensure!(
        record.phase == SetupPhase::Provisioning,
        "candidate unexpectedly left provisioning"
    );
    ensure!(
        record.owner_sid == crate::sid::current_user_sid()?,
        "candidate owner changed"
    );
    if let Some(group) = &record.group {
        ensure!(
            crate::user::local_group_sid(ACCOUNT_GROUP)?.as_deref() == Some(group.sid.as_str()),
            "candidate group SID changed"
        );
    }
    for account in [&record.offline, &record.online].into_iter().flatten() {
        let name = crate::util::wstr(&account.name);
        let mut raw = std::ptr::null_mut();
        let code =
            unsafe { NetUserGetInfo(PCWSTR::null(), crate::util::pcwstr(&name), 4, &mut raw) };
        let raw = NetBuffer(raw);
        account_check("NetUserGetInfo4_status", code, 0)?;
        account_check("NetUserGetInfo4_buffer", u32::from(!raw.0.is_null()), 1)?;
        let info = unsafe { &*raw.0.cast::<USER_INFO_4>() };
        // Deliberately never read usri4_password or any profile/script field.
        let required =
            UF_NORMAL_ACCOUNT | UF_SCRIPT.0 | UF_DONT_EXPIRE_PASSWD.0 | UF_ACCOUNTDISABLE.0;
        account_check(
            "recorded_sid_matches",
            u32::from(crate::sid::psid_to_string(info.usri4_user_sid)? == account.sid),
            1,
        )?;
        account_check("required_flags", info.usri4_flags.0 & required, required)?;
        account_check(
            "unexpected_flags",
            info.usri4_flags.0 & !(required | UF_PASSWD_CANT_CHANGE.0),
            0,
        )?;
        let groups = super::super::activation::memberships(&account.name)?;
        let users = super::super::activation::USERS;
        let group = record.group.as_ref().context("candidate group missing")?;
        let allowed = if policy_can_exist {
            BTreeSet::from([users.to_owned(), group.sid.clone()])
        } else {
            BTreeSet::from([users.to_owned()])
        };
        account_check(
            "allowed_memberships",
            u32::from(groups.is_subset(&allowed)),
            1,
        )?;
        account_check(
            "membership_order",
            u32::from(!groups.contains(&group.sid) || groups.contains(users)),
            1,
        )?;
        let expected_privilege = if groups.contains(users) {
            USER_PRIV_USER
        } else {
            USER_PRIV_GUEST
        };
        account_check(
            "membership_privilege",
            info.usri4_priv.0,
            expected_privilege.0,
        )?;
        account_check("operator_flags", info.usri4_auth_flags.0, 0)?;
        account_check("password_expired", info.usri4_password_expired, 0)?;
    }
    Ok(())
}

fn verify_bootstrap(record: &SetupRecord) -> Result<()> {
    verify_disabled_accounts(record, false)?;
    let key = crate::reg::Key::open(REGISTRY_ROOT, false)?.context("bootstrap key disappeared")?;
    ensure!(
        key.get_dword("MarkerVersion")? == Some(0),
        "bootstrap marker is not zero"
    );
    let value: serde_json::Value = serde_json::from_str(
        &key.get_sz("Bootstrap")?
            .context("bootstrap record disappeared")?,
    )?;
    ensure!(
        serde_json::from_value::<SetupRecord>(value["record"].clone())? == *record,
        "bootstrap record changed"
    );
    Ok(())
}

fn verify_owned(record: &SetupRecord) -> Result<crate::reg::Key> {
    verify_disabled_accounts(record, true)?;
    let key = crate::reg::Key::open(REGISTRY_ROOT, false)?.context("owned key disappeared")?;
    let binding = record.binding(AccountMode::Offline)?;
    key.validate_credential_security(&binding.owner_sid, &binding.account_sid, &binding.group_sid)?;
    ensure!(
        key.get_dword("MarkerVersion")? == Some(0),
        "owned marker is not zero"
    );
    ensure!(
        read_json::<SetupRecord>(&key, "State")?.as_ref() == Some(record),
        "owned record changed"
    );
    super::super::bootstrap::validate_handoff(key.get_sz("Bootstrap")?.as_deref(), record)?;
    Ok(key)
}

fn verify_account_policy(record: &SetupRecord) -> Result<()> {
    let key = verify_owned(record)?;
    super::super::policy::require_complete(
        record,
        key.get_sz(super::super::policy::VALUE)?.as_deref(),
    )?;
    let group = record.group.as_ref().context("candidate group missing")?;
    let activation = super::super::activation::DENIALS
        .into_iter()
        .map(str::to_owned)
        .collect();
    ensure!(
        super::super::activation::rights(&group.sid)? == activation,
        "candidate group rights differ"
    );
    let accounts = [AccountMode::Offline, AccountMode::Online];
    let members = accounts
        .iter()
        .map(|mode| record.binding(*mode).map(|binding| binding.account_sid))
        .collect::<Result<BTreeSet<_>>>()?;
    ensure!(
        super::super::activation::group_members(ACCOUNT_GROUP)? == members,
        "candidate membership differs"
    );
    for mode in accounts {
        let binding = record.binding(mode)?;
        ensure!(
            super::super::activation::memberships(&binding.account_name)?
                == BTreeSet::from([
                    super::super::activation::USERS.to_owned(),
                    group.sid.clone()
                ]),
            "candidate account groups differ"
        );
        ensure!(
            super::super::activation::rights(&binding.account_sid)?.is_empty(),
            "candidate account has direct LSA rights"
        );
    }
    Ok(())
}

struct Secret(String);
impl Secret {
    fn random() -> Result<Self> {
        use windows::Win32::Security::Cryptography::{
            BCRYPT_USE_SYSTEM_PREFERRED_RNG, BCryptGenRandom,
        };
        let mut bytes = [0u8; 32];
        let status = unsafe { BCryptGenRandom(None, &mut bytes, BCRYPT_USE_SYSTEM_PREFERRED_RNG) };
        let mut value = Self(String::with_capacity(68));
        if status.is_ok() {
            value.0.push_str("Aa1!");
            for byte in &bytes {
                use std::fmt::Write;
                write!(&mut value.0, "{byte:02x}")?;
            }
        }
        for byte in &mut bytes {
            unsafe {
                std::ptr::write_volatile(byte, 0);
            }
        }
        std::sync::atomic::compiler_fence(std::sync::atomic::Ordering::SeqCst);
        ensure!(status.is_ok(), "disposable secret entropy failed");
        Ok(value)
    }
}
impl Drop for Secret {
    fn drop(&mut self) {
        unsafe {
            for byte in self.0.as_mut_vec() {
                std::ptr::write_volatile(byte, 0);
            }
        }
        std::sync::atomic::compiler_fence(std::sync::atomic::Ordering::SeqCst);
    }
}

fn stage_and_apply(update: &mut OwnedSetupUpdate, mode: AccountMode) -> Result<()> {
    verify_owned(update.record())?;
    let secret = Secret::random()?;
    let binding = update.record().binding(mode)?;
    let plan = CredentialGenerationPlan::prepare(&binding, &secret.0)?;
    drop(secret);
    update.stage_credential(mode, &plan)?;
    crate::account_credentials::verify_stored_generation(&binding, &plan.reference)?;
    let key = verify_owned(update.record())?;
    let ledger: CredentialLedger =
        read_json(&key, "CredentialLedger")?.context("credential inventory missing")?;
    ledger.validate(update.record())?;
    ensure!(
        ledger.entries == update.credential_inventory(),
        "credential inventory changed"
    );
    update.apply_staged_password(mode)?;
    let key = verify_owned(update.record())?;
    crate::account_credentials::password::require_applied(
        update.record(),
        mode,
        key.get_sz(crate::account_credentials::password::value_name(mode))?
            .as_deref(),
    )?;
    Ok(())
}

fn qualify(report: &mut Report) -> Result<()> {
    report.step("preflight_absence", preflight)?;
    let owner = report.step("read_owner_sid", crate::sid::current_user_sid)?;
    let mut bootstrap = report.step("bootstrap_root", || FirstInstall::create(&owner))?;
    for (create_stage, verify_stage) in [
        ("bootstrap_group", "bootstrap_group_invariants"),
        ("bootstrap_offline_disabled", "bootstrap_offline_invariants"),
        ("bootstrap_online_disabled", "bootstrap_online_invariants"),
    ] {
        report.step(create_stage, || {
            ensure!(
                bootstrap.create_next_principal()?,
                "bootstrap did not create the expected principal"
            );
            Ok(())
        })?;
        report.step(verify_stage, || verify_bootstrap(bootstrap.record()))?;
        let expected = bootstrap.record().clone();
        drop(bootstrap);
        bootstrap = report.step("bootstrap_cold_reopen", || {
            let value = FirstInstall::resume(&owner)?;
            ensure!(
                *value.record() == expected,
                "bootstrap cold reopen changed identity"
            );
            verify_bootstrap(value.record())?;
            Ok(value)
        })?;
    }
    let record = report.step("bootstrap_handoff", || {
        ensure!(
            !bootstrap.create_next_principal()?,
            "bootstrap created an extra principal"
        );
        let record = bootstrap.finish()?;
        verify_owned(&record)?;
        Ok(record)
    })?;
    report.step("fresh_install_refuses_existing", || {
        ensure!(
            FirstInstall::create(&owner).is_err(),
            "first install adopted existing state"
        );
        verify_owned(&record)?;
        Ok(())
    })?;
    let mut update = report.step("owned_update_open", || {
        OwnedSetupUpdate::begin(UpdatePurpose::Repair)
    })?;
    report.step("program_files_storage_birth", || {
        verify_owned(update.record())?;
        update.prepare_storage()?;
        ensure!(
            update
                .record()
                .storage
                .as_ref()
                .is_some_and(|value| value.is_complete()),
            "storage not attested"
        );
        verify_owned(update.record())?;
        Ok(())
    })?;
    let storage = update.record().storage.clone();
    drop(update);
    let mut update = report.step("storage_cold_reopen", || {
        let mut value = OwnedSetupUpdate::begin(UpdatePurpose::Repair)?;
        verify_owned(value.record())?;
        value.prepare_storage()?;
        ensure!(
            value.record().storage == storage,
            "storage identity changed on reopen"
        );
        verify_owned(value.record())?;
        Ok(value)
    })?;
    // Install denials before memberships, then apply credentials only to the
    // still-disabled ordinary Users accounts. No early bootstrap membership.
    report.step("disabled_account_policy", || {
        verify_owned(update.record())?;
        update.prepare_account_policy()?;
        verify_account_policy(update.record())
    })?;
    report.step("disabled_account_policy_owned_retry", || {
        verify_account_policy(update.record())?;
        update.prepare_account_policy()?;
        verify_account_policy(update.record())
    })?;
    report.step("offline_initial_password", || {
        stage_and_apply(&mut update, AccountMode::Offline)
    })?;
    report.step("online_initial_password", || {
        stage_and_apply(&mut update, AccountMode::Online)
    })?;
    let initial_inventory = report.step("initial_inventory_verified", || {
        verify_owned(update.record())?;
        ensure!(
            update.credential_inventory().len() == 2,
            "initial credential inventory differs"
        );
        Ok(update.credential_inventory().to_vec())
    })?;
    drop(update);
    let mut update = report.step("credentials_cold_reopen", || {
        OwnedSetupUpdate::begin(UpdatePurpose::Repair)
    })?;
    report.step("offline_password_rotation", || {
        stage_and_apply(&mut update, AccountMode::Offline)
    })?;
    report.step("online_password_rotation", || {
        stage_and_apply(&mut update, AccountMode::Online)
    })?;
    report.step("old_generation_inventory_retained", || {
        ensure!(
            update.credential_inventory().len() == 4
                && initial_inventory
                    .iter()
                    .all(|entry| update.credential_inventory().contains(entry)),
            "rotation lost owned generation inventory"
        );
        for entry in update.credential_inventory() {
            crate::account_credentials::verify_stored_generation(&entry.binding, &entry.reference)?;
        }
        verify_owned(update.record())?;
        Ok(())
    })?;
    report.step("offline_fence_native_transaction", || {
        verify_account_policy(update.record())?;
        update.prepare_offline_fence()?;
        let key = verify_owned(update.record())?;
        super::super::activation::wfp::require_complete(
            update.record(),
            key.get_sz("WfpOwnership")?.as_deref(),
        )
    })?;
    let record = update.record().clone();
    drop(update);
    report.step("offline_fence_cold_owned_retry", || {
        let mut update = OwnedSetupUpdate::begin(UpdatePurpose::Repair)?;
        ensure!(
            *update.record() == record,
            "fence cold reopen changed ownership"
        );
        verify_account_policy(update.record())?;
        // Production prepare reopens BFE and revalidates every exact filter,
        // action, principal, namespace key and DACL inside its transaction.
        update.prepare_offline_fence()?;
        verify_account_policy(update.record())?;
        let key = verify_owned(update.record())?;
        let before = key.get_sz("WfpOwnership")?;
        super::super::activation::wfp::require_complete(update.record(), before.as_deref())?;
        ensure!(
            update.remove_offline_fence().is_err(),
            "unguarded WFP removal became available"
        );
        ensure!(
            key.get_sz("WfpOwnership")? == before,
            "refused removal changed ownership"
        );
        Ok(())
    })?;
    report.step("final_disabled_unready_state", || {
        let lease = crate::execution_lease::ExecutionLease::acquire()?;
        verify_owned(&record)?;
        verify_account_policy(&record)?;
        ensure!(
            crate::account_install::ReadyInstallation::read(&lease).is_err(),
            "disabled setup became executable"
        );
        ensure!(
            !crate::account_state::pending_journal_path()?.try_exists()?,
            "unexpected execution journal"
        );
        Ok(())
    })?;
    report.value.accounts_verified_disabled_at_completion = true;
    report.value.marker_zero_verified_at_completion = true;
    report.value.passed = true;
    report.value.current_stage = "complete_disabled_unready";
    report.save()
}

#[test]
#[ignore = "mutates fresh disposable GitHub-hosted Windows VM only; explicit manual opt-in required; NEVER run locally"]
fn candidate_disposable_host_disabled_setup_native_effects() -> Result<()> {
    let admission = Admission::from_environment();
    admission.validate()?; // Must precede even evidence-file or lease creation.
    let mut report = Report::create(&admission)?;
    qualify(&mut report)
}

#[test]
fn candidate_disposable_failure_diagnostics_exclude_raw_error_text() {
    let error = account_check("required_flags", 2, 3)
        .unwrap_err()
        .context("secret password and arbitrary native text must never be serialized");
    let detail = error.downcast_ref::<AccountCheckFailure>().unwrap();
    assert_eq!(
        serde_json::to_value(detail).unwrap(),
        serde_json::json!({
            "check": "required_flags", "actual": 2, "expected": 3,
        })
    );
    assert!(super::super::bootstrap::sanitized_failure(&error).is_none());
    let unknown = anyhow::anyhow!("untyped private credential material");
    assert!(unknown.downcast_ref::<AccountCheckFailure>().is_none());
    assert!(super::super::bootstrap::sanitized_failure(&unknown).is_none());
}

#[test]
fn candidate_disposable_host_admission_rejects_local_and_implicit_execution() {
    let approved = Admission {
        opt_in: Some(OPT_IN.into()),
        github_actions: Some("true".into()),
        environment: Some("github-hosted".into()),
        runner_os: Some("Windows".into()),
        repository: Some("AlysisAi/alysis-code-internal".into()),
        event: Some("workflow_dispatch".into()),
        run_id: Some("123456".into()),
        run_attempt: Some("1".into()),
        windows_x64: true,
    };
    assert!(approved.validate().is_ok());
    for index in 0..9 {
        for invalid in [None, Some("unexpected".to_owned()), Some(String::new())] {
            let mut value = approved.clone();
            match index {
                0 => value.opt_in = invalid,
                1 => value.github_actions = invalid,
                2 => value.environment = invalid,
                3 => value.runner_os = invalid,
                4 => value.repository = invalid,
                5 => value.event = invalid,
                6 => value.run_id = invalid,
                7 => value.run_attempt = invalid,
                _ => value.windows_x64 = false,
            }
            assert!(value.validate().is_err());
        }
    }
    for event in ["push", "pull_request", "pull_request_target", "schedule"] {
        let mut value = approved.clone();
        value.event = Some(event.into());
        assert!(value.validate().is_err());
    }
    let mut local = approved;
    local.environment = Some("self-hosted".into());
    assert!(local.validate().is_err());
}
