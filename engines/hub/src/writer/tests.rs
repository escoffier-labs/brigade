use super::*;
use rusqlite::Connection;
use std::path::Path;
use std::sync::atomic::{AtomicUsize, Ordering};
static NEXT: AtomicUsize = AtomicUsize::new(0);
pub(super) fn fixture() -> PathBuf {
    let dir = std::env::temp_dir().join(format!(
        "hub-writer-{}-{}",
        std::process::id(),
        NEXT.fetch_add(1, Ordering::Relaxed)
    ));
    std::fs::create_dir_all(&dir).unwrap();
    let path = dir.join("fleet-hub.db");
    Connection::open(&path).unwrap().execute_batch("PRAGMA journal_mode=WAL; CREATE TABLE rows(id INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT UNIQUE); CREATE TABLE child(parent INTEGER REFERENCES rows(id) DEFERRABLE INITIALLY DEFERRED);").unwrap();
    path
}
struct Sql(&'static str);
impl Command for Sql {
    fn apply(&self, tx: &dyn WriteTx) -> Result<Json, ApplyError> {
        if self.0 == "FULL" {
            tx.query("PRAGMA max_page_count=10", &[])?;
            tx.execute("INSERT INTO rows(value) VALUES(zeroblob(1000000))", &[])?;
        } else {
            tx.execute(self.0, &[])?;
        }
        Ok(Json::Bool(true))
    }
}
fn budget(durable: bool) -> Budget {
    Budget {
        events: 1,
        bytes: 100,
        durable,
        deadline: Instant::now() + Duration::from_secs(10),
    }
}
fn batch(path: &Path, commands: Vec<Sql>) -> Vec<Answer> {
    batch_with_stats(path, commands, Arc::default())
}
fn batch_with_stats(path: &Path, commands: Vec<Sql>, stats: Arc<WriterStats>) -> Vec<Answer> {
    let arbiter = Arc::new(Arbiter::default());
    let (sender, receiver) = mpsc::channel(100);
    let mut replies = Vec::new();
    for command in commands {
        let (reply, receiver) = oneshot::channel();
        sender
            .try_send(Envelope {
                command: Box::new(command),
                budget: budget(false),
                reply: Some(reply),
                _permits: vec![],
                arbiter: arbiter.clone(),
                queued: Instant::now(),
            })
            .unwrap_or_else(|_| panic!("queue"));
        replies.push(receiver);
    }
    drop(sender);
    worker::run(
        worker::open(path).unwrap(),
        receiver,
        Config::new(path.to_path_buf()),
        arbiter,
        stats,
    );
    replies
        .into_iter()
        .map(|r| r.blocking_recv().unwrap())
        .collect()
}
fn values(path: &Path) -> i64 {
    Connection::open(path)
        .unwrap()
        .query_row("SELECT count(*) FROM rows", [], |r| r.get(0))
        .unwrap()
}
#[test]
fn savepoint_failure_keeps_neighbors() {
    let path = fixture();
    let results = batch(
        &path,
        vec![
            Sql("INSERT INTO rows(value) VALUES('a')"),
            Sql("INSERT INTO rows(value) VALUES('a')"),
            Sql("INSERT INTO rows(value) VALUES('b')"),
        ],
    );
    assert!(results[0].is_ok() && results[1].is_err() && results[2].is_ok());
    assert_eq!(values(&path), 2);
}
#[test]
fn transaction_abort_replays_prior_command_once() {
    let path = fixture();
    let results = batch(
        &path,
        vec![
            Sql("INSERT INTO rows(value) VALUES('a')"),
            Sql("INSERT OR ROLLBACK INTO rows(value) VALUES('a')"),
            Sql("INSERT INTO rows(value) VALUES('b')"),
        ],
    );
    assert!(results[0].is_ok() && results[1].is_err() && results[2].is_ok());
    assert_eq!(values(&path), 2);
}
#[test]
fn actual_sqlite_full_never_acknowledges_lost_rows() {
    struct Full;
    impl Command for Full {
        fn apply(&self, tx: &dyn WriteTx) -> Result<Json, ApplyError> {
            tx.query("PRAGMA max_page_count=10", &[])?;
            tx.execute("INSERT INTO rows(value) VALUES(zeroblob(1000000))", &[])?;
            Ok(Json::Null)
        }
    }
    let path = fixture();
    let db = worker::open(&path).unwrap();
    db.execute_batch("BEGIN IMMEDIATE").unwrap();
    let arbiter = Arc::new(Arbiter::default());
    let (reply, _) = oneshot::channel();
    let e = Envelope {
        command: Box::new(Full),
        budget: budget(false),
        reply: Some(reply),
        _permits: vec![],
        arbiter,
        queued: Instant::now(),
    };
    // The SQLite FULL error really aborts the outer transaction.
    assert!(super::worker::test_apply(&db, &e).is_err());
    assert!(db.is_autocommit());
    drop(db);
    let results = batch(
        &path,
        vec![
            Sql("INSERT INTO rows(value) VALUES('a')"),
            Sql("FULL"),
            Sql("INSERT INTO rows(value) VALUES('b')"),
        ],
    );
    assert!(results[0].is_ok() && results[1].is_err() && results[2].is_ok());
    assert_eq!(values(&path), 2);
}
#[test]
fn failed_commit_fails_entire_batch_and_reopens() {
    let path = fixture();
    let results = batch(
        &path,
        vec![
            Sql("INSERT INTO rows(value) VALUES('a')"),
            Sql("INSERT INTO child(parent) VALUES(999)"),
        ],
    );
    assert!(
        results
            .iter()
            .all(|r| r == &Err(Failure::storage("commit-failed")))
    );
    assert_eq!(values(&path), 0);
    assert!(batch(&path, vec![Sql("INSERT INTO rows(value) VALUES('b')")])[0].is_ok());
}
#[tokio::test]
async fn overload_and_durable_reserve_are_bounded() {
    let path = fixture();
    let arbiter = Arc::new(Arbiter::default());
    let turn = arbiter
        .acquire(Lane::Memory, Instant::now() + Duration::from_secs(1))
        .unwrap();
    let mut config = Config::new(path);
    config.capacity = 6;
    config.durable_reserve = 2;
    let writer = start_fixture(config, arbiter).unwrap();
    let mut accepted = Vec::new();
    let mut rejected = 0;
    for _ in 0..18 {
        match writer
            .submit(
                Sql("INSERT INTO rows(value) VALUES(random())"),
                budget(false),
            )
            .await
        {
            Ok(r) => accepted.push(r),
            Err(e) => {
                assert_eq!(e.status, 503);
                rejected += 1;
            }
        }
    }
    assert_eq!(accepted.len(), 4);
    assert_eq!(rejected, 14);
    assert_eq!(writer.slots.available_permits(), 2);
    let durable = writer
        .submit(
            Sql("INSERT INTO rows(value) VALUES('durable')"),
            budget(true),
        )
        .await
        .unwrap();
    drop(turn);
    for r in accepted {
        assert!(r.await.unwrap().is_ok());
    }
    assert!(durable.await.unwrap().is_ok());
}
#[tokio::test]
async fn python_lock_for_three_seconds_delays_durable_command() {
    use std::io::{BufRead, BufReader};
    use std::process::{Command as Process, Stdio};
    let path = fixture();
    let writer = start_fixture(Config::new(path.to_path_buf()), Arc::default()).unwrap();
    let mut child = Process::new("python3").arg("-c").arg("import sqlite3,sys,time\nc=sqlite3.connect(sys.argv[1]); c.execute('BEGIN IMMEDIATE'); print('locked',flush=True); time.sleep(3); c.rollback()")
        .arg(&path).stdout(Stdio::piped()).spawn().unwrap();
    let mut line = String::new();
    BufReader::new(child.stdout.take().unwrap())
        .read_line(&mut line)
        .unwrap();
    assert_eq!(line.trim(), "locked");
    let start = Instant::now();
    let r = writer
        .submit(Sql("INSERT INTO rows(value) VALUES('claim')"), budget(true))
        .await
        .unwrap();
    assert!(r.await.unwrap().is_ok());
    assert!(start.elapsed() >= Duration::from_millis(2800));
    assert!(child.wait().unwrap().success());
}
#[test]
fn memory_bucket_and_durable_hub_priority() {
    let a = Arc::new(Arbiter::default());
    let turn = a
        .acquire(Lane::Memory, Instant::now() + Duration::from_secs(1))
        .unwrap();
    std::thread::sleep(Duration::from_millis(300));
    drop(turn);
    assert!(
        a.acquire(Lane::Memory, Instant::now() + Duration::from_millis(10))
            .is_none()
    );
    assert!(
        a.acquire(Lane::Hub, Instant::now() + Duration::from_millis(10))
            .is_some()
    );
}

#[test]
#[ignore = "subprocess helper for kill_nine_twenty_runs"]
fn kill_child() {
    use std::io::Write;
    let path = PathBuf::from(std::env::var_os("HUB_CRASH_DB").unwrap());
    let ledger_path = std::env::var_os("HUB_CRASH_LEDGER").unwrap();
    let mut ledger = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(ledger_path)
        .unwrap();
    let rt = tokio::runtime::Runtime::new().unwrap();
    rt.block_on(async {
        struct Insert(String);
        impl Command for Insert {
            fn apply(&self, tx: &dyn WriteTx) -> Result<Json, ApplyError> {
                tx.execute(
                    "INSERT INTO rows(value) VALUES(?1)",
                    &[Value::Text(self.0.clone())],
                )?;
                Ok(Json::Null)
            }
        }
        let writer = start_fixture(Config::new(path), Arc::default()).unwrap();
        for base in (0..100000).step_by(64) {
            let mut in_flight = Vec::new();
            for i in base..base + 64 {
                let value = format!("{}-{i}", std::process::id());
                let reply = writer
                    .submit(Insert(value.clone()), budget(true))
                    .await
                    .unwrap();
                in_flight.push((value, reply));
            }
            for (value, reply) in in_flight {
                assert!(reply.await.unwrap().is_ok());
                ledger.write_all(format!("{value}\n").as_bytes()).unwrap();
                ledger.sync_all().unwrap();
            }
        }
    });
}
#[test]
#[ignore = "20 SIGKILL subprocess runs with external acknowledgement ledger"]
fn kill_nine_twenty_runs() {
    let path = fixture();
    let ledger = path.with_extension("ack");
    for run in 0..20 {
        let before = std::fs::read_to_string(&ledger)
            .unwrap_or_default()
            .lines()
            .count();
        let mut child = std::process::Command::new(std::env::current_exe().unwrap())
            .args(["--ignored", "--exact", "writer::tests::kill_child"])
            .env("HUB_CRASH_DB", &path)
            .env("HUB_CRASH_LEDGER", &ledger)
            .stdout(std::process::Stdio::null())
            .spawn()
            .unwrap();
        let deadline = Instant::now() + Duration::from_secs(15);
        while std::fs::read_to_string(&ledger)
            .unwrap_or_default()
            .lines()
            .count()
            < before + 10 + run
        {
            assert!(Instant::now() < deadline, "child made no progress");
            assert!(child.try_wait().unwrap().is_none());
            std::thread::sleep(Duration::from_millis(5));
        }
        child.kill().unwrap();
        child.wait().unwrap();
        let db = Connection::open(&path).unwrap();
        assert_eq!(
            db.query_row("PRAGMA integrity_check", [], |r| r.get::<_, String>(0))
                .unwrap(),
            "ok"
        );
        let entries = std::fs::read_to_string(&ledger).unwrap();
        let complete = entries.rfind('\n').map(|i| i + 1).unwrap_or(0);
        std::fs::OpenOptions::new()
            .write(true)
            .open(&ledger)
            .unwrap()
            .set_len(complete as u64)
            .unwrap();
        for value in entries[..complete].split_terminator('\n') {
            assert!(
                db.query_row(
                    "SELECT EXISTS(SELECT 1 FROM rows WHERE value=?1)",
                    [value],
                    |r| r.get::<_, bool>(0)
                )
                .unwrap()
            );
        }
    }
    std::fs::remove_dir_all(path.parent().unwrap()).unwrap();
}

#[tokio::test]
async fn expired_and_expensive_commands_do_not_commit() {
    struct Expensive;
    impl Command for Expensive {
        fn apply(&self, tx: &dyn WriteTx) -> Result<Json, ApplyError> {
            tx.execute("INSERT INTO rows(value) VALUES('must-rollback')", &[])?;
            tx.query("WITH RECURSIVE x(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM x WHERE n<100000000) SELECT sum(n) FROM x",&[])?;
            Ok(Json::Null)
        }
    }
    let path = fixture();
    let writer = start_fixture(Config::new(path.clone()), Arc::default()).unwrap();
    let mut b = budget(true);
    b.deadline = Instant::now() + Duration::from_millis(20);
    let reply = writer.submit(Expensive, b).await.unwrap();
    assert_eq!(reply.await.unwrap(), Err(Failure::timeout(true)));
    assert_eq!(values(&path), 0);
}

#[tokio::test]
async fn overload_three_times_default_capacity_records_rss() {
    struct Payload(Vec<u8>);
    impl Command for Payload {
        fn apply(&self, tx: &dyn WriteTx) -> Result<Json, ApplyError> {
            tx.execute(
                "INSERT INTO rows(value) VALUES(?1)",
                &[Value::Blob(self.0.clone())],
            )?;
            Ok(Json::Null)
        }
    }
    fn rss() -> usize {
        #[cfg(target_os = "linux")]
        {
            std::fs::read_to_string("/proc/self/status")
                .unwrap()
                .lines()
                .find(|l| l.starts_with("VmRSS:"))
                .unwrap()
                .split_whitespace()
                .nth(1)
                .unwrap()
                .parse::<usize>()
                .unwrap()
                * 1024
        }
        #[cfg(not(target_os = "linux"))]
        {
            0
        }
    }
    let path = fixture();
    let arbiter = Arc::new(Arbiter::default());
    let turn = arbiter
        .acquire(Lane::Memory, Instant::now() + Duration::from_secs(2))
        .unwrap();
    let writer = start_fixture(Config::new(path), arbiter).unwrap();
    let before = rss();
    let mut replies = Vec::new();
    let mut rejected = 0;
    for _ in 0..3 * 4096 {
        let mut b = budget(false);
        b.bytes = 16 * 1024;
        match writer.submit(Payload(vec![0; 16 * 1024]), b).await {
            Ok(r) => replies.push(r),
            Err(e) => {
                assert_eq!(e.status, 503);
                rejected += 1;
            }
        }
    }
    let growth = rss().saturating_sub(before);
    eprintln!(
        "3x overload: accepted={}, excess={rejected}, RSS growth={growth} bytes",
        replies.len()
    );
    assert_eq!(replies.len(), 3584);
    assert_eq!(rejected, 12288 - 3584);
    assert!(growth < 96 * 1024 * 1024);
    drop(turn);
    drop(writer);
    drop(replies);
}

#[test]
fn batch_budgets_and_no_linger() {
    let path = fixture();
    let db = worker::open(&path).unwrap();
    let commits = Arc::new(AtomicUsize::new(0));
    let observed = commits.clone();
    db.commit_hook(Some(move || {
        observed.fetch_add(1, Ordering::Relaxed);
        false
    }))
    .unwrap();
    let arbiter = Arc::new(Arbiter::default());
    let (sender, receiver) = mpsc::channel(300);
    let mut replies = Vec::new();
    for i in 0..300 {
        struct Insert(usize);
        impl Command for Insert {
            fn apply(&self, tx: &dyn WriteTx) -> Result<Json, ApplyError> {
                tx.execute(
                    "INSERT INTO rows(value) VALUES(?1)",
                    &[Value::Integer(self.0 as i64)],
                )?;
                Ok(Json::Null)
            }
        }
        let (reply, r) = oneshot::channel();
        let mut b = budget(false);
        b.events = 20;
        b.bytes = 40000;
        sender
            .try_send(Envelope {
                command: Box::new(Insert(i)),
                budget: b,
                reply: Some(reply),
                _permits: vec![],
                arbiter: arbiter.clone(),
                queued: Instant::now(),
            })
            .unwrap_or_else(|_| panic!("queue"));
        replies.push(r);
    }
    drop(sender);
    worker::run(db, receiver, Config::new(path), arbiter, Arc::default());
    for r in replies {
        assert!(r.blocking_recv().unwrap().is_ok());
    }
    // Event budget fits 204 commands and byte budget fits 209, so two commits.
    assert_eq!(commits.load(Ordering::Relaxed), 2);
}

#[tokio::test]
async fn byte_admission_and_begin_deadline() {
    let path = fixture();
    let arbiter = Arc::new(Arbiter::default());
    let turn = arbiter
        .acquire(Lane::Memory, Instant::now() + Duration::from_secs(1))
        .unwrap();
    let mut config = Config::new(path.clone());
    config.queue_bytes = 1024;
    let writer = start_fixture(config, arbiter).unwrap();
    let mut b = budget(false);
    b.bytes = 600;
    let first = writer
        .submit(Sql("INSERT INTO rows(value) VALUES('bytes')"), b)
        .await
        .unwrap();
    assert!(matches!(
        writer.submit(Sql("SELECT 1"), b).await,
        Err(Failure {
            status: 503,
            reason: "writer-queue-full"
        })
    ));
    drop(turn);
    assert!(first.await.unwrap().is_ok());
    let lock = Connection::open(&path).unwrap();
    lock.execute_batch("BEGIN IMMEDIATE").unwrap();
    let mut b = budget(true);
    b.deadline = Instant::now() + Duration::from_millis(50);
    let started = Instant::now();
    let reply = writer
        .submit(Sql("INSERT INTO rows(value) VALUES('late')"), b)
        .await
        .unwrap();
    assert_eq!(reply.await.unwrap(), Err(Failure::timeout(true)));
    assert!(started.elapsed() < Duration::from_millis(500));
    lock.execute_batch("ROLLBACK").unwrap();
    assert_eq!(values(&path), 1);
}
#[tokio::test]
async fn domain_refusal_rolls_back_partial_command() {
    struct Refuse;
    impl Command for Refuse {
        fn apply(&self, tx: &dyn WriteTx) -> Result<Json, ApplyError> {
            tx.execute("INSERT INTO rows(value) VALUES('refused')", &[])?;
            Err(ApplyError::Rejected(Failure {
                status: 409,
                reason: "conflict",
            }))
        }
    }
    let path = fixture();
    let writer = start_fixture(Config::new(path.clone()), Arc::default()).unwrap();
    let reply = writer.submit(Refuse, budget(false)).await.unwrap();
    assert_eq!(
        reply.await.unwrap(),
        Err(Failure {
            status: 409,
            reason: "conflict"
        })
    );
    assert_eq!(values(&path), 0);
}

#[test]
fn second_abort_fails_prior_commands_instead_of_replaying_again() {
    struct AbortOnReplay(Arc<AtomicUsize>);
    impl Command for AbortOnReplay {
        fn apply(&self, tx: &dyn WriteTx) -> Result<Json, ApplyError> {
            tx.execute("INSERT INTO rows(value) VALUES('a')", &[])?;
            if self.0.fetch_add(1, Ordering::Relaxed) > 0 {
                tx.execute("INSERT OR ROLLBACK INTO rows(value) VALUES('a')", &[])?;
            }
            Ok(Json::Null)
        }
    }
    let path = fixture();
    let arbiter = Arc::new(Arbiter::default());
    let (sender, receiver) = mpsc::channel(4);
    let calls = Arc::new(AtomicUsize::new(0));
    let mut replies = Vec::new();
    let commands: Vec<Box<dyn Command>> = vec![
        Box::new(AbortOnReplay(calls.clone())),
        Box::new(Sql("INSERT OR ROLLBACK INTO rows(value) VALUES('a')")),
        Box::new(Sql("INSERT INTO rows(value) VALUES('survivor')")),
    ];
    for command in commands {
        let (reply, r) = oneshot::channel();
        sender
            .try_send(Envelope {
                command,
                budget: budget(false),
                reply: Some(reply),
                _permits: vec![],
                arbiter: arbiter.clone(),
                queued: Instant::now(),
            })
            .unwrap_or_else(|_| panic!("queue"));
        replies.push(r);
    }
    drop(sender);
    worker::run(
        worker::open(&path).unwrap(),
        receiver,
        Config::new(path.clone()),
        arbiter,
        Arc::default(),
    );
    let results: Vec<_> = replies
        .into_iter()
        .map(|r| r.blocking_recv().unwrap())
        .collect();
    assert!(results[0].is_err() && results[1].is_err() && results[2].is_ok());
    assert_eq!(calls.load(Ordering::Relaxed), 2);
    assert_eq!(values(&path), 1);
}

#[tokio::test]
async fn interrupted_write_abort_keeps_durable_timeout_status() {
    struct ExpensiveWrite;
    impl Command for ExpensiveWrite {
        fn apply(&self, tx: &dyn WriteTx) -> Result<Json, ApplyError> {
            tx.execute("WITH RECURSIVE x(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM x WHERE n<100000000) INSERT INTO rows(value) SELECT n FROM x",&[])?;
            Ok(Json::Null)
        }
    }
    let path = fixture();
    let writer = start_fixture(Config::new(path.clone()), Arc::default()).unwrap();
    let mut b = budget(true);
    b.deadline = Instant::now() + Duration::from_millis(20);
    let reply = writer.submit(ExpensiveWrite, b).await.unwrap();
    assert_eq!(reply.await.unwrap(), Err(Failure::timeout(true)));
    assert_eq!(values(&path), 0);
}

#[test]
fn sync_directory_is_canonical_database_directory() {
    let path = fixture();
    let db = worker::open(&path).unwrap();
    assert_eq!(
        worker::database_directory(&db).unwrap(),
        std::fs::canonicalize(&path).unwrap().parent().unwrap()
    );
    drop(db);
    std::fs::remove_dir_all(path.parent().unwrap()).unwrap();
}

#[cfg(unix)]
#[test]
fn opener_refuses_symlinked_database_and_sidecars() {
    use std::os::unix::fs::symlink;
    let path = fixture();
    let alias = path.with_extension("link");
    symlink(&path, &alias).unwrap();
    assert!(worker::open(&alias).is_err());
    std::fs::remove_file(alias).unwrap();
    for suffix in ["-wal", "-shm", "-journal"] {
        let sidecar = PathBuf::from(format!("{}{suffix}", path.display()));
        symlink(&path, &sidecar).unwrap();
        assert!(worker::open(&path).is_err(), "accepted {suffix} symlink");
        std::fs::remove_file(sidecar).unwrap();
    }
    // Python permits a symlinked parent. Sync still uses the canonical directory.
    let parent_alias = path.parent().unwrap().with_extension("link");
    symlink(path.parent().unwrap(), &parent_alias).unwrap();
    let db = worker::open(&parent_alias.join(path.file_name().unwrap())).unwrap();
    assert_eq!(
        worker::database_directory(&db).unwrap(),
        std::fs::canonicalize(path.parent().unwrap()).unwrap()
    );
    drop(db);
    std::fs::remove_file(parent_alias).unwrap();
    std::fs::remove_dir_all(path.parent().unwrap()).unwrap();
}

// Storage fault fixtures intentionally have a tiny schema. Production startup
// always checks the full Python-owned contract before starting its worker.
fn start_fixture(config: Config, arbiter: Arc<Arbiter>) -> rusqlite::Result<Writer> {
    let connection = worker::open(&config.path)?;
    Writer::start_connection(config, arbiter, connection)
}

#[test]
fn startup_refuses_unrecognized_schema() {
    assert!(Writer::start(Config::new(fixture()), Arc::default()).is_err());
}

#[test]
fn maintenance_commands_use_writer_and_refuse_unsupported_work() {
    use crate::maintenance::{Sweep, WriterCommands};
    let path = fixture();
    let writer = start_fixture(Config::new(path.clone()), Arc::default()).unwrap();
    let sweeps = writer.sweeps(|sweep| match sweep {
        Sweep::HubRetention => {
            Some(Box::new(Sql("INSERT INTO rows(value) VALUES('sweep')")) as Box<dyn Command>)
        }
        _ => None,
    });
    assert!(!sweeps.schedule(Sweep::MemoryRetention));
    assert!(!sweeps.schedule(Sweep::VectorCleanup));
    assert!(sweeps.schedule(Sweep::HubRetention));
    let deadline = Instant::now() + Duration::from_secs(5);
    while values(&path) == 0 {
        assert!(Instant::now() < deadline);
        std::thread::sleep(Duration::from_millis(10));
    }
}

#[test]
fn maintenance_admission_is_bounded_and_releases_partial_permits() {
    use crate::maintenance::{Sweep, WriterCommands};
    let mut config = Config::new(fixture());
    config.queue_bytes = 1024;
    let writer = start_fixture(config, Arc::default()).unwrap();
    let sweeps = writer.sweeps(|_| Some(Box::new(Sql("SELECT 1")) as Box<dyn Command>));
    assert!(!sweeps.schedule(Sweep::HubRetention));
    assert_eq!(writer.slots.available_permits(), 4096);
    assert_eq!(writer.regular.available_permits(), 3584);
    assert_eq!(writer.bytes.available_permits(), 1024);
}

fn metric(text: &str, name: &str) -> f64 {
    let prefix = format!("brigade_hub_{name} ");
    text.lines()
        .find_map(|line| line.strip_prefix(&prefix))
        .unwrap_or_else(|| panic!("{name} missing from {text}"))
        .parse()
        .unwrap()
}

#[tokio::test]
async fn writer_metrics_move_under_writes() {
    let path = fixture();
    let writer = start_fixture(Config::new(path.clone()), Arc::default()).unwrap();
    let telemetry = crate::telemetry::Telemetry::default();
    let before = telemetry.render();
    assert!(before.contains("brigade_hub_writer_queue_depth NaN"));
    assert!(before.contains("No writer in this process"));
    telemetry.attach_writer(writer.stats());
    let idle = telemetry.render();
    for name in [
        "writer_queue_depth",
        "writer_batch_size",
        "writer_apply_seconds",
        "writer_commit_seconds",
        "writer_lock_wait_seconds",
        "writer_aborted_batches_total",
    ] {
        assert_eq!(metric(&idle, name), 0.0, "{name} starts at zero");
    }
    // A foreign write lock holds the worker in its BEGIN retry. The first command is
    // already off the queue, so the other two are what the depth gauge reports.
    let lock = Connection::open(&path).unwrap();
    lock.execute_batch("BEGIN IMMEDIATE").unwrap();
    let mut replies = Vec::new();
    for n in 0..3 {
        let command = Sql(Box::leak(
            format!("INSERT INTO rows(value) VALUES('metric-{n}')").into_boxed_str(),
        ));
        replies.push(writer.submit(command, budget(false)).await.unwrap());
    }
    let deadline = Instant::now() + Duration::from_secs(5);
    while writer.stats().snapshot().queue_depth != 2 {
        assert!(Instant::now() < deadline, "queue depth never reached 2");
        std::thread::sleep(Duration::from_millis(5));
    }
    assert_eq!(metric(&telemetry.render(), "writer_queue_depth"), 2.0);
    std::thread::sleep(Duration::from_millis(250));
    lock.execute_batch("ROLLBACK").unwrap();
    for reply in replies {
        assert!(reply.await.unwrap().is_ok());
    }
    assert_eq!(values(&path), 3);
    let after = telemetry.render();
    assert_eq!(metric(&after, "writer_queue_depth"), 0.0);
    assert_eq!(metric(&after, "writer_batch_size"), 3.0);
    assert!(metric(&after, "writer_apply_seconds") > 0.0);
    assert!(metric(&after, "writer_commit_seconds") > 0.0);
    assert!(
        metric(&after, "writer_lock_wait_seconds") >= 0.2,
        "lock wait covers the BEGIN retries"
    );
    assert_eq!(metric(&after, "writer_aborted_batches_total"), 0.0);
}

#[test]
fn aborted_batches_count_once_per_batch() {
    let clean = Arc::new(WriterStats::default());
    let path = fixture();
    batch_with_stats(
        &path,
        vec![
            Sql("INSERT INTO rows(value) VALUES('a')"),
            Sql("INSERT INTO rows(value) VALUES('a')"),
            Sql("INSERT INTO rows(value) VALUES('b')"),
        ],
        clean.clone(),
    );
    // A refused command rolls back to its savepoint and is not an abort.
    let snapshot = clean.snapshot();
    assert_eq!((snapshot.batch_size, snapshot.aborted_batches), (3, 0));

    let aborted = Arc::new(WriterStats::default());
    let path = fixture();
    batch_with_stats(
        &path,
        vec![
            Sql("INSERT INTO rows(value) VALUES('a')"),
            Sql("INSERT OR ROLLBACK INTO rows(value) VALUES('a')"),
            Sql("INSERT INTO rows(value) VALUES('b')"),
        ],
        aborted.clone(),
    );
    let snapshot = aborted.snapshot();
    assert_eq!((snapshot.batch_size, snapshot.aborted_batches), (2, 1));
    assert!(snapshot.commit_seconds > 0.0);
}

#[tokio::test]
async fn heartbeat_bytes_leave_durable_admission_reserve() {
    let path = fixture();
    let arbiter = Arc::new(Arbiter::default());
    let turn = arbiter
        .acquire(Lane::Memory, Instant::now() + Duration::from_secs(1))
        .unwrap();
    let writer = start_fixture(Config::new(path.clone()), arbiter).unwrap();
    let mut heartbeat = budget(false);
    heartbeat.bytes = 8 * 1024 * 1024;
    let mut accepted = Vec::new();
    for _ in 0..7 {
        accepted.push(
            writer
                .submit(Sql("INSERT INTO rows(value) VALUES(random())"), heartbeat)
                .await
                .unwrap(),
        );
    }
    let refused = writer
        .submit(Sql("INSERT INTO rows(value) VALUES(random())"), heartbeat)
        .await;
    assert!(matches!(
        refused,
        Err(Failure {
            status: 503,
            reason: "writer-queue-full"
        })
    ));
    let mut claim = budget(true);
    claim.bytes = 8 * 1024 * 1024;
    claim.deadline = Instant::now() + Duration::from_secs(1);
    let reply = writer
        .submit(Sql("INSERT INTO rows(value) VALUES('claim')"), claim)
        .await
        .unwrap();
    assert_eq!(writer.bytes.available_permits(), 0);
    drop(turn);
    for reply in accepted {
        assert!(reply.await.unwrap().is_ok());
    }
    assert!(reply.await.unwrap().is_ok());
    assert_eq!(values(&path), 8);
}

#[test]
fn commit_histogram_has_sum() {
    let stats = Arc::new(WriterStats::default());
    stats.batch(1, Duration::ZERO, Some(Duration::from_millis(2)), false);
    stats.batch(1, Duration::ZERO, Some(Duration::from_millis(21)), true);
    let telemetry = crate::telemetry::Telemetry::default();
    telemetry.attach_writer(stats);
    let text = telemetry.render();
    assert_eq!(metric(&text, "writer_commit_duration_seconds_count"), 2.0);
    assert!((metric(&text, "writer_commit_duration_seconds_sum") - 0.023).abs() < 1e-9);
}

#[test]
fn durable_commands_run_ahead_of_queued_ordinary_work() {
    // Three ordinary maximum-size commands queue ahead of one claim-sized
    // durable command. Each ordinary command fills a batch on its own, so FIFO
    // order would commit the durable command fourth.
    let path = fixture();
    let arbiter = Arc::new(Arbiter::default());
    let (sender, receiver) = mpsc::channel(100);
    let mut replies = Vec::new();
    for (value, durable) in [
        ("first", false),
        ("second", false),
        ("third", false),
        ("claim", true),
    ] {
        if durable {
            arbiter.durable(1);
        }
        let (reply, answer) = oneshot::channel();
        let sql: &'static str =
            Box::leak(format!("INSERT INTO rows(value) VALUES('{value}')").into_boxed_str());
        sender
            .try_send(Envelope {
                command: Box::new(Sql(sql)),
                budget: Budget {
                    bytes: if durable { 100 } else { 8 * 1024 * 1024 },
                    ..budget(durable)
                },
                reply: Some(reply),
                _permits: vec![],
                arbiter: arbiter.clone(),
                queued: Instant::now(),
            })
            .unwrap_or_else(|_| panic!("queue"));
        replies.push(answer);
    }
    drop(sender);
    let stats = Arc::new(WriterStats::default());
    worker::run(
        worker::open(&path).unwrap(),
        receiver,
        Config::new(path.clone()),
        arbiter,
        stats.clone(),
    );
    for reply in replies {
        assert!(reply.blocking_recv().unwrap().is_ok());
    }
    let db = Connection::open(&path).unwrap();
    let order: Vec<String> = db
        .prepare("SELECT value FROM rows ORDER BY id")
        .unwrap()
        .query_map([], |r| r.get(0))
        .unwrap()
        .collect::<rusqlite::Result<_>>()
        .unwrap();
    assert_eq!(order, ["claim", "first", "second", "third"]);
    assert_eq!(stats.snapshot().queue_depth, 0);
}

fn ordered_values(path: &Path) -> Vec<Option<String>> {
    Connection::open(path)
        .unwrap()
        .prepare("SELECT value FROM rows ORDER BY id")
        .unwrap()
        .query_map([], |r| r.get(0))
        .unwrap()
        .collect::<rusqlite::Result<_>>()
        .unwrap()
}

/// Durable work that keeps its own class non-empty at every pick: each command
/// inserts a NULL row, then queues its successor until `stop` is set or a
/// safety cap passes. Batches close on their 50 ms window, so the chain spans
/// many batches.
struct Chain {
    sender: mpsc::Sender<Envelope>,
    arbiter: Arc<Arbiter>,
    stop: Arc<std::sync::atomic::AtomicBool>,
    until: Instant,
}
impl Chain {
    fn envelope(self) -> Envelope {
        self.arbiter.durable(1);
        let arbiter = self.arbiter.clone();
        Envelope {
            command: Box::new(self),
            budget: budget(true),
            reply: None,
            _permits: vec![],
            arbiter,
            queued: Instant::now(),
        }
    }
}
impl Command for Chain {
    fn apply(&self, tx: &dyn WriteTx) -> Result<Json, ApplyError> {
        tx.execute("INSERT INTO rows(value) VALUES(NULL)", &[])?;
        std::thread::sleep(Duration::from_millis(2));
        if !self.stop.load(Ordering::Relaxed) && Instant::now() < self.until {
            let next = Chain {
                sender: self.sender.clone(),
                arbiter: self.arbiter.clone(),
                stop: self.stop.clone(),
                until: self.until,
            };
            let _ = self.sender.try_send(next.envelope());
        }
        Ok(Json::Null)
    }
}

/// Starts a worker thread behind a durable chain with `ordinary` queued after
/// its first command. The chain stops once the returned flag is set.
fn behind_durable_chain(
    path: &Path,
    ordinary: Vec<(&'static str, Budget, Instant)>,
) -> (
    Arc<std::sync::atomic::AtomicBool>,
    Vec<oneshot::Receiver<Answer>>,
    std::thread::JoinHandle<()>,
) {
    let arbiter = Arc::new(Arbiter::default());
    let (sender, receiver) = mpsc::channel(100);
    let stop = Arc::new(std::sync::atomic::AtomicBool::new(false));
    let chain = Chain {
        sender: sender.clone(),
        arbiter: arbiter.clone(),
        stop: stop.clone(),
        until: Instant::now() + Duration::from_secs(15),
    };
    sender
        .try_send(chain.envelope())
        .unwrap_or_else(|_| panic!("queue"));
    let mut replies = Vec::new();
    for (sql, budget, queued) in ordinary {
        let (reply, answer) = oneshot::channel();
        sender
            .try_send(Envelope {
                command: Box::new(Sql(sql)),
                budget,
                reply: Some(reply),
                _permits: vec![],
                arbiter: arbiter.clone(),
                queued,
            })
            .unwrap_or_else(|_| panic!("queue"));
        replies.push(answer);
    }
    drop(sender);
    let db = worker::open(path).unwrap();
    let config = Config::new(path.to_path_buf());
    let handle =
        std::thread::spawn(move || worker::run(db, receiver, config, arbiter, Arc::default()));
    (stop, replies, handle)
}

#[test]
fn starved_ordinary_work_is_shed_at_its_deadline_or_aged_ahead() {
    // Strict durable priority alone would answer neither command while the
    // chain runs: the expired one would hold its permits and the heartbeat
    // would wait out its 10 s deadline. Aging runs it at 5 s instead.
    let path = fixture();
    let start = Instant::now();
    let expired = Budget {
        deadline: start.checked_sub(Duration::from_millis(1)).unwrap(),
        ..budget(false)
    };
    let heartbeat = budget(false);
    let (stop, replies, worker) = behind_durable_chain(
        &path,
        vec![
            (
                "INSERT INTO rows(value) VALUES('expired')",
                expired,
                start.checked_sub(Duration::from_secs(10)).unwrap(),
            ),
            (
                "INSERT INTO rows(value) VALUES('ordinary')",
                heartbeat,
                start,
            ),
        ],
    );
    let mut replies = replies.into_iter();
    let shed = replies.next().unwrap().blocking_recv().unwrap();
    assert_eq!(shed, Err(Failure::timeout(false)));
    assert!(start.elapsed() < Duration::from_secs(1), "answered at once");
    let answered = replies.next().unwrap().blocking_recv().unwrap();
    let waited = start.elapsed();
    // Let the chain run a little past the heartbeat before stopping it.
    std::thread::sleep(Duration::from_millis(200));
    stop.store(true, Ordering::Relaxed);
    worker.join().unwrap();
    assert!(answered.is_ok(), "{answered:?}");
    assert!(
        waited >= Duration::from_secs(5),
        "aged, not drained: {waited:?}"
    );
    let remaining = heartbeat.deadline.saturating_duration_since(start + waited);
    assert!(remaining > Duration::from_secs(4), "{waited:?}");
    let order = ordered_values(&path);
    assert!(!order.contains(&Some("expired".into())));
    let at = order
        .iter()
        .position(|v| v.as_deref() == Some("ordinary"))
        .unwrap();
    assert!(order[..at].iter().filter(|v| v.is_none()).count() > 1);
    assert!(
        order[at + 1..].iter().any(Option::is_none),
        "durable work was still queued when the heartbeat ran"
    );
}

#[test]
fn short_budget_ordinary_work_ages_at_half_its_budget() {
    // A HubRetention sweep has 250 ms and ages at 125 ms. This 1 s budget
    // follows the same half-budget rule with more slack for a loaded runner.
    let path = fixture();
    let start = Instant::now();
    let sweep = Budget {
        deadline: start + Duration::from_secs(1),
        ..budget(false)
    };
    let (stop, replies, worker) = behind_durable_chain(
        &path,
        vec![("INSERT INTO rows(value) VALUES('sweep')", sweep, start)],
    );
    let answered = replies.into_iter().next().unwrap().blocking_recv().unwrap();
    let waited = start.elapsed();
    stop.store(true, Ordering::Relaxed);
    worker.join().unwrap();
    assert!(answered.is_ok(), "{answered:?}");
    assert!(waited >= Duration::from_millis(500), "{waited:?}");
    assert!(ordered_values(&path).contains(&Some("sweep".into())));
}

#[test]
fn deferred_durable_command_keeps_its_place_ahead_of_ordinary_work() {
    // The second claim overflows the first batch's byte budget and is deferred.
    // It must return to the durable head: deferred into the ordinary class, it
    // would run after the third claim.
    let path = fixture();
    let arbiter = Arc::new(Arbiter::default());
    let (sender, receiver) = mpsc::channel(100);
    let mut replies = Vec::new();
    for (value, durable, bytes) in [
        ("one", true, 8 * 1024 * 1024),
        ("two", true, 8 * 1024 * 1024),
        ("three", true, 100),
        ("ordinary", false, 100),
    ] {
        if durable {
            arbiter.durable(1);
        }
        let (reply, answer) = oneshot::channel();
        let sql: &'static str =
            Box::leak(format!("INSERT INTO rows(value) VALUES('{value}')").into_boxed_str());
        sender
            .try_send(Envelope {
                command: Box::new(Sql(sql)),
                budget: Budget {
                    bytes,
                    ..budget(durable)
                },
                reply: Some(reply),
                _permits: vec![],
                arbiter: arbiter.clone(),
                queued: Instant::now(),
            })
            .unwrap_or_else(|_| panic!("queue"));
        replies.push(answer);
    }
    drop(sender);
    worker::run(
        worker::open(&path).unwrap(),
        receiver,
        Config::new(path.clone()),
        arbiter,
        Arc::default(),
    );
    for reply in replies {
        assert!(reply.blocking_recv().unwrap().is_ok());
    }
    let order: Vec<_> = ordered_values(&path).into_iter().flatten().collect();
    assert_eq!(order, ["one", "two", "three", "ordinary"]);
}

#[test]
fn queue_depth_counts_arrivals_during_a_batch() {
    // The first command enqueues four more while it applies, so they reach the
    // channel after the batch began. The second records the gauge while the
    // other three wait in the backlog of the same batch.
    struct Enqueue(std::sync::Mutex<Option<(mpsc::Sender<Envelope>, Vec<Envelope>)>>);
    impl Command for Enqueue {
        fn apply(&self, tx: &dyn WriteTx) -> Result<Json, ApplyError> {
            tx.execute("INSERT INTO rows(value) VALUES('enqueue')", &[])?;
            if let Some((sender, envelopes)) = self.0.lock().unwrap().take() {
                for e in envelopes {
                    sender.try_send(e).unwrap_or_else(|_| panic!("queue"));
                }
            }
            Ok(Json::Null)
        }
    }
    struct Probe(Arc<WriterStats>, Arc<AtomicUsize>);
    impl Command for Probe {
        fn apply(&self, tx: &dyn WriteTx) -> Result<Json, ApplyError> {
            tx.execute("INSERT INTO rows(value) VALUES('probe')", &[])?;
            self.1
                .store(self.0.snapshot().queue_depth, Ordering::Relaxed);
            Ok(Json::Null)
        }
    }
    let path = fixture();
    let arbiter = Arc::new(Arbiter::default());
    let stats = Arc::new(WriterStats::default());
    let seen = Arc::new(AtomicUsize::new(usize::MAX));
    let (sender, receiver) = mpsc::channel(100);
    let mut replies = Vec::new();
    let mut envelope = |command: Box<dyn Command>| {
        let (reply, answer) = oneshot::channel();
        replies.push(answer);
        Envelope {
            command,
            budget: budget(false),
            reply: Some(reply),
            _permits: vec![],
            arbiter: arbiter.clone(),
            queued: Instant::now(),
        }
    };
    let mut later = vec![envelope(Box::new(Probe(stats.clone(), seen.clone())))];
    for n in 0..3 {
        let sql: &'static str =
            Box::leak(format!("INSERT INTO rows(value) VALUES('later-{n}')").into_boxed_str());
        later.push(envelope(Box::new(Sql(sql))));
    }
    let first = envelope(Box::new(Enqueue(std::sync::Mutex::new(Some((
        sender.clone(),
        later,
    ))))));
    sender.try_send(first).unwrap_or_else(|_| panic!("queue"));
    drop(sender);
    worker::run(
        worker::open(&path).unwrap(),
        receiver,
        Config::new(path.clone()),
        arbiter,
        stats.clone(),
    );
    for reply in replies {
        assert!(reply.blocking_recv().unwrap().is_ok());
    }
    assert_eq!(seen.load(Ordering::Relaxed), 3);
    assert_eq!(stats.snapshot().queue_depth, 0);
    assert_eq!(values(&path), 5);
}

#[test]
fn one_pick_sheds_every_expired_ordinary_head() {
    // Aging would also reach an expired head, but only one per batch. Shedding
    // answers the whole expired run at one pick and frees its permits.
    let arbiter = Arc::new(Arbiter::default());
    let stats = Arc::new(WriterStats::default());
    let (sender, mut receiver) = mpsc::channel(100);
    let now = Instant::now();
    let send = |value: &str, durable: bool, deadline: Instant, queued: Instant| {
        if durable {
            arbiter.durable(1);
        }
        let (reply, answer) = oneshot::channel();
        let sql: &'static str =
            Box::leak(format!("INSERT INTO rows(value) VALUES('{value}')").into_boxed_str());
        sender
            .try_send(Envelope {
                command: Box::new(Sql(sql)),
                budget: Budget {
                    deadline,
                    ..budget(durable)
                },
                reply: Some(reply),
                _permits: vec![],
                arbiter: arbiter.clone(),
                queued,
            })
            .unwrap_or_else(|_| panic!("queue"));
        answer
    };
    let past = now.checked_sub(Duration::from_millis(1)).unwrap();
    let long_ago = now.checked_sub(Duration::from_secs(10)).unwrap();
    let mut claim = send("claim", true, now + Duration::from_secs(10), now);
    let mut shed: Vec<_> = (0..50)
        .map(|n| send(&format!("expired-{n}"), false, past, long_ago))
        .collect();
    let mut heartbeat = send("heartbeat", false, now + Duration::from_secs(10), now);
    let mut backlog = worker::Backlog::new(stats.clone());
    let first = backlog.next(&mut receiver).unwrap();
    assert!(first.budget.durable, "the claim, not an aged expired head");
    for answer in &mut shed {
        assert_eq!(answer.try_recv().unwrap(), Err(Failure::timeout(false)));
    }
    assert_eq!(backlog.len(), 1);
    assert_eq!(stats.snapshot().queue_depth, 1);
    assert!(heartbeat.try_recv().is_err() && claim.try_recv().is_err());
}
