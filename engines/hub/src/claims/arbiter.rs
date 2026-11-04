//! `fleet_hub_claims` decisions, statement for statement. A returned reply
//! commits its writes (Python returned a value); `Stop` rolls them back
//! (Python raised).
use super::{MAX_GENERATION, Reply, Request, Stop, fence, fence_reply, json_value, repr};
use crate::wire::{iso, object, py_space, quote, string, text};
use crate::writer::{ReadTx, WriteTx};
use rusqlite::types::Value;
use sha2::{Digest, Sha256};
use std::collections::HashMap;

const MAX_CLAIM_TARGETS: i64 = 100_000;
const MAX_RETIRED_HOLDERS: i64 = 1_000_000;
const MAX_RETIRED_HOLDERS_PER_NODE: i64 = 100_000;
const RETIRED_HOLDER_HORIZON_SECONDS: f64 = 7.0 * 86400.0;
const RETIRED_HOLDER_EVICTION_BATCH: i64 = 256;
const EXPIRED_CLAIM_PRUNE_BATCH: i64 = 32;
const MAX_CLAIM_TARGET_BYTES: usize = 1024;
const MAX_ACTIVE_CLAIMS_PER_NODE: i64 = 1024;
const MAX_EVENTS_PER_PRODUCER_PER_HOUR: i64 = 120;
const COLUMNS: &str = "target, owner_node, owner_conductor, harness, role, job, session, acquired_at, renewed_at, ttl_seconds, expires_at, holder_token, lock_token, lock_acquired_at, lock_run_dir";
const RETIRED_SCHEMA: &str = "CREATE TABLE claim_retired_holders ( target TEXT NOT NULL, owner_node TEXT NOT NULL, holder_token TEXT NOT NULL, generation INTEGER NOT NULL CHECK (typeof(generation) = 'integer' AND generation >= 1 AND generation <= 9223372036854775807), retired_at REAL NOT NULL CHECK (typeof(retired_at) IN ('integer', 'real') AND retired_at >= 0 AND retired_at <= 1.7976931348623157e308), PRIMARY KEY (target, owner_node, holder_token) )";
const RETIRED_INDEXES: [&str; 2] = [
    "CREATE INDEX claim_retired_holders_age ON claim_retired_holders (retired_at)",
    "CREATE INDEX claim_retired_holders_node_age ON claim_retired_holders (owner_node, retired_at)",
];
const CORRUPT_FENCE: &str = "claim generation fence is missing or corrupt";
const CORRUPT_RETIRED: &str = "claim retired-holder fence is missing or corrupt";

pub struct Clock {
    /// `hub._now_epoch()`, microsecond rounded.
    pub now: f64,
    /// `time.time()`, used by the event outbox.
    pub time: f64,
    pub events: bool,
}

