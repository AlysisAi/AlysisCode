//! Controller owns original process observation only, never either job.
use super::*;

struct Evidence {
    expected_policy: PolicyKind,
    expected_birth: RootBirthKind,
    root: PathBuf,
    point: String,
    passed: bool,
    stage: &'static str,
    kill_progress_age_ms: Option<u64>,
    kill_effect_ack_age_ms: Option<u128>,
}
impl Drop for Evidence {
    fn drop(&mut self) {
        let Some(temp) = std::env::var_os("RUNNER_TEMP") else {
            return;
        };
        let temp = PathBuf::from(temp);
        let read = |stage| {
            std::fs::File::open(self.root.join(format!("paired-profile-{stage}.json")))
                .ok()
                .and_then(|file| read_reported_proof(file, &self.point, stage).ok())
                .and_then(|bytes| serde_json::from_slice::<serde_json::Value>(&bytes).ok())
        };
        let report = serde_json::json!({"point":self.point,"controller_passed":self.passed,"controller_stage":self.stage,
            "original_observation":read("observe"),"original_armed":read("armed"),"original_proof":read("cut"),
            "original_family":read("family"),
            "controller_pinned_before_consumer_resume":self.passed && !created(&self.point) && !writer(&self.point),
            "original_four_pinned_before_consumer_resume":self.passed && !created(&self.point),
            "writer_family_pinned_while_alive":self.passed && writer(&self.point),
            "initialized_resource_prefixes_preserved":self.passed && initialized(&self.point),
            "station_grant_preserved_after_broker_death":self.passed && initialized(&self.point),
            "namespace_and_desktop_absent_after_original_references_closed":self.passed && initialized(&self.point),
            "initializer_stopped_before_writer":self.passed && initialized(&self.point),
            "initializer_reference_reconstructed":false,
            "writer_progress_observation_bound_ms":if writer(&self.point) {Some(3000)} else {None},
            "writer_clock_origin":if writer(&self.point) {Some("before-initial-writer-observation-ack")} else {None},
            "fresh_effect_ack_kill_bound_ms":if live_writer(&self.point) {None} else {Some(8000)},
            "live_writer_pre_receive_kill_bound_ms":if live_writer(&self.point) {Some(3000)} else {None},
            "kill_progress_age_ms":self.kill_progress_age_ms,"kill_effect_ack_age_ms":self.kill_effect_ack_age_ms,
            "descendant_registry_writes":false,"startup_bound_ms":60000,
            "observer_owns_job":false,"cold_rows_authorize_cleanup":false,"production_ready":false,
            "expected_rpc_policy":self.expected_policy,"expected_namespace_birth":self.expected_birth});
        if let Ok(bytes) = serde_json::to_vec_pretty(&report) {
            let _ = std::fs::write(
                temp.join(format!("alysis-account-birth-cut-{}.json", self.point)),
                bytes,
            );
        }
        if let Ok(file) = std::fs::File::open(self.root.join("paired-profile-broker.stderr.log")) {
            let mut bytes = Vec::new();
            if file.take(32768).read_to_end(&mut bytes).is_ok() {
                let _ = std::fs::write(
                    temp.join(format!("alysis-account-birth-broker-{}.log", self.point)),
                    bytes,
                );
            }
        }
    }
}
fn read_reported_proof(reader: impl Read, expected_point: &str, stage: &str) -> Result<Vec<u8>> {
    // The caller selects the cut before any bytes are read. A claimed point
    // cannot enlarge its own allowance, including while exporting failure data.
    let limit = proof_limit(expected_point)?;
    let mut bytes = Vec::new();
    reader.take(limit as u64).read_to_end(&mut bytes)?;
    eprintln!(
        "paired profile controller proof bytes: point={expected_point} stage={stage} bytes={} limit={limit}",
        bytes.len()
    );
    ensure!(bytes.len() < limit, "profile proof exceeds bound");
    #[derive(Deserialize)]
    struct Selection {
        point: String,
        stage: String,
    }
    let selection: Selection = serde_json::from_slice(&bytes)?;
    ensure!(
        selection.point == expected_point,
        "profile proof point differs"
    );
    ensure!(selection.stage == stage, "profile proof stage differs");
    // This envelope is diagnostic only. Controller admission still requires
    // the complete typed Proof and every existing resource-prefix check below.
    Ok(bytes)
}
fn read_proof(
    fixture: &Fixture,
    broker: &mut Broker,
    expected_point: &str,
    stage: &str,
    deadline: Instant,
    expected_policy: PolicyKind,
    expected_birth: RootBirthKind,
) -> Result<Proof> {
    let path = fixture.root.join(format!("paired-profile-{stage}.json"));
    while !path.exists() {
        ensure!(
            broker.0.try_wait()?.is_none(),
            "paired profile broker exited before {stage}; logs retained"
        );
        ensure!(
            Instant::now() < deadline,
            "paired profile {stage} timed out; logs retained"
        );
        std::thread::sleep(Duration::from_millis(10));
    }
    ensure!(
        Instant::now() < deadline,
        "paired profile proof exceeded deadline"
    );
    let bytes = read_reported_proof(std::fs::File::open(path)?, expected_point, stage)?;
    let proof: Proof = serde_json::from_slice(&bytes)?;
    proof
        .snapshot
        .require(expected_point, stage, expected_policy, expected_birth);
    require_writer_receipt(&proof)?;
    Ok(proof)
}
fn acknowledge(fixture: &Fixture, nonce: &str, stage: &str) -> Result<()> {
    let destination = fixture
        .root
        .join(format!("paired-profile-{stage}-ack.json"));
    ensure!(!destination.exists(), "profile acknowledgment reused");
    let part = destination.with_extension("part");
    std::fs::write(&part, nonce.as_bytes())?;
    std::fs::rename(part, destination)?;
    Ok(())
}
fn same_originals(before: &Proof, after: &Proof) -> Result<()> {
    let family_birth =
        writer(&before.point) && before.stage == "observe" && after.stage == "family";
    require_original_roles(&before.processes, &after.processes, family_birth)?;
    if family_birth {
        ensure!(
            before.writer_receipt.is_none()
                && before.progress_tick.is_none()
                && after.writer_receipt.is_some()
                && after.progress_tick.is_some(),
            "writer family replaced original roles or expanded incorrectly"
        );
    } else {
        ensure!(
            before.writer_receipt == after.writer_receipt
                && before.progress_tick == after.progress_tick,
            "profile original family or progress clock changed"
        );
    }
    ensure!(
        before.nonce == after.nonce
            && before.point == after.point
            && before.execution == after.execution
            && before.consumer_suspended_tick == after.consumer_suspended_tick
            && before.snapshot.plan == after.snapshot.plan
            && before.snapshot.account == after.snapshot.account,
        "profile replaced original observer identities or acknowledged accounts"
    );
    require_same_resources(&before.snapshot, &after.snapshot)?;
    // Immutable committed evidence may only extend, never replace either role.
    for (prior, later) in [
        (&before.snapshot.bootstrap, &after.snapshot.bootstrap),
        (&before.snapshot.resume, &after.snapshot.resume),
        (&before.snapshot.profile, &after.snapshot.profile),
    ] {
        ensure!(
            prior.iter().all(|r| later.iter().any(|v| v == r)),
            "profile committed evidence changed"
        );
    }
    Ok(())
}
fn require_same_resources(before: &Snapshot, after: &Snapshot) -> Result<()> {
    // Unlike the account profile journal, these complete prefixes must not
    // advance at any selected cut. Even an added cleanup intent is a failure.
    for (prior, later) in [
        (&before.namespace, &after.namespace),
        (&before.host_profile, &after.host_profile),
        (&before.station, &after.station),
        (&before.desktop, &after.desktop),
        (&before.initializer, &after.initializer),
    ] {
        ensure!(prior == later, "initialized writer resource prefix changed");
    }
    Ok(())
}
fn require_original_roles(
    before: &[(String, Reference)],
    after: &[(String, Reference)],
    expand: bool,
) -> Result<()> {
    ensure!(
        if expand {
            before.len() == 4
                && after.len() == 6
                && after[..4] == *before
                && after[4].0 == "consumer-child"
                && after[5].0 == "consumer-grandchild"
        } else {
            before == after
        },
        "profile original roles replaced or expanded incorrectly"
    );
    Ok(())
}
fn require_jobs(proof: &Proof, observed: &[OwnedHandle]) -> Result<()> {
    for consumer in [false, true] {
        let name =
            crate::job::paired_execution_name(&proof.execution.identity.container, consumer)?;
        let active = counts(&name).context("original profile job absent")?.0;
        let expected = if consumer && (created(&proof.point) || retired(&proof.point, &proof.stage))
        {
            0
        } else {
            if consumer && writer(&proof.point) && proof.stage != "observe" {
                4
            } else {
                2
            }
        };
        // Windows may add its console host to an original strict job. Pin and
        // verify the required role processes, without mistaking them for the
        // complete native inventory. Quiescence still requires exact zero.
        ensure!(
            if expected == 0 {
                active == 0
            } else {
                active >= expected
            },
            "profile job active count differs"
        );
        // Query-only, scoped and closed before forced broker death.
        let job =
            OwnedHandle(unsafe { OpenJobObjectW(JOB_OBJECT_QUERY, false, pcwstr(&wstr(&name)))? });
        for ((kind, _), process) in proof.processes.iter().zip(observed) {
            if retired(&proof.point, &proof.stage) && kind.starts_with("consumer-") {
                continue;
            }
            let mut member = windows::core::BOOL::default();
            unsafe {
                IsProcessInJob(process.raw(), Some(job.raw()), &mut member)?;
            }
            ensure!(
                member.as_bool() == kind.starts_with(if consumer { "consumer-" } else { "owner-" }),
                "profile process escaped or joined sibling job"
            );
        }
    }
    Ok(())
}

