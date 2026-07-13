use std::{collections::HashMap, hint::black_box, path::Path, time::Instant};

use graphtrail::{
    model::SearchRow,
    query::{build_context_pack_from_entry_points, personalize_context_pack},
    store::init_schema,
};
use rusqlite::{Connection, params};
use serde::Deserialize;
use serde_json::json;

const CORPUS: &str = include_str!("../benchmarks/context-ranking/corpus.json");

#[derive(Deserialize)]
struct Corpus {
    schema_version: u32,
    thresholds: Thresholds,
    cases: Vec<Case>,
}

#[derive(Deserialize)]
struct Thresholds {
    minimum_cases: usize,
    minimum_personalized_mrr: f64,
    minimum_mrr_gain: f64,
    maximum_personalized_p95_us: u128,
    maximum_p95_overhead_us: u128,
    warmup_iterations: usize,
    measured_iterations: usize,
}

#[derive(Deserialize)]
struct Case {
    id: String,
    task: String,
    nodes: Vec<Node>,
    edges: Vec<[String; 2]>,
    entry_points: Vec<String>,
    relevant_file: String,
}

#[derive(Deserialize)]
struct Node {
    id: String,
    path: String,
}

fn load_corpus() -> Corpus {
    serde_json::from_str(CORPUS).expect("benchmark corpus must be valid JSON")
}

fn case_connection(case: &Case) -> Connection {
    let conn = Connection::open_in_memory().unwrap();
    init_schema(&conn).unwrap();
    for node in &case.nodes {
        conn.execute(
            "INSERT INTO files(path, content_hash, size, modified_at, indexed_at, language)
             VALUES (?1, 'fixture', 1, 1, 1, 'rust')",
            params![node.path],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO symbols(id, kind, name, qualified_name, file_path, start_line, end_line, signature, content_hash)
             VALUES (?1, 'function', ?1, ?1, ?2, 1, 1, ?1, 'fixture')",
            params![node.id, node.path],
        )
        .unwrap();
    }
    for [source, target] in &case.edges {
        conn.execute(
            "INSERT INTO edges(source, target, kind, line) VALUES (?1, ?2, 'calls', 1)",
            params![source, target],
        )
        .unwrap();
    }
    conn
}

fn entry_points(case: &Case) -> Vec<SearchRow> {
    let nodes: HashMap<_, _> = case
        .nodes
        .iter()
        .map(|node| (node.id.as_str(), node.path.as_str()))
        .collect();
    case.entry_points
        .iter()
        .enumerate()
        .map(|(index, id)| SearchRow {
            id: id.clone(),
            kind: "function".to_string(),
            name: id.clone(),
            qualified_name: id.clone(),
            file_path: nodes[id.as_str()].to_string(),
            start_line: 1,
            end_line: 1,
            signature: id.clone(),
            score: 1.0 / (index + 1) as f64,
        })
        .collect()
}

fn reciprocal_rank(files: &[String], relevant: &str) -> f64 {
    files
        .iter()
        .position(|path| path == relevant)
        .map_or(0.0, |index| 1.0 / (index + 1) as f64)
}

fn p95(samples: &mut [u128]) -> u128 {
    samples.sort_unstable();
    samples[(samples.len() * 95).div_ceil(100).saturating_sub(1)]
}

#[test]
fn corpus_is_deterministic_and_privacy_safe() {
    let corpus = load_corpus();
    assert_eq!(corpus.schema_version, 1);
    assert!(corpus.cases.len() >= corpus.thresholds.minimum_cases);
    assert!(corpus.thresholds.measured_iterations >= 20);

    for case in &corpus.cases {
        assert!(!case.id.is_empty());
        assert!(!case.nodes.is_empty());
        assert!(!case.entry_points.is_empty());
        assert!(
            case.nodes
                .iter()
                .any(|node| node.path == case.relevant_file)
        );
        for node in &case.nodes {
            let path = Path::new(&node.path);
            assert!(path.is_relative(), "{} contains an absolute path", case.id);
            assert!(
                !node.path.contains(".."),
                "{} escapes the corpus root",
                case.id
            );
        }
    }
}

