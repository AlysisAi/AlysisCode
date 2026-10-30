//! Account setup is limited to machines with a local SAM. On a domain
//! controller NetUserAdd/Del(NULL) can target directory accounts instead.
//! Query the role directly; do not infer it from failed name lookup or rely on
//! the Server service's availability. Unknown roles and query errors fail closed.

use anyhow::{Context, Result, ensure};
use windows::Win32::Networking::ActiveDirectory::{
    DSROLE_MACHINE_ROLE, DSROLE_PRIMARY_DOMAIN_INFO_BASIC, DsRole_RoleMemberServer,
    DsRole_RoleMemberWorkstation, DsRole_RoleStandaloneServer, DsRole_RoleStandaloneWorkstation,
    DsRoleFreeMemory, DsRoleGetPrimaryDomainInformation, DsRolePrimaryDomainInfoBasic,
};
use windows::core::PCWSTR;

fn validate_role(status: u32, role: Option<DSROLE_MACHINE_ROLE>) -> Result<()> {
    ensure!(
        status == 0,
        "cannot establish local account host role: {status}"
    );
    let role = role.context("local account host role is missing")?;
    ensure!(
        [
            DsRole_RoleStandaloneWorkstation,
            DsRole_RoleMemberWorkstation,
            DsRole_RoleStandaloneServer,
            DsRole_RoleMemberServer
        ]
        .contains(&role),
        "sandbox account maintenance requires a standalone/member workstation or server; domain controllers and unknown roles are unsupported"
    );
    Ok(())
}

fn query_role() -> Result<DSROLE_MACHINE_ROLE> {
    let mut buffer = std::ptr::null_mut();
    let status = unsafe {
        DsRoleGetPrimaryDomainInformation(PCWSTR::null(), DsRolePrimaryDomainInfoBasic, &mut buffer)
    };
    let role = if status == 0 && !buffer.is_null() {
        Some(unsafe { (*buffer.cast::<DSROLE_PRIMARY_DOMAIN_INFO_BASIC>()).MachineRole })
    } else {
        None
    };
    if !buffer.is_null() {
        unsafe {
            DsRoleFreeMemory(buffer.cast());
        }
    }
    validate_role(status, role)?;
    role.context("local account host role is missing")
}

/// Read-only and usable before elevation. Maintenance repeats this check under
/// its execution lease, before registry/SAM/policy changes. Execution and crash
/// recovery do not depend on this administrative setup guard.
pub fn require_local_sam() -> Result<()> {
    query_role().map(|_| ())
}

#[cfg(test)]
mod tests {
    use super::*;
    use windows::Win32::Networking::ActiveDirectory::{
        DsRole_RoleBackupDomainController, DsRole_RolePrimaryDomainController,
    };

    #[test]
    fn local_account_host_allows_standalone_and_member_workstations_and_servers() {
        for role in [
            DsRole_RoleStandaloneWorkstation,
            DsRole_RoleMemberWorkstation,
            DsRole_RoleStandaloneServer,
            DsRole_RoleMemberServer,
        ] {
            validate_role(0, Some(role)).unwrap();
        }
    }

    #[test]
    fn local_account_host_refuses_controllers_unknown_roles_and_query_errors() {
        for role in [
            DsRole_RoleBackupDomainController,
            DsRole_RolePrimaryDomainController,
            DSROLE_MACHINE_ROLE(-1),
            DSROLE_MACHINE_ROLE(6),
            DSROLE_MACHINE_ROLE(i32::MAX),
        ] {
            assert!(validate_role(0, Some(role)).is_err());
        }
        assert!(validate_role(0, None).is_err());
        for status in [5, 50, 1722, 2114] {
            assert!(validate_role(status, Some(DsRole_RoleStandaloneServer)).is_err());
            assert!(validate_role(status, None).is_err());
        }
    }

    #[test]
    #[ignore = "read-only live role qualification; run explicitly on a non-elevated supported host"]
    fn local_account_host_current_role_read_only() {
        use windows::Win32::{
            Foundation::HANDLE,
            Security::{GetTokenInformation, TOKEN_ELEVATION, TOKEN_QUERY, TokenElevation},
            System::Threading::{GetCurrentProcess, OpenProcessToken},
        };
        let mut handle = HANDLE::default();
        unsafe {
            OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut handle).unwrap();
        }
        let token = crate::util::OwnedHandle(handle);
        let mut elevation = TOKEN_ELEVATION::default();
        let mut size = 0;
        unsafe {
            GetTokenInformation(
                token.raw(),
                TokenElevation,
                Some((&mut elevation as *mut TOKEN_ELEVATION).cast()),
                std::mem::size_of::<TOKEN_ELEVATION>() as u32,
                &mut size,
            )
            .unwrap();
        }
        assert_eq!(
            elevation.TokenIsElevated, 0,
            "qualification must use normal user token"
        );
        let role = query_role().unwrap();
        println!(
            "local account role query passed: role={}, elevated=false",
            role.0
        );
    }
}