fn require_consumer_margin(proof: &Proof) -> Result<()> {
    ensure!(
        proof.consumer_suspended_tick.is_none() == created(&proof.point),
        "profile consumer timing proof differs"
    );
    if proof.point == POINTS[1] {
        let before_start = proof
            .consumer_suspended_tick
            .context("consumer source birth clock missing")?;
        let now = unsafe { windows::Win32::System::SystemInformation::GetTickCount64() };
        ensure!(
            now.checked_sub(before_start)
                .is_some_and(|elapsed| elapsed < 8000),
            "Using cut reached original consumer receive margin"
        );
    }
    if writer(&proof.point)
        && (proof.stage == "family" || (live_writer(&proof.point) && proof.stage != "observe"))
    {
        let now = unsafe { windows::Win32::System::SystemInformation::GetTickCount64() };
        require_writer_clock(
            proof
                .progress_tick
                .context("writer progress clock absent")?,
            now,
        )?;
    }
    Ok(())
}
fn require_writer_clock(tick: u64, now: u64) -> Result<()> {
    ensure!(
        now.checked_sub(tick).is_some_and(|elapsed| elapsed < 3000),
        "writer cut reached original progress receive margin"
    );
    Ok(())
}

fn require_writer_receipt(proof: &Proof) -> Result<()> {
    if !writer(&proof.point) || proof.stage == "observe" {
        ensure!(
            proof.writer_receipt.is_none() && proof.progress_tick.is_none(),
            "unexpected writer evidence"
        );
        ensure!(
            proof.writer_acknowledged_rounds == 0,
            "unexpected writer progress"
        );
        return Ok(());
    }
    let receipt = proof
        .writer_receipt
        .as_ref()
        .context("writer receipt absent")?;
    ensure!(
        proof.writer_acknowledged_rounds
            == if retired(&proof.point, &proof.stage) {
                12
            } else {
                0
            },
        "writer original progress count differs"
    );
    let request: crate::account_control::bootstrap::entry::credential_wire::child::probe::Request =
        serde_json::from_value(receipt["request"].clone())?;
    let offer: crate::account_control::writer_fixture::Offer =
        serde_json::from_value(receipt.clone())?;
    ensure!(
        &offer.fixture_proof(&request)? == receipt,
        "writer receipt is noncanonical"
    );
    let resumed: serde_json::Value = serde_json::from_str(&proof.snapshot.resume[0].2)?;
    ensure!(
        resumed["request"] == receipt["request"],
        "writer receipt differs from original resume"
    );
    ensure!(
        proof.processes.len() == 6 && proof.progress_tick.is_some(),
        "writer observation incomplete"
    );
    let mut pids = vec![proof.execution.pid];
    for (_, reference) in &proof.processes {
        ensure!(
            !pids.contains(&reference.pid),
            "writer process identities alias"
        );
        pids.push(reference.pid);
    }
    for (index, (_, reference)) in proof.processes[4..].iter().enumerate() {
        let (_, pid, created) = offer.references[index];
        // Receipt integers belong to the bootstrap table, not this broker.
        ensure!(
            pid == reference.pid && created == reference.created,
            "writer descendant original differs"
        );
    }
    Ok(())
}

