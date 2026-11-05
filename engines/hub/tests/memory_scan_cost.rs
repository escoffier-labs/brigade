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
