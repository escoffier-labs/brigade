//! `fleet_hub_questions`: credential-generation-bound operator questions. The
//! `fleet_questions_reply` trigger Python installed answers them on insert.
use super::{
    DAY_US, Fail, Op, Reply, SECOND_US, count, exists, generation, integer, quote_utf8, sha256_hex,
    t, text, text_of,
};
use crate::claims::json_value;
use crate::wire::{iso_us, object, quote};
use rusqlite::types::Value;
use serde_json::{Map, Value as Json};
use subtle::ConstantTimeEq;

const QUESTION_LIMIT: usize = 4096;
const COLUMNS: &str = "question_id,caller,request_id,operator,message_id,deadline,status,answer,answer_id,source,answered_by,answered_at,late_answer_message_id,late_answered_at,operator_reply_after_desktop_message_id,operator_reply_after_desktop_at";

/// `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`.
fn request_id(s: &str) -> bool {
    let b = s.as_bytes();
    !b.is_empty()
        && b.len() <= 64
        && b[0].is_ascii_alphanumeric()
        && b.iter()
            .all(|c| c.is_ascii_alphanumeric() || b"._-".contains(c))
}
/// `validate_create`.
fn validate(raw: &Map<String, Json>) -> Result<(String, i64, String), Fail> {
    let allowed = ["action", "question", "timeout_secs", "request_id"];
    if raw.keys().any(|k| !allowed.contains(&k.as_str())) {
        return Err(Fail::error("unsupported question fields"));
    }
    let question = text(raw.get("question"), "question", QUESTION_LIMIT, true)?.unwrap();
    let timeout = match raw.get("timeout_secs") {
        None => Some(60),
        v => integer(v, true).filter(|t| (1..=120).contains(t)),
    };
    let Some(timeout) = timeout else {
        return Err(Fail::error("timeout_secs must be an integer from 1 to 120"));
    };
    let Some(id) = raw
        .get("request_id")
        .and_then(Json::as_str)
        .filter(|s| request_id(s))
    else {
        return Err(Fail::error(
            "request_id must be 1 to 64 ASCII identifier characters",
        ));
    };
    Ok((question, timeout, id.to_string()))
}
/// `_result`: ids as strings, plus `created`.
fn result(row: &[Value], created: bool) -> String {
    let names = COLUMNS.split(',');
    let mut fields: Vec<(&str, String)> = names
        .zip(row)
        .map(|(name, v)| {
            let as_text = matches!(
                name,
                "message_id"
                    | "answer_id"
                    | "late_answer_message_id"
                    | "operator_reply_after_desktop_message_id"
            );
            let value = match v {
                Value::Integer(i) if as_text => quote(&i.to_string()),
                // `str()` of any other non-null stored value.
                Value::Text(s) if as_text => quote(s),
                other => json_value(other),
            };
            (name, value)
        })
        .collect();
    fields.push(("created", created.to_string()));
    object(&fields)
}
fn fetch(op: &Op, question_id: &str) -> rusqlite::Result<Option<Vec<Value>>> {
    Ok(op
        .tx
        .query(
            &format!("SELECT {COLUMNS} FROM fleet_questions WHERE question_id=?"),
            &[t(question_id)],
        )?
        .into_iter()
        .next())
}
/// `fleet_hub_questions.handle`'s operation, inside the caller's write lock.
pub(super) fn handle(op: &mut Op) -> Result<Reply, Fail> {
    let w = op.w;
    let raw = &w.raw;
    let caller = w.caller.as_str();
    let generation = generation(op.tx, caller)?;
    if !bool::from(generation.as_bytes().ct_eq(w.key.as_bytes())) {
        return Err(Fail::auth("question credential generation changed"));
    }
    let now = iso_us(op.now);
    let action = raw.get("action").and_then(Json::as_str).unwrap_or_default();
    let create = action == "question_create";
    if create {
        validate(raw)?;
    } else {
        let allowed: &[&str] = if action == "question_desktop" {
            &["action", "question_id", "answer"]
        } else {
            &["action", "question_id"]
        };
        if raw.keys().any(|k| !allowed.contains(&k.as_str())) {
            return Err(Fail::error("unsupported question fields"));
        }
        let valid = raw
            .get("question_id")
            .and_then(Json::as_str)
            .is_some_and(|id| {
                id.len() == 32
                    && id
                        .bytes()
                        .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
            });
        if !valid {
            return Err(Fail::error("question_id is invalid"));
        }
        if action == "question_desktop" {
            text(raw.get("answer"), "answer", super::BODY_LIMIT, true)?;
        }
    }
    // `_expire`.
    op.tx.execute(
        "UPDATE fleet_questions SET status='timeout' WHERE status='pending' AND deadline<=?",
        &[t(&now)],
    )?;
    op.tx.execute(
        "DELETE FROM fleet_questions WHERE created_at<?",
        &[t(&iso_us(op.now - w.policy.retention_days * DAY_US))],
    )?;
    if create {
        return create_question(op, &generation, &now);
    }
    let question_id = raw["question_id"].as_str().unwrap();
    let owner = op.tx.query(
        "SELECT caller,generation FROM fleet_questions WHERE question_id=?",
        &[t(question_id)],
    )?;
    let owned = owner.first().is_some_and(|r| {
        text_of(&r[0]) == Some(caller) && text_of(&r[1]) == Some(generation.as_str())
    });
    if !owned {
        return Err(Fail::Forbidden {
            message: "question is not owned by this credential".into(),
            code: "question_not_owned",
        });
    }
    if action == "question_desktop" {
        op.tx.execute(
            "UPDATE fleet_questions SET status='answered',answer=?,source='caller_reported_desktop',answered_by=?,answered_at=? \
             WHERE question_id=? AND status='pending' AND deadline>?",
            &[
                raw["answer"].clone().as_str().map_or(Value::Null, t),
                t(caller),
                t(&now),
                t(question_id),
                t(&now),
            ],
        )?;
        op.notify.push(caller.to_string());
    }
    let row = fetch(op, question_id)?.ok_or(Fail::Python)?;
    Ok((200, result(&row, false)))
}
/// `_create`.
fn create_question(op: &mut Op, generation: &str, now: &str) -> Result<Reply, Fail> {
    let w = op.w;
    let caller = w.caller.as_str();
    let (question, timeout, request) = validate(&w.raw)?;
    let digest = sha256_hex(format!("[{}, {timeout}]", quote_utf8(&question)).as_bytes());
    let existing = op.tx.query(
        "SELECT question_id,payload_hash FROM fleet_questions WHERE caller=? AND generation=? AND request_id=?",
        &[t(caller), t(generation), t(&request)],
    )?;
    if let Some(row) = existing.first() {
        if text_of(&row[1]) != Some(digest.as_str()) {
            return Err(Fail::Conflict {
                message: "request_id was used for a different question".into(),
                code: Some("question_request_conflict"),
                id: None,
            });
        }
        let id = text_of(&row[0]).unwrap_or_default().to_string();
        let row = fetch(op, &id)?.ok_or(Fail::Python)?;
        return Ok((200, result(&row, false)));
    }
    if count(
        op.tx,
        "SELECT COUNT(*) FROM fleet_questions WHERE caller=? AND status='pending'",
        &[t(caller)],
    )? >= 2
    {
        return Err(Fail::quota(
            "question station has two pending questions",
            "question_station_busy",
            None,
        ));
    }
    let operator = w.operator.as_str();
    if !operator.starts_with("agent:")
        || operator == caller
        || !exists(
            op.tx,
            "SELECT 1 FROM fleet_message_principals WHERE principal_id=? AND revoked_at IS NULL",
            &[t(operator)],
        )?
    {
        return Err(Fail::Forbidden {
            message: "hub operator recipient must be a dedicated active agent: message principal different from the caller".into(),
            code: "question_operator_invalid",
        });
    }
    let [question_id, client_id] = w.seeds.question.clone();
    let deadline = iso_us(op.now + timeout * SECOND_US);
    let mut fields = Map::new();
    fields.insert("to".into(), Json::String(operator.into()));
    fields.insert("subject".into(), Json::String("Operator question".into()));
    fields.insert("body".into(), Json::String(question.clone()));
    fields.insert("client_id".into(), Json::String(client_id));
    let message = op.send(
        &fields,
        None,
        vec![("kind", "operator_question".to_string())],
    )?;
    let message_id = message
        .as_deref()
        .and_then(|m| serde_json::from_str::<Json>(m).ok())
        .and_then(|m| m["id"].as_i64())
        .ok_or(Fail::Python)?;
    op.tx.execute(
        "INSERT INTO fleet_questions(question_id,caller,generation,request_id,payload_hash,operator,question,message_id,created_at,deadline) VALUES(?,?,?,?,?,?,?,?,?,?)",
        &[
            t(&question_id),
            t(caller),
            t(generation),
            t(&request),
            t(&digest),
            t(operator),
            t(&question),
            Value::Integer(message_id),
            t(now),
            t(&deadline),
        ],
    )?;
    let row = fetch(op, &question_id)?.ok_or(Fail::Python)?;
    Ok((201, result(&row, true)))
}
