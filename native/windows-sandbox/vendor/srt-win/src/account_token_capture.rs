//! Fixed disposable six-object capture comparison. The private pipe prefix is
//! opened only, never created. This is not OS-constructor or release authority.
use super::*;
use serde::{Deserialize, Serialize};
use std::mem::size_of_val;
use windows::Wdk::Storage::FileSystem::NtQuerySecurityObject;
use windows::Win32::Foundation::{CloseHandle, OBJ_DONT_REPARSE, OBJECT_ATTRIBUTE_FLAGS};
use windows::Win32::System::IO::IO_STATUS_BLOCK;

#[link(name = "ntdll")]
unsafe extern "system" {
    fn NtOpenFile(
        handle: *mut HANDLE,
        access: u32,
        attrs: *const OBJECT_ATTRIBUTES,
        io: *mut IO_STATUS_BLOCK,
        share: u32,
        options: u32,
    ) -> NTSTATUS;
    fn NtOpenSymbolicLinkObject(
        handle: *mut HANDLE,
        access: u32,
        attrs: *const OBJECT_ATTRIBUTES,
    ) -> NTSTATUS;
    fn NtQuerySymbolicLinkObject(
        handle: HANDLE,
        value: *mut UNICODE_STRING,
        returned: *mut u32,
    ) -> NTSTATUS;
    fn NtQueryObject(
        handle: HANDLE,
        class: u32,
        data: *mut core::ffi::c_void,
        size: u32,
        returned: *mut u32,
    ) -> NTSTATUS;
}
const OBJECTS: [(&str, u32); 6] = [
    ("package", 0xf000f),
    ("private_rpc", 0x20003),
    ("Local", 0x20001),
    ("Global", 0x20001),
    ("Session", 0x20001),
    ("pipe_prefix", 0x120080), // SYNCHRONIZE, READ_CONTROL, FILE_READ_ATTRIBUTES
];
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Proof {
    diagnostic_only: bool,
    objects: Vec<(String, u32)>,
    identities_and_security_unchanged: bool,
    source_handles_closed: bool,
}
impl Proof {
    pub(crate) fn require(&self) -> Result<()> {
        ensure!(
            self.diagnostic_only
                && self.identities_and_security_unchanged
                && self.source_handles_closed,
            "capture subset proof incomplete"
        );
        ensure!(
            self.objects == OBJECTS.map(|(name, access)| (name.into(), access)),
            "capture subset objects/rights differ"
        );
        Ok(())
    }
}
fn text_extent(offset: usize, bytes: usize, returned: usize) -> Result<std::ops::Range<usize>> {
    let end = offset
        .checked_add(bytes)
        .context("capture object text overflow")?;
    ensure!(
        offset >= size_of::<UNICODE_STRING>()
            && offset % 2 == 0
            && bytes > 0
            && bytes % 2 == 0
            && bytes <= 4096
            && end <= returned,
        "capture object text outside native response"
    );
    Ok(offset..end)
}
fn text(handle: HANDLE, class: u32) -> Result<String> {
    ensure!(matches!(class, 1 | 2), "capture query class differs");
    let mut words = [0u64; 1024];
    let mut returned = 0;
    unsafe {
        NtQueryObject(
            handle,
            class,
            words.as_mut_ptr().cast(),
            size_of_val(&words) as u32,
            &mut returned,
        )
        .ok()?
    };
    ensure!(
        (size_of::<UNICODE_STRING>()..=size_of_val(&words)).contains(&(returned as usize)),
        "capture object response extent"
    );
    let value = unsafe { &*words.as_ptr().cast::<UNICODE_STRING>() };
    let base = words.as_ptr() as usize;
    let offset = (value.Buffer.0 as usize)
        .checked_sub(base)
        .context("capture text precedes response")?;
    let range = text_extent(offset, value.Length as usize, returned as usize)?;
    let units =
        unsafe { std::slice::from_raw_parts((base + range.start) as *const u16, range.len() / 2) };
    let text = String::from_utf16(units)?;
    ensure!(
        !text.chars().any(char::is_control),
        "capture object text contains controls"
    );
    Ok(text)
}
fn security(handle: HANDLE) -> Result<Vec<u8>> {
    let mut words = [0u64; 1024];
    let mut returned = 0;
    unsafe {
        NtQuerySecurityObject(
            handle,
            0x17,
            Some(PSECURITY_DESCRIPTOR(words.as_mut_ptr().cast())),
            size_of_val(&words) as u32,
            &mut returned,
        )
        .ok()?
    };
    ensure!(
        (20..=size_of_val(&words)).contains(&(returned as usize)),
        "capture security response extent"
    );
    Ok(unsafe { std::slice::from_raw_parts(words.as_ptr().cast(), returned as usize) }.to_vec())
}
fn link_target(handle: HANDLE) -> Result<String> {
    let mut buffer = [0u16; 2048];
    let mut value = UNICODE_STRING {
        Length: 0,
        MaximumLength: size_of_val(&buffer) as u16,
        Buffer: PWSTR(buffer.as_mut_ptr()),
    };
    let mut returned = 0;
    unsafe { NtQuerySymbolicLinkObject(handle, &mut value, &mut returned).ok()? };
    let length = value.Length as usize;
    ensure!(
        value.Buffer.0 == buffer.as_mut_ptr()
            && value.MaximumLength as usize == size_of_val(&buffer)
            && length > 0
            && length % 2 == 0
            && length <= size_of_val(&buffer)
            && (returned as usize == length
                || (returned as usize == length + 2
                    && length + 2 <= size_of_val(&buffer)
                    && buffer[length / 2] == 0)),
        "capture link response extent"
    );
    Ok(String::from_utf16(&buffer[..length / 2])?)
}
struct Pin {
    handle: OwnedHandle,
    name: String,
    kind: &'static str,
    security: Vec<u8>,
    target: Option<String>,
}
impl Pin {
    fn open_pipe(root_name: &str, stage: &mut String) -> Result<Self> {
        // Exact hosted constructor format is \\Device\\NamedPipe%ws, where
        // %ws is the original private namespace name used by Local/Session.
        let path = format!("\\Device\\NamedPipe{root_name}");
        let (mut wide, length) = crate::util::counted_utf16(&path);
        let name = UNICODE_STRING {
            Length: length,
            MaximumLength: length,
            Buffer: PWSTR(wide.as_mut_ptr()),
        };
        let attrs = OBJECT_ATTRIBUTES {
            Length: size_of::<OBJECT_ATTRIBUTES>() as u32,
            ObjectName: &name,
            Attributes: OBJECT_ATTRIBUTE_FLAGS(OBJ_CASE_INSENSITIVE.0 | OBJ_DONT_REPARSE.0),
            ..Default::default()
        };
        let mut raw = HANDLE::default();
        let mut io = IO_STATUS_BLOCK::default();
        *stage = "captured_namespace_pipe_open".into();
        // NtOpenFile cannot create a missing prefix. Directory + synchronous
        // nonalert I/O forbids an outstanding request referencing this stack.
        let status = unsafe { NtOpenFile(&mut raw, OBJECTS[5].1, &attrs, &mut io, 3, 0x21) };
        status.ok()?;
        let handle = OwnedHandle(raw);
        ensure!(
            status.0 == 0 && unsafe { io.Anonymous.Status.0 } == 0 && io.Information == 1,
            "private pipe prefix was not synchronously opened"
        );
        *stage = "captured_namespace_pipe_identity".into();
        let pin = Self {
            security: security(raw)?,
            handle,
            name: path,
            kind: "File",
            target: None,
        };
        pin.verify()?;
        Ok(pin)
    }
    fn open(
        name: &str,
        root: HANDLE,
        access: u32,
        full_name: String,
        target: Option<String>,
    ) -> Result<Self> {
        let (mut wide, length) = crate::util::counted_utf16(name);
        let name = UNICODE_STRING {
            Length: length,
            MaximumLength: length,
            Buffer: PWSTR(wide.as_mut_ptr()),
        };
        let attrs = OBJECT_ATTRIBUTES {
            Length: size_of::<OBJECT_ATTRIBUTES>() as u32,
            RootDirectory: root,
            ObjectName: &name,
            Attributes: OBJECT_ATTRIBUTE_FLAGS(OBJ_CASE_INSENSITIVE.0 | OBJ_DONT_REPARSE.0),
            ..Default::default()
        };
        let mut raw = HANDLE::default();
        let status = unsafe {
            if target.is_some() {
                NtOpenSymbolicLinkObject(&mut raw, access, &attrs)
            } else {
                NtOpenDirectoryObject(&mut raw, access, &attrs)
            }
        };
        status.ok()?;
        let handle = OwnedHandle(raw);
        let pin = Self {
            security: security(raw)?,
            handle,
            name: full_name,
            kind: if target.is_some() {
                "SymbolicLink"
            } else {
                "Directory"
            },
            target,
        };
        pin.verify()?;
        Ok(pin)
    }
    fn verify(&self) -> Result<()> {
        crate::account_namespace::verify_noninheritable(self.handle.raw())?;
        ensure!(
            text(self.handle.raw(), 1)? == self.name && text(self.handle.raw(), 2)? == self.kind,
            "capture object resolved outside fixed private namespace"
        );
        ensure!(
            security(self.handle.raw())? == self.security,
            "captured object security changed"
        );
        if let Some(target) = &self.target {
            ensure!(
                link_target(self.handle.raw())? == *target,
                "captured link target differs"
            );
        }
        Ok(())
    }
}
pub(crate) fn prepare(
    container: &str,
    account: &str,
    owner: &str,
    stage: &mut String,
) -> Result<(OwnedHandle, OwnedHandle, Proof)> {
    let filtered = super::filtered_account_base(account, owner)?;
    let (token, proof) = compare(&filtered, container, account, stage)?;
    Ok((filtered, token, proof))
}

