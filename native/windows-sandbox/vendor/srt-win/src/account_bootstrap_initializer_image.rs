//! Fixed OS image pin for a future never-resumed ordinary-AppContainer birth.
//! No caller-supplied application, PATH search, process creation or resume API.
//! Serviced system files may have WinSxS hardlinks: this guard pins the original
//! file with real data access, denies data writes across aliases and deletion
//! of the selected name. Other hardlink names can be removed; link-count drift
//! is refused. Exact descriptors and retained ancestors remain mandatory.
//! General candidate state/image pins continue to refuse all hardlinked files.
use crate::{
    account_state::fs::{self, Pinned},
    path_id::file_id_from_handle,
    util::{OwnedHandle, OwnedSd, pcwstr, wstr},
};
use anyhow::{Context, Result, ensure};
use std::path::{Path, PathBuf};
use windows::Win32::{
    Foundation::HANDLE,
    Globalization::{CSTR_EQUAL, CompareStringOrdinal},
    Security::{
        Authorization::{GetSecurityInfo, SE_FILE_OBJECT},
        DACL_SECURITY_INFORMATION, GetSecurityDescriptorControl, GetSecurityDescriptorOwner,
        OWNER_SECURITY_INFORMATION, PSECURITY_DESCRIPTOR, PSID,
    },
    Storage::FileSystem::*,
    System::{SystemInformation::GetSystemDirectoryW, Threading::*},
};

#[derive(PartialEq, Eq)]
struct Shape {
    owner: String,
    control: u16,
    aces: Vec<Vec<u8>>,
}
fn shape(sd: &OwnedSd) -> Result<Shape> {
    let mut owner = PSID::default();
    let mut defaulted = false.into();
    let mut control = 0;
    let mut revision = 0;
    unsafe {
        GetSecurityDescriptorOwner(sd.ptr, &mut owner, &mut defaulted)?;
        GetSecurityDescriptorControl(sd.ptr, &mut control, &mut revision)?;
    }
    ensure!(!owner.0.is_null(), "system image owner absent");
    Ok(Shape {
        owner: crate::sid::psid_to_string(owner)?,
        control: control & 0x150c,
        aces: fs::ace_bytes(sd)?,
    })
}
fn security(handle: HANDLE) -> Result<OwnedSd> {
    let mut raw = PSECURITY_DESCRIPTOR::default();
    unsafe {
        GetSecurityInfo(
            handle,
            SE_FILE_OBJECT,
            OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION,
            None,
            None,
            None,
            None,
            Some(&mut raw),
        )
    }
    .ok()?;
    Ok(OwnedSd::from_raw(raw))
}
fn system_directory() -> Result<PathBuf> {
    ensure!(
        cfg!(all(windows, target_arch = "x86_64")),
        "initializer requires native x64 Windows"
    );
    let mut wide = [0u16; 32768];
    let n = unsafe { GetSystemDirectoryW(Some(&mut wide)) } as usize;
    ensure!(
        n > 0 && n < wide.len(),
        "system directory query exceeds bounds"
    );
    let path = PathBuf::from(String::from_utf16(&wide[..n])?);
    fs::extended_path(&path)?;
    Ok(path)
}
fn final_path(handle: HANDLE, path: &Path) -> Result<()> {
    let mut wide = [0u16; 32768];
    let n = unsafe {
        GetFinalPathNameByHandleW(
            handle,
            &mut wide,
            GETFINALPATHNAMEBYHANDLE_FLAGS(FILE_NAME_NORMALIZED.0 | VOLUME_NAME_DOS.0),
        )
    } as usize;
    ensure!(
        n > 0 && n < wide.len(),
        "initializer final path query exceeds bounds"
    );
    let actual = String::from_utf16(&wide[..n])?;
    let actual: Vec<u16> = crate::path_id::strip_extended_prefix(&actual)
        .trim_end_matches('\\')
        .encode_utf16()
        .collect();
    let expected: Vec<u16> = path
        .to_str()
        .context("system path is not Unicode")?
        .trim_end_matches('\\')
        .encode_utf16()
        .collect();
    ensure!(
        unsafe { CompareStringOrdinal(&actual, &expected, true) } == CSTR_EQUAL,
        "initializer image path aliases or changed"
    );
    Ok(())
}
fn metadata(handle: HANDLE, directory: bool) -> Result<u32> {
    crate::account_namespace::verify_noninheritable(handle)?;
    let mut info = BY_HANDLE_FILE_INFORMATION::default();
    unsafe { GetFileInformationByHandle(handle, &mut info) }?;
    ensure!(
        info.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT.0 == 0
            && (info.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY.0 != 0) == directory
            && info.nNumberOfLinks > 0,
        "initializer object type/reparse/link count invalid"
    );
    Ok(info.nNumberOfLinks)
}
fn pin_leaf(path: &Path) -> Result<OwnedHandle> {
    let wide = wstr(&fs::extended_path(path)?);
    Ok(OwnedHandle(
        unsafe {
            CreateFileW(
                pcwstr(&wide),
                FILE_READ_DATA.0 | FILE_READ_ATTRIBUTES.0 | 0x20000,
                FILE_SHARE_READ,
                None,
                OPEN_EXISTING,
                FILE_FLAG_OPEN_REPARSE_POINT,
                None,
            )
        }
        .context("pin fixed system initializer image without write/delete sharing")?,
    ))
}
fn verify_leaf(
    handle: HANDLE,
    path: &Path,
    id: [u8; 24],
    links: u32,
    owner: &str,
    expected: &Shape,
) -> Result<()> {
    ensure!(
        metadata(handle, false)? == links && file_id_from_handle(handle)?.as_bytes() == id,
        "initializer original image identity/link count changed"
    );
    final_path(handle, path)?;
    let descriptor = security(handle)?;
    fs::validate_ancestor(&descriptor, owner, true)?;
    ensure!(
        shape(&descriptor)? == *expected,
        "initializer image security changed"
    );
    Ok(())
}

