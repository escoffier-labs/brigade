//! Integration tests for the MCP request handler against a real read-only graph db.

use std::fs;

use graphtrail::mcp::handle_request;
use graphtrail::store::{init_schema, open_db, open_read_only, sync_repo};
use serde_json::json;

fn ro_conn() -> (tempfile::TempDir, rusqlite::Connection) {
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
    drop(conn);
    let ro = open_read_only(&db).unwrap();
    (dir, ro)
}

#[test]
fn initialize_echoes_protocol_and_advertises_tools() {
    let (_dir, conn) = ro_conn();
    let resp = handle_request(
        &conn,
        &json!({"jsonrpc":"2.0","id":1,"method":"initialize",
                "params":{"protocolVersion":"2025-06-18"}}),
    )
    .unwrap();
    assert_eq!(resp["result"]["protocolVersion"], "2025-06-18");
    assert_eq!(resp["result"]["serverInfo"]["name"], "graphtrail");
}

#[test]
fn notifications_get_no_response() {
    let (_dir, conn) = ro_conn();
    let resp = handle_request(
        &conn,
        &json!({"jsonrpc":"2.0","method":"notifications/initialized"}),
    );
    assert!(resp.is_none());
}

#[test]
fn tools_list_exposes_the_six_query_tools() {
    let (_dir, conn) = ro_conn();
    let resp = handle_request(
        &conn,
        &json!({"jsonrpc":"2.0","id":2,"method":"tools/list"}),
    )
    .unwrap();
    let names: Vec<&str> = resp["result"]["tools"]
        .as_array()
        .unwrap()
        .iter()
        .map(|t| t["name"].as_str().unwrap())
        .collect();
    for expected in ["search", "callers", "callees", "impact", "context", "stats"] {
        assert!(names.contains(&expected), "missing tool: {expected}");
    }
}

#[test]
fn tools_call_search_returns_json_content() {
    let (_dir, conn) = ro_conn();
    let resp = handle_request(
        &conn,
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
fn tools_call_unknown_tool_is_error() {
    let (_dir, conn) = ro_conn();
    let resp = handle_request(
        &conn,
        &json!({"jsonrpc":"2.0","id":4,"method":"tools/call",
                "params":{"name":"nope","arguments":{}}}),
    )
    .unwrap();
    assert_eq!(resp["result"]["isError"], true);
}

#[test]
fn unknown_method_with_id_returns_error() {
    let (_dir, conn) = ro_conn();
    let resp = handle_request(&conn, &json!({"jsonrpc":"2.0","id":5,"method":"bogus"})).unwrap();
    assert_eq!(resp["error"]["code"], -32601);
}
