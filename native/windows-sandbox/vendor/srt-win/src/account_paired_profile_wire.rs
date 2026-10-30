//! Fixed authenticated owner protocol. Frames carry observations, never native
//! handles or cold cleanup authority. Both endpoints enforce the same sequence.
use super::lease::{UseLease, is_renewal};
use super::*;

#[derive(Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub(super) enum Phase {
    Create,
    Created,
    Use,
    Using,
    Capture,
    Captured,
    Remove,
    Removed,
}

#[derive(Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Message {
    pub request: probe::Request,
    pub phase: Phase,
    pub folder: Option<[u8; 24]>,
    pub use_nonce: Option<String>,
    pub consumer: Option<BirthRequest>,
    pub retirement: Option<String>,
}
impl Message {
    pub fn create(request: &probe::Request) -> Self {
        Self {
            request: request.clone(),
            phase: Phase::Create,
            folder: None,
            use_nonce: None,
            consumer: None,
            retirement: None,
        }
    }
    pub fn at(&self, phase: Phase) -> Self {
        Self {
            phase,
            ..self.clone()
        }
    }
    pub(super) fn validate(&self) -> Result<()> {
        subject(&self.request)?;
        ensure!(
            match self.phase {
                Phase::Create =>
                    self.folder.is_none()
                        && self.use_nonce.is_none()
                        && self.consumer.is_none()
                        && self.retirement.is_none(),
                Phase::Created =>
                    self.folder.is_some_and(|f| f != [0; 24])
                        && self.use_nonce.is_none()
                        && self.consumer.is_none()
                        && self.retirement.is_none(),
                _ =>
                    self.folder.is_some_and(|f| f != [0; 24])
                        && self.use_nonce.is_some()
                        && self.consumer.is_some()
                        && self.retirement.is_some()
                            == matches!(
                                self.phase,
                                Phase::Capture | Phase::Captured | Phase::Remove | Phase::Removed
                            ),
            },
            "paired profile frame shape differs"
        );
        if let (Some(nonce), Some(consumer)) = (&self.use_nonce, &self.consumer) {
            validate_random_id(nonce)?;
            consumer.validate()?;
            let owner = &self.request.birth;
            ensure!(
                consumer.purpose.is_paired_consumer()
                    && consumer.selection.account == owner.selection.account
                    && consumer.selection.operation == owner.selection.operation
                    && consumer.selection.session == owner.selection.session
                    && consumer.selection.image_id == owner.selection.image_id
                    && consumer.selection.credential_generation
                        == owner.selection.credential_generation
                    && consumer.selection.bootstrap_pid != owner.selection.bootstrap_pid,
                "paired profile consumer binding differs"
            );
            #[cfg(test)]
            if let Purpose::FixtureProfileWriterV1 { binding } = &consumer.purpose {
                binding.require_owner(
                    &self.request,
                    self.folder.context("writer folder missing")?,
                    nonce,
                )?;
            }
            #[cfg(test)]
            if let Purpose::FixtureStationLowboxV1 { binding } = &consumer.purpose {
                binding.require_owner(
                    &self.request,
                    self.folder.context("station probe owner folder missing")?,
                    nonce,
                )?;
            }
            let nonces = [
                &self.request.nonce,
                &owner.nonce,
                &owner.selection.logon,
                &owner.selection.operation,
                &owner.selection.ready,
                &consumer.nonce,
                &consumer.selection.logon,
                &consumer.selection.ready,
                nonce,
            ];
            for (i, item) in nonces.iter().enumerate() {
                ensure!(
                    !nonces[..i].contains(item),
                    "paired profile frame nonce reused"
                );
            }
            if let Some(retirement) = &self.retirement {
                validate_random_id(retirement)?;
                ensure!(
                    !nonces.contains(&retirement),
                    "paired profile retirement nonce reused"
                );
            }
        }
        Ok(())
    }
    pub fn wire(&self) -> Result<Vec<u8>> {
        self.validate()?;
        let mut bytes = b"alysis-paired-owner-profile-v1:".to_vec();
        bytes.extend_from_slice(&serde_json::to_vec(self)?);
        ensure!(bytes.len() <= 16384, "paired profile frame exceeds bound");
        Ok(bytes)
    }
    pub fn read(bytes: &[u8], request: &probe::Request, phase: Phase) -> Result<Self> {
        const PREFIX: &[u8] = b"alysis-paired-owner-profile-v1:";
        ensure!(
            bytes.len() <= 16384 && bytes.starts_with(PREFIX),
            "paired profile wire differs"
        );
        let event: Self = serde_json::from_slice(&bytes[PREFIX.len()..])
            .map_err(|_| anyhow::anyhow!("malformed paired profile frame"))?;
        event.validate()?;
        ensure!(
            &event.request == request && event.phase == phase,
            "paired profile frame subject/phase differs"
        );
        Ok(event)
    }
    pub fn follows(&self, previous: &Self) -> Result<()> {
        self.validate()?;
        previous.validate()?;
        ensure!(
            self.request == previous.request,
            "paired profile original request changed"
        );
        let expected = match (previous.phase, self.phase) {
            (Phase::Create, Phase::Created) => Self {
                folder: self.folder,
                ..previous.at(self.phase)
            },
            (Phase::Created, Phase::Use) => Self {
                use_nonce: self.use_nonce.clone(),
                consumer: self.consumer.clone(),
                ..previous.at(self.phase)
            },
            (Phase::Using, Phase::Capture) => Self {
                retirement: self.retirement.clone(),
                ..previous.at(self.phase)
            },
            (Phase::Use, Phase::Using)
            | (Phase::Capture, Phase::Captured)
            | (Phase::Captured, Phase::Remove)
            | (Phase::Remove, Phase::Removed) => previous.at(self.phase),
            _ => anyhow::bail!("paired profile wire sequence differs"),
        };
        ensure!(
            *self == expected,
            "paired profile original storage/use binding changed"
        );
        Ok(())
    }
    pub fn event(&self) -> Result<Event> {
        self.validate()?;
        let request = self.request.clone();
        if self.phase == Phase::Create {
            return Ok(Event::CreateIntent { request });
        }
        let folder = self.folder.context("paired profile folder missing")?;
        if self.phase == Phase::Created {
            return Ok(Event::Created { request, folder });
        }
        let nonce = self
            .use_nonce
            .clone()
            .context("paired profile use nonce missing")?;
        let consumer = self
            .consumer
            .clone()
            .context("paired profile consumer missing")?;
        if self.phase == Phase::Use {
            return Ok(Event::UseIntent {
                request,
                folder,
                nonce,
                consumer,
            });
        }
        if self.phase == Phase::Using {
            return Ok(Event::Using {
                request,
                folder,
                nonce,
                consumer,
            });
        }
        let retirement = self
            .retirement
            .clone()
            .context("paired profile retirement missing")?;
        Ok(match self.phase {
            Phase::Capture => Event::CaptureIntent {
                request,
                folder,
                nonce,
                consumer,
                retirement,
            },
            Phase::Captured => Event::Captured {
                request,
                folder,
                nonce,
                consumer,
                retirement,
            },
            Phase::Remove => Event::RemoveIntent {
                request,
                folder,
                nonce,
                consumer,
                retirement,
            },
            Phase::Removed => Event::Removed {
                request,
                folder,
                nonce,
                consumer,
                retirement,
            },
            _ => anyhow::bail!("invalid paired profile journal frame"),
        })
    }
}

