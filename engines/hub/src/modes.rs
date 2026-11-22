//! Fail closed until a family's native handler and shadow read audit land.
use std::collections::HashMap;
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Mode {
    Native,
    Shadow,
    Proxy,
}
#[derive(Clone, Default)]
pub struct Modes(HashMap<String, Mode>);
impl Modes {
    pub fn parse(values: &[String]) -> Result<Self, String> {
        let mut modes = Self::default();
        for value in values {
            let (family, mode) = value
                .split_once('=')
                .ok_or("route mode must be family=native|shadow|proxy")?;
            let mode = match mode {
                "native" => Mode::Native,
                "shadow" => Mode::Shadow,
                "proxy" => Mode::Proxy,
                _ => return Err("unknown route mode".into()),
            };
            let native = matches!(
                family,
                "health"
                    | "metrics"
                    | "registration"
                    | "heartbeat"
                    | "claims"
                    | "messages"
                    | "memory"
            );
            // Only health has a shadow comparator and housekeeping audit so far.
            let shadow = family == "health";
            if !matches!(
                family,
                "health"
                    | "metrics"
                    | "registration"
                    | "heartbeat"
                    | "claims"
                    | "messages"
                    | "memory"
                    | "cold"
                    | "browser"
            ) {
                return Err("unknown route family".into());
            }
            if (mode == Mode::Native && !native) || (mode == Mode::Shadow && !shadow) {
                return Err(format!(
                    "{family} has no native handler or shadow read audit yet"
                ));
            }
            if family == "metrics" && mode != Mode::Native {
                return Err("metrics requires native mode".into());
            }
            // Python has no memory routes. `memory=proxy` configures memory
            // with no handler, which answers 503 memory-unavailable (D21).
            modes.0.insert(family.into(), mode);
        }
        Ok(modes)
    }
    /// The mode named on the command line, if any. Memory is configured only
    /// when its mode is named; otherwise `/memory` is an unknown path (404).
    pub fn configured(&self, family: &str) -> Option<Mode> {
        self.0.get(family).copied()
    }
    pub fn get(&self, family: &str) -> Mode {
        self.0
            .get(family)
            .copied()
            .unwrap_or(if matches!(family, "health" | "metrics") {
                Mode::Native
            } else {
                Mode::Proxy
            })
    }
}
pub fn family(method: &str, path: &str, action: &str) -> &'static str {
    match (method, path) {
        ("GET", "/health" | "/ready") => "health",
        ("GET", "/metrics") => "metrics",
        ("POST", "/nodes" | "/message-principals") | ("GET", "/nodes") => "registration",
        (_, "/events" | "/status" | "/sessions") => "heartbeat",
        (_, "/claims" | "/owners") => "claims",
        ("POST", "/messages") if !action.starts_with("events_") => "messages",
        (_, "/memory") => "memory",
        _ if path == "/"
            || [
                "/deck",
                "/view/",
                "/dashboard/",
                "/static/",
                "/assets/",
                "/enroll",
            ]
            .iter()
            .any(|p| path.starts_with(p)) =>
        {
            "browser"
        }
        _ => "cold",
    }
}

pub fn method_label(method: &str) -> &'static str {
    match method {
        "GET" => "GET",
        "POST" => "POST",
        "PUT" => "PUT",
        "PATCH" => "PATCH",
        "DELETE" => "DELETE",
        "HEAD" => "HEAD",
        "OPTIONS" => "OPTIONS",
        _ => "other",
    }
}

