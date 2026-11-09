//! Slice 1 of the memory verification plan: per-card read counts, their
//! budgets, the version 10 migration and the retention sweep (#2387).
use super::{
    proposal_tests::{OLD, clean, op, submit},
    read_counts::*,
    tests::*,
    *,
};
use rusqlite::{Connection, params};
use serde_json::json;
use std::time::Duration;

fn limits(f: impl FnOnce(&mut Limits)) -> Limits {
    let mut limits = Limits::default();
    f(&mut limits);
    limits
}
fn memory_db() -> Connection {
    let db = Connection::open_in_memory().unwrap();
    schema::migrate(&db, 1_800_000_000_000).unwrap();
    db
}
/// The writer's queue probe when nothing is queued.
fn idle() -> bool {
    false
}
fn count(db: &Connection, sql: &str) -> i64 {
    db.query_row(sql, [], |r| r.get(0)).unwrap()
}
fn meta(db: &Connection, key: &str) -> Option<String> {
    db.query_row("SELECT value FROM memory_meta WHERE key=?1", [key], |r| {
        r.get(0)
    })
    .ok()
}
/// Drains the store's accumulator through a second connection, as the writer would.
fn flush(h: &Hub) -> Connection {
    let db = worker::open(&h.memory()).unwrap();
    let counts = &h.store.as_ref().unwrap().read_counts;
    while flush_once(&db, counts, &idle).unwrap().written > 0 {}
    db
}
fn day_row(db: &Connection, scope: &str, card: &str) -> Option<(i64, i64)> {
    db.query_row(
        "SELECT search_hits,card_reads FROM memory_read_days WHERE scope=?1 AND card_id=?2",
        params![scope, card],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )
    .ok()
}
fn search(query: &str) -> Json {
    json!({"action":"search","query":query})
}
/// A search that waits for the index to reach `seq`, so it sees every put.
fn search_after(query: &str, seq: i64) -> Json {
    json!({"action":"search","query":query,"min_seq":seq})
}
fn card(id: &str, offset: Option<i64>) -> Json {
    let mut body = json!({"action":"card","card_id":id,"scope":"fleet"});
    if let Some(offset) = offset {
        body["offset"] = json!(offset);
    }
    body
}

/// Every table and index except the version 10 and 11 tables, with their rows.
fn snapshot(db: &Connection) -> Vec<String> {
    let mut out = Vec::new();
    let objects: Vec<(String, String, Option<String>)> = db
        .prepare("SELECT type,name,sql FROM sqlite_schema WHERE name NOT LIKE 'memory_read_%' AND name NOT LIKE 'memory_verifier%' ORDER BY name")
        .unwrap()
        .query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))
        .unwrap()
        .collect::<rusqlite::Result<_>>()
        .unwrap();
    for (kind, name, sql) in objects {
        out.push(format!("{kind} {name} {sql:?}"));
        if kind != "table" {
            continue;
        }
        let filter = if name == "memory_meta" {
            " WHERE key NOT IN ('read_counts_since_day','read_days_rows','read_totals_rows')"
        } else {
            ""
        };
        let mut statement = db
            .prepare(&format!("SELECT * FROM {name}{filter}"))
            .unwrap();
        let columns = statement.column_count();
        let rows = statement
            .query_map([], |r| {
                (0..columns)
                    .map(|i| r.get::<_, rusqlite::types::Value>(i))
                    .collect::<rusqlite::Result<Vec<_>>>()
            })
            .unwrap()
            .collect::<rusqlite::Result<Vec<_>>>()
            .unwrap();
        out.push(format!("{name} {rows:?}"));
    }
    out
}

#[tokio::test]
async fn version_nine_migrates_to_ten_without_touching_other_tables() {
    let mut h = Hub::with(clean(), None);
    let a = h.put(ADMIN_TOKEN, "fleet", "a", None, OLD).await;
    let opened = submit(&h, ADMIN_TOKEN, vec![op("fleet", "a", rev(&a))]).await;
    assert_eq!(opened.status, 201, "{opened:?}");
    h.store.take();
    let db = Connection::open(h.memory()).unwrap();
    db.execute_batch(
        "DROP TABLE memory_read_days; DROP TABLE memory_read_totals;
         DROP TABLE memory_verifiers; DROP TABLE memory_verifier_credentials;
         DELETE FROM memory_meta WHERE key IN ('read_counts_since_day','read_days_rows','read_totals_rows');
         PRAGMA user_version=9;",
    )
    .unwrap();
    let before = snapshot(&db);
    let epoch = schema::log_epoch(&db).unwrap();
    let later = h.now() + 3 * DAY_MS;
    schema::migrate(&db, later).unwrap();
    assert_eq!(
        db.query_row("PRAGMA user_version", [], |r| r.get::<_, i64>(0))
            .unwrap(),
        schema::USER_VERSION
    );
    assert_eq!(snapshot(&db), before);
    assert_eq!(schema::log_epoch(&db).unwrap(), epoch);
    assert_eq!(
        count(
            &db,
            "SELECT count(*) FROM memory_proposals WHERE proposal_id IS NOT NULL"
        ),
        1
    );
    assert_eq!(
        meta(&db, "read_counts_since_day"),
        Some(day(later).to_string())
    );
    assert_eq!(count(&db, "SELECT count(*) FROM memory_read_days"), 0);
    // A second start is a no-op: nothing moves, the start day included.
    schema::migrate(&db, later + 10 * DAY_MS).unwrap();
    assert_eq!(snapshot(&db), before);
    assert_eq!(
        meta(&db, "read_counts_since_day"),
        Some(day(later).to_string())
    );
}

#[tokio::test]
async fn search_hits_count_once_per_card_and_card_reads_count_the_first_chunk() {
    let h = Hub::with(clean(), None);
    let mut seq = 0;
    for id in ["alpha", "beta"] {
        let text = format!("shared wording {id}");
        let put = h.put(NODE_TOKEN, "fleet", id, None, &text).await;
        assert_eq!(put.status, 201);
        seq = rev(&put);
    }
    let found = h
        .call(NODE_TOKEN, search_after("shared wording", seq))
        .await;
    assert_eq!(found.status, 200);
    assert_eq!(found.body["matches"].as_array().unwrap().len(), 2);
    for body in [
        card("alpha", None),
        card("alpha", Some(0)),
        card("alpha", Some(3)),
    ] {
        assert_eq!(h.call(NODE_TOKEN, body).await.status, 200);
    }
    let db = flush(&h);
    assert_eq!(day_row(&db, "fleet", "alpha"), Some((1, 2)));
    assert_eq!(day_row(&db, "fleet", "beta"), Some((1, 0)));
    let (first, last, total): (i64, i64, i64) = db
        .query_row(
            "SELECT first_read_day,last_read_day,total_reads FROM memory_read_totals WHERE card_id='alpha'",
            [],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        )
        .unwrap();
    assert_eq!((first, last, total), (day(h.now()), day(h.now()), 3));
    assert_eq!(meta(&db, "read_days_rows"), Some("2".into()));
    assert_eq!(meta(&db, "read_totals_rows"), Some("2".into()));
}

