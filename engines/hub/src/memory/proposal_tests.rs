//! S19a wire acceptance. The caller never supplies card bytes for a rollback.
use super::{tests::*, *};
use rusqlite::Connection;
use serde_json::{Value as Json, json};

pub(super) const OLD: &str = "Use the focused gate. ^c-abcdef";
const NEW: &str = "Use the scoped gate.";
pub(super) fn clean() -> Arc<dyn Scanner> {
    Arc::new(Fixed(Scan::Clean {
        ruleset: "test-v7".into(),
    }))
}
pub(super) async fn grant(h: &Hub, scope: &str, rights: &[&str]) {
    let r = h.call(ADMIN_TOKEN, json!({"action":"grant", "principal":"agent:dot", "scope":scope,"rights":rights,"client_event_id":h.id()})).await;
    assert_eq!(r.status, 200, "{r:?}");
}
pub(super) fn op(scope: &str, card: &str, revision: i64) -> Json {
    json!({"type":"replace_block", "scope":scope, "card_id":card,
           "base_revision":revision,"block_id":"c-abcdef", "expected_block_hash":ids::sha256_hex(OLD.as_bytes()), "text":NEW})
}
pub(super) async fn submit(h: &Hub, token: &str, ops: Vec<Json>) -> Reply {
    h.call(
        token,
        json!({"action":"propose","base_epoch":h.epoch().await,"ops":ops,"client_event_id":h.id()}),
    )
    .await
}
pub(super) async fn decide(h: &Hub, token: &str, id: &Json) -> Reply {
    h.call(token, json!({"action":"decide","proposal_id":id,"decision":"accept","base_epoch":h.epoch().await,"client_event_id":h.id()})).await
}
pub(super) async fn card(h: &Hub, scope: &str, id: &str) -> Json {
    h.call(
        ADMIN_TOKEN,
        json!({"action":"card","scope":scope,"card_id":id}),
    )
    .await
    .body["card"]
        .clone()
}
#[tokio::test]
async fn proposal_requires_decision_then_replays_and_rollback_requires_another_decision() {
    let h = Hub::with(clean(), None);
    let a = h.put(ADMIN_TOKEN, "fleet", "a", None, OLD).await;
    let before = card(&h, "fleet", "a").await;
    let opened = submit(&h, ADMIN_TOKEN, vec![op("fleet", "a", rev(&a))]).await;
    assert_eq!(opened.status, 201, "{opened:?}");
    assert_eq!(opened.body["state"], "pending");
    assert!(opened.body["diff"].as_str().unwrap().contains(NEW));
    assert_eq!(card(&h, "fleet", "a").await, before);
    let request = json!({"action":"decide","proposal_id":opened.body["proposal_id"],"decision":"accept","base_epoch":h.epoch().await,"client_event_id":h.id()});
    let applied = h.call(ADMIN_TOKEN, request.clone()).await;
    assert_eq!(applied.body["state"], "applied", "{applied:?}");
    let after = card(&h, "fleet", "a").await;
    assert_eq!(after["trust"], "reviewed");
    assert!(after["text"].as_str().unwrap().contains(NEW));
    assert_eq!(h.call(ADMIN_TOKEN, request).await, applied);
    assert_eq!(
        decide(&h, ADMIN_TOKEN, &opened.body["proposal_id"])
            .await
            .status,
        409
    );
    let rollback = h.call(ADMIN_TOKEN,json!({"action":"rollback","proposal_id":opened.body["proposal_id"],"base_epoch":h.epoch().await,"client_event_id":h.id()})).await;
    assert_eq!(rollback.status, 201, "{rollback:?}");
    assert_eq!(rollback.body["state"], "pending");
    assert_eq!(card(&h, "fleet", "a").await, after);
    let restored = decide(&h, ADMIN_TOKEN, &rollback.body["proposal_id"]).await;
    assert_eq!(restored.body["state"], "applied", "{restored:?}");
    let current = card(&h, "fleet", "a").await;
    assert_eq!(current["text"], before["text"]);
    assert_eq!(current["trust"], before["trust"]);
    assert_ne!(current["revision"], before["revision"]);
}
#[tokio::test]
async fn invalid_op_refuses_whole_submission_with_indexed_reasons_and_no_storage() {
    let h = Hub::with(clean(), None);
    let a = h.put(ADMIN_TOKEN, "fleet", "a", None, OLD).await;
    let before = h.events();
    let mut invalid = op("fleet", "a", rev(&a));
    invalid["block_id"] = json!("c-000000");
    let r = submit(
        &h,
        ADMIN_TOKEN,
        vec![
            op("fleet", "a", rev(&a)),
            invalid,
            json!({"type":"edit_skill","path":"AGENTS.md"}),
        ],
    )
    .await;
    assert_eq!(
        (r.status, code(&r)),
        (422, "memory_proposal_invalid"),
        "{r:?}"
    );
    assert_eq!(r.body["ops"].as_array().unwrap().len(), 3);
    assert!(r.body["ops"][1]["reason"].is_string());
    assert!(r.body["ops"][2]["reason"].is_string());
    assert_eq!(h.events(), before);
    let count: i64 = Connection::open(h.memory())
        .unwrap()
        .query_row("SELECT count(*) FROM memory_proposals", [], |r| r.get(0))
        .unwrap();
    assert_eq!(count, 0);
}
#[tokio::test]
async fn one_stale_card_or_changed_pin_changes_no_card() {
    for corrupt_pin in [false, true] {
        let h = Hub::with(clean(), None);
        let a = h.put(ADMIN_TOKEN, "fleet", "a", None, OLD).await;
        let b = h.put(ADMIN_TOKEN, "fleet", "b", None, OLD).await;
        let opened = submit(
            &h,
            ADMIN_TOKEN,
            vec![op("fleet", "a", rev(&a)), op("fleet", "b", rev(&b))],
        )
        .await;
        assert_eq!(opened.status, 201, "{opened:?}");
        if corrupt_pin {
            // Simulate inconsistent restored bytes at an unchanged revision.
            Connection::open(h.memory())
                .unwrap()
                .execute(
                    "UPDATE memory_blobs SET bytes=?1 WHERE content_hash=?2",
                    rusqlite::params![
                        "Changed. ^c-abcdef\n".as_bytes(),
                        b.body["event"]["content_hash"].as_str().unwrap()
                    ],
                )
                .unwrap();
        } else {
            h.put(ADMIN_TOKEN, "fleet", "b", Some(rev(&b)), "Later. ^c-abcdef")
                .await;
        }
        let before_a = card(&h, "fleet", "a").await;
        let before_b = card(&h, "fleet", "b").await;
        let r = decide(&h, ADMIN_TOKEN, &opened.body["proposal_id"]).await;
        assert_eq!(r.body["state"], "stale", "{r:?}");
        assert_eq!(card(&h, "fleet", "a").await, before_a);
        assert_eq!(card(&h, "fleet", "b").await, before_b);
    }
}
#[tokio::test]
async fn rollback_after_edit_is_stale_at_request_or_at_decision() {
    for edit_before_request in [false, true] {
        let h = Hub::with(clean(), None);
        let a = h.put(ADMIN_TOKEN, "fleet", "a", None, OLD).await;
        let opened = submit(&h, ADMIN_TOKEN, vec![op("fleet", "a", rev(&a))]).await;
        decide(&h, ADMIN_TOKEN, &opened.body["proposal_id"]).await;
        let applied = card(&h, "fleet", "a").await;
        let request = json!({"action":"rollback","proposal_id":opened.body["proposal_id"],"base_epoch":h.epoch().await,"client_event_id":h.id()});
        if edit_before_request {
            h.put(
                ADMIN_TOKEN,
                "fleet",
                "a",
                applied["revision"].as_i64(),
                "Later. ^c-abcdef",
            )
            .await;
        }
        let rb = h.call(ADMIN_TOKEN, request).await;
        if edit_before_request {
            assert_eq!(rb.body["state"], "stale", "{rb:?}");
        } else {
            h.put(
                ADMIN_TOKEN,
                "fleet",
                "a",
                applied["revision"].as_i64(),
                "Later. ^c-abcdef",
            )
            .await;
            assert_eq!(
                decide(&h, ADMIN_TOKEN, &rb.body["proposal_id"]).await.body["state"],
                "stale"
            );
        }
        assert_eq!(card(&h, "fleet", "a").await["text"], "Later. ^c-abcdef\n");
    }
}
#[tokio::test]
async fn target_scope_grants_guard_propose_decide_apply_and_rollback_including_collections() {
    for scope in ["repo:restricted", "collection:notes"] {
        let h = Hub::with(clean(), None);
        let a = h.put(ADMIN_TOKEN, scope, "a", None, OLD).await;
        grant(&h, "repo:other", &["reflect", "review"]).await;
        if scope.starts_with("collection:") {
            grant(&h, "*", &["reflect", "review"]).await;
        }
        assert_eq!(
            submit(&h, AGENT_TOKEN, vec![op(scope, "a", rev(&a))])
                .await
                .status,
            403
        );
        let opened = submit(&h, ADMIN_TOKEN, vec![op(scope, "a", rev(&a))]).await;
        assert_eq!(
            decide(&h, AGENT_TOKEN, &opened.body["proposal_id"])
                .await
                .status,
            403
        );
        grant(&h, scope, &["read", "reflect"]).await;
        assert_eq!(
            submit(&h, AGENT_TOKEN, vec![op(scope, "a", rev(&a))])
                .await
                .status,
            201
        );
        assert_eq!(
            decide(&h, AGENT_TOKEN, &opened.body["proposal_id"])
                .await
                .status,
            403
        );
        grant(&h, scope, &["read", "review"]).await;
        let apply = decide(&h, AGENT_TOKEN, &opened.body["proposal_id"]).await;
        assert_eq!(apply.body["state"], "applied", "{apply:?}");
        grant(&h, scope, &[]).await;
        let rb = json!({"action":"rollback","proposal_id":opened.body["proposal_id"],"base_epoch":h.epoch().await,"client_event_id":h.id()});
        assert_eq!(h.call(AGENT_TOKEN, rb.clone()).await.status, 403);
        grant(&h, scope, &["read", "review"]).await;
        let pending = h.call(AGENT_TOKEN, rb).await;
        assert_eq!(pending.body["state"], "pending", "{pending:?}");
        grant(&h, scope, &[]).await;
        let before = card(&h, scope, "a").await;
        assert_eq!(
            decide(&h, AGENT_TOKEN, &pending.body["proposal_id"])
                .await
                .status,
            403
        );
        assert_eq!(card(&h, scope, "a").await, before);
    }
}
#[tokio::test]
async fn schema_v3_upgrades_without_changing_epoch_or_cards() {
    let mut h = Hub::with(clean(), None);
    h.put(ADMIN_TOKEN, "fleet", "a", None, OLD).await;
    let before = card(&h, "fleet", "a").await;
    let epoch = h.epoch().await;
    h.store.take();
    let db = Connection::open(h.memory()).unwrap();
    let old_max: i64 = db
        .query_row("SELECT max(seq) FROM memory_events", [], |r| r.get(0))
        .unwrap();
    db.execute_batch("DROP TABLE IF EXISTS memory_proposals; PRAGMA user_version=3;")
        .unwrap();
    drop(db);
    h.open(clean(), None);
    assert_eq!(h.epoch().await, epoch);
    assert_eq!(card(&h, "fleet", "a").await, before);
    assert_eq!(
        Connection::open(h.memory())
            .unwrap()
            .pragma_query_value(None, "user_version", |r| r.get::<_, i64>(0))
            .unwrap(),
        4
    );
    let next = h
        .put(ADMIN_TOKEN, "fleet", "after-upgrade", None, "Next card.")
        .await;
    assert_eq!(next.status, 201, "{next:?}");
    assert!(rev(&next) > old_max);
}

