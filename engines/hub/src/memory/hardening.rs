//! Resource limits and compact freshness bindings for proposal work.
use super::{
    Reply, ids,
    ops::{self, Authority},
    proposals,
};
use crate::writer::{ReadTx, WriteTx};
use rusqlite::{Connection, types::Value};
use serde_json::{Value as Json, json};
use std::{
    cell::Cell,
    collections::BTreeMap,
    time::{Duration, Instant},
};

#[derive(Clone, Debug)]
pub struct Limits {
    pub pending_per_principal: i64,
    pub pending_per_scope: i64,
    pub bytes_per_principal: i64,
    pub bytes_per_scope: i64,
    pub retention_days: i64,
    pub diagnostic_rows: usize,
    pub diagnostic_ms: u64,
    pub lock_ms: u64,
    pub auto_principal_ms: i64,
    pub auto_proposal_ms: i64,
}
impl Default for Limits {
    fn default() -> Self {
        Self {
            pending_per_principal: 32,
            pending_per_scope: 128,
            bytes_per_principal: 64 * 1024 * 1024,
            bytes_per_scope: 256 * 1024 * 1024,
            retention_days: 7,
            diagnostic_rows: 100_000,
            // Sized from a 1,500-card scope, where one automatic exact-duplicate
            // merge reads for about 150 ms and applies in about 170 ms on a
            // release build, and for a debug build on a slower runner. See
            // docs/memory-operations.md.
            diagnostic_ms: 1_000,
            lock_ms: 1_000,
            auto_principal_ms: 1_000,
            auto_proposal_ms: 60_000,
        }
    }
}
impl Limits {
    pub fn parse(config: &Json) -> Result<Self, String> {
        let mut limits = Self::default();
        let Some(values) = config.get("proposal_limits") else {
            return Ok(limits);
        };
        let values = values
            .as_object()
            .ok_or("memory.proposal_limits must be an object")?;
        for (key, value) in values {
            let n = value
                .as_i64()
                .filter(|n| *n > 0 && *n <= 1_000_000_000)
                .ok_or("proposal limits must be positive bounded integers")?;
            match key.as_str() {
                "pending_per_principal" => limits.pending_per_principal = n,
                "pending_per_scope" => limits.pending_per_scope = n,
                "bytes_per_principal" => limits.bytes_per_principal = n,
                "bytes_per_scope" => limits.bytes_per_scope = n,
                "retention_days" => limits.retention_days = n.min(365),
                "diagnostic_rows" => limits.diagnostic_rows = n as usize,
                "diagnostic_ms" => limits.diagnostic_ms = n as u64,
                "lock_ms" => limits.lock_ms = n as u64,
                "auto_principal_ms" => limits.auto_principal_ms = n,
                "auto_proposal_ms" => limits.auto_proposal_ms = n,
                _ => return Err("unknown memory.proposal_limits key".into()),
            }
        }
        Ok(limits)
    }
}

