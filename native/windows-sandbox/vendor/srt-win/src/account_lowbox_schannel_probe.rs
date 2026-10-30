//! Fixed offline compatibility observation, after the original child is verified.
//! No certificate, private key, connection, security context or network token.
//! Credential handles never enter a receipt. Acquisition alone cannot prove TLS.
use super::*;
use windows::Win32::Security::{Authentication::Identity::*, Credentials::SecHandle};
use windows::core::PCWSTR;

use super::rpc_error;

#[path = "account_rpc_server_descriptor.rs"]
mod server_descriptor;

#[path = "account_lowbox_self_lsa.rs"]
mod self_lsa;

const FLAGS: u32 = SCH_CRED_NO_DEFAULT_CREDS.0
    | SCH_CRED_CACHE_ONLY_URL_RETRIEVAL_ON_CREATE.0
    | SCH_USE_STRONG_CRYPTO.0;

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Proof {
    pub(super) passed: bool,
    flags: u32,
    acquire_status: i32,
    release_status: Option<i32>,
    package_query_status: i32,
    package_buffer_returned: bool,
    package_buffer_free_status: Option<i32>,
    dependencies: Dependencies,
    https_qualified: bool,
    network_io_attempted: bool,
    #[serde(default)]
    self_lsa: Option<self_lsa::Comparison>,
    #[serde(default)]
    self_lsa_error: Option<i32>,
}
impl Proof {
    pub(super) fn failure_status(&self) -> Option<i32> {
        if self.acquire_status < 0 {
            Some(self.acquire_status)
        } else {
            self.release_status
                .filter(|status| *status < 0)
                .or((self.package_query_status < 0).then_some(self.package_query_status))
                .or(self.package_buffer_free_status.filter(|status| *status < 0))
                .or(self.self_lsa_error)
        }
    }
    pub(super) fn require(&self) -> Result<()> {
        self.dependencies.require()?;
        if let Some(comparison) = &self.self_lsa {
            comparison.require()?;
        }
        ensure!(
            self.self_lsa_error.is_none_or(|status| status < 0)
                && !(self.self_lsa.is_some() && self.self_lsa_error.is_some()),
            "self-token comparison cannot report both completed observation and error"
        );
        ensure!(
            self.flags == FLAGS && !self.https_qualified && !self.network_io_attempted,
            "Schannel initialization cannot qualify HTTPS or change fixed credential policy"
        );
        ensure!(
            self.acquire_status <= 0
                && self.release_status.is_some() == (self.acquire_status == 0)
                && self.release_status.is_none_or(|s| s <= 0)
                && self.package_query_status <= 0
                && (!self.package_buffer_returned || self.package_query_status == 0)
                && self.package_buffer_free_status.is_some() == self.package_buffer_returned
                && self.package_buffer_free_status.is_none_or(|s| s <= 0)
                && self.passed
                    == (self.acquire_status == 0
                        && self.release_status == Some(0)
                        && self.package_query_status == 0
                        && self.package_buffer_returned
                        && self.package_buffer_free_status == Some(0)
                        && self.self_lsa_error.is_none()
                        && self.dependencies.closed()),
            "Schannel acquire/release evidence contradicts success"
        );
        Ok(())
    }
}

