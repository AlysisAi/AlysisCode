//! Disposable fixture publication of the exact test executable to one package.
//! No production writer, all-packages grant, inherited directory grant or
//! restoration authority. Original full image pins stay held through re-pinning.
use super::*;
use crate::acl::{self, GrantMask, SbAceSet};
use crate::path_id::{FileId, capture_file_id};
use std::path::{Path, PathBuf};

const TRAVERSE: u32 = 0x0010_00a0;

#[derive(serde::Serialize)]
pub(super) struct Proof {
    package_sid: String,
    image_id: [u8; 24],
    directory_id: [u8; 24],
    image_mask: u32,
    directory_mask: u32,
    image_flags: u8,
    directory_flags: u8,
    original_objects_retained: bool,
    unrelated_aces_unchanged: bool,
    owners_unchanged: bool,
    fixture_vm_lifetime_only: bool,
}

fn require_path(path: &Path, program_files: &Path, mode: AccountMode) -> Result<PathBuf> {
    ensure!(
        path.file_name().and_then(|s| s.to_str()) == Some("native-tests.exe"),
        "contained image is not the fixed executable"
    );
    let directory = path.parent().context("contained image parent absent")?;
    ensure!(
        directory.parent() == Some(program_files),
        "contained image is outside original Program Files fixture"
    );
    let name = directory
        .file_name()
        .and_then(|s| s.to_str())
        .context("contained fixture directory is not Unicode")?;
    let prefix = match mode {
        AccountMode::Offline => "AlysisV3ContainedofflineQA-",
        AccountMode::Online => "AlysisV3ContainedonlineQA-",
    };
    let suffix = name
        .strip_prefix(prefix)
        .context("contained image fixture mode differs")?;
    crate::account_install::validate_random_id(suffix)?;
    Ok(directory.to_owned())
}

struct Snapshot {
    owner: String,
    aces: Vec<Vec<u8>>,
}

fn snapshot(path: &str) -> Result<Snapshot> {
    use windows::Win32::Security::{
        Authorization::{GetNamedSecurityInfoW, SE_FILE_OBJECT},
        DACL_SECURITY_INFORMATION, OWNER_SECURITY_INFORMATION, PSECURITY_DESCRIPTOR, PSID,
    };
    let name = crate::util::wstr(path);
    let mut acl = std::ptr::null_mut();
    let mut owner = PSID::default();
    let mut raw = PSECURITY_DESCRIPTOR::default();
    let status = unsafe {
        GetNamedSecurityInfoW(
            crate::util::pcwstr(&name),
            SE_FILE_OBJECT,
            DACL_SECURITY_INFORMATION | OWNER_SECURITY_INFORMATION,
            Some(&mut owner),
            None,
            Some(&mut acl),
            None,
            &mut raw,
        )
    };
    crate::util::win32_ok(status, "contained fixture security snapshot")?;
    let sd = crate::util::OwnedSd::from_raw(raw);
    ensure!(!owner.0.is_null(), "contained fixture owner missing");
    let owner = crate::sid::psid_to_string(owner)?;
    ensure!(!acl.is_null(), "contained image null DACL refused");
    ensure!(
        acl::sd_dacl_protected(&sd)?,
        "contained fixture DACL must stay protected"
    );
    let rows = acl::filter_aces(acl, |_, _| true)?.0;
    let aces = rows
        .into_iter()
        .map(|(p, n)| unsafe { std::slice::from_raw_parts(p.cast::<u8>(), n as usize).to_vec() })
        .collect();
    Ok(Snapshot { owner, aces })
}

fn require_change(
    before: &Snapshot,
    after: &Snapshot,
    sid: &[u8],
    flags: u8,
    mask: u32,
) -> Result<()> {
    ensure!(
        before.owner == after.owner,
        "contained fixture owner changed"
    );
    require_added(&before.aces, &after.aces, sid, flags, mask)
}

fn require_added(
    before: &[Vec<u8>],
    after: &[Vec<u8>],
    sid: &[u8],
    flags: u8,
    mask: u32,
) -> Result<()> {
    ensure!(
        before.iter().all(|r| !acl::ace_sid_is(r, sid)),
        "contained fixture already has selected package ACE"
    );
    let (selected, others): (Vec<_>, Vec<_>) = after.iter().partition(|r| acl::ace_sid_is(r, sid));
    ensure!(
        others
            .iter()
            .map(|r| r.as_slice())
            .eq(before.iter().map(Vec::as_slice)),
        "contained fixture unrelated ACEs changed"
    );
    ensure!(
        selected.len() == 1,
        "contained fixture package ACE count differs"
    );
    let row = selected[0];
    ensure!(
        row.len() == 8 + sid.len()
            && row[0] == 0
            && row[1] == flags
            && u32::from_le_bytes(row[4..8].try_into()?) == mask,
        "contained fixture package grant differs: type={} flags={} mask={:#x} bytes={} expected_flags={} expected_mask={:#x}",
        row[0],
        row[1],
        u32::from_le_bytes(row[4..8].try_into()?),
        row.len(),
        flags,
        mask
    );
    Ok(())
}