/// Request log labels for the native dispatch and Python's fleet_hub_http
/// route table. Match the path separately from the method so refusals still
/// identify the endpoint. Only fixed strings may leave this function.
pub fn route_label(path: &str) -> &'static str {
    match path {
        "/" => "/",
        "/actions" => "/actions",
        "/apple-touch-icon.png" => "/apple-touch-icon.png",
        "/audit" => "/audit",
        "/ci" => "/ci",
        "/ci/refresh" => "/ci/refresh",
        "/ci/work" => "/ci/work",
        "/claims" => "/claims",
        "/cloud" => "/cloud",
        "/dashboard/enrollment" => "/dashboard/enrollment",
        "/dashboard/sessions" => "/dashboard/sessions",
        "/deck" => "/deck",
        "/deck/actions/compose" => "/deck/actions/compose",
        "/deck/actions/enable" => "/deck/actions/enable",
        "/deck/actions/submit" => "/deck/actions/submit",
        "/deck/activity" => "/deck/activity",
        "/deck/alerts" => "/deck/alerts",
        "/deck/capacity" => "/deck/capacity",
        "/deck/capacity/cloud" => "/deck/capacity/cloud",
        "/deck/capacity/github" => "/deck/capacity/github",
        "/deck/ci" => "/deck/ci",
        "/deck/mcp" => "/deck/mcp",
        "/deck/owners" => "/deck/owners",
        "/deck/policy" => "/deck/policy",
        "/deck/pr-rules" => "/deck/pr-rules",
        "/deck/projects" => "/deck/projects",
        "/deck/pulls" => "/deck/pulls",
        "/deck/reporting" => "/deck/reporting",
        "/deck/repos" => "/deck/repos",
        "/deck/roster" => "/deck/roster",
        "/deck/skills" => "/deck/skills",
        "/deck/skills/plan" => "/deck/skills/plan",
        "/deck/stats" => "/deck/stats",
        "/deck/work" => "/deck/work",
        "/enroll" => "/enroll",
        "/events" => "/events",
        "/favicon-32x32.png" => "/favicon-32x32.png",
        "/favicon.ico" => "/favicon.ico",
        "/favicon.svg" => "/favicon.svg",
        "/findings" => "/findings",
        "/grokbot" => "/grokbot",
        "/health" => "/health",
        "/icon-192.png" => "/icon-192.png",
        "/icon-512.png" => "/icon-512.png",
        "/mcp/report" => "/mcp/report",
        "/memory" => "/memory",
        "/mesh/identity" => "/mesh/identity",
        "/mesh/statements" => "/mesh/statements",
        "/message-principals" => "/message-principals",
        "/messages" => "/messages",
        "/metrics" => "/metrics",
        "/models" => "/models",
        "/models/usage" => "/models/usage",
        "/nodes" => "/nodes",
        "/owners" => "/owners",
        "/policy" => "/policy",
        "/policy/inventory" => "/policy/inventory",
        "/policy/status" => "/policy/status",
        "/preference" => "/preference",
        "/projects/board" => "/projects/board",
        "/ready" => "/ready",
        "/sessions" => "/sessions",
        "/site.webmanifest" => "/site.webmanifest",
        "/skills/report" => "/skills/report",
        "/stats" => "/stats",
        "/status" => "/status",
        "/view/machines" => "/view/machines",
        "/view/repos" => "/view/repos",
        "/work" => "/work",
        _ => {
            if let Some(tail) = path.strip_prefix("/work/") {
                work_route_label(tail)
            } else if let Some(id) = path.strip_prefix("/deck/actions/") {
                if id.starts_with("aa_") && !id.contains('/') {
                    "/deck/actions/:id"
                } else {
                    "other"
                }
            } else if let Some(group) = path.strip_prefix("/deck/observed/") {
                if !group.is_empty() && !group.contains('/') {
                    "/deck/observed/:group"
                } else {
                    "other"
                }
            } else {
                "other"
            }
        }
    }
}

