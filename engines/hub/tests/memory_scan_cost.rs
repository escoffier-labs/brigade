//! Reproducible S18 scan cost, outside storage and lock-wait overhead.
#![cfg(feature = "memory-scan")]
use brigade_hub::memory::scan;
use std::time::Instant;

/// The verdict a hit on this bundle rule alone produces.
fn rule_state(rule: &str) -> &'static str {
    let bundle: serde_json::Value =
        serde_json::from_str(include_str!("../../../src/brigade/guard/memory-rules.json")).unwrap();
    let action = bundle["rules"]
        .as_array()
        .unwrap()
        .iter()
        .chain(bundle["injections"].as_array().unwrap())
        .find(|r| r["id"] == rule)
        .map(|r| r["action"].as_str().unwrap().to_string());
    match action.as_deref() {
        Some("block") => "blocked",
        Some("private") => "private",
        _ => "flagged",
    }
}

#[test]
#[ignore = "manual 512 KiB scan benchmark"]
fn memory_scan_cost() {
    let scanner = scan::bundled();
    assert!(scanner.describe().0);
    // Long near-matches exercise the base64 lookaheads, unanchored email and
    // Windows path rules. Each case is one 512 KiB line, with no finding prefix.
    for (name, unit) in [
        ("letter-run", "a"),
        ("windows-near-match", "C:\\Users\\example "), // <!-- content-guard: allow windows-home-path -->
        (
            "decode-near-match",
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa decode!",
        ),
    ] {
        let text = unit.repeat((512 * 1024 / unit.len()) + 1);
        let text = &text[..512 * 1024];
        for run in 1..=2 {
            let start = Instant::now();
            let result = scanner.scan(text);
            println!(
                "{name} run={run} bytes={} seconds={:.6} state={}",
                text.len(),
                start.elapsed().as_secs_f64(),
                result.state()
            );
        }
    }
}

fn corpus() -> serde_json::Value {
    serde_json::from_str(include_str!(
        "../../../tests/fixtures/memory-scan-corpus.json"
    ))
    .unwrap()
}

#[test]
fn s18_round2_supported_corpus_is_clean() {
    let scanner = scan::bundled();
    for size in [128 * 1024, 512 * 1024] {
        for (name, unit) in corpus().as_object().unwrap() {
            let unit = unit.as_str().unwrap();
            let text = unit.repeat(size / unit.len() + 1);
            let result = scanner.scan(&text[..size]);
            assert_eq!(result.state(), "clean", "{name} bytes={size}");
        }
    }
}

#[test]
#[ignore = "manual shared corpus scan benchmark"]
fn s18_round2_corpus_cost() {
    let scanner = scan::bundled();
    for (name, unit) in corpus().as_object().unwrap() {
        let unit = unit.as_str().unwrap();
        let size = 512 * 1024;
        let text = unit.repeat(size / unit.len() + 1);
        for run in 1..=2 {
            let start = Instant::now();
            let result = scanner.scan(&text[..size]);
            println!(
                "{name} run={run} bytes={size} seconds={:.6} state={}",
                start.elapsed().as_secs_f64(),
                result.state()
            );
        }
    }
}

#[test]
fn s18_round2_salutation_gap_is_bounded() {
    let scanner = scan::bundled();
    assert_eq!(
        scanner
            .scan(&format!("dear assistant{} override", " x".repeat(200)))
            .state(),
        "clean"
    );
    assert_eq!(
        scanner
            .scan("dear assistant hello\nplease override")
            .state(),
        "flagged"
    );
    assert_eq!(
        scanner
            .scan(&format!("dear assistant {} override", "x".repeat(254)))
            .state(),
        "flagged"
    );
    assert_eq!(
        scanner
            .scan(&format!("dear assistant {} override", "x".repeat(255)))
            .state(),
        "clean"
    );
}

#[test]
fn s18_round3_nested_path_is_private() {
    let scanner = scan::bundled();
    let text = format!("C:\\Users\\{}!", "a/".repeat(1000)); // <!-- content-guard: allow windows-home-path -->
    for run in 1..=2 {
        let start = Instant::now();
        let result = scanner.scan(&text);
        println!(
            "nested-path scan run={run} seconds={:.6} state={}",
            start.elapsed().as_secs_f64(),
            result.state()
        );
        assert_eq!(result.state(), "private");
        assert!(result.rules().iter().any(|id| id == "windows-home-path"));
    }
}

