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
