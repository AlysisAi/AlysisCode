//! Pure protocol/identity checks. No SAM, process, job, profile or USER effects.
use super::*;
use crate::account_control::bootstrap::entry::initializer_wire;

fn id(value: u32) -> String {
    format!("{value:032x}")
}

pub(super) fn fixture() -> (BirthRequest, FixtureStationLowboxBinding, probe::Request) {
    fixture_mode(AccountMode::Offline)
}

fn fixture_mode(mode: AccountMode) -> (BirthRequest, FixtureStationLowboxBinding, probe::Request) {
    let ready = crate::account_install::synthetic_ready();
    let selected = Selection {
        protocol: Selection::protocol(),
        operation: id(1),
        logon: id(15),
        ready: id(16),
        bootstrap_pid: 600,
        bootstrap_created: 200,
        image_id: [2; 24],
        session: 1,
        account: ready.binding(mode).unwrap(),
        credential_generation: match mode {
            AccountMode::Offline => ready.offline.as_ref(),
            AccountMode::Online => ready.online.as_ref(),
        }
        .unwrap()
        .credential_generation
        .clone()
        .unwrap(),
    };
    let container = format!("Alysis.Execution.{}", id(2));
    let package_sid = crate::appcontainer::derive(&container)
        .unwrap()
        .string()
        .unwrap();
    let profile = FixtureWriterBinding {
        container: container.clone(),
        package_sid: package_sid.clone(),
        folder: [4; 24],
        owner_birth: id(11),
        owner_resume: id(12),
        owner_pid: 501,
        owner_created: 100,
        use_nonce: id(13),
        unsupported_registry: false,
        hold_rounds: 0,
        descendant_files: false,
    };
    let binding = FixtureStationLowboxBinding {
        profile,
        initializer: paired::Request {
            subject: initializer_wire::Request {
                operation: selected.operation.clone(),
                nonce: id(3),
                bootstrap_pid: selected.bootstrap_pid,
                bootstrap_created: selected.bootstrap_created,
                container: container.clone(),
                package_sid: package_sid.clone(),
                owner: selected.account.owner_sid.clone(),
                session: selected.session,
                // The initializer is a different image from the account helper.
                image_id: [8; 24],
                omit_profile_environment: false,
            },
            resources: paired::Resources {
                owner_job: id(4),
                consumer_job: id(5),
                namespace: id(6),
                host_profile: id(7),
                host_profile_use: id(8),
                station: id(9),
                desktop: id(10),
            },
        },
        policy: 1,
        completion: LowboxCompletion::LegacyDiagnosticV1,
    };
    let mut owner_selection = selected.clone();
    owner_selection.logon = id(17);
    owner_selection.ready = id(18);
    owner_selection.bootstrap_pid = 500;
    owner_selection.bootstrap_created = 90;
    let mut owner_birth = BirthRequest::new(owner_selection, id(11)).unwrap();
    owner_birth.purpose = Purpose::TrustedPairedProfileV1 {
        container,
        package_sid,
        policy: 1,
    };
    let owner = probe::Request {
        birth: owner_birth,
        nonce: id(12),
        pid: 501,
        created: 100,
    };
    let mut birth = BirthRequest::new(selected, id(14)).unwrap();
    birth.purpose = Purpose::FixtureStationLowboxV1 {
        binding: binding.clone(),
    };
    (birth, binding, owner)
}

fn v3_fixture(mode: AccountMode) -> (BirthRequest, FixtureStationLowboxBinding, probe::Request) {
    let (mut birth, mut binding, owner) = fixture_mode(mode);
    binding.policy = 6;
    binding.completion = match mode {
        AccountMode::Offline => LowboxCompletion::V3ShadowBoundedV1,
        AccountMode::Online => LowboxCompletion::V3ShadowOnlineBoundedV1,
    };
    birth.purpose = Purpose::FixtureStationLowboxV1 {
        binding: binding.clone(),
    };
    (birth, binding, owner)
}

#[test]
fn station_lowbox_codec_binds_original_profile_initializer_and_derived_desktop() {
    let (birth, binding, owner) = fixture();
    binding
        .validate_for(&birth.selection, &birth.nonce)
        .unwrap();
    binding
        .require_owner(&owner, binding.profile.folder, &binding.profile.use_nonce)
        .unwrap();
    binding.require_initializer(&binding.initializer).unwrap();
    assert_eq!(
        binding.desktop_name().unwrap(),
        format!("WinSta0\\alysis-paired-desktop-{}", id(1))
    );
    let bytes = birth.encode().unwrap();
    assert!(bytes.len() <= 4608);
    assert!(BirthRequest::decode(&bytes, &birth.selection).unwrap() == birth);
    assert!(binding.initializer.subject.image_id != birth.selection.image_id);
}

