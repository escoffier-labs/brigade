//! Regression cases from the S19a independent reviews and conductor decisions.
use super::{proposal_tests::*, tests::*, *};
use rusqlite::Connection;
use serde_json::{Value as Json, json};

fn stored(h: &Hub, id: &Json) -> Json {
    let text: String = Connection::open(h.memory())
        .unwrap()
        .query_row(
            "SELECT body FROM memory_proposals WHERE proposal_id=?1",
            [id.as_str().unwrap()],
            |r| r.get(0),
        )
        .unwrap();
    serde_json::from_str(&text).unwrap()
}
#[tokio::test]
async fn duplicate_merge_refuses_python_fences_with_different_indentation() {
    let h = Hub::with(clean(), None);
    let a = h
        .put(
            ADMIN_TOKEN,
            "fleet",
            "a",
            None,
            &format!("{OLD}\n\n```python\nif ready:\n    work()\n    finish()\n```\n"),
        )
        .await;
    let b = h
        .put(
            ADMIN_TOKEN,
            "fleet",
            "b",
            None,
            &format!("{OLD}\n\n```python\nif ready:\n    work()\nfinish()\n```\n"),
        )
        .await;
    let mut merge = op("fleet", "a", rev(&a));
    merge["type"] = json!("merge_exact_duplicates");
    merge.as_object_mut().unwrap().remove("text");
    merge["other_card"] = json!({"scope":"fleet","card_id":"b","base_revision":rev(&b)});
    let before_a = card(&h, "fleet", "a").await;
    let before_b = card(&h, "fleet", "b").await;
    let events = h.events();
    let r = submit(&h, ADMIN_TOKEN, vec![merge]).await;
    assert_eq!(
        (r.status, code(&r)),
        (422, "memory_proposal_invalid"),
        "{r:?}"
    );
    assert_eq!(h.events(), events);
    assert_eq!(card(&h, "fleet", "a").await, before_a);
    assert_eq!(card(&h, "fleet", "b").await, before_b);
}
#[tokio::test]
async fn supersede_refuses_block_scalars_before_blank_line_continuations() {
    for marker in ["|", "|-", "|+", "|2", ">", ">-", ">+", ">2-"] {
        let h = Hub::with(clean(), None);
        let a = h
            .put(
                ADMIN_TOKEN,
                "fleet",
                "a",
                None,
                &format!("---\nstatus: {marker}\n\n  active\ntitle: Example\n---\n{OLD}\n"),
            )
            .await;
        let b = h.put(ADMIN_TOKEN, "fleet", "b", None, OLD).await;
        let mut supersede = op("fleet", "a", rev(&a));
        supersede["type"] = json!("supersede_card");
        supersede.as_object_mut().unwrap().remove("text");
        supersede["other_card"] = json!({"scope":"fleet","card_id":"b","base_revision":rev(&b)});
        let before = card(&h, "fleet", "a").await;
        let events = h.events();
        let r = submit(&h, ADMIN_TOKEN, vec![supersede]).await;
        assert_eq!(
            (r.status, code(&r)),
            (422, "memory_proposal_invalid"),
            "{marker}: {r:?}"
        );
        assert_eq!(h.events(), events);
        assert_eq!(card(&h, "fleet", "a").await, before);
    }
}
#[tokio::test]
async fn rollback_prepared_while_pending_uses_the_writer_applied_body() {
    let h = Hub::with(clean(), None);
    let a = h.put(ADMIN_TOKEN, "fleet", "a", None, OLD).await;
    let opened = submit(&h, ADMIN_TOKEN, vec![op("fleet", "a", rev(&a))]).await;
    let raw = json!({"action":"rollback","proposal_id":opened.body["proposal_id"],"base_epoch":h.epoch().await,"client_event_id":h.id()});
    let store = h.store.as_ref().unwrap();
    let who = Credential::Admin;
    let mut prepared = store
        .prepare(&who, "rollback", raw.as_object().unwrap(), &raw)
        .unwrap();
    let db = Connection::open(h.memory()).unwrap();
    prepared.action =
        Action::Proposal(proposals::prepare(&Conn(&db), &who, raw, clean().as_ref()).unwrap());
    assert_eq!(
        decide(&h, ADMIN_TOKEN, &opened.body["proposal_id"])
            .await
            .body["state"],
        "applied"
    );
    let applied = card(&h, "fleet", "a").await;
    let rb = store.write(prepared).await;
    assert_eq!(rb.body["state"], "pending", "{rb:?}");
    let inverse = stored(&h, &rb.body["proposal_id"]);
    assert_eq!(inverse["rollback_of"], opened.body["proposal_id"]);
    assert_eq!(inverse["cards"][0]["base_revision"], applied["revision"]);
    assert_eq!(card(&h, "fleet", "a").await, applied);
}
#[tokio::test]
async fn pending_compensation_has_no_decision_in_storage_or_events() {
    let h = Hub::with(clean(), None);
    let a = h.put(ADMIN_TOKEN, "fleet", "a", None, OLD).await;
    let opened = submit(&h, ADMIN_TOKEN, vec![op("fleet", "a", rev(&a))]).await;
    decide(&h, ADMIN_TOKEN, &opened.body["proposal_id"]).await;
    let rb=h.call(ADMIN_TOKEN,json!({"action":"rollback","proposal_id":opened.body["proposal_id"],"base_epoch":h.epoch().await,"client_event_id":h.id()})).await;
    let inverse = stored(&h, &rb.body["proposal_id"]);
    assert_eq!(inverse["state"], "pending");
    assert!(inverse["decision"].is_null(), "{inverse}");
    let db = Connection::open(h.memory()).unwrap();
    let mut q = db
        .prepare(
            "SELECT payload FROM memory_events WHERE kind='proposal.rollback' AND revision IS NULL",
        )
        .unwrap();
    let rows = q
        .query_map([], |r| r.get::<_, String>(0))
        .unwrap()
        .collect::<Result<Vec<_>, _>>()
        .unwrap();
    assert!(!rows.is_empty());
    for row in rows {
        let event: Json = serde_json::from_str(&row).unwrap();
        assert_eq!(event["proposal_id"], rb.body["proposal_id"]);
        assert_eq!(event["rollback_of"], opened.body["proposal_id"]);
        assert_eq!(event["state"], "pending");
        assert!(event["decision"].is_null(), "{event}");
    }
    let decided_by: Option<String> = db
        .query_row(
            "SELECT decided_by FROM memory_proposals WHERE proposal_id=?1",
            [rb.body["proposal_id"].as_str().unwrap()],
            |r| r.get(0),
        )
        .unwrap();
    assert!(decided_by.is_none());
}
#[tokio::test]
async fn blocked_or_failed_decision_is_held_with_reason_and_no_card_changes() {
    struct Mutable(Arc<std::sync::Mutex<Scan>>);
    impl Scanner for Mutable {
        fn scan(&self, _: &str) -> Scan {
            self.0.lock().unwrap().clone()
        }
        fn describe(&self) -> (bool, Option<String>) {
            (true, Some("test-v7".into()))
        }
    }
    for (scan, reason, rules) in [
        (
            Scan::Blocked {
                ruleset: "test-v7".into(),
                rules: vec!["blocking-rule".into()],
            },
            "memory_content_blocked",
            json!(["blocking-rule"]),
        ),
        (Scan::Failed, "memory_scan_failed", json!([])),
    ] {
        let state = Arc::new(std::sync::Mutex::new(Scan::Clean {
            ruleset: "test-v7".into(),
        }));
        let h = Hub::with(Arc::new(Mutable(state.clone())), None);
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
        *state.lock().unwrap() = scan;
        let r = decide(&h, ADMIN_TOKEN, &opened.body["proposal_id"]).await;
        assert_eq!(r.status, 200, "{r:?}");
        assert_eq!(r.body["state"], "held");
        assert_eq!(r.body["reason"], reason);
        assert_eq!(r.body["rules"], rules);
        let saved = stored(&h, &opened.body["proposal_id"]);
        assert_eq!(saved["state"], "held");
        assert_eq!(saved["reason"], reason);
        assert_eq!(saved["rules"], rules);
        assert_eq!(card(&h, "fleet", "a").await, before_a);
        assert_eq!(card(&h, "fleet", "b").await, before_b);
        let db = Connection::open(h.memory()).unwrap();
        let payload:String=db.query_row("SELECT payload FROM memory_events WHERE kind='proposal.decide' ORDER BY seq DESC LIMIT 1",[],|r|r.get(0)).unwrap();
        let payload: Json = serde_json::from_str(&payload).unwrap();
        assert_eq!(payload["state"], "held");
        assert_eq!(payload["reason"], reason);
        assert_eq!(payload["rules"], rules);
        *state.lock().unwrap() = Scan::Clean {
            ruleset: "test-v7".into(),
        };
        let applied = decide(&h, ADMIN_TOKEN, &opened.body["proposal_id"]).await;
        assert_eq!(applied.body["state"], "applied");
        assert_eq!(applied.body["rules"], json!([]));
        assert_eq!(stored(&h, &opened.body["proposal_id"])["rules"], json!([]));
        let payload:String=db.query_row("SELECT payload FROM memory_events WHERE kind='proposal.decide' ORDER BY seq DESC LIMIT 1",[],|r|r.get(0)).unwrap();
        let payload: Json = serde_json::from_str(&payload).unwrap();
        assert_eq!(payload["rules"], json!([]));
    }
}
#[tokio::test]
async fn reflect_or_review_without_target_read_never_discloses_card_data() {
    for scope in ["repo:restricted", "collection:notes"] {
        let h = Hub::with(clean(), None);
        let a = h.put(ADMIN_TOKEN, scope, "a", None, OLD).await;
        let opened = submit(&h, ADMIN_TOKEN, vec![op(scope, "a", rev(&a))]).await;
        grant(&h, "*", &["read", "reflect", "review"]).await;
        // Remove wildcard access for the repository case, retaining the
        // collection case to exercise wildcard exclusion of read as well.
        if scope.starts_with("repo:") {
            grant(&h, "*", &[]).await;
        }
        grant(&h, scope, &["reflect"]).await;
        for card_id in ["a", "missing"] {
            let r = submit(&h, AGENT_TOKEN, vec![op(scope, card_id, rev(&a))]).await;
            assert_eq!((r.status, code(&r)), (403, "memory_grant_missing"), "{r:?}");
            assert!(r.body.get("diff").is_none());
            assert!(r.body.get("ops").is_none());
        }
        grant(&h, scope, &["review"]).await;
        assert_eq!(
            code(&decide(&h, AGENT_TOKEN, &opened.body["proposal_id"]).await),
            "memory_grant_missing"
        );
        decide(&h, ADMIN_TOKEN, &opened.body["proposal_id"]).await;
        let rb = json!({"action":"rollback","proposal_id":opened.body["proposal_id"],"base_epoch":h.epoch().await,"client_event_id":h.id()});
        assert_eq!(
            code(&h.call(AGENT_TOKEN, rb.clone()).await),
            "memory_grant_missing"
        );
        grant(&h, scope, &["read", "review"]).await;
        assert_eq!(h.call(AGENT_TOKEN, rb).await.body["state"], "pending");
    }
}
#[tokio::test]
async fn malformed_ops_have_a_top_level_reason_and_empty_op_results() {
    let h = Hub::with(clean(), None);
    for ops in [Json::Null, json!({}), json!([]), json!(vec![json!({}); 65])] {
        let events = h.events();
        let r = submit_raw(&h, ops).await;
        assert_eq!(
            (r.status, code(&r)),
            (422, "memory_proposal_invalid"),
            "{r:?}"
        );
        assert!(r.body["reason"].as_str().is_some_and(|s| !s.is_empty()));
        assert_eq!(r.body["ops"], json!([]));
        assert_eq!(h.events(), events);
    }
}
async fn submit_raw(h: &Hub, ops: Json) -> Reply {
    h.call(
        ADMIN_TOKEN,
        json!({"action":"propose","base_epoch":h.epoch().await,"ops":ops,"client_event_id":h.id()}),
    )
    .await
}
#[tokio::test]
async fn self_accept_is_refused_for_forward_and_compensation_proposers_except_admin() {
    for scope in ["repo:restricted", "collection:notes"] {
        let h = Hub::with(clean(), None);
        let a = h.put(ADMIN_TOKEN, scope, "a", None, OLD).await;
        grant(&h, scope, &["read", "reflect", "review"]).await;
        let opened = submit(&h, AGENT_TOKEN, vec![op(scope, "a", rev(&a))]).await;
        assert_eq!(opened.status, 201, "{opened:?}");
        let before = card(&h, scope, "a").await;
        let events = h.events();
        let denied = decide(&h, AGENT_TOKEN, &opened.body["proposal_id"]).await;
        assert_eq!((denied.status, code(&denied)), (403, "memory_self_review"));
        assert_eq!(h.events(), events);
        assert_eq!(card(&h, scope, "a").await, before);
        let granted=h.call(ADMIN_TOKEN,json!({"action":"grant","principal":"node-1","scope":scope,"rights":["read","review"],"client_event_id":h.id()})).await;
        assert_eq!(granted.status, 200);
        assert_eq!(
            decide(&h, NODE_TOKEN, &opened.body["proposal_id"])
                .await
                .body["state"],
            "applied"
        );
        let decided_by: String = Connection::open(h.memory())
            .unwrap()
            .query_row(
                "SELECT decided_by FROM memory_proposals WHERE proposal_id=?1",
                [opened.body["proposal_id"].as_str().unwrap()],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(decided_by, "node-1");

        let rb=h.call(AGENT_TOKEN,json!({"action":"rollback","proposal_id":opened.body["proposal_id"],"base_epoch":h.epoch().await,"client_event_id":h.id()})).await;
        assert_eq!(rb.body["state"], "pending", "{rb:?}");
        assert_eq!(
            code(&decide(&h, AGENT_TOKEN, &rb.body["proposal_id"]).await),
            "memory_self_review"
        );
        assert_eq!(
            decide(&h, ADMIN_TOKEN, &rb.body["proposal_id"]).await.body["state"],
            "applied"
        );
        let admin = submit(
            &h,
            ADMIN_TOKEN,
            vec![op(
                scope,
                "a",
                card(&h, scope, "a").await["revision"].as_i64().unwrap(),
            )],
        )
        .await;
        assert_eq!(
            decide(&h, ADMIN_TOKEN, &admin.body["proposal_id"])
                .await
                .body["state"],
            "applied"
        );
    }
}

#[tokio::test]
async fn operator_proposal_reads_redact_diffs_and_recheck_all_target_grants() {
    let h = Hub::with(clean(), None);
    let a = h.put(ADMIN_TOKEN, "fleet", "a", None, OLD).await;
    let b = h
        .put(ADMIN_TOKEN, "collection:example", "b", None, OLD)
        .await;
    let opened = submit(
        &h,
        ADMIN_TOKEN,
        vec![
            op("fleet", "a", rev(&a)),
            op("collection:example", "b", rev(&b)),
        ],
    )
    .await;
    let id = &opened.body["proposal_id"];
    grant(&h, "fleet", &["read", "reflect"]).await;
    let denied = h
        .call(
            AGENT_TOKEN,
            json!({"action":"proposal_show","proposal_id":id}),
        )
        .await;
    assert_eq!(denied.status, 403, "{denied:?}");
    let hidden = h.call(AGENT_TOKEN, json!({"action":"proposal_list"})).await;
    assert_eq!(hidden.status, 200, "{hidden:?}");
    assert_eq!(hidden.body["proposals"], json!([]));
    grant(&h, "collection:example", &["read"]).await;
    let before = h.events();
    let redacted = h
        .call(
            AGENT_TOKEN,
            json!({"action":"proposal_show","proposal_id":id}),
        )
        .await;
    assert_eq!(redacted.status, 200, "{redacted:?}");
    assert!(redacted.body["diff"].is_null());
    assert!(redacted.body["targets"][0]["after_hash"].is_null());
    for scope in ["fleet", "collection:example"] {
        grant(&h, scope, &["read", "review"]).await;
    }
    let shown = h
        .call(
            AGENT_TOKEN,
            json!({"action":"proposal_show","proposal_id":id}),
        )
        .await;
    assert!(shown.body["diff"].as_str().unwrap().contains(OLD));
    let event_count = h.events();
    assert!(event_count >= before);
    grant(&h, "collection:example", &[]).await;
    let revoked = h
        .call(
            AGENT_TOKEN,
            json!({"action":"proposal_show","proposal_id":id}),
        )
        .await;
    assert_eq!(revoked.status, 403, "{revoked:?}");
    assert_eq!(stored(&h, id)["state"], "pending");
}

#[tokio::test]
async fn operator_proposal_list_defaults_pending_and_pages_without_diffs() {
    let h = Hub::with(clean(), None);
    let a = h.put(ADMIN_TOKEN, "fleet", "a", None, OLD).await;
    let one = submit(&h, ADMIN_TOKEN, vec![op("fleet", "a", rev(&a))]).await;
    let two = submit(&h, ADMIN_TOKEN, vec![op("fleet", "a", rev(&a))]).await;
    let rejected = h.call(ADMIN_TOKEN, json!({"action":"decide","proposal_id":one.body["proposal_id"],"decision":"reject","base_epoch":h.epoch().await,"client_event_id":h.id()})).await;
    assert_eq!(rejected.body["state"], "rejected");
    let before = h.events();
    let pending = h.call(ADMIN_TOKEN, json!({"action":"proposal_list"})).await;
    assert_eq!(pending.status, 200, "{pending:?}");
    assert_eq!(pending.body["proposals"].as_array().unwrap().len(), 1);
    assert_eq!(
        pending.body["proposals"][0]["proposal_id"],
        two.body["proposal_id"]
    );
    assert!(pending.body["proposals"][0].get("diff").is_none());
    let first = h
        .call(
            ADMIN_TOKEN,
            json!({"action":"proposal_list","all":true,"limit":1}),
        )
        .await;
    let cursor = &first.body["next_after"];
    assert!(cursor.is_string(), "{first:?}");
    let next = h
        .call(
            ADMIN_TOKEN,
            json!({"action":"proposal_list","all":true,"limit":1,"after":cursor}),
        )
        .await;
    assert_eq!(next.body["proposals"].as_array().unwrap().len(), 1);
    assert_ne!(
        first.body["proposals"][0]["proposal_id"],
        next.body["proposals"][0]["proposal_id"]
    );
    assert_eq!(h.events(), before);
}

#[tokio::test]
async fn operator_proposal_reads_validate_fields_and_use_read_admission() {
    let h = Hub::with(clean(), None);
    let before = h.events();
    for request in [
        json!({"action":"proposal_list","all":"yes"}),
        json!({"action":"proposal_list","limit":0}),
        json!({"action":"proposal_list","limit":101}),
        json!({"action":"proposal_list","after":"invalid"}),
        json!({"action":"proposal_list","unexpected":true}),
        json!({"action":"proposal_show","proposal_id":"invalid"}),
    ] {
        assert!(read_only(&serde_json::to_vec(&request).unwrap()));
        let reply = h.call(ADMIN_TOKEN, request).await;
        assert_eq!(reply.status, 400, "{reply:?}");
    }
    assert_eq!(h.events(), before);
}

#[tokio::test]
async fn operator_hidden_pages_use_opaque_cursors_and_refuse_tampering() {
    let h = Hub::with(clean(), None);
    let a = h
        .put(ADMIN_TOKEN, "collection:private", "a", None, OLD)
        .await;
    let mut hidden = Vec::new();
    for _ in 0..3 {
        let opened = submit(
            &h,
            ADMIN_TOKEN,
            vec![op("collection:private", "a", rev(&a))],
        )
        .await;
        hidden.push(opened.body["proposal_id"].as_str().unwrap().to_string());
    }
    // This principal has no grants, so every proposal is hidden.
    let mut after = Json::Null;
    for page in 0..3 {
        let mut request = json!({"action":"proposal_list","limit":1});
        if page != 0 {
            request["after"] = after.clone();
        }
        let reply = h.call(AGENT_TOKEN, request).await;
        assert_eq!(reply.status, 200, "{reply:?}");
        assert_eq!(reply.body["proposals"], json!([]));
        let encoded = reply.body.to_string();
        for id in &hidden {
            assert!(!encoded.contains(id));
            assert!(!encoded.contains(&id[..10]));
        }
        if page < 2 {
            let cursor = reply.body["next_after"].as_str().unwrap();
            assert!(ids::parse_event_id(cursor).is_none());
            assert_ne!(after, reply.body["next_after"]);
            let mut tampered = cursor.as_bytes().to_vec();
            tampered[0] = if tampered[0] == b'a' { b'b' } else { b'a' };
            let refused = h.call(AGENT_TOKEN, json!({"action":"proposal_list","limit":1,"after":String::from_utf8(tampered).unwrap()})).await;
            assert_eq!(refused.status, 400);
        } else {
            assert!(reply.body["next_after"].is_null());
        }
        after = reply.body["next_after"].clone();
    }
}

#[tokio::test]
async fn operator_held_proposals_appear_only_with_all() {
    let h = Hub::with(clean(), None);
    let a = h.put(ADMIN_TOKEN, "fleet", "a", None, OLD).await;
    let opened = submit(&h, ADMIN_TOKEN, vec![op("fleet", "a", rev(&a))]).await;
    let id = &opened.body["proposal_id"];
    // The state column is authoritative for listing, as it is for proposal loads.
    Connection::open(h.memory())
        .unwrap()
        .execute(
            "UPDATE memory_proposals SET state='held' WHERE proposal_id=?1",
            [id.as_str().unwrap()],
        )
        .unwrap();
    let pending = h.call(ADMIN_TOKEN, json!({"action":"proposal_list"})).await;
    assert_eq!(pending.status, 200);
    assert_eq!(pending.body["proposals"], json!([]));
    let all = h
        .call(ADMIN_TOKEN, json!({"action":"proposal_list","all":true}))
        .await;
    assert_eq!(all.body["proposals"][0]["proposal_id"], *id);
    assert_eq!(all.body["proposals"][0]["state"], "held");
}
