//! Real protected marker/database/image and two original strict sibling jobs.
//! Fixed trusted bootstrap only; no SAM, credentials, profile or user commands.
use super::*;
use crate::{
    account_control::{ControlAddress, ControlServer},
    account_state::image::PinnedRunnerImage,
};
use paired_plan::{CommittedPlan, Role, native::NATIVE_CHECKPOINT};
use std::{
    cell::{Cell, RefCell},
    rc::Rc,
    time::Duration,
};
use windows::Win32::{
    Foundation::{DUPLICATE_HANDLE_OPTIONS, DuplicateHandle, ERROR_FILE_NOT_FOUND},
    System::{
        JobObjects::*,
        SystemServices::JOB_OBJECT_QUERY,
        Threading::{GetExitCodeProcess, PROCESS_NAME_WIN32, QueryFullProcessImageNameW},
    },
};

#[path = "account_paired_crash_tests.rs"]
mod crash;

#[path = "account_paired_account_native_tests.rs"]
mod account;

#[path = "account_paired_resume_native_tests.rs"]
mod account_resume;

#[path = "account_paired_profile_native_tests.rs"]
mod account_profile;

#[path = "account_paired_profile_crash_tests.rs"]
mod profile_crash;

#[path = "account_paired_namespace_native_tests.rs"]
mod namespace;

#[path = "account_paired_contained_image_fixture.rs"]
mod contained_image;

