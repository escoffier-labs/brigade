//! S8 messages seam. S9 selects it for the messages family. `who`, `claims`
//! and `keys_publish` stay on the Python upstream: the station reads build
//! paging snapshots over sessions and claims, and a key record is stored in
//! the client's key order, which serde_json does not keep.
use crate::registration::{Reply, digest, parse_body, quote};
use axum::{
    body::{Body, Bytes},
    http::{Response, request::Parts},
};
use brigade_hub::{
    claims::ReadConnection,
    messages::{self, Fail, Listed, Policy, Request, wake::Wake},
    read::ReaderPool,
    writer,
};
use serde_json::{Map, Value as Json};
use std::{
    sync::Arc,
    time::{Duration, Instant},
};
use tokio::sync::{OwnedSemaphorePermit, mpsc, oneshot};

/// A re-read's mark and its page (with whether it had rows), or a refusal.
type RereadAnswer = (u64, Result<(bool, messages::Reply), messages::Reply>);
/// One woken waiter's re-read, answered with the mark taken before the query.
struct Reread {
    recipient: String,
    /// The poll's bearer digest, re-checked against the principal's credential.
    key: String,
    after: i64,
    unacked: bool,
    answer: oneshot::Sender<Option<RereadAnswer>>,
}
/// Woken waiters re-read in groups of up to 256 per reader job.
const GROUP: usize = 256;

