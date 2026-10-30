//! Private candidate credential plumbing. There is intentionally no password
//! accessor or generic runner/Exec adapter, including for the online identity.

use crate::account_install::{AccountBinding, AccountMode, REGISTRY_ROOT, ReadyInstallation};
use anyhow::{Context, Result, ensure};
use serde::{Deserialize, Serialize};

#[path = "account_password.rs"]
pub(crate) mod password;

pub(crate) fn generation_path(
    installation: &str,
    mode: AccountMode,
    reference: &str,
) -> Result<String> {
    crate::account_install::validate_random_id(installation)?;
    let suffix = reference
        .strip_prefix("CredOwner-")
        .context("invalid candidate credential reference")?;
    crate::account_install::validate_random_id(suffix)?;
    Ok(format!(
        r"{REGISTRY_ROOT}\{installation}\{}\{reference}",
        mode.directory()
    ))
}

/// Preparation only: the later elevated installer must create `path` with this
/// descriptor atomically, refuse an existing key, validate the opened key, and
/// only then persist encrypted_blob. Nothing here touches HKLM/SAM/WFP.
pub struct CredentialGenerationPlan {
    pub reference: String,
    pub path: String,
    pub security_descriptor: String,
    pub encrypted_blob: Vec<u8>,
}

impl CredentialGenerationPlan {
    pub(crate) fn validate_for(&self, binding: &AccountBinding) -> Result<()> {
        ensure!(
            self.path == generation_path(&binding.installation_id, binding.mode, &self.reference)?,
            "candidate credential plan path does not match its identity"
        );
        ensure!(
            self.security_descriptor
                == crate::reg::credential_key_sddl(
                    &binding.owner_sid,
                    &binding.account_sid,
                    &binding.group_sid
                )?,
            "candidate credential plan security differs from its identity"
        );
        decode_blob(&self.encrypted_blob, binding, &self.reference)?;
        Ok(())
    }

    pub(crate) fn write_fresh(&self, binding: &AccountBinding) -> Result<()> {
        self.validate_for(binding)?;
        let (key, created) = crate::reg::Key::create(&self.path, Some(&self.security_descriptor))?;
        Self::require_fresh(created)?;
        key.validate_credential_security(
            &binding.owner_sid,
            &binding.account_sid,
            &binding.group_sid,
        )?;
        key.set_binary("Blob", &self.encrypted_blob)?;
        // A later password setter must never rely on a generation that exists
        // only in the registry cache. Flush this fresh object before publishing
        // it as the selected generation or allowing any SAM mutation.
        key.flush()?;
        ensure!(
            key.get_binary("Blob")?.as_ref() == Some(&self.encrypted_blob),
            "candidate credential write verification failed"
        );
        Ok(())
    }

    pub fn prepare(binding: &AccountBinding, password: &str) -> Result<Self> {
        use windows::Win32::Security::Cryptography::{
            BCRYPT_USE_SYSTEM_PREFERRED_RNG, BCryptGenRandom,
        };
        binding.validate()?;
        validate_password(password)?;
        let mut random = [0u8; 16];
        unsafe {
            BCryptGenRandom(None, &mut random, BCRYPT_USE_SYSTEM_PREFERRED_RNG).ok()?;
        }
        let reference = format!(
            "CredOwner-{}",
            random
                .iter()
                .map(|byte| format!("{byte:02x}"))
                .collect::<String>()
        );
        let path = generation_path(&binding.installation_id, binding.mode, &reference)?;
        let security_descriptor = crate::reg::credential_key_sddl(
            &binding.owner_sid,
            &binding.account_sid,
            &binding.group_sid,
        )?;
        let payload = CredentialPayload {
            version: 1,
            binding: binding.clone(),
            generation: reference.clone(),
            password: password.into(),
        };
        let encoded = SecretBytes(serde_json::to_vec(&payload)?);
        let encrypted_blob = crate::dpapi::protect_machine(&encoded.0)?;
        Ok(Self {
            reference,
            path,
            security_descriptor,
            encrypted_blob,
        })
    }