#[path = "account_paired_station_native_tests.rs"]
mod station;
pub(super) fn hosted_station_pair(fixture: Fixture, case: &str) -> Result<serde_json::Value> {
    station::hosted(fixture, case)
}
pub(super) fn hosted_station_profile_pair(
    fixture: Fixture,
    password: &str,
    case: &str,
) -> Result<serde_json::Value> {
    namespace::hosted_station_profile(fixture, password, case)
}
#[path = "account_paired_desktop_native_tests.rs"]
mod desktop;
#[path = "account_paired_initializer_native_tests.rs"]
mod initializer;
pub(super) fn hosted_initializer_cases() -> &'static [&'static str] {
    &initializer::CASES
}
pub(super) fn hosted_initializer_pair(fixture: Fixture, case: &str) -> Result<serde_json::Value> {
    initializer::hosted(fixture, case)
}
pub(super) fn hosted_initializer_profile_pair(
    fixture: Fixture,
    password: &str,
    case: &str,
) -> Result<serde_json::Value> {
    namespace::hosted_initializer_profile(fixture, password, case)
}
pub(super) fn hosted_rpc_v3_profile_pair(
    fixture: Fixture,
    password: &str,
) -> Result<serde_json::Value> {
    namespace::hosted_rpc_v3_profile(fixture, password)
}
pub(super) fn hosted_rpc_v3_profile_crash(
    fixture: Fixture,
    password: &str,
    point: &str,
) -> Result<()> {
    profile_crash::run_v3(fixture, password, point)
}
pub(super) fn hosted_rpc_v3_profile_crash_broker(password: &str) {
    profile_crash::broker_v3(password)
}
pub(super) fn hosted_rpc_v3_shadow_profile_pair(
    fixture: Fixture,
    password: &str,
) -> Result<serde_json::Value> {
    namespace::hosted_rpc_v3_shadow_profile(fixture, password)
}
pub(super) fn hosted_rpc_v3_shadow_profile_crash(
    fixture: Fixture,
    password: &str,
    point: &str,
) -> Result<()> {
    profile_crash::run_v3_shadow(fixture, password, point)
}
pub(super) fn hosted_rpc_v3_shadow_profile_crash_broker(password: &str) {
    profile_crash::broker_v3_shadow(password)
}
pub(super) fn hosted_station_lowbox(fixture: Fixture, password: &str) -> Result<serde_json::Value> {
    hosted_lowbox_inner(fixture, password, false)
}
pub(super) fn hosted_v3_contained_offline(
    fixture: Fixture,
    password: &str,
) -> Result<serde_json::Value> {
    crate::account_rpc_v3_lifecycle_fixture::require_contained_offline_live()?;
    hosted_lowbox_inner(fixture, password, true)
}
pub(super) fn hosted_v3_contained_online(
    fixture: Fixture,
    password: &str,
) -> Result<serde_json::Value> {
    crate::account_rpc_v3_lifecycle_fixture::require_contained_online_live()?;
    hosted_lowbox_inner(fixture, password, true)
}
fn hosted_lowbox_inner(fixture: Fixture, password: &str, v3: bool) -> Result<serde_json::Value> {
    use crate::account_control::station_lowbox_fixture::{
        BROKER_STAGE, ENABLED, RECEIPT, SHADOW_BORN,
    };
    use crate::account_state::execution::resources::FixtureRpcPolicy as RpcPolicy;
    // Select before activating the guard or constructing any fixture resources.
    BROKER_STAGE.with(|s| s.set("select_policy"));
    let rpc_policy = if v3 {
        RpcPolicy::SystemNsiReadInheritanceV3
    } else {
        RpcPolicy::selected_fixture_policy()?
    };
    struct Reset(bool);
    impl Drop for Reset {
        fn drop(&mut self) {
            if self.0 {
                ENABLED.with(|s| s.set(false));
            }
        }
    }
    ensure!(
        !ENABLED.with(|s| s.get()),
        "station lowbox fixture already active"
    );
    if !v3 {
        ENABLED.with(|s| s.set(true));
    }
    let _reset = Reset(!v3);
    rpc_policy.require_live()?;
    SHADOW_BORN.with(|s| s.set(false));
    ensure!(
        RECEIPT.with(|s| s.borrow().is_none()),
        "station lowbox receipt already exists"
    );
    let mode = if v3 {
        crate::account_rpc_v3_lifecycle_fixture::contained_mode()?
            .context("original contained mode absent")?
    } else {
        crate::account_control::station_lowbox_fixture::fixture_mode()
    };
    BROKER_STAGE.with(|s| s.set("fixture_storage"));
    let mut fixture = PairFixture::from_fixture_image_mode(fixture, true, mode);
    let owner = fixture.owner_server.take().unwrap();
    let consumer = fixture.consumer_server.take().unwrap();
    let marker = std::fs::read(fixture._fixture.path())?;
    BROKER_STAGE.with(|s| s.set("plan_commit"));
    let plan = if v3 && mode == crate::account_install::AccountMode::Online {
        CommittedPlan::prepare_rpc_v3_contained_online_fixture(
            &fixture.db,
            &fixture.active,
            &fixture.image,
            &fixture.ready,
            &fixture.owner_address,
            &fixture.consumer_address,
        )?
    } else if v3 {
        CommittedPlan::prepare_rpc_v3_contained_offline_fixture(
            &fixture.db,
            &fixture.active,
            &fixture.image,
            &fixture.ready,
            &fixture.owner_address,
            &fixture.consumer_address,
        )?
    } else if rpc_policy == RpcPolicy::KernelBaseAccountFullV1 {
        CommittedPlan::prepare_rpc_constructor_fixture(
            &fixture.db,
            &fixture.active,
            &fixture.image,
            &fixture.ready,
            &fixture.owner_address,
            &fixture.consumer_address,
        )?
    } else if rpc_policy == RpcPolicy::SystemInheritanceV1 {
        CommittedPlan::prepare_rpc_system_inheritance_fixture(
            &fixture.db,
            &fixture.active,
            &fixture.image,
            &fixture.ready,
            &fixture.owner_address,
            &fixture.consumer_address,
        )?
    } else if rpc_policy == RpcPolicy::NsiSystemInheritanceV1 {
        CommittedPlan::prepare_rpc_nsi_system_fixture(
            &fixture.db,
            &fixture.active,
            &fixture.image,
            &fixture.ready,
            &fixture.owner_address,
            &fixture.consumer_address,
        )?
    } else {
        fixture.plan()
    };
    let raw_plan: String = fixture.db.connection()?.query_row(
        "SELECT plan_json FROM candidate_paired_bootstrap_plan",
        [],
        |r| r.get(0),
    )?;
    let typed_plan: paired_plan::Plan = serde_json::from_str(&raw_plan)?;
    typed_plan.fixture_require_rpc_policy(rpc_policy)?;
    let expected_birth = if v3 {
        crate::account_state::execution::resources::FixtureRootBirthKind::SessionShadowDirectoryV1
    } else {
        crate::account_state::execution::resources::FixtureRootBirthKind::LegacyUnspecified
    };
    typed_plan.fixture_require_namespace_birth(expected_birth)?;
    BROKER_STAGE.with(|s| s.set("create_jobs"));
    let jobs = plan.create_jobs()?;
    BROKER_STAGE.with(|s| s.set("namespace"));
    let namespace = jobs.with_empty_namespace()?;
    BROKER_STAGE.with(|s| s.set("host_profile"));
    let profile = namespace.with_host_profile_for_use()?;
    BROKER_STAGE.with(|s| s.set("station"));
    let station = profile.with_station()?;
    BROKER_STAGE.with(|s| s.set("desktop"));
    let desktop = station.with_unused_desktop()?;
    BROKER_STAGE.with(|s| s.set("profile_lifetime"));
    let completed = desktop.fixture_run_initialized_profile_lifetime(
        owner,
        consumer,
        crate::account_control::station_lowbox_fixture::control_timeout(),
        password,
    )?;
    BROKER_STAGE.with(|s| s.set("retirement_verify"));
    completed.verify()?;
    ensure!(
        fixture.rows(Role::Owner) == 9 && fixture.rows(Role::Consumers) == 9,
        "station original role retirement incomplete"
    );
    let mut rows = serde_json::Map::new();
    let mut event_rows = serde_json::Map::new();
    for (table, expected) in [
        ("candidate_paired_profile_events", 11),
        ("candidate_paired_account_resume_events", 3),
        ("candidate_paired_namespace", 4),
        ("candidate_paired_host_profile", 6),
        ("candidate_paired_station", 4),
        ("candidate_paired_desktop", 4),
        ("candidate_paired_initializer", 7),
    ] {
        let count: usize = fixture.db.connection()?.query_row(
            &format!("SELECT count(*) FROM {table}"),
            [],
            |r| r.get(0),
        )?;
        ensure!(
            count == expected,
            "station lifecycle durable prefix differs"
        );
        let events: Vec<serde_json::Value> = fixture
            .db
            .connection()?
            .prepare(&format!("SELECT event_json FROM {table} ORDER BY sequence"))?
            .query_map([], |r| r.get::<_, String>(0))?
            .map(|r| Ok(serde_json::from_str(&r?)?))
            .collect::<Result<_>>()?;
        ensure!(events.len() == expected, "contained event evidence differs");
        event_rows.insert(table.into(), serde_json::json!(events));
        rows.insert(table.into(), serde_json::json!(count));
    }
    drop(completed);
    paired_plan::native::namespace::fixture_require_absent(&plan)?;
    paired_plan::native::namespace::station::desktop::fixture_require_absent(&plan)?;
    let after_plan: String = fixture.db.connection()?.query_row(
        "SELECT plan_json FROM candidate_paired_bootstrap_plan",
        [],
        |r| r.get(0),
    )?;
    ensure!(raw_plan == after_plan, "contained lifecycle plan changed");
    ensure!(
        counts(&fixture.name(Role::Owner)).is_none()
            && counts(&fixture.name(Role::Consumers)).is_none(),
        "station original jobs survived closure"
    );
    ensure!(
        std::fs::read(fixture._fixture.path())? == marker,
        "station fixture marker changed"
    );
    ensure!(
        RECEIPT.with(|s| s.borrow().as_ref().is_some_and(|r| r.passed)),
        "station native proof missing"
    );
    Ok(
        serde_json::json!({"plan":typed_plan,"expected_rpc_policy":rpc_policy,"expected_namespace_birth":expected_birth,
        "bounded_contained_v3":v3,"station_diagnostic_guard_used":!v3,
        "original_namespace_absent":true,"original_desktop_absent":true,"pending_marker_unchanged":true,
        "rows":rows,"events":event_rows,"role_rows":[9,9],"original_jobs_absent":true,
        "original_consumer_job_observed_before_resume":true,"initializer_and_profile_retained_through_jobs":true,
        "consumer_retired_before_owner_capture":true,"contained_image_access":fixture.contained_image_access,
        "production_ready":false,"full_matrix_qualified":false}),
    )
}
pub(super) fn hosted_desktop_pair(fixture: Fixture, case: &str) -> Result<serde_json::Value> {
    desktop::hosted(fixture, case)
}
pub(super) fn hosted_desktop_profile_pair(
    fixture: Fixture,
    password: &str,
    case: &str,
) -> Result<serde_json::Value> {
    namespace::hosted_desktop_profile(fixture, password, case)
}

