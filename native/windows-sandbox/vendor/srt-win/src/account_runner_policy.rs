//! Read-only admission of a dedicated raw account runner, never token repair
//! or authority to launch. Host bootstrap policy is deliberately separate.
use super::*;
use windows::Win32::Foundation::LUID;

// Sealed test-child admission only. This does not relax raw runner admission
// or the production AppContainer verifier.
#[cfg(test)]
#[path = "account_filtered_primary_policy.rs"]
pub(crate) mod filtered;

const ENABLED: u32 = 4;
const DENY_ONLY: u32 = 16;

fn fixed<T: Default>(token: HANDLE, class: TOKEN_INFORMATION_CLASS) -> Result<T> {
    let mut value = T::default();
    let mut returned = 0;
    unsafe {
        GetTokenInformation(
            token,
            class,
            Some((&mut value as *mut T).cast()),
            size_of::<T>() as u32,
            &mut returned,
        )?;
    }
    ensure!(
        returned as usize == size_of::<T>(),
        "dedicated runner token information size differs"
    );
    Ok(value)
}

struct Buffer {
    words: Vec<usize>,
    bytes: usize,
}
impl Buffer {
    fn read(token: HANDLE, class: TOKEN_INFORMATION_CLASS) -> Result<Self> {
        let mut required = 0;
        let first = unsafe { GetTokenInformation(token, class, None, 0, &mut required) };
        ensure!(
            first
                .is_err_and(|e| e.code()
                    == windows::Win32::Foundation::ERROR_INSUFFICIENT_BUFFER.to_hresult())
                && required > 0
                && required <= 64 * 1024,
            "dedicated runner token buffer query invalid"
        );
        let mut words = vec![0usize; (required as usize).div_ceil(size_of::<usize>())];
        let capacity = required;
        unsafe {
            GetTokenInformation(
                token,
                class,
                Some(words.as_mut_ptr().cast()),
                capacity,
                &mut required,
            )?;
        }
        ensure!(
            required > 0 && required <= capacity,
            "dedicated runner token buffer length changed"
        );
        Ok(Self {
            words,
            bytes: required as usize,
        })
    }
    fn header<T>(&self) -> Result<&T> {
        ensure!(
            self.bytes >= size_of::<T>()
                && std::mem::align_of::<T>() <= std::mem::align_of::<usize>(),
            "dedicated runner token header truncated"
        );
        Ok(unsafe { &*self.words.as_ptr().cast::<T>() })
    }
    fn sid(&self, sid: PSID, minimum_offset: usize) -> Result<String> {
        let start = self.words.as_ptr() as usize;
        let end = start + self.bytes;
        let ptr = sid.0 as usize;
        ensure!(
            minimum_offset <= self.bytes
                && ptr >= start + minimum_offset
                && ptr <= end.saturating_sub(8),
            "dedicated runner SID outside native buffer"
        );
        let count = unsafe { *sid.0.cast::<u8>().add(1) } as usize;
        ensure!(
            count <= 15 && 8 + count * 4 <= end - ptr && unsafe { IsValidSid(sid) }.as_bool(),
            "dedicated runner SID invalid or truncated"
        );
        crate::sid::psid_to_string(sid)
    }
    fn array<T>(&self, pointer: *const T, count: usize) -> Result<&[T]> {
        let offset = (pointer as usize)
            .checked_sub(self.words.as_ptr() as usize)
            .context("token array starts before buffer")?;
        ensure!(
            count <= 1024
                && offset <= self.bytes
                && count <= ((self.bytes - offset) / size_of::<T>())
                && (pointer as usize) % std::mem::align_of::<T>() == 0,
            "dedicated runner token array exceeds buffer"
        );
        Ok(unsafe { std::slice::from_raw_parts(pointer, count) })
    }
}

