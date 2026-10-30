//! Real local pipes, retained empty jobs and synthetic SQLite. No account/profile
//! setup, process assignment or installed state. Fixtures bypass only installed
//! admission; the native guard and journal transition/readback code is unchanged.
use super::*;
use crate::{
    account_control::{ControlServer, ExpectedPeer},
    account_install::{AccountMode, synthetic_ready},
};
use windows::Win32::System::{RemoteDesktop::ProcessIdToSessionId, Threading::GetCurrentProcess};

const TIMEOUT: Duration = Duration::from_secs(1);

fn identity() -> Identity {
    let mut binding = synthetic_ready().binding(AccountMode::Offline).unwrap();
    binding.owner_sid = crate::sid::current_user_sid().unwrap();
    let candidate = crate::account_state::CandidateJournalIdentity::new(
        binding,
        format!(
            "Alysis.Execution.{}",
            crate::account_state::execution::random_id().unwrap()
        ),
    )
    .unwrap();
    let mut execution = crate::account_state::execution::fixture_record(candidate);
    execution.scope = RESOURCE_SCOPE.into();
    let mut session_id = 0;
    unsafe {
        ProcessIdToSessionId(std::process::id(), &mut session_id).unwrap();
    }
    Identity {
        execution,
        session_id,
    }
}
fn database(identity: &Identity) -> Connection {
    let mut db = Connection::open_in_memory().unwrap();
    crate::account_state::initialize_header(&mut db, &identity.execution.identity.binding).unwrap();
    db
}
fn journal<'a>(db: &'a Connection, id: &Identity, create: bool) -> ResourceJournal<'a> {
    ResourceJournal::open(db, id.clone(), Authority::FixtureLive, create).unwrap()
}
fn pair() -> (ControlChannel<'static>, ControlChannel<'static>) {
    let (server, address) = ControlServer::prepare("S-1-5-21-1-2-3-9999").unwrap();
    let client = ControlChannel::connect(
        &address,
        ExpectedPeer::retain(unsafe { GetCurrentProcess() }).unwrap(),
    )
    .unwrap();
    let server = server
        .accept(
            ExpectedPeer::retain(unsafe { GetCurrentProcess() }).unwrap(),
            TIMEOUT,
        )
        .unwrap();
    (server, client)
}
fn request(id: &Identity, nonce: &str) -> Request {
    Request::new(id.clone(), nonce.into()).unwrap()
}
fn client<'a>(channel: ControlChannel<'a>, id: &Identity, nonce: &str) -> EmptyJobConfirmation<'a> {
    EmptyJobConfirmation::start(
        channel,
        id.execution.clone(),
        id.session_id,
        nonce.into(),
        TIMEOUT,
    )
    .unwrap()
}
fn cleanup(db: &Connection, id: &Identity, guard: &mut EmptyBootstrapJob) {
    let stopped = ResourceJournal::open(db, id.clone(), Authority::FixtureStopped, false).unwrap();
    stopped.retire_empty_bootstrap_job(guard).unwrap();
    stopped.recovered().unwrap();
}

#[test]
fn resource_control_native_birth_ack_follows_commit_and_fresh_database_readback() {
    let id = identity();
    let root =
        crate::util::test_temp_dir().join(format!("resource-control-{}", id.execution.transaction));
    std::fs::create_dir(&root).unwrap();
    let path = root.join("synthetic-state.db");
    let mut db = Connection::open(&path).unwrap();
    crate::account_state::initialize_header(&mut db, &id.execution.identity.binding).unwrap();
    db.execute_batch("PRAGMA journal_mode=DELETE; PRAGMA synchronous=FULL;")
        .unwrap();
    let journal = journal(&db, &id, true);
    let mut guard = journal.create_empty_bootstrap_job().unwrap();
    let nonce = guard.birth_operation_nonce().to_owned();
    let (server, peer) = pair();
    let mut control = ResourceControl::new(server, &journal).unwrap();
    let peer = client(peer, &id, &nonce);
    assert_eq!(journal.replay().unwrap().events, 1);
    control
        .acknowledge_empty_bootstrap_job_birth(&journal, &mut guard, TIMEOUT)
        .unwrap();
    // Read through a separate connection before consuming the network ack.
    let reopened = Connection::open(&path).unwrap();
    let observed = self::journal(&reopened, &id, false).replay().unwrap();
    assert_eq!(observed.events, 2);
    assert!(matches!(
        observed.states[&Resource::BootstrapJob],
        State::Present { .. }
    ));
    peer.finish(TIMEOUT).unwrap();
    drop(reopened);
    assert!(
        control
            .acknowledge_empty_bootstrap_job_birth(&journal, &mut guard, TIMEOUT)
            .is_err()
    );
    assert!(control.channel.is_none());
    assert_eq!(journal.replay().unwrap().events, 2);
    cleanup(&db, &id, &mut guard);
    drop(journal);
    drop(db);
    std::fs::remove_file(path).unwrap();
    std::fs::remove_dir(root).unwrap();
}

