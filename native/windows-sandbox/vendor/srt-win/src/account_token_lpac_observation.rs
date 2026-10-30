//! Read only the fixed WIN://NOALLAPPPKG UINT64 attribute. This diagnostic is
//! inspired by NtCoreLib's LowPrivilegeAppContainer and Chromium's native token
//! attribute layout; it supplies no authority and never changes the token.
//! https://github.com/googleprojectzero/sandbox-attacksurface-analysis-tools/blob/main/NtCoreLib/NtToken.cs
//! https://github.com/chromium/chromium/blob/main/base/win/access_token.cc
use anyhow::{Result, ensure};
use serde::{Deserialize, Serialize};
use windows::Win32::{
    Foundation::{ERROR_INSUFFICIENT_BUFFER, HANDLE},
    Security::{GetTokenInformation, TokenSecurityAttributes},
};

const LIMIT: usize = 65536;
const NAME: &str = "WIN://NOALLAPPPKG";

#[derive(Clone, Serialize, Deserialize)]
#[serde(tag = "status", rename_all = "snake_case", deny_unknown_fields)]
pub(super) enum Observation {
    QueryFailed { hresult: i32 },
    InvalidBuffer,
    Observed { attribute: Option<Attribute> },
}

#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Attribute {
    value_type: u16,
    flags: u32,
    count: u32,
    value: u64,
}

impl Observation {
    pub(super) fn require(&self) -> Result<()> {
        match self {
            Self::QueryFailed { hresult } => {
                ensure!(*hresult < 0, "attribute query status differs")
            }
            Self::Observed { attribute: Some(a) } => {
                ensure!(
                    a.value_type == 2 && a.count == 1,
                    "fixed attribute shape differs"
                );
            }
            _ => {}
        }
        Ok(())
    }

    pub(super) fn observe(token: HANDLE) -> Self {
        let mut size = 0;
        let status =
            unsafe { GetTokenInformation(token, TokenSecurityAttributes, None, 0, &mut size) };
        match status {
            Err(error) if error.code() == ERROR_INSUFFICIENT_BUFFER.to_hresult() => {}
            Err(error) => {
                return Self::QueryFailed {
                    hresult: error.code().0,
                };
            }
            Ok(()) => return Self::InvalidBuffer,
        }
        if !(16..=LIMIT as u32).contains(&size) {
            return Self::InvalidBuffer;
        }
        // Native pointers refer into this allocation. Keep its address stable
        // while parsing, and use only the returned byte length (not padding).
        let mut words = vec![0usize; (size as usize).div_ceil(std::mem::size_of::<usize>())];
        let capacity = size;
        if let Err(error) = unsafe {
            GetTokenInformation(
                token,
                TokenSecurityAttributes,
                Some(words.as_mut_ptr().cast()),
                capacity,
                &mut size,
            )
        } {
            return Self::QueryFailed {
                hresult: error.code().0,
            };
        }
        if size > capacity {
            return Self::InvalidBuffer;
        }
        let bytes =
            unsafe { std::slice::from_raw_parts(words.as_ptr().cast::<u8>(), size as usize) };
        match parse(bytes, words.as_ptr() as usize) {
            Some(attribute) => Self::Observed { attribute },
            None => Self::InvalidBuffer,
        }
    }
}

// AMD64 native TOKEN_SECURITY_ATTRIBUTES_INFORMATION (16 bytes), followed by
// TOKEN_SECURITY_ATTRIBUTE_V1 records (40 bytes, UNICODE_STRING name). These
// differ from CLAIM_SECURITY_ATTRIBUTE_V1, whose name is a PWSTR. Never follow
// a native pointer: convert it to a checked offset into the returned bytes.
fn parse(bytes: &[u8], base: usize) -> Option<Option<Attribute>> {
    if std::mem::size_of::<usize>() != 8 || !(16..=LIMIT).contains(&bytes.len()) {
        return None;
    }
    let buffer = Buffer { bytes, base };
    if buffer.u16(0)? != 1 || buffer.u16(2)? != 0 {
        return None;
    }
    let count = buffer.u32(4)? as usize;
    if count > 128 {
        return None;
    }
    if count == 0 {
        return Some(None);
    }
    let entries = buffer.range(buffer.u64(8)?, count.checked_mul(40)?, 8)?;
    let expected: Vec<u16> = NAME.encode_utf16().collect();
    let mut found = None;
    for index in 0..count {
        let entry = entries + index * 40;
        let length = buffer.u16(entry)? as usize;
        let maximum = buffer.u16(entry + 2)? as usize;
        if length == 0 || length % 2 != 0 || maximum % 2 != 0 || length > maximum || maximum > 1024
        {
            return None;
        }
        let name = buffer.range(buffer.u64(entry + 8)?, maximum, 2)?;
        if length != expected.len() * 2
            || !(0..expected.len()).all(|i| buffer.u16(name + i * 2) == Some(expected[i]))
        {
            continue;
        }
        let value_type = buffer.u16(entry + 16)?;
        let reserved = buffer.u16(entry + 18)?;
        let flags = buffer.u32(entry + 20)?;
        let count = buffer.u32(entry + 24)?;
        if found.is_some() || value_type != 2 || reserved != 0 || count != 1 {
            return None;
        }
        let value = buffer.range(buffer.u64(entry + 32)?, 8, 8)?;
        found = Some(Attribute {
            value_type,
            flags,
            count,
            value: buffer.u64(value)?,
        });
    }
    Some(found)
}

