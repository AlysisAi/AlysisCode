//! Fixed CI-only primary-AppContainer checks of the original private RPC object.
//! This module grants no rights. Parsed evidence cannot construct a retained pin.
use super::*;
use serde::{Deserialize, Serialize};
use windows::Win32::System::Threading::GetCurrentProcess;

const DENIED: i32 = 0xc0000022u32 as i32;
const FORBIDDEN: [u32; 4] = [0x10000, 0x40000, 0x80000, DIRECTORY_ALL];

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Subject {
    pub policy: PolicyKind,
    pub network: crate::appcontainer::NetworkAccess,
    pub owner: String,
    pub account: String,
    pub container: String,
    pub package: String,
    pub session: u32,
}
impl Subject {
    fn descriptor(&self) -> Result<OwnedSd> {
        ensure!(
            self.container.starts_with("Alysis.Execution.")
                && self.container.len() == "Alysis.Execution.".len() + 32
                && self.container["Alysis.Execution.".len()..]
                    .bytes()
                    .all(|c| c.is_ascii_hexdigit())
                && crate::appcontainer::derive(&self.container)?.string()? == self.package,
            "RPC probe package differs from fixed execution"
        );
        subject_descriptor(&self.owner, &self.account, &self.package, self.policy)
    }
    fn path(&self) -> String {
        format!(
            "\\Sessions\\{}\\AppContainerNamedObjects\\{}",
            self.session, self.package
        )
    }
    fn current_session(&self) -> Result<()> {
        let mut session = 0;
        unsafe {
            windows::Win32::System::RemoteDesktop::ProcessIdToSessionId(
                std::process::id(),
                &mut session,
            )?;
        }
        ensure!(session == self.session, "RPC probe session differs");
        Ok(())
    }
}

/// Only live native handles hold the original object across child execution.
/// No constructors deserialize a pin, and no pin is inherited by the child.
pub(crate) struct Pin {
    subject: Subject,
    root: OwnedHandle,
    rpc: OwnedHandle,
    security: SecurityShape,
    root_security: SecurityShape,
}
impl Pin {
    pub(crate) fn open(subject: Subject) -> Result<Self> {
        crate::account_impersonation::require_process_identity()?;
        subject.current_session()?;
        let security = descriptor_shape(&subject.descriptor()?)?;
        let root = open_directory(&subject.path(), HANDLE::default(), QUERY_ACCESS)
            .map_err(|s| status_error(s, "pin private RPC root"))?;
        let rpc = open_directory("RPC Control", root.raw(), PACKAGE_ACCESS)
            .map_err(|s| status_error(s, "pin private RPC object"))?;
        let root_security = security_shape(&query_security(root.raw())?)?;
        let pin = Self {
            subject,
            root,
            rpc,
            security,
            root_security,
        };
        pin.verify()?;
        Ok(pin)
    }
    pub(crate) fn verify(&self) -> Result<()> {
        crate::account_namespace::verify_noninheritable(self.root.raw())?;
        crate::account_namespace::verify_noninheritable(self.rpc.raw())?;
        ensure!(
            object_name::directory_name(self.root.raw())? == self.subject.path(),
            "RPC root resolved outside private namespace"
        );
        object_name::require_child(self.root.raw(), self.rpc.raw(), "RPC Control")?;
        ensure!(
            security_shape(&query_security(self.root.raw())?)? == self.root_security,
            "original private namespace root security changed"
        );
        ensure!(
            security_shape(&query_security(self.rpc.raw())?)? == self.security,
            "private RPC security changed"
        );
        let fresh = open_directory("RPC Control", self.root.raw(), QUERY_ACCESS)
            .map_err(|s| status_error(s, "reopen original private RPC"))?;
        let verified = self.same(fresh.raw());
        unsafe { CloseHandle(fresh.into_raw()) }
            .context("RPC verification handle close uncertain")?;
        verified
    }
    fn same(&self, handle: HANDLE) -> Result<()> {
        crate::account_namespace::verify_noninheritable(handle)?;
        object_name::require_child(self.root.raw(), handle, "RPC Control")?;
        ensure!(
            unsafe { CompareObjectHandles(self.rpc.raw(), handle) }.as_bool(),
            "RPC open differs from original pinned object"
        );
        Ok(())
    }
    pub(crate) fn close(self) -> Result<()> {
        self.verify()?;
        // Attempt both closes even if the first result is uncertain.
        let rpc = unsafe { CloseHandle(self.rpc.into_raw()) };
        let root = unsafe { CloseHandle(self.root.into_raw()) };
        rpc.context("original RPC pin close uncertain")?;
        root.context("original RPC root close uncertain")?;
        Ok(())
    }
}

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Proof {
    subject: Subject,
    pub passed: bool,
    stage: String,
    actor_verified: bool,
    maximum_access: Option<u32>,
    open_denials: Vec<(u32, i32)>,
    duplicate_denial: Option<i32>,
    mutation_denials: Vec<(u32, i32)>,
    unchanged: bool,
    closed: bool,
}
impl Proof {
    pub(crate) fn require(&self, subject: &Subject) -> Result<()> {
        drop(subject.descriptor()?);
        ensure!(
            &self.subject == subject,
            "RPC proof differs from original subject"
        );
        ensure!(
            [
                "actor",
                "pin",
                "maximum",
                "open",
                "duplicate",
                "mutation",
                "close",
                "complete"
            ]
            .contains(&self.stage.as_str())
                && self.open_denials.len() <= FORBIDDEN.len()
                && self.mutation_denials.len() <= 3,
            "RPC proof exceeds fixed shape"
        );
        if self.passed {
            ensure!(
                self.stage == "complete"
                    && self.actor_verified
                    && self.maximum_access == Some(PACKAGE_ACCESS)
                    && self.open_denials == FORBIDDEN.map(|a| (a, DENIED))
                    && self.duplicate_denial == Some(DENIED)
                    && self.mutation_denials
                        == [
                            DACL_SECURITY_INFORMATION.0,
                            OWNER_SECURITY_INFORMATION.0,
                            LABEL_SECURITY_INFORMATION.0
                        ]
                        .map(|a| (a, DENIED))
                    && self.unchanged
                    && self.closed,
                "RPC boundary proof incomplete"
            );
        } else {
            ensure!(
                self.stage != "complete",
                "failed RPC proof claims completion"
            );
        }
        Ok(())
    }
}

