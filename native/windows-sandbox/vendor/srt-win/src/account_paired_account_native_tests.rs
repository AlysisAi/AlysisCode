//! Native refusal locally; successful separate-account births remain confined
//! to the existing explicitly admitted disposable-host fixture.
use super::*;
use paired_plan::native::account::ACCOUNT_CHECKPOINT;

fn account_rows(fixture: &PairFixture, role: Role) -> usize {
    fixture
        .db
        .connection()
        .unwrap()
        .query_row(
            "SELECT count(*) FROM candidate_paired_account_events WHERE role=?1",
            [role.index()],
            |r| r.get(0),
        )
        .unwrap()
}

#[test]
fn paired_account_refusals_drain_exact_role_preserve_owner_and_never_read_ambient_credentials() {
    for (case, expected) in [
        ("missing-lease", 1),
        ("intent-readonly", 0),
        ("synthetic-account-mismatch", 3),
    ] {
        let mut fixture = PairFixture::new();
        let owner_server = fixture.owner_server.take().unwrap();
        let consumer_server = fixture.consumer_server.take().unwrap();
        let marker = std::fs::read(fixture._fixture.path()).unwrap();
        let plan = fixture.plan();
        let jobs = plan.create_jobs().unwrap();
        let owner = jobs.prepare_bootstrap(Role::Owner).unwrap();
        let owner_ref = owner.fixture_readonly_observer().unwrap();
        let consumer = jobs.prepare_bootstrap(Role::Consumers).unwrap();
        let consumer_ref = consumer.fixture_readonly_observer().unwrap();
        let owner = owner.start(owner_server, Duration::from_secs(5)).unwrap();
        let consumer = consumer
            .start(consumer_server, Duration::from_secs(5))
            .unwrap();
        if case == "intent-readonly" {
            fixture
                .db
                .connection()
                .unwrap()
                .execute_batch("PRAGMA query_only=ON")
                .unwrap();
        }
        let result = if case == "synthetic-account-mismatch" {
            consumer
                .fixture_observe_account(Duration::from_secs(5), "Synthetic-never-valid-account!")
        } else {
            consumer.observe_account(Duration::from_secs(5))
        };
        let error = result.err().expect("paired account refusal was bypassed");
        if case == "intent-readonly" {
            require_readonly(&error);
        }
        if case == "missing-lease" {
            assert!(format!("{error:#}").contains("original execution lease"));
        }
        if case == "synthetic-account-mismatch" {
            assert!(
                format!("{error:#}").contains("account birth"),
                "unexpected refusal: {error:#}"
            );
        }
        fixture
            .db
            .connection()
            .unwrap()
            .execute_batch("PRAGMA query_only=OFF")
            .unwrap();
        assert_eq!(
            account_rows(&fixture, Role::Consumers),
            expected,
            "case {case}"
        );
        assert_eq!(account_rows(&fixture, Role::Owner), 0);
        dead(&consumer_ref);
        alive(&owner_ref);
        assert_eq!(counts(&fixture.name(Role::Consumers)).unwrap().0, 0);
        assert_eq!(
            fixture.rows(Role::Consumers),
            7,
            "failure is not successful retirement"
        );
        assert_eq!(fixture.rows(Role::Owner), 7);
        assert_eq!(std::fs::read(fixture._fixture.path()).unwrap(), marker);
        drop(owner);
        drop(jobs);
        assert!(counts(&fixture.name(Role::Consumers)).is_none());
        assert!(counts(&fixture.name(Role::Owner)).is_none());
    }
}

