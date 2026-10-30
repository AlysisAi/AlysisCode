//! Sealed fixture: one primary lowbox, one private USER pair, one bounded endpoint.
//! The inherited-USER experiment passes only fresh lowbox-opened limited handles;
//! full original USER handles, tokens and job handles remain private.
use super::super::private_station_fixture::{render, security, text};
use super::*;
use crate::{launch::SpawnedChild, process_attributes::ProcThreadAttrs};
use windows::{
    Wdk::{Foundation::OBJECT_ATTRIBUTES, Storage::FileSystem::NtSetSecurityObject},
    Win32::{
        Security::*,
        System::{
            Diagnostics::Debug::{
                GetErrorMode, OutputDebugStringW, SEM_FAILCRITICALERRORS, SEM_NOGPFAULTERRORBOX,
                SetErrorMode, THREAD_ERROR_MODE,
            },
            JobObjects::IsProcessInJob,
            LibraryLoader::*,
            StationsAndDesktops::*,
        },
    },
    core::{PCWSTR, PWSTR, s, w},
};
const TEST: &str = "account_control::station_lowbox_fixture::native::station_lowbox_child";
const ENV: &str = "ALYSIS_STATION_LOWBOX_ENDPOINT";
const STARTED: &[u8] = b"alysis-station-lowbox-started-v1";
// Source-pinned experiment only, never selected by an untrusted environment.
// Compare KernelBase's constructor after the private shadow/startup fixes,
// with the same filtered primary. No loader/debugger, additional inherited USER
// handles or fallback token. This comparison cannot report qualification.
// Capture six known private objects using existing permissions. Open the pipe
// prefix without creation; this cannot qualify OS-constructor equivalence.
const CAPTURED_NAMESPACE_COMPARISON: bool = false;
const ATTRIBUTE_BIRTH_COMPARISON: bool = false;
const DOCUMENTED_DESKTOP_COMPARISON: bool = false;
const INHERITED_DESKTOP_SELECTION: bool = false;
const ATTRIBUTE_WITHOUT_USER_HANDLES: bool = true;
// Source-pinned loader comparison applies only to the later filtered child.
// The original lowbox keeps normal creation flags and endpoint-only inheritance.
const FILTERED_LOADER_COMPARISON: bool = true;
// Fixture-only private station: READ_CONTROL, ENUMDESKTOPS, READATTRIBUTES,
// ACCESSGLOBALATOMS. Hosted win32kfull!xxxResolveDesktop rechecks 0x20020
// for AppContainer tokens. The station is nonvisible; all job UI limits,
// including GLOBALATOMS, still apply. This comparison is not qualification.
const PRIVATE_STATION_ACCESS: u32 = 0x20023;
#[path = "account_station_lowbox_family.rs"]
pub(super) mod family;
#[path = "account_station_filtered_primary.rs"]
pub(super) mod filtered_primary;
#[path = "account_station_lowbox_inherited.rs"]
pub(super) mod inherited_user;
#[path = "account_station_lowbox_user_access.rs"]
pub(super) mod user_access;

/// Fixed read-only namespace opens for constructor diagnostics. No enumeration,
/// creation, alias traversal or ACL mutation. Shared names are explicit rather
/// than followed through the package's Global symbolic link.
fn constructor_directories(container: &str, session: u32) -> Result<Vec<(String, u32, i32, bool)>> {
    use windows::Wdk::Storage::FileSystem::NtOpenDirectoryObject;
    crate::account_impersonation::require_process_identity()?;
    let package = crate::appcontainer::derive(container)?;
    let root = crate::account_namespace::namespace_path(&package.string()?)?;
    let paths = [
        ("package", root.clone(), 0x0f),
        ("private_rpc", format!("{root}\\RPC Control"), 0x0f),
        ("shared_base", "\\BaseNamedObjects".into(), 0x0f),
        ("shared_rpc", "\\RPC Control".into(), 0x0f),
        (
            "session_base",
            format!("\\Sessions\\{session}\\BaseNamedObjects"),
            0x20003,
        ),
        (
            "session_appcontainers",
            format!("\\Sessions\\{session}\\AppContainerNamedObjects"),
            0x0f,
        ),
    ];
    let mut observations = Vec::with_capacity(6);
    for (kind, path, access) in paths {
        let (mut wide, length) = crate::util::counted_utf16(&path);
        let name = UNICODE_STRING {
            Length: length,
            MaximumLength: length,
            Buffer: PWSTR(wide.as_mut_ptr()),
        };
        let attrs = OBJECT_ATTRIBUTES {
            Length: size_of::<OBJECT_ATTRIBUTES>() as u32,
            ObjectName: &name,
            Attributes: OBJECT_ATTRIBUTE_FLAGS(OBJ_CASE_INSENSITIVE.0 | OBJ_DONT_REPARSE.0),
            ..Default::default()
        };
        let mut raw = HANDLE::default();
        // Local static constructor code requests these directory-specific bits.
        // Exact hosted code is recorded separately; this alone is not causality.
        let status = unsafe { NtOpenDirectoryObject(&mut raw, access, &attrs) };
        let closed = if status.is_ok() {
            let handle = OwnedHandle(raw);
            crate::account_namespace::verify_noninheritable(handle.raw())?;
            unsafe { CloseHandle(handle.into_raw())? };
            true
        } else {
            false
        };
        observations.push((kind.into(), access, status.0, closed));
    }
    Ok(observations)
}

/// Mirror only the constructor's full-access duplication of two fixed private
/// directories. Never invoke its subsequent label setter or change security.
fn constructor_directory_upgrades(container: &str) -> Result<Vec<(String, u32, i32, bool)>> {
    use windows::Wdk::Storage::FileSystem::NtOpenDirectoryObject;
    #[link(name = "ntdll")]
    unsafe extern "system" {
        fn NtDuplicateObject(
            source_process: HANDLE,
            source: HANDLE,
            target_process: HANDLE,
            target: *mut HANDLE,
            access: u32,
            attributes: u32,
            options: u32,
        ) -> NTSTATUS;
    }
    crate::account_impersonation::require_process_identity()?;
    let package = crate::appcontainer::derive(container)?;
    let root = crate::account_namespace::namespace_path(&package.string()?)?;
    let mut observations = Vec::with_capacity(2);
    for (kind, path) in [
        ("package", root.clone()),
        ("private_rpc", format!("{root}\\RPC Control")),
    ] {
        let (mut wide, length) = crate::util::counted_utf16(&path);
        let name = UNICODE_STRING {
            Length: length,
            MaximumLength: length,
            Buffer: PWSTR(wide.as_mut_ptr()),
        };
        let attrs = OBJECT_ATTRIBUTES {
            Length: size_of::<OBJECT_ATTRIBUTES>() as u32,
            ObjectName: &name,
            Attributes: OBJECT_ATTRIBUTE_FLAGS(OBJ_CASE_INSENSITIVE.0 | OBJ_DONT_REPARSE.0),
            ..Default::default()
        };
        let mut raw = HANDLE::default();
        unsafe { NtOpenDirectoryObject(&mut raw, 0x0f, &attrs).ok()? };
        let source = OwnedHandle(raw);
        noninheritable(source.raw())?;
        let mut duplicate = HANDLE::default();
        let status = unsafe {
            let process = windows::Win32::System::Threading::GetCurrentProcess();
            NtDuplicateObject(
                process,
                source.raw(),
                process,
                &mut duplicate,
                0xf000f,
                0,
                0,
            )
        };
        let closed = if status.is_ok() {
            let duplicate = OwnedHandle(duplicate);
            noninheritable(duplicate.raw())?;
            unsafe { CloseHandle(duplicate.into_raw())? };
            true
        } else {
            false
        };
        unsafe { CloseHandle(source.into_raw())? };
        observations.push((kind.into(), 0xf000f, status.0, closed));
    }
    Ok(observations)
}

/// Open the three fixed private link objects themselves, never their targets.
/// Full-access requests mirror the hosted constructor's OPENIF link operation;
/// this observer performs no create, delete, target change or security write.
fn constructor_links(container: &str) -> Result<Vec<(String, u32, i32, bool)>> {
    #[link(name = "ntdll")]
    unsafe extern "system" {
        fn NtOpenSymbolicLinkObject(
            handle: *mut HANDLE,
            access: u32,
            attributes: *const OBJECT_ATTRIBUTES,
        ) -> NTSTATUS;
    }
    crate::account_impersonation::require_process_identity()?;
    let package = crate::appcontainer::derive(container)?;
    let root = crate::account_namespace::namespace_path(&package.string()?)?;
    let mut observations = Vec::with_capacity(6);
    for component in ["Global", "Local", "Session"] {
        let (mut wide, length) = crate::util::counted_utf16(&format!("{root}\\{component}"));
        let name = UNICODE_STRING {
            Length: length,
            MaximumLength: length,
            Buffer: PWSTR(wide.as_mut_ptr()),
        };
        let attrs = OBJECT_ATTRIBUTES {
            Length: size_of::<OBJECT_ATTRIBUTES>() as u32,
            ObjectName: &name,
            Attributes: OBJECT_ATTRIBUTE_FLAGS(OBJ_CASE_INSENSITIVE.0 | OBJ_DONT_REPARSE.0),
            ..Default::default()
        };
        // NtOpenSymbolicLinkObject selects SymbolicLink itself. Do not add
        // OBJ_OPENLINK (invalid for this API) or resolve/query its target.
        for access in [1, 0xf0001] {
            let mut raw = HANDLE::default();
            let status = unsafe { NtOpenSymbolicLinkObject(&mut raw, access, &attrs) };
            let closed = if status.is_ok() {
                let handle = OwnedHandle(raw);
                noninheritable(handle.raw())?;
                unsafe {
                    CloseHandle(handle.into_raw())?;
                }
                true
            } else {
                false
            };
            observations.push((component.into(), access, status.0, closed));
        }
    }
    Ok(observations)
}

