//! Contract tests for the versioned JSON pack shape and the meta provenance table.

use std::fs;

use graphtrail::model::{ContextPack, GraphQueryResult, SymbolCandidate};
use graphtrail::store::{SCHEMA_VERSION, init_schema, meta, open_db, sync_repo};

#[test]
fn context_pack_json_has_versioned_stable_shape() {
    let pack = ContextPack {
        schema_version: SCHEMA_VERSION,
        task: "demo".to_string(),
        entry_points: vec![],
        callers: vec![],
        callees: vec![],
        related_files: vec![],
    };
    let value: serde_json::Value = serde_json::to_value(&pack).unwrap();
    let obj = value.as_object().unwrap();
    for key in [
        "schema_version",
        "task",
        "entry_points",
        "callers",
        "callees",
        "related_files",
    ] {
        assert!(obj.contains_key(key), "missing key: {key}");
    }
    assert_eq!(obj["schema_version"], serde_json::json!(SCHEMA_VERSION));
}

#[test]
fn graph_query_json_has_resolution_fields_and_edges() {
    let candidate = SymbolCandidate {
        id: "abc".to_string(),
        kind: "function".to_string(),
        name: "run".to_string(),
        qualified_name: "run".to_string(),
        file_path: "src/a.py".to_string(),
        start_line: 1,
        end_line: 2,
    };
    let result = GraphQueryResult {
        query: "run".to_string(),
        resolution: "name".to_string(),
        fuzzy: false,
        ambiguous: true,
        selected: vec![candidate.clone()],
        candidates: vec![candidate],
        edges: vec![],
    };
    let value: serde_json::Value = serde_json::to_value(&result).unwrap();
    let obj = value.as_object().unwrap();
    let mut keys: Vec<&str> = obj.keys().map(String::as_str).collect();
    keys.sort_unstable();
    assert_eq!(
        keys,
        vec![
            "ambiguous",
            "candidates",
            "edges",
            "fuzzy",
            "query",
            "resolution",
            "selected"
        ]
    );
    let candidate = obj["candidates"][0].as_object().unwrap();
    let mut keys: Vec<&str> = candidate.keys().map(String::as_str).collect();
    keys.sort_unstable();
    assert_eq!(
        keys,
        vec![
            "end_line",
            "file_path",
            "id",
            "kind",
            "name",
            "qualified_name",
            "start_line"
        ]
    );
}

#[test]
fn sync_records_provenance_in_meta() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    fs::write(root.join("m.py"), "def f():\n    return 1\n").unwrap();

    let conn = open_db(&root.join("graphtrail.db")).unwrap();
    init_schema(&conn).unwrap();
    sync_repo(&conn, root).unwrap();

    assert_eq!(
        meta::read(&conn, "schema_version").unwrap().as_deref(),
        Some(SCHEMA_VERSION.to_string().as_str())
    );
    assert_eq!(
        meta::read(&conn, "tool_version").unwrap().as_deref(),
        Some(env!("CARGO_PKG_VERSION"))
    );
    assert!(meta::read(&conn, "synced_at").unwrap().is_some());
}

#[test]
fn doctor_exposes_stale_resolver_markers_and_sync_repairs_them() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    let db = root.join("g.db");
    fs::write(root.join("main.py"), "def run():\n    pass\n").unwrap();
    let conn = open_db(&db).unwrap();
    init_schema(&conn).unwrap();
    sync_repo(&conn, root).unwrap();
    for key in ["resolver_version", "resolver_synced_at"] {
        for value in [None, Some("obsolete")] {
            conn.execute("DELETE FROM meta WHERE key = ?1", [key])
                .unwrap();
            if let Some(value) = value {
                meta::upsert(&conn, key, value).unwrap();
            }
            let report = graphtrail::query::doctor(&conn, root, &db).unwrap();
            assert_eq!(report.verdict, "STALE", "{key}={value:?}");
            assert_eq!(report.exit_code(), 1);
            let json = serde_json::to_value(&report).unwrap();
            assert_eq!(json["resolver"]["stale"], true);
            assert!(json["resolver"]["current"].is_string());
            assert_eq!(
                json["resolver"]["stored"],
                serde_json::to_value(meta::read(&conn, "resolver_version").unwrap()).unwrap()
            );
            sync_repo(&conn, root).unwrap();
            let fresh = graphtrail::query::doctor(&conn, root, &db).unwrap();
            assert_eq!(fresh.verdict, "FRESH");
            assert_eq!(fresh.exit_code(), 0);
            let json = serde_json::to_value(fresh).unwrap();
            assert_eq!(json["resolver"]["stale"], false);
            assert_eq!(json["resolver"]["stored"], json["resolver"]["current"]);
        }
    }
}