#[derive(Clone)]
pub struct Messages {
    pub readers: Arc<ReaderPool>,
    pub writer: writer::Writer,
    pub admin: String,
    pub clock: Option<String>,
    pub allow_admin_writes: bool,
    pub events: bool,
    pub policy: Policy,
    pub wake: Arc<Wake>,
    rereads: mpsc::Sender<Reread>,
}
fn reply((status, body): messages::Reply) -> Response<Body> {
    Reply { status, body }.response()
}
fn key(parts: &Parts) -> Option<String> {
    parts
        .headers
        .get("authorization")
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.strip_prefix("Bearer "))
        .filter(|s| !s.is_empty())
        .map(digest)
}
fn random_hex() -> Option<String> {
    use std::io::Read;
    let mut raw = [0u8; 16];
    std::fs::File::open("/dev/urandom")
        .and_then(|mut f| f.read_exact(&mut raw))
        .ok()?;
    Some(raw.iter().fold(String::with_capacity(32), |mut out, b| {
        use std::fmt::Write;
        let _ = write!(out, "{b:02x}");
        out
    }))
}
/// Actions the Python upstream keeps answering in native mode.
fn proxied(action: &str) -> bool {
    matches!(action, "who" | "claims" | "keys_publish") || action.starts_with("events_")
}
impl Messages {
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        readers: Arc<ReaderPool>,
        writer: writer::Writer,
        admin: String,
        clock: Option<String>,
        allow_admin_writes: bool,
        policy: Policy,
        wake: Arc<Wake>,
    ) -> Self {
        let (rereads, receiver) = mpsc::channel(16 * 1024);
        let route = Self {
            readers,
            writer,
            admin,
            events: brigade_hub::claims::events_enabled(),
            clock,
            allow_admin_writes,
            policy,
            wake,
            rereads,
        };
        tokio::spawn(route.clone().regroup(receiver));
        route
    }
    pub fn route(method: &str, path: &str) -> bool {
        method == "POST" && path == "/messages"
    }
    /// `_caller` and `admit_post`, before the body is read. `Ok(None)` is the
    /// admin token under `--allow-admin-writes`.
    pub async fn auth(&self, parts: &Parts) -> Result<Option<String>, Box<Response<Body>>> {
        let Some(key) = key(parts) else {
            return Err(Box::new(reply(messages::error_reply(401, "unauthorized"))));
        };
        let (admin, allow) = (self.admin.clone(), self.allow_admin_writes);
        match self
            .readers
            .submit(move |c| messages::authenticate(&ReadConnection(c), &key, &admin, allow))
            .await
        {
            Ok(Ok(caller)) => Ok(caller),
            Ok(Err(refusal)) => Err(Box::new(reply(refusal))),
            Err(_) => Err(Box::new(crate::busy("credential-lookup"))),
        }
    }
    /// `Err` hands the body back to leave the request to the Python upstream.
    /// `source` is `None` for the front, which per-source waiter caps exempt.
    /// `budget` holds the body's byte reservation: a `list` releases it and
    /// the body once parsed, so an idle waiter costs only its future and its
    /// `Waiter`.
    pub async fn handle(
        &self,
        parts: &Parts,
        caller: Option<String>,
        body: Bytes,
        budget: &mut Vec<OwnedSemaphorePermit>,
        source: Option<String>,
    ) -> Result<Response<Body>, Bytes> {
        if !parts.headers.contains_key("content-length") {
            return Ok(Reply::error(400, "bad Content-Length or Transfer-Encoding").response());
        }
        let raw = match parse_body(&body) {
            Ok(raw) => raw,
            Err(message) => return Ok(Reply::error(400, &message).response()),
        };
        if messages::python_only(&raw) {
            return Err(body);
        }
        let action = raw
            .get("action")
            .and_then(Json::as_str)
            .unwrap_or_default()
            .to_string();
        if proxied(&action) {
            return Err(body);
        }
        let Some(caller) = caller else {
            return Ok(reply(messages::error_reply(
                403,
                "a node principal is required",
            )));
        };
        let Json::Object(raw) = raw else {
            return Ok(reply(
                Fail::error("message request must be an object")
                    .reply()
                    .unwrap(),
            ));
        };
        let plaintext = !raw.contains_key("envelope");
        if self.policy.require_sealed
            && (matches!(action.as_str(), "question_create" | "question_desktop")
                || (matches!(action.as_str(), "send" | "reply") && plaintext))
        {
            return Ok(reply(
                Fail::code("hub requires sealed messages", "message_sealing_required")
                    .reply()
                    .unwrap(),
            ));
        }
        let key = key(parts).expect("authorized before the body was read");
        let left = |answer: Option<Response<Body>>, body: Bytes| answer.ok_or(body);
        match action.as_str() {
            "list" => {
                // `list` never leaves the request to Python after this point.
                // Only the fields it reads outlive the reservation.
                let fields = messages::ListFields::new(raw);
                drop(body);
                budget.clear();
                Ok(self
                    .list(caller, key, fields, source)
                    .await
                    .unwrap_or_else(|| crate::busy("messages-unavailable")))
            }
            "count" | "sent" | "sent_lookup" | "identity" | "keys_get" | "manifests_get" => {
                left(self.read(caller, key, raw).await, body)
            }
            "send" | "reply" | "ack" | "question_create" | "question_status"
            | "question_desktop" => {
                let size = body.len();
                left(self.write(caller, key, raw, size).await, body)
            }
            _ => Ok(reply(
                Fail::error(
                    "action must be send, list, sent, count, ack, reply, who, claims, or identity",
                )
                .reply()
                .unwrap(),
            )),
        }
    }
    fn now(&self) -> i64 {
        brigade_hub::wire::epoch_us(self.clock.as_deref())
    }
    async fn read(
        &self,
        caller: String,
        key: String,
        raw: Map<String, Json>,
    ) -> Option<Response<Body>> {
        let (policy, now) = (self.policy.clone(), self.now());
        let answer = self
            .readers
            .submit(move |c| {
                let request = Request {
                    caller: &caller,
                    key: &key,
                    raw: &raw,
                    now,
                    policy: &policy,
                };
                match messages::read(&ReadConnection(c), &request) {
                    Ok(r) => Ok(Some(r)),
                    Err(Fail::Sql(e)) => Err(e),
                    Err(fail) => Ok(fail.reply()),
                }
            })
            .await;
        match answer {
            Ok(Some(r)) => Some(reply(r)),
            Ok(None) => None,
            Err(_) => Some(crate::busy("reader-timeout")),
        }
    }
    async fn write(
        &self,
        caller: String,
        key: String,
        raw: Map<String, Json>,
        size: usize,
    ) -> Option<Response<Body>> {
        let seeds = match (random_hex(), random_hex(), random_hex(), random_hex()) {
            (Some(a), Some(b), Some(c), Some(d)) => messages::Seeds {
                client_id: a,
                event: b,
                question: [c, d],
            },
            _ => return Some(crate::busy("messages-unavailable")),
        };
        let command = messages::Write {
            caller,
            key,
            raw,
            policy: self.policy.clone(),
            clock: self.clock.clone(),
            seeds,
            events: self.events,
            operator: std::env::var("BRIGADE_FLEET_OPERATOR_RECIPIENT").unwrap_or_default(),
            wake: self.wake.clone(),
            notified: Default::default(),
        };
        let budget = writer::Budget {
            events: 1,
            // A valid write carries at most a 96 KiB envelope or a 64 KiB body; larger
            // bodies are refused by validation, so the reservation stays under the cap.
            bytes: (size + 256 * 1024).min(8 * 1024 * 1024),
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
        Some(match result {
            Ok(v) if v.get("python") == Some(&Json::Bool(true)) => return None,
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
        })
    }
    /// `list_messages`, with the long-poll protocol for `wait_ms` above 0.
    async fn list(
        &self,
        caller: String,
        key: String,
        fields: messages::ListFields,
        source: Option<String>,
    ) -> Option<Response<Body>> {
        let (policy, now) = (self.policy.clone(), self.now());
        let recipient = caller.clone();
        let digest = key.clone();
        // Plan step 1: a poll that may wait is watched before its first mark,
        // so the external-commit watcher cannot absorb a row that another
        // process commits between this snapshot and the registration below.
        let first_read = fields
            .raw()
            .get("wait_ms")
            .and_then(Json::as_u64)
            .is_some_and(|w| w > 0)
            .then(|| self.wake.first_read());
        // The reader opens its snapshot before the closure runs, so the mark
        // comes first: a send committed after it either is visible or moves
        // the epoch past it.
        let mut mark = self.wake.epoch(&recipient);
        let first = self
            .readers
            .submit(move |c| {
                let request = Request {
                    caller: &caller,
                    key: &key,
                    raw: fields.raw(),
                    now,
                    policy: &policy,
                };
                match messages::list(&ReadConnection(c), &request) {
                    Ok(listed) => Ok(Ok(listed)),
                    Err(Fail::Sql(e)) => Err(e),
                    Err(fail) => Ok(Err(fail.reply())),
                }
            })
            .await;
        let (after, unacked, wait_ms) = match first {
            Ok(Ok(Listed::Page(r))) => return Some(reply(r)),
            Ok(Ok(Listed::Wait {
                after,
                unacked,
                wait_ms,
            })) => (after, unacked, wait_ms),
            Ok(Err(r)) => return r.map(reply),
            Err(_) => return Some(crate::busy("reader-timeout")),
        };
        // Only waiters count against the waiter caps; `Limits::total` is the
        // ceiling. Open connections that do not wait never refuse a wait.
        let Some(_waiter) = self.wake.register(&recipient, source.as_deref()) else {
            return Some(reply(
                Fail::quota("message wait capacity reached", "message_wait_capped", None)
                    .reply()
                    .unwrap(),
            ));
        };
        drop(first_read);
        let deadline = tokio::time::Instant::now() + Duration::from_millis(wait_ms);
        loop {
            self.wake.wait(&recipient, mark, deadline).await;
            let (answer, receive) = oneshot::channel();
            let request = Reread {
                recipient: recipient.clone(),
                key: digest.clone(),
                after,
                unacked,
                answer,
            };
            if self.rereads.send(request).await.is_err() {
                return Some(crate::busy("reader-timeout"));
            }
            let (next, result) = match receive.await {
                Ok(Some(answer)) => answer,
                _ => return Some(crate::busy("reader-timeout")),
            };
            mark = next;
            match result {
                Err(refusal) => return Some(reply(refusal)),
                Ok((rows, page))
                    if rows || tokio::time::Instant::now() >= deadline || self.wake.closing() =>
                {
                    return Some(reply(page));
                }
                Ok(_) => {}
            }
        }
    }
    /// Run woken waiters' re-reads in groups. Every recipient's epoch is
    /// marked when its group is submitted, before the reader's snapshot.
    async fn regroup(self, mut receiver: mpsc::Receiver<Reread>) {
        while let Some(first) = receiver.recv().await {
            let mut group = vec![first];
            while group.len() < GROUP {
                match receiver.try_recv() {
                    Ok(next) => group.push(next),
                    Err(_) => break,
                }
            }
            let (policy, now) = (self.policy.clone(), self.now());
            let marks: Vec<u64> = group
                .iter()
                .map(|r| self.wake.epoch(&r.recipient))
                .collect();
            let jobs: Vec<(String, String, i64, bool)> = group
                .iter()
                .map(|r| (r.recipient.clone(), r.key.clone(), r.after, r.unacked))
                .collect();
            let answers = self
                .readers
                .submit(move |c| {
                    let tx = ReadConnection(c);
                    let mut answers = Vec::with_capacity(jobs.len());
                    for ((recipient, key, after, unacked), mark) in jobs.iter().zip(marks) {
                        let result = match messages::reread(
                            &tx, recipient, key, *after, *unacked, now, &policy,
                        ) {
                            Ok(read) => Ok(read),
                            Err(Fail::Sql(e)) => return Err(e),
                            Err(fail) => Err(fail
                                .reply()
                                .unwrap_or((503, messages::error_reply(503, "hub busy").1))),
                        };
                        answers.push((mark, result));
                    }
                    Ok(answers)
                })
                .await;
            match answers {
                Ok(answers) => {
                    for (request, answer) in group.into_iter().zip(answers) {
                        let _ = request.answer.send(Some(answer));
                    }
                }
                Err(_) => {
                    for request in group {
                        let _ = request.answer.send(None);
                    }
                }
            }
        }
    }
}
