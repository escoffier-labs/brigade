//! Command-line surface and output formatting.

use std::path::PathBuf;

use anyhow::Result;
use clap::{Parser, Subcommand};

use crate::model::{ContextPack, Direction, EdgeRow, SearchRow};
use crate::query::{
    DEFAULT_IMPACT_DEPTH, build_context_pack, build_context_pack_from_entry_points,
    context::{edge_location, symbol_location},
    file_neighbors, graph_edges_with_depth, impact_edges, normalize_depth, render_markdown,
    search_symbols_with_path, stats,
};
use crate::store::{db_path, init_schema, open_db, open_default_read_only, sync_repo_force};

#[derive(Parser)]
#[command(version, about)]
pub struct Cli {
    #[arg(long, global = true, value_name = "PATH")]
    db: Option<PathBuf>,

    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    Init {
        #[arg(default_value = ".")]
        root: PathBuf,
    },
    Sync {
        #[arg(default_value = ".")]
        root: PathBuf,
        /// Rebuild every file even if nothing changed.
        #[arg(long)]
        force: bool,
    },
    Search {
        query: String,
        #[arg(long, value_name = "PATH")]
        path: Option<String>,
        #[arg(long, default_value_t = 20)]
        limit: usize,
        #[arg(long)]
        json: bool,
    },
    Neighbors {
        path: String,
        #[arg(long)]
        json: bool,
    },
    Callers {
        symbol: String,
        #[arg(long, default_value_t = DEFAULT_IMPACT_DEPTH)]
        depth: usize,
        #[arg(long)]
        json: bool,
    },
    Callees {
        symbol: String,
        #[arg(long, default_value_t = DEFAULT_IMPACT_DEPTH)]
        depth: usize,
        #[arg(long)]
        json: bool,
    },
    Impact {
        symbol: String,
        #[arg(long, default_value_t = DEFAULT_IMPACT_DEPTH)]
        depth: usize,
        #[arg(long)]
        json: bool,
    },
    Context {
        task: String,
        #[arg(long, default_value_t = 12)]
        limit: usize,
        #[arg(long)]
        json: bool,
        /// Render the pack as Brigade-friendly markdown.
        #[arg(long)]
        markdown: bool,
        /// Use Code Search semantic hits as entry points, then rank them with graph centrality.
        #[cfg(feature = "codesearch")]
        #[arg(long)]
        blend_code_search: bool,
        #[cfg(feature = "codesearch")]
        #[arg(long, default_value_t = 0.6)]
        embed_weight: f64,
        #[cfg(feature = "codesearch")]
        #[arg(long, default_value_t = 0.4)]
        graph_weight: f64,
        /// Append read-only MiseLedger evidence links for the task and entry points.
        #[cfg(feature = "miseledger")]
        #[arg(long)]
        evidence: bool,
    },
    Stats {
        #[arg(long)]
        json: bool,
    },
    /// Blend Code Search embedding hits with graph centrality (feature: codesearch).
    #[cfg(feature = "codesearch")]
    Blend {
        query: String,
        #[arg(long, default_value_t = 10)]
        limit: usize,
        #[arg(long, default_value_t = 0.6)]
        embed_weight: f64,
        #[arg(long, default_value_t = 0.4)]
        graph_weight: f64,
        #[arg(long)]
        json: bool,
    },
    /// Surface MiseLedger evidence items mentioning a symbol/term (feature: miseledger).
    #[cfg(feature = "miseledger")]
    Links {
        term: String,
        #[arg(long, default_value_t = 10)]
        limit: usize,
        #[arg(long)]
        json: bool,
    },
}

