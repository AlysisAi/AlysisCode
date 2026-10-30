//! Ignored observation qualification on an explicitly selected disposable VM.
//! No repair capability, ACL mutation, raw metadata output, or installed state.

use super::*;
use serde::Serialize;
use std::io::{Seek, SeekFrom, Write};
use std::os::windows::io::AsRawHandle;
use std::path::{Path, PathBuf};
use windows::Win32::Foundation::HANDLE;
use windows::Win32::Security::{GetTokenInformation, TOKEN_ELEVATION, TOKEN_QUERY, TokenElevation};
use windows::Win32::Storage::FileSystem::{
    CreateDirectoryW, CreateFileW, FILE_FLAG_BACKUP_SEMANTICS, FILE_FLAG_OPEN_REPARSE_POINT,
    FILE_SHARE_DELETE, FILE_SHARE_READ, FILE_SHARE_WRITE, FILE_STANDARD_INFO, FileStandardInfo,
    GetFileInformationByHandleEx, GetVolumeNameForVolumeMountPointW, GetVolumePathNameW,
    OPEN_EXISTING,
};
use windows::Win32::System::Threading::{GetCurrentProcess, OpenProcessToken};

const SENTINEL: &str = "github-hosted-read-only-observations";
const TEST: &str = "ntfs_disposable_host_privileged_observations";
const REUSE_LIMIT: u32 = 32;

#[derive(Clone)]
struct Admission {
    sentinel: Option<String>,
    github: Option<String>,
    environment: Option<String>,
    os: Option<String>,
    repository: Option<String>,
    event: Option<String>,
    run: Option<String>,
    attempt: Option<String>,
    windows_x64: bool,
}
impl Admission {
    fn current() -> Self {
        let env = |key| std::env::var(key).ok();
        Self {
            sentinel: env("ALYSIS_RUN_DISPOSABLE_NTFS_OBSERVATIONS"),
            github: env("GITHUB_ACTIONS"),
            environment: env("RUNNER_ENVIRONMENT"),
            os: env("RUNNER_OS"),
            repository: env("GITHUB_REPOSITORY"),
            event: env("GITHUB_EVENT_NAME"),
            run: env("GITHUB_RUN_ID"),
            attempt: env("GITHUB_RUN_ATTEMPT"),
            windows_x64: cfg!(all(windows, target_arch = "x86_64")),
        }
    }
    fn validate(&self) -> Result<()> {
        // Accidental-execution gates, not cryptographic host attestation.
        ensure!(
            self.windows_x64,
            "NTFS observation test requires Windows x64"
        );
        for (actual, expected) in [
            (&self.sentinel, SENTINEL),
            (&self.github, "true"),
            (&self.environment, "github-hosted"),
            (&self.os, "Windows"),
            (&self.repository, "AlysisAi/alysis-code-internal"),
            (&self.event, "workflow_dispatch"),
        ] {
            ensure!(
                actual.as_deref() == Some(expected),
                "NTFS disposable-host admission refused"
            );
        }
        for value in [&self.run, &self.attempt] {
            ensure!(
                value.as_deref().is_some_and(|v| !v.is_empty()
                    && v.len() <= 32
                    && v.bytes().all(|b| b.is_ascii_digit())),
                "NTFS observation run identity refused"
            );
        }
        Ok(())
    }
}

fn elevated() -> Result<bool> {
    let mut raw = HANDLE::default();
    unsafe {
        OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut raw)?;
    }
    let token = crate::util::OwnedHandle(raw);
    let mut info = TOKEN_ELEVATION::default();
    let mut returned = 0;
    unsafe {
        GetTokenInformation(
            token.raw(),
            TokenElevation,
            Some((&mut info as *mut TOKEN_ELEVATION).cast()),
            std::mem::size_of::<TOKEN_ELEVATION>() as u32,
            &mut returned,
        )?;
    }
    ensure!(
        returned as usize == std::mem::size_of::<TOKEN_ELEVATION>(),
        "invalid elevation observation"
    );
    Ok(info.TokenIsElevated != 0)
}

#[derive(Serialize)]
struct Observation {
    stage: &'static str,
    category: &'static str,
}

#[derive(Debug)]
struct ControlFailure(&'static str);
impl std::fmt::Display for ControlFailure {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.0)
    }
}
impl std::error::Error for ControlFailure {}