/// A private claim row: the payload columns, holder and lock fields, then generation.
struct Row {
    v: Vec<Value>,
    generation: i64,
}
impl Row {
    fn owner(&self) -> &str {
        string(&self.v[1])
    }
    fn holder_is(&self, holder: Option<&str>) -> bool {
        holder.is_some_and(|h| matches!(&self.v[11], Value::Text(s) if s == h))
    }
    fn expires(&self) -> Result<f64, Stop> {
        expires(&self.v[10])
    }
}
fn expires(v: &Value) -> Result<f64, Stop> {
    match v {
        Value::Real(f) => Ok(*f),
        Value::Integer(i) => Ok(*i as f64),
        _ => Err(Stop::Sql(rusqlite::Error::InvalidColumnType(
            10,
            "expires_at".into(),
            rusqlite::types::Type::Real,
        ))),
    }
}
fn payload(row: &Row) -> Result<String, Stop> {
    payload_with(row, None)
}
fn payload_with(row: &Row, expired: Option<bool>) -> Result<String, Stop> {
    let names = [
        "target",
        "owner_node",
        "owner_conductor",
        "harness",
        "role",
        "job",
        "session",
        "acquired_at",
        "renewed_at",
        "ttl_seconds",
    ];
    let mut fields: Vec<(&str, String)> = names
        .iter()
        .zip(&row.v)
        .map(|(name, value)| (*name, json_value(value)))
        .collect();
    fields.push(("expires_at", quote(&iso(row.expires()?))));
    fields.push(("generation", row.generation.to_string()));
    fields.push(("protocol", quote("legacy")));
    if let Some(expired) = expired {
        fields.push(("expired", expired.to_string()));
    }
    Ok(object(&fields))
}
fn one(tx: &(impl ReadTx + ?Sized), sql: &str, params: &[Value]) -> Result<i64, Stop> {
    match tx.query(sql, params)?.first().map(|r| &r[0]) {
        Some(Value::Integer(i)) => Ok(*i),
        _ => Err(Stop::Sql(rusqlite::Error::QueryReturnedNoRows)),
    }
}
fn checked_generation(v: &Value) -> Result<i64, Stop> {
    match v {
        Value::Integer(i) if *i >= 1 => Ok(*i),
        _ => fence(CORRUPT_FENCE),
    }
}
fn checked_retired_at(v: &Value) -> Result<f64, Stop> {
    match v {
        Value::Integer(i) if *i >= 0 => Ok(*i as f64),
        Value::Real(f) if f.is_finite() && *f >= 0.0 => Ok(*f),
        _ => fence("claim retired-holder fence timestamp is missing or corrupt"),
    }
}
fn generation(tx: &(impl ReadTx + ?Sized), target: &str, has_claim: bool) -> Result<i64, Stop> {
    match tx
        .query(
            "SELECT generation FROM claim_fences WHERE target = ?",
            &[text(target)],
        )?
        .first()
    {
        None if has_claim => fence(CORRUPT_FENCE),
        None => Ok(0),
        Some(r) => checked_generation(&r[0]),
    }
}
fn fetch(tx: &(impl ReadTx + ?Sized), target: &str) -> Result<Option<Row>, Stop> {
    let row = tx
        .query(
            &format!("SELECT {COLUMNS} FROM claims WHERE target = ?"),
            &[text(target)],
        )?
        .into_iter()
        .next();
    let generation = generation(tx, target, row.is_some())?;
    Ok(row.map(|v| Row { v, generation }))
}
fn advance(tx: &dyn WriteTx, target: &str, current: i64) -> Result<i64, Stop> {
    if current == MAX_GENERATION {
        return fence("claim generation fence exhausted");
    }
    if current == 0 {
        if target.len() > MAX_CLAIM_TARGET_BYTES {
            return Err(Stop::Reply((
                409,
                object(&[
                    (
                        "error",
                        object(&[
                            ("code", quote("invalid-field")),
                            ("field", quote("target")),
                            ("message", quote("claim target exceeds UTF-8 byte limit")),
                            ("limit", MAX_CLAIM_TARGET_BYTES.to_string()),
                            ("unit", quote("utf8-bytes")),
                        ]),
                    ),
                    ("reason", quote("claim-target-too-long")),
                    ("owner", "{}".into()),
                ]),
            )));
        }
        if one(tx, "SELECT COUNT(*) FROM claim_fences", &[])? >= MAX_CLAIM_TARGETS {
            return fence("claim generation fence capacity exceeded");
        }
        tx.execute(
            "INSERT INTO claim_fences (target, generation) VALUES (?, 1)",
            &[text(target)],
        )?;
    } else {
        tx.execute(
            "UPDATE claim_fences SET generation = ? WHERE target = ?",
            &[Value::Integer(current + 1), text(target)],
        )?;
    }
    Ok(current + 1)
}
fn retired(
    tx: &dyn WriteTx,
    request: &Request,
    holder: &str,
    current: i64,
    now: f64,
) -> Result<i64, Stop> {
    let rows = tx.query(
        "SELECT generation, retired_at FROM claim_retired_holders WHERE target = ? AND owner_node = ? AND holder_token = ?",
        &[text(&request.target), text(&request.node), text(holder)],
    )?;
    let Some(row) = rows.first() else {
        return Ok(0);
    };
    let value = checked_generation(&row[0])?;
    if value > current {
        return fence(CORRUPT_RETIRED);
    }
    Ok(
        if checked_retired_at(&row[1])? >= now - RETIRED_HOLDER_HORIZON_SECONDS {
            value
        } else {
            0
        },
    )
}
fn normalize(sql: &str) -> String {
    sql.split(py_space)
        .filter(|s| !s.is_empty())
        .collect::<Vec<_>>()
        .join(" ")
}
/// `_validate_structure` for the retired-holder table.
fn validate_retired(tx: &dyn WriteTx) -> Result<(), Stop> {
    let entries = tx.query(
        "SELECT type, sql FROM sqlite_master WHERE tbl_name = ? COLLATE NOCASE",
        &[text("claim_retired_holders")],
    )?;
    if entries.is_empty() {
        return fence("claim retired-holder fence storage is missing or corrupt");
    }
    let sql = |r: &Vec<Value>| normalize(string(&r[1]));
    let tables: Vec<String> = entries
        .iter()
        .filter(|r| string(&r[0]) == "table")
        .map(sql)
        .collect();
    let mut actual: Vec<(String, String)> = entries
        .iter()
        .filter(|r| string(&r[0]) != "table" && !(string(&r[0]) == "index" && r[1] == Value::Null))
        .map(|r| (string(&r[0]).to_string(), sql(r)))
        .collect();
    actual.sort();
    let mut declared: Vec<(String, String)> = RETIRED_INDEXES
        .iter()
        .map(|s| ("index".to_string(), s.to_string()))
        .collect();
    declared.sort();
    if tables.len() != 1 || actual != declared || tables[0] != RETIRED_SCHEMA {
        return fence("claim retired-holder fence storage has an unsupported definition");
    }
    Ok(())
}

