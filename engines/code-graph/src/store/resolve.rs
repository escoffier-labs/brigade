//! Persisted call-edge rebuilding and cross-file target resolution.

use std::cell::RefCell;
use std::cmp::Reverse;
use std::collections::{HashMap, HashSet, VecDeque};

use anyhow::Result;
use rusqlite::{Connection, params};

use crate::extractors::python::WILDCARD_IMPORT;
use crate::model::{CallKind, Import, PendingCall};

/// Bump when resolution rules change; persisted edges must be rebuilt on sync.
///
/// v3 follows Python re-exports and resolves names through `from x import *`.
/// v4 solves Python module bindings as a fixpoint and ignores function- and
/// class-local imports as module exports.
/// v5 treats star-import cycles that can bind more than one definition as ambiguous.
/// v6 drops binding statements shadowed by a later unconditional binding.
pub(crate) const RESOLVER_VERSION: &str = "6";

#[derive(Clone, PartialEq)]
pub(super) struct SymbolCandidate {
    pub(super) id: String,
    pub(super) file_path: String,
    pub(super) container: Option<String>,
    /// First line of the symbol, which orders it against imports in its module.
    pub(super) line: usize,
}

enum ImportResolution {
    NoImport,
    Resolved(Vec<SymbolCandidate>),
    Unresolved,
    Fallback,
}

/// Derive the `edges` table from every stored pending call.
///
/// Rebuilding from scratch keeps resolution a pure function of the current
/// symbols, imports, and pending calls: a definition added in one file gains
/// edges from callers in unchanged files, and resolutions that a change made
/// stale (a fallback superseded by a strict match, a deleted target) disappear
/// instead of lingering.
pub(super) fn rebuild_edges(tx: &Connection) -> Result<()> {
    tx.execute("DELETE FROM edges", [])?;
    let name_index = load_name_index(tx)?;
    let import_index = load_import_index(tx)?;
    let source_index = load_symbol_id_index(tx)?;
    let file_index = load_file_index(tx)?;

    let mut select = tx.prepare(
        "SELECT source_id, file_path, target_name, kind, qualifier, line FROM pending_calls",
    )?;
    let mut insert = tx.prepare(
        "INSERT OR IGNORE INTO edges(source, target, kind, line, confidence)
         VALUES (?1, ?2, 'calls', ?3, ?4)",
    )?;
    let rows = select.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, String>(2)?,
            row.get::<_, String>(3)?,
            row.get::<_, Option<String>>(4)?,
            row.get::<_, i64>(5)?,
        ))
    })?;
    for row in rows {
        let (source_id, source_file, target_name, kind, qualifier, line) = row?;
        let Some(kind) = CallKind::parse(&kind) else {
            continue;
        };
        let call = PendingCall {
            source_id,
            target_name,
            qualifier,
            kind,
            line: line.max(0) as usize,
            source_file,
        };
        for target in resolve_call(
            &call,
            &name_index,
            &import_index,
            &source_index,
            &file_index,
        ) {
            if target.candidate.id == call.source_id {
                continue;
            }
            insert.execute(params![
                call.source_id,
                target.candidate.id,
                call.line as i64,
                target.confidence
            ])?;
        }
    }
    super::meta::upsert(tx, "resolver_version", RESOLVER_VERSION)?;
    Ok(())
}

/// Partition candidates once, avoiding per-call filtering and cloning of homonyms.
pub(crate) type NameIndex = HashMap<&'static str, HashMap<String, Vec<SymbolCandidate>>>;

/// Map language family and symbol name to candidates.
pub(super) fn load_name_index(conn: &Connection) -> Result<NameIndex> {
    let mut stmt =
        conn.prepare("SELECT name, id, file_path, container, start_line FROM symbols")?;
    let rows = stmt.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            SymbolCandidate {
                id: row.get::<_, String>(1)?,
                file_path: row.get::<_, String>(2)?,
                container: row.get::<_, Option<String>>(3)?,
                line: row.get::<_, i64>(4)?.max(0) as usize,
            },
        ))
    })?;
    let mut map: NameIndex = HashMap::new();
    for row in rows {
        let (name, candidate) = row?;
        if let Some(family) = language_family(&candidate.file_path) {
            map.entry(family)
                .or_default()
                .entry(name)
                .or_default()
                .push(candidate);
        }
    }
    for candidates in map.values_mut().flat_map(|names| names.values_mut()) {
        candidates.sort_by(|left, right| {
            left.file_path
                .cmp(&right.file_path)
                .then_with(|| left.id.cmp(&right.id))
        });
    }
    Ok(map)
}

pub(super) fn load_symbol_id_index(conn: &Connection) -> Result<HashMap<String, SymbolCandidate>> {
    let mut stmt = conn.prepare("SELECT id, file_path, container, start_line FROM symbols")?;
    let rows = stmt.query_map([], |row| {
        Ok(SymbolCandidate {
            id: row.get::<_, String>(0)?,
            file_path: row.get::<_, String>(1)?,
            container: row.get::<_, Option<String>>(2)?,
            line: row.get::<_, i64>(3)?.max(0) as usize,
        })
    })?;
    let mut map = HashMap::new();
    for row in rows {
        let candidate = row?;
        map.insert(candidate.id.clone(), candidate);
    }
    Ok(map)
}

pub(crate) struct FileIndex {
    files: HashSet<String>,
    python_roots: HashMap<String, Vec<String>>,
    /// Literal Python `__all__` lists by file.
    python_exports: HashMap<String, Vec<String>>,
    /// Ids of Python top-level symbols defined under a conditional block.
    python_conditional_symbols: HashSet<String>,
    /// Complete Python binding lookups, shared by every call resolved against
    /// this index so re-export walks are not repeated per call.
    python_bindings: RefCell<HashMap<(String, String), Binding>>,
}

