//! Persisted call-edge rebuilding and cross-file target resolution.

use std::collections::{HashMap, HashSet};

use anyhow::Result;
use rusqlite::{Connection, params};

use crate::model::{CallKind, Import, PendingCall};

#[derive(Clone)]
struct SymbolCandidate {
    id: String,
    file_path: String,
    container: Option<String>,
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
    Ok(())
}

/// Map symbol name -> candidates, used to resolve call targets.
fn load_name_index(conn: &Connection) -> Result<HashMap<String, Vec<SymbolCandidate>>> {
    let mut stmt = conn.prepare("SELECT name, id, file_path, container FROM symbols")?;
    let rows = stmt.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            SymbolCandidate {
                id: row.get::<_, String>(1)?,
                file_path: row.get::<_, String>(2)?,
                container: row.get::<_, Option<String>>(3)?,
            },
        ))
    })?;
    let mut map: HashMap<String, Vec<SymbolCandidate>> = HashMap::new();
    for row in rows {
        let (name, candidate) = row?;
        map.entry(name).or_default().push(candidate);
    }
    for candidates in map.values_mut() {
        candidates.sort_by(|left, right| {
            left.file_path
                .cmp(&right.file_path)
                .then_with(|| left.id.cmp(&right.id))
        });
    }
    Ok(map)
}

fn load_symbol_id_index(conn: &Connection) -> Result<HashMap<String, SymbolCandidate>> {
    let mut stmt = conn.prepare("SELECT id, file_path, container FROM symbols")?;
    let rows = stmt.query_map([], |row| {
        Ok(SymbolCandidate {
            id: row.get::<_, String>(0)?,
            file_path: row.get::<_, String>(1)?,
            container: row.get::<_, Option<String>>(2)?,
        })
    })?;
    let mut map = HashMap::new();
    for row in rows {
        let candidate = row?;
        map.insert(candidate.id.clone(), candidate);
    }
    Ok(map)
}

fn load_file_index(conn: &Connection) -> Result<HashSet<String>> {
    let mut stmt = conn.prepare("SELECT path FROM files")?;
    let rows = stmt.query_map([], |row| row.get::<_, String>(0))?;
    let mut files = HashSet::new();
    for row in rows {
        files.insert(row?);
    }
    Ok(files)
}

