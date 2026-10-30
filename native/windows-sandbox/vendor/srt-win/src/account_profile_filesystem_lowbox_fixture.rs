//! Fixed test-only lowbox write and outside-canary denial. Data receipts confer
//! no cleanup authority. The caller retains the original profile root and must
//! prove original child exit before consuming the retained observations.
use super::*;
use crate::appcontainer::NetworkAccess;
use serde::{Deserialize, Serialize};
use windows::Win32::System::Threading::GetCurrentProcess;

const PATH_LIMIT: usize = 1024;
const CANARY_LIMIT: usize = 4096;
const PAYLOAD_LIMIT: usize = 128;
const CANARY: &str = "outside-canary.bin";

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct FixedLowboxFilesystemSubject {
    ac_path: String,
    ac_nt_name: String,
    package_id: [u8; 24],
    ac_id: [u8; 24],
    use_nonce: String,
    outside_path: String,
    outside_id: [u8; 24],
}
impl FixedLowboxFilesystemSubject {
    pub(crate) fn require(&self) -> Result<()> {
        crate::account_install::validate_random_id(&self.use_nonce)?;
        bounded_path(&self.ac_path)?;
        require_nt_ac_name(&self.ac_nt_name)?;
        bounded_path(&self.outside_path)?;
        ensure!(
            self.ac_path.ends_with("\\AC")
                && self.outside_path.ends_with(&format!("\\{CANARY}"))
                && !self
                    .outside_path
                    .to_ascii_lowercase()
                    .starts_with(&format!("{}\\", self.ac_path.to_ascii_lowercase())),
            "fixed lowbox filesystem names differ"
        );
        ensure!(
            self.package_id != [0; 24]
                && self.ac_id != [0; 24]
                && self.outside_id != [0; 24]
                && self.package_id != self.ac_id
                && self.package_id != self.outside_id
                && self.ac_id != self.outside_id,
            "fixed lowbox filesystem identities differ"
        );
        Ok(())
    }
    pub(crate) fn require_binding(&self, package_id: [u8; 24], use_nonce: &str) -> Result<()> {
        self.require()?;
        ensure!(
            self.package_id == package_id && self.use_nonce == use_nonce,
            "fixed lowbox filesystem original binding differs"
        );
        Ok(())
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum Stage {
    Token,
    Ac,
    Create,
    Write,
    Outside,
    Recheck,
    Close,
    Complete,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct FixedLowboxFilesystemProof {
    pub(crate) passed: bool,
    pub(crate) stage: Stage,
    pub(crate) hresult: Option<i32>,
    pub(crate) failure_reason: Option<String>,
    subject: FixedLowboxFilesystemSubject,
    token_verified: bool,
    dos_name_observed: bool,
    dos_name_hresult: Option<i32>,
    ac_verified: bool,
    file_id: Option<[u8; 24]>,
    bytes: u32,
    write_verified: bool,
    outside_write_hresult: Option<i32>,
    outside_unexpected_handle_closed: Option<bool>,
    file_closed: bool,
    ac_closed: bool,
    token_rechecked: bool,
}
impl FixedLowboxFilesystemProof {
    fn new(subject: &FixedLowboxFilesystemSubject) -> Self {
        Self {
            passed: false,
            stage: Stage::Token,
            hresult: None,
            failure_reason: None,
            subject: subject.clone(),
            token_verified: false,
            dos_name_observed: false,
            dos_name_hresult: None,
            ac_verified: false,
            file_id: None,
            bytes: 0,
            write_verified: false,
            outside_write_hresult: None,
            outside_unexpected_handle_closed: None,
            file_closed: false,
            ac_closed: false,
            token_rechecked: false,
        }
    }
    pub(crate) fn require_subject(&self, subject: &FixedLowboxFilesystemSubject) -> Result<()> {
        subject.require()?;
        ensure!(
            self.subject == *subject,
            "fixed filesystem proof subject differs"
        );
        let payload_size = fixed_writer::payload(&subject.use_nonce, 0).len();
        ensure!(
            payload_size <= PAYLOAD_LIMIT
                && self.bytes as usize <= payload_size
                && self.hresult.is_none_or(|v| v < 0)
                && self
                    .failure_reason
                    .as_ref()
                    .is_none_or(|v| !v.is_empty() && v.len() <= 160 && !v.contains('\0'))
                && self.outside_write_hresult.is_none_or(|v| v < 0)
                && self
                    .dos_name_hresult
                    .is_none_or(|v| v < 0 && self.dos_name_observed)
                && !(self.outside_write_hresult.is_some()
                    && self.outside_unexpected_handle_closed.is_some()),
            "fixed filesystem proof status or bounds differ"
        );
        if let Some(id) = self.file_id {
            ensure!(
                id != [0; 24]
                    && id != subject.package_id
                    && id != subject.ac_id
                    && id != subject.outside_id
                    && self.ac_verified,
                "fixed filesystem proof file identity differs"
            );
        }
        ensure!(
            (!self.ac_verified || self.token_verified)
                && (!self.write_verified
                    || (self.file_id.is_some() && self.bytes as usize == payload_size))
                && (!self.token_rechecked || self.write_verified),
            "fixed filesystem proof completion order differs"
        );
        let complete = self.token_verified
            && self.dos_name_observed
            && self.ac_verified
            && self.write_verified
            && self.token_rechecked
            && self.file_closed
            && self.ac_closed
            && self.outside_write_hresult == Some(ERROR_ACCESS_DENIED.to_hresult().0)
            && self.outside_unexpected_handle_closed.is_none();
        ensure!(
            if self.passed {
                self.stage == Stage::Complete
                    && self.hresult.is_none()
                    && self.failure_reason.is_none()
                    && complete
            } else {
                self.stage != Stage::Complete && self.hresult.is_some()
            },
            "fixed filesystem proof claims incomplete success"
        );
        Ok(())
    }
}

/// Original handles are neither serializable nor inheritable. There is no
/// path-based recovery, permission repair or deletion operation in this adapter.
pub(crate) struct FixedLowboxFilesystem {
    subject: FixedLowboxFilesystemSubject,
    ac: OriginalFile,
    ac_initial: Metadata,
    outside: OriginalFile,
    outside_initial: Snapshot,
}
impl FixedLowboxFilesystem {
    pub(crate) fn subject(&self) -> FixedLowboxFilesystemSubject {
        self.subject.clone()
    }
    /// Caller must already have verified the exact original child has exited.
    /// Both pins close even on a failed proof or changed-object observation.
    pub(crate) fn verify_and_close(self, proof: &FixedLowboxFilesystemProof) -> Result<()> {
        let verified = (|| -> Result<()> {
            crate::account_impersonation::require_process_identity()?;
            proof.require_subject(&self.subject)?;
            ensure!(proof.passed, "fixed lowbox filesystem child failed");
            ensure!(
                self.ac.metadata()? == self.ac_initial,
                "original AC changed"
            );
            let file = self
                .ac
                .child(&fixed_writer::filename(&self.subject.use_nonce, 0))?;
            let readback = (|| -> Result<()> {
                let snapshot = file.snapshot()?;
                ensure!(
                    Some(snapshot.metadata.id) == proof.file_id
                        && !snapshot.metadata.directory
                        && snapshot.children.is_empty()
                        && snapshot.data == fixed_writer::payload(&self.subject.use_nonce, 0),
                    "fixed lowbox file original identity or readback differs"
                );
                Ok(())
            })();
            let closed = file.close();
            readback?;
            closed?;
            ensure!(
                self.ac.metadata()? == self.ac_initial,
                "original AC changed during readback"
            );
            ensure!(
                self.outside.snapshot()? == self.outside_initial,
                "original outside canary changed"
            );
            Ok(())
        })();
        let ac_closed = self.ac.close();
        let outside_closed = self.outside.close();
        verified?;
        ac_closed?;
        outside_closed?;
        Ok(())
    }
}

pub(super) fn prepare(
    package: &OriginalFile,
    initial: &Metadata,
    sdk_path: &str,
    use_nonce: &str,
) -> Result<FixedLowboxFilesystem> {
    crate::account_install::validate_random_id(use_nonce)?;
    // The caller has already verified the raw actor and exact SDK path. Prove
    // that path still denotes the retained original package before pinning AC.
    let sdk_package = open_read(sdk_path, true, true)?;
    let sdk_matches = sdk_package.metadata().map(|observed| observed == *initial);
    let sdk_closed = sdk_package.close();
    ensure!(
        sdk_matches?,
        "SDK package differs from original retained package"
    );
    sdk_closed?;
    ensure!(
        initial.directory && package.metadata()? == *initial,
        "original package changed"
    );
    let ac = package.child(&[65, 67])?;
    let ac_initial = ac.metadata()?;
    ensure!(ac_initial.directory, "original AC is not a directory");
    if let Some(existing) = ac.child_or_absent(&fixed_writer::filename(use_nonce, 0))? {
        existing.close()?;
        anyhow::bail!("fixed lowbox file already exists");
    }
    let outside_path = fixed_outside_path()?;
    let outside = open_read(&outside_path, false, false)?;
    ensure!(
        (1..=CANARY_LIMIT as i64).contains(&outside.standard()?.EndOfFile),
        "outside canary data bound differs"
    );
    let outside_initial = outside.snapshot()?;
    ensure!(
        !outside_initial.metadata.directory
            && !outside_initial.data.is_empty()
            && outside_initial.data.len() <= CANARY_LIMIT,
        "outside canary size or type differs"
    );
    let subject = FixedLowboxFilesystemSubject {
        ac_path: local_name(&ac_initial)?,
        ac_nt_name: String::from_utf16(&original_final_name(ac.raw()?, FinalNameForm::Nt)?)?,
        package_id: initial.id,
        ac_id: ac_initial.id,
        use_nonce: use_nonce.into(),
        outside_path,
        outside_id: outside_initial.metadata.id,
    };
    subject.require_binding(initial.id, use_nonce)?;
    ensure!(
        local_name(&outside_initial.metadata)? == subject.outside_path,
        "outside canary used an alias"
    );
    Ok(FixedLowboxFilesystem {
        subject,
        ac,
        ac_initial,
        outside,
        outside_initial,
    })
}

fn bounded_path(path: &str) -> Result<()> {
    ensure!(
        path.encode_utf16().count() <= PATH_LIMIT,
        "fixed filesystem path exceeds bound"
    );
    local_components(path)?;
    Ok(())
}
fn require_nt_ac_name(path: &str) -> Result<()> {
    ensure!(
        path.encode_utf16().count() <= PATH_LIMIT
            && path.starts_with("\\Device\\")
            && path.ends_with("\\AC"),
        "fixed AC native name differs"
    );
    for part in path[1..].split('\\') {
        component(&part.encode_utf16().collect::<Vec<_>>())?;
    }
    Ok(())
}
fn open_lowbox_ac(
    subject: &FixedLowboxFilesystemSubject,
    proof: &mut FixedLowboxFilesystemProof,
) -> Result<OriginalFile> {
    let wide = crate::util::wstr(&subject.ac_path);
    let raw = unsafe {
        CreateFileW(
            PCWSTR(wide.as_ptr()),
            FILE_GENERIC_READ.0,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            None,
            OPEN_EXISTING,
            FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_BACKUP_SEMANTICS,
            None,
        )?
    };
    // This DOS-name read is diagnostic only. AppContainer may deny the mount
    // manager lookup even while ordinary file access is permitted. Always use
    // normalized NT names for the contained file's authoritative comparisons.
    let file = OriginalFile(Some(raw), FinalNameForm::Nt);
    crate::account_namespace::verify_noninheritable(raw)?;
    proof.dos_name_observed = true;
    proof.dos_name_hresult = original_final_name(raw, FinalNameForm::Dos)
        .err()
        .map(|e| error_hresult(&e));
    let metadata = file.metadata()?;
    ensure!(
        metadata.directory
            && metadata.id == subject.ac_id
            && String::from_utf16(&metadata.name)? == subject.ac_nt_name,
        "original AC native name identity or type differs"
    );
    Ok(file)
}
fn local_name(metadata: &Metadata) -> Result<String> {
    let final_name = String::from_utf16(&metadata.name)?;
    let result = crate::path_id::strip_extended_prefix(&final_name).to_owned();
    bounded_path(&result)?;
    Ok(result)
}
fn fixed_outside_path() -> Result<String> {
    let image = std::env::current_exe()?;
    let path = image
        .parent()
        .context("fixed lowbox image parent missing")?
        .join(CANARY);
    let text = path.to_str().context("fixed canary path is not Unicode")?;
    let result = crate::path_id::strip_extended_prefix(text).to_owned();
    bounded_path(&result)?;
    Ok(result)
}
fn open_read(path: &str, directory: bool, share_delete: bool) -> Result<OriginalFile> {
    bounded_path(path)?;
    let wide = crate::util::wstr(path);
    let mut share = FILE_SHARE_READ | FILE_SHARE_WRITE;
    if share_delete {
        share |= FILE_SHARE_DELETE;
    }
    let raw = unsafe {
        CreateFileW(
            PCWSTR(wide.as_ptr()),
            FILE_GENERIC_READ.0,
            share,
            None,
            OPEN_EXISTING,
            FILE_FLAG_OPEN_REPARSE_POINT
                | if directory {
                    FILE_FLAG_BACKUP_SEMANTICS
                } else {
                    FILE_FLAGS_AND_ATTRIBUTES(0)
                },
            None,
        )?
    };
    let file = OriginalFile::adopt(raw)?;
    let metadata = file.metadata()?;
    ensure!(
        metadata.directory == directory && local_name(&metadata)? == path,
        "fixed filesystem open used an alias or wrong type"
    );
    Ok(file)
}
fn require_actor(container: &str, account_sid: &str, network: NetworkAccess) -> Result<()> {
    crate::account_impersonation::require_process_identity()?;
    crate::account_token::verify_account_process(
        unsafe { GetCurrentProcess() },
        container,
        account_sid,
        network,
    )
}
fn write_file(file: &OriginalFile, nonce: &str) -> Result<([u8; 24], u32)> {
    let payload = fixed_writer::payload(nonce, 0);
    ensure!(
        payload.len() <= PAYLOAD_LIMIT,
        "fixed lowbox payload exceeds bound"
    );
    let before = file.snapshot()?;
    ensure!(
        !before.metadata.directory && before.data.is_empty(),
        "fixed lowbox file is not empty"
    );
    let mut written = 0;
    unsafe {
        WriteFile(file.raw()?, Some(&payload), Some(&mut written), None)?;
        ensure!(
            written as usize == payload.len(),
            "fixed lowbox write was partial"
        );
        FlushFileBuffers(file.raw()?)?;
    }
    let after = file.snapshot()?;
    ensure!(
        after.metadata == before.metadata && after.data == payload && after.children.is_empty(),
        "fixed lowbox write readback differs"
    );
    Ok((after.metadata.id, written))
}
fn error_hresult(error: &anyhow::Error) -> i32 {
    error
        .chain()
        .find_map(|cause| {
            cause
                .downcast_ref::<windows::core::Error>()
                .map(|e| e.code().0)
        })
        .filter(|value| *value < 0)
        .unwrap_or(E_FAIL.0)
}
// Only fixed diagnostic labels leave this helper. Paths and arbitrary error
// payloads are never copied into the receipt.
fn failure_reason(error: &anyhow::Error) -> String {
    const LABELS: &[&str] = &[
        "query original profile final name",
        "profile final name query overflowed",
        "query original profile file security",
        "truncated profile file security",
        "profile reparse point refused",
        "original profile file is being deleted",
        "profile hardlinked file refused",
        "empty profile file identity",
        "fixed filesystem open used an alias or wrong type",
        "original AC identity differs",
        "original AC native name identity or type differs",
        "fixed lowbox file is not empty",
        "fixed lowbox write was partial",
        "fixed lowbox write readback differs",
        "outside canary write access unexpectedly allowed",
        "outside canary denial was not ACCESS_DENIED",
        "original AC changed during child write",
    ];
    error
        .chain()
        .find_map(|cause| {
            let text = cause.to_string();
            LABELS
                .iter()
                .find(|label| **label == text)
                .map(|label| (*label).to_owned())
        })
        .unwrap_or_else(|| "unclassified native or validation failure".into())
}

/// Invoked only by the authenticated, fixed original child. This checks its
/// actual primary token; the raw-profile writer's SDK admission is not reused.
pub(crate) fn observe_fixed_lowbox_filesystem(
    subject: &FixedLowboxFilesystemSubject,
    container: &str,
    account_sid: &str,
    network: NetworkAccess,
) -> Result<FixedLowboxFilesystemProof> {
    subject.require()?;
    crate::job::execution_name(container)?;
    ensure!(
        subject
            .ac_path
            .to_ascii_lowercase()
            .ends_with(&format!("\\packages\\{container}\\ac").to_ascii_lowercase())
            && subject.outside_path == fixed_outside_path()?,
        "fixed lowbox filesystem actor paths differ"
    );
    let mut proof = FixedLowboxFilesystemProof::new(subject);
    let mut ac = None;
    let mut file = None;
    let result = (|| -> Result<()> {
        require_actor(container, account_sid, network)?;
        proof.token_verified = true;
        proof.stage = Stage::Ac;
        // Direct open avoids READ_CONTROL requirements on drive ancestry.
        // Original ancestry remains pinned by the authenticated raw parent.
        ac = Some(open_lowbox_ac(subject, &mut proof)?);
        let original = ac.as_ref().context("fixed AC missing")?;
        let initial = original.metadata()?;
        ensure!(initial.id == subject.ac_id, "original AC identity differs");
        proof.ac_verified = true;
        proof.stage = Stage::Create;
        file = Some(fixed_writer::create_file(
            original,
            &fixed_writer::filename(&subject.use_nonce, 0),
        )?);
        proof.stage = Stage::Write;
        let (id, bytes) = write_file(
            file.as_ref().context("fixed file missing")?,
            &subject.use_nonce,
        )?;
        proof.file_id = Some(id);
        proof.bytes = bytes;
        proof.write_verified = true;
        proof.stage = Stage::Outside;
        let outside = crate::util::wstr(&subject.outside_path);
        match unsafe {
            CreateFileW(
                PCWSTR(outside.as_ptr()),
                FILE_WRITE_DATA.0,
                FILE_SHARE_READ | FILE_SHARE_WRITE,
                None,
                OPEN_EXISTING,
                FILE_FLAG_OPEN_REPARSE_POINT,
                None,
            )
        } {
            Ok(raw) => {
                // Do not inspect or write through an unexpected write handle.
                let closed = OriginalFile(Some(raw), FinalNameForm::Nt).close();
                proof.outside_unexpected_handle_closed = Some(closed.is_ok());
                closed?;
                anyhow::bail!("outside canary write access unexpectedly allowed");
            }
            Err(error) => {
                proof.outside_write_hresult = Some(error.code().0);
                ensure!(
                    error.code() == ERROR_ACCESS_DENIED.to_hresult(),
                    "outside canary denial was not ACCESS_DENIED"
                );
            }
        }
        proof.stage = Stage::Recheck;
        ensure!(
            original.metadata()? == initial,
            "original AC changed during child write"
        );
        require_actor(container, account_sid, network)?;
        proof.token_rechecked = true;
        Ok(())
    })();
    if let Err(error) = &result {
        proof.hresult = Some(error_hresult(error));
        proof.failure_reason = Some(failure_reason(error));
    }
    if result.is_ok() {
        proof.stage = Stage::Close;
    }
    for (owned, flag) in [
        (file.take(), &mut proof.file_closed),
        (ac.take(), &mut proof.ac_closed),
    ] {
        if let Some(owned) = owned {
            match owned.close() {
                Ok(()) => *flag = true,
                Err(error) => {
                    proof.hresult.get_or_insert(error_hresult(&error));
                    proof
                        .failure_reason
                        .get_or_insert_with(|| "original filesystem handle close failed".into());
                }
            }
        }
    }
    if proof.hresult.is_none() {
        proof.passed = true;
        proof.stage = Stage::Complete;
    }
    proof.require_subject(subject)?;
    Ok(proof)
}

#[cfg(test)]
mod tests {
    use super::*;
    const NONCE: &str = "6123456789abcdef0123456789abcdef";
    fn subject() -> FixedLowboxFilesystemSubject {
        FixedLowboxFilesystemSubject {
            ac_path: "C:\\Users\\Synthetic\\Packages\\Alysis.Execution.7123456789abcdef0123456789abcdef\\AC".into(),
            ac_nt_name: "\\Device\\HarddiskVolume3\\Users\\Synthetic\\Packages\\Alysis.Execution.7123456789abcdef0123456789abcdef\\AC".into(),
            package_id: [1; 24], ac_id: [2; 24], use_nonce: NONCE.into(),
            outside_path: "C:\\Program Files\\AlysisSynthetic\\outside-canary.bin".into(), outside_id: [3; 24],
        }
    }
    fn complete(
        subject: &FixedLowboxFilesystemSubject,
        id: [u8; 24],
    ) -> FixedLowboxFilesystemProof {
        let mut proof = FixedLowboxFilesystemProof::new(subject);
        proof.passed = true;
        proof.stage = Stage::Complete;
        proof.token_verified = true;
        proof.dos_name_observed = true;
        proof.ac_verified = true;
        proof.token_rechecked = true;
        proof.file_id = Some(id);
        proof.bytes = fixed_writer::payload(NONCE, 0).len() as u32;
        proof.write_verified = true;
        proof.file_closed = true;
        proof.ac_closed = true;
        proof.outside_write_hresult = Some(ERROR_ACCESS_DENIED.to_hresult().0);
        proof
    }
    #[test]
    fn station_filesystem_subject_refuses_cross_binding_alias_and_unknown_fields() {
        let original = subject();
        original.require_binding([1; 24], NONCE).unwrap();
        assert!(original.require_binding([2; 24], NONCE).is_err());
        assert!(
            original
                .require_binding([1; 24], "7123456789abcdef0123456789abcdef")
                .is_err()
        );
        for change in 0..6 {
            let mut invalid = original.clone();
            match change {
                0 => invalid.ac_path = "C:\\root\\..\\AC".into(),
                1 => invalid.outside_path = format!("{}\\{CANARY}", invalid.ac_path),
                2 => invalid.ac_id = invalid.package_id,
                3 => invalid.outside_id = [0; 24],
                4 => invalid.outside_path = "C:\\outside.bin".into(),
                _ => invalid.ac_nt_name = "\\Device\\HarddiskVolume3\\..\\AC".into(),
            }
            assert!(invalid.require().is_err());
        }
        let mut wire = serde_json::to_value(original).unwrap();
        wire["cleanup_authority"] = true.into();
        assert!(serde_json::from_value::<FixedLowboxFilesystemSubject>(wire).is_err());
    }
    #[test]
    fn station_filesystem_proof_refuses_missing_closure_denial_and_original_binding() {
        let subject = subject();
        let proof = complete(&subject, [4; 24]);
        proof.require_subject(&subject).unwrap();
        for change in 0..8 {
            let mut invalid = proof.clone();
            match change {
                0 => invalid.file_closed = false,
                1 => invalid.ac_closed = false,
                2 => invalid.outside_write_hresult = Some(ERROR_FILE_NOT_FOUND.to_hresult().0),
                3 => invalid.token_rechecked = false,
                4 => invalid.file_id = Some(subject.outside_id),
                5 => invalid.bytes -= 1,
                6 => invalid.subject.use_nonce = "7123456789abcdef0123456789abcdef".into(),
                _ => invalid.hresult = Some(E_FAIL.0),
            }
            assert!(invalid.require_subject(&subject).is_err());
        }
        let mut failed = FixedLowboxFilesystemProof::new(&subject);
        failed.hresult = Some(E_FAIL.0);
        failed.require_subject(&subject).unwrap();
        failed.stage = Stage::Complete;
        assert!(failed.require_subject(&subject).is_err());
    }
    #[test]
    fn station_filesystem_parent_checks_original_readback_and_canary_without_deleting() {
        for corrupt in 0..3 {
            let path = crate::util::test_temp_dir().join(format!(
                "alysis-lowbox-files-{}",
                crate::account_state::execution::random_id().unwrap()
            ));
            std::fs::create_dir(&path).unwrap();
            let path = std::fs::canonicalize(path).unwrap();
            let path = std::path::PathBuf::from(crate::path_id::strip_extended_prefix(
                path.to_str().unwrap(),
            ));
            let ac_path = path.join("AC");
            let outside_path = path.join(CANARY);
            std::fs::create_dir(&ac_path).unwrap();
            std::fs::write(&outside_path, b"synthetic canary").unwrap();
            let ac = open_read(ac_path.to_str().unwrap(), true, true).unwrap();
            let ac_initial = ac.metadata().unwrap();
            let outside = open_read(outside_path.to_str().unwrap(), false, false).unwrap();
            let outside_initial = outside.snapshot().unwrap();
            let root = open_read(path.to_str().unwrap(), true, true).unwrap();
            let package_id = root.metadata().unwrap().id;
            root.close().unwrap();
            let subject = FixedLowboxFilesystemSubject {
                ac_path: ac_path.to_str().unwrap().into(),
                ac_nt_name: String::from_utf16(
                    &original_final_name(ac.raw().unwrap(), FinalNameForm::Nt).unwrap(),
                )
                .unwrap(),
                package_id,
                ac_id: ac_initial.id,
                use_nonce: NONCE.into(),
                outside_path: outside_path.to_str().unwrap().into(),
                outside_id: outside_initial.metadata.id,
            };
            // Same original directory, viewed using the native form used by
            // the lowbox, then independently read back through the DOS parent.
            let mut observation = FixedLowboxFilesystemProof::new(&subject);
            let native_ac = open_lowbox_ac(&subject, &mut observation).unwrap();
            assert!(observation.dos_name_observed);
            let file =
                fixed_writer::create_file(&native_ac, &fixed_writer::filename(NONCE, 0)).unwrap();
            let (id, _) = write_file(&file, NONCE).unwrap();
            file.close().unwrap();
            native_ac.close().unwrap();
            let proof = complete(&subject, id);
            let prepared = FixedLowboxFilesystem {
                subject,
                ac,
                ac_initial,
                outside,
                outside_initial,
            };
            let leaf = ac_path.join(String::from_utf16(&fixed_writer::filename(NONCE, 0)).unwrap());
            if corrupt == 1 {
                std::fs::write(&leaf, b"wrong bounded bytes").unwrap();
            }
            if corrupt == 2 {
                // The original canary permits concurrent writes, so retention
                // alone must never stand in for the post-exit comparison.
                std::fs::write(&outside_path, b"changed synthetic canary").unwrap();
            }
            // Holding this exact original file prevents replacement/deletion;
            // any failure still has to close the pin before fixture cleanup.
            assert!(std::fs::remove_file(&outside_path).is_err());
            assert_eq!(prepared.verify_and_close(&proof).is_ok(), corrupt == 0);
            assert!(leaf.exists());
            assert_eq!(
                std::fs::read(&outside_path).unwrap(),
                if corrupt == 2 {
                    b"changed synthetic canary".as_slice()
                } else {
                    b"synthetic canary".as_slice()
                }
            );
            // Test-only cleanup of this exclusively-created synthetic fixture.
            std::fs::remove_file(leaf).unwrap();
            std::fs::remove_file(outside_path).unwrap();
            std::fs::remove_dir(ac_path).unwrap();
            std::fs::remove_dir(path).unwrap();
        }
    }
    #[test]
    fn station_filesystem_diagnostic_preserves_native_error_without_error_payloads() {
        let native = anyhow::Error::from(windows::core::Error::from_hresult(
            ERROR_ACCESS_DENIED.to_hresult(),
        ))
        .context("query original profile final name");
        assert_eq!(error_hresult(&native), ERROR_ACCESS_DENIED.to_hresult().0);
        assert_eq!(failure_reason(&native), "query original profile final name");
        assert_eq!(
            failure_reason(&anyhow::anyhow!("untrusted-private-error-payload")),
            "unclassified native or validation failure"
        );
    }
}