pub(super) fn load_file_index(conn: &Connection) -> Result<FileIndex> {
    let mut stmt = conn.prepare("SELECT path FROM files")?;
    let rows = stmt.query_map([], |row| row.get::<_, String>(0))?;
    let mut files = HashSet::new();
    for row in rows {
        files.insert(row?);
    }
    let mut python_roots: HashMap<String, Vec<String>> = HashMap::new();
    for file in &files {
        let Some(package_dir) = file.strip_suffix("/__init__.py") else {
            continue;
        };
        let Some((root, package)) = package_dir.rsplit_once('/') else {
            continue;
        };
        // A directory that is itself a package is not a source root.
        if !matches!(root.rsplit('/').next(), Some("src" | "lib" | "python"))
            || files.contains(&format!("{root}/__init__.py"))
        {
            continue;
        }
        python_roots
            .entry(package.to_string())
            .or_default()
            .push(root.to_string());
    }
    for roots in python_roots.values_mut() {
        roots.sort();
        roots.dedup();
    }
    Ok(FileIndex {
        files,
        python_roots,
        python_exports: load_python_exports(conn)?,
        python_conditional_symbols: load_conditional_symbols(conn)?,
        python_bindings: RefCell::default(),
    })
}

fn table_exists(conn: &Connection, table: &str) -> Result<bool> {
    Ok(conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?1)",
        [table],
        |row| row.get(0),
    )?)
}

/// Read the ids of conditionally defined symbols. An older database has none.
fn load_conditional_symbols(conn: &Connection) -> Result<HashSet<String>> {
    if !table_exists(conn, "conditional_symbols")? {
        return Ok(HashSet::new());
    }
    let mut stmt = conn.prepare("SELECT symbol_id FROM conditional_symbols")?;
    let rows = stmt.query_map([], |row| row.get::<_, String>(0))?;
    rows.collect::<rusqlite::Result<_>>().map_err(Into::into)
}

/// Read stored `__all__` lists. A database written before the table existed
/// has none, which means "no declared export list" everywhere.
fn load_python_exports(conn: &Connection) -> Result<HashMap<String, Vec<String>>> {
    let mut exports = HashMap::new();
    if !table_exists(conn, "module_exports")? {
        return Ok(exports);
    }
    let mut stmt = conn.prepare("SELECT file_path, names FROM module_exports")?;
    let rows = stmt.query_map([], |row| {
        Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
    })?;
    for row in rows {
        let (file, names) = row?;
        // An unreadable list is treated as undeclared rather than failing resolution.
        if let Ok(names) = serde_json::from_str::<Vec<String>>(&names) {
            exports.insert(file, names);
        }
    }
    Ok(exports)
}

pub(super) fn load_import_index(conn: &Connection) -> Result<HashMap<String, Vec<Import>>> {
    // A read-only database written before these columns existed has only
    // unconditional module-scope rows.
    let column_or = |column: &'static str, default: &'static str| -> Result<&'static str> {
        Ok(
            if super::schema::table_has_column(conn, "imports", column)? {
                column
            } else {
                default
            },
        )
    };
    let module_scope = column_or("module_scope", "1")?;
    let conditional = column_or("conditional", "0")?;
    let mut stmt = conn.prepare(&format!(
        "SELECT file_path, module, local_name, imported_name, alias, line, {module_scope}, {conditional}
         FROM imports ORDER BY file_path, line, module, local_name"
    ))?;
    let rows = stmt.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            Import {
                module: row.get::<_, String>(1)?,
                local_name: row.get::<_, Option<String>>(2)?,
                imported_name: row.get::<_, Option<String>>(3)?,
                alias: row.get::<_, Option<String>>(4)?,
                line: row.get::<_, i64>(5)? as usize,
                module_scope: row.get::<_, bool>(6)?,
                conditional: row.get::<_, bool>(7)?,
            },
        ))
    })?;
    let mut map: HashMap<String, Vec<Import>> = HashMap::new();
    for row in rows {
        let (file, import) = row?;
        map.entry(file).or_default().push(import);
    }
    Ok(map)
}

/// A resolved call target plus how the resolver got there.
///
/// Confidence encodes the resolution path, not a probability: import-strict
/// matches beat same-file matches, which beat cross-file name guesses. The
/// values order the paths and leave room between them; consumers should treat
/// them ordinally.
struct ScoredTarget {
    candidate: SymbolCandidate,
    confidence: f64,
}

/// How a call resolved (or failed to). One call resolves through exactly one
/// path; every target it produced shares that path and its confidence.
#[derive(Debug, Clone, PartialEq)]
pub(crate) enum ResolutionPath {
    /// Import matched and the target's file agrees with the imported module.
    ImportStrict,
    /// Same file, and the qualifier matched the candidate's container.
    SameFileQualified,
    /// Same file, bare call.
    SameFileBare,
    /// Cross-file bare call and exactly one symbol has this name.
    NameUnique,
    /// Import matched but the module could not be pinned to indexed files, or a
    /// bare Python call matched a name brought in by `from x import *`.
    ImportFallback,
    /// Cross-file bare call with several same-named candidates.
    NameAmbiguous,
    /// An import matched but points outside the index (stdlib, third party).
    UnresolvedExternal,
    /// No indexed symbol carries the called name.
    NoCandidates,
    /// Qualified call with no import match and no same-file container match.
    UnresolvedQualified,
}

impl ResolutionPath {
    pub(crate) fn confidence(&self) -> Option<f64> {
        match self {
            ResolutionPath::ImportStrict => Some(0.9),
            ResolutionPath::SameFileQualified => Some(0.85),
            ResolutionPath::SameFileBare => Some(0.8),
            ResolutionPath::NameUnique => Some(0.7),
            ResolutionPath::ImportFallback => Some(0.55),
            ResolutionPath::NameAmbiguous => Some(0.5),
            ResolutionPath::UnresolvedExternal
            | ResolutionPath::NoCandidates
            | ResolutionPath::UnresolvedQualified => None,
        }
    }

