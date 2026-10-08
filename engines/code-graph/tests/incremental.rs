//! Integration tests for incremental sync: no-op when unchanged, rebuild on change, purge on delete.

use std::collections::BTreeSet;
use std::fs;
use std::path::Path;
use std::thread::sleep;
use std::time::Duration;

use graphtrail::extractors::common::symbol_id;
use graphtrail::extractors::{python, rust};
use graphtrail::query::doctor;
use graphtrail::store::{SCHEMA_VERSION, init_schema, meta, open_db, sync_repo};
use rusqlite::Connection;

#[test]
fn second_sync_is_noop_then_change_and_delete_are_detected() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    fs::write(root.join("a.py"), "def helper():\n    return 1\n").unwrap();
    fs::write(root.join("b.py"), "def run():\n    return 1\n").unwrap();

    let conn = open_db(&root.join("g.db")).unwrap();
    init_schema(&conn).unwrap();

    let first = sync_repo(&conn, root).unwrap();
    assert!(!first.unchanged);
    assert_eq!(first.files, 2);

    // Nothing changed -> no-op.
    let second = sync_repo(&conn, root).unwrap();
    assert!(second.unchanged, "second sync should be a no-op");
    assert_eq!(second.files, 2);

    // Modify a file (sleep so mtime advances at 1s resolution) -> rebuild.
    sleep(Duration::from_millis(1100));
    fs::write(
        root.join("a.py"),
        "def helper():\n    return 2\n\ndef extra():\n    return 9\n",
    )
    .unwrap();
    let third = sync_repo(&conn, root).unwrap();
    assert!(!third.unchanged, "modified file should trigger a rebuild");

    // Delete a file -> purge its rows and report it.
    fs::remove_file(root.join("b.py")).unwrap();
    let fourth = sync_repo(&conn, root).unwrap();
    assert!(!fourth.unchanged);
    assert_eq!(fourth.deleted, 1);
    let remaining: i64 = conn
        .query_row("SELECT COUNT(*) FROM files WHERE path = 'b.py'", [], |r| {
            r.get(0)
        })
        .unwrap();
    assert_eq!(remaining, 0, "deleted file rows should be purged");

    // And now it's a no-op again.
    let fifth = sync_repo(&conn, root).unwrap();
    assert!(fifth.unchanged);
}

#[test]
fn sync_disambiguates_same_named_javascript_functions_on_one_line() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    let source = "function duplicate() { first(); } function duplicate() { second(); }\n";
    write_file(root.join("bundle.js"), source);

    let conn = open_graph(root);
    let summary = sync_repo(&conn, root).unwrap();
    let mut statement = conn
        .prepare("SELECT id FROM symbols WHERE name = 'duplicate' ORDER BY start_line, id")
        .unwrap();
    let ids: Vec<String> = statement
        .query_map([], |row| row.get(0))
        .unwrap()
        .collect::<Result<_, _>>()
        .unwrap();
    let mut call_statement = conn
        .prepare(
            "SELECT target_name, source_id FROM pending_calls
             WHERE target_name IN ('first', 'second') ORDER BY line, target_name",
        )
        .unwrap();
    let calls: std::collections::HashMap<String, String> = call_statement
        .query_map([], |row| Ok((row.get(0)?, row.get(1)?)))
        .unwrap()
        .collect::<Result<_, _>>()
        .unwrap();
    let first_id = symbol_id("bundle.js", "duplicate", "function", 0);
    let second_id = symbol_id("bundle.js", "duplicate", "function", 1);

    assert_eq!(summary.symbols, 2);
    assert_eq!(ids.len(), 2);
    assert_ne!(ids[0], ids[1]);
    assert_eq!(calls["first"], first_id);
    assert_eq!(calls["second"], second_id);
}

#[test]
fn changed_sync_refreshes_synced_at() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    fs::write(root.join("a.py"), "def helper():\n    return 1\n").unwrap();

    let conn = open_db(&root.join("g.db")).unwrap();
    init_schema(&conn).unwrap();

    let first = sync_repo(&conn, root).unwrap();
    assert!(!first.unchanged);
    let first_synced_at: i64 = meta::read(&conn, "synced_at")
        .unwrap()
        .expect("synced_at after first sync")
        .parse()
        .unwrap();

    sleep(Duration::from_millis(1100));
    fs::write(
        root.join("a.py"),
        "def helper():\n    return 2\n\ndef extra():\n    return 9\n",
    )
    .unwrap();

    let second = sync_repo(&conn, root).unwrap();
    assert!(!second.unchanged);
    let second_synced_at: i64 = meta::read(&conn, "synced_at")
        .unwrap()
        .expect("synced_at after changed sync")
        .parse()
        .unwrap();

    assert!(second_synced_at > first_synced_at);
}