fn load_import_index(conn: &Connection) -> Result<HashMap<String, Vec<Import>>> {
    let mut stmt = conn.prepare(
        "SELECT file_path, module, local_name, imported_name, alias, line FROM imports
         ORDER BY file_path, line, module, local_name",
    )?;
    let rows = stmt.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            Import {
                module: row.get::<_, String>(1)?,
                local_name: row.get::<_, Option<String>>(2)?,
                imported_name: row.get::<_, Option<String>>(3)?,
                alias: row.get::<_, Option<String>>(4)?,
                line: row.get::<_, i64>(5)? as usize,
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

/// Import matched and the target's file agrees with the imported module.
const CONFIDENCE_IMPORT_STRICT: f64 = 0.9;
/// Same file, and the qualifier matched the candidate's container.
const CONFIDENCE_SAME_FILE_QUALIFIED: f64 = 0.85;
/// Same file, bare call.
const CONFIDENCE_SAME_FILE_BARE: f64 = 0.8;
/// Cross-file bare call and exactly one symbol has this name.
const CONFIDENCE_NAME_UNIQUE: f64 = 0.7;
/// Import matched but the module could not be pinned to indexed files.
const CONFIDENCE_IMPORT_FALLBACK: f64 = 0.55;
/// Cross-file bare call with several same-named candidates.
const CONFIDENCE_NAME_AMBIGUOUS: f64 = 0.5;

fn resolve_call(
    call: &PendingCall,
    name_index: &HashMap<String, Vec<SymbolCandidate>>,
    import_index: &HashMap<String, Vec<Import>>,
    source_index: &HashMap<String, SymbolCandidate>,
    file_index: &HashSet<String>,
) -> Vec<ScoredTarget> {
    let import_resolution = resolve_imported_call(call, name_index, import_index, file_index);
    let use_name_fallback = match import_resolution {
        ImportResolution::Resolved(import_targets) => {
            return scored(import_targets, CONFIDENCE_IMPORT_STRICT);
        }
        ImportResolution::Unresolved => return Vec::new(),
        ImportResolution::Fallback => true,
        ImportResolution::NoImport => false,
    };

    let Some(candidates) = name_index.get(&call.target_name) else {
        return Vec::new();
    };

    if use_name_fallback {
        return scored(
            candidates.iter().take(8).cloned().collect(),
            CONFIDENCE_IMPORT_FALLBACK,
        );
    }

    if let Some(same_file) =
        resolve_same_file_call(call, candidates, source_index).filter(|matches| !matches.is_empty())
    {
        let confidence = if call.kind == CallKind::Bare {
            CONFIDENCE_SAME_FILE_BARE
        } else {
            CONFIDENCE_SAME_FILE_QUALIFIED
        };
        return scored(same_file, confidence);
    }

    if call.kind != CallKind::Bare {
        return Vec::new();
    }

    let confidence = if candidates.len() == 1 {
        CONFIDENCE_NAME_UNIQUE
    } else {
        CONFIDENCE_NAME_AMBIGUOUS
    };
    scored(candidates.iter().take(8).cloned().collect(), confidence)
}

fn scored(candidates: Vec<SymbolCandidate>, confidence: f64) -> Vec<ScoredTarget> {
    candidates
        .into_iter()
        .map(|candidate| ScoredTarget {
            candidate,
            confidence,
        })
        .collect()
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
    name_index: &HashMap<String, Vec<SymbolCandidate>>,
    import_index: &HashMap<String, Vec<Import>>,
    file_index: &HashSet<String>,
) -> ImportResolution {
    let Some(imports) = import_index.get(&call.source_file) else {
        return ImportResolution::NoImport;
    };
    let matched_import = imports.iter().find(|import| match call.kind {
        CallKind::Bare => import.local_name.as_deref() == Some(call.target_name.as_str()),
        CallKind::Member | CallKind::Scoped => call
            .qualifier
            .as_deref()
            .is_some_and(|qualifier| import_matches_qualifier(import, qualifier)),
    });
    let Some(matched_import) = matched_import else {
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
    let Some(candidates) = name_index.get(target_name) else {
        let module_targets = module_targets(&call.source_file, matched_import, call.kind);
        return unresolved_import_resolution(call, &module_targets, file_index);
    };
    let module_targets = module_targets(&call.source_file, matched_import, call.kind);
    let targets: Vec<SymbolCandidate> = candidates
        .iter()
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

fn unresolved_import_resolution(
    call: &PendingCall,
    module_targets: &ModuleTargets,
    file_index: &HashSet<String>,
) -> ImportResolution {
    if call.kind == CallKind::Bare || module_targets.is_external(file_index) {
        ImportResolution::Unresolved
    } else {
        ImportResolution::Fallback
    }
}

fn import_matches_qualifier(import: &Import, qualifier: &str) -> bool {
    import.local_name.as_deref() == Some(qualifier)
        || import.alias.as_deref() == Some(qualifier)
        || import.module == qualifier
}

#[derive(Default)]
struct ModuleTargets {
    files: Vec<String>,
    dirs: Vec<String>,
    relative: bool,
}

impl ModuleTargets {
    fn matches(&self, file_path: &str) -> bool {
        self.files.iter().any(|file| file == file_path)
            || self.dirs.iter().any(|dir| file_path.starts_with(dir))
    }

    fn has_indexed_match(&self, file_index: &HashSet<String>) -> bool {
        file_index.iter().any(|file| self.matches(file))
    }

    fn is_external(&self, file_index: &HashSet<String>) -> bool {
        !self.relative && !self.has_indexed_match(file_index)
    }

    fn finish(&mut self) {
        self.files.sort();
        self.files.dedup();
        self.dirs.sort();
        self.dirs.dedup();
    }
}

fn module_targets(source_file: &str, import: &Import, call_kind: CallKind) -> ModuleTargets {
    let mut targets = ModuleTargets::default();
    if source_file.ends_with(".py") {
        targets.relative = import.module.starts_with('.');
        if let Some(prefix) = python_module_prefix(source_file, import, call_kind) {
            push_module_variants(&mut targets.files, &prefix, &["py"]);
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
            push_module_variants(&mut targets.files, &prefix, &["ts", "tsx", "js", "jsx"]);
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

fn push_module_variants(files: &mut Vec<String>, prefix: &str, exts: &[&str]) {
    for ext in exts {
        files.push(format!("{prefix}.{ext}"));
        files.push(format!("{prefix}/index.{ext}"));
        if *ext == "py" {
            files.push(format!("{prefix}/__init__.py"));
        }
    }
}