pub const DDL: &str = "
CREATE TABLE memory_proposal_scopes(proposal_id TEXT NOT NULL REFERENCES memory_proposals(proposal_id), scope TEXT NOT NULL, PRIMARY KEY(scope,proposal_id)) WITHOUT ROWID;
ALTER TABLE memory_proposals ADD COLUMN digest TEXT;
ALTER TABLE memory_proposals ADD COLUMN retained_bytes INTEGER NOT NULL DEFAULT 0;
ALTER TABLE memory_proposals ADD COLUMN terminal_ms INTEGER;
ALTER TABLE memory_proposals ADD COLUMN compacted INTEGER NOT NULL DEFAULT 0;
CREATE INDEX memory_proposals_principal ON memory_proposals(principal,state);
CREATE INDEX memory_proposals_digest ON memory_proposals(digest,state);
CREATE INDEX memory_card_revisions_hash ON memory_card_revisions(content_hash);
CREATE INDEX memory_handoffs_hash ON memory_handoffs(content_hash);
CREATE INDEX memory_proposals_terminal ON memory_proposals(compacted,terminal_ms);
CREATE TRIGGER memory_proposal_audit_immutable BEFORE UPDATE ON memory_proposals WHEN old.compacted=1 BEGIN SELECT RAISE(ABORT,'proposal audit is immutable'); END;
CREATE TRIGGER memory_proposals_no_delete BEFORE DELETE ON memory_proposals BEGIN SELECT RAISE(ABORT,'proposal audit is retained'); END;
ALTER TABLE memory_retrieval_evals ADD COLUMN dense_identity TEXT;
CREATE TRIGGER memory_vectors_changed AFTER UPDATE ON memory_vectors BEGIN INSERT INTO memory_vector_log(embedder_id,content_hash,present) VALUES(new.embedder_id,new.content_hash,1); END;
";
/// v16: covering indexes for the usage probes in `quota_excluding` and
/// `settle`. A probe reads only `state` and `retained_bytes` per proposal, so
/// the index keeps the walk off the proposal body. The queries name their
/// index with `INDEXED BY`: without statistics SQLite keeps the WITHOUT ROWID
/// primary-key probe and reads every body, which is what made a large
/// automatic-apply window miss the writer deadline (issue #2621).
pub const QUOTA_INDEXES: &str = "
CREATE INDEX IF NOT EXISTS memory_proposals_scope_quota ON memory_proposals(proposal_id, state, retained_bytes);
CREATE INDEX IF NOT EXISTS memory_proposals_principal_quota ON memory_proposals(principal, state, retained_bytes);
";
/// The exact principal usage probe in `quota_excluding`.
pub(super) const PRINCIPAL_USAGE: &str = "SELECT COALESCE(SUM(state IN ('pending','held')),0),COALESCE(SUM(retained_bytes),0),COALESCE(SUM(proposal_id=?2 AND state IN ('pending','held')),0),COALESCE(SUM(CASE WHEN proposal_id=?3 THEN retained_bytes ELSE 0 END),0) FROM memory_proposals INDEXED BY memory_proposals_principal_quota WHERE principal=?1";
/// The exact scope usage probe in `quota_excluding`.
pub(super) const SCOPE_USAGE: &str = "SELECT COALESCE(SUM(p.state IN ('pending','held')),0),COALESCE(SUM(p.retained_bytes),0),COALESCE(SUM(p.proposal_id=?2 AND p.state IN ('pending','held')),0),COALESCE(SUM(CASE WHEN p.proposal_id=?3 THEN p.retained_bytes ELSE 0 END),0) FROM memory_proposals p INDEXED BY memory_proposals_scope_quota JOIN memory_proposal_scopes s USING(proposal_id) WHERE s.scope=?1";
/// The strict principal total in `settle`.
pub(super) const SETTLE_PRINCIPAL_USAGE: &str = "SELECT COALESCE(SUM(state IN ('pending','held')),0),COALESCE(SUM(retained_bytes),0) FROM memory_proposals INDEXED BY memory_proposals_principal_quota WHERE principal=?1";
/// Pending proposal slots automatic anchoring leaves free for repair
/// proposals. Admission enforces the reservation in the same transaction that
/// reads the principal's pending proposals, so two reflectors cannot both
/// spend the reserve after reading one `status` count.
pub const ANCHOR_RESERVE_SLOTS: i64 = 4;
/// The strict scope total in `settle`.
pub(super) const SETTLE_SCOPE_USAGE: &str = "SELECT COALESCE(SUM(p.state IN ('pending','held')),0),COALESCE(SUM(p.retained_bytes),0) FROM memory_proposals p INDEXED BY memory_proposals_scope_quota JOIN memory_proposal_scopes s USING(proposal_id) WHERE s.scope=?1";

