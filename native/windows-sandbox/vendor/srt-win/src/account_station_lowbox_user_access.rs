//! Diagnostic opens only. Impersonation observations do not establish that
//! primary-token USER32 startup or permission-mutation refusal will succeed.
use super::*;
#[path = "account_station_lowbox_station_lookup.rs"]
mod lookup;
const MAXIMUM: u32 = 0x02000000;
const STATION: u32 = PRIVATE_STATION_ACCESS;
const DESKTOP: u32 = 0x20083;

#[repr(C)]
#[derive(Default)]
struct ObjectBasic {
    attributes: u32,
    granted_access: u32,
    handle_count: u32,
    pointer_count: u32,
    reserved: [u32; 10],
}
#[link(name = "ntdll")]
unsafe extern "system" {
    fn NtQueryObject(
        handle: HANDLE,
        class: u32,
        data: *mut core::ffi::c_void,
        length: u32,
        returned: *mut u32,
    ) -> windows::Win32::Foundation::NTSTATUS;
}
pub(super) fn granted(handle: HANDLE) -> Result<u32> {
    let mut value = ObjectBasic::default();
    let mut length = 0;
    unsafe {
        NtQueryObject(
            handle,
            0,
            (&mut value as *mut ObjectBasic).cast(),
            size_of::<ObjectBasic>() as u32,
            &mut length,
        )
    }
    .ok()?;
    ensure!(
        length as usize == size_of::<ObjectBasic>(),
        "USER basic information size differs"
    );
    Ok(value.granted_access)
}
pub(super) fn original_name(handle: HANDLE) -> Result<String> {
    lookup::name(handle)
}

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct OpenObservation {
    requested: u32,
    opened: bool,
    original: bool,
    granted: Option<u32>,
    closed: bool,
    hresult: Option<i32>,
}
impl OpenObservation {
    fn new(requested: u32) -> Self {
        Self {
            requested,
            opened: false,
            original: false,
            granted: None,
            closed: false,
            hresult: None,
        }
    }
    fn inspect(&mut self, handle: HANDLE, original: HANDLE) -> Result<()> {
        self.opened = true;
        noninheritable(handle)?;
        self.original = unsafe { CompareObjectHandles(handle, original) }.as_bool();
        ensure!(self.original, "USER diagnostic opened another object");
        self.granted = Some(granted(handle)?);
        Ok(())
    }
    fn require(&self) -> Result<()> {
        ensure!(
            self.opened || (!self.original && self.granted.is_none() && !self.closed),
            "unopened USER object has observations"
        );
        ensure!(
            self.granted.is_none() || self.original,
            "USER grant lacks original identity"
        );
        Ok(())
    }
}

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Probe {
    diagnostic_only: bool,
    primary_startup_qualified: bool,
    station: Vec<OpenObservation>,
    desktop: Vec<OpenObservation>,
    change_notify_attributes: Option<u32>,
    impersonation_hresult: Option<i32>,
    descriptors_unchanged: bool,
    associations_unchanged: bool,
    lookup: Option<lookup::Probe>,
}
impl Probe {
    pub(super) fn observe(users: &UserPair, token: &OwnedHandle) -> Result<Self> {
        crate::account_impersonation::require_process_identity()?;
        let station = users.station.raw()?;
        let desktop = users.desktop.0.context("original private desktop absent")?;
        let before_desktop = security(HANDLE(desktop.0), INFO)?.0;
        let mut probe = Self {
            diagnostic_only: true,
            primary_startup_qualified: false,
            station: Vec::new(),
            desktop: Vec::new(),
            change_notify_attributes: change_notify(token.raw())?,
            impersonation_hresult: None,
            descriptors_unchanged: false,
            associations_unchanged: false,
            lookup: Some(lookup::Probe::observe(users, token)?),
        };
        let observed = crate::account_impersonation::with_token(token, || {
            // Keep the station opened by the lowbox as the relative lookup root.
            // Never substitute the trusted caller's original full-access handle.
            let mut root_observation = OpenObservation::new(MAXIMUM);
            let root = unsafe { OpenWindowStationW(pcwstr(&wstr(&users.name)), false, MAXIMUM) };
            let mut root = match root {
                Ok(raw) => {
                    let root = Station(Some(raw));
                    if let Err(error) = root_observation.inspect(HANDLE(raw.0), HANDLE(station.0)) {
                        root_observation.hresult = native_code(&error);
                    }
                    Some(root)
                }
                Err(error) => {
                    root_observation.hresult = Some(error.code().0);
                    None
                }
            };
            let mut minimum = OpenObservation::new(STATION);
            let result = (|| -> Result<()> {
                let raw =
                    unsafe { OpenWindowStationW(pcwstr(&wstr(&users.name)), false, STATION)? };
                let mut opened = Station(Some(raw));
                let observed = minimum.inspect(HANDLE(raw.0), HANDLE(station.0));
                opened.close()?;
                minimum.closed = true;
                observed
            })();
            if let Err(error) = result {
                minimum.hresult = native_code(&error);
            }
            if root_observation.original {
                for requested in [MAXIMUM, DESKTOP] {
                    let mut observation = OpenObservation::new(requested);
                    let result = (|| -> Result<()> {
                        let (mut buffer, length) = crate::util::counted_utf16(&users.desktop_name);
                        let mut name = UNICODE_STRING {
                            Length: length,
                            MaximumLength: length,
                            Buffer: PWSTR(buffer.as_mut_ptr()),
                        };
                        let raw = unsafe {
                            (users.open)(
                                &object_attributes(&mut name, root.as_ref().unwrap().raw()?, None),
                                0,
                                requested,
                            )
                        };
                        if raw.is_invalid() {
                            return Err(windows::core::Error::from_thread().into());
                        }
                        let mut opened = Desktop(Some(raw));
                        let observed = observation.inspect(HANDLE(raw.0), HANDLE(desktop.0));
                        opened.close()?;
                        observation.closed = true;
                        observed
                    })();
                    if let Err(error) = result {
                        observation.hresult = native_code(&error);
                    }
                    probe.desktop.push(observation);
                }
            }
            if let Some(root) = &mut root {
                match root.close() {
                    Ok(()) => root_observation.closed = true,
                    Err(error) => root_observation.hresult = native_code(&error),
                }
            }
            probe.station = vec![root_observation, minimum];
            Ok(())
        });
        if let Err(error) = observed {
            probe.impersonation_hresult = native_code(&error);
        }
        crate::account_impersonation::require_process_identity()?;
        probe.descriptors_unchanged = security(HANDLE(station.0), INFO)?.0 == users.station_bytes
            && security(HANDLE(desktop.0), INFO)?.0 == before_desktop
            && security(HANDLE(users.original.0), ORIGINAL_INFO)?.0 == users.original_bytes;
        probe.associations_unchanged = unsafe {
            CompareObjectHandles(
                HANDLE(GetProcessWindowStation()?.0),
                HANDLE(users.original.0),
            )
            .as_bool()
                && CompareObjectHandles(
                    HANDLE(GetThreadDesktop(GetCurrentThreadId())?.0),
                    HANDLE(users.original_desktop.0),
                )
                .as_bool()
        };
        ensure!(
            probe.descriptors_unchanged && probe.associations_unchanged,
            "read-only USER diagnostic changed original state"
        );
        probe.require()?;
        Ok(probe)
    }
    pub(crate) fn require(&self) -> Result<()> {
        ensure!(
            self.diagnostic_only && !self.primary_startup_qualified,
            "USER diagnostic cannot qualify primary startup"
        );
        ensure!(
            self.station.is_empty()
                || self.station.iter().map(|o| o.requested).collect::<Vec<_>>()
                    == [MAXIMUM, STATION],
            "USER station matrix differs"
        );
        ensure!(
            self.desktop.is_empty()
                || self.desktop.iter().map(|o| o.requested).collect::<Vec<_>>()
                    == [MAXIMUM, DESKTOP],
            "USER desktop matrix differs"
        );
        ensure!(
            self.desktop.is_empty() || self.station.first().is_some_and(|o| o.original),
            "USER desktop lookup lacks original lowbox-opened station"
        );
        for o in self.station.iter().chain(self.desktop.iter()) {
            o.require()?;
        }
        if let Some(lookup) = &self.lookup {
            lookup.require()?;
        }
        ensure!(
            serde_json::to_vec(self)?.len() <= 6144,
            "USER diagnostic exceeds bound"
        );
        Ok(())
    }
}
fn change_notify(token: HANDLE) -> Result<Option<u32>> {
    let mut luid = windows::Win32::Foundation::LUID::default();
    unsafe {
        LookupPrivilegeValueW(None, w!("SeChangeNotifyPrivilege"), &mut luid)?;
    }
    let mut buffer = [0u64; 128];
    let mut length = 0;
    unsafe {
        GetTokenInformation(
            token,
            TokenPrivileges,
            Some(buffer.as_mut_ptr().cast()),
            size_of_val(&buffer) as u32,
            &mut length,
        )?;
    }
    let offset = std::mem::offset_of!(TOKEN_PRIVILEGES, Privileges);
    ensure!(
        length as usize >= offset && length as usize <= size_of_val(&buffer),
        "privilege diagnostic size differs"
    );
    let info = unsafe { &*buffer.as_ptr().cast::<TOKEN_PRIVILEGES>() };
    let count = info.PrivilegeCount as usize;
    ensure!(
        count <= (length as usize - offset) / size_of::<LUID_AND_ATTRIBUTES>(),
        "privilege count exceeds buffer"
    );
    let privileges = unsafe { std::slice::from_raw_parts(info.Privileges.as_ptr(), count) };
    Ok(privileges
        .iter()
        .find(|p| p.Luid == luid)
        .map(|p| p.Attributes.0))
}

