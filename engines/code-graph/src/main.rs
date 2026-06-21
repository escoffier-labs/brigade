use std::collections::{BTreeMap, HashMap, HashSet};
use std::fs;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use anyhow::{Context, Result, anyhow};
use clap::{Parser, Subcommand};
use regex::Regex;
use rusqlite::{Connection, params};
use serde::Serialize;
use sha2::{Digest, Sha256};
use tree_sitter::{Language, Node as TsNode, Parser as TsParser};
use walkdir::{DirEntry, WalkDir};

#[derive(Parser)]
#[command(version, about)]
struct Cli {
    #[arg(long, global = true, value_name = "PATH")]
    db: Option<PathBuf>,

    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    Init {
        #[arg(default_value = ".")]
        root: PathBuf,
    },
    Sync {
        #[arg(default_value = ".")]
        root: PathBuf,
    },
    Search {
        query: String,
        #[arg(long, default_value_t = 20)]
        limit: usize,
        #[arg(long)]
        json: bool,
    },
    Callers {
        symbol: String,
        #[arg(long)]
        json: bool,
    },
    Callees {
        symbol: String,
        #[arg(long)]
        json: bool,
    },
    Impact {
        symbol: String,
        #[arg(long)]
        json: bool,
    },
    Context {
        task: String,
        #[arg(long, default_value_t = 12)]
        limit: usize,
        #[arg(long)]
        json: bool,
    },
    Stats {
        #[arg(long)]
        json: bool,
    },
}

#[derive(Debug, Clone, Serialize)]
struct Symbol {
    id: String,
    kind: String,
    name: String,
    qualified_name: String,
    file_path: String,
    start_line: usize,
    end_line: usize,
    signature: String,
    container: Option<String>,
    content_hash: String,
}

#[derive(Debug)]
struct PendingCall {
    source_id: String,
    target_name: String,
    line: usize,
}

#[derive(Debug)]
struct FileGraph {
    path: String,
    language: String,
    hash: String,
    size: u64,
    modified_at: i64,
    symbols: Vec<Symbol>,
    imports: Vec<(String, usize)>,
    calls: Vec<PendingCall>,
}

#[derive(Debug, Serialize)]
struct EdgeRow {
    source_id: String,
    source: String,
    target_id: String,
    target: String,
    kind: String,
    line: Option<usize>,
    source_file: String,
    target_file: String,
}

#[derive(Debug, Serialize)]
struct SearchRow {
    id: String,
    kind: String,
    name: String,
    qualified_name: String,
    file_path: String,
    start_line: usize,
    end_line: usize,
    signature: String,
    score: f64,
}

#[derive(Debug, Serialize)]
struct ContextPack {
    task: String,
    entry_points: Vec<SearchRow>,
    callers: Vec<EdgeRow>,
    callees: Vec<EdgeRow>,
    related_files: Vec<String>,
}

