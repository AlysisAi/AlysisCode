//! Copy and inspect this process's RPC default server requirements after the
//! existing LSA call. Never opens the service, changes an ACL, or retries RPC.
use anyhow::{Context, Result, ensure};
use serde::{Deserialize, Serialize};
use windows::Win32::{
    Security::*,
    System::Rpc::{I_RpcFree, I_RpcGetDefaultSD},
};

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Observation {
    query_status: i32,
    failure_output_nonnull: bool,
    copy_returned: bool,
    free_called: bool,
    shape: Option<Shape>,
}

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Shape {
    control: u16,
    owner: Option<String>,
    group: Option<String>,
    dacl: Option<Vec<Ace>>,
    sacl: Option<Vec<Ace>>,
}
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Ace {
    kind: u8,
    flags: u8,
    mask: u32,
    sid: String,
}

struct Copy(*mut core::ffi::c_void);
impl Drop for Copy {
    fn drop(&mut self) {
        unsafe { I_RpcFree(self.0) };
    }
}

impl Observation {
    pub(super) fn require(&self) -> Result<()> {
        ensure!(
            self.copy_returned == self.free_called
                && (!self.copy_returned || self.query_status == 0)
                && (!self.failure_output_nonnull
                    || (self.query_status != 0 && !self.copy_returned))
                && (self.shape.is_none() || (self.query_status == 0 && self.copy_returned)),
            "RPC descriptor copy/cleanup shape differs"
        );
        if let Some(shape) = &self.shape {
            ensure!(
                [&shape.owner, &shape.group]
                    .into_iter()
                    .flatten()
                    .all(|sid| sid.len() <= 192),
                "RPC descriptor SID exceeds bound"
            );
            ensure!(
                shape.control & 0x8000 != 0,
                "RPC descriptor must be relative"
            );
            for acl in [&shape.dacl, &shape.sacl].into_iter().flatten() {
                ensure!(
                    acl.len() <= 32
                        && acl
                            .iter()
                            .all(|a| matches!(a.kind, 0 | 1 | 2 | 17) && a.sid.len() <= 192),
                    "RPC descriptor entries exceed fixed shape"
                );
            }
        }
        Ok(())
    }
    pub(super) fn observe() -> Self {
        let mut pointer = std::ptr::null_mut();
        let query_status = unsafe { I_RpcGetDefaultSD(&mut pointer) }.0;
        Self::from_result(query_status, pointer)
    }
    fn from_result(query_status: i32, pointer: *mut core::ffi::c_void) -> Self {
        // The native copy helper may free a failed conversion's allocation
        // without clearing its out parameter. A failure output is not ours:
        // never dereference or free it, even when non-null.
        let failure_output_nonnull = query_status != 0 && !pointer.is_null();
        let copy_returned = query_status == 0 && !pointer.is_null();
        let shape = if copy_returned {
            let copy = Copy(pointer);
            let value = read_copy(&copy).ok();
            drop(copy);
            value
        } else {
            None
        };
        // I_RpcFree returns void: record the call, never claim a checked result.
        Self {
            query_status,
            failure_output_nonnull,
            copy_returned,
            free_called: copy_returned,
            shape,
        }
    }
}

fn read_copy(copy: &Copy) -> Result<Shape> {
    let descriptor = PSECURITY_DESCRIPTOR(copy.0);
    ensure!(
        unsafe { IsValidSecurityDescriptor(descriptor) }.as_bool(),
        "invalid copied RPC descriptor"
    );
    let mut control = 0u16;
    let mut revision = 0;
    unsafe { GetSecurityDescriptorControl(descriptor, &mut control, &mut revision)? };
    ensure!(
        control & 0x8000 != 0 && revision == 1,
        "RPC copy is not self-relative"
    );
    let length = unsafe { GetSecurityDescriptorLength(descriptor) } as usize;
    ensure!((20..=65536).contains(&length), "RPC descriptor size bound");
    parse(unsafe { std::slice::from_raw_parts(copy.0.cast::<u8>(), length) })
}

