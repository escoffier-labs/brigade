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

#[test]
fn unreadable_file_is_skipped_while_valid_new_file_indexes() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    write_file(root.join("pkg/core.py"), "def helper():\n    return 1\n");
    let conn = open_graph(root);
    sync_repo(&conn, root).unwrap();

    write_file(root.join("pkg/new.py"), "def newfn():\n    return 2\n");
    write_bytes(root.join("pkg/bad.py"), b"x = 1\n\xff\xfe bad\n");

    let summary = sync_repo(&conn, root).expect("one unreadable file must not abort sync");

    assert!(!summary.unchanged);
    assert_eq!(summary.skipped, 1);
    assert_eq!(indexed_paths(&conn), paths(["pkg/core.py", "pkg/new.py"]));
    assert_eq!(symbol_count(&conn, "newfn"), 1);
    assert_eq!(
        skipped_rows(&conn),
        vec![("pkg/bad.py".to_string(), "unreadable_utf8".to_string())]
    );

    let report = doctor(&conn, root, &root.join("g.db")).unwrap();
    assert_eq!(
        report.verdict, "FRESH",
        "a skipped file alone must not pin doctor to STALE"
    );
    assert!(report.pending.is_empty(), "{:?}", report.pending);
    assert_eq!(report.skipped.count, 1);
    assert_eq!(report.skipped.sample[0].path, "pkg/bad.py");
    assert_eq!(report.skipped.sample[0].reason, "unreadable_utf8");
    assert!(!report.warnings.is_empty());

    let again = sync_repo(&conn, root).unwrap();
    assert!(
        again.unchanged,
        "an unchanged skipped file must not force a resync"
    );
    assert_eq!(again.skipped, 1);
}

#[test]
fn fixing_a_skipped_file_indexes_it_and_clears_the_entry() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    write_file(root.join("core.py"), "def helper():\n    return 1\n");
    write_bytes(root.join("bad.py"), b"x = 1\n\xff\xfe bad\n");
    let conn = open_graph(root);
    let first = sync_repo(&conn, root).unwrap();
    assert_eq!(first.skipped, 1);

    write_file(root.join("bad.py"), "def repaired():\n    return 3\n");
    let second = sync_repo(&conn, root).unwrap();

    assert!(!second.unchanged);
    assert_eq!(second.skipped, 0);
    assert_eq!(indexed_paths(&conn), paths(["bad.py", "core.py"]));
    assert_eq!(symbol_count(&conn, "repaired"), 1);
    assert!(skipped_rows(&conn).is_empty());
    let report = doctor(&conn, root, &root.join("g.db")).unwrap();
    assert_eq!(report.verdict, "FRESH");
    assert_eq!(report.skipped.count, 0);
    assert!(report.warnings.is_empty());
}

#[test]
fn deleting_a_skipped_file_drops_its_entry() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    write_file(root.join("core.py"), "def helper():\n    return 1\n");
    write_bytes(root.join("bad.py"), b"\xff\xfe\n");
    let conn = open_graph(root);
    sync_repo(&conn, root).unwrap();
    assert_eq!(skipped_rows(&conn).len(), 1);

    fs::remove_file(root.join("bad.py")).unwrap();
    let summary = sync_repo(&conn, root).unwrap();

    assert_eq!(summary.skipped, 0);
    assert!(skipped_rows(&conn).is_empty());
    let report = doctor(&conn, root, &root.join("g.db")).unwrap();
    assert_eq!(report.verdict, "FRESH");
    assert_eq!(report.skipped.count, 0);
}

#[test]
fn indexed_file_that_turns_unreadable_loses_its_stale_rows() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    write_file(root.join("core.py"), "def helper():\n    return 1\n");
    write_file(root.join("mod.py"), "def old_symbol():\n    return 1\n");
    let conn = open_graph(root);
    sync_repo(&conn, root).unwrap();
    assert_eq!(symbol_count(&conn, "old_symbol"), 1);

    write_bytes(
        root.join("mod.py"),
        b"def old_symbol():\n    return '\xff'\n",
    );
    let summary = sync_repo(&conn, root).unwrap();

    assert_eq!(summary.skipped, 1);
    assert_eq!(indexed_paths(&conn), paths(["core.py"]));
    assert_eq!(symbol_count(&conn, "old_symbol"), 0);
    assert_eq!(
        skipped_rows(&conn),
        vec![("mod.py".to_string(), "unreadable_utf8".to_string())]
    );
}