    pub(crate) fn label(&self) -> &'static str {
        match self {
            ResolutionPath::ImportStrict => "import-strict",
            ResolutionPath::SameFileQualified => "same-file-qualified",
            ResolutionPath::SameFileBare => "same-file-bare",
            ResolutionPath::NameUnique => "unique-name",
            ResolutionPath::ImportFallback => "import-fallback",
            ResolutionPath::NameAmbiguous => "ambiguous-name",
            ResolutionPath::UnresolvedExternal => "unresolved-external",
            ResolutionPath::NoCandidates => "no-candidates",
            ResolutionPath::UnresolvedQualified => "unresolved-qualified",
        }
    }

    pub(crate) fn describe(&self) -> &'static str {
        match self {
            ResolutionPath::ImportStrict => {
                "an import matched the call and the target lives in the imported module"
            }
            ResolutionPath::SameFileQualified => {
                "the target is in the calling file and the qualifier matched its container"
            }
            ResolutionPath::SameFileBare => "a bare call matched a symbol in the calling file",
            ResolutionPath::NameUnique => {
                "exactly one indexed symbol in the caller's language family carries this name, in another file"
            }
            ResolutionPath::ImportFallback => {
                "an import matched but its module could not be pinned to indexed files, or the name came in through `from x import *`"
            }
            ResolutionPath::NameAmbiguous => {
                "several indexed symbols in the caller's language family carry this name; candidates kept up to the cap"
            }
            ResolutionPath::UnresolvedExternal => {
                "an import matched but points outside the index (stdlib or third party); no edge"
            }
            ResolutionPath::NoCandidates => {
                "no indexed symbol in the caller's language family carries this name; no edge"
            }
            ResolutionPath::UnresolvedQualified => {
                "qualified call with no import match and no same-file container match; no edge"
            }
        }
    }
}

/// The full outcome of resolving one pending call: the path taken and the
/// targets it produced (empty on the unresolved paths).
pub(crate) struct ResolvedCall {
    pub(crate) path: ResolutionPath,
    targets: Vec<SymbolCandidate>,
}

impl ResolvedCall {
    /// The candidates the resolver produced, for explanation rendering.
    pub(super) fn targets_for_explain(&self) -> &[SymbolCandidate] {
        &self.targets
    }
}

fn resolve_call(
    call: &PendingCall,
    name_index: &NameIndex,
    import_index: &HashMap<String, Vec<Import>>,
    source_index: &HashMap<String, SymbolCandidate>,
    file_index: &FileIndex,
) -> Vec<ScoredTarget> {
    let resolved = resolve_call_explained(call, name_index, import_index, source_index, file_index);
    let Some(confidence) = resolved.path.confidence() else {
        return Vec::new();
    };
    resolved
        .targets
        .into_iter()
        .map(|candidate| ScoredTarget {
            candidate,
            confidence,
        })
        .collect()
}

pub(crate) fn resolve_call_explained(
    call: &PendingCall,
    name_index: &NameIndex,
    import_index: &HashMap<String, Vec<Import>>,
    source_index: &HashMap<String, SymbolCandidate>,
    file_index: &FileIndex,
) -> ResolvedCall {
    let import_resolution = resolve_imported_call(call, name_index, import_index, file_index);
    let use_name_fallback = match import_resolution {
        ImportResolution::Resolved(import_targets) => {
            return ResolvedCall {
                path: ResolutionPath::ImportStrict,
                targets: import_targets,
            };
        }
        ImportResolution::Unresolved => {
            return ResolvedCall {
                path: ResolutionPath::UnresolvedExternal,
                targets: Vec::new(),
            };
        }
        ImportResolution::Fallback => true,
        ImportResolution::NoImport => false,
    };

    let candidates = language_family(&call.source_file)
        .and_then(|family| name_index.get(family))
        .and_then(|names| names.get(&call.target_name));
    // A star import can bind a name that no symbol carries, through an alias
    // (`from core import original as exported`), so it is tried before giving up.
    let star_targets = || {
        (!use_name_fallback && call.kind == CallKind::Bare)
            .then(|| resolve_star_imported_call(call, name_index, import_index, file_index))
            .flatten()
    };
    let Some(candidates) = candidates else {
        if let Some(targets) = star_targets() {
            return ResolvedCall {
                path: ResolutionPath::ImportFallback,
                targets,
            };
        }
        return ResolvedCall {
            path: ResolutionPath::NoCandidates,
            targets: Vec::new(),
        };
    };

    if use_name_fallback {
        return ResolvedCall {
            path: ResolutionPath::ImportFallback,
            targets: candidates.iter().take(8).cloned().collect(),
        };
    }

    if let Some(same_file) =
        resolve_same_file_call(call, candidates, source_index).filter(|matches| !matches.is_empty())
    {
        let path = if call.kind == CallKind::Bare {
            ResolutionPath::SameFileBare
        } else {
            ResolutionPath::SameFileQualified
        };
        return ResolvedCall {
            path,
            targets: same_file,
        };
    }

    if call.kind != CallKind::Bare {
        return ResolvedCall {
            path: ResolutionPath::UnresolvedQualified,
            targets: Vec::new(),
        };
    }

    if let Some(targets) = star_targets() {
        return ResolvedCall {
            path: ResolutionPath::ImportFallback,
            targets,
        };
    }

    let path = if candidates.len() == 1 {
        ResolutionPath::NameUnique
    } else {
        ResolutionPath::NameAmbiguous
    };
    ResolvedCall {
        path,
        targets: candidates.iter().take(8).cloned().collect(),
    }
}

fn language_family(path: &str) -> Option<&'static str> {
    match path.rsplit_once('.')?.1 {
        "py" => Some("python"),
        "js" | "jsx" | "ts" | "tsx" | "astro" => Some("js/ts"),
        "rs" => Some("rust"),
        "go" => Some("go"),
        _ => None,
    }
}

fn resolve_same_file_call(
    call: &PendingCall,
    candidates: &[SymbolCandidate],
    source_index: &HashMap<String, SymbolCandidate>,
) -> Option<Vec<SymbolCandidate>> {
    let same_file: Vec<SymbolCandidate> = candidates
        .iter()
        .filter(|candidate| candidate.file_path == call.source_file)
        .cloned()
        .collect();
    if same_file.is_empty() {
        return None;
    }

    match call.kind {
        CallKind::Bare => Some(same_file),
        CallKind::Scoped => {
            let qualifier = call.qualifier.as_deref()?;
            let scoped: Vec<SymbolCandidate> = same_file
                .into_iter()
                .filter(|candidate| candidate.container.as_deref() == Some(qualifier))
                .collect();
            Some(scoped)
        }
        CallKind::Member => {
            let qualifier = call.qualifier.as_deref()?;
            if matches!(qualifier, "self" | "this") {
                let source_container = source_index
                    .get(&call.source_id)
                    .and_then(|source| source.container.as_deref())?;
                let method_targets = same_file
                    .into_iter()
                    .filter(|candidate| candidate.container.as_deref() == Some(source_container))
                    .collect();
                return Some(method_targets);
            }
            None
        }
    }
}

