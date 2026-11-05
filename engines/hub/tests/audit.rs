use brigade_hub::schema::audit;
use rusqlite::{Connection, OpenFlags};

#[test]
fn native_audit_records_writes_locks_and_route() {
    let dir = std::env::temp_dir().join(format!("hub-audit-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let db = dir.join("hub.db");
    Connection::open(&db).unwrap();
    let route = dir.join("routed.db");
    std::fs::hard_link(&db, &route).unwrap();
    let log = dir.join("audit.jsonl");
    std::fs::File::create(&log).unwrap();
    assert!(
        std::process::Command::new(std::env::current_exe().unwrap())
            .args(["--ignored", "--exact", "audit_child"])
            .env("HUB_AUDIT_FILE", &log)
            .env("HUB_AUDIT_DB", &db)
            .env("HUB_AUDIT_DB_ROUTE", &route)
            .status()
            .unwrap()
            .success()
    );
    let records: Vec<serde_json::Value> = std::fs::read_to_string(&log)
        .unwrap()
        .lines()
        .map(|s| serde_json::from_str(s).unwrap())
        .collect();
    assert!(records.iter().any(|r| r["kind"] == "ready"));
    assert!(records.iter().filter(|r| r["kind"] == "write").count() >= 3);
    assert!(records.iter().any(|r| r["kind"] == "begin"));
    assert_eq!(
        Connection::open(&route)
            .unwrap()
            .query_row("SELECT count(*) FROM events", [], |r| r.get::<_, i64>(0))
            .unwrap(),
        1
    );
    std::fs::remove_dir_all(dir).unwrap();
}
#[test]
#[ignore = "subprocess entry for native_audit_records_writes_locks_and_route"]
fn audit_child() {
    let db = std::env::var_os("HUB_AUDIT_DB").unwrap();
    let c = audit::open(std::path::Path::new(&db), OpenFlags::SQLITE_OPEN_READ_WRITE).unwrap();
    assert_eq!(
        c.path(),
        std::env::var("HUB_AUDIT_DB_ROUTE").ok().as_deref()
    );
    c.execute_batch("CREATE TABLE events(x); INSERT INTO events VALUES(1); /* comment */ BEGIN IMMEDIATE; ROLLBACK; PRAGMA wal_checkpoint(PASSIVE);").unwrap();
}
