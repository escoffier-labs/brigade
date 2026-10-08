use std::{collections::HashMap, hint::black_box, path::Path, time::Instant};

use graphtrail::{
    model::SearchRow,
    query::{
        build_context_pack, build_context_pack_from_entry_points, personalize_context_pack,
        search::{NAME_COVERAGE_FLOOR, RELEVANCE_FLOOR_RULE, floored_search, search_symbols},
    },
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

// ---------------------------------------------------------------------------
// Relevance floor (brigade#1648): labeled search tasks, scored on entry points.
// ---------------------------------------------------------------------------

const FLOOR_CORPUS: &str = include_str!("../benchmarks/context-ranking/floor-corpus.json");

#[derive(Deserialize)]
struct FloorCorpus {
    schema_version: u32,
    limit: usize,
    cases: Vec<FloorCase>,
}

#[derive(Deserialize)]
struct FloorCase {
    id: String,
    task: String,
    nodes: Vec<FloorNode>,
    edges: Vec<[String; 2]>,
    relevant: Vec<String>,
    #[serde(default)]
    first: Option<String>,
}

#[derive(Deserialize)]
struct FloorNode {
    id: String,
    #[serde(default)]
    name: Option<String>,
    path: String,
}

fn load_floor_corpus() -> FloorCorpus {
    serde_json::from_str(FLOOR_CORPUS).expect("floor corpus must be valid JSON")
}

fn index_fixture(nodes: &[(String, String, String)], edges: &[[String; 2]]) -> Connection {
    let conn = Connection::open_in_memory().unwrap();
    init_schema(&conn).unwrap();
    for (id, name, path) in nodes {
        conn.execute(
            "INSERT OR IGNORE INTO files(path, content_hash, size, modified_at, indexed_at, language)
             VALUES (?1, 'fixture', 1, 1, 1, 'python')",
            params![path],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO symbols(id, kind, name, qualified_name, file_path, start_line, end_line, signature, content_hash)
             VALUES (?1, 'function', ?2, ?2, ?3, 1, 1, ?2, 'fixture')",
            params![id, name, path],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO symbols_fts(symbol_id, name, qualified_name, signature, file_path)
             VALUES (?1, ?2, ?2, ?2, ?3)",
            params![id, name, path],
        )
        .unwrap();
    }
    for [source, target] in edges {
        conn.execute(
            "INSERT INTO edges(source, target, kind, line) VALUES (?1, ?2, 'calls', 1)",
            params![source, target],
        )
        .unwrap();
    }
    conn
}

fn floor_case_connection(case: &FloorCase) -> Connection {
    let nodes: Vec<_> = case
        .nodes
        .iter()
        .map(|node| {
            let name = node.name.clone().unwrap_or_else(|| node.id.clone());
            (node.id.clone(), name, node.path.clone())
        })
        .collect();
    index_fixture(&nodes, &case.edges)
}

#[derive(Default, Clone, Copy)]
struct Tally {
    returned: usize,
    relevant: usize,
    hits: usize,
}

impl Tally {
    fn add(&mut self, returned: &[String], relevant: &[String]) {
        self.returned += returned.len();
        self.relevant += relevant.len();
        self.hits += returned.iter().filter(|id| relevant.contains(id)).count();
    }

    fn precision(&self) -> f64 {
        if self.returned == 0 {
            1.0
        } else {
            self.hits as f64 / self.returned as f64
        }
    }

    fn recall(&self) -> f64 {
        if self.relevant == 0 {
            1.0
        } else {
            self.hits as f64 / self.relevant as f64
        }
    }

    /// F-beta with beta 0.5, weighting precision over recall.
    fn f05(&self) -> f64 {
        let (p, r) = (self.precision(), self.recall());
        if p + r == 0.0 {
            0.0
        } else {
            1.25 * p * r / (0.25 * p + r)
        }
    }

    fn json(&self) -> serde_json::Value {
        json!({
            "returned": self.returned,
            "relevant": self.relevant,
            "hits": self.hits,
            "precision": self.precision(),
            "recall": self.recall(),
            "f05": self.f05(),
        })
    }
}

fn ids(rows: &[SearchRow]) -> Vec<String> {
    rows.iter().map(|row| row.id.clone()).collect()
}

#[test]
fn floor_corpus_is_synthetic_and_labeled() {
    let corpus = load_floor_corpus();
    assert_eq!(corpus.schema_version, 1);
    assert!(corpus.limit > 0);
    assert!(corpus.cases.iter().any(|case| case.relevant.is_empty()));
    assert!(corpus.cases.iter().any(|case| !case.relevant.is_empty()));
    for case in &corpus.cases {
        let node_ids: Vec<_> = case.nodes.iter().map(|node| node.id.as_str()).collect();
        let mut unique = node_ids.clone();
        unique.sort_unstable();
        unique.dedup();
        assert_eq!(
            unique.len(),
            node_ids.len(),
            "{} repeats a node id",
            case.id
        );
        for id in &case.relevant {
            assert!(
                node_ids.contains(&id.as_str()),
                "{} labels unknown {id}",
                case.id
            );
        }
        if let Some(first) = &case.first {
            assert!(
                case.relevant.contains(first),
                "{} first is unlabeled",
                case.id
            );
        }
        for node in &case.nodes {
            assert!(
                Path::new(&node.path).is_relative(),
                "{} absolute path",
                case.id
            );
            assert!(
                !node.path.contains(".."),
                "{} escapes the corpus root",
                case.id
            );
        }
    }
}

#[test]
fn relevance_floor_raises_precision_without_losing_recall() {
    let corpus = load_floor_corpus();
    let mut baseline = Tally::default();
    let mut floored = Tally::default();
    let mut cases = Vec::new();
    for case in &corpus.cases {
        let conn = floor_case_connection(case);
        let before = ids(&search_symbols(&conn, &case.task, corpus.limit).unwrap());
        let pack = build_context_pack(&conn, case.task.clone(), corpus.limit).unwrap();
        let after = ids(&pack.entry_points);
        baseline.add(&before, &case.relevant);
        floored.add(&after, &case.relevant);
        cases.push(json!({"id": case.id, "before": before, "after": after}));

        if case.relevant.is_empty() {
            assert!(
                !pack.confident,
                "{} should have no confident context",
                case.id
            );
            assert!(after.is_empty(), "{} kept {after:?}", case.id);
        } else {
            assert!(pack.confident, "{} lost its confident context", case.id);
        }
        if let Some(first) = &case.first {
            assert_eq!(after.first(), Some(first), "{} entry point 1", case.id);
        }
    }

    println!(
        "{}",
        serde_json::to_string_pretty(&json!({
            "floor": {
                "rule": RELEVANCE_FLOOR_RULE,
                "min_name_coverage": NAME_COVERAGE_FLOOR,
            },
            "baseline": baseline.json(),
            "floored": floored.json(),
            "cases": cases,
        }))
        .unwrap()
    );

    assert!(
        floored.recall() >= baseline.recall(),
        "floor lost recall: {:.3} < {:.3}",
        floored.recall(),
        baseline.recall()
    );
    assert!(
        floored.precision() > baseline.precision(),
        "floor did not raise precision: {:.3} <= {:.3}",
        floored.precision(),
        baseline.precision()
    );
}

#[test]
fn name_coverage_floor_matches_its_calibration_sweep() {
    let corpus = load_floor_corpus();
    let grid: Vec<f64> = (0..=20).map(|step| step as f64 / 20.0).collect();
    let mut sweep = Vec::new();
    for &floor in &grid {
        let mut tally = Tally::default();
        for case in &corpus.cases {
            let conn = floor_case_connection(case);
            let rows = floored_search(&conn, &case.task, corpus.limit, floor)
                .unwrap()
                .rows;
            tally.add(&ids(&rows), &case.relevant);
        }
        sweep.push((floor, tally));
    }
    let best_recall = sweep
        .iter()
        .map(|(_, tally)| tally.recall())
        .fold(0.0, f64::max);
    let full_recall = |tally: &Tally| (tally.recall() - best_recall).abs() < 1e-12;
    let best_f05 = sweep
        .iter()
        .filter(|(_, tally)| full_recall(tally))
        .map(|(_, tally)| tally.f05())
        .fold(0.0, f64::max);
    let optimal: Vec<f64> = sweep
        .iter()
        .filter(|(_, tally)| full_recall(tally) && (tally.f05() - best_f05).abs() < 1e-12)
        .map(|(floor, _)| *floor)
        .collect();
    let calibrated = optimal[0];

    println!(
        "{}",
        serde_json::to_string_pretty(&json!({
            "sweep": sweep
                .iter()
                .map(|(floor, tally)| json!({"min_name_coverage": floor, "score": tally.json()}))
                .collect::<Vec<_>>(),
            "optimal_band": optimal,
            "calibrated": calibrated,
        }))
        .unwrap()
    );

    // The most permissive floor that reaches the best precision-weighted score
    // with no recall loss. Change the corpus, not this constant, to move it.
    assert!(
        (NAME_COVERAGE_FLOOR - calibrated).abs() < 1e-12,
        "NAME_COVERAGE_FLOOR is {NAME_COVERAGE_FLOOR}, calibration picks {calibrated} (band {optimal:?})"
    );
}

#[test]
fn ranking_corpus_relevant_files_survive_the_floor() {
    let corpus = load_corpus();
    for case in &corpus.cases {
        let nodes: Vec<_> = case
            .nodes
            .iter()
            .map(|node| (node.id.clone(), node.id.clone(), node.path.clone()))
            .collect();
        let conn = index_fixture(&nodes, &case.edges);
        let mut pack = build_context_pack(&conn, case.task.clone(), 12).unwrap();
        personalize_context_pack(&conn, &mut pack).unwrap();
        assert!(
            pack.related_files.contains(&case.relevant_file),
            "{} lost {} under the floor: {:?}",
            case.id,
            case.relevant_file,
            pack.related_files
        );
    }
}
