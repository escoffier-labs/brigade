//! S7 claims seam. S9 selects it for the claims family. `GET /owners` joins
//! policy, sessions and Grok Bot state, so it stays on the Python upstream.
use crate::registration::{Reply, digest, parse_body, quote};
use axum::{
    body::Body,
    http::{Response, request::Parts},
};
use brigade_hub::{claims, read::ReaderPool, writer};
use std::{
    sync::Arc,
    time::{Duration, Instant},
};

#[derive(Clone)]
pub struct Claims {
    pub readers: Arc<ReaderPool>,
    pub writer: writer::Writer,
    pub admin: String,
    pub clock: Option<String>,
    pub allow_admin_writes: bool,
    pub events: bool,
}
fn reply((status, body): claims::Reply) -> Response<Body> {
    Reply { status, body }.response()
}
/// The bearer digest. `_bearer()` partitions on the first space.
fn key(parts: &Parts) -> Option<String> {
    parts
        .headers
        .get("authorization")
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.strip_prefix("Bearer "))
        .filter(|s| !s.is_empty())
        .map(digest)
}
/// `parse_qs(query).get("all", [""])[0].lower() in ("1", "true", "yes")`.
fn include_all(query: Option<&str>) -> bool {
    query
        .and_then(|q| {
            url::form_urlencoded::parse(q.as_bytes())
                .find(|(name, value)| name == "all" && !value.is_empty())
                .map(|(_, value)| value.to_lowercase())
        })
        .is_some_and(|v| matches!(v.as_str(), "1" | "true" | "yes"))
}
impl Claims {
    pub fn route(method: &str, path: &str) -> bool {
        path == "/claims" && matches!(method, "GET" | "POST")
    }
    /// `_caller` and `admit_post`, before the body is read. `Ok` carries the
    /// caller's node, or `None` for the admin token.
    pub async fn auth(&self, parts: &Parts) -> Result<Option<String>, Box<Response<Body>>> {
        let Some(key) = key(parts) else {
            return Err(Box::new(reply(claims::error(401, "unauthorized"))));
        };
        let admin = self.admin.clone();
        let write = parts.method == "POST";
        let allow = self.allow_admin_writes;
        match self
            .readers
            .submit(move |c| claims::caller(&claims::ReadConnection(c), &key, &admin, write, allow))
            .await
        {
            Ok(Ok(node)) => Ok(node),
            Ok(Err(refusal)) => Err(Box::new(reply(refusal))),
            Err(_) => Err(Box::new(crate::busy("credential-lookup"))),
        }
    }
    /// `None` leaves the request to the Python upstream.
    pub async fn handle(
        &self,
        parts: &Parts,
        node: Option<String>,
        body: &[u8],
    ) -> Option<Response<Body>> {
        let key = key(parts).expect("authorized before the body was read");
        let clock = self.clock.clone();
        let admin = self.admin.clone();
        if parts.method == "GET" {
            let all = include_all(parts.uri.query());
            return match self
                .readers
                .submit(move |c| claims::list(c, &key, &admin, all, clock.as_deref()))
                .await
            {
                Ok(Some(r)) => Some(reply(r)),
                Ok(None) => None,
                Err(_) => Some(crate::busy("reader-timeout")),
            };
        }
        if !parts.headers.contains_key("content-length") {
            return Some(Reply::error(400, "bad Content-Length or Transfer-Encoding").response());
        }
        let raw = match parse_body(body) {
            Ok(raw) => raw,
            Err(message) => return Some(Reply::error(400, &message).response()),
        };
        if claims::python_only(&raw) {
            return None;
        }
        let mut request = match claims::validate(&raw) {
            Ok(request) => request,
            Err(refusal) => return Some(reply(refusal)),
        };
        // The caller's node binds the body before any storage decision.
        if let Some(refusal) = claims::forbidden(&request, node.as_deref()) {
            return Some(reply(refusal));
        }
        let allow = self.allow_admin_writes;
        if request.action == "inspect" {
            let (key, admin) = (key.clone(), admin.clone());
            return Some(
                match self
                    .readers
                    .submit(move |c| {
                        claims::inspect(c, &key, &admin, allow, &request, clock.as_deref())
                    })
                    .await
                {
                    Ok(r) => reply(r),
                    Err(_) => crate::busy("reader-timeout"),
                },
            );
        }
        if request.needs_snapshot() {
            let observed = self
                .readers
                .submit(move |c| claims::observe(c, &mut request).map(|r| (r, request)))
                .await;
            request = match observed {
                Ok((Some(refusal), _)) => return Some(reply(refusal)),
                Ok((None, request)) => request,
                Err(_) => return Some(crate::busy("reader-timeout")),
            };
        }
        let command = claims::Claim {
            request,
            key,
            admin,
            allow_admin_writes: allow,
            clock,
            events: self.events,
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
        Some(match result {
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
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn include_all_matches_python_parse_qs() {
        assert!(include_all(Some("all=1")));
        assert!(include_all(Some("all=TRUE&x=1")));
        assert!(include_all(Some("all=&all=yes")));
        assert!(!include_all(Some("all")));
        assert!(!include_all(Some("all=0&all=1")));
        assert!(!include_all(None));
    }
}
