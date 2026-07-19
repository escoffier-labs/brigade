//! Contract tests for the versioned JSON pack shape and the meta provenance table.

use std::fs;

use graphtrail::model::ContextPack;
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
