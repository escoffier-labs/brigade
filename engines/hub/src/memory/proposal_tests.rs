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
            let action_right = if action == "propose" {
                "reflect"
            } else {
                "review"
            };
            for revoked in ["read", action_right] {
                let h = Hub::with(clean(), None);
                let a = h.put(ADMIN_TOKEN, scope, "a", None, OLD).await;
                let opened = submit(&h, ADMIN_TOKEN, vec![op(scope, "a", rev(&a))]).await;
                if action == "rollback" {
                    decide(&h, ADMIN_TOKEN, &opened.body["proposal_id"]).await;
                }
                // Keep the unscoped action gate satisfied after target revocation.
                grant(&h, "repo:other", &["read", "reflect", "review"]).await;
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
                let retained: Vec<_> = ["read", "reflect", "review"]
                    .into_iter()
                    .filter(|right| *right != revoked)
                    .collect();
                grant(&h, scope, &retained).await;
                let events = h.events();
                let before = card(&h, scope, "a").await;
                let refusal = store.write(prepared).await;
                assert_eq!(
                    (refusal.status, code(&refusal)),
                    (403, "memory_grant_missing"),
                    "{scope} {action} revoked {revoked}: {refusal:?}"
                );
                assert_eq!(h.events(), events);
                assert_eq!(card(&h, scope, "a").await, before);
            }
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

