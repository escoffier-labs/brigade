//! Long-poll wake epochs and waiter caps ([Long-poll] in the v2 plan).
//!
//! A waiter marks its recipient's epoch before every read and waits only
//! while the epoch still equals that mark, so a send that commits between an
//! empty read and the wait ends the wait at once. Native commits bump the
//! epoch in `post_commit`. Commits by other processes are found by a watch
//! connection that samples `PRAGMA data_version` while anyone waits.
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
    pub fn for_connections(max_connections: usize) -> (Self, usize) {
        let kept = (max_connections / 4).min(1024);
        (
            Self {
                total: (max_connections - kept).min(3072),
                per_principal: 2,
                per_source: 64,
            },
            kept,
        )
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
    limits: Limits,
}
impl Wake {
    pub fn new(limits: Limits) -> Self {
        Self {
            state: Mutex::default(),
            waiting: AtomicUsize::new(0),
            limits,
        }
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
        self.waiting.load(Ordering::Relaxed)
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
        self.waiting.fetch_add(1, Ordering::Relaxed);
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
        self.wake.waiting.fetch_sub(1, Ordering::Relaxed);
    }
}
/// The safety net for messages another process commits: while anyone waits,
/// sample `data_version` every 250 ms and bump the recipients of new rows.
pub fn watch_external(path: &Path, wake: Arc<Wake>) -> Result<(), String> {
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
            let mut last = max_id(&db);
            let mut seen = version(&db);
            loop {
                std::thread::sleep(Duration::from_millis(250));
                if wake.waiting() == 0 {
                    // Nobody waits: rows committed now are found by their first read.
                    last = max_id(&db);
                    seen = version(&db);
                    continue;
                }
                let now = version(&db);
                if now == seen {
                    continue;
                }
                seen = now;
                let Ok(mut statement) = db.prepare_cached(
                    "SELECT recipient,MAX(id) FROM fleet_messages WHERE id>? GROUP BY recipient",
                ) else {
                    continue;
                };
                let rows: Vec<(String, i64)> = statement
                    .query_map([last], |r| Ok((r.get(0)?, r.get(1)?)))
                    .and_then(|rows| rows.collect())
                    .unwrap_or_default();
                for (recipient, id) in rows {
                    last = last.max(id);
                    wake.notify(&recipient);
                }
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
        let wake = Arc::new(Wake::new(Limits::for_connections(4096).0));
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
    async fn caps_hold_per_principal_and_per_source_but_not_for_the_front() {
        let wake = Arc::new(Wake::new(Limits {
            total: 4,
            per_principal: 2,
            per_source: 3,
        }));
        let a = [
            wake.register("a", Some("10.0.0.1")).unwrap(),
            wake.register("a", Some("10.0.0.1")).unwrap(),
        ];
        assert!(wake.register("a", None).is_none());
        let b = wake.register("b", Some("10.0.0.1")).unwrap();
        assert!(wake.register("c", Some("10.0.0.1")).is_none());
        let front = wake.register("c", None).unwrap();
        assert!(wake.register("d", None).is_none(), "total cap");
        drop((a, b, front));
        assert_eq!(wake.waiting(), 0);
        assert!(wake.register("a", Some("10.0.0.1")).is_some());
    }
    #[tokio::test]
    async fn a_wait_times_out_without_a_wake() {
        let wake = Arc::new(Wake::new(Limits::for_connections(16).0));
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
