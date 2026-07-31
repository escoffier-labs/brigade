//! Contract tests for the bounded, file-rooted static HTML map query.

use graphtrail::query::{MapDirection, MapOptions, export_html_map};
use graphtrail::store::init_schema;
use rusqlite::{Connection, params};
use serde_json::Value;
use std::collections::BTreeSet;

const MAP_DATA_OPEN: &str = r#"<script id="graphtrail-map-data" type="application/json">"#;
const MAP_DATA_CLOSE: &str = "</script>";

#[derive(Clone, Copy)]
struct Symbol<'a> {
    id: &'a str,
    name: &'a str,
    qualified_name: &'a str,
    file_path: &'a str,
    start_line: i64,
}

#[derive(Clone, Copy)]
struct Edge<'a> {
    source: &'a str,
    target: &'a str,
    kind: &'a str,
    line: i64,
}

fn connection(files: &[&str], symbols: &[Symbol<'_>], edges: &[Edge<'_>]) -> Connection {
    let conn = Connection::open_in_memory().unwrap();
    init_schema(&conn).unwrap();

    for path in files {
        conn.execute(
            "INSERT INTO files (path, content_hash, size, modified_at, indexed_at, language) \
             VALUES (?1, 'fixture', 0, 0, 0, 'rust')",
            [path],
        )
        .unwrap();
    }
    for symbol in symbols {
        conn.execute(
            "INSERT INTO symbols \
             (id, kind, name, qualified_name, file_path, start_line, end_line, signature, content_hash) \
             VALUES (?1, 'function', ?2, ?3, ?4, ?5, ?5, '', 'fixture')",
            params![
                symbol.id,
                symbol.name,
                symbol.qualified_name,
                symbol.file_path,
                symbol.start_line,
            ],
        )
        .unwrap();
    }
    for edge in edges {
        conn.execute(
            "INSERT INTO edges (source, target, kind, line, confidence) VALUES (?1, ?2, ?3, ?4, 1.0)",
            params![edge.source, edge.target, edge.kind, edge.line],
        )
        .unwrap();
    }
    conn
}

fn fixture(reverse_insert_order: bool) -> Connection {
    let files = [
        "src/caller.rs",
        "src/focus.rs",
        "src/callee.rs",
        "src/neighbor.rs",
    ];
    let symbols = [
        Symbol {
            id: "caller",
            name: "caller",
            qualified_name: "caller::entry",
            file_path: "src/caller.rs",
            start_line: 8,
        },
        Symbol {
            id: "focus_a",
            name: "focus_a",
            qualified_name: "focus::alpha",
            file_path: "src/focus.rs",
            start_line: 4,
        },
        Symbol {
            id: "focus_b",
            name: "focus_b",
            qualified_name: "focus::beta",
            file_path: "src/focus.rs",
            start_line: 18,
        },
        Symbol {
            id: "callee",
            name: "callee",
            qualified_name: "callee::work",
            file_path: "src/callee.rs",
            start_line: 12,
        },
        Symbol {
            id: "neighbor",
            name: "neighbor",
            qualified_name: "neighbor::deep",
            file_path: "src/neighbor.rs",
            start_line: 3,
        },
    ];
    let edges = [
        Edge {
            source: "caller",
            target: "focus_a",
            kind: "call",
            line: 21,
        },
        Edge {
            source: "focus_a",
            target: "callee",
            kind: "call",
            line: 9,
        },
        Edge {
            source: "focus_b",
            target: "callee",
            kind: "call",
            line: 22,
        },
        Edge {
            source: "callee",
            target: "neighbor",
            kind: "call",
            line: 16,
        },
    ];

    if reverse_insert_order {
        let mut reverse_files = files.to_vec();
        let mut reverse_symbols = symbols.to_vec();
        let mut reverse_edges = edges.to_vec();
        reverse_files.reverse();
        reverse_symbols.reverse();
        reverse_edges.reverse();
        connection(&reverse_files, &reverse_symbols, &reverse_edges)
    } else {
        connection(&files, &symbols, &edges)
    }
}