#[test]
fn python_syntax_error_is_indexed_and_counted_as_parse_error() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    write_file(root.join("ok.py"), "def fine():\n    return 1\n");
    write_file(
        root.join("broken.py"),
        "def good():\n    return 1\n\ndef broken(:\n    pass\n",
    );
    let conn = open_graph(root);
    sync_repo(&conn, root).unwrap();

    assert_eq!(indexed_paths(&conn), paths(["broken.py", "ok.py"]));
    assert_eq!(symbol_count(&conn, "good"), 1);
    let report = doctor(&conn, root, &root.join("g.db")).unwrap();
    assert_eq!(report.verdict, "FRESH");
    assert_eq!(report.parse_errors.count, 1);
    assert_eq!(report.parse_errors.sample, vec!["broken.py".to_string()]);
    assert!(!report.warnings.is_empty());

    write_file(root.join("broken.py"), "def good():\n    return 1\n");
    sync_repo(&conn, root).unwrap();
    let repaired = doctor(&conn, root, &root.join("g.db")).unwrap();
    assert_eq!(repaired.parse_errors.count, 0);
    assert!(repaired.warnings.is_empty());
}

#[test]
fn sync_upgrades_v7_schema_with_skip_and_parse_error_tracking() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    write_file(
        root.join("broken.py"),
        "def good():\n    return 1\n\ndef broken(:\n    pass\n",
    );
    let conn = open_graph(root);
    sync_repo(&conn, root).unwrap();

    // Simulate a v7 database: no parse_errors column, no skipped_files table.
    conn.execute("ALTER TABLE files DROP COLUMN parse_errors", [])
        .unwrap();
    conn.execute("DROP TABLE skipped_files", []).unwrap();
    conn.execute(
        "UPDATE meta SET value = '7' WHERE key = 'schema_version'",
        [],
    )
    .unwrap();
    let stale = doctor(&conn, root, &root.join("g.db")).unwrap();
    assert_eq!(stale.verdict, "NEEDS-MIGRATION");
    assert_eq!(stale.skipped.count, 0);
    assert_eq!(stale.parse_errors.count, 0);

    write_bytes(root.join("bad.py"), b"\xff\n");
    let summary = sync_repo(&conn, root).unwrap();

    assert!(!summary.unchanged, "v7 database must reindex once");
    assert_eq!(summary.skipped, 1);
    assert_eq!(
        meta::read(&conn, "schema_version").unwrap().as_deref(),
        Some(SCHEMA_VERSION.to_string().as_str())
    );
    let report = doctor(&conn, root, &root.join("g.db")).unwrap();
    assert_eq!(report.verdict, "FRESH");
    assert_eq!(
        report.parse_errors.count, 1,
        "reindex must census parse errors"
    );
    assert_eq!(report.skipped.count, 1);
}

#[test]
fn failed_v8_upgrade_sync_keeps_the_parse_census_obligation() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    write_file(
        root.join("broken.py"),
        "def good():\n    return 1\n\ndef broken(:\n    pass\n",
    );
    let conn = open_graph(root);
    sync_repo(&conn, root).unwrap();

    // Simulate a v7 database.
    conn.execute("ALTER TABLE files DROP COLUMN parse_errors", [])
        .unwrap();
    conn.execute("DROP TABLE skipped_files", []).unwrap();
    conn.execute(
        "UPDATE meta SET value = '7' WHERE key = 'schema_version'",
        [],
    )
    .unwrap();

    // The first upgrade sync fails after the schema change, inside the
    // reindex transaction.
    conn.execute_batch(
        "CREATE TRIGGER abort_upgrade BEFORE INSERT ON files
         BEGIN SELECT RAISE(ABORT, 'simulated failure'); END;",
    )
    .unwrap();
    assert!(sync_repo(&conn, root).is_err());
    conn.execute("DROP TRIGGER abort_upgrade", []).unwrap();

    let retry = sync_repo(&conn, root).unwrap();

    assert!(
        !retry.unchanged,
        "the retry must still run the one-time reindex"
    );
    let report = doctor(&conn, root, &root.join("g.db")).unwrap();
    assert_eq!(report.verdict, "FRESH");
    assert_eq!(report.parse_errors.count, 1);
    assert_eq!(report.parse_errors.sample, vec!["broken.py".to_string()]);

    let after = sync_repo(&conn, root).unwrap();
    assert!(after.unchanged, "the census obligation clears once it runs");
}

