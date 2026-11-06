//! Identifiers, validation, normalization and the hub clock for memory v2.
use serde_json::Value as Json;
use sha2::{Digest, Sha256};
use std::{
    path::PathBuf,
    sync::atomic::{AtomicU64, Ordering},
};

const CROCKFORD: &[u8; 32] = b"0123456789ABCDEFGHJKMNPQRSTVWXYZ";
/// The acceptance window for `client_event_id` (29 days) and the clock skew allowance.
pub const WINDOW_MS: i64 = 29 * 24 * 3600 * 1000;
pub const FUTURE_MS: i64 = 5 * 60 * 1000;
/// Idempotency records are kept one day longer than the window.
pub const RETENTION_MS: i64 = 30 * 24 * 3600 * 1000;
/// Largest normalized card, handoff or import text, in bytes.
pub const MAX_TEXT_BYTES: usize = 512 * 1024;

pub fn hex(bytes: &[u8]) -> String {
    const HEX: &[u8] = b"0123456789abcdef";
    let mut out = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        out.push(HEX[(b >> 4) as usize] as char);
        out.push(HEX[(b & 15) as usize] as char);
    }
    out
}
pub fn sha256_hex(bytes: &[u8]) -> String {
    hex(&Sha256::digest(bytes))
}

/// The time inside a 26-character Crockford base32 time-ordered id, in Unix
/// milliseconds. The id is returned in its canonical upper-case spelling, so
/// two spellings of one id share one idempotency key.
pub fn parse_event_id(raw: &str) -> Option<(String, i64)> {
    if raw.len() != 26 {
        return None;
    }
    let upper = raw.to_ascii_uppercase();
    let mut value: u128 = 0;
    for b in upper.bytes() {
        let digit = CROCKFORD.iter().position(|c| *c == b)? as u128;
        value = (value << 5) | digit;
    }
    // 130 bits of text carry 128 bits of id: the first character is at most 7.
    if upper.as_bytes()[0] > b'7' {
        return None;
    }
    Some((upper, (value >> 80) as i64))
}

fn encode(value: u128) -> String {
    (0..26)
        .rev()
        .map(|i| CROCKFORD[((value >> (i * 5)) & 31) as usize] as char)
        .collect()
}
/// A new time-ordered id: 48 bits of milliseconds, then 80 random bits.
pub fn mint(now_ms: i64) -> String {
    let random = random_bytes();
    let mut tail: u128 = 0;
    for b in &random[..10] {
        tail = (tail << 8) | u128::from(*b);
    }
    encode(((now_ms as u128 & ((1 << 48) - 1)) << 80) | tail)
}
#[cfg(test)]
pub fn mint_at(now_ms: i64, tail: u128) -> String {
    encode(((now_ms as u128) << 80) | (tail & ((1 << 80) - 1)))
}

static COUNTER: AtomicU64 = AtomicU64::new(0);
/// 32 unpredictable bytes. The OS source is preferred. Elsewhere the standard
/// library's OS-seeded hasher keys are mixed with a counter and the clock.
/// Ids need uniqueness, not secrecy; no secret is derived from these bytes.
fn random_bytes() -> [u8; 32] {
    #[cfg(unix)]
    {
        use std::io::Read;
        let mut raw = [0u8; 32];
        if std::fs::File::open("/dev/urandom")
            .and_then(|mut f| f.read_exact(&mut raw))
            .is_ok()
        {
            return raw;
        }
    }
    use std::hash::{BuildHasher, Hasher};
    let mut hasher = std::collections::hash_map::RandomState::new().build_hasher();
    hasher.write_u64(COUNTER.fetch_add(1, Ordering::Relaxed));
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    let mut seed = Sha256::new();
    seed.update(hasher.finish().to_le_bytes());
    seed.update(nanos.to_le_bytes());
    seed.update(std::process::id().to_le_bytes());
    seed.finalize().into()
}

