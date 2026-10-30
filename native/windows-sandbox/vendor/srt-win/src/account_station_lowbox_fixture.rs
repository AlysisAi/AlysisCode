//! Fixed disposable station permission diagnostic. Serialized data is never a
//! launch authority: the broker must observe the original suspended process.
use super::*;
use crate::account_profile_filesystem::{FixedLowboxFilesystemProof, FixedLowboxFilesystemSubject};
use crate::account_state::execution::resources::fixture_rpc_lowbox as rpc_boundary;
use crate::account_state::image::{PinnedRunnerImage, account::AccountImageObserver};
use bootstrap::entry::credential_wire::child::{
    FixtureStationLowboxBinding, Purpose, probe::Request,
};
use serde::{Deserialize, Serialize};

fn rpc_subject(request: &Request) -> Result<rpc_boundary::Subject> {
    let b = binding(request)?;
    Ok(rpc_boundary::Subject {
        policy: b.rpc_policy()?,
        network: network(request),
        owner: request.birth.selection.account.owner_sid.clone(),
        account: request.birth.selection.account.account_sid.clone(),
        container: b.profile.container.clone(),
        package: b.profile.package_sid.clone(),
        session: request.birth.selection.session,
    })
}

#[path = "account_desktop_lowbox_probe.rs"]
mod desktop;
#[path = "account_station_https_probe.rs"]
mod https;
#[path = "account_station_lowbox_loader.rs"]
mod loader;
#[path = "account_station_logon_observation.rs"]
mod logon_observation;
#[path = "account_station_lowbox_native.rs"]
mod native;
#[path = "account_lowbox_rpc_error.rs"]
mod rpc_error;
#[path = "account_lowbox_schannel_probe.rs"]
mod schannel;
#[path = "account_station_lowbox_startup.rs"]
mod startup;
#[path = "account_lowbox_token_observation.rs"]
mod token_observation;
const LIMIT: usize = 32768;
const TIMEOUT: Duration = Duration::from_secs(5);
// Source-pinned, admitted disposable fixture only. Normal plan preparation and
// the production backend stay unchanged. Exactly one diagnostic may be selected.
pub(crate) const RPC_CONSTRUCTOR_COMPARISON: bool = false;
pub(crate) const RPC_SYSTEM_INHERITANCE_COMPARISON: bool = false;
// Run38004063063 reaches registered NSI but fails at the contained transport.
// This separate policy tests only exact NSI service-SID read inheritance.
pub(crate) const RPC_NSI_SYSTEM_INHERITANCE_COMPARISON: bool = true;
pub(crate) const HTTPS_COMPARISON: bool = true;
// Immutable per tested source revision, never selected by child input or an
// environment variable. Both account enablement and token capabilities follow it.
const HTTPS_ACCOUNT_MODE: crate::account_install::AccountMode =
    crate::account_install::AccountMode::Offline;