pub(super) fn hosted_namespace_pair(
    fixture: Fixture,
    password: &str,
    case: &str,
) -> Result<serde_json::Value> {
    namespace::hosted(fixture, password, case)
}
pub(super) fn hosted_host_profile_pair(
    fixture: Fixture,
    password: &str,
    case: &str,
) -> Result<serde_json::Value> {
    namespace::hosted_host_profile(fixture, password, case)
}

pub(super) fn hosted_profile_crash(fixture: Fixture, password: &str, point: &str) -> Result<()> {
    profile_crash::run(fixture, password, point)
}

pub(super) fn hosted_profile_crash_broker(password: &str) {
    profile_crash::broker(password);
}

pub(super) fn hosted_profile_pair(
    fixture: Fixture,
    password: &str,
    case: &str,
) -> Result<serde_json::Value> {
    account_profile::hosted(fixture, password, case)
}

pub(super) fn hosted_consumer_self_check(
    fixture: Fixture,
    password: &str,
    case: &str,
) -> Result<()> {
    account_resume::hosted(fixture, password, case)
}

#[path = "account_paired_account_crash_tests.rs"]
mod account_crash;

pub(super) fn hosted_account_crash(fixture: Fixture, password: &str, point: &str) -> Result<()> {
    account_crash::run(fixture, password, point)
}

