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
            let native = matches!(family, "health" | "metrics" | "registration" | "claims");
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
            modes.0.insert(family.into(), mode);
        }
        Ok(modes)
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
#[cfg(test)]
mod tests {
    #[test]
    fn registration_accepts_native_and_proxy_but_not_shadow() {
        let modes = Modes::parse(&["registration=native".into()]).unwrap();
        assert_eq!(modes.get("registration"), Mode::Native);
        assert_eq!(Modes::default().get("registration"), Mode::Proxy);
        assert!(Modes::parse(&["registration=shadow".into()]).is_err());
        assert!(Modes::parse(&["heartbeat=native".into()]).is_err());
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
