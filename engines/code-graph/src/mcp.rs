//! Minimal MCP server: newline-delimited JSON-RPC 2.0 over stdin/stdout, exposing GraphTrail's
//! read-only queries as tools. No async runtime and no extra dependencies, keeping the sidecar
//! small.
//!
//! Multi-repo: the server holds a default db path but opens the database lazily per `tools/call`.
//! Each tool accepts an optional `repo` (uses `<repo>/.graphtrail/graphtrail.db`) or `db` (explicit
//! path) argument, so a single registered server can answer for any indexed repository. Query
//! connections are always opened `SQLITE_OPEN_READ_ONLY`; the opt-in `refresh: true` parameter is
//! the one sanctioned write path, running the CLI's incremental sync (fail-open) before the query.

use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::io::{BufRead, Write};
use std::path::{Path, PathBuf};
use std::sync::{OnceLock, mpsc};
use std::thread;
use std::time::Duration;

use anyhow::{Result, anyhow};
use rusqlite::{OptionalExtension, params};
use serde::Serialize;
use serde_json::{Value, json};

use crate::model::Direction;
#[cfg(feature = "codesearch")]
use crate::query::build_context_pack_from_entry_points;
use crate::query::{
    DEFAULT_AFFECTED_DEPTH, DEFAULT_IMPACT_DEPTH, affected, build_context_pack, cycles, dead_code,
    diff_graphs, doctor, file_neighbors, graph_edges_with_depth, impact_edges, normalize_depth,
    render_markdown, search_symbols_with_path, stats,
};
use crate::store::{init_schema, open_db, open_read_only, sync_repo};

const PROTOCOL_VERSION: &str = "2024-11-05";
const REFRESH_TIMEOUT: Duration = Duration::from_secs(10);
#[cfg(feature = "codesearch")]
const SEMANTIC_SEARCH_DEFAULT_LIMIT: usize = 10;
#[cfg(feature = "codesearch")]
const SEMANTIC_SEARCH_MAX_LIMIT: usize = 50;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum ToolId {
    Search,
    Callers,
    Callees,
    Impact,
    #[cfg(feature = "codesearch")]
    SemanticSearch,
    Context,
    Stats,
    Doctor,
    FileNeighbors,
    DeadCode,
    Cycles,
    Affected,
    Diff,
    Repos,
}

struct ToolSpec {
    id: ToolId,
    name: &'static str,
    description: &'static str,
    input_schema: Value,
    supports_refresh: bool,
    returns_json_rpc_error: bool,
}

/// Read JSON-RPC requests line by line and write one response line per request. `default_db` is the
/// fallback database used when a request does not specify its own `repo`/`db`.
pub fn serve(default_db: &Path, input: impl BufRead, mut output: impl Write) -> Result<()> {
    for line in input.lines() {
        let line = line?;
        if line.trim().is_empty() {
            continue;
        }
        let req = match serde_json::from_str::<Value>(&line) {
            Ok(req) => req,
            Err(_) => {
                writeln!(
                    output,
                    "{}",
                    serde_json::to_string(&error(None, -32700, "parse error"))?
                )?;
                output.flush()?;
                continue;
            }
        };
        if let Some(resp) = handle_request(default_db, &req) {
            writeln!(output, "{}", serde_json::to_string(&resp)?)?;
            output.flush()?;
        }
    }
    Ok(())
}

