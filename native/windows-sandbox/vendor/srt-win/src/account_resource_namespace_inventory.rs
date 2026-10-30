//! Bounded read-only namespace observations. Names/types are diagnostic data,
//! never ownership, relative-open authority, process quiescence or ACL policy.
use super::*;
use std::mem::size_of_val;

const CAPACITY: usize = 8192;
const MAX_ENTRIES: usize = 16;
#[repr(C)]
#[derive(Clone, Copy)]
struct EntryHeader {
    name: UNICODE_STRING,
    kind: UNICODE_STRING,
}

#[derive(Debug, PartialEq, Eq, PartialOrd, Ord)]
pub(in crate::account_state::execution) struct DirectoryEntry {
    pub(in crate::account_state::execution) name: String,
    pub(in crate::account_state::execution) kind: String,
}
fn parse(buffer: &[usize], returned: usize) -> Result<DirectoryEntry> {
    ensure!(
        returned >= size_of::<EntryHeader>()
            && returned <= size_of_val(buffer)
            && returned <= CAPACITY,
        "namespace directory result extent invalid"
    );
    let bytes = unsafe { std::slice::from_raw_parts(buffer.as_ptr().cast::<u8>(), returned) };
    // Read fields only after bounding the complete header. Embedded pointers
    // are converted into checked offsets, never dereferenced directly.
    let header = unsafe { std::ptr::read_unaligned(bytes.as_ptr().cast::<EntryHeader>()) };
    let mut ranges = Vec::new();
    let mut text = |value: UNICODE_STRING, max: usize| -> Result<String> {
        let length = value.Length as usize;
        let capacity = value.MaximumLength as usize;
        let address = value.Buffer.0 as usize;
        let start = address
            .checked_sub(buffer.as_ptr() as usize)
            .context("namespace string before returned buffer")?;
        let end = start
            .checked_add(capacity)
            .context("namespace string extent overflow")?;
        ensure!(
            length > 0
                && length % 2 == 0
                && length <= max * 2
                && capacity >= length
                && capacity % 2 == 0
                && start >= size_of::<EntryHeader>()
                && address % 2 == 0
                && end <= returned,
            "namespace string outside bounded payload"
        );
        ensure!(
            ranges.iter().all(|&(a, b)| end <= a || start >= b),
            "namespace strings overlap"
        );
        ranges.push((start, end));
        let wide = bytes[start..start + length]
            .chunks_exact(2)
            .map(|b| u16::from_le_bytes([b[0], b[1]]))
            .collect::<Vec<_>>();
        let text = String::from_utf16(&wide).context("invalid namespace UTF-16")?;
        ensure!(
            !text
                .chars()
                .any(|c| c.is_control() || c == '\\' || c == '/')
                && text != "."
                && text != "..",
            "namespace component is not a simple name"
        );
        Ok(text)
    };
    Ok(DirectoryEntry {
        name: text(header.name, 512)?,
        kind: text(header.kind, 128)?,
    })
}

pub(in crate::account_state::execution) fn observe_children(
    root: HANDLE,
) -> Result<Vec<DirectoryEntry>> {
    crate::account_impersonation::require_process_identity()?;
    crate::account_namespace::verify_noninheritable(root)?;
    let mut context = 0;
    let mut result: Vec<DirectoryEntry> = Vec::new();
    for index in 0..=MAX_ENTRIES {
        let mut buffer = [0usize; CAPACITY / size_of::<usize>()];
        let mut returned = 0;
        let status = unsafe {
            NtQueryDirectoryObject(
                root,
                Some(buffer.as_mut_ptr().cast()),
                CAPACITY as u32,
                true,
                index == 0,
                &mut context,
                Some(&mut returned),
            )
        };
        if status == NO_MORE_ENTRIES {
            result.sort();
            ensure!(
                result.windows(2).all(|p| p[0].name != p[1].name),
                "duplicate namespace entry observation"
            );
            return Ok(result);
        }
        ensure!(
            status.0 == 0 && index < MAX_ENTRIES,
            "namespace inventory incomplete or exceeds bounds (NTSTATUS=0x{:08x})",
            status.0 as u32
        );
        result.push(parse(&buffer, returned as usize)?);
    }
    unreachable!()
}

