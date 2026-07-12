//! Context packs: entry points for a task plus their caller/callee neighborhoods.

use std::collections::{BTreeMap, BTreeSet, HashSet};

use anyhow::Result;
use rusqlite::Connection;

use crate::model::{ContextPack, Direction, EdgeRow, SearchRow};
use crate::query::graph::edges_for_symbol_id;
use crate::query::search::search_symbols;
use crate::store::SCHEMA_VERSION;

pub fn build_context_pack(conn: &Connection, task: String, limit: usize) -> Result<ContextPack> {
    let entry_points = search_symbols(conn, &task, limit)?;
    build_context_pack_from_entry_points(conn, task, entry_points)
}

pub fn build_context_pack_from_entry_points(
    conn: &Connection,
    task: String,
    entry_points: Vec<SearchRow>,
) -> Result<ContextPack> {
    let mut callers = Vec::new();
    let mut callees = Vec::new();
    let mut files = HashSet::new();
    for row in &entry_points {
        files.insert(row.file_path.clone());
        callers.extend(edges_for_symbol_id(conn, &row.id, Direction::Incoming)?);
        callees.extend(edges_for_symbol_id(conn, &row.id, Direction::Outgoing)?);
    }
    for edge in callers.iter().chain(callees.iter()) {
        files.insert(edge.source_file.clone());
        files.insert(edge.target_file.clone());
    }
    let mut related_files: Vec<String> = files.into_iter().collect();
    related_files.sort();
    Ok(ContextPack {
        schema_version: SCHEMA_VERSION,
        task,
        entry_points,
        callers,
        callees,
        related_files,
    })
}

pub fn personalize_context_pack(conn: &Connection, pack: &mut ContextPack) -> Result<()> {
    let ranks = personalized_file_ranks(conn, &pack.task, &pack.entry_points)?;
    let rank = |path: &str| ranks.get(path).copied().unwrap_or(0.0);
    let task_lower = pack.task.to_lowercase();
    let mut files = conn.prepare("SELECT path FROM files ORDER BY path")?;
    for row in files.query_map([], |row| row.get::<_, String>(0))? {
        let path = row?;
        if task_lower.contains(&path.to_lowercase()) && !pack.related_files.contains(&path) {
            pack.related_files.push(path);
        }
    }
    pack.entry_points.sort_by(|a, b| {
        rank(&b.file_path)
            .total_cmp(&rank(&a.file_path))
            .then_with(|| a.file_path.cmp(&b.file_path))
            .then_with(|| a.start_line.cmp(&b.start_line))
    });
    pack.callers.sort_by(|a, b| {
        rank(&b.source_file)
            .total_cmp(&rank(&a.source_file))
            .then_with(|| a.source_file.cmp(&b.source_file))
            .then_with(|| a.line.cmp(&b.line))
            .then_with(|| a.source.cmp(&b.source))
            .then_with(|| a.target.cmp(&b.target))
    });
    pack.callees.sort_by(|a, b| {
        rank(&b.target_file)
            .total_cmp(&rank(&a.target_file))
            .then_with(|| a.target_file.cmp(&b.target_file))
            .then_with(|| a.line.cmp(&b.line))
            .then_with(|| a.source.cmp(&b.source))
            .then_with(|| a.target.cmp(&b.target))
    });
    pack.related_files
        .sort_by(|a, b| rank(b).total_cmp(&rank(a)).then_with(|| a.cmp(b)));
    Ok(())
}

