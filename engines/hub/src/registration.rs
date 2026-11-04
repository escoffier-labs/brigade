//! S5 registration seam. S9 can select this handler for its registration family.
use axum::{
    body::Body,
    http::{Response, request::Parts},
};
use brigade_hub::{
    read::ReaderPool,
    writer::{self, ReadTx, WriteTx},
};
use rusqlite::types::Value;
use serde_json::{Value as Json, json};
use sha2::{Digest, Sha256};
use std::{
    sync::Arc,
    time::{Duration, Instant},
};
use subtle::ConstantTimeEq;

#[path = "registration_principals.rs"]
mod principals;

pub fn digest(token: &str) -> String {
    let mut output = String::with_capacity(64);
    const HEX: &[u8] = b"0123456789abcdef";
    for b in Sha256::digest(token.as_bytes()) {
        output.push(HEX[(b >> 4) as usize] as char);
        output.push(HEX[(b & 15) as usize] as char);
    }
    output
}
pub fn quote(s: &str) -> String {
    let encoded = serde_json::to_string(s).unwrap();
    let mut out = String::new();
    for c in encoded.chars() {
        if c.is_ascii() {
            out.push(c);
        } else {
            let mut buf = [0; 2];
            for unit in c.encode_utf16(&mut buf) {
                out.push_str(&format!("\\u{unit:04x}"));
            }
        }
    }
    out
}
pub fn object(fields: &[(&str, String)]) -> String {
    format!(
        "{{{}}}",
        fields
            .iter()
            .map(|(k, v)| format!("{}: {v}", quote(k)))
            .collect::<Vec<_>>()
            .join(", ")
    )
}
pub fn array(items: impl IntoIterator<Item = String>) -> String {
    format!("[{}]", items.into_iter().collect::<Vec<_>>().join(", "))
}
pub fn value(v: &Value) -> String {
    match v {
        Value::Text(s) => quote(s),
        Value::Null => "null".into(),
        _ => unreachable!(),
    }
}
pub fn text(s: &str) -> Value {
    Value::Text(s.into())
}
pub fn string(v: &Value) -> &str {
    if let Value::Text(s) = v { s } else { "" }
}
#[derive(Clone)]
pub struct Reply {
    pub status: u16,
    pub body: String,
}
impl Reply {
    pub fn new(status: u16, fields: &[(&str, String)]) -> Self {
        Self {
            status,
            body: object(fields),
        }
    }
    pub fn error(status: u16, message: &str) -> Self {
        Self::new(status, &[("error", quote(message))])
    }
    pub fn response(self) -> Response<Body> {
        Response::builder()
            .status(self.status)
            .header("content-type", "application/json")
            .header("server", "brigade-fleet-hub/1")
            .header("cache-control", "no-store")
            .header("referrer-policy", "no-referrer")
            .header("content-length", self.body.len())
            .body(Body::from(self.body))
            .unwrap()
    }
    fn packed(self) -> Json {
        json!({"status":self.status,"body":self.body})
    }
}
// Errors are already public refusals. Storage errors never include SQL or credentials.
pub type Result<T> = std::result::Result<T, DomainError>;
pub enum DomainError {
    Refusal(Reply),
    Sql(rusqlite::Error),
}
impl From<rusqlite::Error> for DomainError {
    fn from(e: rusqlite::Error) -> Self {
        Self::Sql(e)
    }
}
pub fn refuse<T>(message: &str) -> Result<T> {
    Err(DomainError::Refusal(Reply::error(400, message)))
}
pub fn random(bytes: usize) -> Result<String> {
    use std::io::Read;
    let mut raw = vec![0u8; bytes];
    std::fs::File::open("/dev/urandom")
        .and_then(|mut f| f.read_exact(&mut raw))
        .map_err(|_| {
            DomainError::Refusal(Reply::error(503, "message authorization unavailable"))
        })?;
    let alphabet = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    let mut out = String::new();
    let mut acc = 0u32;
    let mut bits = 0;
    for b in raw {
        acc = (acc << 8) | u32::from(b);
        bits += 8;
        while bits >= 6 {
            bits -= 6;
            out.push(alphabet[((acc >> bits) & 63) as usize] as char);
        }
    }
    if bits > 0 {
        out.push(alphabet[((acc << (6 - bits)) & 63) as usize] as char);
    }
    Ok(out)
}
pub fn now(clock: Option<&str>) -> String {
    let d = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap();
    let epoch = clock
        .and_then(|p| std::fs::read_to_string(p).ok())
        .and_then(|s| s.trim().parse::<f64>().ok())
        .filter(|v| v.is_finite() && *v >= 0.0 && *v < 253402300799.0);
    let secs = epoch
        .map(|v| v.floor() as libc::time_t)
        .unwrap_or(d.as_secs() as libc::time_t);
    let micros = epoch
        .map(|v| ((v - v.floor()) * 1_000_000.0).round() as u32)
        .unwrap_or(d.subsec_micros());
    let mut tm = std::mem::MaybeUninit::<libc::tm>::uninit();
    // gmtime_r writes the supplied structure and keeps no shared mutable state.
    let tm = unsafe {
        libc::gmtime_r(&secs, tm.as_mut_ptr());
        tm.assume_init()
    };
    let fraction = if micros == 0 {
        String::new()
    } else {
        format!(".{micros:06}")
    };
    format!(
        "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}{fraction}+00:00",
        tm.tm_year + 1900,
        tm.tm_mon + 1,
        tm.tm_mday,
        tm.tm_hour,
        tm.tm_min,
        tm.tm_sec
    )
}
pub fn valid_node(s: &str) -> bool {
    s != "unknown"
        && !s.is_empty()
        && s.len() <= 128
        && s.as_bytes()[0].is_ascii_alphanumeric()
        && s.bytes()
            .all(|c| c.is_ascii_alphanumeric() || b"._-".contains(&c))
}
fn node(row: &[Value]) -> String {
    object(&[
        ("node_id", value(&row[0])),
        ("label", value(&row[1])),
        ("created_at", value(&row[2])),
        ("revoked_at", value(&row[3])),
    ])
}
fn nodes(tx: &dyn ReadTx) -> rusqlite::Result<Reply> {
    let rows = tx.query(
        "SELECT node_id,label,created_at,revoked_at FROM nodes ORDER BY node_id",
        &[],
    )?;
    Ok(Reply::new(
        200,
        &[("nodes", array(rows.iter().map(|r| node(r))))],
    ))
}
fn mutate_node(tx: &dyn WriteTx, raw: &Json, stamp: &str, token: &str) -> Result<Reply> {
    let Some(raw) = raw.as_object() else {
        return refuse("node request must be a JSON object");
    };
    let action = raw.get("action").and_then(Json::as_str).unwrap_or("");
    if !matches!(action, "add" | "revoke") {
        return refuse("node field 'action' must be one of: add, revoke");
    }
    let id = raw
        .get("node_id")
        .and_then(Json::as_str)
        .unwrap_or("")
        .trim();
    if !valid_node(id) {
        return refuse(
            "field 'node_id' must be a real identity ([A-Za-z0-9._-], max 128 chars, never the literal 'unknown')",
        );
    }
    if action == "add" {
        let label = raw.get("label").unwrap_or(&Json::Null);
        if !label.is_null() && label.as_str().is_none_or(|s| s.chars().count() > 128) {
            return refuse("field 'label' must be a string of at most 128 chars");
        }
        let label = label
            .as_str()
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .map(text)
            .unwrap_or(Value::Null);
        let changed=tx.execute("INSERT INTO nodes(node_id,token_sha256,label,created_at,revoked_at) VALUES(?1,?2,?3,?4,NULL) ON CONFLICT(node_id) DO UPDATE SET token_sha256=excluded.token_sha256,label=excluded.label,created_at=excluded.created_at,revoked_at=NULL WHERE nodes.revoked_at IS NOT NULL", &[text(id),text(&digest(token)),label,text(stamp)])?;
        if changed != 1 {
            return Ok(Reply::new(409,&[("added","false".into()),("error",quote(&format!("node {id:?} is already enrolled; revoke it first to issue a new token").replace('"',"'")))]));
        }
    } else {
        let changed = tx.execute(
            "UPDATE nodes SET revoked_at=?1 WHERE node_id=?2 AND revoked_at IS NULL",
            &[text(stamp), text(id)],
        )?;
        principals::revoke_keys(tx, id, stamp)?;
        tx.execute(
            "DELETE FROM fleet_message_send_grants WHERE principal_id=?1",
            &[text(id)],
        )?;
        if changed == 1 {
            principals::audit(tx, id, "member.remove", json!({"node_id":id}), stamp)?;
        }
    }
    let rows = tx.query(
        "SELECT node_id,label,created_at,revoked_at FROM nodes WHERE node_id=?1",
        &[text(id)],
    )?;
    if rows.is_empty() {
        return Ok(Reply::new(
            404,
            &[
                ("revoked", "false".into()),
                ("error", quote(&format!("node '{id}' is not enrolled"))),
            ],
        ));
    }
    Ok(if action == "add" {
        Reply::new(
            200,
            &[
                ("added", "true".into()),
                ("node", node(&rows[0])),
                ("token", quote(token)),
            ],
        )
    } else {
        Reply::new(200, &[("revoked", "true".into()), ("node", node(&rows[0]))])
    })
}
struct ConnectionRead<'a>(&'a rusqlite::Connection);
impl ReadTx for ConnectionRead<'_> {
    fn query(&self, sql: &str, params: &[Value]) -> rusqlite::Result<Vec<Vec<Value>>> {
        let mut statement = self.0.prepare_cached(sql)?;
        let n = statement.column_count();
        statement
            .query_map(rusqlite::params_from_iter(params), |r| {
                (0..n).map(|i| r.get(i)).collect()
            })?
            .collect()
    }
}
fn authorize<T: ReadTx + ?Sized>(
    tx: &T,
    key: &str,
    admin: &str,
    path: &str,
) -> rusqlite::Result<Option<Reply>> {
    if bool::from(key.as_bytes().ct_eq(admin.as_bytes())) {
        return Ok(None);
    }
    let rows = tx.query(
        "SELECT token_sha256,revoked_at FROM nodes WHERE token_sha256=?1",
        &[text(key)],
    )?;
    let error = if let Some(row) = rows
        .first()
        .filter(|r| bool::from(string(&r[0]).as_bytes().ct_eq(key.as_bytes())))
    {
        if row[1] != Value::Null {
            Reply::error(401, "unauthorized: node token revoked")
        } else {
            Reply::error(
                403,
                if path == "/nodes" {
                    "the admin token is required to manage nodes"
                } else {
                    "the admin token is required to enroll message principals"
                },
            )
        }
    } else {
        Reply::error(401, "unauthorized")
    };
    Ok(Some(error))
}
#[derive(Clone)]
pub struct Registration {
    pub readers: Arc<ReaderPool>,
    pub writer: writer::Writer,
    pub admin: String,
    pub clock: Option<String>,
}
struct Mutation {
    key: String,
    admin: String,
    path: String,
    raw: Json,
    stamp: String,
    token: String,
    seeds: [String; 3],
}
impl writer::Command for Mutation {
    fn apply(&self, tx: &dyn WriteTx) -> std::result::Result<Json, writer::ApplyError> {
        if let Some(reply) = authorize(tx, &self.key, &self.admin, &self.path)? {
            return Ok(reply.packed());
        }
        let result = if self.path == "/nodes" {
            mutate_node(tx, &self.raw, &self.stamp, &self.token)
        } else {
            principals::handle(tx, &self.raw, &self.stamp, &self.token, &self.seeds)
        };
        match result {
            Ok(reply) => Ok(reply.packed()),
            Err(DomainError::Refusal(reply)) => Ok(reply.packed()),
            Err(DomainError::Sql(e)) => Err(e.into()),
        }
    }
}
impl Registration {
    pub fn route(method: &str, path: &str) -> bool {
        (method == "GET" && path == "/nodes")
            || (method == "POST" && matches!(path, "/nodes" | "/message-principals"))
    }
    pub async fn auth(&self, parts: &Parts) -> Option<Response<Body>> {
        let key = parts
            .headers
            .get("authorization")
            .and_then(|v| v.to_str().ok())
            .and_then(|v| v.strip_prefix("Bearer "))
            .filter(|s| !s.is_empty())
            .map(digest);
        let Some(key) = key else {
            return Some(Reply::error(401, "unauthorized").response());
        };
        let admin = self.admin.clone();
        let path = parts.uri.path().to_string();
        match self
            .readers
            .submit(move |c| authorize(&ConnectionRead(c), &key, &admin, &path))
            .await
        {
            Ok(reply) => reply.map(Reply::response),
            Err(_) => Some(crate::busy("credential-lookup")),
        }
    }
    pub async fn handle(&self, parts: &Parts, body: &[u8]) -> Response<Body> {
        if parts.method == "GET" {
            return match self.readers.submit(|c| nodes(&ConnectionRead(c))).await {
                Ok(r) => r.response(),
                Err(_) => crate::busy("reader-timeout"),
            };
        }
        if !parts.headers.contains_key("content-length") {
            return Reply::error(400, "bad Content-Length or Transfer-Encoding").response();
        }
        let raw = match parse_body(body) {
            Ok(raw) => raw,
            Err(message) => return Reply::error(400, &message).response(),
        };
        let token = match random(32) {
            Ok(t) => t,
            Err(_) => return crate::busy("registration-unavailable"),
        };
        let command = Mutation {
            key: digest(
                parts.headers["authorization"]
                    .to_str()
                    .unwrap()
                    .strip_prefix("Bearer ")
                    .unwrap(),
            ),
            admin: self.admin.clone(),
            path: parts.uri.path().into(),
            raw,
            stamp: now(self.clock.as_deref()),
            seeds: match (random(24), random(24), random(24)) {
                (Ok(a), Ok(b), Ok(c)) => [a, b, c],
                _ => return crate::busy("registration-unavailable"),
            },
            token,
        };
        let budget = writer::Budget {
            events: 1,
            bytes: body.len() + 32 * 1024,
            durable: true,
            deadline: Instant::now() + Duration::from_secs(10),
        };
        let result = match self.writer.submit(command, budget).await {
            Ok(rx) => rx.await.unwrap_or(Err(writer::Failure {
                status: 503,
                reason: "writer-stopped",
            })),
            Err(e) => Err(e),
        };
        match result {
            Ok(v) => Reply {
                status: v["status"].as_u64().unwrap() as u16,
                body: v["body"].as_str().unwrap().into(),
            }
            .response(),
            Err(e) => Reply::new(
                e.status,
                &[("error", quote("hub busy")), ("reason", quote(e.reason))],
            )
            .response(),
        }
    }
}