pub(super) fn hosted_account_crash_broker(password: &str) {
    account_crash::broker(password);
}

pub(super) fn hosted_account_pair(fixture: Fixture, password: &str, case: &str) -> Result<()> {
    account::hosted(fixture, password, case)
}

struct PairFixture {
    image: PinnedRunnerImage,
    contained_image_access: Option<contained_image::Proof>,
    active: ActiveExecution<'static>,
    db: PrivateDatabase<'static>,
    ready: ReadyInstallation,
    owner_server: Option<ControlServer>,
    consumer_server: Option<ControlServer>,
    owner_address: ControlAddress,
    consumer_address: ControlAddress,
    _fixture: Fixture,
}
impl PairFixture {
    fn new() -> Self {
        let fixture = protected_fixture();
        Self::from_fixture(fixture)
    }
    fn from_fixture(fixture: Fixture) -> Self {
        Self::from_fixture_image(fixture, false)
    }
    fn from_fixture_image(fixture: Fixture, production_image: bool) -> Self {
        Self::from_fixture_image_mode(fixture, production_image, AccountMode::Offline)
    }
    fn from_fixture_image_mode(
        fixture: Fixture,
        production_image: bool,
        mode: AccountMode,
    ) -> Self {
        let db = PrivateDatabase::open_under(&fixture.root, &fixture.setup, mode, None).unwrap();
        let ready = ReadyInstallation::fixture(fixture.setup.clone(), &fixture.root).unwrap();
        let pending = fixture.pending(true).unwrap();
        let identity = CandidateJournalIdentity::new(
            fixture.setup.binding(mode).unwrap(),
            format!("Alysis.Execution.{}", random_id().unwrap()),
        )
        .unwrap();
        let mut record = fixture_record(identity);
        record.scope = PAIRED_PLAN_SCOPE.into();
        record.job = crate::job::paired_execution_name(&record.identity.container, false).unwrap();
        record.database_id = db.file_identity();
        record.pending_id = pending.file.id.as_bytes();
        record
            .validate(&record.identity.binding, db.file_identity())
            .unwrap();
        pending.write_new(&record).unwrap();
        let active = ActiveExecution {
            pending,
            record,
            _lease: None,
        };
        let (image, contained_image_access) = if production_image {
            if crate::account_rpc_v3_lifecycle_fixture::contained_mode()
                .unwrap()
                .is_some()
            {
                let (image, proof) =
                    contained_image::prepare(&ready, &active.record.identity).unwrap();
                (image, Some(proof))
            } else {
                (PinnedRunnerImage::prepare(&ready).unwrap(), None)
            }
        } else {
            (PinnedRunnerImage::fixture_installed(&ready).unwrap(), None)
        };
        let account = ready.binding(mode).unwrap().account_sid;
        let (owner_server, owner_address) = ControlServer::prepare(&account).unwrap();
        let (consumer_server, consumer_address) = ControlServer::prepare(&account).unwrap();
        Self {
            image,
            contained_image_access,
            active,
            db,
            ready,
            owner_server: Some(owner_server),
            consumer_server: Some(consumer_server),
            owner_address,
            consumer_address,
            _fixture: fixture,
        }
    }
    fn plan(&self) -> CommittedPlan<'_, 'static> {
        CommittedPlan::prepare(
            &self.db,
            &self.active,
            &self.image,
            &self.ready,
            &self.owner_address,
            &self.consumer_address,
        )
        .unwrap()
    }
    fn plan_rpc_v3_lifecycle(&self) -> CommittedPlan<'_, 'static> {
        CommittedPlan::prepare_rpc_v3_lifecycle_fixture(
            &self.db,
            &self.active,
            &self.image,
            &self.ready,
            &self.owner_address,
            &self.consumer_address,
        )
        .unwrap()
    }
    fn plan_rpc_v3_shadow_lifecycle(&self) -> CommittedPlan<'_, 'static> {
        CommittedPlan::prepare_rpc_v3_shadow_lifecycle_fixture(
            &self.db,
            &self.active,
            &self.image,
            &self.ready,
            &self.owner_address,
            &self.consumer_address,
        )
        .unwrap()
    }
    fn rows(&self, role: Role) -> usize {
        self.db
            .connection()
            .unwrap()
            .query_row(
                "SELECT count(*) FROM candidate_paired_bootstrap_events WHERE role=?1",
                [role.index()],
                |r| r.get(0),
            )
            .unwrap()
    }
    fn name(&self, role: Role) -> String {
        crate::job::paired_execution_name(
            &self.active.record.identity.container,
            role == Role::Consumers,
        )
        .unwrap()
    }
}
fn counts(name: &str) -> Option<(u32, u32)> {
    let handle = match unsafe { OpenJobObjectW(JOB_OBJECT_QUERY, false, pcwstr(&wstr(name))) } {
        Ok(handle) => OwnedHandle(handle),
        Err(error) => {
            assert_eq!(error.code(), ERROR_FILE_NOT_FOUND.to_hresult());
            return None;
        }
    };
    let mut info = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION::default();
    let mut returned = 0;
    unsafe {
        QueryInformationJobObject(
            Some(handle.raw()),
            JobObjectBasicAccountingInformation,
            (&mut info as *mut JOBOBJECT_BASIC_ACCOUNTING_INFORMATION).cast(),
            size_of::<JOBOBJECT_BASIC_ACCOUNTING_INFORMATION>() as u32,
            Some(&mut returned),
        )
        .unwrap();
    }
    assert_eq!(
        returned as usize,
        size_of::<JOBOBJECT_BASIC_ACCOUNTING_INFORMATION>()
    );
    Some((info.ActiveProcesses, info.TotalProcesses))
}

