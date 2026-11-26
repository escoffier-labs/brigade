use std::fs::{self, FileTimes};
use std::io::{BufRead, BufReader};
use std::path::PathBuf;
use std::process::{Child, ChildStderr, Command, Stdio};
use std::time::{Duration, SystemTime};

use super::*;
use crate::store::lock::{SyncLock, lock_path};

#[link(name = "kernel32")]
unsafe extern "system" {
    fn GetHandleInformation(handle: RawHandle, flags: *mut u32) -> i32;
}

const HANDLE_FLAG_INHERIT: u32 = 1;
const HELPER_DB: &str = "CODE_GRAPH_TEST_LOCK_DB";
const HELPER_CONTENDER: &str = "CODE_GRAPH_TEST_LOCK_CONTENDER";

struct Helper(Child, BufReader<ChildStderr>);

impl Helper {
    fn start(db: Option<&Path>) -> Self {
        Self::spawn(db, false)
    }

    fn spawn(db: Option<&Path>, contender: bool) -> Self {
        let mut command = Command::new(std::env::current_exe().unwrap());
        command
            .args([
                "--exact",
                "store::lock::windows::tests::lock_helper",
                "--ignored",
                "--nocapture",
            ])
            .stdin(Stdio::piped())
            .stdout(Stdio::null())
            .stderr(Stdio::piped());
        if let Some(db) = db {
            command.env(HELPER_DB, db);
        } else {
            command.env_remove(HELPER_DB);
        }
        if contender {
            command.env(HELPER_CONTENDER, "1");
        } else {
            command.env_remove(HELPER_CONTENDER);
        }
        let mut child = command.spawn().unwrap();
        let stderr = BufReader::new(child.stderr.take().unwrap());
        let mut helper = Self(child, stderr);
        assert_eq!(helper.status(), "ready");
        helper
    }

    fn status(&mut self) -> String {
        loop {
            let mut line = String::new();
            assert_ne!(self.1.read_line(&mut line).unwrap(), 0, "helper exited");
            if !line.starts_with("reclaimed stale sync lock ") {
                return line.trim().to_owned();
            }
        }
    }

    fn terminate(&mut self) {
        // Child::kill calls TerminateProcess on Windows. Wait until the
        // process has exited and the OS has closed its file handles.
        self.0.kill().unwrap();
        assert!(!self.0.wait().unwrap().success());
    }
}

impl Drop for Helper {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

#[test]
#[ignore = "run in a separate process by the Windows lock tests"]
fn lock_helper() {
    let contender = std::env::var_os(HELPER_CONTENDER).is_some();
    if contender {
        eprintln!("ready");
        io::stdin().read_exact(&mut [0_u8]).unwrap();
    }
    let lock = std::env::var_os(HELPER_DB)
        .map(|db| SyncLock::acquire(&PathBuf::from(db)))
        .transpose();
    let _lock = match lock {
        Ok(lock) if contender => {
            eprintln!("acquired");
            lock
        }
        Err(err)
            if contender
                && err
                    .to_string()
                    .starts_with("another sync is already running") =>
        {
            eprintln!("busy");
            return;
        }
        result => {
            let lock = result.unwrap();
            eprintln!("ready");
            lock
        }
    };
    // Wait for the parent to kill us or close the pipe.
    let _ = io::stdin().read(&mut [0_u8]).unwrap();
}

#[test]
fn terminated_holder_releases_lock_and_next_sync_succeeds() {
    let dir = tempfile::tempdir().unwrap();
    let db = dir.path().join("g.db");
    fs::write(dir.path().join("example.py"), "def example(): pass\n").unwrap();
    let mut holder = Helper::start(Some(&db));
    assert!(lock_path(&db).exists());
    holder.terminate();
    assert!(!lock_path(&db).exists());

    let conn = crate::store::db::open_db(&db).unwrap();
    crate::store::schema::init_schema(&conn).unwrap();
    let summary = crate::store::sync::sync_repo(&conn, dir.path()).unwrap();
    assert_eq!(summary.files, 1);
    assert!(!lock_path(&db).exists());
}

#[test]
fn pending_delete_is_busy_until_reader_closes() {
    let dir = tempfile::tempdir().unwrap();
    let db = dir.path().join("g.db");
    let path = lock_path(&db);
    let mut holder = Helper::start(Some(&db));
    let reader = File::open(&path).unwrap();
    holder.terminate();

    let error = SyncLock::acquire(&db).unwrap_err().to_string();
    assert!(
        error.starts_with("another sync is already running"),
        "{error}"
    );
    drop(reader);
    drop(SyncLock::acquire(&db).unwrap());
    assert!(!path.exists());
}

#[test]
fn vanished_conflict_is_retried() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("g.db.lock");
    let mut attempts = 0;
    let lock = open_lock(&path, |path| {
        attempts += 1;
        if attempts == 1 {
            // The conflicting file disappeared before the legacy inspection.
            Err(io::Error::from_raw_os_error(5))
        } else {
            create(path)
        }
    })
    .unwrap();
    assert_eq!(attempts, 2);
    drop(lock);
}

