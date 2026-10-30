//! Read-only token semantics and synthetic ACL comparisons. No impersonation,
//! object ACL writes, capability changes or service calls. These observations
//! cannot qualify the real RPC server descriptor or authorize token changes.
use super::*;
use windows::Win32::Security::*;

#[path = "account_token_lpac_observation.rs"]
mod lpac_attribute;

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Comparison {
    diagnostic_only: bool,
    raw: Snapshot,
    filtered: Snapshot,
    lowbox: Snapshot,
    original_child: Option<((u32, u64), Snapshot)>,
}

impl Comparison {
    pub(super) fn has_child(&self) -> bool {
        self.original_child.is_some()
    }
    pub(super) fn observe(filtered: HANDLE, lowbox: HANDLE) -> Result<Self> {
        crate::account_impersonation::require_process_identity()?;
        let mut raw = HANDLE::default();
        unsafe { OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY | TOKEN_DUPLICATE, &mut raw)? };
        let raw = OwnedHandle(raw);
        let raw_snapshot = Snapshot::observe(raw.raw())?;
        unsafe { CloseHandle(raw.into_raw()) }.context("token diagnostic raw close uncertain")?;
        Ok(Self {
            diagnostic_only: true,
            raw: raw_snapshot,
            filtered: Snapshot::observe(filtered)?,
            lowbox: Snapshot::observe(lowbox)?,
            original_child: None,
        })
    }

    pub(super) fn child(&mut self, token: HANDLE, original: (u32, u64)) -> Result<()> {
        ensure!(
            self.original_child.is_none(),
            "token child observation already present"
        );
        self.original_child = Some((original, Snapshot::observe(token)?));
        self.require(Some(original))
    }

    pub(super) fn require(&self, original: Option<(u32, u64)>) -> Result<()> {
        ensure!(
            self.diagnostic_only,
            "token diagnostic cannot supply authority"
        );
        for snapshot in [&self.raw, &self.filtered, &self.lowbox] {
            snapshot.require()?;
        }
        ensure!(
            !self.raw.appcontainer && !self.filtered.appcontainer && self.lowbox.appcontainer,
            "token comparison actor class differs"
        );
        if let Some((observed, snapshot)) = &self.original_child {
            snapshot.require()?;
            ensure!(
                Some(*observed) == original
                    && observed.0 != 0
                    && observed.1 != 0
                    && snapshot.appcontainer,
                "token observation differs from original lowbox child"
            );
        }
        Ok(())
    }
}

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Snapshot {
    ever_filtered: bool,
    is_restricted: bool,
    appcontainer: bool,
    less_privileged_appcontainer: Option<u32>,
    less_privileged_query_hresult: i32,
    no_all_app_packages: lpac_attribute::Observation,
    groups: Groups,
    restricting: Groups,
    synthetic_control: Access,
    synthetic_leading_rc_deny: Access,
    rpc_descriptor_model: RpcModel,
}

// An in-memory model of the seven fixed SSPISRV ACEs, not a read of the live
// service descriptor. Hosted run37954956113 shows Rpc AccessCheck uses
// MAXIMUM_ALLOWED and requires both TRUE and a nonzero granted mask. Owner and
// group are explicitly SYSTEM here; transport impersonation is not modeled.
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct RpcModel {
    diagnostic_only: bool,
    maximum_allowed: Access,
    read_control: Access,
}

fn rpc_descriptor_model(token: HANDLE) -> Result<RpcModel> {
    // The fixed public lpacIdentityServices SID was derived with the documented
    // DeriveCapabilitySidsFromName API. Merely placing it in this memory-only
    // model grants no capability to a token and changes no real object.
    const LPAC_IDENTITY: &str = "S-1-15-3-1024-1788129303-2183208577-3999474272-3147359985-1757322193-3815756386-151582180-1888101193";
    // Registration maps GR/GW to0x20001, GX to0x20000, GA to0x1f0001.
    // Preserve the observed input order, including the second-position deny.
    let descriptor = crate::util::OwnedSd::from_sddl(&format!(
        "O:SYG:SYD:(A;;0x20001;;;WD)(D;;0x20001;;;RC)(A;;0x20001;;;AN)(A;;0x1f0001;;;BA)(A;;0x1f0001;;;OW)(A;;0x20001;;;AC)(A;;0x20001;;;{LPAC_IDENTITY})"
    ))?;
    let mapping = GENERIC_MAPPING {
        GenericRead: 0x20000,
        GenericWrite: 0x20000,
        GenericExecute: 0x20000,
        GenericAll: 0x1f0000,
    };
    Ok(RpcModel {
        diagnostic_only: true,
        maximum_allowed: access(descriptor.ptr, token, &mapping, 0x02000000)?,
        read_control: access(descriptor.ptr, token, &mapping, 0x20000)?,
    })
}

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Groups {
    count: u32,
    restricted_code_attributes: Option<u32>,
    all_packages_attributes: Option<u32>,
}

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Access {
    allowed: bool,
    granted: u32,
}

