use super::{proposal_tests::*, tests::*, *};
use rusqlite::Connection;

const POLICY: &str = r#"{"auto_apply":true,"max_age_days":7}"#;

async fn ready() -> (Hub, Json) {
    ready_with_hook(None).await
}

async fn ready_with_hook(hook: Option<writer::Hook>) -> (Hub, Json) {
    let h = Hub::with(clean(), hook);
    grant(&h, "fleet", &["read", "reflect"]).await;
    let r = h.call(ADMIN_TOKEN, json!({"action":"verifier","principal":"agent:dot","verifier":true,"client_event_id":h.id()})).await;
    assert_eq!(r.status, 200, "{r:?}");
    set_owner(&h, "hub");
    let a = h.put(ADMIN_TOKEN, "fleet", "a", None, OLD).await;
    let opened = submit(&h, AGENT_TOKEN, vec![op("fleet", "a", rev(&a))]).await;
    assert_eq!(opened.status, 201, "{opened:?}");
    (h, opened.body["proposal_id"].clone())
}
fn set_owner(h: &Hub, owner: &str) {
    Connection::open(&h.hub)
        .unwrap()
        .execute(
            "UPDATE fleet_policy_revisions SET document=?1 WHERE revision=1",
            [json!({"memory":{"curation_owner":owner,"auto_apply":true}}).to_string()],
        )
        .unwrap();
}
async fn publish(h: &Hub, token: &str, class: &str) -> Reply {
    let classes: serde_json::Map<String, Json> =
        ["confirm", "correct", "history", "supersede", "no-change"]
            .into_iter()
            .map(|name| {
                (
                    name.into(),
                    json!({"decision":if name==class {"go"}else{"no-go"}}),
                )
            })
            .collect();
    h.call(token, json!({"action":"verify_eval.record", "principal":"agent:dot", "base_epoch":h.epoch().await,
        "record":{"schema":"brigade.memory-verify-decision.v1","binding_sha256":"a".repeat(64),"policy_sha256":ids::sha256_hex(POLICY.as_bytes()),"classes":classes},
        "policy":POLICY,"client_event_id":h.id()})).await
}
async fn apply(h: &Hub, id: &Json, types: Json) -> Reply {
    h.call(AGENT_TOKEN, json!({"action":"verify_auto_apply","proposal_id":id,"base_epoch":h.epoch().await,"binding_sha256":"a".repeat(64),"policy_sha256":ids::sha256_hex(POLICY.as_bytes()),"expected_op_types":types,"client_event_id":h.id()})).await
}

fn automatic_request(mut request: Json) -> Json {
    if request["action"] == "auto_apply" {
        let object = request.as_object_mut().unwrap();
        for key in ["binding_sha256", "policy_sha256", "expected_op_types"] {
            object.remove(key);
        }
    }
    request
}

fn assert_no_writer_locks(h: &Hub, steps: &std::sync::Mutex<Vec<writer::Step>>, batches: u64) {
    let observed = steps.lock().unwrap();
    assert!(
        !observed.contains(&writer::Step::MemoryLocked),
        "{observed:?}"
    );
    assert!(!observed.contains(&writer::Step::HubLocked), "{observed:?}");
    assert_eq!(h.store.as_ref().unwrap().metrics().batches, batches);
}

#[tokio::test]
async fn automatic_prelock_rate_refusals() {
    for action in ["auto_apply", "verify_auto_apply"] {
        let steps = Arc::new(std::sync::Mutex::new(Vec::new()));
        let record = steps.clone();
        let (h, id) =
            ready_with_hook(Some(Arc::new(move |s| record.lock().unwrap().push(s)))).await;
        let request = json!({"action":action,"proposal_id":id,"base_epoch":h.epoch().await,
            "binding_sha256":"a".repeat(64),"policy_sha256":ids::sha256_hex(POLICY.as_bytes()),
            "expected_op_types":["replace_block"],"client_event_id":h.id()});
        assert_eq!(
            h.call(AGENT_TOKEN, automatic_request(request.clone()))
                .await
                .status,
            409
        );
        let mut fresh = request;
        fresh["client_event_id"] = json!(h.id());
        steps.lock().unwrap().clear();
        let batches = h.store.as_ref().unwrap().metrics().batches;
        let refused = h.call(AGENT_TOKEN, automatic_request(fresh)).await;
        assert_eq!(
            (refused.status, code(&refused)),
            (429, "memory_auto_apply_rate_limited")
        );
        assert_no_writer_locks(&h, &steps, batches);
    }
}

