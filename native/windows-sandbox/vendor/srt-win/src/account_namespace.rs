//! Namespace identity helpers shared by the disabled account candidate.
//! The obsolete prototype creator was removed: its Global child open followed
//! an OS symbolic link into shared BaseNamedObjects. All namespace effects must
//! use the retained, journaled single-job lifecycle in account_state.
use anyhow::{Result, ensure};
use windows::Win32::Foundation::*;

pub(crate) fn namespace_path(package_sid: &str) -> Result<String> {
    let _sid = crate::sid::LocalPsid::from_string(package_sid)?;
    ensure!(
        package_sid.starts_with("S-1-15-2-"),
        "invalid AppContainer package SID"
    );
    let mut session = 0;
    unsafe {
        windows::Win32::System::RemoteDesktop::ProcessIdToSessionId(
            std::process::id(),
            &mut session,
        )?;
    }
    Ok(format!(
        "\\Sessions\\{session}\\AppContainerNamedObjects\\{package_sid}"
    ))
}

pub(crate) fn verify_noninheritable(handle: HANDLE) -> Result<()> {
    let mut flags = 0;
    unsafe {
        GetHandleInformation(handle, &mut flags)?;
    }
    ensure!(
        flags & HANDLE_FLAG_INHERIT.0 == 0,
        "private namespace/token handle is inheritable"
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn namespace_paths_reject_non_package_identities() {
        for sid in ["S-1-1-0", "S-1-5-18", "not-a-sid"] {
            assert!(namespace_path(sid).is_err());
        }
    }
}