fn resolve_imported_call(
    call: &PendingCall,
    name_index: &NameIndex,
    import_index: &HashMap<String, Vec<Import>>,
    file_index: &FileIndex,
) -> ImportResolution {
    let Some(imports) = import_index.get(&call.source_file) else {
        return ImportResolution::NoImport;
    };
    let Some(matched_import) = matched_import_for(call, imports) else {
        return ImportResolution::NoImport;
    };

    let target_name = if call.kind == CallKind::Bare {
        matched_import
            .imported_name
            .as_deref()
            .unwrap_or(call.target_name.as_str())
    } else {
        call.target_name.as_str()
    };
    let names = language_family(&call.source_file).and_then(|family| name_index.get(family));
    let module_targets = module_targets(&call.source_file, matched_import, call.kind, file_index);
    if call.source_file.ends_with(".py") {
        // Python modules export what they bind at top level, including names
        // they only re-export, as a package `__init__.py` publishing a flat API does.
        let files = module_targets.indexed_files(file_index);
        let bindings = PythonBindings {
            names,
            import_index,
            file_index,
        };
        return match bindings.in_files(&files, target_name) {
            Binding::Found(targets) => ImportResolution::Resolved(targets),
            // A package that neither defines nor imports the name says nothing
            // about where it lives, so let the name fallback decide.
            Binding::Unbound
                if call.kind == CallKind::Bare
                    && !files.is_empty()
                    && files.iter().all(|file| is_python_package_init(file)) =>
            {
                ImportResolution::NoImport
            }
            // The name comes through a star-import cycle that can supply more
            // than one definition, so no import-strict claim is safe. The
            // lower-confidence name fallback still applies.
            Binding::Ambiguous if call.kind == CallKind::Bare => ImportResolution::NoImport,
            Binding::Unbound | Binding::Missing | Binding::Ambiguous => {
                unresolved_import_resolution(call, &module_targets, file_index)
            }
        };
    }
    let targets: Vec<SymbolCandidate> = names
        .and_then(|names| names.get(target_name))
        .into_iter()
        .flatten()
        .filter(|candidate| module_targets.matches(&candidate.file_path))
        .take(8)
        .cloned()
        .collect();
    if targets.is_empty() {
        unresolved_import_resolution(call, &module_targets, file_index)
    } else {
        ImportResolution::Resolved(targets)
    }
}

fn is_python_package_init(file: &str) -> bool {
    file == "__init__.py" || file.ends_with("/__init__.py")
}

fn is_wildcard_import(import: &Import) -> bool {
    import.imported_name.as_deref() == Some(WILDCARD_IMPORT)
}

/// What a Python module binds at top level under one name.
#[derive(Clone, PartialEq)]
enum Binding {
    /// The top-level definitions the binding resolves to.
    Found(Vec<SymbolCandidate>),
    /// The module binds the name, but not to an indexed definition: an import
    /// chain that ends without one, leaves the index, loops, or names a module.
    Missing,
    /// The module neither defines nor imports the name.
    Unbound,
    /// The binding depends on a star-import cycle that can supply more than
    /// one definition, or on a cycle whose fixpoint did not converge. Python
    /// would pick one by runtime import order, which static analysis cannot see.
    Ambiguous,
}

/// A `(module file, name)` pair whose binding the resolver needs.
type BindingKey = (String, String);

/// One module-scope statement that may bind a name, with the `(module, name)`
/// pairs it reads already resolved.
enum Statement {
    /// A top-level `def` or `class`.
    Definition(SymbolCandidate),
    /// `import x as name` binds a module, not a callable.
    ModuleImport,
    /// `from x import y as name` always binds. It is `Missing` unless `y` is found.
    Explicit(Vec<BindingKey>),
    /// `from x import *` when `x` exports the name. `listed` is set when a
    /// literal `__all__` names it, which binds the name even if nothing
    /// indexed defines it.
    Star {
        reads: Vec<BindingKey>,
        listed: bool,
    },
}

/// A binding statement plus whether it sits under a block that may not run.
struct Binder {
    statement: Statement,
    conditional: bool,
}

impl Binder {
    fn reads(&self) -> &[BindingKey] {
        self.statement.reads()
    }
}

impl Statement {
    fn reads(&self) -> &[BindingKey] {
        match self {
            Statement::Explicit(reads) | Statement::Star { reads, .. } => reads,
            Statement::Definition(_) | Statement::ModuleImport => &[],
        }
    }
}

/// Python module namespaces, read from the stored definitions and imports.
///
/// A module's binding for a name is its last module-scope binding statement
/// in line order: a top-level definition, `from x import name`, or a
/// `from x import *` that exports the name. Imports inside a function or
/// class body bind there, not in the module.
///
/// Bindings depend on other `(module, name)` bindings, and star imports make
/// those dependencies cyclic. Instead of walking paths, which is exponential
/// in a dense cycle, every unsolved pair reachable from a query is grouped
/// into strongly connected components and solved in dependency order. An
/// acyclic pair is evaluated once. A cycle is resolved only when it can bind
/// at most one definition, because which of several definitions Python binds
/// depends on runtime import order. Otherwise it is `Ambiguous`. The work is
/// polynomial and there is no hop limit. Solved pairs are memoized for the
/// rest of the resolution pass.
struct PythonBindings<'a> {
    names: Option<&'a HashMap<String, Vec<SymbolCandidate>>>,
    import_index: &'a HashMap<String, Vec<Import>>,
    file_index: &'a FileIndex,
}

