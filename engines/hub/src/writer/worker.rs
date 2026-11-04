use super::*;
use rusqlite::{Connection, ErrorCode, OpenFlags};
use std::{
    panic::{AssertUnwindSafe, catch_unwind},
    path::Path,
};

pub(super) fn open(path: &Path) -> rusqlite::Result<Connection> {
    // Match Python: relocated parent directories are allowed, file symlinks are not.
    let parent = path
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .unwrap_or(Path::new("."));
    let parent = std::fs::canonicalize(parent)
        .map_err(|_| rusqlite::Error::InvalidPath(path.to_path_buf()))?;
    let filename = path
        .file_name()
        .ok_or_else(|| rusqlite::Error::InvalidPath(path.to_path_buf()))?;
    let resolved = parent.join(filename);
    let path = resolved.as_path();
    for suffix in ["", "-wal", "-shm", "-journal"] {
        let mut name = path.as_os_str().to_os_string();
        name.push(suffix);
        match std::fs::symlink_metadata(Path::new(&name)) {
            Ok(info) if !info.file_type().is_file() => {
                return Err(rusqlite::Error::InvalidPath(name.into()));
            }
            Err(error) if error.kind() != std::io::ErrorKind::NotFound => {
                return Err(rusqlite::Error::InvalidPath(name.into()));
            }
            _ => {}
        }
    }
    let flags = OpenFlags::SQLITE_OPEN_READ_WRITE
        | OpenFlags::SQLITE_OPEN_NO_MUTEX
        | OpenFlags::SQLITE_OPEN_NOFOLLOW;
    #[cfg(test)]
    let vfs = super::powercut::writer_vfs();
    #[cfg(not(test))]
    let vfs: Option<&str> = None;
    let db = match vfs {
        Some(vfs) => Connection::open_with_flags_and_vfs(path, flags, vfs)?,
        None => Connection::open_with_flags(path, flags)?,
    };
    db.busy_timeout(Duration::ZERO)?;
    db.execute_batch("PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA foreign_keys=ON; PRAGMA wal_autocheckpoint=0;")?;
    let level: i64 = db.query_row("PRAGMA synchronous", [], |r| r.get(0))?;
    if level != 2 {
        return Err(rusqlite::Error::InvalidQuery);
    }
    db.set_prepared_statement_cache_capacity(128);
    Ok(db)
}
pub(super) fn database_directory(db: &Connection) -> std::io::Result<PathBuf> {
    let path = db
        .path()
        .ok_or_else(|| std::io::Error::other("missing database path"))?;
    let resolved = std::fs::canonicalize(path)?;
    Ok(resolved
        .parent()
        .expect("database has a parent")
        .to_path_buf())
}
fn sync_directory(db: &Connection) -> std::io::Result<()> {
    let parent = database_directory(db)?;
    #[cfg(test)]
    if super::powercut::skip_directory_sync() {
        return Ok(());
    }
    std::fs::File::open(&parent)?.sync_all()?;
    #[cfg(test)]
    super::powercut::directory_synced(&parent)?;
    Ok(())
}
struct Tx<'a>(&'a Connection);
impl ReadTx for Tx<'_> {
    fn query(&self, sql: &str, params: &[Value]) -> rusqlite::Result<Vec<Vec<Value>>> {
        let mut stmt = self.0.prepare_cached(sql)?;
        let count = stmt.column_count();
        stmt.query_map(rusqlite::params_from_iter(params), |row| {
            (0..count).map(|i| row.get(i)).collect()
        })?
        .collect()
    }
}
impl WriteTx for Tx<'_> {
    fn execute(&self, sql: &str, params: &[Value]) -> rusqlite::Result<usize> {
        self.0
            .prepare_cached(sql)?
            .execute(rusqlite::params_from_iter(params))
    }
}
fn answer(mut e: Envelope, result: Answer) {
    if let Some(reply) = e.reply.take() {
        let _ = reply.send(result);
    }
}
fn begin(db: &Connection, deadline: Instant) -> rusqlite::Result<()> {
    let mut pause = Duration::from_millis(1);
    loop {
        match db.execute_batch("BEGIN IMMEDIATE") {
            Ok(()) => return Ok(()),
            Err(e)
                if e.sqlite_error_code() == Some(ErrorCode::DatabaseBusy)
                    && Instant::now() < deadline =>
            {
                std::thread::sleep(pause.min(deadline.saturating_duration_since(Instant::now())));
                pause = (pause * 2).min(Duration::from_millis(50));
            }
            Err(e) => return Err(e),
        }
    }
}
fn apply(db: &Connection, e: &Envelope) -> Answer {
    if Instant::now() >= e.budget.deadline {
        return Err(Failure::timeout(e.budget.durable));
    }
    if db.execute_batch("SAVEPOINT command").is_err() {
        return Err(Failure::storage("storage-error"));
    }
    let deadline = e.budget.deadline;
    let _ = db.progress_handler(1000, Some(move || Instant::now() >= deadline));
    let result = catch_unwind(AssertUnwindSafe(|| e.command.apply(&Tx(db))));
    let _ = db.progress_handler(0, None::<fn() -> bool>);
    if db.is_autocommit() {
        return Err(if Instant::now() >= deadline {
            Failure::timeout(e.budget.durable)
        } else {
            Failure::storage("storage-error")
        });
    }
    let result = match result {
        Ok(Ok(value)) if Instant::now() < deadline => Ok(value),
        Ok(Ok(_)) => Err(Failure::timeout(e.budget.durable)),
        Ok(Err(ApplyError::Sqlite(error)))
            if error.sqlite_error_code() == Some(ErrorCode::OperationInterrupted) =>
        {
            Err(Failure::timeout(e.budget.durable))
        }
        Ok(Err(ApplyError::Rejected(failure))) => Err(failure),
        _ => Err(Failure::storage("command-failed")),
    };
    let finish = if result.is_ok() {
        "RELEASE command"
    } else {
        "ROLLBACK TO command; RELEASE command"
    };
    if db.execute_batch(finish).is_err() {
        let _ = db.execute_batch("ROLLBACK");
        return Err(Failure::storage("storage-error"));
    }
    result
}

