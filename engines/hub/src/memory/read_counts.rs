//! Per-card daily read counts (D2 of the memory verification design, #2387).
//!
//! Reads run on the reader pool, so a read never writes. A served `search`
//! or first-chunk `card` read adds to an in-memory accumulator keyed by
//! `(scope, card_id, day)`. The memory writer drains it in small
//! transactions when it has nothing else to do. Only counts are kept: no
//! principal, query, time of day or client address reaches the file, and the
//! per-principal limits below hold only keyed hashes in memory.
//!
//! Every budget either drops the increment or leaves work for a later tick.
//! Nothing here can fail or block a read: the read path takes the
//! accumulator lock with `try_lock` and counts a contended increment as lost.
use rusqlite::{Connection, OptionalExtension, params};
use serde_json::Value as Json;
use std::{
    collections::{BTreeMap, HashMap, HashSet, hash_map::RandomState},
    hash::BuildHasher,
    sync::{
        Mutex,
        atomic::{AtomicU64, Ordering},
    },
    time::{Duration, Instant},
};

pub const DAY_MS: i64 = 86_400_000;
/// Per-day counters saturate here (2^32 - 1).
pub const DAY_MAX: i64 = 4_294_967_295;
/// The writer asks for a flush this often.
pub const FLUSH_INTERVAL_MS: i64 = 60_000;
/// Deletes run in statements of at most this many rows, so the time budget
/// is checked between them.
const SWEEP_CHUNK: usize = 256;
/// How long past its time budget one stalled statement may run before the
/// progress handler stops it, which rolls the whole transaction back. Four
/// budgets, and never under 80 ms, so a small budget on a loaded host stops at
/// a row or chunk boundary instead. The default 20 ms budget stops at 100 ms.
pub(super) fn hard_margin(budget: Duration) -> Duration {
    (budget * 4).max(Duration::from_millis(80))
}

