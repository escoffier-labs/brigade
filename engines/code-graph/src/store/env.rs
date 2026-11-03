//! Renamed environment variables (docs/phase-engine-rename.md, "Resolution rule").
//!
//! The canonical variable is read first. The legacy variable still works for the rename window,
//! v0.31 and v0.32. Both set to different values is refused, which matches Brigade's
//! `component_bins` and `engine_paths` helpers. An empty value counts as unset.
//!
//! Reading a legacy variable prints nothing. Its stderr deprecation line lands in R5 together
//! with the legacy entry point notice: Brigade's callers and Windows PowerShell 5.1 callers that
//! run with `$ErrorActionPreference = 'Stop'` treat any native stderr as an error, so stderr stays
//! unchanged until Brigade no longer depends on legacy names. `brigade doctor` reports the
//! legacy variable instead.

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

/// Read a renamed variable from the process environment. Never prints (see the module notes).
pub fn read_renamed(canonical: &'static str, legacy: &'static str) -> Result<Option<OsString>> {
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
