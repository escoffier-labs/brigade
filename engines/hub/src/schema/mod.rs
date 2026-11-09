pub mod audit;
// Python owns DDL during rollback. This module only inspects structure.
use rusqlite::{Connection, OptionalExtension, types::ValueRef};
use serde_json::{Value, json};
use std::sync::atomic::{AtomicBool, Ordering};

pub const REQUIRED: &str = include_str!("../../schema/required.json");
/// Python's `FleetHubUnsupportedSchema.public_message`, sent with 503.
pub const NEWER_SCHEMA: &str = "fleet hub database schema is newer than supported";
/// The newest hub schema this build supports: required.json's `user_version`.
pub fn supported_version() -> i64 {
    static VERSION: std::sync::OnceLock<i64> = std::sync::OnceLock::new();
    *VERSION.get_or_init(|| {
        serde_json::from_str::<Value>(REQUIRED)
            .ok()
            .and_then(|v| v.get("user_version").and_then(Value::as_i64))
            .expect("required.json records user_version")
    })
}
/// Python re-checks the schema after BEGIN IMMEDIATE, because another hub can
/// migrate between admission and the write lock. Commands call this first.
pub fn newer_than_supported<T: crate::writer::ReadTx + ?Sized>(tx: &T) -> rusqlite::Result<bool> {
    let rows = tx.query("PRAGMA user_version", &[])?;
    let version = match rows.first().and_then(|row| row.first()) {
        Some(rusqlite::types::Value::Integer(v)) => *v,
        _ => 0,
    };
    Ok(version > supported_version())
}
/// One pass over the DDL. Quoted text ('..', "..", `..` with doubled-quote
/// escapes, and [..]) and comments (`--` through its newline, `/* .. */`) are
/// copied unchanged. Elsewhere a whitespace run becomes one space, dropped at
/// either end, after `(`, and before `,` or `)`. SQLite's ADD COLUMN splices
/// `, col` in front of the closing parenthesis, so a newline there becomes
/// ` ,`. Whitespace is SQLite's ASCII set. lockstep.py `normalize` must stay
/// identical (normalize-cases.json).
fn normalize(sql: &str) -> String {
    let s: Vec<char> = sql.chars().collect();
    let n = s.len();
    let find = |from: usize, needle: &[char]| {
        (from..n)
            .find(|&j| s[j..].starts_with(needle))
            .map_or(n, |j| j + needle.len())
    };
    let mut out: Vec<char> = Vec::with_capacity(n);
    let (mut i, mut pending) = (0, false);
    while i < n {
        let c = s[i];
        if matches!(c, ' ' | '\t' | '\n' | '\r' | '\x0b' | '\x0c') {
            pending = true;
            i += 1;
            continue;
        }
        if pending && !out.is_empty() && out.last() != Some(&'(') && !matches!(c, ',' | ')') {
            out.push(' ');
        }
        pending = false;
        let next = s.get(i + 1).copied();
        let end = match c {
            '\'' | '"' | '`' => {
                let mut j = i + 1;
                loop {
                    j = find(j, &[c]);
                    if j < n && s[j] == c {
                        j += 1;
                    } else {
                        break j;
                    }
                }
            }
            '[' => find(i + 1, &[']']),
            '-' if next == Some('-') => find(i + 2, &['\n']),
            '/' if next == Some('*') => find(i + 2, &['*', '/']),
            _ => i + 1,
        };
        out.extend_from_slice(&s[i..end]);
        i = end;
    }
    out.into_iter().collect()
}
fn quoted(name: &str) -> String {
    format!("\"{}\"", name.replace('"', "\"\""))
}
fn rows(c: &Connection, sql: &str) -> rusqlite::Result<Vec<Value>> {
    let mut statement = c.prepare(sql)?;
    let count = statement.column_count();
    statement
        .query_map([], |r| {
            (0..count)
                .map(|i| {
                    Ok(match r.get_ref(i)? {
                        ValueRef::Null => Value::Null,
                        ValueRef::Integer(v) => json!(v),
                        ValueRef::Real(v) => json!(v),
                        ValueRef::Text(v) => json!(String::from_utf8_lossy(v)),
                        ValueRef::Blob(_) => return Err(rusqlite::Error::InvalidQuery),
                    })
                })
                .collect::<rusqlite::Result<Vec<_>>>()
                .map(Value::Array)
        })?
        .collect()
}
fn checks(sql: &str) -> Vec<String> {
    let chars: Vec<char> = sql.chars().collect();
    let mut result = Vec::new();
    let mut i = 0;
    while i + 5 < chars.len() {
        if chars[i..i + 5]
            .iter()
            .collect::<String>()
            .eq_ignore_ascii_case("CHECK")
            && (i == 0 || !(chars[i - 1].is_alphanumeric() || chars[i - 1] == '_'))
        {
            let mut start = i + 5;
            while start < chars.len() && chars[start].is_whitespace() {
                start += 1;
            }
            if chars.get(start) == Some(&'(') {
                start += 1;
                let (mut end, mut depth, mut quote) = (start, 1, None);
                while end < chars.len() && depth > 0 {
                    let ch = chars[end];
                    if let Some(q) = quote {
                        if ch == q {
                            if chars.get(end + 1) == Some(&q) {
                                end += 1;
                            } else {
                                quote = None;
                            }
                        }
                    } else if matches!(ch, '\'' | '"' | '`') {
                        quote = Some(ch);
                    } else if ch == '(' {
                        depth += 1;
                    } else if ch == ')' {
                        depth -= 1;
                    }
                    end += 1;
                }
                result.push(normalize(&chars[start..end - 1].iter().collect::<String>()));
            }
        }
        i += 1;
    }
    result.sort();
    result
}
pub fn snapshot(c: &Connection) -> rusqlite::Result<Value> {
    let objects = rows(
        c,
        "SELECT type,name,sql FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%' ORDER BY name",
    )?;
    let mut tables = serde_json::Map::new();
    let mut triggers = serde_json::Map::new();
    for object in objects {
        let name = object[1].as_str().unwrap();
        let sql = object[2].as_str().unwrap_or("");
        match object[0].as_str().unwrap() {
            "trigger" => {
                triggers.insert(name.into(), json!(normalize(sql)));
            }
            "table" => {
                let mut columns: Vec<Value> =
                    rows(c, &format!("PRAGMA table_xinfo({})", quoted(name)))?
                        .into_iter()
                        .map(|r| Value::Array(r.as_array().unwrap()[1..].to_vec()))
                        .collect();
                columns.sort_by(|a, b| a[0].as_str().cmp(&b[0].as_str()));
                let mut indexes = serde_json::Map::new();
                for index in rows(c, &format!("PRAGMA index_list({})", quoted(name)))? {
                    let n = index[1].as_str().unwrap();
                    let ddl: Option<String> = c
                        .query_row("SELECT sql FROM sqlite_schema WHERE name=?", [n], |r| {
                            r.get::<_, Option<String>>(0)
                        })
                        .optional()?
                        .flatten();
                    indexes.insert(n.into(), json!({"unique":index[2], "origin":index[3], "partial":index[4],
                        "columns": rows(c, &format!("PRAGMA index_xinfo({})", quoted(n)))?.into_iter().map(|r| { let a=r.as_array().unwrap(); let mut v=vec![a[0].clone()]; v.extend_from_slice(&a[2..]); Value::Array(v) }).collect::<Vec<_>>(), "sql":normalize(&ddl.unwrap_or_default())}));
                }
                tables.insert(name.into(), json!({"columns":columns, "indexes":indexes,
                    "foreign_keys":rows(c, &format!("PRAGMA foreign_key_list({})", quoted(name)))?,
                    "checks":checks(sql), "autoincrement":sql.to_uppercase().contains("AUTOINCREMENT"),
                    "sql":normalize(sql), "column_order":rows(c, &format!("PRAGMA table_xinfo({})", quoted(name)))?.into_iter().map(|r| r[1].clone()).collect::<Vec<_>>()}));
            }
            _ => {}
        }
    }
    Ok(
        json!({"user_version": c.pragma_query_value(None,"user_version",|r| r.get::<_,i64>(0))?, "tables":tables,"triggers":triggers}),
    )
}
pub fn check(c: &Connection) -> Result<(), String> {
    let _snapshot = if c.is_autocommit() {
        Some(c.unchecked_transaction().map_err(|e| e.to_string())?)
    } else {
        None
    };
    let actual = snapshot(c).map_err(|e| e.to_string())?;
    accepts(&actual)
}
fn accepts(actual: &Value) -> Result<(), String> {
    accepts_against(
        actual,
        serde_json::from_str(REQUIRED).map_err(|e| e.to_string())?,
    )
}
fn accepts_against(actual: &Value, mut required: Value) -> Result<(), String> {
    let variants = required
        .as_object_mut()
        .unwrap()
        .remove("variants")
        .unwrap();
    // Optional tables carry their full required indexes and constraints. Only
    // absence is permitted; a present object must equal the reviewed structure.
    let optional = required
        .as_object_mut()
        .unwrap()
        .remove("optional")
        .unwrap();
    for kind in ["tables", "triggers"] {
        for (name, structure) in optional[kind].as_object().unwrap() {
            if actual[kind].get(name).is_some() {
                required[kind]
                    .as_object_mut()
                    .unwrap()
                    .insert(name.clone(), structure.clone());
            }
        }
    }
    for (name, accepted) in variants.as_object().unwrap() {
        if let Some(table) = actual["tables"].get(name) {
            if accepted.as_array().unwrap().contains(table) {
                required["tables"]
                    .as_object_mut()
                    .unwrap()
                    .insert(name.clone(), table.clone());
            }
        }
    }
    if *actual == required {
        Ok(())
    } else {
        Err("schema-changed: Python schema differs from required.json".into())
    }
}
/// Shared gate for future writers and readiness. Revalidate on upstream restart too.
#[derive(Default)]
pub struct SchemaGate(AtomicBool);
impl SchemaGate {
    pub fn ready(&self) -> bool {
        self.0.load(Ordering::Acquire)
    }
    pub fn recheck(&self, c: &Connection) -> Result<(), String> {
        self.0.store(false, Ordering::Release);
        check(c)?;
        c.flush_prepared_statement_cache();
        self.0.store(true, Ordering::Release);
        Ok(())
    }
    pub fn on_error(&self, c: &Connection, error: &rusqlite::Error) -> Result<(), String> {
        if matches!(error, rusqlite::Error::SqliteFailure(e, _) if e.code == rusqlite::ErrorCode::SchemaChanged)
        {
            self.recheck(c)?;
        }
        Ok(())
    }
}
/// Keep this on the writer connection. Its own commits do not change data_version.
pub struct DataVersion(i64);
impl DataVersion {
    pub fn new(c: &Connection) -> rusqlite::Result<Self> {
        Ok(Self(
            c.pragma_query_value(None, "data_version", |r| r.get(0))?,
        ))
    }
    pub fn changed(&mut self, c: &Connection) -> rusqlite::Result<bool> {
        let next = Self::new(c)?.0;
        let changed = self.0 != next;
        self.0 = next;
        Ok(changed)
    }
}