#[tokio::test]
async fn metadata_ops_are_reviewed_and_other_card_needs_its_own_scope_grant() {
    let h = Hub::with(clean(), None);
    let a = h.put(ADMIN_TOKEN, "fleet", "a", None, OLD).await;
    let b = h.put(ADMIN_TOKEN, "collection:notes", "b", None, OLD).await;
    let mut merge = op("fleet", "a", rev(&a));
    merge["type"] = json!("merge_exact_duplicates");
    merge.as_object_mut().unwrap().remove("text");
    merge["other_card"] = json!({"scope":"collection:notes","card_id":"b","base_revision":rev(&b)});
    grant(&h, "fleet", &["read", "reflect"]).await;
    assert_eq!(
        submit(&h, AGENT_TOKEN, vec![merge.clone()]).await.status,
        403
    );
    let opened = submit(&h, ADMIN_TOKEN, vec![merge]).await;
    assert_eq!(opened.status, 201, "{opened:?}");
    assert_eq!(
        decide(&h, ADMIN_TOKEN, &opened.body["proposal_id"])
            .await
            .body["state"],
        "applied"
    );
    assert_eq!(card(&h, "collection:notes", "b").await["state"], "retired");
    assert!(
        card(&h, "fleet", "a").await["text"]
            .as_str()
            .unwrap()
            .contains("collection:notes/b")
    );
    let rb=h.call(ADMIN_TOKEN,json!({"action":"rollback","proposal_id":opened.body["proposal_id"],"base_epoch":h.epoch().await,"client_event_id":h.id()})).await;
    assert_eq!(
        decide(&h, ADMIN_TOKEN, &rb.body["proposal_id"]).await.body["state"],
        "applied"
    );
    assert_eq!(card(&h, "collection:notes", "b").await["state"], "live");
    assert_eq!(
        card(&h, "collection:notes", "b").await["text"],
        format!("{OLD}\n")
    );
    let current = card(&h, "fleet", "a").await;
    let mut append = op("fleet", "a", current["revision"].as_i64().unwrap());
    append["type"] = json!("append_evidence");
    append.as_object_mut().unwrap().remove("text");
    append["evidence_ids"] = json!(["node-1/workspace-1/evidence-1"]);
    let opened = submit(&h, ADMIN_TOKEN, vec![append]).await;
    assert_eq!(opened.status, 201, "{opened:?}");
    decide(&h, ADMIN_TOKEN, &opened.body["proposal_id"]).await;
    assert!(
        card(&h, "fleet", "a").await["text"]
            .as_str()
            .unwrap()
            .contains("node-1/workspace-1/evidence-1")
    );
}