/// `_RetirementBudget`: transaction-local counts and bounded reclamation.
struct Budget<'a> {
    tx: &'a dyn WriteTx,
    now: f64,
    cutoff: f64,
    nodes: HashMap<String, i64>,
    count: i64,
}
impl<'a> Budget<'a> {
    fn new(tx: &'a dyn WriteTx, now: f64) -> Result<Self, Stop> {
        validate_retired(tx)?;
        let count = one(tx, "SELECT COUNT(*) FROM claim_retired_holders", &[])?;
        if count > MAX_RETIRED_HOLDERS {
            return fence("claim retired-holder fence capacity exceeded");
        }
        let mut budget = Self {
            tx,
            now,
            cutoff: now - RETIRED_HOLDER_HORIZON_SECONDS,
            nodes: HashMap::new(),
            count,
        };
        budget.evict()?;
        Ok(budget)
    }
    fn evict(&mut self) -> Result<(), Stop> {
        let rows = self.tx.query(
            "SELECT r.rowid, r.generation, r.retired_at, f.generation FROM claim_retired_holders r LEFT JOIN claim_fences f ON f.target = r.target WHERE r.retired_at < ? ORDER BY r.retired_at LIMIT ?",
            &[Value::Real(self.cutoff), Value::Integer(RETIRED_HOLDER_EVICTION_BATCH)],
        )?;
        for row in &rows {
            if checked_generation(&row[1])? > checked_generation(&row[3])? {
                return fence(CORRUPT_RETIRED);
            }
            checked_retired_at(&row[2])?;
        }
        for row in &rows {
            self.tx.execute(
                "DELETE FROM claim_retired_holders WHERE rowid = ?",
                &[row[0].clone()],
            )?;
        }
        self.count -= rows.len() as i64;
        Ok(())
    }
    fn key(target: &str, node: &str, holder: &str) -> [Value; 3] {
        [text(target), text(node), text(holder)]
    }
    fn check(
        &mut self,
        target: &str,
        node: &str,
        holder: &str,
        enforce: bool,
    ) -> Result<(bool, bool), Stop> {
        let old = self.tx.query(
            "SELECT generation, retired_at FROM claim_retired_holders WHERE target = ? AND owner_node = ? AND holder_token = ?",
            &Self::key(target, node, holder),
        )?;
        let mut old = old.into_iter().next();
        if let Some(row) = &old {
            if checked_generation(&row[0])? > generation(self.tx, target, false)? {
                return fence(CORRUPT_RETIRED);
            }
            if checked_retired_at(&row[1])? >= self.cutoff {
                return Ok((false, false));
            }
        }
        if enforce && !self.nodes.contains_key(node) {
            let count = one(
                self.tx,
                "SELECT COUNT(*) FROM claim_retired_holders WHERE owner_node = ? AND retired_at >= ?",
                &[text(node), Value::Real(self.cutoff)],
            )?;
            self.nodes.insert(node.into(), count);
        }
        let node_full = enforce && self.nodes[node] >= MAX_RETIRED_HOLDERS_PER_NODE;
        if node_full || (old.is_none() && self.count >= MAX_RETIRED_HOLDERS) {
            self.evict()?;
            old = self
                .tx
                .query(
                    "SELECT 1 FROM claim_retired_holders WHERE target = ? AND owner_node = ? AND holder_token = ?",
                    &Self::key(target, node, holder),
                )?
                .into_iter()
                .next();
        }
        if node_full {
            return capacity("claim retired-holder fence per-node quota exceeded");
        }
        if old.is_none() && self.count >= MAX_RETIRED_HOLDERS {
            return capacity("claim retired-holder fence capacity exceeded");
        }
        Ok((old.is_none(), true))
    }
    fn retire(
        &mut self,
        target: &str,
        node: &str,
        holder: &str,
        current: i64,
        enforce: bool,
    ) -> Result<(), Stop> {
        let (new_row, new_active) = self.check(target, node, holder, enforce)?;
        self.tx.execute(
            "INSERT INTO claim_retired_holders (target, owner_node, holder_token, generation, retired_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT(target, owner_node, holder_token) DO UPDATE SET generation = excluded.generation, retired_at = excluded.retired_at",
            &[
                text(target),
                text(node),
                text(holder),
                Value::Integer(current),
                Value::Real(self.now),
            ],
        )?;
        self.count += i64::from(new_row);
        if new_active {
            if let Some(n) = self.nodes.get_mut(node) {
                *n += 1;
            }
        }
        Ok(())
    }
}
fn capacity<T>(message: &str) -> Result<T, Stop> {
    Err(Stop::Capacity(fence_reply(message)))
}

