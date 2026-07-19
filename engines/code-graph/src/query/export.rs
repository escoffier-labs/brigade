//! Graph export for visualization tools: Graphviz dot, GraphML, JSON Lines.
//!
//! Read-only by construction: the export walks the symbols and edges tables
//! and renders text. File scope aggregates call edges between files (readable
//! for whole repos); symbol scope emits every node and edge.

use anyhow::Result;
use rusqlite::Connection;
use serde::Serialize;
use std::fmt::Write as _;

#[derive(Clone, Copy, Debug, PartialEq, Eq, clap::ValueEnum)]
pub enum ExportFormat {
    Dot,
    Graphml,
    Jsonl,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, clap::ValueEnum)]
pub enum ExportScope {
    /// One node per file; edges aggregate cross-file calls with a count.
    Files,
    /// One node per symbol; edges carry line and confidence.
    Symbols,
}

#[derive(Serialize)]
struct SymbolNode {
    r#type: &'static str,
    id: String,
    qualified_name: String,
    kind: String,
    file_path: String,
    start_line: i64,
}

#[derive(Serialize)]
struct SymbolEdge {
    r#type: &'static str,
    source: String,
    target: String,
    line: Option<i64>,
    confidence: Option<f64>,
}

#[derive(Serialize)]
struct FileNode {
    r#type: &'static str,
    id: String,
    language: String,
    symbols: i64,
}

#[derive(Serialize)]
struct FileEdge {
    r#type: &'static str,
    source: String,
    target: String,
    calls: i64,
}

pub fn export_graph(conn: &Connection, format: ExportFormat, scope: ExportScope) -> Result<String> {
    match scope {
        ExportScope::Files => {
            let (nodes, edges) = file_graph(conn)?;
            Ok(match format {
                ExportFormat::Dot => file_dot(&nodes, &edges),
                ExportFormat::Graphml => file_graphml(&nodes, &edges),
                ExportFormat::Jsonl => jsonl(&nodes, &edges)?,
            })
        }
        ExportScope::Symbols => {
            let (nodes, edges) = symbol_graph(conn)?;
            Ok(match format {
                ExportFormat::Dot => symbol_dot(&nodes, &edges),
                ExportFormat::Graphml => symbol_graphml(&nodes, &edges),
                ExportFormat::Jsonl => jsonl(&nodes, &edges)?,
            })
        }
    }
}

fn file_graph(conn: &Connection) -> Result<(Vec<FileNode>, Vec<FileEdge>)> {
    let mut nodes = Vec::new();
    let mut stmt = conn.prepare(
        "SELECT f.path, f.language, COUNT(s.id) FROM files f
         LEFT JOIN symbols s ON s.file_path = f.path
         GROUP BY f.path ORDER BY f.path",
    )?;
    let rows = stmt.query_map([], |row| {
        Ok(FileNode {
            r#type: "node",
            id: row.get(0)?,
            language: row.get(1)?,
            symbols: row.get(2)?,
        })
    })?;
    for row in rows {
        nodes.push(row?);
    }

    let mut edges = Vec::new();
    let mut stmt = conn.prepare(
        "SELECT src.file_path, dst.file_path, COUNT(*) FROM edges e
         JOIN symbols src ON src.id = e.source
         JOIN symbols dst ON dst.id = e.target
         WHERE src.file_path <> dst.file_path
         GROUP BY src.file_path, dst.file_path
         ORDER BY src.file_path, dst.file_path",
    )?;
    let rows = stmt.query_map([], |row| {
        Ok(FileEdge {
            r#type: "edge",
            source: row.get(0)?,
            target: row.get(1)?,
            calls: row.get(2)?,
        })
    })?;
    for row in rows {
        edges.push(row?);
    }
    Ok((nodes, edges))
}

fn symbol_graph(conn: &Connection) -> Result<(Vec<SymbolNode>, Vec<SymbolEdge>)> {
    let mut nodes = Vec::new();
    let mut stmt = conn.prepare(
        "SELECT id, qualified_name, kind, file_path, start_line FROM symbols
         ORDER BY file_path, start_line, id",
    )?;
    let rows = stmt.query_map([], |row| {
        Ok(SymbolNode {
            r#type: "node",
            id: row.get(0)?,
            qualified_name: row.get(1)?,
            kind: row.get(2)?,
            file_path: row.get(3)?,
            start_line: row.get(4)?,
        })
    })?;
    for row in rows {
        nodes.push(row?);
    }

    let has_confidence = crate::store::schema::table_has_column(conn, "edges", "confidence")?;
    let sql = if has_confidence {
        "SELECT source, target, line, confidence FROM edges ORDER BY source, target, line"
    } else {
        "SELECT source, target, line, NULL FROM edges ORDER BY source, target, line"
    };
    let mut edges = Vec::new();
    let mut stmt = conn.prepare(sql)?;
    let rows = stmt.query_map([], |row| {
        Ok(SymbolEdge {
            r#type: "edge",
            source: row.get(0)?,
            target: row.get(1)?,
            line: row.get(2)?,
            confidence: row.get(3)?,
        })
    })?;
    for row in rows {
        edges.push(row?);
    }
    Ok((nodes, edges))
}

