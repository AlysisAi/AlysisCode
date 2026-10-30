//! Read-only NTFS observations for a future, explicitly privileged repair tool.
//!
//! NOT wired into recovery. No outcome authorizes clearing a journal or changing
//! an ACL. A same-reference result also cannot distinguish a live original from
//! a reference reused after the 16-bit sequence wraps. This is a checked parser
//! of successful filesystem-driver responses, not a raw-disk/MFT reader.
//!
//! Contracts: Microsoft Learn `FSCTL_GET_NTFS_FILE_RECORD` (downward, in-use
//! enumeration), `NTFS_VOLUME_DATA_BUFFER`, `FILE_RECORD_SEGMENT_HEADER`,
//! `MULTI_SECTOR_HEADER`, and `MFT_SEGMENT_REFERENCE`. See the adjacent
//! `ntfs_identity_evidence.md` for qualification and integration requirements.

use anyhow::{Context, Result, ensure};

use crate::path_id::FileId;

#[cfg(test)]
#[path = "ntfs_identity_disposable_host_tests.rs"]
mod disposable_host_tests;

/// Fixed diagnostic context only; never carries returned metadata or paths.
#[derive(Debug)]
struct ProbeStep(&'static str);
impl std::fmt::Display for ProbeStep {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(formatter, "NTFS observation step {}", self.0)
    }
}
impl std::error::Error for ProbeStep {}

/// Typed rejection reasons preserve strict parser acceptance while allowing
/// sanitized qualification evidence. No variant contains returned metadata.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum ParseFailure {
    ReferenceSequenceZero,
    IdentityNotRepresentable,
    MetadataOffsetOverflow,
    MetadataTruncated,
    FileRecordVolumeMismatch,
    RequestedRecordOutsideMft,
    ReturnedHigherOrdinal,
    RecordLengthMismatch,
    ResponseLength,
    ResponsePadding,
    RecordSignature,
    HeaderSequenceMismatch,
    RecordFlags,
    ExtensionRecord,
    FirstAttributeOffset,
    UpdateSequenceHeader,
    UpdateSequenceLengthOverflow,
    UpdateSequenceOffsetOverflow,
    UpdateSequenceOverlap,
    FullReferenceMismatch,
    AnchorIdentityMismatch,
    OutputEncodingMismatch,
}
impl ParseFailure {
    #[cfg(test)]
    fn label(self) -> &'static str {
        match self {
            Self::ReferenceSequenceZero => "reference_sequence_zero",
            Self::IdentityNotRepresentable => "identity_not_representable",
            Self::MetadataOffsetOverflow => "metadata_offset_overflow",
            Self::MetadataTruncated => "metadata_truncated",
            Self::FileRecordVolumeMismatch => "file_record_volume_mismatch",
            Self::RequestedRecordOutsideMft => "requested_record_outside_mft",
            Self::ReturnedHigherOrdinal => "returned_higher_ordinal",
            Self::RecordLengthMismatch => "record_length_mismatch",
            Self::ResponseLength => "response_length",
            Self::ResponsePadding => "response_padding",
            Self::RecordSignature => "record_signature",
            Self::HeaderSequenceMismatch => "header_sequence_mismatch",
            Self::RecordFlags => "record_flags",
            Self::ExtensionRecord => "extension_record",
            Self::FirstAttributeOffset => "first_attribute_offset",
            Self::UpdateSequenceHeader => "update_sequence_header",
            Self::UpdateSequenceLengthOverflow => "update_sequence_length_overflow",
            Self::UpdateSequenceOffsetOverflow => "update_sequence_offset_overflow",
            Self::UpdateSequenceOverlap => "update_sequence_overlap",
            Self::FullReferenceMismatch => "full_reference_mismatch",
            Self::AnchorIdentityMismatch => "anchor_identity_mismatch",
            Self::OutputEncodingMismatch => "output_encoding_mismatch",
        }
    }
}
impl std::fmt::Display for ParseFailure {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(match self {
            Self::ReferenceSequenceZero => "NTFS reference has reserved sequence zero",
            Self::IdentityNotRepresentable => {
                "file identity is not losslessly representable as an NTFS reference"
            }
            Self::MetadataOffsetOverflow => "NTFS offset overflow",
            Self::MetadataTruncated => "truncated NTFS metadata",
            Self::FileRecordVolumeMismatch => "file-record volume mismatch",
            Self::RequestedRecordOutsideMft => {
                "requested record lies outside observed MFT; no absence inference"
            }
            Self::ReturnedHigherOrdinal => "NTFS lookup returned a higher record ordinal",
            Self::RecordLengthMismatch => "NTFS record length differs from volume geometry",
            Self::ResponseLength => "truncated or oversized NTFS file-record response",
            Self::ResponsePadding => "unexpected NTFS response padding",
            Self::RecordSignature => "invalid NTFS file-record signature",
            Self::HeaderSequenceMismatch => "NTFS header and full returned reference disagree",
            Self::RecordFlags => "NTFS lookup returned an unused or unsupported record",
            Self::ExtensionRecord => "extension MFT record is unsupported identity evidence",
            Self::FirstAttributeOffset => "invalid NTFS first-attribute offset",
            Self::UpdateSequenceHeader => "invalid NTFS update-sequence header",
            Self::UpdateSequenceLengthOverflow => "NTFS USA length overflow",
            Self::UpdateSequenceOffsetOverflow => "NTFS USA offset overflow",
            Self::UpdateSequenceOverlap => {
                "NTFS update-sequence array overlaps data or transfer trailer"
            }
            Self::FullReferenceMismatch => "NTFS full reference mismatch",
            Self::AnchorIdentityMismatch => "NTFS live anchor did not reproduce its full identity",
            Self::OutputEncodingMismatch => "NTFS output differs from the calibrated encoding",
        })
    }
}
impl std::error::Error for ParseFailure {}

