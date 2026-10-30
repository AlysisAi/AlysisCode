//! Only new private desktops; never switch input/thread desktops, edit WinSta0,
//! create accounts or launch a process. Full execution composition is pending.
use super::*;
fn plan() -> Plan {
    let mut p = super::super::tests::plan();
    let owner = crate::sid::current_user_sid().unwrap();
    p.execution.owner = owner.clone();
    p.execution.identity.binding.owner_sid = owner;
    unsafe { ProcessIdToSessionId(std::process::id(), &mut p.session) }.unwrap();
    p
}
#[test]
fn desktop_post_crash_observer_does_not_gain_original_actor_authority() {
    let mut p = plan();
    p.execution.pid = if p.execution.pid == 1 { 2 } else { 1 };
    let subject = Subject::Atomic(p.clone());
    assert!(actor(&subject).is_err());
    fixture_observer_actor(&subject).unwrap();
    p.session = p.session.wrapping_add(1);
    assert!(fixture_observer_actor(&Subject::Atomic(p)).is_err());
    // Admission-only regression: never open/create a native desktop or station.
}
#[test]
fn atomic_desktop_native_exclusive_birth_refuses_collision_and_preserves_original() {
    let p = plan();
    let original = unsafe { GetThreadDesktop(GetCurrentThreadId()) }.unwrap();
    let mut desktop = NativeDesktop::create(&p).unwrap();
    for _ in 0..2 {
        let error = NativeDesktop::create(&p)
            .err()
            .expect("existing desktop adopted");
        assert!(
            error
                .chain()
                .filter_map(|e| e.downcast_ref::<windows::core::Error>())
                .any(|e| e.code() == windows::core::HRESULT::from_win32(ERROR_ALREADY_EXISTS.0))
        );
        desktop.verify().unwrap();
    }
    assert_eq!(
        unsafe { GetThreadDesktop(GetCurrentThreadId()) }.unwrap(),
        original
    );
    desktop.close().unwrap();
}
#[test]
fn atomic_desktop_native_extra_reference_refuses_absence_without_closing_foreign_handle() {
    let p = plan();
    let mut desktop = NativeDesktop::create(&p).unwrap();
    let extra = relative_open(&desktop.api, desktop.parent.raw(), &name(&p).unwrap()).unwrap();
    assert!(format!("{:#}", desktop.close().unwrap_err()).contains("remains referenced"));
    assert_eq!(
        Snapshot::read(HANDLE(extra.0)).unwrap(),
        Snapshot::from_raw_descriptor(bytes(&descriptor(&p, true, true).unwrap()).unwrap())
            .unwrap()
    );
    unsafe { CloseDesktop(extra) }.unwrap();
    let parent = Station::open_current("WinSta0", PARENT_ACCESS).unwrap();
    let api = Api::load().unwrap();
    let e = relative_open(&api, parent.raw(), &name(&p).unwrap()).unwrap_err();
    assert_eq!(
        e.downcast_ref::<windows::core::Error>().unwrap().code(),
        windows::core::HRESULT::from_win32(ERROR_FILE_NOT_FOUND.0)
    );
}
#[test]
fn atomic_desktop_native_refuses_foreign_actor_before_effect() {
    let mut p = plan();
    p.session = p.session.wrapping_add(1);
    assert!(NativeDesktop::create(&p).is_err());
    let parent = Station::open_current("WinSta0", PARENT_ACCESS).unwrap();
    let e = relative_open(&Api::load().unwrap(), parent.raw(), &name(&p).unwrap()).unwrap_err();
    assert_eq!(
        e.downcast_ref::<windows::core::Error>().unwrap().code(),
        windows::core::HRESULT::from_win32(ERROR_FILE_NOT_FOUND.0)
    );
}

#[test]
fn atomic_desktop_native_refuses_win32_existing_object_without_modification() {
    let p = plan();
    let private = descriptor(&p, false, false).unwrap();
    let sa = SECURITY_ATTRIBUTES {
        nLength: size_of::<SECURITY_ATTRIBUTES>() as u32,
        lpSecurityDescriptor: private.ptr.0,
        bInheritHandle: false.into(),
    };
    let wide = crate::util::wstr(&name(&p).unwrap());
    let foreign = unsafe {
        CreateDesktopW(
            windows::core::PCWSTR(wide.as_ptr()),
            None,
            None,
            DESKTOP_CONTROL_FLAGS(0),
            DESKTOP_ALL,
            Some(&sa),
        )
    }
    .unwrap();
    let before =
        security::security_shape(&security::query_security(HANDLE(foreign.0)).unwrap()).unwrap();
    let error = NativeDesktop::create(&p)
        .err()
        .expect("Win32 existing desktop adopted");
    assert!(
        error
            .chain()
            .filter_map(|e| e.downcast_ref::<windows::core::Error>())
            .any(|e| e.code() == windows::core::HRESULT::from_win32(ERROR_ALREADY_EXISTS.0))
    );
    assert_eq!(
        security::security_shape(&security::query_security(HANDLE(foreign.0)).unwrap()).unwrap(),
        before
    );
    unsafe { CloseDesktop(foreign) }.unwrap();
}

#[test]
fn atomic_desktop_native_birth_failures_release_only_original_reference() {
    struct Reset;
    impl Drop for Reset {
        fn drop(&mut self) {
            FAIL_STAGE.with(|s| s.set(None));
        }
    }
    let _reset = Reset;
    for stage in [
        BirthStage::Private,
        BirthStage::Labelled,
        BirthStage::Published,
    ] {
        let p = plan();
        FAIL_STAGE.with(|s| s.set(Some(stage)));
        let error = NativeDesktop::create(&p)
            .err()
            .expect("injected birth failure absent");
        assert!(format!("{error:#}").contains("injected desktop birth failure"));
        FAIL_STAGE.with(|s| s.set(None));
        let parent = Station::open_current("WinSta0", PARENT_ACCESS).unwrap();
        let e = relative_open(&Api::load().unwrap(), parent.raw(), &name(&p).unwrap()).unwrap_err();
        assert_eq!(
            e.downcast_ref::<windows::core::Error>().unwrap().code(),
            windows::core::HRESULT::from_win32(ERROR_FILE_NOT_FOUND.0)
        );
    }
}