pub(crate) fn relay(
    broker: &mut ControlChannel<'_>,
    account: &mut ControlChannel<'_>,
    request: &probe::Request,
    timeout: Duration,
) -> Result<()> {
    subject(request)?;
    let mut previous = None;
    let mut use_lease = None;
    for (phase, reply) in [
        (Phase::Create, Phase::Created),
        (Phase::Use, Phase::Using),
        (Phase::Capture, Phase::Captured),
        (Phase::Remove, Phase::Removed),
    ] {
        let bytes = loop {
            let bytes = broker.receive(timeout)?;
            if phase != Phase::Capture || !is_renewal(&bytes) {
                break bytes;
            }
            let lease: &mut UseLease = use_lease
                .as_mut()
                .context("paired profile original use lease missing")?;
            let acknowledgment = lease.accept(&bytes)?;
            // Forward the authenticated original bytes. The relay may not ACK
            // on the owner's behalf or renew its total lifetime.
            account.send(&bytes, timeout)?;
            let confirmed = account.receive(timeout)?;
            ensure!(
                confirmed == acknowledgment,
                "paired profile owner lease acknowledgment differs"
            );
            lease.require_current()?;
            broker.send(&confirmed, timeout)?;
            lease.require_current()?;
        };
        let event = Message::read(&bytes, request, phase)?;
        if let Some(previous) = &previous {
            event.follows(previous)?;
        }
        if phase == Phase::Capture {
            use_lease
                .as_ref()
                .context("paired profile original use lease missing")?
                .require_current()?;
        }
        account.send(&event.wire()?, timeout)?;
        let receipt = Message::read(&account.receive(timeout)?, request, reply)?;
        receipt.follows(&event)?;
        if reply == Phase::Using {
            use_lease = Some(UseLease::new(&receipt)?);
        }
        broker.send(&receipt.wire()?, timeout)?;
        previous = Some(receipt);
    }
    let confirmed = Message::read(&broker.receive(timeout)?, request, Phase::Removed)?;
    ensure!(
        Some(&confirmed) == previous.as_ref(),
        "paired removal commit differs"
    );
    // The account remains alive until the broker has durably committed Removed.
    account.send(&confirmed.wire()?, timeout)
}

