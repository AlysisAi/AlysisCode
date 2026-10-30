//! Bounded post-baseline LSA comparison in the original lowbox process. The
//! only impersonated identity is a duplicate of this process's primary token.
//! Reverting inside SSPI therefore cannot fall back to a raw helper identity.
use super::*;
use windows::Win32::Security::{
    GetTokenInformation, TOKEN_DUPLICATE, TOKEN_QUERY, TOKEN_STATISTICS, TokenIsAppContainer,
    TokenStatistics,
};

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Comparison {
    diagnostic_only: bool,
    own_lowbox_token: bool,
    primary_unchanged: bool,
    thread_restored: bool,
    impersonated: Connection,
    reverted: Connection,
}

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Connection {
    status: i32,
    handle_returned: bool,
    close_status: Option<i32>,
    rpc_error: Option<rpc_error::Observation>,
}

impl Connection {
    fn observe() -> Self {
        let mut handle = HANDLE::default();
        let status = unsafe { LsaConnectUntrusted(&mut handle) }.0;
        let rpc_error = (status < 0).then(rpc_error::Observation::observe);
        let handle_returned = status == 0 && !handle.is_invalid();
        let close_status = handle_returned.then(|| unsafe { LsaDeregisterLogonProcess(handle) }.0);
        Self {
            status,
            handle_returned,
            close_status,
            rpc_error,
        }
    }

    fn require(&self) -> Result<()> {
        if let Some(error) = &self.rpc_error {
            error.require()?;
        }
        ensure!(
            self.status <= 0
                && self.handle_returned == (self.status == 0)
                && self.close_status == self.handle_returned.then_some(0)
                && self.rpc_error.is_some() == (self.status < 0),
            "self-token LSA observation has uncertain connection or closure"
        );
        Ok(())
    }
}

fn statistics(token: HANDLE) -> Result<TOKEN_STATISTICS> {
    let mut value = TOKEN_STATISTICS::default();
    let mut returned = 0;
    unsafe {
        GetTokenInformation(
            token,
            TokenStatistics,
            Some((&mut value as *mut TOKEN_STATISTICS).cast()),
            std::mem::size_of::<TOKEN_STATISTICS>() as u32,
            &mut returned,
        )?;
    }
    ensure!(
        returned as usize == std::mem::size_of::<TOKEN_STATISTICS>(),
        "invalid primary statistics size"
    );
    Ok(value)
}

impl Comparison {
    pub(super) fn require(&self) -> Result<()> {
        ensure!(
            self.diagnostic_only
                && self.own_lowbox_token
                && self.primary_unchanged
                && self.thread_restored,
            "self-token LSA diagnostic identity or restoration unverified"
        );
        self.impersonated.require()?;
        self.reverted.require()
    }

    pub(super) fn observe() -> Result<Self> {
        crate::account_impersonation::require_process_identity()?;
        let mut raw = HANDLE::default();
        unsafe {
            OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY | TOKEN_DUPLICATE, &mut raw)?;
        }
        let token = OwnedHandle(raw);
        noninheritable(token.raw())?;
        let before = statistics(token.raw())?;
        let mut appcontainer = 0u32;
        let mut returned = 0;
        unsafe {
            GetTokenInformation(
                token.raw(),
                TokenIsAppContainer,
                Some((&mut appcontainer as *mut u32).cast()),
                4,
                &mut returned,
            )?;
        }
        ensure!(
            returned == 4 && appcontainer == 1,
            "self-token LSA comparison requires original lowbox"
        );
        // The existing checked guard binds the exact duplicate TokenId/AuthId/
        // ModifiedId before and after the call. Restoration failure aborts.
        // No privileges, capabilities, groups or token attributes are changed.
        let impersonated =
            crate::account_impersonation::with_token(&token, || Ok(Connection::observe()))?;
        crate::account_impersonation::require_process_identity()?;
        let reverted = Connection::observe();
        crate::account_impersonation::require_process_identity()?;
        // Reopen the actual process token instead of re-reading only a retained
        // handle, so replacement cannot masquerade as an unchanged primary.
        let mut current = HANDLE::default();
        unsafe {
            OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut current)?;
        }
        let current = OwnedHandle(current);
        let after = statistics(current.raw())?;
        let primary_unchanged = before.TokenId == after.TokenId
            && before.AuthenticationId == after.AuthenticationId
            && before.ModifiedId == after.ModifiedId;
        unsafe {
            CloseHandle(current.into_raw())?;
            CloseHandle(token.into_raw())?;
        }
        let proof = Self {
            diagnostic_only: true,
            own_lowbox_token: true,
            primary_unchanged,
            thread_restored: true,
            impersonated,
            reverted,
        };
        proof.require()?;
        Ok(proof)
    }
}

#[test]
fn station_lowbox_self_lsa_requires_same_identity_restoration_and_checked_close() {
    let value = serde_json::json!({"diagnostic_only":true,"own_lowbox_token":true,
        "primary_unchanged":true,"thread_restored":true,
        "impersonated":{"status":0,"handle_returned":true,"close_status":0,"rpc_error":null},
        "reverted":{"status":0,"handle_returned":true,"close_status":0,"rpc_error":null}});
    serde_json::from_value::<Comparison>(value.clone())
        .unwrap()
        .require()
        .unwrap();
    for field in [
        "diagnostic_only",
        "own_lowbox_token",
        "primary_unchanged",
        "thread_restored",
    ] {
        let mut changed = value.clone();
        changed[field] = false.into();
        assert!(
            serde_json::from_value::<Comparison>(changed)
                .unwrap()
                .require()
                .is_err()
        );
    }
    for actor in ["impersonated", "reverted"] {
        for (field, replacement) in [
            ("close_status", serde_json::Value::Null),
            ("handle_returned", false.into()),
            ("status", (-1073741790i32).into()),
        ] {
            let mut changed = value.clone();
            changed[actor][field] = replacement;
            assert!(
                serde_json::from_value::<Comparison>(changed)
                    .unwrap()
                    .require()
                    .is_err()
            );
        }
    }
}