fn compare(
    filtered: &OwnedHandle,
    container: &str,
    account: &str,
    stage: &mut String,
) -> Result<(OwnedHandle, Proof)> {
    crate::account_impersonation::require_process_identity()?;
    super::verify_account_runner(account)?;
    super::verify_user(filtered.raw(), account)?;
    crate::account_namespace::verify_noninheritable(filtered.raw())?;
    let package = crate::appcontainer::derive(container)?;
    let path = crate::account_namespace::namespace_path(&package.string()?)?;
    let root = Pin::open(&path, HANDLE::default(), OBJECTS[0].1, path.clone(), None)?;
    let rpc = Pin::open(
        "RPC Control",
        root.handle.raw(),
        OBJECTS[1].1,
        format!("{path}\\RPC Control"),
        None,
    )?;
    let mut pins = vec![root, rpc];
    for (name, access) in &OBJECTS[2..5] {
        let target = if *name == "Global" {
            "\\BaseNamedObjects".into()
        } else {
            path.clone()
        };
        pins.push(Pin::open(
            name,
            pins[0].handle.raw(),
            *access,
            format!("{path}\\{name}"),
            Some(target),
        )?);
    }
    pins.push(Pin::open_pipe(&path, stage)?);
    *stage = "captured_namespace_token".into();
    let result: Result<OwnedHandle> = (|| {
        let handles = pins.iter().map(|pin| pin.handle.raw()).collect::<Vec<_>>();
        let token = super::native_lowbox(filtered.raw(), package.raw(), &[], &handles)?;
        crate::account_namespace::verify_noninheritable(token.raw())?;
        super::verify_token(token.raw(), package.raw(), account, NetworkAccess::Off)?;
        for pin in &pins {
            pin.verify()?;
        }
        Ok(token)
    })();
    // The new token retains its own references. No source handle is inherited
    // or returned to the child, and all source closures are checked before use.
    for pin in pins.into_iter().rev() {
        unsafe { CloseHandle(pin.handle.into_raw())? };
    }
    let token = result?;
    let proof = Proof {
        diagnostic_only: true,
        objects: OBJECTS.map(|(name, access)| (name.into(), access)).into(),
        identities_and_security_unchanged: true,
        source_handles_closed: true,
    };
    proof.require()?;
    Ok((token, proof))
}

