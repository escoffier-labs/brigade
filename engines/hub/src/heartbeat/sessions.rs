//! Local presence, exact provider links and the read-only cloud projection.
use super::*;
const COLUMNS: &str = "node_id,harness,session_id,repo_identity,identity_scope,repo_label,checkout_path,branch,dirty_paths_json,dirty_truncated,state,started_at,heartbeat_at,ended_at,ttl_seconds,expires_at,cloud_context_json,provider_link_json";
pub(super) fn python_only(raw: &Json) -> bool {
    raw.get("cloud_context").is_some()
        // Python's frozenset membership raises on unhashable actions.
        || raw.get("action").is_some_and(|v| v.is_array() || v.is_object())
        || raw.as_object().is_some_and(|o| {
            o.keys()
                .any(|s| !s.is_ascii() || controls(s) || s.contains(['\'', '"', '\\']))
        })
}
fn required(raw: &Json, field: &str, limit: usize) -> Result<String> {
    let Some(s) = raw
        .get(field)
        .and_then(Json::as_str)
        .filter(|s| !py_trim(s).is_empty())
    else {
        return invalid(&format!(
            "session field '{field}' must be a non-empty string"
        ));
    };
    let s = py_trim(s);
    if controls(s) {
        return Err(error(
            422,
            &format!("session field '{field}' must not contain control characters"),
        ));
    }
    if s.chars().count() > limit {
        return invalid(&format!(
            "session field '{field}' must be at most {limit} characters"
        ));
    }
    Ok(s.into())
}
fn opaque(raw: &Json, field: &str) -> Result<String> {
    let s = required(raw, field, 128)?;
    if !s.as_bytes()[0].is_ascii_alphanumeric()
        || !s
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"._:-".contains(&b))
    {
        return invalid(&format!(
            "session field '{field}' must be an opaque identity"
        ));
    }
    Ok(s)
}
struct Request {
    action: String,
    key: Vec<Value>,
    fields: Vec<Value>,
    link: bool,
}
fn validate(raw: &Json, caller: Option<&str>) -> Result<Request> {
    let Some(o) = raw.as_object() else {
        return invalid("session request must be a JSON object");
    };
    let action = raw.get("action").and_then(Json::as_str).unwrap_or("");
    if !matches!(action, "upsert" | "end") {
        return invalid("session field 'action' must be 'upsert' or 'end'");
    }
    let allowed = if action == "end" {
        &[
            "action",
            "harness",
            "session_id",
            "repo_identity",
            "node_id",
        ][..]
    } else {
        &[
            "action",
            "harness",
            "session_id",
            "repo_identity",
            "node_id",
            "identity_scope",
            "repo_label",
            "checkout_path",
            "branch",
            "dirty_paths",
            "dirty_truncated",
            "ttl_seconds",
            "provider_link",
            "cloud_context",
        ][..]
    };
    for key in o.keys() {
        if !allowed.contains(&key.as_str()) {
            return invalid(&format!("session request has unsupported field '{key}'"));
        }
    }
    let node = match caller {
        Some(node) => {
            if o.contains_key("node_id") {
                return invalid("session field 'node_id' is not allowed on node writes");
            }
            node.to_string()
        }
        None => required(raw, "node_id", 128)?,
    };
    // Session identities permit the literal unknown, unlike claims.
    if node.is_empty()
        || !node.as_bytes()[0].is_ascii_alphanumeric()
        || !node
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b))
    {
        return invalid("session field 'node_id' is not a valid node identity");
    }
    let harness = opaque(raw, "harness")?;
    let session = opaque(raw, "session_id")?;
    let repo = required(raw, "repo_identity", 512)?;
    if !repo_identity(&repo) {
        return invalid("session field 'repo_identity' must be a credential-free identity");
    }
    let key = vec![text(&node), text(&harness), text(&session), text(&repo)];
    if action == "end" {
        return Ok(Request {
            action: action.into(),
            key,
            fields: vec![],
            link: false,
        });
    }
    let scope = required(raw, "identity_scope", 16)?;
    if !matches!(scope.as_str(), "fleet" | "node") {
        return invalid("session field 'identity_scope' must be 'fleet' or 'node'");
    }
    let label = required(raw, "repo_label", 256)?;
    let checkout = required(raw, "checkout_path", 1024)?;
    let branch = match raw.get("branch").unwrap_or(&Json::Null) {
        Json::Null => Value::Null,
        Json::String(s) if py_trim(s).is_empty() => Value::Null,
        Json::String(_) => text(&required(raw, "branch", 256)?),
        _ => return invalid("session field 'branch' must be a string"),
    };
    let Some(paths) = raw.get("dirty_paths").and_then(Json::as_array) else {
        return invalid("session field 'dirty_paths' must be an array of strings");
    };
    if paths.len() > 64 {
        return invalid("session field 'dirty_paths' must have at most 64 entries");
    }
    let mut dirty = Vec::new();
    for item in paths {
        let Some(s) = item.as_str().filter(|s| !s.is_empty()) else {
            return invalid("session field 'dirty_paths' must be an array of strings");
        };
        if controls(s) {
            return Err(error(
                422,
                "session field 'dirty_paths' must not contain control characters",
            ));
        }
        let s = s.replace('\\', "/");
        if s.starts_with('/') || s.split('/').any(|p| p == "..") {
            return invalid("session field 'dirty_paths' must be repository-relative Git paths");
        }
        if s.chars().count() > 512 {
            return invalid("session field 'dirty_paths' entries must be at most 512 characters");
        }
        dirty.push(serde_json::to_string(&s).unwrap());
    }
    let dirty = format!("[{}]", dirty.join(", "));
    if dirty.len() > 32768 {
        return invalid("session field 'dirty_paths' exceeds the encoded size limit");
    }
    let Some(truncated) = raw.get("dirty_truncated").and_then(Json::as_bool) else {
        return invalid("session field 'dirty_truncated' must be a boolean");
    };
    let ttl = raw.get("ttl_seconds").unwrap_or(&Json::Null);
    let ttl = if o.contains_key("ttl_seconds") {
        ttl.as_i64().unwrap_or(0)
    } else {
        900
    };
    if !(120..=3600).contains(&ttl) {
        return invalid("session field 'ttl_seconds' must be an integer from 120 to 3600");
    }
    let link = raw.get("provider_link").unwrap_or(&Json::Null);
    let link = if link.is_null() {
        Value::Null
    } else {
        if harness != "t3"
            || link.as_object().is_none_or(|o| {
                o.len() != 2 || !o.contains_key("harness") || !o.contains_key("session_id")
            })
            || !matches!(link["harness"].as_str(), Some("claude" | "codex"))
        {
            return invalid(
                "session provider_link must identify a Claude or Codex session observed by T3",
            );
        }
        let id = match opaque(link, "session_id") {
            Ok(s) => s,
            Err(Error::Refusal(r)) => {
                return Err(Error::Refusal(Reply {
                    status: r.status,
                    body: r.body.replace("'session_id'", "'provider_link.session_id'"),
                }));
            }
            Err(e) => return Err(e),
        };
        text(&object(&[
            ("harness", quote(link["harness"].as_str().unwrap())),
            ("session_id", quote(&id)),
        ]))
    };
    Ok(Request {
        action: action.into(),
        key,
        fields: vec![
            text(&scope),
            text(&label),
            text(&checkout),
            branch,
            text(&dirty),
            Value::Integer(i64::from(truncated)),
            Value::Integer(ttl),
            link,
        ],
        link: o.contains_key("provider_link"),
    })
}
fn payload(row: &[Value], now: f64) -> Option<String> {
    let names = [
        "node_id",
        "harness",
        "session_id",
        "repo_identity",
        "identity_scope",
        "repo_label",
        "checkout_path",
        "branch",
        "dirty_paths",
        "dirty_truncated",
        "state",
        "started_at",
        "heartbeat_at",
        "ended_at",
        "ttl_seconds",
        "expires_at",
    ];
    let mut fields: Vec<(&str, String)> = names
        .iter()
        .zip(row)
        .map(|(k, v)| (*k, sql_json(v)))
        .collect();
    let dirty = stored_json(string(&row[8]))?;
    let dirty = if dirty.is_array() { dirty } else { json!([]) };
    fields[8].1 = json_wire(&dirty);
    fields[9].1 = (row[9] != Value::Integer(0)).to_string();
    if row[17] != Value::Null {
        fields.push(("provider_link", json_wire(&stored_json(string(&row[17]))?)));
    }
    if row[16] != Value::Null {
        let context = stored_json(string(&row[16]))?;
        fields.push(("cloud_context", json_wire(&context)));
        if context["version"] == 2 {
            fields.push(("evidence_verification", quote("reported-unverified")));
        }
        fields[6].1 = "null".into();
        if string(&row[3]).is_empty() {
            fields[3].1 = "null".into();
        }
        fields.push(("provider_lifecycle", quote("unobserved")));
        fields.push(("inventory_coverage", quote("explicitly-reported-sessions")));
        let expires = match row[15] {
            Value::Real(f) => f,
            Value::Integer(i) => i as f64,
            _ => 0.0,
        };
        fields.push((
            "presence_state",
            quote(if string(&row[10]) == "active" && expires > now {
                "active"
            } else {
                "stale"
            }),
        ));
    }
    Some(object(&fields))
}
fn fetch(tx: &(impl ReadTx + ?Sized), key: &[Value], now: f64) -> Result<String> {
    let rows = tx.query(&format!("SELECT {COLUMNS} FROM interactive_sessions WHERE node_id=? AND harness=? AND session_id=? AND repo_identity=?"),key)?;
    rows.first().map_or_else(
        || Ok("null".into()),
        |r| payload(r, now).ok_or(Error::Python),
    )
}
pub(super) fn store(tx: &dyn WriteTx, raw: &Json, caller: Option<&str>, now: f64) -> Result<Reply> {
    let r = validate(raw, caller)?;
    let (sql, params) = if r.action == "end" {
        (
            "SELECT cloud_context_json FROM interactive_sessions WHERE node_id=? AND harness=? AND session_id=? AND repo_identity=?",
            r.key.as_slice(),
        )
    } else {
        (
            "SELECT cloud_context_json FROM interactive_sessions WHERE node_id=? AND harness=? AND session_id=?",
            &r.key[..3],
        )
    };
    let prior = tx.query(sql, params)?;
    if prior.iter().any(|row| row[0] != Value::Null) {
        return invalid(if r.action == "end" {
            "cloud provider lifecycle is unobserved; end is unsupported"
        } else {
            "session cloud linkage cannot change"
        });
    }
    let stamp = text(&iso(now));
    if r.action == "end" {
        let mut params = vec![stamp.clone(), stamp];
        params.extend(r.key.clone());
        tx.execute("UPDATE interactive_sessions SET state='ended',ended_at=COALESCE(ended_at,?),heartbeat_at=? WHERE node_id=? AND harness=? AND session_id=? AND repo_identity=?",&params)?;
    } else {
        if matches!(string(&r.key[1]), "claude" | "codex") {
            let mut params = vec![stamp.clone()];
            params.extend(r.key.clone());
            tx.execute("UPDATE interactive_sessions SET state='ended',ended_at=? WHERE node_id=? AND harness=? AND session_id=? AND repo_identity!=? AND state='active'",&params)?;
        }
        let mut params = r.key.clone();
        params.extend(r.fields[..6].iter().cloned());
        let ttl = match r.fields[6] {
            Value::Integer(i) => i,
            _ => unreachable!(),
        };
        params.extend([
            stamp.clone(),
            stamp,
            r.fields[6].clone(),
            Value::Real(now + ttl as f64),
            r.fields[7].clone(),
            Value::Real(now),
            Value::Integer(i64::from(r.link)),
        ]);
        tx.execute("INSERT INTO interactive_sessions(node_id,harness,session_id,repo_identity,identity_scope,repo_label,checkout_path,branch,dirty_paths_json,dirty_truncated,state,started_at,heartbeat_at,ended_at,ttl_seconds,expires_at,cloud_context_json,provider_link_json) VALUES(?,?,?,?,?,?,?,?,?,?,'active',?,?,NULL,?,?,NULL,?) ON CONFLICT(node_id,harness,session_id,repo_identity) DO UPDATE SET identity_scope=excluded.identity_scope,repo_label=excluded.repo_label,checkout_path=excluded.checkout_path,branch=excluded.branch,dirty_paths_json=excluded.dirty_paths_json,dirty_truncated=excluded.dirty_truncated,state='active',started_at=CASE WHEN interactive_sessions.state='active' AND interactive_sessions.expires_at>? THEN interactive_sessions.started_at ELSE excluded.started_at END,heartbeat_at=excluded.heartbeat_at,ended_at=NULL,ttl_seconds=excluded.ttl_seconds,expires_at=excluded.expires_at,cloud_context_json=NULL,provider_link_json=CASE WHEN ? THEN excluded.provider_link_json ELSE interactive_sessions.provider_link_json END",&params)?;
    }
    Ok(Reply::new(200, &[("session", fetch(tx, &r.key, now)?)]))
}
pub(super) fn list(tx: &dyn ReadTx, all: bool, now: f64) -> Result<Option<Reply>> {
    let rows = tx.query(&format!("SELECT {COLUMNS} FROM interactive_sessions WHERE (?=1 OR (state='active' AND expires_at>?)) AND NOT (harness='t3' AND provider_link_json IS NOT NULL AND state='active' AND expires_at>? AND EXISTS (SELECT 1 FROM interactive_sessions AS provider WHERE provider.node_id=interactive_sessions.node_id AND provider.harness=json_extract(interactive_sessions.provider_link_json,'$.harness') AND provider.session_id=json_extract(interactive_sessions.provider_link_json,'$.session_id') AND provider.checkout_path=interactive_sessions.checkout_path AND provider.state='active' AND provider.expires_at>?)) ORDER BY heartbeat_at DESC,started_at DESC,node_id,harness,session_id LIMIT ?"),&[Value::Integer(i64::from(all)),Value::Real(now),Value::Real(now),Value::Real(now),Value::Integer(if all {1000} else {500})])?;
    let Some(sessions) = rows
        .iter()
        .map(|r| payload(r, now))
        .collect::<Option<Vec<_>>>()
    else {
        return Ok(None);
    };
    Ok(Some(Reply::new(200, &[("sessions", array(sessions))])))
}