pub fn digest(body: &Json) -> String {
    let cards: Vec<_> = proposals::cards(body).iter().map(|c| json!({"scope":c["scope"],"card_id":c["card_id"],"base_revision":c["base_revision"]})).collect();
    ids::sha256_hex(ids::canonical(&json!({"epoch":body["base_epoch"],"cards":cards,"ops":body["ops"],"policy_digest":body["policy_digest"]})).as_bytes())
}
pub fn migrate(db: &Connection) -> rusqlite::Result<()> {
    // Each addition tolerates an already-applied step, as schema::step does.
    for statement in DDL.split(";\n") {
        let statement = statement.trim();
        if statement.is_empty() {
            continue;
        }
        let words: Vec<_> = statement.split_whitespace().collect();
        let present = if statement.starts_with("ALTER TABLE") {
            let table = words[2];
            let column = words[5];
            let mut columns = db.prepare(&format!("PRAGMA table_info({table})"))?;
            columns
                .query_map([], |r| r.get::<_, String>(1))?
                .any(|c| c.is_ok_and(|c| c == column))
        } else {
            let name = words[2].split('(').next().unwrap_or("");
            db.query_row(
                "SELECT EXISTS(SELECT 1 FROM sqlite_schema WHERE name=?1)",
                [name],
                |r| r.get::<_, bool>(0),
            )?
        };
        if !present {
            db.execute_batch(statement)?;
        }
    }
    let mut statement = db.prepare(
        "SELECT proposal_id,body,state,created_at FROM memory_proposals WHERE compacted=0",
    )?;
    let mut rows = statement.query([])?;
    while let Some(row) = rows.next()? {
        let id: String = row.get(0)?;
        let raw: String = row.get(1)?;
        let body: Json = serde_json::from_str(&raw).map_err(|_| rusqlite::Error::InvalidQuery)?;
        db.execute("UPDATE memory_proposals SET digest=?2,retained_bytes=?3,terminal_ms=CASE WHEN state IN ('applied','rejected','stale') THEN COALESCE(terminal_ms,(SELECT CAST(strftime('%s',MAX(created_at)) AS INTEGER)*1000 FROM memory_events WHERE kind='proposal.decide' AND json_extract(payload,'$.proposal_id')=?1),CAST(strftime('%s',memory_proposals.created_at) AS INTEGER)*1000,0) ELSE NULL END WHERE proposal_id=?1",rusqlite::params![id,digest(&body),raw.len() as i64])?;
        for card in proposals::cards(&body) {
            db.execute(
                "INSERT OR IGNORE INTO memory_proposal_scopes VALUES(?1,?2)",
                rusqlite::params![id, proposals::string(card, "scope")],
            )?;
        }
    }
    Ok(())
}
fn number(v: &Value) -> i64 {
    if let Value::Integer(n) = v { *n } else { 0 }
}
pub fn admit(tx: &dyn WriteTx, body: &Json, limits: &Limits) -> rusqlite::Result<Option<Reply>> {
    admit_excluding(tx, body, limits, None)
}
/// Only an exact compensation may exclude its validated review referral.
pub(super) fn admit_excluding(
    tx: &dyn WriteTx,
    body: &Json,
    limits: &Limits,
    referral: Option<&str>,
) -> rusqlite::Result<Option<Reply>> {
    let d = digest(body);
    let rows = tx.query(
        "SELECT proposal_id,json_extract(body,'$.tier') FROM memory_proposals WHERE digest=?1 AND state='pending' AND (?2 IS NULL OR proposal_id<>?2) LIMIT 1",
        &[ops::t(&d), referral.map(ops::t).unwrap_or(Value::Null)],
    )?;
    if let Some(row) = rows.first() {
        let mut reply = ops::error(
            409,
            "memory_proposal_duplicate_pending",
            "an identical pending proposal already exists",
        );
        reply.body["proposal_id"] = json!(ops::s(&row[0]));
        reply.body["tier"] = json!(number(&row[1]));
        return Ok(Some(reply));
    }
    if let Some(r) = quota_excluding(tx, body, limits, referral.unwrap_or(""), "")? {
        return Ok(Some(r));
    }
    anchor_reserve(tx, body, limits, referral.unwrap_or(""))
}
/// Anchoring repairs need pending slots, so an all-anchor proposal is refused
/// once the principal's pending and held proposals would leave fewer than
/// `ANCHOR_RESERVE_SLOTS` free. Every proposal with another op kind is admitted
/// under the normal limits, which is how repairs use the last four slots. The
/// count and the insert share one writer transaction.
fn anchor_reserve(
    tx: &dyn WriteTx,
    body: &Json,
    limits: &Limits,
    count_excluded: &str,
) -> rusqlite::Result<Option<Reply>> {
    let anchor_only = body["ops"]
        .as_array()
        .is_some_and(|ops| !ops.is_empty() && ops.iter().all(|op| op["type"] == "anchor_blocks"));
    if !anchor_only {
        return Ok(None);
    }
    let pending = i64::from(matches!(
        proposals::string(body, "state"),
        "pending" | "held"
    ));
    let usage = tx.query(
        PRINCIPAL_USAGE,
        &[
            ops::t(proposals::string(body, "proposer")),
            ops::t(count_excluded),
            ops::t(""),
        ],
    )?;
    let pending_after = number(&usage[0][0])
        .saturating_sub(number(&usage[0][2]))
        .saturating_add(pending);
    if pending_after
        > limits
            .pending_per_principal
            .saturating_sub(ANCHOR_RESERVE_SLOTS)
    {
        return Ok(Some(ops::error(
            429,
            "memory_anchor_quota_reserve",
            "the anchoring repair reserve is full",
        )));
    }
    Ok(None)
}
/// A dimension passes when its usage stays within the limit or does not grow.
/// Admission always grows pending count and bytes, so it stays strict, while a
/// decision that drains an existing backlog above a lowered limit is allowed.
fn within(before: i64, after: i64, limit: i64) -> bool {
    after <= limit || after <= before
}
/// Checks the usage a write leaves behind. `replacing` names the stored row
/// whose current pending count and bytes the new body replaces.
pub fn quota(
    tx: &dyn WriteTx,
    body: &Json,
    limits: &Limits,
    replacing: bool,
) -> rusqlite::Result<Option<Reply>> {
    let own = if replacing {
        proposals::string(body, "proposal_id")
    } else {
        ""
    };
    quota_excluding(tx, body, limits, own, own)
}
/// `count_excluded` names one stored row left out of the pending count, and
/// `bytes_excluded` one left out of the byte total. A replaced row is both. A
/// review referral an exact compensation closes in the same savepoint is only
/// its pending slot: it keeps its bytes until it is closed or compacted.
fn quota_excluding(
    tx: &dyn WriteTx,
    body: &Json,
    limits: &Limits,
    count_excluded: &str,
    bytes_excluded: &str,
) -> rusqlite::Result<Option<Reply>> {
    let pending = i64::from(matches!(
        proposals::string(body, "state"),
        "pending" | "held"
    ));
    let bytes = i64::try_from(body.to_string().len()).unwrap_or(i64::MAX);
    // Columns: total pending, total bytes, then the replaced row's share of each.
    let allowed = |r: &[Value], pending_limit, bytes_limit| {
        let (total_pending, total_bytes) = (number(&r[0]), number(&r[1]));
        let pending_after = total_pending
            .saturating_sub(number(&r[2]))
            .saturating_add(pending);
        let bytes_after = total_bytes
            .saturating_sub(number(&r[3]))
            .saturating_add(bytes);
        within(total_pending, pending_after, pending_limit)
            && within(total_bytes, bytes_after, bytes_limit)
    };
    let usage = tx.query(
        PRINCIPAL_USAGE,
        &[
            ops::t(proposals::string(body, "proposer")),
            ops::t(count_excluded),
            ops::t(bytes_excluded),
        ],
    )?;
    if !allowed(
        &usage[0],
        limits.pending_per_principal,
        limits.bytes_per_principal,
    ) {
        return Ok(Some(ops::error(
            429,
            "memory_proposal_principal_quota",
            "principal proposal storage quota reached",
        )));
    }
    let scopes: std::collections::BTreeSet<_> = proposals::cards(body)
        .iter()
        .map(|c| proposals::string(c, "scope"))
        .collect();
    for scope in scopes {
        let usage = tx.query(
            SCOPE_USAGE,
            &[
                ops::t(scope),
                ops::t(count_excluded),
                ops::t(bytes_excluded),
            ],
        )?;
        if !allowed(&usage[0], limits.pending_per_scope, limits.bytes_per_scope) {
            return Ok(Some(ops::error(
                429,
                "memory_proposal_scope_quota",
                "scope proposal storage quota reached",
            )));
        }
    }
    Ok(None)
}
/// Strict totals for a write that used an admission discount. Unlike `quota`,
/// nothing is replaced or excluded and the "does not grow" allowance does not
/// apply, so an inverse that did not close its referral cannot leave the
/// principal or any scope over its pending limit. The caller runs it inside the
/// same savepoint so a refusal rolls the inverse back.
pub fn settle(tx: &dyn WriteTx, body: &Json, limits: &Limits) -> rusqlite::Result<Option<Reply>> {
    let usage = tx.query(
        SETTLE_PRINCIPAL_USAGE,
        &[ops::t(proposals::string(body, "proposer"))],
    )?;
    if number(&usage[0][0]) > limits.pending_per_principal
        || number(&usage[0][1]) > limits.bytes_per_principal
    {
        return Ok(Some(ops::error(
            429,
            "memory_proposal_principal_quota",
            "principal proposal storage quota reached",
        )));
    }
    let scopes: std::collections::BTreeSet<_> = proposals::cards(body)
        .iter()
        .map(|c| proposals::string(c, "scope"))
        .collect();
    for scope in scopes {
        let usage = tx.query(SETTLE_SCOPE_USAGE, &[ops::t(scope)])?;
        if number(&usage[0][0]) > limits.pending_per_scope
            || number(&usage[0][1]) > limits.bytes_per_scope
        {
            return Ok(Some(ops::error(
                429,
                "memory_proposal_scope_quota",
                "scope proposal storage quota reached",
            )));
        }
    }
    Ok(None)
}
/// Replaces only derived proposal bodies. Events and audit identities are never removed.
pub fn compact(db: &Connection, now: i64, limits: &Limits, max: usize) -> rusqlite::Result<usize> {
    let mut stmt=db.prepare("SELECT proposal_id,body,principal,created_at,decided_by,digest,terminal_ms,state FROM memory_proposals WHERE compacted=0 AND terminal_ms IS NOT NULL AND terminal_ms<?1 LIMIT ?2")?;
    let mut rows = stmt.query(rusqlite::params![
        now.saturating_sub(limits.retention_days * 86_400_000),
        max as i64
    ])?;
    let mut count = 0;
    while let Some(row) = rows.next()? {
        let raw: String = row.get(1)?;
        let body: Json = serde_json::from_str(&raw).map_err(|_| rusqlite::Error::InvalidQuery)?;
        let audit = json!({"proposal_id":row.get::<_,String>(0)?,"compacted":true,"proposer":row.get::<_,String>(2)?,"created_at":row.get::<_,String>(3)?,"decided_by":row.get::<_,Option<String>>(4)?,"proposal_digest":row.get::<_,Option<String>>(5)?,"terminal_ms":row.get::<_,i64>(6)?,"state":row.get::<_,String>(7)?,"body_digest":ids::sha256_hex(raw.as_bytes()),"decision":body["decision"],"decision_mode":body["decision_mode"],"base_epoch":body["base_epoch"],"policy_digest":body["policy_digest"],"decision_policy_digest":body["decision_policy_digest"],"cards":proposals::cards(&body).iter().map(|c|json!({"scope":c["scope"],"card_id":c["card_id"],"base_revision":c["base_revision"],"before_hash":c["before_hash"],"after_hash":c["after_hash"]})).collect::<Vec<_>>()});
        let raw = audit.to_string();
        db.execute("UPDATE memory_proposals SET body=?2,retained_bytes=?3,compacted=1 WHERE proposal_id=?1",rusqlite::params![row.get::<_,String>(0)?,raw,raw.len() as i64])?;
        super::manifest_binding::compacted(db, &row.get::<_, String>(0)?, now)?;
        // Older hubs published proposed blobs before acceptance. Reclaim only
        // bytes with no durable card or handoff reference. Pending bodies own
        // their own text and will republish their blob if subsequently applied.
        for card in proposals::cards(&body) {
            db.execute("DELETE FROM memory_blobs WHERE content_hash=?1 AND NOT EXISTS(SELECT 1 FROM memory_card_revisions WHERE content_hash=?1) AND NOT EXISTS(SELECT 1 FROM memory_handoffs WHERE content_hash=?1) AND NOT EXISTS(SELECT 1 FROM memory_cards WHERE content_hash=?1)",[proposals::string(card,"after_hash")])?;
        }
        count += 1;
    }
    Ok(count)
}

