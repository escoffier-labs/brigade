//! Checkpoints use independent connections. Retention always goes through a writer.
use crate::schema::{DataVersion, SchemaGate};
use rusqlite::{Connection, OpenFlags};
use std::{
    path::{Path, PathBuf},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
        mpsc,
    },
    thread::JoinHandle,
    time::{Duration, Instant},
};
const MIB: u64 = 1024 * 1024;
const GIB: u64 = 1024 * MIB;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Sweep {
    HubRetention,
    MemoryRetention,
    VectorCleanup,
}
/// S4a implements this with try_send to its bounded command queue. False means
/// retry next tick. Each command runs bounded work inside the owning writer.
pub trait WriterCommands: Send + Sync + 'static {
    fn schedule(&self, command: Sweep) -> bool;
}
pub struct NoSweeps;
impl WriterCommands for NoSweeps {
    fn schedule(&self, _: Sweep) -> bool {
        false
    }
}
#[derive(Default, Clone, Debug)]
pub struct Metrics {
    pub checkpoints: u64,
    pub hub_checkpoints: u64,
    pub memory_checkpoints: u64,
    pub log_frames: i64,
    pub checkpointed_frames: i64,
    pub busy: u64,
    pub failures: u64,
    pub wal_bytes: u64,
    pub free_bytes: Option<u64>,
    pub sweeps_rejected: u64,
}
#[derive(Default)]
pub struct DiskPressure(AtomicBool);
impl DiskPressure {
    pub fn update(&self, wal_bytes: u64, free_bytes: Option<u64>) {
        self.0.store(
            wal_bytes > GIB || free_bytes.is_some_and(|free| free < GIB),
            Ordering::Release,
        );
    }
    pub fn admit(&self, durable: bool) -> Result<(), &'static str> {
        if !durable && self.0.load(Ordering::Acquire) {
            Err("disk-pressure")
        } else {
            Ok(())
        }
    }
}
pub fn checkpoint(c: &Connection, truncate: bool) -> rusqlite::Result<(i64, i64, i64)> {
    // SQLite holds WAL_WRITE_LOCK while a TRUNCATE busy handler waits for
    // readers. Return busy immediately and retry on a later tick so a pinned
    // reader cannot stall the writer. Zero is within the 250 ms busy ceiling.
    if truncate {
        c.busy_timeout(Duration::ZERO)?;
    }
    c.query_row(
        if truncate {
            "PRAGMA wal_checkpoint(TRUNCATE)"
        } else {
            "PRAGMA wal_checkpoint(PASSIVE)"
        },
        [],
        |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
    )
}
fn open(path: &Path) -> Result<Connection, String> {
    // Never create the memory file before its schema owner (S13a) does.
    let c = crate::schema::audit::open(
        path,
        OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .map_err(|e| e.to_string())?;
    c.busy_timeout(Duration::from_millis(250))
        .map_err(|e| e.to_string())?;
    c.execute_batch("PRAGMA synchronous=FULL; PRAGMA wal_autocheckpoint=0;")
        .map_err(|e| e.to_string())?;
    let sync: i64 = c
        .pragma_query_value(None, "synchronous", |r| r.get(0))
        .map_err(|e| e.to_string())?;
    if sync != 2 {
        return Err("maintenance synchronous is not FULL".into());
    }
    Ok(c)
}
fn database_directory(path: &Path) -> &Path {
    path.parent()
        .filter(|parent| !parent.as_os_str().is_empty())
        .unwrap_or(Path::new("."))
}

#[cfg(unix)]
#[allow(clippy::unnecessary_cast)] // statvfs field widths vary across Unix targets.
fn free_bytes(path: &Path) -> std::io::Result<u64> {
    use std::{ffi::CString, mem::MaybeUninit, os::unix::ffi::OsStrExt};
    let directory = CString::new(database_directory(path).as_os_str().as_bytes())?;
    let mut stat = MaybeUninit::<libc::statvfs>::uninit();
    // SAFETY: directory is NUL-terminated and stat points to writable storage.
    if unsafe { libc::statvfs(directory.as_ptr(), stat.as_mut_ptr()) } != 0 {
        return Err(std::io::Error::last_os_error());
    }
    // SAFETY: statvfs initialized stat on success.
    let stat = unsafe { stat.assume_init() };
    Ok((stat.f_bavail as u64).saturating_mul(stat.f_frsize as u64))
}

#[cfg(windows)]
fn free_bytes(path: &Path) -> std::io::Result<u64> {
    use std::os::windows::ffi::OsStrExt;
    use windows_sys::Win32::Storage::FileSystem::GetDiskFreeSpaceExW;
    let directory: Vec<u16> = database_directory(path)
        .as_os_str()
        .encode_wide()
        .chain(Some(0))
        .collect();
    let mut available = 0;
    // SAFETY: directory is NUL-terminated and available is writable; the other
    // optional output pointers are null.
    if unsafe {
        GetDiskFreeSpaceExW(
            directory.as_ptr(),
            &mut available,
            std::ptr::null_mut(),
            std::ptr::null_mut(),
        )
    } == 0
    {
        return Err(std::io::Error::last_os_error());
    }
    Ok(available)
}

#[cfg(not(any(unix, windows)))]
fn free_bytes(_: &Path) -> std::io::Result<u64> {
    Err(std::io::Error::new(
        std::io::ErrorKind::Unsupported,
        "disk space sampler unavailable",
    ))
}

fn sample_space<'a>(paths: impl Iterator<Item = &'a Path>) -> Option<u64> {
    // Failed samples are unknown, never zero. Ignore them for admission while
    // retaining low-space pressure from any successfully sampled volume.
    paths
        .filter_map(|path| match free_bytes(path) {
            Ok(free) => Some(free),
            Err(error) => {
                eprintln!("disk space sample failed for {}: {error}", path.display());
                None
            }
        })
        .min()
}
struct FileState {
    path: PathBuf,
    connection: Connection,
    version: DataVersion,
    last_commit: Instant,
}
impl FileState {
    fn new(path: PathBuf) -> Result<Self, String> {
        let connection = open(&path)?;
        let version = DataVersion::new(&connection).map_err(|e| e.to_string())?;
        Ok(Self {
            path,
            connection,
            version,
            last_commit: Instant::now(),
        })
    }
}
enum Command {
    Stop,
    Sample(tokio::sync::oneshot::Sender<Result<i64, String>>),
}
pub struct Maintenance {
    pub disk: Arc<DiskPressure>,
    pub schema: Arc<SchemaGate>,
    pub metrics: Arc<Mutex<Metrics>>,
    stop: mpsc::SyncSender<Command>,
    thread: Option<JoinHandle<()>>,
}
impl Maintenance {
    /// S9 samples the same dedicated watch connection before forwarding and
    /// after comparison. Values from different connections are not comparable.
    pub async fn sample_data_version(&self) -> Result<i64, String> {
        let (reply, receive) = tokio::sync::oneshot::channel();
        self.stop
            .try_send(Command::Sample(reply))
            .map_err(|_| "maintenance-queue-full")?;
        tokio::time::timeout(Duration::from_secs(2), receive)
            .await
            .map_err(|_| "maintenance-timeout")?
            .map_err(|_| "maintenance-stopped")?
    }
    pub fn start(
        hub: &Path,
        memory: &Path,
        writer: Arc<dyn WriterCommands>,
    ) -> Result<Self, String> {
        let watch = crate::schema::audit::open(
            hub,
            OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
        )
        .map_err(|e| e.to_string())?;
        watch
            .execute_batch("PRAGMA query_only=ON; PRAGMA wal_autocheckpoint=0")
            .map_err(|e| e.to_string())?;
        watch
            .busy_timeout(Duration::ZERO)
            .map_err(|e| e.to_string())?;
        let hub = FileState::new(hub.into())?;
        let schema = Arc::new(SchemaGate::default());
        schema.recheck(&hub.connection)?;
        // Startup already validated this cookie. Repeating a valid check on the
        // first timer tick would briefly close the shared admission gate.
        let initial_cookie = Some((
            hub.connection
                .pragma_query_value(None, "schema_version", |r| r.get::<_, i64>(0))
                .map_err(|e| e.to_string())?,
            hub.connection
                .pragma_query_value(None, "user_version", |r| r.get::<_, i64>(0))
                .map_err(|e| e.to_string())?,
        ));
        let mut files = vec![hub];
        // A memory file the store cannot use leaves memory unavailable (503).
        // It never stops the hub's own maintenance: a failed open is counted
        // and retried on later ticks.
        let mut memory_failed = false;
        if memory.is_file() {
            match FileState::new(memory.into()) {
                Ok(file) => files.push(file),
                Err(_) => memory_failed = true,
            }
        }
        let memory_path = memory.to_path_buf();
        let disk = Arc::new(DiskPressure::default());
        // Set the admission state before accepting traffic.
        disk.update(
            files
                .iter()
                .map(|f| {
                    std::fs::metadata(format!("{}-wal", f.path.display()))
                        .map(|m| m.len())
                        .unwrap_or(0)
                })
                .max()
                .unwrap_or(0),
            sample_space(files.iter().map(|f| f.path.as_path())),
        );
        let metrics = Arc::new(Mutex::new(Metrics {
            failures: u64::from(memory_failed),
            ..Metrics::default()
        }));
        let (stop, receive) = mpsc::sync_channel(256);
        let (d, s, m) = (disk.clone(), schema.clone(), metrics.clone());
        let thread = std::thread::spawn(move || {
            let mut sweep_at = Instant::now();
            let mut schema_cookie = initial_cookie;
            let mut space_sample = Instant::now() - Duration::from_secs(1);
            let mut free = None;
            loop {
                match receive.recv_timeout(Duration::from_millis(250)) {
                    Ok(Command::Stop) | Err(mpsc::RecvTimeoutError::Disconnected) => break,
                    Ok(Command::Sample(reply)) => {
                        let sample = watch
                            .pragma_query_value(None, "data_version", |r| r.get(0))
                            .map_err(|e| e.to_string());
                        let _ = reply.send(sample);
                    }
                    Err(mpsc::RecvTimeoutError::Timeout) => {}
                }
                if files.len() == 1 && memory_path.is_file() {
                    match FileState::new(memory_path.clone()) {
                        Ok(file) => files.push(file),
                        Err(_) => m.lock().unwrap().failures += 1,
                    }
                }
                // The read-only audit pauses Python sweepers too. Keep native
                // connections live and instrumented, while excluding autonomous
                // checkpoints and retention from its request-only write window.
                if crate::schema::audit::enabled() {
                    continue;
                }
                let mut max_wal = 0;
                if space_sample.elapsed() >= Duration::from_secs(1) {
                    free = sample_space(files.iter().map(|f| f.path.as_path()));
                    space_sample = Instant::now();
                }
                for (index, file) in files.iter_mut().enumerate() {
                    if file.version.changed(&file.connection).unwrap_or(true) {
                        file.last_commit = Instant::now();
                    }
                    if index == 0 {
                        let cookie: rusqlite::Result<(i64, i64)> = (|| {
                            Ok((
                                file.connection.pragma_query_value(
                                    None,
                                    "schema_version",
                                    |r| r.get(0),
                                )?,
                                file.connection
                                    .pragma_query_value(None, "user_version", |r| r.get(0))?,
                            ))
                        })();
                        match cookie {
                            Ok(cookie) if schema_cookie == Some(cookie) && s.ready() => {}
                            Ok(cookie) => {
                                if s.recheck(&file.connection).is_ok() {
                                    schema_cookie = Some(cookie);
                                }
                            }
                            Err(_) => {
                                let _ = s.recheck(&file.connection);
                            }
                        }
                    }
                    let wal = std::fs::metadata(format!("{}-wal", file.path.display()))
                        .map(|m| m.len())
                        .unwrap_or(0);
                    max_wal = max_wal.max(wal);
                    if wal > 64 * MIB || file.last_commit.elapsed() >= Duration::from_secs(1) {
                        let mut metrics = m.lock().unwrap();
                        match checkpoint(&file.connection, wal > 256 * MIB) {
                            Ok((busy, log, done)) => {
                                metrics.checkpoints += 1;
                                if index == 0 {
                                    metrics.hub_checkpoints += 1;
                                } else {
                                    metrics.memory_checkpoints += 1;
                                }
                                metrics.log_frames = log;
                                metrics.checkpointed_frames = done;
                                if busy != 0 || done < log {
                                    metrics.busy += 1;
                                }
                            }
                            Err(_) => metrics.failures += 1,
                        }
                    }
                }
                d.update(max_wal, free);
                {
                    let mut metrics = m.lock().unwrap();
                    metrics.wal_bytes = max_wal;
                    metrics.free_bytes = free;
                }
                if sweep_at.elapsed() >= Duration::from_secs(1) {
                    for command in [
                        Sweep::HubRetention,
                        Sweep::MemoryRetention,
                        Sweep::VectorCleanup,
                    ] {
                        if !writer.schedule(command) {
                            m.lock().unwrap().sweeps_rejected += 1;
                        }
                    }
                    sweep_at = Instant::now();
                }
            }
            for file in files {
                let _ = checkpoint(&file.connection, false);
            }
        });
        Ok(Self {
            disk,
            schema,
            metrics,
            stop,
            thread: Some(thread),
        })
    }
}
impl Drop for Maintenance {
    fn drop(&mut self) {
        let _ = self.stop.send(Command::Stop);
        if let Some(t) = self.thread.take() {
            let _ = t.join();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn failed_sample_does_not_trip_pressure() {
        let path = std::env::temp_dir()
            .join(format!("hub-missing-volume-{}", std::process::id()))
            .join("missing")
            .join("hub.db");
        let sample = sample_space(std::iter::once(path.as_path()));
        assert_eq!(sample, None);
        let disk = DiskPressure::default();
        disk.update(0, sample);
        assert_eq!(disk.admit(false), Ok(()));
        disk.update(GIB + 1, sample);
        assert_eq!(disk.admit(false), Err("disk-pressure"));
        disk.update(0, Some(0));
        assert_eq!(disk.admit(false), Err("disk-pressure"));
    }

    #[cfg(unix)]
    #[test]
    fn unix_sampler_starts_no_child_process() {
        if std::env::var_os("HUB_SAMPLER_TEST_CHILD").is_none() {
            let dir = std::env::temp_dir().join(format!("hub-sampler-{}", std::process::id()));
            std::fs::create_dir_all(&dir).unwrap();
            let result = std::process::Command::new(std::env::current_exe().unwrap())
                .args([
                    "--exact",
                    "maintenance::tests::unix_sampler_starts_no_child_process",
                ])
                .env("HUB_SAMPLER_TEST_CHILD", "1")
                .current_dir(&dir)
                .status()
                .unwrap();
            std::fs::remove_dir_all(dir).unwrap();
            assert!(result.success());
            return;
        }
        #[cfg(all(target_os = "linux", target_arch = "x86_64"))]
        {
            // This isolated test thread may not create processes. A subprocess
            // implementation cannot return a successful sample under this filter.
            let mut filter = vec![libc::sock_filter {
                code: 0x20,
                jt: 0,
                jf: 0,
                k: 0, // BPF_LD | BPF_W | BPF_ABS: syscall nr
            }];
            for syscall in [
                libc::SYS_clone,
                libc::SYS_clone3,
                libc::SYS_fork,
                libc::SYS_vfork,
            ] {
                filter.push(libc::sock_filter {
                    code: 0x15,
                    jt: 0,
                    jf: 1,
                    k: syscall as u32, // BPF_JMP | BPF_JEQ
                });
                filter.push(libc::sock_filter {
                    code: 0x06,
                    jt: 0,
                    jf: 0, // BPF_RET: EPERM
                    k: libc::SECCOMP_RET_ERRNO | libc::EPERM as u32,
                });
            }
            filter.push(libc::sock_filter {
                code: 0x06,
                jt: 0,
                jf: 0,
                k: libc::SECCOMP_RET_ALLOW,
            });
            let program = libc::sock_fprog {
                len: filter.len() as u16,
                filter: filter.as_mut_ptr(),
            };
            // SAFETY: prctl receives the documented scalar arguments and a valid
            // BPF program. The filter applies only to this isolated test thread.
            unsafe {
                assert_eq!(libc::prctl(libc::PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0), 0);
                assert_eq!(
                    libc::prctl(libc::PR_SET_SECCOMP, libc::SECCOMP_MODE_FILTER, &program),
                    0
                );
            }
        }
        // No other tests run in this subprocess, so child resource accounting
        // also catches reaped sampler children on other Unix platforms.
        let children = || {
            let mut usage = std::mem::MaybeUninit::<libc::rusage>::uninit();
            // SAFETY: usage points to writable storage, initialized on success.
            assert_eq!(
                unsafe { libc::getrusage(libc::RUSAGE_CHILDREN, usage.as_mut_ptr()) },
                0
            );
            unsafe { usage.assume_init() }
        };
        let before = children();
        assert_eq!(database_directory(Path::new("hub.db")), Path::new("."));
        for _ in 0..3 {
            assert!(free_bytes(Path::new("hub.db")).unwrap() > 0);
        }
        let after = children();
        assert_eq!(
            (
                before.ru_utime.tv_sec,
                before.ru_utime.tv_usec,
                before.ru_stime.tv_sec,
                before.ru_stime.tv_usec
            ),
            (
                after.ru_utime.tv_sec,
                after.ru_utime.tv_usec,
                after.ru_stime.tv_sec,
                after.ru_stime.tv_usec
            )
        );
    }
}