fn child_environment(
    windows: String,
    system: String,
    local: String,
    profile: String,
    roaming: String,
    endpoint: usize,
) -> Result<Vec<u16>> {
    ensure!(
        endpoint > 0 && endpoint < isize::MAX as usize,
        "invalid child endpoint"
    );
    for value in [&windows, &system, &local, &profile, &roaming] {
        ensure!(
            std::path::Path::new(value).is_absolute() && !value.chars().any(char::is_control),
            "child environment path is invalid"
        );
    }
    let values = [
        ("SystemRoot".into(), windows.clone()),
        ("WINDIR".into(), windows),
        ("PATH".into(), system),
        ("APPDATA".into(), roaming),
        ("LOCALAPPDATA".into(), local.clone()),
        ("USERPROFILE".into(), profile),
        ("TEMP".into(), format!("{local}\\Temp")),
        ("TMP".into(), format!("{local}\\Temp")),
        (ENV.into(), endpoint.to_string()),
    ];
    let environment = crate::launch::build_env_block_with_inheritance(&values, false);
    ensure!(
        environment.len() <= 32767,
        "child environment exceeds bound"
    );
    Ok(environment)
}

fn loader_image(name: &str) -> Result<bool> {
    let Some(tail) = name.strip_prefix("alysis-station-loader-") else {
        return Ok(false);
    };
    let run = tail
        .strip_suffix(".exe")
        .context("loader image suffix differs")?;
    ensure!(
        !run.is_empty()
            && run.len() <= 20
            && !run.starts_with('0')
            && run.bytes().all(|b| b.is_ascii_digit()),
        "loader image run differs"
    );
    Ok(true)
}

fn loader_targets(name: &str) -> Result<(bool, bool)> {
    let selected = loader_image(name)?;
    Ok((
        selected && !FILTERED_LOADER_COMPARISON,
        selected && FILTERED_LOADER_COMPARISON,
    ))
}
pub(super) fn terminal_checkpoint_stage() -> &'static str {
    if FILTERED_LOADER_COMPARISON {
        "filtered_primary_control"
    } else {
        "loader_observation"
    }
}

struct HeadlessErrors(THREAD_ERROR_MODE);
impl HeadlessErrors {
    fn enter(receipt: &mut Receipt) -> Result<Self> {
        // This is a separate fixed raw-account process, not the host broker.
        // The newly born child inherits this process setting. Do not change
        // any system/WER registry setting or mask alignment faults.
        let old = THREAD_ERROR_MODE(unsafe { GetErrorMode() });
        let guard = Self(old);
        let desired = old | SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX;
        unsafe {
            SetErrorMode(desired);
        }
        let observed = unsafe { GetErrorMode() };
        ensure!(observed == desired.0, "fixed child error mode differs");
        receipt.headless_error_mode = Some(observed);
        Ok(guard)
    }
}
impl Drop for HeadlessErrors {
    fn drop(&mut self) {
        unsafe {
            SetErrorMode(self.0);
        }
    }
}
const INFO: OBJECT_SECURITY_INFORMATION = OBJECT_SECURITY_INFORMATION(
    OWNER_SECURITY_INFORMATION.0 | DACL_SECURITY_INFORMATION.0 | LABEL_SECURITY_INFORMATION.0,
);
const ORIGINAL_INFO: OBJECT_SECURITY_INFORMATION =
    OBJECT_SECURITY_INFORMATION(INFO.0 | GROUP_SECURITY_INFORMATION.0);
