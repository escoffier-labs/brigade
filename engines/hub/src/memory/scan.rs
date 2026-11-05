//! Versioned guard scanning for memory writes and prompt loads (D24, S18).
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Scan {
    /// No engine or rule set. Never treated as clean.
    Unavailable,
    /// Input-specific engine failure. Refuse writes, never store pending.
    Failed,
    Clean {
        ruleset: String,
    },
    /// Stored as `flagged`, never pushed, never raised.
    Flagged {
        ruleset: String,
        rules: Vec<String>,
    },
    /// Refused with `memory_content_blocked` and the rule ids, never the text.
    Blocked {
        ruleset: String,
        rules: Vec<String>,
    },
}
impl Scan {
    pub fn state(&self) -> &'static str {
        match self {
            Scan::Unavailable => "pending",
            Scan::Failed => "failed",
            Scan::Clean { .. } => "clean",
            Scan::Flagged { .. } => "flagged",
            Scan::Blocked { .. } => "blocked",
        }
    }
    pub fn ruleset(&self) -> &str {
        match self {
            Scan::Unavailable => "unavailable",
            Scan::Failed => "failed",
            Scan::Clean { ruleset }
            | Scan::Flagged { ruleset, .. }
            | Scan::Blocked { ruleset, .. } => ruleset,
        }
    }
    pub fn rules(&self) -> &[String] {
        match self {
            Scan::Flagged { rules, .. } | Scan::Blocked { rules, .. } => rules,
            _ => &[],
        }
    }
    /// Only a clean scan under a known rule set may raise trust.
    pub fn permits_raise(&self) -> bool {
        matches!(self, Scan::Clean { .. })
    }
}
/// Runs inside the authorized memory transaction on the exact stored bytes.
pub trait Scanner: Send + Sync + 'static {
    fn scan(&self, normalized: &str) -> Scan;
    /// `status.scan`: `(available, ruleset_version)`.
    fn describe(&self) -> (bool, Option<String>);
}
/// The S13a hook: reports unavailable for every input.
pub struct Unavailable;
impl Scanner for Unavailable {
    fn scan(&self, _: &str) -> Scan {
        Scan::Unavailable
    }
    fn describe(&self) -> (bool, Option<String>) {
        (false, None)
    }
}

impl Scan {
    /// All warnings require human review. S19 must use this floor, never a
    /// submitter's claimed tier. Rule ids remain attached to the content hash.
    pub fn tier_floor(&self) -> u8 {
        if matches!(self, Self::Flagged { .. }) {
            2
        } else {
            0
        }
    }
}

/// A statically linked engine cannot disappear from PATH. Builds without it,
/// failed rule compilation, or a pin mismatch use the same fail-closed hook.
pub fn bundled() -> std::sync::Arc<dyn Scanner> {
    #[cfg(feature = "memory-scan")]
    if let Some(scanner) = native::Guard::new() {
        return std::sync::Arc::new(scanner);
    }
    std::sync::Arc::new(Unavailable)
}

/// The single prompt-load boundary for native memory surfaces. Callers pass
/// only the exact fields they intend to inject. No text survives a finding.
pub fn prompt_hit(
    scanner: &dyn Scanner,
    card_id: &str,
    hit: serde_json::Value,
    fields: &[&str],
) -> serde_json::Value {
    let joined = fields
        .iter()
        .map(|s| s.strip_prefix('\u{feff}').unwrap_or(s))
        .collect::<Vec<_>>()
        .join("\n");
    for text in fields
        .iter()
        .copied()
        .chain(std::iter::once(joined.as_str()))
    {
        let result = scanner.scan(text);
        let rule = match &result {
            Scan::Unavailable => Some("memory_scan_unavailable"),
            Scan::Failed => Some("memory_scan_failed"),
            Scan::Blocked { rules, .. } | Scan::Flagged { rules, .. } => rules
                .first()
                .map(String::as_str)
                .or(Some("memory_scan_flagged")),
            Scan::Clean { .. } => None,
        };
        if let Some(rule) = rule {
            return serde_json::json!({"card_id": card_id, "blocked": rule});
        }
    }
    hit
}

#[cfg(feature = "memory-scan")]
mod native {
    use super::*;
    use brigade_guard::Regex;
    use serde_json::Value;

    struct Rule {
        id: String,
        block: bool,
        utf: Regex,
        ascii: Regex,
    }
    pub struct Guard {
        version: String,
        injections: Vec<Rule>,
        rules: Vec<Rule>,
    }
    // Leave room for linear scans of a supported card, including bounded
    // salutation near-matches. Keep a deterministic ceiling on backtracking.
    const MATCH_LIMIT: usize = 16 * super::super::ids::MAX_TEXT_BYTES;

