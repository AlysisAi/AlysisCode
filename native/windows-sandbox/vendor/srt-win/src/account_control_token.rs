//! Retained primary-token continuity only, not executable/job trust or launch
//! authority. No token from the wire, no impersonation and no token modification.
use super::*;
use windows::Win32::Security::*;

#[derive(PartialEq, Eq)]
struct Stamp {
    token: (u32, i32),
    logon: (u32, i32),
    modified: (u32, i32),
    groups: u32,
    privileges: u32,
}

fn fixed<T: Default>(token: HANDLE, class: TOKEN_INFORMATION_CLASS) -> Result<T> {
    let mut value = T::default();
    let mut length = 0;
    unsafe {
        GetTokenInformation(
            token,
            class,
            Some((&mut value as *mut T).cast()),
            size_of::<T>() as u32,
            &mut length,
        )?;
    }
    ensure!(
        length as usize == size_of::<T>(),
        "unexpected peer token information size"
    );
    Ok(value)
}

fn stamp(token: HANDLE) -> Result<Stamp> {
    let stats: TOKEN_STATISTICS = fixed(token, TokenStatistics)?;
    ensure!(stats.TokenType == TokenPrimary, "peer token is not primary");
    Ok(Stamp {
        token: (stats.TokenId.LowPart, stats.TokenId.HighPart),
        logon: (
            stats.AuthenticationId.LowPart,
            stats.AuthenticationId.HighPart,
        ),
        modified: (stats.ModifiedId.LowPart, stats.ModifiedId.HighPart),
        groups: stats.GroupCount,
        privileges: stats.PrivilegeCount,
    })
}

fn user(token: HANDLE) -> Result<String> {
    // TOKEN_USER and the largest supported SID fit in this aligned, fixed bound.
    let mut words = [0usize; 32];
    let mut length = 0;
    unsafe {
        GetTokenInformation(
            token,
            TokenUser,
            Some(words.as_mut_ptr().cast()),
            std::mem::size_of_val(&words) as u32,
            &mut length,
        )?;
    }
    ensure!(
        (size_of::<TOKEN_USER>()..=std::mem::size_of_val(&words)).contains(&(length as usize)),
        "unexpected peer user information size"
    );
    let sid = unsafe { (*words.as_ptr().cast::<TOKEN_USER>()).User.Sid };
    let start = words.as_ptr() as usize;
    let end = start + length as usize;
    let ptr = sid.0 as usize;
    ensure!(
        ptr >= start + size_of::<TOKEN_USER>() && ptr <= end.saturating_sub(8),
        "peer user SID is outside token buffer"
    );
    let count = unsafe { *sid.0.cast::<u8>().add(1) } as usize;
    ensure!(
        count <= 15 && 8 + count * 4 <= end - ptr,
        "peer user SID exceeds token buffer"
    );
    ensure!(
        unsafe { IsValidSid(sid) }.as_bool(),
        "invalid peer user SID"
    );
    crate::sid::psid_to_string(sid)
}

#[derive(PartialEq, Eq)]
struct Identity {
    stamp: Stamp,
    user: String,
    session: u32,
    appcontainer: u32,
}

fn observe(token: HANDLE) -> Result<Identity> {
    noninheritable(token)?;
    let before = stamp(token)?;
    let user = user(token)?;
    let session = fixed(token, TokenSessionId)?;
    let appcontainer: u32 = fixed(token, TokenIsAppContainer)?;
    ensure!(appcontainer <= 1, "unknown peer AppContainer state");
    ensure!(
        before == stamp(token)?,
        "peer token changed during observation"
    );
    Ok(Identity {
        stamp: before,
        user,
        session,
        appcontainer,
    })
}

fn open(process: HANDLE) -> Result<OwnedHandle> {
    let mut token = HANDLE::default();
    unsafe { OpenProcessToken(process, TOKEN_QUERY, &mut token)? };
    let token = OwnedHandle(token);
    noninheritable(token.raw())?;
    Ok(token)
}

pub(super) struct PrimaryToken {
    // Keep the original object alive; a token identifier alone is not ownership.
    token: OwnedHandle,
    identity: Identity,
}

impl PrimaryToken {
    #[cfg(test)]
    pub(super) fn fixture_stamp(&self, process: HANDLE) -> Result<([u32; 2], [u32; 2], [u32; 2])> {
        self.verify(process)?;
        let stamp = &self.identity.stamp;
        Ok((
            [stamp.token.0, stamp.token.1 as u32],
            [stamp.logon.0, stamp.logon.1 as u32],
            [stamp.modified.0, stamp.modified.1 as u32],
        ))
    }
    pub(super) fn retain(process: HANDLE) -> Result<Self> {
        let token = open(process)?;
        let identity = observe(token.raw())?;
        let this = Self { token, identity };
        this.verify(process)?;
        Ok(this)
    }
    pub(super) fn verify(&self, process: HANDLE) -> Result<()> {
        ensure!(
            observe(self.token.raw())? == self.identity,
            "original peer token changed"
        );
        // Query the already-retained process, never OpenProcess by a supplied PID.
        let actual = open(process)?;
        ensure!(
            observe(actual.raw())? == self.identity,
            "peer primary token was replaced or changed"
        );
        Ok(())
    }
    pub(super) fn require_account_runner(&self, account: &str, session: u32) -> Result<()> {
        ensure!(
            self.identity.user == account
                && self.identity.session == session
                && self.identity.appcontainer == 0,
            "peer is not the selected raw account runner in the broker session"
        );
        Ok(())
    }

