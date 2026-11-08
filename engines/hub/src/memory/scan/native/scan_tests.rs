use super::*;

/// Source pins cover every class and fold, and also catch edits to
/// this scanner when only the Rust CI jobs run.
#[test]
fn scanner_sources_match_the_bundle() {
    let bundle: Value = serde_json::from_str(include_str!(
        "../../../../../../src/brigade/guard/memory-rules.json"
    ))
    .unwrap();
    for (name, source) in [
        (
            "python",
            include_str!("../../../../../../src/brigade/memory_content_scan.py"),
        ),
        ("rust", include_str!("../../scan.rs")),
    ] {
        assert_eq!(
            super::super::super::ids::sha256_hex(source.replace("\r\n", "\n").as_bytes()),
            bundle["scanner_sources"][name].as_str().unwrap(),
            "{name} scanner changed: run python scripts/memory_scan_bundle.py"
        );
    }
}

fn nfkc_fixture() -> Value {
    serde_json::from_str(include_str!(
        "../../../../../../tests/fixtures/memory-scan-nfkc.json"
    ))
    .unwrap()
}

/// The crate's Unicode data is part of the scan verdict, so a bump
/// is a reviewed change: rerun the parity tests and bump the bundle.
#[test]
fn nfkc_crate_unicode_version_is_the_recorded_one() {
    assert_eq!(unicode_normalization::UNICODE_VERSION, (18, 0, 0));
}

fn hex(bytes: &[u8]) -> String {
    use std::fmt::Write;
    bytes.iter().fold(String::new(), |mut out, b| {
        write!(out, "{b:02x}").unwrap();
        out
    })
}

#[test]
fn nfkc_matches_python_on_every_code_point() {
    use sha2::{Digest, Sha256};
    let guard = Guard::new().unwrap();
    let fixture = nfkc_fixture();
    assert_eq!(fixture["unicode"], "14.0.0");
    let mut digest = Sha256::new();
    let mut changed = 0;
    for code in 0..=0x10FFFFu32 {
        let Some(c) = char::from_u32(code) else {
            continue;
        };
        let text = c.to_string();
        let out = nfkc(&guard.unassigned, &text);
        if out != text {
            digest.update(format!("{code:X}:{}\n", hex(out.as_bytes())));
            changed += 1;
        }
    }
    assert_eq!(changed, fixture["changed_code_points"].as_u64().unwrap());
    assert_eq!(
        hex(&digest.finalize()),
        fixture["changed_sha256"].as_str().unwrap()
    );
}

#[test]
fn scan_copy_matches_python_for_compatibility_sequences() {
    let guard = Guard::new().unwrap();
    for pair in nfkc_fixture()["lines"].as_array().unwrap() {
        let input = pair[0].as_str().unwrap();
        let lines = nfkc_lines(&guard.unassigned, input);
        assert_eq!(lines, pair[1].as_str().unwrap(), "{input:?}");
    }
}

#[test]
fn bundle_with_another_unicode_version_is_refused() {
    let source = include_str!("../../../../../../src/brigade/guard/memory-rules.json");
    assert!(Guard::from_bundle(source).is_some());
    for version in ["15.0.0", "14.0", ""] {
        let edited = source.replacen(
            "\"unicode\": \"14.0.0\"",
            &format!("\"unicode\": \"{version}\""),
            1,
        );
        assert_ne!(edited, source, "bundle spelling of the pin changed");
        assert!(Guard::from_bundle(&edited).is_none(), "{version}");
    }
}

#[test]
fn unassigned_table_must_be_sorted_and_disjoint() {
    assert!(unassigned_table("5-9,B-C").is_some());
    assert!(unassigned_table("9-5").is_none());
    assert!(unassigned_table("5-9,9-C").is_none());
    assert!(unassigned_table("B-C,5-9").is_none());
    assert!(unassigned_table("5-").is_none());
    assert!(unassigned_table("").is_none());
}

fn error_guard() -> (Guard, String) {
    let mut guard = Guard::new().unwrap();
    let pattern = "(*LIMIT_MATCH=1)(*NO_START_OPT)(*NO_AUTO_POSSESS)(a+)+$";
    let utf = brigade_guard::compile(pattern, 0, true).unwrap();
    let ascii = brigade_guard::compile(pattern, 0, false).unwrap();
    let text = format!("{}!", "a".repeat(100));
    assert!(utf.is_match(text.as_bytes()).is_err());
    guard.injections.clear();
    guard.rules = vec![Rule {
        id: "test-limit".into(),
        class: Class::Warn,
        utf,
        ascii,
    }];
    (guard, text)
}

#[test]
fn s18_round2_production_limit_still_fails_closed() {
    let mut guard = Guard::new().unwrap();
    guard.injections.clear();
    guard.rules = rules(&[serde_json::json!({
        "id": "pathological", "action": "warn", "flags": 0,
        "pattern": "(*NO_START_OPT)(*NO_AUTO_POSSESS)(a+)+$"
    })])
    .unwrap();
    let text = format!("{}!", "a".repeat(100));
    assert_eq!(
        guard.rules[0]
            .utf
            .is_match(text.as_bytes())
            .unwrap_err()
            .code(),
        -47
    );
    for run in 1..=2 {
        let start = std::time::Instant::now();
        assert_eq!(guard.scan(&text), Scan::Failed);
        println!(
            "match-limit run={run} seconds={:.6}",
            start.elapsed().as_secs_f64()
        );
    }
}

#[test]
fn s18_review_match_error_preserves_blocks() {
    let (guard, text) = error_guard();
    assert!(matches!(
        guard.scan(&format!("\u{202e}{text}")),
        Scan::Blocked { .. }
    ));
}

#[test]
fn s18_review_match_error_is_distinct_from_missing_engine() {
    let (guard, text) = error_guard();
    let failed = guard.scan(&text);
    assert_eq!(failed.state(), "failed");
    assert!(!failed.permits_raise());
}

#[tokio::test]
async fn s18_review_match_error_refuses_every_write_without_storage() {
    use crate::memory::tests::{ADMIN_TOKEN, Hub};
    let (guard, text) = error_guard();
    let h = Hub::with(std::sync::Arc::new(guard), None);
    for action in ["put", "import", "handoff"] {
        let mut body = serde_json::json!({"action":action, "text":text, "client_event_id":h.id()});
        if action == "handoff" {
            body["kind"] = serde_json::json!("note");
        } else {
            body["scope"] = serde_json::json!("fleet");
            body["card_id"] = serde_json::json!("failed");
        }
        let r = h.call(ADMIN_TOKEN, body).await;
        assert_eq!(
            (r.status, r.body["code"].as_str()),
            (503, Some("memory_scan_failed")),
            "{action}: {:?}",
            r.body
        );
        assert_eq!(r.body["retryable"], false, "{action}: {:?}", r.body);
        assert!(
            r.body["error"]
                .as_str()
                .unwrap()
                .contains("retrying will not help")
        );
    }
    let c = rusqlite::Connection::open(h.memory()).unwrap();
    for table in [
        "memory_blobs",
        "memory_scans",
        "memory_events",
        "memory_idempotency",
    ] {
        assert_eq!(
            c.query_row(&format!("SELECT count(*) FROM {table}"), [], |r| r
                .get::<_, i64>(0))
                .unwrap(),
            0
        );
    }
}
