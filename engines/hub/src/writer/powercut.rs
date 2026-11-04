//! Test-only wrapper of SQLite's real VFS. Unsynced file images and directory
//! entries are restored after an injected I/O failure, with no process flush.
#![allow(unsafe_op_in_unsafe_fn)]
use rusqlite::ffi::*;
use std::{
    cell::RefCell,
    collections::BTreeMap,
    ffi::{CStr, c_char, c_int, c_void},
    path::PathBuf,
    ptr,
};
#[derive(Default)]
pub(crate) struct Disk {
    /// Memory power-cut runs (S13a): the NORMAL control, an SQLITE_FULL on
    /// the next commit frame, and a delay added to syncs of matching files.
    pub(crate) normal: bool,
    pub(crate) full_on_commit: bool,
    pub(crate) sync_delay: Option<(String, std::time::Duration, usize)>,
    pub(crate) writer: bool,
    pub(crate) skip_directory_sync: bool,
    pub(crate) stable: BTreeMap<PathBuf, Vec<u8>>,
    pub(crate) names: BTreeMap<PathBuf, bool>,
    pub(crate) paths: Vec<PathBuf>,
    pub(crate) count: usize,
    pub(crate) cut: usize,
    pub(crate) crashed: bool,
    pub(crate) ordered: bool,
    pub(crate) inside_sync: bool,
    pub(crate) phase: usize,
    pub(crate) active: Option<usize>,
    pub(crate) commit_frame: bool,
    pub(crate) commit_complete: bool,
    pub(crate) volatile: BTreeMap<PathBuf, Vec<u8>>,
    pub(crate) loss_mode: usize,
}
thread_local! { pub(crate) static DISK: RefCell<Disk> = RefCell::default(); }
// Registration is process-global, while fault state and writer switches are thread-local.
pub(crate) static VFS_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());
pub(crate) fn synchronous() -> (&'static str, i64) {
    if DISK.with(|d| d.borrow().normal) {
        ("NORMAL", 1)
    } else {
        ("FULL", 2)
    }
}
pub(super) fn writer_vfs() -> Option<&'static str> {
    DISK.with(|d| d.borrow().writer.then_some("hub-powercut"))
}
pub(super) fn skip_directory_sync() -> bool {
    DISK.with(|d| d.borrow().writer && d.borrow().skip_directory_sync)
}
pub(super) fn directory_synced(parent: &std::path::Path) -> std::io::Result<()> {
    if !DISK.with(|d| d.borrow().writer) {
        return Ok(());
    }
    if step(8) {
        return Err(std::io::Error::other("power cut before directory sync"));
    }
    DISK.with(|d| {
        let mut d = d.borrow_mut();
        for entry in std::fs::read_dir(parent).unwrap() {
            d.names.insert(entry.unwrap().path(), true);
        }
    });
    if step(8) {
        return Err(std::io::Error::other("power cut after directory sync"));
    }
    Ok(())
}
#[repr(C)]
struct File {
    base: sqlite3_file,
    real: *mut sqlite3_file,
    path: PathBuf,
}
unsafe fn f<'a>(p: *mut sqlite3_file) -> &'a mut File {
    &mut *p.cast::<File>()
}
unsafe fn io(p: *mut sqlite3_file) -> &'static sqlite3_io_methods {
    &*(*f(p).real).pMethods
}
pub(crate) fn step(phase: usize) -> bool {
    DISK.with(|d| {
        let mut d = d.borrow_mut();
        if d.crashed {
            return true;
        }
        if d.active.unwrap_or(phase) != d.phase {
            return false;
        }
        d.count += 1;
        if d.cut > 0 && d.count == d.cut {
            d.crashed = true;
            let paths = d.paths.clone();
            for path in paths {
                if let Ok(bytes) = std::fs::read(&path) {
                    d.volatile.insert(path, bytes);
                }
            }
        }
        d.crashed
    })
}
fn phase(default: usize) -> usize {
    DISK.with(|d| d.borrow().active.unwrap_or(default))
}
unsafe extern "C" fn open(
    v: *mut sqlite3_vfs,
    name: *const c_char,
    p: *mut sqlite3_file,
    flags: c_int,
    out: *mut c_int,
) -> c_int {
    let parent = (*v).pAppData.cast::<sqlite3_vfs>();
    let real = sqlite3_malloc((*parent).szOsFile).cast::<sqlite3_file>();
    if real.is_null() {
        return SQLITE_NOMEM;
    }
    ptr::write_bytes(real.cast::<u8>(), 0, (*parent).szOsFile as usize);
    let rc = ((*parent).xOpen.unwrap())(parent, name, real, flags, out);
    if rc != SQLITE_OK {
        sqlite3_free(real.cast());
        return rc;
    }
    let path = if name.is_null() {
        PathBuf::new()
    } else {
        PathBuf::from(CStr::from_ptr(name).to_str().unwrap())
    };
    DISK.with(|d| {
        let mut d = d.borrow_mut();
        if !d.paths.contains(&path) {
            d.paths.push(path.clone());
        }
    });
    ptr::write(
        p.cast::<File>(),
        File {
            base: sqlite3_file { pMethods: &METHODS },
            real,
            path,
        },
    );
    SQLITE_OK
}
unsafe extern "C" fn close(p: *mut sqlite3_file) -> c_int {
    let rc = (io(p).xClose.unwrap())(f(p).real);
    sqlite3_free(f(p).real.cast());
    ptr::drop_in_place(p.cast::<File>());
    rc
}
unsafe extern "C" fn read(p: *mut sqlite3_file, b: *mut c_void, n: c_int, o: i64) -> c_int {
    if phase(0) == 6 && step(6) {
        SQLITE_IOERR_READ
    } else {
        (io(p).xRead.unwrap())(f(p).real, b, n, o)
    }
}
unsafe extern "C" fn write(p: *mut sqlite3_file, b: *const c_void, n: c_int, o: i64) -> c_int {
    let wal = f(p).path.to_string_lossy().ends_with("-wal");
    if wal && n == 24 && o >= 32 {
        let header = std::slice::from_raw_parts(b.cast::<u8>(), 24);
        DISK.with(|d| {
            let mut d = d.borrow_mut();
            d.commit_frame = header[4..8] != [0, 0, 0, 0];
            d.commit_complete = false;
        });
    }
    let kind = if wal {
        if DISK.with(|d| d.borrow().commit_complete) {
            1
        } else {
            0
        }
    } else if std::fs::metadata(&f(p).path)
        .map(|m| o + n as i64 > m.len() as i64)
        .unwrap_or(true)
    {
        5
    } else {
        0
    };
    if step(kind) {
        return SQLITE_IOERR_WRITE;
    }
    if wal
        && n > 24
        && DISK.with(|d| {
            let mut d = d.borrow_mut();
            let full = d.commit_frame && d.full_on_commit;
            if full {
                d.full_on_commit = false;
            }
            full
        })
    {
        return SQLITE_FULL;
    }
    let rc = (io(p).xWrite.unwrap())(f(p).real, b, n, o);
    if wal && n > 24 && DISK.with(|d| d.borrow().commit_frame) {
        DISK.with(|d| d.borrow_mut().commit_complete = true);
    }
    let after = if wal && DISK.with(|d| d.borrow().commit_complete) {
        1
    } else {
        kind
    };
    if step(after) { SQLITE_IOERR_WRITE } else { rc }
}
unsafe extern "C" fn truncate(p: *mut sqlite3_file, n: i64) -> c_int {
    if step(1) {
        return SQLITE_IOERR_TRUNCATE;
    }
    let rc = (io(p).xTruncate.unwrap())(f(p).real, n);
    if step(4) { SQLITE_IOERR_TRUNCATE } else { rc }
}
unsafe extern "C" fn sync(p: *mut sqlite3_file, flags: c_int) -> c_int {
    if step(1) {
        return SQLITE_IOERR_FSYNC;
    }
    let path = f(p).path.clone();
    let delay = DISK.with(|d| {
        let mut d = d.borrow_mut();
        match d.sync_delay.as_mut() {
            Some((suffix, delay, remaining))
                if *remaining > 0 && path.to_string_lossy().ends_with(suffix.as_str()) =>
            {
                *remaining -= 1;
                Some(*delay)
            }
            _ => None,
        }
    });
    if let Some(delay) = delay {
        std::thread::sleep(delay);
    }
    let rc = (io(p).xSync.unwrap())(f(p).real, flags);
    // A separate cut inside sync persists a sector prefix of its dirty image.
    let partial_cut = DISK.with(|d| d.borrow().inside_sync) && step(2);
    DISK.with(|d| {
        let mut d = d.borrow_mut();
        if let Ok(bytes) = std::fs::read(&path) {
            if partial_cut {
                let old = d.stable.entry(path.clone()).or_default();
                let prefix = (bytes.len() / 1024) * 512;
                old.resize(old.len().max(prefix), 0);
                old[..prefix].copy_from_slice(&bytes[..prefix]);
            } else if rc == SQLITE_OK {
                d.stable.insert(path.clone(), bytes);
            }
        }
        if d.ordered {
            d.names.insert(path, true);
        }
    });
    if partial_cut || step(1) {
        SQLITE_IOERR_FSYNC
    } else {
        rc
    }
}
unsafe extern "C" fn size(p: *mut sqlite3_file, n: *mut i64) -> c_int {
    (io(p).xFileSize.unwrap())(f(p).real, n)
}
unsafe extern "C" fn lock(p: *mut sqlite3_file, n: c_int) -> c_int {
    if step(7) {
        return SQLITE_IOERR_LOCK;
    }
    let rc = (io(p).xLock.unwrap())(f(p).real, n);
    if step(7) { SQLITE_IOERR_LOCK } else { rc }
}
unsafe extern "C" fn unlock(p: *mut sqlite3_file, n: c_int) -> c_int {
    (io(p).xUnlock.unwrap())(f(p).real, n)
}
unsafe extern "C" fn reserved(p: *mut sqlite3_file, n: *mut c_int) -> c_int {
    (io(p).xCheckReservedLock.unwrap())(f(p).real, n)
}
unsafe extern "C" fn control(p: *mut sqlite3_file, n: c_int, a: *mut c_void) -> c_int {
    (io(p).xFileControl.unwrap())(f(p).real, n, a)
}
unsafe extern "C" fn sector(p: *mut sqlite3_file) -> c_int {
    (io(p).xSectorSize.unwrap())(f(p).real)
}
unsafe extern "C" fn device(_: *mut sqlite3_file) -> c_int {
    0
}
unsafe extern "C" fn map(
    p: *mut sqlite3_file,
    i: c_int,
    n: c_int,
    w: c_int,
    out: *mut *mut c_void,
) -> c_int {
    (io(p).xShmMap.unwrap())(f(p).real, i, n, w, out)
}
unsafe extern "C" fn shm_lock(p: *mut sqlite3_file, o: c_int, n: c_int, flags: c_int) -> c_int {
    (io(p).xShmLock.unwrap())(f(p).real, o, n, flags)
}
unsafe extern "C" fn barrier(p: *mut sqlite3_file) {
    (io(p).xShmBarrier.unwrap())(f(p).real)
}
unsafe extern "C" fn unmap(p: *mut sqlite3_file, d: c_int) -> c_int {
    (io(p).xShmUnmap.unwrap())(f(p).real, d)
}
unsafe extern "C" fn delete(v: *mut sqlite3_vfs, name: *const c_char, dir: c_int) -> c_int {
    if step(0) {
        return SQLITE_IOERR_DELETE;
    }
    let parent = (*v).pAppData.cast::<sqlite3_vfs>();
    let rc = ((*parent).xDelete.unwrap())(parent, name, dir);
    if dir != 0 {
        DISK.with(|d| {
            d.borrow_mut()
                .names
                .insert(PathBuf::from(CStr::from_ptr(name).to_str().unwrap()), false);
        });
    }
    if step(4) { SQLITE_IOERR_DELETE } else { rc }
}
static METHODS: sqlite3_io_methods = sqlite3_io_methods {
    iVersion: 2,
    xClose: Some(close),
    xRead: Some(read),
    xWrite: Some(write),
    xTruncate: Some(truncate),
    xSync: Some(sync),
    xFileSize: Some(size),
    xLock: Some(lock),
    xUnlock: Some(unlock),
    xCheckReservedLock: Some(reserved),
    xFileControl: Some(control),
    xSectorSize: Some(sector),
    xDeviceCharacteristics: Some(device),
    xShmMap: Some(map),
    xShmLock: Some(shm_lock),
    xShmBarrier: Some(barrier),
    xShmUnmap: Some(unmap),
    xFetch: None,
    xUnfetch: None,
};
pub(crate) struct Registration(*mut sqlite3_vfs);
impl Registration {
    pub(crate) unsafe fn new() -> Self {
        let parent = sqlite3_vfs_find(ptr::null());
        let mut v = Box::new(ptr::read(parent));
        v.zName = c"hub-powercut".as_ptr();
        v.szOsFile = std::mem::size_of::<File>() as c_int;
        v.pAppData = parent.cast();
        v.xOpen = Some(open);
        v.xDelete = Some(delete);
        let v = Box::into_raw(v);
        assert_eq!(sqlite3_vfs_register(v, 0), SQLITE_OK);
        Self(v)
    }
}
impl Drop for Registration {
    fn drop(&mut self) {
        unsafe {
            sqlite3_vfs_unregister(self.0);
            drop(Box::from_raw(self.0));
        }
    }
}