// S19b: classification must depend on hub facts rather than submitted tiers.
fn evidence_op(scope: &str, card_id: &str, revision: i64) -> Json {
    let mut value = op(scope, card_id, revision);
    value["type"] = json!("append_evidence");
    value.as_object_mut().unwrap().remove("text");
    value["evidence_ids"] = json!(["node/workspace/receipt"]);
    value
}
#[tokio::test]
async fn computed_tiers_ignore_claims_and_never_apply_without_review() {
    let h = Hub::with(clean(), None);
    let a = h.put(ADMIN_TOKEN, "fleet", "a", None, OLD).await;
    for (mut operation, want) in [
        (evidence_op("fleet", "a", rev(&a)), 0),
        (
            {
                let mut o = op("fleet", "a", rev(&a));
                o["text"] = json!("Use  the focused gate.");
                o
            },
            1,
        ),
        (op("fleet", "a", rev(&a)), 2),
        (
            {
                let mut o = op("fleet", "a", rev(&a));
                o["text"] = json!("Do not use the focused gate.");
                o
            },
            2,
        ),
    ] {
        operation["tier"] = json!(0);
        let before = card(&h, "fleet", "a").await;
        let r = h.call(ADMIN_TOKEN, json!({"action":"propose","tier":0,"ops":[operation],"base_epoch":h.epoch().await,"client_event_id":h.id()})).await;
        assert_eq!(r.status, 201, "{r:?}");
        assert_eq!(r.body["tier"], want);
        assert_eq!(r.body["state"], "pending");
        assert_eq!(card(&h, "fleet", "a").await, before);
        let db = Connection::open(h.memory()).unwrap();
        let payload: String = db.query_row("SELECT payload FROM memory_events WHERE kind='proposal.open' ORDER BY seq DESC LIMIT 1", [], |r|r.get(0)).unwrap();
        assert_eq!(
            serde_json::from_str::<Json>(&payload).unwrap()["tier"],
            want
        );
        let refused = h.call(ADMIN_TOKEN, json!({"action":"decide","decision":"auto","proposal_id":r.body["proposal_id"],"base_epoch":h.epoch().await,"client_event_id":h.id()})).await;
        assert_eq!(code(&refused), "memory_auto_apply_disabled");
    }
}
#[tokio::test]
async fn cross_scope_and_mixed_trust_inputs_force_tier_two() {
    for mixed_trust in [false, true] {
        let h = Hub::with(clean(), None);
        let a = h.put(ADMIN_TOKEN, "fleet", "a", None, OLD).await;
        let scope = if mixed_trust { "fleet" } else { "repo:example" };
        let b = h.put(ADMIN_TOKEN, scope, "b", None, OLD).await;
        if mixed_trust {
            Connection::open(h.memory())
                .unwrap()
                .execute(
                    "UPDATE memory_cards SET trust='imported' WHERE card_id='b'",
                    [],
                )
                .unwrap();
        }
        let r = submit(
            &h,
            ADMIN_TOKEN,
            vec![
                evidence_op("fleet", "a", rev(&a)),
                evidence_op(scope, "b", rev(&b)),
            ],
        )
        .await;
        assert_eq!(r.status, 201, "{r:?}");
        assert_eq!(r.body["tier"], 2);
    }
}
#[tokio::test]
async fn unknown_scan_or_trust_and_flagged_inputs_force_tier_two() {
    for scan in [
        Scan::Unavailable,
        Scan::Private {
            ruleset: "test-v7".into(),
            rules: vec!["private".into()],
        },
        Scan::Flagged {
            ruleset: "test-v7".into(),
            rules: vec!["role-override".into()],
        },
    ] {
        let h = Hub::with(Arc::new(Fixed(scan)), None);
        let a = h.put(ADMIN_TOKEN, "fleet", "a", None, OLD).await;
        let r = submit(&h, ADMIN_TOKEN, vec![evidence_op("fleet", "a", rev(&a))]).await;
        assert_eq!(r.status, 201, "{r:?}");
        assert_eq!(r.body["tier"], 2);
    }
    let h = Hub::with(clean(), None);
    let a = h.put(ADMIN_TOKEN, "fleet", "a", None, OLD).await;
    Connection::open(h.memory())
        .unwrap()
        .execute_batch(
            "PRAGMA ignore_check_constraints=ON; UPDATE memory_cards SET trust='unknown' WHERE card_id='a';",
        )
        .unwrap();
    assert_eq!(
        submit(&h, ADMIN_TOKEN, vec![evidence_op("fleet", "a", rev(&a))])
            .await
            .body["tier"],
        2
    );
}
#[tokio::test]
async fn newer_admin_edit_reclassifies_lower_tier_proposal_and_keeps_head() {
    let h = Hub::with(clean(), None);
    let a = h.put(ADMIN_TOKEN, "fleet", "a", None, OLD).await;
    let opened = submit(&h, ADMIN_TOKEN, vec![evidence_op("fleet", "a", rev(&a))]).await;
    assert_eq!(opened.body["tier"], 0);
    h.put(
        ADMIN_TOKEN,
        "fleet",
        "a",
        Some(rev(&a)),
        "Human correction. ^c-abcdef",
    )
    .await;
    let before = card(&h, "fleet", "a").await;
    let r = decide(&h, ADMIN_TOKEN, &opened.body["proposal_id"]).await;
    assert_eq!(r.body["tier"], 2);
    assert_eq!(r.body["state"], "stale");
    assert_eq!(card(&h, "fleet", "a").await, before);
}
#[tokio::test]
async fn changed_policy_requires_fresh_decision_bound_to_current_digest() {
    for decision in ["accept", "reject"] {
        let h = Hub::with(clean(), None);
        let a = h.put(ADMIN_TOKEN, "fleet", "a", None, OLD).await;
        let opened = submit(&h, ADMIN_TOKEN, vec![evidence_op("fleet", "a", rev(&a))]).await;
        let request = json!({"action":"decide","proposal_id":opened.body["proposal_id"],"decision":decision,"base_epoch":h.epoch().await,"client_event_id":h.id()});
        Connection::open(&h.hub).unwrap().execute_batch("INSERT INTO fleet_policy_revisions VALUES(2,'policy-digest-2'); UPDATE fleet_policy_meta SET current_revision=2;").unwrap();
        let before = card(&h, "fleet", "a").await;
        let first = h.call(ADMIN_TOKEN, request.clone()).await;
        assert_eq!(first.body["decision_policy_digest"], "policy-digest-2");
        if decision == "accept" {
            assert_eq!(first.body["state"], "stale");
            assert_eq!(card(&h, "fleet", "a").await, before);
            assert_eq!(h.call(ADMIN_TOKEN, request).await, first);
            let next = decide_expected(
                &h,
                ADMIN_TOKEN,
                &opened.body["proposal_id"],
                json!("policy-digest-2"),
            )
            .await;
            assert_eq!(next.body["state"], "applied", "{next:?}");
            assert_eq!(next.body["decision_policy_digest"], "policy-digest-2");
        } else {
            assert_eq!(first.body["state"], "rejected");
        }
        let db = Connection::open(h.memory()).unwrap();
        let (digest, payload): (String, String) = db.query_row("SELECT policy_digest,payload FROM memory_events WHERE kind='proposal.decide' ORDER BY seq DESC LIMIT 1", [], |r|Ok((r.get(0)?,r.get(1)?))).unwrap();
        assert_eq!(digest, "policy-digest-2");
        assert_eq!(
            serde_json::from_str::<Json>(&payload).unwrap()["decision_policy_digest"],
            "policy-digest-2"
        );
    }
}