/// S23: a connector's own search (`query_source: "connector"`) is a read,
/// though push-filtered. Unsolicited push hits on `fleet_digest` and
/// `inbox_list` (`query_source: "push"`) are not.
#[tokio::test]
async fn connector_searches_count_and_unsolicited_pushes_do_not() {
    let h = Hub::with(clean(), None);
    let mut seq = 0;
    for id in ["alpha", "beta"] {
        let body = json!({"action":"import","scope":"fleet","card_id":id,
                          "text":format!("shared wording {id}"),"client_event_id":h.id()});
        let r = h.call(ADMIN_TOKEN, body).await;
        assert_eq!(r.status, 201, "{:?}", r.body);
        seq = r.body["event"]["seq"].as_i64().unwrap();
    }
    let source = |s: &str| json!({"action":"search","query":"shared wording","query_source":s,"min_seq":seq});
    let pushed = h.call(NODE_TOKEN, source("push")).await;
    assert_eq!(pushed.status, 200, "{:?}", pushed.body);
    assert_eq!(pushed.body["matches"].as_array().unwrap().len(), 2);
    let db = flush(&h);
    assert_eq!(day_row(&db, "fleet", "alpha"), None);
    assert_eq!(count(&db, "SELECT count(*) FROM memory_read_totals"), 0);
    let found = h.call(NODE_TOKEN, source("connector")).await;
    assert_eq!(found.status, 200, "{:?}", found.body);
    assert_eq!(found.body["matches"].as_array().unwrap().len(), 2);
    let db = flush(&h);
    assert_eq!(day_row(&db, "fleet", "alpha"), Some((1, 0)));
    assert_eq!(day_row(&db, "fleet", "beta"), Some((1, 0)));
    assert_eq!(
        count(&db, "SELECT sum(total_reads) FROM memory_read_totals"),
        2
    );
    // Unknown sources stay refused.
    let odd = h.call(NODE_TOKEN, source("hook")).await;
    assert_eq!((odd.status, code(&odd)), (400, "memory_query_invalid"));
}

#[tokio::test]
async fn eval_log_status_proposal_and_refused_reads_are_not_counted() {
    let h = Hub::with(clean(), None);
    let a = h.put(ADMIN_TOKEN, "fleet", "a", None, OLD).await;
    let opened = submit(&h, ADMIN_TOKEN, vec![op("fleet", "a", rev(&a))]).await;
    let id = opened.body["proposal_id"].clone();
    let mut eval = search_after("focused gate", rev(&a));
    eval["eval"] = json!(true);
    let evaluated = h.call(ADMIN_TOKEN, eval.clone()).await;
    assert_eq!(evaluated.body["matches"].as_array().map(Vec::len), Some(1));
    for (token, body, status) in [
        (ADMIN_TOKEN, eval, 200),
        (ADMIN_TOKEN, json!({"action":"log"}), 200),
        (ADMIN_TOKEN, json!({"action":"status"}), 200),
        (ADMIN_TOKEN, json!({"action":"proposal_list"}), 200),
        (
            ADMIN_TOKEN,
            json!({"action":"proposal_show","proposal_id":id}),
            200,
        ),
        // No grant for this principal: refused, so not a read.
        (AGENT_TOKEN, card("a", None), 403),
        (AGENT_TOKEN, search("focused gate"), 403),
        (ADMIN_TOKEN, card("missing", None), 404),
    ] {
        let reply = h.call(token, body.clone()).await;
        assert_eq!(reply.status, status, "{body} {reply:?}");
    }
    let store = h.store.as_ref().unwrap();
    assert!(store.read_counts.is_empty());
    // The hub's own push searches are not reads either.
    let pushed = store
        .push(
            &Credential::Admin,
            search("focused gate").as_object().unwrap(),
        )
        .await;
    assert_eq!(pushed.status, 200);
    assert!(store.read_counts.is_empty());
}

#[tokio::test]
async fn counts_hold_no_principal_query_or_time_of_day() {
    let h = Hub::with(clean(), None);
    let put = h
        .put(NODE_TOKEN, "fleet", "alpha", None, "zebra crossing")
        .await;
    let found = h
        .call(NODE_TOKEN, search_after("zebra crossing", rev(&put)))
        .await;
    assert_eq!(found.body["matches"].as_array().unwrap().len(), 1);
    h.call(NODE_TOKEN, card("alpha", None)).await;
    let db = flush(&h);
    for (table, expected) in [
        (
            "memory_read_days",
            &["scope", "card_id", "day", "search_hits", "card_reads"][..],
        ),
        (
            "memory_read_totals",
            &[
                "scope",
                "card_id",
                "first_read_day",
                "last_read_day",
                "total_reads",
            ][..],
        ),
    ] {
        let columns: Vec<String> = db
            .prepare(&format!("PRAGMA table_info({table})"))
            .unwrap()
            .query_map([], |r| r.get(1))
            .unwrap()
            .collect::<rusqlite::Result<_>>()
            .unwrap();
        assert_eq!(columns, expected);
        let dump: String = db
            .query_row(
                &format!(
                    "SELECT group_concat(quote({}), ',') FROM {table}",
                    expected.join(")||quote(")
                ),
                [],
                |r| r.get(0),
            )
            .unwrap();
        for secret in ["node-1", "zebra", "crossing", "hub:admin"] {
            assert!(!dump.contains(secret), "{table} holds {secret}: {dump}");
        }
    }
    // Days are whole UTC days, never a time.
    assert_eq!(
        count(&db, "SELECT day FROM memory_read_days"),
        h.now() / DAY_MS
    );
}