pub(crate) fn run(fixture: Fixture, password: &str, point: &str) -> Result<()> {
    run_inner(fixture, password, point, Lifecycle::Baseline)
}
pub(crate) fn run_v3(fixture: Fixture, password: &str, point: &str) -> Result<()> {
    run_inner(fixture, password, point, Lifecycle::V3)
}
pub(crate) fn run_v3_shadow(fixture: Fixture, password: &str, point: &str) -> Result<()> {
    run_inner(fixture, password, point, Lifecycle::V3Shadow)
}
fn run_inner(fixture: Fixture, password: &str, point: &str, lifecycle: Lifecycle) -> Result<()> {
    lifecycle.require(point)?;
    let expected_policy = lifecycle.policy();
    let expected_birth = lifecycle.birth();
    let mut evidence = Evidence {
        expected_policy,
        expected_birth,
        root: fixture.root.clone(),
        point: point.into(),
        passed: false,
        stage: "controller-created",
        kill_progress_age_ms: None,
        kill_effect_ack_age_ms: None,
    };
    let nonce = random_id()?;
    std::fs::write(
        fixture.root.join("paired-profile-control.json"),
        serde_json::to_vec(&DiskCrashControl {
            setup: fixture.setup.clone(),
            checkpoint: point.into(),
            nonce: nonce.clone(),
        })?,
    )?;
    let mut broker = Broker(
        Command::new(std::env::current_exe()?)
            .args([
                lifecycle.broker_test(),
                "--exact",
                "--ignored",
                "--nocapture",
                "--test-threads=1",
            ])
            .env(ROOT, &fixture.root)
            .env(NONCE, &nonce)
            .env_remove("RUST_TEST_THREADS")
            .stdin(Stdio::piped())
            .stdout(std::fs::File::create_new(
                fixture.root.join("paired-profile-broker.stdout.log"),
            )?)
            .stderr(std::fs::File::create_new(
                fixture.root.join("paired-profile-broker.stderr.log"),
            )?)
            .spawn()?,
    );
    broker
        .0
        .stdin
        .take()
        .context("profile broker stdin absent")?
        .write_all(password.as_bytes())?;
    let original = HANDLE(broker.0.as_raw_handle());
    let original_created = process_created(original)?;
    let before = read_proof(
        &fixture,
        &mut broker,
        point,
        "observe",
        Instant::now() + Duration::from_secs(60),
        expected_policy,
        expected_birth,
    )?;
    ensure!(
        before.nonce == nonce
            && before.point == point
            && before.execution.pid == broker.0.id()
            && before.execution.created == original_created
            && before.execution.scope == PAIRED_PLAN_SCOPE,
        "profile original broker binding differs"
    );
    let expected = if created(point) {
        vec!["owner-bootstrap", "owner-account"]
    } else {
        vec![
            "owner-bootstrap",
            "owner-account",
            "consumer-bootstrap",
            "consumer-account",
        ]
    };
    ensure!(
        before
            .processes
            .iter()
            .map(|(k, _)| k.as_str())
            .collect::<Vec<_>>()
            == expected,
        "profile original roles differ"
    );
    let mut pids = before
        .processes
        .iter()
        .map(|(_, r)| r.pid)
        .collect::<Vec<_>>();
    pids.sort();
    pids.dedup();
    ensure!(
        pids.len() == expected.len() && pids.iter().all(|p| *p != broker.0.id()),
        "profile process identities alias"
    );
    let marker = std::fs::read(fixture.path())?;
    ensure!(
        serde_json::from_slice::<ExecutionRecord>(&marker)? == before.execution,
        "profile pending marker differs"
    );
    let ready = ReadyInstallation::fixture(fixture.setup.clone(), &fixture.root)?;
    let image = PinnedRunnerImage::prepare(&ready)?;
    let mut observed = before
        .processes
        .iter()
        .map(|(_, r)| original_reference(original, r, &image))
        .collect::<Vec<_>>();
    let plan: serde_json::Value = serde_json::from_str(&before.snapshot.plan)?;
    ensure!(
        plan["execution"] == serde_json::to_value(&before.execution)?,
        "profile plan execution differs"
    );
    for (role, label) in [(0, "owner"), (1, "consumer")] {
        if role == 1 && created(point) {
            continue;
        }
        let reference = |suffix: &str| {
            &before
                .processes
                .iter()
                .find(|(k, _)| k == &format!("{label}-{suffix}"))
                .unwrap()
                .1
        };
        let row = |table: &[(u32, u32, String)], sequence| -> Result<serde_json::Value> {
            Ok(serde_json::from_str(
                &table
                    .iter()
                    .find(|r| r.0 == role && r.1 == sequence)
                    .context("profile original birth row missing")?
                    .2,
            )?)
        };
        let bootstrap = row(&before.snapshot.bootstrap, 3)?;
        let account = row(&before.snapshot.account, 4)?;
        let selection = &account["request"]["selection"];
        ensure!(
            bootstrap["pid"] == reference("bootstrap").pid
                && bootstrap["created"] == reference("bootstrap").created
                && selection["bootstrap_pid"] == bootstrap["pid"]
                && selection["bootstrap_created"] == bootstrap["created"]
                && account["pid"] == reference("account").pid
                && account["created"] == reference("account").created,
            "profile original process references differ from durable births"
        );
        ensure!(
            bootstrap["image_id"] == plan["image_id"]
                && account["image_id"] == plan["image_id"]
                && selection["image_id"] == plan["image_id"]
                && selection["operation"] == plan["operation"],
            "profile original image or operation differs"
        );
        if role == 1 {
            ensure!(
                if writer(point) {
                    account["request"]["purpose"]["fixture_profile_writer_v1"]["binding"]["descendant_files"]
                        == true
                } else {
                    account["request"]["purpose"] == "trusted_self_check_v1"
                },
                "unexpected profile consumer purpose"
            );
        }
    }
    require_jobs(&before, &observed)?;
    require_consumer_margin(&before)?;
    acknowledge(&fixture, &nonce, "observe")?;
    let family = if writer(point) {
        let family = read_proof(
            &fixture,
            &mut broker,
            point,
            "family",
            Instant::now() + Duration::from_secs(10),
            expected_policy,
            expected_birth,
        )?;
        same_originals(&before, &family)?;
        for (_, reference) in &family.processes[4..] {
            let process = original_reference(original, reference, &image);
            let receipt = family.writer_receipt.as_ref().unwrap();
            let request: crate::account_control::bootstrap::entry::credential_wire::child::probe::Request =
                serde_json::from_value(receipt["request"].clone())?;
            let selected = request.birth.selection;
            crate::account_control::ExpectedPeer::retain(process.raw())?.require_profile_runner(
                &selected.account.account_sid,
                selected.session,
                &selected.account.group_sid,
            )?;
            observed.push(process);
        }
        require_jobs(&family, &observed)?;
        for ((kind, _), handle) in family.processes.iter().zip(&observed) {
            require_state(point, "family", kind, handle);
        }
        require_consumer_margin(&family)?;
        acknowledge(&fixture, &nonce, "family")?;
        Some(family)
    } else {
        None
    };
    let armed = read_proof(
        &fixture,
        &mut broker,
        point,
        "armed",
        Instant::now() + Duration::from_secs(if writer(point) { 20 } else { 10 }),
        expected_policy,
        expected_birth,
    )?;
    same_originals(family.as_ref().unwrap_or(&before), &armed)?;
    require_jobs(&armed, &observed)?;
    require_consumer_margin(&armed)?;
    for ((kind, _), handle) in armed.processes.iter().zip(&observed) {
        require_state(point, "armed", kind, handle);
    }
    // ACK precedes the selected native effect frame. Every cut follows its
    // successful receipt, so the next source/account receive starts AFTER this
    // instant. Earlier pipe timeouts can only fail the case, never earn a cut.
    // No deadline is extended: kill stays below those next ten-second receives.
    // Live writer uses its EARLIER pre-receive tick throughout; the following
    // eight-second bound cannot override that stricter three-second check.
    let began = Instant::now();
    acknowledge(&fixture, &nonce, "armed")?;
    let proof = read_proof(
        &fixture,
        &mut broker,
        point,
        "cut",
        began + Duration::from_secs(8),
        expected_policy,
        expected_birth,
    )?;
    same_originals(&armed, &proof)?;
    require_jobs(&proof, &observed)?;
    for ((kind, _), handle) in proof.processes.iter().zip(&observed) {
        require_state(point, "cut", kind, handle);
    }
    ensure!(
        began.elapsed() < Duration::from_secs(8),
        "profile pre-kill crossed receive margin"
    );
    require_consumer_margin(&proof)?;
    unsafe {
        TerminateProcess(original, 74)?;
    }
    evidence.kill_effect_ack_age_ms = Some(began.elapsed().as_millis());
    if live_writer(point) {
        evidence.kill_progress_age_ms = Some(
            unsafe { windows::Win32::System::SystemInformation::GetTickCount64() }
                .checked_sub(proof.progress_tick.context("writer kill clock absent")?)
                .context("writer kill clock moved backwards")?,
        );
    }
    ensure!(
        began.elapsed() < Duration::from_secs(8),
        "profile kill crossed receive margin"
    );
    require_consumer_margin(&proof)?;
    ensure!(
        unsafe { WaitForSingleObject(original, 5000) } == WAIT_OBJECT_0
            && broker.0.wait()?.code() == Some(74),
        "profile broker was not forcibly stopped"
    );
    let deadline = Instant::now() + Duration::from_secs(10);
    for handle in &observed {
        dead(handle);
    }
    ensure!(
        Instant::now() < deadline,
        "profile children exceeded death bound"
    );
    evidence.stage = "original-processes-exited";
    if initialized(point) {
        evidence.stage = "station-grant-check";
        let intent: serde_json::Value = serde_json::from_str(&proof.snapshot.station[0].1)?;
        let expected: crate::acl::object_grants::Snapshot =
            serde_json::from_value(intent["after"].clone())?;
        ensure!(
            crate::account_state::execution::resources::station::Station::fixture_snapshot_current(
            )? == expected,
            "initialized writer broker death changed the original station grant"
        );
        evidence.stage = "station-grant-preserved";
    }
    drop(observed);
    evidence.stage = "original-references-closed";
    for consumer in [false, true] {
        wait_absent(&crate::job::paired_execution_name(
            &proof.execution.identity.container,
            consumer,
        )?);
    }
    evidence.stage = "original-jobs-absent";
    if initialized(point) {
        // A controller-held process/token reference can itself keep native
        // objects alive. All six original process pins are closed above.
        // These Plan-only observations grant no reconstruction or cleanup.
        let plan: paired_plan::Plan = serde_json::from_str(&proof.snapshot.plan)?;
        plan.fixture_require_rpc_policy(expected_policy)?;
        plan.fixture_require_namespace_birth(expected_birth)?;
        evidence.stage = "namespace-absence";
        paired_plan::native::namespace::fixture_require_absent_plan(&plan)?;
        evidence.stage = "desktop-absence";
        paired_plan::native::namespace::station::desktop::fixture_require_absent_plan(&plan)?;
    }
    evidence.stage = "cold-or-hot-prefix-check";
    if hot(point) {
        verify_hot(&fixture, &proof, &marker, expected_policy, expected_birth)?;
    } else {
        let path =
            crate::account_state::database_path(&fixture.root, &proof.execution.identity.binding)?;
        let original_bytes = std::fs::read(&path)?;
        let manifest = std::fs::read(path.with_file_name("identity.json"))?;
        let db = protected_database(&fixture);
        ensure!(
            db.file_identity() == proof.execution.database_id,
            "profile cold database identity changed"
        );
        let cold = Snapshot::read(db.connection()?);
        cold.require_resources(point, expected_policy, expected_birth);
        assert_eq!(cold, proof.snapshot);
        drop(db);
        ensure!(
            std::fs::read(&path)? == original_bytes
                && std::fs::read(path.with_file_name("identity.json"))? == manifest
                && !path.with_extension("db-journal").exists(),
            "profile cold evidence changed"
        );
    }
    ensure!(
        std::fs::read(fixture.path())? == marker,
        "profile crash claimed cleanup"
    );
    evidence.stage = "complete";
    evidence.passed = true;
    Ok(())
}