#[tokio::test]
async fn automatic_prelock_conflicting_replays() {
    for action in ["auto_apply", "verify_auto_apply"] {
        let steps = Arc::new(std::sync::Mutex::new(Vec::new()));
        let record = steps.clone();
        let (h, id) =
            ready_with_hook(Some(Arc::new(move |s| record.lock().unwrap().push(s)))).await;
        assert_eq!(publish(&h, ADMIN_TOKEN, "correct").await.status, 201);
        let request = json!({"action":"verify_auto_apply","proposal_id":id,"base_epoch":h.epoch().await,
            "binding_sha256":"a".repeat(64),"policy_sha256":ids::sha256_hex(POLICY.as_bytes()),
            "expected_op_types":["replace_block"],"client_event_id":h.id()});
        let applied = h
            .call(AGENT_TOKEN, automatic_request(request.clone()))
            .await;
        assert_eq!(applied.body["state"], "applied");
        assert_eq!(
            h.call(AGENT_TOKEN, automatic_request(request.clone()))
                .await,
            applied
        );
        let mut conflict = request;
        conflict["action"] = json!(action);
        conflict["binding_sha256"] = json!("b".repeat(64));
        steps.lock().unwrap().clear();
        let batches = h.store.as_ref().unwrap().metrics().batches;
        let refused = h.call(AGENT_TOKEN, automatic_request(conflict)).await;
        assert_eq!(
            (refused.status, code(&refused)),
            (409, "memory_client_event_conflict")
        );
        assert_no_writer_locks(&h, &steps, batches);
    }
}

#[tokio::test]
async fn automatic_prelock_nonpending_requests() {
    for action in ["auto_apply", "verify_auto_apply"] {
        let steps = Arc::new(std::sync::Mutex::new(Vec::new()));
        let record = steps.clone();
        let (h, id) =
            ready_with_hook(Some(Arc::new(move |s| record.lock().unwrap().push(s)))).await;
        assert_eq!(publish(&h, ADMIN_TOKEN, "correct").await.status, 201);
        assert_eq!(
            apply(&h, &id, json!(["replace_block"])).await.body["state"],
            "applied"
        );
        let mut request = json!({"action":action,"proposal_id":id,"base_epoch":h.epoch().await,
            "binding_sha256":"a".repeat(64),"policy_sha256":ids::sha256_hex(POLICY.as_bytes()),
            "expected_op_types":["replace_block"],"client_event_id":h.id()});
        steps.lock().unwrap().clear();
        let batches = h.store.as_ref().unwrap().metrics().batches;
        let refused = h
            .call(AGENT_TOKEN, automatic_request(request.clone()))
            .await;
        assert_eq!(
            (refused.status, code(&refused)),
            (429, "memory_auto_apply_rate_limited")
        );
        assert_no_writer_locks(&h, &steps, batches);
        h.set_clock(h.now() + 60_001);
        request["client_event_id"] = json!(h.id());
        let refused = h.call(AGENT_TOKEN, automatic_request(request)).await;
        assert_eq!(refused.status, 409);
        assert_no_writer_locks(&h, &steps, batches);
    }
}

#[tokio::test]
async fn verify_auto_rate_limits_pending_proposal_retries() {
    let (h, id) = ready().await;
    assert_eq!(apply(&h, &id, json!(["replace_block"])).await.status, 409);
    // The principal window expires while the proposal window remains active.
    h.set_clock(h.now() + 1_001);
    let refused = apply(&h, &id, json!(["replace_block"])).await;
    assert_eq!(
        (refused.status, code(&refused)),
        (429, "memory_auto_apply_rate_limited")
    );
    h.set_clock(h.now() + 60_000);
    assert_eq!(apply(&h, &id, json!(["replace_block"])).await.status, 409);
}

