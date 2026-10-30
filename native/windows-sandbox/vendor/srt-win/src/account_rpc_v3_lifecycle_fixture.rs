//! Source-selected disposable lifecycle admission, never a production capability.
//! This guard changes neither the writer purpose nor namespace construction.
//! Keep it alive through original retirement; copied records cannot recreate it.
#![cfg(test)]

use anyhow::{Context, Result, ensure};
use std::{cell::Cell, marker::PhantomData, rc::Rc, thread::LocalKey};

use crate::account_install::AccountMode;
use crate::account_state::execution::resources::FixtureRpcPolicy;

pub(crate) const POLICY: FixtureRpcPolicy = FixtureRpcPolicy::SystemNsiReadInheritanceV3;
pub(crate) const SOURCE_SELECTED: bool = true;
pub(crate) const SHADOW_SOURCE_SELECTED: bool = true;
pub(crate) const CONTAINED_OFFLINE_SOURCE_SELECTED: bool = true;
pub(crate) const CONTAINED_ONLINE_SOURCE_SELECTED: bool = true;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Active {
    Ordinary,
    Shadow,
    ContainedOffline,
    ContainedOnline,
}

impl Active {
    const ALL: [Self; 4] = [
        Self::Ordinary,
        Self::Shadow,
        Self::ContainedOffline,
        Self::ContainedOnline,
    ];

    fn selected(self) -> bool {
        match self {
            Self::Ordinary => SOURCE_SELECTED,
            Self::Shadow => SHADOW_SOURCE_SELECTED,
            Self::ContainedOffline => CONTAINED_OFFLINE_SOURCE_SELECTED,
            Self::ContainedOnline => CONTAINED_ONLINE_SOURCE_SELECTED,
        }
    }
    fn contained_mode(self) -> Option<AccountMode> {
        match self {
            Self::ContainedOffline => Some(AccountMode::Offline),
            Self::ContainedOnline => Some(AccountMode::Online),
            Self::Ordinary | Self::Shadow => None,
        }
    }
    fn required_environment(self) -> [(&'static str, &'static str); 2] {
        match self {
            Self::Ordinary => [
                ("ALYSIS_ACCOUNT_BIRTH_SELECTION", "rpc-v3-lifecycle"),
                (
                    "ALYSIS_RUN_DISPOSABLE_RPC_V3_LIFECYCLE",
                    "github-hosted-rpc-v3-lifecycle-only",
                ),
            ],
            Self::ContainedOffline => [
                ("ALYSIS_ACCOUNT_BIRTH_SELECTION", "rpc-v3-contained-offline"),
                (
                    "ALYSIS_RUN_DISPOSABLE_RPC_V3_CONTAINED_OFFLINE",
                    "github-hosted-rpc-v3-contained-offline-only",
                ),
            ],
            Self::ContainedOnline => [
                ("ALYSIS_ACCOUNT_BIRTH_SELECTION", "rpc-v3-contained-online"),
                (
                    "ALYSIS_RUN_DISPOSABLE_RPC_V3_CONTAINED_ONLINE",
                    "github-hosted-rpc-v3-contained-online-only",
                ),
            ],
            Self::Shadow => [
                ("ALYSIS_ACCOUNT_BIRTH_SELECTION", "rpc-v3-shadow-lifecycle"),
                (
                    "ALYSIS_RUN_DISPOSABLE_RPC_V3_SHADOW_LIFECYCLE",
                    "github-hosted-rpc-v3-shadow-lifecycle-only",
                ),
            ],
        }
    }
    fn others(self) -> impl Iterator<Item = Self> {
        Self::ALL.into_iter().filter(move |route| *route != self)
    }
}

thread_local! {
    static ACTIVE: Cell<Option<Active>> = const { Cell::new(None) };
}

/// Exclusive to the entering thread, with no Clone or deserialization path.
#[must_use = "retain the admission guard through original lifecycle retirement"]
pub(crate) struct Guard {
    state: &'static LocalKey<Cell<Option<Active>>>,
    // A thread-local admission must never be moved or shared across threads.
    _thread: PhantomData<Rc<()>>,
}

impl Drop for Guard {
    fn drop(&mut self) {
        self.state.with(|active| active.set(None));
    }
}

/// Enter only from the separately admitted hosted root or fixed held broker.
/// Environment checks admit this fixed policy; they never select a policy.
pub(crate) fn enter() -> Result<Guard> {
    enter_checked(&ACTIVE, Active::Ordinary, || {
        current_admission(Active::Ordinary)
    })
}