pub(crate) fn control_timeout() -> Duration {
    Duration::from_secs(10)
}
fn child_timeout() -> Duration {
    if HTTPS_COMPARISON {
        control_timeout()
    } else {
        TIMEOUT
    }
}
pub(crate) fn require_source_selection() -> Result<()> {
    // Validate selected diagnostic waits against the real shared control API
    // before the hosted entry creates any fixture accounts or native resources.
    super::deadline(control_timeout())?;
    super::deadline(child_timeout())?;
    let policy =
        crate::account_state::execution::resources::FixtureRpcPolicy::selected_fixture_policy()?;
    ensure!(
        !HTTPS_COMPARISON || matches!(policy.fixture_binding_policy(), 3 | 5),
        "HTTPS comparison requires the source-selected SYSTEM inheritance policy"
    );
    Ok(())
}
pub(crate) fn fixture_mode() -> crate::account_install::AccountMode {
    if HTTPS_COMPARISON {
        HTTPS_ACCOUNT_MODE
    } else {
        crate::account_install::AccountMode::Offline
    }
}
fn network(request: &Request) -> crate::appcontainer::NetworkAccess {
    match request.birth.selection.account.mode {
        crate::account_install::AccountMode::Offline => crate::appcontainer::NetworkAccess::Off,
        crate::account_install::AccountMode::Online => crate::appcontainer::NetworkAccess::On,
    }
}
pub(crate) const EXIT: u32 = 0x4160;
type Reference = (u64, u32, u64);
type Stamp = ([u32; 2], [u32; 2], [u32; 2]);
thread_local! {
    pub(crate) static BROKER_STAGE: std::cell::Cell<&'static str> = const {std::cell::Cell::new("not_started")};
    pub(crate) static ENABLED: std::cell::Cell<bool> = const {std::cell::Cell::new(false)};
    pub(crate) static SHADOW_BORN: std::cell::Cell<bool> = const {std::cell::Cell::new(false)};
    pub(crate) static RECEIPT: std::cell::RefCell<Option<Receipt>> = const {std::cell::RefCell::new(None)};
    pub(crate) static LOGON_OBSERVATION: std::cell::RefCell<Option<logon_observation::Observation>> = const {std::cell::RefCell::new(None)};
    // A diagnostic captured before fallible debugger cleanup is kept separately
    // from the terminal receipt and can never satisfy a successful fixture.
    pub(crate) static CHECKPOINT: std::cell::RefCell<Option<Receipt>> = const {std::cell::RefCell::new(None)};
}
#[test]
fn station_https_selected_timeouts_obey_original_control_contract() {
    require_source_selection().unwrap();
    assert!(super::deadline(control_timeout()).is_ok());
    assert!(super::deadline(child_timeout()).is_ok());
    // These were invalid assumptions in the first HTTPS fixture. The common
    // protocol must continue to reject them, including in an HTTPS test build.
    for seconds in [35, 60] {
        assert!(super::deadline(Duration::from_secs(seconds)).is_err());
    }
}
pub(crate) fn binding(request: &Request) -> Result<&FixtureStationLowboxBinding> {
    request.validate()?;
    let Purpose::FixtureStationLowboxV1 { binding } = &request.birth.purpose else {
        anyhow::bail!("fixed station lowbox purpose missing")
    };
    binding.validate_for(&request.birth.selection, &request.birth.nonce)?;
    binding.require_resume_nonce(&request.nonce)?;
    Ok(binding)
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ChildProof {
    pub(super) passed: bool,
    pub(super) stage: String,
    pub(super) hresult: Option<i32>,
    pub(super) positive_open: bool,
    pub(super) native_name_verified: bool,
    pub(super) open_denials: Vec<(u32, i32)>,
    pub(super) set_user_dacl: Option<i32>,
    pub(super) set_user_owner: Option<i32>,
    pub(super) nt_set_dacl: Option<i32>,
    pub(super) nt_set_owner: Option<i32>,
    pub(super) attached_same_object: bool,
    pub(super) attached_set_user_dacl: Option<i32>,
    pub(super) attached_set_user_owner: Option<i32>,
    pub(super) attached_nt_set_dacl: Option<i32>,
    pub(super) attached_nt_set_owner: Option<i32>,
}
impl ChildProof {
    fn new() -> Self {
        Self {
            passed: false,
            stage: "token".into(),
            hresult: None,
            positive_open: false,
            native_name_verified: false,
            open_denials: Vec::new(),
            set_user_dacl: None,
            set_user_owner: None,
            nt_set_dacl: None,
            nt_set_owner: None,
            attached_same_object: false,
            attached_set_user_dacl: None,
            attached_set_user_owner: None,
            attached_nt_set_dacl: None,
            attached_nt_set_owner: None,
        }
    }
    fn require(&self) -> Result<()> {
        ensure!(
            [
                "token",
                "attached",
                "allowed_open",
                "denied_open",
                "set_user_dacl",
                "set_user_owner",
                "nt_set_dacl",
                "nt_set_owner",
                "attached_set_user_dacl",
                "attached_set_user_owner",
                "attached_nt_set_dacl",
                "attached_nt_set_owner",
                "complete"
            ]
            .contains(&self.stage.as_str()),
            "unknown lowbox probe stage"
        );
        ensure!(
            self.open_denials.len() <= 3,
            "excess station denial observations"
        );
        if self.passed {
            let denied = windows::core::HRESULT::from_win32(ERROR_ACCESS_DENIED.0).0;
            ensure!(
                self.stage == "complete"
                    && self.hresult.is_none()
                    && self.positive_open
                    && self.native_name_verified
                    && self.open_denials
                        == [(0x40000, denied), (0x80000, denied), (0xc0000, denied)]
                    && self.set_user_dacl == Some(denied)
                    && self.set_user_owner == Some(denied)
                    && self.nt_set_dacl == Some(0xc0000022u32 as i32)
                    && self.nt_set_owner == Some(0xc0000022u32 as i32)
                    && self.attached_same_object
                    && self.attached_set_user_dacl == Some(denied)
                    && self.attached_set_user_owner == Some(denied)
                    && self.attached_nt_set_dacl == Some(0xc0000022u32 as i32)
                    && self.attached_nt_set_owner == Some(0xc0000022u32 as i32),
                "station mutation refusal proof incomplete"
            );
        } else {
            ensure!(self.stage != "complete", "failed child claims completion");
        }
        Ok(())
    }
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct ChildFailure {
    wait_status: u32,
    process_code: Option<u32>,
    process_query_hresult: Option<i32>,
    thread_code: Option<u32>,
    thread_query_hresult: Option<i32>,
}

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Receipt {
    request: Request,
    token_stamp: Stamp,
    pub(crate) passed: bool,
    pub(crate) stage: String,
    pub(crate) hresult: Option<i32>,
    original_station: Option<String>,
    original_folder_verified: bool,
    station_name: Option<String>,
    desktop_name: Option<String>,
    station_before: Option<String>,
    station_after: Option<String>,
    desktop_before: Option<String>,
    desktop_after: Option<String>,
    desktop_closed: bool,
    station_closed: bool,
    original_associations_unchanged: bool,
    shared_descriptor_unchanged: bool,
    original_child: Option<(u32, u64)>,
    original_child_exit: Option<u32>,
    headless_error_mode: Option<u32>,
    child_started: bool,
    startup: Option<startup::Probe>,
    token_observation: Option<token_observation::Comparison>,
    user_access: Option<native::user_access::Probe>,
    inherited_user: Option<native::inherited_user::Proof>,
    #[serde(default)]
    security_capabilities_birth: bool,
    #[serde(default)]
    kernelbase_token_birth: bool,
    #[serde(default)]
    rpc_system_inheritance: bool,
    #[serde(default)]
    rpc_nsi_system_inheritance: bool,
    #[serde(default)]
    v3_shadow_bounded: bool,
    #[serde(default)]
    kernelbase_constructor_returned: bool,
    #[serde(default)]
    constructor_rpc_unchanged_before_birth: bool,
    #[serde(default)]
    constructor_directories: Vec<(String, u32, i32, bool)>,
    #[serde(default)]
    constructor_links: Vec<(String, u32, i32, bool)>,
    #[serde(default)]
    constructor_directory_upgrades: Vec<(String, u32, i32, bool)>,
    #[serde(default)]
    captured_namespace_birth: bool,
    #[serde(default)]
    captured_namespace: Option<crate::account_token::capture::Proof>,
    #[serde(default)]
    documented_desktop_birth: bool,
    #[serde(default)]
    desktop_creator_restored: bool,
    #[serde(default)]
    inherited_desktop_selection: bool,
    #[serde(default)]
    appcontainer_without_inherited_user: bool,
    #[serde(default)]
    creation_failure_ntstatus: Option<[i32; 2]>,
    loader: Option<loader::LoaderTrace>,
    child_failure: Option<ChildFailure>,
    child: Option<ChildProof>,
    desktop_child: Option<desktop::DesktopProof>,
    schannel_child: Option<schannel::Proof>,
    #[serde(default)]
    rpc_child: Option<rpc_boundary::Proof>,
    #[serde(default)]
    rpc_original_closed_unchanged: bool,
    raw_schannel_control: Option<schannel::Proof>,
    #[serde(default)]
    https_child: Option<https::Proof>,
    #[serde(default)]
    raw_https_control: Option<https::Proof>,
    #[serde(default)]
    filesystem_subject: Option<FixedLowboxFilesystemSubject>,
    #[serde(default)]
    filesystem_child: Option<FixedLowboxFilesystemProof>,
    #[serde(default)]
    filesystem_parent_verified: bool,
    #[serde(default)]
    family: Option<native::family::Proof>,
    #[serde(default)]
    family_root_hresult: Option<i32>,
    family_root_stage: Option<native::family::RootStage>,
    #[serde(default)]
    filtered_primary_control: Option<native::filtered_primary::Observation>,
}
impl Receipt {
    fn new(request: &Request, token_stamp: Stamp) -> Result<Self> {
        binding(request)?;
        Ok(Self {
            request: request.clone(),
            token_stamp,
            passed: false,
            stage: "original".into(),
            hresult: None,
            original_station: None,
            original_folder_verified: false,
            station_name: None,
            desktop_name: None,
            station_before: None,
            station_after: None,
            desktop_before: None,
            desktop_after: None,
            desktop_closed: false,
            station_closed: false,
            original_associations_unchanged: false,
            shared_descriptor_unchanged: false,
            original_child: None,
            original_child_exit: None,
            headless_error_mode: None,
            child_started: false,
            startup: None,
            token_observation: None,
            user_access: None,
            inherited_user: None,
            security_capabilities_birth: false,
            kernelbase_token_birth: false,
            rpc_system_inheritance: false,
            rpc_nsi_system_inheritance: false,
            v3_shadow_bounded: false,
            kernelbase_constructor_returned: false,
            constructor_rpc_unchanged_before_birth: false,
            constructor_directories: Vec::new(),
            constructor_links: Vec::new(),
            constructor_directory_upgrades: Vec::new(),
            captured_namespace_birth: false,
            captured_namespace: None,
            documented_desktop_birth: false,
            desktop_creator_restored: false,
            inherited_desktop_selection: false,
            appcontainer_without_inherited_user: false,
            creation_failure_ntstatus: None,
            loader: None,
            child_failure: None,
            child: None,
            desktop_child: None,
            schannel_child: None,
            rpc_child: None,
            rpc_original_closed_unchanged: false,
            raw_schannel_control: None,
            https_child: None,
            raw_https_control: None,
            filesystem_subject: None,
            filesystem_child: None,
            filesystem_parent_verified: false,
            family: None,
            family_root_hresult: None,
            family_root_stage: None,
            filtered_primary_control: None,
        })
    }
    fn require(&self, request: &Request, stamp: Stamp) -> Result<()> {
        let selected = binding(request)?;
        let v3 = selected.is_v3_shadow();
        ensure!(
            !self.v3_shadow_bounded || v3,
            "V3 completion flag lacks explicit binding"
        );
        ensure!(
            !v3 || self.original_child.is_none() || self.v3_shadow_bounded,
            "V3 child lacks original completion identity"
        );
        ensure!(
            !v3 || !(self.rpc_system_inheritance
                || self.rpc_nsi_system_inheritance
                || self.kernelbase_token_birth
                || self.security_capabilities_birth
                || self.captured_namespace_birth
                || self.documented_desktop_birth
                || self.inherited_desktop_selection
                || self.appcontainer_without_inherited_user),
            "V3 completion cannot mix diagnostic constructors"
        );
        ensure!(
            self.request == *request && self.token_stamp == stamp,
            "station receipt original actor differs"
        );
        ensure!(
            [
                "original",
                "profile_root",
                "filesystem_prepare",
                "filesystem_verify",
                "family",
                "create_station",
                "create_desktop",
                "create_lowbox",
                "token_semantics",
                "kernelbase_token",
                "captured_namespace_token",
                "captured_namespace_pipe_open",
                "captured_namespace_pipe_identity",
                "prepare_inherited_user",
                "create_child",
                "child_inherited_user",
                "offer",
                "resume",
                "loader_observation",
                "child_started",
                "child_receipt",
                "child_decode",
                "child_subject",
                "child_proof",
                "child_exit",
                "filtered_primary_control",
                "descriptor",
                "desktop_descriptor",
                "close_desktop",
                "close_station",
                "unchanged",
                "raw_schannel_control",
                "raw_https_control",
                "child_checks",
                "complete"
            ]
            .contains(&self.stage.as_str()),
            "unknown station fixture stage"
        );
        for value in [
            &self.original_station,
            &self.station_name,
            &self.desktop_name,
            &self.station_before,
            &self.station_after,
            &self.desktop_before,
            &self.desktop_after,
        ]
        .into_iter()
        .flatten()
        {
            ensure!(
                !value.is_empty() && value.len() <= 8192 && !value.contains('\0'),
                "station evidence text exceeds bound"
            );
        }
        if let Some(child) = &self.child {
            child.require()?;
        }
        if let Some(child) = &self.desktop_child {
            child.require_subject(
                self.desktop_name
                    .as_deref()
                    .context("desktop proof name absent")?,
                self.desktop_before
                    .as_deref()
                    .context("desktop proof original policy absent")?,
                &request.birth.selection.account.account_sid,
            )?;
        }
        if let Some(proof) = &self.schannel_child {
            proof.require()?;
        }
        if let Some(proof) = &self.rpc_child {
            proof.require(&rpc_subject(request)?)?;
        }
        if let Some(proof) = &self.https_child {
            proof.require()?;
            ensure!(
                (self.rpc_system_inheritance
                    || self.rpc_nsi_system_inheritance
                    || self.v3_shadow_bounded)
                    && self.child.as_ref().is_some_and(|p| p.passed)
                    && self.desktop_child.as_ref().is_some_and(|p| p.passed)
                    && self.schannel_child.as_ref().is_some_and(|p| p.passed)
                    && self.rpc_child.as_ref().is_some_and(|p| p.passed),
                "HTTPS observation precedes original boundary and Schannel checks"
            );
        }
        if let Some(proof) = &self.raw_https_control {
            proof.require()?;
            ensure!(
                self.https_child.is_some()
                    && self.original_child_exit.is_some()
                    && self.desktop_closed
                    && self.station_closed
                    && self.rpc_original_closed_unchanged,
                "raw HTTPS control precedes original child/resource closure"
            );
        }
        if let Some(subject) = &self.filesystem_subject {
            ensure!(
                v3 && self.v3_shadow_bounded,
                "filesystem probe lacks V3 completion"
            );
            subject.require_binding(selected.profile.folder, &selected.profile.use_nonce)?;
        }
        if let Some(proof) = &self.filesystem_child {
            proof.require_subject(
                self.filesystem_subject
                    .as_ref()
                    .context("filesystem subject absent")?,
            )?;
            ensure!(
                self.https_child
                    .as_ref()
                    .is_some_and(|p| p.network_qualified(network(request))),
                "filesystem probe precedes network and original boundary checks"
            );
        }
        ensure!(
            !self.filesystem_parent_verified
                || (self.original_child_exit == Some(EXIT)
                    && self.filesystem_child.as_ref().is_some_and(|p| p.passed)),
            "filesystem verification precedes successful original child exit"
        );
        if let Some(proof) = &self.family {
            ensure!(
                v3 && self.v3_shadow_bounded,
                "descendant proof lacks V3 completion"
            );
            proof.require_original(
                request,
                self.original_child.context("family original root absent")?,
                stamp.1,
            )?;
        }
        ensure!(
            self.family_root_stage.is_some() == self.family_root_hresult.is_some()
                && self.family_root_hresult.is_none_or(|code| code < 0
                    && v3
                    && !self.passed
                    && self.family.as_ref().is_some_and(|p| !p.passed)),
            "family root failure contradicts original completion"
        );
        if self.rpc_original_closed_unchanged {
            ensure!(
                self.original_child_exit.is_some(),
                "RPC pin closed before original child exit"
            );
        }
        if let Some(proof) = &self.raw_schannel_control {
            proof.require()?;
            ensure!(
                self.original_child_exit.is_some()
                    && self.desktop_closed
                    && self.station_closed
                    && self.original_associations_unchanged
                    && self.shared_descriptor_unchanged,
                "raw Schannel control must follow original child exit and USER closure"
            );
        }
        if let Some(proof) = &self.filtered_primary_control {
            proof.require(self)?;
        }
        if let Some(startup) = &self.startup {
            startup.require(self.original_child.context("startup subject absent")?)?;
        }
        if let Some(tokens) = &self.token_observation {
            tokens.require(self.original_child)?;
            ensure!(
                !self.passed || tokens.has_child(),
                "successful diagnostic lacks original token observation"
            );
        }
        if let Some(user_access) = &self.user_access {
            user_access.require()?;
        }
        if let Some(inherited) = &self.inherited_user {
            inherited.require()?;
            ensure!(
                !self.passed,
                "inherited USER startup diagnostic cannot qualify containment"
            );
        }
        if let Some(loader) = &self.loader {
            loader.require()?;
            ensure!(
                !self.passed,
                "loader diagnostics cannot qualify containment"
            );
        }
        ensure!(
            !self.security_capabilities_birth || !self.passed,
            "AppContainer birth comparison cannot qualify containment"
        );
        ensure!(
            !self.kernelbase_token_birth || (!self.passed && !self.security_capabilities_birth),
            "KernelBase comparison cannot qualify containment or combine constructors"
        );
        let constructor_policy = binding(request)?.policy == 2;
        let system_policy = binding(request)?.policy == 3;
        let nsi_policy = binding(request)?.policy == 5;
        ensure!(
            !system_policy || !self.passed,
            "SYSTEM inheritance comparison policy cannot qualify containment"
        );
        ensure!(
            !(self.rpc_system_inheritance || system_policy)
                || (system_policy
                    && !self.kernelbase_token_birth
                    && !self.security_capabilities_birth
                    && !self.captured_namespace_birth),
            "SYSTEM inheritance comparison lacks its exclusive immutable policy"
        );
        ensure!(
            !system_policy || self.original_child.is_none() || self.rpc_system_inheritance,
            "SYSTEM inheritance child lacks original comparison identity"
        );
        ensure!(
            !nsi_policy || !self.passed,
            "NSI inheritance comparison policy cannot qualify containment"
        );
        ensure!(
            !(self.rpc_nsi_system_inheritance || nsi_policy)
                || (nsi_policy
                    && !self.rpc_system_inheritance
                    && !self.kernelbase_token_birth
                    && !self.security_capabilities_birth
                    && !self.captured_namespace_birth),
            "NSI inheritance comparison lacks its exclusive immutable policy"
        );
        ensure!(
            !nsi_policy || self.original_child.is_none() || self.rpc_nsi_system_inheritance,
            "NSI inheritance child lacks original comparison identity"
        );
        ensure!(
            !constructor_policy || !self.passed,
            "KernelBase comparison policy cannot qualify containment"
        );
        ensure!(
            !self.kernelbase_constructor_returned || self.kernelbase_token_birth,
            "constructor return observation lacks original comparison"
        );
        ensure!(
            !self.kernelbase_token_birth || binding(request)?.policy == 2,
            "constructor comparison lacks its immutable RPC policy binding"
        );
        ensure!(
            !self.constructor_rpc_unchanged_before_birth || self.kernelbase_constructor_returned,
            "constructor RPC observation precedes constructor return"
        );
        ensure!(
            !(self.kernelbase_token_birth || constructor_policy)
                || self.original_child.is_none()
                || (self.kernelbase_token_birth
                    && self.kernelbase_constructor_returned
                    && self.constructor_rpc_unchanged_before_birth),
            "constructor child lacks unchanged original RPC/root security"
        );
        ensure!(
            !self.captured_namespace_birth
                || (!self.passed
                    && !self.kernelbase_token_birth
                    && !self.security_capabilities_birth),
            "capture subset cannot qualify containment or combine constructors"
        );
        if let Some(proof) = &self.captured_namespace {
            ensure!(
                self.captured_namespace_birth,
                "capture proof lacks original comparison"
            );
            proof.require()?;
        }
        ensure!(
            !self.captured_namespace_birth
                || self.original_child.is_none()
                || self.captured_namespace.is_some(),
            "capture child lacks original object/closure proof"
        );
        if !self.constructor_directories.is_empty() {
            ensure!(
                self.kernelbase_token_birth && self.constructor_directories.len() == 6,
                "constructor directory observation scope differs"
            );
            for ((name, access, status, closed), (expected, mask)) in
                self.constructor_directories.iter().zip([
                    ("package", 0x0f),
                    ("private_rpc", 0x0f),
                    ("shared_base", 0x0f),
                    ("shared_rpc", 0x0f),
                    ("session_base", 0x20003),
                    ("session_appcontainers", 0x0f),
                ])
            {
                ensure!(
                    name == expected && *access == mask && *closed == (*status >= 0),
                    "constructor directory result or closure differs"
                );
            }
        }
        if !self.constructor_directory_upgrades.is_empty() {
            ensure!(
                self.kernelbase_token_birth && self.constructor_directory_upgrades.len() == 2,
                "constructor directory upgrade scope differs"
            );
            for ((name, access, status, closed), expected) in self
                .constructor_directory_upgrades
                .iter()
                .zip(["package", "private_rpc"])
            {
                ensure!(
                    name == expected && *access == 0xf000f && *closed == (*status >= 0),
                    "constructor directory upgrade result or checked closure differs"
                );
            }
        }
        if !self.constructor_links.is_empty() {
            ensure!(
                self.kernelbase_token_birth && self.constructor_links.len() == 6,
                "constructor link observation scope differs"
            );
            for ((name, access, status, closed), (expected, mask)) in
                self.constructor_links.iter().zip(
                    ["Global", "Local", "Session"]
                        .into_iter()
                        .flat_map(|name| [(name, 1), (name, 0xf0001)]),
                )
            {
                ensure!(
                    name == expected && *access == mask && *closed == (*status >= 0),
                    "constructor link result or checked closure differs"
                );
            }
        }
        ensure!(
            !self.documented_desktop_birth
                || (!self.passed
                    && (self.original_child.is_none() || self.desktop_creator_restored)),
            "documented desktop comparison cannot qualify containment or launch before restoration"
        );
        ensure!(
            !self.inherited_desktop_selection || (!self.passed && self.inherited_user.is_some()),
            "inherited desktop selection is diagnostic only and requires original pair"
        );
        ensure!(
            !self.appcontainer_without_inherited_user
                || (!self.passed
                    && self.security_capabilities_birth
                    && !self.inherited_desktop_selection
                    && self.inherited_user.is_some()),
            "AppContainer handle comparison lacks original policy or claims authority"
        );
        ensure!(
            self.creation_failure_ntstatus.is_none()
                || (!self.passed && self.original_child.is_none()),
            "creation failure status contradicts child admission"
        );
        if self.passed {
            if v3 {
                ensure!(
                    self.v3_shadow_bounded
                        && self.family.as_ref().is_some_and(|p| p.passed)
                        && self.filesystem_parent_verified
                        && self.filesystem_child.as_ref().is_some_and(|p| p.passed)
                        && self
                            .https_child
                            .as_ref()
                            .is_some_and(|p| p.network_qualified(network(request)))
                        && self
                            .raw_https_control
                            .as_ref()
                            .is_some_and(|p| p.succeeded() && p.socket_connected()),
                    "V3 bounded completion requires its exact network proof and raw positives"
                );
            }
            ensure!(
                self.stage == "complete"
                    && self.hresult.is_none()
                    && self.station_name.is_some()
                    && self.desktop_name.is_some()
                    && self.station_before.is_some()
                    && self.station_before == self.station_after
                    && self.desktop_before.is_some()
                    && self.desktop_before == self.desktop_after
                    && self.desktop_child.as_ref().is_some_and(|p| p.passed)
                    && self.schannel_child.as_ref().is_some_and(|p| p.passed)
                    && self.rpc_child.as_ref().is_some_and(|p| p.passed)
                    && self.rpc_original_closed_unchanged
                    && self.raw_schannel_control.as_ref().is_some_and(|p| p.passed)
                    && self.desktop_closed
                    && self.station_closed
                    && self.original_associations_unchanged
                    && self.shared_descriptor_unchanged
                    && self.original_child.is_some()
                    && self.original_folder_verified
                    && self.original_station.is_some()
                    && self.original_child_exit == Some(EXIT)
                    && self.headless_error_mode.is_some_and(|m| m & 3 == 3)
                    && self.child_started
                    && self.child_failure.is_none()
                    && self.child.as_ref().is_some_and(|p| p.passed),
                "station success evidence incomplete"
            );
        } else {
            ensure!(self.stage != "complete", "failed station claims completion");
        }
        Ok(())
    }
}

#[path = "account_station_lowbox_fixture_tests.rs"]
mod tests;
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields, tag = "phase")]
enum Frame {
    Diagnostic {
        receipt: Receipt,
    },
    Born {
        request: Request,
        reference: Reference,
        authentication_id: [u32; 2],
    },
    FamilyBorn {
        request: Request,
        depth: u32,
        reference: Reference,
        authentication_id: [u32; 2],
    },
    Finished {
        receipt: Receipt,
    },
}
fn require_checkpoint(
    receipt: &Receipt,
    request: &Request,
    stamp: Stamp,
    stage: &str,
) -> Result<()> {
    receipt.require(request, stamp)?;
    ensure!(
        !receipt.passed && receipt.stage == stage,
        "diagnostic phase differs"
    );
    match stage {
        "create_child" => ensure!(
            receipt.original_child.is_none() && receipt.loader.is_none(),
            "pre-birth diagnostic already contains child observations"
        ),
        "loader_observation" => {
            let loader = receipt
                .loader
                .as_ref()
                .context("loader diagnostic absent")?;
            ensure!(
                receipt.original_child == Some((loader.original_pid, loader.original_created)),
                "loader diagnostic original child differs"
            );
        }
        "filtered_primary_control" => ensure!(
            native::terminal_checkpoint_stage() == stage
                && receipt.original_child.is_some()
                && matches!(receipt.original_child_exit, Some(code) if code == EXIT || code == EXIT + 1)
                && receipt.child.as_ref().is_some_and(|p| p.passed)
                && receipt.desktop_child.as_ref().is_some_and(|p| p.passed)
                && receipt.schannel_child.is_some()
                && receipt.filtered_primary_control.is_none()
                && receipt.loader.is_none()
                && !receipt.desktop_closed
                && !receipt.station_closed
                && receipt.raw_schannel_control.is_none(),
            "filtered loader checkpoint lacks original proofs or claims later cleanup"
        ),
        "family" => ensure!(
            binding(request)?.is_v3_shadow()
                && receipt.child_started
                && receipt.original_child.is_some()
                && receipt.original_child_exit.is_none()
                && receipt.filesystem_subject.is_some()
                && receipt.family.is_none()
                && !receipt.station_closed
                && !receipt.desktop_closed,
            "family checkpoint lacks original retained scope"
        ),
        _ => anyhow::bail!("unsupported diagnostic phase"),
    }
    Ok(())
}
fn checkpoint(channel: &mut ControlChannel<'_>, receipt: &Receipt) -> Result<()> {
    require_checkpoint(
        receipt,
        &receipt.request,
        receipt.token_stamp,
        &receipt.stage,
    )?;
    channel.send(
        &encode(&Frame::Diagnostic {
            receipt: receipt.clone(),
        })?,
        TIMEOUT,
    )
}
// At most one diagnostic per phase, inside the phase's original deadline.
// Repeated progress cannot keep the fixture alive or replace a Born/Finished
// authority-bearing frame. The callback only preserves/relays observations.
fn receive_phase(
    request: &Request,
    stamp: Stamp,
    stage: &str,
    timeout: Duration,
    mut read: impl FnMut(Duration) -> Result<Vec<u8>>,
    mut save: impl FnMut(&Receipt, Duration) -> Result<()>,
) -> Result<Frame> {
    let end = deadline(timeout)?;
    let remaining = || -> Result<Duration> {
        let left = end.saturating_duration_since(Instant::now());
        ensure!(
            left >= Duration::from_millis(1),
            "diagnostic phase deadline exceeded"
        );
        Ok(left)
    };
    let first: Frame = decode(&read(remaining()?)?)?;
    if let Frame::Diagnostic { receipt } = first {
        require_checkpoint(&receipt, request, stamp, stage)?;
        save(&receipt, remaining()?)?;
        let next: Frame = decode(&read(remaining()?)?)?;
        ensure!(
            !matches!(next, Frame::Diagnostic { .. }),
            "repeated diagnostic phase"
        );
        Ok(next)
    } else {
        Ok(first)
    }
}
fn save_checkpoint(receipt: &Receipt, _: Duration) -> Result<()> {
    CHECKPOINT.with(|slot| {
        if let Some(previous) = slot.borrow().as_ref() {
            ensure!(
                previous.stage == "create_child"
                    && receipt.stage == "loader_observation"
                    && previous.request == receipt.request
                    && previous.token_stamp == receipt.token_stamp,
                "diagnostic replacement differs"
            );
        }
        *slot.borrow_mut() = Some(receipt.clone());
        Ok(())
    })
}
fn encode<T: Serialize>(value: &T) -> Result<Vec<u8>> {
    let mut bytes = b"alysis-station-lowbox-v1:".to_vec();
    bytes.extend_from_slice(&serde_json::to_vec(value)?);
    ensure!(bytes.len() <= LIMIT, "station lowbox frame exceeds bound");
    Ok(bytes)
}
fn decode<T: for<'de> Deserialize<'de>>(bytes: &[u8]) -> Result<T> {
    const PREFIX: &[u8] = b"alysis-station-lowbox-v1:";
    ensure!(
        bytes.len() <= LIMIT && bytes.starts_with(PREFIX),
        "station lowbox frame differs"
    );
    serde_json::from_slice(&bytes[PREFIX.len()..])
        .map_err(|_| anyhow::anyhow!("invalid station lowbox frame"))
}
fn ack(request: &Request, phase: &str) -> Result<Vec<u8>> {
    binding(request)?;
    ensure!(
        matches!(phase, "resume" | "finished"),
        "invalid station acknowledgment phase"
    );
    encode(&(phase, request))
}
fn family_ack(request: &Request, depth: u32) -> Result<Vec<u8>> {
    ensure!(
        binding(request)?.is_v3_shadow() && matches!(depth, 1 | 2),
        "descendant acknowledgment selection differs"
    );
    encode(&("family_resume", depth, request))
}
fn family_remaining(end: Instant) -> Result<Duration> {
    let left = end.saturating_duration_since(Instant::now());
    ensure!(
        left >= Duration::from_millis(1),
        "original family deadline exceeded"
    );
    Ok(left)
}
fn require_family_birth(
    request: &Request,
    root: (u32, u64),
    previous: &[(u32, u64)],
    depth: u32,
    reference: Reference,
) -> Result<()> {
    ensure!(
        binding(request)?.is_v3_shadow()
            && previous.len() < 2
            && depth as usize == previous.len() + 1,
        "descendant birth order differs"
    );
    require_reference(request, reference)?;
    let creator = previous.last().copied().unwrap_or(root);
    ensure!(
        reference.1 != root.0
            && !previous.iter().any(|p| p.0 == reference.1)
            && reference.2 >= creator.1,
        "original descendant chain aliases or predates creator"
    );
    Ok(())
}
fn require_family_retired(receipt: &Receipt, originals: &[OwnedHandle]) -> Result<()> {
    let actual = originals
        .iter()
        .map(|p| {
            Ok((
                unsafe { GetProcessId(p.raw()) },
                bootstrap::pull::creation_time(p.raw())?,
            ))
        })
        .collect::<Result<Vec<_>>>()?;
    if let Some(proof) = &receipt.family {
        ensure!(
            proof.identities() == actual,
            "family receipt differs from original retained processes"
        );
        if proof.passed {
            for (process, identity) in originals.iter().zip(actual).rev() {
                exited(process.raw(), identity, native::family::EXIT)?;
            }
        }
    } else {
        ensure!(
            !receipt.passed || !binding(&receipt.request)?.is_v3_shadow(),
            "successful terminal receipt lost original family observations"
        );
    }
    Ok(())
}
fn require_reference(request: &Request, reference: Reference) -> Result<()> {
    let b = binding(request)?;
    ensure!(
        reference.0 > 0
            && reference.0 < isize::MAX as u64
            && reference.1 != 0
            && ![
                request.pid,
                request.birth.selection.bootstrap_pid,
                b.profile.owner_pid
            ]
            .contains(&reference.1)
            && reference.2 >= request.created,
        "station original child reference differs"
    );
    Ok(())
}
pub(crate) fn require_lowbox(
    process: HANDLE,
    request: &Request,
    authentication_id: [u32; 2],
) -> Result<()> {
    let b = binding(request)?;
    noninheritable(process)?;
    crate::account_token::verify_account_process(
        process,
        &b.profile.container,
        &request.birth.selection.account.account_sid,
        network(request),
    )?;
    let peer = ExpectedPeer::retain(process)?;
    ensure!(
        peer.token.fixture_stamp(process)?.1 == authentication_id,
        "lowbox original logon session differs"
    );
    let mut session = 0;
    unsafe {
        windows::Win32::System::RemoteDesktop::ProcessIdToSessionId(peer.pid, &mut session)?;
    }
    ensure!(
        session == request.birth.selection.session,
        "lowbox original session differs"
    );
    peer.alive()
}
fn exited(process: HANDLE, expected: (u32, u64), code: u32) -> Result<()> {
    ensure!(
        unsafe { GetProcessId(process) } == expected.0
            && bootstrap::pull::creation_time(process)? == expected.1
            && unsafe { WaitForSingleObject(process, 5000) } == WAIT_OBJECT_0,
        "original lowbox child exit missing"
    );
    let mut actual = 0;
    unsafe {
        GetExitCodeProcess(process, &mut actual)?;
    }
    ensure!(actual == code, "original lowbox child exit differs");
    Ok(())
}
fn native_code(error: &anyhow::Error) -> Option<i32> {
    error
        .chain()
        .find_map(|e| e.downcast_ref::<windows::core::Error>())
        .map(|e| e.code().0)
}
pub(super) fn client(
    channel: &mut ControlChannel<'_>,
    request: &Request,
    image: &AccountImageObserver,
) -> Result<()> {
    binding(request)?;
    ensure!(!channel.server, "station fixture client role differs");
    channel.peer.verify(channel.handle.raw(), false)?;
    image.verify_current()?;
    let current = ExpectedPeer::retain(unsafe { GetCurrentProcess() })?;
    let selected = &request.birth.selection;
    current.require_profile_runner(
        &selected.account.account_sid,
        selected.session,
        &selected.account.group_sid,
    )?;
    ensure!(
        current.pid == request.pid
            && bootstrap::pull::creation_time(current.process.raw())? == request.created,
        "station fixture original account differs"
    );
    let stamp = current.token.fixture_stamp(current.process.raw())?;
    let mut receipt = Receipt::new(request, stamp)?;
    let result = native::run(channel, request, image, stamp.1, &mut receipt);
    if let Err(error) = result {
        receipt.hresult = native_code(&error)
            .or(receipt.child.as_ref().and_then(|p| p.hresult))
            .or(receipt.desktop_child.as_ref().and_then(|p| p.hresult))
            .or(receipt
                .schannel_child
                .as_ref()
                .and_then(|p| p.failure_status()))
            .or(receipt
                .raw_schannel_control
                .as_ref()
                .and_then(|p| p.failure_status()))
            .or(receipt.filesystem_child.as_ref().and_then(|p| p.hresult))
            .or(receipt.family.as_ref().and_then(|p| p.hresult))
            .or(receipt.family_root_hresult);
    }
    current.alive()?;
    receipt.require(request, current.token.fixture_stamp(current.process.raw())?)?;
    channel.send(&encode(&Frame::Finished { receipt })?, TIMEOUT)?;
    ensure!(
        channel.receive(TIMEOUT)? == ack(request, "finished")?,
        "station completion acknowledgment differs"
    );
    // The broker refuses a failed receipt. Keep this original raw actor alive
    // for the normal terminal handshake (or broker drain), so its exit cannot
    // race the source's ACK post-write original-peer verification.
    Ok(())
}
pub(super) fn relay(
    broker: &mut ControlChannel<'_>,
    account: &mut ControlChannel<'_>,
    child: &crate::launch::SpawnedChild,
    request: &Request,
    image: &PinnedRunnerImage,
    timeout: Duration,
) -> Result<Option<OwnedHandle>> {
    binding(request)?;
    let stamp = account
        .peer
        .token
        .fixture_stamp(account.peer.process.raw())?;
    let mut frame = receive_phase(
        request,
        stamp,
        "create_child",
        timeout,
        |left| account.receive(left),
        |receipt, left| {
            broker.send(
                &encode(&Frame::Diagnostic {
                    receipt: receipt.clone(),
                })?,
                left,
            )
        },
    )?;
    let mut retained = None;
    let mut family_retained = Vec::new();
    let mut family_identities = Vec::new();
    let mut family_end = None;
    if let Frame::Born {
        request: subject,
        reference,
        authentication_id,
    } = &mut frame
    {
        ensure!(
            *subject == *request && *authentication_id == stamp.1,
            "station source offer differs"
        );
        require_reference(request, *reference)?;
        let mut raw = HANDLE::default();
        unsafe {
            DuplicateHandle(
                child.process(),
                HANDLE(reference.0 as usize as *mut _),
                GetCurrentProcess(),
                &mut raw,
                (PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SYNCHRONIZE).0,
                false,
                DUPLICATE_HANDLE_OPTIONS(0),
            )?;
        }
        let original = OwnedHandle(raw);
        require_lowbox(raw, request, stamp.1)?;
        image.verify_process(raw)?;
        ensure!(
            unsafe { GetProcessId(raw) } == reference.1
                && bootstrap::pull::creation_time(raw)? == reference.2,
            "source lowbox original identity differs"
        );
        reference.0 = raw.0 as usize as u64;
        retained = Some(original);
        broker.send(&encode(&frame)?, timeout)?;
        ensure!(
            broker.receive(timeout)? == ack(request, "resume")?,
            "station broker resume acknowledgment differs"
        );
        require_lowbox(raw, request, stamp.1)?;
        account.send(&ack(request, "resume")?, timeout)?;
        family_end = Some(deadline(timeout)?);
        frame = receive_phase(
            request,
            stamp,
            if binding(request)?.is_v3_shadow() {
                "family"
            } else {
                native::terminal_checkpoint_stage()
            },
            timeout,
            |left| account.receive(left),
            |receipt, left| {
                broker.send(
                    &encode(&Frame::Diagnostic {
                        receipt: receipt.clone(),
                    })?,
                    left,
                )
            },
        )?;
    }
    while let Frame::FamilyBorn {
        request: subject,
        depth,
        reference,
        authentication_id,
    } = &mut frame
    {
        ensure!(
            *subject == *request && *authentication_id == stamp.1,
            "source descendant offer differs"
        );
        let root = retained
            .as_ref()
            .context("family offer precedes original root")?;
        let root_identity = (
            unsafe { GetProcessId(root.raw()) },
            bootstrap::pull::creation_time(root.raw())?,
        );
        require_family_birth(
            request,
            root_identity,
            &family_identities,
            *depth,
            *reference,
        )?;
        let end = family_end.context("family original deadline absent")?;
        let mut raw = HANDLE::default();
        unsafe {
            DuplicateHandle(
                child.process(),
                HANDLE(reference.0 as usize as *mut _),
                GetCurrentProcess(),
                &mut raw,
                (PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SYNCHRONIZE).0,
                false,
                DUPLICATE_HANDLE_OPTIONS(0),
            )?;
        }
        let original = OwnedHandle(raw);
        require_lowbox(raw, request, stamp.1)?;
        image.verify_process(raw)?;
        ensure!(
            unsafe { GetProcessId(raw) } == reference.1
                && bootstrap::pull::creation_time(raw)? == reference.2,
            "source descendant original identity differs"
        );
        family_identities.push((reference.1, reference.2));
        reference.0 = raw.0 as usize as u64;
        family_retained.push(original);
        let expected_ack = family_ack(request, *depth)?;
        broker.send(&encode(&frame)?, family_remaining(end)?)?;
        ensure!(
            broker.receive(family_remaining(end)?)? == expected_ack,
            "descendant broker resume acknowledgment differs"
        );
        require_lowbox(raw, request, stamp.1)?;
        account.send(&expected_ack, family_remaining(end)?)?;
        frame = decode(&account.receive(family_remaining(end)?)?)?;
    }
    let Frame::Finished { receipt } = frame else {
        anyhow::bail!("station terminal receipt missing")
    };
    receipt.require(request, stamp)?;
    require_family_retired(&receipt, &family_retained)?;
    if let Some(original) = &retained {
        if let (Some(identity), Some(code)) = (receipt.original_child, receipt.original_child_exit)
        {
            exited(original.raw(), identity, code)?;
        }
        ensure!(
            !receipt.passed || receipt.original_child_exit == Some(EXIT),
            "station child exit proof missing"
        );
    } else {
        ensure!(!receipt.passed, "station success without original offer");
    }
    broker.send(&encode(&Frame::Finished { receipt })?, timeout)?;
    ensure!(
        broker.receive(timeout)? == ack(request, "finished")?,
        "station broker completion acknowledgment differs"
    );
    account.send(&ack(request, "finished")?, timeout)?;
    Ok(retained)
}
pub(crate) fn receive(
    channel: &mut ControlChannel<'_>,
    account: &ExpectedPeer<'_>,
    request: &Request,
    timeout: Duration,
    mut pull: impl FnMut(Reference) -> Result<OwnedHandle>,
) -> Result<Option<OwnedHandle>> {
    binding(request)?;
    account.alive()?;
    let stamp = account.token.fixture_stamp(account.process.raw())?;
    let observation = logon_observation::observe(&channel.peer, account, request)?;
    LOGON_OBSERVATION.with(|slot| -> Result<()> {
        ensure!(slot.borrow().is_none(), "logon pair already observed");
        *slot.borrow_mut() = Some(observation);
        Ok(())
    })?;
    let mut frame = receive_phase(
        request,
        stamp,
        "create_child",
        timeout,
        |left| channel.receive(left),
        save_checkpoint,
    )?;
    let mut original = None;
    let mut family_retained = Vec::new();
    let mut family_identities = Vec::new();
    let mut family_end = None;
    if let Frame::Born {
        request: subject,
        reference,
        authentication_id,
    } = &frame
    {
        ensure!(
            subject == request && *authentication_id == stamp.1,
            "broker lowbox offer differs"
        );
        require_reference(request, *reference)?;
        let retained = pull(*reference)?; // exact original source table + original consumer job/image
        require_lowbox(retained.raw(), request, stamp.1)?;
        original = Some(retained);
        channel.send(&ack(request, "resume")?, timeout)?;
        family_end = Some(deadline(timeout)?);
        frame = receive_phase(
            request,
            stamp,
            if binding(request)?.is_v3_shadow() {
                "family"
            } else {
                native::terminal_checkpoint_stage()
            },
            timeout,
            |left| channel.receive(left),
            save_checkpoint,
        )?;
    }
    while let Frame::FamilyBorn {
        request: subject,
        depth,
        reference,
        authentication_id,
    } = &frame
    {
        ensure!(
            subject == request && *authentication_id == stamp.1,
            "broker descendant offer differs"
        );
        let root = original
            .as_ref()
            .context("family offer precedes original root")?;
        let root_identity = (
            unsafe { GetProcessId(root.raw()) },
            bootstrap::pull::creation_time(root.raw())?,
        );
        require_family_birth(
            request,
            root_identity,
            &family_identities,
            *depth,
            *reference,
        )?;
        let end = family_end.context("family original deadline absent")?;
        let retained = pull(*reference)?; // same original source table, pinned image and exact consumer job
        require_lowbox(retained.raw(), request, stamp.1)?;
        ensure!(
            unsafe { GetProcessId(retained.raw()) } == reference.1
                && bootstrap::pull::creation_time(retained.raw())? == reference.2,
            "broker descendant original identity differs"
        );
        family_identities.push((reference.1, reference.2));
        family_retained.push(retained);
        channel.send(&family_ack(request, *depth)?, family_remaining(end)?)?;
        frame = decode(&channel.receive(family_remaining(end)?)?)?;
    }
    let Frame::Finished { receipt } = frame else {
        anyhow::bail!("station final receipt missing")
    };
    receipt.require(request, stamp)?;
    require_family_retired(&receipt, &family_retained)?;
    if let Some(original) = &original {
        if let (Some(identity), Some(code)) = (receipt.original_child, receipt.original_child_exit)
        {
            exited(original.raw(), identity, code)?;
        }
        ensure!(
            !receipt.passed || receipt.original_child_exit == Some(EXIT),
            "broker original child exit absent"
        );
    } else {
        ensure!(!receipt.passed, "station success without original child");
    }
    let passed = receipt.passed;
    RECEIPT.with(|slot| -> Result<()> {
        ensure!(slot.borrow().is_none(), "station receipt already recorded");
        *slot.borrow_mut() = Some(receipt);
        Ok(())
    })?;
    channel.send(&ack(request, "finished")?, timeout)?;
    ensure!(passed, "fixed station lowbox diagnostic refused");
    Ok(original)
}
