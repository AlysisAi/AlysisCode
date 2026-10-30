//! Fixed test-only consumer writes. SDK selection and original package identity
//! precede every effect. No path, handle, profile creation or deletion API escapes.
use super::*;
use windows::Wdk::Storage::FileSystem::{FILE_CREATE, FILE_NON_DIRECTORY_FILE};

const PAYLOAD_LIMIT: usize = 128;

/// Data-only observation, never a root pin or cleanup/resume capability.
#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct FixedProfileFileReceipt {
    pub(crate) depth: u32,
    pub(crate) file_id: [u8; 24],
    pub(crate) bytes: u32,
}

fn validate_binding(container: &str, expected: [u8; 24], nonce: &str, depth: u32) -> Result<()> {
    ensure!(depth <= 2, "fixed profile writer depth outside bound");
    crate::account_install::validate_random_id(nonce)?;
    crate::job::execution_name(container)?;
    ensure!(
        expected != [0; 24],
        "fixed profile writer package identity missing"
    );
    Ok(())
}

pub(super) fn filename(nonce: &str, depth: u32) -> Vec<u16> {
    format!("AlysisFixedConsumer-{nonce}-{depth}.bin")
        .encode_utf16()
        .collect()
}
pub(super) fn payload(nonce: &str, depth: u32) -> Vec<u8> {
    format!("Alysis fixed paired consumer data\nuse={nonce}\ndepth={depth}\n").into_bytes()
}

/// Caller authenticates the fixed consumer purpose before invoking this API.
/// The expected ID is the owner's package root, not the SDK's AC child.
pub(crate) fn write_fixed_profile_file(
    container: &str,
    account_sid: &str,
    expected_package_root: [u8; 24],
    use_nonce: &str,
    depth: u32,
) -> Result<FixedProfileFileReceipt> {
    validate_binding(container, expected_package_root, use_nonce, depth)?;
    // package_path verifies the raw current actor and exact SDK/known-folder
    // relationship; names from the request are never treated as filesystem paths.
    let path = package_path(container, account_sid)?;
    let root = Root::open(&path)?;
    root.write(expected_package_root, use_nonce, depth)
}