    /// DACL tightening cannot revoke old open handles. A collision must never
    /// be accepted even if the old object's current DACL looks correct.
    pub fn require_fresh(created: bool) -> Result<()> {
        ensure!(
            created,
            "candidate credential generation already exists; refusing reuse"
        );
        Ok(())
    }
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct CredentialPayload {
    version: u32,
    binding: AccountBinding,
    generation: String,
    password: String,
}
impl Drop for CredentialPayload {
    fn drop(&mut self) {
        unsafe {
            scrub(self.password.as_mut_vec());
        }
    }
}

struct SecretBytes(Vec<u8>);
impl Drop for SecretBytes {
    fn drop(&mut self) {
        scrub(&mut self.0);
    }
}
fn scrub(bytes: &mut [u8]) {
    for byte in bytes {
        unsafe {
            std::ptr::write_volatile(byte, 0);
        }
    }
    std::sync::atomic::compiler_fence(std::sync::atomic::Ordering::SeqCst);
}

fn validate_password(password: &str) -> Result<()> {
    ensure!(
        !password.is_empty() && !password.contains('\0') && password.encode_utf16().count() <= 127,
        "candidate credential has invalid password shape"
    );
    Ok(())
}

struct Credential {
    user: String,
    password: String,
}
impl Drop for Credential {
    fn drop(&mut self) {
        unsafe {
            scrub(self.password.as_mut_vec());
        }
    }
}

fn decode_blob(blob: &[u8], expected: &AccountBinding, generation: &str) -> Result<Credential> {
    expected.validate()?;
    generation_path(&expected.installation_id, expected.mode, generation)?;
    let decoded = SecretBytes(crate::dpapi::unprotect(blob)?);
    let mut payload: CredentialPayload =
        serde_json::from_slice(&decoded.0).context("invalid candidate credential payload")?;
    ensure!(
        payload.version == 1 && payload.binding == *expected && payload.generation == generation,
        "candidate credential identity/generation mismatch"
    );
    validate_password(&payload.password)?;
    Ok(Credential {
        user: expected.account_name.clone(),
        password: std::mem::take(&mut payload.password),
    })
}

pub(crate) fn verify_stored_generation(binding: &AccountBinding, generation: &str) -> Result<()> {
    read_stored_credential(binding, generation)?;
    Ok(())
}

fn read_stored_credential(binding: &AccountBinding, generation: &str) -> Result<Credential> {
    let path = generation_path(&binding.installation_id, binding.mode, generation)?;
    let key = crate::reg::Key::open(&path, false)?.context("candidate credential key missing")?;
    key.validate_credential_security(&binding.owner_sid, &binding.account_sid, &binding.group_sid)?;
    let blob = key
        .get_binary("Blob")?
        .context("candidate credential blob missing")?;
    ensure!(
        blob.len() <= 64 * 1024,
        "candidate credential blob exceeds size limit"
    );
    decode_blob(&blob, binding, generation)
}

struct SecretWide(Vec<u16>);
impl Drop for SecretWide {
    fn drop(&mut self) {
        for value in &mut self.0 {
            unsafe {
                std::ptr::write_volatile(value, 0);
            }
        }
        std::sync::atomic::compiler_fence(std::sync::atomic::Ordering::SeqCst);
    }
}

/// Fixed activation proof only: no command, impersonation callback, credential
/// accessor or escaping token. The online identity cannot reach legacy Exec.
/// This runs only from the elevated candidate setup verifier under its lease.
pub(crate) fn verify_activation_logons(binding: &AccountBinding, generation: &str) -> Result<()> {
    crate::account_impersonation::require_process_identity()?;
    use crate::util::{OwnedHandle, pcwstr, wstr};
    use windows::Win32::{Foundation::HANDLE, Security::*};
    let credential = read_stored_credential(binding, generation)?;
    let name = wstr(&credential.user);
    let password = SecretWide(wstr(&credential.password));
    let mut raw = HANDLE::default();
    unsafe {
        LogonUserW(
            pcwstr(&name),
            windows::core::w!("."),
            pcwstr(&password.0),
            LOGON32_LOGON_INTERACTIVE,
            LOGON32_PROVIDER_DEFAULT,
            &mut raw,
        )
    }
    .context("candidate fresh credential did not authenticate for the local interactive logon")?;
    let token = OwnedHandle(raw);
    verify_activation_token(token.raw(), binding)?;
    for kind in [
        LOGON32_LOGON_NETWORK,
        LOGON32_LOGON_BATCH,
        LOGON32_LOGON_SERVICE,
    ] {
        let mut raw = HANDLE::default();
        let result = unsafe {
            LogonUserW(
                pcwstr(&name),
                windows::core::w!("."),
                pcwstr(&password.0),
                kind,
                LOGON32_PROVIDER_DEFAULT,
                &mut raw,
            )
        };
        // Even an unexpected successful token never leaves this function.
        let _returned = (!raw.is_invalid()).then(|| OwnedHandle(raw));
        require_policy_denial(&result)
            .with_context(|| format!("candidate forbidden logon type {}", kind.0))?;
    }
    Ok(())
}

fn require_policy_denial(result: &windows::core::Result<()>) -> Result<()> {
    ensure!(
        result
            .as_ref()
            .err()
            .is_some_and(|error| error.code() == windows::core::HRESULT::from_win32(1385)),
        "forbidden logon did not return ERROR_LOGON_TYPE_NOT_GRANTED"
    );
    Ok(())
}

fn verify_activation_token(
    token: windows::Win32::Foundation::HANDLE,
    binding: &AccountBinding,
) -> Result<()> {
    binding.validate()?;
    // Setup's authentication proof must satisfy the same raw-account role as
    // later runner admission. Keep one bounded native parser and reject split
    // admin/UIAccess/wrong-integrity/privilege states here too; never repair.
    crate::account_token::runner_policy::require(token, &binding.account_sid, &binding.group_sid)
        .context("candidate activation token failed dedicated-account policy")
}

fn load(
    installation: &ReadyInstallation,
    lease: &crate::execution_lease::ExecutionLease,
    mode: AccountMode,
) -> Result<Credential> {
    // Before opening a credential key or reading/decrypting a value, recheck
    // both readiness and caller identity. Caller holds the shared lease so
    // elevated maintenance cannot rotate the account underneath this read.
    let current = ReadyInstallation::read(lease)?;
    ensure!(
        current.record == installation.record,
        "candidate installation changed since validation; refusing stale credential snapshot"
    );
    let binding = installation.binding(mode)?;
    let generation = installation
        .record
        .account(mode)?
        .credential_generation
        .as_deref()
        .context("candidate credential missing")?;
    let path = generation_path(&binding.installation_id, mode, generation)?;
    let key =
        crate::reg::Key::open(&path, false)?.context("candidate credential key is missing")?;
    key.validate_credential_security(&binding.owner_sid, &binding.account_sid, &binding.group_sid)?;
    // Same handle for ACL validation and ciphertext read, with no repair path.
    let blob = key
        .get_binary("Blob")?
        .context("candidate credential blob is missing")?;
    ensure!(
        blob.len() <= 64 * 1024,
        "candidate credential blob exceeds size limit"
    );
    decode_blob(&blob, &binding, generation)
}

/// The live, consumed logon preparation is the sole credential-delivery route.
/// Passwords stay inside this module and the scrubbed authenticated wire frame.
pub(crate) fn deliver_to_bootstrap(
    delivery:&mut crate::account_state::execution::bootstrap_plan::lifecycle::resume::logon::delivery::CredentialDelivery<'_, '_, '_, '_>,
) -> Result<()> {
    crate::account_impersonation::require_process_identity()?;
    let (ready, lease, mode) = delivery.begin_read()?;
    let credential = load(ready, lease, mode)?;
    delivery.send_password(&credential.user, &credential.password)
}

/// Same protected reader, but the paired lifecycle supplies its own original
/// role-bound, consuming intent. No password is returned to the caller.
pub(crate) fn deliver_to_paired_bootstrap(
    delivery: &mut crate::account_state::execution::paired_plan::native::account::Delivery<
        '_,
        '_,
        '_,
        '_,
    >,
) -> Result<()> {
    crate::account_impersonation::require_process_identity()?;
    let (ready, lease, mode) = delivery.begin_read()?;
    let credential = load(ready, lease, mode)?;
    delivery.send_password(&credential.user, &credential.password)
}

impl ReadyInstallation {
    /// Reserved internal building block, deliberately unavailable. A serialized
    /// identity is not evidence of durable profile/namespace/job ownership, and
    /// the current ACL-only execution guard cannot authorize this route.
    /// Refuse before credential access until a complete opaque lifecycle guard
    /// replaces this gate. No generic credential accessor exists.
    #[allow(dead_code)]
    pub(crate) fn launch(
        &self,
        lease: &crate::execution_lease::ExecutionLease,
        journal: &crate::account_state::CandidateJournalIdentity,
        spec: &crate::account_launch::AccountContainerSpec,
        quiet: bool,
    ) -> Result<u32> {
        require_full_execution_lifecycle()?;
        let mode = AccountMode::from(spec.network);
        let expected = self.binding(mode)?;
        journal.validate(&expected)?;
        spec.validate()?;
        ensure!(
            spec.container == journal.container
                && spec.account_sid == expected.account_sid
                && spec.owner_sid == expected.owner_sid,
            "candidate launch identity differs from journal/setup"
        );
        let credential = load(self, lease, mode)?;
        crate::logon::spawn_account_appcontainer(
            &credential.user,
            &credential.password,
            spec,
            quiet,
        )
    }

