//! Two synthetic profiles only, admitted on an explicit disposable hosted VM.
//! Failed fixtures are preserved, never repaired or removed by guessed paths.
use super::*;
use windows::Wdk::Foundation::OBJECT_ATTRIBUTES;
use windows::Win32::Foundation::CloseHandle;
use windows::Win32::Security::Cryptography::{BCRYPT_USE_SYSTEM_PREFERRED_RNG, BCryptGenRandom};
use windows::Win32::Security::Isolation::DeleteAppContainerProfile;
use windows::Win32::System::Registry::*;
use windows::core::PCWSTR;

const SENTINEL: &str = "github-hosted-retained-profile-registry-only";
const TEST: &str = "candidate_profile_registry_disposable_native_lifecycle";

fn admitted(values: &[Option<String>], run: &str, attempt: &str) -> bool {
    let expected = [
        SENTINEL,
        "true",
        "github-hosted",
        "Windows",
        "AlysisAi/alysis-code-internal",
        "workflow_dispatch",
    ];
    values.len() == expected.len()
        && values
            .iter()
            .zip(expected)
            .all(|(v, x)| v.as_deref() == Some(x))
        && [run, attempt]
            .iter()
            .all(|s| !s.is_empty() && s.len() <= 24 && s.bytes().all(|c| c.is_ascii_digit()))
        && cfg!(all(windows, target_arch = "x86_64"))
}

#[test]
fn account_registry_disposable_admission_requires_every_gate() {
    let values = [
        SENTINEL,
        "true",
        "github-hosted",
        "Windows",
        "AlysisAi/alysis-code-internal",
        "workflow_dispatch",
    ]
    .map(|s| Some(s.to_owned()));
    assert!(admitted(&values, "123", "1"));
    for i in 0..values.len() {
        let mut invalid = values.clone();
        invalid[i] = None;
        assert!(!admitted(&invalid, "123", "1"));
        invalid[i] = Some("wrong".into());
        assert!(!admitted(&invalid, "123", "1"));
    }
    for invalid in ["", "1x", "1234567890123456789012345"] {
        assert!(!admitted(&values, invalid, "1"));
        assert!(!admitted(&values, "123", invalid));
    }
}

fn close_token(token: OwnedHandle) -> Result<()> {
    let raw = token.raw();
    std::mem::forget(token); // Exactly one close; never retry an ambiguous result.
    unsafe { CloseHandle(raw) }.context("close disposable profile token")
}

fn born(actor: &str) -> Result<(String, OwnedHandle)> {
    crate::account_token::verify_raw_profile_actor(actor)?;
    let mut nonce = [0u8; 16];
    unsafe { BCryptGenRandom(None, &mut nonce, BCRYPT_USE_SYSTEM_PREFERRED_RNG) }.ok()?;
    let name = format!(
        "Alysis.Execution.{}",
        nonce.iter().map(|b| format!("{b:02x}")).collect::<String>()
    );
    // SDK birth must succeed exclusively before this fixture can delete a name.
    // No Drop-based deletion after a failed step, and at most two calls per run.
    crate::appcontainer::create(&name, &[])?;
    let package = crate::appcontainer::derive(&name)?;
    let source = OwnedHandle(crate::token::open_self_token()?);
    type Create = unsafe extern "system" fn(
        *mut HANDLE,
        HANDLE,
        u32,
        *const OBJECT_ATTRIBUTES,
        PSID,
        u32,
        *const SID_AND_ATTRIBUTES,
        u32,
        *const HANDLE,
    ) -> NTSTATUS;
    let module = unsafe { GetModuleHandleW(windows::core::w!("ntdll.dll"))? };
    let function = unsafe { GetProcAddress(module, windows::core::s!("NtCreateLowBoxToken")) }
        .context("NtCreateLowBoxToken unavailable")?;
    let create: Create = unsafe { std::mem::transmute(function) };
    let attrs = OBJECT_ATTRIBUTES {
        Length: size_of::<OBJECT_ATTRIBUTES>() as u32,
        ..Default::default()
    };
    let mut raw = HANDLE::default();
    let status = unsafe {
        create(
            &mut raw,
            source.raw(),
            (TOKEN_QUERY | TOKEN_DUPLICATE | TOKEN_IMPERSONATE).0,
            &attrs,
            package.raw(),
            0,
            std::ptr::null(),
            0,
            std::ptr::null(),
        )
    };
    status
        .ok()
        .context("create same-user zero-capability profile token")?;
    let token = OwnedHandle(raw);
    close_token(source)?;
    verify_profile_token(
        token.raw(),
        &crate::sid::sid_bytes(&package.string()?)?,
        &crate::sid::sid_bytes(actor)?,
    )?;
    Ok((name, token))
}

