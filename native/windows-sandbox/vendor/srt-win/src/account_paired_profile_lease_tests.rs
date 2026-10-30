//! Parser/state tests only: no profiles, accounts, processes or native effects.
use super::super::{AccountMode, BirthRequest, Purpose, Selection, probe};
use super::*;

fn nonce(value: u32) -> String {
    format!("{value:032x}")
}
fn using() -> Message {
    let setup = crate::account_install::synthetic_ready();
    let selected = Selection {
        protocol: Selection::protocol(),
        operation: nonce(1),
        logon: nonce(2),
        ready: nonce(3),
        bootstrap_pid: 500,
        bootstrap_created: 100,
        image_id: [2; 24],
        session: 1,
        account: setup.binding(AccountMode::Offline).unwrap(),
        credential_generation: setup.offline.unwrap().credential_generation.unwrap(),
    };
    let container = format!("Alysis.Execution.{}", nonce(10));
    let mut birth = BirthRequest::new(selected, nonce(4)).unwrap();
    birth.purpose = Purpose::TrustedPairedProfileV1 {
        package_sid: crate::appcontainer::derive(&container)
            .unwrap()
            .string()
            .unwrap(),
        container,
        policy: 1,
    };
    let request = probe::Request {
        birth: birth.clone(),
        nonce: nonce(5),
        pid: 501,
        created: 101,
    };
    let mut consumer = birth;
    consumer.purpose = Purpose::TrustedSelfCheckV1;
    consumer.nonce = nonce(6);
    consumer.selection.logon = nonce(7);
    consumer.selection.ready = nonce(8);
    consumer.selection.bootstrap_pid = 600;
    consumer.selection.bootstrap_created = 200;
    Message {
        request,
        phase: Phase::Using,
        folder: Some([4; 24]),
        use_nonce: Some(nonce(9)),
        consumer: Some(consumer),
        retirement: None,
    }
}
fn json_frame(value: serde_json::Value) -> Vec<u8> {
    let mut bytes = PREFIX.to_vec();
    bytes.extend(serde_json::to_vec(&value).unwrap());
    bytes
}
fn refused(lease: &mut UseLease, request: &[u8]) {
    assert!(lease.require_current().is_err());
    assert!(lease.accept(request).is_err());
    assert!(lease.renewal().is_err());
}

#[test]
fn paired_profile_lease_requires_fully_valid_original_using() {
    let using = using();
    UseLease::new(&using).unwrap().require_current().unwrap();
    for phase in [
        Phase::Create,
        Phase::Created,
        Phase::Use,
        Phase::Capture,
        Phase::Captured,
        Phase::Remove,
        Phase::Removed,
    ] {
        assert!(UseLease::new(&using.at(phase)).is_err());
    }
    for fault in 0..5 {
        let mut wrong = using.clone();
        match fault {
            0 => wrong.folder = Some([0; 24]),
            1 => wrong.use_nonce = Some(wrong.request.nonce.clone()),
            2 => wrong.consumer.as_mut().unwrap().selection.image_id[0] ^= 1,
            3 => wrong.consumer.as_mut().unwrap().purpose = Purpose::ObserveOnly,
            4 => wrong.request.birth.purpose = Purpose::TrustedSelfCheckV1,
            _ => unreachable!(),
        }
        assert!(UseLease::new(&wrong).is_err());
    }
}

#[test]
fn paired_profile_lease_binds_all_four_original_nonces_and_folder() {
    let using = using();
    let request = UseLease::new(&using).unwrap().renewal().unwrap();
    for field in 0..5 {
        let mut frame = Frame::read(&request).unwrap();
        match field {
            0 => frame.subject.owner_birth = frame.subject.use_nonce.clone(),
            1 => frame.subject.owner_resume = frame.subject.owner_birth.clone(),
            2 => frame.subject.consumer_birth = frame.subject.owner_resume.clone(),
            3 => frame.subject.use_nonce = frame.subject.consumer_birth.clone(),
            4 => frame.subject.folder[0] ^= 1,
            _ => unreachable!(),
        }
        let mut receiver = UseLease::new(&using).unwrap();
        assert!(receiver.accept(&frame.wire().unwrap()).is_err());
        refused(&mut receiver, &request);
    }
}

#[test]
fn paired_profile_lease_replay_and_skipped_sequence_are_permanent_refusals() {
    let using = using();
    let mut sender = UseLease::new(&using).unwrap();
    let mut receiver = UseLease::new(&using).unwrap();
    let first = sender.renewal().unwrap();
    let acknowledgment = receiver.accept(&first).unwrap();
    sender.confirm(&acknowledgment).unwrap();
    let second = sender.renewal().unwrap();
    assert_eq!(Frame::read(&second).unwrap().sequence, 1);
    assert!(receiver.accept(&first).is_err());
    refused(&mut receiver, &second);
    let mut skipped = UseLease::new(&using).unwrap();
    assert!(skipped.accept(&second).is_err());
    refused(&mut skipped, &first);
}