/// Fixed names prevent observations from becoming caller-selected traversal.
#[derive(Clone, Copy, Debug)]
pub(in crate::account_state::execution) enum InitializerLink {
    Global,
    Local,
    Session,
}
impl InitializerLink {
    fn name(self) -> &'static str {
        match self {
            Self::Global => "Global",
            Self::Local => "Local",
            Self::Session => "Session",
        }
    }
}
const LINK_UNITS: usize = 2048;
fn parse_link_target(
    buffer: &[u16; LINK_UNITS],
    value: &UNICODE_STRING,
    returned: u32,
) -> Result<String> {
    let length = usize::from(value.Length);
    ensure!(
        value.Buffer.0 == buffer.as_ptr().cast_mut()
            && usize::from(value.MaximumLength) == size_of_val(buffer)
            && length > 0
            && length % 2 == 0
            && length <= size_of_val(buffer)
            && (returned as usize == length
                || (returned as usize == length + 2
                    && length + 2 <= size_of_val(buffer)
                    && buffer[length / 2] == 0)),
        "symbolic-link query changed buffer or returned an invalid extent"
    );
    let text = String::from_utf16(&buffer[..length / 2]).context("invalid link target UTF-16")?;
    ensure!(
        !text.chars().any(|c| c.is_control()),
        "symbolic-link target contains control characters"
    );
    Ok(text)
}
/// Query the link itself relative to the original root. No target open, ACL
/// mutation or lifecycle authority is conveyed by the returned diagnostic text.
pub(in crate::account_state::execution) fn observe_initializer_link(
    root: HANDLE,
    link: InitializerLink,
) -> Result<String> {
    let target = query_link(root, link.name())?;
    ensure!(
        target.starts_with('\\'),
        "initializer link is not an absolute NT name"
    );
    Ok(target)
}
fn query_link(root: HANDLE, link: &str) -> Result<String> {
    use windows::Wdk::Storage::FileSystem::{NtOpenSymbolicLinkObject, NtQuerySymbolicLinkObject};
    ensure!(
        !link.is_empty()
            && link.len() <= 64
            && link != "."
            && link != ".."
            && !link
                .chars()
                .any(|c| c.is_control() || c == '\\' || c == '/'),
        "link query requires one bounded relative component"
    );
    crate::account_impersonation::require_process_identity()?;
    crate::account_namespace::verify_noninheritable(root)?;
    let (mut wide, length) = unicode(link)?;
    let mut name = UNICODE_STRING {
        Length: length,
        MaximumLength: length,
        Buffer: PWSTR(wide.as_mut_ptr()),
    };
    // NtOpenSymbolicLinkObject selects the link object type itself. Windows
    // rejects OBJ_OPENLINK for this API; retain OBJ_DONT_REPARSE on the name.
    let attrs = attributes(&mut name, root);
    let mut raw = HANDLE::default();
    let status = unsafe { NtOpenSymbolicLinkObject(&mut raw, 1, &attrs) }; // SYMBOLIC_LINK_QUERY only
    ensure!(
        status.0 == 0,
        "open initializer link {:?} without traversal: NTSTATUS=0x{:08x}",
        link,
        status.0 as u32
    );
    let handle = OwnedHandle(raw);
    let observation = (|| {
        crate::account_namespace::verify_noninheritable(handle.raw())?;
        let mut buffer = [0u16; LINK_UNITS];
        let mut target = UNICODE_STRING {
            Length: 0,
            MaximumLength: size_of_val(&buffer) as u16,
            Buffer: PWSTR(buffer.as_mut_ptr()),
        };
        let mut returned = 0;
        let status =
            unsafe { NtQuerySymbolicLinkObject(handle.raw(), &mut target, Some(&mut returned)) };
        ensure!(
            status.0 == 0,
            "bounded initializer link query {:?}: NTSTATUS=0x{:08x}",
            link,
            status.0 as u32
        );
        parse_link_target(&buffer, &target, returned)
    })();
    unsafe { CloseHandle(handle.into_raw()) }
        .context("initializer link query handle close failed")?;
    observation
}