type Create = unsafe extern "system" fn(
    *const OBJECT_ATTRIBUTES,
    *const UNICODE_STRING,
    *const core::ffi::c_void,
    u32,
    u32,
    u32,
) -> HDESK;
type Open = unsafe extern "system" fn(*const OBJECT_ATTRIBUTES, u32, u32) -> HDESK;
struct Station(Option<HWINSTA>);
impl Station {
    fn raw(&self) -> Result<HWINSTA> {
        self.0.context("original private station closed")
    }
    fn close(&mut self) -> Result<()> {
        unsafe {
            CloseWindowStation(self.0.take().context("station close repeated")?)?;
        }
        Ok(())
    }
}
impl Drop for Station {
    fn drop(&mut self) {
        if let Some(h) = self.0.take() {
            let _ = unsafe { CloseWindowStation(h) };
        }
    }
}
struct Desktop(Option<HDESK>);
impl Desktop {
    fn close(&mut self) -> Result<()> {
        unsafe {
            CloseDesktop(self.0.take().context("desktop close repeated")?)?;
        }
        Ok(())
    }
}
impl Drop for Desktop {
    fn drop(&mut self) {
        if let Some(h) = self.0.take() {
            let _ = unsafe { CloseDesktop(h) };
        }
    }
}
// Only the admitted disposable diagnostic calls this. The trusted creator
// restores both original associations before any lowbox child can be born.
// No shared descriptor is changed and the child receives the same limited pair.
struct CreatorAssociation {
    station: HWINSTA,
    desktop: HDESK,
    restored: bool,
}
impl CreatorAssociation {
    fn restore(&mut self) -> Result<()> {
        unsafe {
            SetProcessWindowStation(self.station)?;
            if !CompareObjectHandles(
                HANDLE(GetThreadDesktop(GetCurrentThreadId())?.0),
                HANDLE(self.desktop.0),
            )
            .as_bool()
            {
                SetThreadDesktop(self.desktop)?;
            }
            ensure!(
                CompareObjectHandles(HANDLE(GetProcessWindowStation()?.0), HANDLE(self.station.0))
                    .as_bool()
                    && CompareObjectHandles(
                        HANDLE(GetThreadDesktop(GetCurrentThreadId())?.0),
                        HANDLE(self.desktop.0)
                    )
                    .as_bool(),
                "documented desktop creator associations differ"
            );
        }
        self.restored = true;
        Ok(())
    }
}
impl Drop for CreatorAssociation {
    fn drop(&mut self) {
        if !self.restored {
            let _ = self.restore();
        }
    }
}
fn documented_desktop(
    station: HWINSTA,
    original_station: HWINSTA,
    original_desktop: HDESK,
    name: &str,
    security: &OwnedSd,
    receipt: &mut Receipt,
) -> Result<Desktop> {
    receipt.documented_desktop_birth = true;
    let mut association = CreatorAssociation {
        station: original_station,
        desktop: original_desktop,
        restored: false,
    };
    unsafe {
        SetProcessWindowStation(station)?;
    }
    ensure!(
        unsafe { CompareObjectHandles(HANDLE(GetProcessWindowStation()?.0), HANDLE(station.0)) }
            .as_bool(),
        "documented desktop creator did not select original private station"
    );
    let sa = SECURITY_ATTRIBUTES {
        nLength: size_of::<SECURITY_ATTRIBUTES>() as u32,
        lpSecurityDescriptor: security.ptr.0,
        bInheritHandle: false.into(),
    };
    // This API can open an existing name. The fixture owns the newly exclusive
    // private station and uses its sealed 128-bit nonce before any untrusted
    // child exists. This comparison is NOT atomic-creation qualification.
    let created = unsafe {
        CreateDesktopW(
            pcwstr(&wstr(name)),
            PCWSTR::null(),
            None,
            DESKTOP_CONTROL_FLAGS(0),
            0xf01ff,
            Some(&sa),
        )
    }
    .map(|h| Desktop(Some(h)));
    association.restore()?;
    receipt.desktop_creator_restored = true;
    Ok(created?)
}
struct UserPair {
    desktop: Desktop, // original desktop closes before its original parent
    station: Station,
    name: String,
    desktop_name: String,
    desktop_bytes: Vec<u8>,
    station_bytes: Vec<u8>,
    original: HWINSTA,
    original_desktop: HDESK,
    original_bytes: Vec<u8>,
    open: Open,
}
fn object_attributes(
    name: &mut UNICODE_STRING,
    station: HWINSTA,
    sd: Option<&OwnedSd>,
) -> OBJECT_ATTRIBUTES {
    OBJECT_ATTRIBUTES {
        Length: size_of::<OBJECT_ATTRIBUTES>() as u32,
        RootDirectory: HANDLE(station.0),
        ObjectName: name,
        Attributes: OBJECT_ATTRIBUTE_FLAGS(OBJ_CASE_INSENSITIVE.0 | OBJ_DONT_REPARSE.0),
        SecurityDescriptor: sd.map_or(std::ptr::null(), |s| s.ptr.0.cast()),
        ..Default::default()
    }
}
impl UserPair {
    fn create(request: &Request, receipt: &mut Receipt) -> Result<Self> {
        let b = binding(request)?;
        crate::account_impersonation::require_process_identity()?;
        let original = unsafe { GetProcessWindowStation()? };
        let original_desktop = unsafe { GetThreadDesktop(GetCurrentThreadId())? };
        receipt.original_station = Some(text(HANDLE(original.0), UOI_NAME)?);
        let original_bytes = security(HANDLE(original.0), ORIGINAL_INFO)?.0;
        let module = unsafe { GetModuleHandleW(w!("win32u.dll"))? };
        let create: Create = unsafe {
            std::mem::transmute(
                GetProcAddress(module, s!("NtUserCreateDesktopEx"))
                    .context("exclusive desktop API missing")?,
            )
        };
        let open: Open = unsafe {
            std::mem::transmute(
                GetProcAddress(module, s!("NtUserOpenDesktop"))
                    .context("relative desktop API missing")?,
            )
        };
        let owner = &request.birth.selection.account.account_sid;
        let package = &b.profile.package_sid;
        // Only PRIVATE_STATION_ACCESS is granted to the package. OW
        // suppresses implicit owner rights; its actual USER enforcement is the
        // hypothesis tested below, not an assertion made by this descriptor.
        let sd = OwnedSd::from_sddl(&format!(
            "O:{owner}D:P(A;;0xf037f;;;SY)(A;;0xf037f;;;{owner})(A;;0x{PRIVATE_STATION_ACCESS:x};;;{package})(A;;RC;;;OW)S:(ML;;NW;;;LW)"
        ))?;
        let sa = SECURITY_ATTRIBUTES {
            nLength: size_of::<SECURITY_ATTRIBUTES>() as u32,
            lpSecurityDescriptor: sd.ptr.0,
            bInheritHandle: false.into(),
        };
        receipt.stage = "create_station".into();
        let raw = unsafe { CreateWindowStationW(PCWSTR::null(), 1, 0xf037f, Some(&sa))? };
        let station = Station(Some(raw));
        ensure!(
            !unsafe { CompareObjectHandles(HANDLE(raw.0), HANDLE(original.0)) }.as_bool(),
            "private station aliases original"
        );
        noninheritable(HANDLE(raw.0))?;
        let name = text(HANDLE(raw.0), UOI_NAME)?;
        ensure!(
            !name.contains(['\\', '/']),
            "station name is not a component"
        );
        ensure!(
            text(HANDLE(raw.0), UOI_TYPE)? == "WindowStation",
            "private station type differs"
        );
        let mut flags = USEROBJECTFLAGS::default();
        let mut bytes = 0;
        unsafe {
            GetUserObjectInformationW(
                HANDLE(raw.0),
                UOI_FLAGS,
                Some((&mut flags as *mut USEROBJECTFLAGS).cast()),
                size_of::<USEROBJECTFLAGS>() as u32,
                Some(&mut bytes),
            )?;
        }
        ensure!(
            bytes as usize == size_of::<USEROBJECTFLAGS>()
                && !flags.fInherit.as_bool()
                && flags.dwFlags & 1 == 0,
            "private station must be noninheritable and nonvisible"
        );
        let before = security(HANDLE(raw.0), INFO)?;
        ensure!(
            before.1 == render(sd.ptr, INFO)?,
            "private station descriptor differs"
        );
        receipt.station_name = Some(name.clone());
        receipt.station_before = Some(before.1);
        match unsafe { CreateWindowStationW(PCWSTR::null(), 1, 0xf037f, Some(&sa)) } {
            Ok(h) => {
                let mut unexpected = Station(Some(h));
                unexpected.close()?;
                anyhow::bail!("exclusive station collision succeeded");
            }
            Err(e) => ensure!(
                e.code() == ERROR_ALREADY_EXISTS.to_hresult(),
                "exclusive station collision differs"
            ),
        }
        // Prove the native security operation is usable on THIS station before
        // treating its later lowbox refusal as a permission observation.
        unsafe {
            NtSetSecurityObject(
                HANDLE(raw.0),
                DACL_SECURITY_INFORMATION.0 | PROTECTED_DACL_SECURITY_INFORMATION.0,
                sd.ptr,
            )
        }
        .ok()?;
        ensure!(
            security(HANDLE(raw.0), INFO)?.0 == before.0,
            "station positive native set changed descriptor"
        );
        receipt.stage = "create_desktop".into();
        let desktop_name = format!("alysis-station-lowbox-{}", request.birth.nonce);
        let (mut buf, length) = crate::util::counted_utf16(&desktop_name);
        let mut unicode = UNICODE_STRING {
            Length: length,
            MaximumLength: length,
            Buffer: PWSTR(buf.as_mut_ptr()),
        };
        let private = OwnedSd::from_sddl(&format!(
            "O:{owner}D:P(A;;0xf01ff;;;SY)(A;;0xf01ff;;;{owner})"
        ))?;
        let low = OwnedSd::from_sddl(&format!(
            "O:{owner}D:P(A;;0xf01ff;;;SY)(A;;0xf01ff;;;{owner})S:(ML;;NW;;;LW)"
        ))?;
        let published = OwnedSd::from_sddl(&format!(
            "O:{owner}D:P(A;;0xf01ff;;;SY)(A;;0xf01ff;;;{owner})(A;;0x20083;;;{package})(A;;RC;;;OW)S:(ML;;NW;;;LW)"
        ))?;
        let image = std::env::current_exe()?;
        let documented = DOCUMENTED_DESKTOP_COMPARISON
            && loader_targets(
                image
                    .file_name()
                    .and_then(|n| n.to_str())
                    .context("fixed desktop creator image invalid")?,
            )?
            .0;
        let desktop = if documented {
            documented_desktop(
                raw,
                original,
                original_desktop,
                &desktop_name,
                &private,
                receipt,
            )?
        } else {
            let handle = unsafe {
                create(
                    &object_attributes(&mut unicode, raw, Some(&private)),
                    std::ptr::null(),
                    std::ptr::null(),
                    0,
                    0xf01ff,
                    0,
                )
            };
            if handle.is_invalid() {
                return Err(windows::core::Error::from_thread().into());
            }
            Desktop(Some(handle))
        };
        let raw_desktop = desktop.0.context("new private desktop absent")?;
        noninheritable(HANDLE(raw_desktop.0))?;
        ensure!(
            security(
                HANDLE(raw_desktop.0),
                OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION
            )?
            .1 == render(
                private.ptr,
                OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION
            )?,
            "private desktop birth policy differs"
        );
        unsafe {
            NtSetSecurityObject(HANDLE(raw_desktop.0), LABEL_SECURITY_INFORMATION.0, low.ptr)
        }
        .ok()?;
        ensure!(
            security(HANDLE(raw_desktop.0), INFO)?.1 == render(low.ptr, INFO)?,
            "desktop Low label differs"
        );
        unsafe {
            NtSetSecurityObject(
                HANDLE(raw_desktop.0),
                DACL_SECURITY_INFORMATION.0 | PROTECTED_DACL_SECURITY_INFORMATION.0,
                published.ptr,
            )
        }
        .ok()?;
        ensure!(
            security(HANDLE(raw_desktop.0), INFO)?.1 == render(published.ptr, INFO)?,
            "desktop package policy differs"
        );
        let collision = unsafe {
            create(
                &object_attributes(&mut unicode, raw, Some(&private)),
                std::ptr::null(),
                std::ptr::null(),
                0,
                0xf01ff,
                0,
            )
        };
        if !collision.is_invalid() {
            let mut unexpected = Desktop(Some(collision));
            unexpected.close()?;
            anyhow::bail!("exclusive desktop collision succeeded");
        }
        ensure!(
            windows::core::Error::from_thread().code() == ERROR_ALREADY_EXISTS.to_hresult(),
            "desktop collision code differs"
        );
        let observed = unsafe { open(&object_attributes(&mut unicode, raw, None), 0, 0x20081) };
        if observed.is_invalid() {
            return Err(windows::core::Error::from_thread().into());
        }
        let mut observed = Desktop(Some(observed));
        let same =
            unsafe { CompareObjectHandles(HANDLE(raw_desktop.0), HANDLE(observed.0.unwrap().0)) }
                .as_bool();
        observed.close()?;
        ensure!(same, "desktop name is not original object");
        receipt.desktop_name = Some(desktop_name.clone());
        let desktop_before = security(HANDLE(raw_desktop.0), INFO)?;
        receipt.desktop_before = Some(desktop_before.1);
        Ok(Self {
            desktop,
            station,
            name,
            desktop_name,
            desktop_bytes: desktop_before.0,
            station_bytes: before.0,
            original,
            original_desktop,
            original_bytes,
            open,
        })
    }
    fn finish(&mut self, receipt: &mut Receipt) -> Result<()> {
        let raw = self.station.raw()?;
        receipt.stage = "desktop_descriptor".into();
        let desktop_after = security(
            HANDLE(self.desktop.0.context("original desktop closed")?.0),
            INFO,
        )?;
        receipt.desktop_after = Some(desktop_after.1);
        ensure!(
            desktop_after.0 == self.desktop_bytes,
            "lowbox changed original desktop security"
        );
        receipt.stage = "descriptor".into();
        let after = security(HANDLE(raw.0), INFO)?;
        receipt.station_after = Some(after.1);
        ensure!(
            after.0 == self.station_bytes,
            "lowbox changed original station security"
        );
        receipt.stage = "close_desktop".into();
        self.desktop.close()?;
        receipt.desktop_closed = true;
        let (mut buf, length) = crate::util::counted_utf16(&self.desktop_name);
        let mut name = UNICODE_STRING {
            Length: length,
            MaximumLength: length,
            Buffer: PWSTR(buf.as_mut_ptr()),
        };
        let opened = unsafe { (self.open)(&object_attributes(&mut name, raw, None), 0, 0x20081) };
        if !opened.is_invalid() {
            let mut unexpected = Desktop(Some(opened));
            unexpected.close()?;
            anyhow::bail!("original private desktop still exists");
        }
        ensure!(
            windows::core::Error::from_thread().code() == ERROR_FILE_NOT_FOUND.to_hresult(),
            "desktop absence was not established"
        );
        receipt.stage = "close_station".into();
        self.station.close()?;
        receipt.station_closed = true;
        receipt.stage = "unchanged".into();
        receipt.original_associations_unchanged = unsafe {
            CompareObjectHandles(
                HANDLE(self.original.0),
                HANDLE(GetProcessWindowStation()?.0),
            )
            .as_bool()
                && CompareObjectHandles(
                    HANDLE(self.original_desktop.0),
                    HANDLE(GetThreadDesktop(GetCurrentThreadId())?.0),
                )
                .as_bool()
        };
        receipt.shared_descriptor_unchanged =
            security(HANDLE(self.original.0), ORIGINAL_INFO)?.0 == self.original_bytes;
        ensure!(
            receipt.original_associations_unchanged && receipt.shared_descriptor_unchanged,
            "original shared station/associations changed"
        );
        Ok(())
    }
}
struct Endpoint(OwnedHandle);
fn observe_failed_child(child: &SpawnedChild) -> ChildFailure {
    // Diagnostic data only, captured from original handles before the armed
    // child guard terminates anything. It cannot authorize resource retirement.
    let wait_status = unsafe { WaitForSingleObject(child.process(), 0) }.0;
    let mut process_code = 0;
    let process_error = unsafe { GetExitCodeProcess(child.process(), &mut process_code) }.err();
    let mut thread_code = 0;
    let thread_error = unsafe { GetExitCodeThread(child.thread(), &mut thread_code) }.err();
    ChildFailure {
        wait_status,
        process_code: process_error.is_none().then_some(process_code),
        process_query_hresult: process_error.map(|e| e.code().0),
        thread_code: thread_error.is_none().then_some(thread_code),
        thread_query_hresult: thread_error.map(|e| e.code().0),
    }
}
impl Endpoint {
    fn send<T: Serialize>(&self, value: &T) -> Result<()> {
        let mut bytes = encode(value)?;
        let end = deadline(TIMEOUT)?;
        io::transfer(
            self.0.raw(),
            &mut (bytes.len() as u32).to_le_bytes(),
            true,
            end,
        )?;
        io::transfer(self.0.raw(), &mut bytes, true, end)
    }
    fn receive<T: for<'de> Deserialize<'de>>(&self) -> Result<T> {
        self.receive_before(deadline(TIMEOUT)?)
    }
    fn receive_before<T: for<'de> Deserialize<'de>>(&self, end: Instant) -> Result<T> {
        decode(&self.receive_wire_before(end)?)
    }
    fn receive_wire_before(&self, end: Instant) -> Result<Vec<u8>> {
        let mut size = [0; 4];
        io::transfer(self.0.raw(), &mut size, false, end)?;
        let size = u32::from_le_bytes(size) as usize;
        ensure!(
            size > 0 && size <= LIMIT,
            "inherited station frame exceeds bound"
        );
        let mut bytes = vec![0; size];
        io::transfer(self.0.raw(), &mut bytes, false, end)?;
        Ok(bytes)
    }
}
fn endpoints(owner: &str) -> Result<(Endpoint, OwnedHandle)> {
    let address = ControlAddress::fresh()?;
    let sd = OwnedSd::from_sddl(&format!(
        "O:{owner}D:P(A;;GA;;;SY)(A;;GA;;;{owner})(A;;RC;;;OW)S:(ML;;NWNR;;;ME)"
    ))?;
    let sa = SECURITY_ATTRIBUTES {
        nLength: size_of::<SECURITY_ATTRIBUTES>() as u32,
        lpSecurityDescriptor: sd.ptr.0,
        bInheritHandle: false.into(),
    };
    let raw = unsafe {
        CreateNamedPipeW(
            pcwstr(&wstr(&address.path()?)),
            PIPE_ACCESS_DUPLEX | FILE_FLAG_FIRST_PIPE_INSTANCE | FILE_FLAG_OVERLAPPED,
            PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
            1,
            LIMIT as u32,
            LIMIT as u32,
            0,
            Some(&sa),
        )
    };
    if raw.is_invalid() {
        return Err(windows::core::Error::from_thread().into());
    }
    let server = OwnedHandle(raw);
    let client = OwnedHandle(unsafe {
        CreateFileW(
            pcwstr(&wstr(&address.path()?)),
            GENERIC_READ.0 | GENERIC_WRITE.0,
            FILE_SHARE_MODE(0),
            None,
            OPEN_EXISTING,
            FILE_FLAG_OVERLAPPED,
            None,
        )?
    });
    io::connect(server.raw(), deadline(TIMEOUT)?)?;
    noninheritable(server.raw())?;
    noninheritable(client.raw())?;
    let mut inherited = HANDLE::default();
    unsafe {
        DuplicateHandle(
            GetCurrentProcess(),
            client.raw(),
            GetCurrentProcess(),
            &mut inherited,
            0,
            true,
            DUPLICATE_SAME_ACCESS,
        )?;
    }
    Ok((Endpoint(server), OwnedHandle(inherited)))
}
#[derive(Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct ChildRequest {
    request: Request,
    pid: u32,
    created: u64,
    authentication_id: [u32; 2],
    station: String,
    native_station: String,
    desktop: String,
    desktop_descriptor: String,
    descriptor: String,
    filesystem: Option<FixedLowboxFilesystemSubject>,
    family_clients: Option<[u64; 2]>,
}
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct ChildReport {
    subject: ChildRequest,
    proof: ChildProof,
    desktop_proof: Option<desktop::DesktopProof>,
    schannel_proof: Option<schannel::Proof>,
    rpc_proof: Option<rpc_boundary::Proof>,
    https_proof: Option<https::Proof>,
    filesystem_proof: Option<FixedLowboxFilesystemProof>,
    family_completed: bool,
    family_root_hresult: Option<i32>,
    family_root_stage: Option<family::RootStage>,
}
impl ChildRequest {
    fn require(&self) -> Result<()> {
        let binding = binding(&self.request)?;
        ensure!(
            self.filesystem.is_some() == binding.is_v3_shadow(),
            "fixed filesystem selection differs"
        );
        if let Some(subject) = &self.filesystem {
            subject.require_binding(binding.profile.folder, &binding.profile.use_nonce)?;
        }
        ensure!(
            self.family_clients.is_some() == binding.is_v3_shadow(),
            "fixed family selection differs"
        );
        if let Some(clients) = self.family_clients {
            ensure!(
                clients[0] != clients[1]
                    && clients.iter().all(|v| *v > 0 && *v < isize::MAX as u64),
                "fixed family client handles differ"
            );
        }
        require_reference(&self.request, (1, self.pid, self.created))?;
        ensure!(
            !self.station.is_empty()
                && self.station.len() <= 512
                && !self.station.contains(['\\', '/'])
                && !self.station.chars().any(char::is_control)
                && self.native_station
                    == format!(
                        "\\Sessions\\{}\\Windows\\WindowStations\\{}",
                        self.request.birth.selection.session, self.station
                    )
                && self.desktop == format!("alysis-station-lowbox-{}", self.request.birth.nonce)
                && !self.descriptor.is_empty()
                && self.descriptor.len() <= 8192
                && !self.descriptor.contains('\0'),
            "lowbox fixed request differs"
        );
        ensure!(
            !self.desktop_descriptor.is_empty()
                && self.desktop_descriptor.len() <= 8192
                && !self.desktop_descriptor.contains('\0'),
            "original desktop descriptor exceeds bounds"
        );
        Ok(())
    }
}
pub(super) fn run(
    channel: &mut ControlChannel<'_>,
    request: &Request,
    image: &AccountImageObserver,
    auth: [u32; 2],
    receipt: &mut Receipt,
) -> Result<()> {
    let b = binding(request)?;
    let v3 = b.is_v3_shadow();
    if v3 {
        ensure!(b.rpc_policy()? == crate::account_state::execution::resources::FixtureRpcPolicy::SystemNsiReadInheritanceV3
            && HTTPS_COMPARISON && b.v3_mode() == Some(request.birth.selection.account.mode),
            "bounded V3 source selection differs");
    } else {
        ensure!(b.rpc_policy()? == crate::account_state::execution::resources::FixtureRpcPolicy::selected_fixture_policy()?,
            "station RPC binding differs from source-selected diagnostic");
    }
    ensure!(
        v3 || request.birth.selection.account.mode == fixture_mode(),
        "station account differs from source-selected mode"
    );
    ensure!(
        !HTTPS_COMPARISON
            || ((matches!(b.policy, 3 | 5) || v3)
                && !CAPTURED_NAMESPACE_COMPARISON
                && !ATTRIBUTE_BIRTH_COMPARISON
                && !DOCUMENTED_DESKTOP_COMPARISON
                && !INHERITED_DESKTOP_SELECTION),
        "HTTPS fixture requires sole original manual SYSTEM inheritance policy"
    );
    // Open before child birth; this live pin survives until that original child
    // exits. It is never placed in the explicit inherited-handle list.
    let rpc_pin = rpc_boundary::Pin::open(rpc_subject(request)?)?;
    let kernelbase_comparison = b.policy == 2;
    receipt.rpc_system_inheritance = b.policy == 3;
    receipt.rpc_nsi_system_inheritance = b.policy == 5;
    receipt.v3_shadow_bounded = v3;
    receipt.stage = "profile_root".into();
    ensure!(
        !((kernelbase_comparison
            || receipt.rpc_system_inheritance
            || receipt.rpc_nsi_system_inheritance
            || v3)
            && (CAPTURED_NAMESPACE_COMPARISON
                || ATTRIBUTE_BIRTH_COMPARISON
                || DOCUMENTED_DESKTOP_COMPARISON
                || INHERITED_DESKTOP_SELECTION)),
        "token comparisons cannot be combined"
    );
    crate::account_profile_registry::require_loaded_actor_classes(
        &request.birth.selection.account.account_sid,
    )?;
    let filesystem = crate::account_profile_filesystem::RetainedProfileFilesystem::open(
        &b.profile.container,
        &request.birth.selection.account.account_sid,
    )?;
    ensure!(
        filesystem.original_root_id()? == b.profile.folder,
        "station probe SDK folder differs from original profile owner"
    );
    let root = filesystem.retain_root_for_use()?;
    receipt.original_folder_verified = true;
    let filesystem_probe = if v3 {
        receipt.stage = "filesystem_prepare".into();
        let prepared = root.prepare_fixed_lowbox(
            &b.profile.container,
            &request.birth.selection.account.account_sid,
            b.profile.folder,
            &b.profile.use_nonce,
        )?;
        receipt.filesystem_subject = Some(prepared.subject());
        Some(prepared)
    } else {
        None
    };
    let mut family_probe = if v3 {
        Some(family::Prepared::new(
            &request.birth.selection.account.account_sid,
        )?)
    } else {
        None
    };
    let family_clients = family_probe
        .as_ref()
        .map(|p| p.inherited_values())
        .transpose()?;
    let app = std::env::current_exe()?;
    let (debug_loader, filtered_loader) = loader_targets(
        app.file_name()
            .and_then(|n| n.to_str())
            .context("fixed image name invalid")?,
    )?;
    // This guard is declared before every child/process/token/endpoint guard.
    // Any error after native birth must drop the armed child before USER roots.
    let mut users = UserPair::create(request, receipt)?;
    receipt.stage = "create_lowbox".into();
    let (restricted_base, token) = if CAPTURED_NAMESPACE_COMPARISON {
        receipt.stage = "captured_namespace_token".into();
        receipt.captured_namespace_birth = true;
        let (base, token, proof) = crate::account_token::capture::prepare(
            &b.profile.container,
            &request.birth.selection.account.account_sid,
            &request.birth.selection.account.owner_sid,
            &mut receipt.stage,
        )?;
        receipt.captured_namespace = Some(proof);
        (base, token)
    } else {
        crate::account_token::prepare_birth_comparison(
            &b.profile.container,
            &request.birth.selection.account.account_sid,
            &request.birth.selection.account.owner_sid,
            network(request),
        )?
    };
    let token = if kernelbase_comparison {
        receipt.stage = "kernelbase_token".into();
        receipt.kernelbase_token_birth = true;
        receipt.constructor_directories =
            constructor_directories(&b.profile.container, request.birth.selection.session)?;
        receipt.constructor_directory_upgrades =
            constructor_directory_upgrades(&b.profile.container)?;
        receipt.constructor_links = constructor_links(&b.profile.container)?;
        let constructed = crate::account_token::compare_kernelbase_lowbox(
            &restricted_base,
            &b.profile.container,
            &request.birth.selection.account.account_sid,
            &mut receipt.kernelbase_constructor_returned,
        )?;
        // Constructor setup may operate on these objects. Refuse child birth
        // unless both original private descriptors are byte-shape unchanged.
        rpc_pin.verify()?;
        receipt.constructor_rpc_unchanged_before_birth = true;
        constructed
    } else {
        token
    };
    receipt.stage = "token_semantics".into();
    receipt.token_observation = Some(token_observation::Comparison::observe(
        restricted_base.raw(),
        token.raw(),
    )?);
    receipt.user_access = Some(user_access::Probe::observe(&users, &token)?);
    receipt.stage = "prepare_inherited_user".into();
    // Normal launch selects the explicit private pair by name. Do not even
    // create inheritable USER copies outside the fixed loader experiment.
    let mut inherited_users = if debug_loader {
        let pair = inherited_user::Pair::prepare(&users, &token)?;
        receipt.inherited_user = Some(pair.proof().clone());
        Some(pair)
    } else {
        None
    };
    let (endpoint, inherited) = endpoints(&request.birth.selection.account.account_sid)?;
    // The disposable wrapper pins a fresh run-specific image and IFEO key.
    // Only this test-only fixed-purpose route can inspect that diagnostic image.
    let app = app.to_str().context("fixed lowbox image not Unicode")?;
    let mut command = wstr(&format!(
        "{} --exact {TEST} --ignored --nocapture --test-threads=1",
        crate::launch::quote_arg(app)
    ));
    let directory = bootstrap::os_directory(false)?;
    let windows = bootstrap::os_directory(true)?;
    // Resolve profile locations under the verified raw account's primary
    // identity and its already loaded profile. Windows uses these during
    // AppContainer birth. Never inherit the broker's environment or secrets.
    use crate::account_state::execution::bootstrap_plan::initializer_image::known_profile_folder;
    use windows::Win32::UI::Shell::{
        FOLDERID_LocalAppData, FOLDERID_Profile, FOLDERID_RoamingAppData,
    };
    let mut environment = child_environment(
        windows,
        directory.clone(),
        known_profile_folder(&FOLDERID_LocalAppData)?,
        known_profile_folder(&FOLDERID_Profile)?,
        known_profile_folder(&FOLDERID_RoamingAppData)?,
        inherited.raw().0 as usize,
    )?;
    // Fixed loader diagnostic only: compare documented AppContainer process
    // birth with the manual-lowbox baseline. SID storage outlives capabilities,
    // which in turn outlive the native attribute list on every unwind path.
    // Keep the manual token alive for the existing namespace and USER probes.
    let package = crate::appcontainer::derive(&b.profile.container)?;
    let capabilities = SECURITY_CAPABILITIES {
        AppContainerSid: package.raw(),
        ..Default::default() // zero capability SIDs: network remains off
    };
    let attribute_birth = debug_loader && ATTRIBUTE_BIRTH_COMPARISON;
    let mut attrs = ProcThreadAttrs::new(if attribute_birth { 2 } else { 1 })?;
    if attribute_birth {
        unsafe { attrs.set_security_capabilities(&capabilities)? };
        receipt.security_capabilities_birth = true;
    }
    let omit_users = attribute_birth && ATTRIBUTE_WITHOUT_USER_HANDLES;
    noninheritable(HANDLE(users.station.raw()?.0))?;
    noninheritable(HANDLE(
        users.desktop.0.context("original desktop closed")?.0,
    ))?;
    if let Some(pair) = inherited_users.as_mut().filter(|_| omit_users) {
        // Close the inheritable USER copies BEFORE birth, not merely remove
        // them from the explicit list. Full originals were verified private.
        pair.close()?;
        receipt.inherited_user = Some(pair.proof().clone());
        receipt.appcontainer_without_inherited_user = true;
        attrs.set_handle_list(&[inherited.raw()])?;
    } else if let Some(pair) = &inherited_users {
        let [station_handle, desktop_handle] = pair.handles()?;
        attrs.set_handle_list(&[inherited.raw(), station_handle, desktop_handle])?;
    } else {
        let mut handles = vec![inherited.raw()];
        if let Some(prepared) = &family_probe {
            handles.extend(prepared.inherited_handles()?);
        }
        attrs.set_handle_list(&handles)?;
    }
    let mut desktop = wstr(&format!("{}\\{}", users.name, users.desktop_name));
    let mut startup = STARTUPINFOEXW::default();
    startup.StartupInfo.cb = size_of::<STARTUPINFOEXW>() as u32;
    if debug_loader && INHERITED_DESKTOP_SELECTION {
        // Both limited original USER handles remain on the exact explicit
        // handle list and are verified in the suspended child before resume.
        // Do not infer its actual association from this startup selection.
        receipt.inherited_desktop_selection = true;
        startup.StartupInfo.lpDesktop = PWSTR::null();
    } else {
        startup.StartupInfo.lpDesktop = PWSTR(desktop.as_mut_ptr());
    }
    startup.lpAttributeList = attrs.list();
    let mut info = PROCESS_INFORMATION::default();
    image.verify_current()?;
    let mut in_job = windows::core::BOOL::default();
    unsafe {
        IsProcessInJob(GetCurrentProcess(), None, &mut in_job)?;
    }
    ensure!(in_job.as_bool(), "lowbox parent is outside original job");
    let _headless_errors = HeadlessErrors::enter(receipt)?;
    receipt.stage = "create_child".into();
    if debug_loader {
        checkpoint(channel, receipt)?;
    }
    type LastStatus = unsafe extern "system" fn() -> NTSTATUS;
    let last_status: LastStatus = unsafe {
        std::mem::transmute(
            GetProcAddress(GetModuleHandleW(w!("ntdll.dll"))?, s!("RtlGetLastNtStatus"))
                .context("creation status getter missing")?,
        )
    };
    let status_before = unsafe { last_status().0 };
    let created = unsafe {
        CreateProcessAsUserW(
            Some(if attribute_birth {
                restricted_base.raw()
            } else {
                token.raw()
            }),
            pcwstr(&wstr(app)),
            Some(PWSTR(command.as_mut_ptr())),
            None,
            None,
            true,
            CREATE_SUSPENDED
                | CREATE_NO_WINDOW
                | CREATE_UNICODE_ENVIRONMENT
                | EXTENDED_STARTUPINFO_PRESENT
                | if debug_loader {
                    DEBUG_ONLY_THIS_PROCESS
                } else {
                    PROCESS_CREATION_FLAGS(0)
                },
            Some(environment.as_mut_ptr().cast()),
            pcwstr(&wstr(&directory)),
            &startup.StartupInfo,
            &mut info,
        )
    };
    if let Err(error) = created {
        // A post-wrapper status is context, not established syscall causality.
        receipt.creation_failure_ntstatus = Some([status_before, unsafe { last_status().0 }]);
        return Err(error.into());
    }
    let child = SpawnedChild::new(info); // stays armed through every failure and confirmed exit
    if let Some(prepared) = &mut family_probe {
        prepared.close_clients_after_birth()?;
    }
    let debug = if debug_loader {
        Some(loader::Guard::new(
            &child,
            users
                .desktop
                .0
                .as_ref()
                .context("original private desktop closed")?,
        )?)
    } else {
        None
    };
    drop(inherited);
    receipt.stage = "child_inherited_user".into();
    if let Some(pair) = inherited_users.as_mut().filter(|_| !omit_users) {
        let received = pair.verify_child(child.process());
        receipt.inherited_user = Some(pair.proof().clone());
        received?;
        pair.close()?;
        receipt.inherited_user = Some(pair.proof().clone());
    }
    require_lowbox(child.process(), request, auth)?;
    image.verify_process(child.process())?;
    let reference = (
        child.process().0 as usize as u64,
        unsafe { GetProcessId(child.process()) },
        bootstrap::pull::creation_time(child.process())?,
    );
    require_reference(request, reference)?;
    receipt.original_child = Some((reference.1, reference.2));
    // Observe the actual child token. In the attribute path Windows creates a
    // different token object from the same filtered base; the supplied manual
    // lowbox cannot stand in for the child's self-access observation.
    let mut actual_token = HANDLE::default();
    unsafe {
        OpenProcessToken(
            child.process(),
            TOKEN_QUERY | TOKEN_DUPLICATE | TOKEN_ACCESS_MASK(READ_CONTROL.0),
            &mut actual_token,
        )?;
    }
    let actual_token = OwnedHandle(actual_token);
    noninheritable(actual_token.raw())?;
    receipt
        .token_observation
        .as_mut()
        .context("token comparison absent")?
        .child(actual_token.raw(), (reference.1, reference.2))?;
    receipt.startup = Some(startup::Probe::observe(
        child.process(),
        &actual_token,
        (reference.1, reference.2),
    )?);
    drop(actual_token);
    receipt.stage = "offer".into();
    channel.send(
        &encode(&Frame::Born {
            request: request.clone(),
            reference,
            authentication_id: auth,
        })?,
        TIMEOUT,
    )?;
    ensure!(
        channel.receive(TIMEOUT)? == ack(request, "resume")?,
        "original lowbox observation acknowledgment differs"
    );
    require_lowbox(child.process(), request, auth)?;
    image.verify_process(child.process())?;
    receipt.stage = "resume".into();
    ensure!(
        unsafe { ResumeThread(child.thread()) } == 1,
        "lowbox original thread was not singly suspended"
    );
    if let Some(mut debug) = debug {
        receipt.stage = "loader_observation".into();
        receipt.loader = Some(loader::LoaderTrace::new());
        let observed = debug.observe(receipt.loader.as_mut().unwrap());
        // Preserve the bounded trace before detachment/armed-child cleanup,
        // whose failure must not erase the reason Windows refused startup.
        checkpoint(channel, receipt)?;
        drop(debug); // detach before original armed child/resource cleanup
        receipt.child_failure = Some(observe_failed_child(&child));
        observed?;
        anyhow::bail!(
            "loader observation is diagnostic only; normal containment gate remains failed"
        );
    }
    drop(debug); // release the empty diagnostic slot before normal child retirement
    let subject = ChildRequest {
        request: request.clone(),
        pid: reference.1,
        created: reference.2,
        authentication_id: auth,
        station: users.name.clone(),
        native_station: user_access::original_name(HANDLE(users.station.raw()?.0))?,
        desktop: users.desktop_name.clone(),
        desktop_descriptor: receipt
            .desktop_before
            .clone()
            .context("original desktop policy absent")?,
        descriptor: receipt.station_before.clone().unwrap(),
        filesystem: receipt.filesystem_subject.clone(),
        family_clients,
    };
    subject.require()?;
    endpoint.send(&subject)?;
    // Keep the HTTPS wait within the original control protocol's 10s bound.
    // Frames cannot renew it; original job drain still governs timeout cleanup.
    let report_deadline = deadline(child_timeout())?;
    receipt.stage = "child_started".into();
    let started: Vec<u8> = match endpoint.receive_before(report_deadline) {
        Ok(marker) => marker,
        Err(error) => {
            receipt.child_failure = Some(observe_failed_child(&child));
            return Err(error);
        }
    };
    if started != STARTED {
        receipt.child_failure = Some(observe_failed_child(&child));
        anyhow::bail!("fixed child startup marker differs");
    }
    receipt.child_started = true;
    if let Some(prepared) = family_probe {
        receipt.stage = "family".into();
        checkpoint(channel, receipt)?;
        receipt.family = Some(prepared.run_parent(
            child.process(),
            &endpoint,
            &subject,
            image,
            report_deadline,
            |depth, reference| {
                let remaining = family_remaining(report_deadline)?;
                channel.send(
                    &encode(&Frame::FamilyBorn {
                        request: request.clone(),
                        depth,
                        reference,
                        authentication_id: auth,
                    })?,
                    remaining,
                )?;
                ensure!(
                    channel.receive(family_remaining(report_deadline)?)?
                        == family_ack(request, depth)?,
                    "original descendant broker acknowledgment differs"
                );
                Ok(())
            },
        )?);
    }
    receipt.stage = "child_receipt".into();
    let bytes = match endpoint.receive_wire_before(report_deadline) {
        Ok(bytes) => bytes,
        Err(error) => {
            receipt.child_failure = Some(observe_failed_child(&child));
            return Err(error);
        }
    };
    receipt.stage = "child_decode".into();
    let report: ChildReport = match decode(&bytes) {
        Ok(report) => report,
        Err(error) => {
            receipt.child_failure = Some(observe_failed_child(&child));
            return Err(error);
        }
    };
    receipt.stage = "child_subject".into();
    if report.subject != subject {
        receipt.child_failure = Some(observe_failed_child(&child));
        anyhow::bail!("lowbox report differs from exact original subject");
    }
    receipt.stage = "child_proof".into();
    let passed = match record_child_proofs(
        receipt,
        &subject,
        report.proof,
        report.desktop_proof,
        report.schannel_proof,
        report.rpc_proof,
        report.https_proof,
        report.filesystem_proof,
        report.family_completed,
        report.family_root_hresult,
        report.family_root_stage,
    ) {
        Ok(passed) => passed,
        Err(error) => {
            receipt.child_failure = Some(observe_failed_child(&child));
            return Err(error);
        }
    };
    endpoint.send(&ack(request, "finished")?)?;
    receipt.stage = "child_exit".into();
    let expected = if passed { EXIT } else { EXIT + 1 };
    exited(child.process(), (reference.1, reference.2), expected)?;
    receipt.original_child_exit = Some(expected);
    drop(child); // original child/thread close precedes desktop/station close
    rpc_pin.close()?;
    receipt.rpc_original_closed_unchanged = true;
    if let Some(prepared) = filesystem_probe {
        receipt.stage = "filesystem_verify".into();
        if passed {
            prepared.verify_and_close(
                receipt
                    .filesystem_child
                    .as_ref()
                    .context("filesystem child proof missing")?,
            )?;
            receipt.filesystem_parent_verified = true;
        }
    }
    // One sealed diagnostic child, never a workload or fallback. Preserve all
    // original lowbox evidence and drain this child before closing USER roots.
    if b.policy == 1
        && receipt.child.as_ref().is_some_and(|p| p.passed)
        && receipt.desktop_child.as_ref().is_some_and(|p| p.passed)
    {
        receipt.stage = "filtered_primary_control".into();
        if filtered_loader {
            // Preserve original lowbox proofs before debug-handle custody can
            // fail closed. The original USER pair is still open at this point.
            checkpoint(channel, receipt)?;
        }
        receipt.filtered_primary_control = Some(filtered_primary::observe(
            &subject,
            receipt.token_stamp,
            &restricted_base,
            image,
            &users,
        ));
    }
    root.unchanged()?;
    users.finish(receipt)?;
    // Run the identical positive control only AFTER the measured lowbox has
    // exited and both original USER objects are closed. Do not warm TLS/LSA
    // state before the lowbox observation or accept raw-account success as a
    // substitute for the lowbox result. The original raw token is rechecked
    // by the authenticated client before serializing this terminal receipt.
    receipt.stage = "raw_schannel_control".into();
    receipt.raw_schannel_control = Some(schannel::probe()?);
    if HTTPS_COMPARISON
        && receipt.https_child.is_some()
        && receipt
            .raw_schannel_control
            .as_ref()
            .is_some_and(|p| p.passed)
    {
        receipt.stage = "raw_https_control".into();
        receipt.raw_https_control = Some(https::observe());
    }
    receipt.stage = "child_checks".into();
    ensure!(
        !HTTPS_COMPARISON
            || receipt
                .raw_https_control
                .as_ref()
                .is_some_and(|p| p.succeeded() && p.socket_connected()),
        "fixed public HTTPS or numeric TCP positive control absent or failed"
    );
    ensure!(
        receipt.filtered_primary_control.is_none(),
        "filtered-primary Schannel comparison is diagnostic only"
    );
    ensure!(
        receipt.inherited_user.is_none(),
        "inherited USER startup experiment is diagnostic only"
    );
    ensure!(
        passed
            && receipt
                .raw_schannel_control
                .as_ref()
                .is_some_and(|p| p.passed),
        "actual lowbox USER boundary or Schannel credential check failed"
    );
    ensure!(
        (b.policy == 1 || v3)
            && !receipt.kernelbase_token_birth
            && !receipt.captured_namespace_birth,
        "RPC policy or token construction comparison is diagnostic only"
    );
    receipt.stage = "complete".into();
    receipt.passed = true;
    Ok(())
}
fn record_child_proofs(
    receipt: &mut Receipt,
    subject: &ChildRequest,
    station: ChildProof,
    desktop: Option<desktop::DesktopProof>,
    schannel: Option<schannel::Proof>,
    rpc: Option<rpc_boundary::Proof>,
    https: Option<https::Proof>,
    filesystem: Option<FixedLowboxFilesystemProof>,
    family_completed: bool,
    family_root_hresult: Option<i32>,
    family_root_stage: Option<family::RootStage>,
) -> Result<bool> {
    station.require()?;
    let station_passed = station.passed;
    receipt.child = Some(station);
    // A rejected desktop report must not contaminate the terminal receipt and
    // prevent the parent from returning already validated station evidence.
    if let Some(proof) = &desktop {
        proof.require_subject(
            &subject.desktop,
            &subject.desktop_descriptor,
            &subject.request.birth.selection.account.account_sid,
        )?;
    }
    let passed = station_passed && desktop.as_ref().is_some_and(|p| p.passed);
    receipt.desktop_child = desktop;
    if let Some(proof) = &schannel {
        proof.require()?;
    }
    let passed = passed && schannel.as_ref().is_some_and(|p| p.passed);
    receipt.schannel_child = schannel;
    if let Some(proof) = &rpc {
        proof.require(&rpc_subject(&subject.request)?)?;
    }
    let passed = passed && rpc.as_ref().is_some_and(|p| p.passed);
    receipt.rpc_child = rpc;
    ensure!(
        https.is_some() == (HTTPS_COMPARISON && passed),
        "HTTPS observation missing or precedes original checks"
    );
    let passed = if let Some(proof) = &https {
        proof.require()?;
        passed && proof.network_qualified(network(&subject.request))
    } else {
        passed
    };
    receipt.https_child = https;
    ensure!(
        filesystem.is_some() == (subject.filesystem.is_some() && passed),
        "filesystem proof missing or precedes boundary and network checks"
    );
    if let Some(proof) = &filesystem {
        ensure!(
            receipt.filesystem_subject == subject.filesystem,
            "filesystem original subject differs"
        );
        proof.require_subject(
            subject
                .filesystem
                .as_ref()
                .context("filesystem subject absent")?,
        )?;
    }
    let passed = passed && filesystem.as_ref().is_none_or(|p| p.passed);
    receipt.filesystem_child = filesystem;
    ensure!(
        family_completed == receipt.family.as_ref().is_some_and(|p| p.passed),
        "original lowbox family completion differs from raw parent observation"
    );
    let passed = passed && (subject.family_clients.is_none() || family_completed);
    ensure!(
        family_root_stage.is_some() == family_root_hresult.is_some()
            && family_root_hresult.is_none_or(|code| code < 0
                && subject.family_clients.is_some()
                && !family_completed),
        "family root failure differs"
    );
    receipt.family_root_hresult = family_root_hresult;
    receipt.family_root_stage = family_root_stage;
    Ok(passed)
}
// A fixed native open reaches the private station's ACL directly. Win32's
// parent-directory open can fail first and cannot prove this station's denial.
// The bounded absolute path comes from the parent's retained original handle,
// and the child compares it with its actual attached object before any open.
fn open_subject_station(subject: &ChildRequest, access: u32) -> Result<HWINSTA> {
    subject.require()?;
    let module = unsafe { GetModuleHandleW(w!("win32u.dll"))? };
    type OpenStation = unsafe extern "system" fn(*const OBJECT_ATTRIBUTES, u32) -> HWINSTA;
    let open: OpenStation = unsafe {
        std::mem::transmute(
            GetProcAddress(module, s!("NtUserOpenWindowStation"))
                .context("native station open missing")?,
        )
    };
    let (mut buffer, length) = crate::util::counted_utf16(&subject.native_station);
    let mut name = UNICODE_STRING {
        Length: length,
        MaximumLength: length,
        Buffer: PWSTR(buffer.as_mut_ptr()),
    };
    let attrs = object_attributes(&mut name, HWINSTA::default(), None);
    let handle = unsafe { open(&attrs, access) };
    if handle.is_invalid() {
        return Err(windows::core::Error::from_thread().into());
    }
    Ok(handle)
}
fn probe(subject: &ChildRequest, endpoint: &Endpoint) -> Result<ChildProof> {
    subject.require()?;
    let mut proof = ChildProof::new();
    let result = (|| -> Result<()> {
        crate::account_impersonation::require_process_identity()?;
        let current = ExpectedPeer::retain(unsafe { GetCurrentProcess() })?;
        require_lowbox(
            current.process.raw(),
            &subject.request,
            subject.authentication_id,
        )?;
        ensure!(
            current.pid == subject.pid
                && bootstrap::pull::creation_time(current.process.raw())? == subject.created,
            "lowbox current identity differs"
        );
        let mut server = 0;
        unsafe {
            GetNamedPipeServerProcessId(endpoint.0.raw(), &mut server)?;
        }
        ensure!(
            server == subject.request.pid,
            "inherited station endpoint original parent differs"
        );
        // Borrow the system-established connection reference. It may have
        // different granted access from a deliberately limited fresh open;
        // both paths must deny permission mutation. Never close this handle.
        proof.stage = "attached".into();
        let attached = unsafe { GetProcessWindowStation()? };
        noninheritable(HANDLE(attached.0))?;
        ensure!(
            text(HANDLE(attached.0), UOI_NAME)? == subject.station
                && text(
                    HANDLE(unsafe { GetThreadDesktop(GetCurrentThreadId())? }.0),
                    UOI_NAME
                )? == subject.desktop,
            "lowbox attached USER pair differs"
        );
        ensure!(
            user_access::original_name(HANDLE(attached.0))? == subject.native_station,
            "attached station differs from retained original native name"
        );
        proof.native_name_verified = true;
        proof.stage = "allowed_open".into();
        let mut allowed = Station(Some(open_subject_station(subject, 0x20002)?));
        noninheritable(HANDLE(allowed.raw()?.0))?;
        ensure!(
            user_access::granted(HANDLE(allowed.raw()?.0))? == 0x20002,
            "fresh private station grant differs"
        );
        proof.attached_same_object =
            unsafe { CompareObjectHandles(HANDLE(attached.0), HANDLE(allowed.raw()?.0)).as_bool() };
        ensure!(
            proof.attached_same_object,
            "attached station differs from fresh original open"
        );
        ensure!(
            security(HANDLE(allowed.raw()?.0), INFO)?.1 == subject.descriptor,
            "fresh allowed station descriptor differs"
        );
        proof.positive_open = true;
        proof.stage = "denied_open".into();
        for rights in [0x40000, 0x80000, 0xc0000] {
            let result = open_subject_station(subject, rights);
            let code = match result {
                Ok(h) => {
                    let mut unexpected = Station(Some(h));
                    unexpected.close()?;
                    0
                }
                Err(e) => native_code(&e).context("native station failure has no status")?,
            };
            proof.open_denials.push((rights, code));
            ensure!(
                code == ERROR_ACCESS_DENIED.to_hresult().0,
                "fresh station mutation open was not access-denied"
            );
        }
        let owner = &subject.request.birth.selection.account.account_sid;
        let mutation = OwnedSd::from_sddl(&format!("O:{owner}D:P(A;;0xf037f;;;SY)"))?;
        proof.stage = "set_user_dacl".into();
        let code = unsafe {
            SetUserObjectSecurity(
                HANDLE(allowed.raw()?.0),
                &DACL_SECURITY_INFORMATION,
                mutation.ptr,
            )
        }
        .err()
        .map_or(0, |e| e.code().0);
        proof.set_user_dacl = Some(code);
        ensure!(
            code == ERROR_ACCESS_DENIED.to_hresult().0,
            "USER DACL mutation was not access-denied"
        );
        proof.stage = "set_user_owner".into();
        let code = unsafe {
            SetUserObjectSecurity(
                HANDLE(allowed.raw()?.0),
                &OWNER_SECURITY_INFORMATION,
                mutation.ptr,
            )
        }
        .err()
        .map_or(0, |e| e.code().0);
        proof.set_user_owner = Some(code);
        ensure!(
            code == ERROR_ACCESS_DENIED.to_hresult().0,
            "USER owner mutation was not access-denied"
        );
        proof.stage = "nt_set_dacl".into();
        let code = unsafe {
            NtSetSecurityObject(
                HANDLE(allowed.raw()?.0),
                DACL_SECURITY_INFORMATION.0,
                mutation.ptr,
            )
        }
        .0;
        proof.nt_set_dacl = Some(code);
        ensure!(
            code == 0xc0000022u32 as i32,
            "native station DACL mutation was not access-denied"
        );
        proof.stage = "nt_set_owner".into();
        let code = unsafe {
            NtSetSecurityObject(
                HANDLE(allowed.raw()?.0),
                OWNER_SECURITY_INFORMATION.0,
                mutation.ptr,
            )
        }
        .0;
        proof.nt_set_owner = Some(code);
        ensure!(
            code == 0xc0000022u32 as i32,
            "native station owner mutation was not access-denied"
        );
        proof.stage = "attached_set_user_dacl".into();
        let code = unsafe {
            SetUserObjectSecurity(HANDLE(attached.0), &DACL_SECURITY_INFORMATION, mutation.ptr)
        }
        .err()
        .map_or(0, |e| e.code().0);
        proof.attached_set_user_dacl = Some(code);
        ensure!(
            code == ERROR_ACCESS_DENIED.to_hresult().0,
            "attached USER DACL mutation was not access-denied"
        );
        proof.stage = "attached_set_user_owner".into();
        let code = unsafe {
            SetUserObjectSecurity(
                HANDLE(attached.0),
                &OWNER_SECURITY_INFORMATION,
                mutation.ptr,
            )
        }
        .err()
        .map_or(0, |e| e.code().0);
        proof.attached_set_user_owner = Some(code);
        ensure!(
            code == ERROR_ACCESS_DENIED.to_hresult().0,
            "attached USER owner mutation was not access-denied"
        );
        proof.stage = "attached_nt_set_dacl".into();
        let code = unsafe {
            NtSetSecurityObject(
                HANDLE(attached.0),
                DACL_SECURITY_INFORMATION.0,
                mutation.ptr,
            )
        }
        .0;
        proof.attached_nt_set_dacl = Some(code);
        ensure!(
            code == 0xc0000022u32 as i32,
            "attached native station DACL mutation was not access-denied"
        );
        proof.stage = "attached_nt_set_owner".into();
        let code = unsafe {
            NtSetSecurityObject(
                HANDLE(attached.0),
                OWNER_SECURITY_INFORMATION.0,
                mutation.ptr,
            )
        }
        .0;
        proof.attached_nt_set_owner = Some(code);
        ensure!(
            code == 0xc0000022u32 as i32,
            "attached native station owner mutation was not access-denied"
        );
        allowed.close()?;
        Ok(())
    })();
    match result {
        Ok(()) => {
            proof.passed = true;
            proof.stage = "complete".into();
        }
        Err(e) => proof.hresult = native_code(&e),
    }
    proof.require()?;
    Ok(proof)
}
#[test]
#[ignore = "fixed inherited lowbox endpoint only; never a standalone launcher"]
fn station_lowbox_child() {
    unsafe {
        OutputDebugStringW(w!("ALYSIS_STATION_LOWBOX_MAIN_ENTERED"));
    }
    // Reserved fixed failure codes make pre-report startup visible through
    // the parent's original process/thread handles without logging secrets.
    let mut startup_code = 0x4170;
    let result = (|| -> Result<bool> {
        ensure!(
            std::env::args().skip(1).collect::<Vec<_>>()
                == [
                    "--exact",
                    TEST,
                    "--ignored",
                    "--nocapture",
                    "--test-threads=1"
                ],
            "lowbox arguments differ"
        );
        startup_code = 0x4171;
        let value = std::env::var(ENV)?;
        ensure!(
            value.len() <= 20 && value.bytes().all(|b| b.is_ascii_digit()),
            "lowbox inherited endpoint malformed"
        );
        let value: usize = value.parse()?;
        ensure!(
            value > 0 && value < isize::MAX as usize,
            "lowbox inherited endpoint invalid"
        );
        startup_code = 0x4172;
        let inherited = OwnedHandle(HANDLE(value as *mut _));
        let mut flags = 0;
        unsafe {
            GetHandleInformation(inherited.raw(), &mut flags)?;
        }
        ensure!(
            flags & HANDLE_FLAG_INHERIT.0 != 0
                && unsafe { GetFileType(inherited.raw()) } == FILE_TYPE_PIPE,
            "lowbox endpoint was not inherited pipe"
        );
        startup_code = 0x4173;
        let mut raw = HANDLE::default();
        unsafe {
            DuplicateHandle(
                GetCurrentProcess(),
                inherited.raw(),
                GetCurrentProcess(),
                &mut raw,
                0,
                false,
                DUPLICATE_SAME_ACCESS,
            )?;
        }
        let endpoint = Endpoint(OwnedHandle(raw));
        drop(inherited);
        noninheritable(raw)?;
        ensure!(
            unsafe { GetErrorMode() } & 3 == 3,
            "headless error mode not inherited"
        );
        startup_code = 0x4178;
        endpoint.send(&STARTED)?;
        startup_code = 0x4174;
        let request: ChildRequest = endpoint.receive()?;
        startup_code = 0x4175;
        let proof = probe(&request, &endpoint)?;
        let desktop_proof = if proof.passed {
            Some(desktop::probe(
                &request.desktop,
                &request.desktop_descriptor,
                &request.request.birth.selection.account.account_sid,
            )?)
        } else {
            None
        };
        let passed = proof.passed && desktop_proof.as_ref().is_some_and(|p| p.passed);
        let rpc_proof = if passed {
            Some(rpc_boundary::probe(rpc_subject(&request.request)?)?)
        } else {
            None
        };
        let schannel_proof = if passed {
            Some(schannel::probe_child()?)
        } else {
            None
        };
        let passed = passed && schannel_proof.as_ref().is_some_and(|p| p.passed);
        let passed = passed && rpc_proof.as_ref().is_some_and(|p| p.passed);
        let https_proof = if passed && HTTPS_COMPARISON {
            let observed = https::observe();
            require_lowbox(
                unsafe { GetCurrentProcess() },
                &request.request,
                request.authentication_id,
            )?;
            Some(observed)
        } else {
            None
        };
        let passed = passed
            && https_proof
                .as_ref()
                .is_none_or(|p| p.network_qualified(network(&request.request)));
        let filesystem_proof = if passed {
            request
                .filesystem
                .as_ref()
                .map(|subject| {
                    require_lowbox(
                        unsafe { GetCurrentProcess() },
                        &request.request,
                        request.authentication_id,
                    )?;
                    let b = binding(&request.request)?;
                    let proof = crate::account_profile_filesystem::observe_fixed_lowbox_filesystem(
                        subject,
                        &b.profile.container,
                        &request.request.birth.selection.account.account_sid,
                        network(&request.request),
                    )?;
                    require_lowbox(
                        unsafe { GetCurrentProcess() },
                        &request.request,
                        request.authentication_id,
                    )?;
                    Ok::<_, anyhow::Error>(proof)
                })
                .transpose()?
        } else {
            None
        };
        let passed = passed && filesystem_proof.as_ref().is_none_or(|p| p.passed);
        let mut family_root_hresult = None;
        let mut family_root_stage = None;
        let family_completed = if let Some(clients) = request.family_clients {
            if passed {
                match family::run_root(&request, &endpoint, clients, deadline(child_timeout())?) {
                    Ok(()) => true,
                    Err(error) => {
                        family_root_hresult =
                            Some(native_code(&error).filter(|v| *v < 0).unwrap_or(E_FAIL.0));
                        family_root_stage = Some(family::root_failure_stage(&error));
                        false
                    }
                }
            } else {
                endpoint.send(&None::<family::Offer>)?;
                false
            }
        } else {
            false
        };
        let passed = passed && (request.family_clients.is_none() || family_completed);
        startup_code = 0x4176;
        endpoint.send(&ChildReport {
            subject: request.clone(),
            proof,
            desktop_proof,
            schannel_proof,
            rpc_proof,
            https_proof,
            filesystem_proof,
            family_completed,
            family_root_hresult,
            family_root_stage,
        })?;
        startup_code = 0x4177;
        ensure!(
            endpoint.receive::<Vec<u8>>()? == ack(&request.request, "finished")?,
            "lowbox terminal acknowledgment differs"
        );
        Ok(passed)
    })();
    std::process::exit(match result {
        Ok(true) => EXIT,
        Ok(false) => EXIT + 1,
        Err(_) => startup_code,
    } as i32);
}

