use std::{collections::HashSet, fs, path::Path, process::Command, time::Instant};

use serde::Deserialize;
use serde_json::Value;

#[derive(Deserialize)]
struct Corpus {
    corpus_schema_version: u32,
    result_schema_version: u32,
    response_budget_chars: usize,
    languages: Vec<String>,
    cases: Vec<Case>,
}

#[derive(Deserialize)]
struct Case {
    id: String,
    task: String,
    fixture_variant: String,
    behavior: String,
    graphtrail_mode: String,
    #[serde(default)]
    changed_files: Vec<String>,
    expected_files: Vec<String>,
    expected_tokens: Vec<String>,
}

fn repository_path(path: &str) -> std::path::PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join(path)
}

fn python() -> &'static str {
    if cfg!(windows) { "python" } else { "python3" }
}

#[test]
fn cross_tool_corpus_is_versioned_public_and_complete() {
    let corpus_path = repository_path("benchmarks/cross-tool/corpus.v1.json");
    let corpus: Corpus = serde_json::from_str(
        &fs::read_to_string(&corpus_path)
            .unwrap_or_else(|error| panic!("{} must exist: {error}", corpus_path.display())),
    )
    .expect("cross-tool corpus must be valid JSON");

    assert_eq!(corpus.corpus_schema_version, 1);
    assert_eq!(corpus.result_schema_version, 1);
    assert!(corpus.response_budget_chars >= 1_000);
    assert_eq!(
        corpus.languages.into_iter().collect::<HashSet<_>>(),
        HashSet::from([
            "go".to_string(),
            "python".to_string(),
            "rust".to_string(),
            "typescript".to_string(),
        ])
    );

    let behaviors: HashSet<_> = corpus
        .cases
        .iter()
        .map(|case| case.behavior.as_str())
        .collect();
    for behavior in [
        "branch_drift",
        "disconnected_named_file",
        "high_degree_hub",
        "test_attribution",
        "unresolved_reference",
    ] {
        assert!(behaviors.contains(behavior), "missing {behavior} case");
    }

    for case in &corpus.cases {
        assert!(!case.id.trim().is_empty());
        assert!(!case.task.trim().is_empty());
        assert!(matches!(case.fixture_variant.as_str(), "base" | "drift"));
        assert!(
            matches!(
                case.graphtrail_mode.as_str(),
                "context" | "affected" | "doctor"
            ),
            "{} uses an unsupported graphtrail mode",
            case.id
        );
        assert_eq!(
            case.graphtrail_mode == "affected",
            !case.changed_files.is_empty(),
            "{} must provide changed_files only for affected mode",
            case.id
        );
        assert!(
            !case.expected_files.is_empty() || !case.expected_tokens.is_empty(),
            "{} has no correctness labels",
            case.id
        );
        for file in &case.expected_files {
            let path = Path::new(file);
            assert!(path.is_relative(), "{} uses an absolute path", case.id);
            assert!(!file.contains(".."), "{} escapes the fixture", case.id);
            assert!(
                repository_path(&format!(
                    "benchmarks/cross-tool/fixtures/polyglot/base/{file}"
                ))
                .is_file()
                    || repository_path(&format!(
                        "benchmarks/cross-tool/fixtures/polyglot/drift/{file}"
                    ))
                    .is_file(),
                "{} expects missing fixture file {file}",
                case.id
            );
        }
        let task = case.task.to_lowercase();
        let disclosed_setup = format!(
            "{} {} {} {}",
            case.id,
            case.behavior,
            case.fixture_variant,
            if case.fixture_variant == "drift" {
                "main drift"
            } else {
                ""
            }
        )
        .to_lowercase();
        for label in case.expected_files.iter().chain(&case.expected_tokens) {
            assert!(
                !task.contains(&label.to_lowercase())
                    && !disclosed_setup.contains(&label.to_lowercase()),
                "{} leaks correctness label {label:?} in disclosed inputs",
                case.id
            );
        }
    }
}