fn personalized_file_ranks(
    conn: &Connection,
    task: &str,
    entry_points: &[SearchRow],
) -> Result<BTreeMap<String, f64>> {
    let mut adjacency: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
    let mut stmt = conn.prepare(
        "SELECT DISTINCT src.file_path, dst.file_path FROM edges e
         JOIN symbols src ON src.id = e.source
         JOIN symbols dst ON dst.id = e.target
         WHERE src.file_path != dst.file_path",
    )?;
    let rows = stmt.query_map([], |row| {
        Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
    })?;
    for row in rows {
        let (source, target) = row?;
        adjacency
            .entry(source.clone())
            .or_default()
            .insert(target.clone());
        adjacency.entry(target).or_default().insert(source);
    }
    let mut seeds: BTreeMap<String, f64> = BTreeMap::new();
    for (index, entry) in entry_points.iter().enumerate() {
        *seeds.entry(entry.file_path.clone()).or_default() += 1.0 / (index + 1) as f64;
        adjacency.entry(entry.file_path.clone()).or_default();
    }
    let task_lower = task.to_lowercase();
    let mut files = conn.prepare("SELECT path FROM files ORDER BY path")?;
    for row in files.query_map([], |row| row.get::<_, String>(0))? {
        let path = row?;
        let basename = path.rsplit('/').next().unwrap_or(&path);
        if task_lower.contains(&path.to_lowercase())
            || task_lower.contains(&basename.to_lowercase())
        {
            *seeds.entry(path.clone()).or_default() += 2.0;
        }
        adjacency.entry(path).or_default();
    }
    if adjacency.is_empty() {
        return Ok(BTreeMap::new());
    }
    if seeds.is_empty() {
        for path in adjacency.keys() {
            seeds.insert(path.clone(), 1.0);
        }
    }
    let seed_total: f64 = seeds.values().sum();
    let teleport: BTreeMap<String, f64> = adjacency
        .keys()
        .map(|path| {
            (
                path.clone(),
                seeds.get(path).copied().unwrap_or(0.0) / seed_total,
            )
        })
        .collect();
    let mut ranks = teleport.clone();
    const DAMPING: f64 = 0.85;
    for _ in 0..24 {
        let mut next: BTreeMap<String, f64> = teleport
            .iter()
            .map(|(path, weight)| (path.clone(), (1.0 - DAMPING) * weight))
            .collect();
        let mut dangling = 0.0;
        for (path, score) in &ranks {
            let neighbors = &adjacency[path];
            if neighbors.is_empty() {
                dangling += score;
                continue;
            }
            let share = DAMPING * score / neighbors.len() as f64;
            for neighbor in neighbors {
                *next.entry(neighbor.clone()).or_default() += share;
            }
        }
        for (path, weight) in &teleport {
            *next.entry(path.clone()).or_default() += DAMPING * dangling * weight;
        }
        ranks = next;
    }
    // Preserve an explicit seed prior after propagation so an unrelated hub
    // cannot displace the file the task actually named.
    for (path, weight) in teleport {
        *ranks.entry(path).or_default() += weight;
    }
    Ok(ranks)
}

/// Render a context pack as a Brigade-friendly markdown document (droppable into a handoff's
/// evidence/context section, or readable directly by an agent).
pub fn render_markdown(pack: &ContextPack) -> String {
    use std::fmt::Write;
    let mut md = String::new();
    let _ = writeln!(md, "# Context Pack: {}\n", pack.task);
    let _ = writeln!(
        md,
        "_schema v{} - {} entry points - {} callers - {} callees - {} related files_\n",
        pack.schema_version,
        pack.entry_points.len(),
        pack.callers.len(),
        pack.callees.len(),
        pack.related_files.len()
    );

    let _ = writeln!(md, "## Entry points\n");
    if pack.entry_points.is_empty() {
        let _ = writeln!(md, "_none_\n");
    } else {
        for row in &pack.entry_points {
            let _ = writeln!(
                md,
                "- `{}` ({}) - {}",
                row.qualified_name,
                row.kind,
                symbol_location(row)
            );
        }
        md.push('\n');
    }

    let _ = writeln!(md, "## Callers\n");
    if pack.callers.is_empty() {
        let _ = writeln!(md, "_none_\n");
    } else {
        for edge in &pack.callers {
            let _ = writeln!(
                md,
                "- `{}` -> `{}` - {}",
                edge.source,
                edge.target,
                edge_location(edge)
            );
        }
        md.push('\n');
    }

    let _ = writeln!(md, "## Callees\n");
    if pack.callees.is_empty() {
        let _ = writeln!(md, "_none_\n");
    } else {
        for edge in &pack.callees {
            let _ = writeln!(
                md,
                "- `{}` -> `{}` - {}",
                edge.source,
                edge.target,
                edge_location(edge)
            );
        }
        md.push('\n');
    }

    let _ = writeln!(md, "## Related files\n");
    if pack.related_files.is_empty() {
        let _ = writeln!(md, "_none_");
    } else {
        for file in &pack.related_files {
            let _ = writeln!(md, "- {file}");
        }
    }

    md
}

