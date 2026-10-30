//! Local tests are data/native-reference refusal only; no account/profile effects.
use super::*;
use crate::account_control::bootstrap::entry::credential_wire::{
    Selection,
    child::{BirthRequest, FixtureWriterBinding},
};
use crate::account_install::AccountMode;

fn request() -> Request {
    let nonce = |n: u32| format!("{n:032x}");
    let setup = crate::account_install::synthetic_ready();
    let selection = Selection {
        protocol: Selection::protocol(),
        operation: nonce(1),
        logon: nonce(2),
        ready: nonce(3),
        bootstrap_pid: 600,
        bootstrap_created: 200,
        image_id: [2; 24],
        session: 1,
        account: setup.binding(AccountMode::Offline).unwrap(),
        credential_generation: setup.offline.unwrap().credential_generation.unwrap(),
    };
    let container = format!("Alysis.Execution.{}", nonce(10));
    let mut birth = BirthRequest::new(selection, nonce(4)).unwrap();
    birth.purpose = Purpose::FixtureProfileWriterV1 {
        binding: FixtureWriterBinding {
            package_sid: crate::appcontainer::derive(&container)
                .unwrap()
                .string()
                .unwrap(),
            container,
            folder: [4; 24],
            owner_birth: nonce(5),
            owner_resume: nonce(6),
            owner_pid: 501,
            owner_created: 101,
            use_nonce: nonce(7),
            unsupported_registry: false,
            hold_rounds: 12,
            descendant_files: true,
        },
    };
    let value = Request {
        birth,
        nonce: nonce(8),
        pid: 601,
        created: 201,
    };
    value.validate().unwrap();
    value
}
fn file(depth: u32) -> FixedProfileFileReceipt {
    FixedProfileFileReceipt {
        depth,
        file_id: [(depth + 10) as u8; 24],
        bytes: 80,
    }
}
fn receipt() -> Receipt {
    Receipt {
        request: request(),
        depth: 1,
        file: file(1),
        grandchild: Some(((123, 603, 203), file(2))),
    }
}

