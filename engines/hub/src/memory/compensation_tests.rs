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

async fn verifier_signal(
    h: &Hub,
    token: &str,
    id: &str,
    state: &str,
    negative: bool,
    drift: &str,
) -> String {
    use base64::{Engine, engine::general_purpose::STANDARD};
    let principal = if token == AGENT_TOKEN {
        "agent:dot"
    } else {
        "node-1"
    };
    attestation_tests::change_policy(h, "reflect_principals", json!(["hub:admin"]));
    assert_eq!(h.call(ADMIN_TOKEN,json!({"action":"verifier","principal":principal,"verifier":true,"client_event_id":h.id()})).await.status,200);
    let c = card(h, "fleet", "a").await;
    let credential = attestation::credential_id(
        if token == AGENT_TOKEN {
            "principal"
        } else {
            "node"
        },
        principal,
        &digest(token),
    );
    let mut manifest = json!({"schema":"brigade.memory-verify-manifest.v1","card":{"scope":"fleet","card_id":"a","base_revision":c["revision"],"content_hash":c["content_hash"]},"blocks":[],"effectiveness":{"status":"failed","verifier_credential":credential,"observed_ms":h.now()}});
    if !negative {
        manifest["blocks"] = json!([{"block_id":"c-abcdef","verdict":"contradicted"}]);
    }
    match drift {
        "revision" => manifest["card"]["base_revision"] = json!(1),
        "hash" => manifest["card"]["content_hash"] = json!("wrong"),
        "credential" => manifest["effectiveness"]["verifier_credential"] = json!("wrong"),
        "time" => manifest["effectiveness"]["observed_ms"] = json!(0),
        "block" => manifest["blocks"][0]["block_id"] = json!("c-123456"),
        _ => (),
    }
    let bytes = manifest.to_string();
    let id = format!("{principal}/workspace-a/signal-{id}");
    let r=h.call(token,json!({"action":"evidence.attach","scope":"fleet","card_id":"a","workspace_id":"workspace-a","evidence_id":id,"artifact_kind":"memory-verify-manifest","artifact_sha256":ids::sha256_hex(bytes.as_bytes()),"artifact_base64":STANDARD.encode(bytes),"observed_at":h.now(),"stance":"contrary","client_event_id":h.id()})).await;
    assert_eq!(r.status, 201, "{r:?}");
    if state != "pending_review" {
        // Seed a completed review transition without changing the watched head.
        rusqlite::Connection::open(h.memory()).unwrap().execute("UPDATE memory_evidence_bindings SET state=?1,proposal_id='review-fixture' WHERE evidence_id=?2",[state,&id]).unwrap();
    }
    id
}

async fn watch_negative(h: &Hub, ids: Vec<String>) -> Reply {
    h.call(ADMIN_TOKEN,json!({"action":"compensation_watch","scope":"fleet","base_epoch":h.epoch().await,"client_event_id":h.id(),"negative_outcomes":ids})).await
}

#[tokio::test]
async fn compensation_watch_negative_outcome_requires_attested_exact_live_binding() {
    for drift in [
        "revision",
        "hash",
        "credential",
        "time",
        "revoked",
        "pending_review",
        "inert",
    ] {
        let (h, _, _) = applied().await;
        let state = if matches!(drift, "pending_review" | "inert") {
            drift
        } else {
            "active"
        };
        let id = verifier_signal(&h, AGENT_TOKEN, "failed", state, true, drift).await;
        if drift == "revoked" {
            rusqlite::Connection::open(&h.hub)
                .unwrap()
                .execute(
                    "UPDATE fleet_message_principals SET revoked_at='revoked'",
                    [],
                )
                .unwrap();
        }
        let before = attestation_tests::bytes_snapshot(&h);
        assert_eq!(
            watch_negative(&h, vec![id]).await.body["results"],
            json!([]),
            "{drift}"
        );
        assert_eq!(attestation_tests::bytes_snapshot(&h), before);
    }
}

