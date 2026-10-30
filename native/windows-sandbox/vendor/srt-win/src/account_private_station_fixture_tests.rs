//! Parser and immutable-binding tests only: no SAM, logon, station or SDK effects.
use super::*;
use bootstrap::entry::credential_wire::{Selection, child::BirthRequest};

fn request() -> Request {
    Request {
        birth: BirthRequest {
            protocol: "alysis-suspended-account-birth-v2".into(),
            nonce: "6123456789abcdef0123456789abcdef".into(),
            purpose: Purpose::FixturePrivateStationV1,
            selection: Selection {
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
            },
        },
        nonce: "7123456789abcdef0123456789abcdef".into(),
        pid: 8,
        created: 10,
    }
}
const STAMP: TokenStamp = ([1, 0], [2, 3], [4, 0]);

#[test]
fn private_station_sddl_capacity_is_not_string_length() {
    let prefix: Vec<_> = "O:SYD:P(A;;0xf037f;;;SY)".encode_utf16().chain([0]).collect();
    let mut reads = 0;
    let decoded = bounded_sddl(8192, |index| {
        // Indexing deliberately fails if the parser reads trailing capacity.
        reads += 1;
        prefix[index]
    }).unwrap();
    assert_eq!(decoded, "O:SYD:P(A;;0xf037f;;;SY)");
    assert_eq!(reads, prefix.len());
    for capacity in [0, 8193] {
        assert!(bounded_sddl(capacity, |_| panic!("invalid capacity read")).is_err());
    }
    assert!(bounded_sddl(4, |_| b'x' as u16).is_err());
    assert!(bounded_sddl(1, |_| 0).is_err());
    assert!(bounded_sddl(2, |index| [0xd800, 0][index]).is_err());
}

#[test]
fn private_station_sddl_native_conversion_preserves_complete_policy() {
    // In-memory descriptor conversion only: no USER object or account effects.
    let policy = "O:S-1-5-21-1-2-3-1004D:P(A;;0xf037f;;;SY)(A;;0xf037f;;;S-1-5-21-1-2-3-1004)S:(ML;;NW;;;LW)";
    let sd = OwnedSd::from_sddl(policy).unwrap();
    let info = OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION | LABEL_SECURITY_INFORMATION;
    assert_eq!(render(sd.ptr, info).unwrap(), policy);
}

#[test]
fn private_station_receipt_binds_full_request_and_original_authentication_id() {
    let expected = request();
    let receipt = Receipt::new(&expected, STAMP).unwrap();
    let bytes = receipt.encode().unwrap();
    assert!(Receipt::decode(&bytes, &expected, STAMP).is_ok());
    for index in 0..9 {
        let mut changed = expected.clone();
        match index {
            0 => changed.pid += 1,
            1 => changed.created += 1,
            2 => changed.nonce.replace_range(0..1, "8"),
            3 => changed.birth.nonce.replace_range(0..1, "9"),
            4 => changed.birth.selection.image_id[0] ^= 1,
            5 => changed.birth.selection.session += 1,
            6 => changed.birth.selection.logon.replace_range(0..1, "a"),
            7 => changed.birth.selection.bootstrap_created += 1,
            _ => changed.birth.selection.account.account_sid.push('1'),
        }
        assert!(Receipt::decode(&bytes, &changed, STAMP).is_err());
    }
    for changed in [
        ([9, 0], [2, 3], [4, 0]),
        ([1, 0], [2, 4], [4, 0]),
        ([1, 0], [2, 3], [5, 0]),
    ] {
        assert!(Receipt::decode(&bytes, &expected, changed).is_err());
    }
}

#[test]
fn private_station_has_exclusive_test_purpose_and_no_paired_admission() {
    let mut value = request();
    assert!(value.birth.purpose.is_self_check());
    assert!(value.birth.purpose.uses_handoff());
    assert!(!value.birth.purpose.is_paired_consumer());
    for purpose in [
        Purpose::ObserveOnly,
        Purpose::TrustedSelfCheckV1,
        Purpose::TrustedHandoffSelfCheckV1,
        Purpose::TrustedUnusedProfileV1 {
            container: "unused".into(),
            package_sid: "S-1-15-2-1".into(),
            policy: 1,
        },
        Purpose::TrustedPairedProfileV1 {
            container: "unused".into(),
            package_sid: "S-1-15-2-1".into(),
            policy: 1,
        },
    ] {
        value.birth.purpose = purpose;
        assert!(Receipt::new(&value, STAMP).is_err());
        assert!(acknowledgment(&value).is_err());
    }
}

#[test]
fn private_station_rejects_malformed_unknown_oversize_and_wrong_direction_frames() {
    let request = request();
    let receipt = Receipt::new(&request, STAMP).unwrap();
    assert!(Receipt::decode(&acknowledgment(&request).unwrap(), &request, STAMP).is_err());
    let mut unknown = serde_json::to_value(&receipt).unwrap();
    unknown["cleanup_authority"] = serde_json::json!(true);
    let mut frame = PREFIX.to_vec();
    frame.extend_from_slice(&serde_json::to_vec(&unknown).unwrap());
    assert!(Receipt::decode(&frame, &request, STAMP).is_err());
    assert!(Receipt::decode(&vec![b' '; LIMIT + 1], &request, STAMP).is_err());
    let mut frame = receipt.encode().unwrap();
    frame.pop();
    assert!(Receipt::decode(&frame, &request, STAMP).is_err());
    let mut oversized = receipt;
    oversized.original_station = Some("x".repeat(1025));
    assert!(oversized.encode().is_err());
}

#[test]
fn private_station_initial_collision_is_failure_and_success_requires_complete_close() {
    let request = request();
    let mut receipt = Receipt::new(&request, STAMP).unwrap();
    receipt.stage = Stage::Create;
    receipt.hresult = Some(windows::core::HRESULT::from_win32(ERROR_ALREADY_EXISTS.0).0);
    assert!(receipt.encode().is_ok()); // Preserved refusal, never successful adoption.
    receipt.passed = true;
    assert!(receipt.encode().is_err());
    receipt.stage = Stage::Complete;
    receipt.hresult = None;
    receipt.original_station = Some("WinSta0".into());
    receipt.private_station = Some("Service-0x0-123$".into());
    receipt.descriptor = Some("fixed descriptor".into());
    receipt.expected_descriptor = receipt.descriptor.clone();
    receipt.collision_error = Some(183);
    receipt.noninheritable = true;
    receipt.nonvisible = true;
    receipt.original_associations_unchanged = true;
    receipt.shared_descriptor_unchanged = true;
    assert!(receipt.encode().is_err());
    receipt.closed = true;
    assert!(receipt.encode().is_ok());
    receipt.collision_error = Some(5);
    assert!(receipt.encode().is_err());
}
