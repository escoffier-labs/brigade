use brigade_hub::{
    maintenance::{DiskPressure, checkpoint},
    schema::DataVersion,
};
use rusqlite::Connection;
use std::time::{Duration, Instant};

#[test]
fn pinned_reader_keeps_checkpoint_busy_but_not_writer() {
    let path = std::env::temp_dir().join(format!("hub-checkpoint-{}.db", std::process::id()));
    let writer = Connection::open(&path).unwrap();
    writer.execute_batch("PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA wal_autocheckpoint=0; CREATE TABLE t(x);").unwrap();
    let reader = Connection::open(&path).unwrap();
    reader
        .execute_batch("BEGIN DEFERRED; SELECT * FROM t;")
        .unwrap();
    writer.execute("INSERT INTO t VALUES(1)", []).unwrap();
    let checkpoint_path = path.clone();
    let (started, receive) = std::sync::mpsc::channel();
    let thread = std::thread::spawn(move || {
        let c = Connection::open(checkpoint_path).unwrap();
        c.busy_timeout(Duration::from_millis(250)).unwrap();
        started.send(()).unwrap();
        checkpoint(&c, true).unwrap()
    });
    receive.recv().unwrap();
    let at = Instant::now();
    writer.execute("INSERT INTO t VALUES(2)", []).unwrap();
    assert!(at.elapsed() < Duration::from_millis(200));
    assert_eq!(thread.join().unwrap().0, 1);
    reader.execute_batch("ROLLBACK").unwrap();
    assert_eq!(checkpoint(&writer, true).unwrap().0, 0);
    drop(reader);
    drop(writer);
    std::fs::remove_file(path).unwrap();
}
#[test]
fn version_watch_ignores_own_commits_and_notices_other_connections() {
    let path = std::env::temp_dir().join(format!("hub-version-{}.db", std::process::id()));
    let a = Connection::open(&path).unwrap();
    a.execute_batch("PRAGMA journal_mode=WAL; CREATE TABLE t(x)")
        .unwrap();
    let b = Connection::open(&path).unwrap();
    let mut watch = DataVersion::new(&a).unwrap();
    a.execute("INSERT INTO t VALUES(1)", []).unwrap();
    assert!(!watch.changed(&a).unwrap());
    b.execute("INSERT INTO t VALUES(2)", []).unwrap();
    assert!(watch.changed(&a).unwrap());
    assert!(!watch.changed(&a).unwrap());
    drop(a);
    drop(b);
    std::fs::remove_file(path).unwrap();
}
#[test]
fn disk_pressure_preserves_durable_writes_and_recovers() {
    let disk = DiskPressure::default();
    for (wal, free) in [(0, 0), (1024 * 1024 * 1024 + 1, u64::MAX)] {
        disk.update(wal, free);
        assert_eq!(disk.admit(false), Err("disk-pressure"));
        assert_eq!(disk.admit(true), Ok(()));
    }
    disk.update(0, u64::MAX);
    assert_eq!(disk.admit(false), Ok(()));
}

#[test]
fn maintenance_checkpoints_both_files_and_schedules_writer_commands() {
    use brigade_hub::maintenance::{Maintenance, Sweep, WriterCommands};
    use std::sync::{Arc, Mutex};
    struct Commands(Mutex<Vec<Sweep>>);
    impl WriterCommands for Commands {
        fn schedule(&self, command: Sweep) -> bool {
            self.0.lock().unwrap().push(command);
            true
        }
    }
    let dir = std::env::temp_dir().join(format!("hub-maintenance-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let hub = dir.join("hub.db");
    assert!(std::process::Command::new("python3").env("PYTHONPATH",concat!(env!("CARGO_MANIFEST_DIR"),"/../../src"))
        .args(["-c","from brigade import fleet_hub; from pathlib import Path; import sys; fleet_hub.init_db(Path(sys.argv[1])).close()"]).arg(&hub).status().unwrap().success());
    let memory = dir.join("memory.db");
    let commands = Arc::new(Commands(Mutex::new(Vec::new())));
    let maintenance = Maintenance::start(&hub, &memory, commands.clone()).unwrap();
    let c = Connection::open(&memory).unwrap();
    c.execute_batch("PRAGMA journal_mode=WAL; PRAGMA wal_autocheckpoint=0; CREATE TABLE events(x); INSERT INTO events VALUES(1)").unwrap();
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        if {
            let m = maintenance.metrics.lock().unwrap();
            m.hub_checkpoints >= 1 && m.memory_checkpoints >= 1
        } && commands.0.lock().unwrap().len() >= 3
        {
            break;
        }
        assert!(Instant::now() < deadline, "maintenance did not run");
        std::thread::sleep(Duration::from_millis(20));
    }
    assert!(commands.0.lock().unwrap().contains(&Sweep::VectorCleanup));
    assert_eq!(maintenance.metrics.lock().unwrap().failures, 0);
    drop(maintenance);
    drop(c);
    std::fs::remove_dir_all(dir).unwrap();
}

#[test]
fn durable_admission_leaves_sqlite_full_to_the_writer() {
    let disk = DiskPressure::default();
    disk.update(0, 0);
    assert_eq!(disk.admit(true), Ok(()));
    let c = Connection::open_in_memory().unwrap();
    c.execute_batch("PRAGMA page_size=512; PRAGMA max_page_count=2; CREATE TABLE t(x)")
        .unwrap();
    let error = c
        .execute("INSERT INTO t VALUES(zeroblob(10000))", [])
        .unwrap_err();
    assert!(
        matches!(error,rusqlite::Error::SqliteFailure(e,_) if e.code==rusqlite::ErrorCode::DiskFull)
    );
    assert_eq!(
        c.query_row("SELECT count(*) FROM t", [], |r| r.get::<_, i64>(0))
            .unwrap(),
        0
    );
}

#[tokio::test]
async fn shadow_samples_use_one_dedicated_watch_connection() {
    use brigade_hub::maintenance::{Maintenance, NoSweeps};
    let dir = std::env::temp_dir().join(format!("hub-watch-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let hub = dir.join("hub.db");
    assert!(std::process::Command::new("python3").env("PYTHONPATH",concat!(env!("CARGO_MANIFEST_DIR"),"/../../src"))
        .args(["-c","from brigade import fleet_hub; from pathlib import Path; import sys; fleet_hub.init_db(Path(sys.argv[1])).close()"]).arg(&hub).status().unwrap().success());
    let maintenance =
        Maintenance::start(&hub, &dir.join("memory.db"), std::sync::Arc::new(NoSweeps)).unwrap();
    let before = maintenance.sample_data_version().await.unwrap();
    let writer = Connection::open(&hub).unwrap();
    writer.execute_batch("PRAGMA user_version=48").unwrap();
    let after = maintenance.sample_data_version().await.unwrap();
    assert_ne!(before, after);
    assert_eq!(after, maintenance.sample_data_version().await.unwrap());
    drop(writer);
    drop(maintenance);
    std::fs::remove_dir_all(dir).unwrap();
}