fn main() -> Result<()> {
    let cli = Cli::parse();

    match cli.command {
        Command::Init { root } => {
            let db_path = db_path(cli.db, &root);
            let conn = open_db(&db_path)?;
            init_schema(&conn)?;
            println!("initialized {}", db_path.display());
        }
        Command::Sync { root } => {
            let db_path = db_path(cli.db, &root);
            let conn = open_db(&db_path)?;
            init_schema(&conn)?;
            let summary = sync_repo(&conn, &root)?;
            println!(
                "indexed files={} symbols={} calls={} imports={} db={}",
                summary.files,
                summary.symbols,
                summary.calls,
                summary.imports,
                db_path.display()
            );
        }
        Command::Search { query, limit, json } => {
            let conn = open_default(cli.db)?;
            let rows = search_symbols(&conn, &query, limit)?;
            print_json_or_symbols(json, &rows)?;
        }
        Command::Callers { symbol, json } => {
            let conn = open_default(cli.db)?;
            let edges = graph_edges(&conn, &symbol, Direction::Incoming)?;
            print_json_or_edges(json, &edges)?;
        }
        Command::Callees { symbol, json } => {
            let conn = open_default(cli.db)?;
            let edges = graph_edges(&conn, &symbol, Direction::Outgoing)?;
            print_json_or_edges(json, &edges)?;
        }
        Command::Impact { symbol, json } => {
            let conn = open_default(cli.db)?;
            let mut edges = graph_edges(&conn, &symbol, Direction::Incoming)?;
            edges.extend(graph_edges(&conn, &symbol, Direction::Outgoing)?);
            edges.sort_by(|a, b| {
                a.source_file
                    .cmp(&b.source_file)
                    .then_with(|| a.source.cmp(&b.source))
                    .then_with(|| a.target.cmp(&b.target))
            });
            print_json_or_edges(json, &edges)?;
        }
        Command::Context { task, limit, json } => {
            let conn = open_default(cli.db)?;
            let entry_points = search_symbols(&conn, &task, limit)?;
            let mut callers = Vec::new();
            let mut callees = Vec::new();
            let mut files = HashSet::new();
            for row in &entry_points {
                files.insert(row.file_path.clone());
                callers.extend(edges_for_symbol_id(&conn, &row.id, Direction::Incoming)?);
                callees.extend(edges_for_symbol_id(&conn, &row.id, Direction::Outgoing)?);
            }
            for edge in callers.iter().chain(callees.iter()) {
                files.insert(edge.source_file.clone());
                files.insert(edge.target_file.clone());
            }
            let mut related_files: Vec<String> = files.into_iter().collect();
            related_files.sort();
            let pack = ContextPack {
                task,
                entry_points,
                callers,
                callees,
                related_files,
            };
            if json {
                println!("{}", serde_json::to_string_pretty(&pack)?);
            } else {
                print_context(&pack);
            }
        }
        Command::Stats { json } => {
            let conn = open_default(cli.db)?;
            let stats = stats(&conn)?;
            if json {
                println!("{}", serde_json::to_string_pretty(&stats)?);
            } else {
                for (key, value) in stats {
                    println!("{key}: {value}");
                }
            }
        }
    }

    Ok(())
}

fn db_path(explicit: Option<PathBuf>, root: &Path) -> PathBuf {
    explicit.unwrap_or_else(|| root.join(".graphtrail").join("graphtrail.db"))
}

fn open_default(explicit: Option<PathBuf>) -> Result<Connection> {
    let db = explicit.unwrap_or_else(|| PathBuf::from(".graphtrail/graphtrail.db"));
    open_db(&db)
}

fn open_db(path: &Path) -> Result<Connection> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)
            .with_context(|| format!("failed to create db directory {}", parent.display()))?;
    }
    let conn =
        Connection::open(path).with_context(|| format!("failed to open {}", path.display()))?;
    conn.pragma_update(None, "journal_mode", "WAL")?;
    Ok(conn)
}