#[derive(Serialize)]
struct Failure {
    category: &'static str,
    probe_step: Option<&'static str>,
    parser_reason: Option<&'static str>,
    reference_input: Option<&'static str>,
    hresult: Option<i32>,
    os_code: Option<i32>,
}
fn failure(error: &anyhow::Error) -> Failure {
    let hresult = error
        .chain()
        .find_map(|e| e.downcast_ref::<windows::core::Error>())
        .map(|e| e.code().0);
    let os_code = error
        .chain()
        .find_map(|e| e.downcast_ref::<std::io::Error>())
        .and_then(|e| e.raw_os_error());
    Failure {
        category: error
            .downcast_ref::<ControlFailure>()
            .map(|e| e.0)
            .unwrap_or(if hresult.is_some() || os_code.is_some() {
                "native_api_failure"
            } else {
                "parser_or_invariant_refusal"
            }),
        probe_step: error.downcast_ref::<ProbeStep>().map(|e| e.0),
        parser_reason: error.downcast_ref::<ParseFailure>().map(|e| e.label()),
        reference_input: error.downcast_ref::<ReferenceInput>().map(|e| e.label()),
        hresult,
        os_code,
    }
}

#[derive(Serialize)]
struct Evidence {
    schema: u32,
    test: &'static str,
    run_id: String,
    run_attempt: String,
    passed: bool,
    failed: bool,
    current_stage: &'static str,
    completed_stages: Vec<&'static str>,
    observations: Vec<Observation>,
    failure: Option<Failure>,
    privileged_probe_calls: u32,
    natural_reuse_attempts: u32,
    natural_reuse_observed: bool,
    fixture_cleanup_complete: bool,
    fixtures_preserved_on_failure: bool,
    no_repair_or_acl_mutation: bool,
    no_raw_metadata_retained: bool,
}
struct Report {
    file: std::fs::File,
    value: Evidence,
}
impl Report {
    fn create(admission: &Admission, parent: &Path) -> Result<Self> {
        let file = std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(parent.join("alysis-ntfs-observations.json"))?;
        let mut report = Self {
            file,
            value: Evidence {
                schema: 1,
                test: TEST,
                run_id: admission.run.clone().unwrap(),
                run_attempt: admission.attempt.clone().unwrap(),
                passed: false,
                failed: false,
                current_stage: "admitted",
                completed_stages: Vec::new(),
                observations: Vec::new(),
                failure: None,
                privileged_probe_calls: 0,
                natural_reuse_attempts: 0,
                natural_reuse_observed: false,
                fixture_cleanup_complete: false,
                fixtures_preserved_on_failure: true,
                no_repair_or_acl_mutation: true,
                no_raw_metadata_retained: true,
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
    fn step<T>(&mut self, stage: &'static str, action: impl FnOnce() -> Result<T>) -> Result<T> {
        self.value.current_stage = stage;
        self.save()?;
        match action() {
            Ok(value) => {
                self.value.completed_stages.push(stage);
                self.save()?;
                Ok(value)
            }
            Err(error) => {
                self.value.failed = true;
                self.value.failure = Some(failure(&error));
                self.save()?;
                anyhow::bail!(
                    "NTFS disposable qualification failed at {stage}; inspect sanitized evidence"
                )
            }
        }
    }
    fn observe(
        &mut self,
        stage: &'static str,
        volume: &str,
        id: &FileId,
    ) -> Result<RecordObservation> {
        self.value.privileged_probe_calls += 1;
        let value = self.step(stage, || probe_volume(volume, id))?;
        self.value.observations.push(Observation {
            stage,
            category: match value {
                RecordObservation::SameReference => "same_reference",
                RecordObservation::DifferentSequence { .. } => "different_sequence",
                RecordObservation::LowerInUseRecord { .. } => "lower_in_use_record",
            },
        });
        self.save()?;
        Ok(value)
    }
}

fn control(value: bool, category: &'static str) -> Result<()> {
    if !value {
        return Err(ControlFailure(category).into());
    }
    Ok(())
}

struct Fixture {
    root: PathBuf,
    id: FileId,
    pin: crate::util::OwnedHandle,
}
impl Fixture {
    fn create(parent: &Path, admission: &Admission) -> Result<Self> {
        use windows::Win32::Security::SECURITY_ATTRIBUTES;
        let root = parent.join(format!(
            "alysis-ntfs-observations-{}-{}",
            admission.run.as_ref().unwrap(),
            admission.attempt.as_ref().unwrap()
        ));
        let owner = crate::sid::current_user_sid()?;
        let descriptor = crate::util::OwnedSd::from_sddl(&format!(
            "O:{owner}D:P(A;OICI;FA;;;{owner})(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"
        ))?;
        let attributes = SECURITY_ATTRIBUTES {
            nLength: std::mem::size_of::<SECURITY_ATTRIBUTES>() as u32,
            lpSecurityDescriptor: descriptor.ptr.0,
            bInheritHandle: false.into(),
        };
        let wide = crate::util::wstr(root.to_str().context("non-Unicode fixture root")?);
        unsafe {
            CreateDirectoryW(crate::util::pcwstr(&wide), Some(&attributes))?;
        }
        let pin = crate::util::OwnedHandle(unsafe {
            CreateFileW(
                crate::util::pcwstr(&wide),
                1,
                FILE_SHARE_READ | FILE_SHARE_WRITE,
                None,
                OPEN_EXISTING,
                FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
                None,
            )?
        });
        let id = crate::path_id::file_id_from_handle(pin.raw())?;
        let root = root.canonicalize()?;
        ensure!(
            root.starts_with(parent) && root != parent,
            "fixture escaped selected parent"
        );
        Ok(Self { root, id, pin })
    }
    fn cleanup(self) -> Result<()> {
        ensure!(
            crate::path_id::file_id_from_handle(self.pin.raw())? == self.id
                && crate::path_id::capture_file_id(
                    self.root.to_str().context("fixture root Unicode")?
                )? == self.id,
            "fixture identity changed before cleanup"
        );
        ensure!(
            std::fs::read_dir(&self.root)?.next().is_none(),
            "fixture cleanup is not empty"
        );
        drop(self.pin);
        std::fs::remove_dir(self.root)?;
        Ok(())
    }
}

fn volume_for(path: &Path) -> Result<String> {
    let path = crate::util::wstr(path.to_str().context("non-Unicode fixture path")?);
    let mut mount = [0u16; 1024];
    let mut guid = [0u16; 64];
    unsafe {
        GetVolumePathNameW(crate::util::pcwstr(&path), &mut mount)?;
        GetVolumeNameForVolumeMountPointW(crate::util::pcwstr(&mount), &mut guid)?;
    }
    let end = guid
        .iter()
        .position(|unit| *unit == 0)
        .context("unterminated volume selection")?;
    let result = String::from_utf16(&guid[..end])?;
    validate_volume_root(&result)?;
    Ok(result)
}

fn open_metadata(path: &Path) -> Result<crate::util::OwnedHandle> {
    let wide = crate::util::wstr(path.to_str().context("fixture path Unicode")?);
    Ok(crate::util::OwnedHandle(unsafe {
        CreateFileW(
            crate::util::pcwstr(&wide),
            0,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            None,
            OPEN_EXISTING,
            FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )?
    }))
}

fn run_controls(
    report: &mut Report,
    fixture: &Fixture,
    directory: bool,
    volume: &str,
) -> Result<FileId> {
    let stages = if directory {
        [
            "directory_birth",
            "directory_live",
            "directory_live_check",
            "directory_move",
            "directory_moved",
            "directory_moved_check",
            "directory_unlink",
            "directory_held_pending",
            "directory_held_check",
            "directory_final_close",
            "directory_final_deleted",
            "directory_deleted_check",
        ]
    } else {
        [
            "file_birth",
            "file_live",
            "file_live_check",
            "file_move",
            "file_moved",
            "file_moved_check",
            "file_unlink",
            "file_held_pending",
            "file_held_check",
            "file_final_close",
            "file_final_deleted",
            "file_deleted_check",
        ]
    };
    let path = fixture
        .root
        .join(if directory { "directory" } else { "file" });
    let moved = fixture.root.join(if directory {
        "directory-moved"
    } else {
        "file-moved"
    });
    let (held, expected) = report.step(stages[0], || {
        if directory {
            std::fs::create_dir(&path)?;
        } else {
            let mut file = std::fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(&path)?;
            file.write_all(b"bounded NTFS observation fixture")?;
        }
        let held = open_metadata(&path)?;
        let expected = crate::path_id::file_id_from_handle(held.raw())?;
        Ok((held, expected))
    })?;
    let observed = report.observe(stages[1], volume, &expected)?;
    report.step(stages[2], || {
        control(
            observed == RecordObservation::SameReference,
            "live_identity_mismatch",
        )
    })?;
    report.step(stages[3], || {
        std::fs::rename(&path, &moved)?;
        control(
            crate::path_id::capture_file_id(moved.to_str().context("fixture move Unicode")?)?
                == expected,
            "move_identity_mismatch",
        )
    })?;
    let observed = report.observe(stages[4], volume, &expected)?;
    report.step(stages[5], || {
        control(
            observed == RecordObservation::SameReference,
            "moved_identity_mismatch",
        )
    })?;
    report.step(stages[6], || {
        if directory {
            std::fs::remove_dir(&moved)?;
        } else {
            std::fs::remove_file(&moved)?;
        }
        let mut info = FILE_STANDARD_INFO::default();
        unsafe {
            GetFileInformationByHandleEx(
                held.raw(),
                FileStandardInfo,
                (&mut info as *mut FILE_STANDARD_INFO).cast(),
                std::mem::size_of::<FILE_STANDARD_INFO>() as u32,
            )?;
        }
        control(
            info.DeletePending || info.NumberOfLinks == 0,
            "fixture_not_delete_pending",
        )?;
        control(
            crate::path_id::file_id_from_handle(held.raw())? == expected,
            "held_identity_mismatch",
        )
    })?;
    let observed = report.observe(stages[7], volume, &expected)?;
    report.step(stages[8], || {
        control(
            observed == RecordObservation::SameReference,
            "held_reference_mismatch",
        )
    })?;
    report.step(stages[9], || {
        drop(held);
        Ok(())
    })?;
    let observed = report.observe(stages[10], volume, &expected)?;
    report.step(stages[11], || {
        control(
            observed != RecordObservation::SameReference,
            "deleted_reference_still_in_use",
        )
    })?;
    Ok(expected)
}

fn qualify(report: &mut Report, parent: &Path, admission: &Admission) -> Result<()> {
    report.step("elevated_host", || {
        control(elevated()?, "elevation_required")
    })?;
    let fixture = report.step("private_fixture_birth", || {
        Fixture::create(parent, admission)
    })?;
    let volume = report.step("select_fixture_volume", || volume_for(&fixture.root))?;
    let deleted_file = run_controls(report, &fixture, false, &volume)?;
    run_controls(report, &fixture, true, &volume)?;
    let wanted = report.step("deleted_reference_for_reuse", || {
        Reference::from_id(&deleted_file)
    })?;
    for _ in 0..REUSE_LIMIT {
        report.value.natural_reuse_attempts += 1;
        let path = fixture.root.join("reuse-candidate");
        let (file, current) = report.step("bounded_reuse_birth", || {
            let file = std::fs::OpenOptions::new()
                .read(true)
                .write(true)
                .create_new(true)
                .open(&path)?;
            let current = crate::path_id::file_id_from_handle(HANDLE(file.as_raw_handle()))?;
            Ok((file, current))
        })?;
        let reference = report.step("bounded_reuse_compare", || Reference::from_id(&current))?;
        if current.volume_serial == deleted_file.volume_serial && reference.record == wanted.record
        {
            report.step("bounded_reuse_distinct", || {
                control(
                    reference.sequence != wanted.sequence,
                    "reuse_generation_ambiguous",
                )
            })?;
            let new_observation = report.observe("reused_live_identity", &volume, &current)?;
            report.step("reused_live_identity_check", || {
                control(
                    new_observation == RecordObservation::SameReference,
                    "reused_live_identity_mismatch",
                )
            })?;
            let old_observation = report.observe("reused_old_identity", &volume, &deleted_file)?;
            report.step("reused_old_identity_check", || {
                control(
                    old_observation
                        == RecordObservation::DifferentSequence {
                            observed: reference.sequence,
                        },
                    "reuse_observation_mismatch",
                )
            })?;
            report.value.natural_reuse_observed = true;
        }
        report.step("bounded_reuse_cleanup", || {
            drop(file);
            std::fs::remove_file(path)?;
            Ok(())
        })?;
        if report.value.natural_reuse_observed {
            break;
        }
    }
    report.step("private_fixture_cleanup", || fixture.cleanup())?;
    report.value.fixture_cleanup_complete = true;
    report.value.fixtures_preserved_on_failure = false;
    report.value.passed = true;
    report.value.current_stage = "complete_observations";
    report.save()
}

#[test]
#[ignore = "privileged NTFS observations on fresh manual GitHub-hosted Windows VM only; NEVER run locally"]
fn ntfs_disposable_host_privileged_observations() -> Result<()> {
    let admission = Admission::current();
    admission.validate()?; // Before files, handles, or any privileged probe.
    let parent = PathBuf::from(std::env::var_os("RUNNER_TEMP").context("RUNNER_TEMP required")?)
        .canonicalize()?;
    ensure!(
        parent.is_absolute() && parent.is_dir(),
        "invalid evidence parent"
    );
    let mut report = Report::create(&admission, &parent)?;
    qualify(&mut report, &parent, &admission)
}

#[test]
fn ntfs_disposable_admission_and_sanitization_are_fail_closed() {
    let approved = Admission {
        sentinel: Some(SENTINEL.into()),
        github: Some("true".into()),
        environment: Some("github-hosted".into()),
        os: Some("Windows".into()),
        repository: Some("AlysisAi/alysis-code-internal".into()),
        event: Some("workflow_dispatch".into()),
        run: Some("12345".into()),
        attempt: Some("1".into()),
        windows_x64: true,
    };
    approved.validate().unwrap();
    for index in 0..9 {
        for invalid in [None, Some("unapproved".into()), Some(String::new())] {
            let mut value = approved.clone();
            match index {
                0 => value.sentinel = invalid,
                1 => value.github = invalid,
                2 => value.environment = invalid,
                3 => value.os = invalid,
                4 => value.repository = invalid,
                5 => value.event = invalid,
                6 => value.run = invalid,
                7 => value.attempt = invalid,
                _ => value.windows_x64 = false,
            }
            assert!(value.validate().is_err());
        }
    }
    for key in ["push", "pull_request", "pull_request_target", "schedule"] {
        let mut value = approved.clone();
        value.event = Some(key.into());
        assert!(value.validate().is_err());
    }
    let mut value = approved.clone();
    value.environment = Some("self-hosted".into());
    assert!(value.validate().is_err());
    value = approved;
    value.run = Some("1".repeat(33));
    assert!(value.validate().is_err());
    let error = anyhow::Error::new(windows::core::Error::from_hresult(
        windows::Win32::Foundation::ERROR_ACCESS_DENIED.to_hresult(),
    ))
    .context("secret filename and raw metadata must never be emitted")
    .context(ProbeStep("query_selected_record"));
    assert_eq!(
        serde_json::to_value(failure(&error)).unwrap(),
        serde_json::json!({
        "category":"native_api_failure", "probe_step":"query_selected_record", "parser_reason":null, "reference_input":null, "hresult": -2147024891i32, "os_code":null })
    );
    let error = anyhow::anyhow!("secret MFT buffer").context(ProbeStep("parse_live_anchor"));
    assert_eq!(
        serde_json::to_value(failure(&error)).unwrap(),
        serde_json::json!({
        "category":"parser_or_invariant_refusal", "probe_step":"parse_live_anchor", "parser_reason":null, "reference_input":null, "hresult":null, "os_code":null })
    );
}

#[test]
fn ntfs_disposable_parser_diagnostics_never_serialize_error_text_or_metadata() {
    let error = anyhow::anyhow!("secret arbitrary filename, credential and raw MFT contents")
        .context(ParseFailure::ReferenceSequenceZero)
        .context(ReferenceInput::ReturnedReference)
        .context(ProbeStep("parse_live_anchor"));
    assert_eq!(
        serde_json::to_value(failure(&error)).unwrap(),
        serde_json::json!({
            "category":"parser_or_invariant_refusal",
            "probe_step":"parse_live_anchor",
            "parser_reason":"reference_sequence_zero",
            "reference_input":"returned_reference",
            "hresult":null, "os_code":null
        })
    );
    let untyped = anyhow::anyhow!("reference_sequence_zero secret forged diagnostic")
        .context("returned_reference parse_live_anchor");
    let value = serde_json::to_value(failure(&untyped)).unwrap();
    assert!(value["parser_reason"].is_null());
    assert!(value["reference_input"].is_null());
    assert!(value["probe_step"].is_null());
    assert!(!value.to_string().contains("secret"));
}