#[test]
fn s18_round3_repeated_groups() {
    let scanner = scan::bundled();
    let corpus: serde_json::Value = serde_json::from_str(include_str!(
        "../../../tests/fixtures/memory-scan-groups.json"
    ))
    .unwrap();
    for size in [128 * 1024, 512 * 1024] {
        for (name, case) in corpus.as_object().unwrap() {
            let prefix: String = case["prefix_parts"]
                .as_array()
                .unwrap()
                .iter()
                .map(|part| part.as_str().unwrap())
                .collect();
            let unit = case["unit"].as_str().unwrap();
            let text = format!("{prefix}{}", unit.repeat(size / unit.len() + 1));
            let result = scanner.scan(&text[..size]);
            if let Some(rule) = case["rule"].as_str() {
                assert_eq!(result.state(), rule_state(rule), "{name} bytes={size}");
                assert!(
                    result.rules().iter().any(|id| id == rule),
                    "{name}: {result:?}"
                );
            } else {
                assert_eq!(result.state(), "clean", "{name} bytes={size}");
            }
        }
    }
}

fn round4_vectors(case: &serde_json::Value, size: usize) -> Vec<(String, String, Option<String>)> {
    let prefix: String = case["prefix_parts"]
        .as_array()
        .unwrap()
        .iter()
        .map(|p| p.as_str().unwrap())
        .collect();
    let literal = case["literal"].as_str().unwrap();
    let unit = case["unit"].as_str().unwrap();
    let mut vectors = Vec::new();
    for folded in [false, true] {
        let length = size / if folded { 3 } else { 1 };
        let run = unit.repeat(length / unit.len() + 1);
        let run = &run[..length - prefix.len() - literal.len() - 3];
        for (position, mut text) in [
            ("before", format!("{literal}! {prefix}{run}!")),
            ("after", format!("{prefix}{run}{literal}!!!")),
            ("isolated", format!("{prefix}{run}! {literal}!")),
        ] {
            if folded {
                text = text
                    .chars()
                    .map(|c| match c {
                        '!'..='~' => char::from_u32(c as u32 + 0xFEE0).unwrap(),
                        ' ' => '\u{3000}',
                        _ => c,
                    })
                    .collect();
                text.push_str(&"!".repeat(size - text.len()));
            }
            let expected = case["expected"][position].as_str().map(String::from);
            vectors.push((
                format!("{position}{}", if folded { "-fullwidth" } else { "" }),
                text,
                expected,
            ));
        }
    }
    vectors
}

#[test]
fn s18_round4_adversarial_rule_corpus() {
    let scanner = scan::bundled();
    assert!(scanner.describe().0);
    let corpus: serde_json::Value = serde_json::from_str(include_str!(
        "../../../tests/fixtures/memory-scan-adversarial.json"
    ))
    .unwrap();
    let mut vectors = Vec::new();
    for size in [128 * 1024, 512 * 1024] {
        for (name, case) in corpus.as_object().unwrap() {
            for (position, text, expected) in round4_vectors(case, size) {
                assert_eq!(text.len(), size);
                vectors.push((format!("{name}/{position}"), text, expected));
            }
        }
    }
    vectors.push((
        "codex-exact".into(),
        format!("{} @", "ｌａｂｅｌ．".repeat(8000)),
        None,
    ));
    vectors.push((
        "claude-exact".into(),
        format!("{}@", "label.".repeat(87_000)),
        None,
    ));
    // PCRE2 UCP does not classify U+001F as whitespace. Unlike Python,
    // its negative colon assertion excludes this address in both modes.
    vectors.push(("ascii-email-control".into(), "a@b.co:\u{1f}x".into(), None));
    vectors.push((
        "ascii-bearer-control".into(),
        format!("Bearer {}\u{1f}{}", "a".repeat(10), "a".repeat(10)),
        None,
    ));
    for (name, text, expected) in vectors {
        for run in 1..=2 {
            let start = Instant::now();
            let result = scanner.scan(&text);
            let elapsed = start.elapsed().as_secs_f64();
            println!(
                "round4 {name} run={run} bytes={} seconds={elapsed:.6} state={}",
                text.len(),
                result.state()
            );
            assert!(elapsed < 5.0, "{name} exceeded five seconds");
            if let Some(rule) = &expected {
                assert!(
                    matches!(result.state(), "private" | "flagged" | "blocked"),
                    "{name}: {result:?}"
                );
                assert!(result.rules().contains(rule), "{name}: {result:?}");
            } else {
                assert_eq!(result.state(), "clean", "{name}: {result:?}");
            }
        }
    }
}

#[test]
fn s18_round5_overbound_secrets_block() {
    let scanner = scan::bundled();
    let cases: serde_json::Value = serde_json::from_str(include_str!(
        "../../../tests/fixtures/memory-scan-overbound.json"
    ))
    .unwrap();
    for (name, case) in cases.as_object().unwrap() {
        let parts = |key: &str| -> String {
            case[key]
                .as_array()
                .unwrap()
                .iter()
                .map(|p| p.as_str().unwrap())
                .collect()
        };
        let text = format!(
            "{}{}{}",
            parts("prefix_parts"),
            case["unit"]
                .as_str()
                .unwrap()
                .repeat(case["repeat"].as_u64().unwrap() as usize),
            parts("suffix_parts")
        );
        assert_eq!(scanner.scan(&text).state(), "blocked", "{name}");
    }
}