impl PythonBindings<'_> {
    /// The binding of `name` across the files one import names.
    fn in_files(&self, files: &[String], name: &str) -> Binding {
        let keys: Vec<BindingKey> = files
            .iter()
            .map(|file| (file.clone(), name.to_string()))
            .collect();
        self.solve(&keys);
        let memo = self.file_index.python_bindings.borrow();
        combine(&keys, |key| {
            memo.get(key).cloned().unwrap_or(Binding::Unbound)
        })
    }

    /// What `from x import *` in `file` binds under `name`, or `None` when the
    /// star import cannot bind it.
    fn star_import_binding(&self, file: &str, import: &Import, name: &str) -> Option<Binding> {
        let Statement::Star { reads, listed } = self.star_statement(file, import, name)? else {
            return None;
        };
        self.solve(&reads);
        let memo = self.file_index.python_bindings.borrow();
        match combine(&reads, |key| {
            memo.get(key).cloned().unwrap_or(Binding::Unbound)
        }) {
            Binding::Unbound if listed => Some(Binding::Missing),
            binding => Some(binding),
        }
    }

    /// The module-scope statements of `file` that can still decide its binding
    /// of `name`, latest first.
    ///
    /// Reading bottom up, the first statement that certainly binds the name
    /// shadows everything above it: an unconditional definition, `import`, or
    /// `from x import name`, or an unconditional star import whose `__all__`
    /// lists the name. Statements under an `if`, `try`, `with`, loop, or
    /// `match` may not run, so earlier statements stay. Shadowed statements add
    /// no definitions and no dependencies, which keeps them out of cycles.
    fn statements(&self, file: &str, name: &str) -> Vec<Binder> {
        let mut statements: Vec<(usize, bool, Binder)> = self
            .names
            .and_then(|names| names.get(name))
            .into_iter()
            .flatten()
            // Only top-level definitions are module attributes, so `Box.helper` is not one.
            .filter(|candidate| candidate.file_path == file && candidate.container.is_none())
            .map(|candidate| {
                let conditional = self
                    .file_index
                    .python_conditional_symbols
                    .contains(&candidate.id);
                (
                    candidate.line,
                    !conditional,
                    Binder {
                        statement: Statement::Definition(candidate.clone()),
                        conditional,
                    },
                )
            })
            .collect();
        for import in self.import_index.get(file).into_iter().flatten() {
            if !import.module_scope {
                continue;
            }
            let statement = if is_wildcard_import(import) {
                self.star_statement(file, import, name)
            } else if import.local_name.as_deref() == Some(name) {
                Some(match import.imported_name.as_deref() {
                    Some(imported) => Statement::Explicit(self.reads(file, import, imported)),
                    None => Statement::ModuleImport,
                })
            } else {
                None
            };
            if let Some(statement) = statement {
                let binds = match &statement {
                    Statement::Star { listed, .. } => *listed,
                    _ => true,
                };
                statements.push((
                    import.line,
                    binds && !import.conditional,
                    Binder {
                        statement,
                        conditional: import.conditional,
                    },
                ));
            }
        }
        // Python keeps the last binding, so read the module bottom up.
        statements.sort_by_key(|(line, _, _)| Reverse(*line));
        let shadowing = statements
            .iter()
            .position(|(_, certain, _)| *certain)
            .map_or(statements.len(), |position| position + 1);
        statements.truncate(shadowing);
        statements
            .into_iter()
            .map(|(_, _, statement)| statement)
            .collect()
    }

    /// A star import as a statement, or `None` when its module does not export
    /// `name`: a literal `__all__` omits it, or it is private and there is no `__all__`.
    fn star_statement(&self, file: &str, import: &Import, name: &str) -> Option<Statement> {
        let reads = self.reads(file, import, name);
        let declared: Vec<&Vec<String>> = reads
            .iter()
            .filter_map(|(module, _)| self.file_index.python_exports.get(module))
            .collect();
        let listed = declared
            .iter()
            .any(|names| names.iter().any(|listed| listed == name));
        let exported = if declared.is_empty() {
            !name.starts_with('_')
        } else {
            listed
        };
        exported.then_some(Statement::Star { reads, listed })
    }

    /// The `(module, name)` pairs an import of `name` from `import`'s module reads.
    fn reads(&self, file: &str, import: &Import, name: &str) -> Vec<BindingKey> {
        module_targets(file, import, CallKind::Bare, self.file_index)
            .indexed_files(self.file_index)
            .into_iter()
            .map(|module| (module, name.to_string()))
            .collect()
    }

    /// Solve every unsolved pair reachable from `roots` and memoize the results.
    fn solve(&self, roots: &[BindingKey]) {
        let mut statements: Vec<Vec<Binder>> = Vec::new();
        let mut keys: Vec<BindingKey> = Vec::new();
        let mut index: HashMap<BindingKey, usize> = HashMap::new();
        {
            let memo = self.file_index.python_bindings.borrow();
            let mut pending: Vec<BindingKey> = roots.to_vec();
            while let Some(key) = pending.pop() {
                if memo.contains_key(&key) || index.contains_key(&key) {
                    continue;
                }
                let key_statements = self.statements(&key.0, &key.1);
                pending.extend(
                    key_statements
                        .iter()
                        .flat_map(Binder::reads)
                        .filter(|read| !memo.contains_key(*read) && !index.contains_key(*read))
                        .cloned(),
                );
                index.insert(key.clone(), keys.len());
                keys.push(key);
                statements.push(key_statements);
            }
        }
        if keys.is_empty() {
            return;
        }

        // Edges from each pair to the unsolved pairs it reads.
        let reads: Vec<Vec<usize>> = statements
            .iter()
            .map(|key_statements| {
                let mut targets: Vec<usize> = key_statements
                    .iter()
                    .flat_map(Binder::reads)
                    .filter_map(|read| index.get(read).copied())
                    .collect();
                targets.sort_unstable();
                targets.dedup();
                targets
            })
            .collect();

        let mut values: Vec<Option<Binding>> = vec![None; keys.len()];
        {
            let memo = self.file_index.python_bindings.borrow();
            // Components come out with everything they read already solved.
            for component in strongly_connected_components(&reads) {
                let cyclic = component.len() > 1 || reads[component[0]].contains(&component[0]);
                let solved = if cyclic {
                    solve_cycle(&component, &statements, &reads, &index, &values, &memo)
                } else {
                    let member = component[0];
                    let value = evaluate(&statements[member], |read| {
                        read_value(read, &index, &values, &memo)
                    });
                    vec![(member, value)]
                };
                for (member, value) in solved {
                    values[member] = Some(value);
                }
            }
        }
        let solved: Vec<(BindingKey, Binding)> = keys
            .into_iter()
            .zip(values)
            .map(|(key, value)| (key, value.unwrap_or(Binding::Ambiguous)))
            .collect();
        self.file_index.python_bindings.borrow_mut().extend(solved);
    }
}

