//! Database lifecycle: path resolution, connection opening, and time helpers.

use std::fs;
use std::path::{Path, PathBuf};
use std::sync::{Mutex, OnceLock};
use std::time::{SystemTime, UNIX_EPOCH};
use std::{
    collections::HashSet,
    ffi::{OsStr, OsString},
};

use anyhow::{Context, Result};
use rusqlite::{Connection, OpenFlags};

use super::env;

static CREATED_DB_DIRS: OnceLock<Mutex<HashSet<PathBuf>>> = OnceLock::new();

/// Canonical index location relative to the repository root.
pub const INDEX_DIR: [&str; 2] = [".brigade", "code-graph"];
pub const INDEX_DB_NAME: &str = "code-graph.db";
/// Legacy index location, read and written in place for the rename window (v0.31, v0.32).
pub const LEGACY_INDEX_DIR: &str = ".graphtrail";
pub const LEGACY_INDEX_DB_NAME: &str = "graphtrail.db";
pub const DB_ENV: &str = "BRIGADE_CODE_GRAPH_DB";
pub const LEGACY_DB_ENV: &str = "GRAPHTRAIL_DB";

/// Where a resolved index location came from (docs/phase-engine-rename.md, "Resolution rule").
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum DbSource {
    /// `--db`, then the canonical variable, then the legacy one.
    Explicit,
    /// The canonical index exists.
    Canonical,
    /// Only the legacy index exists; it is read and written in place.
    Legacy,
    /// Neither exists; the canonical index will be created.
    New,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ResolvedDb {
    pub path: PathBuf,
    pub source: DbSource,
    /// True whenever the legacy index exists, so doctor can report both.
    pub legacy_present: bool,
}

/// Canonical index directory for `root`.
pub fn index_dir(root: &Path) -> PathBuf {
    INDEX_DIR
        .iter()
        .fold(root.to_path_buf(), |path, part| path.join(part))
}

/// Legacy index directory for `root`.
pub fn legacy_index_dir(root: &Path) -> PathBuf {
    root.join(LEGACY_INDEX_DIR)
}

/// Resolve the index of the repository at `root` from what exists on disk (rules 2 to 4).
/// Explicit overrides are the caller's business; per-call MCP `repo` selectors use this directly.
pub fn resolve_repo_db(root: &Path) -> ResolvedDb {
    let canonical = index_dir(root).join(INDEX_DB_NAME);
    let legacy = legacy_index_dir(root).join(LEGACY_INDEX_DB_NAME);
    let legacy_present = legacy.is_file();
    if canonical.is_file() {
        ResolvedDb {
            path: canonical,
            source: DbSource::Canonical,
            legacy_present,
        }
    } else if legacy_present {
        ResolvedDb {
            path: legacy,
            source: DbSource::Legacy,
            legacy_present,
        }
    } else {
        ResolvedDb {
            path: canonical,
            source: DbSource::New,
            legacy_present,
        }
    }
}

/// The full resolution rule with an injected environment lookup. Never prints. A relative
/// explicit path is returned as given, so it stays relative to the working directory.
pub fn resolve_db_with(
    explicit: Option<PathBuf>,
    root: &Path,
    lookup: impl Fn(&str) -> Option<OsString>,
) -> Result<ResolvedDb> {
    let repo = resolve_repo_db(root);
    let explicit = match explicit {
        Some(path) => Some(path),
        None => env::read_renamed_with(DB_ENV, LEGACY_DB_ENV, lookup)?
            .map(|found| PathBuf::from(found.value)),
    };
    Ok(match explicit {
        Some(path) => ResolvedDb {
            path,
            source: DbSource::Explicit,
            legacy_present: repo.legacy_present,
        },
        None => repo,
    })
}

/// Resolve the index for `root` from `--db`, the environment, then the disk layout. Warns once on
/// stderr when the legacy variable is in effect and refuses conflicting variables.
pub fn resolve_db(explicit: Option<PathBuf>, root: &Path) -> Result<ResolvedDb> {
    let explicit = match explicit {
        Some(path) => Some(path),
        None => env::read_renamed(DB_ENV, LEGACY_DB_ENV)?.map(PathBuf::from),
    };
    resolve_db_with(explicit, root, |_| None)
}

/// Resolve the db path for `root` (see [`resolve_db`]).
pub fn db_path(explicit: Option<PathBuf>, root: &Path) -> Result<PathBuf> {
    Ok(resolve_db(explicit, root)?.path)
}

/// Open the db for a query command, resolving the default in the working directory.
pub fn open_default(explicit: Option<PathBuf>) -> Result<Connection> {
    open_db(&db_path(explicit, Path::new(""))?)
}

/// Open the db read-only for a query command, resolving the default in the working directory.
pub fn open_default_read_only(explicit: Option<PathBuf>) -> Result<Connection> {
    open_read_only(&db_path(explicit, Path::new(""))?)
}

/// Repository root for an index path in either layout, or `None` for any other location.
pub fn repo_root_for_db(db: &Path) -> Option<PathBuf> {
    let graph_dir = db.parent()?;
    let parent = if graph_dir.file_name()? == LEGACY_INDEX_DIR {
        graph_dir.parent()?
    } else if is_index_dir(graph_dir) {
        graph_dir.parent()?.parent()?
    } else {
        return None;
    };
    // A relative default like `.brigade/code-graph/code-graph.db` has an empty parent here;
    // the repo root in that case is the current directory.
    if parent.as_os_str().is_empty() {
        std::env::current_dir().ok()
    } else {
        Some(parent.to_path_buf())
    }
}

/// True for a `<repo>/.brigade/code-graph` directory path.
pub(crate) fn is_index_dir(path: &Path) -> bool {
    path.file_name() == Some(OsStr::new(INDEX_DIR[1]))
        && path.parent().and_then(Path::file_name) == Some(OsStr::new(INDEX_DIR[0]))
}

/// Open (creating parent dirs) a WAL-mode SQLite connection.
pub fn open_db(path: &Path) -> Result<Connection> {
    if let Some(parent) = path.parent() {
        let records_index_dir_creation = (parent.file_name() == Some(OsStr::new(LEGACY_INDEX_DIR))
            || is_index_dir(parent))
            && !parent.exists();
        fs::create_dir_all(parent)
            .with_context(|| format!("failed to create db directory {}", parent.display()))?;
        if records_index_dir_creation {
            record_created_db_dir(parent);
        }
    }
    let conn =
        Connection::open(path).with_context(|| format!("failed to open {}", path.display()))?;
    conn.pragma_update(None, "journal_mode", "WAL")?;
    Ok(conn)
}

pub(crate) fn take_created_graph_dir(path: &Path) -> bool {
    let normalized = normalize_dir(path);
    created_db_dirs()
        .lock()
        .map(|mut dirs| dirs.remove(&normalized))
        .unwrap_or(false)
}

fn record_created_db_dir(path: &Path) {
    let normalized = normalize_dir(path);
    if let Ok(mut dirs) = created_db_dirs().lock() {
        dirs.insert(normalized);
    }
}

fn created_db_dirs() -> &'static Mutex<HashSet<PathBuf>> {
    CREATED_DB_DIRS.get_or_init(|| Mutex::new(HashSet::new()))
}