pub(crate) struct SystemInitializerImage {
    owner: String,
    parents: Vec<Pinned>,
    parent_security: Vec<Shape>,
    image: OwnedHandle,
    path: PathBuf,
    id: [u8; 24],
    links: u32,
    image_security: Shape,
}
impl SystemInitializerImage {
    pub(crate) fn prepare(owner: &str) -> Result<Self> {
        crate::account_impersonation::require_process_identity()?;
        ensure!(
            owner == crate::sid::current_user_sid()?,
            "initializer actor differs"
        );
        let directory = system_directory()?;
        let text = directory
            .to_str()
            .context("system directory is not Unicode")?;
        let anchor = &text[..3];
        let wide = wstr(anchor);
        let mut filesystem = [0u16; 32];
        ensure!(
            unsafe { GetDriveTypeW(pcwstr(&wide)) } == 3,
            "system image needs a fixed local volume"
        );
        unsafe {
            GetVolumeInformationW(pcwstr(&wide), None, None, None, None, Some(&mut filesystem))
        }?;
        ensure!(
            filesystem.starts_with(&[78, 84, 70, 83, 0]),
            "system image requires NTFS"
        );
        let mut parents = vec![Pinned::open(Path::new(anchor), true, false)?];
        fs::validate_ancestor(&parents[0].security()?, owner, false)?;
        for part in text[3..].split('\\') {
            let child = fs::open_child_directory(parents.last().unwrap(), part, None)?.0;
            fs::validate_ancestor(&child.security()?, owner, child.path == directory)?;
            parents.push(child);
        }
        let mut parent_security = Vec::new();
        for parent in &parents {
            parent_security.push(shape(&parent.security()?)?);
        }
        let path = directory.join("cmd.exe");
        let image = pin_leaf(&path)?;
        let links = metadata(image.raw(), false)?;
        final_path(image.raw(), &path)?;
        let id = file_id_from_handle(image.raw())?.as_bytes();
        let descriptor = security(image.raw())?;
        fs::validate_ancestor(&descriptor, owner, true)?;
        let value = Self {
            owner: owner.into(),
            parents,
            parent_security,
            image,
            path,
            id,
            links,
            image_security: shape(&descriptor)?,
        };
        value.verify()?;
        Ok(value)
    }
    pub(crate) fn verify(&self) -> Result<()> {
        crate::account_impersonation::require_process_identity()?;
        ensure!(
            self.owner == crate::sid::current_user_sid()?
                && self.path.parent() == Some(system_directory()?.as_path())
                && self.path.file_name() == Some(std::ffi::OsStr::new("cmd.exe"))
                && !self.parents.is_empty()
                && self.parents.len() == self.parent_security.len(),
            "initializer actor or fixed OS selection changed"
        );
        for (i, parent) in self.parents.iter().enumerate() {
            metadata(parent.handle.raw(), true)?;
            final_path(parent.handle.raw(), &parent.path)?;
            ensure!(
                file_id_from_handle(parent.handle.raw())? == parent.id,
                "initializer original ancestor identity changed"
            );
            let descriptor = parent.security()?;
            fs::validate_ancestor(&descriptor, &self.owner, i + 1 == self.parents.len())?;
            ensure!(
                shape(&descriptor)? == self.parent_security[i],
                "initializer ancestor security changed"
            );
        }
        verify_leaf(
            self.image.raw(),
            &self.path,
            self.id,
            self.links,
            &self.owner,
            &self.image_security,
        )
    }
    pub(crate) fn application(&self) -> Result<&Path> {
        self.verify()?;
        Ok(&self.path)
    }
    pub(crate) fn environment(&self) -> Result<Vec<u16>> {
        self.verify()?;
        let mut wide = [0u16; 32768];
        let n = unsafe {
            windows::Win32::System::SystemInformation::GetWindowsDirectoryW(Some(&mut wide))
        } as usize;
        ensure!(
            n > 0 && n < wide.len(),
            "initializer Windows directory query exceeds bounds"
        );
        let windows = String::from_utf16(&wide[..n])?;
        // Bind these non-secret OS values to an ancestor already held by this
        // image pin. Never use ambient SystemRoot, PATH or a caller overlay.
        let wanted: Vec<u16> = windows.encode_utf16().collect();
        ensure!(
            self.parents.iter().any(|p| {
                let actual: Vec<u16> = p
                    .path
                    .as_os_str()
                    .to_string_lossy()
                    .encode_utf16()
                    .collect();
                (unsafe { CompareStringOrdinal(&actual, &wanted, true) }) == CSTR_EQUAL
            }),
            "initializer Windows directory is not a pinned OS ancestor"
        );
        let system = self
            .path
            .parent()
            .context("initializer system directory absent")?
            .to_str()
            .context("initializer system directory is not Unicode")?
            .to_owned();
        let mut values = vec![
            ("SystemRoot".into(), windows.clone()),
            ("WINDIR".into(), windows),
            ("PATH".into(), system),
        ];
        #[cfg(test)]
        if OMIT_PROFILE_ENVIRONMENT.with(|s| s.get()) {
            return Ok(crate::launch::build_env_block_with_inheritance(
                &values, false,
            ));
        }
        // Windows constructs the AppContainer environment during native birth,
        // including LOCALAPPDATA/TEMP/TMP redirection. Resolve only known folder
        // paths through the OS; never inherit ambient secrets or proxy settings.
        use windows::Win32::UI::Shell::{
            FOLDERID_LocalAppData, FOLDERID_Profile, FOLDERID_RoamingAppData,
        };
        let local = known_profile_folder(&FOLDERID_LocalAppData)?;
        let profile = known_profile_folder(&FOLDERID_Profile)?;
        let roaming = known_profile_folder(&FOLDERID_RoamingAppData)?;
        values.extend([
            ("APPDATA".into(), roaming),
            ("LOCALAPPDATA".into(), local.clone()),
            ("USERPROFILE".into(), profile),
            ("TEMP".into(), format!("{local}\\Temp")),
            ("TMP".into(), format!("{local}\\Temp")),
        ]);
        let environment = crate::launch::build_env_block_with_inheritance(&values, false);
        ensure!(
            environment.len() <= 32767,
            "initializer environment exceeds bound"
        );
        self.verify()?;
        Ok(environment)
    }
    pub(crate) fn identity(&self) -> Result<[u8; 24]> {
        self.verify()?;
        Ok(self.id)
    }
    pub(crate) fn verify_process(&self, process: HANDLE) -> Result<()> {
        self.verify()?;
        let mut wide = [0u16; 32768];
        let mut n = wide.len() as u32;
        unsafe {
            QueryFullProcessImageNameW(
                process,
                PROCESS_NAME_WIN32,
                windows::core::PWSTR(wide.as_mut_ptr()),
                &mut n,
            )
        }?;
        ensure!(
            n > 0 && (n as usize) < wide.len(),
            "initializer process image query exceeds bounds"
        );
        let expected: Vec<u16> = self.path.to_str().unwrap().encode_utf16().collect();
        ensure!(
            unsafe { CompareStringOrdinal(&wide[..n as usize], &expected, true) } == CSTR_EQUAL,
            "initializer process differs from pinned system image"
        );
        self.verify()
    }
}