fn connect(path: &std::path::Path) -> rusqlite::Result<rusqlite::Connection> {
    rusqlite::Connection::open_with_flags_and_vfs(
        path,
        rusqlite::OpenFlags::SQLITE_OPEN_READ_WRITE,
        "hub-powercut",
    )
}
fn seed(path: &std::path::Path, ordered: bool) {
    seed_files(&[path], ordered);
}
/// Treat the current bytes of each file and its WAL as durable.
pub(crate) fn seed_files(files: &[&std::path::Path], ordered: bool) {
    let mut disk = Disk {
        ordered,
        ..Disk::default()
    };
    for path in files {
        for suffix in ["", "-wal"] {
            let p = PathBuf::from(format!("{}{suffix}", path.display()));
            if let Ok(bytes) = std::fs::read(&p) {
                disk.stable.insert(p.clone(), bytes);
                disk.names.insert(p.clone(), true);
            }
            disk.paths.push(p);
        }
    }
    DISK.with(|d| *d.borrow_mut() = disk);
}
pub(crate) fn restore() {
    DISK.with(|d| {
        let mut d = d.borrow_mut();
        d.cut = 0;
        d.crashed = false;
        for p in &d.paths {
            if d.names.get(p) == Some(&true) {
                if let Some(bytes) = d.stable.get(p) {
                    let mut image = bytes.clone();
                    if let Some(volatile) = d.volatile.get(p) {
                        if d.loss_mode == 1 {
                            let end = (volatile.len() / 1024) * 512;
                            image.resize(image.len().max(end), 0);
                            image[..end].copy_from_slice(&volatile[..end]);
                        } else if d.loss_mode == 2 {
                            for (i, sector) in volatile.chunks(512).enumerate().rev() {
                                if i % 2 == 0 {
                                    let start = i * 512;
                                    image.resize(image.len().max(start + sector.len()), 0);
                                    image[start..start + sector.len()].copy_from_slice(sector);
                                }
                            }
                        }
                    }
                    std::fs::write(p, image).unwrap();
                }
            } else {
                let _ = std::fs::remove_file(p);
            }
            let _ = std::fs::remove_file(format!("{}-shm", p.display()));
        }
    });
}