pub fn run(cli: Cli) -> Result<()> {
    match cli.command {
        Command::Init { root } => {
            let db_path = db_path(cli.db, &root);
            let conn = open_db(&db_path)?;
            init_schema(&conn)?;
            println!("initialized {}", db_path.display());
        }
        Command::Sync { root, force } => {
            let db_path = db_path(cli.db, &root);
            let conn = open_db(&db_path)?;
            init_schema(&conn)?;
            let summary = sync_repo_force(&conn, &root, force)?;
            if summary.unchanged {
                println!(
                    "unchanged: files={} symbols={} edges={} imports={} db={}",
                    summary.files,
                    summary.symbols,
                    summary.calls,
                    summary.imports,
                    db_path.display()
                );
            } else {
                println!(
                    "indexed files={} symbols={} calls={} imports={} deleted={} db={}",
                    summary.files,
                    summary.symbols,
                    summary.calls,
                    summary.imports,
                    summary.deleted,
                    db_path.display()
                );
            }
        }
        Command::Search {
            query,
            path,
            limit,
            json,
        } => {
            let conn = open_default_read_only(cli.db)?;
            let rows = search_symbols_with_path(&conn, &query, path.as_deref(), limit)?;
            print_json_or_symbols(json, &rows)?;
        }
        Command::Neighbors { path, json } => {
            let conn = open_default_read_only(cli.db)?;
            let rows = file_neighbors(&conn, &path)?;
            if json {
                println!("{}", serde_json::to_string_pretty(&rows)?);
            } else {
                for row in &rows {
                    println!(
                        "{} incoming={} outgoing={}",
                        row.file_path, row.incoming_edges, row.outgoing_edges
                    );
                }
            }
        }
        Command::Callers {
            symbol,
            depth,
            json,
        } => {
            let conn = open_default_read_only(cli.db)?;
            let edges = graph_edges_with_depth(
                &conn,
                &symbol,
                Direction::Incoming,
                normalize_depth(depth),
            )?;
            print_json_or_edges(json, &edges)?;
        }
        Command::Callees {
            symbol,
            depth,
            json,
        } => {
            let conn = open_default_read_only(cli.db)?;
            let edges = graph_edges_with_depth(
                &conn,
                &symbol,
                Direction::Outgoing,
                normalize_depth(depth),
            )?;
            print_json_or_edges(json, &edges)?;
        }
        Command::Impact {
            symbol,
            depth,
            json,
        } => {
            let conn = open_default_read_only(cli.db)?;
            let edges = impact_edges(&conn, &symbol, normalize_depth(depth))?;
            print_json_or_edges(json, &edges)?;
        }
        Command::Context {
            task,
            limit,
            json,
            markdown,
            #[cfg(feature = "codesearch")]
            blend_code_search,
            #[cfg(feature = "codesearch")]
            embed_weight,
            #[cfg(feature = "codesearch")]
            graph_weight,
            #[cfg(feature = "miseledger")]
            evidence,
        } => {
            let conn = open_default_read_only(cli.db)?;
            #[cfg(feature = "codesearch")]
            let pack = if blend_code_search {
                let client = crate::adapters::codesearch::CodeSearchClient::from_env();
                let hits = client.search(&task, limit.max(20))?;
                let rows = crate::query::blend(&conn, &hits, embed_weight, graph_weight, limit)?;
                let entry_points = rows.into_iter().map(|row| row.symbol).collect();
                build_context_pack_from_entry_points(&conn, task.clone(), entry_points)?
            } else {
                build_context_pack(&conn, task.clone(), limit)?
            };
            #[cfg(not(feature = "codesearch"))]
            let pack = build_context_pack(&conn, task.clone(), limit)?;
            if json {
                println!("{}", serde_json::to_string_pretty(&pack)?);
            } else if markdown {
                print!("{}", render_markdown(&pack));
                #[cfg(feature = "miseledger")]
                if evidence {
                    print!("{}", render_evidence_links(&task, &pack, limit)?);
                }
            } else {
                print!("{}", render_context(&pack));
                #[cfg(feature = "miseledger")]
                if evidence {
                    print!("{}", render_evidence_links(&task, &pack, limit)?);
                }
            }
        }
        Command::Stats { json } => {
            let conn = open_default_read_only(cli.db)?;
            let stats = stats(&conn)?;
            if json {
                println!("{}", serde_json::to_string_pretty(&stats)?);
            } else {
                println!("schema_version: {}", stats.schema_version);
                println!("files: {}", stats.files);
                println!("symbols: {}", stats.symbols);
                println!("edges: {}", stats.edges);
                println!("imports: {}", stats.imports);
                if let Some(synced_at) = stats.synced_at {
                    println!("synced_at: {synced_at}");
                }
                if let Some(tool_version) = stats.tool_version {
                    println!("tool_version: {tool_version}");
                }
                for (language, files) in stats.language_files {
                    println!("language_files.{language}: {files}");
                }
            }
        }
        #[cfg(feature = "codesearch")]
        Command::Blend {
            query,
            limit,
            embed_weight,
            graph_weight,
            json,
        } => {
            let conn = open_default_read_only(cli.db)?;
            let client = crate::adapters::codesearch::CodeSearchClient::from_env();
            let hits = client.search(&query, limit.max(20))?;
            let rows = crate::query::blend(&conn, &hits, embed_weight, graph_weight, limit)?;
            if json {
                println!("{}", serde_json::to_string_pretty(&rows)?);
            } else {
                for row in &rows {
                    println!(
                        "{:.3} {} {}:{} (embed {:.3}, graph {:.3})",
                        row.blended_score,
                        row.symbol.qualified_name,
                        row.symbol.file_path,
                        row.symbol.start_line,
                        row.embedding_score,
                        row.graph_score
                    );
                }
            }
        }
        #[cfg(feature = "miseledger")]
        Command::Links { term, limit, json } => {
            let db = crate::adapters::miseledger::default_db_path();
            let hits = crate::adapters::miseledger::search_evidence(&db, &term, limit)?;
            if json {
                println!("{}", serde_json::to_string_pretty(&hits)?);
            } else {
                for hit in &hits {
                    println!("[{}] {} — {}", hit.source_kind, hit.item_id, hit.snippet);
                }
            }
        }
    }

    Ok(())
}

