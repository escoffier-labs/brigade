//! Integration tests for the MCP request handler against real read-only graph dbs.

use std::fs;
use std::io::Cursor;
use std::path::PathBuf;

use graphtrail::mcp::{handle_request, serve};
use graphtrail::store::{init_schema, open_db, sync_repo};
use serde_json::json;
use tempfile::TempDir;

/// Build a synced graph db from a one-file repo; return the temp dir and the db path.
fn ro_db() -> (TempDir, PathBuf) {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    fs::write(
        root.join("app.py"),
        r#"
def helper():
    return 1

def run():
    return helper()
"#,
    )
    .unwrap();
    let db = root.join("graphtrail.db");
    let conn = open_db(&db).unwrap();
    init_schema(&conn).unwrap();
    sync_repo(&conn, root).unwrap();
    (dir, db)
}

#[test]
fn initialize_echoes_protocol_and_advertises_tools() {
    let (_dir, db) = ro_db();
    let resp = handle_request(
        &db,
        &json!({"jsonrpc":"2.0","id":1,"method":"initialize",
                "params":{"protocolVersion":"2025-06-18"}}),
    )
    .unwrap();
    assert_eq!(resp["result"]["protocolVersion"], "2025-06-18");
    assert_eq!(resp["result"]["serverInfo"]["name"], "graphtrail");
}

#[test]
fn notifications_get_no_response() {
    let (_dir, db) = ro_db();
    let resp = handle_request(
        &db,
        &json!({"jsonrpc":"2.0","method":"notifications/initialized"}),
    );
    assert!(resp.is_none());
}

#[test]
fn tools_list_exposes_the_six_query_tools_with_location_args() {
    let (_dir, db) = ro_db();
    let resp = handle_request(&db, &json!({"jsonrpc":"2.0","id":2,"method":"tools/list"})).unwrap();
    let tools = resp["result"]["tools"].as_array().unwrap();
    let names: Vec<&str> = tools.iter().map(|t| t["name"].as_str().unwrap()).collect();
    for expected in ["search", "callers", "callees", "impact", "context", "stats"] {
        assert!(names.contains(&expected), "missing tool: {expected}");
    }
    // Every tool advertises the optional repo/db selector.
    for tool in tools {
        let props = &tool["inputSchema"]["properties"];
        assert!(props.get("repo").is_some(), "{} missing repo", tool["name"]);
        assert!(props.get("db").is_some(), "{} missing db", tool["name"]);
    }
}

#[test]
fn tools_call_search_returns_json_content() {
    let (_dir, db) = ro_db();
    let resp = handle_request(
        &db,
        &json!({"jsonrpc":"2.0","id":3,"method":"tools/call",
                "params":{"name":"search","arguments":{"query":"helper"}}}),
    )
    .unwrap();
    assert_eq!(resp["result"]["isError"], false);
    let text = resp["result"]["content"][0]["text"].as_str().unwrap();
    let rows: serde_json::Value = serde_json::from_str(text).unwrap();
    assert!(
        rows.as_array()
            .unwrap()
            .iter()
            .any(|r| r["name"] == "helper")
    );
}

#[test]
fn tools_call_context_defaults_to_json_content() {
    let (_dir, db) = ro_db();
    let resp = handle_request(
        &db,
        &json!({"jsonrpc":"2.0","id":9,"method":"tools/call",
                "params":{"name":"context","arguments":{"task":"helper"}}}),
    )
    .unwrap();
    assert_eq!(resp["result"]["isError"], false);
    let text = resp["result"]["content"][0]["text"].as_str().unwrap();
    let pack: serde_json::Value = serde_json::from_str(text).unwrap();
    assert_eq!(pack["task"], "helper");
    assert!(
        pack["entry_points"]
            .as_array()
            .unwrap()
            .iter()
            .any(|r| r["name"] == "helper")
    );
}

#[test]
fn tools_call_context_can_return_markdown_content() {
    let (_dir, db) = ro_db();
    let resp = handle_request(
        &db,
        &json!({"jsonrpc":"2.0","id":10,"method":"tools/call",
                "params":{"name":"context","arguments":{"task":"helper","format":"markdown"}}}),
    )
    .unwrap();
    assert_eq!(resp["result"]["isError"], false);
    let text = resp["result"]["content"][0]["text"].as_str().unwrap();

    assert!(text.starts_with("# Context Pack: helper"));
    assert!(text.contains("- `helper` (function) - app.py:2-3"));
    assert!(text.contains("- `run` -> `helper` - app.py:6 -> app.py"));
}

#[test]
fn per_call_db_override_targets_another_repo() {
    // Default db has `helper`; a second repo has `widget`. A call with an explicit `db`
    // arg must query the second repo, not the default.
    let (_dir, default_db) = ro_db();
    let other = tempfile::tempdir().unwrap();
    fs::write(other.path().join("w.py"), "def widget():\n    return 1\n").unwrap();
    let other_db = other.path().join("graphtrail.db");
    let conn = open_db(&other_db).unwrap();
    init_schema(&conn).unwrap();
    sync_repo(&conn, other.path()).unwrap();

    let resp = handle_request(
        &default_db,
        &json!({"jsonrpc":"2.0","id":7,"method":"tools/call",
                "params":{"name":"search","arguments":{"query":"widget","db": other_db.to_str().unwrap()}}}),
    )
    .unwrap();
    let text = resp["result"]["content"][0]["text"].as_str().unwrap();
    let rows: serde_json::Value = serde_json::from_str(text).unwrap();
    assert!(
        rows.as_array()
            .unwrap()
            .iter()
            .any(|r| r["name"] == "widget")
    );
}

