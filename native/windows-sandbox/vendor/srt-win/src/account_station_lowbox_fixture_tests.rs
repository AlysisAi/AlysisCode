//! Parser and evidence refusal only. No account, profile, process or USER effect.
use super::*;
#[test]
fn station_family_birth_requires_two_ordered_distinct_originals_and_v3() {
    let mut request = FixtureStationLowboxBinding::synthetic_request();
    let root = (700, request.created + 10);
    let first = (21, 701, root.1 + 1);
    assert!(require_family_birth(&request, root, &[], 1, first).is_err());
    let Purpose::FixtureStationLowboxV1 { binding } = &mut request.birth.purpose else {
        unreachable!()
    };
    binding.policy = 6;
    binding.completion =
        bootstrap::entry::credential_wire::child::LowboxCompletion::V3ShadowBoundedV1;
    require_family_birth(&request, root, &[], 1, first).unwrap();
    let previous = [(first.1, first.2)];
    let second = (22, 702, first.2 + 1);
    require_family_birth(&request, root, &previous, 2, second).unwrap();
    for depth in [0, 2, 3] {
        assert!(require_family_birth(&request, root, &[], depth, first).is_err());
    }
    for wrong in [
        (22, root.0, second.2),
        (22, first.1, second.2),
        (22, second.1, root.1),
        (0, second.1, second.2),
    ] {
        assert!(require_family_birth(&request, root, &previous, 2, wrong).is_err());
    }
    assert!(
        require_family_birth(
            &request,
            root,
            &[(first.1, first.2), (second.1, second.2)],
            3,
            (23, 703, second.2 + 1)
        )
        .is_err()
    );
    assert_ne!(
        family_ack(&request, 1).unwrap(),
        family_ack(&request, 2).unwrap()
    );
    assert!(family_ack(&request, 0).is_err());
    assert!(family_remaining(Instant::now() - Duration::from_millis(1)).is_err());
}
fn checkpoint_fixture() -> (Request, Stamp, Receipt) {
    let request = FixtureStationLowboxBinding::synthetic_request();
    let stamp = ([1, 0], [2, 0], [3, 0]);
    let mut receipt = Receipt::new(&request, stamp).unwrap();
    receipt.stage = "create_child".into();
    (request, stamp, receipt)
}
fn constructor_fixture() -> (Request, Stamp, Receipt) {
    let (mut request, stamp, _) = checkpoint_fixture();
    let Purpose::FixtureStationLowboxV1 { binding } = &mut request.birth.purpose else {
        unreachable!()
    };
    binding.policy = 2;
    let mut receipt = Receipt::new(&request, stamp).unwrap();
    receipt.stage = "kernelbase_token".into();
    (request, stamp, receipt)
}
#[test]
fn station_nsi_inheritance_has_distinct_flag_and_cannot_qualify_with_omitted_flags() {
    let (mut request, stamp, mut receipt) = checkpoint_fixture();
    receipt.rpc_nsi_system_inheritance = true;
    assert!(receipt.require(&request, stamp).is_err());
    let Purpose::FixtureStationLowboxV1 { binding } = &mut request.birth.purpose else {
        unreachable!()
    };
    binding.policy = 5;
    let mut receipt = Receipt::new(&request, stamp).unwrap();
    receipt.stage = "create_child".into();
    receipt.require(&request, stamp).unwrap();
    let mut wire = serde_json::to_value(&receipt).unwrap();
    wire.as_object_mut()
        .unwrap()
        .remove("rpc_nsi_system_inheritance");
    wire["passed"] = true.into();
    assert!(
        serde_json::from_value::<Receipt>(wire)
            .unwrap()
            .require(&request, stamp)
            .unwrap_err()
            .to_string()
            .contains("NSI inheritance comparison policy")
    );
    receipt.original_child = Some((7, 9));
    assert!(
        receipt
            .require(&request, stamp)
            .unwrap_err()
            .to_string()
            .contains("original comparison identity")
    );
    receipt.rpc_nsi_system_inheritance = true;
    receipt.require(&request, stamp).unwrap();
    for field in [
        "rpc_system_inheritance",
        "kernelbase_token_birth",
        "security_capabilities_birth",
        "captured_namespace_birth",
    ] {
        let mut wire = serde_json::to_value(&receipt).unwrap();
        wire[field] = true.into();
        assert!(
            serde_json::from_value::<Receipt>(wire)
                .unwrap()
                .require(&request, stamp)
                .is_err()
        );
    }
}
#[test]
fn station_system_inheritance_requires_immutable_identity_and_never_qualifies() {
    let (mut request, stamp, mut receipt) = checkpoint_fixture();
    receipt.rpc_system_inheritance = true;
    assert!(receipt.require(&request, stamp).is_err());
    let Purpose::FixtureStationLowboxV1 { binding } = &mut request.birth.purpose else {
        unreachable!()
    };
    binding.policy = 3;
    let mut receipt = Receipt::new(&request, stamp).unwrap();
    receipt.stage = "create_child".into();
    receipt.require(&request, stamp).unwrap();
    // No optional diagnostic field can turn immutable policy3 into qualification.
    let mut wire = serde_json::to_value(&receipt).unwrap();
    for field in [
        "rpc_system_inheritance",
        "kernelbase_token_birth",
        "security_capabilities_birth",
        "captured_namespace_birth",
    ] {
        wire.as_object_mut().unwrap().remove(field);
    }
    wire["passed"] = true.into();
    let omitted: Receipt = serde_json::from_value(wire.clone()).unwrap();
    assert!(
        omitted
            .require(&request, stamp)
            .unwrap_err()
            .to_string()
            .contains("SYSTEM inheritance comparison policy")
    );
    wire["passed"] = false.into();
    for field in ["security_capabilities_birth", "captured_namespace_birth"] {
        let mut mixed = wire.clone();
        mixed[field] = true.into();
        assert!(
            serde_json::from_value::<Receipt>(mixed)
                .unwrap()
                .require(&request, stamp)
                .unwrap_err()
                .to_string()
                .contains("exclusive immutable policy")
        );
    }
    receipt.original_child = Some((7, 9));
    assert!(
        receipt
            .require(&request, stamp)
            .unwrap_err()
            .to_string()
            .contains("original comparison identity")
    );
    receipt.rpc_system_inheritance = true;
    receipt.require(&request, stamp).unwrap();
    for field in [
        "kernelbase_token_birth",
        "security_capabilities_birth",
        "captured_namespace_birth",
    ] {
        let mut wire = serde_json::to_value(&receipt).unwrap();
        wire[field] = true.into();
        assert!(
            serde_json::from_value::<Receipt>(wire)
                .unwrap()
                .require(&request, stamp)
                .is_err()
        );
    }
}
#[test]
fn station_constructor_requires_distinct_policy_and_unchanged_private_objects_before_birth() {
    let (request, stamp, mut receipt) = checkpoint_fixture();
    receipt.kernelbase_token_birth = true;
    assert!(receipt.require(&request, stamp).is_err());
    let (request, stamp, mut receipt) = constructor_fixture();
    receipt.passed = true;
    assert!(
        receipt
            .require(&request, stamp)
            .unwrap_err()
            .to_string()
            .contains("KernelBase comparison policy")
    );
    receipt.passed = false;
    receipt.original_child = Some((7, 9));
    assert!(
        receipt
            .require(&request, stamp)
            .unwrap_err()
            .to_string()
            .contains("unchanged original RPC/root")
    );
    receipt.original_child = None;
    receipt.kernelbase_token_birth = true;
    receipt.constructor_rpc_unchanged_before_birth = true;
    assert!(receipt.require(&request, stamp).is_err());
    receipt.kernelbase_constructor_returned = true;
    receipt.require(&request, stamp).unwrap();
    receipt.original_child = Some((7, 9));
    receipt.constructor_rpc_unchanged_before_birth = false;
    assert!(
        receipt
            .require(&request, stamp)
            .unwrap_err()
            .to_string()
            .contains("unchanged original RPC/root")
    );
}
#[test]
fn station_lowbox_schannel_control_cannot_precede_exit_or_replace_lowbox_failure() {
    let (request, stamp, mut receipt) = checkpoint_fixture();
    let success = serde_json::json!({
        "passed": true, "flags": 0x420010, "acquire_status": 0,
        "release_status": 0, "https_qualified": false, "network_io_attempted": false,
        "package_query_status": 0, "package_buffer_returned": true, "package_buffer_free_status": 0,
        "dependencies": {"diagnostic_only": true, "schannel_module_status": 0,
            "lsa_connect_ntstatus": 0, "lsa_handle_returned": true, "lsa_close_ntstatus": 0,
            "lsa_rpc_error": null,
            "lsa_ready_event": {"open_ntstatus": 0, "handle_returned": true,
                "zero_wait_result": 0, "close_hresult": 0}}
    });
    receipt.raw_schannel_control = Some(serde_json::from_value(success.clone()).unwrap());
    assert!(receipt.require(&request, stamp).is_err());
    receipt.original_child_exit = Some(EXIT + 1);
    receipt.desktop_closed = true;
    receipt.station_closed = true;
    receipt.original_associations_unchanged = true;
    receipt.shared_descriptor_unchanged = true;
    receipt.stage = "child_checks".into();
    let mut failed = success;
    failed["passed"] = false.into();
    failed["acquire_status"] = (0x80090305u32 as i32).into();
    failed["release_status"] = serde_json::Value::Null;
    receipt.schannel_child = Some(serde_json::from_value(failed).unwrap());
    receipt.require(&request, stamp).unwrap();
    let decoded: Receipt = decode(&encode(&receipt).unwrap()).unwrap();
    decoded.require(&request, stamp).unwrap();
    assert!(!decoded.schannel_child.unwrap().passed);
    assert!(decoded.raw_schannel_control.unwrap().passed);
    for field in [
        "original_child_exit",
        "desktop_closed",
        "station_closed",
        "original_associations_unchanged",
        "shared_descriptor_unchanged",
    ] {
        let mut changed = serde_json::to_value(&receipt).unwrap();
        changed[field] = if field == "original_child_exit" {
            serde_json::Value::Null
        } else {
            false.into()
        };
        assert!(
            serde_json::from_value::<Receipt>(changed)
                .unwrap()
                .require(&request, stamp)
                .is_err()
        );
    }
    receipt.passed = true;
    receipt.stage = "complete".into();
    assert!(receipt.require(&request, stamp).is_err());
}
#[test]
fn station_lowbox_desktop_receipt_refuses_rebound_original_name_policy_and_owner() {
    let (request, stamp, mut receipt) = checkpoint_fixture();
    let name = format!("alysis-station-lowbox-{}", request.birth.nonce);
    let evidence = serde_json::json!({
        "name": name,
        "descriptor": "O:SYD:P",
        "owner_sid": request.birth.selection.account.account_sid,
        "passed": false, "stage": "actor", "actor_verified": false,
        "attached_name_verified": false, "positive_open": false,
        "attached_same_object": false, "descriptor_matches": false,
        "open_denials": [], "fresh_closed": false
    });
    receipt.desktop_name = Some(name);
    receipt.desktop_before = Some("O:SYD:P".into());
    receipt.desktop_child = Some(serde_json::from_value(evidence.clone()).unwrap());
    receipt.require(&request, stamp).unwrap(); // Incomplete diagnostic, never success.
    for (field, value) in [
        (
            "name",
            "alysis-station-lowbox-ffffffffffffffffffffffffffffffff",
        ),
        ("descriptor", "O:SYD:P(A;;GA;;;SY)"),
        ("owner_sid", "S-1-5-21-1-2-3-9999"),
    ] {
        let mut changed = evidence.clone();
        changed[field] = value.into();
        receipt.desktop_child = Some(serde_json::from_value(changed).unwrap());
        assert!(receipt.require(&request, stamp).is_err(), "changed {field}");
    }
    receipt.desktop_child = Some(serde_json::from_value(evidence).unwrap());
    receipt.desktop_before = None;
    assert!(receipt.require(&request, stamp).is_err());
}