/// The current value of a pair: solved in this pass, memoized earlier, or unbound.
fn read_value(
    read: &BindingKey,
    index: &HashMap<BindingKey, usize>,
    values: &[Option<Binding>],
    memo: &HashMap<BindingKey, Binding>,
) -> Binding {
    match index.get(read) {
        Some(&slot) => values[slot].clone().unwrap_or(Binding::Unbound),
        None => memo.get(read).cloned().unwrap_or(Binding::Unbound),
    }
}

/// Solve one strongly connected component of star and explicit imports.
///
/// Which definition a cycle binds depends on runtime import order. The
/// component is resolved only when everything that can flow into it (its own
/// top-level definitions plus the solved bindings it reads from outside)
/// agrees on at most one definition. Then a worklist fixpoint settles which
/// members bind it. Otherwise, or if the fixpoint does not converge within its
/// polynomial budget, every member is `Ambiguous` and nothing unconverged is
/// cached as solved.
fn solve_cycle(
    component: &[usize],
    statements: &[Vec<Binder>],
    reads: &[Vec<usize>],
    index: &HashMap<BindingKey, usize>,
    values: &[Option<Binding>],
    memo: &HashMap<BindingKey, Binding>,
) -> Vec<(usize, Binding)> {
    let ambiguous = || {
        component
            .iter()
            .map(|member| (*member, Binding::Ambiguous))
            .collect()
    };
    let members: HashSet<usize> = component.iter().copied().collect();
    let mut supplied: HashSet<&str> = HashSet::new();
    for member in component {
        for statement in &statements[*member] {
            if let Statement::Definition(candidate) = &statement.statement {
                supplied.insert(&candidate.id);
            }
            for read in statement.reads() {
                if index.get(read).is_some_and(|slot| members.contains(slot)) {
                    continue;
                }
                match index.get(read) {
                    Some(&slot) => match &values[slot] {
                        Some(Binding::Found(targets)) => {
                            supplied.extend(targets.iter().map(|target| target.id.as_str()));
                        }
                        Some(Binding::Ambiguous) => return ambiguous(),
                        _ => {}
                    },
                    None => match memo.get(read) {
                        Some(Binding::Found(targets)) => {
                            supplied.extend(targets.iter().map(|target| target.id.as_str()));
                        }
                        Some(Binding::Ambiguous) => return ambiguous(),
                        _ => {}
                    },
                }
            }
        }
    }
    if supplied.len() > 1 {
        return ambiguous();
    }

    // At most one definition can flow, so the worklist only settles which
    // members bind it and which stay unbound or missing.
    let mut current: HashMap<usize, Binding> = component
        .iter()
        .map(|member| (*member, Binding::Unbound))
        .collect();
    let mut readers: HashMap<usize, Vec<usize>> = HashMap::new();
    for member in component {
        for read in &reads[*member] {
            if members.contains(read) {
                readers.entry(*read).or_default().push(*member);
            }
        }
    }
    let mut queue: VecDeque<usize> = component.iter().copied().collect();
    let mut queued: HashSet<usize> = members.clone();
    let statement_count: usize = component
        .iter()
        .map(|member| statements[*member].len())
        .sum();
    let mut budget = component
        .len()
        .saturating_mul(statement_count + 1)
        .saturating_mul(4);
    while let Some(member) = queue.pop_front() {
        queued.remove(&member);
        if budget == 0 {
            return ambiguous();
        }
        budget -= 1;
        let value = evaluate(&statements[member], |read| match index.get(read) {
            Some(slot) if members.contains(slot) => current[slot].clone(),
            _ => read_value(read, index, values, memo),
        });
        if current[&member] != value {
            current.insert(member, value);
            for reader in readers.get(&member).into_iter().flatten() {
                if queued.insert(*reader) {
                    queue.push_back(*reader);
                }
            }
        }
    }
    current.into_iter().collect()
}

/// Tarjan's strongly connected components, iterative so deep import chains
/// cannot overflow the stack. Components are returned in reverse topological
/// order: every component comes after all the components it reads.
fn strongly_connected_components(edges: &[Vec<usize>]) -> Vec<Vec<usize>> {
    const UNVISITED: usize = usize::MAX;
    let count = edges.len();
    let mut order = vec![UNVISITED; count];
    let mut low = vec![0; count];
    let mut on_stack = vec![false; count];
    let mut stack: Vec<usize> = Vec::new();
    let mut components = Vec::new();
    let mut next_order = 0;
    for start in 0..count {
        if order[start] != UNVISITED {
            continue;
        }
        // Frames are (node, index of the next edge to follow).
        let mut frames: Vec<(usize, usize)> = vec![(start, 0)];
        order[start] = next_order;
        low[start] = next_order;
        next_order += 1;
        stack.push(start);
        on_stack[start] = true;
        while let Some(frame) = frames.last_mut() {
            let (node, edge) = *frame;
            if let Some(&target) = edges[node].get(edge) {
                frame.1 += 1;
                if order[target] == UNVISITED {
                    order[target] = next_order;
                    low[target] = next_order;
                    next_order += 1;
                    stack.push(target);
                    on_stack[target] = true;
                    frames.push((target, 0));
                } else if on_stack[target] {
                    low[node] = low[node].min(order[target]);
                }
                continue;
            }
            frames.pop();
            if let Some(&(parent, _)) = frames.last() {
                low[parent] = low[parent].min(low[node]);
            }
            if low[node] == order[node] {
                let mut component = Vec::new();
                while let Some(member) = stack.pop() {
                    on_stack[member] = false;
                    component.push(member);
                    if member == node {
                        break;
                    }
                }
                component.sort_unstable();
                components.push(component);
            }
        }
    }
    components
}