#[test]
#[ignore = "authenticated fixed descendant subprocess only; hosted account fixture"]
fn fixed_profile_writer_descendant_entry() {
    let mut phase = 0;
    if descendant(&mut phase).is_err() {
        std::process::exit((0x4300_0000u32 | phase) as i32);
    }
    panic!("fixed descendant unexpectedly returned");
}
#[test]
fn descendant_writer_requires_prebound_fixed_purpose_and_depth() {
    let request = request();
    for depth in [1, 2] {
        require_request(&request, depth).unwrap();
    }
    for depth in [0, 3, u32::MAX] {
        assert!(require_request(&request, depth).is_err());
    }
    for case in 0..4 {
        let mut wrong = request.clone();
        let Purpose::FixtureProfileWriterV1 { binding } = &mut wrong.birth.purpose else {
            unreachable!()
        };
        match case {
            0 => binding.descendant_files = false,
            1 => binding.hold_rounds = 0,
            2 => binding.unsupported_registry = true,
            _ => binding.folder = [0; 24],
        }
        assert!(require_request(&wrong, 1).is_err());
    }
}
#[test]
fn descendant_writer_requires_original_root_and_parent_chain() {
    let request = request();
    let root = (1, request.pid, request.created);
    let child = (2, 602, 202);
    require_chain(&request, 1, root, root).unwrap();
    require_chain(&request, 2, child, root).unwrap();
    assert!(require_chain(&request, 1, child, root).is_err());
    assert!(require_chain(&request, 2, root, root).is_err());
    assert!(require_chain(&request, 2, (2, 602, 200), root).is_err());
    for bad in [
        (1, request.pid + 1, request.created),
        (1, request.pid, request.created + 1),
    ] {
        assert!(require_chain(&request, 1, bad, bad).is_err());
        assert!(require_chain(&request, 2, child, bad).is_err());
    }
}
#[test]
fn descendant_writer_handoff_bounds_and_unknown_fields_fail_closed() {
    let handoff = Handoff {
        request: request(),
        depth: 1,
        parent: 1,
        consumer: 2,
        address: ControlAddress::fresh().unwrap(),
    };
    let valid = serde_json::to_vec(&handoff).unwrap();
    Handoff::read(&valid).unwrap();
    for bad in [vec![b'x'; LIMIT + 1], b"{}".to_vec(), b"null".to_vec()] {
        assert!(Handoff::read(&bad).is_err());
    }
    for (key, value) in [
        ("parent", 0u64),
        ("consumer", 1),
        ("consumer", u64::MAX),
        ("depth", 3),
    ] {
        let mut bad = serde_json::to_value(&handoff).unwrap();
        bad[key] = value.into();
        assert!(Handoff::read(&serde_json::to_vec(&bad).unwrap()).is_err());
    }
    let mut bad = serde_json::to_value(&handoff).unwrap();
    bad["command"] = "write".into();
    assert!(Handoff::read(&serde_json::to_vec(&bad).unwrap()).is_err());
}
#[test]
fn descendant_writer_receipt_binds_request_depth_and_distinct_files() {
    let request = request();
    let valid = receipt().wire().unwrap();
    Receipt::read(&valid, &request, 1).unwrap();
    let mut other = request.clone();
    other.nonce = format!("{:032x}", 99);
    assert!(Receipt::read(&valid, &other, 1).is_err());
    assert!(Receipt::read(&valid, &request, 2).is_err());
    for case in 0..6 {
        let mut bad = receipt();
        match case {
            0 => bad.file.depth = 2,
            1 => bad.file.file_id = [4; 24],
            2 => bad.file.bytes = 129,
            3 => bad.grandchild.as_mut().unwrap().1.file_id = bad.file.file_id,
            4 => bad.grandchild = None,
            _ => bad.grandchild.as_mut().unwrap().0.1 = request.pid,
        }
        assert!(bad.wire().is_err());
    }
    let mut bad = serde_json::to_value(receipt()).unwrap();
    bad["cleanup"] = true.into();
    assert!(Receipt::read(&serde_json::to_vec(&bad).unwrap(), &request, 1).is_err());
}
#[test]
fn descendant_writer_effect_frames_are_exact_and_phase_bounded() {
    let request = request();
    assert_ne!(
        frame(&request, 1, "write").unwrap(),
        frame(&request, 2, "write").unwrap()
    );
    assert_ne!(
        frame(&request, 1, "write").unwrap(),
        frame(&request, 1, "hold").unwrap()
    );
    for phase in ["", "resume", "cleanup", "write\0"] {
        assert!(frame(&request, 1, phase).is_err());
    }
}
#[test]
fn descendant_writer_rejects_noninherited_reference_before_any_effect() {
    let original = duplicate(
        unsafe { GetCurrentProcess() },
        unsafe { GetCurrentProcess() },
        false,
    )
    .unwrap();
    let error = inherited(original.raw().0 as usize as u64).err().unwrap();
    assert!(error.to_string().contains("was not inherited"));
    describe(original.raw()).unwrap();
    assert!(inherited(0).is_err());
    assert!(inherited(u64::MAX).is_err());
}
#[test]
fn descendant_writer_root_offer_requires_all_three_distinct_file_ids() {
    let request = request();
    let valid = Offer {
        request: request.clone(),
        references: [(1, 602, 202), (2, 603, 203)],
        file: file(0),
        descendant_files: vec![file(1), file(2)],
        registry_written: true,
    };
    valid.validate(&request).unwrap();
    for case in 0..5 {
        let mut bad = valid.clone();
        match case {
            0 => bad.descendant_files.clear(),
            1 => bad.descendant_files.swap(0, 1),
            2 => bad.descendant_files[1].file_id = bad.descendant_files[0].file_id,
            3 => bad.descendant_files[0].file_id = bad.file.file_id,
            _ => bad.descendant_files[1].file_id = [4; 24],
        }
        assert!(bad.validate(&request).is_err());
    }
}
