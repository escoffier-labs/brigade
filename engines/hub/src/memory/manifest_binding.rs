//! Proposal-bound attestation state and the reviewer hash read
//! (phase-memory-verify D5, slice 4).
//!
//! A record attached by a verifier principal is a verification manifest. It
//! starts in `pending_review`, binds to the first proposal that passes the
//! binding check, and leaves that state exactly once:
//!
//! - `active` when its proposal is applied. A `contrary` manifest then holds
//!   automatic apply like any other contrary record.
//! - `inert` when its proposal is rejected, goes stale for a reason that
//!   cannot be decided again, or is compacted, or when no proposal cites it
//!   within 24 hours.
//!
//! A `pending_review` record never holds automatic apply and never counts as
//! proof. An `inert` record also frees its quota slot, as a purged one does.
//! `memory_evidence` stays immutable, so the state lives in this side table,
//! and a trigger lets a row move forward only. Records attached by anyone
//! else have no row here and behave exactly as before.
use super::{
    Reply, auth,
    ops::{error, s, t},
    proposals::{self, string},
};
use crate::writer::{ReadTx, WriteTx};
use rusqlite::{Connection, types::Value};
use serde_json::{Value as Json, json};
use std::collections::BTreeSet;

pub const DDL: &str = "CREATE TABLE IF NOT EXISTS memory_evidence_bindings(
  evidence_id TEXT PRIMARY KEY REFERENCES memory_evidence(evidence_id),
  base_revision INTEGER NOT NULL CHECK(base_revision > 0),
  state TEXT NOT NULL CHECK(state IN ('pending_review','active','inert')),
  proposal_id TEXT,
  created_ms INTEGER NOT NULL,
  updated_ms INTEGER NOT NULL,
  CHECK(state != 'active' OR proposal_id IS NOT NULL)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS memory_evidence_bindings_proposal ON memory_evidence_bindings(proposal_id);
CREATE INDEX IF NOT EXISTS memory_evidence_bindings_unbound ON memory_evidence_bindings(state,created_ms) WHERE proposal_id IS NULL;
CREATE TRIGGER IF NOT EXISTS memory_evidence_bindings_forward BEFORE UPDATE ON memory_evidence_bindings
 WHEN OLD.state!='pending_review'
 OR OLD.evidence_id IS NOT NEW.evidence_id OR OLD.base_revision IS NOT NEW.base_revision
 OR OLD.created_ms IS NOT NEW.created_ms
 OR (OLD.proposal_id IS NOT NULL AND OLD.proposal_id IS NOT NEW.proposal_id)
 BEGIN SELECT RAISE(ABORT,'evidence binding moves forward only'); END;
CREATE TRIGGER IF NOT EXISTS memory_evidence_bindings_no_delete BEFORE DELETE ON memory_evidence_bindings
 BEGIN SELECT RAISE(ABORT,'evidence binding is retained'); END;
";

/// The capability `status` advertises once the hub enforces this life cycle.
pub const CAPABILITY: &str = "verifier_manifests";
pub const MANIFEST_KIND: &str = "memory-verify-manifest";
pub const MANIFEST_SCHEMA: &str = "brigade.memory-verify-manifest.v1";
/// A record no proposal cites within this window becomes inert.
pub const UNBOUND_MS: i64 = 24 * 3600 * 1000;

/// Appended to a `memory_evidence` query: the record holds and counts only if
/// it has no binding row or its row is `active`.
pub const COUNTS: &str = " AND NOT EXISTS(SELECT 1 FROM memory_evidence_bindings b WHERE b.evidence_id=memory_evidence.evidence_id AND b.state!='active')";
/// Appended to a quota query: an inert record frees its slot.
pub const OCCUPIES: &str = " AND NOT EXISTS(SELECT 1 FROM memory_evidence_bindings b WHERE b.evidence_id=memory_evidence.evidence_id AND b.state='inert')";

fn invalid_manifest() -> Reply {
    error(
        422,
        "memory_evidence_invalid",
        "a verifier attachment must be a memory-verify-manifest naming its card",
    )
}