// Status-only diagnostic, after the fixed provider attempt. No module load,
// process access, authentication package request, secret, logon or network I/O.
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Dependencies {
    diagnostic_only: bool,
    schannel_module_status: i32,
    lsa_connect_ntstatus: i32,
    lsa_handle_returned: bool,
    lsa_close_ntstatus: Option<i32>,
    lsa_ready_event: ReadyEvent,
    lsa_rpc_error: Option<rpc_error::Observation>,
    #[serde(default)]
    rpc_server_descriptor: Option<server_descriptor::Observation>,
}
impl Dependencies {
    fn closed(&self) -> bool {
        self.lsa_connect_ntstatus == 0
            && self.lsa_handle_returned
            && self.lsa_close_ntstatus == Some(0)
            && self.lsa_ready_event.passed()
    }
    fn require(&self) -> Result<()> {
        self.lsa_ready_event.require()?;
        if let Some(error) = &self.lsa_rpc_error {
            error.require()?;
        }
        if let Some(descriptor) = &self.rpc_server_descriptor {
            descriptor.require()?;
        }
        ensure!(
            self.diagnostic_only
                && self.lsa_rpc_error.is_some() == (self.lsa_connect_ntstatus < 0)
                && self.schannel_module_status <= 0
                && self.lsa_connect_ntstatus <= 0
                && (!self.lsa_handle_returned || self.lsa_connect_ntstatus == 0)
                && self.lsa_close_ntstatus.is_some() == self.lsa_handle_returned
                && self.lsa_close_ntstatus.is_none_or(|s| s <= 0),
            "LSA diagnostic has invalid status, cleanup or authority"
        );
        Ok(())
    }
    fn observe() -> Self {
        use windows::Win32::System::LibraryLoader::GetModuleHandleW;
        // Borrowed module query only, before the additional LSA connection.
        let schannel_module_status = unsafe { GetModuleHandleW(windows::core::w!("schannel.dll")) }
            .err()
            .map_or(0, |e| e.code().0);
        let mut handle = windows::Win32::Foundation::HANDLE::default();
        let lsa_connect_ntstatus = unsafe { LsaConnectUntrusted(&mut handle) }.0;
        // Snapshot immediately on failure, before another RPC call can replace
        // thread error state. Empty diagnostics do not explain the denial.
        let lsa_rpc_error = (lsa_connect_ntstatus < 0).then(rpc_error::Observation::observe);
        let lsa_handle_returned = lsa_connect_ntstatus == 0 && !handle.is_invalid();
        // LSA handles require this close API; never use CloseHandle. Free once
        // immediately, without any fallible allocation or early return.
        let lsa_close_ntstatus = if lsa_handle_returned {
            Some(unsafe { LsaDeregisterLogonProcess(handle) }.0)
        } else {
            None
        };
        Self {
            diagnostic_only: true,
            schannel_module_status,
            lsa_connect_ntstatus,
            lsa_handle_returned,
            lsa_close_ntstatus,
            lsa_ready_event: ReadyEvent::observe(),
            lsa_rpc_error,
            rpc_server_descriptor: Some(server_descriptor::Observation::observe()),
        }
    }
}

