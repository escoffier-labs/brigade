use brigade_hub::schema::{self, SchemaGate};
use rusqlite::Connection;
use std::{path::PathBuf, process::Command};
fn fixture() -> PathBuf {
    let dir = std::env::temp_dir().join(format!("hub-drift-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    assert!(Command::new("python3").env("PYTHONPATH", concat!(env!("CARGO_MANIFEST_DIR"), "/../../src")).args(["-c", "from pathlib import Path; from brigade import fleet_hub; import sys; fleet_hub.init_db(Path(sys.argv[1])).close()"]).arg(dir.join("hub.db")).status().unwrap().success());
    dir.join("hub.db")
}
#[test]
fn schema_gate_fails_closed_then_recovers_and_mutations_are_detected() {
    let path = fixture();
    let c = Connection::open(&path).unwrap();
    let gate = SchemaGate::default();
    gate.recheck(&c).unwrap();
    assert!(gate.ready());
    c.execute_batch("BEGIN; DROP TRIGGER fleet_questions_reply")
        .unwrap();
    assert!(gate.recheck(&c).is_err());
    assert!(!gate.ready());
    c.execute_batch("ROLLBACK").unwrap();
    gate.recheck(&c).unwrap();
    assert!(gate.ready());
    for sql in [
        "PRAGMA user_version=999",
        "ALTER TABLE events ADD COLUMN unexpected TEXT",
        "DROP INDEX events_received_at",
    ] {
        c.execute_batch(&format!("BEGIN; {sql}")).unwrap();
        assert!(schema::check(&c).is_err(), "missed {sql}");
        c.execute_batch("ROLLBACK").unwrap();
    }
    drop(c);
    std::fs::remove_dir_all(path.parent().unwrap()).unwrap();
}
/// The production hub's appended claims and model_policy layouts, opened by
/// Python's real init_db, pass the Rust gate. Real differences still fail.
#[test]
fn production_upgrade_layouts_pass_the_gate_after_python_startup() {
    let dir = std::env::temp_dir().join(format!("hub-production-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let path = dir.join("hub.db");
    let ddl = include_str!("../schema/upgrade-fixtures/production-v50.sql");
    Connection::open(&path).unwrap().execute_batch(ddl).unwrap();
    assert!(Command::new("python3").env("PYTHONPATH", concat!(env!("CARGO_MANIFEST_DIR"), "/../../src")).args(["-c", "from pathlib import Path; from brigade import fleet_hub; import sys; fleet_hub.init_db(Path(sys.argv[1])).close()"]).arg(&path).status().unwrap().success());
    let c = Connection::open(&path).unwrap();
    schema::check(&c).unwrap();
    for sql in [
        "ALTER TABLE claims DROP COLUMN session",
        "ALTER TABLE model_policy DROP COLUMN notes",
        "ALTER TABLE model_policy ADD COLUMN unexpected TEXT",
    ] {
        c.execute_batch(&format!("BEGIN; {sql}")).unwrap();
        assert!(schema::check(&c).is_err(), "missed {sql}");
        c.execute_batch("ROLLBACK").unwrap();
    }
    schema::check(&c).unwrap();
    drop(c);
    std::fs::remove_dir_all(dir).unwrap();
}