#[test]
fn one_principal_past_either_daily_limit_is_not_counted_and_the_limit_resets() {
    let counts = ReadCounts::new(limits(|l| {
        l.principal_card_daily = 3;
        l.principal_daily = 5;
    }));
    let now = 1_800_000_000_000;
    for _ in 0..5 {
        counts.record("p", &[("fleet", "a")], Kind::CardRead, now);
    }
    assert_eq!(counts.stats.budget("principal_card_daily"), 2);
    counts.record(
        "p",
        &[("fleet", "b"), ("fleet", "c"), ("fleet", "d")],
        Kind::SearchHit,
        now,
    );
    // Three on "a" and two of the others reach the daily limit of five.
    assert_eq!(counts.stats.budget("principal_daily"), 1);
    // Another principal is not held back.
    counts.record("q", &[("fleet", "a")], Kind::CardRead, now);
    let db = memory_db();
    flush_once(&db, &counts, &idle).unwrap();
    assert_eq!(day_row(&db, "fleet", "a"), Some((0, 4)));
    assert_eq!(day_row(&db, "fleet", "d"), None);
    // The next UTC day starts both limits over.
    counts.record("p", &[("fleet", "a")], Kind::CardRead, now + DAY_MS);
    flush_once(&db, &counts, &idle).unwrap();
    let tomorrow = day(now + DAY_MS);
    assert_eq!(
        count(
            &db,
            &format!(
                "SELECT card_reads FROM memory_read_days WHERE card_id='a' AND day={tomorrow}"
            )
        ),
        1
    );
    assert_eq!(counts.stats.dropped(), 0);
}

#[tokio::test]
async fn a_limited_read_is_still_served() {
    let mut h = Hub::with(clean(), None);
    h.store
        .as_mut()
        .unwrap()
        .configure_retrieval(&json!({"read_counts": {"principal_card_daily": 1}}))
        .unwrap();
    h.put(NODE_TOKEN, "fleet", "alpha", None, "text").await;
    for _ in 0..3 {
        let reply = h.call(NODE_TOKEN, card("alpha", None)).await;
        assert_eq!(reply.status, 200);
        assert_eq!(reply.body["card"]["text"], "text\n");
    }
    let db = flush(&h);
    assert_eq!(day_row(&db, "fleet", "alpha"), Some((0, 1)));
}

#[test]
fn configuration_refuses_unknown_keys_and_out_of_range_retention() {
    assert_eq!(Limits::parse(&json!({})).unwrap(), Limits::default());
    assert_eq!(Limits::default().retention_days, 35);
    for bad in [
        json!({"read_counts": {"retention_days": 6}}),
        json!({"read_counts": {"retention_days": 366}}),
        json!({"read_counts": {"flush_rows": 0}}),
        json!({"read_counts": {"surprise": 1}}),
        json!({"read_counts": 3}),
    ] {
        assert!(Limits::parse(&bad).is_err(), "{bad}");
    }
    let parsed =
        Limits::parse(&json!({"read_counts": {"retention_days": 7, "day_rows": 10}})).unwrap();
    assert_eq!((parsed.retention_days, parsed.day_rows), (7, 10));
}

#[test]
fn a_flush_stops_at_the_row_cap_and_resumes_without_double_counting() {
    let counts = ReadCounts::new(limits(|l| l.flush_rows = 10));
    let now = 1_800_000_000_000;
    for n in 0..25 {
        counts.record(
            &format!("p{n}"),
            &[("fleet", &format!("c{n:02}"))],
            Kind::SearchHit,
            now,
        );
    }
    let db = memory_db();
    let first = flush_once(&db, &counts, &idle).unwrap();
    assert_eq!(
        first,
        Flush {
            written: 10,
            more: true
        }
    );
    // Oldest first.
    assert_eq!(
        count(
            &db,
            "SELECT count(*) FROM memory_read_days WHERE card_id<'c10'"
        ),
        10
    );
    assert_eq!(flush_once(&db, &counts, &idle).unwrap().written, 10);
    assert_eq!(
        flush_once(&db, &counts, &idle).unwrap(),
        Flush {
            written: 5,
            more: false
        }
    );
    assert_eq!(
        count(&db, "SELECT sum(search_hits) FROM memory_read_days"),
        25
    );
    assert_eq!(
        count(&db, "SELECT sum(total_reads) FROM memory_read_totals"),
        25
    );
    assert!(counts.stats.budget("flush_rows") >= 2);
}

#[test]
fn a_flush_stops_at_the_time_budget_commits_and_resumes() {
    let counts = ReadCounts::new(limits(|l| {
        l.flush_ms = 1;
        l.flush_rows = 100_000;
        l.principal_daily = 100_000;
        l.principals = 1;
        l.principal_pairs = 20_000;
        l.limiter_pairs = 20_000;
    }));
    let now = 1_800_000_000_000;
    let ids: Vec<String> = (0..20_000).map(|n| format!("c{n:05}")).collect();
    let cards: Vec<(&str, &str)> = ids.iter().map(|id| ("fleet", id.as_str())).collect();
    counts.record("p", &cards, Kind::SearchHit, now);
    let db = memory_db();
    let first = flush_once(&db, &counts, &idle).unwrap();
    assert!(
        first.more && first.written > 0 && first.written < 20_000,
        "{first:?}"
    );
    assert_eq!(
        count(&db, "SELECT count(*) FROM memory_read_days"),
        first.written as i64
    );
    assert_eq!(counts.stats.budget("flush_ms"), 1);
    let mut rounds = 1;
    while flush_once(&db, &counts, &idle).unwrap().written > 0 {
        rounds += 1;
    }
    assert!(rounds > 1);
    assert_eq!(
        count(&db, "SELECT sum(search_hits) FROM memory_read_days"),
        20_000
    );
    assert_eq!(meta(&db, "read_days_rows"), Some("20000".into()));
}

#[test]
fn a_failed_flush_returns_its_keys_and_counts_nothing_twice() {
    let dir = temp_dir();
    let path = dir.join("memory.db");
    let db = worker::open_file(&path, true).unwrap();
    schema::migrate(&db, 1_800_000_000_000).unwrap();
    let counts = ReadCounts::default();
    counts.record(
        "p",
        &[("fleet", "a"), ("fleet", "b")],
        Kind::SearchHit,
        1_800_000_000_000,
    );
    // Another connection holds the write lock past the flush's lock wait.
    let holder = Connection::open(&path).unwrap();
    holder.execute_batch("BEGIN IMMEDIATE").unwrap();
    assert!(flush_once(&db, &counts, &idle).is_err());
    assert_eq!(counts.len(), 2);
    holder.execute_batch("ROLLBACK").unwrap();
    assert_eq!(flush_once(&db, &counts, &idle).unwrap().written, 2);
    assert!(counts.is_empty());
    assert_eq!(
        count(&db, "SELECT sum(search_hits) FROM memory_read_days"),
        2
    );
    let _ = std::fs::remove_dir_all(dir);
}