#[repr(C)]
#[derive(Default)]
struct Basic {
    attributes: u32,
    granted_access: u32,
    handle_count: u32,
    pointer_count: u32,
    reserved: [u32; 10],
}
#[link(name = "ntdll")]
unsafe extern "system" {
    fn NtQueryObject(
        handle: HANDLE,
        class: u32,
        data: *mut core::ffi::c_void,
        length: u32,
        returned: *mut u32,
    ) -> NTSTATUS;
    fn NtDuplicateObject(
        source_process: HANDLE,
        source: HANDLE,
        target_process: HANDLE,
        target: *mut HANDLE,
        access: u32,
        attributes: u32,
        options: u32,
    ) -> NTSTATUS;
}
fn access(handle: HANDLE) -> Result<u32> {
    let mut data = Basic::default();
    let mut returned = 0;
    unsafe {
        NtQueryObject(
            handle,
            0,
            (&mut data as *mut Basic).cast(),
            size_of::<Basic>() as u32,
            &mut returned,
        )
    }
    .ok()?;
    ensure!(
        returned as usize == size_of::<Basic>() && data.attributes & 2 == 0,
        "RPC basic handle observation differs"
    );
    Ok(data.granted_access)
}

/// Only the fixed child calls this, after its normal original-process checks.
/// Independently verify its actual primary token before any negative mutation.
pub(crate) fn probe(subject: Subject) -> Result<Proof> {
    let mut proof = Proof {
        subject: subject.clone(),
        passed: false,
        stage: "actor".into(),
        actor_verified: false,
        maximum_access: None,
        open_denials: Vec::new(),
        duplicate_denial: None,
        mutation_denials: Vec::new(),
        unchanged: false,
        closed: false,
    };
    let observed = (|| -> Result<()> {
        crate::account_impersonation::require_process_identity()?;
        subject.current_session()?;
        crate::account_token::verify_account_process(
            unsafe { GetCurrentProcess() },
            &subject.container,
            &subject.account,
            subject.network,
        )?;
        proof.actor_verified = true;
        proof.stage = "pin".into();
        let pin = Pin::open(subject.clone())?;
        let result = (|| -> Result<()> {
            proof.stage = "maximum".into();
            let maximum = open_directory("RPC Control", pin.root.raw(), 0x02000000)
                .map_err(|s| status_error(s, "maximum private RPC open"))?;
            let read = (|| -> Result<u32> {
                pin.same(maximum.raw())?;
                access(maximum.raw())
            })();
            unsafe { CloseHandle(maximum.into_raw()) }
                .context("RPC maximum handle close uncertain")?;
            proof.maximum_access = Some(read?);
            ensure!(
                proof.maximum_access == Some(PACKAGE_ACCESS),
                "RPC maximum access exceeds fixed package grant"
            );
            proof.stage = "open".into();
            for mask in FORBIDDEN {
                let status = match open_directory("RPC Control", pin.root.raw(), mask) {
                    Err(status) => status.0,
                    Ok(handle) => {
                        unsafe {
                            CloseHandle(handle.into_raw())?;
                        }
                        0
                    }
                };
                proof.open_denials.push((mask, status));
                ensure!(
                    status == DENIED,
                    "RPC prohibited open was not access denied"
                );
            }
            proof.stage = "duplicate".into();
            let mut raw = HANDLE::default();
            let status = unsafe {
                NtDuplicateObject(
                    GetCurrentProcess(),
                    pin.rpc.raw(),
                    GetCurrentProcess(),
                    &mut raw,
                    DIRECTORY_ALL,
                    0,
                    0,
                )
            };
            if status.is_ok() {
                unsafe {
                    CloseHandle(raw)?;
                }
            }
            proof.duplicate_denial = Some(status.0);
            ensure!(
                status.0 == DENIED,
                "RPC handle upgrade was not access denied"
            );
            proof.stage = "mutation".into();
            let descriptor = subject.descriptor()?;
            for information in [
                DACL_SECURITY_INFORMATION,
                OWNER_SECURITY_INFORMATION,
                LABEL_SECURITY_INFORMATION,
            ] {
                // Even a no-op publication requires the native mutation right.
                // Original owner/label/DACL bytes are checked through the pin.
                let status =
                    unsafe { NtSetSecurityObject(pin.rpc.raw(), information.0, descriptor.ptr) };
                proof.mutation_denials.push((information.0, status.0));
                ensure!(
                    status.0 == DENIED,
                    "RPC security mutation was not access denied"
                );
            }
            pin.verify()?;
            proof.unchanged = true;
            Ok(())
        })();
        let close = pin.close();
        proof.closed = close.is_ok();
        result?;
        proof.stage = "close".into();
        close?;
        Ok(())
    })();
    if observed.is_ok() {
        proof.passed = true;
        proof.stage = "complete".into();
    }
    proof.require(&subject)?;
    Ok(proof)
}

