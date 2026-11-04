//! Content scan hook. S18 plugs the guard's rule set in here. Until then the
//! hook reports unavailable: writes store scan state `pending` and every trust
//! raise is refused with `memory_scan_unavailable` (D24).
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
/// Runs outside every lock, before a command reaches the memory writer.
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