pub fn render_markdown_budgeted(pack: &ContextPack, max_chars: usize) -> String {
    if max_chars == 0 {
        return String::new();
    }
    let task_title = pack.task.lines().next().unwrap_or_default();
    let compact_task = if task_title.chars().count() > 120 {
        format!("{}...", task_title.chars().take(117).collect::<String>())
    } else {
        task_title.to_string()
    };
    let mut lines = vec![
        format!("# Context Pack: {compact_task}"),
        String::new(),
        format!(
            "_schema v{} - {} entry points - {} callers - {} callees - {} related files_",
            pack.schema_version,
            pack.entry_points.len(),
            pack.callers.len(),
            pack.callees.len(),
            pack.related_files.len()
        ),
        String::new(),
        "## Entry points".to_string(),
        String::new(),
    ];
    if pack.entry_points.is_empty() {
        lines.push("_none_".to_string());
    } else {
        lines.extend(pack.entry_points.iter().map(|row| {
            format!(
                "- `{}` ({}) - {}",
                row.qualified_name,
                row.kind,
                symbol_location(row)
            )
        }));
    }
    lines.extend([String::new(), "## Related files".to_string(), String::new()]);
    if pack.related_files.is_empty() {
        lines.push("_none_".to_string());
    } else {
        lines.extend(pack.related_files.iter().map(|file| format!("- {file}")));
    }
    lines.extend([String::new(), "## Callers".to_string(), String::new()]);
    if pack.callers.is_empty() {
        lines.push("_none_".to_string());
    } else {
        lines.extend(pack.callers.iter().map(|edge| {
            format!(
                "- `{}` -> `{}` - {}",
                edge.source,
                edge.target,
                edge_location(edge)
            )
        }));
    }
    lines.extend([String::new(), "## Callees".to_string(), String::new()]);
    if pack.callees.is_empty() {
        lines.push("_none_".to_string());
    } else {
        lines.extend(pack.callees.iter().map(|edge| {
            format!(
                "- `{}` -> `{}` - {}",
                edge.source,
                edge.target,
                edge_location(edge)
            )
        }));
    }

    let mut output = String::new();
    let mut used_chars = 0usize;
    for line in lines {
        let candidate = format!("{line}\n");
        let candidate_chars = candidate.chars().count();
        if used_chars + candidate_chars > max_chars {
            break;
        }
        output.push_str(&candidate);
        used_chars += candidate_chars;
    }
    output
}

pub(crate) fn symbol_location(row: &SearchRow) -> String {
    format!(
        "{}:{}",
        row.file_path,
        line_range(row.start_line, row.end_line)
    )
}

pub(crate) fn edge_location(edge: &EdgeRow) -> String {
    match edge.line {
        Some(line) => format!("{}:{} -> {}", edge.source_file, line, edge.target_file),
        None => format!("{}:? -> {}", edge.source_file, edge.target_file),
    }
}