#[test]
fn paired_profile_lease_wrong_direction_and_ack_replay_poison_state() {
    let using = using();
    let mut sender = UseLease::new(&using).unwrap();
    let mut receiver = UseLease::new(&using).unwrap();
    let request = sender.renewal().unwrap();
    let acknowledgment = receiver.accept(&request).unwrap();
    assert!(is_renewal(&request) && is_renewal(&acknowledgment));
    assert!(
        UseLease::new(&using)
            .unwrap()
            .accept(&acknowledgment)
            .is_err()
    );
    assert!(sender.confirm(&request).is_err());
    assert!(sender.confirm(&acknowledgment).is_err());
    assert!(receiver.renewal().is_err());
    refused(&mut receiver, &request);

    let mut sender = UseLease::new(&using).unwrap();
    let mut receiver = UseLease::new(&using).unwrap();
    let request = sender.renewal().unwrap();
    let acknowledgment = receiver.accept(&request).unwrap();
    sender.confirm(&acknowledgment).unwrap();
    assert!(sender.confirm(&acknowledgment).is_err());
    refused(&mut sender, &request);

    let mut sender = UseLease::new(&using).unwrap();
    let mut receiver = UseLease::new(&using).unwrap();
    let request = sender.renewal().unwrap();
    let acknowledgment = receiver.accept(&request).unwrap();
    sender.confirm(&acknowledgment).unwrap();
    assert!(sender.accept(&request).is_err());
    refused(&mut sender, &request);
}

#[test]
fn paired_profile_lease_pending_ack_blocks_capture_and_second_renewal() {
    let using = using();
    for capture in [false, true] {
        let mut sender = UseLease::new(&using).unwrap();
        let mut receiver = UseLease::new(&using).unwrap();
        let request = sender.renewal().unwrap();
        let acknowledgment = receiver.accept(&request).unwrap();
        if capture {
            assert!(sender.require_current().is_err());
        } else {
            assert!(sender.renewal().is_err());
        }
        assert!(sender.confirm(&acknowledgment).is_err());
        refused(&mut sender, &request);
    }
}

#[test]
fn paired_profile_lease_malformed_unknown_and_oversized_requests_poison_state() {
    let using = using();
    let request = UseLease::new(&using).unwrap().renewal().unwrap();
    let value = serde_json::to_value(Frame::read(&request).unwrap()).unwrap();
    let mut extra = value.clone();
    extra["handle"] = serde_json::json!(42);
    let mut nested = value.clone();
    nested["subject"]["container"] = serde_json::json!("not-authority");
    let mut unknown = value;
    unknown["phase"] = serde_json::json!("capture");
    let mut duplicate = request.clone();
    duplicate.pop();
    duplicate.extend_from_slice(b",\"sequence\":0}");
    let mut oversized = request.clone();
    oversized.resize(MAX_FRAME + 1, b' ');
    let malformed = [
        Vec::new(),
        b"other-prefix:{}".to_vec(),
        PREFIX.to_vec(),
        json_frame(extra),
        json_frame(nested),
        json_frame(unknown),
        duplicate,
        oversized,
    ];
    for bytes in malformed {
        let mut receiver = UseLease::new(&using).unwrap();
        assert!(receiver.accept(&bytes).is_err());
        refused(&mut receiver, &request);
    }
    let mut at_bound = request;
    at_bound.resize(MAX_FRAME, b' ');
    UseLease::new(&using).unwrap().accept(&at_bound).unwrap();
}

#[test]
fn paired_profile_lease_confirmation_requires_exact_bound_acknowledgment() {
    let using = using();
    for field in 0..7 {
        let mut sender = UseLease::new(&using).unwrap();
        let mut receiver = UseLease::new(&using).unwrap();
        let request = sender.renewal().unwrap();
        let acknowledgment = receiver.accept(&request).unwrap();
        let mut wrong = serde_json::to_value(Frame::read(&acknowledgment).unwrap()).unwrap();
        match field {
            0 => wrong["sequence"] = serde_json::json!(1),
            1 => wrong["subject"]["owner_birth"] = serde_json::json!(nonce(100)),
            2 => wrong["subject"]["owner_resume"] = serde_json::json!(nonce(100)),
            3 => wrong["subject"]["consumer_birth"] = serde_json::json!(nonce(100)),
            4 => wrong["subject"]["use_nonce"] = serde_json::json!(nonce(100)),
            5 => wrong["subject"]["folder"] = serde_json::to_value([5u8; 24]).unwrap(),
            6 => wrong["extra"] = serde_json::json!(true),
            _ => unreachable!(),
        }
        assert!(sender.confirm(&json_frame(wrong)).is_err());
        assert!(sender.confirm(&acknowledgment).is_err());
        refused(&mut sender, &request);
    }
}