#[tokio::test]
async fn verify_auto_rate_limits_principal_across_proposals() {
    let (h, id) = ready().await;
    let b = h.put(ADMIN_TOKEN, "fleet", "b", None, OLD).await;
    let opened = submit(&h, AGENT_TOKEN, vec![op("fleet", "b", rev(&b))]).await;
    assert_eq!(opened.status, 201, "{opened:?}");
    assert_eq!(apply(&h, &id, json!(["replace_block"])).await.status, 409);
    let refused = apply(&h, &opened.body["proposal_id"], json!(["replace_block"])).await;
    assert_eq!(
        (refused.status, code(&refused)),
        (429, "memory_auto_apply_rate_limited")
    );
}

#[tokio::test]
async fn verify_auto_rate_preserves_committed_replay() {
    let (h, id) = ready().await;
    assert_eq!(publish(&h, ADMIN_TOKEN, "correct").await.status, 201);
    let request = json!({"action":"verify_auto_apply","proposal_id":id,"base_epoch":h.epoch().await,
        "binding_sha256":"a".repeat(64),"policy_sha256":ids::sha256_hex(POLICY.as_bytes()),
        "expected_op_types":["replace_block"],"client_event_id":h.id()});
    let applied = h.call(AGENT_TOKEN, request.clone()).await;
    assert_eq!(applied.body["state"], "applied", "{applied:?}");
    assert_eq!(h.call(AGENT_TOKEN, request).await, applied);
    let fresh = apply(&h, &id, json!(["replace_block"])).await;
    assert_eq!(fresh.status, 429, "{fresh:?}");
}

#[tokio::test]
async fn verify_auto_rate_is_shared_with_auto_apply() {
    let (h, id) = ready().await;
    let request = super::attestation_tests::auto_request(&h, &id).await;
    let held = h.call(AGENT_TOKEN, request).await;
    assert_eq!((held.status, code(&held)), (409, "memory_verifier_floor"));
    h.set_clock(h.now() + 1_001);
    let refused = apply(&h, &id, json!(["replace_block"])).await;
    assert_eq!(
        (refused.status, code(&refused)),
        (429, "memory_auto_apply_rate_limited")
    );
}