/// `fleet_hub_events.record_claim_expired`.
fn record_expired(
    tx: &dyn WriteTx,
    clock: &Clock,
    target: &str,
    owner: &str,
    holder: &Value,
    expires_at: &Value,
) -> Result<(), Stop> {
    if !clock.events
        || tx
            .query(
                "SELECT 1 FROM fleet_event_subscriptions WHERE name=? AND (expires_at IS NULL OR expires_at>?) LIMIT 1",
                &[text("claim.expired"), Value::Real(clock.time)],
            )?
            .is_empty()
    {
        return Ok(());
    }
    let recent = one(
        tx,
        "SELECT COUNT(*) FROM fleet_events WHERE name=? AND producer=? AND created_at>?",
        &[
            text("claim.expired"),
            text(owner),
            Value::Real(clock.time - 3600.0),
        ],
    )?;
    if recent >= MAX_EVENTS_PER_PRODUCER_PER_HOUR {
        return Ok(());
    }
    let id = format!(
        "[{}, {}, {}]",
        quote(target),
        json_value(holder),
        json_value(expires_at)
    );
    let digest = Sha256::digest(id.as_bytes());
    let hex: String = digest[..16].iter().map(|b| format!("{b:02x}")).collect();
    let data = format!(
        "{{\"owner_node\":{},\"target\":{},\"expired_at\":{}}}",
        quote(owner),
        quote(target),
        quote(&iso(expires(expires_at)?))
    );
    tx.execute(
        "INSERT OR IGNORE INTO fleet_events(event_id,name,subject,producer,data,created_at) VALUES(?,?,?,?,?,?)",
        &[
            text(&format!("evt_claim_{hex}")),
            text("claim.expired"),
            text(owner),
            text(owner),
            text(&data),
            Value::Real(clock.time),
        ],
    )?;
    Ok(())
}

fn stale(
    tx: &dyn WriteTx,
    request: &Request,
    row: Option<&Row>,
    current: i64,
    now: f64,
) -> Result<Option<Reply>, Stop> {
    let action = request.action;
    let holder = request.holder.as_deref();
    let mut retired_generation = 0;
    if let Some(holder) = holder {
        if action != "release" || request.scope == "holder" {
            retired_generation = retired(tx, request, holder, current, now)?;
        }
    }
    let expected = request.generation;
    let retry = action == "acquire"
        && row.is_some_and(|r| r.owner() == request.node && r.holder_is(holder))
        && expected == Some(current - 1)
        && expected.is_some_and(|e| e >= retired_generation);
    let reason = match expected {
        Some(e) if e != current && !retry => "stale-generation",
        None if retired_generation != 0 => "stale-holder",
        _ => return Ok(None),
    };
    if action == "release" && row.is_none() && reason == "stale-holder" {
        return Ok(Some((
            200,
            object(&[("released", "false".into()), ("reason", quote(reason))]),
        )));
    }
    let key = match action {
        "acquire" => "granted",
        "renew" => "renewed",
        _ => "released",
    };
    Ok(Some((
        409,
        object(&[
            (key, "false".into()),
            ("reason", quote(reason)),
            (
                "error",
                quote(&format!("claim operation refused: {reason}")),
            ),
            (
                "owner",
                match row {
                    Some(r) => payload(r)?,
                    None => "{}".into(),
                },
            ),
        ]),
    )))
}

