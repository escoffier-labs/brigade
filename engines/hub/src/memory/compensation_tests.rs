use super::{proposal_tests::*, tests::*, *};

async fn applied() -> (Hub, Json, String) {
    let (h, id) = attestation_tests::ready().await;
    let before = card(&h, "fleet", "a").await["text"]
        .as_str()
        .unwrap()
        .to_string();
    retrieval_eval_tests::record_current(&h).await;
    let r = h
        .call(ADMIN_TOKEN, attestation_tests::auto_request(&h, &id).await)
        .await;
    assert_eq!(r.body["state"], "applied", "{r:?}");
    h.set_clock(h.now() + 1);
    (h, id, before)
}
async fn compensate(h: &Hub, id: &Json, token: &str) -> Reply {
    h.call(token,json!({"action":"rollback","proposal_id":id,"base_epoch":h.epoch().await,"client_event_id":h.id()})).await
}
#[tokio::test]
async fn compensation_exact_reverse_auto_applies() {
    let (h, id, before) = applied().await;
    let restored = compensate(&h, &id, ADMIN_TOKEN).await;
    assert_eq!(restored.body["state"], "applied", "{restored:?}");
    assert_eq!(restored.body["tier"], 0);
    assert_eq!(card(&h, "fleet", "a").await["text"], before);
}
#[tokio::test]
async fn compensation_other_actor_and_human_edit_stay_review_only() {
    for edit in [false, true] {
        let (h, id, _) = applied().await;
        grant(&h, "fleet", &["read", "reflect", "review"]).await;
        if edit {
            let current = card(&h, "fleet", "a").await;
            h.put(
                ADMIN_TOKEN,
                "fleet",
                "a",
                current["revision"].as_i64(),
                "Human correction. ^c-abcdef",
            )
            .await;
        }
        let before = card(&h, "fleet", "a").await;
        let result = compensate(&h, &id, if edit { ADMIN_TOKEN } else { AGENT_TOKEN }).await;
        assert_ne!(result.body["state"], "applied");
        assert_eq!(result.body["tier"], 2);
        assert_eq!(card(&h, "fleet", "a").await, before);
    }
}

#[tokio::test]
async fn compensation_watch_negative_outcome_trips_and_records_failure_after_edit() {
    for edit in [false, true] {
        let (h, id, _) = applied().await;
        grant(&h, "fleet", &["read", "reflect", "review"]).await;
        if edit {
            let c = card(&h, "fleet", "a").await;
            h.put(
                ADMIN_TOKEN,
                "fleet",
                "a",
                c["revision"].as_i64(),
                "Human correction. ^c-abcdef",
            )
            .await;
        }
        let before = card(&h, "fleet", "a").await;
        let result = h.call(ADMIN_TOKEN,json!({"action":"compensation_watch","scope":"fleet","base_epoch":h.epoch().await,"client_event_id":h.id(),"negative_outcomes":[{"card_id":"a","observed_ms":h.now(),"evidence_id":"verify/failed"}]})).await;
        assert_eq!(result.status, 200, "{result:?}");
        assert_eq!(result.body["results"][0]["rollback_of"], id);
        assert_eq!(result.body["results"][0]["trigger"], "negative-outcome");
        if edit {
            assert_eq!(result.body["results"][0]["result"], "failed");
            assert_eq!(card(&h, "fleet", "a").await, before);
        } else {
            assert_eq!(result.body["results"][0]["result"], "applied");
        }
    }
}

async fn watch(h: &Hub) -> Reply {
    h.call(ADMIN_TOKEN,json!({"action":"compensation_watch","scope":"fleet","base_epoch":h.epoch().await,"client_event_id":h.id()})).await
}

#[tokio::test]
async fn compensation_watch_each_retrieval_metric_trips_but_old_or_other_workloads_do_not() {
    for metric in ["hit_rate", "mrr", "precision_at_k", "recall_at_k"] {
        let (h, _id, _) = applied().await;
        assert_eq!(watch(&h).await.body["results"], json!([]));
        let mut report = retrieval_eval_tests::gate_report(&h).await;
        report["adapters"]["hub-lexical"]["overall"][metric] = json!(0.25);
        report["gate"]["passed"] = json!(false);
        report["gate"]["failures"] = json!(["regression"]);
        assert_eq!(retrieval_eval_tests::record(&h, &report).await.status, 201);
        let result = watch(&h).await;
        assert_eq!(
            result.body["results"][0]["trigger"], "retrieval-regression",
            "{result:?}"
        );
        assert_eq!(result.body["results"][0]["result"], "applied");
        assert_eq!(watch(&h).await.body["results"], json!([]));
    }
}

#[tokio::test]
async fn compensation_watch_contradicted_written_block_trips_and_other_block_does_not() {
    use base64::{Engine, engine::general_purpose::STANDARD};
    let (h, _, _) = applied().await;
    // A verifier cannot be in the reflector allowlist.
    attestation_tests::change_policy(&h, "reflect_principals", json!(["hub:admin", "node-1"]));
    let r=h.call(ADMIN_TOKEN,json!({"action":"verifier","principal":"agent:dot","verifier":true,"client_event_id":h.id()})).await;
    assert_eq!(r.status, 200, "{r:?}");
    let current = card(&h, "fleet", "a").await;
    for (block, trips) in [("c-123456", false), ("c-abcdef", true)] {
        let bytes = json!({"schema":"brigade.memory-verify-manifest.v1","card":{"scope":"fleet","card_id":"a","base_revision":current["revision"]},"blocks":[{"block_id":block,"verdict":"contradicted","confidence":"high"}]}).to_string();
        let r=h.call(AGENT_TOKEN,json!({"action":"evidence.attach","scope":"fleet","card_id":"a","workspace_id":"workspace-a","evidence_id":format!("agent:dot/workspace-a/{block}"),"artifact_kind":"memory-verify-manifest","artifact_sha256":ids::sha256_hex(bytes.as_bytes()),"artifact_base64":STANDARD.encode(bytes),"observed_at":h.now(),"stance":"contrary","client_event_id":h.id()})).await;
        assert_eq!(r.status, 201, "{r:?}");
        let result = watch(&h).await;
        if trips {
            assert_eq!(
                result.body["results"][0]["trigger"], "contradicted",
                "{result:?}"
            );
            assert_eq!(result.body["results"][0]["result"], "applied");
        } else {
            assert_eq!(result.body["results"], json!([]));
        }
    }
}

