use brigade_hub::{read::ReaderPool, schema};
use rusqlite::Connection;
use std::{process::Command, time::Duration};

#[test]
fn python_lockstep_and_both_sqlite_builds() {
    let dir = std::env::temp_dir().join(format!("hub-schema-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let status = Command::new("python3")
        .arg(concat!(env!("CARGO_MANIFEST_DIR"), "/schema/lockstep.py"))
        .arg(&dir)
        .status()
        .unwrap();
    assert!(status.success());
    for name in ["fresh", "user-version-22", "user-version-34"] {
        if name != "fresh" {
            let raw = Connection::open(dir.join(format!("{name}.raw.db"))).unwrap();
            assert_eq!(
                raw.query_row("PRAGMA quick_check", [], |r| r.get::<_, String>(0))
                    .unwrap(),
                "ok"
            );
        }
        let path = dir.join(format!("{name}.db"));
        let conn = Connection::open(&path).unwrap();
        schema::check(&conn).unwrap();
        conn.execute_batch("DROP TRIGGER fleet_questions_reply")
            .unwrap();
        assert!(schema::check(&conn).is_err());
    }
    std::fs::remove_dir_all(dir).unwrap();
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn reader_interrupts_expensive_sql_and_recovers_slot() {
    let path = std::env::temp_dir().join(format!("hub-read-{}.db", std::process::id()));
    Connection::open(&path).unwrap();
    let pool = std::sync::Arc::new(
        ReaderPool::open_unchecked(&path, 2, 4, Duration::from_millis(500)).unwrap(),
    );
    let (started, starting) = std::sync::mpsc::channel();
    let p = pool.clone();
    let slow = tokio::spawn(async move {
        p.submit(move |c| {
        started.send(()).unwrap();
        c.query_row("WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<100000000) SELECT sum(x) FROM n", [], |r|r.get::<_,i64>(0))
    }).await
    });
    starting.recv_timeout(Duration::from_secs(2)).unwrap();
    let fast = tokio::time::timeout(
        Duration::from_millis(250),
        pool.submit(|c| c.query_row("SELECT 42", [], |r| r.get::<_, i64>(0))),
    )
    .await
    .unwrap();
    assert_eq!(fast.unwrap(), 42);
    assert!(!slow.is_finished(), "fast reader waited for slow reader");
    assert!(slow.await.unwrap().is_err());
    assert_eq!(
        pool.submit(|c| c.query_row("SELECT 7", [], |r| r.get::<_, i64>(0)))
            .await
            .unwrap(),
        7
    );
    drop(pool);
    std::fs::remove_file(path).unwrap();
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn cpu_queue_is_bounded_and_worker_recovers() {
    use brigade_hub::read::CpuPool;
    use std::sync::{Arc, mpsc};
    let pool = Arc::new(CpuPool::new(1, 1));
    let (started, starting) = mpsc::channel();
    let (release, waiting) = mpsc::channel();
    let p = pool.clone();
    let first = tokio::spawn(async move {
        p.submit(move || {
            started.send(()).unwrap();
            waiting.recv().unwrap();
            1
        })
        .await
    });
    starting.recv_timeout(Duration::from_secs(2)).unwrap();
    let second = pool.submit(|| 2);
    tokio::pin!(second);
    tokio::select! { biased; result=&mut second => panic!("queued job ran early: {result:?}"), _=tokio::task::yield_now()=>{} }
    assert_eq!(pool.submit(|| 3).await, Err("cpu-queue-full"));
    release.send(()).unwrap();
    assert_eq!(first.await.unwrap(), Ok(1));
    assert_eq!(second.await, Ok(2));
}
