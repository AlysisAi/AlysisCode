//! Retain the one OS-created private RPC directory without following aliases.
//! The retained handle remains query-only. The initializer coordinator may
//! publish one journaled account policy using a temporary same-object DACL handle;
//! observed names and cold records never supply this authority.
use super::*;

#[cfg(test)]
#[path = "account_initializer_rpc_lowbox.rs"]
pub(crate) mod lowbox;

/// Fixed descriptor identity, not an admission or mutation capability. The
/// constructor comparison has only an explicitly admitted disposable test route.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub(crate) enum PolicyKind {
    #[default]
    BaselineV1,
    // Distinct persisted candidate identity. It is readable as evidence but
    // cannot be published until the exact policy's release gates are qualified.
    SystemReadInheritanceV2,
    SystemNsiReadInheritanceV3,
    #[cfg(test)]
    KernelBaseAccountFullV1,
    #[cfg(test)]
    SystemInheritanceV1,
    #[cfg(test)]
    NsiSystemInheritanceV1,
}
const PACKAGE_ACCESS: u32 = QUERY_ACCESS | 0x0000000c;
impl PolicyKind {
    pub(in crate::account_state::execution) fn is_baseline(&self) -> bool {
        *self == Self::BaselineV1
    }
    pub(in crate::account_state::execution) fn require_live(self) -> Result<()> {
        match self {
            Self::BaselineV1 => Ok(()),
            Self::SystemReadInheritanceV2 => {
                anyhow::bail!("SYSTEM read inheritance production lifecycle is unqualified")
            }
            Self::SystemNsiReadInheritanceV3 => {
                #[cfg(test)]
                return crate::account_rpc_v3_lifecycle_fixture::require_live();
                #[cfg(not(test))]
                anyhow::bail!("SYSTEM/NSI read inheritance production lifecycle is unqualified")
            }
            #[cfg(test)]
            Self::KernelBaseAccountFullV1
            | Self::SystemInheritanceV1
            | Self::NsiSystemInheritanceV1 => {
                use crate::account_control::station_lowbox_fixture as fixture;
                ensure!(
                    Self::selected_fixture_policy()? == self,
                    "RPC fixture policy is not selected by this source"
                );
                require_constructor_fixture_admission(
                    cfg!(all(windows, target_arch = "x86_64")),
                    true,
                    fixture::ENABLED.with(|s| s.get()),
                    |key| std::env::var(key).ok(),
                )
            }
        }
    }
    #[cfg(test)]
    pub(crate) fn selected_fixture_policy() -> Result<Self> {
        use crate::account_control::station_lowbox_fixture as fixture;
        select_fixture_policy(
            fixture::RPC_CONSTRUCTOR_COMPARISON,
            fixture::RPC_SYSTEM_INHERITANCE_COMPARISON,
            fixture::RPC_NSI_SYSTEM_INHERITANCE_COMPARISON,
        )
    }
    #[cfg(test)]
    pub(crate) fn fixture_binding_policy(self) -> u32 {
        match self {
            Self::BaselineV1 => 1,
            Self::KernelBaseAccountFullV1 => 2,
            Self::SystemInheritanceV1 => 3,
            Self::NsiSystemInheritanceV1 => 5,
            // Reserved identity only; the current diagnostic wire decoder must
            // reject it rather than turn future production data into a fixture.
            Self::SystemReadInheritanceV2 => 4,
            Self::SystemNsiReadInheritanceV3 => 6,
        }
    }
    /// Codec identity only; a child has neither the root's thread-local guard
    /// nor its admission environment. This never admits descriptor publication.
    #[cfg(test)]
    pub(crate) fn from_fixture_binding_policy(policy: u32) -> Result<Self> {
        match policy {
            1 => Ok(Self::BaselineV1),
            // Passive evidence can represent either fixed diagnostic. Effectful
            // preparation separately requires the exact selected live policy.
            2 => Ok(Self::KernelBaseAccountFullV1),
            3 => Ok(Self::SystemInheritanceV1),
            5 => Ok(Self::NsiSystemInheritanceV1),
            _ => anyhow::bail!("station RPC policy identity is unknown"),
        }
    }
    fn account_access(self) -> u32 {
        match self {
            Self::BaselineV1 | Self::SystemReadInheritanceV2 | Self::SystemNsiReadInheritanceV3 => {
                PACKAGE_ACCESS
            }
            #[cfg(test)]
            Self::KernelBaseAccountFullV1 => DIRECTORY_ALL,
            #[cfg(test)]
            Self::SystemInheritanceV1 | Self::NsiSystemInheritanceV1 => PACKAGE_ACCESS,
        }
    }
    fn inherits_system_read(self) -> bool {
        match self {
            Self::SystemReadInheritanceV2 | Self::SystemNsiReadInheritanceV3 => true,
            #[cfg(test)]
            Self::SystemInheritanceV1 | Self::NsiSystemInheritanceV1 => true,
            _ => false,
        }
    }
    fn inherits_nsi_read(self) -> bool {
        match self {
            Self::SystemNsiReadInheritanceV3 => true,
            #[cfg(test)]
            Self::NsiSystemInheritanceV1 => true,
            _ => false,
        }
    }
}