#[tokio::test]
async fn exact_merges_include_other_card_scope_and_trust_in_classification() {
    for (scope, imported, want) in [
        ("fleet", false, 0),
        ("repo:other", false, 2),
        ("fleet", true, 2),
    ] {
        let h = Hub::with(clean(), None);
        let a = h.put(ADMIN_TOKEN, "fleet", "a", None, OLD).await;
        let b = h.put(ADMIN_TOKEN, scope, "b", None, OLD).await;
        if imported {
            Connection::open(h.memory())
                .unwrap()
                .execute(
                    "UPDATE memory_cards SET trust='imported' WHERE card_id='b'",
                    [],
                )
                .unwrap();
        }
        let mut merge = op("fleet", "a", rev(&a));
        merge["type"] = json!("merge_exact_duplicates");
        merge.as_object_mut().unwrap().remove("text");
        merge["other_card"] = json!({"scope":scope,"card_id":"b","base_revision":rev(&b)});
        let opened = submit(&h, ADMIN_TOKEN, vec![merge]).await;
        assert_eq!(opened.status, 201, "{opened:?}");
        assert_eq!(opened.body["tier"], want);
        assert_eq!(opened.body["state"], "pending");
    }
}
#[tokio::test]
async fn policy_removal_binds_decision_to_absence_instead_of_old_digest() {
    let h = Hub::with(clean(), None);
    let a = h.put(ADMIN_TOKEN, "fleet", "a", None, OLD).await;
    let opened = submit(&h, ADMIN_TOKEN, vec![op("fleet", "a", rev(&a))]).await;
    Connection::open(&h.hub)
        .unwrap()
        .execute("DELETE FROM fleet_policy_meta", [])
        .unwrap();
    let r = decide(&h, ADMIN_TOKEN, &opened.body["proposal_id"]).await;
    assert_eq!(r.body["state"], "stale");
    assert!(r.body["decision_policy_digest"].is_null());
    let digest: Option<String> = Connection::open(h.memory()).unwrap().query_row("SELECT policy_digest FROM memory_events WHERE kind='proposal.decide' ORDER BY seq DESC LIMIT 1", [], |r|r.get(0)).unwrap();
    assert!(digest.is_none());
}

#[tokio::test]
async fn claimed_higher_tiers_are_ignored_and_mixed_ops_take_highest_tier() {
    let h = Hub::with(clean(), None);
    let a = h.put(ADMIN_TOKEN, "fleet", "a", None, OLD).await;
    for claimed in [json!(2), json!("unknown"), json!({"tier":0})] {
        let mut evidence = evidence_op("fleet", "a", rev(&a));
        evidence["tier"] = claimed.clone();
        let r = h.call(ADMIN_TOKEN, json!({"action":"propose","tier":claimed,"ops":[evidence],"base_epoch":h.epoch().await,"client_event_id":h.id()})).await;
        assert_eq!(r.status, 201, "{r:?}");
        assert_eq!(r.body["tier"], 0);
    }
    for reverse in [false, true] {
        let mut cosmetic = op("fleet", "a", rev(&a));
        cosmetic["text"] = json!("Use  the focused gate.");
        let mut ops = vec![cosmetic, evidence_op("fleet", "a", rev(&a))];
        if reverse {
            ops.reverse();
        }
        let r = submit(&h, ADMIN_TOKEN, ops).await;
        assert_eq!(r.status, 201, "{r:?}");
        assert_eq!(r.body["tier"], 1);
    }
}