#[test]
fn cross_tool_runner_executes_the_adapter_protocol_without_answer_keys() {
    let output = tempfile::NamedTempFile::new().expect("result file must be creatable");
    let status = Command::new(python())
        .arg(repository_path("benchmarks/cross-tool/run.py"))
        .args(["--graphtrail", env!("CARGO_BIN_EXE_graphtrail")])
        .arg("--comparator")
        .arg(format!(
            "fixture={} \"{}\"",
            python(),
            repository_path("tests/fixtures/cross_tool_comparator.py").display()
        ))
        .args(["--output", output.path().to_str().unwrap()])
        .status()
        .expect("runner must execute");
    assert!(status.success(), "runner must execute both implementations");

    let report: Value = serde_json::from_str(
        &fs::read_to_string(output.path()).expect("runner must write its report"),
    )
    .expect("runner report must be valid JSON");
    let results = report["results"]
        .as_array()
        .expect("results must be an array");
    assert_eq!(results.len(), 12);
    assert!(
        results
            .iter()
            .all(|result| result["correctness"]["passed"] == true)
    );
    let graphtrail_drift = results
        .iter()
        .find(|result| {
            result["implementation"] == "graphtrail"
                && result["case_id"] == "typescript-branch-drift"
        })
        .expect("GraphTrail branch-drift result must be present");
    assert_eq!(graphtrail_drift["setup_steps"], 2);
    assert_eq!(graphtrail_drift["tool_calls"], 1);
}

#[test]
fn cross_tool_branch_drift_cannot_pass_by_echoing_the_request() {
    let output = tempfile::NamedTempFile::new().expect("result file must be creatable");
    let status = Command::new(python())
        .arg(repository_path("benchmarks/cross-tool/run.py"))
        .args(["--graphtrail", env!("CARGO_BIN_EXE_graphtrail")])
        .args(["--case", "typescript-branch-drift"])
        .arg("--comparator")
        .arg(format!(
            "echo={} \"{}\"",
            python(),
            repository_path("tests/fixtures/cross_tool_request_echo.py").display()
        ))
        .args(["--output", output.path().to_str().unwrap()])
        .status()
        .expect("runner must execute");
    assert!(
        !status.success(),
        "request echo must not satisfy drift correctness"
    );

    let report: Value = serde_json::from_str(
        &fs::read_to_string(output.path()).expect("runner must write its report"),
    )
    .expect("runner report must be valid JSON");
    let echo = report["results"]
        .as_array()
        .unwrap()
        .iter()
        .find(|result| result["implementation"] == "echo")
        .expect("echo comparator result must be present");
    assert_eq!(
        echo["status"], "completed",
        "echo adapter must actually launch"
    );
    assert_eq!(echo["correctness"]["passed"], false);
}

#[test]
fn comparator_parser_preserves_quoted_windows_paths() {
    let output = Command::new(python())
        .args([
            "-c",
            "import importlib.util, os, sys; sys.stdout.reconfigure(newline='\\r\\n'); spec = importlib.util.spec_from_file_location('cross_tool_runner', os.environ['CROSS_TOOL_RUNNER']); module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module); print('\\n'.join(module.parse_comparators([r'fixture=python \"C:\\work tree\\adapter.py\"'], windows=True)[0][1]))",
        ])
        .env(
            "CROSS_TOOL_RUNNER",
            repository_path("benchmarks/cross-tool/run.py"),
        )
        .output()
        .expect("parser probe must execute");
    assert!(
        output.status.success(),
        "parser probe failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert_eq!(
        String::from_utf8(output.stdout)
            .unwrap()
            .lines()
            .collect::<Vec<_>>(),
        vec!["python", "C:\\work tree\\adapter.py"]
    );
}

#[test]
fn cross_tool_runner_normalizes_comparator_paths() {
    let output = tempfile::NamedTempFile::new().expect("result file must be creatable");
    let status = Command::new(python())
        .arg(repository_path("benchmarks/cross-tool/run.py"))
        .args(["--graphtrail", env!("CARGO_BIN_EXE_graphtrail")])
        .args(["--case", "typescript-symbol-navigation"])
        .arg("--comparator")
        .arg(format!(
            "windows-paths={} \"{}\"",
            python(),
            repository_path("tests/fixtures/cross_tool_windows_paths.py").display()
        ))
        .args(["--output", output.path().to_str().unwrap()])
        .status()
        .expect("runner must execute");
    assert!(
        status.success(),
        "path separators must not change correctness"
    );
}