#[test]
fn resource_control_commit_failures_send_no_ack_and_keep_original_intent_or_receipt() {
    for after_commit in [false, true] {
        let id = identity();
        let db = database(&id);
        let journal = journal(&db, &id, true);
        let mut guard = journal.create_empty_bootstrap_job().unwrap();
        let (server, peer) = pair();
        let mut control = ResourceControl::new(server, &journal).unwrap();
        let peer = client(peer, &id, guard.birth_operation_nonce());
        if after_commit {
            db.execute_batch("CREATE TRIGGER fail_confirmation AFTER INSERT ON candidate_resource_events BEGIN UPDATE candidate_resource_runs SET session_id=session_id+1; END;").unwrap();
        } else {
            db.execute_batch("CREATE TRIGGER fail_confirmation BEFORE INSERT ON candidate_resource_events BEGIN SELECT RAISE(ABORT,'injected'); END;").unwrap();
        }
        assert!(
            control
                .acknowledge_empty_bootstrap_job_birth(&journal, &mut guard, TIMEOUT)
                .is_err()
        );
        assert!(control.channel.is_none());
        assert!(peer.finish(TIMEOUT).is_err(), "failure published an ack");
        let count: usize = db
            .query_row("SELECT count(*) FROM candidate_resource_events", [], |r| {
                r.get(0)
            })
            .unwrap();
        assert_eq!(count, if after_commit { 2 } else { 1 });
        // Repair only our deliberately damaged fixture. The failed connection
        // stays closed, and rechecking the original guard never repeats birth.
        db.execute_batch("DROP TRIGGER fail_confirmation").unwrap();
        if after_commit {
            db.execute(
                "UPDATE candidate_resource_runs SET session_id=?1",
                [id.session_id],
            )
            .unwrap();
        }
        assert!(
            control
                .acknowledge_empty_bootstrap_job_birth(&journal, &mut guard, TIMEOUT)
                .is_err()
        );
        journal.record_empty_bootstrap_job(&mut guard).unwrap();
        assert_eq!(journal.replay().unwrap().events, 2);
        cleanup(&db, &id, &mut guard);
    }
}

#[test]
fn resource_control_foreign_or_malformed_requests_cannot_commit_birth() {
    for defect in [
        "protocol",
        "nonce",
        "session",
        "pid",
        "created",
        "transaction",
        "database",
        "pending",
        "account",
        "scope",
        "unknown",
        "json",
    ] {
        let id = identity();
        let db = database(&id);
        let journal = journal(&db, &id, true);
        let mut guard = journal.create_empty_bootstrap_job().unwrap();
        let (server, mut peer) = pair();
        let mut control = ResourceControl::new(server, &journal).unwrap();
        let mut wire = serde_json::to_value(request(&id, guard.birth_operation_nonce())).unwrap();
        match defect {
            "protocol" => wire["protocol"] = "unknown".into(),
            "nonce" => wire["operation_nonce"] = "f".repeat(32).into(),
            "session" => wire["identity"]["session_id"] = (id.session_id + 1).into(),
            "pid" => wire["identity"]["execution"]["pid"] = (id.execution.pid + 1).into(),
            "created" => {
                wire["identity"]["execution"]["created"] = (id.execution.created + 1).into()
            }
            "transaction" => wire["identity"]["execution"]["transaction"] = "e".repeat(32).into(),
            "database" => wire["identity"]["execution"]["database_id"][0] = 99.into(),
            "pending" => wire["identity"]["execution"]["pending_id"][0] = 99.into(),
            "account" => {
                wire["identity"]["execution"]["identity"]["binding"]["account_sid"] =
                    "S-1-5-21-1-2-3-9998".into()
            }
            "scope" => wire["identity"]["execution"]["scope"] = "explicit-read-acls-v1".into(),
            "unknown" => wire["effect"] = "create_profile".into(),
            "json" => {}
            _ => unreachable!(),
        }
        let bytes = if defect == "json" {
            b"{incomplete".to_vec()
        } else {
            serde_json::to_vec(&wire).unwrap()
        };
        peer.send(&bytes, TIMEOUT).unwrap();
        assert!(
            control
                .acknowledge_empty_bootstrap_job_birth(&journal, &mut guard, TIMEOUT)
                .is_err(),
            "accepted {defect}"
        );
        assert!(control.channel.is_none());
        assert_eq!(journal.replay().unwrap().events, 1);
        assert!(peer.receive(TIMEOUT).is_err());
        journal.record_empty_bootstrap_job(&mut guard).unwrap();
        cleanup(&db, &id, &mut guard);
    }
}

