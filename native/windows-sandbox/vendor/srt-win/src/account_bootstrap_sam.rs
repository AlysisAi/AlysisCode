//! Create-only fixed candidate names. Passwords never leave this module; the
//! accounts remain disabled until a future policy/credential installer acts.

use super::{Observed, Principal};
use crate::account_install::{ACCOUNT_GROUP, OFFLINE_USER, ONLINE_USER};
use crate::util::{pcwstr, wstr};
use anyhow::{Context, Result, ensure};
use windows::Win32::{
    Foundation::ERROR_INVALID_PARAMETER,
    NetworkManagement::NetManagement::{
        LOCALGROUP_INFO_1, NERR_UserNotFound, NetApiBufferFree, NetLocalGroupAdd, NetUserAdd,
        NetUserGetInfo, UF_ACCOUNTDISABLE, UF_DONT_EXPIRE_PASSWD, UF_SCRIPT, USER_INFO_1,
        USER_INFO_23, USER_PRIV_USER,
    },
    Security::Cryptography::{BCRYPT_USE_SYSTEM_PREFERRED_RNG, BCryptGenRandom},
};
use windows::core::{PCWSTR, PWSTR};

/// Only fixed operation names and numeric API results; never input buffers.
#[derive(Debug)]
pub(super) struct NativeFailure {
    pub(super) operation: &'static str,
    pub(super) status: u32,
    pub(super) parameter: Option<u32>,
}

impl std::fmt::Display for NativeFailure {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            formatter,
            "candidate {} failed: status={}, parameter={:?}; no adoption or reset attempted",
            self.operation, self.status, self.parameter
        )
    }
}
impl std::error::Error for NativeFailure {}

fn require_success(operation: &'static str, status: u32, parameter: Option<u32>) -> Result<()> {
    if status != 0 {
        return Err(NativeFailure {
            operation,
            status,
            // parm_err is meaningful only for ERROR_INVALID_PARAMETER.
            parameter: (status == ERROR_INVALID_PARAMETER.0)
                .then_some(parameter)
                .flatten(),
        }
        .into());
    }
    Ok(())
}

fn account(name: &str) -> Result<Option<(String, bool)>> {
    let name = wstr(name);
    let mut buffer = std::ptr::null_mut();
    let status = unsafe { NetUserGetInfo(PCWSTR::null(), pcwstr(&name), 23, &mut buffer) };
    if status == NERR_UserNotFound {
        return Ok(None);
    }
    require_success("NetUserGetInfo23", status, None)?;
    ensure!(
        !buffer.is_null(),
        "candidate account observation returned no buffer"
    );
    let info = unsafe { &*buffer.cast::<USER_INFO_23>() };
    let sid = crate::sid::psid_to_string(info.usri23_user_sid);
    let disabled = (info.usri23_flags & UF_ACCOUNTDISABLE).0 != 0;
    unsafe {
        let _ = NetApiBufferFree(Some(buffer.cast()));
    }
    Ok(Some((sid?, disabled)))
}

pub(super) fn observe() -> Result<Observed> {
    // Every coordinator mutation starts with observe(), including fresh root
    // creation, resume, each create intent and final handoff. A failed role
    // query cannot fall through to the local-computer SAM mutation APIs.
    crate::local_account_host::require_local_sam()?;
    Ok(Observed {
        group: crate::user::local_group_sid(ACCOUNT_GROUP)?,
        offline: account(OFFLINE_USER)?,
        online: account(ONLINE_USER)?,
    })
}

struct Password(Vec<u16>);
impl Drop for Password {
    fn drop(&mut self) {
        for value in &mut self.0 {
            unsafe {
                std::ptr::write_volatile(value, 0);
            }
        }
        std::sync::atomic::compiler_fence(std::sync::atomic::Ordering::SeqCst);
    }
}

