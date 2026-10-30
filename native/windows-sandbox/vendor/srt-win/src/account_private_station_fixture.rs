//! Disposable, fixed-purpose station feasibility only. No production module,
//! resource authority, desktop attachment, ACL publication or cold adoption.
use super::*;
use bootstrap::entry::credential_wire::child::{Purpose, probe::Request};
use serde::{Deserialize, Serialize};
use windows::Win32::Security::{
    Authorization::ConvertSecurityDescriptorToStringSecurityDescriptorW, *,
};
use windows::Win32::System::StationsAndDesktops::*;

const PREFIX: &[u8] = b"alysis-private-account-station-v1:";
const ACK: &[u8] = b"alysis-private-account-station-ack-v1:";
const LIMIT: usize = 32768;
const TIMEOUT: Duration = Duration::from_secs(5);
type TokenStamp = ([u32; 2], [u32; 2], [u32; 2]);

#[derive(Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum Stage {
    Original,
    Create,
    Query,
    Collision,
    Close,
    Unchanged,
    Complete,
}

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Receipt {
    request: Request,
    token_id: [u32; 2],
    authentication_id: [u32; 2],
    modified_id: [u32; 2],
    pub(crate) stage: Stage,
    pub(crate) hresult: Option<i32>,
    pub(crate) passed: bool,
    original_station: Option<String>,
    private_station: Option<String>,
    descriptor: Option<String>,
    expected_descriptor: Option<String>,
    collision_error: Option<u32>,
    noninheritable: bool,
    nonvisible: bool,
    closed: bool,
    original_associations_unchanged: bool,
    shared_descriptor_unchanged: bool,
}

thread_local! {
    pub(crate) static RECEIPT: std::cell::RefCell<Option<Receipt>> = const { std::cell::RefCell::new(None) };
}

fn require_request(request: &Request) -> Result<()> {
    request.validate()?;
    ensure!(
        request.birth.purpose == Purpose::FixturePrivateStationV1,
        "private station requires its exclusive test purpose"
    );
    Ok(())
}

impl Receipt {
    fn new(request: &Request, token_stamp: TokenStamp) -> Result<Self> {
        require_request(request)?;
        Ok(Self {
            request: request.clone(),
            token_id: token_stamp.0,
            authentication_id: token_stamp.1,
            modified_id: token_stamp.2,
            stage: Stage::Original,
            hresult: None,
            passed: false,
            original_station: None,
            private_station: None,
            descriptor: None,
            expected_descriptor: None,
            collision_error: None,
            noninheritable: false,
            nonvisible: false,
            closed: false,
            original_associations_unchanged: false,
            shared_descriptor_unchanged: false,
        })
    }
    fn require(&self, request: &Request, stamp: TokenStamp) -> Result<()> {
        require_request(request)?;
        ensure!(
            self.request == *request
                && (self.token_id, self.authentication_id, self.modified_id) == stamp,
            "private station receipt differs from original process/token"
        );
        for text in [&self.original_station, &self.private_station] {
            if let Some(text) = text {
                ensure!(
                    !text.is_empty() && text.len() <= 1024 && !text.contains('\0'),
                    "private station name exceeds bounds"
                );
            }
        }
        for text in [&self.descriptor, &self.expected_descriptor] {
            if let Some(text) = text {
                ensure!(
                    !text.is_empty() && text.len() <= 8192 && !text.contains('\0'),
                    "private station descriptor exceeds bounds"
                );
            }
        }
        if self.passed {
            ensure!(
                self.stage == Stage::Complete
                    && self.hresult.is_none()
                    && self.original_station.is_some()
                    && self.private_station.is_some()
                    && self.descriptor.is_some()
                    && self.descriptor == self.expected_descriptor
                    && self.collision_error == Some(ERROR_ALREADY_EXISTS.0)
                    && self.noninheritable
                    && self.nonvisible
                    && self.closed
                    && self.original_associations_unchanged
                    && self.shared_descriptor_unchanged,
                "private station success evidence incomplete"
            );
        } else {
            ensure!(
                self.stage != Stage::Complete,
                "failed station receipt claims completion"
            );
        }
        Ok(())
    }
    fn encode(&self) -> Result<Vec<u8>> {
        self.require(
            &self.request,
            (self.token_id, self.authentication_id, self.modified_id),
        )?;
        let mut bytes = PREFIX.to_vec();
        bytes.extend_from_slice(&serde_json::to_vec(self)?);
        ensure!(
            bytes.len() <= LIMIT,
            "private station receipt exceeds frame bound"
        );
        Ok(bytes)
    }
    fn decode(bytes: &[u8], request: &Request, stamp: TokenStamp) -> Result<Self> {
        ensure!(
            bytes.len() <= LIMIT && bytes.starts_with(PREFIX),
            "invalid station receipt frame"
        );
        let receipt: Self = serde_json::from_slice(&bytes[PREFIX.len()..])
            .map_err(|_| anyhow::anyhow!("invalid private station receipt"))?;
        receipt.require(request, stamp)?;
        Ok(receipt)
    }
}

