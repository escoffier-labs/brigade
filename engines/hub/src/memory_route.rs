//! `POST /memory` (S13a, D21): additive under contract v1, native only.
//! Not configured: `/memory` stays an unknown path and the Python upstream
//! answers `404 {"error": "not found"}`. Configured but unable to serve (the
//! store failed to open or migrate, or the family is forced to `proxy`):
//! `503 {"error": "hub busy", "reason": "memory-unavailable"}`.
use axum::{
    body::Body,
    http::{Response, request::Parts},
};
use brigade_hub::memory::{Credential, Reply, Store};
use std::sync::Arc;

/// Memory bodies are small JSON requests. Card text is capped at 512 KiB.
pub const BODY_LIMIT: u64 = 2 * 1024 * 1024;

#[derive(Clone)]
pub enum MemoryRoute {
    Store(Arc<Store>),
    Unavailable,
}

pub fn respond(reply: Reply) -> Response<Body> {
    let body = reply.body.to_string();
    Response::builder()
        .status(reply.status)
        .header("content-type", "application/json")
        .header("server", "brigade-fleet-hub/1")
        .header("cache-control", "no-store")
        .header("referrer-policy", "no-referrer")
        .header("content-length", body.len())
        .body(Body::from(body))
        .unwrap()
}
fn refuse(status: u16, error: &str, code: Option<&str>) -> Response<Body> {
    let mut body = serde_json::json!({ "error": error });
    if let Some(code) = code {
        body["code"] = code.into();
    }
    respond(Reply::new(status, body))
}

/// A body that ended before its declared length, or one past the limit.
pub fn body_refusal(too_large: bool) -> Response<Body> {
    if too_large {
        refuse(
            413,
            "memory request body is too large",
            Some("memory_card_too_large"),
        )
    } else {
        refuse(
            400,
            "incomplete request body",
            Some("memory_request_invalid"),
        )
    }
}
/// A body that did not arrive within the read deadline.
pub fn timeout() -> Response<Body> {
    refuse(408, "request body timeout", Some("memory_request_invalid"))
}

impl MemoryRoute {
    /// Runs before the body is read: availability, method, size and the
    /// credential. The token is hashed inside `authenticate` and never kept.
    pub async fn admit(&self, parts: &Parts) -> Result<Credential, Box<Response<Body>>> {
        let fail = |r: Response<Body>| Err(Box::new(r));
        let MemoryRoute::Store(store) = self else {
            return fail(crate::busy("memory-unavailable"));
        };
        if parts.method != "POST" {
            let mut refusal = refuse(
                405,
                "memory accepts POST only",
                Some("memory_method_invalid"),
            );
            // RFC 9110 section 15.5.6: a 405 names the allowed methods.
            refusal
                .headers_mut()
                .insert("allow", axum::http::HeaderValue::from_static("POST"));
            return fail(refusal);
        }
        if parts.headers.contains_key("transfer-encoding") {
            return fail(refuse(
                411,
                "Content-Length is required",
                Some("memory_request_invalid"),
            ));
        }
        if parts.headers.get_all("content-length").iter().count() > 1 {
            return fail(refuse(
                400,
                "bad Content-Length or Transfer-Encoding",
                Some("memory_request_invalid"),
            ));
        }
        let length = parts
            .headers
            .get("content-length")
            .and_then(|v| v.to_str().ok())
            .and_then(|v| v.parse::<u64>().ok());
        match length {
            None => {
                return fail(refuse(
                    411,
                    "Content-Length is required",
                    Some("memory_request_invalid"),
                ));
            }
            Some(n) if n > BODY_LIMIT => {
                return fail(refuse(
                    413,
                    "memory request body is too large",
                    Some("memory_card_too_large"),
                ));
            }
            _ => {}
        }
        let token = parts
            .headers
            .get("authorization")
            .and_then(|v| v.to_str().ok())
            .and_then(|v| v.strip_prefix("Bearer "))
            .filter(|t| !t.is_empty());
        let Some(token) = token else {
            return fail(refuse(401, "unauthorized", None));
        };
        match store.authenticate(token).await {
            Ok(credential) => Ok(credential),
            Err(reply) => fail(respond(reply)),
        }
    }
    pub async fn handle(&self, credential: &Credential, body: &[u8]) -> Response<Body> {
        match self {
            MemoryRoute::Store(store) => respond(store.handle(credential, body).await),
            MemoryRoute::Unavailable => crate::busy("memory-unavailable"),
        }
    }
}