#[test]
fn a_full_accumulator_never_flushes_while_a_write_waits() {
    let counts = ReadCounts::new(limits(|l| l.accumulator_keys = 4));
    let now = 1_800_000_000_000;
    counts.record("p", &[("fleet", "a")], Kind::SearchHit, now);
    assert!(due(true, false));
    assert!(!due(true, true));
    assert!(!due(false, false));
    // Full: new keys are dropped and counted, and a waiting write still wins.
    let cards = [
        ("fleet", "b"),
        ("fleet", "c"),
        ("fleet", "d"),
        ("fleet", "e"),
    ];
    counts.record("p", &cards, Kind::SearchHit, now);
    assert_eq!(counts.len(), 4);
    assert!(counts.pressed());
    assert_eq!(counts.stats.dropped(), 1);
    assert!(!due(true, true));
}

/// A probe that reports a queued job from its `after`th call on.
fn arrives_after(after: usize) -> impl Fn() -> bool {
    let calls = std::cell::Cell::new(0);
    move || {
        calls.set(calls.get() + 1);
        calls.get() > after
    }
}

#[test]
fn a_flush_releases_the_lock_for_a_queued_write_and_commits_nothing() {
    let dir = temp_dir();
    let path = dir.join("memory.db");
    let db = worker::open_file(&path, true).unwrap();
    schema::migrate(&db, 1_800_000_000_000).unwrap();
    let counts = ReadCounts::default();
    let cards: Vec<String> = (0..10).map(|n| format!("c{n}")).collect();
    let cards: Vec<(&str, &str)> = cards.iter().map(|c| ("fleet", c.as_str())).collect();
    counts.record("p", &cards, Kind::SearchHit, 1_800_000_000_000);
    // A write already queued: the flush never takes the lock.
    let queued = flush_once(&db, &counts, &|| true).unwrap();
    assert_eq!(
        queued,
        Flush {
            written: 0,
            more: true
        }
    );
    assert!(db.is_autocommit());
    assert_eq!(counts.len(), 10);
    // A write arriving mid-flush: the flush rolls back at the next row
    // boundary, and another connection can take the write lock at once.
    let midway = flush_once(&db, &counts, &arrives_after(4)).unwrap();
    assert_eq!(
        midway,
        Flush {
            written: 0,
            more: true
        }
    );
    assert!(db.is_autocommit());
    let writer = Connection::open(&path).unwrap();
    writer.execute_batch("BEGIN IMMEDIATE; ROLLBACK").unwrap();
    assert_eq!(count(&db, "SELECT count(*) FROM memory_read_days"), 0);
    assert_eq!(meta(&db, "read_days_rows"), Some("0".into()));
    assert_eq!(counts.stats.budget("write_waiting"), 1);
    // Every key is back, oldest first, and lands exactly once when idle.
    assert_eq!(counts.len(), 10);
    assert_eq!(flush_once(&db, &counts, &idle).unwrap().written, 10);
    assert_eq!(
        count(&db, "SELECT sum(search_hits) FROM memory_read_days"),
        10
    );
    let _ = std::fs::remove_dir_all(dir);
}

#[test]
fn a_sweep_rolls_back_for_a_queued_write() {
    let counts = ReadCounts::default();
    let now = 1_800_000_000_000;
    let db = memory_db();
    seed_days(&db, &[day(now) - 100], 1_000);
    let mut cursor = SweepCursor::default();
    let swept = sweep(&db, &counts, now, &mut cursor, &arrives_after(2)).unwrap();
    assert!(swept.more && db.is_autocommit());
    assert_eq!(count(&db, "SELECT count(*) FROM memory_read_days"), 1_000);
    assert_eq!(meta(&db, "read_days_rows"), Some("1000".into()));
    assert_eq!(counts.stats.budget("write_waiting"), 1);
    assert_eq!(
        sweep(&db, &counts, now, &mut cursor, &idle).unwrap().days,
        1_000
    );
}

#[tokio::test]
async fn a_queued_write_takes_the_lock_before_a_flush_of_a_full_accumulator() {
    use std::sync::{
        Mutex as StdMutex, OnceLock,
        atomic::{AtomicBool, Ordering},
        mpsc,
    };
    let counts: Arc<OnceLock<Arc<ReadCounts>>> = Arc::default();
    // Each committed write batch records how many keys had been flushed.
    let commits: Arc<StdMutex<Vec<u64>>> = Arc::default();
    let armed = Arc::new(AtomicBool::new(false));
    let paused = Arc::new(tokio::sync::Notify::new());
    let (release, gate) = mpsc::channel::<()>();
    let gate = Arc::new(StdMutex::new(Some(gate)));
    let hook: writer::Hook = {
        let (counts, commits, armed, paused) = (
            counts.clone(),
            commits.clone(),
            armed.clone(),
            paused.clone(),
        );
        Arc::new(move |step| {
            if step == writer::Step::Committed {
                let flushed = counts.get().map_or(0, |c| c.stats.flushed());
                commits.lock().unwrap().push(flushed);
            }
            // Hold the first armed batch until the flush request and the next
            // write are both queued behind it.
            if step == writer::Step::Applied && armed.swap(false, Ordering::SeqCst) {
                paused.notify_one();
                let gate = gate.lock().unwrap().take().unwrap();
                gate.recv().unwrap();
            }
        })
    };
    let mut h = Hub::with(clean(), Some(hook));
    h.store
        .as_mut()
        .unwrap()
        .configure_retrieval(&json!({"read_counts": {"accumulator_keys": 4}}))
        .unwrap();
    let store = h.store.as_ref().unwrap();
    counts.set(store.read_counts.clone()).ok().unwrap();
    let cards = [
        ("fleet", "a"),
        ("fleet", "b"),
        ("fleet", "c"),
        ("fleet", "d"),
    ];
    store
        .read_counts
        .record("p", &cards, Kind::SearchHit, h.now());
    assert!(store.read_counts.pressed());
    commits.lock().unwrap().clear();
    armed.store(true, Ordering::SeqCst);
    let first = h.put(NODE_TOKEN, "fleet", "one", None, "one");
    let second = async {
        paused.notified().await;
        // Queues the flush request (and the retention sweep) ahead of the write.
        assert!(store.sweep());
        let put = h.put(NODE_TOKEN, "fleet", "two", None, "two");
        tokio::pin!(put);
        let started = std::time::Instant::now();
        while store.writer.queued() < 3 {
            assert!(
                started.elapsed() < Duration::from_secs(10),
                "write never queued"
            );
            tokio::select! {
                biased;
                reply = &mut put => panic!("write answered while the writer was held: {reply:?}"),
                () = tokio::task::yield_now() => {}
            }
        }
        release.send(()).unwrap();
        put.await
    };
    let (one, two) = tokio::join!(first, second);
    assert_eq!((one.status, two.status), (201, 201), "{one:?} {two:?}");
    // Both write batches committed before any read count was flushed.
    assert_eq!(*commits.lock().unwrap(), [0, 0]);
    let started = std::time::Instant::now();
    while store.read_counts.stats.flushed() < 4 {
        assert!(started.elapsed() < Duration::from_secs(10), "no idle flush");
        tokio::task::yield_now().await;
        std::thread::sleep(Duration::from_millis(5));
    }
    assert_eq!(*commits.lock().unwrap(), [0, 0]);
}