fn stored_proposal(h: &Hub, id: &Json) -> String {
    Connection::open(h.memory())
        .unwrap()
        .query_row(
            "SELECT body FROM memory_proposals WHERE proposal_id=?1",
            [id.as_str().unwrap()],
            |r| r.get(0),
        )
        .unwrap()
}
fn edit_policy(h: &Hub, revision: i64, digest: &str) {
    let db = Connection::open(&h.hub).unwrap();
    db.execute(
        "INSERT INTO fleet_policy_revisions VALUES(?1,?2)",
        rusqlite::params![revision, digest],
    )
    .unwrap();
    db.execute(
        "UPDATE fleet_policy_meta SET current_revision=?1",
        [revision],
    )
    .unwrap();
}
async fn decide_expected(h: &Hub, token: &str, id: &Json, digest: Json) -> Reply {
    h.call(
        token,
        json!({"action":"decide","proposal_id":id,"decision":"accept",
        "expected_policy_digest":digest,"base_epoch":h.epoch().await,"client_event_id":h.id()}),
    )
    .await
}

#[tokio::test]
async fn s19b_security_merge_requires_identical_non_union_metadata() {
    for (keeper, other, want) in [
        ("status: superseded\nsuperseded_by: fleet/c\n", "", 2),
        ("", "expires: 2030-01-01\n", 2),
        ("visibility: private\n", "visibility: public\n", 2),
        ("status: active\n", "status:  active\n", 2),
        ("status: active\n", "status: active\n", 0),
        (
            "status: active\naliases: [\"fleet/old-a\"]\nevidence: []\n",
            "evidence: [\"node/workspace/receipt\"]\nstatus: active\naliases: []\n",
            0,
        ),
    ] {
        let h = Hub::with(clean(), None);
        let text = |meta: &str| {
            if meta.is_empty() {
                OLD.to_string()
            } else {
                format!("---\n{meta}---\n{OLD}\n")
            }
        };
        let a = h.put(ADMIN_TOKEN, "fleet", "a", None, &text(keeper)).await;
        let b = h.put(ADMIN_TOKEN, "fleet", "b", None, &text(other)).await;
        let mut merge = op("fleet", "a", rev(&a));
        merge["type"] = json!("merge_exact_duplicates");
        merge.as_object_mut().unwrap().remove("text");
        merge["other_card"] = json!({"scope":"fleet","card_id":"b","base_revision":rev(&b)});
        let r = submit(&h, ADMIN_TOKEN, vec![merge]).await;
        assert_eq!(r.status, 201);
        assert_eq!(r.body["tier"], want);
        assert_eq!(r.body["state"], "pending");
    }
}

#[tokio::test]
async fn s19b_security_ops_discard_client_tier_before_storage() {
    let h = Hub::with(clean(), None);
    let a = h.put(ADMIN_TOKEN, "fleet", "a", None, OLD).await;
    let b = h.put(ADMIN_TOKEN, "fleet", "b", None, OLD).await;
    let mut first = evidence_op("fleet", "a", rev(&a));
    first["tier"] = json!(2);
    let mut second = evidence_op("fleet", "b", rev(&b));
    second["tier"] = json!({"untrusted":[0,1,2]});
    let r = submit(&h, ADMIN_TOKEN, vec![first, second]).await;
    assert_eq!(r.status, 201);
    let body: Json = serde_json::from_str(&stored_proposal(&h, &r.body["proposal_id"])).unwrap();
    assert_eq!(body["tier"], 0);
    let ops = body["ops"].as_array().unwrap();
    assert_eq!(ops.len(), 2);
    assert!(ops.iter().all(|op| op.get("tier").is_none()));
}

