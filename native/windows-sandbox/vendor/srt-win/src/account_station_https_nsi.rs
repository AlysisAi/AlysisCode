//! Fixed NSI mapper and read-only RPC management comparison, after WinHTTP.
//! No NSI application procedure, caller endpoint or explicit credentials.
use super::rpc_error;
use anyhow::{Result, ensure};
use serde::{Deserialize, Serialize};
use std::{ffi::c_void, time::Instant};
use windows::{
    Win32::System::Rpc::*,
    core::{GUID, PCWSTR, PWSTR, w},
};

const INTERFACE: &str = "7ea70bcf-48af-4f6a-8968-6a440754d5fa:1.0";
// rpcdce.h constant; windows-rs exposes it under the separate COM feature.
const RPC_C_AUTHN_LEVEL_PKT_PRIVACY: u32 = 6;
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Observation {
    interface: String,
    compose_status: i32,
    string_returned: bool,
    string_free_status: Option<i32>,
    binding_status: Option<i32>,
    binding_returned: bool,
    timeout_status: Option<i32>,
    auth_status: Option<i32>,
    resolve_status: Option<i32>,
    listening_status: Option<i32>,
    rpc_before: Option<rpc_error::Observation>,
    rpc_after: Option<rpc_error::Observation>,
    free_status: Option<i32>,
    elapsed_ms: u64,
}
fn interface() -> RPC_CLIENT_INTERFACE {
    RPC_CLIENT_INTERFACE {
        Length: std::mem::size_of::<RPC_CLIENT_INTERFACE>() as u32,
        InterfaceId: RPC_SYNTAX_IDENTIFIER {
            SyntaxGUID: GUID::from_u128(0x7ea70bcf_48af_4f6a_8968_6a440754d5fa),
            SyntaxVersion: RPC_VERSION {
                MajorVersion: 1,
                MinorVersion: 0,
            },
        },
        TransferSyntax: RPC_SYNTAX_IDENTIFIER {
            SyntaxGUID: GUID::from_u128(0x8a885d04_1ceb_11c9_9fe8_08002b104860),
            SyntaxVersion: RPC_VERSION {
                MajorVersion: 2,
                MinorVersion: 0,
            },
        },
        ..Default::default()
    }
}
impl Observation {
    pub(super) fn require(&self) -> Result<()> {
        ensure!(
            self.interface == INTERFACE && self.elapsed_ms <= 120000,
            "NSI observation identity/bound differs"
        );
        ensure!(
            self.string_returned == (self.compose_status == 0)
                && self.string_free_status.is_some() == self.string_returned
                && self.binding_status.is_some() == self.string_returned
                && self.binding_returned == (self.binding_status == Some(0))
                && self.timeout_status.is_some() == self.binding_returned
                && self.free_status.is_some() == self.binding_returned
                && self.auth_status.is_some() == (self.timeout_status == Some(0))
                && self.resolve_status.is_some() == (self.auth_status == Some(0))
                && self.listening_status.is_some() == (self.resolve_status == Some(0))
                && self.rpc_before.is_some() == self.listening_status.is_some()
                && self.rpc_after.is_some() == self.listening_status.is_some(),
            "NSI observation lacks staged call or owned handle closure"
        );
        for observation in [&self.rpc_before, &self.rpc_after].into_iter().flatten() {
            observation.require()?;
        }
        Ok(())
    }
    pub(super) fn observe() -> Self {
        let start = Instant::now();
        let mut string = PWSTR::null();
        // Matches the locally observed WINNSI binding's fixed security options.
        let compose_status = unsafe {
            RpcStringBindingComposeW(
                PCWSTR::null(),
                w!("ncalrpc"),
                PCWSTR::null(),
                PCWSTR::null(),
                w!("Security=Impersonation Dynamic True"),
                Some(&mut string),
            )
        }
        .0;
        let mut p = Self {
            interface: INTERFACE.into(),
            compose_status,
            string_returned: compose_status == 0 && !string.is_null(),
            string_free_status: None,
            binding_status: None,
            binding_returned: false,
            timeout_status: None,
            auth_status: None,
            resolve_status: None,
            listening_status: None,
            rpc_before: None,
            rpc_after: None,
            free_status: None,
            elapsed_ms: 0,
        };
        if p.string_returned {
            let mut binding: *mut c_void = std::ptr::null_mut();
            p.binding_status =
                Some(unsafe { RpcBindingFromStringBindingW(PCWSTR(string.0), &mut binding) }.0);
            p.string_free_status = Some(unsafe { RpcStringFreeW(&mut string) }.0);
            p.binding_returned = p.binding_status == Some(0) && !binding.is_null();
            if p.binding_returned {
                p.timeout_status =
                    Some(unsafe { RpcBindingSetOption(binding, RPC_C_OPT_CALL_TIMEOUT, 1000) }.0);
                if p.timeout_status == Some(0) {
                    // Current fixed fixture account context; no credential input.
                    p.auth_status = Some(
                        unsafe {
                            RpcBindingSetAuthInfoW(
                                binding,
                                PCWSTR::null(),
                                RPC_C_AUTHN_LEVEL_PKT_PRIVACY,
                                RPC_C_AUTHN_WINNT,
                                None,
                                RPC_C_AUTHZ_NONE,
                            )
                        }
                        .0,
                    );
                }
                if p.auth_status == Some(0) {
                    let spec = interface();
                    p.resolve_status = Some(
                        unsafe {
                            RpcEpResolveBinding(
                                binding,
                                (&spec as *const RPC_CLIENT_INTERFACE).cast(),
                            )
                        }
                        .0,
                    );
                }
                if p.resolve_status == Some(0) {
                    p.rpc_before = Some(rpc_error::Observation::observe());
                    // Read-only built-in management procedure to the resolved
                    // local server. Its authorization/autolisten behavior differs
                    // from NSI: a failure is NOT evidence NSI is unavailable.
                    p.listening_status = Some(unsafe { RpcMgmtIsServerListening(Some(binding)) }.0);
                    p.rpc_after = Some(rpc_error::Observation::observe());
                }
                p.free_status = Some(unsafe { RpcBindingFree(&mut binding) }.0);
            }
        }
        p.elapsed_ms = start.elapsed().as_millis().min(u64::MAX as u128) as u64;
        p
    }
}