fn map_data(html: &str) -> Value {
    let start = html
        .find(MAP_DATA_OPEN)
        .expect("map data element must have a stable id")
        + MAP_DATA_OPEN.len();
    let end = html[start..]
        .find(MAP_DATA_CLOSE)
        .map(|offset| start + offset)
        .expect("map data element must close");
    serde_json::from_str(&html[start..end]).expect("map data must be valid JSON")
}

fn ids(data: &Value) -> BTreeSet<&str> {
    data["nodes"]
        .as_array()
        .expect("map data must contain nodes")
        .iter()
        .map(|node| node["id"].as_str().expect("node id must be text"))
        .collect()
}

fn shell(html: &str) -> String {
    let start = html
        .find(MAP_DATA_OPEN)
        .expect("map data element must have a stable id")
        + MAP_DATA_OPEN.len();
    let end = html[start..]
        .find(MAP_DATA_CLOSE)
        .map(|offset| start + offset)
        .expect("map data element must close");
    format!("{}{{}}{}", &html[..start], &html[end..])
}

fn options(direction: MapDirection, depth: u8, max_nodes: usize, max_edges: usize) -> MapOptions {
    MapOptions {
        direction,
        depth,
        max_nodes,
        max_edges,
    }
}

#[test]
fn defaults_are_neighbor_depth_one_with_bounded_output() {
    let defaults = MapOptions::default();

    assert_eq!(defaults.direction, MapDirection::Neighbors);
    assert_eq!(defaults.depth, 1);
    assert_eq!(defaults.max_nodes, 100);
    assert_eq!(defaults.max_edges, 250);
}

#[test]
fn map_is_rooted_at_file_symbols_and_respects_direction_and_depth() {
    let conn = fixture(false);

    let neighbors = export_html_map(&conn, "src/focus.rs", MapOptions::default()).unwrap();
    let neighbor_data = map_data(&neighbors);
    assert_eq!(
        ids(&neighbor_data),
        BTreeSet::from(["caller", "focus_a", "focus_b", "callee"]),
        "depth one includes both incoming and outgoing calls, but not the next hop"
    );
    assert_eq!(neighbor_data["focus_path"], "src/focus.rs");
    assert_eq!(neighbor_data["direction"], "neighbors");
    assert_eq!(neighbor_data["depth"], 1);

    let callers = export_html_map(
        &conn,
        "src/focus.rs",
        options(MapDirection::Callers, 1, 100, 250),
    )
    .unwrap();
    assert_eq!(
        ids(&map_data(&callers)),
        BTreeSet::from(["caller", "focus_a", "focus_b"])
    );

    let callees = export_html_map(
        &conn,
        "src/focus.rs",
        options(MapDirection::Callees, 2, 100, 250),
    )
    .unwrap();
    assert_eq!(
        ids(&map_data(&callees)),
        BTreeSet::from(["focus_a", "focus_b", "callee", "neighbor"])
    );
}

#[test]
fn map_data_includes_symbols_paths_and_call_site_labels() {
    let conn = fixture(false);
    let html = export_html_map(&conn, "src/focus.rs", MapOptions::default()).unwrap();
    let data = map_data(&html);

    assert!(html.contains("focus::alpha"));
    assert!(html.contains("caller::entry"));
    assert!(html.contains("callee::work"));
    assert!(html.contains("src/focus.rs"));
    assert!(html.contains("src/caller.rs"));
    assert!(html.contains("src/callee.rs"));
    assert!(
        data["edges"]
            .as_array()
            .unwrap()
            .iter()
            .any(|edge| edge["kind"] == "call" && edge["line"] == 21)
    );
}

#[test]
fn legacy_edges_schema_without_confidence_exports_null_edge_confidence() {
    let conn = fixture(false);
    conn.execute_batch("ALTER TABLE edges DROP COLUMN confidence")
        .expect("fixture schema must support the pre-confidence edges layout");

    let html = export_html_map(&conn, "src/focus.rs", MapOptions::default())
        .expect("legacy edges schema must remain exportable");

    for edge in map_data(&html)["edges"]
        .as_array()
        .expect("map data must contain edges")
    {
        assert!(
            edge["confidence"].is_null(),
            "legacy edge confidence must serialize as null: {edge}"
        );
    }
}