pub const DDL: &str = "
CREATE TABLE IF NOT EXISTS memory_read_days(
  scope TEXT NOT NULL,
  card_id TEXT NOT NULL,
  day INTEGER NOT NULL,
  search_hits INTEGER NOT NULL DEFAULT 0 CHECK(search_hits BETWEEN 0 AND 4294967295),
  card_reads INTEGER NOT NULL DEFAULT 0 CHECK(card_reads BETWEEN 0 AND 4294967295),
  PRIMARY KEY(scope, card_id, day)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS memory_read_days_day ON memory_read_days(day);
CREATE TABLE IF NOT EXISTS memory_read_totals(
  scope TEXT NOT NULL,
  card_id TEXT NOT NULL,
  first_read_day INTEGER NOT NULL,
  last_read_day INTEGER NOT NULL,
  total_reads INTEGER NOT NULL CHECK(total_reads >= 0),
  PRIMARY KEY(scope, card_id)
) WITHOUT ROWID;
";

/// Version 9 to 10. Tolerates a file that already has the tables, as the
/// other additive steps do. `read_counts_since_day` is written once.
pub fn migrate(db: &Connection, now_ms: i64) -> rusqlite::Result<()> {
    db.execute_batch(DDL)?;
    for (key, value) in [
        ("read_counts_since_day", day(now_ms).to_string()),
        ("read_days_rows", "0".into()),
        ("read_totals_rows", "0".into()),
    ] {
        db.execute(
            "INSERT OR IGNORE INTO memory_meta(key,value) VALUES(?1,?2)",
            params![key, value],
        )?;
    }
    Ok(())
}

pub fn day(now_ms: i64) -> i64 {
    now_ms.div_euclid(DAY_MS)
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Limits {
    pub accumulator_keys: usize,
    pub principal_card_daily: u32,
    pub principal_daily: u32,
    pub flush_rows: usize,
    pub flush_ms: u64,
    pub day_rows: i64,
    pub total_rows: i64,
    pub sweep_rows: usize,
    pub sweep_ms: u64,
    pub retention_days: i64,
    /// Principals the daily limiter tracks at once.
    pub principals: usize,
    /// Distinct cards one principal may have counted per day.
    pub principal_pairs: usize,
    /// All limiter entries together. At least `principals * principal_pairs`.
    pub limiter_pairs: usize,
}
impl Default for Limits {
    fn default() -> Self {
        Self {
            accumulator_keys: 65_536,
            principal_card_daily: 64,
            principal_daily: 20_000,
            flush_rows: 2_048,
            flush_ms: 20,
            day_rows: 1_000_000,
            total_rows: 250_000,
            sweep_rows: 2_000,
            sweep_ms: 20,
            retention_days: 35,
            principals: 64,
            principal_pairs: 2_048,
            limiter_pairs: 131_072,
        }
    }
}
impl Limits {
    /// `memory.read_counts`. Unknown keys and out-of-range values fail
    /// configuration. Retention is 7 to 365 days.
    pub fn parse(config: &Json) -> Result<Self, String> {
        let mut limits = Self::default();
        let Some(values) = config.get("read_counts") else {
            return Ok(limits);
        };
        let values = values
            .as_object()
            .ok_or("memory.read_counts must be an object")?;
        for (key, value) in values {
            let (min, max) = match key.as_str() {
                "retention_days" => (7, 365),
                "flush_ms" | "sweep_ms" => (1, 1_000),
                "flush_rows" | "sweep_rows" => (1, 100_000),
                "accumulator_keys" => (1, 1_048_576),
                "principal_card_daily" | "principal_daily" => (1, 1_000_000),
                "day_rows" | "total_rows" => (1, 100_000_000),
                "principals" => (1, 4_096),
                "principal_pairs" => (1, 1_000_000),
                "limiter_pairs" => (1, 16_777_216),
                _ => return Err(format!("unknown memory.read_counts key {key:?}")),
            };
            let n = value
                .as_i64()
                .filter(|n| (min..=max).contains(n))
                .ok_or_else(|| {
                    format!("memory.read_counts.{key} must be an integer from {min} to {max}")
                })?;
            match key.as_str() {
                "retention_days" => limits.retention_days = n,
                "flush_ms" => limits.flush_ms = n as u64,
                "sweep_ms" => limits.sweep_ms = n as u64,
                "flush_rows" => limits.flush_rows = n as usize,
                "sweep_rows" => limits.sweep_rows = n as usize,
                "accumulator_keys" => limits.accumulator_keys = n as usize,
                "principal_card_daily" => limits.principal_card_daily = n as u32,
                "principal_daily" => limits.principal_daily = n as u32,
                "day_rows" => limits.day_rows = n,
                "principals" => limits.principals = n as usize,
                "principal_pairs" => limits.principal_pairs = n as usize,
                "limiter_pairs" => limits.limiter_pairs = n as usize,
                _ => limits.total_rows = n,
            }
        }
        // Every tracked principal must be able to use its whole share, so one
        // principal can never fill the limiter for the others.
        if limits.limiter_pairs < limits.principals.saturating_mul(limits.principal_pairs) {
            return Err(format!(
                "memory.read_counts.limiter_pairs ({}) must be at least principals ({}) x principal_pairs ({})",
                limits.limiter_pairs, limits.principals, limits.principal_pairs
            ));
        }
        Ok(limits)
    }
    /// A tracked principal with fewer distinct cards than this is a light
    /// user. It is never denied, and leaves the table only when every tracked
    /// principal is light.
    pub fn light_pairs(&self) -> usize {
        (self.principal_pairs / 16).max(1)
    }
    /// Light principals that may be evicted per day before newcomers go
    /// uncounted: sixteen per table slot.
    pub fn carried_principals(&self) -> usize {
        self.principals.saturating_mul(16)
    }
}

/// Which counter a read adds to.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    SearchHit,
    CardRead,
}

/// The budgets that can bite, each one bounded telemetry label.
/// `write_waiting` counts flushes and sweeps that rolled back to let a
/// queued write go first. `principal_evicted` counts evictions of the
/// heaviest principal and `evicted_reads` the reads it made afterwards.
/// `light_evicted` counts light principals moved out to make room.
pub const BUDGETS: [&str; 17] = [
    "accumulator_keys",
    "principal_card_daily",
    "principal_daily",
    "principal_pairs",
    "limiter_pairs",
    "principals_full",
    "principal_evicted",
    "light_evicted",
    "evicted_reads",
    "contended",
    "flush_rows",
    "flush_ms",
    "day_rows",
    "total_rows",
    "sweep_rows",
    "sweep_ms",
    "write_waiting",
];
fn budget(name: &str) -> usize {
    BUDGETS.iter().position(|b| *b == name).unwrap()
}

#[derive(Default)]
pub struct Stats {
    budgets: [AtomicU64; BUDGETS.len()],
    /// Increments lost to a full accumulator, a full limiter or principal
    /// table, lock contention, the day-row cap or a failed flush. Reads held
    /// back by a principal's own limits are policy, not loss, and appear only
    /// under their budget label.
    dropped: AtomicU64,
    flushed: AtomicU64,
    swept: AtomicU64,
}
impl Stats {
    fn bump(&self, name: &str) {
        self.budgets[budget(name)].fetch_add(1, Ordering::Relaxed);
    }
    fn drop_increments(&self, n: u64) {
        self.dropped.fetch_add(n, Ordering::Relaxed);
    }
    pub fn budget(&self, name: &str) -> u64 {
        self.budgets[budget(name)].load(Ordering::Relaxed)
    }
    pub fn dropped(&self) -> u64 {
        self.dropped.load(Ordering::Relaxed)
    }
    pub fn flushed(&self) -> u64 {
        self.flushed.load(Ordering::Relaxed)
    }
    pub fn swept(&self) -> u64 {
        self.swept.load(Ordering::Relaxed)
    }
}

#[derive(Clone, Debug, Hash, PartialEq, Eq)]
pub struct Key {
    pub scope: String,
    pub card_id: String,
    pub day: i64,
}
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Tally {
    pub search_hits: u32,
    pub card_reads: u32,
}
impl Tally {
    fn add(&mut self, other: Tally) {
        self.search_hits = self.search_hits.saturating_add(other.search_hits);
        self.card_reads = self.card_reads.saturating_add(other.card_reads);
    }
    fn total(self) -> u64 {
        u64::from(self.search_hits) + u64::from(self.card_reads)
    }
}
struct Slot {
    seq: i64,
    tally: Tally,
}

/// One principal's daily usage, keyed by hashes only.
#[derive(Default)]
struct Usage {
    total: u32,
    pairs: HashMap<u64, u32>,
    /// The `State::tick` of its last counted or limited read.
    last: u64,
}

struct State {
    limits: Limits,
    /// The UTC day the per-principal limits belong to.
    limiter_day: i64,
    principals: HashMap<u64, Usage>,
    /// Principals evicted today. Their reads are served, not counted, until
    /// the day resets, so eviction never refills a heavy principal's budget.
    evicted: HashSet<u64>,
    /// Light principals evicted today to make room, with the daily total they
    /// had used. Each may be evicted this way once a day, and its total comes
    /// back when it is tracked again.
    carried: HashMap<u64, u32>,
    /// Pair entries across every tracked principal.
    pair_entries: usize,
    /// Activity clock for least-recently-active eviction.
    tick: u64,
    keys: HashMap<Key, Slot>,
    /// Oldest first. Holds exactly one entry per key in `keys`.
    order: BTreeMap<i64, Key>,
    /// Next sequence at the back (counts up) and at the front (counts down).
    next: i64,
    front: i64,
}
impl State {
    fn insert_back(&mut self, key: Key, tally: Tally) {
        let seq = self.next;
        self.next += 1;
        self.order.insert(seq, key.clone());
        self.keys.insert(key, Slot { seq, tally });
    }
    fn insert_front(&mut self, key: Key, tally: Tally) {
        let seq = self.front;
        self.front -= 1;
        self.order.insert(seq, key.clone());
        self.keys.insert(key, Slot { seq, tally });
    }
    /// Starts tracking `who`, making room when the table is full. False when
    /// there is no room: the read is served, not counted.
    ///
    /// If any tracked principal holds at least `light_pairs` distinct cards,
    /// the heaviest is evicted and denied for the rest of the day. If all are
    /// light, the least recently active one not already evicted today leaves
    /// without being denied. Its daily total is carried, so it is tracked
    /// again on its next read with its daily limit intact.
    fn track(&mut self, who: u64, stats: &Stats) -> bool {
        if self.principals.contains_key(&who) {
            return true;
        }
        if self.principals.len() >= self.limits.principals && !self.make_room(stats) {
            stats.bump("principals_full");
            return false;
        }
        let total = self.carried.get(&who).copied().unwrap_or(0);
        self.principals.insert(
            who,
            Usage {
                total,
                ..Usage::default()
            },
        );
        true
    }
    fn make_room(&mut self, stats: &Stats) -> bool {
        let light = self.limits.light_pairs();
        let heaviest = self
            .principals
            .iter()
            .map(|(id, usage)| (usage.pairs.len(), usage.total, *id))
            .max();
        if let Some((pairs, _, id)) = heaviest.filter(|h| h.0 >= light) {
            if self.evicted.len() >= self.limits.principals {
                return false;
            }
            self.principals.remove(&id);
            self.pair_entries -= pairs;
            self.evicted.insert(id);
            stats.bump("principal_evicted");
            return true;
        }
        if self.carried.len() >= self.limits.carried_principals() {
            return false;
        }
        let idle = self
            .principals
            .iter()
            .filter(|(id, _)| !self.carried.contains_key(id))
            .min_by_key(|(id, usage)| (usage.last, **id))
            .map(|(id, _)| *id);
        let Some(id) = idle else {
            return false;
        };
        let usage = self.principals.remove(&id).unwrap();
        self.pair_entries -= usage.pairs.len();
        self.carried.insert(id, usage.total);
        stats.bump("light_evicted");
        true
    }
}

pub struct ReadCounts {
    state: Mutex<State>,
    hasher: RandomState,
    pub stats: Stats,
}
impl Default for ReadCounts {
    fn default() -> Self {
        Self::new(Limits::default())
    }
}
impl ReadCounts {
    pub fn new(limits: Limits) -> Self {
        Self {
            state: Mutex::new(State {
                limits,
                limiter_day: i64::MIN,
                principals: HashMap::new(),
                evicted: HashSet::new(),
                carried: HashMap::new(),
                pair_entries: 0,
                tick: 0,
                keys: HashMap::new(),
                order: BTreeMap::new(),
                next: 0,
                front: -1,
            }),
            hasher: RandomState::new(),
            stats: Stats::default(),
        }
    }
    pub fn configure(&self, limits: Limits) {
        self.state.lock().unwrap().limits = limits;
    }
    pub fn limits(&self) -> Limits {
        self.state.lock().unwrap().limits.clone()
    }
    /// Holds the accumulator lock, so a test can show a read never waits on it.
    #[cfg(test)]
    pub(super) fn hold(&self) -> impl Drop + '_ {
        self.state.lock().unwrap()
    }
    /// Ordering entries, which always match the keys one for one.
    #[cfg(test)]
    pub(super) fn order_len(&self) -> usize {
        self.state.lock().unwrap().order.len()
    }
    /// Keys waiting for a flush.
    pub fn len(&self) -> usize {
        self.state.lock().unwrap().keys.len()
    }
    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }
    /// Over three quarters of the key budget: ask the writer for a flush now
    /// instead of at the next minute. The flush itself still waits for an
    /// idle writer.
    pub fn pressed(&self) -> bool {
        let state = self.state.lock().unwrap();
        state.keys.len().saturating_mul(4) > state.limits.accumulator_keys.saturating_mul(3)
    }

    /// Records the cards one served search reply returned. Hits without a
    /// scope (a prompt hit blocked by the scanner) are not cards and are
    /// skipped.
    pub fn search(&self, principal: &str, matches: &Json, now_ms: i64) {
        let Some(matches) = matches.as_array() else {
            return;
        };
        let cards: Vec<(&str, &str)> = matches
            .iter()
            .filter_map(|m| Some((m.get("scope")?.as_str()?, m.get("card_id")?.as_str()?)))
            .collect();
        self.record(principal, &cards, Kind::SearchHit, now_ms);
    }

    /// Adds one increment per card. Never blocks: a contended lock loses the
    /// increments and counts them.
    pub fn record(&self, principal: &str, cards: &[(&str, &str)], kind: Kind, now_ms: i64) {
        if cards.is_empty() {
            return;
        }
        let Ok(mut state) = self.state.try_lock() else {
            self.stats.bump("contended");
            self.stats.drop_increments(cards.len() as u64);
            return;
        };
        let state = &mut *state;
        let today = day(now_ms);
        if state.limiter_day != today {
            state.limiter_day = today;
            state.principals.clear();
            state.evicted.clear();
            state.carried.clear();
            state.pair_entries = 0;
        }
        let who = self.hasher.hash_one(principal);
        state.tick += 1;
        let tick = state.tick;
        let tally = match kind {
            Kind::SearchHit => Tally {
                search_hits: 1,
                card_reads: 0,
            },
            Kind::CardRead => Tally {
                search_hits: 0,
                card_reads: 1,
            },
        };
        for &(scope, card_id) in cards {
            let key = Key {
                scope: scope.into(),
                card_id: card_id.into(),
                day: today,
            };
            if !state.keys.contains_key(&key) && state.keys.len() >= state.limits.accumulator_keys {
                self.stats.bump("accumulator_keys");
                self.stats.drop_increments(1);
                continue;
            }
            if state.evicted.contains(&who) {
                self.stats.bump("evicted_reads");
                continue;
            }
            if !state.track(who, &self.stats) {
                self.stats.drop_increments(1);
                continue;
            }
            let limits = &state.limits;
            let usage = state.principals.get_mut(&who).unwrap();
            usage.last = tick;
            if usage.total >= limits.principal_daily {
                self.stats.bump("principal_daily");
                continue;
            }
            let pair = self.hasher.hash_one((principal, scope, card_id));
            let used = usage.pairs.get(&pair).copied();
            if used.is_none() {
                if usage.pairs.len() >= limits.principal_pairs {
                    self.stats.bump("principal_pairs");
                    continue;
                }
                if state.pair_entries >= limits.limiter_pairs {
                    self.stats.bump("limiter_pairs");
                    self.stats.drop_increments(1);
                    continue;
                }
                state.pair_entries += 1;
            }
            if used.unwrap_or(0) >= limits.principal_card_daily {
                self.stats.bump("principal_card_daily");
                continue;
            }
            *usage.pairs.entry(pair).or_default() += 1;
            usage.total += 1;
            if let Some(slot) = state.keys.get_mut(&key) {
                slot.tally.add(tally);
            } else {
                state.insert_back(key, tally);
            }
        }
    }

    /// Removes up to `max` of the oldest keys for a flush.
    pub(super) fn take(&self, max: usize) -> (Limits, Vec<(Key, Tally)>) {
        let mut state = self.state.lock().unwrap();
        let mut taken = Vec::new();
        while taken.len() < max {
            let Some((_, key)) = state.order.pop_first() else {
                break;
            };
            let slot = state.keys.remove(&key).expect("order matches keys");
            taken.push((key, slot.tally));
        }
        (state.limits.clone(), taken)
    }

    /// Puts keys a flush did not commit back at the front, oldest first, so
    /// the next flush takes them before anything newer. A key recorded again
    /// meanwhile merges into one slot. A key with no room is lost and counted.
    pub(super) fn restore(&self, rows: &[(Key, Tally)]) {
        let mut state = self.state.lock().unwrap();
        for (key, tally) in rows.iter().rev() {
            let mut tally = *tally;
            // A reread made a new slot meanwhile: merge it and drop its
            // ordering entry, so `order` never outgrows `keys`.
            if let Some(slot) = state.keys.remove(key) {
                state.order.remove(&slot.seq);
                tally.add(slot.tally);
            } else if state.keys.len() >= state.limits.accumulator_keys {
                self.stats.bump("accumulator_keys");
                self.stats.drop_increments(tally.total());
                continue;
            }
            state.insert_front(key.clone(), tally);
        }
    }
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Flush {
    /// Accumulator keys committed.
    pub written: usize,
    /// A row or time budget stopped this flush with keys still waiting.
    pub more: bool,
}