struct Buffer<'a> {
    bytes: &'a [u8],
    base: usize,
}
impl Buffer<'_> {
    fn u16(&self, offset: usize) -> Option<u16> {
        Some(u16::from_le_bytes(
            self.bytes
                .get(offset..offset.checked_add(2)?)?
                .try_into()
                .ok()?,
        ))
    }
    fn u32(&self, offset: usize) -> Option<u32> {
        Some(u32::from_le_bytes(
            self.bytes
                .get(offset..offset.checked_add(4)?)?
                .try_into()
                .ok()?,
        ))
    }
    fn u64(&self, offset: usize) -> Option<u64> {
        Some(u64::from_le_bytes(
            self.bytes
                .get(offset..offset.checked_add(8)?)?
                .try_into()
                .ok()?,
        ))
    }
    fn range(&self, pointer: u64, length: usize, alignment: usize) -> Option<usize> {
        let pointer = usize::try_from(pointer).ok()?;
        let offset = pointer.checked_sub(self.base)?;
        if pointer % alignment != 0 || offset.checked_add(length)? > self.bytes.len() {
            None
        } else {
            Some(offset)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    const BASE: usize = 0x10000;
    fn put(bytes: &mut [u8], offset: usize, data: &[u8]) {
        bytes[offset..offset + data.len()].copy_from_slice(data);
    }
    fn fixture(value: u64) -> Vec<u8> {
        let mut b = vec![0; 160];
        put(&mut b, 0, &1u16.to_le_bytes());
        put(&mut b, 4, &1u32.to_le_bytes());
        put(&mut b, 8, &(BASE as u64 + 16).to_le_bytes());
        let name: Vec<u8> = NAME.encode_utf16().flat_map(u16::to_le_bytes).collect();
        put(&mut b, 16, &(name.len() as u16).to_le_bytes());
        put(&mut b, 18, &(name.len() as u16).to_le_bytes());
        put(&mut b, 24, &(BASE as u64 + 96).to_le_bytes());
        put(&mut b, 32, &2u16.to_le_bytes());
        put(&mut b, 40, &1u32.to_le_bytes());
        put(&mut b, 48, &(BASE as u64 + 144).to_le_bytes());
        put(&mut b, 96, &name);
        put(&mut b, 144, &value.to_le_bytes());
        b
    }

    #[test]
    fn station_lpac_attribute_keeps_absence_zero_and_nonzero_distinct() {
        for value in [0, 1, u64::MAX] {
            let b = fixture(value);
            assert_eq!(parse(&b, BASE).unwrap().unwrap().value, value);
        }
        let mut b = fixture(1);
        put(&mut b, 4, &0u32.to_le_bytes());
        assert!(parse(&b[..16], BASE).unwrap().is_none());
        let mut b = fixture(1);
        b[96] = b'X';
        assert!(parse(&b, BASE).unwrap().is_none());
    }

    #[test]
    fn station_lpac_attribute_rejects_truncation_pointer_escape_and_wrong_shape() {
        let b = fixture(1);
        for length in 0..152 {
            assert!(parse(&b[..length], BASE).is_none());
        }
        for offset in [8, 24, 48] {
            for pointer in [
                0,
                BASE as u64 - 8,
                BASE as u64 + 161,
                u64::MAX,
                BASE as u64 + 17,
            ] {
                let mut b = fixture(1);
                put(&mut b, offset, &pointer.to_le_bytes());
                assert!(parse(&b, BASE).is_none());
            }
        }
        for (offset, data) in [
            (0, 2u16.to_le_bytes().to_vec()),
            (2, 1u16.to_le_bytes().to_vec()),
            (4, 129u32.to_le_bytes().to_vec()),
            (16, 31u16.to_le_bytes().to_vec()),
            (18, 2u16.to_le_bytes().to_vec()),
            (32, 3u16.to_le_bytes().to_vec()),
            (34, 1u16.to_le_bytes().to_vec()),
            (40, 2u32.to_le_bytes().to_vec()),
        ] {
            let mut b = fixture(1);
            put(&mut b, offset, &data);
            assert!(parse(&b, BASE).is_none());
        }
    }

    #[test]
    fn station_lpac_attribute_rejects_duplicate_matches_and_unknown_fields() {
        let mut b = fixture(1);
        let record = b[16..56].to_vec();
        put(&mut b, 56, &record);
        put(&mut b, 4, &2u32.to_le_bytes());
        assert!(parse(&b, BASE).is_none());
        assert!(Observation::QueryFailed { hresult: 0 }.require().is_err());
        let mut value = serde_json::to_value(Observation::Observed { attribute: None }).unwrap();
        value["token_handle"] = 7.into();
        assert!(serde_json::from_value::<Observation>(value).is_err());
    }
}
