//! Account effects run only inside the admitted disposable hosted fixture.
//! Locally we test refusal before credentials and both-job failure draining.
use super::*;
use paired_plan::native::account::{ACCOUNT_CRASH_CHECKPOINT, profile::PROFILE_CHECKPOINT};

#[test]
fn paired_profile_refuses_missing_lease_before_consumer_birth() {
    let mut fixture = PairFixture::new();
    let owner = fixture.owner_server.take().unwrap();
    let consumer = fixture.consumer_server.take().unwrap();
    let marker = std::fs::read(fixture._fixture.path()).unwrap();
    let plan = fixture.plan();
    let jobs = plan.create_jobs().unwrap();
    let error = jobs
        .run_profile_self_check(owner, consumer, Duration::from_secs(5))
        .err()
        .expect("missing lease admitted");
    assert!(format!("{error:#}").contains("original execution lease"));
    assert_eq!(fixture.rows(Role::Owner), 7);
    assert_eq!(fixture.rows(Role::Consumers), 2);
    for role in [Role::Owner, Role::Consumers] {
        assert_eq!(counts(&fixture.name(role)).unwrap().0, 0);
    }
    assert_eq!(std::fs::read(fixture._fixture.path()).unwrap(), marker);
    drop(jobs);
    for role in [Role::Owner, Role::Consumers] {
        assert!(counts(&fixture.name(role)).is_none());
    }
}