#[tokio::test]
async fn the_writer_flushes_on_request() {
    let h = Hub::with(clean(), None);
    h.put(NODE_TOKEN, "fleet", "alpha", None, "text").await;
    h.call(NODE_TOKEN, card("alpha", None)).await;
    let store = h.store.as_ref().unwrap();
    assert!(store.sweep());
    let started = std::time::Instant::now();
    while store.read_counts.stats.flushed() == 0 {
        assert!(started.elapsed() < Duration::from_secs(10), "no flush");
        std::thread::sleep(Duration::from_millis(10));
    }
    let db = Connection::open(h.memory()).unwrap();
    assert_eq!(day_row(&db, "fleet", "alpha"), Some((0, 1)));
    assert!(
        store
            .read_count_metrics()
            .contains("brigade_hub_memory_read_count_flushed_total 1\n")
    );
}

#[test]
fn both_row_caps_stop_new_keys_while_existing_rows_update() {
    let counts = ReadCounts::new(limits(|l| {
        l.day_rows = 2;
        l.total_rows = 3;
    }));
    let now = 1_800_000_000_000;
    let db = memory_db();
    counts.record("p", &[("fleet", "a"), ("fleet", "b")], Kind::SearchHit, now);
    flush_once(&db, &counts, &idle).unwrap();
    counts.record(
        "q",
        &[("fleet", "a"), ("fleet", "c"), ("fleet", "d")],
        Kind::SearchHit,
        now,
    );
    flush_once(&db, &counts, &idle).unwrap();
    assert_eq!(day_row(&db, "fleet", "a"), Some((2, 0)));
    assert_eq!(day_row(&db, "fleet", "c"), None);
    assert_eq!(count(&db, "SELECT count(*) FROM memory_read_days"), 2);
    assert_eq!(count(&db, "SELECT count(*) FROM memory_read_totals"), 3);
    assert_eq!(
        count(
            &db,
            "SELECT total_reads FROM memory_read_totals WHERE card_id='a'"
        ),
        2
    );
    assert_eq!(counts.stats.budget("day_rows"), 2);
    assert_eq!(counts.stats.budget("total_rows"), 1);
    assert_eq!(counts.stats.dropped(), 2);
    assert_eq!(meta(&db, "read_days_rows"), Some("2".into()));
    assert_eq!(meta(&db, "read_totals_rows"), Some("3".into()));
}

#[tokio::test]
async fn reads_are_unaffected_when_the_accumulator_is_full_or_locked() {
    let mut h = Hub::with(clean(), None);
    h.store
        .as_mut()
        .unwrap()
        .configure_retrieval(&json!({"read_counts": {"accumulator_keys": 1}}))
        .unwrap();
    let mut seq = 0;
    for id in ["alpha", "beta", "gamma"] {
        let text = format!("shared wording {id}");
        seq = rev(&h.put(NODE_TOKEN, "fleet", id, None, &text).await);
    }
    let found = h
        .call(NODE_TOKEN, search_after("shared wording", seq))
        .await;
    assert_eq!(found.status, 200);
    assert_eq!(found.body["matches"].as_array().unwrap().len(), 3);
    let store = h.store.as_ref().unwrap();
    assert_eq!(store.read_counts.len(), 1);
    assert_eq!(store.read_counts.stats.budget("accumulator_keys"), 2);
    assert_eq!(store.read_counts.stats.dropped(), 2);
    // A held accumulator lock loses the count, never the read.
    let held = store.read_counts.hold();
    let reply = h.call(NODE_TOKEN, card("alpha", None)).await;
    drop(held);
    assert_eq!(reply.status, 200);
    assert_eq!(store.read_counts.stats.budget("contended"), 1);
    assert_eq!(store.read_counts.stats.dropped(), 3);
    assert!(
        store
            .read_count_metrics()
            .contains("brigade_hub_memory_read_count_dropped_total 3\n")
    );
}

fn seed_days(db: &Connection, days: &[i64], cards: usize) {
    let tx = db.unchecked_transaction().unwrap();
    for d in days {
        for n in 0..cards {
            tx.execute(
                "INSERT INTO memory_read_days VALUES('fleet',?1,?2,1,0)",
                params![format!("c{n:05}"), d],
            )
            .unwrap();
        }
    }
    let rows = (days.len() * cards) as i64;
    tx.execute(
        "UPDATE memory_meta SET value=?1 WHERE key='read_days_rows'",
        [rows.to_string()],
    )
    .unwrap();
    tx.commit().unwrap();
}

#[test]
fn the_sweep_stays_within_its_row_budget_and_finishes_over_several_ticks() {
    let counts = ReadCounts::default();
    let now = 1_800_000_000_000;
    let today = day(now);
    let db = memory_db();
    // 5,000 expired rows across two old days, and 10 kept rows on the oldest kept day.
    seed_days(&db, &[today - 50, today - 35], 2_500);
    seed_days(&db, &[today - 34], 10);
    db.execute(
        "UPDATE memory_meta SET value='5010' WHERE key='read_days_rows'",
        [],
    )
    .unwrap();
    let mut cursor = SweepCursor::default();
    let first = sweep(&db, &counts, now, &mut cursor, &idle).unwrap();
    assert_eq!((first.days, first.more), (2_000, true));
    // Oldest first.
    assert_eq!(
        count(
            &db,
            &format!(
                "SELECT count(*) FROM memory_read_days WHERE day={}",
                today - 50
            )
        ),
        500
    );
    let second = sweep(&db, &counts, now, &mut cursor, &idle).unwrap();
    assert_eq!((second.days, second.more), (2_000, true));
    let third = sweep(&db, &counts, now, &mut cursor, &idle).unwrap();
    assert_eq!((third.days, third.more), (1_000, false));
    assert_eq!(count(&db, "SELECT count(*) FROM memory_read_days"), 10);
    assert_eq!(meta(&db, "read_days_rows"), Some("10".into()));
    assert_eq!(counts.stats.budget("sweep_rows"), 2);
}

