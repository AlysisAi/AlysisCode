//! Read-only admission for the fixed filtered-primary Schannel comparison.
use super::*;
type Stamp = ([u32; 2], [u32; 2], [u32; 2]);

fn fixed<T: Default>(token: HANDLE, class: TOKEN_INFORMATION_CLASS) -> Result<T> {
    super::fixed(token, class)
        .with_context(|| format!("filtered primary scalar token class {}", class.0))
}
fn information(token: HANDLE, class: TOKEN_INFORMATION_CLASS) -> Result<Buffer> {
    Buffer::read(token, class)
        .with_context(|| format!("filtered primary buffered token class {}", class.0))
}

struct Observation {
    user: String,
    primary: bool,
    appcontainer: u32,
    filtered: u32,
    elevated: u32,
    ui_access: u32,
    session: u32,
    auth: [u32; 2],
    integrity: String,
    restrictions: u32,
    capabilities: u32,
    groups: Vec<(String, u32)>,
    privileges: Vec<LUID>,
}
impl Observation {
    fn require(&self, account: &str, session: u32, auth: [u32; 2], traverse: LUID) -> Result<()> {
        self.require_integrity(account, session, auth, traverse, "S-1-16-8192")
    }
    fn require_integrity(
        &self,
        account: &str,
        session: u32,
        auth: [u32; 2],
        traverse: LUID,
        integrity: &str,
    ) -> Result<()> {
        ensure!(
            matches!(integrity, "S-1-16-4096" | "S-1-16-8192"),
            "unsupported fixed comparison integrity"
        );
        ensure!(
            self.user == account && self.session == session && self.auth == auth,
            "filtered primary account, session or logon differs"
        );
        ensure!(
            self.primary
                && self.appcontainer == 0
                && self.filtered == 1
                && self.elevated == 0
                && self.ui_access == 0
                && self.integrity == integrity
                && self.restrictions == 0
                && self.capabilities == 0,
            "fixed comparison requires the exact filtered integrity without AppContainer, capabilities or restricting SIDs"
        );
        for (sid, flags) in &self.groups {
            if sid == "S-1-5-32-544"
                || flags & 0xc0000000 == 0xc0000000
                || sid.starts_with("S-1-5-5-")
            {
                ensure!(
                    flags & DENY_ONLY != 0 && flags & ENABLED == 0,
                    "filtered primary administrative or logon SID is not disabled"
                );
            }
        }
        ensure!(
            self.privileges.iter().all(|id| *id == traverse),
            "filtered primary retains an unexpected privilege"
        );
        Ok(())
    }
}

pub(crate) fn require(token: HANDLE, account: &str, session: u32, auth: [u32; 2]) -> Result<Stamp> {
    require_integrity(token, account, session, auth, "S-1-16-8192")
}
pub(crate) fn require_low(
    token: HANDLE,
    account: &str,
    session: u32,
    auth: [u32; 2],
) -> Result<Stamp> {
    require_integrity(token, account, session, auth, "S-1-16-4096")
}
fn require_integrity(
    token: HANDLE,
    account: &str,
    session: u32,
    auth: [u32; 2],
    integrity_sid: &str,
) -> Result<Stamp> {
    crate::account_impersonation::require_process_identity()?;
    crate::account_namespace::verify_noninheritable(token)?;
    crate::sid::LocalPsid::from_string(account)?;
    let before: TOKEN_STATISTICS = fixed(token, TokenStatistics)?;
    let user = information(token, TokenUser)?;
    let user = user.sid(
        user.header::<TOKEN_USER>()?.User.Sid,
        size_of::<TOKEN_USER>(),
    )?;
    let mandatory = information(token, TokenIntegrityLevel)?;
    let label = mandatory.header::<TOKEN_MANDATORY_LABEL>()?;
    ensure!(
        label.Label.Attributes & 0x20 != 0,
        "filtered integrity is not mandatory"
    );
    let integrity = mandatory.sid(label.Label.Sid, size_of::<TOKEN_MANDATORY_LABEL>())?;
    let groups = information(token, TokenGroups)?;
    let header = groups.header::<TOKEN_GROUPS>()?;
    let entries = groups.array(header.Groups.as_ptr(), header.GroupCount as usize)?;
    let minimum = std::mem::offset_of!(TOKEN_GROUPS, Groups) + std::mem::size_of_val(entries);
    let groups = entries
        .iter()
        .map(|g| Ok((groups.sid(g.Sid, minimum)?, g.Attributes)))
        .collect::<Result<Vec<_>>>()?;
    let privileges = information(token, TokenPrivileges)?;
    let count = *privileges.header::<u32>()? as usize;
    let pointer = unsafe {
        privileges
            .words
            .as_ptr()
            .cast::<u8>()
            .add(std::mem::offset_of!(TOKEN_PRIVILEGES, Privileges))
            .cast::<LUID_AND_ATTRIBUTES>()
    };
    let privileges = privileges
        .array(pointer, count)?
        .iter()
        .map(|p| p.Luid)
        .collect();
    let luid = |id: LUID| [id.LowPart, id.HighPart as u32];
    // Native token output still needs word-aligned storage when the returned
    // value is one byte. A stack u8 passed to GetTokenInformation is not enough.
    let restrictions_flag = information(token, TokenHasRestrictions)?;
    ensure!(
        restrictions_flag.bytes == 1,
        "TokenHasRestrictions extent differs"
    );
    let restrictions_flag = *restrictions_flag.header::<u8>()?;
    ensure!(
        restrictions_flag <= 1,
        "TokenHasRestrictions is not Boolean"
    );
    let observed = Observation {
        user,
        primary: before.TokenType == TokenPrimary,
        appcontainer: fixed(token, TokenIsAppContainer)?,
        // TokenHasRestrictions returns a BOOLEAN, not a DWORD (measured by
        // the existing four-stage observation on the admitted Windows host).
        filtered: u32::from(restrictions_flag),
        elevated: fixed::<TOKEN_ELEVATION>(token, TokenElevation)?.TokenIsElevated,
        ui_access: fixed(token, TokenUIAccess)?,
        session: fixed(token, TokenSessionId)?,
        auth: luid(before.AuthenticationId),
        integrity,
        restrictions: *information(token, TokenRestrictedSids)?.header::<u32>()?,
        capabilities: *information(token, TokenCapabilities)?.header::<u32>()?,
        groups,
        privileges,
    };
    let mut traverse = LUID::default();
    unsafe {
        LookupPrivilegeValueW(
            None,
            windows::core::w!("SeChangeNotifyPrivilege"),
            &mut traverse,
        )?
    };
    observed.require_integrity(account, session, auth, traverse, integrity_sid)?;
    ensure!(
        same_stamp(&before, &fixed(token, TokenStatistics)?),
        "filtered primary changed during observation"
    );
    Ok((
        luid(before.TokenId),
        luid(before.AuthenticationId),
        luid(before.ModifiedId),
    ))
}