/// Handle one JSON-RPC message. Returns `None` for notifications (no response expected).
pub fn handle_request(default_db: &Path, req: &Value) -> Option<Value> {
    if !req.is_object() {
        return Some(error(None, -32600, "invalid request"));
    }
    let id = req.get("id").cloned();
    if req.get("jsonrpc").and_then(|v| v.as_str()) != Some("2.0") {
        return Some(error(id, -32600, "invalid request"));
    }
    let Some(method) = req.get("method").and_then(|m| m.as_str()) else {
        return Some(error(id, -32600, "invalid request"));
    };
    match method {
        "initialize" => {
            let pv = req
                .pointer("/params/protocolVersion")
                .and_then(|v| v.as_str())
                .unwrap_or(PROTOCOL_VERSION)
                .to_string();
            Some(ok(
                id,
                json!({
                    "protocolVersion": pv,
                    "capabilities": { "tools": {} },
                    "serverInfo": { "name": "graphtrail", "version": env!("CARGO_PKG_VERSION") }
                }),
            ))
        }
        "ping" => Some(ok(id, json!({}))),
        "tools/list" => Some(ok(id, json!({ "tools": tool_defs() }))),
        "tools/call" => {
            let params = match req.get("params") {
                Some(params) if params.is_object() => params,
                _ => return Some(error(id, -32602, "invalid params")),
            };
            let Some(name) = params.get("name").and_then(|v| v.as_str()) else {
                return Some(error(id, -32602, "invalid params"));
            };
            let args = match params.get("arguments") {
                Some(args) if args.is_object() => args.clone(),
                None => json!({}),
                _ => return Some(error(id, -32602, "invalid params")),
            };
            let result = match validate_tool_args(name, &args) {
                Ok(()) => call_tool(default_db, name, &args),
                Err(err) => return Some(error(id, -32602, &err)),
            };
            Some(match result {
                Ok(text) => ok(
                    id,
                    json!({ "content": [{ "type": "text", "text": text }], "isError": false }),
                ),
                Err(err) if returns_json_rpc_tool_error(name) => {
                    error(id, -32000, &err.to_string())
                }
                Err(err) => ok(
                    id,
                    json!({ "content": [{ "type": "text", "text": format!("error: {err}") }], "isError": true }),
                ),
            })
        }
        // Notifications carry no id and expect no response.
        _ if id.is_none() => None,
        _ => Some(error(id, -32601, "method not found")),
    }
}

/// Resolve which database a call targets: explicit `db`, else `<repo>/.graphtrail/graphtrail.db`,
/// else the server default.
fn resolve_db(default_db: &Path, args: &Value) -> PathBuf {
    if let Some(db) = args.get("db").and_then(|v| v.as_str()) {
        PathBuf::from(db)
    } else if let Some(repo) = args.get("repo").and_then(|v| v.as_str()) {
        Path::new(repo).join(".graphtrail").join("graphtrail.db")
    } else {
        default_db.to_path_buf()
    }
}

