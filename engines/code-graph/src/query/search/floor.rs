//! Relevance floor for task search (brigade#1648). Opt-in: `context` applies it
//! only with `--relevance-floor`.
//!
//! Keyword search ORs every word of a task sentence, so words like "update" or
//! "install" pull in symbols from anywhere in the repository. The floor reads the
//! top `CANDIDATE_POOL` keyword rows, keeps a hit only when the task carries
//! evidence for it, and truncates to `limit`:
//!
//! 1. identified: the task names the hit's file, or spells the hit's name as a
//!    code identifier or as a compound name in any case (`codegraphbrief`), and
//!    no other symbol shares that name.
//! 2. described: the hit's name and path explain at least `min_task_coverage`
//!    of the task's distinctive words, at least one of them in the name, and the
//!    task explains at least `min_name_coverage` of the hit's name.
//! 3. located (fallback only): the task names a directory the hit lives in ("the
//!    Rust routing hub"), or names its file stem while a second distinctive task
//!    word appears in its name ("the request router" for `routeRequest`).
//!
//! An exact name that other symbols share (`run_dir`, `__init__`) is only
//! ordinary evidence. Identified and described hits are kept together,
//! identified first. Located hits are used only when neither tier has any, or
//! when every kept hit is a test (then only located code, not tests, joins).
//! Symbols found by exact-name or named-file lookup count only as identified. A
//! task about documentation (README, CHANGELOG, a `docs:` change, a Markdown
//! path) keeps identified hits only. Nested helpers inside test functions and
//! vendored or minified files never count as described or located.
//!
//! When no hit clears the floor the pack is empty and marked not confident. Both
//! thresholds are calibrated on hand-labeled real issue titles. See
//! `tests/context_ranking_benchmark.rs`.

use std::cmp::Ordering;
use std::collections::{HashMap, HashSet};

use anyhow::Result;
use rusqlite::{Connection, params};

use super::{search_row_from_sql, search_symbols, sqlite_limit};
use crate::model::{RelevanceFloor, SearchRow};

/// Name of the rule `floored_search` applies, recorded on every context pack.
pub const RELEVANCE_FLOOR_RULE: &str = "task-coverage-v2";

/// Minimum share of the task's distinctive words a described hit must explain.
/// Calibrated by `floor_thresholds_match_their_real_calibration_sweep`.
pub const MIN_TASK_COVERAGE: f64 = 0.30;

/// Minimum share of a described hit's name tokens the task must explain.
/// Calibrated by `floor_thresholds_match_their_real_calibration_sweep`.
pub const MIN_NAME_COVERAGE: f64 = 0.10;

/// Thresholds for the described tier.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct FloorParams {
    pub min_task_coverage: f64,
    pub min_name_coverage: f64,
}

impl FloorParams {
    pub const CALIBRATED: FloorParams = FloorParams {
        min_task_coverage: MIN_TASK_COVERAGE,
        min_name_coverage: MIN_NAME_COVERAGE,
    };
}

/// Keyword rows the floor looks at before truncating to `limit`, so a relevant
/// hit ranked just past `limit` is not lost to noise ranked above it.
pub const CANDIDATE_POOL: usize = 50;

/// Most exact-name and named-file symbols added to the keyword candidates. A cost
/// guard only.
const LOOKUP_LIMIT: usize = 4096;

/// Generic words that never count as evidence: English function words, the
/// verbs and nouns most task titles use for any kind of change, and
/// conventional-commit types.
#[rustfmt::skip]
const STOPWORDS: &[&str] = &[
    "a", "about", "add", "added", "adds", "after", "all", "also", "an", "and", "any", "are", "as",
    "at", "be", "before", "build", "but", "by", "can", "change", "changed", "changes", "check",
    "chore", "ci", "could", "do", "does", "doc", "docs", "ensure", "explain", "feat", "find",
    "fix", "fixed", "fixes", "for", "from", "go", "has", "have", "how", "identify", "if", "in",
    "inspect", "into", "investigate", "is", "it", "its", "look", "make", "makes", "mention",
    "mentions", "more", "new", "no", "not", "of", "old", "on", "only", "or", "out", "perf",
    "refactor", "remove", "removed", "removes", "section", "should", "show", "so", "some",
    "style", "support", "test", "tests", "than", "that", "the", "their", "them", "then", "there",
    "these", "this", "those", "to", "trace", "update", "updated", "updates", "use", "used",
    "uses", "via", "was", "were", "when", "where", "which", "while", "will", "with", "would",
];

/// Words that make a task about documentation files rather than code.
const DOC_WORDS: &[&str] = &[
    "readme",
    "changelog",
    "license",
    "contributing",
    "documentation",
];

