//! Versioned guard scanning for memory writes and prompt loads (D24, S18).
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Scan {
    /// No engine or rule set. Never treated as clean.
    Unavailable,
    /// Input-specific engine failure. Refuse content writes, never store pending.
    /// A proposal decision records `held` and leaves every card unchanged.
    Failed,
    Clean {
        ruleset: String,
    },
    /// Stored as `private`: the operator's own infrastructure and contact
    /// details. Accepted, searchable, raised by import and loaded into local
    /// prompts, whose harness providers are inside the boundary. Refused on
    /// every path to a connector audience, with [`PRIVATE_CODE`] when nothing
    /// worse was found (S18 amendment).
    Private {
        ruleset: String,
        rules: Vec<String>,
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
            Scan::Private { .. } => "private",
            Scan::Flagged { .. } => "flagged",
            Scan::Blocked { .. } => "blocked",
        }
    }
    pub fn ruleset(&self) -> &str {
        match self {
            Scan::Unavailable => "unavailable",
            Scan::Failed => "failed",
            Scan::Clean { ruleset }
            | Scan::Private { ruleset, .. }
            | Scan::Flagged { ruleset, .. }
            | Scan::Blocked { ruleset, .. } => ruleset,
        }
    }
    pub fn rules(&self) -> &[String] {
        match self {
            Scan::Private { rules, .. }
            | Scan::Flagged { rules, .. }
            | Scan::Blocked { rules, .. } => rules,
            _ => &[],
        }
    }
    /// Only a clean or private scan under a known rule set may raise trust.
    /// Private text stays on the operator's machines, which hold the store.
    pub fn permits_raise(&self) -> bool {
        matches!(self, Scan::Clean { .. } | Scan::Private { .. })
    }
    /// Why this text must not leave the operator's machines, as
    /// `(code, rule id)`, or `None` when it may. Every egress path (connector
    /// relays, pushes, a non-loopback embedder) refuses on `Some`.
    pub fn egress_refusal(&self) -> Option<(&'static str, &str)> {
        match self {
            Scan::Clean { .. } => None,
            Scan::Unavailable => Some(("memory_scan_unavailable", "memory_scan_unavailable")),
            Scan::Failed => Some(("memory_scan_failed", "memory_scan_failed")),
            Scan::Private { rules, .. } => Some((
                PRIVATE_CODE,
                rules
                    .first()
                    .map_or("memory_content_private", String::as_str),
            )),
            Scan::Flagged { rules, .. } | Scan::Blocked { rules, .. } => Some((
                "memory_content_blocked",
                rules.first().map_or("memory_scan_flagged", String::as_str),
            )),
        }
    }
}
/// The refusal code when only the private class keeps text on the
/// operator's machines. Never `memory_content_blocked`.
pub const PRIVATE_CODE: &str = "memory_content_private";
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
/// `egress` is true when the hit goes to a connector audience (a connector
/// push). Private text then gets a placeholder with [`PRIVATE_CODE`]. A local
/// prompt load keeps it, minus U+034F and U+200C.
pub fn prompt_hit(
    scanner: &dyn Scanner,
    card_id: &str,
    hit: serde_json::Value,
    fields: &[&str],
    egress: bool,
) -> serde_json::Value {
    let joined = fields
        .iter()
        .map(|s| s.strip_prefix('\u{feff}').unwrap_or(s))
        .collect::<Vec<_>>()
        .join("\n");
    // A worse finding in any field wins over a private one in an earlier field.
    let mut private = None;
    for text in fields
        .iter()
        .copied()
        .chain(std::iter::once(joined.as_str()))
    {
        let result = scanner.scan(text);
        match result.egress_refusal() {
            None => {}
            Some((PRIVATE_CODE, rule)) => {
                if egress && private.is_none() {
                    private = Some(rule.to_string());
                }
            }
            Some((_, rule)) => return serde_json::json!({"card_id": card_id, "blocked": rule}),
        }
    }
    match private {
        Some(rule) => {
            serde_json::json!({"card_id": card_id, "blocked": rule, "code": PRIVATE_CODE})
        }
        None => {
            let mut hit = hit;
            strip_render(&mut hit);
            hit
        }
    }
}