#[test]
fn station_capture_subset_refuses_changed_objects_grants_or_missing_closure() {
    let good = Proof {
        diagnostic_only: true,
        objects: OBJECTS.map(|(n, a)| (n.into(), a)).into(),
        identities_and_security_unchanged: true,
        source_handles_closed: true,
    };
    good.require().unwrap();
    for case in 0..8 {
        let mut bad = good.clone();
        match case {
            0 => bad.objects[1].1 = 0xf000f,
            1 => bad.objects[1].0 = "shared_rpc".into(),
            2 => {
                bad.objects.pop();
            }
            3 => bad.source_handles_closed = false,
            4 => bad.identities_and_security_unchanged = false,
            5 => bad.objects[5].1 = 0x1f0006,
            6 => bad.objects[5].0 = "shared_pipe".into(),
            _ => bad.diagnostic_only = false,
        }
        assert!(bad.require().is_err());
    }
}
#[test]
fn station_capture_object_text_rejects_foreign_unaligned_and_overflowed_ranges() {
    assert_eq!(text_extent(16, 20, 36).unwrap(), 16..36);
    for (offset, bytes, returned) in [
        (0, 20, 36),
        (17, 20, 40),
        (16, 19, 36),
        (16, 0, 36),
        (16, 4098, 8192),
        (16, 20, 35),
        (usize::MAX - 1, 8, usize::MAX),
    ] {
        assert!(text_extent(offset, bytes, returned).is_err());
    }
}
