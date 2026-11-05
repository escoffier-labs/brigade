use std::time::Instant;

#[test]
fn s18_round3_nested_path_error_code() {
    let bundle: serde_json::Value =
        serde_json::from_str(include_str!("../../../src/brigade/guard/memory-rules.json")).unwrap();
    let rule = bundle["rules"]
        .as_array()
        .unwrap()
        .iter()
        .find(|r| r["id"] == "windows-home-path")
        .unwrap();
    let pattern = format!(
        "(*LIMIT_MATCH=8388608){}",
        rule["pattern"].as_str().unwrap()
    );
    let text = format!("C:\\Users\\{}!", "a/".repeat(1000));
    for utf in [false, true] {
        let old = pcre2::bytes::RegexBuilder::new()
            .caseless(true)
            .utf(utf)
            .ucp(utf)
            .jit(true)
            .build(&pattern)
            .unwrap();
        assert_eq!(old.is_match(text.as_bytes()).unwrap_err().code(), -46);
        for run in 1..=2 {
            let regex = brigade_guard::compile(&pattern, 2, utf).unwrap();
            let start = Instant::now();
            let result = regex.is_match(text.as_bytes());
            println!(
                "nested-path utf={utf} run={run} seconds={:.6} result={result:?}",
                start.elapsed().as_secs_f64()
            );
            assert!(result.unwrap());
        }
    }
}

#[test]
fn s18_round3_stack_ceiling_is_explicit() {
    let pattern = "(*LIMIT_MATCH=8388608)(*NO_START_OPT)(*NO_AUTO_POSSESS)(a|b)+$";
    let regex = brigade_guard::compile(pattern, 0, false).unwrap();
    let text = format!("{}!", "a".repeat(2 * 1024 * 1024));
    assert_eq!(regex.is_match(text.as_bytes()).unwrap_err().code(), -46);
}