#[test]
fn empty_indexed_file_renders_a_valid_explicit_empty_state() {
    let conn = connection(&["src/empty.rs"], &[], &[]);

    let html = export_html_map(&conn, "src/empty.rs", MapOptions::default()).unwrap();

    assert!(html.starts_with("<!doctype html>"));
    assert!(html.contains("No indexed symbols in ${graph.focus_path}."));
    assert_eq!(map_data(&html)["empty"], true);
    assert!(map_data(&html)["nodes"].as_array().unwrap().is_empty());
    assert!(map_data(&html)["edges"].as_array().unwrap().is_empty());
}

#[test]
fn selected_node_and_edge_limits_cap_data_and_report_omissions() {
    let conn = fixture(false);
    let html = export_html_map(
        &conn,
        "src/focus.rs",
        options(MapDirection::Neighbors, 2, 2, 1),
    )
    .unwrap();
    let data = map_data(&html);

    assert!(data["nodes"].as_array().unwrap().len() <= 2);
    assert!(data["edges"].as_array().unwrap().len() <= 1);
    assert_eq!(data["status"]["rendered_nodes"], 2);
    assert_eq!(data["status"]["omitted_nodes"], 3);
    assert_eq!(data["status"]["rendered_edges"], 0);
    assert_eq!(data["status"]["omitted_edges"], 4);
    let node_ids = ids(&data);
    for edge in data["edges"].as_array().unwrap() {
        assert!(node_ids.contains(edge["source"].as_str().unwrap()));
        assert!(node_ids.contains(edge["target"].as_str().unwrap()));
    }
    assert!(html.contains("role=\"status\""));
    assert!(html.contains("nodes rendered, ${graph.status.omitted_nodes} omitted"));
    assert!(html.contains("edges rendered, ${graph.status.omitted_edges} omitted"));
}

#[test]
fn hostile_graph_text_is_json_escaped_and_only_rendered_as_text() {
    let hostile_path = "src/<script>alert(1)</script>&\".rs";
    let hostile_name = "bad </script><img src=x onerror=alert(1)> & \"quoted\"";
    let conn = connection(
        &[hostile_path, "src/focus.rs"],
        &[
            Symbol {
                id: "focus",
                name: "focus",
                qualified_name: "focus",
                file_path: "src/focus.rs",
                start_line: 1,
            },
            Symbol {
                id: "hostile",
                name: hostile_name,
                qualified_name: hostile_name,
                file_path: hostile_path,
                start_line: 2,
            },
        ],
        &[Edge {
            source: "focus",
            target: "hostile",
            kind: "call </script><b>unsafe</b> & \"quoted\"",
            line: 7,
        }],
    );

    let html = export_html_map(&conn, "src/focus.rs", MapOptions::default()).unwrap();

    assert!(!html.contains("</script><img"));
    assert!(!html.contains("</script><b>unsafe"));
    assert!(!html.contains("<script>alert(1)</script>"));
    assert!(html.contains("\\u003c"));
    assert!(html.contains("\\u003e"));
    assert!(html.contains("\\u0026"));
    assert!(!html.contains("innerHTML"));
    assert!(html.contains("textContent"));
    assert_eq!(map_data(&html)["nodes"][1]["qualified_name"], hostile_name);
}

#[test]
fn equivalent_graphs_inserted_in_different_orders_produce_identical_html() {
    let first = fixture(false);
    let second = fixture(true);
    let options = options(MapDirection::Neighbors, 2, 100, 250);

    assert_eq!(
        export_html_map(&first, "src/focus.rs", options).unwrap(),
        export_html_map(&second, "src/focus.rs", options).unwrap()
    );
}