#[test]
fn station_lowbox_rpc_binding_policy_is_explicit_passive_and_plan_bound() {
    use crate::account_state::execution::resources::FixtureRpcPolicy as Policy;
    let (birth, baseline, _) = fixture();
    let policies = [
        Policy::BaselineV1,
        Policy::KernelBaseAccountFullV1,
        Policy::SystemInheritanceV1,
        Policy::NsiSystemInheritanceV1,
    ];
    for kind in policies {
        let mut variant = baseline.clone();
        variant.policy = kind.fixture_binding_policy();
        variant.validate().unwrap(); // A fixed codec identity is not live authority.
        for original in policies {
            assert_eq!(
                variant.require_rpc_policy(original).is_ok(),
                kind == original
            );
        }
        let mut selected = birth.clone();
        selected.purpose = Purpose::FixtureStationLowboxV1 { binding: variant };
        let bytes = selected.encode().unwrap();
        assert!(bytes.len() <= 4608);
        assert!(BirthRequest::decode(&bytes, &selected.selection).unwrap() == selected);
        let mut wrong = selected.selection.clone();
        wrong.bootstrap_created += 1;
        assert!(BirthRequest::decode(&bytes, &wrong).is_err());
    }
    assert!(!crate::account_control::station_lowbox_fixture::ENABLED.with(|s| s.get()));
    let mut variant = baseline.clone();
    for policy in [0, 4, 6, u32::MAX] {
        variant.policy = policy;
        assert!(variant.validate().is_err());
    }
    let mut omitted = serde_json::to_value(&baseline).unwrap();
    omitted.as_object_mut().unwrap().remove("policy");
    assert!(serde_json::from_value::<FixtureStationLowboxBinding>(omitted).is_err());
}

#[test]
fn station_lowbox_completion_omission_preserves_legacy_and_refuses_v3_policy() {
    let (birth, baseline, _) = fixture();
    let wire = serde_json::to_value(&baseline).unwrap();
    assert!(wire.get("completion").is_none());
    let decoded: FixtureStationLowboxBinding = serde_json::from_value(wire.clone()).unwrap();
    assert_eq!(decoded.completion, LowboxCompletion::LegacyDiagnosticV1);
    assert!(!decoded.is_v3_shadow());
    assert_eq!(decoded.v3_mode(), None);
    decoded.validate().unwrap();

    let original = serde_json::to_value(&birth).unwrap();
    for completion in [None, Some("legacy_diagnostic_v1")] {
        let mut value = original.clone();
        let binding = &mut value["purpose"]["fixture_station_lowbox_v1"]["binding"];
        binding["policy"] = serde_json::json!(6);
        if let Some(completion) = completion {
            binding["completion"] = serde_json::json!(completion);
        }
        assert!(
            BirthRequest::decode(&serde_json::to_vec(&value).unwrap(), &birth.selection).is_err()
        );
    }
    for completion in [
        serde_json::json!(null),
        serde_json::json!("future_completion"),
    ] {
        let mut value = wire.clone();
        value["completion"] = completion;
        assert!(serde_json::from_value::<FixtureStationLowboxBinding>(value).is_err());
    }
}