#[test]
fn paired_profile_lease_allows_capture_after_96_acks_but_refuses_pulse_97() {
    let using = using();
    let mut sender = UseLease::new(&using).unwrap();
    let mut receiver = UseLease::new(&using).unwrap();
    sender.require_current().unwrap();
    receiver.require_current().unwrap();
    let mut last = None;
    for sequence in 0..MAX_PULSES {
        let request = sender.renewal().unwrap();
        assert!(request.len() <= MAX_FRAME);
        assert_eq!(Frame::read(&request).unwrap().sequence, sequence);
        let acknowledgment = receiver.accept(&request).unwrap();
        assert!(acknowledgment.len() <= MAX_FRAME);
        sender.confirm(&acknowledgment).unwrap();
        sender.require_current().unwrap();
        receiver.require_current().unwrap();
        last = Some(request);
    }
    let mut over = Frame::read(last.as_ref().unwrap()).unwrap();
    over.sequence = MAX_PULSES;
    assert!(sender.renewal().is_err());
    assert!(receiver.accept(&over.wire().unwrap()).is_err());
    refused(&mut sender, last.as_ref().unwrap());
    refused(&mut receiver, last.as_ref().unwrap());
}

#[test]
fn paired_profile_lease_renewals_never_refresh_the_original_deadline() {
    let using = using();
    let mut sender = UseLease::new(&using).unwrap();
    let mut receiver = UseLease::new(&using).unwrap();
    let started = sender.started;
    sender.now = Some(started + LIFETIME - Duration::from_millis(1));
    receiver.now = Some(receiver.started + LIFETIME - Duration::from_millis(1));
    let request = sender.renewal().unwrap();
    let acknowledgment = receiver.accept(&request).unwrap();
    sender.confirm(&acknowledgment).unwrap();
    assert_eq!(sender.started, started);
    sender.require_current().unwrap();
    sender.now = Some(started + LIFETIME);
    assert!(sender.require_current().is_err());
    sender.now = Some(started);
    refused(&mut sender, &request);
}

#[test]
fn paired_profile_lease_checks_expiry_on_request_accept_and_confirmation() {
    let using = using();
    let mut sender = UseLease::new(&using).unwrap();
    let request = sender.renewal().unwrap();
    let mut receiver = UseLease::new(&using).unwrap();
    let acknowledgment = receiver.accept(&request).unwrap();
    sender.now = Some(sender.started + LIFETIME);
    assert!(sender.confirm(&acknowledgment).is_err());
    sender.now = Some(sender.started);
    assert!(sender.confirm(&acknowledgment).is_err());

    let mut receiver = UseLease::new(&using).unwrap();
    receiver.now = Some(receiver.started + LIFETIME);
    assert!(receiver.accept(&request).is_err());
    receiver.now = Some(receiver.started);
    refused(&mut receiver, &request);

    let mut expired = UseLease::new(&using).unwrap();
    expired.now = Some(expired.started + LIFETIME);
    assert!(expired.renewal().is_err());
    expired.now = Some(expired.started);
    refused(&mut expired, &request);
}

#[test]
fn paired_profile_lease_clock_reversal_refuses_permanently() {
    let mut lease = UseLease::new(&using()).unwrap();
    lease.now = Some(lease.started - Duration::from_millis(1));
    assert!(lease.require_current().is_err());
    lease.now = Some(lease.started);
    assert!(lease.renewal().is_err());
}

#[test]
fn paired_profile_lease_relay_ack_matches_owner_without_changing_using() {
    let using = using();
    let original = using.wire().unwrap();
    let mut broker = UseLease::new(&using).unwrap();
    let mut relay = UseLease::new(&using).unwrap();
    let mut owner = UseLease::new(&using).unwrap();
    for _ in 0..3 {
        let bytes = broker.renewal().unwrap();
        let expected = relay.accept(&bytes).unwrap();
        let acknowledgment = owner.accept(&bytes).unwrap();
        assert_eq!(expected, acknowledgment);
        broker.confirm(&acknowledgment).unwrap();
    }
    for lease in [&broker, &relay, &owner] {
        lease.require_current().unwrap();
    }
    assert_eq!(using.wire().unwrap(), original);
    let capture = Message {
        retirement: Some(nonce(11)),
        ..using.at(Phase::Capture)
    };
    capture.follows(&using).unwrap();
    assert!(!is_renewal(&capture.wire().unwrap()));
}
