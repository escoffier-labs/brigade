//! Sealed envelopes (#2005): format checks only, never decryption. Python's
//! `fleet_hub_sealed` and `fleet_hub_message_keys` are the oracle.
use super::{Fail, Policy};
use crate::wire::quote;
use crate::writer::ReadTx;
use rusqlite::types::Value;
use serde_json::{Map, Value as Json};

const ROUTING: [&str; 14] = [
    "v",
    "suite",
    "hub",
    "from",
    "from_kid",
    "to",
    "kind",
    "priority",
    "client_id",
    "created_at",
    "deadline",
    "in_reply_to",
    "pad",
    "rcpt",
];
const DIGESTS: [&str; 3] = ["ct_sha256", "wraps_sha256", "rc"];
const CONTENT: [&str; 3] = ["sig", "ct", "recipients"];
/// `MAX_ENVELOPE_BYTES` in `fleet_hub_sealed`.
pub const ENVELOPE_LIMIT: i64 = 96 * 1024;

/// `json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=True)`.
pub fn canonical(value: &Json) -> String {
    let mut out = String::new();
    write_canonical(value, &mut out);
    out
}
fn write_canonical(value: &Json, out: &mut String) {
    match value {
        Json::Null => out.push_str("null"),
        Json::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Json::Number(n) => match (n.as_i64(), n.as_u64(), n.as_f64()) {
            (Some(i), _, _) => out.push_str(&i.to_string()),
            (_, Some(u), _) => out.push_str(&u.to_string()),
            (_, _, Some(f)) => out.push_str(&crate::wire::py_float(f)),
            _ => out.push_str("null"),
        },
        Json::String(s) => out.push_str(&quote(s)),
        Json::Array(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                write_canonical(item, out);
            }
            out.push(']');
        }
        // serde_json's map is ordered by key bytes, which is Python's code point order.
        Json::Object(map) => {
            out.push('{');
            for (i, (key, item)) in map.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                out.push_str(&quote(key));
                out.push(':');
                write_canonical(item, out);
            }
            out.push('}');
        }
    }
}