fn jsonl<N: Serialize, E: Serialize>(nodes: &[N], edges: &[E]) -> Result<String> {
    let mut text = String::new();
    for node in nodes {
        let _ = writeln!(text, "{}", serde_json::to_string(node)?);
    }
    for edge in edges {
        let _ = writeln!(text, "{}", serde_json::to_string(edge)?);
    }
    Ok(text)
}

fn dot_escape(value: &str) -> String {
    value.replace('\\', "\\\\").replace('"', "\\\"")
}

fn file_dot(nodes: &[FileNode], edges: &[FileEdge]) -> String {
    let mut text = String::from("digraph graphtrail {\n  rankdir=LR;\n  node [shape=box];\n");
    for node in nodes {
        let _ = writeln!(
            text,
            "  \"{}\" [label=\"{}\\n{} symbols\"];",
            dot_escape(&node.id),
            dot_escape(&node.id),
            node.symbols
        );
    }
    for edge in edges {
        let _ = writeln!(
            text,
            "  \"{}\" -> \"{}\" [label=\"{}\"];",
            dot_escape(&edge.source),
            dot_escape(&edge.target),
            edge.calls
        );
    }
    text.push_str("}\n");
    text
}

fn symbol_dot(nodes: &[SymbolNode], edges: &[SymbolEdge]) -> String {
    let mut text = String::from("digraph graphtrail {\n  rankdir=LR;\n");
    for node in nodes {
        let _ = writeln!(
            text,
            "  \"{}\" [label=\"{}\"];",
            dot_escape(&node.id),
            dot_escape(&node.qualified_name)
        );
    }
    for edge in edges {
        let _ = writeln!(
            text,
            "  \"{}\" -> \"{}\";",
            dot_escape(&edge.source),
            dot_escape(&edge.target)
        );
    }
    text.push_str("}\n");
    text
}

fn xml_escape(value: &str) -> String {
    value
        .replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
}

const GRAPHML_HEADER: &str = concat!(
    "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n",
    "<graphml xmlns=\"http://graphml.graphdrawing.org/xmlns\">\n"
);

fn file_graphml(nodes: &[FileNode], edges: &[FileEdge]) -> String {
    let mut text = String::from(GRAPHML_HEADER);
    text.push_str(
        "  <key id=\"language\" for=\"node\" attr.name=\"language\" attr.type=\"string\"/>\n",
    );
    text.push_str(
        "  <key id=\"symbols\" for=\"node\" attr.name=\"symbols\" attr.type=\"long\"/>\n",
    );
    text.push_str("  <key id=\"calls\" for=\"edge\" attr.name=\"calls\" attr.type=\"long\"/>\n");
    text.push_str("  <graph edgedefault=\"directed\">\n");
    for node in nodes {
        let _ = writeln!(
            text,
            "    <node id=\"{}\"><data key=\"language\">{}</data><data key=\"symbols\">{}</data></node>",
            xml_escape(&node.id),
            xml_escape(&node.language),
            node.symbols
        );
    }
    for (idx, edge) in edges.iter().enumerate() {
        let _ = writeln!(
            text,
            "    <edge id=\"e{}\" source=\"{}\" target=\"{}\"><data key=\"calls\">{}</data></edge>",
            idx,
            xml_escape(&edge.source),
            xml_escape(&edge.target),
            edge.calls
        );
    }
    text.push_str("  </graph>\n</graphml>\n");
    text
}

fn symbol_graphml(nodes: &[SymbolNode], edges: &[SymbolEdge]) -> String {
    let mut text = String::from(GRAPHML_HEADER);
    text.push_str("  <key id=\"qualified_name\" for=\"node\" attr.name=\"qualified_name\" attr.type=\"string\"/>\n");
    text.push_str("  <key id=\"kind\" for=\"node\" attr.name=\"kind\" attr.type=\"string\"/>\n");
    text.push_str(
        "  <key id=\"file_path\" for=\"node\" attr.name=\"file_path\" attr.type=\"string\"/>\n",
    );
    text.push_str("  <key id=\"line\" for=\"edge\" attr.name=\"line\" attr.type=\"long\"/>\n");
    text.push_str(
        "  <key id=\"confidence\" for=\"edge\" attr.name=\"confidence\" attr.type=\"double\"/>\n",
    );
    text.push_str("  <graph edgedefault=\"directed\">\n");
    for node in nodes {
        let _ = writeln!(
            text,
            "    <node id=\"{}\"><data key=\"qualified_name\">{}</data><data key=\"kind\">{}</data><data key=\"file_path\">{}</data></node>",
            xml_escape(&node.id),
            xml_escape(&node.qualified_name),
            xml_escape(&node.kind),
            xml_escape(&node.file_path)
        );
    }
    for (idx, edge) in edges.iter().enumerate() {
        let mut data = String::new();
        if let Some(line) = edge.line {
            let _ = write!(data, "<data key=\"line\">{line}</data>");
        }
        if let Some(confidence) = edge.confidence {
            let _ = write!(data, "<data key=\"confidence\">{confidence}</data>");
        }
        let _ = writeln!(
            text,
            "    <edge id=\"e{}\" source=\"{}\" target=\"{}\">{}</edge>",
            idx,
            xml_escape(&edge.source),
            xml_escape(&edge.target),
            data
        );
    }
    text.push_str("  </graph>\n</graphml>\n");
    text
}