    /// Reserved internal recovery route. An identity and empty child job do
    /// not prove profile ownership or runner/bootstrap quiescence. Refuse before
    /// credential access until full opaque resource-recovery authority replaces
    /// this gate and the cleanup runner itself is durably inventoried.
    #[allow(dead_code)]
    pub(crate) fn cleanup_account_profile(
        &self,
        lease: &crate::execution_lease::ExecutionLease,
        journal: &crate::account_state::CandidateJournalIdentity,
        quiet: bool,
    ) -> Result<u32> {
        require_full_profile_recovery()?;
        let expected = self.binding(journal.binding.mode)?;
        journal.validate(&expected)?;
        crate::job::wait_for_execution_exit(&journal.container)?;
        let credential = load(self, lease, expected.mode)?;
        crate::logon::spawn_account_profile_cleanup(
            &credential.user,
            &credential.password,
            &crate::account_launch::AccountProfileCleanup {
                account_sid: expected.account_sid,
                container: journal.container.clone(),
            },
            quiet,
        )
    }
}

fn require_full_execution_lifecycle() -> Result<()> {
    anyhow::bail!(
        "candidate launch is unavailable until durable profile, namespace and job lifecycle ownership is implemented"
    )
}

fn require_full_profile_recovery() -> Result<()> {
    anyhow::bail!(
        "candidate profile cleanup is unavailable until durable profile ownership and complete process quiescence are proven"
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn candidate_activation_cleanup_cannot_use_an_identity_as_resource_authority() {
        assert!(
            require_full_profile_recovery()
                .unwrap_err()
                .to_string()
                .contains("candidate profile cleanup is unavailable")
        );
    }

    #[test]
    fn candidate_activation_launch_cannot_use_an_identity_as_lifecycle_authority() {
        // This unconditional gate is the first operation in the sole ready-
        // installation launch route, before credential loading or native calls.
        assert!(
            require_full_execution_lifecycle()
                .unwrap_err()
                .to_string()
                .contains("candidate launch is unavailable")
        );
    }

    #[test]
    fn candidate_activation_credential_denial_requires_policy_error_not_auth_failure() {
        let failure = |code| {
            Err(windows::core::Error::from_hresult(
                windows::core::HRESULT::from_win32(code),
            ))
        };
        require_policy_denial(&failure(1385)).unwrap();
        for code in [5, 87, 1326, 1331, 1909] {
            assert!(require_policy_denial(&failure(code)).is_err());
        }
        assert!(require_policy_denial(&Ok(())).is_err());
    }

    #[test]
    fn candidate_activation_credential_proof_rejects_the_host_token() {
        let binding = crate::account_install::synthetic_ready()
            .binding(AccountMode::Online)
            .unwrap();
        let token = crate::util::OwnedHandle(crate::token::open_self_token().unwrap());
        assert!(
            verify_activation_token(token.raw(), &binding)
                .unwrap_err()
                .chain()
                .any(|e| e.to_string().contains("dedicated runner account differs"))
        );
    }

    #[test]
    fn candidate_activation_refuses_existing_identity_before_credential_access() {
        use windows::Win32::Security::{ImpersonateLoggedOnUser, RevertToSelf};
        struct Revert;
        impl Drop for Revert {
            fn drop(&mut self) {
                unsafe {
                    RevertToSelf().unwrap();
                }
            }
        }
        let binding = crate::account_install::synthetic_ready()
            .binding(AccountMode::Offline)
            .unwrap();
        let token = crate::util::OwnedHandle(crate::token::open_self_token().unwrap());
        unsafe {
            ImpersonateLoggedOnUser(token.raw()).unwrap();
        }
        let guard = Revert;
        // These synthetics have no installed credential key. The refusal must
        // occur on caller identity before any registry read or native logon.
        let logon =
            verify_activation_logons(&binding, "CredOwner-1123456789abcdef0123456789abcdef");
        let observation = verify_activation_token(token.raw(), &binding);
        let identity_unchanged = crate::account_impersonation::require_process_identity();
        drop(guard);
        for result in [logon, observation, identity_unchanged] {
            assert!(result.unwrap_err().chain().any(|e| {
                e.to_string()
                    .contains("must not replace an existing thread identity")
            }));
        }
        crate::account_impersonation::require_process_identity().unwrap();
    }

    #[test]
    fn candidate_credential_paths_cannot_escape_or_cross_account_slots() {
        let binding = crate::account_install::synthetic_ready()
            .binding(AccountMode::Offline)
            .unwrap();
        let reference = "CredOwner-1123456789abcdef0123456789abcdef";
        let offline =
            generation_path(&binding.installation_id, AccountMode::Offline, reference).unwrap();
        let online =
            generation_path(&binding.installation_id, AccountMode::Online, reference).unwrap();
        assert_ne!(offline, online);
        for invalid in [
            "Cred",
            "CredOwner-../Cred",
            "CredOwner-0000",
            "CredOwner-1123456789ABCDEF0123456789abcdef",
        ] {
            assert!(generation_path(&binding.installation_id, binding.mode, invalid).is_err());
        }
        assert!(CredentialGenerationPlan::require_fresh(false).is_err());
    }

    #[test]
    fn candidate_credential_ciphertext_is_bound_to_all_identities_and_generation() {
        let record = crate::account_install::synthetic_ready();
        let binding = record.binding(AccountMode::Offline).unwrap();
        // Synthetic password only; no installed setup/registry/credential read.
        let plan =
            CredentialGenerationPlan::prepare(&binding, "Synthetic-test-password-123!").unwrap();
        let decoded = decode_blob(&plan.encrypted_blob, &binding, &plan.reference).unwrap();
        assert_eq!(decoded.user, binding.account_name);
        assert_eq!(decoded.password, "Synthetic-test-password-123!");
        assert!(
            decode_blob(
                &plan.encrypted_blob,
                &record.binding(AccountMode::Online).unwrap(),
                &plan.reference
            )
            .is_err()
        );
        let mut foreign = binding.clone();
        foreign.installation_id = "1123456789abcdef0123456789abcdef".into();
        assert!(decode_blob(&plan.encrypted_blob, &foreign, &plan.reference).is_err());
        let mut foreign = binding.clone();
        foreign.owner_sid = "S-1-5-21-1-2-3-9999".into();
        assert!(decode_blob(&plan.encrypted_blob, &foreign, &plan.reference).is_err());
        let mut foreign = binding.clone();
        foreign.group_sid = "S-1-5-21-1-2-3-9999".into();
        assert!(decode_blob(&plan.encrypted_blob, &foreign, &plan.reference).is_err());
        assert!(
            decode_blob(
                &plan.encrypted_blob,
                &binding,
                "CredOwner-2123456789abcdef0123456789abcdef"
            )
            .is_err()
        );
        let other =
            CredentialGenerationPlan::prepare(&binding, "Synthetic-test-password-123!").unwrap();
        assert_ne!(plan.reference, other.reference);
    }
}