fn acknowledgment(request: &Request) -> Result<Vec<u8>> {
    require_request(request)?;
    let mut bytes = ACK.to_vec();
    bytes.extend_from_slice(&serde_json::to_vec(request)?);
    ensure!(
        bytes.len() <= 6144,
        "private station acknowledgment exceeds bound"
    );
    Ok(bytes)
}

/// Called only after promotion of the authenticated original-source channel.
pub(super) fn client(channel: &mut ControlChannel<'_>, request: &Request) -> Result<()> {
    require_request(request)?;
    ensure!(!channel.server, "private station client role differs");
    channel.peer.verify(channel.handle.raw(), false)?;
    let current = ExpectedPeer::retain(unsafe { GetCurrentProcess() })?;
    let selection = &request.birth.selection;
    current.token.require_dedicated_runner(
        &selection.account.account_sid,
        selection.session,
        &selection.account.group_sid,
    )?;
    ensure!(
        current.pid == request.pid
            && bootstrap::pull::creation_time(current.process.raw())? == request.created,
        "private station current process differs from original birth"
    );
    let stamp = current.token.fixture_stamp(current.process.raw())?;
    let receipt = observe(request, stamp)?;
    current.alive()?;
    receipt.require(request, current.token.fixture_stamp(current.process.raw())?)?;
    channel.send(&receipt.encode()?, TIMEOUT)?;
    ensure!(
        channel.receive(TIMEOUT)? == acknowledgment(request)?,
        "private station receipt acknowledgment differs"
    );
    ensure!(
        receipt.passed,
        "original private station diagnostic refused"
    );
    Ok(())
}

/// The source keeps its original armed child and both pipes through this receipt.
pub(super) fn relay(
    broker: &mut ControlChannel<'_>,
    child: &mut ControlChannel<'_>,
    request: &Request,
    timeout: Duration,
) -> Result<()> {
    require_request(request)?;
    ensure!(
        child.server && !broker.server,
        "private station relay roles differ"
    );
    let bytes = child.receive(timeout)?;
    Receipt::decode(
        &bytes,
        request,
        child.peer.token.fixture_stamp(child.peer.process.raw())?,
    )?;
    broker.send(&bytes, timeout)?;
    let ack = broker.receive(timeout)?;
    ensure!(
        ack == acknowledgment(request)?,
        "private station broker acknowledgment differs"
    );
    child.send(&ack, timeout)
}

/// Only the broker's original account observer supplies the comparison token.
pub(crate) fn receive(
    channel: &mut ControlChannel<'_>,
    child: &ExpectedPeer<'_>,
    request: &Request,
    timeout: Duration,
) -> Result<()> {
    require_request(request)?;
    ensure!(channel.server, "private station broker role differs");
    child.alive()?;
    let bytes = channel.receive(timeout)?;
    child.alive()?;
    let receipt = Receipt::decode(
        &bytes,
        request,
        child.token.fixture_stamp(child.process.raw())?,
    )?;
    let passed = receipt.passed;
    RECEIPT.with(|slot| -> Result<()> {
        ensure!(
            slot.borrow().is_none(),
            "private station receipt already observed"
        );
        *slot.borrow_mut() = Some(receipt);
        Ok(())
    })?;
    channel.send(&acknowledgment(request)?, timeout)?;
    ensure!(passed, "original private station diagnostic refused");
    Ok(())
}

struct Station(Option<HWINSTA>);
impl Station {
    fn close(&mut self) -> Result<bool> {
        let Some(handle) = self.0.take() else {
            return Ok(false);
        };
        unsafe {
            CloseWindowStation(handle)?;
        }
        Ok(true)
    }
}
impl Drop for Station {
    fn drop(&mut self) {
        let _ = self.close();
    }
}

