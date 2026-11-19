//! The caller-safe `memory_policy` summary on the `status` reply.
use super::retrieval_eval::FIXTURE_SCOPE;
use super::{proposal_tests::*, tests::*, *};
use rusqlite::Connection;

const A: &str = "repo:acme/api";
const B: &str = "repo:acme/web";

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
/// Grant a principal other than the default caller. `node-1` is the listed
/// reflect principal in these tests.
async fn grant_to(h: &Hub, principal: &str, scope: &str, rights: &[&str]) {
    let r = h.call(ADMIN_TOKEN, json!({"action":"grant", "principal":principal, "scope":scope,"rights":rights,"client_event_id":h.id()})).await;
    assert_eq!(r.status, 200, "{r:?}");
}
fn reserved() -> Json {
    json!({"memory": {"auto_apply": true,
        "tier0_op_classes": ["merge_exact_duplicates", "append_evidence"],
        "reflect_principals": ["node-1"]}})
}
/// Neither the internal marker nor a principal name may reach a caller.
fn assert_no_leak(r: &Reply) {
    let text = r.body.to_string();
    assert!(!text.contains("reflect_scope_holders"), "{r:?}");
    assert!(!text.contains("reflect_holder"), "{r:?}");
    assert!(!text.contains("node-1"), "{r:?}");
}

#[tokio::test]
async fn a_listed_holder_on_one_scope_reserves_only_that_scope() {
    let h = Hub::with(clean(), None);
    grant_to(&h, "node-1", A, &["read", "reflect"]).await;
    grant_to(&h, "agent:dot", A, &["read", "reflect"]).await;
    grant_to(&h, "agent:dot", B, &["read", "reflect"]).await;
    set_policy(&h, reserved());
    let r = status(&h, AGENT_TOKEN).await;
    assert_eq!(r.status, 200, "{r:?}");
    assert_eq!(
        r.body["memory_policy"],
        json!({"auto_apply": true,
               "tier0_op_classes": ["merge_exact_duplicates", "append_evidence"],
               "reserved_scopes": [A]})
    );
    assert_no_leak(&r);
}

#[tokio::test]
async fn no_listed_holder_on_the_callers_scopes_reserves_nothing() {
    let h = Hub::with(clean(), None);
    // The listed principal reflects elsewhere, and a principal that is not
    // listed holds reflect on the caller's scope.
    grant_to(&h, "node-1", B, &["read", "reflect"]).await;
    grant_to(&h, "agent:other", A, &["read", "reflect"]).await;
    grant_to(&h, "agent:dot", A, &["read", "reflect"]).await;
    set_policy(&h, reserved());
    let r = status(&h, AGENT_TOKEN).await;
    assert_eq!(r.status, 200, "{r:?}");
    assert_eq!(
        r.body["memory_policy"]["reserved_scopes"],
        json!([]),
        "{r:?}"
    );
    assert_no_leak(&r);
}

#[tokio::test]
async fn a_scope_the_caller_cannot_reflect_never_appears() {
    let h = Hub::with(clean(), None);
    grant_to(&h, "node-1", A, &["read", "reflect"]).await;
    grant_to(&h, "node-1", B, &["read", "reflect"]).await;
    grant_to(&h, "agent:dot", A, &["read", "reflect"]).await;
    grant_to(&h, "agent:dot", B, &["read", "write"]).await;
    set_policy(&h, reserved());
    let r = status(&h, AGENT_TOKEN).await;
    assert_eq!(
        r.body["memory_policy"]["reserved_scopes"],
        json!([A]),
        "{r:?}"
    );
    assert_no_leak(&r);
}

#[tokio::test]
async fn a_wildcard_grant_counts_for_a_scope_the_caller_names() {
    let h = Hub::with(clean(), None);
    grant_to(&h, "node-1", "repo:*", &["read", "reflect"]).await;
    grant_to(&h, "agent:dot", FIXTURE_SCOPE, &["read", "reflect"]).await;
    set_policy(&h, reserved());
    let r = status(&h, AGENT_TOKEN).await;
    assert_eq!(
        r.body["memory_policy"]["reserved_scopes"],
        json!([FIXTURE_SCOPE]),
        "{r:?}"
    );
}

#[tokio::test]
async fn a_read_only_caller_gets_no_policy_summary() {
    let h = Hub::with(clean(), None);
    grant_to(&h, "node-1", A, &["read", "reflect"]).await;
    grant_to(&h, "agent:dot", A, &["read"]).await;
    set_policy(&h, reserved());
    let r = status(&h, AGENT_TOKEN).await;
    assert_eq!(r.status, 200, "{r:?}");
    assert!(r.body.get("memory_policy").is_none(), "{r:?}");
    assert_no_leak(&r);
    // An enrolled node reads by default but holds no reflect grant here.
    grant_to(&h, "node-1", A, &["read"]).await;
    let node = status(&h, NODE_TOKEN).await;
    assert_eq!(node.status, 200, "{node:?}");
    assert!(node.body.get("memory_policy").is_none(), "{node:?}");
    assert_no_leak_marker(&node);
}
fn assert_no_leak_marker(r: &Reply) {
    let text = r.body.to_string();
    assert!(
        !text.contains("reflect_scope_holders") && !text.contains("reflect_holder"),
        "{r:?}"
    );
}

#[tokio::test]
async fn a_policy_without_a_memory_section_reads_as_off() {
    let h = Hub::with(clean(), None);
    grant_to(&h, "agent:dot", A, &["read", "reflect"]).await;
    set_policy(&h, json!({}));
    let r = status(&h, AGENT_TOKEN).await;
    assert_eq!(r.status, 200, "{r:?}");
    assert_eq!(
        r.body["memory_policy"],
        json!({"auto_apply": false, "tier0_op_classes": [], "reserved_scopes": []})
    );
}

#[tokio::test]
async fn an_empty_principal_list_reserves_nothing() {
    let h = Hub::with(clean(), None);
    grant_to(&h, "node-1", A, &["read", "reflect"]).await;
    grant_to(&h, "agent:dot", A, &["read", "reflect"]).await;
    set_policy(
        &h,
        json!({"memory": {"auto_apply": false,
               "tier0_op_classes": ["append_evidence"], "reflect_principals": []}}),
    );
    let r = status(&h, AGENT_TOKEN).await;
    assert_eq!(
        r.body["memory_policy"],
        json!({"auto_apply": false, "tier0_op_classes": ["append_evidence"],
               "reserved_scopes": []})
    );
}
