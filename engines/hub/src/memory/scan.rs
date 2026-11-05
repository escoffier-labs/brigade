//! Versioned guard scanning for memory writes and prompt loads (D24, S18).
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Scan {
    /// No engine or rule set. Never treated as clean.
    Unavailable,
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
            Scan::Clean { .. } => "clean",
            Scan::Flagged { .. } => "flagged",
            Scan::Blocked { .. } => "blocked",
        }
    }
    pub fn ruleset(&self) -> &str {
        match self {
            Scan::Unavailable => "unavailable",
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
            Scan::Blocked { rules, .. } | Scan::Flagged { rules, .. } => {
                rules.first().map(String::as_str)
            }
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
    fn rules(values: &[Value]) -> Option<Vec<Rule>> {
        values
            .iter()
            .map(|v| {
                let pattern = v["pattern"].as_str()?;
                let flags = v["flags"].as_u64()? as u32;
                Some(Rule {
                    id: v["id"].as_str()?.into(),
                    block: v["action"].as_str()? == "block",
                    utf: brigade_guard::compile(pattern, flags, true).ok()?,
                    // Run byte-class matching as well on exact UTF-8 bytes. A hit
                    // in either mode counts, including the S18a divergent rules.
                    ascii: brigade_guard::compile(pattern, flags, false).ok()?,
                })
            })
            .collect()
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
        fn inspect(&self, text: &str) -> Option<Scan> {
            let mut blocks = Vec::new();
            let mut warnings = Vec::new();
            for (index, c) in text.chars().enumerate() {
                let rule = match c as u32 {
                    0x202A..=0x202E | 0x2066..=0x2069 => Some(("bidi-control", true)),
                    0x200B | 0x2060..=0x2064 | 0xE0000..=0xE007F => {
                        Some(("invisible-character", true))
                    }
                    0xFEFF if index != 0 => Some(("invisible-character", true)),
                    0x200C | 0x200D => Some(("zero-width-joiner", false)),
                    _ => None,
                };
                if let Some((id, block)) = rule {
                    let ids = if block { &mut blocks } else { &mut warnings };
                    if !ids.iter().any(|s| s == id) {
                        ids.push(id.to_string());
                    }
                }
            }
            let normalized = text.split_whitespace().collect::<Vec<_>>().join(" ");
            for rule in self.injections.iter().chain(&self.rules) {
                let matched = rule.utf.is_match(text.as_bytes()).ok()?
                    | rule.ascii.is_match(text.as_bytes()).ok()?;
                let cross_line = rule.id == "classic-injection"
                    && rule.utf.is_match(normalized.as_bytes()).ok()?;
                if matched || cross_line {
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
            Some(if !blocks.is_empty() {
                Scan::Blocked {
                    ruleset: self.version.clone(),
                    rules: blocks,
                }
            } else if !warnings.is_empty() {
                Scan::Flagged {
                    ruleset: self.version.clone(),
                    rules: warnings,
                }
            } else {
                Scan::Clean {
                    ruleset: self.version.clone(),
                }
            })
        }
    }
    impl Scanner for Guard {
        fn scan(&self, text: &str) -> Scan {
            self.inspect(text).unwrap_or(Scan::Unavailable)
        }
        fn describe(&self) -> (bool, Option<String>) {
            (true, Some(self.version.clone()))
        }
    }
}
