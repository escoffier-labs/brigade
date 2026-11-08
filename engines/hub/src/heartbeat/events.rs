//! Event validation and deduplicated insertion, with atomic outbox writes.
use super::*;
const FIELDS: [(&str, usize); 10] = [
    ("node_id", 128),
    ("run_id", 16384),
    ("state", 128),
    ("ts", 64),
    ("digest", 128),
    ("repo", 1024),
    ("seat", 512),
    ("harness", 512),
    ("capability_fingerprint", 128),
    ("repo_identity", 512),
];
pub(super) fn python_only(raw: &Json) -> bool {
    let check = |e: &Json| {
        !e.get("context").unwrap_or(&Json::Null).is_null()
            || e.get("sequence")
                .and_then(Json::as_u64)
                .is_some_and(|v| v > i64::MAX as u64)
            || ["sequence", "exit_status"].iter().any(|field| {
                e.get(*field).is_some_and(|v| {
                    v.as_i64().is_none()
                        && v.as_u64().is_none()
                        && v.as_f64().is_some_and(|f| {
                            (f == 0.0 && f.is_sign_negative()) || f.abs() >= 9223372036854775808.0
                        })
                })
            })
            || e.get("node_id")
                .and_then(Json::as_str)
                .is_some_and(|s| !s.is_ascii() || controls(s))
    };
    raw.as_array()
        .map_or_else(|| check(raw), |a| a.iter().any(check))
}
fn validate(raw: &Json) -> Result<Vec<Value>> {
    if !raw.is_object() {
        return invalid("event must be a JSON object");
    }
    let mut fields = Vec::new();
    for (i, (name, limit)) in FIELDS[..10].iter().enumerate() {
        // Python validates sequence between ts and digest.
        if i == 4 {
            let Some(_) = raw.get("sequence").and_then(Json::as_u64) else {
                return invalid("event field 'sequence' must be a non-negative integer");
            };
        }
        let val = raw.get(*name).and_then(Json::as_str);
        if i < 5 && val.is_none_or(|s| py_trim(s).is_empty()) {
            return invalid(&format!("event field '{name}' must be a non-empty string"));
        }
        if let Some(s) = val {
            if s.chars().count() > *limit {
                return Err(error(
                    422,
                    &format!("event field '{name}' must be at most {limit} characters"),
                ));
            }
            if controls(s) {
                return Err(error(
                    422,
                    &format!("event field '{name}' must not contain control characters"),
                ));
            }
        }
        let val = val.filter(|s| !py_trim(s).is_empty());
        let val = if *name == "repo_identity" {
            val.map(py_trim)
        } else {
            val
        };
        if *name == "repo_identity" && val.is_some_and(|s| !repo_identity(s)) {
            return invalid("event field 'repo_identity' must be a credential-free identity");
        }
        fields.push(val.map(text).unwrap_or(Value::Null));
    }
    let exit = raw.get("exit_status").unwrap_or(&Json::Null);
    if !exit.is_null() && exit.as_i64().is_none() {
        return invalid("event field 'exit_status' must be a signed 64-bit integer");
    }
    // SQL order: node,run,sequence,digest,repo,seat,harness,state,ts,exit,cap,identity,context.
    Ok(vec![
        fields[0].clone(),
        fields[1].clone(),
        Value::Integer(raw["sequence"].as_u64().unwrap() as i64),
        fields[4].clone(),
        fields[5].clone(),
        fields[6].clone(),
        fields[7].clone(),
        fields[2].clone(),
        fields[3].clone(),
        exit.as_i64().map(Value::Integer).unwrap_or(Value::Null),
        fields[8].clone(),
        fields[9].clone(),
        Value::Null,
    ])
}
pub(super) fn store(
    tx: &dyn WriteTx,
    raw: &Json,
    caller: Option<&str>,
    now: f64,
    seed: &str,
    route: &Heartbeat,
) -> Result<Reply> {
    let items: Vec<&Json> = match raw {
        Json::Object(_) => vec![raw],
        Json::Array(a) => a.iter().collect(),
        _ => return invalid("body must be an event object or array of events"),
    };
    if items.len() > 1000 {
        return invalid("too many events in one POST (max 1000)");
    }
    let events = items
        .into_iter()
        .map(validate)
        .collect::<Result<Vec<_>>>()?;
    for e in &events {
        if let Some(caller) = caller {
            let node = string(&e[0]);
            if node != caller {
                return Err(error(
                    403,
                    &format!(
                        "event node_id {} does not match the caller's node token ({caller})",
                        repr(node)
                    ),
                ));
            }
            if string(&e[6]) == "grokbot" {
                return invalid(
                    "event field 'harness' is reserved for hub-owned grokbot lifecycle",
                );
            }
        }
    }
    let mut accepted = 0;
    let total = events.len();
    for (index, mut e) in events.into_iter().enumerate() {
        e.push(text(&iso(now)));
        let changed = tx.execute("INSERT OR IGNORE INTO events (node_id,run_id,sequence,digest,repo,seat,harness,state,ts,exit_status,capability_fingerprint,repo_identity,context,received_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", &e)?;
        accepted += changed;
        if changed != 0 && string(&e[7]) == "run.failed" && route.events {
            failed(tx, &e, seed, index, route)?;
        }
    }
    Ok(Reply::new(
        200,
        &[
            ("accepted", accepted.to_string()),
            ("duplicate", (total - accepted).to_string()),
        ],
    ))
}
fn failed(
    tx: &dyn WriteTx,
    e: &[Value],
    seed: &str,
    index: usize,
    route: &Heartbeat,
) -> Result<()> {
    let now = route
        .clock
        .as_deref()
        .and_then(|p| std::fs::read_to_string(p).ok())
        .and_then(|s| s.trim().parse::<f64>().ok())
        .filter(|v| v.is_finite() && *v >= 0.0 && *v < 253402300799.0)
        .unwrap_or_else(|| {
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_secs_f64()
        });
    let node = string(&e[0]);
    if tx.query("SELECT 1 FROM fleet_event_subscriptions WHERE name='run.failed' AND (expires_at IS NULL OR expires_at>?) LIMIT 1", &[Value::Real(now)])?.is_empty() { return Ok(()); }
    if matches!(tx.query("SELECT COUNT(*) FROM fleet_events WHERE name='run.failed' AND producer=? AND created_at>?", &[text(node), Value::Real(now-3600.0)])?.first().map(|r| &r[0]),Some(Value::Integer(n)) if *n>=120)
    {
        return Ok(());
    }
    let mut data = vec![("node_id", quote(node)), ("run_id", sql_json(&e[1]))];
    if e[9] != Value::Null {
        data.push(("exit_status", sql_json(&e[9])));
    }
    let ts = string(&e[8]);
    if (10..=40).contains(&ts.len())
        && ts.as_bytes()[0].is_ascii_digit()
        && ts
            .bytes()
            .all(|b| b.is_ascii_digit() || b"T:.+Z-".contains(&b))
    {
        data.push(("failed_at", quote(ts)));
    }
    // Python's compact encoder, preserving field order.
    let data = format!(
        "{{{}}}",
        data.iter()
            .map(|(k, v)| format!("{}:{v}", quote(k)))
            .collect::<Vec<_>>()
            .join(",")
    );
    tx.execute("INSERT OR IGNORE INTO fleet_events(event_id,name,subject,producer,data,created_at) VALUES(?,'run.failed',?,?,?,?)", &[text(&format!("evt_{}", &digest(&format!("{seed}:{index}"))[..32])),text(node),text(node),text(&data),Value::Real(now)])?;
    Ok(())
}

fn repr(s: &str) -> String {
    let q = if s.contains('\'') && !s.contains('"') {
        '"'
    } else {
        '\''
    };
    format!(
        "{q}{}{q}",
        s.replace('\\', "\\\\").replace(q, &format!("\\{q}"))
    )
}
