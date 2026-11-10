//! `brigade-hub memory-restore-epoch` (#2292): a restored copy gets a new
//! `log_epoch` before the hub serves it, and the command refuses a held file,
//! a schema it does not know and stale sidecars without changing anything.
use super::restore::{self, rotate_restored};
use super::tests::{ADMIN_TOKEN, Hub, NODE_TOKEN, code, rev};
use super::*;
use rusqlite::Connection;
use serde_json::json;
use std::path::{Path, PathBuf};

fn epoch_of(path: &Path) -> String {
    schema::log_epoch(&Connection::open(path).unwrap()).unwrap()
}

/// Stop the hub, then put `copy` back in place of its memory file the way the
/// runbook does: the old file's sidecars go first.
fn restore_copy(h: &mut Hub, copy: &Path) {
    h.store.take();
    for suffix in ["", "-wal", "-shm", "-journal"] {
        let _ = std::fs::remove_file(format!("{}{suffix}", h.memory().display()));
    }
    std::fs::copy(copy, h.memory()).unwrap();
}

fn take_copy(h: &Hub, name: &str) -> PathBuf {
    let copy = h.dir.join(name);
    Connection::open(h.memory())
        .unwrap()
        .execute("VACUUM INTO ?1", [copy.to_str().unwrap()])
        .unwrap();
    copy
}

#[tokio::test]
async fn restored_copy_gets_a_new_epoch_and_a_pre_restore_write_is_refused() {
    let mut h = Hub::new();
    let r1 = rev(&h.put(NODE_TOKEN, "fleet", "restored", None, "one").await);
    let copy = take_copy(&h, "copy.db");
    let old_epoch = h.epoch().await;
    let r2 = rev(&h
        .put(NODE_TOKEN, "fleet", "restored", Some(r1), "two")
        .await);
    restore_copy(&mut h, &copy);
    assert_eq!(
        epoch_of(&h.memory()),
        old_epoch,
        "the copy carries the old epoch"
    );
    let memory = h.memory();
    let args = [
        "memory-restore-epoch",
        "--memory-db",
        memory.to_str().unwrap(),
    ];
    assert_eq!(restore::main(args.map(Into::into)), 0);
    let rotated = epoch_of(&h.memory());
    assert_ne!(rotated, old_epoch);
    h.open(Arc::new(scan::Unavailable), None);
    assert_eq!(h.epoch().await, rotated);
    // The restored log hands out r2 again, for a different edit.
    let reissued = h
        .put(
            NODE_TOKEN,
            "fleet",
            "restored",
            Some(r1),
            "two after restore",
        )
        .await;
    assert_eq!(rev(&reissued), r2, "{reissued:?}");
    let before = h.events();
    let stale = h
        .call(
            NODE_TOKEN,
            json!({"action":"put","scope":"fleet","card_id":"restored","base_revision":r2,
                   "base_epoch":old_epoch,"text":"edit of the lost r2","client_event_id":h.id()}),
        )
        .await;
    assert_eq!((stale.status, code(&stale)), (409, "memory_epoch_changed"));
    assert_eq!(stale.body["log_epoch"], rotated);
    assert_eq!(h.events(), before);
}