fn verify_hot(
    fixture: &Fixture,
    proof: &Proof,
    marker: &[u8],
    expected_policy: PolicyKind,
    expected_birth: RootBirthKind,
) -> Result<()> {
    ensure!(hot(&proof.point), "profile cut is not a hot receipt");
    let path =
        crate::account_state::database_path(&fixture.root, &proof.execution.identity.binding)?;
    let journal = path.with_extension("db-journal");
    let original = std::fs::read(&path)?;
    let rollback = std::fs::read(&journal)?;
    let manifest = std::fs::read(path.with_file_name("identity.json"))?;
    ensure!(
        original.len() <= 1024 * 1024 && rollback.len() > 512 && rollback.len() <= 1024 * 1024,
        "profile hot files exceed bound"
    );
    ensure!(
        original != std::fs::read(fixture.root.join("paired-profile-before-write.db"))?
            && rollback[..8] == [0xd9, 0xd5, 0x05, 0xf9, 0x20, 0xa1, 0x63, 0xd7],
        "profile receipt did not create hot journal"
    );
    let unchanged = || -> Result<()> {
        ensure!(
            std::fs::read(&path)? == original
                && std::fs::read(&journal)? == rollback
                && std::fs::read(path.with_file_name("identity.json"))? == manifest
                && std::fs::read(fixture.path())? == marker,
            "profile original hot evidence changed"
        );
        Ok(())
    };
    for _ in 0..2 {
        let error =
            PrivateDatabase::open_under(&fixture.root, &fixture.setup, AccountMode::Offline, None)
                .err()
                .context("profile hot original was admitted")?;
        ensure!(
            format!("{error:#}").contains("auxiliary or unknown object"),
            "profile hot original refused for different reason"
        );
        unchanged()?;
    }
    let root = fixture.root.join("paired-profile-rollback-oracle");
    std::fs::create_dir(&root)?;
    let copy = root.join("state.db");
    std::fs::write(&copy, &original)?;
    std::fs::write(copy.with_extension("db-journal"), &rollback)?;
    let sql = Connection::open_with_flags_and_vfs(
        fs::extended_path(&copy)?,
        rusqlite::OpenFlags::SQLITE_OPEN_READ_WRITE,
        "win32-longpath",
    )?;
    ensure!(
        sql.query_row("PRAGMA integrity_check", [], |r| r.get::<_, String>(0))? == "ok",
        "profile rollback copy corrupt"
    );
    crate::account_state::validate_header(&sql, &proof.execution.identity.binding)?;
    let recovered = Snapshot::read(&sql);
    recovered.require_resources(&proof.point, expected_policy, expected_birth);
    assert_eq!(recovered, proof.snapshot);
    drop(sql);
    ensure!(
        !copy.with_extension("db-journal").exists(),
        "profile copied rollback incomplete"
    );
    unchanged()
}

