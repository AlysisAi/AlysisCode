//! Read-only startup probes against the selected token and original suspended
//! fixture child. No descriptor repair, new grants, logon or profile effects.
use super::*;
use windows::Wdk::{Foundation::OBJECT_ATTRIBUTES, Storage::FileSystem::NtOpenDirectoryObject};
use windows::Win32::Security::Authorization::{GetSecurityInfo, SE_KERNEL_OBJECT, SetSecurityInfo};

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Probe {
    token_object_dacl_granted: u32,
    namespace_reads: Vec<ReadProbe>,
    child_process_dacl_granted: Option<u32>,
    child_token_dacl_granted: Option<u32>,
    child_process_query: Option<ReadProbe>,
    child_token_query: Option<ReadProbe>,
    child_token_full_access: Option<ReadProbe>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct ReadProbe {
    object: String,
    succeeded: bool,
    native_hresult: Option<i32>,
}

fn observation(name: &str, result: Result<()>) -> ReadProbe {
    ReadProbe {
        object: name.into(),
        succeeded: result.is_ok(),
        native_hresult: result.err().and_then(|e| {
            e.chain()
                .find_map(|e| e.downcast_ref::<windows::core::Error>())
                .map(|e| e.code().0)
        }),
    }
}

fn granted(object: HANDLE, token: HANDLE, token_object: bool) -> Result<u32> {
    let mut raw = PSECURITY_DESCRIPTOR::default();
    unsafe {
        GetSecurityInfo(
            object,
            SE_KERNEL_OBJECT,
            OWNER_SECURITY_INFORMATION | GROUP_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION,
            None,
            None,
            None,
            None,
            Some(&mut raw),
        )
    }
    .ok()?;
    let descriptor = OwnedSd::from_raw(raw);
    let mapping = if token_object {
        GENERIC_MAPPING {
            GenericRead: TOKEN_READ.0,
            GenericWrite: TOKEN_WRITE.0,
            GenericExecute: TOKEN_EXECUTE.0,
            GenericAll: TOKEN_ALL_ACCESS.0,
        }
    } else {
        GENERIC_MAPPING {
            GenericRead: 0x20410,
            GenericWrite: 0x20bea,
            GenericExecute: 0x120000,
            GenericAll: PROCESS_ALL_ACCESS.0,
        }
    };
    let mut raw = HANDLE::default();
    unsafe {
        DuplicateToken(token, SecurityImpersonation, &mut raw)?;
    }
    let token = OwnedHandle(raw);
    let mut privileges = [0usize; 128];
    let mut length = size_of_val(&privileges) as u32;
    let mut access = 0;
    let mut allowed = false.into();
    unsafe {
        AccessCheck(
            descriptor.ptr,
            token.raw(),
            0x02000000,
            &mapping,
            Some(privileges.as_mut_ptr().cast()),
            &mut length,
            &mut access,
            &mut allowed,
        )?;
    }
    Ok(if allowed.as_bool() { access } else { 0 })
}

fn read_directory(path: &str) -> Result<()> {
    let (mut buffer, length) = crate::util::counted_utf16(path);
    let name = UNICODE_STRING {
        Length: length,
        MaximumLength: length,
        Buffer: PWSTR(buffer.as_mut_ptr()),
    };
    let attributes = OBJECT_ATTRIBUTES {
        Length: size_of::<OBJECT_ATTRIBUTES>() as u32,
        ObjectName: &name,
        Attributes: OBJ_CASE_INSENSITIVE,
        ..Default::default()
    };
    let mut raw = HANDLE::default();
    // DIRECTORY_QUERY | DIRECTORY_TRAVERSE only; never create or alter a name.
    unsafe { NtOpenDirectoryObject(&mut raw, 3, &attributes) }.ok()?;
    drop(OwnedHandle(raw));
    Ok(())
}

impl Probe {
    pub(super) fn require_owned(&self) -> Result<()> {
        ensure!(
            self.token_object_dacl_granted == TOKEN_ALL_ACCESS.0
                && self.child_token_dacl_granted == Some(TOKEN_ALL_ACCESS.0)
                && self.child_process_dacl_granted == Some(PROCESS_ALL_ACCESS.0)
                && self
                    .child_token_full_access
                    .as_ref()
                    .is_some_and(|p| p.succeeded),
            "filtered fixture's own process/token access differs from explicit policy"
        );
        Ok(())
    }
    pub(super) fn observe(token: &OwnedHandle) -> Result<Self> {
        let token_object_dacl_granted = granted(token.raw(), token.raw(), true)?;
        let mut session = 0;
        unsafe {
            windows::Win32::System::RemoteDesktop::ProcessIdToSessionId(
                std::process::id(),
                &mut session,
            )?;
        }
        let namespace_reads = crate::account_impersonation::with_token(token, || {
            Ok([
                "\\BaseNamedObjects".into(),
                format!("\\Sessions\\{session}\\BaseNamedObjects"),
                "\\RPC Control".into(),
                format!("\\Sessions\\{session}\\Windows"),
            ]
            .iter()
            .map(|path| observation(path, read_directory(path)))
            .collect())
        })
        .unwrap_or_else(|error| vec![observation("selected_token_impersonation", Err(error))]);
        Ok(Self {
            token_object_dacl_granted,
            namespace_reads,
            child_process_dacl_granted: None,
            child_token_dacl_granted: None,
            child_process_query: None,
            child_token_query: None,
            child_token_full_access: None,
        })
    }

    pub(super) fn child(&mut self, child: &SpawnedChild, token: &OwnedHandle) -> Result<()> {
        self.child_process_dacl_granted = Some(granted(child.process(), token.raw(), false)?);
        let mut raw = HANDLE::default();
        unsafe {
            OpenProcessToken(
                child.process(),
                TOKEN_QUERY | TOKEN_ACCESS_MASK(READ_CONTROL.0),
                &mut raw,
            )?;
        }
        let primary = OwnedHandle(raw);
        self.child_token_dacl_granted = Some(granted(primary.raw(), token.raw(), true)?);
        let observed = crate::account_impersonation::with_token(token, || {
            let process = observation(
                "original_child_process_query",
                (|| -> Result<()> {
                    let pid = unsafe { GetProcessId(child.process()) };
                    ensure!(pid != 0, "original child process unavailable");
                    let opened = OwnedHandle(unsafe {
                        OpenProcess(
                            PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SYNCHRONIZE,
                            false,
                            pid,
                        )?
                    });
                    ensure!(
                        unsafe { CompareObjectHandles(opened.raw(), child.process()) }.as_bool(),
                        "startup probe process changed"
                    );
                    Ok(())
                })(),
            );
            let primary = observation(
                "original_child_token_query",
                (|| -> Result<()> {
                    let mut raw = HANDLE::default();
                    unsafe {
                        OpenProcessToken(child.process(), TOKEN_QUERY, &mut raw)?;
                    }
                    drop(OwnedHandle(raw));
                    Ok(())
                })(),
            );
            let full = observation(
                "original_child_token_full_access",
                (|| -> Result<()> {
                    let mut raw = HANDLE::default();
                    unsafe {
                        OpenProcessToken(child.process(), TOKEN_ALL_ACCESS, &mut raw)?;
                    }
                    drop(OwnedHandle(raw));
                    Ok(())
                })(),
            );
            Ok((process, primary, full))
        });
        match observed {
            Ok((process, primary, full)) => {
                self.child_process_query = Some(process);
                self.child_token_query = Some(primary);
                self.child_token_full_access = Some(full);
            }
            Err(error) => {
                // Distinguish inability to establish the observation context
                // from a completed native access check that returned denial.
                self.child_process_query =
                    Some(observation("selected_token_impersonation", Err(error)));
            }
        }
        Ok(())
    }
}

/// This is the trusted broker fixture's newly-created token, never the
/// controller token or an untrusted command token. An elevated parent's token
/// DACL may only grant the user query rights via its logon SID. The filtered
/// fixture needs to own its own token and objects after administrators become
/// deny-only. Specify user/SYSTEM, not Administrators or broad logon groups.
pub(super) fn prepare_owned_token(token: &OwnedHandle) -> Result<()> {
    let controller = OwnedHandle(crate::token::open_self_token()?);
    ensure!(
        !unsafe { CompareObjectHandles(controller.raw(), token.raw()) }.as_bool(),
        "refuse controller token policy mutation"
    );
    let owner = crate::sid::current_user_sid()?;
    let object = OwnedSd::from_sddl(&format!(
        "O:{owner}D:P(A;;0x{:x};;;SY)(A;;0x{:x};;;{owner})",
        TOKEN_ALL_ACCESS.0, TOKEN_ALL_ACCESS.0
    ))?;
    let defaults = OwnedSd::from_sddl(&format!("O:{owner}D:P(A;;GA;;;SY)(A;;GA;;;{owner})"))?;
    let dacl = |descriptor: &OwnedSd| -> Result<*mut ACL> {
        let mut present = false.into();
        let mut defaulted = false.into();
        let mut acl = std::ptr::null_mut();
        unsafe {
            GetSecurityDescriptorDacl(descriptor.ptr, &mut present, &mut acl, &mut defaulted)?;
        }
        ensure!(
            present.as_bool() && !acl.is_null(),
            "fixture token policy missing"
        );
        Ok(acl)
    };
    let default_acl = TOKEN_DEFAULT_DACL {
        DefaultDacl: dacl(&defaults)?,
    };
    unsafe {
        SetTokenInformation(
            token.raw(),
            TokenDefaultDacl,
            (&default_acl as *const TOKEN_DEFAULT_DACL).cast(),
            size_of_val(&default_acl) as u32,
        )?;
        SetSecurityInfo(
            token.raw(),
            SE_KERNEL_OBJECT,
            DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION,
            None,
            None,
            Some(dacl(&object)?),
            None,
        )
        .ok()?;
    }
    let mut actual = PSECURITY_DESCRIPTOR::default();
    unsafe {
        GetSecurityInfo(
            token.raw(),
            SE_KERNEL_OBJECT,
            OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION,
            None,
            None,
            None,
            None,
            Some(&mut actual),
        )
    }
    .ok()?;
    let actual = OwnedSd::from_raw(actual);
    let mut control = 0;
    let mut revision = 0;
    let mut actual_owner = PSID::default();
    let mut defaulted = false.into();
    unsafe {
        GetSecurityDescriptorControl(actual.ptr, &mut control, &mut revision)?;
        GetSecurityDescriptorOwner(actual.ptr, &mut actual_owner, &mut defaulted)?;
    }
    let owner_sid = crate::sid::LocalPsid::from_string(&owner)?;
    ensure!(
        control & 0x1000 != 0
            && unsafe { EqualSid(actual_owner, owner_sid.as_psid()) }.is_ok()
            && acl_bytes(dacl(&actual)?)? == acl_bytes(dacl(&object)?)?,
        "fixture token protected policy readback differs"
    );
    ensure!(
        granted(token.raw(), token.raw(), true)? == TOKEN_ALL_ACCESS.0,
        "filtered fixture token cannot access itself"
    );
    Ok(())
}

fn acl_bytes(acl: *mut ACL) -> Result<Vec<u8>> {
    ensure!(
        !acl.is_null() && unsafe { IsValidAcl(acl) }.as_bool(),
        "invalid native fixture ACL"
    );
    let size = unsafe { (*acl).AclSize } as usize;
    ensure!(
        (size_of::<ACL>()..=65535).contains(&size),
        "invalid fixture ACL size"
    );
    Ok(unsafe { std::slice::from_raw_parts(acl.cast::<u8>(), size) }.to_vec())
}

#[test]
fn bootstrap_filtered_token_policy_refuses_controller_token() {
    let controller = OwnedHandle(crate::token::open_self_token().unwrap());
    assert!(
        prepare_owned_token(&controller)
            .unwrap_err()
            .to_string()
            .contains("refuse controller token")
    );
}
