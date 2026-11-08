//! S6 heartbeat routes. All mutations use the hub's single FULL writer.
mod events;
mod sessions;
mod status;
use crate::registration::{Reply, digest, parse_body};
use axum::{
    body::Body,
    http::{Response, request::Parts},
};
use brigade_hub::{
    claims,
    read::ReaderPool,
    wire::*,
    writer::{self, ReadTx, WriteTx},
};
use rusqlite::types::Value;
use serde_json::{Value as Json, json};
use std::{
    sync::Arc,
    time::{Duration, Instant},
};

type Result<T> = std::result::Result<T, Error>;
enum Error {
    Refusal(Reply),
    Sql(rusqlite::Error),
    Python,
}
impl From<rusqlite::Error> for Error {
    fn from(e: rusqlite::Error) -> Self {
        Self::Sql(e)
    }
}
fn error(status: u16, message: &str) -> Error {
    Error::Refusal(Reply::error(status, message))
}
fn invalid<T>(message: &str) -> Result<T> {
    Err(error(400, message))
}
fn packed(reply: Reply) -> Json {
    json!({"status": reply.status, "body": reply.body})
}
fn sql_json(v: &Value) -> String {
    match v {
        Value::Null | Value::Blob(_) => "null".into(),
        Value::Integer(i) => i.to_string(),
        Value::Real(f) => py_float(*f),
        Value::Text(s) => quote(s),
    }
}
fn json_wire(v: &Json) -> String {
    match v {
        Json::Null => "null".into(),
        Json::Bool(b) => b.to_string(),
        Json::Number(n) => n
            .as_i64()
            .map(|i| i.to_string())
            .or_else(|| n.as_u64().map(|i| i.to_string()))
            .unwrap_or_else(|| py_float(n.as_f64().unwrap())),
        Json::String(s) => quote(s),
        Json::Array(a) => array(a.iter().map(json_wire)),
        Json::Object(o) => object(
            &o.iter()
                .map(|(k, v)| (k.as_str(), json_wire(v)))
                .collect::<Vec<_>>(),
        ),
    }
}
// Python can preserve lone surrogates and arbitrary-size integers in stored
// JSON. Serde either refuses them or rounds them into floats. Delegate both.
fn stored_json(s: &str) -> Option<Json> {
    fn exact(v: &Json) -> bool {
        match v {
            Json::Array(a) => a.iter().all(exact),
            Json::Object(o) => o.values().all(exact),
            Json::Number(n) => {
                !n.is_f64() || n.as_f64().is_some_and(|f| f.abs() < 9223372036854775808.0)
            }
            _ => true,
        }
    }
    serde_json::from_str(s).ok().filter(exact)
}
fn repo_identity(s: &str) -> bool {
    let lower = s.to_lowercase();
    !s.is_empty()
        && s.chars().count() <= 512
        && !controls(s)
        && !lower.contains("://")
        && !lower.contains("token=")
        && !lower.contains("password=")
        && !s.split('/').any(|part| {
            part.split_once(':').is_some_and(|(a, b)| {
                !a.is_empty() && b.split_once('@').is_some_and(|(c, _)| !c.is_empty())
            })
        })
}
fn bearer(parts: &Parts) -> Option<String> {
    parts
        .headers
        .get("authorization")?
        .to_str()
        .ok()?
        .strip_prefix("Bearer ")
        .filter(|s| !s.is_empty())
        .map(digest)
}
#[derive(Clone)]
pub struct Heartbeat {
    pub readers: Arc<ReaderPool>,
    pub writer: writer::Writer,
    pub admin: String,
    pub clock: Option<String>,
    pub allow_admin_writes: bool,
    pub events: bool,
    pub stale_after: i64,
}
struct Mutation {
    raw: Json,
    path: String,
    key: String,
    route: Heartbeat,
    seed: String,
}
impl writer::Command for Mutation {
    fn apply(&self, tx: &dyn WriteTx) -> std::result::Result<Json, writer::ApplyError> {
        if brigade_hub::schema::newer_than_supported(tx)? {
            return Err(writer::ApplyError::Refused(packed(Reply::error(
                503,
                brigade_hub::schema::NEWER_SCHEMA,
            ))));
        }
        let node = match claims::caller(
            tx,
            &self.key,
            &self.route.admin,
            true,
            self.route.allow_admin_writes,
        )? {
            Ok(node) => node,
            Err((status, body)) => {
                return Err(writer::ApplyError::Refused(packed(Reply { status, body })));
            }
        };
        let now = epoch(self.route.clock.as_deref());
        let result = if self.path == "/events" {
            events::store(tx, &self.raw, node.as_deref(), now, &self.seed, &self.route)
        } else {
            sessions::store(tx, &self.raw, node.as_deref(), now)
        };
        match result {
            Ok(reply) => Ok(packed(reply)),
            Err(Error::Refusal(reply)) => Err(writer::ApplyError::Refused(packed(reply))),
            Err(Error::Sql(e)) => Err(e.into()),
            Err(Error::Python) => Err(writer::ApplyError::Refused(json!({"python": true}))),
        }
    }
}
impl Heartbeat {
    pub fn route(method: &str, path: &str) -> bool {
        matches!(
            (method, path),
            ("POST", "/events" | "/sessions") | ("GET", "/status" | "/sessions")
        )
    }
    pub async fn auth(&self, parts: &Parts) -> Option<Response<Body>> {
        let Some(key) = bearer(parts) else {
            return Some(Reply::error(401, "unauthorized").response());
        };
        let (admin, allow, write) = (
            self.admin.clone(),
            self.allow_admin_writes,
            parts.method == "POST",
        );
        match self
            .readers
            .submit(move |c| claims::caller(&claims::ReadConnection(c), &key, &admin, write, allow))
            .await
        {
            Ok(Ok(_)) => None,
            Ok(Err((status, body))) => Some(Reply { status, body }.response()),
            Err(_) if !write => None,
            Err(_) => Some(crate::busy("credential-lookup")),
        }
    }
    pub async fn handle(&self, parts: &Parts, body: &[u8]) -> Option<Response<Body>> {
        let key = bearer(parts).unwrap();
        if parts.method == "GET" {
            let route = self.clone();
            let all = parts
                .uri
                .query()
                .and_then(|q| {
                    url::form_urlencoded::parse(q.as_bytes())
                        .find(|(k, v)| k == "all" && !v.is_empty())
                        .map(|(_, v)| v.to_lowercase())
                })
                .is_some_and(|v| matches!(v.as_str(), "1" | "true" | "yes"));
            let path = parts.uri.path().to_string();
            return match self
                .readers
                .submit(move |c| {
                    let tx = claims::ReadConnection(c);
                    if let Err((status, body)) =
                        claims::caller(&tx, &key, &route.admin, false, route.allow_admin_writes)?
                    {
                        return Ok(Some(Reply { status, body }));
                    }
                    let now = epoch(route.clock.as_deref());
                    let result = if path == "/status" {
                        status::list(&tx, all, now, route.stale_after)
                    } else {
                        sessions::list(&tx, all, now)
                    };
                    match result {
                        Ok(r) => Ok(r),
                        Err(Error::Refusal(r)) => Ok(Some(r)),
                        Err(Error::Sql(_) | Error::Python) => Ok(None),
                    }
                })
                .await
            {
                Ok(r) => r.map(Reply::response),
                Err(_) => None,
            };
        }
        if !parts.headers.contains_key("content-length") {
            return Some(Reply::error(400, "bad Content-Length or Transfer-Encoding").response());
        }
        let raw = match parse_body(body) {
            Ok(raw) => raw,
            Err(e) => return Some(Reply::error(400, &e).response()),
        };
        // Python owns the extended cloud/context validators and exotic JSON
        // spellings. Keep those on the upstream, as S5/S7/S8 do.
        if if parts.uri.path() == "/events" {
            events::python_only(&raw)
        } else {
            sessions::python_only(&raw)
        } {
            return None;
        }
        let events = if parts.uri.path() == "/events" {
            raw.as_array().map_or(1, Vec::len).min(1000)
        } else {
            1
        };
        let seed = match crate::registration::random(16) {
            Ok(s) => digest(&s),
            Err(_) => return Some(crate::busy("heartbeat-unavailable")),
        };
        let command = Mutation {
            raw,
            path: parts.uri.path().into(),
            key,
            route: self.clone(),
            seed,
        };
        let budget = writer::Budget {
            events,
            // The inclusive wire payload cap must remain admissible. Reserve
            // reply headroom where it fits within the writer payload ceiling.
            bytes: (body.len() + 32 * 1024).min(8 * 1024 * 1024),
            // Admission class only. Every accepted command still commits at FULL.
            durable: false,
            deadline: Instant::now() + Duration::from_secs(10),
        };
        let result = match self.writer.submit(command, budget).await {
            Ok(rx) => rx.await.unwrap_or(Err(writer::Failure {
                status: 503,
                reason: "writer-stopped",
            })),
            Err(e) => Err(e),
        };
        if result.as_ref().is_ok_and(|v| v["python"] == true) {
            return None;
        }
        Some(match result {
            Ok(v) => Reply {
                status: v["status"].as_u64().unwrap() as u16,
                body: v["body"].as_str().unwrap().into(),
            }
            .response(),
            Err(e) => crate::busy(e.reason),
        })
    }
}