fn call_tool(default_db: &Path, name: &str, args: &Value) -> Result<String> {
    let spec = tool_spec(name).ok_or_else(|| anyhow!("unknown tool '{name}'"))?;
    if spec.id == ToolId::Repos {
        let db = resolve_db(default_db, args);
        return to_pretty(&repos_response(&db, args)?);
    }
    // `diff` needs two databases, so it opens its own connections before the
    // single shared-connection open below.
    if spec.id == ToolId::Diff {
        let before = open_read_only(Path::new(&str_arg(args, "before")))?;
        let after = open_read_only(Path::new(&str_arg(args, "after")))?;
        return to_pretty(&diff_graphs(&before, &after)?);
    }

    let db = resolve_db(default_db, args);
    let refresh_error = if spec.supports_refresh && bool_arg(args, "refresh", false) {
        refresh_db(default_db, args, &db)
    } else {
        None
    };
    let conn = open_read_only(&db)?;
    let text = match spec.id {
        ToolId::Search => to_pretty(&search_symbols_with_path(
            &conn,
            &str_arg(args, "query"),
            optional_str_arg(args, "path").as_deref(),
            usize_arg(args, "limit", 20),
        )?),
        ToolId::Callers => to_pretty(&graph_edges_with_depth(
            &conn,
            &str_arg(args, "symbol"),
            Direction::Incoming,
            normalize_depth(usize_arg(args, "depth", DEFAULT_IMPACT_DEPTH)),
        )?),
        ToolId::Callees => to_pretty(&graph_edges_with_depth(
            &conn,
            &str_arg(args, "symbol"),
            Direction::Outgoing,
            normalize_depth(usize_arg(args, "depth", DEFAULT_IMPACT_DEPTH)),
        )?),
        ToolId::Impact => {
            let symbol = str_arg(args, "symbol");
            let edges = impact_edges(
                &conn,
                &symbol,
                normalize_depth(usize_arg(args, "depth", DEFAULT_IMPACT_DEPTH)),
            )?;
            to_pretty(&edges)
        }
        #[cfg(feature = "codesearch")]
        ToolId::SemanticSearch => {
            let limit = semantic_search_limit(args);
            let repo_root = code_search_repo_root(default_db, args, &db);
            let hits = code_search_hits(&str_arg(args, "query"), limit, repo_root.as_deref())?;
            if bool_arg(args, "blend", true) {
                to_pretty(&crate::query::blend(
                    &conn,
                    &hits,
                    f64_arg(args, "embed_weight", 0.6),
                    f64_arg(args, "graph_weight", 0.4),
                    limit,
                )?)
            } else {
                to_pretty(&hits)
            }
        }
        ToolId::Context => {
            let task = str_arg(args, "task");
            let limit = usize_arg(args, "limit", 12);
            #[cfg(feature = "codesearch")]
            let pack = if bool_arg(args, "blend_code_search", false) {
                let search_limit =
                    limit.clamp(SEMANTIC_SEARCH_DEFAULT_LIMIT, SEMANTIC_SEARCH_MAX_LIMIT);
                let repo_root = code_search_repo_root(default_db, args, &db);
                let hits = code_search_hits(&task, search_limit, repo_root.as_deref())?;
                let rows = crate::query::blend(
                    &conn,
                    &hits,
                    f64_arg(args, "embed_weight", 0.6),
                    f64_arg(args, "graph_weight", 0.4),
                    limit,
                )?;
                let entry_points = rows.into_iter().map(|row| row.symbol).collect();
                build_context_pack_from_entry_points(&conn, task.clone(), entry_points)?
            } else {
                build_context_pack(&conn, task.clone(), limit)?
            };
            #[cfg(not(feature = "codesearch"))]
            let pack = build_context_pack(&conn, task.clone(), limit)?;
            match str_arg(args, "format").as_str() {
                "" | "json" => to_pretty(&pack),
                "markdown" => Ok(render_markdown(&pack)),
                other => Err(anyhow!("unknown context format '{other}'")),
            }
        }
        ToolId::FileNeighbors => to_pretty(&file_neighbors(&conn, &str_arg(args, "path"))?),
        ToolId::DeadCode => to_pretty(&dead_code(&conn, usize_arg(args, "limit", 100))?),
        ToolId::Cycles => to_pretty(&cycles(&conn)?),
        ToolId::Affected => to_pretty(&affected(
            &conn,
            &files_arg(args),
            usize_arg(args, "depth", DEFAULT_AFFECTED_DEPTH),
        )?),
        ToolId::Stats => to_pretty(&stats(&conn)?),
        ToolId::Doctor => to_pretty(&doctor(&conn, &doctor_root(default_db, args, &db), &db)?),
        ToolId::Diff | ToolId::Repos => unreachable!("special tools return before opening one db"),
    }?;
    Ok(with_refresh_error(text, refresh_error))
}

fn validate_tool_args(name: &str, args: &Value) -> std::result::Result<(), String> {
    optional_string(args, "db")?;
    optional_string(args, "repo")?;
    let Some(spec) = tool_spec(name) else {
        return Ok(());
    };
    if spec.supports_refresh {
        optional_bool(args, "refresh")?;
    }
    match spec.id {
        ToolId::Search => {
            require_string(args, "query")?;
            optional_string(args, "path")?;
            require_usize(args, "limit")?;
        }
        ToolId::Callers | ToolId::Callees | ToolId::Impact => {
            require_string(args, "symbol")?;
            require_usize(args, "depth")?;
        }
        #[cfg(feature = "codesearch")]
        ToolId::SemanticSearch => {
            require_string(args, "query")?;
            require_usize(args, "limit")?;
            optional_bool(args, "blend")?;
            optional_number(args, "embed_weight")?;
            optional_number(args, "graph_weight")?;
        }
        ToolId::Context => {
            require_string(args, "task")?;
            require_usize(args, "limit")?;
            require_format(args)?;
            #[cfg(feature = "codesearch")]
            {
                optional_bool(args, "blend_code_search")?;
                optional_number(args, "embed_weight")?;
                optional_number(args, "graph_weight")?;
            }
        }
        ToolId::FileNeighbors => {
            require_string(args, "path")?;
        }
        ToolId::DeadCode => {
            require_usize(args, "limit")?;
        }
        ToolId::Affected => {
            require_files(args)?;
            require_usize(args, "depth")?;
        }
        ToolId::Diff => {
            require_string(args, "before")?;
            require_string(args, "after")?;
        }
        ToolId::Repos => {
            require_roots(args)?;
        }
        ToolId::Stats | ToolId::Doctor | ToolId::Cycles => {}
    }
    Ok(())
}