fn synthetic_child_subject() -> ChildRequest {
    let request = FixtureStationLowboxBinding::synthetic_request();
    ChildRequest {
        pid: u32::MAX - 1,
        created: request.created + 1,
        authentication_id: [1, 0],
        station: "Service-0x0-123$".into(),
        native_station: format!(
            "\\Sessions\\{}\\Windows\\WindowStations\\Service-0x0-123$",
            request.birth.selection.session
        ),
        desktop: format!("alysis-station-lowbox-{}", request.birth.nonce),
        desktop_descriptor: "O:SYD:P".into(),
        descriptor: "O:SYD:P".into(),
        filesystem: None,
        family_clients: None,
        request,
    }
}

#[test]
fn station_lowbox_rejected_desktop_report_preserves_serializable_station_evidence() {
    let subject = synthetic_child_subject();
    let stamp = ([1, 0], [2, 0], [3, 0]);
    let mut receipt = Receipt::new(&subject.request, stamp).unwrap();
    receipt.stage = "child_proof".into();
    let wrong = serde_json::json!({
        "name": subject.desktop, "descriptor": "D:P",
        "owner_sid": subject.request.birth.selection.account.account_sid,
        "passed": false, "stage": "actor", "actor_verified": false,
        "attached_name_verified": false, "positive_open": false,
        "attached_same_object": false, "descriptor_matches": false,
        "open_denials": [], "fresh_closed": false
    });
    assert!(
        record_child_proofs(
            &mut receipt,
            &subject,
            ChildProof::new(),
            Some(serde_json::from_value(wrong).unwrap()),
            None,
            None,
            None,
            None,
            false,
            None,
            None,
        )
        .is_err()
    );
    assert!(receipt.child.is_some());
    assert!(receipt.desktop_child.is_none());
    receipt.require(&subject.request, stamp).unwrap();
    let decoded: Receipt = decode(&encode(&receipt).unwrap()).unwrap();
    decoded.require(&subject.request, stamp).unwrap();
}