fn parse(bytes: &[u8]) -> Result<Shape> {
    let b = Bytes(bytes);
    ensure!(
        (20..=65536).contains(&bytes.len()) && bytes[0] == 1,
        "RPC descriptor header bound"
    );
    let control = b.u16(2)?;
    ensure!(control & 0x8000 != 0, "RPC descriptor not relative");
    let sid = |field| -> Result<Option<String>> {
        let offset = b.u32(field)? as usize;
        if offset == 0 {
            Ok(None)
        } else {
            ensure!(offset >= 20 && offset % 4 == 0, "RPC SID offset differs");
            Ok(Some(b.sid(offset, bytes.len())?.0))
        }
    };
    Ok(Shape {
        control,
        owner: sid(4)?,
        group: sid(8)?,
        dacl: b.acl(b.u32(16)? as usize, control & 4 != 0)?,
        sacl: b.acl(b.u32(12)? as usize, control & 16 != 0)?,
    })
}

struct Bytes<'a>(&'a [u8]);
impl Bytes<'_> {
    fn slice(&self, offset: usize, length: usize) -> Result<&[u8]> {
        self.0
            .get(offset..offset.checked_add(length).context("RPC field overflow")?)
            .context("RPC field truncated")
    }
    fn u16(&self, at: usize) -> Result<u16> {
        Ok(u16::from_le_bytes(self.slice(at, 2)?.try_into()?))
    }
    fn u32(&self, at: usize) -> Result<u32> {
        Ok(u32::from_le_bytes(self.slice(at, 4)?.try_into()?))
    }
    fn sid(&self, at: usize, end: usize) -> Result<(String, usize)> {
        let h = self.slice(at, 8)?;
        ensure!(h[0] == 1 && h[1] <= 15, "RPC SID shape differs");
        let length = 8 + 4 * h[1] as usize;
        ensure!(
            at.checked_add(length).is_some_and(|n| n <= end),
            "RPC SID exceeds component"
        );
        let authority = h[2..8].iter().fold(0u64, |a, byte| (a << 8) | *byte as u64);
        let mut value = format!("S-1-{authority}");
        for index in 0..h[1] as usize {
            value.push_str(&format!("-{}", self.u32(at + 8 + index * 4)?));
        }
        Ok((value, length))
    }
    fn acl(&self, at: usize, present: bool) -> Result<Option<Vec<Ace>>> {
        if !present {
            ensure!(at == 0, "absent RPC ACL carries data");
            return Ok(None);
        }
        // Null versus absent remains distinguishable through the control bits.
        if at == 0 {
            return Ok(None);
        }
        ensure!(at >= 20 && at % 4 == 0, "RPC ACL offset differs");
        let header = self.slice(at, 8)?;
        ensure!(matches!(header[0], 2 | 4), "RPC ACL revision differs");
        let length = self.u16(at + 2)? as usize;
        let count = self.u16(at + 4)? as usize;
        ensure!(length >= 8 && count <= 32, "RPC ACL size/count bound");
        self.slice(at, length)?;
        let end = at + length;
        let mut cursor = at + 8;
        let mut entries = Vec::new();
        for _ in 0..count {
            ensure!(cursor + 8 <= end, "RPC ACE header exceeds ACL");
            let header = self.slice(cursor, 8)?;
            let size = self.u16(cursor + 2)? as usize;
            ensure!(
                size >= 16
                    && size % 4 == 0
                    && cursor + size <= end
                    && matches!(header[0], 0 | 1 | 2 | 17),
                "RPC ACE size/type unsupported"
            );
            let (sid, sid_size) = self.sid(cursor + 8, cursor + size)?;
            ensure!(sid_size + 8 == size, "RPC ACE has unmodeled payload");
            entries.push(Ace {
                kind: header[0],
                flags: header[1],
                mask: self.u32(cursor + 4)?,
                sid,
            });
            cursor += size;
        }
        ensure!(cursor <= end, "RPC ACL overflow");
        Ok(Some(entries))
    }
}

#[test]
fn station_rpc_server_descriptor_parses_owner_group_inheritance_and_label() {
    let sd =
        crate::util::OwnedSd::from_sddl("O:SYG:SYD:(A;OICI;GR;;;AN)(A;ID;GR;;;SY)S:(ML;;NW;;;LW)")
            .unwrap();
    let len = unsafe { GetSecurityDescriptorLength(sd.ptr) } as usize;
    let bytes = unsafe { std::slice::from_raw_parts(sd.ptr.0.cast::<u8>(), len) };
    let shape = parse(bytes).unwrap();
    assert_eq!(shape.owner.as_deref(), Some("S-1-5-18"));
    assert_eq!(shape.group.as_deref(), Some("S-1-5-18"));
    let acl = shape.dacl.unwrap();
    assert_eq!(
        (acl[0].flags, acl[0].mask, acl[0].sid.as_str()),
        (3, 0x80000000, "S-1-5-7")
    );
    assert_eq!(acl[1].flags, 16);
    assert_eq!(shape.sacl.unwrap()[0].sid, "S-1-16-4096");
    assert!(parse(&bytes[..19]).is_err());
    let mut bad = bytes.to_vec();
    bad[16..20].copy_from_slice(&u32::MAX.to_le_bytes());
    assert!(parse(&bad).is_err());
    let mut bad = bytes.to_vec();
    let dacl = u32::from_le_bytes(bad[16..20].try_into().unwrap()) as usize;
    bad[dacl + 4..dacl + 6].copy_from_slice(&33u16.to_le_bytes());
    assert!(parse(&bad).is_err());
    let mut bad = bytes.to_vec();
    bad[dacl + 8] = 9;
    assert!(parse(&bad).is_err());
}

