//! Shadow comparison never receives a writable connection. Rules are shared with the drive.
use serde_json::Value;
use sha2::{Digest, Sha256};
pub const RULES: &str =
    include_str!("../../../registry/skills/verify-brigade/hub-shadow-rules.json");
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Class {
    Match,
    Definite,
    Transient,
    Inconclusive,
    Boundary,
    Projection,
}
impl Class {
    pub fn label(self) -> &'static str {
        match self {
            Self::Match => "match",
            Self::Definite => "definite",
            Self::Transient => "transient",
            Self::Inconclusive => "inconclusive",
            Self::Boundary => "boundary",
            Self::Projection => "projection",
        }
    }
}
/// Each observation records equality and whether another connection committed.
/// Stop retrying immediately at a stable mismatch; at most three observations.
pub fn classify(observations: &[(bool, bool)]) -> Class {
    for (i, (equal, committed)) in observations.iter().take(3).enumerate() {
        if *equal {
            return if i == 0 {
                Class::Match
            } else {
                Class::Transient
            };
        }
        if !committed {
            return Class::Definite;
        }
    }
    Class::Inconclusive
}
fn first_difference(a: &Value, b: &Value, path: &str) -> Option<String> {
    if a == b {
        return None;
    }
    if let (Some(a), Some(b)) = (a.as_object(), b.as_object()) {
        for key in a.keys().chain(b.keys()) {
            let next = format!("{path}/{}", key.replace('~', "~0").replace('/', "~1"));
            match (a.get(key), b.get(key)) {
                (Some(a), Some(b)) => {
                    if let Some(p) = first_difference(a, b, &next) {
                        return Some(p);
                    }
                }
                _ => return Some(next),
            }
        }
    } else if let (Some(a), Some(b)) = (a.as_array(), b.as_array()) {
        for i in 0..a.len().max(b.len()) {
            let next = format!("{path}/{i}");
            match (a.get(i), b.get(i)) {
                (Some(a), Some(b)) => {
                    if let Some(p) = first_difference(a, b, &next) {
                        return Some(p);
                    }
                }
                _ => return Some(next),
            }
        }
    }
    Some(path.into())
}
/// Exact by default. Clock exceptions apply only to declared numeric JSON pointers.
pub fn compare(family: &str, a: &Value, b: &Value, elapsed: f64) -> (Class, String) {
    let rules: Value = serde_json::from_str(RULES).expect("checked-in shadow rules");
    compare_rules(&rules["families"][family], a, b, elapsed)
}
pub fn compare_rules(rules: &Value, a: &Value, b: &Value, elapsed: f64) -> (Class, String) {
    let mut a = a.clone();
    let mut b = b.clone();
    if let Some(paths) = rules["clock_seconds"].as_array() {
        for path in paths.iter().filter_map(Value::as_str) {
            if let (Some(x), Some(y)) = (
                a.pointer(path).and_then(Value::as_f64),
                b.pointer(path).and_then(Value::as_f64),
            ) {
                if (x - y).abs() <= elapsed.max(0.0) {
                    *a.pointer_mut(path).unwrap() = Value::Null;
                    *b.pointer_mut(path).unwrap() = Value::Null;
                }
            }
        }
    }
    // Each namespace maps generated values to first-seen order. Stored IDs
    // remain exact unless the route explicitly declares their JSON pointers.
    if let Some(groups) = rules["generated_ids"].as_array() {
        for group in groups {
            for value in [&mut a, &mut b] {
                let mut ids: Vec<Value> = Vec::new();
                for path in group
                    .as_array()
                    .into_iter()
                    .flatten()
                    .filter_map(Value::as_str)
                {
                    if let Some(item) = value.pointer_mut(path) {
                        let rank = ids.iter().position(|id| id == item).unwrap_or_else(|| {
                            ids.push(item.clone());
                            ids.len() - 1
                        });
                        *item = Value::from(rank);
                    }
                }
            }
        }
    }
    match first_difference(&a, &b, "") {
        None => (Class::Match, String::new()),
        Some(path) => (Class::Definite, path),
    }
}
/// Budget exhaustion is handler metadata, never inferred from a short page.
pub fn boundary(a: &[Value], b: &[Value], both_byte_limited: bool) -> bool {
    both_byte_limited && a.len() != b.len() && a.iter().zip(b).all(|(x, y)| x == y)
}
/// The handler computes eligibility and the complete lifecycle projection from
/// its read snapshot. Compare the complete result so unrelated fields stay exact.
pub fn projection(rust: &Value, projected_python: &Value, eligible: bool) -> bool {
    eligible && rust == projected_python
}
pub fn record(
    route: &str,
    action: &str,
    statuses: (u16, u16),
    class: Class,
    path: &str,
    bodies: (&[u8], &[u8]),
) -> Value {
    serde_json::json!({"event":"shadow","route":route,"action":action,"python_status":statuses.0,"rust_status":statuses.1,"result":class.label(),"path":path,"python_sha256":format!("{:x}",Sha256::digest(bodies.0)),"rust_sha256":format!("{:x}",Sha256::digest(bodies.1))})
}

/// Return the exception as a comparator result only with handler evidence.
pub fn compare_projection(a: &Value, b: &Value, projected: &Value, eligible: bool) -> Class {
    if a == b {
        Class::Match
    } else if projection(b, projected, eligible) {
        Class::Projection
    } else {
        Class::Definite
    }
}
pub fn compare_boundary(a: &[Value], b: &[Value], both_byte_limited: bool) -> Class {
    if a == b {
        Class::Match
    } else if boundary(a, b, both_byte_limited) {
        Class::Boundary
    } else {
        Class::Definite
    }
}

/// Dedicated read-only watcher. Blocking SQLite calls stay off the runtime.
pub struct Watch(std::sync::Arc<std::sync::Mutex<rusqlite::Connection>>);
impl Watch {
    pub async fn open(path: std::path::PathBuf) -> Option<Self> {
        tokio::task::spawn_blocking(move || {
            let c = rusqlite::Connection::open_with_flags(
                path,
                rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
            )
            .ok()?;
            c.execute_batch("PRAGMA query_only=ON;").ok()?;
            Some(Self(std::sync::Arc::new(std::sync::Mutex::new(c))))
        })
        .await
        .ok()
        .flatten()
    }
    pub async fn sample(&self) -> Option<i64> {
        let c = self.0.clone();
        tokio::task::spawn_blocking(move || {
            c.lock()
                .ok()?
                .pragma_query_value(None, "data_version", |r| r.get(0))
                .ok()
        })
        .await
        .ok()
        .flatten()
    }
}

/// The native health shape and the drive read the same contract data.
pub fn health_shape() -> Value {
    let rules: Value = serde_json::from_str(RULES).expect("checked-in shadow rules");
    rules["families"]["health"]["shape"].clone()
}