#[cfg(test)]
mod variant_tests {
    use super::*;
    #[test]
    fn every_variant_is_exact_and_hybrids_are_refused() {
        let required: Value = serde_json::from_str(REQUIRED).unwrap();
        let mut actual = required.clone();
        actual.as_object_mut().unwrap().remove("variants");
        actual.as_object_mut().unwrap().remove("optional");
        for (name, variants) in required["variants"].as_object().unwrap() {
            let original = actual["tables"].get(name).cloned();
            for variant in variants.as_array().unwrap() {
                actual["tables"]
                    .as_object_mut()
                    .unwrap()
                    .insert(name.clone(), variant.clone());
                accepts(&actual).unwrap();
                for other in variants.as_array().unwrap() {
                    if variant["columns"] != other["columns"] {
                        let mut hybrid = variant.clone();
                        hybrid["columns"] = other["columns"].clone();
                        assert!(!variants.as_array().unwrap().contains(&hybrid));
                        actual["tables"][name] = hybrid;
                        assert!(accepts(&actual).is_err(), "accepted hybrid {name}");
                        actual["tables"][name] = variant.clone();
                    }
                }
            }
            if let Some(value) = original {
                actual["tables"][name] = value;
            } else {
                actual["tables"].as_object_mut().unwrap().remove(name);
            }
        }
    }
}

