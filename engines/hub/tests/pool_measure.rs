//! Opt-in measurement, excluded from routine PR CI because timings depend on host load.
use brigade_hub::read::ReaderPool;
use rusqlite::Connection;
use std::time::{Duration, Instant};
fn rss() -> u64 {
    std::fs::read_to_string("/proc/self/status")
        .unwrap()
        .lines()
        .find(|s| s.starts_with("VmRSS:"))
        .unwrap()
        .split_whitespace()
        .nth(1)
        .unwrap()
        .parse()
        .unwrap()
}
#[tokio::test]
#[ignore = "host-dependent latency and Linux resident-memory measurement"]
async fn pool_defaults_measurement() {
    let threads: usize = std::env::var("HUB_MEASURE_THREADS")
        .unwrap_or("1".into())
        .parse()
        .unwrap();
    let path = std::env::temp_dir().join(format!("hub-measure-{}.db", std::process::id()));
    let c = Connection::open(&path).unwrap();
    c.execute_batch("PRAGMA journal_mode=WAL; CREATE TABLE tokens(digest TEXT PRIMARY KEY, active INTEGER); INSERT INTO tokens VALUES('fake',1)").unwrap();
    let before = rss();
    let pool = ReaderPool::open_unchecked(&path, threads, 1024, Duration::from_secs(2)).unwrap();
    let at = Instant::now();
    for _ in 0..10000 {
        assert_eq!(
            pool.submit(|c| c
                .prepare_cached("SELECT active FROM tokens WHERE digest=?")?
                .query_row(["fake"], |r| r.get::<_, i64>(0)))
                .await
                .unwrap(),
            1
        );
    }
    println!(
        "threads={threads} query_mean_us={:.3} rss_before_kib={before} rss_after_kib={}",
        at.elapsed().as_secs_f64() * 1e6 / 10000.0,
        rss()
    );
    drop(pool);
    drop(c);
    std::fs::remove_file(path).unwrap();
}