#[test]
fn writer_clock_refuses_boundary_and_backward_time() {
    assert!(require_writer_clock(100, 3099).is_ok());
    assert!(require_writer_clock(100, 3100).is_err());
    assert!(require_writer_clock(100, 99).is_err());
}

fn synthetic_roles(family: bool) -> Vec<(String, Reference)> {
    [
        "owner-bootstrap",
        "owner-account",
        "consumer-bootstrap",
        "consumer-account",
        "consumer-child",
        "consumer-grandchild",
    ]
    .iter()
    .take(if family { 6 } else { 4 })
    .enumerate()
    .map(|(i, kind)| {
        (
            (*kind).into(),
            Reference {
                handle: i as u64 + 1,
                pid: i as u32 + 100,
                created: i as u64 + 1000,
            },
        )
    })
    .collect()
}

#[test]
fn original_roles_allow_only_one_exact_family_expansion() {
    let before = synthetic_roles(false);
    let family = synthetic_roles(true);
    assert!(require_original_roles(&before, &family, true).is_ok());
    assert!(require_original_roles(&family, &family, false).is_ok());
    assert!(require_original_roles(&family, &family, true).is_err());
    assert!(require_original_roles(&before, &family, false).is_err());
    assert!(require_original_roles(&family, &before, false).is_err());
}

