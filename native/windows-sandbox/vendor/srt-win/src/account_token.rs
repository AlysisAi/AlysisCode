//! Experimental dedicated-account AppContainer tokens.
//!
//! Unlike the current host-user backend, the selected account itself supplies
//! the independent filesystem deny check. No restricting SID is added. Earlier
//! compatibility observations used an obsolete namespace prototype; the current
//! composed lowbox still fails Schannel/LSA qualification. This is not a proven
//! TLS-compatible token policy. This module is available
//! only to the explicitly selected, separately qualified candidate backend.

use anyhow::{Context, Result, ensure};
use std::mem::size_of;
use windows::Wdk::Foundation::OBJECT_ATTRIBUTES;
use windows::Wdk::Storage::FileSystem::NtOpenDirectoryObject;
use windows::Win32::Foundation::{HANDLE, LUID, NTSTATUS, OBJ_CASE_INSENSITIVE, UNICODE_STRING};
use windows::Win32::Security::*;
use windows::Win32::System::LibraryLoader::{GetModuleHandleW, GetProcAddress};
use windows::Win32::System::Threading::OpenProcessToken;
use windows::core::PWSTR;

use crate::appcontainer::NetworkAccess;
use crate::util::OwnedHandle;

#[cfg(test)]
#[path = "account_token_capture.rs"]
pub(crate) mod capture;
#[path = "account_runner_policy.rs"]
pub(crate) mod runner_policy;

/// The namespace has already been prepared by the trusted host broker and is
/// retained by that broker until the execution job is empty. The caller runs as
/// the selected dedicated account, with its profile loaded. It must never pass
/// the returned token to a legacy raw-execution path.
pub fn create_account_lowbox(
    container: &str,
    account_sid: &str,
    owner_sid: &str,
    network: NetworkAccess,
) -> Result<OwnedHandle> {
    let (_, lowbox) = prepare_account_lowbox(container, account_sid, owner_sid, network)?;
    Ok(lowbox)
}

/// Test-only comparison of process-attribute and explicit-token birth. Retain
/// the exact filtered primary supplied to NtCreateLowBoxToken; never substitute
/// a raw account token or the ordinary backend's different filtering policy.
#[cfg(test)]
pub(crate) fn prepare_birth_comparison(
    container: &str,
    account_sid: &str,
    owner_sid: &str,
    network: NetworkAccess,
) -> Result<(OwnedHandle, OwnedHandle)> {
    prepare_account_lowbox(container, account_sid, owner_sid, network)
}

/// Fixed disposable comparison with KernelBase's AppContainer constructor.
/// Chromium uses this entrypoint for OS-specific namespace setup. It is not a
/// supported production contract here: no fallback, new capability or raw base.
#[cfg(test)]
pub(crate) fn compare_kernelbase_lowbox(
    filtered: &OwnedHandle,
    container: &str,
    account_sid: &str,
    constructor_returned: &mut bool,
) -> Result<OwnedHandle> {
    ensure!(
        !*constructor_returned,
        "constructor comparison already returned"
    );
    crate::account_impersonation::require_process_identity()?;
    verify_user(filtered.raw(), account_sid)?;
    crate::account_namespace::verify_noninheritable(filtered.raw())?;
    let package = crate::appcontainer::derive(container)?;
    // The OS implementation accepts an in/out structure and may temporarily
    // replace its capability array while constructing the token.
    let mut capabilities = SECURITY_CAPABILITIES {
        AppContainerSid: package.raw(),
        ..Default::default()
    };
    type Create = unsafe extern "system" fn(
        HANDLE,
        *mut SECURITY_CAPABILITIES,
        *mut HANDLE,
    ) -> windows::core::BOOL;
    let module = unsafe { GetModuleHandleW(windows::core::w!("kernelbase.dll"))? };
    let function = unsafe { GetProcAddress(module, windows::core::s!("CreateAppContainerToken")) }
        .context("KernelBase AppContainer comparison unavailable")?;
    let create: Create = unsafe { std::mem::transmute(function) };
    let mut token = HANDLE::default();
    unsafe { create(filtered.raw(), &mut capabilities, &mut token) }
        .ok()
        .context("KernelBase AppContainer comparison refused token creation")?;
    let token = OwnedHandle(token);
    *constructor_returned = true;
    ensure!(
        !token.raw().is_invalid(),
        "KernelBase returned invalid token"
    );
    crate::account_namespace::verify_noninheritable(token.raw())?;
    verify_token(token.raw(), package.raw(), account_sid, NetworkAccess::Off)?;
    Ok(token)
}