#[test]
fn station_lowbox_v3_shadow_completion_is_explicit_passive_and_root_bound() {
    let (mut birth, mut binding, owner) = fixture();
    binding.completion = LowboxCompletion::V3ShadowBoundedV1;
    binding.policy = 6;
    binding
        .validate_for(&birth.selection, &birth.nonce)
        .unwrap();
    binding
        .require_owner(&owner, binding.profile.folder, &binding.profile.use_nonce)
        .unwrap();
    binding.require_initializer(&binding.initializer).unwrap();
    assert!(binding.is_v3_shadow());
    assert_eq!(binding.v3_mode(), Some(AccountMode::Offline));
    assert_eq!(
        binding.rpc_policy().unwrap(),
        FixtureRpcPolicy::SystemNsiReadInheritanceV3
    );
    binding
        .require_rpc_policy(FixtureRpcPolicy::SystemNsiReadInheritanceV3)
        .unwrap();
    binding
        .require_namespace_birth(FixtureRootBirthKind::SessionShadowDirectoryV1)
        .unwrap();
    for wrong in [
        FixtureRootBirthKind::LegacyUnspecified,
        FixtureRootBirthKind::OrdinaryDirectoryV1,
    ] {
        assert!(binding.require_namespace_birth(wrong).is_err());
    }
    assert!(
        binding
            .require_rpc_policy(FixtureRpcPolicy::NsiSystemInheritanceV1)
            .is_err()
    );
    assert!(FixtureRpcPolicy::from_fixture_binding_policy(6).is_err());
    assert!(crate::account_rpc_v3_lifecycle_fixture::require_live().is_err());
    assert!(!crate::account_control::station_lowbox_fixture::ENABLED.with(|s| s.get()));

    birth.purpose = Purpose::FixtureStationLowboxV1 { binding };
    let bytes = birth.encode().unwrap();
    assert!(bytes.len() <= 4608);
    assert!(BirthRequest::decode(&bytes, &birth.selection).unwrap() == birth);
    let mut omitted = serde_json::to_value(&birth).unwrap();
    let wire = &mut omitted["purpose"]["fixture_station_lowbox_v1"]["binding"];
    assert_eq!(
        wire["completion"],
        serde_json::json!("v3_shadow_bounded_v1")
    );
    wire.as_object_mut().unwrap().remove("completion");
    assert!(
        BirthRequest::decode(&serde_json::to_vec(&omitted).unwrap(), &birth.selection).is_err()
    );
}

#[test]
fn station_lowbox_v3_online_completion_is_distinct_passive_and_root_bound() {
    let (birth, binding, owner) = v3_fixture(AccountMode::Online);
    binding
        .validate_for(&birth.selection, &birth.nonce)
        .unwrap();
    binding
        .require_owner(&owner, binding.profile.folder, &binding.profile.use_nonce)
        .unwrap();
    binding.require_initializer(&binding.initializer).unwrap();
    assert!(binding.is_v3_shadow());
    assert_eq!(binding.v3_mode(), Some(AccountMode::Online));
    binding
        .require_rpc_policy(FixtureRpcPolicy::SystemNsiReadInheritanceV3)
        .unwrap();
    binding
        .require_namespace_birth(FixtureRootBirthKind::SessionShadowDirectoryV1)
        .unwrap();
    for wrong in [
        FixtureRootBirthKind::LegacyUnspecified,
        FixtureRootBirthKind::OrdinaryDirectoryV1,
    ] {
        assert!(binding.require_namespace_birth(wrong).is_err());
    }
    assert!(FixtureRpcPolicy::from_fixture_binding_policy(6).is_err());
    assert!(crate::account_rpc_v3_lifecycle_fixture::require_contained_online_live().is_err());
    assert_eq!(
        crate::account_rpc_v3_lifecycle_fixture::contained_mode().unwrap(),
        None
    );
    assert!(!crate::account_control::station_lowbox_fixture::ENABLED.with(|s| s.get()));

    let bytes = birth.encode().unwrap();
    assert!(bytes.len() <= 4608);
    assert!(BirthRequest::decode(&bytes, &birth.selection).unwrap() == birth);
    let mut omitted = serde_json::to_value(&birth).unwrap();
    let wire = &mut omitted["purpose"]["fixture_station_lowbox_v1"]["binding"];
    assert_eq!(
        wire["completion"],
        serde_json::json!("v3_shadow_online_bounded_v1")
    );
    wire.as_object_mut().unwrap().remove("completion");
    assert!(
        BirthRequest::decode(&serde_json::to_vec(&omitted).unwrap(), &birth.selection).is_err()
    );
}