fn print_json_or_symbols(json: bool, rows: &[SearchRow]) -> Result<()> {
    if json {
        println!("{}", serde_json::to_string_pretty(rows)?);
        return Ok(());
    }
    for row in rows {
        println!(
            "{} {} {}:{} {}",
            row.kind, row.qualified_name, row.file_path, row.start_line, row.signature
        );
    }
    Ok(())
}

fn print_json_or_edges(json: bool, rows: &[EdgeRow]) -> Result<()> {
    if json {
        println!("{}", serde_json::to_string_pretty(rows)?);
        return Ok(());
    }
    for row in rows {
        println!(
            "{} --{}@{} hops={}--> {}  ({} -> {})",
            row.source,
            row.kind,
            row.line.unwrap_or_default(),
            row.hops,
            row.target,
            row.source_file,
            row.target_file
        );
    }
    Ok(())
}

fn render_context(pack: &ContextPack) -> String {
    use std::fmt::Write;

    let mut text = String::new();
    let _ = writeln!(text, "# Context\n");
    let _ = writeln!(text, "task: {}\n", pack.task);
    let _ = writeln!(text, "## Entry Points");
    for row in &pack.entry_points {
        let _ = writeln!(
            text,
            "- {} `{}` at {}",
            row.kind,
            row.qualified_name,
            symbol_location(row)
        );
    }
    let _ = writeln!(text, "\n## Callers");
    for edge in &pack.callers {
        let _ = writeln!(
            text,
            "- `{}` calls `{}` at {}",
            edge.source,
            edge.target,
            edge_location(edge)
        );
    }
    let _ = writeln!(text, "\n## Callees");
    for edge in &pack.callees {
        let _ = writeln!(
            text,
            "- `{}` calls `{}` at {}",
            edge.source,
            edge.target,
            edge_location(edge)
        );
    }
    let _ = writeln!(text, "\n## Related Files");
    for file in &pack.related_files {
        let _ = writeln!(text, "- {file}");
    }
    text
}

#[cfg(feature = "miseledger")]
fn render_evidence_links(task: &str, pack: &ContextPack, limit: usize) -> Result<String> {
    use std::collections::BTreeSet;
    use std::fmt::Write;

    let db = crate::adapters::miseledger::default_db_path();
    let mut terms = BTreeSet::new();
    terms.insert(task.to_string());
    for row in &pack.entry_points {
        terms.insert(row.qualified_name.clone());
        terms.insert(row.name.clone());
    }

    let mut md = String::new();
    let _ = writeln!(md, "\n## Evidence links\n");
    let mut written = 0usize;
    for term in terms {
        if written >= limit {
            break;
        }
        let hits = crate::adapters::miseledger::search_evidence(&db, &term, 3)?;
        if hits.is_empty() {
            continue;
        }
        let _ = writeln!(md, "### `{term}`\n");
        for hit in hits {
            if written >= limit {
                break;
            }
            let _ = writeln!(
                md,
                "- [{}] `{}` - {}",
                hit.source_kind, hit.item_id, hit.snippet
            );
            written += 1;
        }
        md.push('\n');
    }
    if written == 0 {
        let _ = writeln!(md, "_none_\n");
    }
    Ok(md)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::store::SCHEMA_VERSION;

    fn sample_pack() -> ContextPack {
        ContextPack {
            schema_version: SCHEMA_VERSION,
            task: "wire context".to_string(),
            entry_points: vec![SearchRow {
                id: "sym-run".to_string(),
                kind: "function".to_string(),
                name: "run".to_string(),
                qualified_name: "run".to_string(),
                file_path: "app.py".to_string(),
                start_line: 5,
                end_line: 7,
                signature: "def run():".to_string(),
                score: 1.0,
            }],
            callers: vec![EdgeRow {
                source_id: "sym-main".to_string(),
                source: "main".to_string(),
                target_id: "sym-run".to_string(),
                target: "run".to_string(),
                kind: "call".to_string(),
                line: Some(12),
                source_file: "cli.py".to_string(),
                target_file: "app.py".to_string(),
                hops: 1,
            }],
            callees: vec![EdgeRow {
                source_id: "sym-run".to_string(),
                source: "run".to_string(),
                target_id: "sym-helper".to_string(),
                target: "helper".to_string(),
                kind: "call".to_string(),
                line: Some(6),
                source_file: "app.py".to_string(),
                target_file: "lib.py".to_string(),
                hops: 1,
            }],
            related_files: vec![
                "app.py".to_string(),
                "cli.py".to_string(),
                "lib.py".to_string(),
            ],
        }
    }

    #[test]
    fn cli_context_renders_ranges_and_edge_locations() {
        let text = render_context(&sample_pack());

        assert!(text.contains("- function `run` at app.py:5-7"));
        assert!(text.contains("- `main` calls `run` at cli.py:12 -> app.py"));
        assert!(text.contains("- `run` calls `helper` at app.py:6 -> lib.py"));
    }
}
