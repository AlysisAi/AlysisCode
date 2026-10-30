//! Read-only comparison of the retained original bootstrap and raw account.
//! No PID reopen, impersonation, token duplication or policy modification.
use super::*;
use windows::Win32::Security::*;
const SE_GROUP_LOGON_ID: u32 = 0xc0000000;

#[derive(Clone, Serialize)]
pub(crate) struct Observation {
    diagnostic_only: bool,
    source: Snapshot,
    account: Snapshot,
    shared_logon_sids: Vec<Vec<u8>>,
    originals_unchanged: bool,
}
#[derive(Clone, Serialize)]
struct Snapshot {
    process: (u32, u64),
    user: String,
    session: u32,
    stamp: Stamp,
    logon_groups: Vec<(Vec<u8>, u32)>,
    query_handle_closed: bool,
}

fn sid_extent(
    offset: usize,
    count: usize,
    minimum: usize,
    returned: usize,
) -> Result<std::ops::Range<usize>> {
    ensure!(count <= 15, "excess logon SID subauthorities");
    let end = offset
        .checked_add(8 + count * 4)
        .context("logon SID overflow")?;
    ensure!(
        offset >= minimum && end <= returned,
        "logon SID outside response"
    );
    Ok(offset..end)
}

fn groups(token: HANDLE) -> Result<Vec<(Vec<u8>, u32)>> {
    let mut buffer = [0usize; 8192];
    let mut returned = 0;
    unsafe {
        GetTokenInformation(
            token,
            TokenGroups,
            Some(buffer.as_mut_ptr().cast()),
            std::mem::size_of_val(&buffer) as u32,
            &mut returned,
        )?;
    }
    let returned = returned as usize;
    let offset = std::mem::offset_of!(TOKEN_GROUPS, Groups);
    ensure!(
        (offset..=std::mem::size_of_val(&buffer)).contains(&returned),
        "logon group response extent"
    );
    let count = unsafe { *buffer.as_ptr().cast::<u32>() } as usize;
    let minimum = offset
        .checked_add(
            count
                .checked_mul(size_of::<SID_AND_ATTRIBUTES>())
                .context("logon group overflow")?,
        )
        .context("logon group overflow")?;
    ensure!(
        count <= 4096 && minimum <= returned,
        "logon group array outside response"
    );
    let entries = unsafe {
        std::slice::from_raw_parts(
            buffer
                .as_ptr()
                .cast::<u8>()
                .add(offset)
                .cast::<SID_AND_ATTRIBUTES>(),
            count,
        )
    };
    let mut result = Vec::new();
    for entry in entries {
        if entry.Attributes & SE_GROUP_LOGON_ID != SE_GROUP_LOGON_ID {
            continue;
        }
        ensure!(result.len() < 4, "excess logon groups");
        let base = buffer.as_ptr() as usize;
        let position = (entry.Sid.0 as usize)
            .checked_sub(base)
            .context("logon SID precedes response")?;
        sid_extent(position, 0, minimum, returned)?;
        let count = unsafe { *entry.Sid.0.cast::<u8>().add(1) } as usize;
        let range = sid_extent(position, count, minimum, returned)?;
        ensure!(
            unsafe { IsValidSid(entry.Sid) }.as_bool(),
            "invalid logon SID"
        );
        let bytes =
            unsafe { std::slice::from_raw_parts(entry.Sid.0.cast::<u8>(), range.len()) }.to_vec();
        ensure!(
            !result.iter().any(|(prior, _)| prior == &bytes),
            "duplicate logon SID"
        );
        result.push((bytes, entry.Attributes));
    }
    result.sort_by(|a, b| a.0.cmp(&b.0));
    Ok(result)
}