#[test]
fn station_https_nsi_proof_rejects_missing_closure_and_skipped_stages() {
    // A recorded setup failure is valid diagnostic data, never a success claim.
    let p = Observation {
        interface: INTERFACE.into(),
        compose_status: 0,
        string_returned: true,
        string_free_status: Some(0),
        binding_status: Some(0),
        binding_returned: true,
        timeout_status: Some(0),
        auth_status: Some(5),
        resolve_status: None,
        listening_status: None,
        rpc_before: None,
        rpc_after: None,
        free_status: Some(0),
        elapsed_ms: 2,
    };
    p.require().unwrap();
    for (key, value) in [
        ("interface", serde_json::json!("other:1.0")),
        ("string_free_status", serde_json::Value::Null),
        ("free_status", serde_json::Value::Null),
        ("resolve_status", serde_json::json!(0)),
        ("listening_status", serde_json::json!(0)),
        ("auth_status", serde_json::json!(0)),
        ("endpoint", serde_json::json!("caller supplied")),
    ] {
        let mut wire = serde_json::to_value(&p).unwrap();
        wire[key] = value;
        assert!(!serde_json::from_value::<Observation>(wire).is_ok_and(|v| v.require().is_ok()));
    }
    let spec = interface();
    assert_eq!(spec.Length, 96);
    assert!(spec.DispatchTable.is_null() && spec.InterpreterInfo.is_null());
    assert!(spec.RpcProtseqEndpoint.is_null() && spec.RpcProtseqEndpointCount == 0);
}