#[cfg(test)]
fn select_fixture_policy(
    constructor: bool,
    system_inheritance: bool,
    nsi: bool,
) -> Result<PolicyKind> {
    match (constructor, system_inheritance, nsi) {
        (false, false, false) => Ok(PolicyKind::BaselineV1),
        (true, false, false) => Ok(PolicyKind::KernelBaseAccountFullV1),
        (false, true, false) => Ok(PolicyKind::SystemInheritanceV1),
        (false, false, true) => Ok(PolicyKind::NsiSystemInheritanceV1),
        _ => anyhow::bail!("RPC fixture comparisons cannot be combined"),
    }
}

#[cfg(test)]
fn require_constructor_fixture_admission(
    platform: bool,
    selected: bool,
    enabled: bool,
    environment: impl Fn(&str) -> Option<String>,
) -> Result<()> {
    ensure!(
        platform && selected && enabled,
        "RPC constructor fixture has no original live admission"
    );
    for (key, expected) in CONSTRUCTOR_ADMISSION {
        ensure!(
            environment(key).as_deref() == Some(expected),
            "RPC constructor disposable admission refused"
        );
    }
    for key in ["GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT"] {
        let value = environment(key).context("RPC constructor run identity absent")?;
        ensure!(
            (1..=24).contains(&value.len())
                && value.bytes().all(|c| c.is_ascii_digit())
                && value.bytes().any(|c| c != b'0'),
            "RPC constructor run identity invalid"
        );
    }
    Ok(())
}
#[cfg(test)]
const CONSTRUCTOR_ADMISSION: [(&str, &str); 11] = [
    (
        "ALYSIS_RUN_DISPOSABLE_ACCOUNT_BIRTH",
        "github-hosted-account-birth-only",
    ),
    (
        "ALYSIS_RUN_DISPOSABLE_ATOMIC_STATION",
        "github-hosted-atomic-station-only",
    ),
    (
        "ALYSIS_RUN_DISPOSABLE_STATION_LOWBOX",
        "github-hosted-station-lowbox-only",
    ),
    (
        "ALYSIS_RUN_DISPOSABLE_ATOMIC_PROFILE",
        "github-hosted-atomic-profile-only",
    ),
    (
        "ALYSIS_RUN_DISPOSABLE_UNUSED_PROFILE",
        "github-hosted-unused-profile-only",
    ),
    ("ALYSIS_ACCOUNT_BIRTH_SELECTION", "station-lowbox"),
    ("GITHUB_ACTIONS", "true"),
    ("RUNNER_ENVIRONMENT", "github-hosted"),
    ("RUNNER_OS", "Windows"),
    ("GITHUB_REPOSITORY", "AlysisAi/alysis-code-internal"),
    ("GITHUB_EVENT_NAME", "workflow_dispatch"),
];

pub(in crate::account_state::execution) struct RetainedRpc {
    handle: OwnedHandle,
    security: SecurityShape,
    access_attempted: bool,
}
impl RetainedRpc {
    pub(in crate::account_state::execution) fn open(root: HANDLE) -> Result<Self> {
        crate::account_impersonation::require_process_identity()?;
        crate::account_namespace::verify_noninheritable(root)?;
        // The sole fixed relative component is never an observed string or alias.
        // NtOpenDirectoryObject selects the Directory type; DONT_REPARSE remains set.
        let handle = open_directory("RPC Control", root, QUERY_ACCESS)
            .map_err(|s| status_error(s, "retain original initializer RPC directory"))?;
        object_name::require_child(root, handle.raw(), "RPC Control")?;
        crate::account_namespace::verify_noninheritable(handle.raw())?;
        let security = security_shape(&query_security(handle.raw())?)?;
        let original = Self {
            handle,
            security,
            access_attempted: false,
        };
        original.verify(root)?;
        Ok(original)
    }
    pub(in crate::account_state::execution) fn verify(&self, root: HANDLE) -> Result<()> {
        crate::account_impersonation::require_process_identity()?;
        crate::account_namespace::verify_noninheritable(root)?;
        crate::account_namespace::verify_noninheritable(self.handle.raw())?;
        object_name::require_child(root, self.handle.raw(), "RPC Control")?;
        let current = open_directory("RPC Control", root, QUERY_ACCESS)
            .map_err(|s| status_error(s, "observe retained initializer RPC directory"))?;
        object_name::require_child(root, current.raw(), "RPC Control")?;
        let same = unsafe { CompareObjectHandles(self.handle.raw(), current.raw()) }.as_bool();
        unsafe { CloseHandle(current.into_raw()) }
            .context("RPC observation handle close uncertain")?;
        ensure!(
            same,
            "initializer RPC directory no longer names the original object"
        );
        ensure!(
            security_shape(&query_security(self.handle.raw())?)? == self.security,
            "original initializer RPC security changed"
        );
        Ok(())
    }
    pub(in crate::account_state::execution) fn prepare_account_access(
        &self,
        root: HANDLE,
        identity: &impl NamespacePolicySource,
    ) -> Result<AccessPolicy> {
        identity.rpc_policy().require_live()?;
        ensure!(
            !self.access_attempted,
            "RPC account access already attempted"
        );
        self.verify(root)?;
        require_empty(self.handle.raw())?;
        let policy = AccessPolicy {
            kind: identity.rpc_policy(),
            before: self.security.clone(),
            after: descriptor_shape(&account_descriptor(identity)?)?,
        };
        policy.validate(identity)?;
        Ok(policy)
    }
    /// Caller must commit/read back the exact policy before this one-shot effect.
    /// The write handle is opened relative to the original root and compared to
    /// the retained query pin before any mutation. No alias target is opened.
    pub(in crate::account_state::execution) fn grant_account(
        &mut self,
        root: HANDLE,
        identity: &impl NamespacePolicySource,
        policy: &AccessPolicy,
    ) -> Result<()> {
        identity.rpc_policy().require_live()?;
        ensure!(
            !std::mem::replace(&mut self.access_attempted, true),
            "RPC account access already attempted"
        );
        policy.validate(identity)?;
        ensure!(
            crate::sid::current_user_sid()? == identity.execution().owner,
            "RPC grant requires original host owner"
        );
        self.verify(root)?;
        ensure!(
            self.security == policy.before,
            "RPC grant before-policy changed"
        );
        require_empty(self.handle.raw())?;
        let writer = open_directory(
            "RPC Control",
            root,
            QUERY_ACCESS | 0x00040000, /* WRITE_DAC */
        )
        .map_err(|s| status_error(s, "open original private RPC DACL handle"))?;
        crate::account_namespace::verify_noninheritable(writer.raw())?;
        object_name::require_child(root, writer.raw(), "RPC Control")?;
        ensure!(
            unsafe { CompareObjectHandles(self.handle.raw(), writer.raw()) }.as_bool(),
            "RPC DACL handle differs from original query pin"
        );
        ensure!(
            security_shape(&query_security(writer.raw())?)? == policy.before,
            "RPC security changed before publication"
        );
        require_empty(writer.raw())?;
        let descriptor = account_descriptor(identity)?;
        unsafe {
            NtSetSecurityObject(
                writer.raw(),
                (DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION).0,
                descriptor.ptr,
            )
        }
        .ok()
        .context("publish private RPC account policy")?;
        ensure!(
            security_shape(&query_security(writer.raw())?)? == policy.after,
            "private RPC account policy readback differs"
        );
        // Only the read-only pin is kept across account execution.
        unsafe { CloseHandle(writer.into_raw()) }.context("RPC policy handle close uncertain")?;
        self.security = policy.after.clone();
        self.verify(root)
    }
    pub(in crate::account_state::execution) fn verify_account_access(
        &self,
        root: HANDLE,
        identity: &impl NamespacePolicySource,
        policy: &AccessPolicy,
    ) -> Result<()> {
        identity.rpc_policy().require_live()?;
        policy.validate(identity)?;
        ensure!(
            self.access_attempted && self.security == policy.after,
            "RPC access has no original successful publication"
        );
        self.verify(root)
    }
    pub(in crate::account_state::execution) fn close(self) -> Result<()> {
        unsafe { CloseHandle(self.handle.into_raw()) }
            .context("original initializer RPC close uncertain")
    }
}