#[test]
fn original_roles_refuse_replacement_and_descendant_reordering() {
    let before = synthetic_roles(false);
    for index in 0..6 {
        let mut family = synthetic_roles(true);
        family[index].1.handle += 50;
        assert!(require_original_roles(&synthetic_roles(true), &family, false).is_err());
        if index < 4 {
            assert!(require_original_roles(&before, &family, true).is_err());
        }
    }
    let mut family = synthetic_roles(true);
    family.swap(4, 5);
    assert!(require_original_roles(&before, &family, true).is_err());
}

#[test]
fn writer_cut_states_keep_live_use_and_uncommitted_receipts_distinct() {
    for point in &POINTS[4..] {
        assert_eq!(counts_for(point, "observe"), (4, [7, 7], 0));
        assert_eq!(counts_for(point, "family"), (6, [7, 7], 2));
    }
    for points in [&POINTS[4..7], &POINTS[7..10]] {
        assert_eq!(counts_for(points[0], "cut"), (6, [7, 7], 2));
        assert_eq!(counts_for(points[1], "cut"), (7, [7, 9], 3));
        assert_eq!(counts_for(points[2], "cut"), (9, [7, 9], 3));
        assert_eq!(counts_for(points[0], "armed"), (6, [7, 7], 2));
        assert_eq!(counts_for(points[1], "armed"), (6, [7, 9], 3));
        assert_eq!(counts_for(points[2], "armed"), (8, [7, 9], 3));
        assert!(!hot(points[0]) && hot(points[1]) && hot(points[2]));
        assert_eq!(before_phase(points[0]), "");
        assert_eq!(before_phase(points[1]), "before-capture");
        assert_eq!(before_phase(points[2]), "before-remove");
    }
}