pub(super) fn text(handle: HANDLE, index: USER_OBJECT_INFORMATION_INDEX) -> Result<String> {
    let mut buffer = [0u16; 512];
    let mut bytes = 0;
    unsafe {
        GetUserObjectInformationW(
            handle,
            index,
            Some(buffer.as_mut_ptr().cast()),
            std::mem::size_of_val(&buffer) as u32,
            Some(&mut bytes),
        )?;
    }
    ensure!(
        bytes >= 2 && bytes as usize <= std::mem::size_of_val(&buffer) && bytes % 2 == 0,
        "station text exceeds bound"
    );
    let length = bytes as usize / 2;
    ensure!(
        buffer[length - 1] == 0 && !buffer[..length - 1].contains(&0),
        "invalid station text"
    );
    Ok(String::from_utf16(&buffer[..length - 1])?)
}

fn bounded_sddl(capacity: u32, mut read: impl FnMut(usize) -> u16) -> Result<String> {
    ensure!((1..=8192).contains(&capacity), "station SDDL exceeds bound");
    let mut prefix = Vec::new();
    for index in 0..capacity as usize {
        let word = read(index);
        if word == 0 {
            ensure!(!prefix.is_empty(), "empty station SDDL");
            return Ok(String::from_utf16(&prefix)?);
        }
        prefix.push(word);
    }
    anyhow::bail!("station SDDL missing bounded terminator")
}

pub(super) fn render(
    sd: PSECURITY_DESCRIPTOR,
    info: OBJECT_SECURITY_INFORMATION,
) -> Result<String> {
    let mut string = windows::core::PWSTR::null();
    let mut length = 0;
    unsafe {
        ConvertSecurityDescriptorToStringSecurityDescriptorW(
            sd,
            1,
            info,
            &mut string,
            Some(&mut length),
        )?;
    }
    let result = (|| -> Result<String> {
        ensure!(!string.is_null(), "station SDDL buffer missing");
        // The API reports allocation capacity, not string length. Read only
        // through the first bounded NUL; trailing allocation may be uninitialized.
        bounded_sddl(length, |index| unsafe { *string.0.add(index) })
    })();
    crate::util::local_free(string.0.cast());
    result
}

pub(super) fn security(
    handle: HANDLE,
    info: OBJECT_SECURITY_INFORMATION,
) -> Result<(Vec<u8>, String)> {
    let mut words = [0usize; 2048];
    let mut length = 0;
    let sd = PSECURITY_DESCRIPTOR(words.as_mut_ptr().cast());
    unsafe {
        GetUserObjectSecurity(
            handle,
            &info.0,
            Some(sd),
            std::mem::size_of_val(&words) as u32,
            &mut length,
        )?;
    }
    ensure!(
        length >= 20
            && length as usize <= std::mem::size_of_val(&words)
            && unsafe { IsValidSecurityDescriptor(sd) }.as_bool()
            && unsafe { GetSecurityDescriptorLength(sd) } == length,
        "invalid bounded station descriptor"
    );
    let raw = unsafe { std::slice::from_raw_parts(words.as_ptr().cast::<u8>(), length as usize) }
        .to_vec();
    Ok((raw, render(sd, info)?))
}

