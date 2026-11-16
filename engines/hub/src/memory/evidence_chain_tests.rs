//! Hub-owned evidence heads stay ordered across workspace rollback and reset.
use super::{tests::*, *};
use rusqlite::Connection;
use serde_json::json;

const SCOPE: &str = "principal:node-1";
const WORKSPACE: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const FIRST: &str = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
const SECOND: &str = "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc";

async fn grant(h: &Hub, principal: &str, scope: &str) {
    let r = h
        .call(
            ADMIN_TOKEN,
            json!({"action":"grant", "principal":principal,
        "scope":scope,"rights":["evidence_chain"],"client_event_id":h.id()}),
        )
        .await;
    assert_eq!(r.status, 200, "{r:?}");
}
fn latest() -> Json {
    json!({"action":"evidence_chain","operation":"latest","scope":SCOPE,
        "workspace_id":WORKSPACE,"stream":"card_reads"})
}
fn append(h: &Hub, seq: i64, prev: Option<&str>, digest: &str, kind: &str) -> Json {
    let mut r = latest();
    r["operation"] = json!("append");
    r["seq"] = json!(seq);
    r["prev_digest"] = json!(prev);
    r["digest"] = json!(digest);
    r["kind"] = json!(kind);
    r["client_event_id"] = json!(h.id());
    r
}
async fn granted() -> Hub {
    let h = Hub::new();
    grant(&h, "node-1", SCOPE).await;
    h
}

#[tokio::test]
async fn append_in_order_and_latest_use_server_time() {
    let h = granted().await;
    let empty = h.call(NODE_TOKEN, latest()).await;
    assert_eq!(empty.status, 200, "{empty:?}");
    assert_eq!(empty.body["head"], Json::Null);
    let r = h
        .call(NODE_TOKEN, append(&h, 1, None, FIRST, "append"))
        .await;
    assert_eq!(r.status, 201, "{r:?}");
    assert_eq!(r.body["head"]["seq"], 1);
    assert_eq!(r.body["head"]["prev_digest"], Json::Null);
    assert_eq!(r.body["head"]["created_at"], ids::isoformat(h.now()));
    h.set_clock(h.now() + 1000);
    let r = h
        .call(NODE_TOKEN, append(&h, 2, Some(FIRST), SECOND, "append"))
        .await;
    assert_eq!(r.status, 201, "{r:?}");
    let head = h.call(NODE_TOKEN, latest()).await;
    assert_eq!(head.body, r.body);
    assert_eq!(head.body["head"]["digest"], SECOND);
    assert_eq!(head.body["head"]["created_at"], ids::isoformat(h.now()));
}

#[tokio::test]
async fn gaps_wrong_previous_digest_and_replays_refuse_without_moving_head() {
    let h = granted().await;
    let first = append(&h, 1, None, FIRST, "append");
    assert_eq!(h.call(NODE_TOKEN, first.clone()).await.status, 201);
    for request in [
        append(&h, 3, Some(FIRST), SECOND, "append"),
        append(&h, 2, Some(SECOND), SECOND, "append"),
        append(&h, 2, None, SECOND, "append"),
        first,
    ] {
        let r = h.call(NODE_TOKEN, request).await;
        assert_eq!(
            (r.status, code(&r)),
            (409, "memory_evidence_chain_conflict")
        );
        assert_eq!(h.call(NODE_TOKEN, latest()).await.body["head"]["seq"], 1);
    }
}

#[tokio::test]
async fn only_the_owner_with_an_exact_grant_can_read_or_append() {
    let h = Hub::new();
    for request in [latest(), append(&h, 1, None, FIRST, "append")] {
        let r = h.call(NODE_TOKEN, request).await;
        assert_eq!((r.status, code(&r)), (403, "memory_grant_missing"));
    }
    grant(&h, "node-1", "*").await;
    assert_eq!(h.call(NODE_TOKEN, latest()).await.status, 403);
    grant(&h, "node-1", SCOPE).await;
    assert_eq!(
        h.call(NODE_TOKEN, append(&h, 1, None, FIRST, "append"))
            .await
            .status,
        201
    );
    grant(&h, "agent:dot", "principal:agent:dot").await;
    for token in [AGENT_TOKEN, ADMIN_TOKEN] {
        for request in [latest(), append(&h, 2, Some(FIRST), SECOND, "reset")] {
            let r = h.call(token, request).await;
            assert_eq!((r.status, code(&r)), (403, "memory_grant_missing"));
        }
    }
    // The same opaque workspace id under another principal is a separate chain.
    let mut own = latest();
    own["scope"] = json!("principal:agent:dot");
    let r = h.call(AGENT_TOKEN, own).await;
    assert_eq!(r.status, 200, "{r:?}");
    assert_eq!(r.body["head"], Json::Null);
}