impl Snapshot {
    fn require(&self) -> Result<()> {
        self.no_all_app_packages.require()?;
        ensure!(
            self.less_privileged_query_hresult <= 0
                && self.less_privileged_appcontainer.is_some()
                    == (self.less_privileged_query_hresult == 0)
                && self
                    .less_privileged_appcontainer
                    .is_none_or(|value| value <= 1),
            "LPAC observation cannot replace unsupported query with false"
        );
        for groups in [&self.groups, &self.restricting] {
            ensure!(
                groups.count <= 4096,
                "token group diagnostic count unbounded"
            );
            ensure!(
                groups.count != 0
                    || (groups.restricted_code_attributes.is_none()
                        && groups.all_packages_attributes.is_none()),
                "empty token groups carry a subject"
            );
        }
        ensure!(
            self.is_restricted == (self.restricting.count != 0),
            "restricted token list differs"
        );
        for access in [&self.synthetic_control, &self.synthetic_leading_rc_deny] {
            ensure!(
                access.granted <= 1 && access.allowed == (access.granted == 1),
                "synthetic access diagnostic differs from fixed test bit"
            );
        }
        let model = &self.rpc_descriptor_model;
        ensure!(model.diagnostic_only, "RPC model cannot supply authority");
        ensure!(
            model.maximum_allowed.granted & !0x1f0001 == 0
                && model.maximum_allowed.allowed == (model.maximum_allowed.granted != 0)
                && matches!(model.read_control.granted, 0 | 0x20000)
                && model.read_control.allowed == (model.read_control.granted == 0x20000),
            "RPC model mask or success observation differs"
        );
        Ok(())
    }

    fn observe(token: HANDLE) -> Result<Self> {
        let mapping = GENERIC_MAPPING {
            GenericRead: 1,
            GenericWrite: 2,
            GenericExecute: 4,
            GenericAll: 7,
        };
        // Deliberately synthetic and not the service's noncanonical descriptor.
        // The only difference is one leading Restricted Code deny for bit1.
        let control = crate::util::OwnedSd::from_sddl("O:SYG:SYD:(A;;0x1;;;WD)(A;;0x1;;;AC)")?;
        let deny =
            crate::util::OwnedSd::from_sddl("O:SYG:SYD:(D;;0x1;;;RC)(A;;0x1;;;WD)(A;;0x1;;;AC)")?;
        let mut duplicate = HANDLE::default();
        unsafe {
            DuplicateTokenEx(
                token,
                TOKEN_QUERY,
                None,
                SecurityImpersonation,
                TokenImpersonation,
                &mut duplicate,
            )?
        };
        let duplicate = OwnedHandle(duplicate);
        let control = access(control.ptr, duplicate.raw(), &mapping, 1)?;
        let deny = access(deny.ptr, duplicate.raw(), &mapping, 1)?;
        let rpc_descriptor_model = rpc_descriptor_model(duplicate.raw())?;
        unsafe { CloseHandle(duplicate.into_raw()) }
            .context("token diagnostic duplicate close uncertain")?;
        // Some hosts expose the enum but reject this information class. Keep
        // that diagnostic failure explicit; it must not become LPAC=false.
        let mut lpac = 0u32;
        let mut lpac_size = 0;
        let lpac_result = unsafe {
            GetTokenInformation(
                token,
                TokenIsLessPrivilegedAppContainer,
                Some((&mut lpac as *mut u32).cast()),
                4,
                &mut lpac_size,
            )
        };
        let lpac_status = lpac_result.err().map_or(0, |error| error.code().0);
        let snapshot = Self {
            ever_filtered: boolean(token, TokenHasRestrictions)?,
            is_restricted: restricted(token)?,
            appcontainer: boolean(token, TokenIsAppContainer)?,
            less_privileged_appcontainer: (lpac_status == 0).then_some(lpac),
            less_privileged_query_hresult: lpac_status,
            no_all_app_packages: lpac_attribute::Observation::observe(token),
            groups: groups(token, TokenGroups)?,
            restricting: groups(token, TokenRestrictedSids)?,
            synthetic_control: control,
            synthetic_leading_rc_deny: deny,
            rpc_descriptor_model,
        };
        snapshot.require()?;
        Ok(snapshot)
    }
}

