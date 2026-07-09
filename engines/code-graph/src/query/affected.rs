//! Changed files to statically attributed tests, in one BFS over call edges.
//!
//! The caller supplies the changed files (usually `git diff --name-only`);
//! GraphTrail walks incoming call edges from every symbol in those files and
//! reports which test files reach them. "Statically attributed" is a lower
//! bound on real coverage, never "tested": fixtures, dynamic dispatch, and
//! harness-driven tests are invisible to call edges, and Rust `#[cfg(test)]`
//! modules inside source files count as source, not tests.

use std::collections::{HashMap, HashSet, VecDeque};

use anyhow::Result;
use rusqlite::{Connection, params};
use serde::Serialize;

use crate::query::graph::normalize_depth;

/// Default caller-BFS depth for affected-test attribution.
pub const DEFAULT_AFFECTED_DEPTH: usize = 3;
/// How many files each report list holds at most.
pub const AFFECTED_FILE_CAP: usize = 500;

/// In-band honesty note for affected-test reports.
pub const AFFECTED_ATTRIBUTION: &str = "tests statically attributed through incoming call \
     edges; a lower bound, not coverage. Absence here does not mean untested (fixtures, \
     dynamic dispatch, and harness-driven tests are invisible to call edges).";

#[derive(Debug, Serialize)]
pub struct AffectedReport {
    pub attribution: &'static str,
    pub depth: usize,
    /// Input files found in the index.
    pub changed_files: Vec<String>,
    /// Input files the index does not know (not indexed, ignored, or unsupported language).
    pub missing_files: Vec<String>,
    /// Test files whose symbols statically reach a changed symbol.
    pub affected_tests: Vec<AffectedFile>,
    /// Non-test files whose symbols statically reach a changed symbol.
    pub impacted_files: Vec<AffectedFile>,
    pub truncated: bool,
}

#[derive(Debug, Serialize)]
pub struct AffectedFile {
    pub file_path: String,
    /// Shortest caller-path length from this file to a changed symbol.
    /// 0 means the file itself changed.
    pub min_hops: usize,
    /// Up to five reached symbols in this file, nearest first.
    pub via: Vec<String>,
}

