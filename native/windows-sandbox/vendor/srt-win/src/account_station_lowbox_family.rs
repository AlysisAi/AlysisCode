//! Fixed test-only two-generation CreateProcessW observation. The raw helper
//! owns every pipe server and pulls offers from retained original process tables.
//! Neither pipe contents nor process IDs are live process/job authority.
use super::*;

const TEST: &str =
    "account_control::station_lowbox_fixture::native::family::fixed_lowbox_descendant";
const ENV: &str = "ALYSIS_FIXED_LOWBOX_FAMILY_HANDLES";
pub(crate) const EXIT: u32 = 0x4180;
const FAILURE_EXIT: u32 = 0x4181;
const MAX_BUDGET_MS: u64 = 10_000;

/// Fixed diagnostic identities only; never serialize native error text or paths.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum RootStage {
    SelfProcess,
    SelfToken,
    SelfJob,
    SelfImagePath,
    SelfImageIdentity,
    SelfStation,
    SelfStationName,
    SelfDesktop,
    ClientHandoff,
    InheritedCopy,
    Attributes,
    CreateChild,
    ChildToken,
    ChildJob,
    Close,
    Unknown,
}
impl std::fmt::Display for RootStage {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{self:?}")
    }
}
impl std::error::Error for RootStage {}
pub(super) fn root_failure_stage(error: &anyhow::Error) -> RootStage {
    error
        .downcast_ref::<RootStage>()
        .copied()
        .unwrap_or(RootStage::Unknown)
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Origin {
    nonce: String,
    use_nonce: String,
    account: String,
    container: String,
    image: [u8; 24],
    network: crate::appcontainer::NetworkAccess,
    root: (u32, u64),
    authentication_id: [u32; 2],
}
impl Origin {
    fn new(request: &Request, root: (u32, u64), authentication_id: [u32; 2]) -> Result<Self> {
        let b = binding(request)?;
        let value = Self {
            nonce: request.birth.nonce.clone(),
            use_nonce: b.profile.use_nonce.clone(),
            account: request.birth.selection.account.account_sid.clone(),
            container: b.profile.container.clone(),
            image: request.birth.selection.image_id,
            network: network(request),
            root,
            authentication_id,
        };
        value.require()?;
        Ok(value)
    }
    fn subject(subject: &ChildRequest) -> Result<Self> {
        Self::new(
            &subject.request,
            (subject.pid, subject.created),
            subject.authentication_id,
        )
    }
    fn require(&self) -> Result<()> {
        crate::account_install::validate_random_id(&self.nonce)?;
        crate::account_install::validate_random_id(&self.use_nonce)?;
        crate::job::execution_name(&self.container)?;
        ensure!(
            self.account.len() <= 184
                && self.account.starts_with("S-1-5-")
                && self.image != [0; 24]
                && self.root.0 != 0
                && self.root.1 != 0
                && self.authentication_id != [0; 2],
            "family original binding invalid"
        );
        Ok(())
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Offer {
    origin: Origin,
    depth: u32,
    creator: (u32, u64),
    reference: Reference,
}
impl Offer {
    fn require(&self, origin: &Origin, depth: u32, creator: (u32, u64)) -> Result<()> {
        origin.require()?;
        ensure!(
            matches!(depth, 1 | 2)
                && self.origin == *origin
                && self.depth == depth
                && self.creator == creator
                && creator.0 != 0
                && creator.1 != 0
                && (depth != 1 || creator == origin.root)
                && (depth != 2 || (creator.0 != origin.root.0 && creator.1 >= origin.root.1)),
            "family offer depth or creator differs"
        );
        let (raw, pid, created) = self.reference;
        handle_value(raw)?;
        ensure!(
            pid != 0 && pid != creator.0 && pid != origin.root.0 && created >= creator.1,
            "family offer original process differs"
        );
        Ok(())
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Row {
    depth: u32,
    creator: (u32, u64),
    identity: (u32, u64),
    token_image_verified: bool,
    broker_acknowledged: bool,
    child_report_verified: bool,
    normal_exit: bool,
    exit_code: Option<u32>,
    references_closed: bool,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Proof {
    pub(crate) passed: bool,
    pub(crate) stage: String,
    pub(crate) hresult: Option<i32>,
    origin: Origin,
    skipped: bool,
    raw_clients_closed: bool,
    servers_closed: bool,
    rows: Vec<Row>,
}
impl Proof {
    pub(crate) fn identities(&self) -> Vec<(u32, u64)> {
        self.rows.iter().map(|row| row.identity).collect()
    }
    pub(crate) fn require_original(
        &self,
        request: &Request,
        root: (u32, u64),
        auth: [u32; 2],
    ) -> Result<()> {
        ensure!(
            self.origin == Origin::new(request, root, auth)?,
            "family proof original differs"
        );
        self.require_shape()
    }
    fn require_subject(&self, subject: &ChildRequest) -> Result<()> {
        subject.require()?;
        self.require_original(
            &subject.request,
            (subject.pid, subject.created),
            subject.authentication_id,
        )
    }
    fn require_shape(&self) -> Result<()> {
        self.origin.require()?;
        ensure!(
            [
                "offer_one",
                "verify_one",
                "ack_one",
                "offer_two",
                "verify_two",
                "ack_two",
                "exit_two",
                "exit_one",
                "close",
                "skipped",
                "complete"
            ]
            .contains(&self.stage.as_str())
                && self.rows.len() <= 2
                && self.hresult.is_none_or(|v| v < 0),
            "family proof bounds differ"
        );
        let mut creator = self.origin.root;
        for (index, row) in self.rows.iter().enumerate() {
            ensure!(
                row.depth == index as u32 + 1
                    && row.creator == creator
                    && row.identity.0 != 0
                    && row.identity.0 != creator.0
                    && row.identity.0 != self.origin.root.0
                    && row.identity.1 >= creator.1
                    && (!row.broker_acknowledged || row.token_image_verified)
                    && (!row.child_report_verified || row.broker_acknowledged)
                    && (row.normal_exit == (row.exit_code == Some(EXIT)))
                    && (!row.normal_exit || row.child_report_verified),
                "family proof generation differs"
            );
            creator = row.identity;
        }
        ensure!(
            !self.rows.first().is_some_and(|row| row.normal_exit)
                || self.rows.get(1).is_some_and(|row| row.normal_exit),
            "family parent exit precedes grandchild exit"
        );
        ensure!(
            !self.skipped
                || (self.rows.is_empty()
                    && !self.passed
                    && self.stage == "skipped"
                    && self.hresult.is_none()),
            "family skipped proof differs"
        );
        if self.passed {
            ensure!(
                !self.skipped
                    && self.stage == "complete"
                    && self.hresult.is_none()
                    && self.raw_clients_closed
                    && self.servers_closed
                    && self.rows.len() == 2
                    && self
                        .rows
                        .iter()
                        .all(|r| r.normal_exit && r.references_closed),
                "family success lacks original exits or closure"
            );
        } else if !self.skipped {
            ensure!(
                self.stage != "complete" && self.hresult.is_some(),
                "family failure status absent"
            );
        }
        Ok(())
    }
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Start {
    subject: ChildRequest,
    offer: Offer,
    budget_ms: u64,
}
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Ack {
    offer: Offer,
    phase: String,
}
impl Ack {
    fn new(offer: &Offer, phase: &str) -> Self {
        Self {
            offer: offer.clone(),
            phase: phase.into(),
        }
    }
    fn require(&self, offer: &Offer, phase: &str) -> Result<()> {
        ensure!(
            self.offer == *offer && self.phase == phase && matches!(phase, "resume" | "exit"),
            "family acknowledgment differs"
        );
        Ok(())
    }
}
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Completion {
    offer: Offer,
    token_verified: bool,
    image_verified: bool,
    descendant_exit: Option<u32>,
}
impl Completion {
    fn require(&self, offer: &Offer) -> Result<()> {
        ensure!(
            self.offer == *offer
                && self.token_verified
                && self.image_verified
                && self.descendant_exit == (offer.depth == 1).then_some(EXIT),
            "family completion differs"
        );
        Ok(())
    }
}

fn handle_value(value: u64) -> Result<()> {
    ensure!(
        value > 0 && value < isize::MAX as u64,
        "family handle value invalid"
    );
    Ok(())
}
fn remaining(end: Instant) -> Result<Duration> {
    let value = end
        .checked_duration_since(Instant::now())
        .context("family deadline expired")?;
    ensure!(
        value.as_millis() > 0 && value <= Duration::from_millis(MAX_BUDGET_MS),
        "family deadline outside bound"
    );
    Ok(value)
}
fn send<T: Serialize>(endpoint: &Endpoint, value: &T, end: Instant) -> Result<()> {
    remaining(end)?;
    let mut bytes = encode(value)?;
    io::transfer(
        endpoint.0.raw(),
        &mut (bytes.len() as u32).to_le_bytes(),
        true,
        end,
    )?;
    io::transfer(endpoint.0.raw(), &mut bytes, true, end)
}
fn close(owned: OwnedHandle) -> Result<()> {
    unsafe { CloseHandle(owned.into_raw()) }?;
    Ok(())
}
fn duplicate(source: HANDLE, handle: HANDLE, access: u32, inherit: bool) -> Result<OwnedHandle> {
    let mut raw = HANDLE::default();
    unsafe {
        DuplicateHandle(
            source,
            handle,
            GetCurrentProcess(),
            &mut raw,
            access,
            inherit,
            if access == 0 {
                DUPLICATE_SAME_ACCESS
            } else {
                DUPLICATE_HANDLE_OPTIONS(0)
            },
        )?;
    }
    Ok(OwnedHandle(raw))
}
fn identity(process: HANDLE) -> Result<(u32, u64)> {
    let pid = unsafe { GetProcessId(process) };
    ensure!(pid != 0, "family process reference invalid");
    Ok((pid, bootstrap::pull::creation_time(process)?))
}
fn reference(process: HANDLE) -> Result<Reference> {
    noninheritable(process)?;
    let (pid, created) = identity(process)?;
    Ok((process.0 as usize as u64, pid, created))
}
fn job_member(process: HANDLE) -> Result<()> {
    let mut member = windows::core::BOOL::default();
    unsafe {
        IsProcessInJob(process, None, &mut member)?;
    }
    ensure!(member.as_bool(), "family process outside job");
    Ok(())
}
fn wait_exit(process: HANDLE, expected: (u32, u64), end: Instant) -> Result<()> {
    ensure!(
        identity(process)? == expected,
        "family exit original differs"
    );
    let wait = unsafe { WaitForSingleObject(process, remaining(end)?.as_millis() as u32) };
    ensure!(wait == WAIT_OBJECT_0, "family original exit missing");
    let mut code = 0;
    unsafe {
        GetExitCodeProcess(process, &mut code)?;
    }
    ensure!(code == EXIT, "family original exit code differs");
    Ok(())
}
fn failure(error: &anyhow::Error) -> i32 {
    native_code(error).filter(|v| *v < 0).unwrap_or(E_FAIL.0)
}

// Trusted raw-helper custody only. Never inherited or exposed to the broker.
// On failure, drain known generations in reverse order before object cleanup.
struct Live {
    control: Option<OwnedHandle>,
    query: Option<OwnedHandle>,
    identity: (u32, u64),
    armed: bool,
}
impl Live {
    fn pull(
        source: HANDLE,
        offer: &Offer,
        subject: &ChildRequest,
        image: &AccountImageObserver,
    ) -> Result<Self> {
        let access = (PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SYNCHRONIZE | PROCESS_TERMINATE)
            .0
            | if offer.depth == 1 {
                PROCESS_DUP_HANDLE.0
            } else {
                0
            };
        let control = duplicate(
            source,
            HANDLE(offer.reference.0 as usize as *mut _),
            access,
            false,
        )?;
        // An untrusted offer is not termination authority. Keep this copy
        // unarmed until the independent original-job hook acknowledges it.
        let mut value = Self {
            identity: (offer.reference.1, offer.reference.2),
            control: Some(control),
            query: None,
            armed: false,
        };
        ensure!(
            identity(value.control())? == value.identity,
            "family original table offer differs"
        );
        require_lowbox(value.control(), &subject.request, subject.authentication_id)?;
        image.verify_process(value.control())?;
        job_member(value.control())?;
        value.query = Some(duplicate(
            unsafe { GetCurrentProcess() },
            value.control(),
            (PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SYNCHRONIZE).0,
            false,
        )?);
        Ok(value)
    }
    fn control(&self) -> HANDLE {
        self.control.as_ref().expect("live family control").raw()
    }
    fn query(&self) -> HANDLE {
        self.query.as_ref().expect("live family query").raw()
    }
    fn drain(&mut self) -> Result<()> {
        if self.armed {
            let process = self.control();
            let _ = unsafe { TerminateProcess(process, FAILURE_EXIT) };
            ensure!(
                unsafe { WaitForSingleObject(process, 5000) } == WAIT_OBJECT_0,
                "family termination unverified"
            );
            self.armed = false;
        }
        Ok(())
    }
    fn checked_close(&mut self) -> Result<()> {
        ensure!(!self.armed, "family reference closure before exit");
        let query = self.query.take().map(close).transpose();
        let control = self.control.take().map(close).transpose();
        query?;
        control?;
        Ok(())
    }
}
impl Drop for Live {
    fn drop(&mut self) {
        if let Err(error) = self.drain() {
            crate::util::exit_for_unverified_cleanup("fixed lowbox family", &error);
        }
    }
}

pub(super) struct Prepared {
    servers: [Option<Endpoint>; 2],
    clients: [Option<OwnedHandle>; 2],
    raw_clients_closed: bool,
}
impl Prepared {
    pub(super) fn new(account_sid: &str) -> Result<Self> {
        let (one, client_one) = endpoints(account_sid)?;
        let (two, client_two) = endpoints(account_sid)?;
        Ok(Self {
            servers: [Some(one), Some(two)],
            clients: [Some(client_one), Some(client_two)],
            raw_clients_closed: false,
        })
    }
    pub(super) fn inherited_handles(&self) -> Result<[HANDLE; 2]> {
        ensure!(
            !self.raw_clients_closed,
            "family inherited clients already closed"
        );
        Ok([
            self.clients[0]
                .as_ref()
                .context("family client one absent")?
                .raw(),
            self.clients[1]
                .as_ref()
                .context("family client two absent")?
                .raw(),
        ])
    }
    pub(super) fn inherited_values(&self) -> Result<[u64; 2]> {
        Ok(self.inherited_handles()?.map(|h| h.0 as usize as u64))
    }
    pub(super) fn close_clients_after_birth(&mut self) -> Result<()> {
        ensure!(!self.raw_clients_closed, "family raw clients closed twice");
        let one = self.clients[0]
            .take()
            .context("family client one absent")
            .and_then(close);
        let two = self.clients[1]
            .take()
            .context("family client two absent")
            .and_then(close);
        one?;
        two?;
        self.raw_clients_closed = true;
        Ok(())
    }
    fn server(&self, index: usize) -> &Endpoint {
        self.servers[index]
            .as_ref()
            .expect("family server retained")
    }
    pub(super) fn run_parent(
        mut self,
        root_process: HANDLE,
        root_endpoint: &Endpoint,
        subject: &ChildRequest,
        image: &AccountImageObserver,
        end: Instant,
        mut before_resume: impl FnMut(u32, Reference) -> Result<()>,
    ) -> Result<Proof> {
        subject.require()?;
        ensure!(
            binding(&subject.request)?.is_v3_shadow()
                && self.raw_clients_closed
                && identity(root_process)? == (subject.pid, subject.created),
            "family parent scope differs"
        );
        let origin = Origin::subject(subject)?;
        let mut proof = Proof {
            passed: false,
            stage: "offer_one".into(),
            hresult: None,
            origin: origin.clone(),
            skipped: false,
            raw_clients_closed: true,
            servers_closed: false,
            rows: Vec::new(),
        };
        let mut originals: Vec<Live> = Vec::new();
        // Until a complete offer is pulled, terminating its creator cannot prove
        // an unobserved descendant exited. Stop this helper, without unwinding,
        // on that ambiguity; the original broker must drain the containing job.
        let mut unknown_child_possible = true;
        let result = (|| -> Result<()> {
            remaining(end)?;
            require_lowbox(root_process, &subject.request, subject.authentication_id)?;
            let Some(first) = root_endpoint.receive_before::<Option<Offer>>(end)? else {
                unknown_child_possible = false;
                proof.skipped = true;
                proof.stage = "skipped".into();
                return Ok(());
            };
            first.require(&origin, 1, origin.root)?;
            proof.stage = "verify_one".into();
            originals.push(Live::pull(root_process, &first, subject, image)?);
            proof.rows.push(row(&first));
            proof.stage = "ack_one".into();
            before_resume(1, reference(originals[0].query())?)?;
            originals[0].armed = true;
            proof.rows[0].broker_acknowledged = true;
            unknown_child_possible = true;
            send(root_endpoint, &Ack::new(&first, "resume"), end)?;
            send(
                self.server(0),
                &Start {
                    subject: subject.clone(),
                    offer: first.clone(),
                    budget_ms: remaining(end)?.as_millis() as u64,
                },
                end,
            )?;
            proof.stage = "offer_two".into();
            let second: Offer = self.server(0).receive_before(end)?;
            second.require(&origin, 2, originals[0].identity)?;
            proof.stage = "verify_two".into();
            originals.push(Live::pull(originals[0].control(), &second, subject, image)?);
            proof.rows.push(row(&second));
            proof.stage = "ack_two".into();
            before_resume(2, reference(originals[1].query())?)?;
            originals[1].armed = true;
            unknown_child_possible = false;
            proof.rows[1].broker_acknowledged = true;
            send(self.server(0), &Ack::new(&second, "resume"), end)?;
            send(
                self.server(1),
                &Start {
                    subject: subject.clone(),
                    offer: second.clone(),
                    budget_ms: remaining(end)?.as_millis() as u64,
                },
                end,
            )?;
            for (index, offer) in [(1, &second), (0, &first)] {
                proof.stage = if index == 1 { "exit_two" } else { "exit_one" }.into();
                let completed: Completion = self.server(index).receive_before(end)?;
                completed.require(offer)?;
                require_lowbox(
                    originals[index].query(),
                    &subject.request,
                    subject.authentication_id,
                )?;
                image.verify_process(originals[index].query())?;
                proof.rows[index].child_report_verified = true;
                send(self.server(index), &Ack::new(offer, "exit"), end)?;
                wait_exit(originals[index].query(), originals[index].identity, end)?;
                originals[index].armed = false;
                proof.rows[index].normal_exit = true;
                proof.rows[index].exit_code = Some(EXIT);
            }
            Ok(())
        })();
        if let Err(error) = &result {
            if unknown_child_possible {
                crate::util::exit_for_unverified_cleanup(
                    "unobserved fixed lowbox descendant",
                    error,
                );
            }
            proof.hresult = Some(failure(error));
        }
        for (index, original) in originals.iter_mut().enumerate().rev() {
            if let Err(error) = original.drain() {
                crate::util::exit_for_unverified_cleanup("fixed lowbox descendant", &error);
            }
            match original.checked_close() {
                Ok(()) => proof.rows[index].references_closed = true,
                Err(error) => {
                    proof.hresult.get_or_insert(failure(&error));
                    if result.is_ok() {
                        proof.stage = "close".into();
                    }
                }
            }
        }
        let closed = self
            .servers
            .iter_mut()
            .map(|slot| slot.take().map(|e| close(e.0)).transpose())
            .collect::<Vec<_>>();
        proof.servers_closed = closed.iter().all(Result::is_ok);
        for status in closed {
            if let Err(error) = status {
                proof.hresult.get_or_insert(failure(&error));
                proof.stage = "close".into();
                proof.skipped = false;
            }
        }
        if result.is_ok() && !proof.skipped && proof.hresult.is_none() {
            proof.passed = true;
            proof.stage = "complete".into();
        }
        proof.require_subject(subject)?;
        Ok(proof)
    }
}
fn row(offer: &Offer) -> Row {
    Row {
        depth: offer.depth,
        creator: offer.creator,
        identity: (offer.reference.1, offer.reference.2),
        token_image_verified: true,
        broker_acknowledged: false,
        child_report_verified: false,
        normal_exit: false,
        exit_code: None,
        references_closed: false,
    }
}

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Handles {
    endpoint: u64,
    downstream: Option<u64>,
}
impl Handles {
    fn require(&self) -> Result<()> {
        handle_value(self.endpoint)?;
        if let Some(next) = self.downstream {
            handle_value(next)?;
            ensure!(next != self.endpoint, "family inherited endpoints alias");
        }
        Ok(())
    }
    fn read(text: &str) -> Result<Self> {
        ensure!(text.len() <= 128, "family inherited handoff exceeds bound");
        let value: Self = serde_json::from_str(text)?;
        value.require()?;
        Ok(value)
    }
}
fn take_client(value: u64) -> Result<OwnedHandle> {
    handle_value(value)?;
    let owned = OwnedHandle(HANDLE(value as usize as *mut _));
    let mut flags = 0;
    unsafe {
        GetHandleInformation(owned.raw(), &mut flags)?;
    }
    ensure!(
        flags & HANDLE_FLAG_INHERIT.0 != 0 && unsafe { GetFileType(owned.raw()) } == FILE_TYPE_PIPE,
        "family client is not inherited pipe"
    );
    let copy = duplicate(unsafe { GetCurrentProcess() }, owned.raw(), 0, false)?;
    close(owned)?;
    noninheritable(copy.raw())?;
    Ok(copy)
}
fn self_check(subject: &ChildRequest, expected: (u32, u64)) -> Result<()> {
    subject.require()?;
    ensure!(
        binding(&subject.request)?.is_v3_shadow(),
        "family requires fixed V3 completion"
    );
    crate::account_impersonation::require_process_identity()?;
    let current =
        ExpectedPeer::retain(unsafe { GetCurrentProcess() }).context(RootStage::SelfProcess)?;
    ensure!(
        identity(current.process.raw())? == expected,
        "family current original differs"
    );
    require_lowbox(
        current.process.raw(),
        &subject.request,
        subject.authentication_id,
    )
    .context(RootStage::SelfToken)?;
    job_member(current.process.raw()).context(RootStage::SelfJob)?;
    let image = std::env::current_exe().context(RootStage::SelfImagePath)?;
    ensure!(
        crate::path_id::capture_file_id(image.to_str().context("family image not Unicode")?)
            .context(RootStage::SelfImageIdentity)?
            .as_bytes()
            == subject.request.birth.selection.image_id,
        "family current image ID differs"
    );
    ensure!(
        unsafe { GetErrorMode() } & 3 == 3,
        "family error mode not inherited"
    );
    let station = unsafe { GetProcessWindowStation() }.context(RootStage::SelfStation)?;
    ensure!(
        user_access::original_name(HANDLE(station.0)).context(RootStage::SelfStationName)?
            == subject.native_station
            && text(
                HANDLE(
                    unsafe { GetThreadDesktop(GetCurrentThreadId()) }
                        .context(RootStage::SelfDesktop)?
                        .0
                ),
                UOI_NAME
            )
            .context(RootStage::SelfDesktop)?
                == subject.desktop,
        "family private USER association differs"
    );
    Ok(())
}
fn spawn(
    subject: &ChildRequest,
    depth: u32,
    own: HANDLE,
    downstream: Option<HANDLE>,
) -> Result<SpawnedChild> {
    ensure!(
        matches!(depth, 1 | 2) && downstream.is_some() == (depth == 1),
        "family launch depth differs"
    );
    let parent = identity(unsafe { GetCurrentProcess() })?;
    self_check(subject, parent)?;
    let endpoint = duplicate(unsafe { GetCurrentProcess() }, own, 0, true)
        .context(RootStage::InheritedCopy)?;
    let next = downstream
        .map(|h| duplicate(unsafe { GetCurrentProcess() }, h, 0, true))
        .transpose()
        .context(RootStage::InheritedCopy)?;
    let handoff = Handles {
        endpoint: endpoint.raw().0 as usize as u64,
        downstream: next.as_ref().map(|h| h.raw().0 as usize as u64),
    };
    handoff.require()?;
    let image = std::env::current_exe()?;
    let image = image.to_str().context("family image path not Unicode")?;
    let app = wstr(image);
    let mut command = wstr(&format!(
        "{} --exact {TEST} --ignored --nocapture --test-threads=1",
        crate::launch::quote_arg(image)
    ));
    let system = bootstrap::os_directory(false)?;
    let windows = bootstrap::os_directory(true)?;
    let mut values = vec![
        ("SystemRoot".into(), windows.clone()),
        ("WINDIR".into(), windows),
        ("PATH".into(), system.clone()),
        (ENV.into(), serde_json::to_string(&handoff)?),
    ];
    for name in ["APPDATA", "LOCALAPPDATA", "USERPROFILE", "TEMP", "TMP"] {
        let value = std::env::var(name)?;
        ensure!(
            value.len() <= 4096
                && std::path::Path::new(&value).is_absolute()
                && !value.chars().any(char::is_control),
            "family profile environment invalid"
        );
        values.push((name.into(), value));
    }
    let mut environment = crate::launch::build_env_block_with_inheritance(&values, false);
    ensure!(
        environment.len() <= 32767,
        "family environment exceeds bound"
    );
    let mut handles = vec![endpoint.raw()];
    if let Some(next) = &next {
        handles.push(next.raw());
    }
    let mut attrs = ProcThreadAttrs::new(1).context(RootStage::Attributes)?;
    attrs
        .set_handle_list(&handles)
        .context(RootStage::Attributes)?;
    let mut desktop = wstr(&format!("{}\\{}", subject.station, subject.desktop));
    let mut startup = STARTUPINFOEXW::default();
    startup.StartupInfo.cb = size_of::<STARTUPINFOEXW>() as u32;
    startup.StartupInfo.lpDesktop = PWSTR(desktop.as_mut_ptr());
    startup.lpAttributeList = attrs.list();
    let mut info = PROCESS_INFORMATION::default();
    unsafe {
        CreateProcessW(
            pcwstr(&app),
            Some(PWSTR(command.as_mut_ptr())),
            None,
            None,
            true,
            CREATE_SUSPENDED
                | CREATE_NO_WINDOW
                | CREATE_UNICODE_ENVIRONMENT
                | EXTENDED_STARTUPINFO_PRESENT,
            Some(environment.as_mut_ptr().cast()),
            pcwstr(&wstr(&system)),
            &startup.StartupInfo,
            &mut info,
        )
        .context(RootStage::CreateChild)?;
    }
    let child = SpawnedChild::new(info);
    let closed_endpoint = close(endpoint);
    let closed_next = next.map(close).transpose();
    closed_endpoint.context(RootStage::Close)?;
    closed_next.context(RootStage::Close)?;
    require_lowbox(child.process(), &subject.request, subject.authentication_id)
        .context(RootStage::ChildToken)?;
    job_member(child.process()).context(RootStage::ChildJob)?;
    ensure!(
        identity(child.process())?.1 >= parent.1,
        "family creation predates creator"
    );
    Ok(child)
}
fn offer(subject: &ChildRequest, child: &SpawnedChild, depth: u32) -> Result<Offer> {
    let origin = Origin::subject(subject)?;
    let creator = identity(unsafe { GetCurrentProcess() })?;
    let value = Offer {
        origin: origin.clone(),
        depth,
        creator,
        reference: reference(child.process())?,
    };
    value.require(&origin, depth, creator)?;
    Ok(value)
}
fn resume(child: &SpawnedChild, endpoint: &Endpoint, offer: &Offer, end: Instant) -> Result<()> {
    let ack: Ack = endpoint.receive_before(end)?;
    ack.require(offer, "resume")?;
    remaining(end)?;
    ensure!(
        unsafe { ResumeThread(child.thread()) } == 1,
        "family resume count differs"
    );
    Ok(())
}

/// Caller sends None::<Offer> instead when earlier original checks failed.
/// The root must send its ordinary ChildReport only after this function returns.
pub(super) fn run_root(
    subject: &ChildRequest,
    endpoint: &Endpoint,
    clients: [u64; 2],
    end: Instant,
) -> Result<()> {
    let setup = (|| -> Result<SpawnedChild> {
        subject.require()?;
        remaining(end)?;
        self_check(subject, (subject.pid, subject.created))?;
        ensure!(clients[0] != clients[1], "family root clients alias");
        let one = take_client(clients[0]).context(RootStage::ClientHandoff)?;
        let two = take_client(clients[1]).context(RootStage::ClientHandoff)?;
        let child = spawn(subject, 1, one.raw(), Some(two.raw()))?;
        let close_one = close(one);
        let close_two = close(two);
        close_one.context(RootStage::Close)?;
        close_two.context(RootStage::Close)?;
        Ok(child)
    })();
    let mut child = match setup {
        Ok(child) => child,
        Err(error) => {
            send(endpoint, &None::<Offer>, end)?;
            return Err(error);
        }
    };
    let offered = offer(subject, &child, 1)?;
    send(endpoint, &Some(offered.clone()), end)?;
    resume(&child, endpoint, &offered, end)?;
    wait_exit(
        child.process(),
        (offered.reference.1, offered.reference.2),
        end,
    )?;
    child.defuse();
    drop(child);
    self_check(subject, (subject.pid, subject.created))
}

#[test]
#[ignore = "fixed inherited family endpoint only; no standalone effect admission"]
fn fixed_lowbox_descendant() {
    let result = (|| -> Result<()> {
        ensure!(
            std::env::args().skip(1).collect::<Vec<_>>()
                == [
                    "--exact",
                    TEST,
                    "--ignored",
                    "--nocapture",
                    "--test-threads=1"
                ],
            "family arguments differ"
        );
        let handles = Handles::read(&std::env::var(ENV)?)?;
        let endpoint = Endpoint(take_client(handles.endpoint)?);
        let downstream = handles.downstream.map(take_client).transpose()?;
        let start: Start = endpoint.receive_before(deadline(TIMEOUT)?)?;
        ensure!(
            (1..=MAX_BUDGET_MS).contains(&start.budget_ms),
            "family supplied deadline differs"
        );
        let end = Instant::now() + Duration::from_millis(start.budget_ms);
        let origin = Origin::subject(&start.subject)?;
        start
            .offer
            .require(&origin, start.offer.depth, start.offer.creator)?;
        ensure!(
            downstream.is_some() == (start.offer.depth == 1),
            "family downstream shape differs"
        );
        let own = (start.offer.reference.1, start.offer.reference.2);
        self_check(&start.subject, own)?;
        let mut server = 0;
        unsafe {
            GetNamedPipeServerProcessId(endpoint.0.raw(), &mut server)?;
        }
        ensure!(
            server == start.subject.request.pid,
            "family original server differs"
        );
        let descendant_exit = if let Some(next) = downstream {
            let mut child = spawn(&start.subject, 2, next.raw(), None)?;
            close(next)?;
            let offered = offer(&start.subject, &child, 2)?;
            send(&endpoint, &offered, end)?;
            resume(&child, &endpoint, &offered, end)?;
            wait_exit(
                child.process(),
                (offered.reference.1, offered.reference.2),
                end,
            )?;
            child.defuse();
            drop(child);
            Some(EXIT)
        } else {
            None
        };
        self_check(&start.subject, own)?;
        send(
            &endpoint,
            &Completion {
                offer: start.offer.clone(),
                token_verified: true,
                image_verified: true,
                descendant_exit,
            },
            end,
        )?;
        let ack: Ack = endpoint.receive_before(end)?;
        ack.require(&start.offer, "exit")?;
        close(endpoint.0)?;
        Ok(())
    })();
    std::process::exit(if result.is_ok() { EXIT } else { FAILURE_EXIT } as i32);
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn station_family_root_failure_stage_never_serializes_arbitrary_error_text() {
        let error = anyhow::Error::new(windows::core::Error::from(E_ACCESSDENIED))
            .context("private path and arbitrary payload")
            .context(RootStage::CreateChild);
        assert_eq!(root_failure_stage(&error), RootStage::CreateChild);
        assert_eq!(failure(&error), E_ACCESSDENIED.0);
        assert_eq!(
            serde_json::to_string(&root_failure_stage(&error)).unwrap(),
            "\"create_child\""
        );
        assert_eq!(
            root_failure_stage(&anyhow::anyhow!("untrusted arbitrary path")),
            RootStage::Unknown
        );
        assert!(serde_json::from_str::<RootStage>("\"arbitrary path\"").is_err());
    }
    fn origin() -> Origin {
        Origin {
            nonce: "6123456789abcdef0123456789abcdef".into(),
            use_nonce: "7123456789abcdef0123456789abcdef".into(),
            account: "S-1-5-21-1-2-3-1001".into(),
            container: "Alysis.Execution.8123456789abcdef0123456789abcdef".into(),
            image: [1; 24],
            network: crate::appcontainer::NetworkAccess::Off,
            root: (10, 100),
            authentication_id: [2, 0],
        }
    }
    fn offer(depth: u32) -> Offer {
        Offer {
            origin: origin(),
            depth,
            creator: if depth == 1 { (10, 100) } else { (11, 101) },
            reference: (44, 10 + depth, 100 + depth as u64),
        }
    }
    #[test]
    fn station_family_codec_refuses_unbounded_alias_and_unknown_handle_data() {
        Handles::read("{\"endpoint\":4,\"downstream\":8}").unwrap();
        for text in [
            "{\"endpoint\":4,\"downstream\":4}",
            "{\"endpoint\":0,\"downstream\":null}",
            "{\"endpoint\":4,\"downstream\":null,\"command\":\"x\"}",
        ] {
            assert!(Handles::read(text).is_err());
        }
        assert!(Handles::read(&" ".repeat(129)).is_err());
    }
    #[test]
    fn station_family_offer_refuses_depth_parent_and_cross_origin_replay() {
        let first = offer(1);
        first.require(&origin(), 1, (10, 100)).unwrap();
        let second = offer(2);
        second.require(&origin(), 2, (11, 101)).unwrap();
        assert!(second.require(&origin(), 2, (10, 100)).is_err());
        for change in 0..5 {
            let mut invalid = second.clone();
            match change {
                0 => invalid.depth = 3,
                1 => invalid.reference.1 = 10,
                2 => invalid.reference.2 = 100,
                3 => invalid.origin.authentication_id = [3, 0],
                _ => invalid.reference.0 = u64::MAX,
            }
            assert!(invalid.require(&origin(), 2, (11, 101)).is_err());
        }
        let ack = Ack::new(&first, "resume");
        assert!(ack.require(&second, "resume").is_err());
        assert!(ack.require(&first, "exit").is_err());
    }
    #[test]
    fn station_family_proof_refuses_missing_reverse_exit_closure_and_job_ack() {
        let mut rows = [offer(1), offer(2)].iter().map(row).collect::<Vec<_>>();
        for row in &mut rows {
            row.broker_acknowledged = true;
            row.child_report_verified = true;
            row.normal_exit = true;
            row.exit_code = Some(EXIT);
            row.references_closed = true;
        }
        let proof = Proof {
            passed: true,
            stage: "complete".into(),
            hresult: None,
            origin: origin(),
            skipped: false,
            raw_clients_closed: true,
            servers_closed: true,
            rows,
        };
        proof.require_shape().unwrap();
        for change in 0..7 {
            let mut invalid = proof.clone();
            match change {
                0 => invalid.rows.pop().map(|_| ()).unwrap(),
                1 => invalid.rows[1].broker_acknowledged = false,
                2 => invalid.rows[0].references_closed = false,
                3 => invalid.rows[1].exit_code = Some(FAILURE_EXIT),
                4 => invalid.rows.swap(0, 1),
                5 => invalid.raw_clients_closed = false,
                _ => invalid.servers_closed = false,
            }
            assert!(invalid.require_shape().is_err());
        }
        let mut wire = serde_json::to_value(proof).unwrap();
        wire["job_authority"] = true.into();
        assert!(serde_json::from_value::<Proof>(wire).is_err());
    }
}
