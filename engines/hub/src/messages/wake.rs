//! Long-poll wake epochs and waiter caps ([Long-poll] in the v2 plan).
//!
//! A waiter marks its recipient's epoch before every read and waits only
//! while the epoch still equals that mark, so a send that commits between an
//! empty read and the wait ends the wait at once. Native commits bump the
//! epoch in `post_commit`. Commits by other processes are found by a watch
//! thread on its own read-only connection, which samples `PRAGMA
//! data_version` while anyone waits or is about to. That connection is not the
//! writer's, so a native commit made while anyone waits also moves its
//! `data_version`: the watcher then runs its `GROUP BY recipient` query and
//! notifies those recipients a second time. The extra notify is harmless, as a
//! waiter re-reads and waits again when it finds nothing new.
use std::{
    collections::HashMap,
    path::Path,
    sync::{
        Arc, Mutex,
        atomic::{AtomicUsize, Ordering},
    },
    time::Duration,
};
use tokio::sync::Notify;

/// Waiter caps from the admission table, default profile.
#[derive(Clone, Copy, Debug)]
pub struct Limits {
    pub total: usize,
    pub per_principal: usize,
    pub per_source: usize,
}
impl Limits {
    /// 3,072 waiters with 1,024 connections kept for other requests at the
    /// default 4,096 connections, scaled down for smaller connection caps.
    /// `total` is the whole waiter ceiling: open connections that do not wait,
    /// such as idle keep-alives, never count against it.
    pub fn for_connections(max_connections: usize) -> Self {
        let kept = (max_connections / 4).min(1024);
        Self {
            total: (max_connections - kept).min(3072),
            per_principal: 2,
            per_source: 64,
        }
    }
}
#[derive(Default)]
struct Slot {
    epoch: u64,
    waiters: usize,
    notify: Arc<Notify>,
}
#[derive(Default)]
struct State {
    recipients: HashMap<String, Slot>,
    sources: HashMap<String, usize>,
}
pub struct Wake {
    state: Mutex<State>,
    waiting: AtomicUsize,
    /// Polls between their first mark and their waiter registration.
    reading: AtomicUsize,
    /// Watch-thread iterations, for tests that order a commit against a tick.
    ticks: AtomicUsize,
    limits: Limits,
}
impl Wake {
    pub fn new(limits: Limits) -> Self {
        Self {
            state: Mutex::default(),
            waiting: AtomicUsize::new(0),
            reading: AtomicUsize::new(0),
            ticks: AtomicUsize::new(0),
            limits,
        }
    }
    /// Count a poll that may wait as watched before it takes its first mark,
    /// so the watcher cannot absorb a commit that lands between that poll's
    /// first snapshot and its registration (plan step 1). Hold the guard
    /// until the poll has registered its waiter or returned.
    pub fn first_read(self: &Arc<Self>) -> FirstRead {
        self.reading.fetch_add(1, Ordering::SeqCst);
        FirstRead(self.clone())
    }
    /// Waiters plus polls still in their first read.
    fn watched(&self) -> usize {
        self.waiting.load(Ordering::SeqCst) + self.reading.load(Ordering::SeqCst)
    }
    pub fn ticks(&self) -> usize {
        self.ticks.load(Ordering::SeqCst)
    }
    pub fn epoch(&self, recipient: &str) -> u64 {
        self.state
            .lock()
            .unwrap()
            .recipients
            .get(recipient)
            .map_or(0, |s| s.epoch)
    }
    /// `_notify_recipient`: move the epoch and wake every waiter.
    pub fn notify(&self, recipient: &str) {
        let mut state = self.state.lock().unwrap();
        let slot = state.recipients.entry(recipient.to_string()).or_default();
        slot.epoch += 1;
        slot.notify.notify_waiters();
    }
    pub fn waiting(&self) -> usize {
        self.waiting.load(Ordering::SeqCst)
    }
    /// Take a waiter slot, or `None` at any cap. `source` is `None` for the
    /// front, which per-source caps exempt (D29).
    pub fn register(self: &Arc<Self>, recipient: &str, source: Option<&str>) -> Option<Waiter> {
        let mut state = self.state.lock().unwrap();
        let held = state.recipients.get(recipient).map_or(0, |s| s.waiters);
        let from_source = source.map_or(0, |s| state.sources.get(s).copied().unwrap_or(0));
        if held >= self.limits.per_principal
            || self.waiting() >= self.limits.total
            || (source.is_some() && from_source >= self.limits.per_source)
        {
            return None;
        }
        state
            .recipients
            .entry(recipient.to_string())
            .or_default()
            .waiters += 1;
        if let Some(source) = source {
            *state.sources.entry(source.to_string()).or_default() += 1;
        }
        self.waiting.fetch_add(1, Ordering::SeqCst);
        Some(Waiter {
            wake: self.clone(),
            recipient: recipient.to_string(),
            source: source.map(String::from),
        })
    }
    /// Wait until the epoch moves past `mark` or the deadline passes.
    pub async fn wait(&self, recipient: &str, mark: u64, deadline: tokio::time::Instant) {
        let notify = {
            let mut state = self.state.lock().unwrap();
            let slot = state.recipients.entry(recipient.to_string()).or_default();
            if slot.epoch != mark {
                return;
            }
            slot.notify.clone()
        };
        let notified = notify.notified();
        tokio::pin!(notified);
        notified.as_mut().enable();
        if self.epoch(recipient) != mark {
            return;
        }
        let _ = tokio::time::timeout_at(deadline, notified).await;
    }
}
/// A held waiter slot, released on drop (a finished or cancelled poll).
pub struct Waiter {
    wake: Arc<Wake>,
    recipient: String,
    source: Option<String>,
}
impl Drop for Waiter {
    fn drop(&mut self) {
        let mut state = self.wake.state.lock().unwrap();
        if let Some(slot) = state.recipients.get_mut(&self.recipient) {
            slot.waiters -= 1;
        }
        if let Some(source) = &self.source {
            if let Some(n) = state.sources.get_mut(source) {
                *n -= 1;
                if *n == 0 {
                    state.sources.remove(source);
                }
            }
        }
        self.wake.waiting.fetch_sub(1, Ordering::SeqCst);
    }
}
/// A poll in its first read, counted until dropped.
pub struct FirstRead(Arc<Wake>);
impl Drop for FirstRead {
    fn drop(&mut self) {
        self.0.reading.fetch_sub(1, Ordering::SeqCst);
    }
}
/// The watcher's position: the highest message id it has notified for, and
/// the `data_version` it last read rows at.
struct Watch {
    last: i64,
    seen: Option<i64>,
}
impl Watch {
    /// One watcher pass. `sample` reads `(data_version, MAX(id))`, `watched`
    /// counts waiters and first reads, and `query` lists each recipient's
    /// newest id above `last`.
    fn tick(
        &mut self,
        wake: &Wake,
        sample: impl FnOnce() -> (Option<i64>, i64),
        watched: impl FnOnce() -> usize,
        query: impl FnOnce(i64) -> rusqlite::Result<Vec<(String, i64)>>,
    ) {
        // Sample first, then check for watchers. A poll that starts its
        // first read after the check reads after these samples, so every
        // row absorbed here is visible to that read. A poll that started
        // before the check keeps this tick on the busy path below.
        let (now, top) = sample();
        if watched() == 0 {
            self.last = top;
            self.seen = now;
            return;
        }
        if now == self.seen {
            return;
        }
        // A failed query keeps the old `seen`, so the next tick reads the
        // same rows again instead of treating them as already notified.
        let Ok(rows) = query(self.last) else {
            return;
        };
        self.seen = now;
        for (recipient, id) in rows {
            self.last = self.last.max(id);
            wake.notify(&recipient);
        }
    }
}
/// The safety net for messages another process commits: while anyone waits
/// or is in a first read, sample `data_version` every 250 ms and bump the
/// recipients of new rows.
pub fn watch_external(path: &Path, wake: Arc<Wake>) -> Result<(), String> {
    watch_every(path, wake, Duration::from_millis(250))
}
fn watch_every(path: &Path, wake: Arc<Wake>, interval: Duration) -> Result<(), String> {
    let db = crate::schema::audit::open(
        path,
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY | rusqlite::OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .map_err(|e| e.to_string())?;
    db.execute_batch("PRAGMA query_only=ON")
        .map_err(|e| e.to_string())?;
    let max_id = |db: &rusqlite::Connection| -> i64 {
        db.query_row("SELECT COALESCE(MAX(id),0) FROM fleet_messages", [], |r| {
            r.get(0)
        })
        .unwrap_or(0)
    };
    let version = |db: &rusqlite::Connection| -> Option<i64> {
        db.query_row("PRAGMA data_version", [], |r| r.get(0)).ok()
    };
    std::thread::Builder::new()
        .name("hub-message-watch".into())
        .spawn(move || {
            let mut watch = Watch {
                last: max_id(&db),
                seen: version(&db),
            };
            loop {
                std::thread::sleep(interval);
                wake.ticks.fetch_add(1, Ordering::SeqCst);
                watch.tick(
                    &wake,
                    || (version(&db), max_id(&db)),
                    || wake.watched(),
                    |last| {
                        let mut statement = db.prepare_cached(
                            "SELECT recipient,MAX(id) FROM fleet_messages WHERE id>? GROUP BY recipient",
                        )?;
                        let rows = statement.query_map([last], |r| Ok((r.get(0)?, r.get(1)?)))?;
                        rows.collect()
                    },
                );
            }
        })
        .map_err(|e| e.to_string())?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn a_send_before_the_wait_ends_it_at_once() {
        let wake = Arc::new(Wake::new(Limits::for_connections(4096)));
        let mark = wake.epoch("node-b");
        let _waiter = wake.register("node-b", None).unwrap();
        wake.notify("node-b");
        let started = std::time::Instant::now();
        wake.wait(
            "node-b",
            mark,
            tokio::time::Instant::now() + Duration::from_secs(5),
        )
        .await;
        assert!(started.elapsed() < Duration::from_millis(100));
    }
    #[tokio::test]
    async fn a_commit_between_the_first_read_and_registration_still_wakes() {
        // The exact interleaving from review: an empty first snapshot, then a
        // commit by another process, then a watcher tick, then registration.
        let dir = std::env::temp_dir().join(format!("hub-wake-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("fleet-hub.db");
        let _ = std::fs::remove_file(&path);
        let other = rusqlite::Connection::open(&path).unwrap();
        other
            .execute_batch(
                "PRAGMA journal_mode=WAL; \
                 CREATE TABLE fleet_messages(id INTEGER PRIMARY KEY AUTOINCREMENT, recipient TEXT)",
            )
            .unwrap();
        let wake = Arc::new(Wake::new(Limits::for_connections(16)));
        watch_every(&path, wake.clone(), Duration::from_millis(20)).unwrap();
        let ticks = |n: usize| {
            let start = wake.ticks();
            let deadline = std::time::Instant::now() + Duration::from_secs(5);
            while wake.ticks() < start + n {
                assert!(std::time::Instant::now() < deadline, "watcher stalled");
                std::thread::sleep(Duration::from_millis(5));
            }
        };
        ticks(2);
        // `list`: watched, then the mark, then a snapshot that finds no rows.
        let first_read = wake.first_read();
        let mark = wake.epoch("node-b");
        // Another process commits a row for the recipient.
        other
            .execute("INSERT INTO fleet_messages(recipient) VALUES('node-b')", [])
            .unwrap();
        // A full watcher tick runs before the poll registers.
        ticks(2);
        let _waiter = wake.register("node-b", None).unwrap();
        drop(first_read);
        let started = std::time::Instant::now();
        wake.wait(
            "node-b",
            mark,
            tokio::time::Instant::now() + Duration::from_secs(5),
        )
        .await;
        assert!(
            started.elapsed() < Duration::from_millis(250),
            "woke after {:?}",
            started.elapsed()
        );
        let _ = std::fs::remove_dir_all(&dir);
    }
    #[test]
    fn a_failed_watcher_query_keeps_the_rows_for_the_next_tick() {
        let wake = Arc::new(Wake::new(Limits::for_connections(16)));
        let _waiter = wake.register("node-b", None).unwrap();
        let mut watch = Watch {
            last: 0,
            seen: Some(1),
        };
        // Another process committed row 1; the watcher's query fails once,
        // for example with SQLITE_BUSY while the `-shm` is rebuilt.
        watch.tick(
            &wake,
            || (Some(2), 1),
            || wake.watched(),
            |_| Err(rusqlite::Error::InvalidQuery),
        );
        assert_eq!(wake.epoch("node-b"), 0);
        // The next tick sees the same data_version and must still read the row.
        watch.tick(
            &wake,
            || (Some(2), 1),
            || wake.watched(),
            |last| {
                assert_eq!(last, 0);
                Ok(vec![("node-b".into(), 1)])
            },
        );
        assert_eq!(wake.epoch("node-b"), 1, "the wake was lost");
    }
    #[test]
    fn a_tick_samples_before_it_checks_for_watchers() {
        // The interleaving the order guards against: between the tick's two
        // first steps, a poll starts its first read (empty snapshot) and then
        // another process commits row 1 for that poll's recipient. Whichever
        // step runs first triggers the race, so a tick that checks watchers
        // before it samples absorbs row 1 and the poll's wake is lost.
        let wake = Arc::new(Wake::new(Limits::for_connections(16)));
        let db = Mutex::new((Some(1_i64), 0_i64));
        let first_read = Mutex::new(None);
        let raced = std::sync::atomic::AtomicBool::new(false);
        let race = || {
            if !raced.swap(true, Ordering::SeqCst) {
                *first_read.lock().unwrap() = Some(wake.first_read());
                *db.lock().unwrap() = (Some(2), 1);
            }
        };
        let mut watch = Watch {
            last: 0,
            seen: Some(1),
        };
        watch.tick(
            &wake,
            || {
                let sample = *db.lock().unwrap();
                race();
                sample
            },
            || {
                let watched = wake.watched();
                race();
                watched
            },
            |_| Ok(vec![]),
        );
        // The poll registers and drops its first-read guard.
        let mark = wake.epoch("node-b");
        let _waiter = wake.register("node-b", None).unwrap();
        drop(first_read.lock().unwrap().take());
        watch.tick(
            &wake,
            || *db.lock().unwrap(),
            || wake.watched(),
            |last| {
                Ok(if last < 1 {
                    vec![("node-b".into(), 1)]
                } else {
                    vec![]
                })
            },
        );
        assert_ne!(wake.epoch("node-b"), mark, "the watcher absorbed row 1");
    }
    #[tokio::test]
    async fn caps_hold_per_principal_and_per_source_but_not_for_the_front() {
        let wake = Arc::new(Wake::new(Limits {
            total: 4,
            per_principal: 2,
            per_source: 3,
        }));
        let a = [
            wake.register("a", Some("192.0.2.1")).unwrap(),
            wake.register("a", Some("192.0.2.1")).unwrap(),
        ];
        assert!(wake.register("a", None).is_none());
        let b = wake.register("b", Some("192.0.2.1")).unwrap();
        assert!(wake.register("c", Some("192.0.2.1")).is_none());
        let front = wake.register("c", None).unwrap();
        assert!(wake.register("d", None).is_none(), "total cap");
        drop((a, b, front));
        assert_eq!(wake.waiting(), 0);
        assert!(wake.register("a", Some("192.0.2.1")).is_some());
    }
    #[tokio::test]
    async fn a_wait_times_out_without_a_wake() {
        let wake = Arc::new(Wake::new(Limits::for_connections(16)));
        let _waiter = wake.register("x", None).unwrap();
        let started = std::time::Instant::now();
        wake.wait(
            "x",
            0,
            tokio::time::Instant::now() + Duration::from_millis(50),
        )
        .await;
        assert!(started.elapsed() >= Duration::from_millis(45));
    }
}