#[test]
#[ignore = "storage fault matrix, run alone with --ignored --exact writer::powercut::power_cut_matrix"]
fn power_cut_matrix() {
    // A failed test elsewhere must not poison every later power-cut run.
    let _lock = VFS_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let _vfs = unsafe { Registration::new() };
    let mut normal_losses = 0;
    let mut sync_losses = [0usize; 2];
    let template = super::tests::fixture();
    let status=std::process::Command::new("python3").args(["-c", "from pathlib import Path; import sys; from brigade.fleet_hub import init_db; init_db(Path(sys.argv[1])).close()"])
        .arg(&template).status().unwrap();
    assert!(status.success());
    for ordered in [false, true] {
        for full in [true, false] {
            for phase in 0..7 {
                eprintln!("powercut: ordered={ordered} FULL={full} phase={phase}");
                for cut in 1..=100 {
                    let path = super::tests::fixture();
                    std::fs::copy(&template, &path).unwrap();
                    let anchor = rusqlite::Connection::open(&path).unwrap();
                    anchor.execute_batch("PRAGMA wal_autocheckpoint=0; INSERT INTO rows(value) VALUES('baseline')").unwrap();
                    seed(&path, ordered);
                    let db = connect(&path).unwrap();
                    db.execute_batch(if full {
                        "PRAGMA synchronous=FULL; PRAGMA wal_autocheckpoint=0"
                    } else {
                        "PRAGMA synchronous=NORMAL; PRAGMA wal_autocheckpoint=0"
                    })
                    .unwrap();
                    let mut acknowledged = vec!["baseline".to_string()];
                    // The external ledger is outside the VFS-managed files.
                    let ledger = path.with_extension("ledger");
                    std::fs::write(&ledger, "baseline\n").unwrap();
                    DISK.with(|d| {
                        let mut d = d.borrow_mut();
                        d.cut = cut;
                        d.loss_mode = cut % 3;
                        d.count = 0;
                        d.inside_sync = phase == 2;
                        d.phase = phase;
                        if phase == 6 {
                            d.cut = 0;
                        }
                    });
                    let trials = if !full && phase == 2 { 3 } else { 120 };
                    let mut identities = Vec::new();
                    for i in 0..trials {
                        let value = format!("command-{i}");
                        let sql = "INSERT INTO rows(value) VALUES(?1)";
                        let body_size = if phase == 0 || phase == 5 || phase == 6 {
                            8192
                        } else {
                            4
                        };
                        let applied = (|| -> rusqlite::Result<(i64, i64)> {
                            db.execute_batch("BEGIN IMMEDIATE")?;
                            db.execute(sql, [&value])?;
                            db.execute("INSERT INTO fleet_messages(sender,recipient,subject,body,priority,client_id,payload_hash,created_at,received_at) VALUES('test-sender','test-recipient','test',zeroblob(?2),'normal',?1,'fake-hash','2000-01-01','2000-01-01')",rusqlite::params![&value,body_size])?;
                            let id = db.last_insert_rowid();
                            db.execute("INSERT INTO claim_fences(target,generation) VALUES('powercut-test',1) ON CONFLICT(target) DO UPDATE SET generation=generation+1",[])?;
                            let generation = db.query_row(
                                "SELECT generation FROM claim_fences WHERE target='powercut-test'",
                                [],
                                |r| r.get(0),
                            )?;
                            db.execute_batch("COMMIT")?;
                            Ok((id, generation))
                        })();
                        let Ok(identity) = applied else {
                            break;
                        };
                        identities.push((value.clone(), identity));
                        let stored: String = db
                            .query_row("SELECT value FROM rows ORDER BY id DESC LIMIT 1", [], |r| {
                                r.get(0)
                            })
                            .unwrap();
                        acknowledged.push(stored);
                        std::fs::write(
                            &ledger,
                            serde_json::to_vec(&(&acknowledged, &identities)).unwrap(),
                        )
                        .unwrap();
                        if phase == 3 || phase == 5 {
                            DISK.with(|d| {
                                d.borrow_mut().active = if phase == 3 { Some(3) } else { None }
                            });
                            let _ = db.execute_batch("PRAGMA wal_checkpoint(PASSIVE)");
                            DISK.with(|d| d.borrow_mut().active = None);
                        }
                        if phase == 4 {
                            DISK.with(|d| d.borrow_mut().active = Some(4));
                            let _ = db.execute_batch("PRAGMA wal_checkpoint(TRUNCATE)");
                            DISK.with(|d| d.borrow_mut().active = None);
                        }
                        if DISK.with(|d| d.borrow().crashed) {
                            break;
                        }
                    }
                    if full && phase != 6 {
                        assert!(
                            DISK.with(|d| d.borrow().crashed),
                            "no phase cut phase={phase}, cut={cut}"
                        );
                    }
                    // Closing cannot persist more bytes after the simulated power loss.
                    DISK.with(|d| d.borrow_mut().crashed = true);
                    drop(db);
                    drop(anchor);
                    restore();
                    if phase == 6 {
                        DISK.with(|d| {
                            let mut d = d.borrow_mut();
                            d.count = 0;
                            d.cut = cut;
                            d.active = Some(6);
                        });
                        if let Ok(db) = connect(&path) {
                            let _ = db
                                .query_row("SELECT count(*) FROM rows", [], |r| r.get::<_, i64>(0));
                            drop(db);
                        }
                        if full {
                            assert!(DISK.with(|d| d.borrow().crashed), "no recovery cut {cut}");
                        }
                        DISK.with(|d| d.borrow_mut().crashed = true);
                        restore();
                    }
                    DISK.with(|d| d.borrow_mut().active = None);
                    let recovered = connect(&path).unwrap();
                    assert_eq!(
                        recovered
                            .query_row("PRAGMA integrity_check", [], |r| r.get::<_, String>(0))
                            .unwrap(),
                        "ok"
                    );
                    for value in acknowledged {
                        let present: bool = recovered
                            .query_row(
                                "SELECT EXISTS(SELECT 1 FROM rows WHERE value=?1)",
                                [value],
                                |r| r.get(0),
                            )
                            .unwrap();
                        if full {
                            assert!(
                                present,
                                "FULL loss: ordered={ordered}, phase={phase}, cut={cut}"
                            );
                        } else if !present {
                            normal_losses += 1;
                            if phase == 1 || phase == 2 {
                                sync_losses[phase - 1] += 1;
                            }
                        }
                    }
                    for (client, (id, generation)) in identities {
                        let stored = recovered.query_row(
                            "SELECT id FROM fleet_messages WHERE client_id=?1",
                            [&client],
                            |r| r.get::<_, i64>(0),
                        );
                        if full {
                            assert_eq!(stored.unwrap(), id);
                            assert!(recovered.query_row("SELECT generation FROM claim_fences WHERE target='powercut-test'",[],|r|r.get::<_,i64>(0)).unwrap()>=generation);
                        }
                    }
                    // Multi-table unacknowledged commands are all present or all absent.
                    let partial:i64=recovered.query_row("SELECT count(*) FROM fleet_messages m WHERE NOT EXISTS(SELECT 1 FROM rows r WHERE r.value=m.client_id)",[],|r|r.get(0)).unwrap();
                    assert_eq!(partial, 0);
                    let orphan:i64=recovered.query_row("SELECT count(*) FROM rows r WHERE value!='baseline' AND NOT EXISTS(SELECT 1 FROM fleet_messages m WHERE m.client_id=r.value)",[],|r|r.get(0)).unwrap();
                    assert_eq!(orphan, 0);
                    let count: i64 = recovered
                        .query_row("SELECT count(*) FROM fleet_messages", [], |r| r.get(0))
                        .unwrap();
                    let generation:i64=recovered.query_row("SELECT COALESCE((SELECT generation FROM claim_fences WHERE target='powercut-test'),0)",[],|r|r.get(0)).unwrap();
                    assert_eq!(count, generation);
                    let previous: i64 = recovered
                        .query_row("SELECT COALESCE(max(id),0) FROM fleet_messages", [], |r| {
                            r.get(0)
                        })
                        .unwrap();
                    recovered.execute("INSERT INTO fleet_messages(sender,recipient,subject,body,priority,client_id,payload_hash,created_at,received_at) VALUES('test-sender','test-recipient','test','test','normal','after-cut','fake-hash','2000-01-01','2000-01-01')",[]).unwrap();
                    assert!(recovered.last_insert_rowid() > previous);
                    drop(recovered);
                    std::fs::remove_dir_all(path.parent().unwrap()).unwrap();
                }
            }
        }
    }
    assert!(
        normal_losses > 0,
        "NORMAL control must detect acknowledged loss"
    );
    assert!(
        sync_losses.iter().all(|n| *n > 0),
        "NORMAL must lose acknowledged writes in both sync phases: {sync_losses:?}"
    );
    eprintln!("NORMAL acknowledged losses: {normal_losses}, sync phases: {sync_losses:?}");
    std::fs::remove_dir_all(template.parent().unwrap()).unwrap();
}

