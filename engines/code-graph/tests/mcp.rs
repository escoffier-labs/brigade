//! Integration tests for the MCP request handler with real graph dbs, read-only query connections,
//! and the opt-in `refresh: true` incremental sync writer with its 10-second fail-open wait.

use std::fs;
use std::io::Cursor;
#[cfg(feature = "codesearch")]
use std::io::{Read, Write};
#[cfg(feature = "codesearch")]
use std::net::TcpListener;
use std::path::PathBuf;
#[cfg(feature = "codesearch")]
use std::sync::Mutex;
#[cfg(feature = "codesearch")]
use std::thread::{self, JoinHandle};
#[cfg(feature = "codesearch")]
use std::time::{Duration, Instant};

use graphtrail::mcp::{handle_request, serve};
use graphtrail::query::build_context_pack;
use graphtrail::store::{init_schema, open_db, sync_repo};
use serde_json::json;
use tempfile::TempDir;

#[cfg(feature = "codesearch")]
static CODE_SEARCH_ENV_LOCK: Mutex<()> = Mutex::new(());

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
fn tools_list_exposes_the_query_tools_with_location_args() {
    let (_dir, db) = ro_db();
    let resp = handle_request(&db, &json!({"jsonrpc":"2.0","id":2,"method":"tools/list"})).unwrap();
    let tools = resp["result"]["tools"].as_array().unwrap();
    let names: Vec<&str> = tools.iter().map(|t| t["name"].as_str().unwrap()).collect();
    let mut expected = vec![
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
    ];
    #[cfg(feature = "codesearch")]
    expected.insert(1, "semantic_search");
    assert_eq!(names, expected);
    // Every single-db tool advertises the optional repo/db selector. `diff` is the
    // exception: it takes two explicit db paths (`before`/`after`) instead.
    for tool in tools {
        let props = &tool["inputSchema"]["properties"];
        if tool["name"] == "diff" {
            assert!(props.get("before").is_some(), "diff missing before");
            assert!(props.get("after").is_some(), "diff missing after");
            assert!(props.get("refresh").is_none(), "diff must not refresh");
            continue;
        }
        assert!(props.get("repo").is_some(), "{} missing repo", tool["name"]);
        assert!(props.get("db").is_some(), "{} missing db", tool["name"]);
        if matches!(tool["name"].as_str(), Some("repos" | "doctor")) {
            assert!(
                props.get("refresh").is_none(),
                "{} must not refresh",
                tool["name"]
            );
        } else {
            assert!(
                props.get("refresh").is_some(),
                "{} missing refresh",
                tool["name"]
            );
        }
        if tool["name"] == "context" {
            #[cfg(not(feature = "codesearch"))]
            assert!(
                props.get("blend_code_search").is_none(),
                "default context schema must not expose codesearch arguments"
            );
            #[cfg(feature = "codesearch")]
            assert!(
                props.get("blend_code_search").is_some(),
                "codesearch context schema must expose blend_code_search"
            );
        }
    }
}

#[test]
fn tools_call_doctor_returns_freshness_report() {
    let (dir, db) = ro_db();
    let resp = handle_request(
        &db,
        &json!({"jsonrpc":"2.0","id":56,"method":"tools/call",
                "params":{"name":"doctor","arguments":{}}}),
    )
    .unwrap();

    assert_eq!(resp["result"]["isError"], false);
    let text = resp["result"]["content"][0]["text"].as_str().unwrap();
    let report: serde_json::Value = serde_json::from_str(text).unwrap();
    assert_eq!(report["repo_root"], dir.path().to_string_lossy().as_ref());
    assert_eq!(report["db_path"], db.to_string_lossy().as_ref());
    assert_eq!(report["verdict"], "FRESH");
    assert_eq!(report["pending"]["new_files"], 0);
    assert_eq!(report["pending"]["changed_files"], 0);
    assert_eq!(report["pending"]["deleted_files"], 0);
    assert_eq!(report["pending"]["fingerprint_stale"], 0);
    assert_eq!(report["schema"]["needs_migration"], false);
}

#[test]
fn tools_call_doctor_missing_db_stays_tool_error_result() {
    let (_dir, db) = ro_db();
    let missing_db = db.with_file_name("missing-doctor.db");

    let resp = handle_request(
        &db,
        &json!({"jsonrpc":"2.0","id":57,"method":"tools/call",
                "params":{"name":"doctor","arguments":{"db": missing_db}}}),
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
        "MCP doctor must not create missing databases"
    );
}