/// Attach-time check for a verifier's record. Returns the manifest's base
/// revision, or the refusal. The manifest must name the attestation's own
/// scope and card id.
pub fn manifest_card(request: &Json, bytes: &[u8]) -> Result<i64, Reply> {
    if string(request, "artifact_kind") != MANIFEST_KIND {
        return Err(invalid_manifest());
    }
    let Ok(manifest) = serde_json::from_slice::<Json>(bytes) else {
        return Err(invalid_manifest());
    };
    let card = &manifest["card"];
    let (Some(scope), Some(card_id), Some(revision)) = (
        card["scope"].as_str(),
        card["card_id"].as_str(),
        card["base_revision"].as_i64().filter(|r| *r > 0),
    ) else {
        return Err(invalid_manifest());
    };
    if manifest["schema"] != MANIFEST_SCHEMA {
        return Err(invalid_manifest());
    }
    if scope != string(request, "scope") || card_id != string(request, "card_id") {
        return Err(error(
            422,
            "memory_manifest_card_mismatch",
            "the manifest's card differs from the attestation's scope and card id",
        ));
    }
    Ok(revision)
}

/// Record a verifier attachment in `pending_review`, unbound.
pub fn record(tx: &dyn WriteTx, id: &str, revision: i64, now: i64) -> rusqlite::Result<()> {
    tx.execute(
        "INSERT INTO memory_evidence_bindings(evidence_id,base_revision,state,proposal_id,created_ms,updated_ms) VALUES(?1,?2,'pending_review',NULL,?3,?3)",
        &[t(id), Value::Integer(revision), Value::Integer(now)],
    )?;
    Ok(())
}

fn binding_invalid(reason: &str) -> Reply {
    let mut reply = error(
        422,
        "memory_manifest_binding_invalid",
        "a cited verifier manifest can bind only to its own pending proposal for the exact card, scope and revision it checked",
    );
    reply.body["reason"] = json!(reason);
    reply
}

/// The binding check at `propose`. Every verifier record an `append_evidence`
/// op cites must bind, or the whole proposal is refused and nothing is
/// stored. Returns the ids to bind once the proposal is stored.
pub fn check(
    tx: &dyn WriteTx,
    body: &Json,
    proposer: &str,
    now: i64,
) -> rusqlite::Result<Result<Vec<String>, Reply>> {
    let ops = body["ops"].as_array().map(Vec::as_slice).unwrap_or(&[]);
    // Each cited id is looked up once, however many ops cite it.
    let cited: BTreeSet<&str> = ops
        .iter()
        .filter(|op| op["type"] == "append_evidence")
        .filter_map(|op| op["evidence_ids"].as_array())
        .flatten()
        .filter_map(Json::as_str)
        .collect();
    let mut bound = Vec::new();
    for id in cited {
        let rows = tx.query(
                "SELECT b.state,b.proposal_id,b.base_revision,b.created_ms,e.principal,e.scope,e.card_id,e.tombstoned FROM memory_evidence_bindings b JOIN memory_evidence e USING(evidence_id) WHERE b.evidence_id=?1",
                &[t(id)],
            )?;
        let Some(row) = rows.first() else {
            continue;
        };
        let revision = match row[2] {
            Value::Integer(n) => n,
            _ => 0,
        };
        let (scope, card_id) = (s(&row[5]), s(&row[6]));
        if s(&row[4]) != proposer {
            return Ok(Err(binding_invalid(
                "manifest attached by another principal",
            )));
        }
        if s(&row[0]) != "pending_review"
            || row[1] != Value::Null
            || row[7] != Value::Integer(0)
            || matches!(row[3], Value::Integer(created) if now.saturating_sub(created) >= UNBOUND_MS)
        {
            return Ok(Err(binding_invalid("manifest is not pending and unbound")));
        }
        // The citing op and every other op address the attested card at
        // the manifest's base revision. `other_card` stays in its scope.
        for target in ops {
            if string(target, "scope") != scope
                || string(target, "card_id") != card_id
                || target["base_revision"].as_i64() != Some(revision)
            {
                return Ok(Err(binding_invalid(
                    "an op addresses a card, scope or revision the manifest did not check",
                )));
            }
            if target
                .get("other_card")
                .is_some_and(|other| string(other, "scope") != scope)
            {
                return Ok(Err(binding_invalid("other_card is in another scope")));
            }
        }
        bound.push(id.to_owned());
    }
    Ok(Ok(bound))
}