#[test]
fn station_lowbox_child_request_binds_native_name_to_original_session_and_component() {
    let mut subject = synthetic_child_subject();
    subject.require().unwrap();
    let original = subject.clone();
    for path in [
        r"\Windows\WindowStations\WinSta0".to_owned(),
        format!("{}\\other", original.native_station),
        format!("{}\0", original.native_station),
        original.native_station.replace("123$", "456$"),
    ] {
        subject.native_station = path;
        assert!(subject.require().is_err());
    }
    subject = original.clone();
    subject.request.birth.selection.session += 1;
    assert!(subject.require().is_err());
    for component in ["bad\\name", "bad/name", "bad\nname", "bad\0name"] {
        subject = original.clone();
        subject.station = component.into();
        subject.native_station = format!(
            "\\Sessions\\{}\\Windows\\WindowStations\\{component}",
            subject.request.birth.selection.session
        );
        assert!(subject.require().is_err());
    }
}

#[test]
fn station_lowbox_loader_image_requires_exact_run_component() {
    assert!(loader_image("alysis-station-loader-37822157308.exe").unwrap());
    for name in ["native-tests.exe", "srt_win-test.exe"] {
        assert!(!loader_image(name).unwrap());
    }
    for name in [
        "alysis-station-loader-.exe",
        "alysis-station-loader-01.exe",
        "alysis-station-loader-1.exe.other",
        "alysis-station-loader-1/2.exe",
        "alysis-station-loader-111111111111111111111.exe",
    ] {
        assert!(loader_image(name).is_err());
    }
}

