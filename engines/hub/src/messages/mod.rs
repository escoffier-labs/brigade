//! S8 native messages. Python's `fleet_hub_messages`, `fleet_hub_sealed`,
//! `fleet_hub_message_grants`, `fleet_hub_message_keys` and
//! `fleet_hub_questions` are the oracle: the same validation order, refusal
//! text and response bytes. The Python hub keeps the schema (D6).
//!
//! Reads never write. They project the lifecycle Python's pre-read
//! `_prune_expired` would have written: expired rows are hidden, a `reply_to`
//! to an expired parent reads as null, and every sealed row acknowledged at
//! least 7 days ago reads as its tombstone. Writer commands run Python's
//! housekeeping inside the same transaction, the 32-row purge batch included.
mod questions;
pub mod sealed;
pub mod wake;
use crate::claims::json_value;
use crate::wire::{array, iso_us, object, py_trim, quote, valid_node};
use crate::writer::{ApplyError, Command, ReadTx, WriteTx};
use rusqlite::types::Value;
use serde_json::{Map, Value as Json, json};
use sha2::{Digest, Sha256};
use std::sync::{Arc, Mutex};
use subtle::ConstantTimeEq;

pub const SUBJECT_LIMIT: usize = 200;
pub const BODY_LIMIT: usize = 64 * 1024;
const REF_LIMIT: usize = 256;
const UNACKED_QUOTA: i64 = 1000;
const SENDER_DAILY_QUOTA: i64 = 1000;
const AGENT_RECIPIENT_UNACKED_CAP: i64 = 200;
pub const MAX_LIST_RESPONSE_BYTES: usize = 7 * 1024 * 1024;
pub const MAX_WAIT_MS: i64 = 20_000;
/// `PURGE_BATCH_SIZE` in `fleet_hub_sealed`.
pub const PURGE_BATCH: i64 = 32;
/// The maintenance sweep's bound per command.
pub const SWEEP_BATCH: i64 = 256;
/// Expired rows one writer command deletes, with their children's `reply_to`
/// cleared. Python deletes every expired row at once. Reads, quota and cap
/// counts and the storage sums all filter on the retention cutoff, and `send`
/// deletes an expired `(sender, client_id)` row before its dedup check, so a
/// backlog drains over later commands with the same results. No single command
/// runs a delete long enough to hit the writer deadline (for example after the
/// retention window is shortened). A refused command rolls its batch back,
/// as Python's `conn.rollback()` undoes its prune; the backlog then changes
/// no answer, only how many commands it takes to drain.
pub const RETENTION_BATCH: i64 = 1024;
const SECOND_US: i64 = 1_000_000;
const DAY_US: i64 = 86_400 * SECOND_US;
const SENT_COLUMNS: &str = "m.id,m.sender,m.recipient,m.subject,m.body,m.reference,m.priority,m.client_id,m.sealed_reply_to,m.reply_to,m.is_reply,m.created_at,m.acked_at,m.acked_by,m.sender_label,m.thread,m.kind,m.envelope,m.envelope_bytes,m.content_state,(SELECT p.created_at<?1 FROM fleet_messages AS p WHERE p.id=m.reply_to)";

/// A finished answer: status and Python's exact body.
pub type Reply = (u16, String);

/// Python's message exceptions, as `_post_api` answers them, plus the two
/// native outcomes: a write the Python upstream must answer, and SQLite.
#[derive(Debug)]
pub enum Fail {
    Error {
        message: String,
        code: Option<&'static str>,
    },
    Forbidden {
        message: String,
        code: &'static str,
    },
    Future(String),
    Conflict {
        message: String,
        code: Option<&'static str>,
        id: Option<i64>,
    },
    Quota {
        message: String,
        code: &'static str,
        recipient: Option<String>,
    },
    Inactive(String),
    Auth(String),
    Reply(Reply),
    /// Leave the request to the Python upstream. Nothing was written.
    Python,
    Sql(rusqlite::Error),
}
impl From<rusqlite::Error> for Fail {
    fn from(e: rusqlite::Error) -> Self {
        Self::Sql(e)
    }
}
impl Fail {
    pub fn error(message: &str) -> Self {
        Self::Error {
            message: message.into(),
            code: None,
        }
    }
    pub fn code(message: &str, code: &'static str) -> Self {
        Self::Error {
            message: message.into(),
            code: Some(code),
        }
    }
    pub fn conflict(message: &str, code: &'static str) -> Self {
        Self::Conflict {
            message: message.into(),
            code: Some(code),
            id: None,
        }
    }
    pub fn quota(message: &str, code: &'static str, recipient: Option<&str>) -> Self {
        Self::Quota {
            message: message.into(),
            code,
            recipient: recipient.map(Into::into),
        }
    }
    fn auth(message: &str) -> Self {
        Self::Auth(message.into())
    }
    fn inactive() -> Self {
        Self::auth("sender is not an active hub principal")
    }
    /// The status and body `_post_api` sends, or `None` for Python and SQLite.
    pub fn reply(self) -> Option<Reply> {
        let error = |m: &str| ("error", quote(m));
        Some(match self {
            Self::Error { message, code } => {
                let mut fields = vec![error(&message)];
                fields.extend(code.map(|c| ("code", quote(c))));
                (400, object(&fields))
            }
            Self::Forbidden { message, code } => {
                (403, object(&[error(&message), ("code", quote(code))]))
            }
            Self::Future(message) => (
                400,
                object(&[error(&message), ("code", quote("message_timestamp_future"))]),
            ),
            Self::Conflict { message, code, id } => {
                let mut fields = vec![error(&message)];
                fields.extend(id.map(|i| ("id", i.to_string())));
                fields.extend(code.map(|c| ("code", quote(c))));
                (409, object(&fields))
            }
            Self::Quota {
                message,
                code,
                recipient,
            } => {
                let mut fields = vec![error(&message), ("code", quote(code))];
                fields.extend(recipient.map(|r| ("recipient", quote(&r))));
                (429, object(&fields))
            }
            Self::Inactive(recipient) => (
                400,
                object(&[
                    error("recipient is temporarily inactive"),
                    ("code", quote("message_recipient_inactive")),
                    ("recipient", quote(&recipient)),
                ]),
            ),
            Self::Auth(message) => (
                403,
                object(&[error(&message), ("code", quote("message_sender_inactive"))]),
            ),
            Self::Reply(reply) => reply,
            Self::Python | Self::Sql(_) => return None,
        })
    }
}
pub fn error_reply(status: u16, message: &str) -> Reply {
    (status, object(&[("error", quote(message))]))
}

/// `StoragePolicy` as `fleet_hub_startup.build_server` resolves it.
#[derive(Clone, Debug)]
pub struct Policy {
    pub require_sealed: bool,
    pub max_envelope_bytes: i64,
    pub sender_storage_bytes: i64,
    pub fleet_storage_bytes: Option<i64>,
    pub retention_days: i64,
}
impl Default for Policy {
    fn default() -> Self {
        Self {
            require_sealed: false,
            max_envelope_bytes: sealed::ENVELOPE_LIMIT,
            sender_storage_bytes: 1024 * 1024 * 1024,
            fleet_storage_bytes: None,
            retention_days: 90,
        }
    }
}
impl Policy {
    /// `policy_from_environment(os.environ)` plus `--require-sealed`.
    pub fn from_env(
        get: impl Fn(&str) -> Option<String>,
        require_sealed: bool,
    ) -> Result<Self, String> {
        let flag = get("BRIGADE_FLEET_MESSAGES_REQUIRE_SEALED").unwrap_or_default();
        if !matches!(flag.as_str(), "" | "0" | "1") {
            return Err(
                "BRIGADE_FLEET_MESSAGES_REQUIRE_SEALED must be unset, empty, 0, or 1".into(),
            );
        }
        let mut policy = Self {
            require_sealed: require_sealed || flag == "1",
            ..Self::default()
        };
        for field in [
            "max_envelope_bytes",
            "sender_storage_bytes",
            "fleet_storage_bytes",
            "retention_days",
        ] {
            let name = format!("BRIGADE_FLEET_MESSAGES_{}", field.to_uppercase());
            let Some(text) = get(&name) else { continue };
            let valid = !text.is_empty()
                && text.as_bytes()[0] != b'0'
                && text.bytes().all(|b| b.is_ascii_digit());
            let value = valid
                .then(|| text.parse::<i64>().ok())
                .flatten()
                .ok_or(format!("{name} must be a positive integer"))?;
            match field {
                "max_envelope_bytes" => policy.max_envelope_bytes = value,
                "sender_storage_bytes" => policy.sender_storage_bytes = value,
                "fleet_storage_bytes" => policy.fleet_storage_bytes = Some(value),
                _ => {
                    if value > 3650 {
                        return Err(
                            "BRIGADE_FLEET_MESSAGES_RETENTION_DAYS must be an integer from 1 to 3650"
                                .into(),
                        );
                    }
                    policy.retention_days = value;
                }
            }
        }
        Ok(policy)
    }
    fn cutoff(&self, now: i64) -> String {
        iso_us(now - self.retention_days * DAY_US)
    }
}