#[test]
fn initialized_cuts_require_all_resource_prefixes_without_changing_old_cases() {
    assert_eq!(
        &POINTS[..7],
        &[
            "paired-profile-created-hot-receipt",
            "paired-profile-using",
            "paired-profile-captured-hot-receipt",
            "paired-profile-removed-hot-receipt",
            "writer-family-live-use",
            "writer-family-captured-hot-receipt",
            "writer-family-removed-hot-receipt"
        ]
    );
    for point in &POINTS[..7] {
        assert!(!initialized(point));
        assert_eq!(resource_counts_for(point), [0; 5]);
    }
    for point in &POINTS[7..] {
        assert!(initialized(point) && writer(point));
        assert_eq!(resource_counts_for(point), [2, 3, 2, 2, 5]);
    }
    for stage in ["observe", "family", "armed", "cut"] {
        for offset in 0..3 {
            assert_eq!(
                counts_for(POINTS[4 + offset], stage),
                counts_for(POINTS[7 + offset], stage)
            );
            assert_eq!(
                retired(POINTS[4 + offset], stage),
                retired(POINTS[7 + offset], stage)
            );
        }
    }
}

#[test]
fn initialized_resource_prefix_comparison_refuses_mutation_addition_and_removal() {
    // Comparison-only data: never admitted as a valid plan or native proof.
    let snapshot = || Snapshot {
        plan: String::new(),
        bootstrap: Vec::new(),
        account: Vec::new(),
        resume: Vec::new(),
        profile: Vec::new(),
        namespace: vec![(0, "namespace".into())],
        host_profile: vec![(0, "host profile".into())],
        station: vec![(0, "station".into())],
        desktop: vec![(0, "desktop".into())],
        initializer: vec![(0, "initializer".into())],
    };
    let original = snapshot();
    assert!(require_same_resources(&original, &snapshot()).is_ok());
    for index in 0..5 {
        for change in 0..3 {
            let mut changed = snapshot();
            let rows = match index {
                0 => &mut changed.namespace,
                1 => &mut changed.host_profile,
                2 => &mut changed.station,
                3 => &mut changed.desktop,
                _ => &mut changed.initializer,
            };
            match change {
                0 => rows[0].1.push(' '), // Even JSON-equivalent bytes differ.
                1 => rows.push((1, "cleanup intent".into())),
                _ => rows.clear(),
            }
            assert!(require_same_resources(&original, &changed).is_err());
        }
    }
}

fn synthetic_reported_proof(point: &str, stage: &str, length: usize) -> Vec<u8> {
    // Framing data only: this deliberately lacks a complete Proof and grants
    // no account, process, or resource authority on successful envelope read.
    let mut bytes = serde_json::to_vec(&serde_json::json!({"point":point,"stage":stage})).unwrap();
    assert!(bytes.len() <= length);
    bytes.resize(length, b' ');
    bytes
}

#[test]
fn reported_proof_reads_are_strictly_bounded_for_every_selected_cut() {
    assert_eq!(MAX_PROOF, 65536); // The unchanged control-frame/old-cut ceiling.
    assert_eq!(MAX_INITIALIZED_PROOF, 131072);
    for (index, point) in POINTS.iter().enumerate() {
        let expected_limit = if index < 7 { 65536 } else { 131072 };
        assert_eq!(proof_limit(point).unwrap(), expected_limit);
        for length in [expected_limit - 1, expected_limit, expected_limit + 1] {
            let bytes = synthetic_reported_proof(point, "cut", length);
            let mut reader = std::io::Cursor::new(&bytes);
            let result = read_reported_proof(&mut reader, point, "cut");
            assert_eq!(reader.position() as usize, length.min(expected_limit));
            if length < expected_limit {
                assert_eq!(result.unwrap(), bytes);
            } else {
                assert!(format!("{:#}", result.unwrap_err()).contains("proof exceeds bound"));
            }
        }
    }
}