pub(crate) fn client(
    channel: &mut ControlChannel<'_>,
    request: &probe::Request,
    diagnostic_phase: &mut u32,
) -> Result<()> {
    let container = subject(request)?;
    let selected = &request.birth.selection;
    let verify = || -> Result<()> {
        crate::account_impersonation::require_process_identity()?;
        let peer = crate::account_control::ExpectedPeer::retain(unsafe {
            windows::Win32::System::Threading::GetCurrentProcess()
        })?;
        peer.require_profile_runner(
            &selected.account.account_sid,
            selected.session,
            &selected.account.group_sid,
        )?;
        ensure!(
            std::process::id() == request.pid
                && process_created(unsafe {
                    windows::Win32::System::Threading::GetCurrentProcess()
                })? == request.created,
            "paired profile original actor changed"
        );
        Ok(())
    };
    let timeout = Duration::from_secs(10);
    Message::read(&channel.receive(timeout)?, request, Phase::Create)?;
    *diagnostic_phase = 25;
    verify()?;
    crate::account_profile_registry::require_loaded_actor_classes(&selected.account.account_sid)?;
    let mut native = NativeHostProfile::default();
    *diagnostic_phase = 26;
    let folder = native.create_host(container, &selected.account.account_sid)?;
    let created = Message {
        folder: Some(folder),
        ..Message::create(request).at(Phase::Created)
    };
    verify()?;
    channel.send(&created.wire()?, timeout)?;
    let use_intent = Message::read(&channel.receive(timeout)?, request, Phase::Use)?;
    use_intent.follows(&created)?;
    *diagnostic_phase = 27;
    verify()?;
    let used = native.begin_use()?;
    used.verify()?;
    let using = use_intent.at(Phase::Using);
    let mut lease = UseLease::new(&using)?;
    channel.send(&using.wire()?, timeout)?;
    let capture = loop {
        let bytes = channel.receive(timeout)?;
        if is_renewal(&bytes) {
            let acknowledgment = lease.accept(&bytes)?;
            // Verify the original actor, not mutable profile contents. The
            // pulse carries no resource effect or cleanup authority.
            verify()?;
            lease.require_current()?;
            channel.send(&acknowledgment, timeout)?;
            lease.require_current()?;
            continue;
        }
        let capture = Message::read(&bytes, request, Phase::Capture)?;
        capture.follows(&using)?;
        lease.require_current()?;
        break capture;
    };
    *diagnostic_phase = 28;
    verify()?;
    lease.require_current()?;
    // Only the authenticated live broker can issue this frame, after consuming
    // the original CompletedConsumers proof. This owner retains original roots.
    let mut native = used.capture().map_err(|error| {
        #[cfg(test)]
        if let Some(BirthRequest {
            purpose: Purpose::FixtureProfileWriterV1 { binding },
            ..
        }) = &capture.consumer
        {
            if binding.unsupported_registry
                && crate::account_profile_registry::fixture_is_writer_link_refusal(
                    &error,
                    &binding.container,
                    &binding.use_nonce,
                )
            {
                *diagnostic_phase = 33;
            }
        }
        error
    })?;
    let captured = capture.at(Phase::Captured);
    verify()?;
    channel.send(&captured.wire()?, timeout)?;
    let remove = Message::read(&channel.receive(timeout)?, request, Phase::Remove)?;
    remove.follows(&captured)?;
    *diagnostic_phase = 29;
    verify()?;
    native.verify_unused()?;
    native.close_original_trees()?;
    *diagnostic_phase = 30;
    native.delete_original()?;
    *diagnostic_phase = 31;
    native.verify_absent()?;
    verify()?;
    let removed = remove.at(Phase::Removed);
    channel.send(&removed.wire()?, timeout)?;
    let confirm = Message::read(&channel.receive(timeout)?, request, Phase::Removed)?;
    ensure!(
        confirm == removed,
        "paired profile final durable acknowledgment differs"
    );
    *diagnostic_phase = 32;
    verify()
}