/// `decode_binary`: unpadded URL-safe base64 of exactly `size` bytes, canonical.
pub fn binary(value: &Json, size: usize) -> bool {
    let Some(text) = value.as_str() else {
        return false;
    };
    let bytes = text.as_bytes();
    if bytes.len() != (size * 8).div_ceil(6) || bytes.is_empty() {
        return false;
    }
    let mut bits: u32 = 0;
    let mut count = 0;
    let mut decoded = 0;
    for &b in bytes {
        let v = match b {
            b'A'..=b'Z' => b - b'A',
            b'a'..=b'z' => b - b'a' + 26,
            b'0'..=b'9' => b - b'0' + 52,
            b'-' => 62,
            b'_' => 63,
            _ => return false,
        };
        bits = (bits << 6) | u32::from(v);
        count += 6;
        if count >= 8 {
            count -= 8;
            decoded += 1;
            bits &= (1 << count) - 1;
        }
    }
    // Leftover bits must be zero, or re-encoding would differ.
    decoded == size && bits == 0
}
fn days_in_month(year: i64, month: i64) -> i64 {
    match month {
        2 if (year % 4 == 0 && year % 100 != 0) || year % 400 == 0 => 29,
        2 => 28,
        4 | 6 | 9 | 11 => 30,
        _ => 31,
    }
}
fn digits(bytes: &[u8]) -> Option<i64> {
    if bytes.is_empty() || !bytes.iter().all(u8::is_ascii_digit) {
        return None;
    }
    std::str::from_utf8(bytes).ok()?.parse().ok()
}
/// A date and time Python's `fromisoformat` accepts: year, month, day, hour,
/// minute, second and microseconds, before any offset.
pub(crate) fn civil<'a>(
    bytes: &'a [u8],
    fraction_lengths: &[usize],
) -> Option<([i64; 6], i64, &'a [u8])> {
    if bytes.len() < 19
        || bytes[4] != b'-'
        || bytes[7] != b'-'
        || bytes[10] != b'T'
        || bytes[13] != b':'
        || bytes[16] != b':'
    {
        return None;
    }
    let field = |a: usize, b: usize| digits(&bytes[a..b]);
    let parts = [
        field(0, 4)?,
        field(5, 7)?,
        field(8, 10)?,
        field(11, 13)?,
        field(14, 16)?,
        field(17, 19)?,
    ];
    let [year, month, day, hour, minute, second] = parts;
    if year < 1
        || !(1..=12).contains(&month)
        || day < 1
        || day > days_in_month(year, month)
        || hour > 23
        || minute > 59
        || second > 59
    {
        return None;
    }
    let mut rest = &bytes[19..];
    let mut micros = 0;
    if rest.first() == Some(&b'.') {
        let n = rest[1..].iter().take_while(|b| b.is_ascii_digit()).count();
        if !fraction_lengths.contains(&n) {
            return None;
        }
        let mut value = digits(&rest[1..=n])?;
        for _ in n..6 {
            value *= 10;
        }
        micros = value;
        rest = &rest[1 + n..];
    }
    Some((parts, micros, rest))
}
/// `fleet_hub_message_keys.timestamp`: the mesh timestamp pattern, then a real date.
pub fn timestamp(value: &Json) -> bool {
    let Some(text) = value.as_str() else {
        return false;
    };
    let bytes = text.as_bytes();
    if bytes.len() > 32 {
        return false;
    }
    let Some((_, _, rest)) = civil(bytes, &[3, 6]) else {
        return false;
    };
    match rest {
        b"Z" => true,
        [b'+' | b'-', h1, h2, b':', m1, m2] => {
            let (Some(h), Some(m)) = (digits(&[*h1, *h2]), digits(&[*m1, *m2])) else {
                return false;
            };
            h <= 23 && m <= 59
        }
        _ => false,
    }
}
fn principal(value: &Json) -> bool {
    let Some(s) = value.as_str() else {
        return false;
    };
    if let Some(name) = s.strip_prefix("agent:") {
        return super::principal_name(name);
    }
    let b = s.as_bytes();
    !b.is_empty()
        && b.len() <= 128
        && b[0].is_ascii_alphanumeric()
        && b.iter()
            .all(|c| c.is_ascii_alphanumeric() || b"._-".contains(c))
}
fn hub(value: &Json) -> bool {
    match value.as_str() {
        Some("mesh") => true,
        Some(s) => s
            .strip_prefix("h_")
            .is_some_and(|rest| binary(&Json::String(rest.into()), 16)),
        None => false,
    }
}
pub fn kid(value: &Json) -> bool {
    value
        .as_str()
        .and_then(|s| s.strip_prefix("k1_"))
        .is_some_and(|rest| binary(&Json::String(rest.into()), 16))
}
fn client_id(value: &Json) -> bool {
    value.as_str().is_some_and(|s| {
        (1..=64).contains(&s.len())
            && s.bytes()
                .all(|c| c.is_ascii_alphanumeric() || c == b'_' || c == b'-')
    })
}
fn int(value: &Json) -> Option<i64> {
    match value {
        Json::Number(n) if n.is_i64() || n.is_u64() => n.as_i64(),
        _ => None,
    }
}
/// `_parse`: the bm1 wire format, with nesting bounded before decoding.
fn parse(encoded: &str) -> Option<Map<String, Json>> {
    let bytes = encoded.as_bytes();
    if !encoded.is_ascii() || bytes.contains(&0) {
        return None;
    }
    let (mut depth, mut quoted, mut escaped) = (0i32, false, false);
    for &b in bytes {
        if quoted {
            if escaped {
                escaped = false;
            } else if b == b'\\' {
                escaped = true;
            } else if b == b'"' {
                quoted = false;
            }
        } else if b == b'"' {
            quoted = true;
        } else if b == b'[' || b == b'{' {
            depth += 1;
            if depth > 4 {
                return None;
            }
        } else if b == b']' || b == b'}' {
            depth -= 1;
            if depth < 0 {
                return None;
            }
        }
    }
    if depth != 0 || quoted {
        return None;
    }
    // A duplicate key, a non-finite number or any other spelling Python's
    // json.loads tolerates fails the canonical comparison at the end.
    let Json::Object(value) = serde_json::from_str::<Json>(encoded).ok()? else {
        return None;
    };
    let allowed = |k: &str| ROUTING.contains(&k) || DIGESTS.contains(&k) || CONTENT.contains(&k);
    let required = ROUTING
        .iter()
        .filter(|k| !matches!(**k, "deadline" | "in_reply_to"))
        .chain(DIGESTS.iter())
        .chain(CONTENT.iter());
    if !value.keys().all(|k| allowed(k)) || !required.clone().all(|k| value.contains_key(*k)) {
        return None;
    }
    let get = |k: &str| &value[k];
    let ok = int(get("v")) == Some(1)
        && get("suite") == "bm1"
        && hub(get("hub"))
        && principal(get("from"))
        && principal(get("to"))
        && kid(get("from_kid"))
        && matches!(
            get("kind").as_str(),
            Some("message" | "question" | "completion")
        )
        && matches!(
            get("priority").as_str(),
            Some("low" | "normal" | "high" | "urgent")
        )
        && client_id(get("client_id"))
        && timestamp(get("created_at"))
        && ((get("kind") == "question") == value.contains_key("deadline"))
        && value.get("deadline").is_none_or(timestamp);
    if !ok {
        return None;
    }
    if let Some(link) = value.get("in_reply_to") {
        let Json::Object(link) = link else {
            return None;
        };
        if link.len() != 3
            || !link.get("hub").is_some_and(hub)
            || !link.get("from").is_some_and(principal)
            || !link.get("client_id").is_some_and(client_id)
        {
            return None;
        }
    }
    let pad = int(get("pad"))?;
    if !(1024..=65 * 1024).contains(&pad) || pad % 1024 != 0 {
        return None;
    }
    let Json::Array(rcpt) = get("rcpt") else {
        return None;
    };
    if !(1..=2).contains(&rcpt.len()) || !rcpt.iter().all(kid) {
        return None;
    }
    let kids: Vec<&str> = rcpt.iter().filter_map(Json::as_str).collect();
    if kids.windows(2).any(|w| w[0] >= w[1]) || !kids.contains(&get("from_kid").as_str()?) {
        return None;
    }
    if !DIGESTS.iter().all(|k| binary(get(k), 32))
        || !binary(get("sig"), 64)
        || !binary(get("ct"), pad as usize + 16)
    {
        return None;
    }
    let Json::Array(recipients) = get("recipients") else {
        return None;
    };
    if recipients.len() != rcpt.len() {
        return None;
    }
    for (kid, entry) in rcpt.iter().zip(recipients) {
        let Json::Object(entry) = entry else {
            return None;
        };
        if entry.len() != 2
            || entry.get("kid") != Some(kid)
            || !entry.get("wrap").is_some_and(|w| binary(w, 1168))
        {
            return None;
        }
    }
    (canonical(&Json::Object(value.clone())) == encoded).then_some(value)
}