#[test]
fn reported_proof_claims_cannot_enlarge_the_expected_cut_limit() {
    for claimed in &POINTS[7..] {
        let bytes = synthetic_reported_proof(claimed, "cut", MAX_PROOF + 1);
        let mut reader = std::io::Cursor::new(&bytes);
        let error = read_reported_proof(&mut reader, POINTS[4], "cut").unwrap_err();
        assert!(format!("{error:#}").contains("proof exceeds bound"));
        assert_eq!(reader.position(), MAX_PROOF as u64);
    }
    let bytes = synthetic_reported_proof(POINTS[4], "cut", MAX_PROOF + 1);
    let mut reader = std::io::Cursor::new(&bytes);
    let error = read_reported_proof(&mut reader, POINTS[7], "cut").unwrap_err();
    assert!(format!("{error:#}").contains("proof point differs"));
    assert_eq!(reader.position() as usize, bytes.len());
}

#[test]
fn reported_proof_rejects_unknown_expected_cut_and_wrong_point_or_stage() {
    let bytes = synthetic_reported_proof(POINTS[7], "cut", 256);
    let mut reader = std::io::Cursor::new(&bytes);
    assert!(read_reported_proof(&mut reader, "initialized-writer-family-future", "cut").is_err());
    assert_eq!(reader.position(), 0);
    let error = read_reported_proof(bytes.as_slice(), POINTS[8], "cut").unwrap_err();
    assert!(format!("{error:#}").contains("proof point differs"));
    let error = read_reported_proof(bytes.as_slice(), POINTS[7], "armed").unwrap_err();
    assert!(format!("{error:#}").contains("proof stage differs"));
    // Do not collapse the original bytes through Value before typed parsing:
    // duplicate selection fields must remain an error instead of last-wins.
    let duplicate = format!(
        r#"{{"point":"{}","point":"{}","stage":"cut"}}"#,
        POINTS[7], POINTS[7]
    );
    assert!(read_reported_proof(duplicate.as_bytes(), POINTS[7], "cut").is_err());
}

#[test]
fn larger_initialized_proof_allowance_still_requires_complete_resource_prefixes() {
    let snapshot = Snapshot {
        plan: serde_json::to_string(&paired_plan::fixture_plan()).unwrap(),
        bootstrap: Vec::new(),
        account: Vec::new(),
        resume: Vec::new(),
        profile: Vec::new(),
        namespace: Vec::new(),
        host_profile: Vec::new(),
        station: Vec::new(),
        desktop: Vec::new(),
        initializer: Vec::new(),
    };
    for point in &POINTS[..7] {
        snapshot.require_resources(
            point,
            PolicyKind::BaselineV1,
            RootBirthKind::LegacyUnspecified,
        );
    }
    for root in ["ordinary_directory_v1", "session_shadow_directory_v1"] {
        let mut wire = serde_json::to_value(paired_plan::fixture_plan()).unwrap();
        wire["namespace_birth"] = root.into();
        let altered = Snapshot {
            plan: serde_json::to_string(&wire).unwrap(),
            ..snapshot.clone()
        };
        assert!(
            std::panic::catch_unwind(|| altered.require_resources(
                POINTS[0],
                PolicyKind::BaselineV1,
                RootBirthKind::LegacyUnspecified
            ))
            .is_err()
        );
    }
    for point in &POINTS[7..] {
        assert_eq!(proof_limit(point).unwrap(), MAX_INITIALIZED_PROOF);
        assert!(
            std::panic::catch_unwind(|| snapshot.require_resources(
                point,
                PolicyKind::BaselineV1,
                RootBirthKind::LegacyUnspecified
            ))
            .is_err()
        );
    }
    // A self-consistent alternate plan cannot choose its controller's oracle,
    // even at a prefix with no initialized resource rows yet.
    for kind in [
        PolicyKind::SystemNsiReadInheritanceV3,
        PolicyKind::NsiSystemInheritanceV1,
        PolicyKind::SystemReadInheritanceV2,
        PolicyKind::SystemInheritanceV1,
    ] {
        let mut wire = serde_json::to_value(paired_plan::fixture_plan()).unwrap();
        wire["rpc_policy"] = serde_json::to_value(kind).unwrap();
        let alternate = Snapshot {
            plan: serde_json::to_string(&wire).unwrap(),
            ..snapshot.clone()
        };
        for expected in [
            PolicyKind::BaselineV1,
            PolicyKind::SystemNsiReadInheritanceV3,
        ] {
            for point in &POINTS[..7] {
                assert_eq!(
                    std::panic::catch_unwind(|| alternate.require_resources(
                        point,
                        expected,
                        RootBirthKind::LegacyUnspecified
                    ))
                    .is_ok(),
                    kind == expected
                );
            }
        }
    }
}
