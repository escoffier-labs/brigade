//! The hub-file side of memory `read_stats` auditing. One `fleet_audit_log`
//! row per request goes through the shared S4a hub writer. This lives outside
//! `memory/` so the memory module never names a hub writer or writes a hub
//! table (D23). The memory store calls it only after its read finished, with
//! no memory lock held.
use crate::memory::read_stats::{AUDIT_ACTION, Audit, AuditLog};
use crate::writer::{ApplyError, Budget, Command, WriteTx, Writer};
use rusqlite::types::Value;
use serde_json::{Value as Json, json};
use std::{
    future::Future,
    pin::Pin,
    time::{Duration, Instant},
};

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
                Value::Text(AUDIT_ACTION.into()),
                Value::Text(row.detail().to_string()),
            ],
        )?;
        Ok(json!({}))
    }
}

/// The hub writer as the memory store's audit log.
pub struct HubAudit(pub Writer);
impl AuditLog for HubAudit {
    fn append(&self, row: Audit) -> Pin<Box<dyn Future<Output = bool> + Send + '_>> {
        Box::pin(async move {
            // Non-durable: an overloaded writer refuses at once, and the request is
            // answered 503 instead of served without its audit row.
            let budget = Budget {
                events: 1,
                bytes: 1024,
                durable: false,
                deadline: Instant::now() + DEADLINE,
            };
            match self.0.submit(Row(row), budget).await {
                Ok(answer) => matches!(answer.await, Ok(Ok(_))),
                Err(_) => false,
            }
        })
    }
}
