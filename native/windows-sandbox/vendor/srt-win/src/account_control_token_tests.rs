//! Native token continuity, using disposable child/duplicate tokens only.
use super::*;
use windows::Win32::Security::*;

fn query<T: Default>(token: HANDLE, class: TOKEN_INFORMATION_CLASS) -> T {
    let mut value = T::default();
    let mut length = 0;
    unsafe {
        GetTokenInformation(
            token,
            class,
            Some((&mut value as *mut T).cast()),
            size_of::<T>() as u32,
            &mut length,
        )
        .unwrap();
    }
    assert_eq!(length as usize, size_of::<T>());
    value
}

pub(super) fn toggle_traverse(token: HANDLE) -> TOKEN_PRIVILEGES {
    let mut luid = LUID::default();
    unsafe {
        LookupPrivilegeValueW(
            None,
            windows::core::w!("SeChangeNotifyPrivilege"),
            &mut luid,
        )
        .unwrap()
    };
    let mut words = [0usize; 512];
    let mut length = 0;
    unsafe {
        GetTokenInformation(
            token,
            TokenPrivileges,
            Some(words.as_mut_ptr().cast()),
            size_of_val(&words) as u32,
            &mut length,
        )
        .unwrap()
    };
    let info = unsafe { &*words.as_ptr().cast::<TOKEN_PRIVILEGES>() };
    assert!(
        std::mem::offset_of!(TOKEN_PRIVILEGES, Privileges)
            + info.PrivilegeCount as usize * size_of::<LUID_AND_ATTRIBUTES>()
            <= length as usize
    );
    let entries = unsafe {
        std::slice::from_raw_parts(info.Privileges.as_ptr(), info.PrivilegeCount as usize)
    };
    let original = entries
        .iter()
        .find(|p| p.Luid == luid)
        .expect("disposable token must contain traverse privilege")
        .Attributes;
    let mut change = TOKEN_PRIVILEGES {
        PrivilegeCount: 1,
        Privileges: [LUID_AND_ATTRIBUTES {
            Luid: luid,
            Attributes: TOKEN_PRIVILEGES_ATTRIBUTES(original.0 ^ SE_PRIVILEGE_ENABLED.0),
        }],
    };
    // This helper receives only a disposable child's token, never a host token.
    let mut previous = TOKEN_PRIVILEGES::default();
    let mut returned = 0;
    unsafe {
        SetLastError(ERROR_SUCCESS);
        AdjustTokenPrivileges(
            token,
            false,
            Some(&mut change),
            size_of::<TOKEN_PRIVILEGES>() as u32,
            Some(&mut previous),
            Some(&mut returned),
        )
        .unwrap();
        assert_eq!(GetLastError(), ERROR_SUCCESS);
    }
    previous
}

pub(super) fn mutate_disposable_child(child: &crate::launch::SpawnedChild) -> OwnedHandle {
    let mut raw = HANDLE::default();
    unsafe {
        OpenProcessToken(
            child.process(),
            TOKEN_QUERY | TOKEN_ADJUST_PRIVILEGES,
            &mut raw,
        )
        .unwrap()
    };
    let child_token = OwnedHandle(raw);
    let host = OwnedHandle(crate::token::open_self_token().unwrap());
    let before: TOKEN_STATISTICS = query(child_token.raw(), TokenStatistics);
    let host_before: TOKEN_STATISTICS = query(host.raw(), TokenStatistics);
    assert_ne!(
        before.TokenId, host_before.TokenId,
        "fixture must own a different token"
    );
    let previous = toggle_traverse(child_token.raw());
    let after: TOKEN_STATISTICS = query(child_token.raw(), TokenStatistics);
    let host_after: TOKEN_STATISTICS = query(host.raw(), TokenStatistics);
    assert_eq!(before.TokenId, after.TokenId);
    assert_eq!(previous.PrivilegeCount, 1);
    assert_ne!(
        before.ModifiedId, after.ModifiedId,
        "native mutation was not exercised"
    );
    assert_eq!(
        host_before.ModifiedId, host_after.ModifiedId,
        "host token changed"
    );
    child_token
}

#[test]
fn account_control_original_peer_refuses_native_primary_token_mutation() {
    let child = super::tests::suspended();
    let peer = ExpectedPeer::retain(child.process()).unwrap();
    let _token = mutate_disposable_child(&child);
    assert!(
        peer.alive().is_err(),
        "same live process was trusted after its primary token changed"
    );
}