fn prune(
    tx: &dyn WriteTx,
    clock: &Clock,
    budget: &mut Budget,
    requested: &str,
) -> Result<(), Stop> {
    let now = Value::Real(clock.now);
    let mut targets = tx.query(
        "SELECT target, owner_node, holder_token, expires_at FROM claims WHERE target = ? AND expires_at <= ?",
        &[text(requested), now.clone()],
    )?;
    targets.extend(tx.query(
        "SELECT target, owner_node, holder_token, expires_at FROM claims WHERE expires_at <= ? AND target != ? LIMIT ?",
        &[now, text(requested), Value::Integer(EXPIRED_CLAIM_PRUNE_BATCH)],
    )?);
    for row in targets {
        let (target, node, holder) = (string(&row[0]), string(&row[1]), string(&row[2]));
        let current = generation(tx, target, true)?;
        if current == MAX_GENERATION {
            return fence("claim generation fence exhausted");
        }
        match budget.retire(target, node, holder, current + 1, false) {
            Err(Stop::Capacity(_)) if target != requested => continue,
            Err(stop) => return Err(stop),
            Ok(()) => {}
        }
        advance(tx, target, current)?;
        tx.execute("DELETE FROM claims WHERE target = ?", &[text(target)])?;
        record_expired(tx, clock, target, node, &row[2], &row[3])?;
    }
    Ok(())
}

fn acquire(tx: &dyn WriteTx, request: &Request, clock: &Clock) -> Result<Reply, Stop> {
    let now = clock.now;
    let mut budget = Budget::new(tx, now)?;
    prune(tx, clock, &mut budget, &request.target)?;
    let (target, node) = (request.target.as_str(), request.node.as_str());
    let holder = request.holder.as_deref().unwrap();
    let prior = fetch(tx, target)?;
    let current = generation(tx, target, prior.is_some())?;
    if let Some(reply) = stale(tx, request, prior.as_ref(), current, now)? {
        return Ok(reply);
    }
    let retry = prior
        .as_ref()
        .is_some_and(|p| p.owner() == node && p.holder_is(Some(holder)));
    let replace = match (&prior, &request.supersede) {
        (Some(p), Some(s)) => {
            p.owner() == node
                && matches!(&p.v[12], Value::Text(t) if *t == s.token)
                && match (&s.acquired_at, &p.v[13]) {
                    (None, _) | (_, Value::Null) => true,
                    (Some(a), Value::Text(t)) => t.as_str() <= a.as_str(),
                    _ => false,
                }
        }
        _ => false,
    };
    if let Some(p) = prior.as_ref().filter(|_| !retry && !replace) {
        let mut error = format!("target {} is held by {}", repr(target), p.owner());
        if request.scope == "node" && p.owner() == node {
            error += " under another lease than the reconciled dead run's (not superseded)";
        }
        return Ok((
            409,
            object(&[
                ("granted", "false".into()),
                ("error", quote(&error)),
                ("owner", payload(p)?),
            ]),
        ));
    }
    if prior.is_none()
        && one(
            tx,
            "SELECT count(*) FROM (SELECT 1 FROM claims WHERE owner_node = ? AND expires_at > ? LIMIT ?)",
            &[
                text(node),
                Value::Real(now),
                Value::Integer(MAX_ACTIVE_CLAIMS_PER_NODE),
            ],
        )? >= MAX_ACTIVE_CLAIMS_PER_NODE
    {
        return Err(Stop::Reply((
            409,
            object(&[
                ("granted", "false".into()),
                ("reason", quote("claim-cardinality-exceeded")),
                ("owner", "{}".into()),
                (
                    "error",
                    quote(&format!(
                        "active claims per node exceed limit {MAX_ACTIVE_CLAIMS_PER_NODE}"
                    )),
                ),
                ("limit", MAX_ACTIVE_CLAIMS_PER_NODE.to_string()),
            ]),
        )));
    }
    if !retry {
        budget.check(target, node, holder, true)?;
        let started = advance(tx, target, current)?;
        if let Some(p) = &prior {
            budget.retire(target, p.owner(), string(&p.v[11]), started, false)?;
        }
    }
    let opt = |v: &Option<String>| v.as_deref().map(text).unwrap_or(Value::Null);
    let (lock_token, lock_acquired, lock_run_dir) = match &request.lock {
        Some(l) => (text(&l.token), opt(&l.acquired_at), opt(&l.run_dir)),
        None => (Value::Null, Value::Null, Value::Null),
    };
    let now_iso = text(&iso(now));
    let acquired_at = match (&prior, retry) {
        (Some(p), true) => p.v[7].clone(),
        _ => now_iso.clone(),
    };
    let [harness, role, job, session] = &request.labels;
    tx.execute(
        "INSERT INTO claims (target, owner_node, owner_conductor, harness, role, job, session, holder_token, lock_token, lock_acquired_at, lock_run_dir, acquired_at, renewed_at, ttl_seconds, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(target) DO UPDATE SET owner_node=excluded.owner_node, owner_conductor=excluded.owner_conductor, harness=excluded.harness, role=excluded.role, job=excluded.job, session=excluded.session, holder_token=excluded.holder_token, lock_token=excluded.lock_token, lock_acquired_at=excluded.lock_acquired_at, lock_run_dir=excluded.lock_run_dir, acquired_at=excluded.acquired_at, renewed_at=excluded.renewed_at, ttl_seconds=excluded.ttl_seconds, expires_at=excluded.expires_at",
        &[
            text(target),
            text(node),
            opt(&request.conductor),
            opt(harness),
            opt(role),
            opt(job),
            opt(session),
            text(holder),
            lock_token,
            lock_acquired,
            lock_run_dir,
            acquired_at,
            now_iso,
            Value::Integer(request.ttl),
            Value::Real(now + request.ttl as f64),
        ],
    )?;
    let row = fetch(tx, target)?.expect("claim row was just written");
    let mut fields = vec![("granted", "true".to_string()), ("claim", payload(&row)?)];
    if let Some(p) = prior
        .as_ref()
        .filter(|p| request.scope == "node" && !p.holder_is(Some(holder)))
    {
        fields.push(("superseded", payload(p)?));
    }
    Ok((200, object(&fields)))
}