/// Fixed source context, never the reference or ordinal itself.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum ReferenceInput {
    FileId,
    ReturnedReference,
}
impl ReferenceInput {
    fn label(self) -> &'static str {
        match self {
            Self::FileId => "file_id",
            Self::ReturnedReference => "returned_reference",
        }
    }
}
impl std::fmt::Display for ReferenceInput {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(formatter, "NTFS reference input {}", self.label())
    }
}
impl std::error::Error for ReferenceInput {}

const RECORD_MASK: u64 = (1u64 << 48) - 1;
const MAX_RECORD_BYTES: usize = 64 * 1024;
const VOLUME_BASE_BYTES: usize = 96;
const RECORD_PREFIX_BYTES: usize = 12;
const HEADER_BYTES: usize = 42;
// CTL_CODE(FILE_DEVICE_FILE_SYSTEM=9, function=25/26, METHOD_BUFFERED=0,
// FILE_ANY_ACCESS=0). Both operations query metadata; neither writes it.
const FSCTL_GET_NTFS_VOLUME_DATA: u32 = (9 << 16) | (25 << 2);
const FSCTL_GET_NTFS_FILE_RECORD: u32 = (9 << 16) | (26 << 2);

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct Reference {
    raw: u64,
    record: u64,
    sequence: u16,
}

impl Reference {
    fn from_raw(raw: u64) -> Result<Self> {
        let value = Self {
            raw,
            record: raw & RECORD_MASK,
            sequence: (raw >> 48) as u16,
        };
        ensure!(value.sequence != 0, ParseFailure::ReferenceSequenceZero);
        Ok(value)
    }

    fn from_id(id: &FileId) -> Result<Self> {
        ensure!(
            id.id128[8..] == [0; 8],
            ParseFailure::IdentityNotRepresentable
        );
        Self::from_raw(u64::from_le_bytes(id.id128[..8].try_into().unwrap()))
            .context(ReferenceInput::FileId)
    }
}

/// Geometry and version parsed from one successful volume-data response.
/// Private fields prevent callers from bypassing the validation gates.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct VolumeEvidence {
    serial: u64,
    major: u16,
    minor: u16,
    bytes_per_sector: u32,
    bytes_per_cluster: u32,
    bytes_per_record: u32,
    mft_length: u64,
}

/// An observation at the instant of the successful driver query, not a durable
/// absence certificate. Do not collapse errors into any of these variants.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RecordObservation {
    /// The complete returned reference matches. Cleanup still needs a verified
    /// object handle; this observation supplies neither a path nor that handle.
    SameReference,
    /// The requested record is in use with a different, nonzero sequence.
    DifferentSequence { observed: u16 },
    /// Under the documented downward lookup contract, the requested ordinal was
    /// not returned as in-use. Never interpret this as a tombstone record: the
    /// returned record belongs to a different object and must not be modified.
    LowerInUseRecord { record: u64, sequence: u16 },
}

fn bytes<const N: usize>(data: &[u8], offset: usize) -> Result<[u8; N]> {
    data.get(
        offset
            ..offset
                .checked_add(N)
                .context(ParseFailure::MetadataOffsetOverflow)?,
    )
    .context(ParseFailure::MetadataTruncated)?
    .try_into()
    .map_err(Into::into)
}

fn u16_at(data: &[u8], offset: usize) -> Result<u16> {
    Ok(u16::from_le_bytes(bytes(data, offset)?))
}
fn u32_at(data: &[u8], offset: usize) -> Result<u32> {
    Ok(u32::from_le_bytes(bytes(data, offset)?))
}
fn u64_at(data: &[u8], offset: usize) -> Result<u64> {
    Ok(u64::from_le_bytes(bytes(data, offset)?))
}

/// Parse only the actual bytes returned by a successful volume FSCTL. The
/// extended version header is mandatory; missing/unsupported layouts fail.
pub fn parse_volume_data(expected: &FileId, data: &[u8]) -> Result<VolumeEvidence> {
    Reference::from_id(expected)?;
    ensure!(
        (104..=256).contains(&data.len()),
        "invalid NTFS volume response length"
    );
    let extended = u32_at(data, VOLUME_BASE_BYTES)? as usize;
    ensure!(
        (8..=data.len() - VOLUME_BASE_BYTES).contains(&extended),
        "incomplete NTFS version data"
    );
    let volume = VolumeEvidence {
        serial: u64_at(data, 0)?,
        major: u16_at(data, 100)?,
        minor: u16_at(data, 102)?,
        bytes_per_sector: u32_at(data, 40)?,
        bytes_per_cluster: u32_at(data, 44)?,
        bytes_per_record: u32_at(data, 48)?,
        mft_length: u64_at(data, 56)?,
    };
    ensure!(
        volume.serial == expected.volume_serial,
        "NTFS volume serial differs from recorded identity"
    );
    ensure!(
        volume.major == 3 && volume.minor <= 1,
        "unsupported NTFS record layout version"
    );
    ensure!(
        (512..=65536).contains(&volume.bytes_per_sector)
            && volume.bytes_per_sector.is_power_of_two(),
        "unsupported NTFS sector size"
    );
    ensure!(
        volume.bytes_per_cluster >= volume.bytes_per_sector
            && volume.bytes_per_cluster <= 2 * 1024 * 1024
            && volume.bytes_per_cluster.is_power_of_two(),
        "unsupported NTFS cluster size"
    );
    ensure!(
        (512..=MAX_RECORD_BYTES as u32).contains(&volume.bytes_per_record)
            && volume.bytes_per_record.is_power_of_two(),
        "unsupported NTFS file-record size"
    );
    ensure!(
        volume.mft_length > 0
            && volume.mft_length <= i64::MAX as u64
            && volume.mft_length % u64::from(volume.bytes_per_record) == 0,
        "invalid NTFS MFT length"
    );
    ensure!(
        u64_at(data, 8)? > 0 && u64_at(data, 8)? <= i64::MAX as u64,
        "invalid NTFS volume sector count"
    );
    Ok(volume)
}