/// U+034F and U+200C scan private, so their text loads into local prompts.
/// Stored bytes and content hashes keep them. The rendered hit drops them, so
/// a mix of the two cannot carry hidden bits into a model's prompt.
pub const RENDER_STRIPPED: [char; 2] = ['\u{34f}', '\u{200c}'];

fn strip_render(value: &mut serde_json::Value) {
    match value {
        serde_json::Value::String(s) if s.contains(&RENDER_STRIPPED[..]) => {
            s.retain(|c| !RENDER_STRIPPED.contains(&c));
        }
        serde_json::Value::Array(items) => items.iter_mut().for_each(strip_render),
        serde_json::Value::Object(map) => map.values_mut().for_each(strip_render),
        _ => {}
    }
}

#[cfg(feature = "memory-scan")]
mod native {
    use super::*;
    use brigade_guard::Regex;
    use serde_json::Value;
    use unicode_normalization::UnicodeNormalization;

    #[derive(Clone, Copy, PartialEq, Eq)]
    enum Class {
        Block,
        Warn,
        Private,
    }
    struct Rule {
        id: String,
        class: Class,
        utf: Regex,
        ascii: Regex,
    }
    pub struct Guard {
        version: String,
        /// Code points Unicode 14.0.0 leaves unassigned, from the bundle.
        unassigned: Vec<(u32, u32)>,
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
                // An unknown action fails the whole bundle closed.
                let class = match v["action"].as_str()? {
                    "block" => Class::Block,
                    "warn" => Class::Warn,
                    "private" => Class::Private,
                    _ => return None,
                };
                Some(Rule {
                    id: v["id"].as_str()?.into(),
                    class,
                    utf: brigade_guard::compile(&pattern, flags, true).ok()?,
                    // Run byte-class matching as well on exact UTF-8 bytes. A hit
                    // in either mode counts, including the S18a divergent rules.
                    ascii: brigade_guard::compile(&pattern, flags, false).ok()?,
                })
            })
            .collect()
    }
    // Extended_Pictographic blocks that take U+FE0F, shared with Python.
    fn emoji_base(c: char) -> bool {
        matches!(c as u32, 0xA9 | 0xAE | 0x203C | 0x2049 | 0x2122 | 0x2139 |
            0x2190..=0x21FF | 0x2300..=0x23FF | 0x24C2 | 0x25A0..=0x25FF |
            0x2600..=0x27BF | 0x2934..=0x2935 | 0x2B00..=0x2BFF | 0x3030 | 0x303D |
            0x3297 | 0x3299 | 0x1F000..=0x1F2FF | 0x1F300..=0x1FAFF)
    }
    /// U+FE0E or U+FE0F right after an emoji base, or a keycap's U+FE0F.
    /// U+FE00 to U+FE0D pick no presentation and would hide data after
    /// every visible symbol, so they block.
    fn selector_exempt(previous: Option<char>, c: char, next: Option<char>) -> bool {
        (matches!(c, '\u{fe0e}' | '\u{fe0f}') && previous.is_some_and(emoji_base))
            || (c == '\u{fe0f}'
                && previous.is_some_and(|p| matches!(p, '#' | '*' | '0'..='9'))
                && next == Some('\u{20e3}'))
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
    fn unassigned_table(spec: &str) -> Option<Vec<(u32, u32)>> {
        let table: Vec<(u32, u32)> = spec
            .split(',')
            .map(|item| {
                let (lo, hi) = item.split_once('-')?;
                Some((
                    u32::from_str_radix(lo, 16).ok()?,
                    u32::from_str_radix(hi, 16).ok()?,
                ))
            })
            .collect::<Option<_>>()?;
        // Sorted and disjoint, or the binary search below would misjudge.
        table
            .iter()
            .all(|(lo, hi)| lo <= hi)
            .then_some(())
            .filter(|()| table.windows(2).all(|w| w[0].1 < w[1].0))?;
        Some(table)
    }
    /// NFKC over runs of code points Unicode 14.0.0 assigns. Stability keeps
    /// their NFKC the same in this crate's newer Unicode data and in every
    /// Python 3.11 or later. A code point the pin leaves unassigned passes
    /// through and ends the run, as an older runtime treats it.
    fn nfkc(unassigned: &[(u32, u32)], text: &str) -> String {
        if text.is_ascii() {
            return text.to_string();
        }
        let assigned = |c: char| {
            let at = unassigned.partition_point(|&(_, hi)| hi < c as u32);
            unassigned.get(at).is_none_or(|&(lo, _)| lo > c as u32)
        };
        let mut out = String::with_capacity(text.len());
        let mut run = 0;
        for (index, c) in text.char_indices() {
            if !assigned(c) {
                out.extend(text[run..index].nfkc());
                out.push(c);
                run = index + c.len_utf8();
            }
        }
        out.extend(text[run..].nfkc());
        out
    }
    /// Boundaries kept as newlines and ignorable code points removed.
    fn base_lines(text: &str) -> String {
        text.replace("\r\n", "\n")
            .chars()
            .filter(|c| !ignorable(*c))
            .map(|c| match c as u32 {
                0xA..=0xD | 0x1C..=0x1E | 0x85 | 0x2028..=0x2029 => '\n',
                _ => c,
            })
            .collect()
    }
    /// The v6 fold. It stays a scan copy beside NFKC: composition can absorb
    /// a trailing ASCII letter and a compatibility symbol can become word
    /// characters, which breaks `\b` and `(?!\w)` around a match. Scanning
    /// both copies keeps every verdict at least as severe as v6.
    fn fold_fullwidth(base: &str) -> String {
        base.chars()
            .map(|c| match c as u32 {
                0xFF01..=0xFF5E => char::from_u32(c as u32 - 0xFEE0).unwrap(),
                0x3000 => ' ',
                _ => c,
            })
            .collect()
    }
    /// Words of `text` split on the shared whitespace set, joined by `sep`.
    fn join_words(text: &str, sep: char) -> String {
        let mut out = String::with_capacity(text.len());
        for word in text.split(whitespace).filter(|s| !s.is_empty()) {
            if !out.is_empty() {
                out.push(sep);
            }
            out.push_str(word);
        }
        out
    }
    /// (lines, injection lines, normalized) for one line copy.
    fn derive_copies(lines: String) -> (String, String, String) {
        let normalized = join_words(&lines, ' ');
        let mut injection_lines = String::with_capacity(lines.len());
        for (index, line) in lines.split('\n').enumerate() {
            if index > 0 {
                injection_lines.push('\n');
            }
            injection_lines.push_str(&join_words(line, ' '));
        }
        (lines, injection_lines, normalized)
    }
    /// The NFKC lines copy alone, as the Python fixture records it.
    #[cfg(test)]
    fn nfkc_lines(unassigned: &[(u32, u32)], text: &str) -> String {
        nfkc(unassigned, &base_lines(text))
    }
    /// The v6 fold copies and the NFKC copies. Equal line copies are scanned
    /// once, so ASCII text pays nothing extra.
    fn scan_copies(unassigned: &[(u32, u32)], text: &str) -> Vec<(String, String, String)> {
        let base = base_lines(text);
        let fold = fold_fullwidth(&base);
        let normalized = nfkc(unassigned, &base);
        let mut copies = Vec::with_capacity(2);
        if normalized != fold {
            copies.push(derive_copies(normalized));
        }
        copies.insert(0, derive_copies(fold));
        copies
    }
    impl Guard {
        pub fn new() -> Option<Self> {
            Self::from_bundle(include_str!(
                "../../../../src/brigade/guard/memory-rules.json"
            ))
        }
        /// Refuses a bundle whose NFKC table is not the Unicode 14.0.0 one
        /// this scanner's parity tests cover.
        fn from_bundle(source: &str) -> Option<Self> {
            if !brigade_guard::pinned() {
                return None;
            }
            let bundle: Value = serde_json::from_str(source).ok()?;
            (bundle["nfkc"]["unicode"].as_str()? == "14.0.0").then_some(())?;
            Some(Self {
                version: format!(
                    "{}:{}",
                    bundle["version"].as_str()?,
                    super::super::ids::sha256_hex(source.as_bytes())
                ),
                unassigned: unassigned_table(bundle["nfkc"]["unassigned"].as_str()?)?,
                injections: rules(bundle["injections"].as_array()?)?,
                rules: rules(bundle["rules"].as_array()?)?,
            })
        }
        fn inspect(&self, text: &str) -> Scan {
            let mut blocks = Vec::new();
            let mut warnings = Vec::new();
            let mut privates = Vec::new();
            let mut previous = None;
            let mut chars = text.chars().enumerate().peekable();
            while let Some((index, c)) = chars.next() {
                let next = chars.peek().map(|(_, n)| *n);
                let rule = match c as u32 {
                    0x61C | 0x200E | 0x200F | 0x202A..=0x202E | 0x2066..=0x2069 => {
                        Some(("bidi-control", Class::Block))
                    }
                    0xFEFF if index == 0 => None,
                    0x200D => Some(("zero-width-joiner", Class::Warn)),
                    // Pasted-text joiners. Scan copies drop them, stored bytes keep them.
                    0x200C => Some(("zero-width-non-joiner", Class::Private)),
                    0x34F => Some(("combining-grapheme-joiner", Class::Private)),
                    0xFE00..=0xFE0F if selector_exempt(previous, c, next) => None,
                    0xFFF9..=0xFFFB => Some(("invisible-character", Class::Block)),
                    _ if ignorable(c) => Some(("invisible-character", Class::Block)),
                    _ => None,
                };
                previous = Some(c);
                if let Some((id, class)) = rule {
                    let ids = match class {
                        Class::Block => &mut blocks,
                        Class::Warn => &mut warnings,
                        Class::Private => &mut privates,
                    };
                    if !ids.iter().any(|s| s == id) {
                        ids.push(id.to_string());
                    }
                }
            }
            let copies = scan_copies(&self.unassigned, text);
            let mut failed = false;
            for (rules, injection) in [(&self.injections, true), (&self.rules, false)] {
                for rule in rules {
                    let mut matched = false;
                    for copy in std::iter::once(text).chain(copies.iter().flat_map(
                        |(lines, injection_lines, normalized)| {
                            std::iter::once(lines.as_str())
                                .chain(injection.then_some(injection_lines.as_str()))
                                .chain(injection.then_some(normalized.as_str()))
                        },
                    )) {
                        for regex in [&rule.utf, &rule.ascii] {
                            match regex.is_match(copy.as_bytes()) {
                                Ok(hit) => matched |= hit,
                                Err(_) => failed = true,
                            }
                        }
                    }
                    if matched {
                        let ids = match rule.class {
                            Class::Block => &mut blocks,
                            Class::Warn => &mut warnings,
                            Class::Private => &mut privates,
                        };
                        if !ids.contains(&rule.id) {
                            ids.push(rule.id.clone());
                        }
                    }
                }
            }
            if !blocks.is_empty() {
                blocks.extend(warnings);
                blocks.extend(privates);
                Scan::Blocked {
                    ruleset: self.version.clone(),
                    rules: blocks,
                }
            } else if failed {
                Scan::Failed
            } else if !warnings.is_empty() {
                warnings.extend(privates);
                Scan::Flagged {
                    ruleset: self.version.clone(),
                    rules: warnings,
                }
            } else if !privates.is_empty() {
                Scan::Private {
                    ruleset: self.version.clone(),
                    rules: privates,
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

        /// Source pins cover every class and fold, and also catch edits to
        /// this scanner when only the Rust CI jobs run.
        #[test]
        fn scanner_sources_match_the_bundle() {
            let bundle: Value = serde_json::from_str(include_str!(
                "../../../../src/brigade/guard/memory-rules.json"
            ))
            .unwrap();
            for (name, source) in [
                (
                    "python",
                    include_str!("../../../../src/brigade/memory_content_scan.py"),
                ),
                ("rust", include_str!("scan.rs")),
            ] {
                assert_eq!(
                    super::super::super::ids::sha256_hex(source.replace("\r\n", "\n").as_bytes()),
                    bundle["scanner_sources"][name].as_str().unwrap(),
                    "{name} scanner changed: run python scripts/memory_scan_bundle.py"
                );
            }
        }

        fn nfkc_fixture() -> Value {
            serde_json::from_str(include_str!(
                "../../../../tests/fixtures/memory-scan-nfkc.json"
            ))
            .unwrap()
        }

        /// The crate's Unicode data is part of the scan verdict, so a bump
        /// is a reviewed change: rerun the parity tests and bump the bundle.
        #[test]
        fn nfkc_crate_unicode_version_is_the_recorded_one() {
            assert_eq!(unicode_normalization::UNICODE_VERSION, (18, 0, 0));
        }

        fn hex(bytes: &[u8]) -> String {
            use std::fmt::Write;
            bytes.iter().fold(String::new(), |mut out, b| {
                write!(out, "{b:02x}").unwrap();
                out
            })
        }

        #[test]
        fn nfkc_matches_python_on_every_code_point() {
            use sha2::{Digest, Sha256};
            let guard = Guard::new().unwrap();
            let fixture = nfkc_fixture();
            assert_eq!(fixture["unicode"], "14.0.0");
            let mut digest = Sha256::new();
            let mut changed = 0;
            for code in 0..=0x10FFFFu32 {
                let Some(c) = char::from_u32(code) else {
                    continue;
                };
                let text = c.to_string();
                let out = nfkc(&guard.unassigned, &text);
                if out != text {
                    digest.update(format!("{code:X}:{}\n", hex(out.as_bytes())));
                    changed += 1;
                }
            }
            assert_eq!(changed, fixture["changed_code_points"].as_u64().unwrap());
            assert_eq!(
                hex(&digest.finalize()),
                fixture["changed_sha256"].as_str().unwrap()
            );
        }

        #[test]
        fn scan_copy_matches_python_for_compatibility_sequences() {
            let guard = Guard::new().unwrap();
            for pair in nfkc_fixture()["lines"].as_array().unwrap() {
                let input = pair[0].as_str().unwrap();
                let lines = nfkc_lines(&guard.unassigned, input);
                assert_eq!(lines, pair[1].as_str().unwrap(), "{input:?}");
            }
        }

        #[test]
        fn bundle_with_another_unicode_version_is_refused() {
            let source = include_str!("../../../../src/brigade/guard/memory-rules.json");
            assert!(Guard::from_bundle(source).is_some());
            for version in ["15.0.0", "14.0", ""] {
                let edited = source.replacen(
                    "\"unicode\": \"14.0.0\"",
                    &format!("\"unicode\": \"{version}\""),
                    1,
                );
                assert_ne!(edited, source, "bundle spelling of the pin changed");
                assert!(Guard::from_bundle(&edited).is_none(), "{version}");
            }
        }

        #[test]
        fn unassigned_table_must_be_sorted_and_disjoint() {
            assert!(unassigned_table("5-9,B-C").is_some());
            assert!(unassigned_table("9-5").is_none());
            assert!(unassigned_table("5-9,9-C").is_none());
            assert!(unassigned_table("B-C,5-9").is_none());
            assert!(unassigned_table("5-").is_none());
            assert!(unassigned_table("").is_none());
        }

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
                class: Class::Warn,
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
            let text = format!("{}!", "a".repeat(100));
            assert_eq!(
                guard.rules[0]
                    .utf
                    .is_match(text.as_bytes())
                    .unwrap_err()
                    .code(),
                -47
            );
            for run in 1..=2 {
                let start = std::time::Instant::now();
                assert_eq!(guard.scan(&text), Scan::Failed);
                println!(
                    "match-limit run={run} seconds={:.6}",
                    start.elapsed().as_secs_f64()
                );
            }
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