#[cfg(test)]
mod upgrade_layout_tests {
    use super::*;
    const PRODUCTION: &str = include_str!("../../schema/upgrade-fixtures/production-v50.sql");

    #[test]
    fn normalize_matches_lockstep_cases() {
        let cases: Value =
            serde_json::from_str(include_str!("../../schema/normalize-cases.json")).unwrap();
        for case in cases["cases"].as_array().unwrap() {
            let input = case["input"].as_str().unwrap();
            assert_eq!(
                normalize(input),
                case["output"].as_str().unwrap(),
                "{input:?}"
            );
        }
    }

    /// Quoted text, comments and identifiers keep their whitespace: a CHECK,
    /// default or trigger body that accepts other values must not compare equal.
    #[test]
    fn normalize_keeps_quoted_and_commented_whitespace_distinct() {
        let cases: Value =
            serde_json::from_str(include_str!("../../schema/normalize-cases.json")).unwrap();
        let distinct = cases["distinct"].as_array().unwrap();
        assert!(!distinct.is_empty());
        for pair in distinct {
            let (a, b) = (pair[0].as_str().unwrap(), pair[1].as_str().unwrap());
            assert_ne!(normalize(a), normalize(b), "{a:?} vs {b:?}");
        }
    }

    /// The reviewed startup schema with `tables` replaced from a DDL script.
    fn with_tables(ddl: &str) -> Value {
        let mut actual: Value = serde_json::from_str(REQUIRED).unwrap();
        actual.as_object_mut().unwrap().remove("variants");
        actual.as_object_mut().unwrap().remove("optional");
        let c = Connection::open_in_memory().unwrap();
        c.execute_batch(ddl).unwrap();
        for (name, table) in snapshot(&c).unwrap()["tables"].as_object().unwrap() {
            actual["tables"][name] = table.clone();
        }
        actual
    }

