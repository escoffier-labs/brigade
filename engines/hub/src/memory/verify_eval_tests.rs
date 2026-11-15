use super::{proposal_tests::*, tests::*, *};
use rusqlite::Connection;

const POLICY: &str = r#"{"auto_apply":true,"max_age_days":7}"#;

async fn ready() -> (Hub, Json) {
    let h = Hub::with(clean(), None);
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
#[tokio::test]
async fn evaluator_authority_and_append_only_admission() {
    let (h, id) = ready().await;
    let before = card(&h, "fleet", "a").await;
    assert_eq!(publish(&h, AGENT_TOKEN, "correct").await.status, 403);
    assert_eq!(apply(&h, &id, json!(["replace_block"])).await.status, 409);
    assert_eq!(card(&h, "fleet", "a").await, before);
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
