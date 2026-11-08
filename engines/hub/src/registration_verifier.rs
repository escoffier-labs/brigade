//! Native enrollment binds the memory verifier flag (phase-memory-verify D4).
//!
//! Before the hub issues a node or message principal credential, the memory
//! writer records that credential's copy of the principal's `verifier` flag.
//! The request may carry its own `verifier` boolean. A request for an
//! unflagged credential of a verifier principal is refused with
//! `memory_verifier_credential_unflagged`, and nothing is issued. Binding
//! runs through the memory store's writer, so this module never opens the
//! memory file and the hub writer never touches it (D23).
use super::{Json, digest, py_trim, valid_node};
use axum::{body::Body, http::Response};
use brigade_hub::memory::{Reply as MemoryReply, Store, ids};
use serde_json::json;

fn refuse(status: u16, code: &str, message: &str) -> Box<Response<Body>> {
    Box::new(crate::memory_route::respond(MemoryReply::new(
        status,
        json!({"error": message, "code": code}),
    )))
}

/// The principal id and credential class an add request would issue.
fn target(path: &str, raw: &Json) -> Option<(String, &'static str)> {
    if path == "/nodes" {
        let id = py_trim(raw.get("node_id").and_then(Json::as_str)?);
        return (raw.get("action").and_then(Json::as_str) == Some("add") && valid_node(id))
            .then(|| (id.to_string(), "node"));
    }
    let action = raw.get("action").unwrap_or(&Json::Null);
    if !(action.is_null() || action == "add") {
        return None;
    }
    let id = format!("agent:{}", raw.get("name").and_then(Json::as_str)?);
    ids::valid_principal(&id).then_some((id, "principal"))
}

/// Strip the request's `verifier` field and bind the copy for the credential
/// the request would issue. `generated` is the token the hub issues when the
/// request names none. An `Err` is the response to send: nothing was issued.
pub async fn bind(
    memory: Option<&Store>,
    path: &str,
    raw: &mut Json,
    generated: &str,
) -> Result<(), Box<Response<Body>>> {
    let requested = match raw.as_object_mut().and_then(|o| o.remove("verifier")) {
        None => None,
        Some(Json::Bool(b)) => Some(b),
        Some(_) => {
            return Err(refuse(
                400,
                "memory_request_invalid",
                "verifier must be a boolean",
            ));
        }
    };
    let Some((principal, class)) = target(path, raw) else {
        if requested.is_some() {
            return Err(refuse(
                400,
                "memory_request_invalid",
                "verifier applies only to a valid add request",
            ));
        }
        return Ok(());
    };
    let Some(store) = memory else {
        // No memory store, so no principal can hold the flag.
        return match requested {
            Some(true) => Err(refuse(
                409,
                "memory_verifier_flag_mismatch",
                "hub memory is not served natively, so no principal is a verifier. Nothing was issued",
            )),
            _ => Ok(()),
        };
    };
    // A message principal enrollment that names its own token issues that one.
    let token = match (path, raw.get("token").and_then(Json::as_str)) {
        ("/message-principals", Some(token)) => token,
        _ => generated,
    };
    let reply = store
        .bind_credential(&principal, class, &digest(token), requested)
        .await;
    if reply.status == 200 {
        Ok(())
    } else {
        Err(Box::new(crate::memory_route::respond(reply)))
    }
}