struct Root {
    package: OriginalFile,
    initial: Metadata,
    ancestors: Vec<Anchor>,
}
impl Root {
    fn open(path: &str) -> Result<Self> {
        let components = local_components(path)?;
        let drive = crate::util::wstr(&path[..3]);
        ensure!(
            unsafe { GetDriveTypeW(PCWSTR(drive.as_ptr())) } == 3,
            "fixed profile writer requires fixed local NTFS"
        );
        let mut filesystem = [0u16; 32];
        unsafe {
            GetVolumeInformationW(
                PCWSTR(drive.as_ptr()),
                None,
                None,
                None,
                None,
                Some(&mut filesystem),
            )?;
        }
        ensure!(
            filesystem.starts_with(&[78, 84, 70, 83, 0]),
            "fixed profile writer requires NTFS"
        );
        let raw = unsafe {
            CreateFileW(
                PCWSTR(drive.as_ptr()),
                1 | 8 | 0x80 | READ_CONTROL,
                FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                None,
                OPEN_EXISTING,
                FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
                None,
            )?
        };
        let mut file = OriginalFile::adopt(raw)?;
        let mut ancestors = Vec::new();
        for component in components {
            let initial = file.metadata()?;
            ensure!(
                initial.directory,
                "fixed profile ancestor is not a directory"
            );
            let child = file.child(&component)?;
            ancestors.push(Anchor { file, initial });
            file = child;
        }
        let initial = file.metadata()?;
        ensure!(
            initial.directory && !ancestors.is_empty(),
            "fixed profile package root differs"
        );
        let root = Self {
            package: file,
            initial,
            ancestors,
        };
        root.unchanged()?;
        Ok(root)
    }
    fn unchanged(&self) -> Result<()> {
        crate::account_impersonation::require_process_identity()?;
        for anchor in &self.ancestors {
            ensure!(
                anchor.file.metadata()? == anchor.initial,
                "fixed profile ancestor changed"
            );
        }
        ensure!(
            self.package.metadata()? == self.initial,
            "fixed profile package root changed"
        );
        Ok(())
    }
    fn close(self) -> Result<()> {
        self.package.close()?;
        for anchor in self.ancestors.into_iter().rev() {
            anchor.file.close()?;
        }
        Ok(())
    }
    fn write(self, expected: [u8; 24], nonce: &str, depth: u32) -> Result<FixedProfileFileReceipt> {
        // Repeat the effect-boundary checks even for internal test entry points.
        ensure!(depth <= 2, "fixed profile writer depth outside bound");
        crate::account_install::validate_random_id(nonce)?;
        self.unchanged()?;
        ensure!(
            expected != [0; 24] && self.initial.id == expected,
            "fixed profile writer original package identity differs"
        );
        let ac = self
            .package
            .child(&"AC".encode_utf16().collect::<Vec<_>>())?;
        let ac_initial = ac.metadata()?;
        ensure!(ac_initial.directory, "fixed profile AC is not a directory");
        let name = filename(nonce, depth);
        component(&name)?;
        let data = payload(nonce, depth);
        ensure!(
            data.len() <= PAYLOAD_LIMIT,
            "fixed profile payload exceeds bound"
        );
        self.unchanged()?;
        let file = create_file(&ac, &name)?;
        let before = file.snapshot()?;
        ensure!(
            !before.metadata.directory && before.data.is_empty(),
            "fixed profile file was not exclusively empty"
        );
        let mut written = 0;
        unsafe {
            WriteFile(file.raw()?, Some(&data), Some(&mut written), None)?;
        }
        ensure!(
            written as usize == data.len(),
            "fixed profile write was partial"
        );
        unsafe {
            FlushFileBuffers(file.raw()?)?;
        }
        let after = file.snapshot()?;
        ensure!(
            after.metadata == before.metadata && after.data == data && after.children.is_empty(),
            "fixed profile file identity or readback differs"
        );
        self.unchanged()?;
        ensure!(ac.metadata()? == ac_initial, "fixed profile AC changed");
        let receipt = FixedProfileFileReceipt {
            depth,
            file_id: after.metadata.id,
            bytes: data.len() as u32,
        };
        // Every original handle closes once before a successful receipt escapes.
        file.close()?;
        ac.close()?;
        self.close()?;
        Ok(receipt)
    }
}

pub(super) fn create_file(parent: &OriginalFile, name: &[u16]) -> Result<OriginalFile> {
    component(name)?;
    let mut wide = name.to_vec();
    let object_name = UNICODE_STRING {
        Length: (wide.len() * 2) as u16,
        MaximumLength: (wide.len() * 2) as u16,
        Buffer: PWSTR(wide.as_mut_ptr()),
    };
    let attributes = OBJECT_ATTRIBUTES {
        Length: size_of::<OBJECT_ATTRIBUTES>() as u32,
        RootDirectory: parent.raw()?,
        ObjectName: &object_name,
        Attributes: OBJ_CASE_INSENSITIVE | OBJ_DONT_REPARSE,
        ..Default::default()
    };
    let mut raw = HANDLE::default();
    let mut io = IO_STATUS_BLOCK::default();
    let status = unsafe {
        NtCreateFile(
            &mut raw,
            FILE_ACCESS_RIGHTS(FILE_GENERIC_READ.0 | FILE_GENERIC_WRITE.0),
            &attributes,
            &mut io,
            None,
            FILE_ATTRIBUTE_NORMAL,
            FILE_SHARE_READ,
            FILE_CREATE,
            FILE_NON_DIRECTORY_FILE | FILE_OPEN_REPARSE_POINT | FILE_SYNCHRONOUS_IO_NONALERT,
            None,
            0,
        )
    };
    // Own any returned handle before checking status or creation information.
    let owned = if raw.is_invalid() {
        None
    } else {
        Some(OriginalFile(Some(raw), parent.1))
    };
    status
        .ok()
        .context("exclusive fixed profile file creation refused")?;
    let file = owned.context("exclusive fixed profile file has no handle")?;
    crate::account_namespace::verify_noninheritable(file.raw()?)?;
    ensure!(
        io.Information == 2,
        "fixed profile file was not newly created"
    );
    let observed = file.metadata()?;
    let mut expected = parent.metadata()?.name;
    if expected.last() != Some(&92) {
        expected.push(92);
    }
    expected.extend_from_slice(name);
    ensure!(
        observed.name == expected && !observed.directory,
        "fixed profile file used an alias or escaped AC"
    );
    Ok(file)
}

