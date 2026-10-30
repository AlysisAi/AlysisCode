//! Private candidate offline fence transaction. No caller-selected identifiers,
//! permissive rule, security repair, CLI route or unowned-object adoption.
//! The only native caller owns a marker-zero OwnedSetupUpdate and its lease.
//! BFE commits atomically; registry/BFE commits are deliberately not conflated.

use super::*;
use anyhow::Context;
use windows::Win32::Security::{
    ACE_HEADER, ACL, DACL_SECURITY_INFORMATION, GROUP_SECURITY_INFORMATION, GetAce,
    GetSecurityDescriptorDacl, GetSecurityDescriptorGroup, GetSecurityDescriptorOwner, IsValidAcl,
    OWNER_SECURITY_INFORMATION, PSID, SE_DACL_PROTECTED,
};
use windows::core::PWSTR;

const VALUE: &str = "WfpOwnership";
const FILTER_NOT_FOUND: u32 = 0x8032_0003;
const SUBLAYER_NOT_FOUND: u32 = 0x8032_0007;
// Documented FWPM_GENERIC_ALL: STANDARD_RIGHTS_REQUIRED plus all eleven WFP
// rights. Explicit numeric bits avoid generic-mask mapping/readback ambiguity.
const ALL: u32 = 0x000f_07ff;
const SECURITY: &str = "O:BAG:BAD:P(A;;0x000f07ff;;;SY)(A;;0x000f07ff;;;BA)";

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
enum Phase {
    PendingPresent,
    Present,
    PendingRemove,
    Removed,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Ownership {
    version: u32,
    binding: AccountBinding,
    online_sid: String,
    phase: Phase,
}
impl Ownership {
    fn new(record: &SetupRecord, phase: Phase) -> Result<Self> {
        Ok(Self {
            version: 1,
            binding: record.binding(AccountMode::Offline)?,
            online_sid: record.account(AccountMode::Online)?.sid.clone(),
            phase,
        })
    }
    fn validate(&self, record: &SetupRecord) -> Result<()> {
        ensure!(
            *self == Self::new(record, self.phase)?,
            "candidate WFP ownership belongs to another installation or principal"
        );
        filter_keys(record)?;
        Ok(())
    }
}

fn decode(text: Option<&str>, record: &SetupRecord) -> Result<Option<Ownership>> {
    text.map(|text| {
        ensure!(
            text.len() <= 16 * 1024,
            "candidate WFP ownership is oversized"
        );
        let value: Ownership = serde_json::from_str(text)?;
        value.validate(record)?;
        Ok(value)
    })
    .transpose()
}

pub(in crate::account_install) fn require_complete(
    record: &SetupRecord,
    text: Option<&str>,
) -> Result<()> {
    ensure!(
        decode(text, record)?.is_some_and(|value| value.phase == Phase::Present),
        "candidate offline WFP ownership is absent, pending or removed"
    );
    Ok(())
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub(super) struct Observed {
    sublayer: bool,
    filters: [bool; 2],
    pub(super) complete: bool,
}
impl Observed {
    fn empty(self) -> bool {
        !self.sublayer && self.filters == [false; 2]
    }
}

trait Store {
    fn read(&self, record: &SetupRecord) -> Result<Option<Ownership>>;
    fn write(&mut self, value: &Ownership) -> Result<()>;
}
trait Transaction {
    fn observe(&self, record: &SetupRecord) -> Result<Observed>;
    fn remove(&mut self, record: &SetupRecord, observed: Observed) -> Result<()>;
    fn add(&mut self, record: &SetupRecord) -> Result<()>;
    fn commit(&mut self) -> Result<()>;
}

#[derive(Clone, Copy, Debug)]
enum Desired {
    Present,
    #[cfg_attr(not(test), allow(dead_code))] // Native removal is intentionally gated off.
    Removed,
}

// Called only after opening ONE BFE write transaction. All ownership admission
// happens before registry intent; all intent writes complete before mutation.
// On any error the transaction drops/aborts, except an ambiguous successful
// commit may already be durable. In either case the recorded intent survives.
fn reconcile(
    store: &mut impl Store,
    transaction: &mut impl Transaction,
    record: &SetupRecord,
    desired: Desired,
) -> Result<()> {
    let prior = store.read(record)?;
    if let Some(prior) = &prior {
        prior.validate(record)?;
    }
    let observed = transaction.observe(record)?;
    ensure!(
        prior.is_some() || observed.empty(),
        "candidate WFP namespace exists without recorded ownership; refusing adoption"
    );
    if let Some(prior) = &prior {
        if prior.phase == Phase::Removed {
            ensure!(
                observed.empty(),
                "retired candidate WFP namespace reappeared; refusing adoption"
            );
        }
    }
    match desired {
        Desired::Present => {
            ensure!(
                !prior
                    .as_ref()
                    .is_some_and(|p| matches!(p.phase, Phase::PendingRemove | Phase::Removed)),
                "candidate WFP removal cannot be silently reversed"
            );
            store.write(&Ownership::new(record, Phase::PendingPresent)?)?;
            if !observed.complete {
                transaction.remove(record, observed)?;
                transaction.add(record)?;
            }
            ensure!(
                transaction.observe(record)?.complete,
                "candidate WFP write did not produce the exact offline fence"
            );
        }
        Desired::Removed => {
            store.write(&Ownership::new(record, Phase::PendingRemove)?)?;
            transaction.remove(record, observed)?;
            ensure!(
                transaction.observe(record)?.empty(),
                "candidate WFP removal is incomplete"
            );
        }
    }
    transaction
        .commit()
        .context("candidate WFP commit outcome may be uncertain; preserve ownership intent")?;
    // A failed write/readback here leaves an owned BFE result but no readiness.
    // Reopening revalidates the exact objects; it never deletes them as rollback.
    store.write(&Ownership::new(
        record,
        match desired {
            Desired::Present => Phase::Present,
            Desired::Removed => Phase::Removed,
        },
    )?)
}

struct RegistryStore<'a>(&'a crate::reg::Key);
impl Store for RegistryStore<'_> {
    fn read(&self, record: &SetupRecord) -> Result<Option<Ownership>> {
        decode(self.0.get_sz(VALUE)?.as_deref(), record)
    }
    fn write(&mut self, value: &Ownership) -> Result<()> {
        let text = serde_json::to_string(value)?;
        ensure!(
            text.len() <= 16 * 1024,
            "candidate WFP ownership exceeds bound"
        );
        self.0.set_sz(VALUE, &text)?;
        self.0.flush()?;
        ensure!(
            self.0.get_sz(VALUE)?.as_ref() == Some(&text),
            "candidate WFP ownership readback failed; preserve uncertain outcome"
        );
        Ok(())
    }
}