fn diagnose_job_processes(name: &str) {
    let job = OwnedHandle(unsafe {
        OpenJobObjectW(JOB_OBJECT_QUERY, false, pcwstr(&wstr(name))).unwrap()
    });
    let mut buffer = vec![0usize; 66];
    unsafe {
        QueryInformationJobObject(
            Some(job.raw()),
            JobObjectBasicProcessIdList,
            buffer.as_mut_ptr().cast(),
            (buffer.len() * size_of::<usize>()) as u32,
            None,
        )
        .unwrap();
    }
    let header = unsafe { &*buffer.as_ptr().cast::<JOBOBJECT_BASIC_PROCESS_ID_LIST>() };
    assert!(header.NumberOfProcessIdsInList <= 64);
    let ids = unsafe {
        std::slice::from_raw_parts(
            header.ProcessIdList.as_ptr(),
            header.NumberOfProcessIdsInList as usize,
        )
    };
    let mut names = Vec::new();
    for id in ids {
        let process = OwnedHandle(unsafe {
            OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, false, *id as u32).unwrap()
        });
        let mut image = vec![0u16; 32768];
        let mut length = image.len() as u32;
        unsafe {
            QueryFullProcessImageNameW(
                process.raw(),
                PROCESS_NAME_WIN32,
                windows::core::PWSTR(image.as_mut_ptr()),
                &mut length,
            )
            .unwrap();
        }
        names.push(String::from_utf16(&image[..length as usize]).unwrap());
    }
    eprintln!("paired fixed family original-job image inventory: {names:?}");
}
fn retain_original(process: HANDLE) -> OwnedHandle {
    let mut copy = HANDLE::default();
    unsafe {
        DuplicateHandle(
            GetCurrentProcess(),
            process,
            GetCurrentProcess(),
            &mut copy,
            (PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SYNCHRONIZE).0,
            false,
            DUPLICATE_HANDLE_OPTIONS(0),
        )
        .unwrap();
    }
    OwnedHandle(copy)
}
fn alive(process: &OwnedHandle) {
    assert_eq!(
        unsafe { WaitForSingleObject(process.raw(), 0) },
        WAIT_TIMEOUT
    );
}
fn dead(process: &OwnedHandle) {
    assert_eq!(
        unsafe { WaitForSingleObject(process.raw(), 5000) },
        WAIT_OBJECT_0
    );
}