/// Exact bounded diagnostic state, never a live object or mutation ticket.
#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub(in crate::account_state::execution) struct AccessPolicy {
    kind: PolicyKind,
    before: SecurityShape,
    after: SecurityShape,
}
impl AccessPolicy {
    pub(in crate::account_state::execution) fn validate(
        &self,
        identity: &impl NamespacePolicySource,
    ) -> Result<()> {
        ensure!(
            self.kind == identity.rpc_policy(),
            "RPC policy identity differs from original namespace plan"
        );
        let expected = descriptor_shape(&account_descriptor(identity)?)?;
        ensure!(
            self.after == expected,
            "RPC account policy differs from fixed subjects/masks"
        );
        ensure!(
            self.before.owner == expected.owner
                && self.before.label == expected.label
                && !self.before.dacl.is_empty()
                && self.before.dacl.len() <= 2048
                && self.before != self.after,
            "RPC original owner/Low label or bounded before-policy differs"
        );
        Ok(())
    }
}
fn account_descriptor(identity: &impl NamespacePolicySource) -> Result<OwnedSd> {
    // Validate the same three distinct owner/account/package subjects as root.
    drop(descriptor(identity)?);
    subject_descriptor(
        &identity.execution().owner,
        &identity.execution().identity.binding.account_sid,
        &identity.execution().identity.package_sid,
        identity.rpc_policy(),
    )
}
fn subject_descriptor(
    owner: &str,
    account: &str,
    package: &str,
    kind: PolicyKind,
) -> Result<OwnedSd> {
    for sid in [owner, account, package] {
        let _sid = crate::sid::LocalPsid::from_string(sid)?;
    }
    ensure!(
        owner != account
            && owner != package
            && account != package
            && owner != "S-1-5-18"
            && package.starts_with("S-1-15-2-"),
        "RPC policy subjects overlap or differ"
    );
    // Package access never changes. Only the test-only descriptor representation
    // can describe an account ACE with full rights; publication requires the
    // original live disposable-fixture admission as well as this exact policy.
    let account_access = kind.account_access();
    let inheritance = if kind.inherits_system_read() {
        // Inherit-only: no additional rights on this directory. Eligible
        // private descendants can inherit SYSTEM read access. RPC also derives
        // its client-side server requirements from this private parent DACL.
        "(A;OIIO;GR;;;SY)"
    } else {
        ""
    };
    let nsi = if kind.inherits_nsi_read() {
        // Exact NT SERVICE\nsi identity, not the LocalService user or Everyone.
        // Applies to eligible private descendants, never direct directory access.
        "(A;OIIO;GR;;;S-1-5-80-2310782386-4237065203-3688974353-390202159-3511571085)"
    } else {
        ""
    };
    OwnedSd::from_sddl(&format!(
        "O:{owner}D:P(A;;0x{DIRECTORY_ALL:x};;;SY)(A;;0x{DIRECTORY_ALL:x};;;{owner})(A;;0x{account_access:x};;;{account})(A;;0x{PACKAGE_ACCESS:x};;;{package}){inheritance}{nsi}S:(ML;;NW;;;LW)"
    ))
}
/// Bounded account-side observations only. Caller-supplied subjects authorize
/// no mutations, handle transfer, resource creation, command or cleanup.
pub(crate) fn read_only_account_probe(
    owner: &str,
    account: &str,
    package: &str,
    session: u32,
) -> Result<()> {
    crate::account_impersonation::require_process_identity()?;
    ensure!(
        crate::sid::current_user_sid()? == account,
        "RPC probe requires selected account"
    );
    let expected = descriptor_shape(&subject_descriptor(
        owner,
        account,
        package,
        PolicyKind::BaselineV1,
    )?)?;
    let mut current_session = 0;
    unsafe {
        windows::Win32::System::RemoteDesktop::ProcessIdToSessionId(
            std::process::id(),
            &mut current_session,
        )?;
    }
    ensure!(current_session == session, "RPC probe session differs");
    let path = crate::account_namespace::namespace_path(package)?;
    let root = open_directory(&path, HANDLE::default(), QUERY_ACCESS)
        .map_err(|s| status_error(s, "account query private namespace"))?;
    crate::account_namespace::verify_noninheritable(root.raw())?;
    let rpc = open_directory("RPC Control", root.raw(), QUERY_ACCESS | 0x0000000c)
        .map_err(|s| status_error(s, "account private RPC allowed access"))?;
    crate::account_namespace::verify_noninheritable(rpc.raw())?;
    ensure!(
        security_shape(&query_security(rpc.raw())?)? == expected,
        "account observed RPC policy differs"
    );
    for denied in [0x00010000, 0x00040000, 0x00080000] {
        // DELETE, WRITE_DAC, WRITE_OWNER
        match open_directory("RPC Control", root.raw(), denied) {
            Err(status) if status == NTSTATUS(0xc0000022u32 as i32) => (),
            Err(status) => {
                return Err(status_error(
                    status,
                    "RPC prohibited access denial unverified",
                ));
            }
            Ok(unexpected) => {
                unsafe { CloseHandle(unexpected.into_raw()) }
                    .context("unexpected RPC access handle close uncertain")?;
                anyhow::bail!("account obtained prohibited RPC directory access");
            }
        }
    }
    ensure!(
        security_shape(&query_security(rpc.raw())?)? == expected,
        "RPC policy changed during account probe"
    );
    unsafe { CloseHandle(rpc.into_raw()) }.context("account RPC query handle close uncertain")?;
    unsafe { CloseHandle(root.into_raw()) }
        .context("account namespace query handle close uncertain")
}