fn normalize_dir(path: &Path) -> PathBuf {
    path.canonicalize().unwrap_or_else(|_| path.to_path_buf())
}

/// Open an existing db read-only. Used by the MCP server and query commands so they can never
/// mutate the graph. Deliberately NOT `immutable=1`: these dbs are rewritten by a background
/// sync, and immutable connections skip locking entirely, so a concurrent write could serve
/// torn reads. SQLite (3.22+) reads a quiescent WAL db without creating sidecar files.
pub fn open_read_only(path: &Path) -> Result<Connection> {
    let conn = Connection::open_with_flags(
        path,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .with_context(|| format!("failed to open {} read-only", path.display()))?;
    Ok(conn)
}

pub fn now_ts() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_secs() as i64)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::Value;
    use std::collections::HashMap;

    /// The resolution cases shared with the Python and Go tests.
    fn shared_cases() -> Value {
        let path = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../tests/fixtures/engine-paths-cases.json");
        let text = fs::read_to_string(&path)
            .unwrap_or_else(|err| panic!("read {}: {err}", path.display()));
        serde_json::from_str(&text).unwrap()
    }

    #[test]
    fn shared_engine_path_cases_resolve_as_listed() {
        let fixture = shared_cases();
        let block = &fixture["code_graph"];
        let layouts = &block["layouts"];
        let layout = |role: &str| PathBuf::from(layouts[role].as_str().unwrap());
        assert_eq!(
            layout("canonical"),
            index_dir(Path::new("")).join(INDEX_DB_NAME)
        );
        assert_eq!(
            layout("legacy"),
            legacy_index_dir(Path::new("")).join(LEGACY_INDEX_DB_NAME)
        );
        assert_eq!(block["env"]["canonical"], DB_ENV);
        assert_eq!(block["env"]["legacy"], LEGACY_DB_ENV);

        let cases = block["cases"].as_array().unwrap();
        assert!(!cases.is_empty());
        for case in cases {
            let name = case["name"].as_str().unwrap();
            let dir = tempfile::tempdir().unwrap();
            let root = dir.path();
            for role in case["present"].as_array().unwrap() {
                let file = root.join(layout(role.as_str().unwrap()));
                fs::create_dir_all(file.parent().unwrap()).unwrap();
                fs::write(&file, b"").unwrap();
            }
            let mut vars: HashMap<String, OsString> = HashMap::new();
            if let Some(env) = case.get("env").and_then(Value::as_object) {
                for (role, value) in env {
                    let variable = block["env"][role.as_str()].as_str().unwrap();
                    vars.insert(variable.to_string(), value.as_str().unwrap().into());
                }
            }
            let explicit = case
                .get("explicit")
                .and_then(Value::as_str)
                .map(PathBuf::from);
            let resolved = resolve_db_with(explicit, root, |name| vars.get(name).cloned());
            let expect = &case["expect"];
            if expect.get("error").is_some() {
                let err = resolved.expect_err(name);
                assert!(err.to_string().contains("conflicting"), "{name}: {err}");
                continue;
            }
            let resolved = resolved.unwrap_or_else(|err| panic!("{name}: {err}"));
            let source = match expect["source"].as_str().unwrap() {
                "explicit" => DbSource::Explicit,
                "canonical" => DbSource::Canonical,
                "legacy" => DbSource::Legacy,
                "new" => DbSource::New,
                other => panic!("{name}: unknown source {other}"),
            };
            assert_eq!(resolved.source, source, "{name}");
            assert_eq!(
                resolved.legacy_present,
                expect["legacy_present"].as_bool().unwrap(),
                "{name}"
            );
            let expected_path = match expect.get("path").and_then(Value::as_str) {
                // Explicit engine paths stay as given (relative to the working directory).
                Some(path) => PathBuf::from(path),
                None => root.join(layout(expect["layout"].as_str().unwrap())),
            };
            assert_eq!(resolved.path, expected_path, "{name}");
        }
    }

    #[test]
    fn repo_root_for_db_accepts_both_layouts_only() {
        let root = Path::new("/tmp/example");
        assert_eq!(
            repo_root_for_db(&index_dir(root).join(INDEX_DB_NAME)),
            Some(root.to_path_buf())
        );
        assert_eq!(
            repo_root_for_db(&legacy_index_dir(root).join(LEGACY_INDEX_DB_NAME)),
            Some(root.to_path_buf())
        );
        assert_eq!(
            repo_root_for_db(&index_dir(Path::new("")).join(INDEX_DB_NAME)),
            std::env::current_dir().ok()
        );
        assert_eq!(repo_root_for_db(&root.join("other").join("x.db")), None);
        assert_eq!(
            repo_root_for_db(&root.join("code-graph").join("x.db")),
            None
        );
    }
}