// Start with only the checkpointed database durable. No anchor may pre-create a WAL.
// Run synchronously so the real opener, worker and VFS share the fault state.
fn writer_creation_cut(cut: usize, skip: bool) -> (usize, bool, bool) {
    use super::*;
    struct Insert;
    impl Command for Insert {
        fn apply(&self, tx: &dyn WriteTx) -> Result<Json, ApplyError> {
            tx.execute("INSERT INTO rows(value) VALUES('acknowledged')", &[])?;
            Ok(Json::Null)
        }
        fn post_commit(&self, _: bool) {
            step(8); // Cut immediately after the real reply was sent.
            // Power loss also stops the shutdown checkpoint and connection-close writes.
            DISK.with(|d| d.borrow_mut().crashed = true);
        }
    }
    let path = super::tests::fixture();
    let wal = PathBuf::from(format!("{}-wal", path.display()));
    assert!(!wal.exists(), "schedule must start without a WAL");
    seed(&path, false);
    DISK.with(|d| {
        let mut d = d.borrow_mut();
        assert!(!d.names.contains_key(&wal));
        d.writer = true;
        d.skip_directory_sync = skip;
        d.cut = cut;
        d.active = Some(8);
        d.phase = 8;
    });
    let arbiter = std::sync::Arc::new(Arbiter::default());
    let (sender, receiver) = mpsc::channel(1);
    let (reply, result) = oneshot::channel();
    sender
        .try_send(Envelope {
            command: Box::new(Insert),
            budget: Budget {
                events: 1,
                bytes: 100,
                durable: false,
                deadline: Instant::now() + Duration::from_secs(10),
            },
            reply: Some(reply),
            _permits: vec![],
            arbiter: arbiter.clone(),
        })
        .unwrap_or_else(|_| panic!("queue"));
    drop(sender);
    match worker::open(&path) {
        Ok(db) => worker::run(
            db,
            receiver,
            Config::new(path.clone()),
            arbiter,
            Arc::default(),
        ),
        Err(_) => drop(receiver),
    }
    let acknowledged = matches!(result.blocking_recv(), Ok(Ok(_)));
    // This ledger is outside VFS-managed storage, like the long matrix's ledger.
    let ledger = path.with_extension("ledger");
    std::fs::write(&ledger, if acknowledged { "acknowledged" } else { "" }).unwrap();
    let count = DISK.with(|d| {
        let mut d = d.borrow_mut();
        d.crashed = true;
        d.writer = false;
        d.count
    });
    restore();
    DISK.with(|d| d.borrow_mut().active = None);
    let db = rusqlite::Connection::open(&path).unwrap();
    assert_eq!(
        db.query_row("PRAGMA integrity_check", [], |r| r.get::<_, String>(0))
            .unwrap(),
        "ok"
    );
    let present: bool = db
        .query_row(
            "SELECT EXISTS(SELECT 1 FROM rows WHERE value='acknowledged')",
            [],
            |r| r.get(0),
        )
        .unwrap();
    let lost = !std::fs::read_to_string(ledger).unwrap().is_empty() && !present;
    drop(db);
    DISK.with(|d| *d.borrow_mut() = Disk::default());
    std::fs::remove_dir_all(path.parent().unwrap()).unwrap();
    (count, acknowledged, lost)
}

