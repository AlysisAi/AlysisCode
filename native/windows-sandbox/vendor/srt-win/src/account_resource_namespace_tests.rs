//! Default tests are synthetic SQLite/security parsing only. The effectful
//! sequence has one ignored entry point guarded for disposable hosted CI.
use super::*;
use crate::account_install::{AccountMode, synthetic_ready};
use crate::util::OwnedSd;
use windows::Win32::{
    Foundation::*,
    Security::*,
    System::{RemoteDesktop::ProcessIdToSessionId, Threading::*},
};

fn identity() -> Identity {
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
    let mut session = 0;
    unsafe {
        ProcessIdToSessionId(std::process::id(), &mut session).unwrap();
    }
    Identity {
        execution,
        session_id: session,
    }
}
fn database(identity: &Identity) -> Connection {
    let mut db = Connection::open_in_memory().unwrap();
    crate::account_state::initialize_header(&mut db, &identity.execution.identity.binding).unwrap();
    db
}
fn live<'a>(db: &'a Connection, id: &Identity, create: bool) -> ResourceJournal<'a> {
    ResourceJournal::open(db, id.clone(), Authority::FixtureLive, create).unwrap()
}
fn stopped<'a>(db: &'a Connection, id: &Identity) -> ResourceJournal<'a> {
    ResourceJournal::open(db, id.clone(), Authority::FixtureStopped, false).unwrap()
}
fn security_bytes(sd: &OwnedSd) -> Vec<u8> {
    let n = unsafe { GetSecurityDescriptorLength(sd.ptr) } as usize;
    unsafe { std::slice::from_raw_parts(sd.ptr.0.cast::<u8>(), n) }.to_vec()
}
#[test]
fn candidate_namespace_security_parser_is_bounded_and_preserves_exact_components() {
    let sd = OwnedSd::from_sddl("O:SYD:P(A;;0xf000f;;;SY)S:P(ML;;NW;;;LW)").unwrap();
    let bytes = security_bytes(&sd);
    let expected = native::security_shape(&bytes).unwrap();
    for n in 0..bytes.len() {
        assert!(
            native::security_shape(&bytes[..n]).is_err(),
            "accepted truncation {n}"
        );
    }
    for index in [4usize, 12, 16] {
        let mut bad = bytes.clone();
        bad[index..index + 4].copy_from_slice(&u32::MAX.to_le_bytes());
        assert!(native::security_shape(&bad).is_err());
    }
    let other = OwnedSd::from_sddl("O:SYD:P(A;;0xf000f;;;SY)S:P(ML;;NW;;;ME)").unwrap();
    assert_ne!(
        expected,
        native::security_shape(&security_bytes(&other)).unwrap()
    );
    let other = OwnedSd::from_sddl("O:SYD:P(A;;0xf000e;;;SY)S:P(ML;;NW;;;LW)").unwrap();
    assert_ne!(
        expected,
        native::security_shape(&security_bytes(&other)).unwrap()
    );
    let mut overlap = bytes.clone();
    overlap[16..20].copy_from_slice(&bytes[12..16]);
    assert!(native::security_shape(&overlap).is_err());
}
#[test]
fn candidate_namespace_birth_requires_original_actor_and_live_authority_before_effect() {
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
        let db = database(&id);
        let journal = live(&db, &id, true);
        assert!(journal.create_empty_namespace_root().is_err());
        assert_eq!(journal.replay().unwrap().events, 0);
    }
    let id = identity();
    let db = database(&id);
    drop(live(&db, &id, true));
    assert!(stopped(&db, &id).create_empty_namespace_root().is_err());
    assert_eq!(stopped(&db, &id).replay().unwrap().events, 0);
}
#[test]
fn candidate_namespace_typed_receipt_and_independent_birth_do_not_waive_bootstrap() {
    let id = identity();
    let db = database(&id);
    let journal = live(&db, &id, true);
    let nonce = journal.create_intent(ROLE, None).unwrap();
    assert!(
        journal
            .created(
                ROLE,
                nonce.clone(),
                Observation::EmptyNamespaceRoot { policy_version: 3 }
            )
            .is_err()
    );
    assert!(
        journal
            .created(
                ROLE,
                nonce.clone(),
                Observation::EmptyBootstrapJob { policy_version: 1 }
            )
            .is_err()
    );
    assert!(
        journal
            .created(
                ROLE,
                nonce.clone(),
                Observation::EmptyNamespaceRoot { policy_version: 1 }
            )
            .is_err()
    );
    journal.created(ROLE, nonce, observation()).unwrap();
    assert!(
        journal
            .create_intent(Resource::BootstrapDesktop, None)
            .is_err()
    );
    assert!(
        journal
            .create_intent(Resource::BootstrapProcess, None)
            .is_err()
    );
    assert!(journal.create_empty_namespace_root().is_err()); // mixed/existing plan rejected before native open
    assert_eq!(journal.replay().unwrap().events, 2);
}

#[cfg(test)]
#[path = "account_resource_namespace_host_tests.rs"]
mod disposable;

#[test]
fn candidate_namespace_private_birth_parser_accepts_absent_label_without_orphan_offsets() {
    // The native effect fixture independently checks this same policy through
    // Windows readback before and after Low labelling; this test only parses.
    let sd = OwnedSd::from_sddl("O:SYD:P(A;;0xf000f;;;SY)").unwrap();
    let bytes = security_bytes(&sd);
    native::security_shape(&bytes).unwrap();
    let mut orphan_sacl = bytes.clone();
    orphan_sacl[12..16].copy_from_slice(&20u32.to_le_bytes());
    assert!(native::security_shape(&orphan_sacl).is_err());
    for n in 0..bytes.len() {
        assert!(native::security_shape(&bytes[..n]).is_err());
    }
}