/// A module's binding from its statements, latest first, given the bindings it reads.
///
/// The latest statement that binds the name wins. When it is conditional, an
/// earlier statement may bind instead, so every candidate up to the first
/// unconditional binding counts. If those candidates name more than one
/// definition, the binding is `Ambiguous`.
fn evaluate(statements: &[Binder], read: impl Fn(&BindingKey) -> Binding) -> Binding {
    let mut chosen: Option<Binding> = None;
    let mut definitions: Vec<String> = Vec::new();
    for binder in statements {
        let value = match &binder.statement {
            Statement::Definition(candidate) => Binding::Found(vec![candidate.clone()]),
            Statement::ModuleImport => Binding::Missing,
            Statement::Explicit(reads) => match combine(reads, &read) {
                Binding::Found(targets) => Binding::Found(targets),
                Binding::Ambiguous => Binding::Ambiguous,
                Binding::Missing | Binding::Unbound => Binding::Missing,
            },
            Statement::Star { reads, listed } => match combine(reads, &read) {
                Binding::Unbound if !listed => continue,
                Binding::Unbound => Binding::Missing,
                binding => binding,
            },
        };
        match &value {
            Binding::Ambiguous => return Binding::Ambiguous,
            Binding::Found(targets) => {
                // One import can name several files. Only a different
                // definition from another statement makes the binding ambiguous.
                if !definitions.is_empty()
                    && targets
                        .iter()
                        .any(|target| !definitions.contains(&target.id))
                {
                    return Binding::Ambiguous;
                }
                definitions.extend(targets.iter().map(|target| target.id.clone()));
            }
            Binding::Missing | Binding::Unbound => {}
        }
        chosen.get_or_insert(value);
        if !binder.conditional {
            break;
        }
    }
    chosen.unwrap_or(Binding::Unbound)
}

/// Merge the bindings of one import's target files (normally a single file).
fn combine(keys: &[BindingKey], read: impl Fn(&BindingKey) -> Binding) -> Binding {
    let mut found: Vec<SymbolCandidate> = Vec::new();
    let mut missing = false;
    for key in keys {
        match read(key) {
            Binding::Found(targets) => {
                for target in targets {
                    if !found.iter().any(|existing| existing.id == target.id) {
                        found.push(target);
                    }
                }
            }
            Binding::Ambiguous => return Binding::Ambiguous,
            Binding::Missing => missing = true,
            Binding::Unbound => {}
        }
    }
    if !found.is_empty() {
        found.truncate(8);
        Binding::Found(found)
    } else if missing {
        Binding::Missing
    } else {
        Binding::Unbound
    }
}

/// Resolve a bare Python call through the caller's `from x import *` imports.
/// The last star import that binds the name wins, as at runtime.
fn resolve_star_imported_call(
    call: &PendingCall,
    name_index: &NameIndex,
    import_index: &HashMap<String, Vec<Import>>,
    file_index: &FileIndex,
) -> Option<Vec<SymbolCandidate>> {
    if !call.source_file.ends_with(".py") {
        return None;
    }
    let bindings = PythonBindings {
        names: name_index.get("python"),
        import_index,
        file_index,
    };
    let mut stars: Vec<&Import> = import_index
        .get(&call.source_file)?
        .iter()
        .filter(|import| is_wildcard_import(import))
        .collect();
    stars.sort_by_key(|import| Reverse(import.line));
    for import in stars {
        match bindings.star_import_binding(&call.source_file, import, &call.target_name) {
            Some(Binding::Found(targets)) => return Some(targets),
            // A later star import binds the name to something unindexed, or to
            // a definition a cycle cannot pin down.
            Some(Binding::Missing | Binding::Ambiguous) => return None,
            Some(Binding::Unbound) | None => {}
        }
    }
    None
}

fn unresolved_import_resolution(
    call: &PendingCall,
    module_targets: &ModuleTargets,
    file_index: &FileIndex,
) -> ImportResolution {
    if call.kind == CallKind::Bare || module_targets.is_external(file_index) {
        ImportResolution::Unresolved
    } else {
        ImportResolution::Fallback
    }
}

/// The import a call would resolve through, shared by resolution and explain.
pub(super) fn matched_import_for<'i>(
    call: &PendingCall,
    imports: &'i [Import],
) -> Option<&'i Import> {
    // A star import binds no single name, so it never matches by name here.
    imports
        .iter()
        .filter(|import| !is_wildcard_import(import))
        .find(|import| match call.kind {
            CallKind::Bare => import.local_name.as_deref() == Some(call.target_name.as_str()),
            CallKind::Member | CallKind::Scoped => call
                .qualifier
                .as_deref()
                .is_some_and(|qualifier| import_matches_qualifier(import, qualifier)),
        })
}

fn import_matches_qualifier(import: &Import, qualifier: &str) -> bool {
    import.local_name.as_deref() == Some(qualifier)
        || import.alias.as_deref() == Some(qualifier)
        || import.module == qualifier
}

#[derive(Default)]
struct ModuleTargets {
    family: Option<&'static str>,
    files: Vec<String>,
    dirs: Vec<String>,
    relative: bool,
}

impl ModuleTargets {
    fn matches(&self, file_path: &str) -> bool {
        if self.family.is_none() || language_family(file_path) != self.family {
            return false;
        }
        self.files.iter().any(|file| file == file_path)
            || self.dirs.iter().any(|dir| {
                file_path.strip_prefix(dir).is_some_and(|rest| {
                    // Go packages comprise files directly in the directory.
                    self.family != Some("go") || !rest.contains('/')
                })
            })
    }

    fn has_indexed_match(&self, file_index: &FileIndex) -> bool {
        self.files
            .iter()
            .any(|file| file_index.files.contains(file) && self.matches(file))
            || (!self.dirs.is_empty() && file_index.files.iter().any(|file| self.matches(file)))
    }

    /// Indexed files this target names exactly, in sorted order.
    fn indexed_files(&self, file_index: &FileIndex) -> Vec<String> {
        self.files
            .iter()
            .filter(|file| file_index.files.contains(*file) && self.matches(file))
            .cloned()
            .collect()
    }

    fn is_external(&self, file_index: &FileIndex) -> bool {
        !self.relative && !self.has_indexed_match(file_index)
    }

    fn finish(&mut self) {
        self.files.sort();
        self.files.dedup();
        self.dirs.sort();
        self.dirs.dedup();
    }
}