#[cfg(test)]
mod tests {
    use super::*;

    const NONCE: &str = "6123456789abcdef0123456789abcdef";
    const CONTAINER: &str = "Alysis.Execution.7123456789abcdef0123456789abcdef";

    fn fixture() -> std::path::PathBuf {
        let path = crate::util::test_temp_dir().join(format!(
            "alysis-fixed-profile-{}",
            crate::account_state::execution::random_id().unwrap()
        ));
        std::fs::create_dir(&path).unwrap();
        let canonical = std::fs::canonicalize(path).unwrap();
        // Only the exclusively-created synthetic fixture is canonicalized.
        std::path::PathBuf::from(crate::path_id::strip_extended_prefix(
            canonical.to_str().unwrap(),
        ))
    }
    fn root(path: &std::path::Path) -> Root {
        Root::open(path.to_str().unwrap()).unwrap()
    }
    fn leaf(path: &std::path::Path, depth: u32) -> std::path::PathBuf {
        path.join("AC")
            .join(String::from_utf16(&filename(NONCE, depth)).unwrap())
    }

    #[test]
    fn fixed_profile_writer_invalid_binding_refuses_before_sdk_lookup() {
        for (container, id, nonce, depth, message) in [
            (CONTAINER, [1; 24], NONCE, 3, "depth outside bound"),
            (
                CONTAINER,
                [1; 24],
                "../outside",
                0,
                "invalid candidate installation/generation ID",
            ),
            (
                "C:\\outside",
                [1; 24],
                NONCE,
                0,
                "invalid execution container name",
            ),
            (CONTAINER, [0; 24], NONCE, 0, "package identity missing"),
        ] {
            // Deliberately invalid actor: the asserted argument error proves no
            // actor token/SDK profile lookup was used to make this test pass.
            let error = write_fixed_profile_file(container, "not-a-sid", id, nonce, depth)
                .err()
                .unwrap();
            assert!(format!("{error:#}").contains(message));
        }
    }

    #[test]
    fn fixed_profile_writer_requires_original_package_id_before_creating_any_file() {
        let path = fixture();
        std::fs::create_dir(path.join("AC")).unwrap();
        let original = root(&path);
        let mut wrong = original.initial.id;
        wrong[0] ^= 1;
        let error = original.write(wrong, NONCE, 0).err().unwrap();
        assert!(
            error
                .to_string()
                .contains("original package identity differs")
        );
        assert_eq!(std::fs::read_dir(path.join("AC")).unwrap().count(), 0);
        // An AC identity is not the expected package identity either.
        let ac = root(&path.join("AC"));
        let ac_id = ac.initial.id;
        ac.close().unwrap();
        assert!(root(&path).write(ac_id, NONCE, 0).is_err());
        assert_eq!(std::fs::read_dir(path.join("AC")).unwrap().count(), 0);
        std::fs::remove_dir(path.join("AC")).unwrap();
        std::fs::remove_dir(path).unwrap();
    }

    #[test]
    fn fixed_profile_writer_creates_three_distinct_bounded_files_and_refuses_retry() {
        let path = fixture();
        std::fs::create_dir(path.join("AC")).unwrap();
        let mut identities = Vec::new();
        for depth in 0..=2 {
            let original = root(&path);
            let id = original.initial.id;
            let receipt = original.write(id, NONCE, depth).unwrap();
            assert_eq!(receipt.depth, depth);
            assert_eq!(receipt.bytes as usize, payload(NONCE, depth).len());
            assert!(receipt.bytes as usize <= PAYLOAD_LIMIT && receipt.file_id != [0; 24]);
            assert!(!identities.contains(&receipt.file_id));
            identities.push(receipt.file_id);
            let bytes = std::fs::read(leaf(&path, depth)).unwrap();
            assert_eq!(bytes, payload(NONCE, depth));
            assert!(root(&path).write(id, NONCE, depth).is_err());
            assert_eq!(std::fs::read(leaf(&path, depth)).unwrap(), bytes);
        }
        assert_eq!(std::fs::read_dir(path.join("AC")).unwrap().count(), 3);
        for depth in 0..=2 {
            std::fs::remove_file(leaf(&path, depth)).unwrap();
        }
        std::fs::remove_dir(path.join("AC")).unwrap();
        std::fs::remove_dir(path).unwrap();
    }

