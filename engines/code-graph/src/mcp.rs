//! Minimal MCP server: newline-delimited JSON-RPC 2.0 over stdin/stdout, exposing GraphTrail's
//! read-only queries as tools. No async runtime and no extra dependencies, keeping the sidecar
//! small.
//!
//! Multi-repo: the server holds a default db path but opens the database lazily per `tools/call`.
//! Each tool accepts an optional `repo` (uses `<repo>/.graphtrail/graphtrail.db`) or `db` (explicit
//! path) argument, so a single registered server can answer for any indexed repository. Connections
//! are always opened `SQLITE_OPEN_READ_ONLY`, so the server can never mutate a graph.

use std::io::{BufRead, Write};
use std::path::{Path, PathBuf};

use anyhow::{Result, anyhow};
use rusqlite::Connection;
use serde::Serialize;
use serde_json::{Value, json};

use crate::model::Direction;
use crate::query::{build_context_pack, graph_edges, search_symbols, stats};
use crate::store::open_read_only;

const PROTOCOL_VERSION: &str = "2024-11-05";

/// Read JSON-RPC requests line by line and write one response line per request. `default_db` is the
/// fallback database used when a request does not specify its own `repo`/`db`.
pub fn serve(default_db: &Path, input: impl BufRead, mut output: impl Write) -> Result<()> {
    for line in input.lines() {
        let line = line?;
        if line.trim().is_empty() {
            continue;
        }
        let Ok(req) = serde_json::from_str::<Value>(&line) else {
            continue;
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
    let id = req.get("id").cloned();
    let method = req.get("method").and_then(|m| m.as_str()).unwrap_or("");
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
            let name = req
                .pointer("/params/name")
                .and_then(|v| v.as_str())
                .unwrap_or("");
            let args = req
                .pointer("/params/arguments")
                .cloned()
                .unwrap_or_else(|| json!({}));
            let db = resolve_db(default_db, &args);
            let result = open_read_only(&db).and_then(|conn| call_tool(&conn, name, &args));
            Some(match result {
                Ok(text) => ok(
                    id,
                    json!({ "content": [{ "type": "text", "text": text }], "isError": false }),
                ),
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

fn call_tool(conn: &Connection, name: &str, args: &Value) -> Result<String> {
    match name {
        "search" => to_pretty(&search_symbols(
            conn,
            &str_arg(args, "query")?,
            usize_arg(args, "limit", 20),
        )?),
        "callers" => to_pretty(&graph_edges(
            conn,
            &str_arg(args, "symbol")?,
            Direction::Incoming,
        )?),
        "callees" => to_pretty(&graph_edges(
            conn,
            &str_arg(args, "symbol")?,
            Direction::Outgoing,
        )?),
        "impact" => {
            let symbol = str_arg(args, "symbol")?;
            let mut edges = graph_edges(conn, &symbol, Direction::Incoming)?;
            edges.extend(graph_edges(conn, &symbol, Direction::Outgoing)?);
            to_pretty(&edges)
        }
        "context" => to_pretty(&build_context_pack(
            conn,
            str_arg(args, "task")?,
            usize_arg(args, "limit", 12),
        )?),
        "stats" => to_pretty(&stats(conn)?),
        other => Err(anyhow!("unknown tool '{other}'")),
    }
}

fn tool_defs() -> Value {
    // Every tool also accepts an optional repo/db selector for multi-repo use.
    let location = json!({
        "repo": { "type": "string", "description": "Repo path; uses <repo>/.graphtrail/graphtrail.db." },
        "db": { "type": "string", "description": "Explicit graphtrail.db path (overrides repo and the server default)." }
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
    let symbol_tool = |desc: &str| {
        with_location(
            json!({ "symbol": { "type": "string", "description": desc } }),
            json!(["symbol"]),
        )
    };
    json!([
        {
            "name": "search",
            "description": "Full-text search code symbols (functions, classes, methods) by name.",
            "inputSchema": with_location(
                json!({
                    "query": { "type": "string", "description": "Search terms." },
                    "limit": { "type": "integer", "description": "Max results (default 20)." }
                }),
                json!(["query"])
            )
        },
        { "name": "callers", "description": "Symbols that call the given symbol (incoming call edges).", "inputSchema": symbol_tool("Symbol name to find callers of.") },
        { "name": "callees", "description": "Symbols called by the given symbol (outgoing call edges).", "inputSchema": symbol_tool("Symbol name to find callees of.") },
        { "name": "impact", "description": "Combined callers and callees of a symbol (blast radius of a change).", "inputSchema": symbol_tool("Symbol name to assess impact for.") },
        {
            "name": "context",
            "description": "A context pack for a task: matching entry points plus their caller/callee neighborhood and related files.",
            "inputSchema": with_location(
                json!({
                    "task": { "type": "string", "description": "Task or feature description to gather context for." },
                    "limit": { "type": "integer", "description": "Max entry points (default 12)." }
                }),
                json!(["task"])
            )
        },
        { "name": "stats", "description": "Counts of files, symbols, edges, imports, and the schema version.", "inputSchema": with_location(json!({}), json!([])) }
    ])
}

fn to_pretty<T: Serialize>(value: &T) -> Result<String> {
    Ok(serde_json::to_string_pretty(value)?)
}

fn str_arg(args: &Value, key: &str) -> Result<String> {
    args.get(key)
        .and_then(|v| v.as_str())
        .map(|s| s.to_string())
        .ok_or_else(|| anyhow!("missing string argument '{key}'"))
}

fn usize_arg(args: &Value, key: &str, default: usize) -> usize {
    args.get(key)
        .and_then(|v| v.as_u64())
        .map_or(default, |n| n as usize)
}

fn ok(id: Option<Value>, result: Value) -> Value {
    json!({ "jsonrpc": "2.0", "id": id, "result": result })
}

fn error(id: Option<Value>, code: i64, message: &str) -> Value {
    json!({ "jsonrpc": "2.0", "id": id, "error": { "code": code, "message": message } })
}