#[test]
fn resource_control_original_guard_is_required_even_for_an_exact_request() {
    let id = identity();
    let db = database(&id);
    let journal = journal(&db, &id, true);
    let mut original = journal.create_empty_bootstrap_job().unwrap();
    let foreign_id = identity();
    let foreign_db = database(&foreign_id);
    let foreign = self::journal(&foreign_db, &foreign_id, true);
    let mut wrong_guard = foreign.create_empty_bootstrap_job().unwrap();
    let (server, peer) = pair();
    let mut control = ResourceControl::new(server, &journal).unwrap();
    // Correct selected operation data still cannot replace the native guard.
    let peer = client(peer, &id, wrong_guard.birth_operation_nonce());
    assert!(
        control
            .acknowledge_empty_bootstrap_job_birth(&journal, &mut wrong_guard, TIMEOUT)
            .is_err()
    );
    assert!(peer.finish(TIMEOUT).is_err());
    assert_eq!(journal.replay().unwrap().events, 1);
    assert_eq!(foreign.replay().unwrap().events, 1);
    journal.record_empty_bootstrap_job(&mut original).unwrap();
    foreign
        .record_empty_bootstrap_job(&mut wrong_guard)
        .unwrap();
    cleanup(&db, &id, &mut original);
    cleanup(&foreign_db, &foreign_id, &mut wrong_guard);
}

#[test]
fn resource_control_disconnected_reply_preserves_committed_birth_without_rollback() {
    let id = identity();
    let db = database(&id);
    let journal = journal(&db, &id, true);
    let mut guard = journal.create_empty_bootstrap_job().unwrap();
    let (server, peer) = pair();
    let mut control = ResourceControl::new(server, &journal).unwrap();
    let peer = client(peer, &id, guard.birth_operation_nonce());
    let pending = control
        .begin_empty_job_birth(&journal, guard.birth_operation_nonce(), TIMEOUT)
        .unwrap();
    journal.record_empty_bootstrap_job(&mut guard).unwrap();
    drop(peer); // Deterministically disconnect between durable commit and reply.
    assert!(
        control
            .finish_empty_job_birth(pending, &journal, TIMEOUT)
            .is_err()
    );
    assert!(control.channel.is_none());
    assert_eq!(journal.replay().unwrap().events, 2);
    assert!(
        control
            .acknowledge_empty_bootstrap_job_birth(&journal, &mut guard, TIMEOUT)
            .is_err()
    );
    journal.record_empty_bootstrap_job(&mut guard).unwrap();
    cleanup(&db, &id, &mut guard);
}

#[test]
fn resource_control_client_refuses_forged_ack_or_wrong_endpoint_role() {
    for defect in [
        "nonce",
        "session",
        "protocol",
        "sequence",
        "observation",
        "unknown",
        "json",
    ] {
        let id = identity();
        let (mut server, peer) = pair();
        let nonce = "1".repeat(32);
        let peer = client(peer, &id, &nonce);
        server.receive(TIMEOUT).unwrap();
        let mut ack = serde_json::to_value(Acknowledgment {
            request: request(&id, &nonce),
            committed_sequence: 1,
            observation: Observation::EmptyBootstrapJob { policy_version: 1 },
        })
        .unwrap();
        match defect {
            "nonce" => ack["request"]["operation_nonce"] = "2".repeat(32).into(),
            "session" => ack["request"]["identity"]["session_id"] = (id.session_id + 1).into(),
            "protocol" => ack["request"]["protocol"] = "different".into(),
            "sequence" => ack["committed_sequence"] = 0.into(),
            "observation" => ack["observation"]["policy_version"] = 2.into(),
            "unknown" => ack["retirement_authority"] = true.into(),
            "json" => {}
            _ => unreachable!(),
        }
        server
            .send(
                &if defect == "json" {
                    b"true".to_vec()
                } else {
                    serde_json::to_vec(&ack).unwrap()
                },
                TIMEOUT,
            )
            .unwrap();
        assert!(peer.finish(TIMEOUT).is_err(), "accepted {defect}");
    }
    let id = identity();
    let db = database(&id);
    let journal = journal(&db, &id, true);
    let (server, peer) = pair();
    assert!(ResourceControl::new(peer, &journal).is_err());
    assert!(
        EmptyJobConfirmation::start(server, id.execution, id.session_id, "1".repeat(32), TIMEOUT)
            .is_err()
    );
}