fn tool_specs() -> &'static [ToolSpec] {
    static SPECS: OnceLock<Vec<ToolSpec>> = OnceLock::new();
    SPECS.get_or_init(build_tool_specs).as_slice()
}

fn build_tool_specs() -> Vec<ToolSpec> {
    // Every tool also accepts an optional repo/db selector for multi-repo use.
    let location = json!({
        "repo": { "type": "string", "description": "Repo path; uses <repo>/.graphtrail/graphtrail.db." },
        "db": { "type": "string", "description": "Explicit graphtrail.db path (overrides repo and the server default)." }
    });
    let refresh = json!({
        "refresh": { "type": "boolean", "description": "Default false; sync is incremental and cheap on warm repos." }
    });
    let with_location = |props: Value, required: Value| {
        let mut merged = location.clone();
        if let (Some(dst), Some(src)) = (merged.as_object_mut(), props.as_object()) {
            for (k, v) in src {
                dst.insert(k.clone(), v.clone());
            }
        }
        json!({ "type": "object", "properties": merged, "required": required })
    };
    let with_refresh = |props: Value| {
        let mut merged = props;
        if let (Some(dst), Some(src)) = (merged.as_object_mut(), refresh.as_object()) {
            for (k, v) in src {
                dst.insert(k.clone(), v.clone());
            }
        }
        merged
    };
    let symbol_tool = |desc: &str| {
        with_location(
            with_refresh(json!({
                "symbol": { "type": "string", "description": desc },
                "depth": { "type": "integer", "description": "Traversal depth, clamped to 1..5 (default 1)." }
            })),
            json!(["symbol"]),
        )
    };
    let context_props = json!({
        "task": { "type": "string", "description": "Task or feature description to gather context for." },
        "limit": { "type": "integer", "description": "Max entry points (default 12)." },
        "format": {
            "type": "string",
            "enum": ["json", "markdown"],
            "description": "Response format (default json)."
        }
    });
    #[cfg(feature = "codesearch")]
    let mut context_props = context_props;
    #[cfg(feature = "codesearch")]
    if let Some(props) = context_props.as_object_mut() {
        props.insert(
            "blend_code_search".to_string(),
            json!({ "type": "boolean", "description": "Default false; use Code Search semantic hits as context entry points." }),
        );
        props.insert(
            "embed_weight".to_string(),
            json!({ "type": "number", "description": "Embedding score weight for blended Code Search context (default 0.6)." }),
        );
        props.insert(
            "graph_weight".to_string(),
            json!({ "type": "number", "description": "Graph centrality score weight for blended Code Search context (default 0.4)." }),
        );
    }
    let tool = |id, name, description, input_schema, supports_refresh| ToolSpec {
        id,
        name,
        description,
        input_schema,
        supports_refresh,
        returns_json_rpc_error: false,
    };
    let mut tools = vec![tool(
        ToolId::Search,
        "search",
        "Full-text search code symbols (functions, classes, methods) by name.",
        with_location(
            with_refresh(json!({
                "query": { "type": "string", "description": "Search terms." },
                "path": { "type": "string", "description": "Optional file path, directory prefix, or path fragment." },
                "limit": { "type": "integer", "description": "Max results (default 20)." }
            })),
            json!(["query"]),
        ),
        true,
    )];
    #[cfg(feature = "codesearch")]
    tools.push(ToolSpec {
        returns_json_rpc_error: true,
        ..tool(
            ToolId::SemanticSearch,
            "semantic_search",
            "Code Search semantic hits, optionally blended with GraphTrail graph centrality.",
            with_location(
                with_refresh(json!({
                    "query": { "type": "string", "description": "Semantic search query." },
                    "limit": { "type": "integer", "description": "Max results, clamped to 1..50 (default 10)." },
                    "blend": { "type": "boolean", "description": "Default true; return blended symbol rows instead of raw per-file Code Search hits." },
                    "embed_weight": { "type": "number", "description": "Embedding score weight when blend is true (default 0.6)." },
                    "graph_weight": { "type": "number", "description": "Graph centrality score weight when blend is true (default 0.4)." }
                })),
                json!(["query"]),
            ),
            true,
        )
    });
    tools.extend([
        tool(
            ToolId::Callers,
            "callers",
            "Symbols that call the given symbol (incoming call edges).",
            symbol_tool("Symbol name to find callers of."),
            true,
        ),
        tool(
            ToolId::Callees,
            "callees",
            "Symbols called by the given symbol (outgoing call edges).",
            symbol_tool("Symbol name to find callees of."),
            true,
        ),
        tool(
            ToolId::Impact,
            "impact",
            "Combined callers and callees of a symbol (blast radius of a change).",
            symbol_tool("Symbol name to assess impact for."),
            true,
        ),
        tool(
            ToolId::Context,
            "context",
            "A context pack for a task: matching entry points plus their caller/callee neighborhood and related files.",
            with_location(
                with_refresh(context_props),
                json!(["task"]),
            ),
            true,
        ),
        tool(
            ToolId::Stats,
            "stats",
            "Counts of files, symbols, edges, imports, sync metadata, and per-language file counts.",
            with_location(with_refresh(json!({})), json!([])),
            true,
        ),
        tool(
            ToolId::Doctor,
            "doctor",
            "Freshness contract for the graph: schema status, last sync age, pending file changes, ignored entries, and FRESH/STALE/NEEDS-MIGRATION verdict.",
            with_location(json!({}), json!([])),
            false,
        ),
        tool(
            ToolId::FileNeighbors,
            "file_neighbors",
            "Files connected to a file by incoming or outgoing call edges.",
            with_location(
                with_refresh(json!({ "path": { "type": "string", "description": "Indexed file path to inspect." } })),
                json!(["path"]),
            ),
            true,
        ),
        tool(
            ToolId::DeadCode,
            "dead_code",
            "Callables with no incoming call edges: a dead-code candidate list, not proof (dynamic dispatch, exports, and entry points are invisible to call edges).",
            with_location(
                with_refresh(json!({ "limit": { "type": "integer", "description": "Max symbols returned (default 100)." } })),
                json!([]),
            ),
            true,
        ),
        tool(
            ToolId::Cycles,
            "cycles",
            "File-level dependency cycles from cross-file call edges, grouped into strongly connected components.",
            with_location(with_refresh(json!({})), json!([])),
            true,
        ),
        tool(
            ToolId::Affected,
            "affected",
            "Tests statically attributed to changed files via incoming call edges: a lower bound on what to run, not coverage. Pass changed files from `git diff --name-only`.",
            with_location(
                with_refresh(json!({
                    "files": { "type": "array", "items": { "type": "string" }, "description": "Changed files, repo-relative." },
                    "depth": { "type": "integer", "description": "Caller-BFS depth, clamped to 1..5 (default 3)." }
                })),
                json!(["files"]),
            ),
            true,
        ),
        tool(
            ToolId::Diff,
            "diff",
            "Structural diff of two indexed graph DBs (before -> after): added/removed/changed symbols and added/removed call edges. Build the two DBs with `graphtrail --db <path> sync <root>`.",
            json!({
                "type": "object",
                "properties": {
                    "before": { "type": "string", "description": "Path to the 'before' graphtrail.db." },
                    "after": { "type": "string", "description": "Path to the 'after' graphtrail.db." }
                },
                "required": ["before", "after"]
            }),
            false,
        ),
        tool(
            ToolId::Repos,
            "repos",
            "Default database metadata plus optional one-level scans for indexed repos under root directories.",
            with_location(
                json!({ "roots": { "type": "array", "items": { "type": "string" }, "description": "Root directories to scan one level for .graphtrail/graphtrail.db." } }),
                json!([]),
            ),
            false,
        ),
    ]);
    tools
}