    fn rules(values: &[Value]) -> Option<Vec<Rule>> {
        values
            .iter()
            .map(|v| {
                let pattern = format!("(*LIMIT_MATCH={MATCH_LIMIT}){}", v["pattern"].as_str()?);
                let flags = v["flags"].as_u64()? as u32;
                Some(Rule {
                    id: v["id"].as_str()?.into(),
                    block: v["action"].as_str()? == "block",
                    utf: brigade_guard::compile(&pattern, flags, true).ok()?,
                    // Run byte-class matching as well on exact UTF-8 bytes. A hit
                    // in either mode counts, including the S18a divergent rules.
                    ascii: brigade_guard::compile(&pattern, flags, false).ok()?,
                })
            })
            .collect()
    }
    fn emoji_base(c: char) -> bool {
        matches!(c as u32, 0x2600 | 0x2764 | 0x1F680 | 0x1F600..=0x1F64F)
    }
    // Unicode 16 Default_Ignorable_Code_Point, shared with Python.
    fn ignorable(c: char) -> bool {
        matches!(c as u32, 0xAD | 0x34F | 0x61C | 0x115F..=0x1160 | 0x17B4..=0x17B5 |
            0x180B..=0x180F | 0x200B..=0x200F | 0x202A..=0x202E | 0x2060..=0x206F |
            0x3164 | 0xFE00..=0xFE0F | 0xFEFF | 0xFFA0 | 0xFFF0..=0xFFF8 |
            0x1BCA0..=0x1BCA3 | 0x1D173..=0x1D17A | 0xE0000..=0xE0FFF)
    }
    fn whitespace(c: char) -> bool {
        matches!(c as u32, 0x9..=0xD | 0x1C..=0x20 | 0x85 | 0xA0 | 0x1680 |
            0x2000..=0x200A | 0x2028..=0x2029 | 0x202F | 0x205F | 0x3000)
    }
    fn scan_copies(text: &str) -> (String, String, String) {
        let lines: String = text
            .replace("\r\n", "\n")
            .chars()
            .filter(|c| !ignorable(*c))
            .map(|c| match c as u32 {
                0xA..=0xD | 0x1C..=0x1E | 0x85 | 0x2028..=0x2029 => '\n',
                0xFF01..=0xFF5E => char::from_u32(c as u32 - 0xFEE0).unwrap(),
                0x3000 => ' ',
                _ => c,
            })
            .collect();
        let normalized = lines
            .split(whitespace)
            .filter(|s| !s.is_empty())
            .collect::<Vec<_>>()
            .join(" ");
        let injection_lines = lines
            .split('\n')
            .map(|line| {
                line.split(whitespace)
                    .filter(|s| !s.is_empty())
                    .collect::<Vec<_>>()
                    .join(" ")
            })
            .collect::<Vec<_>>()
            .join("\n");
        (lines, injection_lines, normalized)
    }
    impl Guard {
        pub fn new() -> Option<Self> {
            if !brigade_guard::pinned() {
                return None;
            }
            let bundle: Value = serde_json::from_str(include_str!(
                "../../../../src/brigade/guard/memory-rules.json"
            ))
            .ok()?;
            Some(Self {
                version: format!(
                    "{}:{}",
                    bundle["version"].as_str()?,
                    super::super::ids::sha256_hex(include_bytes!(
                        "../../../../src/brigade/guard/memory-rules.json"
                    ))
                ),
                injections: rules(bundle["injections"].as_array()?)?,
                rules: rules(bundle["rules"].as_array()?)?,
            })
        }
        fn inspect(&self, text: &str) -> Scan {
            let mut blocks = Vec::new();
            let mut warnings = Vec::new();
            let mut previous = None;
            for (index, c) in text.chars().enumerate() {
                let rule = match c as u32 {
                    0x61C | 0x200E | 0x200F | 0x202A..=0x202E | 0x2066..=0x2069 => {
                        Some(("bidi-control", true))
                    }
                    0xFEFF if index == 0 => None,
                    0x200C | 0x200D => Some(("zero-width-joiner", false)),
                    0xFE00..=0xFE0F if previous.is_some_and(emoji_base) => None,
                    0xFFF9..=0xFFFB => Some(("invisible-character", true)),
                    _ if ignorable(c) => Some(("invisible-character", true)),
                    _ => None,
                };
                previous = Some(c);
                if let Some((id, block)) = rule {
                    let ids = if block { &mut blocks } else { &mut warnings };
                    if !ids.iter().any(|s| s == id) {
                        ids.push(id.to_string());
                    }
                }
            }
            let (lines, injection_lines, normalized) = scan_copies(text);
            let mut failed = false;
            for (rules, injection) in [(&self.injections, true), (&self.rules, false)] {
                for rule in rules {
                    let mut matched = false;
                    for copy in [text, &lines]
                        .into_iter()
                        .chain(injection.then_some(injection_lines.as_str()))
                        .chain(injection.then_some(normalized.as_str()))
                    {
                        for regex in [&rule.utf, &rule.ascii] {
                            match regex.is_match(copy.as_bytes()) {
                                Ok(hit) => matched |= hit,
                                Err(_) => failed = true,
                            }
                        }
                    }
                    if matched {
                        let ids = if rule.block {
                            &mut blocks
                        } else {
                            &mut warnings
                        };
                        if !ids.contains(&rule.id) {
                            ids.push(rule.id.clone());
                        }
                    }
                }
            }
            if !blocks.is_empty() {
                blocks.extend(warnings);
                Scan::Blocked {
                    ruleset: self.version.clone(),
                    rules: blocks,
                }
            } else if failed {
                Scan::Failed
            } else if !warnings.is_empty() {
                Scan::Flagged {
                    ruleset: self.version.clone(),
                    rules: warnings,
                }
            } else {
                Scan::Clean {
                    ruleset: self.version.clone(),
                }
            }
        }
    }
    impl Scanner for Guard {
        fn scan(&self, text: &str) -> Scan {
            self.inspect(text)
        }
        fn describe(&self) -> (bool, Option<String>) {
            (true, Some(self.version.clone()))
        }
    }
    #[cfg(test)]
    mod tests {
        use super::*;