/// `[a-z0-9][a-z0-9._-]{0,127}`.
pub fn valid_card_id(s: &str) -> bool {
    let b = s.as_bytes();
    !b.is_empty()
        && b.len() <= 128
        && (b[0].is_ascii_lowercase() || b[0].is_ascii_digit())
        && b.iter()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || b"._-".contains(c))
}
/// A node id, `agent:<name>` or `hub:admin`.
pub fn valid_principal(s: &str) -> bool {
    if s == super::ADMIN {
        return true;
    }
    if let Some(name) = s.strip_prefix("agent:") {
        let b = name.as_bytes();
        return !b.is_empty()
            && b.len() <= 63
            && b[0].is_ascii_lowercase()
            && b.iter()
                .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || b"._-".contains(c));
    }
    let b = s.as_bytes();
    s != "unknown"
        && !b.is_empty()
        && b.len() <= 128
        && b[0].is_ascii_alphanumeric()
        && b.iter()
            .all(|c| c.is_ascii_alphanumeric() || b"._-".contains(c))
}
/// `fleet`, `repo:<identity>`, `collection:<name>` or `principal:<principal id>`.
/// A collection name follows the repository identity rule, so neither form
/// can spell a wildcard.
pub fn valid_scope(s: &str) -> bool {
    if s == "fleet" {
        return true;
    }
    if let Some(name) = s
        .strip_prefix("repo:")
        .or_else(|| s.strip_prefix("collection:"))
    {
        return !name.is_empty()
            && name.len() <= 200
            && name
                .bytes()
                .all(|c| c.is_ascii_graphic() && !b"*\"\\".contains(&c));
    }
    s.strip_prefix("principal:").is_some_and(valid_principal)
}
/// A grant names a scope, `repo:*` or `*`. `*` covers `fleet` and repository
/// scopes, never a `collection:` scope or another principal's private scope.
/// A collection opens only to a grant on its exact scope.
pub fn valid_grant_scope(s: &str) -> bool {
    s == "*" || s == "repo:*" || valid_scope(s)
}

/// UTF-8, LF line endings, exactly one trailing newline. Front matter is kept as written.
pub fn normalize(text: &str) -> String {
    let mut out = text.replace("\r\n", "\n").replace('\r', "\n");
    let trimmed = out.trim_end_matches('\n').len();
    out.truncate(trimmed);
    out.push('\n');
    out
}

/// Title, summary and tags from simple front matter, else the first heading.
pub fn describe(text: &str) -> (Option<String>, Option<String>, Option<String>) {
    let mut title = None;
    let mut summary = None;
    let mut tags = None;
    let mut lines = text.lines();
    let clean = |v: &str| {
        let v = v.trim().trim_matches(|c| c == '"' || c == '\'').trim();
        (!v.is_empty()).then(|| v.chars().take(300).collect::<String>())
    };
    if text.starts_with("---\n") {
        lines.next();
        for line in lines.by_ref() {
            if line == "---" {
                break;
            }
            if let Some((key, value)) = line.split_once(':') {
                match key.trim() {
                    "title" | "name" if title.is_none() => title = clean(value),
                    "summary" | "description" if summary.is_none() => summary = clean(value),
                    "tags" if tags.is_none() => {
                        let list: Vec<String> = value
                            .trim()
                            .trim_start_matches('[')
                            .trim_end_matches(']')
                            .split(',')
                            .filter_map(clean)
                            .take(32)
                            .collect();
                        if !list.is_empty() {
                            tags = Some(serde_json::to_string(&list).unwrap());
                        }
                    }
                    _ => {}
                }
            }
        }
    }
    if title.is_none() {
        title = lines.find_map(|l| l.strip_prefix("# ")).and_then(clean);
    }
    (title, summary, tags)
}

/// Sorted keys and compact separators, so equal requests share one digest.
pub fn canonical(value: &Json) -> String {
    match value {
        Json::Object(map) => {
            let mut keys: Vec<_> = map.keys().collect();
            keys.sort();
            let fields: Vec<String> = keys
                .into_iter()
                .map(|k| {
                    format!(
                        "{}:{}",
                        serde_json::to_string(k).unwrap(),
                        canonical(&map[k])
                    )
                })
                .collect();
            format!("{{{}}}", fields.join(","))
        }
        Json::Array(items) => format!(
            "[{}]",
            items.iter().map(canonical).collect::<Vec<_>>().join(",")
        ),
        other => other.to_string(),
    }
}