    #[test]
    fn fixed_profile_writer_refuses_existing_hardlink_and_preserves_outside_canary() {
        let path = fixture();
        std::fs::create_dir(path.join("AC")).unwrap();
        let canary = path.join("outside-canary");
        std::fs::write(&canary, b"unchanged outside target").unwrap();
        std::fs::hard_link(&canary, leaf(&path, 0)).unwrap();
        let original = root(&path);
        let id = original.initial.id;
        assert!(original.write(id, NONCE, 0).is_err());
        assert_eq!(std::fs::read(&canary).unwrap(), b"unchanged outside target");
        assert_eq!(
            std::fs::read(leaf(&path, 0)).unwrap(),
            b"unchanged outside target"
        );
        std::fs::remove_file(leaf(&path, 0)).unwrap();
        std::fs::remove_file(canary).unwrap();
        std::fs::remove_dir(path.join("AC")).unwrap();
        std::fs::remove_dir(path).unwrap();
    }

    #[test]
    fn fixed_profile_writer_refuses_ac_file_or_case_alias_before_effects() {
        for alias in [false, true] {
            let path = fixture();
            if alias {
                std::fs::create_dir(path.join("ac")).unwrap();
            } else {
                std::fs::write(path.join("AC"), b"not a directory").unwrap();
            }
            let original = root(&path);
            let id = original.initial.id;
            assert!(original.write(id, NONCE, 0).is_err());
            if alias {
                assert_eq!(std::fs::read_dir(path.join("ac")).unwrap().count(), 0);
                std::fs::remove_dir(path.join("ac")).unwrap();
            } else {
                assert_eq!(std::fs::read(path.join("AC")).unwrap(), b"not a directory");
                std::fs::remove_file(path.join("AC")).unwrap();
            }
            std::fs::remove_dir(path).unwrap();
        }
    }

    #[test]
    fn fixed_profile_writer_refuses_ac_junction_without_touching_outside_target() {
        let path = fixture();
        let package = path.join("package");
        let outside = path.join("outside");
        std::fs::create_dir(&package).unwrap();
        std::fs::create_dir(&outside).unwrap();
        std::fs::write(outside.join("canary"), b"unchanged").unwrap();
        let link = package.join("AC");
        let mut system = vec![0u16; 32768];
        let length = unsafe {
            windows::Win32::System::SystemInformation::GetSystemDirectoryW(Some(&mut system))
        } as usize;
        assert!(length > 0 && length < system.len());
        let command = std::path::PathBuf::from(String::from_utf16(&system[..length]).unwrap())
            .join("cmd.exe");
        let output = std::process::Command::new(command)
            .args(["/d", "/c", "mklink", "/J"])
            .arg(&link)
            .arg(&outside)
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "fixed writer synthetic junction failed"
        );
        let original = root(&package);
        let id = original.initial.id;
        assert!(original.write(id, NONCE, 0).is_err());
        assert_eq!(std::fs::read(outside.join("canary")).unwrap(), b"unchanged");
        assert_eq!(std::fs::read_dir(&outside).unwrap().count(), 1);
        // Delete only this fixture's junction entry; never follow it recursively.
        std::fs::remove_dir(link).unwrap();
        std::fs::remove_dir(package).unwrap();
        std::fs::remove_file(outside.join("canary")).unwrap();
        std::fs::remove_dir(outside).unwrap();
        std::fs::remove_dir(path).unwrap();
    }
}
