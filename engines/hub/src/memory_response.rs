//! Shared memory HTTP response encoding.
use axum::{body::Body, http::Response};
use serde_json::Value as Json;

pub fn respond(status: u16, value: Json) -> Response<Body> {
    let body = value.to_string();
    Response::builder()
        .status(status)
        .header("content-type", "application/json")
        .header("server", "brigade-fleet-hub/1")
        .header("cache-control", "no-store")
        .header("referrer-policy", "no-referrer")
        .header("content-length", body.len())
        .body(Body::from(body))
        .unwrap()
}