fn t(s: &str) -> Value {
    Value::Text(s.into())
}
fn text_of(v: &Value) -> Option<&str> {
    match v {
        Value::Text(s) => Some(s),
        _ => None,
    }
}
pub(crate) fn count<T: ReadTx + ?Sized>(
    tx: &T,
    sql: &str,
    params: &[Value],
) -> rusqlite::Result<i64> {
    Ok(match tx.query(sql, params)?.first().map(|r| &r[0]) {
        Some(Value::Integer(i)) => *i,
        _ => 0,
    })
}
fn exists<T: ReadTx + ?Sized>(tx: &T, sql: &str, params: &[Value]) -> rusqlite::Result<bool> {
    Ok(!tx.query(sql, params)?.is_empty())
}
/// `_PRINCIPAL_NAME_RE`: `^[a-z][a-z0-9._-]{0,62}$`.
pub(crate) fn principal_name(name: &str) -> bool {
    let b = name.as_bytes();
    !b.is_empty()
        && b.len() <= 63
        && b[0].is_ascii_lowercase()
        && b.iter()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || b"._-".contains(c))
}
fn label_pattern(s: &str, max: usize, extra: &[u8]) -> bool {
    let b = s.as_bytes();
    !b.is_empty()
        && b.len() <= max
        && b[0].is_ascii_alphanumeric()
        && b.iter()
            .all(|c| c.is_ascii_alphanumeric() || extra.contains(c))
}
/// `isinstance(value, int) and not isinstance(value, bool)` within SQLite's range.
pub fn integer(value: Option<&Json>, allow_zero: bool) -> Option<i64> {
    match value {
        Some(Json::Number(n)) if n.is_i64() || n.is_u64() => n
            .as_i64()
            .filter(|v| if allow_zero { *v >= 0 } else { *v >= 1 }),
        _ => None,
    }
}
/// `_text`: a non-empty (after `strip`) UTF-8 string of at most `limit` bytes.
fn text(
    value: Option<&Json>,
    name: &str,
    limit: usize,
    required: bool,
) -> Result<Option<String>, Fail> {
    let refusal = || {
        Fail::error(&format!(
            "{name} must be a non-empty UTF-8 string of at most {limit} bytes"
        ))
    };
    match value {
        None | Some(Json::Null) if !required => Ok(None),
        Some(Json::String(s)) if (!required || !py_trim(s).is_empty()) && s.len() <= limit => {
            Ok(Some(s.clone()))
        }
        _ => Err(refusal()),
    }
}
/// `raw.get(key)`: an explicit `null` is Python's `None`, like a missing key.
fn get<'a>(raw: &'a Map<String, Json>, key: &str) -> Option<&'a Json> {
    raw.get(key).filter(|v| !v.is_null())
}
/// Bodies serde_json reads differently from `json.loads` stay on the Python
/// upstream: serde reads the integer `-0` as the float -0.0.
pub fn python_only(raw: &Json) -> bool {
    match raw {
        Json::Number(n) => n.as_f64().is_some_and(|f| f == 0.0 && f.is_sign_negative()),
        Json::Array(items) => items.iter().any(python_only),
        Json::Object(map) => map.values().any(python_only),
        _ => false,
    }
}
/// `_metadata`: optional `sender_label`, `thread` and `kind`, in that order.
fn metadata(raw: &Map<String, Json>, sender: &str) -> Result<Vec<(&'static str, String)>, Fail> {
    let mut extras = Vec::new();
    for field in ["sender_label", "thread"] {
        let Some(value) = get(raw, field) else {
            continue;
        };
        if field == "sender_label" && sender.starts_with("agent:") {
            return Err(Fail::error(
                "sender_label is only available to node principals",
            ));
        }
        let valid = value.as_str().is_some_and(|s| {
            if field == "sender_label" {
                label_pattern(s, 64, b"._-")
            } else {
                label_pattern(s, 128, b"._:/#-")
            }
        });
        if !valid {
            return Err(Fail::error(&format!("{field} is invalid")));
        }
        extras.push((field, value.as_str().unwrap().to_string()));
    }
    if let Some(kind) = get(raw, "kind") {
        if kind == "operator_question" {
            return Err(Fail::error(
                "operator_question is reserved for hub-created operator questions",
            ));
        }
        match kind.as_str() {
            Some("message") => {}
            Some(k @ ("question" | "completion")) => extras.push(("kind", k.to_string())),
            _ => return Err(Fail::error("kind must be message, question, or completion")),
        }
    }
    Ok(extras)
}
fn sha256_hex(bytes: &[u8]) -> String {
    let digest = Sha256::digest(bytes);
    digest.iter().fold(String::with_capacity(64), |mut out, b| {
        use std::fmt::Write;
        let _ = write!(out, "{b:02x}");
        out
    })
}
/// `json.dumps(value, ensure_ascii=False)` of a string.
fn quote_utf8(s: &str) -> String {
    serde_json::to_string(s).unwrap()
}
/// `_payload_digest`: SHA-256 of `json.dumps([...], ensure_ascii=False)`.
fn payload_digest(
    fields: [&str; 3],
    reference: Option<&str>,
    priority: &str,
    reply_to: Option<i64>,
    extras: &[(&str, String)],
) -> String {
    let mut items: Vec<String> = fields.iter().map(|s| quote_utf8(s)).collect();
    items.push(reference.map_or("null".into(), quote_utf8));
    items.push(quote_utf8(priority));
    items.push(reply_to.map_or("null".into(), |r| r.to_string()));
    if !extras.is_empty() {
        let pairs: Vec<String> = extras
            .iter()
            .map(|(k, v)| format!("{}: {}", quote_utf8(k), quote_utf8(v)))
            .collect();
        items.push(format!("{{{}}}", pairs.join(", ")));
    }
    sha256_hex(array(items).as_bytes())
}

/// One stored row as `_row` and `json.dumps` render it, with the lifecycle
/// projection applied. `row` follows `SENT_COLUMNS`.
fn message(row: &[Value], purge_cutoff: &str) -> (i64, String) {
    let id = match row[0] {
        Value::Integer(i) => i,
        _ => 0,
    };
    let reply_to = match (&row[8], &row[20]) {
        (Value::Null, Value::Integer(1)) => Value::Null,
        (Value::Null, _) => row[9].clone(),
        (sealed, _) => sealed.clone(),
    };
    let kind = match text_of(&row[16]) {
        Some(k) if !k.is_empty() => quote(k),
        _ => quote("message"),
    };
    let names = [
        "id",
        "sender",
        "recipient",
        "subject",
        "body",
        "ref",
        "priority",
        "client_id",
    ];
    let mut fields: Vec<(&str, String)> = names
        .iter()
        .zip(&row[..8])
        .map(|(k, v)| (*k, json_value(v)))
        .collect();
    fields.push(("reply_to", json_value(&reply_to)));
    fields.push((
        "is_reply",
        if row[10] == Value::Integer(1) {
            "true"
        } else {
            "false"
        }
        .into(),
    ));
    for (k, v) in [
        "created_at",
        "acked_at",
        "acked_by",
        "sender_label",
        "thread",
    ]
    .iter()
    .zip(&row[11..16])
    {
        fields.push((k, json_value(v)));
    }
    fields.push(("kind", kind));
    if let Some(envelope) = text_of(&row[17]) {
        let expired = text_of(&row[19]) == Some("sealed")
            && text_of(&row[12]).is_some_and(|acked| acked <= purge_cutoff);
        match expired.then(|| sealed::tombstone(envelope)) {
            Some(Some(tombstone)) => {
                fields.push(("envelope", quote(&tombstone)));
                fields.push(("envelope_bytes", tombstone.len().to_string()));
                fields.push(("content_state", quote("content_expired")));
            }
            // A corrupt stored envelope past its lifetime has no tombstone. Fail
            // closed: report the content as expired and never return its bytes.
            Some(None) => fields.push(("content_state", quote("content_expired"))),
            None => {
                fields.push(("envelope", json_value(&row[17])));
                fields.push(("envelope_bytes", json_value(&row[18])));
                fields.push(("content_state", json_value(&row[19])));
            }
        }
    }
    (id, object(&fields))
}
/// `_message_page`: rows up to `MAX_LIST_RESPONSE_BYTES` and the cursor.
fn page(rows: &[Vec<Value>], after: i64, purge_cutoff: &str) -> String {
    let mut messages = Vec::new();
    let mut size = 0;
    for row in rows {
        let (id, candidate) = message(row, purge_cutoff);
        let overhead = format!("{{\"messages\": [], \"next_cursor\": {id}}}").len() - 2;
        let separator = if messages.is_empty() { 0 } else { 2 };
        if overhead + size + separator + candidate.len() > MAX_LIST_RESPONSE_BYTES {
            break;
        }
        size += separator + candidate.len();
        messages.push((id, candidate));
    }
    let cursor = messages.last().map_or(after, |(id, _)| *id);
    object(&[
        ("messages", array(messages.into_iter().map(|(_, m)| m))),
        ("next_cursor", cursor.to_string()),
    ])
}
fn purge_cutoff(now: i64) -> String {
    iso_us(now - 7 * DAY_US)
}
fn fetch<T: ReadTx + ?Sized>(
    tx: &T,
    id: i64,
    now: i64,
    policy: &Policy,
) -> rusqlite::Result<Option<String>> {
    let rows = tx.query(
        &format!(
            "SELECT {SENT_COLUMNS} FROM fleet_messages AS m WHERE m.id=?2 AND m.created_at>=?1"
        ),
        &[t(&policy.cutoff(now)), Value::Integer(id)],
    )?;
    Ok(rows.first().map(|r| message(r, &purge_cutoff(now)).1))
}

