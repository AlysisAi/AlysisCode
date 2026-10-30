//! Fixed public metadata request for the admitted disposable station fixture.
//! No endpoint, headers, credentials or request body are accepted from a caller.
//! Synchronous API timeouts bound individual operations; the original parent
//! additionally owns the child deadline and drains its job on timeout.
use super::rpc_error;
#[path = "account_station_https_endpoint.rs"]
mod endpoint;
#[path = "account_station_https_nsi.rs"]
mod nsi;
#[path = "account_station_tcp_probe.rs"]
mod tcp;
use anyhow::{Result, ensure};
use serde::{Deserialize, Serialize};
use std::{ffi::c_void, time::Instant};
use windows::{
    Win32::Networking::WinHttp::*,
    core::{PCWSTR, w},
};

const BODY_LIMIT: usize = 65536;
const BUDGET_MS: u64 = 15000;
const DISABLED: u32 = WINHTTP_DISABLE_AUTHENTICATION
    | WINHTTP_DISABLE_COOKIES
    | WINHTTP_DISABLE_REDIRECTS
    | WINHTTP_DISABLE_KEEP_ALIVE;

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct ExtendedError {
    query_hresult: i32,
    returned_bytes: u32,
    winsock: Option<u32>,
}
impl ExtendedError {
    fn observe() -> Self {
        let (mut value, mut length) = (0u32, 4u32);
        // Documented thread-local WinHTTP mapping of the last socket error.
        // Query before any subsequent WinHTTP call (including handle close),
        // never WSAGetLastError from an unrelated worker or operation.
        let query_hresult = unsafe {
            WinHttpQueryOption(
                std::ptr::null_mut(),
                WINHTTP_OPTION_EXTENDED_ERROR,
                Some((&mut value as *mut u32).cast()),
                &mut length,
            )
        }
        .err()
        .map_or(0, |e| e.code().0);
        Self {
            query_hresult,
            returned_bytes: length,
            winsock: (query_hresult == 0 && length == 4).then_some(value),
        }
    }
    fn require(&self) -> Result<()> {
        ensure!(
            self.winsock.is_some() == (self.query_hresult == 0 && self.returned_bytes == 4),
            "HTTPS extended error lacks successful exact DWORD query"
        );
        Ok(())
    }
    fn access_denied(&self) -> bool {
        self.require().is_ok() && self.winsock == Some(10013) // WSAEACCES
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Proof {
    stage: String,
    hresult: Option<i32>,
    policy_applied: bool,
    request_attempted: bool,
    http_status: Option<u32>,
    body_bytes: usize,
    metadata_matches: bool,
    opened: usize,
    close_results: Vec<i32>,
    elapsed_ms: u64,
    #[serde(default)]
    rpc_before: Option<rpc_error::Observation>,
    #[serde(default)]
    rpc_after: Option<rpc_error::Observation>,
    #[serde(default)]
    endpoint_after: Option<endpoint::Observation>,
    #[serde(default)]
    nsi_after: Option<nsi::Observation>,
    #[serde(default)]
    extended_error: Option<ExtendedError>,
    #[serde(default)]
    socket_after: Option<tcp::Observation>,
}
impl Proof {
    pub(super) fn require(&self) -> Result<()> {
        if let Some(socket) = &self.socket_after {
            socket.require()?;
        }
        if let Some(error) = &self.extended_error {
            error.require()?;
            ensure!(
                self.hresult.is_some()
                    && self.request_attempted
                    && matches!(self.stage.as_str(), "send" | "receive"),
                "HTTPS extended error lacks failed request context"
            );
        }
        if let Some(observation) = &self.nsi_after {
            observation.require()?;
        }
        if let Some(observation) = &self.endpoint_after {
            observation.require()?;
        }
        for observation in [&self.rpc_before, &self.rpc_after].into_iter().flatten() {
            observation.require()?;
        }
        ensure!(
            self.rpc_after.is_none()
                || (self.rpc_before.is_some()
                    && self.request_attempted
                    && self.hresult.is_some()
                    && matches!(self.stage.as_str(), "send" | "receive")),
            "HTTPS RPC snapshot lacks failed request context"
        );
        ensure!(
            [
                "session",
                "policy",
                "connect",
                "request",
                "request_policy",
                "send",
                "receive",
                "status",
                "body",
                "metadata",
                "complete"
            ]
            .contains(&self.stage.as_str()),
            "HTTPS stage differs"
        );
        ensure!(
            self.opened <= 3
                && self.close_results.len() == self.opened
                && self.body_bytes <= BODY_LIMIT
                && self.elapsed_ms <= 120000
                && self.http_status.is_none_or(|s| (100..=599).contains(&s)),
            "HTTPS bounds or handle closure differ"
        );
        ensure!(
            !self.request_attempted || (self.policy_applied && self.opened == 3),
            "HTTPS request precedes fixed policy"
        );
        ensure!(
            !self.metadata_matches
                || (self.http_status == Some(200) && self.body_bytes > 0 && self.request_attempted),
            "HTTPS metadata lacks response"
        );
        ensure!(
            self.stage != "complete" || (self.hresult.is_none() && self.metadata_matches),
            "HTTPS completion lacks evidence"
        );
        Ok(())
    }
    pub(super) fn succeeded(&self) -> bool {
        self.require().is_ok() && self.stage == "complete" && self.close_results == [0, 0, 0]
    }
    pub(super) fn access_denied(&self) -> bool {
        // DNS errors, timeouts, TLS failures and generic connection failure alone
        // are not denial proof. A mapped connection failure requires the exact
        // documented WinHTTP extended DWORD WSAEACCES, not worker-thread errno.
        self.require().is_ok()
            && matches!(self.stage.as_str(), "send" | "receive")
            && self.request_attempted
            && self.http_status.is_none()
            && self.body_bytes == 0
            && !self.metadata_matches
            && self.close_results == [0, 0, 0]
            && (matches!(
                self.hresult.map(|h| h as u32),
                Some(0x80070005 | 0x80070041 | 0x8007271d)
            ) || (self.hresult
                == Some(windows::core::HRESULT::from_win32(ERROR_WINHTTP_CANNOT_CONNECT).0)
                && self
                    .extended_error
                    .as_ref()
                    .is_some_and(ExtendedError::access_denied)))
    }
    pub(super) fn socket_connected(&self) -> bool {
        self.require().is_ok()
            && self
                .socket_after
                .as_ref()
                .is_some_and(tcp::Observation::connected)
    }
    pub(super) fn offline_network_denied(&self) -> bool {
        // Separate TCP evidence proves the connect denial. A failed HTTPS
        // lookup remains a failed lookup; it is never relabeled access denied.
        self.require().is_ok()
            && self.stage == "send"
            && self.request_attempted
            && self.hresult.is_some()
            && self.http_status.is_none()
            && self.body_bytes == 0
            && !self.metadata_matches
            && self.close_results == [0, 0, 0]
            && self
                .socket_after
                .as_ref()
                .is_some_and(tcp::Observation::denied)
    }
    pub(super) fn network_qualified(&self, network: crate::appcontainer::NetworkAccess) -> bool {
        match network {
            crate::appcontainer::NetworkAccess::Off => self.offline_network_denied(),
            crate::appcontainer::NetworkAccess::On => self.succeeded() && self.socket_connected(),
        }
    }
}

struct Handle(*mut c_void);
impl Handle {
    fn take(raw: *mut c_void, proof: &mut Proof) -> windows::core::Result<Self> {
        if raw.is_null() {
            return Err(windows::core::Error::from_thread());
        }
        proof.opened += 1;
        Ok(Self(raw))
    }
    fn close(mut self) -> i32 {
        let raw = std::mem::replace(&mut self.0, std::ptr::null_mut());
        unsafe { WinHttpCloseHandle(raw) }
            .err()
            .map_or(0, |e| e.code().0)
    }
}
impl Drop for Handle {
    fn drop(&mut self) {
        if !self.0.is_null() {
            let _ = unsafe { WinHttpCloseHandle(self.0) };
        }
    }
}
fn option(handle: &Handle, name: u32, value: u32) -> windows::core::Result<()> {
    unsafe { WinHttpSetOption(Some(handle.0), name, Some(&value.to_ne_bytes())) }
}
fn timeouts(handle: &Handle, start: Instant) -> windows::core::Result<()> {
    let left = BUDGET_MS.saturating_sub(start.elapsed().as_millis() as u64);
    if left == 0 {
        return Err(windows::core::Error::from_hresult(
            windows::core::HRESULT::from_win32(ERROR_WINHTTP_TIMEOUT),
        ));
    }
    let ms = left.min(3000) as i32;
    unsafe { WinHttpSetTimeouts(handle.0, ms, ms, ms, ms) }
}
fn append(body: &mut Vec<u8>, bytes: &[u8]) -> Result<()> {
    ensure!(
        body.len()
            .checked_add(bytes.len())
            .is_some_and(|n| n <= BODY_LIMIT),
        "HTTPS body exceeds bound"
    );
    body.extend_from_slice(bytes);
    Ok(())
}
fn metadata(body: &[u8]) -> bool {
    if body.is_empty() || body.len() > BODY_LIMIT {
        return false;
    }
    serde_json::from_slice::<serde_json::Value>(body)
        .ok()
        .is_some_and(|value| value["name"] == "is-number" && value["version"] == "7.0.0")
}

pub(super) fn observe() -> Proof {
    let start = Instant::now();
    let mut proof = Proof {
        stage: "session".into(),
        hresult: None,
        policy_applied: false,
        request_attempted: false,
        http_status: None,
        body_bytes: 0,
        metadata_matches: false,
        opened: 0,
        close_results: Vec::new(),
        elapsed_ms: 0,
        rpc_before: None,
        rpc_after: None,
        endpoint_after: None,
        nsi_after: None,
        extended_error: None,
        socket_after: None,
    };
    let (mut session, mut connect, mut request): (Option<Handle>, Option<Handle>, Option<Handle>) =
        (None, None, None);
    let result = (|| -> Result<()> {
        session = Some(Handle::take(
            unsafe {
                WinHttpOpen(
                    w!("AlysisSandboxQA/1"),
                    WINHTTP_ACCESS_TYPE_NO_PROXY,
                    PCWSTR::null(),
                    PCWSTR::null(),
                    0,
                )
            },
            &mut proof,
        )?);
        let session = session.as_ref().unwrap();
        proof.stage = "policy".into();
        timeouts(session, start)?;
        option(session, WINHTTP_OPTION_CONNECT_RETRIES, 1)?;
        option(session, WINHTTP_OPTION_MAX_RESPONSE_HEADER_SIZE, 16384)?;
        proof.stage = "connect".into();
        connect = Some(Handle::take(
            unsafe { WinHttpConnect(session.0, w!("registry.npmjs.org"), 443, 0) },
            &mut proof,
        )?);
        proof.stage = "request".into();
        request = Some(Handle::take(
            unsafe {
                WinHttpOpenRequest(
                    connect.as_ref().unwrap().0,
                    w!("GET"),
                    w!("/is-number/7.0.0"),
                    PCWSTR::null(),
                    PCWSTR::null(),
                    std::ptr::null(),
                    WINHTTP_FLAG_SECURE,
                )
            },
            &mut proof,
        )?);
        let request = request.as_ref().unwrap();
        proof.stage = "request_policy".into();
        option(request, WINHTTP_OPTION_DISABLE_FEATURE, DISABLED)?;
        option(
            request,
            WINHTTP_OPTION_AUTOLOGON_POLICY,
            WINHTTP_AUTOLOGON_SECURITY_LEVEL_HIGH,
        )?;
        // Do not set security-ignore flags or supply client certificates.
        timeouts(request, start)?;
        proof.policy_applied = true;
        proof.stage = "send".into();
        proof.request_attempted = true;
        // Preserve a before snapshot: existing caller-thread error records are
        // not assumed to originate in this request. WinHTTP may use workers;
        // an empty/unchanged caller snapshot cannot establish their RPC state.
        proof.rpc_before = Some(rpc_error::Observation::observe());
        unsafe {
            WinHttpSendRequest(request.0, None, None, 0, 0, 0)?;
        }
        proof.stage = "receive".into();
        timeouts(request, start)?;
        unsafe {
            WinHttpReceiveResponse(request.0, std::ptr::null_mut())?;
        }
        proof.stage = "status".into();
        let (mut status, mut length, mut index) = (0u32, 4u32, 0u32);
        unsafe {
            WinHttpQueryHeaders(
                request.0,
                WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER,
                PCWSTR::null(),
                Some((&mut status as *mut u32).cast()),
                &mut length,
                &mut index,
            )?;
        }
        ensure!(
            length == 4 && (100..=599).contains(&status),
            "HTTPS status shape differs"
        );
        proof.http_status = Some(status);
        ensure!(status == 200, "HTTPS status is not successful");
        proof.stage = "body".into();
        let mut body = Vec::new();
        let mut buffer = [0u8; 8192];
        loop {
            timeouts(request, start)?;
            let mut read = 0;
            unsafe {
                WinHttpReadData(
                    request.0,
                    buffer.as_mut_ptr().cast(),
                    buffer.len() as u32,
                    &mut read,
                )?;
            }
            ensure!(
                read as usize <= buffer.len(),
                "HTTPS native read count differs"
            );
            if read == 0 {
                break;
            }
            append(&mut body, &buffer[..read as usize])?;
            proof.body_bytes = body.len();
        }
        proof.stage = "metadata".into();
        proof.metadata_matches = metadata(&body);
        ensure!(proof.metadata_matches, "HTTPS fixed metadata differs");
        proof.stage = "complete".into();
        Ok(())
    })();
    if let Err(error) = result {
        // Only native numeric codes; never serialize an error string or body.
        proof.hresult = error
            .downcast_ref::<windows::core::Error>()
            .map(|e| e.code().0);
        if proof.hresult.is_some() && matches!(proof.stage.as_str(), "send" | "receive") {
            proof.rpc_after = Some(rpc_error::Observation::observe());
            proof.extended_error = Some(ExtendedError::observe());
        }
    }
    for handle in [request.take(), connect.take(), session.take()]
        .into_iter()
        .flatten()
    {
        proof.close_results.push(handle.close());
    }
    proof.elapsed_ms = start.elapsed().as_millis().min(u64::MAX as u128) as u64;
    // Run after the baseline request, error capture and HTTP handle closures;
    // this comparison must not warm up the endpoint mapper before WinHTTP.
    proof.endpoint_after = Some(endpoint::Observation::observe());
    proof.nsi_after = Some(nsi::Observation::observe());
    proof.socket_after = Some(tcp::Observation::observe());
    proof
}

#[test]
fn station_https_bounded_metadata_rejects_overflow_and_wrong_response() {
    let good = br#"{"name":"is-number","version":"7.0.0"}"#;
    assert!(metadata(good));
    for bad in [
        b"".as_slice(),
        b"not-json",
        br#"{"name":"other","version":"7.0.0"}"#,
        br#"{"name":"is-number","version":"8.0.0"}"#,
    ] {
        assert!(!metadata(bad));
    }
    let mut body = vec![0u8; BODY_LIMIT - good.len()];
    append(&mut body, good).unwrap();
    assert_eq!(body.len(), BODY_LIMIT);
    assert!(append(&mut body, b"x").is_err());
    assert_eq!(body.len(), BODY_LIMIT);
    assert!(!metadata(&vec![b' '; BODY_LIMIT + 1]));
}
#[test]
fn station_https_network_proofs_cannot_cross_modes_or_omit_tcp() {
    use crate::appcontainer::NetworkAccess::{Off, On};
    let mut wire = serde_json::json!({
        "stage":"complete", "hresult":null, "policy_applied":true,
        "request_attempted":true, "http_status":200, "body_bytes":40,
        "metadata_matches":true, "opened":3, "close_results":[0,0,0], "elapsed_ms":15,
        "socket_after":{"target":"1.1.1.1:443","startup_error":0,"version":514,
            "socket_hresult":null,"opened":true,"nonblocking_error":0,"connect_error":0,
            "wait":null,"close_error":0,"cleanup_error":0,"elapsed_ms":1}
    });
    let online: Proof = serde_json::from_value(wire.clone()).unwrap();
    assert!(online.network_qualified(On));
    assert!(!online.network_qualified(Off));
    wire["stage"] = "send".into();
    wire["hresult"] =
        serde_json::json!(windows::core::HRESULT::from_win32(ERROR_WINHTTP_NAME_NOT_RESOLVED).0);
    wire["http_status"] = serde_json::Value::Null;
    wire["body_bytes"] = 0.into();
    wire["metadata_matches"] = false.into();
    wire["socket_after"]["connect_error"] = 10013.into();
    let offline: Proof = serde_json::from_value(wire.clone()).unwrap();
    assert!(offline.network_qualified(Off));
    assert!(!offline.network_qualified(On));
    for proof in [online, offline] {
        for (field, value) in [
            ("socket_after", serde_json::Value::Null),
            ("close_results", serde_json::json!([0, 0, -1])),
        ] {
            let mut altered = serde_json::to_value(&proof).unwrap();
            altered[field] = value;
            let wrong: Proof = serde_json::from_value(altered).unwrap();
            assert!(!wrong.network_qualified(Off));
            assert!(!wrong.network_qualified(On));
        }
    }
    for ambiguous in [10060, 10061, 0] {
        wire["socket_after"]["connect_error"] = ambiguous.into();
        let wrong: Proof = serde_json::from_value(wire.clone()).unwrap();
        assert!(!wrong.network_qualified(Off));
        assert!(!wrong.network_qualified(On));
    }
}
#[test]
fn station_https_evidence_refuses_ambiguous_denials_and_missing_close() {
    let mut p = Proof {
        stage: "complete".into(),
        hresult: None,
        policy_applied: true,
        request_attempted: true,
        http_status: Some(200),
        body_bytes: 40,
        metadata_matches: true,
        opened: 3,
        close_results: vec![0, 0, 0],
        elapsed_ms: 15,
        rpc_before: None,
        rpc_after: None,
        endpoint_after: None,
        nsi_after: None,
        extended_error: None,
        socket_after: None,
    };
    assert!(p.succeeded());
    for (name, value) in [
        ("policy_applied", false.into()),
        ("request_attempted", false.into()),
        ("close_results", serde_json::json!([0, 0])),
        ("metadata_matches", false.into()),
        ("http_status", 302.into()),
        ("url", "https://elsewhere.invalid".into()),
    ] {
        let mut wire = serde_json::to_value(&p).unwrap();
        wire[name] = value;
        assert!(!serde_json::from_value::<Proof>(wire).is_ok_and(|v| v.succeeded()));
    }
    p.stage = "send".into();
    p.http_status = None;
    p.body_bytes = 0;
    p.metadata_matches = false;
    for code in [
        ERROR_WINHTTP_TIMEOUT,
        ERROR_WINHTTP_NAME_NOT_RESOLVED,
        ERROR_WINHTTP_CANNOT_CONNECT,
        ERROR_WINHTTP_SECURE_FAILURE,
    ] {
        p.hresult = Some(windows::core::HRESULT::from_win32(code).0);
        assert!(!p.access_denied());
        assert!(!p.succeeded());
    }
    p.hresult = Some(0x80070005u32 as i32);
    assert!(p.access_denied());
    p.stage = "policy".into();
    assert!(!p.access_denied());
    p.stage = "send".into();
    p.body_bytes = 1;
    assert!(!p.access_denied());
    p.body_bytes = 0;
    p.close_results[0] = -1;
    assert!(!p.access_denied());
    p.close_results[0] = 0;
    p.hresult = Some(windows::core::HRESULT::from_win32(ERROR_WINHTTP_CANNOT_CONNECT).0);
    assert!(!p.access_denied());
    p.extended_error = Some(ExtendedError {
        query_hresult: 0,
        returned_bytes: 4,
        winsock: Some(10013),
    });
    assert!(p.access_denied());
    for wrong in [0, 5, 10060, 10061, 11001] {
        p.extended_error.as_mut().unwrap().winsock = Some(wrong);
        assert!(!p.access_denied());
    }
    p.extended_error.as_mut().unwrap().winsock = Some(10013);
    for case in 0..4 {
        let mut wrong = p.clone();
        match case {
            0 => wrong.extended_error.as_mut().unwrap().query_hresult = -1,
            1 => wrong.extended_error.as_mut().unwrap().returned_bytes = 8,
            2 => wrong.hresult = Some(windows::core::HRESULT::from_win32(ERROR_WINHTTP_TIMEOUT).0),
            _ => wrong.close_results[0] = -1,
        }
        assert!(!wrong.access_denied());
    }
    // Preserve the exact DNS failure; only a separately observed TCP connect
    // denial can qualify the network check. A missing/failed raw positive is
    // rejected by the parent independently.
    p.hresult = Some(windows::core::HRESULT::from_win32(ERROR_WINHTTP_NAME_NOT_RESOLVED).0);
    p.extended_error.as_mut().unwrap().winsock = Some(0);
    assert!(!p.access_denied());
    assert!(!p.offline_network_denied());
    let socket = serde_json::json!({"target":"1.1.1.1:443","startup_error":0,"version":514,
        "socket_hresult":null,"opened":true,"nonblocking_error":0,"connect_error":10013,
        "wait":null,"close_error":0,"cleanup_error":0,"elapsed_ms":1});
    p.socket_after = Some(serde_json::from_value(socket.clone()).unwrap());
    assert!(p.offline_network_denied());
    assert!(!p.access_denied());
    assert!(!p.socket_connected());
    for (key, value) in [
        ("connect_error", 10060.into()),
        ("connect_error", 0.into()),
        ("close_error", 10038.into()),
        ("cleanup_error", 10091.into()),
    ] {
        let mut altered = socket.clone();
        altered[key] = value;
        p.socket_after = Some(serde_json::from_value(altered).unwrap());
        assert!(!p.offline_network_denied());
    }
}