fn tool_spec(name: &str) -> Option<&'static ToolSpec> {
    tool_specs().iter().find(|spec| spec.name == name)
}

fn tool_defs() -> Value {
    Value::Array(
        tool_specs()
            .iter()
            .map(|spec| {
                json!({
                    "name": spec.name,
                    "description": spec.description,
                    "inputSchema": spec.input_schema.clone(),
                })
            })
            .collect(),
    )
}

fn to_pretty<T: Serialize>(value: &T) -> Result<String> {
    Ok(serde_json::to_string_pretty(value)?)
}

fn returns_json_rpc_tool_error(name: &str) -> bool {
    tool_spec(name).is_some_and(|spec| spec.returns_json_rpc_error)
}

fn refresh_db(default_db: &Path, args: &Value, db: &Path) -> Option<String> {
    let db = db.to_path_buf();
    let root = refresh_root(default_db, args, &db);
    let (tx, rx) = mpsc::channel();
    thread::spawn(move || {
        let result = (|| -> Result<()> {
            let conn = open_db(&db)?;
            init_schema(&conn)?;
            sync_repo(&conn, &root)?;
            Ok(())
        })();
        let _ = tx.send(result);
    });

    match rx.recv_timeout(REFRESH_TIMEOUT) {
        Ok(Ok(())) => None,
        Ok(Err(err)) => Some(err.to_string()),
        Err(mpsc::RecvTimeoutError::Timeout) => Some(format!(
            "refresh timed out after {}s",
            REFRESH_TIMEOUT.as_secs()
        )),
        Err(mpsc::RecvTimeoutError::Disconnected) => Some("refresh worker stopped".to_string()),
    }
}