// ---------------------------------------------------------------- callers

/// `_caller` plus `admit_post` for `POST /messages`: the admin token, a node
/// token, then a message-principal token. `Ok(None)` is the admin with admin
/// writes allowed, which `handle` refuses once the body is read.
pub fn authenticate<T: ReadTx + ?Sized>(
    tx: &T,
    key: &str,
    admin: &str,
    allow_admin_writes: bool,
) -> rusqlite::Result<Result<Option<String>, Reply>> {
    match crate::claims::caller(tx, key, admin, true, allow_admin_writes)? {
        Ok(caller) => Ok(Ok(caller)),
        Err((401, body)) if body == object(&[("error", quote("unauthorized"))]) => {
            let rows = tx.query(
                "SELECT principal_id FROM fleet_message_principals WHERE token_hash=? AND revoked_at IS NULL",
                &[t(key)],
            )?;
            Ok(match rows.first().and_then(|r| text_of(&r[0])) {
                Some(p) => Ok(Some(p.to_string())),
                None => Err((401, body)),
            })
        }
        Err(reply) => Ok(Err(reply)),
    }
}
/// `_active_principal`.
fn active<T: ReadTx + ?Sized>(tx: &T, principal: &str) -> rusqlite::Result<bool> {
    Ok(exists(
        tx,
        "SELECT 1 FROM nodes WHERE node_id=? AND revoked_at IS NULL",
        &[t(principal)],
    )? || exists(
        tx,
        "SELECT 1 FROM fleet_message_principals WHERE principal_id=? AND revoked_at IS NULL",
        &[t(principal)],
    )?)
}
fn require_active<T: ReadTx + ?Sized>(tx: &T, principal: &str) -> Result<(), Fail> {
    if active(tx, principal)? {
        Ok(())
    } else {
        Err(Fail::inactive())
    }
}
/// `fleet_hub_questions._generation`.
fn generation<T: ReadTx + ?Sized>(tx: &T, caller: &str) -> Result<String, Fail> {
    let rows = tx.query(
        "SELECT token_sha256 FROM nodes WHERE node_id=? AND revoked_at IS NULL \
         UNION ALL SELECT token_hash FROM fleet_message_principals WHERE principal_id=? AND revoked_at IS NULL",
        &[t(caller), t(caller)],
    )?;
    rows.first()
        .map(|r| match &r[0] {
            Value::Text(s) => s.clone(),
            other => json_value(other),
        })
        .ok_or_else(Fail::inactive)
}
fn same_generation<T: ReadTx + ?Sized>(tx: &T, caller: &str, key: &str) -> Result<bool, Fail> {
    Ok(bool::from(
        generation(tx, caller)?.as_bytes().ct_eq(key.as_bytes()),
    ))
}
fn require_generation<T: ReadTx + ?Sized>(
    tx: &T,
    caller: &str,
    key: &str,
    message: &str,
) -> Result<(), Fail> {
    if same_generation(tx, caller, key)? {
        Ok(())
    } else {
        Err(Fail::auth(message))
    }
}

// ----------------------------------------------------------------- grants

/// `classify_principal`.
fn classify<T: ReadTx + ?Sized>(tx: &T, principal: &str) -> rusqlite::Result<Option<&'static str>> {
    if let Some(name) = principal.strip_prefix("agent:") {
        if !principal_name(name) {
            return Ok(None);
        }
        let rows = tx.query(
            "SELECT role FROM fleet_message_principals WHERE principal_id=? AND revoked_at IS NULL",
            &[t(principal)],
        )?;
        return Ok(match rows.first().and_then(|r| text_of(&r[0])) {
            Some("agent") => Some("agent"),
            Some("teammate") => Some("teammate"),
            _ => None,
        });
    }
    if !valid_node(principal) {
        return Ok(None);
    }
    Ok(exists(
        tx,
        "SELECT 1 FROM nodes WHERE node_id=? AND revoked_at IS NULL",
        &[t(principal)],
    )?
    .then_some("node"))
}
fn grants_for<T: ReadTx + ?Sized>(tx: &T, principal: &str) -> rusqlite::Result<Vec<String>> {
    Ok(tx
        .query(
            "SELECT pattern FROM fleet_message_send_grants WHERE principal_id=? ORDER BY pattern",
            &[t(principal)],
        )?
        .iter()
        .filter_map(|r| text_of(&r[0]).map(String::from))
        .collect())
}
/// `permits`: one-way read visibility.
fn permits<T: ReadTx + ?Sized>(
    tx: &T,
    patterns: &[String],
    sender: &str,
    recipient: &str,
) -> rusqlite::Result<bool> {
    let Some(role) = classify(tx, sender)? else {
        return Ok(false);
    };
    if role == "node" {
        return Ok(true);
    }
    let class = if recipient.starts_with("agent:") {
        "agent:*"
    } else {
        "node:*"
    };
    Ok(recipient != sender
        && patterns
            .iter()
            .any(|p| p == recipient || p == "*" || p == class))
}
/// `require_granted`.
fn require_granted<T: ReadTx + ?Sized>(tx: &T, sender: &str, recipient: &str) -> Result<(), Fail> {
    let Some(sender_role) = classify(tx, sender)? else {
        return Err(Fail::inactive());
    };
    let recipient_role = classify(tx, recipient)?;
    let symmetric = sender_role == "teammate" || recipient_role == Some("teammate");
    let named = |source: &str, target: &str| -> rusqlite::Result<bool> {
        let class = if classify(tx, target)? == Some("node") {
            "node:*"
        } else {
            "agent:*"
        };
        Ok(source != target
            && grants_for(tx, source)?
                .iter()
                .any(|p| p == target || p == "*" || p == class))
    };
    let refused = if symmetric {
        !named(sender, recipient)? || !named(recipient, sender)?
    } else {
        sender_role != "node" && !permits(tx, &grants_for(tx, sender)?, sender, recipient)?
    };
    if refused {
        return Err(Fail::Forbidden {
            message: "recipient is not permitted by send grants".into(),
            code: "message_recipient_not_permitted",
        });
    }
    Ok(())
}
/// `cutoff` keeps rows a bounded prune has not deleted yet out of the count,
/// as Python's full `_prune_expired` would have.
fn require_recipient_cap<T: ReadTx + ?Sized>(
    tx: &T,
    sender: &str,
    recipient: &str,
    cutoff: &str,
) -> Result<(), Fail> {
    if classify(tx, sender)? == Some("node") {
        return Ok(());
    }
    if count(
        tx,
        "SELECT COUNT(*) FROM fleet_messages WHERE sender=? AND recipient=? AND acked_at IS NULL \
         AND created_at>=?",
        &[t(sender), t(recipient), t(cutoff)],
    )? >= AGENT_RECIPIENT_UNACKED_CAP
    {
        return Err(Fail::quota(
            "sender recipient message quota exceeded",
            "message_sender_recipient_quota",
            Some(recipient),
        ));
    }
    Ok(())
}

