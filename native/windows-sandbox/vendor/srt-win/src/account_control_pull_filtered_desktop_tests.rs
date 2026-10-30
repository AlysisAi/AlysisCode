//! Test-only GUI attachment for a filtered broker created by an elevated CI
//! controller. Read existing descriptors; modify only fresh test-owned objects.
use super::*;
use windows::Win32::System::StationsAndDesktops::*;

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Probe {
    pub(super) startup_security: Option<security::Probe>,
    token_integrity: u32,
    station_dacl_granted: u32,
    inherited_desktop_dacl_granted: u32,
    pub(super) selected_desktop_dacl_granted: u32,
    station_label: Option<Label>,
    inherited_desktop_label: Option<Label>,
    pub(super) selected_station_dacl_granted: u32,
    pub(super) selected_station_label: Option<Label>,
    pub(super) selected_desktop_label: Option<Label>,
    private_station: bool,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Label {
    integrity: u32,
    policy: u32,
}

fn label(object: HANDLE) -> Result<Option<Label>> {
    let mut words = [0usize; 8192];
    let mut length = 0;
    unsafe {
        GetUserObjectSecurity(
            object,
            &LABEL_SECURITY_INFORMATION.0,
            Some(PSECURITY_DESCRIPTOR(words.as_mut_ptr().cast())),
            size_of_val(&words) as u32,
            &mut length,
        )
        .context("read USER integrity label")?;
    }
    ensure!(
        length > 0 && length as usize <= size_of_val(&words),
        "invalid label descriptor"
    );
    let mut present = false.into();
    let mut defaulted = false.into();
    let mut acl = std::ptr::null_mut();
    unsafe {
        GetSecurityDescriptorSacl(
            PSECURITY_DESCRIPTOR(words.as_mut_ptr().cast()),
            &mut present,
            &mut acl,
            &mut defaulted,
        )?;
    }
    if !present.as_bool() || acl.is_null() {
        return Ok(None);
    }
    let start = words.as_ptr() as usize;
    let end = start + length as usize;
    ensure!(
        (acl as usize) >= start && (acl as usize) <= end.saturating_sub(size_of::<ACL>()),
        "label ACL outside descriptor"
    );
    let count = unsafe { (*acl).AceCount };
    ensure!(count <= 32, "label ACL exceeds bound");
    let mut result = None;
    for index in 0..count {
        let mut ace = std::ptr::null_mut();
        unsafe {
            GetAce(acl, index as u32, &mut ace)?;
        }
        let ptr = ace as usize;
        ensure!(
            ptr >= start && ptr <= end.saturating_sub(4),
            "label ACE outside descriptor"
        );
        let header = unsafe { &*ace.cast::<ACE_HEADER>() };
        let bytes = header.AceSize as usize;
        ensure!(bytes >= 4 && bytes <= end - ptr, "label ACE size invalid");
        if header.AceType == 0x11 {
            // SYSTEM_MANDATORY_LABEL_ACE_TYPE
            ensure!(
                result.is_none() && bytes == 20,
                "unsupported label ACE shape"
            );
            let raw = unsafe { std::slice::from_raw_parts(ace.cast::<u8>(), bytes) };
            ensure!(
                raw[8..16] == [1, 1, 0, 0, 0, 0, 0, 16],
                "invalid integrity SID authority"
            );
            result = Some(Label {
                integrity: u32::from_le_bytes(raw[16..20].try_into()?),
                policy: u32::from_le_bytes(raw[4..8].try_into()?),
            });
        }
    }
    Ok(result)
}

fn mapping(desktop: bool) -> GENERIC_MAPPING {
    if desktop {
        GENERIC_MAPPING {
            GenericRead: 0x20041,
            GenericWrite: 0x200be,
            GenericExecute: 0x20100,
            GenericAll: 0xf01ff,
        }
    } else {
        GENERIC_MAPPING {
            GenericRead: 0x20303,
            GenericWrite: 0x2001c,
            GenericExecute: 0x20060,
            GenericAll: 0xf037f,
        }
    }
}

fn granted(object: HANDLE, token: HANDLE, desktop: bool) -> Result<u32> {
    let mut words = [0usize; 8192];
    let mut length = 0;
    unsafe {
        GetUserObjectSecurity(
            object,
            &(OWNER_SECURITY_INFORMATION | GROUP_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION)
                .0,
            Some(PSECURITY_DESCRIPTOR(words.as_mut_ptr().cast())),
            size_of_val(&words) as u32,
            &mut length,
        )?;
    }
    ensure!(
        length > 0 && length as usize <= size_of_val(&words),
        "invalid USER descriptor size"
    );
    let mut raw = HANDLE::default();
    unsafe {
        DuplicateToken(token, SecurityImpersonation, &mut raw)?;
    }
    let copy = OwnedHandle(raw);
    let mut privileges = [0usize; 128];
    let mut privilege_length = size_of_val(&privileges) as u32;
    let mut access = 0;
    let mut allowed = false.into();
    unsafe {
        AccessCheck(
            PSECURITY_DESCRIPTOR(words.as_mut_ptr().cast()),
            copy.raw(),
            0x0200_0000, // MAXIMUM_ALLOWED; report DACL access, not MIC admission.
            &mapping(desktop),
            Some(privileges.as_mut_ptr().cast::<PRIVILEGE_SET>()),
            &mut privilege_length,
            &mut access,
            &mut allowed,
        )?;
    }
    Ok(if allowed.as_bool() { access } else { 0 })
}

impl Probe {
    pub(super) fn require_medium(&self) -> Result<()> {
        ensure!(
            self.token_integrity == 8192,
            "filtered fixture requires Medium integrity"
        );
        Ok(())
    }
    pub(super) fn observe(token: HANDLE) -> Result<Self> {
        let mut words = [0usize; 128];
        let mut length = 0;
        unsafe {
            GetTokenInformation(
                token,
                TokenIntegrityLevel,
                Some(words.as_mut_ptr().cast()),
                size_of_val(&words) as u32,
                &mut length,
            )?;
        }
        ensure!(
            length as usize <= size_of_val(&words)
                && length as usize >= size_of::<TOKEN_MANDATORY_LABEL>(),
            "invalid token integrity size"
        );
        let sid = unsafe { (*words.as_ptr().cast::<TOKEN_MANDATORY_LABEL>()).Label.Sid };
        let start = words.as_ptr() as usize;
        let end = start + length as usize;
        let ptr = sid.0 as usize;
        ensure!(
            ptr >= start + size_of::<TOKEN_MANDATORY_LABEL>() && ptr <= end.saturating_sub(12),
            "integrity SID outside token buffer"
        );
        ensure!(
            unsafe { *sid.0.cast::<u8>().add(1) } == 1 && unsafe { IsValidSid(sid) }.as_bool(),
            "invalid integrity SID"
        );
        let integrity = unsafe { *GetSidSubAuthority(sid, 0) };
        let station = unsafe { GetProcessWindowStation() }?;
        let desktop = unsafe { GetThreadDesktop(GetCurrentThreadId()) }?;
        Ok(Self {
            startup_security: None,
            token_integrity: integrity,
            station_dacl_granted: granted(HANDLE(station.0), token, false)?,
            inherited_desktop_dacl_granted: granted(HANDLE(desktop.0), token, true)?,
            selected_desktop_dacl_granted: 0,
            station_label: label(HANDLE(station.0))?,
            inherited_desktop_label: label(HANDLE(desktop.0))?,
            selected_station_dacl_granted: 0,
            selected_station_label: None,
            selected_desktop_label: None,
            private_station: false,
        })
    }
}

pub(super) struct Prepared {
    handle: HDESK,
    station: Option<HWINSTA>,
    pub(super) path: Vec<u16>,
}
impl Drop for Prepared {
    fn drop(&mut self) {
        unsafe {
            let _ = CloseDesktop(self.handle);
            if let Some(station) = self.station {
                let _ = CloseWindowStation(station);
            }
        }
    }
}
impl Prepared {
    pub(super) fn create(private_station: bool) -> Result<Self> {
        let original = unsafe { GetProcessWindowStation() }?;
        let desktop_before = unsafe { GetThreadDesktop(GetCurrentThreadId()) }?;
        let nonce = ControlAddress::fresh()?.nonce;
        let suffix: String = nonce.iter().map(|b| format!("{b:02x}")).collect();
        let name = format!("alysis-filtered-test-{suffix}");
        let station_name = if private_station {
            format!("alysis-filtered-station-{suffix}")
        } else {
            crate::winsta::current_winsta_name()?
        };
        let owner = crate::sid::current_user_sid()?;
        let descriptor = OwnedSd::from_sddl(&format!(
            "O:{owner}D:P(A;;GA;;;SY)(A;;GA;;;{owner})S:(ML;;NW;;;ME)"
        ))?;
        let security = SECURITY_ATTRIBUTES {
            nLength: size_of::<SECURITY_ATTRIBUTES>() as u32,
            lpSecurityDescriptor: descriptor.ptr.0,
            bInheritHandle: false.into(),
        };
        // Named station creation requires an administrator. Only the disposable
        // real-account controller selects this path; local ordinary-user tests
        // explicitly retain their existing station and create just a desktop.
        // This is an upfront fixture choice, never fallback after a failed call.
        let station = if private_station {
            Some(
                unsafe {
                    CreateWindowStationW(pcwstr(&wstr(&station_name)), 1, 0xf037f, Some(&security))
                }
                .context("create private fixture window station")?,
            )
        } else {
            None
        };
        let mut prepared = Self {
            handle: HDESK::default(),
            station,
            path: vec![],
        };
        let restore = station.map(|_| RestoreStation(original));
        if let Some(station) = station {
            unsafe {
                SetProcessWindowStation(station)
                    .context("select private fixture window station")?;
            }
        }
        // Only this test process's selection changes, not the input desktop or
        // any existing descriptor. Restore it before any child is created.
        prepared.handle = unsafe {
            CreateDesktopW(
                pcwstr(&wstr(&name)),
                None,
                None,
                DESKTOP_CONTROL_FLAGS(0),
                0xf01ff,
                Some(&security),
            )
        }
        .context("create private fixture desktop")?;
        drop(restore);
        ensure!(
            unsafe { GetProcessWindowStation() }? == original
                && unsafe { GetThreadDesktop(GetCurrentThreadId()) }? == desktop_before,
            "test controller USER context was not restored"
        );
        prepared.path = wstr(&format!("{station_name}\\{name}"));
        Ok(prepared)
    }
    pub(super) fn observe(&self, token: HANDLE, probe: &mut Probe) -> Result<()> {
        let station = match self.station {
            Some(station) => station,
            None => unsafe { GetProcessWindowStation() }?,
        };
        let station_access = granted(HANDLE(station.0), token, false)?;
        let access = granted(HANDLE(self.handle.0), token, true)?;
        ensure!(
            access & 0xf01ff == 0xf01ff && station_access & 0xf037f == 0xf037f,
            "filtered fixture cannot access its private USER objects"
        );
        probe.selected_desktop_dacl_granted = access;
        probe.selected_station_dacl_granted = station_access;
        probe.selected_station_label = label(HANDLE(station.0))?;
        probe.selected_desktop_label = label(HANDLE(self.handle.0))?;
        probe.private_station = self.station.is_some();
        let mut labels = vec![&probe.selected_desktop_label];
        if self.station.is_some() {
            labels.push(&probe.selected_station_label);
        }
        for label in labels {
            ensure!(
                label
                    .as_ref()
                    .is_some_and(|l| l.integrity == 8192 && l.policy == 1),
                "test-owned USER object did not retain Medium/no-write-up label"
            );
        }
        Ok(())
    }
}

struct RestoreStation(HWINSTA);
impl Drop for RestoreStation {
    fn drop(&mut self) {
        if unsafe { SetProcessWindowStation(self.0) }.is_err() {
            // The selection is process-local. Do not continue tests or unwind
            // through other USER operations if it cannot be restored.
            std::process::abort();
        }
    }
}

pub(super) fn controller_pipe() -> Result<(ControlServer, ControlAddress)> {
    let address = ControlAddress::fresh()?;
    let owner = crate::sid::current_user_sid()?;
    // Same protected principal rights as ControlServer, with an explicit Medium
    // label at birth for this test's elevated-controller/filtered-client pair.
    // No existing pipe, station, desktop or production descriptor is modified.
    let descriptor = OwnedSd::from_sddl(&format!(
        "O:{owner}D:P(A;;GA;;;SY)(A;;GA;;;{owner})(A;;0x{CLIENT_RIGHTS:x};;;{CLIENT})(A;;RC;;;OW)S:(ML;;NW;;;ME)"
    ))?;
    let security = SECURITY_ATTRIBUTES {
        nLength: size_of::<SECURITY_ATTRIBUTES>() as u32,
        lpSecurityDescriptor: descriptor.ptr.0,
        bInheritHandle: false.into(),
    };
    let raw = unsafe {
        CreateNamedPipeW(
            pcwstr(&wstr(&address.path()?)),
            PIPE_ACCESS_DUPLEX | FILE_FLAG_FIRST_PIPE_INSTANCE | FILE_FLAG_OVERLAPPED,
            PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
            1,
            (MAX_FRAME + HEADER) as u32,
            (MAX_FRAME + HEADER) as u32,
            0,
            Some(&security),
        )
    };
    ensure!(!raw.is_invalid(), "test controller pipe creation refused");
    let handle = OwnedHandle(raw);
    noninheritable(handle.raw())?;
    Ok((
        ControlServer {
            handle,
            nonce: address.nonce,
        },
        address,
    ))
}