/// Parse a successful ordinal query on the previously verified volume.
/// This public parser still requires a full reference in the prefix;
/// ordinal-only input cannot acquire the private live-probe calibration.
pub fn parse_file_record(
    expected: &FileId,
    volume: &VolumeEvidence,
    data: &[u8],
) -> Result<RecordObservation> {
    let record = ValidatedRecord::parse(expected, volume, data)?;
    let returned = Reference::from_raw(record.prefix).context(ReferenceInput::ReturnedReference)?;
    ensure!(
        returned == record.reference,
        ParseFailure::HeaderSequenceMismatch
    );
    record.observation(OutputEncoding::FullReference)
}

/// Complete structural validation, not permission to interpret the prefix.
/// No raw response bytes survive this private value.
struct ValidatedRecord {
    wanted: Reference,
    prefix: u64,
    reference: Reference,
}

impl ValidatedRecord {
    fn parse(expected: &FileId, volume: &VolumeEvidence, data: &[u8]) -> Result<Self> {
        let wanted = Reference::from_id(expected)?;
        ensure!(
            volume.serial == expected.volume_serial,
            ParseFailure::FileRecordVolumeMismatch
        );
        let records = volume.mft_length / u64::from(volume.bytes_per_record);
        ensure!(
            wanted.record < records,
            ParseFailure::RequestedRecordOutsideMft
        );
        let prefix = u64_at(data, 0)?;
        let ordinal = prefix & RECORD_MASK;
        ensure!(
            ordinal <= wanted.record,
            ParseFailure::ReturnedHigherOrdinal
        );
        let record_len = u32_at(data, 8)? as usize;
        ensure!(
            record_len == volume.bytes_per_record as usize,
            ParseFailure::RecordLengthMismatch
        );
        // SDK allocation formula is sizeof(OUTPUT_BUFFER) + record_size - 1;
        // the first record byte is at offset12, with up to3 final padding bytes.
        let end = RECORD_PREFIX_BYTES + record_len;
        ensure!(
            (end..=end + 3).contains(&data.len()),
            ParseFailure::ResponseLength
        );
        ensure!(
            data[end..].iter().all(|byte| *byte == 0),
            ParseFailure::ResponsePadding
        );
        let record = &data[RECORD_PREFIX_BYTES..end];
        ensure!(&record[..4] == b"FILE", ParseFailure::RecordSignature);
        let sequence = u16_at(record, 16)?;
        ensure!(sequence != 0, ParseFailure::HeaderSequenceMismatch);
        let flags = u16_at(record, 22)?;
        ensure!(flags & 1 != 0 && flags & !3 == 0, ParseFailure::RecordFlags);
        ensure!(u64_at(record, 32)? == 0, ParseFailure::ExtensionRecord);
        let first_attribute = u16_at(record, 20)? as usize;
        ensure!(
            first_attribute >= HEADER_BYTES
                && first_attribute % 8 == 0
                && first_attribute <= record_len - 4,
            ParseFailure::FirstAttributeOffset
        );
        let usa_offset = u16_at(record, 4)? as usize;
        let usa_size = u16_at(record, 6)? as usize;
        // Identity fields are entirely before the first512-byte transfer trailer.
        // Do not undo/apply raw-sector fixups to driver-returned bytes. Microsoft
        // describes this field as bytes, whereas layouts may expose USHORT counts;
        // reserve the larger extent conservatively and reject overlaps. This is a
        // bounds check, NOT an assertion that raw-disk integrity was validated.
        ensure!(
            usa_offset >= HEADER_BYTES && usa_offset % 2 == 0 && usa_size >= 2,
            ParseFailure::UpdateSequenceHeader
        );
        let usa_end = usa_offset
            .checked_add(
                usa_size
                    .checked_mul(2)
                    .context(ParseFailure::UpdateSequenceLengthOverflow)?,
            )
            .context(ParseFailure::UpdateSequenceOffsetOverflow)?;
        ensure!(
            usa_end <= 510 && usa_end <= first_attribute,
            ParseFailure::UpdateSequenceOverlap
        );
        // The prefix is not yet accepted as either representation. Only the
        // strict public decoder or the retained live calibration can authorize it.
        Ok(Self {
            wanted,
            prefix,
            reference: Reference {
                raw: ordinal | (u64::from(sequence) << 48),
                record: ordinal,
                sequence,
            },
        })
    }

    fn require_encoding(&self, encoding: OutputEncoding) -> Result<()> {
        let expected = match encoding {
            OutputEncoding::FullReference => self.reference.raw,
            OutputEncoding::OrdinalOnly => self.reference.record,
        };
        ensure!(
            self.prefix == expected,
            ParseFailure::OutputEncodingMismatch
        );
        Ok(())
    }

    fn require_anchor(&self) -> Result<()> {
        ensure!(
            self.reference == self.wanted,
            ParseFailure::AnchorIdentityMismatch
        );
        Ok(())
    }

    fn observation(&self, encoding: OutputEncoding) -> Result<RecordObservation> {
        self.require_encoding(encoding)?;
        if self.reference.record < self.wanted.record {
            Ok(RecordObservation::LowerInUseRecord {
                record: self.reference.record,
                sequence: self.reference.sequence,
            })
        } else if self.reference.sequence != self.wanted.sequence {
            Ok(RecordObservation::DifferentSequence {
                observed: self.reference.sequence,
            })
        } else {
            ensure!(
                self.reference.raw == self.wanted.raw,
                ParseFailure::FullReferenceMismatch
            );
            Ok(RecordObservation::SameReference)
        }
    }
}