#[test]
fn station_lowbox_checkpoint_is_bounded_and_never_a_terminal_receipt() {
    let (request, stamp, receipt) = checkpoint_fixture();
    let diagnostic = encode(&Frame::Diagnostic {
        receipt: receipt.clone(),
    })
    .unwrap();
    let finished = encode(&Frame::Finished {
        receipt: receipt.clone(),
    })
    .unwrap();
    let mut frames = std::collections::VecDeque::from([diagnostic.clone(), finished]);
    let mut saved = 0;
    let frame = receive_phase(
        &request,
        stamp,
        "create_child",
        TIMEOUT,
        |_| Ok(frames.pop_front().unwrap()),
        |r, _| {
            assert!(!r.passed);
            saved += 1;
            Ok(())
        },
    )
    .unwrap();
    assert!(matches!(frame, Frame::Finished { .. }));
    assert_eq!(saved, 1);
    let mut frames = std::collections::VecDeque::from([diagnostic.clone(), diagnostic]);
    assert!(
        receive_phase(
            &request,
            stamp,
            "create_child",
            TIMEOUT,
            |_| Ok(frames.pop_front().unwrap()),
            |_, _| Ok(())
        )
        .is_err()
    );
    assert!(frames.is_empty()); // Never reads a third frame after repetition.
    assert!(
        receive_phase(
            &request,
            stamp,
            "create_child",
            Duration::ZERO,
            |_| panic!("expired deadline read"),
            |_, _| panic!("expired deadline save")
        )
        .is_err()
    );
    CHECKPOINT.with(|slot| *slot.borrow_mut() = None);
    save_checkpoint(&receipt, TIMEOUT).unwrap();
    assert!(save_checkpoint(&receipt, TIMEOUT).is_err());
    RECEIPT.with(|slot| assert!(slot.borrow().is_none()));
    CHECKPOINT.with(|slot| *slot.borrow_mut() = None);
}
#[test]
fn station_lowbox_checkpoint_refuses_changed_subject_phase_and_success() {
    let (request, stamp, mut receipt) = checkpoint_fixture();
    require_checkpoint(&receipt, &request, stamp, "create_child").unwrap();
    assert!(
        require_checkpoint(
            &receipt,
            &request,
            ([7, 0], stamp.1, stamp.2),
            "create_child"
        )
        .is_err()
    );
    let mut other = request.clone();
    other.nonce = format!("{:032x}", 20);
    assert!(require_checkpoint(&receipt, &other, stamp, "create_child").is_err());
    assert!(require_checkpoint(&receipt, &request, stamp, "loader_observation").is_err());
    receipt.passed = true;
    assert!(require_checkpoint(&receipt, &request, stamp, "create_child").is_err());
    receipt.passed = false;
    receipt.original_child = Some((7, 9));
    assert!(require_checkpoint(&receipt, &request, stamp, "create_child").is_err());
    receipt.stage = "loader_observation".into();
    receipt.loader = Some(loader::LoaderTrace::new());
    assert!(require_checkpoint(&receipt, &request, stamp, "loader_observation").is_err());
}
#[test]
fn station_lowbox_birth_comparison_is_recorded_but_cannot_qualify_containment() {
    let (request, stamp, mut receipt) = checkpoint_fixture();
    receipt.security_capabilities_birth = true;
    let decoded: Receipt = decode(&encode(&receipt).unwrap()).unwrap();
    assert!(decoded.security_capabilities_birth);
    decoded.require(&request, stamp).unwrap();
    receipt.passed = true;
    assert!(
        receipt
            .require(&request, stamp)
            .unwrap_err()
            .to_string()
            .contains("birth comparison cannot qualify containment")
    );
}
fn successful() -> ChildProof {
    let deny = ERROR_ACCESS_DENIED.to_hresult().0;
    ChildProof {
        passed: true,
        stage: "complete".into(),
        hresult: None,
        positive_open: true,
        native_name_verified: true,
        open_denials: vec![(0x40000, deny), (0x80000, deny), (0xc0000, deny)],
        set_user_dacl: Some(deny),
        set_user_owner: Some(deny),
        nt_set_dacl: Some(0xc0000022u32 as i32),
        nt_set_owner: Some(0xc0000022u32 as i32),
        attached_same_object: true,
        attached_set_user_dacl: Some(deny),
        attached_set_user_owner: Some(deny),
        attached_nt_set_dacl: Some(0xc0000022u32 as i32),
        attached_nt_set_owner: Some(0xc0000022u32 as i32),
    }
}
#[test]
fn station_lowbox_kernelbase_comparison_cannot_mix_birth_paths_or_claim_authority() {
    let (request, stamp, mut receipt) = constructor_fixture();
    receipt.stage = "kernelbase_token".into();
    receipt.kernelbase_token_birth = true;
    receipt.require(&request, stamp).unwrap();
    let decoded: Receipt = decode(&encode(&receipt).unwrap()).unwrap();
    assert!(decoded.kernelbase_token_birth);
    decoded.require(&request, stamp).unwrap();
    receipt.security_capabilities_birth = true;
    assert!(receipt.require(&request, stamp).is_err());
    receipt.security_capabilities_birth = false;
    receipt.passed = true;
    assert!(
        receipt
            .require(&request, stamp)
            .unwrap_err()
            .to_string()
            .contains("KernelBase comparison")
    );
}
#[test]
fn station_lowbox_constructor_observations_reject_unknown_paths_and_unclosed_success() {
    let (request, stamp, mut receipt) = constructor_fixture();
    receipt.kernelbase_token_birth = true;
    receipt.constructor_directories = [
        ("package", 0x0f),
        ("private_rpc", 0x0f),
        ("shared_base", 0x0f),
        ("shared_rpc", 0x0f),
        ("session_base", 0x20003),
        ("session_appcontainers", 0x0f),
    ]
    .into_iter()
    .map(|(name, mask)| (name.into(), mask, 0, true))
    .collect();
    receipt.require(&request, stamp).unwrap();
    for case in 0..5 {
        let mut changed = receipt.clone();
        match case {
            0 => changed.constructor_directories[0].0 = "caller_path".into(),
            1 => changed.constructor_directories[0].1 = 0xf000f,
            2 => changed.constructor_directories[0].3 = false,
            3 => changed.constructor_directories[0].2 = 0xc0000022u32 as i32,
            _ => {
                changed.constructor_directories.pop();
            }
        }
        assert!(changed.require(&request, stamp).is_err());
    }
}
#[test]
fn station_lowbox_evidence_requires_all_native_denials_and_positive_control() {
    successful().require().unwrap();
    for case in 0..15 {
        let mut proof = successful();
        match case {
            0 => proof.positive_open = false,
            1 => {
                proof.open_denials.pop();
            }
            2 => proof.open_denials[0].0 = 0x20000,
            3 => proof.open_denials.swap(0, 1),
            4 => proof.set_user_dacl = None,
            5 => proof.set_user_owner = None,
            6 => proof.nt_set_dacl = None,
            7 => proof.nt_set_owner = None,
            8 => proof.hresult = Some(ERROR_ACCESS_DENIED.to_hresult().0),
            9 => proof.attached_same_object = false,
            10 => proof.attached_set_user_dacl = None,
            11 => proof.attached_set_user_owner = None,
            12 => proof.attached_nt_set_dacl = None,
            13 => proof.attached_nt_set_owner = None,
            _ => proof.native_name_verified = false,
        }
        assert!(proof.require().is_err(), "missing control {case}");
    }
}