fn init_schema(conn: &Connection) -> Result<()> {
    conn.execute_batch(
        r#"
        CREATE TABLE IF NOT EXISTS files (
            path TEXT PRIMARY KEY,
            content_hash TEXT NOT NULL,
            size INTEGER NOT NULL,
            modified_at INTEGER NOT NULL,
            indexed_at INTEGER NOT NULL,
            language TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS symbols (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            name TEXT NOT NULL,
            qualified_name TEXT NOT NULL,
            file_path TEXT NOT NULL,
            start_line INTEGER NOT NULL,
            end_line INTEGER NOT NULL,
            signature TEXT NOT NULL,
            container TEXT,
            content_hash TEXT NOT NULL,
            FOREIGN KEY(file_path) REFERENCES files(path) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS edges (
            source TEXT NOT NULL,
            target TEXT NOT NULL,
            kind TEXT NOT NULL,
            line INTEGER,
            PRIMARY KEY(source, target, kind, line),
            FOREIGN KEY(source) REFERENCES symbols(id) ON DELETE CASCADE,
            FOREIGN KEY(target) REFERENCES symbols(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS imports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path TEXT NOT NULL,
            module TEXT NOT NULL,
            line INTEGER NOT NULL
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS symbols_fts USING fts5(
            symbol_id UNINDEXED,
            name,
            qualified_name,
            signature,
            file_path
        );

        CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
        CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_path);
        CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source);
        CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target);
        "#,
    )?;
    Ok(())
}

#[derive(Default)]
struct SyncSummary {
    files: usize,
    symbols: usize,
    calls: usize,
    imports: usize,
}

fn sync_repo(conn: &Connection, root: &Path) -> Result<SyncSummary> {
    let root = root.canonicalize().unwrap_or_else(|_| root.to_path_buf());
    let mut graphs = Vec::new();
    for entry in WalkDir::new(&root).into_iter().filter_entry(keep_entry) {
        let entry = entry?;
        if !entry.file_type().is_file() {
            continue;
        }
        let Some(language) = language_for(entry.path()) else {
            continue;
        };
        let graph = index_file(&root, entry.path(), language)?;
        graphs.push(graph);
    }

    let tx = conn.unchecked_transaction()?;
    let changed_paths: Vec<String> = graphs.iter().map(|g| g.path.clone()).collect();
    for path in &changed_paths {
        tx.execute(
            "DELETE FROM edges WHERE source IN (SELECT id FROM symbols WHERE file_path = ?1)",
            params![path],
        )?;
        tx.execute(
            "DELETE FROM symbols_fts WHERE file_path = ?1",
            params![path],
        )?;
        tx.execute("DELETE FROM symbols WHERE file_path = ?1", params![path])?;
        tx.execute("DELETE FROM imports WHERE file_path = ?1", params![path])?;
    }

    let now = now_ts();
    for graph in &graphs {
        tx.execute(
            "INSERT OR REPLACE INTO files(path, content_hash, size, modified_at, indexed_at, language)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
            params![
                graph.path,
                graph.hash,
                graph.size as i64,
                graph.modified_at,
                now,
                graph.language
            ],
        )?;
        for symbol in &graph.symbols {
            tx.execute(
                "INSERT INTO symbols(id, kind, name, qualified_name, file_path, start_line, end_line, signature, container, content_hash)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10)",
                params![
                    symbol.id,
                    symbol.kind,
                    symbol.name,
                    symbol.qualified_name,
                    symbol.file_path,
                    symbol.start_line as i64,
                    symbol.end_line as i64,
                    symbol.signature,
                    symbol.container,
                    symbol.content_hash,
                ],
            )?;
            tx.execute(
                "INSERT INTO symbols_fts(symbol_id, name, qualified_name, signature, file_path)
                 VALUES (?1, ?2, ?3, ?4, ?5)",
                params![
                    symbol.id,
                    symbol.name,
                    symbol.qualified_name,
                    symbol.signature,
                    symbol.file_path
                ],
            )?;
        }
        for (module, line) in &graph.imports {
            tx.execute(
                "INSERT INTO imports(file_path, module, line) VALUES (?1, ?2, ?3)",
                params![graph.path, module, *line as i64],
            )?;
        }
    }

    let name_index = load_name_index(&tx)?;
    let mut inserted_calls = 0;
    for graph in &graphs {
        for call in &graph.calls {
            if let Some(targets) = name_index.get(&call.target_name) {
                for target in targets.iter().take(8) {
                    if target == &call.source_id {
                        continue;
                    }
                    tx.execute(
                        "INSERT OR IGNORE INTO edges(source, target, kind, line) VALUES (?1, ?2, 'calls', ?3)",
                        params![call.source_id, target, call.line as i64],
                    )?;
                    inserted_calls += 1;
                }
            }
        }
    }
    tx.commit()?;

    Ok(SyncSummary {
        files: graphs.len(),
        symbols: graphs.iter().map(|g| g.symbols.len()).sum(),
        imports: graphs.iter().map(|g| g.imports.len()).sum(),
        calls: inserted_calls,
    })
}

fn keep_entry(entry: &DirEntry) -> bool {
    let name = entry.file_name().to_string_lossy();
    !matches!(
        name.as_ref(),
        ".git"
            | ".graphtrail"
            | "node_modules"
            | "target"
            | "dist"
            | "build"
            | ".next"
            | ".turbo"
            | ".venv"
            | "__pycache__"
    )
}

fn language_for(path: &Path) -> Option<&'static str> {
    match path.extension().and_then(|e| e.to_str())? {
        "py" => Some("python"),
        "js" | "jsx" | "ts" | "tsx" => Some("typescript"),
        _ => None,
    }
}

