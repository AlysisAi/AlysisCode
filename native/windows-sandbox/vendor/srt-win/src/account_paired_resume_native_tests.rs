//! Fixed self-check only on admitted disposable accounts. Local tests prove
//! refusal before credential access, never provisioning or installed activation.
use super::*;
use paired_plan::native::account::resume::RESUME_CHECKPOINT;

#[test]
fn paired_consumer_self_check_refuses_missing_lease_and_owner_role() {
    for role in [Role::Owner, Role::Consumers] {
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
        let (selected, other) = if role == Role::Owner {
            (owner, consumer)
        } else {
            (consumer, owner)
        };
        let error = selected
            .run_consumer_self_check(Duration::from_secs(5))
            .err()
            .expect("self-check admission unexpectedly passed");
        assert!(format!("{error:#}").contains(if role == Role::Owner {
            "purpose not admitted"
        } else {
            "original execution lease"
        }));
        assert_eq!(counts(&fixture.name(role)).unwrap().0, 0);
        if role == Role::Owner {
            dead(&owner_ref);
            alive(&consumer_ref);
        } else {
            dead(&consumer_ref);
            alive(&owner_ref);
        }
        assert_eq!(fixture.rows(Role::Owner), 7);
        assert_eq!(fixture.rows(Role::Consumers), 7);
        assert_eq!(std::fs::read(fixture._fixture.path()).unwrap(), marker);
        drop(other);
        drop(jobs);
    }
}

pub(super) fn hosted(fixture: Fixture, password: &str, case: &str) -> Result<()> {
    ensure!(
        ["success", "before-intent", "resumed", "exited", "retiring"].contains(&case),
        "unknown paired self-check case"
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
    let cut = case.to_owned();
    let hits = Rc::new(Cell::new(0));
    let counted = hits.clone();
    RESUME_CHECKPOINT.with(|s| {
        *s.borrow_mut() = Some(Box::new(move |phase, sql| {
            if phase == cut {
                counted.set(counted.get() + 1);
                sql.execute_batch("PRAGMA query_only=ON").unwrap();
            }
        }))
    });
    let result = consumer.fixture_run_consumer_self_check(Duration::from_secs(10), password);
    RESUME_CHECKPOINT.with(|s| {
        s.borrow_mut().take();
    });
    let sql = fixture.db.connection()?;
    sql.execute_batch("PRAGMA query_only=OFF")?;
    let rows: usize = sql.query_row(
        "SELECT count(*) FROM candidate_paired_account_resume_events",
        [],
        |r| r.get(0),
    )?;
    ensure!(
        counts(&fixture.name(Role::Consumers)).unwrap().0 == 0,
        "paired self-check retained consumer processes"
    );
    dead(&consumer_ref);
    alive(&owner_ref);
    ensure!(
        owner.fixture_identity()? == owner_identity,
        "paired self-check lost original account owner"
    );
    if case == "success" {
        ensure!(
            hits.get() == 0 && rows == 3,
            "self-check completion records differ"
        );
        let completed = result?;
        ensure!(
            fixture.rows(Role::Consumers) == 9,
            "self-check consumers not retired"
        );
        owner.retire_owner(completed)?.verify()?;
        dead(&owner_ref);
    } else {
        let error = result
            .err()
            .context("paired self-check fault unexpectedly passed")?;
        require_readonly(&error);
        ensure!(
            hits.get() == 1,
            "paired self-check checkpoint was not reached exactly once"
        );
        let expected = match case {
            "before-intent" => 0,
            "resumed" => 1,
            "exited" => 2,
            "retiring" => 3,
            _ => unreachable!(),
        };
        ensure!(
            rows == expected
                && fixture.rows(Role::Consumers) == 7
                && fixture.rows(Role::Owner) == 7,
            "paired self-check failure changed prefix or claimed completion"
        );
        drop(owner);
    }
    drop(jobs);
    ensure!(
        counts(&fixture.name(Role::Owner)).is_none()
            && counts(&fixture.name(Role::Consumers)).is_none(),
        "paired self-check jobs survived completion"
    );
    ensure!(
        std::fs::read(fixture._fixture.path())? == marker,
        "paired self-check changed pending marker"
    );
    Ok(())
}