#[test]
fn station_lowbox_user_access_evidence_never_qualifies_primary_startup() {
    let observation = |requested| OpenObservation {
        requested,
        opened: true,
        original: true,
        granted: Some(requested),
        closed: true,
        hresult: None,
    };
    let mut probe = Probe {
        diagnostic_only: true,
        primary_startup_qualified: false,
        station: vec![observation(MAXIMUM), observation(STATION)],
        desktop: vec![observation(MAXIMUM), observation(DESKTOP)],
        change_notify_attributes: Some(3),
        impersonation_hresult: None,
        descriptors_unchanged: true,
        associations_unchanged: true,
        lookup: None,
    };
    probe.require().unwrap();
    probe.primary_startup_qualified = true;
    assert!(probe.require().is_err());
    probe.primary_startup_qualified = false;
    probe.station[0].original = false;
    assert!(probe.require().is_err());
    probe.station[0].original = true;
    probe.desktop.swap(0, 1);
    assert!(probe.require().is_err());
    probe.desktop.swap(0, 1);
    probe.desktop[0].opened = false;
    assert!(probe.require().is_err());
    let mut value = serde_json::to_value(probe).unwrap();
    value["resume_authorized"] = true.into();
    assert!(serde_json::from_value::<Probe>(value).is_err());
}
