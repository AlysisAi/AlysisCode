//! Exact, read-only candidate offline fence verification. Baseline WFP status
//! deliberately accepts tagged approximations; activation must inspect actual
//! actions, layers, conditions, persistence and installation-bound identities.
//! Private installation/repair primitives live in the update child module.
//! No CLI/default-backend route or BFE access delegation is provided.

use crate::{
    account_install::{AccountBinding, AccountMode, OFFLINE_SUBLAYER, SetupRecord},
    util::OwnedSd,
};
use anyhow::{Result, ensure};
use serde::{Deserialize, Serialize};
use std::{collections::BTreeSet, ffi::c_void};
use windows::{
    Win32::{
        Foundation::HANDLE,
        NetworkManagement::WindowsFilteringPlatform::*,
        Security::{
            GetSecurityDescriptorControl, GetSecurityDescriptorLength, IsValidSecurityDescriptor,
            PSECURITY_DESCRIPTOR, SE_SELF_RELATIVE,
        },
    },
    core::{GUID, PCWSTR},
};

const SUBLAYER_WEIGHT: u16 = 0x7000;
const BLOCK_WEIGHT: u64 = 0x0f40_0000_0000_0000;

#[path = "account_wfp_update.rs"]
mod update;
pub(in crate::account_install) use update::{prepare, refuse_removal, require_complete};

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
enum Kind {
    Sublayer,
    Ipv4,
    Ipv6,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Tag {
    version: u32,
    tool: String,
    binding: AccountBinding,
    online_sid: String,
    kind: Kind,
}

impl Tag {
    fn expected(record: &SetupRecord, kind: Kind) -> Result<Self> {
        Ok(Self {
            version: 1,
            tool: "alysis-account-sandbox".into(),
            binding: record.binding(AccountMode::Offline)?,
            online_sid: record.account(AccountMode::Online)?.sid.clone(),
            kind,
        })
    }
}

// The random installation ID already exists durably before policy work. Its
// two domain-separated keys are immutable and recoverable from that identity;
// a future writer must still refuse unowned existing sublayer/filter objects.
fn filter_keys(record: &SetupRecord) -> Result<[GUID; 2]> {
    crate::account_install::validate_random_id(&record.installation_id)?;
    let installation = u128::from_str_radix(&record.installation_id, 16)?;
    let keys = [
        GUID::from_u128(installation ^ 0x34a4d780_7758_4e6b_895c_68c226b980f1),
        GUID::from_u128(installation ^ 0x96e478b6_5666_4c4f_bf69_6df3a25ec723),
    ];
    ensure!(
        keys.iter()
            .all(|key| *key != GUID::zeroed() && *key != OFFLINE_SUBLAYER),
        "invalid candidate filter key"
    );
    Ok(keys)
}

fn validate_tag(blob: &FWP_BYTE_BLOB, expected: &Tag) -> Result<()> {
    ensure!(
        (1..=16 * 1024).contains(&blob.size) && !blob.data.is_null(),
        "candidate WFP ownership tag absent/oversized"
    );
    let data = unsafe { std::slice::from_raw_parts(blob.data, blob.size as usize) };
    let observed: Tag = serde_json::from_slice(data)?;
    ensure!(
        observed == *expected,
        "candidate WFP object belongs to a different installation/account"
    );
    Ok(())
}

fn validate_descriptor(blob: &FWP_BYTE_BLOB, sid: &str) -> Result<()> {
    ensure!(
        (20..=4096).contains(&blob.size) && !blob.data.is_null(),
        "candidate WFP user descriptor absent/oversized"
    );
    let descriptor = PSECURITY_DESCRIPTOR(blob.data.cast());
    ensure!(
        unsafe { IsValidSecurityDescriptor(descriptor) }.as_bool(),
        "invalid candidate WFP user descriptor"
    );
    let mut control = 0;
    let mut revision = 0;
    unsafe {
        GetSecurityDescriptorControl(descriptor, &mut control, &mut revision)?;
    }
    ensure!(
        control & SE_SELF_RELATIVE.0 != 0
            && unsafe { GetSecurityDescriptorLength(descriptor) } == blob.size,
        "candidate WFP descriptor is not the exact self-relative object"
    );
    let expected = OwnedSd::from_sddl(&crate::wfp::sddl_sandbox_user(sid))?;
    let current = unsafe { std::slice::from_raw_parts(blob.data, blob.size as usize) };
    let expected =
        unsafe { std::slice::from_raw_parts(expected.ptr.0.cast::<u8>(), expected.len as usize) };
    ensure!(
        current == expected,
        "candidate WFP user condition is not the exact offline SID match"
    );
    Ok(())
}

fn validate_filter(filter: &FWPM_FILTER0, record: &SetupRecord, index: usize) -> Result<()> {
    let keys = filter_keys(record)?;
    let (layer, kind) = match index {
        0 => (FWPM_LAYER_ALE_AUTH_CONNECT_V4, Kind::Ipv4),
        1 => (FWPM_LAYER_ALE_AUTH_CONNECT_V6, Kind::Ipv6),
        _ => anyhow::bail!("invalid candidate address family"),
    };
    ensure!(
        filter.filterKey == keys[index]
            && filter.subLayerKey == OFFLINE_SUBLAYER
            && filter.layerKey == layer,
        "candidate WFP filter identity/layer mismatch"
    );
    ensure!(
        filter.flags == FWPM_FILTER_FLAG_PERSISTENT
            && filter.providerKey.is_null()
            && filter.action.r#type == FWP_ACTION_BLOCK
            && unsafe { filter.Anonymous.rawContext } == 0,
        "candidate WFP filter is disabled, nonpersistent, contextual or not a hard block"
    );
    ensure!(
        filter.weight.r#type == FWP_UINT64
            && !unsafe { filter.weight.Anonymous.uint64 }.is_null()
            && unsafe { *filter.weight.Anonymous.uint64 } == BLOCK_WEIGHT,
        "candidate WFP block weight mismatch"
    );
    validate_tag(&filter.providerData, &Tag::expected(record, kind)?)?;
    ensure!(
        filter.numFilterConditions == 1 && !filter.filterCondition.is_null(),
        "candidate offline block must be unconditional except for its user SID"
    );
    let condition = unsafe { &*filter.filterCondition };
    ensure!(
        condition.fieldKey == FWPM_CONDITION_ALE_USER_ID
            && condition.matchType == FWP_MATCH_EQUAL
            && condition.conditionValue.r#type == FWP_SECURITY_DESCRIPTOR_TYPE,
        "candidate offline block has a different condition"
    );
    let descriptor = unsafe { condition.conditionValue.Anonymous.sd };
    ensure!(
        !descriptor.is_null(),
        "candidate WFP user descriptor missing"
    );
    validate_descriptor(
        unsafe { &*descriptor },
        &record.account(AccountMode::Offline)?.sid,
    )
}

struct Engine(HANDLE);
impl Drop for Engine {
    fn drop(&mut self) {
        unsafe {
            let _ = FwpmTransactionAbort0(self.0);
            let _ = FwpmEngineClose0(self.0);
        }
    }
}
struct Buffer<T>(*mut T);
impl<T> Drop for Buffer<T> {
    fn drop(&mut self) {
        if !self.0.is_null() {
            let mut pointer = self.0.cast::<c_void>();
            unsafe {
                FwpmFreeMemory0(&mut pointer);
            }
        }
    }
}
struct Enumeration {
    engine: HANDLE,
    handle: HANDLE,
}
impl Drop for Enumeration {
    fn drop(&mut self) {
        unsafe {
            let _ = FwpmFilterDestroyEnumHandle0(self.engine, self.handle);
        }
    }
}

pub(super) fn verify(record: &SetupRecord) -> Result<()> {
    let mut engine = HANDLE::default();
    let code = unsafe { FwpmEngineOpen0(PCWSTR::null(), 0xffff_ffff, None, None, &mut engine) };
    ensure!(code == 0, "candidate WFP inspection unavailable: {code:#x}");
    let engine = Engine(engine);
    let code = unsafe { FwpmTransactionBegin0(engine.0, FWPM_TXN_READ_ONLY) };
    ensure!(
        code == 0,
        "cannot obtain read-only candidate WFP snapshot: {code:#x}"
    );
    ensure!(
        update::observe(engine.0, record)?.complete,
        "candidate offline WFP policy is absent or incomplete"
    );
    Ok(())
}

fn namespace_keys(engine: HANDLE) -> Result<BTreeSet<u128>> {
    // Also refuse unexpected rules in this owned namespace. Tag-only filtering
    // could silently skip an untagged permit or a filter for the online user.
    let mut observed = BTreeSet::new();
    {
        // NULL template observes every layer, including unexpected candidate
        // rules outside ALE_AUTH_CONNECT. WFP returns only readable objects;
        // the installer must retain its administrator-only namespace policy.
        let mut handle = HANDLE::default();
        let code = unsafe { FwpmFilterCreateEnumHandle0(engine, None, &mut handle) };
        ensure!(
            code == 0,
            "cannot enumerate candidate WFP snapshot: {code:#x}"
        );
        let enumeration = Enumeration { engine, handle };
        let mut total = 0;
        loop {
            let mut entries = std::ptr::null_mut();
            let mut count = 0;
            let code = unsafe {
                FwpmFilterEnum0(engine, enumeration.handle, 128, &mut entries, &mut count)
            };
            let entries = Buffer(entries);
            total += count;
            ensure!(
                code == 0
                    && count <= 128
                    && total <= 16_384
                    && (count == 0 || !entries.0.is_null()),
                "candidate WFP enumeration failed or exceeded bounds: {code:#x}"
            );
            if count == 0 {
                break;
            }
            for pointer in unsafe { std::slice::from_raw_parts(entries.0, count as usize) } {
                ensure!(!pointer.is_null(), "null candidate WFP enumeration entry");
                let filter = unsafe { &**pointer };
                if filter.subLayerKey == OFFLINE_SUBLAYER {
                    ensure!(
                        observed.insert(filter.filterKey.to_u128()),
                        "duplicate candidate WFP filter identity"
                    );
                }
            }
        }
    }
    Ok(observed)
}

#[cfg(test)]
mod tests {
    use super::*;
    struct Fixture {
        filter: FWPM_FILTER0,
        _tag: Vec<u8>,
        _sd: OwnedSd,
        blob: Box<FWP_BYTE_BLOB>,
        condition: Box<FWPM_FILTER_CONDITION0>,
        weight: Box<u64>,
    }
    impl Fixture {
        fn new(record: &SetupRecord, index: usize) -> Self {
            let mut tag = serde_json::to_vec(
                &Tag::expected(record, if index == 0 { Kind::Ipv4 } else { Kind::Ipv6 }).unwrap(),
            )
            .unwrap();
            let sd = OwnedSd::from_sddl(&crate::wfp::sddl_sandbox_user(
                &record.offline.as_ref().unwrap().sid,
            ))
            .unwrap();
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
            let filter = FWPM_FILTER0 {
                filterKey: filter_keys(record).unwrap()[index],
                flags: FWPM_FILTER_FLAG_PERSISTENT,
                subLayerKey: OFFLINE_SUBLAYER,
                layerKey: if index == 0 {
                    FWPM_LAYER_ALE_AUTH_CONNECT_V4
                } else {
                    FWPM_LAYER_ALE_AUTH_CONNECT_V6
                },
                weight: FWP_VALUE0 {
                    r#type: FWP_UINT64,
                    Anonymous: FWP_VALUE0_0 {
                        uint64: &mut *weight,
                    },
                },
                providerData: FWP_BYTE_BLOB {
                    size: tag.len() as u32,
                    data: tag.as_mut_ptr(),
                },
                action: FWPM_ACTION0 {
                    r#type: FWP_ACTION_BLOCK,
                    ..Default::default()
                },
                numFilterConditions: 1,
                filterCondition: &mut *condition,
                ..Default::default()
            };
            Self {
                filter,
                _tag: tag,
                _sd: sd,
                blob,
                condition,
                weight,
            }
        }
    }
    #[test]
    fn candidate_activation_wfp_requires_actual_action_layer_flags_and_exact_condition() {
        let record = crate::account_install::synthetic_ready();
        for index in 0..2 {
            validate_filter(&Fixture::new(&record, index).filter, &record, index).unwrap();
        }
        for mutation in 0..10 {
            let mut fixture = Fixture::new(&record, 0);
            match mutation {
                0 => fixture.filter.action.r#type = FWP_ACTION_PERMIT,
                1 => fixture.filter.flags = FWPM_FILTER_FLAG_DISABLED,
                2 => fixture.filter.layerKey = FWPM_LAYER_ALE_AUTH_CONNECT_V6,
                3 => fixture.filter.numFilterConditions = 2,
                4 => fixture.condition.matchType = FWP_MATCH_NOT_EQUAL,
                5 => fixture.condition.fieldKey = FWPM_CONDITION_IP_REMOTE_PORT,
                6 => *fixture.weight = 0,
                7 => fixture.filter.filterKey = GUID::zeroed(),
                8 => fixture.filter.subLayerKey = crate::wfp::DEFAULT_SUBLAYER_GUID,
                _ => fixture.blob.size = 0,
            }
            assert!(
                validate_filter(&fixture.filter, &record, 0).is_err(),
                "accepted mutation {mutation}"
            );
        }
    }
    #[test]
    fn candidate_activation_wfp_rejects_online_or_broad_user_match_despite_correct_tag() {
        let record = crate::account_install::synthetic_ready();
        for sid in [
            &record.online.as_ref().unwrap().sid,
            "S-1-1-0",
            "S-1-5-32-545",
        ] {
            let mut fixture = Fixture::new(&record, 0);
            let sd = OwnedSd::from_sddl(&crate::wfp::sddl_sandbox_user(sid)).unwrap();
            fixture.blob.data = sd.ptr.0.cast();
            fixture.blob.size = sd.len;
            assert!(validate_filter(&fixture.filter, &record, 0).is_err());
        }
        let mut foreign = record.clone();
        foreign.storage = None;
        foreign.installation_id = "1123456789abcdef0123456789abcdef".into();
        let fixture = Fixture::new(&record, 0);
        assert!(validate_filter(&fixture.filter, &foreign, 0).is_err());
        assert_ne!(
            filter_keys(&record).unwrap(),
            filter_keys(&foreign).unwrap()
        );
    }
}
