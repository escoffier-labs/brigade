//! S19c gates must use live hub facts and must never grant apply authority.
use super::{proposal_tests::*, tests::*, *};
use rusqlite::Connection;

fn evidence(revision: i64) -> Json {
    let mut o = op("fleet", "a", revision);
    o.as_object_mut().unwrap().remove("text");
    o["type"] = json!("append_evidence");
    o["evidence_ids"] = json!([
        "node-a/workspace-a/one",
        "node-b/workspace-b/two",
        "node-a/workspace-a/three"
    ]);
    o
}
fn policy(h: &Hub, memory: Json) {
    let db = Connection::open(&h.hub).unwrap();
    db.execute(
        "UPDATE fleet_policy_revisions SET document=?1 WHERE revision=1",
        [json!({"memory":memory}).to_string()],
    )
    .unwrap();
}
async fn preflight(h: &Hub, token: &str, id: &Json) -> Reply {
    h.call(token, json!({"action":"preflight","proposal_id":id}))
        .await
}
fn check(report: &Reply, gate: &str) -> String {
    report.body["checks"]
        .as_array()
        .unwrap()
        .iter()
        .find(|c| c["gate"] == gate)
        .unwrap()["code"]
        .as_str()
        .unwrap()
        .into()
}
fn snapshot(h: &Hub) -> Vec<String> {
    let db = Connection::open(h.memory()).unwrap();
    // Include blobs, vectors, jobs, grants, proposals, decisions and idempotency.
    let tables: Vec<String> = db
        .prepare("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        .unwrap()
        .query_map([], |r| r.get(0))
        .unwrap()
        .map(Result::unwrap)
        .collect();
    tables
        .iter()
        .map(|table| {
            let mut stmt = db.prepare(&format!("SELECT * FROM {table}")).unwrap();
            let columns = stmt.column_count();
            let mut rows: Vec<String> = stmt
                .query_map([], |r| {
                    Ok((0..columns)
                        .map(|i| format!("{:?}", r.get_ref(i).unwrap()))
                        .collect::<Vec<_>>()
                        .join("|"))
                })
                .unwrap()
                .map(Result::unwrap)
                .collect();
            rows.sort();
            format!("{table}:{rows:?}")
        })
        .collect()
}
#[tokio::test]
async fn preflight_uses_hub_facts_without_writes_or_fabricated_proof() {
    let h = Hub::with(clean(), None);
    policy(
        &h,
        json!({"auto_apply":false,"tier0_op_classes":["append_evidence"],"reflect_principals":["hub:admin"]}),
    );
    let a = h.put(ADMIN_TOKEN, "fleet", "a", None, OLD).await;
    let opened = submit(&h, ADMIN_TOKEN, vec![evidence(rev(&a))]).await;
    let before = snapshot(&h);
    let r = preflight(&h, ADMIN_TOKEN, &opened.body["proposal_id"]).await;
    assert_eq!(r.status, 200, "{r:?}");
    assert_eq!(r.body["schema"], "brigade.memory-tier0-preflight.v1");
    assert_eq!(r.body["diagnostic_only"], true);
    assert_eq!(r.body["eligible"], false);
    assert_eq!(r.body["status"], "held");
    assert_eq!(r.body["policy_sha256"], "policy-digest-1");
    assert_eq!(r.body["proof_evidence_count"], Json::Null);
    assert_eq!(r.body["proof_source_group_count"], Json::Null);
    assert_eq!(check(&r, "policy"), "policy_satisfied");
    assert_eq!(check(&r, "candidate"), "candidate_satisfied");
    assert_eq!(check(&r, "snapshot"), "snapshot_satisfied");
    assert_eq!(
        check(&r, "evidence_authority"),
        "evidence_authority_unbound"
    );
    assert_eq!(check(&r, "retrieval_eval"), "retrieval_eval_unbound");
    assert_eq!(before, snapshot(&h));
    assert_eq!(
        r,
        preflight(&h, ADMIN_TOKEN, &opened.body["proposal_id"]).await
    );
}
#[tokio::test]
async fn preflight_enforces_both_policy_allowlists_without_changing_edit_tiers() {
    for (memory, expected) in [
        (json!({}), "class_disabled"),
        (
            json!({"tier0_op_classes":["merge_exact_duplicates"],"reflect_principals":["hub:admin"]}),
            "class_disabled",
        ),
        (
            json!({"tier0_op_classes":["append_evidence"],"reflect_principals":[]}),
            "reflect_principal_disabled",
        ),
        (
            json!({"tier0_op_classes":["append_evidence"],"reflect_principals":["agent:other"]}),
            "reflect_principal_disabled",
        ),
    ] {
        let h = Hub::with(clean(), None);
        policy(&h, memory);
        let a = h.put(ADMIN_TOKEN, "fleet", "a", None, OLD).await;
        let opened = submit(&h, ADMIN_TOKEN, vec![evidence(rev(&a))]).await;
        assert_eq!(opened.body["tier"], 0);
        let r = preflight(&h, ADMIN_TOKEN, &opened.body["proposal_id"]).await;
        assert_eq!(r.status, 200, "{r:?}");
        assert_eq!(check(&r, "policy"), expected);
        assert_eq!(r.body["eligible"], false);
    }
}
#[tokio::test]
async fn preflight_requires_only_read_but_read_on_every_target() {
    let h = Hub::with(clean(), None);
    let a = h.put(ADMIN_TOKEN, "collection:notes", "a", None, OLD).await;
    let opened = submit(&h, ADMIN_TOKEN, vec![op("collection:notes", "a", rev(&a))]).await;
    grant(&h, "*", &["reflect", "review", "read"]).await;
    let before = snapshot(&h);
    let r = preflight(&h, AGENT_TOKEN, &opened.body["proposal_id"]).await;
    assert_eq!((r.status, code(&r)), (403, "memory_grant_missing"));
    assert_eq!(before, snapshot(&h));
    grant(&h, "collection:notes", &["read"]).await;
    assert_eq!(
        preflight(&h, AGENT_TOKEN, &opened.body["proposal_id"])
            .await
            .status,
        200
    );
}
#[tokio::test]
async fn decide_rechecks_trust_and_scan_state_after_preflight_and_preparation() {
    for mutation in ["trust", "scan_state"] {
        let h = Hub::with(clean(), None);
        let a = h.put(ADMIN_TOKEN, "fleet", "a", None, OLD).await;
        let opened = submit(&h, ADMIN_TOKEN, vec![evidence(rev(&a))]).await;
        let id = &opened.body["proposal_id"];
        assert_eq!(preflight(&h, ADMIN_TOKEN, id).await.status, 200);
        let credential = h
            .store
            .as_ref()
            .unwrap()
            .authenticate(ADMIN_TOKEN)
            .await
            .unwrap();
        let request = json!({"action":"decide","proposal_id":id,"decision":"accept","base_epoch":h.epoch().await,"client_event_id":h.id()});
        let mut prepared = h
            .store
            .as_ref()
            .unwrap()
            .prepare(
                &credential,
                "decide",
                request.as_object().unwrap(),
                &request,
            )
            .unwrap();
        let db = Connection::open(h.memory()).unwrap();
        let tx = crate::writer::worker::Tx(&db);
        prepared.action = Action::Proposal(
            proposals::prepare(&tx, &credential, request, clean().as_ref()).unwrap(),
        );
        if mutation == "trust" {
            db.execute(
                "UPDATE memory_cards SET trust='flagged' WHERE card_id='a'",
                [],
            )
            .unwrap();
        } else {
            db.execute(
                "UPDATE memory_card_revisions SET scan_state='flagged' WHERE card_id='a'",
                [],
            )
            .unwrap();
        }
        let before = card(&h, "fleet", "a").await;
        let r = h.store.as_ref().unwrap().write(prepared).await;
        assert_eq!(r.body["state"], "held", "{mutation}: {r:?}");
        assert_eq!(r.body["tier"], 2);
        assert_eq!(card(&h, "fleet", "a").await, before);
    }
}
#[tokio::test]
async fn preflight_rechecks_pins_heads_policy_and_tier() {
    for mutation in ["head", "pin", "policy"] {
        let h = Hub::with(clean(), None);
        let a = h.put(ADMIN_TOKEN, "fleet", "a", None, OLD).await;
        let opened = submit(&h, ADMIN_TOKEN, vec![evidence(rev(&a))]).await;
        let id = &opened.body["proposal_id"];
        assert_eq!(
            check(&preflight(&h, ADMIN_TOKEN, id).await, "snapshot"),
            "snapshot_satisfied"
        );
        match mutation {
            "head" => {
                h.put(
                    ADMIN_TOKEN,
                    "fleet",
                    "a",
                    Some(rev(&a)),
                    "New fact. ^c-abcdef",
                )
                .await;
            }
            "pin" => {
                Connection::open(h.memory())
                    .unwrap()
                    .execute(
                        "UPDATE memory_blobs SET bytes=?1 WHERE content_hash=?2",
                        rusqlite::params![
                            b"Corrupt bytes. ^c-abcdef\n",
                            a.body["event"]["content_hash"].as_str().unwrap()
                        ],
                    )
                    .unwrap();
            }
            _ => {
                Connection::open(&h.hub)
                    .unwrap()
                    .execute("UPDATE fleet_policy_revisions SET digest='changed'", [])
                    .unwrap();
            }
        }
        let before = snapshot(&h);
        let r = preflight(&h, ADMIN_TOKEN, id).await;
        assert_ne!(
            check(&r, "snapshot"),
            "snapshot_satisfied",
            "{mutation}: {r:?}"
        );
        assert_eq!(before, snapshot(&h));
        let before = card(&h, "fleet", "a").await;
        let r = decide(&h, ADMIN_TOKEN, id).await;
        assert_eq!(r.body["state"], "stale");
        assert_eq!(card(&h, "fleet", "a").await, before);
    }
}
#[tokio::test]
async fn every_typed_op_refuses_non_card_targets_and_non_block_addresses() {
    let h = Hub::with(clean(), None);
    let a = h.put(ADMIN_TOKEN, "fleet", "a", None, OLD).await;
    let b = h.put(ADMIN_TOKEN, "fleet", "b", None, OLD).await;
    for kind in [
        "replace_block",
        "remove_block",
        "add_previously",
        "append_evidence",
        "supersede_card",
        "merge_exact_duplicates",
    ] {
        let mut valid = op("fleet", "a", rev(&a));
        valid["type"] = json!(kind);
        if !matches!(kind, "replace_block" | "add_previously") {
            valid.as_object_mut().unwrap().remove("text");
        }
        if kind == "append_evidence" {
            valid["evidence_ids"] = json!(["node/workspace/proof"]);
        }
        if matches!(kind, "supersede_card" | "merge_exact_duplicates") {
            valid["other_card"] = json!({"scope":"fleet","card_id":"b","base_revision":rev(&b)});
        }
        let baseline = submit(&h, ADMIN_TOKEN, vec![valid.clone()]).await;
        assert_eq!(baseline.status, 201, "{kind}: {baseline:?}");
        for target in ["AGENTS.md", "CLAUDE.md", "skills/example.md", "policy.json"] {
            let mut o = valid.clone();
            o["path"] = json!(target);
            let before = snapshot(&h);
            let r = submit(&h, ADMIN_TOKEN, vec![o]).await;
            assert_eq!((r.status, code(&r)), (422, "memory_proposal_invalid"));
            assert_eq!(before, snapshot(&h));
        }
    }
    for block in ["AGENTS.md", "", "../c-abcdef", "c-000000"] {
        let mut o = evidence(rev(&a));
        o["block_id"] = json!(block);
        let before = snapshot(&h);
        assert_eq!(submit(&h, ADMIN_TOKEN, vec![o]).await.status, 422);
        assert_eq!(before, snapshot(&h));
    }
}
#[tokio::test]
async fn preflight_blast_cap_and_cooldown_use_actual_changes_and_hub_history() {
    let h = Hub::with(clean(), None);
    let a = h.put(ADMIN_TOKEN, "fleet", "a", None, OLD).await;
    let b = h.put(ADMIN_TOKEN, "fleet", "b", None, OLD).await;
    let mut second = evidence(rev(&b));
    second["card_id"] = json!("b");
    let opened = submit(&h, ADMIN_TOKEN, vec![evidence(rev(&a)), second]).await;
    let r = preflight(&h, ADMIN_TOKEN, &opened.body["proposal_id"]).await;
    assert_eq!(check(&r, "delta"), "blast_cap_exceeded");
    assert_eq!(r.body["changed_path_count"], 2);
    assert_eq!(check(&r, "cooldown_history"), "cooldown_history_satisfied");
    // A human may still review a multi-card edit. Diagnostic defaults grant no authority.
    let applied = decide(&h, ADMIN_TOKEN, &opened.body["proposal_id"]).await;
    assert_eq!(applied.body["state"], "applied", "{applied:?}");
    let head = card(&h, "fleet", "a").await;
    let mut next = evidence(head["revision"].as_i64().unwrap());
    next["evidence_ids"] = json!(["node-c/workspace-c/four"]);
    let opened = submit(&h, ADMIN_TOKEN, vec![next]).await;
    let r = preflight(&h, ADMIN_TOKEN, &opened.body["proposal_id"]).await;
    assert_eq!(check(&r, "cooldown_history"), "cooldown_active");
    h.set_clock(h.now() + 86_400_000);
    assert_eq!(
        check(
            &preflight(&h, ADMIN_TOKEN, &opened.body["proposal_id"]).await,
            "cooldown_history"
        ),
        "cooldown_history_satisfied"
    );
}
#[tokio::test]
async fn failed_atomic_write_keeps_all_card_and_vector_bytes() {
    let h = Hub::with(clean(), None);
    let a = h.put(ADMIN_TOKEN, "fleet", "a", None, OLD).await;
    let b = h.put(ADMIN_TOKEN, "fleet", "b", None, OLD).await;
    let opened = submit(
        &h,
        ADMIN_TOKEN,
        vec![op("fleet", "a", rev(&a)), op("fleet", "b", rev(&b))],
    )
    .await;
    let db = Connection::open(h.memory()).unwrap();
    db.execute(
        "INSERT INTO memory_vectors VALUES(?1,'test-embedder',1,?2)",
        rusqlite::params![
            a.body["event"]["content_hash"].as_str().unwrap(),
            b"synthetic-vector-bytes"
        ],
    )
    .unwrap();
    db.execute_batch("CREATE TRIGGER fail_second BEFORE INSERT ON memory_card_revisions WHEN NEW.card_id='b' BEGIN SELECT RAISE(ABORT,'injected write failure'); END;").unwrap();
    let before = snapshot(&h);
    let failed = decide(&h, ADMIN_TOKEN, &opened.body["proposal_id"]).await;
    assert_eq!(failed.status, 503);
    assert_eq!(snapshot(&h), before);
}
#[tokio::test]
async fn policy_document_is_reread_under_hub_lock_after_preflight_and_preparation() {
    use std::sync::atomic::{AtomicBool, Ordering};
    let mut h = Hub::with(clean(), None);
    policy(
        &h,
        json!({"auto_apply":false,"tier0_op_classes":["append_evidence"],"reflect_principals":["hub:admin"]}),
    );
    h.store.take();
    let armed = Arc::new(AtomicBool::new(false));
    let flag = armed.clone();
    let hub = h.hub.clone();
    h.open(clean(), Some(Arc::new(move |step| {
        if step == writer::Step::MemoryLocked && flag.swap(false, Ordering::SeqCst) {
            Connection::open(&hub).unwrap().execute_batch("INSERT INTO fleet_policy_revisions(revision,digest,document) VALUES(2,'policy-digest-2','{\"memory\":{\"auto_apply\":false,\"tier0_op_classes\":[\"append_evidence\"],\"reflect_principals\":[]}}'); UPDATE fleet_policy_meta SET current_revision=2;").unwrap();
        }
    })));
    let a = h.put(ADMIN_TOKEN, "fleet", "a", None, OLD).await;
    let opened = submit(&h, ADMIN_TOKEN, vec![evidence(rev(&a))]).await;
    let id = &opened.body["proposal_id"];
    assert_eq!(
        check(&preflight(&h, ADMIN_TOKEN, id).await, "policy"),
        "policy_satisfied"
    );
    let before = card(&h, "fleet", "a").await;
    armed.store(true, Ordering::SeqCst);
    let r = decide(&h, ADMIN_TOKEN, id).await;
    assert_eq!(r.body["state"], "stale");
    assert_eq!(r.body["reason"], "policy_changed");
    assert_eq!(r.body["decision_policy_digest"], "policy-digest-2");
    assert_eq!(card(&h, "fleet", "a").await, before);
    let r = h.call(ADMIN_TOKEN,json!({"action":"decide","proposal_id":id,"decision":"accept","expected_policy_digest":"policy-digest-2","base_epoch":h.epoch().await,"client_event_id":h.id()})).await;
    assert_eq!(r.body["state"], "applied", "{r:?}");
    let report = Reply::new(200, r.body["preflight"].clone());
    assert_eq!(check(&report, "policy"), "reflect_principal_disabled");
}

fn stored_body(h: &Hub, id: &Json) -> Json {
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
fn save_body(h: &Hub, id: &Json, body: &Json) {
    Connection::open(h.memory())
        .unwrap()
        .execute(
            "UPDATE memory_proposals SET body=?1 WHERE proposal_id=?2",
            [body.to_string(), id.as_str().unwrap().to_string()],
        )
        .unwrap();
}
#[tokio::test]
async fn preflight_disclosure_requires_review_on_every_target() {
    let h = Hub::with(clean(), None);
    policy(
        &h,
        json!({"tier0_op_classes":["append_evidence"],"reflect_principals":["hub:admin"]}),
    );
    let a = h.put(ADMIN_TOKEN, "fleet", "a", None, OLD).await;
    let b = h.put(ADMIN_TOKEN, "collection:notes", "b", None, OLD).await;
    let opened = submit(
        &h,
        ADMIN_TOKEN,
        vec![evidence(rev(&a)), op("collection:notes", "b", rev(&b))],
    )
    .await;
    let id = &opened.body["proposal_id"];
    grant(&h, "fleet", &["read"]).await;
    grant(&h, "collection:notes", &["read"]).await;
    for review_first_target in [false, true] {
        if review_first_target {
            grant(&h, "collection:notes", &["read", "review"]).await;
        }
        for token in [AGENT_TOKEN, ADMIN_TOKEN] {
            let before = snapshot(&h);
            let r = preflight(&h, token, id).await;
            assert_eq!(r.status, 200, "{r:?}");
            for value in [
                &r.body["candidate_sha256"],
                &r.body["delta_sha256"],
                &r.body["policy_limits"]["reflect_principals"],
            ] {
                assert_eq!(value.is_null(), token == AGENT_TOKEN);
            }
            // A digest over hidden values would still disclose guesses indirectly.
            let mut visible = r.body.clone();
            visible["evaluation_sha256"] = Json::Null;
            assert_eq!(
                r.body["evaluation_sha256"],
                ids::sha256_hex(ids::canonical(&visible).as_bytes())
            );
            assert_eq!(before, snapshot(&h));
        }
    }
    grant(&h, "fleet", &["read", "review"]).await;
    let r = preflight(&h, AGENT_TOKEN, id).await;
    assert!(r.body["candidate_sha256"].is_string());
    assert!(r.body["delta_sha256"].is_string());
    assert_eq!(
        r.body["policy_limits"]["reflect_principals"],
        json!(["hub:admin"])
    );
}
#[tokio::test]
async fn legacy_scan_binding_stays_unbound_until_rejected_and_reproposed() {
    let h = Hub::with(clean(), None);
    let a = h.put(ADMIN_TOKEN, "fleet", "a", None, OLD).await;
    let opened = submit(&h, ADMIN_TOKEN, vec![evidence(rev(&a))]).await;
    let id = &opened.body["proposal_id"];
    let mut body = stored_body(&h, id);
    body["cards"][0]
        .as_object_mut()
        .unwrap()
        .remove("before_scan");
    save_body(&h, id, &body);
    let before = card(&h, "fleet", "a").await;
    let snapshot_before = snapshot(&h);
    let r = preflight(&h, ADMIN_TOKEN, id).await;
    assert_eq!(check(&r, "card_trust"), "memory_input_scan_unbound");
    assert_eq!(snapshot(&h), snapshot_before);
    for _ in 0..2 {
        let held = decide(&h, ADMIN_TOKEN, id).await;
        assert_eq!(held.body["state"], "held");
        assert_eq!(held.body["reason"], "memory_input_scan_unbound");
        assert_eq!(card(&h, "fleet", "a").await, before);
        assert!(stored_body(&h, id)["cards"][0].get("before_scan").is_none());
    }
    let rejected = h.call(ADMIN_TOKEN, json!({"action":"decide","proposal_id":id,"decision":"reject","base_epoch":h.epoch().await,"client_event_id":h.id()})).await;
    assert_eq!(rejected.body["state"], "rejected");
    assert_eq!(card(&h, "fleet", "a").await, before);
    let fresh = submit(&h, ADMIN_TOKEN, vec![evidence(rev(&a))]).await;
    assert_eq!(
        stored_body(&h, &fresh.body["proposal_id"])["cards"][0]["before_scan"],
        "clean"
    );
    assert_eq!(
        decide(&h, ADMIN_TOKEN, &fresh.body["proposal_id"])
            .await
            .body["state"],
        "applied"
    );
}
#[tokio::test]
async fn preflight_revalidates_stored_op_target_schema() {
    let h = Hub::with(clean(), None);
    let a = h.put(ADMIN_TOKEN, "fleet", "a", None, OLD).await;
    let opened = submit(&h, ADMIN_TOKEN, vec![evidence(rev(&a))]).await;
    let id = &opened.body["proposal_id"];
    assert_eq!(
        check(&preflight(&h, ADMIN_TOKEN, id).await, "host"),
        "host_satisfied"
    );
    let valid = stored_body(&h, id);
    for invalid in [json!({"type":"edit_skill","path":"AGENTS.md"}), {
        let mut o = valid["ops"][0].clone();
        o["path"] = json!("policy.json");
        o
    }] {
        let mut body = valid.clone();
        body["ops"][0] = invalid;
        save_body(&h, id, &body);
        let before = snapshot(&h);
        assert_eq!(
            check(&preflight(&h, ADMIN_TOKEN, id).await, "host"),
            "card_target_invalid"
        );
        assert_eq!(snapshot(&h), before);
    }
}
#[tokio::test]
async fn opened_tier_survives_apply_time_reclassification() {
    let h = Hub::with(clean(), None);
    let a = h.put(ADMIN_TOKEN, "fleet", "a", None, OLD).await;
    let opened = submit(&h, ADMIN_TOKEN, vec![evidence(rev(&a))]).await;
    let id = &opened.body["proposal_id"];
    assert_eq!(stored_body(&h, id)["opened_tier"], 0);
    h.put(
        ADMIN_TOKEN,
        "fleet",
        "a",
        Some(rev(&a)),
        "New fact. ^c-abcdef",
    )
    .await;
    let r = decide(&h, ADMIN_TOKEN, id).await;
    assert_eq!(r.body["state"], "stale");
    let body = stored_body(&h, id);
    assert_eq!(body["tier"], 2);
    assert_eq!(body["opened_tier"], 0);
}