// Test-only writes to descendants of an exclusively born disposable profile.
// No production guard exposes a mutation method or a raw handle.
fn fixture_child(parent: &RetainedKey, name: &str) -> Result<RetainedKey> {
    let name = crate::util::wstr(name);
    let mut raw = HKEY::default();
    let mut disposition = REG_CREATE_KEY_DISPOSITION::default();
    unsafe {
        RegCreateKeyExW(
            parent.key.context("fixture parent consumed")?,
            PCWSTR(name.as_ptr()),
            None,
            PCWSTR::null(),
            REG_OPTION_NON_VOLATILE,
            KEY_ALL_ACCESS | KEY_WOW64_64KEY,
            None,
            &mut raw,
            Some(&mut disposition),
        )
    }
    .ok()?;
    let key = RetainedKey { key: Some(raw) };
    ensure!(
        disposition == REG_CREATED_NEW_KEY,
        "fixture child collision"
    );
    crate::account_namespace::verify_noninheritable(HANDLE(raw.0))?;
    Ok(key)
}
fn fixture_value(key: &RetainedKey, kind: REG_VALUE_TYPE, bytes: &[u8]) -> Result<()> {
    unsafe {
        RegSetValueExW(
            key.key.context("fixture key consumed")?,
            windows::core::w!("BoundedSyntheticValue"),
            None,
            kind,
            Some(bytes),
        )
    }
    .ok()?;
    Ok(())
}

fn present(key: &RetainedProfileRegistry) -> Result<RegistryObservation> {
    match key.observe()? {
        KeyObservation::Present(value) => Ok(value),
        KeyObservation::OriginalKeyDeleted => anyhow::bail!("unexpected deleted registry key"),
    }
}
fn delete(name: &str) -> Result<()> {
    crate::account_impersonation::require_process_identity()?;
    let name = crate::util::wstr(name);
    unsafe { DeleteAppContainerProfile(PCWSTR(name.as_ptr())) }
        .context("delete exclusively born fixture profile")
}