#[tokio::test]
async fn refuses_while_the_hub_or_a_reader_holds_the_file() {
    let mut h = Hub::new();
    h.put(NODE_TOKEN, "fleet", "held", None, "one").await;
    let epoch = h.epoch().await;
    // The hub's store is open. Its WAL is checked first, because opening the
    // file would replay a stale one, so either refusal may answer.
    let held = rotate_restored(&h.memory(), h.now()).unwrap_err();
    assert!(
        held.contains("held by another process") || held.contains("-wal sits beside"),
        "{held}"
    );
    assert_eq!(epoch_of(&h.memory()), epoch);
    // The copy is back in place with the hub stopped, and only an idle WAL
    // connection, as every hub connection is, holds it. Its WAL has no
    // frames, so the lock is what refuses.
    let copy = take_copy(&h, "copy.db");
    restore_copy(&mut h, &copy);
    let reader = Connection::open(h.memory()).unwrap();
    reader.pragma_update(None, "journal_mode", "WAL").unwrap();
    assert_eq!(schema::log_epoch(&reader).unwrap(), epoch);
    let wal = std::fs::metadata(format!("{}-wal", h.memory().display())).unwrap();
    assert_eq!(wal.len(), 0);
    let held = rotate_restored(&h.memory(), h.now()).unwrap_err();
    assert!(held.contains("held by another process"), "{held}");
    assert_eq!(schema::log_epoch(&reader).unwrap(), epoch);
    drop(reader);
    // Released: the rotation runs.
    let rotated = rotate_restored(&h.memory(), h.now()).unwrap();
    assert_eq!(rotated.previous, epoch);
    assert_ne!(rotated.epoch, epoch);
    // A hub opening the file waits for nothing: the command released it.
    h.open(Arc::new(scan::Unavailable), None);
    assert_eq!(h.epoch().await, rotated.epoch);
}

#[tokio::test]
async fn refuses_a_schema_it_does_not_know_and_changes_nothing() {
    let mut h = Hub::new();
    h.put(NODE_TOKEN, "fleet", "schema", None, "one").await;
    h.store.take();
    let epoch = epoch_of(&h.memory());
    let refuse = |path: &Path, expected: &str| {
        let error = rotate_restored(path, h.now()).unwrap_err();
        assert!(error.contains(expected), "{error}");
    };
    // A copy from a newer hub.
    let newer = take_copy(&h, "newer.db");
    Connection::open(&newer)
        .unwrap()
        .pragma_update(None, "user_version", schema::USER_VERSION + 1)
        .unwrap();
    refuse(&newer, "newer than this hub");
    assert_eq!(epoch_of(&newer), epoch);
    // Another application's file, and a database with no memory schema.
    let foreign = take_copy(&h, "foreign.db");
    Connection::open(&foreign)
        .unwrap()
        .pragma_update(None, "application_id", 7)
        .unwrap();
    refuse(&foreign, "foreign application_id");
    assert_eq!(epoch_of(&foreign), epoch);
    let empty = h.dir.join("empty.db");
    Connection::open(&empty)
        .unwrap()
        .execute_batch("CREATE TABLE t(x)")
        .unwrap();
    refuse(&empty, "no memory schema");
    // A memory schema with no log_epoch row.
    let headless = take_copy(&h, "headless.db");
    Connection::open(&headless)
        .unwrap()
        .execute("DELETE FROM memory_meta WHERE key='log_epoch'", [])
        .unwrap();
    refuse(&headless, "no log_epoch row");
    // A mistyped path is refused and never created.
    let missing = h.dir.join("missing.db");
    refuse(&missing, "missing.db");
    assert!(!missing.exists());
    // The live file was never touched by any of these.
    assert_eq!(epoch_of(&h.memory()), epoch);
    let args = [
        "memory-restore-epoch",
        "--memory-db",
        newer.to_str().unwrap(),
    ];
    assert_eq!(restore::main(args.map(Into::into)), 1);
}

#[tokio::test]
async fn refuses_stale_sidecars_beside_the_copy() {
    let mut h = Hub::new();
    h.put(NODE_TOKEN, "fleet", "sidecar", None, "one").await;
    h.store.take();
    let epoch = epoch_of(&h.memory());
    let wal = PathBuf::from(format!("{}-wal", h.memory().display()));
    std::fs::write(&wal, b"frames of the replaced file").unwrap();
    let error = rotate_restored(&h.memory(), h.now()).unwrap_err();
    assert!(
        error.contains("-wal sits beside the memory file"),
        "{error}"
    );
    // An empty WAL is what a clean close can leave, and is not refused.
    std::fs::write(&wal, b"").unwrap();
    let journal = PathBuf::from(format!("{}-journal", h.memory().display()));
    std::fs::write(&journal, b"").unwrap();
    let error = rotate_restored(&h.memory(), h.now()).unwrap_err();
    assert!(
        error.contains("-journal sits beside the memory file"),
        "{error}"
    );
    std::fs::remove_file(&journal).unwrap();
    assert_eq!(epoch_of(&h.memory()), epoch);
    assert!(rotate_restored(&h.memory(), h.now()).is_ok());
}