// ------------------------------------------------------------------ reads

/// What one reader snapshot answers for a request.
pub struct Request<'a> {
    pub caller: &'a str,
    /// The bearer's SHA-256 digest, Python's `authenticated_generation`.
    pub key: &'a str,
    pub raw: &'a Map<String, Json>,
    pub now: i64,
    pub policy: &'a Policy,
}
/// A `list` either answers at once or waits for mail.
pub enum Listed {
    Page(Reply),
    Wait {
        after: i64,
        unacked: bool,
        wait_ms: u64,
    },
}
fn inbox<T: ReadTx + ?Sized>(
    tx: &T,
    recipient: &str,
    after: i64,
    unacked: bool,
    now: i64,
    policy: &Policy,
) -> rusqlite::Result<Vec<Vec<Value>>> {
    tx.query(
        &format!(
            "SELECT {SENT_COLUMNS} FROM fleet_messages AS m WHERE m.recipient=?2 AND m.id>?3 AND m.created_at>=?1 {}ORDER BY m.id LIMIT 100",
            if unacked { "AND m.acked_at IS NULL " } else { "" }
        ),
        &[t(&policy.cutoff(now)), t(recipient), Value::Integer(after)],
    )
}
/// The part of a `list` body that `list` reads: `after`, `unacked` and
/// `wait_ms`. Any other key, and any value that is not a number or a boolean
/// (each fails its check either way), is dropped, so a padded body is freed
/// before the route releases its byte reservation and the poll waits.
pub struct ListFields(Map<String, Json>);
impl ListFields {
    pub fn new(mut raw: Map<String, Json>) -> Self {
        let mut kept = Map::new();
        for name in ["after", "unacked", "wait_ms"] {
            if let Some(value) = raw.remove(name) {
                let value = match value {
                    Json::Number(_) | Json::Bool(_) => value,
                    _ => Json::Null,
                };
                kept.insert(name.into(), value);
            }
        }
        Self(kept)
    }
    pub fn raw(&self) -> &Map<String, Json> {
        &self.0
    }
}
/// `list_messages` up to its first wait. The caller marks the wake epoch
/// before this snapshot begins, as Python's `_wake_epoch` call precedes its read.
pub fn list<T: ReadTx + ?Sized>(tx: &T, r: &Request) -> Result<Listed, Fail> {
    require_active(tx, r.caller)?;
    let Some(after) = r
        .raw
        .get("after")
        .map_or(Some(0), |v| integer(Some(v), true))
    else {
        return Err(Fail::error(
            "after must be a non-negative integer within SQLite's range",
        ));
    };
    let unacked = match r.raw.get("unacked") {
        None => false,
        Some(Json::Bool(b)) => *b,
        Some(_) => return Err(Fail::error("unacked must be a boolean")),
    };
    let wait_ms = match r.raw.get("wait_ms") {
        None => Some(0),
        v => integer(v, true).filter(|w| *w <= MAX_WAIT_MS),
    };
    let Some(wait_ms) = wait_ms else {
        return Err(Fail::error(&format!(
            "wait_ms must be an integer between 0 and {MAX_WAIT_MS}"
        )));
    };
    let rows = inbox(tx, r.caller, after, unacked, r.now, r.policy)?;
    if !rows.is_empty() || wait_ms == 0 {
        return Ok(Listed::Page((
            200,
            page(&rows, after, &purge_cutoff(r.now)),
        )));
    }
    Ok(Listed::Wait {
        after,
        unacked,
        wait_ms: wait_ms as u64,
    })
}
/// One re-read after a wake: the principal re-check, then the inbox. A
/// principal revoked or given a new credential while it waited gets the v1
/// auth error and no rows (plan step 4). `key` is the poll's bearer digest.
/// Python's `list` checks only that the principal is active, so a poll whose
/// token was rotated mid-wait is stricter here than on v1.
pub fn reread<T: ReadTx + ?Sized>(
    tx: &T,
    recipient: &str,
    key: &str,
    after: i64,
    unacked: bool,
    now: i64,
    policy: &Policy,
) -> Result<(bool, Reply), Fail> {
    require_active(tx, recipient)?;
    if !same_generation(tx, recipient, key)? {
        return Err(Fail::inactive());
    }
    let rows = inbox(tx, recipient, after, unacked, now, policy)?;
    Ok((
        !rows.is_empty(),
        (200, page(&rows, after, &purge_cutoff(now))),
    ))
}
/// `count`, `sent`, `sent_lookup`, `identity`, `keys_get` and `manifests_get`.
pub fn read<T: ReadTx + ?Sized>(tx: &T, r: &Request) -> Result<Reply, Fail> {
    let action = r.raw.get("action").and_then(Json::as_str).unwrap_or("");
    let keys = |allowed: &[&str]| r.raw.keys().all(|k| allowed.contains(&k.as_str()));
    let exact = |allowed: &[&str]| keys(allowed) && r.raw.len() == allowed.len();
    // Python validates identity peers before it opens the caller's snapshot.
    if action == "identity" {
        return identity(tx, r);
    }
    require_active(tx, r.caller)?;
    let cutoff = r.policy.cutoff(r.now);
    match action {
        "count" => {
            if !keys(&["action"]) {
                return Err(Fail::error("count accepts no arguments"));
            }
            let n = count(
                tx,
                "SELECT COUNT(*) FROM fleet_messages WHERE recipient=? AND acked_at IS NULL AND created_at>=?",
                &[t(r.caller), t(&cutoff)],
            )?;
            Ok((200, object(&[("unread_count", n.to_string())])))
        }
        "sent" => {
            if !keys(&["action", "after", "ids"])
                || (r.raw.contains_key("after") && r.raw.contains_key("ids"))
            {
                return Err(Fail::error("sent accepts either after or ids"));
            }
            let Some(after) = r
                .raw
                .get("after")
                .map_or(Some(0), |v| integer(Some(v), true))
            else {
                return Err(Fail::error(
                    "after must be a non-negative integer within SQLite's range",
                ));
            };
            let mut params = vec![t(&cutoff), t(r.caller)];
            let condition = if let Some(ids) = r.raw.get("ids") {
                let ids: Option<Vec<i64>> = ids
                    .as_array()
                    .filter(|a| (1..=100).contains(&a.len()))
                    .and_then(|a| a.iter().map(|i| integer(Some(i), false)).collect());
                let Some(ids) = ids else {
                    return Err(Fail::error(
                        "ids must contain 1 to 100 positive integers within SQLite's range",
                    ));
                };
                let marks = vec!["?"; ids.len()].join(",");
                params.extend(ids.into_iter().map(Value::Integer));
                format!("m.id IN ({marks})")
            } else {
                params.push(Value::Integer(after));
                "m.id>?".into()
            };
            let rows = tx.query(
                &format!(
                    "SELECT {SENT_COLUMNS} FROM fleet_messages AS m WHERE m.sender=?2 AND m.created_at>=?1 AND {condition} ORDER BY m.id LIMIT 100"
                ),
                &params,
            )?;
            Ok((200, page(&rows, after, &purge_cutoff(r.now))))
        }
        "sent_lookup" => {
            require_generation(tx, r.caller, r.key, "lookup credential generation changed")?;
            if !exact(&["action", "client_id"]) {
                return Err(Fail::error("sent_lookup requires only client_id"));
            }
            let client_id = text(r.raw.get("client_id"), "client_id", 64, true)?.unwrap();
            let found = tx.query(
                "SELECT id FROM fleet_messages WHERE sender=? AND client_id=? AND created_at>=?",
                &[t(r.caller), t(&client_id), t(&cutoff)],
            )?;
            let found = match found.first().map(|row| &row[0]) {
                Some(Value::Integer(id)) => fetch(tx, *id, r.now, r.policy)?,
                _ => None,
            };
            Ok(match found {
                Some(m) => (200, object(&[("message", m)])),
                None => (
                    404,
                    object(&[
                        ("error", quote("message not found")),
                        ("code", quote("not_found")),
                    ]),
                ),
            })
        }
        "keys_get" => keys_get(tx, r),
        "manifests_get" => {
            require_generation(
                tx,
                r.caller,
                r.key,
                "mesh read credential generation changed",
            )?;
            if !exact(&["action"]) {
                return Err(Fail::error("manifests_get takes no arguments"));
            }
            // Statements are stored as `json.dumps` text, which a load and dump reproduces.
            let rows = tx.query(
                "SELECT kind,statement FROM fleet_mesh_statements ORDER BY kind",
                &[],
            )?;
            let statements: Vec<(String, String)> = rows
                .iter()
                .map(|r| {
                    (
                        text_of(&r[0]).unwrap_or_default().to_string(),
                        text_of(&r[1]).unwrap_or("null").to_string(),
                    )
                })
                .collect();
            let fields: Vec<(&str, String)> = statements
                .iter()
                .map(|(k, v)| (k.as_str(), v.clone()))
                .collect();
            Ok((200, object(&[("statements", object(&fields))])))
        }
        _ => Err(Fail::Python),
    }
}
fn identity<T: ReadTx + ?Sized>(tx: &T, r: &Request) -> Result<Reply, Fail> {
    if r.raw.len() == 1 && r.raw.contains_key("action") {
        require_active(tx, r.caller)?;
        return Ok((200, object(&[("principal", quote(r.caller))])));
    }
    let peers: Option<Vec<&str>> = r
        .raw
        .get("peers")
        .and_then(Json::as_array)
        .filter(|p| (1..=16).contains(&p.len()))
        .and_then(|p| p.iter().map(Json::as_str).collect());
    let valid = r.raw.len() == 2
        && peers.as_ref().is_some_and(|peers| {
            peers
                .iter()
                .all(|p| p.strip_prefix("agent:").is_some_and(principal_name))
                && peers
                    .iter()
                    .collect::<std::collections::BTreeSet<_>>()
                    .len()
                    == peers.len()
                && !peers.contains(&r.caller)
        });
    if !valid {
        return Err(Fail::code("invalid identity arguments", "invalid_argument"));
    }
    require_active(tx, r.caller)?;
    if !r.caller.starts_with("agent:") {
        return Err(Fail::Forbidden {
            message: "identity peers requires an agent principal".into(),
            code: "message_identity_peers_unsupported",
        });
    }
    if !same_generation(tx, r.caller, r.key)? {
        return Err(Fail::inactive());
    }
    let epochs = |principal: &str| -> rusqlite::Result<Option<(String, String)>> {
        let rows = tx.query(
            "SELECT authorization_enrollment_epoch,grant_epoch FROM fleet_message_principals \
             WHERE principal_id=? AND revoked_at IS NULL",
            &[t(principal)],
        )?;
        let epoch = |v: &Value| text_of(v).filter(|s| label_epoch(s)).map(String::from);
        Ok(rows
            .first()
            .and_then(|row| Some((epoch(&row[0])?, epoch(&row[1])?))))
    };
    let Some((enrollment, grant)) = epochs(r.caller)? else {
        return Err(Fail::inactive());
    };
    let caller_grants = grants_for(tx, r.caller)?;
    let mut records = Vec::new();
    for peer in peers.unwrap() {
        let peer_epochs = epochs(peer)?;
        let authorized = peer_epochs.is_some()
            && permits(tx, &caller_grants, r.caller, peer)?
            && permits(tx, &grants_for(tx, peer)?, peer, r.caller)?;
        let shown = |pick: fn(&(String, String)) -> &String| match (&peer_epochs, authorized) {
            (Some(e), true) => quote(pick(e)),
            _ => "null".into(),
        };
        records.push(object(&[
            ("principal", quote(peer)),
            ("authorized", authorized.to_string()),
            ("enrollment_epoch", shown(|e| &e.0)),
            ("grant_epoch", shown(|e| &e.1)),
        ]));
    }
    Ok((
        200,
        object(&[
            ("schema", quote("brigade.message_authorization.v1")),
            ("principal", quote(r.caller)),
            ("enrollment_epoch", quote(&enrollment)),
            ("grant_epoch", quote(&grant)),
            ("peers", array(records)),
        ]),
    ))
}
/// `[A-Za-z0-9_-]{16,128}`.
fn label_epoch(s: &str) -> bool {
    (16..=128).contains(&s.len())
        && s.bytes()
            .all(|c| c.is_ascii_alphanumeric() || c == b'_' || c == b'-')
}
fn keys_get<T: ReadTx + ?Sized>(tx: &T, r: &Request) -> Result<Reply, Fail> {
    require_generation(
        tx,
        r.caller,
        r.key,
        "mesh read credential generation changed",
    )?;
    let kid = r.raw.get("kid").and_then(Json::as_str);
    let Some(kid) = kid.filter(|k| r.raw.len() == 2 && k.starts_with("k1_")) else {
        return Err(Fail::error("keys_get requires a key id"));
    };
    if !sealed::kid(&Json::String(kid.into())) {
        return Err(Fail::error("invalid mesh binary format"));
    }
    let rows = tx.query(
        "SELECT principal_id,kid,record,state,created_at,retired_at,revoked_at,compromised_from \
         FROM fleet_message_keys WHERE kid=?",
        &[t(kid)],
    )?;
    let Some(row) = rows.first() else {
        return Ok(error_reply(404, "key not found"));
    };
    let owner = text_of(&row[0]).unwrap_or_default();
    let own = owner == r.caller && classify(tx, r.caller)?.is_some();
    if !own {
        let patterns = if r.caller.starts_with("agent:") {
            grants_for(tx, r.caller)?
        } else {
            vec![]
        };
        if !permits(tx, &patterns, r.caller, owner)? {
            return Ok(error_reply(404, "key not found"));
        }
    }
    let names = [
        "principal_id",
        "kid",
        "record",
        "state",
        "created_at",
        "retired_at",
        "revoked_at",
        "compromised_from",
    ];
    // The record was stored as `json.dumps` text, which a load and dump reproduces.
    let fields: Vec<(&str, String)> = names
        .iter()
        .zip(row)
        .map(|(k, v)| {
            (
                *k,
                if *k == "record" {
                    text_of(v).unwrap_or("null").to_string()
                } else {
                    json_value(v)
                },
            )
        })
        .collect();
    Ok((200, object(&[("key", object(&fields))])))
}