fn meta(db: &Connection, key: &str) -> rusqlite::Result<i64> {
    Ok(db
        .query_row("SELECT value FROM memory_meta WHERE key=?1", [key], |r| {
            r.get::<_, String>(0)
        })
        .optional()?
        .and_then(|v| v.parse().ok())
        .unwrap_or(0)
        .max(0))
}
fn set_meta(db: &Connection, key: &str, value: i64) -> rusqlite::Result<()> {
    db.execute(
        "INSERT INTO memory_meta(key,value) VALUES(?1,?2) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        params![key, value.max(0).to_string()],
    )?;
    Ok(())
}

/// Ends a transaction that did not commit. SQLite can leave it open after a
/// failed COMMIT, so roll back whatever state the connection is in.
fn abandon(db: &Connection) {
    let _ = db.progress_handler(0, None::<fn() -> bool>);
    if !db.is_autocommit() {
        let _ = db.execute_batch("ROLLBACK");
    }
}

/// One bounded flush transaction on the memory writer's connection: at most
/// `flush_rows` of the oldest keys, stopping at a row boundary once
/// `flush_ms` has passed. Keys it did not commit go back to the accumulator.
///
/// `waiting` reports queued writer work. It is checked before the lock and at
/// every row boundary. When it turns true the flush rolls back, returns every
/// key and yields, so a write never waits behind a flush. Rolling back costs
/// no sync; committing would.
pub fn flush_once(
    db: &Connection,
    counts: &ReadCounts,
    waiting: &dyn Fn() -> bool,
) -> rusqlite::Result<Flush> {
    if waiting() {
        return Ok(Flush {
            written: 0,
            more: true,
        });
    }
    let max = counts.limits().flush_rows;
    let (limits, batch) = counts.take(max);
    if batch.is_empty() {
        return Ok(Flush::default());
    }
    if let Err(e) = crate::writer::worker::begin(db, Instant::now() + Duration::from_millis(250)) {
        counts.restore(&batch);
        return Err(e);
    }
    let budget = Duration::from_millis(limits.flush_ms);
    let deadline = Instant::now() + budget;
    // The row-boundary check below is the budget. This only stops a single
    // statement that stalls, which rolls the whole transaction back.
    let hard = deadline + hard_margin(budget);
    let _ = db.progress_handler(1000, Some(move || Instant::now() >= hard));
    let mut written = 0;
    let mut timed_out = false;
    let mut yielded = false;
    let applied = (|| -> rusqlite::Result<()> {
        let mut day_rows = meta(db, "read_days_rows")?;
        let mut total_rows = meta(db, "read_totals_rows")?;
        let (start_days, start_totals) = (day_rows, total_rows);
        for (key, tally) in &batch {
            if waiting() {
                yielded = true;
                return Ok(());
            }
            if written > 0 && Instant::now() >= deadline {
                timed_out = true;
                break;
            }
            let (search, reads) = (i64::from(tally.search_hits), i64::from(tally.card_reads));
            let updated = db.execute(
                "UPDATE memory_read_days SET search_hits=MIN(search_hits+?4,?6),card_reads=MIN(card_reads+?5,?6) WHERE scope=?1 AND card_id=?2 AND day=?3",
                params![key.scope, key.card_id, key.day, search, reads, DAY_MAX],
            )?;
            if updated == 0 {
                if day_rows < limits.day_rows {
                    db.execute(
                        "INSERT INTO memory_read_days(scope,card_id,day,search_hits,card_reads) VALUES(?1,?2,?3,?4,?5)",
                        params![key.scope, key.card_id, key.day, search, reads],
                    )?;
                    day_rows += 1;
                } else {
                    counts.stats.bump("day_rows");
                    counts.stats.drop_increments(tally.total());
                }
            }
            let total = search + reads;
            let updated = db.execute(
                "UPDATE memory_read_totals SET first_read_day=MIN(first_read_day,?3),last_read_day=MAX(last_read_day,?3),total_reads=CASE WHEN total_reads>9223372036854775807-?4 THEN 9223372036854775807 ELSE total_reads+?4 END WHERE scope=?1 AND card_id=?2",
                params![key.scope, key.card_id, key.day, total],
            )?;
            if updated == 0 {
                if total_rows < limits.total_rows {
                    db.execute(
                        "INSERT INTO memory_read_totals(scope,card_id,first_read_day,last_read_day,total_reads) VALUES(?1,?2,?3,?3,?4)",
                        params![key.scope, key.card_id, key.day, total],
                    )?;
                    total_rows += 1;
                } else {
                    counts.stats.bump("total_rows");
                }
            }
            written += 1;
        }
        if day_rows != start_days {
            set_meta(db, "read_days_rows", day_rows)?;
        }
        if total_rows != start_totals {
            set_meta(db, "read_totals_rows", total_rows)?;
        }
        Ok(())
    })();
    let _ = db.progress_handler(0, None::<fn() -> bool>);
    if applied.is_ok() && yielded {
        abandon(db);
        counts.restore(&batch);
        counts.stats.bump("write_waiting");
        return Ok(Flush {
            written: 0,
            more: true,
        });
    }
    let committed = applied.and_then(|()| db.execute_batch("COMMIT"));
    if let Err(e) = committed {
        abandon(db);
        counts.restore(&batch);
        return Err(e);
    }
    counts.restore(&batch[written..]);
    counts
        .stats
        .flushed
        .fetch_add(written as u64, Ordering::Relaxed);
    if timed_out {
        counts.stats.bump("flush_ms");
    }
    let full = batch.len() == max && !counts.is_empty();
    if full && !timed_out {
        counts.stats.bump("flush_rows");
    }
    Ok(Flush {
        written,
        more: timed_out || full,
    })
}