pub(super) fn run(
    mut db: Connection,
    mut receiver: mpsc::Receiver<Envelope>,
    config: Config,
    arbiter: Arc<Arbiter>,
) {
    let mut pending = None;
    let mut directory_synced = false;
    let mut last_version: Option<i64> = None;
    while let Some(first) = pending.take().or_else(|| receiver.blocking_recv()) {
        let lock_deadline = (Instant::now() + config.begin_wait.min(Duration::from_secs(5)))
            .min(first.budget.deadline);
        let Some(turn) = arbiter.acquire(Lane::Hub, lock_deadline) else {
            let durable = first.budget.durable;
            answer(first, Err(Failure::timeout(durable)));
            continue;
        };
        if let Err(error) = begin(&db, lock_deadline) {
            let failure = if error.sqlite_error_code() == Some(ErrorCode::DatabaseBusy) {
                Failure::timeout(first.budget.durable)
            } else {
                Failure::storage("storage-error")
            };
            answer(first, Err(failure));
            continue;
        }
        if !directory_synced {
            if sync_directory(&db).is_err() {
                let _ = db.execute_batch("ROLLBACK");
                answer(first, Err(Failure::storage("storage-error")));
                continue;
            }
            directory_synced = true;
        }
        let version: i64 = db
            .query_row("PRAGMA data_version", [], |r| r.get(0))
            .unwrap_or_default();
        let mut external_change = last_version.is_some_and(|old| old != version);
        last_version = Some(version);
        let started = Instant::now();
        let mut batch: Vec<(Envelope, Answer)> = Vec::new();
        let (mut events, mut bytes) = (0, 0);
        let mut next = Some(first);
        let mut replayed = false;
        let mut broken = false;
        while let Some(e) = next.take() {
            if !batch.is_empty()
                && (batch.len() >= 256
                    || events + e.budget.events > 4096
                    || bytes + e.budget.bytes > 8 * 1024 * 1024
                    || started.elapsed() >= Duration::from_millis(50))
            {
                pending = Some(e);
                break;
            }
            events += e.budget.events;
            bytes += e.budget.bytes;
            let result = apply(&db, &e);
            if db.is_autocommit() {
                answer(e, result);
                if replayed || begin(&db, lock_deadline).is_err() {
                    broken = true;
                    break;
                }
                replayed = true;
                if let Ok(version) = db.query_row("PRAGMA data_version", [], |r| r.get::<_, i64>(0))
                {
                    external_change |= last_version.is_some_and(|old| old != version);
                    last_version = Some(version);
                }
                for (earlier, result) in &mut batch {
                    if result.is_ok() {
                        *result = apply(&db, earlier);
                    }
                    if db.is_autocommit() {
                        broken = true;
                        break;
                    }
                }
                if broken {
                    break;
                }
            } else {
                batch.push((e, result));
            }
            next = receiver.try_recv().ok();
        }
        let committed = !broken && db.execute_batch("COMMIT").is_ok();
        if !committed {
            let _ = db.execute_batch("ROLLBACK");
            for (e, _) in batch {
                answer(e, Err(Failure::storage("commit-failed")));
            }
            drop(db);
            match open(&config.path).and_then(|db| {
                let check: String = db.query_row("PRAGMA quick_check", [], |r| r.get(0))?;
                if check != "ok" {
                    return Err(rusqlite::Error::InvalidQuery);
                }
                Ok(db)
            }) {
                Ok(reopened) => {
                    db = reopened;
                    directory_synced = false;
                    last_version = None;
                }
                Err(_) => {
                    receiver.close();
                    if let Some(e) = pending.take() {
                        answer(e, Err(Failure::storage("storage-error")));
                    }
                    while let Some(e) = receiver.blocking_recv() {
                        answer(e, Err(Failure::storage("storage-error")));
                    }
                    return;
                }
            }
        } else {
            drop(turn);
            for (mut e, result) in batch {
                let successful = result.is_ok();
                if let Some(reply) = e.reply.take() {
                    let _ = reply.send(result);
                }
                if successful {
                    let _ =
                        catch_unwind(AssertUnwindSafe(|| e.command.post_commit(external_change)));
                }
            }
        }
    }
    let _ = db.execute_batch("PRAGMA wal_checkpoint(PASSIVE)");
}

#[cfg(test)]
pub(super) fn test_apply(db: &Connection, e: &Envelope) -> Answer {
    apply(db, e)
}