#[tokio::test]
async fn invalid_fields_are_refused_and_streams_and_workspaces_are_independent() {
    let h = granted().await;
    for (key, value) in [
        ("stream", json!("unknown")),
        ("workspace_id", json!("/tmp/cards")),
        ("workspace_id", json!("a".repeat(65))),
        ("workspace_id", json!("A".repeat(64))),
        ("seq", json!(0)),
        ("seq", json!(1.5)),
        ("seq", json!(true)),
        ("digest", json!("bad")),
        ("prev_digest", json!("bad")),
        ("kind", json!("delete")),
        ("created_at", json!("forged")),
        ("principal", json!("node-1")),
    ] {
        let mut request = append(&h, 1, None, FIRST, "append");
        request[key] = value;
        let r = h.call(NODE_TOKEN, request).await;
        assert_eq!(
            (r.status, code(&r)),
            (400, "memory_request_invalid"),
            "{r:?}"
        );
    }
    let mut unknown = latest();
    unknown["stream"] = json!("unknown");
    assert_eq!(h.call(NODE_TOKEN, unknown).await.status, 400);
    for (workspace, stream) in [
        (WORKSPACE, "card_reads"),
        (WORKSPACE, "lesson_observations"),
        (SECOND, "card_reads"),
    ] {
        let mut request = append(&h, 1, None, FIRST, "append");
        request["workspace_id"] = json!(workspace);
        request["stream"] = json!(stream);
        assert_eq!(h.call(NODE_TOKEN, request).await.status, 201);
    }
}

#[tokio::test]
async fn reset_is_an_ordered_record_and_survives_reopen() {
    let mut h = granted().await;
    assert_eq!(
        h.call(NODE_TOKEN, append(&h, 1, None, FIRST, "append"))
            .await
            .status,
        201
    );
    assert_eq!(
        h.call(NODE_TOKEN, append(&h, 3, Some(FIRST), SECOND, "reset"))
            .await
            .status,
        409
    );
    let reset = h
        .call(NODE_TOKEN, append(&h, 2, Some(FIRST), SECOND, "reset"))
        .await;
    assert_eq!(reset.status, 201, "{reset:?}");
    assert_eq!(reset.body["head"]["kind"], "reset");
    assert_eq!(reset.body["head"]["prev_digest"], FIRST);
    h.store.take();
    h.open(Arc::new(scan::Unavailable), None);
    assert_eq!(h.call(NODE_TOKEN, latest()).await.body, reset.body);
    assert_eq!(
        h.call(NODE_TOKEN, append(&h, 3, Some(SECOND), FIRST, "append"))
            .await
            .status,
        201
    );
    let db = Connection::open(h.memory()).unwrap();
    let count: i64 = db
        .query_row("SELECT count(*) FROM memory_evidence_chain", [], |r| {
            r.get(0)
        })
        .unwrap();
    assert_eq!(count, 3);
    assert!(db.execute("DELETE FROM memory_evidence_chain", []).is_err());
    assert!(
        db.execute("UPDATE memory_evidence_chain SET kind='reset'", [])
            .is_err()
    );
}

#[tokio::test]
async fn concurrent_appends_have_one_winner() {
    let h = granted().await;
    let (a, b) = tokio::join!(
        h.call(NODE_TOKEN, append(&h, 1, None, FIRST, "append")),
        h.call(NODE_TOKEN, append(&h, 1, None, SECOND, "append"))
    );
    let mut statuses = [a.status, b.status];
    statuses.sort();
    assert_eq!(statuses, [201, 409]);
}

#[test]
fn migration_from_current_schema_preserves_rows_and_is_repeatable() {
    // Restore the version 13 layout by removing the new table, as in other migration tests.
    let db = Connection::open_in_memory().unwrap();
    schema::migrate(&db, 1_800_000_000_000).unwrap();
    db.execute_batch(
        "DROP TABLE IF EXISTS memory_evidence_chain; PRAGMA user_version=13;
        UPDATE memory_meta SET value='old-epoch' WHERE key='log_epoch';",
    )
    .unwrap();
    schema::migrate(&db, 1_800_000_000_000).unwrap();
    schema::migrate(&db, 1_800_000_000_000).unwrap();
    let version: i64 = db
        .pragma_query_value(None, "user_version", |r| r.get(0))
        .unwrap();
    assert_eq!(version, 16);
    let epoch = schema::log_epoch(&db).unwrap();
    assert_eq!(epoch, "old-epoch");
    let count: i64 = db
        .query_row("SELECT count(*) FROM memory_evidence_chain", [], |r| {
            r.get(0)
        })
        .unwrap();
    assert_eq!(count, 0);
}