#[test]
fn unchanged_sync_refreshes_synced_at_without_reindexing_files() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    fs::write(root.join("a.py"), "def helper():\n    return 1\n").unwrap();

    let conn = open_db(&root.join("g.db")).unwrap();
    init_schema(&conn).unwrap();

    let first = sync_repo(&conn, root).unwrap();
    assert!(!first.unchanged);
    let first_synced_at: i64 = meta::read(&conn, "synced_at")
        .unwrap()
        .expect("synced_at after first sync")
        .parse()
        .unwrap();
    let first_indexed_at: i64 = conn
        .query_row(
            "SELECT indexed_at FROM files WHERE path = 'a.py'",
            [],
            |row| row.get(0),
        )
        .unwrap();

    sleep(Duration::from_millis(1100));

    let second = sync_repo(&conn, root).unwrap();
    assert!(second.unchanged);
    let second_synced_at: i64 = meta::read(&conn, "synced_at")
        .unwrap()
        .expect("synced_at after unchanged sync")
        .parse()
        .unwrap();
    let second_indexed_at: i64 = conn
        .query_row(
            "SELECT indexed_at FROM files WHERE path = 'a.py'",
            [],
            |row| row.get(0),
        )
        .unwrap();

    assert!(second_synced_at > first_synced_at);
    assert_eq!(second_indexed_at, first_indexed_at);
}

#[test]
fn old_extractor_fingerprint_reextracts_file_with_same_content() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    write_file(root.join("a.py"), "def helper():\n    return 1\n");

    let conn = open_graph(root);
    let first = sync_repo(&conn, root).unwrap();
    assert!(!first.unchanged);
    let first_indexed_at = indexed_at(&conn, "a.py");

    conn.execute(
        "UPDATE files SET extractor_fingerprint = 'python-old' WHERE path = 'a.py'",
        [],
    )
    .unwrap();
    sleep(Duration::from_millis(1100));

    let second = sync_repo(&conn, root).unwrap();

    assert!(!second.unchanged, "old extractor fingerprint is stale");
    assert!(indexed_at(&conn, "a.py") > first_indexed_at);
    assert_eq!(
        extractor_fingerprint(&conn, "a.py").as_deref(),
        Some(python::EXTRACTOR_FINGERPRINT)
    );
}

#[test]
fn only_doctored_language_row_is_reextracted() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    write_file(root.join("lib.rs"), "fn helper() {}\n");
    write_file(root.join("app.py"), "def helper():\n    return 1\n");

    let conn = open_graph(root);
    let first = sync_repo(&conn, root).unwrap();
    assert!(!first.unchanged);
    let rust_indexed_at = indexed_at(&conn, "lib.rs");
    let python_indexed_at = indexed_at(&conn, "app.py");

    conn.execute(
        "UPDATE files SET extractor_fingerprint = 'rust-old' WHERE path = 'lib.rs'",
        [],
    )
    .unwrap();
    sleep(Duration::from_millis(1100));

    let second = sync_repo(&conn, root).unwrap();

    assert!(!second.unchanged);
    assert!(indexed_at(&conn, "lib.rs") > rust_indexed_at);
    assert_eq!(indexed_at(&conn, "app.py"), python_indexed_at);
    assert_eq!(
        extractor_fingerprint(&conn, "lib.rs").as_deref(),
        Some(rust::EXTRACTOR_FINGERPRINT)
    );
    assert_eq!(
        extractor_fingerprint(&conn, "app.py").as_deref(),
        Some(python::EXTRACTOR_FINGERPRINT)
    );
}

#[test]
fn sync_migrates_v3_files_table_and_populates_extractor_fingerprint() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    write_file(root.join("a.py"), "def helper():\n    return 1\n");

    let conn = open_graph(root);
    let first = sync_repo(&conn, root).unwrap();
    assert!(!first.unchanged);
    conn.execute(
        "UPDATE meta SET value = '3' WHERE key = 'schema_version'",
        [],
    )
    .unwrap();
    conn.execute("ALTER TABLE files DROP COLUMN extractor_fingerprint", [])
        .unwrap();

    let second = sync_repo(&conn, root).unwrap();

    assert!(!second.unchanged);
    assert_eq!(
        meta::read(&conn, "schema_version").unwrap().as_deref(),
        Some(SCHEMA_VERSION.to_string().as_str())
    );
    assert_eq!(
        extractor_fingerprint(&conn, "a.py").as_deref(),
        Some(python::EXTRACTOR_FINGERPRINT)
    );
}

#[test]
fn sync_honors_root_gitignore_patterns() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    make_git_repo(root);
    write_file(root.join(".gitignore"), "vendor/\n*.gen.py\n");
    write_file(root.join("app.py"), "def kept():\n    return 1\n");
    write_file(
        root.join("vendor/pkg.py"),
        "def vendored():\n    return 1\n",
    );
    write_file(
        root.join("schema.gen.py"),
        "def generated():\n    return 1\n",
    );

    let conn = open_graph(root);
    let summary = sync_repo(&conn, root).unwrap();

    assert_eq!(summary.files, 1);
    assert_eq!(indexed_paths(&conn), paths(["app.py"]));
}