#[tokio::test]
async fn scanner_never_raises_trust_without_a_clean_or_private_review() {
    struct Mutable(Arc<std::sync::Mutex<Scan>>);
    impl Scanner for Mutable {
        fn scan(&self, _: &str) -> Scan {
            self.0.lock().unwrap().clone()
        }
        fn describe(&self) -> (bool, Option<String>) {
            (true, Some("test-v7".into()))
        }
    }
    let state = Arc::new(std::sync::Mutex::new(Scan::Clean {
        ruleset: "test-v7".into(),
    }));
    let h = Hub::with(Arc::new(Mutable(state.clone())), None);
    let a = h.put(ADMIN_TOKEN, "fleet", "a", None, OLD).await;
    let before = card(&h, "fleet", "a").await;
    let opened = submit(&h, ADMIN_TOKEN, vec![op("fleet", "a", rev(&a))]).await;
    *state.lock().unwrap() = Scan::Unavailable;
    assert_eq!(
        code(&decide(&h, ADMIN_TOKEN, &opened.body["proposal_id"]).await),
        "memory_scan_unavailable"
    );
    assert_eq!(card(&h, "fleet", "a").await, before);
    *state.lock().unwrap() = Scan::Flagged {
        ruleset: "test-v7".into(),
        rules: vec!["role-override".into()],
    };
    assert_eq!(
        decide(&h, ADMIN_TOKEN, &opened.body["proposal_id"])
            .await
            .body["state"],
        "held"
    );
    assert_eq!(card(&h, "fleet", "a").await, before);
    *state.lock().unwrap() = Scan::Private {
        ruleset: "test-v7".into(),
        rules: vec!["private".into()],
    };
    let applied = decide(&h, ADMIN_TOKEN, &opened.body["proposal_id"]).await;
    assert_eq!(applied.body["state"], "applied", "{applied:?}");
    assert_eq!(card(&h, "fleet", "a").await["trust"], "reviewed");
    assert_eq!(card(&h, "fleet", "a").await["scan_state"], "private");
}