/// Separate fixed entrypoint; no environment value chooses this route.
pub(crate) fn enter_shadow() -> Result<Guard> {
    enter_checked(&ACTIVE, Active::Shadow, || {
        current_admission(Active::Shadow)
    })
}

/// The V3 policy arm calls this on every live preparation/grant/verification.
/// Neither valid environment values alone nor a persisted policy admit effects.
pub(crate) fn require_live() -> Result<()> {
    current_admission(require_owned(&ACTIVE, None)?)
}

pub(crate) fn require_ordinary_live() -> Result<()> {
    current_admission(require_owned(&ACTIVE, Some(Active::Ordinary))?)
}

pub(crate) fn require_shadow_live() -> Result<()> {
    current_admission(require_owned(&ACTIVE, Some(Active::Shadow))?)
}

pub(crate) fn enter_contained_offline() -> Result<Guard> {
    enter_checked(&ACTIVE, Active::ContainedOffline, || {
        current_admission(Active::ContainedOffline)
    })
}
pub(crate) fn require_contained_offline_live() -> Result<()> {
    current_admission(require_owned(&ACTIVE, Some(Active::ContainedOffline))?)
}
pub(crate) fn enter_contained_online() -> Result<Guard> {
    enter_checked(&ACTIVE, Active::ContainedOnline, || {
        current_admission(Active::ContainedOnline)
    })
}
pub(crate) fn require_contained_online_live() -> Result<()> {
    current_admission(require_owned(&ACTIVE, Some(Active::ContainedOnline))?)
}
pub(crate) fn require_shadow_root_live() -> Result<()> {
    let route = require_owned(&ACTIVE, None)?;
    ensure!(
        matches!(
            route,
            Active::Shadow | Active::ContainedOffline | Active::ContainedOnline
        ),
        "explicit shadow root requires original matching route"
    );
    current_admission(route)
}
pub(crate) fn contained_offline_selected() -> Result<bool> {
    if ACTIVE.with(|s| s.get()) == Some(Active::ContainedOffline) {
        require_contained_offline_live()?;
        Ok(true)
    } else {
        Ok(false)
    }
}
pub(crate) fn contained_online_selected() -> Result<bool> {
    if ACTIVE.with(|s| s.get()) == Some(Active::ContainedOnline) {
        require_contained_online_live()?;
        Ok(true)
    } else {
        Ok(false)
    }
}
/// Only original contained admission supplies a mode; environment alone cannot.
pub(crate) fn contained_mode() -> Result<Option<AccountMode>> {
    contained_mode_checked(&ACTIVE, current_admission)
}
fn contained_mode_checked(
    state: &'static LocalKey<Cell<Option<Active>>>,
    check: impl FnOnce(Active) -> Result<()>,
) -> Result<Option<AccountMode>> {
    let Some(route) = state.with(|s| s.get()) else {
        return Ok(None);
    };
    let mode = route.contained_mode();
    if mode.is_some() {
        check(route)?;
    }
    Ok(mode)
}
fn current_admission(route: Active) -> Result<()> {
    require_admission(
        route,
        cfg!(all(windows, target_arch = "x86_64")),
        route.selected(),
        station_probe_active(),
        |key| std::env::var(key).ok(),
    )
}

fn station_probe_active() -> bool {
    // Refuse a mixed route rather than allowing its writer-purpose replacement.
    // This fixture never sets or borrows the station fixture's admission.
    crate::account_control::station_lowbox_fixture::ENABLED.with(|active| active.get())
}

fn enter_checked(
    state: &'static LocalKey<Cell<Option<Active>>>,
    route: Active,
    check: impl FnOnce() -> Result<()>,
) -> Result<Guard> {
    ensure!(
        state.with(|active| active.get().is_none()),
        "RPC V3 lifecycle admission is already active"
    );
    check()?;
    // Recheck after validation too: a reentrant attempt cannot acquire a second
    // guard or clear the first guard's responsibility on a failed admission.
    state.with(|active| -> Result<()> {
        ensure!(
            active.get().is_none(),
            "RPC V3 lifecycle admission became active during validation"
        );
        active.set(Some(route));
        Ok(())
    })?;
    Ok(Guard {
        state,
        _thread: PhantomData,
    })
}

fn require_owned(
    state: &'static LocalKey<Cell<Option<Active>>>,
    expected: Option<Active>,
) -> Result<Active> {
    let route = state
        .with(|active| active.get())
        .context("RPC V3 lifecycle lacks its original thread admission")?;
    ensure!(
        expected.is_none_or(|expected| expected == route),
        "RPC V3 lifecycle requires a different owned route"
    );
    Ok(route)
}

