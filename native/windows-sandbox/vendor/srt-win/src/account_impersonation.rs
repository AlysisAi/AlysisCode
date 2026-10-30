//! Checked, non-nesting thread impersonation for the disabled account backend.
//!
//! The caller must authenticate the source token before entering. This module
//! proves that the actual thread uses the requested duplicate, and restores the
//! process identity before returning any operation result. It grants no profile
//! ownership, cleanup authority or permission to execute an untrusted callback.

use anyhow::{Context, Result, ensure};
use std::marker::PhantomData;
use std::mem::size_of;
use std::rc::Rc;
use windows::Win32::Foundation::{ERROR_NO_TOKEN, HANDLE};
use windows::Win32::Security::{
    DuplicateTokenEx, GetTokenInformation, ImpersonateLoggedOnUser, RevertToSelf,
    SecurityImpersonation, TOKEN_IMPERSONATE, TOKEN_QUERY, TOKEN_STATISTICS, TokenImpersonation,
    TokenStatistics,
};
use windows::Win32::System::Threading::{GetCurrentThread, GetCurrentThreadId, OpenThreadToken};

use crate::util::OwnedHandle;

/// Never replace an existing caller identity or interpret an unreadable token
/// as the absence of impersonation. OpenAsSelf affects this access check only.
pub(crate) fn require_process_identity() -> Result<()> {
    let mut raw = HANDLE::default();
    match unsafe { OpenThreadToken(GetCurrentThread(), TOKEN_QUERY, true, &mut raw) } {
        Ok(()) => {
            drop(OwnedHandle(raw));
            anyhow::bail!("account maintenance must not replace an existing thread identity")
        }
        Err(error) if error.code() == ERROR_NO_TOKEN.to_hresult() => Ok(()),
        Err(error) => Err(error).context("cannot verify account thread identity"),
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
            size_of::<TOKEN_STATISTICS>() as u32,
            &mut returned,
        )?;
    }
    ensure!(
        returned as usize == size_of::<TOKEN_STATISTICS>(),
        "unexpected token statistics size"
    );
    Ok(value)
}

fn effective_statistics() -> Result<TOKEN_STATISTICS> {
    let mut raw = HANDLE::default();
    unsafe { OpenThreadToken(GetCurrentThread(), TOKEN_QUERY, true, &mut raw)? };
    let token = OwnedHandle(raw);
    statistics(token.raw())
}

fn verify_effective(expected: &TOKEN_STATISTICS) -> Result<()> {
    let actual = effective_statistics()?;
    ensure!(
        actual.TokenId == expected.TokenId
            && actual.AuthenticationId == expected.AuthenticationId
            && actual.ModifiedId == expected.ModifiedId
            && actual.TokenType == TokenImpersonation
            && actual.ImpersonationLevel == SecurityImpersonation,
        "effective thread token differs from requested impersonation"
    );
    Ok(())
}

// Deliberately private and neither Send nor Sync: only the originating thread
// may revert. No returned guard, deserialization, raw-handle escape or Clone.
struct Revert {
    thread: u32,
    _thread_bound: PhantomData<Rc<()>>,
}

impl Drop for Revert {
    fn drop(&mut self) {
        if unsafe { GetCurrentThreadId() } != self.thread
            || unsafe { RevertToSelf() }.is_err()
            || require_process_identity().is_err()
        {
            // Returning would allow profile/ACL recovery under an unverified
            // identity. Do not unwind into higher-level cleanup on failure.
            std::process::abort();
        }
    }
}

/// Invoke trusted synchronous probe code with the authenticated source token.
/// Failures and unwinding revert before caller cleanup; nesting is refused.
pub(crate) fn with_token<T>(
    source: &OwnedHandle,
    operation: impl FnOnce() -> Result<T>,
) -> Result<T> {
    require_process_identity()?;
    let mut raw = HANDLE::default();
    unsafe {
        DuplicateTokenEx(
            source.raw(),
            TOKEN_QUERY | TOKEN_IMPERSONATE,
            None,
            SecurityImpersonation,
            TokenImpersonation,
            &mut raw,
        )?;
    }
    let token = OwnedHandle(raw);
    crate::account_namespace::verify_noninheritable(token.raw())?;
    let expected = statistics(token.raw())?;
    ensure!(
        expected.TokenType == TokenImpersonation
            && expected.ImpersonationLevel == SecurityImpersonation,
        "invalid duplicate impersonation token"
    );
    unsafe { ImpersonateLoggedOnUser(token.raw())? };
    let revert = Revert {
        thread: unsafe { GetCurrentThreadId() },
        _thread_bound: PhantomData,
    };
    verify_effective(&expected)?;
    let result = operation();
    // Detect a trusted callback accidentally changing its context before
    // publishing even a successful result. Reversion still precedes errors.
    let verified = verify_effective(&expected);
    drop(revert);
    verified?;
    result
}

#[cfg(test)]
mod tests {
    use super::*;

    fn source() -> OwnedHandle {
        OwnedHandle(crate::token::open_self_token().unwrap())
    }

    #[test]
    fn account_impersonation_success_error_and_unwind_restore_process_identity() {
        require_process_identity().unwrap();
        let token = source();
        assert_eq!(
            with_token(&token, || {
                let actual = effective_statistics()?;
                ensure!(
                    actual.TokenType == TokenImpersonation,
                    "missing effective token"
                );
                Ok(42)
            })
            .unwrap(),
            42
        );
        require_process_identity().unwrap();
        assert!(with_token(&token, || -> Result<()> { anyhow::bail!("probe refused") }).is_err());
        require_process_identity().unwrap();
        let panic = std::panic::catch_unwind(|| {
            let _ = with_token(&token, || -> Result<()> { panic!("synthetic probe panic") });
        });
        assert!(panic.is_err());
        require_process_identity().unwrap();
    }

    #[test]
    fn account_impersonation_nested_request_preserves_exact_outer_token() {
        let token = source();
        with_token(&token, || {
            let before = effective_statistics()?;
            let mut invoked = false;
            assert!(
                with_token(&token, || {
                    invoked = true;
                    Ok(())
                })
                .is_err()
            );
            assert!(!invoked);
            assert_eq!(effective_statistics()?.TokenId, before.TokenId);
            Ok(())
        })
        .unwrap();
        require_process_identity().unwrap();
    }

    #[test]
    fn account_impersonation_wrong_handle_cannot_invoke_operation() {
        // A real event is a valid owned HANDLE, but can never serve as a token.
        let event = OwnedHandle(unsafe {
            windows::Win32::System::Threading::CreateEventW(None, true, false, None).unwrap()
        });
        let mut invoked = false;
        assert!(
            with_token(&event, || {
                invoked = true;
                Ok(())
            })
            .is_err()
        );
        assert!(!invoked);
        require_process_identity().unwrap();
    }

    #[test]
    fn account_impersonation_callback_context_change_refuses_success() {
        let token = source();
        assert!(
            with_token(&token, || {
                unsafe { RevertToSelf()? };
                Ok(())
            })
            .is_err()
        );
        require_process_identity().unwrap();
    }
}