/// `prepare`: the envelope a send or reply carries, checked against the caller.
pub fn prepare(
    raw: &Map<String, Json>,
    sender: &str,
    reply_to: bool,
    policy: &Policy,
) -> Result<Map<String, Json>, Fail> {
    let allowed: [&str; 3] = if raw.get("action").and_then(Json::as_str) == Some("reply") {
        ["action", "envelope", "id"]
    } else {
        ["action", "envelope", "reply_to"]
    };
    if raw.keys().any(|k| !allowed.contains(&k.as_str())) {
        return Err(Fail::error(
            "an envelope cannot accompany plaintext or routing fields",
        ));
    }
    let Some(text) = raw["envelope"].as_str().filter(|t| t.is_ascii()) else {
        return Err(Fail::code(
            "envelope must be canonical ASCII JSON text",
            "envelope_malformed",
        ));
    };
    if text.len() as i64 > policy.max_envelope_bytes.min(ENVELOPE_LIMIT) {
        return Err(Fail::code(
            "sealed envelope exceeds storage limit",
            "message_too_large",
        ));
    }
    let Some(value) = parse(text) else {
        return Err(Fail::code("invalid sealed envelope", "envelope_malformed"));
    };
    if value["from"] != sender {
        return Err(Fail::Forbidden {
            message: "envelope sender must match caller".into(),
            code: "message_sender_mismatch",
        });
    }
    if reply_to != value.contains_key("in_reply_to") {
        return Err(Fail::error(
            "reply_to and signed in_reply_to must both be present or absent",
        ));
    }
    Ok(value)
}

