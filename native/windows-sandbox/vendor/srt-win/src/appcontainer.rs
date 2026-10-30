//! Per-execution AppContainers with an explicit network capability policy.
use anyhow::{Context, Result, ensure};
use serde::{Deserialize, Serialize};
use windows::Win32::Security::Isolation::{
    CreateAppContainerProfile, DeleteAppContainerProfile,
    DeriveAppContainerSidFromAppContainerName, GetAppContainerFolderPath,
};
use windows::Win32::Security::{FreeSid, PSID, SID_AND_ATTRIBUTES};
use windows::core::PCWSTR;

/// Deliberately independent of approval-mode names. `On` grants the Windows
/// outbound internet capability, not a destination allowlist or private-network
/// server capability. `Off` remains the default and grants no capabilities.
#[derive(Clone, Copy, Debug, Default, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum NetworkAccess {
    #[default]
    Off,
    On,
}

impl NetworkAccess {
    pub fn capability_sids(self) -> &'static [&'static str] {
        match self {
            Self::Off => &[],
            // WinCapabilityInternetClientSid; never add the server, private
            // network, enterprise authentication or credential capabilities.
            Self::On => &["S-1-15-3-1"],
        }
    }
}

pub struct ContainerSid(PSID);
impl ContainerSid {
    pub fn raw(&self) -> PSID {
        self.0
    }
    pub fn string(&self) -> Result<String> {
        crate::sid::psid_to_string(self.0)
    }
}
impl Drop for ContainerSid {
    fn drop(&mut self) {
        unsafe {
            FreeSid(self.0);
        }
    }
}

fn validate_name(name: &str) -> Result<Vec<u16>> {
    let suffix = name
        .strip_prefix("Alysis.Execution.")
        .context("invalid AppContainer namespace")?;
    ensure!(
        suffix.len() == 32 && suffix.bytes().all(|b| b.is_ascii_hexdigit()),
        "invalid AppContainer name"
    );
    Ok(crate::util::wstr(name))
}

pub fn derive(name: &str) -> Result<ContainerSid> {
    let wide = validate_name(name)?;
    Ok(ContainerSid(unsafe {
        DeriveAppContainerSidFromAppContainerName(PCWSTR(wide.as_ptr()))?
    }))
}

pub fn create(name: &str, capabilities: &[SID_AND_ATTRIBUTES]) -> Result<()> {
    let wide = validate_name(name)?;
    // Reuse is forbidden: even a collision must never inherit another command's state.
    let _sid = ContainerSid(unsafe {
        CreateAppContainerProfile(
            PCWSTR(wide.as_ptr()),
            PCWSTR(wide.as_ptr()),
            PCWSTR(wide.as_ptr()),
            if capabilities.is_empty() {
                None
            } else {
                Some(capabilities)
            },
        )
        .context("create fresh AppContainer profile")?
    });
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn network_policy_never_accepts_implicit_or_unknown_access() {
        assert_eq!(NetworkAccess::default(), NetworkAccess::Off);
        assert!(NetworkAccess::Off.capability_sids().is_empty());
        for value in ["true", "null", "\"full-access\"", "\"private\"", "\"ON\""] {
            assert!(serde_json::from_str::<NetworkAccess>(value).is_err());
        }
    }

    #[test]
    fn internet_policy_contains_only_windows_outbound_capability() {
        use windows::Win32::Security::{CreateWellKnownSid, WinCapabilityInternetClientSid};
        let mut bytes = [0u8; 68];
        let mut size = bytes.len() as u32;
        unsafe {
            CreateWellKnownSid(
                WinCapabilityInternetClientSid,
                None,
                Some(PSID(bytes.as_mut_ptr().cast())),
                &mut size,
            )
            .unwrap();
        }
        let sid = crate::sid::psid_to_string(PSID(bytes.as_mut_ptr().cast())).unwrap();
        assert_eq!(NetworkAccess::On.capability_sids(), &[sid]);
    }
}

pub fn delete(name: &str) -> Result<()> {
    let wide = validate_name(name)?;
    unsafe {
        DeleteAppContainerProfile(PCWSTR(wide.as_ptr()))
            .context("delete execution AppContainer profile")
    }
}

pub fn environment(name: &str) -> Result<Vec<(String, String)>> {
    let sid = derive(name)?;
    let wide = crate::util::wstr(&sid.string()?);
    let folder = unsafe { GetAppContainerFolderPath(PCWSTR(wide.as_ptr()))? };
    let path = crate::util::from_pwstr(folder);
    // GetAppContainerFolderPath allocates with CoTaskMemAlloc.
    #[link(name = "ole32")]
    unsafe extern "system" {
        fn CoTaskMemFree(memory: *const std::ffi::c_void);
    }
    unsafe {
        CoTaskMemFree(folder.0.cast());
    }
    let profile = std::path::PathBuf::from(path);
    let local = profile.join("Local");
    let roaming = profile.join("Roaming");
    let temp = profile.join("Temp");
    for path in [&local, &roaming, &temp] {
        std::fs::create_dir_all(path)?;
    }
    Ok([
        ("USERPROFILE", &profile),
        ("HOME", &profile),
        ("LOCALAPPDATA", &local),
        ("APPDATA", &roaming),
        ("TEMP", &temp),
        ("TMP", &temp),
    ]
    .into_iter()
    .map(|(key, value)| (key.to_owned(), value.to_string_lossy().into_owned()))
    .collect())
}
