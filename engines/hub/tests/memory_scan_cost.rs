//! Reproducible S18 scan cost, outside storage and lock-wait overhead.
#![cfg(feature = "memory-scan")]
use brigade_hub::memory::scan;
use std::time::Instant;

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
fn s18_round3_nested_path_is_flagged() {
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
        assert_eq!(result.state(), "flagged");
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
                assert_eq!(result.state(), "flagged", "{name} bytes={size}");
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