    /// Dedicated account role, separate from the host bootstrap's raw-token
    /// continuity check. The host bootstrap may legitimately be elevated.
    pub(super) fn require_dedicated_runner(
        &self,
        account: &str,
        session: u32,
        group: &str,
    ) -> Result<()> {
        self.require_account_runner(account, session)?;
        ensure!(
            observe(self.token.raw())? == self.identity,
            "dedicated runner token changed"
        );
        crate::account_token::runner_policy::require(self.token.raw(), account, group)?;
        ensure!(
            observe(self.token.raw())? == self.identity,
            "dedicated runner token changed during policy check"
        );
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    const TIMEOUT: Duration = Duration::from_secs(1);

    fn duplicate(kind: TOKEN_TYPE) -> OwnedHandle {
        let original = OwnedHandle(crate::token::open_self_token().unwrap());
        let mut raw = HANDLE::default();
        unsafe {
            DuplicateTokenEx(
                original.raw(),
                TOKEN_QUERY | TOKEN_DUPLICATE | TOKEN_ADJUST_PRIVILEGES,
                None,
                SecurityImpersonation,
                kind,
                &mut raw,
            )
            .unwrap();
        }
        OwnedHandle(raw)
    }

    #[test]
    fn account_control_token_binds_native_user_session_and_primary_object() {
        let original = PrimaryToken::retain(unsafe { GetCurrentProcess() }).unwrap();
        let mut session = 0;
        unsafe {
            windows::Win32::System::RemoteDesktop::ProcessIdToSessionId(
                std::process::id(),
                &mut session,
            )
            .unwrap()
        };
        original
            .require_account_runner(&crate::sid::current_user_sid().unwrap(), session)
            .unwrap();
        assert!(
            original
                .require_account_runner("S-1-5-21-1-2-3-9999", session)
                .is_err()
        );
        assert!(
            original
                .require_account_runner(&original.identity.user, session.wrapping_add(1))
                .is_err()
        );
        original.verify(unsafe { GetCurrentProcess() }).unwrap();
        noninheritable(original.token.raw()).unwrap();
        let other = duplicate(TokenPrimary);
        let other = observe(other.raw()).unwrap();
        assert_eq!(other.user, original.identity.user);
        assert!(other.stamp.logon == original.identity.stamp.logon);
        assert!(other.stamp.token != original.identity.stamp.token);
        assert!(
            other != original.identity,
            "same user/logon is not the original primary object"
        );
    }

    #[test]
    fn account_control_token_refuses_impersonation_or_non_token_handles() {
        let token = duplicate(TokenImpersonation);
        assert!(observe(token.raw()).is_err());
        assert!(observe(unsafe { GetCurrentProcess() }).is_err());
        assert!(observe(HANDLE::default()).is_err());
    }

    #[test]
    fn account_control_token_restoring_privileges_does_not_restore_original_stamp() {
        let token = duplicate(TokenPrimary);
        let initial = observe(token.raw()).unwrap();
        super::super::token_tests::toggle_traverse(token.raw());
        let changed = observe(token.raw()).unwrap();
        assert!(initial.stamp.token == changed.stamp.token);
        assert!(initial != changed);
        super::super::token_tests::toggle_traverse(token.raw());
        let restored = observe(token.raw()).unwrap();
        assert!(initial.stamp.token == restored.stamp.token);
        assert!(
            initial != restored,
            "restored privilege state erased observed mutation history"
        );
    }

    #[test]
    fn account_control_token_handle_drift_permanently_refuses_channel() {
        let me = || ExpectedPeer::retain(unsafe { GetCurrentProcess() }).unwrap();
        let (server, address) = ControlServer::prepare("S-1-5-21-1-2-3-9999").unwrap();
        let _client = ControlChannel::connect(&address, me()).unwrap();
        let mut server = server.accept(me(), TIMEOUT).unwrap();
        let original = server.peer.token.token.raw();
        unsafe {
            SetHandleInformation(original, HANDLE_FLAG_INHERIT.0, HANDLE_FLAG_INHERIT).unwrap()
        };
        let refused = server.send(b"must-not-send", TIMEOUT);
        unsafe { SetHandleInformation(original, HANDLE_FLAG_INHERIT.0, HANDLE_FLAGS(0)).unwrap() };
        assert!(refused.is_err());
        assert!(!server.usable);
        assert!(server.send(b"restored", TIMEOUT).is_err());
    }

    #[test]
    fn account_control_token_retained_reference_cannot_substitute_same_user_token() {
        let mut original = PrimaryToken::retain(unsafe { GetCurrentProcess() }).unwrap();
        original.token = duplicate(TokenPrimary); // Fault-inject a different native object.
        assert!(original.verify(unsafe { GetCurrentProcess() }).is_err());
    }

    #[test]
    fn account_control_token_raw_runner_predicate_refuses_lowbox_observation() {
        let mut original = PrimaryToken::retain(unsafe { GetCurrentProcess() }).unwrap();
        original.identity.appcontainer = 1; // Predicate test; no SDK profile is created.
        assert!(
            original
                .require_account_runner(&original.identity.user, original.identity.session)
                .is_err()
        );
    }
}