struct Policy {
    user: String,
    primary: bool,
    elevated: u32,
    elevation_type: i32,
    appcontainer: u32,
    ui_access: u32,
    integrity: String,
    groups: Vec<(String, u32)>,
    privileges: Vec<(LUID, u32)>,
}
impl Policy {
    fn validate(&self, account: &str, group: &str, traverse: LUID) -> Result<()> {
        ensure!(self.user == account, "dedicated runner account differs");
        ensure!(
            self.primary
                && self.elevated == 0
                && self.elevation_type == TokenElevationTypeDefault.0
                && self.appcontainer == 0
                && self.ui_access == 0
                && self.integrity == "S-1-16-8192",
            "dedicated runner must be an ordinary Medium primary token without elevation, split-token or UI access"
        );
        // Reject an administrator SID even when deny-only. A filtered linked
        // administrator token is not the dedicated ordinary account contract.
        ensure!(
            !self.groups.iter().any(|(sid, _)| sid == "S-1-5-32-544"),
            "dedicated runner carries an administrative group"
        );
        for expected in ["S-1-5-32-545", group] {
            ensure!(
                self.groups.iter().any(|(sid, flags)| sid == expected
                    && flags & ENABLED != 0
                    && flags & DENY_ONLY == 0),
                "dedicated runner lacks an enabled required account group"
            );
        }
        ensure!(
            self.privileges
                .iter()
                .all(|(id, flags)| flags & SE_PRIVILEGE_ENABLED.0 == 0 || *id == traverse),
            "dedicated runner has an unexpected enabled privilege"
        );
        Ok(())
    }
}

fn same_stamp(left: &TOKEN_STATISTICS, right: &TOKEN_STATISTICS) -> bool {
    left.TokenId == right.TokenId
        && left.AuthenticationId == right.AuthenticationId
        && left.ModifiedId == right.ModifiedId
        && left.TokenType == right.TokenType
        && left.GroupCount == right.GroupCount
        && left.PrivilegeCount == right.PrivilegeCount
}

pub(crate) fn require(token: HANDLE, account: &str, group: &str) -> Result<()> {
    crate::account_impersonation::require_process_identity()?;
    crate::sid::LocalPsid::from_string(account)?;
    crate::sid::LocalPsid::from_string(group)?;
    let before: TOKEN_STATISTICS = fixed(token, TokenStatistics)?;
    let user = Buffer::read(token, TokenUser)?;
    let user = user.sid(
        user.header::<TOKEN_USER>()?.User.Sid,
        size_of::<TOKEN_USER>(),
    )?;
    let mandatory = Buffer::read(token, TokenIntegrityLevel)?;
    let label = mandatory.header::<TOKEN_MANDATORY_LABEL>()?;
    ensure!(
        label.Label.Attributes & 0x20 != 0,
        "dedicated runner integrity SID is not mandatory"
    );
    let integrity = mandatory.sid(label.Label.Sid, size_of::<TOKEN_MANDATORY_LABEL>())?;
    let groups = Buffer::read(token, TokenGroups)?;
    let header = groups.header::<TOKEN_GROUPS>()?;
    let array = groups.array(header.Groups.as_ptr(), header.GroupCount as usize)?;
    let minimum = std::mem::offset_of!(TOKEN_GROUPS, Groups) + std::mem::size_of_val(array);
    let groups = array
        .iter()
        .map(|g| Ok((groups.sid(g.Sid, minimum)?, g.Attributes)))
        .collect::<Result<Vec<_>>>()?;
    let privileges = Buffer::read(token, TokenPrivileges)?;
    let count = *privileges.header::<u32>()? as usize;
    let pointer = unsafe {
        privileges
            .words
            .as_ptr()
            .cast::<u8>()
            .add(std::mem::offset_of!(TOKEN_PRIVILEGES, Privileges))
            .cast::<LUID_AND_ATTRIBUTES>()
    };
    let privileges = privileges
        .array(pointer, count)?
        .iter()
        .map(|p| (p.Luid, p.Attributes.0))
        .collect();
    let elevation: TOKEN_ELEVATION = fixed(token, TokenElevation)?;
    let kind: TOKEN_ELEVATION_TYPE = fixed(token, TokenElevationType)?;
    let policy = Policy {
        user,
        primary: before.TokenType == TokenPrimary,
        elevated: elevation.TokenIsElevated,
        elevation_type: kind.0,
        appcontainer: fixed(token, TokenIsAppContainer)?,
        ui_access: fixed(token, TokenUIAccess)?,
        integrity,
        groups,
        privileges,
    };
    let mut traverse = LUID::default();
    unsafe {
        LookupPrivilegeValueW(
            None,
            windows::core::w!("SeChangeNotifyPrivilege"),
            &mut traverse,
        )?;
    }
    policy.validate(account, group, traverse)?;
    ensure!(
        same_stamp(&before, &fixed(token, TokenStatistics)?),
        "dedicated runner token changed during policy observation"
    );
    Ok(())
}

#[cfg(test)]
#[path = "account_runner_policy_tests.rs"]
mod tests;
