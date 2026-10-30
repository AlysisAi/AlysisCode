//! Separate station DACL evaluation from Win32 name lookup and native opens.
//! No grant, station association, inherited USER handle, or launch authority.
use super::*;
type OpenStation = unsafe extern "system" fn(*const OBJECT_ATTRIBUTES, u32) -> HWINSTA;
const MASKS: [u32; 5] = [MAXIMUM, STATION, 0, 0x20000, 2];

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Probe {
    diagnostic_only: bool,
    primary_startup_qualified: bool,
    original_nt_name: String,
    station_dacl_access: Option<u32>,
    station_dacl_hresult: Option<i32>,
    raw_win32: OpenObservation,
    raw_native: OpenObservation,
    lowbox_native: Vec<OpenObservation>,
    trusted_parent_original: bool,
    trusted_parent_hresult: Option<i32>,
    raw_relative: Option<OpenObservation>,
    lowbox_relative: Vec<OpenObservation>,
    lowbox_parent_traverse: Option<OpenObservation>,
    desktop_native: Vec<OpenObservation>,
    impersonation_hresult: Option<i32>,
}
impl Probe {
    pub(super) fn observe(users: &UserPair, token: &OwnedHandle) -> Result<Self> {
        crate::account_impersonation::require_process_identity()?;
        let station = users.station.raw()?;
        let original_nt_name = name(HANDLE(station.0))?;
        ensure!(
            original_nt_name.starts_with('\\')
                && original_nt_name.ends_with(&format!("\\{}", users.name)),
            "original station native name differs"
        );
        let module = unsafe { GetModuleHandleW(w!("win32u.dll"))? };
        let open: OpenStation = unsafe {
            std::mem::transmute(
                GetProcAddress(module, s!("NtUserOpenWindowStation"))
                    .context("native station open missing")?,
            )
        };
        let mut probe = Self {
            diagnostic_only: true,
            primary_startup_qualified: false,
            original_nt_name,
            station_dacl_access: None,
            station_dacl_hresult: None,
            raw_win32: observe_open(station, MAXIMUM, || unsafe {
                OpenWindowStationW(pcwstr(&wstr(&users.name)), false, MAXIMUM).map_err(Into::into)
            }),
            raw_native: OpenObservation::new(MAXIMUM),
            lowbox_native: Vec::new(),
            trusted_parent_original: false,
            trusted_parent_hresult: None,
            raw_relative: None,
            lowbox_relative: Vec::new(),
            lowbox_parent_traverse: None,
            desktop_native: Vec::new(),
            impersonation_hresult: None,
        };
        probe.raw_native = observe_open(station, MAXIMUM, || {
            native_open(open, HANDLE::default(), &probe.original_nt_name, MAXIMUM)
        });
        // This original directory pin is used only inside this synchronous
        // diagnostic. It is never inherited, published, or used for launch.
        let parent = match parent_directory(&probe.original_nt_name) {
            Ok(handle) => {
                probe.trusted_parent_original = true;
                probe.raw_relative = Some(observe_open(station, MAXIMUM, || {
                    native_open(open, handle.raw(), &users.name, MAXIMUM)
                }));
                Some(handle)
            }
            Err(error) => {
                probe.trusted_parent_hresult = native_code(&error);
                None
            }
        };
        match dacl_access(station, token.raw()) {
            Ok(access) => probe.station_dacl_access = Some(access),
            Err(error) => probe.station_dacl_hresult = native_code(&error),
        }
        let result = crate::account_impersonation::with_token(token, || {
            if let Some(parent) = &parent {
                let mut observation = OpenObservation::new(2);
                let result = (|| -> Result<()> {
                    let opened = parent_directory(&probe.original_nt_name)?;
                    let result = observation.inspect(opened.raw(), parent.raw());
                    unsafe {
                        CloseHandle(opened.into_raw())?;
                    }
                    observation.closed = true;
                    result
                })();
                if let Err(error) = result {
                    observation.hresult = native_code(&error);
                }
                probe.lowbox_parent_traverse = Some(observation);
            }
            for access in MASKS {
                probe.lowbox_native.push(observe_open(station, access, || {
                    native_open(open, HANDLE::default(), &probe.original_nt_name, access)
                }));
                if let Some(parent) = &parent {
                    probe
                        .lowbox_relative
                        .push(observe_open(station, access, || {
                            native_open(open, parent.raw(), &users.name, access)
                        }));
                }
            }
            // Only a fresh station opened as this lowbox supplies this root.
            // The raw-account original station is never used for desktop lookup.
            let raw = native_open(open, HANDLE::default(), &probe.original_nt_name, STATION)?;
            let mut opened = Station(Some(raw));
            noninheritable(HANDLE(raw.0))?;
            ensure!(
                unsafe { CompareObjectHandles(HANDLE(raw.0), HANDLE(station.0)) }.as_bool(),
                "desktop diagnostic station differs"
            );
            let original = users
                .desktop
                .0
                .context("desktop diagnostic original closed")?;
            for requested in [MAXIMUM, DESKTOP] {
                let mut observation = OpenObservation::new(requested);
                let result = (|| -> Result<()> {
                    let (mut buffer, length) = crate::util::counted_utf16(&users.desktop_name);
                    let mut name = UNICODE_STRING {
                        Length: length,
                        MaximumLength: length,
                        Buffer: PWSTR(buffer.as_mut_ptr()),
                    };
                    let desktop = unsafe {
                        (users.open)(&object_attributes(&mut name, raw, None), 0, requested)
                    };
                    if desktop.is_invalid() {
                        return Err(windows::core::Error::from_thread().into());
                    }
                    let mut desktop = Desktop(Some(desktop));
                    let result =
                        observation.inspect(HANDLE(desktop.0.unwrap().0), HANDLE(original.0));
                    desktop.close()?;
                    observation.closed = true;
                    result
                })();
                if let Err(error) = result {
                    observation.hresult = native_code(&error);
                }
                probe.desktop_native.push(observation);
            }
            opened.close()?;
            Ok(())
        });
        if let Err(error) = result {
            probe.impersonation_hresult = native_code(&error);
        }
        crate::account_impersonation::require_process_identity()?;
        drop(parent);
        probe.require()?;
        Ok(probe)
    }
    pub(super) fn require(&self) -> Result<()> {
        ensure!(
            self.diagnostic_only && !self.primary_startup_qualified,
            "station lookup cannot qualify primary startup"
        );
        ensure!(
            !self.original_nt_name.is_empty()
                && self.original_nt_name.len() <= 2048
                && !self.original_nt_name.chars().any(char::is_control),
            "station lookup name exceeds bounds"
        );
        ensure!(
            self.station_dacl_access.is_none() || self.station_dacl_hresult.is_none(),
            "station DACL observations contradict each other"
        );
        ensure!(
            self.raw_win32.requested == MAXIMUM && self.raw_native.requested == MAXIMUM,
            "station lookup controls differ"
        );
        ensure!(
            self.lowbox_native.is_empty()
                || self
                    .lowbox_native
                    .iter()
                    .map(|o| o.requested)
                    .collect::<Vec<_>>()
                    == MASKS,
            "native station lookup matrix differs"
        );
        ensure!(
            self.trusted_parent_original == self.raw_relative.is_some()
                && (self.lowbox_relative.is_empty()
                    || (self.trusted_parent_original
                        && self
                            .lowbox_relative
                            .iter()
                            .map(|o| o.requested)
                            .collect::<Vec<_>>()
                            == MASKS)),
            "relative station lookup lacks its original diagnostic parent"
        );
        ensure!(
            self.lowbox_parent_traverse
                .as_ref()
                .is_none_or(|p| self.trusted_parent_original && p.requested == 2),
            "parent traverse observation lacks original control"
        );
        ensure!(
            self.desktop_native.is_empty()
                || (self
                    .lowbox_native
                    .iter()
                    .any(|o| o.requested == STATION && o.original)
                    && self
                        .desktop_native
                        .iter()
                        .map(|o| o.requested)
                        .collect::<Vec<_>>()
                        == [MAXIMUM, DESKTOP]),
            "native desktop lookup lacks lowbox station or correct mask matrix"
        );
        for value in [&self.raw_win32, &self.raw_native]
            .into_iter()
            .chain(&self.lowbox_native)
            .chain(self.raw_relative.iter())
            .chain(&self.lowbox_relative)
            .chain(self.lowbox_parent_traverse.iter())
            .chain(&self.desktop_native)
        {
            value.require()?;
        }
        Ok(())
    }
}
fn observe_open(
    original: HWINSTA,
    access: u32,
    operation: impl FnOnce() -> Result<HWINSTA>,
) -> OpenObservation {
    let mut observation = OpenObservation::new(access);
    let result = (|| -> Result<()> {
        let raw = operation()?;
        let mut handle = Station(Some(raw));
        let result = observation.inspect(HANDLE(raw.0), HANDLE(original.0));
        handle.close()?;
        observation.closed = true;
        result
    })();
    if let Err(error) = result {
        observation.hresult = native_code(&error);
    }
    observation
}
fn native_open(open: OpenStation, root: HANDLE, path: &str, access: u32) -> Result<HWINSTA> {
    let (mut buffer, length) = crate::util::counted_utf16(path);
    let mut name = UNICODE_STRING {
        Length: length,
        MaximumLength: length,
        Buffer: PWSTR(buffer.as_mut_ptr()),
    };
    let mut attributes = object_attributes(&mut name, HWINSTA::default(), None);
    attributes.RootDirectory = root;
    let handle = unsafe { open(&attributes, access) };
    if handle.is_invalid() {
        return Err(windows::core::Error::from_thread().into());
    }
    Ok(handle)
}
fn parent_directory(path: &str) -> Result<OwnedHandle> {
    let (parent, _) = path.rsplit_once('\\').context("station parent missing")?;
    ensure!(!parent.is_empty(), "station parent is root");
    let (mut buffer, length) = crate::util::counted_utf16(parent);
    let mut unicode = UNICODE_STRING {
        Length: length,
        MaximumLength: length,
        Buffer: PWSTR(buffer.as_mut_ptr()),
    };
    let attributes = object_attributes(&mut unicode, HWINSTA::default(), None);
    let mut raw = HANDLE::default();
    // DIRECTORY_TRAVERSE only. Requesting QUERY as well denied the raw-account
    // control in run37907827674. No changes, enumeration, or child inheritance.
    unsafe { windows::Wdk::Storage::FileSystem::NtOpenDirectoryObject(&mut raw, 2, &attributes) }
        .ok()?;
    let handle = OwnedHandle(raw);
    noninheritable(handle.raw())?;
    ensure!(
        name(handle.raw())? == parent,
        "station parent native name differs"
    );
    Ok(handle)
}
pub(super) fn name(handle: HANDLE) -> Result<String> {
    let mut buffer = [0u64; 512];
    let mut length = 0;
    unsafe {
        NtQueryObject(
            handle,
            1,
            buffer.as_mut_ptr().cast(),
            size_of_val(&buffer) as u32,
            &mut length,
        )
    }
    .ok()?;
    ensure!(
        length as usize >= size_of::<UNICODE_STRING>() && length as usize <= size_of_val(&buffer),
        "station name response size differs"
    );
    let value = unsafe { &*buffer.as_ptr().cast::<UNICODE_STRING>() };
    let base = buffer.as_ptr() as usize;
    let offset = (value.Buffer.0 as usize)
        .checked_sub(base)
        .context("station name pointer precedes buffer")?;
    let range = name_range(offset, value.Length as usize, length as usize)?;
    let units =
        unsafe { std::slice::from_raw_parts((base + range.start) as *const u16, range.len() / 2) };
    Ok(String::from_utf16(units)?)
}
fn name_range(offset: usize, bytes: usize, returned: usize) -> Result<std::ops::Range<usize>> {
    let end = offset
        .checked_add(bytes)
        .context("station name range overflow")?;
    ensure!(
        offset >= size_of::<UNICODE_STRING>()
            && offset % 2 == 0
            && bytes > 0
            && bytes <= 2048
            && bytes % 2 == 0
            && end <= returned,
        "station name range exceeds response"
    );
    Ok(offset..end)
}
fn dacl_access(station: HWINSTA, token: HANDLE) -> Result<u32> {
    // AccessCheck evaluates the actual DACL without traversing the namespace.
    // It is explicitly not a substitute for MIC, USER policy, or native opens.
    let info = OWNER_SECURITY_INFORMATION | GROUP_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION;
    let sd = OwnedSd::from_sddl(&security(HANDLE(station.0), info)?.1)?;
    let mut raw = HANDLE::default();
    unsafe {
        DuplicateToken(token, SecurityImpersonation, &mut raw)?;
    }
    let token = OwnedHandle(raw);
    let mapping = GENERIC_MAPPING {
        // Microsoft documents a separate noninteractive-station mapping.
        GenericRead: 0x20103,
        GenericWrite: 0x2000c,
        GenericExecute: 0x20060,
        GenericAll: 0xf016f,
    };
    let mut privileges = [0usize; 128];
    let mut length = size_of_val(&privileges) as u32;
    let mut access = 0;
    let mut allowed = false.into();
    unsafe {
        AccessCheck(
            sd.ptr,
            token.raw(),
            MAXIMUM,
            &mapping,
            Some(privileges.as_mut_ptr().cast()),
            &mut length,
            &mut access,
            &mut allowed,
        )?;
    }
    Ok(if allowed.as_bool() { access } else { 0 })
}