fn index_file(root: &Path, path: &Path, language: &str) -> Result<FileGraph> {
    let content =
        fs::read_to_string(path).with_context(|| format!("failed to read {}", path.display()))?;
    let rel = path
        .strip_prefix(root)
        .unwrap_or(path)
        .to_string_lossy()
        .replace('\\', "/");
    let metadata = fs::metadata(path)?;
    let modified_at = metadata
        .modified()
        .ok()
        .and_then(|m| m.duration_since(UNIX_EPOCH).ok())
        .map_or(0, |d| d.as_secs() as i64);
    let hash = hex_hash(content.as_bytes());

    let mut graph = match language {
        "python" => extract_python(&rel, &content, &hash)?,
        "typescript" => extract_typescript(&rel, &content, &hash)?,
        _ => return Err(anyhow!("unsupported language {language}")),
    };
    graph.language = language.to_string();
    graph.size = metadata.len();
    graph.modified_at = modified_at;
    Ok(graph)
}

fn extract_python(path: &str, content: &str, content_hash: &str) -> Result<FileGraph> {
    let import_re =
        Regex::new(r"^\s*(?:from\s+([A-Za-z0-9_\.]+)\s+import|import\s+([A-Za-z0-9_\.]+))")?;
    let call_re = Regex::new(r"\b([A-Za-z_][A-Za-z0-9_\.]*)\s*\(")?;
    let lines: Vec<&str> = content.lines().collect();
    let mut imports = Vec::new();

    for (idx, line) in lines.iter().enumerate() {
        let line_no = idx + 1;
        if let Some(cap) = import_re.captures(line) {
            let module = cap
                .get(1)
                .or_else(|| cap.get(2))
                .map(|m| m.as_str().to_string())
                .unwrap_or_default();
            if !module.is_empty() {
                imports.push((module, line_no));
            }
        }
    }

    let symbols = extract_tree_sitter_symbols(
        path,
        content,
        content_hash,
        tree_sitter_python::LANGUAGE.into(),
        SymbolLanguage::Python,
    )?;
    let calls = collect_calls(path, &lines, &symbols, &call_re, python_call_skip());
    Ok(FileGraph {
        path: path.to_string(),
        language: String::new(),
        hash: content_hash.to_string(),
        size: 0,
        modified_at: 0,
        symbols,
        imports,
        calls,
    })
}