#[test]
fn document_shell_matches_snapshot_and_has_no_external_surface() {
    let conn = fixture(false);
    let html = export_html_map(&conn, "src/focus.rs", MapOptions::default()).unwrap();

    for required in [
        "<h2 id=\"map-focus-heading\">Focus file</h2>",
        "<h2 id=\"map-callers-heading\">Callers (incoming relationships)</h2>",
        "<h2 id=\"map-callees-heading\">Callees (outgoing relationships)</h2>",
        "<h2 id=\"map-additional-neighbors-heading\">Additional neighbors</h2>",
        "<h2 id=\"map-relationships-heading\">Relationships (source → target)</h2>",
        "id=\"map-focus-lane\"",
        "id=\"map-callers-lane\"",
        "id=\"map-callees-lane\"",
        "id=\"map-additional-neighbors-lane\"",
        "id=\"map-relationships-lane\"",
        "id=\"map-relationship-list\"",
        "const focusNodes = graph.nodes.filter((node) => node.file_path === graph.focus_path);",
        "const callerIds = new Set(edges.filter((edge) => focusIds.has(edge.target)).map((edge) => edge.source));",
        "const calleeIds = new Set(edges.filter((edge) => focusIds.has(edge.source)).map((edge) => edge.target));",
        "const callers = graph.nodes.filter((node) => !focusIds.has(node.id) && callerIds.has(node.id));",
        "const callees = graph.nodes.filter((node) => !focusIds.has(node.id) && calleeIds.has(node.id));",
        "const additionalNeighbors = graph.nodes.filter((node) => !focusIds.has(node.id) && !callerIds.has(node.id) && !calleeIds.has(node.id));",
        "button.setAttribute('role', 'treeitem');",
        "button.type = 'button';",
        "button:focus-visible",
        "event.key === 'Enter'",
        "event.key === ' '",
        "textContent = `${source.qualified_name} ${edge.kind} (line ${edge.line}) → ${target.qualified_name}`;",
        "No focus-file symbols.",
        "No direct callers.",
        "No direct callees.",
        "No additional neighbors.",
        "No selected relationships.",
    ] {
        assert!(html.contains(required), "must include {required}");
    }
    assert!(!html.contains("details.focus()"));
    assert_eq!(shell(&html), include_str!("snapshots/map_shell.html"));
    assert_eq!(html.matches("<style>").count(), 1);
    assert_eq!(html.matches("<script").count(), 2);
    assert!(html.contains(MAP_DATA_OPEN));
    assert!(html.contains("<main id=\"map-main\" tabindex=\"-1\">"));
    assert!(html.contains("role=\"status\" aria-live=\"polite\""));
    assert!(html.contains("role=\"tree\""));
    assert!(html.contains("ArrowDown"));
    assert!(html.contains("ArrowUp"));
    assert!(html.contains("event.key === 'Enter'"));
    assert!(html.contains("event.key === ' '"));
    assert!(html.contains("button:focus-visible"));
    assert!(html.contains("@media (prefers-color-scheme: dark)"));
    assert!(html.contains("@media (prefers-reduced-motion: reduce)"));
    assert!(!html.contains("innerHTML"));
    assert!(!html.contains("insertAdjacentHTML"));
    for prohibited in ["http://", "https://", "//cdn", "@import", "telemetry"] {
        assert!(!html.contains(prohibited), "must not include {prohibited}");
    }
}

#[test]
fn invalid_bounds_and_missing_focus_path_return_clear_errors() {
    let conn = fixture(false);

    for (label, invalid) in [
        ("depth", options(MapDirection::Neighbors, 0, 100, 250)),
        ("depth", options(MapDirection::Neighbors, 6, 100, 250)),
        ("max_nodes", options(MapDirection::Neighbors, 1, 0, 250)),
        ("max_nodes", options(MapDirection::Neighbors, 1, 251, 250)),
        ("max_edges", options(MapDirection::Neighbors, 1, 100, 0)),
        ("max_edges", options(MapDirection::Neighbors, 1, 100, 501)),
    ] {
        let error = export_html_map(&conn, "src/focus.rs", invalid).unwrap_err();
        assert!(
            error.to_string().contains(label),
            "{label} error should name the invalid option: {error:#}"
        );
    }

    let error = export_html_map(&conn, "src/missing.rs", MapOptions::default()).unwrap_err();
    assert!(error.to_string().contains("src/missing.rs"));
    assert!(error.to_string().contains("indexed focus file"));
}