fn prepare_account_lowbox(
    container: &str,
    account_sid: &str,
    owner_sid: &str,
    network: NetworkAccess,
) -> Result<(OwnedHandle, OwnedHandle)> {
    let restricted = filtered_account_base(account_sid, owner_sid)?;
    let package = crate::appcontainer::derive(container)?;
    let token = prepare_from_filtered(&restricted, &package, account_sid, network)?;
    Ok((restricted, token))
}

// Shared with the fixed capture comparison so it never creates a one-object
// lowbox first. The same original raw-account admission and filtering applies.
fn filtered_account_base(account_sid: &str, owner_sid: &str) -> Result<OwnedHandle> {
    crate::account_impersonation::require_process_identity()?;
    let account = crate::sid::LocalPsid::from_string(account_sid)?;
    let owner = crate::sid::LocalPsid::from_string(owner_sid)?;
    ensure!(
        unsafe { EqualSid(account.as_psid(), owner.as_psid()).is_err() },
        "sandbox account must differ from its owner"
    );
    let base = OwnedHandle(crate::token::open_self_token()?);
    runner_policy::require(base.raw(), account_sid, "S-1-5-32-545")?;
    let restricted = OwnedHandle(crate::token::make_sandbox_token_for_owner(
        base.raw(),
        Some(owner_sid),
    )?);
    crate::account_namespace::verify_noninheritable(restricted.raw())?;
    Ok(restricted)
}

fn prepare_from_filtered(
    restricted: &OwnedHandle,
    package: &crate::appcontainer::ContainerSid,
    account_sid: &str,
    network: NetworkAccess,
) -> Result<OwnedHandle> {
    let path = crate::account_namespace::namespace_path(&package.string()?)?;
    let (mut path, length) = crate::util::counted_utf16(&path);
    let name = UNICODE_STRING {
        Length: length,
        MaximumLength: length,
        Buffer: PWSTR(path.as_mut_ptr()),
    };
    let attributes = OBJECT_ATTRIBUTES {
        Length: size_of::<OBJECT_ATTRIBUTES>() as u32,
        ObjectName: &name,
        Attributes: OBJ_CASE_INSENSITIVE,
        ..Default::default()
    };
    let mut directory = HANDLE::default();
    unsafe { NtOpenDirectoryObject(&mut directory, 0x000f000f, &attributes) }
        .ok()
        .context("open broker-prepared private AppContainer namespace")?;
    let directory = OwnedHandle(directory);
    let capability_sids = network
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
    let token = native_lowbox(
        restricted.raw(),
        package.raw(),
        &capabilities,
        &[directory.raw()],
    )?;
    crate::account_namespace::verify_noninheritable(token.raw())?;
    verify_token(token.raw(), package.raw(), account_sid, network)?;
    Ok(token)
}