fn exited_code(process: &OwnedHandle) -> u32 {
    dead(process);
    let mut code = 0;
    unsafe {
        GetExitCodeProcess(process.raw(), &mut code).unwrap();
    }
    code
}

fn require_readonly(error: &anyhow::Error) {
    assert!(error.chain().any(|cause| matches!(
        cause.downcast_ref::<rusqlite::Error>(),
        Some(rusqlite::Error::SqliteFailure(code, _)) if code.code == rusqlite::ErrorCode::ReadOnly
    )), "expected injected SQLite ReadOnly failure: {error:#}");
}

#[test]
fn paired_native_consumers_retire_before_original_owner_and_keep_strict_jobs() {
    let mut fixture = PairFixture::new();
    let owner_server = fixture.owner_server.take().unwrap();
    let consumer_server = fixture.consumer_server.take().unwrap();
    let plan = fixture.plan();
    let jobs = plan.create_jobs().unwrap();
    assert!(plan.create_jobs().is_err());
    let owner = jobs.prepare_bootstrap(Role::Owner).unwrap();
    let owner_ref = owner.fixture_readonly_observer().unwrap();
    let consumer = jobs.prepare_bootstrap(Role::Consumers).unwrap();
    let consumer_ref = consumer.fixture_readonly_observer().unwrap();
    let owner = owner.start(owner_server, Duration::from_secs(5)).unwrap();
    let consumer = consumer
        .start(consumer_server, Duration::from_secs(5))
        .unwrap();
    alive(&owner_ref);
    alive(&consumer_ref);
    assert_eq!(fixture.rows(Role::Owner), 7);
    assert_eq!(fixture.rows(Role::Consumers), 7);
    assert!(jobs.prepare_bootstrap(Role::Owner).is_err());
    assert!(jobs.prepare_bootstrap(Role::Consumers).is_err());
    let completed = jobs.retire_consumers(consumer).unwrap();
    dead(&consumer_ref);
    alive(&owner_ref);
    assert_eq!(counts(&fixture.name(Role::Consumers)).unwrap().0, 0);
    assert!(counts(&fixture.name(Role::Owner)).unwrap().0 > 0);
    assert_eq!(fixture.rows(Role::Consumers), 9);
    assert_eq!(fixture.rows(Role::Owner), 7);
    let pair = jobs.retire_owner(owner, completed).unwrap();
    pair.verify().unwrap();
    dead(&owner_ref);
    assert_eq!(fixture.rows(Role::Owner), 9);
    drop(pair);
    drop(owner_ref);
    drop(consumer_ref);
    drop(jobs);
    assert_eq!(counts(&fixture.name(Role::Consumers)), None);
    assert_eq!(counts(&fixture.name(Role::Owner)), None);
    assert!(
        fixture._fixture.path().is_file(),
        "paired completion is not marker/profile cleanup authority"
    );
}

#[test]
fn paired_native_retirement_drains_child_and_grandchild_before_owner() {
    let mut fixture = PairFixture::new();
    let owner_server = fixture.owner_server.take().unwrap();
    let consumer_server = fixture.consumer_server.take().unwrap();
    let plan = fixture.plan();
    let jobs = plan.create_jobs().unwrap();
    let owner = jobs.prepare_bootstrap(Role::Owner).unwrap();
    let owner_ref = owner.fixture_readonly_observer().unwrap();
    let consumer = jobs.prepare_family_fixture().unwrap();
    let consumer_ref = consumer.fixture_readonly_observer().unwrap();
    let owner = owner.start(owner_server, Duration::from_secs(5)).unwrap();
    let mut consumer = consumer
        .start(consumer_server, Duration::from_secs(5))
        .unwrap();
    let family = consumer.fixture_family_references().unwrap();
    assert_eq!(family.len(), 2);
    for process in &family {
        alive(process);
    }
    diagnose_job_processes(&fixture.name(Role::Consumers));
    diagnose_job_processes(&fixture.name(Role::Owner));
    let before = counts(&fixture.name(Role::Consumers)).unwrap();
    // Windows may add native console hosts. All are still subject to the
    // original full-job drain, never an allowlist or exemption by image name.
    assert!(before.0 >= 3 && before.1 >= before.0);
    assert!(counts(&fixture.name(Role::Owner)).unwrap().0 >= 1);
    let deadline = std::time::Instant::now() + Duration::from_secs(10);
    let completed = jobs.retire_consumers(consumer).unwrap();
    assert_eq!(exited_code(&consumer_ref), 1);
    for process in &family {
        assert_eq!(
            exited_code(process),
            1,
            "family exited naturally or by watchdog"
        );
    }
    assert!(std::time::Instant::now() < deadline);
    alive(&owner_ref);
    let after = counts(&fixture.name(Role::Consumers)).unwrap();
    assert_eq!(after.0, 0, "whole job including OS helpers must drain");
    assert!(after.1 >= before.1);
    jobs.retire_owner(owner, completed)
        .unwrap()
        .verify()
        .unwrap();
    dead(&owner_ref);
    assert_eq!(fixture.rows(Role::Consumers), 9);
    assert_eq!(fixture.rows(Role::Owner), 9);
}