#[tokio::test]
async fn failed_second_card_write_rolls_back_the_first_card_and_decision() {
    let h = Hub::with(clean(), None);
    let a = h.put(ADMIN_TOKEN, "fleet", "a", None, OLD).await;
    let b = h.put(ADMIN_TOKEN, "fleet", "b", None, OLD).await;
    let opened = submit(
        &h,
        ADMIN_TOKEN,
        vec![op("fleet", "a", rev(&a)), op("fleet", "b", rev(&b))],
    )
    .await;
    let before_a = card(&h, "fleet", "a").await;
    let before_b = card(&h, "fleet", "b").await;
    let before_events = h.events();
    Connection::open(h.memory()).unwrap().execute_batch("CREATE TRIGGER fail_second BEFORE INSERT ON memory_card_revisions WHEN NEW.card_id='b' BEGIN SELECT RAISE(ABORT,'injected write failure'); END;").unwrap();
    assert_eq!(
        decide(&h, ADMIN_TOKEN, &opened.body["proposal_id"])
            .await
            .status,
        503
    );
    assert_eq!(card(&h, "fleet", "a").await, before_a);
    assert_eq!(card(&h, "fleet", "b").await, before_b);
    assert_eq!(h.events(), before_events);
    Connection::open(h.memory())
        .unwrap()
        .execute_batch("DROP TRIGGER fail_second")
        .unwrap();
    assert_eq!(
        decide(&h, ADMIN_TOKEN, &opened.body["proposal_id"])
            .await
            .body["state"],
        "applied"
    );
}

