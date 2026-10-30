//! Selection-bound suspended account birth. A distinct fixed self-check purpose
//! admits one durable resume exchange; no user command or fallback assignment.
use super::*;
use crate::launch::SpawnedChild;

#[path = "account_bootstrap_account_probe_wire.rs"]
pub(crate) mod probe;

#[cfg(test)]
#[path = "account_station_lowbox_binding.rs"]
mod station_lowbox;
#[cfg(test)]
pub(crate) use station_lowbox::{FixtureStationLowboxBinding, LowboxCompletion};

/// Fixed disposable workload data. It has no native authority and is checked
/// against the original owner's durable Using receipt before consumer resume.
#[cfg(test)]
#[derive(Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct FixtureWriterBinding {
    pub container: String,
    pub package_sid: String,
    pub folder: [u8; 24],
    pub owner_birth: String,
    pub owner_resume: String,
    pub owner_pid: u32,
    pub owner_created: u64,
    pub use_nonce: String,
    pub unsupported_registry: bool,
    pub hold_rounds: u32,
    pub descendant_files: bool,
}
#[cfg(test)]
impl FixtureWriterBinding {
    pub(crate) fn validate(&self) -> Result<()> {
        ensure!(
            matches!(self.hold_rounds, 0 | 12),
            "fixed writer lifetime outside fixture bounds"
        );
        ensure!(
            !self.descendant_files || (self.hold_rounds == 12 && !self.unsupported_registry),
            "fixed descendant workload binding differs"
        );
        crate::job::execution_name(&self.container)?;
        ensure!(
            crate::appcontainer::derive(&self.container)?.string()? == self.package_sid
                && self.folder != [0; 24]
                && self.owner_pid != 0
                && self.owner_created != 0,
            "fixed writer profile identity missing or different"
        );
        let nonces = [&self.owner_birth, &self.owner_resume, &self.use_nonce];
        for (index, nonce) in nonces.iter().enumerate() {
            validate_random_id(nonce)?;
            ensure!(
                !nonces[..index].contains(nonce),
                "fixed writer nonce reused"
            );
        }
        Ok(())
    }
    pub(crate) fn require_owner(
        &self,
        owner: &probe::Request,
        folder: [u8; 24],
        nonce: &str,
    ) -> Result<()> {
        self.validate()?;
        owner.validate()?;
        ensure!(
            owner.birth.purpose
                == Purpose::TrustedPairedProfileV1 {
                    container: self.container.clone(),
                    package_sid: self.package_sid.clone(),
                    policy: 1
                }
                && owner.birth.nonce == self.owner_birth
                && owner.nonce == self.owner_resume
                && owner.pid == self.owner_pid
                && owner.created == self.owner_created
                && self.folder == folder
                && self.use_nonce == nonce,
            "fixed writer differs from original owner/profile/use"
        );
        Ok(())
    }
}