#[test]
fn paired_native_failed_second_job_receipt_drains_both_and_preserves_intent() {
    let fixture = PairFixture::new();
    let plan = fixture.plan();
    let hits = Rc::new(Cell::new(0));
    let observed_hits = hits.clone();
    NATIVE_CHECKPOINT.with(|s| {
        *s.borrow_mut() = Some(Box::new(move |phase, role, sql, _| {
            if phase == "job-created" && role == Role::Consumers {
                observed_hits.set(observed_hits.get() + 1);
                sql.execute_batch("PRAGMA query_only=ON").unwrap();
            }
        }))
    });
    let result = plan.create_jobs();
    NATIVE_CHECKPOINT.with(|s| {
        s.borrow_mut().take();
    });
    assert!(result.is_err());
    assert_eq!(hits.get(), 1);
    require_readonly(result.as_ref().err().unwrap());
    assert!(plan.create_jobs().is_err());
    fixture
        .db
        .connection()
        .unwrap()
        .execute_batch("PRAGMA query_only=OFF")
        .unwrap();
    assert_eq!(fixture.rows(Role::Owner), 2);
    assert_eq!(fixture.rows(Role::Consumers), 1);
    assert_eq!(counts(&fixture.name(Role::Owner)), None);
    assert_eq!(counts(&fixture.name(Role::Consumers)), None);
}

#[test]
fn paired_native_consumer_write_failures_stop_original_child_without_retiring_owner() {
    for (at, expected) in [
        ("bootstrap-born", 3),
        ("bootstrap-resumed", 5),
        ("bootstrap-ready", 6),
        ("job-drained", 8),
    ] {
        let mut fixture = PairFixture::new();
        let owner_server = fixture.owner_server.take().unwrap();
        let consumer_server = fixture.consumer_server.take().unwrap();
        let plan = fixture.plan();
        let jobs = plan.create_jobs().unwrap();
        let owner = jobs.prepare_bootstrap(Role::Owner).unwrap();
        let owner_ref = owner.fixture_readonly_observer().unwrap();
        let owner = owner.start(owner_server, Duration::from_secs(5)).unwrap();
        let retained: Rc<RefCell<Option<OwnedHandle>>> = Rc::new(RefCell::new(None));
        let copy = retained.clone();
        let hits = Rc::new(Cell::new(0));
        let observed_hits = hits.clone();
        NATIVE_CHECKPOINT.with(|s| {
            *s.borrow_mut() = Some(Box::new(move |phase, role, sql, process| {
                if role == Role::Consumers && phase == "bootstrap-born" {
                    *copy.borrow_mut() = process.map(retain_original);
                }
                if role == Role::Consumers && phase == at {
                    observed_hits.set(observed_hits.get() + 1);
                    sql.execute_batch("PRAGMA query_only=ON").unwrap();
                }
            }))
        });
        let result = jobs
            .prepare_bootstrap(Role::Consumers)
            .and_then(|child| child.start(consumer_server, Duration::from_secs(5)))
            .and_then(|running| jobs.retire_consumers(running));
        NATIVE_CHECKPOINT.with(|s| {
            s.borrow_mut().take();
        });
        assert!(result.is_err(), "consumer unexpectedly completed at {at}");
        assert_eq!(
            hits.get(),
            1,
            "injected checkpoint was not reached exactly once at {at}"
        );
        require_readonly(result.as_ref().err().unwrap());
        dead(retained.borrow().as_ref().unwrap());
        alive(&owner_ref);
        fixture
            .db
            .connection()
            .unwrap()
            .execute_batch("PRAGMA query_only=OFF")
            .unwrap();
        assert_eq!(
            fixture.rows(Role::Consumers),
            expected,
            "wrong preserved prefix at {at}"
        );
        assert_eq!(fixture.rows(Role::Owner), 7);
        assert!(jobs.prepare_bootstrap(Role::Consumers).is_err());
        drop(result);
        drop(owner);
        drop(jobs);
        dead(&owner_ref);
    }
}