pub(in crate::account_install) fn prepare(
    record: &SetupRecord,
    key: &crate::reg::Key,
) -> Result<()> {
    // This is not a public record-based authority constructor. Only the owned
    // setup hook can reach it; that hook validates the same handle and poisons
    // itself before this call. No account activation is performed here.
    let mut transaction = NativeTransaction::begin()?;
    reconcile(
        &mut RegistryStore(key),
        &mut transaction,
        record,
        Desired::Present,
    )
}

pub(in crate::account_install) fn refuse_removal() -> Result<()> {
    // Account disablement and an execution lease do not revoke existing tokens.
    // Keep this unavailable before registry/BFE access until an opaque complete
    // execution/account quiescence authority is implemented and qualified.
    anyhow::bail!("candidate WFP removal is unavailable until account/process quiescence is proven")
}

struct NativeTransaction(Engine);
impl NativeTransaction {
    fn begin() -> Result<Self> {
        let mut handle = HANDLE::default();
        let code = unsafe { FwpmEngineOpen0(PCWSTR::null(), 0xffff_ffff, None, None, &mut handle) };
        ensure!(code == 0, "open candidate WFP writer: {code:#x}");
        let engine = Engine(handle);
        let code = unsafe { FwpmTransactionBegin0(engine.0, 0) };
        ensure!(
            code == 0,
            "begin candidate WFP write transaction: {code:#x}"
        );
        Ok(Self(engine))
    }
}
impl Transaction for NativeTransaction {
    fn observe(&self, record: &SetupRecord) -> Result<Observed> {
        observe(self.0.0, record)
    }
    fn remove(&mut self, record: &SetupRecord, observed: Observed) -> Result<()> {
        // Only exact keys already admitted within this same transaction. Do not
        // treat not-found as a successful delete: it would contradict admission.
        for (key, exists) in filter_keys(record)?.into_iter().zip(observed.filters) {
            if exists {
                let code = unsafe { FwpmFilterDeleteByKey0(self.0.0, &key) };
                ensure!(code == 0, "delete owned candidate filter: {code:#x}");
            }
        }
        if observed.sublayer {
            let code = unsafe { FwpmSubLayerDeleteByKey0(self.0.0, &OFFLINE_SUBLAYER) };
            ensure!(code == 0, "delete owned candidate sublayer: {code:#x}");
        }
        Ok(())
    }
    fn add(&mut self, record: &SetupRecord) -> Result<()> {
        let security = OwnedSd::from_sddl(SECURITY)?;
        validate_security(security.ptr)?;
        let mut name = crate::util::wstr("Alysis candidate offline fence");
        let mut tag = serde_json::to_vec(&Tag::expected(record, Kind::Sublayer)?)?;
        let sublayer = FWPM_SUBLAYER0 {
            subLayerKey: OFFLINE_SUBLAYER,
            flags: FWPM_SUBLAYER_FLAG_PERSISTENT,
            displayData: FWPM_DISPLAY_DATA0 {
                name: PWSTR(name.as_mut_ptr()),
                ..Default::default()
            },
            providerData: FWP_BYTE_BLOB {
                size: tag.len() as u32,
                data: tag.as_mut_ptr(),
            },
            weight: SUBLAYER_WEIGHT,
            ..Default::default()
        };
        let code = unsafe { FwpmSubLayerAdd0(self.0.0, &sublayer, Some(security.ptr)) };
        ensure!(
            code == 0,
            "create fresh candidate sublayer (collisions are not adopted): {code:#x}"
        );
        for index in 0..2 {
            let filter = Filter::new(record, index)?;
            validate_filter(&filter.value, record, index)?;
            let code = unsafe { FwpmFilterAdd0(self.0.0, &filter.value, Some(security.ptr), None) };
            ensure!(
                code == 0,
                "create fresh candidate filter (collisions are not adopted): {code:#x}"
            );
        }
        Ok(())
    }
    fn commit(&mut self) -> Result<()> {
        let code = unsafe { FwpmTransactionCommit0(self.0.0) };
        ensure!(code == 0, "commit candidate WFP transaction: {code:#x}");
        Ok(())
    }
}