#[derive(Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case", deny_unknown_fields)]
pub(crate) enum Purpose {
    ObserveOnly,
    TrustedSelfCheckV1,
    #[cfg(test)]
    FixturePrivateStationV1,
    #[cfg(test)]
    FixtureStationLowboxV1 {
        binding: FixtureStationLowboxBinding,
    },
    #[cfg(test)]
    FixtureProfileWriterV1 {
        binding: FixtureWriterBinding,
    },
    TrustedHandoffSelfCheckV1,
    TrustedUnusedProfileV1 {
        container: String,
        package_sid: String,
        policy: u32,
    },
    TrustedPairedProfileV1 {
        container: String,
        package_sid: String,
        policy: u32,
    },
    TrustedRpcSelfCheckV1 {
        package_sid: String,
        initializer: String,
    },
}
impl Purpose {
    // This fixed test route authenticates the original source, then requires a
    // second broker observation before any lowbox thread can resume.
    fn require_native_dispatch(&self) -> Result<()> {
        #[cfg(test)]
        if let Self::FixtureStationLowboxV1 { binding } = self {
            binding.validate()?;
        }
        Ok(())
    }
    pub(crate) fn is_paired_consumer(&self) -> bool {
        #[cfg(test)]
        if matches!(
            self,
            Self::FixtureProfileWriterV1 { .. } | Self::FixtureStationLowboxV1 { .. }
        ) {
            return true;
        }
        matches!(self, Self::TrustedSelfCheckV1)
    }
    pub(crate) fn is_self_check(&self) -> bool {
        #[cfg(test)]
        if matches!(self, Self::FixturePrivateStationV1) {
            return true;
        }
        #[cfg(test)]
        if matches!(
            self,
            Self::FixtureProfileWriterV1 { .. } | Self::FixtureStationLowboxV1 { .. }
        ) {
            return true;
        }
        matches!(
            self,
            Self::TrustedSelfCheckV1
                | Self::TrustedHandoffSelfCheckV1
                | Self::TrustedUnusedProfileV1 { .. }
                | Self::TrustedPairedProfileV1 { .. }
                | Self::TrustedRpcSelfCheckV1 { .. }
        )
    }
    pub(crate) fn uses_handoff(&self) -> bool {
        #[cfg(test)]
        if matches!(self, Self::FixturePrivateStationV1) {
            return true;
        }
        #[cfg(test)]
        if matches!(
            self,
            Self::FixtureProfileWriterV1 { .. } | Self::FixtureStationLowboxV1 { .. }
        ) {
            return true;
        }
        matches!(
            self,
            Self::TrustedHandoffSelfCheckV1
                | Self::TrustedUnusedProfileV1 { .. }
                | Self::TrustedPairedProfileV1 { .. }
        )
    }
    fn logon_flags(&self) -> CREATE_PROCESS_LOGON_FLAGS {
        if matches!(
            self,
            Self::TrustedUnusedProfileV1 { .. } | Self::TrustedPairedProfileV1 { .. }
        ) {
            LOGON_WITH_PROFILE
        } else {
            CREATE_PROCESS_LOGON_FLAGS(0)
        }
    }
}

#[derive(Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct BirthRequest {
    pub protocol: String,
    pub nonce: String,
    pub selection: Selection,
    pub purpose: Purpose,
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct BirthStatus {
    nonce: String,
    success: bool,
    hresult: Option<i32>,
    stage: BirthStage,
}