fn extract_typescript(path: &str, content: &str, content_hash: &str) -> Result<FileGraph> {
    let import_re =
        Regex::new(r#"^\s*import.*?from\s+['"]([^'"]+)['"]|^\s*import\s+['"]([^'"]+)['"]"#)?;
    let call_re = Regex::new(r"\b([A-Za-z_$][A-Za-z0-9_$\.]*)\s*\(")?;
    let lines: Vec<&str> = content.lines().collect();
    let mut imports = Vec::new();

    for (idx, line) in lines.iter().enumerate() {
        let line_no = idx + 1;
        if let Some(cap) = import_re.captures(line) {
            let module = cap
                .get(1)
                .or_else(|| cap.get(2))
                .map(|m| m.as_str().to_string())
                .unwrap_or_default();
            if !module.is_empty() {
                imports.push((module, line_no));
            }
        }
    }

    let language = if path.ends_with(".tsx") {
        tree_sitter_typescript::LANGUAGE_TSX.into()
    } else if path.ends_with(".ts") {
        tree_sitter_typescript::LANGUAGE_TYPESCRIPT.into()
    } else {
        tree_sitter_javascript::LANGUAGE.into()
    };
    let symbols = extract_tree_sitter_symbols(
        path,
        content,
        content_hash,
        language,
        SymbolLanguage::TypeScript,
    )?;
    let calls = collect_calls(path, &lines, &symbols, &call_re, js_call_skip());
    Ok(FileGraph {
        path: path.to_string(),
        language: String::new(),
        hash: content_hash.to_string(),
        size: 0,
        modified_at: 0,
        symbols,
        imports,
        calls,
    })
}

#[derive(Clone, Copy)]
enum SymbolLanguage {
    Python,
    TypeScript,
}

fn extract_tree_sitter_symbols(
    path: &str,
    content: &str,
    content_hash: &str,
    language: Language,
    symbol_language: SymbolLanguage,
) -> Result<Vec<Symbol>> {
    let mut parser = TsParser::new();
    parser
        .set_language(&language)
        .map_err(|err| anyhow!("failed to set tree-sitter language: {err}"))?;
    let tree = parser
        .parse(content, None)
        .ok_or_else(|| anyhow!("tree-sitter returned no parse tree for {path}"))?;
    let lines: Vec<&str> = content.lines().collect();
    let mut symbols = Vec::new();
    let mut stack = Vec::new();
    visit_symbol_node(
        tree.root_node(),
        path,
        content_hash,
        content.as_bytes(),
        &lines,
        symbol_language,
        &mut stack,
        &mut symbols,
    );
    Ok(symbols)
}

fn visit_symbol_node(
    node: TsNode<'_>,
    path: &str,
    content_hash: &str,
    source: &[u8],
    lines: &[&str],
    language: SymbolLanguage,
    stack: &mut Vec<String>,
    symbols: &mut Vec<Symbol>,
) {
    if let Some((kind, name_node)) = symbol_candidate(node, language) {
        let name = node_text(name_node, source);
        if !name.is_empty() {
            let start_line = node.start_position().row + 1;
            let end_line = node.end_position().row + 1;
            let signature = lines
                .get(start_line.saturating_sub(1))
                .map_or("", |line| *line)
                .trim()
                .to_string();
            let container = stack.last().cloned();
            let qualified_name = container
                .as_ref()
                .map_or_else(|| name.clone(), |parent| format!("{parent}.{name}"));
            let id = symbol_id(path, &qualified_name, start_line, kind);
            symbols.push(Symbol {
                id,
                kind: kind.to_string(),
                name: name.clone(),
                qualified_name: qualified_name.clone(),
                file_path: path.to_string(),
                start_line,
                end_line,
                signature,
                container,
                content_hash: content_hash.to_string(),
            });

            stack.push(qualified_name);
            visit_symbol_children(
                node,
                path,
                content_hash,
                source,
                lines,
                language,
                stack,
                symbols,
            );
            stack.pop();
            return;
        }
    }

    visit_symbol_children(
        node,
        path,
        content_hash,
        source,
        lines,
        language,
        stack,
        symbols,
    );
}

fn visit_symbol_children(
    node: TsNode<'_>,
    path: &str,
    content_hash: &str,
    source: &[u8],
    lines: &[&str],
    language: SymbolLanguage,
    stack: &mut Vec<String>,
    symbols: &mut Vec<Symbol>,
) {
    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        visit_symbol_node(
            child,
            path,
            content_hash,
            source,
            lines,
            language,
            stack,
            symbols,
        );
    }
}

