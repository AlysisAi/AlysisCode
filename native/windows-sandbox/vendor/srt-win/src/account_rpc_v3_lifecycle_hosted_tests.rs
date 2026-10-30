//! The real writer purpose and original namespace path under the exact V3 policy.
//! Normal retirement and three crash cuts only; never production activation.
use super::*;
use crate::account_rpc_v3_lifecycle_fixture::{POLICY, enter, enter_shadow};
use crate::account_state::execution::resources::namespace_native::RootBirthKind;
use std::io::{Seek, SeekFrom, Write};

const CASES: [&str; 3] = [
    "initialized-writer-family-live-use",
    "initialized-writer-family-captured-hot-receipt",
    "initialized-writer-family-removed-hot-receipt",
];

pub(super) fn run() {
    let _admission = enter().unwrap();
    run_inner(false);
}
pub(super) fn run_shadow() {
    let _admission = enter_shadow().unwrap();
    run_inner(true);
}
// Selection is a private fixed entrypoint argument, never a report/env value.
fn run_inner(shadow: bool) {
    station::require_station_host().unwrap();
    let (name, expected_birth) = if shadow {
        (
            "account_rpc_v3_shadow_lifecycle_disposable",
            RootBirthKind::SessionShadowDirectoryV1,
        )
    } else {
        (
            "account_rpc_v3_lifecycle_disposable",
            RootBirthKind::LegacyUnspecified,
        )
    };
    let root = std::path::PathBuf::from(std::env::var_os("RUNNER_TEMP").unwrap());
    let mut report = std::fs::File::create_new(root.join("alysis-rpc-v3-lifecycle.json")).unwrap();
    let mut publish = |stage: &str,
                       normal: &Option<serde_json::Value>,
                       completed: &[&str],
                       failure: Option<&str>,
                       hresult: Option<i32>,
                       disabled: Option<bool>,
                       passed: bool|
     -> Result<()> {
        let value = serde_json::json!({"schema":1,"test":name,
            "run_id":std::env::var("GITHUB_RUN_ID")?,"run_attempt":std::env::var("GITHUB_RUN_ATTEMPT")?,
            "expected_rpc_policy":POLICY,"expected_namespace_birth":expected_birth,"stage":stage,"normal_retirement":normal,
            "selected_crash_cases":CASES,"completed_crash_cases":completed,
            "failure_kind":failure,"hresult":hresult,"accounts_disabled_afterward":disabled,
            "passed":passed,"production_ready":false,"full_matrix_qualified":false,
            "station_diagnostic_guard_used":false,"untrusted_command_qualified":false});
        let bytes = serde_json::to_vec(&value)?;
        ensure!(bytes.len() < 131072, "V3 lifecycle report exceeds bound");
        report.seek(SeekFrom::Start(0))?;
        report.write_all(&bytes)?;
        report.set_len(bytes.len() as u64)?;
        report.sync_all()?;
        Ok(())
    };
    let mut stage = "fresh_principals";
    let mut normal = None;
    let mut completed = Vec::new();
    publish(stage, &normal, &completed, None, None, None, false).unwrap();
    let mut principals = Principals {
        offline: None,
        online: None,
        group: None,
    };
    let password = Password(format!(
        "Aa1!{}{}",
        random_id().unwrap(),
        random_id().unwrap()
    ));
    let outcome = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| -> Result<()> {
        create(&mut principals, &password)?;
        stage = "normal_writer_retirement";
        publish(stage, &normal, &completed, None, None, None, false)?;
        let fixture = bound_fixture(
            principals.offline.as_deref().unwrap(),
            principals.online.as_deref().unwrap(),
            principals.group.as_deref().unwrap(),
        )?;
        normal = Some(if shadow {
            super::super::super::paired::hosted_rpc_v3_shadow_profile_pair(fixture, &password.0)?
        } else {
            super::super::super::paired::hosted_rpc_v3_profile_pair(fixture, &password.0)?
        });
        for point in CASES {
            stage = point;
            publish(stage, &normal, &completed, None, None, None, false)?;
            let fixture = bound_fixture(
                principals.offline.as_deref().unwrap(),
                principals.online.as_deref().unwrap(),
                principals.group.as_deref().unwrap(),
            )?;
            if shadow {
                super::super::super::paired::hosted_rpc_v3_shadow_profile_crash(
                    fixture,
                    &password.0,
                    point,
                )?;
            } else {
                super::super::super::paired::hosted_rpc_v3_profile_crash(
                    fixture,
                    &password.0,
                    point,
                )?;
            }
            completed.push(point);
        }
        Ok(())
    }));
    let disabled = principals.disable_all();
    let (ok, failure, error) = match &outcome {
        Ok(Ok(())) if disabled.is_ok() => (true, None, None),
        Ok(Ok(())) => (true, Some("account_disable_error"), disabled.as_ref().err()),
        Ok(Err(error)) => (false, Some("test_error"), Some(error)),
        Err(_) => (false, Some("panic"), None),
    };
    let hresult = error.and_then(|error| {
        error
            .chain()
            .find_map(|cause| cause.downcast_ref::<windows::core::Error>())
            .map(|e| e.code().0)
    });
    let passed = ok
        && disabled.is_ok()
        && normal.as_ref().is_some_and(|v| v["passed"] == true)
        && completed.as_slice() == CASES.as_slice();
    if passed {
        stage = "complete";
    }
    publish(
        stage,
        &normal,
        &completed,
        failure,
        hresult,
        Some(disabled.is_ok()),
        passed,
    )
    .unwrap();
    assert!(
        passed,
        "V3 lifecycle failed at {stage}; HRESULT={hresult:?}; sanitized report retained"
    );
}