fn line_range(start: usize, end: usize) -> String {
    if start == end {
        start.to_string()
    } else {
        format!("{start}-{end}")
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::{EdgeRow, SearchRow};
    use crate::store::init_schema;
    use rusqlite::params;

    #[test]
    fn markdown_context_renders_symbol_ranges_and_edge_locations() {
        let pack = ContextPack {
            schema_version: SCHEMA_VERSION,
            task: "wire context".to_string(),
            entry_points: vec![SearchRow {
                id: "sym-run".to_string(),
                kind: "function".to_string(),
                name: "run".to_string(),
                qualified_name: "run".to_string(),
                file_path: "app.py".to_string(),
                start_line: 5,
                end_line: 7,
                signature: "def run():".to_string(),
                score: 1.0,
            }],
            callers: vec![EdgeRow {
                source_id: "sym-main".to_string(),
                source: "main".to_string(),
                target_id: "sym-run".to_string(),
                target: "run".to_string(),
                kind: "call".to_string(),
                line: Some(12),
                source_file: "cli.py".to_string(),
                target_file: "app.py".to_string(),
                hops: 1,
                confidence: None,
            }],
            callees: vec![EdgeRow {
                source_id: "sym-run".to_string(),
                source: "run".to_string(),
                target_id: "sym-helper".to_string(),
                target: "helper".to_string(),
                kind: "call".to_string(),
                line: Some(6),
                source_file: "app.py".to_string(),
                target_file: "lib.py".to_string(),
                hops: 1,
                confidence: None,
            }],
            related_files: vec![
                "app.py".to_string(),
                "cli.py".to_string(),
                "lib.py".to_string(),
            ],
        };

        let md = render_markdown(&pack);

        assert!(md.contains("- `run` (function) - app.py:5-7"));
        assert!(md.contains("- `main` -> `run` - cli.py:12 -> app.py"));
        assert!(md.contains("- `run` -> `helper` - app.py:6 -> lib.py"));
    }

    #[test]
    fn personalized_task_seed_outranks_unrelated_high_degree_hub() {
        let conn = Connection::open_in_memory().unwrap();
        init_schema(&conn).unwrap();
        for (id, path) in [
            ("seed", "src/target.rs"),
            ("hub", "src/hub.rs"),
            ("a", "src/a.rs"),
            ("b", "src/b.rs"),
            ("c", "src/c.rs"),
        ] {
            conn.execute(
                "INSERT INTO files(path, content_hash, size, modified_at, indexed_at, language)
                 VALUES (?1, 'h', 1, 1, 1, 'rust')",
                params![path],
            )
            .unwrap();
            conn.execute(
                "INSERT INTO symbols(id, kind, name, qualified_name, file_path, start_line, end_line, signature, content_hash)
                 VALUES (?1, 'function', ?1, ?1, ?2, 1, 1, ?1, 'h')",
                params![id, path],
            )
            .unwrap();
        }
        for target in ["a", "b", "c"] {
            conn.execute(
                "INSERT INTO edges(source, target, kind, line) VALUES ('hub', ?1, 'calls', 1)",
                params![target],
            )
            .unwrap();
        }
        let row = |id: &str, path: &str| SearchRow {
            id: id.to_string(),
            kind: "function".to_string(),
            name: id.to_string(),
            qualified_name: id.to_string(),
            file_path: path.to_string(),
            start_line: 1,
            end_line: 1,
            signature: id.to_string(),
            score: 1.0,
        };
        let mut pack = build_context_pack_from_entry_points(
            &conn,
            "change src/target.rs".to_string(),
            vec![row("hub", "src/hub.rs"), row("seed", "src/target.rs")],
        )
        .unwrap();

        let ranks = personalized_file_ranks(&conn, &pack.task, &pack.entry_points).unwrap();
        assert!(
            ranks["src/target.rs"] > ranks["src/hub.rs"],
            "target={} hub={}",
            ranks["src/target.rs"],
            ranks["src/hub.rs"]
        );

        personalize_context_pack(&conn, &mut pack).unwrap();

        assert_eq!(pack.entry_points[0].file_path, "src/target.rs");
        assert_eq!(pack.related_files[0], "src/target.rs");
    }

    #[test]
    fn personalized_ranks_are_exactly_deterministic_for_symmetric_files() {
        let conn = Connection::open_in_memory().unwrap();
        init_schema(&conn).unwrap();
        for (id, path) in [
            ("seed", "src/seed.rs"),
            ("left", "src/left.rs"),
            ("right", "src/right.rs"),
        ] {
            conn.execute(
                "INSERT INTO files(path, content_hash, size, modified_at, indexed_at, language)
                 VALUES (?1, 'h', 1, 1, 1, 'rust')",
                params![path],
            )
            .unwrap();
            conn.execute(
                "INSERT INTO symbols(id, kind, name, qualified_name, file_path, start_line, end_line, signature, content_hash)
                 VALUES (?1, 'function', ?1, ?1, ?2, 1, 1, ?1, 'h')",
                params![id, path],
            )
            .unwrap();
        }
        for index in 0..24 {
            for side in ["a", "b"] {
                let id = format!("{side}{index}");
                let path = format!("src/{id}.rs");
                conn.execute(
                    "INSERT INTO files(path, content_hash, size, modified_at, indexed_at, language)
                     VALUES (?1, 'h', 1, 1, 1, 'rust')",
                    params![path],
                )
                .unwrap();
                conn.execute(
                    "INSERT INTO symbols(id, kind, name, qualified_name, file_path, start_line, end_line, signature, content_hash)
                     VALUES (?1, 'function', ?1, ?1, ?2, 1, 1, ?1, 'h')",
                    params![id, path],
                )
                .unwrap();
                conn.execute(
                    "INSERT INTO edges(source, target, kind, line) VALUES ('seed', ?1, 'calls', 1)",
                    params![id],
                )
                .unwrap();
                let target = if side == "a" { "left" } else { "right" };
                conn.execute(
                    "INSERT INTO edges(source, target, kind, line) VALUES (?1, ?2, 'calls', 2)",
                    params![id, target],
                )
                .unwrap();
            }
        }
        let entry = SearchRow {
            id: "seed".to_string(),
            kind: "function".to_string(),
            name: "seed".to_string(),
            qualified_name: "seed".to_string(),
            file_path: "src/seed.rs".to_string(),
            start_line: 1,
            end_line: 1,
            signature: "seed".to_string(),
            score: 1.0,
        };

        let mut expected = None;
        for _ in 0..32 {
            let ranks =
                personalized_file_ranks(&conn, "seed", std::slice::from_ref(&entry)).unwrap();
            assert_eq!(
                ranks["src/left.rs"].to_bits(),
                ranks["src/right.rs"].to_bits()
            );
            let mut bits: Vec<_> = ranks
                .into_iter()
                .map(|(path, rank)| (path, rank.to_bits()))
                .collect();
            bits.sort();
            if let Some(expected) = &expected {
                assert_eq!(&bits, expected);
            } else {
                expected = Some(bits);
            }
        }
    }

    #[test]
    fn personalized_callers_are_ordered_by_caller_file_rank() {
        let conn = Connection::open_in_memory().unwrap();
        init_schema(&conn).unwrap();
        for (id, path) in [
            ("target", "src/target.rs"),
            ("low", "src/a-low.rs"),
            ("high", "src/z-high.rs"),
            ("leaf1", "src/leaf1.rs"),
            ("leaf2", "src/leaf2.rs"),
            ("leaf3", "src/leaf3.rs"),
        ] {
            conn.execute(
                "INSERT INTO files(path, content_hash, size, modified_at, indexed_at, language)
                 VALUES (?1, 'h', 1, 1, 1, 'rust')",
                params![path],
            )
            .unwrap();
            conn.execute(
                "INSERT INTO symbols(id, kind, name, qualified_name, file_path, start_line, end_line, signature, content_hash)
                 VALUES (?1, 'function', ?1, ?1, ?2, 1, 1, ?1, 'h')",
                params![id, path],
            )
            .unwrap();
        }
        for (source, target) in [
            ("low", "target"),
            ("high", "target"),
            ("high", "leaf1"),
            ("high", "leaf2"),
            ("high", "leaf3"),
        ] {
            conn.execute(
                "INSERT INTO edges(source, target, kind, line) VALUES (?1, ?2, 'calls', 1)",
                params![source, target],
            )
            .unwrap();
        }
        let entry = SearchRow {
            id: "target".to_string(),
            kind: "function".to_string(),
            name: "target".to_string(),
            qualified_name: "target".to_string(),
            file_path: "src/target.rs".to_string(),
            start_line: 1,
            end_line: 1,
            signature: "target".to_string(),
            score: 1.0,
        };
        let mut pack = build_context_pack_from_entry_points(
            &conn,
            "change src/target.rs".to_string(),
            vec![entry],
        )
        .unwrap();

        personalize_context_pack(&conn, &mut pack).unwrap();

        assert_eq!(pack.callers[0].source_file, "src/z-high.rs");
        assert_eq!(pack.callers[1].source_file, "src/a-low.rs");
    }

    #[test]
    fn personalized_context_keeps_exact_task_mentioned_files() {
        let conn = Connection::open_in_memory().unwrap();
        init_schema(&conn).unwrap();
        for (id, path) in [("entry", "src/entry.rs"), ("mentioned", "src/mentioned.rs")] {
            conn.execute(
                "INSERT INTO files(path, content_hash, size, modified_at, indexed_at, language)
                 VALUES (?1, 'h', 1, 1, 1, 'rust')",
                params![path],
            )
            .unwrap();
            conn.execute(
                "INSERT INTO symbols(id, kind, name, qualified_name, file_path, start_line, end_line, signature, content_hash)
                 VALUES (?1, 'function', ?1, ?1, ?2, 1, 1, ?1, 'h')",
                params![id, path],
            )
            .unwrap();
        }
        let entry = SearchRow {
            id: "entry".to_string(),
            kind: "function".to_string(),
            name: "entry".to_string(),
            qualified_name: "entry".to_string(),
            file_path: "src/entry.rs".to_string(),
            start_line: 1,
            end_line: 1,
            signature: "entry".to_string(),
            score: 1.0,
        };
        let mut pack = build_context_pack_from_entry_points(
            &conn,
            "change src/mentioned.rs".to_string(),
            vec![entry],
        )
        .unwrap();

        personalize_context_pack(&conn, &mut pack).unwrap();

        assert!(pack.related_files.contains(&"src/mentioned.rs".to_string()));
    }

    #[test]
    fn budgeted_markdown_never_splits_lines_or_exceeds_budget() {
        let mut pack = ContextPack {
            schema_version: SCHEMA_VERSION,
            task: "x".repeat(500),
            entry_points: Vec::new(),
            callers: Vec::new(),
            callees: Vec::new(),
            related_files: (0..100).map(|i| format!("src/file-{i}.rs")).collect(),
        };
        pack.related_files.sort();
        let rendered = render_markdown_budgeted(&pack, 400);
        assert!(rendered.len() <= 400);
        assert!(rendered.ends_with('\n'));
        assert!(!rendered.contains(&"x".repeat(121)));
    }

    #[test]
    fn budgeted_markdown_does_not_replay_multiline_task_text() {
        let pack = ContextPack {
            schema_version: SCHEMA_VERSION,
            task: "short title\nSECOND-LINE-SHOULD-NOT-BE-ECHOED\n## Injected section".to_string(),
            entry_points: Vec::new(),
            callers: Vec::new(),
            callees: Vec::new(),
            related_files: vec!["src/app.rs".to_string()],
        };

        let rendered = render_markdown_budgeted(&pack, 400);

        assert!(rendered.starts_with("# Context Pack: short title\n"));
        assert!(!rendered.contains("SECOND-LINE-SHOULD-NOT-BE-ECHOED"));
        assert!(!rendered.contains("## Injected section"));
        assert!(rendered.contains("src/app.rs"));
    }
}