// ----------------------------------------------------------------- writes

/// Random values a write may need, drawn before submission so a replayed
/// command makes the same decisions.
#[derive(Clone, Debug, Default)]
pub struct Seeds {
    /// `secrets.token_hex(16)` for a missing plaintext `client_id`.
    pub client_id: String,
    /// `uuid4().hex` for a `message.created` event.
    pub event: String,
    /// `question_id` and the operator message's `client_id`.
    pub question: [String; 2],
}
/// One message write: `send`, `reply`, `ack` or a `question_*` action.
pub struct Write {
    pub caller: String,
    pub key: String,
    pub raw: Map<String, Json>,
    pub policy: Policy,
    pub clock: Option<String>,
    pub seeds: Seeds,
    pub events: bool,
    /// `BRIGADE_FLEET_OPERATOR_RECIPIENT`, read when the request arrived.
    pub operator: String,
    pub wake: Arc<wake::Wake>,
    pub notified: Mutex<Vec<String>>,
}
/// `{"python": true}` tells the route to forward the request upstream.
pub fn python_answer() -> Json {
    json!({"python": true})
}
pub fn pack((status, body): Reply) -> Json {
    json!({"status": status, "body": body})
}
impl Command for Write {
    fn apply(&self, tx: &dyn WriteTx) -> Result<Json, ApplyError> {
        self.notified.lock().unwrap().clear();
        if crate::schema::newer_than_supported(tx)? {
            return Err(ApplyError::Refused(pack(error_reply(
                503,
                crate::schema::NEWER_SCHEMA,
            ))));
        }
        let mut op = Op {
            tx,
            w: self,
            now: crate::wire::epoch_us(self.clock.as_deref()),
            notify: Vec::new(),
        };
        let result = require_active(tx, &self.caller).and_then(|()| op.dispatch());
        match result {
            Ok(reply) => {
                *self.notified.lock().unwrap() = op.notify;
                Ok(pack(reply))
            }
            Err(Fail::Python) => Err(ApplyError::Refused(python_answer())),
            Err(Fail::Sql(e)) => Err(e.into()),
            Err(fail) => Err(ApplyError::Refused(pack(fail.reply().unwrap()))),
        }
    }
    fn post_commit(&self, _external_change: bool) {
        for recipient in self.notified.lock().unwrap().drain(..) {
            self.wake.notify(&recipient);
        }
    }
}
/// One application of a `Write` inside the writer transaction.
struct Op<'a> {
    tx: &'a dyn WriteTx,
    w: &'a Write,
    now: i64,
    notify: Vec<String>,
}
impl Op<'_> {
    fn dispatch(&mut self) -> Result<Reply, Fail> {
        let w = self.w;
        let raw = &w.raw;
        let action = raw.get("action").and_then(Json::as_str).unwrap_or("");
        let caller = w.caller.as_str();
        match action {
            "question_create" | "question_status" | "question_desktop" => questions::handle(self),
            "ack" => self.ack(),
            "reply" | "send" if action == "reply" || get(raw, "reply_to").is_some() => {
                require_generation(
                    self.tx,
                    caller,
                    &self.w.key,
                    "reply credential generation changed",
                )?;
                let message = if action == "reply" {
                    self.reply()?
                } else {
                    let reply_to = raw.get("reply_to").cloned();
                    let extras = metadata(raw, caller)?;
                    self.send(raw, reply_to.as_ref(), extras)?
                };
                Ok(created(message))
            }
            "send" => {
                require_generation(
                    self.tx,
                    caller,
                    &self.w.key,
                    "send credential generation changed",
                )?;
                let extras = metadata(raw, caller)?;
                // Unlike a reply, a plain send answers 201 even when `_fetch` finds nothing.
                let message = self.send(raw, None, extras)?;
                Ok((
                    201,
                    object(&[("message", message.unwrap_or_else(|| "{}".into()))]),
                ))
            }
            _ => Err(Fail::Python),
        }
    }
    fn cutoff(&self) -> String {
        self.w.policy.cutoff(self.now)
    }
    fn fetch(&self, id: i64) -> rusqlite::Result<Option<String>> {
        fetch(self.tx, id, self.now, &self.w.policy)
    }
    /// `_prune_expired`.
    fn prune(&self) -> rusqlite::Result<()> {
        prune(self.tx, self.now, &self.w.policy, PURGE_BATCH)
    }
    fn ack(&mut self) -> Result<Reply, Fail> {
        self.prune()?;
        let Some(id) = integer(self.w.raw.get("id"), false) else {
            return Err(Fail::error(
                "id must be a positive integer within SQLite's range",
            ));
        };
        let caller = self.w.caller.as_str();
        let now = iso_us(self.now);
        self.tx.execute(
            "UPDATE fleet_messages SET acked_at=COALESCE(acked_at,?), acked_by=COALESCE(acked_by,?) \
             WHERE id=? AND recipient=? AND created_at>=?",
            &[t(&now), t(caller), Value::Integer(id), t(caller), t(&self.cutoff())],
        )?;
        let message = if exists(
            self.tx,
            "SELECT 1 FROM fleet_messages WHERE id=? AND recipient=?",
            &[Value::Integer(id), t(caller)],
        )? {
            self.fetch(id)?
        } else {
            None
        };
        Ok(match message {
            Some(m) => (200, object(&[("message", m)])),
            None => error_reply(404, "message not found"),
        })
    }
    /// `reply`: a sealed reply, a plaintext reply, or the retry of an
    /// accepted reply whose parent retention removed.
    fn reply(&mut self) -> Result<Option<String>, Fail> {
        let w = self.w;
        let raw = &w.raw;
        let sender = w.caller.as_str();
        let invalid_id = || Fail::error("id must be a positive integer within SQLite's range");
        if raw.contains_key("envelope") {
            let id = integer(raw.get("id"), false).ok_or_else(invalid_id)?;
            let extras = metadata(raw, sender)?;
            return self.send(raw, Some(&Json::from(id)), extras);
        }
        metadata(raw, sender)?;
        self.prune()?;
        let id = integer(raw.get("id"), false).ok_or_else(invalid_id)?;
        let original = self.parent(id)?.filter(|o| o.recipient == sender);
        let mut fields = Map::new();
        let field = |k: &str| raw.get(k).cloned().unwrap_or(Json::Null);
        match original {
            None => {
                let Some(client_id) =
                    text(raw.get("client_id"), "client_id", 64, false)?.filter(|c| !c.is_empty())
                else {
                    return Ok(None);
                };
                // Python's full prune has cleared `reply_to` on every child of
                // an expired parent. A bounded prune can leave that parent in
                // place, so a link to an expired row counts as cleared here.
                let rows = self.tx.query(
                    "SELECT recipient,subject,reference,priority,thread FROM fleet_messages \
                     WHERE sender=?1 AND client_id=?2 AND (reply_to IS NULL OR reply_to IN \
                     (SELECT id FROM fleet_messages WHERE created_at<?3)) AND created_at>=?3",
                    &[t(sender), t(&client_id), t(&self.cutoff())],
                )?;
                let Some(accepted) = rows.first() else {
                    return Ok(None);
                };
                let sql_json = |v: &Value| match v {
                    Value::Text(s) => Json::String(s.clone()),
                    Value::Integer(i) => Json::from(*i),
                    _ => Json::Null,
                };
                let or = |k: &str, v: &Value| get(raw, k).cloned().unwrap_or_else(|| sql_json(v));
                fields.insert("to".into(), sql_json(&accepted[0]));
                fields.insert(
                    "subject".into(),
                    match get(raw, "subject") {
                        Some(s) => s.clone(),
                        None => Json::String(text_of(&accepted[1]).unwrap_or("None").into()),
                    },
                );
                fields.insert("body".into(), field("body"));
                fields.insert("ref".into(), or("ref", &accepted[2]));
                fields.insert(
                    "priority".into(),
                    raw.get("priority")
                        .cloned()
                        .unwrap_or_else(|| sql_json(&accepted[3])),
                );
                fields.insert("client_id".into(), Json::String(client_id));
                fields.insert("enqueued_at".into(), field("enqueued_at"));
                fields.insert(
                    "thread".into(),
                    raw.get("thread")
                        .cloned()
                        .unwrap_or_else(|| sql_json(&accepted[4])),
                );
                for k in ["sender_label", "kind"] {
                    if let Some(v) = raw.get(k) {
                        fields.insert(k.into(), v.clone());
                    }
                }
            }
            Some(original) => {
                let subject = match get(raw, "subject") {
                    None => {
                        let prefixed = format!("Re: {}", original.subject);
                        Json::String(if prefixed.len() <= SUBJECT_LIMIT {
                            prefixed
                        } else {
                            original.subject.clone()
                        })
                    }
                    s => Json::String(text(s, "subject", SUBJECT_LIMIT, true)?.unwrap()),
                };
                let reference = match get(raw, "ref") {
                    None => original.reference.clone().map_or(Json::Null, Json::String),
                    r => text(r, "ref", REF_LIMIT, false)?.map_or(Json::Null, Json::String),
                };
                fields.insert("to".into(), Json::String(original.sender.clone()));
                fields.insert("subject".into(), subject);
                fields.insert("body".into(), field("body"));
                fields.insert("ref".into(), reference);
                fields.insert(
                    "priority".into(),
                    raw.get("priority")
                        .cloned()
                        .unwrap_or_else(|| "normal".into()),
                );
                fields.insert("client_id".into(), field("client_id"));
                fields.insert("enqueued_at".into(), field("enqueued_at"));
                fields.insert(
                    "thread".into(),
                    raw.get("thread").cloned().unwrap_or_else(|| {
                        original.thread.clone().map_or(Json::Null, Json::String)
                    }),
                );
                fields.insert("sender_label".into(), field("sender_label"));
                fields.insert("kind".into(), field("kind"));
            }
        }
        let extras = metadata(&fields, sender)?;
        let message = self.send(&fields, Some(&Json::from(id)), extras)?;
        // `_fetch(conn, message["id"])` after the insert or the idempotent return.
        Ok(message)
    }
    /// The stored parent of a reply, as `_fetch` returns it.
    fn parent(&self, id: i64) -> rusqlite::Result<Option<Parent>> {
        let rows = self.tx.query(
            &format!(
                "SELECT {SENT_COLUMNS} FROM fleet_messages AS m WHERE m.id=?2 AND m.created_at>=?1"
            ),
            &[t(&self.cutoff()), Value::Integer(id)],
        )?;
        let purge = purge_cutoff(self.now);
        Ok(rows.first().map(|row| {
            let owned = |i: usize| text_of(&row[i]).map(String::from);
            let content_state = owned(19).map(|state| {
                let expired = state == "sealed"
                    && text_of(&row[12]).is_some_and(|acked| acked <= purge.as_str());
                if expired && owned(17).as_deref().and_then(sealed::tombstone).is_some() {
                    "content_expired".to_string()
                } else {
                    state
                }
            });
            Parent {
                sender: owned(1).unwrap_or_default(),
                recipient: owned(2).unwrap_or_default(),
                subject: owned(3).unwrap_or_default(),
                reference: owned(5),
                client_id: owned(7).unwrap_or_default(),
                thread: owned(15),
                kind: owned(16)
                    .filter(|k| !k.is_empty())
                    .unwrap_or_else(|| "message".into()),
                content_state,
            }
        }))
    }
    /// `_send`: validation, then Python's checks in order, the insert and the event.
    fn send(
        &mut self,
        raw: &Map<String, Json>,
        reply_to: Option<&Json>,
        extras: Vec<(&'static str, String)>,
    ) -> Result<Option<String>, Fail> {
        let w = self.w;
        let sender = w.caller.clone();
        let policy = &w.policy;
        let reply_to = reply_to.filter(|v| !v.is_null());
        let mut extras = extras;
        let envelope = match raw.get("envelope") {
            Some(_) => Some(sealed::prepare(raw, &sender, reply_to.is_some(), policy)?),
            None => None,
        };
        let (recipient, subject, body, reference, priority, client_id, enqueued, hash);
        if let Some(value) = &envelope {
            let field = |k: &str| value[k].as_str().unwrap_or_default().to_string();
            recipient = field("to");
            priority = field("priority");
            client_id = field("client_id");
            subject = String::new();
            body = String::new();
            reference = None;
            extras = vec![("kind", field("kind"))];
            enqueued = self.enqueued_at(value.get("created_at"))?;
            hash = sha256_hex(raw["envelope"].as_str().unwrap().as_bytes());
        } else {
            if policy.require_sealed {
                return Err(Fail::code(
                    "hub requires sealed messages",
                    "message_sealing_required",
                ));
            }
            recipient = text(raw.get("to"), "to", 256, true)?.unwrap();
            subject = text(raw.get("subject"), "subject", SUBJECT_LIMIT, true)?.unwrap();
            body = text(raw.get("body"), "body", BODY_LIMIT, true)?.unwrap();
            reference = text(raw.get("ref"), "ref", REF_LIMIT, false)?;
            client_id = text(raw.get("client_id"), "client_id", 64, false)?
                .filter(|c| !c.is_empty())
                .unwrap_or_else(|| w.seeds.client_id.clone());
            enqueued = self.enqueued_at(raw.get("enqueued_at"))?;
            priority = match raw.get("priority") {
                None => "normal".into(),
                Some(Json::String(p))
                    if matches!(p.as_str(), "low" | "normal" | "high" | "urgent") =>
                {
                    p.clone()
                }
                Some(_) => {
                    return Err(Fail::error("priority must be low, normal, high, or urgent"));
                }
            };
            // Python hashes the raw reply_to; only a valid integer survives to compare.
            hash = payload_digest(
                [&recipient, &subject, &body],
                reference.as_deref(),
                &priority,
                reply_to.and_then(|v| integer(Some(v), false)),
                &extras,
            );
        }
        self.prune()?;
        require_active(self.tx, &sender)?;
        let invalid_reply =
            || Fail::error("reply_to must be a positive integer within SQLite's range");
        let mut parent_missing = false;
        if let (Some(reply_to), None) = (reply_to, &envelope) {
            let id = integer(Some(reply_to), false).ok_or_else(invalid_reply)?;
            let original = self
                .parent(id)?
                .filter(|o| o.recipient == sender && o.sender == recipient);
            parent_missing = original.is_none();
            if original.is_some_and(|o| {
                matches!(
                    o.content_state.as_deref(),
                    Some("sealed" | "content_expired")
                )
            }) {
                return Err(Fail::code(
                    "a sealed parent requires a sealed reply",
                    "message_sealing_required",
                ));
            }
        }
        let cutoff = self.cutoff();
        let mut existing = self.tx.query(
            "SELECT id,payload_hash,created_at<? FROM fleet_messages WHERE sender=? AND client_id=?",
            &[t(&cutoff), t(&sender), t(&client_id)],
        )?;
        // A bounded prune can leave this row expired but present. Python's
        // full prune would have removed it, so remove it the same way here
        // and let the send insert fresh, as it would after that prune.
        if let Some(row) = existing.first().filter(|row| row[2] == Value::Integer(1)) {
            let id = row[0].clone();
            self.tx.execute(
                "UPDATE fleet_messages SET reply_to=NULL WHERE reply_to=?",
                std::slice::from_ref(&id),
            )?;
            self.tx
                .execute("DELETE FROM fleet_messages WHERE id=?", &[id])?;
            existing.clear();
        }
        if let Some(row) = existing.first() {
            let id = match row[0] {
                Value::Integer(i) => i,
                _ => 0,
            };
            let stored = text_of(&row[1]);
            if stored.is_some_and(|s| s != hash) {
                return Err(Fail::Conflict {
                    message: "client_id was already used for a different message".into(),
                    code: Some("message_client_id_conflict"),
                    id: Some(id),
                });
            }
            if parent_missing && stored.is_none() {
                return Err(Fail::error(
                    "reply_to must identify an inbox message from the recipient",
                ));
            }
            let row = self.fetch(id)?;
            self.notify.push(recipient);
            return Ok(row);
        }
        if let Some(reply_to) = reply_to {
            let id = integer(Some(reply_to), false).ok_or_else(invalid_reply)?;
            let Some(original) = self
                .parent(id)?
                .filter(|o| o.recipient == sender && o.sender == recipient)
            else {
                return Err(Fail::error(
                    "reply_to must identify an inbox message from the recipient",
                ));
            };
            if let Some(value) = &envelope {
                if original.kind == "operator_question" {
                    return Err(Fail::code(
                        "sealed replies to legacy operator questions are unavailable until sealed question support",
                        "sealed_question_reply_unavailable",
                    ));
                }
                let link = &value["in_reply_to"];
                if link["from"] != original.sender.as_str()
                    || link["client_id"] != original.client_id.as_str()
                {
                    return Err(Fail::code(
                        "signed in_reply_to must match the routed parent sender and client_id",
                        "envelope_reply_mismatch",
                    ));
                }
            }
        }
        require_granted(self.tx, &sender, &recipient)?;
        if !active(self.tx, &recipient)? {
            if exists(
                self.tx,
                "SELECT 1 FROM nodes WHERE node_id=? AND revoked_at IS NOT NULL \
                 UNION ALL SELECT 1 FROM fleet_message_principals WHERE principal_id=? AND revoked_at IS NOT NULL \
                 LIMIT 1",
                &[t(&recipient), t(&recipient)],
            )? {
                return Err(Fail::Inactive(recipient));
            }
            return Err(Fail::error("recipient is not an active hub principal"));
        }
        require_recipient_cap(self.tx, &sender, &recipient, &cutoff)?;
        if let Some(value) = &envelope {
            // An installed hub identity needs `_installed`'s signed-metadata
            // checks, which stay with the Python upstream.
            if exists(
                self.tx,
                "SELECT 1 FROM hub_meta WHERE key IN ('identity','identity_marks','migration')",
                &[],
            )? {
                return Err(Fail::Python);
            }
            sealed::require_current_keys(self.tx, value)?;
            let size = raw["envelope"].as_str().unwrap().len() as i64;
            sealed::admit(self.tx, &sender, size, policy, &cutoff)?;
        }
        if count(
            self.tx,
            "SELECT COUNT(*) FROM fleet_messages WHERE recipient=? AND acked_at IS NULL AND created_at>=?",
            &[t(&recipient), t(&cutoff)],
        )? >= UNACKED_QUOTA
        {
            return Err(Fail::quota(
                "recipient message quota exceeded",
                "message_recipient_quota",
                Some(&recipient),
            ));
        }
        let received = iso_us(self.now);
        if count(
            self.tx,
            "SELECT COUNT(*) FROM fleet_message_sends WHERE sender=? AND received_at>=?",
            &[t(&sender), t(&iso_us(self.now - DAY_US))],
        )? >= SENDER_DAILY_QUOTA
        {
            return Err(Fail::quota(
                "sender message quota exceeded",
                "message_sender_quota",
                None,
            ));
        }
        let extra = |k: &str| {
            extras
                .iter()
                .find(|(name, _)| *name == k)
                .map_or(Value::Null, |(_, v)| t(v))
        };
        let envelope_text = envelope.as_ref().map(|_| raw["envelope"].as_str().unwrap());
        let reply_id = reply_to.and_then(|v| integer(Some(v), false));
        self.tx.execute(
            "INSERT INTO fleet_messages(sender,recipient,subject,body,reference,priority,client_id,payload_hash,reply_to,is_reply,created_at,received_at,sender_label,thread,kind,envelope,envelope_bytes,content_state,sealed_reply_to) \
             VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            &[
                t(&sender),
                t(&recipient),
                t(&subject),
                t(&body),
                reference.as_deref().map_or(Value::Null, t),
                t(&priority),
                t(&client_id),
                t(&hash),
                reply_id.map_or(Value::Null, Value::Integer),
                Value::Integer(i64::from(reply_id.is_some())),
                t(&enqueued),
                t(&received),
                extra("sender_label"),
                extra("thread"),
                extra("kind"),
                envelope_text.map_or(Value::Null, t),
                envelope_text.map_or(Value::Null, |e| Value::Integer(e.len() as i64)),
                envelope_text.map_or(Value::Null, |_| t("sealed")),
                match (envelope_text, reply_id) {
                    (Some(_), Some(id)) => Value::Integer(id),
                    _ => Value::Null,
                },
            ],
        )?;
        let id = count(self.tx, "SELECT last_insert_rowid()", &[])?;
        self.tx.execute(
            "INSERT INTO fleet_message_sends(sender,client_id,received_at) VALUES(?,?,?)",
            &[t(&sender), t(&client_id), t(&received)],
        )?;
        let row = self.fetch(id)?;
        if row.is_some() {
            self.message_created(
                id,
                &sender,
                &recipient,
                &priority,
                &enqueued,
                &extra("kind"),
            )?;
        }
        self.notify.push(recipient);
        Ok(row)
    }
    /// `_enqueued_at`. Spellings outside the common ISO forms stay with Python.
    fn enqueued_at(&self, value: Option<&Json>) -> Result<String, Fail> {
        let Some(value) = value.filter(|v| !v.is_null()) else {
            return Ok(iso_us(self.now));
        };
        let text = text(Some(value), "enqueued_at", 64, true)?.unwrap();
        let stamp = match stamp(&text) {
            Stamp::Utc(us) => us,
            Stamp::Naive => return Err(Fail::error("enqueued_at must include a UTC offset")),
            Stamp::Python => return Err(Fail::Python),
        };
        if stamp < self.now - self.w.policy.retention_days * DAY_US {
            return Err(Fail::error("message expired before delivery"));
        }
        if stamp > self.now + 5 * 60 * SECOND_US {
            return Err(Fail::Future("enqueued_at is too far in the future".into()));
        }
        Ok(iso_us(stamp))
    }
    /// `fleet_hub_events.record_message_created` and `_append`.
    fn message_created(
        &self,
        id: i64,
        sender: &str,
        recipient: &str,
        priority: &str,
        created_at: &str,
        kind: &Value,
    ) -> rusqlite::Result<()> {
        if !self.w.events {
            return Ok(());
        }
        let time = crate::claims::raw_time(self.w.clock.as_deref());
        if !exists(
            self.tx,
            "SELECT 1 FROM fleet_event_subscriptions WHERE name=? AND (expires_at IS NULL OR expires_at>?) LIMIT 1",
            &[t("message.created"), Value::Real(time)],
        )? {
            return Ok(());
        }
        if count(
            self.tx,
            "SELECT COUNT(*) FROM fleet_events WHERE name=? AND producer=? AND created_at>?",
            &[t("message.created"), t(sender), Value::Real(time - 3600.0)],
        )? >= 120
        {
            return Ok(());
        }
        let kind = text_of(kind).filter(|k| !k.is_empty()).unwrap_or("message");
        let data = format!(
            "{{\"message_id\":{},\"recipient\":{},\"sender\":{},\"priority\":{},\"created_at\":{},\"kind\":{}}}",
            quote(&id.to_string()),
            quote(recipient),
            quote(sender),
            quote(priority),
            quote(created_at),
            quote(kind)
        );
        self.tx.execute(
            "INSERT OR IGNORE INTO fleet_events(event_id,name,subject,producer,data,created_at) VALUES(?,?,?,?,?,?)",
            &[
                t(&format!("evt_{}", self.w.seeds.event)),
                t("message.created"),
                t(recipient),
                t(sender),
                t(&data),
                Value::Real(time),
            ],
        )?;
        Ok(())
    }
}
fn created(message: Option<String>) -> Reply {
    match message {
        Some(m) => (201, object(&[("message", m)])),
        None => error_reply(404, "message not found"),
    }
}
struct Parent {
    sender: String,
    recipient: String,
    subject: String,
    reference: Option<String>,
    client_id: String,
    thread: Option<String>,
    kind: String,
    content_state: Option<String>,
}

