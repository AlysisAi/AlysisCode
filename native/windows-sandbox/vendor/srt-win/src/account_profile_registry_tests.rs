use super::*;

#[test]
fn account_registry_basic_name_checks_extent_and_component() {
    let mut bytes = vec![0; 16];
    bytes[12..16].copy_from_slice(&4u32.to_ne_bytes());
    bytes.extend_from_slice(&[b'a', 0, b'b', 0]);
    assert_eq!(bounded_basic_name(&bytes).unwrap(), &[b'a', 0, b'b', 0]);
    for end in 0..bytes.len() {
        assert!(bounded_basic_name(&bytes[..end]).is_err());
    }
    for length in [0u32, 1, 3, 6, 1026, u32::MAX] {
        bytes[12..16].copy_from_slice(&length.to_ne_bytes());
        assert!(bounded_basic_name(&bytes).is_err());
    }
    bytes[12..16].copy_from_slice(&4u32.to_ne_bytes());
    for invalid in [
        [0, 0, b'a', 0],
        [92, 0, b'a', 0],
        [47, 0, b'a', 0],
        [0, 0xd8, b'a', 0],
    ] {
        bytes[16..].copy_from_slice(&invalid);
        assert!(bounded_basic_name(&bytes).is_err());
    }
}

#[test]
fn account_registry_name_query_rejects_truncation_and_excess_length() {
    let mut bytes = 4u32.to_ne_bytes().to_vec();
    bytes.extend_from_slice(&[b'a', 0, b'b', 0]);
    assert_eq!(bounded_key_name(&bytes).unwrap(), &[b'a', 0, b'b', 0]);
    for end in 0..bytes.len() {
        assert!(bounded_key_name(&bytes[..end]).is_err());
    }
    for length in [0, 1, 3, 5, u32::MAX] {
        bytes[..4].copy_from_slice(&length.to_ne_bytes());
        assert!(bounded_key_name(&bytes).is_err());
    }
}

#[test]
fn account_registry_token_sid_query_checks_header_pointer_and_full_extent() {
    let sid = crate::sid::sid_bytes("S-1-5-18").unwrap();
    let mut buffer = QueryBuffer::new();
    let header = size_of::<TOKEN_USER>();
    buffer.length = header + sid.len();
    let start = buffer.words.as_ptr() as usize;
    buffer.words[0] = start + header;
    unsafe {
        std::ptr::copy_nonoverlapping(
            sid.as_ptr(),
            buffer.words.as_mut_ptr().cast::<u8>().add(header),
            sid.len(),
        )
    };
    assert_eq!(buffer.sid(header).unwrap(), sid);
    for address in [
        0,
        start - 1,
        start,
        start + header - 1,
        start + buffer.length,
        usize::MAX,
    ] {
        buffer.words[0] = address;
        assert!(buffer.sid(header).is_err());
    }
    buffer.words[0] = start + header;
    buffer.length -= 1;
    assert!(buffer.sid(header).is_err());
    buffer.length = MAX_QUERY + 1;
    assert!(buffer.bytes().is_err());
}

#[test]
fn account_registry_uncertain_close_is_never_retried() {
    let mut key = Some(HKEY(1usize as *mut _));
    let mut calls = 0;
    assert!(
        close_once(&mut key, |_| {
            calls += 1;
            anyhow::bail!("uncertain close")
        })
        .is_err()
    );
    close_once(&mut key, |_| {
        calls += 1;
        Ok(())
    })
    .unwrap();
    assert_eq!(calls, 1);
    assert!(key.is_none());
}

#[test]
fn account_registry_ordinary_token_is_refused_before_sdk_storage_access() {
    let token = OwnedHandle(crate::token::open_self_token().unwrap());
    let actor = crate::sid::current_user_sid().unwrap();
    let result = RetainedProfileRegistry::open(
        &token,
        "Alysis.Execution.0123456789abcdef0123456789abcdef",
        &actor,
    );
    assert!(
        result
            .err()
            .unwrap()
            .to_string()
            .contains("requires an AppContainer token")
    );
    crate::account_impersonation::require_process_identity().unwrap();
}

#[path = "account_profile_registry_host_tests.rs"]
mod disposable;