pub fn cheap<T: ReadTx + ?Sized>(
    tx: &T,
    body: &Json,
    authority: &Authority,
) -> rusqlite::Result<Option<&'static str>> {
    if authority.policy_document["memory"]
        .get("auto_apply")
        .is_some_and(|v| !v.is_boolean())
    {
        return Ok(Some("policy_invalid"));
    }
    if authority.policy_document["memory"]["auto_apply"] != true {
        return Ok(Some("auto_apply_disabled"));
    }
    if tx
        .query(
            "SELECT value FROM memory_meta WHERE key='auto_apply_enabled'",
            &[],
        )?
        .first()
        .is_some_and(|r| ops::s(&r[0]) != "true")
    {
        return Ok(Some("auto_apply_killed"));
    }
    if super::compensation::class_held(tx, body)?.is_some() {
        return Ok(Some("compensation_class_held"));
    }
    // Named apart from the generic tier hold, so the reflect client leaves a
    // lineage proposal open for review instead of withdrawing and retrying it.
    if body["rollback_of"].is_null() && super::proposal_tiers::lineage_review(tx, body)? {
        return Ok(Some("lineage_requires_review"));
    }
    Ok((body["opened_tier"] != 0 || !body["rollback_of"].is_null())
        .then_some("tier_requires_review"))
}
/// Counts every diagnostic row, including evidence resolution, before allocating it.
pub struct Budget<'a> {
    pub db: &'a Connection,
    pub remaining: Cell<usize>,
    pub deadline: Instant,
}
impl ReadTx for Budget<'_> {
    fn query(&self, sql: &str, params: &[Value]) -> rusqlite::Result<Vec<Vec<Value>>> {
        let mut stmt = self.db.prepare(sql)?;
        let columns = stmt.column_count();
        let mut rows = stmt.query(rusqlite::params_from_iter(params))?;
        let mut result = Vec::new();
        while let Some(row) = rows.next()? {
            if self.remaining.get() == 0 || Instant::now() >= self.deadline {
                return Err(rusqlite::Error::InvalidQuery);
            }
            self.remaining.set(self.remaining.get() - 1);
            result.push(
                (0..columns)
                    .map(|i| row.get(i))
                    .collect::<rusqlite::Result<Vec<_>>>()?,
            );
        }
        Ok(result)
    }
}
/// Conservative global high water marks make a post-snapshot write hold the gate.
/// sqlite_sequence survives vector-log retention, so deletion cannot restore a token.
pub fn token<T: ReadTx + ?Sized>(tx: &T) -> rusqlite::Result<Json> {
    let mut token = json!({"epoch":ops::epoch(tx)?,"memory_events":0,"memory_vector_log":0,"memory_retrieval_history":0});
    for row in tx.query("SELECT name,seq FROM sqlite_sequence WHERE name IN ('memory_events','memory_vector_log','memory_retrieval_history') ORDER BY name",&[])? {
        token[ops::s(&row[0])]=json!(number(&row[1]));
    }
    Ok(token)
}