pub(super) fn prepare(
    ready: &ReadyInstallation,
    identity: &CandidateJournalIdentity,
) -> Result<(PinnedRunnerImage, Proof)> {
    // An environment string alone cannot admit this mutation.
    let mode = crate::account_rpc_v3_lifecycle_fixture::contained_mode()?
        .context("original contained image admission missing")?;
    identity.validate(&ready.binding(mode)?)?;
    let original = PinnedRunnerImage::prepare(ready)?;
    let path = original.application()?.to_owned();
    let image_id = original.file_identity()?;
    let program_files =
        crate::account_state::execution::bootstrap_plan::initializer_image::known_profile_folder(
            &windows::Win32::UI::Shell::FOLDERID_ProgramFiles,
        )?;
    let directory = require_path(&path, Path::new(&program_files), mode)?;
    let image_text = path.to_str().context("contained image not Unicode")?;
    let directory_text = directory
        .to_str()
        .context("contained directory not Unicode")?;
    let directory_id = capture_file_id(directory_text)?;
    let sid = crate::sid::LocalPsid::from_string(&identity.package_sid)?;
    let before_image = snapshot(image_text)?;
    let before_directory = snapshot(directory_text)?;
    ensure!(
        before_image
            .aces
            .iter()
            .chain(&before_directory.aces)
            .all(|r| !acl::ace_sid_is(r, sid.as_bytes())),
        "contained fixture package access already exists"
    );
    // The parent grant is object-only and cannot reach the outside canary.
    acl::apply_sandbox_aces_for_id(
        directory_text,
        &identity.package_sid,
        SbAceSet {
            grant: Some(GrantMask::Traverse),
            ..Default::default()
        },
        Some(directory_id),
    )?;
    acl::apply_sandbox_aces_for_id(
        image_text,
        &identity.package_sid,
        SbAceSet {
            grant: Some(GrantMask::ReadOnly),
            ..Default::default()
        },
        Some(FileId::from_bytes(&image_id)?),
    )
    .context("contained image grant")?;
    require_change(
        &before_directory,
        &snapshot(directory_text)?,
        sid.as_bytes(),
        0,
        TRAVERSE,
    )
    .context("contained directory access readback")?;
    require_change(
        &before_image,
        &snapshot(image_text)?,
        sid.as_bytes(),
        // SetSecurityInfo strips OI/CI from a regular file's stored ACE.
        // Require object-only flags, never directory inheritance.
        0,
        acl::Mask::FILE_READ_EXEC.bits(),
    )
    .context("contained image access readback")?;
    // Do not ask the old guard to bless its intentionally changed descriptor.
    // It retains original objects; a fresh full-policy guard observes new ACLs.
    let image = PinnedRunnerImage::prepare(ready)?;
    ensure!(
        image.file_identity()? == image_id
            && capture_file_id(directory_text)?.as_bytes() == directory_id.as_bytes(),
        "contained publication replaced original objects"
    );
    drop(original);
    Ok((
        image,
        Proof {
            package_sid: identity.package_sid.clone(),
            image_id,
            directory_id: directory_id.as_bytes(),
            image_mask: acl::Mask::FILE_READ_EXEC.bits(),
            directory_mask: TRAVERSE,
            image_flags: 0,
            directory_flags: 0,
            original_objects_retained: true,
            unrelated_aces_unchanged: true,
            owners_unchanged: true,
            fixture_vm_lifetime_only: true,
        },
    ))
}

#[test]
fn station_contained_image_publication_refuses_other_path_mode_or_binary() {
    let base = Path::new(r"C:\Program Files");
    let directory = base.join(format!("AlysisV3ContainedofflineQA-{}", "1".repeat(32)));
    let valid = directory.join("native-tests.exe");
    assert_eq!(
        require_path(&valid, base, AccountMode::Offline).unwrap(),
        directory
    );
    assert!(require_path(&valid, base, AccountMode::Online).is_err());
    for path in [
        base.join("native-tests.exe"),
        directory.join("other.exe"),
        directory.join("nested").join("native-tests.exe"),
        base.join("AlysisV3ContainedofflineQA-other")
            .join("native-tests.exe"),
        Path::new(r"C:\Users\fixture")
            .join(directory.file_name().unwrap())
            .join("native-tests.exe"),
    ] {
        assert!(require_path(&path, base, AccountMode::Offline).is_err());
    }
}

