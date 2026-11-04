use brigade_hub::shadow::{Class, classify, compare};
use serde_json::json;
#[test]
fn definite_without_commit_even_when_injected() {
    assert_eq!(classify(&[(false, false)]), Class::Definite);
}
#[test]
fn transient_and_inconclusive_require_commits() {
    assert_eq!(classify(&[(false, true), (true, false)]), Class::Transient);
    assert_eq!(classify(&[(false, true); 3]), Class::Inconclusive);
    assert_eq!(classify(&[(false, true), (false, false)]), Class::Definite);
}
#[test]
fn boundary_requires_both_budget_flags_and_equal_common_prefix() {
    use brigade_hub::shadow::{boundary, compare_boundary};
    assert_eq!(
        compare_boundary(&[json!(1)], &[json!(1), json!(2)], true),
        Class::Boundary
    );
    assert!(boundary(&[json!(1)], &[json!(1), json!(2)], true));
    assert!(!boundary(&[json!(1)], &[json!(1), json!(2)], false));
    assert!(!boundary(&[json!(3)], &[json!(1), json!(2)], true));
}
#[test]
fn projection_requires_eligibility_and_exact_tombstone() {
    use brigade_hub::shadow::{compare_projection, projection};
    assert_eq!(
        compare_projection(
            &json!({"stored":true}),
            &json!({"tombstone":true}),
            &json!({"tombstone":true}),
            true
        ),
        Class::Projection
    );
    let projected =
        json!({"id":1,"content_state":"content_expired","envelope":{"ciphertext":null}});
    assert!(projection(&projected, &projected, true));
    assert!(!projection(&projected, &projected, false));
    assert!(!projection(&json!({"id":2}), &projected, true));
}
#[test]
fn stored_values_are_exact_and_record_contains_only_digests() {
    use brigade_hub::shadow::record;
    assert_eq!(
        compare("health", &json!({"ok":true}), &json!({"ok":false}), 100.0).1,
        "/ok"
    );
    assert_eq!(
        compare(
            "claims",
            &json!({"generation":1}),
            &json!({"generation":2}),
            100.0
        )
        .0,
        Class::Definite
    );
    let text = record(
        "/health",
        "",
        (200, 200),
        Class::Definite,
        "/ok",
        (b"private body", b"other"),
    )
    .to_string();
    assert!(!text.contains("private body"));
}

#[test]
fn generated_ids_preserve_references_and_clocks_have_bounded_tolerance() {
    use brigade_hub::shadow::compare_rules;
    let rules = json!({"generated_ids":[["/new_id","/ref"]],"clock_seconds":["/remaining"]});
    let a = json!({"new_id":"a","ref":"a","remaining":5.0});
    let b = json!({"new_id":"b","ref":"b","remaining":5.5});
    assert_eq!(compare_rules(&rules, &a, &b, 0.6).0, Class::Match);
    assert_eq!(compare_rules(&rules, &a, &b, 0.4).0, Class::Definite);
    assert_eq!(
        compare_rules(
            &rules,
            &a,
            &json!({"new_id":"b","ref":"c","remaining":5.0}),
            0.6
        )
        .0,
        Class::Definite
    );
}
#[test]
fn query_only_shadow_connection_refuses_writes() {
    let c = rusqlite::Connection::open_in_memory().unwrap();
    c.execute_batch("CREATE TABLE t(x); PRAGMA query_only=ON;")
        .unwrap();
    assert!(c.execute("INSERT INTO t VALUES (1)", []).is_err());
}