// The exact hosted SSPI implementation first opens this fixed public event
// with SYNCHRONIZE. Observe the same read-only access after all TLS/LSA calls;
// never set/reset it, wait without a deadline, inherit it or alter its ACL.
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct ReadyEvent {
    open_ntstatus: i32,
    handle_returned: bool,
    zero_wait_result: Option<u32>,
    close_hresult: Option<i32>,
}
impl ReadyEvent {
    fn passed(&self) -> bool {
        self.open_ntstatus == 0
            && self.handle_returned
            && self.zero_wait_result == Some(0)
            && self.close_hresult == Some(0)
    }
    fn require(&self) -> Result<()> {
        ensure!(
            self.open_ntstatus <= 0
                && (!self.handle_returned || self.open_ntstatus == 0)
                && self.zero_wait_result.is_some() == self.handle_returned
                && self.close_hresult.is_some() == self.handle_returned
                && self.close_hresult.is_none_or(|s| s <= 0),
            "LSA ready event observation or checked closure invalid"
        );
        Ok(())
    }
    fn observe() -> Self {
        use windows::Wdk::Foundation::OBJECT_ATTRIBUTES;
        use windows::Win32::Foundation::{
            CloseHandle, HANDLE, NTSTATUS, OBJ_CASE_INSENSITIVE, UNICODE_STRING,
        };
        use windows::Win32::System::Threading::WaitForSingleObject;
        #[link(name = "ntdll")]
        unsafe extern "system" {
            fn NtOpenEvent(
                handle: *mut HANDLE,
                access: u32,
                attributes: *const OBJECT_ATTRIBUTES,
            ) -> NTSTATUS;
        }
        let (mut value, length) =
            crate::util::counted_utf16(r"\SECURITY\LSA_AUTHENTICATION_INITIALIZED");
        let name = UNICODE_STRING {
            Length: length,
            MaximumLength: length,
            Buffer: windows::core::PWSTR(value.as_mut_ptr()),
        };
        let attributes = OBJECT_ATTRIBUTES {
            Length: std::mem::size_of::<OBJECT_ATTRIBUTES>() as u32,
            ObjectName: &name,
            Attributes: OBJ_CASE_INSENSITIVE,
            ..Default::default()
        };
        let mut handle = HANDLE::default();
        let open_ntstatus = unsafe { NtOpenEvent(&mut handle, 0x100000, &attributes) }.0;
        let handle_returned = open_ntstatus == 0 && !handle.is_invalid();
        let (zero_wait_result, close_hresult) = if handle_returned {
            let wait = unsafe { WaitForSingleObject(handle, 0) }.0;
            let close = unsafe { CloseHandle(handle) }
                .err()
                .map_or(0, |e| e.code().0);
            (Some(wait), Some(close))
        } else {
            (None, None)
        };
        Self {
            open_ntstatus,
            handle_returned,
            zero_wait_result,
            close_hresult,
        }
    }
}

pub(super) fn probe() -> Result<Proof> {
    crate::account_impersonation::require_process_identity()?;
    // No supplied certificates and NO_DEFAULT_CREDS prevent automatic client
    // certificate selection. Peer validation stays at its normal default.
    let credentials = SCH_CREDENTIALS {
        dwVersion: SCH_CREDENTIALS_VERSION,
        dwFlags: FLAGS,
        ..Default::default()
    };
    let mut handle = SecHandle::default();
    let acquired = unsafe {
        AcquireCredentialsHandleW(
            PCWSTR::null(),
            UNISP_NAME_W,
            SECPKG_CRED_OUTBOUND,
            None,
            Some((&credentials as *const SCH_CREDENTIALS).cast()),
            None,
            None,
            &mut handle,
            None,
        )
    };
    // Free immediately: no fallible Rust operation, allocation, callback or
    // early return can intervene after successful acquisition. Close once;
    // failure is retained and cannot be reported as a passed observation.
    let (acquire_status, release_status) = match acquired {
        Ok(()) => (
            0,
            Some(
                unsafe { FreeCredentialsHandle(&handle) }
                    .err()
                    .map_or(0, |e| e.code().0),
            ),
        ),
        Err(error) => (error.code().0, None),
    };
    // Query only the fixed provider AFTER acquisition and immediate release.
    // This cannot warm discovery before the original acquisition. Do not read
    // or serialize package metadata/pointers; release the OS buffer at once.
    let (package_query_status, package_buffer_returned, package_buffer_free_status) =
        match unsafe { QuerySecurityPackageInfoW(UNISP_NAME_W) } {
            Ok(buffer) if !buffer.is_null() => (
                0,
                true,
                Some(
                    unsafe { FreeContextBuffer(buffer.cast()) }
                        .err()
                        .map_or(0, |e| e.code().0),
                ),
            ),
            Ok(_) => (0, false, None), // Contract violation cannot qualify success.
            Err(error) => (error.code().0, false, None),
        };
    let dependencies = Dependencies::observe();
    let proof = Proof {
        passed: acquire_status == 0
            && release_status == Some(0)
            && package_query_status == 0
            && package_buffer_returned
            && package_buffer_free_status == Some(0)
            && dependencies.closed(),
        flags: FLAGS,
        acquire_status,
        release_status,
        package_query_status,
        package_buffer_returned,
        package_buffer_free_status,
        dependencies,
        https_qualified: false,
        network_io_attempted: false,
        self_lsa: None,
        self_lsa_error: None,
    };
    proof.require()?;
    Ok(proof)
}