#[tokio::test]
async fn a_rerun_mints_another_unseen_epoch_and_changes_nothing_else() {
    let mut h = Hub::new();
    let r1 = rev(&h.put(NODE_TOKEN, "fleet", "again", None, "one").await);
    h.put(NODE_TOKEN, "fleet", "again", Some(r1), "two").await;
    let status = h.call(ADMIN_TOKEN, json!({"action":"status"})).await;
    h.store.take();
    let rows = |path: &Path| -> Vec<String> {
        let c = Connection::open(path).unwrap();
        let mut out = Vec::new();
        for table in schema::TABLES {
            let sql = format!("SELECT count(*) FROM {table}");
            let n: i64 = c.query_row(&sql, [], |r| r.get(0)).unwrap();
            out.push(format!("{table}={n}"));
        }
        let mut meta = c
            .prepare("SELECT key, value FROM memory_meta WHERE key<>'log_epoch' ORDER BY key")
            .unwrap();
        out.extend(
            meta.query_map([], |r| {
                Ok(format!(
                    "{}={}",
                    r.get::<_, String>(0)?,
                    r.get::<_, String>(1)?
                ))
            })
            .unwrap()
            .map(Result::unwrap),
        );
        out
    };
    let before = rows(&h.memory());
    let first = rotate_restored(&h.memory(), h.now()).unwrap();
    let second = rotate_restored(&h.memory(), h.now()).unwrap();
    assert_eq!(first.previous, status.body["log_epoch"].as_str().unwrap());
    assert_eq!(second.previous, first.epoch);
    assert_ne!(second.epoch, first.epoch);
    assert_eq!(rows(&h.memory()), before);
    assert_eq!(first.user_version, schema::USER_VERSION);
}

/// SQLite follows a symlink and replays the WAL beside the real file, so a
/// link must not let a stale WAL there pass the sidecar check.
#[cfg(unix)]
#[tokio::test]
async fn refuses_a_symlinked_path_and_checks_sidecars_beside_the_real_file() {
    let mut h = Hub::new();
    h.put(NODE_TOKEN, "fleet", "linked", None, "one").await;
    let copy = take_copy(&h, "copy.db");
    restore_copy(&mut h, &copy);
    let real = h.memory();
    let wal = PathBuf::from(format!("{}-wal", real.display()));
    std::fs::write(&wal, b"frames of the replaced file").unwrap();
    let (file_before, wal_before) = (std::fs::read(&real).unwrap(), std::fs::read(&wal).unwrap());
    // A link to the file, whose own `-wal` name holds nothing.
    let link = h.dir.join("linked-memory.db");
    std::os::unix::fs::symlink(&real, &link).unwrap();
    let error = rotate_restored(&link, h.now()).unwrap_err();
    assert!(error.contains("is a symbolic link"), "{error}");
    let args = [
        "memory-restore-epoch",
        "--memory-db",
        link.to_str().unwrap(),
    ];
    assert_eq!(restore::main(args.map(Into::into)), 1);
    // A symlinked parent directory resolves to the real file's sidecars.
    let parent = h.dir.join("linked-dir");
    std::os::unix::fs::symlink(&h.dir, &parent).unwrap();
    let through = parent.join("fleet-hub-memory.db");
    let error = rotate_restored(&through, h.now()).unwrap_err();
    assert!(
        error.contains("-wal sits beside the memory file"),
        "{error}"
    );
    let canonical = std::fs::canonicalize(&real).unwrap();
    assert!(error.contains(&canonical.display().to_string()), "{error}");
    assert_eq!(std::fs::read(&real).unwrap(), file_before);
    assert_eq!(std::fs::read(&wal).unwrap(), wal_before);
}