    fn required() -> Value {
        serde_json::from_str(REQUIRED).unwrap()
    }

    #[test]
    fn production_upgrade_layouts_are_accepted() {
        accepts_against(&with_tables(PRODUCTION), required()).unwrap();
    }

    #[test]
    fn whitespace_only_normalization_refuses_production_claims() {
        let mut actual = with_tables(PRODUCTION);
        let c = Connection::open_in_memory().unwrap();
        c.execute_batch(PRODUCTION).unwrap();
        let raw: String = c
            .query_row(
                "SELECT sql FROM sqlite_schema WHERE name='claims'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        let legacy = raw.split_whitespace().collect::<Vec<_>>().join(" ");
        assert_ne!(legacy, normalize(&raw));
        actual["tables"]["claims"]["sql"] = json!(legacy);
        assert!(accepts_against(&actual, required()).is_err());
    }

    #[test]
    fn production_model_policy_needs_its_recorded_variant() {
        let actual = with_tables(PRODUCTION);
        let mut required = required();
        let production = actual["tables"]["model_policy"].clone();
        let variants = required["variants"]["model_policy"].as_array_mut().unwrap();
        let before = variants.len();
        variants.retain(|v| *v != production);
        assert_eq!(
            variants.len(),
            before - 1,
            "production variant is recorded once"
        );
        assert!(accepts_against(&actual, required).is_err());
    }

    #[test]
    fn different_schemas_are_still_refused() {
        for (from, to) in [
            (", session TEXT)", ")"),
            ("expires_at REAL NOT NULL", "expires_at TEXT NOT NULL"),
            ("enabled INTEGER NOT NULL", "enabled TEXT NOT NULL"),
            ("t3_model TEXT NOT NULL DEFAULT ''", "t3_model TEXT"),
            (
                "PRIMARY KEY,\n    provider",
                "PRIMARY KEY,\n    extra TEXT,\n    provider",
            ),
        ] {
            let changed = PRODUCTION.replacen(from, to, 1);
            assert_ne!(changed, PRODUCTION, "{from}");
            assert!(
                accepts_against(&with_tables(&changed), required()).is_err(),
                "{to}"
            );
        }
    }
}
