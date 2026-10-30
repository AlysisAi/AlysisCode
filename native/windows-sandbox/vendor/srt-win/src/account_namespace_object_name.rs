//! Bounded native object identity queries. A relative open can resolve through
//! a directory shadow, so repeated handle equality alone does not prove location.
use super::*;
use std::mem::size_of_val;
#[link(name = "ntdll")]
unsafe extern "system" {
    fn NtQueryObject(
        handle: HANDLE,
        class: u32,
        data: *mut core::ffi::c_void,
        length: u32,
        returned: *mut u32,
    ) -> NTSTATUS;
}
fn extent(offset: usize, bytes: usize, returned: usize) -> Result<std::ops::Range<usize>> {
    let end = offset.checked_add(bytes).context("object text overflow")?;
    ensure!(
        offset >= size_of::<UNICODE_STRING>()
            && offset % 2 == 0
            && bytes > 0
            && bytes % 2 == 0
            && bytes <= 4096
            && end <= returned,
        "object text outside native response"
    );
    Ok(offset..end)
}
fn query(handle: HANDLE, class: u32) -> Result<String> {
    ensure!(matches!(class, 1 | 2), "unsupported object identity query");
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
    }
    .ok()
    .context("query retained native object identity")?;
    ensure!(
        (size_of::<UNICODE_STRING>()..=size_of_val(&words)).contains(&(returned as usize)),
        "native object response size differs"
    );
    let value = unsafe { &*words.as_ptr().cast::<UNICODE_STRING>() };
    let base = words.as_ptr() as usize;
    let offset = (value.Buffer.0 as usize)
        .checked_sub(base)
        .context("object pointer precedes buffer")?;
    let range = extent(offset, value.Length as usize, returned as usize)?;
    let units =
        unsafe { std::slice::from_raw_parts((base + range.start) as *const u16, range.len() / 2) };
    let text = String::from_utf16(units).context("object identity is not Unicode")?;
    ensure!(
        !text.chars().any(char::is_control),
        "object identity contains controls"
    );
    Ok(text)
}
pub(super) fn directory_name(handle: HANDLE) -> Result<String> {
    ensure!(
        query(handle, 2)? == "Directory",
        "retained object is not Directory"
    );
    let name = query(handle, 1)?;
    ensure!(
        name.starts_with('\\'),
        "native directory identity is not absolute"
    );
    Ok(name)
}
pub(super) fn require_child(root: HANDLE, child: HANDLE, component: &str) -> Result<()> {
    ensure!(
        component == "RPC Control",
        "native child identity requires fixed component"
    );
    let root = directory_name(root)?;
    let child = directory_name(child)?;
    ensure!(
        child == format!("{root}\\{component}"),
        "RPC directory resolved outside original private root"
    );
    Ok(())
}
#[test]
fn namespace_object_text_refuses_foreign_unaligned_truncated_and_oversized_ranges() {
    assert_eq!(extent(16, 20, 36).unwrap(), 16..36);
    for (offset, bytes, returned) in [
        (0, 20, 36),
        (17, 20, 40),
        (16, 19, 36),
        (16, 0, 36),
        (16, 4098, 8192),
        (16, 20, 35),
        (usize::MAX - 1, 8, usize::MAX),
    ] {
        assert!(extent(offset, bytes, returned).is_err());
    }
}