fn parse_body(body: &[u8]) -> std::result::Result<Json, String> {
    if body.is_empty() {
        return Err("missing body".into());
    }
    // Count lexical containers and items before decoding, including duplicate keys.
    let mut quoted = false;
    let mut escape = false;
    let mut stack = Vec::new();
    let mut containers = 0;
    let mut items = 0;
    for &b in body {
        if quoted {
            if escape {
                escape = false;
            } else if b == b'\\' {
                escape = true;
            } else if b == b'"' {
                quoted = false;
            }
            continue;
        }
        if !b.is_ascii_whitespace() && !matches!(b, b',' | b']' | b'}') {
            if let Some(empty) = stack.last_mut() {
                if *empty {
                    items += 1;
                    *empty = false;
                }
            }
        }
        match b {
            b'"' => quoted = true,
            b'[' | b'{' => {
                containers += 1;
                stack.push(true);
            }
            b']' | b'}' => {
                stack.pop();
            }
            b',' => items += 1,
            _ => {}
        }
        if containers > 32768 {
            return Err("JSON containers exceed 32768".into());
        }
        if items > 131072 {
            return Err("JSON aggregate items exceed 131072".into());
        }
        if stack.len() > 32 {
            return Err("JSON nesting exceeds 32".into());
        }
    }
    let raw: Json =
        serde_json::from_slice(body).map_err(|_| "body is not valid JSON".to_string())?;
    if raw.is_null() {
        return Err("body must be a JSON object or array".into());
    }
    let mut pending = vec![&raw];
    while let Some(item) = pending.pop() {
        match item {
            Json::Array(a) => {
                if a.len() > 1000 {
                    return Err("JSON container exceeds 1000 items".into());
                }
                pending.extend(a);
            }
            Json::Object(o) => {
                if o.len() > 1000 {
                    return Err("JSON container exceeds 1000 items".into());
                }
                pending.extend(o.values());
            }
            _ => {}
        }
    }
    Ok(raw)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn python_json_spacing_unicode_and_bounds() {
        assert_eq!(
            object(&[("label", quote("café 🎉"))]),
            r#"{"label": "caf\u00e9 \ud83c\udf89"}"#
        );
        assert_eq!(
            parse_body(b"null").unwrap_err(),
            "body must be a JSON object or array"
        );
        assert_eq!(parse_body(b"{").unwrap_err(), "body is not valid JSON");
        assert_eq!(
            parse_body("[".repeat(33).as_bytes()).unwrap_err(),
            "JSON nesting exceeds 32"
        );
        assert_eq!(
            parse_body(format!("[{}]", vec!["0"; 1001].join(",")).as_bytes()).unwrap_err(),
            "JSON container exceeds 1000 items"
        );
        assert_eq!(digest("fake-token").len(), 64);
    }
}