// Pointer-bearing fields refer only to owned heap allocations, which remain
// stable if the Filter itself moves. No descriptor storage escapes this value.
struct Filter {
    value: FWPM_FILTER0,
    _tag: Vec<u8>,
    _name: Vec<u16>,
    _sd: OwnedSd,
    _blob: Box<FWP_BYTE_BLOB>,
    _condition: Box<FWPM_FILTER_CONDITION0>,
    _weight: Box<u64>,
}
impl Filter {
    fn new(record: &SetupRecord, index: usize) -> Result<Self> {
        ensure!(index < 2, "invalid offline address family");
        let mut tag = serde_json::to_vec(&Tag::expected(
            record,
            if index == 0 { Kind::Ipv4 } else { Kind::Ipv6 },
        )?)?;
        let mut name = crate::util::wstr(if index == 0 {
            "Alysis candidate offline IPv4"
        } else {
            "Alysis candidate offline IPv6"
        });
        let sd = OwnedSd::from_sddl(&crate::wfp::sddl_sandbox_user(
            &record.account(AccountMode::Offline)?.sid,
        ))?;
        let mut blob = Box::new(FWP_BYTE_BLOB {
            size: sd.len,
            data: sd.ptr.0.cast(),
        });
        let mut condition = Box::new(FWPM_FILTER_CONDITION0 {
            fieldKey: FWPM_CONDITION_ALE_USER_ID,
            matchType: FWP_MATCH_EQUAL,
            conditionValue: FWP_CONDITION_VALUE0 {
                r#type: FWP_SECURITY_DESCRIPTOR_TYPE,
                Anonymous: FWP_CONDITION_VALUE0_0 { sd: &mut *blob },
            },
        });
        let mut weight = Box::new(BLOCK_WEIGHT);
        let value = FWPM_FILTER0 {
            filterKey: filter_keys(record)?[index],
            subLayerKey: OFFLINE_SUBLAYER,
            layerKey: if index == 0 {
                FWPM_LAYER_ALE_AUTH_CONNECT_V4
            } else {
                FWPM_LAYER_ALE_AUTH_CONNECT_V6
            },
            flags: FWPM_FILTER_FLAG_PERSISTENT,
            displayData: FWPM_DISPLAY_DATA0 {
                name: PWSTR(name.as_mut_ptr()),
                ..Default::default()
            },
            providerData: FWP_BYTE_BLOB {
                size: tag.len() as u32,
                data: tag.as_mut_ptr(),
            },
            weight: FWP_VALUE0 {
                r#type: FWP_UINT64,
                Anonymous: FWP_VALUE0_0 {
                    uint64: &mut *weight,
                },
            },
            action: FWPM_ACTION0 {
                r#type: FWP_ACTION_BLOCK,
                ..Default::default()
            },
            numFilterConditions: 1,
            filterCondition: &mut *condition,
            ..Default::default()
        };
        Ok(Self {
            value,
            _tag: tag,
            _name: name,
            _sd: sd,
            _blob: blob,
            _condition: condition,
            _weight: weight,
        })
    }
}