#[test]
fn station_lowbox_loader_routing_selects_only_one_fixed_child() {
    assert!(FILTERED_LOADER_COMPARISON);
    assert_eq!(
        loader_targets("alysis-station-loader-37822157308.exe").unwrap(),
        (false, true)
    );
    assert_eq!(loader_targets("srt_win.exe").unwrap(), (false, false));
    assert!(loader_targets("alysis-station-loader-0.exe").is_err());
    assert_eq!(terminal_checkpoint_stage(), "filtered_primary_control");
}

#[test]
fn station_lowbox_environment_is_bounded_and_exactly_allowlisted() {
    let environment = child_environment(
        r"C:\Windows".into(),
        r"C:\Windows\System32".into(),
        r"C:\Users\fixture\AppData\Local".into(),
        r"C:\Users\fixture".into(),
        r"C:\Users\fixture\AppData\Roaming".into(),
        64,
    )
    .unwrap();
    assert!(environment.ends_with(&[0, 0]));
    let text = String::from_utf16(&environment).unwrap();
    let actual: std::collections::BTreeSet<_> =
        text.split('\0').filter(|s| !s.is_empty()).collect();
    let expected = [
        r"SystemRoot=C:\Windows",
        r"WINDIR=C:\Windows",
        r"PATH=C:\Windows\System32",
        r"APPDATA=C:\Users\fixture\AppData\Roaming",
        r"LOCALAPPDATA=C:\Users\fixture\AppData\Local",
        r"USERPROFILE=C:\Users\fixture",
        r"TEMP=C:\Users\fixture\AppData\Local\Temp",
        r"TMP=C:\Users\fixture\AppData\Local\Temp",
        "ALYSIS_STATION_LOWBOX_ENDPOINT=64",
    ]
    .into_iter()
    .collect();
    assert_eq!(actual, expected);
    let compose = |local: String, endpoint| {
        child_environment(
            r"C:\Windows".into(),
            r"C:\Windows\System32".into(),
            local,
            r"C:\Users\fixture".into(),
            r"C:\Users\fixture\AppData\Roaming".into(),
            endpoint,
        )
    };
    for local in [
        "relative".into(),
        "C:\\bad\0value".into(),
        "C:\\bad\nvalue".into(),
        format!("C:\\{}", "x".repeat(12000)),
    ] {
        assert!(compose(local, 64).is_err());
    }
    for endpoint in [0, isize::MAX as usize, usize::MAX] {
        assert!(compose(r"C:\Users\fixture\AppData\Local".into(), endpoint).is_err());
    }
}
