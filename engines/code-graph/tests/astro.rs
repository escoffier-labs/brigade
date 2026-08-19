//! Astro component files are indexed and queryable through callers/impact/affected.

use std::fs;
use std::path::Path;

use graphtrail::model::Direction;
use graphtrail::query::{affected, graph_edges, impact_edges, stats};
use graphtrail::store::{init_schema, open_db, sync_repo};

fn write(path: impl AsRef<Path>, content: &str) {
    let path = path.as_ref();
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).unwrap();
    }
    fs::write(path, content).unwrap();
}

fn astro_site() -> (tempfile::TempDir, rusqlite::Connection) {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    write(
        root.join("site/src/layouts/Page.astro"),
        r#"---
export function layoutTitle() {
  return "page";
}
---
<html>
  <body>
    <slot />
  </body>
</html>
"#,
    );
    write(
        root.join("site/src/pages/index.astro"),
        r#"---
import Layout from "../layouts/Page.astro";
import { layoutTitle } from "../layouts/Page.astro";

export function home() {
  return layoutTitle();
}
---
<Layout>
  <h1>Hello</h1>
</Layout>
"#,
    );
    write(
        root.join("site/src/styles/global.css"),
        "body { margin: 0; }\n",
    );

    let conn = open_db(&root.join("graphtrail.db")).unwrap();
    init_schema(&conn).unwrap();
    sync_repo(&conn, root).unwrap();
    (dir, conn)
}

#[test]
fn astro_files_are_indexed_and_css_is_skipped() {
    let (_dir, conn) = astro_site();
    let report = stats(&conn).unwrap();
    assert_eq!(report.language_files.get("astro").copied(), Some(2));
    assert!(!report.language_files.contains_key("css"));

    let indexed: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM files WHERE path = 'site/src/layouts/Page.astro'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(indexed, 1);

    let css: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM files WHERE path LIKE '%.css'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(css, 0);
}

#[test]
fn astro_imports_and_symbols_resolve_across_components() {
    let (_dir, conn) = astro_site();

    let layout_file: String = conn
        .query_row(
            r#"
            SELECT dst.file_path
            FROM edges e
            JOIN symbols src ON src.id = e.source
            JOIN symbols dst ON dst.id = e.target
            WHERE src.name = 'home' AND dst.name = 'layoutTitle'
            "#,
            [],
            |row| row.get(0),
        )
        .expect("home -> layoutTitle edge");
    assert_eq!(layout_file, "site/src/layouts/Page.astro");

    let component_file: String = conn
        .query_row(
            r#"
            SELECT dst.file_path
            FROM edges e
            JOIN symbols src ON src.id = e.source
            JOIN symbols dst ON dst.id = e.target
            WHERE src.name = 'index' AND dst.name = 'Page'
            "#,
            [],
            |row| row.get(0),
        )
        .expect("index template -> Page component edge");
    assert_eq!(component_file, "site/src/layouts/Page.astro");
}

#[test]
fn astro_layout_is_queryable_through_affected_callers_and_impact() {
    let (_dir, conn) = astro_site();

    let report = affected(&conn, &["site/src/layouts/Page.astro".to_string()], 3).unwrap();
    assert!(
        report.missing_files.is_empty(),
        "Page.astro should be indexed, got missing {report:?}"
    );
    assert_eq!(report.changed_files, vec!["site/src/layouts/Page.astro"]);
    assert!(
        report
            .impacted_files
            .iter()
            .any(|file| file.file_path == "site/src/pages/index.astro"),
        "downstream page should be impacted: {report:?}"
    );

    let callers = graph_edges(&conn, "Page", Direction::Incoming).unwrap();
    assert!(
        callers
            .iter()
            .any(|edge| edge.source_file == "site/src/pages/index.astro"),
        "callers(Page) should include the page: {callers:?}"
    );

    let impact = impact_edges(&conn, "layoutTitle", 2).unwrap();
    assert!(
        impact
            .iter()
            .any(|edge| edge.source_file == "site/src/pages/index.astro"
                && edge.target_file == "site/src/layouts/Page.astro"),
        "impact(layoutTitle) should include the page call: {impact:?}"
    );
}