#[tokio::test]
async fn epoch_idempotency_scan_and_auto_apply_guards() {
    let h = Hub::with(clean(), None);
    let a = h.put(ADMIN_TOKEN, "fleet", "a", None, OLD).await;
    let mut request = json!({"action":"propose","base_epoch":h.epoch().await,"ops":[op("fleet","a",rev(&a))],"client_event_id":h.id()});
    let opened = h.call(ADMIN_TOKEN, request.clone()).await;
    h.put(
        ADMIN_TOKEN,
        "fleet",
        "a",
        Some(rev(&a)),
        "A later edit. ^c-abcdef",
    )
    .await;
    assert_eq!(h.call(ADMIN_TOKEN, request.clone()).await, opened);
    request["ops"][0]["text"] = json!("Different claim.");
    assert_eq!(
        code(&h.call(ADMIN_TOKEN, request.clone()).await),
        "memory_client_event_conflict"
    );
    request["client_event_id"] = json!(ids::mint_at(h.now() - ids::WINDOW_MS - 1, 1));
    assert_eq!(
        code(&h.call(ADMIN_TOKEN, request.clone()).await),
        "memory_client_event_expired"
    );
    request["client_event_id"] = json!(h.id());
    request["base_epoch"] = json!(ids::mint(1));
    assert_eq!(
        code(&h.call(ADMIN_TOKEN, request).await),
        "memory_epoch_changed"
    );
    let auto=h.call(ADMIN_TOKEN,json!({"action":"decide","proposal_id":opened.body["proposal_id"],"base_epoch":h.epoch().await,"decision":"auto","client_event_id":h.id()})).await;
    assert_eq!(
        (auto.status, code(&auto)),
        (422, "memory_auto_apply_disabled")
    );
    let h = Hub::with(scan::bundled(), None);
    let a = h.put(ADMIN_TOKEN, "fleet", "a", None, OLD).await;
    let mut blocked = op("fleet", "a", rev(&a));
    blocked["text"] = json!("Hidden\u{200b} claim.");
    let events = h.events();
    let r = submit(&h, ADMIN_TOKEN, vec![blocked]).await;
    assert_eq!((r.status, code(&r)), (422, "memory_proposal_invalid"));
    assert_eq!(h.events(), events);
}

#[tokio::test]
async fn proposal_events_do_not_disclose_collection_targets_or_card_bytes() {
    let h = Hub::with(clean(), None);
    let a = h.put(ADMIN_TOKEN, "collection:notes", "a", None, OLD).await;
    let opened = submit(&h, ADMIN_TOKEN, vec![op("collection:notes", "a", rev(&a))]).await;
    decide(&h, ADMIN_TOKEN, &opened.body["proposal_id"]).await;
    let log = h.call(NODE_TOKEN, json!({"action":"log"})).await;
    assert!(!log.body.to_string().contains("collection:notes"));
    let db = Connection::open(h.memory()).unwrap();
    let mut stmt = db
        .prepare("SELECT payload FROM memory_events WHERE kind LIKE 'proposal.%'")
        .unwrap();
    for row in stmt.query_map([], |r| r.get::<_, String>(0)).unwrap() {
        let row = row.unwrap();
        assert!(!row.contains(OLD) && !row.contains(NEW), "{row}");
    }
}

