use super::*;

#[test]
fn the_sweep_stops_at_its_time_budget() {
    let counts = ReadCounts::new(limits(|l| {
        l.sweep_ms = 3_600_000;
        l.sweep_rows = 100_000;
    }));
    let now = 1_800_000_000_000;
    let db = memory_db();
    seed_days(&db, &[day(now) - 100], 768);
    let mut cursor = SweepCursor::default();
    for tick in 1..=3 {
        super::super::read_counts::sweep_clock::expire_after_one_chunk();
        let result = sweep(&db, &counts, now, &mut cursor, &idle).unwrap();
        assert_eq!((result.days, result.more), (256, true));
        assert_eq!(counts.stats.budget("sweep_ms"), tick);
    }
    super::super::read_counts::sweep_clock::expire_after_one_chunk();
    let last = sweep(&db, &counts, now, &mut cursor, &idle).unwrap();
    assert_eq!((last.days, last.more), (0, false));
    assert_eq!(count(&db, "SELECT count(*) FROM memory_read_days"), 0);
    assert_eq!(meta(&db, "read_days_rows"), Some("0".into()));
}
