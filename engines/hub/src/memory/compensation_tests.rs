use super::retrieval_eval::FIXTURE_SCOPE;
use super::{proposal_tests::*, tests::*, *};

pub(super) async fn applied() -> (Hub, Json, String) {
    let (h, id) = attestation_tests::ready().await;
    let before = card(&h, FIXTURE_SCOPE, "a").await["text"]
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
    assert_eq!(card(&h, FIXTURE_SCOPE, "a").await["text"], before);
}
#[tokio::test]
async fn compensation_other_actor_and_human_edit_stay_review_only() {
    for edit in [false, true] {
        let (h, id, _) = applied().await;
        grant(&h, FIXTURE_SCOPE, &["read", "reflect", "review"]).await;
        if edit {
            let current = card(&h, FIXTURE_SCOPE, "a").await;
            h.put(
                ADMIN_TOKEN,
                FIXTURE_SCOPE,
                "a",
                current["revision"].as_i64(),
                "Human correction. ^c-abcdef",
            )
            .await;
        }
        let before = card(&h, FIXTURE_SCOPE, "a").await;
        let result = compensate(&h, &id, if edit { ADMIN_TOKEN } else { AGENT_TOKEN }).await;
        assert_ne!(result.body["state"], "applied");
        assert_eq!(result.body["tier"], 2);
        assert_eq!(card(&h, FIXTURE_SCOPE, "a").await, before);
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
    verifier_signal_for_card(h, "a", token, id, state, negative, drift).await
}
async fn verifier_signal_for_card(
    h: &Hub,
    card_id: &str,
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
    let c = card(h, FIXTURE_SCOPE, card_id).await;
    let credential = attestation::credential_id(
        if token == AGENT_TOKEN {
            "principal"
        } else {
            "node"
        },
        principal,
        &digest(token),
    );
    let mut manifest = json!({"schema":"brigade.memory-verify-manifest.v1","card":{"scope":FIXTURE_SCOPE,"card_id":card_id,"base_revision":c["revision"],"content_hash":c["content_hash"]},"blocks":[],"effectiveness":{"status":"failed","verifier_credential":credential,"observed_ms":h.now()}});
    if !negative {
        manifest.as_object_mut().unwrap().remove("effectiveness");
        // Match the normal CLI manifest, which binds by immutable revision.
        manifest["card"]
            .as_object_mut()
            .unwrap()
            .remove("content_hash");
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
    let r=h.call(token,json!({"action":"evidence.attach","scope":FIXTURE_SCOPE,"card_id":card_id,"workspace_id":"workspace-a","evidence_id":id,"artifact_kind":"memory-verify-manifest","artifact_sha256":ids::sha256_hex(bytes.as_bytes()),"artifact_base64":STANDARD.encode(bytes),"observed_at":h.now(),"stance":"contrary","client_event_id":h.id()})).await;
    assert_eq!(r.status, 201, "{r:?}");
    if state != "pending_review" && drift != "revision" {
        let mut operation = op(FIXTURE_SCOPE, card_id, c["revision"].as_i64().unwrap());
        operation["type"] = json!("append_evidence");
        operation.as_object_mut().unwrap().remove("text");
        operation["expected_block_hash"] =
            proposal_blocks::pins(c["text"].as_str().unwrap())[0]["hash"].clone();
        operation["evidence_ids"] = json!([id]);
        let opened = h.call(token, json!({"action":"propose","workspace_id":"workspace-a","base_epoch":h.epoch().await,"ops":[operation],"client_event_id":h.id()})).await;
        assert_eq!(opened.status, 201, "{opened:?}");
        let reviewed = h.call(ADMIN_TOKEN, json!({"action":"decide","proposal_id":opened.body["proposal_id"],"decision":if state == "active" {"accept"} else {"reject"},"base_epoch":h.epoch().await,"client_event_id":h.id()})).await;
        assert_eq!(
            reviewed.body["state"],
            if state == "active" {
                "applied"
            } else {
                "rejected"
            },
            "{reviewed:?}"
        );
    }
    id
}

struct RefuseInverse(String);
impl Scanner for RefuseInverse {
    fn scan(&self, text: &str) -> Scan {
        if text == self.0 {
            Scan::Failed
        } else {
            Scan::Clean {
                ruleset: "test-v1".into(),
            }
        }
    }
    fn describe(&self) -> (bool, Option<String>) {
        (true, Some("test-v1".into()))
    }
}

async fn watch_negative(h: &Hub, ids: Vec<String>) -> Reply {
    h.call(ADMIN_TOKEN,json!({"action":"compensation_watch","scope":FIXTURE_SCOPE,"base_epoch":h.epoch().await,"client_event_id":h.id(),"negative_outcomes":ids})).await
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
            let c = card(&h, FIXTURE_SCOPE, "a").await;
            h.put(
                ADMIN_TOKEN,
                FIXTURE_SCOPE,
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
    h.call(ADMIN_TOKEN,json!({"action":"compensation_watch","scope":FIXTURE_SCOPE,"base_epoch":h.epoch().await,"client_event_id":h.id()})).await
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
            drop(db);
            assert_eq!(watch(&h).await.body["results"], json!([]));
            h.set_clock(h.now() + 1001);
            let refused = h
                .call(
                    ADMIN_TOKEN,
                    attestation_tests::auto_request(&h, proposal).await,
                )
                .await;
            assert_eq!(
                refused.body["reason"], "compensation_not_exact",
                "{refused:?}"
            );
            assert_eq!(attestation_tests::bytes_snapshot(&h), before);
        }
    }
}

#[tokio::test]
async fn compensation_tampered_inverse_never_applies_and_preserves_all_card_bytes() {
    let (h, id, _) = applied().await;
    // A different actor may file a reviewed compensation, but cannot grant automatic authority.
    grant(&h, FIXTURE_SCOPE, &["read", "reflect", "review"]).await;
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
    let r=h.call(ADMIN_TOKEN,json!({"action":"compensation_watch","scope":FIXTURE_SCOPE,"base_epoch":h.epoch().await,"client_event_id":h.id(),"negative_outcomes":["unknown/workspace/failed"]})).await;
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
    let current = card(&h, FIXTURE_SCOPE, "a").await;
    let mut operation = op(FIXTURE_SCOPE, "a", current["revision"].as_i64().unwrap());
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
            json!({"action":"auto_apply_switch","enabled":true,"scope":FIXTURE_SCOPE,"op_type":"append_evidence","client_event_id":h.id()}),
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
    assert_eq!(result.body["compensation_result"], "failed");
    assert_eq!(attestation_tests::bytes_snapshot(&h), before);
    let db = Connection::open(h.memory()).unwrap();
    let count:i64=db.query_row("SELECT count(*) FROM memory_events WHERE json_extract(payload,'$.compensation_result')='failed'",[],|r|r.get(0)).unwrap();
    assert!(count > 0);
}

#[tokio::test]
async fn compensation_watch_unresolved_evidence_is_ignored() {
    let (h, _, _) = applied().await;
    let before = attestation_tests::bytes_snapshot(&h);
    let r = h.call(ADMIN_TOKEN,json!({"action":"compensation_watch","scope":FIXTURE_SCOPE,"base_epoch":h.epoch().await,"client_event_id":h.id(),"negative_outcomes":["unknown/workspace/failed"]})).await;
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
                FIXTURE_SCOPE,
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
        &"INSERT INTO memory_meta VALUES('compensation_hold:fleet/replace_block','fixture')"
            .replace("fleet", FIXTURE_SCOPE),
        [],
    )
    .unwrap();
    let tx = crate::writer::worker::Tx(&db);
    assert!(
        compensation::class_held(
            &tx,
            &json!({"ops":[{"scope":"unheld","type":"append_evidence"}]})
        )
        .unwrap()
        .is_none()
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
    h.call(ADMIN_TOKEN,json!({"action":"auto_apply_switch","enabled":true,"scope":FIXTURE_SCOPE,"op_type":"append_evidence","client_event_id":h.id()})).await;
    assert_eq!(
        db.query_row(
            "SELECT count(*) FROM memory_meta WHERE key LIKE 'compensation_hold:%'",
            [],
            |r| r.get::<_, i64>(0)
        )
        .unwrap(),
        2
    );
    let matched = |body: Json| compensation::class_held(&tx, &body).unwrap();
    assert!(matched(json!({"ops":[{"scope":FIXTURE_SCOPE,"type":"append_evidence"}]})).is_none());
    // The refusal names the pair it matched.
    assert_eq!(
        matched(json!({"ops":[{"scope":"other","type":"append_evidence"}]})).as_deref(),
        Some("other/append_evidence")
    );
}

#[tokio::test]
async fn compensation_holds_every_class_of_a_non_verifier_proposal() {
    let (h, id, _) = applied().await;
    assert_eq!(
        compensate(&h, &id, ADMIN_TOKEN).await.body["state"],
        "applied"
    );
    let held = || -> Vec<String> {
        let db = rusqlite::Connection::open(h.memory()).unwrap();
        let mut st = db
            .prepare(
                "SELECT key FROM memory_meta WHERE key LIKE 'compensation_hold:%' ORDER BY key",
            )
            .unwrap();
        st.query_map([], |r| r.get::<_, String>(0))
            .unwrap()
            .map(Result::unwrap)
            .collect()
    };
    assert_eq!(
        held(),
        vec![compensation::hold_key(FIXTURE_SCOPE, "append_evidence")]
    );
    // The same hold covers every op class when the proposal has more than one.
    let db = rusqlite::Connection::open(h.memory()).unwrap();
    compensation::hold(
        &crate::writer::worker::Tx(&db),
        &json!({"proposal_id":"fixture","ops":[
            {"scope":FIXTURE_SCOPE,"type":"replace_block"},
            {"scope":FIXTURE_SCOPE,"type":"add_previously"}]}),
    )
    .unwrap();
    assert_eq!(
        held(),
        vec![
            compensation::hold_key(FIXTURE_SCOPE, "add_previously"),
            compensation::hold_key(FIXTURE_SCOPE, "append_evidence"),
            compensation::hold_key(FIXTURE_SCOPE, "replace_block"),
        ]
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

#[tokio::test]
async fn compensation_review_required_inverse_cannot_auto_apply() {
    let (h, _, _) = applied().await;
    h.put(ADMIN_TOKEN, FIXTURE_SCOPE, "decoy", None, "Another card.")
        .await;
    h.set_clock(h.now() + 1);
    let mut report = retrieval_eval_tests::gate_report(&h).await;
    report["adapters"]["hub-lexical"]["overall"]["mrr"] = json!(0.25);
    assert_eq!(retrieval_eval_tests::record(&h, &report).await.status, 201);
    let referral = watch(&h).await;
    assert_eq!(referral.body["results"][0]["result"], "review_required");
    let before = attestation_tests::bytes_snapshot(&h);
    h.set_clock(h.now() + 1001);
    let refused = h
        .call(
            ADMIN_TOKEN,
            attestation_tests::auto_request(&h, &referral.body["results"][0]["proposal_id"]).await,
        )
        .await;
    assert_eq!(
        refused.body["reason"], "compensation_not_exact",
        "{refused:?}"
    );
    assert_eq!(attestation_tests::bytes_snapshot(&h), before);
}

#[tokio::test]
async fn compensation_release_can_keep_kill_switch_off() {
    let (h, id, _) = applied().await;
    assert_eq!(
        compensate(&h, &id, ADMIN_TOKEN).await.body["state"],
        "applied"
    );
    h.call(
        ADMIN_TOKEN,
        json!({"action":"auto_apply_switch","enabled":false,"client_event_id":h.id()}),
    )
    .await;
    let result = h.call(ADMIN_TOKEN, json!({"action":"auto_apply_switch","enabled":false,"scope":FIXTURE_SCOPE,"op_type":"append_evidence","client_event_id":h.id()})).await;
    assert_eq!(result.status, 200, "{result:?}");
    let db = rusqlite::Connection::open(h.memory()).unwrap();
    assert_eq!(
        db.query_row(
            "SELECT value FROM memory_meta WHERE key='auto_apply_enabled'",
            [],
            |r| r.get::<_, String>(0)
        )
        .unwrap(),
        "false"
    );
    assert_eq!(
        db.query_row(
            &"SELECT count(*) FROM memory_meta WHERE key='compensation_hold:fleet/append_evidence'"
                .replace("fleet", FIXTURE_SCOPE),
            [],
            |r| r.get::<_, i64>(0)
        )
        .unwrap(),
        0
    );
}

#[tokio::test]
async fn compensation_failed_watch_can_retry_after_kill_switch_go() {
    let (h, _, _) = applied().await;
    verifier_signal(&h, AGENT_TOKEN, "one", "active", false, "").await;
    verifier_signal(&h, NODE_TOKEN, "two", "active", false, "").await;
    h.call(
        ADMIN_TOKEN,
        json!({"action":"auto_apply_switch","enabled":false,"client_event_id":h.id()}),
    )
    .await;
    assert_eq!(watch(&h).await.body["results"][0]["result"], "failed");
    h.call(
        ADMIN_TOKEN,
        json!({"action":"auto_apply_switch","enabled":true,"client_event_id":h.id()}),
    )
    .await;
    assert_eq!(watch(&h).await.body["results"][0]["result"], "applied");
    assert_eq!(watch(&h).await.body["results"], json!([]));
}

#[tokio::test]
async fn compensation_second_active_verifier_can_upgrade_referral() {
    let (h, id, before) = applied().await;
    verifier_signal(&h, AGENT_TOKEN, "one", "active", false, "").await;
    assert_eq!(
        watch(&h).await.body["results"][0]["result"],
        "review_required"
    );
    assert_eq!(watch(&h).await.body["results"], json!([]));
    verifier_signal(&h, NODE_TOKEN, "two", "active", false, "").await;
    let r = watch(&h).await;
    assert_eq!(r.body["results"][0]["result"], "applied", "{r:?}");
    assert_eq!(r.body["results"][0]["rollback_of"], id);
    assert_eq!(card(&h, FIXTURE_SCOPE, "a").await["text"], before);
    let db = rusqlite::Connection::open(h.memory()).unwrap();
    let trips: i64 = db.query_row("SELECT count(*) FROM memory_events WHERE kind='proposal.decide' AND json_extract(payload,'$.rollback_of')=?1 AND json_extract(payload,'$.watch_trip.trigger')='contradicted'", [id.as_str().unwrap()], |r| r.get(0)).unwrap();
    assert!(trips > 0);
}

#[tokio::test]
async fn compensation_evidence_appends_do_not_hide_human_edit_and_revert() {
    let (h, _, _) = applied().await;
    verifier_signal(&h, AGENT_TOKEN, "one", "active", false, "").await;
    let c = card(&h, FIXTURE_SCOPE, "a").await;
    let edited = h
        .put(
            ADMIN_TOKEN,
            FIXTURE_SCOPE,
            "a",
            c["revision"].as_i64(),
            "A human edit. ^c-abcdef",
        )
        .await;
    h.put(
        ADMIN_TOKEN,
        FIXTURE_SCOPE,
        "a",
        Some(rev(&edited)),
        c["text"].as_str().unwrap(),
    )
    .await;
    verifier_signal(&h, NODE_TOKEN, "two", "active", false, "").await;
    let before = attestation_tests::bytes_snapshot(&h);
    assert_eq!(watch(&h).await.body["results"][0]["result"], "failed");
    assert_eq!(attestation_tests::bytes_snapshot(&h), before);
    let db = rusqlite::Connection::open(h.memory()).unwrap();
    assert_eq!(db.query_row("SELECT count(*) FROM memory_meta WHERE key LIKE 'compensation_watch:%' AND json_extract(value,'$.result')='stale'", [], |r| r.get::<_, i64>(0)).unwrap(), 1);
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
async fn compensation_contradictions_pin_the_attested_version_hash() {
    let (h, _, _) = applied().await;
    verifier_signal(&h, AGENT_TOKEN, "wrong-hash", "active", false, "hash").await;
    verifier_signal(&h, NODE_TOKEN, "wrong-hash", "active", false, "hash").await;
    assert_eq!(watch(&h).await.body["results"], json!([]));
}

#[tokio::test]
async fn compensation_failed_retrieval_trip_retries_and_journal_expires() {
    let (h, _, _) = applied().await;
    let mut report = retrieval_eval_tests::gate_report(&h).await;
    report["adapters"]["hub-lexical"]["overall"]["mrr"] = json!(0.25);
    assert_eq!(retrieval_eval_tests::record(&h, &report).await.status, 201);
    h.call(
        ADMIN_TOKEN,
        json!({"action":"auto_apply_switch","enabled":false,"client_event_id":h.id()}),
    )
    .await;
    assert_eq!(watch(&h).await.body["results"][0]["result"], "failed");
    h.call(
        ADMIN_TOKEN,
        json!({"action":"auto_apply_switch","enabled":true,"client_event_id":h.id()}),
    )
    .await;
    assert_eq!(watch(&h).await.body["results"][0]["result"], "applied");
    let db = rusqlite::Connection::open(h.memory()).unwrap();
    assert_eq!(
        db.query_row(
            "SELECT count(*) FROM memory_meta WHERE key LIKE 'compensation_watch:%'",
            [],
            |r| r.get::<_, i64>(0)
        )
        .unwrap(),
        1
    );
    h.set_clock(h.now() + 604_800_001);
    watch(&h).await;
    assert_eq!(db.query_row("SELECT count(*) FROM memory_meta WHERE key LIKE 'compensation_watch:%' OR key LIKE 'compensation_retrieval:%'", [], |r| r.get::<_, i64>(0)).unwrap(), 0);
}

#[tokio::test]
async fn compensation_watch_discovers_attested_failure_without_client_ids() {
    let (h, _, _) = applied().await;
    verifier_signal(&h, AGENT_TOKEN, "one", "active", true, "").await;
    verifier_signal(&h, NODE_TOKEN, "two", "active", true, "").await;
    assert_eq!(watch(&h).await.body["results"][0]["result"], "applied");
}

#[tokio::test]
async fn compensation_stale_watch_is_final_after_one_attempt() {
    let (h, id, _) = applied().await;
    verifier_signal(&h, AGENT_TOKEN, "one", "active", false, "").await;
    verifier_signal(&h, NODE_TOKEN, "two", "active", false, "").await;
    let c = card(&h, FIXTURE_SCOPE, "a").await;
    h.put(
        ADMIN_TOKEN,
        FIXTURE_SCOPE,
        "a",
        c["revision"].as_i64(),
        "Human correction. ^c-abcdef",
    )
    .await;
    assert_eq!(watch(&h).await.body["results"][0]["result"], "failed");
    assert_eq!(watch(&h).await.body["results"], json!([]));
    let db = rusqlite::Connection::open(h.memory()).unwrap();
    let proposals: i64 = db
        .query_row(
            "SELECT count(*) FROM memory_proposals WHERE json_extract(body,'$.rollback_of')=?1",
            [id.as_str().unwrap()],
            |r| r.get(0),
        )
        .unwrap();
    let events: i64 = db.query_row("SELECT count(*) FROM memory_events WHERE kind='proposal.decide' AND json_extract(payload,'$.rollback_of')=?1", [id.as_str().unwrap()], |r| r.get(0)).unwrap();
    assert_eq!(proposals, 1);
    assert_eq!(events, 1);
}

#[tokio::test]
async fn compensation_stale_watch_skips_the_manifest_rescan_until_a_distinct_trip() {
    use std::sync::atomic::{AtomicUsize, Ordering};
    struct Counting(Arc<AtomicUsize>);
    impl Scanner for Counting {
        fn scan(&self, _: &str) -> Scan {
            self.0.fetch_add(1, Ordering::SeqCst);
            Scan::Clean {
                ruleset: "test-v7".into(),
            }
        }
        fn describe(&self) -> (bool, Option<String>) {
            (true, Some("test-v7".into()))
        }
    }
    let (mut h, id, _) = applied().await;
    verifier_signal(&h, AGENT_TOKEN, "contradiction-one", "active", false, "").await;
    verifier_signal(&h, NODE_TOKEN, "contradiction-two", "active", false, "").await;
    let c = card(&h, FIXTURE_SCOPE, "a").await;
    h.put(
        ADMIN_TOKEN,
        FIXTURE_SCOPE,
        "a",
        c["revision"].as_i64(),
        "Human correction. ^c-abcdef",
    )
    .await;
    let scans = Arc::new(AtomicUsize::new(0));
    h.store.take();
    h.open(Arc::new(Counting(scans.clone())), None);
    let first = watch(&h).await;
    assert_eq!(first.body["results"][0]["result"], "failed");
    assert!(
        scans.load(Ordering::SeqCst) > 0,
        "the first stale evaluation scans its manifests"
    );
    scans.store(0, Ordering::SeqCst);
    assert_eq!(watch(&h).await.body["results"], json!([]));
    assert_eq!(
        scans.load(Ordering::SeqCst),
        0,
        "a known stale watch must not re-scan unchanged manifests"
    );
    let one = verifier_signal(&h, AGENT_TOKEN, "negative-one", "active", true, "").await;
    let two = verifier_signal(&h, NODE_TOKEN, "negative-two", "active", true, "").await;
    scans.store(0, Ordering::SeqCst);
    let later = watch_negative(&h, vec![one, two]).await;
    assert_eq!(later.body["results"][0]["rollback_of"], id, "{later:?}");
    assert_eq!(later.body["results"][0]["trigger"], "negative-outcome");
    assert!(
        scans.load(Ordering::SeqCst) > 0,
        "a distinct trip must re-scan the changed manifests"
    );
}

#[tokio::test]
async fn compensation_watch_scan_refusal_retries_after_scanner_recovers() {
    let (mut h, id, before) = applied().await;
    verifier_signal(&h, AGENT_TOKEN, "one", "active", true, "").await;
    verifier_signal(&h, NODE_TOKEN, "two", "active", true, "").await;
    h.store.take();
    h.open(Arc::new(RefuseInverse(before.clone())), None);
    let refused = watch(&h).await;
    assert_eq!(
        refused.body["results"][0]["result"], "failed",
        "{refused:?}"
    );
    assert_eq!(refused.body["results"][0]["reason"], "scan_not_clean");
    let again = watch(&h).await;
    assert_eq!(again.body["results"][0]["result"], "failed");
    assert_eq!(
        again.body["results"][0]["proposal_id"],
        refused.body["results"][0]["proposal_id"]
    );
    h.store.take();
    h.open(
        Arc::new(Fixed(Scan::Clean {
            ruleset: "test-v1".into(),
        })),
        None,
    );
    let retried = watch(&h).await;
    assert_eq!(
        retried.body["results"][0]["result"], "applied",
        "{retried:?}"
    );
    assert_eq!(
        retried.body["results"][0]["proposal_id"],
        refused.body["results"][0]["proposal_id"]
    );
    assert_eq!(card(&h, FIXTURE_SCOPE, "a").await["text"], before);
    let db = rusqlite::Connection::open(h.memory()).unwrap();
    let inverses: i64 = db
        .query_row(
            "SELECT count(*) FROM memory_proposals WHERE json_extract(body,'$.rollback_of')=?1",
            [id.as_str().unwrap()],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(inverses, 1);
}

#[tokio::test]
async fn compensation_watch_deduplicates_changed_review_trips() {
    let (h, id, _) = applied().await;
    verifier_signal(&h, AGENT_TOKEN, "one", "active", false, "").await;
    assert_eq!(
        watch(&h).await.body["results"][0]["result"],
        "review_required"
    );
    verifier_signal(&h, AGENT_TOKEN, "another", "active", false, "").await;
    assert_eq!(watch(&h).await.body["results"], json!([]));
    let db = rusqlite::Connection::open(h.memory()).unwrap();
    let pending: i64 = db.query_row("SELECT count(*) FROM memory_proposals WHERE state='pending' AND json_extract(body,'$.rollback_of')=?1", [id.as_str().unwrap()], |r| r.get(0)).unwrap();
    assert_eq!(pending, 1);
    drop(db);
    verifier_signal(&h, NODE_TOKEN, "two", "active", false, "").await;
    assert_eq!(watch(&h).await.body["results"][0]["result"], "applied");
}

#[tokio::test]
async fn compensation_release_leaves_enabled_switch_unchanged() {
    let (h, id, _) = applied().await;
    assert_eq!(
        compensate(&h, &id, ADMIN_TOKEN).await.body["state"],
        "applied"
    );
    h.call(
        ADMIN_TOKEN,
        json!({"action":"auto_apply_switch","enabled":true,"client_event_id":h.id()}),
    )
    .await;
    let result = h.call(ADMIN_TOKEN, json!({"action":"auto_apply_switch","enabled":false,"scope":FIXTURE_SCOPE,"op_type":"append_evidence","client_event_id":h.id()})).await;
    assert_eq!(result.body["auto_apply_enabled"], true);
    let db = rusqlite::Connection::open(h.memory()).unwrap();
    let enabled: String = db
        .query_row(
            "SELECT value FROM memory_meta WHERE key='auto_apply_enabled'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(enabled, "true");
}

#[tokio::test]
async fn compensation_stale_trip_does_not_silence_a_distinct_negative_outcome() {
    let (h, id, _) = applied().await;
    verifier_signal(&h, AGENT_TOKEN, "contradiction-one", "active", false, "").await;
    verifier_signal(&h, NODE_TOKEN, "contradiction-two", "active", false, "").await;
    let c = card(&h, FIXTURE_SCOPE, "a").await;
    h.put(
        ADMIN_TOKEN,
        FIXTURE_SCOPE,
        "a",
        c["revision"].as_i64(),
        "Human correction. ^c-abcdef",
    )
    .await;
    let first = watch(&h).await;
    assert_eq!(first.body["results"][0]["trigger"], "contradicted");
    assert_eq!(first.body["results"][0]["result"], "failed");
    // An existing round-4 journal must retain its fingerprint on upgrade.
    let db = rusqlite::Connection::open(h.memory()).unwrap();
    db.execute(
        "UPDATE memory_meta SET value=json_remove(value,'$.completed_trips') WHERE key=?1",
        [format!("compensation_watch:{}", id.as_str().unwrap())],
    )
    .unwrap();
    drop(db);
    assert_eq!(watch(&h).await.body["results"], json!([]));
    let one = verifier_signal(&h, AGENT_TOKEN, "negative-one", "active", true, "").await;
    let two = verifier_signal(&h, NODE_TOKEN, "negative-two", "active", true, "").await;
    let before = attestation_tests::bytes_snapshot(&h);
    let later = watch_negative(&h, vec![one.clone(), two.clone()]).await;
    assert_eq!(later.body["results"][0]["rollback_of"], id, "{later:?}");
    assert_eq!(later.body["results"][0]["trigger"], "negative-outcome");
    assert_eq!(later.body["results"][0]["result"], "failed");
    assert_eq!(later.body["results"][0]["reason"], "memory_base_mismatch");
    assert_ne!(
        later.body["results"][0]["proposal_id"],
        first.body["results"][0]["proposal_id"]
    );
    assert_eq!(
        watch_negative(&h, vec![one, two]).await.body["results"],
        json!([])
    );
    // Revisiting the old fingerprint must not create a third inverse.
    assert_eq!(watch_negative(&h, vec![]).await.body["results"], json!([]));
    let db = rusqlite::Connection::open(h.memory()).unwrap();
    let inverses: i64 = db
        .query_row(
            "SELECT count(*) FROM memory_proposals WHERE json_extract(body,'$.rollback_of')=?1",
            [id.as_str().unwrap()],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(inverses, 2);
    assert_eq!(attestation_tests::bytes_snapshot(&h), before);
}

#[tokio::test]
async fn compensation_retry_rechecks_authority_exactness_and_watch_gates() {
    for drift in [
        "inverse", "head", "kill", "policy", "verifier", "window", "actor",
    ] {
        let (mut h, id, before) = applied().await;
        verifier_signal(&h, AGENT_TOKEN, "one", "active", false, "").await;
        verifier_signal(&h, NODE_TOKEN, "two", "active", false, "").await;
        h.store.take();
        h.open(Arc::new(RefuseInverse(before)), None);
        let refused = watch(&h).await;
        assert_eq!(refused.body["results"][0]["result"], "failed");
        let inverse = refused.body["results"][0]["proposal_id"].clone();
        h.store.take();
        h.open(
            Arc::new(Fixed(Scan::Clean {
                ruleset: "test-v1".into(),
            })),
            None,
        );
        match drift {
            "inverse" => attestation_tests::mutate_body(&h, &inverse, |b| {
                b["cards"][0]["after_text"] = json!("An unrelated edit.");
            }),
            "head" => {
                let c = card(&h, FIXTURE_SCOPE, "a").await;
                h.put(
                    ADMIN_TOKEN,
                    FIXTURE_SCOPE,
                    "a",
                    c["revision"].as_i64(),
                    "Human correction. ^c-abcdef",
                )
                .await;
            }
            "kill" => {
                h.call(
                    ADMIN_TOKEN,
                    json!({"action":"auto_apply_switch","enabled":false,"client_event_id":h.id()}),
                )
                .await;
            }
            "policy" => attestation_tests::change_policy(&h, "auto_apply", json!(false)),
            "verifier" => {
                h.call(ADMIN_TOKEN, json!({"action":"verifier","principal":"node-1","verifier":false,"client_event_id":h.id()})).await;
            }
            "window" => h.set_clock(h.now() + 86_400_001),
            "actor" => attestation_tests::change_policy(&h, "reflect_principals", json!([])),
            _ => unreachable!(),
        }
        let bytes = attestation_tests::bytes_snapshot(&h);
        // A retained watch attempt cannot be applied directly to skip a new watch.
        let direct = h
            .call(
                ADMIN_TOKEN,
                attestation_tests::auto_request(&h, &inverse).await,
            )
            .await;
        assert_ne!(direct.body["state"], "applied", "{drift}: {direct:?}");
        let retried = watch(&h).await;
        assert!(
            retried.body["results"]
                .as_array()
                .unwrap()
                .iter()
                .all(|r| r["result"] != "applied"),
            "{drift}: {retried:?}"
        );
        assert_eq!(attestation_tests::bytes_snapshot(&h), bytes, "{drift}");
        if ["inverse", "head", "kill", "policy"].contains(&drift) {
            assert_eq!(
                retried.body["results"][0]["proposal_id"], inverse,
                "{drift}: {retried:?}"
            );
            let db = rusqlite::Connection::open(h.memory()).unwrap();
            let inverses: i64 = db.query_row("SELECT count(*) FROM memory_proposals WHERE json_extract(body,'$.rollback_of')=?1", [id.as_str().unwrap()], |r| r.get(0)).unwrap();
            assert_eq!(inverses, 1, "{drift}");
        }
    }
}

// Seed healthy applied history before the real regression without 1024 apply RPCs.
#[tokio::test]
async fn compensation_watch_continues_across_polls_and_ignores_forged_after() {
    let (mut h, id, _) = applied().await;
    // Isolate pagination from the 50 ms writer deadline on this bulk fixture.
    h.store
        .as_mut()
        .unwrap()
        .configure_retrieval(&json!({"proposal_limits":{"lock_ms":30_000}}))
        .unwrap();
    let db = rusqlite::Connection::open(h.memory()).unwrap();
    for n in 0..1024 {
        let older = format!("{n:026}");
        db.execute("INSERT INTO memory_proposals(proposal_id,state,body,principal,created_at,decided_by,terminal_ms,compacted) SELECT ?1,state,json_set(body,'$.proposal_id',?1,'$.cards',json('[]')),principal,created_at,decided_by,terminal_ms,compacted FROM memory_proposals WHERE proposal_id=?2", rusqlite::params![older,id.as_str().unwrap()]).unwrap();
        db.execute(
            &"INSERT INTO memory_proposal_scopes(proposal_id,scope) VALUES(?1,'fleet')"
                .replace("fleet", FIXTURE_SCOPE),
            [&older],
        )
        .unwrap();
    }
    verifier_signal(&h, AGENT_TOKEN, "one", "active", true, "").await;
    verifier_signal(&h, NODE_TOKEN, "two", "active", true, "").await;
    for _ in 0..32 {
        let r = h.call(ADMIN_TOKEN,json!({"action":"compensation_watch","scope":FIXTURE_SCOPE,"base_epoch":h.epoch().await,"client_event_id":h.id(),"after":"7ZZZZZZZZZZZZZZZZZZZZZZZZZ"})).await;
        assert_eq!(r.status, 200, "{r:?}");
        assert_eq!(r.body["results"], json!([]), "{r:?}");
        assert!(r.body["next_after"].is_string());
    }
    // A fresh scheduled run starts without any client cursor.
    let r = watch(&h).await;
    assert_eq!(r.body["results"][0]["rollback_of"], id, "{r:?}");
    assert_eq!(r.body["results"][0]["result"], "applied", "{r:?}");
    assert!(r.body["next_after"].is_null());
    // A completed cycle wraps to healthy history instead of starving old entries.
    assert!(watch(&h).await.body["next_after"].is_string());
}

#[tokio::test]
async fn compensation_watch_bounds_cycles_under_sustained_arrivals() {
    let (mut h, id, _) = applied().await;
    h.store
        .as_mut()
        .unwrap()
        .configure_retrieval(&json!({"proposal_limits":{"lock_ms":30_000}}))
        .unwrap();
    let db = rusqlite::Connection::open(h.memory()).unwrap();
    // The real proposal was opened first but becomes eligible after scanning starts.
    db.execute(
        "UPDATE memory_proposals SET state='pending',terminal_ms=NULL WHERE proposal_id=?1",
        [id.as_str().unwrap()],
    )
    .unwrap();
    let insert_ahead = |run: usize| {
        for n in 0..1025 {
            let newer = format!("6{run:05}{n:020}");
            db.execute("INSERT INTO memory_proposals(proposal_id,state,body,principal,created_at,decided_by,terminal_ms,compacted) SELECT ?1,'applied',json_set(body,'$.proposal_id',?1,'$.cards',json('[]')),principal,created_at,decided_by,?3,compacted FROM memory_proposals WHERE proposal_id=?2", rusqlite::params![newer,id.as_str().unwrap(),h.now()]).unwrap();
            db.execute(
                &"INSERT INTO memory_proposal_scopes(proposal_id,scope) VALUES(?1,'fleet')"
                    .replace("fleet", FIXTURE_SCOPE),
                [&newer],
            )
            .unwrap();
        }
    };
    insert_ahead(0);
    verifier_signal(&h, AGENT_TOKEN, "one", "active", true, "").await;
    verifier_signal(&h, NODE_TOKEN, "two", "active", true, "").await;
    let mut scanned = false;
    let mut completed_cycles = 0;
    for run in 0..3 {
        if run > 0 {
            // More than a run's budget arrives ahead of the previous cursor.
            insert_ahead(run);
            if run == 1 {
                db.execute(
                    "UPDATE memory_proposals SET state='applied',terminal_ms=?2 WHERE proposal_id=?1",
                    rusqlite::params![id.as_str().unwrap(), h.now()],
                )
                .unwrap();
                h.set_clock(h.now() + 1);
                // Failure attestations must follow the delayed apply time.
                verifier_signal(&h, AGENT_TOKEN, "late-one", "active", true, "").await;
                verifier_signal(&h, NODE_TOKEN, "late-two", "active", true, "").await;
            }
        }
        for _ in 0..32 {
            let r = watch(&h).await;
            assert_eq!(r.status, 200, "{r:?}");
            scanned |= r.body["results"]
                .as_array()
                .unwrap()
                .iter()
                .any(|r| r["rollback_of"] == id && r["result"] == "applied");
            if r.body["next_after"].is_null() {
                completed_cycles += 1;
                break;
            }
        }
        if scanned {
            break;
        }
    }
    assert!(
        scanned,
        "older proposal must be compensated within the next cycle despite sustained arrivals"
    );
    assert!(completed_cycles >= 1, "the initial cycle must finish");
}

#[tokio::test]
async fn compensation_watch_retry_rebases_after_evidence_append() {
    let (mut h, id, before) = applied().await;
    attestation_tests::change_policy(&h, "reflect_principals", json!(["hub:admin"]));
    let mut report = retrieval_eval_tests::gate_report(&h).await;
    report["adapters"]["hub-lexical"]["overall"]["mrr"] = json!(0.25);
    assert_eq!(retrieval_eval_tests::record(&h, &report).await.status, 201);
    h.store.take();
    h.open(Arc::new(RefuseInverse(before.clone())), None);
    let refused = watch(&h).await;
    assert_eq!(
        refused.body["results"][0]["result"], "failed",
        "{refused:?}"
    );
    assert_eq!(refused.body["results"][0]["reason"], "scan_not_clean");
    let again = watch(&h).await;
    assert_eq!(again.body["results"][0]["result"], "failed");
    assert_eq!(
        again.body["results"][0]["proposal_id"],
        refused.body["results"][0]["proposal_id"]
    );
    let revision = card(&h, FIXTURE_SCOPE, "a").await["revision"]
        .as_i64()
        .unwrap();
    // Retrieval trips do not change when an evidence-only proposal advances the head.
    verifier_signal(&h, AGENT_TOKEN, "later", "active", false, "").await;
    assert!(
        card(&h, FIXTURE_SCOPE, "a").await["revision"]
            .as_i64()
            .unwrap()
            > revision
    );
    h.store.take();
    h.open(
        Arc::new(Fixed(Scan::Clean {
            ruleset: "test-v1".into(),
        })),
        None,
    );
    let retried = watch(&h).await;
    assert_eq!(
        retried.body["results"][0]["result"], "applied",
        "{retried:?}"
    );
    assert_eq!(
        retried.body["results"][0]["proposal_id"],
        refused.body["results"][0]["proposal_id"]
    );
    assert_eq!(card(&h, FIXTURE_SCOPE, "a").await["text"], before);
    let db = rusqlite::Connection::open(h.memory()).unwrap();
    let inverses: i64 = db
        .query_row(
            "SELECT count(*) FROM memory_proposals WHERE json_extract(body,'$.rollback_of')=?1",
            [id.as_str().unwrap()],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(inverses, 1);
    let inverse_id = retried.body["results"][0]["proposal_id"].as_str().unwrap();
    let inverse = proposals::load(&crate::writer::worker::Tx(&db), inverse_id)
        .unwrap()
        .unwrap();
    let digest: String = db
        .query_row(
            "SELECT digest FROM memory_proposals WHERE proposal_id=?1",
            [inverse_id],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(digest, hardening::digest(&inverse));
}

#[tokio::test]
async fn compensation_watch_prefers_later_card_quorum() {
    let (h, id, before) = applied().await;
    let db = rusqlite::Connection::open(h.memory()).unwrap();
    // Seed a second card with the same applied snapshot in this two-card fixture.
    db.execute("INSERT INTO memory_cards SELECT scope,'b',revision,content_hash,state,trust,writer,writer_class,title,summary,tags,created_at,updated_at FROM memory_cards WHERE card_id='a'", []).unwrap();
    db.execute("INSERT INTO memory_card_revisions SELECT scope,'b',revision,content_hash,trust,state,writer,seq,proposal_id,scan_state,created_at FROM memory_card_revisions WHERE card_id='a'", []).unwrap();
    attestation_tests::mutate_body(&h, &id, |b| {
        let mut second = b["cards"][0].clone();
        second["card_id"] = json!("b");
        b["cards"].as_array_mut().unwrap().push(second);
    });
    verifier_signal(&h, AGENT_TOKEN, "first", "active", true, "").await;
    verifier_signal_for_card(&h, "b", AGENT_TOKEN, "second-one", "active", true, "").await;
    verifier_signal_for_card(&h, "b", NODE_TOKEN, "second-two", "active", true, "").await;
    let result = watch(&h).await;
    assert_eq!(result.body["results"][0]["result"], "applied", "{result:?}");
    assert_eq!(card(&h, FIXTURE_SCOPE, "a").await["text"], before);
    assert_eq!(card(&h, FIXTURE_SCOPE, "b").await["text"], before);
}

#[tokio::test]
async fn compensation_watch_review_retrieval_prefers_later_card_quorum() {
    let (h, id, before) = applied().await;
    let db = rusqlite::Connection::open(h.memory()).unwrap();
    // Seed a second card with the same applied snapshot in this two-card fixture.
    db.execute("INSERT INTO memory_cards SELECT scope,'b',revision,content_hash,state,trust,writer,writer_class,title,summary,tags,created_at,updated_at FROM memory_cards WHERE card_id='a'", []).unwrap();
    db.execute("INSERT INTO memory_card_revisions SELECT scope,'b',revision,content_hash,trust,state,writer,seq,proposal_id,scan_state,created_at FROM memory_card_revisions WHERE card_id='a'", []).unwrap();
    attestation_tests::mutate_body(&h, &id, |b| {
        let mut second = b["cards"][0].clone();
        second["card_id"] = json!("b");
        b["cards"].as_array_mut().unwrap().push(second);
    });
    verifier_signal(&h, AGENT_TOKEN, "first", "active", true, "").await;
    verifier_signal_for_card(&h, "b", AGENT_TOKEN, "second-one", "active", true, "").await;
    verifier_signal_for_card(&h, "b", NODE_TOKEN, "second-two", "active", true, "").await;
    h.put(
        ADMIN_TOKEN,
        FIXTURE_SCOPE,
        "decoy",
        None,
        "Unrelated corpus change.",
    )
    .await;
    h.set_clock(h.now() + 1);
    let mut report = retrieval_eval_tests::gate_report(&h).await;
    report["adapters"]["hub-lexical"]["overall"]["mrr"] = json!(0.25);
    assert_eq!(retrieval_eval_tests::record(&h, &report).await.status, 201);
    let result = watch(&h).await;
    assert_eq!(result.body["results"][0]["result"], "applied", "{result:?}");
    assert_eq!(result.body["results"][0]["trigger"], "negative-outcome");
    let journal: Json = serde_json::from_str(
        &db.query_row(
            "SELECT value FROM memory_meta WHERE key=?1",
            [format!("compensation_watch:{}", id.as_str().unwrap())],
            |r| r.get::<_, String>(0),
        )
        .unwrap(),
    )
    .unwrap();
    assert_eq!(journal["trip"]["trigger"], "negative-outcome");
    assert_eq!(journal["trip"]["review_required"], false);
    let fingerprint = ids::sha256_hex(ids::canonical(&journal["trip"]).as_bytes());
    assert_eq!(journal["completed_trips"][fingerprint], "applied");
    assert_eq!(card(&h, FIXTURE_SCOPE, "a").await["text"], before);
    assert_eq!(card(&h, FIXTURE_SCOPE, "b").await["text"], before);
}

#[tokio::test]
async fn compensation_pending_retrieval_referral_is_superseded_by_quorum() {
    for scenario in ["quorum", "no-quorum", "different-inverse"] {
        let (h, id, before) = applied().await;
        verifier_signal(&h, AGENT_TOKEN, "one", "active", true, "").await;
        verifier_signal(&h, NODE_TOKEN, "two", "active", true, "").await;
        // Toggle verifier authority without advancing the card head or digest.
        h.call(ADMIN_TOKEN, json!({"action":"verifier","principal":"node-1","verifier":false,"client_event_id":h.id()})).await;
        let mut report = retrieval_eval_tests::gate_report(&h).await;
        report["adapters"]["hub-lexical"]["overall"]["mrr"] = json!(0.25);
        assert_eq!(retrieval_eval_tests::record(&h, &report).await.status, 201);
        let first = watch(&h).await;
        assert_eq!(first.body["results"][0]["result"], "review_required");
        assert_eq!(first.body["results"][0]["trigger"], "retrieval-regression");
        let referral = first.body["results"][0]["proposal_id"].as_str().unwrap();
        let head = card(&h, FIXTURE_SCOPE, "a").await["revision"].clone();
        if scenario == "different-inverse" {
            attestation_tests::mutate_body(&h, &json!(referral), |b| {
                b["cards"][0]["after_text"] = json!("A different restoration target.");
                b["cards"][0]["after_hash"] =
                    json!(ids::sha256_hex(b"A different restoration target."));
            });
        }
        if scenario != "no-quorum" {
            h.call(ADMIN_TOKEN, json!({"action":"verifier","principal":"node-1","verifier":true,"client_event_id":h.id()})).await;
        }
        assert_eq!(card(&h, FIXTURE_SCOPE, "a").await["revision"], head);
        let second = watch(&h).await;
        let db = rusqlite::Connection::open(h.memory()).unwrap();
        let closed = proposals::load(&crate::writer::worker::Tx(&db), referral)
            .unwrap()
            .unwrap();
        if scenario == "quorum" {
            assert_eq!(second.body["results"][0]["result"], "applied", "{second:?}");
            assert_eq!(closed["state"], "rejected");
            assert_eq!(closed["reason"], "superseded_by_automatic_compensation");
            assert_eq!(card(&h, FIXTURE_SCOPE, "a").await["text"], before);
            let applied: i64 = db.query_row("SELECT count(*) FROM memory_proposals WHERE state='applied' AND json_extract(body,'$.rollback_of')=?1", [id.as_str().unwrap()], |r| r.get(0)).unwrap();
            assert_eq!(applied, 1);
            assert_eq!(watch(&h).await.body["results"], json!([]));
        } else {
            assert_eq!(closed["state"], "pending");
            if scenario == "no-quorum" {
                assert_eq!(second.body["results"], json!([]));
            } else {
                assert_eq!(
                    closed["cards"][0]["after_text"],
                    "A different restoration target."
                );
                assert_eq!(
                    second.body["results"][0]["reason"],
                    "memory_proposal_duplicate_pending"
                );
            }
        }
    }
}

#[tokio::test]
async fn compensation_pending_referral_survives_disabled_apply_until_retry() {
    let (h, id, before) = applied().await;
    verifier_signal(&h, AGENT_TOKEN, "one", "active", true, "").await;
    verifier_signal(&h, NODE_TOKEN, "two", "active", true, "").await;
    h.call(
        ADMIN_TOKEN,
        json!({"action":"verifier","principal":"node-1","verifier":false,"client_event_id":h.id()}),
    )
    .await;
    let mut report = retrieval_eval_tests::gate_report(&h).await;
    report["adapters"]["hub-lexical"]["overall"]["mrr"] = json!(0.25);
    assert_eq!(retrieval_eval_tests::record(&h, &report).await.status, 201);
    let first = watch(&h).await;
    assert_eq!(first.body["results"][0]["result"], "review_required");
    let referral = first.body["results"][0]["proposal_id"].as_str().unwrap();
    h.call(
        ADMIN_TOKEN,
        json!({"action":"verifier","principal":"node-1","verifier":true,"client_event_id":h.id()}),
    )
    .await;
    h.call(
        ADMIN_TOKEN,
        json!({"action":"auto_apply_switch","enabled":false,"client_event_id":h.id()}),
    )
    .await;
    let snapshot = card(&h, FIXTURE_SCOPE, "a").await;
    assert_eq!(watch(&h).await.body["results"][0]["result"], "failed");
    assert_eq!(card(&h, FIXTURE_SCOPE, "a").await, snapshot);
    let db = rusqlite::Connection::open(h.memory()).unwrap();
    let tx = crate::writer::worker::Tx(&db);
    let pending = proposals::load(&tx, referral).unwrap().unwrap();
    assert_eq!(pending["state"], "pending");
    let superseded_events = || {
        db.query_row("SELECT count(*) FROM memory_events WHERE kind='proposal.decide' AND json_extract(payload,'$.proposal_id')=?1 AND json_extract(payload,'$.reason')='superseded_by_automatic_compensation'", [referral], |r| r.get::<_, i64>(0)).unwrap()
    };
    assert_eq!(superseded_events(), 0);
    let pending_count: i64 = db.query_row("SELECT count(*) FROM memory_proposals WHERE state='pending' AND json_extract(body,'$.rollback_of')=?1", [id.as_str().unwrap()], |r| r.get(0)).unwrap();
    assert_eq!(pending_count, 1);
    h.call(
        ADMIN_TOKEN,
        json!({"action":"auto_apply_switch","enabled":true,"client_event_id":h.id()}),
    )
    .await;
    let retried = watch(&h).await;
    assert_eq!(
        retried.body["results"][0]["result"], "applied",
        "{retried:?}"
    );
    assert_eq!(card(&h, FIXTURE_SCOPE, "a").await["text"], before);
    let closed = proposals::load(&tx, referral).unwrap().unwrap();
    assert_eq!(closed["state"], "rejected");
    assert_eq!(closed["reason"], "superseded_by_automatic_compensation");
    assert_eq!(superseded_events(), 1);
    assert_eq!(watch(&h).await.body["results"], json!([]));
}

fn pending_limits(h: &mut Hub, limits: Json) {
    h.store
        .as_mut()
        .unwrap()
        .configure_retrieval(&json!({ "proposal_limits": limits }))
        .unwrap();
}

async fn reject(h: &Hub, id: &str) -> Reply {
    h.call(
        ADMIN_TOKEN,
        json!({"action":"decide","proposal_id":id,"decision":"reject","base_epoch":h.epoch().await,"client_event_id":h.id()}),
    )
    .await
}

async fn review_referral(h: &Hub) -> String {
    verifier_signal(h, AGENT_TOKEN, "one", "active", true, "").await;
    verifier_signal(h, NODE_TOKEN, "two", "active", true, "").await;
    // Toggle verifier authority without advancing the card head or digest.
    h.call(
        ADMIN_TOKEN,
        json!({"action":"verifier","principal":"node-1","verifier":false,"client_event_id":h.id()}),
    )
    .await;
    let first = watch(h).await;
    assert_eq!(
        first.body["results"][0]["result"], "review_required",
        "{first:?}"
    );
    first.body["results"][0]["proposal_id"]
        .as_str()
        .unwrap()
        .to_string()
}

/// A second applied automatic proposal on the same hub, as another cycle would
/// leave one. The stored snapshot is copied so the trip is grouped under a
/// distinct `rollback_of` and its own watch journal key.
fn clone_applied(h: &Hub, id: &str) -> String {
    h.set_clock(h.now() + 1);
    let db = rusqlite::Connection::open(h.memory()).unwrap();
    let raw: String = db
        .query_row(
            "SELECT body FROM memory_proposals WHERE proposal_id=?1",
            [id],
            |r| r.get(0),
        )
        .unwrap();
    let mut body: Json = serde_json::from_str(&raw).unwrap();
    let clone = ids::mint(h.now());
    body["proposal_id"] = json!(clone);
    let raw = body.to_string();
    db.execute(
        "INSERT INTO memory_proposals(proposal_id,state,body,principal,created_at,decided_by,digest,retained_bytes,terminal_ms,compacted) SELECT ?1,state,?2,principal,created_at,decided_by,?3,?4,terminal_ms,compacted FROM memory_proposals WHERE proposal_id=?5",
        rusqlite::params![
            clone,
            raw,
            hardening::digest(&body),
            raw.len() as i64,
            id
        ],
    )
    .unwrap();
    db.execute(
        "INSERT INTO memory_proposal_scopes SELECT ?1,scope FROM memory_proposal_scopes WHERE proposal_id=?2",
        rusqlite::params![clone, id],
    )
    .unwrap();
    clone
}

fn journal(db: &rusqlite::Connection, id: &str) -> Json {
    serde_json::from_str(
        &db.query_row(
            "SELECT value FROM memory_meta WHERE key=?1",
            [format!("compensation_watch:{id}")],
            |r| r.get::<_, String>(0),
        )
        .unwrap(),
    )
    .unwrap()
}

fn result<'a>(reply: &'a Reply, rollback_of: &str) -> &'a Json {
    reply.body["results"]
        .as_array()
        .unwrap()
        .iter()
        .find(|r| r["rollback_of"] == rollback_of)
        .unwrap_or_else(|| panic!("no trip for {rollback_of}: {reply:?}"))
}

async fn assert_quorum_applies_at(limits: Json) {
    let (mut h, id, before) = applied().await;
    let referral = review_referral(&h).await;
    // The referral owns the constrained pending slot. The automatic upgrade
    // closes that row in the same savepoint when it applies.
    pending_limits(&mut h, limits);
    h.call(
        ADMIN_TOKEN,
        json!({"action":"verifier","principal":"node-1","verifier":true,"client_event_id":h.id()}),
    )
    .await;
    let second = watch(&h).await;
    assert_eq!(second.body["results"][0]["result"], "applied", "{second:?}");
    assert_eq!(second.body["results"][0]["rollback_of"], id);
    assert_eq!(card(&h, FIXTURE_SCOPE, "a").await["text"], before);
    let db = rusqlite::Connection::open(h.memory()).unwrap();
    let closed = proposals::load(&crate::writer::worker::Tx(&db), &referral)
        .unwrap()
        .unwrap();
    assert_eq!(closed["state"], "rejected");
    assert_eq!(closed["reason"], "superseded_by_automatic_compensation");
}

#[tokio::test]
async fn compensation_referral_at_principal_quota_still_lets_the_quorum_apply() {
    assert_quorum_applies_at(json!({"pending_per_principal": 1, "pending_per_scope": 1000})).await;
}

#[tokio::test]
async fn compensation_referral_at_scope_quota_still_lets_the_quorum_apply() {
    assert_quorum_applies_at(json!({"pending_per_principal": 1000, "pending_per_scope": 1})).await;
}

#[tokio::test]
async fn compensation_held_inverse_never_exceeds_the_pending_quota() {
    let (mut h, id, _) = applied().await;
    let referral = review_referral(&h).await;
    pending_limits(
        &mut h,
        json!({"pending_per_principal": 1, "pending_per_scope": 1}),
    );
    h.call(
        ADMIN_TOKEN,
        json!({"action":"verifier","principal":"node-1","verifier":true,"client_event_id":h.id()}),
    )
    .await;
    h.call(
        ADMIN_TOKEN,
        json!({"action":"auto_apply_switch","enabled":false,"client_event_id":h.id()}),
    )
    .await;
    let snapshot = card(&h, FIXTURE_SCOPE, "a").await;
    // The inverse is admitted by discounting the referral, but it holds. The
    // referral stays open, so the strict totals must refuse and roll it back.
    let result = watch(&h).await;
    assert_eq!(result.body["results"][0]["result"], "failed", "{result:?}");
    assert_eq!(
        result.body["results"][0]["reason"],
        "memory_proposal_principal_quota"
    );
    assert_eq!(card(&h, FIXTURE_SCOPE, "a").await, snapshot);
    let db = rusqlite::Connection::open(h.memory()).unwrap();
    let principal_count = |principal: &str| -> i64 {
        db.query_row(
            "SELECT count(*) FROM memory_proposals WHERE state IN ('pending','held') AND principal=?1",
            [principal],
            |r| r.get(0),
        )
        .unwrap()
    };
    let scope_count = |scope: &str| -> i64 {
        db.query_row(
            "SELECT count(*) FROM memory_proposals WHERE state IN ('pending','held') AND proposal_id IN (SELECT proposal_id FROM memory_proposal_scopes WHERE scope=?1)",
            [scope],
            |r| r.get(0),
        )
        .unwrap()
    };
    assert_eq!(principal_count("hub:admin"), 1);
    assert_eq!(scope_count(FIXTURE_SCOPE), 1);
    // Repeating a manual rollback while the stop control is engaged cannot
    // stack held rows past the limit either.
    for _ in 0..3 {
        let manual = compensate(&h, &id, ADMIN_TOKEN).await;
        assert_eq!(manual.body["result"], "failed", "{manual:?}");
        assert_eq!(manual.body["reason"], "memory_proposal_principal_quota");
    }
    assert_eq!(principal_count("hub:admin"), 1);
    assert_eq!(scope_count(FIXTURE_SCOPE), 1);
    let held: i64 = db
        .query_row(
            "SELECT count(*) FROM memory_proposals WHERE state='held'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(held, 0);
    let pending = proposals::load(&crate::writer::worker::Tx(&db), &referral)
        .unwrap()
        .unwrap();
    assert_eq!(pending["state"], "pending");
    assert_eq!(card(&h, FIXTURE_SCOPE, "a").await, snapshot);
}

#[tokio::test]
async fn compensation_reviewer_rejection_vetoes_the_held_automatic_inverse() {
    let (h, id, before) = applied().await;
    let referral = review_referral(&h).await;
    h.call(
        ADMIN_TOKEN,
        json!({"action":"verifier","principal":"node-1","verifier":true,"client_event_id":h.id()}),
    )
    .await;
    h.call(
        ADMIN_TOKEN,
        json!({"action":"auto_apply_switch","enabled":false,"client_event_id":h.id()}),
    )
    .await;
    let snapshot = card(&h, FIXTURE_SCOPE, "a").await;
    assert_eq!(watch(&h).await.body["results"][0]["result"], "failed");
    assert_eq!(card(&h, FIXTURE_SCOPE, "a").await, snapshot);
    let db = rusqlite::Connection::open(h.memory()).unwrap();
    let held: i64 = db
        .query_row(
            "SELECT count(*) FROM memory_proposals WHERE state='held' AND json_extract(body,'$.decision_mode')='automatic' AND json_extract(body,'$.rollback_of')=?1",
            [id.as_str().unwrap()],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(held, 1);
    // A reviewer's rejection is a decision for this trip, not only for the
    // referral row the reviewer saw.
    assert_eq!(reject(&h, &referral).await.body["state"], "rejected");
    let marker: i64 = db
        .query_row(
            "SELECT count(*) FROM memory_meta WHERE key=?1",
            [format!("compensation_veto:{}", id.as_str().unwrap())],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(marker, 1);
    h.call(
        ADMIN_TOKEN,
        json!({"action":"auto_apply_switch","enabled":true,"client_event_id":h.id()}),
    )
    .await;
    // A second cycle on the same database: another applied automatic proposal
    // with its own `rollback_of` and journal key.
    let distinct = clone_applied(&h, id.as_str().unwrap());
    let retried = watch(&h).await;
    assert_eq!(result(&retried, id.as_str().unwrap())["result"], "failed");
    assert_eq!(
        result(&retried, id.as_str().unwrap())["reason"],
        "compensation_vetoed"
    );
    assert_eq!(
        result(&retried, &distinct)["result"],
        "applied",
        "{retried:?}"
    );
    assert_eq!(card(&h, FIXTURE_SCOPE, "a").await["text"], before);
    assert_eq!(journal(&db, id.as_str().unwrap())["result"], "stale");
    assert_eq!(journal(&db, &distinct)["result"], "applied");
    let applied_count: i64 = db
        .query_row(
            "SELECT count(*) FROM memory_proposals WHERE state='applied' AND json_extract(body,'$.rollback_of')=?1",
            [id.as_str().unwrap()],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(applied_count, 0);
    // The veto ends retries of this trip. Nothing mutates the card afterwards.
    assert_eq!(watch(&h).await.body["results"], json!([]));
    assert_eq!(card(&h, FIXTURE_SCOPE, "a").await["text"], before);
}

#[tokio::test]
async fn compensation_rejected_manual_rollback_does_not_veto_the_automatic_inverse() {
    let (h, id, before) = applied().await;
    // A reviewer rejecting a manual rollback proposal for the same id is not
    // the review referral decision the veto covers.
    grant(&h, FIXTURE_SCOPE, &["read", "reflect", "review"]).await;
    let opened = compensate(&h, &id, AGENT_TOKEN).await;
    assert_ne!(opened.body["state"], "applied", "{opened:?}");
    let manual = opened.body["proposal_id"].as_str().unwrap().to_string();
    assert_eq!(reject(&h, &manual).await.body["state"], "rejected");
    let db = rusqlite::Connection::open(h.memory()).unwrap();
    let marker: i64 = db
        .query_row(
            "SELECT count(*) FROM memory_meta WHERE key=?1",
            [format!("compensation_veto:{}", id.as_str().unwrap())],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(marker, 0);
    verifier_signal(&h, AGENT_TOKEN, "one", "active", true, "").await;
    verifier_signal(&h, NODE_TOKEN, "two", "active", true, "").await;
    let applied_watch = watch(&h).await;
    assert_eq!(
        result(&applied_watch, id.as_str().unwrap())["result"],
        "applied",
        "{applied_watch:?}"
    );
    assert_eq!(card(&h, FIXTURE_SCOPE, "a").await["text"], before);
}