/// Bind the checked records to the proposal just stored.
pub fn bind(tx: &dyn WriteTx, ids: &[String], proposal: &str, now: i64) -> rusqlite::Result<()> {
    for id in ids {
        tx.execute(
            "UPDATE memory_evidence_bindings SET proposal_id=?2,updated_ms=?3 WHERE evidence_id=?1 AND state='pending_review' AND proposal_id IS NULL",
            &[t(id), t(proposal), Value::Integer(now)],
        )?;
    }
    Ok(())
}

/// Follow a stored proposal transition. Applied activates its records.
/// Rejected and stale make them inert, except a policy-stale proposal, which
/// a reviewer may still decide and which compaction settles if nobody does.
pub fn settle(tx: &dyn WriteTx, body: &Json, now: i64) -> rusqlite::Result<()> {
    let next = match (string(body, "state"), string(body, "reason")) {
        ("applied", _) => "active",
        ("stale", "policy_changed") => return Ok(()),
        ("rejected" | "stale", _) => "inert",
        _ => return Ok(()),
    };
    tx.execute(
        "UPDATE memory_evidence_bindings SET state=?2,updated_ms=?3 WHERE proposal_id=?1 AND state='pending_review'",
        &[t(string(body, "proposal_id")), t(next), Value::Integer(now)],
    )?;
    Ok(())
}

/// Compaction settles every record its proposal still left pending.
pub fn compacted(db: &Connection, proposal: &str, now: i64) -> rusqlite::Result<()> {
    db.execute(
        "UPDATE memory_evidence_bindings SET state='inert',updated_ms=?2 WHERE proposal_id=?1 AND state='pending_review'",
        rusqlite::params![proposal, now],
    )?;
    Ok(())
}

/// The idle sweep: records no proposal cited within 24 hours become inert.
pub fn sweep(db: &Connection, now: i64, max: usize) -> rusqlite::Result<usize> {
    db.execute(
        "UPDATE memory_evidence_bindings SET state='inert',updated_ms=?1 WHERE evidence_id IN (SELECT evidence_id FROM memory_evidence_bindings WHERE state='pending_review' AND proposal_id IS NULL AND created_ms<=?2 ORDER BY created_ms LIMIT ?3)",
        rusqlite::params![now, now.saturating_sub(UNBOUND_MS), max as i64],
    )
}

/// The reviewer hash read on `proposal_show`. The list is added only for a
/// caller with `review` on every proposal target and on the scope of every
/// bound record. For anyone else the reply is left exactly as it was.
pub fn reveal<T: ReadTx + ?Sized>(
    tx: &T,
    who: &auth::Credential,
    body: &Json,
    response: &mut Json,
) -> rusqlite::Result<()> {
    for card in proposals::cards(body) {
        if !proposals::scoped_right(tx, who, string(card, "scope"), "review")? {
            return Ok(());
        }
    }
    let rows = tx.query(
        "SELECT b.evidence_id,e.artifact_kind,e.artifact_sha256,e.stance,b.state,e.scope FROM memory_evidence_bindings b JOIN memory_evidence e USING(evidence_id) WHERE b.proposal_id=?1 ORDER BY b.evidence_id",
        &[t(string(body, "proposal_id"))],
    )?;
    let mut list = Vec::with_capacity(rows.len());
    for row in &rows {
        if !proposals::scoped_right(tx, who, &s(&row[5]), "review")? {
            return Ok(());
        }
        list.push(json!({
            "evidence_id": s(&row[0]),
            "artifact_kind": s(&row[1]),
            "artifact_sha256": s(&row[2]),
            "stance": s(&row[3]),
            "state": s(&row[4]),
        }));
    }
    response["verifier_manifests"] = json!(list);
    Ok(())
}