pub fn authority_digest(a: &Authority) -> String {
    ids::sha256_hex(ids::canonical(&json!({"policy":a.policy,"document":a.policy_document,"credentials":a.valid_credentials,"retrieval":a.retrieval.as_ref().map(|r|json!({"embedder":r.embedder_id,"weights":r.weights}))})).as_bytes())
}
#[derive(Default)]
pub struct Rate {
    principals: BTreeMap<String, i64>,
    proposals: BTreeMap<String, i64>,
}
impl Rate {
    pub fn admit(&mut self, principal: &str, proposal: &str, now: i64, limits: &Limits) -> bool {
        self.principals
            .retain(|_, at| now.saturating_sub(*at) < limits.auto_principal_ms);
        self.proposals
            .retain(|_, at| now.saturating_sub(*at) < limits.auto_proposal_ms);
        if self.principals.contains_key(principal)
            || self.proposals.contains_key(proposal)
            || self.principals.len() >= 4096
            || self.proposals.len() >= 4096
        {
            return false;
        }
        self.principals.insert(principal.into(), now);
        self.proposals.insert(proposal.into(), now);
        true
    }
}
/// The reflect-controlled commands the writer deadline and the diagnostics
/// deadline bound. Reviewed decisions and ordinary writes are outside it.
pub fn budgeted(action: &ops::Action) -> bool {
    matches!(action, ops::Action::Proposal(c) if matches!(c.name(), "auto_apply" | "verify_auto_apply" | "propose" | "compensation_watch" | "rollback"))
}
/// Principals whose budgeted command ran out of budget. The rate gate only
/// covers automatic apply, so without this a principal could repeat work that
/// runs to the full deadline under both writers. A principal stays here for
/// `auto_proposal_ms` after its last exhausted command and has its budgeted
/// commands refused before the writer is taken. Work that finishes inside its
/// budget never enters the map.
#[derive(Default)]
pub struct Cooldown {
    principals: BTreeMap<String, i64>,
}
impl Cooldown {
    /// Fails closed like `Rate::admit`: a full map refuses every principal it
    /// does not hold, because one it cannot record could not be cooled down.
    pub fn active(&mut self, principal: &str, now: i64, limits: &Limits) -> bool {
        self.principals
            .retain(|_, at| now.saturating_sub(*at) < limits.auto_proposal_ms);
        self.principals.contains_key(principal) || self.principals.len() >= 4096
    }
    /// A batch already past the check can add a few entries beyond the cap. The
    /// cap then refuses new principals until entries expire.
    pub fn start(&mut self, principal: &str, now: i64) {
        self.principals.insert(principal.into(), now);
    }
}
pub fn exhausted(reply: &Reply) -> bool {
    reply.body["code"] == "memory_auto_apply_budget_exceeded"
}
pub fn cooldown_refusal() -> Reply {
    ops::error(
        429,
        "memory_auto_apply_cooldown",
        "a recent command from this principal exhausted its work budget",
    )
}
pub fn deadline(ms: u64) -> Instant {
    Instant::now() + Duration::from_millis(ms)
}

/// Cheap holds retain useful review diagnostics without walking the corpus.
pub fn held(body: &Json, reason: &str) -> Json {
    let gate = match reason {
        "auto_apply_disabled" => "auto_apply_enabled",
        "auto_apply_killed" => "kill_switch",
        "policy_invalid" => "policy",
        _ => "candidate",
    };
    json!({"schema":"brigade.memory-tier0-preflight.v1","checks":[{"gate":gate,"code":reason,"state":"held"}],"candidate_sha256":digest(body),"delta_sha256":ids::sha256_hex(proposals::string(body,"diff").as_bytes()),"candidate_id":body["proposal_id"]})
}