#[test]
fn per_call_repo_override_resolves_db_path() {
    let (_dir, default_db) = ro_db();
    let other = tempfile::tempdir().unwrap();
    fs::write(other.path().join("w.py"), "def gadget():\n    return 1\n").unwrap();
    let conn = open_db(&other.path().join(".graphtrail").join("graphtrail.db")).unwrap();
    init_schema(&conn).unwrap();
    sync_repo(&conn, other.path()).unwrap();

    let resp = handle_request(
        &default_db,
        &json!({"jsonrpc":"2.0","id":8,"method":"tools/call",
                "params":{"name":"search","arguments":{"query":"gadget","repo": other.path().to_str().unwrap()}}}),
    )
    .unwrap();
    let text = resp["result"]["content"][0]["text"].as_str().unwrap();
    let rows: serde_json::Value = serde_json::from_str(text).unwrap();
    assert!(
        rows.as_array()
            .unwrap()
            .iter()
            .any(|r| r["name"] == "gadget")
    );
}

#[test]
fn tools_call_unknown_tool_is_error() {
    let (_dir, db) = ro_db();
    let resp = handle_request(
        &db,
        &json!({"jsonrpc":"2.0","id":4,"method":"tools/call",
                "params":{"name":"nope","arguments":{}}}),
    )
    .unwrap();
    assert_eq!(resp["result"]["isError"], true);
}

#[test]
fn serve_returns_parse_error_for_invalid_json() {
    let (_dir, db) = ro_db();
    let mut output = Vec::new();

    serve(&db, Cursor::new("{not json}\n"), &mut output).unwrap();

    let resp: serde_json::Value = serde_json::from_slice(&output).unwrap();
    assert_eq!(resp["id"], serde_json::Value::Null);
    assert_eq!(resp["error"]["code"], -32700);
}

#[test]
fn non_object_request_returns_invalid_request() {
    let (_dir, db) = ro_db();

    let resp = handle_request(&db, &json!("not a request")).unwrap();

    assert_eq!(resp["id"], serde_json::Value::Null);
    assert_eq!(resp["error"]["code"], -32600);
}

#[test]
fn missing_jsonrpc_returns_invalid_request() {
    let (_dir, db) = ro_db();

    let resp = handle_request(&db, &json!({"id": 9, "method": "ping"})).unwrap();

    assert_eq!(resp["id"], 9);
    assert_eq!(resp["error"]["code"], -32600);
}

#[test]
fn tools_call_missing_required_argument_returns_invalid_params() {
    let (_dir, db) = ro_db();

    let resp = handle_request(
        &db,
        &json!({"jsonrpc":"2.0","id":10,"method":"tools/call",
                "params":{"name":"search","arguments":{}}}),
    )
    .unwrap();

    assert_eq!(resp["id"], 10);
    assert_eq!(resp["error"]["code"], -32602);
}

#[test]
fn tools_call_malformed_params_returns_invalid_params() {
    let (_dir, db) = ro_db();

    let resp = handle_request(
        &db,
        &json!({"jsonrpc":"2.0","id":11,"method":"tools/call",
                "params":{"name":"search","arguments":[]}}),
    )
    .unwrap();

    assert_eq!(resp["id"], 11);
    assert_eq!(resp["error"]["code"], -32602);
}

#[test]
fn tools_call_non_string_repo_selector_returns_invalid_params() {
    let (_dir, db) = ro_db();

    let resp = handle_request(
        &db,
        &json!({"jsonrpc":"2.0","id":12,"method":"tools/call",
                "params":{"name":"stats","arguments":{"repo":42}}}),
    )
    .unwrap();

    assert_eq!(resp["id"], 12);
    assert_eq!(resp["error"]["code"], -32602);

    let resp = handle_request(
        &db,
        &json!({"jsonrpc":"2.0","id":13,"method":"tools/call",
                "params":{"name":"stats","arguments":{"db":["not","a","path"]}}}),
    )
    .unwrap();

    assert_eq!(resp["id"], 13);
    assert_eq!(resp["error"]["code"], -32602);
}

#[test]
fn tools_call_execution_failure_stays_tool_error_result() {
    let (_dir, db) = ro_db();
    let missing_db = db.with_file_name("missing.db");

    let resp = handle_request(
        &db,
        &json!({"jsonrpc":"2.0","id":12,"method":"tools/call",
                "params":{"name":"search","arguments":{"query":"helper","db": missing_db}}}),
    )
    .unwrap();

    assert!(resp.get("error").is_none());
    assert_eq!(resp["result"]["isError"], true);
    assert!(
        resp["result"]["content"][0]["text"]
            .as_str()
            .unwrap()
            .contains("read-only")
    );
    assert!(
        !missing_db.exists(),
        "MCP must not create missing databases"
    );
}

#[test]
fn unknown_method_with_id_returns_error() {
    let (_dir, db) = ro_db();
    let resp = handle_request(&db, &json!({"jsonrpc":"2.0","id":5,"method":"bogus"})).unwrap();
    assert_eq!(resp["error"]["code"], -32601);
}
