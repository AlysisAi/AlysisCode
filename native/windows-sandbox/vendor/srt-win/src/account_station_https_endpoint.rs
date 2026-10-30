//! Fixed local endpoint-mapper comparison after the HTTP observation. Resolving
//! this interface does not invoke a firewall procedure or print its endpoint.
use anyhow::{Result, ensure};
use serde::{Deserialize, Serialize};
use std::{ffi::c_void, time::Instant};
use windows::{
    Win32::System::Rpc::*,
    core::{GUID, w},
};

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Observation {
    interface: String,
    binding_status: i32,
    binding_returned: bool,
    timeout_status: Option<i32>,
    resolve_status: Option<i32>,
    free_status: Option<i32>,
    elapsed_ms: u64,
}

const INTERFACE: &str = "7f9d11bf-7fb9-436b-a812-b2d50c5d4c03:1.0";
fn interface() -> RPC_CLIENT_INTERFACE {
    RPC_CLIENT_INTERFACE {
        Length: std::mem::size_of::<RPC_CLIENT_INTERFACE>() as u32,
        InterfaceId: RPC_SYNTAX_IDENTIFIER {
            SyntaxGUID: GUID::from_u128(0x7f9d11bf_7fb9_436b_a812_b2d50c5d4c03),
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
            "HTTPS endpoint observation identity/bound differs"
        );
        ensure!(
            self.binding_returned == (self.binding_status == 0)
                && self.timeout_status.is_some() == self.binding_returned
                && self.free_status.is_some() == self.binding_returned
                && self.resolve_status.is_some() == (self.timeout_status == Some(0)),
            "HTTPS endpoint observation lacks binding, timeout or closure"
        );
        Ok(())
    }

    pub(super) fn observe() -> Self {
        let start = Instant::now();
        let spec = interface();
        let mut binding: *mut c_void = std::ptr::null_mut();
        // Fresh partially-bound local RPC handle. No endpoint, server address,
        // object UUID, auth credentials or caller-selected interface is accepted.
        let binding_status =
            unsafe { RpcBindingFromStringBindingW(w!("ncalrpc:"), &mut binding) }.0;
        let returned = binding_status == 0 && !binding.is_null();
        let (mut timeout_status, mut resolve_status, mut free_status) = (None, None, None);
        if returned {
            timeout_status =
                Some(unsafe { RpcBindingSetOption(binding, RPC_C_OPT_CALL_TIMEOUT, 1000) }.0);
            if timeout_status == Some(0) {
                // Only interface/transfer identities are consumed by resolution;
                // there is no procedure table or NDR call. The original parent
                // job deadline is still authoritative if the RPC runtime stalls.
                resolve_status = Some(
                    unsafe {
                        RpcEpResolveBinding(binding, (&spec as *const RPC_CLIENT_INTERFACE).cast())
                    }
                    .0,
                );
            }
            free_status = Some(unsafe { RpcBindingFree(&mut binding) }.0);
        }
        Self {
            interface: INTERFACE.into(),
            binding_status,
            binding_returned: returned,
            timeout_status,
            resolve_status,
            free_status,
            elapsed_ms: start.elapsed().as_millis().min(u64::MAX as u128) as u64,
        }
    }
}

#[test]
fn station_https_endpoint_proof_requires_fixed_identity_and_owned_binding_closure() {
    let valid = Observation {
        interface: INTERFACE.into(),
        binding_status: 0,
        binding_returned: true,
        timeout_status: Some(0),
        resolve_status: Some(1753),
        free_status: Some(0),
        elapsed_ms: 2,
    };
    valid.require().unwrap();
    for (key, value) in [
        ("interface", serde_json::json!("other:1.0")),
        ("binding_returned", serde_json::json!(false)),
        ("timeout_status", serde_json::Value::Null),
        ("free_status", serde_json::Value::Null),
        ("resolve_status", serde_json::Value::Null),
        ("endpoint", serde_json::json!("untrusted")),
    ] {
        let mut wire = serde_json::to_value(&valid).unwrap();
        wire[key] = value;
        assert!(!serde_json::from_value::<Observation>(wire).is_ok_and(|p| p.require().is_ok()));
    }
    let spec = interface();
    assert_eq!(spec.Length, 96);
    assert!(spec.DispatchTable.is_null() && spec.InterpreterInfo.is_null());
    assert!(spec.RpcProtseqEndpoint.is_null() && spec.RpcProtseqEndpointCount == 0);
}
