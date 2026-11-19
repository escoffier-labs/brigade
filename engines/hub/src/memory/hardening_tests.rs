//! Regression coverage for the four findings in #2379.
use super::retrieval_eval::FIXTURE_SCOPE;
use super::{hardening::*, proposal_tests::*, tests::*, *};
use rusqlite::Connection;

fn count(db: &Connection, table: &str) -> i64 {
    db.query_row(&format!("SELECT count(*) FROM {table}"), [], |r| r.get(0))
        .unwrap()
}
#[tokio::test]
async fn duplicate_pending_returns_original_without_any_storage_write() {
    let h = Hub::with(clean(), None);
    let a = h.put(ADMIN_TOKEN, FIXTURE_SCOPE, "a", None, OLD).await;
    let operation = op(FIXTURE_SCOPE, "a", rev(&a));
    let first = submit(&h, ADMIN_TOKEN, vec![operation.clone()]).await;
    assert_eq!(first.status, 201);
    let db = Connection::open(h.memory()).unwrap();
    let before = (
        h.events(),
        count(&db, "memory_idempotency"),
        count(&db, "memory_blobs"),
    );
    for _ in 0..3 {
        let again = submit(&h, ADMIN_TOKEN, vec![operation.clone()]).await;
        assert_eq!(code(&again), "memory_proposal_duplicate_pending");
        assert_eq!(again.body["proposal_id"], first.body["proposal_id"]);
    }
    assert_eq!(
        before,
        (
            h.events(),
            count(&db, "memory_idempotency"),
            count(&db, "memory_blobs")
        )
    );
    assert_eq!(count(&db, "memory_proposals"), 1);
}
#[tokio::test]
async fn quota_refusal_is_atomic_for_principal_scope_and_bytes() {
    for (key, expected) in [
        ("pending_per_principal", "memory_proposal_principal_quota"),
        ("pending_per_scope", "memory_proposal_scope_quota"),
        ("bytes_per_principal", "memory_proposal_principal_quota"),
        ("bytes_per_scope", "memory_proposal_scope_quota"),
    ] {
        let mut h = Hub::with(clean(), None);
        let a = h.put(ADMIN_TOKEN, FIXTURE_SCOPE, "a", None, OLD).await;
        let b = h.put(ADMIN_TOKEN, FIXTURE_SCOPE, "b", None, OLD).await;
        if key.starts_with("pending") {
            assert_eq!(
                submit(&h, ADMIN_TOKEN, vec![op(FIXTURE_SCOPE, "a", rev(&a))])
                    .await
                    .status,
                201
            );
        }
        let mut limits = json!({});
        limits[key] = json!(1);
        h.store
            .as_mut()
            .unwrap()
            .configure_retrieval(&json!({"proposal_limits":limits}))
            .unwrap();
        let db = Connection::open(h.memory()).unwrap();
        let before = (
            h.events(),
            count(&db, "memory_idempotency"),
            count(&db, "memory_proposals"),
            count(&db, "memory_blobs"),
        );
        let r = submit(&h, ADMIN_TOKEN, vec![op(FIXTURE_SCOPE, "b", rev(&b))]).await;
        assert_eq!((r.status, code(&r)), (429, expected));
        assert_eq!(
            before,
            (
                h.events(),
                count(&db, "memory_idempotency"),
                count(&db, "memory_proposals"),
                count(&db, "memory_blobs")
            )
        );
    }
}
#[tokio::test]
async fn retention_compacts_text_preserves_immutable_audit_and_event_log() {
    let h = Hub::with(clean(), None);
    let a = h.put(ADMIN_TOKEN, FIXTURE_SCOPE, "a", None, OLD).await;
    let opened = submit(&h, ADMIN_TOKEN, vec![op(FIXTURE_SCOPE, "a", rev(&a))]).await;
    let id = opened.body["proposal_id"].as_str().unwrap();
    let r=h.call(ADMIN_TOKEN,json!({"action":"decide","proposal_id":id,"decision":"reject","base_epoch":h.epoch().await,"client_event_id":h.id()})).await;
    assert_eq!(r.body["state"], "rejected");
    let db = Connection::open(h.memory()).unwrap();
    let before = h.events();
    assert_eq!(
        compact(&db, h.now() + 8 * 86_400_000, &Limits::default(), 256).unwrap(),
        1
    );
    let raw: String = db
        .query_row(
            "SELECT body FROM memory_proposals WHERE proposal_id=?1",
            [id],
            |r| r.get(0),
        )
        .unwrap();
    let audit: Json = serde_json::from_str(&raw).unwrap();
    for key in [
        "proposal_id",
        "proposer",
        "decided_by",
        "created_at",
        "terminal_ms",
        "decision",
        "body_digest",
        "proposal_digest",
        "policy_digest",
    ] {
        assert!(!audit[key].is_null(), "{key}");
    }
    for key in ["diff", "ops", "preflight"] {
        assert!(audit.get(key).is_none());
    }
    assert!(!raw.contains(OLD));
    assert!(audit["cards"][0].get("after_text").is_none());
    assert_eq!(h.events(), before);
    assert!(
        db.execute(
            "UPDATE memory_proposals SET body='{}' WHERE proposal_id=?1",
            [id]
        )
        .is_err()
    );
    assert!(db.execute("DELETE FROM memory_events", []).is_err());
    let expired = h
        .call(ADMIN_TOKEN, json!({"action":"preflight","proposal_id":id}))
        .await;
    assert_eq!(
        (expired.status, code(&expired)),
        (409, "memory_proposal_compacted")
    );
}
#[test]
fn diagnostic_row_budget_and_rate_limits_refuse() {
    let db = Connection::open_in_memory().unwrap();
    let budget = Budget {
        db: &db,
        remaining: std::cell::Cell::new(1),
        deadline: deadline(100),
    };
    assert!(crate::writer::ReadTx::query(&budget, "SELECT 1 UNION ALL SELECT 2", &[]).is_err());
    let mut rate = Rate::default();
    let limits = Limits::default();
    assert!(rate.admit("one", "a", 1000, &limits));
    assert!(!rate.admit("one", "b", 1001, &limits));
    assert!(!rate.admit("two", "a", 1001, &limits));
    assert!(rate.admit("one", "a", 61_000, &limits));
}
#[tokio::test]
async fn policy_off_never_scans_members_and_rate_refusal_writes_nothing() {
    let mut h = Hub::with(clean(), None);
    let a = h.put(ADMIN_TOKEN, FIXTURE_SCOPE, "a", None, OLD).await;
    let opened = submit(&h, ADMIN_TOKEN, vec![op(FIXTURE_SCOPE, "a", rev(&a))]).await;
    // Deliberately remove corpus tables after preparation. Cheap gates must not query them.
    let db = Connection::open(h.memory()).unwrap();
    db.execute_batch("ALTER TABLE memory_cards RENAME TO hidden_cards")
        .unwrap();
    let request = || json!({"action":"auto_apply","proposal_id":opened.body["proposal_id"],"base_epoch":opened.body["log_epoch"],"client_event_id":h.id()});
    let before = h.events();
    let r = h.call(ADMIN_TOKEN, request()).await;
    assert_eq!(r.status, 200);
    assert_eq!(
        r.body["preflight"]["checks"][0]["code"],
        "auto_apply_disabled"
    );
    let r = h.call(ADMIN_TOKEN, request()).await;
    assert_eq!(
        (r.status, code(&r)),
        (429, "memory_auto_apply_rate_limited")
    );
    assert_eq!(before, h.events());
    // Stop the publisher before the table fixture is removed.
    h.store.take();
}
#[tokio::test]
async fn read_without_review_gets_no_legacy_proposal_diagnostics_from_log() {
    let h = Hub::with(clean(), None);
    let a = h.put(ADMIN_TOKEN, FIXTURE_SCOPE, "a", None, OLD).await;
    let opened = submit(&h, ADMIN_TOKEN, vec![op(FIXTURE_SCOPE, "a", rev(&a))]).await;
    grant(&h, FIXTURE_SCOPE, &["read"]).await;
    let r = h.call(AGENT_TOKEN, json!({"action":"log"})).await;
    let event = r.body["events"]
        .as_array()
        .unwrap()
        .iter()
        .find(|e| e["kind"] == "proposal.open")
        .unwrap();
    assert_eq!(event["payload"]["proposal_id"], opened.body["proposal_id"]);
    let raw = event["payload"].to_string();
    for key in [
        "after_hash",
        "before_hash",
        "pins",
        "attesting_credentials",
        "retrieval_eval_record",
        "auto_apply_gates",
    ] {
        assert!(!raw.contains(key));
    }
    let admin = h.call(ADMIN_TOKEN, json!({"action":"log"})).await;
    assert!(admin.body.to_string().contains("after_hash"));

    let (automatic, id) = super::attestation_tests::ready().await;
    super::retrieval_eval_tests::record_current(&automatic).await;
    let applied = automatic
        .call(
            ADMIN_TOKEN,
            super::attestation_tests::auto_request(&automatic, &id).await,
        )
        .await;
    assert_eq!(applied.body["state"], "applied");
    grant(&automatic, FIXTURE_SCOPE, &["read"]).await;
    let admin = automatic.call(ADMIN_TOKEN, json!({"action":"log"})).await;
    let audit = admin.body["events"]
        .as_array()
        .unwrap()
        .iter()
        .find(|e| e["kind"] == "proposal.apply" && e["payload"]["decision_mode"] == "automatic")
        .unwrap();
    assert!(
        !audit["payload"]["gates"]["attesting_credentials"]
            .as_array()
            .unwrap()
            .is_empty()
    );
    assert!(audit["payload"]["gates"]["retrieval_eval_record"].is_object());
    let reader = automatic.call(AGENT_TOKEN, json!({"action":"log"})).await;
    let mut seen = std::collections::BTreeSet::new();
    for event in reader.body["events"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|e| proposals::string(e, "kind").starts_with("proposal."))
    {
        seen.insert(proposals::string(event, "kind"));
        let payload = event["payload"].to_string();
        for key in [
            "after_hash",
            "before_hash",
            "pins",
            "attesting_credentials",
            "retrieval_eval_record",
            "auto_apply_gates",
        ] {
            assert!(!payload.contains(key), "{key}");
        }
    }
    for kind in ["proposal.open", "proposal.apply", "proposal.decide"] {
        assert!(seen.contains(kind));
    }
}
#[tokio::test]
async fn version_eight_upgrade_preserves_proposals_and_epoch() {
    let h = Hub::with(clean(), None);
    let a = h.put(ADMIN_TOKEN, FIXTURE_SCOPE, "a", None, OLD).await;
    let opened = submit(&h, ADMIN_TOKEN, vec![op(FIXTURE_SCOPE, "a", rev(&a))]).await;
    let source = Connection::open(h.memory()).unwrap();
    let raw: String = source
        .query_row("SELECT body FROM memory_proposals", [], |r| r.get(0))
        .unwrap();
    let db = Connection::open_in_memory().unwrap();
    db.execute_batch(&schema::ddl().replace(DDL, "")).unwrap();
    db.execute_batch(&format!(
        "PRAGMA application_id={}; PRAGMA user_version=8",
        schema::APPLICATION_ID
    ))
    .unwrap();
    db.execute(
        "INSERT INTO memory_meta VALUES('log_epoch','fixture-epoch')",
        [],
    )
    .unwrap();
    db.execute(
        "INSERT INTO memory_proposals VALUES(?1,'pending',?2,'hub:admin','fixture-time',NULL)",
        rusqlite::params![opened.body["proposal_id"].as_str().unwrap(), raw],
    )
    .unwrap();
    schema::migrate(&db, h.now()).unwrap();
    assert_eq!(schema::log_epoch(&db).unwrap(), "fixture-epoch");
    assert_eq!(count(&db, "memory_proposal_scopes"), 1);
    let (stored_digest, bytes): (String, i64) = db
        .query_row(
            "SELECT digest,retained_bytes FROM memory_proposals",
            [],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .unwrap();
    assert_eq!(stored_digest.len(), 64);
    assert!(bytes > 0);
    assert_eq!(
        db.query_row("PRAGMA user_version", [], |r| r.get::<_, i64>(0))
            .unwrap(),
        schema::USER_VERSION
    );
}

#[tokio::test]
async fn automatic_diagnostic_budget_exhaustion_leaves_all_storage_unchanged() {
    use super::attestation_tests::{auto_request, ready};
    let (mut h, id) = ready().await;
    super::retrieval_eval_tests::record_current(&h).await;
    h.store
        .as_mut()
        .unwrap()
        .configure_retrieval(&json!({"proposal_limits":{"diagnostic_rows":1}}))
        .unwrap();
    let db = Connection::open(h.memory()).unwrap();
    let before = (
        h.events(),
        count(&db, "memory_idempotency"),
        count(&db, "memory_card_revisions"),
    );
    let r = h.call(ADMIN_TOKEN, auto_request(&h, &id).await).await;
    assert_eq!(
        (r.status, code(&r)),
        (503, "memory_auto_apply_budget_exceeded")
    );
    assert_eq!(
        before,
        (
            h.events(),
            count(&db, "memory_idempotency"),
            count(&db, "memory_card_revisions")
        )
    );
}

#[tokio::test]
async fn sqlite_writer_scan_budget_abort_returns_specific_code_and_rolls_back() {
    use super::attestation_tests::{auto_request, ready};
    let (mut h, id) = ready().await;
    super::retrieval_eval_tests::record_current(&h).await;
    h.store
        .as_mut()
        .unwrap()
        .configure_retrieval(&json!({"proposal_limits":{"lock_ms":1}}))
        .unwrap();
    let db = Connection::open(h.memory()).unwrap();
    db.execute_batch("CREATE TRIGGER expensive_apply BEFORE INSERT ON memory_events WHEN new.kind='proposal.apply' BEGIN SELECT (WITH RECURSIVE work(n) AS (VALUES(1) UNION ALL SELECT n+1 FROM work WHERE n<100000000) SELECT sum(n) FROM work); END;").unwrap();
    let before = (
        h.events(),
        count(&db, "memory_idempotency"),
        count(&db, "memory_card_revisions"),
    );
    let r = h.call(ADMIN_TOKEN, auto_request(&h, &id).await).await;
    assert_eq!(
        (r.status, code(&r)),
        (503, "memory_auto_apply_budget_exceeded")
    );
    assert_eq!(
        before,
        (
            h.events(),
            count(&db, "memory_idempotency"),
            count(&db, "memory_card_revisions")
        )
    );
}

#[tokio::test]
async fn reviewed_apply_retains_its_existing_budget_contract() {
    let mut h = Hub::with(clean(), None);
    let a = h.put(ADMIN_TOKEN, FIXTURE_SCOPE, "a", None, OLD).await;
    let opened = submit(&h, ADMIN_TOKEN, vec![op(FIXTURE_SCOPE, "a", rev(&a))]).await;
    h.store
        .as_mut()
        .unwrap()
        .configure_retrieval(&json!({"proposal_limits":{"lock_ms":1}}))
        .unwrap();
    let db = Connection::open(h.memory()).unwrap();
    // A reviewer is outside the reflect-controlled automatic work budget.
    db.execute_batch("CREATE TRIGGER reviewed_work BEFORE INSERT ON memory_events WHEN new.kind='proposal.apply' BEGIN SELECT (WITH RECURSIVE work(n) AS (VALUES(1) UNION ALL SELECT n+1 FROM work WHERE n<100000) SELECT sum(n) FROM work); END;").unwrap();
    let applied = decide(&h, ADMIN_TOKEN, &opened.body["proposal_id"]).await;
    assert_eq!(applied.status, 200);
    assert_eq!(applied.body["state"], "applied");
}

fn pending(db: &Connection) -> i64 {
    db.query_row(
        "SELECT count(*) FROM memory_proposals WHERE state IN ('pending','held')",
        [],
        |r| r.get(0),
    )
    .unwrap()
}
fn limits(h: &mut Hub, limits: Json) {
    h.store
        .as_mut()
        .unwrap()
        .configure_retrieval(&json!({ "proposal_limits": limits }))
        .unwrap();
}
async fn reject(h: &Hub, id: &Json) -> Reply {
    h.call(ADMIN_TOKEN, json!({"action":"decide","proposal_id":id,"decision":"reject","base_epoch":h.epoch().await,"client_event_id":h.id()})).await
}
/// Opens `n` pending proposals for one principal on distinct cards in `fleet`.
async fn backlog(h: &mut Hub, n: usize) -> Vec<Json> {
    limits(
        h,
        json!({"pending_per_principal": 1000, "pending_per_scope": 1000}),
    );
    let mut ids = Vec::new();
    for i in 0..n {
        let card = format!("c{i}");
        let put = h.put(ADMIN_TOKEN, FIXTURE_SCOPE, &card, None, OLD).await;
        let r = submit(h, ADMIN_TOKEN, vec![op(FIXTURE_SCOPE, &card, rev(&put))]).await;
        assert_eq!(r.status, 201, "{r:?}");
        ids.push(r.body["proposal_id"].clone());
    }
    ids
}
/// Decides proposals one at a time, alternating reject and accept, until the
/// pending count reaches `floor`. Every decision must succeed.
async fn drain(h: &Hub, db: &Connection, ids: &mut Vec<Json>, floor: i64) {
    let mut accept = false;
    while pending(db) > floor {
        let id = ids.pop().unwrap();
        let before = pending(db);
        let r = if accept {
            decide(h, ADMIN_TOKEN, &id).await
        } else {
            reject(h, &id).await
        };
        assert_eq!(r.status, 200, "{r:?}");
        let expected = if accept { "applied" } else { "rejected" };
        assert_eq!(r.body["state"], expected);
        assert_eq!(pending(db), before - 1);
        accept = !accept;
    }
}
#[tokio::test]
async fn upgraded_backlog_above_default_quota_drains_by_reject_and_accept() {
    let mut h = Hub::with(clean(), None);
    let mut ids = backlog(&mut h, 34).await;
    // An upgrade from an unbounded store returns to the default 32 pending.
    limits(&mut h, json!({}));
    let db = Connection::open(h.memory()).unwrap();
    assert_eq!(pending(&db), 34);
    let extra = h.put(ADMIN_TOKEN, FIXTURE_SCOPE, "extra", None, OLD).await;
    let extra_op = op(FIXTURE_SCOPE, "extra", rev(&extra));
    let refused = submit(&h, ADMIN_TOKEN, vec![extra_op.clone()]).await;
    assert_eq!(
        (refused.status, code(&refused)),
        (429, "memory_proposal_principal_quota")
    );
    drain(&h, &db, &mut ids, 32).await;
    // Still at the limit, so admission remains closed.
    let refused = submit(&h, ADMIN_TOKEN, vec![extra_op.clone()]).await;
    assert_eq!(code(&refused), "memory_proposal_principal_quota");
    drain(&h, &db, &mut ids, 31).await;
    let admitted = submit(&h, ADMIN_TOKEN, vec![extra_op]).await;
    assert_eq!(admitted.status, 201, "{admitted:?}");
    assert_eq!(pending(&db), 32);
}
#[tokio::test]
async fn lowered_pending_limits_below_backlog_drain_and_still_refuse_admission() {
    for (key, expected) in [
        ("pending_per_principal", "memory_proposal_principal_quota"),
        ("pending_per_scope", "memory_proposal_scope_quota"),
    ] {
        let mut h = Hub::with(clean(), None);
        let mut ids = backlog(&mut h, 5).await;
        let mut lowered = json!({"pending_per_principal": 1000, "pending_per_scope": 1000});
        lowered[key] = json!(2);
        limits(&mut h, lowered);
        let db = Connection::open(h.memory()).unwrap();
        let extra = h.put(ADMIN_TOKEN, FIXTURE_SCOPE, "extra", None, OLD).await;
        let extra_op = op(FIXTURE_SCOPE, "extra", rev(&extra));
        let refused = submit(&h, ADMIN_TOKEN, vec![extra_op.clone()]).await;
        assert_eq!((refused.status, code(&refused)), (429, expected), "{key}");
        drain(&h, &db, &mut ids, 2).await;
        let refused = submit(&h, ADMIN_TOKEN, vec![extra_op.clone()]).await;
        assert_eq!(code(&refused), expected, "{key}");
        drain(&h, &db, &mut ids, 1).await;
        let admitted = submit(&h, ADMIN_TOKEN, vec![extra_op]).await;
        assert_eq!(admitted.status, 201, "{key}: {admitted:?}");
    }
}
#[tokio::test]
async fn draining_pending_backlog_cannot_push_bytes_past_a_limit_they_were_within() {
    for (key, expected) in [
        ("bytes_per_principal", "memory_proposal_principal_quota"),
        ("bytes_per_scope", "memory_proposal_scope_quota"),
    ] {
        let mut h = Hub::with(clean(), None);
        let mut ids = backlog(&mut h, 3).await;
        let db = Connection::open(h.memory()).unwrap();
        let bytes: i64 = db
            .query_row(
                "SELECT SUM(retained_bytes) FROM memory_proposals",
                [],
                |r| r.get(0),
            )
            .unwrap();
        // Pending is over its limit while bytes sit exactly at theirs. A
        // decision records more audit bytes, so it would cross the byte limit.
        let mut lowered = json!({"pending_per_principal": 1, "pending_per_scope": 1});
        lowered[key] = json!(bytes);
        limits(&mut h, lowered);
        let id = ids.pop().unwrap();
        let before = (h.events(), count(&db, "memory_card_revisions"));
        for r in [reject(&h, &id).await, decide(&h, ADMIN_TOKEN, &id).await] {
            assert_eq!((r.status, code(&r)), (429, expected), "{key}: {r:?}");
        }
        assert_eq!(pending(&db), 3);
        assert_eq!(before, (h.events(), count(&db, "memory_card_revisions")));
        // With byte headroom, the pending overage alone does not block a decision.
        let mut relaxed = json!({"pending_per_principal": 1, "pending_per_scope": 1});
        relaxed[key] = json!(bytes.saturating_mul(4));
        limits(&mut h, relaxed);
        ids.push(id);
        drain(&h, &db, &mut ids, 1).await;
    }
}

/// A realistic scope: card count and size spread follow a measured workspace
/// (median about 1.4 KB, p90 about 3.8 KB, a thin tail past 10 KB), with
/// generated words, a few anchored blocks and some wiki links per card. No
/// text here comes from any real card.
mod scale {
    use super::super::{
        merge_drift_tests::{GONE, KEEP, merge_op},
        ranking_gate_tests::Real,
    };
    use super::*;
    use std::time::Instant;

    struct Lcg(u64);
    impl Lcg {
        fn next(&mut self) -> u64 {
            self.0 = self
                .0
                .wrapping_mul(6364136223846793005)
                .wrapping_add(1442695040888963407);
            self.0 >> 33
        }
        fn below(&mut self, n: u64) -> u64 {
            self.next() % n
        }
    }
    const SYLLABLES: [&str; 24] = [
        "ka", "lo", "mi", "ren", "tu", "vash", "ode", "pri", "sul", "ban", "cre", "dov", "elm",
        "fia", "gor", "hel", "ith", "jun", "kep", "lur", "mox", "nel", "ora", "pek",
    ];
    fn word(rng: &mut Lcg) -> String {
        (0..1 + rng.below(3))
            .map(|_| SYLLABLES[rng.below(24) as usize])
            .collect()
    }
    /// Target byte size drawn from the measured quantiles.
    fn target(rng: &mut Lcg) -> usize {
        match rng.below(100) {
            0..=9 => 400 + rng.below(260) as usize,
            10..=49 => 660 + rng.below(710) as usize,
            50..=89 => 1370 + rng.below(2450) as usize,
            90..=98 => 3820 + rng.below(6800) as usize,
            _ => 10_650 + rng.below(6500) as usize,
        }
    }
    fn card_text(i: usize, count: usize, rng: &mut Lcg) -> String {
        let title: Vec<_> = (0..4).map(|_| word(rng)).collect();
        let mut text = format!(
            "---\ntitle: {} #{i}\ntags: [{}, {}, {}]\ncreated: 2026-08-14\n---\n\n",
            title.join(" "),
            word(rng),
            word(rng),
            word(rng)
        );
        let goal = target(rng);
        while text.len() < goal {
            let sentence: Vec<_> = (0..8 + rng.below(14)).map(|_| word(rng)).collect();
            text.push_str(&sentence.join(" "));
            text.push('.');
            match rng.below(9) {
                0 => text.push_str(&format!(" ^c-{:06x}\n\n", rng.below(0xff_ffff))),
                1 => text.push_str(&format!(" [[card-{:04}]]\n\n", rng.below(count as u64))),
                2 => text.push_str("\n\n"),
                _ => text.push(' '),
            }
        }
        text.push('\n');
        text
    }
    /// A pair of exact duplicate claim bodies of ordinary card size.
    fn pair_text(anchor: &str) -> String {
        let mut rng = Lcg(7);
        let body: Vec<_> = (0..180).map(|_| word(&mut rng)).collect();
        format!("{}. ^c-{anchor}\n", body.join(" "))
    }
    /// `count` other cards beside one exact-duplicate pair, all agent-unreviewed
    /// and written by the admin, on the real scope `fleet`.
    pub(super) async fn scope(count: usize) -> Real {
        let mut rng = Lcg(42);
        let (keeper, retired) = (pair_text("aaaaaa"), pair_text("bbbbbb"));
        let texts: Vec<_> = (0..count).map(|i| card_text(i, count, &mut rng)).collect();
        let mut cards: Vec<(String, &str)> = vec![(KEEP.into(), &keeper), (GONE.into(), &retired)];
        cards.extend(
            texts
                .iter()
                .enumerate()
                .map(|(i, t)| (format!("card-{i:04}"), t.as_str())),
        );
        let refs: Vec<_> = cards.iter().map(|(id, t)| (id.as_str(), *t)).collect();
        Real::untrusted(&refs).await
    }
    pub(super) async fn open(r: &Real) -> Reply {
        let opened = submit(&r.h, ADMIN_TOKEN, vec![merge_op(r, KEEP, GONE).await]).await;
        assert_eq!(
            (opened.status, &opened.body["tier"]),
            (201, &json!(0)),
            "{opened:?}"
        );
        opened
    }
    /// `queries` judged use rows: each a recorded search and the card the agent used.
    pub(super) fn seed_history(r: &Real, queries: usize) {
        let epoch = super::super::ops::epoch(&crate::writer::worker::Tx(&r.db)).unwrap();
        let now = r.h.now();
        for i in 0..queries {
            let id = format!("card-{:04}", i * 7);
            let (hash, revision): (String, i64) = r.db
                .query_row("SELECT content_hash,revision FROM memory_cards WHERE scope='fleet' AND card_id=?1", [&id], |row| Ok((row.get(0)?, row.get(1)?)))
                .unwrap();
            let search = json!({"session_id":format!("s{i}"),"session_kind":"interactive","query":format!("{} {}", SYLLABLES[i % 24], SYLLABLES[(i * 5 + 3) % 24]),"results":[{"scope":"fleet","card_id":id,"content_hash":hash,"revision":revision}],"epoch":epoch});
            r.db.execute("INSERT INTO memory_retrieval_history(principal,credential,session,kind,created_ms,payload) VALUES('p','c',?1,'search',?2,?3)", rusqlite::params![format!("s{i}"), now - 5000, search.to_string()]).unwrap();
            let seq = r.db.last_insert_rowid();
            let used = json!({"search_seq":seq,"match":{"scope":"fleet","card_id":id,"content_hash":hash,"revision":revision}});
            r.db.execute("INSERT INTO memory_retrieval_history(principal,credential,session,kind,created_ms,payload) VALUES('p','c',?1,'use',?2,?3)", rusqlite::params![format!("s{i}"), now - 4000, used.to_string()]).unwrap();
        }
    }
    fn ms(since: Instant) -> f64 {
        since.elapsed().as_secs_f64() * 1000.0
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
    /// The automatic exact-duplicate merge on a real-size scope, under the limits
    /// an operator gets without configuring any. On a 1,500-card scope the
    /// earlier defaults (100 ms diagnostics, 50 ms writer) refused it with
    /// `memory_auto_apply_budget_exceeded`. The deadlines are wall clock, so a
    /// debug build gets three times the defaults: the old veto spent about
    /// 3.9 s per phase there and still fails, while this code spends about
    /// 0.3 s. A release build runs on the defaults themselves.
    #[tokio::test]
    async fn exact_merge_applies_on_a_realistic_scope_with_default_limits() {
        let defaults = super::super::hardening::Limits::default();
        assert_eq!((defaults.diagnostic_ms, defaults.lock_ms), (1_000, 1_000));
        let mut r = scope(1500).await;
        if cfg!(debug_assertions) {
            let (d, l) = (defaults.diagnostic_ms * 3, defaults.lock_ms * 3);
            r.h.store
                .as_mut()
                .unwrap()
                .configure_retrieval(&json!({"proposal_limits":{"diagnostic_ms":d,"lock_ms":l}}))
                .unwrap();
        }
        let opened = open(&r).await;
        let applied = super::super::merge_drift_tests::auto(&r, &opened).await;
        assert_eq!(applied.body["state"], "applied", "{applied:?}");
        assert_eq!(card(&r.h, "fleet", GONE).await["state"], "retired");
        assert_eq!(card(&r.h, "fleet", KEEP).await["state"], "live");
    }
    /// With no judged history the veto has nothing to replay and holds nothing.
    /// Once history exists it still replays, and the merge's corpus change
    /// moves a judged query's ranking, so the veto holds it.
    #[tokio::test]
    async fn history_veto_still_replays_when_history_exists() {
        let r = scope(300).await;
        let opened = open(&r).await;
        let body = r.body(&opened);
        assert_eq!(r.hold(&body, "admin", None), None);
        seed_history(&r, 20);
        assert_eq!(
            r.hold(&body, "admin", None),
            Some("retrieval_eval_query_veto")
        );
    }
    #[tokio::test]
    #[ignore = "host-dependent timing measurement"]
    async fn measure_exact_merge_at_scope() {
        let count: usize = std::env::var("SCOPE_CARDS")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(1500);
        let started = Instant::now();
        let mut r = scope(count).await;
        let history: usize = std::env::var("HISTORY")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(0)
            .min(count.div_ceil(7));
        seed_history(&r, history);
        eprintln!("seeded {} cards in {:.0} ms", count + 2, ms(started));
        r.h.store
            .as_mut()
            .unwrap()
            .configure_retrieval(
                &json!({"proposal_limits":{"diagnostic_ms":60_000,"lock_ms":60_000}}),
            )
            .unwrap();
        let start = Instant::now();
        let opened = open(&r).await;
        eprintln!("propose: {:.1} ms", ms(start));
        let body = r.body(&opened);
        eprintln!(
            "diagnostics core (preflight::evaluate, no budget): {:.1} ms",
            best(|| {
                let report = r.evaluate_as(&body, "admin", ADMIN);
                assert_eq!(report["structural_merge_proof"], true);
            })
        );
        eprintln!(
            "  retrieval_eval::hold: {:.1} ms",
            best(|| {
                let held = r.hold(&body, "admin", None);
                if history == 0 {
                    assert_eq!(held, None);
                }
            })
        );
        if history > 0 {
            return;
        }
        r.h.set_clock(r.h.now() + 1);
        let start = Instant::now();
        let applied = super::super::merge_drift_tests::auto(&r, &opened).await;
        eprintln!(
            "auto-apply request total: {:.1} ms state={}",
            ms(start),
            applied.body["state"]
        );
    }
}