#[test]
fn sync_honors_nested_gitignore_patterns() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    make_git_repo(root);
    write_file(root.join("pkg/.gitignore"), "generated/\n");
    write_file(root.join("pkg/kept.py"), "def kept():\n    return 1\n");
    write_file(
        root.join("pkg/generated/noise.py"),
        "def ignored():\n    return 1\n",
    );

    let conn = open_graph(root);
    let summary = sync_repo(&conn, root).unwrap();

    assert_eq!(summary.files, 1);
    assert_eq!(indexed_paths(&conn), paths(["pkg/kept.py"]));
}

#[test]
fn non_git_root_ignores_only_the_hardcoded_floor() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    write_file(root.join(".gitignore"), "ignored.py\n");
    write_file(root.join("kept.py"), "def kept():\n    return 1\n");
    write_file(
        root.join("ignored.py"),
        "def still_indexed():\n    return 1\n",
    );
    write_file(
        root.join("vendor/pkg.py"),
        "def vendored():\n    return 1\n",
    );
    write_file(
        root.join("venv/lib/site.py"),
        "def skipped():\n    return 1\n",
    );
    write_file(
        root.join("node_modules/pkg/index.js"),
        "export function skipped() { return 1 }\n",
    );

    let conn = open_graph(root);
    let summary = sync_repo(&conn, root).unwrap();

    assert_eq!(summary.files, 3);
    assert_eq!(
        indexed_paths(&conn),
        paths(["ignored.py", "kept.py", "vendor/pkg.py"])
    );
}

#[test]
fn file_removed_by_new_gitignore_rule_self_cleans_from_db() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    make_git_repo(root);
    write_file(root.join("app.py"), "def kept():\n    return 1\n");
    write_file(
        root.join("vendor/pkg.py"),
        "def vendored():\n    return 1\n",
    );

    let conn = open_graph(root);
    let first = sync_repo(&conn, root).unwrap();
    assert_eq!(first.files, 2);
    assert_eq!(indexed_paths(&conn), paths(["app.py", "vendor/pkg.py"]));

    write_file(root.join(".gitignore"), "vendor/\n");
    let second = sync_repo(&conn, root).unwrap();

    assert!(!second.unchanged);
    assert_eq!(second.deleted, 1);
    assert_eq!(indexed_paths(&conn), paths(["app.py"]));
}

#[test]
fn nested_worktree_and_nested_clone_are_not_indexed() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    make_git_repo(root);
    write_file(root.join("app.py"), "def kept():\n    return 1\n");
    write_file(root.join("pkg/mod.py"), "def normal():\n    return 1\n");
    // Linked worktree: `.git` is a file.
    write_file(
        root.join(".worktrees/x/.git"),
        "gitdir: /elsewhere/.git/worktrees/x\n",
    );
    write_file(
        root.join(".worktrees/x/app.py"),
        "def kept():\n    return 1\n",
    );
    // Nested clone: `.git` is a directory.
    make_git_repo(&root.join("vendor/y"));
    write_file(
        root.join("vendor/y/lib.py"),
        "def vendored():\n    return 1\n",
    );
    // A directory below a worktree is covered by the same boundary.
    write_file(
        root.join(".worktrees/x/sub/deep.py"),
        "def deep():\n    return 1\n",
    );

    let conn = open_graph(root);
    let summary = sync_repo(&conn, root).unwrap();

    assert_eq!(summary.files, 2);
    assert_eq!(indexed_paths(&conn), paths(["app.py", "pkg/mod.py"]));
}

#[test]
fn nested_repo_boundary_applies_without_a_root_git_marker() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    write_file(root.join("app.py"), "def kept():\n    return 1\n");
    write_file(root.join("wt/.git"), "gitdir: /elsewhere\n");
    write_file(root.join("wt/app.py"), "def kept():\n    return 1\n");

    let conn = open_graph(root);
    sync_repo(&conn, root).unwrap();

    assert_eq!(indexed_paths(&conn), paths(["app.py"]));
}

#[test]
fn root_that_is_itself_a_linked_worktree_is_still_indexed() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    write_file(
        root.join(".git"),
        "gitdir: /elsewhere/.git/worktrees/root\n",
    );
    write_file(root.join("app.py"), "def kept():\n    return 1\n");
    write_file(root.join("pkg/mod.py"), "def normal():\n    return 1\n");

    let conn = open_graph(root);
    let summary = sync_repo(&conn, root).unwrap();

    assert_eq!(summary.files, 2);
    assert_eq!(indexed_paths(&conn), paths(["app.py", "pkg/mod.py"]));
}

#[test]
fn existing_nested_repo_rows_are_dropped_on_next_sync() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    make_git_repo(root);
    write_file(root.join("app.py"), "def kept():\n    return 1\n");
    write_file(
        root.join(".worktrees/x/app.py"),
        "def kept():\n    return 1\n",
    );
    write_file(
        root.join(".worktrees/x/extra.py"),
        "def extra():\n    return 1\n",
    );

    let conn = open_graph(root);
    let first = sync_repo(&conn, root).unwrap();
    assert_eq!(first.files, 3, "no .git marker yet, so the copy is indexed");

    // The directory becomes a worktree, as when `git worktree add` lands on a
    // path an older engine already walked.
    write_file(
        root.join(".worktrees/x/.git"),
        "gitdir: /elsewhere/.git/worktrees/x\n",
    );
    let second = sync_repo(&conn, root).unwrap();

    assert!(!second.unchanged);
    assert_eq!(second.deleted, 2);
    assert_eq!(indexed_paths(&conn), paths(["app.py"]));
    let orphans: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM symbols WHERE file_path LIKE '.worktrees/%'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(orphans, 0, "symbols of dropped files are purged too");
}