/// Fixed diagnostic categories only: no caller-controlled strings, credentials,
/// or paths cross this status channel. A stage never grants launch authority.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
enum BirthStage {
    Handoff,
    Admission,
    NativeLogon,
    TokenObservation,
    ImageObservation,
    RunnerPolicy,
    CreationIdentity,
    Complete,
}
impl BirthRequest {
    pub(crate) fn new(selection: Selection, nonce: String) -> Result<Self> {
        let request = Self {
            protocol: "alysis-suspended-account-birth-v2".into(),
            purpose: Purpose::ObserveOnly,
            nonce,
            selection,
        };
        request.validate()?;
        Ok(request)
    }
    pub(crate) fn validate(&self) -> Result<()> {
        self.selection.validate()?;
        validate_random_id(&self.nonce)?;
        #[cfg(test)]
        if let Purpose::FixtureStationLowboxV1 { binding } = &self.purpose {
            binding.validate_for(&self.selection, &self.nonce)?;
        }
        #[cfg(test)]
        if let Purpose::FixtureProfileWriterV1 { binding } = &self.purpose {
            binding.validate()?;
            for nonce in [
                &binding.owner_birth,
                &binding.owner_resume,
                &binding.use_nonce,
            ] {
                ensure!(
                    ![
                        &self.nonce,
                        &self.selection.operation,
                        &self.selection.logon,
                        &self.selection.ready
                    ]
                    .contains(&nonce),
                    "writer binding nonce overlaps consumer birth"
                );
            }
        }
        if let Purpose::TrustedUnusedProfileV1 {
            container,
            package_sid,
            policy,
        }
        | Purpose::TrustedPairedProfileV1 {
            container,
            package_sid,
            policy,
        } = &self.purpose
        {
            crate::job::execution_name(container)?;
            ensure!(
                *policy == 1 && crate::appcontainer::derive(container)?.string()? == *package_sid,
                "unused account profile policy or package differs"
            );
        }
        if let Purpose::TrustedRpcSelfCheckV1 {
            package_sid,
            initializer,
        } = &self.purpose
        {
            // Read-only diagnostic subject; never a native handle or authority.
            let _sid = crate::sid::LocalPsid::from_string(package_sid)?;
            ensure!(
                package_sid.starts_with("S-1-15-2-") && package_sid.len() <= 184,
                "RPC probe package identity differs"
            );
            validate_random_id(initializer)?;
            ensure!(
                ![
                    &self.nonce,
                    &self.selection.operation,
                    &self.selection.logon,
                    &self.selection.ready
                ]
                .contains(&initializer),
                "RPC probe initializer nonce overlaps birth"
            );
        }
        ensure!(
            self.protocol == "alysis-suspended-account-birth-v2"
                && ![
                    &self.selection.operation,
                    &self.selection.logon,
                    &self.selection.ready
                ]
                .contains(&&self.nonce),
            "account birth operation differs"
        );
        Ok(())
    }
    pub(crate) fn encode(&self) -> Result<Vec<u8>> {
        self.validate()?;
        let bytes = serde_json::to_vec(self)?;
        ensure!(bytes.len() <= 4608, "account birth request exceeds bounds");
        Ok(bytes)
    }
    fn decode(bytes: &[u8], selected: &Selection) -> Result<Self> {
        ensure!(bytes.len() <= 4608, "account birth request exceeds bounds");
        let request: Self = serde_json::from_slice(bytes)
            .map_err(|_| anyhow::anyhow!("invalid account birth request"))?;
        request.validate()?;
        ensure!(
            &request.selection == selected,
            "account birth selected a different credential"
        );
        Ok(request)
    }
    pub(crate) fn committed(&self, pid: u32, created: u64) -> Result<Vec<u8>> {
        ensure!(pid != 0 && created != 0, "account birth identity missing");
        let mut frame = b"account-birth-committed:".to_vec();
        frame.extend_from_slice(&self.encode()?);
        frame.extend_from_slice(&pid.to_le_bytes());
        frame.extend_from_slice(&created.to_le_bytes());
        Ok(frame)
    }
    pub(crate) fn require_status(&self, frame: &[u8]) -> Result<()> {
        ensure!(frame.len() <= 512, "account birth status exceeds bounds");
        let status: BirthStatus = serde_json::from_slice(frame)
            .map_err(|_| anyhow::anyhow!("invalid account birth status"))?;
        ensure!(
            status.nonce == self.nonce,
            "account birth status operation differs"
        );
        if status.success {
            ensure!(
                status.hresult.is_none() && status.stage == BirthStage::Complete,
                "successful account birth reported a failure"
            );
            return Ok(());
        }
        if let Some(value) = status.hresult {
            ensure!(value < 0, "invalid native failure status");
            return Err(windows::core::Error::from_hresult(windows::core::HRESULT(
                value,
            )))
            .with_context(|| format!("source account birth refused at {:?}", status.stage));
        }
        anyhow::bail!(
            "source account birth admission refused at {:?}",
            status.stage
        )
    }
    pub(crate) fn profile_load_wait(&self) -> Result<std::time::Duration> {
        self.validate()?;
        ensure!(
            matches!(
                self.purpose,
                Purpose::TrustedUnusedProfileV1 { .. } | Purpose::TrustedPairedProfileV1 { .. }
            ),
            "extended birth wait requires the fixed loaded-profile purpose"
        );
        Ok(std::time::Duration::from_secs(60))
    }
}

struct WidePassword(Vec<u16>);
impl Drop for WidePassword {
    fn drop(&mut self) {
        for unit in &mut self.0 {
            unsafe {
                std::ptr::write_volatile(unit, 0);
            }
        }
        std::sync::atomic::compiler_fence(std::sync::atomic::Ordering::SeqCst);
    }
}