#[cfg(test)]
mod tests {
    use super::*;
    fn messages() -> Vec<Message> {
        let setup = crate::account_install::synthetic_ready();
        let selected = Selection {
            protocol: Selection::protocol(),
            operation: random_id().unwrap(),
            logon: random_id().unwrap(),
            ready: random_id().unwrap(),
            bootstrap_pid: 500,
            bootstrap_created: 100,
            image_id: [2; 24],
            session: 1,
            account: setup.binding(AccountMode::Offline).unwrap(),
            credential_generation: setup.offline.unwrap().credential_generation.unwrap(),
        };
        let mut birth = BirthRequest::new(selected, random_id().unwrap()).unwrap();
        let container = format!("Alysis.Execution.{}", random_id().unwrap());
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
            nonce: random_id().unwrap(),
            pid: 501,
            created: 101,
        };
        let create = Message::create(&request);
        let created = Message {
            folder: Some([4; 24]),
            ..create.at(Phase::Created)
        };
        let mut consumer = birth;
        consumer.purpose = Purpose::TrustedSelfCheckV1;
        consumer.nonce = random_id().unwrap();
        consumer.selection.logon = random_id().unwrap();
        consumer.selection.ready = random_id().unwrap();
        consumer.selection.bootstrap_pid = 600;
        consumer.selection.bootstrap_created = 200;
        let used = Message {
            use_nonce: Some(random_id().unwrap()),
            consumer: Some(consumer),
            ..created.at(Phase::Use)
        };
        let using = used.at(Phase::Using);
        let capture = Message {
            retirement: Some(random_id().unwrap()),
            ..using.at(Phase::Capture)
        };
        let captured = capture.at(Phase::Captured);
        let remove = captured.at(Phase::Remove);
        let removed = remove.at(Phase::Removed);
        vec![
            create, created, used, using, capture, captured, remove, removed,
        ]
    }
    #[test]
    fn paired_station_probe_use_binds_original_owner_and_resource_subject() {
        use crate::account_control::bootstrap::entry::{
            credential_wire::child::{FixtureStationLowboxBinding, FixtureWriterBinding},
            initializer_wire::{self, paired},
        };
        let mut message = messages()[2].clone();
        let owner = &message.request;
        let Purpose::TrustedPairedProfileV1 {
            container,
            package_sid,
            ..
        } = &owner.birth.purpose
        else {
            unreachable!()
        };
        let profile = FixtureWriterBinding {
            container: container.clone(),
            package_sid: package_sid.clone(),
            folder: message.folder.unwrap(),
            owner_birth: owner.birth.nonce.clone(),
            owner_resume: owner.nonce.clone(),
            owner_pid: owner.pid,
            owner_created: owner.created,
            use_nonce: message.use_nonce.clone().unwrap(),
            unsupported_registry: false,
            hold_rounds: 0,
            descendant_files: false,
        };
        let selected = &message.consumer.as_ref().unwrap().selection;
        let initializer = paired::Request {
            subject: initializer_wire::Request {
                operation: selected.operation.clone(),
                nonce: random_id().unwrap(),
                bootstrap_pid: selected.bootstrap_pid,
                bootstrap_created: selected.bootstrap_created,
                container: profile.container.clone(),
                package_sid: profile.package_sid.clone(),
                owner: selected.account.owner_sid.clone(),
                session: selected.session,
                image_id: [8; 24],
                omit_profile_environment: false,
            },
            resources: paired::Resources {
                owner_job: random_id().unwrap(),
                consumer_job: random_id().unwrap(),
                namespace: random_id().unwrap(),
                host_profile: random_id().unwrap(),
                host_profile_use: random_id().unwrap(),
                station: random_id().unwrap(),
                desktop: random_id().unwrap(),
            },
        };
        message.consumer.as_mut().unwrap().purpose = Purpose::FixtureStationLowboxV1 {
            binding: FixtureStationLowboxBinding {
                completion: Default::default(),
                profile,
                initializer,
                policy: 1,
            },
        };
        message.wire().unwrap();
        message.consumer.as_ref().unwrap().encode().unwrap();
        for field in 0..4 {
            let mut changed = message.clone();
            match field {
                0 => changed.folder = Some([9; 24]),
                1 => changed.use_nonce = Some(random_id().unwrap()),
                2 => changed.request.pid += 1,
                _ => {
                    let Purpose::FixtureStationLowboxV1 { binding } =
                        &mut changed.consumer.as_mut().unwrap().purpose
                    else {
                        unreachable!()
                    };
                    binding.initializer.subject.bootstrap_created += 1;
                }
            }
            assert!(
                changed.wire().is_err(),
                "station use accepted changed field {field}"
            );
        }
    }
    #[test]
    fn paired_profile_writer_wire_requires_exact_original_owner_folder_and_use() {
        use crate::account_control::bootstrap::entry::credential_wire::child::FixtureWriterBinding;
        let mut message = messages()[2].clone();
        let owner = &message.request;
        let Purpose::TrustedPairedProfileV1 {
            container,
            package_sid,
            ..
        } = &owner.birth.purpose
        else {
            unreachable!()
        };
        let binding = FixtureWriterBinding {
            container: container.clone(),
            package_sid: package_sid.clone(),
            folder: message.folder.unwrap(),
            owner_birth: owner.birth.nonce.clone(),
            owner_resume: owner.nonce.clone(),
            owner_pid: owner.pid,
            owner_created: owner.created,
            use_nonce: message.use_nonce.clone().unwrap(),
            unsupported_registry: false,
            hold_rounds: 0,
            descendant_files: false,
        };
        message.consumer.as_mut().unwrap().purpose = Purpose::FixtureProfileWriterV1 {
            binding: binding.clone(),
        };
        message.wire().unwrap();
        message.consumer.as_ref().unwrap().encode().unwrap();
        for field in [
            "folder",
            "owner_birth",
            "owner_resume",
            "owner_pid",
            "owner_created",
            "use_nonce",
            "container",
            "package_sid",
        ] {
            let mut changed = binding.clone();
            match field {
                "folder" => changed.folder[0] ^= 1,
                "owner_birth" => changed.owner_birth = random_id().unwrap(),
                "owner_resume" => changed.owner_resume = random_id().unwrap(),
                "owner_pid" => changed.owner_pid += 1,
                "owner_created" => changed.owner_created += 1,
                "use_nonce" => changed.use_nonce = random_id().unwrap(),
                "container" => {
                    changed.container = format!("Alysis.Execution.{}", random_id().unwrap())
                }
                "package_sid" => changed.package_sid.push('1'),
                _ => unreachable!(),
            }
            let mut wrong = message.clone();
            wrong.consumer.as_mut().unwrap().purpose =
                Purpose::FixtureProfileWriterV1 { binding: changed };
            assert!(wrong.wire().is_err(), "{field}");
        }
        for nonce in [
            &binding.owner_birth,
            &binding.owner_resume,
            &binding.use_nonce,
        ] {
            let mut wrong = message.consumer.clone().unwrap();
            wrong.nonce = nonce.clone();
            assert!(wrong.validate().is_err());
        }
    }
    #[test]
    fn paired_profile_wire_binds_order_storage_consumer_and_retirement() {
        let messages = messages();
        for (index, message) in messages.iter().enumerate() {
            let copy =
                Message::read(&message.wire().unwrap(), &message.request, message.phase).unwrap();
            assert!(copy == *message);
            if index > 0 {
                message.follows(&messages[index - 1]).unwrap();
            }
            assert!(message.follows(message).is_err());
        }
        for index in 2..messages.len() {
            let mut changed = messages[index].clone();
            changed.folder = Some([5; 24]);
            assert!(changed.follows(&messages[index - 1]).is_err());
        }
        for index in [3, 5, 6, 7] {
            let mut changed = messages[index].clone();
            changed.use_nonce = Some(random_id().unwrap());
            assert!(changed.follows(&messages[index - 1]).is_err());
            let mut changed = messages[index].clone();
            changed.consumer.as_mut().unwrap().nonce = random_id().unwrap();
            assert!(changed.follows(&messages[index - 1]).is_err());
        }
        let mut wrong = messages[2].clone();
        wrong.consumer.as_mut().unwrap().purpose = Purpose::ObserveOnly;
        assert!(wrong.wire().is_err());
        let mut wrong = messages[2].clone();
        wrong.use_nonce = Some(wrong.request.nonce.clone());
        assert!(wrong.wire().is_err());
        let mut wrong = messages[7].clone();
        wrong.retirement = Some(random_id().unwrap());
        assert!(wrong.follows(&messages[6]).is_err());
    }
    #[test]
    fn paired_profile_wire_refuses_wrong_subject_oversize_and_unknown_fields() {
        let messages = messages();
        let message = &messages[0];
        let mut other = message.request.clone();
        other.nonce = random_id().unwrap();
        assert!(Message::read(&message.wire().unwrap(), &other, message.phase).is_err());
        assert!(Message::read(&vec![b'x'; 16385], &message.request, message.phase).is_err());
        let mut json = serde_json::to_value(message).unwrap();
        json["handle"] = serde_json::json!(42);
        let mut bytes = b"alysis-paired-owner-profile-v1:".to_vec();
        bytes.extend(serde_json::to_vec(&json).unwrap());
        assert!(Message::read(&bytes, &message.request, message.phase).is_err());
    }
}
