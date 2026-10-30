# NTFS identity evidence: review and integration contract

This module is deliberately separate from baseline recovery. It does not locate
objects, alter ACLs, clear journals, enumerate volumes or acquire privileges.
The public probe accepts only one exact volume GUID root. Its five synchronous
FSCTL queries and memory allocation are bounded; kernel IO duration is not.

The public byte parser accepts only lossless 64-bit references (upper 8 bytes of the
captured 128-bit ID must be zero), the exact 64-bit volume serial, NTFS 3.0/3.1,
bounded power-of-two geometry, complete buffers, an in-use base `FILE` record,
and a nonzero header sequence matching the full returned reference. Unknown
flags, extension records, ordinal-only returned references and inconsistent
headers remain errors. An out-of-range requested MFT record is also an error,
not evidence of deletion. This strict subset may reject valid Windows layouts;
it must not be relaxed without documented semantics and live qualification.

The update-sequence array receives conservative bounds checks only. The module
does not interpret its ambiguous byte-versus-USHORT-count field, apply fixups,
or claim to validate raw-sector integrity. All inspected identity fields precede
the first 512-byte transfer trailer. Inputs to a production decision must come
from successful FSCTL calls on the retained volume handle, never from user-
provided bytes, a journal-provided MFT image, raw disk reads or a saved response.

## Outcomes are observations, not repair authority

- Same reference: no absence evidence. A 16-bit sequence can wrap, so equality
  alone is not stronger proof of identity than the existing Windows file ID.
- Different sequence at the same ordinal: a different generation was observed.
- Lower in-use record: the documented downward query did not return the wanted
  ordinal as in-use. The returned record is unrelated; never modify it.
- Any API/parsing/volume/representation failure: unknown, with journal retained.

The observation probe has a separate private calibration, bound to retained
volume and live-root handles for one call. After complete structural validation,
the anchor's nonzero header sequence and full ordinal must exactly reproduce
its captured handle identity. Its output prefix must be either that full
reference or exactly its zero-high-bits ordinal. This selects one closed encoding;
the selected record cannot choose another encoding or fall back after a mismatch.
The public byte parser and ordinary FileId decoding remain full-reference-only.

The probe performs five bounded FSCTL calls: geometry, live anchor, selected
record, second live anchor, and geometry recheck. Before returning an observation,
the second anchor must reproduce the same full identity and encoding, geometry
must remain equal, and the retained anchor handle must still identify the same
object. No returned filenames or raw MFT data are retained. This addresses
volume/representation confusion but is not an atomic filesystem snapshot, a
defense against hostile media cloning, or an assurance that a file's 16-bit
sequence cannot eventually be reused.

Microsoft documents the downward ordinal lookup and record-header sequence
semantics, but the output-structure documentation does not explicitly promise
sequence bits in the prefix. The private ordinal-only convention is therefore
accepted only through exact live calibration and still requires hosted
qualification. It is not a general reinterpretation of arbitrary zero-sequence
references. Header, geometry, flags, base-record, attribute and conservative USA
checks remain required in both encodings; no raw-sector fixups are applied.

## Required before any baseline integration

1. Independently review the ordinal-query and full-reference contracts. Capture
   privileged read-only results for live, moved, delete-pending, fully deleted
   and naturally reused disposable objects. Confirm NTFS 3.0/3.1 layouts and
   output conventions; the present synthetic tests do not establish those.
2. Use an explicitly authorized privileged repair entry point. Verify the
   owner, journal schema and identity, shared execution lease, broker identity
   and empty containment job before collecting fresh evidence. No automatic
   UAC, elevation or error 87-to-absence conversion is provided here.
3. Retain the selected volume handle and verified identity context through the
   repair decision; the current observation-only API does not provide a sealed
   cleanup capability. Never alter an object returned by a lower-record query.
4. Prove which journal rows can be retired. Absence of a recorded ACL root does
   not prove that recursively granted descendants were deleted: descendants
   may have moved elsewhere. The current root-only recursive grant journal is
   a separate obstacle to authorizing complete cleanup from root absence.