/// Wall clock, or the acceptance clock file shared with the Python upstream.
#[derive(Clone, Default, Debug)]
pub struct Clock {
    pub file: Option<PathBuf>,
}
impl Clock {
    pub fn now_ms(&self) -> i64 {
        if let Some(epoch) = self
            .file
            .as_ref()
            .and_then(|p| std::fs::read_to_string(p).ok())
            .and_then(|s| s.trim().parse::<f64>().ok())
            .filter(|v| v.is_finite() && *v >= 0.0 && *v < 253402300799.0)
        {
            return (epoch * 1000.0).round() as i64;
        }
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_millis() as i64)
            .unwrap_or(0)
    }
}
/// Python's `datetime.isoformat()` for a UTC time with millisecond input.
pub fn isoformat(ms: i64) -> String {
    let secs = ms.div_euclid(1000);
    let micros = ms.rem_euclid(1000) * 1000;
    let days = secs.div_euclid(86_400);
    let rem = secs.rem_euclid(86_400);
    // Civil date from days since 1970-01-01 (Howard Hinnant's algorithm).
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = yoe + era * 400 + i64::from(month <= 2);
    let fraction = if micros == 0 {
        String::new()
    } else {
        format!(".{micros:06}")
    };
    format!(
        "{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}{fraction}+00:00",
        rem / 3600,
        rem % 3600 / 60,
        rem % 60
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn event_ids_round_trip_and_reject_bad_spellings() {
        let id = mint(1_700_000_000_123);
        let (canonical, ms) = parse_event_id(&id.to_ascii_lowercase()).unwrap();
        assert_eq!((canonical.as_str(), ms), (id.as_str(), 1_700_000_000_123));
        assert!(parse_event_id("8ZZZZZZZZZZZZZZZZZZZZZZZZZ").is_none());
        assert!(parse_event_id("01HZZZZZZZZZZZZZZZZZZZZZZU").is_none());
        assert!(parse_event_id("short").is_none());
        assert_ne!(mint(1), mint(1));
    }
    #[test]
    fn normalization_and_identity_rules() {
        assert_eq!(normalize("a\r\nb\rc\n\n\n"), "a\nb\nc\n");
        assert_eq!(normalize("x"), "x\n");
        assert!(valid_card_id("build.notes-1") && !valid_card_id("Build"));
        assert!(valid_scope("repo:github.com/escoffier-labs/brigade"));
        assert!(valid_scope("principal:agent:dot") && valid_scope("principal:node-1"));
        assert!(!valid_scope("repo:*") && valid_grant_scope("repo:*"));
        assert!(!valid_scope("principal:unknown") && !valid_scope("other"));
        // A collection name follows the repository identity rule.
        assert!(valid_scope("collection:vault/cerebro") && valid_grant_scope("collection:notes"));
        for bad in [
            "collection:",
            "collection:*",
            "collection:a*b",
            "collection:has space",
            "collection:quote\"",
            "collection:back\\slash",
            "collection",
        ] {
            assert!(!valid_scope(bad) && !valid_grant_scope(bad), "{bad}");
        }
        assert!(valid_scope(&format!("collection:{}", "n".repeat(200))));
        assert!(!valid_scope(&format!("collection:{}", "n".repeat(201))));
        assert_eq!(
            canonical(&serde_json::json!({"b":1,"a":[{"d":2,"c":"x"}]})),
            r#"{"a":[{"c":"x","d":2}],"b":1}"#
        );
    }
    #[test]
    fn python_isoformat_and_front_matter() {
        assert_eq!(isoformat(1_700_000_000_000), "2023-11-14T22:13:20+00:00");
        assert_eq!(isoformat(1_250), "1970-01-01T00:00:01.250000+00:00");
        assert_eq!(isoformat(951_782_400_000), "2000-02-29T00:00:00+00:00");
        let (t, s, tags) = describe("---\ntitle: Build\nsummary: 'How'\ntags: [a, b]\n---\nbody\n");
        assert_eq!(
            (t.as_deref(), s.as_deref(), tags.as_deref()),
            (Some("Build"), Some("How"), Some(r#"["a","b"]"#))
        );
        assert_eq!(describe("intro\n# Heading\n").0.as_deref(), Some("Heading"));
    }
}