impl Snapshot {
    fn observe(
        peer: &ExpectedPeer<'_>,
        identity: (u32, u64),
        user: &str,
        session: u32,
    ) -> Result<Self> {
        peer.alive()?;
        peer.token.require_account_runner(user, session)?;
        ensure!(
            peer.pid == identity.0
                && bootstrap::pull::creation_time(peer.process.raw())? == identity.1,
            "logon observation differs from original process"
        );
        let stamp = peer.token.fixture_stamp(peer.process.raw())?;
        let mut raw = HANDLE::default();
        unsafe {
            OpenProcessToken(peer.process.raw(), TOKEN_QUERY, &mut raw)?;
        }
        let token = OwnedHandle(raw);
        noninheritable(raw)?;
        let mut stats = TOKEN_STATISTICS::default();
        let mut returned = 0;
        unsafe {
            GetTokenInformation(
                raw,
                TokenStatistics,
                Some((&mut stats as *mut TOKEN_STATISTICS).cast()),
                size_of::<TOKEN_STATISTICS>() as u32,
                &mut returned,
            )?;
        }
        let pair = |id: windows::Win32::Foundation::LUID| [id.LowPart, id.HighPart as u32];
        ensure!(
            returned as usize == size_of::<TOKEN_STATISTICS>()
                && stats.TokenType == TokenPrimary
                && (
                    pair(stats.TokenId),
                    pair(stats.AuthenticationId),
                    pair(stats.ModifiedId)
                ) == stamp,
            "logon query token differs from retained original"
        );
        let logon_groups = groups(raw)?;
        unsafe {
            CloseHandle(token.into_raw())?;
        }
        ensure!(
            peer.token.fixture_stamp(peer.process.raw())? == stamp,
            "logon query changed original token"
        );
        peer.alive()?;
        Ok(Self {
            process: identity,
            user: user.into(),
            session,
            stamp,
            logon_groups,
            query_handle_closed: true,
        })
    }
}

fn intersection(source: &[(Vec<u8>, u32)], account: &[(Vec<u8>, u32)]) -> Vec<Vec<u8>> {
    source
        .iter()
        .filter(|(sid, _)| account.iter().any(|(other, _)| sid == other))
        .map(|(sid, _)| sid.clone())
        .collect()
}

pub(super) fn observe(
    source: &ExpectedPeer<'_>,
    account: &ExpectedPeer<'_>,
    request: &Request,
) -> Result<Observation> {
    crate::account_impersonation::require_process_identity()?;
    binding(request)?;
    let selected = &request.birth.selection;
    ensure!(
        selected.bootstrap_pid != request.pid
            && selected.account.owner_sid != selected.account.account_sid,
        "logon comparison aliases source and account"
    );
    let source_snapshot = Snapshot::observe(
        source,
        (selected.bootstrap_pid, selected.bootstrap_created),
        &selected.account.owner_sid,
        selected.session,
    )?;
    let account_snapshot = Snapshot::observe(
        account,
        (request.pid, request.created),
        &selected.account.account_sid,
        selected.session,
    )?;
    ensure!(
        source.token.fixture_stamp(source.process.raw())? == source_snapshot.stamp
            && account.token.fixture_stamp(account.process.raw())? == account_snapshot.stamp,
        "logon pair changed during observation"
    );
    source.alive()?;
    account.alive()?;
    Ok(Observation {
        diagnostic_only: true,
        shared_logon_sids: intersection(
            &source_snapshot.logon_groups,
            &account_snapshot.logon_groups,
        ),
        source: source_snapshot,
        account: account_snapshot,
        originals_unchanged: true,
    })
}

#[test]
fn station_logon_comparison_uses_sid_bytes_independently_of_attributes() {
    let source = vec![(vec![1, 2], 0xc0000004), (vec![1, 3], 0xc0000004)];
    let account = vec![(vec![1, 2], 0xc0000010), (vec![1, 4], 0xc0000004)];
    assert_eq!(intersection(&source, &account), vec![vec![1, 2]]);
    assert!(intersection(&source, &[]).is_empty());
    assert!(intersection(&source, &[(vec![2, 1], 0xc0000004)]).is_empty());
}
#[test]
fn station_logon_sid_bounds_reject_array_alias_truncation_and_overflow() {
    assert_eq!(sid_extent(32, 3, 32, 52).unwrap(), 32..52);
    for (offset, count, minimum, returned) in [
        (31, 3, 32, 52),
        (32, 3, 32, 51),
        (32, 16, 32, 1024),
        (usize::MAX, 3, 32, usize::MAX),
    ] {
        assert!(sid_extent(offset, count, minimum, returned).is_err());
    }
}