pub(super) fn probe_child() -> Result<Proof> {
    // Baseline acquisition and primary-token LSA call remain first. An explicit
    // self-token success cannot replace their failure or qualify HTTPS.
    let mut proof = probe()?;
    match self_lsa::Comparison::observe() {
        Ok(comparison) => proof.self_lsa = Some(comparison),
        Err(error) => {
            // Keep the original USER/Schannel evidence even if this additional
            // diagnostic cannot run. Uncertain restoration still aborts inside
            // the guard; no unchecked identity can return here.
            proof.self_lsa_error = Some(
                error
                    .downcast_ref::<windows::core::Error>()
                    .map_or(0x80004005u32 as i32, |error| error.code().0),
            );
            proof.passed = false;
        }
    }
    proof.require()?;
    Ok(proof)
}

#[test]
fn station_lowbox_schannel_proof_requires_acquisition_and_release_without_tls_claims() {
    let proof = Proof {
        passed: true,
        flags: FLAGS,
        acquire_status: 0,
        release_status: Some(0),
        package_query_status: 0,
        package_buffer_returned: true,
        package_buffer_free_status: Some(0),
        dependencies: Dependencies {
            diagnostic_only: true,
            schannel_module_status: 0,
            lsa_connect_ntstatus: 0,
            lsa_handle_returned: true,
            lsa_close_ntstatus: Some(0),
            lsa_rpc_error: None,
            rpc_server_descriptor: None,
            lsa_ready_event: ReadyEvent {
                open_ntstatus: 0,
                handle_returned: true,
                zero_wait_result: Some(0),
                close_hresult: Some(0),
            },
        },
        https_qualified: false,
        network_io_attempted: false,
        self_lsa: None,
        self_lsa_error: None,
    };
    proof.require().unwrap();
    assert_eq!(proof.failure_status(), None);
    for field in ["https_qualified", "network_io_attempted"] {
        let mut value = serde_json::to_value(&proof).unwrap();
        value[field] = true.into();
        assert!(
            serde_json::from_value::<Proof>(value)
                .unwrap()
                .require()
                .is_err()
        );
    }
    for (acquire, release) in [
        (1, Some(0)),
        (0, None),
        (0, Some(1)),
        (0, Some(0x80090301u32 as i32)),
        (0x8009030eu32 as i32, Some(0)),
    ] {
        let bad = Proof {
            acquire_status: acquire,
            release_status: release,
            ..proof.clone()
        };
        assert!(bad.require().is_err());
    }
    let failure = Proof {
        passed: false,
        acquire_status: 0x8009030eu32 as i32,
        release_status: None,
        ..proof.clone()
    };
    failure.require().unwrap();
    assert_eq!(failure.failure_status(), Some(0x8009030eu32 as i32));
    let mut diagnostic_failure = failure.clone();
    diagnostic_failure.self_lsa_error = Some(0x80070005u32 as i32);
    diagnostic_failure.require().unwrap();
    assert_eq!(
        diagnostic_failure.failure_status(),
        failure.failure_status()
    );
    let mut diagnostic_failure = proof.clone();
    diagnostic_failure.self_lsa_error = Some(0x80070005u32 as i32);
    assert!(diagnostic_failure.require().is_err());
    diagnostic_failure.passed = false;
    diagnostic_failure.require().unwrap();
    assert_eq!(
        diagnostic_failure.failure_status(),
        Some(0x80070005u32 as i32)
    );
    let close_failure = Proof {
        passed: false,
        release_status: Some(0x80090301u32 as i32),
        ..proof.clone()
    };
    close_failure.require().unwrap();
    assert_eq!(close_failure.failure_status(), Some(0x80090301u32 as i32));
    let query_failure = Proof {
        passed: false,
        package_query_status: 0x80090305u32 as i32,
        package_buffer_returned: false,
        package_buffer_free_status: None,
        ..proof.clone()
    };
    query_failure.require().unwrap();
    assert_eq!(query_failure.failure_status(), Some(0x80090305u32 as i32));
    let null_buffer = Proof {
        passed: false,
        package_buffer_returned: false,
        package_buffer_free_status: None,
        ..proof.clone()
    };
    null_buffer.require().unwrap();
    for (status, returned, freed) in [
        (1, true, Some(0)),
        (0, true, None),
        (0, false, Some(0)),
        (0, true, Some(1)),
        (0x80090305u32 as i32, true, Some(0)),
    ] {
        let invalid = Proof {
            passed: false,
            package_query_status: status,
            package_buffer_returned: returned,
            package_buffer_free_status: freed,
            ..proof.clone()
        };
        assert!(invalid.require().is_err());
    }
    let mut bad = proof.clone();
    bad.flags |= SCH_CRED_MANUAL_CRED_VALIDATION.0;
    assert!(bad.require().is_err());
    let mut value = serde_json::to_value(&proof).unwrap();
    value["credential_handle"] = 1.into();
    assert!(serde_json::from_value::<Proof>(value).is_err());
}

