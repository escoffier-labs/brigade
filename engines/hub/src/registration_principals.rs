//! Principal enrollment, generation fencing, grants and revocation.
use super::*;
fn conflict<T>(message: &str, code: Option<&str>) -> Result<T> {
    let mut fields = vec![("error", quote(message))];
    if let Some(code) = code {
        fields.push(("code", quote(code)));
    }
    Err(DomainError::Refusal(Reply::new(409, &fields)))
}
fn name_valid(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 63
        && s.as_bytes()[0].is_ascii_lowercase()
        && s.bytes()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || b"._-".contains(&c))
}
fn opaque(s: &Json, min: usize) -> bool {
    s.as_str().is_some_and(|s| {
        s.len() >= min
            && s.len() <= 128
            && s.bytes()
                .all(|c| c.is_ascii_alphanumeric() || b"_-".contains(&c))
    })
}
fn field<'a>(raw: &'a Json, key: &str, limit: usize) -> Result<&'a str> {
    match raw.get(key).and_then(Json::as_str) {
        Some(s) if !s.trim().is_empty() && s.len() <= limit => Ok(s),
        _ => refuse(&format!(
            "{key} must be a non-empty UTF-8 string of at most {limit} bytes"
        )),
    }
}
fn patterns(raw: &Json) -> Result<Vec<String>> {
    let Some(items) = raw.as_array().filter(|a| a.len() <= 64) else {
        return refuse("may_send_to must be a list of at most 64 patterns");
    };
    let mut out = Vec::new();
    for item in items {
        let Some(s) = item.as_str() else {
            return refuse("invalid recipient grant pattern");
        };
        if !(matches!(s, "*" | "node:*" | "agent:*")
            || s.strip_prefix("agent:")
                .map(name_valid)
                .unwrap_or_else(|| valid_node(s)))
        {
            return refuse("invalid recipient grant pattern");
        }
        out.push(s.to_string());
    }
    out.sort();
    out.dedup();
    Ok(out)
}
fn grants<T: ReadTx + ?Sized>(tx: &T, id: &str) -> rusqlite::Result<Vec<String>> {
    Ok(tx
        .query(
            "SELECT pattern FROM fleet_message_send_grants WHERE principal_id=?1 ORDER BY pattern",
            &[text(id)],
        )?
        .iter()
        .map(|r| string(&r[0]).to_string())
        .collect())
}
fn grants_json(patterns: &[String]) -> String {
    array(patterns.iter().map(|s| quote(s)))
}
fn replace(
    tx: &dyn WriteTx,
    id: &str,
    patterns: &[String],
    stamp: &str,
    epoch: &str,
) -> Result<()> {
    tx.execute(
        "DELETE FROM fleet_message_send_grants WHERE principal_id=?1",
        &[text(id)],
    )?;
    for pattern in patterns {
        tx.execute("INSERT INTO fleet_message_send_grants(principal_id,pattern,created_at) VALUES(?1,?2,?3)",&[text(id),text(pattern),text(stamp)])?;
    }
    tx.execute(
        "UPDATE fleet_message_principals SET grant_epoch=?1 WHERE principal_id=?2",
        &[text(epoch), text(id)],
    )?;
    Ok(())
}
pub fn revoke_keys(tx: &dyn WriteTx, id: &str, stamp: &str) -> rusqlite::Result<()> {
    tx.execute("UPDATE fleet_message_keys SET state='revoked',revoked_at=COALESCE(revoked_at,?1),compromised_from=CASE WHEN compromised_from IS NOT NULL AND julianday(compromised_from)<=julianday(created_at) THEN compromised_from ELSE created_at END WHERE principal_id=?2",&[text(stamp),text(id)])?;
    Ok(())
}
pub fn audit(
    tx: &dyn WriteTx,
    id: &str,
    action: &str,
    detail: Json,
    stamp: &str,
) -> rusqlite::Result<()> {
    tx.execute(
        "INSERT INTO fleet_audit_log(at,actor,action,subject,detail) VALUES(?1,'admin',?2,?3,?4)",
        &[
            text(stamp),
            text(action),
            text(id),
            text(&detail.to_string()),
        ],
    )?;
    Ok(())
}
fn get<T: ReadTx + ?Sized>(tx: &T, name: &str) -> Result<Option<String>> {
    let id = format!("agent:{name}");
    let rows=tx.query("SELECT principal_id,name,enrollment_id,revoked_at FROM fleet_message_principals WHERE principal_id=?1",&[text(&id)])?;
    let Some(r) = rows.first() else {
        return Ok(None);
    };
    Ok(Some(object(&[
        ("principal_id", value(&r[0])),
        ("name", value(&r[1])),
        ("enrollment_id", value(&r[2])),
        ("revoked", (r[3] != Value::Null).to_string()),
        ("may_send_to", grants_json(&grants(tx, &id)?)),
    ])))
}
fn envelope(principal: Option<String>, status: u16) -> Reply {
    match principal {
        Some(p) => Reply::new(status, &[("principal", p)]),
        None => Reply::error(404, "principal not found"),
    }
}
pub fn handle(
    tx: &dyn WriteTx,
    raw: &Json,
    stamp: &str,
    generated_token: &str,
    seeds: &[String; 3],
) -> Result<Reply> {
    let action = raw.get("action").and_then(Json::as_str).unwrap_or("add");
    let keys = |allowed: &[&str]| {
        raw.as_object().is_some_and(|o| {
            o.len() == allowed.len() && o.keys().all(|k| allowed.contains(&k.as_str()))
        })
    };
    if action == "get" {
        if !keys(&["action", "name"]) {
            return refuse("principal lookup requires action='get' and name");
        }
        return Ok(envelope(get(tx, field(raw, "name", 63)?)?, 200));
    }
    if action == "revoke" {
        if !keys(&["action", "name", "expected_enrollment_id"]) {
            return refuse(
                "principal revocation requires action='revoke', name, and expected enrollment generation",
            );
        }
        let name = field(raw, "name", 63)?;
        let id = format!("agent:{name}");
        let expected = &raw["expected_enrollment_id"];
        if !expected.is_null() && !opaque(expected, 16) {
            return refuse("expected_enrollment_id is invalid");
        }
        let rows = tx.query(
            "SELECT enrollment_id,revoked_at FROM fleet_message_principals WHERE principal_id=?1",
            &[text(&id)],
        )?;
        let Some(r) = rows.first() else {
            return Ok(envelope(None, 200));
        };
        if value(&r[0])
            != expected
                .as_str()
                .map(quote)
                .unwrap_or_else(|| "null".into())
        {
            return conflict(
                "principal enrollment generation changed",
                Some("principal_enrollment_changed"),
            );
        }
        let changed=tx.execute("UPDATE fleet_message_principals SET revoked_at=?1 WHERE principal_id=?2 AND enrollment_id IS ?3 AND revoked_at IS NULL",&[text(stamp),text(&id),r[0].clone()])?;
        revoke_keys(tx, &id, stamp)?;
        if changed == 1 {
            audit(tx, &id, "member.remove", json!({"principal_id":id}), stamp)?;
        }
        return Ok(envelope(
            Some(object(&[
                ("principal_id", quote(&id)),
                ("name", quote(name)),
                ("enrollment_id", value(&r[0])),
            ])),
            200,
        ));
    }
    if action == "grants" {
        let node = raw.get("node_id").is_some();
        if !keys(&[
            "action",
            if node { "node_id" } else { "name" },
            "may_send_to",
        ]) {
            return refuse(
                "principal grants requires action='grants', one target, and may_send_to",
            );
        }
        let target = raw[if node { "node_id" } else { "name" }]
            .as_str()
            .unwrap_or("");
        if !(if node {
            valid_node(target)
        } else {
            name_valid(target)
        }) {
            return refuse("invalid principal grant target");
        }
        let id = if node {
            target.into()
        } else {
            format!("agent:{target}")
        };
        let patterns = patterns(&raw["may_send_to"])?;
        let principal = if node {
            if tx
                .query(
                    "SELECT 1 FROM nodes WHERE node_id=?1 AND revoked_at IS NULL",
                    &[text(&id)],
                )?
                .is_empty()
            {
                return refuse("principal grant target is not active");
            }
            object(&[("principal_id", quote(&id)), ("node_id", quote(target))])
        } else {
            match get(tx, target)? {
                Some(p) => p,
                None => return Ok(envelope(None, 200)),
            }
        };
        replace(tx, &id, &patterns, stamp, &seeds[2])?;
        audit(tx, &id, "grants.change", json!({"grants":patterns}), stamp)?;
        let principal = if node {
            format!(
                "{}, \"may_send_to\": {}}}",
                principal.trim_end_matches('}'),
                grants_json(&patterns)
            )
        } else {
            get(tx, target)?.unwrap()
        };
        return Ok(envelope(Some(principal), 200));
    }
    if action != "add" {
        return refuse("principal action must be add, get, grants, or revoke");
    }
    let Some(o) = raw.as_object() else {
        return refuse("principal enrollment requires only name and optional label");
    };
    if o.keys().any(|k| {
        ![
            "action",
            "name",
            "label",
            "request_id",
            "token",
            "expected_enrollment_id",
            "may_send_to",
        ]
        .contains(&k.as_str())
    }) {
        return refuse("principal enrollment requires only name and optional label");
    }
    if raw
        .get("action")
        .is_some_and(|v| !v.is_null() && !v.is_string())
    {
        return refuse("principal action must be add, get, grants, or revoke");
    }
    let patterns = patterns(raw.get("may_send_to").unwrap_or(&json!([])))?;
    let name = field(raw, "name", 63)?;
    let label = if raw.get("label").is_none() {
        name
    } else {
        field(raw, "label", 128)?
    };
    if !name_valid(name) {
        return refuse(
            "name must start with a lowercase letter and contain only lowercase letters, digits, dots, underscores, or hyphens",
        );
    }
    let id = format!("agent:{name}");
    let expected = raw.get("expected_enrollment_id").unwrap_or(&Json::Null);
    if !expected.is_null() && !opaque(expected, 16) {
        return refuse("expected_enrollment_id is invalid");
    }
    let mut request = raw.get("request_id").cloned().unwrap_or(Json::Null);
    let mut token = raw.get("token").cloned().unwrap_or(Json::Null);
    if request.is_null() && token.is_null() {
        request = Json::String(seeds[0].clone());
        token = Json::String(generated_token.into());
    }
    if request.is_null() != token.is_null() {
        return refuse("principal enrollment request_id and token must be provided together");
    }
    if !opaque(&request, 16) || !opaque(&token, 32) {
        return refuse("principal enrollment request_id or token is invalid");
    }
    let request = request.as_str().unwrap();
    let token = token.as_str().unwrap();
    let hash = digest(token);
    let rows=tx.query("SELECT revoked_at,enrollment_id,token_hash,label FROM fleet_message_principals WHERE principal_id=?1",&[text(&id)])?;
    let mut replay = false;
    if let Some(r) = rows.first() {
        if r[0] != Value::Null {
            if string(&r[1]) == request {
                return conflict(
                    "revoked principal enrollment cannot be replayed",
                    Some("principal_enrollment_revoked"),
                );
            }
            if !o.contains_key("expected_enrollment_id")
                || value(&r[1])
                    != expected
                        .as_str()
                        .map(quote)
                        .unwrap_or_else(|| "null".into())
            {
                return conflict(
                    "principal enrollment generation changed",
                    Some("principal_enrollment_changed"),
                );
            }
        } else if string(&r[1]) == request {
            if !bool::from(string(&r[2]).as_bytes().ct_eq(hash.as_bytes()))
                || string(&r[3]) != label
            {
                return conflict(
                    "request_id was already used for a different principal enrollment",
                    None,
                );
            }
            if grants(tx, &id)? != patterns {
                return conflict(
                    "principal send grants changed since enrollment. Inspect principals show or revoke before re-enrolling",
                    Some("principal_enrollment_grants_changed"),
                );
            }
            replay = true;
        } else {
            return refuse("message principal already exists");
        }
    } else if !expected.is_null() {
        return conflict(
            "principal enrollment generation changed",
            Some("principal_enrollment_changed"),
        );
    }
    if !replay {
        let epoch = &seeds[1];
        tx.execute("INSERT INTO fleet_message_principals(principal_id,name,label,token_hash,created_at,enrollment_id,authorization_enrollment_epoch) VALUES(?1,?2,?3,?4,?5,?6,?7) ON CONFLICT(principal_id) DO UPDATE SET label=excluded.label,token_hash=excluded.token_hash,created_at=excluded.created_at,revoked_at=NULL,enrollment_id=excluded.enrollment_id,authorization_enrollment_epoch=excluded.authorization_enrollment_epoch",&[text(&id),text(name),text(label),text(&hash),text(stamp),text(request),text(epoch)])?;
        replace(tx, &id, &patterns, stamp, &seeds[2])?;
    }
    Ok(envelope(
        Some(object(&[
            ("principal_id", quote(&id)),
            ("name", quote(name)),
            ("label", quote(label)),
            ("token", quote(token)),
            ("enrollment_id", quote(request)),
            ("may_send_to", grants_json(&patterns)),
        ])),
        201,
    ))
}