#[test]
fn personalized_ranking_meets_relevance_and_latency_thresholds() {
    let corpus = load_corpus();
    let mut baseline_reciprocal_ranks = Vec::new();
    let mut personalized_reciprocal_ranks = Vec::new();
    let mut baseline_latencies = Vec::new();
    let mut personalized_latencies = Vec::new();
    let mut case_results = Vec::new();

    for case in &corpus.cases {
        let conn = case_connection(case);
        let entries = entry_points(case);
        let baseline =
            build_context_pack_from_entry_points(&conn, case.task.clone(), entries.clone())
                .unwrap();
        let mut personalized =
            build_context_pack_from_entry_points(&conn, case.task.clone(), entries.clone())
                .unwrap();
        personalize_context_pack(&conn, &mut personalized).unwrap();

        let baseline_rr = reciprocal_rank(&baseline.related_files, &case.relevant_file);
        let personalized_rr = reciprocal_rank(&personalized.related_files, &case.relevant_file);
        baseline_reciprocal_ranks.push(baseline_rr);
        personalized_reciprocal_ranks.push(personalized_rr);
        case_results.push(json!({
            "id": case.id,
            "baseline_rank": if baseline_rr == 0.0 { None } else { Some((1.0 / baseline_rr) as usize) },
            "personalized_rank": if personalized_rr == 0.0 { None } else { Some((1.0 / personalized_rr) as usize) }
        }));

        for _ in 0..corpus.thresholds.warmup_iterations {
            let mut ranked =
                build_context_pack_from_entry_points(&conn, case.task.clone(), entries.clone())
                    .unwrap();
            personalize_context_pack(&conn, &mut ranked).unwrap();
            black_box(ranked);
        }
        for _ in 0..corpus.thresholds.measured_iterations {
            let started = Instant::now();
            let baseline =
                build_context_pack_from_entry_points(&conn, case.task.clone(), entries.clone())
                    .unwrap();
            baseline_latencies.push(started.elapsed().as_micros());
            black_box(&baseline);

            let started = Instant::now();
            let mut personalized =
                build_context_pack_from_entry_points(&conn, case.task.clone(), entries.clone())
                    .unwrap();
            personalize_context_pack(&conn, &mut personalized).unwrap();
            personalized_latencies.push(started.elapsed().as_micros());
            black_box(personalized);
        }
    }

    let mean = |values: &[f64]| values.iter().sum::<f64>() / values.len() as f64;
    let baseline_mrr = mean(&baseline_reciprocal_ranks);
    let personalized_mrr = mean(&personalized_reciprocal_ranks);
    let mrr_gain = personalized_mrr - baseline_mrr;
    let baseline_p95_us = p95(&mut baseline_latencies);
    let personalized_p95_us = p95(&mut personalized_latencies);
    let p95_overhead_us = personalized_p95_us.saturating_sub(baseline_p95_us);

    println!(
        "{}",
        serde_json::to_string_pretty(&json!({
            "schema_version": corpus.schema_version,
            "cases": case_results,
            "relevance": {
                "baseline_mrr": baseline_mrr,
                "personalized_mrr": personalized_mrr,
                "mrr_gain": mrr_gain
            },
            "latency": {
                "samples_per_mode": baseline_latencies.len(),
                "baseline_p95_us": baseline_p95_us,
                "personalized_p95_us": personalized_p95_us,
                "p95_overhead_us": p95_overhead_us
            }
        }))
        .unwrap()
    );

    assert!(
        personalized_mrr >= corpus.thresholds.minimum_personalized_mrr,
        "personalized MRR {personalized_mrr:.3} is below {:.3}",
        corpus.thresholds.minimum_personalized_mrr
    );
    assert!(
        mrr_gain >= corpus.thresholds.minimum_mrr_gain,
        "MRR gain {mrr_gain:.3} is below {:.3}",
        corpus.thresholds.minimum_mrr_gain
    );
    assert!(
        personalized_p95_us <= corpus.thresholds.maximum_personalized_p95_us,
        "personalized p95 {personalized_p95_us}us exceeds {}us",
        corpus.thresholds.maximum_personalized_p95_us
    );
    assert!(
        p95_overhead_us <= corpus.thresholds.maximum_p95_overhead_us,
        "p95 overhead {p95_overhead_us}us exceeds {}us",
        corpus.thresholds.maximum_p95_overhead_us
    );
}