pub(super) fn hold_suspended(received: ReceivedCredential<'_>, frame: &[u8]) -> Result<()> {
    let ReceivedCredential {
        password,
        selection,
        mut channel,
        image,
    } = received;
    let request = BirthRequest::decode(frame, &selection)?;
    let mut stage = BirthStage::Handoff;
    let result = (|| {
        // Validate the fixed test subject before creating its handoff pipe;
        // the later original-child observation still gates its lowbox resume.
        request.purpose.require_native_dispatch()?;
        let handoff = crate::account_control::handoff::Source::prepare(&request, image)?;
        let child = create_suspended(password, &request, image, &mut stage)?;
        stage = BirthStage::Complete;
        Ok::<_, anyhow::Error>((handoff, child))
    })();
    let status = BirthStatus {
        nonce: request.nonce.clone(),
        success: result.is_ok(),
        hresult: result.as_ref().err().and_then(|e| {
            e.chain()
                .find_map(|c| c.downcast_ref::<windows::core::Error>())
                .map(|e| e.code().0)
        }),
        stage,
    };
    channel.send(&serde_json::to_vec(&status)?, TIMEOUT)?;
    let (handoff, child) = match result {
        Ok(value) => value,
        Err(_) => {
            let _refused = FrameStorage(channel.receive(TIMEOUT)?);
            anyhow::bail!("account birth refused; no retry");
        }
    };
    let pending = super::super::super::pull::PendingProcessOffer::send_bound(
        &child, channel, &request, TIMEOUT,
    )?;
    // Receipt acknowledgment keeps the original child armed and suspended.
    pending.wait_for_birth_commit(&request, image, handoff, TIMEOUT)?;
    drop(child);
    Ok(())
}

