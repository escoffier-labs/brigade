//! Issue #2621: the rollback quota probes stay inside the writer deadline as a
//! scope accumulates automatic applies.
use super::compensation_tests::applied;
use super::retrieval_eval::FIXTURE_SCOPE;
use super::tests::*;
use super::{ADMIN, hardening, schema};
use rusqlite::Connection;
use serde_json::json;
use std::time::Instant;

fn plan(db: &Connection, sql: &str, params: &[&dyn rusqlite::ToSql]) -> String {
    let mut statement = db.prepare(&format!("EXPLAIN QUERY PLAN {sql}")).unwrap();
    let rows = statement
        .query_map(params, |r| r.get::<_, String>(3))
        .unwrap()
        .collect::<rusqlite::Result<Vec<_>>>()
        .unwrap();
    rows.join(" | ")
}

fn ms(start: Instant) -> f64 {
    start.elapsed().as_secs_f64() * 1000.0
}

fn best(mut run: impl FnMut()) -> f64 {
    (0..5)
        .map(|_| {
            let start = Instant::now();
            run();
            ms(start)
        })
        .fold(f64::MAX, f64::min)
}

/// One applied automatic proposal charged to `FIXTURE_SCOPE`, shaped like a
/// real body: the row is what an unhinted probe would read in full.
fn seed_apply(db: &Connection, index: usize, principal: &str) {
    let proposal_id = format!("seed-{index:06}");
    let body = json!({
        "proposal_id": proposal_id,
        "proposer": principal,
        "state": "applied",
        "decision": "apply",
        "decision_mode": "automatic",
        "tier": 0,
        "base_epoch": "epoch",
        "policy_digest": "0".repeat(64),
        "cards": [{"scope": FIXTURE_SCOPE, "card_id": "a", "base_revision": 1,
                   "before_hash": "0".repeat(64), "after_hash": "1".repeat(64)}],
        "ops": [],
        "diff": "x".repeat(4_000)
    })
    .to_string();
    db.execute(
        "INSERT INTO memory_proposals(proposal_id,state,body,principal,created_at,digest,retained_bytes,terminal_ms,compacted) VALUES(?1,'applied',?2,?3,'2026-01-01T00:00:00Z',?4,?5,1800000000000,0)",
        rusqlite::params![proposal_id, body, principal, format!("digest-{index:06}"), body.len() as i64],
    )
    .unwrap();
    db.execute(
        "INSERT INTO memory_proposal_scopes VALUES(?1,?2)",
        rusqlite::params![proposal_id, FIXTURE_SCOPE],
    )
    .unwrap();
}

fn seed(db: &Connection, count: usize, principal: &str) {
    for index in 0..count {
        seed_apply(db, index, principal);
    }
}

fn version(db: &Connection) -> i64 {
    db.query_row("PRAGMA user_version", [], |r| r.get(0))
        .unwrap()
}

fn rows(db: &Connection) -> (i64, i64) {
    db.query_row(
        "SELECT (SELECT count(*) FROM memory_proposals),(SELECT sum(retained_bytes) FROM memory_proposals)",
        [],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )
    .unwrap()
}

/// The shipped probes read the covering index and never the proposal body. The
/// planner otherwise keeps the WITHOUT ROWID primary-key probe, so this pins
/// the `INDEXED BY` clause that makes the index effective without statistics.
#[test]
fn rollback_quota_probes_read_the_covering_indexes() {
    let h = Hub::new();
    let db = Connection::open(h.memory()).unwrap();
    seed(&db, 8, ADMIN);
    let scope = plan(&db, hardening::SCOPE_USAGE, &[&FIXTURE_SCOPE, &"", &""]);
    assert!(
        scope.contains("COVERING INDEX memory_proposals_scope_quota"),
        "{scope}"
    );
    assert!(!scope.contains("PRIMARY KEY (proposal_id=?)"), "{scope}");
    let principal = plan(&db, hardening::PRINCIPAL_USAGE, &[&ADMIN, &"", &""]);
    assert!(
        principal.contains("COVERING INDEX memory_proposals_principal_quota"),
        "{principal}"
    );
    let settle_scope = plan(&db, hardening::SETTLE_SCOPE_USAGE, &[&FIXTURE_SCOPE]);
    assert!(
        settle_scope.contains("COVERING INDEX memory_proposals_scope_quota"),
        "{settle_scope}"
    );
    let settle_principal = plan(&db, hardening::SETTLE_PRINCIPAL_USAGE, &[&ADMIN]);
    assert!(
        settle_principal.contains("COVERING INDEX memory_proposals_principal_quota"),
        "{settle_principal}"
    );
}