pub(super) fn hosted(fixture: Fixture, password: &str, case: &str) -> Result<serde_json::Value> {
    let descendants = matches!(
        case,
        "writer-descendants-success" | "writer-descendants-before-capture"
    );
    let cut_case = case
        .strip_prefix("writer-descendants-")
        .or_else(|| case.strip_prefix("writer-"))
        .unwrap_or(case);
    let lifetime = descendants
        || matches!(
            case,
            "writer-long-success" | "writer-long-interrupted" | "writer-long-source-exit"
        );
    let source_exit = case == "writer-long-source-exit";
    let interrupted = case == "writer-long-interrupted" || source_exit;
    let writer = matches!(
        case,
        "writer-success" | "writer-unsupported" | "writer-before-capture"
    ) || lifetime;
    let success = matches!(
        case,
        "success" | "writer-success" | "writer-long-success" | "writer-descendants-success"
    );
    let unsupported = case == "writer-unsupported";
    let phases = [
        "before-resume-intent",
        "resumed",
        "before-create",
        "created",
        "before-use",
        "using",
        "before-capture",
        "captured",
        "before-remove",
        "removed",
        "exited",
        "retiring",
    ];
    ensure!(
        case == "success" || writer || phases.contains(&case),
        "unknown paired profile case"
    );
    let mut fixture = PairFixture::from_fixture_image(fixture, true);
    let owner = fixture.owner_server.take().unwrap();
    let consumer = fixture.consumer_server.take().unwrap();
    let marker = std::fs::read(fixture._fixture.path())?;
    let plan = fixture.plan();
    let jobs = plan.create_jobs()?;
    let originals = Rc::new(RefCell::new(Vec::new()));
    let retained = originals.clone();
    ACCOUNT_CRASH_CHECKPOINT.with(|slot| {
        *slot.borrow_mut() = Some(Box::new(move |phase, role, _, observed| {
            if phase == "acknowledged" {
                retained.borrow_mut().push((role, observed));
            }
        }));
    });
    let bootstrap = Rc::new(RefCell::new(Vec::new()));
    let retained = bootstrap.clone();
    NATIVE_CHECKPOINT.with(|slot| {
        *slot.borrow_mut() = Some(Box::new(move |phase, role, _, raw| {
            if phase == "bootstrap-born" {
                retained
                    .borrow_mut()
                    .push((role, retain_original(raw.unwrap())));
            }
        }));
    });
    let hits = Rc::new(Cell::new(0));
    let counted = hits.clone();
    let cut = cut_case.to_owned();
    PROFILE_CHECKPOINT.with(|slot| {
        *slot.borrow_mut() = Some(Box::new(move |phase, sql| {
            if phase == cut {
                counted.set(counted.get() + 1);
                sql.execute_batch("PRAGMA query_only=ON").unwrap();
            }
        }));
    });
    crate::account_control::writer_fixture::REPORT.with(|slot| {
        slot.borrow_mut().take();
    });
    crate::account_control::writer_fixture::PROGRESS.with(|slot| slot.set(0));
    let family_refs = Rc::new(RefCell::new(Vec::new()));
    let retained = family_refs.clone();
    crate::account_control::writer_fixture::FAMILY_OBSERVED.with(|slot| {
        *slot.borrow_mut() = Some(Box::new(move |raw| {
            retained.borrow_mut().push(retain_original(raw))
        }));
    });
    let result = if lifetime {
        jobs.fixture_run_profile_lifetime(
            owner,
            consumer,
            Duration::from_secs(10),
            password,
            if source_exit {
                2
            } else if interrupted {
                1
            } else {
                0
            },
            descendants,
        )
    } else if writer {
        jobs.fixture_run_profile_writer(
            owner,
            consumer,
            Duration::from_secs(10),
            password,
            unsupported,
        )
    } else {
        jobs.fixture_run_profile_self_check(owner, consumer, Duration::from_secs(10), password)
    };
    let writer_report =
        crate::account_control::writer_fixture::REPORT.with(|slot| slot.borrow_mut().take());
    crate::account_control::writer_fixture::FAMILY_OBSERVED.with(|slot| {
        slot.borrow_mut().take();
    });
    let progress = crate::account_control::writer_fixture::PROGRESS.with(|slot| slot.get());
    let mut family_codes = Vec::new();
    for original in family_refs.borrow_mut().drain(..) {
        let code = exited_code(&original);
        family_codes.push(code);
    }
    ACCOUNT_CRASH_CHECKPOINT.with(|s| {
        s.borrow_mut().take();
    });
    NATIVE_CHECKPOINT.with(|s| {
        s.borrow_mut().take();
    });
    PROFILE_CHECKPOINT.with(|s| {
        s.borrow_mut().take();
    });
    let sql = fixture.db.connection()?;
    sql.execute_batch("PRAGMA query_only=OFF")?;
    let rows: usize = sql.query_row(
        "SELECT count(*) FROM candidate_paired_profile_events",
        [],
        |r| r.get(0),
    )?;
    let resume_rows: usize = sql.query_row(
        "SELECT count(*) FROM candidate_paired_account_resume_events",
        [],
        |r| r.get(0),
    )?;
    let mut original_codes = Vec::new();
    for (role, original) in originals.borrow_mut().drain(..) {
        let code = original.fixture_wait_code()?;
        if writer {
            // Fixed case, role, counts and original exit code only; retain even
            // when a subsequent prefix assertion fails. No paths or secrets.
            eprintln!(
                "paired writer case={case} role={} original_exit=0x{code:08x} profile_rows={rows} resume_rows={resume_rows} completed_family={}",
                role.index(),
                writer_report.is_some()
            );
        }
        original_codes.push((role, code));
    }
    ensure!(
        family_codes.len() == if writer { 2 } else { 0 },
        "fixed writer family original references missing"
    );
    ensure!(
        family_codes.iter().all(|c| *c == 1),
        "fixed writer family exited without original job retirement/drain"
    );
    ensure!(
        progress
            == if interrupted {
                5
            } else if lifetime {
                12
            } else {
                0
            },
        "fixed writer renewal progress differs; original codes {original_codes:?}"
    );
    for role in [Role::Owner, Role::Consumers] {
        ensure!(
            counts(&fixture.name(role)).unwrap().0 == 0,
            "profile case retained live role"
        );
    }
    let expected = if success {
        11
    } else if interrupted {
        6
    } else if unsupported {
        7
    } else {
        phases.iter().position(|p| *p == cut_case).unwrap()
    };
    if success {
        match result {
            Ok(completed) => completed.verify()?,
            Err(error) => {
                return Err(error).with_context(|| {
                    format!(
                        "paired profile case {case}; original account exit codes {original_codes:?}"
                    )
                });
            }
        }
        ensure!(
            hits.get() == 0 && fixture.rows(Role::Owner) == 9 && fixture.rows(Role::Consumers) == 9,
            "paired profile successful pair not retired"
        );
    } else {
        let error = result
            .err()
            .context("paired profile write fault unexpectedly passed")?;
        if source_exit {
            // The original source exit code is independently checked below.
            ensure!(
                progress == 5,
                "original source fault did not follow five renewals"
            );
        } else if interrupted {
            ensure!(
                format!("{error:#}")
                    .contains("fixed original-owner renewal deliberately interrupted"),
                "long writer failed for another reason"
            );
        } else if !unsupported {
            require_readonly(&error);
        }
        ensure!(
            hits.get() == if unsupported || interrupted { 0 } else { 1 }
                && fixture.rows(Role::Owner) == 7,
            "paired profile fault count or owner prefix differs"
        );
        ensure!(
            fixture.rows(Role::Consumers)
                == if expected < 4 {
                    2
                } else if expected < 6 || interrupted {
                    7
                } else {
                    9
                },
            "paired profile consumer prefix differs"
        );
    }
    ensure!(
        rows == expected
            && resume_rows
                == if interrupted {
                    2
                } else if expected < 6 {
                    0
                } else {
                    3
                },
        "paired profile durable prefix differs"
    );
    let processes = if expected < 4 { 1 } else { 2 };
    ensure!(
        original_codes.len() == processes && bootstrap.borrow().len() == processes,
        "paired profile original process references missing"
    );
    let mut exit_codes = Vec::new();
    for (role, code) in original_codes {
        if success {
            ensure!(code == 0x4150, "paired profile helper failed");
        }
        if unsupported {
            ensure!(
                code == if role == Role::Owner {
                    0x42210000
                } else {
                    0x4150
                },
                "unsupported writer content did not fail at original capture"
            );
        }
        exit_codes.push((role.index(), code));
    }
    for (role, original) in bootstrap.borrow_mut().drain(..) {
        if source_exit && role == Role::Owner {
            ensure!(
                exited_code(&original) == 74,
                "original owner source was not forcibly stopped"
            );
        } else {
            dead(&original);
        }
    }
    let records: Vec<serde_json::Value> = sql
        .prepare("SELECT event_json FROM candidate_paired_profile_events ORDER BY sequence")?
        .query_map([], |r| r.get::<_, String>(0))?
        .map(|r| Ok(serde_json::from_str(&r?)?))
        .collect::<Result<_>>()?;
    let prefixes = [fixture.rows(Role::Owner), fixture.rows(Role::Consumers)];
    let kinds: Vec<_> = records.iter().map(|r| r["kind"].clone()).collect();
    ensure!(
        (writer && !interrupted) == writer_report.is_some(),
        "fixed writer original family report missing or unexpected"
    );
    if let Some(report) = &writer_report {
        ensure!(
            report["original_descendants"]
                .as_array()
                .is_some_and(|a| a.len() == 2)
                && report["live_after_consumer_exit"] == true
                && report["consumer_job_retired_before_capture"] == true,
            "fixed writer original family proof incomplete"
        );
    }
    if let Some(report) = &writer_report {
        let files = report["descendant_files"]
            .as_array()
            .context("descendant file report missing")?;
        ensure!(
            report["descendants_write_profile"] == descendants
                && files.len() == if descendants { 2 } else { 0 },
            "descendant writer report shape differs"
        );
        if descendants {
            ensure!(
                files[0]["depth"] == 1
                    && files[1]["depth"] == 2
                    && files[0]["file_id"] != files[1]["file_id"]
                    && files
                        .iter()
                        .all(|f| f["file_id"] != report["file"]["file_id"]),
                "descendant receipt depth/identity differs"
            );
        }
    }
    drop(jobs);
    for role in [Role::Owner, Role::Consumers] {
        ensure!(
            counts(&fixture.name(role)).is_none(),
            "paired profile job survived closure"
        );
    }
    ensure!(
        std::fs::read(fixture._fixture.path())? == marker,
        "paired profile changed pending marker"
    );
    Ok(
        serde_json::json!({"case":case,"passed":true,"profile_rows":rows,"resume_rows":resume_rows,"role_rows":prefixes,"original_account_exit_codes":exit_codes,"original_bootstraps_exited":processes,"original_jobs_absent":true,"pending_marker_unchanged":true,"profile_kinds":kinds,"profile_first":records.first(),"profile_last":records.last(),"profile_use":records.get(4),"consumer":if descendants { "fixed-profile-writer-with-writing-descendants" } else if writer { "fixed-profile-writer-with-idle-descendants" } else { "fixed-self-check-only" },"writer_report":writer_report,"writer_progress":progress,"original_family_exit_codes":family_codes,"owner_source_forcibly_stopped":source_exit,"untrusted_command_qualified":false}),
    )
}
