//! Renamed environment variables (docs/phase-engine-rename.md, "Resolution rule").
//!
//! The canonical variable is read first. The legacy variable still works for the rename window,
//! v0.31 and v0.32. Both set to different values is refused, which matches Brigade's
//! `component_bins` and `engine_paths` helpers. An empty value counts as unset.
//!
//! Reading a legacy variable prints one stderr deprecation notice per process.

use std::ffi::OsString;

use anyhow::{Result, bail};

/// A renamed variable's value, and the legacy name when only that one was set.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RenamedValue {
    pub value: OsString,
    pub legacy: Option<&'static str>,
}

/// Read `canonical`, then `legacy`, through an injected lookup. Never prints.
pub fn read_renamed_with(
    canonical: &'static str,
    legacy: &'static str,
    lookup: impl Fn(&str) -> Option<OsString>,
) -> Result<Option<RenamedValue>> {
    let current = lookup(canonical).filter(|value| !value.is_empty());
    let old = lookup(legacy).filter(|value| !value.is_empty());
    match (current, old) {
        (Some(current), Some(old)) if current != old => {
            bail!("conflicting values for {canonical} and {legacy}; unset {legacy}")
        }
        (Some(value), _) => Ok(Some(RenamedValue {
            value,
            legacy: None,
        })),
        (None, Some(value)) => Ok(Some(RenamedValue {
            value,
            legacy: Some(legacy),
        })),
        (None, None) => Ok(None),
    }
}

/// Read a renamed variable and report legacy use once per process.
pub fn read_renamed(canonical: &'static str, legacy: &'static str) -> Result<Option<OsString>> {
    if std::env::var_os(legacy).is_some_and(|v| !v.is_empty()) {
        use std::sync::{Mutex, OnceLock};
        static SEEN: OnceLock<Mutex<std::collections::HashSet<&'static str>>> = OnceLock::new();
        if SEEN
            .get_or_init(Default::default)
            .lock()
            .unwrap()
            .insert(legacy)
        {
            eprintln!("{legacy} is deprecated; use {canonical}; removed in v0.33.0");
        }
    }
    let found = read_renamed_with(canonical, legacy, |name| std::env::var_os(name))?;
    Ok(found.map(|found| found.value))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    fn lookup(pairs: &[(&str, &str)]) -> impl Fn(&str) -> Option<OsString> {
        let map: HashMap<String, OsString> = pairs
            .iter()
            .map(|(key, value)| ((*key).to_string(), OsString::from(value)))
            .collect();
        move |name| map.get(name).cloned()
    }

    #[test]
    fn canonical_wins_and_legacy_is_reported() {
        let found = read_renamed_with("NEW_VAR", "OLD_VAR", lookup(&[("OLD_VAR", "a")])).unwrap();
        assert_eq!(
            found,
            Some(RenamedValue {
                value: "a".into(),
                legacy: Some("OLD_VAR")
            })
        );
        let found = read_renamed_with(
            "NEW_VAR",
            "OLD_VAR",
            lookup(&[("NEW_VAR", "a"), ("OLD_VAR", "a")]),
        )
        .unwrap();
        assert_eq!(found.unwrap().legacy, None);
    }

    #[test]
    fn conflicting_values_are_refused_and_empty_is_unset() {
        let err = read_renamed_with(
            "NEW_VAR",
            "OLD_VAR",
            lookup(&[("NEW_VAR", "a"), ("OLD_VAR", "b")]),
        )
        .unwrap_err();
        assert!(err.to_string().contains("conflicting values"));
        let found = read_renamed_with(
            "NEW_VAR",
            "OLD_VAR",
            lookup(&[("NEW_VAR", ""), ("OLD_VAR", "")]),
        )
        .unwrap();
        assert_eq!(found, None);
    }
}