/// A populated version 15 file gains both indexes on open, with its proposals,
/// bytes and log epoch unchanged. The step is additive and re-running it is a
/// no-op.
#[test]
fn version_fifteen_gains_the_quota_indexes_on_open() {
    let dir = temp_dir();
    let path = dir.join("memory.db");
    let db = Connection::open(&path).unwrap();
    schema::migrate(&db, 1_800_000_000_000).unwrap();
    seed(&db, 4, ADMIN);
    let before = rows(&db);
    let epoch = schema::log_epoch(&db).unwrap();
    db.execute_batch(
        "DROP INDEX memory_proposals_scope_quota;
         DROP INDEX memory_proposals_principal_quota;
         PRAGMA user_version=15;",
    )
    .unwrap();
    drop(db);
    let db = Connection::open(&path).unwrap();
    schema::migrate(&db, 1_800_000_000_000).unwrap();
    assert_eq!(version(&db), schema::USER_VERSION);
    assert_eq!(rows(&db), before);
    assert_eq!(schema::log_epoch(&db).unwrap(), epoch);
    for index in [
        "memory_proposals_scope_quota",
        "memory_proposals_principal_quota",
    ] {
        assert_eq!(
            db.query_row(
                "SELECT count(*) FROM sqlite_schema WHERE type='index' AND name=?1",
                [index],
                |r| r.get::<_, i64>(0),
            )
            .unwrap(),
            1,
            "{index}"
        );
    }
    // Idempotent: the step tolerates an already-applied index.
    schema::migrate(&db, 1_800_000_000_000).unwrap();
    assert_eq!(rows(&db), before);
    assert_eq!(version(&db), schema::USER_VERSION);
    drop(db);
    let _ = std::fs::remove_dir_all(dir);
}

/// The issue's regression: a rollback on a scope holding 5,000 automatic
/// applies completes inside the production 50 ms writer deadline. Before the
/// covering indexes the two quota calls walked every proposal body and the
/// progress handler refused the command. Debug builds run the rest of the
/// command several times slower, so this is a scale test like
/// `measure_exact_merge_at_scope`; `rollback_quota_probes_read_the_covering_indexes`
/// guards the same plan in the default suite.
#[tokio::test]
#[ignore = "scale: 5,000 applies against the production 50 ms writer deadline"]
async fn rollback_completes_under_the_writer_deadline_with_five_thousand_applies() {
    let (mut h, id, _) = applied().await;
    let db = Connection::open(h.memory()).unwrap();
    seed(&db, 5_000, ADMIN);
    drop(db);
    // The production bound is 50 ms on a release build. A debug build runs the
    // rest of the command several times slower, so it gets three times the
    // bound, as `hardening_tests` sizes its deadlines.
    let lock_ms = if cfg!(debug_assertions) { 150 } else { 50 };
    h.store
        .as_mut()
        .unwrap()
        .configure_retrieval(&json!({"proposal_limits":{"lock_ms":lock_ms}}))
        .unwrap();
    let epoch = h.epoch().await;
    let start = Instant::now();
    let restored = h
        .call(
            ADMIN_TOKEN,
            json!({"action":"rollback","proposal_id":id,"base_epoch":epoch,"client_event_id":h.id()}),
        )
        .await;
    assert_eq!(
        restored.body["state"],
        "applied",
        "{restored:?} after {:.1} ms",
        ms(start)
    );
    eprintln!("rollback with 5,000 applies: {:.1} ms", ms(start));
}

/// Host-dependent measurement: `QUOTA_APPLIES=1000,2000,5000`.
#[test]
#[ignore = "host-dependent timing measurement"]
fn measure_rollback_quota_at_scale() {
    let counts: Vec<usize> = std::env::var("QUOTA_APPLIES")
        .unwrap_or_else(|_| "1000,2000,5000".into())
        .split(',')
        .filter_map(|v| v.trim().parse().ok())
        .collect();
    for count in counts {
        let h = Hub::new();
        let db = Connection::open(h.memory()).unwrap();
        seed(&db, count, ADMIN);
        eprintln!("applies={count}");
        eprintln!(
            "  scope plan: {}",
            plan(&db, hardening::SCOPE_USAGE, &[&FIXTURE_SCOPE, &"", &""])
        );
        eprintln!(
            "  principal plan: {}",
            plan(&db, hardening::PRINCIPAL_USAGE, &[&ADMIN, &"", &""])
        );
        eprintln!(
            "  scope usage: {:.2} ms",
            best(|| {
                let mut s = db.prepare_cached(hardening::SCOPE_USAGE).unwrap();
                s.query_row(rusqlite::params![FIXTURE_SCOPE, "", ""], |_| Ok(()))
                    .unwrap();
            })
        );
        eprintln!(
            "  principal usage: {:.2} ms",
            best(|| {
                let mut s = db.prepare_cached(hardening::PRINCIPAL_USAGE).unwrap();
                s.query_row(rusqlite::params![ADMIN, "", ""], |_| Ok(()))
                    .unwrap();
            })
        );
    }
}