#[test]
fn the_sweep_stops_at_its_time_budget() {
    let counts = ReadCounts::new(limits(|l| {
        l.sweep_ms = 1;
        l.sweep_rows = 100_000;
    }));
    let now = 1_800_000_000_000;
    let db = memory_db();
    seed_days(&db, &[day(now) - 100], 60_000);
    let mut cursor = SweepCursor::default();
    let first = sweep(&db, &counts, now, &mut cursor, &idle).unwrap();
    assert!(first.more && first.days < 60_000, "{first:?}");
    assert_eq!(counts.stats.budget("sweep_ms"), 1);
    let mut ticks = 1;
    while sweep(&db, &counts, now, &mut cursor, &idle).unwrap().more {
        ticks += 1;
    }
    assert!(ticks > 1);
    assert_eq!(count(&db, "SELECT count(*) FROM memory_read_days"), 0);
    assert_eq!(meta(&db, "read_days_rows"), Some("0".into()));
}

#[test]
fn the_sweep_removes_totals_of_retired_cards_resuming_across_ticks() {
    let counts = ReadCounts::new(limits(|l| l.sweep_rows = 3));
    let now = 1_800_000_000_000;
    let db = memory_db();
    db.execute("INSERT INTO memory_blobs VALUES('h',x'',0,'t')", [])
        .unwrap();
    for (n, state) in ["retired", "live", "retired", "retired", "live", "retired"]
        .iter()
        .enumerate()
    {
        db.execute(
            "INSERT INTO memory_cards(scope,card_id,revision,content_hash,state,trust,writer,writer_class,created_at,updated_at) VALUES('fleet',?1,1,'h',?2,'reviewed','w','admin','t','t')",
            params![format!("c{n}"), state],
        )
        .unwrap();
        db.execute(
            "INSERT INTO memory_read_totals VALUES('fleet',?1,1,1,1)",
            [format!("c{n}")],
        )
        .unwrap();
    }
    // A totals row whose card no longer exists goes too.
    db.execute(
        "INSERT INTO memory_read_totals VALUES('fleet','zz',1,1,1)",
        [],
    )
    .unwrap();
    db.execute(
        "UPDATE memory_meta SET value='7' WHERE key='read_totals_rows'",
        [],
    )
    .unwrap();
    let mut cursor = SweepCursor::default();
    let first = sweep(&db, &counts, now, &mut cursor, &idle).unwrap();
    assert_eq!((first.examined, first.totals), (3, 2));
    assert_eq!(cursor, SweepCursor(Some(("fleet".into(), "c2".into()))));
    let second = sweep(&db, &counts, now, &mut cursor, &idle).unwrap();
    assert_eq!((second.examined, second.totals), (3, 2));
    let third = sweep(&db, &counts, now, &mut cursor, &idle).unwrap();
    assert_eq!((third.examined, third.totals), (1, 1));
    assert_eq!(cursor, SweepCursor(None));
    let kept: Vec<String> = db
        .prepare("SELECT card_id FROM memory_read_totals ORDER BY card_id")
        .unwrap()
        .query_map([], |r| r.get(0))
        .unwrap()
        .collect::<rusqlite::Result<_>>()
        .unwrap();
    assert_eq!(kept, ["c1", "c4"]);
    assert_eq!(meta(&db, "read_totals_rows"), Some("2".into()));
}

#[test]
fn retention_keeps_thirty_five_days_by_default() {
    let counts = ReadCounts::default();
    let now = 1_800_000_000_000;
    let today = day(now);
    let db = memory_db();
    seed_days(&db, &[today - 35, today - 34, today], 1);
    sweep(&db, &counts, now, &mut SweepCursor::default(), &idle).unwrap();
    assert_eq!(
        count(&db, "SELECT min(day) FROM memory_read_days"),
        today - 34
    );
    assert_eq!(count(&db, "SELECT count(*) FROM memory_read_days"), 2);
}

#[test]
fn the_hard_stop_sits_past_the_budget_with_a_floor() {
    let ms = Duration::from_millis;
    // The default 20 ms budget is stopped 100 ms after it starts.
    assert_eq!(ms(20) + hard_margin(ms(20)), ms(100));
    assert_eq!(hard_margin(ms(1)), ms(80));
    assert_eq!(hard_margin(ms(100)), ms(400));
}

fn distinct(prefix: &str, n: usize) -> Vec<String> {
    (0..n).map(|i| format!("{prefix}{i:05}")).collect()
}
fn in_fleet(ids: &[String]) -> Vec<(&str, &str)> {
    ids.iter().map(|id| ("fleet", id.as_str())).collect()
}

#[test]
fn one_principal_filling_its_share_never_stops_another_being_counted() {
    let counts = ReadCounts::new(limits(|l| {
        l.principals = 4;
        l.principal_pairs = 8;
        l.limiter_pairs = 32;
    }));
    let now = 1_800_000_000_000;
    let heavy = distinct("h", 100);
    counts.record("heavy", &in_fleet(&heavy), Kind::SearchHit, now);
    assert_eq!(counts.stats.budget("principal_pairs"), 92);
    // The second principal still has its whole share.
    let light = distinct("l", 8);
    counts.record("light", &in_fleet(&light), Kind::CardRead, now);
    assert_eq!(counts.len(), 16);
    assert_eq!(counts.stats.budget("limiter_pairs"), 0);
    assert_eq!(counts.stats.dropped(), 0);
    let db = memory_db();
    flush_once(&db, &counts, &idle).unwrap();
    assert_eq!(
        count(&db, "SELECT sum(card_reads) FROM memory_read_days"),
        8
    );
    assert_eq!(
        count(&db, "SELECT sum(search_hits) FROM memory_read_days"),
        8
    );
}

fn counted_cards(db: &Connection, filter: &str) -> Vec<String> {
    db.prepare(&format!(
        "SELECT card_id FROM memory_read_days WHERE {filter} ORDER BY card_id"
    ))
    .unwrap()
    .query_map([], |r| r.get(0))
    .unwrap()
    .collect::<rusqlite::Result<_>>()
    .unwrap()
}