#[test]
fn doctor_reports_nested_repo_skip_count() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    make_git_repo(root);
    write_file(root.join("app.py"), "def kept():\n    return 1\n");
    write_file(root.join(".worktrees/x/.git"), "gitdir: /elsewhere\n");
    write_file(
        root.join(".worktrees/x/app.py"),
        "def kept():\n    return 1\n",
    );
    make_git_repo(&root.join("vendor/y"));
    write_file(
        root.join("vendor/y/lib.py"),
        "def vendored():\n    return 1\n",
    );

    let conn = open_graph(root);
    sync_repo(&conn, root).unwrap();
    let report = doctor::doctor(&conn, root, &root.join("g.db")).unwrap();

    assert_eq!(report.ignored.nested_repo, 2);
    assert_eq!(report.pending.deleted_files, 0);
}

#[test]
fn first_graphtrail_index_in_git_repo_adds_root_gitignore_entry() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    make_git_repo(root);
    write_file(root.join("app.py"), "def kept():\n    return 1\n");

    let conn = open_graphtrail_graph(root);
    let summary = sync_repo(&conn, root).unwrap();

    assert!(!summary.unchanged);
    assert_eq!(
        fs::read_to_string(root.join(".gitignore")).unwrap(),
        ".graphtrail/\n"
    );
}

#[test]
fn second_graphtrail_sync_does_not_duplicate_gitignore_entry() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    make_git_repo(root);
    write_file(root.join("app.py"), "def kept():\n    return 1\n");

    let conn = open_graphtrail_graph(root);
    sync_repo(&conn, root).unwrap();
    sync_repo(&conn, root).unwrap();

    assert_eq!(
        fs::read_to_string(root.join(".gitignore")).unwrap(),
        ".graphtrail/\n"
    );
}

#[test]
fn first_graphtrail_index_respects_covering_gitignore_pattern() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    make_git_repo(root);
    write_file(root.join(".gitignore"), ".graphtrail*\n");
    write_file(root.join("app.py"), "def kept():\n    return 1\n");

    let conn = open_graphtrail_graph(root);
    sync_repo(&conn, root).unwrap();

    assert_eq!(
        fs::read_to_string(root.join(".gitignore")).unwrap(),
        ".graphtrail*\n"
    );
}

#[test]
fn first_graphtrail_index_in_non_git_root_does_not_write_gitignore() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    write_file(root.join("app.py"), "def kept():\n    return 1\n");

    let conn = open_graphtrail_graph(root);
    sync_repo(&conn, root).unwrap();

    assert!(!root.join(".gitignore").exists());
}

#[test]
fn preexisting_graphtrail_dir_does_not_write_gitignore() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    make_git_repo(root);
    fs::create_dir(root.join(".graphtrail")).unwrap();
    write_file(root.join("app.py"), "def kept():\n    return 1\n");

    let conn = open_graphtrail_graph(root);
    sync_repo(&conn, root).unwrap();

    assert!(!root.join(".gitignore").exists());
}

#[test]
fn hidden_paths_are_indexed() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    make_git_repo(root);
    write_file(
        root.join(".github/workflows/check.py"),
        "def hidden_workflow():\n    return 1\n",
    );
    write_file(
        root.join(".hidden.py"),
        "def hidden_file():\n    return 1\n",
    );

    let conn = open_graph(root);
    let summary = sync_repo(&conn, root).unwrap();

    assert_eq!(summary.files, 2);
    assert_eq!(
        indexed_paths(&conn),
        paths([".github/workflows/check.py", ".hidden.py"])
    );
}

#[test]
fn new_definition_resolves_calls_from_unchanged_files() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    write_file(root.join("b.py"), "def run():\n    helper()\n");

    let conn = open_graph(root);
    let first = sync_repo(&conn, root).unwrap();
    assert!(!first.unchanged);
    assert_eq!(edge_count(&conn), 0, "helper is undefined, no edges yet");
    let b_indexed_at = indexed_at(&conn, "b.py");

    // Define helper in a new file; b.py itself does not change.
    sleep(Duration::from_millis(1100));
    write_file(root.join("a.py"), "def helper():\n    return 1\n");
    let second = sync_repo(&conn, root).unwrap();

    assert!(!second.unchanged);
    assert_eq!(
        indexed_at(&conn, "b.py"),
        b_indexed_at,
        "unchanged file must not be re-extracted"
    );
    assert_eq!(
        edge_names(&conn),
        vec![("run".to_string(), "helper".to_string())],
        "call in the unchanged file must resolve to the new definition"
    );

    // Remove the definition again; the derived edge must disappear, not linger.
    sleep(Duration::from_millis(1100));
    write_file(root.join("a.py"), "def helper2():\n    return 1\n");
    let third = sync_repo(&conn, root).unwrap();

    assert!(!third.unchanged);
    assert_eq!(indexed_at(&conn, "b.py"), b_indexed_at);
    assert_eq!(
        edge_count(&conn),
        0,
        "stale resolution must be dropped when its target goes away"
    );
}