#[test]
fn tools_call_diff_reports_added_symbols_and_edges() {
    // Two separate indexed DBs (before/after); `after` adds a function and a call.
    let before = tempfile::tempdir().unwrap();
    fs::write(before.path().join("m.py"), "def foo():\n    return 1\n").unwrap();
    let before_db = before.path().join("graphtrail.db");
    let conn = open_db(&before_db).unwrap();
    init_schema(&conn).unwrap();
    sync_repo(&conn, before.path()).unwrap();

    let after = tempfile::tempdir().unwrap();
    fs::write(
        after.path().join("m.py"),
        "def foo():\n    return 1\n\ndef bar():\n    foo()\n",
    )
    .unwrap();
    let after_db = after.path().join("graphtrail.db");
    let conn = open_db(&after_db).unwrap();
    init_schema(&conn).unwrap();
    sync_repo(&conn, after.path()).unwrap();

    let resp = handle_request(
        &before_db,
        &json!({"jsonrpc":"2.0","id":50,"method":"tools/call",
                "params":{"name":"diff","arguments":{
                    "before": before_db.to_str().unwrap(),
                    "after": after_db.to_str().unwrap()}}}),
    )
    .unwrap();

    assert_eq!(resp["result"]["isError"], false);
    let text = resp["result"]["content"][0]["text"].as_str().unwrap();
    let diff: serde_json::Value = serde_json::from_str(text).unwrap();
    assert_eq!(diff["summary"]["added_nodes"], 1);
    assert_eq!(diff["summary"]["added_edges"], 1);
    assert_eq!(diff["added_nodes"][0]["qualified_name"], "bar");
}