fn native_lowbox(
    filtered: HANDLE,
    package: PSID,
    capabilities: &[SID_AND_ATTRIBUTES],
    handles: &[HANDLE],
) -> Result<OwnedHandle> {
    type CreateLowBox = unsafe extern "system" fn(
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
    // Documented native API. Resolve from the already loaded system ntdll;
    // never search PATH or load a caller-selected DLL.
    let module = unsafe { GetModuleHandleW(windows::core::w!("ntdll.dll"))? };
    let function = unsafe { GetProcAddress(module, windows::core::s!("NtCreateLowBoxToken")) }
        .context("NtCreateLowBoxToken is unavailable")?;
    let create: CreateLowBox = unsafe { std::mem::transmute(function) };
    let attributes = OBJECT_ATTRIBUTES {
        Length: size_of::<OBJECT_ATTRIBUTES>() as u32,
        ..Default::default()
    };
    let mut token = HANDLE::default();
    unsafe {
        create(
            &mut token,
            filtered,
            TOKEN_ALL_ACCESS.0,
            &attributes,
            package,
            capabilities.len() as u32,
            if capabilities.is_empty() {
                std::ptr::null()
            } else {
                capabilities.as_ptr()
            },
            handles.len() as u32,
            handles.as_ptr(),
        )
    }
    .ok()
    .context("create dedicated-account AppContainer token")?;
    Ok(OwnedHandle(token))
}

/// Validate the actual suspended child, not just the token offered at creation.
/// The caller must also verify job assignment and filesystem denial, then resume.
pub fn verify_account_process(
    process: HANDLE,
    container: &str,
    account_sid: &str,
    network: NetworkAccess,
) -> Result<()> {
    let package = crate::appcontainer::derive(container)?;
    let mut handle = HANDLE::default();
    unsafe {
        OpenProcessToken(process, TOKEN_QUERY, &mut handle)?;
    }
    let token = OwnedHandle(handle);
    verify_token(token.raw(), package.raw(), account_sid, network)
}

/// Only a trusted raw account runner may create or recover its profiles. An
/// untrusted child must not re-enter these maintenance/launch entrypoints.
pub(crate) fn verify_account_runner(account_sid: &str) -> Result<()> {
    crate::account_impersonation::require_process_identity()?;
    let base = OwnedHandle(crate::token::open_self_token()?);
    runner_policy::require(base.raw(), account_sid, "S-1-5-32-545")
}

/// Shared profile inventory supports both the host owner and dedicated account.
/// Establish only the current non-impersonating raw actor; this is NOT dedicated
/// runner admission, profile ownership, quiescence or deletion authority.
pub(crate) fn verify_raw_profile_actor(actor_sid: &str) -> Result<()> {
    crate::account_impersonation::require_process_identity()?;
    let token = OwnedHandle(crate::token::open_self_token()?);
    verify_user(token.raw(), actor_sid)?;
    ensure!(
        read_u32(token.raw(), TokenIsAppContainer)? == 0,
        "profile inventory actor is already contained"
    );
    Ok(())
}

/// Prove the actual suspended child cannot reopen its named job to weaken or
/// terminate containment. The package lowbox check must deny the account ACE.
pub(crate) fn verify_child_job_isolation(process: HANDLE, container: &str) -> Result<()> {
    crate::account_impersonation::require_process_identity()?;
    use windows::Win32::Foundation::{ERROR_ACCESS_DENIED, ERROR_FILE_NOT_FOUND};
    use windows::Win32::System::JobObjects::OpenJobObjectW;
    use windows::core::PCWSTR;
    let mut handle = HANDLE::default();
    unsafe {
        OpenProcessToken(process, TOKEN_QUERY | TOKEN_DUPLICATE, &mut handle)?;
    }
    let token = OwnedHandle(handle);
    let name = crate::util::wstr(&crate::job::execution_name(container)?);
    crate::account_impersonation::with_token(&token, || -> Result<()> {
        // ASSIGN_PROCESS, SET_ATTRIBUTES, TERMINATE, SET_SECURITY_ATTRIBUTES,
        // WRITE_DAC and WRITE_OWNER are independent mutation rights.
        for access in [0x1u32, 0x2, 0x8, 0x10, 0x40000, 0x80000] {
            match unsafe { OpenJobObjectW(access, false, PCWSTR(name.as_ptr())) } {
                Ok(handle) => {
                    drop(OwnedHandle(handle));
                    anyhow::bail!(
                        "sandbox token can alter its containment job (right {access:#x})"
                    );
                }
                Err(error)
                    if error.code() == ERROR_ACCESS_DENIED.to_hresult()
                        || error.code() == ERROR_FILE_NOT_FOUND.to_hresult() => {}
                Err(error) => return Err(error).context("cannot establish child job isolation"),
            }
        }
        Ok(())
    })
}

// Word-backed buffers preserve the alignment required by the token structures.
fn information(token: HANDLE, class: TOKEN_INFORMATION_CLASS) -> Result<Vec<usize>> {
    let mut size = 0;
    unsafe {
        let _ = GetTokenInformation(token, class, None, 0, &mut size);
    }
    ensure!(size > 0, "token information has no size");
    let mut words = vec![0; (size as usize).div_ceil(size_of::<usize>())];
    unsafe {
        GetTokenInformation(
            token,
            class,
            Some(words.as_mut_ptr().cast()),
            size,
            &mut size,
        )?;
    }
    Ok(words)
}

fn read_u32(token: HANDLE, class: TOKEN_INFORMATION_CLASS) -> Result<u32> {
    let words = information(token, class)?;
    Ok(unsafe { *words.as_ptr().cast::<u32>() })
}

fn verify_user(token: HANDLE, expected: &str) -> Result<()> {
    let expected = crate::sid::LocalPsid::from_string(expected)?;
    let user = information(token, TokenUser)?;
    let actual = unsafe { (*user.as_ptr().cast::<TOKEN_USER>()).User.Sid };
    ensure!(
        unsafe { EqualSid(actual, expected.as_psid()).is_ok() },
        "sandbox account identity mismatch"
    );
    Ok(())
}

fn verify_token(
    token: HANDLE,
    package: PSID,
    account_sid: &str,
    network: NetworkAccess,
) -> Result<()> {
    verify_user(token, account_sid)?;
    ensure!(
        read_u32(token, TokenIsAppContainer)? == 1,
        "child is not an AppContainer"
    );
    let info = information(token, TokenAppContainerSid)?;
    let actual =
        unsafe { (*info.as_ptr().cast::<TOKEN_APPCONTAINER_INFORMATION>()).TokenAppContainer };
    ensure!(
        unsafe { EqualSid(actual, package).is_ok() },
        "child AppContainer identity mismatch"
    );

    let restrictions = information(token, TokenRestrictedSids)?;
    ensure!(
        unsafe { (*restrictions.as_ptr().cast::<TOKEN_GROUPS>()).GroupCount } == 0,
        "unexpected restricting SIDs in account AppContainer"
    );
    let caps = information(token, TokenCapabilities)?;
    let caps = unsafe { &*caps.as_ptr().cast::<TOKEN_GROUPS>() };
    ensure!(
        caps.GroupCount as usize == network.capability_sids().len(),
        "unexpected AppContainer capabilities"
    );
    let caps =
        unsafe { std::slice::from_raw_parts(caps.Groups.as_ptr(), caps.GroupCount as usize) };
    for expected in network.capability_sids() {
        let expected = crate::sid::LocalPsid::from_string(expected)?;
        ensure!(
            caps.iter().any(|cap| cap.Attributes == 0x4
                && unsafe { EqualSid(cap.Sid, expected.as_psid()).is_ok() }),
            "requested capability missing or changed"
        );
    }
    let info = information(token, TokenIntegrityLevel)?;
    let integrity = unsafe { (*info.as_ptr().cast::<TOKEN_MANDATORY_LABEL>()).Label.Sid };
    let level =
        unsafe { *GetSidSubAuthority(integrity, (*GetSidSubAuthorityCount(integrity) - 1) as u32) };
    ensure!(
        level == 0x1000,
        "account AppContainer must have Low integrity"
    );
    let groups = information(token, TokenGroups)?;
    let groups = unsafe { &*groups.as_ptr().cast::<TOKEN_GROUPS>() };
    let admins = crate::sid::LocalPsid::from_string("S-1-5-32-544")?;
    for group in
        unsafe { std::slice::from_raw_parts(groups.Groups.as_ptr(), groups.GroupCount as usize) }
    {
        if group.Attributes & 0xc0000000 == 0xc0000000
            || unsafe { EqualSid(group.Sid, admins.as_psid()).is_ok() }
        {
            ensure!(
                group.Attributes & 0x10 != 0 && group.Attributes & 0x4 == 0,
                "administrative or shared logon group remained enabled"
            );
        }
    }
    let mut allowed = LUID::default();
    unsafe {
        LookupPrivilegeValueW(
            None,
            windows::core::w!("SeChangeNotifyPrivilege"),
            &mut allowed,
        )?;
    }
    let privileges = information(token, TokenPrivileges)?;
    let privileges = unsafe { &*privileges.as_ptr().cast::<TOKEN_PRIVILEGES>() };
    for privilege in unsafe {
        std::slice::from_raw_parts(
            privileges.Privileges.as_ptr(),
            privileges.PrivilegeCount as usize,
        )
    } {
        ensure!(
            privilege.Luid.LowPart == allowed.LowPart
                && privilege.Luid.HighPart == allowed.HighPart,
            "unexpected privilege in account AppContainer"
        );
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    struct OuterIdentity;
    impl Drop for OuterIdentity {
        fn drop(&mut self) {
            if unsafe { RevertToSelf() }.is_err() {
                std::process::abort();
            }
        }
    }

    fn outer_identity() -> OuterIdentity {
        let token = OwnedHandle(crate::token::open_self_token().unwrap());
        unsafe { ImpersonateLoggedOnUser(token.raw()).unwrap() };
        OuterIdentity
    }

    fn effective_token_id() -> Result<LUID> {
        let mut raw = HANDLE::default();
        unsafe {
            windows::Win32::System::Threading::OpenThreadToken(
                windows::Win32::System::Threading::GetCurrentThread(),
                TOKEN_QUERY,
                true,
                &mut raw,
            )?;
        }
        let token = OwnedHandle(raw);
        let info = information(token.raw(), TokenStatistics)?;
        Ok(unsafe { (*info.as_ptr().cast::<TOKEN_STATISTICS>()).TokenId })
    }

    #[test]
    fn account_maintenance_refuses_existing_thread_identity_without_changing_it() {
        let sid = crate::sid::current_user_sid().unwrap();
        let outer = outer_identity();
        let before = effective_token_id().unwrap();
        let result = verify_account_runner(&sid);
        let after = effective_token_id();
        drop(outer);
        assert_eq!(
            after.unwrap(),
            before,
            "maintenance replaced the caller identity"
        );
        assert!(result.is_err(), "maintenance admitted a thread identity");
    }

    #[test]
    fn account_job_probe_refuses_existing_thread_identity_without_clearing_it() {
        let outer = outer_identity();
        let before = effective_token_id().unwrap();
        let result = verify_child_job_isolation(
            unsafe { windows::Win32::System::Threading::GetCurrentProcess() },
            "Alysis.Execution.0123456789abcdef0123456789abcdef",
        );
        let after = effective_token_id();
        drop(outer);
        assert_eq!(
            after.unwrap(),
            before,
            "job probe replaced the caller identity"
        );
        assert!(result.is_err(), "job probe admitted a thread identity");
    }

    #[test]
    fn ordinary_process_cannot_pass_account_container_verification() {
        let current = unsafe { windows::Win32::System::Threading::GetCurrentProcess() };
        let sid = crate::sid::current_user_sid().unwrap();
        let error = verify_account_process(
            current,
            "Alysis.Execution.0123456789abcdef0123456789abcdef",
            &sid,
            NetworkAccess::Off,
        )
        .unwrap_err();
        assert!(error.to_string().contains("not an AppContainer"));
    }

    #[test]
    fn host_identity_cannot_be_selected_as_the_dedicated_account() {
        let sid = crate::sid::current_user_sid().unwrap();
        assert!(
            create_account_lowbox(
                "Alysis.Execution.0123456789abcdef0123456789abcdef",
                &sid,
                &sid,
                NetworkAccess::Off
            )
            .is_err()
        );
    }
}