pub fn decide(tx: &dyn WriteTx, request: &Request, clock: &Clock) -> Result<Reply, Stop> {
    if request.action == "acquire" {
        return acquire(tx, request, clock);
    }
    let now = clock.now;
    let mut budget = Budget::new(tx, now)?;
    let (target, node) = (request.target.as_str(), request.node.as_str());
    let holder = request.holder.as_deref();
    let row = fetch(tx, target)?;
    let current = generation(tx, target, row.is_some())?;
    if let Some(reply) = stale(tx, request, row.as_ref(), current, now)? {
        return Ok(reply);
    }
    if request.action == "renew" {
        if let Some(r) = &row {
            let live = r.expires()? > now;
            if live && r.holder_is(holder) && r.owner() == node {
                tx.execute(
                    "UPDATE claims SET renewed_at = ?, ttl_seconds = ?, expires_at = ? WHERE target = ?",
                    &[
                        text(&iso(now)),
                        Value::Integer(request.ttl),
                        Value::Real(now + request.ttl as f64),
                        text(target),
                    ],
                )?;
                let renewed = fetch(tx, target)?.expect("renewed claim row");
                return Ok((
                    200,
                    object(&[("renewed", "true".into()), ("claim", payload(&renewed)?)]),
                ));
            }
            if live {
                return Ok((
                    409,
                    object(&[
                        ("renewed", "false".into()),
                        (
                            "error",
                            quote(&format!("target {} is held by {}", repr(target), r.owner())),
                        ),
                        ("owner", payload(r)?),
                    ]),
                ));
            }
        }
        return Ok((
            409,
            object(&[
                ("renewed", "false".into()),
                (
                    "error",
                    quote(&format!("claim on {} is expired or missing", repr(target))),
                ),
                ("owner", "null".into()),
            ]),
        ));
    }
    let scope = request.scope;
    let expected = request.acquired_at.as_deref();
    let accepted = match &row {
        None => true,
        Some(r) if scope == "holder" => r.holder_is(holder) && r.owner() == node,
        Some(r) => {
            (scope == "force" || r.owner() == node)
                && expected.is_none_or(|e| matches!(&r.v[7], Value::Text(t) if t == e))
        }
    };
    if accepted {
        let ended = advance(tx, target, current)?;
        if let Some(r) = &row {
            budget.retire(target, r.owner(), string(&r.v[11]), ended, false)?;
        } else if scope == "holder" {
            budget.retire(target, node, holder.unwrap(), ended, true)?;
        }
        tx.execute("DELETE FROM claims WHERE target = ?", &[text(target)])?;
        if let Some(r) = &row {
            if r.expires()? <= now {
                record_expired(tx, clock, target, r.owner(), &r.v[11], &r.v[10])?;
            }
        }
        let mut fields = vec![("released", row.is_some().to_string())];
        if scope == "holder" && row.is_none() {
            fields.push(("reason", quote("stale-holder")));
        }
        if let Some(r) = row.as_ref().filter(|_| scope != "holder") {
            fields.push(("claim", payload(r)?));
        }
        return Ok((200, object(&fields)));
    }
    let r = row.as_ref().unwrap();
    let error = if scope == "holder" {
        format!("target {} is held by {}", repr(target), r.owner())
    } else if scope != "force" && r.owner() != node {
        format!(
            "target {} is held by {}, not {node}",
            repr(target),
            r.owner()
        )
    } else {
        format!(
            "target {} was re-acquired since it was inspected (now acquired {})",
            repr(target),
            string(&r.v[7])
        )
    };
    Ok((
        409,
        object(&[
            ("released", "false".into()),
            ("error", quote(&error)),
            ("owner", payload(r)?),
        ]),
    ))
}