#[cfg(unix)]
#[test]
fn io_error_skip_is_retried_once_the_file_is_readable_again() {
    use std::os::unix::fs::PermissionsExt;

    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    write_file(root.join("core.py"), "def helper():\n    return 1\n");
    write_file(root.join("locked.py"), "def locked_fn():\n    return 2\n");
    let locked = root.join("locked.py");
    fs::set_permissions(&locked, fs::Permissions::from_mode(0o000)).unwrap();
    if fs::read(&locked).is_ok() {
        // Running as root (or on a filesystem without permission checks):
        // the failure cannot be reproduced.
        fs::set_permissions(&locked, fs::Permissions::from_mode(0o644)).unwrap();
        return;
    }
    let conn = open_graph(root);

    let first = sync_repo(&conn, root).unwrap();
    assert_eq!(first.skipped, 1);
    assert_eq!(
        skipped_rows(&conn),
        vec![("locked.py".to_string(), "io_error".to_string())]
    );
    let still_locked = sync_repo(&conn, root).unwrap();
    assert!(
        still_locked.unchanged,
        "a file that still cannot be read must not churn every sync"
    );
    let report = doctor(&conn, root, &root.join("g.db")).unwrap();
    assert_eq!(report.verdict, "FRESH");

    // chmod changes neither size nor mtime.
    fs::set_permissions(&locked, fs::Permissions::from_mode(0o644)).unwrap();
    let readable = doctor(&conn, root, &root.join("g.db")).unwrap();
    assert_eq!(readable.verdict, "STALE");
    assert_eq!(readable.pending.new_files, 1);

    let retried = sync_repo(&conn, root).unwrap();

    assert!(!retried.unchanged);
    assert_eq!(retried.skipped, 0);
    assert_eq!(symbol_count(&conn, "locked_fn"), 1);
    assert!(skipped_rows(&conn).is_empty());
    let fresh = doctor(&conn, root, &root.join("g.db")).unwrap();
    assert_eq!(fresh.verdict, "FRESH");
}

#[cfg(unix)]
#[test]
fn skipped_file_replaced_by_fifo_or_symlink_does_not_hang_doctor_or_sync() {
    use std::os::unix::fs::PermissionsExt;
    use std::sync::mpsc;

    for replacement in ["fifo", "symlink"] {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().to_path_buf();
        write_file(root.join("core.py"), "def helper():\n    return 1\n");
        write_file(root.join("locked.py"), "def locked_fn():\n    return 2\n");
        let locked = root.join("locked.py");
        fs::set_permissions(&locked, fs::Permissions::from_mode(0o000)).unwrap();
        if fs::read(&locked).is_ok() {
            return; // Running as root: the io_error skip cannot be reproduced.
        }
        let conn = open_graph(&root);
        sync_repo(&conn, &root).unwrap();
        assert_eq!(
            skipped_rows(&conn),
            vec![("locked.py".to_string(), "io_error".to_string())]
        );
        drop(conn);

        fs::remove_file(&locked).unwrap();
        if replacement == "fifo" {
            let made = std::process::Command::new("mkfifo").arg(&locked).status();
            if !made.is_ok_and(|status| status.success()) {
                continue; // mkfifo unavailable.
            }
        } else {
            std::os::unix::fs::symlink(root.join("core.py"), &locked).unwrap();
        }

        let (tx, rx) = mpsc::channel();
        let worker_root = root.clone();
        std::thread::spawn(move || {
            let conn = open_graph(&worker_root);
            let report = doctor(&conn, &worker_root, &worker_root.join("g.db")).unwrap();
            let summary = sync_repo(&conn, &worker_root).unwrap();
            let _ = tx.send((report.verdict, summary.skipped, skipped_rows(&conn)));
        });
        let result = rx.recv_timeout(Duration::from_secs(10));
        if result.is_err() && replacement == "fifo" {
            let _ = fs::OpenOptions::new().write(true).open(&locked);
        }
        let (verdict, skipped, rows) =
            result.unwrap_or_else(|_| panic!("doctor or sync hung on a {replacement}"));

        // The walk indexes regular files only, so the replaced path leaves
        // the graph and its skip entry clears.
        assert_eq!(verdict, "FRESH", "{replacement}");
        assert_eq!(skipped, 0, "{replacement}");
        assert!(rows.is_empty(), "{replacement}");
    }
}

#[test]
fn database_errors_still_abort_sync() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    write_file(root.join("a.py"), "def helper():\n    return 1\n");
    let conn = open_graph(root);
    sync_repo(&conn, root).unwrap();

    conn.execute("DROP TABLE imports", []).unwrap();
    write_file(root.join("b.py"), "import os\n\ndef run():\n    return 1\n");

    assert!(sync_repo(&conn, root).is_err());
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

fn write_bytes(path: impl AsRef<Path>, content: &[u8]) {
    let path = path.as_ref();
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).unwrap();
    }
    fs::write(path, content).unwrap();
}

fn symbol_count(conn: &Connection, name: &str) -> i64 {
    conn.query_row(
        "SELECT COUNT(*) FROM symbols WHERE name = ?1",
        [name],
        |row| row.get(0),
    )
    .unwrap()
}

/// (path, reason) for every recorded skipped file, ordered by path.
fn skipped_rows(conn: &Connection) -> Vec<(String, String)> {
    let mut stmt = conn
        .prepare("SELECT path, reason FROM skipped_files ORDER BY path")
        .unwrap();
    stmt.query_map([], |row| Ok((row.get(0)?, row.get(1)?)))
        .unwrap()
        .map(|row| row.unwrap())
        .collect()
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