#[test]
fn station_lowbox_v3_completion_rejects_cross_mode_selection_owner_and_codec_replay() {
    for (mode, other) in [
        (AccountMode::Offline, AccountMode::Online),
        (AccountMode::Online, AccountMode::Offline),
    ] {
        let (birth, binding, owner) = v3_fixture(mode);
        binding
            .validate_for(&birth.selection, &birth.nonce)
            .unwrap();
        binding
            .require_owner(&owner, binding.profile.folder, &binding.profile.use_nonce)
            .unwrap();
        let (other_birth, other_binding, other_owner) = v3_fixture(other);
        // Both identities are individually valid; only the explicit mode differs.
        other_birth.validate().unwrap();
        other_owner.validate().unwrap();
        assert!(
            binding
                .validate_for(&other_birth.selection, &birth.nonce)
                .unwrap_err()
                .to_string()
                .contains("account mode")
        );
        assert!(
            binding
                .require_owner(
                    &other_owner,
                    binding.profile.folder,
                    &binding.profile.use_nonce
                )
                .unwrap_err()
                .to_string()
                .contains("owner mode")
        );

        let mut crossed = birth.clone();
        crossed.selection = other_birth.selection;
        assert!(
            BirthRequest::decode(&serde_json::to_vec(&crossed).unwrap(), &crossed.selection)
                .is_err()
        );
        crossed = birth.clone();
        let Purpose::FixtureStationLowboxV1 { binding: changed } = &mut crossed.purpose else {
            unreachable!()
        };
        changed.completion = other_binding.completion;
        assert!(
            BirthRequest::decode(&serde_json::to_vec(&crossed).unwrap(), &birth.selection).is_err()
        );
    }
}

#[test]
fn station_lowbox_legacy_completion_keeps_existing_account_modes() {
    for mode in [AccountMode::Offline, AccountMode::Online] {
        let (birth, binding, owner) = fixture_mode(mode);
        assert_eq!(binding.v3_mode(), None);
        binding
            .validate_for(&birth.selection, &birth.nonce)
            .unwrap();
        binding
            .require_owner(&owner, binding.profile.folder, &binding.profile.use_nonce)
            .unwrap();
        let bytes = birth.encode().unwrap();
        assert!(BirthRequest::decode(&bytes, &birth.selection).unwrap() == birth);
        assert!(
            serde_json::to_value(&binding)
                .unwrap()
                .get("completion")
                .is_none()
        );
    }
}

#[test]
fn station_lowbox_completion_rejects_mixed_policy_and_root_identities() {
    let (_, baseline, _) = fixture();
    for policy in [1, 2, 3, 5] {
        let mut legacy = baseline.clone();
        legacy.policy = policy;
        legacy
            .require_namespace_birth(FixtureRootBirthKind::LegacyUnspecified)
            .unwrap();
        assert!(
            legacy
                .require_namespace_birth(FixtureRootBirthKind::SessionShadowDirectoryV1)
                .is_err()
        );
        assert!(
            legacy
                .require_namespace_birth(FixtureRootBirthKind::OrdinaryDirectoryV1)
                .is_err()
        );
        assert!(
            legacy
                .require_rpc_policy(FixtureRpcPolicy::SystemNsiReadInheritanceV3)
                .is_err()
        );
    }
    for completion in [
        LowboxCompletion::V3ShadowBoundedV1,
        LowboxCompletion::V3ShadowOnlineBoundedV1,
    ] {
        for policy in [0, 1, 2, 3, 4, 5, u32::MAX] {
            let mut mixed = baseline.clone();
            mixed.completion = completion;
            mixed.policy = policy;
            assert!(mixed.validate().is_err());
            assert!(
                mixed
                    .require_namespace_birth(FixtureRootBirthKind::SessionShadowDirectoryV1)
                    .is_err()
            );
        }
    }
}

#[test]
fn station_lowbox_v3_completion_preserves_original_request_and_owner_validation() {
    let (birth, mut binding, owner) = fixture();
    binding.completion = LowboxCompletion::V3ShadowBoundedV1;
    binding.policy = 6;
    let mut wrong_selection = birth.selection.clone();
    wrong_selection.bootstrap_created += 1;
    assert!(
        binding
            .validate_for(&wrong_selection, &birth.nonce)
            .is_err()
    );
    let mut wrong_owner = owner.clone();
    wrong_owner.created += 1;
    assert!(
        binding
            .require_owner(
                &wrong_owner,
                binding.profile.folder,
                &binding.profile.use_nonce
            )
            .is_err()
    );
    let mut wrong_initializer = binding.initializer.clone();
    wrong_initializer.resources.namespace = id(80);
    assert!(binding.require_initializer(&wrong_initializer).is_err());
    assert!(
        binding
            .require_resume_nonce(&binding.profile.use_nonce)
            .is_err()
    );
    binding.profile.unsupported_registry = true;
    assert!(binding.validate().is_err());
}