#[test]
fn station_lowbox_constructor_links_require_fixed_objects_rights_and_closure() {
    let (request, stamp, mut receipt) = constructor_fixture();
    receipt.kernelbase_token_birth = true;
    receipt.constructor_links = ["Global", "Local", "Session"]
        .into_iter()
        .flat_map(|name| {
            [
                (name.into(), 1, 0, true),
                (name.into(), 0xf0001, 0xc0000022u32 as i32, false),
            ]
        })
        .collect();
    receipt.require(&request, stamp).unwrap();
    for case in 0..6 {
        let mut changed = receipt.clone();
        match case {
            0 => changed.constructor_links[0].0 = "Global\\foreign".into(),
            1 => changed.constructor_links[0].1 = 0xf0001,
            2 => changed.constructor_links[0].3 = false,
            3 => changed.constructor_links[1].3 = true,
            4 => changed.kernelbase_token_birth = false,
            _ => {
                changed.constructor_links.pop();
            }
        }
        assert!(changed.require(&request, stamp).is_err());
    }
}
#[test]
fn station_lowbox_constructor_directory_upgrades_require_fixed_scope_and_closure() {
    let (request, stamp, mut receipt) = constructor_fixture();
    receipt.kernelbase_token_birth = true;
    receipt.constructor_directory_upgrades = vec![
        ("package".into(), 0xf000f, 0, true),
        ("private_rpc".into(), 0xf000f, 0xc0000022u32 as i32, false),
    ];
    receipt.require(&request, stamp).unwrap();
    for case in 0..7 {
        let mut changed = receipt.clone();
        match case {
            0 => changed.constructor_directory_upgrades[1].0 = "shared_rpc".into(),
            1 => changed.constructor_directory_upgrades[1].1 = 0x2000f,
            2 => changed.constructor_directory_upgrades[0].3 = false,
            3 => changed.constructor_directory_upgrades[1].3 = true,
            4 => changed.kernelbase_token_birth = false,
            5 => changed.passed = true,
            _ => {
                changed.constructor_directory_upgrades.pop();
            }
        }
        assert!(changed.require(&request, stamp).is_err());
    }
}