fn refresh_root(default_db: &Path, args: &Value, db: &Path) -> PathBuf {
    if let Some(repo) = args.get("repo").and_then(|v| v.as_str()) {
        return PathBuf::from(repo);
    }
    repo_from_db(db)
        .or_else(|| repo_from_db(default_db))
        .or_else(|| db.parent().map(|path| path.to_path_buf()))
        .unwrap_or_else(|| PathBuf::from("."))
}

fn doctor_root(default_db: &Path, args: &Value, db: &Path) -> PathBuf {
    if let Some(repo) = args.get("repo").and_then(|v| v.as_str()) {
        return PathBuf::from(repo);
    }
    repo_from_db(db)
        .or_else(|| repo_from_db(default_db))
        .or_else(|| db.parent().map(|path| path.to_path_buf()))
        .unwrap_or_else(|| PathBuf::from("."))
}

fn with_refresh_error(mut text: String, refresh_error: Option<String>) -> String {
    if let Some(err) = refresh_error {
        text.push_str("\n\nrefresh_error: ");
        text.push_str(&err);
    }
    text
}

fn str_arg(args: &Value, key: &str) -> String {
    args.get(key)
        .and_then(|v| v.as_str())
        .map(|s| s.to_string())
        .unwrap_or_default()
}

fn optional_str_arg(args: &Value, key: &str) -> Option<String> {
    args.get(key)
        .and_then(|v| v.as_str())
        .map(|s| s.to_string())
}

fn usize_arg(args: &Value, key: &str, default: usize) -> usize {
    args.get(key)
        .and_then(|v| v.as_u64())
        .and_then(|n| usize::try_from(n).ok())
        .unwrap_or(default)
}

#[cfg(feature = "codesearch")]
fn f64_arg(args: &Value, key: &str, default: f64) -> f64 {
    args.get(key).and_then(|v| v.as_f64()).unwrap_or(default)
}

fn bool_arg(args: &Value, key: &str, default: bool) -> bool {
    args.get(key).and_then(|v| v.as_bool()).unwrap_or(default)
}

#[cfg(feature = "codesearch")]
fn semantic_search_limit(args: &Value) -> usize {
    usize_arg(args, "limit", SEMANTIC_SEARCH_DEFAULT_LIMIT).clamp(1, SEMANTIC_SEARCH_MAX_LIMIT)
}

#[cfg(feature = "codesearch")]
fn code_search_hits(
    query: &str,
    limit: usize,
    repo_root: Option<&Path>,
) -> Result<Vec<crate::query::ExternalHit>> {
    let client = crate::adapters::codesearch::CodeSearchClient::from_env_for_repo(repo_root);
    client.search(query, limit).map_err(|err| {
        anyhow!("Code Search API is unreachable; check CODE_SEARCH_URL and the service: {err}")
    })
}