fn symbol_candidate(
    node: TsNode<'_>,
    language: SymbolLanguage,
) -> Option<(&'static str, TsNode<'_>)> {
    match language {
        SymbolLanguage::Python => match node.kind() {
            "class_definition" => node.child_by_field_name("name").map(|name| ("class", name)),
            "function_definition" => node
                .child_by_field_name("name")
                .map(|name| ("function", name)),
            _ => None,
        },
        SymbolLanguage::TypeScript => match node.kind() {
            "class_declaration" => node.child_by_field_name("name").map(|name| ("class", name)),
            "function_declaration" => node
                .child_by_field_name("name")
                .map(|name| ("function", name)),
            "method_definition" => node
                .child_by_field_name("name")
                .map(|name| ("method", name)),
            "variable_declarator" => {
                let value = node.child_by_field_name("value")?;
                if matches!(value.kind(), "arrow_function" | "function") {
                    node.child_by_field_name("name")
                        .map(|name| ("function", name))
                } else {
                    None
                }
            }
            _ => None,
        },
    }
}

fn node_text(node: TsNode<'_>, source: &[u8]) -> String {
    node.utf8_text(source).unwrap_or("").to_string()
}

fn collect_calls(
    _path: &str,
    lines: &[&str],
    symbols: &[Symbol],
    call_re: &Regex,
    skip: HashSet<&'static str>,
) -> Vec<PendingCall> {
    let mut calls = Vec::new();
    for (idx, line) in lines.iter().enumerate() {
        let line_no = idx + 1;
        let Some(source) = symbols
            .iter()
            .find(|s| s.start_line <= line_no && s.end_line >= line_no)
        else {
            continue;
        };
        for cap in call_re.captures_iter(line) {
            let raw = cap.get(1).map_or("", |m| m.as_str());
            let target = raw.rsplit('.').next().unwrap_or(raw);
            if target.is_empty() || skip.contains(target) || target == source.name {
                continue;
            }
            calls.push(PendingCall {
                source_id: source.id.clone(),
                target_name: target.to_string(),
                line: line_no,
            });
        }
    }
    calls
}

fn python_call_skip() -> HashSet<&'static str> {
    HashSet::from([
        "if",
        "for",
        "while",
        "with",
        "return",
        "print",
        "len",
        "str",
        "int",
        "float",
        "bool",
        "list",
        "dict",
        "set",
        "tuple",
        "super",
        "isinstance",
        "range",
    ])
}

fn js_call_skip() -> HashSet<&'static str> {
    HashSet::from([
        "if",
        "for",
        "while",
        "switch",
        "return",
        "console",
        "log",
        "map",
        "filter",
        "reduce",
        "then",
        "catch",
        "setTimeout",
        "Promise",
    ])
}

fn load_name_index(conn: &Connection) -> Result<HashMap<String, Vec<String>>> {
    let mut stmt = conn.prepare("SELECT name, id FROM symbols")?;
    let rows = stmt.query_map([], |row| {
        Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
    })?;
    let mut map: HashMap<String, Vec<String>> = HashMap::new();
    for row in rows {
        let (name, id) = row?;
        map.entry(name).or_default().push(id);
    }
    Ok(map)
}

fn search_symbols(conn: &Connection, query: &str, limit: usize) -> Result<Vec<SearchRow>> {
    let fts = fts_query(query);
    let mut rows = Vec::new();
    if !fts.is_empty() {
        let sql = r#"
            SELECT s.id, s.kind, s.name, s.qualified_name, s.file_path, s.start_line,
                   s.end_line, s.signature, bm25(symbols_fts) AS rank
            FROM symbols_fts
            JOIN symbols s ON s.id = symbols_fts.symbol_id
            WHERE symbols_fts MATCH ?1
            ORDER BY bm25(symbols_fts), s.file_path, s.start_line
            LIMIT ?2
        "#;
        let mut stmt = conn.prepare(sql)?;
        let mapped = stmt.query_map(params![fts, limit as i64], search_row_from_sql)?;
        for row in mapped {
            rows.push(row?);
        }
    }

    if rows.is_empty() {
        let like = format!("%{query}%");
        let mut stmt = conn.prepare(
            r#"
            SELECT id, kind, name, qualified_name, file_path, start_line,
                   end_line, signature, 1.0 AS rank
            FROM symbols
            WHERE name LIKE ?1 OR qualified_name LIKE ?1 OR signature LIKE ?1
            ORDER BY file_path, start_line
            LIMIT ?2
            "#,
        )?;
        let mapped = stmt.query_map(params![like, limit as i64], search_row_from_sql)?;
        for row in mapped {
            rows.push(row?);
        }
    }
    Ok(rows)
}