#[test]
fn access_denied_without_lock_preserves_the_permission_error() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("g.db.lock");
    let error = open_lock(&path, |_| Err(io::Error::from_raw_os_error(5))).unwrap_err();
    assert!(error.to_string().starts_with("failed to create lock"));
    assert_eq!(
        error.downcast_ref::<io::Error>().unwrap().kind(),
        ErrorKind::PermissionDenied
    );
}

#[test]
fn live_holder_blocks_new_and_legacy_writers() {
    let dir = tempfile::tempdir().unwrap();
    let db = dir.path().join("g.db");
    let path = lock_path(&db);
    let holder = Helper::start(Some(&db));
    let pid = holder.0.id();

    let error = SyncLock::acquire(&db).unwrap_err().to_string();
    assert!(error.contains(&format!("PID {pid} is live")), "{error}");
    // Model the old engine's create/read/unlink path, including its age
    // fallback's attempt to unlink a file without knowing the PID is live.
    let old_create = OpenOptions::new().write(true).create_new(true).open(&path);
    assert!(old_create.is_err());
    assert_eq!(fs::read_to_string(&path).unwrap().trim(), pid.to_string());
    assert!(fs::remove_file(&path).is_err());
    assert!(path.exists());
}

#[test]
fn legacy_lock_with_exited_owner_is_reclaimed() {
    let dir = tempfile::tempdir().unwrap();
    let db = dir.path().join("g.db");
    let path = lock_path(&db);
    let mut old_owner = Helper::start(None);
    let pid = old_owner.0.id();
    fs::write(&path, format!("{pid}\n")).unwrap();
    old_owner.terminate();

    let lock = SyncLock::acquire(&db).unwrap();
    assert_eq!(
        fs::read_to_string(&path).unwrap().trim(),
        std::process::id().to_string()
    );
    drop(lock);
    assert!(!path.exists());
}

#[test]
fn old_legacy_lock_with_live_owner_is_preserved() {
    let dir = tempfile::tempdir().unwrap();
    let db = dir.path().join("g.db");
    let path = lock_path(&db);
    let owner = Helper::start(None);
    let pid = owner.0.id();
    fs::write(&path, format!("{pid}\n")).unwrap();
    age_lock(&path);

    let error = SyncLock::acquire(&db).unwrap_err().to_string();
    assert!(error.contains(&format!("PID {pid} is live")), "{error}");
    assert_eq!(fs::read_to_string(&path).unwrap().trim(), pid.to_string());
}

#[test]
fn legacy_lock_without_pid_uses_age_fallback() {
    let dir = tempfile::tempdir().unwrap();
    let db = dir.path().join("g.db");
    let path = lock_path(&db);
    fs::write(&path, "invalid\n").unwrap();
    assert!(SyncLock::acquire(&db).is_err());
    age_lock(&path);
    drop(SyncLock::acquire(&db).unwrap());
    assert!(!path.exists());
}

#[test]
fn oversized_legacy_lock_uses_age_fallback() {
    let dir = tempfile::tempdir().unwrap();
    let db = dir.path().join("g.db");
    let path = lock_path(&db);
    fs::write(&path, [b'x'; 80]).unwrap();
    assert!(
        SyncLock::acquire(&db)
            .unwrap_err()
            .to_string()
            .starts_with("another sync is already running")
    );
    age_lock(&path);
    drop(SyncLock::acquire(&db).unwrap());
    assert!(!path.exists());
}

fn age_lock(path: &Path) {
    OpenOptions::new()
        .write(true)
        .open(path)
        .unwrap()
        .set_times(
            FileTimes::new().set_modified(
                SystemTime::now() - Duration::from_secs(STALE_LOCK_MAX_AGE_SECS + 60),
            ),
        )
        .unwrap();
}

#[test]
fn racing_legacy_reclaimers_have_one_winner() {
    let dir = tempfile::tempdir().unwrap();
    let db = dir.path().join("g.db");
    let path = lock_path(&db);
    let mut old_owner = Helper::start(None);
    fs::write(&path, format!("{}\n", old_owner.0.id())).unwrap();
    old_owner.terminate();
    let mut first = Helper::spawn(Some(&db), true);
    let mut second = Helper::spawn(Some(&db), true);
    first.0.stdin.as_mut().unwrap().write_all(&[0]).unwrap();
    second.0.stdin.as_mut().unwrap().write_all(&[0]).unwrap();
    // The winner keeps its guard until both attempts have reported their result.
    let mut results = [first.status(), second.status()];
    results.sort();
    assert_eq!(results, ["acquired", "busy"]);
    assert!(path.exists());
    for mut contender in [first, second] {
        drop(contender.0.stdin.take());
        assert!(contender.0.wait().unwrap().success());
    }
    assert!(!path.exists());
}

#[test]
fn lock_handle_is_not_inherited_by_child() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("g.db.lock");
    let file = acquire(&path).unwrap();
    let mut flags = 0;
    // SAFETY: file is open and flags is a writable DWORD.
    assert_ne!(
        unsafe { GetHandleInformation(file.as_raw_handle(), &mut flags) },
        0
    );
    assert_eq!(flags & HANDLE_FLAG_INHERIT, 0);

    let mut child = Helper::start(None);
    drop(file);
    assert!(!path.exists());
    assert!(child.0.try_wait().unwrap().is_none());
}