#[test]
fn measured_timeout_does_not_bound_fixture_setup() {
    let output = tempfile::NamedTempFile::new().expect("result file must be creatable");
    let status = Command::new(python())
        .arg(repository_path("benchmarks/cross-tool/run.py"))
        .args(["--graphtrail", env!("CARGO_BIN_EXE_graphtrail")])
        .args(["--case", "typescript-symbol-navigation"])
        .args(["--timeout-seconds", "0.000001"])
        .args(["--output", output.path().to_str().unwrap()])
        .status()
        .expect("runner must execute");
    assert!(
        !status.success(),
        "measured GraphTrail command must time out"
    );

    let report: Value = serde_json::from_str(
        &fs::read_to_string(output.path()).expect("timeout must still write a report"),
    )
    .expect("runner report must be valid JSON");
    assert_eq!(report["results"][0]["status"], "failed");
    assert!(
        report["results"][0]["error"]
            .as_str()
            .unwrap()
            .contains("timed out")
    );
}

#[test]
fn cross_tool_runner_times_out_hung_comparators() {
    let output = tempfile::NamedTempFile::new().expect("result file must be creatable");
    let started = Instant::now();
    let status = Command::new(python())
        .arg(repository_path("benchmarks/cross-tool/run.py"))
        .args(["--graphtrail", env!("CARGO_BIN_EXE_graphtrail")])
        .args(["--case", "typescript-symbol-navigation"])
        .args(["--timeout-seconds", "0.2"])
        .arg("--comparator")
        .arg(format!(
            "hung={} -c 'import subprocess,sys,time; subprocess.Popen([sys.executable,\"-c\",\"import time; time.sleep(10)\"]); time.sleep(60)'",
            python()
        ))
        .args(["--output", output.path().to_str().unwrap()])
        .status()
        .expect("runner must execute");
    assert!(
        !status.success(),
        "a timed-out comparator must fail the run"
    );
    assert!(
        started.elapsed().as_secs_f64() < 30.0,
        "runner must return after timeout"
    );

    let report: Value = serde_json::from_str(
        &fs::read_to_string(output.path()).expect("timeout must still write a report"),
    )
    .expect("runner report must be valid JSON");
    let timed_out = report["results"]
        .as_array()
        .unwrap()
        .iter()
        .find(|result| result["implementation"] == "hung")
        .expect("hung comparator result must be present");
    assert_eq!(timed_out["status"], "failed");
    assert!(timed_out["error"].as_str().unwrap().contains("timed out"));
    assert!(
        timed_out["elapsed_ms"].as_f64().unwrap() < 5_000.0,
        "measured timeout cleanup must remain bounded"
    );
}

#[test]
fn cross_tool_runner_preserves_the_measurement_contract() {
    let runner_path = repository_path("benchmarks/cross-tool/run.py");
    let runner = fs::read_to_string(&runner_path)
        .unwrap_or_else(|error| panic!("{} must exist: {error}", runner_path.display()));

    for required in [
        "--comparator",
        "--graphtrail",
        "response_budget_chars",
        "correctness",
        "setup_steps",
        "tool_calls",
        "response_chars",
        "elapsed_ms",
        "peak_memory_kib",
        "result_schema_version",
    ] {
        assert!(runner.contains(required), "runner must record {required}");
    }

    for path in [
        "benchmarks/cross-tool/README.md",
        "benchmarks/cross-tool/result-schema.v1.json",
        "benchmarks/cross-tool/fixtures/polyglot/base/python/service.py",
        "benchmarks/cross-tool/fixtures/polyglot/base/typescript/router.ts",
        "benchmarks/cross-tool/fixtures/polyglot/base/rust/src/lib.rs",
        "benchmarks/cross-tool/fixtures/polyglot/base/go/service.go",
        "tests/fixtures/cross_tool_comparator.py",
        "tests/fixtures/cross_tool_request_echo.py",
        "tests/fixtures/cross_tool_windows_paths.py",
    ] {
        assert!(
            repository_path(path).is_file(),
            "missing benchmark asset {path}"
        );
    }

    let schema: Value = serde_json::from_str(
        &fs::read_to_string(repository_path(
            "benchmarks/cross-tool/result-schema.v1.json",
        ))
        .expect("result schema must be readable"),
    )
    .expect("result schema must be valid JSON");
    assert_eq!(schema["properties"]["result_schema_version"]["const"], 1);
    let result_properties = &schema["properties"]["results"]["items"]["properties"];
    for metric in [
        "correctness",
        "setup_steps",
        "tool_calls",
        "response_chars",
        "elapsed_ms",
        "peak_memory_kib",
    ] {
        assert!(
            result_properties.get(metric).is_some(),
            "result schema must preserve {metric}"
        );
    }
}