fn writer_creation_schedule(long: bool) {
    // A failed test elsewhere must not poison every later power-cut run.
    let _lock = VFS_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let _vfs = unsafe { Registration::new() };
    for skip in [false, true] {
        let (operations, ack, lost) = writer_creation_cut(0, skip);
        assert!(ack);
        assert_eq!(lost, skip, "directory sync negative control");
        let cuts: Vec<_> = if long {
            (1..=operations + 1).collect()
        } else {
            vec![
                1,
                operations / 2,
                operations - 1,
                operations,
                operations + 1,
            ]
        };
        let mut losses = usize::from(lost);
        let mut acknowledgements = usize::from(ack);
        for cut in cuts {
            let (_, ack, lost) = writer_creation_cut(cut, skip);
            acknowledgements += usize::from(ack);
            losses += usize::from(lost);
            if !skip {
                assert!(!lost, "writer acknowledged loss at cut {cut}");
            }
        }
        eprintln!(
            "writer WAL creation: sync_disabled={skip}, acknowledgements={acknowledgements}, losses={losses}"
        );
        if skip {
            assert!(losses > 0, "sync-disabled control must detect loss");
        }
    }
}

#[test]
fn writer_wal_creation_short() {
    writer_creation_schedule(false);
}

#[test]
#[ignore = "all writer WAL creation operation cuts, run manually"]
fn writer_wal_creation_matrix() {
    writer_creation_schedule(true);
}