/// Which test files (and other dependents) statically reach the changed files.
pub fn affected(conn: &Connection, files: &[String], depth: usize) -> Result<AffectedReport> {
    let depth = normalize_depth(depth);
    let mut changed_files = Vec::new();
    let mut missing_files = Vec::new();
    for file in files {
        let known: bool = conn.query_row(
            "SELECT EXISTS(SELECT 1 FROM files WHERE path = ?1)",
            params![file],
            |row| row.get(0),
        )?;
        if known {
            changed_files.push(file.clone());
        } else {
            missing_files.push(file.clone());
        }
    }
    changed_files.sort();
    changed_files.dedup();
    missing_files.sort();
    missing_files.dedup();

    // Symbol id -> (name, file). One pass; the whole map is a few MB even on
    // large repos, far below the cost of per-symbol queries in the BFS.
    let mut symbols: HashMap<String, (String, String)> = HashMap::new();
    {
        let mut stmt = conn.prepare("SELECT id, name, file_path FROM symbols")?;
        let rows = stmt.query_map([], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
            ))
        })?;
        for row in rows {
            let (id, name, file) = row?;
            symbols.insert(id, (name, file));
        }
    }

    // Incoming adjacency: callee -> callers.
    let mut callers: HashMap<String, Vec<String>> = HashMap::new();
    {
        let mut stmt = conn.prepare("SELECT source, target FROM edges WHERE kind = 'calls'")?;
        let rows = stmt.query_map([], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
        })?;
        for row in rows {
            let (source, target) = row?;
            callers.entry(target).or_default().push(source);
        }
    }

    let changed_set: HashSet<&str> = changed_files.iter().map(String::as_str).collect();
    let mut queue: VecDeque<(String, usize)> = VecDeque::new();
    let mut reached: HashMap<String, usize> = HashMap::new();
    for (id, (_, file)) in &symbols {
        if changed_set.contains(file.as_str()) {
            reached.insert(id.clone(), 0);
            queue.push_back((id.clone(), 0));
        }
    }

    while let Some((symbol_id, hops)) = queue.pop_front() {
        if hops >= depth {
            continue;
        }
        let Some(sources) = callers.get(&symbol_id) else {
            continue;
        };
        for source in sources {
            if !reached.contains_key(source) {
                reached.insert(source.clone(), hops + 1);
                queue.push_back((source.clone(), hops + 1));
            }
        }
    }

    // Group reached symbols by file, keeping the nearest first.
    let mut per_file: HashMap<&str, Vec<(usize, &str)>> = HashMap::new();
    for (symbol_id, hops) in &reached {
        let Some((name, file)) = symbols.get(symbol_id) else {
            continue;
        };
        per_file
            .entry(file.as_str())
            .or_default()
            .push((*hops, name.as_str()));
    }

    let mut affected_tests = Vec::new();
    let mut impacted_files = Vec::new();
    for (file, mut hits) in per_file {
        hits.sort();
        let min_hops = hits.first().map_or(0, |(hops, _)| *hops);
        let is_test = is_test_file(file);
        if !is_test && changed_set.contains(file) {
            // The changed source files themselves are the input, not a finding.
            continue;
        }
        let row = AffectedFile {
            file_path: file.to_string(),
            min_hops,
            via: hits
                .into_iter()
                .map(|(_, name)| name.to_string())
                .take(5)
                .collect(),
        };
        if is_test {
            affected_tests.push(row);
        } else {
            impacted_files.push(row);
        }
    }
    let sort_key = |row: &AffectedFile| (row.min_hops, row.file_path.clone());
    affected_tests.sort_by_key(sort_key);
    impacted_files.sort_by_key(sort_key);
    let truncated =
        affected_tests.len() > AFFECTED_FILE_CAP || impacted_files.len() > AFFECTED_FILE_CAP;
    affected_tests.truncate(AFFECTED_FILE_CAP);
    impacted_files.truncate(AFFECTED_FILE_CAP);

    Ok(AffectedReport {
        attribution: AFFECTED_ATTRIBUTION,
        depth,
        changed_files,
        missing_files,
        affected_tests,
        impacted_files,
        truncated,
    })
}

/// Path-based test heuristic for the supported languages. Rust `#[cfg(test)]`
/// modules inside src files are invisible to this and stay classified as source.
pub(crate) fn is_test_file(path: &str) -> bool {
    let lower = path.to_ascii_lowercase();
    let file_name = lower.rsplit('/').next().unwrap_or(&lower);
    let in_test_dir = lower.split('/').any(|segment| {
        matches!(
            segment,
            "tests" | "test" | "__tests__" | "testdata" | "spec"
        )
    });
    in_test_dir
        || file_name.starts_with("test_")
        || file_name.ends_with("_test.py")
        || file_name.ends_with("_test.go")
        || file_name.ends_with("_test.rs")
        || file_name.contains(".test.")
        || file_name.contains(".spec.")
}

#[cfg(test)]
mod tests {
    use super::is_test_file;

    #[test]
    fn test_paths_are_detected_per_language() {
        for path in [
            "tests/incremental.rs",
            "pkg/store_test.go",
            "src/__tests__/app.ts",
            "src/app.test.tsx",
            "src/app.spec.js",
            "tests/test_sync.py",
            "pkg/test_helpers.py",
            "app/lib_test.py",
        ] {
            assert!(is_test_file(path), "{path} should be a test file");
        }
    }

    #[test]
    fn source_paths_are_not_tests() {
        for path in [
            "src/store/sync.rs",
            "app/contest.py",
            "src/latest.ts",
            "pkg/attestation.go",
        ] {
            assert!(!is_test_file(path), "{path} should not be a test file");
        }
    }
}
