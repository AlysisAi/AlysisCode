//! Read-only observations of the original suspended child's own kernel objects.
//! These results explain startup failures; they never authorize a launch or ACL change.
use super::*;
use windows::Win32::Security::{
    Authorization::{GetSecurityInfo, SE_KERNEL_OBJECT},
    *,
};
use windows::Win32::System::SystemServices::PROCESS_MITIGATION_SYSTEM_CALL_DISABLE_POLICY;

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Observation {
    access: Option<u32>,
    hresult: Option<i32>,
}
impl Observation {
    fn from(result: Result<u32>) -> Self {
        match result {
            Ok(access) => Self {
                access: Some(access),
                hresult: None,
            },
            Err(error) => Self {
                access: None,
                hresult: native_code(&error),
            },
        }
    }
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct LabeledAccess {
    selected_token: Observation,
    child_process: Observation,
    child_token: Observation,
}

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Probe {
    diagnostic_only: bool,
    original_child: (u32, u64),
    selected_token_dacl: Observation,
    child_process_dacl: Observation,
    child_token_dacl: Observation,
    #[serde(default)]
    labeled_self_access: Option<LabeledAccess>,
    native_process_query: Option<Observation>,
    native_token_full_access: Option<Observation>,
    impersonation_hresult: Option<i32>,
    #[serde(default)]
    system_call_disable_flags: Option<u32>,
    #[serde(default)]
    system_call_disable_hresult: Option<i32>,
}
impl Probe {
    pub(super) fn observe(
        child: HANDLE,
        token: &OwnedHandle,
        original: (u32, u64),
    ) -> Result<Self> {
        crate::account_impersonation::require_process_identity()?;
        ensure!(
            unsafe { GetProcessId(child) } == original.0
                && bootstrap::pull::creation_time(child)? == original.1,
            "startup diagnostic original child differs"
        );
        let mut probe = Self {
            diagnostic_only: true,
            original_child: original,
            selected_token_dacl: Observation::from(granted(token.raw(), token.raw(), true, false)),
            child_process_dacl: Observation::from(granted(child, token.raw(), false, false)),
            child_token_dacl: Observation::from(child_token_granted(child, token.raw(), false)),
            // Retain the old DACL-only model separately. Omitting the mandatory
            // label can change AccessCheck's answer for Low integrity callers;
            // it is not evidence that the actual object's ACL needs widening.
            labeled_self_access: Some(LabeledAccess {
                selected_token: Observation::from(granted(token.raw(), token.raw(), true, true)),
                child_process: Observation::from(granted(child, token.raw(), false, true)),
                child_token: Observation::from(child_token_granted(child, token.raw(), true)),
            }),
            native_process_query: None,
            native_token_full_access: None,
            impersonation_hresult: None,
            system_call_disable_flags: None,
            system_call_disable_hresult: None,
        };
        // Observe the original suspended process, never change mitigation policy.
        // Bit 0 disables Win32k system calls and would forbid NtGdiInit outright.
        let mut policy = PROCESS_MITIGATION_SYSTEM_CALL_DISABLE_POLICY::default();
        match unsafe {
            GetProcessMitigationPolicy(
                child,
                ProcessSystemCallDisablePolicy,
                (&mut policy as *mut PROCESS_MITIGATION_SYSTEM_CALL_DISABLE_POLICY).cast(),
                size_of_val(&policy),
            )
        } {
            Ok(()) => probe.system_call_disable_flags = Some(unsafe { policy.Anonymous.Flags }),
            Err(error) => probe.system_call_disable_hresult = Some(error.code().0),
        }
        let native = crate::account_impersonation::with_token(token, || {
            let process = Observation::from((|| -> Result<u32> {
                let access = PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SYNCHRONIZE;
                let opened = OwnedHandle(unsafe { OpenProcess(access, false, original.0)? });
                ensure!(
                    unsafe { CompareObjectHandles(opened.raw(), child) }.as_bool(),
                    "startup diagnostic reopened another process"
                );
                Ok(access.0)
            })());
            let primary = Observation::from((|| -> Result<u32> {
                let mut raw = HANDLE::default();
                unsafe {
                    OpenProcessToken(child, TOKEN_ALL_ACCESS, &mut raw)?;
                }
                drop(OwnedHandle(raw));
                Ok(TOKEN_ALL_ACCESS.0)
            })());
            Ok((process, primary))
        });
        match native {
            Ok((process, primary)) => {
                probe.native_process_query = Some(process);
                probe.native_token_full_access = Some(primary);
            }
            Err(error) => probe.impersonation_hresult = native_code(&error),
        }
        crate::account_impersonation::require_process_identity()?;
        probe.require(original)?;
        Ok(probe)
    }
    pub(super) fn require(&self, original: (u32, u64)) -> Result<()> {
        ensure!(
            self.diagnostic_only
                && self.original_child == original
                && original.0 != 0
                && original.1 != 0,
            "startup diagnostic subject differs"
        );
        ensure!(
            self.native_process_query.is_some() == self.native_token_full_access.is_some(),
            "startup diagnostic native observation is partial"
        );
        ensure!(
            self.system_call_disable_flags.is_none() || self.system_call_disable_hresult.is_none(),
            "startup mitigation query contradicts itself"
        );
        for value in [
            &self.selected_token_dacl,
            &self.child_process_dacl,
            &self.child_token_dacl,
        ]
        .into_iter()
        .chain(self.native_process_query.iter())
        .chain(self.native_token_full_access.iter())
        .chain(
            self.labeled_self_access
                .iter()
                .flat_map(|row| [&row.selected_token, &row.child_process, &row.child_token]),
        ) {
            ensure!(
                value.access.is_none() || value.hresult.is_none(),
                "startup diagnostic result contradicts itself"
            );
        }
        ensure!(
            serde_json::to_vec(self)?.len() <= 2048,
            "startup diagnostic exceeds bound"
        );
        Ok(())
    }
}

fn child_token_granted(child: HANDLE, token: HANDLE, with_label: bool) -> Result<u32> {
    let mut raw = HANDLE::default();
    unsafe {
        OpenProcessToken(
            child,
            TOKEN_QUERY | TOKEN_ACCESS_MASK(READ_CONTROL.0),
            &mut raw,
        )?;
    }
    let primary = OwnedHandle(raw);
    granted(primary.raw(), token, true, with_label)
}

fn descriptor_information(with_label: bool) -> OBJECT_SECURITY_INFORMATION {
    let basic = OWNER_SECURITY_INFORMATION | GROUP_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION;
    if with_label {
        basic | LABEL_SECURITY_INFORMATION
    } else {
        basic
    }
}

fn granted(object: HANDLE, token: HANDLE, token_object: bool, with_label: bool) -> Result<u32> {
    let mut raw = PSECURITY_DESCRIPTOR::default();
    unsafe {
        GetSecurityInfo(
            object,
            SE_KERNEL_OBJECT,
            descriptor_information(with_label),
            None,
            None,
            None,
            None,
            Some(&mut raw),
        )
    }
    .ok()?;
    let descriptor = OwnedSd::from_raw(raw);
    let mapping = if token_object {
        GENERIC_MAPPING {
            GenericRead: TOKEN_READ.0,
            GenericWrite: TOKEN_WRITE.0,
            GenericExecute: TOKEN_EXECUTE.0,
            GenericAll: TOKEN_ALL_ACCESS.0,
        }
    } else {
        GENERIC_MAPPING {
            GenericRead: 0x20410,
            GenericWrite: 0x20bea,
            GenericExecute: 0x120000,
            GenericAll: PROCESS_ALL_ACCESS.0,
        }
    };
    let mut raw = HANDLE::default();
    unsafe {
        DuplicateToken(token, SecurityImpersonation, &mut raw)?;
    }
    let token = OwnedHandle(raw);
    let mut privileges = [0usize; 128];
    let mut length = size_of_val(&privileges) as u32;
    let mut access = 0;
    let mut allowed = false.into();
    unsafe {
        AccessCheck(
            descriptor.ptr,
            token.raw(),
            0x02000000,
            &mapping,
            Some(privileges.as_mut_ptr().cast()),
            &mut length,
            &mut access,
            &mut allowed,
        )?;
    }
    Ok(if allowed.as_bool() { access } else { 0 })
}

#[test]
fn station_lowbox_startup_diagnostic_requires_original_subject_and_cannot_claim_authority() {
    let value = serde_json::json!({"diagnostic_only":true,"original_child":[7,9],
        "selected_token_dacl":{"access":0,"hresult":null},
        "child_process_dacl":{"access":null,"hresult":-2147024891},
        "child_token_dacl":{"access":null,"hresult":null},
        "native_process_query":null,"native_token_full_access":null,"impersonation_hresult":null});
    let probe: Probe = serde_json::from_value(value.clone()).unwrap();
    probe.require((7, 9)).unwrap();
    assert!(probe.require((7, 10)).is_err());
    let mut changed = value.clone();
    changed["diagnostic_only"] = false.into();
    assert!(
        serde_json::from_value::<Probe>(changed)
            .unwrap()
            .require((7, 9))
            .is_err()
    );
    let mut changed = value.clone();
    changed["production_ready"] = true.into();
    assert!(serde_json::from_value::<Probe>(changed).is_err());
    let mut changed = value;
    changed["selected_token_dacl"]["hresult"] = (-2147024891i32).into();
    assert!(
        serde_json::from_value::<Probe>(changed)
            .unwrap()
            .require((7, 9))
            .is_err()
    );
}

#[test]
fn station_startup_labeled_model_keeps_failed_query_distinct_from_denial() {
    assert!(descriptor_information(true).contains(LABEL_SECURITY_INFORMATION));
    assert!(!descriptor_information(false).contains(LABEL_SECURITY_INFORMATION));
    // Failed label reads remain explicit failures; they cannot become an empty
    // successful mask or disappear into the historical DACL-only observation.
    let denied = ERROR_ACCESS_DENIED.to_hresult().0;
    let mut value = serde_json::json!({"diagnostic_only":true,"original_child":[7,9],
        "selected_token_dacl":{"access":0,"hresult":null},
        "child_process_dacl":{"access":0,"hresult":null},
        "child_token_dacl":{"access":0,"hresult":null},
        "native_process_query":null,"native_token_full_access":null,"impersonation_hresult":null,
        "labeled_self_access": {
            "selected_token":{"access":null,"hresult":denied},
            "child_process":{"access":0,"hresult":null},
            "child_token":{"access":TOKEN_ALL_ACCESS.0,"hresult":null}}});
    let probe: Probe = serde_json::from_value(value.clone()).unwrap();
    probe.require((7, 9)).unwrap();
    let round_trip = serde_json::to_value(&probe).unwrap();
    assert_eq!(
        round_trip["labeled_self_access"],
        value["labeled_self_access"]
    );
    value["labeled_self_access"]["selected_token"]["access"] = 0.into();
    assert!(
        serde_json::from_value::<Probe>(value)
            .unwrap()
            .require((7, 9))
            .is_err()
    );
}
