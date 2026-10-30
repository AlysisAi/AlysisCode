//! Synthetic descriptor proposals plus read-only native window-station checks.
//! Never calls a USER/kernel security setter, station switch, desktop or process creation.
use super::*;
use crate::account_install::{AccountMode, synthetic_ready};
use crate::util::OwnedSd;
use windows::Win32::{
    Security::{
        DACL_SECURITY_INFORMATION, GetSecurityDescriptorLength, GetUserObjectSecurity,
        OWNER_SECURITY_INFORMATION, PSECURITY_DESCRIPTOR,
    },
    System::RemoteDesktop::ProcessIdToSessionId,
};

pub(super) fn snapshot(text: &str) -> Snapshot {
    let sd = OwnedSd::from_sddl(text).unwrap();
    let length = unsafe { GetSecurityDescriptorLength(sd.ptr) } as usize;
    Snapshot::from_raw_descriptor(unsafe { std::slice::from_raw_parts(sd.ptr.0.cast(), length) })
        .unwrap()
}
fn package() -> String {
    crate::appcontainer::derive("Alysis.Execution.10000000000000000000000000000001")
        .unwrap()
        .string()
        .unwrap()
}

#[test]
fn station_plan_preserves_owner_raw_ace_order_and_dacl_control() {
    let package = package();
    for control in ["", "P", "AI", "PAI"] {
        let before = snapshot(&format!(
            "O:SYD:{control}(D;;0x40000;;;WD)(A;;0x0;;;BA)(A;CIIO;0x2037f;;;SY)(A;ID;0x20000;;;WD)"
        ));
        let after = proposed(&before, &package).unwrap();
        let expected = snapshot(&format!(
            "O:SYD:{control}(D;;0x40000;;;WD)(A;;0x0;;;BA)(A;CIIO;0x2037f;;;SY)(A;;0x2037f;;;{package})(A;ID;0x20000;;;WD)"
        ));
        assert_eq!(after, expected);
        assert_eq!(after.owner, before.owner);
        assert_eq!(after.control, before.control);
        assert_eq!(&after.aces[..3], &before.aces[..3]);
        assert_eq!(after.aces[4], before.aces[3]);
        let added = &after.aces[3];
        assert_eq!(added[1], 0, "grant must not inherit onto any desktop");
        assert_eq!(
            u32::from_le_bytes(added[4..8].try_into().unwrap()) & 0xd0000,
            0,
            "grant must not include DELETE/WRITE_DAC/WRITE_OWNER"
        );
    }
}

#[test]
fn station_plan_refuses_adoption_and_unsupported_precedence() {
    let package = package();
    for text in [
        format!("O:SYD:(A;;0x20000;;;{package})"),
        format!("O:SYD:(D;;0x20000;;;{package})"),
        "O:SYD:(D;ID;0x40000;;;WD)(A;ID;0x20000;;;SY)".into(),
        "O:SYD:(A;ID;0x20000;;;WD)(A;;0x20000;;;SY)".into(),
    ] {
        let before = snapshot(&text);
        assert!(proposed(&before, &package).is_err(), "accepted {text}");
    }
    let mut before = snapshot("O:SYD:P(A;;0x2037f;;;SY)");
    before.control |= 0x100;
    assert!(proposed(&before, &package).is_err());
    assert!(proposed(&snapshot("O:SYD:P(A;;0x2037f;;;SY)"), "S-1-5-18").is_err());
}

#[test]
fn station_plan_bounds_snapshots_and_validates_original_descriptor() {
    let package = package();
    let original = snapshot("O:SYD:P(A;;0x2037f;;;SY)");
    let mut malformed = original.clone();
    malformed.aces[0][2] = 0;
    assert!(proposed(&malformed, &package).is_err());
    let mut large = original.clone();
    large.aces = vec![original.aces[0].clone(); 2000];
    assert!(proposed(&large, &package).is_err());
    let mut bad_owner = original;
    bad_owner.owner = "not-a-SID".into();
    assert!(proposed(&bad_owner, &package).is_err());
}