/// `purge_content` with a row bound.
fn purge(tx: &dyn WriteTx, now: i64, limit: i64) -> rusqlite::Result<usize> {
    let rows = tx.query(
        "SELECT id,envelope FROM fleet_messages WHERE content_state='sealed' AND acked_at<=? \
         ORDER BY acked_at,id LIMIT ?",
        &[t(&purge_cutoff(now)), Value::Integer(limit)],
    )?;
    for row in &rows {
        let Some(tombstone) = text_of(&row[1]).and_then(sealed::tombstone) else {
            continue;
        };
        tx.execute(
            "UPDATE fleet_messages SET envelope=?,envelope_bytes=?,content_state='content_expired' WHERE id=?",
            &[t(&tombstone), Value::Integer(tombstone.len() as i64), row[0].clone()],
        )?;
    }
    Ok(rows.len())
}
/// `_prune_expired` with a purge bound (Python's 32, or the sweep's 256) and
/// at most `RETENTION_BATCH` expired rows deleted per command.
fn prune(tx: &dyn WriteTx, now: i64, policy: &Policy, limit: i64) -> rusqlite::Result<()> {
    retain(tx, now, policy, limit, RETENTION_BATCH)
}
fn retain(
    tx: &dyn WriteTx,
    now: i64,
    policy: &Policy,
    limit: i64,
    batch: i64,
) -> rusqlite::Result<()> {
    purge(tx, now, limit)?;
    let cutoff = policy.cutoff(now);
    // Both statements name the same oldest `batch` expired rows: the UPDATE
    // does not change `created_at`, so the subquery reads the same set twice
    // in this transaction, through the `created_at` index.
    let expired = "SELECT id FROM fleet_messages WHERE created_at < ?1 \
                   ORDER BY created_at,id LIMIT ?2";
    let params = [t(&cutoff), Value::Integer(batch)];
    tx.execute(
        &format!("UPDATE fleet_messages SET reply_to=NULL WHERE reply_to IN ({expired})"),
        &params,
    )?;
    tx.execute(
        &format!("DELETE FROM fleet_messages WHERE id IN ({expired})"),
        &params,
    )?;
    tx.execute(
        "DELETE FROM fleet_message_sends WHERE received_at < ?",
        &[t(&iso_us(now - DAY_US))],
    )?;
    Ok(())
}
/// The maintenance sweep: Python's retention writes with a 256-row purge bound.
pub struct Sweep {
    pub policy: Policy,
    pub clock: Option<String>,
}
impl Command for Sweep {
    fn apply(&self, tx: &dyn WriteTx) -> Result<Json, ApplyError> {
        if crate::schema::newer_than_supported(tx)? {
            return Err(ApplyError::Refused(Json::Null));
        }
        let now = crate::wire::epoch_us(self.clock.as_deref());
        prune(tx, now, &self.policy, SWEEP_BATCH)?;
        Ok(Json::Null)
    }
}