#[test]
fn station_capture_subset_cannot_mix_constructors_or_claim_success() {
    let (request, stamp, mut receipt) = checkpoint_fixture();
    receipt.captured_namespace_birth = true;
    receipt.require(&request, stamp).unwrap();
    for case in 0..4 {
        let mut changed = receipt.clone();
        match case {
            0 => changed.kernelbase_token_birth = true,
            1 => changed.security_capabilities_birth = true,
            2 => changed.passed = true,
            _ => changed.original_child = Some((123, request.created + 1)),
        }
        assert!(changed.require(&request, stamp).is_err());
    }
}

#[test]
fn station_capture_failure_keeps_bounded_terminal_receipt() {
    for stage in [
        "captured_namespace_token",
        "captured_namespace_pipe_open",
        "captured_namespace_pipe_identity",
    ] {
        let (request, stamp, mut receipt) = checkpoint_fixture();
        receipt.stage = stage.into();
        receipt.captured_namespace_birth = true;
        receipt.hresult = Some(ERROR_ACCESS_DENIED.to_hresult().0);
        let bytes = encode(&Frame::Finished { receipt }).unwrap();
        let Frame::Finished { receipt } = decode(&bytes).unwrap() else {
            panic!("terminal receipt lost")
        };
        receipt.require(&request, stamp).unwrap();
        assert!(
            !receipt.passed
                && receipt.original_child.is_none()
                && receipt.captured_namespace.is_none()
        );
        assert_eq!(receipt.hresult, Some(ERROR_ACCESS_DENIED.to_hresult().0));
    }
}