fn access(
    sd: PSECURITY_DESCRIPTOR,
    token: HANDLE,
    mapping: &GENERIC_MAPPING,
    desired: u32,
) -> Result<Access> {
    let mut privileges = [0usize; 128];
    let mut length = std::mem::size_of_val(&privileges) as u32;
    let mut granted = 0;
    let mut allowed = windows::core::BOOL::default();
    unsafe {
        AccessCheck(
            sd,
            token,
            desired,
            mapping,
            Some(privileges.as_mut_ptr().cast()),
            &mut length,
            &mut granted,
            &mut allowed,
        )?
    };
    Ok(Access {
        allowed: allowed.as_bool(),
        granted,
    })
}

fn information(token: HANDLE, class: TOKEN_INFORMATION_CLASS) -> Result<Vec<usize>> {
    let mut bytes = 0;
    unsafe {
        let _ = GetTokenInformation(token, class, None, 0, &mut bytes);
    }
    ensure!(
        (1..=65536).contains(&bytes),
        "token diagnostic buffer size differs"
    );
    let mut words = vec![0usize; (bytes as usize).div_ceil(std::mem::size_of::<usize>())];
    let capacity = bytes;
    unsafe {
        GetTokenInformation(
            token,
            class,
            Some(words.as_mut_ptr().cast()),
            capacity,
            &mut bytes,
        )?
    };
    ensure!(bytes <= capacity, "token diagnostic result exceeds buffer");
    Ok(words)
}

fn boolean(token: HANDLE, class: TOKEN_INFORMATION_CLASS) -> Result<bool> {
    let info = information(token, class)?;
    // TokenHasRestrictions is returned as a one-byte BOOLEAN on this host.
    // TokenIsAppContainer is DWORD. Word backing keeps both reads aligned.
    let value = if class == TokenHasRestrictions {
        unsafe { *info.as_ptr().cast::<u8>() as u32 }
    } else {
        ensure!(
            class == TokenIsAppContainer,
            "unexpected boolean token class"
        );
        unsafe { *info.as_ptr().cast::<u32>() }
    };
    ensure!(value <= 1, "token diagnostic boolean differs");
    Ok(value == 1)
}

fn restricted(token: HANDLE) -> Result<bool> {
    // The generated Result wrapper cannot distinguish an ordinary FALSE from
    // an API error. Clear/check last-error around the actual BOOL API.
    use windows::Win32::Foundation::{ERROR_SUCCESS, GetLastError, SetLastError};
    #[link(name = "advapi32")]
    unsafe extern "system" {
        #[link_name = "IsTokenRestricted"]
        fn query_restricted(token: HANDLE) -> windows::core::BOOL;
    }
    unsafe { SetLastError(ERROR_SUCCESS) };
    let value = unsafe { query_restricted(token) }.as_bool();
    let error = unsafe { GetLastError() };
    if !value && error != ERROR_SUCCESS {
        return Err(windows::core::Error::from_hresult(error.to_hresult()).into());
    }
    Ok(value)
}