#[test]
#[ignore = "explicit disposable hosted retained registry qualification"]
fn candidate_profile_registry_disposable_native_lifecycle() {
    let values = [
        "ALYSIS_RUN_DISPOSABLE_PROFILE_REGISTRY_NATIVE",
        "GITHUB_ACTIONS",
        "RUNNER_ENVIRONMENT",
        "RUNNER_OS",
        "GITHUB_REPOSITORY",
        "GITHUB_EVENT_NAME",
    ]
    .map(|name| std::env::var(name).ok());
    let run = std::env::var("GITHUB_RUN_ID").unwrap_or_default();
    let attempt = std::env::var("GITHUB_RUN_ATTEMPT").unwrap_or_default();
    assert!(
        admitted(&values, &run, &attempt),
        "disposable registry admission refused; no local override"
    );
    let directory =
        std::path::PathBuf::from(std::env::var_os("RUNNER_TEMP").expect("CI temp required"));
    assert!(directory.is_absolute() && directory.is_dir());
    let output = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(directory.join("alysis-profile-registry-native.json"))
        .expect("preserve prior report");
    let mut cases = Vec::new();
    let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| -> Result<()> {
        let actor = crate::sid::current_user_sid()?;
        let (first, token) = born(&actor)?;
        let (second, control_token) = born(&actor)?;
        ensure!(first != second, "synthetic profile collision");
        cases.push("two_exclusive_profiles_and_zero_capability_tokens");

        ensure!(
            RetainedProfileRegistry::open(&token, &second, &actor).is_err(),
            "wrong package admitted"
        );
        crate::account_impersonation::with_token(&token, || {
            ensure!(
                RetainedProfileRegistry::open(&token, &first, &actor).is_err(),
                "nested profile context admitted"
            );
            Ok(())
        })?;
        crate::account_impersonation::require_process_identity()?;
        cases.push("actual_lowbox_duplicate_and_context_refusals");

        let empty = RetainedProfileRegistry::open(&token, &first, &actor)?;
        let fixture_root = RetainedKey::open(&token, &first, &actor)?;
        let fixture_parent = fixture_child(&fixture_root, "Qualification")?;
        let fixture_leaf = fixture_child(&fixture_parent, "Nested")?;
        fixture_value(&fixture_leaf, REG_BINARY, b"synthetic-v1")?;
        ensure!(empty.observe().is_err(), "added child topology was ignored");
        empty.close()?;
        cases.push("new_child_topology_refused");

        let key = RetainedProfileRegistry::open(&token, &first, &actor)?;
        ensure!(
            present(&key)?.has_sdk_descendants(),
            "populated descendants not retained"
        );
        let repeat = RetainedProfileRegistry::open(&token, &first, &actor)?;
        let control = RetainedProfileRegistry::open(&control_token, &second, &actor)?;
        let original = present(&key)?;
        let unchanged = present(&control)?;
        ensure!(
            original == present(&repeat)?,
            "repeat SDK observation changed"
        );
        ensure!(
            original.distinct_roots(&unchanged),
            "separate SDK profile key is not distinct"
        );
        cases.push("retained_sdk_keys_are_profile_specific");

        fixture_value(&fixture_leaf, REG_BINARY, b"synthetic-v2")?;
        ensure!(key.observe().is_err(), "descendant value drift was ignored");
        fixture_value(&fixture_leaf, REG_LINK, b"unsupported-synthetic-link-type")?;
        ensure!(key.observe().is_err(), "REG_LINK type admitted");
        ensure!(
            RetainedProfileRegistry::open(&token, &first, &actor).is_err(),
            "new link inventory admitted"
        );
        fixture_value(&fixture_leaf, REG_BINARY, b"synthetic-v1")?;
        ensure!(
            present(&key)? == original,
            "restored synthetic bytes differ"
        );
        fixture_leaf.close()?;
        fixture_parent.close()?;
        fixture_root.close()?;
        cases.push("populated_descendant_value_and_link_refusals");

        // Compose observations of both storage surfaces on the same two
        // exclusively born profiles. No production receipt or deletion API is
        // exposed by either guard. Close filesystem handles BEFORE SDK delete.
        use crate::account_profile_filesystem::{FilesystemObservation, RetainedProfileFilesystem};
        use std::io::Write;
        let empty_files = RetainedProfileFilesystem::open(&first, &actor)?;
        let control_files = RetainedProfileFilesystem::open(&second, &actor)?;
        ensure!(
            empty_files.distinct_root(&control_files),
            "filesystem profile roots overlap"
        );
        cases.push("actual_profile_filesystem_roots_are_distinct");
        let fixture_directory =
            RetainedProfileFilesystem::fixture_ac(&first, &actor)?.join("QualificationFilesystem");
        std::fs::create_dir(&fixture_directory)?;
        let fixture_file = fixture_directory.join("synthetic.txt");
        {
            let mut file = std::fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(&fixture_file)?;
            file.write_all(b"synthetic-v1")?;
            file.sync_all()?;
        }
        ensure!(
            empty_files.observe().is_err(),
            "added filesystem topology ignored"
        );
        empty_files.close()?;
        let files = RetainedProfileFilesystem::open(&first, &actor)?;
        std::fs::write(&fixture_file, b"synthetic-v2")?;
        ensure!(files.observe().is_err(), "filesystem content drift ignored");
        std::fs::write(&fixture_file, b"synthetic-v1")?;
        ensure!(
            matches!(files.observe()?, FilesystemObservation::Present),
            "restored filesystem observation differs"
        );
        ensure!(
            matches!(control_files.observe()?, FilesystemObservation::Present),
            "separate filesystem control changed"
        );
        cases.push("populated_filesystem_topology_and_content_refused");
        // Trusted bounded mutations stand in for post-command contents. These
        // observations do not prove process quiescence or authorize cleanup.
        let file_id = files.original_root_id()?;
        let file_root = files.retain_root_for_use()?;
        let registry_roots =
            RetainedProfileRegistry::open(&token, &first, &actor)?.retain_roots_for_use()?;
        std::fs::write(&fixture_file, b"synthetic-post-use")?;
        let added_file = fixture_directory.join("new-cache.txt");
        std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&added_file)?
            .write_all(b"bounded post-use cache")?;
        let sdk = RetainedKey::open(&token, &first, &actor)?;
        let added_key = fixture_child(&sdk, "PostUse")?;
        fixture_value(&added_key, REG_BINARY, b"bounded post-use setting")?;
        added_key.close()?;
        sdk.close()?;
        let files = file_root.capture_after_use()?;
        let post_registry = registry_roots.capture_after_use()?;
        ensure!(
            files.original_root_id()? == file_id,
            "post-use filesystem root differs"
        );
        ensure!(
            matches!(files.observe()?, FilesystemObservation::Present),
            "post-use filesystem drift"
        );
        present(&post_registry)?;
        ensure!(
            present(&control)? == unchanged,
            "post-use changed separate registry control"
        );
        ensure!(
            matches!(control_files.observe()?, FilesystemObservation::Present),
            "post-use changed separate filesystem control"
        );
        cases.push("bounded_post_use_inventory_retains_original_roots");
        let parent = files.close_tree_for_observation()?;
        let control_parent = control_files.close_tree_for_observation()?;
        for retained in [&parent, &control_parent] {
            let error = retained
                .observe_absent()
                .err()
                .context("live profile reported absent")?;
            ensure!(
                error.to_string().contains("still present or replaced"),
                "unexpected parent observation failure"
            );
        }
        cases.push("checked_filesystem_closes_before_sdk_delete");
        let registry_parent = post_registry.close_trees_for_observation()?;
        ensure!(
            registry_parent
                .observe_absent()
                .err()
                .context("live registry reported absent")?
                .to_string()
                .contains("still present or replaced"),
            "unexpected registry-parent failure"
        );

        delete(&first)?;
        ensure!(
            matches!(key.observe()?, KeyObservation::OriginalKeyDeleted),
            "first original key did not report deletion"
        );
        ensure!(
            matches!(repeat.observe()?, KeyObservation::OriginalKeyDeleted),
            "repeat original key did not report deletion"
        );
        cases.push("original_retained_keys_report_deleted");
        ensure!(
            present(&control)? == unchanged,
            "separate profile key changed"
        );
        cases.push("separate_control_unchanged");

        key.close()?;
        repeat.close()?;
        close_token(token)?;
        delete(&first)?; // One bounded SDK retry after all first-profile handles closed.
        let registry_control_parent = control.close_trees_for_observation()?;
        close_token(control_token)?;
        delete(&second)?;
        cases.push("checked_closes_before_bounded_sdk_cleanup");
        parent.observe_absent()?;
        control_parent.observe_absent()?;
        parent.close()?;
        control_parent.close()?;
        cases.push("retained_filesystem_parents_confirm_post_cleanup_absence");
        registry_parent.observe_absent()?;
        registry_control_parent.observe_absent()?;
        registry_parent.close()?;
        registry_control_parent.close()?;
        cases.push("retained_registry_parents_confirm_post_cleanup_absence");
        Ok(())
    }));
    let passed = matches!(&result, Ok(Ok(())));
    let report = serde_json::json!({"test":TEST, "run_id":run, "run_attempt":attempt,
        "passed":passed, "completed_cases":cases, "synthetic_profile_limit":2,
        "no_account_or_process_creation":true, "synthetic_registry_value_writes_only":true, "bounded_descendant_inventory":true,
        "bounded_filesystem_inventory":true, "synthetic_filesystem_writes_only":true,
        "retained_parent_absence_observations":true,
        "retained_registry_parent_observations":true,
        "post_use_root_inventory":true, "process_quiescence_authority":false,
        "no_shared_acl_changes":true, "production_cleanup_authority":false,
        "complete_storage_inventory":false});
    serde_json::to_writer_pretty(output, &report).unwrap();
    match result {
        Ok(result) => result.unwrap(),
        Err(panic) => std::panic::resume_unwind(panic),
    }
}