/// Whether the writer flushes now: only when asked and no write waits, however
/// full the accumulator is. Under pressure new keys are dropped and counted.
pub fn due(requested: bool, writes_waiting: bool) -> bool {
    requested && !writes_waiting
}

/// Where the totals scan for retired cards resumes. Kept by the writer.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct SweepCursor(pub Option<(String, String)>);

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Swept {
    pub days: usize,
    pub totals: usize,
    pub examined: usize,
    /// The time budget stopped this sweep, or either stage used the whole
    /// row budget, so rows may remain and the writer should sweep again.
    pub more: bool,
}

/// One bounded retention transaction: expired day rows oldest first, then
/// the totals rows of retired or vanished cards, resuming from `cursor`.
/// Day rows and examined totals rows together stay within `sweep_rows`.
/// Like a flush, it rolls back and yields when `waiting` turns true. Its
/// deletes are idempotent, so the next tick redoes them.
pub fn sweep(
    db: &Connection,
    counts: &ReadCounts,
    now_ms: i64,
    cursor: &mut SweepCursor,
    waiting: &dyn Fn() -> bool,
) -> rusqlite::Result<Swept> {
    let limits = counts.limits();
    let yielded = Swept {
        more: true,
        ..Swept::default()
    };
    if waiting() {
        return Ok(yielded);
    }
    crate::writer::worker::begin(db, Instant::now() + Duration::from_millis(250))?;
    let budget = Duration::from_millis(limits.sweep_ms);
    let deadline = Instant::now() + budget;
    let hard = deadline + hard_margin(budget);
    let _ = db.progress_handler(1000, Some(move || Instant::now() >= hard));
    let mut swept = Swept::default();
    let mut next_cursor = cursor.clone();
    let mut timed_out = false;
    let mut gave_way = false;
    let cutoff = day(now_ms) - limits.retention_days;
    let applied = (|| -> rusqlite::Result<()> {
        while swept.days < limits.sweep_rows {
            if waiting() {
                gave_way = true;
                return Ok(());
            }
            if Instant::now() >= deadline {
                timed_out = true;
                return Ok(());
            }
            let chunk = SWEEP_CHUNK.min(limits.sweep_rows - swept.days);
            let n = db.execute(
                "DELETE FROM memory_read_days WHERE (scope,card_id,day) IN (SELECT scope,card_id,day FROM memory_read_days WHERE day<=?1 ORDER BY day LIMIT ?2)",
                params![cutoff, chunk as i64],
            )?;
            swept.days += n;
            if n < chunk {
                break;
            }
        }
        // Totals of retired or vanished cards, in pages from the cursor. Every
        // row boundary checks for a queued job (roll back and yield) and the
        // time budget (commit what is done, resume next tick). At least one row
        // moves per transaction, so a small budget still makes progress.
        let mut room = limits.sweep_rows - swept.days;
        while room > 0 {
            let page = SWEEP_CHUNK.min(room);
            let (scope, card) = next_cursor.0.clone().unwrap_or_default();
            let mut statement = db.prepare_cached(
                "SELECT t.scope,t.card_id,c.state IS NOT 'live' FROM memory_read_totals t LEFT JOIN memory_cards c ON c.scope=t.scope AND c.card_id=t.card_id WHERE (t.scope,t.card_id)>(?1,?2) ORDER BY t.scope,t.card_id LIMIT ?3",
            )?;
            let rows = statement
                .query_map(params![scope, card, page as i64], |r| {
                    Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?, r.get::<_, bool>(2)?))
                })?
                .collect::<rusqlite::Result<Vec<_>>>()?;
            for (scope, card_id, gone) in &rows {
                if waiting() {
                    gave_way = true;
                    return Ok(());
                }
                if swept.days + swept.examined > 0 && Instant::now() >= deadline {
                    timed_out = true;
                    return Ok(());
                }
                if *gone {
                    swept.totals += db.execute(
                        "DELETE FROM memory_read_totals WHERE scope=?1 AND card_id=?2",
                        params![scope, card_id],
                    )?;
                }
                swept.examined += 1;
                next_cursor.0 = Some((scope.clone(), card_id.clone()));
            }
            room -= rows.len();
            // A short page reached the end of the table: start over next time.
            if rows.len() < page {
                next_cursor.0 = None;
                break;
            }
        }
        Ok(())
    })()
    .and_then(|()| {
        if swept.days > 0 {
            set_meta(db, "read_days_rows", meta(db, "read_days_rows")? - swept.days as i64)?;
        }
        if swept.totals > 0 {
            set_meta(db, "read_totals_rows", meta(db, "read_totals_rows")? - swept.totals as i64)?;
        }
        Ok(())
    });
    let _ = db.progress_handler(0, None::<fn() -> bool>);
    if applied.is_ok() && gave_way {
        abandon(db);
        counts.stats.bump("write_waiting");
        return Ok(yielded);
    }
    if let Err(e) = applied.and_then(|()| db.execute_batch("COMMIT")) {
        abandon(db);
        return Err(e);
    }
    *cursor = next_cursor;
    // Either stage used the whole row budget, so rows may remain. The writer
    // queues the next sweep at once. A sweep that finds nothing left wraps the
    // cursor and reports false.
    let full = swept.days == limits.sweep_rows
        || (swept.days + swept.examined == limits.sweep_rows && cursor.0.is_some());
    if timed_out {
        counts.stats.bump("sweep_ms");
    } else if full {
        counts.stats.bump("sweep_rows");
    }
    counts
        .stats
        .swept
        .fetch_add((swept.days + swept.totals) as u64, Ordering::Relaxed);
    swept.more = timed_out || full;
    Ok(swept)
}

/// Bounded labels for `/metrics`. No scope, card id or principal.
pub fn render(counts: &ReadCounts) -> String {
    let mut text = String::from(
        "# HELP brigade_hub_memory_read_count_budget_total Times a read-count budget bit (counter).\n",
    );
    for name in BUDGETS {
        text += &format!(
            "brigade_hub_memory_read_count_budget_total{{budget=\"{name}\"}} {}\n",
            counts.stats.budget(name)
        );
    }
    text += &format!(
        "# HELP brigade_hub_memory_read_count_dropped_total Read increments lost under pressure (counter).\nbrigade_hub_memory_read_count_dropped_total {}\nbrigade_hub_memory_read_count_flushed_total {}\nbrigade_hub_memory_read_count_swept_total {}\nbrigade_hub_memory_read_count_pending_keys {}\n",
        counts.stats.dropped(),
        counts.stats.flushed(),
        counts.stats.swept(),
        counts.len()
    );
    text
}