#[test]
fn a_full_table_evicts_and_denies_the_heaviest_before_any_light_user() {
    // Two principals, 64 pairs each. A principal under 4 pairs is light.
    let counts = ReadCounts::new(limits(|l| {
        l.principals = 2;
        l.principal_pairs = 64;
        l.limiter_pairs = 128;
    }));
    assert_eq!(counts.limits().light_pairs(), 4);
    let now = 1_800_000_000_000;
    // The light principal is the least recently active, and still stays.
    counts.record("light", &[("fleet", "a")], Kind::SearchHit, now);
    let heavy = distinct("h", 10);
    counts.record("heavy", &in_fleet(&heavy), Kind::SearchHit, now);
    counts.record("new", &[("fleet", "b")], Kind::SearchHit, now);
    assert_eq!(counts.stats.budget("principal_evicted"), 1);
    assert_eq!(counts.stats.budget("light_evicted"), 0);
    counts.record("light", &[("fleet", "c")], Kind::SearchHit, now);
    // The evicted heavy principal is served but not counted for the rest of
    // the day, so eviction never refills its budget.
    counts.record("heavy", &[("fleet", "d")], Kind::SearchHit, now);
    assert_eq!(counts.stats.budget("evicted_reads"), 1);
    // Only light users left: the least recently active one ("new") makes room
    // without being denied, and comes back on its next read.
    counts.record("late", &[("fleet", "e")], Kind::SearchHit, now);
    counts.record("light", &[("fleet", "f")], Kind::SearchHit, now);
    counts.record("new", &[("fleet", "g")], Kind::SearchHit, now);
    assert_eq!(counts.stats.budget("light_evicted"), 2);
    assert_eq!(counts.stats.budget("principals_full"), 0);
    assert_eq!(counts.stats.dropped(), 0);
    let db = memory_db();
    flush_once(&db, &counts, &idle).unwrap();
    assert_eq!(
        counted_cards(&db, "card_id NOT LIKE 'h%'"),
        ["a", "b", "c", "e", "f", "g"]
    );
    // The next UTC day starts everyone over.
    counts.record("heavy", &[("fleet", "d")], Kind::SearchHit, now + DAY_MS);
    assert_eq!(counts.len(), 1);
}

#[test]
fn sixty_four_light_principals_never_leave_a_newcomer_uncounted() {
    let counts = ReadCounts::default();
    let now = 1_800_000_000_000;
    for n in 0..64 {
        counts.record(
            &format!("p{n}"),
            &[("fleet", "shared")],
            Kind::SearchHit,
            now,
        );
    }
    counts.record("newcomer", &[("fleet", "fresh")], Kind::SearchHit, now);
    assert_eq!(counts.stats.budget("light_evicted"), 1);
    assert_eq!(counts.stats.budget("principals_full"), 0);
    // The evicted principal (the least recently active, p0) is counted again.
    counts.record("p0", &[("fleet", "again")], Kind::SearchHit, now);
    assert_eq!(counts.stats.budget("light_evicted"), 2);
    let db = memory_db();
    flush_once(&db, &counts, &idle).unwrap();
    assert_eq!(day_row(&db, "fleet", "shared"), Some((64, 0)));
    assert_eq!(day_row(&db, "fleet", "fresh"), Some((1, 0)));
    assert_eq!(day_row(&db, "fleet", "again"), Some((1, 0)));
    assert_eq!(counts.stats.dropped(), 0);
}

#[test]
fn a_re_tracked_light_principal_stays_within_its_bound() {
    // One slot, light under 2 cards, at most 3 counts per card and 5 a day.
    let counts = ReadCounts::new(limits(|l| {
        l.principals = 1;
        l.principal_pairs = 32;
        l.limiter_pairs = 32;
        l.principal_card_daily = 3;
        l.principal_daily = 5;
    }));
    let now = 1_800_000_000_000;
    let reread = |who: &str, n: usize| {
        for _ in 0..n {
            counts.record(who, &[("fleet", "a")], Kind::CardRead, now);
        }
    };
    reread("p", 10);
    // "q" moves "p" out. "p" then moves "q" out and is tracked again.
    reread("q", 1);
    reread("p", 10);
    // Its per-card memory restarted, but its daily total came back with it:
    // 3 before, then only 2 more of the 5 a day.
    let db = memory_db();
    flush_once(&db, &counts, &idle).unwrap();
    assert_eq!(day_row(&db, "fleet", "a"), Some((0, 3 + 1 + 2)));
    // Extra counts over the per-card cap: at most (light_pairs - 1) x
    // principal_card_daily, here 1 x 3. "p" got 5 on one card against a cap of 3.
    let limits = counts.limits();
    let bound = (limits.light_pairs() as u32 - 1) * limits.principal_card_daily;
    assert_eq!(bound, 3);
    assert!(5 - limits.principal_card_daily <= bound);
    // Each light principal leaves this way once a day. With both evicted once,
    // a further newcomer is not counted, and the loss is.
    reread("q", 1);
    assert_eq!(counts.stats.budget("light_evicted"), 2);
    assert_eq!(counts.stats.budget("principals_full"), 1);
    assert_eq!(counts.stats.dropped(), 1);
}

#[test]
fn the_limiter_bound_must_cover_every_principal_share() {
    let defaults = Limits::default();
    assert_eq!(
        (
            defaults.principals,
            defaults.principal_pairs,
            defaults.limiter_pairs
        ),
        (64, 2_048, 131_072)
    );
    assert_eq!(defaults.light_pairs(), 128);
    let short = json!({"read_counts": {"principals": 65}});
    let error = Limits::parse(&short).unwrap_err();
    assert!(error.contains("limiter_pairs"), "{error}");
    let raised = json!({"read_counts": {"principals": 65, "limiter_pairs": 133_120}});
    assert_eq!(Limits::parse(&raised).unwrap().principals, 65);
}