#[test]
fn station_lowbox_schannel_dependency_diagnostic_retains_failure_and_requires_close() {
    let closed = Dependencies {
        diagnostic_only: true,
        schannel_module_status: 0,
        lsa_connect_ntstatus: 0,
        lsa_handle_returned: true,
        lsa_close_ntstatus: Some(0),
        lsa_rpc_error: None,
        rpc_server_descriptor: None,
        lsa_ready_event: ReadyEvent {
            open_ntstatus: 0,
            handle_returned: true,
            zero_wait_result: Some(0),
            close_hresult: Some(0),
        },
    };
    closed.require().unwrap();
    assert!(closed.closed());
    let mut event_refused = closed.clone();
    event_refused.lsa_ready_event = ReadyEvent {
        open_ntstatus: 0xc0000022u32 as i32,
        handle_returned: false,
        zero_wait_result: None,
        close_hresult: None,
    };
    event_refused.require().unwrap();
    assert!(!event_refused.closed());
    let mut event_close_failed = closed.clone();
    event_close_failed.lsa_ready_event.close_hresult = Some(0x80070006u32 as i32);
    event_close_failed.require().unwrap();
    assert!(!event_close_failed.closed());
    let mut event_unclosed = closed.clone();
    event_unclosed.lsa_ready_event.close_hresult = None;
    assert!(event_unclosed.require().is_err());
    let refused = Dependencies {
        lsa_connect_ntstatus: 0xc0000022u32 as i32,
        lsa_handle_returned: false,
        lsa_close_ntstatus: None,
        lsa_rpc_error: Some(
            serde_json::from_value(serde_json::json!({
                "start_status": 1761, "next_status": null, "end_status": null,
                "limit_reached": false, "rows": []
            }))
            .unwrap(),
        ),
        ..closed.clone()
    };
    refused.require().unwrap();
    assert!(!refused.closed());
    let close_failure = Dependencies {
        lsa_close_ntstatus: Some(0xc0000008u32 as i32),
        ..closed.clone()
    };
    close_failure.require().unwrap();
    assert!(!close_failure.closed());
    for field in ["diagnostic_only", "lsa_handle_returned"] {
        let mut changed = serde_json::to_value(&closed).unwrap();
        changed[field] = false.into();
        assert!(
            serde_json::from_value::<Dependencies>(changed)
                .unwrap()
                .require()
                .is_err()
        );
    }
    let mut changed = serde_json::to_value(&closed).unwrap();
    changed["lsa_handle"] = 42.into();
    assert!(serde_json::from_value::<Dependencies>(changed).is_err());
}