/// Path segments too common to say anything about a file.
const PATH_NOISE: &[&str] = &[
    "src", "test", "tests", "lib", "mod", "index", "main", "init",
];

/// Extensions that make a task token a file reference rather than a dotted name.
#[rustfmt::skip]
const FILE_EXTENSIONS: &[&str] = &[
    "adoc", "astro", "bash", "c", "cc", "cjs", "cpp", "cs", "css", "fish", "go", "h", "hpp",
    "html", "java", "js", "json", "jsx", "kt", "kts", "md", "mjs", "php", "py", "pyi", "rb",
    "rs", "rst", "scala", "sh", "sql", "svelte", "swift", "toml", "ts", "tsx", "txt", "vue",
    "yaml", "yml", "zsh",
];

/// Extensions of documentation files.
const DOC_EXTENSIONS: &[&str] = &["md", "rst", "txt", "adoc"];

/// Search hits that cleared the relevance floor, plus how the floor was applied.
#[derive(Debug)]
pub struct FlooredSearch {
    pub rows: Vec<SearchRow>,
    pub floor: RelevanceFloor,
}

/// Search the task text and keep at most `limit` hits that clear the floor.
///
/// The keyword candidates are the top `limit` rows, so the floor filters what
/// search would have returned. Symbols named exactly by the task and symbols in
/// files the task names are added whatever their keyword rank.
pub fn floored_search(
    conn: &Connection,
    task: &str,
    limit: usize,
    params: FloorParams,
) -> Result<FlooredSearch> {
    let terms = TaskTerms::parse(task);
    let mentioned = mentioned_files(conn, &terms.paths)?;
    let keyword = search_symbols(conn, task, limit.max(CANDIDATE_POOL))?;
    let lookups = lookup_candidates(conn, &terms, &mentioned)?;
    Ok(select(
        &terms, &keyword, &lookups, &mentioned, limit, params,
    ))
}

/// Apply the floor to candidates already in hand. `mentioned` holds the indexed
/// files the task names.
pub fn select_entry_points(
    task: &str,
    candidates: &[SearchRow],
    mentioned: &HashSet<String>,
    limit: usize,
    params: FloorParams,
) -> FlooredSearch {
    select(
        &TaskTerms::parse(task),
        candidates,
        &[],
        mentioned,
        limit,
        params,
    )
}

/// `keyword` rows are what search returned. `lookups` are symbols the task
/// names exactly or that live in files it names: they count only as identified
/// evidence, never as described or located.
fn select(
    terms: &TaskTerms,
    keyword: &[SearchRow],
    lookups: &[SearchRow],
    mentioned: &HashSet<String>,
    limit: usize,
    params: FloorParams,
) -> FlooredSearch {
    let mut seen = HashSet::new();
    let pool: Vec<(&SearchRow, bool)> = keyword
        .iter()
        .map(|row| (row, false))
        .chain(lookups.iter().map(|row| (row, true)))
        .filter(|(row, _)| seen.insert(row.id.as_str()))
        .collect();
    let mut name_counts: HashMap<String, usize> = HashMap::new();
    for (row, _) in &pool {
        *name_counts.entry(compact(&row.name)).or_default() += 1;
    }
    let mut kept = Vec::new();
    let mut located = Vec::new();
    for &(row, lookup_only) in &pool {
        match evidence(row, terms, mentioned, &name_counts, params) {
            // Lookups found by name count only when the task names them.
            Some(found) if lookup_only && !found.named => {}
            Some(found) if matches!(found.tier, Tier::Located) => located.push((found, row)),
            Some(found) => kept.push((found, row)),
            None => {}
        }
    }
    if kept.is_empty() {
        kept = located;
    } else if kept.iter().all(|(_, row)| is_test_symbol(row)) {
        // Only tests matched, so the code under test is missing. Add the
        // located code, never more located tests.
        kept.extend(located.into_iter().filter(|(_, row)| !is_test_symbol(row)));
    }
    kept.sort_by(|(a_ev, a), (b_ev, b)| {
        (a_ev.tier as u8)
            .cmp(&(b_ev.tier as u8))
            .then_with(|| b_ev.verbatim.cmp(&a_ev.verbatim))
            .then_with(|| b_ev.task_coverage.total_cmp(&a_ev.task_coverage))
            .then_with(|| b_ev.name_coverage.total_cmp(&a_ev.name_coverage))
            .then_with(|| b.score.partial_cmp(&a.score).unwrap_or(Ordering::Equal))
            .then_with(|| a.file_path.cmp(&b.file_path))
            .then_with(|| a.start_line.cmp(&b.start_line))
            .then_with(|| a.id.cmp(&b.id))
    });
    let survivors = kept.len();
    let rows = kept
        .into_iter()
        .take(limit)
        .map(|(_, row)| row.clone())
        .collect();
    FlooredSearch {
        rows,
        floor: RelevanceFloor {
            rule: RELEVANCE_FLOOR_RULE.to_string(),
            min_task_coverage: params.min_task_coverage,
            min_name_coverage: params.min_name_coverage,
            candidates: pool.len(),
            kept: survivors,
            dropped: pool.len() - survivors,
        },
    }
}