#[test]
fn sync_upgrades_v4_schema_and_repopulates_pending_calls() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    write_file(root.join("a.py"), "def helper():\n    return 1\n");
    write_file(root.join("b.py"), "def run():\n    helper()\n");

    let conn = open_graph(root);
    let first = sync_repo(&conn, root).unwrap();
    assert!(!first.unchanged);
    assert_eq!(edge_names(&conn), vec![("run".into(), "helper".into())]);

    // Simulate a v4 database: no pending_calls table, stored version 4.
    conn.execute("DROP TABLE pending_calls", []).unwrap();
    conn.execute(
        "UPDATE meta SET value = '4' WHERE key = 'schema_version'",
        [],
    )
    .unwrap();

    let second = sync_repo(&conn, root).unwrap();

    assert!(!second.unchanged, "v4 database must trigger a reindex");
    assert_eq!(
        meta::read(&conn, "schema_version").unwrap().as_deref(),
        Some(SCHEMA_VERSION.to_string().as_str())
    );
    let pending: i64 = conn
        .query_row("SELECT COUNT(*) FROM pending_calls", [], |row| row.get(0))
        .unwrap();
    assert!(pending > 0, "reindex must repopulate pending_calls");
    assert_eq!(edge_names(&conn), vec![("run".into(), "helper".into())]);
}

#[test]
fn sync_upgrades_v5_schema_by_rebuilding_edges_without_reparsing() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    write_file(root.join("a.py"), "def helper():\n    return 1\n");
    write_file(root.join("b.py"), "def run():\n    helper()\n");

    let conn = open_graph(root);
    let first = sync_repo(&conn, root).unwrap();
    assert!(!first.unchanged);
    let first_indexed_at = indexed_at(&conn, "a.py");

    // Simulate a v5 database: edges lack the confidence column.
    conn.execute("ALTER TABLE edges DROP COLUMN confidence", [])
        .unwrap();
    conn.execute(
        "UPDATE meta SET value = '5' WHERE key = 'schema_version'",
        [],
    )
    .unwrap();

    let second = sync_repo(&conn, root).unwrap();

    assert!(!second.unchanged, "v5 database must rebuild edges");
    assert_eq!(
        indexed_at(&conn, "a.py"),
        first_indexed_at,
        "edge-only upgrade must not re-parse files"
    );
    assert_eq!(
        meta::read(&conn, "schema_version").unwrap().as_deref(),
        Some(SCHEMA_VERSION.to_string().as_str())
    );
    let unscored: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM edges WHERE confidence IS NULL",
            [],
            |row| row.get(0),
        )
        .unwrap();
    let total: i64 = conn
        .query_row("SELECT COUNT(*) FROM edges", [], |row| row.get(0))
        .unwrap();
    assert!(total > 0);
    assert_eq!(unscored, 0, "rebuilt edges must carry confidence");
}

#[test]
fn doctor_reports_branch_drift_as_stale() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    make_git_repo(root);
    write_file(root.join("a.py"), "def helper():\n    return 1\n");

    let conn = open_graph(root);
    sync_repo(&conn, root).unwrap();
    assert_eq!(
        meta::read(&conn, "synced_branch").unwrap().as_deref(),
        Some("main")
    );

    let fresh = doctor(&conn, root, &root.join("g.db")).unwrap();
    assert_eq!(fresh.verdict, "FRESH");
    assert!(!fresh.branch.drifted);

    // Same files on disk, different checked-out branch: the graph describes
    // the other branch, so doctor must not call it fresh.
    fs::write(root.join(".git/HEAD"), "ref: refs/heads/feature-x\n").unwrap();
    let drifted = doctor(&conn, root, &root.join("g.db")).unwrap();

    assert_eq!(drifted.verdict, "STALE");
    assert!(drifted.branch.drifted);
    assert_eq!(drifted.branch.synced.as_deref(), Some("main"));
    assert_eq!(drifted.branch.current.as_deref(), Some("feature-x"));

    // Re-syncing on the new branch clears the drift.
    sync_repo(&conn, root).unwrap();
    let resynced = doctor(&conn, root, &root.join("g.db")).unwrap();
    assert_eq!(resynced.verdict, "FRESH");
    assert!(!resynced.branch.drifted);
}