impl Password {
    fn random() -> Result<Self> {
        // 256 random bits encoded from a 64-character alphabet, plus mandatory
        // character classes. Rejection means setup stops, never weaker policy.
        const ALPHABET: &[u8; 64] =
            b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789!@";
        let mut random = [0u8; 44];
        let status = unsafe { BCryptGenRandom(None, &mut random, BCRYPT_USE_SYSTEM_PREFERRED_RNG) };
        if status.0 != 0 {
            for byte in &mut random {
                unsafe {
                    std::ptr::write_volatile(byte, 0);
                }
            }
            anyhow::bail!("bootstrap password entropy failed: {status:?}");
        }
        let mut value = Self(Vec::with_capacity(49));
        value.0.extend("Aa1!".encode_utf16());
        value.0.extend(
            random
                .iter()
                .map(|byte| u16::from(ALPHABET[(byte & 63) as usize])),
        );
        value.0.push(0);
        for byte in &mut random {
            unsafe {
                std::ptr::write_volatile(byte, 0);
            }
        }
        Ok(value)
    }
}

pub(super) fn create_only(principal: Principal) -> Result<String> {
    crate::local_account_host::require_local_sam()?;
    if let Some(mode) = principal.mode() {
        let mut name = wstr(mode.name());
        let mut password = Password::random()?;
        let mut comment = wstr("Alysis AppContainer candidate; disabled until verified setup");
        let info = USER_INFO_1 {
            usri1_name: PWSTR(name.as_mut_ptr()),
            usri1_password: PWSTR(password.0.as_mut_ptr()),
            usri1_priv: USER_PRIV_USER,
            usri1_comment: PWSTR(comment.as_mut_ptr()),
            usri1_flags: UF_SCRIPT | UF_DONT_EXPIRE_PASSWD | UF_ACCOUNTDISABLE,
            ..Default::default()
        };
        let mut parameter = 0;
        let status = unsafe {
            NetUserAdd(
                PCWSTR::null(),
                1,
                (&info as *const USER_INFO_1).cast(),
                Some(&mut parameter),
            )
        };
        // In particular, NERR_UserExists/GroupExists is never a repair route.
        require_success("NetUserAdd1", status, Some(parameter))?;
        drop(password);
        let (sid, disabled) = account(mode.name())?.context(
            "created candidate account cannot be observed; explicit administrator repair required",
        )?;
        ensure!(
            disabled,
            "created candidate account is unexpectedly enabled"
        );
        Ok(sid)
    } else {
        let mut name = wstr(ACCOUNT_GROUP);
        let mut comment = wstr("Alysis AppContainer candidate account group");
        let info = LOCALGROUP_INFO_1 {
            lgrpi1_name: PWSTR(name.as_mut_ptr()),
            lgrpi1_comment: PWSTR(comment.as_mut_ptr()),
        };
        let mut parameter = 0;
        let status = unsafe {
            NetLocalGroupAdd(
                PCWSTR::null(),
                1,
                (&info as *const LOCALGROUP_INFO_1).cast(),
                Some(&mut parameter),
            )
        };
        require_success("NetLocalGroupAdd1", status, Some(parameter))?;
        crate::user::local_group_sid(ACCOUNT_GROUP)?.context(
            "created candidate group cannot be observed; explicit administrator repair required",
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bootstrap_native_failure_retains_only_api_status_and_valid_parameter_index() {
        require_success("NetUserAdd1", 0, Some(99)).unwrap();
        for (status, parameter) in [(87, Some(7)), (2245, None), (2224, None), (5, None)] {
            let error = require_success("NetUserAdd1", status, Some(7)).unwrap_err();
            let diagnostic = error.downcast_ref::<NativeFailure>().unwrap();
            assert_eq!(diagnostic.operation, "NetUserAdd1");
            assert_eq!(diagnostic.status, status);
            assert_eq!(diagnostic.parameter, parameter);
        }
    }

    #[test]
    fn bootstrap_initial_password_is_random_long_and_class_complete() {
        let first = Password::random().unwrap();
        let second = Password::random().unwrap();
        assert_eq!(first.0.len(), 49);
        assert_eq!(first.0.last(), Some(&0));
        assert_ne!(first.0, second.0);
        assert!(first.0[..48].iter().all(|value| (33..=126).contains(value)));
        for class in [
            b"ABCDEFGHIJKLMNOPQRSTUVWXYZ".as_slice(),
            b"abcdefghijklmnopqrstuvwxyz",
            b"0123456789",
            b"!@",
        ] {
            assert!(first.0.iter().any(|value| class.contains(&(*value as u8))));
        }
    }
}
