//! Explicit candidate runner. Credentials never enter this module; its caller
//! has already selected the installed account from policy and logged on through
//! the trusted broker. The broker retains the journaled private namespace and both
//! profile identities until this runner has verified job/profile teardown.

use anyhow::{Context, Result, ensure};
use serde::{Deserialize, Serialize};
use std::mem::size_of;
use std::path::{Path, PathBuf};
use windows::Win32::Foundation::{ERROR_FILE_NOT_FOUND, ERROR_NOT_FOUND, WAIT_OBJECT_0};
use windows::Win32::Security::{EqualSid, SID_AND_ATTRIBUTES};
use windows::Win32::System::JobObjects::IsProcessInJob;
use windows::Win32::System::Threading::*;
use windows::core::{PCWSTR, PWSTR};

use crate::appcontainer::NetworkAccess;
use crate::launch::{ProcThreadAttrs, SpawnedChild};

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AccountContainerSpec {
    /// Validated installed execution account selected by trusted network policy.
    pub account_sid: String,
    /// Trusted host owner; never substitute the execution account here.
    pub owner_sid: String,
    /// Fresh journaled name; broker has already prepared and retains namespace.
    pub container: String,
    pub cwd: PathBuf,
    pub argv: Vec<String>,
    /// Complete allowlist. No account/broker environment is inherited.
    pub env: Vec<(String, String)>,
    /// Required explicitly on the wire; no implicit online or legacy fallback.
    pub network: NetworkAccess,
    pub protected_paths: Vec<String>,
    pub protected_ancestors: Vec<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AccountProfileCleanup {
    pub account_sid: String,
    pub container: String,
}

impl AccountContainerSpec {
    pub fn validate(&self) -> Result<()> {
        let account = crate::sid::LocalPsid::from_string(&self.account_sid)?;
        let owner = crate::sid::LocalPsid::from_string(&self.owner_sid)?;
        ensure!(
            unsafe { EqualSid(account.as_psid(), owner.as_psid()).is_err() },
            "execution account and trusted owner must differ"
        );
        crate::appcontainer::derive(&self.container)?;
        ensure!(
            !self.argv.is_empty() && Path::new(&self.argv[0]).is_absolute(),
            "candidate executable must be absolute"
        );
        ensure!(
            self.argv.iter().all(|argument| !argument.contains('\0')),
            "command arguments contain NUL"
        );
        ensure!(
            self.cwd.is_absolute() && !self.cwd.to_string_lossy().contains('\0'),
            "candidate cwd must be an absolute path"
        );
        ensure!(
            !self.protected_paths.is_empty(),
            "ambient filesystem probes are required"
        );
        for path in self.protected_paths.iter().chain(&self.protected_ancestors) {
            ensure!(
                Path::new(path).is_absolute() && !path.contains('\0'),
                "protected probe path must be absolute"
            );
        }
        for (key, value) in &self.env {
            ensure!(
                !key.is_empty() && !key.contains(['=', '\0']) && !value.contains('\0'),
                "invalid candidate environment entry"
            );
        }
        Ok(())
    }
}

/// Wire requests carry data only. Keep every candidate runner/logon boundary
/// unavailable until an authenticated, opaque lifecycle authority replaces this
/// gate. The broker's higher-level refusal alone does not cover direct callers.
pub(crate) fn require_lifecycle_authority() -> Result<()> {
    anyhow::bail!(
        "candidate runner lifecycle authority unavailable; account execution and profile cleanup remain disabled"
    )
}

/// Reserved account entry point, refused before validation or native effects.
pub fn run(spec: &AccountContainerSpec) -> Result<u32> {
    require_lifecycle_authority()?;
    spec.validate()?;
    crate::account_token::verify_account_runner(&spec.account_sid)?;
    crate::job::require_compatible_host_job()?;
    ensure!(spec.cwd.is_dir(), "candidate cwd does not exist");
    ensure!(
        !crate::winsta::on_default_desktop(),
        "candidate runner must use an isolated desktop"
    );
    let _executable = crate::self_protect::share_lock_current_exe()?;
    crate::self_protect::install_broker_dacl(Some(&spec.owner_sid))?;
    let capability_sids = spec
        .network
        .capability_sids()
        .iter()
        .map(|sid| crate::sid::LocalPsid::from_string(sid))
        .collect::<Result<Vec<_>>>()?;
    let capabilities = capability_sids
        .iter()
        .map(|sid| SID_AND_ATTRIBUTES {
            Sid: sid.as_psid(),
            Attributes: 0x4,
        })
        .collect::<Vec<_>>();
    // This is the account's HKCU profile; the broker has a separate host HKCU
    // profile with the same unique name. Never reuse either existing profile.
    crate::appcontainer::create(&spec.container, &capabilities)?;
    let result = run_with_profile(spec);
    let cleanup = cleanup_profile(&AccountProfileCleanup {
        account_sid: spec.account_sid.clone(),
        container: spec.container.clone(),
    });
    match (result, cleanup) {
        (Ok(code), Ok(())) => Ok(code),
        (Err(error), Ok(())) => Err(error),
        (Ok(_), Err(error)) => {
            Err(error).context("account profile cleanup failed; recovery required")
        }
        (Err(error), Err(cleanup)) => Err(error).context(format!(
            "account profile cleanup also failed; recovery required: {cleanup:#}"
        )),
    }
}

fn run_with_profile(spec: &AccountContainerSpec) -> Result<u32> {
    // The account deliberately cannot edit the window-station DACL. The host
    // must hold the package station grant and fresh Low desktop for this entire
    // runner lifetime; missing package access fails process creation.
    let desktop = crate::winsta::current_desktop_name().context("candidate desktop is missing")?;
    ensure!(
        desktop.starts_with("srt-sb-"),
        "candidate runner did not receive a fresh sandbox desktop"
    );
    let mut desktop = crate::util::wstr(&format!(
        "{}\\{desktop}",
        crate::winsta::current_winsta_name()?
    ));
    let token = crate::account_token::create_account_lowbox(
        &spec.container,
        &spec.account_sid,
        &spec.owner_sid,
        spec.network,
    )?;
    let job = crate::job::Job::for_account_execution(
        &spec.container,
        &spec.account_sid,
        &spec.owner_sid,
    )?;
    let mut env = isolated_environment(spec)?;
    let application = crate::util::wstr(&spec.argv[0]);
    let mut command = crate::util::wstr(&crate::launch::build_cmdline(
        Path::new(&spec.argv[0]),
        &spec.argv[1..],
    ));
    let cwd = crate::util::wstr(&spec.cwd.to_string_lossy());
    let stdio = crate::launch::collect_inheritable_std_handles();
    // Runner logon supplies all three pipes. Missing streams indicate a broken
    // trusted handoff, not a reason to inherit arbitrary alternate handles.
    ensure!(
        stdio.iter().all(|handle| !handle.0.is_null()),
        "candidate runner requires three valid stdio handles"
    );
    let mut handles = stdio.to_vec();
    handles.sort_by_key(|handle| handle.0 as usize);
    handles.dedup();
    let mut attributes = ProcThreadAttrs::new(3)?;
    // Same JIT-compatible mitigation set as the baseline launch stack.
    let mitigation = (1u64 << 32) | (1u64 << 48) | (1u64 << 52) | (1u64 << 56);
    attributes.set_mitigation_policy(&mitigation)?;
    attributes.set_handle_list(&mut handles)?;
    attributes.set_job(&job)?;
    let mut info = STARTUPINFOEXW::default();
    info.StartupInfo.cb = size_of::<STARTUPINFOEXW>() as u32;
    info.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
    info.StartupInfo.hStdInput = stdio[0];
    info.StartupInfo.hStdOutput = stdio[1];
    info.StartupInfo.hStdError = stdio[2];
    info.StartupInfo.lpDesktop = PWSTR(desktop.as_mut_ptr());
    info.lpAttributeList = attributes.list();
    let mut flags = CREATE_SUSPENDED
        | CREATE_UNICODE_ENVIRONMENT
        | CREATE_NO_WINDOW
        | EXTENDED_STARTUPINFO_PRESENT;
    if crate::job::require_compatible_host_job()? {
        flags |= CREATE_BREAKAWAY_FROM_JOB;
    }
    let mut pi = PROCESS_INFORMATION::default();
    unsafe {
        CreateProcessAsUserW(
            Some(token.raw()),
            PCWSTR(application.as_ptr()),
            Some(PWSTR(command.as_mut_ptr())),
            None,
            None,
            true,
            flags,
            Some(env.as_mut_ptr().cast()),
            PCWSTR(cwd.as_ptr()),
            &info.StartupInfo,
            &mut pi,
        )
    }
    .context("create suspended dedicated-account AppContainer in named job")?;
    let mut child = SpawnedChild::new(pi);
    let mut assigned = windows::core::BOOL::default();
    unsafe {
        IsProcessInJob(child.process(), Some(job.raw()), &mut assigned)?;
    }
    ensure!(
        assigned.as_bool(),
        "candidate child is outside containment job"
    );
    crate::account_token::verify_account_process(
        child.process(),
        &spec.container,
        &spec.account_sid,
        spec.network,
    )?;
    crate::token::verify_child_filesystem(
        child.process(),
        &spec.protected_paths,
        &spec.protected_ancestors,
    )?;
    crate::account_token::verify_child_job_isolation(child.process(), &spec.container)?;
    ensure!(
        unsafe { ResumeThread(child.thread()) } != u32::MAX,
        "cannot resume verified candidate child"
    );
    child.defuse();
    ensure!(
        unsafe { WaitForSingleObject(child.process(), INFINITE) } == WAIT_OBJECT_0,
        "candidate child wait failed"
    );
    let mut code = 0;
    unsafe {
        GetExitCodeProcess(child.process(), &mut code)?;
    }
    drop(attributes);
    drop(child);
    // Job::drop terminates descendants and waits for zero active processes.
    // On unverified exit it terminates this runner (125), preserving the host
    // journal instead of returning into profile/ACL cleanup.
    drop(job);
    Ok(code)
}

fn isolated_environment(spec: &AccountContainerSpec) -> Result<Vec<u16>> {
    let mut env = spec.env.clone();
    // Added last so callers cannot point HOME/TEMP back into either real user
    // profile. Account profile ownership already supplies its ordinary access.
    env.extend(crate::appcontainer::environment(&spec.container)?);
    Ok(crate::launch::build_env_block_with_inheritance(&env, false))
}

/// Reserved account-side recovery, unavailable without original composite
/// ownership and full writer quiescence. An identity/name is not that authority.
pub fn cleanup_profile(spec: &AccountProfileCleanup) -> Result<()> {
    require_lifecycle_authority()?;
    crate::appcontainer::derive(&spec.container)?;
    crate::account_token::verify_account_runner(&spec.account_sid)?;
    crate::job::wait_for_execution_exit(&spec.container)?;
    let name = crate::util::wstr(&spec.container);
    match unsafe {
        windows::Win32::Security::Isolation::DeleteAppContainerProfile(PCWSTR(name.as_ptr()))
    } {
        Ok(()) => Ok(()),
        Err(error)
            if error.code() == ERROR_FILE_NOT_FOUND.to_hresult()
                || error.code() == ERROR_NOT_FOUND.to_hresult() =>
        {
            Ok(())
        }
        Err(error) => Err(error).context("delete account execution profile"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn spec() -> AccountContainerSpec {
        AccountContainerSpec {
            account_sid: "S-1-5-21-1-2-3-1001".into(),
            owner_sid: "S-1-5-21-1-2-3-1002".into(),
            container: "Alysis.Execution.0123456789abcdef0123456789abcdef".into(),
            cwd: PathBuf::from("C:\\workspace"),
            argv: vec!["C:\\Windows\\System32\\cmd.exe".into()],
            env: vec![],
            network: NetworkAccess::Off,
            protected_paths: vec!["C:\\Windows".into()],
            protected_ancestors: vec![],
        }
    }

    #[test]
    fn account_request_requires_distinct_identity_and_explicit_network_policy() {
        let mut request = spec();
        assert!(request.validate().is_ok());
        request.owner_sid = request.account_sid.clone();
        assert!(request.validate().is_err());
        let mut value = serde_json::to_value(spec()).unwrap();
        value.as_object_mut().unwrap().remove("network");
        assert!(serde_json::from_value::<AccountContainerSpec>(value).is_err());
    }

    #[test]
    fn account_request_refuses_ambiguous_or_injected_paths_and_environment() {
        let mut request = spec();
        request.argv[0] = "cmd.exe".into();
        assert!(request.validate().is_err());
        let mut request = spec();
        request.env.push(("PATH\0SECRET".into(), "x".into()));
        assert!(request.validate().is_err());
        let mut request = spec();
        request.protected_paths.clear();
        assert!(request.validate().is_err());
        let mut value = serde_json::to_value(spec()).unwrap();
        value["raw_exec"] = serde_json::json!(true);
        assert!(serde_json::from_value::<AccountContainerSpec>(value).is_err());
    }

    #[test]
    fn account_cleanup_refuses_external_profile_and_wrong_account_before_mutation() {
        let cleanup = AccountProfileCleanup {
            account_sid: crate::sid::current_user_sid().unwrap(),
            container: "Unrelated.Profile".into(),
        };
        assert!(cleanup_profile(&cleanup).is_err());
        let cleanup = AccountProfileCleanup {
            account_sid: "S-1-5-18".into(),
            container: spec().container,
        };
        assert!(cleanup_profile(&cleanup).is_err());
    }

    #[test]
    fn account_commands_have_distinct_wire_variants() {
        let command = crate::runner::RunnerCmd::AccountAppContainer(spec());
        let bytes = crate::runner::encode_cmd(&command).unwrap();
        assert!(matches!(
            serde_json::from_slice::<crate::runner::RunnerCmd>(&bytes[4..]).unwrap(),
            crate::runner::RunnerCmd::AccountAppContainer(_)
        ));
        assert!(!String::from_utf8_lossy(&bytes[4..]).contains("\"Exec\""));
    }

    #[test]
    fn account_direct_entries_require_lifecycle_authority_before_native_effects() {
        let mut request = spec();
        request.container = "not-an-execution-profile".into();
        let cleanup = AccountProfileCleanup {
            account_sid: "invalid".into(),
            container: request.container.clone(),
        };
        for error in [
            run(&request).unwrap_err(),
            cleanup_profile(&cleanup).unwrap_err(),
        ] {
            assert!(
                error
                    .to_string()
                    .contains("candidate runner lifecycle authority unavailable"),
                "{error:#}"
            );
        }
    }

    #[test]
    fn account_logon_entries_require_lifecycle_authority_before_native_effects() {
        let mut request = spec();
        request.container = "not-an-execution-profile".into();
        let cleanup = AccountProfileCleanup {
            account_sid: "invalid".into(),
            container: request.container.clone(),
        };
        let run_command = crate::runner::RunnerCmd::AccountAppContainer(request.clone());
        let cleanup_command = crate::runner::RunnerCmd::CleanupAccountAppContainer(cleanup.clone());
        // Invalid request shapes stop the old code before logon/native effects.
        // Credential arguments are empty; no real credential is accessed.
        for error in [
            crate::logon::spawn_account_appcontainer("", "", &request, true).unwrap_err(),
            crate::logon::spawn_account_profile_cleanup("", "", &cleanup, true).unwrap_err(),
            crate::logon::spawn_runner("", "", "invalid", None, &run_command, true).unwrap_err(),
            crate::logon::spawn_runner("", "", "invalid", None, &cleanup_command, true)
                .unwrap_err(),
        ] {
            assert!(
                error
                    .to_string()
                    .contains("candidate runner lifecycle authority unavailable"),
                "{error:#}"
            );
        }
    }
}