/// Worklore ignores empty segments, then dispatches on the resource and tail.
fn work_route_label(tail: &str) -> &'static str {
    let mut parts = tail.split('/').filter(|part| !part.is_empty());
    match (
        parts.next(),
        parts.next(),
        parts.next(),
        parts.next(),
        parts.next(),
    ) {
        (Some("campaigns"), None, None, None, None) => "/work/campaigns",
        (Some("campaigns"), Some(_), None, None, None) => "/work/campaigns/:key",
        (Some("events"), None, None, None, None) => "/work/events",
        (Some("imports"), None, None, None, None) => "/work/imports",
        (Some("items"), None, None, None, None) => "/work/items",
        (Some("items"), Some(_), None, None, None) => "/work/items/:id",
        (Some("items"), Some(_), Some("attempts"), None, None) => "/work/items/:id/attempts",
        (Some("items"), Some(_), Some("completions"), None, None) => "/work/items/:id/completions",
        (Some("items"), Some(_), Some("events"), None, None) => "/work/items/:id/events",
        (Some("items"), Some(_), Some("execution"), None, None) => "/work/items/:id/execution",
        (Some("items"), Some(_), Some("links"), None, None) => "/work/items/:id/links",
        (Some("items"), Some(_), Some("links"), Some(_), None) => "/work/items/:id/links/:link",
        (Some("items"), Some(_), Some("ownership"), None, None) => "/work/items/:id/ownership",
        (Some("items"), Some(_), Some("prepared-handoff"), None, None) => {
            "/work/items/:id/prepared-handoff"
        }
        (Some("items"), Some(_), Some("transitions"), None, None) => "/work/items/:id/transitions",
        (Some("queue"), Some("burn"), None, None, None) => "/work/queue/burn",
        _ => "other",
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn registration_accepts_native_and_proxy_but_not_shadow() {
        let modes = Modes::parse(&["registration=native".into()]).unwrap();
        assert_eq!(modes.get("registration"), Mode::Native);
        assert_eq!(Modes::default().get("registration"), Mode::Proxy);
        assert!(Modes::parse(&["registration=shadow".into()]).is_err());
        assert!(Modes::parse(&["heartbeat=native".into()]).is_ok());
        assert!(Modes::parse(&["heartbeat=shadow".into()]).is_err());
    }
    #[test]
    fn messages_accept_native_and_proxy_but_not_shadow() {
        let modes = Modes::parse(&["messages=native".into()]).unwrap();
        assert_eq!(modes.get("messages"), Mode::Native);
        assert_eq!(Modes::default().get("messages"), Mode::Proxy);
        assert!(Modes::parse(&["messages=shadow".into()]).is_err());
    }
    #[test]
    fn claims_accept_native_and_proxy_but_not_shadow() {
        let modes = Modes::parse(&["claims=native".into()]).unwrap();
        assert_eq!(modes.get("claims"), Mode::Native);
        assert_eq!(Modes::default().get("claims"), Mode::Proxy);
        assert!(Modes::parse(&["claims=shadow".into()]).is_err());
    }
    use super::*;
    #[test]
    fn memory_is_configured_only_when_named_and_never_shadowed() {
        assert_eq!(Modes::default().configured("memory"), None);
        let native = Modes::parse(&["memory=native".into()]).unwrap();
        assert_eq!(native.configured("memory"), Some(Mode::Native));
        let forced = Modes::parse(&["memory=proxy".into()]).unwrap();
        assert_eq!(forced.configured("memory"), Some(Mode::Proxy));
        assert!(Modes::parse(&["memory=shadow".into()]).is_err());
        assert_eq!(family("POST", "/memory", ""), "memory");
        assert_eq!(family("GET", "/memory", ""), "memory");
        assert_eq!(family("POST", "/memoryx", ""), "cold");
    }
    #[test]
    fn modes_reject_unimplemented_handlers_and_keep_last_override() {
        assert!(Modes::parse(&["claims=shadow".into()]).is_err());
        assert!(Modes::parse(&["unknown=proxy".into()]).is_err());
        assert!(Modes::parse(&["metrics=proxy".into()]).is_err());
        assert_eq!(
            Modes::parse(&["health=proxy".into(), "health=shadow".into()])
                .unwrap()
                .get("health"),
            Mode::Shadow
        );
        assert_eq!(family("POST", "/messages", "events_list"), "cold");
        assert_eq!(family("POST", "/messages", "question_create"), "messages");
    }
}
