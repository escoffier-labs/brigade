//! Regression coverage for the four findings in #2379.
use super::{hardening::*, proposal_tests::*, tests::*, *};
use rusqlite::Connection;

fn count(db: &Connection, table: &str) -> i64 {
    db.query_row(&format!("SELECT count(*) FROM {table}"), [], |r| r.get(0))
        .unwrap()
}
#[tokio::test]
async fn duplicate_pending_returns_original_without_any_storage_write() {
    let h = Hub::with(clean(), None);
    let a = h.put(ADMIN_TOKEN, "fleet", "a", None, OLD).await;
    let operation = op("fleet", "a", rev(&a));
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
        let a = h.put(ADMIN_TOKEN, "fleet", "a", None, OLD).await;
        let b = h.put(ADMIN_TOKEN, "fleet", "b", None, OLD).await;
        if key.starts_with("pending") {
            assert_eq!(
                submit(&h, ADMIN_TOKEN, vec![op("fleet", "a", rev(&a))])
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
        let r = submit(&h, ADMIN_TOKEN, vec![op("fleet", "b", rev(&b))]).await;
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
    let a = h.put(ADMIN_TOKEN, "fleet", "a", None, OLD).await;
    let opened = submit(&h, ADMIN_TOKEN, vec![op("fleet", "a", rev(&a))]).await;
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
    let a = h.put(ADMIN_TOKEN, "fleet", "a", None, OLD).await;
    let opened = submit(&h, ADMIN_TOKEN, vec![op("fleet", "a", rev(&a))]).await;
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
    let a = h.put(ADMIN_TOKEN, "fleet", "a", None, OLD).await;
    let opened = submit(&h, ADMIN_TOKEN, vec![op("fleet", "a", rev(&a))]).await;
    grant(&h, "fleet", &["read"]).await;
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
}
#[tokio::test]
async fn version_eight_upgrade_preserves_proposals_and_epoch() {
    let h = Hub::with(clean(), None);
    let a = h.put(ADMIN_TOKEN, "fleet", "a", None, OLD).await;
    let opened = submit(&h, ADMIN_TOKEN, vec![op("fleet", "a", rev(&a))]).await;
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
        9
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