fn create_suspended(
    password: PasswordFrame,
    request: &BirthRequest,
    image: &PinnedRunnerImage,
    stage: &mut BirthStage,
) -> Result<SpawnedChild> {
    *stage = BirthStage::Admission;
    request.purpose.require_native_dispatch()?;
    request.validate()?;
    let selection = &request.selection;
    selection.require_recipient(&selection.ready, image)?;
    password.validate(&selection)?;
    crate::account_impersonation::require_process_identity()?;
    ensure!(
        crate::user::local_user_sid(&selection.account.account_name)?.as_deref()
            == Some(&selection.account.account_sid),
        "selected account native identity changed before birth"
    );
    ensure!(
        crate::user::local_group_sid(crate::account_install::ACCOUNT_GROUP)?.as_deref()
            == Some(&selection.account.group_sid),
        "selected account group changed before birth"
    );
    let app = wstr(
        image
            .application()?
            .to_str()
            .context("account image is not Unicode")?,
    );
    // Purpose is fixed BEFORE native birth and echoed in every receipt/frame.
    let arguments = match &request.purpose {
        #[cfg(test)]
        Purpose::FixtureStationLowboxV1 { .. } => probe::entry_arguments(),
        #[cfg(test)]
        Purpose::FixturePrivateStationV1 => probe::entry_arguments(),
        #[cfg(test)]
        Purpose::FixtureProfileWriterV1 { .. } => probe::entry_arguments(),
        Purpose::ObserveOnly => format!(
            "{} candidate-account-child",
            crate::cli::SRT_WIN_DISPATCH_ARG1
        ),
        Purpose::TrustedSelfCheckV1
        | Purpose::TrustedHandoffSelfCheckV1
        | Purpose::TrustedRpcSelfCheckV1 { .. }
        | Purpose::TrustedPairedProfileV1 { .. }
        | Purpose::TrustedUnusedProfileV1 { .. } => probe::entry_arguments(),
    };
    let mut command = wstr(&format!(
        "{} {arguments}",
        crate::launch::quote_arg(image.application()?.to_str().unwrap())
    ));
    let directory = os_directory(false)?;
    let windows = os_directory(true)?;
    let mut values = vec![
        ("SystemRoot".into(), windows.clone()),
        ("WINDIR".into(), windows),
        ("PATH".into(), directory.clone()),
    ];
    if request.purpose.is_self_check() {
        values.push((
            probe::ENVIRONMENT_KEY.into(),
            String::from_utf8(request.encode()?)?,
        ));
    }
    let mut environment = crate::launch::build_env_block_with_inheritance(&values, false);
    let startup = STARTUPINFOW {
        cb: std::mem::size_of::<STARTUPINFOW>() as u32,
        ..Default::default()
    };
    let mut wide = WidePassword(Vec::with_capacity(128));
    wide.0.extend(
        password.0.0[39..]
            .chunks_exact(2)
            .map(|p| u16::from_le_bytes([p[0], p[1]])),
    );
    wide.0.push(0);
    let mut info = PROCESS_INFORMATION::default();
    *stage = BirthStage::NativeLogon;
    let result = unsafe {
        CreateProcessWithLogonW(
            pcwstr(&wstr(&selection.account.account_name)),
            pcwstr(&wstr(".")),
            windows::core::PCWSTR(wide.0.as_ptr()),
            request.purpose.logon_flags(),
            pcwstr(&app),
            Some(windows::core::PWSTR(command.as_mut_ptr())),
            CREATE_SUSPENDED | CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT,
            Some(environment.as_mut_ptr().cast()),
            pcwstr(&wstr(&directory)),
            &startup,
            &mut info,
        )
    };
    // Own a successful native child before any fallible postcheck.
    let child = result.map(|()| SpawnedChild::new(info));
    drop(wide);
    drop(password);
    let child = child.context("create suspended dedicated account child")?;
    *stage = BirthStage::TokenObservation;
    let peer = ExpectedPeer::retain(child.process())?;
    *stage = BirthStage::ImageObservation;
    let peer = peer.with_image(image)?;
    *stage = BirthStage::RunnerPolicy;
    peer.token.require_dedicated_runner(
        &selection.account.account_sid,
        selection.session,
        &selection.account.group_sid,
    )?;
    *stage = BirthStage::CreationIdentity;
    let created = super::super::super::pull::creation_time(child.process())?;
    ensure!(
        created >= selection.bootstrap_created,
        "account child predates original bootstrap"
    );
    Ok(child)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn paired_profile_birth_has_distinct_validated_loaded_purpose() {
        let selection = super::super::tests::selected();
        let mut birth =
            BirthRequest::new(selection, "6123456789abcdef0123456789abcdef".into()).unwrap();
        let container = "Alysis.Execution.8123456789abcdef0123456789abcdef";
        birth.purpose = Purpose::TrustedPairedProfileV1 {
            container: container.into(),
            package_sid: crate::appcontainer::derive(container)
                .unwrap()
                .string()
                .unwrap(),
            policy: 1,
        };
        assert!(birth.purpose.is_self_check() && birth.purpose.uses_handoff());
        assert_eq!(birth.purpose.logon_flags(), LOGON_WITH_PROFILE);
        assert_eq!(
            birth.profile_load_wait().unwrap(),
            std::time::Duration::from_secs(60)
        );
        BirthRequest::decode(&birth.encode().unwrap(), &birth.selection).unwrap();
        for case in 0..3 {
            let mut wrong = birth.clone();
            if let Purpose::TrustedPairedProfileV1 {
                container,
                package_sid,
                policy,
            } = &mut wrong.purpose
            {
                match case {
                    0 => container.push('x'),
                    1 => *package_sid = "S-1-1-0".into(),
                    _ => *policy = 2,
                }
            }
            assert!(wrong.profile_load_wait().is_err());
            assert!(wrong.encode().is_err());
        }
    }
    #[test]
    fn account_profile_load_requires_explicit_valid_bound_purpose() {
        let selection = super::super::tests::selected();
        let mut birth =
            BirthRequest::new(selection, "6123456789abcdef0123456789abcdef".into()).unwrap();
        for purpose in [
            Purpose::ObserveOnly,
            Purpose::TrustedSelfCheckV1,
            Purpose::TrustedHandoffSelfCheckV1,
        ] {
            assert_eq!(purpose.logon_flags().0, 0);
            birth.purpose = purpose;
            assert!(birth.profile_load_wait().is_err());
        }
        let container = "Alysis.Execution.8123456789abcdef0123456789abcdef";
        birth.purpose = Purpose::TrustedUnusedProfileV1 {
            container: container.into(),
            package_sid: crate::appcontainer::derive(container)
                .unwrap()
                .string()
                .unwrap(),
            policy: 1,
        };
        birth.validate().unwrap();
        assert_eq!(birth.purpose.logon_flags(), LOGON_WITH_PROFILE);
        assert_eq!(
            birth.profile_load_wait().unwrap(),
            std::time::Duration::from_secs(60)
        );
        let (server, address) =
            ControlServer::prepare(&birth.selection.account.account_sid).unwrap();
        let _client = ControlChannel::connect(
            &address,
            ExpectedPeer::retain(unsafe { GetCurrentProcess() }).unwrap(),
        )
        .unwrap();
        let mut channel = server
            .accept(
                ExpectedPeer::retain(unsafe { GetCurrentProcess() }).unwrap(),
                std::time::Duration::from_secs(1),
            )
            .unwrap();
        assert!(channel.receive_loaded_profile_birth_status(&birth).is_err());
        assert!(
            channel
                .receive(std::time::Duration::from_millis(1))
                .unwrap_err()
                .to_string()
                .contains("permanently refused")
        );
        for case in 0..3 {
            let mut wrong = birth.clone();
            if let Purpose::TrustedUnusedProfileV1 {
                container,
                package_sid,
                policy,
            } = &mut wrong.purpose
            {
                match case {
                    0 => container.push('x'),
                    1 => *package_sid = "S-1-1-0".into(),
                    _ => *policy = 0,
                }
            }
            assert!(wrong.validate().is_err());
            assert!(wrong.profile_load_wait().is_err());
        }
    }
    #[test]
    fn account_birth_request_refuses_cross_credential_replay_and_unbounded_input() {
        let selected = super::super::tests::selected();
        let request =
            BirthRequest::new(selected.clone(), "6123456789abcdef0123456789abcdef".into()).unwrap();
        let encoded = request.encode().unwrap();
        BirthRequest::decode(&encoded, &selected).unwrap();
        for case in 0..5 {
            let mut wrong = request.clone();
            match case {
                0 => wrong.protocol.push('x'),
                1 => wrong.nonce = wrong.selection.logon.clone(),
                2 => {
                    wrong.selection.credential_generation =
                        "CredOwner-7123456789abcdef0123456789abcdef".into()
                }
                3 => wrong.selection.account.account_sid.push('1'),
                _ => wrong.selection.bootstrap_created += 1,
            }
            assert!(BirthRequest::decode(&serde_json::to_vec(&wrong).unwrap(), &selected).is_err());
        }
        let mut value = serde_json::to_value(&request).unwrap();
        value["password"] = serde_json::json!("synthetic-not-allowed");
        let error = BirthRequest::decode(&serde_json::to_vec(&value).unwrap(), &selected)
            .err()
            .unwrap();
        assert_eq!(error.to_string(), "invalid account birth request");
        assert!(BirthRequest::decode(&vec![b'x'; 4609], &selected).is_err());
        assert!(request.committed(0, 1).is_err());
        assert!(request.committed(1, 0).is_err());
        assert_ne!(
            request.committed(1, 2).unwrap(),
            request.committed(1, 3).unwrap()
        );
        let encode_status = |nonce: String, success: bool, hresult: Option<i32>| {
            serde_json::to_vec(&BirthStatus {
                nonce,
                success,
                hresult,
                stage: if success {
                    BirthStage::Complete
                } else {
                    BirthStage::NativeLogon
                },
            })
            .unwrap()
        };
        request
            .require_status(&encode_status(request.nonce.clone(), true, None))
            .unwrap();
        assert!(
            request
                .require_status(&encode_status(selected.logon.clone(), true, None))
                .is_err()
        );
        assert!(
            request
                .require_status(&encode_status(
                    request.nonce.clone(),
                    true,
                    Some(-2147024891)
                ))
                .is_err()
        );
        assert!(
            request
                .require_status(&encode_status(request.nonce.clone(), false, Some(0)))
                .is_err()
        );
        let error = request
            .require_status(&encode_status(
                request.nonce.clone(),
                false,
                Some(-2147024891),
            ))
            .err()
            .unwrap();
        assert_eq!(
            error
                .chain()
                .find_map(|c| c.downcast_ref::<windows::core::Error>())
                .unwrap()
                .code()
                .0,
            -2147024891
        );
        assert!(request.require_status(&vec![b'x'; 513]).is_err());
        let incomplete = BirthStatus {
            nonce: request.nonce.clone(),
            success: true,
            hresult: None,
            stage: BirthStage::NativeLogon,
        };
        assert!(
            request
                .require_status(&serde_json::to_vec(&incomplete).unwrap())
                .is_err()
        );
        let mut unexpected = serde_json::to_value(incomplete).unwrap();
        unexpected["stage"] = serde_json::json!("caller controlled diagnostic");
        assert!(
            request
                .require_status(&serde_json::to_vec(&unexpected).unwrap())
                .is_err()
        );
    }
}
