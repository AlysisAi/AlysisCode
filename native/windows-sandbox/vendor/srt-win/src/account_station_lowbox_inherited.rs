//! Fixed startup experiment: transfer only USER rights granted by the lowbox.
//! The original full-access handles remain private and noninheritable.
use super::*;
const STATION: u32 = PRIVATE_STATION_ACCESS;
const DESKTOP: u32 = 0x20083;

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Proof {
    diagnostic_only: bool,
    containment_qualified: bool,
    selected_lowbox_opens: bool,
    original_station: bool,
    original_desktop: bool,
    station_access: u32,
    desktop_access: u32,
    station_inheritable: bool,
    desktop_inheritable: bool,
    full_originals_noninheritable: bool,
    associations_unchanged: bool,
    parent_copies_closed: bool,
    child_station_received: bool,
    child_desktop_received: bool,
}
impl Proof {
    pub(crate) fn require(&self) -> Result<()> {
        ensure!(
            self.diagnostic_only && !self.containment_qualified,
            "inherited USER observation grants no containment qualification"
        );
        ensure!(
            self.selected_lowbox_opens
                && self.original_station
                && self.original_desktop
                && self.station_access == STATION
                && self.desktop_access == DESKTOP
                && self.station_inheritable
                && self.desktop_inheritable
                && self.full_originals_noninheritable
                && self.associations_unchanged,
            "inherited USER selection is not the exact limited original pair"
        );
        Ok(())
    }
}
pub(super) struct Pair {
    desktop: Desktop,
    station: Station,
    proof: Proof,
}
fn inheritable(handle: HANDLE) -> Result<bool> {
    let mut flags = USEROBJECTFLAGS::default();
    let mut length = 0;
    unsafe {
        GetUserObjectInformationW(
            handle,
            UOI_FLAGS,
            Some((&mut flags as *mut USEROBJECTFLAGS).cast()),
            size_of::<USEROBJECTFLAGS>() as u32,
            Some(&mut length),
        )?;
    }
    ensure!(
        length as usize == size_of::<USEROBJECTFLAGS>(),
        "USER inheritance flags size differs"
    );
    Ok(flags.fInherit.as_bool())
}
impl Pair {
    pub(super) fn prepare(users: &UserPair, token: &OwnedHandle) -> Result<Self> {
        crate::account_impersonation::require_process_identity()?;
        let original_station = users.station.raw()?;
        let original_desktop = users.desktop.0.context("original desktop absent")?;
        ensure!(
            !inheritable(HANDLE(original_station.0))? && !inheritable(HANDLE(original_desktop.0))?,
            "full USER originals must not be inheritable"
        );
        let path = user_access::original_name(HANDLE(original_station.0))?;
        let module = unsafe { GetModuleHandleW(w!("win32u.dll"))? };
        type OpenStation = unsafe extern "system" fn(*const OBJECT_ATTRIBUTES, u32) -> HWINSTA;
        let open: OpenStation = unsafe {
            std::mem::transmute(
                GetProcAddress(module, s!("NtUserOpenWindowStation"))
                    .context("native station open missing")?,
            )
        };
        let pair = crate::account_impersonation::with_token(token, || {
            let (mut buffer, length) = crate::util::counted_utf16(&path);
            let mut name = UNICODE_STRING {
                Length: length,
                MaximumLength: length,
                Buffer: PWSTR(buffer.as_mut_ptr()),
            };
            let mut attrs = object_attributes(&mut name, HWINSTA::default(), None);
            attrs.Attributes.0 |= OBJ_INHERIT.0;
            let raw = unsafe { open(&attrs, STATION) };
            if raw.is_invalid() {
                return Err(windows::core::Error::from_thread().into());
            }
            let station = Station(Some(raw));
            ensure!(
                unsafe { CompareObjectHandles(HANDLE(raw.0), HANDLE(original_station.0)) }
                    .as_bool(),
                "inherited station is not original"
            );
            ensure!(
                user_access::granted(HANDLE(raw.0))? == STATION && inheritable(HANDLE(raw.0))?,
                "inherited station rights or flags differ"
            );
            let (mut buffer, length) = crate::util::counted_utf16(&users.desktop_name);
            let mut name = UNICODE_STRING {
                Length: length,
                MaximumLength: length,
                Buffer: PWSTR(buffer.as_mut_ptr()),
            };
            let mut attrs = object_attributes(&mut name, raw, None);
            attrs.Attributes.0 |= OBJ_INHERIT.0;
            let desk = unsafe { (users.open)(&attrs, 0, DESKTOP) };
            if desk.is_invalid() {
                return Err(windows::core::Error::from_thread().into());
            }
            let desktop = Desktop(Some(desk));
            ensure!(
                unsafe { CompareObjectHandles(HANDLE(desk.0), HANDLE(original_desktop.0)) }
                    .as_bool(),
                "inherited desktop is not original"
            );
            ensure!(
                user_access::granted(HANDLE(desk.0))? == DESKTOP && inheritable(HANDLE(desk.0))?,
                "inherited desktop rights or flags differ"
            );
            Ok(Pair {
                desktop,
                station,
                proof: Proof {
                    diagnostic_only: true,
                    containment_qualified: false,
                    selected_lowbox_opens: true,
                    original_station: true,
                    original_desktop: true,
                    station_access: STATION,
                    desktop_access: DESKTOP,
                    station_inheritable: true,
                    desktop_inheritable: true,
                    full_originals_noninheritable: true,
                    associations_unchanged: false,
                    parent_copies_closed: false,
                    child_station_received: false,
                    child_desktop_received: false,
                },
            })
        })?;
        let mut pair = pair;
        pair.proof.associations_unchanged = unsafe {
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
        pair.proof.full_originals_noninheritable =
            !inheritable(HANDLE(original_station.0))? && !inheritable(HANDLE(original_desktop.0))?;
        pair.proof.require()?;
        Ok(pair)
    }
    pub(super) fn proof(&self) -> &Proof {
        &self.proof
    }
    pub(super) fn verify_child(&mut self, child: HANDLE) -> Result<()> {
        // Check the original suspended child's actual handle table, not just
        // the requested inheritance list. No source handle is closed or changed.
        let station = self.station.raw()?;
        let desktop = self.desktop.0.context("inherited desktop closed")?;
        let mut raw = HANDLE::default();
        unsafe {
            DuplicateHandle(
                child,
                HANDLE(station.0),
                GetCurrentProcess(),
                &mut raw,
                0,
                false,
                DUPLICATE_SAME_ACCESS,
            )?;
        }
        let mut copy = Station(Some(HWINSTA(raw.0)));
        let matches = unsafe { CompareObjectHandles(raw, HANDLE(station.0)) }.as_bool()
            && user_access::granted(raw)? == STATION;
        copy.close()?;
        ensure!(
            matches,
            "original child did not receive exact limited station"
        );
        self.proof.child_station_received = true;
        let mut raw = HANDLE::default();
        unsafe {
            DuplicateHandle(
                child,
                HANDLE(desktop.0),
                GetCurrentProcess(),
                &mut raw,
                0,
                false,
                DUPLICATE_SAME_ACCESS,
            )?;
        }
        let mut copy = Desktop(Some(HDESK(raw.0)));
        let matches = unsafe { CompareObjectHandles(raw, HANDLE(desktop.0)) }.as_bool()
            && user_access::granted(raw)? == DESKTOP;
        copy.close()?;
        ensure!(
            matches,
            "original child did not receive exact limited desktop"
        );
        self.proof.child_desktop_received = true;
        Ok(())
    }
    pub(super) fn handles(&self) -> Result<[HANDLE; 2]> {
        self.proof.require()?;
        let station = HANDLE(self.station.raw()?.0);
        let desktop = HANDLE(self.desktop.0.context("inherited desktop closed")?.0);
        ensure!(
            user_access::granted(station)? == STATION
                && user_access::granted(desktop)? == DESKTOP
                && inheritable(station)?
                && inheritable(desktop)?,
            "inherited USER pair changed before birth"
        );
        Ok([station, desktop])
    }
    pub(super) fn close(&mut self) -> Result<()> {
        self.desktop.close()?;
        self.station.close()?;
        self.proof.parent_copies_closed = true;
        Ok(())
    }
}
#[test]
fn station_lowbox_inherited_pair_refuses_full_access_and_false_authority() {
    let proof = Proof {
        diagnostic_only: true,
        containment_qualified: false,
        selected_lowbox_opens: true,
        original_station: true,
        original_desktop: true,
        station_access: STATION,
        desktop_access: DESKTOP,
        station_inheritable: true,
        desktop_inheritable: true,
        full_originals_noninheritable: true,
        associations_unchanged: true,
        parent_copies_closed: false,
        child_station_received: false,
        child_desktop_received: false,
    };
    proof.require().unwrap();
    for field in [
        "containment_qualified",
        "original_station",
        "original_desktop",
        "station_inheritable",
        "desktop_inheritable",
        "full_originals_noninheritable",
        "associations_unchanged",
        "selected_lowbox_opens",
    ] {
        let mut value = serde_json::to_value(&proof).unwrap();
        value[field] = (!value[field].as_bool().unwrap()).into();
        assert!(
            serde_json::from_value::<Proof>(value)
                .unwrap()
                .require()
                .is_err()
        );
    }
    for (field, mask) in [
        ("station_access", 0x20003), // Missing the kernel's AppContainer check.
        ("station_access", STATION | 0x4), // Clipboard.
        ("station_access", STATION | 0x8), // Create desktops.
        ("station_access", STATION | 0x10), // Write attributes.
        ("station_access", STATION | 0x40), // Exit Windows.
        ("station_access", STATION | 0x40000), // Write DACL.
        ("station_access", STATION | 0x80000), // Write owner.
        ("station_access", 0xf037f),
        ("desktop_access", 0xf01ff),
    ] {
        let mut value = serde_json::to_value(&proof).unwrap();
        value[field] = mask.into();
        assert!(
            serde_json::from_value::<Proof>(value)
                .unwrap()
                .require()
                .is_err()
        );
    }
    let mut value = serde_json::to_value(&proof).unwrap();
    value["resume_authorized"] = true.into();
    assert!(serde_json::from_value::<Proof>(value).is_err());
}