#[test]
fn rpc_lowbox_proof_refuses_missing_denials_widened_access_and_rebound_subject() {
    let container = "Alysis.Execution.0123456789abcdef0123456789abcdef";
    let subject = Subject {
        network: crate::appcontainer::NetworkAccess::Off,
        policy: PolicyKind::BaselineV1,
        owner: "S-1-5-21-1-2-3-4".into(),
        account: "S-1-5-21-1-2-3-5".into(),
        container: container.into(),
        package: crate::appcontainer::derive(container)
            .unwrap()
            .string()
            .unwrap(),
        session: 7,
    };
    let proof = Proof {
        subject: subject.clone(),
        passed: true,
        stage: "complete".into(),
        actor_verified: true,
        maximum_access: Some(PACKAGE_ACCESS),
        open_denials: FORBIDDEN.map(|a| (a, DENIED)).to_vec(),
        duplicate_denial: Some(DENIED),
        mutation_denials: [
            DACL_SECURITY_INFORMATION.0,
            OWNER_SECURITY_INFORMATION.0,
            LABEL_SECURITY_INFORMATION.0,
        ]
        .map(|a| (a, DENIED))
        .to_vec(),
        unchanged: true,
        closed: true,
    };
    proof.require(&subject).unwrap();
    for key in ["actor_verified", "unchanged", "closed"] {
        let mut wire = serde_json::to_value(&proof).unwrap();
        wire[key] = false.into();
        assert!(
            serde_json::from_value::<Proof>(wire)
                .unwrap()
                .require(&subject)
                .is_err()
        );
    }
    let mut bad = proof.clone();
    bad.maximum_access = Some(DIRECTORY_ALL);
    assert!(bad.require(&subject).is_err());
    let mut bad = proof.clone();
    bad.open_denials.pop();
    assert!(bad.require(&subject).is_err());
    let mut bad = proof.clone();
    bad.duplicate_denial = Some(0);
    assert!(bad.require(&subject).is_err());
    let mut bad = proof.clone();
    bad.mutation_denials[2].1 = 0;
    assert!(bad.require(&subject).is_err());
    let mut different = subject.clone();
    different.session += 1;
    assert!(proof.require(&different).is_err());
    let mut different = subject.clone();
    different.policy = PolicyKind::KernelBaseAccountFullV1;
    assert!(proof.require(&different).is_err());
    let mut different = subject.clone();
    different.network = crate::appcontainer::NetworkAccess::On;
    assert!(proof.require(&different).is_err());
}