/// No public constructor or serialization: encoding is selected only for one
/// live probe. A record cannot choose its own permissive interpretation.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum OutputEncoding {
    FullReference,
    OrdinalOnly,
}

fn calibrate_anchor(record: &ValidatedRecord) -> Result<OutputEncoding> {
    record.require_anchor()?;
    let encoding = if record.prefix == record.wanted.raw {
        OutputEncoding::FullReference
    } else if record.prefix == record.wanted.record {
        OutputEncoding::OrdinalOnly
    } else {
        return Err(ParseFailure::OutputEncodingMismatch.into());
    };
    record.require_encoding(encoding)?;
    Ok(encoding)
}

/// References keep both handles alive; this value never escapes probe_volume.
/// Its only production constructor obtains anchor identity and metadata through
/// these handles. It is neither a persisted layout hint nor repair authority.
struct CalibratedProbe<'a> {
    device: &'a crate::util::OwnedHandle,
    anchor: &'a crate::util::OwnedHandle,
    anchor_id: FileId,
    volume: VolumeEvidence,
    encoding: OutputEncoding,
}

impl<'a> CalibratedProbe<'a> {
    fn new(
        device: &'a crate::util::OwnedHandle,
        anchor: &'a crate::util::OwnedHandle,
        volume: VolumeEvidence,
    ) -> Result<Self> {
        let anchor_id = crate::path_id::file_id_from_handle(anchor.raw())?;
        let reference = Reference::from_id(&anchor_id)?;
        let bytes = query_record(device.raw(), &volume, reference)
            .context(ProbeStep("query_live_anchor"))?;
        let record = ValidatedRecord::parse(&anchor_id, &volume, &bytes)
            .context(ProbeStep("parse_live_anchor"))?;
        let encoding = calibrate_anchor(&record).context(ProbeStep("calibrate_live_anchor"))?;
        Ok(Self {
            device,
            anchor,
            anchor_id,
            volume,
            encoding,
        })
    }

    fn observe(&self, expected: &FileId) -> Result<RecordObservation> {
        let wanted = Reference::from_id(expected)?;
        let bytes = query_record(self.device.raw(), &self.volume, wanted)
            .context(ProbeStep("query_selected_record"))?;
        let record = ValidatedRecord::parse(expected, &self.volume, &bytes)
            .context(ProbeStep("parse_selected_record"))?;
        record
            .require_encoding(self.encoding)
            .context(ProbeStep("validate_selected_encoding"))?;
        record.observation(self.encoding)
    }

    fn recheck(&self) -> Result<()> {
        let reference = Reference::from_id(&self.anchor_id)?;
        let bytes = query_record(self.device.raw(), &self.volume, reference)
            .context(ProbeStep("query_anchor_recheck"))?;
        let record = ValidatedRecord::parse(&self.anchor_id, &self.volume, &bytes)
            .context(ProbeStep("parse_anchor_recheck"))?;
        record
            .require_encoding(self.encoding)
            .context(ProbeStep("validate_anchor_encoding"))?;
        record
            .require_anchor()
            .context(ProbeStep("validate_anchor_identity"))?;
        let after = parse_volume_data(
            &self.anchor_id,
            &query(self.device.raw(), FSCTL_GET_NTFS_VOLUME_DATA, &[], 256)
                .context(ProbeStep("query_volume_recheck"))?,
        )
        .context(ProbeStep("parse_volume_recheck"))?;
        ensure!(
            after == self.volume
                && crate::path_id::file_id_from_handle(self.anchor.raw())? == self.anchor_id,
            "NTFS volume/anchor changed during evidence probe"
        );
        Ok(())
    }
}

fn query_record(
    device: windows::Win32::Foundation::HANDLE,
    volume: &VolumeEvidence,
    reference: Reference,
) -> Result<Vec<u8>> {
    query(
        device,
        FSCTL_GET_NTFS_FILE_RECORD,
        &reference.record.to_le_bytes(),
        volume.bytes_per_record as usize + 15,
    )
}