fn search_row_from_sql(row: &rusqlite::Row<'_>) -> rusqlite::Result<SearchRow> {
    let rank: f64 = row.get(8)?;
    Ok(SearchRow {
        id: row.get(0)?,
        kind: row.get(1)?,
        name: row.get(2)?,
        qualified_name: row.get(3)?,
        file_path: row.get(4)?,
        start_line: row.get::<_, i64>(5)? as usize,
        end_line: row.get::<_, i64>(6)? as usize,
        signature: row.get(7)?,
        score: if rank < 0.0 { -rank } else { rank },
    })
}

#[derive(Clone, Copy)]
enum Direction {
    Incoming,
    Outgoing,
}

fn graph_edges(
    conn: &Connection,
    symbol_query: &str,
    direction: Direction,
) -> Result<Vec<EdgeRow>> {
    let symbols = search_symbols(conn, symbol_query, 20)?;
    let mut edges = Vec::new();
    for symbol in symbols {
        edges.extend(edges_for_symbol_id(conn, &symbol.id, direction)?);
    }
    dedupe_edges(edges)
}

fn edges_for_symbol_id(
    conn: &Connection,
    symbol_id: &str,
    direction: Direction,
) -> Result<Vec<EdgeRow>> {
    let (where_clause, order) = match direction {
        Direction::Incoming => ("e.target = ?1", "src.file_path, src.start_line"),
        Direction::Outgoing => ("e.source = ?1", "dst.file_path, dst.start_line"),
    };
    let sql = format!(
        r#"
        SELECT e.source, src.qualified_name, e.target, dst.qualified_name, e.kind, e.line,
               src.file_path, dst.file_path
        FROM edges e
        JOIN symbols src ON src.id = e.source
        JOIN symbols dst ON dst.id = e.target
        WHERE {where_clause}
        ORDER BY {order}
        "#
    );
    let mut stmt = conn.prepare(&sql)?;
    let mapped = stmt.query_map(params![symbol_id], |row| {
        Ok(EdgeRow {
            source_id: row.get(0)?,
            source: row.get(1)?,
            target_id: row.get(2)?,
            target: row.get(3)?,
            kind: row.get(4)?,
            line: row.get::<_, Option<i64>>(5)?.map(|v| v as usize),
            source_file: row.get(6)?,
            target_file: row.get(7)?,
        })
    })?;
    let mut rows = Vec::new();
    for row in mapped {
        rows.push(row?);
    }
    Ok(rows)
}

fn dedupe_edges(edges: Vec<EdgeRow>) -> Result<Vec<EdgeRow>> {
    let mut seen = HashSet::new();
    let mut out = Vec::new();
    for edge in edges {
        let key = format!(
            "{}:{}:{}:{}",
            edge.source_id,
            edge.target_id,
            edge.kind,
            edge.line.unwrap_or_default()
        );
        if seen.insert(key) {
            out.push(edge);
        }
    }
    Ok(out)
}

fn stats(conn: &Connection) -> Result<BTreeMap<String, i64>> {
    let mut map = BTreeMap::new();
    for (key, sql) in [
        ("files", "SELECT COUNT(*) FROM files"),
        ("symbols", "SELECT COUNT(*) FROM symbols"),
        ("edges", "SELECT COUNT(*) FROM edges"),
        ("imports", "SELECT COUNT(*) FROM imports"),
    ] {
        let count: i64 = conn.query_row(sql, [], |row| row.get(0))?;
        map.insert(key.to_string(), count);
    }
    Ok(map)
}

