use super::retrieval_eval::FIXTURE_SCOPE;
use super::{proposal_tests::*, tests::*, *};
use rusqlite::Connection;

const POLICY: &str = r#"{"auto_apply":true,"max_age_days":7}"#;

async fn ready() -> (Hub, Json) {
    ready_with_hook(None).await
}

async fn ready_with_hook(hook: Option<writer::Hook>) -> (Hub, Json) {
    let h = Hub::with(clean(), hook);
    grant(&h, FIXTURE_SCOPE, &["read", "reflect"]).await;
    let r = h.call(ADMIN_TOKEN, json!({"action":"verifier","principal":"agent:dot","verifier":true,"client_event_id":h.id()})).await;
    assert_eq!(r.status, 200, "{r:?}");
    set_owner(&h, "hub");
    let a = h.put(ADMIN_TOKEN, FIXTURE_SCOPE, "a", None, OLD).await;
    let opened = submit(&h, AGENT_TOKEN, vec![op(FIXTURE_SCOPE, "a", rev(&a))]).await;
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

/// Attach a verifier manifest checked against the card's current version.
async fn attach_manifest(h: &Hub, card_id: &str, evidence: &str) -> i64 {
    use base64::{Engine, engine::general_purpose::STANDARD};
    let before = card(h, FIXTURE_SCOPE, card_id).await;
    let revision = before["revision"].as_i64().unwrap();
    let bytes = json!({"schema":"brigade.memory-verify-manifest.v1",
        "card":{"scope":FIXTURE_SCOPE,"card_id":card_id,"base_revision":revision}})
    .to_string();
    let attached = h.call(AGENT_TOKEN, json!({"action":"evidence.attach","scope":FIXTURE_SCOPE,
        "card_id":card_id,"workspace_id":"workspace-a","evidence_id":evidence,
        "artifact_kind":"memory-verify-manifest","artifact_sha256":ids::sha256_hex(bytes.as_bytes()),
        "artifact_base64":STANDARD.encode(&bytes),"observed_at":h.now(),"stance":"contrary",
        "client_event_id":h.id()})).await;
    assert_eq!(attached.status, 201, "{attached:?}");
    revision
}

/// A real verifier correction: one block replacement plus the trailing
/// authenticated manifest binding that class admission exempts.
async fn verifier_correction(h: &Hub, card_id: &str, evidence: &str) -> Json {
    let revision = attach_manifest(h, card_id, evidence).await;
    let correction = op(FIXTURE_SCOPE, card_id, revision);
    let mut binding = correction.clone();
    binding.as_object_mut().unwrap().remove("text");
    binding["type"] = json!("append_evidence");
    binding["evidence_ids"] = json!([evidence]);
    let opened = submit(h, AGENT_TOKEN, vec![correction, binding]).await;
    assert_eq!(opened.status, 201, "{opened:?}");
    opened.body["proposal_id"].clone()
}

fn held_classes(h: &Hub) -> Vec<String> {
    let db = Connection::open(h.memory()).unwrap();
    let mut st = db
        .prepare("SELECT key FROM memory_meta WHERE key LIKE 'compensation_hold:%' ORDER BY key")
        .unwrap();
    st.query_map([], |r| r.get::<_, String>(0))
        .unwrap()
        .map(Result::unwrap)
        .collect()
}

/// An automatic compensation actor needs reflect in scope, and Tier 0 needs to
/// admit an ordinary evidence append for the not-held assertion.
fn enable_compensation(h: &Hub) {
    set_owner(h, "hub");
    attestation_tests::change_policy(h, "reflect_principals", json!(["agent:dot"]));
    attestation_tests::change_policy(h, "tier0_op_classes", json!(["append_evidence"]));
}

/// A hub with a verifier-flagged `agent:dot`, cards `a` and `b`, and an
/// automatic compensation actor configured.
async fn compensation_hub() -> Hub {
    let h = Hub::with(clean(), None);
    grant(&h, FIXTURE_SCOPE, &["read", "reflect"]).await;
    let flagged = h
        .call(
            ADMIN_TOKEN,
            json!({"action":"verifier","principal":"agent:dot","verifier":true,"client_event_id":h.id()}),
        )
        .await;
    assert_eq!(flagged.status, 200, "{flagged:?}");
    enable_compensation(&h);
    h.put(ADMIN_TOKEN, FIXTURE_SCOPE, "a", None, OLD).await;
    h.put(ADMIN_TOKEN, FIXTURE_SCOPE, "b", None, OLD).await;
    h
}

async fn compensate(h: &Hub, id: &Json) -> Reply {
    h.set_clock(h.now() + 1);
    h.call(AGENT_TOKEN, json!({"action":"rollback","proposal_id":id,"base_epoch":h.epoch().await,"client_event_id":h.id()})).await
}

/// An `append_evidence` op on the card's current version, citing `evidence`.
fn append_op(card_id: &str, card: &Json, evidence: Json) -> Json {
    let mut operation = op(FIXTURE_SCOPE, card_id, card["revision"].as_i64().unwrap());
    operation["type"] = json!("append_evidence");
    operation.as_object_mut().unwrap().remove("text");
    operation["evidence_ids"] = evidence;
    operation
}

async fn fleet_verifier_proposal() -> (Hub, Json, Json) {
    let h = Hub::with(clean(), None);
    grant(&h, "fleet", &["read", "reflect"]).await;
    set_owner(&h, "hub");
    let r = h.call(ADMIN_TOKEN, json!({"action":"verifier","principal":"agent:dot","verifier":true,"client_event_id":h.id()})).await;
    assert_eq!(r.status, 200, "{r:?}");
    let a = h.put(ADMIN_TOKEN, "fleet", "a", None, OLD).await;
    let before = card(&h, "fleet", "a").await;
    let opened = submit(&h, AGENT_TOKEN, vec![op("fleet", "a", rev(&a))]).await;
    assert_eq!(opened.status, 201, "{opened:?}");
    assert_eq!(publish(&h, ADMIN_TOKEN, "correct").await.status, 201);
    (h, opened.body["proposal_id"].clone(), before)
}

#[tokio::test]
async fn fleet_verifier_correction_applies_with_current_admission() {
    let (h, id, before) = fleet_verifier_proposal().await;
    let applied = apply(&h, &id, json!(["replace_block"])).await;
    assert_eq!(applied.body["state"], "applied", "{applied:?}");
    let after = card(&h, "fleet", "a").await;
    assert_ne!(after["text"], before["text"]);
    assert!(
        after["text"]
            .as_str()
            .unwrap()
            .contains("Use the scoped gate.")
    );
}

#[tokio::test]
async fn fleet_exact_automatic_compensation_restores_bytes() {
    let (h, id, before) = fleet_verifier_proposal().await;
    let applied = apply(&h, &id, json!(["replace_block"])).await;
    assert_eq!(applied.body["state"], "applied", "{applied:?}");
    assert_ne!(card(&h, "fleet", "a").await["text"], before["text"]);
    attestation_tests::change_policy(&h, "reflect_principals", json!(["agent:dot"]));
    h.set_clock(h.now() + 1);
    let restored = h.call(AGENT_TOKEN, json!({"action":"rollback","proposal_id":id,"base_epoch":h.epoch().await,"client_event_id":h.id()})).await;
    assert_eq!(restored.body["state"], "applied", "{restored:?}");
    assert_eq!(restored.body["decision_mode"], "automatic");
    assert_eq!(card(&h, "fleet", "a").await["text"], before["text"]);
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
    let b = h.put(ADMIN_TOKEN, FIXTURE_SCOPE, "b", None, OLD).await;
    let opened = submit(&h, AGENT_TOKEN, vec![op(FIXTURE_SCOPE, "b", rev(&b))]).await;
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
        let before = card(&h, FIXTURE_SCOPE, "a").await;
        if shape == "wrong_pin" {
            h.put(
                ADMIN_TOKEN,
                FIXTURE_SCOPE,
                "a",
                before["revision"].as_i64(),
                &format!("{OLD}\n\nOther claim. ^c-fedcba"),
            )
            .await;
        }
        let before = card(&h, FIXTURE_SCOPE, "a").await;
        let revision = before["revision"].as_i64().unwrap();
        let bytes = json!({"schema":"brigade.memory-verify-manifest.v1",
            "card":{"scope":FIXTURE_SCOPE,"card_id":"a","base_revision":revision}})
        .to_string();
        let evidence = "agent:dot/workspace-a/manifest";
        let attached = h.call(AGENT_TOKEN, json!({"action":"evidence.attach","scope":FIXTURE_SCOPE,"card_id":"a",
            "workspace_id":"workspace-a","evidence_id":evidence,"artifact_kind":"memory-verify-manifest",
            "artifact_sha256":ids::sha256_hex(bytes.as_bytes()),"artifact_base64":STANDARD.encode(&bytes),
            "observed_at":h.now(),"stance":"contrary","client_event_id":h.id()})).await;
        assert_eq!(attached.status, 201, "{attached:?}");
        let correction = op(FIXTURE_SCOPE, "a", revision);
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
                let attached = h.call(AGENT_TOKEN, json!({"action":"evidence.attach","scope":FIXTURE_SCOPE,"card_id":"a",
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
        let current = card(&h, FIXTURE_SCOPE, "a").await;
        let applied = apply(&h, &opened.body["proposal_id"], json!(types)).await;
        if shape == "binding" {
            assert_eq!(applied.body["state"], "applied", "{applied:?}");
        } else {
            assert_eq!(applied.status, 409, "{shape}: {applied:?}");
            assert_eq!(card(&h, FIXTURE_SCOPE, "a").await, current, "{shape}");
        }
    }
}
#[tokio::test]
async fn evaluator_authority_and_append_only_admission() {
    let (h, id) = ready().await;
    let before = card(&h, FIXTURE_SCOPE, "a").await;
    assert_eq!(publish(&h, AGENT_TOKEN, "correct").await.status, 403);
    assert_eq!(apply(&h, &id, json!(["replace_block"])).await.status, 409);
    assert_eq!(card(&h, FIXTURE_SCOPE, "a").await, before);
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
    assert_ne!(
        card(&h, FIXTURE_SCOPE, "a").await["revision"],
        before["revision"]
    );
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
                let before = card(&h, FIXTURE_SCOPE, "a").await;
                h.put(
                    ADMIN_TOKEN,
                    FIXTURE_SCOPE,
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
        let before = card(&h, FIXTURE_SCOPE, "a").await;
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
        assert_eq!(card(&h, FIXTURE_SCOPE, "a").await, before, "{change}");
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
    let before = card(&h, FIXTURE_SCOPE, "a").await;
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
    assert_eq!(card(&h, FIXTURE_SCOPE, "a").await, before);
}

#[test]
fn version_thirteen_upgrade_adds_immutable_admission_store() {
    let db = Connection::open_in_memory().unwrap();
    schema::migrate(&db, 1_800_000_000_000).unwrap();
    db.execute_batch("DROP TABLE memory_verify_evals; DROP TABLE memory_retrieval_history; PRAGMA user_version=13")
        .unwrap();
    schema::migrate(&db, 1_800_000_000_001).unwrap();
    let version: i64 = db
        .pragma_query_value(None, "user_version", |r| r.get(0))
        .unwrap();
    assert_eq!(version, 15);
    db.execute(
        "INSERT INTO memory_verify_evals VALUES(1,'agent:test','{}')",
        [],
    )
    .unwrap();
    assert!(db.execute("DELETE FROM memory_verify_evals", []).is_err());
}

#[tokio::test]
async fn automatic_refusals_before_authorization_spend_no_rate_slot() {
    for action in ["auto_apply", "verify_auto_apply"] {
        let (h, id) = ready().await;
        let request = json!({"action":action,"proposal_id":id,"base_epoch":h.epoch().await,
            "binding_sha256":"a".repeat(64),"policy_sha256":ids::sha256_hex(POLICY.as_bytes()),
            "expected_op_types":["replace_block"],"client_event_id":h.id()});
        // A request for an unknown proposal is refused without using the principal's window.
        let mut missing = request.clone();
        missing["proposal_id"] = json!(h.id());
        missing["client_event_id"] = json!(h.id());
        let refused = h.call(AGENT_TOKEN, automatic_request(missing)).await;
        assert_eq!(refused.status, 404, "{action}");
        let next = h.call(AGENT_TOKEN, automatic_request(request)).await;
        assert_ne!(
            (next.status, code(&next)),
            (429, "memory_auto_apply_rate_limited"),
            "{action}"
        );
    }
}

#[tokio::test]
async fn verifier_applies_keep_the_card_cooldown() {
    let (h, id) = ready().await;
    assert_eq!(publish(&h, ADMIN_TOKEN, "correct").await.status, 201);
    // A reviewed change applied to the same card less than 24 hours ago.
    Connection::open(h.memory())
        .unwrap()
        .execute(
            "INSERT INTO memory_events(seq,event_id,client_event_id,kind,principal,scope,card_id,revision,payload,created_at) \
             VALUES(900000,'recent-apply','recent-apply','proposal.apply','reviewer',?2,'a',900000,'{}',?1)",
            rusqlite::params![ids::isoformat(h.now() - 60_000), FIXTURE_SCOPE],
        )
        .unwrap();
    let request = json!({"action":"verify_auto_apply","proposal_id":id,"base_epoch":h.epoch().await,
        "binding_sha256":"a".repeat(64),"policy_sha256":ids::sha256_hex(POLICY.as_bytes()),
        "expected_op_types":["replace_block"],"client_event_id":h.id()});
    let refused = h.call(AGENT_TOKEN, automatic_request(request)).await;
    assert_eq!(
        (refused.status, code(&refused)),
        (409, "memory_card_cooldown_active"),
        "{refused:?}"
    );
}

#[tokio::test]
async fn verifier_applies_respect_compensation_class_holds() {
    let h = compensation_hub().await;
    let id = verifier_correction(&h, "a", "agent:dot/workspace-a/manifest-a").await;
    assert_eq!(publish(&h, ADMIN_TOKEN, "correct").await.status, 201);
    let applied = apply(&h, &id, json!(["replace_block", "append_evidence"])).await;
    assert_eq!(applied.body["state"], "applied", "{applied:?}");

    // Drive a real compensation of the verifier proposal, not a hand-inserted hold.
    let compensated = compensate(&h, &id).await;
    assert_eq!(compensated.body["state"], "applied", "{compensated:?}");
    assert_eq!(
        held_classes(&h),
        vec![compensation::hold_key(FIXTURE_SCOPE, "replace_block")]
    );
    assert!(!held_classes(&h).contains(&compensation::hold_key(FIXTURE_SCOPE, "append_evidence")));

    // Ordinary Tier 0 `append_evidence` in the same scope is not held.
    let b = card(&h, FIXTURE_SCOPE, "b").await;
    let ordinary = submit(
        &h,
        ADMIN_TOKEN,
        vec![append_op("b", &b, json!(["hub:admin/workspace-a/plain"]))],
    )
    .await;
    assert_eq!(ordinary.status, 201, "{ordinary:?}");
    let report = attestation_tests::report(&h, &ordinary.body["proposal_id"]).await;
    assert!(
        report.body["checks"]
            .as_array()
            .unwrap()
            .iter()
            .all(|c| c["code"] != "compensation_class_held"),
        "{report:?}"
    );

    // A later verifier proposal in the same scope stops on the content class.
    let later = submit(
        &h,
        AGENT_TOKEN,
        vec![op(FIXTURE_SCOPE, "b", b["revision"].as_i64().unwrap())],
    )
    .await;
    assert_eq!(later.status, 201, "{later:?}");
    h.set_clock(h.now() + 60_001);
    let refused = apply(&h, &later.body["proposal_id"], json!(["replace_block"])).await;
    assert_eq!(
        (refused.status, code(&refused)),
        (409, "compensation_class_held"),
        "{refused:?}"
    );
    assert!(
        refused.body["error"]
            .as_str()
            .is_some_and(|message| message.contains(&format!("{FIXTURE_SCOPE}/replace_block"))),
        "{refused:?}"
    );
    assert_eq!(card(&h, FIXTURE_SCOPE, "b").await, b);

    // Releasing this exact scope and operation lets the apply proceed.
    let released = h
        .call(
            ADMIN_TOKEN,
            json!({"action":"auto_apply_switch","enabled":true,"scope":FIXTURE_SCOPE,"op_type":"replace_block","client_event_id":h.id()}),
        )
        .await;
    assert_eq!(released.status, 200, "{released:?}");
    assert!(held_classes(&h).is_empty(), "{:?}", held_classes(&h));
    h.set_clock(h.now() + 60_001);
    let retried = apply(&h, &later.body["proposal_id"], json!(["replace_block"])).await;
    assert_eq!(retried.body["state"], "applied", "{retried:?}");
    assert_ne!(card(&h, FIXTURE_SCOPE, "b").await["text"], b["text"]);
}

#[tokio::test]
async fn verifier_confirm_after_a_verifier_compensation_is_not_held() {
    let h = compensation_hub().await;
    let id = verifier_correction(&h, "a", "agent:dot/workspace-a/manifest-a").await;
    assert_eq!(publish(&h, ADMIN_TOKEN, "correct").await.status, 201);
    let applied = apply(&h, &id, json!(["replace_block", "append_evidence"])).await;
    assert_eq!(applied.body["state"], "applied", "{applied:?}");
    let compensated = compensate(&h, &id).await;
    assert_eq!(compensated.body["state"], "applied", "{compensated:?}");

    // A later confirm-only verifier proposal: one evidence append plus its
    // trailing authenticated manifest binding.
    let manifest = "agent:dot/workspace-a/manifest-b";
    let revision = attach_manifest(&h, "b", manifest).await;
    let mut evidence = op(FIXTURE_SCOPE, "b", revision);
    evidence["type"] = json!("append_evidence");
    evidence.as_object_mut().unwrap().remove("text");
    evidence["evidence_ids"] = json!(["hub:admin/workspace-a/plain"]);
    let mut binding = evidence.clone();
    binding["evidence_ids"] = json!([manifest]);
    let opened = submit(&h, AGENT_TOKEN, vec![evidence, binding]).await;
    assert_eq!(opened.status, 201, "{opened:?}");
    assert_eq!(publish(&h, ADMIN_TOKEN, "confirm").await.status, 201);
    h.set_clock(h.now() + 60_001);
    let confirmed = apply(
        &h,
        &opened.body["proposal_id"],
        json!(["append_evidence", "append_evidence"]),
    )
    .await;
    assert_eq!(confirmed.body["state"], "applied", "{confirmed:?}");
    assert!(!held_classes(&h).contains(&compensation::hold_key(FIXTURE_SCOPE, "append_evidence")));
}

#[test]
fn version_thirteen_and_fourteen_upgrade_preserve_tables_and_rows() {
    for version in [13, 14] {
        let db = Connection::open_in_memory().unwrap();
        // Use the existing upgrade fixture, excluding both post-v13 additions.
        schema::migrate(&db, 1_800_000_000_000).unwrap();
        db.execute_batch("DROP TABLE memory_retrieval_history")
            .unwrap();
        if version == 13 {
            db.execute_batch("DROP TABLE memory_verify_evals").unwrap();
        } else {
            db.execute(
                "INSERT INTO memory_verify_evals VALUES(7,'agent:test','{}')",
                [],
            )
            .unwrap();
        }
        db.pragma_update(None, "user_version", version).unwrap();
        db.execute(
            "INSERT INTO memory_blobs VALUES('saved',X'616263',3,'before')",
            [],
        )
        .unwrap();
        db.execute(
            "INSERT INTO memory_grants VALUES('agent:test','fleet','read','before','admin')",
            [],
        )
        .unwrap();
        let tables: Vec<String> = db
            .prepare("SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name")
            .unwrap()
            .query_map([], |r| r.get(0))
            .unwrap()
            .map(Result::unwrap)
            .collect();
        let snapshot = || {
            tables
                .iter()
                .map(|table| {
                    let mut statement = db.prepare(&format!("SELECT * FROM {table}")).unwrap();
                    let columns = statement.column_count();
                    let mut rows: Vec<Vec<rusqlite::types::Value>> = statement
                        .query_map([], |row| (0..columns).map(|i| row.get(i)).collect())
                        .unwrap()
                        .map(Result::unwrap)
                        .collect();
                    rows.sort_by_key(|row| format!("{row:?}"));
                    rows
                })
                .collect::<Vec<_>>()
        };
        let before = snapshot();
        schema::migrate(&db, 1_800_000_000_001).unwrap();
        assert_eq!(
            db.pragma_query_value(None, "user_version", |r| r.get::<_, i64>(0))
                .unwrap(),
            15
        );
        for table in schema::TABLES {
            assert!(
                db.query_row(
                    "SELECT EXISTS(SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?1)",
                    [table],
                    |r| r.get::<_, bool>(0)
                )
                .unwrap(),
                "{version}: {table}"
            );
        }
        assert_eq!(snapshot(), before, "version {version}");
        if version == 14 {
            assert!(db.execute("DELETE FROM memory_verify_evals", []).is_err());
        }
        schema::migrate(&db, 1_800_000_000_002).unwrap();
        assert_eq!(snapshot(), before, "repeat version {version}");
    }
}