fn observe(request: &Request, stamp: TokenStamp) -> Result<Receipt> {
    let mut receipt = Receipt::new(request, stamp)?;
    let mut station = Station(None);
    let mut originals = None;
    let mut result = (|| -> Result<()> {
        crate::account_impersonation::require_process_identity()?;
        let original = unsafe { GetProcessWindowStation()? };
        let desktop = unsafe { GetThreadDesktop(GetCurrentThreadId())? };
        receipt.original_station = Some(text(HANDLE(original.0), UOI_NAME)?);
        let original_security = security(
            HANDLE(original.0),
            OWNER_SECURITY_INFORMATION
                | GROUP_SECURITY_INFORMATION
                | DACL_SECURITY_INFORMATION
                | LABEL_SECURITY_INFORMATION,
        )?
        .0;
        originals = Some((original, desktop, original_security));
        let owner = &request.birth.selection.account.account_sid;
        let sd = OwnedSd::from_sddl(&format!(
            "O:{owner}D:P(A;;0xf037f;;;SY)(A;;0xf037f;;;{owner})S:(ML;;NW;;;LW)"
        ))?;
        let info =
            OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION | LABEL_SECURITY_INFORMATION;
        receipt.expected_descriptor = Some(render(sd.ptr, info)?);
        let attributes = SECURITY_ATTRIBUTES {
            nLength: size_of::<SECURITY_ATTRIBUTES>() as u32,
            lpSecurityDescriptor: sd.ptr.0,
            bInheritHandle: false.into(),
        };
        receipt.stage = Stage::Create;
        // The null name is derived by Windows from this exact logon session.
        // Initial collision is a refusal; it must never become cold adoption.
        let handle = unsafe {
            CreateWindowStationW(windows::core::PCWSTR::null(), 1, 0xf037f, Some(&attributes))?
        };
        ensure!(
            handle.0 != original.0,
            "station creation returned borrowed original handle"
        );
        station.0 = Some(handle);
        receipt.stage = Stage::Query;
        ensure!(
            !unsafe { CompareObjectHandles(HANDLE(handle.0), HANDLE(original.0)) }.as_bool(),
            "private station is the existing process station"
        );
        receipt.private_station = Some(text(HANDLE(handle.0), UOI_NAME)?);
        ensure!(
            text(HANDLE(handle.0), UOI_TYPE)? == "WindowStation",
            "private object type differs"
        );
        receipt.descriptor = Some(security(HANDLE(handle.0), info)?.1);
        ensure!(
            receipt.descriptor == receipt.expected_descriptor,
            "private station policy differs"
        );
        let mut flags = USEROBJECTFLAGS::default();
        let mut bytes = 0;
        unsafe {
            GetUserObjectInformationW(
                HANDLE(handle.0),
                UOI_FLAGS,
                Some((&mut flags as *mut USEROBJECTFLAGS).cast()),
                size_of::<USEROBJECTFLAGS>() as u32,
                Some(&mut bytes),
            )?;
        }
        ensure!(
            bytes as usize == size_of::<USEROBJECTFLAGS>(),
            "station flags size differs"
        );
        receipt.noninheritable = !flags.fInherit.as_bool();
        receipt.nonvisible = flags.dwFlags & 1 == 0;
        ensure!(
            receipt.noninheritable && receipt.nonvisible,
            "private station flags differ"
        );
        receipt.stage = Stage::Collision;
        match unsafe {
            CreateWindowStationW(windows::core::PCWSTR::null(), 1, 0xf037f, Some(&attributes))
        } {
            Ok(other) => {
                // Own only this unexpected returned reference, never adopt it.
                let mut unexpected = Station(Some(other));
                unexpected.close()?;
                anyhow::bail!("exclusive station collision unexpectedly succeeded");
            }
            Err(error) => {
                receipt.collision_error = Some(error.code().0 as u32 & 0xffff);
                if error.code() != windows::core::HRESULT::from_win32(ERROR_ALREADY_EXISTS.0) {
                    return Err(error.into());
                }
            }
        }
        Ok(())
    })();
    if result.is_ok() {
        receipt.stage = Stage::Close;
    }
    let close = station.close();
    if let Ok(closed) = &close {
        receipt.closed = *closed;
    }
    if result.is_ok() {
        result = close.map(|_| ());
    }
    if result.is_ok() {
        receipt.stage = Stage::Unchanged;
    }
    if let Some((original, desktop, before)) = originals {
        let unchanged = (|| -> Result<()> {
            receipt.original_associations_unchanged = unsafe {
                CompareObjectHandles(HANDLE(original.0), HANDLE(GetProcessWindowStation()?.0))
                    .as_bool()
                    && CompareObjectHandles(
                        HANDLE(desktop.0),
                        HANDLE(GetThreadDesktop(GetCurrentThreadId())?.0),
                    )
                    .as_bool()
            };
            receipt.shared_descriptor_unchanged = security(
                HANDLE(original.0),
                OWNER_SECURITY_INFORMATION
                    | GROUP_SECURITY_INFORMATION
                    | DACL_SECURITY_INFORMATION
                    | LABEL_SECURITY_INFORMATION,
            )?
            .0 == before;
            ensure!(
                receipt.original_associations_unchanged && receipt.shared_descriptor_unchanged,
                "original USER association or security changed"
            );
            Ok(())
        })();
        if result.is_ok() {
            result = unchanged;
        }
    }
    match result {
        Ok(()) => {
            receipt.passed = true;
            receipt.stage = Stage::Complete;
        }
        Err(error) => {
            receipt.hresult = error
                .chain()
                .find_map(|cause| cause.downcast_ref::<windows::core::Error>())
                .map(|error| error.code().0);
        }
    }
    receipt.require(request, stamp)?;
    Ok(receipt)
}

#[path = "account_private_station_fixture_tests.rs"]
mod tests;