#[cfg(feature = "codesearch")]
fn code_search_repo_root(default_db: &Path, args: &Value, db: &Path) -> Option<PathBuf> {
    if let Some(repo) = args.get("repo").and_then(|v| v.as_str()) {
        return Some(PathBuf::from(repo));
    }
    repo_from_db(db)
        .or_else(|| repo_from_db(default_db))
        .or_else(|| std::env::current_dir().ok())
}

fn optional_string(args: &Value, key: &str) -> std::result::Result<(), String> {
    match args.get(key) {
        None => Ok(()),
        Some(value) if value.as_str().is_some() => Ok(()),
        _ => Err(format!("invalid string argument '{key}'")),
    }
}

fn optional_bool(args: &Value, key: &str) -> std::result::Result<(), String> {
    match args.get(key) {
        None => Ok(()),
        Some(value) if value.as_bool().is_some() => Ok(()),
        _ => Err(format!("invalid boolean argument '{key}'")),
    }
}

#[cfg(feature = "codesearch")]
fn optional_number(args: &Value, key: &str) -> std::result::Result<(), String> {
    match args.get(key) {
        None => Ok(()),
        Some(value) if value.as_f64().is_some() => Ok(()),
        _ => Err(format!("invalid number argument '{key}'")),
    }
}

fn require_string(args: &Value, key: &str) -> std::result::Result<(), String> {
    match args.get(key) {
        Some(value) if value.as_str().is_some() => Ok(()),
        _ => Err(format!("missing string argument '{key}'")),
    }
}

fn require_usize(args: &Value, key: &str) -> std::result::Result<(), String> {
    match args.get(key) {
        Some(value) if value.as_u64().is_none() => Err(format!("invalid integer argument '{key}'")),
        Some(value) => usize::try_from(value.as_u64().unwrap())
            .map(|_| ())
            .map_err(|_| format!("integer argument '{key}' is too large")),
        None => Ok(()),
    }
}

fn require_files(args: &Value) -> std::result::Result<(), String> {
    match args.get("files") {
        Some(Value::Array(files))
            if !files.is_empty() && files.iter().all(|file| file.as_str().is_some()) =>
        {
            Ok(())
        }
        Some(_) => Err("invalid string array argument 'files'".to_string()),
        None => Err("missing required argument 'files'".to_string()),
    }
}

fn files_arg(args: &Value) -> Vec<String> {
    args.get("files")
        .and_then(|v| v.as_array())
        .into_iter()
        .flatten()
        .filter_map(|v| v.as_str())
        .map(str::to_string)
        .collect()
}

fn require_roots(args: &Value) -> std::result::Result<(), String> {
    match args.get("roots") {
        None => Ok(()),
        Some(Value::Array(roots)) => {
            if roots.iter().all(|root| root.as_str().is_some()) {
                Ok(())
            } else {
                Err("invalid string array argument 'roots'".to_string())
            }
        }
        Some(_) => Err("invalid string array argument 'roots'".to_string()),
    }
}

fn require_format(args: &Value) -> std::result::Result<(), String> {
    match args.get("format") {
        Some(value) if matches!(value.as_str(), Some("json" | "markdown")) => Ok(()),
        Some(value) if value.as_str().is_some() => Err(format!(
            "invalid context format '{}'",
            value.as_str().unwrap()
        )),
        Some(_) => Err("invalid string argument 'format'".to_string()),
        _ => Ok(()),
    }
}

fn ok(id: Option<Value>, result: Value) -> Value {
    json!({ "jsonrpc": "2.0", "id": id, "result": result })
}

fn error(id: Option<Value>, code: i64, message: &str) -> Value {
    json!({ "jsonrpc": "2.0", "id": id, "error": { "code": code, "message": message } })
}

#[derive(Serialize)]
struct ReposResponse {
    default: RepoInfo,
    repos: Vec<RepoInfo>,
}

#[derive(Serialize)]
struct RepoInfo {
    repo: Option<String>,
    db: String,
    exists: bool,
    metadata: BTreeMap<String, String>,
}

fn repos_response(default_db: &Path, args: &Value) -> Result<ReposResponse> {
    let mut seen = BTreeSet::new();
    let mut repos = Vec::new();
    for root in roots_arg(args) {
        for db in graph_dbs_one_level(&root)? {
            let key = db.to_string_lossy().to_string();
            if seen.insert(key) {
                repos.push(repo_info(&db)?);
            }
        }
    }
    Ok(ReposResponse {
        default: repo_info(default_db)?,
        repos,
    })
}