#[test]
fn resource_control_missing_request_timeout_permanently_closes_protocol() {
    let id = identity();
    let db = database(&id);
    let journal = journal(&db, &id, true);
    let mut guard = journal.create_empty_bootstrap_job().unwrap();
    let (server, mut peer) = pair();
    let mut control = ResourceControl::new(server, &journal).unwrap();
    assert!(
        control
            .acknowledge_empty_bootstrap_job_birth(&journal, &mut guard, Duration::from_millis(5))
            .is_err()
    );
    assert!(control.channel.is_none());
    assert!(peer.receive(TIMEOUT).is_err());
    assert_eq!(journal.replay().unwrap().events, 1);
    journal.record_empty_bootstrap_job(&mut guard).unwrap();
    cleanup(&db, &id, &mut guard);
}

#[test]
fn resource_control_missing_commit_or_stopped_authority_cannot_acknowledge() {
    for stopped_authority in [false, true] {
        let id = identity();
        let db = database(&id);
        let live = journal(&db, &id, true);
        let mut guard = live.create_empty_bootstrap_job().unwrap();
        let (server, peer) = pair();
        let mut control = ResourceControl::new(server, &live).unwrap();
        let peer = client(peer, &id, guard.birth_operation_nonce());
        if stopped_authority {
            let stopped =
                ResourceJournal::open(&db, id.clone(), Authority::FixtureStopped, false).unwrap();
            assert!(
                control
                    .acknowledge_empty_bootstrap_job_birth(&stopped, &mut guard, TIMEOUT)
                    .is_err()
            );
        } else {
            let pending = control
                .begin_empty_job_birth(&live, guard.birth_operation_nonce(), TIMEOUT)
                .unwrap();
            // Fault injection at the adapter/transport boundary: no commit has
            // occurred. Exact wire data and an open native resource are not enough.
            assert!(
                control
                    .finish_empty_job_birth(pending, &live, TIMEOUT)
                    .is_err()
            );
        }
        assert!(control.channel.is_none());
        assert!(peer.finish(TIMEOUT).is_err());
        assert_eq!(live.replay().unwrap().events, 1);
        live.record_empty_bootstrap_job(&mut guard).unwrap();
        cleanup(&db, &id, &mut guard);
    }
}

#[test]
fn resource_control_native_policy_drift_blocks_ack_before_committing_birth() {
    use windows::Win32::System::JobObjects::*;
    use windows::Win32::System::SystemServices::{JOB_OBJECT_QUERY, JOB_OBJECT_SET_ATTRIBUTES};
    let id = identity();
    let db = database(&id);
    let journal = journal(&db, &id, true);
    let mut guard = journal.create_empty_bootstrap_job().unwrap();
    let name = crate::util::wstr(&id.name(Resource::BootstrapJob));
    let handle = crate::util::OwnedHandle(unsafe {
        OpenJobObjectW(
            JOB_OBJECT_QUERY | JOB_OBJECT_SET_ATTRIBUTES,
            false,
            crate::util::pcwstr(&name),
        )
        .unwrap()
    });
    let mut original = JOBOBJECT_BASIC_UI_RESTRICTIONS::default();
    unsafe {
        QueryInformationJobObject(
            Some(handle.raw()),
            JobObjectBasicUIRestrictions,
            (&mut original as *mut JOBOBJECT_BASIC_UI_RESTRICTIONS).cast(),
            std::mem::size_of_val(&original) as u32,
            None,
        )
        .unwrap();
    }
    let changed = JOBOBJECT_BASIC_UI_RESTRICTIONS {
        UIRestrictionsClass: JOB_OBJECT_UILIMIT_NONE,
    };
    unsafe {
        SetInformationJobObject(
            handle.raw(),
            JobObjectBasicUIRestrictions,
            (&changed as *const JOBOBJECT_BASIC_UI_RESTRICTIONS).cast(),
            std::mem::size_of_val(&changed) as u32,
        )
        .unwrap();
    }
    let (server, peer) = pair();
    let mut control = ResourceControl::new(server, &journal).unwrap();
    let peer = client(peer, &id, guard.birth_operation_nonce());
    let refused = control.acknowledge_empty_bootstrap_job_birth(&journal, &mut guard, TIMEOUT);
    // Restore this synthetic empty job before asserting, even if refusal failed.
    unsafe {
        SetInformationJobObject(
            handle.raw(),
            JobObjectBasicUIRestrictions,
            (&original as *const JOBOBJECT_BASIC_UI_RESTRICTIONS).cast(),
            std::mem::size_of_val(&original) as u32,
        )
        .unwrap();
    }
    drop(handle);
    assert!(refused.is_err());
    assert!(peer.finish(TIMEOUT).is_err());
    assert!(control.channel.is_none());
    assert_eq!(journal.replay().unwrap().events, 1);
    assert!(
        control
            .acknowledge_empty_bootstrap_job_birth(&journal, &mut guard, TIMEOUT)
            .is_err()
    );
    journal.record_empty_bootstrap_job(&mut guard).unwrap();
    cleanup(&db, &id, &mut guard);
}