#[test]
fn sync_fails_fast_when_lock_is_held() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    write_file(root.join("a.py"), "def helper():\n    return 1\n");

    let conn = open_graph(root);
    let lock_file = root.join("g.db.lock");
    fs::write(&lock_file, format!("{}\n", std::process::id())).unwrap();

    let err = sync_repo(&conn, root).unwrap_err();
    assert!(
        err.to_string().contains("another sync is already running"),
        "unexpected error: {err}"
    );

    fs::remove_file(&lock_file).unwrap();
    let summary = sync_repo(&conn, root).unwrap();
    assert!(!summary.unchanged);
    assert!(
        !lock_file.exists(),
        "sync must release the lock when it finishes"
    );
}

#[cfg(unix)]
#[test]
fn sync_reclaims_lock_from_dead_process() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    write_file(root.join("a.py"), "def helper():\n    return 1\n");

    let conn = open_graph(root);
    // PIDs are capped far below this on Linux, so this owner cannot be alive.
    fs::write(root.join("g.db.lock"), "999999999\n").unwrap();

    let summary = sync_repo(&conn, root).unwrap();
    assert!(!summary.unchanged);
    assert!(!root.join("g.db.lock").exists());
}

fn make_git_repo(root: &Path) {
    fs::create_dir_all(root.join(".git")).unwrap();
    fs::write(root.join(".git/HEAD"), "ref: refs/heads/main\n").unwrap();
}

fn open_graph(root: &Path) -> Connection {
    let conn = open_db(&root.join("g.db")).unwrap();
    init_schema(&conn).unwrap();
    conn
}

fn open_graphtrail_graph(root: &Path) -> Connection {
    let conn = open_db(&root.join(".graphtrail").join("graphtrail.db")).unwrap();
    init_schema(&conn).unwrap();
    conn
}

fn write_file(path: impl AsRef<Path>, content: &str) {
    let path = path.as_ref();
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).unwrap();
    }
    fs::write(path, content).unwrap();
}

fn indexed_paths(conn: &Connection) -> BTreeSet<String> {
    let mut stmt = conn
        .prepare("SELECT path FROM files ORDER BY path")
        .unwrap();
    stmt.query_map([], |row| row.get::<_, String>(0))
        .unwrap()
        .map(|row| row.unwrap())
        .collect()
}

fn indexed_at(conn: &Connection, path: &str) -> i64 {
    conn.query_row(
        "SELECT indexed_at FROM files WHERE path = ?1",
        [path],
        |row| row.get(0),
    )
    .unwrap()
}

fn edge_count(conn: &Connection) -> i64 {
    conn.query_row("SELECT COUNT(*) FROM edges", [], |row| row.get(0))
        .unwrap()
}

/// (source name, target name) pairs for every call edge, ordered.
fn edge_names(conn: &Connection) -> Vec<(String, String)> {
    let mut stmt = conn
        .prepare(
            "SELECT src.name, dst.name FROM edges e
             JOIN symbols src ON src.id = e.source
             JOIN symbols dst ON dst.id = e.target
             ORDER BY src.name, dst.name",
        )
        .unwrap();
    stmt.query_map([], |row| Ok((row.get(0)?, row.get(1)?)))
        .unwrap()
        .map(|row| row.unwrap())
        .collect()
}

fn extractor_fingerprint(conn: &Connection, path: &str) -> Option<String> {
    conn.query_row(
        "SELECT extractor_fingerprint FROM files WHERE path = ?1",
        [path],
        |row| row.get(0),
    )
    .unwrap()
}

fn paths<const N: usize>(paths: [&str; N]) -> BTreeSet<String> {
    paths.into_iter().map(str::to_owned).collect()
}

// --- Incremental sync must equal a cold rebuild ---------------------------------

/// Every table sync writes, as sorted rows of SQL `quote()`d columns. Timestamps
/// (`modified_at`, `indexed_at`) and autoincrement ids (`imports.id`,
/// `pending_calls.id`) are legitimately nondeterministic, so they are left out.
/// Rows stay a sorted multiset, so a duplicated row is a difference.
const SNAPSHOT_TABLES: [(&str, &str, &str); 8] = [
    (
        "files",
        "path, content_hash, size, language, extractor_fingerprint",
        "files",
    ),
    (
        "symbols",
        "id, kind, name, qualified_name, file_path, start_line, end_line, signature, container, content_hash, body_hash",
        "symbols",
    ),
    (
        "imports",
        "file_path, module, local_name, imported_name, alias, line, module_scope, conditional",
        "imports",
    ),
    (
        "pending_calls",
        "source_id, file_path, target_name, kind, qualifier, line",
        "pending_calls",
    ),
    ("edges", "source, target, kind, line, confidence", "edges"),
    (
        "conditional_symbols",
        "symbol_id, file_path",
        "conditional_symbols",
    ),
    ("module_exports", "file_path, names", "module_exports"),
    (
        "symbols_fts",
        "symbol_id, name, qualified_name, signature, file_path",
        "symbols_fts",
    ),
];