#[test]
fn paired_failed_retirement_intent_drains_entire_family_while_owner_survives() {
    let mut fixture = PairFixture::new();
    let owner_server = fixture.owner_server.take().unwrap();
    let consumer_server = fixture.consumer_server.take().unwrap();
    let marker = std::fs::read(fixture._fixture.path()).unwrap();
    let plan = fixture.plan();
    let jobs = plan.create_jobs().unwrap();
    let owner = jobs.prepare_bootstrap(Role::Owner).unwrap();
    let owner_ref = owner.fixture_readonly_observer().unwrap();
    let owner = owner.start(owner_server, Duration::from_secs(5)).unwrap();
    let consumer = jobs.prepare_family_fixture().unwrap();
    let consumer_ref = consumer.fixture_readonly_observer().unwrap();
    let mut consumer = consumer
        .start(consumer_server, Duration::from_secs(5))
        .unwrap();
    let family = consumer.fixture_family_references().unwrap();
    assert_eq!(family.len(), 2);
    for process in &family {
        alive(process);
    }
    fixture
        .db
        .connection()
        .unwrap()
        .execute_batch("PRAGMA query_only=ON")
        .unwrap();
    let error = jobs
        .retire_consumers(consumer)
        .err()
        .expect("read-only retirement unexpectedly passed");
    require_readonly(&error);
    fixture
        .db
        .connection()
        .unwrap()
        .execute_batch("PRAGMA query_only=OFF")
        .unwrap();
    // Keep both original job owners in scope. Outer Drop must not rescue this.
    assert_eq!(
        counts(&fixture.name(Role::Consumers)).unwrap().0,
        0,
        "retirement refusal left descendants alive"
    );
    dead(&consumer_ref);
    for process in &family {
        assert_eq!(exited_code(process), 1);
    }
    alive(&owner_ref);
    assert_eq!(fixture.rows(Role::Consumers), 7);
    assert_eq!(fixture.rows(Role::Owner), 7);
    assert_eq!(std::fs::read(fixture._fixture.path()).unwrap(), marker);
    drop(owner);
    drop(jobs);
}

#[test]
fn paired_native_swapped_address_refuses_before_resume_and_preserves_both_births() {
    let mut fixture = PairFixture::new();
    let wrong_server = fixture.owner_server.take().unwrap();
    let plan = fixture.plan();
    let jobs = plan.create_jobs().unwrap();
    let owner = jobs.prepare_bootstrap(Role::Owner).unwrap();
    let owner_ref = owner.fixture_readonly_observer().unwrap();
    let consumer = jobs.prepare_bootstrap(Role::Consumers).unwrap();
    let consumer_ref = consumer.fixture_readonly_observer().unwrap();
    assert!(
        consumer
            .start(wrong_server, Duration::from_secs(5))
            .is_err()
    );
    dead(&consumer_ref);
    alive(&owner_ref);
    assert_eq!(fixture.rows(Role::Consumers), 4);
    assert_eq!(fixture.rows(Role::Owner), 4);
    drop(owner);
    drop(jobs);
    dead(&owner_ref);
}

#[test]
fn paired_scope_refuses_single_job_authority_and_preserves_marker() {
    let fixture = PairFixture::new();
    assert!(
        bootstrap_plan::CommittedPlan::prepare(
            &fixture.db,
            &fixture.active,
            &fixture.image,
            &fixture.ready,
            &fixture.owner_address
        )
        .is_err()
    );
    assert!(fixture.active.acl_record().is_err());
    let before = std::fs::read(fixture._fixture.path()).unwrap();
    let PairFixture {
        active,
        _fixture: fixture,
        ..
    } = fixture;
    assert!(active.stop().is_err());
    assert_eq!(std::fs::read(fixture.path()).unwrap(), before);
}
