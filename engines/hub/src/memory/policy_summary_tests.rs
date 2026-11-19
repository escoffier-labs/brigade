//! The caller-safe `memory_policy` summary on the `status` reply.
use super::retrieval_eval::FIXTURE_SCOPE;
use super::{proposal_tests::*, tests::*, *};
use rusqlite::Connection;

fn set_policy(h: &Hub, document: Json) {
    Connection::open(&h.hub)
        .unwrap()
        .execute(
            "UPDATE fleet_policy_revisions SET document=?1 WHERE revision=1",
            [document.to_string()],
        )
        .unwrap();
}
async fn status(h: &Hub, token: &str) -> Reply {
    h.call(token, json!({"action":"status"})).await
}
fn reserved() -> Json {
    json!({"memory": {"auto_apply": true,
        "tier0_op_classes": ["merge_exact_duplicates", "append_evidence"],
        "reflect_principals": ["agent:writer"]}})
}

#[tokio::test]
async fn a_reflect_caller_sees_the_policy_summary_without_the_principal_list() {
    let h = Hub::with(clean(), None);
    grant(&h, FIXTURE_SCOPE, &["read", "reflect"]).await;
    set_policy(&h, reserved());
    let r = status(&h, AGENT_TOKEN).await;
    assert_eq!(r.status, 200, "{r:?}");
    assert_eq!(
        r.body["memory_policy"],
        json!({"auto_apply": true,
               "tier0_op_classes": ["merge_exact_duplicates", "append_evidence"],
               "reflect_principals_nonempty": true})
    );
    assert!(!r.body.to_string().contains("agent:writer"), "{r:?}");
}

#[tokio::test]
async fn a_read_only_caller_gets_no_policy_summary() {
    let h = Hub::with(clean(), None);
    grant(&h, FIXTURE_SCOPE, &["read"]).await;
    set_policy(&h, reserved());
    let r = status(&h, AGENT_TOKEN).await;
    assert_eq!(r.status, 200, "{r:?}");
    assert!(r.body.get("memory_policy").is_none(), "{r:?}");
    // An enrolled node reads by default but holds no reflect grant.
    let node = status(&h, NODE_TOKEN).await;
    assert!(node.body.get("memory_policy").is_none(), "{node:?}");
}

#[tokio::test]
async fn a_policy_without_a_memory_section_reads_as_off() {
    let h = Hub::with(clean(), None);
    grant(&h, FIXTURE_SCOPE, &["read", "reflect"]).await;
    set_policy(&h, json!({}));
    let r = status(&h, AGENT_TOKEN).await;
    assert_eq!(r.status, 200, "{r:?}");
    assert_eq!(
        r.body["memory_policy"],
        json!({"auto_apply": false, "tier0_op_classes": [],
               "reflect_principals_nonempty": false})
    );
}

#[tokio::test]
async fn an_empty_principal_list_and_a_disabled_policy_are_reported_as_is() {
    let h = Hub::with(clean(), None);
    grant(&h, FIXTURE_SCOPE, &["read", "reflect"]).await;
    set_policy(
        &h,
        json!({"memory": {"auto_apply": false,
               "tier0_op_classes": ["append_evidence"], "reflect_principals": []}}),
    );
    let r = status(&h, AGENT_TOKEN).await;
    assert_eq!(
        r.body["memory_policy"],
        json!({"auto_apply": false, "tier0_op_classes": ["append_evidence"],
               "reflect_principals_nonempty": false})
    );
}