#[derive(Debug, Default)]
struct TaskTerms {
    /// Words outside file references that are neither stopwords nor doc words,
    /// deduplicated.
    distinctive: Vec<String>,
    /// Identifier segments as written (`Foo::bar` gives `Foo` and `bar`).
    identifiers: Vec<String>,
    /// Lowercase file references (`src/a.rs`, `context.rs`).
    paths: Vec<String>,
    /// Every non-path token and identifier segment, lowercased with separators
    /// removed, so `codegraphbrief` and `CODE_GRAPH_BRIEF` both name `CodeGraphBrief`.
    compact: Vec<String>,
    /// The task is about documentation files, not code.
    docs: bool,
}

impl TaskTerms {
    fn parse(task: &str) -> Self {
        let mut terms = TaskTerms::default();
        let lead = task.trim_start().to_lowercase();
        terms.docs = lead.starts_with("docs:") || lead.starts_with("docs(");
        for raw in task.split_whitespace() {
            let token = raw.trim_matches(|c: char| !(c.is_alphanumeric() || c == '_'));
            if token.is_empty() {
                continue;
            }
            if is_file_reference(token) {
                let lower = token.to_lowercase();
                let doc_file = lower.starts_with("docs/")
                    || lower
                        .rsplit_once('.')
                        .is_some_and(|(_, ext)| DOC_EXTENSIONS.contains(&ext));
                terms.docs |= doc_file;
                terms.paths.push(lower);
                continue;
            }
            for word in name_tokens(token) {
                if DOC_WORDS.contains(&word.as_str()) {
                    terms.docs = true;
                } else if !is_stopword(&word) && !terms.distinctive.contains(&word) {
                    terms.distinctive.push(word);
                }
            }
            for segment in std::iter::once(token).chain(token.split(['.', ':'])) {
                let key = compact(segment);
                if !key.is_empty() && !terms.compact.contains(&key) {
                    terms.compact.push(key);
                }
            }
            if is_code_identifier(token) {
                for segment in token.split(['.', ':']) {
                    if !name_tokens(segment).is_empty() {
                        terms.identifiers.push(segment.to_string());
                    }
                }
            }
        }
        terms
    }
}

/// Lowercase alphanumerics only: `Code_Graph-Brief` becomes `codegraphbrief`.
fn compact(name: &str) -> String {
    name.chars()
        .filter(|c| c.is_alphanumeric())
        .flat_map(char::to_lowercase)
        .collect()
}

fn is_file_reference(token: &str) -> bool {
    if token.contains('/') {
        return true;
    }
    match token.rsplit_once('.') {
        Some((stem, ext)) => !stem.is_empty() && FILE_EXTENSIONS.contains(&ext),
        None => false,
    }
}

fn is_code_identifier(token: &str) -> bool {
    let has_snake = token.contains('_') && token.chars().any(char::is_alphanumeric);
    let chars: Vec<char> = token.chars().collect();
    let has_camel = chars
        .windows(2)
        .any(|pair| (pair[0].is_lowercase() || pair[0].is_ascii_digit()) && pair[1].is_uppercase());
    has_snake || has_camel || token.contains("::")
}

/// Lowercase sub-words of a name: split on non-alphanumerics and lower-to-upper
/// case changes, dropping one-character pieces.
pub(crate) fn name_tokens(name: &str) -> Vec<String> {
    let mut tokens = Vec::new();
    let mut current = String::new();
    let mut previous: Option<char> = None;
    for c in name.chars() {
        if !c.is_alphanumeric() {
            push_token(&mut tokens, &mut current);
            previous = None;
            continue;
        }
        let boundary = previous
            .is_some_and(|prev| (prev.is_lowercase() || prev.is_ascii_digit()) && c.is_uppercase());
        if boundary {
            push_token(&mut tokens, &mut current);
        }
        current.extend(c.to_lowercase());
        previous = Some(c);
    }
    push_token(&mut tokens, &mut current);
    tokens
}

fn push_token(tokens: &mut Vec<String>, current: &mut String) {
    if current.chars().count() >= 2 {
        tokens.push(std::mem::take(current));
    } else {
        current.clear();
    }
}