#[test]
fn station_lowbox_unrelated_native_errors_cannot_qualify_refusal() {
    for wrong in [
        0,
        ERROR_INVALID_HANDLE.to_hresult().0,
        ERROR_INVALID_OWNER.to_hresult().0,
        ERROR_FILE_NOT_FOUND.to_hresult().0,
        0xc0000008u32 as i32,
    ] {
        for case in 0..9 {
            let mut proof = successful();
            match case {
                0 => proof.open_denials[0].1 = wrong,
                1 => proof.set_user_dacl = Some(wrong),
                2 => proof.set_user_owner = Some(wrong),
                3 => proof.nt_set_dacl = Some(wrong),
                4 => proof.nt_set_owner = Some(wrong),
                5 => proof.attached_set_user_dacl = Some(wrong),
                6 => proof.attached_set_user_owner = Some(wrong),
                7 => proof.attached_nt_set_dacl = Some(wrong),
                _ => proof.attached_nt_set_owner = Some(wrong),
            }
            assert!(
                proof.require().is_err(),
                "unrelated status {wrong} case {case}"
            );
        }
    }
}
#[test]
fn station_lowbox_codec_rejects_unknown_fields_bad_prefix_and_oversize() {
    let bytes = encode(&successful()).unwrap();
    decode::<ChildProof>(&bytes).unwrap().require().unwrap();
    let mut value = serde_json::to_value(successful()).unwrap();
    value["skip_owner_check"] = serde_json::json!(true);
    assert!(decode::<ChildProof>(&encode(&value).unwrap()).is_err());
    let mut wrong = bytes.clone();
    wrong[0] ^= 1;
    assert!(decode::<ChildProof>(&wrong).is_err());
    let mut wrong = bytes.clone();
    wrong.pop();
    assert!(decode::<ChildProof>(&wrong).is_err());
    let mut wrong = bytes;
    wrong.resize(LIMIT + 1, b' ');
    assert!(decode::<ChildProof>(&wrong).is_err());
    assert!(encode(&"x".repeat(LIMIT)).is_err());
}
#[test]
fn station_lowbox_failure_stage_does_not_become_completed_evidence() {
    let mut proof = ChildProof::new();
    proof.require().unwrap();
    proof.stage = "complete".into();
    assert!(proof.require().is_err());
    proof.stage = "pretend_deny".into();
    assert!(proof.require().is_err());
    proof.stage = "denied_open".into();
    proof.open_denials = vec![(0, 0); 4];
    assert!(proof.require().is_err());
}

