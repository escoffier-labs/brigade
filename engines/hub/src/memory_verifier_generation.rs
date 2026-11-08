//! The hub-file side of memory verifier generations (phase-memory-verify D4).
//! Before a verifier flag change reaches the memory writer, the store records
//! the change's generation in `hub_meta` through the shared S4a hub writer,
//! durably. The Python policy writer reads that value and refuses a verifier
//! list published at an older generation. This lives outside `memory/` so the
//! memory module never names a hub writer or writes a hub table (D23). The
//! store calls it with no memory lock held.
use crate::memory::verifier::{GenerationLog, HUB_GENERATION_KEY};
use crate::writer::{ApplyError, Budget, Command, WriteTx, Writer};
use rusqlite::types::Value;
use serde_json::{Value as Json, json};
use std::{
    future::Future,
    pin::Pin,
    time::{Duration, Instant},
};

/// How long one announcement may wait for the hub writer.
const DEADLINE: Duration = Duration::from_secs(5);

struct Announce(i64);
impl Command for Announce {
    fn apply(&self, tx: &dyn WriteTx) -> Result<Json, ApplyError> {
        // Generations only move forward, whatever order announcements land in.
        tx.execute(
            "INSERT INTO hub_meta(key,value) VALUES(?1,?2) ON CONFLICT(key) DO UPDATE SET value=excluded.value WHERE CAST(hub_meta.value AS INTEGER) < CAST(excluded.value AS INTEGER)",
            &[
                Value::Text(HUB_GENERATION_KEY.into()),
                Value::Text(self.0.to_string()),
            ],
        )?;
        Ok(json!({}))
    }
}

/// The hub writer as the store's generation log.
pub struct HubGeneration {
    writer: Writer,
}
impl HubGeneration {
    pub fn new(writer: Writer) -> Self {
        Self { writer }
    }
}
impl GenerationLog for HubGeneration {
    fn record(&self, generation: i64) -> Pin<Box<dyn Future<Output = bool> + Send + '_>> {
        Box::pin(async move {
            let budget = Budget {
                events: 1,
                bytes: 256,
                durable: true,
                deadline: Instant::now() + DEADLINE,
            };
            match self.writer.submit(Announce(generation), budget).await {
                Ok(answer) => matches!(answer.await, Ok(Ok(_))),
                Err(_) => false,
            }
        })
    }
}