/// Same word, allowing a plural `s` or `es` on either side.
fn token_matches(a: &str, b: &str) -> bool {
    a == b
        || a.strip_suffix('s') == Some(b)
        || b.strip_suffix('s') == Some(a)
        || a.strip_suffix("es") == Some(b)
        || b.strip_suffix("es") == Some(a)
}

fn is_stopword(word: &str) -> bool {
    STOPWORDS.contains(&word)
}

#[derive(Clone, Copy)]
enum Tier {
    Identified = 0,
    Described = 1,
    Located = 2,
}

struct Evidence {
    tier: Tier,
    /// The task spells this name exactly as written.
    verbatim: bool,
    /// The task spells the whole name, ignoring case and separators.
    named: bool,
    task_coverage: f64,
    name_coverage: f64,
}

/// A helper defined inside a test function (`test_x.readiness`), not a test.
fn is_nested_test_helper(qualified_name: &str) -> bool {
    let mut segments: Vec<&str> = qualified_name
        .split(['.', ':'])
        .filter(|segment| !segment.is_empty())
        .collect();
    segments.pop();
    segments.iter().any(|segment| segment.starts_with("test"))
}

/// A test: under a `test`/`tests` directory, in a `test_*`/`*_test` file, or
/// named `test*`.
fn is_test_symbol(row: &SearchRow) -> bool {
    let mut segments: Vec<&str> = row.file_path.split('/').collect();
    let file = segments.pop().unwrap_or_default();
    let stem = file.split('.').next().unwrap_or(file);
    segments
        .iter()
        .any(|segment| *segment == "test" || *segment == "tests")
        || stem.starts_with("test_")
        || stem.ends_with("_test")
        || row.name.to_lowercase().starts_with("test")
}

/// Vendored, bundled, or minified code.
fn is_vendored(file_path: &str) -> bool {
    let path = format!("/{}", file_path.to_lowercase());
    ["/node_modules/", "/vendor/", "/third_party/", "/dist/"]
        .iter()
        .any(|dir| path.contains(dir))
        || path.contains(".min.")
}

/// Distinctive task words that appear among `tokens`.
fn words_in<'a>(distinctive: &'a [String], tokens: &[String]) -> Vec<&'a String> {
    distinctive
        .iter()
        .filter(|word| tokens.iter().any(|token| token_matches(token, word)))
        .collect()
}

fn evidence(
    row: &SearchRow,
    terms: &TaskTerms,
    mentioned: &HashSet<String>,
    name_counts: &HashMap<String, usize>,
    params: FloorParams,
) -> Option<Evidence> {
    let name = name_tokens(&row.name);
    let compact_name = compact(&row.name);
    // An exact name identifies a symbol only when no other symbol shares it,
    // ignoring case and separators: `run_dir` next to several `_run_dir`
    // helpers, `__init__` or `main` point at nothing in particular. Shared
    // names still count as ordinary evidence below.
    let unique = name_counts.get(&compact_name).copied().unwrap_or(0) <= 1;
    let verbatim = unique && terms.identifiers.contains(&row.name);
    // A compound name spelled out in full, in any case and with or without
    // separators, names the symbol. A one-word name stays a plain word.
    let exact = verbatim
        || (unique
            && !name.is_empty()
            && terms
                .identifiers
                .iter()
                .any(|ident| name_tokens(ident) == name))
        || (unique && name.len() >= 2 && terms.compact.contains(&compact_name));
    if exact || mentioned.contains(&row.file_path) {
        return Some(Evidence {
            tier: Tier::Identified,
            verbatim,
            named: true,
            task_coverage: 1.0,
            name_coverage: 1.0,
        });
    }
    if terms.docs
        || terms.distinctive.is_empty()
        || is_nested_test_helper(&row.qualified_name)
        || is_vendored(&row.file_path)
    {
        return None;
    }
    let name: Vec<String> = name
        .into_iter()
        .filter(|token| token != "test" && token != "tests")
        .collect();
    let mut segments: Vec<&str> = row.file_path.split('/').collect();
    let file = segments.pop().unwrap_or_default();
    let stem = file.split('.').next().unwrap_or(file);
    let path_tokens: Vec<String> = segments
        .iter()
        .flat_map(|segment| name_tokens(segment))
        .chain(name_tokens(stem))
        .filter(|token| !PATH_NOISE.contains(&token.as_str()))
        .collect();
    // A task word that spells the whole name without separators
    // (`codegraphbrief`) matches the name as a whole.
    let whole_name = name.len() >= 2 && terms.compact.contains(&compact_name);
    let mut in_name = words_in(&terms.distinctive, &name);
    if whole_name && !in_name.contains(&&compact_name) {
        in_name.extend(
            terms
                .distinctive
                .iter()
                .filter(|word| **word == compact_name),
        );
    }
    let in_path = words_in(&terms.distinctive, &path_tokens);
    let explained = terms
        .distinctive
        .iter()
        .filter(|word| in_name.contains(word) || in_path.contains(word))
        .count();
    let task_coverage = explained as f64 / terms.distinctive.len() as f64;
    let name_coverage = if whole_name {
        1.0
    } else if name.is_empty() {
        0.0
    } else {
        name.iter()
            .filter(|token| {
                terms
                    .distinctive
                    .iter()
                    .any(|word| token_matches(token, word))
            })
            .count() as f64
            / name.len() as f64
    };
    let tier = if !in_name.is_empty()
        && task_coverage >= params.min_task_coverage
        && name_coverage >= params.min_name_coverage
    {
        Tier::Described
    } else if located_by_task(&segments, stem, &in_name, &terms.distinctive) {
        Tier::Located
    } else {
        return None;
    };
    Some(Evidence {
        tier,
        verbatim: false,
        named: whole_name,
        task_coverage,
        name_coverage,
    })
}