type Snapshot = Vec<(&'static str, Vec<String>)>;

fn snapshot(conn: &Connection) -> Snapshot {
    SNAPSHOT_TABLES
        .iter()
        .map(|(label, columns, table)| {
            let expression = columns
                .split(", ")
                .map(|column| format!("quote({column})"))
                .collect::<Vec<_>>()
                .join(" || '|' || ");
            let mut statement = conn
                .prepare(&format!("SELECT {expression} FROM {table}"))
                .unwrap();
            let mut rows: Vec<String> = statement
                .query_map([], |row| row.get(0))
                .unwrap()
                .map(|row| row.unwrap())
                .collect();
            rows.sort();
            (*label, rows)
        })
        .collect()
}

/// A readable report of every row present on one side only, or empty when equal.
fn snapshot_diff(incremental: &Snapshot, cold: &Snapshot) -> String {
    let mut report = String::new();
    for ((label, left), (_, right)) in incremental.iter().zip(cold) {
        if left == right {
            continue;
        }
        let mut counts: std::collections::BTreeMap<&str, i64> = Default::default();
        for row in left {
            *counts.entry(row).or_default() += 1;
        }
        for row in right {
            *counts.entry(row).or_default() -= 1;
        }
        report.push_str(&format!("[{label}]\n"));
        for (row, count) in counts.into_iter().filter(|(_, count)| *count != 0) {
            let side = if count > 0 {
                "incremental only"
            } else {
                "cold only"
            };
            report.push_str(&format!("  {side} x{}: {row}\n", count.abs()));
        }
    }
    report
}

enum Op {
    Write(&'static str, String),
    Remove(&'static str),
    Rename(&'static str, &'static str),
}

/// A tree that is edited step by step. Every write moves the file's mtime to a
/// fresh value, so change detection never depends on wall-clock sleeps.
struct EditedTree {
    root: std::path::PathBuf,
    clock: u64,
}

impl EditedTree {
    fn apply(&mut self, op: &Op) {
        match op {
            Op::Write(path, content) => {
                write_file(self.root.join(path), content);
                self.clock += 10;
                let file = fs::File::options()
                    .write(true)
                    .open(self.root.join(path))
                    .unwrap();
                file.set_modified(
                    std::time::UNIX_EPOCH + Duration::from_secs(1_700_000_000 + self.clock),
                )
                .unwrap();
            }
            Op::Remove(path) => fs::remove_file(self.root.join(path)).unwrap(),
            Op::Rename(from, to) => {
                if let Some(parent) = self.root.join(to).parent() {
                    fs::create_dir_all(parent).unwrap();
                }
                fs::rename(self.root.join(from), self.root.join(to)).unwrap();
            }
        }
    }
}

fn copy_tree(from: &Path, to: &Path) {
    fs::create_dir_all(to).unwrap();
    for entry in fs::read_dir(from).unwrap() {
        let entry = entry.unwrap();
        let target = to.join(entry.file_name());
        if entry.file_type().unwrap().is_dir() {
            copy_tree(&entry.path(), &target);
        } else {
            fs::copy(entry.path(), target).unwrap();
        }
    }
}

/// Sync `root` into a brand-new database and return what it holds.
fn cold_snapshot(root: &Path) -> Snapshot {
    let dir = tempfile::tempdir().unwrap();
    let conn = open_graph(dir.path());
    sync_repo(&conn, root).unwrap();
    snapshot(&conn)
}

fn assert_incremental_equals_cold(conn: &Connection, root: &Path, step: &str) {
    let incremental = snapshot(conn);
    let diff = snapshot_diff(&incremental, &cold_snapshot(root));
    assert!(
        diff.is_empty(),
        "incremental graph differs from a cold rebuild after step {step:?}:\n{diff}"
    );
}

#[test]
fn incremental_sync_matches_cold_rebuild() {
    let fixture = Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/golden/mixed");
    let tree_dir = tempfile::tempdir().unwrap();
    let db_dir = tempfile::tempdir().unwrap();
    let root = tree_dir.path();
    copy_tree(&fixture, root);
    make_git_repo(root);

    let conn = open_graph(db_dir.path());
    sync_repo(&conn, root).unwrap();
    assert!(
        edge_count(&conn) > 0,
        "the baseline fixture must resolve calls"
    );
    assert_incremental_equals_cold(&conn, root, "baseline");

    let mut tree = EditedTree {
        root: root.to_path_buf(),
        clock: 0,
    };
    let steps: Vec<(&str, Vec<Op>)> = vec![
        (
            "add files in two languages",
            vec![
                Op::Write(
                    "python/pkg/commands/extra.py",
                    "from .handoff_cmd import lint\n\n\ndef extra_task():\n    return lint()\n".into(),
                ),
                Op::Write("ts/src/extra.ts", "export function extra() {\n  return 1;\n}\n".into()),
            ],
        ),
        (
            "modify callers and callees",
            vec![
                Op::Write(
                    "python/pkg/commands/entry.py",
                    "from pkg.commands import lint\nfrom .sub.sibling import *\nfrom .extra import extra_task\n\n\ndef tag(name):\n    return lambda fn: fn\n\n\n@tag(\"cli\")\ndef cli():\n    extra_task()\n    return lint()\n\n\nif __name__ == \"__main__\":\n    cli()\n    func()\n".into(),
                ),
                Op::Write(
                    "go/pkg/pkg.go",
                    "package pkg\n\nfunc Func() {}\n\nfunc Other() { Func() }\n".into(),
                ),
            ],
        ),
        (
            "rename a function whose caller is in an unchanged file",
            vec![Op::Write(
                "ts/src/util.ts",
                "export function parseAll() {\n  return 1;\n}\n".into(),
            )],
        ),
        (
            "add a definition that resolves a call in an unchanged file",
            vec![Op::Write(
                "go/builder.go",
                "package main\n\nfunc build() {}\n".into(),
            )],
        ),
        (
            "delete a file that other files call into",
            vec![Op::Remove("src/factory.rs")],
        ),
        (
            "move the import target of unchanged callers to a new module",
            vec![
                Op::Write(
                    "python/pkg/commands/lint_impl.py",
                    "def lint():\n    return 1\n".into(),
                ),
                Op::Write(
                    "python/pkg/commands/__init__.py",
                    "from .lint_impl import lint\n".into(),
                ),
                Op::Write(
                    "python/pkg/commands/handoff_cmd.py",
                    "def other():\n    return 1\n".into(),
                ),
            ],
        ),
        (
            "rename a module that is imported relatively",
            vec![Op::Rename(
                "python/pkg/commands/sub/sibling.py",
                "python/pkg/commands/sub/sibling_renamed.py",
            )],
        ),
        (
            "edit a Rust file so a call in another file loses its target",
            vec![Op::Write(
                "src/m.rs",
                "pub fn f() {}\n\npub fn g2() {}\n".into(),
            )],
        ),
        (
            "add a gitignore rule that hides an indexed file",
            vec![Op::Write(".gitignore", "ts/src/extra.ts\n".into())],
        ),
        (
            "remove the gitignore rule again",
            vec![Op::Write(".gitignore", String::new())],
        ),
    ];
    for (step, ops) in steps {
        for op in &ops {
            tree.apply(op);
        }
        let summary = sync_repo(&conn, root).unwrap();
        assert!(!summary.unchanged, "step {step:?} must be noticed by sync");
        assert_incremental_equals_cold(&conn, root, step);
    }
}

#[test]
fn incremental_sync_matches_cold_rebuild_over_seeded_random_edits() {
    // Small name pool so calls collide and resolve differently as files come and go.
    struct Lcg(u64);
    impl Lcg {
        fn next(&mut self, bound: u64) -> u64 {
            self.0 = self
                .0
                .wrapping_mul(6364136223846793005)
                .wrapping_add(1442695040888963407);
            (self.0 >> 33) % bound
        }
    }
    const FILES: [&str; 8] = [
        "m0.py",
        "m1.py",
        "pkg/m2.py",
        "t0.ts",
        "t1.ts",
        "g0.go",
        "g1.go",
        "r0.rs",
    ];
    fn content(rng: &mut Lcg, file: &str) -> String {
        let (a, b, c) = (rng.next(6), rng.next(6), rng.next(6));
        let other = FILES[rng.next(FILES.len() as u64) as usize];
        let stem = other.rsplit_once('.').unwrap().0.replace('/', ".");
        match file.rsplit_once('.').unwrap().1 {
            "py" => format!(
                "from {stem} import f{c}\n\n\ndef f{a}():\n    f{b}()\n    f{c}()\n\n\ndef f{b}():\n    f{a}()\n"
            ),
            "ts" => format!(
                "import {{ f{c} }} from './{stem}';\nexport function f{a}() {{ f{b}(); f{c}(); }}\n"
            ),
            "go" => {
                format!("package main\n\nfunc f{a}() {{ f{b}() }}\n\nfunc f{c}() {{ f{a}() }}\n")
            }
            _ => format!("pub fn f{a}() {{ f{b}(); }}\n\npub fn f{c}() {{ f{a}(); }}\n"),
        }
    }

    let tree_dir = tempfile::tempdir().unwrap();
    let db_dir = tempfile::tempdir().unwrap();
    let root = tree_dir.path();
    let conn = open_graph(db_dir.path());
    let mut tree = EditedTree {
        root: root.to_path_buf(),
        clock: 0,
    };
    let mut rng = Lcg(0x1649);
    let mut live: BTreeSet<&str> = BTreeSet::new();

    for step in 0..24 {
        let file = FILES[rng.next(FILES.len() as u64) as usize];
        let kind = rng.next(10);
        let (label, op) = if !live.contains(file) || kind < 5 {
            live.insert(file);
            ("write", Op::Write(file, content(&mut rng, file)))
        } else if kind < 8 {
            live.remove(file);
            ("delete", Op::Remove(file))
        } else {
            let target = FILES[rng.next(FILES.len() as u64) as usize];
            if live.contains(target) {
                continue;
            }
            live.remove(file);
            live.insert(target);
            ("rename", Op::Rename(file, target))
        };
        tree.apply(&op);
        sync_repo(&conn, root).unwrap();
        assert_incremental_equals_cold(&conn, root, &format!("random step {step}: {label} {file}"));
    }
}