5. On missing permissions, unsupported versions/layouts, volume changes, parse
   failures, identity ambiguity or pending deletion, retain recovery state.
   Privileged availability and power-loss durability require separate tests.

## Current qualification (2026-10-03)

Six synthetic parser/selection tests passed, including truncated buffers,
unsupported layouts, different sequences, lower/higher ordinals, and identities
whose record number exceeds 32 bits and sequence has its high bit set. The
explicit disposable-file test also passed as the ordinary Windows user: opening
the selected raw volume for read returned access denied (5), producing no
absence observation. The fixture was removed with an asserted successful
delete. No privileged MFT response has been accepted or qualified by this work.

An October 4 regression separates delete-pending and unverifiable final deletion
for files and directories, with both ordinary-holder and orphan ledgers. Live
and moved objects resolve, held unlinked objects remain delete-pending, and the
final deleted identity is retained when Windows cannot establish absence. A new
object at the old path keeps its own DACL unchanged.

The separate manual `ntfs_observations` workflow input now selects an ignored
disposable-host test on Windows 2022 and 2025, serialized across the matrix.
It requires a fixed sentinel and exact hosted/manual/run identity before files
or privileged queries, plus an elevated token before its fixture is created.
These are accidental-execution guards, not cryptographic host attestation.
Only private synthetic file/directory identities and their selected volume are
queried. Live, moved, held-unlinked and fully deleted controls must pass; at most
32 tiny create/delete attempts can additionally observe natural record reuse.
No reuse is claimed when that bounded search finds none.

The report retains fixed stage/category labels and numeric API diagnostics,
never filenames, raw MFT bytes or record numbers. Every API/parser failure fails
qualification. Completion requires successful nonrecursive fixture cleanup and
fresh run-bound evidence; zero selected tests cannot pass. The first privileged run (37232155632, commit f0333a9c) failed on both
Windows 2022 and 2025 at `file_live` / `parse_live_anchor`, before accepting any
observation. Both private fixtures were preserved. The strict predicates remain
unchanged; typed fixed rejection reasons and reference-input labels now identify
the next refusal without serializing error text or returned metadata. Nine
harmless tests pass with both privileged probes ignored locally. This adds no repair
authority, journal deletion, permission change to an existing object, or
baseline recovery integration.

The diagnostic rerun (37233912261, commit 5936a09f) confirmed on both Windows
2022 and 2025 that the rejected reference was the returned prefix with zero
sequence bits. The private calibration change addresses that specific
representation gate; it does not assume the remaining structure is valid.
Synthetic controls cover both encodings, mixed/unknown encodings, changed
anchor identities, high ordinal/sequence bits, malformed headers and truncated
buffers. The subsequent hosted qualification is recorded below. No baseline
recovery path, ACL mutation, journal retirement or repair authority is added.

The subsequent exact-source hosted run 37235846922 at c375424c passed on both
Windows 2022 and 2025. Each made ten observation probes (five bounded FSCTL calls
per probe): live, moved and held-delete-pending files/directories matched their
original references; final-deleted identities returned lower in-use records.
Natural reuse was observed on the first bounded attempt on both hosts: the new
identity matched while the old identity reported a different sequence. Private
fixture cleanup succeeded. The reports retain only fixed observation categories,
not raw MFT data. This qualifies these observations on the tested platforms,
without authorizing any existing recovery row or journal to be retired.

## Primary sources

- [FSCTL lookup semantics](https://learn.microsoft.com/en-us/windows/win32/api/winioctl/ni-winioctl-fsctl_get_ntfs_file_record)
- [Output layout](https://learn.microsoft.com/en-us/windows/win32/api/winioctl/ns-winioctl-ntfs_file_record_output_buffer)
- [Volume serial, geometry and version](https://learn.microsoft.com/en-us/windows/win32/api/winioctl/ns-winioctl-ntfs_volume_data_buffer)
- [Record header and sequence](https://learn.microsoft.com/en-us/windows/win32/devnotes/file-record-segment-header)
- [Full reference and circular sequence](https://learn.microsoft.com/en-us/windows/win32/devnotes/mft-segment-reference)
- [Multisector header and transfer fixups](https://learn.microsoft.com/en-us/windows/win32/devnotes/multi-sector-header)