/// The task words that spell out every token of a path segment, if any do.
fn segment_named(segment: &str, distinctive: &[String]) -> Option<Vec<String>> {
    let tokens = name_tokens(segment);
    if tokens.is_empty() {
        return None;
    }
    tokens
        .iter()
        .map(|token| {
            distinctive
                .iter()
                .find(|word| token_matches(token, word))
                .cloned()
        })
        .collect()
}

/// The task names a directory the hit lives in, or names the hit's file stem and
/// a second distinctive word of the task appears in the hit's name.
fn located_by_task(
    directories: &[&str],
    stem: &str,
    in_name: &[&String],
    distinctive: &[String],
) -> bool {
    if directories
        .iter()
        .any(|dir| segment_named(dir, distinctive).is_some())
    {
        return true;
    }
    match segment_named(stem, distinctive) {
        Some(stem_words) => in_name.iter().any(|word| !stem_words.contains(word)),
        None => false,
    }
}

fn mentioned_files(conn: &Connection, paths: &[String]) -> Result<HashSet<String>> {
    let mut mentioned = HashSet::new();
    if paths.is_empty() {
        return Ok(mentioned);
    }
    let mut stmt = conn.prepare("SELECT path FROM files")?;
    for row in stmt.query_map([], |row| row.get::<_, String>(0))? {
        let path = row?;
        let lower = path.to_lowercase();
        let named = paths.iter().any(|token| {
            lower == *token
                || lower.ends_with(&format!("/{token}"))
                || token.ends_with(&format!("/{lower}"))
        });
        if named {
            mentioned.insert(path);
        }
    }
    Ok(mentioned)
}