fn require_admission(
    route: Active,
    platform: bool,
    selected: bool,
    station_active: bool,
    environment: impl Fn(&str) -> Option<String>,
) -> Result<()> {
    ensure!(
        platform && selected && !station_active,
        "RPC V3 lifecycle source, platform or exclusive route differs"
    );
    for (key, expected) in COMMON_ENVIRONMENT
        .into_iter()
        .chain(route.required_environment())
    {
        ensure!(
            environment(key).as_deref() == Some(expected),
            "RPC V3 lifecycle disposable admission refused"
        );
    }
    for other in route.others() {
        let other_sentinel = other.required_environment()[1].0;
        ensure!(
            environment(other_sentinel).is_none_or(|value| value.is_empty()),
            "RPC V3 lifecycle contains a stale or mixed route sentinel"
        );
    }
    for key in ["GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT"] {
        let value = environment(key).context("RPC V3 lifecycle run identity absent")?;
        ensure!(
            (1..=24).contains(&value.len())
                && value.bytes().all(|c| c.is_ascii_digit())
                && value.bytes().any(|c| c != b'0'),
            "RPC V3 lifecycle run identity invalid"
        );
    }
    Ok(())
}

const COMMON_ENVIRONMENT: [(&str, &str); 9] = [
    (
        "ALYSIS_RUN_DISPOSABLE_ACCOUNT_BIRTH",
        "github-hosted-account-birth-only",
    ),
    (
        "ALYSIS_RUN_DISPOSABLE_ATOMIC_STATION",
        "github-hosted-atomic-station-only",
    ),
    (
        "ALYSIS_RUN_DISPOSABLE_ATOMIC_PROFILE",
        "github-hosted-atomic-profile-only",
    ),
    (
        "ALYSIS_RUN_DISPOSABLE_UNUSED_PROFILE",
        "github-hosted-unused-profile-only",
    ),
    ("GITHUB_ACTIONS", "true"),
    ("RUNNER_ENVIRONMENT", "github-hosted"),
    ("RUNNER_OS", "Windows"),
    ("GITHUB_REPOSITORY", "AlysisAi/alysis-code-internal"),
    ("GITHUB_EVENT_NAME", "workflow_dispatch"),
];

mod tests {
    use super::*;

    // Synthetic state is deliberately separate from the real admission key.
    thread_local! {
        static TEST_ACTIVE: Cell<Option<Active>> = const { Cell::new(None) };
    }

    fn environment(route: Active, key: &str) -> Option<String> {
        COMMON_ENVIRONMENT
            .into_iter()
            .chain(route.required_environment())
            .find(|(name, _)| *name == key)
            .map(|(_, value)| value.into())
            .or_else(|| match key {
                "GITHUB_RUN_ID" => Some("38005276610".into()),
                "GITHUB_RUN_ATTEMPT" => Some("1".into()),
                _ => None,
            })
    }

    #[test]
    fn rpc_v3_lifecycle_admission_requires_each_fixed_sentinel() {
        assert_eq!(POLICY, FixtureRpcPolicy::SystemNsiReadInheritanceV3);
        for route in Active::ALL {
            let env = |key: &str| environment(route, key);
            require_admission(route, true, true, false, env).unwrap();
            for (key, _) in COMMON_ENVIRONMENT
                .into_iter()
                .chain(route.required_environment())
            {
                for replacement in [None, Some("different".to_owned())] {
                    assert!(
                        require_admission(route, true, true, false, |name| {
                            if name == key {
                                replacement.clone()
                            } else {
                                env(name)
                            }
                        })
                        .is_err(),
                        "accepted different admission for {key}"
                    );
                }
            }
            for (platform, selected, station) in [
                (false, true, false),
                (true, false, false),
                (true, true, true),
            ] {
                assert!(require_admission(route, platform, selected, station, env).is_err());
            }
        }
    }

    #[test]
    fn rpc_v3_lifecycle_run_identity_is_present_bounded_decimal_and_nonzero() {
        for route in Active::ALL {
            for key in ["GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT"] {
                for invalid in [
                    None,
                    Some(""),
                    Some("0"),
                    Some("000"),
                    Some(" 1"),
                    Some("1a"),
                    Some("-1"),
                    Some("1111111111111111111111111"),
                ] {
                    assert!(
                        require_admission(route, true, true, false, |name| {
                            if name == key {
                                invalid.map(str::to_owned)
                            } else {
                                environment(route, name)
                            }
                        })
                        .is_err()
                    );
                }
            }
        }
    }

