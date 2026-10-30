use super::*;

pub(super) fn selected() -> Selection {
    Selection {
        protocol: Selection::protocol(),
        operation: "0123456789abcdef0123456789abcdef".into(),
        logon: "1123456789abcdef0123456789abcdef".into(),
        ready: "2123456789abcdef0123456789abcdef".into(),
        bootstrap_pid: 7,
        bootstrap_created: 9,
        image_id: [3; 24],
        session: 1,
        account: crate::account_install::synthetic_ready()
            .binding(crate::account_install::AccountMode::Offline)
            .unwrap(),
        credential_generation: "CredOwner-3123456789abcdef0123456789abcdef".into(),
    }
}
fn wiped(length: usize) {
    let audit = crate::account_control::FRAME_SCRUB_AUDIT
        .with(|s| s.take())
        .unwrap();
    assert_eq!(audit, (length, true, true));
}

#[test]
fn credential_wire_password_roundtrip_is_bounded_and_all_owned_copies_are_scrubbed() {
    let selected = selected();
    for password in [
        "Synthetic-only!".to_owned(),
        "A".repeat(127),
        "a🦀é!".into(),
    ] {
        let frame = PasswordFrame::encode(&password, &selected).unwrap();
        let bytes = frame.0.0.clone(); // Synthetic test-only copy; immediately transferred into wiping guard.
        let length = bytes.len();
        frame.validate(&selected).unwrap();
        drop(frame);
        wiped(length);
        let received = PasswordFrame::receive(bytes, &selected).unwrap();
        drop(received);
        wiped(length);
    }
    for invalid in ["".into(), "A".repeat(128), "abc\0def".into()] {
        assert!(PasswordFrame::encode(&invalid, &selected).is_err());
    }
}

#[test]
fn credential_wire_rejects_corrupted_or_cross_operation_secret_and_scrubs_failures() {
    let selected = selected();
    for case in 0..8 {
        let frame = PasswordFrame::encode("Synthetic-only!", &selected).unwrap();
        let mut bytes = frame.0.0.clone();
        match case {
            0 => bytes.truncate(38),
            1 => bytes[0] ^= 1,
            2 => bytes[5] ^= 1,
            3 => bytes[37] = 0,
            4 => {
                bytes[39] = 0;
                bytes[40] = 0;
            }
            5 => {
                bytes[39] = 0;
                bytes[40] = 0xdc;
            }
            6 => bytes.push(0),
            _ => bytes.resize(294, 42),
        }
        drop(frame);
        let length = bytes.len();
        assert!(PasswordFrame::receive(bytes, &selected).is_err());
        wiped(length);
    }
}

#[test]
fn credential_selection_rejects_unknown_fields_missing_identity_and_changed_acknowledgment() {
    let selected = selected();
    Selection::decode(&selected.encode().unwrap()).unwrap();
    let mut value = serde_json::to_value(&selected).unwrap();
    value["password"] = serde_json::json!("never accepted");
    let error = Selection::decode(&serde_json::to_vec(&value).unwrap())
        .err()
        .unwrap()
        .to_string();
    assert_eq!(error, "invalid credential selection");
    assert!(Selection::decode(&vec![b'x'; 4097]).is_err());
    for case in 0..8 {
        let mut wrong = selected.clone();
        match case {
            0 => wrong.protocol.push('x'),
            1 => wrong.logon = wrong.ready.clone(),
            2 => wrong.bootstrap_pid = 0,
            3 => wrong.bootstrap_created = 0,
            4 => wrong.image_id = [0; 24],
            5 => wrong.account.account_name.push('x'),
            6 => wrong.credential_generation.push('x'),
            _ => wrong.operation = wrong.ready.clone(),
        }
        assert!(wrong.validate().is_err());
    }
    for received in [false, true] {
        let ack = selected.acknowledgment(received).unwrap();
        selected.require_ack(&ack, received).unwrap();
        assert!(selected.require_ack(&ack, !received).is_err());
        let mut changed = selected.clone();
        changed.credential_generation = "CredOwner-4123456789abcdef0123456789abcdef".into();
        assert!(changed.require_ack(&ack, received).is_err());
    }
}

#[test]
fn credential_selection_checks_original_native_recipient_before_prepared_ack() {
    let image = PinnedRunnerImage::fixture(&std::env::current_exe().unwrap()).unwrap();
    let mut selection = selected();
    selection.bootstrap_pid = std::process::id();
    selection.bootstrap_created =
        super::super::super::pull::creation_time(unsafe { GetCurrentProcess() }).unwrap();
    selection.image_id = image.file_identity().unwrap();
    selection.account.owner_sid = crate::sid::current_user_sid().unwrap();
    unsafe {
        windows::Win32::System::RemoteDesktop::ProcessIdToSessionId(
            std::process::id(),
            &mut selection.session,
        )
        .unwrap();
    }
    selection
        .require_recipient(&selection.ready, &image)
        .unwrap();
    for case in 0..6 {
        let mut wrong = selection.clone();
        match case {
            0 => wrong.bootstrap_pid += 1,
            1 => wrong.bootstrap_created += 1,
            2 => wrong.session += 1,
            3 => wrong.image_id[0] ^= 1,
            4 => wrong.account.owner_sid.push('1'),
            _ => wrong.ready = "5123456789abcdef0123456789abcdef".into(),
        }
        assert!(
            wrong.require_recipient(&selection.ready, &image).is_err(),
            "case {case}"
        );
    }
}
