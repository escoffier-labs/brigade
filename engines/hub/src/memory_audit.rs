//! The hub-file side of memory `read_stats` auditing. Rows go to
//! `fleet_audit_log` through the shared S4a hub writer. This lives outside
//! `memory/` so the memory module never names a hub writer or writes a hub
//! table (D23). The memory store calls it only after its read finished, with
//! no memory lock held.
//!
//! Audit rows never take writer capacity other families need: at most
//! [`IN_FLIGHT`] inserts are queued or running at once, admission is
//! non-durable, and a row that finds no free slot is refused at once instead
//! of waiting.
use crate::memory::read_stats::{Audit, AuditLog};
use crate::writer::{ApplyError, Budget, Command, WriteTx, Writer};
use rusqlite::types::Value;
use serde_json::{Value as Json, json};
use std::{
    future::Future,
    pin::Pin,
    sync::Arc,
    time::{Duration, Instant},
};
use tokio::sync::Semaphore;

/// Audit inserts queued or running on the hub writer at once.
pub const IN_FLIGHT: usize = 4;
/// How long one audit row may wait for the hub writer.
const DEADLINE: Duration = Duration::from_secs(5);

struct Row(Audit);
impl Command for Row {
    fn apply(&self, tx: &dyn WriteTx) -> Result<Json, ApplyError> {
        let row = &self.0;
        tx.execute(
            "INSERT INTO fleet_audit_log(at,actor,action,subject,detail) VALUES(?1,?2,?3,?2,?4)",
            &[
                Value::Text(row.at.clone()),
                Value::Text(row.actor.clone()),
                Value::Text(row.action.into()),
                Value::Text(row.detail().to_string()),
            ],
        )?;
        Ok(json!({}))
    }
}

/// The hub writer as the memory store's audit log, with its own small budget.
pub struct HubAudit {
    writer: Writer,
    slots: Arc<Semaphore>,
}
impl HubAudit {
    pub fn new(writer: Writer) -> Self {
        Self {
            writer,
            slots: Arc::new(Semaphore::new(IN_FLIGHT)),
        }
    }
    /// Free audit slots, for tests.
    pub fn available(&self) -> usize {
        self.slots.available_permits()
    }
}
impl AuditLog for HubAudit {
    fn append(&self, row: Audit) -> Pin<Box<dyn Future<Output = bool> + Send + '_>> {
        Box::pin(async move {
            // Held until the row is committed or refused, so no more than
            // IN_FLIGHT audit rows ever sit in the writer's queue.
            let Ok(_slot) = self.slots.clone().try_acquire_owned() else {
                return false;
            };
            let budget = Budget {
                events: 1,
                bytes: 1024,
                durable: false,
                deadline: Instant::now() + DEADLINE,
            };
            match self.writer.submit(Row(row), budget).await {
                Ok(answer) => matches!(answer.await, Ok(Ok(_))),
                Err(_) => false,
            }
        })
    }
}