#[tokio::test]
async fn only_the_exact_proposal_manifest_is_exempt_from_class_admission() {
    use base64::{Engine, engine::general_purpose::STANDARD};
    for shape in [
        "binding",
        "ordinary",
        "mixed",
        "duplicate",
        "not_trailing",
        "extra_correction",
        "wrong_pin",
    ] {
        let (h, _) = ready().await;
        let before = card(&h, "fleet", "a").await;
        if shape == "wrong_pin" {
            h.put(
                ADMIN_TOKEN,
                "fleet",
                "a",
                before["revision"].as_i64(),
                &format!("{OLD}\n\nOther claim. ^c-fedcba"),
            )
            .await;
        }
        let before = card(&h, "fleet", "a").await;
        let revision = before["revision"].as_i64().unwrap();
        let bytes = json!({"schema":"brigade.memory-verify-manifest.v1",
            "card":{"scope":"fleet","card_id":"a","base_revision":revision}})
        .to_string();
        let evidence = "agent:dot/workspace-a/manifest";
        let attached = h.call(AGENT_TOKEN, json!({"action":"evidence.attach","scope":"fleet","card_id":"a",
            "workspace_id":"workspace-a","evidence_id":evidence,"artifact_kind":"memory-verify-manifest",
            "artifact_sha256":ids::sha256_hex(bytes.as_bytes()),"artifact_base64":STANDARD.encode(&bytes),
            "observed_at":h.now(),"stance":"contrary","client_event_id":h.id()})).await;
        assert_eq!(attached.status, 201, "{attached:?}");
        let correction = op("fleet", "a", revision);
        let mut binding = correction.clone();
        binding.as_object_mut().unwrap().remove("text");
        binding["type"] = json!("append_evidence");
        binding["evidence_ids"] = json!([evidence]);
        let mut ordinary = binding.clone();
        ordinary["evidence_ids"] = json!(["hub:admin/workspace-a/other"]);
        let ops = match shape {
            "ordinary" => vec![correction, ordinary],
            "mixed" => {
                binding["evidence_ids"] = json!([evidence, "hub:admin/workspace-a/other"]);
                vec![correction, binding]
            }
            "duplicate" => {
                // A second authenticated manifest has a real delta and also binds this proposal.
                // Citing the identical id twice would be refused earlier as a no-delta op.
                let second = "agent:dot/workspace-a/manifest-two";
                let attached = h.call(AGENT_TOKEN, json!({"action":"evidence.attach","scope":"fleet","card_id":"a",
                    "workspace_id":"workspace-a","evidence_id":second,"artifact_kind":"memory-verify-manifest",
                    "artifact_sha256":ids::sha256_hex(bytes.as_bytes()),"artifact_base64":STANDARD.encode(&bytes),
                    "observed_at":h.now(),"stance":"contrary","client_event_id":h.id()})).await;
                assert_eq!(attached.status, 201, "{attached:?}");
                let mut duplicate = binding.clone();
                duplicate["evidence_ids"] = json!([second]);
                vec![correction, duplicate, binding]
            }
            "not_trailing" => vec![correction, binding, ordinary],
            "extra_correction" => vec![correction, ordinary, binding],
            "wrong_pin" => {
                // A second real block allows a different valid pin, rather than an invalid proposal.
                binding["block_id"] = json!("c-fedcba");
                binding["expected_block_hash"] = json!(ids::sha256_hex(b"Other claim. ^c-fedcba"));
                vec![correction, binding]
            }
            _ => vec![correction, binding],
        };
        let types: Vec<_> = ops.iter().map(|op| op["type"].clone()).collect();
        let opened = submit(&h, AGENT_TOKEN, ops).await;
        assert_eq!(opened.status, 201, "{shape}: {opened:?}");
        assert_eq!(publish(&h, ADMIN_TOKEN, "correct").await.status, 201);
        let current = card(&h, "fleet", "a").await;
        let applied = apply(&h, &opened.body["proposal_id"], json!(types)).await;
        if shape == "binding" {
            assert_eq!(applied.body["state"], "applied", "{applied:?}");
        } else {
            assert_eq!(applied.status, 409, "{shape}: {applied:?}");
            assert_eq!(card(&h, "fleet", "a").await, current, "{shape}");
        }
    }
}
#[tokio::test]
async fn evaluator_authority_and_append_only_admission() {
    let (h, id) = ready().await;
    let before = card(&h, "fleet", "a").await;
    assert_eq!(publish(&h, AGENT_TOKEN, "correct").await.status, 403);
    assert_eq!(apply(&h, &id, json!(["replace_block"])).await.status, 409);
    assert_eq!(card(&h, "fleet", "a").await, before);
    h.set_clock(h.now() + 60_001);
    assert_eq!(publish(&h, ADMIN_TOKEN, "correct").await.status, 201);
    let status = h.call(AGENT_TOKEN, json!({"action":"status"})).await;
    let forged = h
        .call(
            ADMIN_TOKEN,
            json!({"action":"verify_eval.record","principal":"agent:dot",
        "base_epoch":h.epoch().await,"record":status.body["verify_evaluation"]["record"],
        "policy":POLICY.replace("true","false"),"client_event_id":h.id()}),
        )
        .await;
    assert_eq!(forged.status, 422, "{forged:?}");
    let db = Connection::open(h.memory()).unwrap();
    assert!(
        db.execute("UPDATE memory_verify_evals SET body='{}'", [])
            .is_err()
    );
    assert!(db.execute("DELETE FROM memory_verify_evals", []).is_err());
    let applied = apply(&h, &id, json!(["replace_block"])).await;
    assert_eq!(applied.body["state"], "applied", "{applied:?}");
    assert_eq!(applied.body["decision_mode"], "automatic");
    assert_ne!(card(&h, "fleet", "a").await["revision"], before["revision"]);
    let shown = h
        .call(
            AGENT_TOKEN,
            json!({"action":"proposal_show","proposal_id":id}),
        )
        .await;
    assert_eq!(shown.body["decision_mode"], "automatic");
}
#[tokio::test]
async fn owner_kill_policy_class_and_snapshot_rechecked_at_apply() {
    for change in [
        "external",
        "handoff-only",
        "kill",
        "class",
        "types",
        "policy",
        "head",
        "revoked",
        "expired",
        "binding",
    ] {
        let (h, id) = ready().await;
        assert_eq!(
            publish(
                &h,
                ADMIN_TOKEN,
                if change == "class" {
                    "confirm"
                } else {
                    "correct"
                }
            )
            .await
            .status,
            201
        );
        match change {
            "external" | "handoff-only" => set_owner(&h, change),
            "kill" => {
                let r=h.call(ADMIN_TOKEN,json!({"action":"auto_apply_switch","enabled":false,"client_event_id":h.id()})).await;
                assert_eq!(r.status, 200);
            }
            "policy" => {
                Connection::open(&h.hub)
                    .unwrap()
                    .execute(
                        "UPDATE fleet_policy_revisions SET digest='changed' WHERE revision=1",
                        [],
                    )
                    .unwrap();
            }
            "head" => {
                let before = card(&h, "fleet", "a").await;
                h.put(
                    ADMIN_TOKEN,
                    "fleet",
                    "a",
                    before["revision"].as_i64(),
                    "Another claim. ^c-abcdef",
                )
                .await;
            }
            "revoked" => {
                h.call(ADMIN_TOKEN,json!({"action":"verifier","principal":"agent:dot","verifier":false,"client_event_id":h.id()})).await;
            }
            "expired" => {
                h.set_clock(h.now() + 8 * 86_400_000);
            }
            "binding" => {
                assert_eq!(publish(&h, ADMIN_TOKEN, "confirm").await.status, 201);
            }
            _ => (),
        }
        let before = card(&h, "fleet", "a").await;
        let applied = apply(
            &h,
            &id,
            if change == "types" {
                json!(["supersede_card"])
            } else {
                json!(["replace_block"])
            },
        )
        .await;
        assert_ne!(applied.body["state"], "applied", "{change}: {applied:?}");
        assert_eq!(card(&h, "fleet", "a").await, before, "{change}");
    }
}