#[tokio::test]
async fn s19b_security_redecide_requires_matching_current_policy_digest() {
    for decision in ["accept", "reject"] {
        let h = Hub::with(clean(), None);
        let a = h.put(ADMIN_TOKEN, "fleet", "a", None, OLD).await;
        let opened = submit(&h, ADMIN_TOKEN, vec![evidence_op("fleet", "a", rev(&a))]).await;
        let id = &opened.body["proposal_id"];
        edit_policy(&h, 2, "policy-digest-2");
        assert_eq!(decide(&h, ADMIN_TOKEN, id).await.body["state"], "stale");
        let before = stored_proposal(&h, id);
        let head = card(&h, "fleet", "a").await;
        let events = h.events();
        for expected in [
            None,
            Some(json!("policy-digest-1")),
            Some(Json::Null),
            Some(json!({"digest":"policy-digest-2"})),
        ] {
            let mut request = json!({"action":"decide","proposal_id":id,"decision":decision,
                "base_epoch":h.epoch().await,"client_event_id":h.id()});
            if let Some(expected) = expected {
                request["expected_policy_digest"] = expected;
            }
            let r = h.call(ADMIN_TOKEN, request).await;
            assert_eq!((r.status, code(&r)), (409, "memory_policy_digest_mismatch"));
            assert_eq!(stored_proposal(&h, id), before);
            assert_eq!(card(&h, "fleet", "a").await, head);
            assert_eq!(h.events(), events);
        }
        // A digest seen in an earlier stale response cannot acknowledge a later policy.
        edit_policy(&h, 3, "policy-digest-3");
        let r = decide_expected(&h, ADMIN_TOKEN, id, json!("policy-digest-2")).await;
        assert_eq!((r.status, code(&r)), (409, "memory_policy_digest_mismatch"));
        assert_eq!(stored_proposal(&h, id), before);
        assert_eq!(h.events(), events);
        let r = h.call(ADMIN_TOKEN, json!({"action":"decide","proposal_id":id,"decision":decision,
            "expected_policy_digest":"policy-digest-3","base_epoch":h.epoch().await,"client_event_id":h.id()})).await;
        assert_eq!(r.status, 200);
        assert_eq!(
            r.body["state"],
            if decision == "accept" {
                "applied"
            } else {
                "rejected"
            }
        );
        assert_eq!(r.body["decision_policy_digest"], "policy-digest-3");
    }
}

#[tokio::test]
async fn s19b_security_redecide_rechecks_moved_head() {
    let h = Hub::with(clean(), None);
    let a = h.put(ADMIN_TOKEN, "fleet", "a", None, OLD).await;
    let opened = submit(&h, ADMIN_TOKEN, vec![evidence_op("fleet", "a", rev(&a))]).await;
    edit_policy(&h, 2, "policy-digest-2");
    h.put(
        ADMIN_TOKEN,
        "fleet",
        "a",
        Some(rev(&a)),
        "Human correction. ^c-abcdef",
    )
    .await;
    let id = &opened.body["proposal_id"];
    let first = decide(&h, ADMIN_TOKEN, id).await;
    assert_eq!(first.body["reason"], "policy_changed");
    let head = card(&h, "fleet", "a").await;
    let r = decide_expected(&h, ADMIN_TOKEN, id, json!("policy-digest-2")).await;
    assert_eq!(r.status, 200);
    assert_eq!(r.body["state"], "stale");
    assert_eq!(r.body["reason"], "memory_base_mismatch");
    assert_eq!(card(&h, "fleet", "a").await, head);
}

#[tokio::test]
async fn s19b_security_redecide_preserves_self_review_and_target_grants() {
    for self_review in [true, false] {
        let h = Hub::with(clean(), None);
        let a = h.put(ADMIN_TOKEN, "fleet", "a", None, OLD).await;
        grant(&h, "fleet", &["read", "reflect", "review"]).await;
        let opened = submit(
            &h,
            if self_review {
                AGENT_TOKEN
            } else {
                ADMIN_TOKEN
            },
            vec![evidence_op("fleet", "a", rev(&a))],
        )
        .await;
        let id = &opened.body["proposal_id"];
        edit_policy(&h, 2, "policy-digest-2");
        let first = decide(
            &h,
            if self_review {
                ADMIN_TOKEN
            } else {
                AGENT_TOKEN
            },
            id,
        )
        .await;
        assert_eq!(first.body["reason"], "policy_changed");
        if !self_review {
            grant(&h, "fleet", &["read"]).await;
        }
        let before = stored_proposal(&h, id);
        let head = card(&h, "fleet", "a").await;
        let events = h.events();
        let r = decide_expected(&h, AGENT_TOKEN, id, json!("policy-digest-2")).await;
        assert_eq!(
            (r.status, code(&r)),
            (
                403,
                if self_review {
                    "memory_self_review"
                } else {
                    "memory_grant_missing"
                }
            )
        );
        assert_eq!(stored_proposal(&h, id), before);
        assert_eq!(card(&h, "fleet", "a").await, head);
        assert_eq!(h.events(), events);
    }
}