pub(crate) fn known_profile_folder(id: &windows::core::GUID) -> Result<String> {
    use windows::Win32::UI::Shell::{KF_FLAG_DEFAULT, SHGetKnownFolderPath};
    #[link(name = "ole32")]
    unsafe extern "system" {
        fn CoTaskMemFree(pointer: *const std::ffi::c_void);
    }
    let pointer = unsafe { SHGetKnownFolderPath(id, KF_FLAG_DEFAULT, None)? };
    let value = unsafe { pointer.to_string() };
    unsafe {
        CoTaskMemFree(pointer.0.cast());
    }
    let value = value?;
    crate::account_state::fs::extended_path(Path::new(&value))?;
    ensure!(
        !value.chars().any(char::is_control),
        "initializer known folder contains controls"
    );
    Ok(value)
}
#[cfg(test)]
thread_local! {pub(crate) static OMIT_PROFILE_ENVIRONMENT:std::cell::Cell<bool>=const{std::cell::Cell::new(false)};}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn initializer_system_image_is_fixed_and_retains_os_file_identity() {
        let owner = crate::sid::current_user_sid().unwrap();
        let image = SystemInitializerImage::prepare(&owner).unwrap();
        assert_eq!(
            image.application().unwrap(),
            system_directory().unwrap().join("cmd.exe")
        );
        assert_ne!(image.identity().unwrap(), [0; 24]);
        let environment = image.environment().unwrap();
        assert!(environment.ends_with(&[0, 0]));
        let entries = environment
            .split(|u| *u == 0)
            .filter(|v| !v.is_empty())
            .map(|v| String::from_utf16(v).unwrap())
            .collect::<Vec<_>>();
        assert_eq!(entries.len(), 8);
        assert_eq!(
            entries
                .iter()
                .map(|s| s.split_once('=').unwrap().0)
                .collect::<Vec<_>>(),
            [
                "APPDATA",
                "LOCALAPPDATA",
                "PATH",
                "SystemRoot",
                "TEMP",
                "TMP",
                "USERPROFILE",
                "WINDIR"
            ]
        );
        assert_eq!(
            entries[2],
            format!("PATH={}", system_directory().unwrap().display())
        );
        assert_eq!(
            entries[3].split_once('=').unwrap().1,
            entries[7].split_once('=').unwrap().1
        );

        // Only query existing resources. No system file mutation or child birth.
        assert!(
            image
                .verify_process(unsafe { GetCurrentProcess() })
                .is_err()
        );
        image.verify().unwrap();
    }
    #[test]
    fn initializer_system_image_refuses_foreign_actor_before_pinning() {
        assert!(SystemInitializerImage::prepare("S-1-5-18").is_err());
    }
    #[test]
    fn initializer_system_image_pin_denies_alias_writes_and_refuses_link_count_drift() {
        // Real sharing checks on fresh synthetic hardlinks only, never OS writes.
        let nonce = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = crate::util::test_temp_dir().join(format!(
            "alysis-initializer-image-{}-{nonce}",
            std::process::id()
        ));
        std::fs::create_dir(&root).unwrap();
        let path = root.join("image.exe");
        let alias = root.join("servicing-alias.exe");
        let owner = crate::sid::current_user_sid().unwrap();
        let sd = OwnedSd::from_sddl(&format!(
            "O:{owner}D:P(A;;FA;;;{owner})(A;;FA;;;SY)(A;;FA;;;BA)"
        ))
        .unwrap();
        let attributes = windows::Win32::Security::SECURITY_ATTRIBUTES {
            nLength: std::mem::size_of::<windows::Win32::Security::SECURITY_ATTRIBUTES>() as u32,
            lpSecurityDescriptor: sd.ptr.0,
            bInheritHandle: false.into(),
        };
        drop(OwnedHandle(
            unsafe {
                CreateFileW(
                    pcwstr(&wstr(&fs::extended_path(&path).unwrap())),
                    FILE_WRITE_DATA.0,
                    FILE_SHARE_READ,
                    Some(&attributes),
                    CREATE_NEW,
                    FILE_FLAG_OPEN_REPARSE_POINT,
                    None,
                )
            }
            .unwrap(),
        ));
        std::fs::hard_link(&path, &alias).unwrap();
        let image = pin_leaf(&path).unwrap();
        assert_eq!(metadata(image.raw(), false).unwrap(), 2);
        let id = file_id_from_handle(image.raw()).unwrap().as_bytes();
        let initial = shape(&security(image.raw()).unwrap()).unwrap();
        verify_leaf(image.raw(), &path, id, 2, &owner, &initial).unwrap();
        let open = |path: &Path, access: u32| unsafe {
            CreateFileW(
                pcwstr(&wstr(&fs::extended_path(path).unwrap())),
                access,
                FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                None,
                OPEN_EXISTING,
                FILE_FLAG_OPEN_REPARSE_POINT,
                None,
            )
        };
        for (name, access) in [
            (&path, FILE_WRITE_DATA.0),
            (&path, 0x10000),
            (&alias, FILE_WRITE_DATA.0),
        ] {
            let error = match open(name, access) {
                Err(error) => error,
                Ok(raw) => {
                    drop(OwnedHandle(raw));
                    panic!(
                        "alias writer/delete authority was admitted: name={}, access={access:#x}",
                        name.file_name().unwrap().to_string_lossy()
                    );
                }
            };
            assert_eq!(
                error.code(),
                windows::core::HRESULT::from_win32(
                    windows::Win32::Foundation::ERROR_SHARING_VIOLATION.0
                )
            );
        }
        // NTFS deletion sharing is per hardlink name: a different name can be
        // unlinked. That does not replace or write the pinned original object.
        drop(OwnedHandle(open(&alias, 0x10000).unwrap()));
        std::fs::remove_file(&alias).unwrap();
        assert_eq!(metadata(image.raw(), false).unwrap(), 1);
        assert_eq!(file_id_from_handle(image.raw()).unwrap().as_bytes(), id);
        assert!(
            format!(
                "{:#}",
                verify_leaf(image.raw(), &path, id, 2, &owner, &initial).unwrap_err()
            )
            .contains("identity/link count changed")
        );
        assert_eq!(std::fs::read(&path).unwrap(), b"");
        assert!(open(&path, FILE_WRITE_DATA.0).is_err());
        drop(image);
        // Establish that sharing, rather than the fixture DACL, caused refusal.
        drop(OwnedHandle(
            open(&path, FILE_WRITE_DATA.0 | 0x10000).unwrap(),
        ));
        std::fs::remove_file(&path).unwrap();
        std::fs::remove_dir(&root).unwrap();
    }
}