/// Symbols whose name, stripped of case and separators, equals a task token,
/// plus every symbol in a file the task names. Keyword rank does not matter.
fn lookup_candidates(
    conn: &Connection,
    terms: &TaskTerms,
    mentioned: &HashSet<String>,
) -> Result<Vec<SearchRow>> {
    let mut extra = Vec::new();
    // Exact-name lookups bypass keyword rank, one bounded query per chunk of keys.
    for keys in terms.compact.chunks(256) {
        let placeholders = vec!["?"; keys.len()].join(", ");
        let sql = format!(
            "SELECT id, kind, name, qualified_name, file_path, start_line, end_line, signature, 0.0
             FROM symbols
             WHERE replace(replace(replace(lower(name), '_', ''), '-', ''), '$', '') IN ({placeholders})
             ORDER BY file_path, start_line
             LIMIT {LOOKUP_LIMIT}"
        );
        let mut stmt = conn.prepare(&sql)?;
        let rows = stmt.query_map(rusqlite::params_from_iter(keys.iter()), search_row_from_sql)?;
        for row in rows {
            extra.push(row?);
        }
    }
    if !mentioned.is_empty() {
        let mut stmt = conn.prepare(
            "SELECT id, kind, name, qualified_name, file_path, start_line, end_line, signature, 0.0
             FROM symbols WHERE file_path = ?1 ORDER BY start_line LIMIT ?2",
        )?;
        let mut files: Vec<&String> = mentioned.iter().collect();
        files.sort();
        for file in files {
            let rows = stmt.query_map(
                params![file, sqlite_limit(LOOKUP_LIMIT)],
                search_row_from_sql,
            )?;
            for row in rows {
                extra.push(row?);
            }
        }
    }
    Ok(extra)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::store::init_schema;

    fn index_symbols(conn: &Connection, symbols: &[(&str, &str, &str)]) {
        for (id, name, file_path) in symbols {
            conn.execute(
                "INSERT OR IGNORE INTO files(path, content_hash, size, modified_at, indexed_at, language)
                 VALUES (?1, 'hash', 1, 1, 1, 'python')",
                params![file_path],
            )
            .unwrap();
            conn.execute(
                "INSERT INTO symbols(id, kind, name, qualified_name, file_path, start_line, end_line, signature, content_hash)
                 VALUES (?1, 'function', ?2, ?2, ?3, 1, 2, ?2, 'hash')",
                params![id, name, file_path],
            )
            .unwrap();
            conn.execute(
                "INSERT INTO symbols_fts(symbol_id, name, qualified_name, signature, file_path)
                 VALUES (?1, ?2, ?2, ?2, ?3)",
                params![id, name, file_path],
            )
            .unwrap();
        }
    }

    fn floored_ids(conn: &Connection, task: &str) -> Vec<String> {
        floored_search(conn, task, 8, FloorParams::CALIBRATED)
            .unwrap()
            .rows
            .into_iter()
            .map(|row| row.id)
            .collect()
    }

    fn floor_fixture() -> Connection {
        let conn = Connection::open_in_memory().unwrap();
        init_schema(&conn).unwrap();
        index_symbols(
            &conn,
            &[
                (
                    "pipx_error",
                    "_is_transient_pipx_install_error",
                    "scripts/accept.py",
                ),
                (
                    "readme_brand",
                    "test_readme_header_matches_brand",
                    "tests/test_readme.py",
                ),
                ("install_a", "install", "src/skills/install.py"),
                ("install_b", "install", "src/care.py"),
                ("security_fix", "fix", "src/security/config.py"),
                (
                    "changelog",
                    "_changelog_unreleased",
                    "src/release/candidate.py",
                ),
                (
                    "typo_test",
                    "test_prose_fix_typo_still_routes_docs",
                    "tests/test_router.py",
                ),
                ("extract", "extract_delta_files", "src/context_eval.py"),
                (
                    "extract_test",
                    "test_extract_delta_files_still_reads_legacy_node_keys",
                    "tests/test_context_eval.py",
                ),
                ("hook_sidecar", "_sidecar", "src/hooks/install_cmd.py"),
                ("hub_client", "HubClient", "src/fleet/hub_client.py"),
                ("render", "render_markdown", "src/query/context.rs"),
            ],
        );
        conn
    }

    #[test]
    fn floor_rejects_tasks_whose_words_only_brush_against_symbol_names() {
        let conn = floor_fixture();
        for task in [
            "Update the README install section to mention pipx",
            "fix typo in CHANGELOG",
        ] {
            let floored = floored_search(&conn, task, 8, FloorParams::CALIBRATED).unwrap();
            assert!(
                floored.rows.is_empty(),
                "{task} kept {:?}",
                floored.rows.iter().map(|row| &row.name).collect::<Vec<_>>()
            );
            assert!(
                floored.floor.candidates > 0,
                "{task} had no candidates to drop"
            );
            assert_eq!(floored.floor.dropped, floored.floor.candidates);
            assert_eq!(floored.floor.kept, 0);
            assert_eq!(floored.floor.rule, RELEVANCE_FLOOR_RULE);
        }
    }

    #[test]
    fn floor_puts_an_exactly_named_symbol_first_and_drops_its_noisy_neighbours() {
        let conn = floor_fixture();
        let ids = floored_ids(
            &conn,
            "fix extract_delta_files reading the wrong sidecar keys",
        );
        // The exact name comes first. Its test explains four of the task's seven
        // distinctive words, so it stays. One-word `_sidecar` explains one.
        assert_eq!(ids, vec!["extract".to_string(), "extract_test".to_string()]);
    }

    #[test]
    fn floor_keeps_a_type_whose_whole_name_the_task_spells_out_in_words() {
        let conn = floor_fixture();
        let ids = floored_ids(
            &conn,
            "the hub client should retry when a request times out",
        );
        assert_eq!(ids, vec!["hub_client".to_string()]);
    }

    #[test]
    fn floor_keeps_symbols_in_a_file_the_task_names() {
        let conn = floor_fixture();
        let ids = floored_ids(&conn, "tighten the floor in src/query/context.rs");
        assert_eq!(ids, vec!["render".to_string()]);
    }

    #[test]
    fn floor_finds_a_compound_name_typed_in_any_case_or_without_separators() {
        let conn = Connection::open_in_memory().unwrap();
        init_schema(&conn).unwrap();
        index_symbols(
            &conn,
            &[
                ("brief_type", "CodeGraphBrief", "src/briefs.py"),
                ("brief_fn", "code_graph_brief", "src/briefs.py"),
                ("drift_type", "DriftImpactBrief", "src/briefs.py"),
                ("install_a", "install", "src/a.py"),
                ("install_b", "install", "src/b.py"),
            ],
        );
        for task in [
            "codegraphbrief",
            "CODEGRAPHBRIEF",
            "fix the codegraphbrief truncation",
            "rename CODE_GRAPH_BRIEF",
        ] {
            let mut ids = floored_ids(&conn, task);
            ids.sort();
            assert_eq!(ids, vec!["brief_fn", "brief_type"], "{task}");
        }
        // A one-word name stays a plain word: matching it exactly is not
        // evidence that the task names that symbol.
        assert!(floored_ids(&conn, "Update the README install section").is_empty());
    }

    #[test]
    fn floor_finds_an_exact_identifier_outside_the_keyword_candidate_pool() {
        let conn = Connection::open_in_memory().unwrap();
        init_schema(&conn).unwrap();
        let mut symbols = Vec::new();
        for index in 0..300 {
            symbols.push((
                format!("noise{index}"),
                format!("test_widget_parse_{index}_widget_parse_widget"),
                format!("tests/test_widget_{index}.py"),
            ));
        }
        symbols.push((
            "target".to_string(),
            "widget_parse".to_string(),
            "src/widget_parser_module_with_a_long_path.py".to_string(),
        ));
        let borrowed: Vec<(&str, &str, &str)> = symbols
            .iter()
            .map(|(id, name, path)| (id.as_str(), name.as_str(), path.as_str()))
            .collect();
        index_symbols(&conn, &borrowed);

        let ids = floored_ids(&conn, "widget_parse");

        assert_eq!(ids.first().map(String::as_str), Some("target"));
    }

    #[test]
    fn floor_keeps_names_that_hold_every_distinctive_word_of_a_keyword_query() {
        let conn = Connection::open_in_memory().unwrap();
        init_schema(&conn).unwrap();
        index_symbols(
            &conn,
            &[
                ("alpha", "evidence_alpha", "evidence.py"),
                ("beta", "evidence_beta", "evidence.py"),
                ("other", "ledger_alpha", "ledger.py"),
            ],
        );
        assert_eq!(floored_ids(&conn, "evidence"), vec!["alpha", "beta"]);
        // Each name explains one of three distinctive words, under the task floor.
        assert!(floored_ids(&conn, "rotate the evidence ledger archives").is_empty());
    }

    #[test]
    fn a_one_word_identifier_must_name_a_single_symbol() {
        let conn = Connection::open_in_memory().unwrap();
        init_schema(&conn).unwrap();
        index_symbols(
            &conn,
            &[
                ("init_a", "__init__", "src/a.py"),
                ("init_b", "__init__", "src/b.py"),
                ("init_c", "__init__", "src/c.py"),
                ("layout", "_layout", "src/hooks/install_cmd.py"),
            ],
        );

        assert!(floored_ids(&conn, "__init__ re-exports drop edges").is_empty());
        assert_eq!(
            floored_ids(&conn, "the _layout probe misreads"),
            vec!["layout"]
        );
    }

    #[test]
    fn floor_looks_past_the_first_keyword_rows_before_truncating() {
        let conn = Connection::open_in_memory().unwrap();
        init_schema(&conn).unwrap();
        let mut symbols: Vec<(String, String, String)> = (0..30)
            .map(|index| {
                (
                    format!("bundle{index}"),
                    "rotate_operator_report_dirs".to_string(),
                    format!("dist/b{index:02}.js"),
                )
            })
            .collect();
        symbols.push((
            "real".to_string(),
            "rotate_operator_report_dirs".to_string(),
            "src/zz/module.py".to_string(),
        ));
        let borrowed: Vec<(&str, &str, &str)> = symbols
            .iter()
            .map(|(id, name, path)| (id.as_str(), name.as_str(), path.as_str()))
            .collect();
        index_symbols(&conn, &borrowed);
        let task = "center report build: rotate old operator report dirs automatically";
        let keyword = search_symbols(&conn, task, 8).unwrap();
        assert!(
            keyword.iter().all(|row| row.id != "real"),
            "fixture must rank real past 8"
        );

        assert_eq!(floored_ids(&conn, task), vec!["real"]);
    }

    #[test]
    fn a_shared_compound_name_is_not_identified_by_itself() {
        let conn = Connection::open_in_memory().unwrap();
        init_schema(&conn).unwrap();
        index_symbols(
            &conn,
            &[
                ("dir_a", "run_dir", "src/brigade/research/registry.py"),
                ("dir_b", "_run_dir", "tests/test_run_lifecycle.py"),
                ("dir_c", "_run_dir", "tests/test_run_journal.py"),
                ("dir_d", "_run_dir", "tests/test_run_shadow.py"),
                (
                    "unique",
                    "release_claim_without_force",
                    "src/brigade/fleet_claims.py",
                ),
            ],
        );

        let ids = floored_ids(
            &conn,
            "fleet claims --release follow-ups: NULL run_dir claims need a non-force path",
        );

        assert_eq!(ids, vec!["unique"]);
    }

    #[test]
    fn located_code_joins_when_only_tests_matched() {
        let conn = Connection::open_in_memory().unwrap();
        init_schema(&conn).unwrap();
        index_symbols(
            &conn,
            &[
                (
                    "test",
                    "dispatch_reaches_each_handler",
                    "rust/tests/impact.rs",
                ),
                ("hub", "dispatch", "rust/src/lib.rs"),
                ("other", "unrelated_helper", "python/service.py"),
            ],
        );

        let ids = floored_ids(
            &conn,
            "Trace the central Rust routing hub and identify its downstream handlers.",
        );

        assert_eq!(ids, vec!["test", "hub"]);
    }

    #[test]
    fn floor_needs_the_hit_to_explain_enough_of_the_task() {
        let conn = Connection::open_in_memory().unwrap();
        init_schema(&conn).unwrap();
        index_symbols(
            &conn,
            &[
                ("compose", "_compose", "src/brigade/memory_proposals.py"),
                ("hub_error", "FleetHubError", "src/brigade/fleet_hub.py"),
                ("image", "build_hub_image", "src/brigade/fleet/hub_image.py"),
            ],
        );

        let ids = floored_ids(
            &conn,
            "feat(fleet): provide persistent OCI Hub image and Compose/Podman recipes",
        );

        assert_eq!(ids, vec!["image"]);
    }

    #[test]
    fn floor_drops_nested_test_helpers_and_vendored_code() {
        let conn = Connection::open_in_memory().unwrap();
        init_schema(&conn).unwrap();
        index_symbols(
            &conn,
            &[
                (
                    "helper",
                    "test_bound_swap_cannot_authorize.browser_preflight_readiness",
                    "tests/test_run_io.py",
                ),
                (
                    "bundled",
                    "browser_preflight_readiness",
                    "plugin/dist/main.js",
                ),
                ("minified", "browser_preflight_readiness", "web/app.min.js"),
                (
                    "real",
                    "require_browser_preflight",
                    "src/research/preflight.py",
                ),
            ],
        );

        let ids = floored_ids(
            &conn,
            "feat(research): require named browser preflight before projecting execution readiness",
        );

        assert_eq!(ids, vec!["real"]);
    }

    #[test]
    fn docs_tasks_keep_only_symbols_they_name() {
        let conn = Connection::open_in_memory().unwrap();
        init_schema(&conn).unwrap();
        index_symbols(
            &conn,
            &[
                (
                    "quickstart",
                    "hub_quickstart",
                    "src/brigade/hub_quickstart.py",
                ),
                ("render", "render_quickstart", "src/brigade/docs_render.py"),
            ],
        );

        assert!(floored_ids(&conn, "docs(fleet): add native Hub quickstart").is_empty());
        assert!(floored_ids(&conn, "explain the hub quickstart in docs/hub.md").is_empty());
        assert_eq!(
            floored_ids(&conn, "docs: describe render_quickstart output"),
            vec!["render"]
        );
    }

    #[test]
    fn floor_keeps_symbols_located_by_a_named_directory_or_file_plus_a_name_word() {
        let conn = Connection::open_in_memory().unwrap();
        init_schema(&conn).unwrap();
        index_symbols(
            &conn,
            &[
                ("route", "routeRequest", "typescript/router.ts"),
                ("handle", "handleRequest", "typescript/service.ts"),
                ("authorize", "authorize", "typescript/service.ts"),
                ("dispatch", "dispatch", "rust/src/lib.rs"),
                ("selection", "install_selection", "src/install.py"),
            ],
        );

        let mut ids = floored_ids(
            &conn,
            "Find the request router and the service function it invokes.",
        );
        ids.sort();
        assert_eq!(ids, vec!["handle", "route"]);
        assert_eq!(
            floored_ids(&conn, "Trace the central Rust routing hub"),
            vec!["dispatch"]
        );
        assert!(floored_ids(&conn, "Update the README install section").is_empty());
    }

    #[test]
    fn name_tokens_split_snake_camel_and_digits() {
        assert_eq!(
            name_tokens("_is_transientPipxInstall2Error"),
            vec!["is", "transient", "pipx", "install2", "error"]
        );
        assert_eq!(name_tokens("HTTPServer"), vec!["httpserver"]);
        assert_eq!(name_tokens("a"), Vec::<String>::new());
    }
}