/// How `_enqueued_at` reads a client timestamp.
enum Stamp {
    Utc(i64),
    Naive,
    Python,
}
/// `datetime.fromisoformat(text.replace("Z", "+00:00"))` for the common
/// spellings: `YYYY-MM-DDTHH:MM:SS`, an optional 3- or 6-digit fraction and
/// an optional `Z` or `+HH:MM` offset. Anything else, including a valid
/// spelling near the end of the calendar, is left to Python.
fn stamp(text: &str) -> Stamp {
    let Some(([year, month, day, hour, minute, second], micros, rest)) =
        sealed::civil(text.as_bytes(), &[3, 6])
    else {
        return Stamp::Python;
    };
    if !(2..=9998).contains(&year) {
        return Stamp::Python;
    }
    let offset = match rest {
        [] => return Stamp::Naive,
        b"Z" => 0,
        [sign @ (b'+' | b'-'), h1, h2, b':', m1, m2]
            if [h1, h2, m1, m2].iter().all(|b| b.is_ascii_digit()) =>
        {
            let h = i64::from(h1 - b'0') * 10 + i64::from(h2 - b'0');
            let m = i64::from(m1 - b'0') * 10 + i64::from(m2 - b'0');
            if h > 23 || m > 59 {
                return Stamp::Python;
            }
            let minutes = h * 60 + m;
            if *sign == b'-' { -minutes } else { minutes }
        }
        _ => return Stamp::Python,
    };
    let days = crate::wire::days_from_civil(year, month, day);
    let local = ((days * 24 + hour) * 60 + minute) * 60 + second;
    Stamp::Utc((local - offset * 60) * SECOND_US + micros)
}

#[cfg(test)]
mod tests;