#[test]
fn tools_call_diff_missing_after_returns_invalid_params() {
    let (_dir, db) = ro_db();
    let resp = handle_request(
        &db,
        &json!({"jsonrpc":"2.0","id":51,"method":"tools/call",
                "params":{"name":"diff","arguments":{"before": db.to_str().unwrap()}}}),
    )
    .unwrap();
    assert_eq!(resp["error"]["code"], -32602);
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
fn tools_call_refresh_absent_or_false_does_not_resync() {
    let dir = synced_repo_with_graph_dir();
    let root = dir.path();
    let db = root.join(".graphtrail").join("graphtrail.db");
    fs::write(root.join("late.py"), "def late_symbol():\n    return 2\n").unwrap();

    let absent = handle_request(
        &db,
        &json!({"jsonrpc":"2.0","id":52,"method":"tools/call",
        "params":{"name":"search","arguments":{
            "query":"late_symbol",
            "repo": root.to_str().unwrap()
        }}}),
    )
    .unwrap();
    let rows: serde_json::Value =
        serde_json::from_str(absent["result"]["content"][0]["text"].as_str().unwrap()).unwrap();
    assert!(rows.as_array().unwrap().is_empty());

    let explicit_false = handle_request(
        &db,
        &json!({"jsonrpc":"2.0","id":53,"method":"tools/call",
        "params":{"name":"search","arguments":{
            "query":"late_symbol",
            "repo": root.to_str().unwrap(),
            "refresh": false
        }}}),
    )
    .unwrap();
    let rows: serde_json::Value = serde_json::from_str(
        explicit_false["result"]["content"][0]["text"]
            .as_str()
            .unwrap(),
    )
    .unwrap();
    assert!(rows.as_array().unwrap().is_empty());
}

#[test]
fn tools_call_refresh_true_resyncs_before_querying() {
    let dir = synced_repo_with_graph_dir();
    let root = dir.path();
    let db = root.join(".graphtrail").join("graphtrail.db");
    fs::write(root.join("late.py"), "def late_symbol():\n    return 2\n").unwrap();

    let resp = handle_request(
        &db,
        &json!({"jsonrpc":"2.0","id":54,"method":"tools/call",
        "params":{"name":"search","arguments":{
            "query":"late_symbol",
            "repo": root.to_str().unwrap(),
            "refresh": true
        }}}),
    )
    .unwrap();

    assert_eq!(resp["result"]["isError"], false);
    let text = resp["result"]["content"][0]["text"].as_str().unwrap();
    let rows: serde_json::Value = serde_json::from_str(text).unwrap();
    assert!(
        rows.as_array()
            .unwrap()
            .iter()
            .any(|row| row["name"] == "late_symbol")
    );
}

#[test]
fn tools_call_refresh_failure_still_answers_with_note() {
    let dir = synced_repo_with_graph_dir();
    let root = dir.path();
    let db = root.join(".graphtrail").join("graphtrail.db");
    fs::write(root.join("bad.py"), [0xff, 0xfe, 0xfd]).unwrap();

    let resp = handle_request(
        &db,
        &json!({"jsonrpc":"2.0","id":55,"method":"tools/call",
        "params":{"name":"search","arguments":{
            "query":"helper",
            "repo": root.to_str().unwrap(),
            "refresh": true
        }}}),
    )
    .unwrap();

    assert_eq!(resp["result"]["isError"], false);
    let text = resp["result"]["content"][0]["text"].as_str().unwrap();
    assert!(text.contains("\"name\": \"helper\""));
    assert!(text.contains("refresh_error:"));
}

#[test]
fn read_only_request_accepts_v3_db_without_extractor_fingerprint_column() {
    let (_dir, db) = ro_db();
    {
        let conn = open_db(&db).unwrap();
        conn.execute(
            "UPDATE meta SET value = '3' WHERE key = 'schema_version'",
            [],
        )
        .unwrap();
        conn.execute("ALTER TABLE files DROP COLUMN extractor_fingerprint", [])
            .unwrap();
    }

    let resp = handle_request(
        &db,
        &json!({"jsonrpc":"2.0","id":34,"method":"tools/call",
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
fn tools_call_search_accepts_path_filter() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    fs::create_dir(root.join("tests")).unwrap();
    fs::write(root.join("app.py"), "def helper():\n    return 1\n").unwrap();
    fs::write(
        root.join("tests").join("app_test.py"),
        "def helper():\n    return 2\n",
    )
    .unwrap();
    let db = root.join("graphtrail.db");
    let conn = open_db(&db).unwrap();
    init_schema(&conn).unwrap();
    sync_repo(&conn, root).unwrap();

    let resp = handle_request(
        &db,
        &json!({"jsonrpc":"2.0","id":31,"method":"tools/call",
                "params":{"name":"search","arguments":{"query":"helper","path":"tests"}}}),
    )
    .unwrap();

    assert_eq!(resp["result"]["isError"], false);
    let text = resp["result"]["content"][0]["text"].as_str().unwrap();
    let rows: serde_json::Value = serde_json::from_str(text).unwrap();
    assert_eq!(rows.as_array().unwrap().len(), 1);
    assert_eq!(rows[0]["file_path"], "tests/app_test.py");
}

#[test]
fn tools_call_file_neighbors_returns_adjacent_files() {
    let (_dir, db) = ro_db();

    let resp = handle_request(
        &db,
        &json!({"jsonrpc":"2.0","id":32,"method":"tools/call",
                "params":{"name":"file_neighbors","arguments":{"path":"app.py"}}}),
    )
    .unwrap();

    assert_eq!(resp["result"]["isError"], false);
    let text = resp["result"]["content"][0]["text"].as_str().unwrap();
    let rows: serde_json::Value = serde_json::from_str(text).unwrap();
    assert!(rows.as_array().unwrap().is_empty());
}

#[test]
fn tools_call_dead_code_returns_json_content() {
    let (_dir, db) = ro_db();
    let resp = handle_request(
        &db,
        &json!({"jsonrpc":"2.0","id":81,"method":"tools/call",
                "params":{"name":"dead_code","arguments":{"limit":10}}}),
    )
    .unwrap();

    assert_eq!(resp["result"]["isError"], false);
    let report: serde_json::Value =
        serde_json::from_str(resp["result"]["content"][0]["text"].as_str().unwrap()).unwrap();
    assert!(report["symbols"].is_array());
    assert!(
        report["attribution"]
            .as_str()
            .unwrap()
            .contains("not proof")
    );
}

#[test]
fn tools_call_cycles_returns_json_content() {
    let (_dir, db) = ro_db();
    let resp = handle_request(
        &db,
        &json!({"jsonrpc":"2.0","id":82,"method":"tools/call",
                "params":{"name":"cycles","arguments":{}}}),
    )
    .unwrap();

    assert_eq!(resp["result"]["isError"], false);
    let report: serde_json::Value =
        serde_json::from_str(resp["result"]["content"][0]["text"].as_str().unwrap()).unwrap();
    assert!(report["groups"].is_array());
    assert_eq!(report["total_groups"], 0);
}

#[test]
fn tools_call_affected_returns_json_content() {
    let (_dir, db) = ro_db();
    let resp = handle_request(
        &db,
        &json!({"jsonrpc":"2.0","id":83,"method":"tools/call",
                "params":{"name":"affected","arguments":{"files":["app.py"],"depth":3}}}),
    )
    .unwrap();

    assert_eq!(resp["result"]["isError"], false);
    let report: serde_json::Value =
        serde_json::from_str(resp["result"]["content"][0]["text"].as_str().unwrap()).unwrap();
    assert_eq!(report["changed_files"], json!(["app.py"]));
    assert!(report["affected_tests"].is_array());
}

#[test]
fn tools_call_repos_reports_default_db_and_scanned_roots() {
    let (_dir, default_db) = ro_db();
    let root = tempfile::tempdir().unwrap();
    let repo = root.path().join("repo-a");
    fs::create_dir(&repo).unwrap();
    fs::write(repo.join("a.py"), "def alpha():\n    return 1\n").unwrap();
    let repo_db = repo.join(".graphtrail").join("graphtrail.db");
    let conn = open_db(&repo_db).unwrap();
    init_schema(&conn).unwrap();
    sync_repo(&conn, &repo).unwrap();

    let resp = handle_request(
        &default_db,
        &json!({"jsonrpc":"2.0","id":33,"method":"tools/call",
                "params":{"name":"repos","arguments":{"roots":[root.path().to_str().unwrap()]}}}),
    )
    .unwrap();

    assert_eq!(resp["result"]["isError"], false);
    let text = resp["result"]["content"][0]["text"].as_str().unwrap();
    let value: serde_json::Value = serde_json::from_str(text).unwrap();
    assert_eq!(value["default"]["exists"], true);
    assert!(
        value["default"]["metadata"]["tool_version"]
            .as_str()
            .is_some()
    );
    assert!(
        value["repos"]
            .as_array()
            .unwrap()
            .iter()
            .any(|repo| repo["db"] == repo_db.to_string_lossy().as_ref())
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
fn tools_call_context_without_new_arguments_matches_direct_pack() {
    let (_dir, db) = ro_db();
    let resp = handle_request(
        &db,
        &json!({"jsonrpc":"2.0","id":39,"method":"tools/call",
                "params":{"name":"context","arguments":{"task":"helper"}}}),
    )
    .unwrap();

    assert_eq!(resp["result"]["isError"], false);
    let text = resp["result"]["content"][0]["text"].as_str().unwrap();
    let conn = open_db(&db).unwrap();
    let pack = build_context_pack(&conn, "helper".to_string(), 12).unwrap();
    let expected = serde_json::to_string_pretty(&pack).unwrap();
    assert_eq!(text, expected);
}

#[cfg(feature = "codesearch")]
#[test]
fn tools_call_context_explicit_blend_false_matches_absent_argument() {
    let (_dir, db) = ro_db();
    let absent = with_code_search_url("http://127.0.0.1:9", || {
        handle_request(
            &db,
            &json!({"jsonrpc":"2.0","id":70,"method":"tools/call",
                    "params":{"name":"context","arguments":{"task":"helper"}}}),
        )
        .unwrap()
    });
    let explicit_false = with_code_search_url("http://127.0.0.1:9", || {
        handle_request(
            &db,
            &json!({"jsonrpc":"2.0","id":71,"method":"tools/call",
            "params":{"name":"context","arguments":{
                "task":"helper",
                "blend_code_search": false
            }}}),
        )
        .unwrap()
    });

    assert_eq!(absent["result"]["isError"], false);
    assert_eq!(explicit_false["result"]["isError"], false);
    assert_eq!(
        absent["result"]["content"][0]["text"],
        explicit_false["result"]["content"][0]["text"]
    );
}

#[cfg(feature = "codesearch")]
#[test]
fn tools_call_semantic_search_blends_mock_code_search_hits() {
    let (_dir, db) = ro_db();
    let mock = MockCodeSearch::new(
        "semantic helper",
        r#"{"results":[{"file_path":"app.py","score":0.91},{"file_path":"missing.py","score":0.80}]}"#,
    );

    let resp = with_code_search_url(&mock.base_url, || {
        handle_request(
            &db,
            &json!({"jsonrpc":"2.0","id":72,"method":"tools/call",
            "params":{"name":"semantic_search","arguments":{
                "query":"semantic helper",
                "limit":5,
                "embed_weight":1.0,
                "graph_weight":0.0
            }}}),
        )
        .unwrap()
    });
    mock.join();

    assert_eq!(resp["result"]["isError"], false);
    let text = resp["result"]["content"][0]["text"].as_str().unwrap();
    let rows: serde_json::Value = serde_json::from_str(text).unwrap();
    assert!(
        rows.as_array()
            .unwrap()
            .iter()
            .any(|row| row["symbol"]["file_path"] == "app.py"
                && row["embedding_score"].as_f64().unwrap() > 0.9)
    );
}

#[cfg(feature = "codesearch")]
#[test]
fn tools_call_semantic_search_unreachable_code_search_returns_json_rpc_error() {
    let (_dir, db) = ro_db();
    let resp = with_code_search_url("http://127.0.0.1:9", || {
        handle_request(
            &db,
            &json!({"jsonrpc":"2.0","id":73,"method":"tools/call",
                    "params":{"name":"semantic_search","arguments":{"query":"helper"}}}),
        )
        .unwrap()
    });

    assert_eq!(resp["id"], 73);
    assert_eq!(resp["error"]["code"], -32000);
    assert!(
        resp["error"]["message"]
            .as_str()
            .unwrap()
            .contains("Code Search API is unreachable")
    );
    assert!(
        resp["error"]["message"]
            .as_str()
            .unwrap()
            .contains("CODE_SEARCH_URL")
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
fn tools_call_invalid_new_args_return_invalid_params() {
    let (_dir, db) = ro_db();

    let resp = handle_request(
        &db,
        &json!({"jsonrpc":"2.0","id":41,"method":"tools/call",
                "params":{"name":"search","arguments":{"query":"helper","path":42}}}),
    )
    .unwrap();
    assert_eq!(resp["error"]["code"], -32602);

    let resp = handle_request(
        &db,
        &json!({"jsonrpc":"2.0","id":42,"method":"tools/call",
                "params":{"name":"file_neighbors","arguments":{"path":42}}}),
    )
    .unwrap();
    assert_eq!(resp["error"]["code"], -32602);

    let resp = handle_request(
        &db,
        &json!({"jsonrpc":"2.0","id":43,"method":"tools/call",
                "params":{"name":"repos","arguments":{"roots":["/tmp", 42]}}}),
    )
    .unwrap();
    assert_eq!(resp["error"]["code"], -32602);
}

#[test]
fn tools_call_bad_depth_returns_invalid_params() {
    let (_dir, db) = ro_db();

    let resp = handle_request(
        &db,
        &json!({"jsonrpc":"2.0","id":44,"method":"tools/call",
                "params":{"name":"impact","arguments":{"symbol":"helper","depth":"3"}}}),
    )
    .unwrap();

    assert_eq!(resp["id"], 44);
    assert_eq!(resp["error"]["code"], -32602);
}

#[test]
fn tools_call_affected_rejects_non_array_files() {
    let (_dir, db) = ro_db();
    let resp = handle_request(
        &db,
        &json!({"jsonrpc":"2.0","id":84,"method":"tools/call",
                "params":{"name":"affected","arguments":{"files":"app.py"}}}),
    )
    .unwrap();

    assert_eq!(resp["error"]["code"], -32602);
}

#[test]
fn tools_call_dead_code_rejects_non_integer_limit() {
    let (_dir, db) = ro_db();
    let resp = handle_request(
        &db,
        &json!({"jsonrpc":"2.0","id":85,"method":"tools/call",
                "params":{"name":"dead_code","arguments":{"limit":"10"}}}),
    )
    .unwrap();

    assert_eq!(resp["error"]["code"], -32602);
}

#[test]
fn tools_call_affected_rejects_non_integer_depth() {
    let (_dir, db) = ro_db();
    let resp = handle_request(
        &db,
        &json!({"jsonrpc":"2.0","id":86,"method":"tools/call",
                "params":{"name":"affected","arguments":{"files":["app.py"],"depth":"3"}}}),
    )
    .unwrap();

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

fn synced_repo_with_graph_dir() -> TempDir {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    fs::write(root.join("app.py"), "def helper():\n    return 1\n").unwrap();
    let db = root.join(".graphtrail").join("graphtrail.db");
    let conn = open_db(&db).unwrap();
    init_schema(&conn).unwrap();
    sync_repo(&conn, root).unwrap();
    dir
}

#[cfg(feature = "codesearch")]
fn with_code_search_url<T>(url: &str, f: impl FnOnce() -> T) -> T {
    let _guard = CODE_SEARCH_ENV_LOCK.lock().unwrap();
    let old_url = std::env::var_os("CODE_SEARCH_URL");
    let old_key = std::env::var_os("CODE_SEARCH_API_KEY");
    let old_manifest = std::env::var_os("CODE_INDEX_MANIFEST");
    unsafe {
        std::env::set_var("CODE_SEARCH_URL", url);
        std::env::remove_var("CODE_SEARCH_API_KEY");
        // Point manifest discovery at a path that never exists. Without this,
        // a developer whose real manifest enrolls this checkout gets repo
        // matching and prefix stripping applied to the mock's hits, and the
        // suite fails only on their machine.
        std::env::set_var(
            "CODE_INDEX_MANIFEST",
            "/nonexistent/graphtrail-test-manifest.json",
        );
    }
    let out = f();
    unsafe {
        match old_url {
            Some(value) => std::env::set_var("CODE_SEARCH_URL", value),
            None => std::env::remove_var("CODE_SEARCH_URL"),
        }
        match old_key {
            Some(value) => std::env::set_var("CODE_SEARCH_API_KEY", value),
            None => std::env::remove_var("CODE_SEARCH_API_KEY"),
        }
        match old_manifest {
            Some(value) => std::env::set_var("CODE_INDEX_MANIFEST", value),
            None => std::env::remove_var("CODE_INDEX_MANIFEST"),
        }
    }
    out
}

#[cfg(feature = "codesearch")]
struct MockCodeSearch {
    base_url: String,
    handle: JoinHandle<()>,
}

#[cfg(feature = "codesearch")]
impl MockCodeSearch {
    fn new(expected_query: &'static str, body: &'static str) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let base_url = format!("http://{}", listener.local_addr().unwrap());
        let handle = thread::spawn(move || {
            let deadline = Instant::now() + Duration::from_secs(2);
            let (mut stream, _) = loop {
                match listener.accept() {
                    Ok(accepted) => break accepted,
                    Err(err) if err.kind() == std::io::ErrorKind::WouldBlock => {
                        assert!(Instant::now() < deadline, "mock code search was not called");
                        thread::sleep(Duration::from_millis(10));
                    }
                    Err(err) => panic!("mock code search accept failed: {err}"),
                }
            };
            let mut request = Vec::new();
            let mut buf = [0_u8; 1024];
            loop {
                let read = stream.read(&mut buf).unwrap();
                assert!(read > 0, "mock code search request ended early");
                request.extend_from_slice(&buf[..read]);
                if request_complete(&request) {
                    break;
                }
            }
            let text = String::from_utf8_lossy(&request);
            assert!(text.starts_with("POST /api/search "));
            assert!(text.contains(expected_query));
            let response = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                body.len(),
                body
            );
            stream.write_all(response.as_bytes()).unwrap();
        });
        Self { base_url, handle }
    }

    fn join(self) {
        self.handle.join().unwrap();
    }
}

#[cfg(feature = "codesearch")]
fn request_complete(request: &[u8]) -> bool {
    let Some(header_end) = request.windows(4).position(|window| window == b"\r\n\r\n") else {
        return false;
    };
    let headers = String::from_utf8_lossy(&request[..header_end]);
    let content_length = headers
        .lines()
        .find_map(|line| {
            let (name, value) = line.split_once(':')?;
            if name.eq_ignore_ascii_case("content-length") {
                value.trim().parse::<usize>().ok()
            } else {
                None
            }
        })
        .unwrap_or(0);
    request.len() >= header_end + 4 + content_length
}