#[tokio::test]
async fn prepared_request_cannot_race_the_hub_off_switch() {
    let (h, id) = ready().await;
    assert_eq!(publish(&h, ADMIN_TOKEN, "correct").await.status, 201);
    let store = h.store.as_ref().unwrap();
    let credential = store.authenticate(AGENT_TOKEN).await.unwrap();
    let request = json!({"action":"verify_auto_apply","proposal_id":id,"base_epoch":h.epoch().await,
        "binding_sha256":"a".repeat(64),"policy_sha256":ids::sha256_hex(POLICY.as_bytes()),
        "expected_op_types":["replace_block"],"client_event_id":h.id()});
    let mut prepared = store
        .prepare(
            &credential,
            "verify_auto_apply",
            request.as_object().unwrap(),
            &request,
        )
        .unwrap();
    let db = Connection::open(h.memory()).unwrap();
    prepared.action = ops::Action::Proposal(
        proposals::prepare(
            &crate::writer::worker::Tx(&db),
            &credential,
            request,
            clean().as_ref(),
        )
        .unwrap(),
    );
    let before = card(&h, "fleet", "a").await;
    assert_eq!(
        h.call(
            ADMIN_TOKEN,
            json!({"action":"auto_apply_switch","enabled":false,"client_event_id":h.id()})
        )
        .await
        .status,
        200
    );
    let result = store.write(prepared).await;
    assert_eq!(result.status, 409, "{result:?}");
    assert_eq!(card(&h, "fleet", "a").await, before);
}

#[test]
fn version_thirteen_upgrade_adds_immutable_admission_store() {
    let db = Connection::open_in_memory().unwrap();
    schema::migrate(&db, 1_800_000_000_000).unwrap();
    db.execute_batch("DROP TABLE memory_verify_evals; PRAGMA user_version=13")
        .unwrap();
    schema::migrate(&db, 1_800_000_000_001).unwrap();
    let version: i64 = db
        .pragma_query_value(None, "user_version", |r| r.get(0))
        .unwrap();
    assert_eq!(version, 14);
    db.execute(
        "INSERT INTO memory_verify_evals VALUES(1,'agent:test','{}')",
        [],
    )
    .unwrap();
    assert!(db.execute("DELETE FROM memory_verify_evals", []).is_err());
}