fn validate_security(sd: PSECURITY_DESCRIPTOR) -> Result<()> {
    ensure!(
        !sd.0.is_null() && unsafe { IsValidSecurityDescriptor(sd) }.as_bool(),
        "invalid candidate WFP object security"
    );
    let mut owner = PSID::default();
    let mut group = PSID::default();
    let mut defaulted = false.into();
    let mut present = false.into();
    let mut acl: *mut ACL = std::ptr::null_mut();
    let mut control = 0;
    let mut revision = 0;
    unsafe {
        GetSecurityDescriptorOwner(sd, &mut owner, &mut defaulted)?;
        GetSecurityDescriptorGroup(sd, &mut group, &mut defaulted)?;
        GetSecurityDescriptorControl(sd, &mut control, &mut revision)?;
        GetSecurityDescriptorDacl(sd, &mut present, &mut acl, &mut defaulted)?;
    }
    ensure!(
        crate::sid::psid_to_string(owner)? == "S-1-5-32-544"
            && crate::sid::psid_to_string(group)? == "S-1-5-32-544"
            && control & SE_DACL_PROTECTED.0 != 0
            && control & 0x108 == 0
            && present.as_bool()
            && !defaulted.as_bool()
            && !acl.is_null()
            && unsafe { IsValidAcl(acl) }.as_bool(),
        "candidate WFP security is not protected administrator ownership"
    );
    ensure!(
        unsafe { (*acl).AceCount } == 2,
        "candidate WFP DACL has unexpected grants"
    );
    let mut principals = BTreeSet::new();
    for index in 0..2 {
        let mut raw = std::ptr::null_mut();
        unsafe {
            GetAce(acl, index, &mut raw)?;
        }
        let header = unsafe { &*raw.cast::<ACE_HEADER>() };
        ensure!(
            header.AceType == 0 && header.AceFlags == 0 && header.AceSize >= 16,
            "candidate WFP DACL has unexpected ACE type/flags"
        );
        let bytes =
            unsafe { std::slice::from_raw_parts(raw.cast::<u8>(), header.AceSize as usize) };
        ensure!(
            u32::from_le_bytes(bytes[4..8].try_into().unwrap()) == ALL,
            "candidate WFP administrator access differs from exact policy"
        );
        let sid = PSID(unsafe { raw.cast::<u8>().add(8) }.cast());
        ensure!(
            principals.insert(crate::sid::psid_to_string(sid)?),
            "duplicate WFP security principal"
        );
    }
    ensure!(
        principals == BTreeSet::from(["S-1-5-18".into(), "S-1-5-32-544".into()]),
        "candidate WFP object grants access outside SYSTEM/Administrators"
    );
    Ok(())
}