#[tokio::test]
async fn compensation_tampered_inverse_never_applies_and_preserves_all_card_bytes() {
    let (h, id, _) = applied().await;
    // A different actor may file a reviewed compensation, but cannot grant automatic authority.
    grant(&h, "fleet", &["read", "reflect", "review"]).await;
    let inverse = compensate(&h, &id, AGENT_TOKEN).await;
    assert_eq!(inverse.body["tier"], 2);
    let before = attestation_tests::bytes_snapshot(&h);
    attestation_tests::mutate_body(&h, &inverse.body["proposal_id"], |b| {
        b["cards"][0]["after_text"] = json!("A different edit.");
    });
    h.set_clock(h.now() + 1001);
    let refused = h
        .call(
            ADMIN_TOKEN,
            attestation_tests::auto_request(&h, &inverse.body["proposal_id"]).await,
        )
        .await;
    assert_eq!(refused.body["reason"], "compensation_not_exact");
    assert_eq!(attestation_tests::bytes_snapshot(&h), before);
    let db = rusqlite::Connection::open(h.memory()).unwrap();
    let failed:i64=db.query_row("SELECT count(*) FROM memory_events WHERE json_extract(payload,'$.proposal_id')=?1 AND json_extract(payload,'$.compensation_result')='failed'",[inverse.body["proposal_id"].as_str().unwrap()],|r|r.get(0)).unwrap();
    assert_eq!(failed, 1);
}

#[tokio::test]
async fn compensation_watch_window_and_class_hold_require_later_go() {
    use rusqlite::Connection;
    let (h, id, _) = applied().await;
    attestation_tests::change_policy(&h, "rollback_watch_seconds", json!(1));
    h.set_clock(h.now() + 2000);
    let r=h.call(ADMIN_TOKEN,json!({"action":"compensation_watch","scope":"fleet","base_epoch":h.epoch().await,"client_event_id":h.id(),"negative_outcomes":[{"card_id":"a","observed_ms":h.now(),"evidence_id":"verify/failed"}]})).await;
    assert_eq!(r.body["results"], json!([]));
    attestation_tests::change_policy(&h, "rollback_watch_seconds", json!(86400));
    let r=h.call(ADMIN_TOKEN,json!({"action":"compensation_watch","scope":"fleet","base_epoch":h.epoch().await,"client_event_id":h.id(),"negative_outcomes":[{"card_id":"a","observed_ms":h.now(),"evidence_id":"verify/failed"}]})).await;
    assert_eq!(r.body["results"][0]["rollback_of"], id);
    let db = Connection::open(h.memory()).unwrap();
    assert_eq!(
        db.query_row(
            "SELECT count(*) FROM memory_meta WHERE key LIKE 'compensation_hold:%'",
            [],
            |r| r.get::<_, i64>(0)
        )
        .unwrap(),
        1
    );
    let current = card(&h, "fleet", "a").await;
    let mut operation = op("fleet", "a", current["revision"].as_i64().unwrap());
    operation["type"] = json!("append_evidence");
    operation["expected_block_hash"] =
        proposal_blocks::pins(current["text"].as_str().unwrap())[0]["hash"].clone();
    operation.as_object_mut().unwrap().remove("text");
    operation["evidence_ids"] = json!(["fixture/unknown/evidence"]);
    let proposal = submit(&h, ADMIN_TOKEN, vec![operation]).await;
    let report = attestation_tests::report(&h, &proposal.body["proposal_id"]).await;
    assert!(
        report.body["checks"]
            .as_array()
            .unwrap()
            .iter()
            .any(|c| c["code"] == "compensation_class_held")
    );
    let r = h
        .call(
            ADMIN_TOKEN,
            json!({"action":"auto_apply_switch","enabled":true,"client_event_id":h.id()}),
        )
        .await;
    assert_eq!(r.status, 200);
    assert_eq!(
        db.query_row(
            "SELECT count(*) FROM memory_meta WHERE key LIKE 'compensation_hold:%'",
            [],
            |r| r.get::<_, i64>(0)
        )
        .unwrap(),
        0
    );
}

#[tokio::test]
async fn compensation_failed_scan_keeps_all_cards_identical_and_records_failed() {
    use rusqlite::Connection;
    let (mut h, id, _) = applied().await;
    let before = attestation_tests::bytes_snapshot(&h);
    h.store.take();
    h.open(Arc::new(Fixed(Scan::Failed)), None);
    let result = compensate(&h, &id, ADMIN_TOKEN).await;
    assert_ne!(result.body["state"], "applied", "{result:?}");
    assert_eq!(attestation_tests::bytes_snapshot(&h), before);
    let db = Connection::open(h.memory()).unwrap();
    let count:i64=db.query_row("SELECT count(*) FROM memory_events WHERE json_extract(payload,'$.compensation_result')='failed'",[],|r|r.get(0)).unwrap();
    assert!(count > 0);
}
