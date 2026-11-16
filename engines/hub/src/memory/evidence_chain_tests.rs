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
    for field in [
        "last_reset_seq",
        "last_reset_digest",
        "last_reset_created_at",
    ] {
        assert_eq!(empty.body[field], Json::Null);
    }
    let r = h
        .call(NODE_TOKEN, append(&h, 1, None, FIRST, "append"))
        .await;
    assert_eq!(r.status, 201, "{r:?}");
    assert_eq!(r.body["head"]["seq"], 1);
    assert_eq!(r.body["head"]["prev_digest"], Json::Null);
    for field in [
        "last_reset_seq",
        "last_reset_digest",
        "last_reset_created_at",
    ] {
        assert_eq!(r.body["head"][field], Json::Null);
        assert_eq!(r.body[field], Json::Null);
    }
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
        h.set_clock(h.now() + 1000);
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
    h.set_clock(h.now() + 1000);
    let reset = h
        .call(NODE_TOKEN, append(&h, 2, Some(FIRST), SECOND, "reset"))
        .await;
    assert_eq!(reset.status, 201, "{reset:?}");
    assert_eq!(reset.body["head"]["kind"], "reset");
    assert_eq!(reset.body["head"]["prev_digest"], FIRST);
    h.store.take();
    h.open(Arc::new(scan::Unavailable), None);
    assert_eq!(h.call(NODE_TOKEN, latest()).await.body, reset.body);
    h.set_clock(h.now() + 1000);
    assert_eq!(
        h.call(NODE_TOKEN, append(&h, 3, Some(SECOND), FIRST, "append"))
            .await
            .status,
        201
    );
    let head = h.call(NODE_TOKEN, latest()).await.body["head"].clone();
    assert_eq!(head["seq"], 3);
    assert_eq!(head["last_reset_seq"], 2);
    assert_eq!(head["last_reset_digest"], SECOND);
    assert_eq!(
        head["last_reset_created_at"],
        reset.body["head"]["created_at"]
    );
    let latest = h.call(NODE_TOKEN, latest()).await.body;
    for field in [
        "last_reset_seq",
        "last_reset_digest",
        "last_reset_created_at",
    ] {
        assert_eq!(latest[field], head[field]);
        assert_eq!(reset.body[field], reset.body["head"][field]);
    }
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
fn migration_from_schema_15_preserves_rows_and_is_repeatable() {
    // After rebasing #2606 and #2605 this retains their real schema-15 layout.
    let db = Connection::open_in_memory().unwrap();
    schema::migrate(&db, 1_800_000_000_000).unwrap();
    db.execute_batch(
        "DROP TABLE IF EXISTS memory_evidence_chain; PRAGMA user_version=15;
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

#[test]
fn schema_16_step_requires_schema_15() {
    for version in 0..=15 {
        let db = Connection::open_in_memory().unwrap();
        db.pragma_update(None, "user_version", version).unwrap();
        let result = schema::evidence_chain_step(&db);
        if version < 15 {
            let error = result.unwrap_err().to_string();
            assert!(error.contains("schema 16 requires schema 15"), "{error}");
            assert_eq!(
                db.pragma_query_value(None, "user_version", |r| r.get::<_, i64>(0))
                    .unwrap(),
                version
            );
            assert_eq!(
                db.query_row(
                    "SELECT COUNT(*) FROM sqlite_schema WHERE name='memory_evidence_chain'",
                    [],
                    |r| r.get::<_, i64>(0)
                )
                .unwrap(),
                0
            );
        } else {
            result.unwrap();
            assert_eq!(
                db.pragma_query_value(None, "user_version", |r| r.get::<_, i64>(0))
                    .unwrap(),
                16
            );
            db.execute("SELECT * FROM memory_evidence_chain", [])
                .unwrap();
        }
    }
}

#[tokio::test]
async fn quota_refusals_preserve_head_and_cover_all_principal_streams() {
    for principal_quota in [false, true] {
        let mut h = granted().await;
        let config = if principal_quota {
            json!({"proposal_limits":{"evidence_rows_per_principal":1}})
        } else {
            json!({"proposal_limits":{"evidence_rows_per_stream":1}})
        };
        h.store
            .as_mut()
            .unwrap()
            .configure_retrieval(&config)
            .unwrap();
        assert_eq!(
            h.call(NODE_TOKEN, append(&h, 1, None, FIRST, "append"))
                .await
                .status,
            201
        );
        let before = h.call(NODE_TOKEN, latest()).await.body;
        h.set_clock(h.now() + 1000);
        let mut request = append(&h, 2, Some(FIRST), SECOND, "append");
        if principal_quota {
            request["workspace_id"] = json!(SECOND);
            request["stream"] = json!("lesson_observations");
            request["seq"] = json!(1);
            request["prev_digest"] = Json::Null;
        }
        let reply = h.call(NODE_TOKEN, request).await;
        let expected = if principal_quota {
            "memory_evidence_chain_principal_quota"
        } else {
            "memory_evidence_chain_stream_quota"
        };
        assert_eq!((reply.status, code(&reply)), (429, expected));
        assert_eq!(h.call(NODE_TOKEN, latest()).await.body, before);
        let db = Connection::open(h.memory()).unwrap();
        assert_eq!(
            db.query_row("SELECT COUNT(*) FROM memory_evidence_chain", [], |r| r
                .get::<_, i64>(0))
                .unwrap(),
            1
        );
    }
}

#[tokio::test]
async fn rate_refusals_span_workspaces_and_survive_reopen_without_moving_head() {
    let mut h = granted().await;
    assert_eq!(
        h.call(NODE_TOKEN, append(&h, 1, None, FIRST, "append"))
            .await
            .status,
        201
    );
    let before = h.call(NODE_TOKEN, latest()).await.body;
    h.store.take();
    h.open(Arc::new(scan::Unavailable), None);
    h.set_clock(h.now() + 999);
    for other in [false, true] {
        let mut request = append(&h, 2, Some(FIRST), SECOND, "reset");
        if other {
            request["workspace_id"] = json!(SECOND);
            request["stream"] = json!("lesson_observations");
            request["seq"] = json!(1);
            request["prev_digest"] = Json::Null;
        }
        let reply = h.call(NODE_TOKEN, request).await;
        assert_eq!(
            (reply.status, code(&reply)),
            (429, "memory_evidence_chain_rate_limited")
        );
        assert_eq!(h.call(NODE_TOKEN, latest()).await.body, before);
    }
    h.set_clock(h.now() + 1);
    assert_eq!(
        h.call(NODE_TOKEN, append(&h, 2, Some(FIRST), SECOND, "reset"))
            .await
            .status,
        201
    );
}

#[tokio::test]
async fn workspace_quota_counts_distinct_ids_across_streams_and_preserves_head() {
    let mut h = granted().await;
    h.store
        .as_mut()
        .unwrap()
        .configure_retrieval(&json!({"proposal_limits":{"evidence_workspaces_per_principal":1}}))
        .unwrap();
    assert_eq!(
        h.call(NODE_TOKEN, append(&h, 1, None, FIRST, "append"))
            .await
            .status,
        201
    );
    let before = h.call(NODE_TOKEN, latest()).await.body;
    h.set_clock(h.now() + 1000);
    let mut other = append(&h, 1, None, SECOND, "append");
    other["workspace_id"] = json!(SECOND);
    let reply = h.call(NODE_TOKEN, other).await;
    assert_eq!(
        (reply.status, code(&reply)),
        (429, "memory_evidence_chain_workspace_quota")
    );
    assert_eq!(h.call(NODE_TOKEN, latest()).await.body, before);
    let mut same = append(&h, 1, None, SECOND, "append");
    same["stream"] = json!("lesson_observations");
    assert_eq!(h.call(NODE_TOKEN, same).await.status, 201);
    h.set_clock(h.now() + 1000);
    assert_eq!(
        h.call(NODE_TOKEN, append(&h, 2, Some(FIRST), SECOND, "append"))
            .await
            .status,
        201
    );
    let db = Connection::open(h.memory()).unwrap();
    assert_eq!(
        db.query_row(
            "SELECT COUNT(DISTINCT workspace_id) FROM memory_evidence_chain",
            [],
            |r| r.get::<_, i64>(0)
        )
        .unwrap(),
        1
    );
}

#[tokio::test]
async fn admin_requires_own_exact_grant_and_cannot_act_as_another_owner() {
    let h = granted().await;
    let mut own = latest();
    own["scope"] = json!("principal:hub:admin");
    let mut own_append = append(&h, 1, None, FIRST, "append");
    own_append["scope"] = own["scope"].clone();
    for request in [
        own.clone(),
        own_append.clone(),
        latest(),
        append(&h, 1, None, FIRST, "append"),
    ] {
        let reply = h.call(ADMIN_TOKEN, request).await;
        assert_eq!((reply.status, code(&reply)), (403, "memory_grant_missing"));
    }
    // The grant API provisions only nodes and agents. Seed an exact admin
    // grant to exercise owner authorization independently of that API.
    Connection::open(h.memory()).unwrap().execute(
        "INSERT INTO memory_grants(principal,scope,rights,updated_at,updated_by) VALUES(?1,'principal:hub:admin','evidence_chain',?2,?1)",
        [ADMIN, &ids::isoformat(h.now())],
    ).unwrap();
    assert_eq!(h.call(ADMIN_TOKEN, own_append).await.status, 201);
    assert_eq!(h.call(ADMIN_TOKEN, own).await.body["head"]["seq"], 1);
    for request in [latest(), append(&h, 1, None, FIRST, "append")] {
        let reply = h.call(ADMIN_TOKEN, request).await;
        assert_eq!((reply.status, code(&reply)), (403, "memory_grant_missing"));
    }
}

#[tokio::test]
async fn reset_recovers_full_generation_but_not_principal_storage() {
    let mut h = granted().await;
    h.store
        .as_mut()
        .unwrap()
        .configure_retrieval(&json!({"proposal_limits": {
            "evidence_rows_per_stream": 2, "evidence_rows_per_principal": 5
        }}))
        .unwrap();
    for (seq, prev, digest, kind, status, expected) in [
        (1, None, FIRST, "append", 201, ""),
        (2, Some(FIRST), SECOND, "append", 201, ""),
        (
            3,
            Some(SECOND),
            FIRST,
            "append",
            429,
            "memory_evidence_chain_stream_quota",
        ),
        (3, Some(SECOND), FIRST, "reset", 201, ""),
        (4, Some(FIRST), SECOND, "append", 201, ""),
        (
            5,
            Some(SECOND),
            FIRST,
            "append",
            429,
            "memory_evidence_chain_stream_quota",
        ),
        (5, Some(SECOND), FIRST, "reset", 201, ""),
        (
            6,
            Some(FIRST),
            SECOND,
            "append",
            429,
            "memory_evidence_chain_principal_quota",
        ),
        (
            6,
            Some(FIRST),
            SECOND,
            "reset",
            429,
            "memory_evidence_chain_principal_quota",
        ),
    ] {
        h.set_clock(h.now() + 1000);
        let before = h.call(NODE_TOKEN, latest()).await.body;
        let reply = h
            .call(NODE_TOKEN, append(&h, seq, prev, digest, kind))
            .await;
        assert_eq!(reply.status, status, "{reply:?}");
        if status == 429 {
            assert_eq!(code(&reply), expected);
            assert_eq!(h.call(NODE_TOKEN, latest()).await.body, before);
        }
    }
    let db = Connection::open(h.memory()).unwrap();
    assert_eq!(
        db.query_row("SELECT COUNT(*) FROM memory_evidence_chain", [], |r| r
            .get::<_, i64>(0))
            .unwrap(),
        5
    );
}