#[test]
fn station_v3_bounded_completion_requires_explicit_identity_and_all_proofs() {
    use bootstrap::entry::credential_wire::child::LowboxCompletion;
    for (mode, completion) in [
        (
            crate::account_install::AccountMode::Offline,
            LowboxCompletion::V3ShadowBoundedV1,
        ),
        (
            crate::account_install::AccountMode::Online,
            LowboxCompletion::V3ShadowOnlineBoundedV1,
        ),
    ] {
        let (mut request, stamp, mut legacy) = checkpoint_fixture();
        request.birth.selection.account = crate::account_install::synthetic_ready()
            .binding(mode)
            .unwrap();
        legacy.v3_shadow_bounded = true;
        assert!(legacy.require(&request, stamp).is_err());
        let Purpose::FixtureStationLowboxV1 { binding } = &mut request.birth.purpose else {
            unreachable!()
        };
        binding.policy = 6;
        binding.completion = completion;
        let mut receipt = Receipt::new(&request, stamp).unwrap();
        receipt.require(&request, stamp).unwrap();
        receipt.original_child = Some((7, 9));
        assert!(
            receipt
                .require(&request, stamp)
                .unwrap_err()
                .to_string()
                .contains("completion identity")
        );
        receipt.v3_shadow_bounded = true;
        receipt.require(&request, stamp).unwrap();
        for flag in [
            "rpc_system_inheritance",
            "rpc_nsi_system_inheritance",
            "kernelbase_token_birth",
            "security_capabilities_birth",
            "captured_namespace_birth",
            "documented_desktop_birth",
            "inherited_desktop_selection",
            "appcontainer_without_inherited_user",
        ] {
            let mut value = serde_json::to_value(&receipt).unwrap();
            value[flag] = true.into();
            assert!(
                serde_json::from_value::<Receipt>(value)
                    .unwrap()
                    .require(&request, stamp)
                    .is_err()
            );
        }
        receipt.passed = true;
        receipt.stage = "complete".into();
        assert!(receipt.require(&request, stamp).is_err());
        let mut value = serde_json::to_value(&receipt).unwrap();
        value.as_object_mut().unwrap().remove("v3_shadow_bounded");
        assert!(
            serde_json::from_value::<Receipt>(value)
                .unwrap()
                .require(&request, stamp)
                .is_err()
        );
    }
}