    #[test]
    fn rpc_v3_lifecycle_routes_refuse_wrong_environment_and_stale_sentinels() {
        for route in Active::ALL {
            for other in route.others() {
                assert!(
                    require_admission(route, true, true, false, |key| environment(other, key))
                        .is_err()
                );
                let (other_key, other_value) = other.required_environment()[1];
                for value in [None, Some(""), Some(" "), Some("stale"), Some(other_value)] {
                    let result = require_admission(route, true, true, false, |key| {
                        if key == other_key {
                            value.map(str::to_owned)
                        } else {
                            environment(route, key)
                        }
                    });
                    assert_eq!(result.is_ok(), value.is_none_or(str::is_empty));
                }
            }
        }
    }

    #[test]
    fn rpc_v3_lifecycle_guard_refuses_nesting_and_preserves_original_until_drop() {
        for route in Active::ALL {
            assert!(require_owned(&TEST_ACTIVE, None).is_err());
            assert!(
                enter_checked(&TEST_ACTIVE, route, || anyhow::bail!("synthetic refusal")).is_err()
            );
            assert!(require_owned(&TEST_ACTIVE, None).is_err());
            let guard = enter_checked(&TEST_ACTIVE, route, || {
                require_admission(route, true, true, false, |key| environment(route, key))
            })
            .unwrap();
            assert_eq!(require_owned(&TEST_ACTIVE, None).unwrap(), route);
            assert_eq!(require_owned(&TEST_ACTIVE, Some(route)).unwrap(), route);
            for other in route.others() {
                assert!(require_owned(&TEST_ACTIVE, Some(other)).is_err());
            }
            for attempted in Active::ALL {
                assert!(
                    enter_checked(&TEST_ACTIVE, attempted, || panic!("nested validation ran"))
                        .is_err()
                );
                assert_eq!(require_owned(&TEST_ACTIVE, None).unwrap(), route);
            }
            // Matching synthetic environment/state never admits the real route.
            assert!(require_live().is_err());
            assert!(require_ordinary_live().is_err());
            assert!(require_shadow_live().is_err());
            assert!(require_shadow_root_live().is_err());
            assert!(require_contained_offline_live().is_err());
            assert!(require_contained_online_live().is_err());
            assert!(!contained_offline_selected().unwrap());
            assert!(!contained_online_selected().unwrap());
            assert_eq!(contained_mode().unwrap(), None);
            drop(guard);
            assert!(require_owned(&TEST_ACTIVE, None).is_err());
        }
    }

    #[test]
    fn rpc_v3_contained_mode_requires_original_route_and_revalidates_admission() {
        assert_eq!(
            contained_mode_checked(&TEST_ACTIVE, |_| panic!("absent guard cannot select mode"))
                .unwrap(),
            None
        );
        for (route, expected) in [
            (Active::Ordinary, None),
            (Active::Shadow, None),
            (Active::ContainedOffline, Some(AccountMode::Offline)),
            (Active::ContainedOnline, Some(AccountMode::Online)),
        ] {
            let guard = enter_checked(&TEST_ACTIVE, route, || Ok(())).unwrap();
            let mut observed = None;
            let mode = contained_mode_checked(&TEST_ACTIVE, |original| {
                observed = Some(original);
                require_admission(original, true, true, false, |key| {
                    environment(original, key)
                })
            })
            .unwrap();
            assert_eq!(mode, expected);
            assert_eq!(observed, expected.map(|_| route));
            assert_eq!(
                contained_mode_checked(&TEST_ACTIVE, |_| anyhow::bail!("changed admission"))
                    .is_err(),
                expected.is_some()
            );
            // A synthetic owned route never changes the real thread admission.
            assert_eq!(contained_mode().unwrap(), None);
            drop(guard);
        }
    }

    #[test]
    fn rpc_v3_lifecycle_guard_resets_on_unwind_and_rechecks_after_validation() {
        for route in Active::ALL {
            assert!(
                std::panic::catch_unwind(|| {
                    let _guard = enter_checked(&TEST_ACTIVE, route, || Ok(())).unwrap();
                    panic!("synthetic failure after admission");
                })
                .is_err()
            );
            assert!(require_owned(&TEST_ACTIVE, None).is_err());
        }
        let mut original = None;
        assert!(
            enter_checked(&TEST_ACTIVE, Active::Ordinary, || {
                original = Some(enter_checked(&TEST_ACTIVE, Active::Shadow, || Ok(()))?);
                Ok(())
            })
            .is_err()
        );
        // A refused outer Ordinary entry must not overwrite the owned Shadow.
        assert_eq!(require_owned(&TEST_ACTIVE, None).unwrap(), Active::Shadow);
        assert!(require_owned(&TEST_ACTIVE, Some(Active::Ordinary)).is_err());
        drop(original);
        assert!(require_owned(&TEST_ACTIVE, None).is_err());
    }
}