fn active_kid<T: ReadTx + ?Sized>(tx: &T, principal: &str) -> rusqlite::Result<Option<String>> {
    Ok(tx
        .query(
            "SELECT kid FROM fleet_message_keys WHERE principal_id=? AND state='active'",
            &[Value::Text(principal.into())],
        )?
        .into_iter()
        .next()
        .and_then(|r| match &r[0] {
            Value::Text(s) => Some(s.clone()),
            _ => None,
        }))
}
/// `require_current_keys`.
pub fn require_current_keys<T: ReadTx + ?Sized>(
    tx: &T,
    value: &Map<String, Json>,
) -> Result<(), Fail> {
    let from = value["from"].as_str().unwrap_or_default();
    let to = value["to"].as_str().unwrap_or_default();
    let sender = active_kid(tx, from)?;
    if sender.as_deref().is_none_or(|kid| value["from_kid"] != kid) {
        return Err(Fail::conflict(
            "sender key is no longer active",
            "sender_key_stale",
        ));
    }
    let sender = sender.unwrap();
    let recipient = active_kid(tx, to)?;
    let expected: Vec<Json> = match &recipient {
        Some(r) => {
            let mut kids = vec![sender.clone(), r.clone()];
            kids.sort();
            kids.dedup();
            kids.into_iter().map(Json::String).collect()
        }
        None => vec![],
    };
    if (from != to && recipient.as_deref() == Some(sender.as_str()))
        || value["rcpt"] != Json::Array(expected)
    {
        return Err(Fail::conflict(
            "recipient key is no longer active",
            "recipient_key_stale",
        ));
    }
    Ok(())
}
/// `admit`: sender and fleet sealed storage quotas. `cutoff` leaves out rows
/// a bounded prune has not deleted yet, which Python's full prune would have.
pub fn admit<T: ReadTx + ?Sized>(
    tx: &T,
    sender: &str,
    size: i64,
    policy: &Policy,
    cutoff: &str,
) -> Result<(), Fail> {
    let used = super::count(
        tx,
        "SELECT COALESCE(SUM(envelope_bytes),0) FROM fleet_messages WHERE sender=? AND created_at>=?",
        &[Value::Text(sender.into()), Value::Text(cutoff.into())],
    )?;
    if used + size > policy.sender_storage_bytes {
        return Err(Fail::quota(
            "sender sealed storage quota exceeded",
            "sender_storage_quota",
            None,
        ));
    }
    if let Some(limit) = policy.fleet_storage_bytes {
        let total = super::count(
            tx,
            "SELECT COALESCE(SUM(envelope_bytes),0) FROM fleet_messages WHERE created_at>=?",
            &[Value::Text(cutoff.into())],
        )?;
        if total + size > limit {
            return Err(Fail::quota(
                "hub sealed storage is full",
                "hub_storage_full",
                None,
            ));
        }
    }
    Ok(())
}
/// The bytes `purge_content` writes for a stored envelope: no `ct` or `recipients`.
pub fn tombstone(envelope: &str) -> Option<String> {
    let Json::Object(mut value) = serde_json::from_str::<Json>(envelope).ok()? else {
        return None;
    };
    value.remove("ct")?;
    value.remove("recipients")?;
    Some(canonical(&Json::Object(value)))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn binary_is_canonical_unpadded_base64url() {
        assert!(binary(&Json::String("AAAAAAAAAAAAAAAAAAAAAA".into()), 16));
        // 16 bytes need 22 characters; the last carries 4 spare bits that must be zero.
        assert!(!binary(&Json::String("AAAAAAAAAAAAAAAAAAAAAB".into()), 16));
        assert!(!binary(&Json::String("AAAAAAAAAAAAAAAAAAAAA=".into()), 16));
        assert!(!binary(&Json::String("AAAA".into()), 16));
        assert!(binary(&Json::String("_-8".into()), 2));
    }
    #[test]
    fn mesh_timestamps_need_a_real_date_and_offset() {
        let ok = |s: &str| timestamp(&Json::String(s.into()));
        assert!(ok("2026-03-02T00:00:00Z"));
        assert!(ok("2024-02-29T23:59:59.123+05:30"));
        assert!(ok("2026-03-02T00:00:00.123456-23:59"));
        assert!(!ok("2026-02-29T00:00:00Z"));
        assert!(!ok("2026-03-02T24:00:00Z"));
        assert!(!ok("2026-03-02T00:00:00"));
        assert!(!ok("2026-03-02T00:00:00.12Z"));
        assert!(!ok("0000-03-02T00:00:00Z"));
        assert!(!ok("2026-03-02T00:00:00+24:00"));
    }
    #[test]
    fn canonical_sorts_and_escapes_like_python() {
        let v: Json = serde_json::from_str(r#"{"b":[1,"é"],"a":{"z":null,"y":true}}"#).unwrap();
        assert_eq!(
            canonical(&v),
            r#"{"a":{"y":true,"z":null},"b":[1,"\u00e9"]}"#
        );
    }
}