#[test]
fn station_lowbox_admission_requires_fixed_handoff_and_validated_native_subject() {
    let (birth, _, _) = fixture();
    assert!(birth.purpose.is_paired_consumer());
    assert!(birth.purpose.is_self_check());
    assert!(birth.purpose.uses_handoff());
    assert_eq!(birth.purpose.logon_flags(), CREATE_PROCESS_LOGON_FLAGS(0));
    birth.purpose.require_native_dispatch().unwrap();
    let mut wrong = birth.purpose.clone();
    let Purpose::FixtureStationLowboxV1 { binding } = &mut wrong else {
        unreachable!()
    };
    binding.policy = 0;
    assert!(wrong.require_native_dispatch().is_err());
    assert!(!Purpose::FixturePrivateStationV1.is_paired_consumer());
    assert!(
        Purpose::TrustedSelfCheckV1
            .require_native_dispatch()
            .is_ok()
    );
}

#[test]
fn station_lowbox_resume_nonce_cannot_alias_any_original_resource_or_owner_use() {
    let (_, binding, _) = fixture();
    binding.require_resume_nonce(&id(31)).unwrap();
    for nonce in binding.reserved_nonces().unwrap() {
        assert!(binding.require_resume_nonce(nonce).is_err());
    }
    assert!(binding.require_resume_nonce("../alias").is_err());
}

#[test]
fn station_lowbox_rejects_wrong_original_owner_folder_and_use() {
    let (_, binding, owner) = fixture();
    for case in 0..10 {
        let mut changed = owner.clone();
        match case {
            0 => changed.pid += 1,
            1 => changed.created += 1,
            2 => changed.nonce = id(80),
            3 => changed.birth.nonce = id(80),
            4 => changed.birth.selection.operation = id(80),
            5 => changed.birth.selection.session += 1,
            6 => changed.birth.selection.account.owner_sid = "S-1-5-21-1-2-3-9876".into(),
            7 => changed.birth.selection.bootstrap_pid = binding.initializer.subject.bootstrap_pid,
            8 => changed.birth.purpose = Purpose::TrustedSelfCheckV1,
            _ => changed.birth.selection.logon = binding.initializer.resources.desktop.clone(),
        }
        assert!(
            binding
                .require_owner(&changed, binding.profile.folder, &binding.profile.use_nonce)
                .is_err(),
            "owner drift case {case}"
        );
    }
    assert!(
        binding
            .require_owner(&owner, [5; 24], &binding.profile.use_nonce)
            .is_err()
    );
    assert!(
        binding
            .require_owner(&owner, binding.profile.folder, &id(80))
            .is_err()
    );
}

#[test]
fn station_lowbox_requires_complete_original_initializer_not_just_subject_name() {
    let (_, binding, _) = fixture();
    for case in 0..15 {
        let mut changed = binding.initializer.clone();
        match case {
            0 => changed.resources.owner_job = id(80),
            1 => changed.resources.consumer_job = id(80),
            2 => changed.resources.namespace = id(80),
            3 => changed.resources.host_profile = id(80),
            4 => changed.resources.host_profile_use = id(80),
            5 => changed.resources.station = id(80),
            6 => changed.resources.desktop = id(80),
            7 => changed.subject.nonce = id(80),
            8 => changed.subject.operation = id(80),
            9 => changed.subject.bootstrap_pid += 1,
            10 => changed.subject.bootstrap_created += 1,
            11 => changed.subject.session += 1,
            12 => changed.subject.image_id[0] ^= 1,
            13 => changed.subject.owner = "S-1-5-21-1-2-3-9876".into(),
            _ => changed.subject.omit_profile_environment = true,
        }
        assert!(
            binding.require_initializer(&changed).is_err(),
            "initializer drift case {case}"
        );
        let changed_binding = FixtureStationLowboxBinding {
            initializer: changed,
            ..binding.clone()
        };
        assert!(
            changed_binding
                .require_initializer(&binding.initializer)
                .is_err()
        );
    }
}