fn print_json_or_symbols(json: bool, rows: &[SearchRow]) -> Result<()> {
    if json {
        println!("{}", serde_json::to_string_pretty(rows)?);
        return Ok(());
    }
    for row in rows {
        println!(
            "{} {} {}:{} {}",
            row.kind, row.qualified_name, row.file_path, row.start_line, row.signature
        );
    }
    Ok(())
}

fn print_json_or_edges(json: bool, rows: &[EdgeRow]) -> Result<()> {
    if json {
        println!("{}", serde_json::to_string_pretty(rows)?);
        return Ok(());
    }
    for row in rows {
        println!(
            "{} --{}@{}--> {}  ({} -> {})",
            row.source,
            row.kind,
            row.line.unwrap_or_default(),
            row.target,
            row.source_file,
            row.target_file
        );
    }
    Ok(())
}

fn print_context(pack: &ContextPack) {
    println!("# Context\n");
    println!("task: {}\n", pack.task);
    println!("## Entry Points");
    for row in &pack.entry_points {
        println!(
            "- {} `{}` at {}:{}",
            row.kind, row.qualified_name, row.file_path, row.start_line
        );
    }
    println!("\n## Callers");
    for edge in &pack.callers {
        println!("- `{}` calls `{}`", edge.source, edge.target);
    }
    println!("\n## Callees");
    for edge in &pack.callees {
        println!("- `{}` calls `{}`", edge.source, edge.target);
    }
    println!("\n## Related Files");
    for file in &pack.related_files {
        println!("- {file}");
    }
}

fn fts_query(query: &str) -> String {
    query
        .split_whitespace()
        .filter_map(|term| {
            let clean: String = term
                .chars()
                .filter(|c| c.is_alphanumeric() || *c == '_' || *c == '$')
                .collect();
            if clean.is_empty() {
                None
            } else {
                Some(format!("\"{clean}\"*"))
            }
        })
        .collect::<Vec<_>>()
        .join(" OR ")
}

fn symbol_id(path: &str, qualified_name: &str, line: usize, kind: &str) -> String {
    hex_hash(format!("{path}:{qualified_name}:{line}:{kind}").as_bytes())
}

fn hex_hash(bytes: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    format!("{:x}", hasher.finalize())
}

fn now_ts() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_secs() as i64)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn python_extractor_finds_symbols_imports_and_calls() {
        let source = r#"
import os

class Runner:
    def start(self):
        helper()

def helper():
    return os.getcwd()
"#;
        let graph = extract_python("src/demo.py", source, "hash").unwrap();
        let names: Vec<&str> = graph.symbols.iter().map(|s| s.name.as_str()).collect();
        assert!(names.contains(&"Runner"));
        assert!(names.contains(&"start"));
        assert!(names.contains(&"helper"));
        assert_eq!(graph.imports[0].0, "os");
        assert!(graph.calls.iter().any(|c| c.target_name == "helper"));
    }

    #[test]
    fn typescript_extractor_finds_common_symbol_shapes() {
        let source = r#"
import { x } from "./x";

export class Runner {}
export function start() {
  helper();
}
const helper = () => x();
"#;
        let graph = extract_typescript("src/demo.ts", source, "hash").unwrap();
        let names: Vec<&str> = graph.symbols.iter().map(|s| s.name.as_str()).collect();
        assert!(names.contains(&"Runner"));
        assert!(names.contains(&"start"));
        assert!(names.contains(&"helper"));
        assert_eq!(graph.imports[0].0, "./x");
        assert!(graph.calls.iter().any(|c| c.target_name == "helper"));
    }

    #[test]
    fn fts_query_quotes_terms_and_strips_punctuation() {
        assert_eq!(fts_query("handoff lint!"), "\"handoff\"* OR \"lint\"*");
    }
}