#[test]
fn station_contained_image_publication_ace_diff_refuses_write_inheritance_or_other_edits() {
    let sid = crate::sid::LocalPsid::from_string("S-1-15-2-1").unwrap();
    let mut row = vec![0, 0, 0, 0];
    row.extend_from_slice(&TRAVERSE.to_le_bytes());
    row.extend_from_slice(sid.as_bytes());
    let size = (row.len() as u16).to_le_bytes();
    row[2..4].copy_from_slice(&size);
    let before = vec![vec![1, 2, 3, 4]];
    let after = vec![row.clone(), before[0].clone()];
    require_added(&before, &after, sid.as_bytes(), 0, TRAVERSE).unwrap();
    let original = Snapshot {
        owner: "S-1-5-18".into(),
        aces: before.clone(),
    };
    let mut observed = Snapshot {
        owner: original.owner.clone(),
        aces: after.clone(),
    };
    require_change(&original, &observed, sid.as_bytes(), 0, TRAVERSE).unwrap();
    observed.owner = "S-1-5-32-544".into();
    assert!(require_change(&original, &observed, sid.as_bytes(), 0, TRAVERSE).is_err());
    for index in [0, 1, 4, after[0].len() - 1] {
        let mut bad = after.clone();
        bad[0][index] ^= 1;
        assert!(require_added(&before, &bad, sid.as_bytes(), 0, TRAVERSE).is_err());
    }
    assert!(require_added(&after, &after, sid.as_bytes(), 0, TRAVERSE).is_err());
    let mut changed = after;
    changed[1][0] ^= 1;
    assert!(require_added(&before, &changed, sid.as_bytes(), 0, TRAVERSE).is_err());
}

#[test]
fn station_contained_image_native_acl_roundtrip_keeps_owner_and_outside_canary() -> Result<()> {
    // Synthetic ordinary files only: no account, profile, Program Files, token,
    // process creation or production publication. Keep failed bytes for review.
    let root = std::env::current_dir()?.join(format!(".qa-contained-image-{}", random_id()?));
    std::fs::create_dir(&root)?;
    let file = root.join("native-tests.exe");
    let canary = root.join("outside.txt");
    std::fs::write(&file, b"synthetic non-executable")?;
    std::fs::write(&canary, b"outside canary")?;
    let host = crate::sid::current_user_sid()?;
    let directory_sddl =
        format!("D:P(A;OICI;FA;;;{host})(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FRFX;;;BU)");
    let file_sddl = format!("D:P(A;;FA;;;{host})(A;;FA;;;SY)(A;;FA;;;BA)(A;;FRFX;;;BU)");
    acl::set_path_dacl_from_sddl(root.to_str().unwrap(), &directory_sddl, "synthetic parent")?;
    acl::set_path_dacl_from_sddl(file.to_str().unwrap(), &file_sddl, "synthetic image")?;
    acl::set_path_dacl_from_sddl(canary.to_str().unwrap(), &file_sddl, "synthetic canary")?;
    let before_canary = snapshot(canary.to_str().unwrap())?;
    let package =
        "S-1-15-2-3981268610-3992117251-1289311728-839211721-2265390185-3700473191-2101416696";
    let sid = crate::sid::LocalPsid::from_string(package)?;
    for (path, grant, flags, mask) in [
        (&root, GrantMask::Traverse, 0, TRAVERSE),
        (
            &file,
            GrantMask::ReadOnly,
            0,
            acl::Mask::FILE_READ_EXEC.bits(),
        ),
    ] {
        let text = path.to_str().unwrap();
        let before = snapshot(text)?;
        let id = capture_file_id(text)?;
        acl::apply_sandbox_aces_for_id(
            text,
            package,
            SbAceSet {
                grant: Some(grant),
                ..Default::default()
            },
            Some(id),
        )?;
        require_change(&before, &snapshot(text)?, sid.as_bytes(), flags, mask)?;
    }
    let after_canary = snapshot(canary.to_str().unwrap())?;
    ensure!(
        before_canary.owner == after_canary.owner && before_canary.aces == after_canary.aces,
        "synthetic canary security changed"
    );
    ensure!(
        std::fs::read(&canary)? == b"outside canary",
        "synthetic canary data changed"
    );
    std::fs::remove_file(file)?;
    std::fs::remove_file(canary)?;
    std::fs::remove_dir(root)?;
    Ok(())
}