/// Resolve the same startup-frozen deck setting as the Python schema owner.
pub async fn stale_after(flag: Option<&str>) -> std::result::Result<i64, String> {
    let script = "import os,sys; from brigade import fleet_command_deck as d; p=d.resolve_config_path(sys.argv[1] or None,os.environ); print((d.load_config(p) if p else d.DeckConfig()).stale_history_after_seconds)";
    let interpreter = std::env::var("BRIGADE_HUB_PYTHON").unwrap_or_else(|_| "python3".into());
    let output = tokio::process::Command::new(&interpreter)
        .args(["-c", script, flag.unwrap_or("")])
        .output()
        .await
        .map_err(|_| "heartbeat configuration unavailable")?;
    if !output.status.success() {
        return Err("heartbeat configuration unavailable".into());
    }
    let value = String::from_utf8_lossy(&output.stdout)
        .trim()
        .parse()
        .map_err(|_| "heartbeat configuration unavailable".to_string())?;
    eprintln!("heartbeat stale_after={value} interpreter={interpreter:?}");
    Ok(value)
}

#[cfg(test)]
mod review_tests {
    use super::*;
    use writer::{Arbiter, Budget, Config, Lane, Writer};

    #[tokio::test]
    async fn refused_heartbeat_and_claim_share_one_full_batch() {
        let dir = std::env::temp_dir().join(format!("heartbeat-batch-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("hub.db");
        let python = std::env::var("BRIGADE_HUB_PYTHON").unwrap_or_else(|_| "python3".into());
        assert!(std::process::Command::new(python).args(["-c", "import sys; from pathlib import Path; from brigade import fleet_hub as h; c=h.init_db(Path(sys.argv[1])); h.add_node(c,'node-a'); c.close()"])
            .arg(&path).status().unwrap().success());
        let arbiter = Arc::new(Arbiter::default());
        let turn = arbiter
            .acquire(Lane::Memory, Instant::now() + Duration::from_secs(1))
            .unwrap();
        let writer = Writer::start(Config::new(path.clone()), arbiter).unwrap();
        let route = Heartbeat {
            readers: Arc::new(ReaderPool::open(&path, 1, 8).unwrap()),
            writer: writer.clone(),
            admin: digest("fake-admin"),
            clock: None,
            allow_admin_writes: true,
            events: false,
            stale_after: 1800,
        };
        let budget = Budget {
            events: 1,
            bytes: 1024,
            durable: false,
            deadline: Instant::now() + Duration::from_secs(10),
        };
        // A node mismatch refuses the heartbeat while the neighboring claim
        // commits in the same FULL batch.
        let event = json!({"node_id":"node-a","run_id":"refused","sequence":1,"digest":"one","state":"run.created","ts":"2026-01-01T00:00:00Z"});
        let mut wrong = event.clone();
        wrong["node_id"] = json!("node-b");
        // Use the node credential for the node-mismatch refusal.
        let db = rusqlite::Connection::open(&path).unwrap();
        let key: String = db
            .query_row(
                "SELECT token_sha256 FROM nodes WHERE node_id='node-a'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        let heartbeat = writer
            .submit(
                Mutation {
                    raw: json!([event, wrong]),
                    path: "/events".into(),
                    key,
                    route,
                    seed: "test-seed".into(),
                },
                budget,
            )
            .await
            .unwrap();
        let request = claims::validate(&json!({"action":"acquire","target":"same-batch","node_id":"node-a","holder":"test-holder"})).unwrap();
        let claim = writer
            .submit(
                claims::Claim {
                    request,
                    key: digest("fake-admin"),
                    admin: digest("fake-admin"),
                    allow_admin_writes: true,
                    clock: None,
                    events: false,
                },
                Budget {
                    durable: true,
                    ..budget
                },
            )
            .await
            .unwrap();
        drop(turn);
        assert_eq!(heartbeat.await.unwrap().unwrap()["status"], 403);
        assert_eq!(claim.await.unwrap().unwrap()["status"], 200);
        assert_eq!(writer.stats().snapshot().batch_size, 2);
        assert_eq!(
            db.query_row("SELECT count(*) FROM events", [], |r| r.get::<_, i64>(0))
                .unwrap(),
            0
        );
        assert_eq!(
            db.query_row("SELECT count(*) FROM claims", [], |r| r.get::<_, i64>(0))
                .unwrap(),
            1
        );
    }
}