        fn error_guard() -> (Guard, String) {
            let mut guard = Guard::new().unwrap();
            let pattern = "(*LIMIT_MATCH=1)(*NO_START_OPT)(*NO_AUTO_POSSESS)(a+)+$";
            let utf = brigade_guard::compile(pattern, 0, true).unwrap();
            let ascii = brigade_guard::compile(pattern, 0, false).unwrap();
            let text = format!("{}!", "a".repeat(100));
            assert!(utf.is_match(text.as_bytes()).is_err());
            guard.injections.clear();
            guard.rules = vec![Rule {
                id: "test-limit".into(),
                block: false,
                utf,
                ascii,
            }];
            (guard, text)
        }

        #[test]
        fn s18_round2_production_limit_still_fails_closed() {
            let mut guard = Guard::new().unwrap();
            guard.injections.clear();
            guard.rules = rules(&[serde_json::json!({
                "id": "pathological", "action": "warn", "flags": 0,
                "pattern": "(*NO_START_OPT)(*NO_AUTO_POSSESS)(a+)+$"
            })])
            .unwrap();
            assert_eq!(guard.scan(&format!("{}!", "a".repeat(100))), Scan::Failed);
        }

        #[test]
        fn s18_review_match_error_preserves_blocks() {
            let (guard, text) = error_guard();
            assert!(matches!(
                guard.scan(&format!("\u{202e}{text}")),
                Scan::Blocked { .. }
            ));
        }

        #[test]
        fn s18_review_match_error_is_distinct_from_missing_engine() {
            let (guard, text) = error_guard();
            let failed = guard.scan(&text);
            assert_eq!(failed.state(), "failed");
            assert!(!failed.permits_raise());
        }

        #[tokio::test]
        async fn s18_review_match_error_refuses_every_write_without_storage() {
            use crate::memory::tests::{ADMIN_TOKEN, Hub};
            let (guard, text) = error_guard();
            let h = Hub::with(std::sync::Arc::new(guard), None);
            for action in ["put", "import", "handoff"] {
                let mut body =
                    serde_json::json!({"action":action, "text":text, "client_event_id":h.id()});
                if action == "handoff" {
                    body["kind"] = serde_json::json!("note");
                } else {
                    body["scope"] = serde_json::json!("fleet");
                    body["card_id"] = serde_json::json!("failed");
                }
                let r = h.call(ADMIN_TOKEN, body).await;
                assert_eq!(
                    (r.status, r.body["code"].as_str()),
                    (503, Some("memory_scan_failed")),
                    "{action}: {:?}",
                    r.body
                );
                assert_eq!(r.body["retryable"], false, "{action}: {:?}", r.body);
                assert!(
                    r.body["error"]
                        .as_str()
                        .unwrap()
                        .contains("retrying will not help")
                );
            }
            let c = rusqlite::Connection::open(h.memory()).unwrap();
            for table in [
                "memory_blobs",
                "memory_scans",
                "memory_events",
                "memory_idempotency",
            ] {
                assert_eq!(
                    c.query_row(&format!("SELECT count(*) FROM {table}"), [], |r| r
                        .get::<_, i64>(0))
                        .unwrap(),
                    0
                );
            }
        }
    }
}