#[cfg(test)]
mod tests {
    use super::*;
    fn fixture() -> (Vec<usize>, usize) {
        let mut data = vec![0usize; 128];
        let mut at = size_of::<EntryHeader>();
        let mut add = |text: &str| {
            let wide = text.encode_utf16().collect::<Vec<_>>();
            let start = at;
            for value in &wide {
                unsafe {
                    std::ptr::write_unaligned(
                        data.as_mut_ptr().cast::<u8>().add(at).cast::<u16>(),
                        *value,
                    );
                }
                at += 2;
            }
            UNICODE_STRING {
                Length: (wide.len() * 2) as u16,
                MaximumLength: (wide.len() * 2) as u16,
                Buffer: PWSTR(unsafe { data.as_mut_ptr().cast::<u8>().add(start).cast() }),
            }
        };
        let header = EntryHeader {
            name: add("RPC Control"),
            kind: add("Directory"),
        };
        unsafe {
            std::ptr::write(data.as_mut_ptr().cast::<EntryHeader>(), header);
        }
        (data, at)
    }
    #[test]
    fn initializer_namespace_inventory_bounds_native_pointer_extents() {
        let (data, returned) = fixture();
        assert_eq!(
            parse(&data, returned).unwrap(),
            DirectoryEntry {
                name: "RPC Control".into(),
                kind: "Directory".into()
            }
        );
        for size in [0, size_of::<EntryHeader>() - 1, returned - 1, CAPACITY + 1] {
            assert!(parse(&data, size).is_err());
        }
        for case in 0..7 {
            let (mut data, returned) = fixture();
            let h = unsafe { &mut *data.as_mut_ptr().cast::<EntryHeader>() };
            match case {
                0 => h.name.Buffer = PWSTR::null(),
                1 => h.name.Length = 1,
                2 => h.name.MaximumLength = h.name.Length - 2,
                3 => h.name.MaximumLength = u16::MAX - 1,
                4 => h.kind.Buffer = h.name.Buffer,
                5 => h.name.Buffer = PWSTR(unsafe { h.name.Buffer.0.cast::<u8>().add(1).cast() }),
                _ => h.name.Buffer = PWSTR(usize::MAX as *mut u16),
            }
            assert!(parse(&data, returned).is_err(), "case {case}");
        }
    }
    #[test]
    fn initializer_namespace_inventory_refuses_noncomponent_and_invalid_utf16() {
        for value in [0, 47, 92, 0xd800] {
            let (mut data, returned) = fixture();
            let h = unsafe { &mut *data.as_mut_ptr().cast::<EntryHeader>() };
            unsafe {
                *h.name.Buffer.0 = value;
            }
            assert!(parse(&data, returned).is_err());
        }
    }
    #[test]
    fn initializer_link_target_refuses_extent_pointer_and_encoding_drift() {
        let mut buffer = [0u16; LINK_UNITS];
        let text: Vec<_> = "\\Sessions\\1\\AppContainerNamedObjects\\fixture"
            .encode_utf16()
            .collect();
        buffer[..text.len()].copy_from_slice(&text);
        let good = UNICODE_STRING {
            Length: (text.len() * 2) as u16,
            MaximumLength: size_of_val(&buffer) as u16,
            Buffer: PWSTR(buffer.as_mut_ptr()),
        };
        assert_eq!(
            parse_link_target(&buffer, &good, good.Length as u32).unwrap(),
            String::from_utf16(&text).unwrap()
        );
        assert!(parse_link_target(&buffer, &good, good.Length as u32 + 2).is_ok());
        buffer[text.len()] = 1;
        assert!(parse_link_target(&buffer, &good, good.Length as u32 + 2).is_err());
        buffer[text.len()] = 0;
        for case in 0..7 {
            let mut value = good;
            let mut returned = value.Length as u32;
            match case {
                0 => value.Buffer = PWSTR::null(),
                1 => value.Length = 1,
                2 => value.Length = 0,
                3 => value.Length = u16::MAX - 1,
                4 => value.MaximumLength -= 2,
                5 => returned += 4,
                _ => value.Buffer = PWSTR(usize::MAX as *mut u16),
            }
            assert!(
                parse_link_target(&buffer, &value, returned).is_err(),
                "case {case}"
            );
        }
        for value in [0, 10, 0xd800] {
            buffer[2] = value;
            assert!(parse_link_target(&buffer, &good, good.Length as u32).is_err());
        }
    }
    #[test]
    fn initializer_link_native_query_opens_link_itself_with_no_reparse_flag() {
        // Read-only OS link, no synthetic namespace, account or profile effects.
        let root = open_directory("\\KnownDlls", HANDLE::default(), QUERY_ACCESS)
            .map_err(|s| status_error(s, "open KnownDlls query-only"))
            .unwrap();
        let global = fixture_global_security().unwrap();
        let target = query_link(root.raw(), "KnownDllPath").unwrap();
        assert!(!target.is_empty());
        assert!(query_link(root.raw(), "missing-alysis-query-only-link").is_err());
        assert!(query_link(root.raw(), "..\\KnownDllPath").is_err());
        unsafe { CloseHandle(root.into_raw()) }.unwrap();
        assert_eq!(fixture_global_security().unwrap(), global);
    }
}