#[test]
fn station_filtered_primary_policy_refuses_raw_lowbox_logon_privilege_and_identity_changes() {
    let account = "S-1-5-21-1-2-3-1003";
    let traverse = LUID {
        LowPart: 23,
        HighPart: 0,
    };
    let valid = || Observation {
        user: account.into(),
        primary: true,
        appcontainer: 0,
        filtered: 1,
        elevated: 0,
        ui_access: 0,
        session: 1,
        auth: [7, 0],
        integrity: "S-1-16-8192".into(),
        restrictions: 0,
        capabilities: 0,
        groups: vec![("S-1-5-5-0-123".into(), 0xc0000010)],
        privileges: vec![traverse],
    };
    valid().require(account, 1, [7, 0], traverse).unwrap();
    assert!(
        valid()
            .require_integrity(account, 1, [7, 0], traverse, "S-1-16-4096")
            .is_err()
    );
    let mut low = valid();
    low.integrity = "S-1-16-4096".into();
    low.require_integrity(account, 1, [7, 0], traverse, "S-1-16-4096")
        .unwrap();
    assert!(low.require(account, 1, [7, 0], traverse).is_err());
    assert!(
        low.require_integrity(account, 1, [7, 0], traverse, "S-1-16-0")
            .is_err()
    );
    let defects: Vec<Box<dyn Fn(&mut Observation)>> = vec![
        Box::new(|p| p.user = "S-1-5-18".into()),
        Box::new(|p| p.primary = false),
        Box::new(|p| p.appcontainer = 1),
        Box::new(|p| p.filtered = 0),
        Box::new(|p| p.elevated = 1),
        Box::new(|p| p.ui_access = 1),
        Box::new(|p| p.session = 2),
        Box::new(|p| p.auth = [8, 0]),
        Box::new(|p| p.integrity = "S-1-16-4096".into()),
        Box::new(|p| p.restrictions = 1),
        Box::new(|p| p.capabilities = 1),
        Box::new(|p| p.groups[0].1 |= ENABLED),
        Box::new(|p| p.groups[0].1 = 0),
        Box::new(|p| p.groups.push(("S-1-5-32-544".into(), ENABLED))),
        Box::new(|p| {
            p.privileges.push(LUID {
                LowPart: 99,
                HighPart: 0,
            })
        }),
    ];
    for defect in defects {
        let mut p = valid();
        defect(&mut p);
        assert!(p.require(account, 1, [7, 0], traverse).is_err());
    }
}

#[test]
fn station_filtered_primary_native_reader_is_read_only_and_refuses_foreign_account() {
    let token = OwnedHandle(crate::token::open_self_token().unwrap());
    let before: TOKEN_STATISTICS = fixed(token.raw(), TokenStatistics).unwrap();
    let account = "S-1-5-21-1-2-3-4294967294";
    assert_ne!(crate::sid::current_user_sid().unwrap(), account);
    let error = require(
        token.raw(),
        account,
        fixed(token.raw(), TokenSessionId).unwrap(),
        [
            before.AuthenticationId.LowPart,
            before.AuthenticationId.HighPart as u32,
        ],
    )
    .unwrap_err();
    assert!(
        error
            .to_string()
            .contains("account, session or logon differs"),
        "{error:#}"
    );
    assert!(same_stamp(
        &before,
        &fixed(token.raw(), TokenStatistics).unwrap()
    ));
}
