//! Relevance floor for task search (brigade#1648).
//!
//! Keyword search ORs every word of a task sentence, so words like "update" or
//! "install" pull in symbols from anywhere in the repository, and the top `limit`
//! rows used to be returned whatever their quality. The floor keeps a hit only
//! when the task carries evidence for it:
//!
//! 1. identified: the task names the hit's file, spells the hit's name as a code
//!    identifier, or a code identifier in the task covers enough of a multi-word
//!    name;
//! 2. described: plain task words cover enough of the hit's name and at least one
//!    of them is not a generic task word, or the hit's name holds every
//!    distinctive word of the task (a keyword query like `evidence`). A one-word
//!    name must also be unique among candidates;
//! 3. located: the task names a directory the hit lives in ("the Rust routing
//!    hub"), or names its file stem while a second distinctive task word appears
//!    in its name ("the request router" for `routeRequest` in `router.ts`).
//!
//! Only the best tier with any hit is kept. When no hit clears the floor the pack
//! is empty and marked not confident.

use std::cmp::Ordering;
use std::collections::{HashMap, HashSet};

use anyhow::Result;
use rusqlite::{Connection, params};

use super::{search_row_from_sql, search_symbols, sqlite_limit};
use crate::model::{RelevanceFloor, SearchRow};

/// Name of the rule `floored_search` applies, recorded on every context pack.
pub const RELEVANCE_FLOOR_RULE: &str = "name-coverage-v1";

/// Minimum share of a symbol's name tokens the task must cover. Calibrated by the
/// sweep in `tests/context_ranking_benchmark.rs`, which fails if this drifts.
pub const NAME_COVERAGE_FLOOR: f64 = 0.55;

/// Most keyword candidates the floor scores. A cost guard only: exact identifier
/// and named-file lookups add their symbols regardless of keyword rank.
const CANDIDATE_POOL: usize = 4096;

/// Generic words that never count as evidence on their own: English function
/// words plus the verbs and nouns most task titles use for any kind of change.
#[rustfmt::skip]
const STOPWORDS: &[&str] = &[
    "a", "about", "add", "added", "adds", "after", "all", "also", "an", "and", "any", "are", "as",
    "at", "be", "before", "but", "by", "can", "change", "changed", "changes", "check", "could",
    "do", "does", "doc", "docs", "ensure", "explain", "find", "fix", "fixed", "fixes", "for",
    "from", "go", "has", "have", "how", "identify", "if", "in", "inspect", "into", "investigate",
    "is", "it", "its", "look", "make", "makes", "mention", "mentions", "more", "new", "no", "not",
    "of", "old", "on", "only", "or", "out", "remove", "removed", "removes", "section", "should",
    "show", "so", "some", "support", "test", "tests", "than", "that", "the", "their", "them",
    "then", "there", "these", "this", "those", "to", "trace", "update", "updated", "updates",
    "use", "used", "uses", "via", "was", "were", "when", "where", "which", "while", "will",
    "with", "would",
];

/// Extensions that make a task token a file reference rather than a dotted name.
#[rustfmt::skip]
const FILE_EXTENSIONS: &[&str] = &[
    "astro", "bash", "c", "cc", "cjs", "cpp", "cs", "css", "fish", "go", "h", "hpp", "html",
    "java", "js", "json", "jsx", "kt", "kts", "md", "mjs", "php", "py", "pyi", "rb", "rs",
    "scala", "sh", "sql", "svelte", "swift", "toml", "ts", "tsx", "txt", "vue", "yaml", "yml",
    "zsh",
];

/// Search hits that cleared the relevance floor, plus how the floor was applied.
#[derive(Debug)]
pub struct FlooredSearch {
    pub rows: Vec<SearchRow>,
    pub floor: RelevanceFloor,
}