/// Caller has already checked the disposable-host sentinel and created owned
/// principals. This function creates no account and never reads installed secrets.
pub(super) fn hosted(fixture: Fixture, password: &str, case: &str) -> Result<()> {
    ensure!(
        matches!(
            case,
            "success"
                | "consumer-born-receipt-failure"
                | "consumer-acknowledged-receipt-failure"
                | "consumer-retire-intent-failure"
                | "consumer-retire-receipt-failure"
        ),
        "unknown paired account fixture case"
    );
    let mut fixture = PairFixture::from_fixture_image(fixture, true);
    let owner_server = fixture.owner_server.take().unwrap();
    let consumer_server = fixture.consumer_server.take().unwrap();
    let marker = std::fs::read(fixture._fixture.path())?;
    let plan = fixture.plan();
    let jobs = plan.create_jobs()?;
    let owner = jobs.prepare_bootstrap(Role::Owner)?;
    let owner_ref = owner.fixture_readonly_observer()?;
    let consumer = jobs.prepare_bootstrap(Role::Consumers)?;
    let consumer_ref = consumer.fixture_readonly_observer()?;
    let owner = owner.start(owner_server, Duration::from_secs(10))?;
    let consumer = consumer.start(consumer_server, Duration::from_secs(10))?;
    let owner = owner.fixture_observe_account(Duration::from_secs(10), password)?;
    let owner_identity = owner.fixture_identity()?;
    let hits = Rc::new(Cell::new(0));
    let observed_hits = hits.clone();
    let cut = match case {
        "consumer-born-receipt-failure" => "born",
        "consumer-acknowledged-receipt-failure" => "acknowledged",
        _ => "none",
    };
    ACCOUNT_CHECKPOINT.with(|s| {
        *s.borrow_mut() = Some(Box::new(move |phase, role, sql| {
            if role == Role::Consumers && phase == cut {
                observed_hits.set(observed_hits.get() + 1);
                sql.execute_batch("PRAGMA query_only=ON").unwrap();
            }
        }))
    });
    let result = consumer.fixture_observe_account(Duration::from_secs(10), password);
    ACCOUNT_CHECKPOINT.with(|s| {
        s.borrow_mut().take();
    });
    fixture
        .db
        .connection()?
        .execute_batch("PRAGMA query_only=OFF")?;
    if case.starts_with("consumer-retire-") {
        ensure!(
            hits.get() == 0,
            "unexpected birth fault during retirement test"
        );
        let consumer = result?;
        let original_account = consumer.fixture_exit_observer()?;
        let retire_hits = hits.clone();
        if case == "consumer-retire-intent-failure" {
            fixture
                .db
                .connection()?
                .execute_batch("PRAGMA query_only=ON")?;
        } else {
            NATIVE_CHECKPOINT.with(|s| {
                *s.borrow_mut() = Some(Box::new(move |phase, role, sql, _| {
                    if role == Role::Consumers && phase == "job-drained" {
                        retire_hits.set(retire_hits.get() + 1);
                        sql.execute_batch("PRAGMA query_only=ON").unwrap();
                    }
                }))
            });
        }
        let retired = consumer.retire_consumers();
        NATIVE_CHECKPOINT.with(|s| {
            s.borrow_mut().take();
        });
        fixture
            .db
            .connection()?
            .execute_batch("PRAGMA query_only=OFF")?;
        let error = retired
            .err()
            .context("paired account retirement fault unexpectedly passed")?;
        require_readonly(&error);
        ensure!(
            hits.get()
                == if case == "consumer-retire-intent-failure" {
                    0
                } else {
                    1
                },
            "retirement checkpoint count differs"
        );
        ensure!(
            counts(&fixture.name(Role::Consumers)).unwrap().0 == 0,
            "retirement refusal retained account descendants"
        );
        ensure!(
            original_account.fixture_wait_code()? == 1,
            "account retired through timeout instead of job termination"
        );
        dead(&consumer_ref);
        alive(&owner_ref);
        ensure!(
            owner.fixture_identity()? == owner_identity,
            "account owner lost during failed consumer retirement"
        );
        ensure!(
            fixture.rows(Role::Consumers)
                == if case == "consumer-retire-intent-failure" {
                    7
                } else {
                    8
                },
            "failed retirement prefix differs"
        );
        ensure!(
            fixture.rows(Role::Owner) == 7 && account_rows(&fixture, Role::Consumers) == 5,
            "retirement refusal changed prior account records"
        );
        drop(owner);
    } else if case == "success" {
        ensure!(hits.get() == 0, "unexpected paired fault checkpoint");
        let consumer = result?;
        let consumer_identity = consumer.fixture_identity()?;
        ensure!(
            owner_identity.0 != consumer_identity.0,
            "paired accounts reused process"
        );
        ensure!(
            owner_identity.2 == fixture.image.file_identity()?
                && consumer_identity.2 == owner_identity.2,
            "paired account image differs"
        );
        let completed = consumer.retire_consumers()?;
        dead(&consumer_ref);
        alive(&owner_ref);
        ensure!(
            owner.fixture_identity()? == owner_identity,
            "original account owner did not survive consumer retirement"
        );
        ensure!(
            counts(&fixture.name(Role::Consumers)).unwrap().0 == 0,
            "account consumer job did not drain"
        );
        ensure!(
            account_rows(&fixture, Role::Consumers) == 5,
            "consumer account receipts incomplete"
        );
        owner.retire_owner(completed)?.verify()?;
        dead(&owner_ref);
        ensure!(
            fixture.rows(Role::Consumers) == 9 && fixture.rows(Role::Owner) == 9,
            "paired account retirement receipts incomplete"
        );
    } else {
        let error = result
            .err()
            .context("paired native receipt fault unexpectedly passed")?;
        ensure!(
            hits.get() == 1,
            "paired native receipt checkpoint did not fire exactly once"
        );
        require_readonly(&error);
        dead(&consumer_ref);
        alive(&owner_ref);
        ensure!(
            owner.fixture_identity()? == owner_identity,
            "account owner lost during consumer refusal"
        );
        ensure!(
            counts(&fixture.name(Role::Consumers)).unwrap().0 == 0,
            "failed account role retained descendants"
        );
        ensure!(
            account_rows(&fixture, Role::Consumers) == if cut == "born" { 3 } else { 4 },
            "paired account failure prefix differs"
        );
        ensure!(
            fixture.rows(Role::Consumers) == 7 && fixture.rows(Role::Owner) == 7,
            "failed account minted retirement"
        );
        drop(owner);
    }
    ensure!(
        account_rows(&fixture, Role::Owner) == 5,
        "owner account receipts incomplete"
    );
    drop(jobs);
    ensure!(
        counts(&fixture.name(Role::Owner)).is_none()
            && counts(&fixture.name(Role::Consumers)).is_none(),
        "paired original jobs survived fixture completion"
    );
    ensure!(
        std::fs::read(fixture._fixture.path())? == marker,
        "paired account changed pending marker"
    );
    Ok(())
}