#[test]
fn rereads_during_flushes_never_grow_the_ordering_past_the_keys() {
    let counts = ReadCounts::new(limits(|l| {
        l.flush_rows = 1;
        l.principal_card_daily = 1_000_000;
        l.principal_daily = 1_000_000;
    }));
    let now = 1_800_000_000_000;
    let cards = [("fleet", "a"), ("fleet", "b"), ("fleet", "c")];
    counts.record("p", &cards, Kind::SearchHit, now);
    for round in 0..1_000 {
        let (_, batch) = counts.take(1);
        assert_eq!(batch.len(), 1);
        // The card is read again while its key is out for a flush.
        let (scope, card) = (batch[0].0.scope.clone(), batch[0].0.card_id.clone());
        counts.record("p", &[(&scope, &card)], Kind::SearchHit, now);
        counts.restore(&batch);
        assert_eq!(counts.order_len(), counts.len(), "round {round}");
        assert_eq!(counts.len(), 3);
    }
    let db = memory_db();
    while flush_once(&db, &counts, &idle).unwrap().written > 0 {}
    assert_eq!(
        count(&db, "SELECT sum(search_hits) FROM memory_read_days"),
        1_003
    );
    assert_eq!(counts.order_len(), 0);
}

fn seed_retired_totals(db: &Connection, n: usize) {
    let tx = db.unchecked_transaction().unwrap();
    tx.execute(
        "INSERT OR IGNORE INTO memory_blobs VALUES('h',x'',0,'t')",
        [],
    )
    .unwrap();
    for card in distinct("r", n) {
        tx.execute(
            "INSERT INTO memory_cards(scope,card_id,revision,content_hash,state,trust,writer,writer_class,created_at,updated_at) VALUES('fleet',?1,1,'h','retired','reviewed','w','admin','t','t')",
            [&card],
        )
        .unwrap();
        tx.execute(
            "INSERT INTO memory_read_totals VALUES('fleet',?1,1,1,1)",
            [&card],
        )
        .unwrap();
    }
    tx.execute(
        "UPDATE memory_meta SET value=?1 WHERE key='read_totals_rows'",
        [n.to_string()],
    )
    .unwrap();
    tx.commit().unwrap();
}

#[test]
fn a_write_queued_mid_totals_sweep_commits_before_the_sweep_finishes_its_page() {
    let dir = temp_dir();
    let path = dir.join("memory.db");
    let db = worker::open_file(&path, true).unwrap();
    let now = 1_800_000_000_000;
    schema::migrate(&db, now).unwrap();
    seed_retired_totals(&db, 10);
    let counts = ReadCounts::default();
    let mut cursor = SweepCursor::default();
    // Probe calls: before the lock, the day loop, then one per totals row. The
    // job arrives at the third totals row, after two deletes.
    let swept = sweep(&db, &counts, now, &mut cursor, &arrives_after(4)).unwrap();
    assert!(swept.more && db.is_autocommit());
    assert_eq!(cursor, SweepCursor::default());
    assert_eq!(counts.stats.budget("write_waiting"), 1);
    // The queued write takes the lock and commits while the page is unfinished.
    let writer = Connection::open(&path).unwrap();
    writer
        .execute_batch("BEGIN IMMEDIATE; INSERT INTO memory_meta VALUES('probe','1'); COMMIT;")
        .unwrap();
    assert_eq!(count(&db, "SELECT count(*) FROM memory_read_totals"), 10);
    assert_eq!(meta(&db, "read_totals_rows"), Some("10".into()));
    // Idle again, the sweep redoes the page in full.
    assert_eq!(
        sweep(&db, &counts, now, &mut cursor, &idle).unwrap().totals,
        10
    );
    assert_eq!(meta(&db, "read_totals_rows"), Some("0".into()));
    let _ = std::fs::remove_dir_all(dir);
}

#[test]
fn a_totals_sweep_at_its_time_budget_commits_partial_progress_and_resumes() {
    let counts = ReadCounts::new(limits(|l| {
        l.sweep_ms = 1;
        l.sweep_rows = 100_000;
    }));
    let now = 1_800_000_000_000;
    let db = memory_db();
    seed_retired_totals(&db, 30_000);
    let mut cursor = SweepCursor::default();
    let first = sweep(&db, &counts, now, &mut cursor, &idle).unwrap();
    assert!(
        first.more && first.totals > 0 && first.totals < 30_000,
        "{first:?}"
    );
    assert_eq!(first.examined, first.totals);
    assert!(db.is_autocommit());
    // What it did is committed and the cursor sits on the last row it examined.
    let left = 30_000 - first.totals as i64;
    assert_eq!(count(&db, "SELECT count(*) FROM memory_read_totals"), left);
    assert_eq!(meta(&db, "read_totals_rows"), Some(left.to_string()));
    let last = format!("r{:05}", first.examined - 1);
    assert_eq!(cursor, SweepCursor(Some(("fleet".into(), last))));
    assert_eq!(counts.stats.budget("sweep_ms"), 1);
    let mut ticks = 1;
    while sweep(&db, &counts, now, &mut cursor, &idle).unwrap().more {
        ticks += 1;
    }
    assert!(ticks > 1);
    assert_eq!(count(&db, "SELECT count(*) FROM memory_read_totals"), 0);
    assert_eq!(meta(&db, "read_totals_rows"), Some("0".into()));
}

#[test]
fn a_totals_stage_that_fills_the_row_budget_asks_for_another_sweep() {
    let counts = ReadCounts::new(limits(|l| l.sweep_rows = 5));
    let now = 1_800_000_000_000;
    let db = memory_db();
    seed_retired_totals(&db, 6);
    let mut cursor = SweepCursor::default();
    let first = sweep(&db, &counts, now, &mut cursor, &idle).unwrap();
    assert_eq!((first.examined, first.totals, first.more), (5, 5, true));
    let second = sweep(&db, &counts, now, &mut cursor, &idle).unwrap();
    assert_eq!((second.examined, second.totals, second.more), (1, 1, false));
    assert_eq!(cursor, SweepCursor(None));
    assert_eq!(count(&db, "SELECT count(*) FROM memory_read_totals"), 0);
}

#[tokio::test]
async fn the_writer_queues_the_next_sweep_until_the_totals_are_done() {
    let mut h = Hub::with(clean(), None);
    h.store
        .as_mut()
        .unwrap()
        .configure_retrieval(&json!({"read_counts": {"sweep_rows": 5}}))
        .unwrap();
    seed_retired_totals(&Connection::open(h.memory()).unwrap(), 6);
    let store = h.store.as_ref().unwrap();
    assert!(store.sweep());
    let started = std::time::Instant::now();
    while store.read_counts.stats.swept() < 6 {
        assert!(
            started.elapsed() < Duration::from_secs(10),
            "second sweep never ran"
        );
        std::thread::sleep(Duration::from_millis(5));
    }
    let db = Connection::open(h.memory()).unwrap();
    assert_eq!(count(&db, "SELECT count(*) FROM memory_read_totals"), 0);
    assert_eq!(meta(&db, "read_totals_rows"), Some("0".into()));
}