fn roots_arg(args: &Value) -> Vec<PathBuf> {
    args.get("roots")
        .and_then(|v| v.as_array())
        .into_iter()
        .flatten()
        .filter_map(|v| v.as_str())
        .map(expand_tilde)
        .collect()
}

fn graph_dbs_one_level(root: &Path) -> Result<Vec<PathBuf>> {
    let mut dbs = Vec::new();
    let direct = root.join(".graphtrail").join("graphtrail.db");
    if direct.exists() {
        dbs.push(direct);
    }
    if !root.is_dir() {
        return Ok(dbs);
    }
    for entry in fs::read_dir(root)? {
        let entry = entry?;
        let path = entry.path();
        if path.is_dir() {
            let db = path.join(".graphtrail").join("graphtrail.db");
            if db.exists() {
                dbs.push(db);
            }
        }
    }
    dbs.sort();
    Ok(dbs)
}

fn repo_info(db: &Path) -> Result<RepoInfo> {
    Ok(RepoInfo {
        repo: repo_from_db(db).map(|path| path.to_string_lossy().to_string()),
        db: db.to_string_lossy().to_string(),
        exists: db.exists(),
        metadata: db_metadata(db)?,
    })
}

fn repo_from_db(db: &Path) -> Option<PathBuf> {
    let graph_dir = db.parent()?;
    if graph_dir.file_name()? != ".graphtrail" {
        return None;
    }
    let parent = graph_dir.parent()?;
    // A relative default like `.graphtrail/graphtrail.db` has an empty parent here;
    // the repo root in that case is the current directory.
    if parent.as_os_str().is_empty() {
        std::env::current_dir().ok()
    } else {
        Some(parent.to_path_buf())
    }
}

fn db_metadata(db: &Path) -> Result<BTreeMap<String, String>> {
    let mut out = BTreeMap::new();
    if !db.exists() {
        return Ok(out);
    }
    let conn = open_read_only(db)?;
    for key in ["schema_version", "tool_version", "synced_at"] {
        if let Some(value) = conn
            .query_row(
                "SELECT value FROM meta WHERE key = ?1",
                params![key],
                |row| row.get::<_, String>(0),
            )
            .optional()?
        {
            out.insert(key.to_string(), value);
        }
    }
    Ok(out)
}

fn expand_tilde(path: &str) -> PathBuf {
    let home = (path == "~" || path.starts_with("~/"))
        .then(|| std::env::var_os("HOME"))
        .flatten();
    if let Some(home) = home {
        let mut expanded = PathBuf::from(home);
        if path.len() > 2 {
            expanded.push(&path[2..]);
        }
        return expanded;
    }
    PathBuf::from(path)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tool_specs_are_unique_complete_and_refresh_consistent() {
        let specs = tool_specs();
        let names: BTreeSet<_> = specs.iter().map(|spec| spec.name).collect();
        assert_eq!(names.len(), specs.len(), "tool names must be unique");
        for (index, spec) in specs.iter().enumerate() {
            assert!(
                !specs[..index].iter().any(|prior| prior.id == spec.id),
                "dispatch identity must be unique for {}",
                spec.name
            );
        }

        let mut expected = BTreeSet::from([
            "search",
            "callers",
            "callees",
            "impact",
            "context",
            "stats",
            "doctor",
            "file_neighbors",
            "dead_code",
            "cycles",
            "affected",
            "diff",
            "repos",
        ]);
        #[cfg(feature = "codesearch")]
        expected.insert("semantic_search");
        assert_eq!(names, expected);

        let mut refresh_tools = BTreeSet::from([
            "search",
            "callers",
            "callees",
            "impact",
            "context",
            "stats",
            "file_neighbors",
            "dead_code",
            "cycles",
            "affected",
        ]);
        #[cfg(feature = "codesearch")]
        refresh_tools.insert("semantic_search");
        for spec in specs {
            assert_eq!(
                spec.supports_refresh,
                refresh_tools.contains(spec.name),
                "refresh policy drifted for {}",
                spec.name
            );
            assert_eq!(spec.input_schema["type"], "object", "{}", spec.name);
        }
    }
}