fn validate_volume_root(root: &str) -> Result<()> {
    let guid = root
        .strip_prefix(r"\\?\Volume{")
        .and_then(|rest| rest.strip_suffix(r"}\"))
        .context("NTFS evidence probe requires an exact volume GUID root")?;
    ensure!(
        guid.len() == 36
            && guid.bytes().enumerate().all(|(index, value)| {
                if [8, 13, 18, 23].contains(&index) {
                    value == b'-'
                } else {
                    value.is_ascii_hexdigit()
                }
            }),
        "invalid volume GUID root"
    );
    Ok(())
}

/// Five bounded read-only FSCTL calls on one explicitly selected mounted volume:
/// volume geometry, pinned live-root calibration, requested ordinal, live-root
/// recheck, geometry recheck. Opens request no write access. No retries, drive scan, privilege
/// adjustment, elevation, raw sector read, returned filename or recovery action.
///
/// The retained live anchor must certify one exact output representation and
/// reproduce it after the selected query. The public parser remains strict.
/// Privileged qualification of accepted observations is still required.
/// Memory/query count are bounded; synchronous Windows IO has no wall-time bound.
pub fn probe_volume(root: &str, expected: &FileId) -> Result<RecordObservation> {
    use crate::util::{OwnedHandle, pcwstr, wstr};
    use windows::Win32::Foundation::GENERIC_READ;
    use windows::Win32::Storage::FileSystem::{
        CreateFileW, FILE_FLAG_BACKUP_SEMANTICS, FILE_FLAG_OPEN_REPARSE_POINT,
        FILE_FLAGS_AND_ATTRIBUTES, FILE_SHARE_READ, FILE_SHARE_WRITE, GetDriveTypeW, OPEN_EXISTING,
    };

    validate_volume_root(root)?;
    Reference::from_id(expected)?;
    let root_wide = wstr(root);
    ensure!(
        unsafe { GetDriveTypeW(pcwstr(&root_wide)) } == 3,
        "NTFS evidence requires a fixed local volume"
    );
    let anchor = OwnedHandle(
        unsafe {
            CreateFileW(
                pcwstr(&root_wide),
                1,
                FILE_SHARE_READ | FILE_SHARE_WRITE,
                None,
                OPEN_EXISTING,
                FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
                None,
            )
        }
        .context("open live volume-root identity anchor")?,
    );
    let anchor_id = crate::path_id::file_id_from_handle(anchor.raw())?;
    ensure!(
        anchor_id.volume_serial == expected.volume_serial,
        "mounted volume root differs from recorded volume"
    );
    let device_wide = wstr(root.trim_end_matches('\\'));
    let device = OwnedHandle(
        unsafe {
            CreateFileW(
                pcwstr(&device_wide),
                GENERIC_READ.0,
                FILE_SHARE_READ | FILE_SHARE_WRITE,
                None,
                OPEN_EXISTING,
                FILE_FLAGS_AND_ATTRIBUTES(0),
                None,
            )
        }
        .context(
            "open selected NTFS volume read-only; explicit administrative access may be required",
        )?,
    );
    let volume = parse_volume_data(
        expected,
        &query(device.raw(), FSCTL_GET_NTFS_VOLUME_DATA, &[], 256)
            .context(ProbeStep("query_volume_geometry"))?,
    )
    .context(ProbeStep("parse_volume_geometry"))?;
    let calibration = CalibratedProbe::new(&device, &anchor, volume)?;
    let observation = calibration.observe(expected)?;
    calibration.recheck()?;
    Ok(observation)
}

fn query(
    handle: windows::Win32::Foundation::HANDLE,
    code: u32,
    input: &[u8],
    capacity: usize,
) -> Result<Vec<u8>> {
    use windows::Win32::System::IO::DeviceIoControl;
    ensure!(
        capacity <= MAX_RECORD_BYTES + 15,
        "NTFS query buffer exceeds cap"
    );
    let mut output = vec![0u8; capacity];
    let mut returned = 0;
    unsafe {
        DeviceIoControl(
            handle,
            code,
            if input.is_empty() {
                None
            } else {
                Some(input.as_ptr().cast())
            },
            input.len() as u32,
            Some(output.as_mut_ptr().cast()),
            output.len() as u32,
            Some(&mut returned),
            None,
        )
    }
    .with_context(|| format!("read-only NTFS query {code:#x} failed; no absence evidence"))?;
    ensure!(
        returned as usize <= output.len(),
        "NTFS driver returned an oversized buffer length"
    );
    output.truncate(returned as usize);
    Ok(output)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn id(record: u64, sequence: u16) -> FileId {
        let mut id128 = [0; 16];
        id128[..8].copy_from_slice(&(record | (u64::from(sequence) << 48)).to_le_bytes());
        FileId {
            volume_serial: 0xFEDC_BA98_7654_3210,
            id128,
        }
    }
    fn volume_bytes() -> Vec<u8> {
        let mut data = vec![0; 104];
        data[0..8].copy_from_slice(&id(50, 7).volume_serial.to_le_bytes());
        data[8..16].copy_from_slice(&1_000_000u64.to_le_bytes());
        data[40..44].copy_from_slice(&512u32.to_le_bytes());
        data[44..48].copy_from_slice(&4096u32.to_le_bytes());
        data[48..52].copy_from_slice(&1024u32.to_le_bytes());
        data[56..64].copy_from_slice(&(1024 * 1000u64).to_le_bytes());
        data[96..100].copy_from_slice(&8u32.to_le_bytes());
        data[100..102].copy_from_slice(&3u16.to_le_bytes());
        data[102..104].copy_from_slice(&1u16.to_le_bytes());
        data
    }
    fn record_bytes(record: u64, sequence: u16) -> Vec<u8> {
        let mut data = vec![0; 12 + 1024];
        data[..8].copy_from_slice(&id(record, sequence).id128[..8]);
        data[8..12].copy_from_slice(&1024u32.to_le_bytes());
        data[12..16].copy_from_slice(b"FILE");
        data[16..18].copy_from_slice(&48u16.to_le_bytes());
        data[18..20].copy_from_slice(&3u16.to_le_bytes());
        data[28..30].copy_from_slice(&sequence.to_le_bytes());
        data[32..34].copy_from_slice(&56u16.to_le_bytes());
        data[34..36].copy_from_slice(&1u16.to_le_bytes());
        data
    }
    fn parse(data: &[u8]) -> Result<RecordObservation> {
        let expected = id(50, 7);
        parse_file_record(
            &expected,
            &parse_volume_data(&expected, &volume_bytes())?,
            data,
        )
    }

    #[test]
    fn ntfs_evidence_distinguishes_full_identity_reuse_and_lower_record() {
        assert_eq!(
            parse(&record_bytes(50, 7)).unwrap(),
            RecordObservation::SameReference
        );
        assert_eq!(
            parse(&record_bytes(50, 8)).unwrap(),
            RecordObservation::DifferentSequence { observed: 8 }
        );
        assert_eq!(
            parse(&record_bytes(49, 42)).unwrap(),
            RecordObservation::LowerInUseRecord {
                record: 49,
                sequence: 42
            }
        );
        // Sequence is circular: do not compare raw64-bit magnitudes or assume
        // that a numerically smaller sequence cannot represent reuse.
        assert_eq!(
            parse(&record_bytes(50, 1)).unwrap(),
            RecordObservation::DifferentSequence { observed: 1 }
        );
        assert!(parse(&record_bytes(51, 1)).is_err());
        assert!(parse(&record_bytes(50, 0)).is_err());
    }

    #[test]
    fn ntfs_evidence_rejects_lossy_ids_and_mismatched_volumes() {
        let expected = id(50, 7);
        let mut extended = expected;
        extended.id128[8] = 1;
        assert!(parse_volume_data(&extended, &volume_bytes()).is_err());
        let mut other = expected;
        other.volume_serial ^= 1u64 << 63;
        assert!(parse_volume_data(&other, &volume_bytes()).is_err());
        let volume = parse_volume_data(&expected, &volume_bytes()).unwrap();
        assert!(parse_file_record(&other, &volume, &record_bytes(50, 7)).is_err());
        assert!(parse_file_record(&id(1000, 7), &volume, &record_bytes(49, 7)).is_err());
    }

    #[test]
    fn ntfs_evidence_preserves_all_record_and_sequence_bits() {
        let ordinal = 0x1234_5678_9abc;
        let expected = id(ordinal, 0xfedc);
        let mut data = volume_bytes();
        data[56..64].copy_from_slice(&((ordinal + 1) * 1024).to_le_bytes());
        let volume = parse_volume_data(&expected, &data).unwrap();
        assert_eq!(
            parse_file_record(&expected, &volume, &record_bytes(ordinal, 0xfedc)).unwrap(),
            RecordObservation::SameReference
        );
        assert_eq!(
            parse_file_record(&expected, &volume, &record_bytes(ordinal, 1)).unwrap(),
            RecordObservation::DifferentSequence { observed: 1 }
        );
        assert_eq!(
            parse_file_record(&expected, &volume, &record_bytes(ordinal - 1, 0xffff)).unwrap(),
            RecordObservation::LowerInUseRecord {
                record: ordinal - 1,
                sequence: 0xffff
            }
        );
    }

    #[test]
    fn ntfs_evidence_requires_supported_version_geometry_and_complete_bytes() {
        for length in 0..104 {
            assert!(parse_volume_data(&id(50, 7), &volume_bytes()[..length]).is_err());
        }
        for (offset, replacement) in [
            (96, 7u32),
            (96, 9),
            (100, 4),
            (100, (2 << 16) | 3),
            (40, 0),
            (40, 513),
            (44, 256),
            (48, 0),
            (48, 131072),
        ] {
            let mut data = volume_bytes();
            data[offset..offset + 4].copy_from_slice(&replacement.to_le_bytes());
            assert!(
                parse_volume_data(&id(50, 7), &data).is_err(),
                "offset {offset}, value {replacement}"
            );
        }
        for length in [0u64, u64::MAX, 1025] {
            let mut data = volume_bytes();
            data[56..64].copy_from_slice(&length.to_le_bytes());
            assert!(parse_volume_data(&id(50, 7), &data).is_err());
        }
    }

    #[test]
    fn ntfs_evidence_rejects_malformed_headers_instead_of_inferring_absence() {
        let valid = record_bytes(49, 7);
        for length in 0..valid.len() {
            assert!(parse(&valid[..length]).is_err());
        }
        for (offset, value) in [
            (12, b'X'),
            (28, 0),
            (28, 8),
            (34, 0),
            (34, 5),
            (44, 1),
            (16, 0),
            (18, 0),
            (18, 255),
            (32, 0),
            (32, 55),
        ] {
            let mut malformed = valid.clone();
            malformed[offset] = value;
            assert!(parse(&malformed).is_err(), "byte offset {offset}");
        }
        let mut ordinal_only = valid.clone();
        ordinal_only[6..8].fill(0);
        assert!(parse(&ordinal_only).is_err());
        let mut oversized = valid.clone();
        oversized.extend([0; 4]);
        assert!(parse(&oversized).is_err());
        let mut padding = valid.clone();
        padding.extend([0; 3]);
        assert!(parse(&padding).is_ok());
        padding[valid.len()] = 1;
        assert!(parse(&padding).is_err());
    }

    fn encoded_record(record: u64, sequence: u16, encoding: OutputEncoding) -> Vec<u8> {
        let mut data = record_bytes(record, sequence);
        if encoding == OutputEncoding::OrdinalOnly {
            data[6..8].fill(0);
        }
        data
    }

    #[test]
    fn ntfs_evidence_private_calibration_preserves_all_identity_bits_and_observations() {
        for encoding in [OutputEncoding::FullReference, OutputEncoding::OrdinalOnly] {
            let anchor_id = id(5, 9);
            let volume = parse_volume_data(&anchor_id, &volume_bytes()).unwrap();
            let anchor =
                ValidatedRecord::parse(&anchor_id, &volume, &encoded_record(5, 9, encoding))
                    .unwrap();
            assert_eq!(calibrate_anchor(&anchor).unwrap(), encoding);
            for (ordinal, sequence, expected) in [
                (50, 7, RecordObservation::SameReference),
                (50, 8, RecordObservation::DifferentSequence { observed: 8 }),
                (50, 1, RecordObservation::DifferentSequence { observed: 1 }),
                (
                    49,
                    42,
                    RecordObservation::LowerInUseRecord {
                        record: 49,
                        sequence: 42,
                    },
                ),
            ] {
                let record = ValidatedRecord::parse(
                    &id(50, 7),
                    &volume,
                    &encoded_record(ordinal, sequence, encoding),
                )
                .unwrap();
                assert_eq!(record.observation(encoding).unwrap(), expected);
            }
            let ordinal = 0x1234_5678_9abc;
            let expected = id(ordinal, 0xfedc);
            let mut geometry = volume_bytes();
            geometry[56..64].copy_from_slice(&((ordinal + 1) * 1024).to_le_bytes());
            let volume = parse_volume_data(&expected, &geometry).unwrap();
            for sequence in [0xfedc, 1] {
                let data = encoded_record(ordinal, sequence, encoding);
                let record = ValidatedRecord::parse(&expected, &volume, &data).unwrap();
                let observation = record.observation(encoding).unwrap();
                assert_eq!(
                    observation,
                    if sequence == 0xfedc {
                        RecordObservation::SameReference
                    } else {
                        RecordObservation::DifferentSequence { observed: 1 }
                    }
                );
                assert_eq!(
                    parse_file_record(&expected, &volume, &data).is_ok(),
                    encoding == OutputEncoding::FullReference
                );
            }
        }
        assert!(Reference::from_raw(50).is_err());
        assert!(Reference::from_id(&id(50, 0)).is_err());
    }

    #[test]
    fn ntfs_evidence_calibration_refuses_changed_anchor_unknown_and_mixed_encodings() {
        let anchor_id = id(50, 7);
        let volume = parse_volume_data(&anchor_id, &volume_bytes()).unwrap();
        for encoding in [OutputEncoding::FullReference, OutputEncoding::OrdinalOnly] {
            let anchor =
                ValidatedRecord::parse(&anchor_id, &volume, &encoded_record(50, 7, encoding))
                    .unwrap();
            let admitted = calibrate_anchor(&anchor).unwrap();
            for (ordinal, sequence) in [(49, 7), (50, 8)] {
                let changed = ValidatedRecord::parse(
                    &anchor_id,
                    &volume,
                    &encoded_record(ordinal, sequence, encoding),
                )
                .unwrap();
                assert!(calibrate_anchor(&changed).is_err());
                assert!(changed.require_anchor().is_err()); // second-anchor recheck
            }
            assert!(
                ValidatedRecord::parse(&anchor_id, &volume, &encoded_record(51, 7, encoding))
                    .is_err()
            );
            let other = if encoding == OutputEncoding::FullReference {
                OutputEncoding::OrdinalOnly
            } else {
                OutputEncoding::FullReference
            };
            let mixed =
                ValidatedRecord::parse(&anchor_id, &volume, &encoded_record(50, 7, other)).unwrap();
            assert!(mixed.observation(admitted).is_err()); // selected response
            assert!(mixed.require_encoding(admitted).is_err()); // anchor recheck
        }
        let mut unknown = record_bytes(50, 7);
        unknown[..8].copy_from_slice(&((8u64 << 48) | 50).to_le_bytes());
        let unknown = ValidatedRecord::parse(&anchor_id, &volume, &unknown).unwrap();
        assert!(calibrate_anchor(&unknown).is_err());
        for encoding in [OutputEncoding::FullReference, OutputEncoding::OrdinalOnly] {
            assert!(unknown.observation(encoding).is_err());
        }
        let mut wrong_volume = volume;
        wrong_volume.serial ^= 1;
        assert!(ValidatedRecord::parse(&anchor_id, &wrong_volume, &record_bytes(50, 7)).is_err());
        let mut lossy = anchor_id;
        lossy.id128[8] = 1;
        assert!(ValidatedRecord::parse(&lossy, &volume, &record_bytes(50, 7)).is_err());
    }

    #[test]
    fn ntfs_evidence_calibrated_records_keep_structural_and_bounds_refusals() {
        let expected = id(50, 7);
        let volume = parse_volume_data(&expected, &volume_bytes()).unwrap();
        for encoding in [OutputEncoding::FullReference, OutputEncoding::OrdinalOnly] {
            let valid = encoded_record(50, 7, encoding);
            for length in 0..valid.len() {
                assert!(ValidatedRecord::parse(&expected, &volume, &valid[..length]).is_err());
            }
            for (offset, replacement) in [
                (8, 1),
                (12, b'X'),
                (28, 0),
                (34, 0),
                (34, 5),
                (44, 1),
                (16, 0),
                (18, 0),
                (18, 255),
                (32, 0),
                (32, 55),
            ] {
                let mut malformed = valid.clone();
                malformed[offset] = replacement;
                assert!(ValidatedRecord::parse(&expected, &volume, &malformed).is_err());
            }
            let mut oversized = valid.clone();
            oversized.extend([0; 4]);
            assert!(ValidatedRecord::parse(&expected, &volume, &oversized).is_err());
            let mut padding = valid;
            padding.extend([0; 3]);
            assert!(ValidatedRecord::parse(&expected, &volume, &padding).is_ok());
            *padding.last_mut().unwrap() = 1;
            assert!(ValidatedRecord::parse(&expected, &volume, &padding).is_err());
            assert!(
                ValidatedRecord::parse(&id(1000, 7), &volume, &encoded_record(49, 7, encoding))
                    .is_err()
            );
        }
    }

    #[test]
    fn ntfs_evidence_fixed_reasons_identify_rejections_without_relaxing_parser() {
        let reason = |error: anyhow::Error| {
            *error
                .downcast_ref::<ParseFailure>()
                .expect("typed parser reason")
        };
        assert_eq!(
            reason(Reference::from_raw(50).unwrap_err()),
            ParseFailure::ReferenceSequenceZero
        );
        let mut lossy = id(50, 7);
        lossy.id128[8] = 1;
        assert_eq!(
            reason(Reference::from_id(&lossy).unwrap_err()),
            ParseFailure::IdentityNotRepresentable
        );
        assert_eq!(
            reason(bytes::<1>(&[], usize::MAX).unwrap_err()),
            ParseFailure::MetadataOffsetOverflow
        );
        assert_eq!(
            reason(parse(&[]).unwrap_err()),
            ParseFailure::MetadataTruncated
        );
        let volume = parse_volume_data(&id(50, 7), &volume_bytes()).unwrap();
        let mut wrong_volume = id(50, 7);
        wrong_volume.volume_serial ^= 1;
        assert_eq!(
            reason(parse_file_record(&wrong_volume, &volume, &record_bytes(50, 7)).unwrap_err()),
            ParseFailure::FileRecordVolumeMismatch
        );
        assert_eq!(
            reason(parse_file_record(&id(1000, 7), &volume, &record_bytes(49, 7)).unwrap_err()),
            ParseFailure::RequestedRecordOutsideMft
        );
        assert_eq!(
            reason(parse(&record_bytes(51, 7)).unwrap_err()),
            ParseFailure::ReturnedHigherOrdinal
        );
        for (expected, offset, replacement) in [
            (ParseFailure::RecordLengthMismatch, 8, 1),
            (ParseFailure::RecordSignature, 12, b'X'),
            (ParseFailure::HeaderSequenceMismatch, 28, 8),
            (ParseFailure::RecordFlags, 34, 0),
            (ParseFailure::ExtensionRecord, 44, 1),
            (ParseFailure::FirstAttributeOffset, 32, 55),
            (ParseFailure::UpdateSequenceHeader, 16, 0),
            (ParseFailure::UpdateSequenceOverlap, 18, 255),
        ] {
            let mut malformed = record_bytes(50, 7);
            malformed[offset] = replacement;
            assert_eq!(reason(parse(&malformed).unwrap_err()), expected);
        }
        let mut oversized = record_bytes(50, 7);
        oversized.extend([0; 4]);
        assert_eq!(
            reason(parse(&oversized).unwrap_err()),
            ParseFailure::ResponseLength
        );
        let mut padding = record_bytes(50, 7);
        padding.push(1);
        assert_eq!(
            reason(parse(&padding).unwrap_err()),
            ParseFailure::ResponsePadding
        );

        let mut ordinal_only = record_bytes(50, 7);
        ordinal_only[6..8].fill(0);
        let error = parse(&ordinal_only).unwrap_err();
        assert_eq!(
            error.downcast_ref::<ParseFailure>(),
            Some(&ParseFailure::ReferenceSequenceZero)
        );
        assert_eq!(
            error.downcast_ref::<ReferenceInput>(),
            Some(&ReferenceInput::ReturnedReference)
        );
        let error = Reference::from_id(&id(50, 0)).unwrap_err();
        assert_eq!(
            error.downcast_ref::<ReferenceInput>(),
            Some(&ReferenceInput::FileId)
        );
    }

    #[test]
    fn ntfs_evidence_volume_selection_cannot_escape_to_a_file_or_device() {
        assert!(validate_volume_root(r"\\?\Volume{12345678-1234-5678-90ab-123456789abc}\").is_ok());
        for invalid in [
            r"C:\",
            r"\\.\C:",
            r"\\?\Volume{12345678-1234-5678-90ab-123456789abc}",
            r"\\?\Volume{12345678-1234-5678-90ab-123456789abc}\other",
            r"\\?\Volume{12345678-1234-5678-90ab-123456789abg}\",
            r"\\server\share\",
        ] {
            assert!(validate_volume_root(invalid).is_err());
        }
    }

    /// Explicitly opt-in: creates one ordinary disposable file and reads only
    /// volume metadata. Never invokes the helper or touches installed state.
    #[test]
    #[ignore = "explicit read-only volume qualification, normally denied without elevation"]
    fn ntfs_evidence_disposable_live_probe() -> Result<()> {
        use crate::util::{pcwstr, wstr};
        use std::os::windows::io::AsRawHandle;
        use windows::Win32::Foundation::{ERROR_ACCESS_DENIED, HANDLE};
        use windows::Win32::Storage::FileSystem::{
            GetVolumeNameForVolumeMountPointW, GetVolumePathNameW,
        };

        struct Fixture(std::path::PathBuf, Option<std::fs::File>);
        impl Drop for Fixture {
            fn drop(&mut self) {
                self.1.take();
                let _ = std::fs::remove_file(&self.0);
            }
        }
        let unique = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)?
            .as_nanos();
        let fixture_path = std::env::temp_dir().join(format!(
            "alysis-ntfs-evidence-{}-{unique}.tmp",
            std::process::id()
        ));
        let file = std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&fixture_path)?;
        // Register cleanup only after exclusive creation succeeded.
        let mut fixture = Fixture(fixture_path, Some(file));
        let expected = crate::path_id::file_id_from_handle(HANDLE(
            fixture.1.as_ref().unwrap().as_raw_handle(),
        ))?;
        let path = wstr(fixture.0.to_str().context("non-Unicode test path")?);
        let mut mount = [0u16; 1024];
        let mut guid = [0u16; 64];
        unsafe {
            GetVolumePathNameW(pcwstr(&path), &mut mount)?;
            GetVolumeNameForVolumeMountPointW(pcwstr(&mount), &mut guid)?;
        }
        let end = guid
            .iter()
            .position(|unit| *unit == 0)
            .context("unterminated volume GUID")?;
        let root = String::from_utf16(&guid[..end])?;
        match probe_volume(&root, &expected) {
            Ok(observation) => assert_eq!(observation, RecordObservation::SameReference),
            Err(error) => {
                ensure!(
                    error
                        .root_cause()
                        .downcast_ref::<windows::core::Error>()
                        .is_some_and(|code| code.code() == ERROR_ACCESS_DENIED.to_hresult()),
                    "unexpected metadata probe failure: {error:#}"
                );
                eprintln!("read-only NTFS evidence unavailable: {error:#}");
            }
        }
        fixture.1.take();
        std::fs::remove_file(&fixture.0)?;
        Ok(())
    }
}