#[cfg(test)]
pub(in crate::account_state::execution) fn fixture_access_policy(
    identity: &impl NamespacePolicySource,
) -> AccessPolicy {
    let owner = &identity.execution().owner;
    let before = OwnedSd::from_sddl(&format!(
        "O:{owner}D:P(A;;0xf000f;;;SY)(A;;0xf000f;;;{owner})S:(ML;;NW;;;LW)"
    ))
    .unwrap();
    let policy = AccessPolicy {
        kind: identity.rpc_policy(),
        before: descriptor_shape(&before).unwrap(),
        after: descriptor_shape(&account_descriptor(identity).unwrap()).unwrap(),
    };
    policy.validate(identity).unwrap();
    policy
}

#[cfg(test)]
mod policy_tests {
    use super::*;

    struct Identity {
        execution: ExecutionRecord,
        kind: PolicyKind,
    }
    impl NamespacePolicySource for Identity {
        fn execution(&self) -> &ExecutionRecord {
            &self.execution
        }
        fn session(&self) -> u32 {
            7
        }
        fn rpc_policy(&self) -> PolicyKind {
            self.kind
        }
    }
    fn identity(kind: PolicyKind) -> Identity {
        let setup = crate::account_install::synthetic_ready();
        let binding = setup
            .binding(crate::account_install::AccountMode::Offline)
            .unwrap();
        let execution = crate::account_state::execution::fixture_record(
            crate::account_state::CandidateJournalIdentity::new(
                binding,
                format!(
                    "Alysis.Execution.{}",
                    crate::account_state::execution::random_id().unwrap()
                ),
            )
            .unwrap(),
        );
        Identity { execution, kind }
    }
    fn expected(identity: &Identity, account_access: u32, package_access: u32) -> SecurityShape {
        let owner = &identity.execution.owner;
        let account = &identity.execution.identity.binding.account_sid;
        let package = &identity.execution.identity.package_sid;
        descriptor_shape(&OwnedSd::from_sddl(&format!(
            "O:{owner}D:P(A;;0xf000f;;;SY)(A;;0xf000f;;;{owner})(A;;0x{account_access:x};;;{account})(A;;0x{package_access:x};;;{package})S:(ML;;NW;;;LW)"
        )).unwrap()).unwrap()
    }
    #[test]
    fn rpc_policy_production_inheritance_has_distinct_identity_and_no_live_admission() {
        let mut id = identity(PolicyKind::SystemInheritanceV1);
        let diagnostic = fixture_access_policy(&id);
        id.kind = PolicyKind::SystemReadInheritanceV2;
        let production = fixture_access_policy(&id);
        assert_eq!(production.before, diagnostic.before);
        assert_eq!(production.after, diagnostic.after);
        assert_ne!(production.kind, diagnostic.kind);
        assert!(diagnostic.validate(&id).is_err());
        production.validate(&id).unwrap();
        assert!(id.kind.require_live().is_err());
        assert_eq!(id.kind.account_access(), 0x2000f);
        assert!(PolicyKind::from_fixture_binding_policy(id.kind.fixture_binding_policy()).is_err());
        let encoded = serde_json::to_string(&id.kind).unwrap();
        assert_eq!(encoded, "\"system_read_inheritance_v2\"");
        assert_eq!(
            serde_json::from_str::<PolicyKind>(&encoded).unwrap(),
            id.kind
        );
        id.kind = PolicyKind::SystemInheritanceV1;
        assert!(production.validate(&id).is_err());
        id.kind = PolicyKind::BaselineV1;
        assert!(production.validate(&id).is_err());
    }
    #[test]
    fn rpc_policy_production_nsi_identity_is_passive_and_rejects_every_older_identity() {
        let mut id = identity(PolicyKind::NsiSystemInheritanceV1);
        let diagnostic = fixture_access_policy(&id);
        id.kind = PolicyKind::SystemNsiReadInheritanceV3;
        let production = fixture_access_policy(&id);
        assert_eq!(production.before, diagnostic.before);
        assert_eq!(production.after, diagnostic.after);
        assert_ne!(production.kind, diagnostic.kind);
        production.validate(&id).unwrap();
        assert!(id.kind.require_live().is_err());
        assert_eq!(id.kind.account_access(), 0x2000f);
        assert_eq!(id.kind.fixture_binding_policy(), 6);
        assert!(PolicyKind::from_fixture_binding_policy(6).is_err());
        let encoded = serde_json::to_string(&id.kind).unwrap();
        assert_eq!(encoded, "\"system_nsi_read_inheritance_v3\"");
        assert_eq!(
            serde_json::from_str::<PolicyKind>(&encoded).unwrap(),
            id.kind
        );
        for old in [
            PolicyKind::BaselineV1,
            PolicyKind::KernelBaseAccountFullV1,
            PolicyKind::SystemInheritanceV1,
            PolicyKind::SystemReadInheritanceV2,
            PolicyKind::NsiSystemInheritanceV1,
        ] {
            id.kind = old;
            let previous = fixture_access_policy(&id);
            assert!(production.validate(&id).is_err());
            id.kind = PolicyKind::SystemNsiReadInheritanceV3;
            assert!(previous.validate(&id).is_err());
        }
    }
    #[test]
    fn rpc_policy_system_inheritance_changes_only_one_exact_inherit_only_ace() {
        let mut id = identity(PolicyKind::BaselineV1);
        let baseline = fixture_access_policy(&id);
        id.kind = PolicyKind::SystemInheritanceV1;
        let variant = fixture_access_policy(&id);
        assert_eq!(id.kind.account_access(), 0x2000f);
        assert_eq!(variant.before, baseline.before);
        assert_eq!(variant.after.owner, baseline.after.owner);
        assert_eq!(variant.after.label, baseline.after.label);
        assert_eq!(variant.after.control, baseline.after.control);
        let at = baseline.after.dacl.len();
        assert_eq!(&variant.after.dacl[8..at], &baseline.after.dacl[8..]);
        // ALLOW, OI|IO, size20, GENERIC_READ, SYSTEM SID S-1-5-18.
        assert_eq!(
            &variant.after.dacl[at..],
            &[
                0, 9, 20, 0, 0, 0, 0, 128, 1, 1, 0, 0, 0, 0, 0, 5, 18, 0, 0, 0
            ]
        );
        assert!(id.kind.require_live().is_err());
        for offset in [at, at + 1, at + 4, at + 16] {
            let mut changed = variant.clone();
            changed.after.dacl[offset] ^= 1;
            assert!(changed.validate(&id).is_err());
        }
        for other in [PolicyKind::BaselineV1, PolicyKind::KernelBaseAccountFullV1] {
            id.kind = other;
            assert!(variant.validate(&id).is_err());
            id.kind = PolicyKind::SystemInheritanceV1;
            let mut changed = variant.clone();
            changed.kind = other;
            assert!(changed.validate(&id).is_err());
        }
    }
    #[test]
    fn rpc_policy_source_selection_refuses_combined_experiments() {
        assert_eq!(
            select_fixture_policy(false, false, false).unwrap(),
            PolicyKind::BaselineV1
        );
        assert_eq!(
            select_fixture_policy(true, false, false).unwrap(),
            PolicyKind::KernelBaseAccountFullV1
        );
        assert_eq!(
            select_fixture_policy(false, true, false).unwrap(),
            PolicyKind::SystemInheritanceV1
        );
        assert_eq!(
            select_fixture_policy(false, false, true).unwrap(),
            PolicyKind::NsiSystemInheritanceV1
        );
        for flags in [
            (true, true, false),
            (true, false, true),
            (false, true, true),
            (true, true, true),
        ] {
            assert!(select_fixture_policy(flags.0, flags.1, flags.2).is_err());
        }
        assert!(PolicyKind::NsiSystemInheritanceV1.require_live().is_err());
        assert!(PolicyKind::KernelBaseAccountFullV1.require_live().is_err());
        assert!(PolicyKind::SystemInheritanceV1.require_live().is_err());
    }
    #[test]
    fn rpc_policy_nsi_inherits_only_exact_service_read_and_preserves_original_rights() {
        let mut id = identity(PolicyKind::SystemInheritanceV1);
        let previous = fixture_access_policy(&id);
        id.kind = PolicyKind::NsiSystemInheritanceV1;
        let variant = fixture_access_policy(&id);
        assert_eq!(id.kind.account_access(), 0x2000f);
        assert_eq!(id.kind.fixture_binding_policy(), 5);
        assert!(id.kind.require_live().is_err());
        assert_eq!(variant.before, previous.before);
        assert_eq!(variant.after.owner, previous.after.owner);
        assert_eq!(variant.after.label, previous.after.label);
        assert_eq!(variant.after.control, previous.after.control);
        let at = previous.after.dacl.len();
        assert_eq!(&variant.after.dacl[8..at], &previous.after.dacl[8..]);
        // ALLOW/OI|IO/GR + exact service SID, independent binary expectation.
        let mut service_ace = vec![0, 9, 40, 0, 0, 0, 0, 128, 1, 6, 0, 0, 0, 0, 0, 5];
        for sub in [
            80u32, 2310782386, 4237065203, 3688974353, 390202159, 3511571085,
        ] {
            service_ace.extend_from_slice(&sub.to_le_bytes());
        }
        assert_eq!(&variant.after.dacl[at..], service_ace);
        for offset in [at, at + 1, at + 4, at + 16, at + 20, at + 36] {
            let mut changed = variant.clone();
            changed.after.dacl[offset] ^= 1;
            assert!(changed.validate(&id).is_err());
        }
        for kind in [
            PolicyKind::BaselineV1,
            PolicyKind::KernelBaseAccountFullV1,
            PolicyKind::SystemInheritanceV1,
            PolicyKind::SystemReadInheritanceV2,
            PolicyKind::SystemNsiReadInheritanceV3,
        ] {
            id.kind = kind;
            assert!(variant.validate(&id).is_err());
        }
        id.kind = PolicyKind::NsiSystemInheritanceV1;
        assert!(previous.validate(&id).is_err());
    }
    #[test]
    fn rpc_policy_live_constructor_requires_every_original_fixture_sentinel() {
        let environment = |key: &str| -> Option<String> {
            CONSTRUCTOR_ADMISSION
                .iter()
                .find(|(k, _)| *k == key)
                .map(|(_, value)| value.to_string())
                .or_else(|| match key {
                    "GITHUB_RUN_ID" => Some("37985757668".into()),
                    "GITHUB_RUN_ATTEMPT" => Some("1".into()),
                    _ => None,
                })
        };
        require_constructor_fixture_admission(true, true, true, environment).unwrap();
        for gates in [
            (false, true, true),
            (true, false, true),
            (true, true, false),
        ] {
            assert!(
                require_constructor_fixture_admission(gates.0, gates.1, gates.2, environment)
                    .is_err()
            );
        }
        for (key, _) in CONSTRUCTOR_ADMISSION {
            for replacement in [None, Some("wrong".to_owned())] {
                assert!(
                    require_constructor_fixture_admission(true, true, true, |current| {
                        if current == key {
                            replacement.clone()
                        } else {
                            environment(current)
                        }
                    })
                    .is_err(),
                    "accepted missing/changed {key}"
                );
            }
        }
        assert!(
            require_constructor_fixture_admission(true, true, true, |key| {
                if key == "ALYSIS_ACCOUNT_BIRTH_SELECTION" {
                    Some("station-lowbox-loader".into())
                } else {
                    environment(key)
                }
            })
            .is_err()
        );
        for key in ["GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT"] {
            for value in [
                None,
                Some("".into()),
                Some("0".into()),
                Some("1a".into()),
                Some("1".repeat(25)),
            ] {
                assert!(
                    require_constructor_fixture_admission(true, true, true, |current| {
                        if current == key {
                            value.clone()
                        } else {
                            environment(current)
                        }
                    })
                    .is_err()
                );
            }
        }
        // Cold policy evidence and source selection never set the live guard.
        assert!(!crate::account_control::station_lowbox_fixture::ENABLED.with(|s| s.get()));
        assert!(PolicyKind::KernelBaseAccountFullV1.require_live().is_err());
    }
    #[test]
    fn rpc_policy_fixed_masks_preserve_package_owner_label_and_live_refusal() {
        let mut id = identity(PolicyKind::BaselineV1);
        let baseline = fixture_access_policy(&id);
        assert_eq!(baseline.after, expected(&id, 0x2000f, 0x2000f));
        assert!(id.kind.require_live().is_ok());
        id.kind = PolicyKind::KernelBaseAccountFullV1;
        let variant = fixture_access_policy(&id);
        assert_eq!(variant.after, expected(&id, 0xf000f, 0x2000f));
        assert_eq!(variant.before, baseline.before);
        assert_eq!(variant.after.owner, baseline.after.owner);
        assert_eq!(variant.after.label, baseline.after.label);
        assert_eq!(variant.after.control, baseline.after.control);
        assert_ne!(variant.after.dacl, baseline.after.dacl);
        assert!(id.kind.require_live().is_err());
        assert!(baseline.validate(&id).is_err());
        id.kind = PolicyKind::BaselineV1;
        assert!(variant.validate(&id).is_err());
    }
    #[test]
    fn rpc_policy_rejects_forged_kind_package_owner_label_and_inheritance() {
        for kind in [
            PolicyKind::BaselineV1,
            PolicyKind::SystemReadInheritanceV2,
            PolicyKind::SystemNsiReadInheritanceV3,
            PolicyKind::KernelBaseAccountFullV1,
            PolicyKind::SystemInheritanceV1,
            PolicyKind::NsiSystemInheritanceV1,
        ] {
            let id = identity(kind);
            let policy = fixture_access_policy(&id);
            let wire = serde_json::to_value(&policy).unwrap();
            let parsed: AccessPolicy = serde_json::from_value(wire.clone()).unwrap();
            parsed.validate(&id).unwrap();
            let mut missing = wire.clone();
            missing.as_object_mut().unwrap().remove("kind");
            assert!(serde_json::from_value::<AccessPolicy>(missing).is_err());
            let mut unknown = wire;
            unknown["kind"] = serde_json::json!("arbitrary_full_access");
            assert!(serde_json::from_value::<AccessPolicy>(unknown).is_err());
            for case in 0..8 {
                let mut changed = policy.clone();
                match case {
                    0 => {
                        changed.kind = if kind == PolicyKind::BaselineV1 {
                            PolicyKind::KernelBaseAccountFullV1
                        } else {
                            PolicyKind::BaselineV1
                        }
                    }
                    1 => changed.after = expected(&id, kind.account_access(), 0xf000f),
                    2 => changed.after.owner[8] ^= 1,
                    3 => changed.before.owner[8] ^= 1,
                    4 => *changed.after.label.last_mut().unwrap() ^= 1,
                    5 => *changed.before.label.last_mut().unwrap() ^= 1,
                    6 => changed.after.control ^= 0x1000, // protected DACL
                    7 => changed.after.dacl[9] = 2,       // container inheritance on first ACE
                    _ => unreachable!(),
                }
                let parsed: AccessPolicy =
                    serde_json::from_value(serde_json::to_value(changed).unwrap()).unwrap();
                assert!(parsed.validate(&id).is_err(), "accepted mutation {case}");
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn create(name: &str, parent: HANDLE, sd: &OwnedSd) -> Result<OwnedHandle> {
        let (mut wide, length) = unicode(name)?;
        let mut name = UNICODE_STRING {
            Length: length,
            MaximumLength: length,
            Buffer: PWSTR(wide.as_mut_ptr()),
        };
        let mut attrs = attributes(&mut name, parent);
        attrs.SecurityDescriptor = sd.ptr.0.cast();
        let mut raw = HANDLE::default();
        unsafe { NtCreateDirectoryObject(&mut raw, DIRECTORY_ALL, &attrs) }.ok()?;
        Ok(OwnedHandle(raw))
    }
    fn root(sd: &OwnedSd) -> Result<OwnedHandle> {
        let mut session = 0;
        unsafe {
            windows::Win32::System::RemoteDesktop::ProcessIdToSessionId(
                std::process::id(),
                &mut session,
            )?;
        }
        let parent = open_directory(
            &format!(r"\Sessions\{session}\BaseNamedObjects"),
            HANDLE::default(),
            PARENT_ACCESS,
        )
        .map_err(|s| status_error(s, "open synthetic RPC fixture parent"))?;
        create(
            &format!(
                "Alysis.RpcPin.Test.{}",
                crate::account_state::execution::random_id()?
            ),
            parent.raw(),
            sd,
        )
    }
    fn descriptor() -> Result<OwnedSd> {
        let owner = crate::sid::current_user_sid()?;
        OwnedSd::from_sddl(&format!(
            "O:{owner}D:P(A;;0xf000f;;;SY)(A;;0xf000f;;;{owner})"
        ))
    }
    struct Identity(ExecutionRecord);
    impl NamespacePolicySource for Identity {
        fn execution(&self) -> &ExecutionRecord {
            &self.0
        }
        fn session(&self) -> u32 {
            0
        }
    }
    fn identity() -> Identity {
        let setup = crate::account_install::synthetic_ready();
        let mut binding = setup
            .binding(crate::account_install::AccountMode::Offline)
            .unwrap();
        binding.owner_sid = crate::sid::current_user_sid().unwrap();
        let id = crate::account_state::CandidateJournalIdentity::new(
            binding,
            format!(
                "Alysis.Execution.{}",
                crate::account_state::execution::random_id().unwrap()
            ),
        )
        .unwrap();
        Identity(crate::account_state::execution::fixture_record(id))
    }
    fn private_rpc(root: HANDLE, id: &Identity) -> OwnedHandle {
        let rpc = create("RPC Control", root, &descriptor().unwrap()).unwrap();
        let label = account_descriptor(id).unwrap();
        unsafe { NtSetSecurityObject(rpc.raw(), LABEL_SECURITY_INFORMATION.0, label.ptr) }
            .ok()
            .unwrap();
        rpc
    }
    #[test]
    fn initializer_rpc_account_policy_is_one_shot_private_and_query_pin_stays_readonly() {
        let id = identity();
        let root = root(&descriptor().unwrap()).unwrap();
        let _rpc = private_rpc(root.raw(), &id);
        let mut pin = RetainedRpc::open(root.raw()).unwrap();
        let policy = pin.prepare_account_access(root.raw(), &id).unwrap();
        assert!(pin.verify_account_access(root.raw(), &id, &policy).is_err());
        pin.grant_account(root.raw(), &id, &policy).unwrap();
        pin.verify_account_access(root.raw(), &id, &policy).unwrap();
        assert!(pin.grant_account(root.raw(), &id, &policy).is_err());
        assert!(pin.prepare_account_access(root.raw(), &id).is_err());
        let final_sd = account_descriptor(&id).unwrap();
        assert!(
            unsafe {
                NtSetSecurityObject(pin.handle.raw(), DACL_SECURITY_INFORMATION.0, final_sd.ptr)
            }
            .is_err()
        );
        let mut other = id;
        other.0.identity.binding.account_sid = "S-1-5-21-1-2-3-1234".into();
        assert!(
            pin.verify_account_access(root.raw(), &other, &policy)
                .is_err()
        );
        pin.close().unwrap();
    }
    #[test]
    fn initializer_rpc_account_policy_refuses_drift_wrong_label_and_retries() {
        let id = identity();
        let root = root(&descriptor().unwrap()).unwrap();
        let rpc = private_rpc(root.raw(), &id);
        let mut pin = RetainedRpc::open(root.raw()).unwrap();
        let policy = pin.prepare_account_access(root.raw(), &id).unwrap();
        let changed =
            OwnedSd::from_sddl(&format!("O:{}D:P(A;;0xf000f;;;{})", id.0.owner, id.0.owner))
                .unwrap();
        unsafe { NtSetSecurityObject(rpc.raw(), DACL_SECURITY_INFORMATION.0, changed.ptr) }
            .ok()
            .unwrap();
        assert!(pin.grant_account(root.raw(), &id, &policy).is_err());
        let drift = security_shape(&query_security(rpc.raw()).unwrap()).unwrap();
        assert_eq!(drift.dacl, descriptor_shape(&changed).unwrap().dacl);
        let original = descriptor().unwrap();
        unsafe { NtSetSecurityObject(rpc.raw(), DACL_SECURITY_INFORMATION.0, original.ptr) }
            .ok()
            .unwrap();
        assert!(pin.grant_account(root.raw(), &id, &policy).is_err());
        for bad in ["ME", "S-1-16-0"] {
            let label = OwnedSd::from_sddl(&format!("S:(ML;;NW;;;{bad})")).unwrap();
            unsafe { NtSetSecurityObject(rpc.raw(), LABEL_SECURITY_INFORMATION.0, label.ptr) }
                .ok()
                .unwrap();
            let pin = RetainedRpc::open(root.raw()).unwrap();
            assert!(pin.prepare_account_access(root.raw(), &id).is_err());
        }
    }
    #[test]
    fn initializer_rpc_pin_refuses_other_root_security_drift_and_write_authority() {
        let sd = descriptor().unwrap();
        let root = root(&sd).unwrap();
        assert!(RetainedRpc::open(root.raw()).is_err());
        let rpc = create("RPC Control", root.raw(), &sd).unwrap();
        let retained = RetainedRpc::open(root.raw()).unwrap();
        let other = create("other", root.raw(), &sd).unwrap();
        let _other_rpc = create("RPC Control", other.raw(), &sd).unwrap();
        assert!(retained.verify(other.raw()).is_err());
        assert!(
            unsafe {
                NtSetSecurityObject(retained.handle.raw(), DACL_SECURITY_INFORMATION.0, sd.ptr)
            }
            .is_err()
        );
        let owner = crate::sid::current_user_sid().unwrap();
        let changed = OwnedSd::from_sddl(&format!("O:{owner}D:P(A;;0xf000f;;;{owner})")).unwrap();
        unsafe { NtSetSecurityObject(rpc.raw(), DACL_SECURITY_INFORMATION.0, changed.ptr) }
            .ok()
            .unwrap();
        assert!(retained.verify(root.raw()).is_err());
        unsafe { NtSetSecurityObject(rpc.raw(), DACL_SECURITY_INFORMATION.0, sd.ptr) }
            .ok()
            .unwrap();
        retained.verify(root.raw()).unwrap();
        drop(rpc); // The original read-only pin still retains the same object.
        retained.verify(root.raw()).unwrap();
        retained.close().unwrap();
        assert!(RetainedRpc::open(root.raw()).is_err());
    }
    #[test]
    fn initializer_rpc_pin_refuses_symbolic_link_to_shared_target() {
        // Test-only missing SDK binding; native signature documented by PHNT:
        // https://github.com/winsiderss/phnt/blob/master/ntobapi.h
        #[link(name = "ntdll")]
        unsafe extern "system" {
            fn NtCreateSymbolicLinkObject(
                handle: *mut HANDLE,
                access: u32,
                attributes: *const OBJECT_ATTRIBUTES,
                target: *const UNICODE_STRING,
            ) -> NTSTATUS;
        }
        let global = fixture_global_security().unwrap();
        let sd = descriptor().unwrap();
        let root = root(&sd).unwrap();
        let (mut wide, length) = unicode("RPC Control").unwrap();
        let mut name = UNICODE_STRING {
            Length: length,
            MaximumLength: length,
            Buffer: PWSTR(wide.as_mut_ptr()),
        };
        let mut attrs = attributes(&mut name, root.raw());
        attrs.SecurityDescriptor = sd.ptr.0.cast();
        let (mut target, length) = unicode(r"\BaseNamedObjects").unwrap();
        let target = UNICODE_STRING {
            Length: length,
            MaximumLength: length,
            Buffer: PWSTR(target.as_mut_ptr()),
        };
        let mut raw = HANDLE::default();
        unsafe { NtCreateSymbolicLinkObject(&mut raw, 0x000f0001, &attrs, &target) }
            .ok()
            .unwrap();
        let _link = OwnedHandle(raw);
        assert!(RetainedRpc::open(root.raw()).is_err());
        assert_eq!(fixture_global_security().unwrap(), global);
    }
}
