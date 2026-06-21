//! Integration tests for the MCP request handler against real read-only graph dbs.

use std::fs;
use std::path::PathBuf;

use graphtrail::mcp::handle_request;
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
fn unknown_method_with_id_returns_error() {
    let (_dir, db) = ro_db();
    let resp = handle_request(&db, &json!({"jsonrpc":"2.0","id":5,"method":"bogus"})).unwrap();
    assert_eq!(resp["error"]["code"], -32601);
}