fn groups(token: HANDLE, class: TOKEN_INFORMATION_CLASS) -> Result<Groups> {
    let info = information(token, class)?;
    let count = unsafe { *info.as_ptr().cast::<u32>() };
    let bytes = std::mem::size_of_val(info.as_slice());
    let offset = std::mem::offset_of!(TOKEN_GROUPS, Groups);
    ensure!(
        count <= 4096
            && offset + count as usize * std::mem::size_of::<SID_AND_ATTRIBUTES>() <= bytes,
        "token diagnostic group array outside buffer"
    );
    let entries = unsafe {
        std::slice::from_raw_parts(
            info.as_ptr()
                .cast::<u8>()
                .add(offset)
                .cast::<SID_AND_ATTRIBUTES>(),
            count as usize,
        )
    };
    let rc = crate::sid::LocalPsid::from_string("S-1-5-12")?;
    let packages = crate::sid::LocalPsid::from_string("S-1-15-2-1")?;
    let mut result = Groups {
        count,
        restricted_code_attributes: None,
        all_packages_attributes: None,
    };
    for entry in entries {
        let begin = info.as_ptr() as usize;
        let pointer = entry.Sid.0 as usize;
        ensure!(
            pointer >= begin
                && pointer
                    .checked_add(8)
                    .is_some_and(|end| end <= begin + bytes),
            "token diagnostic SID header outside buffer"
        );
        let subauthorities = unsafe { *(entry.Sid.0.cast::<u8>().add(1)) } as usize;
        ensure!(
            pointer + 8 + subauthorities * 4 <= begin + bytes,
            "token diagnostic SID outside buffer"
        );
        for (expected, output) in [
            (rc.as_psid(), &mut result.restricted_code_attributes),
            (packages.as_psid(), &mut result.all_packages_attributes),
        ] {
            if unsafe { EqualSid(entry.Sid, expected) }.is_ok() {
                ensure!(output.is_none(), "token diagnostic fixed SID repeated");
                *output = Some(entry.Attributes);
            }
        }
    }
    Ok(result)
}

#[test]
fn station_lowbox_token_semantics_keep_filtering_separate_from_restricting_sids() {
    let snapshot = Snapshot {
        ever_filtered: true,
        is_restricted: false,
        appcontainer: true,
        less_privileged_appcontainer: None,
        less_privileged_query_hresult: 0x80070057u32 as i32,
        no_all_app_packages: lpac_attribute::Observation::QueryFailed {
            hresult: 0x80070057u32 as i32,
        },
        groups: Groups {
            count: 2,
            restricted_code_attributes: None,
            all_packages_attributes: Some(4),
        },
        restricting: Groups {
            count: 0,
            restricted_code_attributes: None,
            all_packages_attributes: None,
        },
        synthetic_control: Access {
            allowed: true,
            granted: 1,
        },
        synthetic_leading_rc_deny: Access {
            allowed: false,
            granted: 0,
        },
        rpc_descriptor_model: RpcModel {
            diagnostic_only: true,
            maximum_allowed: Access {
                allowed: true,
                granted: 0x20001,
            },
            read_control: Access {
                allowed: true,
                granted: 0x20000,
            },
        },
    };
    snapshot.require().unwrap();
    let mut wrong = snapshot.clone();
    wrong.is_restricted = true;
    assert!(wrong.require().is_err());
    let mut wrong = snapshot.clone();
    wrong.restricting.restricted_code_attributes = Some(4);
    assert!(wrong.require().is_err());
    let mut wrong = snapshot.clone();
    wrong.synthetic_control.granted = 7;
    assert!(wrong.require().is_err());
    let mut wrong = snapshot.clone();
    wrong.rpc_descriptor_model.maximum_allowed.granted |= 0x80000000;
    assert!(wrong.require().is_err());
    let mut wrong = snapshot.clone();
    wrong.rpc_descriptor_model.read_control.granted = 0;
    assert!(wrong.require().is_err());
    let mut value = serde_json::to_value(&snapshot).unwrap();
    value["token_handle"] = 1.into();
    assert!(serde_json::from_value::<Snapshot>(value).is_err());
}

#[test]
fn station_lowbox_token_observation_queries_local_token_without_account_or_object_effects() {
    crate::account_impersonation::require_process_identity().unwrap();
    let mut raw = HANDLE::default();
    unsafe {
        OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY | TOKEN_DUPLICATE, &mut raw).unwrap()
    };
    let raw = OwnedHandle(raw);
    let snapshot = Snapshot::observe(raw.raw()).unwrap();
    assert!(snapshot.groups.count > 0);
    snapshot.require().unwrap();
    // Read-only synthetic descriptor comparison, no interpretation of the
    // host token as a stand-in for the unqualified account lowbox.
    unsafe { CloseHandle(raw.into_raw()).unwrap() };
    crate::account_impersonation::require_process_identity().unwrap();
}