#[test]
fn station_plan_native_read_only_binds_original_handle_and_raw_descriptor() {
    let current = unsafe { GetProcessWindowStation() }.unwrap();
    let name = object_text(HANDLE(current.0), UOI_NAME).unwrap();
    let before = Snapshot::read(HANDLE(current.0)).unwrap();
    let station = Station::open_current(&name, READ_CONTROL | READ_ATTRIBUTES).unwrap();
    station.verify(&name).unwrap();
    assert_ne!(station.0.0, current.0);
    assert_eq!(station.read().unwrap(), before);
    assert!(station.verify("not-the-current-station").is_err());
    // Calibrate the raw kernel query against the documented USER query on this
    // actual object. Never write either descriptor back to the interactive station.
    let mut words = vec![0usize; 65536 / std::mem::size_of::<usize>()];
    let mut bytes = 0;
    unsafe {
        GetUserObjectSecurity(
            HANDLE(station.0.0),
            &(OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION).0,
            Some(PSECURITY_DESCRIPTOR(words.as_mut_ptr().cast())),
            65536,
            &mut bytes,
        )
    }
    .unwrap();
    let user = Snapshot::from_raw_descriptor(unsafe {
        std::slice::from_raw_parts(words.as_ptr().cast(), bytes as usize)
    })
    .unwrap();
    assert_eq!(user, before);
    drop(station);
    assert_eq!(Snapshot::read(HANDLE(current.0)).unwrap(), before);
}

pub(super) fn identity() -> Identity {
    let mut binding = synthetic_ready().binding(AccountMode::Offline).unwrap();
    binding.owner_sid = crate::sid::current_user_sid().unwrap();
    let candidate = crate::account_state::CandidateJournalIdentity::new(
        binding,
        format!(
            "Alysis.Execution.{}",
            super::super::super::random_id().unwrap()
        ),
    )
    .unwrap();
    let mut execution = super::super::super::fixture_record(candidate);
    execution.scope = RESOURCE_SCOPE.into();
    let mut session_id = 0;
    unsafe { ProcessIdToSessionId(std::process::id(), &mut session_id) }.unwrap();
    Identity {
        execution,
        session_id,
    }
}
pub(super) fn db(id: &Identity) -> Connection {
    let mut db = Connection::open_in_memory().unwrap();
    crate::account_state::initialize_header(&mut db, &id.execution.identity.binding).unwrap();
    db
}

#[test]
fn station_plan_wrong_actor_or_authority_refuses_without_resource_intent() {
    for defect in ["pid", "created", "session", "owner"] {
        let mut id = identity();
        match defect {
            "pid" => id.execution.pid += 100000,
            "created" => id.execution.created += 1,
            "session" => id.session_id += 1,
            "owner" => {
                id.execution.owner = "S-1-5-21-1-2-3-9001".into();
                id.execution.identity.binding.owner_sid = id.execution.owner.clone();
            }
            _ => unreachable!(),
        }
        let db = db(&id);
        let journal = ResourceJournal::open(&db, id, Authority::FixtureLive, true).unwrap();
        assert!(
            journal.prepare_window_station_grant().is_err(),
            "accepted {defect}"
        );
        assert_eq!(journal.replay().unwrap().events, 0);
    }
    let id = identity();
    let db = db(&id);
    drop(ResourceJournal::open(&db, id.clone(), Authority::FixtureLive, true).unwrap());
    let stopped = ResourceJournal::open(&db, id, Authority::FixtureStopped, false).unwrap();
    assert!(stopped.prepare_window_station_grant().is_err());
    assert_eq!(stopped.replay().unwrap().events, 0);
}

#[test]
fn station_plan_native_preparation_never_acknowledges_or_changes_acl() {
    let id = identity();
    let db = db(&id);
    let journal = ResourceJournal::open(&db, id, Authority::FixtureLive, true).unwrap();
    let current = unsafe { GetProcessWindowStation() }.unwrap();
    let name = object_text(HANDLE(current.0), UOI_NAME).unwrap();
    let before = Snapshot::read(HANDLE(current.0)).unwrap();
    let result = journal.prepare_window_station_grant();
    if name == "WinSta0" {
        let plan = result.unwrap();
        plan.verify(&journal).unwrap();
        assert_eq!(plan.operation.before, before);
        assert_eq!(plan.operation.after.aces.len(), before.aces.len() + 1);
        assert_eq!(journal.replay().unwrap().events, 0);
        assert!(
            journal
                .create_intent(Resource::BootstrapDesktop, None)
                .is_err(),
            "a prepared station must not waive resource prerequisites"
        );
        assert_eq!(journal.replay().unwrap().events, 0);
        journal.create_intent(Resource::HostProfile, None).unwrap();
        assert!(
            plan.verify(&journal).is_err(),
            "resource effects invalidate the read-only plan"
        );
        drop(plan);
        eprintln!("station read-only preparation exercised on WinSta0; zero station ACL writes");
    } else {
        assert!(
            result
                .err()
                .unwrap()
                .to_string()
                .contains("unexpected broker window station")
        );
        assert_eq!(journal.replay().unwrap().events, 0);
        eprintln!(
            "noninteractive station correctly refused preparation; positive WinSta0 preparation not exercised here"
        );
    }
    assert_eq!(Snapshot::read(HANDLE(current.0)).unwrap(), before);
}