#[test]
fn station_lowbox_lookup_rejects_native_name_pointer_ranges() {
    assert_eq!(name_range(16, 6, 24).unwrap(), 16..22);
    for (offset, bytes, returned) in [
        (0, 6, 24),
        (17, 6, 24),
        (16, 0, 24),
        (16, 5, 24),
        (16, 10, 24),
        (16, 2050, 4096),
        (usize::MAX - 1, 4, usize::MAX),
    ] {
        assert!(name_range(offset, bytes, returned).is_err());
    }
}

#[test]
fn station_lowbox_lookup_evidence_cannot_promote_diagnostic_parent_to_authority() {
    let mut probe = Probe {
        diagnostic_only: true,
        primary_startup_qualified: false,
        original_nt_name: "\\Sessions\\2\\Windows\\WindowStations\\Synthetic".into(),
        station_dacl_access: Some(STATION),
        station_dacl_hresult: None,
        raw_win32: OpenObservation::new(MAXIMUM),
        raw_native: OpenObservation::new(MAXIMUM),
        lowbox_native: MASKS.into_iter().map(OpenObservation::new).collect(),
        trusted_parent_original: false,
        trusted_parent_hresult: None,
        raw_relative: None,
        lowbox_relative: Vec::new(),
        lowbox_parent_traverse: None,
        desktop_native: Vec::new(),
        impersonation_hresult: None,
    };
    probe.require().unwrap();
    probe.lowbox_relative = MASKS.into_iter().map(OpenObservation::new).collect();
    assert!(probe.require().is_err());
    probe.trusted_parent_original = true;
    probe.raw_relative = Some(OpenObservation::new(MAXIMUM));
    probe.require().unwrap();
    probe.lowbox_relative.swap(0, 1);
    assert!(probe.require().is_err());
    probe.lowbox_relative.swap(0, 1);
    probe.primary_startup_qualified = true;
    assert!(probe.require().is_err());
    probe.primary_startup_qualified = false;
    probe.station_dacl_hresult = Some(-2147024891);
    assert!(probe.require().is_err());
    let mut value = serde_json::to_value(probe).unwrap();
    value["inherited_parent_authorized"] = true.into();
    assert!(serde_json::from_value::<Probe>(value).is_err());
}