#[test]
fn station_rpc_server_descriptor_cleanup_and_unknown_shapes_cannot_claim_observed_data() {
    let unknown = Observation {
        query_status: 5,
        failure_output_nonnull: false,
        copy_returned: false,
        free_called: false,
        shape: None,
    };
    unknown.require().unwrap();
    let mut wrong = unknown.clone();
    wrong.copy_returned = true;
    assert!(wrong.require().is_err());
    let mut value = serde_json::to_value(unknown).unwrap();
    value["raw_pointer"] = 1.into();
    assert!(serde_json::from_value::<Observation>(value).is_err());
}

#[test]
fn station_rpc_server_descriptor_failed_query_never_adopts_or_frees_stale_output() {
    // A failed native conversion can leave an already-freed pointer behind.
    // This sentinel must not reach either the descriptor reader or allocator.
    let failed = Observation::from_result(14, std::ptr::dangling_mut::<core::ffi::c_void>());
    failed.require().unwrap();
    assert!(failed.failure_output_nonnull);
    assert!(!failed.copy_returned && !failed.free_called && failed.shape.is_none());
    let absent = Observation::from_result(0, std::ptr::null_mut());
    absent.require().unwrap();
    assert!(!absent.copy_returned && absent.shape.is_none());
}

#[test]
fn station_rpc_server_descriptor_models_system_inheritance_without_object_changes() {
    use windows::Win32::{
        Foundation::{CloseHandle, HANDLE},
        System::Threading::{GetCurrentProcess, OpenProcessToken},
    };
    // Memory-only comparison of RPC's descriptor construction. No native
    // object receives either descriptor and no token permission is changed.
    let input = crate::util::OwnedSd::from_sddl("D:(A;;GR;;;AN)").unwrap();
    let mapping = GENERIC_MAPPING {
        GenericRead: 0x20001,
        GenericWrite: 0x20001,
        GenericExecute: 0x20000,
        GenericAll: 0x1f0001,
    };
    for (extra, expected) in [("", false), ("(A;OIIO;GR;;;SY)", true)] {
        let parent =
            crate::util::OwnedSd::from_sddl(&format!("O:SYG:SYD:P(A;;0xf000f;;;SY){extra}"))
                .unwrap();
        let mut token = HANDLE::default();
        unsafe { OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut token).unwrap() };
        let mut result = PSECURITY_DESCRIPTOR::default();
        let status = unsafe {
            CreatePrivateObjectSecurityEx(
                Some(parent.ptr),
                Some(input.ptr),
                &mut result,
                None,
                false,
                SEF_DACL_AUTO_INHERIT,
                Some(token),
                &mapping,
            )
        };
        let close = unsafe { CloseHandle(token) };
        status.unwrap();
        let length = unsafe { GetSecurityDescriptorLength(result) } as usize;
        let shape = if (20..=65536).contains(&length) {
            parse(unsafe { std::slice::from_raw_parts(result.0.cast::<u8>(), length) })
        } else {
            Err(anyhow::anyhow!("model descriptor exceeds bound"))
        };
        unsafe { DestroyPrivateObjectSecurity(&result).unwrap() };
        close.unwrap();
        let acl = shape.unwrap().dacl.unwrap();
        assert_eq!(
            acl.iter()
                .any(|a| a.kind == 0 && a.sid == "S-1-5-18" && a.mask & 0x20001 == 0x20001),
            expected
        );
        assert!(
            acl.iter()
                .any(|a| a.kind == 0 && a.sid == "S-1-5-7" && a.mask == 0x20001)
        );
        if expected {
            assert_eq!(acl.iter().find(|a| a.sid == "S-1-5-18").unwrap().flags, 16);
        }
    }
}