#[test]
fn station_lowbox_selection_policy_and_workload_drift_are_refused() {
    let (birth, binding, _) = fixture();
    for case in 0..5 {
        let mut selected = birth.selection.clone();
        match case {
            0 => selected.operation = id(80),
            1 => selected.bootstrap_pid += 1,
            2 => selected.bootstrap_created += 1,
            3 => selected.session += 1,
            _ => selected.account.owner_sid = "S-1-5-21-1-2-3-9876".into(),
        }
        assert!(binding.validate_for(&selected, &birth.nonce).is_err());
    }
    for case in 0..10 {
        let mut changed = binding.clone();
        match case {
            0 => changed.policy = 0,
            1 => changed.initializer.subject.omit_profile_environment = true,
            2 => changed.profile.unsupported_registry = true,
            3 => changed.profile.hold_rounds = 12,
            4 => {
                changed.profile.descendant_files = true;
                changed.profile.hold_rounds = 12;
            }
            5 => changed.profile.folder = [0; 24],
            6 => changed.profile.owner_pid = changed.initializer.subject.bootstrap_pid,
            7 => changed.profile.owner_created = changed.initializer.subject.bootstrap_created + 1,
            8 => changed.profile.package_sid = "S-1-15-2-1".into(),
            _ => {
                changed.profile.container = format!("Alysis.Execution.{}", id(80));
                changed.profile.package_sid =
                    crate::appcontainer::derive(&changed.profile.container)
                        .unwrap()
                        .string()
                        .unwrap();
            }
        }
        assert!(
            changed
                .validate_for(&birth.selection, &birth.nonce)
                .is_err(),
            "policy case {case}"
        );
    }
    let bytes = birth.encode().unwrap();
    let mut changed = birth.selection.clone();
    changed.image_id[0] ^= 1;
    assert!(BirthRequest::decode(&bytes, &changed).is_err());
    changed = birth.selection.clone();
    changed.credential_generation = format!("CredOwner-{}", id(80));
    assert!(BirthRequest::decode(&bytes, &changed).is_err());
}

#[test]
fn station_lowbox_rejects_cross_lifetime_nonce_aliases() {
    let (birth, binding, _) = fixture();
    for nonce in binding.reserved_nonces().unwrap() {
        assert!(binding.validate_for(&birth.selection, nonce).is_err());
        let mut changed = birth.selection.clone();
        changed.logon = nonce.into();
        assert!(binding.validate_for(&changed, &birth.nonce).is_err());
        let mut changed = birth.selection.clone();
        changed.ready = nonce.into();
        assert!(binding.validate_for(&changed, &birth.nonce).is_err());
    }
    for nonce in &binding.reserved_nonces().unwrap()[..10] {
        let mut changed = binding.clone();
        changed.profile.owner_birth = (*nonce).into();
        assert!(changed.validate().is_err());
        changed = binding.clone();
        changed.profile.owner_resume = (*nonce).into();
        assert!(changed.validate().is_err());
        changed = binding.clone();
        changed.profile.use_nonce = (*nonce).into();
        assert!(changed.validate().is_err());
    }
    assert!(
        binding
            .validate_for(&birth.selection, &birth.selection.logon)
            .is_err()
    );
    assert!(
        binding
            .validate_for(&birth.selection, &birth.selection.ready)
            .is_err()
    );
}

#[test]
fn station_lowbox_codec_refuses_unknown_omitted_malformed_and_oversize_data() {
    let (birth, _, _) = fixture();
    let original = serde_json::to_value(&birth).unwrap();
    for case in 0..6 {
        let mut value = original.clone();
        let binding = &mut value["purpose"]["fixture_station_lowbox_v1"]["binding"];
        match case {
            0 => binding["desktop_path"] = serde_json::json!("WinSta0\\Default"),
            1 => {
                binding.as_object_mut().unwrap().remove("initializer");
            }
            2 => {
                binding["initializer"]["resources"]
                    .as_object_mut()
                    .unwrap()
                    .remove("namespace");
            }
            3 => binding["initializer"]["resources"]["namespace"] = serde_json::json!(123),
            4 => binding["initializer"]["subject"]["nonce"] = serde_json::json!("../not-a-nonce"),
            _ => binding["initializer"]["subject"]["owner"] = serde_json::json!("x".repeat(8192)),
        }
        assert!(
            BirthRequest::decode(&serde_json::to_vec(&value).unwrap(), &birth.selection).is_err(),
            "codec case {case}"
        );
    }
    let mut bytes = birth.encode().unwrap();
    bytes.resize(4608, b' ');
    assert!(BirthRequest::decode(&bytes, &birth.selection).is_ok());
    bytes.push(b' ');
    assert!(BirthRequest::decode(&bytes, &birth.selection).is_err());
    let mut bytes = birth.encode().unwrap();
    bytes.pop();
    assert!(BirthRequest::decode(&bytes, &birth.selection).is_err());
}