#[tokio::test]
async fn s19b_security_redecide_acknowledges_policy_absence_explicitly() {
    let h = Hub::with(clean(), None);
    let a = h.put(ADMIN_TOKEN, "fleet", "a", None, OLD).await;
    let opened = submit(&h, ADMIN_TOKEN, vec![evidence_op("fleet", "a", rev(&a))]).await;
    let id = &opened.body["proposal_id"];
    Connection::open(&h.hub)
        .unwrap()
        .execute("DELETE FROM fleet_policy_meta", [])
        .unwrap();
    assert_eq!(
        decide(&h, ADMIN_TOKEN, id).await.body["reason"],
        "policy_changed"
    );
    assert_eq!(
        code(&decide(&h, ADMIN_TOKEN, id).await),
        "memory_policy_digest_mismatch"
    );
    let r = decide_expected(&h, ADMIN_TOKEN, id, Json::Null).await;
    assert_eq!(r.body["state"], "applied");
    assert!(r.body["decision_policy_digest"].is_null());
}

#[tokio::test]
async fn s19b_security_normal_decide_checks_digest_when_supplied() {
    let h = Hub::with(clean(), None);
    let a = h.put(ADMIN_TOKEN, "fleet", "a", None, OLD).await;
    let opened = submit(&h, ADMIN_TOKEN, vec![evidence_op("fleet", "a", rev(&a))]).await;
    let id = &opened.body["proposal_id"];
    let before = stored_proposal(&h, id);
    let events = h.events();
    let r = decide_expected(&h, ADMIN_TOKEN, id, json!("wrong-digest")).await;
    assert_eq!((r.status, code(&r)), (409, "memory_policy_digest_mismatch"));
    assert_eq!(stored_proposal(&h, id), before);
    assert_eq!(h.events(), events);
    let r = decide_expected(&h, ADMIN_TOKEN, id, json!("policy-digest-1")).await;
    assert_eq!(r.body["state"], "applied");
}

#[tokio::test]
async fn s19b_security_merge_rejects_hidden_python_metadata_lines() {
    for separator in [
        "\u{85}", "\\u0085", "\u{2028}", "\\u2028", "\u{2029}", "\\u2029",
    ] {
        for hidden in ["expires: 2020-01-01", "status: superseded"] {
            for hidden_on_keeper in [true, false] {
                let h = Hub::with(clean(), None);
                let hidden_text =
                    format!("---\naliases: [\"{separator}{hidden}{separator}\"]\n---\n{OLD}\n");
                let normal_text = format!("---\naliases: []\n---\n{OLD}\n");
                let (keeper, other) = if hidden_on_keeper {
                    (&hidden_text, &normal_text)
                } else {
                    (&normal_text, &hidden_text)
                };
                let a = h.put(ADMIN_TOKEN, "fleet", "a", None, keeper).await;
                let b = h.put(ADMIN_TOKEN, "fleet", "b", None, other).await;
                assert_eq!((a.status, b.status), (201, 201));
                let mut merge = op("fleet", "a", rev(&a));
                merge["type"] = json!("merge_exact_duplicates");
                merge.as_object_mut().unwrap().remove("text");
                merge["other_card"] =
                    json!({"scope":"fleet","card_id":"b","base_revision":rev(&b)});
                let r = submit(&h, ADMIN_TOKEN, vec![merge]).await;
                assert_eq!(r.status, 201);
                assert_eq!(r.body["tier"], 2);
                assert_eq!(r.body["state"], "pending");
            }
        }
    }
}