fn security(engine: HANDLE, key: &GUID, sublayer: bool) -> Result<()> {
    let mut sd = PSECURITY_DESCRIPTOR::default();
    let information =
        (OWNER_SECURITY_INFORMATION | GROUP_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION).0;
    let code = unsafe {
        if sublayer {
            FwpmSubLayerGetSecurityInfoByKey0(
                engine,
                Some(key),
                information,
                std::ptr::null_mut(),
                std::ptr::null_mut(),
                std::ptr::null_mut(),
                std::ptr::null_mut(),
                &mut sd,
            )
        } else {
            FwpmFilterGetSecurityInfoByKey0(
                engine,
                Some(key),
                information,
                std::ptr::null_mut(),
                std::ptr::null_mut(),
                std::ptr::null_mut(),
                std::ptr::null_mut(),
                &mut sd,
            )
        }
    };
    // BFE allocations must use FwpmFreeMemory0, not LocalFree/OwnedSd.
    let _buffer = Buffer(sd.0.cast::<u8>());
    ensure!(code == 0, "read candidate WFP object security: {code:#x}");
    validate_security(sd)
}

pub(super) fn observe(engine: HANDLE, record: &SetupRecord) -> Result<Observed> {
    let mut result = Observed::default();
    let mut raw = std::ptr::null_mut();
    let code = unsafe { FwpmSubLayerGetByKey0(engine, &OFFLINE_SUBLAYER, &mut raw) };
    let layer = Buffer(raw);
    if code != SUBLAYER_NOT_FOUND {
        ensure!(
            code == 0 && !layer.0.is_null(),
            "inspect candidate sublayer: {code:#x}"
        );
        let layer = unsafe { &*layer.0 };
        ensure!(
            layer.subLayerKey == OFFLINE_SUBLAYER,
            "candidate sublayer key mismatch"
        );
        validate_tag(&layer.providerData, &Tag::expected(record, Kind::Sublayer)?)?;
        security(engine, &OFFLINE_SUBLAYER, true)?;
        result.sublayer = true;
        result.complete = layer.flags == FWPM_SUBLAYER_FLAG_PERSISTENT
            && layer.providerKey.is_null()
            && layer.weight == SUBLAYER_WEIGHT;
    }
    let keys = filter_keys(record)?;
    for (index, key) in keys.iter().enumerate() {
        let mut raw = std::ptr::null_mut();
        let code = unsafe { FwpmFilterGetByKey0(engine, key, &mut raw) };
        let filter = Buffer(raw);
        if code == FILTER_NOT_FOUND {
            result.complete = false;
            continue;
        }
        ensure!(
            code == 0 && !filter.0.is_null(),
            "inspect candidate filter: {code:#x}"
        );
        let filter = unsafe { &*filter.0 };
        ensure!(
            filter.filterKey == *key && filter.subLayerKey == OFFLINE_SUBLAYER && result.sublayer,
            "candidate filter key collides with another namespace"
        );
        validate_tag(
            &filter.providerData,
            &Tag::expected(record, if index == 0 { Kind::Ipv4 } else { Kind::Ipv6 })?,
        )?;
        security(engine, key, false)?;
        result.filters[index] = true;
        result.complete &= validate_filter(filter, record, index).is_ok();
    }
    let expected = keys
        .into_iter()
        .zip(result.filters)
        .filter_map(|(key, exists)| exists.then_some(key.to_u128()))
        .collect();
    ensure!(
        namespace_keys(engine)? == expected,
        "candidate WFP namespace has unowned or unexpected rules"
    );
    Ok(result)
}

#[cfg(test)]
#[path = "account_wfp_update_tests.rs"]
mod tests;