#[tokio::test]
async fn writer_rechecks_target_grants_after_authorized_preparation() {
    for scope in ["repo:restricted", "collection:notes"] {
        for action in ["propose", "decide", "rollback"] {
            let h = Hub::with(clean(), None);
            let a = h.put(ADMIN_TOKEN, scope, "a", None, OLD).await;
            let opened = submit(&h, ADMIN_TOKEN, vec![op(scope, "a", rev(&a))]).await;
            if action == "rollback" {
                decide(&h, ADMIN_TOKEN, &opened.body["proposal_id"]).await;
            }
            grant(&h, scope, &["read", "reflect", "review"]).await;
            let raw = if action == "propose" {
                json!({"action":action,"base_epoch":h.epoch().await,"ops":[op(scope,"a",rev(&a))],"client_event_id":h.id()})
            } else {
                json!({"action":action,"proposal_id":opened.body["proposal_id"],"base_epoch":h.epoch().await,"client_event_id":h.id()})
            };
            let mut raw = raw;
            if action == "decide" {
                raw["decision"] = json!("accept");
            }
            let store = h.store.as_ref().unwrap();
            let who = store.authenticate(AGENT_TOKEN).await.unwrap();
            let mut prepared = store
                .prepare(&who, action, raw.as_object().unwrap(), &raw)
                .unwrap();
            let db = Connection::open(h.memory()).unwrap();
            let command = proposals::prepare(&Conn(&db), &who, raw, clean().as_ref()).unwrap();
            assert!(command.draft.is_ok());
            prepared.action = Action::Proposal(command);
            grant(&h, scope, &["reflect", "review"]).await;
            let events = h.events();
            let before = card(&h, scope, "a").await;
            assert_eq!(store.write(prepared).await.status, 403);
            assert_eq!(h.events(), events);
            assert_eq!(card(&h, scope, "a").await, before);
        }
    }
}

#[tokio::test]
async fn policy_change_since_open_stales_the_decision() {
    let h = Hub::with(clean(), None);
    let a = h.put(ADMIN_TOKEN, "fleet", "a", None, OLD).await;
    let opened = submit(&h, ADMIN_TOKEN, vec![op("fleet", "a", rev(&a))]).await;
    Connection::open(&h.hub).unwrap().execute_batch("INSERT INTO fleet_policy_revisions VALUES(2,'policy-digest-2'); UPDATE fleet_policy_meta SET current_revision=2;").unwrap();
    let before = card(&h, "fleet", "a").await;
    let r = decide(&h, ADMIN_TOKEN, &opened.body["proposal_id"]).await;
    assert_eq!(r.body["state"], "stale");
    assert_eq!(r.body["reason"], "policy_changed");
    assert_eq!(card(&h, "fleet", "a").await, before);
}

#[tokio::test]
async fn duplicate_merge_can_retire_a_source_when_keeper_metadata_already_matches() {
    let h = Hub::with(clean(), None);
    let a = h
        .put(
            ADMIN_TOKEN,
            "fleet",
            "a",
            None,
            &format!("---\naliases: [\"fleet/b\"]\nevidence: []\n---\n{OLD}\n"),
        )
        .await;
    let b = h.put(ADMIN_TOKEN, "fleet", "b", None, OLD).await;
    let mut merge = op("fleet", "a", rev(&a));
    merge["type"] = json!("merge_exact_duplicates");
    merge.as_object_mut().unwrap().remove("text");
    merge["other_card"] = json!({"scope":"fleet","card_id":"b","base_revision":rev(&b)});
    let opened = submit(&h, ADMIN_TOKEN, vec![merge]).await;
    assert_eq!(opened.status, 201, "{opened:?}");
    assert!(opened.body["diff"].as_str().unwrap().contains("+retired"));
    assert_eq!(
        decide(&h, ADMIN_TOKEN, &opened.body["proposal_id"])
            .await
            .body["state"],
        "applied"
    );
    assert_eq!(card(&h, "fleet", "b").await["state"], "retired");
    assert_eq!(card(&h, "fleet", "a").await["revision"], rev(&a));
}
