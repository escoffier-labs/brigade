use super::*;

fn refusal(raw: Json) -> Reply {
    validate(&raw).unwrap_err()
}
#[test]
fn validation_follows_python_order_and_text() {
    assert_eq!(
        refusal(json!(["acquire"])),
        error(400, "claim request must be a JSON object")
    );
    assert_eq!(
        refusal(json!({"action": "acquire", "scope": "force"})),
        error(
            400,
            "claim field 'scope' for acquire must be one of: holder, node"
        )
    );
    assert_eq!(
        refusal(json!({"action": "release", "scope": null})),
        error(
            400,
            "claim field 'scope' for release must be one of: force, holder, node"
        )
    );
    // The target's control check runs before node_id is read at all.
    assert_eq!(
        refusal(json!({"action": "acquire", "target": "a\u{85}b"})).0,
        422
    );
    assert_eq!(
        refusal(json!({"action": "acquire", "target": "t", "node_id": "n", "holder": "unknown"})),
        error(
            400,
            "claim field 'holder' must be a real identity ([A-Za-z0-9._-], max 128 chars, never the literal 'unknown')"
        )
    );
    let base = json!({"action": "renew", "target": "t", "node_id": "n", "holder": "h"});
    let with = |key: &str, value: Json| {
        let mut raw = base.clone();
        raw[key] = value;
        refusal(raw)
    };
    assert_eq!(
        with("role", json!("x")),
        error(400, "claim field 'role' is only valid for acquire")
    );
    assert_eq!(
        with("ttl_seconds", json!(900.0)),
        error(
            400,
            "claim field 'ttl_seconds' must be an integer in [1, 86400]"
        )
    );
    assert_eq!(
        with("ttl_seconds", json!(true)).1,
        error(
            400,
            "claim field 'ttl_seconds' must be an integer in [1, 86400]"
        )
        .1
    );
    assert_eq!(
        with("generation", Json::Null),
        error(
            400,
            "claim field 'generation' must be an integer in [0, 9223372036854775807]"
        )
    );
    assert_eq!(
        with("lock", json!({"token": "x"})),
        error(400, "claim field 'lock' is only valid for acquire")
    );
    // The control check on conductor is on the unstripped value.
    assert_eq!(with("conductor", json!(" c\n")).0, 422);
    assert!(validate(&with_ok(&base, "conductor", json!(5))).is_ok());
}
fn with_ok(base: &Json, key: &str, value: Json) -> Json {
    let mut raw = base.clone();
    raw[key] = value;
    raw
}
#[test]
fn leases_and_node_release_rules() {
    let acquire =
        json!({"action": "acquire", "scope": "node", "target": "t", "node_id": "n", "holder": "h"});
    assert_eq!(
        refusal(acquire.clone()),
        error(
            400,
            "claim field 'supersede' (the reconciled dead lease) is required for a node-scoped acquire"
        )
    );
    assert_eq!(
        refusal(with_ok(&acquire, "supersede", json!({"token": " "}))),
        error(
            400,
            "claim field 'supersede' must carry a non-empty 'token'"
        )
    );
    assert_eq!(
        refusal(with_ok(
            &acquire,
            "supersede",
            json!({"token": "x", "run_dir": 3})
        )),
        error(400, "claim field 'supersede'.run_dir must be a string")
    );
    let request = validate(&with_ok(
        &acquire,
        "supersede",
        json!({"token": " x ", "acquired_at": "  "}),
    ))
    .unwrap();
    let lease = request.supersede.unwrap();
    assert_eq!((lease.token.as_str(), lease.acquired_at), ("x", None));
    let release = json!({"action": "release", "scope": "node", "target": "t", "node_id": "n"});
    assert_eq!(
        refusal(release.clone()),
        error(
            400,
            "claim field 'acquired_at' is required for a token-less node-scoped release"
        )
    );
    let request = validate(&with_ok(&release, "acquired_at", json!(" a "))).unwrap();
    assert!(request.needs_snapshot());
    assert_eq!(
        (request.holder, request.acquired_at.as_deref()),
        (None, Some("a"))
    );
}
#[test]
fn python_repr_quotes_and_non_ascii_bodies() {
    assert_eq!(repr("repo"), "'repo'");
    assert_eq!(repr("it's"), "\"it's\"");
    assert_eq!(repr("a'b\"c\\"), "'a\\'b\"c\\\\'");
    assert!(python_only(&json!({"target": "café"})));
    assert!(!python_only(&json!({"target": "repo", "role": "café"})));
    assert!(!python_only(&json!({"target": 5})));
}
#[test]
fn forbidden_names_the_token_node() {
    let request =
        validate(&json!({"action": "inspect", "target": "t", "node_id": "other"})).unwrap();
    assert_eq!(
        forbidden(&request, Some("self")),
        Some(error(
            403,
            "claim node_id 'other' does not match the caller's node token (self)"
        ))
    );
    assert_eq!(forbidden(&request, None), None);
}
#[test]
fn stored_values_render_as_python_json() {
    assert_eq!(json_value(&Value::Real(1767226500.0)), "1767226500.0");
    assert_eq!(json_value(&Value::Integer(900)), "900");
    assert_eq!(json_value(&Value::Null), "null");
    assert_eq!(json_value(&text("é")), "\"\\u00e9\"");
}