#[tokio::test]
async fn compensation_watch_two_independent_failures_apply_but_stale_heads_do_not_hold() {
    for edit in [false, true] {
        let (h, id, _) = applied().await;
        let one = verifier_signal(&h, AGENT_TOKEN, "one", "active", true, "").await;
        let two = verifier_signal(&h, NODE_TOKEN, "two", "active", true, "").await;
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
        let before = attestation_tests::bytes_snapshot(&h);
        let result = watch_negative(&h, vec![one, two]).await;
        assert_eq!(result.body["results"][0]["rollback_of"], id, "{result:?}");
        assert_eq!(
            result.body["results"][0]["result"],
            if edit { "failed" } else { "applied" }
        );
        let holds: i64 = rusqlite::Connection::open(h.memory())
            .unwrap()
            .query_row(
                "SELECT count(*) FROM memory_meta WHERE key LIKE 'compensation_hold:%'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(holds, if edit { 0 } else { 1 });
        if edit {
            assert_eq!(attestation_tests::bytes_snapshot(&h), before);
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
async fn compensation_watch_contradictions_require_active_independent_verifiers() {
    for (state, count, result) in [
        ("pending_review", 2, "none"),
        ("inert", 2, "none"),
        ("active", 1, "review_required"),
        ("active", 2, "applied"),
    ] {
        let (h, _, _) = applied().await;
        verifier_signal(&h, AGENT_TOKEN, "one", state, false, "").await;
        // Two records by one principal still require review.
        verifier_signal(&h, AGENT_TOKEN, "same-principal", state, false, "").await;
        if count == 2 {
            verifier_signal(&h, NODE_TOKEN, "two", state, false, "").await;
        }
        let before = attestation_tests::bytes_snapshot(&h);
        let r = watch(&h).await;
        if result == "none" {
            assert_eq!(r.body["results"], json!([]));
        } else {
            assert_eq!(r.body["results"][0]["result"], result, "{r:?}");
        }
        if result != "applied" {
            assert_eq!(attestation_tests::bytes_snapshot(&h), before);
        }
        if result == "review_required" {
            let proposal = &r.body["results"][0]["proposal_id"];
            let db = rusqlite::Connection::open(h.memory()).unwrap();
            assert_eq!(
                proposals::load(&crate::writer::worker::Tx(&db), proposal.as_str().unwrap())
                    .unwrap()
                    .unwrap()["tier"],
                2
            );
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
    let r=h.call(ADMIN_TOKEN,json!({"action":"compensation_watch","scope":"fleet","base_epoch":h.epoch().await,"client_event_id":h.id(),"negative_outcomes":["unknown/workspace/failed"]})).await;
    assert_eq!(r.body["results"], json!([]));
    attestation_tests::change_policy(&h, "rollback_watch_seconds", json!(86400));
    let r = compensate(&h, &id, ADMIN_TOKEN).await;
    assert_eq!(r.body["state"], "applied", "{r:?}");
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
            json!({"action":"auto_apply_switch","enabled":true,"scope":"fleet","op_type":"append_evidence","client_event_id":h.id()}),
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

#[tokio::test]
async fn compensation_watch_unresolved_evidence_is_ignored() {
    let (h, _, _) = applied().await;
    let before = attestation_tests::bytes_snapshot(&h);
    let r = h.call(ADMIN_TOKEN,json!({"action":"compensation_watch","scope":"fleet","base_epoch":h.epoch().await,"client_event_id":h.id(),"negative_outcomes":["unknown/workspace/failed"]})).await;
    assert_eq!(r.status, 200, "{r:?}");
    assert_eq!(r.body["results"], json!([]));
    assert_eq!(attestation_tests::bytes_snapshot(&h), before);
}

#[tokio::test]
async fn compensation_respects_policy_and_kill_switch() {
    for policy in [false, true] {
        let (h, id, _) = applied().await;
        if policy {
            attestation_tests::change_policy(&h, "auto_apply", json!(false));
        } else {
            assert_eq!(
                h.call(
                    ADMIN_TOKEN,
                    json!({"action":"auto_apply_switch","enabled":false,"client_event_id":h.id()})
                )
                .await
                .status,
                200
            );
        }
        let before = attestation_tests::bytes_snapshot(&h);
        assert_ne!(
            compensate(&h, &id, ADMIN_TOKEN).await.body["state"],
            "applied"
        );
        assert_eq!(attestation_tests::bytes_snapshot(&h), before);
    }
}

#[tokio::test]
async fn compensation_retrieval_regression_after_first_page_is_seen() {
    let (h, _, _) = applied().await;
    for n in 0..129 {
        h.set_clock(h.now() + 1);
        let mut report = retrieval_eval_tests::gate_report(&h).await;
        if n == 128 {
            report["adapters"]["hub-lexical"]["overall"]["mrr"] = json!(0.25);
        }
        assert_eq!(retrieval_eval_tests::record(&h, &report).await.status, 201);
    }
    assert_eq!(watch(&h).await.body["results"], json!([]));
    let r = watch(&h).await;
    assert_eq!(r.body["results"][0]["result"], "applied", "{r:?}");
}

#[tokio::test]
async fn compensation_retrieval_noise_is_ignored_and_changed_corpus_requires_review() {
    for unrelated in [false, true] {
        let (h, _, _) = applied().await;
        if unrelated {
            h.put(
                ADMIN_TOKEN,
                "fleet",
                "decoy",
                None,
                "Unrelated corpus change.",
            )
            .await;
        }
        h.set_clock(h.now() + 1);
        let mut report = retrieval_eval_tests::gate_report(&h).await;
        report["adapters"]["hub-lexical"]["overall"]["mrr"] =
            json!(if unrelated { 0.25 } else { 0.495 });
        assert_eq!(retrieval_eval_tests::record(&h, &report).await.status, 201);
        let before = attestation_tests::bytes_snapshot(&h);
        let r = watch(&h).await;
        if unrelated {
            assert_eq!(r.body["results"][0]["result"], "review_required", "{r:?}");
        } else {
            assert_eq!(r.body["results"], json!([]));
        }
        assert_eq!(attestation_tests::bytes_snapshot(&h), before);
    }
}

#[tokio::test]
async fn compensation_holds_are_scoped_and_go_releases_only_one_pair() {
    let (h, id, _) = applied().await;
    assert_eq!(
        compensate(&h, &id, ADMIN_TOKEN).await.body["state"],
        "applied"
    );
    let db = rusqlite::Connection::open(h.memory()).unwrap();
    db.execute(
        "INSERT INTO memory_meta VALUES('compensation_hold:other/append_evidence','fixture')",
        [],
    )
    .unwrap();
    db.execute(
        "INSERT INTO memory_meta VALUES('compensation_hold:fleet/replace_block','fixture')",
        [],
    )
    .unwrap();
    let tx = crate::writer::worker::Tx(&db);
    assert!(
        !compensation::class_held(
            &tx,
            &json!({"ops":[{"scope":"unheld","type":"append_evidence"}]})
        )
        .unwrap()
    );
    h.call(
        ADMIN_TOKEN,
        json!({"action":"auto_apply_switch","enabled":true,"client_event_id":h.id()}),
    )
    .await;
    assert_eq!(
        db.query_row(
            "SELECT count(*) FROM memory_meta WHERE key LIKE 'compensation_hold:%'",
            [],
            |r| r.get::<_, i64>(0)
        )
        .unwrap(),
        3
    );
    h.call(ADMIN_TOKEN,json!({"action":"auto_apply_switch","enabled":true,"scope":"fleet","op_type":"append_evidence","client_event_id":h.id()})).await;
    assert_eq!(
        db.query_row(
            "SELECT count(*) FROM memory_meta WHERE key LIKE 'compensation_hold:%'",
            [],
            |r| r.get::<_, i64>(0)
        )
        .unwrap(),
        2
    );
    assert!(
        !compensation::class_held(
            &tx,
            &json!({"ops":[{"scope":"fleet","type":"append_evidence"}]})
        )
        .unwrap()
    );
    assert!(
        compensation::class_held(
            &tx,
            &json!({"ops":[{"scope":"other","type":"append_evidence"}]})
        )
        .unwrap()
    );
}

#[tokio::test]
async fn compensation_manual_rollback_outside_window_never_automatically_applies() {
    let (h, id, _) = applied().await;
    h.set_clock(h.now() + 86_400_001);
    let before = attestation_tests::bytes_snapshot(&h);
    let r = compensate(&h, &id, ADMIN_TOKEN).await;
    assert_ne!(r.body["state"], "applied");
    let r = h
        .call(
            ADMIN_TOKEN,
            attestation_tests::auto_request(&h, &r.body["proposal_id"]).await,
        )
        .await;
    assert_ne!(r.body["state"], "applied");
    assert_eq!(attestation_tests::bytes_snapshot(&h), before);
}

#[tokio::test]
async fn compensation_watch_contradictions_on_other_blocks_are_ignored() {
    let (h, _, _) = applied().await;
    verifier_signal(&h, AGENT_TOKEN, "other-block", "active", false, "block").await;
    verifier_signal(&h, NODE_TOKEN, "other-block", "active", false, "block").await;
    let before = attestation_tests::bytes_snapshot(&h);
    assert_eq!(watch(&h).await.body["results"], json!([]));
    assert_eq!(attestation_tests::bytes_snapshot(&h), before);
}