/// Search the task text and keep at most `limit` hits that clear the floor.
pub fn floored_search(
    conn: &Connection,
    task: &str,
    limit: usize,
    min_name_coverage: f64,
) -> Result<FlooredSearch> {
    let terms = TaskTerms::parse(task);
    let mentioned = mentioned_files(conn, &terms.paths)?;
    let pool = candidate_pool(conn, task, &terms, &mentioned)?;
    let mut name_counts: HashMap<Vec<String>, usize> = HashMap::new();
    for row in &pool {
        *name_counts.entry(name_tokens(&row.name)).or_default() += 1;
    }
    let mut tiers: [Vec<(Evidence, &SearchRow)>; 3] = Default::default();
    for row in &pool {
        if let Some(found) = evidence(row, &terms, &mentioned, &name_counts, min_name_coverage) {
            tiers[found.tier as usize].push((found, row));
        }
    }
    let mut kept = tiers
        .into_iter()
        .find(|tier| !tier.is_empty())
        .unwrap_or_default();
    kept.sort_by(|(a_ev, a), (b_ev, b)| {
        b_ev.verbatim
            .cmp(&a_ev.verbatim)
            .then_with(|| b_ev.exact.cmp(&a_ev.exact))
            .then_with(|| b_ev.coverage.total_cmp(&a_ev.coverage))
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
    Ok(FlooredSearch {
        rows,
        floor: RelevanceFloor {
            rule: RELEVANCE_FLOOR_RULE.to_string(),
            min_name_coverage,
            candidates: pool.len(),
            kept: survivors,
            dropped: pool.len() - survivors,
        },
    })
}

#[derive(Debug, Default)]
struct TaskTerms {
    /// Lowercase word tokens of the whole task.
    words: Vec<String>,
    /// Words outside file references that are not stopwords, deduplicated.
    distinctive: Vec<String>,
    /// Lowercase word tokens of code-shaped identifiers only.
    identifier_words: Vec<String>,
    /// Identifier segments as written (`Foo::bar` gives `Foo` and `bar`).
    identifiers: Vec<String>,
    /// Lowercase file references (`src/a.rs`, `context.rs`).
    paths: Vec<String>,
    /// Every non-path token and identifier segment, lowercased with separators
    /// removed, so `codegraphbrief` and `CODE_GRAPH_BRIEF` both name `CodeGraphBrief`.
    compact: Vec<String>,
}

impl TaskTerms {
    fn parse(task: &str) -> Self {
        let mut terms = TaskTerms::default();
        for raw in task.split_whitespace() {
            let token = raw.trim_matches(|c: char| !(c.is_alphanumeric() || c == '_'));
            if token.is_empty() {
                continue;
            }
            let words = name_tokens(token);
            terms.words.extend(words.iter().cloned());
            if is_file_reference(token) {
                terms.paths.push(token.to_lowercase());
                continue;
            }
            for word in words {
                if !is_stopword(&word) && !terms.distinctive.contains(&word) {
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
                terms.identifier_words.extend(name_tokens(token));
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
    /// The task names this symbol as an identifier, ignoring case and separators.
    exact: bool,
    coverage: f64,
}

/// Share of `name` tokens matched by `words`, and whether any match is not a stopword.
fn coverage(name: &[String], words: &[String]) -> (f64, bool) {
    if name.is_empty() {
        return (0.0, false);
    }
    let mut covered = 0usize;
    let mut distinctive = false;
    for token in name {
        let mut matched = false;
        for word in words.iter().filter(|word| token_matches(token, word)) {
            matched = true;
            distinctive |= !is_stopword(word);
        }
        covered += usize::from(matched);
    }
    (covered as f64 / name.len() as f64, distinctive)
}

fn evidence(
    row: &SearchRow,
    terms: &TaskTerms,
    mentioned: &HashSet<String>,
    name_counts: &HashMap<Vec<String>, usize>,
    min_name_coverage: f64,
) -> Option<Evidence> {
    let name = name_tokens(&row.name);
    let verbatim = terms.identifiers.contains(&row.name);
    // A compound name spelled out in full, in any case and with or without
    // separators, names the symbol. A one-word name stays a plain word.
    let exact = verbatim
        || (!name.is_empty()
            && terms
                .identifiers
                .iter()
                .any(|ident| name_tokens(ident) == name))
        || (name.len() >= 2 && terms.compact.contains(&compact(&row.name)));
    let (identifier_coverage, _) = coverage(&name, &terms.identifier_words);
    let identifier_covered =
        name.len() >= 2 && identifier_coverage > 0.0 && identifier_coverage >= min_name_coverage;
    if exact || identifier_covered || mentioned.contains(&row.file_path) {
        return Some(Evidence {
            tier: Tier::Identified,
            verbatim,
            exact,
            coverage: identifier_coverage,
        });
    }
    let (word_coverage, distinctive) = coverage(&name, &terms.words);
    let covered = distinctive && word_coverage >= min_name_coverage;
    let holds_task = !terms.distinctive.is_empty()
        && terms
            .distinctive
            .iter()
            .all(|word| name.iter().any(|token| token_matches(token, word)));
    let unambiguous = name.len() >= 2 || name_counts.get(&name).copied().unwrap_or(0) <= 1;
    let tier = if (covered || holds_task) && unambiguous {
        Tier::Described
    } else if located_by_task(row, &name, &terms.distinctive) {
        Tier::Located
    } else {
        return None;
    };
    Some(Evidence {
        tier,
        verbatim: false,
        exact: false,
        coverage: word_coverage,
    })
}

/// The task words that spell out every token of a path segment, if any do.
fn segment_words(segment: &str, distinctive: &[String]) -> Option<Vec<String>> {
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
fn located_by_task(row: &SearchRow, name: &[String], distinctive: &[String]) -> bool {
    let mut segments: Vec<&str> = row.file_path.split('/').collect();
    let Some(file) = segments.pop() else {
        return false;
    };
    if segments
        .iter()
        .any(|dir| segment_words(dir, distinctive).is_some())
    {
        return true;
    }
    let stem = file.split('.').next().unwrap_or(file);
    let Some(stem_words) = segment_words(stem, distinctive) else {
        return false;
    };
    name.iter().any(|token| {
        distinctive
            .iter()
            .any(|word| !stem_words.contains(word) && token_matches(token, word))
    })
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

fn candidate_pool(
    conn: &Connection,
    task: &str,
    terms: &TaskTerms,
    mentioned: &HashSet<String>,
) -> Result<Vec<SearchRow>> {
    let mut pool = search_symbols(conn, task, CANDIDATE_POOL)?;
    let mut seen: HashSet<String> = pool.iter().map(|row| row.id.clone()).collect();
    let mut extra = Vec::new();
    // Exact-name lookups bypass keyword rank, one bounded query per chunk of keys.
    for keys in terms.compact.chunks(256) {
        let placeholders = vec!["?"; keys.len()].join(", ");
        let sql = format!(
            "SELECT id, kind, name, qualified_name, file_path, start_line, end_line, signature, 0.0
             FROM symbols
             WHERE replace(replace(replace(lower(name), '_', ''), '-', ''), '$', '') IN ({placeholders})
             ORDER BY file_path, start_line
             LIMIT {CANDIDATE_POOL}"
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
                params![file, sqlite_limit(CANDIDATE_POOL)],
                search_row_from_sql,
            )?;
            for row in rows {
                extra.push(row?);
            }
        }
    }
    for row in extra {
        if seen.insert(row.id.clone()) {
            pool.push(row);
        }
    }
    Ok(pool)
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
        floored_search(conn, task, 8, NAME_COVERAGE_FLOOR)
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
            let floored = floored_search(&conn, task, 8, NAME_COVERAGE_FLOOR).unwrap();
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
        assert_eq!(ids, vec!["extract".to_string()]);
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
        assert!(floored_ids(&conn, "update the evidence ledger docs").is_empty());
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