/// `_decide` for inspect. It writes nothing, so it runs on a reader snapshot.
pub fn inspect(tx: &dyn ReadTx, request: &Request, now: f64) -> Result<Reply, Stop> {
    let row = fetch(tx, &request.target)?;
    let current = generation(tx, &request.target, row.is_some())?;
    let live = match &row {
        Some(r) if r.expires()? > now => r,
        _ => {
            let mut fields = vec![
                ("inspected", "true".to_string()),
                ("claim", "null".into()),
                ("owned", "false".into()),
            ];
            if current != 0 {
                fields.push(("generation", current.to_string()));
            }
            return Ok((200, object(&fields)));
        }
    };
    let owned = live.owner() == request.node;
    let mut fields = vec![
        ("inspected", "true".to_string()),
        ("claim", payload(live)?),
        ("owned", owned.to_string()),
        ("generation", current.to_string()),
    ];
    if owned {
        fields.push(("lock_run_dir", json_value(&live.v[14])));
    }
    Ok((200, object(&fields)))
}

/// The ingress fence snapshot for a legacy operator release.
pub fn observe(tx: &dyn ReadTx, target: &str) -> Result<i64, Stop> {
    let rows = tx.query(
        "SELECT f.generation, EXISTS(SELECT 1 FROM claims WHERE target = ?) FROM (SELECT 1) LEFT JOIN claim_fences f ON f.target = ?",
        &[text(target), text(target)],
    )?;
    let row = &rows[0];
    match &row[0] {
        Value::Null if row[1] == Value::Integer(1) => fence(CORRUPT_FENCE),
        Value::Null => Ok(0),
        value => checked_generation(value),
    }
}

/// `list_claims`: every row with its fence in one snapshot.
pub fn list(tx: &dyn ReadTx, include_all: bool, now: f64) -> Result<Reply, Stop> {
    let columns = COLUMNS
        .split(", ")
        .map(|c| format!("c.{c}"))
        .collect::<Vec<_>>()
        .join(", ");
    let rows = tx.query(
        &format!(
            "SELECT {columns}, f.generation FROM claims c LEFT JOIN claim_fences f ON f.target = c.target ORDER BY c.target"
        ),
        &[],
    )?;
    let mut claims = Vec::new();
    for mut v in rows {
        let generation = match checked_generation(&v[15]) {
            Ok(g) => g,
            Err(Stop::Reply(_) | Stop::Capacity(_)) => {
                return Ok((
                    503,
                    object(&[
                        ("error", quote(CORRUPT_FENCE)),
                        ("reason", quote("claim-fence-refused")),
                    ]),
                ));
            }
            Err(e) => return Err(e),
        };
        v.truncate(15);
        let row = Row { v, generation };
        let expired = row.expires()? <= now;
        if expired && !include_all {
            continue;
        }
        claims.push(payload_with(&row, Some(expired))?);
    }
    Ok((200, object(&[("claims", crate::wire::array(claims))])))
}
