use super::*;
const ACCOUNT: &str = "S-1-5-21-1-2-3-1003";
const GROUP: &str = "S-1-5-21-1-2-3-1002";
fn traverse() -> LUID {
    LUID {
        LowPart: 23,
        HighPart: 0,
    }
}
fn policy() -> Policy {
    Policy {
        user: ACCOUNT.into(),
        primary: true,
        elevated: 0,
        elevation_type: TokenElevationTypeDefault.0,
        appcontainer: 0,
        ui_access: 0,
        integrity: "S-1-16-8192".into(),
        groups: vec![("S-1-5-32-545".into(), ENABLED), (GROUP.into(), ENABLED)],
        privileges: vec![(traverse(), SE_PRIVILEGE_ENABLED.0)],
    }
}

#[test]
fn dedicated_runner_policy_refuses_elevation_split_admin_ui_access_and_wrong_role() {
    policy().validate(ACCOUNT, GROUP, traverse()).unwrap();
    let defects: Vec<Box<dyn Fn(&mut Policy)>> = vec![
        Box::new(|p| p.user = "S-1-5-18".into()),
        Box::new(|p| p.primary = false),
        Box::new(|p| p.elevated = 1),
        Box::new(|p| p.elevation_type = TokenElevationTypeFull.0),
        Box::new(|p| p.elevation_type = TokenElevationTypeLimited.0),
        Box::new(|p| p.appcontainer = 1),
        Box::new(|p| p.ui_access = 1),
        Box::new(|p| p.integrity = "S-1-16-4096".into()),
        Box::new(|p| p.integrity = "S-1-16-12288".into()),
        Box::new(|p| p.groups.push(("S-1-5-32-544".into(), ENABLED))),
        Box::new(|p| p.groups.push(("S-1-5-32-544".into(), DENY_ONLY))),
        Box::new(|p| {
            p.privileges.push((
                LUID {
                    LowPart: 99,
                    HighPart: 0,
                },
                SE_PRIVILEGE_ENABLED.0,
            ))
        }),
    ];
    for defect in defects {
        let mut p = policy();
        defect(&mut p);
        assert!(p.validate(ACCOUNT, GROUP, traverse()).is_err());
    }
}

#[test]
fn dedicated_runner_policy_requires_enabled_builtin_and_selected_groups() {
    for index in 0..2 {
        for flags in [0, DENY_ONLY, ENABLED | DENY_ONLY] {
            let mut p = policy();
            p.groups[index].1 = flags;
            assert!(p.validate(ACCOUNT, GROUP, traverse()).is_err());
        }
        let mut p = policy();
        p.groups.remove(index);
        assert!(p.validate(ACCOUNT, GROUP, traverse()).is_err());
    }
    assert!(
        policy()
            .validate(ACCOUNT, "S-1-5-21-1-2-3-9999", traverse())
            .is_err()
    );
    let mut p = policy();
    p.privileges.clear();
    p.validate(ACCOUNT, GROUP, traverse()).unwrap();
    // Disabled ordinary account privileges are not equivalent to enabled ones;
    // the later lowbox construction removes all but traverse independently.
    p.privileges.push((
        LUID {
            LowPart: 99,
            HighPart: 0,
        },
        0,
    ));
    p.validate(ACCOUNT, GROUP, traverse()).unwrap();
}

#[test]
fn dedicated_runner_policy_native_observation_is_read_only_and_rejects_foreign_identity() {
    let token = OwnedHandle(crate::token::open_self_token().unwrap());
    let before: TOKEN_STATISTICS = fixed(token.raw(), TokenStatistics).unwrap();
    let error = require(token.raw(), ACCOUNT, GROUP).unwrap_err();
    assert!(
        error.to_string().contains("account differs"),
        "unexpected native observation failure: {error:#}"
    );
    assert!(same_stamp(
        &before,
        &fixed(token.raw(), TokenStatistics).unwrap()
    ));
    assert!(require(HANDLE::default(), ACCOUNT, GROUP).is_err());
    assert!(
        require(
            unsafe { windows::Win32::System::Threading::GetCurrentProcess() },
            ACCOUNT,
            GROUP
        )
        .is_err()
    );
}

#[test]
fn dedicated_runner_policy_refuses_impersonation_without_replacing_it() {
    let token = OwnedHandle(crate::token::open_self_token().unwrap());
    unsafe {
        ImpersonateLoggedOnUser(token.raw()).unwrap();
    }
    let refused = require(token.raw(), ACCOUNT, GROUP);
    let mut raw = HANDLE::default();
    let still_set = unsafe {
        windows::Win32::System::Threading::OpenThreadToken(
            windows::Win32::System::Threading::GetCurrentThread(),
            TOKEN_QUERY,
            true,
            &mut raw,
        )
    };
    let held = (!raw.is_invalid()).then(|| OwnedHandle(raw));
    unsafe {
        RevertToSelf().unwrap();
    }
    assert!(refused.is_err());
    assert!(still_set.is_ok());
    drop(held);
}

#[test]
fn dedicated_runner_policy_bounds_native_sid_and_variable_array_views() {
    let buffer = Buffer {
        words: vec![0; 8],
        bytes: 8,
    };
    assert!(buffer.header::<TOKEN_USER>().is_err());
    assert!(buffer.sid(PSID::default(), 0).is_err());
    assert!(
        buffer
            .sid(PSID(buffer.words.as_ptr() as *mut _), 9)
            .is_err()
    );
    assert!(
        buffer
            .sid(PSID(buffer.words.as_ptr() as *mut _), 0)
            .is_err()
    ); // invalid revision
    assert!(buffer.array(buffer.words.as_ptr(), 2).is_err());
    assert!(
        buffer
            .array(unsafe { buffer.words.as_ptr().add(2) }, 1)
            .is_err()
    );
    assert!(buffer.array(buffer.words.as_ptr(), usize::MAX).is_err());
    let empty = Buffer {
        words: vec![0; 1],
        bytes: 4,
    };
    let tail = unsafe {
        empty
            .words
            .as_ptr()
            .cast::<u8>()
            .add(4)
            .cast::<LUID_AND_ATTRIBUTES>()
    };
    assert_eq!(empty.array(tail, 0).unwrap().len(), 0);
    assert!(empty.array(tail, 1).is_err());
    let before: TOKEN_STATISTICS = unsafe { std::mem::zeroed() };
    let mut after = before;
    after.ModifiedId.LowPart = 1;
    assert!(!same_stamp(&before, &after));
}

#[test]
fn dedicated_runner_policy_keeps_host_profile_actor_admission_distinct() {
    let actor = crate::sid::current_user_sid().unwrap();
    crate::account_token::verify_raw_profile_actor(&actor).unwrap();
    assert!(crate::account_token::verify_raw_profile_actor(ACCOUNT).is_err());
    let token = OwnedHandle(crate::token::open_self_token().unwrap());
    let kind: TOKEN_ELEVATION_TYPE = fixed(token.raw(), TokenElevationType).unwrap();
    if kind != TokenElevationTypeDefault {
        assert!(crate::account_token::verify_account_runner(&actor).is_err());
    }
    unsafe {
        ImpersonateLoggedOnUser(token.raw()).unwrap();
    }
    let refused = crate::account_token::verify_raw_profile_actor(&actor);
    unsafe {
        RevertToSelf().unwrap();
    }
    assert!(refused.is_err());
}