fn module_targets(
    source_file: &str,
    import: &Import,
    call_kind: CallKind,
    file_index: &FileIndex,
) -> ModuleTargets {
    let mut targets = ModuleTargets {
        family: language_family(source_file),
        ..ModuleTargets::default()
    };
    if source_file.ends_with(".py") {
        targets.relative = import.module.starts_with('.');
        if let Some(prefix) = python_module_prefix(source_file, import, call_kind) {
            push_module_variants(&mut targets.files, &prefix, &["py"]);
            // Prefer the repository-root module for flat layouts. Only look under
            // inferred source roots when that module has no indexed file.
            if !targets.relative && !targets.has_indexed_match(file_index) {
                let package = import.module.split('.').next().unwrap_or("");
                if let Some(roots) = file_index.python_roots.get(package) {
                    let mut deepest = None;
                    let mut selected = Vec::new();
                    let mut tied = false;
                    for root in roots {
                        let parent = root.rsplit_once('/').map_or("", |(parent, _)| parent);
                        if !parent.is_empty()
                            && !source_file
                                .strip_prefix(parent)
                                .is_some_and(|rest| rest.starts_with('/'))
                        {
                            continue;
                        }
                        let mut files = Vec::new();
                        push_module_variants(&mut files, &format!("{root}/{prefix}"), &["py"]);
                        if !files.iter().any(|file| file_index.files.contains(file)) {
                            continue;
                        }
                        let depth = parent.split('/').filter(|part| !part.is_empty()).count();
                        if deepest.is_none_or(|previous| depth > previous) {
                            deepest = Some(depth);
                            selected = files;
                            tied = false;
                        } else if deepest == Some(depth) {
                            tied = true;
                        }
                    }
                    if !tied {
                        targets.files.extend(selected);
                    }
                }
            }
        }
    } else if source_file.ends_with(".go") {
        push_go_module_targets(&mut targets, &import.module);
    } else if source_file.ends_with(".rs") {
        targets.relative = rust_module_prefix(&import.module).is_some();
        if let Some(prefix) = rust_module_prefix(&import.module) {
            push_module_variants(&mut targets.files, &prefix, &["rs"]);
            targets.dirs.push(format!("{prefix}/"));
        }
    } else if import.module.starts_with('.') {
        targets.relative = true;
        if let Some(prefix) = normalize_relative_path_module(source_file, &import.module) {
            if has_js_like_extension(&prefix) {
                targets.files.push(prefix);
            } else {
                push_module_variants(
                    &mut targets.files,
                    &prefix,
                    &["ts", "tsx", "js", "jsx", "astro"],
                );
            }
        }
    }
    targets.finish();
    targets
}

fn python_module_prefix(source_file: &str, import: &Import, call_kind: CallKind) -> Option<String> {
    if import.module.starts_with('.') {
        let mut prefix = normalize_python_relative_module(source_file, &import.module)?;
        if let Some(imported_name) = import
            .imported_name
            .as_deref()
            .filter(|name| call_kind != CallKind::Bare && !name.is_empty())
        {
            append_module_path(&mut prefix, imported_name);
        }
        Some(prefix)
    } else {
        let mut prefix = import.module.replace('.', "/");
        if let Some(imported_name) = import
            .imported_name
            .as_deref()
            .filter(|name| call_kind != CallKind::Bare && !name.is_empty())
        {
            append_module_path(&mut prefix, imported_name);
        }
        Some(prefix)
    }
}

fn normalize_python_relative_module(source_file: &str, module: &str) -> Option<String> {
    let dot_count = module
        .chars()
        .take_while(|character| *character == '.')
        .count();
    if dot_count == 0 {
        return Some(module.replace('.', "/"));
    }
    let base_dir = source_file.rsplit_once('/').map_or("", |(dir, _)| dir);
    let mut parts: Vec<&str> = base_dir
        .split('/')
        .filter(|part| !part.is_empty())
        .collect();
    for _ in 1..dot_count {
        parts.pop();
    }
    let rest = &module[dot_count..];
    push_path_components(&mut parts, rest.split('.'));
    if parts.is_empty() {
        None
    } else {
        Some(parts.join("/"))
    }
}

fn push_path_components<'a>(
    parts: &mut Vec<&'a str>,
    components: impl IntoIterator<Item = &'a str>,
) {
    parts.extend(components.into_iter().filter(|part| !part.is_empty()));
}

fn append_module_path(prefix: &mut String, module: &str) {
    if !prefix.is_empty() {
        prefix.push('/');
    }
    prefix.push_str(&module.replace('.', "/"));
}

fn rust_module_prefix(module: &str) -> Option<String> {
    let stripped = module
        .strip_prefix("crate")
        .or_else(|| module.strip_prefix("graphtrail"))?;
    let stripped = stripped.strip_prefix("::").unwrap_or(stripped);
    if stripped.is_empty() {
        Some("src/lib".to_string())
    } else {
        Some(format!("src/{}", stripped.replace("::", "/")))
    }
}

fn push_go_module_targets(targets: &mut ModuleTargets, module: &str) {
    let parts: Vec<&str> = module.split('/').filter(|part| !part.is_empty()).collect();
    for start in 0..parts.len() {
        targets.dirs.push(format!("{}/", parts[start..].join("/")));
    }
}

fn normalize_relative_path_module(source_file: &str, module: &str) -> Option<String> {
    let base_dir = source_file.rsplit_once('/').map_or("", |(dir, _)| dir);
    let mut parts: Vec<&str> = base_dir
        .split('/')
        .filter(|part| !part.is_empty())
        .collect();
    for part in module.split('/') {
        match part {
            "" | "." => {}
            ".." => {
                parts.pop();
            }
            name => parts.push(name),
        }
    }
    if parts.is_empty() {
        None
    } else {
        Some(parts.join("/"))
    }
}

fn has_js_like_extension(path: &str) -> bool {
    [".ts", ".tsx", ".js", ".jsx", ".astro"]
        .iter()
        .any(|ext| path.ends_with(ext))
}

fn push_module_variants(files: &mut Vec<String>, prefix: &str, exts: &[&str]) {
    for ext in exts {
        files.push(format!("{prefix}.{ext}"));
        files.push(format!("{prefix}/index.{ext}"));
        if *ext == "py" {
            files.push(format!("{prefix}/__init__.py"));
        }
    }
}