#[tokio::test]
async fn s19b_security_evidence_roundtrip_stays_escaped_and_review_only() {
    for (escaped, separator) in [
        ("\\u0085", '\u{85}'),
        ("\\u2028", '\u{2028}'),
        ("\\u2029", '\u{2029}'),
    ] {
        for hidden in ["expires: 2020-01-01", "status: superseded"] {
            let h = Hub::with(clean(), None);
            let original = format!(
                "---\nevidence: [\"{escaped}{hidden}{escaped}\"]\nstatus: active\n---\n{OLD}\n"
            );
            let a = h.put(ADMIN_TOKEN, "fleet", "a", None, &original).await;
            let opened = submit(&h, ADMIN_TOKEN, vec![evidence_op("fleet", "a", rev(&a))]).await;
            assert_eq!(opened.status, 201);
            assert_eq!(opened.body["tier"], 2);
            let body: Json =
                serde_json::from_str(&stored_proposal(&h, &opened.body["proposal_id"])).unwrap();
            let after = body["cards"][0]["after_text"].as_str().unwrap();
            assert!(!after.contains(separator));
            assert!(after.contains(&format!("{escaped}{hidden}{escaped}")));
            let fields = text::front_matter(after);
            assert_eq!(
                fields
                    .iter()
                    .map(|(key, _)| key.as_str())
                    .collect::<Vec<_>>(),
                ["status", "evidence"]
            );
            assert_eq!(fields[0].1, text::Value::Str("active".into()));
            assert_eq!(
                decide(&h, ADMIN_TOKEN, &opened.body["proposal_id"])
                    .await
                    .body["state"],
                "applied"
            );
            assert!(
                !card(&h, "fleet", "a").await["text"]
                    .as_str()
                    .unwrap()
                    .contains(separator)
            );
        }
    }
}

#[tokio::test]
async fn s19b_security_all_lower_tiers_preserve_protected_metadata() {
    for kind in ["append_evidence", "replace_block", "merge_exact_duplicates"] {
        let h = Hub::with(clean(), None);
        let original = format!("---\nstatus: active\naliases: []\nevidence: []\n---\n{OLD}\n");
        let a = h.put(ADMIN_TOKEN, "fleet", "a", None, &original).await;
        let mut operation = evidence_op("fleet", "a", rev(&a));
        if kind == "replace_block" {
            operation = op("fleet", "a", rev(&a));
            operation["text"] = json!("Use  the focused gate.");
        } else if kind == "merge_exact_duplicates" {
            let b = h.put(ADMIN_TOKEN, "fleet", "b", None, &original).await;
            operation["type"] = json!(kind);
            operation.as_object_mut().unwrap().remove("evidence_ids");
            operation["other_card"] =
                json!({"scope":"fleet","card_id":"b","base_revision":rev(&b)});
        }
        let opened = submit(&h, ADMIN_TOKEN, vec![operation]).await;
        assert_eq!(opened.status, 201);
        assert_eq!(
            opened.body["tier"],
            if kind == "replace_block" { 1 } else { 0 }
        );
        let body: Json =
            serde_json::from_str(&stored_proposal(&h, &opened.body["proposal_id"])).unwrap();
        for (old, new) in [
            ("status: active", "status: superseded"),
            (
                "status: active",
                "status: active\u{2028}expires: 2020-01-01",
            ),
            (
                if kind == "append_evidence" {
                    "aliases: []"
                } else {
                    "status: active"
                },
                if kind == "append_evidence" {
                    "aliases: [\"different\"]"
                } else {
                    "status: inactive"
                },
            ),
        ] {
            // Model a faulty metadata writer while keeping the actual pinned
            // input and current head. The classifier must independently refuse it.
            let mut altered = body.clone();
            let after = altered["cards"][0]["after_text"]
                .as_str()
                .unwrap()
                .replace(old, new);
            altered["cards"][0]["after_hash"] = json!(ids::sha256_hex(after.as_bytes()));
            altered["cards"][0]["after_text"] = json!(after);
            let db = Connection::open(h.memory()).unwrap();
            assert_eq!(
                proposal_tiers::classify(&Conn(&db), &altered, clean().as_ref()).unwrap(),
                2
            );
        }
    }
}
